#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 HERRAMIENTA DE ASISTENCIA PARA CONVOCATORIAS PÚBLICAS
=====================================================================
Vigilancia automatizada de boletines oficiales para municipios
pequeños que no disponen de recursos para monitorizar continuamente
las convocatorias de subvenciones, ayudas y licitaciones.

Fuentes:
  - BOE  (Boletín Oficial del Estado)          -> API oficial (real, funcional)
  - DOCM (Diario Oficial de Castilla-La Mancha) -> plantilla de scraping (ajustar selectores)
  - BOP  (Boletín Oficial de la Provincia)      -> plantilla de scraping (ajustar selectores)
  - Canales institucionales (Junta / Diputación) -> plantilla de scraping (ajustar selectores)

Salida:
  - Resumen semanal en Markdown, filtrado por áreas de interés del municipio
  - Ficha individual por convocatoria (datos clave)
  - Resumen en lenguaje natural de cada convocatoria, generado por IA (opcional)
  - Alertas de plazos próximos a vencer
  - Registro JSON para no duplicar avisos entre ejecuciones

USO
---
    python monitor_convocatorias.py --dias 7 --municipio "Ayuntamiento de Ejemplo" \
        --area-interes cultura deporte infraestructuras \
        --provincia Toledo --plazo-alerta 10 --usar-ia

Requiere:
    pip install requests beautifulsoup4
    # Opcional, solo si se usa --usar-ia:
    pip install google-genai pymupdf

IMPORTANTE SOBRE LA CLAVE DE IA
--------------------------------
La clave de la API de Gemini NUNCA se escribe en este fichero. Se lee de
la variable de entorno GEMINI_API_KEY (o del argumento --gemini-key, solo
recomendado para pruebas puntuales en local). Consíguela gratis en
https://aistudio.google.com/apikey y, si alguna vez la has compartido o
pegado en un chat/repositorio, revócala y genera una nueva.

    # Linux / macOS
    export GEMINI_API_KEY="tu_clave_aqui"
    # Windows PowerShell
    $env:GEMINI_API_KEY = "tu_clave_aqui"
=====================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET
import requests

try:
    from bs4 import BeautifulSoup, NavigableString  # usado por los scrapers de DOCM/BOP
except ImportError:
    BeautifulSoup = None  # se avisa en tiempo de ejecución si falta
    NavigableString = str

try:
    # SDK oficial y actualmente soportado (el paquete antiguo
    # 'google-generativeai' ha sido descontinuado por Google).
    from google import genai as genai_sdk
except ImportError:
    genai_sdk = None

_GEMINI_CLIENT = None  # se inicializa en _configurar_gemini()

try:
    import fitz  # PyMuPDF, usado para extraer texto de los PDF de convocatorias
except ImportError:
    fitz = None


# =====================================================================
# 1. CONFIGURACIÓN
# =====================================================================

HEADERS = {"User-Agent": "MonitorConvocatoriasPublicas/1.0 (uso institucional)"}
ESTADO_PATH = Path("convocatorias_vistas.json")

# Configuración del bot de Telegram usado para avisar de plazos urgentes.
# NUNCA se escribe el token aquí en el código. Hay dos formas de
# configurarlo, a elegir la que resulte más cómoda:
#
#   OPCIÓN A (recomendada, sin usar la terminal): crea, en la misma carpeta
#   que este archivo, un fichero de texto llamado "config.env" con una
#   línea así (puedes copiar "config.env.ejemplo" y renombrarlo):
#       TELEGRAM_BOT_TOKEN=tu_token_de_botfather
#   Ese fichero se lee solo al arrancar; no hay que exportar nada.
#
#   OPCIÓN B (variable de entorno, para quien prefiera la terminal):
#       export TELEGRAM_BOT_TOKEN="tu_token_de_botfather"      (Linux/macOS)
#       $env:TELEGRAM_BOT_TOKEN = "tu_token_de_botfather"      (PowerShell)
#   Si el valor ya está definido como variable de entorno, tiene prioridad
#   sobre lo que haya en config.env.
#
# Si no se configura por ninguna de las dos vías, notificar_plazos_urgentes()
# simplemente no hace nada (lo avisa por consola, no falla la búsqueda).

def _cargar_config_env() -> None:
    """Lee `config.env` (si existe) y copia sus claves a las variables de
    entorno del proceso, sin sobrescribir las que ya estuvieran definidas
    de antes (para que 'export'/PowerShell siga teniendo prioridad si
    alguien lo usa). Formato: una línea por clave, `CLAVE=valor`; las
    líneas vacías o que empiezan por # se ignoran. No requiere ninguna
    librería adicional (no usa python-dotenv)."""
    ruta = Path(__file__).resolve().parent / "config.env"
    if not ruta.exists():
        return
    try:
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, _, valor = linea.partition("=")
            clave = clave.strip()
            valor = valor.strip().strip('"').strip("'")
            if clave and clave not in os.environ:
                os.environ[clave] = valor
    except OSError as e:
        print(f"[AVISO] No se pudo leer config.env: {e}", file=sys.stderr)


_cargar_config_env()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# Palabras clave para clasificar cada anuncio detectado
CATEGORIAS_KEYWORDS = {
    "Subvención / ayuda pública": [
        "subvención", "subvenciones", "ayuda", "ayudas", "beca", "becas",
        "convocatoria de ayudas", "concesión de ayudas", "bases reguladoras",
    ],
    "Licitación / contrato menor": [
        "licitación", "licitaciones", "contrato menor", "contratación",
        "concurso público", "adjudicación", "pliego de condiciones",
        "procedimiento abierto", "anuncio de licitación",
    ],
    "Programa de cooperación municipal": [
        "cooperación municipal", "plan provincial", "fondo de cooperación",
        "diputación provincial", "convenio de colaboración", "cooperación local",
    ],
    "Empleo público / Oposición": [
        "oposición", "oposiciones", "proceso selectivo", "procesos selectivos",
        "pruebas selectivas", "concurso-oposición", "concurso de méritos",
        "bolsa de trabajo", "bolsa de empleo", "oferta de empleo público",
        "convocatoria de plazas", "personal funcionario", "personal laboral",
        "funcionario de carrera", "tribunal calificador", "listado de admitidos",
        "lista de admitidos", "nombramiento de funcionarios", "provisión de puestos",
    ],
}

# Palabras que indican irrelevancia (para reducir ruido, ajustable).
# NOTA: ya no excluimos términos de empleo público/oposiciones aquí, porque
# ahora tienen su propia categoría arriba ("Empleo público / Oposición").
EXCLUIR_KEYWORDS: list[str] = []

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}


# =====================================================================
# 2. MODELO DE DATOS
# =====================================================================

@dataclass
class Convocatoria:
    fuente: str                     # BOE / DOCM / BOP / Institucional
    titulo: str
    categoria: str
    fecha_publicacion: str
    url: str
    plazo: Optional[str] = None
    dias_restantes: Optional[int] = None
    importe: Optional[str] = None
    organismo: Optional[str] = None
    resumen: Optional[str] = None
    id_unico: str = field(default="")

    def __post_init__(self):
        if not self.id_unico:
            self.id_unico = f"{self.fuente}:{self.url}"

    def ficha(self) -> str:
        """Ficha individual con los datos clave de la convocatoria."""
        lineas = [
            f"### {self.titulo}",
            f"- **Fuente:** {self.fuente}",
            f"- **Categoría:** {self.categoria}",
            f"- **Organismo:** {self.organismo or 'No especificado'}",
            f"- **Fecha de publicación:** {self.fecha_publicacion}",
            f"- **Plazo:** {self.plazo or 'No detectado automáticamente — revisar fuente'}",
        ]
        if self.dias_restantes is not None:
            lineas.append(f"- **Días restantes:** {self.dias_restantes}")
        lineas.append(f"- **Cuantía / importe:** {self.importe or 'No especificado'}")
        if self.resumen:
            lineas.append(f"- **Resumen:** {self.resumen}")
        lineas.append(f"- **Enlace oficial:** {self.url}")
        return "\n".join(lineas)


