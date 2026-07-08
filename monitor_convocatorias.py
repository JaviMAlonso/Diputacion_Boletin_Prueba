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
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests

try:
    from bs4 import BeautifulSoup  # usado por los scrapers de DOCM/BOP
except ImportError:
    BeautifulSoup = None  # se avisa en tiempo de ejecución si falta

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
    """
    texto_low = texto.lower()

    # Patrón "dd de <mes> de aaaa"
    m = re.search(
        r"(\d{1,2})\s+de\s+(" + "|".join(MESES.keys()) + r")\s+de\s+(\d{4})",
        texto_low,
    )
    if m:
        dia, mes_nombre, anio = m.groups()
        try:
            fecha_limite = date(int(anio), MESES[mes_nombre], int(dia))
            dias = (fecha_limite - date.today()).days
            return f"Hasta el {dia} de {mes_nombre} de {anio}", dias
        except ValueError:
            pass

    # Patrón dd/mm/aaaa
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", texto)
    if m:
        dia, mes, anio = map(int, m.groups())
        try:
            fecha_limite = date(anio, mes, dia)
            dias = (fecha_limite - date.today()).days
            return f"Hasta el {dia:02d}/{mes:02d}/{anio}", dias
        except ValueError:
            pass

    # Patrón "plazo de X días (hábiles/naturales)"
    m = re.search(r"plazo\s+de\s+(\d{1,3})\s+d[ií]as", texto_low)
    if m:
        dias = int(m.group(1))
        return f"Plazo de {dias} días desde la publicación", None

    return None, None


def extraer_importe(texto: str) -> Optional[str]:
    """Busca cuantías económicas del tipo '1.234,56 euros' o '1.234.567 €'.
    Si no encuentra ningún importe en euros, busca un porcentaje máximo de
    financiación (p. ej. 'hasta el 80%', 'máximo del 50 %', '80% del coste'),
    que en muchas subvenciones sustituye a una cuantía fija.
    """
    m = re.search(r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s*(?:€|euros)", texto, re.IGNORECASE)
    if m:
        return f"{m.group(1)} €"

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
    """Devuelve una lista de dicts {titulo, url_pdf} con los anuncios del
    BOP de Toledo publicados en la fecha indicada (por defecto, hoy).
    No aplica ninguna clasificación ni filtro: es el listado en bruto,
    pensado para el modo "ver solo lo publicado hoy" de la interfaz web.
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
    vistos: set[str] = set()

    # Cada anuncio incluye un enlace "Ver anuncio" que lleva al PDF (o a
    # una ficha con el PDF incrustado). Buscamos ese enlace y, a partir
    # de su bloque contenedor, extraemos el título en "Resumen/Asunto".
    for enlace in soup.find_all("a"):
        texto_enlace = enlace.get_text(strip=True).lower()
        if "ver anuncio" not in texto_enlace:
            continue

        href = enlace.get("href", "").strip()
        if not href:
            continue
        url_pdf = href if href.startswith("http") else f"https://bop.diputoledo.es{href}"

        # Subimos hasta encontrar el bloque que contiene también el
        # "Resumen/Asunto" de este mismo anuncio (evita coger el título
        # de otro anuncio vecino si el contenedor es demasiado amplio).
        contenedor = enlace
        titulo = ""
        for _ in range(6):
            contenedor = contenedor.find_parent()
            if contenedor is None:
                break
            texto_bloque = contenedor.get_text(" ", strip=True)
            if "resumen/asunto" in texto_bloque.lower():
                partes = re.split(r"resumen/asunto\s*:?\s*", texto_bloque, flags=re.IGNORECASE)
                if len(partes) > 1:
                    titulo = partes[-1].strip()
                break

        if not titulo:
            continue

        # Evita duplicados (el mismo anuncio puede aparecer en más de
        # un bloque anidado durante la búsqueda hacia arriba).
        clave = (titulo, url_pdf)
        if clave in vistos:
            continue
        vistos.add(clave)

        resultados.append({"titulo": titulo, "url_pdf": url_pdf})

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
# 8 BIS. NOTIFICACIONES DE PLAZO URGENTE (Email)
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
# El correo remitente, su contraseña de aplicación y el servidor SMTP se
# configuran una sola vez en el servidor (variables de entorno), no en el
# formulario: quien usa la web solo indica a qué correo quiere que llegue
# el aviso.

NOTIFICADAS_PATH = Path("convocatorias_notificadas.json")


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


def generar_alertas_urgentes(
    convocatorias: list[Convocatoria], dias_min: int = 3, dias_max: int = 5
) -> list[Convocatoria]:
    """
    Devuelve las convocatorias cuyo plazo vence dentro de la ventana crítica
    [dias_min, dias_max] (ambos incluidos). Por defecto, entre 3 y 5 días.
    """
    return [
        c for c in convocatorias
        if c.dias_restantes is not None and dias_min <= c.dias_restantes <= dias_max
    ]


def construir_mensaje_alerta(convocatorias: list[Convocatoria], municipio: str) -> str:
    """Construye un mensaje de texto plano para el correo de alerta, con el
    listado de convocatorias en la ventana crítica de vencimiento."""
    lineas = [f"⚠️ Alerta de plazos próximos a vencer — {municipio}", ""]
    for c in sorted(convocatorias, key=lambda x: x.dias_restantes):
        lineas.append(f"• {c.titulo}")
        lineas.append(f"  Quedan {c.dias_restantes} día(s) · Fuente: {c.fuente}")
        if c.organismo:
            lineas.append(f"  Organismo: {c.organismo}")
        lineas.append(f"  Enlace: {c.url}")
        lineas.append("")
    return "\n".join(lineas).strip()


