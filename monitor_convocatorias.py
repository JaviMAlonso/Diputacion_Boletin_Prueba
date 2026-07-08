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

HEADERS = {"User-Agent": "MonitorConvocatoriasPublicas/1.0 (uso institutional)"}
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
# 3. UTILIDADES DE EXTRACCIÓN (Optimizado con reglas mejoradas)
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
    hoy = date.today()

    # 1. Patrón amplio "hasta el dd de <mes> [de aaaa]"
    meses_patron = "|".join(MESES.keys())
    m = re.search(
        r"(?:hasta\s+el\s+|vence\s+el\s+|término\s+el\s+)?(\d{1,2})\s+de\s+(" + meses_patron + r")(?:\s+de\s+(\d{4}))?",
        texto_low,
    )
    if m:
        dia, mes_nombre, anio_opt = m.groups()
        anio = int(anio_opt) if anio_opt else hoy.year
        try:
            fecha_limite = date(anio, MESES[mes_nombre], int(dia))
            # Si el boletín omitió el año y la fecha resultante ya pasó, pertenece al año siguiente
            if not anio_opt and fecha_limite < hoy:
                fecha_limite = date(anio + 1, MESES[mes_nombre], int(dia))
            dias = (fecha_limite - hoy).days
            return f"Hasta el {dia} de {mes_nombre} de {fecha_limite.year}", dias
        except ValueError:
            pass

    # 2. Patrón estándar dd/mm/aaaa o dd-mm-aaaa
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", texto)
    if m:
        dia, mes, anio = map(int, m.groups())
        try:
            fecha_limite = date(anio, mes, dia)
            dias = (fecha_limite - hoy).days
            return f"Hasta el {dia:02d}/{mes:02d}/{anio}", dias
        except ValueError:
            pass

    # 3. Patrón genérico "plazo de X días"
    m = re.search(r"plazo\s+de\s+(\d{1,3})\s+d[ií]as", texto_low)
    if m:
        dias = int(m.group(1))
        return f"Plazo de {dias} días desde la publicación", None

    return None, None