# =====================================================================
# 3. UTILIDADES DE EXTRACCIÓN (IA ligera basada en reglas / regex)
# =====================================================================

def clasificar_texto(texto: str) -> Optional[str]:
    """Clasifica un texto en una de las categorías de interés, o None."""
    texto_low = texto.lower()
    if any(palabra in texto_low for palabra in EXCLUIR_KEYWORDS):
        return None
    for categoria, palabras in CATEGORIAS_KEYWORDS.items():
        if any(palabra in texto_low for palabra in palabras):
            return categoria
    return None


def extraer_plazo(texto: str) -> tuple[Optional[str], Optional[int]]:
    """
    Busca menciones de plazo en el texto ('hasta el DD de MES de AAAA',
    'DD/MM/AAAA', 'plazo de X días') y devuelve (texto_plazo, dias_restantes).

    OJO: muchos anuncios (sobre todo convocatorias de oposiciones) empiezan
    con "Resolución de DD de MES de AAAA, de..." — esa fecha es cuándo se
    firmó la resolución, NO un plazo límite. Antes este regex la cogía sin
    más y la confundía con la fecha de fin de plazo. Ahora:
      1º Buscamos una fecha con un indicador explícito de plazo delante
         ("hasta el...", "antes del...", "fecha límite...").
      2º Si no lo hay, buscamos cualquier fecha "dd de mes de aaaa" pero
         descartando las que vienen justo después de "resolución de",
         "orden de", "acuerdo de", "de fecha", etc. (fechas de emisión del
         propio documento, no plazos).
    OJO 2: el texto de un documento real (sobre todo extraído de un PDF)
    viene con saltos de línea en mitad de las frases. Si no se normaliza,
    patrones como "plazo\b.{0,60}?\bde" nunca cruzan esos saltos de línea
    y se pierden coincidencias que en el documento están perfectamente
    seguidas. Por eso lo primero que hacemos es colapsar cualquier
    secuencia de espacios en blanco (incluidos saltos de línea) a un único
    espacio.
    """
    texto = re.sub(r"\s+", " ", texto)
    texto_low = texto.lower()
    patron_fecha = r"(\d{1,2})\s+de\s+(" + "|".join(MESES.keys()) + r")\s+de\s+(\d{4})"

    lead_plazo = (
        r"(?:hasta(?:\s+el)?|antes\s+del|no\s+m[aá]s\s+tarde\s+del|"
        r"fecha\s+l[ií]mite\s*:?|finaliza(?:r[aá])?\s+el|vence\s+el|concluye\s+el)"
    )
    lead_emision = (
        r"(?:resoluci[oó]n\s+n?[ºo]?\s*\d*\s*,?\s*de|orden\s+de|acuerdo\s+de|"
        r"instrucci[oó]n\s+de|circular\s+de|decreto\s+de|de\s+fecha)"
    )
    lead_apertura = r"apertura\s+(?:de\s+ofertas|sobre\s+administrativa|sobre\s+oferta\s+\w+|de\s+las?\s+plicas)"

    # 1) Fecha precedida de un indicador explícito de plazo ("hasta el...").
    m = re.search(lead_plazo + r"\s+" + patron_fecha, texto_low)
    if m:
        dia, mes_nombre, anio = m.groups()
        try:
            fecha_limite = date(int(anio), MESES[mes_nombre], int(dia))
            dias = (fecha_limite - date.today()).days
            return f"Hasta el {dia} de {mes_nombre} de {anio}", dias
        except ValueError:
            pass

    # 2) Licitaciones: la fecha de "apertura de ofertas" / "apertura sobre
    #    administrativa" no es literalmente el plazo de presentación (suele
    #    ser un poco posterior), pero cuando no hay un plazo explícito es el
    #    dato más útil y cercano que aparece en el anuncio. La etiquetamos
    #    aparte para no dar a entender que es una fecha límite real, y la
    #    comprobamos ANTES del patrón genérico de abajo para que no se
    #    cuele ahí mal etiquetada como "Hasta el...".
    m = re.search(lead_apertura + r"[^0-9]{0,40}" + patron_fecha, texto_low)
    if m:
        dia, mes_nombre, anio = m.groups()
        try:
            fecha_apertura = date(int(anio), MESES[mes_nombre], int(dia))
            dias = (fecha_apertura - date.today()).days
            return f"Apertura de ofertas: {dia} de {mes_nombre} de {anio}", dias
        except ValueError:
            pass

    # 3) "Plazo de ejecución": el periodo comprendido entre dos fechas
    #    (típico de las bases de subvenciones: "el periodo comprendido
    #    entre el 1 de diciembre de 2025 y el 31 de diciembre de 2026").
    #    No es un plazo de presentación, sino el plazo para ejecutar la
    #    actividad subvencionada una vez concedida; lo etiquetamos aparte
    #    y usamos la fecha de FIN del periodo (la relevante para saber
    #    hasta cuándo hay margen).
    m = re.search(
        r"comprendido\s+entre\s+el\s+" + patron_fecha + r"\s+y\s+el\s+" + patron_fecha,
        texto_low,
    )
    if m:
        dia_fin, mes_fin, anio_fin = m.group(4), m.group(5), m.group(6)
        try:
            fecha_fin = date(int(anio_fin), MESES[mes_fin], int(dia_fin))
            dias = (fecha_fin - date.today()).days
            return f"Plazo de ejecución: hasta el {dia_fin} de {mes_fin} de {anio_fin}", dias
        except ValueError:
            pass

    # 4) Cualquier fecha "dd de mes de aaaa", descartando las que sean en
    #    realidad la fecha de emisión del documento (resolución/orden/etc.),
    #    una fecha de apertura de ofertas (punto 2), el principio/fin de
    #    un periodo de ejecución (punto 3, ya cubierto arriba), o la fecha
    #    de FIRMA con la que casi todo documento oficial termina, con el
    #    formato "Madrid, 7 de julio de 2026.—La Secretaria de Estado...".
    #    Esa fecha de firma es casi siempre "hoy" (o muy cercana), así que
    #    si no se excluye, el regex la confunde con un plazo que vence en
    #    0 días y nunca deja que se detecte el plazo real (si lo hay) más
    #    adelante en el texto.
    lead_rango = r"(?:comprendido\s+)?entre\s+el"
    lead_firma = r"^[^.,]{0,40},\s*a?\s*$"  # "<Ciudad>, [a ]" justo antes de la fecha
    for m in re.finditer(patron_fecha, texto_low):
        contexto_previo = texto_low[max(0, m.start() - 45):m.start()]
        contexto_posterior = texto_low[m.end():m.end() + 5]
        if (re.search(lead_emision + r"\s*$", contexto_previo)
                or re.search(lead_apertura + r"[^0-9]{0,40}$", contexto_previo)
                or re.search(lead_rango + r"\s*$", contexto_previo)
                or re.search(r"^\.\s*[—–-]", contexto_posterior)
                or re.search(lead_firma, contexto_previo)):
            continue
        dia, mes_nombre, anio = m.groups()
        try:
            fecha_limite = date(int(anio), MESES[mes_nombre], int(dia))
            dias = (fecha_limite - date.today()).days
            return f"Hasta el {dia} de {mes_nombre} de {anio}", dias
        except ValueError:
            continue

    # Patrón dd/mm/aaaa (aplicamos la misma exclusión de fecha de emisión)
    for m in re.finditer(r"(\d{1,2})/(\d{1,2})/(\d{4})", texto):
        contexto_previo = texto_low[max(0, m.start() - 30):m.start()]
        if re.search(lead_emision + r"\s*$", contexto_previo):
            continue
        dia, mes, anio = map(int, m.groups())
        try:
            fecha_limite = date(anio, mes, dia)
            dias = (fecha_limite - date.today()).days
            return f"Hasta el {dia:02d}/{mes:02d}/{anio}", dias
        except ValueError:
            continue

    # Patrón "plazo de X días (hábiles/naturales)", con dígitos o en letra.
    # Para la versión en letra usamos el mismo conversor que extraer_importe
    # (más abajo en el archivo), así detectamos cualquier número escrito
    # ("veinte días hábiles", "cuarenta y cinco días naturales",
    # "un mes y quince días"...) y no solo una lista fija de casos comunes.
    m = re.search(r"plazo\b.{0,60}?\bde\s+(\d{1,3})\s+d[ií]as", texto_low)
    if m:
        dias = int(m.group(1))
        return f"Plazo de {dias} días desde la publicación", None

    m = re.search(
        r"plazo\b.{0,60}?\bde\s+((?:" + _PATRON_NUM_PALABRA + r"\s+)+)d[ií]as",
        texto_low,
    )
    if m:
        dias = _numero_escrito_a_valor(m.group(1))
        if dias is not None:
            return f"Plazo de {dias} días desde la publicación", None

    # Orden inverso, también muy habitual: "veinte días hábiles de plazo"
    # / "20 días naturales de plazo" (en vez de "plazo de veinte días").
    m = re.search(r"(\d{1,3})\s+d[ií]as\s+(?:h[aá]biles|naturales)?\s*de\s+plazo", texto_low)
    if m:
        dias = int(m.group(1))
        return f"Plazo de {dias} días desde la publicación", None

    m = re.search(
        r"((?:" + _PATRON_NUM_PALABRA + r"\s+)+)d[ií]as\s+(?:h[aá]biles|naturales)?\s*de\s+plazo",
        texto_low,
    )
    if m:
        dias = _numero_escrito_a_valor(m.group(1))
        if dias is not None:
            return f"Plazo de {dias} días desde la publicación", None

    return None, None


