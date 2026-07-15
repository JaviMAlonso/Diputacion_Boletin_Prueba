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
import threading
from dataclasses import asdict
from datetime import date, datetime

from flask import Flask, jsonify, render_template, request, send_file

import monitor_convocatorias as mc

app = Flask(__name__)

# Guardamos en memoria el último informe generado para poder descargarlo
# como Markdown sin tener que rehacer las búsquedas.
ULTIMO_INFORME_MD = "# Todavía no se ha generado ningún informe.\n"

# La notificación de plazos urgentes se envía por Telegram, no por correo.
# El token del bot se configura mediante la variable de entorno
# TELEGRAM_BOT_TOKEN (ver monitor_convocatorias.py). Para que un usuario
# normal pueda recibir avisos sin tocar nada técnico, arrancamos un hilo en
# segundo plano que escucha mensajes al bot: quien escriba /start queda
# suscrito automáticamente (ver escuchar_telegram_en_segundo_plano()).
if mc.TELEGRAM_TOKEN:
    hilo_telegram = threading.Thread(
        target=mc.escuchar_telegram_en_segundo_plano, daemon=True
    )
    hilo_telegram.start()
else:
    print(
        "[AVISO] TELEGRAM_BOT_TOKEN no está configurado: los avisos por "
        "Telegram quedarán desactivados hasta que se defina."
    )



@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/telegram-info")
def telegram_info():
    """Da a la web lo necesario para mostrar un enlace 'Conectar mi
    Telegram' sin que el usuario tenga que buscar nada a mano."""
    if not mc.TELEGRAM_TOKEN:
        return jsonify({"configurado": False})
    username = mc.obtener_username_bot()
    return jsonify({
        "configurado": True,
        "username": username,
        "enlace": f"https://t.me/{username}" if username else None,
    })


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
    solo_oposiciones = bool(datos.get("solo_oposiciones"))

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

    # Respaldo SIN IA: para las convocatorias a las que no se les detectó
    # plazo y/o importe a partir del título (habitual en Reales Decretos,
    # órdenes que modifican otras normas, etc.), descargamos el documento
    # completo y volvemos a intentarlo con el mismo regex. Limitado a 25
    # documentos por búsqueda para no ralentizar demasiado ni saturar los
    # servidores de origen.
    try:
        mc.completar_plazo_e_importe_con_texto_completo(relevantes, max_documentos=25)
    except Exception as e:
        print(f"[AVISO] No se pudo completar plazo/importe con el texto completo: {e}")

    bop_hoy = None
    bop_hoy_error = None
    if solo_hoy:
        try:
            bop_hoy = mc.fetch_bop_toledo_resumen_dia(fecha_elegida)
        except Exception as e:
            bop_hoy = []
            bop_hoy_error = str(e)
            print(f"[AVISO] No se pudo obtener el resumen diario del BOP: {e}")

        # El scraper genérico de fetch_bop() (arriba) es solo una plantilla
        # sin selectores reales, así que su contador se queda siempre en 0.
        # El resumen diario de fetch_bop_toledo_resumen_dia() sí funciona de
        # verdad para la provincia de Toledo, así que sustituimos ahí el
        # contador de esa fuente por el resultado real.
        if provincia.strip().lower() == "toledo":
            clave_fuente = f"BOP {provincia}"
            if bop_hoy_error:
                fuentes_estado[clave_fuente] = {"ok": False, "error": bop_hoy_error}
            else:
                fuentes_estado[clave_fuente] = {"ok": True, "detectadas": len(bop_hoy)}

    destacados_empleo = None
    destacados_empleo_error = None
    if solo_oposiciones:
        try:
            destacados_empleo = mc.fetch_diputoledo_empleo_destacados()
            fuentes_estado["Diputación de Toledo (Empleo)"] = {
                "ok": True, "detectadas": len(destacados_empleo),
            }
        except Exception as e:
            destacados_empleo = []
            destacados_empleo_error = str(e)
            fuentes_estado["Diputación de Toledo (Empleo)"] = {"ok": False, "error": str(e)}
            print(f"[AVISO] No se pudo obtener Empleo Público de la Diputación de Toledo: {e}")

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

    # --- Notificación de plazo urgente por Telegram ---
    # Se activa solo si el usuario marca la casilla. El destino (token del
    # bot + chat_id) está fijado en el servidor mediante variables de
    # entorno, no en el formulario. A propósito, el resultado de este envío
    # NUNCA se incluye en la respuesta JSON: no debe aparecer nada sobre
    # esto en la web, funciona en silencio en segundo plano. Cualquier
    # incidencia se registra solo en la consola del servidor.
    notif_cfg = datos.get("notificaciones") or {}
    if notif_cfg.get("activar"):
        dias_min = max(1, int(notif_cfg.get("dias_min") or 3))  # nunca 0: ver nota en generar_alertas_urgentes
        dias_max = max(dias_min, int(notif_cfg.get("dias_max") or 5))
        try:
            resultado_notif = mc.notificar_plazos_urgentes(
                relevantes, municipio, dias_min=dias_min, dias_max=dias_max,
            )
            print(
                f"[Telegram] {resultado_notif['urgentes_detectadas']} urgente(s) detectada(s), "
                f"{resultado_notif['nuevas_a_notificar']} nueva(s) notificada(s) "
                f"(telegram={resultado_notif['telegram']})"
            )
        except Exception as e:
            print(f"[AVISO] No se pudo enviar la notificación por Telegram: {e}")

    return jsonify({
        "total_detectadas": len(todas),
        "total_relevantes": len(relevantes),
        "fuentes": fuentes_estado,
        "ia_estado": ia_estado,
        "convocatorias": [asdict(c) for c in relevantes],
        "alertas_ids": list(alertas_ids),
        "bop_hoy": bop_hoy,
        "fecha_bop": fecha_elegida_str if solo_hoy else None,
        "destacados_empleo": destacados_empleo,
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
    # use_reloader=False: con el recargador activado, Flask reinicia el
    # proceso y volvería a arrancar el hilo de escucha de Telegram por
    # duplicado. Si cambias este archivo tendrás que reiniciar tú mismo.
    app.run(debug=True, port=5000, use_reloader=False)
