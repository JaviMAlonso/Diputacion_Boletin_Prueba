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
from dataclasses import asdict
from datetime import date, datetime

from flask import Flask, jsonify, render_template, request, send_file

import monitor_convocatorias as mc

app = Flask(__name__)

# Guardamos en memoria el último informe generado para poder descargarlo
# como Markdown sin tener que rehacer las búsquedas.
ULTIMO_INFORME_MD = "# Todavía no se ha generado ningún informe.\n"


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

    return jsonify({
        "total_detectadas": len(todas),
        "total_relevantes": len(relevantes),
        "fuentes": fuentes_estado,
        "ia_estado": ia_estado,
        "convocatorias": [asdict(c) for c in relevantes],
        "alertas_ids": list(alertas_ids),
        "bop_hoy": bop_hoy,
        "fecha_bop": fecha_elegida_str if solo_hoy else None,
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