def extraer_importe(texto: str) -> Optional[str]:
    """Busca cuantías económicas en formato numérico o escritas en texto."""
    texto_low = texto.lower()

    # 1. Búsqueda de formato numérico estándar (ej: 125.000,00 €)
    m_num = re.search(r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s*(?:€|euros?)", texto, re.IGNORECASE)
    if m_num:
        return f"{m_num.group(1)} €"

    # 2. Búsqueda de importes escritos literalmente en letras
    m_letras = re.search(r"((?:un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|ciento|doscientos|trescientos|cuatrocientos|quinientos|seiscientos|setecientos|ochocientos|novecientos|mil|mill[oó]n|millones|\s)+)\s*(?:€|euros?)", texto_low)
    if m_letras:
        resultado_letras = m_letras.group(1).strip()
        if len(resultado_letras) > 2:
            return f"{resultado_letras.capitalize()} €"

    # 3. Porcentajes máximos de cofinanciación
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
# 4. FUENTE 1: BOE — API oficial de datos abiertos
# =====================================================================

def _como_lista(valor):
    if valor is None:
        return []
    if isinstance(valor, list):
        return valor
    return [valor]


def fetch_boe(dias_atras: int = 7) -> list[Convocatoria]:
    resultados: list[Convocatoria] = []
    hoy = date.today()
    headers_json = {**HEADERS, "Accept": "application/json"}

    for delta in range(dias_atras):
        fecha = hoy - timedelta(days=delta)
        url = f"https://boe.es/datosabiertos/api/boe/sumario/{fecha.strftime('%Y%m%d')}"

        try:
            resp = requests.get(url, headers=headers_json, timeout=15)
            if resp.status_code != 200:
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
# =====================================================================

def fetch_docm(dias_atras: int = 7) -> list[Convocatoria]:
    resultados: list[Convocatoria] = []

    if BeautifulSoup is None:
        print("[AVISO] BeautifulSoup no instalado: se omite DOCM.", file=sys.stderr)
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
# =====================================================================

def fetch_bop(provincia: str = "Toledo", dias_atras: int = 7) -> list[Convocatoria]:
    resultados: list[Convocatoria] = []

    if BeautifulSoup is None:
        print("[AVISO] BeautifulSoup no instalado: se omite BOP.", file=sys.stderr)
        return resultados

    bop_urls = {
        "toledo": "https://bop.diputoledo.es",
        "ciudad real": "https://bop.dipucr.es",
        "cuenca": "https://www.dipucuenca.es/bop",
        "guadalajara": "https://bop.dguadalajara.es",
        "albacete": "https://bop.dipualba.es",
    }
    base_url = bop_urls.get(provincia.lower())
    if not base_url:
        print(f"[AVISO] No hay URL configurada para la provincia '{provincia}'.", file=sys.stderr)
        return resultados

    try:
        resp = requests.get(base_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[AVISO] No se pudo acceder al BOP de {provincia}: {e}", file=sys.stderr)
        return resultados

    soup = BeautifulSoup(resp.text, "html.parser")

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


def fetch_bop_toledo_resumen_dia(fecha: "date | None" = None) -> list[dict]:
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

    for enlace in soup.find_all("a"):
        texto_enlace = enlace.get_text(strip=True).lower()
        if "ver anuncio" not in texto_enlace:
            continue

        href = enlace.get("href", "").strip()
        if not href:
            continue
        url_pdf = href if href.startswith("http") else f"https://bop.diputoledo.es{href}"

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
    return [
        c for c in convocatorias
        if c.dias_restantes is not None and 0 <= c.dias_restantes <= dias_aviso
    ]


# =====================================================================
# 8 BIS. NOTIFICACIONES DE PLAZO URGENTE (Email)
# =====================================================================

NOTIFICADAS_PATH = Path("convocatorias_notificadas.json")


def cargar_notificadas() -> set[str]:
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
    return [
        c for c in convocatorias
        if c.dias_restantes is not None and dias_min <= c.dias_restantes <= dias_max
    ]


def construir_mensaje_alerta(convocatorias: list[Convocatoria], municipio: str) -> str:
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
# 9. RESUMEN DE CONVOCATORIAS CON IA (Gemini)
# =====================================================================

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
  "alerta_plazo_proximo": true/false
}}

