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
    # Nota (medido - costó una sesión entera de diagnóstico
    # encontrarlo): `sanitize_query` NO se importa de web_search.py a
    # propósito. Este archivo define su PROPIA `sanitize_query` más abajo
    # (multi-pasada, con limpieza de ruido interno/de cierre además de
    # rellenos iniciales - más completa que la de web_search.py para las
    # frases que esta capa necesita cubrir). Antes sí se importaba aquí,
    # pero Python simplemente REBINDEA el nombre en la definición local de
    # más abajo - la importación quedaba silenciosamente ignorada, sin
    # ningún error ni warning, dando la falsa impresión de que este
    # archivo reusaba la versión "oficial" cuando en realidad nunca la
    # tocaba. Ese mismatch fue la causa real de que un fix ya aplicado en
    # web_search.py (cobertura de "on internet" sin la palabra "the") no
    # tuviera ningún efecto en la Capa 1/Capa 2 de este archivo. Se retira
    # el import para que la ausencia sea explícita, no una trampa.
except ImportError:
    from web_search import search_web
    def get_last_search_error():
        return None
    def is_live_event_query(query):
        return False
    def text_is_relevant(query, text):
        return True
    WIKI_FINAL_EXTRACT_CHARS = 7000
    WIKI_PRECISE_FACT_EXTRACT_CHARS = 2500
    WIKI_SEQUENCE_BUDGET_SECONDS = 4.0
    WIKI_API_THUMB_SIZE = 800
    def wiki_rank_search_candidates(*args, **kwargs):
        return []
    def wiki_fetch_single_extract(*args, **kwargs):
        return None
# Criterio de relevancia compartido por las tres capas del pipeline (ver
# el docstring de relevance.py). Antes esta capa tenía su propia copia de
# la extracción de años y del patrón de título retrospectivo, así que cada
# arreglo en web_search.py había que recordar replicarlo aquí a mano.
# Criterio de relevancia compartido por las tres capas del pipeline (ver
# el docstring de relevance.py).
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
    QThread,
    QTimeLine,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QIcon,
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
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSystemTrayIcon,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from logger import format_terminal_log
import math_render
from ollama_manager import OllamaProcessManager
from orchestrator import (
    Orchestrator,
)
from embeddings import prewarm_local_embedding_model
from ui import AutoResizingTextBrowser, ChatDropArea
from wal import WriteAheadLog

# Logger unificado para la capa de UI (consola gráfica vs. logs internos
# - ver _on_health_check_completed, que solo promueve a la consola
# visible los CAMBIOS de estado de Ollama, mientras las comprobaciones
# periódicas sin cambios quedan en DEBUG aquí, sin ensuciar la UI).
logger = logging.getLogger("SovNode.UI")


# =====================================================================
# Nota DE BÚSQUEDA WEB - Sanitización, parsing estructurado y
# resiliencia multi-capa ante fallos de red, SSL, rate-limit, etc.
# =====================================================================


# Nota (rediseño tras varios bugs reales de la misma familia - ver
# el historial de esta sección: "on internet", "in internet", "can/
# could/would/will you", residuo de LLM-rewrite al cierre...). Cada fix
# anterior tapaba una POSICIÓN concreta (inicio con `_LEADING_FILLERS_RE`,
# cierre con `_TRAILING_NOISE_RE`) donde se había CAPTURADO en vivo una
# muletilla - pero el vocabulario "de canal" (verbos de búsqueda,
# "internet", peticiones tipo "can/could/would/will you") puede caer en
# cualquier posición según cómo lo redacte el usuario o cómo lo reordene
# `rewrite_search_query_via_llm`, y una lista anclada nunca cubre todas
# las posiciones a la vez. `_NOISE_ANYWHERE_RE` quita este vocabulario
# SIN importar dónde caiga - sustituye a la parte de `_LEADING_FILLERS_RE`
# que antes solo actuaba si era la PRIMERA palabra, y a la sustitución
# manual de "internet/google/online" que antes solo cubría eso. Deja
# fuera a propósito las frases de FRAMING más largas y específicas
# ("el resultado y detalles de", "quiero saber", "sabes algo sobre"...):
# esas son mucho menos propensas a aparecer fuera de la apertura de la
# frase, y anclarlas reduce el riesgo de comerse contenido legítimo por
# coincidencia.
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

# Saludos: solo son ruido cuando ABREN la frase - "hola, dime la final"
# sí es saludo; a mitad de frase "hi"/"hey" puede ser parte legítima del
# tema (nombres, títulos), así que se mantienen ancladas al inicio.
_LEADING_GREETINGS_RE = re.compile(
    r"^\s*[?¡!¿.,;:'\"]*(hola|hey|buenas|hi|hello)\b\s*[?¡!¿.,;:'\"]*\s*",
    re.IGNORECASE,
)

# Frames de apertura más largos/específicos (deliberadamente ANCLADOS
# al inicio - ver nota de arriba): mucho menos propensos a aparecer
# fuera de la apertura, y anclarlos reduce el riesgo de falsos positivos
# sobre contenido legítimo que use estas mismas palabras con otro
# sentido en medio de la frase.
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

_MAX_SANITIZE_PASSES = 6  # cota dura anti-bucle-infinito; en la práctica converge en 2-3


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

            # Muletillas de "canal de búsqueda" (verbos de acción, "internet",
            # peticiones "can/could/would/will you"...) en cualquier posición
            # del texto - ver la nota en la definición de
            # `_NOISE_ANYWHERE_RE` más arriba para el historial completo de
            # bugs reales que motivó dejar de anclar esto al inicio/cierre.
            q = _NOISE_ANYWHERE_RE.sub(" ", q)

            # Verbos/llamadas a acción internas que no son "canal de
            # búsqueda" puro (estadísticas de, detalles de...).
            q = _INTERNAL_NOISE_RE.sub(" ", q)

            # Saludos de apertura ("hola", "hey"...).
            q = _LEADING_GREETINGS_RE.sub("", q)

            # Frames de apertura más largos y específicos ("quiero saber",
            # "el resultado de", "sabes algo sobre"...).
            q = _LEADING_FILLERS_RE.sub("", q)

            # Ruido de cierre ("en tiempo real", "detalladamente", residuo
            # suelto de "search"/"internet"/"busca" que sobrevivió al
            # primer paso por quedar pegado a otra palabra).
            q = _TRAILING_NOISE_RE.sub("", q).strip()

            # Normalizar espacios entre rondas para que los patrones ancla (^/$)
            # vuelvan a alinearse correctamente en la siguiente pasada.
            q = re.sub(r"\s{2,}", " ", q).strip()

            if q == before:
                break  # punto fijo alcanzado: no hay más muletillas que quitar

        # Limpieza final de puntuación decorativa sobrante (no toca dígitos,
        # letras acentuadas, ni el resto de puntuación semántica del cuerpo).
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