def enviar_email(
    destinatarios: list[str],
    asunto: str,
    cuerpo: str,
    remitente: str,
    password: str,
    smtp_server: str = "smtp.gmail.com",
    smtp_port: int = 587,
) -> bool:
    """
    Envía un correo mediante SMTP con STARTTLS. Con Gmail, `password` debe
    ser una "contraseña de aplicación" (no la contraseña normal de la
    cuenta): https://myaccount.google.com/apppasswords
    """
    import smtplib
    from email.mime.text import MIMEText

    if not destinatarios or not remitente or not password:
        return False

    mensaje = MIMEText(cuerpo, "plain", "utf-8")
    mensaje["Subject"] = asunto
    mensaje["From"] = remitente
    mensaje["To"] = ", ".join(destinatarios)

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as servidor:
            servidor.starttls()
            servidor.login(remitente, password)
            servidor.sendmail(remitente, destinatarios, mensaje.as_string())
        return True
    except Exception as e:
        print(f"[AVISO] No se pudo enviar el correo de alerta: {e}", file=sys.stderr)
        return False


def notificar_plazos_urgentes(
    convocatorias: list[Convocatoria],
    municipio: str,
    dias_min: int = 3,
    dias_max: int = 5,
    email_cfg: Optional[dict] = None,
    ignorar_ya_notificadas: bool = True,
) -> dict:
    """
    Punto de entrada único: detecta las convocatorias en la ventana crítica
    de vencimiento (dias_min a dias_max) y envía un correo de aviso. Evita
    reenviar el mismo aviso en ejecuciones posteriores (salvo que
    `ignorar_ya_notificadas=False`).

    email_cfg: {"destinatarios": [...], "remitente": "...", "password": "...",
                "smtp_server": "...", "smtp_port": 587}

    Devuelve un resumen del resultado, útil para mostrar en la interfaz web
    o en el log de la CLI.
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
        "email": None,
    }

    if not a_notificar:
        return resultado

    mensaje = construir_mensaje_alerta(a_notificar, municipio)
    asunto = f"⚠️ {len(a_notificar)} convocatoria(s) próximas a vencer — {municipio}"

    if email_cfg:
        ok = enviar_email(
            destinatarios=email_cfg.get("destinatarios", []),
            asunto=asunto,
            cuerpo=mensaje,
            remitente=email_cfg.get("remitente", ""),
            password=email_cfg.get("password", ""),
            smtp_server=email_cfg.get("smtp_server", "smtp.gmail.com"),
            smtp_port=int(email_cfg.get("smtp_port", 587)),
        )
        resultado["email"] = "ok" if ok else "error"

    if ignorar_ya_notificadas:
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
             dias_max_urgente: int = 5, email_cfg: Optional[dict] = None) -> None:

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

    # Notificación por email para la ventana crítica de vencimiento
    # (por defecto, entre 3 y 5 días)
    if notificar and email_cfg:
        resultado_notif = notificar_plazos_urgentes(
            relevantes, municipio,
            dias_min=dias_min_urgente, dias_max=dias_max_urgente,
            email_cfg=email_cfg,
        )
        print(
            f"\n📣 Notificaciones urgentes ({dias_min_urgente}-{dias_max_urgente} días): "
            f"{resultado_notif['urgentes_detectadas']} detectadas, "
            f"{resultado_notif['nuevas_a_notificar']} nuevas notificadas "
            f"(email={resultado_notif['email']})"
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

    # --- Notificación de plazo urgente por email ---
    parser.add_argument("--notificar", action="store_true",
                         help="Activa el envío de un aviso por correo para convocatorias en la "
                              "ventana crítica de vencimiento (por defecto, entre 3 y 5 días).")
    parser.add_argument("--dias-min-urgente", type=int, default=3,
                         help="Días mínimos restantes para considerar el plazo urgente.")
    parser.add_argument("--dias-max-urgente", type=int, default=5,
                         help="Días máximos restantes para considerar el plazo urgente.")

    parser.add_argument("--email-destino", nargs="*", default=[],
                         help="Uno o varios correos destinatarios del aviso.")
    parser.add_argument("--email-remitente", default=os.environ.get("ALERTA_EMAIL_REMITENTE"),
                         help="Cuenta de correo remitente (o var. de entorno ALERTA_EMAIL_REMITENTE).")
    parser.add_argument("--email-password", default=os.environ.get("ALERTA_EMAIL_PASSWORD"),
                         help="Contraseña de aplicación del remitente (o var. de entorno "
                              "ALERTA_EMAIL_PASSWORD). Con Gmail, usar una contraseña de "
                              "aplicación: https://myaccount.google.com/apppasswords")
    parser.add_argument("--smtp-server", default=os.environ.get("ALERTA_SMTP_SERVER", "smtp.gmail.com"),
                         help="Servidor SMTP del remitente (por defecto, Gmail).")
    parser.add_argument("--smtp-port", type=int,
                         default=int(os.environ.get("ALERTA_SMTP_PORT", 587)),
                         help="Puerto SMTP (587 = STARTTLS, habitual).")

    args = parser.parse_args()

    email_cfg = None
    if args.email_destino and args.email_remitente and args.email_password:
        email_cfg = {
            "destinatarios": args.email_destino,
            "remitente": args.email_remitente,
            "password": args.email_password,
            "smtp_server": args.smtp_server,
            "smtp_port": args.smtp_port,
        }

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
        email_cfg=email_cfg,
    )


if __name__ == "__main__":
    main()