Texto de la convocatoria:
{texto}
"""


def _configurar_gemini(api_key: Optional[str]) -> bool:
    global _GEMINI_CLIENT

    if genai_sdk is None:
        print("[AVISO] Falta la librería 'google-genai'.", file=sys.stderr)
        return False

    clave = api_key or os.environ.get("GEMINI_API_KEY")
    if not clave:
        print("[AVISO] No hay clave de Gemini disponible.", file=sys.stderr)
        return False

    try:
        _GEMINI_CLIENT = genai_sdk.Client(api_key=clave)
        return True
    except Exception as e:
        print(f"[AVISO] No se pudo inicializar el cliente de Gemini: {e}", file=sys.stderr)
        return False


def descargar_texto_convocatoria(conv: Convocatoria) -> Optional[str]:
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
            print("[AVISO] Falta 'pymupdf' para leer PDFs.", file=sys.stderr)
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

    if BeautifulSoup is not None:
        return BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"<[^>]+>", " ", resp.text).strip() or None


def resumir_con_ia(texto: str, modelo: str = "gemini-2.5-flash-lite") -> Optional[dict]:
    if _GEMINI_CLIENT is None:
        return None

    texto_recortado = texto[:15000]

    try:
        respuesta = _GEMINI_CLIENT.models.generate_content(
            model=modelo,
            contents=PROMPT_RESUMEN_IA.format(texto=texto_recortado),
        )
        contenido = (respuesta.text or "").strip()
        contenido = re.sub(r"^```(json)?|```$", "", contenido, flags=re.MULTILINE).strip()
        return json.loads(contenido)
    except json.JSONDecodeError:
        print("[AVISO] La IA no devolvió un JSON válido; se omite el resumen.", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[AVISO] Error al llamar a Gemini: {e}", file=sys.stderr)
        return None


def enriquecer_con_ia(convocatorias: list[Convocatoria], modelo: str, max_llamadas: int) -> None:
    print(f"\n→ Generando resúmenes con IA (máx. {max_llamadas} convocatorias)...")
    procesadas = 0

    for conv in convocatorias:
        if procesadas >= max_llamadas:
            print(f"[INFO] Límite de {max_llamadas} llamadas a la IA alcanzado.")
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

        if not conv.plazo and datos.get("plazo"):
            conv.plazo = datos["plazo"]

        requisitos = datos.get("requisitos_clave")
        if requisitos:
            conv.resumen = f"{conv.resumen}\n- Requisitos clave: " + "; ".join(requisitos)

        if datos.get("alerta_plazo_proximo") and conv.dias_restantes is None:
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

    relevantes = [c for c in todas if coincide_area_interes(c, areas_interes)]

    if usar_ia:
        if _configurar_gemini(gemini_key):
            enriquecer_con_ia(relevantes, modelo=gemini_modelo, max_llamadas=max_ia)
        else:
            print("[AVISO] Se omite el resumen con IA.", file=sys.stderr)

    vistas = cargar_vistas()
    nuevas = [c for c in relevantes if c.id_unico not in vistas]
    vistas.update(c.id_unico for c in relevantes)
    guardar_vistas(vistas)

    print(f"\nTotal detectadas: {len(todas)} | Relevantes para el municipio: {len(relevantes)} | Nuevas: {len(nuevas)}")

    informe = generar_resumen_semanal(relevantes, municipio)
    salida_path = Path(salida)
    salida_path.write_text(informe, encoding="utf-8")
    print(f"\nResumen semanal guardado en: {salida_path.resolve()}")

    alertas = generar_alertas_plazo(relevantes, dias_aviso)
    if alertas:
        print(f"\n⚠️  ALERTAS DE PLAZO PRÓXIMO A VENCER ({len(alertas)}):")
        for a in alertas:
            print(f"  - [{a.dias_restantes} días] {a.titulo}  ->  {a.url}")
    else:
        print("\nNo hay convocatorias con plazo próximo a vencer.")

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
        description="Herramienta de asistencia para convocatorias públicas."
    )
    parser.add_argument("--municipio", default="Ayuntamiento", help="Nombre del municipio.")
    parser.add_argument("--provincia", default="Toledo", help="Provincia para el BOP.")
    parser.add_argument("--dias", type=int, default=7, help="Días hacia atrás a revisar.")
    parser.add_argument("--area-interes", nargs="*", default=[], help="Áreas de interés.")
    parser.add_argument("--plazo-alerta", type=int, default=10, help="Días para alerta de plazo.")
    parser.add_argument("--salida", default="resumen_semanal.md", help="Ruta de salida Markdown.")
    parser.add_argument("--usar-ia", action="store_true", help="Usa Gemini para resumir.")
    parser.add_argument("--gemini-key", default=None, help="Clave API Gemini.")
    parser.add_argument("--gemini-modelo", default="gemini-2.5-flash-lite", help="Modelo de Gemini.")
    parser.add_argument("--max-ia", type=int, default=15, help="Máximo llamadas IA por ejecución.")

    parser.add_argument("--notificar", action="store_true", help="Activa envío por correo.")
    parser.add_argument("--dias-min-urgente", type=int, default=3, help="Días mínimos urgencia.")
    parser.add_argument("--dias-max-urgente", type=int, default=5, help="Días máximos urgencia.")

    parser.add_argument("--email-destino", nargs="*", default=[], help="Destinatarios de correo.")
    parser.add_argument("--email-remitente", default=os.environ.get("ALERTA_EMAIL_REMITENTE"), help="Remitente.")
    parser.add_argument("--email-password", default=os.environ.get("ALERTA_EMAIL_PASSWORD"), help="Password de app.")
    parser.add_argument("--smtp-server", default=os.environ.get("ALERTA_SMTP_SERVER", "smtp.gmail.com"), help="Servidor SMTP.")
    parser.add_argument("--smtp-port", type=int, default=int(os.environ.get("ALERTA_SMTP_PORT", 587)), help="Puerto SMTP.")

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