# =====================================================================
# INVOCACIÓN SEMÁNTICA DE IMÁGENES - filtro por tema (Módulo 4)
# =====================================================================
# Allowlist deliberada, no denylist: las tarjetas de imagen solo se
# muestran para temas donde una foto realmente aporta algo (deporte,
# noticias de impacto, personas). Para todo lo demás (física, matemáticas,
# código, filosofía, conceptos abstractos) la búsqueda web se sigue
# ejecutando igual -el LLM recibe el texto normalmente- pero la tarjeta
# visual se omite: una foto de stock elegida por reputación de dominio no
# aporta nada a una respuesta sobre mecánica cuántica y solo es ruido.
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
    Clasificador determinista (regex, sin costo de otra llamada al modelo)
    que decide si el tema de `query` justifica renderizar tarjetas de
    imagen. Allowlist, no denylist: solo devuelve True para deportes,
    catástrofes/noticias de impacto o biografías/personas — cualquier otra
    cosa (física, matemáticas, código, filosofía, o cualquier tema que no
    matchee ninguna categoría) devuelve False por diseño, sin necesitar una
    lista aparte de temas abstractos a excluir. La búsqueda de texto para
    el LLM se sigue ejecutando igual; solo se omite la tarjeta visual.
    """
    text = (query or "").strip()
    if not text:
        return False

    return bool(
        _VISUAL_TOPIC_SPORTS_RE.search(text)
        or _VISUAL_TOPIC_DISASTER_RE.search(text)
        or _VISUAL_TOPIC_BIOGRAPHY_RE.search(text)
    )


# Requisito: soporte multilingüe para la API de Wikipedia - antes
# hardcodeada a es.wikipedia.org sin importar el idioma real de la
# consulta o de la conversación. Palabras funcionales cortas y muy
# frecuentes que casi nunca coinciden entre ES/EN: una consulta que
# tenga más hits en una lista que en la otra se asume en ese idioma.
# Es un fallback deliberadamente simple (no es un detector de idioma
# real) para cuando el llamador no puede pasar el idioma explícito de
# la conversación - que es la señal preferida y mucho más confiable
# (ver StreamTurnWorker, que sí conoce self._orchestrator.current_language).
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


# Prefijo del CDN de imágenes de Wikimedia: `upload.wikimedia.org` sirve
# las FOTOS reales de los artículos (las que ahora pide
# `_search_via_wikipedia` vía `prop=...|pageimages`, a `pithumbsize`
# completo), a diferencia de una URL de artículo `*.wikipedia.org/wiki/...`,
# que no es una imagen en absoluto.
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
        return False  # foto real del CDN de Wikimedia
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


# Nota (auditoría de idioma, pedida explícitamente): los mensajes
# `[WEB_SEARCH] ...` que aparecen en la terminal salen de web_search.py,
# que a propósito NO importa nada de este proyecto (evita un ciclo de
# imports con orchestrator.py, que sí lo importa a él) - así que nunca
# tuvo acceso al selector de idioma de la interfaz y sus ~35 mensajes
# quedaron siempre en español, sin importar el idioma elegido en la UI.
# En vez de romper esa separación arquitectónica pasando `lang` a través
# de cada función de web_search.py (varias, como `_call_with_backoff`,
# son genéricas y no deberían saber de idioma), se traduce en el punto
# donde sí se conoce el idioma de interfaz: aquí, justo antes de que el
# mensaje le llegue a `self.log_message.emit`. Basado en PATRONES (la
# parte dinámica - URLs, conteos, nombres de motor - se preserva vía
# grupos de captura), no en una lista de mensajes exactos: un mensaje
# que no matchee ningún patrón simplemente queda en español, degradando
# con gracia en vez de romper algo.
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

    # Idioma resuelto una sola vez para toda la función - antes cada capa
    # decidía el idioma por su cuenta (Capa 1 no lo hacía en absoluto,
    # Capa 2 sí) y esa inconsistencia era la causa real de que una
    # consulta en inglés trajera snippets de Capa 1 en español: DDGS sin
    # `region` no tiene preferencia de idioma y termina devolviendo lo que
    # sea que la red geolocalice.
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

    # Calculado una sola vez para toda la función (antes solo se
    # calculaba dentro del bloque de Capa 2) - Capa 1 también lo necesita
    # ahora para saber si debe llevar la cuenta de cuántas fuentes
    # propias aportó a una consulta de evento en vivo/rumor (ver
    # `capa1_count` y `wiki_only_backup` más abajo).
    query_is_live = is_live_event_query(clean_q)

    # Eje independiente de `query_is_live`: la consulta pide un dato
    # puntual y verificable (marcador, ganador, campeón), sea el evento
    # presente o pasado. `query_strict` (la disyunción de ambos) es el
    # gate de los filtros de relevancia de la Capa 2 más abajo: antes
    # estaban gateados solo por `query_is_live`, así que "quién ganó la
    # final del Mundial 2018" -evento cerrado, sin "hoy" ni "último"- no
    # activaba NINGUNA de las dos defensas y Wikipedia colaba artículos
    # sin relación. Ver relevance.py.
    query_needs_fact = requires_precise_fact(clean_q)
    query_strict = needs_strict_relevance(clean_q)

    # Palabras clave en títulos/URLs que indican páginas de índice no informativas
    INVALID_TITLE_PATTERNS = re.compile(
        r"^(categor[ií]a:|anexo:|lista de|category:|disambiguation|desambiguaci[oó]n)",
        re.IGNORECASE,
    )

    # --- CAPA 1: motor estructurado (DuckDuckGo/SearXNG, vía web_search.py) ---
    # Se consume search_web() directamente (dicts ya filtrados por
    # reputación de dominio y enriquecidos con contenido/imagen real) en
    # vez de reconstruir esa información parseando el bloque de texto
    # formateado para el LLM - así la tarjeta recibe también 'image'
    # (og:image o favicon de respaldo) para las miniaturas.
    # `capa1_count` (nota, diagnóstico "Rodri"): cuenta cuántas
    # fuentes reales aportó esta capa - si es 0 para una consulta de
    # evento en vivo/rumor y Capa 2 (Wikipedia) termina rellenando todo,
    # `wiki_only_backup` más abajo se lo señala a StreamTurnWorker para
    # que inyecte una advertencia anti-alucinación más fuerte que las
    # reglas genéricas.
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

                # Ignorar títulos de categorías o listas vacías de Wikipedia
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

    # --- CAPA 2: Wikipedia API (solo texto y extractos, sin imágenes de Wikimedia) ---
    # Para consultas de dato puntual verificable, Wikipedia deja de ser
    # "respaldo cuando hay menos de 3 resultados" y pasa a consultarse
    # siempre: su infobox suele traer el marcador/ganador en sí, mientras
    # que la Capa 1 puede devolver dos crónicas de reacción legítimas y
    # bien posicionadas donde el dato no aparece en ninguna parte (el caso
    # real que motivó esto - ver `is_hard_data_source` en web_search.py).
    # Los filtros de abajo siguen aplicando, así que un artículo de otro
    # año o sin relación se descarta igual.
    wiki_authoritative_text = ""
    # --- CAPA 2: Wikipedia API (solo texto y extractos, sin imágenes de Wikimedia) ---
    # Para consultas de dato puntual verificable, Wikipedia deja de ser
    # "respaldo cuando hay menos de 3 resultados" y pasa a consultarse siempre.
    if len(results["snippets"]) < 3 or query_needs_fact:
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
            _emit_log_safe(
                log_cb,
                "[WEB_SEARCH] Wikipedia sin fuente específica de la final, reintentando con "
                "consulta más específica..."
                if wants_final_source else
                "[WEB_SEARCH] Wikipedia sin resultado útil, reintentando con consulta más específica...",
            )
            _run_wikipedia_pass(f"{clean_q} final")

    # Nota (participante no confirmado): para consultas de dato
    # puntual con extracto de Wikipedia disponible, cualquier fuente de
    # Capa 1 cuyo TÍTULO nombre un equipo/país/persona que Wikipedia
    # nunca menciona se descarta.
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
        # Si la consulta pide datos en vivo/recientes ("last match",
        # "resultado de hoy"...), un artículo enciclopédico genérico de
        # Wikipedia sin relación real con la consulta es peor que no
        # tener nada - se descarta más abajo con text_is_relevant()
        # (mismo criterio que usa search_web() para Capa 1, ver
        # web_search.py). Consultas que NO son de evento en vivo no se
        # filtran: un artículo enciclopédico normal sí es útil ahí.
        # (`query_is_live` ya se calculó arriba, antes de Capa 1.)
        wiki_domain = "en.wikipedia.org" if resolved_lang == "en" else "es.wikipedia.org"

        # Nota (bug: Mundial 2022, resultado de la final - ver
        # diagnostico completo en la sesion): 280 caracteres estaba
        # HARDCODEADO y siempre con `exintro`, asi que un dato que no vive
        # en la primera frase del articulo (el marcador de una final,
        # casi siempre unas frases mas abajo) quedaba fuera del extracto
        # sin importar cuanto lo pidiera el usuario. `query_needs_fact` ya
        # esta calculado arriba - se reusa para pedir el cuerpo completo
        # del articulo con un exchars mucho mayor SOLO en ese caso, igual
        # que ya hace el motor paralelo de web_search.py
        # (_search_via_wikipedia).
        #
        # nota 2 (medido contra la estructura real del
        # artículo - "Search the final of world cup 2022"): incluso con
        # el presupuesto de arriba, el marcador de LA final seguía sin
        # entrar al extracto. Todo artículo "X World Cup final" de
        # Wikipedia sigue el mismo patrón: introducción → "Background" →
        # "Route to the final" (el camino de ambos equipos hasta llegar
        # ahí, con los marcadores de sus propios octavos/cuartos/
        # semifinal) → recién ahí "Summary", con el marcador real del
        # partido. Solo "Route to the final" mide ~3500-4000 caracteres
        # (verificado contra el artículo real) - más que todo
        # WIKI_PRECISE_FACT_EXTRACT_CHARS (2500) - así que un recorte de
        # 2500 caracteres nunca llegaba a "Summary": se cortaba a mitad
        # de camino, habiendo incluido de rebote los marcadores de rondas
        # ANTERIORES (semifinal, cuartos...) como si fueran evidencia de
        # la final. Consecuencia medida en vivo: ScoreCheck rechazaba
        # bien un marcador de semifinal mal atribuido a la final, pero la
        # corrección posterior no tenía el marcador CORRECTO disponible
        # para ofrecerlo - el turno terminaba en "las fuentes no reportan
        # el resultado" en vez de la respuesta correcta. Mismo criterio
        # `asks_about_final` ya usado para el ordenamiento de candidatos,
        # arriba - un presupuesto mayor SOLO cuando la consulta pide la
        # final específicamente, para no inflar el contexto del resto de
        # consultas de hecho puntual que no tienen este problema.
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
                # Nota (medido - "search the final of world
                # cup 2014"): esta Capa 2 puede sumar hasta 5 fuentes,
                # pero antes las tomaba en el orden crudo que devuelve el
                # ranking de Wikipedia - si el artículo GENÉRICO del
                # torneo rankea antes que el ESPECÍFICO de la final y la
                # Capa 1 ya trajo 2-3 fuentes, el específico puede quedar
                # afuera del cupo de 5 sin haber tenido oportunidad. Mismo
                # criterio que ya se aplica en el ordenamiento de
                # search_web() (`title_names_the_final`): si la consulta
                # pide la final, se lo antepone sin alterar el orden
                # relativo entre el resto de los candidatos.
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
                        # Se pide `thumb_size` chico y se ignora el
                        # resultado ("thumbnail": None abajo) - esta capa
                        # deliberadamente no usa imágenes de Wikipedia.
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

                        # Filtrar desambiguaciones, anexos y categorías
                        if INVALID_TITLE_PATTERNS.search(title):
                            continue

                        # Nota (diagnóstico "Rodri"/Barcelona): un
                        # artículo retrospectivo ("Historia del FC
                        # Barcelona 2000-2010") comparte palabras clave
                        # con la consulta y pasa text_is_relevant() sin
                        # problema - se descarta por patrón de título
                        # ("historia de/del...", "history of...", o un
                        # rango de años en el título). Gateado por
                        # `query_strict`, no por `query_is_live`: un
                        # evento pasado también exige precisión.
                        if query_strict and is_retrospective_title(title):
                            continue

                        extract = clean_html_text(page["extract"])

                        if (
                            query_strict
                            and not text_is_relevant(clean_q, f"{title} {extract}")
                        ):
                            continue

                        # Nota: si la consulta trae un año explícito
                        # (p. ej. "2026") y el artículo trae OTRO año
                        # explícito que no coincide (p. ej. un artículo
                        # titulado/extractado sobre "2002"), se descarta
                        # - text_is_relevant() de arriba no lo detecta
                        # porque comparte otras palabras ("world","cup",
                        # "final"). Si el artículo simplemente no
                        # menciona ningún año (p. ej. un resumen genérico
                        # del torneo), no se descarta por esto - solo se
                        # rechaza un desajuste EXPLÍCITO, nunca la
                        # ausencia de año.
                        query_years = extract_years(clean_q)
                        if query_years:
                            article_years = extract_years(f"{title} {extract}")
                            if article_years and not (query_years & article_years):
                                continue

                        if extract and len(extract) > 30 and len(results["snippets"]) < 5:
                            # Nota (sobre el mismo bug ya documentado
                            # arriba, "Route to the final" ~3500-4000+
                            # caracteres antes de "Summary"): un exchars
                            # más grande no garantiza que el marcador real
                            # quede DENTRO del prefijo - el artículo puede
                            # seguir siendo más largo que el presupuesto,
                            # o el corte crudo de la API puede caer justo
                            # antes de la oración que importa. En vez de
                            # confiar en la posición cruda, se aplica el
                            # mismo ranking por relevancia de oraciones
                            # que Capa 1 ya usa para artículos scrapeados
                            # (ver ARTICLE_MAX_CHARS en web_search.py) -
                            # así la oración con el marcador real sube al
                            # frente aunque haya aparecido tarde en el
                            # extracto crudo.
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
                                    "thumbnail": None,  # No usamos la foto de Wikipedia
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

        # Reintento dirigido (punto 2b): para consultas de hecho puntual,
        # el título correcto casi nunca es el artículo genérico del
        # torneo sino el del EVENTO especifico ("2022 FIFA World Cup
        # final", no "2022 FIFA World Cup") - Wikipedia's gsrsearch no
        # siempre lo prioriza con el texto tal cual lo escribió el
        # usuario.
        #
        # Nota (medido - "search the final of world cup
        # 2014"): la condición original saltaba este reintento cada vez
        # que la consulta YA decía "final" en cualquier parte (pensado
        # para evitar un reintento idéntico al primer intento) - pero
        # "search the final of..." / "quién ganó la final de..." es
        # precisamente la forma MÁS COMÚN de pedir una final, así que el
        # reintento quedaba deshabilitado justo para el caso que más lo
        # necesita. La condición correcta no es "la consulta ya
        # menciona la palabra final" sino "ya se consiguió una fuente
        # cuyo TÍTULO nombra la final en sí" (`title_names_the_final`,
        # mismo criterio que el resto de este arreglo) - si ninguna
        # fuente reunida hasta ahora la nombra, vale la pena reintentar
        # aunque la consulta ya dijera "final", porque el problema
        # nunca fue lo que el usuario escribió sino que Wikipedia no
        # rankeó el artículo específico arriba del genérico. Se
        # comprueba contra `results["sources"]` completo (Capa 1 +
        # esta misma pasada de Capa 2), así que si Capa 1 ya trajo el
        # artículo específico, no se malgasta una llamada de más.
        wants_final_source = query_needs_fact and asks_about_final(clean_q)
        already_has_final_source = wants_final_source and any(
            title_names_the_final(s.get("title", "")) for s in results["sources"]
        )
        if query_needs_fact and not already_has_final_source and (wiki_added == 0 or wants_final_source):
            _emit_log_safe(
                log_cb,
                "[WEB_SEARCH] Wikipedia sin fuente específica de la final, reintentando con "
                "consulta más específica..."
                if wants_final_source else
                "[WEB_SEARCH] Wikipedia sin resultado útil, reintentando con consulta más específica...",
            )
            _run_wikipedia_pass(f"{clean_q} final")

    # Nota (participante no confirmado): para consultas de dato
    # puntual con extracto de Wikipedia disponible, cualquier fuente de
    # Capa 1 cuyo TÍTULO nombre un equipo/país/persona que Wikipedia
    # nunca menciona se descarta. Caso real: un artículo tituló "Spain
    # and Argentina face off in the World Cup final at the NY/NJ
    # stadium" para una consulta sobre el Mundial 2022 - pasó
    # `text_is_relevant` (comparte "world"/"cup"/"final" con la
    # consulta) y `has_conflicting_year` (el título no traía ningún año
    # explícito que contradecir), pese a describir un partido que nunca
    # ocurrió. Se compara solo contra el TÍTULO de cada fuente, no el
    # snippet completo, para no descartar artículos legítimos que
    # mencionan de paso otros nombres propios (comentaristas, sedes,
    # comparaciones históricas) en el cuerpo. Solo se aplica si Wikipedia
    # realmente aportó una base con la que contrastar - si no, no hay
    # autoridad contra la cual rechazar nada.
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

    # Nota (Point B del diagnóstico "Rodri"): si Capa 1 no aportó
    # NINGUNA fuente propia para una consulta de evento en vivo/rumor y
    # toda la evidencia terminó viniendo de Capa 2 (Wikipedia, un resumen
    # enciclopédico genérico, nunca noticias en vivo), se marca aquí -
    # StreamTurnWorker (sovnode_qt.py) usa esta bandera para inyectar una
    # advertencia anti-alucinación más explícita que las reglas
    # genéricas, porque un modelo local (7B) puede malinterpretar un
    # artículo enciclopédico sin relación real como "confirmación" del
    # rumor/fichaje. Si Capa 1 sí aportó algo, o la consulta no es de
    # evento en vivo, o Capa 2 no aportó nada, la bandera queda False.
    results["wiki_only_backup"] = bool(
        query_is_live and capa1_count == 0 and len(results["sources"]) > capa1_count
    )

    # --- REASIGNACIÓN DE FOTO DE LA WEB ---
    # Busca la primera foto de alta calidad que provenga de la web general
    # (no de la Wiki, y no un favicon de respaldo). Los favicons de
    # web_search._favicon_url() (icons.duckduckgo.com) son intencionalmente
    # diminutos (16x16/32x32): _ThumbnailLoader los rechaza por calidad para
    # los slots hero/sub (300x150 / 160x90), así que una fuente con SOLO un
    # favicon como thumbnail terminaría igual mostrando las iniciales de
    # respaldo - hay que tratarla como "sin miniatura real" aquí también,
    # no solo cuando el campo está vacío. (`_is_low_quality_thumbnail` es
    # ahora una función de módulo, compartida con el gate de
    # WebSearchResultsWidget en _on_web_results_ready.)
    best_web_image = None
    for src in results["sources"]:
        thumb = src.get("thumbnail")
        if not _is_low_quality_thumbnail(thumb):
            best_web_image = thumb
            break

    # Ninguna fuente propia dio una foto real: cualquier fuente sin
    # miniatura útil (vacía, de Wikipedia o solo favicon) hereda la mejor
    # encontrada en el resto de la lista, en vez de quedarse con las
    # iniciales de respaldo pudiendo mostrar una foto real.
    if best_web_image:
        for src in results["sources"]:
            if _is_low_quality_thumbnail(src.get("thumbnail")):
                src["thumbnail"] = best_web_image

    # Diagnóstico real en vez de un "sin resultados" genérico: si
    # search_web() (Capa 1) registró un fallo real de backend (timeout,
    # bloqueo/rate-limit del proveedor, etc.) para esta consulta, se
    # registra y reporta aquí siempre que exista - incluso cuando la
    # Capa 2 (Wikipedia u otro proveedor secundario) alcanzó a rellenar
    # snippets/sources parcialmente. Antes solo se consultaba
    # get_last_search_error() cuando ambas capas quedaban completamente
    # vacías, así que un fallo real del proveedor primario quedaba
    # enmascarado sin dejar rastro apenas Wikipedia aportaba algo.
    # Sin `print()` propio: web_search._run_engine_guarded ya loguea este
    # mismo texto vía logger.warning al registrarlo, así que imprimirlo
    # aquí otra vez duplicaba la línea en la consola. El valor sí se
    # sigue usando abajo para el `status_message` visible en la UI.
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


def get_resource_path(relative_path: str) -> str:
    """Obtiene la ruta absoluta para recursos empaquetados por PyInstaller."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


