#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interfaz web local para la Herramienta de Asistencia para Convocatorias
Públicas. Es una capa fina sobre monitor_convocatorias.py: no reimplementa
nada, solo expone sus funciones a través de una pequeña API y sirve la
página HTML del formulario.

Ejecutar con:
    pip install -r requirements.txt
    python app.py

Luego abre http://127.0.0.1:5000 en el navegador.
"""

import io
import os
from dataclasses import asdict
from datetime import date, datetime

from flask import Flask, jsonify, render_template, request, send_file

import monitor_convocatorias as mc

app = Flask(__name__)

# Guardamos en memoria el último informe generado para poder descargarlo
# como Markdown sin tener que rehacer las búsquedas.
ULTIMO_INFORME_MD = "# Todavía no se ha generado ningún informe.\n"

# Configuración del correo remitente de las alertas: se define una sola vez
# en el servidor (variables de entorno), nunca en el formulario web. Quien
# usa la web solo indica a qué correo quiere que llegue el aviso.
#   Linux/macOS: export ALERTA_EMAIL_REMITENTE="alertas@gmail.com"
#                export ALERTA_EMAIL_PASSWORD="contraseña de aplicación"
#   Windows PowerShell: $env:ALERTA_EMAIL_REMITENTE = "alertas@gmail.com"
#                        $env:ALERTA_EMAIL_PASSWORD = "contraseña de aplicación"
EMAIL_REMITENTE = os.environ.get("ALERTA_EMAIL_REMITENTE", "alertasdiputacion@gmail.com")
EMAIL_PASSWORD = os.environ.get("ALERTA_EMAIL_PASSWORD", "jxip jlbk tjpm nmnn")
EMAIL_SMTP_SERVER = os.environ.get("ALERTA_SMTP_SERVER", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.environ.get("ALERTA_SMTP_PORT", 587))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/buscar", methods=["POST"])
def api_buscar():
    global ULTIMO_INFORME_MD

    datos = request.get_json(force=True, silent=True) or {}

    municipio = (datos.get("municipio") or "Ayuntamiento").strip()
    provincia = (datos.get("provincia") or "Toledo").strip()
    dias = max(1, min(int(datos.get("dias") or 7), 30))
    areas = [a.strip() for a in (datos.get("areas") or "").split(",") if a.strip()]
    plazo_alerta = max(1, int(datos.get("plazo_alerta") or 10))
    solo_hoy = bool(datos.get("solo_hoy"))

    fecha_elegida_str = (datos.get("fecha_elegida") or "").strip()
    try:
        fecha_elegida = datetime.strptime(fecha_elegida_str, "%d/%m/%Y").date()
    except ValueError:
        fecha_elegida = date.today()
    fecha_elegida_str = fecha_elegida.strftime("%d/%m/%Y")
    usar_ia = bool(datos.get("usar_ia"))
    gemini_key = (datos.get("gemini_key") or "").strip() or None
    gemini_modelo = (datos.get("gemini_modelo") or "gemini-2.5-flash-lite").strip()
    max_ia = max(1, min(int(datos.get("max_ia") or 10), 30))

    todas: list[mc.Convocatoria] = []
    fuentes_estado = {}

    # Cada fuente se aísla: si una falla, las demás siguen funcionando.
    for nombre_fuente, funcion in (
        ("BOE", lambda: mc.fetch_boe(dias)),
        ("DOCM", lambda: mc.fetch_docm(dias)),
        (f"BOP {provincia}", lambda: mc.fetch_bop(provincia, dias)),
    ):
        try:
            resultado = funcion()
            todas.extend(resultado)
            fuentes_estado[nombre_fuente] = {"ok": True, "detectadas": len(resultado)}
        except Exception as e:
            fuentes_estado[nombre_fuente] = {"ok": False, "error": str(e)}

    relevantes = [c for c in todas if mc.coincide_area_interes(c, areas)]

    if solo_hoy:
        relevantes = [c for c in relevantes if c.fecha_publicacion == fecha_elegida_str]

    bop_hoy = None
    if solo_hoy:
        try:
            bop_hoy = mc.fetch_bop_toledo_resumen_dia(fecha_elegida)
        except Exception as e:
            bop_hoy = []
            print(f"[AVISO] No se pudo obtener el resumen diario del BOP: {e}")

    ia_estado = None
    if usar_ia:
        if mc._configurar_gemini(gemini_key):
            try:
                mc.enriquecer_con_ia(relevantes, modelo=gemini_modelo, max_llamadas=max_ia)
                ia_estado = "ok"
            except Exception as e:
                ia_estado = f"error: {e}"
        else:
            ia_estado = "sin_clave"

    alertas_ids = {c.id_unico for c in mc.generar_alertas_plazo(relevantes, plazo_alerta)}

    ULTIMO_INFORME_MD = mc.generar_resumen_semanal(relevantes, municipio)

    # --- Notificación de plazo urgente por correo electrónico ---
    # Se activa solo si el usuario ha marcado la casilla correspondiente y
    # ha indicado a qué correo(s) enviar el aviso. El remitente, su
    # contraseña de aplicación y el servidor SMTP están fijados en el
    # servidor (variables de entorno), no en el formulario.
    notif_cfg = datos.get("notificaciones") or {}
    notif_resultado = None

    if notif_cfg.get("activar"):
        dias_min = max(0, int(notif_cfg.get("dias_min") or 3))
        dias_max = max(dias_min, int(notif_cfg.get("dias_max") or 5))

        destinatarios = [
            d.strip() for d in (notif_cfg.get("email_destino") or "").split(",") if d.strip()
        ]

        if not destinatarios:
            notif_resultado = {"error": "Indica al menos un correo destinatario."}
        elif not (EMAIL_REMITENTE and EMAIL_PASSWORD):
            notif_resultado = {
                "error": "El servidor no tiene configurado el correo remitente "
                         "(variables de entorno ALERTA_EMAIL_REMITENTE / ALERTA_EMAIL_PASSWORD)."
            }
        else:
            email_cfg = {
                "destinatarios": destinatarios,
                "remitente": EMAIL_REMITENTE,
                "password": EMAIL_PASSWORD,
                "smtp_server": EMAIL_SMTP_SERVER,
                "smtp_port": EMAIL_SMTP_PORT,
            }
            try:
                notif_resultado = mc.notificar_plazos_urgentes(
                    relevantes, municipio,
                    dias_min=dias_min, dias_max=dias_max,
                    email_cfg=email_cfg,
                )
            except Exception as e:
                notif_resultado = {"error": str(e)}

    return jsonify({
        "total_detectadas": len(todas),
        "total_relevantes": len(relevantes),
        "fuentes": fuentes_estado,
        "ia_estado": ia_estado,
        "convocatorias": [asdict(c) for c in relevantes],
        "alertas_ids": list(alertas_ids),
        "bop_hoy": bop_hoy,
        "fecha_bop": fecha_elegida_str if solo_hoy else None,
        "notificaciones": notif_resultado,
    })


@app.route("/api/descargar-informe")
def descargar_informe():
    buffer = io.BytesIO(ULTIMO_INFORME_MD.encode("utf-8"))
    return send_file(
        buffer,
        mimetype="text/markdown",
        as_attachment=True,
        download_name="resumen_semanal.md",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
