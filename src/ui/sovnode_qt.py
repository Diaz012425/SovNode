# Copyright (c) 2026 Stephen Díaz
# SovNode - Local Desktop AI Application
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
SovNode — Sovereign AI Node
============================

Cliente de escritorio local basado en PyQt6 con integración de:
- Razonamiento dinámico e intenciones visuales en tiempo real.
- Búsqueda web enriquecida y blindada (multi-capa, con degradación
  controlada y retroalimentación explícita a la UI).
- Scroll inteligente: el auto-scroll respeta la posición elegida por el
  usuario durante el streaming, sin glitches ni saltos forzados.
- Síntesis de voz local a demanda (TTS por menú de opciones según el idioma seleccionado).
"""

from __future__ import annotations


import contextlib
import ctypes
import html
import json
import logging
import os
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
import re
import sys
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import wave
from pipeline import EventType
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Pattern, Tuple
try:
    from web_search import (
        get_last_search_error,
        is_live_event_query,
        search_topic_images,
        search_web,
        text_is_relevant,
        WIKI_FINAL_EXTRACT_CHARS,
        WIKI_PRECISE_FACT_EXTRACT_CHARS,
        WIKI_SEQUENCE_BUDGET_SECONDS,
        WIKI_API_THUMB_SIZE,
        wiki_rank_search_candidates,
        wiki_fetch_single_extract,
        _extract_relevant_sentences,
    )
except ImportError:
    from web_search import search_web
    def get_last_search_error():
        return None
    def is_live_event_query(query):
        return False
    def text_is_relevant(query, text):
        return True
    def search_topic_images(query, max_results=3, lang=None, log_cb=None):
        return []
    WIKI_FINAL_EXTRACT_CHARS = 2500
    WIKI_PRECISE_FACT_EXTRACT_CHARS = 2500
    WIKI_SEQUENCE_BUDGET_SECONDS = 4.0
    WIKI_API_THUMB_SIZE = 800
    def wiki_rank_search_candidates(*args, **kwargs):
        return []
    def wiki_fetch_single_extract(*args, **kwargs):
        return None
try:
    from relevance import (
        asks_about_final,
        distinctive_words,
        extract_query_matchup_entities,
        extract_years,
        is_retrospective_title,
        needs_strict_relevance,
        requires_precise_fact,
        source_names_unconfirmed_participant,
        title_names_the_final,
    )
except ImportError:
    def asks_about_final(query):
        return False
    def distinctive_words(text):
        return set()
    def extract_query_matchup_entities(query, fallback_evidence=""):
        return set()
    def extract_years(text):
        return set()
    def is_retrospective_title(title):
        return False
    def needs_strict_relevance(query):
        return False
    def requires_precise_fact(query):
        return False
    def source_names_unconfirmed_participant(candidate_text, authoritative_text):
        return False
    def title_names_the_final(title):
        return False
from PyQt6.QtCore import (
    QBuffer,
    QByteArray,
    QEvent,
    QIODevice,
    Qt,
    QSettings,
    QSize,
    QThread,
    QTimeLine,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6 import sip
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QIcon,
    QImage,
    QImageReader,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSystemTrayIcon,
    QTabBar,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from logger import format_terminal_log
import icons
import math_render
from ollama_manager import OllamaProcessManager
from orchestrator import (
    Orchestrator,
)
from embeddings import prewarm_local_embedding_model
from ui import AutoResizingTextBrowser, ChatDropArea
from wal import WriteAheadLog
from async_executor import AsyncExecutor
from sys_optimizer import get_system_telemetry
from workspace_watcher import WorkspaceScanner

logger = logging.getLogger("SovNode.UI")




_NOISE_ANYWHERE_RE = re.compile(
    r"\b("
    r"por\s+favor|please|"
    r"dime|cu[eé]ntame|expl[ií]came|mu[eé]strame|dame|darse|proporci[oó]name|"
    r"tell\s+me|show\s+me|explain\s+to\s+me|give\s+me|"
    r"b[uú]scam?e?|busca(?:r)?|"
    r"search(?:\s+for)?|look\s+up|find\s+out|"
    r"investiga(?:r)?|averigua(?:r)?|consulta(?:r)?|encuentra(?:r)?|rastrea(?:r)?|chequea\w*|"
    r"investigate|check|research|"
    r"en\s+internet|en\s+google|en\s+la\s+web|en\s+l[ií]nea|"
    r"(?:on|in)\s+(?:the\s+)?internet|on\s+google|online|"
    r"(?:can|could|would|will)\s+you(?:\s+please)?"
    r")\b",
    re.IGNORECASE,
)

_LEADING_GREETINGS_RE = re.compile(
    r"^\s*[?¡!¿.,;:'\"]*(hola|hey|buenas|hi|hello)\b\s*[?¡!¿.,;:'\"]*\s*",
    re.IGNORECASE,
)

_LEADING_FILLERS_RE = re.compile(
    r"^\s*["
    r"?¡!¿.,;:'\"]*("
    r"el\s+resultado\s+y\s+detalles\s+de|los\s+resultados\s+de|el\s+resultado\s+de|"
    r"el\s+marcador\s+de|los\s+detalles\s+de|informaci[oó]n\s+sobre|detalles\s+sobre|"
    r"cu[aá]l\s+es|qu[eé]\s+pas[oó]\s+con|saber\s+sobre|quiero\s+saber|me\s+gustar[ií]a\s+saber|"
    r"i\s+want\s+to\s+know|i('d| would)\s+like\s+to\s+know|"
    r"podr[ií]as\s+decirme|quisiera\s+saber|"
    r"sabes\s+(?:algo\s+)?sobre|do\s+you\s+know\s+about"
    r")\b\s*["
    r"?¡!¿.,;:'\"]*\s*",
    re.IGNORECASE,
)

_TRAILING_NOISE_RE = re.compile(
    r"\s*\b("
    r"search|online|internet|busca(?:r)?|"
    r"con\s+todos\s+los\s+detalles|detalladamente|en\s+tiempo\s+real|"
    r"in\s+detail|in\s+real\s*time"
    r")\b\s*$",
    re.IGNORECASE,
)

_INTERNAL_NOISE_RE = re.compile(
    r"\b("
    r"las\s+estad[ií]sticas\s+de|las\s+especificaciones\s+de|los\s+detalles\s+de|"
    r"como\s+fue|que\s+paso|como\s+quedo|de\s+ese\s+partido|de\s+este\s+partido"
    r")\b",
    re.IGNORECASE,
)

_MAX_SANITIZE_PASSES = 6


def sanitize_query(query: str) -> str:
    """
    Sanitización bilingüe (ES/EN) blindada e ITERATIVA de la consulta del usuario.

    DELIBERADAMENTE una implementación propia, NO la de web_search.py
    (ver el comentario en el bloque de imports al principio del archivo):
    esta versión hace varias rondas y cubre ruido interno/de cierre
    además de rellenos iniciales — más de lo que necesita web_search.py
    para sus propios llamadores. Si algún día se decide unificarlas en
    una sola función compartida, hay que migrar TODOS los patrones de
    esta versión (no solo los de rellenos iniciales) para no perder
    cobertura, y actualizar el import de arriba para dejar de sombrearla
    en silencio.

    A diferencia de una única pasada de regex, las muletillas encadenadas
    ("hola, busca en internet por favor investiga sobre...") se eliminan en
    varias rondas hasta que el texto deja de cambiar (punto fijo) o se
    alcanza `_MAX_SANITIZE_PASSES`, lo que evite tanto residuos como bucles
    infinitos. Preserva agresivamente términos clave: no toca dígitos,
    fechas, comillas dentro del cuerpo del texto, ni nombres propios,
    porque solo opera sobre los bordes (^...$) y sobre un conjunto
    explícito de muletillas internas conocidas.

    Garantía: si el resultado limpio queda vacío o por debajo de 3
    caracteres útiles, se retorna la consulta original intacta —
    preferimos una consulta "sucia" a una vacía.
    """
    try:
        if not query:
            return ""

        original = query.strip()
        if not original:
            return ""

        q = original

        for _ in range(_MAX_SANITIZE_PASSES):
            before = q

            q = _NOISE_ANYWHERE_RE.sub(" ", q)

            q = _INTERNAL_NOISE_RE.sub(" ", q)

            q = _LEADING_GREETINGS_RE.sub("", q)

            q = _LEADING_FILLERS_RE.sub("", q)

            q = _TRAILING_NOISE_RE.sub("", q).strip()

            q = re.sub(r"\s{2,}", " ", q).strip()

            if q == before:
                break

        clean = re.sub(r"^[\s?¡!¿.,;:]+|[\s?¡!¿.,;:]+$", "", q)
        clean = re.sub(r"\s{2,}", " ", clean).strip()

        return clean if len(clean) >= 3 else original
    except Exception:
        # Blindaje absoluto: cualquier fallo inesperado en el motor de
        # sanitización nunca debe tumbar la búsqueda; se degrada a la
        # consulta cruda tal como la escribió el usuario.
        return (query or "").strip()


def clean_html_text(raw_html: str) -> str:
    """Limpia etiquetas HTML, entidades especiales y normaliza espacios."""
    if not raw_html:
        return ""
    text = re.sub(r'<[^>]+>', ' ', raw_html)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


_VISUAL_TOPIC_SPORTS_RE = re.compile(
    r"\b("
    r"f[uú]tbol|futbolista|balompi[eé]|mundial|champions|liga\s+(?:mx|de\s+campeones)|"
    r"partido|equipo|jugador|entrenador|t[eé]cnico\s+de|fichaje|traspaso|"
    r"gol|goles|marcador|torneo|campeonato|final(?:es)?\s+de|"
    r"baloncesto|basketball|nba|tenis|b[eé]isbol|beisbol|nfl|f[oó]rmula\s*1|"
    r"boxeo|ufc|mma|olimpiadas|juegos\s+ol[ií]mpicos|"
    r"real\s+madrid|barcelona|bellingham|messi|ronaldo|"
    r"soccer|football|world\s+cup|match|game|team|player|coach|transfer|goal|"
    r"tournament|champions\s+league|tennis|baseball|f1|formula\s*1|ufc|boxing"
    r")\b",
    re.IGNORECASE,
)
_VISUAL_TOPIC_DISASTER_RE = re.compile(
    r"\b("
    r"terremoto|sismo|huracán|hurac[aá]n|tornado|inundaci[oó]n|incendio\s+forestal|"
    r"accidente|choque|colisi[oó]n|explosi[oó]n|derrumbe|"
    r"desastre|catástrofe|catastrofe|erupci[oó]n|tsunami|"
    r"guerra|ataque|atentado|conflicto\s+armado|bombardeo|"
    r"[uú]ltima\s+hora|noticia\s+de\s+impacto|evento\s+en\s+vivo|"
    r"earthquake|hurricane|tornado|flood|wildfire|crash|explosion|disaster|"
    r"catastrophe|tsunami|war|attack|breaking\s+news|live\s+event"
    r")\b",
    re.IGNORECASE,
)
_VISUAL_TOPIC_BIOGRAPHY_RE = re.compile(
    r"\b("
    r"qui[eé]n\s+es|qui[eé]n\s+fue|biograf[ií]a\s+de|"
    r"de\s+qu[eé]\s+muri[oó]|falleci[oó]|fallecimiento|muerte\s+de|"
    r"cu[aá]ntos\s+a[ñn]os\s+ten[ií]a|c[oó]mo\s+muri[oó]|"
    r"presidente|actor|actriz|cantante|artista|celebridad|famoso|famosa|"
    r"who\s+is|who\s+was|biography\s+of|died|passed\s+away|cause\s+of\s+death|"
    r"how\s+old\s+was|president|actor|actress|singer|celebrity|famous"
    r")\b",
    re.IGNORECASE,
)

def should_show_visual_search_cards(query: str) -> bool:
    """
    BLINDAJE (2026-09-07, auditoría de búsqueda web — pedido explícito
    del usuario: "siempre que se active la búsqueda web, siempre agarre
    las tres imágenes relacionadas al tema"): esta función era un
    allowlist por TEMA (solo deportes, catástrofes/noticias de impacto o
    biografías) que omitía la tarjeta visual para cualquier otro tema
    (física, matemáticas, código, filosofía...), incluso cuando
    `search_topic_images()` (ver `_fetch_rich_web_search_impl`) sí trae
    fotos reales y relacionadas para ese tema. Se retiene la función (y
    su firma) para no romper a quien la importe, pero ahora siempre
    autoriza la tarjeta — la única señal real de si aportar algo es si
    HAY imágenes reales, que ya se garantiza aparte (búsqueda de
    imágenes dedicada, no scraping incidental) y que
    `_on_web_results_ready` ya no usa como motivo para omitir toda la
    tarjeta (ver su BLINDAJE): sin imagen real para una fuente puntual,
    el propio widget cae a su respaldo de iniciales, nunca a nada roto.
    """
    return True


_EN_STOPWORDS = frozenset({
    "the", "is", "are", "was", "were", "what", "who", "how", "where",
    "when", "why", "and", "of", "in", "on", "for", "with", "that",
    "this", "did", "does", "will", "would", "can", "could", "last",
})
_ES_STOPWORDS = frozenset({
    "el", "la", "los", "las", "es", "son", "era", "eran", "qué", "que",
    "quién", "quien", "cómo", "como", "dónde", "donde", "cuándo",
    "cuando", "por", "para", "con", "del", "y", "en", "última",
    "último", "fue", "fueron",
})

# Los dos blindajes que antes vivían aquí como copias locales -
# extracción de años explícitos (tarjetas de Wikipedia del año
# equivocado, "2002 FIFA World Cup" al preguntar por 2026) y detección de
# título retrospectivo ("Historia del FC Barcelona 2000-2010", que pasa
# text_is_relevant sin problema y no siempre trae un año en desacuerdo) -
# ahora son `extract_years()` e `is_retrospective_title()` de
# relevance.py, compartidos con web_search.py y orchestrator.py.


_REAL_IMAGE_CDN_HOSTS = ("upload.wikimedia.org",)


def _is_low_quality_thumbnail(url: Optional[str]) -> bool:
    """
    True si `url` no sirve como miniatura "real" para las tarjetas
    visuales de resultados web: vacía, una URL de ARTÍCULO de
    Wikipedia/Wikimedia (que no es una imagen), o el favicon de respaldo
    de web_search._favicon_url() (icons.duckduckgo.com, intencionalmente
    diminuto — _ThumbnailLoader ya lo rechaza por calidad).

    IMPORTANTE — `upload.wikimedia.org` NO es de baja calidad: es el CDN
    de fotos reales de Wikimedia, y desde que `_search_via_wikipedia()`
    pide `pageimages` esas fuentes SÍ traen una foto legítima de varios
    cientos de píxeles. Antes esta función rechazaba cualquier URL que
    contuviera "wikimedia.org", foto real incluida, así que en cualquier
    búsqueda dominada por Wikipedia NINGUNA fuente pasaba el gate y la
    tarjeta visual se omitía siempre ("ninguna fuente trajo una
    miniatura real") — el síntoma reportado.

    Compartida entre la reasignación de foto dentro de
    _fetch_rich_web_search_impl y el gate de WebSearchResultsWidget en
    _on_web_results_ready.
    """
    if not url:
        return True
    lowered = str(url).lower()
    if any(host in lowered for host in _REAL_IMAGE_CDN_HOSTS):
        return False
    return "wikimedia.org" in lowered or "wikipedia.org" in lowered or "icons.duckduckgo.com" in lowered


def _detect_query_language(text: str) -> str:
    """Heurística ligera ES/EN por palabras funcionales — ver nota arriba."""
    words = set(re.findall(r"[a-záéíóúñ]+", (text or "").lower()))
    en_hits = len(words & _EN_STOPWORDS)
    es_hits = len(words & _ES_STOPWORDS)
    return "en" if en_hits > es_hits else "es"


def _detect_query_language_confident(text: str) -> Optional[str]:
    """
    Como `_detect_query_language`, pero devuelve `None` en vez de forzar
    un default a español cuando la señal es ambigua (empate 0-0, o
    cualquier empate real entre hits EN/ES) — así el llamador solo la
    usa para PISAR el idioma de sesión cuando el texto de la consulta
    misma da una pista inequívoca, no cuando no hay pista alguna.

    Bug real que motivó esto: con la UI en Español, una consulta
    tecleada en inglés ("...the last match in world cup 2026") seguía
    resolviéndose a "es" porque el idioma de sesión (explícito, no
    heurístico) tenía prioridad total — la Wikipedia de respaldo
    terminaba consultando es.wikipedia.org y devolviendo artículos
    genéricos sin relación con la consulta real.
    """
    words = set(re.findall(r"[a-záéíóúñ]+", (text or "").lower()))
    en_hits = len(words & _EN_STOPWORDS)
    es_hits = len(words & _ES_STOPWORDS)
    if en_hits == es_hits:
        return None
    return "en" if en_hits > es_hits else "es"


_WEB_SEARCH_LOG_TRANSLATIONS: List[Tuple[Pattern[str], str]] = [
    (re.compile(r"^\[WEB_SEARCH\] Scraping HTML de DuckDuckGo también falló: (.+)$"),
     r"[WEB_SEARCH] DuckDuckGo HTML scraping also failed: \1"),
    (re.compile(r"^\[WEB_SEARCH\] Scraping HTML directo: (.+) resultado\(s\) recuperado\(s\)\.$"),
     r"[WEB_SEARCH] Direct HTML scraping: \1 result(s) recovered."),
    (re.compile(r"^\[WEB_SEARCH\] DuckDuckGo \(librería\) sin resultados — probando scraping HTML directo\.\.\.$"),
     "[WEB_SEARCH] DuckDuckGo (library) returned nothing — trying direct HTML scraping..."),
    (re.compile(r"^\[WEB_SEARCH\] DuckDuckGo: resultado servido desde caché\.$"),
     "[WEB_SEARCH] DuckDuckGo: result served from cache."),
    (re.compile(r"^\[WEB_SEARCH\] Consultando DuckDuckGo \(noticias\)\.\.\.$"),
     "[WEB_SEARCH] Querying DuckDuckGo (news)..."),
    (re.compile(r"^\[WEB_SEARCH\] DuckDuckGo \(noticias\) omitido — falló repetidamente esta sesión, en cooldown\.$"),
     "[WEB_SEARCH] DuckDuckGo (news) skipped — failed repeatedly this session, in cooldown."),
    (re.compile(r"^\[WEB_SEARCH\] DuckDuckGo \(noticias\) falló: (.+)$"),
     r"[WEB_SEARCH] DuckDuckGo (news) failed: \1"),
    (re.compile(r"^\[WEB_SEARCH\] Consultando DuckDuckGo \(resultados web\)\.\.\.$"),
     "[WEB_SEARCH] Querying DuckDuckGo (web results)..."),
    (re.compile(r"^\[WEB_SEARCH\] DuckDuckGo \(web\) falló: (.+)$"),
     r"[WEB_SEARCH] DuckDuckGo (web) failed: \1"),
    (re.compile(r"^\[WEB_SEARCH\] Sin resultados, reintentando con la consulta original\.\.\.$"),
     "[WEB_SEARCH] No results, retrying with the original query..."),
    (re.compile(r"^\[WEB_SEARCH\] DuckDuckGo \(fallback\) falló: (.+)$"),
     r"[WEB_SEARCH] DuckDuckGo (fallback) failed: \1"),
    (re.compile(r"^\[WEB_SEARCH\] Consultando SearXNG\.\.\.$"),
     "[WEB_SEARCH] Querying SearXNG..."),
    (re.compile(r"^\[WEB_SEARCH\] Wikipedia \(rankeo\): resultado servido desde caché\.$"),
     "[WEB_SEARCH] Wikipedia (ranking): result served from cache."),
    (re.compile(r"^\[WEB_SEARCH\] Wikipedia \(pageid=(.+?)\): extracto servido desde caché\.$"),
     r"[WEB_SEARCH] Wikipedia (pageid=\1): extract served from cache."),
    (re.compile(r"^\[WEB_SEARCH\] Wikipedia sin candidatos — reintentando con sugerencia ortográfica: (.+)$"),
     r"[WEB_SEARCH] Wikipedia had no candidates — retrying with spelling suggestion: \1"),
    (re.compile(
        r"^\[WEB_SEARCH\] Wikipedia sin candidatos — sugerencia ortográfica (.+?) cambia un "
        r"nombre propio del original \((.+?)\), se descarta por seguridad\.$"
    ), r"[WEB_SEARCH] Wikipedia had no candidates — spelling suggestion \1 changes a proper "
       r"noun from the original (\2), discarded for safety."),
    (re.compile(r"^\[WEB_SEARCH\] Consultando Wikipedia \(motor paralelo\)\.\.\.$"),
     "[WEB_SEARCH] Querying Wikipedia (parallel engine)..."),
    (re.compile(r"^\[WEB_SEARCH\] Wikipedia también falló: (.+)$"),
     r"[WEB_SEARCH] Wikipedia also failed: \1"),
    (re.compile(r"^\[WEB_SEARCH\] Wikipedia: sin candidatos para esta consulta\.$"),
     "[WEB_SEARCH] Wikipedia: no candidates for this query."),
    (re.compile(r"^\[WEB_SEARCH\] Wikipedia: (.+) resultado\(s\) recuperado\(s\)\.$"),
     r"[WEB_SEARCH] Wikipedia: \1 result(s) recovered."),
    (re.compile(r"^\[WEB_SEARCH\] (.+): (.+) resultado\(s\) recibido\(s\)\.$"),
     r"[WEB_SEARCH] \1: \2 result(s) received."),
    (re.compile(r"^\[WEB_SEARCH\] Extrayendo HTML de (.+)\.\.\.$"),
     r"[WEB_SEARCH] Extracting HTML from \1..."),
    (re.compile(r"^\[WEB_SEARCH\] (.+) fuente\(s\) de Wikipedia omitida\(s\) del scraping \(extracto ya provisto por la API\)\.$"),
     r"[WEB_SEARCH] \1 Wikipedia source(s) skipped from scraping (extract already provided by the API)."),
    (re.compile(r"^\[WEB_SEARCH\] Consultando motor de búsqueda \((.+)\)\.\.\.$"),
     r"[WEB_SEARCH] Querying search engine (\1)..."),
    (re.compile(r"^\[WEB_SEARCH\] Error inesperado en la búsqueda: (.+)$"),
     r"[WEB_SEARCH] Unexpected search error: \1"),
    (re.compile(r"^\[WEB_SEARCH\] Sin resultados de ningún motor\.$"),
     "[WEB_SEARCH] No results from any engine."),
    (re.compile(r"^\[WEB_SEARCH\] Filtro de año descartó (.+) fuente\(s\) — consulta pide (.+?): (.+)$"),
     r"[WEB_SEARCH] Year filter discarded \1 source(s) — query asks for \2: \3"),
    (re.compile(r"^\[WEB_SEARCH\] Ninguna fuente comparte términos con la consulta — se descartan las (.+): (.+)$"),
     r"[WEB_SEARCH] No source shares terms with the query — discarding all \1: \2"),
    (re.compile(r"^\[WEB_SEARCH\] Ningún resultado pasó el filtro de relevancia\.$"),
     "[WEB_SEARCH] No result passed the relevance filter."),
    (re.compile(r"^\[WEB_SEARCH\] Consulta de dato puntual: se prioriza fuente estructurada \((.+)\)\.$"),
     r"[WEB_SEARCH] Precise-fact query: prioritizing structured source (\1)."),
    (re.compile(r"^\[WEB_SEARCH\] (.+) fuente\(s\) seleccionada\(s\), extrayendo contenido completo\.\.\.$"),
     r"[WEB_SEARCH] \1 source(s) selected, extracting full content..."),
    (re.compile(r"^\[WEB_SEARCH\] Búsqueda completa: (.+) fuente\(s\) lista\(s\) para el modelo\.$"),
     r"[WEB_SEARCH] Search complete: \1 source(s) ready for the model."),
    (re.compile(r"^\[WEB_SEARCH\] Formateando contexto para el modelo\.\.\.$"),
     "[WEB_SEARCH] Formatting context for the model..."),
    (re.compile(r"^\[WEB_SEARCH\] Timeout global de búsqueda \((.+?)s\) alcanzado tras (.+?)s\.$"),
     r"[WEB_SEARCH] Global search timeout (\1s) reached after \2s."),
    (re.compile(r"^\[WEB_SEARCH\] (.+?) falló \(intento (\d+)/(\d+): (.+?)\) — reintentando en (.+?)s\.\.\.$"),
     r"[WEB_SEARCH] \1 failed (attempt \2/\3: \4) — retrying in \5s..."),
]


def _translate_web_search_log(message: str) -> str:
    for pattern, replacement in _WEB_SEARCH_LOG_TRANSLATIONS:
        if pattern.match(message):
            return pattern.sub(replacement, message, count=1)
    return message


def _emit_log_safe(log_cb: Optional[Any], message: str) -> None:
    """Envía `message` a `log_cb` (típicamente StreamTurnWorker.log_message.emit) sin romper la búsqueda si falla."""
    if log_cb is None:
        return
    try:
        log_cb(message)
    except Exception:
        pass


def _fetch_rich_web_search_impl(
    query: str, lang: Optional[str] = None, log_cb: Optional[Any] = None
) -> dict:
    """Implementación real de la búsqueda híbrida con filtrado de fuentes obsoletas."""
    clean_q = sanitize_query(query)
    results: dict = {
        "query": clean_q or (query or "").strip(),
        "snippets": [],
        "images": [],
        "sources": [],
        "success": False,
        "status_message": "",
    }

    if not clean_q:
        results["status_message"] = "No se pudo extraer una consulta válida del mensaje."
        return results

    resolved_lang = lang if lang in ("en", "es") else _detect_query_language(clean_q)

    # Blindaje adicional: si el texto de la CONSULTA misma da una señal
    # clara e inequívoca de idioma que difiere de `resolved_lang` (venga
    # este del idioma de sesión o del heurístico anterior con su default
    # a español), se prioriza el de la consulta - ver docstring de
    # `_detect_query_language_confident`. Consultas ambiguas/cortas sin
    # señal propia siguen respetando el idioma de sesión sin cambios.
    query_lang_signal = _detect_query_language_confident(clean_q)
    if query_lang_signal is not None and query_lang_signal != resolved_lang:
        resolved_lang = query_lang_signal

    query_is_live = is_live_event_query(clean_q)

    query_needs_fact = requires_precise_fact(clean_q)
    query_strict = needs_strict_relevance(clean_q)

    INVALID_TITLE_PATTERNS = re.compile(
        r"^(categor[ií]a:|anexo:|lista de|category:|disambiguation|desambiguaci[oó]n)",
        re.IGNORECASE,
    )

    capa1_count = 0
    try:
        structured_results = search_web(clean_q, max_results=5, lang=resolved_lang, log_cb=log_cb)
        for item in structured_results:
            try:
                title = re.sub(r"\s*\([^)]*\)\s*$", "", str(item.get("title", ""))).strip()
                url = str(item.get("url", "")).strip()
                snippet = re.sub(
                    r"\s+", " ", str(item.get("content") or item.get("snippet") or "")
                ).strip()

                if INVALID_TITLE_PATTERNS.search(title) or "categoría:" in url.lower():
                    continue

                if not snippet or len(snippet) < 10:
                    continue

                domain = str(item.get("domain") or "")
                if not domain:
                    with contextlib.suppress(Exception):
                        domain = urllib.parse.urlparse(url).netloc.replace("www.", "")

                if snippet not in results["snippets"]:
                    results["snippets"].append(snippet)
                    results["sources"].append({
                        "title": title or (domain or "Fuente web"),
                        "domain": domain or "web",
                        "url": url,
                        "snippet": snippet,
                        "thumbnail": item.get("image") or None,
                    })
                    capa1_count += 1
            except Exception as item_exc:
                print(f"[WebSearch Structured Item Error]: {item_exc}")
                continue
    except Exception as exc:
        print(f"[WebSearch Structured Error]: {exc}")

    wiki_authoritative_text = ""
    if len(results["snippets"]) < 3 or query_needs_fact:
        # patch_qt65 (2026-09-18, langfix): esta linea (y las dos de
        # reintento de Wikipedia mas abajo) se armaban siempre en
        # espanol -- a diferencia de `search_web()`/`search_topic_images()`
        # (import de src/tools/web_search.py) que SI reciben `lang=` y
        # arman el log ya en el idioma correcto. `resolved_lang` ya esta
        # en scope aca mismo (se usa 2 lineas mas abajo para elegir
        # wiki_domain), asi que se reusa el mismo criterio.
        if resolved_lang == "en":
            _emit_log_safe(
                log_cb,
                "[WEB_SEARCH] Querying Wikipedia ("
                + ("precise fact" if query_needs_fact else "fallback")
                + ")..."
            )
        else:
            _emit_log_safe(
                log_cb,
                "[WEB_SEARCH] Consultando Wikipedia ("
                + ("dato puntual" if query_needs_fact else "respaldo")
                + ")..."
            )

        wiki_domain = "en.wikipedia.org" if resolved_lang == "en" else "es.wikipedia.org"

        wiki_exintro = "" if query_needs_fact else "&exintro"
        if query_needs_fact and asks_about_final(clean_q):
            wiki_exchars = WIKI_FINAL_EXTRACT_CHARS
        elif query_needs_fact:
            wiki_exchars = WIKI_PRECISE_FACT_EXTRACT_CHARS
        else:
            wiki_exchars = 280

        def _run_wikipedia_pass(wiki_query_text: str) -> int:
            added = 0
            sequence_start = time.time()

            def _budget_left() -> float:
                return WIKI_SEQUENCE_BUDGET_SECONDS - (time.time() - sequence_start)

            try:
                encoded_q = urllib.parse.quote(wiki_query_text)
                candidates = wiki_rank_search_candidates(
                    wiki_domain, encoded_q, 5, timeout=3.5,
                    log_cb=log_cb, budget_left_fn=_budget_left,
                )
                if query_needs_fact and asks_about_final(clean_q):
                    candidates = sorted(
                        candidates,
                        key=lambda c: title_names_the_final(c.get("title", "")),
                        reverse=True,
                    )
                for candidate in candidates:
                    if len(results["snippets"]) >= 5:
                        break
                    if _budget_left() <= 0.3:
                        break

                    pageid = candidate.get("pageid")
                    try:
                        page = wiki_fetch_single_extract(
                            wiki_domain, pageid, wiki_exintro, wiki_exchars,
                            WIKI_API_THUMB_SIZE, timeout=3.5,
                            log_cb=log_cb, budget_left_fn=_budget_left,
                        )
                    except Exception as item_exc:
                        print(f"[WebSearch Wiki Item Error]: {item_exc}")
                        _emit_log_safe(log_cb, f"[WEB_SEARCH] Wikipedia (Capa 2): error de extracto: {item_exc}")
                        continue
                    if page is None:
                        continue

                    try:
                        title = page["title"]

                        if INVALID_TITLE_PATTERNS.search(title):
                            continue

                        if query_strict and is_retrospective_title(title):
                            continue

                        extract = clean_html_text(page["extract"])

                        if (
                            query_strict
                            and not text_is_relevant(clean_q, f"{title} {extract}")
                        ):
                            continue

                        query_years = extract_years(clean_q)
                        if query_years:
                            article_years = extract_years(f"{title} {extract}")
                            if article_years and not (query_years & article_years):
                                continue

                        if extract and len(extract) > 30 and len(results["snippets"]) < 5:
                            display_extract = (
                                _extract_relevant_sentences(extract, clean_q, WIKI_PRECISE_FACT_EXTRACT_CHARS)
                                if query_needs_fact and len(extract) > WIKI_PRECISE_FACT_EXTRACT_CHARS
                                else extract
                            )
                            formatted = f"[{title}]: {display_extract}"
                            if formatted not in results["snippets"]:
                                results["snippets"].append(formatted)
                                results["sources"].append({
                                    "title": title,
                                    "domain": wiki_domain,
                                    "url": f"https://{wiki_domain}/wiki/{urllib.parse.quote(title)}",
                                    "snippet": display_extract,
                                    "thumbnail": None,
                                })
                                added += 1
                    except Exception as item_exc:
                        print(f"[WebSearch Wiki Item Error]: {item_exc}")
                        _emit_log_safe(log_cb, f"[WEB_SEARCH] Wikipedia (Capa 2): candidato descartado por error: {item_exc}")
                        continue
            except Exception as exc:
                print(f"[WebSearch Wiki Error]: {exc}")
                _emit_log_safe(log_cb, f"[WEB_SEARCH] Wikipedia (Capa 2) falló: {exc}")
            return added

        wiki_added = _run_wikipedia_pass(clean_q)

        wants_final_source = query_needs_fact and asks_about_final(clean_q)
        already_has_final_source = wants_final_source and any(
            title_names_the_final(s.get("title", "")) for s in results["sources"]
        )
        if query_needs_fact and not already_has_final_source and (wiki_added == 0 or wants_final_source):
            # patch_qt65 (2026-09-18, langfix): mismo caso que el mensaje
            # "Consultando Wikipedia (...)" de mas arriba -- nunca tuvo
            # rama de idioma. Reusa `resolved_lang`, ya en scope.
            if resolved_lang == "en":
                _emit_log_safe(
                    log_cb,
                    "[WEB_SEARCH] Wikipedia has no source specific to the final, retrying with "
                    "a more specific query..."
                    if wants_final_source else
                    "[WEB_SEARCH] Wikipedia had no useful result, retrying with a more specific query...",
                )
            else:
                _emit_log_safe(
                    log_cb,
                    "[WEB_SEARCH] Wikipedia sin fuente específica de la final, reintentando con "
                    "consulta más específica..."
                    if wants_final_source else
                    "[WEB_SEARCH] Wikipedia sin resultado útil, reintentando con consulta más específica...",
                )
            _run_wikipedia_pass(f"{clean_q} final")

    if query_needs_fact:
        wiki_authoritative_text = " ".join(
            f"{s.get('title', '')} {s.get('snippet', '')}"
            for s in results["sources"]
            if "wikipedia.org" in str(s.get("domain", ""))
        )
        if distinctive_words(wiki_authoritative_text):
            kept_sources, kept_snippets = [], []
            for source, snippet in zip(results["sources"], results["snippets"]):
                is_wiki_source = "wikipedia.org" in str(source.get("domain", ""))
                if not is_wiki_source and source_names_unconfirmed_participant(
                    source.get("title", ""), wiki_authoritative_text
                ):
                    _emit_log_safe(
                        log_cb,
                        "[WEB_SEARCH] Fuente descartada — nombra un participante "
                        f"que Wikipedia no confirma: {source.get('title', '')!r}",
                    )
                    continue
                kept_sources.append(source)
                kept_snippets.append(snippet)
            results["sources"] = kept_sources
            results["snippets"] = kept_snippets
        else:
            _emit_log_safe(log_cb, "[WEB_SEARCH] Pocos resultados, consultando Wikipedia (respaldo)...")
        wiki_domain = "en.wikipedia.org" if resolved_lang == "en" else "es.wikipedia.org"

        wiki_exintro = "" if query_needs_fact else "&exintro"
        if query_needs_fact and asks_about_final(clean_q):
            wiki_exchars = WIKI_FINAL_EXTRACT_CHARS
        elif query_needs_fact:
            wiki_exchars = WIKI_PRECISE_FACT_EXTRACT_CHARS
        else:
            wiki_exchars = 280

        def _run_wikipedia_pass(wiki_query_text: str) -> int:
            """
            Consulta Wikipedia con `wiki_query_text` y filtra los
            candidatos hacia `results` con EXACTAMENTE el mismo criterio
            de siempre (título inválido, retrospectivo, relevancia,
            desajuste de año — todos evaluados contra `clean_q`, la
            consulta ORIGINAL del usuario, nunca contra `wiki_query_text`
            — un query refinado no debe autoconfirmar su propia
            relevancia). Devuelve cuántas fuentes agregó esta pasada, para
            que el llamador decida si vale la pena reintentar con un
            query más específico.

            Factorizado como función en vez de duplicar el bucle de
            filtrado por cada intento (ver `_run_wikipedia_pass` más
            abajo, punto 2b): dos copias del mismo filtrado de ~35 líneas
            solo pueden divergir con el tiempo — un arreglo futuro en una
            copia y no en la otra es el bug que esto evita de raíz.

            BLINDAJE (bug real, MEDIDO contra la API real de Wikipedia):
            esta función tenía su PROPIA petición combinada
            `generator=search` + `prop=extracts`, una copia independiente
            del mismo patrón que el motor paralelo de web_search.py
            (`_search_via_wikipedia`) usaba antes de arreglarse — y ese
            arreglo se aplicó SOLO ahí, dejando esta copia con el mismo
            bug: `prop=extracts` combinado con `generator=search` solo
            devuelve el campo "extract" para un subconjunto ARBITRARIO de
            las páginas, no necesariamente la más relevante (verificado:
            el artículo correcto salía rankeado #1 pero sin extracto,
            mientras el genérico salía #5 con extracto completo). Ahora
            reusa `wiki_rank_search_candidates`/`wiki_fetch_single_extract`
            (web_search.py) — las MISMAS funciones que ya arreglan esto
            para el motor paralelo — en vez de mantener una segunda copia
            que puede volver a divergir.
            """
            added = 0
            sequence_start = time.time()

            def _budget_left() -> float:
                return WIKI_SEQUENCE_BUDGET_SECONDS - (time.time() - sequence_start)

            try:
                encoded_q = urllib.parse.quote(wiki_query_text)
                candidates = wiki_rank_search_candidates(
                    wiki_domain, encoded_q, 5, timeout=3.5,
                    log_cb=log_cb, budget_left_fn=_budget_left,
                )
                if query_needs_fact and asks_about_final(clean_q):
                    candidates = sorted(
                        candidates,
                        key=lambda c: title_names_the_final(c.get("title", "")),
                        reverse=True,
                    )
                for candidate in candidates:
                    if len(results["snippets"]) >= 5:
                        break
                    if _budget_left() <= 0.3:
                        break

                    pageid = candidate.get("pageid")
                    try:
                        page = wiki_fetch_single_extract(
                            wiki_domain, pageid, wiki_exintro, wiki_exchars,
                            WIKI_API_THUMB_SIZE, timeout=3.5,
                            log_cb=log_cb, budget_left_fn=_budget_left,
                        )
                    except Exception as item_exc:
                        print(f"[WebSearch Wiki Item Error]: {item_exc}")
                        _emit_log_safe(log_cb, f"[WEB_SEARCH] Wikipedia (Capa 2): error de extracto: {item_exc}")
                        continue
                    if page is None:
                        continue

                    try:
                        title = page["title"]

                        if INVALID_TITLE_PATTERNS.search(title):
                            continue

                        if query_strict and is_retrospective_title(title):
                            continue

                        extract = clean_html_text(page["extract"])

                        if (
                            query_strict
                            and not text_is_relevant(clean_q, f"{title} {extract}")
                        ):
                            continue

                        query_years = extract_years(clean_q)
                        if query_years:
                            article_years = extract_years(f"{title} {extract}")
                            if article_years and not (query_years & article_years):
                                continue

                        if extract and len(extract) > 30 and len(results["snippets"]) < 5:
                            display_extract = (
                                _extract_relevant_sentences(extract, clean_q, WIKI_PRECISE_FACT_EXTRACT_CHARS)
                                if query_needs_fact and len(extract) > WIKI_PRECISE_FACT_EXTRACT_CHARS
                                else extract
                            )
                            formatted = f"[{title}]: {display_extract}"
                            if formatted not in results["snippets"]:
                                results["snippets"].append(formatted)
                                results["sources"].append({
                                    "title": title,
                                    "domain": wiki_domain,
                                    "url": f"https://{wiki_domain}/wiki/{urllib.parse.quote(title)}",
                                    "snippet": display_extract,
                                    "thumbnail": None,
                                })
                                added += 1
                    except Exception as item_exc:
                        print(f"[WebSearch Wiki Item Error]: {item_exc}")
                        _emit_log_safe(log_cb, f"[WEB_SEARCH] Wikipedia (Capa 2): candidato descartado por error: {item_exc}")
                        continue
            except Exception as exc:
                print(f"[WebSearch Wiki Error]: {exc}")
                _emit_log_safe(log_cb, f"[WEB_SEARCH] Wikipedia (Capa 2) falló: {exc}")
            return added

        wiki_added = _run_wikipedia_pass(clean_q)

        wants_final_source = query_needs_fact and asks_about_final(clean_q)
        already_has_final_source = wants_final_source and any(
            title_names_the_final(s.get("title", "")) for s in results["sources"]
        )
        if query_needs_fact and not already_has_final_source and (wiki_added == 0 or wants_final_source):
            # patch_qt65 (2026-09-18, langfix): mismo caso que el mensaje
            # "Consultando Wikipedia (...)" de mas arriba -- nunca tuvo
            # rama de idioma. Reusa `resolved_lang`, ya en scope.
            if resolved_lang == "en":
                _emit_log_safe(
                    log_cb,
                    "[WEB_SEARCH] Wikipedia has no source specific to the final, retrying with "
                    "a more specific query..."
                    if wants_final_source else
                    "[WEB_SEARCH] Wikipedia had no useful result, retrying with a more specific query...",
                )
            else:
                _emit_log_safe(
                    log_cb,
                    "[WEB_SEARCH] Wikipedia sin fuente específica de la final, reintentando con "
                    "consulta más específica..."
                    if wants_final_source else
                    "[WEB_SEARCH] Wikipedia sin resultado útil, reintentando con consulta más específica...",
                )
            _run_wikipedia_pass(f"{clean_q} final")

    if query_needs_fact:
        wiki_authoritative_text = " ".join(
            f"{s.get('title', '')} {s.get('snippet', '')}"
            for s in results["sources"]
            if "wikipedia.org" in str(s.get("domain", ""))
        )
        if distinctive_words(wiki_authoritative_text):
            kept_sources, kept_snippets = [], []
            for source, snippet in zip(results["sources"], results["snippets"]):
                is_wiki_source = "wikipedia.org" in str(source.get("domain", ""))
                if not is_wiki_source and source_names_unconfirmed_participant(
                    source.get("title", ""), wiki_authoritative_text
                ):
                    _emit_log_safe(
                        log_cb,
                        "[WEB_SEARCH] Fuente descartada — nombra un participante "
                        f"que Wikipedia no confirma: {source.get('title', '')!r}",
                    )
                    continue
                kept_sources.append(source)
                kept_snippets.append(snippet)
            results["sources"] = kept_sources
            results["snippets"] = kept_snippets

    results["wiki_only_backup"] = bool(
        query_is_live and capa1_count == 0 and len(results["sources"]) > capa1_count
    )

    best_web_image = None
    for src in results["sources"]:
        thumb = src.get("thumbnail")
        if not _is_low_quality_thumbnail(thumb):
            best_web_image = thumb
            break

    if best_web_image:
        for src in results["sources"]:
            if _is_low_quality_thumbnail(src.get("thumbnail")):
                src["thumbnail"] = best_web_image

    # BLINDAJE (2026-09-07, auditoría de búsqueda web — pedido explícito
    # del usuario: "siempre que se active la búsqueda web, siempre agarre
    # las tres imágenes relacionadas al tema"): la reasignación de arriba
    # solo FUNCIONA si el scraping incidental de algún artículo de texto
    # trajo una foto real -- en la práctica, la mayoría de las consultas
    # (Wikipedia como única fuente, artículos sin og:image, dominios
    # bloqueados al scraping) nunca traían ninguna, así que
    # `best_web_image` quedaba en `None` y la tarjeta visual se omitía
    # SIEMPRE ("ninguna fuente trajo una miniatura real"), sin importar
    # el tema -- visto en vivo incluso en una consulta de terremoto, que
    # sí pasa el filtro de tema. `search_topic_images()` corre una
    # búsqueda de imágenes DEDICADA sobre el tema en sí (no depende de
    # qué artículo de texto haya encontrado la búsqueda normal), así que
    # siempre trae hasta 3 fotos reales relacionadas. Llena
    # `results["images"]` (ya normalizado por `_normalize_search_result`,
    # antes sin ningún productor real) y respalda a cualquier fuente que
    # siga sin miniatura real después del backfill de arriba.
    topic_images = search_topic_images(clean_q, max_results=3, lang=resolved_lang, log_cb=log_cb)
    results["images"] = topic_images
    if topic_images:
        _topic_image_iter = iter(topic_images)
        for src in results["sources"]:
            if _is_low_quality_thumbnail(src.get("thumbnail")):
                _next_image = next(_topic_image_iter, None)
                if _next_image is None:
                    break
                src["thumbnail"] = _next_image["url"]

    backend_error = get_last_search_error()

    if not results["snippets"] and not results["sources"]:
        if backend_error:
            results["status_message"] = (
                f"El proveedor de búsqueda no respondió correctamente "
                f"({backend_error}). Puede ser un bloqueo temporal — "
                f"reintenta en unos minutos."
            )
    elif backend_error:
        results["status_message"] = (
            f"Resultados parciales — el proveedor de búsqueda principal "
            f"falló ({backend_error}) y se completó con fuentes secundarias."
        )

    _emit_log_safe(log_cb, "[WEB_SEARCH] Formateando contexto para el modelo...")
    return _normalize_search_result(results)

def _normalize_search_result(results: dict) -> dict:
    """
    Normaliza el resultado sin perder la evidencia original.

    `snippet` puede alimentar la generación o la tarjeta visual.
    `raw_content` queda reservado para verificadores deterministas.
    """
    safe_snippets = [
        str(snippet)
        for snippet in (results.get("snippets") or [])
        if snippet
    ][:5]

    safe_sources: list[dict] = []

    for source in (results.get("sources") or [])[:5]:
        if not isinstance(source, dict):
            continue

        snippet = str(
            source.get("snippet")
            or source.get("content")
            or ""
        )

        raw_content = str(
            source.get("raw_content")
            or source.get("content")
            or source.get("snippet")
            or ""
        )

        safe_sources.append({
            "title": str(source.get("title") or "Fuente web"),
            "domain": str(source.get("domain") or "web"),
            "url": str(source.get("url") or ""),
            "snippet": snippet,
            "content": str(source.get("content") or snippet),
            "raw_content": raw_content,
            "content_source": str(
                source.get("content_source") or "unknown"
            ),
            "type": str(source.get("type") or ""),
            "score": float(source.get("score") or 0.0),
            "metadata": dict(source.get("metadata") or {}),
            "thumbnail": (
                source.get("thumbnail")
                if isinstance(source.get("thumbnail"), str)
                else None
            ),
        })

    safe_images = []

    for image in (results.get("images") or [])[:3]:
        if not isinstance(image, dict) or not image.get("url"):
            continue

        safe_images.append({
            "title": str(image.get("title") or ""),
            "url": str(image.get("url")),
        })

    success = bool(safe_sources or safe_snippets)

    status_message = str(
        results.get("status_message") or ""
    ).strip() or (
        f"{len(safe_sources)} fuente(s) recuperada(s) correctamente."
        if success
        else "Sin resultados web en tiempo real."
    )

    return {
        "query": str(results.get("query") or ""),
        "snippets": safe_snippets,
        "sources": safe_sources,
        "images": safe_images,
        "success": success,
        "status_message": status_message,
        "wiki_only_backup": bool(
            results.get("wiki_only_backup")
        ),
    }


def fetch_rich_web_search(
    query: str, lang: Optional[str] = None, log_cb: Optional[Any] = None
) -> dict:
    """
    Motor de búsqueda híbrido con blindaje total: nunca propaga excepciones
    hacia el hilo de trabajo. Cualquier fallo de red, SSL, timeout o
    rate-limit se traduce en un resultado degradado con `success=False`
    y un `status_message` explicativo, listo para mostrarse en la UI.
    `lang` ("en"/"es"), cuando se conoce, evita depender de la heurística
    de detección interna — ver StreamTurnWorker, que sí sabe el idioma
    activo de la conversación. `log_cb`, cuando se pasa, recibe una traza
    de texto por cada fase de la búsqueda (típicamente
    `StreamTurnWorker.log_message.emit`, una señal Qt segura de invocar
    desde este hilo de fondo).
    """
    try:
        return _fetch_rich_web_search_impl(query, lang=lang, log_cb=log_cb)
    except Exception as exc:
        return {
            "query": (query or "").strip(),
            "snippets": [],
            "images": [],
            "sources": [],
            "success": False,
            "status_message": f"Fallo crítico en el motor de búsqueda: {exc}",
        }


def search_web_context(query: str) -> str:
    """Helper local para formatear el bloque contextual de alta prioridad para el orquestador."""
    rich_data = fetch_rich_web_search(query)
    snippets = rich_data.get("snippets", [])
    if snippets:
        return (
            f"[CONTEXTO WEB EN TIEMPO REAL PARA '{rich_data['query']}' — PRIORIDAD ABSOLUTA]:\n"
            + "\n".join(f"- {s}" for s in snippets)
        )
    return ""


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_resource_path(relative_path: str) -> str:
    """
    Obtiene la ruta absoluta para recursos empaquetados por PyInstaller.

    BLINDAJE (bug real reportado — el ícono "no se detecta"): la rama sin
    empaquetar usaba `os.path.abspath(".")`, es decir, el directorio de
    trabajo ACTUAL del proceso al lanzar la app — no la ubicación real de
    logo.ico (que vive en la raíz del proyecto, no en src/ui/). Eso
    funciona por casualidad SOLO si el proceso se lanza con la raíz del
    proyecto como cwd; lanzarlo desde un IDE ("ejecutar archivo actual",
    que suele fijar el cwd en la carpeta del propio archivo, src/ui/) o
    desde un acceso directo con otro "Iniciar en" rompe la búsqueda en
    silencio. Ahora la ruta no-empaquetada es relativa a la ubicación de
    ESTE archivo (_PROJECT_ROOT), así que encuentra logo.ico sin importar
    desde dónde se lance el proceso.
    """
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(_PROJECT_ROOT, relative_path)


_APP_FONT_FILES = (
    "Inter-Regular.otf",
    "Inter-Medium.otf",
    "Inter-SemiBold.otf",
    "Inter-Bold.otf",
)


def load_app_fonts() -> str:
    """
    Registra los 4 pesos de Inter empaquetados en assets/fonts/ vía
    QFontDatabase y devuelve el nombre de familia real que Qt les asignó.

    BLINDAJE: si algún .otf falta o QFontDatabase no logra parsear
    NINGUNO (empaquetado incompleto, ruta rota, etc.), esto degrada en
    silencio a devolver la familia por defecto del sistema
    (QApplication.font().family()) - la UI sigue funcionando exactamente
    como antes de este cambio, solo sin la tipografía propia. A
    diferencia de la Palanca 2 (env vars de Ollama que tumbaron el
    servidor entero - ver ollama_manager.py), una fuente no cargada
    NUNCA puede crashear ni dejar la ventana inutilizable: Qt
    sencillamente sustituye por la fuente por defecto, así que este
    fallback es el único manejo de error que hace falta.
    """
    resolved_family: Optional[str] = None
    for filename in _APP_FONT_FILES:
        path = get_resource_path(os.path.join("assets", "fonts", filename))
        try:
            font_id = QFontDatabase.addApplicationFont(path)
        except Exception:
            font_id = -1
        if font_id != -1 and resolved_family is None:
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                resolved_family = families[0]
    return resolved_family or QApplication.font().family()


APP_NAME = "SovNode"
APP_TITLE = "SovNode — Sovereign AI Node"
DEV_MODE = True
SUPPORTED_DROP_EXTENSIONS = {".py", ".txt", ".md", ".json", ".csv"}
SUPPORTED_IMAGE_DROP_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
ATTACHED_IMAGE_MAX_DIMENSION = 1568
DONATION_LINKS = {
    "kofi": "https://ko-fi.com/dr0124",
    "usdt_address": "TDS6AiQs1YNtw6WRJfHC9nwqt6hLtKCTpc",
}

SLASH_COMMANDS = {
    "Español": {
        "/resumir": "Sintetiza el siguiente texto en puntos clave estructurados y concisos:\n\n",
        "/depurar": "Analiza el siguiente código Python, identifica errores de sintaxis o lógica y entrega la versión corregida:\n\n",
        "/traducir": "Traduce el siguiente fragmento al español manteniendo precisión técnica:\n\n",
        "/optimizar": "Analiza este código y propone mejoras de eficiencia, legibilidad y rendimiento:\n\n",
    },
    "English": {
        "/summarize": "Synthesize the following text into structured and concise key points:\n\n",
        "/debug": "Analyze the following Python code, identify syntax or logic errors, and provide the corrected version:\n\n",
        "/translate": "Translate the following fragment into English, maintaining technical precision:\n\n",
        "/optimize": "Analyze this code and propose improvements for efficiency, readability, and performance:\n\n",
    },
}

I18N = {
    "Español": {
        "theme_title": "TEMA VISUAL",
        "lang_title": "IDIOMA / LANGUAGE",
        "status_card": "ESTADO DEL NODO",
        "session_card": "SESIÓN ACTIVA",
        "support_card": "PROYECTO OPEN SOURCE",
        "btn_donate": "Apoyar el proyecto",
        "support_desc": "SovNode es 100% gratuito y privado. Tu apoyo impulsa el desarrollo.",
        "btn_new_chat": "Nueva conversación",
        "btn_new_chat_tooltip": "Nueva pestaña de chat",
        "tab_new_chat_title": "Nuevo chat",
        "tab_close_tooltip": "Cerrar pestaña",
        "btn_config_tooltip": "Configuración",
        "btn_export": "Exportar chat (.md)",
        "btn_export_training": "Exportar dataset de entrenamiento",
        "btn_minimize": "— Minimizar a bandeja",
        "header_title": "Consola de comandos",
        "header_subtitle": "Sesión soberana local · Enter para enviar · Shift+Enter para nueva línea",
        "placeholder": "Escribe una instrucción para SovNode...",
        "btn_send": "Enviar",
        "btn_stop": "Detener",
        "processing": "SovNode está procesando...",
        "terminal_btn_show": "Terminal",
        "terminal_btn_hide": "Ocultar Terminal",
        "status_online": "Online · Ollama local",
        "status_offline": "Offline · Ollama no disponible",
        "status_checking": "Verificando nodo local...",
        "header_online": "● Nodo local en línea",
        "header_offline": "● Nodo local sin conexión",
        "role_general": "Rol: conversación general",
        "role_coder": "Rol: generación de código",
        "turns_count": "Turnos procesados: {}",
        "welcome_msg": "Sistema iniciado. Estoy listo para recibir instrucciones.",
        "new_chat_msg": "Nueva conversación iniciada. ¿En qué puedo ayudarte?",
        "donate_title": "Apoyar SovNode",
        "donate_header": "Apoya el desarrollo de SovNode",
        "donate_desc": "SovNode es un proyecto 100% independiente y de código abierto. Si la herramienta te resulta útil, cualquier contribución ayuda a mantener el desarrollo activo de nuevas funciones.",
        "donate_btn_kofi": "Donar vía Ko-fi / PayPal",
        "donate_crypto_title": "💳 USDT (Red TRON / TRC-20):",
        "donate_btn_copy": "Copiar",
        "donate_copy_title": "Dirección Copiada",
        "donate_copy_msg": "La dirección USDT (TRC-20) se copió al portapapeles.",
        "jump_to_bottom": "⬇ Ir al final",
        "web_badge_live": "🌐 RED VIVA",
        "web_badge_local": "💾 MEMORIA LOCAL",
        "msg_sender_user": "TÚ",
        "msg_sender_error": "ERROR",
        "msg_sender_warning": "SOVNODE (auto-corregido)",
        "btn_download_model": "Descargar modelo",
        "engine_title": "MOTOR DE GENERACIÓN",
        "engine_local": "Local (Ollama)",
        "engine_cloud": "Nube (API externa)",
        # patch_qt66 (2026-09-19, pedido explícito del usuario: "se me
        # acabaron los creditos, podemos usar el modelo de gemini
        # tambien?"): antes esta sección solo sabía hablar con la API de
        # Anthropic -- `cloud_provider_*` son las etiquetas del nuevo
        # selector "Proveedor de Nube" (Claude/Gemini), ver
        # `combo_cloud_provider` en _create_ui. `cloud_key_placeholder`/
        # `cloud_key_tooltip` (Anthropic) se dejan con su texto de
        # siempre; se agregan `_gemini` como sus equivalentes para el
        # otro proveedor -- `_on_cloud_provider_changed` elige cuál
        # mostrar según el proveedor activo.
        "cloud_provider_title": "Proveedor de Nube:",
        "cloud_provider_anthropic": "Claude (Anthropic)",
        "cloud_provider_gemini": "Gemini (Google)",
        "cloud_key_placeholder": "sk-ant-...",
        "cloud_key_tooltip": "API key de console.anthropic.com — NO la contraseña de tu cuenta de Claude.ai. Se guarda solo en este equipo.",
        "cloud_key_placeholder_gemini": "AIza...",
        "cloud_key_tooltip_gemini": "API key de aistudio.google.com (Google AI Studio) — se guarda solo en este equipo, por separado de la key de Claude.",
        "btn_test_cloud_key": "Probar conexión",
        "btn_forget_cloud_key": "Olvidar key guardada",
        "btn_forget_cloud_key_tooltip": "Borra la API key guardada del proveedor activo en este equipo (Registro de Windows). No afecta el resto de la configuración ni la key del otro proveedor.",
        "log_cloud_key_forgotten": "Se borró la API key guardada de este equipo para el proveedor activo.",
        "cloud_usage_idle": "Sin uso todavía en esta sesión.",
        "cloud_usage_fmt": "{0} llamadas · {1} tok in / {2} tok out · ${3:.4f} gastados",
        "cloud_test_testing": "Probando conexión con la API de {0}...",
        "cloud_test_ok": "✅ Conexión OK — la API key funciona (modelo: {0}).",
        "cloud_test_fail": "❌ Falló la conexión: {0}",
        "cloud_test_no_key": "Cargá una API key antes de probar la conexión.",
        "log_engine_changed": "Motor de generación: {0}.",
        "log_cloud_provider_changed": "Proveedor de Nube: {0}.",
        # patch_qt67 (2026-09-19, bug real, MEDIDO por el usuario apenas
        # probó Gemini con patch_qt66 recién cargado: "gemini-2.5-flash"
        # -- el default elegido en ese patch -- ya no está disponible
        # para cuentas nuevas, la propia API de Google devolvió el error
        # sugiriendo "gemini-3.6-flash" como reemplazo). El catálogo de
        # Gemini cambia mucho más rápido que el de Anthropic, así que en
        # vez de perseguir cada baja de modelo con un patch de código se
        # agrega este campo editable: el usuario pega el ID que la API
        # le sugiera y sigue andando sin esperar a nadie.
        "cloud_model_title": "Modelo:",
        "cloud_model_placeholder": "ej: gemini-3.6-flash",
        "cloud_model_tooltip": (
            "ID exacto del modelo a usar con el proveedor activo. Google "
            "(y, con menos frecuencia, Anthropic) cambian o dan de baja "
            "modelos seguido -- si \"Probar conexión\" falla con un error "
            "de \"modelo ya no disponible\", pegá acá el ID nuevo que "
            "sugiera el mensaje de error y probá de nuevo."
        ),
        "log_cloud_model_changed": "Modelo de Nube: {0}.",
        # patch_qt66 (2026-09-19): "Presupuesto de Sonnet" -> "Presupuesto
        # de Nube" -- este selector gobierna el techo de tokens de
        # CUALQUIER proveedor de Nube activo (antes solo existía Claude/
        # Sonnet, ver `_cloud_output_ceiling_tokens_for_cents`), así que
        # nombrarlo por el modelo de un solo proveedor quedaba
        # confuso/incorrecto apenas Gemini está seleccionado.
        "cloud_budget_title": "Presupuesto de Nube por turno",
        "cloud_budget_option_1c": "Bajo — respuestas cortas",
        "cloud_budget_option_2c": "Medio — funciones completas",
        "cloud_budget_option_4c": "Alto — módulos medianos",
        "cloud_budget_option_8c": "Extra — archivos grandes",
        "log_cloud_budget_changed": "Presupuesto de Nube: {0} por turno.",
        "header_cost_badge_idle": "$0.00/turno · ${0:.2f} sesión",
        "header_cost_badge_fmt": "${0:.2f}/turno · ${1:.2f} sesión",
        "header_cost_badge_tooltip": (
            "Costo estimado de la API de Nube activa (Claude o Gemini, "
            "según el proveedor elegido) -- el motor Local no tiene "
            "costo. El color indica qué tan cerca estuvo el último turno "
            "del presupuesto de Nube elegido."
        ),
        "sidebar_section_appearance": "Apariencia",
        "sidebar_section_engine": "Motor",
        "sidebar_section_workspace": "Workspace",
        "workspace_tools_toggle": "Habilitar herramientas de archivo",
        "workspace_tools_toggle_tooltip": (
            "Apagado (por defecto): SovNode nunca crea, lee, edita ni lista "
            "archivos por su cuenta -- todo pedido de código se responde "
            "directo en el chat. Prendé esto solo cuando quieras que pueda "
            "crear/leer/editar/listar archivos de verdad en el workspace "
            "activo."
        ),
        "log_workspace_tools_enabled": (
            "Herramientas de workspace ACTIVADAS: SovNode ya puede crear, "
            "leer, editar y listar archivos en el workspace activo."
        ),
        "log_workspace_tools_disabled": (
            "Herramientas de workspace DESACTIVADAS: todo pedido de código "
            "se responde directo en el chat, sin tocar el disco."
        ),
        # BLINDAJE (2026-09-17, pedido explícito del usuario -- "run_cmd
        # no debería estar con las herramientas de archivo, ¿no?"):
        # interruptor PROPIO para run_cmd, independiente del de arriba.
        # Ver el comentario junto a `Orchestrator.run_cmd_enabled`.
        "run_cmd_toggle": "Habilitar ejecución de comandos",
        "run_cmd_toggle_tooltip": (
            "Apagado (por defecto): SovNode nunca ejecuta comandos reales en "
            "tu sistema por su cuenta -- puede sugerirlos en el chat, pero no "
            "correrlos. Prendé esto solo cuando quieras que pueda ejecutar "
            "comandos de verdad (sandboxeados al workspace activo). "
            "Interruptor independiente de 'Habilitar herramientas de "
            "archivo' -- antes de este fix, run_cmd estaba siempre activo "
            "sin importar esa otra casilla."
        ),
        "log_run_cmd_enabled": (
            "Ejecución de comandos ACTIVADA: SovNode ya puede ejecutar "
            "comandos reales, sandboxeados al workspace activo."
        ),
        "log_run_cmd_disabled": (
            "Ejecución de comandos DESACTIVADA: SovNode puede sugerir "
            "comandos en el chat, pero no los ejecuta."
        ),
        # patch_qt64 (2026-09-18, pedido explícito del usuario -- "un
        # botón en la terminal... 'terminal más avanzada'"): interruptor
        # en el panel de consola que reorganiza el mismo stream de
        # log_message por turno, coloreando en rojo cualquier red de
        # seguridad que haya disparado (circuit-breaker, blindaje de
        # archivos, etc.) en vez de mostrar todo en un único color plano.
        "terminal_console_title": "🖥️ CONSOLA DE SISTEMA / LOGS",
        "btn_clear_terminal": "Limpiar",
        "advanced_terminal_toggle": "Terminal avanzada",
        "advanced_terminal_toggle_tooltip": (
            "Apagado (por defecto): la consola muestra cada línea tal "
            "cual llega, todas del mismo color. Prendé esto para agrupar "
            "por turno (entrada → ruteo → presupuesto → cada "
            "herramienta) y resaltar en rojo cualquier red de seguridad "
            "que haya intervenido (circuit-breaker, blindaje de "
            "archivos, error), con un resumen al cerrar cada turno."
        ),
        "log_advanced_terminal_enabled": (
            "Terminal avanzada ACTIVADA: la consola ahora agrupa por "
            "turno y resalta en rojo cualquier red de seguridad que "
            "dispare."
        ),
        "log_advanced_terminal_disabled": (
            "Terminal avanzada DESACTIVADA: la consola vuelve a mostrar "
            "cada línea tal cual llega."
        ),
        "adv_turn_header": '┌─ turno "{}"',
        "adv_turn_footer_clean": "└─ ✓ sin problemas · {tools} tool_call(s) · ${cost:.4f}",
        "adv_turn_footer_problems": "└─ ⚠ {count} problema(s): {names} · ${cost:.4f}",
        "download_dialog_title": "Descargar modelo de Ollama",
        "download_dialog_desc": "Ingresa o selecciona el tag del modelo a descargar (ej. qwen2.5:7b):",
        "download_btn_start": "Descargar",
        "download_status_idle": "Listo para descargar.",
        "intent_analyzing": "Analizando la intención de la instrucción...",
        "intent_web_search": "Consultando fuentes en internet...",
        "intent_synthesizing": "Sintetizando respuesta con Ollama...",
        "intent_reasoning": "Razonando internamente...",
        "intent_writing_answer": "Redactando la respuesta final...",
        "log_score_check": (
            "🔍 [ScoreCheck] marcadores reclamados en la respuesta: {} | "
            "marcadores en evidencia ({} chars): {} | sin respaldo: {}"
        ),
        "log_score_check_none": "ninguno",
        "log_pass1_no_thought": (
            "⚠️ Pasada 1 no abrió <thought> — usando su salida como respuesta directa."
        ),
        "log_twopass_summary": "📊 [TwoPass] razonamiento≈{}/{}tok | respuesta≈{}/{}tok",
        "log_stream_perf": (
            "⚡ [{}] prefill={}tok/{:.2f}s ({:.1f}tok/s) | "
            "decode={}tok/{:.2f}s ({:.1f}tok/s) | carga={:.2f}s"
        ),
        "log_cpu_phase_timing": "⏱️ [{}] {:.2f}s",
        "log_visible_answer_recovered": (
            "🚑 Respuesta visible vacía (turno consumido en <thought>): recuperada."
        ),
        "intent_tree_of_thought": "Explorando dos líneas de razonamiento alternas...",
        "intent_tool_exec": "⚙️ Ejecutando herramienta local autónoma...",
        "notice_tool_exec": "\n\n⚙️ *Ejecutando herramienta local autónoma (`{}`)...*\n\n",
        "log_init_ok": "SovNode inicializado correctamente en interfaz gráfica.",
        "log_theme_changed": "Tema visual cambiado a: {}",
        "log_lang_changed": "Idioma cambiado a: {}",
        "log_new_chat": "Conversación limpiada, nueva sesión iniciada.",
        "log_sending": "Enviando instrucción: '{}...'",
        "log_stopped_by_user": "Generación detenida por el usuario.",
        "log_turn_error": "Error en turno: {}",
        "log_turn_completed": "Turno completado exitosamente usando el modelo: {}",
        "log_web_search_degraded": "Búsqueda web degradada: {}",
        "log_visual_card_skipped": "Tarjeta visual omitida: el tema no es deportivo, noticioso ni biográfico.",
        "log_visual_card_no_images": "Tarjeta visual omitida: ninguna fuente trajo una miniatura real (solo Wikipedia/favicon de respaldo).",
        "log_reasoning_leak_cleaned": "Fuga de razonamiento detectada (protocolo sin etiquetar): se depuró la respuesta visible.",
        "log_thin_web_context": "Las fuentes recuperadas no cubren lo específico consultado: se instruyó al modelo a declararlo en vez de generalizar.",
        "log_ollama_status": "Estado de conexión Ollama actualizado: {}",
        "log_export_ok": "Chat exportado a: {}",
        "log_export_error": "Error al exportar chat: {}",
        "log_voice_listening": "Escuchando... Habla ahora.",
        "log_voice_processing": "Procesando audio localmente con Whisper...",
        "log_voice_transcribed": "Transcripción capturada: '{}'",
        "log_voice_no_speech": "No se detectó voz clara en la grabación.",
        "no_detail_fallback": "sin detalle",
        "log_model_downloaded": "Modelo '{}' disponible localmente tras la descarga.",
        "web_card_live_title": "Investigación en vivo realizada",
        "web_card_no_data": "Sin datos web en tiempo real — respondiendo desde memoria local. ({})",
        "trace_analysis_label": "Traza analítica",
        "code_copied_title": "Código copiado",
        "code_copied_msg": "El bloque se copió al portapapeles.",
        "file_saved_title": "Archivo guardado",
        "file_saved_msg": "Bloque exportado correctamente a:\n{}",
        "btn_copy_code": "Copiar",
        "btn_save_code": "Guardar",
        "dialog_save_code": "Guardar bloque de código",
        "dialog_save_filter": "Todos los archivos (*.*)",
        "trace_summary_fmt": (
            "{route}  |  Resultado: {outcome}  |  Modelo: {model}\n"
            "Score routing: {score}  |  Nodo persistido: {persisted}\n"
            "Estado lógico: {logical}  |  Búsqueda intentada: {web_attempted}  |  "
            "Contexto web usado: {web_used}  |  Auto-reparaciones AST: {repairs}"
        ),
        "trace_engines_title": "Motores simbólicos:",
        "yes_label": "Sí",
        "no_label": "No",
        "tts_listen": "🔊 Escuchar mensaje",
        "tts_stop": "⏹️ Detener lectura",
        "turn_cancelled": "Generación cancelada.",
        "pipeline_no_done": "El pipeline terminó sin emitir DONE.",
        "log_cache_hit": "[CACHE] Respuesta servida desde caché semántico.",
        "export_no_messages_title": "Sin mensajes",
        "export_no_messages_msg": "No hay mensajes para exportar todavía.",
        "export_dialog_caption": "Exportar conversación",
        "export_dialog_filter": "Markdown (*.md);;Texto (*.txt);;Todos los archivos (*.*)",
        "export_success_title": "Chat exportado",
        "export_success_msg": "La conversación se guardó correctamente en:\n{}",
        "export_error_title": "Error de exportación",
        "export_error_msg": "No se pudo exportar el chat:\n{}",
        "file_save_error_title": "Error al guardar",
        "file_save_error_msg": "No se pudo escribir el archivo:\n{}",
        "training_export_dialog_caption": "Elegir carpeta de salida para el dataset de entrenamiento",
        "log_training_export_failed": "Fallo al exportar dataset de entrenamiento: {}",
        "training_export_error_msg": "No se pudo exportar el dataset de entrenamiento:\n{}",
        "no_corrections_title": "Sin correcciones todavía",
        "no_corrections_msg": (
            "Todavía no hay correcciones registradas en el WAL para exportar. "
            "Esto es normal en una instalación nueva — vuelve a intentarlo tras "
            "usar SovNode un tiempo más, cuando el pipeline haya corregido algún "
            "turno (marcador sin respaldo, idioma equivocado, etc.)."
        ),
        "log_training_export_ok": "Dataset de entrenamiento exportado: {} par(es) -> {}",
        "training_export_success_title": "Dataset exportado",
        "training_export_success_msg": "{} par(es) de entrenamiento exportados a:\n{}\n\n{}",
        "training_export_files_note": "Archivos: dpo_pairs.jsonl (prompt/chosen/rejected) y sft_pairs.jsonl (prompt/response).",
        "tray_still_active_title": "SovNode sigue activo",
        "tray_still_active_msg": "La aplicación continúa disponible en la bandeja del sistema.",
        "workspaces_list_tooltip": (
            "Carpetas vigiladas en segundo plano: los archivos que "
            "cambien adentro se reindexan solos en la memoria vectorial."
        ),
        "btn_add_workspace": "＋ Añadir carpeta",
        "btn_remove_workspace": "Quitar",
        "btn_export_training_tooltip": (
            "Exporta un dataset DPO/SFT a partir de las correcciones reales "
            "que el pipeline ya detectó en el WAL — ver training_export.py."
        ),
        "mic_tooltip": "Dictado de voz a texto (STT local)",
        "tts_toggle_tooltip_off": "Leer respuestas en voz alta (TTS local) — apagado",
        "tts_toggle_tooltip_on": "Leer respuestas en voz alta (TTS local) — encendido",
        "attach_tooltip": "Adjuntar una imagen",
        "attach_dialog_title": "Elegir una imagen",
        "attach_dialog_filter": "Imágenes (*.png *.jpg *.jpeg *.webp *.bmp)",
        "attach_load_failed": "No se pudo cargar la imagen elegida.",
        "attach_file_load_failed": "No se pudo leer el archivo '{}'.",
        "attach_pasted_image_name": "Imagen pegada",
        "log_tts_enabled": "🔊 Lectura en voz alta activada — las próximas respuestas se leerán en voz alta.",
        "log_tts_disabled": "🔇 Lectura en voz alta desactivada.",
        "log_workspaces_load_failed": "No se pudieron cargar los Workspaces guardados: {}",
        "log_workspaces_restored": "📁 {} Workspace(s) restaurado(s) de la sesión anterior.",
        "workspace_select_dialog": "Seleccionar carpeta de Workspace",
        "log_workspace_already_added": "'{}' ya está en Workspaces.",
        "log_workspace_added": "📁 Workspace agregado: '{}' — indexando en segundo plano.",
        "log_workspace_persist_failed": "No se pudo persistir el Workspace '{}': {}",
        "log_workspace_unpersist_failed": "No se pudo quitar el Workspace persistido '{}': {}",
        "log_workspace_removed": "📁 Workspace quitado: '{}' — {} fragmento(s) retirado(s) de memoria.",
        "log_workspace_cleanup_degraded": "Limpieza de '{}' degradada: {}",
        "log_tool_sandbox_synced": "🔧 Carpeta activa para herramientas (leer/escribir/listar archivos): '{}'.",
        "log_matplotlib_missing": (
            "matplotlib no está instalado — las ecuaciones se mostrarán como "
            "texto LaTeX crudo en vez de renderizarse. Instalá con: pip install "
            "matplotlib (en el mismo entorno de Python con el que corrés SovNode) "
            "y reiniciá la app."
        ),
        "log_matplotlib_ok": (
            "Renderizador de ecuaciones LaTeX activo (matplotlib detectado — las "
            "ecuaciones $...$/[...] se van a mostrar como imagen, no como texto crudo)."
        ),
        "model_tag_empty": "El tag del modelo no puede estar vacío.",
        "model_download_cancelled": "Descarga cancelada por el usuario.",
        "model_download_success": "Modelo '{}' descargado con éxito.",
        "badge_sandbox_verified": "🧪 Verificado en sandbox",
        "badge_tot_agreement_suffix": " ({pct} acuerdo)",
        "badge_epistemic_drift": "⚠️ Deriva epistémica corregida",
        "export_md_header": "# Transcripción de sesión — SovNode",
        "export_md_date_label": "Fecha de exportación",
        "export_md_model_label": "Modelo",
        "export_md_role_user": "Usuario",
        "log_file_drop_read_failed": "No se pudo leer '{}' para indexar: {}",
        "log_file_drop_indexed": "📚 '{}' indexado en memoria vectorial: {} fragmento(s).",
        "log_file_drop_index_degraded": "Indexado de '{}' degradado: {}",
        "log_workspace_file_reindexed": "📚 [Workspace] '{}' reindexado: {} fragmento(s).",
        "log_workspace_file_removed": "🗑️ [Workspace] '{}' retirado: {} fragmento(s).",
        "log_workspace_watcher_error": "Workspace watcher: {}",
    },
    "English": {
        "theme_title": "VISUAL THEME",
        "lang_title": "LANGUAGE / IDIOMA",
        "status_card": "NODE STATUS",
        "session_card": "SESSION STATUS",
        "support_card": "OPEN SOURCE PROJECT",
        "btn_donate": "Support the project",
        "support_desc": "SovNode is 100% free and private. Your support keeps development active.",
        "btn_new_chat": "New conversation",
        "btn_new_chat_tooltip": "New chat tab",
        "tab_new_chat_title": "New chat",
        "tab_close_tooltip": "Close tab",
        "btn_config_tooltip": "Settings",
        "btn_export": "Export chat (.md)",
        "btn_export_training": "Export training dataset",
        "btn_minimize": "— Minimize to tray",
        "header_title": "Command Console",
        "header_subtitle": "Local sovereign session · Enter to send · Shift+Enter for new line",
        "placeholder": "Type an instruction for SovNode...",
        "btn_send": "Send",
        "btn_stop": "Stop",
        "processing": "SovNode is processing...",
        "terminal_btn_show": "Terminal",
        "terminal_btn_hide": "Hide Terminal",
        "status_online": "Online · Local Ollama",
        "status_offline": "Offline · Ollama unavailable",
        "status_checking": "Verifying local node...",
        "header_online": "● Local node online",
        "header_offline": "● Local node offline",
        "role_general": "Role: general conversation",
        "role_coder": "Role: code generation",
        "turns_count": "Processed turns: {}",
        "welcome_msg": "System initialized. Ready to receive instructions.",
        "new_chat_msg": "New conversation started. How can I help you?",
        "donate_title": "Support SovNode",
        "donate_header": "Support SovNode Development",
        "donate_desc": "SovNode is a 100% independent and open-source project. If you find this tool useful, any contribution helps keep development active with new features.",
        "donate_btn_kofi": "Donate via Ko-fi / PayPal",
        "donate_crypto_title": "💳 USDT (TRON Network / TRC-20):",
        "donate_btn_copy": "Copy",
        "donate_copy_title": "Address Copied",
        "donate_copy_msg": "The USDT (TRC-20) address was copied to the clipboard.",
        "jump_to_bottom": "⬇ Jump to bottom",
        "web_badge_live": "🌐 LIVE WEB",
        "web_badge_local": "💾 LOCAL MEMORY",
        "msg_sender_user": "YOU",
        "msg_sender_error": "ERROR",
        "msg_sender_warning": "SOVNODE (auto-corrected)",
        "btn_download_model": "Download model",
        "engine_title": "GENERATION ENGINE",
        "engine_local": "Local (Ollama)",
        "engine_cloud": "Cloud (external API)",
        # patch_qt66 (2026-09-19) -- see the Spanish block for the full
        # rationale (user request: ran out of Claude credits, asked to
        # add Gemini as an alternative provider).
        "cloud_provider_title": "Cloud provider:",
        "cloud_provider_anthropic": "Claude (Anthropic)",
        "cloud_provider_gemini": "Gemini (Google)",
        "cloud_key_placeholder": "sk-ant-...",
        "cloud_key_tooltip": "API key from console.anthropic.com — NOT your Claude.ai account password. Stored only on this machine.",
        "cloud_key_placeholder_gemini": "AIza...",
        "cloud_key_tooltip_gemini": "API key from aistudio.google.com (Google AI Studio) — stored only on this machine, separately from your Claude key.",
        "btn_test_cloud_key": "Test connection",
        "btn_forget_cloud_key": "Forget saved key",
        "btn_forget_cloud_key_tooltip": "Erases the active provider's saved API key on this machine (Windows Registry). Doesn't affect the rest of your settings or the other provider's key.",
        "log_cloud_key_forgotten": "Saved API key erased from this device for the active provider.",
        "cloud_usage_idle": "No usage yet this session.",
        "cloud_usage_fmt": "{0} calls · {1} tok in / {2} tok out · ${3:.4f} spent",
        "cloud_test_testing": "Testing connection to the {0} API...",
        "cloud_test_ok": "✅ Connection OK — the API key works (model: {0}).",
        "cloud_test_fail": "❌ Connection failed: {0}",
        "cloud_test_no_key": "Load an API key before testing the connection.",
        "log_engine_changed": "Generation engine: {0}.",
        "log_cloud_provider_changed": "Cloud provider: {0}.",
        # patch_qt67 (2026-09-19) -- see the Spanish block: the user hit
        # a real "model no longer available" error from Gemini within
        # minutes of patch_qt66 shipping, so this field lets them paste
        # a replacement model ID without waiting on a code patch.
        "cloud_model_title": "Model:",
        "cloud_model_placeholder": "e.g. gemini-3.6-flash",
        "cloud_model_tooltip": (
            "Exact model ID to use with the active provider. Google "
            "(and, less often, Anthropic) retire or rename models "
            "fairly often -- if \"Test connection\" fails with a \"model "
            "no longer available\" error, paste the new ID it suggests "
            "here and try again."
        ),
        "log_cloud_model_changed": "Cloud model: {0}.",
        # patch_qt66 (2026-09-19): see the Spanish block for why this
        # dropped the "Sonnet" name (now governs any active Cloud
        # provider, not just Claude).
        "cloud_budget_title": "Cloud budget per turn",
        "cloud_budget_option_1c": "Low — short answers",
        "cloud_budget_option_2c": "Medium — full functions",
        "cloud_budget_option_4c": "High — medium modules",
        "cloud_budget_option_8c": "Extra — large files",
        "log_cloud_budget_changed": "Cloud budget: {0} per turn.",
        "header_cost_badge_idle": "$0.00/turn · ${0:.2f} session",
        "header_cost_badge_fmt": "${0:.2f}/turn · ${1:.2f} session",
        "header_cost_badge_tooltip": (
            "Estimated cost of the active Cloud API (Claude or Gemini, "
            "depending on the chosen provider) -- the Local engine is "
            "free. Color shows how close the last turn got to your "
            "chosen Cloud budget."
        ),
        "sidebar_section_appearance": "Appearance",
        "sidebar_section_engine": "Engine",
        "sidebar_section_workspace": "Workspace",
        "workspace_tools_toggle": "Enable file tools",
        "workspace_tools_toggle_tooltip": (
            "Off (default): SovNode never creates, reads, edits or lists "
            "files on its own -- every code request is answered directly "
            "in the chat. Turn this on only when you want it to actually "
            "create/read/edit/list files in the active workspace."
        ),
        "log_workspace_tools_enabled": (
            "Workspace tools ENABLED: SovNode can now create, read, edit "
            "and list files in the active workspace."
        ),
        "log_workspace_tools_disabled": (
            "Workspace tools DISABLED: every code request is now answered "
            "directly in the chat, without touching disk."
        ),
        "run_cmd_toggle": "Enable command execution",
        "run_cmd_toggle_tooltip": (
            "Off (default): SovNode never runs real commands on your system "
            "on its own -- it can suggest them in chat, but won't run them. "
            "Turn this on only when you want it to actually execute commands "
            "(sandboxed to the active workspace). Independent switch from "
            "'Enable file tools' -- before this fix, run_cmd was always "
            "active regardless of that other checkbox."
        ),
        "log_run_cmd_enabled": (
            "Command execution ENABLED: SovNode can now run real commands, "
            "sandboxed to the active workspace."
        ),
        "log_run_cmd_disabled": (
            "Command execution DISABLED: SovNode can suggest commands in "
            "chat, but won't run them."
        ),
        "terminal_console_title": "🖥️ SYSTEM CONSOLE / LOGS",
        "btn_clear_terminal": "Clear",
        "advanced_terminal_toggle": "Advanced terminal",
        "advanced_terminal_toggle_tooltip": (
            "Off (default): the console shows every line as it arrives, "
            "all the same color. Turn this on to group lines by turn "
            "(input → routing → budget → each tool) and highlight in red "
            "any safety net that fired (circuit-breaker, file guard, "
            "error), with a summary when each turn closes."
        ),
        "log_advanced_terminal_enabled": (
            "Advanced terminal ENABLED: the console now groups by turn "
            "and highlights in red any safety net that fires."
        ),
        "log_advanced_terminal_disabled": (
            "Advanced terminal DISABLED: the console goes back to "
            "showing every line as it arrives."
        ),
        "adv_turn_header": '┌─ turn "{}"',
        "adv_turn_footer_clean": "└─ ✓ no problems · {tools} tool_call(s) · ${cost:.4f}",
        "adv_turn_footer_problems": "└─ ⚠ {count} problem(s): {names} · ${cost:.4f}",
        "download_dialog_title": "Download Ollama model",
        "download_dialog_desc": "Enter or select the model tag to download (e.g. qwen2.5:7b):",
        "download_btn_start": "Download",
        "download_status_idle": "Ready to download.",
        "intent_analyzing": "Analyzing instruction intent...",
        "intent_web_search": "Searching online sources...",
        "intent_synthesizing": "Synthesizing response with Ollama...",
        "intent_reasoning": "Reasoning internally...",
        "intent_writing_answer": "Writing the final answer...",
        "log_score_check": (
            "🔍 [ScoreCheck] score(s) claimed in the response: {} | "
            "score(s) in evidence ({} chars): {} | unsupported: {}"
        ),
        "log_score_check_none": "none",
        "log_pass1_no_thought": (
            "⚠️ Pass 1 did not open <thought> — using its output as the direct response."
        ),
        "log_twopass_summary": "📊 [TwoPass] reasoning≈{}/{}tok | answer≈{}/{}tok",
        "log_stream_perf": (
            "⚡ [{}] prefill={}tok/{:.2f}s ({:.1f}tok/s) | "
            "decode={}tok/{:.2f}s ({:.1f}tok/s) | load={:.2f}s"
        ),
        "log_cpu_phase_timing": "⏱️ [{}] {:.2f}s",
        "log_visible_answer_recovered": (
            "🚑 Empty visible answer (turn consumed inside <thought>): recovered."
        ),
        "intent_tree_of_thought": "Exploring alternate reasoning paths...",
        "intent_tool_exec": "Executing local autonomous tool...",
        "notice_tool_exec": "\n\n⚙️ *Executing local autonomous tool (`{}`)...*\n\n",
        "log_init_ok": "SovNode initialized successfully in the graphical interface.",
        "log_theme_changed": "Visual theme changed to: {}",
        "log_lang_changed": "Language changed to: {}",
        "log_new_chat": "Conversation cleared, new session started.",
        "log_sending": "Sending instruction: '{}...'",
        "log_stopped_by_user": "Generation stopped by the user.",
        "log_turn_error": "Turn error: {}",
        "log_turn_completed": "Turn completed successfully using model: {}",
        "log_web_search_degraded": "Web search degraded: {}",
        "log_visual_card_skipped": "Visual card skipped: the topic isn't sports, breaking news, or biographical.",
        "log_visual_card_no_images": "Visual card skipped: no source had a real thumbnail (Wikipedia/backup favicon only).",
        "log_reasoning_leak_cleaned": "Reasoning leak detected (untagged protocol): the visible answer was cleaned up.",
        "log_thin_web_context": "Retrieved sources don't cover the specific question: the model was instructed to say so instead of generalizing.",
        "log_ollama_status": "Ollama connection status updated: {}",
        "log_export_ok": "Chat exported to: {}",
        "log_export_error": "Error exporting chat: {}",
        "log_voice_listening": "Listening... Speak now.",
        "log_voice_processing": "Processing audio locally with Whisper...",
        "log_voice_transcribed": "Transcription captured: '{}'",
        "log_voice_no_speech": "No clear speech detected in the recording.",
        "no_detail_fallback": "no detail",
        "log_model_downloaded": "Model '{}' available locally after the download.",
        "web_card_live_title": "🌐 Live research completed",
        "web_card_no_data": "No real-time web data — answering from local memory. ({})",
        "trace_analysis_label": "Analytical trace",
        "code_copied_title": "Code copied",
        "code_copied_msg": "The code block was copied to the clipboard.",
        "file_saved_title": "File saved",
        "file_saved_msg": "Block exported successfully to:\n{}",
        "btn_copy_code": "Copy",
        "btn_save_code": "Save",
        "dialog_save_code": "Save code block",
        "dialog_save_filter": "All files (*.*)",
        "trace_summary_fmt": (
            "{route}  |  Outcome: {outcome}  |  Model: {model}\n"
            "Routing score: {score}  |  Knowledge node persisted: {persisted}\n"
            "Logical status: {logical}  |  Search attempted: {web_attempted}  |  "
            "Web context used: {web_used}  |  AST auto-repairs: {repairs}"
        ),
        "trace_engines_title": "Symbolic engines:",
        "yes_label": "Yes",
        "no_label": "No",
        "tts_listen": "🔊 Listen to message",
        "tts_stop": "⏹️ Stop reading",
        "turn_cancelled": "Generation cancelled.",
        "pipeline_no_done": "The pipeline ended without emitting DONE.",
        "log_cache_hit": "[CACHE] Response served from semantic cache.",
        "export_no_messages_title": "No messages",
        "export_no_messages_msg": "There are no messages to export yet.",
        "export_dialog_caption": "Export conversation",
        "export_dialog_filter": "Markdown (*.md);;Text (*.txt);;All files (*.*)",
        "export_success_title": "Chat exported",
        "export_success_msg": "The conversation was saved successfully to:\n{}",
        "export_error_title": "Export error",
        "export_error_msg": "Could not export the chat:\n{}",
        "file_save_error_title": "Save error",
        "file_save_error_msg": "Could not write the file:\n{}",
        "training_export_dialog_caption": "Choose the output folder for the training dataset",
        "log_training_export_failed": "Failed to export training dataset: {}",
        "training_export_error_msg": "Could not export the training dataset:\n{}",
        "no_corrections_title": "No corrections yet",
        "no_corrections_msg": (
            "There are no corrections recorded in the WAL to export yet. "
            "This is normal on a fresh installation — try again after using "
            "SovNode a bit more, once the pipeline has corrected some turn "
            "(unsupported score, wrong language, etc.)."
        ),
        "log_training_export_ok": "Training dataset exported: {} pair(s) -> {}",
        "training_export_success_title": "Dataset exported",
        "training_export_success_msg": "{} training pair(s) exported to:\n{}\n\n{}",
        "training_export_files_note": "Files: dpo_pairs.jsonl (prompt/chosen/rejected) and sft_pairs.jsonl (prompt/response).",
        "tray_still_active_title": "SovNode is still active",
        "tray_still_active_msg": "The application remains available in the system tray.",
        "workspaces_list_tooltip": (
            "Folders watched in the background: files that change inside "
            "get reindexed automatically into vector memory."
        ),
        "btn_add_workspace": "＋ Add folder",
        "btn_remove_workspace": "Remove",
        "btn_export_training_tooltip": (
            "Exports a DPO/SFT dataset from the real corrections the pipeline "
            "already detected in the WAL — see training_export.py."
        ),
        "mic_tooltip": "Voice-to-text dictation (local STT)",
        "tts_toggle_tooltip_off": "Read replies out loud (local TTS) — off",
        "tts_toggle_tooltip_on": "Read replies out loud (local TTS) — on",
        "attach_tooltip": "Attach an image",
        "attach_dialog_title": "Choose an image",
        "attach_dialog_filter": "Images (*.png *.jpg *.jpeg *.webp *.bmp)",
        "attach_load_failed": "Couldn't load the chosen image.",
        "attach_file_load_failed": "Couldn't read the file '{}'.",
        "attach_pasted_image_name": "Pasted image",
        "log_tts_enabled": "🔊 Read-aloud enabled — upcoming replies will be spoken out loud.",
        "log_tts_disabled": "🔇 Read-aloud disabled.",
        "log_workspaces_load_failed": "Could not load saved Workspaces: {}",
        "log_workspaces_restored": "📁 {} Workspace(s) restored from the previous session.",
        "workspace_select_dialog": "Select Workspace folder",
        "log_workspace_already_added": "'{}' is already in Workspaces.",
        "log_workspace_added": "📁 Workspace added: '{}' — indexing in the background.",
        "log_workspace_persist_failed": "Could not persist Workspace '{}': {}",
        "log_workspace_unpersist_failed": "Could not remove persisted Workspace '{}': {}",
        "log_workspace_removed": "📁 Workspace removed: '{}' — {} fragment(s) removed from memory.",
        "log_workspace_cleanup_degraded": "Cleanup of '{}' degraded: {}",
        "log_tool_sandbox_synced": "🔧 Active folder for tools (read/write/list files): '{}'.",
        "log_matplotlib_missing": (
            "matplotlib is not installed — equations will be shown as raw LaTeX "
            "text instead of rendered. Install it with: pip install matplotlib "
            "(in the same Python environment you run SovNode with) and restart "
            "the app."
        ),
        "log_matplotlib_ok": (
            "LaTeX equation renderer active (matplotlib detected — $...$/[...] "
            "equations will be shown as an image, not raw text)."
        ),
        "model_tag_empty": "The model tag cannot be empty.",
        "model_download_cancelled": "Download cancelled by the user.",
        "model_download_success": "Model '{}' downloaded successfully.",
        "badge_sandbox_verified": "🧪 Verified in sandbox",
        "badge_tot_agreement_suffix": " ({pct} agreement)",
        "badge_epistemic_drift": "⚠️ Epistemic drift corrected",
        "export_md_header": "# Session Transcript — SovNode",
        "export_md_date_label": "Export date",
        "export_md_model_label": "Model",
        "export_md_role_user": "User",
        "log_file_drop_read_failed": "Could not read '{}' for indexing: {}",
        "log_file_drop_indexed": "📚 '{}' indexed in vector memory: {} chunk(s).",
        "log_file_drop_index_degraded": "Indexing of '{}' degraded: {}",
        "log_workspace_file_reindexed": "📚 [Workspace] '{}' reindexed: {} chunk(s).",
        "log_workspace_file_removed": "🗑️ [Workspace] '{}' removed: {} chunk(s).",
        "log_workspace_watcher_error": "Workspace watcher: {}",
    },
}


THEMES = {
    "Cyberpunk Dark": {
        "bg": "#0E1117",
        "sidebar": "#14171F",
        "card": "#171B24",
        "input": "#1B1F2A",
        "assistant": "#1E222C",
        "user": "#15304A",
        "accent": "#2EC5E0",
        "accent_soft": "#123A44",
        "text": "#E6E8EC",
        "secondary": "#8B92A5",
        "border": "#262B36",
        "success": "#3DDC97",
        "warning": "#F2C14E",
        "danger": "#F2555A",
        "code": "#10141D",
        "coder_model": "#F2C14E",
        "general_model": "#2EC5E0",
    },
    "OLED Pure Black": {
        "bg": "#000000",
        "sidebar": "#080808",
        "card": "#101010",
        "input": "#151515",
        "assistant": "#171717",
        "user": "#3A2159",
        "accent": "#C77DFF",
        "accent_soft": "#2E1A4D",
        "text": "#FFFFFF",
        "secondary": "#B4B4B4",
        "border": "#303030",
        "success": "#5AFFAA",
        "warning": "#FFD166",
        "danger": "#FF6B6B",
        "code": "#070707",
        "coder_model": "#FFD166",
        "general_model": "#C77DFF",
    },
    "Nordic Slate": {
        "bg": "#2E3440",
        "sidebar": "#252B36",
        "card": "#3B4252",
        "input": "#434C5E",
        "assistant": "#3B4252",
        "user": "#4C566A",
        "accent": "#D08770",
        "accent_soft": "#4A3B36",
        "text": "#ECEFF4",
        "secondary": "#D8DEE9",
        "border": "#4C566A",
        "success": "#A3BE8C",
        "warning": "#EBCB8B",
        "danger": "#BF616A",
        "code": "#272D38",
        "coder_model": "#EBCB8B",
        "general_model": "#D08770",
    },
}

_ACTIVE_THEME: dict[str, str] = dict(THEMES["Cyberpunk Dark"])

_RESOLVED_FONT_FAMILY: str = "Inter"


def _rgba(hex_color: str, alpha: float) -> str:
    """`#RRGGBB` -> `rgba(r, g, b, a)` para tintes translúcidos en QSS.

    Qt no acepta hex de 8 dígitos (`#RRGGBBAA`) en las hojas de estilo,
    así que un color de acento atenuado (p. ej. el fondo rojo suave de la
    (x) de cerrar pestaña al pasar el cursor) hay que expresarlo como
    `rgba()`. Mantiene el tinte anclado al color del tema en vez de
    hardcodear un rojo/azul suelto que no seguiría al cambiar de tema.
    """
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


def build_style(theme: dict[str, str], font_family: str = "Inter") -> str:
    """Genera el QSS completo para el tema seleccionado, con estética HUD/Cyberpunk pulida.

    `font_family` es el nombre real que Qt le asignó a Inter al cargarlo
    (ver `load_app_fonts()`) - se recibe como parámetro en vez de
    hardcodear "Inter" aquí para que, si la carga falla en el entorno del
    usuario, esta función siga recibiendo un nombre de familia válido
    (el que ya esté activo en QApplication) sin necesitar dos rutas de
    código distintas.
    """
    return f"""
        QWidget {{
            font-family: "{font_family}", "Segoe UI", sans-serif;
        }}

        QMainWindow, QWidget#centralWidget {{
            background-color: {theme["bg"]};
            color: {theme["text"]};
        }}

        QFrame#sidebar {{
            background-color: {theme["sidebar"]};
        }}

        /* patch_qt72: QScrollArea#sidebarScroll es el contenedor nuevo
           que envuelve a QFrame#sidebar (ver _create_ui) -- sin esta
           regla, el QScrollArea y su viewport toman el fondo blanco
           por defecto de Qt, que se asomaria como un borde/flash
           blanco alrededor del sidebar (mismo tipo de problema que ya
           tenia QListWidget#workspacesList sin su propia regla, mas
           arriba). El borde derecho de 1px se movio del QFrame de
           adentro al QScrollArea de afuera para que siga
           dibujandose en el mismo lugar visual de siempre. */
        QScrollArea#sidebarScroll {{
            background-color: {theme["sidebar"]};
            border: none;
            border-right: 1px solid {theme["border"]};
        }}

        QScrollArea#sidebarScroll > QWidget > QWidget {{
            background-color: {theme["sidebar"]};
        }}

        QFrame#sidebar QLabel, QFrame#sidebarCard QLabel {{
            color: {theme["text"]};
        }}

        /* border-radius bajado de 12px a 8px (rediseño - "jerarquía de
           esquinas"): las tarjetas de la barra lateral son CHROME
           estructural, no un elemento interactivo protagonista, así que
           llevan un redondeo más discreto. El redondeo pronunciado
           (14-22px) se reserva para los pocos elementos que sí deben
           destacar como interactivos: la burbuja de usuario, el campo de
           entrada y el botón de enviar - ver más abajo. Antes casi todo
           en la UI compartía el mismo radio de 6-16px sin ninguna razón
           para que un elemento se sintiera más "protagonista" que otro. */
        QFrame#sidebarCard {{
            background-color: {theme["card"]};
            border: 1px solid {theme["border"]};
            border-radius: 8px;
        }}

        /* Chip de adjunto pendiente (rediseño visual, pedido del usuario
           "podemos mejorar la interfaz en diseño?" - ver
           attachment_preview_container en _create_ui): mismo tratamiento
           de "chrome estructural" que sidebarCard (8px, no protagonista)
           - agrupa miniatura+nombre+"x" en una tarjeta que abraza su
           contenido, en vez de flotar suelto sobre el fondo del chat. */
        QFrame#attachmentPreviewCard {{
            background-color: {theme["card"]};
            border: 1px solid {theme["border"]};
            border-radius: 10px;
        }}

        QLabel#appTitle {{
            color: {theme["text"]};
            font-size: 20px;
            font-weight: 800;
            letter-spacing: 1px;
        }}

        QLabel#appSubtitle {{
            color: {theme["secondary"]};
            font-size: 11px;
        }}

        QLabel#sectionTitle {{
            color: {theme["secondary"]};
            font-size: 10px;
            font-weight: bold;
            letter-spacing: 1px;
        }}

        /* Encabezado de sección plegable de la barra lateral (rediseño
           2026-09-15, "agruparía esto en secciones plegables... con
           íconos por sección en vez de solo texto en mayúscula") --
           chrome plano (sin fondo ni borde propio, ya vive dentro de
           sidebar_layout) para separarse visualmente de las tarjetas
           "sidebarCard" que agrupa por dentro. */
        QPushButton#sidebarSectionHeader {{
            background-color: transparent;
            color: {theme["text"]};
            border: none;
            border-bottom: 1px solid {theme["border"]};
            border-radius: 0px;
            padding: 4px 2px 8px 2px;
            font-size: 12px;
            font-weight: 700;
            text-align: left;
        }}

        QPushButton#sidebarSectionHeader:hover {{
            color: {theme["accent"]};
        }}

        QFrame#headerBar {{
            background-color: {theme["sidebar"]};
            border-bottom: 1px solid {theme["border"]};
        }}

        QLabel#headerTitle {{
            color: {theme["text"]};
            font-size: 17px;
            font-weight: 700;
        }}

        QLabel#headerSubtitle {{
            color: {theme["secondary"]};
            font-size: 11px;
        }}

        QScrollArea#chatScrollArea {{
            border: none;
            background-color: {theme["bg"]};
        }}

        QWidget#chatWidget {{
            background-color: {theme["bg"]};
        }}

        /* Las respuestas de SovNode siguen fluyendo como texto continuo
           sin caja (Módulo 2 previo). El mensaje del usuario, en cambio,
           vuelve a ser un globo con fondo propio, alineado a la derecha
           (ver MessageBubble.__init__) — la asimetría es intencional:
           distingue de un vistazo quién dijo qué sin repetir la etiqueta
           de rol en cada mensaje. Error y advertencia conservan un acento
           de color mínimo (borde izquierdo delgado, sin caja completa)
           porque son señales de seguridad que no deben volverse invisibles. */
        QFrame#assistantCard {{
            background: transparent;
            border: none;
        }}

        QFrame#userCard {{
            background-color: {theme["user"]};
            border: none;
            border-radius: 16px;
        }}

        QFrame#errorCard {{
            background: transparent;
            border: none;
            border-left: 3px solid {theme["danger"]};
        }}

        QFrame#warningCard {{
            background: transparent;
            border: none;
            border-left: 3px solid {theme["warning"]};
        }}

        QLabel#messageSender {{
            color: {theme["accent"]};
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 0.5px;
        }}

        QLabel#messageTimestamp {{
            color: {theme["secondary"]};
            font-size: 10px;
        }}

        QTextEdit#inputField {{
            background-color: {theme["input"]};
            color: {theme["text"]};
            border: 1px solid {theme["border"]};
            border-radius: 16px;
            padding: 10px;
            font-size: 14px;
            selection-background-color: {theme["accent"]};
        }}

        QTextEdit#inputField:focus {{
            border: 1px solid {theme["accent"]};
        }}

        QTextEdit#inputField:disabled {{
            color: {theme["secondary"]};
            background-color: {theme["card"]};
        }}

        QPushButton#sendButton {{
            background-color: {theme["accent"]};
            color: white;
            border: none;
            border-radius: 22px;
            font-weight: bold;
            font-size: 13px;
        }}

        QPushButton#sendButton:hover {{
            background-color: {theme["accent_soft"]};
            border: 1px solid {theme["accent"]};
        }}

        QPushButton#sendButton:disabled {{
            background-color: {theme["border"]};
            color: {theme["secondary"]};
        }}

        QPushButton#scrollBottomButton {{
            background-color: {theme["accent"]};
            color: white;
            border: none;
            border-radius: 14px;
            padding: 5px 18px;
            font-size: 11px;
            font-weight: 700;
        }}

        QPushButton#scrollBottomButton:hover {{
            background-color: {theme["accent_soft"]};
            border: 1px solid {theme["accent"]};
        }}

        QPushButton#actionButton {{
            background-color: {theme["accent_soft"]};
            color: {theme["accent"]};
            border: 1px solid {theme["border"]};
            border-radius: 6px;
            padding: 9px;
            font-size: 12px;
            font-weight: 600;
        }}

        QPushButton#actionButton:hover {{
            border-color: {theme["accent"]};
        }}

        QPushButton#actionButton:disabled {{
            color: {theme["secondary"]};
        }}

        /* patch_qt70 (2026-09-19, bug real, MEDIDO -- screenshot del
           usuario del panel "Motor de Generacion" con "Probar conexion"/
           "Olvidar key guardada" viendose como cajas VACIAS): el color
           de texto por defecto de secondaryButton era theme["secondary"]
           -- el mismo gris apagado que usan las etiquetas de seccion
           (sectionTitle), pensado para texto secundario/de apoyo, NO
           para el label principal de un boton interactivo -- sobre el
           fondo theme["card"] el contraste era bajo, y a un tamano de
           fuente chico en una pantalla de alta densidad el texto del
           boton quedaba practicamente invisible hasta pasar el mouse
           por encima (:hover si lo subia a theme["text"], pero el
           estado POR DEFECTO -- el que se ve todo el tiempo sin
           interactuar -- es el que importa para poder distinguir que
           dice cada boton). Afecta a TODOS los secondaryButton de la
           app (Probar conexion, Olvidar key guardada, Descargar
           modelo, Anadir/Quitar carpeta, Exportar chat, Exportar
           dataset, engranaje de Ajustes, adjuntar, microfono, TTS), no
           solo a los nuevos de Gemini. Fix: color de texto por defecto
           sube a theme["text"] (el mismo que ya usaba :hover) -- el
           hover sigue aportando el resaltado del borde en
           theme["accent"], pero ya no es la unica forma de leer el
           boton.

           BLINDAJE (2026-09-19, patch_qt71 -- bug real, MEDIDO por
           screenshot del usuario tras patch_qt70: "arregla la interfaz
           ahora, a el estilo que tenia antes" -- toda la UI habia
           quedado con el estilo NATIVO de Windows -- combos, botones y
           la lista de Workspaces en gris plano sin nada del tema
           oscuro/Cyberpunk). Causa raiz: el comentario de arriba se
           escribio originalmente con simbolo numeral al principio de
           cada linea (sintaxis de comentario de PYTHON), pero ese
           bloque entero vive DENTRO del f-string grande que arma todo
           el QSS (ver el return de build_style, cerca de la linea
           2040) -- CSS no reconoce ese simbolo como marcador de
           comentario (eso es Python/shell), asi que esas lineas se
           insertaban como texto LITERAL en medio de la hoja de estilos
           real que se le manda a Qt. Eso rompia el parseo de QSS justo
           ahi, y todo lo que Qt no pudo seguir interpretando despues de
           ese punto (QComboBox, QListWidget#workspacesList, y hasta
           esta misma regla de secondaryButton que se queria arreglar)
           se quedaba sin ningun estilo aplicado, cayendo al render
           nativo del SO -- exactamente lo que se ve en el segundo
           screenshot del usuario. Cambiado a comentario de bloque CSS
           real (slash-asterisco ... asterisco-slash) -- unico formato
           de comentario valido dentro de un QSS de Qt. Leccion para
           futuros patches: cualquier BLINDAJE que se agregue dentro de
           ese f-string grande de build_style tiene que usar ese
           formato de comentario CSS, nunca el simbolo numeral -- fuera
           de ese f-string, el simbolo numeral vuelve a ser un
           comentario de Python normal. */
        QPushButton#secondaryButton {{
            background-color: {theme["card"]};
            color: {theme["text"]};
            border: 1px solid {theme["border"]};
            border-radius: 6px;
            padding: 9px;
            font-size: 12px;
        }}

        QPushButton#secondaryButton:hover {{
            color: {theme["text"]};
            border-color: {theme["accent"]};
        }}

        /* "Detener" (ver stop_button en _create_ui): mismo tamaño/forma
           que secondaryButton, pero con un tinte de "danger" en vez del
           gris neutro - la única acción de la fila de entrada que
           CANCELA algo en curso merece destacarse del resto, sin llegar
           a un rojo sólido que grite demasiado. */
        QPushButton#stopButton {{
            background-color: {_rgba(theme["danger"], 0.14)};
            color: {theme["danger"]};
            border: 1px solid {_rgba(theme["danger"], 0.5)};
            border-radius: 6px;
            padding: 9px;
            font-size: 12px;
            font-weight: 600;
        }}

        QPushButton#stopButton:hover {{
            background-color: {_rgba(theme["danger"], 0.24)};
            border-color: {theme["danger"]};
        }}

        QPushButton#terminalToggleButton {{
            background-color: {theme["code"]};
            color: {theme["success"]};
            border: 1px solid {theme["border"]};
            border-radius: 6px;
            padding: 6px 14px;
            font-size: 11px;
            font-weight: bold;
            font-family: Consolas, "Courier New", monospace;
            outline: none;
        }}

        QPushButton#terminalToggleButton:hover {{
            border: 1px solid {theme["success"]};
            background-color: #141A24;
        }}

        QPushButton#codeActionButton {{
            background-color: {theme["code"]};
            color: {theme["secondary"]};
            border: 1px solid {theme["border"]};
            border-radius: 5px;
            padding: 4px 8px;
            font-size: 10px;
        }}

        QPushButton#codeActionButton:hover {{
            color: {theme["accent"]};
            border-color: {theme["accent"]};
        }}

        /* BLINDAJE (rediseño 2026-09-09 — "el uso de herramientas se ve
        muy tosco"): antes CodeBlockWidget (QFrame#codeBlock) no tenía
        NINGUNA regla propia acá, así que el marco quedaba invisible y
        solo se veía la caja negra de adentro (code_view) pegada al
        fondo de la burbuja, sin agrupación visual clara. Ahora es una
        tarjeta con borde y esquinas redondeadas real. */
        QFrame#codeBlock {{
            background-color: {theme["code"]};
            border: 1px solid {theme["border"]};
            border-radius: 10px;
        }}

        /* ToolResultCard (ver MessageBubble._add_assistant_content y el
        fence `toolresult-{{nombre}}` que arma orchestrator.py): tarjeta
        compacta y distinta del bloque de código — franja de color a la
        izquierda en vez de una caja negra de "TEXT" con botones
        Copiar/Guardar que no aplican a un mensaje de estado. */
        QFrame#toolResultCard {{
            background-color: {theme["card"]};
            border: 1px solid {theme["border"]};
            border-left: 3px solid {theme["accent"]};
            border-radius: 8px;
        }}

        QFrame#toolResultCard[failed="true"] {{
            border-left: 3px solid {theme["danger"]};
        }}

        QPushButton#toolResultCopyButton {{
            background-color: transparent;
            border: none;
            border-radius: 4px;
        }}

        QPushButton#toolResultCopyButton:hover {{
            background-color: {theme["input"]};
        }}

        QPushButton#traceButton {{
            background-color: {theme["code"]};
            color: {theme["secondary"]};
            border: 1px solid {theme["border"]};
            border-radius: 5px;
            padding: 5px 8px;
            font-size: 11px;
            text-align: left;
        }}

        QPushButton#traceButton:hover {{
            color: {theme["text"]};
            border-color: {theme["accent"]};
        }}

        QComboBox {{
            background-color: {theme["input"]};
            color: {theme["text"]};
            border: 1px solid {theme["border"]};
            border-radius: 6px;
            padding: 7px;
            font-size: 12px;
        }}

        QComboBox::drop-down {{
            border: none;
            width: 22px;
        }}

        QComboBox QAbstractItemView {{
            background-color: {theme["card"]};
            color: {theme["text"]};
            selection-background-color: {theme["accent_soft"]};
        }}

        /* QListWidget#workspacesList (panel "Workspaces" del sidebar):
           sin esta regla, Qt le aplicaba la paleta NATIVA del SO (fondo
           blanco, texto negro) — el único widget del sidebar que se
           salía por completo de la estética Cyberpunk/HUD del resto
           (pedido explícito: "que combine con el resto del diseño"). */
        QListWidget#workspacesList {{
            background-color: {theme["input"]};
            color: {theme["text"]};
            border: 1px solid {theme["border"]};
            border-radius: 6px;
            padding: 4px;
            font-size: 11px;
            outline: none;
        }}

        QListWidget#workspacesList::item {{
            padding: 4px 6px;
            border-radius: 4px;
        }}

        QListWidget#workspacesList::item:hover {{
            background-color: {theme["accent_soft"]};
        }}

        QListWidget#workspacesList::item:selected {{
            background-color: {theme["accent_soft"]};
            color: {theme["accent"]};
        }}

        /* QCheckBox#workspaceToolsToggle / QCheckBox#runCmdToggle
           (interruptores "Habilitar herramientas de archivo" y
           "Habilitar ejecución de comandos" del sidebar, pedido
           explícito del usuario 2026-09-15/2026-09-17: "que la opcion
           de habilitar el workspace tenga un diseño como el de la
           interfaz") -- sin esta regla Qt le aplica el checkbox NATIVO
           del SO, el mismo problema que ya tenía QListWidget#workspacesList
           de arriba antes de tener su propia regla. Selector combinado
           (en vez de duplicar el bloque para runCmdToggle) para que los
           dos interruptores se vean SIEMPRE idénticos entre sí, sin
           depender de mantener dos copias sincronizadas a mano cada vez
           que cambie el tema.
           patch_qt64 (2026-09-18): se suma QCheckBox#advancedTerminalToggle
           (toggle "Terminal avanzada" del panel de consola) al mismo
           selector combinado -- mismo motivo, mismo criterio. */
        QCheckBox#workspaceToolsToggle, QCheckBox#runCmdToggle, QCheckBox#advancedTerminalToggle {{
            color: {theme["text"]};
            font-size: 12px;
            spacing: 8px;
            padding: 2px 0px;
        }}

        QCheckBox#workspaceToolsToggle::indicator, QCheckBox#runCmdToggle::indicator, QCheckBox#advancedTerminalToggle::indicator {{
            width: 15px;
            height: 15px;
            border: 1px solid {theme["border"]};
            border-radius: 4px;
            background-color: {theme["input"]};
        }}

        QCheckBox#workspaceToolsToggle::indicator:hover, QCheckBox#runCmdToggle::indicator:hover, QCheckBox#advancedTerminalToggle::indicator:hover {{
            border-color: {theme["accent"]};
        }}

        QCheckBox#workspaceToolsToggle::indicator:checked, QCheckBox#runCmdToggle::indicator:checked, QCheckBox#advancedTerminalToggle::indicator:checked {{
            background-color: {theme["accent"]};
            border: 1px solid {theme["accent"]};
        }}

        QCheckBox#workspaceToolsToggle::indicator:checked:hover, QCheckBox#runCmdToggle::indicator:checked:hover, QCheckBox#advancedTerminalToggle::indicator:checked:hover {{
            background-color: {theme["accent"]};
            border: 1px solid {theme["text"]};
        }}

        QProgressBar {{
            background-color: {theme["card"]};
            border: 1px solid {theme["border"]};
            border-radius: 4px;
            text-align: center;
            color: transparent;
            min-height: 6px;
            max-height: 6px;
        }}

        QProgressBar::chunk {{
            background-color: {theme["accent"]};
            border-radius: 3px;
        }}

        QScrollBar:vertical {{
            background: {theme["sidebar"]};
            width: 8px;
            margin: 0;
            border: none;
        }}

        QScrollBar::handle:vertical {{
            background: {theme["border"]};
            border-radius: 4px;
            min-height: 25px;
        }}

        QScrollBar::handle:vertical:hover {{
            background: {theme["accent"]};
        }}

        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {{
            height: 0px;
        }}

        QDialog {{
            background-color: {theme["card"]};
            color: {theme["text"]};
        }}

        QLineEdit {{
            background-color: {theme["input"]};
            color: {theme["text"]};
            border: 1px solid {theme["border"]};
            border-radius: 6px;
            padding: 8px;
        }}

        QFrame#terminalPanel {{
            background-color: #050505;
            border-top: 1px solid {theme["border"]};
            border-radius: 0px;
        }}

        QLabel#terminalTitle {{
            color: #3DDC97;
            font-size: 11px;
            font-weight: bold;
            font-family: Consolas, "Courier New", monospace;
            letter-spacing: 1px;
        }}

        QLabel#stageMetricsLabel {{
            color: #7FA8A3;
            font-size: 10px;
            font-family: Consolas, "Courier New", monospace;
            padding: 0px 12px 4px 12px;
        }}

        QTextEdit#terminalOutput {{
            background-color: #000000;
            color: #3DDC97;
            border: none;
            font-family: Consolas, "Courier New", monospace;
            font-size: 11px;
            padding: 6px;
        }}

        QPushButton#terminalClearButton {{
            background-color: #0A0A0A;
            color: #6B7280;
            border: 1px solid #1E3A2E;
            border-radius: 5px;
            padding: 4px 8px;
            font-size: 10px;
            font-family: Consolas, "Courier New", monospace;
        }}

        QPushButton#terminalClearButton:hover {{
            color: #3DDC97;
            border-color: #3DDC97;
        }}

        /* ---- Fila de pestañas de chat ----
           Antes: QTabBar + QPushButton "+" sin QSS, o sea con el estilo
           nativo del SO (etiqueta blanca con relieve y una "x" roja
           chillona) — desentonaba con todo el resto de la UI oscura.

           Ahora la fila es una franja de "chrome" del mismo color que la
           barra de cabecera de debajo (`sidebar`), así el bloque
           pestañas + cabecera se lee como una sola pieza. Cada pestaña es
           tipo navegador/editor: la activa es una tarjeta elevada (`card`)
           con un filo superior de acento y esquinas inferiores rectas que
           se funden con la cabecera — sin borde inferior, sin el hueco
           que dejaban las esquinas redondeadas de antes. Las inactivas
           son solo texto tenue, empujado 3 px hacia abajo para que la
           activa "sobresalga". La (x) de cierre (_add_tab_close_button)
           es un botón propio que al pasar el cursor se vuelve un círculo
           rojo translúcido (_rgba(danger)) en vez del bloque rojo sólido
           de antes; el "+" pierde su caja y queda como un glifo limpio
           que solo se tiñe de acento al hover. */
        QFrame#chatTabBarContainer {{
            background-color: {theme["sidebar"]};
            border-bottom: 1px solid {theme["border"]};
        }}

        QTabBar#chatTabBar {{
            background: transparent;
            border: none;
        }}

        QTabBar#chatTabBar::tab {{
            background: transparent;
            color: {theme["secondary"]};
            border: 1px solid transparent;
            border-top: 2px solid transparent;
            border-top-left-radius: 10px;
            border-top-right-radius: 10px;
            border-bottom-left-radius: 0;
            border-bottom-right-radius: 0;
            padding: 8px 10px 9px 15px;
            margin-top: 3px;
            margin-right: 2px;
            min-width: 118px;
            max-width: 240px;
            font-size: 12px;
            font-weight: 600;
        }}

        QTabBar#chatTabBar::tab:!selected:hover {{
            background: {theme["card"]};
            color: {theme["text"]};
        }}

        QTabBar#chatTabBar::tab:selected {{
            background: {theme["card"]};
            color: {theme["text"]};
            border-color: {theme["border"]};
            border-top: 2px solid {theme["accent"]};
            margin-top: 0;
            padding-top: 10px;
        }}

        QTabBar#chatTabBar QToolButton {{
            background: {theme["card"]};
            border: 1px solid {theme["border"]};
            border-radius: 6px;
            margin: 4px 1px;
        }}

        QTabBar#chatTabBar QToolButton:hover {{
            border-color: {theme["accent"]};
        }}

        QPushButton#tabCloseButton {{
            background: transparent;
            color: {theme["secondary"]};
            border: none;
            border-radius: 9px;
            font-size: 13px;
            font-weight: bold;
            padding: 0;
            margin: 0;
        }}

        QPushButton#tabCloseButton:hover {{
            background: {_rgba(theme["danger"], 0.18)};
            color: {theme["danger"]};
        }}

        QPushButton#tabCloseButton:pressed {{
            background: {_rgba(theme["danger"], 0.34)};
        }}

        QPushButton#newTabButton {{
            background: transparent;
            color: {theme["secondary"]};
            border: 1px solid transparent;
            border-radius: 9px;
            font-size: 18px;
            font-weight: 400;
            padding-bottom: 2px;
        }}

        QPushButton#newTabButton:hover {{
            background: {theme["accent_soft"]};
            color: {theme["accent"]};
        }}

        QPushButton#newTabButton:pressed {{
            background: {theme["card"]};
            color: {theme["text"]};
        }}
    """


class _SpinnerWidget(QWidget):
    """
    Spinner circular minimalista — un arco que gira continuamente,
    dibujado a mano con QPainter en cada frame. Sin depender de ningún
    asset externo (gif/svg animado): solo un QTimer que hace avanzar el
    ángulo y pide un repintado, ~60 veces por segundo.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        diameter: int = 15,
        color: str = "#58A6FF",
    ) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self._angle = 0
        self._should_spin = False
        self.setFixedSize(diameter, diameter)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        self._angle = (self._angle + 6) % 360
        self.update()

    def start(self) -> None:
        self._should_spin = True
        if self.isVisible():
            self._timer.start()

    def stop(self) -> None:
        self._should_spin = False
        if hasattr(self, "_timer") and self._timer.isActive():
            self._timer.stop()

    def hideEvent(self, event) -> None:
        if hasattr(self, "_timer") and self._timer.isActive():
            self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        if self._should_spin and not self._timer.isActive():
            self._timer.start()
        super().showEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pen_width = max(2, self.width() // 7)
        rect = self.rect().adjusted(pen_width, pen_width, -pen_width, -pen_width)

        track_pen = QPen(QColor(self._color.red(), self._color.green(), self._color.blue(), 35))
        track_pen.setWidth(pen_width)
        track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.drawEllipse(rect)

        arc_pen = QPen(self._color)
        arc_pen.setWidth(pen_width)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc_pen)
        span = 100 * 16
        start = -self._angle * 16
        painter.drawArc(rect, start, span)


class ThinkingWidget(QWidget):
    """
    Indicador de estado en vivo ("buscando en internet...", "generando
    respuesta...") mientras se procesa un turno.

    Rediseño: se quitó por completo el marco/tarjeta (fondo + borde)
    que tenía antes — queda como texto flotando directamente en el
    flujo del chat, más parecido a un indicador de "escribiendo..."
    minimalista que a una notificación en una caja. El emoji estático
    que rotaba por intención (🔍/⚡/🧠...) se reemplazó por un spinner
    circular animado a la izquierda: transmite "en progreso" de forma
    más clara y consistente que cambiar de ícono cada vez. El texto
    conserva un pulso sutil de opacidad (antes el widget ENTERO
    parpadeaba entre 30-100%; ahora solo el texto, y en un rango más
    suave, para que se lea como "vivo" sin resultar chicloso).
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("thinkingWidget")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 6, 4, 6)
        layout.setSpacing(8)

        self.spinner = _SpinnerWidget(self, diameter=15, color="#58A6FF")

        self.text_label = QLabel("Razonando respuesta...")
        self.text_label.setStyleSheet(
            "color: #8B92A5; font-size: 12px; font-weight: 600; "
            "background: transparent; border: none;"
        )

        layout.addWidget(self.spinner)
        layout.addWidget(self.text_label)
        layout.addStretch()

        self.opacity_effect = QGraphicsOpacityEffect(self.text_label)
        self.text_label.setGraphicsEffect(self.opacity_effect)

        self.timeline = QTimeLine(1400, self)
        self.timeline.setFrameRange(55, 100)
        self.timeline.setLoopCount(0)
        self.timeline.frameChanged.connect(
            lambda f: self.opacity_effect.setOpacity(f / 100.0)
        )
        self.timeline.start()

        self.spinner.start()

        self._current_message = "Razonando respuesta..."
        self._start_time = time.monotonic()
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self._tick_elapsed)
        self.elapsed_timer.start()

    def _tick_elapsed(self) -> None:
        elapsed = int(time.monotonic() - self._start_time)
        self.text_label.setText(f"{self._current_message} ({elapsed}s)")

    def set_intent(self, icon: str, message: str) -> None:
        """
        Actualiza el mensaje de intención dinámicamente. `icon` se
        recibe por compatibilidad con las señales existentes
        (StreamTurnWorker sigue emitiendo un emoji por intención) pero
        ya no se muestra: el spinner reemplaza esa función visual, así
        que no hace falta tocar ningún llamador existente.

        El contador de segundos NO se reinicia por fase — mide el
        tiempo total del turno, no el de la fase actual: mezclar ambas
        semánticas (a veces "desde que arrancó el turno", a veces "desde
        que cambió de fase") confundiría más de lo que aclara.
        """
        self._current_message = message
        self._tick_elapsed()

    def stop(self) -> None:
        if hasattr(self, "timeline") and self.timeline:
            self.timeline.stop()
        if hasattr(self, "spinner") and self.spinner:
            self.spinner.stop()
        if hasattr(self, "elapsed_timer") and self.elapsed_timer:
            self.elapsed_timer.stop()


def _cover_fit_pixmap(pixmap: QPixmap, target_w: int, target_h: int) -> QPixmap:
    """
    Escala un QPixmap al estilo CSS `object-fit: cover`: llena
    exactamente el recuadro (target_w x target_h) recortando el sobrante
    centrado, sin deformar la imagen ni dejar bandas vacías.
    """
    if pixmap.isNull() or target_w <= 0 or target_h <= 0:
        return pixmap
    scaled = pixmap.scaled(
        target_w, target_h,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = max(0, (scaled.width() - target_w) // 2)
    y = max(0, (scaled.height() - target_h) // 2)
    return scaled.copy(x, y, target_w, target_h)


def _apply_bottom_gradient(pixmap: QPixmap, height_fraction: float = 0.7) -> QPixmap:
    """
    Compone un degradado oscuro en la base de un QPixmap (transparente
    arriba, negro semi-opaco abajo) para que el texto blanco superpuesto
    (Módulo 3 — tarjetas editoriales con texto sobre la imagen) siga
    siendo legible sin importar el brillo de la foto de fondo.
    """
    if pixmap.isNull():
        return pixmap
    result = QPixmap(pixmap)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    h = result.height()
    gradient_top = h * (1 - height_fraction)
    gradient = QLinearGradient(0, gradient_top, 0, h)
    gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
    gradient.setColorAt(1.0, QColor(0, 0, 0, 210))
    painter.fillRect(0, int(gradient_top), result.width(), int(h - gradient_top) + 1, gradient)
    painter.end()
    return result


class _ThumbnailLoader(QThread):
    """Descarga en segundo plano los bytes de una miniatura para no bloquear
    la UI mientras la tarjeta de resultados web se renderiza.

    Además de descargar, valida la miniatura ANTES de entregarla al hilo
    GUI: rechaza imágenes corruptas y cualquiera por debajo del mínimo de
    calidad, para que `_OverlayImageCard` nunca reciba una imagen que
    tendría que estirar (mala UX) — en su lugar, mantiene el placeholder
    limpio.

    Los umbrales de mínimo NO son globales: se reciben por instancia
    porque este loader sirve tanto a la tarjeta "hero" (grande, 150px de
    alto) como a las sub-tarjetas (90px de alto). Usar un único mínimo
    global de 150px para ambas rechazaba sistemáticamente miniaturas
    perfectamente válidas para el slot de 90px — de hecho, rechazaba
    prácticamente TODO lo que no fuera una foto de portada de artículo,
    incluidos los favicons de respaldo (`_favicon_url()` en
    web_search.py), que son intencionalmente pequeños y son la única
    imagen disponible para muchas fuentes.
    """

    DEFAULT_MIN_WIDTH = 300
    DEFAULT_MIN_HEIGHT = 150
    MAX_DIMENSION = 6000

    _CACHE: Dict[Tuple[str, int, int], bytes] = {}
    _CACHE_LOCK = threading.Lock()
    _CACHE_MAX_ENTRIES = 200

    loaded = pyqtSignal(bytes)

    def __init__(
        self,
        url: str,
        min_width: int = DEFAULT_MIN_WIDTH,
        min_height: int = DEFAULT_MIN_HEIGHT,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._min_width = max(1, min_width)
        self._min_height = max(1, min_height)

    def run(self) -> None:
        cache_key = (self._url, self._min_width, self._min_height)

        with self._CACHE_LOCK:
            cached = self._CACHE.get(cache_key)
        if cached is not None:
            self.loaded.emit(cached)
            return

        data = b""
        if self._url:
            try:
                req = urllib.request.Request(
                    self._url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SovNode/2.0"},
                )
                with urllib.request.urlopen(req, timeout=4) as resp:
                    data = resp.read()
            except Exception:
                data = b""

        validated = self._validate_dimensions(data)

        with self._CACHE_LOCK:
            if len(self._CACHE) >= self._CACHE_MAX_ENTRIES:
                evict_count = max(1, len(self._CACHE) // 5)
                for stale_key in list(self._CACHE.keys())[:evict_count]:
                    del self._CACHE[stale_key]
            self._CACHE[cache_key] = validated

        self.loaded.emit(validated)

    def _validate_dimensions(self, data: bytes) -> bytes:
        """
        Verifica ancho/alto de la imagen contra el mínimo de ESTA
        instancia (`self._min_width` / `self._min_height`, propio del
        slot que la va a mostrar) sin decodificar los píxeles completos
        y sin tocar QPixmap fuera del hilo GUI.

        Por qué QImageReader y no QPixmap aquí: Qt documenta que QPixmap
        depende del backend de pintura de la plataforma y su uso fuera
        del hilo principal no está garantizado en todas las plataformas;
        QImage/QImageReader, en cambio, son explícitamente seguros de
        usar en cualquier hilo porque no tocan la GUI. Además,
        QImageReader.size() lee solo la cabecera del formato (JPEG/PNG/
        WebP...) en la gran mayoría de los casos, así que rechazar una
        imagen corrupta o demasiado pequeña/grande no paga el costo de
        decodificar el bitmap completo — la decodificación real
        (QPixmap.loadFromData) sigue ocurriendo, como antes, en
        `_OverlayImageCard._on_image_loaded()` en el hilo GUI, solo que
        ahora únicamente para imágenes que ya sabemos que valen la pena.
        """
        if not data:
            return b""

        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            return b""

        try:
            reader = QImageReader(buffer)
            reader.setAutoTransform(True)

            if not reader.canRead():
                return b""

            size = reader.size()
            if not size.isValid() or size.width() <= 0 or size.height() <= 0:
                return b""

            if size.width() < self._min_width or size.height() < self._min_height:
                return b""

            if size.width() > self.MAX_DIMENSION or size.height() > self.MAX_DIMENSION:
                return b""

            return data
        except Exception:
            return b""
        finally:
            buffer.close()


class _OverlayImageCard(QFrame):
    """
    Tarjeta editorial (Módulo 3): imagen a sangre completa con degradado
    oscuro en la base y texto BLANCO SUPERPUESTO directamente sobre la
    imagen — crédito de fuente arriba en pequeño, título grande y
    subtítulo abajo — en vez del formato anterior de miniatura-arriba/
    texto-abajo. El fondo (imagen compuesta + degradado, o color plano de
    respaldo con iniciales del dominio si no hay miniatura) se pinta en
    `paintEvent`; el texto vive en widgets hijos normales sobre ese fondo.
    """

    def __init__(
        self, source: dict, is_hero: bool, min_height: int, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._is_hero = is_hero
        self._radius = 16 if is_hero else 12
        self._raw_pixmap: Optional[QPixmap] = None
        self._composited_pixmap: Optional[QPixmap] = None
        self._domain = str(source.get("domain") or "").strip()
        self._loader: Optional[_ThumbnailLoader] = None

        self.setMinimumHeight(min_height)
        self.setStyleSheet("background: transparent; border: none;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(2)

        credit_text = self._domain.upper()
        self._credit_label = QLabel(credit_text)
        self._credit_label.setStyleSheet(
            "color: rgba(255,255,255,190); font-size: 9px; font-weight: 800; "
            "letter-spacing: 0.5px; background: transparent; border: none;"
        )
        layout.addWidget(self._credit_label)

        layout.addStretch(1)

        self._title_full_text = str(source.get("title") or "").strip()
        self._title_label = QLabel(self._title_full_text)
        self._title_label.setWordWrap(True)
        self._title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._title_label.setMinimumWidth(0)
        self._title_label.setStyleSheet(
            f"color: #FFFFFF; font-size: {21 if is_hero else 13}px; font-weight: 800; "
            "background: transparent; border: none;"
        )
        self._title_label.setToolTip(self._title_full_text)
        layout.addWidget(self._title_label)

        subtitle_text = str(source.get("date") or "").strip()
        self._subtitle_label = QLabel(subtitle_text)
        self._subtitle_label.setWordWrap(True)
        self._subtitle_label.setStyleSheet(
            f"color: rgba(255,255,255,210); font-size: {12 if is_hero else 10}px; "
            "font-weight: 600; background: transparent; border: none;"
        )
        self._subtitle_label.setVisible(bool(subtitle_text))
        layout.addWidget(self._subtitle_label)

        thumbnail_url = str(source.get("thumbnail") or "")
        if thumbnail_url:
            min_width = 240 if is_hero else 150
            self._loader = _ThumbnailLoader(
                thumbnail_url, min_width=min_width, min_height=min_height, parent=self
            )
            self._loader.loaded.connect(self._on_image_loaded)
            self._loader.start()

    def _placeholder_letters(self) -> str:
        """Iniciales del dominio (p. ej. "youtube.com" -> "YO") o el icono
        de globo genérico si el dominio no es utilizable — respaldo cuando
        no hay miniatura, se rechazó por calidad, o falló la descarga."""
        letters = re.sub(r"[^A-Za-z]", "", self._domain.split(".")[0] if self._domain else "")
        return letters[:2].upper() if len(letters) >= 2 else "🌐"

    def _on_image_loaded(self, data: bytes) -> None:
        if not data:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data) and not pixmap.isNull():
            self._raw_pixmap = pixmap
            self._apply_image()

    def _apply_image(self) -> None:
        if self._raw_pixmap is None:
            return
        w, h = max(1, self.width()), max(1, self.height())
        fitted = _cover_fit_pixmap(self._raw_pixmap, w, h)
        self._composited_pixmap = _apply_bottom_gradient(fitted)
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_image()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_image()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), self._radius, self._radius)
        painter.setClipPath(path)

        if self._composited_pixmap is not None:
            painter.drawPixmap(0, 0, self._composited_pixmap)
        else:
            painter.fillRect(self.rect(), QColor("#161B22" if self._is_hero else "#12161D"))
            painter.setPen(QColor("#333B47"))
            font = painter.font()
            font.setPointSize(28 if self._is_hero else 18)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder_letters())

        painter.end()
        super().paintEvent(event)


def _elide_at_word_boundary(text: str, max_len: int = 45) -> str:
    """
    Recorta `text` a lo sumo a `max_len` caracteres sin partir una palabra
    a la mitad: retrocede hasta el último espacio dentro del límite y
    añade puntos suspensivos elegantes ("…", no tres puntos sueltos) solo
    cuando de verdad se recortó algo.
    """
    text = (text or "").strip()
    if len(text) <= max_len:
        return text

    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]

    return truncated.rstrip(" ,.;:-") + "…"


class WebSearchResultsWidget(QFrame):
    """
    Dashboard visual de fuentes web (Módulo 3) — sin texto plano ni
    snippets: la investigación ya la explica el modelo en el texto que
    fluye justo debajo. Layout editorial fiel a la referencia:

    - Hero: 1 tarjeta grande vertical a la IZQUIERDA — imagen a sangre
      completa, degradado oscuro en la base, crédito de fuente arriba en
      pequeño, título grande blanco y subtítulo (fecha, si la fuente la
      trae) abajo.
    - Sub-tarjetas: hasta 2 tarjetas más chicas a la DERECHA, apiladas
      verticalmente, mismo tratamiento de imagen+degradado+texto en menor
      escala. Su alto combinado (+ el espacio entre ellas) iguala el alto
      del hero, porque viven en una columna de igual altura fija.

    Nota de datos: la referencia de diseño muestra un crédito fotográfico
    ("SOPA Images/LightRocket...") y un subtítulo estructurado
    ("Centrocampista · Real Madrid") que vienen de un enriquecimiento de
    entidades que esta app no hace. Aquí el crédito es el dominio de la
    fuente y el subtítulo es la fecha de publicación si está disponible
    — el layout visual es fiel a la referencia, el contenido es el que
    realmente se tiene, nunca inventado.
    """

    HERO_HEIGHT = 280
    SUB_GAP = 8
    SUB_HEIGHT = (HERO_HEIGHT - SUB_GAP) // 2

    def __init__(
        self,
        data: dict,
        parent: Optional[QWidget] = None,
        lang: str = "Español",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("webCard")
        self._lang = lang if lang in I18N else "Español"

        success = bool(data.get("success"))
        sources = data.get("sources", []) or []

        if not success or not sources:
            self._build_failure_state(data)
            return

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 4, 16, 4)
        main_layout.setSpacing(8)

        content_row = QHBoxLayout()
        content_row.setSpacing(self.SUB_GAP)

        hero_card = _OverlayImageCard(sources[0], is_hero=True, min_height=self.HERO_HEIGHT)
        content_row.addWidget(hero_card, 1)

        remaining_sources = sources[1:3]
        if remaining_sources:
            sub_column = QVBoxLayout()
            sub_column.setSpacing(self.SUB_GAP)
            for source in remaining_sources:
                sub_column.addWidget(
                    _OverlayImageCard(source, is_hero=False, min_height=self.SUB_HEIGHT)
                )
            if len(remaining_sources) < 2:
                sub_column.addStretch(1)
            content_row.addLayout(sub_column, 1)

        main_layout.addLayout(content_row)

    def set_available_width(self, width: int) -> None:
        """
        Ancla el ancho máximo de la tarjeta al viewport visible, igual que
        `MessageBubble.set_available_width`. Sin esto, la tarjeta queda
        libre para pedir tanto ancho como su contenido más ancho requiera
        y se sale del área visible en vez de quedarse fija a la izquierda.
        """
        self.setMaximumWidth(max(220, int(width)))

    def _build_failure_state(self, data: dict) -> None:
        self.setStyleSheet("""
            QFrame#webCard {
                background-color: #2A2410;
                border: 1px solid #F2C14E;
                border-left: 4px solid #F2C14E;
                border-radius: 10px;
            }
            QLabel { color: #F2E3B8; font-size: 11px; }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(10)

        icon = QLabel("⚠️")
        text = QLabel(
            I18N[self._lang]["web_card_no_data"].format(data.get("status_message", ""))
        )
        text.setWordWrap(True)

        layout.addWidget(icon)
        layout.addWidget(text, 1)


_TTS_ES_SPAIN_VOICE_HINTS = ("helena", "laura", "pablo", "spain", "españa", "espana", "es-es", "es_es")
_TTS_ES_ANY_VOICE_HINTS = ("sabina", "raul", "raúl", "spanish", "español", "espanol")
_TTS_EN_VOICE_HINTS = ("zira", "david", "mark", "james", "susan", "hazel", "english")


def _select_tts_voice_id(voices, target_is_es: bool) -> Optional[str]:
    """
    Elige el `voice.id` de pyttsx3/SAPI5 más adecuado para el idioma.

    BLINDAJE (bug real): la versión anterior comparaba subcadenas de DOS
    letras sueltas ("es"/"en") contra el nombre/id completo de la voz.
    Pero "Desktop" — presente en el nombre de CASI TODAS las voces SAPI5
    de Windows, p. ej. "Microsoft David Desktop" — CONTIENE la subcadena
    "es" ("D-ES-ktop"); y "Helena" CONTIENE la subcadena "en"
    ("H-el-EN-a"). Resultado: la rama "español" podía terminar
    seleccionando la primera voz EN INGLÉS de la lista (por tener
    "Desktop" en el nombre) y viceversa — una coincidencia de texto sin
    relación con el idioma real de la voz. Ahora solo se compara contra
    listas de nombres/palabras COMPLETAS conocidas, nunca fragmentos de
    2 letras.

    Para español, prueba primero las voces con acento de España
    (_TTS_ES_SPAIN_VOICE_HINTS) y solo si no hay ninguna instalada cae a
    cualquier voz en español de otra región (_TTS_ES_ANY_VOICE_HINTS) —
    mejor esa que terminar hablando en inglés. Devuelve None si no
    encuentra ninguna coincidencia (el llamador conserva la voz default
    del motor).
    """
    if not voices:
        return None
    hint_groups = (
        [_TTS_ES_SPAIN_VOICE_HINTS, _TTS_ES_ANY_VOICE_HINTS] if target_is_es
        else [_TTS_EN_VOICE_HINTS]
    )
    for hints in hint_groups:
        for voice in voices:
            v_name = (getattr(voice, "name", "") or "").lower()
            v_id = (getattr(voice, "id", "") or "").lower()
            if any(hint in v_name or hint in v_id for hint in hints):
                return voice.id
    return None


class TTSWorker(QThread):
    """Hilo secundario que sintetiza texto a voz y permite detención inmediata en Windows."""

    finished_speech = pyqtSignal()

    def __init__(self, text: str, lang: str = "Español") -> None:
        super().__init__()
        self.text = text
        self.lang = lang
        self._engine = None

    def run(self) -> None:
        clean_text = re.sub(r'[*_#`\-\[\]\(\)]', '', self.text).strip()
        if not clean_text:
            self.finished_speech.emit()
            return

        try:
            import pythoncom
            pythoncom.CoInitialize()
        except ImportError:
            pass

        try:
            import pyttsx3

            self._engine = pyttsx3.init()
            self._engine.setProperty('rate', 165)

            voices = self._engine.getProperty('voices')
            target_is_es = "es" in self.lang.lower() or "spa" in self.lang.lower()

            selected_voice_id = _select_tts_voice_id(voices, target_is_es)
            if selected_voice_id:
                self._engine.setProperty('voice', selected_voice_id)

            self._engine.say(clean_text)
            self._engine.runAndWait()
        except Exception as exc:
            print(f"[TTS Error]: {exc}")
        finally:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except ImportError:
                pass

        self.finished_speech.emit()

    def stop(self) -> None:
        """Fuerza la parada inmediata del motor de voz."""
        if self._engine:
            try:
                self._engine.stop()
            except Exception:
                pass
        self.terminate()


class PromptTextEdit(QTextEdit):
    send_requested = pyqtSignal()
    file_dropped = pyqtSignal(str)
    text_file_attached = pyqtSignal(str)
    image_pasted = pyqtSignal(QImage)
    image_path_pasted = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.textChanged.connect(self._adjust_height)
        self.setMinimumHeight(50)
        self.setMaximumHeight(150)
        self._adjust_height()

    def _adjust_height(self) -> None:
        """Ajusta la altura dinámicamente en función del contenido del documento."""
        doc_height = self.document().size().height()
        new_height = max(50, min(150, int(doc_height) + 20))
        self.setFixedHeight(new_height)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                event.accept()
                self.send_requested.emit()
            return
        if (
            event.key() == Qt.Key.Key_V
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            clipboard = QApplication.clipboard()
            mime = clipboard.mimeData()
            if mime is not None and mime.hasImage():
                image = clipboard.image()
                if not image.isNull():
                    event.accept()
                    self.image_pasted.emit(image)
                    return
            if mime is not None and mime.hasUrls():
                _image_paths = [
                    url.toLocalFile()
                    for url in mime.urls()
                    if url.isLocalFile()
                    and Path(url.toLocalFile()).suffix.lower()
                    in SUPPORTED_IMAGE_DROP_EXTENSIONS
                ]
                if _image_paths:
                    event.accept()
                    self.image_path_pasted.emit(_image_paths[0])
                    return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            valid = any(
                url.isLocalFile()
                and Path(url.toLocalFile()).suffix.lower()
                in (SUPPORTED_DROP_EXTENSIONS | SUPPORTED_IMAGE_DROP_EXTENSIONS)
                for url in event.mimeData().urls()
            )
            if valid:
                event.acceptProposedAction()
                return
        super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasUrls():
            super().dropEvent(event)
            return

        inserted = False
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            file_path = Path(url.toLocalFile())
            suffix = file_path.suffix.lower()

            if suffix in SUPPORTED_IMAGE_DROP_EXTENSIONS:
                self.image_path_pasted.emit(str(file_path))
                inserted = True
                continue

            if suffix not in SUPPORTED_DROP_EXTENSIONS:
                continue
            # BLINDAJE (pedido explícito del usuario, 2026-09-15 --
            # screenshot de un bloque de código gigante volcado como
            # texto crudo en el chat al soltar un .py): ya NO se lee ni
            # se inserta el contenido acá -- `text_file_attached` dispara
            # el chip (`MainWindow._attach_text_file_from_path`, que hace
            # su propia lectura del archivo). `file_dropped` se mantiene
            # intacto para la indexación en RAG (_on_file_dropped_for_
            # indexing), que no depende de este campo de texto.
            self.text_file_attached.emit(str(file_path))
            self.file_dropped.emit(str(file_path))
            inserted = True

        if inserted:
            event.acceptProposedAction()
        else:
            event.ignore()


class SmartChatScrollArea(ChatDropArea):
    """
    Extiende ChatDropArea desactivando el scroll horizontal para evitar desplazamientos
    y manteniendo el seguimiento inteligente de scroll vertical.
    """

    pinned_state_changed = pyqtSignal(bool)
    PIN_THRESHOLD_PX = 60

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pinned_to_bottom = True
        
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        self.verticalScrollBar().sliderMoved.connect(
            lambda _value: self._update_pinned_state()
        )
        self.verticalScrollBar().actionTriggered.connect(
            lambda _action: QTimer.singleShot(0, self._update_pinned_state)
        )

    def _update_pinned_state(self) -> None:
        scrollbar = self.verticalScrollBar()
        new_state = (scrollbar.maximum() - scrollbar.value()) <= self.PIN_THRESHOLD_PX
        if new_state != self._pinned_to_bottom:
            self._pinned_to_bottom = new_state
            self.pinned_state_changed.emit(new_state)

    @property
    def is_pinned_to_bottom(self) -> bool:
        return self._pinned_to_bottom

    def pin_to_bottom(self) -> None:
        if not self._pinned_to_bottom:
            self._pinned_to_bottom = True
            self.pinned_state_changed.emit(True)

    def wheelEvent(self, event) -> None:
        super().wheelEvent(event)
        self._update_pinned_state()

    def keyPressEvent(self, event) -> None:
        super().keyPressEvent(event)
        if event.key() in (
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
            Qt.Key.Key_PageUp,
            Qt.Key.Key_PageDown,
            Qt.Key.Key_Home,
            Qt.Key.Key_End,
        ):
            self._update_pinned_state()


_WHISPER_MODEL_SIZE_ENV_VAR = "SOVNODE_WHISPER_MODEL"
_DEFAULT_WHISPER_MODEL_SIZE = "base"
_WHISPER_MODEL_CACHE: Dict[str, Any] = {}
_WHISPER_MODEL_LOCK = threading.Lock()


def _get_whisper_model_size() -> str:
    return (os.environ.get(_WHISPER_MODEL_SIZE_ENV_VAR) or _DEFAULT_WHISPER_MODEL_SIZE).strip()


def _get_cached_whisper_model():
    """
    Devuelve la instancia de `WhisperModel` cacheada para el tamaño
    configurado, cargándola en el primer llamado bajo `_WHISPER_MODEL_
    LOCK` (una grabación real no puede empezar mientras la anterior
    sigue transcribiendo — el botón de mic queda deshabilitado durante
    ese tramo, ver `_on_transcription_ready` — pero el warm-up en
    segundo plano SÍ puede solaparse con el primer click real si el
    usuario es rápido, de ahí el lock). Deja pasar cualquier excepción
    de import/carga tal cual — el caller (`VoiceRecorderWorker.run` /
    `_warm_up_whisper_model`) decide cómo reportarla; nunca la absorbe
    en silencio acá, porque un fallo de carga real (paquete faltante,
    modelo corrupto) tiene que ser visible, no un STT que nunca
    transcribe nada sin ninguna pista de por qué.
    """
    from faster_whisper import WhisperModel

    size = _get_whisper_model_size()
    cached = _WHISPER_MODEL_CACHE.get(size)
    if cached is not None:
        return cached
    with _WHISPER_MODEL_LOCK:
        cached = _WHISPER_MODEL_CACHE.get(size)
        if cached is not None:
            return cached
        model = WhisperModel(size, device="cpu", compute_type="int8")
        _WHISPER_MODEL_CACHE[size] = model
        return model


class VoiceRecorderWorker(QThread):
    transcription_ready = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, language: str = "es") -> None:
        super().__init__()
        self._is_recording = False
        self._audio_data = []
        self._language = language

    def start_recording(self) -> None:
        self._is_recording = True
        self.start()

    def stop_recording(self) -> None:
        self._is_recording = False

    def run(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd

            sample_rate = 16000
            self._audio_data = []

            def callback(indata, frames, time, status):
                if self._is_recording:
                    self._audio_data.append(indata.copy())

            with sd.InputStream(
                samplerate=sample_rate, channels=1, callback=callback
            ):
                while self._is_recording:
                    self.msleep(100)

            if not self._audio_data:
                return

            audio_np = np.concatenate(self._audio_data, axis=0)

            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False
            ) as tmp_file:
                tmp_filename = tmp_file.name

            scaled_audio = (audio_np * 32767).astype(np.int16)
            with wave.open(tmp_filename, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(scaled_audio.tobytes())

            model = _get_cached_whisper_model()
            segments, _ = model.transcribe(
                tmp_filename,
                language=self._language,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                condition_on_previous_text=False,
            )
            transcription = "".join([segment.text for segment in segments]).strip()

            os.remove(tmp_filename)
            self.transcription_ready.emit(transcription)

        except Exception as exc:
            self.error_occurred.emit(f"Error STT: {exc}")


@dataclass
class ChatEntry:
    sender: str
    content: str
    timestamp: str
    trace: Optional[Any] = None
    is_error: bool = False


class _ThoughtStreamFilter:
    """
    Filtra en streaming, token a token, bloques de razonamiento —
    reconoce tanto la variante angular (<thought>/<thought_code>) como la
    de corchetes ([thought]/[thought_code]) que algunos modelos de 3B
    emiten en su lugar. Sin esto, un bloque en la variante no reconocida
    se colaba tal cual en la vista principal del chat mientras se
    streameaba en vivo, aunque el post-proceso (_split_thought_and_content)
    lo filtrara correctamente más tarde en las pasadas de corrección.
    """

    _DEFAULT_TAG_PAIRS: Tuple[Tuple[str, str], ...] = (
        ("<thought>", "</thought>"),
        ("[thought]", "[/thought]"),
    )
    THOUGHT_CODE_TAG_PAIRS: Tuple[Tuple[str, str], ...] = (
        ("<thought_code>", "</thought_code>"),
        ("[thought_code]", "[/thought_code]"),
    )

    def __init__(self, tag_pairs: Optional[Tuple[Tuple[str, str], ...]] = None) -> None:
        self._tag_pairs = tag_pairs or self._DEFAULT_TAG_PAIRS
        self._buffer = ""
        self._inside_thought = False
        self._active_close_tag = ""

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""

        self._buffer += chunk
        visible_parts: List[str] = []

        while True:
            if not self._inside_thought:
                open_idx, open_tag, close_tag = self._find_earliest_open_tag()
                if open_idx == -1:
                    safe_len = self._safe_emit_length(self._buffer)
                    visible_parts.append(self._buffer[:safe_len])
                    self._buffer = self._buffer[safe_len:]
                    break
                visible_parts.append(self._buffer[:open_idx])
                self._buffer = self._buffer[open_idx + len(open_tag):]
                self._inside_thought = True
                self._active_close_tag = close_tag
            else:
                close_idx = self._buffer.lower().find(self._active_close_tag.lower())
                if close_idx == -1:
                    break
                self._buffer = self._buffer[close_idx + len(self._active_close_tag):]
                self._inside_thought = False
                self._active_close_tag = ""

        return "".join(visible_parts)

    def flush(self) -> str:
        """Libera cualquier texto pendiente retenido en el búfer al finalizar el streaming."""
        if not self._inside_thought and self._buffer:
            remaining = self._buffer
            self._buffer = ""
            return remaining
        return ""

    def _find_earliest_open_tag(self) -> Tuple[int, str, str]:
        """Busca, entre todas las variantes de etiqueta de apertura conocidas, la que aparece primero en el búfer."""
        buf_lower = self._buffer.lower()
        best_idx = -1
        best_open_tag = ""
        best_close_tag = ""
        for open_tag, close_tag in self._tag_pairs:
            idx = buf_lower.find(open_tag.lower())
            if idx != -1 and (best_idx == -1 or idx < best_idx):
                best_idx = idx
                best_open_tag = open_tag
                best_close_tag = close_tag
        return best_idx, best_open_tag, best_close_tag

    def _safe_emit_length(self, buf: str) -> int:
        max_tag_len = max(len(open_tag) for open_tag, _ in self._tag_pairs)
        max_check = min(max_tag_len - 1, len(buf))
        buf_lower = buf.lower()
        for i in range(max_check, 0, -1):
            suffix = buf_lower[-i:]
            if any(open_tag.lower().startswith(suffix) for open_tag, _ in self._tag_pairs):
                return len(buf) - i
        return len(buf)


# Pool compartido y persistente para despachar búsquedas web en segundo
# plano desde StreamTurnWorker.run() - antes _submit_web_search() creaba
# un ThreadPoolExecutor(max_workers=1) nuevo en cada turno con búsqueda
# web, pagando el costo de creación/destrucción de hilos por turno en vez
# de reutilizar un pool ya caliente. Seguro de compartir: el blindaje de
# timeouts existente (WEB_SEARCH_HARD_BUDGET_SECONDS aquí y el techo duro
# de respaldo en web_search.py) garantiza que ningún worker queda
# atascado para siempre.
_WEB_SEARCH_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="StreamWebSearch")

# BLINDAJE (2026-09-20, patch_qt74 -- limpieza encontrada en la auditoría
# previa a subir el proyecto a GitHub): había DOS definiciones seguidas
# de `_CancelToken`, una pegada arriba de la otra -- la segunda (con
# docstring y `bool(...)` explícito en `is_set`) pisaba en silencio a la
# primera (sin docstring, sin el `bool()`), así que la primera nunca se
# usaba de verdad (pyflakes: "redefinition of unused '_CancelToken'").
# Comportamiento idéntico en la práctica (`check_fn()` en este código ya
# siempre devuelve un bool real), pero es código muerto duplicado sin
# ninguna razón para existir -- se deja solo la versión con docstring.
class _CancelToken:
    """Adaptador simple: expone `.is_set()` sobre una función de chequeo arbitraria."""

    def __init__(self, check_fn) -> None:
        self._check_fn = check_fn

    def is_set(self) -> bool:
        return bool(self._check_fn())


class StreamTurnWorker(QThread):
    """
    Puente delgado entre la UI de Qt y `Orchestrator.run_turn()`.

    CORRECCIÓN APLICADA: esta clase antes tenía DOS definiciones de
    `__init__`/`run` mezcladas en el mismo cuerpo (una delegando
    correctamente a `run_turn()`, otra reimplementando manualmente todo
    el pipeline — routing, generación en dos pasadas, tool-calling,
    verificación post-hoc — con un `yield PipelineEvent(...)` suelto que
    convertía a `run()` en un generador, rompiendo el contrato de
    `QThread.run()` como método normal). Python conservaba solo la
    ÚLTIMA definición de cada nombre, así que la lógica real dependía de
    cuál quedó "más abajo" en el archivo — un bug latente y confuso.

    Ahora esta clase NO contiene ninguna lógica de negocio: todo el
    pipeline (routing, caché semántico, búsqueda web, generación en dos
    pasadas, tool-calling, verificación y corrección post-hoc) vive
    exclusivamente en `Orchestrator.run_turn()`, que es un GENERADOR de
    `PipelineEvent` (ver pipeline.py). Este worker solo:
      1. Arranca ese generador en un hilo de Qt.
      2. Traduce cada `PipelineEvent` a la señal Qt correspondiente.
      3. Provee `web_search_fn` (la función de búsqueda real, que sigue
         viviendo en este módulo por ahora — ver `fetch_rich_web_search`)
         y `cancel_flag` (adaptador sobre `self._is_cancelled`/
         `isInterruptionRequested()`).

    Ningún método de `Orchestrator` (`_call_llm`, `_router.classify`,
    `_dispatch_slow_path`, `find_unsupported_scores`, `_wal_open_turn`,
    etc.) se invoca directamente desde aquí — todos viven detrás de
    `run_turn()`.
    """

    chunk_received = pyqtSignal(str, str)
    completed = pyqtSignal(object, str)
    intent_changed = pyqtSignal(str, str)
    web_results_ready = pyqtSignal(dict)
    log_message = pyqtSignal(str)

    def __init__(
        self,
        orchestrator: Orchestrator,
        prompt: str,
        force_web_search: bool = False,
        lang: str = "Español",
        image_path: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._orchestrator = orchestrator
        self._prompt = prompt
        self._force_web_search = force_web_search
        self._image_path = image_path
        self._is_cancelled = False
        # Idioma de la INTERFAZ (no el de la respuesta del modelo, que ya
        # maneja Orchestrator por su cuenta) — usado solo para traducir
        # los pocos mensajes de estado que este worker arma él mismo
        # (caché hit, cancelación, pipeline sin DONE). BLINDAJE (bug
        # real reportado): antes estaban hardcodeados en español y se
        # colaban tal cual en el chat aunque la interfaz estuviera en
        # inglés.
        self._tr = I18N.get(lang, I18N["Español"])

    def stop(self) -> None:
        """
        Señaliza al hilo del turno que debe detenerse. Fija AMBAS
        banderas (flag propio + `requestInterruption()` de Qt) para
        cubrir los distintos puntos de chequeo dentro de `run_turn()`
        (búsqueda web, cada pasada de generación, cadena de
        verificación) sin depender de una sola de las dos señales.
        """
        self._is_cancelled = True
        self.requestInterruption()

    def _cancelled(self) -> bool:
        return self._is_cancelled or self.isInterruptionRequested()

    def run(self) -> None:
        """
        Método NORMAL (no generador) de `QThread` — consume el
        generador `Orchestrator.run_turn()` con un `for` simple y
        traduce cada evento a la señal Qt correspondiente. Nunca debe
        contener un `yield` propio.
        """
        cancel_flag = _CancelToken(self._cancelled)

        def web_search_fn(
            query: str,
            lang: Optional[str],
            log_cb: Optional[Any],
        ) -> dict:
            return fetch_rich_web_search(query, lang=lang, log_cb=log_cb)

        try:
            for event in self._orchestrator.run_turn(
                self._prompt,
                force_web_search=self._force_web_search,
                web_search_fn=web_search_fn,
                cancel_flag=cancel_flag,
                image_path=self._image_path,
            ):
                if event.type == EventType.INTENT:
                    icon, message = event.payload
                    self.intent_changed.emit(str(icon), str(message))

                elif event.type == EventType.STATUS:
                    self.log_message.emit(str(event.payload))

                elif event.type == EventType.LOG:
                    self.log_message.emit(str(event.payload))

                elif event.type == EventType.ROUTE_DECIDED:
                    decision = event.payload
                    model = event.meta.get("model", "") if event.meta else ""
                    self.log_message.emit(
                        f"[ROUTER] path={decision.path.value} "
                        f"score={decision.score:+.2f} model={model}"
                    )

                elif event.type == EventType.CACHE_HIT:
                    self.log_message.emit(self._tr["log_cache_hit"])

                elif event.type == EventType.WEB_RESULTS:
                    self.web_results_ready.emit(dict(event.payload or {}))

                elif event.type == EventType.TOOL_CALL_START:
                    name = (event.meta or {}).get("name", "")
                    self.log_message.emit(f"[TOOL] Inicio: {name}")

                elif event.type == EventType.TOOL_CALL_RESULT:
                    name = (event.meta or {}).get("name", "")
                    self.log_message.emit(f"[TOOL] Resultado: {name}")

                elif event.type == EventType.VERIFICATION:
                    with contextlib.suppress(Exception):
                        self.log_message.emit(
                            "[VERIFICATION] "
                            + json.dumps(event.payload or {}, ensure_ascii=False, default=str)
                        )

                elif event.type == EventType.TOKEN:
                    chunk, ast_error = event.payload
                    self.chunk_received.emit(str(chunk), str(ast_error or ""))

                elif event.type == EventType.ERROR:
                    self.log_message.emit(f"[ERROR] {event.payload}")

                elif event.type == EventType.DONE:
                    payload = event.payload or {}
                    trace = payload.get("trace")
                    error = str(payload.get("error") or "")
                    if error == "cancelled":
                        error = self._tr["turn_cancelled"]
                    self.completed.emit(trace, error)
                    return

            self.completed.emit(None, self._tr["pipeline_no_done"])

        except Exception as exc:
            self.completed.emit(None, str(exc))
            
class HealthCheckWorker(QThread):
    completed = pyqtSignal(bool)

    def __init__(self, endpoint: str) -> None:
        super().__init__()
        base_url = endpoint.rsplit("/api", 1)[0] if "/api" in endpoint else endpoint
        self._endpoint = f"{base_url}/api/version"

    def run(self) -> None:
        try:
            import requests

            response = requests.get(self._endpoint, timeout=1.5)
            self.completed.emit(response.status_code == 200)
        except Exception:
            self.completed.emit(False)


class CloudKeyCheckWorker(QThread):
    """
    Prueba la API key de Claude (o, desde patch_qt66, de Gemini) en
    segundo plano (mismo patrón que HealthCheckWorker de arriba, para
    no congelar la UI) — un pedido de 1 token de salida es la forma más
    barata de confirmar que la key es válida sin gastar crédito de
    verdad generando algo largo.

    `provider` (2026-09-19, patch_qt66 -- pedido explícito del usuario:
    "se me acabaron los creditos, podemos usar el modelo de gemini
    tambien?"): "anthropic" (default, comportamiento de siempre) o
    "gemini" -- cada uno habla con su propia API con su propio formato
    de request/response, ver `Orchestrator._call_gemini_api_raw` para
    el mismo detalle del lado del motor de generación real.
    """

    completed = pyqtSignal(bool, str)

    def __init__(self, api_key: str, model_id: str, provider: str = "anthropic") -> None:
        super().__init__()
        self._api_key = api_key
        self._model_id = model_id
        self._provider = provider

    def run(self) -> None:
        try:
            import requests

            if self._provider == "gemini":
                resp = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{self._model_id}:generateContent",
                    headers={
                        "x-goog-api-key": self._api_key,
                        "content-type": "application/json",
                    },
                    json={
                        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                        "generationConfig": {"maxOutputTokens": 1},
                    },
                    timeout=15,
                )
            else:
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self._api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self._model_id,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    timeout=15,
                )
            if resp.status_code == 200:
                self.completed.emit(True, "")
            else:
                detail = ""
                try:
                    detail = resp.json().get("error", {}).get("message", "")
                except Exception:
                    pass
                self.completed.emit(False, detail or f"HTTP {resp.status_code}")
        except Exception as exc:
            self.completed.emit(False, str(exc))



WORKSPACE_WATCHER_INTERVAL_SECONDS = 15


class WorkspaceWatcherWorker(QThread):
    """
    Envoltorio QThread de WorkspaceScanner (workspace_watcher.py, Python
    puro sin Qt): corre en segundo plano un loop de escaneo por polling
    sobre las carpetas de "Workspaces" que el usuario agregó desde la UI,
    e indexa/retira archivos de Orchestrator.workspace_vector_rag a
    medida que cambian en disco — sin esto, el usuario tendría que volver
    a arrastrar cada archivo a mano cada vez que lo edita.

    Un solo hilo dedicado para TODAS las carpetas vigiladas (no uno por
    carpeta): evita que agregar muchos workspaces multiplique hilos en
    segundo plano sin límite — la escala del escaneo real es el tamaño
    total de las carpetas, no la cantidad de raíces.

    Deliberadamente NO reutiliza `self._async_executor` (AsyncExecutor /
    QThreadPool(4), ver MainWindow.__init__): ese pool está pensado para
    tareas cortas y puntuales (un indexado de un archivo soltado, que
    termina); esto es un loop de vida larga atado al ciclo de vida de la
    ventana, con su propio `running` flag y `stop()` — mezclar ambos
    complicaría el apagado ordenado del pool existente sin ganar nada.

    Los métodos de Orchestrator que llama (`index_document_for_rag` /
    `remove_document_from_rag`) ya son thread-safe entre sí y respecto al
    hilo de un turno de chat — ambos toman `Orchestrator._vector_rag_lock`
    internamente (ver orchestrator.py) — así que este hilo puede llamarlos
    sin coordinación adicional aquí.
    """

    file_indexed = pyqtSignal(str, int)
    file_removed = pyqtSignal(str, int)
    scan_error = pyqtSignal(str)

    def __init__(self, orchestrator: Orchestrator, scanner: WorkspaceScanner) -> None:
        super().__init__()
        self._orchestrator = orchestrator
        self._scanner = scanner
        self._running = True

    def stop(self) -> None:
        """
        Señal cooperativa de apagado — no usa QThread.terminate() (destruir
        un hilo a mitad de una llamada a Orchestrator/FAISS podría dejar
        el índice vectorial en un estado inconsistente). `run()` revisa
        `_running` entre pasadas y entre archivos de una misma pasada.
        """
        self._running = False

    def run(self) -> None:
        while self._running:
            try:
                to_index, to_remove = self._scanner.scan_once()
            except Exception as exc:
                self.scan_error.emit(str(exc))
                to_index, to_remove = [], []

            for file_path in to_remove:
                if not self._running:
                    return
                try:
                    removed = self._orchestrator.remove_document_from_rag(file_path)
                    if removed:
                        self.file_removed.emit(file_path, removed)
                except Exception as exc:
                    self.scan_error.emit(f"{file_path}: {exc}")

            for file_path in to_index:
                if not self._running:
                    return
                try:
                    with open(file_path, "r", encoding="utf-8") as fh:
                        content = fh.read()
                except Exception as exc:
                    self.scan_error.emit(f"{file_path}: {exc}")
                    continue
                try:
                    count = self._orchestrator.index_document_for_rag(file_path, content)
                    if count:
                        self.file_indexed.emit(file_path, count)
                except Exception as exc:
                    self.scan_error.emit(f"{file_path}: {exc}")

            for _ in range(WORKSPACE_WATCHER_INTERVAL_SECONDS * 10):
                if not self._running:
                    return
                self.msleep(100)


class ModelPullApiWorker(QThread):
    """
    Descarga en segundo plano un modelo de Ollama hablando directamente
    con el endpoint local /api/pull (streaming NDJSON) en vez de invocar
    el binario CLI por subprocess — así se reporta el progreso (%) tal
    como lo expone la propia API nativa de Ollama, en tiempo real.
    """

    progress_updated = pyqtSignal(str, int)
    completed = pyqtSignal(bool, str)

    def __init__(
        self,
        model_tag: str,
        ollama_base_url: str = "http://localhost:11434",
        lang: str = "Español",
    ) -> None:
        super().__init__()
        self.model_tag = (model_tag or "").strip()
        self._base_url = ollama_base_url.rstrip("/")
        self._cancelled = False
        self._tr = I18N.get(lang, I18N["Español"])

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        if not self.model_tag:
            self.completed.emit(False, self._tr["model_tag_empty"])
            return

        try:
            import requests

            response = requests.post(
                f"{self._base_url}/api/pull",
                json={"name": self.model_tag, "stream": True},
                stream=True,
                timeout=None,
            )
            response.raise_for_status()

            last_status = ""
            for raw_line in response.iter_lines(decode_unicode=True):
                if self._cancelled:
                    self.completed.emit(False, self._tr["model_download_cancelled"])
                    return

                if not raw_line:
                    continue

                try:
                    payload = json.loads(raw_line)
                except (ValueError, TypeError):
                    continue

                if payload.get("error"):
                    self.completed.emit(False, str(payload["error"]))
                    return

                status = str(payload.get("status", "")).strip()
                total = payload.get("total")
                completed_bytes = payload.get("completed")

                percent = -1
                if (
                    isinstance(total, (int, float))
                    and total > 0
                    and isinstance(completed_bytes, (int, float))
                ):
                    percent = int((completed_bytes / total) * 100)

                if status or percent >= 0:
                    last_status = status or last_status
                    self.progress_updated.emit(last_status, percent)

            self.completed.emit(
                True, self._tr["model_download_success"].format(self.model_tag)
            )

        except Exception as exc:
            self.completed.emit(False, str(exc))


class ModelDownloadDialog(QDialog):
    """
    Diálogo para ingresar o seleccionar el tag de un modelo de Ollama y
    descargarlo en caliente vía ModelPullApiWorker (POST /api/pull),
    mostrando el progreso (%) en tiempo real.
    """

    COMMON_TAGS = (
        "huihui_ai/qwen2.5-abliterate:7b-instruct",
        "qwen2.5-coder:7b",
        "qwen2.5:7b",
        "qwen2.5:3b",
        "phi3.5:3.8b",
        "qwen2.5-coder:3b",
        "nomic-embed-text",
    )

    def __init__(
        self,
        ollama_endpoint: str,
        lang: str = "Español",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang
        tr = I18N.get(lang, I18N["Español"])
        self._base_url = (
            ollama_endpoint.rsplit("/api", 1)[0]
            if "/api" in ollama_endpoint
            else ollama_endpoint
        )
        self._worker: Optional[ModelPullApiWorker] = None
        self.downloaded_tag: Optional[str] = None

        self.setWindowTitle(tr["download_dialog_title"])
        self.setModal(True)
        self.setMinimumWidth(440)
        self.setStyleSheet(
            "QDialog { background-color: #14171F; color: #FFFFFF; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        title = QLabel(tr["download_dialog_title"])
        title.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #4C8BF5;"
        )
        layout.addWidget(title)

        desc = QLabel(tr["download_dialog_desc"])
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size: 12px; color: #E6E8EC;")
        layout.addWidget(desc)

        self.combo_tag = QComboBox()
        self.combo_tag.setEditable(True)
        self.combo_tag.addItems(list(self.COMMON_TAGS))
        self.combo_tag.setCurrentIndex(0)
        layout.addWidget(self.combo_tag)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background-color: #1B1F2A; border: 1px solid #262B36;"
            " border-radius: 6px; text-align: center; color: #FFFFFF; }"
            "QProgressBar::chunk { background-color: #3DDC97; border-radius: 5px; }"
        )
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel(tr["download_status_idle"])
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 11px; color: #8B92A5;")
        layout.addWidget(self.status_label)

        self.btn_start = QPushButton(tr["download_btn_start"])
        self.btn_start.setObjectName("actionButton")
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.setStyleSheet(
            "QPushButton {"
            "  background-color: #1E3A8A; color: #FFFFFF; font-weight: bold;"
            "  border: 1px solid #4C8BF5; border-radius: 8px; padding: 10px;"
            " font-size: 13px;"
            "}"
            "QPushButton:hover { background-color: #2563EB; }"
        )
        self.btn_start.clicked.connect(self._start_download)
        layout.addWidget(self.btn_start)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.setStyleSheet(
            "QPushButton {"
            "  background-color: #262B36; color: #FFFFFF; border-radius: 6px;"
            " padding: 6px 18px;"
            "}"
            "QPushButton:hover { background-color: #3B4252; }"
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _start_download(self) -> None:
        tag = self.combo_tag.currentText().strip()
        if not tag:
            return

        self.btn_start.setEnabled(False)
        self.combo_tag.setEnabled(False)
        self.downloaded_tag = None
        self.progress_bar.setValue(0)
        self.status_label.setText(tag)

        self._worker = ModelPullApiWorker(tag, self._base_url, lang=self._lang)
        self._worker.progress_updated.connect(self._on_progress)
        self._worker.completed.connect(self._on_completed)
        self._worker.start()

    def _on_progress(self, message: str, percent: int) -> None:
        if message:
            self.status_label.setText(message)
        if percent >= 0:
            self.progress_bar.setValue(percent)

    def _on_completed(self, success: bool, message: str) -> None:
        tr = I18N.get(self._lang, I18N["Español"])
        self.btn_start.setEnabled(True)
        self.combo_tag.setEnabled(True)

        if success:
            self.downloaded_tag = self.combo_tag.currentText().strip()
            self.progress_bar.setValue(100)
            self.status_label.setText(message)
            QMessageBox.information(self, tr["download_dialog_title"], message)
        else:
            self.status_label.setText(message)
            QMessageBox.critical(self, tr["download_dialog_title"], message)

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(500)
        super().closeEvent(event)


class CollapsibleSection(QWidget):
    """
    Sección plegable de la barra lateral (pedido explícito del
    usuario, 2026-09-15: "agruparía esto en secciones plegables
    (Apariencia, Motor, Workspace) con solo 2-3 abiertas por defecto, e
    íconos por sección en vez de solo texto en mayúscula"). Envuelve
    un grupo de QFrame "sidebarCard" YA EXISTENTES (no les toca nada
    por dentro) bajo un encabezado clickeable con ícono + título +
    chevron -- mismo patrón expand/collapse que
    `TraceWidget.toggle_details` (⌄ cerrado, ⌃ abierto), reusado acá
    para no introducir una segunda convención visual distinta.
    """

    def __init__(
        self,
        icon_emoji: str,
        title: str,
        expanded: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._icon_emoji = icon_emoji
        self._title = title

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.toggle_button = QPushButton(self._header_text(expanded))
        self.toggle_button.setObjectName("sidebarSectionHeader")
        self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_button.clicked.connect(self.toggle)

        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(10)
        self.content.setVisible(expanded)

        layout.addWidget(self.toggle_button)
        layout.addWidget(self.content)

    def _header_text(self, expanded: bool) -> str:
        chevron = "⌃" if expanded else "⌄"
        return f"{chevron} {self._icon_emoji} {self._title}"

    def add_widget(self, widget: QWidget) -> None:
        self.content_layout.addWidget(widget)

    def toggle(self) -> None:
        expanded = not self.content.isVisible()
        self.content.setVisible(expanded)
        self.toggle_button.setText(self._header_text(expanded))

    def set_title(self, title: str) -> None:
        """Re-traduce el título sin perder el estado expandido/colapsado
        actual -- llamado desde `_on_lang_changed` (mismo motivo que el
        resto de los widgets persistentes: se construyen una sola vez en
        `_create_ui` con el idioma de ese momento)."""
        self._title = title
        self.toggle_button.setText(self._header_text(self.content.isVisible()))


class TraceWidget(QWidget):
    def __init__(
        self,
        trace: Any,
        lang: str = "Español",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        tr = I18N.get(lang, I18N["Español"])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 5, 0, 0)
        layout.setSpacing(5)

        routing = getattr(trace, "routing_decision", None)
        route = getattr(getattr(routing, "path", None), "value", "unknown")
        score = getattr(routing, "score", 0.0)
        outcome = getattr(getattr(trace, "outcome", None), "value", "unknown")
        elapsed = getattr(trace, "total_elapsed_ms", 0.0)
        trace_id = str(getattr(trace, "turn_id", "unknown"))[:8]
        model_used = getattr(trace, "model_used", "desconocido")
        repairs = getattr(trace, "syntax_repairs_applied", 0)

        confidence_label = getattr(trace, "confidence_label", "N/D")
        confidence_score = getattr(trace, "confidence_score", None)
        confidence_badge = (
            f" · confianza {confidence_label} ({confidence_score:.2f})"
            if confidence_score is not None
            else ""
        )

        self.toggle_button = QPushButton(
            f"⌄ {tr['trace_analysis_label']} · {trace_id} · {model_used} · "
            f"{elapsed:.1f} ms{confidence_badge}"
        )
        self.toggle_button.setObjectName("traceButton")
        self.toggle_button.clicked.connect(self.toggle_details)

        self.details = QFrame()
        self.details.setObjectName("sidebarCard")

        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(9, 8, 9, 8)
        details_layout.setSpacing(5)

        route_label = (
            "⚡ FAST PATH" if "fast" in str(route).lower() else "🧠 SLOW PATH"
        )
        persisted = (
            tr["yes_label"] if getattr(trace, "knowledge_node_persisted", False) else tr["no_label"]
        )
        logical = getattr(trace, "logical_status", "unknown")
        web_used = tr["yes_label"] if getattr(trace, "web_context_used", False) else tr["no_label"]
        web_attempted = (
            tr["yes_label"] if getattr(trace, "web_search_attempted", False) else tr["no_label"]
        )

        summary = QLabel(
            tr["trace_summary_fmt"].format(
                route=route_label,
                outcome=outcome,
                model=model_used,
                score=f"{score:+.1f}",
                persisted=persisted,
                logical=logical,
                web_attempted=web_attempted,
                web_used=web_used,
                repairs=repairs,
            )
        )
        summary.setWordWrap(True)
        summary.setStyleSheet("font-size: 11px; color: #FFFFFF;")
        details_layout.addWidget(summary)

        genius_badges = []
        if getattr(trace, "thought_code_verified", False):
            genius_badges.append(tr["badge_sandbox_verified"])
        if getattr(trace, "tot_used", False):
            tot_agreement = getattr(trace, "tot_agreement", None)
            agree_txt = (
                tr["badge_tot_agreement_suffix"].format(pct=f"{tot_agreement:.0%}")
                if tot_agreement is not None else ""
            )
            genius_badges.append(f"🌳 Tree-of-Thoughts{agree_txt}")
        if getattr(trace, "epistemic_drift_detected", False):
            genius_badges.append(tr["badge_epistemic_drift"])

        if genius_badges:
            genius_label = QLabel("  ·  ".join(genius_badges))
            genius_label.setWordWrap(True)
            genius_label.setStyleSheet("font-size: 10px; color: #9CDCF0; font-style: italic;")
            details_layout.addWidget(genius_label)

        engine_results = getattr(trace, "engine_results", [])
        if engine_results:
            engine_title = QLabel(tr["trace_engines_title"])
            engine_title.setStyleSheet(
                "font-size: 11px; font-weight: bold; color: #FFFFFF;"
            )
            details_layout.addWidget(engine_title)

            for result in engine_results:
                result_view = QTextBrowser()
                result_view.setPlainText(str(result))
                result_view.setMaximumHeight(110)
                result_view.setStyleSheet(
                    "font-family: Consolas, monospace; font-size: 10px; color:"
                    " #FFFFFF;"
                )
                details_layout.addWidget(result_view)

        self.details.setVisible(False)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self.details)

    def toggle_details(self) -> None:
        visible = not self.details.isVisible()
        self.details.setVisible(visible)
        self.toggle_button.setText(
            self.toggle_button.text().replace("⌄", "⌃", 1)
            if visible
            else self.toggle_button.text().replace("⌃", "⌄", 1)
        )


class _AutoHeightTextEdit(QTextEdit):
    """
    QTextEdit que ajusta su alto real al contenido en vez de un rango
    fijo siempre igual — ver la nota grande junto a `CodeBlockWidget`
    más abajo (pedido explícito del usuario, 2026-09-13: "que los
    mensajes embed se configure su tamaño automaticamente dependiendo
    de la cantidad de texto dentro"). Mismo patrón que
    `PromptTextEdit._adjust_height` (documento -> alto real, acotado
    entre un mínimo y un máximo, con scroll interno más allá del
    máximo) pero disparado desde `resizeEvent` en vez de
    `textChanged`: el contenido de estas tarjetas se fija una sola vez
    al construirlas, así que el ancho real (y por lo tanto cuántas
    líneas ocupa el texto tras el wrap) solo se conoce cuando Qt las
    agrega al layout del padre y las redimensiona por primera vez.
    """

    def __init__(
        self,
        min_height: int,
        max_height: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._min_height = min_height
        self._max_height = max_height
        self.setMinimumHeight(min_height)
        self.setMaximumHeight(max_height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        doc_height = self.document().size().height()
        new_height = max(
            self._min_height, min(self._max_height, int(doc_height) + 12)
        )
        if new_height != self.height():
            self.setFixedHeight(new_height)


class CodeBlockWidget(QFrame):
    def __init__(
        self,
        code: str,
        language: str = "",
        lang: str = "Español",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._code = code
        self._language = language.strip() or "text"
        self._lang = lang if lang in I18N else "Español"
        tr = I18N[self._lang]
        self.setObjectName("codeBlock")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        # BLINDAJE (rediseño 2026-09-09 — "se ve muy tosco"): antes un
        # QLabel de texto plano en blanco FIJO (#FFFFFF, ignoraba el tema
        # activo — se veía mal en Nordic Slate/OLED). Ahora una "píldora"
        # con el color de acento del tema (mismo lenguaje visual que un
        # badge), y los botones llevan ícono además de texto en vez de
        # ser dos rectángulos de texto pelados.
        language_label = QLabel(self._language.upper())
        language_label.setObjectName("codeLanguagePill")
        language_label.setStyleSheet(
            f"font-size: 10px; font-weight: bold; color: {_ACTIVE_THEME['bg']}; "
            f"background-color: {_ACTIVE_THEME['accent']}; "
            "border-radius: 4px; padding: 2px 8px;"
        )

        copy_button = QPushButton(
            icons.icon("clipboard", _ACTIVE_THEME["secondary"], 13), tr["btn_copy_code"]
        )
        copy_button.setObjectName("codeActionButton")
        copy_button.clicked.connect(self.copy_code)

        save_button = QPushButton(
            icons.icon("save", _ACTIVE_THEME["secondary"], 13), tr["btn_save_code"]
        )
        save_button.setObjectName("codeActionButton")
        save_button.clicked.connect(self.save_code)

        toolbar.addWidget(language_label)
        toolbar.addStretch()
        toolbar.addWidget(copy_button)
        toolbar.addWidget(save_button)

        self.code_view = _AutoHeightTextEdit(72, 280)
        self.code_view.setReadOnly(True)
        self.code_view.setPlainText(code)
        self.code_view.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; "
            f"font-size: 12px; border-radius: 8px; padding: 8px; "
            f"background-color: {_ACTIVE_THEME['bg']}; color: {_ACTIVE_THEME['text']}; "
            "border: none;"
        )

        layout.addLayout(toolbar)
        layout.addWidget(self.code_view)

    def copy_code(self) -> None:
        tr = I18N.get(self._lang, I18N["Español"])
        QApplication.clipboard().setText(self._code)
        QMessageBox.information(
            self, tr["code_copied_title"], tr["code_copied_msg"]
        )

    def save_code(self) -> None:
        tr = I18N.get(self._lang, I18N["Español"])
        suggested_extension = {
            "python": "py",
            "py": "py",
            "json": "json",
            "markdown": "md",
            "md": "md",
            "csv": "csv",
        }.get(self._language.lower(), "txt")

        filename, _ = QFileDialog.getSaveFileName(
            self,
            tr["dialog_save_code"],
            f"sovnode_block.{suggested_extension}",
            tr["dialog_save_filter"],
        )

        if not filename:
            return

        try:
            Path(filename).write_text(self._code, encoding="utf-8")
            QMessageBox.information(
                self,
                tr["file_saved_title"],
                tr["file_saved_msg"].format(filename),
            )
        except OSError as exc:
            QMessageBox.critical(
                self,
                tr["file_save_error_title"],
                tr["file_save_error_msg"].format(exc),
            )


class ToolResultCard(QFrame):
    """
    ES: Tarjeta compacta para el resultado de una llamada a herramienta
    (write_file/read_file/list_dir/run_cmd/system_telemetry, y las
    variantes del motor de herramientas dinámico) — rediseño pedido
    2026-09-09 ("el uso de herramientas se ve muy tosco"). Antes
    orchestrator.py armaba un encabezado en negrita
    ("**[RESULTADO DE HERRAMIENTA (...)]**") seguido de un fence
    ```text``` genérico, y `MessageBubble._add_assistant_content` los
    renderizaba como DOS widgets sueltos: una línea de texto plana y
    debajo un CodeBlockWidget completo con etiqueta "TEXT" y botones
    "Copiar"/"Guardar" — que no tienen sentido para un mensaje de estado
    como "Archivo escrito exitosamente en 'pong.py' (2474 bytes).".

    Ahora orchestrator.py emite un ÚNICO fence cuyo "lenguaje" codifica
    el nombre de la herramienta (`toolresult-{nombre}`, ver
    execute_tool_from_call/synthesize_and_run_dynamic_tool) y
    `_add_assistant_content` lo detecta por ese prefijo para instanciar
    ESTA tarjeta en su lugar: un solo bloque compacto con ícono propio
    por herramienta, franja de color a la izquierda (verde/acento si
    salió bien, roja si el texto o el propio tag delatan un fallo) y un
    único botón de copiar discreto — sin "Guardar como archivo", que no
    aplica a un mensaje de estado.

    EN: Compact card for a tool-call result. See the module docstring
    above (Spanish) for the full before/after — in short: replaces a
    bold header + generic ```text``` code block (with meaningless
    Copy/Save-as-file buttons) with one small card with a per-tool icon,
    a colored left accent (success vs. failure), and a single discreet
    copy button.
    """

    _ICONS = {
        "write_file": "save",
        "read_file": "download",
        "list_dir": "attach",
        "run_cmd": "terminal",
        "system_telemetry": "gear",
        "dynamic_reused": "wrench",
        "dynamic_executed": "wrench",
        "dynamic_failed": "warning",
    }

    _LABELS = {
        "Español": {
            "write_file": "Escribir archivo",
            "read_file": "Leer archivo",
            "list_dir": "Listar carpeta",
            "run_cmd": "Ejecutar comando",
            "system_telemetry": "Telemetría del sistema",
            "dynamic_reused": "Herramienta dinámica (reutilizada)",
            "dynamic_executed": "Herramienta dinámica (generada)",
            "dynamic_failed": "Herramienta dinámica (falló)",
        },
        "English": {
            "write_file": "Write file",
            "read_file": "Read file",
            "list_dir": "List folder",
            "run_cmd": "Run command",
            "system_telemetry": "System telemetry",
            "dynamic_reused": "Dynamic tool (reused)",
            "dynamic_executed": "Dynamic tool (generated)",
            "dynamic_failed": "Dynamic tool (failed)",
        },
    }

    _FAILURE_HINTS = ("[SANDBOX", "[FALLO", "[ERROR", "traceback (most recent call last)")

    def __init__(
        self,
        tool_tag: str,
        result_text: str,
        lang: str = "Español",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang if lang in self._LABELS else "Español"
        self._result_text = result_text
        self.setObjectName("toolResultCard")

        is_failed = tool_tag == "dynamic_failed" or any(
            hint.lower() in result_text.lower() for hint in self._FAILURE_HINTS
        )
        self.setProperty("failed", "true" if is_failed else "false")

        icon_kind = self._ICONS.get(tool_tag, "warning" if is_failed else "wrench")
        accent_key = "danger" if is_failed else "accent"
        label = self._LABELS.get(self._lang, {}).get(
            tool_tag, tool_tag.replace("_", " ").capitalize()
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(6)

        icon_label = QLabel()
        icon_label.setPixmap(icons.icon(icon_kind, _ACTIVE_THEME[accent_key], 15).pixmap(15, 15))
        header.addWidget(icon_label)

        title_label = QLabel(label)
        title_label.setStyleSheet(
            f"font-size: 11px; font-weight: bold; color: {_ACTIVE_THEME[accent_key]};"
        )
        header.addWidget(title_label)
        header.addStretch()

        copy_button = QPushButton()
        copy_button.setIcon(icons.icon("clipboard", _ACTIVE_THEME["secondary"], 13))
        copy_button.setFixedSize(22, 22)
        copy_button.setObjectName("toolResultCopyButton")
        copy_button.setToolTip("Copiar" if self._lang == "Español" else "Copy")
        copy_button.clicked.connect(self._copy_result)
        header.addWidget(copy_button)

        layout.addLayout(header)

        body = _AutoHeightTextEdit(28, 140)
        body.setReadOnly(True)
        body.setPlainText(result_text)
        body.setObjectName("toolResultBody")
        body.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 11px; "
            f"color: {_ACTIVE_THEME['secondary']}; border: none; background: transparent;"
        )
        layout.addWidget(body)

    def _copy_result(self) -> None:
        QApplication.clipboard().setText(self._result_text)


class MessageBubble(QWidget):
    CODE_PATTERN = re.compile(
        r"```(?P<language>[A-Za-z0-9_+\-]*)\n?(?P<code>.*?)```",
        re.DOTALL,
    )
    MIN_RENDER_INTERVAL_MS = 90
    tts_requested = pyqtSignal(str)
    tts_stop_requested = pyqtSignal()

    def __init__(
        self,
        sender: str,
        content: str,
        timestamp: str,
        trace: Optional[Any] = None,
        is_error: bool = False,
        is_warning: bool = False,
        lang: str = "Español",
        parent: Optional[QWidget] = None,
        image_path: Optional[str] = None,
    ) -> None:
        super().__init__(parent)

        tr = I18N.get(lang, I18N["Español"])
        self._lang = lang if lang in I18N else "Español"
        self._sender = sender
        self._is_user = sender == "user"
        self._content = content
        self._is_error = is_error
        self._is_warning = is_warning and not is_error
        self._card = QFrame()
        self._last_render_ts = 0.0
        self._pending_render_content: Optional[str] = None
        self._pending_render_timer: Optional[QTimer] = None

        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._card.setSizePolicy(
            QSizePolicy.Policy.Preferred if self._is_user else QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

        if is_error:
            self._card.setObjectName("errorCard")
        elif self._is_warning:
            self._card.setObjectName("warningCard")
        elif self._is_user:
            self._card.setObjectName("userCard")
        else:
            self._card.setObjectName("assistantCard")

        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(0, 10, 0, 10)
        outer_layout.setSpacing(0)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(16, 10, 16, 10) if self._is_user else card_layout.setContentsMargins(16, 4, 16, 4)
        card_layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        sender_icon_kind: Optional[str] = None
        sender_icon_color: str = _ACTIVE_THEME["accent"]
        sender_label: Optional[QLabel] = None
        if self._is_user:
            sender_icon_kind = "user"
            sender_label = QLabel(tr["msg_sender_user"])
        elif is_error:
            sender_icon_kind = "warning"
            sender_icon_color = _ACTIVE_THEME["danger"]
            sender_label = QLabel(tr["msg_sender_error"])
        elif self._is_warning:
            sender_icon_kind = "wrench"
            sender_icon_color = _ACTIVE_THEME["warning"]
            sender_label = QLabel(tr["msg_sender_warning"])

        if sender_label is not None:
            sender_label.setObjectName("messageSender")
            if sender_icon_kind is not None:
                sender_icon_label = QLabel()
                sender_icon_label.setPixmap(
                    icons.icon(sender_icon_kind, sender_icon_color, 13).pixmap(13, 13)
                )
                header_row.addWidget(sender_icon_label)
            header_row.addWidget(sender_label)
            header_row.addStretch(1)

        if sender_label is not None:
            card_layout.addLayout(header_row)
        self._content_start_index = 1 if sender_label is not None else 0

        if self._is_user and image_path:
            with contextlib.suppress(Exception):
                raw_pixmap = QPixmap(image_path)
                if not raw_pixmap.isNull():
                    thumb_label = QLabel()
                    scaled = raw_pixmap.scaled(
                        220, 220,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    thumb_label.setPixmap(scaled)
                    thumb_label.setStyleSheet(
                        "border-radius: 8px; margin-bottom: 4px;"
                    )
                    card_layout.addWidget(thumb_label)
                    self._content_start_index += 1

        if self._is_user or is_error:
            text_view = AutoResizingTextBrowser()
            text_view.setPlainText(content)
            card_layout.addWidget(text_view)
            text_view.update_height()
        else:
            self._add_assistant_content(card_layout, content)

        if trace is not None and not self._is_user:
            card_layout.addWidget(TraceWidget(trace, lang=self._lang))

        timestamp_label = QLabel(timestamp)
        timestamp_label.setObjectName("messageTimestamp")

        self._footer_widget = QWidget()
        footer_row = QHBoxLayout(self._footer_widget)
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.setSpacing(8)
        footer_row.addStretch(1)
        footer_row.addWidget(timestamp_label)

        if not self._is_user and not is_error:
            self.options_btn = QPushButton("⋮")
            self.options_btn.setFixedSize(24, 24)
            self.options_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.options_btn.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    color: #8B92A5;
                    border: none;
                    font-weight: bold;
                    font-size: 14px;
                }
                QPushButton:hover {
                    color: #FFFFFF;
                    background: #1F2430;
                    border-radius: 4px;
                }
            """)
            self.options_btn.clicked.connect(self._show_options_menu)
            footer_row.addWidget(self.options_btn)

        card_layout.addWidget(self._footer_widget)

        if self._is_user:
            outer_layout.addStretch(1)
            outer_layout.addWidget(self._card, 0)
        else:
            outer_layout.addWidget(self._card, 1)

    def _show_options_menu(self) -> None:
        tr = I18N.get(self._lang, I18N["Español"])
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1A1F2C;
                color: #FFFFFF;
                border: 1px solid #30363D;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 16px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #2D3748;
                color: #58A6FF;
            }
        """)

        tts_action = QAction(tr["tts_listen"], self)
        tts_action.triggered.connect(
            lambda: self.tts_requested.emit(self._content)
        )
        menu.addAction(tts_action)

        stop_tts_action = QAction(tr["tts_stop"], self)
        stop_tts_action.triggered.connect(
            lambda: self.tts_stop_requested.emit()
        )
        menu.addAction(stop_tts_action)

        menu.addSeparator()

        copy_action = QAction(
            icons.icon("clipboard", _ACTIVE_THEME["secondary"], 16), "Copiar texto", self
        )
        copy_action.triggered.connect(
            lambda: QApplication.clipboard().setText(self._content)
        )
        menu.addAction(copy_action)

        if hasattr(self, "options_btn"):
            menu.exec(
                self.options_btn.mapToGlobal(
                    self.options_btn.rect().bottomRight()
                )
            )

    def _add_assistant_content(
        self, layout: QVBoxLayout, content: str
    ) -> None:
        matches = list(self.CODE_PATTERN.finditer(content))

        if not matches:
            self._add_markdown_view(layout, content)
            return

        position = 0
        for match in matches:
            before = content[position : match.start()].strip()
            if before:
                self._add_markdown_view(layout, before)

            language = match.group("language")
            code = match.group("code").strip("\n")
            # BLINDAJE (rediseño 2026-09-09, ver ToolResultCard más
            # arriba): orchestrator.py marca los resultados de
            # herramienta con el fence `toolresult-{nombre}` en vez de
            # uno de lenguaje real — acá se detecta ese prefijo para
            # instanciar la tarjeta dedicada en vez del CodeBlockWidget
            # genérico que se usa para código de verdad.
            if language.startswith("toolresult-"):
                layout.addWidget(
                    ToolResultCard(
                        tool_tag=language[len("toolresult-"):],
                        result_text=code,
                        lang=self._lang,
                    )
                )
            else:
                layout.addWidget(
                    CodeBlockWidget(
                        code=code,
                        language=language,
                        lang=self._lang,
                    )
                )
            position = match.end()

        after = content[position:].strip()
        if after:
            self._add_markdown_view(layout, after)

    @staticmethod
    def _render_markdown_with_equations(view: "AutoResizingTextBrowser", text: str) -> None:
        """
        Envoltorio sobre setMarkdown() que además renderiza cualquier
        ecuación LaTeX detectada en `text` (ver math_render.py) como
        imagen embebida, en vez de dejar el bracket/backslash crudo del
        modelo tal cual.

        QTextDocument.setMarkdown() no soporta HTML embebido (un <img>
        puesto directo en el texto fuente desaparece y de paso corrompe
        el resto del documento — verificado), así que el reemplazo por
        imagen real ocurre en un segundo paso, sobre el HTML que
        QTextDocument ya generó: setMarkdown(con_placeholders) ->
        toHtml() -> reemplazar placeholders por <img> -> setHtml(). Si
        el mensaje no tiene ninguna ecuación (el caso común, la enorme
        mayoría de los turnos), extract_equations_as_placeholders()
        devuelve el texto intacto y placeholders vacío — acá se corta
        directo al setMarkdown() de siempre, sin el costo del segundo
        paso.
        """
        text_with_placeholders, placeholders = math_render.extract_equations_as_placeholders(text)
        view.setMarkdown(text_with_placeholders)
        if placeholders:
            html = math_render.splice_images_into_html(view.document().toHtml(), placeholders)
            view.document().setHtml(html)

    @staticmethod
    def _add_markdown_view(layout: QVBoxLayout, text: str) -> None:
        view = AutoResizingTextBrowser()
        MessageBubble._render_markdown_with_equations(view, text)
        layout.addWidget(view)
        view.update_height()

    def update_content(self, new_content: str) -> None:
        """
        Actualiza dinámicamente el contenido del globo. Throttled
        (trailing edge, ver MIN_RENDER_INTERVAL_MS): si se llama de nuevo
        antes de que pase el margen mínimo desde el último render real,
        se guarda `new_content` como pendiente y se programa un único
        render diferido — nunca se pierde la actualización, solo se
        pospone lo suficiente para no repetir el re-layout completo del
        documento en cada tick de streaming.
        """
        self._content = new_content
        now = time.time()
        elapsed_ms = (now - self._last_render_ts) * 1000.0

        if elapsed_ms < self.MIN_RENDER_INTERVAL_MS:
            self._pending_render_content = new_content
            if self._pending_render_timer is None:
                self._pending_render_timer = QTimer(self)
                self._pending_render_timer.setSingleShot(True)
                self._pending_render_timer.timeout.connect(self._flush_pending_render)
            if not self._pending_render_timer.isActive():
                remaining_ms = max(1, int(self.MIN_RENDER_INTERVAL_MS - elapsed_ms))
                self._pending_render_timer.start(remaining_ms)
            return

        self._pending_render_content = None
        self._last_render_ts = now
        self._render_content_now(new_content)

    def _flush_pending_render(self) -> None:
        if self._pending_render_content is None:
            return
        content = self._pending_render_content
        self._pending_render_content = None
        self._last_render_ts = time.time()
        self._render_content_now(content)

    def force_flush_render(self) -> None:
        """
        Cancela el temporizador de render diferido (ver
        MIN_RENDER_INTERVAL_MS/update_content) y ejecuta de inmediato
        cualquier render pendiente — usado cuando el turno YA terminó
        (StreamTurnWorker.completed, ver MainWindow._on_turn_completed)
        para garantizar que ningún token quede atrapado en
        `_pending_render_content` esperando el próximo tick del
        QTimer. Sin esto: `_flush_stream_buffer()` en la señal
        `completed` llama a `update_content()` con el contenido final,
        pero si esa llamada cae dentro de la ventana de
        MIN_RENDER_INTERVAL_MS del último render real, `update_content()`
        solo REPROGRAMA el render diferido (su comportamiento normal,
        correcto durante el streaming activo) en vez de mostrarlo ya —
        dejando el último fragmento de la respuesta invisible hasta que
        ese timer dispare por su cuenta, o indefinidamente si algo
        interrumpe el ciclo de eventos antes de eso.
        """
        if self._pending_render_timer is not None and self._pending_render_timer.isActive():
            self._pending_render_timer.stop()
        self._flush_pending_render()

    def _render_content_now(self, new_content: str) -> None:
        # BLINDAJE (bug real, MEDIDO 2026-09-09 — video demo, "Detener" a
        # mitad de un streaming): `self._card` es un QFrame HIJO de este
        # QWidget. Si el bubble ya fue quitado del layout de chat (Detener
        # generación, limpiar chat, nueva sesión) mientras un render
        # diferido seguía en cola — el QTimer de `update_content`/
        # `_flush_pending_render`, o una señal de streaming que ya viajaba
        # cuando se apretó Detener — Qt ya destruyó el QFrame de C++ por
        # la cascada padre-hijo, pero el objeto Python `self` (y esta
        # llamada encolada) sigue vivo un instante más. Tocar `self._card`
        # en ese estado tira `RuntimeError: wrapped C/C++ object of type
        # QFrame has been deleted` y se lleva puesta TODA la aplicación —
        # no solo este bubble. Un único chequeo acá cubre las tres rutas
        # de entrada (`update_content`, `_flush_pending_render`,
        # `force_flush_render`), porque las tres terminan llamando a este
        # método; simplemente no hay nada que renderizar en un bubble que
        # ya no existe.
        if sip.isdeleted(self._card):
            return
        card_layout = self._card.layout()
        start = self._content_start_index

        matches = list(self.CODE_PATTERN.finditer(new_content))

        if not matches:
            if card_layout.count() >= start + 2:
                first_content_widget = card_layout.itemAt(start).widget()
                next_widget = card_layout.itemAt(start + 1).widget()
                if isinstance(first_content_widget, AutoResizingTextBrowser) and (
                    card_layout.count() == start + 2
                    or isinstance(next_widget, TraceWidget)
                ):
                    MessageBubble._render_markdown_with_equations(first_content_widget, new_content)
                    first_content_widget.update_height()
                    return

        while card_layout.count() > start + 1:
            item = card_layout.itemAt(start)
            if item:
                w = item.widget()
                if isinstance(w, TraceWidget) or w is self._footer_widget:
                    break
                card_layout.removeWidget(w)
                w.deleteLater()
            else:
                break

        if self._is_user or self._is_error:
            text_view = AutoResizingTextBrowser()
            text_view.setPlainText(new_content)
            card_layout.insertWidget(start, text_view)
            text_view.update_height()
        else:
            if not matches:
                self._add_markdown_view_at(card_layout, start, new_content)
            else:
                position = 0
                insert_idx = start
                for match in matches:
                    before = new_content[position : match.start()].strip()
                    if before:
                        self._add_markdown_view_at(
                            card_layout, insert_idx, before
                        )
                        insert_idx += 1

                    # Mismo BLINDAJE de ToolResultCard que
                    # _add_assistant_content (ver su comentario): este es
                    # el camino de re-render INCREMENTAL durante
                    # streaming, tiene que tomar exactamente la misma
                    # decisión o un resultado de herramienta parpadearía
                    # entre las dos tarjetas mientras el turno sigue en
                    # curso.
                    language = match.group("language")
                    code = match.group("code").strip("\n")
                    if language.startswith("toolresult-"):
                        content_widget: QWidget = ToolResultCard(
                            tool_tag=language[len("toolresult-"):],
                            result_text=code,
                            lang=self._lang,
                        )
                    else:
                        content_widget = CodeBlockWidget(
                            code=code,
                            language=language,
                            lang=self._lang,
                        )
                    card_layout.insertWidget(insert_idx, content_widget)
                    insert_idx += 1
                    position = match.end()

                after = new_content[position:].strip()
                if after:
                    self._add_markdown_view_at(card_layout, insert_idx, after)

    @staticmethod
    def _add_markdown_view_at(
        layout: QVBoxLayout, index: int, text: str
    ) -> None:
        view = AutoResizingTextBrowser()
        MessageBubble._render_markdown_with_equations(view, text)
        layout.insertWidget(index, view)
        view.update_height()

    def set_available_width(self, width: int) -> None:
        # Mismo blindaje que _render_content_now: un resize de la ventana
        # puede llegar en cola justo después de que este bubble ya fue
        # retirado del chat (mismo QFrame hijo, misma carrera).
        if sip.isdeleted(self._card):
            return
        if self._is_user:
            self._card.setMaximumWidth(max(160, int(width * 0.75)))
        else:
            self._card.setMaximumWidth(int(width))


class DonationDialog(QDialog):

    def __init__(
        self, lang: str = "English", parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.lang = lang
        tr = I18N.get(lang, I18N["English"])

        self.setWindowTitle(tr["donate_title"])
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setStyleSheet(
            "QDialog { background-color: #14171F; color: #FFFFFF; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_icon = QLabel()
        title_icon.setPixmap(
            icons.icon("coffee", _ACTIVE_THEME["accent"], 20).pixmap(20, 20)
        )
        title = QLabel(tr["donate_header"])
        title.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #FFFFFF;"
        )
        title_row.addWidget(title_icon)
        title_row.addWidget(title, 1)
        layout.addLayout(title_row)

        desc = QLabel(tr["donate_desc"])
        desc.setWordWrap(True)
        desc.setStyleSheet(
            "font-size: 13px; color: #E6E8EC; line-height: 1.4;"
        )
        layout.addWidget(desc)

        btn_kofi = QPushButton(tr["donate_btn_kofi"])
        btn_kofi.setIcon(icons.icon("globe", "#FFFFFF", 16))
        btn_kofi.setIconSize(QSize(16, 16))
        btn_kofi.setObjectName("actionButton")
        btn_kofi.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_kofi.setStyleSheet(
            "QPushButton {"
            "  background-color: #1E3A8A; color: #FFFFFF; font-weight: bold; "
            "  border: 1px solid #4C8BF5; border-radius: 8px; padding: 10px;"
            " font-size: 13px;"
            "}"
            "QPushButton:hover { background-color: #2563EB; }"
        )
        btn_kofi.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(DONATION_LINKS["kofi"]))
        )
        layout.addWidget(btn_kofi)

        crypto_title = QLabel(tr["donate_crypto_title"])
        crypto_title.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #3DDC97; margin-top:"
            " 8px;"
        )
        layout.addWidget(crypto_title)

        crypto_box = QHBoxLayout()
        crypto_box.setSpacing(8)

        self.crypto_field = QLineEdit(DONATION_LINKS["usdt_address"])
        self.crypto_field.setReadOnly(True)
        self.crypto_field.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 12px; "
            "background-color: #0B0E14; color: #3DDC97; border: 1px solid"
            " #262B36; padding: 8px; border-radius: 6px;"
        )

        btn_copy = QPushButton(tr["donate_btn_copy"])
        btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_copy.setFixedHeight(34)
        btn_copy.setStyleSheet(
            "QPushButton {"
            "  background-color: #171B24; color: #FFFFFF; font-weight: bold; "
            "  border: 1px solid #3DDC97; border-radius: 6px; padding: 0"
            " 14px;"
            "}"
            "QPushButton:hover { background-color: #3DDC97; color: #000000; }"
        )
        btn_copy.clicked.connect(self._copy_crypto_address)

        crypto_box.addWidget(self.crypto_field, 1)
        crypto_box.addWidget(btn_copy)
        layout.addLayout(crypto_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.setStyleSheet(
            "QPushButton {"
            "  background-color: #262B36; color: #FFFFFF; border-radius: 6px;"
            " padding: 6px 18px;"
            "}"
            "QPushButton:hover { background-color: #3B4252; }"
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _copy_crypto_address(self) -> None:
        tr = I18N.get(self.lang, I18N["English"])
        address = DONATION_LINKS["usdt_address"]
        QApplication.clipboard().setText(address)
        QMessageBox.information(
            self,
            tr["donate_copy_title"],
            tr["donate_copy_msg"],
        )


class MainWindow(QMainWindow):

    def __init__(self) -> None:
        super().__init__()

        self._theme_name = "Cyberpunk Dark"
        # BLINDAJE (2026-09-19, patch_qt73 -- pedido explícito del
        # usuario: "añade que el lenguaje se guarde el ultimo que
        # elegiste, es engorroso ser ingles y cambiar a cada rato de
        # idioma"). Antes, `self._current_lang` arrancaba SIEMPRE en
        # "Español" hardcodeado, sin importar qué hubiera elegido el
        # usuario la sesión anterior con `self.combo_lang` -- mismo
        # mecanismo de persistencia que ya usan `_on_workspace_tools_
        # toggled`/`_on_run_cmd_toggled`/`_on_advanced_terminal_toggled`
        # (QSettings, clave propia), aplicado acá. `type=str` con
        # default "Español" cubre tanto la primera corrida (sin valor
        # guardado todavía) como un valor corrupto/de otra versión; el
        # chequeo de membresía evita que un valor inesperado en el
        # Registro (ej. de una versión vieja) deje `combo_lang` o
        # `I18N[self._current_lang]` en un estado no reconocido más
        # abajo en `__init__` -- ambos ya consumen `self._current_lang`
        # tal cual, así que no hace falta tocar nada más para que la
        # restauración se propague (selector de idioma, prompt del
        # propio Orchestrator vía `set_language`, todos los textos de
        # la UI).
        _saved_lang = QSettings("SovNode", "SovNode").value(
            "ui/language", "Español", type=str
        )
        self._current_lang = _saved_lang if _saved_lang in ("Español", "English") else "Español"
        self._turn_count = 0
        self._is_online = False
        self._last_ollama_status: Optional[bool] = None
        self._is_quitting = False
        self._is_recording_voice = False
        self._force_web_search = False
        self._voice_worker: Optional[VoiceRecorderWorker] = None
        self._tts_worker: Optional[TTSWorker] = None
        self.tts_enabled: bool = False
        self._thinking_widget: Optional[ThinkingWidget] = None
        self._terminal_visible = False
        self._last_web_mode: Optional[str] = None

        self.wal = WriteAheadLog()
        self.orchestrator = Orchestrator(wal=self.wal)
        self.orchestrator.set_language(self._current_lang)

        self.ollama_mgr = OllamaProcessManager(
            endpoint=self.orchestrator.ollama_endpoint
        )
        if hasattr(self.ollama_mgr, "ensure_server_running"):
            self.ollama_mgr.ensure_server_running()
        elif hasattr(self.ollama_mgr, "ensure_running"):
            self.ollama_mgr.ensure_running()
        elif hasattr(self.ollama_mgr, "start_server"):
            self.ollama_mgr.start_server()
        else:
            print("ℹ️ [INFO] OllamaProcessManager no requiere auto-arranque explícito.")

        threading.Thread(
            target=self.orchestrator.warm_up_general_model,
            daemon=True,
            name="ModelWarmUp",
        ).start()

        threading.Thread(
            target=prewarm_local_embedding_model,
            daemon=True,
            name="EmbeddingWarmUp",
        ).start()

        self._turn_worker: Optional[StreamTurnWorker] = None
        self._health_worker: Optional[HealthCheckWorker] = None
        self._chat_entries: List[ChatEntry] = []
        self._bubble_widgets: List[MessageBubble] = []
        self._web_card_widgets: List[WebSearchResultsWidget] = []
        self._current_stream_bubble: Optional[MessageBubble] = None

        self._async_executor = AsyncExecutor()

        self._workspace_scanner = WorkspaceScanner()
        self._workspace_watcher = WorkspaceWatcherWorker(
            self.orchestrator, self._workspace_scanner
        )
        self._workspace_watcher.file_indexed.connect(self._on_workspace_file_indexed)
        self._workspace_watcher.file_removed.connect(self._on_workspace_file_removed)
        self._workspace_watcher.scan_error.connect(self._on_workspace_scan_error)
        self._workspace_watcher_started = False

        self._chat_sessions: List[Dict[str, Any]] = [self._new_session_dict()]
        self._active_session_index: int = 0

        # BLINDAJE (bug real, MEDIDO en captura de pantalla 2026-09-06): un
        # mismo pedido aparecía DOS veces seguidas en el chat, con DOS
        # respuestas idénticas (mismo axioma, mismo node_id del WAL, cosa
        # esperable ya que `KnowledgeNode.content_hash()` es determinista
        # sobre el contenido — dos corridas independientes del mismo
        # descubrimiento producen el mismo id, así que la coincidencia NO
        # prueba nada por sí sola sobre si corrió una vez o dos). Causa
        # real: `_send_message` no tenía ninguna guarda de reentrancia —
        # mientras el turno corría, `_set_ui_controls_enabled(False)`
        # bloqueaba paneles laterales (idioma, tema, pestañas, exportar)
        # pero NUNCA el propio campo de texto (`input_field` seguía
        # habilitado y con foco) ni el botón de enviar de forma real (solo
        # se OCULTABA con `setVisible(False)`, lo cual no impide que un
        # evento de click/Enter ya encolado antes de ese instante dispare
        # `clicked`/`send_requested` una segunda vez) — un doble-click
        # rápido en "Enviar", o un segundo Enter antes de que el usuario
        # viera que el primero ya se había enviado, arrancaba un SEGUNDO
        # `StreamTurnWorker` completo para el mismo texto. Este flag es la
        # defensa primaria (inmune a carreras de visibilidad/habilitado):
        # `_send_message` la chequea y sale de inmediato si ya hay un
        # turno en curso, ANTES de tocar cualquier otro estado.
        self._is_processing_turn = False
        self._turn_cost_snapshot_usd: float = 0.0
        self._last_turn_cost_usd: Optional[float] = None

        # patch_qt64 (2026-09-18): estado del toggle "Terminal avanzada"
        # -- ver `_on_advanced_terminal_toggled`/`_advanced_terminal_log`.
        # Los contadores se resetean al arrancar cada turno (`_send_message`)
        # y se leen al cerrarlo (`_on_turn_completed`) para el resumen.
        self._advanced_terminal_enabled: bool = False
        self._adv_turn_tool_count: int = 0
        self._adv_turn_problem_names: List[str] = []

        self._stream_buffer = ""
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(50)
        self._render_timer.timeout.connect(self._flush_stream_buffer)

        self.processing_timer = QTimer(self)
        self.processing_timer.timeout.connect(self._update_elapsed_timer)
        self._elapsed_seconds = 0

        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self._run_health_check)
        self.health_timer.start(8000)

        self.metrics_timer = QTimer(self)
        self.metrics_timer.timeout.connect(self._refresh_metrics)
        self.metrics_timer.start(3000)

        self.vector_autosave_timer = QTimer(self)
        self.vector_autosave_timer.timeout.connect(self._autosave_vector_indices)
        self.vector_autosave_timer.start(600000)

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1024, 700)

        _screen = QApplication.primaryScreen()
        if _screen is not None:
            _avail = _screen.availableGeometry()
            _default_w = max(1024, min(1440, int(_avail.width() * 0.8)))
            _default_h = max(700, min(900, int(_avail.height() * 0.8)))
            self.resize(_default_w, _default_h)
            _frame = self.frameGeometry()
            _frame.moveCenter(_avail.center())
            self.move(_frame.topLeft())
        _saved_geometry = QSettings("SovNode", "SovNode").value(
            "window/geometry"
        )
        if _saved_geometry is not None:
            self.restoreGeometry(_saved_geometry)

        self._create_ui()
        self._load_persisted_workspaces()
        self._load_cloud_settings()
        self._apply_theme(self._theme_name)
        self._create_tray_icon()

        self._run_health_check()
        self._show_first_time_setup_dialog()
        self._add_bubble("assistant", I18N[self._current_lang]["welcome_msg"])

        self._terminal_log(
            I18N[self._current_lang]["log_init_ok"], "ok"
        )

        self._warm_up_whisper_model()

        EQRENDER_BUILD_TAG = "eqrender-2026-08-25.2"
        tr = I18N.get(self._current_lang, I18N["Español"])
        if not math_render.MATPLOTLIB_AVAILABLE:
            self._terminal_log(
                f"⚠️ [{EQRENDER_BUILD_TAG}] " + tr["log_matplotlib_missing"],
                "warn",
            )
        else:
            self._terminal_log(
                f"✅ [{EQRENDER_BUILD_TAG}] " + tr["log_matplotlib_ok"],
                "ok",
            )

    def _play_tts(self, text: str) -> None:
        self._stop_tts()
        self._tts_worker = TTSWorker(text, lang=self._current_lang)
        self._tts_worker.start()

    def _stop_tts(self) -> None:
        if self._tts_worker:
            self._tts_worker.stop()
            self._tts_worker = None

    def _export_chat(self) -> None:
        tr = I18N.get(self._current_lang, I18N["Español"])
        if not self._chat_entries:
            QMessageBox.information(
                self,
                tr["export_no_messages_title"],
                tr["export_no_messages_msg"],
            )
            return

        default_name = (
            f"sovnode_chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        )
        filename, _ = QFileDialog.getSaveFileName(
            self,
            tr["export_dialog_caption"],
            default_name,
            tr["export_dialog_filter"],
        )

        if not filename:
            return

        lines = [
            tr["export_md_header"],
            "",
            f"{tr['export_md_date_label']}: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"{tr['export_md_model_label']}: {self.orchestrator.model}",
            "",
        ]

        for entry in self._chat_entries:
            role = tr["export_md_role_user"] if entry.sender == "user" else "SovNode"
            lines.extend(
                [
                    f"## {role} · {entry.timestamp}",
                    "",
                    entry.content,
                    "",
                ]
            )

        try:
            Path(filename).write_text("\n".join(lines), encoding="utf-8")
            self._terminal_log(
                I18N[self._current_lang]["log_export_ok"].format(filename), "ok"
            )
            QMessageBox.information(
                self,
                tr["export_success_title"],
                tr["export_success_msg"].format(filename),
            )
        except OSError as exc:
            self._terminal_log(
                I18N[self._current_lang]["log_export_error"].format(exc), "error"
            )
            QMessageBox.critical(
                self,
                tr["export_error_title"],
                tr["export_error_msg"].format(exc),
            )

    def _export_training_data(self) -> None:
        """
        IDEA DE ARQUITECTURA (2026-08-19): expone `training_export.py`
        en la UI — recorre el WAL local, junta cada corrección real
        (marcador/ganador/contradicción/idioma/consenso) con el prompt
        original del usuario, y exporta dos JSONL (DPO y SFT) listos
        para un fine-tune local del modelo de 3B sobre sus propios
        errores corregidos. Puramente de LECTURA sobre el WAL — nunca
        modifica la conversación en curso ni el WAL mismo.
        """
        tr = I18N.get(self._current_lang, I18N["Español"])
        wal_path = str(getattr(self.wal, "_log_path", "sovnode.wal"))

        default_dir = str(Path(wal_path).resolve().parent / "training_data")
        out_dir = QFileDialog.getExistingDirectory(
            self,
            tr["training_export_dialog_caption"],
            default_dir,
        )
        if not out_dir:
            return

        try:
            from training_export import export_wal_to_training_data
            stats = export_wal_to_training_data(wal_path, out_dir)
        except Exception as exc:
            self._terminal_log(tr["log_training_export_failed"].format(exc), "error")
            QMessageBox.critical(
                self,
                tr["export_error_title"],
                tr["training_export_error_msg"].format(exc),
            )
            return

        if stats["pairs"] == 0:
            QMessageBox.information(
                self,
                tr["no_corrections_title"],
                tr["no_corrections_msg"],
            )
            return

        self._terminal_log(
            tr["log_training_export_ok"].format(stats["pairs"], out_dir), "ok"
        )
        breakdown = "\n".join(
            f"  · {pair_type}: {count}"
            for pair_type, count in sorted(stats["by_type"].items(), key=lambda kv: -kv[1])
        )
        QMessageBox.information(
            self,
            tr["training_export_success_title"],
            tr["training_export_success_msg"].format(stats["pairs"], out_dir, breakdown)
            + "\n\n" + tr["training_export_files_note"],
        )

    def _hide_to_tray(self) -> None:
        tr = I18N.get(self._current_lang, I18N["Español"])
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.showMinimized()
            return

        self.hide()
        self.tray_icon.showMessage(
            tr["tray_still_active_title"],
            tr["tray_still_active_msg"],
            QSystemTrayIcon.MessageIcon.Information,
            2500,
        )

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(
        self,
        reason: QSystemTrayIcon.ActivationReason,
    ) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._restore_from_tray()

    def _autosave_vector_indices(self) -> None:
        """Ver el comentario junto a `vector_autosave_timer` (arriba, cerca
        de `metrics_timer`) — red de seguridad ante un crash entre dos
        cierres ordenados de la app."""
        if hasattr(self, "orchestrator") and self.orchestrator is not None:
            try:
                self.orchestrator.save_vector_indices()
            except Exception:
                pass

    def _quit_application(self) -> None:
        self._is_quitting = True

        try:
            QSettings("SovNode", "SovNode").setValue(
                "window/geometry", self.saveGeometry()
            )
        except Exception:
            pass

        if getattr(self, "_workspace_watcher_started", False):
            self._workspace_watcher.stop()
            self._workspace_watcher.wait(1500)

        if self._turn_worker is not None and self._turn_worker.isRunning():
            self._turn_worker.wait(1500)

        if self._health_worker is not None and self._health_worker.isRunning():
            self._health_worker.wait(500)

        self._stop_tts()

        if hasattr(self, "orchestrator") and self.orchestrator is not None:
            try:
                self.orchestrator.save_vector_indices()
            except Exception:
                pass

        try:
            self.wal.close()
        except Exception:
            pass

        if hasattr(self, "ollama_mgr"):
            self.ollama_mgr.stop_server()

        self.tray_icon.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        self._quit_application()
        event.accept()
        sys.exit(0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._refresh_bubble_widths)

    def _terminal_log(self, message: str, level: str = "info") -> None:
        """Registra logs en la consola gráfica con auto-scroll activo."""
        if hasattr(self, "terminal_output") and self.terminal_output is not None:
            formatted = format_terminal_log(message, level)
            self.terminal_output.append(formatted)
            cursor = self.terminal_output.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.terminal_output.setTextCursor(cursor)
        else:
            print(f"[{level.upper()}] {message}")

    # Curado a mano, 2026-09-18, leyendo cada `yield PipelineEvent(EventType.LOG, ...)`
    # real que acompaña una red de seguridad en `orchestrator.py::run_turn`
    # (blindaje de archivos, circuit-breakers, guardas genéricas) -- estos
    # 3 prefijos son estables en AMBOS idiomas porque el propio código los
    # deja fijos a propósito (solo el resto del mensaje se traduce). Si se
    # agrega una red de seguridad nueva con un prefijo distinto, sumarlo
    # acá a mano (no hay forma automática de saberlo desde la UI).
    _ADV_TERMINAL_PROBLEM_KEYWORDS = ("circuit-breaker", "blindaje de archivos", "guard:")

    def _advanced_terminal_log(self, message: str) -> None:
        """
        patch_qt64 (2026-09-18, pedido explícito del usuario -- "terminal
        más avanzada"). Reemplaza el color único "system" de
        `_on_worker_log_message` por una clasificación liviana del MISMO
        mensaje que ya emitía `StreamTurnWorker.run()` -- no agrega ningún
        dato nuevo salvo `[BUDGET]`/`[PRESUPUESTO]` (ver
        `orchestrator.py`, `codegen_budget_plan`), que antes solo viajaba
        al WAL. No hay turn_id disponible acá (la UI nunca lo tuvo) --
        el "turno actual" se acota por el ciclo de vida del propio
        `StreamTurnWorker` (ver `_send_message`/`_on_turn_completed`).
        """
        lowered = message.lower()
        is_problem = message.startswith("[ERROR]") or any(
            kw in lowered for kw in self._ADV_TERMINAL_PROBLEM_KEYWORDS
        )

        if is_problem:
            self._adv_turn_problem_names.append(message[:80])
            self._terminal_log(f"  ⚠ {message}", "error")
            return

        if message.startswith("[TOOL] Inicio:"):
            self._adv_turn_tool_count += 1
            self._terminal_log(f"  ▶ {message[len('[TOOL] Inicio:'):].strip()}", "ok")
            return
        if message.startswith("[TOOL] Resultado:"):
            self._terminal_log(f"  ◀ {message[len('[TOOL] Resultado:'):].strip()}", "ok")
            return
        if message.startswith("[ROUTER]") or message.startswith("[BUDGET]") or message.startswith("[PRESUPUESTO]"):
            self._terminal_log(f"  · {message}", "system")
            return
        if message.startswith("[VERIFICATION]"):
            self._terminal_log(f"  · {message}", "warn")
            return

        self._terminal_log(f"  · {message}", "info")

    def _adv_terminal_turn_header(self, prompt: str) -> None:
        self._adv_turn_tool_count = 0
        self._adv_turn_problem_names = []
        tr = I18N[self._current_lang]
        short_prompt = prompt if len(prompt) <= 100 else prompt[:99] + "…"
        self._terminal_log(tr["adv_turn_header"].format(short_prompt), "ok")

    def _adv_terminal_turn_footer(self, turn_cost_usd: float) -> None:
        tr = I18N[self._current_lang]
        if self._adv_turn_problem_names:
            self._terminal_log(
                tr["adv_turn_footer_problems"].format(
                    count=len(self._adv_turn_problem_names),
                    names="; ".join(self._adv_turn_problem_names),
                    cost=turn_cost_usd,
                ),
                "error",
            )
        else:
            self._terminal_log(
                tr["adv_turn_footer_clean"].format(
                    tools=self._adv_turn_tool_count, cost=turn_cost_usd
                ),
                "ok",
            )

    def _remove_thinking_widget(self) -> None:
        if self._thinking_widget is not None:
            self._thinking_widget.stop()
            self._thinking_widget.deleteLater()
            self._thinking_widget = None

    def _on_worker_log_message(self, message: str) -> None:
        """
        Traza de progreso en vivo del turno actual (búsqueda web fase a
        fase, ver StreamTurnWorker.log_message / web_search.py log_cb)
        hacia la consola gráfica — _terminal_log ya antepone
        [HH:MM:SS] y mantiene el auto-scroll activo.

        patch_qt64 (2026-09-18): con "Terminal avanzada" activado, el
        MISMO mensaje se reclasifica y colorea vía
        `_advanced_terminal_log` en vez de mostrarse siempre en "system"
        -- ningún dato nuevo se pierde en el modo clásico, es puramente
        una rama de presentación.
        """
        if self._advanced_terminal_enabled:
            self._advanced_terminal_log(message)
        else:
            self._terminal_log(message, "system")

    def _on_intent_changed(self, icon: str, msg: str) -> None:
        if self._thinking_widget is None:
            self._thinking_widget = ThinkingWidget(self)
            item = self.chat_layout.takeAt(self.chat_layout.count() - 1)
            self.chat_layout.addWidget(self._thinking_widget)
            if item:
                self.chat_layout.addItem(item)
        self._thinking_widget.set_intent(icon, msg)

    def _on_web_results_ready(self, data: dict) -> None:
        tr = I18N[self._current_lang]
        if not data.get("success"):
            self._terminal_log(
                tr["log_web_search_degraded"].format(
                    data.get("status_message", tr["no_detail_fallback"])
                ),
                "warn",
            )

        # BLINDAJE (2026-09-07, auditoría de búsqueda web — pedido
        # explícito del usuario: "siempre que se active la búsqueda web,
        # siempre agarre las tres imágenes relacionadas al tema"): antes
        # había DOS gates acá — uno por TEMA (`should_show_visual_search_
        # cards`, deportes/noticias/bio únicamente) y otro por CALIDAD de
        # miniatura (`has_real_image`, que se anulaba a sí mismo casi
        # siempre porque el scraping de artículos nunca extraía una
        # imagen real — ver el BLINDAJE en `_fetch_article_content_and_
        # image` de web_search.py). Juntos hacían que la tarjeta visual
        # se omitiera para casi cualquier consulta, sin importar el
        # tema. `_fetch_rich_web_search_impl` ahora corre una búsqueda de
        # imágenes DEDICADA al tema (`search_topic_images()`), así que
        # siempre hay hasta 3 fotos reales disponibles cuando la búsqueda
        # web se activó — la tarjeta se muestra siempre, y si para una
        # fuente puntual igual no hay miniatura (fallo de red puntual),
        # el propio widget ya sabe caer a su respaldo de iniciales
        # (`_OverlayImageCard`) en vez de a un hueco vacío.
        web_widget = WebSearchResultsWidget(
            data, self.chat_widget, lang=self._current_lang
        )

        if hasattr(self, "chat_scroll") and self.chat_scroll:
            web_widget.set_available_width(self.chat_scroll.viewport().width() - 30)
        self._web_card_widgets.append(web_widget)

        if self._current_stream_bubble:
            idx = self.chat_layout.indexOf(self._current_stream_bubble)
            if idx != -1:
                self.chat_layout.insertWidget(idx, web_widget)
                return

        item = self.chat_layout.takeAt(self.chat_layout.count() - 1)
        self.chat_layout.addWidget(web_widget)
        if item:
            self.chat_layout.addItem(item)

    def _on_chunk_received(self, chunk: str, ast_error: str) -> None:
        self._remove_thinking_widget()
        self._stream_buffer += chunk

    def _update_elapsed_timer(self) -> None:
        self._elapsed_seconds += 1
        if hasattr(self, "processing_label") and self.processing_label:
            proc_text = I18N[self._current_lang]["processing"]
            self.processing_label.setText(f"⚡ {proc_text} ({self._elapsed_seconds}s)")

    def _run_health_check(self) -> None:
        if (
            hasattr(self, "health_worker")
            and self.health_worker
            and self.health_worker.isRunning()
        ):
            return
        self._health_worker = HealthCheckWorker(
            self.orchestrator.ollama_endpoint
        )
        self._health_worker.completed.connect(self._on_health_check_completed)
        self._health_worker.start()

    def _on_health_check_completed(self, is_online: bool) -> None:
        self._is_online = is_online
        tr = I18N[self._current_lang]
        if hasattr(self, "status_label") and self.status_label:
            status_text = tr["status_online"] if is_online else tr["status_offline"]
            self.status_label.setText(status_text)
            self.status_label.setToolTip(
                tr["header_online"] if is_online else tr["header_offline"]
            )
            self._set_node_status_color("success" if is_online else "danger")

            status_label = "Online" if is_online else "Offline"
            status_changed = is_online != self._last_ollama_status
            if status_changed:
                self._terminal_log(
                    tr["log_ollama_status"].format(status_label),
                    "ok" if is_online else "warn",
                )
                self._last_ollama_status = is_online
            else:
                logger.debug("Health-check de Ollama sin cambios: %s", status_label)

    def _show_first_time_setup_dialog(self) -> None:
        pass

    def _refresh_bubble_widths(self) -> None:
        if not hasattr(self, "chat_scroll") or not self.chat_scroll:
            return
        available_width = self.chat_scroll.viewport().width() - 30
        for bubble in self._bubble_widgets:
            bubble.set_available_width(available_width)
        for web_card in self._web_card_widgets:
            web_card.set_available_width(available_width)

    def _create_ui(self) -> None:
        """Construye la interfaz completa de SovNode con panel lateral, controles y consola."""
        tr = I18N[self._current_lang]

        central_widget = QWidget(self)
        central_widget.setObjectName("centralWidget")
        self.setCentralWidget(central_widget)

        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        # patch_qt70 (2026-09-19, pedido explícito del usuario: "mejora
        # esta parte de la interfaz para que todo sea distinguible" --
        # sobre un screenshot real donde el panel "Motor de Generación"
        # se veía como un bloque de cajas vacías/ilegibles). 280px ya
        # quedaba justo antes de Gemini; con el selector de proveedor +
        # el campo de modelo nuevos (textos más largos: "Gemini
        # (Google)", "gemini-3.6-flash", "Probar conexión") necesitaba
        # más aire. El ancho fijo de 300px, que antes vivía en este
        # `sidebar` (QFrame), ahora vive en `sidebar_scroll` (ver más
        # abajo, patch_qt72) -- mismo ancho final, un contenedor
        # distinto lo aplica.
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(16, 20, 16, 20)
        sidebar_layout.setSpacing(14)

        app_title = QLabel(APP_NAME)
        app_title.setObjectName("appTitle")
        app_subtitle = QLabel("Sovereign AI Node")
        app_subtitle.setObjectName("appSubtitle")

        sidebar_layout.addWidget(app_title)
        sidebar_layout.addWidget(app_subtitle)

        theme_card = QFrame()
        theme_card.setObjectName("sidebarCard")
        tc_layout = QVBoxLayout(theme_card)
        tc_layout.setContentsMargins(10, 10, 10, 10)
        self.theme_title_label = QLabel(tr["theme_title"])
        self.theme_title_label.setObjectName("sectionTitle")
        self.combo_theme = QComboBox()
        self.combo_theme.addItems(list(THEMES.keys()))
        self.combo_theme.setCurrentText(self._theme_name)
        self.combo_theme.currentTextChanged.connect(self._on_theme_changed)
        tc_layout.addWidget(self.theme_title_label)
        tc_layout.addWidget(self.combo_theme)

        lang_card = QFrame()
        lang_card.setObjectName("sidebarCard")
        lc_layout = QVBoxLayout(lang_card)
        lc_layout.setContentsMargins(10, 10, 10, 10)
        self.lang_title_label = QLabel(tr["lang_title"])
        self.lang_title_label.setObjectName("sectionTitle")
        self.combo_lang = QComboBox()
        self.combo_lang.addItems(["Español", "English"])
        self.combo_lang.setCurrentText(self._current_lang)
        self.combo_lang.currentTextChanged.connect(self._on_lang_changed)
        lc_layout.addWidget(self.lang_title_label)
        lc_layout.addWidget(self.combo_lang)
        self.section_appearance = CollapsibleSection(
            "🎨", tr["sidebar_section_appearance"], expanded=False,
        )
        self.section_appearance.add_widget(theme_card)
        self.section_appearance.add_widget(lang_card)
        sidebar_layout.addWidget(self.section_appearance)

        status_card = QFrame()
        status_card.setObjectName("sidebarCard")
        sc_layout = QVBoxLayout(status_card)
        sc_layout.setContentsMargins(10, 10, 10, 10)
        self.status_title_label = QLabel(tr["status_card"])
        self.status_title_label.setObjectName("sectionTitle")
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(9, 9)
        self.status_label = QLabel(tr["status_checking"])
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignTop)
        status_row.addWidget(self.status_label, 1)
        self._set_node_status_color("warning")
        sc_layout.addWidget(self.status_title_label)
        sc_layout.addLayout(status_row)

        model_card = QFrame()
        model_card.setObjectName("sidebarCard")
        mc_layout = QVBoxLayout(model_card)
        mc_layout.setContentsMargins(10, 10, 10, 10)

        self.btn_download_model = QPushButton(tr["btn_download_model"])
        self.btn_download_model.setObjectName("secondaryButton")
        self.btn_download_model.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_download_model.clicked.connect(
            self._open_model_download_dialog
        )
        mc_layout.addWidget(self.btn_download_model)


        engine_card = QFrame()
        engine_card.setObjectName("sidebarCard")
        ec_layout = QVBoxLayout(engine_card)
        ec_layout.setContentsMargins(10, 12, 10, 12)
        # patch_qt69 (2026-09-19, pedido explícito del usuario: "mejora
        # las dimensiones de los cuadros en la interfaz para que no se
        # aplasten entre si"): esta tarjeta pasó de 6 widgets (antes de
        # patch_qt66/67) a 13 -- el selector de proveedor y el campo de
        # modelo nuevos se sumaron con el mismo espaciado uniforme de
        # siempre, sin ningún respiro extra entre subgrupos lógicos
        # (motor / proveedor+modelo / credenciales / presupuesto), así
        # que a simple vista todo el panel se veía como un solo bloque
        # apretado. Espaciado base subido de 6 a 9px, más un
        # `addSpacing(8)` extra (ver más abajo) antes de cada widget que
        # arranca un subgrupo nuevo -- separa visualmente sin tocar el
        # tamaño de cada control individual (ya definido en el QSS de
        # QComboBox/QLineEdit).
        ec_layout.setSpacing(9)

        self.engine_title_label = QLabel(tr["engine_title"])
        self.engine_title_label.setObjectName("sectionTitle")
        ec_layout.addWidget(self.engine_title_label)

        self.combo_engine = QComboBox()
        self.combo_engine.addItem(tr["engine_local"], "local")
        self.combo_engine.addItem(tr["engine_cloud"], "cloud")
        self.combo_engine.currentIndexChanged.connect(self._on_engine_changed)
        ec_layout.addWidget(self.combo_engine)

        # patch_qt66 (2026-09-19, pedido explícito del usuario: "se me
        # acabaron los creditos, podemos usar el modelo de gemini
        # tambien?"): selector de PROVEEDOR de Nube, independiente del
        # combo Local/Nube de arriba -- ese elige el MOTOR (local vs.
        # API externa), este elige CUÁL API externa una vez que "Nube"
        # ya está seleccionado. Ver `_on_cloud_provider_changed` y
        # `Orchestrator.set_cloud_backend(..., provider=...)`.
        ec_layout.addSpacing(8)  # patch_qt69: respiro antes del subgrupo "proveedor+modelo"
        self.cloud_provider_label = QLabel(tr["cloud_provider_title"])
        self.cloud_provider_label.setObjectName("sectionTitle")
        ec_layout.addWidget(self.cloud_provider_label)

        self.combo_cloud_provider = QComboBox()
        self.combo_cloud_provider.addItem(
            tr["cloud_provider_anthropic"], self.orchestrator.CLOUD_PROVIDER_ANTHROPIC
        )
        self.combo_cloud_provider.addItem(
            tr["cloud_provider_gemini"], self.orchestrator.CLOUD_PROVIDER_GEMINI
        )
        self.combo_cloud_provider.currentIndexChanged.connect(
            self._on_cloud_provider_changed
        )
        ec_layout.addWidget(self.combo_cloud_provider)

        # patch_qt67 (2026-09-19, bug real, MEDIDO -- ver el BLINDAJE
        # junto a "cloud_model_title" en I18N): campo de MODELO editable
        # a mano, sin esto el `model_id` de cada proveedor solo se podía
        # cambiar con un patch de código. Se guarda por proveedor igual
        # que la API key (`cloud/model_id_anthropic`/`cloud/model_id_
        # gemini`, ver `_on_cloud_model_edited`/`_load_cloud_settings`).
        self.cloud_model_label = QLabel(tr["cloud_model_title"])
        self.cloud_model_label.setObjectName("sectionTitle")
        ec_layout.addWidget(self.cloud_model_label)

        self.cloud_model_input = QLineEdit()
        self.cloud_model_input.setPlaceholderText(tr["cloud_model_placeholder"])
        self.cloud_model_input.setToolTip(tr["cloud_model_tooltip"])
        self.cloud_model_input.editingFinished.connect(self._on_cloud_model_edited)
        ec_layout.addWidget(self.cloud_model_input)

        ec_layout.addSpacing(8)  # patch_qt69: respiro antes del subgrupo "credenciales"
        self.cloud_key_input = QLineEdit()
        self.cloud_key_input.setPlaceholderText(tr["cloud_key_placeholder"])
        self.cloud_key_input.setToolTip(tr["cloud_key_tooltip"])
        self.cloud_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.cloud_key_input.editingFinished.connect(self._on_cloud_key_edited)
        ec_layout.addWidget(self.cloud_key_input)

        self.btn_test_cloud_key = QPushButton(tr["btn_test_cloud_key"])
        self.btn_test_cloud_key.setObjectName("secondaryButton")
        self.btn_test_cloud_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_test_cloud_key.clicked.connect(self._on_test_cloud_key_clicked)
        ec_layout.addWidget(self.btn_test_cloud_key)

        # patch_qt65 (2026-09-18, pedido explícito del usuario tras ver
        # que abrir OTRA copia de la app -- el build en dist/ -- ya
        # traía su key cargada: `cloud_key_input` la guarda en
        # `QSettings("SovNode","SovNode")` -- el Registro de Windows --
        # apenas termina de editarla, sin pedir confirmación. Se
        # mantiene ese guardado automático (más cómodo si sos el único
        # que usa este equipo), pero ahora hay un botón explícito para
        # borrar esa key persistida sin tener que ir al Registro a mano.
        self.btn_forget_cloud_key = QPushButton(tr["btn_forget_cloud_key"])
        self.btn_forget_cloud_key.setObjectName("secondaryButton")
        self.btn_forget_cloud_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_forget_cloud_key.setToolTip(tr["btn_forget_cloud_key_tooltip"])
        self.btn_forget_cloud_key.clicked.connect(self._on_forget_cloud_key_clicked)
        ec_layout.addWidget(self.btn_forget_cloud_key)

        ec_layout.addSpacing(8)  # patch_qt69: respiro antes del subgrupo "presupuesto"
        self.cloud_budget_label = QLabel(tr["cloud_budget_title"])
        self.cloud_budget_label.setObjectName("sectionTitle")
        ec_layout.addWidget(self.cloud_budget_label)

        self.combo_cloud_budget = QComboBox()
        self.combo_cloud_budget.addItem(tr["cloud_budget_option_1c"], 1)
        self.combo_cloud_budget.addItem(tr["cloud_budget_option_2c"], 2)
        self.combo_cloud_budget.addItem(tr["cloud_budget_option_4c"], 4)
        self.combo_cloud_budget.addItem(tr["cloud_budget_option_8c"], 8)
        self.combo_cloud_budget.currentIndexChanged.connect(self._on_cloud_budget_changed)
        ec_layout.addWidget(self.combo_cloud_budget)

        self.cloud_usage_label = QLabel(tr["cloud_usage_idle"])
        self.cloud_usage_label.setObjectName("cloudUsageLabel")
        self.cloud_usage_label.setWordWrap(True)
        self.cloud_usage_label.setStyleSheet("font-size: 10px;")
        ec_layout.addWidget(self.cloud_usage_label)

        self.section_engine = CollapsibleSection(
            "⚙️", tr["sidebar_section_engine"], expanded=True,
        )
        self.section_engine.add_widget(status_card)
        self.section_engine.add_widget(model_card)
        self.section_engine.add_widget(engine_card)
        sidebar_layout.addWidget(self.section_engine)

        workspaces_card = QFrame()
        workspaces_card.setObjectName("sidebarCard")
        wc_layout = QVBoxLayout(workspaces_card)
        wc_layout.setContentsMargins(10, 10, 10, 10)

        self.workspaces_title_label = QLabel("Workspaces")
        self.workspaces_title_label.setObjectName("sectionTitle")
        wc_layout.addWidget(self.workspaces_title_label)

        self.chk_workspace_tools = QCheckBox(tr["workspace_tools_toggle"])
        self.chk_workspace_tools.setObjectName("workspaceToolsToggle")
        self.chk_workspace_tools.setToolTip(tr["workspace_tools_toggle_tooltip"])
        self.chk_workspace_tools.toggled.connect(self._on_workspace_tools_toggled)
        wc_layout.addWidget(self.chk_workspace_tools)

        # BLINDAJE (2026-09-17): interruptor PROPIO para run_cmd,
        # independiente del de arriba -- ver el comentario junto a
        # `Orchestrator.run_cmd_enabled`.
        self.chk_run_cmd = QCheckBox(tr["run_cmd_toggle"])
        self.chk_run_cmd.setObjectName("runCmdToggle")
        self.chk_run_cmd.setToolTip(tr["run_cmd_toggle_tooltip"])
        self.chk_run_cmd.toggled.connect(self._on_run_cmd_toggled)
        wc_layout.addWidget(self.chk_run_cmd)

        self.workspaces_list = QListWidget()
        self.workspaces_list.setObjectName("workspacesList")
        self.workspaces_list.setToolTip(tr["workspaces_list_tooltip"])
        self.workspaces_list.setMaximumHeight(110)
        self.workspaces_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.workspaces_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        wc_layout.addWidget(self.workspaces_list)

        workspaces_btn_row = QHBoxLayout()
        self.btn_add_workspace = QPushButton(tr["btn_add_workspace"])
        self.btn_add_workspace.setObjectName("secondaryButton")
        self.btn_add_workspace.clicked.connect(self._on_add_workspace_clicked)
        self.btn_remove_workspace = QPushButton(tr["btn_remove_workspace"])
        self.btn_remove_workspace.setObjectName("secondaryButton")
        self.btn_remove_workspace.clicked.connect(self._on_remove_workspace_clicked)
        workspaces_btn_row.addWidget(self.btn_add_workspace)
        workspaces_btn_row.addWidget(self.btn_remove_workspace)
        wc_layout.addLayout(workspaces_btn_row)

        self.section_workspace = CollapsibleSection(
            "📁", tr["sidebar_section_workspace"], expanded=True,
        )
        self.section_workspace.add_widget(workspaces_card)
        sidebar_layout.addWidget(self.section_workspace)

        sidebar_layout.addStretch()

        self.btn_export = QPushButton(tr["btn_export"])
        self.btn_export.setObjectName("secondaryButton")
        self.btn_export.clicked.connect(self._export_chat)

        self.btn_export_training = QPushButton(tr["btn_export_training"])
        self.btn_export_training.setObjectName("secondaryButton")
        self.btn_export_training.setToolTip(tr["btn_export_training_tooltip"])
        self.btn_export_training.clicked.connect(self._export_training_data)

        sidebar_layout.addWidget(self.btn_export)
        sidebar_layout.addWidget(self.btn_export_training)

        # patch_qt72 (2026-09-19, bug real, MEDIDO por screenshot del
        # usuario: el panel "Motor de Generación" mostraba texto de
        # filas distintas (etiqueta "Modelo:", el campo de key
        # enmascarado, "Presupuesto de nube por turno") amontonado /
        # pisándose entre sí -- pedido explícito: "que tengan una
        # escala mínima donde se pueda ver... sin tocar el diseño").
        # Causa raíz: `sidebar` (el QFrame con las 3 secciones
        # plegables: Apariencia, Motor, Workspace) se agregaba
        # DIRECTO a `main_layout` sin ningún `QScrollArea` de por
        # medio. La cantidad de controles del sidebar creció mucho
        # esta sesión (selector de proveedor + campo de modelo +
        # botones nuevos de Gemini, patch_qt66/67) y la ventana
        # respeta una geometría MÍNIMA/PERSISTIDA (`self.setMinimumSize
        # (1024, 700)`, más lo que haya quedado guardado en
        # `QSettings("SovNode","SovNode").value("window/geometry")` de
        # una sesión anterior, de antes de que existieran estos
        # campos) -- si esa altura no alcanza para el contenido
        # natural del sidebar, no hay forma de avisarle al usuario
        # salvo comprimir/recortar filas. No lo cambiamos: le damos al
        # sidebar una salida decente, envolviéndolo en un
        # `QScrollArea` con `setWidgetResizable(True)` (el sidebar
        # mantiene su ancho fijo vía el scroll area, no vía sí mismo)
        # y sin scroll horizontal -- así cada control SIEMPRE se
        # dibuja a su tamaño natural (el QSS no se tocó, cero cambios
        # de color/fuente/padding, solo el contenedor que lo aloja) y
        # si no entra todo en alto, aparece una barra de scroll
        # vertical en vez de amontonar texto. `self.config_panel`
        # (el botón de engranaje lo muestra/oculta) pasa a apuntar al
        # scroll area en vez de al QFrame de adentro, para que
        # mostrar/ocultar siga funcionando igual que antes.
        sidebar_scroll = QScrollArea()
        sidebar_scroll.setObjectName("sidebarScroll")
        sidebar_scroll.setWidget(sidebar)
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        sidebar_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        sidebar_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        sidebar_scroll.setFixedWidth(300)
        self.config_panel = sidebar_scroll
        sidebar_scroll.setVisible(False)
        main_layout.addWidget(sidebar_scroll)

        content_container = QWidget()
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        tab_bar_container = QFrame()
        tab_bar_container.setObjectName("chatTabBarContainer")
        tab_bar_row = QHBoxLayout(tab_bar_container)
        tab_bar_row.setContentsMargins(12, 6, 12, 0)
        tab_bar_row.setSpacing(6)

        self.chat_tab_bar = QTabBar()
        self.chat_tab_bar.setObjectName("chatTabBar")
        self.chat_tab_bar.setExpanding(False)
        self.chat_tab_bar.setMovable(False)
        self.chat_tab_bar.setDrawBase(False)
        self.chat_tab_bar.setDocumentMode(True)
        self.chat_tab_bar.addTab(tr["tab_new_chat_title"])
        self._add_tab_close_button(0)
        self.chat_tab_bar.currentChanged.connect(self._on_tab_bar_changed)

        self.btn_new_chat = QPushButton("+")
        self.btn_new_chat.setObjectName("newTabButton")
        self.btn_new_chat.setFixedSize(30, 30)
        self.btn_new_chat.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new_chat.setToolTip(tr["btn_new_chat_tooltip"])
        self.btn_new_chat.clicked.connect(self._on_new_tab_clicked)

        tab_bar_row.addWidget(self.chat_tab_bar, 0)
        tab_bar_row.addWidget(self.btn_new_chat)
        tab_bar_row.addStretch(1)

        content_layout.addWidget(tab_bar_container)

        header_bar = QFrame()
        header_bar.setObjectName("headerBar")
        hb_layout = QHBoxLayout(header_bar)
        hb_layout.setContentsMargins(20, 12, 20, 12)

        hb_info = QVBoxLayout()
        self.header_title_label = QLabel(tr["header_title"])
        self.header_title_label.setObjectName("headerTitle")
        self.header_subtitle_label = QLabel(tr["header_subtitle"])
        self.header_subtitle_label.setObjectName("headerSubtitle")
        hb_info.addWidget(self.header_title_label)
        hb_info.addWidget(self.header_subtitle_label)

        self.btn_toggle_terminal = QPushButton(tr["terminal_btn_show"])
        self.btn_toggle_terminal.setObjectName("terminalToggleButton")
        self.btn_toggle_terminal.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_terminal.clicked.connect(self._toggle_terminal)

        self.btn_donate = QPushButton(tr["btn_donate"])
        self.btn_donate.setObjectName("actionButton")
        self.btn_donate.clicked.connect(
            lambda: DonationDialog(self._current_lang, self).exec()
        )

        self.btn_config = QPushButton("")
        self.btn_config.setObjectName("secondaryButton")
        self.btn_config.setFixedSize(40, 40)
        self.btn_config.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_config.setToolTip(tr["btn_config_tooltip"])
        self.btn_config.clicked.connect(self._toggle_config_panel)

        self.header_status = QLabel("")
        self.header_status.setObjectName("headerStatusBadge")
        self.header_status.setVisible(False)

        self.header_cost_badge = QLabel("")
        self.header_cost_badge.setObjectName("headerCostBadge")
        self.header_cost_badge.setVisible(False)

        hb_layout.addLayout(hb_info)
        hb_layout.addStretch()
        hb_layout.addWidget(self.header_cost_badge)
        hb_layout.addWidget(self.header_status)
        hb_layout.addWidget(self.btn_donate)
        hb_layout.addWidget(self.btn_config)
        hb_layout.addWidget(self.btn_toggle_terminal)

        content_layout.addWidget(header_bar)

        self.chat_scroll = SmartChatScrollArea(self)
        self.chat_scroll.setObjectName("chatScrollArea")
        self.chat_scroll.pinned_state_changed.connect(self._on_pinned_state_changed)

        self.chat_widget = QWidget()
        self.chat_widget.setObjectName("chatWidget")
        self.chat_layout = QVBoxLayout(self.chat_widget)
        # BUG REAL, REPORTADO (2026-09-16 -- "ese espacio en blanco
        # que se puede scrollear como si hubiera el límite allá
        # abajo, en la nada... pasa también con los mensajes
        # normales"): el margen inferior era 90px FIJOS, sin ningún
        # overlay/posicionamiento absoluto en este archivo que
        # necesite esa reserva (el input, el indicador de progreso y
        # la barra de estado son filas normales de `content_layout`,
        # por debajo de `chat_scroll`, no superpuestas) -- era 90px
        # reales de espacio vacío scrolleable después del último
        # mensaje. Bajado a 20px, igual al margen superior (padding
        # vertical simétrico) en vez de un valor 4.5x mayor sin
        # justificación.
        self.chat_layout.setContentsMargins(24, 20, 24, 20)
        self.chat_layout.setSpacing(14)
        self.chat_layout.addStretch()
        self.chat_scroll.setWidget(self.chat_widget)
        self.chat_scroll.setWidgetResizable(True)

        content_layout.addWidget(self.chat_scroll, 1)

        scroll_btn_row = QHBoxLayout()
        scroll_btn_row.setContentsMargins(0, 0, 0, 4)
        scroll_btn_row.addStretch()
        self.scroll_to_bottom_btn = QPushButton(tr["jump_to_bottom"])
        self.scroll_to_bottom_btn.setObjectName("scrollBottomButton")
        self.scroll_to_bottom_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scroll_to_bottom_btn.setFixedHeight(28)
        self.scroll_to_bottom_btn.setVisible(False)
        self.scroll_to_bottom_btn.clicked.connect(self._jump_to_bottom)
        scroll_btn_row.addWidget(self.scroll_to_bottom_btn)
        scroll_btn_row.addStretch()
        content_layout.addLayout(scroll_btn_row)

        processing_container = QWidget()
        pc_layout = QVBoxLayout(processing_container)
        pc_layout.setContentsMargins(24, 0, 24, 4)

        self.processing_label = QLabel("")
        self.processing_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.processing_label.setStyleSheet("font-size: 11px; color: #8B92A5;")

        pc_layout.addWidget(self.processing_label)
        content_layout.addWidget(processing_container)

        self._attached_image_path: Optional[str] = None
        self._attached_file_path: Optional[str] = None
        self._attached_file_content: Optional[str] = None
        self.attachment_preview_container = QWidget()
        outer_preview_layout = QHBoxLayout(self.attachment_preview_container)
        outer_preview_layout.setContentsMargins(24, 0, 24, 8)
        outer_preview_layout.setSpacing(0)

        attachment_card = QFrame()
        attachment_card.setObjectName("attachmentPreviewCard")
        attach_preview_layout = QHBoxLayout(attachment_card)
        attach_preview_layout.setContentsMargins(10, 8, 10, 8)
        attach_preview_layout.setSpacing(8)

        self.attachment_thumb_label = QLabel()
        self.attachment_thumb_label.setFixedSize(40, 40)
        self.attachment_thumb_label.setScaledContents(True)
        self.attachment_thumb_label.setStyleSheet("border-radius: 6px;")

        self.attachment_type_badge_label = QLabel("")
        self.attachment_type_badge_label.setObjectName("codeLanguagePill")
        self.attachment_type_badge_label.setFixedSize(40, 24)
        self.attachment_type_badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.attachment_type_badge_label.setStyleSheet(
            f"font-size: 10px; font-weight: bold; color: {_ACTIVE_THEME['bg']}; "
            f"background-color: {_ACTIVE_THEME['accent']}; "
            "border-radius: 4px; padding: 2px 4px;"
        )
        self.attachment_type_badge_label.setVisible(False)

        self.attachment_name_label = QLabel("")
        self.attachment_name_label.setStyleSheet(
            f"font-size: 11px; color: {_ACTIVE_THEME['secondary']};"
        )

        self.attachment_remove_btn = QPushButton("✕")
        self.attachment_remove_btn.setFixedSize(22, 22)
        self.attachment_remove_btn.setObjectName("secondaryButton")
        self.attachment_remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.attachment_remove_btn.clicked.connect(self._clear_attachment)

        attach_preview_layout.addWidget(self.attachment_thumb_label)
        attach_preview_layout.addWidget(self.attachment_type_badge_label)
        attach_preview_layout.addWidget(self.attachment_name_label)
        attach_preview_layout.addStretch(1)
        attach_preview_layout.addWidget(self.attachment_remove_btn)
        outer_preview_layout.addWidget(attachment_card)
        outer_preview_layout.addStretch(1)
        self.attachment_preview_container.setVisible(False)
        content_layout.addWidget(self.attachment_preview_container)

        input_container = QWidget()
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(24, 8, 24, 20)
        input_layout.setSpacing(10)

        self.attach_button = QPushButton("")
        self.attach_button.setFixedSize(48, 48)
        self.attach_button.setObjectName("secondaryButton")
        self.attach_button.setToolTip(tr["attach_tooltip"])
        self.attach_button.clicked.connect(self._open_attach_file_dialog)

        self.input_field = PromptTextEdit(self)
        self.input_field.setObjectName("inputField")
        self.input_field.setPlaceholderText(tr["placeholder"])
        self.input_field.send_requested.connect(self._send_message)
        self.input_field.file_dropped.connect(self._on_file_dropped_for_indexing)
        self.input_field.image_pasted.connect(self._on_image_pasted)
        self.input_field.image_path_pasted.connect(self._on_image_path_attached)
        self.input_field.text_file_attached.connect(self._on_text_file_attached)

        self.mic_button = QPushButton("")
        self.mic_button.setFixedSize(48, 48)
        self.mic_button.setObjectName("secondaryButton")
        self.mic_button.setToolTip(tr["mic_tooltip"])
        self.mic_button.clicked.connect(self._toggle_voice_recording)

        self.tts_toggle_button = QPushButton("")
        self.tts_toggle_button.setFixedSize(48, 48)
        self.tts_toggle_button.setObjectName("secondaryButton")
        self.tts_toggle_button.setToolTip(tr["tts_toggle_tooltip_off"])
        self.tts_toggle_button.clicked.connect(self._toggle_tts_enabled)


        self.stop_button = QPushButton(tr["btn_stop"])
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setFixedHeight(48)
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._stop_generation)

        self.send_button = QPushButton(tr["btn_send"])
        self.send_button.setObjectName("sendButton")
        self.send_button.setFixedSize(80, 48)
        self.send_button.clicked.connect(self._send_message)

        input_layout.addWidget(self.attach_button)
        input_layout.addWidget(self.input_field, 1)
        input_layout.addWidget(self.mic_button)
        input_layout.addWidget(self.tts_toggle_button)
        input_layout.addWidget(self.stop_button)
        input_layout.addWidget(self.send_button)

        content_layout.addWidget(input_container)

        self.terminal_panel = QFrame()
        self.terminal_panel.setObjectName("terminalPanel")
        self.terminal_panel.setFixedHeight(160)
        self.terminal_panel.setVisible(False)

        term_layout = QVBoxLayout(self.terminal_panel)
        term_layout.setContentsMargins(12, 10, 12, 10)

        term_header = QHBoxLayout()
        # patch_qt65 (2026-09-18, langfix): guardados como self.* (antes
        # eran variables locales) para poder re-traducirlos en
        # `_on_lang_changed` -- quedaban fijos en español aunque el resto
        # de la UI ya estuviera en inglés (mismo patrón "BLINDAJE" que
        # workspaces_list/btn_add_workspace/chk_workspace_tools ahí
        # mismo, solo que a estos dos nunca los agregaron a esa función).
        self.term_title = QLabel(tr["terminal_console_title"])
        self.term_title.setObjectName("terminalTitle")
        term_title = self.term_title

        self.metrics_label = QLabel("")
        self.metrics_label.setObjectName("metricsLabel")

        self.stage_metrics_label = QLabel("")
        self.stage_metrics_label.setObjectName("stageMetricsLabel")
        self.stage_metrics_label.setWordWrap(True)

        self.btn_clear_term = QPushButton(tr["btn_clear_terminal"])
        self.btn_clear_term.setObjectName("terminalClearButton")
        self.btn_clear_term.clicked.connect(self._clear_terminal)
        btn_clear_term = self.btn_clear_term

        # patch_qt64 (2026-09-18, pedido explícito del usuario -- "un
        # botón en la terminal... 'terminal más avanzada'"): interruptor
        # que reorganiza el mismo stream de log_message por turno,
        # resaltando en rojo las redes de seguridad que dispararon. Ver
        # `_on_advanced_terminal_toggled`/`_advanced_terminal_log`.
        self.chk_advanced_terminal = QCheckBox(tr["advanced_terminal_toggle"])
        self.chk_advanced_terminal.setObjectName("advancedTerminalToggle")
        self.chk_advanced_terminal.setToolTip(tr["advanced_terminal_toggle_tooltip"])
        self.chk_advanced_terminal.toggled.connect(self._on_advanced_terminal_toggled)

        term_header.addWidget(term_title)
        term_header.addStretch()
        term_header.addWidget(self.metrics_label)
        term_header.addWidget(self.chk_advanced_terminal)
        term_header.addWidget(btn_clear_term)

        self.terminal_output = QTextEdit()
        self.terminal_output.setObjectName("terminalOutput")
        self.terminal_output.setReadOnly(True)

        term_layout.addLayout(term_header)
        term_layout.addWidget(self.stage_metrics_label)
        term_layout.addWidget(self.terminal_output)

        content_layout.addWidget(self.terminal_panel)
        main_layout.addWidget(content_container, 1)

    def _on_workspace_tools_toggled(self, checked: bool) -> None:
        """
        Espejo de UI del interruptor `Orchestrator.workspace_tools_
        enabled` (pedido explícito del usuario, 2026-09-15). Persistido
        con QSettings -- mismo mecanismo que el resto de settings
        restaurados en `_load_cloud_settings` -- para que sobreviva a un
        reinicio de la app en vez de volver siempre al default (False).

        patch_qt68 (2026-09-19, pedido explícito del usuario: "mejora el
        sistema de workspace si esta desactivado no deberia encenderse
        en ningun momento"): hasta este patch, el toggle solo controlaba
        si el modelo podía usar las herramientas read_file/write_file/
        edit_file/list_dir -- el `WorkspaceWatcherWorker` (escaneo de
        disco en segundo plano para indexar RAG/búsqueda semántica, ver
        `_load_persisted_workspaces`/`_on_add_workspace_clicked` y el
        BLINDAJE de 2026-09-02 sobre los "DOS conceptos de workspace
        totalmente desconectados") seguía corriendo SIN IMPORTAR este
        interruptor -- en contradicción directa con el propio tooltip de
        este control ("Apagado (por defecto): SovNode nunca... sin tocar
        el disco"). Bug real, MEDIDO por el usuario en vivo (log): con
        las herramientas ya desactivadas ("Herramientas de workspace
        DESACTIVADAS" en consola), un turno posterior igual mostró
        "📚 [Workspace] 'minesweeper.py' reindexado: 4 fragmento(s)" --
        el watcher seguía leyendo el archivo del disco de fondo.

        Ahora el toggle también arranca/para el hilo del watcher: apagar
        "Habilitar herramientas de archivo" detiene CUALQUIER lectura de
        disco del lado de Workspaces (no solo la escritura vía
        tool-calling), y volver a prenderlo retoma el escaneo de las
        carpetas que el usuario ya tenía agregadas -- sin que haga falta
        quitarlas y volver a agregarlas. `stop()` es cooperativo (revisa
        un flag entre archivos, nunca mata el hilo a mitad de una
        llamada a FAISS -- ver el docstring de `WorkspaceWatcherWorker`)
        y `wait(1500)` espera a que termine antes de dejar
        `_workspace_watcher_started` en `False`, para que un toggle
        ON/OFF/ON rápido no intente arrancar un hilo que todavía no
        terminó de pararse.
        """
        self.orchestrator.workspace_tools_enabled = bool(checked)
        settings = QSettings("SovNode", "SovNode")
        settings.setValue("workspace/tools_enabled", bool(checked))
        if checked:
            if not self._workspace_watcher_started:
                self._workspace_watcher.start()
                self._workspace_watcher_started = True
        else:
            if self._workspace_watcher_started:
                self._workspace_watcher.stop()
                self._workspace_watcher.wait(1500)
                self._workspace_watcher_started = False
        tr = I18N[self._current_lang]
        self._terminal_log(
            tr["log_workspace_tools_enabled"] if checked
            else tr["log_workspace_tools_disabled"],
            "info",
        )

    def _on_run_cmd_toggled(self, checked: bool) -> None:
        """
        Espejo de UI de `Orchestrator.run_cmd_enabled` (pedido explícito
        del usuario, 2026-09-17 -- "run_cmd no debería estar con las
        herramientas de archivo, ¿no?"). Interruptor INDEPENDIENTE de
        `_on_workspace_tools_toggled`: antes de este fix, run_cmd estaba
        siempre disponible para el modelo sin importar ningún checkbox
        de la UI. Mismo mecanismo de persistencia que el de workspace
        (QSettings, clave propia para no pisar la otra).
        """
        self.orchestrator.run_cmd_enabled = bool(checked)
        settings = QSettings("SovNode", "SovNode")
        settings.setValue("workspace/run_cmd_enabled", bool(checked))
        tr = I18N[self._current_lang]
        self._terminal_log(
            tr["log_run_cmd_enabled"] if checked
            else tr["log_run_cmd_disabled"],
            "info",
        )

    def _on_advanced_terminal_toggled(self, checked: bool) -> None:
        """
        patch_qt64 (2026-09-18, pedido explícito del usuario -- "un botón
        en la terminal... 'terminal más avanzada'"). Mismo mecanismo de
        persistencia que `_on_workspace_tools_toggled`/
        `_on_run_cmd_toggled` (QSettings, clave propia). No cambia NADA
        del pipeline real -- es puramente cómo se renderiza el mismo
        stream de `log_message` que ya existía (ver `_on_worker_log_message`/
        `_advanced_terminal_log`).
        """
        self._advanced_terminal_enabled = bool(checked)
        settings = QSettings("SovNode", "SovNode")
        settings.setValue("ui/advanced_terminal_enabled", bool(checked))
        tr = I18N[self._current_lang]
        self._terminal_log(
            tr["log_advanced_terminal_enabled"] if checked
            else tr["log_advanced_terminal_disabled"],
            "info",
        )

    def _on_theme_changed(self, theme_name: str) -> None:
        self._apply_theme(theme_name)
        self._terminal_log(
            I18N[self._current_lang]["log_theme_changed"].format(theme_name), "info"
        )

    def _apply_cloud_key_field_for_provider(self, provider: str) -> None:
        """
        patch_qt66 (2026-09-19): placeholder/tooltip de `cloud_key_input`
        según el proveedor de Nube activo -- llamado al cargar
        configuración, al cambiar de proveedor y al cambiar de idioma
        (ver `_on_lang_changed`), para que los tres caminos muestren
        siempre el texto correcto en vez de quedarse con el de
        Anthropic a secas como pasaba antes de este patch.
        """
        tr = I18N[self._current_lang]
        if provider == self.orchestrator.CLOUD_PROVIDER_GEMINI:
            self.cloud_key_input.setPlaceholderText(tr["cloud_key_placeholder_gemini"])
            self.cloud_key_input.setToolTip(tr["cloud_key_tooltip_gemini"])
        else:
            self.cloud_key_input.setPlaceholderText(tr["cloud_key_placeholder"])
            self.cloud_key_input.setToolTip(tr["cloud_key_tooltip"])

    def _load_cloud_settings(self) -> None:
        """
        Restaura el motor de generación + proveedor + API key
        persistidos entre reinicios (ver la nota junto a `engine_card`
        en _create_ui). Aplica el estado tanto a la UI como al
        orquestador real (`Orchestrator.set_cloud_backend`) para que un
        turno arrancado apenas se abre la app ya respete lo que el
        usuario dejó configurado la última vez.

        patch_qt66 (2026-09-19, pedido explícito del usuario: "se me
        acabaron los creditos, podemos usar el modelo de gemini
        tambien?"): la API key y el modelo ahora se guardan POR
        PROVEEDOR (`cloud/api_key_<provider>`/`cloud/model_id_<provider>`)
        en vez de una sola key compartida -- migra de forma NO
        DESTRUCTIVA desde las claves viejas `cloud/api_key`/
        `cloud/model_id` (se interpretan como las de Anthropic, el único
        proveedor que existía antes de este patch) si las nuevas
        todavía no existen, sin borrar las viejas -- así una instalación
        con la key de Claude ya guardada no la pierde al actualizar.
        """
        settings = QSettings("SovNode", "SovNode")
        enabled = settings.value("cloud/enabled", False, type=bool)
        provider = settings.value(
            "cloud/provider", self.orchestrator.CLOUD_PROVIDER_ANTHROPIC, type=str
        )
        if provider not in (
            self.orchestrator.CLOUD_PROVIDER_ANTHROPIC,
            self.orchestrator.CLOUD_PROVIDER_GEMINI,
        ):
            provider = self.orchestrator.CLOUD_PROVIDER_ANTHROPIC

        _legacy_api_key = settings.value("cloud/api_key", "", type=str) or ""
        _legacy_model_id = settings.value("cloud/model_id", "", type=str) or ""
        api_key_anthropic = settings.value(
            "cloud/api_key_anthropic", _legacy_api_key, type=str
        ) or ""
        model_id_anthropic = settings.value(
            "cloud/model_id_anthropic",
            _legacy_model_id or self.orchestrator.CLOUD_DEFAULT_MODEL,
            type=str,
        )
        api_key_gemini = settings.value("cloud/api_key_gemini", "", type=str) or ""
        model_id_gemini = settings.value(
            "cloud/model_id_gemini", self.orchestrator.GEMINI_DEFAULT_MODEL, type=str
        )
        if provider == self.orchestrator.CLOUD_PROVIDER_GEMINI:
            api_key, model_id = api_key_gemini, model_id_gemini
        else:
            api_key, model_id = api_key_anthropic, model_id_anthropic
        budget_cents = settings.value("cloud/output_budget_cents", 1, type=int)
        if budget_cents not in (1, 2, 4, 8):
            budget_cents = 1

        self.combo_cloud_provider.blockSignals(True)
        _prov_idx = self.combo_cloud_provider.findData(provider)
        self.combo_cloud_provider.setCurrentIndex(_prov_idx if _prov_idx >= 0 else 0)
        self.combo_cloud_provider.blockSignals(False)
        self._apply_cloud_key_field_for_provider(provider)
        self.cloud_key_input.setText(api_key)
        self.cloud_model_input.setText(model_id)
        self.combo_engine.blockSignals(True)
        self.combo_engine.setCurrentIndex(1 if enabled else 0)
        self.combo_engine.blockSignals(False)
        self.combo_cloud_budget.blockSignals(True)
        _budget_idx = self.combo_cloud_budget.findData(budget_cents)
        self.combo_cloud_budget.setCurrentIndex(_budget_idx if _budget_idx >= 0 else 0)
        self.combo_cloud_budget.blockSignals(False)
        self._update_cloud_key_visibility()

        self.orchestrator.set_cloud_backend(
            bool(enabled),
            api_key=(api_key or None),
            model_id=model_id,
            provider=provider,
        )
        self.orchestrator.set_cloud_output_budget(int(budget_cents))
        self._refresh_cloud_usage_label()
        self._set_header_cost_badge()

        workspace_tools_enabled = settings.value(
            "workspace/tools_enabled", False, type=bool
        )
        self.chk_workspace_tools.blockSignals(True)
        self.chk_workspace_tools.setChecked(bool(workspace_tools_enabled))
        self.chk_workspace_tools.blockSignals(False)
        self.orchestrator.workspace_tools_enabled = bool(workspace_tools_enabled)

        # BLINDAJE (2026-09-17): restaura el interruptor INDEPENDIENTE de
        # run_cmd -- ver `_on_run_cmd_toggled`. Default False, mismo
        # criterio que el de workspace.
        run_cmd_enabled = settings.value(
            "workspace/run_cmd_enabled", False, type=bool
        )
        self.chk_run_cmd.blockSignals(True)
        self.chk_run_cmd.setChecked(bool(run_cmd_enabled))
        self.chk_run_cmd.blockSignals(False)
        self.orchestrator.run_cmd_enabled = bool(run_cmd_enabled)

        # patch_qt64 (2026-09-18): restaura "Terminal avanzada" -- mismo
        # criterio que los dos toggles de arriba (QSettings, default False).
        advanced_terminal_enabled = settings.value(
            "ui/advanced_terminal_enabled", False, type=bool
        )
        self.chk_advanced_terminal.blockSignals(True)
        self.chk_advanced_terminal.setChecked(bool(advanced_terminal_enabled))
        self.chk_advanced_terminal.blockSignals(False)
        self._advanced_terminal_enabled = bool(advanced_terminal_enabled)

    def _update_cloud_key_visibility(self) -> None:
        is_cloud = self.combo_engine.currentData() == "cloud"
        # patch_qt66 (2026-09-19): el selector de proveedor solo tiene
        # sentido cuando el motor "Nube" está activo, mismo criterio que
        # el resto de los widgets de esta sección.
        self.cloud_provider_label.setVisible(is_cloud)
        self.combo_cloud_provider.setVisible(is_cloud)
        self.cloud_model_label.setVisible(is_cloud)
        self.cloud_model_input.setVisible(is_cloud)
        self.cloud_key_input.setVisible(is_cloud)
        self.btn_test_cloud_key.setVisible(is_cloud)
        self.btn_forget_cloud_key.setVisible(is_cloud)
        self.cloud_budget_label.setVisible(is_cloud)
        self.combo_cloud_budget.setVisible(is_cloud)
        if hasattr(self, "header_cost_badge"):
            self.header_cost_badge.setVisible(is_cloud)

    def _on_engine_changed(self, _index: int) -> None:
        is_cloud = self.combo_engine.currentData() == "cloud"
        self._update_cloud_key_visibility()
        settings = QSettings("SovNode", "SovNode")
        settings.setValue("cloud/enabled", is_cloud)
        api_key = self.cloud_key_input.text().strip() or None
        self.orchestrator.set_cloud_backend(is_cloud, api_key=api_key)
        tr = I18N[self._current_lang]
        engine_name = tr["engine_cloud"] if is_cloud else tr["engine_local"]
        self._terminal_log(tr["log_engine_changed"].format(engine_name), "info")

    def _on_cloud_provider_changed(self, _index: int) -> None:
        """
        patch_qt66 (2026-09-19, pedido explícito del usuario: "se me
        acabaron los creditos, podemos usar el modelo de gemini
        tambien?"): cambia de proveedor de Nube en caliente -- carga la
        API key/modelo GUARDADOS de ese proveedor (cada uno vive en su
        propia clave de QSettings, ver `_load_cloud_settings`) en vez de
        dejar en el campo la key del proveedor anterior, que no le
        serviría de nada a la API nueva.
        """
        provider = self.combo_cloud_provider.currentData()
        if provider not in (
            self.orchestrator.CLOUD_PROVIDER_ANTHROPIC,
            self.orchestrator.CLOUD_PROVIDER_GEMINI,
        ):
            return
        settings = QSettings("SovNode", "SovNode")
        settings.setValue("cloud/provider", provider)
        self._apply_cloud_key_field_for_provider(provider)

        _is_gemini = provider == self.orchestrator.CLOUD_PROVIDER_GEMINI
        _key_setting = "cloud/api_key_gemini" if _is_gemini else "cloud/api_key_anthropic"
        _model_setting = (
            "cloud/model_id_gemini" if _is_gemini else "cloud/model_id_anthropic"
        )
        _default_model = (
            self.orchestrator.GEMINI_DEFAULT_MODEL if _is_gemini
            else self.orchestrator.CLOUD_DEFAULT_MODEL
        )
        api_key = settings.value(_key_setting, "", type=str) or ""
        model_id = settings.value(_model_setting, _default_model, type=str)
        self.cloud_key_input.setText(api_key)
        self.cloud_model_input.setText(model_id)

        self.orchestrator.set_cloud_backend(
            self.orchestrator.cloud_backend_enabled,
            api_key=(api_key or None),
            model_id=model_id,
            provider=provider,
        )
        tr = I18N[self._current_lang]
        self._terminal_log(
            tr["log_cloud_provider_changed"].format(self.combo_cloud_provider.currentText()),
            "info",
        )

    def _on_cloud_budget_changed(self, _index: int) -> None:
        cents = self.combo_cloud_budget.currentData()
        if cents is None:
            return
        cents = int(cents)
        settings = QSettings("SovNode", "SovNode")
        settings.setValue("cloud/output_budget_cents", cents)
        self.orchestrator.set_cloud_output_budget(cents)
        tr = I18N[self._current_lang]
        self._terminal_log(
            tr["log_cloud_budget_changed"].format(self.combo_cloud_budget.currentText()),
            "info",
        )

    def _on_cloud_key_edited(self) -> None:
        api_key = self.cloud_key_input.text().strip()
        settings = QSettings("SovNode", "SovNode")
        # patch_qt66 (2026-09-19): la key se guarda bajo la clave del
        # PROVEEDOR activo, no en una sola clave compartida -- ver
        # `_load_cloud_settings`. Para Anthropic se sigue escribiendo
        # también la clave legacy `cloud/api_key`, por compatibilidad
        # hacia atrás con una copia de la app sin este patch.
        provider = (
            self.combo_cloud_provider.currentData()
            if hasattr(self, "combo_cloud_provider")
            else self.orchestrator.CLOUD_PROVIDER_ANTHROPIC
        ) or self.orchestrator.CLOUD_PROVIDER_ANTHROPIC
        _is_gemini = provider == self.orchestrator.CLOUD_PROVIDER_GEMINI
        settings.setValue(
            "cloud/api_key_gemini" if _is_gemini else "cloud/api_key_anthropic", api_key
        )
        if not _is_gemini:
            settings.setValue("cloud/api_key", api_key)
        self.orchestrator.set_cloud_backend(
            self.orchestrator.cloud_backend_enabled, api_key=(api_key or None)
        )

    def _on_cloud_model_edited(self) -> None:
        """
        patch_qt67 (2026-09-19, bug real, MEDIDO -- ver el BLINDAJE junto
        a "cloud_model_title" en I18N): guarda el ID de modelo tipeado a
        mano, por PROVEEDOR (mismo criterio que la API key). Un campo
        vacío cae al default de fábrica del proveedor activo en vez de
        mandarle a la API un `model_id` vacío.
        """
        provider = (
            self.combo_cloud_provider.currentData()
            if hasattr(self, "combo_cloud_provider")
            else self.orchestrator.CLOUD_PROVIDER_ANTHROPIC
        ) or self.orchestrator.CLOUD_PROVIDER_ANTHROPIC
        _is_gemini = provider == self.orchestrator.CLOUD_PROVIDER_GEMINI
        _default_model = (
            self.orchestrator.GEMINI_DEFAULT_MODEL if _is_gemini
            else self.orchestrator.CLOUD_DEFAULT_MODEL
        )
        model_id = self.cloud_model_input.text().strip() or _default_model
        self.cloud_model_input.setText(model_id)
        settings = QSettings("SovNode", "SovNode")
        settings.setValue(
            "cloud/model_id_gemini" if _is_gemini else "cloud/model_id_anthropic",
            model_id,
        )
        if not _is_gemini:
            # Compatibilidad hacia atrás -- mismo criterio que la key.
            settings.setValue("cloud/model_id", model_id)
        self.orchestrator.set_cloud_backend(
            self.orchestrator.cloud_backend_enabled, model_id=model_id
        )
        tr = I18N[self._current_lang]
        self._terminal_log(tr["log_cloud_model_changed"].format(model_id), "info")

    def _on_forget_cloud_key_clicked(self) -> None:
        """
        patch_qt65 (2026-09-18): borra la API key persistida en
        QSettings("SovNode","SovNode") -- Registro de Windows, compartido
        por cualquier copia/build de la app en este mismo usuario de
        Windows -- y la limpia también del campo y de la sesión actual
        en memoria (`orchestrator.cloud_api_key`). No apaga el backend
        cloud ni borra el resto de la config (presupuesto, etc.), solo
        la key.

        patch_qt66 (2026-09-19): borra SOLO la key del proveedor
        ACTIVO (Claude o Gemini, según el selector) -- las dos se
        guardan por separado desde este patch, así que "olvidar" no
        debe borrar la del otro proveedor que el usuario pueda tener
        cargada.
        """
        tr = I18N[self._current_lang]
        settings = QSettings("SovNode", "SovNode")
        provider = (
            self.combo_cloud_provider.currentData()
            if hasattr(self, "combo_cloud_provider")
            else self.orchestrator.CLOUD_PROVIDER_ANTHROPIC
        ) or self.orchestrator.CLOUD_PROVIDER_ANTHROPIC
        _is_gemini = provider == self.orchestrator.CLOUD_PROVIDER_GEMINI
        settings.remove("cloud/api_key_gemini" if _is_gemini else "cloud/api_key_anthropic")
        if not _is_gemini:
            settings.remove("cloud/api_key")
        self.cloud_key_input.clear()
        self.orchestrator.set_cloud_backend(
            self.orchestrator.cloud_backend_enabled, api_key=None
        )
        self._terminal_log(tr["log_cloud_key_forgotten"], "info")

    def _on_test_cloud_key_clicked(self) -> None:
        tr = I18N[self._current_lang]
        api_key = self.cloud_key_input.text().strip()
        if not api_key:
            self._terminal_log(tr["cloud_test_no_key"], "warn")
            return
        self.btn_test_cloud_key.setEnabled(False)
        # patch_qt66 (2026-09-19): prueba la conexión contra el
        # proveedor ACTIVO -- ver `CloudKeyCheckWorker`, que ahora
        # también sabe hablar con la Gemini API además de la de
        # Anthropic.
        provider = (
            self.combo_cloud_provider.currentData()
            if hasattr(self, "combo_cloud_provider")
            else self.orchestrator.CLOUD_PROVIDER_ANTHROPIC
        ) or self.orchestrator.CLOUD_PROVIDER_ANTHROPIC
        provider_label = (
            self.combo_cloud_provider.currentText()
            if hasattr(self, "combo_cloud_provider") else "Claude"
        )
        self._terminal_log(tr["cloud_test_testing"].format(provider_label), "info")
        model_id = self.orchestrator.cloud_model_id
        self._cloud_key_worker = CloudKeyCheckWorker(api_key, model_id, provider=provider)
        self._cloud_key_worker.completed.connect(self._on_cloud_key_tested)
        self._cloud_key_worker.start()

    def _on_cloud_key_tested(self, ok: bool, detail: str) -> None:
        tr = I18N[self._current_lang]
        self.btn_test_cloud_key.setEnabled(True)
        if ok:
            self._terminal_log(
                tr["cloud_test_ok"].format(self.orchestrator.cloud_model_id), "ok"
            )
        else:
            self._terminal_log(tr["cloud_test_fail"].format(detail), "error")

    def _refresh_cloud_usage_label(self) -> None:
        if not hasattr(self, "cloud_usage_label"):
            return
        totals = self.orchestrator.cloud_usage_totals
        tr = I18N[self._current_lang]
        if not totals.get("calls"):
            self.cloud_usage_label.setText(tr["cloud_usage_idle"])
            return
        self.cloud_usage_label.setText(
            tr["cloud_usage_fmt"].format(
                int(totals["calls"]),
                int(totals["input_tokens"]),
                int(totals["output_tokens"]),
                totals["cost_usd"],
            )
        )

    def _on_lang_changed(self, lang_name: str) -> None:
        self._current_lang = lang_name
        # BLINDAJE (2026-09-19, patch_qt73 -- ver el BLINDAJE en
        # `__init__` junto a la carga de `ui/language`): persiste la
        # elección ACÁ, en el único lugar donde el usuario de verdad
        # cambia de idioma (el combo `self.combo_lang`), para que la
        # próxima vez que se abra SovNode arranque en este mismo idioma
        # en vez de volver a "Español" por defecto.
        QSettings("SovNode", "SovNode").setValue("ui/language", lang_name)
        self.orchestrator.set_language(lang_name)
        tr = I18N[lang_name]

        self.input_field.setPlaceholderText(tr["placeholder"])
        self.send_button.setText(tr["btn_send"])
        self.stop_button.setText(tr["btn_stop"])
        self.btn_new_chat.setToolTip(tr["btn_new_chat_tooltip"])
        self.btn_config.setToolTip(tr["btn_config_tooltip"])
        self.btn_export.setText(tr["btn_export"])
        self.btn_export_training.setText(tr["btn_export_training"])
        self.btn_donate.setText(tr["btn_donate"])
        self.engine_title_label.setText(tr["engine_title"])
        self.combo_engine.setItemText(0, tr["engine_local"])
        self.combo_engine.setItemText(1, tr["engine_cloud"])
        # patch_qt66 (2026-09-19): retraduce también el selector de
        # proveedor de Nube y, según cuál esté activo, el placeholder/
        # tooltip correcto del campo de key (antes esto último se
        # retraducía siempre a los textos de Anthropic a secas).
        if hasattr(self, "cloud_provider_label"):
            self.cloud_provider_label.setText(tr["cloud_provider_title"])
        if hasattr(self, "combo_cloud_provider"):
            self.combo_cloud_provider.setItemText(0, tr["cloud_provider_anthropic"])
            self.combo_cloud_provider.setItemText(1, tr["cloud_provider_gemini"])
            self._apply_cloud_key_field_for_provider(
                self.combo_cloud_provider.currentData()
                or self.orchestrator.CLOUD_PROVIDER_ANTHROPIC
            )
        else:
            self.cloud_key_input.setPlaceholderText(tr["cloud_key_placeholder"])
            self.cloud_key_input.setToolTip(tr["cloud_key_tooltip"])
        # patch_qt67 (2026-09-19): retraduce el campo de modelo editable.
        if hasattr(self, "cloud_model_label"):
            self.cloud_model_label.setText(tr["cloud_model_title"])
        if hasattr(self, "cloud_model_input"):
            self.cloud_model_input.setPlaceholderText(tr["cloud_model_placeholder"])
            self.cloud_model_input.setToolTip(tr["cloud_model_tooltip"])
        self.btn_test_cloud_key.setText(tr["btn_test_cloud_key"])
        if hasattr(self, "btn_forget_cloud_key"):
            self.btn_forget_cloud_key.setText(tr["btn_forget_cloud_key"])
            self.btn_forget_cloud_key.setToolTip(tr["btn_forget_cloud_key_tooltip"])
        self.cloud_budget_label.setText(tr["cloud_budget_title"])
        for _cb_i, _cb_cents in enumerate((1, 2, 4, 8)):
            self.combo_cloud_budget.setItemText(_cb_i, tr[f"cloud_budget_option_{_cb_cents}c"])
        self._refresh_cloud_usage_label()
        for i, session in enumerate(self._chat_sessions):
            if not session.get("title"):
                self.chat_tab_bar.setTabText(i, tr["tab_new_chat_title"])
        for i in range(self.chat_tab_bar.count()):
            close_btn = self.chat_tab_bar.tabButton(
                i, QTabBar.ButtonPosition.RightSide
            )
            if close_btn is not None:
                close_btn.setToolTip(tr["tab_close_tooltip"])
        self.header_title_label.setText(tr["header_title"])
        self.header_subtitle_label.setText(tr["header_subtitle"])
        self.scroll_to_bottom_btn.setText(tr["jump_to_bottom"])

        self.theme_title_label.setText(tr["theme_title"])
        self.lang_title_label.setText(tr["lang_title"])
        self.status_title_label.setText(tr["status_card"])

        term_btn_txt = (
            tr["terminal_btn_hide"]
            if self._terminal_visible
            else tr["terminal_btn_show"]
        )
        self.btn_toggle_terminal.setText(term_btn_txt)

        if hasattr(self, "btn_download_model"):
            self.btn_download_model.setText(tr["btn_download_model"])

        # BLINDAJE (bug real reportado): estos widgets se construían UNA
        # sola vez en _create_ui() con texto/tooltip hardcodeado en
        # español, así que cambiar el idioma acá (el combo de arriba) no
        # los actualizaba — quedaban en español aunque el resto de la
        # interfaz ya estuviera en inglés.
        if hasattr(self, "workspaces_list"):
            self.workspaces_list.setToolTip(tr["workspaces_list_tooltip"])
        if hasattr(self, "btn_add_workspace"):
            self.btn_add_workspace.setText(tr["btn_add_workspace"])
        if hasattr(self, "btn_remove_workspace"):
            self.btn_remove_workspace.setText(tr["btn_remove_workspace"])
        if hasattr(self, "chk_workspace_tools"):
            self.chk_workspace_tools.setText(tr["workspace_tools_toggle"])
            self.chk_workspace_tools.setToolTip(tr["workspace_tools_toggle_tooltip"])
        if hasattr(self, "chk_run_cmd"):
            self.chk_run_cmd.setText(tr["run_cmd_toggle"])
            self.chk_run_cmd.setToolTip(tr["run_cmd_toggle_tooltip"])
        if hasattr(self, "btn_export_training"):
            self.btn_export_training.setToolTip(tr["btn_export_training_tooltip"])
        if hasattr(self, "mic_button"):
            self.mic_button.setToolTip(tr["mic_tooltip"])
        if hasattr(self, "attach_button"):
            self.attach_button.setToolTip(tr["attach_tooltip"])

        # patch_qt65 (2026-09-18, langfix): mismo bug de arriba, mismos
        # 3 widgets del panel de consola que faltaban en esta lista
        # (reportado por el usuario viendo "CONSOLA DE SISTEMA / LOGS" /
        # "Terminal avanzada" / "Limpiar" en español con la UI en
        # inglés, en una grabación de showcase).
        if hasattr(self, "term_title"):
            self.term_title.setText(tr["terminal_console_title"])
        if hasattr(self, "btn_clear_term"):
            self.btn_clear_term.setText(tr["btn_clear_terminal"])
        if hasattr(self, "chk_advanced_terminal"):
            self.chk_advanced_terminal.setText(tr["advanced_terminal_toggle"])
            self.chk_advanced_terminal.setToolTip(tr["advanced_terminal_toggle_tooltip"])

        self._set_header_status_badge(self._last_web_mode)
        self._set_header_cost_badge()
        if hasattr(self, "section_appearance"):
            self.section_appearance.set_title(tr["sidebar_section_appearance"])
            self.section_engine.set_title(tr["sidebar_section_engine"])
            self.section_workspace.set_title(tr["sidebar_section_workspace"])

        self._terminal_log(tr["log_lang_changed"].format(lang_name), "info")

    def _open_model_download_dialog(self) -> None:
        """Abre el diálogo para ingresar/seleccionar y descargar un tag de modelo de Ollama."""
        dialog = ModelDownloadDialog(
            self.orchestrator.ollama_endpoint, self._current_lang, self
        )
        dialog.exec()

        if dialog.downloaded_tag:
            self._terminal_log(
                I18N[self._current_lang]["log_model_downloaded"].format(
                    dialog.downloaded_tag
                ),
                "ok",
            )

    def _toggle_terminal(self) -> None:
        self._terminal_visible = not self._terminal_visible
        self.terminal_panel.setVisible(self._terminal_visible)
        tr = I18N[self._current_lang]
        self.btn_toggle_terminal.setText(
            tr["terminal_btn_hide"]
            if self._terminal_visible
            else tr["terminal_btn_show"]
        )
        if self._terminal_visible:
            self._refresh_metrics()

    def _clear_terminal(self) -> None:
        if hasattr(self, "terminal_output") and self.terminal_output:
            self.terminal_output.clear()

    def _toggle_config_panel(self) -> None:
        """⚙: muestra/oculta el panel con lo que antes vivía fijo en la barra lateral."""
        self.config_panel.setVisible(not self.config_panel.isVisible())

    _STAGE_LABELS: Dict[str, str] = {
        "router": "Router",
        "retrieval": "Retrieval",
        "generation": "Generación",
        "tools": "Herramientas",
        "memory": "Memoria",
        "other": "Otro",
    }

    @staticmethod
    def _format_ms(ms: float) -> str:
        if ms >= 1000:
            return f"{ms / 1000:.1f}s"
        return f"{ms:.0f}ms"

    def _format_generation_by_label(self, profile: Dict[str, Any]) -> str:
        """
        Sub-desglose de "Generación" por perf_label (ver
        Orchestrator.TurnProfiler.finalize() -> generation_by_label):
        cada llamada LLM del turno ya trae una etiqueta interna
        ("LeanSingle", "ToT-A", "ToT-B", "ToT-Synthesis", "Verify",
        "Router0.5B", etc.) - esto suma el tiempo real por etiqueta y lo
        muestra entre paréntesis, ordenado de mayor a menor, para ver
        de inmediato cuál llamada concreta explica el total de
        "Generación" (p.ej. la respuesta principal vs. una verificación
        vs. una rama de Tree-of-Thought). Si solo hubo UNA etiqueta no
        agrega nada (el total ya lo dice todo).
        """
        by_label = profile.get("generation_by_label") or {}
        if len(by_label) <= 1:
            return ""
        ranked = sorted(by_label.items(), key=lambda kv: kv[1], reverse=True)
        parts = [f"{label} {self._format_ms(ms)}" for label, ms in ranked]
        return " (" + " · ".join(parts) + ")"

    def _format_generation_perf(self, profile: Dict[str, Any]) -> str:
        """
        Desglose REAL prefill-vs-decode del turno completo (ver
        Orchestrator.TurnProfiler.finalize() -> generation_prefill_s /
        generation_decode_s / generation_prompt_eval_count /
        generation_eval_count). El prefill (leer el prompt) es en
        teoría paralelo y más RÁPIDO por token que el decode
        (secuencial, token a token) - si acá sale al revés (prefill con
        menos tok/s que decode) es la señal de que el KV-cache se está
        invalidando entre llamadas (p.ej. el router 0.5B pidiendo otro
        `num_ctx` justo antes de la llamada principal) en vez de
        reusarse, forzando repaginar el prompt entero cada vez - ver el
        comentario en TurnProfiler.finalize().
        """
        prefill_s = profile.get("generation_prefill_s", 0.0) or 0.0
        decode_s = profile.get("generation_decode_s", 0.0) or 0.0
        prefill_tok = profile.get("generation_prompt_eval_count", 0) or 0
        decode_tok = profile.get("generation_eval_count", 0) or 0
        if prefill_tok <= 0 and decode_tok <= 0:
            return ""
        prefill_tok_s = (prefill_tok / prefill_s) if prefill_s > 0 else 0.0
        decode_tok_s = (decode_tok / decode_s) if decode_s > 0 else 0.0
        bits: List[str] = []
        if prefill_tok > 0:
            bits.append(
                f"prefill {self._format_ms(prefill_s * 1000)}/{prefill_tok}tok "
                f"({prefill_tok_s:.0f}tok/s)"
            )
        if decode_tok > 0:
            bits.append(
                f"decode {self._format_ms(decode_s * 1000)}/{decode_tok}tok "
                f"({decode_tok_s:.0f}tok/s)"
            )
        if not bits:
            return ""
        return "  ⚙ " + " · ".join(bits)

    def _format_stage_profile(self, profile: Dict[str, Any]) -> str:
        """
        Convierte el dict de `Orchestrator.TurnProfiler.finalize()` (ver
        orchestrator.py) en una línea legible para `stage_metrics_label`.
        Omite etapas en 0ms (la mayoría de los turnos no pasan por TODAS
        — p.ej. sin tool calls, "tools" queda en 0 y no vale la pena
        mostrarlo) y solo agrega "Otro" si es una porción no trivial del
        turno (>5%) — el resto normalmente es overhead de formateo/
        contexto, no un cuello de botella real que valga señalar.
        """
        stages = profile.get("stages", {}) or {}
        total_ms = profile.get("total_ms", 0.0) or 0.0
        order = ("router", "retrieval", "generation", "tools", "memory")
        bits: List[str] = []
        for name in order:
            ms = stages.get(name, 0.0)
            if ms <= 0:
                continue
            piece = f"{self._STAGE_LABELS[name]} {self._format_ms(ms)}"
            if name == "generation":
                piece += self._format_generation_by_label(profile)
            bits.append(piece)
        other_ms = stages.get("other", 0.0)
        if total_ms > 0 and other_ms > 0.05 * total_ms:
            bits.append(f"{self._STAGE_LABELS['other']} {self._format_ms(other_ms)}")
        if not bits:
            return ""
        perf_suffix = self._format_generation_perf(profile)
        return (
            "Últ. turno: " + " · ".join(bits)
            + f"  (total {self._format_ms(total_ms)})"
            + perf_suffix
        )

    def _refresh_metrics(self) -> None:
        """
        Actualiza self.metrics_label (VRAM + tok/s) dentro del panel de
        terminal. Solo hace trabajo real si el panel está visible — igual
        que el resto de la consola gráfica, pensado para no gastar ciclos
        en una ventana minimizada/oculta. get_system_telemetry() (sys_
        optimizer.py) ya trae su propia caché TTL de 5s, así que la
        mayoría de estos ticks de 3s solo leen ese caché en vez de
        spawnear PowerShell/nvidia-smi de nuevo. Nunca lanza — una
        métrica que falla en leerse simplemente no se muestra esta vez.
        """
        if not hasattr(self, "metrics_label") or not self.terminal_panel.isVisible():
            return

        parts: List[str] = []
        with contextlib.suppress(Exception):
            telemetry = get_system_telemetry()
            if "gpu_vram_used_mb" in telemetry and "gpu_vram_total_mb" in telemetry:
                parts.append(
                    f"VRAM {telemetry['gpu_vram_used_mb']}/{telemetry['gpu_vram_total_mb']}MB"
                )
            elif "ram_used_gb" in telemetry:
                parts.append(f"RAM {telemetry['ram_used_gb']}GB")

        with contextlib.suppress(Exception):
            stats = getattr(self.orchestrator, "last_generation_stats", None)
            if stats:
                parts.append(f"{stats.get('decode_tok_s', 0):.1f} tok/s")

        self.metrics_label.setText(" · ".join(parts))

        if hasattr(self, "stage_metrics_label"):
            stage_text = ""
            with contextlib.suppress(Exception):
                profile = getattr(self.orchestrator, "last_turn_profile", None)
                if profile:
                    stage_text = self._format_stage_profile(profile)
            self.stage_metrics_label.setText(stage_text)

    def _new_session_dict(self) -> Dict[str, Any]:
        return {"id": uuid.uuid4().hex, "title": None, "entries": []}

    def _snapshot_current_session(self) -> None:
        """Guarda el transcript VISIBLE actual en su sesión, antes de reemplazarlo por el de otra pestaña."""
        if 0 <= self._active_session_index < len(self._chat_sessions):
            self._chat_sessions[self._active_session_index]["entries"] = list(self._chat_entries)

    def _replay_session_entries(self, entries: List["ChatEntry"]) -> None:
        for entry in entries:
            self._add_bubble(
                entry.sender, entry.content, trace=entry.trace, is_error=entry.is_error
            )

    def _reset_chat_widgets_and_memory(self) -> None:
        """
        Núcleo compartido entre _clear_chat() (antes: único botón "Nueva
        conversación") y _activate_session() al entrar a una pestaña CON
        historial guardado: limpia burbujas/tarjetas visibles y resetea
        la memoria de corto plazo del Orchestrator. Deliberadamente NO
        agrega el mensaje de bienvenida ni loguea nada — cada caller
        decide eso según si hay o no un transcript para reemplazarlo.
        """
        self._stop_tts()
        for bubble in self._bubble_widgets:
            bubble.deleteLater()
        self._bubble_widgets.clear()
        for web_card in self._web_card_widgets:
            web_card.deleteLater()
        self._web_card_widgets.clear()
        self._chat_entries.clear()
        self._set_header_status_badge(None)
        self.orchestrator.clear_conversation_memory()

    def _clear_chat(self) -> None:
        self._reset_chat_widgets_and_memory()
        self._add_bubble("assistant", I18N[self._current_lang]["new_chat_msg"])
        self._terminal_log(I18N[self._current_lang]["log_new_chat"], "info")

    def _activate_session(self, index: int) -> None:
        """
        Reemplaza el transcript VISIBLE por el de self._chat_sessions[index].
        Asume que quien llama ya hizo _snapshot_current_session() de la
        pestaña saliente si correspondía guardarla.

        LIMITACIÓN CONOCIDA (documentada a propósito, no oculta):
        Orchestrator mantiene UNA sola memoria de conversación activa en
        proceso (memory_graph + vector_rag — ver clear_conversation_
        memory). Este proyecto no particiona esa memoria por pestaña
        todavía, así que activar OTRA pestaña resetea la memoria de corto
        plazo del modelo (mismo efecto que "Nueva conversación"), aunque
        el TRANSCRIPT visual de cada pestaña se conserva íntegro y se
        puede seguir leyendo/exportando. Particionar memory_graph/
        vector_rag por conversación es un cambio más profundo, fuera de
        este alcance.
        """
        self._active_session_index = index
        session = self._chat_sessions[index]

        if session["entries"]:
            self._reset_chat_widgets_and_memory()
            self._replay_session_entries(session["entries"])
            tr = I18N[self._current_lang]
            self._terminal_log(
                f"{tr['btn_new_chat_tooltip']}: {session.get('title') or tr['tab_new_chat_title']}",
                "info",
            )
        else:
            self._clear_chat()

    def _maybe_set_active_tab_title(self, first_message: str) -> None:
        """Titula la pestaña activa con el primer mensaje del usuario, la primera vez que envía algo en ella."""
        if not (0 <= self._active_session_index < len(self._chat_sessions)):
            return
        session = self._chat_sessions[self._active_session_index]
        if session["title"]:
            return
        title = (first_message or "").strip().replace("\n", " ")
        if len(title) > 28:
            title = title[:28].rstrip() + "…"
        session["title"] = title or I18N[self._current_lang]["tab_new_chat_title"]
        self.chat_tab_bar.setTabText(self._active_session_index, session["title"])

    def _add_tab_close_button(self, index: int) -> None:
        """Instala en la pestaña `index` un botón de cierre propio.

        Sustituye a la (x) nativa de setTabsClosable(True), cuyo icono
        rojo con relieve del SO chocaba con la estética oscura del resto
        de la UI. Este botón se estiliza por QSS (#tabCloseButton) y solo
        se pinta de rojo al pasar el cursor por encima.
        """
        btn = QPushButton("×")
        btn.setObjectName("tabCloseButton")
        btn.setFixedSize(18, 18)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setToolTip(I18N[self._current_lang]["tab_close_tooltip"])
        btn.clicked.connect(lambda _=False, b=btn: self._close_tab_via_button(b))
        self.chat_tab_bar.setTabButton(
            index, QTabBar.ButtonPosition.RightSide, btn
        )

    def _close_tab_via_button(self, btn: QPushButton) -> None:
        """Localiza la pestaña dueña de `btn` y la cierra.

        El índice de cada pestaña cambia al cerrar otras, así que no se
        puede capturar en el lambda — se resuelve en el momento del click.
        """
        for i in range(self.chat_tab_bar.count()):
            if self.chat_tab_bar.tabButton(
                i, QTabBar.ButtonPosition.RightSide
            ) is btn:
                self._on_tab_close_requested(i)
                return

    def _on_new_tab_clicked(self) -> None:
        self._snapshot_current_session()
        self._chat_sessions.append(self._new_session_dict())
        new_index = len(self._chat_sessions) - 1

        self.chat_tab_bar.blockSignals(True)
        self.chat_tab_bar.addTab(I18N[self._current_lang]["tab_new_chat_title"])
        self._add_tab_close_button(new_index)
        self.chat_tab_bar.setCurrentIndex(new_index)
        self.chat_tab_bar.blockSignals(False)

        self._activate_session(new_index)

    def _on_tab_bar_changed(self, index: int) -> None:
        """
        Solo se dispara ante un click DIRECTO del usuario sobre una
        pestaña existente — todo cambio de pestaña disparado por nuestro
        propio código (_on_new_tab_clicked/_on_tab_close_requested)
        bloquea las señales del QTabBar y llama a _activate_session() a
        mano, para no activar la sesión dos veces ni con un índice a
        medio actualizar.
        """
        if index < 0 or index >= len(self._chat_sessions) or index == self._active_session_index:
            return
        self._snapshot_current_session()
        self._activate_session(index)

    def _on_tab_close_requested(self, index: int) -> None:
        if not (0 <= index < len(self._chat_sessions)):
            return

        closing_active_tab = index == self._active_session_index
        if closing_active_tab:
            self._active_session_index = -1
        elif index < self._active_session_index:
            self._active_session_index -= 1

        del self._chat_sessions[index]
        self.chat_tab_bar.blockSignals(True)
        self.chat_tab_bar.removeTab(index)
        self.chat_tab_bar.blockSignals(False)

        if not closing_active_tab:
            return

        if not self._chat_sessions:
            self._chat_sessions.append(self._new_session_dict())
            self.chat_tab_bar.blockSignals(True)
            self.chat_tab_bar.addTab(I18N[self._current_lang]["tab_new_chat_title"])
            self._add_tab_close_button(0)
            self.chat_tab_bar.setCurrentIndex(0)
            self.chat_tab_bar.blockSignals(False)
            self._activate_session(0)
            return

        new_index = min(index, len(self._chat_sessions) - 1)
        self.chat_tab_bar.blockSignals(True)
        self.chat_tab_bar.setCurrentIndex(new_index)
        self.chat_tab_bar.blockSignals(False)
        self._activate_session(new_index)

    def _on_file_dropped_for_indexing(self, file_path: str) -> None:
        """
        Indexa en segundo plano (AsyncExecutor, ver __init__) un archivo
        soltado en la ventana — RAG (chunking AST para .py, ver rag_faiss.
        chunk_document). PromptTextEdit.dropEvent ya lo insertó en el
        prompt de ESTE turno; esto lo hace además recuperable en turnos
        FUTUROS vía fetch_hybrid_context().
        """
        tr = I18N.get(self._current_lang, I18N["Español"])
        path = Path(file_path)
        if path.suffix.lower() not in SUPPORTED_DROP_EXTENSIONS:
            return

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            self._terminal_log(tr["log_file_drop_read_failed"].format(path.name, exc), "warn")
            return

        full_path_id = str(path.resolve())

        def _do_index() -> int:
            return self.orchestrator.index_document_for_rag(full_path_id, content)

        def _on_indexed(chunk_count: int) -> None:
            if chunk_count:
                self._terminal_log(
                    tr["log_file_drop_indexed"].format(path.name, chunk_count),
                    "ok",
                )

        def _on_index_error(err: str) -> None:
            self._terminal_log(tr["log_file_drop_index_degraded"].format(path.name, err), "warn")

        self._async_executor.submit_task(_do_index, _on_indexed, _on_index_error)

    def _sync_tool_sandbox_root(self) -> None:
        """
        BLINDAJE (bug real, reportado 2026-09-02: "el workspace actual no
        me permite ver la carpeta ni modificarla — añadir archivos,
        modificar archivos, leer archivos, analizar archivos" — a pesar
        de que el log mostraba el WorkspaceScanner/RAG re-indexando la
        carpeta correctamente, "Workspace agregado: ... — indexando en
        segundo plano" con decenas de "reindexado: N fragmento(s)").

        Causa raíz: hay DOS conceptos de "workspace" totalmente
        desconectados entre sí. Este panel (Workspaces) solo maneja
        `self._workspace_scanner` (indexado para RAG/búsqueda semántica,
        ver workspace_watcher.py) y `self.orchestrator.memory_graph`
        (persistencia entre sesiones) — nunca tocaba
        `self.orchestrator.tools.sandbox`, la raíz que usan de verdad las
        herramientas `read_file`/`write_file`/`run_cmd`/`list_dir`
        (tools.py: `ToolSandbox.root_dir`). Esa raíz se fijaba UNA sola
        vez, al construir `LocalToolDispatcher()` en el arranque de la
        app, con `os.getcwd()` (la carpeta de instalación) — sin importar
        cuántas carpetas se agregaran o quitaran después desde este panel.
        Por eso "dime la estructura actual del workspace" (que dispara
        `list_dir`) reportaba la carpeta como vacía: estaba listando la
        carpeta de instalación, no la carpeta que el usuario ve en este
        panel ni en el Explorador de Windows.

        Este método sincroniza esa raíz cada vez que la lista de
        Workspaces cambia (agregar, quitar, o restaurar al arrancar),
        apuntándola a la ÚLTIMA carpeta activa en `workspaces_list` — un
        solo "workspace actual" para herramientas, igual que el usuario
        lo nombra ("el workspace actual"), y no una unión de todas las
        carpetas alguna vez agregadas (`ToolSandbox` es un límite de
        seguridad: ampliarlo a todas las carpetas indexadas para RAG
        sería expandir silenciosamente qué puede leer/escribir/ejecutar
        el modelo, más allá de lo pedido). Si no queda ninguna carpeta en
        la lista, vuelve a la raíz aislada por defecto de `ToolSandbox`
        (una carpeta `workspace/` junto a la instalación, NUNCA la
        carpeta de instalación en sí — ver el BLINDAJE 2026-09-09 más
        abajo, junto a `new_root`).

        Nunca lanza: `self.orchestrator.tools` podría no existir todavía
        en algún punto de la inicialización, y un fallo acá no debe
        impedir que el panel de Workspaces siga funcionando para RAG.
        """
        try:
            tools = getattr(self.orchestrator, "tools", None)
            sandbox = getattr(tools, "sandbox", None)
            if sandbox is None:
                return
            count = self.workspaces_list.count()
            # BLINDAJE (bug real, MEDIDO 2026-09-09 — "hola, crea un juego
            # de pong en el workspace" con la lista de Workspaces vacía
            # terminó escribiendo pong.py en la RAÍZ de instalación de la
            # app, junto a ARCHITECTURE.md/README/SovNode.spec, en vez de
            # en una carpeta workspace/). Este `else` volvía a
            # `os.getcwd()` a secas — exactamente el mismo bug que
            # `ToolSandbox._default_isolated_root()` (tools.py) ya
            # documentaba y evitaba para el arranque en frío, pero
            # reintroducido acá por este segundo camino: cada vez que la
            # lista de Workspaces cambia (incluido "no quedó ninguna
            # carpeta"), este método pisa esa raíz por defecto. Se le pide
            # al propio sandbox su raíz aislada por defecto (misma carpeta
            # `workspace/` junto a la instalación, creada si no existe) en
            # vez de duplicar esa lógica acá.
            new_root = (
                self.workspaces_list.item(count - 1).text()
                if count > 0
                else str(sandbox._default_isolated_root())
            )
            sandbox.set_root(new_root)
            tr = I18N.get(self._current_lang, I18N["Español"])
            self._terminal_log(tr["log_tool_sandbox_synced"].format(new_root), "info")
        except Exception as exc:
            logger.warning("[_sync_tool_sandbox_root] No se pudo sincronizar la raíz de herramientas: %s", exc)

    def _load_persisted_workspaces(self) -> None:
        """
        Repuebla `workspaces_list`/`WorkspaceScanner` con las carpetas que
        quedaron guardadas en `sovnode_memory.db` (MemoryGraph.
        list_workspace_folders) de una sesión anterior — llamado una vez
        desde `__init__`, después de `_create_ui()` (necesita que
        `workspaces_list` ya exista) y ANTES de que el usuario pueda tocar
        nada. Si no hay ninguna persistida (primer uso, o base de datos
        nueva), no hace nada — mismo comportamiento que antes de esta
        función existir. Nunca lanza: un fallo de lectura de la base no
        debe impedir que la app arranque.
        """
        tr = I18N.get(self._current_lang, I18N["Español"])
        try:
            folders = self.orchestrator.memory_graph.list_workspace_folders()
        except Exception as exc:
            self._terminal_log(tr["log_workspaces_load_failed"].format(exc), "warn")
            return

        for folder in folders:
            if self._workspace_scanner.add_root(folder):
                _persisted_item = QListWidgetItem(folder)
                _persisted_item.setToolTip(folder)
                self.workspaces_list.addItem(_persisted_item)

        if folders:
            self._terminal_log(
                tr["log_workspaces_restored"].format(len(folders)), "info"
            )
            # patch_qt68 (2026-09-19, pedido explícito del usuario: "mejora
            # el sistema de workspace si esta desactivado no deberia
            # encenderse en ningun momento") -- solo arranca el watcher si
            # las herramientas de workspace YA están habilitadas. Se lee
            # QSettings directo acá, no `self.orchestrator.workspace_
            # tools_enabled`, porque este método corre ANTES que
            # `_load_cloud_settings()` en __init__ (ver el orden de los
            # dos llamados ahí) -- a esta altura ese atributo todavía no
            # se cargó desde disco y vale el default de fábrica (False),
            # sin importar lo que el usuario haya dejado guardado. Ver el
            # BLINDAJE completo junto a `_on_workspace_tools_toggled`.
            _tools_enabled_now = QSettings("SovNode", "SovNode").value(
                "workspace/tools_enabled", False, type=bool
            )
            if _tools_enabled_now and not self._workspace_watcher_started:
                self._workspace_watcher.start()
                self._workspace_watcher_started = True

        self._sync_tool_sandbox_root()

    def _on_add_workspace_clicked(self) -> None:
        """
        Agrega una carpeta a vigilar (WorkspaceScanner.add_root) y arranca
        el hilo WorkspaceWatcherWorker en su primer uso — ver el comentario
        junto a self._workspace_watcher_started en __init__.
        """
        tr = I18N.get(self._current_lang, I18N["Español"])
        folder = QFileDialog.getExistingDirectory(
            self, tr["workspace_select_dialog"]
        )
        if not folder:
            return

        norm = os.path.normpath(folder)
        for i in range(self.workspaces_list.count()):
            if os.path.normpath(self.workspaces_list.item(i).text()) == norm:
                self._terminal_log(tr["log_workspace_already_added"].format(folder), "info")
                return

        added = self._workspace_scanner.add_root(folder)
        if not added:
            return

        item = QListWidgetItem(folder)
        item.setToolTip(folder)
        self.workspaces_list.addItem(item)
        self._terminal_log(tr["log_workspace_added"].format(folder), "ok")

        try:
            self.orchestrator.memory_graph.add_workspace_folder(folder)
        except Exception as exc:
            self._terminal_log(tr["log_workspace_persist_failed"].format(folder, exc), "warn")

        # patch_qt68 (2026-09-19): solo arranca el watcher si las
        # herramientas de workspace ya están habilitadas -- ver el
        # BLINDAJE junto a `_on_workspace_tools_toggled`. Con las
        # herramientas apagadas, la carpeta queda agregada a la lista
        # (`add_root` solo registra la ruta en memoria, no toca disco,
        # ver workspace_watcher.py) pero no se escanea nada hasta que el
        # usuario prenda el interruptor.
        if self.orchestrator.workspace_tools_enabled and not self._workspace_watcher_started:
            self._workspace_watcher.start()
            self._workspace_watcher_started = True

        self._sync_tool_sandbox_root()

    def _on_remove_workspace_clicked(self) -> None:
        """
        Quita la carpeta seleccionada de la vigilancia. Los archivos que
        ya estaban indexados bajo esa carpeta se retiran del índice
        vectorial de inmediato (WorkspaceScanner.remove_root() devuelve
        las rutas conocidas; cada una se borra vía
        Orchestrator.remove_document_from_rag()) — si solo se dejara de
        vigilar sin borrar, esos chunks quedarían recuperables para
        siempre aunque el usuario haya quitado la carpeta explícitamente.
        """
        tr = I18N.get(self._current_lang, I18N["Español"])
        item = self.workspaces_list.currentItem()
        if item is None:
            return
        folder = item.text()
        orphaned = self._workspace_scanner.remove_root(folder)
        self.workspaces_list.takeItem(self.workspaces_list.row(item))
        self._sync_tool_sandbox_root()

        try:
            self.orchestrator.memory_graph.remove_workspace_folder(folder)
        except Exception as exc:
            self._terminal_log(tr["log_workspace_unpersist_failed"].format(folder, exc), "warn")

        def _do_cleanup() -> int:
            total = 0
            for file_path in orphaned:
                total += self.orchestrator.remove_document_from_rag(file_path)
            return total

        def _on_cleanup_done(total: int) -> None:
            self._terminal_log(
                tr["log_workspace_removed"].format(folder, total),
                "info",
            )

        def _on_cleanup_error(err: str) -> None:
            self._terminal_log(tr["log_workspace_cleanup_degraded"].format(folder, err), "warn")

        self._async_executor.submit_task(_do_cleanup, _on_cleanup_done, _on_cleanup_error)

    def _on_workspace_file_indexed(self, file_path: str, chunk_count: int) -> None:
        tr = I18N.get(self._current_lang, I18N["Español"])
        self._terminal_log(
            tr["log_workspace_file_reindexed"].format(Path(file_path).name, chunk_count),
            "ok",
        )

    def _on_workspace_file_removed(self, file_path: str, chunk_count: int) -> None:
        tr = I18N.get(self._current_lang, I18N["Español"])
        self._terminal_log(
            tr["log_workspace_file_removed"].format(Path(file_path).name, chunk_count),
            "info",
        )

    def _on_workspace_scan_error(self, err: str) -> None:
        tr = I18N.get(self._current_lang, I18N["Español"])
        self._terminal_log(tr["log_workspace_watcher_error"].format(err), "warn")

    def _set_ui_controls_enabled(self, enabled: bool) -> None:
        """
        Bloquea/desbloquea en bloque los controles del panel de
        configuración, de pestañas y de entrada que pueden alterar
        estado compartido (idioma, tema, chat activo) mientras hay una
        inferencia en curso — evita que el usuario dispare uno de esos
        cambios a mitad de un turno y deje al Orchestrator o a la UI en
        un estado parcialmente actualizado / con condiciones de carrera.

        BLINDAJE (bug real, MEDIDO en captura de pantalla 2026-09-06,
        ver el comentario junto a `self._is_processing_turn`): esta
        lista bloqueaba los paneles laterales pero, llamativamente,
        nunca el propio campo de texto (`input_field` seguía habilitado
        y con foco durante todo el turno) — la guarda de reentrancia en
        `_send_message` es la defensa PRIMARIA contra el doble-envío,
        pero deshabilitar también el input acá es la segunda capa obvia:
        ni siquiera debería ser posible escribir/tocar Enter mientras
        hay un turno en curso, reentrancia aparte.
        """
        self.input_field.setEnabled(enabled)
        self.combo_lang.setEnabled(enabled)
        self.combo_theme.setEnabled(enabled)
        if hasattr(self, "btn_download_model"):
            self.btn_download_model.setEnabled(enabled)
        self.btn_new_chat.setEnabled(enabled)
        self.btn_export.setEnabled(enabled)
        self.btn_export_training.setEnabled(enabled)
        self.btn_donate.setEnabled(enabled)
        if hasattr(self, "chat_tab_bar"):
            self.chat_tab_bar.setEnabled(enabled)
        if hasattr(self, "mic_button"):
            self.mic_button.setEnabled(enabled)
        if hasattr(self, "attach_button"):
            self.attach_button.setEnabled(enabled)

    def _attach_image_from_source(self, source) -> bool:
        """
        Punto único para adjuntar una imagen sin importar de dónde vino
        -- el diálogo de archivo ("+"), arrastrar y soltar sobre el
        campo de texto, o Ctrl+V desde el portapapeles (pedido
        explícito del usuario, 2026-09-15: "que pueda arrastrar
        imagenes a la interfaz... o que si tengo la imagen copiada, que
        solo tenga que dar control V"). `source` es una ruta de
        archivo (str) o un QImage ya decodificado (portapapeles con
        píxeles crudos, sin archivo de por medio).

        SIEMPRE normaliza a un PNG temporal reescalado a un máximo de
        `ATTACHED_IMAGE_MAX_DIMENSION` de lado antes de guardar la ruta
        en `self._attached_image_path` -- así el carril VISION de
        `Orchestrator.run_turn` siempre recibe el mismo formato sin
        importar si el original era .bmp/.webp/una captura pegada sin
        archivo, y nunca se manda a la API una imagen más pesada de lo
        que hace falta. El PNG temporal vive bajo el directorio
        temporal del sistema (`sovnode_attachments/`) y NO se borra
        acá -- `_clear_attachment` corre antes de que el turno
        asincrónico (`StreamTurnWorker`) llegue a abrir el archivo, así
        que borrarlo ahí sería una carrera real contra un lector que
        todavía no leyó nada; se deja que la limpieza periódica del
        directorio temporal del sistema operativo se encargue.
        """
        tr = I18N.get(self._current_lang, I18N["Español"])
        if isinstance(source, str):
            image = QImage(source)
            display_name = os.path.basename(source)
        else:
            image = source
            display_name = tr["attach_pasted_image_name"]
        if image is None or image.isNull():
            self._terminal_log(tr["attach_load_failed"], "warn")
            return False

        long_side = max(image.width(), image.height())
        if long_side > ATTACHED_IMAGE_MAX_DIMENSION:
            image = image.scaled(
                ATTACHED_IMAGE_MAX_DIMENSION,
                ATTACHED_IMAGE_MAX_DIMENSION,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

        try:
            tmp_dir = os.path.join(tempfile.gettempdir(), "sovnode_attachments")
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_path = os.path.join(tmp_dir, f"attach_{uuid.uuid4().hex}.png")
            if not image.save(tmp_path, "PNG"):
                raise OSError("QImage.save devolvió False")
        except Exception as exc:
            logger.warning("[_attach_image_from_source] No se pudo normalizar la imagen: %s", exc)
            self._terminal_log(tr["attach_load_failed"], "warn")
            return False

        self._attached_file_path = None
        self._attached_file_content = None
        self._attached_image_path = tmp_path
        self.attachment_type_badge_label.setVisible(False)
        self.attachment_thumb_label.setVisible(True)
        self.attachment_thumb_label.setPixmap(QPixmap.fromImage(image))
        self.attachment_name_label.setText(display_name)
        self.attachment_preview_container.setVisible(True)
        return True

    def _on_image_pasted(self, image) -> None:
        """Ctrl+V con píxeles reales en el portapapeles (captura de
        pantalla, copiar desde un navegador/editor de imágenes). Ver
        `_attach_image_from_source`."""
        self._attach_image_from_source(image)

    def _on_image_path_attached(self, file_path: str) -> None:
        """Arrastrar y soltar un archivo de imagen, o Ctrl+V de un
        ARCHIVO copiado (no píxeles) desde el explorador. Ver
        `_attach_image_from_source`."""
        self._attach_image_from_source(file_path)

    def _on_text_file_attached(self, file_path: str) -> None:
        """Arrastrar y soltar un archivo de texto/código soportado
        (.py/.txt/.md/.json/.csv) sobre el campo de entrada. Ver
        `_attach_text_file_from_path`."""
        self._attach_text_file_from_path(file_path)

    def _attach_text_file_from_path(self, file_path: str) -> bool:
        """
        Adjunta un archivo de texto/código como un chip -- nombre +
        insignia de extensión (ej. "PY") -- en vez de volcar su
        contenido entero como texto literal en el campo de entrada
        (pedido explícito del usuario, 2026-09-15: foto de referencia
        de la propia interfaz de Claude, con miniaturas de imagen y una
        tarjeta "PY" para un .py adjunto, en vez de un bloque de código
        gigante pegado en el chat).

        Mismo patrón de slot único que `_attach_image_from_source`:
        adjuntar un archivo de texto limpia cualquier imagen pendiente
        (`_clear_attachment` limpia los dos tipos al tocar la "x"). El
        contenido leído acá se guarda en `self._attached_file_content`
        -- `_send_message` arma con él el texto completo tageado que se
        manda al modelo, separado del texto corto que se ve en la
        burbuja del usuario.
        """
        tr = I18N.get(self._current_lang, I18N["Español"])
        path = Path(file_path)
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning(
                "[_attach_text_file_from_path] No se pudo leer '%s': %s",
                file_path, exc,
            )
            self._terminal_log(
                tr["attach_file_load_failed"].format(path.name), "warn"
            )
            return False

        self._attached_image_path = None
        self._attached_file_path = str(path)
        self._attached_file_content = content

        self.attachment_thumb_label.clear()
        self.attachment_thumb_label.setVisible(False)
        self.attachment_type_badge_label.setText(
            path.suffix.lstrip(".").upper() or "TXT"
        )
        self.attachment_type_badge_label.setVisible(True)
        self.attachment_name_label.setText(path.name)
        self.attachment_preview_container.setVisible(True)
        return True

    def _open_attach_file_dialog(self) -> None:
        """
        Botón "+" (ver icons.py "attach" y _create_ui): abre el selector
        nativo de archivos filtrado a imágenes y delega en
        `_attach_image_from_source` -- mismo camino que arrastrar o
        pegar (ver la nota grande junto a ese método). `_send_message`
        lee `self._attached_image_path` al enviar y se la pasa a
        `StreamTurnWorker`/`Orchestrator.run_turn` (carril VISION,
        `image_path=...`) -- local (Ollama/moondream) o Sonnet vía
        Cloud según `cloud_backend_enabled`, ver
        `Orchestrator._call_claude_api_raw`.
        """
        tr = I18N.get(self._current_lang, I18N["Español"])
        file_path, _ = QFileDialog.getOpenFileName(
            self, tr["attach_dialog_title"], "", tr["attach_dialog_filter"],
        )
        if not file_path:
            return
        self._attach_image_from_source(file_path)

    def _clear_attachment(self) -> None:
        """Saca el adjunto pendiente (botón "✕" de la vista previa) sin
        tocar el campo de texto — el usuario puede haber escrito algo
        junto con el adjunto y arrepentirse solo de eso. Limpia tanto
        una imagen como un archivo de texto adjuntos -- slot único,
        mutuamente excluyentes (ver _attach_image_from_source /
        _attach_text_file_from_path)."""
        self._attached_image_path = None
        self._attached_file_path = None
        self._attached_file_content = None
        self.attachment_thumb_label.clear()
        self.attachment_thumb_label.setVisible(True)
        self.attachment_type_badge_label.setText("")
        self.attachment_type_badge_label.setVisible(False)
        self.attachment_name_label.setText("")
        self.attachment_preview_container.setVisible(False)

    def _send_message(self) -> None:
        # Guarda de reentrancia (ver el comentario junto a
        # `self._is_processing_turn` en __init__ para el bug real que esto
        # arregla): si ya hay un turno en curso, un segundo click/Enter no
        # arranca un StreamTurnWorker duplicado para el mismo pedido — se
        # ignora en silencio, sin tocar el input ni el chat.
        if self._is_processing_turn:
            return
        text = self.input_field.toPlainText().strip()
        image_path = self._attached_image_path
        attached_file_path = self._attached_file_path
        attached_file_content = self._attached_file_content
        if not text and not image_path and not attached_file_content:
            return
        self._is_processing_turn = True
        self._turn_cost_snapshot_usd = float(
            self.orchestrator.cloud_usage_totals.get("cost_usd", 0.0)
        )

        # BLINDAJE (pedido explícito del usuario, 2026-09-15 --
        # screenshot de un archivo .py entero volcado como texto en la
        # burbuja del chat): `orchestrator_text` (con el contenido
        # completo tageado, igual formato que el viejo volcado en el
        # campo de entrada) es lo que recibe el modelo; `display_text`
        # (un marcador corto "📎 nombre.py") es lo único que se ve en la
        # burbuja del usuario -- el chip de arriba (ya visible antes de
        # enviar) ya comunicó qué se adjuntó, no hace falta repetir todo
        # el código en el chat.
        orchestrator_text = text
        display_text = text
        if attached_file_content:
            _file_name = os.path.basename(attached_file_path)
            _tagged = (
                f"[ARCHIVO ADJUNTO: {_file_name}]\n"
                f"```{Path(attached_file_path).suffix.lstrip('.')}\n"
                f"{attached_file_content}\n"
                f"```"
            )
            orchestrator_text = f"{text}\n\n{_tagged}" if text else _tagged
            display_text = f"📎 {_file_name}\n\n{text}" if text else f"📎 {_file_name}"

        self._stop_tts()
        self._set_ui_controls_enabled(False)
        self.input_field.clear()
        self._add_bubble("user", display_text, image_path=image_path)
        self._clear_attachment()
        if text:
            self._maybe_set_active_tab_title(text)
        self._terminal_log(
            I18N[self._current_lang]["log_sending"].format(text[:50]), "info"
        )
        if self._advanced_terminal_enabled:
            self._adv_terminal_turn_header(text or "(sin texto)")

        self._current_stream_bubble = self._add_bubble("assistant", "")
        self._stream_buffer = ""

        self.send_button.setVisible(False)
        self.stop_button.setVisible(True)

        if hasattr(self, "chat_scroll"):
            self.chat_scroll.pin_to_bottom()
            self._scroll_to_bottom(force=True)

        self._elapsed_seconds = 0
        self.processing_timer.start(1000)
        self._render_timer.start()

        if self._turn_worker is not None and self._turn_worker.isRunning():
            # Corrección CRÍTICA (auditoría v3.9): defensa en profundidad
            # — en el flujo normal `self._is_processing_turn` ya impide
            # llegar hasta acá con un worker vivo, pero nunca se
            # sobreescribe `self._turn_worker` sin antes desconectar sus
            # señales (ver `_orphan_turn_worker`), para que un worker
            # previo que quedó corriendo por cualquier motivo no pueda
            # seguir tocando el estado de esta ventana una vez
            # reemplazado.
            self._orphan_turn_worker()

        self._turn_worker = StreamTurnWorker(
            self.orchestrator, orchestrator_text,
            force_web_search=self._force_web_search,
            lang=self._current_lang,
            image_path=image_path,
        )
        self._turn_worker.intent_changed.connect(self._on_intent_changed)
        self._turn_worker.web_results_ready.connect(self._on_web_results_ready)
        self._turn_worker.chunk_received.connect(self._on_chunk_received)
        self._turn_worker.completed.connect(self._on_turn_completed)
        self._turn_worker.log_message.connect(self._on_worker_log_message)
        self._turn_worker.start()

    def _orphan_turn_worker(self) -> None:
        """
        Corrección CRÍTICA (auditoría v3.9): desconecta las señales del
        `StreamTurnWorker` ACTUAL antes de soltar la referencia o de
        permitir un nuevo envío.

        `StreamTurnWorker.stop()` solo fija banderas cooperativas — no
        mata el hilo, y no garantiza que termine antes del siguiente
        checkpoint dentro de `run_turn()` (que puede estar a mitad de
        una llamada de red BLOQUEANTE a la API de Claude, sin ningún
        punto de chequeo hasta que esa llamada retorne). Sin este
        método, un worker "detenido" seguía corriendo en segundo plano
        con sus señales `completed`/`chunk_received`/etc. todavía
        conectadas a esta misma ventana. Si el usuario apretaba
        Detener y volvía a enviar de inmediato, el worker viejo
        terminaba tarde y su señal `completed` llegaba a
        `_on_turn_completed` DESPUÉS de que el worker nuevo ya estaba
        corriendo — pisando la burbuja/buffer del turno nuevo con la
        respuesta del turno viejo, deteniendo timers que pertenecen al
        turno nuevo, reponiendo `_is_processing_turn=False` a mitad del
        turno nuevo (lo que habilita un TERCER envío concurrente = una
        tercera llamada paga a la API), y reactivando los controles de
        UI mientras el turno nuevo seguía en curso.

        El hilo viejo puede seguir vivo hasta que su propio checkpoint
        de cancelación lo note — eso no se puede evitar sin cancelar la
        request de red ya en vuelo, un cambio mayor fuera de alcance
        acá — pero al desconectar sus señales de inmediato deja de
        poder tocar el estado de esta ventana: cierra la vía de
        contaminación cruzada, aunque no el costo de la llamada que ya
        estaba en curso al apretar Detener.
        """
        worker = self._turn_worker
        if worker is None:
            return
        for signal_name in (
            "intent_changed", "web_results_ready", "chunk_received",
            "completed", "log_message",
        ):
            signal = getattr(worker, signal_name, None)
            if signal is not None:
                with contextlib.suppress(TypeError, RuntimeError):
                    signal.disconnect()
        # El hilo huérfano se libera solo cuando termine de verdad, en
        # vez de perder toda referencia a él mientras sigue corriendo.
        with contextlib.suppress(TypeError, RuntimeError):
            worker.finished.connect(worker.deleteLater)

    def _stop_generation(self) -> None:
        # BLINDAJE (2026-09-17, bug real reportado por el usuario:
        # "nunca se detiene y si le doy a detener denuevo crashea"):
        # dos problemas distintos en el mismo método, encontrados
        # comparando esto contra `_on_turn_completed` (el otro único
        # lugar que hace esta misma limpieza).
        #
        # 1) "nunca se detiene": este método llamaba a
        #    `_orphan_turn_worker()`, que a propósito desconecta la
        #    señal `completed` del worker (ver su docstring) -- así
        #    que `_on_turn_completed` NUNCA corre para un turno frenado
        #    a mano. El problema: TODA la limpieza visual de fin de
        #    turno vive Únicamente ahí dentro -- parar
        #    `processing_timer` (el contador "...(Ns)" que se veía
        #    subiendo sin parar en la captura), parar `_render_timer`,
        #    vaciar `processing_label`, y volver a mostrar
        #    `send_button` en vez de `stop_button`. Nada de eso pasaba
        #    acá, así que la UI quedaba visualmente "procesando" para
        #    siempre aunque el turno ya estuviera cancelado por dentro
        #    (`_is_processing_turn` sí se limpiaba, pero eso es interno
        #    -- el usuario no lo ve). Se replica acá esa misma limpieza.
        #
        # 2) "si le doy a detener de nuevo crashea": `self._turn_worker`
        #    nunca se ponía en `None` después de huerfanarlo -- así
        #    que un segundo click de Detener volvía a entrar al `if`
        #    de abajo y llamaba a `self._turn_worker.isRunning()` sobre
        #    la MISMA referencia. Si para ese momento el hilo huérfano
        #    ya terminó de verdad (la llamada de red bloqueante que
        #    mantenía vivo el worker por fin volvió) y su
        #    `finished.connect(worker.deleteLater)` (ver
        #    `_orphan_turn_worker`) ya corrió, ese objeto Qt puede
        #    estar YA BORRADO del lado C++ -- tocar `.isRunning()` sobre
        #    un QThread borrado tira `RuntimeError: wrapped C/C++
        #    object ... has been deleted`, y PyQt6 sin un excepthook
        #    propio se lleva puesta TODA la app con eso (no solo un
        #    error silencioso). Mismo patrón que el `sip.isdeleted`
        #    ya usado en `_render_content_now` para `self._card` -- acá
        #    se aplica el mismo chequeo, y además se pone
        #    `self._turn_worker = None` en cuanto se huerfana: una vez
        #    huerfanado, esta ventana ya no tiene ningún motivo para
        #    seguir referenciando ese worker (ver el docstring de
        #    `_orphan_turn_worker`), así que un segundo click
        #    simplemente no encuentra nada que hacer, en vez de arriesgarse
        #    a tocar un objeto potencialmente ya borrado.
        if (
            self._turn_worker is not None
            and not sip.isdeleted(self._turn_worker)
            and self._turn_worker.isRunning()
        ):
            self._turn_worker.stop()
            self._orphan_turn_worker()
            self._turn_worker = None
            self._is_processing_turn = False
            self._set_ui_controls_enabled(True)

            self.processing_timer.stop()
            self._render_timer.stop()
            self._flush_stream_buffer()
            if self._current_stream_bubble:
                self._current_stream_bubble.force_flush_render()
            self._remove_thinking_widget()
            self.processing_label.setText("")
            self.stop_button.setVisible(False)
            self.send_button.setVisible(True)

            self._terminal_log(I18N[self._current_lang]["log_stopped_by_user"], "warn")

    def _set_header_cost_badge(self, turn_cost: Optional[float] = None) -> None:
        """
        Insignia persistente de costo en la barra superior (pedido
        explícito del usuario, 2026-09-15: "el contador de costo por
        turno merece más protagonismo ... un indicador persistente en
        la barra superior ... con alerta visual si un turno se acerca al
        techo elegido"). Antes el único lugar donde se veía el gasto
        era `cloud_usage_label`, una línea de texto chica DENTRO del
        panel de configuración -- había que abrirlo para verla. Mismo
        patrón theme-consciente que `_set_header_status_badge` (colores
        resueltos del tema activo en cada llamada, nunca hex fijos).

        `turn_cost`: gasto en USD del ÚLTIMO turno completo (ya filtrado
        a >= 0 por el llamador, `_on_turn_completed`). Se guarda en
        `_last_turn_cost_usd` para que un cambio de tema/idioma pueda
        volver a pintar la insignia sin esperar a que termine otro turno
        (mismo patrón que `_last_web_mode`). Si no se pasa (llamada de
        refresco -- carga de settings, cambio de motor/presupuesto,
        cambio de tema/idioma), se reusa el último valor conocido, o
        `None` si todavía no completó ningún turno en esta ventana.

        Color según cuánto del presupuesto de Sonnet elegido
        (`cloud_output_budget_cents`, 1/2/4/8 -- Bajo/Medio/Alto/Extra)
        consumió el turno:
        ese selector solo topea los tokens de SALIDA (ver
        `_cloud_output_ceiling_tokens`/CLOUD_OUTPUT_BUDGET_SAFETY_
        FRACTION en orchestrator.py, que reserva un 10% para tokens de
        entrada frescos) -- el costo REAL del turno (entrada + caché +
        salida) puede terminar cerca o incluso por encima del techo
        elegido cuando hay mucho contexto de entrada (búsqueda web,
        conversación larga). Esta insignia existe justamente para hacer
        visible ESE caso, no solo repetir el número que ya garantiza el
        selector: verde/éxito si quedó cómodo (< 80% del techo),
        ámbar/advertencia si se acercó (>= 80%), rojo/peligro si lo
        igualó o superó.
        """
        if not hasattr(self, "header_cost_badge"):
            return
        if turn_cost is not None:
            self._last_turn_cost_usd = turn_cost
        turn_cost = self._last_turn_cost_usd
        session_cost = float(self.orchestrator.cloud_usage_totals.get("cost_usd", 0.0))
        tr = I18N[self._current_lang]
        theme = THEMES.get(self._theme_name, THEMES["Cyberpunk Dark"])

        if turn_cost is None:
            self.header_cost_badge.setText(tr["header_cost_badge_idle"].format(session_cost))
            color, bg, border = theme["secondary"], theme["input"], theme["border"]
        else:
            self.header_cost_badge.setText(
                tr["header_cost_badge_fmt"].format(turn_cost, session_cost)
            )
            budget_cents = getattr(self.orchestrator, "cloud_output_budget_cents", None)
            budget_usd = (budget_cents / 100.0) if budget_cents else None
            if budget_usd and turn_cost >= budget_usd:
                color, border = theme["danger"], theme["danger"]
                bg = _rgba(theme["danger"], 0.14)
            elif budget_usd and turn_cost >= 0.8 * budget_usd:
                color, border = theme["warning"], theme["warning"]
                bg = _rgba(theme["warning"], 0.14)
            else:
                color, border = theme["success"], theme["success"]
                bg = _rgba(theme["success"], 0.14)

        self.header_cost_badge.setStyleSheet(
            f"color:{color}; font-size:10px; font-weight:800; "
            f"background-color:{bg}; "
            f"border:1px solid {border}; "
            "border-radius:9px; padding:3px 11px;"
        )
        self.header_cost_badge.setToolTip(tr["header_cost_badge_tooltip"])

    def _set_header_status_badge(self, mode: Optional[str]) -> None:
        """
        Actualiza la insignia de procedencia de datos del último turno en
        el header superior (Módulo 3). `mode`: "live" (búsqueda web en
        tiempo real), "local" (memoria local / sin datos web) o None
        (oculta la insignia — p. ej. tras limpiar el chat).

        El texto sale de I18N (Módulo 4): nunca se hardcodea en español,
        así que _on_lang_changed() puede volver a llamar a este método
        con el mismo `mode` para re-renderizar la insignia en el idioma
        nuevo sin esperar a que termine otro turno.
        """
        self._last_web_mode = mode
        tr = I18N[self._current_lang]
        # BLINDAJE (bug real de diseño, MEDIDO por revisión de código
        # 2026-09-08, pedido del usuario "podemos mejorar la interfaz en
        # diseño?"): esta insignia usaba colores hex FIJOS (los de
        # Cyberpunk Dark, "#3DDC97"/"#0F2A20"/"#8B92A5"/"#1B1F2A"/
        # "#30363D") en vez de leer del tema activo, a diferencia de
        # TODO el resto del header (headerTitle/headerSubtitle sí usan
        # `theme[...]` vía QSS). Resultado: al cambiar a "OLED Pure
        # Black" o "Nordic Slate" esta insignia se quedaba pegada a los
        # colores de Cyberpunk, desentonando con el tema recién elegido.
        # Fix: mismo patrón ya establecido en `_set_node_status_color`
        # (el otro widget persistente con color "pintado a mano" en vez
        # de QSS) — resolver el tema activo por nombre en cada llamada.
        # `_apply_theme()` ya vuelve a llamar a este método al cambiar de
        # tema (ver más abajo), así que la insignia se re-pinta sola.
        theme = THEMES.get(self._theme_name, THEMES["Cyberpunk Dark"])

        if mode == "live":
            self.header_status.setText(tr["web_badge_live"])
            self.header_status.setStyleSheet(
                f"color:{theme['success']}; font-size:10px; font-weight:800; "
                f"background-color:{_rgba(theme['success'], 0.14)}; "
                f"border:1px solid {theme['success']}; "
                "border-radius:9px; padding:3px 11px;"
            )
            self.header_status.setVisible(True)
        elif mode == "local":
            self.header_status.setText(tr["web_badge_local"])
            self.header_status.setStyleSheet(
                f"color:{theme['secondary']}; font-size:10px; font-weight:700; "
                f"background-color:{theme['input']}; "
                f"border:1px solid {theme['border']}; "
                "border-radius:9px; padding:3px 11px;"
            )
            self.header_status.setVisible(True)
        else:
            self.header_status.setVisible(False)

    def _on_turn_completed(self, trace: Any, error_msg: str) -> None:
        self.processing_timer.stop()
        self._refresh_cloud_usage_label()
        self._render_timer.stop()
        self._flush_stream_buffer()
        if self._current_stream_bubble:
            self._current_stream_bubble.force_flush_render()
        self._remove_thinking_widget()
        _turn_cost_delta_usd = max(
            0.0,
            float(self.orchestrator.cloud_usage_totals.get("cost_usd", 0.0))
            - self._turn_cost_snapshot_usd,
        )
        self._set_header_cost_badge(_turn_cost_delta_usd)
        if self._advanced_terminal_enabled:
            self._adv_terminal_turn_footer(_turn_cost_delta_usd)
        self._is_processing_turn = False
        self._set_ui_controls_enabled(True)

        self.processing_label.setText("")
        self.stop_button.setVisible(False)
        self.send_button.setVisible(True)

        if error_msg:
            if self._current_stream_bubble and not str(
                getattr(self._current_stream_bubble, "_content", "") or ""
            ).strip():
                self.chat_layout.removeWidget(self._current_stream_bubble)
                if self._current_stream_bubble in self._bubble_widgets:
                    self._bubble_widgets.remove(self._current_stream_bubble)
                self._current_stream_bubble.deleteLater()
                self._current_stream_bubble = None
            self._add_bubble("assistant", f"⚠️ {error_msg}", is_error=True)
            self._terminal_log(
                I18N[self._current_lang]["log_turn_error"].format(error_msg), "error"
            )
        elif trace and self._current_stream_bubble:
            final_text = str(getattr(trace, "final_response", "") or "").strip()
            shown_text = str(getattr(self._current_stream_bubble, "_content", "") or "").strip()
            if final_text and final_text != shown_text:
                self._current_stream_bubble.update_content(final_text)
                self._current_stream_bubble.force_flush_render()

            if self.tts_enabled:
                self._play_tts(final_text or shown_text)

            web_used = getattr(trace, "web_context_used", False)
            mode = "live" if web_used else "local"
            self._set_header_status_badge(mode)

            if hasattr(trace, "model_used"):
                model_name = getattr(trace, "model_used", "desconocido")
                self._terminal_log(
                    I18N[self._current_lang]["log_turn_completed"].format(model_name),
                    "ok",
                )

    def _apply_theme(self, theme_name: str) -> None:
        if theme_name in THEMES:
            theme = THEMES[theme_name]
            style = build_style(theme, font_family=_RESOLVED_FONT_FAMILY)
            self.setStyleSheet(style)
            math_render.set_equation_color(theme["text"])

            _ACTIVE_THEME.clear()
            _ACTIVE_THEME.update(theme)

            self._refresh_themed_icons(theme)

            if hasattr(self, "_node_status_key"):
                self._set_node_status_color(self._node_status_key)

            # Idem para la insignia "MEMORIA LOCAL"/"RED VIVA" del header
            # (ver _set_header_status_badge, mismo BLINDAJE de diseño) -
            # se re-pinta con el color del tema recién activado en vez de
            # quedar pegada al que tenía cuando se mostró por última vez.
            if hasattr(self, "header_status") and hasattr(self, "_last_web_mode"):
                self._set_header_status_badge(self._last_web_mode)

            if hasattr(self, "header_cost_badge"):
                self._set_header_cost_badge()

            # Idem para la etiqueta de nombre del archivo adjuntado (ver
            # attachment_name_label más abajo, mismo BLINDAJE de diseño):
            # el widget es persistente (no se reconstruye por turno como
            # MessageBubble), así que necesita este mismo empujón manual.
            if hasattr(self, "attachment_name_label"):
                self.attachment_name_label.setStyleSheet(
                    f"font-size: 11px; color: {theme['secondary']};"
                )

    def _set_node_status_color(self, status_key: str) -> None:
        """Colorea `status_dot` (el punto de estado del nodo en el
        sidebar) según el tema activo, reemplazando el emoji de semáforo
        fijo ("🟢"/"🔴"/"🟡") que llevaba antes incrustado en el propio
        texto de estado - ver la nota en su construcción (_init_ui)."""
        theme = THEMES.get(self._theme_name, THEMES["Cyberpunk Dark"])
        color = theme.get(status_key, theme["secondary"])
        self._node_status_key = status_key
        if hasattr(self, "status_dot") and self.status_dot:
            self.status_dot.setStyleSheet(
                f"background-color: {color}; border-radius: 4px;"
            )

    def _refresh_themed_icons(self, theme: dict[str, str]) -> None:
        """Vuelve a pintar, con los colores del tema recién activado, los
        íconos de los botones construidos una sola vez en _init_ui (ver
        icons.py) - un QIcon es un pixmap ya rasterizado en el momento en
        que se llamó a icons.icon(), así que cambiar el QSS del tema no
        lo actualiza solo."""
        neutral = theme["secondary"]
        _ICON_SIZE_PX = 16

        if hasattr(self, "btn_config"):
            self.btn_config.setIcon(icons.icon("gear", neutral, 20))
            self.btn_config.setIconSize(QSize(20, 20))
        if hasattr(self, "mic_button") and not getattr(self, "_mic_is_recording", False):
            self.mic_button.setIcon(icons.icon("mic", neutral, 22))
            self.mic_button.setIconSize(QSize(22, 22))
        if hasattr(self, "attach_button"):
            self.attach_button.setIcon(icons.icon("attach", neutral, 22))
            self.attach_button.setIconSize(QSize(22, 22))
        self._refresh_tts_toggle_icon()
        if hasattr(self, "btn_donate"):
            self.btn_donate.setIcon(icons.icon("coffee", theme["accent"], _ICON_SIZE_PX))
            self.btn_donate.setIconSize(QSize(_ICON_SIZE_PX, _ICON_SIZE_PX))
        if hasattr(self, "btn_export"):
            self.btn_export.setIcon(icons.icon("save", neutral, _ICON_SIZE_PX))
            self.btn_export.setIconSize(QSize(_ICON_SIZE_PX, _ICON_SIZE_PX))
        if hasattr(self, "btn_export_training"):
            self.btn_export_training.setIcon(icons.icon("dna", neutral, _ICON_SIZE_PX))
            self.btn_export_training.setIconSize(QSize(_ICON_SIZE_PX, _ICON_SIZE_PX))
        if hasattr(self, "btn_download_model"):
            self.btn_download_model.setIcon(icons.icon("download", neutral, _ICON_SIZE_PX))
            self.btn_download_model.setIconSize(QSize(_ICON_SIZE_PX, _ICON_SIZE_PX))
        if hasattr(self, "btn_toggle_terminal"):
            self.btn_toggle_terminal.setIcon(icons.icon("terminal", theme["success"], 15))
            self.btn_toggle_terminal.setIconSize(QSize(15, 15))
        if hasattr(self, "stop_button"):
            self.stop_button.setIcon(icons.icon("stop", theme["danger"], 14))
            self.stop_button.setIconSize(QSize(14, 14))

    def _create_tray_icon(self) -> None:
        self.tray_icon = QSystemTrayIcon(self)
        icon_path = get_resource_path("logo.ico")
        self.tray_icon.setIcon(QIcon(icon_path))

        tray_menu = QMenu()
        restore_action = QAction("Mostrar SovNode", self)
        restore_action.triggered.connect(self._restore_from_tray)
        quit_action = QAction("Salir", self)
        quit_action.triggered.connect(self._quit_application)

        tray_menu.addAction(restore_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_pinned_state_changed(self, is_pinned: bool) -> None:
        """Muestra u oculta el botón flotante de ir al final según la posición de scroll."""
        if hasattr(self, "scroll_to_bottom_btn"):
            self.scroll_to_bottom_btn.setVisible(not is_pinned)

    def _jump_to_bottom(self) -> None:
        """Botón flotante 'ir al final': baja al fondo y re-ancla la vista."""
        if hasattr(self, "chat_scroll"):
            self.chat_scroll.pin_to_bottom()
            self._scroll_to_bottom(force=True)

    def _scroll_to_bottom(self, force: bool = False) -> None:
        """Desplaza la vista al final del chat.

        Por decisión de producto, el auto-scroll SOLO ocurre cuando el
        usuario envía un mensaje (_send_message llama con force=True). El
        resto del turno — streaming de tokens, tarjetas web, indicador de
        "pensando"— ya no arrastra la vista: el usuario puede leer la
        respuesta desde arriba mientras se genera y usar el botón flotante
        "ir al final" si quiere alcanzarla.

        `force=False` (llamadas heredadas): respeta el anclaje al fondo y
        solo sigue al final si la vista ya estaba pegada ahí.
        `force=True`: baja al fondo siempre, sin importar dónde esté el
        scroll.
        """
        if not hasattr(self, "chat_scroll") or not self.chat_scroll:
            return

        if not force and not self.chat_scroll.is_pinned_to_bottom:
            return

        def _force_scroll():
            if self.chat_widget:
                self.chat_widget.adjustSize()
            scrollbar = self.chat_scroll.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

        _force_scroll()
        QTimer.singleShot(10, _force_scroll)
        if force:
            QTimer.singleShot(60, _force_scroll)

    def _flush_stream_buffer(self) -> None:
        if not self._stream_buffer:
            return

        chunk = self._stream_buffer
        self._stream_buffer = ""

        if self._current_stream_bubble:
            self._current_stream_bubble._content += chunk
            self._current_stream_bubble.update_content(
                self._current_stream_bubble._content
            )
        if hasattr(self, "chat_scroll") and self.chat_scroll:
            self.chat_scroll._update_pinned_state()

    def _add_bubble(
        self,
        sender: str,
        content: str,
        trace: Optional[Any] = None,
        is_error: bool = False,
        is_warning: bool = False,
        image_path: Optional[str] = None,
    ) -> MessageBubble:
        timestamp = datetime.now().strftime("%H:%M")
        bubble = MessageBubble(
            sender=sender,
            content=content,
            timestamp=timestamp,
            trace=trace,
            is_error=is_error,
            is_warning=is_warning,
            lang=self._current_lang,
            parent=self.chat_widget,
            image_path=image_path,
        )
        bubble.tts_requested.connect(self._play_tts)
        bubble.tts_stop_requested.connect(self._stop_tts)

        self._bubble_widgets.append(bubble)
        # FIX (bug real preexistente, encontrado al construir el snapshot
        # de pestañas): self._chat_entries se declaraba, se recorría en
        # _export_chat() y se vaciaba en _clear_chat(), pero ningún call
        # site la llenaba nunca — "Exportar chat" siempre mostraba "no
        # hay mensajes para exportar" sin importar cuánto se hubiera
        # conversado. _replay_session_entries() (pestañas) depende de que
        # esta lista sea real, así que de paso corrige _export_chat().
        self._chat_entries.append(
            ChatEntry(sender=sender, content=content, timestamp=timestamp, trace=trace, is_error=is_error)
        )

        item = self.chat_layout.takeAt(self.chat_layout.count() - 1)
        self.chat_layout.addWidget(bubble)
        if item:
            self.chat_layout.addItem(item)

        self._refresh_bubble_widths()
        self._scroll_to_bottom()
        return bubble

    def changeEvent(self, event) -> None:
        super().changeEvent(event)

        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and QSystemTrayIcon.isSystemTrayAvailable()
        ):
            QTimer.singleShot(0, self._hide_to_tray)

    def _toggle_voice_recording(self) -> None:
        if not self._is_recording_voice:
            self._is_recording_voice = True
            self._mic_is_recording = True
            self.mic_button.setIcon(icons.icon("mic", _ACTIVE_THEME["danger"], 22))
            self.mic_button.setIconSize(QSize(22, 22))
            self.mic_button.setText("")
            self.mic_button.setStyleSheet(
                f"background-color: {_ACTIVE_THEME['danger']}; color: white;"
            )
            self._terminal_log(I18N[self._current_lang]["log_voice_listening"], "system")

            whisper_lang = "es" if self._current_lang == "Español" else "en"
            self._voice_worker = VoiceRecorderWorker(language=whisper_lang)
            self._voice_worker.transcription_ready.connect(
                self._on_transcription_ready
            )
            self._voice_worker.error_occurred.connect(
                lambda err: self._terminal_log(err, "error")
            )
            self._voice_worker.start_recording()
        else:
            self._is_recording_voice = False
            self.mic_button.setIcon(QIcon())
            self.mic_button.setText("⏳")
            self.mic_button.setEnabled(False)
            self._terminal_log(
                I18N[self._current_lang]["log_voice_processing"], "info"
            )

            if self._voice_worker:
                self._voice_worker.stop_recording()

    def _on_transcription_ready(self, text: str) -> None:
        self._mic_is_recording = False
        self.mic_button.setIcon(icons.icon("mic", _ACTIVE_THEME["secondary"], 22))
        self.mic_button.setIconSize(QSize(22, 22))
        self.mic_button.setText("")
        self.mic_button.setStyleSheet("")
        self.mic_button.setEnabled(True)

        if text:
            current = self.input_field.toPlainText()
            new_text = f"{current} {text}".strip() if current else text
            self.input_field.setPlainText(new_text)
            self._terminal_log(
                I18N[self._current_lang]["log_voice_transcribed"].format(text), "ok"
            )
        else:
            self._terminal_log(
                I18N[self._current_lang]["log_voice_no_speech"], "warn"
            )

    def _warm_up_whisper_model(self) -> None:
        """
        Precarga `_get_cached_whisper_model()` en un hilo de fondo
        (Python plano, no QThread — no necesita señales ni tocar la UI,
        solo dejar el modelo en `_WHISPER_MODEL_CACHE` antes de que el
        usuario apriete el mic por primera vez) para que ni siquiera la
        PRIMERA grabación de la sesión pague la carga desde disco.
        Mismo patrón que el precalentamiento de `self.general_model` en
        Ollama del lado de Orchestrator, aplicado acá al modelo de STT.

        Si `faster_whisper`/`sounddevice` no están instalados, la carga
        falla acá en silencio (mismo destino que tendría al fallar en
        `VoiceRecorderWorker.run()`, que ya reporta el error real recién
        cuando el usuario intenta grabar) — un warm-up es una
        optimización de latencia, no el punto donde reportar una
        dependencia faltante por primera vez.
        """
        def _warm_up() -> None:
            with contextlib.suppress(Exception):
                _get_cached_whisper_model()

        threading.Thread(target=_warm_up, daemon=True, name="WhisperWarmUp").start()

    def _toggle_tts_enabled(self) -> None:
        """
        Prende/apaga la lectura en voz alta de las respuestas del
        asistente (ver `self.tts_enabled` en __init__ y `_play_tts`, ya
        llamado desde `_on_turn_completed`). El apagado también corta
        cualquier lectura en curso de inmediato — apagar el modo voz a
        mitad de una respuesta no debería dejarla terminando de hablar
        sola.
        """
        self.tts_enabled = not self.tts_enabled
        if not self.tts_enabled:
            self._stop_tts()
        self._refresh_tts_toggle_icon()
        tr = I18N[self._current_lang]
        self._terminal_log(
            tr["log_tts_enabled"] if self.tts_enabled else tr["log_tts_disabled"],
            "info",
        )

    def _refresh_tts_toggle_icon(self) -> None:
        if not hasattr(self, "tts_toggle_button"):
            return
        tr = I18N[self._current_lang]
        color = _ACTIVE_THEME["accent"] if self.tts_enabled else _ACTIVE_THEME["secondary"]
        self.tts_toggle_button.setIcon(icons.icon("speaker", color, 22))
        self.tts_toggle_button.setIconSize(QSize(22, 22))
        self.tts_toggle_button.setToolTip(
            tr["tts_toggle_tooltip_on"] if self.tts_enabled else tr["tts_toggle_tooltip_off"]
        )


def _configure_logging() -> None:
    """
    ES: Corrección (auditoría v3.9 — pedido explícito del usuario, "de
    dónde viene cada bug"): en TODO el codebase hay llamadas
    `logging.getLogger("SovNode.<Módulo>")` (dynamic_tool_engine.py,
    orchestrator.py, training_export.py, y `logger = logging.getLogger
    ("SovNode.UI")` en este mismo archivo, línea ~192) pero nunca, en
    ningún punto de arranque real, se configuró un handler — ni
    `logging.basicConfig`, ni `addHandler`. `app.py` sí tiene
    `basicConfig`, pero `app.py` es el prototipo Streamlit MUERTO (el
    mismo que llama a `Orchestrator.process_turn()`, confirmado inerte
    en la auditoría anterior) — el punto de arranque real es este
    `main()`, que nunca lo llamó.

    Sin ningún handler configurado, Python usa su "handler de último
    recurso": solo WARNING+ llega a stderr, sin timestamp, sin nombre de
    módulo, sin línea — y todo lo INFO/DEBUG se descarta en silencio.
    Por eso hoy, ante un bug, la terminal no dice de qué módulo/función/
    línea vino: esa información SIEMPRE existió en cada registro de
    logging (`record.name`, `record.funcName`, `record.lineno`), pero
    nunca se imprimía. Se corrige con un formato que la expone completa,
    a stdout (visible en la terminal) y a un archivo rotativo (para
    poder revisar después de cerrar la app). Nivel configurable por
    variable de entorno `SOVNODE_LOG_LEVEL` (default INFO) para poder
    subir a DEBUG sin tocar código al perseguir un bug puntual.

    EN: Fix (v3.9 audit — explicit user request, "where does each bug
    come from"): the whole codebase has `logging.getLogger("SovNode.
    <Module>")` calls (dynamic_tool_engine.py, orchestrator.py,
    training_export.py, and `logger = logging.getLogger("SovNode.UI")`
    in this same file, line ~192) but no real startup path ever
    configured a handler — no `logging.basicConfig`, no `addHandler`.
    `app.py` does have `basicConfig`, but `app.py` is the DEAD Streamlit
    prototype (the same one that calls `Orchestrator.process_turn()`,
    confirmed inert in the previous audit) — the real entry point is
    this `main()`, which never called it.

    With no handler configured, Python falls back to its "handler of
    last resort": only WARNING+ reaches stderr, with no timestamp, no
    module name, no line number — and every INFO/DEBUG call is silently
    dropped. That's why, right now, the terminal doesn't say which
    module/function/line a bug came from: that information always
    existed on every log record (`record.name`, `record.funcName`,
    `record.lineno`), it was just never printed. Fixed with a format
    that exposes it in full, to stdout (visible in the terminal) and to
    a rotating file (so it can be reviewed after closing the app). Level
    configurable via the `SOVNODE_LOG_LEVEL` env var (default INFO) to
    bump to DEBUG without touching code while chasing a specific bug.
    """
    from logging.handlers import RotatingFileHandler

    level_name = os.environ.get("SOVNODE_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s:%(funcName)s:%(lineno)d — %(message)s",
        datefmt="%H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    handlers: list[logging.Handler] = [stream_handler]
    with contextlib.suppress(OSError):
        file_handler = RotatingFileHandler(
            "sovnode_debug.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers, force=True)


def main() -> int:
    _configure_logging()

    try:
        myappid = "sovnode.desktop.sovereignai.1.0"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("SovNode")

    global _RESOLVED_FONT_FAMILY
    _RESOLVED_FONT_FAMILY = load_app_fonts()
    app.setFont(QFont(_RESOLVED_FONT_FAMILY, 10))

    icon_path = get_resource_path("logo.ico")
    app.setWindowIcon(QIcon(icon_path))

    app.setQuitOnLastWindowClosed(True)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())