APP_NAME = "SovNode"
APP_TITLE = "SovNode — Sovereign AI Node v2.0"
DEV_MODE = True
SUPPORTED_DROP_EXTENSIONS = {".py", ".txt", ".md", ".json", ".csv"}
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
        "btn_donate": "☕ Apoyar el proyecto",
        "support_desc": "SovNode es 100% gratuito y privado. Tu apoyo impulsa el desarrollo.",
        "btn_new_chat": "🧹 Nueva conversación",
        "btn_export": "💾 Exportar chat (.md)",
        "btn_export_training": "🧬 Exportar dataset de entrenamiento",
        "btn_minimize": "— Minimizar a bandeja",
        "header_title": "Consola de comandos",
        "header_subtitle": "Sesión soberana local · Enter para enviar · Shift+Enter para nueva línea",
        "placeholder": "Escribe una instrucción para SovNode...",
        "btn_send": "Enviar",
        "btn_stop": "🛑 Detener",
        "processing": "SovNode está procesando...",
        "terminal_btn_show": "🖥️ Terminal",
        "terminal_btn_hide": "🖥️ Ocultar Terminal",
        "status_online": "🟢 Online · Ollama local",
        "status_offline": "🔴 Offline · Ollama no disponible",
        "status_checking": "🟡 Verificando nodo local...",
        "header_online": "● Nodo local en línea",
        "header_offline": "● Nodo local sin conexión",
        "role_general": "Rol: conversación general",
        "role_coder": "Rol: generación de código",
        "turns_count": "Turnos procesados: {}",
        "welcome_msg": "Sistema iniciado. Estoy listo para recibir instrucciones.",
        "new_chat_msg": "Nueva conversación iniciada. ¿En qué puedo ayudarte?",
        "donate_title": "Apoyar SovNode v2.0",
        "donate_header": "☕ Apoya el desarrollo de SovNode",
        "donate_desc": "SovNode es un proyecto 100% independiente y de código abierto. Si la herramienta te resulta útil, cualquier contribución ayuda a mantener el desarrollo activo de nuevas funciones.",
        "donate_btn_kofi": "🌐 Donar vía Ko-fi / PayPal",
        "donate_crypto_title": "💳 USDT (Red TRON / TRC-20):",
        "donate_btn_copy": "Copiar",
        "donate_copy_title": "Dirección Copiada",
        "donate_copy_msg": "La dirección USDT (TRC-20) se copió al portapapeles.",
        "jump_to_bottom": "⬇ Ir al final",
        "web_badge_live": "🌐 RED VIVA",
        "web_badge_local": "💾 MEMORIA LOCAL",
        "msg_sender_user": "🧑 TÚ",
        "msg_sender_error": "⚠️ ERROR",
        "msg_sender_warning": "🛠️ SOVNODE (auto-corregido)",
        "btn_download_model": "📥 Descargar modelo",
        "download_dialog_title": "Descargar modelo de Ollama",
        "download_dialog_desc": "Ingresa o selecciona el tag del modelo a descargar (ej. gpt-oss:20b):",
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
        # Nota (medido): antes decía siempre "trabajo Python
        # local, sin llamada a Ollama en curso" - falso para el sitio
        # "QueryRewrite", que sí puede incluir una llamada HTTP bloqueante
        # a Ollama (ver rewrite_search_query_via_llm). Mensaje neutral;
        # el detalle de red/Python se ve comparando este total contra la
        # línea "⏱️ [QueryRewrite-LLM-Call]" (esa sí, siempre red pura).
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
    },
    "English": {
        "theme_title": "VISUAL THEME",
        "lang_title": "LANGUAGE / IDIOMA",
        "status_card": "NODE STATUS",
        "session_card": "SESSION STATUS",
        "support_card": "OPEN SOURCE PROJECT",
        "btn_donate": "☕ Support the project",
        "support_desc": "SovNode is 100% free and private. Your support keeps development active.",
        "btn_new_chat": "🧹 New conversation",
        "btn_export": "💾 Export chat (.md)",
        "btn_export_training": "🧬 Export training dataset",
        "btn_minimize": "— Minimize to tray",
        "header_title": "Command Console",
        "header_subtitle": "Local sovereign session · Enter to send · Shift+Enter for new line",
        "placeholder": "Type an instruction for SovNode...",
        "btn_send": "Send",
        "btn_stop": "🛑 Stop",
        "processing": "SovNode is processing...",
        "terminal_btn_show": "🖥️ Terminal",
        "terminal_btn_hide": "🖥️ Hide Terminal",
        "status_online": "🟢 Online · Local Ollama",
        "status_offline": "🔴 Offline · Ollama unavailable",
        "status_checking": "🟡 Verifying local node...",
        "header_online": "● Local node online",
        "header_offline": "● Local node offline",
        "role_general": "Role: general conversation",
        "role_coder": "Role: code generation",
        "turns_count": "Processed turns: {}",
        "welcome_msg": "System initialized. Ready to receive instructions.",
        "new_chat_msg": "New conversation started. How can I help you?",
        "donate_title": "Support SovNode v2.0",
        "donate_header": "☕ Support SovNode Development",
        "donate_desc": "SovNode is a 100% independent and open-source project. If you find this tool useful, any contribution helps keep development active with new features.",
        "donate_btn_kofi": "🌐 Donate via Ko-fi / PayPal",
        "donate_crypto_title": "💳 USDT (TRON Network / TRC-20):",
        "donate_btn_copy": "Copy",
        "donate_copy_title": "Address Copied",
        "donate_copy_msg": "The USDT (TRC-20) address was copied to the clipboard.",
        "jump_to_bottom": "⬇ Jump to bottom",
        "web_badge_live": "🌐 LIVE WEB",
        "web_badge_local": "💾 LOCAL MEMORY",
        "msg_sender_user": "🧑 YOU",
        "msg_sender_error": "⚠️ ERROR",
        "msg_sender_warning": "🛠️ SOVNODE (auto-corrected)",
        "btn_download_model": "📥 Download model",
        "download_dialog_title": "Download Ollama model",
        "download_dialog_desc": "Enter or select the model tag to download (e.g. gpt-oss:20b):",
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
    },
}


THEMES = {
    "Cyberpunk Dark": {
        "bg": "#0E1117",
        "sidebar": "#14171F",
        "card": "#171B24",
        "input": "#1B1F2A",
        "assistant": "#1E222C",
        "user": "#1E3A8A",
        "accent": "#4C8BF5",
        "accent_soft": "#1E2A44",
        "text": "#E6E8EC",
        "secondary": "#8B92A5",
        "border": "#262B36",
        "success": "#3DDC97",
        "warning": "#F2C14E",
        "danger": "#F2555A",
        "code": "#10141D",
        "coder_model": "#F2C14E",
        "general_model": "#4C8BF5",
    },
    "OLED Pure Black": {
        "bg": "#000000",
        "sidebar": "#080808",
        "card": "#101010",
        "input": "#151515",
        "assistant": "#171717",
        "user": "#312E81",
        "accent": "#7C9CFF",
        "accent_soft": "#1D2450",
        "text": "#FFFFFF",
        "secondary": "#B4B4B4",
        "border": "#303030",
        "success": "#5AFFAA",
        "warning": "#FFD166",
        "danger": "#FF6B6B",
        "code": "#070707",
        "coder_model": "#FFD166",
        "general_model": "#7C9CFF",
    },
    "Nordic Slate": {
        "bg": "#2E3440",
        "sidebar": "#252B36",
        "card": "#3B4252",
        "input": "#434C5E",
        "assistant": "#3B4252",
        "user": "#4C566A",
        "accent": "#88C0D0",
        "accent_soft": "#3D5167",
        "text": "#ECEFF4",
        "secondary": "#D8DEE9",
        "border": "#4C566A",
        "success": "#A3BE8C",
        "warning": "#EBCB8B",
        "danger": "#BF616A",
        "code": "#272D38",
        "coder_model": "#EBCB8B",
        "general_model": "#88C0D0",
    },
}