def _numero_escrito_a_valor(fragmento: str) -> Optional[int]:
    """Convierte un número escrito en palabras españolas ('veinticinco',
    'cuarenta y ocho', 'ciento veinte', 'dos mil quinientos'...) a un
    entero. Devuelve None si no reconoce nada."""
    UNIDADES = {
        "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3,
        "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
        "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14,
        "quince": 15, "dieciseis": 16, "diecisiete": 17, "dieciocho": 18,
        "diecinueve": 19, "veinte": 20, "veintiun": 21, "veintiuno": 21,
        "veintiuna": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24,
        "veinticinco": 25, "veintiseis": 26, "veintisiete": 27,
        "veintiocho": 28, "veintinueve": 29,
    }
    DECENAS = {
        "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60,
        "setenta": 70, "ochenta": 80, "noventa": 90,
    }
    CENTENAS = {
        "cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200,
        "trescientos": 300, "trescientas": 300, "cuatrocientos": 400,
        "cuatrocientas": 400, "quinientos": 500, "quinientas": 500,
        "seiscientos": 600, "seiscientas": 600, "setecientos": 700,
        "setecientas": 700, "ochocientos": 800, "ochocientas": 800,
        "novecientos": 900, "novecientas": 900,
    }

    def sin_acentos(s: str) -> str:
        return "".join(
            c for c in unicodedata.normalize("NFD", s)
            if unicodedata.category(c) != "Mn"
        )

    tokens = sin_acentos(fragmento.lower()).split()
    total = 0
    actual = 0
    encontrado = False

    for tok in tokens:
        if tok == "y":
            continue
        if tok == "mil":
            actual = actual if actual else 1
            total += actual * 1000
            actual = 0
            encontrado = True
        elif tok in CENTENAS:
            actual += CENTENAS[tok]
            encontrado = True
        elif tok in DECENAS:
            actual += DECENAS[tok]
            encontrado = True
        elif tok in UNIDADES:
            actual += UNIDADES[tok]
            encontrado = True
        else:
            return None  # palabra no reconocida: mejor no arriesgar

    total += actual
    return total if encontrado else None


# Vocabulario reconocido por _numero_escrito_a_valor, usado para construir
# el patrón que localiza estos números dentro de un texto más largo.
_PALABRAS_NUMERO = sorted(
    ["cero", "un", "uno", "una", "dos", "tres", "cuatro", "cinco", "seis",
     "siete", "ocho", "nueve", "diez", "once", "doce", "trece", "catorce",
     "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve",
     "veinte", "veintiún", "veintiuno", "veintiuna", "veintidós",
     "veintitrés", "veinticuatro", "veinticinco", "veintiséis",
     "veintisiete", "veintiocho", "veintinueve", "treinta", "cuarenta",
     "cincuenta", "sesenta", "setenta", "ochenta", "noventa", "cien",
     "ciento", "doscientos", "doscientas", "trescientos", "trescientas",
     "cuatrocientos", "cuatrocientas", "quinientos", "quinientas",
     "seiscientos", "seiscientas", "setecientos", "setecientas",
     "ochocientos", "ochocientas", "novecientos", "novecientas", "mil", "y"],
    key=len, reverse=True,
)
_PATRON_NUM_PALABRA = r"(?:" + "|".join(_PALABRAS_NUMERO) + r")"
_RE_EUROS_EN_LETRA = re.compile(
    r"((?:" + _PATRON_NUM_PALABRA + r"\s+)+)euros?"
    r"(?:\s+y\s+((?:" + _PATRON_NUM_PALABRA + r"\s+)+)c[eé]ntimos?)?",
    re.IGNORECASE,
)