def build_style(theme: dict[str, str]) -> str:
    """Genera el QSS completo para el tema seleccionado, con estética HUD/Cyberpunk pulida."""
    return f"""
        QMainWindow, QWidget#centralWidget {{
            background-color: {theme["bg"]};
            color: {theme["text"]};
        }}

        QFrame#sidebar {{
            background-color: {theme["sidebar"]};
            border-right: 1px solid {theme["border"]};
        }}

        QFrame#sidebar QLabel, QFrame#sidebarCard QLabel {{
            color: {theme["text"]};
        }}

        QFrame#sidebarCard {{
            background-color: {theme["card"]};
            border: 1px solid {theme["border"]};
            border-radius: 12px;
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
            border-radius: 8px;
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

        QPushButton#secondaryButton {{
            background-color: {theme["card"]};
            color: {theme["secondary"]};
            border: 1px solid {theme["border"]};
            border-radius: 8px;
            padding: 9px;
            font-size: 12px;
        }}

        QPushButton#secondaryButton:hover {{
            color: {theme["text"]};
            border-color: {theme["accent"]};
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
            border-radius: 6px;
            padding: 4px 8px;
            font-size: 10px;
        }}

        QPushButton#codeActionButton:hover {{
            color: {theme["accent"]};
            border-color: {theme["accent"]};
        }}

        QPushButton#traceButton {{
            background-color: {theme["code"]};
            color: {theme["secondary"]};
            border: 1px solid {theme["border"]};
            border-radius: 6px;
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
            border-radius: 7px;
            padding: 6px;
            font-size: 11px;
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
            border-radius: 7px;
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
        self._timer.setInterval(16)  # ~60fps
        self._timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        self._angle = (self._angle + 6) % 360
        self.update()

    def start(self) -> None:
        self._should_spin = True
        # Si el widget está oculto (p. ej. la ventana minimizada a la
        # bandeja mientras un turno sigue procesando), no arranca el
        # QTimer todavía - showEvent() lo hace cuando vuelva a ser
        # visible. Evita forzar repintados a 60 FPS de un widget que
        # nadie puede ver.
        if self.isVisible():
            self._timer.start()

    def stop(self) -> None:
        self._should_spin = False
        if hasattr(self, "_timer") and self._timer.isActive():
            self._timer.stop()

    def hideEvent(self, event) -> None:
        # Pausa el QTimer de 16ms mientras el widget esté oculto - antes
        # seguía disparando _tick()/update() 60 veces por segundo aunque
        # nada se estuviera pintando en pantalla (p. ej. ventana
        # minimizada a la bandeja con un turno aún en curso).
        if hasattr(self, "_timer") and self._timer.isActive():
            self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        # Reanuda solo si start() fue llamado y stop() no lo canceló
        # después - así una construcción/showEvent inicial (antes de
        # que el llamador decida arrancar el spinner) no lo hace girar
        # de más.
        if self._should_spin and not self._timer.isActive():
            self._timer.start()
        super().showEvent(event)

    def paintEvent(self, event) -> None:  # noqa: D401 - override de Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pen_width = max(2, self.width() // 7)
        rect = self.rect().adjusted(pen_width, pen_width, -pen_width, -pen_width)

        # Pista tenue de fondo, siempre visible, para que el arco activo
        # tenga algo sobre lo cual "girar" - igual que un spinner nativo.
        track_pen = QPen(QColor(self._color.red(), self._color.green(), self._color.blue(), 35))
        track_pen.setWidth(pen_width)
        track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.drawEllipse(rect)

        # Arco activo: un cuarto de vuelta aprox., rotando.
        arc_pen = QPen(self._color)
        arc_pen.setWidth(pen_width)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(arc_pen)
        span = 100 * 16  # QPainter mide los arcos en 1/16 de grado
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

        # Nota (mejora de UX, pedida explícitamente): el modo de dos
        # pasadas puede tardar 40-90+ segundos sin cambiar de mensaje
        # dentro de una misma fase (p. ej. toda la Pasada 2 redactando
        # la respuesta) - sin ninguna señal de progreso, un turno lento
        # es indistinguible de uno colgado. Un contador de segundos
        # transcurridos, actualizado cada segundo, resuelve esto sin
        # necesitar progreso real (tokens generados) que esta capa de
        # UI no tiene disponible en vivo - ver StreamTurnWorker, que
        # nunca transmite tokens de las pasadas ocultas.
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

    # Valores por defecto (tarjeta hero); las sub-tarjetas pasan los suyos
    # explícitamente al construir el loader - ver _OverlayImageCard.
    DEFAULT_MIN_WIDTH = 300
    DEFAULT_MIN_HEIGHT = 150
    # Techo defensivo: evita decodificar imágenes descomunales (posible
    # "bomba de descompresión" si la URL de miniatura es maliciosa o
    # simplemente un archivo fuera de lugar) - no es parte del requisito
    # explícito, pero es la contraparte natural de "optimizar recursos".
    MAX_DIMENSION = 6000

    # Caché en memoria de miniaturas ya descargadas/validadas - antes
    # cada tarjeta de resultado creaba su propio loader y volvía a
    # descargar la misma url (p. ej. el favicon de respaldo se repite
    # entre varias fuentes del mismo dominio, o una re-renderización de
    # la tarjeta al cambiar de idioma/tema dispara un nuevo loader para
    # una url ya procesada). Clave = (url, min_width, min_height) porque
    # la misma imagen puede validar distinto según el umbral de calidad
    # del slot (hero vs sub-tarjeta - ver docstring de la clase). También
    # se cachea el resultado "inválido" (bytes vacíos) para no
    # re-intentar una url ya sabida como corrupta o demasiado pequeña.
    # Compartida entre hilos (cada instancia es su propio QThread), de
    # ahí el lock.
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
            # Ya descargada y validada (o ya sabida como inválida) en un
            # loader anterior con el mismo url y el mismo umbral de
            # calidad - se emite de inmediato sin tocar la red.
            self.loaded.emit(cached)
            return

        data = b""
        if self._url:
            try:
                req = urllib.request.Request(
                    self._url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SovNode/2.0"},
                )
                # Cota dura de 4s: esta descarga corre en su propio QThread
                # (no bloquea StreamTurnWorker ni la UI), pero igual debe
                # tener un timeout explícito y corto - sin él, un socket
                # colgado deja el hilo vivo indefinidamente en segundo
                # plano en vez de fallar y liberar el recurso.
                with urllib.request.urlopen(req, timeout=4) as resp:
                    data = resp.read()
            except Exception:
                data = b""

        validated = self._validate_dimensions(data)

        with self._CACHE_LOCK:
            if len(self._CACHE) >= self._CACHE_MAX_ENTRIES:
                # Nota (micro-tirones en la UI): un vaciado completo
                # justo al llenarse provocaba que todas las miniaturas ya
                # vistas volvieran a pedirse de red a la vez en la
                # siguiente re-renderización (cambio de tema/idioma,
                # scroll que reconstruye tarjetas) - una ráfaga simultánea
                # de descargas visible como tirones. Desalojo FIFO del
                # ~20% más antiguo en su lugar: dict conserva orden de
                # inserción desde Python 3.7, así que los primeros N ítems
                # son los más antiguos sin necesitar OrderedDict/deque
                # aparte para un caso de uso tan simple (esta caché no
                # hace "hit" de re-inserción - una clave ya cacheada
                # retorna antes en run(), nunca reordena su posición - así
                # que FIFO por inserción es una aproximación razonable a
                # LRU sin la complejidad de rastrear accesos).
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
                return b""  # formato no reconocido o payload corrupto

            size = reader.size()
            if not size.isValid() or size.width() <= 0 or size.height() <= 0:
                return b""  # cabecera corrupta/ilegible

            if size.width() < self._min_width or size.height() < self._min_height:
                return b""  # regla de calidad: demasiado pequeña para este slot, no se estira

            if size.width() > self.MAX_DIMENSION or size.height() > self.MAX_DIMENSION:
                return b""  # defensa contra imágenes descomunales

            return data
        except Exception:
            # Cualquier fallo de la capa de imagen se trata como
            # "miniatura no válida", nunca como un crash del hilo.
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
        # Mismo motivo que en el diseño anterior: sin Ignored, el
        # minimumSizeHint del título largo se propaga hacia arriba y
        # ensancha toda la columna del chat más allá del viewport.
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
            # El mínimo de calidad se ajusta al slot real de esta tarjeta:
            # el hero es alto y angosto-a-ancho (~260x280); las sub-tarjetas
            # son más chicas (~160x130) - exigirles el mismo mínimo que al
            # hero rechazaba casi cualquier miniatura razonable para ellas.
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
        # `data` ya llega pre-validada desde _ThumbnailLoader (dimensiones
        # mínimas + formato legible verificados fuera del hilo GUI); vacío
        # significa "descarga fallida" o "rechazada por calidad" - se
        # conserva el fondo plano de respaldo, nunca se estira nada.
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

    def resizeEvent(self, event) -> None:  # noqa: N802 (override de Qt)
        super().resizeEvent(event)
        self._apply_image()

    def showEvent(self, event) -> None:  # noqa: N802 (override de Qt)
        super().showEvent(event)
        self._apply_image()

    def paintEvent(self, event) -> None:  # noqa: N802 (override de Qt)
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

        # Sin marco/globo alrededor: solo las imágenes (con su propio
        # texto superpuesto en _OverlayImageCard) quedan visibles en el
        # chat, sin fondo/borde de tarjeta ni encabezado con el título
        # "Investigación en vivo realizada" + la consulta entre comillas.
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 4, 0, 4)
        main_layout.setSpacing(8)

        # Fila principal: hero a la izquierda, columna de sub-tarjetas a la derecha
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
            # Si solo hay 1 fuente secundaria, se estira para llenar la
            # columna en vez de dejar un hueco vacío del alto de una
            # segunda tarjeta que no existe.
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

            for voice in voices:
                v_id = voice.id.lower()
                v_name = voice.name.lower()
                if target_is_es and any(k in v_name or k in v_id for k in ["spanish", "es", "helena", "sabina", "raul", "pablo"]):
                    self._engine.setProperty('voice', voice.id)
                    break
                elif not target_is_es and any(k in v_name or k in v_id for k in ["english", "en", "zira", "david"]):
                    self._engine.setProperty('voice', voice.id)
                    break

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
        super().keyPressEvent(event)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            valid = any(
                url.isLocalFile()
                and Path(url.toLocalFile()).suffix.lower()
                in SUPPORTED_DROP_EXTENSIONS
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
            if file_path.suffix.lower() not in SUPPORTED_DROP_EXTENSIONS:
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            tagged_content = (
                f"\n\n[ARCHIVO ADJUNTO: {file_path.name}]\n"
                f"```{file_path.suffix.lstrip('.')}\n"
                f"{content}\n"
                f"```\n"
            )
            cursor = self.textCursor()
            cursor.insertText(tagged_content)
            self.setTextCursor(cursor)
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
        
        # Bloqueo estricto de scroll horizontal para evitar desbordamientos visuales
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
            from faster_whisper import WhisperModel
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

            model = WhisperModel("tiny", device="cpu", compute_type="int8")
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

class _CancelToken:
    def __init__(self, check_fn):
        self._check_fn = check_fn


    def is_set(self) -> bool:
        return self._check_fn()

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
    ) -> None:
        super().__init__()
        self._orchestrator = orchestrator
        self._prompt = prompt
        self._force_web_search = force_web_search
        self._is_cancelled = False

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
                    self.log_message.emit(
                        "[CACHE] Respuesta servida desde caché semántico."
                    )

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
                        error = "Generación cancelada."
                    self.completed.emit(trace, error)
                    return

            # El generador se agotó sin emitir DONE - no debería pasar,
            # pero se cubre para que la UI nunca quede colgada esperando.
            self.completed.emit(None, "El pipeline terminó sin emitir DONE.")

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


class ReflectionWorker(QThread):
    """
    Hilo mínimo para el chequeo periódico de "mensaje espontáneo" (ver
    Orchestrator.generate_spontaneous_reflection). Mismo espíritu que
    HealthCheckWorker (una sola llamada, sin streaming) — no reutiliza
    StreamTurnWorker porque esa clase está armada para el pipeline
    completo de un turno con streaming/eventos de progreso, que esto no
    necesita: es una sola llamada bloqueante a _call_llm.

    HONESTIDAD: esta clase NO decide "si el sistema quiere hablar" — solo
    transporta a un hilo de Qt una llamada que ya vive en Orchestrator y
    traduce su resultado (texto real, o None) a una señal. La decisión
    real, si así se le puede llamar, es la salida de texto determinística
    del modelo sobre un prompt — ver el HONESTIDAD junto a
    generate_spontaneous_reflection en orchestrator.py.
    """

    reflection_ready = pyqtSignal(str)  # texto ya limpio y no vacío
    finished_no_reflection = pyqtSignal()  # corrió bien, pero no había nada que decir
    error_occurred = pyqtSignal(str)

    def __init__(self, orchestrator: Orchestrator) -> None:
        super().__init__()
        self._orchestrator = orchestrator

    def run(self) -> None:
        try:
            resultado = self._orchestrator.generate_spontaneous_reflection()
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            return
        if resultado:
            self.reflection_ready.emit(resultado)
        else:
            self.finished_no_reflection.emit()


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
        self, model_tag: str, ollama_base_url: str = "http://localhost:11434"
    ) -> None:
        super().__init__()
        self.model_tag = (model_tag or "").strip()
        self._base_url = ollama_base_url.rstrip("/")
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        if not self.model_tag:
            self.completed.emit(False, "El tag del modelo no puede estar vacío.")
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
                    self.completed.emit(False, "Descarga cancelada por el usuario.")
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
                True, f"Modelo '{self.model_tag}' descargado con éxito."
            )

        except Exception as exc:
            self.completed.emit(False, str(exc))


class ModelDownloadDialog(QDialog):
    """
    Diálogo para ingresar o seleccionar el tag de un modelo de Ollama y
    descargarlo en caliente vía ModelPullApiWorker (POST /api/pull),
    mostrando el progreso (%) en tiempo real.
    """

    # mistral-nemo-12b-uncensored retirado a pedido del usuario: ya no
    # se ofrece como sugerencia de descarga.
    COMMON_TAGS = (
        "phi3.5:3.8b",  # modelo general por defecto (antes qwen2.5:3b).
        "qwen2.5:3b",
        "qwen2.5:7b",
        "qwen2.5-coder:3b",
        "qwen2.5-coder:7b",
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
        # Limpia el resultado de una descarga previa en este mismo diálogo
        # (éxito o error) antes de arrancar una nueva - si no, un reintento
        # fallido tras un tag descargado con éxito seguiría reportando ese
        # tag anterior como "downloaded_tag" al cerrar el diálogo.
        self.downloaded_tag = None
        self.progress_bar.setValue(0)
        self.status_label.setText(tag)

        self._worker = ModelPullApiWorker(tag, self._base_url)
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

        # Genialidades sistémicas v2.0 que participaron en este turno.
        genius_badges = []
        if getattr(trace, "thought_code_verified", False):
            genius_badges.append("🧪 Verificado en sandbox")
        if getattr(trace, "tot_used", False):
            tot_agreement = getattr(trace, "tot_agreement", None)
            agree_txt = f" ({tot_agreement:.0%} acuerdo)" if tot_agreement is not None else ""
            genius_badges.append(f"🌳 Tree-of-Thoughts{agree_txt}")
        if getattr(trace, "epistemic_drift_detected", False):
            genius_badges.append("⚠️ Deriva epistémica corregida")

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
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        language_label = QLabel(self._language.upper())
        language_label.setStyleSheet(
            "font-size: 10px; font-weight: bold; color: #FFFFFF;"
        )

        copy_button = QPushButton(tr["btn_copy_code"])
        copy_button.setObjectName("codeActionButton")
        copy_button.clicked.connect(self.copy_code)

        save_button = QPushButton(tr["btn_save_code"])
        save_button.setObjectName("codeActionButton")
        save_button.clicked.connect(self.save_code)

        toolbar.addWidget(language_label)
        toolbar.addStretch()
        toolbar.addWidget(copy_button)
        toolbar.addWidget(save_button)

        self.code_view = QTextEdit()
        self.code_view.setReadOnly(True)
        self.code_view.setPlainText(code)
        self.code_view.setMinimumHeight(72)
        self.code_view.setMaximumHeight(280)
        self.code_view.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; "
            "font-size: 12px; border-radius: 8px; padding: 7px; "
            "background-color: #0B0E14; color: #FFFFFF;"
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
                "Error al guardar",
                f"No se pudo escribir el archivo:\n{exc}",
            )


class MessageBubble(QWidget):
    CODE_PATTERN = re.compile(
        r"```(?P<language>[A-Za-z0-9_+\-]*)\n?(?P<code>.*?)```",
        re.DOTALL,
    )
    # Throttle de update_content(): durante el streaming, _flush_stream_
    # buffer() (MainWindow) ya llama a update_content() a lo sumo cada
    # 50ms (_render_timer), pero cada llamada reconstruye el markdown
    # completo y fuerza un re-layout de todo el documento
    # (setMarkdown + document().setTextWidth() + .size().height() en
    # AutoResizingTextBrowser.update_height()) - trabajo que crece con el
    # largo de la respuesta y se repite ~20 veces/segundo durante toda la
    # generación. Este segundo throttle, propio del widget (no depende
    # de quién lo llame), espacia el re-render costoso a lo sumo cada
    # MIN_RENDER_INTERVAL_MS: si llega una llamada antes de ese margen,
    # se pospone (trailing edge) en vez de perderse, así el contenido
    # final siempre se renderiza - solo se difiere el momento.
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
        # Sin caja que delimite las respuestas de SovNode, hace falta más
        # aire vertical entre un mensaje y el siguiente para que la
        # conversación siga siendo legible como texto continuo (estilo
        # Gemini) en vez de un bloque ambiguo. El mensaje del usuario, en
        # cambio, sí es una caja (globo) - ver más abajo.
        outer_layout.setContentsMargins(0, 10, 0, 10)
        outer_layout.setSpacing(0)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(16, 10, 16, 10) if self._is_user else card_layout.setContentsMargins(16, 4, 16, 4)
        card_layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        # El rótulo de rol ("🧑 TÚ") solo tiene sentido en el globo del
        # usuario, que además vive alineado a la derecha - ya se
        # distingue de un vistazo de una respuesta de SovNode sin
        # necesidad de repetir un nombre. Las respuestas normales de
        # SovNode van SIN etiqueta de remitente (antes decían "🛡️
        # SOVNODE" arriba de cada mensaje, ocupando espacio sin aportar
        # nada que el propio layout -a la izquierda, sin globo- ya no
        # comunique). Error y advertencia sí conservan su etiqueta: no es
        # branding, es una señal funcional de que algo salió distinto de
        # lo normal.
        sender_label: Optional[QLabel] = None
        if self._is_user:
            sender_label = QLabel(tr["msg_sender_user"])
        elif is_error:
            sender_label = QLabel(tr["msg_sender_error"])
        elif self._is_warning:
            sender_label = QLabel(tr["msg_sender_warning"])

        if sender_label is not None:
            sender_label.setObjectName("messageSender")
            header_row.addWidget(sender_label)
            header_row.addStretch(1)

        # El header solo se agrega si tiene contenido real (etiqueta de
        # remitente) - para una respuesta normal de SovNode, sin rótulo,
        # header_row queda vacío y no se añade al layout. update_content()
        # usa self._content_start_index (en vez de un índice fijo) para
        # saber a partir de qué posición de card_layout vive el contenido,
        # ya que ese índice cambia según si hay header o no.
        if sender_label is not None:
            card_layout.addLayout(header_row)
        self._content_start_index = 1 if sender_label is not None else 0

        if self._is_user or is_error:
            text_view = AutoResizingTextBrowser()
            text_view.setPlainText(content)
            card_layout.addWidget(text_view)
            text_view.update_height()
        else:
            self._add_assistant_content(card_layout, content)

        if trace is not None and not self._is_user:
            card_layout.addWidget(TraceWidget(trace, lang=self._lang))

        # Footer: hora del mensaje + menú de opciones (⋮), reubicados
        # debajo del texto de respuesta en vez de arriba - la hora y las
        # acciones son metadatos de baja prioridad visual, no algo que
        # deba competir con el contenido por la primera línea de
        # atención del usuario.
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
            # Globo alineado a la derecha: el stretch a la izquierda
            # empuja la tarjeta (de ancho preferido, no expandido - ver
            # setSizePolicy arriba) hacia el borde derecho del chat.
            outer_layout.addStretch(1)
            outer_layout.addWidget(self._card, 0)
        else:
            # Sin sombra: una caja flotante con sombra es exactamente el
            # efecto "globo" que el rediseño Módulo 2 elimina para SovNode
            # - el texto vive directamente sobre el fondo del chat.
            outer_layout.addWidget(self._card, 1)

    def _show_options_menu(self) -> None:
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

        tts_action = QAction("🔊 Escuchar mensaje", self)
        tts_action.triggered.connect(
            lambda: self.tts_requested.emit(self._content)
        )
        menu.addAction(tts_action)

        stop_tts_action = QAction("⏹️ Detener lectura", self)
        stop_tts_action.triggered.connect(
            lambda: self.tts_stop_requested.emit()
        )
        menu.addAction(stop_tts_action)

        menu.addSeparator()

        copy_action = QAction("📋 Copiar texto", self)
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

            layout.addWidget(
                CodeBlockWidget(
                    code=match.group("code").strip("\n"),
                    language=match.group("language"),
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

        # El footer (hora + menú ⋮) siempre vive al final del layout -
        # este bucle nunca debe removerlo, solo el contenido reemplazable
        # entre el header opcional y el TraceWidget/footer.
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

                    code_widget = CodeBlockWidget(
                        code=match.group("code").strip("\n"),
                        language=match.group("language"),
                        lang=self._lang,
                    )
                    card_layout.insertWidget(insert_idx, code_widget)
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
        if self._is_user:
            # Globo del usuario: ancho máximo del 75% del viewport visible,
            # no todo el ancho disponible - así conserva la sensación de
            # "burbuja" en vez de estirarse de borde a borde.
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

        title = QLabel(tr["donate_header"])
        title.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #FFFFFF;"
        )
        layout.addWidget(title)

        desc = QLabel(tr["donate_desc"])
        desc.setWordWrap(True)
        desc.setStyleSheet(
            "font-size: 13px; color: #E6E8EC; line-height: 1.4;"
        )
        layout.addWidget(desc)

        btn_kofi = QPushButton(tr["donate_btn_kofi"])
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
        self._current_lang = "Español"
        self._turn_count = 0
        self._is_online = False
        # Estado previo del último health-check de Ollama (ver
        # _on_health_check_completed) - None hasta el primer resultado,
        # para que ese primer resultado siempre se anuncie en la consola
        # visible sin importar cuál sea.
        self._last_ollama_status: Optional[bool] = None
        self._is_quitting = False
        self._is_recording_voice = False
        self._force_web_search = False
        self._voice_worker: Optional[VoiceRecorderWorker] = None
        self._tts_worker: Optional[TTSWorker] = None
        self._thinking_widget: Optional[ThinkingWidget] = None
        self._terminal_visible = False
        # Módulo 3: procedencia del último turno (None hasta el primero),
        # para poder re-renderizar el badge del header en el idioma nuevo
        # cuando el usuario cambia de idioma sin esperar a un turno nuevo.
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

        # Nota (pedido explícito, "recortar segundos reales de
        # generación" - medido: 7.36s de `load` dentro del QueryRewrite
        # del primer turno de una sesión recién abierta): el runner de
        # Ollama para `general_model` no existe hasta la primera
        # inferencia real - sin esto, esa carga completa quedaba dentro
        # del camino crítico del PRIMER mensaje que escribe el usuario.
        # Se dispara UNA vez acá, en un hilo de fondo daemon, apenas el
        # proceso de Ollama está confirmado arriba - para cuando el
        # usuario termine de leer la ventana recién abierta y escriba su
        # primer mensaje, el modelo ya puede estar caliente en memoria.
        # Un hilo de Python normal (no QThread): `warm_up_general_model`
        # es una llamada de red bloqueante común y corriente, sin tocar
        # ningún widget de Qt ni emitir señales - no hace falta la
        # maquinaria de QThread/pyqtSignal solo para esto. daemon=True
        # para que nunca retrase el cierre de la app si por lo que sea
        # sigue esperando una respuesta de Ollama.
        threading.Thread(
            target=self.orchestrator.warm_up_general_model,
            daemon=True,
            name="ModelWarmUp",
        ).start()

        # Mismo espíritu que el precalentado de arriba, pero para el
        # modelo LOCAL de embeddings (ver embeddings.py): sin esto, el
        # chequeo de caché semántico del primer turno real (que corre
        # antes que nada más, incluso antes del router) paga la carga
        # completa del modelo (unos segundos la primera vez en esta
        # máquina) en el camino crítico del primer mensaje del usuario.
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

        # Mensajes espontáneos ("que pueda escribir cuando quiera o solo
        # cuando se le hable", pedido explícito): apagado por defecto -
        # mismo criterio que _force_web_search, el usuario prende la
        # opción a propósito. El timer corre siempre (barato: solo
        # dispara cada REFLECTION_INTERVAL_MS), pero _maybe_run_reflection
        # revisa el toggle antes de hacer nada. Ver HONESTIDAD junto a
        # Orchestrator.generate_spontaneous_reflection para el porqué
        # esto no es "el sistema decidiendo hablar" en sentido experiencial.
        self._proactive_mode_enabled = False
        self._reflection_worker: Optional[ReflectionWorker] = None
        self.REFLECTION_INTERVAL_MS = 20 * 60 * 1000  # 20 min - conservador, corre un LLM local
        self.reflection_timer = QTimer(self)
        self.reflection_timer.timeout.connect(self._maybe_run_reflection)
        self.reflection_timer.start(self.REFLECTION_INTERVAL_MS)

        # Nota de optimización de recursos (pedido explícito: "que
        # consuma menos... potencia de la PC"): sin esto, un timer de 20
        # min corriendo indefinidamente en una sesión inactiva dispara
        # una llamada real al LLM cada 20 minutos sin límite - 3 por
        # hora, ~24 en una noche con la PC prendida. `True` al arrancar
        # (una sesión recién abierta ya cuenta como "hay actividad
        # nueva" - se le da al menos una oportunidad). Se vuelve a poner
        # en `True` únicamente cuando el usuario manda un mensaje real
        # (_send_message) y en `False` apenas se lanza una reflexión
        # (_maybe_run_reflection) - así, sea cual sea el largo de la
        # inactividad, como mucho se paga UNA llamada al modelo por
        # período de inactividad, no una cada 20 minutos.
        self._had_activity_since_last_reflection = True

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1024, 700)

        self._create_ui()
        self._apply_theme(self._theme_name)
        self._create_tray_icon()

        self._run_health_check()
        self._show_first_time_setup_dialog()
        self._add_bubble("assistant", I18N[self._current_lang]["welcome_msg"])

        self._terminal_log(
            I18N[self._current_lang]["log_init_ok"], "ok"
        )

        # Nota (medido - turno "dame las ecuaciones de
        # Einstein"): cuando matplotlib no está instalado en el
        # intérprete que corre SovNode, math_render.extract_equations_
        # as_placeholders() se degrada en silencio (por diseño - nunca
        # debe romper el render del resto del mensaje) y las ecuaciones
        # vuelven a mostrarse como texto LaTeX crudo, IDÉNTICO a como se
        # veían antes de este módulo. Esa degradación usaba
        # logger.warning() (logging estándar de Python) - invisible acá,
        # porque esta consola gráfica solo muestra lo que pasa por
        # _terminal_log(), no el logging de Python. Sin este aviso, la
        # falta de matplotlib es indistinguible de "el render no
        # funciona" a simple vista.
        #
        # Se loguea siempre (no solo cuando falta) con un marcador de
        # versión fijo (EQRENDER_BUILD_TAG): así, al reiniciar, un
        # vistazo a esta consola alcanza para saber si el proceso que
        # está corriendo de verdad tiene este módulo cargado - sin esa
        # línea (build viejo/proceso no reiniciado/otro intérprete) se
        # distingue al toque de "cargado pero sin matplotlib" (línea de
        # warning) o "cargado y andando" (línea ok). medido: un caso real
        # donde las ecuaciones seguían crudas y esta línea NO apareció en
        # absoluto en consola confirmó que el proceso corriendo no era el
        # que tenía el fix - no un bug de renderizado.
        EQRENDER_BUILD_TAG = "eqrender-2026-08-25.2"
        if not math_render.MATPLOTLIB_AVAILABLE:
            self._terminal_log(
                f"⚠️ [{EQRENDER_BUILD_TAG}] matplotlib no está instalado — las "
                "ecuaciones se mostrarán como texto LaTeX crudo en vez de "
                "renderizarse. Instalá con: pip install matplotlib (en el mismo "
                "entorno de Python con el que corrés SovNode) y reiniciá la app.",
                "warn",
            )
        else:
            self._terminal_log(
                f"✅ [{EQRENDER_BUILD_TAG}] Renderizador de ecuaciones LaTeX activo "
                "(matplotlib detectado — las ecuaciones $...$/[...] se van a "
                "mostrar como imagen, no como texto crudo).",
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
        if not self._chat_entries:
            QMessageBox.information(
                self,
                "Sin mensajes",
                "No hay mensajes para exportar todavía.",
            )
            return

        default_name = (
            f"sovnode_chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        )
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar conversación",
            default_name,
            "Markdown (*.md);;Texto (*.txt);;Todos los archivos (*.*)",
        )

        if not filename:
            return

        lines = [
            "# Transcripción de sesión — SovNode",
            "",
            f"Fecha de exportación: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Modelo: {self.orchestrator.model}",
            "",
        ]

        for entry in self._chat_entries:
            role = "Usuario" if entry.sender == "user" else "SovNode"
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
                "Chat exportado",
                f"La conversación se guardó correctamente en:\n{filename}",
            )
        except OSError as exc:
            self._terminal_log(
                I18N[self._current_lang]["log_export_error"].format(exc), "error"
            )
            QMessageBox.critical(
                self,
                "Error de exportación",
                f"No se pudo exportar el chat:\n{exc}",
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
        wal_path = str(getattr(self.wal, "_log_path", "sovnode.wal"))

        default_dir = str(Path(wal_path).resolve().parent / "training_data")
        out_dir = QFileDialog.getExistingDirectory(
            self,
            "Elegir carpeta de salida para el dataset de entrenamiento",
            default_dir,
        )
        if not out_dir:
            return

        try:
            from training_export import export_wal_to_training_data
            stats = export_wal_to_training_data(wal_path, out_dir)
        except Exception as exc:
            self._terminal_log(f"Fallo al exportar dataset de entrenamiento: {exc}", "error")
            QMessageBox.critical(
                self,
                "Error de exportación",
                f"No se pudo exportar el dataset de entrenamiento:\n{exc}",
            )
            return

        if stats["pairs"] == 0:
            QMessageBox.information(
                self,
                "Sin correcciones todavía",
                "Todavía no hay correcciones registradas en el WAL para exportar. "
                "Esto es normal en una instalación nueva — vuelve a intentarlo tras "
                "usar SovNode un tiempo más, cuando el pipeline haya corregido algún "
                "turno (marcador sin respaldo, idioma equivocado, etc.).",
            )
            return

        self._terminal_log(
            f"Dataset de entrenamiento exportado: {stats['pairs']} par(es) -> {out_dir}", "ok"
        )
        breakdown = "\n".join(
            f"  · {pair_type}: {count}"
            for pair_type, count in sorted(stats["by_type"].items(), key=lambda kv: -kv[1])
        )
        QMessageBox.information(
            self,
            "Dataset exportado",
            f"{stats['pairs']} par(es) de entrenamiento exportados a:\n{out_dir}\n\n"
            f"{breakdown}\n\n"
            "Archivos: dpo_pairs.jsonl (prompt/chosen/rejected) y "
            "sft_pairs.jsonl (prompt/response).",
        )

    def _hide_to_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.showMinimized()
            return

        self.hide()
        self.tray_icon.showMessage(
            "SovNode sigue activo",
            "La aplicación continúa disponible en la bandeja del sistema.",
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

    def _quit_application(self) -> None:
        self._is_quitting = True

        if self._turn_worker is not None and self._turn_worker.isRunning():
            self._turn_worker.wait(1500)

        if self._health_worker is not None and self._health_worker.isRunning():
            self._health_worker.wait(500)

        self._stop_tts()

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
        """
        self._terminal_log(message, "system")

    def _on_intent_changed(self, icon: str, msg: str) -> None:
        if self._thinking_widget is None:
            self._thinking_widget = ThinkingWidget(self)
            item = self.chat_layout.takeAt(self.chat_layout.count() - 1)
            self.chat_layout.addWidget(self._thinking_widget)
            if item:
                self.chat_layout.addItem(item)
        self._thinking_widget.set_intent(icon, msg)
        self._scroll_to_bottom()

    def _on_web_results_ready(self, data: dict) -> None:
        tr = I18N[self._current_lang]
        if not data.get("success"):
            self._terminal_log(
                tr["log_web_search_degraded"].format(
                    data.get("status_message", tr["no_detail_fallback"])
                ),
                "warn",
            )

        # Módulo 4 - Invocación Semántica de Imágenes: la búsqueda de texto
        # ya se ejecutó y alimenta al LLM sin importar el tema; aquí solo
        # se decide si la tarjeta VISUAL aporta algo (deportes, catástrofes/
        # noticias de impacto, biografías) o sería ruido (física, código,
        # filosofía, temas abstractos en general).
        if not should_show_visual_search_cards(data.get("query", "")):
            self._terminal_log(tr["log_visual_card_skipped"], "info")
            return

        # Nota (Point C del diagnóstico "Rodri"): should_show_visual_
        # search_cards() solo filtra por TEMA (deportes/noticias/bio),
        # nunca por CALIDAD de las miniaturas - una consulta deportiva
        # cuyas únicas fuentes son el respaldo de Wikipedia (sin foto
        # propia, ver _fetch_rich_web_search_impl) igual pasaba ese
        # filtro y terminaba renderizando tarjetas hero/sub vacías con
        # solo las iniciales de respaldo ("ES" de es.wikipedia.org). Si
        # NINGUNA fuente tiene una miniatura real (no vacía, no Wiki, no
        # el favicon diminuto de respaldo), se omite la tarjeta entera -
        # el texto de la respuesta del modelo ya cubre el contenido.
        sources = data.get("sources") or []
        has_real_image = any(
            not _is_low_quality_thumbnail(src.get("thumbnail"))
            for src in sources
            if isinstance(src, dict)
        )
        if not has_real_image:
            self._terminal_log(tr["log_visual_card_no_images"], "info")
            return

        web_widget = WebSearchResultsWidget(
            data, self.chat_widget, lang=self._current_lang
        )

        # Anclar el ancho antes de insertarlo en el layout: si se deja para
        # el próximo resize, el primer frame ya se dibuja más ancho que el
        # viewport (efecto "salto hacia la derecha" que reportó el usuario).
        if hasattr(self, "chat_scroll") and self.chat_scroll:
            web_widget.set_available_width(self.chat_scroll.viewport().width() - 30)
        self._web_card_widgets.append(web_widget)

        # Insertar la tarjeta justo arriba de la respuesta que se está generando
        if self._current_stream_bubble:
            idx = self.chat_layout.indexOf(self._current_stream_bubble)
            if idx != -1:
                self.chat_layout.insertWidget(idx, web_widget)
                self._scroll_to_bottom()
                return

        # Inserción de respaldo al final
        item = self.chat_layout.takeAt(self.chat_layout.count() - 1)
        self.chat_layout.addWidget(web_widget)
        if item:
            self.chat_layout.addItem(item)
        self._scroll_to_bottom()

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

            status_label = "Online" if is_online else "Offline"
            status_changed = is_online != self._last_ollama_status
            if status_changed:
                # Solo los CAMBIOS de estado (offline<->online, o el
                # primer resultado tras arrancar) se promueven a la
                # consola visible - el health_timer sondea cada 8s, así
                # que anunciar cada comprobación aunque no cambie nada
                # inundaba la consola con "Online" repetido sin aportar
                # información nueva.
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

        # PANEL LATERAL
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(280)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(16, 20, 16, 20)
        sidebar_layout.setSpacing(14)

        app_title = QLabel(APP_NAME)
        app_title.setObjectName("appTitle")
        app_subtitle = QLabel("Sovereign AI Node v2.0")
        app_subtitle.setObjectName("appSubtitle")

        sidebar_layout.addWidget(app_title)
        sidebar_layout.addWidget(app_subtitle)

        # Tema Visual
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
        sidebar_layout.addWidget(theme_card)

        # Idioma
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
        sidebar_layout.addWidget(lang_card)

        # Estado del Nodo
        status_card = QFrame()
        status_card.setObjectName("sidebarCard")
        sc_layout = QVBoxLayout(status_card)
        sc_layout.setContentsMargins(10, 10, 10, 10)
        self.status_title_label = QLabel(tr["status_card"])
        self.status_title_label.setObjectName("sectionTitle")
        self.status_label = QLabel(tr["status_checking"])
        self.status_label.setWordWrap(True)
        sc_layout.addWidget(self.status_title_label)
        sc_layout.addWidget(self.status_label)
        sidebar_layout.addWidget(status_card)

        # Modelo Activo
        #
        # El selector 3B/7B (combo_model_size / _on_model_size_changed /
        # Orchestrator.set_model_size) se sacó junto con el esquema de
        # variantes 3B/7B: la arquitectura pasó a UN SOLO modelo fijo
        # (gpt-oss:20b — ver Orchestrator.RESPONSE_MODEL). Se conserva SOLO
        # el botón de descarga de modelos, que sigue teniendo función real.
        # (El texto "ACTIVE MODEL (last turn)" ya se había sacado antes,
        # screenshot 2026-08-27 — self.model_title_label / self.model_label
        # no existen.)
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

        sidebar_layout.addWidget(model_card)

        sidebar_layout.addStretch()

        self.btn_new_chat = QPushButton(tr["btn_new_chat"])
        self.btn_new_chat.setObjectName("actionButton")
        self.btn_new_chat.clicked.connect(self._clear_chat)

        self.btn_export = QPushButton(tr["btn_export"])
        self.btn_export.setObjectName("secondaryButton")
        self.btn_export.clicked.connect(self._export_chat)

        self.btn_export_training = QPushButton(tr["btn_export_training"])
        self.btn_export_training.setObjectName("secondaryButton")
        self.btn_export_training.setToolTip(
            "Exporta un dataset DPO/SFT a partir de las correcciones reales "
            "que el pipeline ya detectó en el WAL — ver training_export.py."
        )
        self.btn_export_training.clicked.connect(self._export_training_data)

        self.btn_donate = QPushButton(tr["btn_donate"])
        self.btn_donate.setObjectName("actionButton")
        self.btn_donate.clicked.connect(
            lambda: DonationDialog(self._current_lang, self).exec()
        )

        sidebar_layout.addWidget(self.btn_new_chat)
        sidebar_layout.addWidget(self.btn_export)
        sidebar_layout.addWidget(self.btn_export_training)
        sidebar_layout.addWidget(self.btn_donate)

        main_layout.addWidget(sidebar)

        # PANEL PRINCIPAL
        content_container = QWidget()
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # Header Bar limpia
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

        # Módulo 3: insignia de procedencia de datos (RED VIVA / MEMORIA
        # LOCAL), reubicada aquí desde el globo de cada mensaje - vive en
        # el header, se actualiza una vez por turno en _on_turn_completed().
        self.header_status = QLabel("")
        self.header_status.setObjectName("headerStatusBadge")
        self.header_status.setVisible(False)

        hb_layout.addLayout(hb_info)
        hb_layout.addStretch()
        hb_layout.addWidget(self.header_status)
        hb_layout.addWidget(self.btn_toggle_terminal)

        content_layout.addWidget(header_bar)

        # Chat Scroll Area con seguimiento inteligente de posición (fix del bug de scroll)
        self.chat_scroll = SmartChatScrollArea(self)
        self.chat_scroll.setObjectName("chatScrollArea")
        self.chat_scroll.pinned_state_changed.connect(self._on_pinned_state_changed)

        self.chat_widget = QWidget()
        self.chat_widget.setObjectName("chatWidget")
        self.chat_layout = QVBoxLayout(self.chat_widget)
        self.chat_layout.setContentsMargins(24, 20, 24, 90)
        self.chat_layout.setSpacing(14)
        self.chat_layout.addStretch()
        self.chat_scroll.setWidget(self.chat_widget)
        self.chat_scroll.setWidgetResizable(True)

        content_layout.addWidget(self.chat_scroll, 1)

        # Botón flotante "ir al final" - aparece solo cuando el usuario se aleja del fondo
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

        # Barra de progreso e indicador
        processing_container = QWidget()
        pc_layout = QVBoxLayout(processing_container)
        pc_layout.setContentsMargins(24, 0, 24, 4)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)

        self.processing_label = QLabel("")
        self.processing_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.processing_label.setStyleSheet("font-size: 11px; color: #8B92A5;")

        pc_layout.addWidget(self.progress_bar)
        pc_layout.addWidget(self.processing_label)
        content_layout.addWidget(processing_container)

        # Entrada de Texto
        input_container = QWidget()
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(24, 8, 24, 20)
        input_layout.setSpacing(10)

        self.input_field = PromptTextEdit(self)
        self.input_field.setObjectName("inputField")
        self.input_field.setPlaceholderText(tr["placeholder"])
        self.input_field.send_requested.connect(self._send_message)

        self.mic_button = QPushButton("🎙️")
        self.mic_button.setFixedSize(48, 48)
        self.mic_button.setObjectName("secondaryButton")
        self.mic_button.setToolTip("Dictado de voz a texto (STT local)")
        self.mic_button.clicked.connect(self._toggle_voice_recording)

        self.web_search_toggle_btn = QPushButton("🌐")
        self.web_search_toggle_btn.setFixedSize(48, 48)
        self.web_search_toggle_btn.setObjectName("secondaryButton")
        self.web_search_toggle_btn.setCheckable(True)
        self.web_search_toggle_btn.setToolTip("Forzar búsqueda web en este turno")
        self.web_search_toggle_btn.toggled.connect(self._on_web_search_toggle)

        self.proactive_toggle_btn = QPushButton("💭")
        self.proactive_toggle_btn.setFixedSize(48, 48)
        self.proactive_toggle_btn.setObjectName("secondaryButton")
        self.proactive_toggle_btn.setCheckable(True)
        self.proactive_toggle_btn.setToolTip(
            "Permitir mensajes espontáneos (SovNode puede escribir sin que le hables, "
            "cada ~20 min de inactividad, solo si tiene algo real que aportar)"
        )
        self.proactive_toggle_btn.toggled.connect(self._on_proactive_toggle)

        self.stop_button = QPushButton(tr["btn_stop"])
        self.stop_button.setObjectName("secondaryButton")
        self.stop_button.setFixedHeight(48)
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._stop_generation)

        self.send_button = QPushButton(tr["btn_send"])
        self.send_button.setObjectName("sendButton")
        self.send_button.setFixedSize(80, 48)
        self.send_button.clicked.connect(self._send_message)

        input_layout.addWidget(self.input_field, 1)
        input_layout.addWidget(self.mic_button)
        input_layout.addWidget(self.web_search_toggle_btn)
        input_layout.addWidget(self.proactive_toggle_btn)
        input_layout.addWidget(self.stop_button)
        input_layout.addWidget(self.send_button)

        content_layout.addWidget(input_container)

        # Panel de Terminal
        self.terminal_panel = QFrame()
        self.terminal_panel.setObjectName("terminalPanel")
        self.terminal_panel.setFixedHeight(160)
        self.terminal_panel.setVisible(False)

        term_layout = QVBoxLayout(self.terminal_panel)
        term_layout.setContentsMargins(12, 10, 12, 10)

        term_header = QHBoxLayout()
        term_title = QLabel("🖥️ CONSOLA DE SISTEMA / LOGS")
        term_title.setObjectName("terminalTitle")

        btn_clear_term = QPushButton("Limpiar")
        btn_clear_term.setObjectName("terminalClearButton")
        btn_clear_term.clicked.connect(self._clear_terminal)

        term_header.addWidget(term_title)
        term_header.addStretch()
        term_header.addWidget(btn_clear_term)

        self.terminal_output = QTextEdit()
        self.terminal_output.setObjectName("terminalOutput")
        self.terminal_output.setReadOnly(True)

        term_layout.addLayout(term_header)
        term_layout.addWidget(self.terminal_output)

        content_layout.addWidget(self.terminal_panel)
        main_layout.addWidget(content_container, 1)

    def _on_theme_changed(self, theme_name: str) -> None:
        self._apply_theme(theme_name)
        self._terminal_log(
            I18N[self._current_lang]["log_theme_changed"].format(theme_name), "info"
        )

    def _on_lang_changed(self, lang_name: str) -> None:
        self._current_lang = lang_name
        self.orchestrator.set_language(lang_name)
        tr = I18N[lang_name]

        self.input_field.setPlaceholderText(tr["placeholder"])
        self.send_button.setText(tr["btn_send"])
        self.stop_button.setText(tr["btn_stop"])
        self.btn_new_chat.setText(tr["btn_new_chat"])
        self.btn_export.setText(tr["btn_export"])
        self.btn_export_training.setText(tr["btn_export_training"])
        self.btn_donate.setText(tr["btn_donate"])
        self.header_title_label.setText(tr["header_title"])
        self.header_subtitle_label.setText(tr["header_subtitle"])
        self.scroll_to_bottom_btn.setText(tr["jump_to_bottom"])

        # Títulos de sección de la barra lateral - antes eran variables
        # locales sin referencia guardada (tc_title/lc_title/sc_title/
        # mc_title), así que quedaban fijos en el idioma con el que se
        # construyó la ventana la primera vez, sin importar qué idioma
        # se seleccionara después en este mismo combo. (mc_title/
        # self.model_title_label ya no existe — se sacó junto con todo
        # el texto "ACTIVE MODEL (last turn)", ver la tarjeta "Modelo
        # Activo" más arriba en _init_ui.)
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

        # Re-renderiza la insignia de procedencia del header (Módulo 3) en
        # el idioma nuevo, sin esperar a que termine otro turno - usa el
        # mismo modo ya calculado (o None si aún no hubo ningún turno).
        self._set_header_status_badge(self._last_web_mode)

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

    def _clear_terminal(self) -> None:
        if hasattr(self, "terminal_output") and self.terminal_output:
            self.terminal_output.clear()

    def _clear_chat(self) -> None:
        self._stop_tts()
        for bubble in self._bubble_widgets:
            bubble.deleteLater()
        self._bubble_widgets.clear()
        for web_card in self._web_card_widgets:
            web_card.deleteLater()
        self._web_card_widgets.clear()
        self._chat_entries.clear()
        self._set_header_status_badge(None)
        # CRÍTICO: sin esto, el historial de MemoryGraph (persistente en
        # disco, no en RAM) sobrevivía a "Nueva conversación" - el
        # siguiente mensaje, aunque fuera tan simple como "hi", seguía
        # arrastrando turnos de una sesión anterior completamente
        # distinta al prompt del modelo (ver Orchestrator.clear_conversation_memory).
        self.orchestrator.clear_conversation_memory()
        self._add_bubble("assistant", I18N[self._current_lang]["new_chat_msg"])
        self._terminal_log(I18N[self._current_lang]["log_new_chat"], "info")

    def _set_ui_controls_enabled(self, enabled: bool) -> None:
        """
        Bloquea/desbloquea en bloque los controles de la barra lateral y
        de entrada que pueden alterar estado compartido (idioma, tema,
        tamaño de modelo, descarga, chat) mientras hay una inferencia en
        curso — evita que el usuario dispare uno de esos cambios a mitad
        de un turno y deje al Orchestrator o a la UI en un estado
        parcialmente actualizado / con condiciones de carrera.
        """
        self.combo_lang.setEnabled(enabled)
        self.combo_theme.setEnabled(enabled)
        if hasattr(self, "btn_download_model"):
            self.btn_download_model.setEnabled(enabled)
        self.btn_new_chat.setEnabled(enabled)
        self.btn_export.setEnabled(enabled)
        self.btn_export_training.setEnabled(enabled)
        self.btn_donate.setEnabled(enabled)
        if hasattr(self, "mic_button"):
            self.mic_button.setEnabled(enabled)

    def _on_web_search_toggle(self, checked: bool) -> None:
        self._force_web_search = checked
        self.web_search_toggle_btn.setStyleSheet(
            "background-color: #3DDC97; color: #0F2A20;" if checked else ""
        )

    def _on_proactive_toggle(self, checked: bool) -> None:
        """
        Toggle de "mensajes espontáneos" (pedido explícito: "que pueda
        escribir cuando quiera o solo cuando se le hable"). Apagado por
        defecto — prender esto es una decisión consciente del usuario,
        no un comportamiento que se activa solo.
        """
        self._proactive_mode_enabled = checked
        self.proactive_toggle_btn.setStyleSheet(
            "background-color: #3DDC97; color: #0F2A20;" if checked else ""
        )
        if checked:
            self._terminal_log(
                "Mensajes espontáneos activados: SovNode puede escribirte sin que "
                "le hables, solo si el chequeo periódico encuentra algo real que "
                "aportar (cada ~20 min de inactividad).",
                "info",
            )
        else:
            self._terminal_log("Mensajes espontáneos desactivados.", "info")

    def _on_battery_power(self) -> Optional[bool]:
        """
        `True` si la máquina corre a batería (no enchufada), `False` si
        está enchufada o es un equipo sin batería (desktop), `None` si
        no se puede determinar (psutil ausente, o la plataforma no
        expone el sensor). `None` se trata siempre como "no se puede
        optimizar por esto, seguir adelante" — no se bloquea una función
        que el usuario pidió solo porque no podemos medir el estado de
        energía.

        HONESTIDAD: no hay batería real en el entorno donde escribí y
        corrí los tests de este cambio (sandbox en la nube) — esta
        función es lógica razonada sobre la API documentada de psutil,
        no algo que haya podido verificar contra hardware real. Si en tu
        máquina psutil.sensors_battery() se comporta distinto a lo
        documentado, avisame.
        """
        try:
            import psutil
        except ImportError:
            return None
        try:
            battery = psutil.sensors_battery()
        except Exception:
            return None
        if battery is None:
            return None
        return not battery.power_plugged

    def _maybe_run_reflection(self) -> None:
        """
        Callback de self.reflection_timer — el timer corre siempre (es
        barato: solo dispara esta función cada REFLECTION_INTERVAL_MS),
        pero acá adentro no hace nada salvo que:
          1. El toggle esté prendido (_proactive_mode_enabled).
          2. No haya un turno normal en curso — nunca dos llamadas al
             mismo Ollama local en simultáneo.
          3. No haya ya una reflexión corriendo (guarda contra
             solapamiento si una tardara más que el intervalo).
          4. Haya habido actividad real del usuario desde la ÚLTIMA
             reflexión — evita disparar una llamada real al LLM cada 20
             minutos sin límite durante una sesión inactiva larga; como
             mucho, UNA llamada por período de inactividad (BLINDAJE de
             optimización de recursos, pedido explícito).
          5. La máquina no esté corriendo a batería (si se puede saber
             — ver _on_battery_power) — un chequeo que el usuario ni
             pidió no debería gastar batería de una notebook.
        Sin historial de conversación, Orchestrator.generate_spontaneous_
        reflection ya devuelve None de inmediato sin llamar al LLM — acá
        no hace falta duplicar ese chequeo.
        """
        if not self._proactive_mode_enabled:
            return
        if not self._had_activity_since_last_reflection:
            return
        if self._turn_worker is not None and self._turn_worker.isRunning():
            return
        if self._reflection_worker is not None and self._reflection_worker.isRunning():
            return
        if self._on_battery_power() is True:
            logger.debug("Reflexión espontánea: salteada, la máquina corre a batería.")
            return

        # Se marca ACÁ (no al recibir el resultado): lo que limita la
        # frecuencia es el INTENTO, no si encontró algo que decir - si
        # se marcara solo tras un aporte real, un tramo largo sin nada
        # que agregar seguiría reintentando cada 20 min sin límite.
        self._had_activity_since_last_reflection = False

        self._reflection_worker = ReflectionWorker(self.orchestrator)
        self._reflection_worker.reflection_ready.connect(self._on_reflection_ready)
        self._reflection_worker.finished_no_reflection.connect(self._on_reflection_none)
        self._reflection_worker.error_occurred.connect(self._on_reflection_error)
        self._reflection_worker.start()

    def _on_reflection_ready(self, text: str) -> None:
        """
        El prefijo 💭 es deliberado y ÚNICO propósito visual: distinguir
        de un vistazo un mensaje que nadie pidió de una respuesta a algo
        que el usuario preguntó — no implica nada sobre si "hay alguien
        pensando" detrás (ver HONESTIDAD en generate_spontaneous_reflection).
        """
        self._add_bubble("assistant", f"💭 {text}")
        self._terminal_log("Mensaje espontáneo generado (chequeo periódico).", "info")

    def _on_reflection_none(self) -> None:
        logger.debug("Reflexión espontánea: nada que aportar en este chequeo.")

    def _on_reflection_error(self, msg: str) -> None:
        # Deliberado: un fallo en este chequeo de fondo NO debe
        # mostrarse como un error alarmante al usuario - es una
        # funcionalidad de "nice to have", no un turno que pidió.
        logger.debug("Reflexión espontánea: error en el chequeo periódico: %s", msg)

    def _send_message(self) -> None:
        text = self.input_field.toPlainText().strip()
        if not text:
            return

        # Actividad real del usuario: re-habilita UNA futura reflexión
        # espontánea (ver _maybe_run_reflection) - sin esto, tras la
        # primera reflexión de una sesión larga, la función quedaría
        # bloqueada para siempre aunque el usuario siguiera charlando y
        # después volviera a dejar la app inactiva.
        self._had_activity_since_last_reflection = True

        self._stop_tts()
        self._set_ui_controls_enabled(False)
        self.input_field.clear()
        self._add_bubble("user", text)
        self._terminal_log(
            I18N[self._current_lang]["log_sending"].format(text[:50]), "info"
        )

        self._current_stream_bubble = self._add_bubble("assistant", "")
        self._stream_buffer = ""

        self.send_button.setVisible(False)
        self.stop_button.setVisible(True)
        self.progress_bar.setVisible(True)

        # Restablecer anclaje de scroll al iniciar nueva respuesta
        if hasattr(self, "chat_scroll"):
            self.chat_scroll.pin_to_bottom()

        self._elapsed_seconds = 0
        self.processing_timer.start(1000)
        self._render_timer.start()

        self._turn_worker = StreamTurnWorker(
            self.orchestrator, text, force_web_search=self._force_web_search
        )
        self._turn_worker.intent_changed.connect(self._on_intent_changed)
        self._turn_worker.web_results_ready.connect(self._on_web_results_ready)
        self._turn_worker.chunk_received.connect(self._on_chunk_received)
        self._turn_worker.completed.connect(self._on_turn_completed)
        self._turn_worker.log_message.connect(self._on_worker_log_message)
        self._turn_worker.start()

    def _stop_generation(self) -> None:
        if self._turn_worker and self._turn_worker.isRunning():
            self._turn_worker.stop()
            self._set_ui_controls_enabled(True)
            self._terminal_log(I18N[self._current_lang]["log_stopped_by_user"], "warn")

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

        if mode == "live":
            self.header_status.setText(tr["web_badge_live"])
            self.header_status.setStyleSheet(
                "color:#3DDC97; font-size:10px; font-weight:800; "
                "background-color:#0F2A20; border:1px solid #3DDC97; "
                "border-radius:9px; padding:3px 11px;"
            )
            self.header_status.setVisible(True)
        elif mode == "local":
            self.header_status.setText(tr["web_badge_local"])
            self.header_status.setStyleSheet(
                "color:#8B92A5; font-size:10px; font-weight:700; "
                "background-color:#1B1F2A; border:1px solid #30363D; "
                "border-radius:9px; padding:3px 11px;"
            )
            self.header_status.setVisible(True)
        else:
            self.header_status.setVisible(False)

    def _on_turn_completed(self, trace: Any, error_msg: str) -> None:
        self.processing_timer.stop()
        self._render_timer.stop()
        self._flush_stream_buffer()
        # Nota (vaciado forzado al completar el turno): _flush_stream_
        # buffer() ya vació el buffer CRUDO de chunks hacia update_content(),
        # pero MessageBubble aplica su PROPIO throttle interno
        # (MIN_RENDER_INTERVAL_MS) sobre ese update_content() - si esta
        # última llamada cayó dentro de esa ventana, el fragmento final
        # queda diferido en vez de mostrarse ya. force_flush_render()
        # cancela ese temporizador propio del globo y renderiza de
        # inmediato lo que quedó pendiente, para que ningún token quede
        # atrapado en memoria sin llegar a pantalla.
        if self._current_stream_bubble:
            self._current_stream_bubble.force_flush_render()
        self._remove_thinking_widget()
        self._set_ui_controls_enabled(True)

        self.progress_bar.setVisible(False)
        self.processing_label.setText("")
        self.stop_button.setVisible(False)
        self.send_button.setVisible(True)

        if error_msg:
            # Si el turno falló antes de emitir ningún token (p. ej. error
            # de Ollama detectado de entrada, o cancelación temprana), el
            # globo vacío de streaming se queda flotando arriba del globo
            # de error - se saca antes de agregar el de error.
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
            # RECONCILIACIÓN CON EL TEXTO CANÓNICO: lo que se mostró
            # durante el streaming es el flujo CRUDO del modelo; el texto
            # definitivo del turno es `trace.final_response`, que ya pasó
            # por _split_thought_and_content (bloques etiquetados) Y por
            # _strip_leaked_reasoning (protocolo volcado sin etiquetar).
            #
            # Sin esto, una fuga de razonamiento se quedaba en pantalla
            # para siempre: se detectaba y se limpiaba para memoria/WAL,
            # pero el globo seguía mostrando el borrador interno que ya
            # se había emitido token a token. update_content() reemplaza
            # el contenido completo (no lo concatena), así que basta con
            # volver a renderizar el texto bueno.
            final_text = str(getattr(trace, "final_response", "") or "").strip()
            shown_text = str(getattr(self._current_stream_bubble, "_content", "") or "").strip()
            if final_text and final_text != shown_text:
                self._current_stream_bubble.update_content(final_text)
                self._current_stream_bubble.force_flush_render()

            # Módulo 3: la insignia de procedencia de datos ya no vive en
            # el globo de respuesta - se actualiza en el header superior.
            web_used = getattr(trace, "web_context_used", False)
            mode = "live" if web_used else "local"
            self._set_header_status_badge(mode)

            if hasattr(trace, "model_used"):
                model_name = getattr(trace, "model_used", "desconocido")
                # El texto "Last turn: {modelo}" en el sidebar (self.model_label)
                # se sacó a pedido del usuario — ocupaba espacio y esta misma
                # info ya queda registrada acá abajo, en el log de la terminal.
                self._terminal_log(
                    I18N[self._current_lang]["log_turn_completed"].format(model_name),
                    "ok",
                )

    def _apply_theme(self, theme_name: str) -> None:
        if theme_name in THEMES:
            style = build_style(THEMES[theme_name])
            self.setStyleSheet(style)
            # Los tres temas son variantes oscuras (ver THEMES) - las
            # ecuaciones renderizadas por math_render.py deben usar el
            # mismo color de texto que el resto de la UI en el tema
            # activo, no quedar pinneadas al de Cyberpunk Dark.
            math_render.set_equation_color(THEMES[theme_name]["text"])

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
        """Fuerza el scroll hacia el fondo y vuelve a anclar la vista."""
        if hasattr(self, "chat_scroll"):
            self.chat_scroll.pin_to_bottom()
            self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        """Desplaza automáticamente la vista al final SOLO si la vista está anclada al fondo (Smart Auto-Scroll)."""
        if not hasattr(self, "chat_scroll") or not self.chat_scroll:
            return

        if not self.chat_scroll.is_pinned_to_bottom:
            return

        def _force_scroll():
            if self.chat_widget:
                self.chat_widget.adjustSize()
            scrollbar = self.chat_scroll.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

        _force_scroll()
        QTimer.singleShot(10, _force_scroll)

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

        self._scroll_to_bottom()

    def _add_bubble(
        self,
        sender: str,
        content: str,
        trace: Optional[Any] = None,
        is_error: bool = False,
        is_warning: bool = False,
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
        )
        bubble.tts_requested.connect(self._play_tts)
        bubble.tts_stop_requested.connect(self._stop_tts)

        self._bubble_widgets.append(bubble)

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
            self.mic_button.setText("🔴")
            self.mic_button.setStyleSheet(
                "background-color: #F2555A; color: white;"
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
            self.mic_button.setText("⏳")
            self.mic_button.setEnabled(False)
            self._terminal_log(
                I18N[self._current_lang]["log_voice_processing"], "info"
            )

            if self._voice_worker:
                self._voice_worker.stop_recording()

    def _on_transcription_ready(self, text: str) -> None:
        self.mic_button.setText("🎙️")
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


def main() -> int:
    try:
        myappid = "sovnode.desktop.sovereignai.1.0"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("SovNode")

    icon_path = get_resource_path("logo.ico")
    app.setWindowIcon(QIcon(icon_path))

    app.setQuitOnLastWindowClosed(True)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())