def extraer_importe(texto: str) -> Optional[str]:
    """Busca cuantías económicas del tipo '1.234,56 euros' o '1.234.567 €'.
    Si no encuentra ningún importe en euros, busca un porcentaje máximo de
    financiación (p. ej. 'hasta el 80%', 'máximo del 50 %', '80% del coste'),
    que en muchas subvenciones sustituye a una cuantía fija.

    En licitaciones el importe casi nunca aparece suelto: viene etiquetado
    como "Valor estimado del contrato", "Presupuesto base de licitación" o
    "Importe de licitación". Buscamos primero esas etiquetas (dato más
    fiable y representativo) antes de coger el primer € que aparezca en
    cualquier otro sitio del texto (que podría ser una fianza, un importe
    de IVA aparte, etc.).

    Algunos boletines (sobre todo bases de tasas y convocatorias de
    oposiciones) escriben la cantidad completamente en letra en vez de en
    dígitos, p. ej. "veinticinco euros y cuarenta y ocho céntimos". Si no
    hay ningún importe en dígitos, también probamos a reconocer esto.

    Igual que en extraer_plazo, normalizamos saltos de línea/espacios
    múltiples a uno solo antes de nada, porque el texto de un PDF real
    parte las frases en mitad de una etiqueta como "Valor estimado" y su
    cifra si no se hace esto.
    """
    texto = re.sub(r"\s+", " ", texto)

    m_etiquetado = re.search(
        r"(?:valor\s+estimado(?:\s+del\s+contrato)?|presupuesto\s+base\s+de\s+licitaci[oó]n|"
        r"importe\s+de\s+licitaci[oó]n|valor\s+de\s+la\s+oferta\s+seleccionada)\s*:?\s*"
        r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s*(?:€|euros)",
        texto, re.IGNORECASE,
    )
    if m_etiquetado:
        etiqueta = "oferta adjudicada" if "oferta" in m_etiquetado.group(0).lower() else "valor estimado"
        return f"{m_etiquetado.group(1)} € ({etiqueta})"

    m = re.search(r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s*(?:€|euros)", texto, re.IGNORECASE)
    if m:
        return f"{m.group(1)} €"

    m_letra = _RE_EUROS_EN_LETRA.search(texto)
    if m_letra:
        euros = _numero_escrito_a_valor(m_letra.group(1))
        centimos = _numero_escrito_a_valor(m_letra.group(2)) if m_letra.group(2) else 0
        if euros is not None:
            importe = f"{euros},{centimos:02d} €"
            contexto_previo = texto[max(0, m_letra.start() - 70):m_letra.start()].lower()
            if "tasa" in contexto_previo:
                importe += " (tasa)"
            return importe

    m_pct = re.search(
        r"(?:hasta(?:\s+un|\s+el)?|m[aá]xim[oa](?:\s+de|\s+del)?)\s*(\d{1,3}(?:[.,]\d+)?)\s*%",
        texto, re.IGNORECASE,
    )
    if m_pct:
        return f"Hasta el {m_pct.group(1)} %"

    m_pct_generico = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*%", texto)
    if m_pct_generico:
        return f"{m_pct_generico.group(1)} %"

    return None


def coincide_area_interes(conv: Convocatoria, areas_interes: list[str]) -> bool:
    """Si el municipio no define áreas, todo pasa. Si define, filtra por texto."""
    if not areas_interes:
        return True
    texto = f"{conv.titulo} {conv.resumen or ''}".lower()
    return any(area.lower() in texto for area in areas_interes)


# =====================================================================
# 4. FUENTE 1: BOE — API oficial de datos abiertos (real y funcional)
#    Documentación: https://www.boe.es/datosabiertos/api/api.php
# =====================================================================

def _como_lista(valor):
    """La API del BOE devuelve un dict cuando hay un único elemento y una
    lista cuando hay varios. Esta función normaliza siempre a lista."""
    if valor is None:
        return []
    if isinstance(valor, list):
        return valor
    return [valor]


def fetch_boe(dias_atras: int = 7) -> list[Convocatoria]:
    """
    Descarga los sumarios diarios del BOE de los últimos `dias_atras` días
    usando la API pública oficial (JSON) y extrae los anuncios relevantes
    (subvenciones, ayudas, licitaciones, cooperación).

    Endpoint oficial: https://boe.es/datosabiertos/api/boe/sumario/{AAAAMMDD}
    Documentación: https://www.boe.es/datosabiertos/documentos/APIsumarioBOE.pdf
    """
    resultados: list[Convocatoria] = []
    hoy = date.today()
    headers_json = {**HEADERS, "Accept": "application/json"}

    for delta in range(dias_atras):
        fecha = hoy - timedelta(days=delta)
        url = f"https://boe.es/datosabiertos/api/boe/sumario/{fecha.strftime('%Y%m%d')}"

        try:
            resp = requests.get(url, headers=headers_json, timeout=15)
            if resp.status_code != 200:
                # 404 es normal en domingos/festivos (no hay BOE ese día)
                continue
            payload = resp.json()
        except (requests.RequestException, ValueError):
            continue

        if payload.get("status", {}).get("code") != "200":
            continue

        sumario = payload.get("data", {}).get("sumario", {})
        for diario in _como_lista(sumario.get("diario")):
            for seccion in _como_lista(diario.get("seccion")):
                for depto in _como_lista(seccion.get("departamento")):
                    organismo = depto.get("nombre")

                    # Los <item> pueden colgar directamente del departamento
                    # o estar agrupados dentro de uno o varios <epigrafe>.
                    items = list(_como_lista(depto.get("item")))
                    for epigrafe in _como_lista(depto.get("epigrafe")):
                        items.extend(_como_lista(epigrafe.get("item")))

                    for item in items:
                        titulo = (item.get("titulo") or "").strip()
                        if not titulo:
                            continue

                        categoria = clasificar_texto(titulo)
                        if categoria is None:
                            continue

                        enlace = item.get("url_html") or ""
                        if not enlace:
                            url_pdf = item.get("url_pdf")
                            if isinstance(url_pdf, dict):
                                enlace = url_pdf.get("texto", "")
                            elif isinstance(url_pdf, str):
                                enlace = url_pdf

                        plazo, dias = extraer_plazo(titulo)
                        importe = extraer_importe(titulo)

                        resultados.append(Convocatoria(
                            fuente="BOE",
                            titulo=titulo,
                            categoria=categoria,
                            fecha_publicacion=fecha.strftime("%d/%m/%Y"),
                            url=enlace,
                            plazo=plazo,
                            dias_restantes=dias,
                            importe=importe,
                            organismo=organismo,
                        ))

    return resultados


# =====================================================================
# 5. FUENTE 2: DOCM — Diario Oficial de Castilla-La Mancha
#    NOTA: el DOCM no ofrece una API pública estable. Esta función es
#    una PLANTILLA de scraping sobre el buscador web (docm.jccm.es).
#    Debes revisar los selectores CSS/XPath tras inspeccionar la página
#    actual, ya que el HTML puede cambiar.
# =====================================================================

def fetch_docm(dias_atras: int = 7) -> list[Convocatoria]:
    resultados: list[Convocatoria] = []

    if BeautifulSoup is None:
        print("[AVISO] BeautifulSoup no instalado: se omite DOCM. "
              "Instala con: pip install beautifulsoup4", file=sys.stderr)
        return resultados

    base_url = "https://docm.jccm.es"
    buscador_url = f"{base_url}"

    try:
        resp = requests.get(buscador_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[AVISO] No se pudo acceder al DOCM: {e}", file=sys.stderr)
        return resultados

    soup = BeautifulSoup(resp.text, "html.parser")

    # --- PLANTILLA: ajustar el selector a la estructura real de resultados ---
    # Ejemplo orientativo (revisar y adaptar):
    for enlace in soup.select("a[href*='pdf']"):
        titulo = enlace.get_text(strip=True)
        if not titulo:
            continue
        categoria = clasificar_texto(titulo)
        if categoria is None:
            continue

        href = enlace.get("href", "")
        url_completa = href if href.startswith("http") else f"{base_url}{href}"
        plazo, dias = extraer_plazo(titulo)

        resultados.append(Convocatoria(
            fuente="DOCM",
            titulo=titulo,
            categoria=categoria,
            fecha_publicacion=date.today().strftime("%d/%m/%Y"),
            url=url_completa,
            plazo=plazo,
            dias_restantes=dias,
            importe=extraer_importe(titulo),
            organismo="Junta de Comunidades de Castilla-La Mancha",
        ))

    return resultados


# =====================================================================
# 6. FUENTE 3: BOP — Boletín Oficial de la Provincia
#    NOTA: cada Diputación Provincial gestiona su propio BOP con su
#    propia web y estructura. Esta es una PLANTILLA genérica: indica la
#    URL del buscador de tu provincia y ajusta los selectores.
# =====================================================================

def fetch_bop(provincia: str = "Toledo", dias_atras: int = 7) -> list[Convocatoria]:
    resultados: list[Convocatoria] = []

    if BeautifulSoup is None:
        print("[AVISO] BeautifulSoup no instalado: se omite BOP.", file=sys.stderr)
        return resultados

    # --- PLANTILLA: sustituir por la URL real del BOP de tu provincia ---
    # Ejemplo (Diputación de Toledo): https://bop.diputoledo.es
    bop_urls = {
        "toledo": "https://bop.diputoledo.es",
        "ciudad real": "https://bop.dipucr.es",
        "cuenca": "https://www.dipucuenca.es/bop",
        "guadalajara": "https://bop.dguadalajara.es",
        "albacete": "https://bop.dipualba.es",
    }
    base_url = bop_urls.get(provincia.lower())
    if not base_url:
        print(f"[AVISO] No hay URL configurada para la provincia '{provincia}'. "
              f"Añádela en bop_urls dentro de fetch_bop().", file=sys.stderr)
        return resultados

    try:
        resp = requests.get(base_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[AVISO] No se pudo acceder al BOP de {provincia}: {e}", file=sys.stderr)
        return resultados

    soup = BeautifulSoup(resp.text, "html.parser")

    # --- PLANTILLA: ajustar el selector real de anuncios del portal ---
    for enlace in soup.select("a"):
        titulo = enlace.get_text(strip=True)
        if not titulo or len(titulo) < 15:
            continue
        categoria = clasificar_texto(titulo)
        if categoria is None:
            continue

        href = enlace.get("href", "")
        url_completa = href if href.startswith("http") else f"{base_url}{href}"
        plazo, dias = extraer_plazo(titulo)

        resultados.append(Convocatoria(
            fuente=f"BOP {provincia.title()}",
            titulo=titulo,
            categoria=categoria,
            fecha_publicacion=date.today().strftime("%d/%m/%Y"),
            url=url_completa,
            plazo=plazo,
            dias_restantes=dias,
            importe=extraer_importe(titulo),
            organismo=f"Diputación de {provincia.title()}",
        ))

    return resultados


# =====================================================================
# 6bis. BOP TOLEDO — Resumen diario "en bruto" (título + enlace al PDF)
#    Usa el buscador público de resúmenes diarios del BOP de Toledo:
#    https://bop.diputoledo.es/webEbop/ebopResumen.jsp?publication_date=DD/MM/YYYY&publication_date_to=DD/MM/YYYY
#
#    NOTA: no se ha podido inspeccionar el HTML en directo desde este
#    entorno (el robots.txt del portal bloquea el acceso automatizado a
#    herramientas de terceros), así que el análisis de abajo es lo más
#    fiel posible a la estructura observada en resultados de búsqueda
#    (bloques con "Número de inserción", "Ver anuncio", "Tipo de
#    anuncio" y "Resumen/Asunto"). Si al ejecutarlo no aparece nada,
#    abre la URL en el navegador, pulsa clic derecho -> "Ver código
#    fuente" sobre un anuncio y ajusta el selector indicado más abajo.
# =====================================================================

def fetch_bop_toledo_resumen_dia(fecha: "date | None" = None) -> list[dict]:
    """Devuelve una lista de dicts {titulo, resumen, organismo, url_pdf} con
    los anuncios del BOP de Toledo publicados en la fecha indicada (por
    defecto, hoy). No aplica ninguna clasificación ni filtro: es el listado
    en bruto, pensado para el modo "ver solo un día concreto" de la interfaz
    web.

    Confirmado inspeccionando el HTML real de ebopResumen.jsp: cada anuncio
    va dentro de un <div class="announce"> (con el enlace "Ver anuncio" y el
    "Resumen/Asunto"), y cada grupo de anuncios de una misma entidad va
    precedido de un <h3 class="publisherBlock">Anunciante : NOMBRE</h3>.
    Recorremos el documento en orden y nos quedamos con el último
    "publisherBlock" visto para asignárselo a los anuncios siguientes.
    """
    if BeautifulSoup is None:
        print("[AVISO] BeautifulSoup no instalado: se omite el resumen BOP.", file=sys.stderr)
        return []

    fecha = fecha or date.today()
    fecha_str = fecha.strftime("%d/%m/%Y")

    url = "https://bop.diputoledo.es/webEbop/ebopResumen.jsp"
    params = {
        "publication_date": fecha_str,
        "publication_date_to": fecha_str,
    }

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[AVISO] No se pudo acceder al resumen diario del BOP de Toledo: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    resultados: list[dict] = []
    vistos: set[tuple[str, str]] = set()
    organismo_actual: Optional[str] = None

    for el in soup.find_all(["h3", "div"]):
        clases = el.get("class") or []

        if "publisherBlock" in clases:
            texto = el.get_text(" ", strip=True)
            m = re.search(r"anunciante\s*:\s*(.+)", texto, re.IGNORECASE)
            organismo_actual = (m.group(1) if m else texto).strip()
            continue

        if "announce" not in clases:
            continue

        enlace = el.find("a", href=True)
        if enlace is None:
            continue
        href = enlace["href"].strip()
        if not href:
            continue
        url_pdf = href if href.startswith("http") else f"https://bop.diputoledo.es{href}"

        texto_bloque = el.get_text(" ", strip=True)
        m_resumen = re.search(r"resumen/asunto\s*:?\s*(.+)", texto_bloque, re.IGNORECASE)
        resumen = m_resumen.group(1).strip() if m_resumen else ""
        if not resumen:
            continue

        clave = (resumen, url_pdf)
        if clave in vistos:
            continue
        vistos.add(clave)

        resultados.append({
            "titulo": resumen,
            "resumen": resumen,
            "organismo": organismo_actual or "Entidad no identificada",
            "url_pdf": url_pdf,
        })

    return resultados

def cargar_vistas() -> set[str]:
    if ESTADO_PATH.exists():
        try:
            return set(json.loads(ESTADO_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return set()
    return set()


def guardar_vistas(ids: set[str]) -> None:
    ESTADO_PATH.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8")


# =====================================================================
# 8. GENERACIÓN DE INFORMES
# =====================================================================

def generar_resumen_semanal(convocatorias: list[Convocatoria], municipio: str) -> str:
    hoy = date.today().strftime("%d/%m/%Y")
    partes = [
        f"# Resumen semanal de convocatorias — {municipio}",
        f"*Generado el {hoy}*\n",
        f"Se han detectado **{len(convocatorias)}** convocatorias relevantes en las fuentes monitorizadas.\n",
    ]

    por_categoria: dict[str, list[Convocatoria]] = {}
    for c in convocatorias:
        por_categoria.setdefault(c.categoria, []).append(c)

    for categoria, items in por_categoria.items():
        partes.append(f"\n## {categoria} ({len(items)})\n")
        for c in items:
            partes.append(c.ficha())
            partes.append("")

    return "\n".join(partes)


def generar_alertas_plazo(convocatorias: list[Convocatoria], dias_aviso: int) -> list[Convocatoria]:
    """Devuelve las convocatorias cuyo plazo vence en `dias_aviso` días o menos."""
    return [
        c for c in convocatorias
        if c.dias_restantes is not None and 0 <= c.dias_restantes <= dias_aviso
    ]


# =====================================================================
# 8 BIS. NOTIFICACIÓN DE PLAZO URGENTE POR TELEGRAM
# =====================================================================
#
# Este bloque envía un aviso proactivo cuando una convocatoria entra en la
# "ventana crítica" de vencimiento (por defecto, entre 3 y 5 días antes de
# que caduque el plazo). Es independiente de generar_alertas_plazo(), que
# se usa para el listado más amplio del informe semanal.
#
# Para no repetir el mismo aviso en cada ejecución (p. ej. si el script se
# lanza a diario con cron), se guarda un registro propio de convocatorias
# ya notificadas en NOTIFICADAS_PATH.
#
# El token del bot y el chat de destino se configuran UNA SOLA VEZ en el
# servidor mediante las variables de entorno TELEGRAM_BOT_TOKEN y
# TELEGRAM_CHAT_ID (ver más arriba en el archivo). No hay ningún dato de
# contacto que rellenar desde la web: quien usa el formulario solo puede
# activar/desactivar el aviso y ajustar la ventana de días.

NOTIFICADAS_PATH = Path("convocatorias_notificadas.json")

# Chats de Telegram suscritos a los avisos (quien ha escrito /start al bot).
# Así un usuario normal no necesita buscar su chat_id a mano: solo tiene
# que abrir un enlace al bot y pulsar "Iniciar". Ver
# escuchar_telegram_en_segundo_plano() más abajo.
SUSCRIPTORES_TELEGRAM_PATH = Path("telegram_suscriptores.json")


def cargar_notificadas() -> set[str]:
    """Carga el conjunto de id_unico de convocatorias ya notificadas."""
    if NOTIFICADAS_PATH.exists():
        try:
            return set(json.loads(NOTIFICADAS_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return set()
    return set()


def guardar_notificadas(ids: set[str]) -> None:
    NOTIFICADAS_PATH.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def cargar_suscriptores_telegram() -> set[str]:
    """Carga el conjunto de chat_id que se han suscrito escribiendo /start."""
    if SUSCRIPTORES_TELEGRAM_PATH.exists():
        try:
            return set(json.loads(SUSCRIPTORES_TELEGRAM_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return set()
    return set()


def guardar_suscriptores_telegram(ids: set[str]) -> None:
    SUSCRIPTORES_TELEGRAM_PATH.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def generar_alertas_urgentes(
    convocatorias: list[Convocatoria], dias_min: int = 3, dias_max: int = 5
) -> list[Convocatoria]:
    """
    Devuelve las convocatorias cuyo plazo vence dentro de la ventana crítica
    [dias_min, dias_max] (ambos incluidos). Por defecto, entre 3 y 5 días.

    Por decisión de producto, NUNCA se incluyen convocatorias con
    dias_restantes == 0 (el plazo vence hoy mismo): a esas alturas un
    aviso ya no da tiempo útil de reacción, así que se excluyen aunque
    alguien configure dias_min a 0 desde el formulario.
    """
    return [
        c for c in convocatorias
        if c.dias_restantes is not None
        and c.dias_restantes > 0
        and dias_min <= c.dias_restantes <= dias_max
    ]


def obtener_username_bot() -> Optional[str]:
    """Consulta a la API de Telegram el nombre de usuario del bot (para
    poder construir el enlace https://t.me/<username> que abre el chat).
    Devuelve None si no hay token configurado o la consulta falla."""
    if not TELEGRAM_TOKEN:
        return None
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=10
        )
        datos = resp.json()
        if datos.get("ok"):
            return datos["result"].get("username")
    except requests.RequestException:
        pass
    return None


def _enviar_mensaje_telegram_a(chat_id: str, texto_mensaje: str) -> bool:
    """Envía un mensaje a un chat_id concreto (uso interno)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": texto_mensaje, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except requests.RequestException as e:
        print(f"[AVISO] No se pudo conectar con Telegram: {e}", file=sys.stderr)
        return False


def enviar_telegram(texto_mensaje: str) -> bool:
    """Envía un mensaje formateado en HTML a TODOS los chats suscritos
    (quienes han escrito /start al bot), más el chat fijo de
    TELEGRAM_CHAT_ID si está definido (para no romper instalaciones que
    aún lo configuren así manualmente)."""
    if not TELEGRAM_TOKEN:
        print(
            "[AVISO] Notificación por Telegram no configurada: define la "
            "variable de entorno TELEGRAM_BOT_TOKEN.",
            file=sys.stderr,
        )
        return False

    destinos = set(cargar_suscriptores_telegram())
    if TELEGRAM_CHAT_ID:
        destinos.add(str(TELEGRAM_CHAT_ID))

    if not destinos:
        print(
            "[AVISO] Nadie se ha suscrito al bot de Telegram todavía "
            "(nadie ha escrito /start).",
            file=sys.stderr,
        )
        return False

    exito_alguno = False
    for chat_id in destinos:
        if _enviar_mensaje_telegram_a(chat_id, texto_mensaje):
            exito_alguno = True
        else:
            print(f"[AVISO] Error enviando a Telegram (chat {chat_id}).")

    if exito_alguno:
        print(f"Notificación enviada a Telegram con éxito ({len(destinos)} destinatario(s)).")
    return exito_alguno


def escuchar_telegram_en_segundo_plano() -> None:
    """Bucle infinito (para ejecutar en un hilo aparte) que escucha los
    mensajes que le escriban al bot y da de alta/baja automáticamente a
    quien mande /start o /stop. Así el usuario medio no tiene que buscar
    su chat_id a mano: solo abre un enlace al bot y pulsa "Iniciar".

    Usa "long polling" (getUpdates), que no necesita servidor público ni
    configuración de webhook: sirve igual para un Flask corriendo en
    127.0.0.1.
    """
    if not TELEGRAM_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = None

    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(url, params=params, timeout=30)
            datos = resp.json()

            for actualizacion in datos.get("result", []):
                offset = actualizacion["update_id"] + 1
                mensaje = actualizacion.get("message") or {}
                texto = (mensaje.get("text") or "").strip().lower()
                chat_id = str((mensaje.get("chat") or {}).get("id", ""))
                if not chat_id:
                    continue

                if texto.startswith("/start"):
                    suscriptores = cargar_suscriptores_telegram()
                    if chat_id not in suscriptores:
                        suscriptores.add(chat_id)
                        guardar_suscriptores_telegram(suscriptores)
                    _enviar_mensaje_telegram_a(
                        chat_id,
                        "✅ ¡Listo! A partir de ahora recibirás aquí los avisos de "
                        "plazos próximos a vencer del Boletín Municipal de "
                        "Convocatorias.\n\nEscribe /stop si quieres dejar de recibirlos.",
                    )
                elif texto.startswith("/stop"):
                    suscriptores = cargar_suscriptores_telegram()
                    if chat_id in suscriptores:
                        suscriptores.discard(chat_id)
                        guardar_suscriptores_telegram(suscriptores)
                    _enviar_mensaje_telegram_a(
                        chat_id,
                        "🔕 Avisos desactivados. Escribe /start cuando quieras "
                        "volver a activarlos.",
                    )
        except requests.RequestException as e:
            print(f"[AVISO] Error escuchando Telegram, reintentando en 5s: {e}", file=sys.stderr)
            time.sleep(5)
        except Exception as e:
            print(f"[AVISO] Error inesperado escuchando Telegram: {e}", file=sys.stderr)
            time.sleep(5)


def notificar_plazos_urgentes(
    convocatorias: list[Convocatoria],
    municipio: str,
    dias_min: int = 3,
    dias_max: int = 5,
    ignorar_ya_notificadas: bool = True,
) -> dict:
    """
    Detecta las convocatorias en la ventana crítica de vencimiento y envía
    la alerta a Telegram (en bloques, para evitar el límite de longitud de
    un mensaje). El destino es siempre el chat configurado en el servidor
    (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID); no se puede elegir desde la web.

    Por diseño, nunca notifica convocatorias cuyo plazo vence HOY MISMO
    (dias_restantes == 0): ver generar_alertas_urgentes().
    """
    urgentes = generar_alertas_urgentes(convocatorias, dias_min, dias_max)

    if ignorar_ya_notificadas:
        ya_notificadas = cargar_notificadas()
        a_notificar = [c for c in urgentes if c.id_unico not in ya_notificadas]
    else:
        a_notificar = urgentes

    resultado = {
        "urgentes_detectadas": len(urgentes),
        "nuevas_a_notificar": len(a_notificar),
        "telegram": None,
    }

    if not a_notificar:
        return resultado

    # =========================================================================
    # DIVISION DINÁMICA DE MENSAJES (Para evitar "message is too long")
    # =========================================================================
    encabezado = f"🔔 <b>¡{len(a_notificar)} Convocatoria(s) Urgente(s) en {municipio}!</b>\n\n"
    mensaje_actual = encabezado
    
    enviado_ok = True  # Seguiremos el estado de todos los fragmentos enviados

    for c in a_notificar:
        # Construimos el bloque de texto de esta convocatoria individual
        bloque_item = f"📌 <b>{c.titulo}</b>\n"
        bloque_item += f"⏳ Días restantes: {c.dias_restantes if c.dias_restantes is not None else 'No definido'}\n"
        bloque_item += f"🔗 <a href='{c.url}'>Ver convocatoria</a>\n\n"
        
        # Si al añadir este bloque superamos los 4000 caracteres, enviamos lo acumulado hasta ahora
        if len(mensaje_actual) + len(bloque_item) > 4000:
            ok = enviar_telegram(mensaje_actual)
            if not ok:
                enviado_ok = False
            # Reiniciamos el mensaje para el siguiente bloque con aviso de continuación
            mensaje_actual = encabezado + "<i>(Continuación del listado...)</i>\n\n" + bloque_item
        else:
            mensaje_actual += bloque_item
            
    # Al salir del bucle, enviamos el último fragmento restante que quedó a medias
    if mensaje_actual != encabezado:
        ok = enviar_telegram(mensaje_actual)
        if not ok:
            enviado_ok = False

    resultado["telegram"] = "ok" if enviado_ok else "error"
    
    # Solo marcamos como notificadas si el envío general fue exitoso
    if ignorar_ya_notificadas and enviado_ok:
        ya_notificadas.update(c.id_unico for c in a_notificar)
        guardar_notificadas(ya_notificadas)

    return resultado

# =====================================================================
# 9. RESUMEN DE CONVOCATORIAS CON IA (Gemini, opcional)
# =====================================================================
#
# Este módulo es equivalente al fragmento que compartiste: descarga el PDF
# de la convocatoria, extrae su texto con PyMuPDF (fitz) y pide a Gemini
# un resumen en JSON. Se activa solo con --usar-ia y solo si hay clave
# disponible en GEMINI_API_KEY (o --gemini-key).

PROMPT_RESUMEN_IA = """\
Eres un asistente que ayuda a un pequeño ayuntamiento a entender convocatorias \
públicas (subvenciones, ayudas o licitaciones). Analiza el siguiente texto y \
devuelve EXCLUSIVAMENTE un JSON válido (sin texto adicional, sin markdown) con \
esta estructura exacta:

{{
  "resumen": "resumen claro en 2-4 frases, en español, de qué trata la convocatoria",
  "organismo": "organismo que convoca, o null si no aparece",
  "plazo": "fecha o condición límite tal como aparece en el texto, o null",
  "importe": "cuantía económica si aparece, o null",
  "requisitos_clave": ["lista breve de 1 a 4 requisitos principales"],
  "alerta_plazo_proximo": true/false  // true si el plazo vence en menos de 15 días desde hoy
}}

Texto de la convocatoria:
{texto}
"""


def _configurar_gemini(api_key: Optional[str]) -> bool:
    """Crea el cliente de Gemini (SDK google-genai). Devuelve False si no es posible."""
    global _GEMINI_CLIENT

    if genai_sdk is None:
        print("[AVISO] Falta la librería 'google-genai'. "
              "Instala con: pip install google-genai", file=sys.stderr)
        return False

    clave = api_key or os.environ.get("GEMINI_API_KEY")
    if not clave:
        print("[AVISO] No hay clave de Gemini disponible. Define la variable "
              "de entorno GEMINI_API_KEY o usa --gemini-key.", file=sys.stderr)
        return False

    try:
        _GEMINI_CLIENT = genai_sdk.Client(api_key=clave)
        return True
    except Exception as e:
        print(f"[AVISO] No se pudo inicializar el cliente de Gemini: {e}", file=sys.stderr)
        return False


def descargar_texto_convocatoria(conv: Convocatoria) -> Optional[str]:
    """
    Obtiene el texto de la convocatoria: si el enlace es un PDF lo descarga
    y extrae el texto con PyMuPDF; si es HTML, extrae el texto de la página.
    """
    if not conv.url:
        return None

    try:
        resp = requests.get(conv.url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[AVISO] No se pudo descargar {conv.url}: {e}", file=sys.stderr)
        return None

    es_pdf = "application/pdf" in resp.headers.get("Content-Type", "") or conv.url.lower().endswith(".pdf")

    if es_pdf:
        if fitz is None:
            print("[AVISO] Falta 'pymupdf' para leer PDFs. "
                  "Instala con: pip install pymupdf", file=sys.stderr)
            return None
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(resp.content)
            ruta_tmp = tmp.name
        try:
            texto = ""
            with fitz.open(ruta_tmp) as doc:
                for pagina in doc:
                    texto += pagina.get_text()
            return texto.strip() or None
        finally:
            os.unlink(ruta_tmp)

    # HTML: quitamos etiquetas de forma simple si no hay BeautifulSoup
    if BeautifulSoup is not None:
        return BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"<[^>]+>", " ", resp.text).strip() or None


def completar_plazo_e_importe_con_texto_completo(
    convocatorias: list[Convocatoria], max_documentos: int = 25
) -> None:
    """Respaldo por expresiones regulares (SIN IA) para cuando el título no
    trae ni plazo ni importe.

    El título de un anuncio del BOE/DOCM/BOP es, muchas veces, solo el
    encabezado legal ("Real Decreto X/2026, de..., por el que se modifica
    el Real Decreto Y..."), y la fecha límite o la cuantía real están en
    algún artículo del documento completo, no en el título. Para esos casos
    descargamos el documento (PDF o HTML, reutilizando la misma función que
    usa el resumen con IA) y volvemos a pasarle extraer_plazo()/
    extraer_importe() al texto íntegro.

    Como implica una descarga por convocatoria, es más lento que analizar
    solo el título, así que se limita a `max_documentos` como mucho,
    empezando por las que no tienen NINGÚN dato (para aprovechar mejor el
    límite). Las que ya tienen plazo e importe no se tocan.
    """
    pendientes = [c for c in convocatorias if not c.plazo or not c.importe]
    pendientes.sort(key=lambda c: (c.plazo is not None) + (c.importe is not None))

    for conv in pendientes[:max_documentos]:
        texto = descargar_texto_convocatoria(conv)
        if not texto:
            continue

        if not conv.plazo:
            plazo, dias = extraer_plazo(texto)
            if plazo:
                conv.plazo = plazo
                conv.dias_restantes = dias

        if not conv.importe:
            importe = extraer_importe(texto)
            if importe:
                conv.importe = importe


def resumir_con_ia(texto: str, modelo: str = "gemini-2.5-flash-lite") -> Optional[dict]:
    """
    Envía el texto a Gemini y devuelve un dict con resumen, organismo, plazo,
    importe, requisitos_clave y alerta_plazo_proximo. Devuelve None si falla.
    """
    if _GEMINI_CLIENT is None:
        return None

    # Los modelos gratuitos tienen límite de tokens de entrada; recortamos
    # a un tamaño razonable para no agotar la cuota del tier gratuito.
    texto_recortado = texto[:15000]

    try:
        respuesta = _GEMINI_CLIENT.models.generate_content(
            model=modelo,
            contents=PROMPT_RESUMEN_IA.format(texto=texto_recortado),
        )
        contenido = (respuesta.text or "").strip()
        # Por si el modelo envuelve el JSON en ```json ... ```
        contenido = re.sub(r"^```(json)?|```$", "", contenido, flags=re.MULTILINE).strip()
        return json.loads(contenido)
    except json.JSONDecodeError:
        print("[AVISO] La IA no devolvió un JSON válido; se omite el resumen.", file=sys.stderr)
        return None
    except Exception as e:  # errores de red, cuota agotada, etc.
        print(f"[AVISO] Error al llamar a Gemini: {e}", file=sys.stderr)
        return None


def enriquecer_con_ia(convocatorias: list[Convocatoria], modelo: str, max_llamadas: int) -> None:
    """
    Recorre las convocatorias (hasta `max_llamadas`, para no agotar el tier
    gratuito) y rellena/mejora resumen, organismo, plazo e importe con IA.
    Modifica los objetos Convocatoria in-place.
    """
    print(f"\n→ Generando resúmenes con IA (máx. {max_llamadas} convocatorias)...")
    procesadas = 0

    for conv in convocatorias:
        if procesadas >= max_llamadas:
            print(f"[INFO] Límite de {max_llamadas} llamadas a la IA alcanzado; "
                  f"el resto se queda sin resumen automático.")
            break

        texto = descargar_texto_convocatoria(conv)
        if not texto:
            continue

        datos = resumir_con_ia(texto, modelo=modelo)
        procesadas += 1

        if not datos:
            continue

        conv.resumen = datos.get("resumen") or conv.resumen
        conv.organismo = datos.get("organismo") or conv.organismo
        conv.importe = datos.get("importe") or conv.importe

        # El plazo detectado por IA solo sustituye al del regex si este
        # último no encontró nada (el regex, cuando acierta, suele ser
        # más fiable porque trabaja con el texto literal).
        if not conv.plazo and datos.get("plazo"):
            conv.plazo = datos["plazo"]

        requisitos = datos.get("requisitos_clave")
        if requisitos:
            conv.resumen = f"{conv.resumen}\n- Requisitos clave: " + "; ".join(requisitos)

        if datos.get("alerta_plazo_proximo") and conv.dias_restantes is None:
            # La IA detectó urgencia en el texto aunque el regex no pudo
            # calcular los días exactos (p.ej. "próxima semana").
            conv.plazo = f"{conv.plazo or ''} ⚠️ Plazo próximo a vencer (detectado por IA)".strip()


# =====================================================================
# 10. ORQUESTACIÓN PRINCIPAL
# =====================================================================

def ejecutar(municipio: str, provincia: str, dias_atras: int,
             areas_interes: list[str], dias_aviso: int, salida: str,
             usar_ia: bool = False, gemini_key: Optional[str] = None,
             gemini_modelo: str = "gemini-2.5-flash-lite", max_ia: int = 15,
             notificar: bool = False, dias_min_urgente: int = 3,
             dias_max_urgente: int = 5) -> None:

    print(f"Monitorizando fuentes oficiales para: {municipio}")
    print(f"Provincia: {provincia} | Últimos {dias_atras} días | Alerta de plazo: {dias_aviso} días\n")

    todas: list[Convocatoria] = []

    print("→ Consultando BOE (API oficial)...")
    todas += fetch_boe(dias_atras)

    print("→ Consultando DOCM (plantilla de scraping)...")
    todas += fetch_docm(dias_atras)

    print("→ Consultando BOP (plantilla de scraping)...")
    todas += fetch_bop(provincia, dias_atras)

    # Filtrado por área de interés del municipio
    relevantes = [c for c in todas if coincide_area_interes(c, areas_interes)]

    # Resumen en lenguaje natural con IA (opcional)
    if usar_ia:
        if _configurar_gemini(gemini_key):
            enriquecer_con_ia(relevantes, modelo=gemini_modelo, max_llamadas=max_ia)
        else:
            print("[AVISO] Se omite el resumen con IA (falta librería o clave).", file=sys.stderr)

    # Evitar duplicados respecto a ejecuciones anteriores
    vistas = cargar_vistas()
    nuevas = [c for c in relevantes if c.id_unico not in vistas]
    vistas.update(c.id_unico for c in relevantes)
    guardar_vistas(vistas)

    print(f"\nTotal detectadas: {len(todas)} | Relevantes para el municipio: {len(relevantes)} | Nuevas: {len(nuevas)}")

    # Informe semanal (con todas las relevantes, no solo las nuevas)
    informe = generar_resumen_semanal(relevantes, municipio)
    salida_path = Path(salida)
    salida_path.write_text(informe, encoding="utf-8")
    print(f"\nResumen semanal guardado en: {salida_path.resolve()}")

    # Alertas de plazo próximo (listado informativo, ventana amplia)
    alertas = generar_alertas_plazo(relevantes, dias_aviso)
    if alertas:
        print(f"\n⚠️  ALERTAS DE PLAZO PRÓXIMO A VENCER ({len(alertas)}):")
        for a in alertas:
            print(f"  - [{a.dias_restantes} días] {a.titulo}  ->  {a.url}")
    else:
        print("\nNo hay convocatorias con plazo próximo a vencer.")

    # Notificación por Telegram para la ventana crítica de vencimiento
    # (por defecto, entre 3 y 5 días). Requiere TELEGRAM_BOT_TOKEN y
    # TELEGRAM_CHAT_ID configurados como variables de entorno.
    if notificar:
        resultado_notif = notificar_plazos_urgentes(
            relevantes, municipio,
            dias_min=dias_min_urgente, dias_max=dias_max_urgente,
        )
        print(
            f"\n📣 Notificaciones urgentes ({dias_min_urgente}-{dias_max_urgente} días): "
            f"{resultado_notif['urgentes_detectadas']} detectadas, "
            f"{resultado_notif['nuevas_a_notificar']} nuevas notificadas "
            f"(telegram={resultado_notif['telegram']})"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Herramienta de asistencia para convocatorias públicas — "
                    "vigilancia automatizada de boletines oficiales."
    )
    parser.add_argument("--municipio", default="Ayuntamiento",
                         help="Nombre del municipio/entidad (para el informe).")
    parser.add_argument("--provincia", default="Toledo",
                         help="Provincia, para seleccionar el BOP correspondiente.")
    parser.add_argument("--dias", type=int, default=7,
                         help="Número de días hacia atrás a revisar en BOE/DOCM/BOP.")
    parser.add_argument("--area-interes", nargs="*", default=[],
                         help="Palabras clave de las áreas de interés del municipio "
                              "(ej: cultura deporte infraestructuras). Vacío = todo.")
    parser.add_argument("--plazo-alerta", type=int, default=10,
                         help="Días de antelación para alertar sobre plazos que vencen.")
    parser.add_argument("--salida", default="resumen_semanal.md",
                         help="Ruta del fichero Markdown de salida.")
    parser.add_argument("--usar-ia", action="store_true",
                         help="Genera un resumen de cada convocatoria con Gemini "
                              "(requiere GEMINI_API_KEY y 'pip install google-genai pymupdf').")
    parser.add_argument("--gemini-key", default=None,
                         help="Clave de la API de Gemini. Solo para pruebas puntuales; "
                              "se recomienda usar la variable de entorno GEMINI_API_KEY.")
    parser.add_argument("--gemini-modelo", default="gemini-2.5-flash-lite",
                         help="Modelo de Gemini a usar (por defecto, uno económico del tier gratuito).")
    parser.add_argument("--max-ia", type=int, default=15,
                         help="Número máximo de convocatorias a resumir con IA por ejecución "
                              "(para no agotar la cuota gratuita).")

    # --- Notificación de plazo urgente por Telegram ---
    parser.add_argument("--notificar", action="store_true",
                         help="Activa el envío de un aviso por Telegram para convocatorias en la "
                              "ventana crítica de vencimiento (por defecto, entre 3 y 5 días). "
                              "Requiere las variables de entorno TELEGRAM_BOT_TOKEN y "
                              "TELEGRAM_CHAT_ID.")
    parser.add_argument("--dias-min-urgente", type=int, default=3,
                         help="Días mínimos restantes para considerar el plazo urgente "
                              "(nunca se notifica con 0 días restantes, aunque se ponga aquí).")
    parser.add_argument("--dias-max-urgente", type=int, default=5,
                         help="Días máximos restantes para considerar el plazo urgente.")

    args = parser.parse_args()

    ejecutar(
        municipio=args.municipio,
        provincia=args.provincia,
        dias_atras=args.dias,
        areas_interes=args.area_interes,
        dias_aviso=args.plazo_alerta,
        salida=args.salida,
        usar_ia=args.usar_ia,
        gemini_key=args.gemini_key,
        gemini_modelo=args.gemini_modelo,
        max_ia=args.max_ia,
        notificar=args.notificar,
        dias_min_urgente=args.dias_min_urgente,
        dias_max_urgente=args.dias_max_urgente,
    )


if __name__ == "__main__":
    main()
