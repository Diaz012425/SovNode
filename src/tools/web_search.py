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
El Monolito Personal - Arquitectura v2.0
web_search.py — Motor de búsqueda web con blindaje anti-congelamiento.
"""
from __future__ import annotations

import contextlib
import html
import json
import logging
import math
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Optional, Pattern, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

try:
    from relevance import (
        asks_about_final,
        distinctive_words,
        extract_years as _extract_years,
        has_conflicting_year as _has_conflicting_year,
        is_live_event_query,
        is_topically_relevant,
        keyword_overlap as _keyword_overlap,
        mentions_non_final_round,
        needs_strict_relevance,
        requires_precise_fact,
        round_context_matches_final,
        significant_words as _significant_words,
        text_is_relevant,
        title_names_the_final,
    )
except ImportError:
    _YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

    def _extract_years(text: str) -> Set[str]:
        return set(_YEAR_RE.findall(text or ""))

    def _has_conflicting_year(query: str, text: str) -> bool:
        q_years = _extract_years(query)
        if not q_years:
            return False
        t_years = _extract_years(text)
        return bool(t_years and not (q_years & t_years))

    def asks_about_final(query: str) -> bool:
        return False

    def is_live_event_query(query: str) -> bool:
        return False

    def is_topically_relevant(query: str, text: str) -> bool:
        return True

    def _keyword_overlap(query: str, text: str) -> int:
        q_words = _significant_words(query)
        t_words = _significant_words(text)
        return len(q_words & t_words)

    def mentions_non_final_round(text: str) -> bool:
        return False

    def needs_strict_relevance(query: str) -> bool:
        return False

    def requires_precise_fact(query: str) -> bool:
        return False

    def round_context_matches_final(full_text: str, abs_start: int = 0, abs_end: int = 0) -> bool:
        return True

    def _significant_words(text: str) -> Set[str]:
        return {w.lower() for w in re.findall(r"\b\w{3,}\b", text or "")}

    def distinctive_words(text: str) -> Set[str]:
        return _significant_words(text)

    def text_is_relevant(query: str, text: str) -> bool:
        return True

    def title_names_the_final(title: str) -> bool:
        return False

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

try:
    import requests
except ImportError:
    requests = None

try:
    import trafilatura
    from trafilatura.settings import use_config as _trafilatura_use_config
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False

logger = logging.getLogger("Monolith.WebSearch")

# =====================================================================
# CONFIGURACIÓN Y TIMEOUTS
# =====================================================================
SEARCH_BACKEND = os.getenv("SOVNODE_SEARCH_BACKEND", "duckduckgo").strip().lower()
SEARXNG_ENDPOINT = os.getenv("SEARXNG_ENDPOINT", "http://localhost:8080").rstrip("/")
SEARXNG_AUXILIARY_ENABLED = os.getenv("SOVNODE_SEARXNG_AUXILIARY", "").strip().lower() in ("1", "true", "yes", "on")


def _optional_float_env(var_name: str, default: Optional[float]) -> Optional[float]:
    raw = os.getenv(var_name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in ("", "none", "off", "disabled", "0"):
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("Valor inválido en %s=%r, se usa el default (%s).", var_name, raw, default)
        return default


SEARCH_ENGINE_TIMEOUT_SECONDS: Optional[float] = _optional_float_env("SOVNODE_SEARCH_TIMEOUT", 2.5)
GLOBAL_SEARCH_TIMEOUT_SECONDS: Optional[float] = _optional_float_env("SOVNODE_SEARCH_GLOBAL_TIMEOUT", 5.0)
ARTICLE_FETCH_TIMEOUT_SECONDS: Optional[float] = _optional_float_env("SOVNODE_ARTICLE_TIMEOUT", 2.5)
ARTICLE_MAX_CHARS = int(os.getenv("SOVNODE_ARTICLE_MAX_CHARS", "1600"))
MAX_ARTICLES_TO_SCRAPE = int(os.getenv("SOVNODE_MAX_SCRAPE", "4"))
WIKI_API_EXTRACT_CHARS = int(os.getenv("SOVNODE_WIKI_EXTRACT_CHARS", "500"))
WIKI_PRECISE_FACT_EXTRACT_CHARS = int(os.getenv("SOVNODE_WIKI_PRECISE_FACT_EXTRACT_CHARS", "2500"))
WIKI_FINAL_EXTRACT_CHARS = int(os.getenv("SOVNODE_WIKI_FINAL_EXTRACT_CHARS", "7000"))
WIKI_SEQUENCE_BUDGET_SECONDS: float = float(os.getenv("SOVNODE_SEARCH_WIKI_SEQUENCE_BUDGET", "4.0"))

_ddg_budget_env = os.getenv("SOVNODE_SEARCH_DDG_SEQUENCE_BUDGET")
DDG_SEQUENCE_BUDGET_SECONDS: float = (
    float(_ddg_budget_env) if _ddg_budget_env is not None
    else (6.0 if SEARXNG_AUXILIARY_ENABLED else 4.5)
)

WIKI_API_THUMB_SIZE = int(os.getenv("SOVNODE_WIKI_THUMB_SIZE", "800"))
MAX_TOTAL_CONTEXT_CHARS = int(os.getenv("SOVNODE_MAX_CONTEXT_CHARS", "6000"))
HARD_TIMEOUT_FALLBACK_SECONDS: float = float(os.getenv("SOVNODE_SEARCH_HARD_FALLBACK", "600.0"))


def _effective_timeout(value: Optional[float]) -> float:
    return value if value is not None else HARD_TIMEOUT_FALLBACK_SECONDS


_DDG_USER_AGENTS: Tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
)

_TRAFILATURA_CONFIG = None
if TRAFILATURA_AVAILABLE:
    try:
        _TRAFILATURA_CONFIG = _trafilatura_use_config()
        _timeout_str = str(max(1, round(_effective_timeout(ARTICLE_FETCH_TIMEOUT_SECONDS))))
        _TRAFILATURA_CONFIG.set("DEFAULT", "DOWNLOAD_TIMEOUT", _timeout_str)
        _TRAFILATURA_CONFIG.set("DEFAULT", "EXTRACTION_TIMEOUT", _timeout_str)
        _TRAFILATURA_CONFIG.set("DEFAULT", "SLEEP_TIME", "0")
        _TRAFILATURA_CONFIG.set("DEFAULT", "USER_AGENTS", "\n".join(_DDG_USER_AGENTS))
    except Exception as exc:
        logger.warning("No se pudo configurar timeout de trafilatura: %s", exc)
        _TRAFILATURA_CONFIG = None

_LAST_SEARCH_ERROR: Optional[str] = None


def get_last_search_error() -> Optional[str]:
    global _LAST_SEARCH_ERROR
    return _LAST_SEARCH_ERROR


# =====================================================================
# TRAZA EN VIVO HACIA LA UI
# =====================================================================
LogCallback = Optional[Callable[[str], None]]


def _emit_log(log_cb: LogCallback, message: str) -> None:
    if log_cb is None:
        return
    try:
        log_cb(message)
    except Exception as exc:
        logger.debug("log_cb falló al recibir traza (%r): %s", message, exc)


# Bug real, MEDIDO (screenshot 2026-08-27, idioma de la UI en English):
# TODAS las líneas de este módulo hacia la consola de logs estaban
# hardcodeadas en español ("Consultando motor de búsqueda...", "5
# fuente(s) recuperado(s)...") sin importar el idioma de la UI, aunque
# el parámetro `lang` ya viaja hasta acá desde orchestrator.py en casi
# todas las funciones (search_web/search_web_context/search/
# get_web_results ya lo reciben y lo pasan hacia abajo). El problema
# nunca fue que faltara el dato — era que ningún call site de
# `_emit_log` lo usaba. `_msg()` centraliza la elección: mismo patrón
# bilingüe que ya usa orchestrator.py (`if lang == "English": ... else:
# ...`), adaptado al valor "en"/"es" (no "English"/"Spanish") que este
# módulo usa en su propia convención de `lang` (ver `_search_via_
# wikipedia`, que ya comparaba contra "en" para elegir el dominio de
# Wikipedia).
def _msg(lang: Optional[str], es: str, en: str) -> str:
    return en if (lang or "").strip().lower().startswith("en") else es


# =====================================================================
# PUENTE DE TOLERANCIA A FALLOS
# =====================================================================
_RECOVERABLE_EXCEPTIONS: Tuple[type, ...] = (
    URLError,
    HTTPError,
    NameError,
    json.JSONDecodeError,
    ConnectionError,
    OSError,
    ValueError,
    RuntimeError,
)

_GUARD_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="WebSearchGuard")


def _describe_exception(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def safe_execute(
    engine_fn,
    *args: Any,
    timeout_sec: Optional[float] = None,
    engine_name: Optional[str] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    global _LAST_SEARCH_ERROR
    name = engine_name or getattr(engine_fn, "__name__", "motor_desconocido")
    effective_timeout = _effective_timeout(timeout_sec)

    future = _GUARD_POOL.submit(engine_fn, *args, **kwargs)
    try:
        result = future.result(timeout=effective_timeout)
        return result if isinstance(result, list) else []
    except FuturesTimeoutError:
        reason = "timeout del guardián" if timeout_sec is not None else "techo duro de respaldo"
        msg = f"{name}: sin respuesta dentro de {effective_timeout:.1f}s ({reason})"
        logger.warning("🛡️ [WebSearch-Guard] %s", msg)
        _LAST_SEARCH_ERROR = msg
        return []
    except _RECOVERABLE_EXCEPTIONS as exc:
        msg = f"{name}: {type(exc).__name__}: {exc}"
        logger.warning("🛡️ [WebSearch-Guard] Motor degradado — %s", msg)
        _LAST_SEARCH_ERROR = msg
        return []
    except Exception as exc:
        msg = f"{name}: fallo inesperado {type(exc).__name__}: {exc}"
        logger.warning("🛡️ [WebSearch-Guard] %s", msg)
        _LAST_SEARCH_ERROR = msg
        return []


# =====================================================================
# REINTENTOS CON RETROCESO EXPONENCIAL
# =====================================================================
_MAX_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.5


def _call_with_backoff(
    fn: Callable[[], Any],
    *,
    attempts: int = _MAX_RETRY_ATTEMPTS,
    base_delay: float = _RETRY_BASE_DELAY_SECONDS,
    budget_left_fn: Optional[Callable[[], float]] = None,
    op_name: str = "operación",
    log_cb: LogCallback = None,
    lang: Optional[str] = None,
) -> Any:
    last_exc: Optional[Exception] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            if budget_left_fn is not None:
                remaining = budget_left_fn()
                if remaining <= 0.5:
                    break
                delay = min(delay, max(0.0, remaining - 0.5))
            if delay <= 0:
                break
            _emit_log(
                log_cb,
                _msg(
                    lang,
                    f"[WEB_SEARCH] {op_name} falló (intento {attempt}/{attempts}: {exc}) "
                    f"— reintentando en {delay:.1f}s...",
                    f"[WEB_SEARCH] {op_name} failed (attempt {attempt}/{attempts}: {exc}) "
                    f"— retrying in {delay:.1f}s...",
                ),
            )
            logger.debug("🛡️ [WebSearch-Retry] %s intento %d/%d falló: %s", op_name, attempt, attempts, exc)
            time.sleep(delay)
    raise last_exc


# =====================================================================
# REPUTACIÓN Y FILTRADO DE DOMINIOS
# =====================================================================
TRUSTED_DOMAIN_BONUS: Dict[str, float] = {
    "wikipedia.org": 2.0, "bbc.com": 1.9, "bbc.co.uk": 1.9,
    "reuters.com": 2.0, "apnews.com": 2.0, "efe.com": 1.7, "afp.com": 1.7,
    "elpais.com": 1.6, "eltiempo.com": 1.4, "infobae.com": 1.3,
    "theguardian.com": 1.7, "nytimes.com": 1.7, "washingtonpost.com": 1.7,
    "france24.com": 1.5, "dw.com": 1.5, "github.com": 1.5,
    "developer.mozilla.org": 1.9, "docs.python.org": 1.9,
}

BLOCKED_DOMAINS: set = {
    "tiktok.com", "instagram.com", "facebook.com", "x.com", "twitter.com",
    "pinterest.com", "threads.net", "quora.com", "reddit.com", "tumblr.com",
}

PENALIZED_DOMAIN_SUFFIXES: Dict[str, float] = {
    "blogspot.com": 0.6, "wordpress.com": 0.7, "medium.com": 0.8,
}

_OFFICIAL_TLD_BONUS = {".gov": 1.6, ".edu": 1.4, ".gob": 1.5}

HARD_DATA_DOMAINS: set = {
    "wikipedia.org", "wikidata.org", "britannica.com",
    "espn.com", "espndeportes.espn.com", "fifa.com", "uefa.com",
    "olympics.com", "olympics.org", "premierleague.com", "laliga.com",
    "nba.com", "mlb.com", "nfl.com", "atptour.com", "wtatennis.com",
    "formula1.com", "transfermarkt.com", "transfermarkt.es",
    "besoccer.com", "flashscore.com", "livescore.com", "marca.com",
    "worldbank.org", "who.int", "un.org", "imf.org",
}


def is_hard_data_source(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return False
    if not domain:
        return False
    return any(
        domain == hard or domain.endswith("." + hard)
        for hard in HARD_DATA_DOMAINS
    )


_SLOW_MOVING_ENCYCLOPEDIC_DOMAINS: set = {"wikipedia.org", "wikidata.org", "britannica.com"}
_LIVE_EVENT_ENCYCLOPEDIC_DAMPING: float = 0.45


def _is_slow_moving_encyclopedic(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return False
    return any(
        domain == d or domain.endswith("." + d)
        for d in _SLOW_MOVING_ENCYCLOPEDIC_DOMAINS
    )


_CLICKBAIT_PHRASES_RE = re.compile(
    r"\b(no vas a creer|impactante|viral:|se volvi[oó] viral|click aqu[ií]|haz click)\b",
    re.IGNORECASE,
)
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002700-\U000027BF]+")


def score_domain(url: str) -> Optional[float]:
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return 1.0

    if not domain or any(domain == b or domain.endswith("." + b) for b in BLOCKED_DOMAINS):
        return None

    score = 1.0
    for trusted, bonus in TRUSTED_DOMAIN_BONUS.items():
        if domain == trusted or domain.endswith("." + trusted):
            score = max(score, bonus)
            break

    for suffix, penalty in PENALIZED_DOMAIN_SUFFIXES.items():
        if domain.endswith(suffix):
            score = min(score, penalty)

    for tld, bonus in _OFFICIAL_TLD_BONUS.items():
        if domain.endswith(tld) or f"{tld}." in domain:
            score = max(score, bonus)

    return score


def clean_headline(title: str) -> str:
    if not title:
        return title
    clean = _CLICKBAIT_PHRASES_RE.sub("", title)
    clean = _EMOJI_RE.sub("", clean)
    return re.sub(r"\s{2,}", " ", clean).strip(" -–—|:") or title.strip()


def _favicon_url(url: str) -> Optional[str]:
    if not url:
        return None
    try:
        domain = urlparse(url).netloc
    except Exception:
        return None
    return f"https://icons.duckduckgo.com/ip3/{domain}.ico" if domain else None


# =====================================================================
# SANITIZACIÓN Y LIMPIEZA DE TEXTO
# =====================================================================
_NOISE_ANYWHERE_RE = re.compile(
    r"\b("
    r"por\s+favor|please|"
    r"dime|cu[eé]ntame|expl[ií]came|mu[eé]strame|dame|proporci[oó]name|"
    r"tell\s+me|show\s+me|explain\s+to\s+me|give\s+me|"
    r"b[uú]scam?e?|busca(?:r)?|investiga(?:r)?|averigua(?:r)?|consulta(?:r)?|"
    r"encuentra(?:r)?|rastrea(?:r)?|chequea\w*|"
    r"search(?:\s+for)?|find(?:\s+out)?|look\s+up|investigate|check|research|"
    r"en\s+internet|en\s+google|en\s+la\s+web|en\s+l[ií]nea|"
    r"(?:on|in)\s+(?:the\s+)?internet|on\s+google|online|"
    r"el\s+resultado\s+de|informaci[oó]n\s+sobre|"
    r"(?:can|could|would|will)\s+you(?:\s+please)?"
    r")\b",
    re.IGNORECASE,
)

_LEADING_GREETINGS_RE = re.compile(
    r"^[\s,;:¡!¿?.]*(hola|hey|hi|hello)\b[\s,;:]*",
    re.IGNORECASE,
)

_TRAILING_ACTION_RESIDUE_RE = re.compile(
    r"\s*\b(?:search|online|internet|busca(?:r)?)\b\s*$",
    re.IGNORECASE,
)


def sanitize_query(raw_query: Any) -> str:
    try:
        if not raw_query:
            return ""
        clean = str(raw_query).strip()
        while True:
            subbed = _NOISE_ANYWHERE_RE.sub(" ", clean)
            subbed = _LEADING_GREETINGS_RE.sub("", subbed)
            subbed = _TRAILING_ACTION_RESIDUE_RE.sub("", subbed)
            subbed = re.sub(r"\s{2,}", " ", subbed).strip()
            if subbed == clean:
                break
            clean = subbed
        clean = re.sub(r"\s{2,}", " ", clean).strip("?¡!¿.,;:'\"")
        return clean if len(clean) >= 2 else str(raw_query).strip("?¡!¿.,;:'\"")
    except Exception as exc:
        logger.debug("sanitize_query degradado, se usa el texto crudo: %s", exc)
        return str(raw_query or "").strip()


_NULL_AND_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Bloques cuyo CONTENIDO (no solo la etiqueta) hay que descartar antes del
# strip genérico de tags — bug real: _HTML_TAG_RE por sí solo solo borra
# las etiquetas <script>/<style>, no el JS/CSS que queda ENTRE ellas, así
# que ese código quedaba colándose como texto "limpio" hacia el modelo si
# el fragmento escaneado (snippet/resultado de DDG) llegaba a incluirlo.
_HTML_SCRIPT_STYLE_BLOCK_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)


def _strip_script_and_style_blocks(raw: str) -> str:
    """Descarta <script>/<style>/<noscript> COMPLETOS (etiqueta + contenido).

    Reemplaza por un espacio, no por "": ambos llamadores colapsan
    `\\s{2,}` después, así que esto no agrega espacios visibles, pero
    evita pegar dos palabras si el bloque estaba justo entre ellas.
    """
    return _HTML_SCRIPT_STYLE_BLOCK_RE.sub(" ", raw or "")


def _clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = _NULL_AND_CONTROL_CHARS_RE.sub("", str(text))
    cleaned = _strip_script_and_style_blocks(cleaned)
    # Cada etiqueta se reemplaza por un ESPACIO, no por "". Bug real
    # (2026-09-01): con "" un HTML como `<div>Francia</div><div>ganó</div>`
    # quedaba "Franciaganó" ANTES de que el texto llegara al modelo como
    # evidencia — palabras fusionadas en la fuente misma. El collapse de
    # espacios de abajo absorbe los espacios de más que esto introduce.
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


# =====================================================================
# FILTRO ANTI-FALSO-POSITIVO / RELEVANCIA
# =====================================================================
def _filter_relevance(
    results: List[Dict[str, Any]], clean_q: str, log_cb: LogCallback = None,
    lang: Optional[str] = None,
) -> List[Dict[str, Any]]:
    def _blob(item: Dict[str, Any]) -> str:
        return f"{item.get('title', '')} {item.get('snippet', '')} {item.get('content', '')}"

    survivors = [item for item in results if not _has_conflicting_year(clean_q, _blob(item))]
    if not survivors:
        query_years = sorted(_extract_years(clean_q))
        discarded = [
            f"{item.get('title', '(sin título)')!r} (años: "
            f"{sorted(_extract_years(_blob(item))) or 'ninguno'})"
            for item in results
        ]
        _emit_log(
            log_cb,
            _msg(
                lang,
                f"[WEB_SEARCH] Filtro de año descartó {len(results)} fuente(s) — "
                f"consulta pide {query_years or 'ningún año explícito'}: "
                + "; ".join(discarded[:5]),
                f"[WEB_SEARCH] Year filter discarded {len(results)} source(s) — "
                f"query asks for {query_years or 'no explicit year'}: "
                + "; ".join(discarded[:5]),
            ),
        )
        logger.warning(
            "🛡️ [WebSearch-Relevance] Todas las fuentes descartadas por año "
            "conflictivo (consulta=%s): %s",
            query_years, discarded,
        )
        return []

    if not needs_strict_relevance(clean_q):
        return survivors

    filtered = [item for item in survivors if is_topically_relevant(clean_q, _blob(item))]
    if not filtered:
        discarded_titles = [item.get("title", "(sin título)") for item in survivors]
        _emit_log(
            log_cb,
            _msg(
                lang,
                f"[WEB_SEARCH] Ninguna fuente comparte términos con la consulta — "
                f"se descartan las {len(survivors)}: {discarded_titles[:5]}",
                f"[WEB_SEARCH] No source shares terms with the query — "
                f"discarding all {len(survivors)}: {discarded_titles[:5]}",
            ),
        )
        return []
    return filtered


# =====================================================================
# MOTORES DE BÚSQUEDA (DUCKDUCKGO Y SEARXNG)
# =====================================================================
def _lang_to_ddg_region(lang: Optional[str]) -> str:
    if lang == "en":
        return "us-en"
    if lang == "es":
        return "es-es"
    return "wt-wt"


_DDG_HTML_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]*)"[^>]*>(?P<title>.*?)</a>'
    r'.*?class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_HTML_INNER_TAG_RE = re.compile(r"<[^>]+>")


def _clean_ddg_html_fragment(raw: str) -> str:
    # Mismo bug/fix que _clean_text(): descartar <script>/<style> COMPLETOS
    # (no solo la etiqueta) antes del strip genérico — ver
    # _strip_script_and_style_blocks(). Y, como _clean_text(), reemplazar
    # cada etiqueta por un espacio (no "") para no pegar palabras que
    # estaban en elementos contiguos.
    without_script_style = _strip_script_and_style_blocks(raw or "")
    unescaped = html.unescape(_HTML_INNER_TAG_RE.sub(" ", without_script_style))
    return re.sub(r"\s{2,}", " ", unescaped).strip()


def _unwrap_ddg_redirect_url(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return href
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            qs = urllib.parse.parse_qs(parsed.query)
            real = qs.get("uddg", [None])[0]
            if real:
                return urllib.parse.unquote(real)
    except Exception:
        pass
    return href


def _scrape_duckduckgo_html(
    clean_q: str,
    max_results: int,
    timeout: float,
    log_cb: LogCallback = None,
    budget_left_fn: Optional[Callable[[], float]] = None,
    lang: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if requests is None:
        try:
            urllib.request.urlopen  # noqa: B018 - solo confirma que el módulo está disponible
        except Exception:
            return []

    _emit_log(
        log_cb,
        _msg(
            lang,
            "[WEB_SEARCH] DuckDuckGo (librería) sin resultados — probando scraping HTML directo...",
            "[WEB_SEARCH] DuckDuckGo (library) returned nothing — trying direct HTML scraping...",
        ),
    )

    params = {"q": clean_q}

    def _do_request() -> str:
        headers = {
        "User-Agent": random.choice(_DDG_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://duckduckgo.com/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        }
        if requests is not None:
            resp = requests.get(
                "https://html.duckduckgo.com/html/",
                params=params,
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.text

        query_string = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"https://html.duckduckgo.com/html/?{query_string}", headers=headers
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp_raw:
            return resp_raw.read().decode("utf-8", errors="replace")

    try:
        body = _call_with_backoff(
            _do_request,
            attempts=2,
            budget_left_fn=budget_left_fn,
            op_name="Scraping HTML DuckDuckGo",
            log_cb=log_cb,
            lang=lang,
        )
    except Exception as exc:
        logger.debug("Scraping HTML de DuckDuckGo falló tras reintentos: %s", exc)
        _emit_log(
            log_cb,
            _msg(
                lang,
                f"[WEB_SEARCH] Scraping HTML de DuckDuckGo también falló: {exc}",
                f"[WEB_SEARCH] DuckDuckGo HTML scraping also failed: {exc}",
            ),
        )
        return []

    collected: List[Dict[str, Any]] = []
    seen_urls: set = set()
    try:
        for match in _DDG_HTML_RESULT_RE.finditer(body):
            if len(collected) >= max_results:
                break
            url = _unwrap_ddg_redirect_url(match.group("href"))
            if not url or url in seen_urls:
                continue
            domain_score = score_domain(url)
            if domain_score is None:
                continue
            title = clean_headline(_clean_ddg_html_fragment(match.group("title")))
            snippet = _clean_ddg_html_fragment(match.group("snippet"))
            if not title or not snippet:
                continue
            seen_urls.add(url)
            collected.append({
                "title": title,
                "url": url,
                "domain": urlparse(url).netloc.replace("www.", ""),
                "snippet": snippet,
                "content": snippet,
                "raw_content": snippet,
                "content_source": "snippet",
                "date": "",
                "type": "DDG-HTML-Fallback",
                "score": domain_score,
                "image": None,
                "metadata": {
                    "authoritative": is_hard_data_source(url),
                },
            })
    except Exception as exc:
        logger.debug("Parseo de resultados HTML de DuckDuckGo falló: %s", exc)
        return collected

    _emit_log(
        log_cb,
        _msg(
            lang,
            f"[WEB_SEARCH] Scraping HTML directo: {len(collected)} resultado(s) recuperado(s).",
            f"[WEB_SEARCH] Direct HTML scraping: {len(collected)} result(s) retrieved.",
        ),
    )
    return collected


# =====================================================================
# CACHÉ IN-MEMORY DE RESULTADOS DDG
# =====================================================================
_DDG_RESULT_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_DDG_RESULT_CACHE_LOCK = threading.Lock()
DDG_RESULT_CACHE_TTL_SECONDS = float(os.getenv("SOVNODE_DDG_CACHE_TTL", "300"))


def _ddg_cache_key(clean_q: str, lang: Optional[str], max_results: int) -> str:
    return f"{clean_q}|{lang or ''}|{max_results}"


def _ddg_cache_get(key: str) -> Optional[List[Dict[str, Any]]]:
    with _DDG_RESULT_CACHE_LOCK:
        entry = _DDG_RESULT_CACHE.get(key)
        if entry is None:
            return None
        cached_at, results = entry
        if (time.monotonic() - cached_at) >= DDG_RESULT_CACHE_TTL_SECONDS:
            del _DDG_RESULT_CACHE[key]
            return None
        return results


def _ddg_cache_put(key: str, results: List[Dict[str, Any]]) -> None:
    if not results:
        return
    with _DDG_RESULT_CACHE_LOCK:
        _DDG_RESULT_CACHE[key] = (time.monotonic(), results)


# =====================================================================
# CIRCUIT BREAKER — motores de DuckDuckGo (noticias / texto)
# =====================================================================
# BLINDAJE (bug real, MEDIDO — pedido explícito de reducir los timeouts
# de ~100s sin cambiar de proveedor): este circuit breaker existía SOLO
# para `ddgs.news()`. `ddgs.text()` — usado DOS veces por turno cuando
# noticias no alcanza ("resultados web" + el reintento con la consulta
# CRUDA) — no tenía ninguna protección: en una sesión donde DDG viene
# fallando por red (429 Too Many Requests, TLS handshake EOF — visto
# repetidas veces en esta misma sesión), cada turno pagaba el
# `DDG_SEQUENCE_BUDGET_SECONDS` completo en reintentos con backoff
# contra un backend que YA sabíamos, por el circuit de noticias, que
# estaba caído — puro tiempo tirado antes de caer al respaldo de
# Wikipedia (que sí viene funcionando). Generalizado a una clase
# reutilizable con DOS instancias separadas (no una compartida): la
# búsqueda de noticias pega contra `news.search.yahoo.com` (backend de
# `ddgs`), la de texto contra el backend HTML/API propio de DuckDuckGo
# — son servicios distintos con confiabilidad independiente, así que
# uno caído no debe apagar al otro.
class _DDGCircuitBreaker:
    def __init__(self, threshold: int, cooldown_seconds: float) -> None:
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._threshold = threshold
        self._cooldown = cooldown_seconds

    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def record(self, success: bool) -> None:
        with self._lock:
            if success:
                self._consecutive_failures = 0
                self._open_until = 0.0
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._threshold:
                self._open_until = time.monotonic() + self._cooldown


DDG_NEWS_CIRCUIT_THRESHOLD = int(os.getenv("SOVNODE_DDG_NEWS_CIRCUIT_THRESHOLD", "3"))
DDG_NEWS_CIRCUIT_COOLDOWN_SECONDS = float(os.getenv("SOVNODE_DDG_NEWS_CIRCUIT_COOLDOWN", "300"))
DDG_TEXT_CIRCUIT_THRESHOLD = int(os.getenv("SOVNODE_DDG_TEXT_CIRCUIT_THRESHOLD", "3"))
DDG_TEXT_CIRCUIT_COOLDOWN_SECONDS = float(os.getenv("SOVNODE_DDG_TEXT_CIRCUIT_COOLDOWN", "180"))

_ddg_news_circuit = _DDGCircuitBreaker(DDG_NEWS_CIRCUIT_THRESHOLD, DDG_NEWS_CIRCUIT_COOLDOWN_SECONDS)
_ddg_text_circuit = _DDGCircuitBreaker(DDG_TEXT_CIRCUIT_THRESHOLD, DDG_TEXT_CIRCUIT_COOLDOWN_SECONDS)


def _ddg_news_circuit_open() -> bool:
    return _ddg_news_circuit.is_open()


def _ddg_news_circuit_record(success: bool) -> None:
    _ddg_news_circuit.record(success)


def _search_via_duckduckgo(
    clean_q: str,
    raw_q: str,
    max_results: int,
    lang: Optional[str] = None,
    log_cb: LogCallback = None,
) -> List[Dict[str, Any]]:
    cache_key = _ddg_cache_key(clean_q, lang, max_results)
    cached = _ddg_cache_get(cache_key)
    if cached is not None:
        _emit_log(
            log_cb,
            _msg(
                lang,
                "[WEB_SEARCH] DuckDuckGo: resultado servido desde caché.",
                "[WEB_SEARCH] DuckDuckGo: result served from cache.",
            ),
        )
        return cached

    collected: List[Dict[str, Any]] = []
    seen_urls = set()
    last_exc_text = None
    region = _lang_to_ddg_region(lang)
    sequence_start = time.monotonic()

    def _budget_left() -> float:
        return DDG_SEQUENCE_BUDGET_SECONDS - (time.monotonic() - sequence_start)

    def add_results(items: list, source_label: str) -> None:
        for item in items:
            url = item.get("url") or item.get("href") or ""
            if not url or url in seen_urls:
                continue

            domain_score = score_domain(url)
            if domain_score is None:
                continue

            seen_urls.add(url)
            title = clean_headline(str(item.get("title", "")).strip())
            snippet = str(item.get("body") or item.get("snippet") or "").strip()

            if title and snippet:
                domain = urlparse(url).netloc.replace("www.", "")
                collected.append({
                    "title": title,
                    "url": url,
                    "domain": domain,
                    "snippet": snippet,
                    "content": snippet,
                    "raw_content": snippet,
                    "content_source": "snippet",
                    "date": str(item.get("date", "")).strip(),
                    "type": source_label,
                    "score": domain_score,
                    "image": None,
                    "metadata": {
                        "authoritative": is_hard_data_source(url),
                    },
                })

    try:
        with DDGS(timeout=_effective_timeout(SEARCH_ENGINE_TIMEOUT_SECONDS)) as ddgs:
            if _ddg_news_circuit_open():
                _emit_log(
                    log_cb,
                    _msg(
                        lang,
                        "[WEB_SEARCH] DuckDuckGo (noticias) omitido — falló repetidamente "
                        "esta sesión, en cooldown.",
                        "[WEB_SEARCH] DuckDuckGo (news) skipped — failed repeatedly "
                        "this session, in cooldown.",
                    ),
                )
            else:
                _emit_log(
                    log_cb,
                    _msg(
                        lang,
                        "[WEB_SEARCH] Consultando DuckDuckGo (noticias)...",
                        "[WEB_SEARCH] Querying DuckDuckGo (news)...",
                    ),
                )
                try:
                    news_items = _call_with_backoff(
                        lambda: list(ddgs.news(clean_q, max_results=max_results, region=region)),
                        budget_left_fn=_budget_left,
                        op_name="DDG noticias",
                        log_cb=log_cb,
                        lang=lang,
                    )
                    add_results(news_items, "Noticia")
                    _ddg_news_circuit_record(success=True)
                except Exception as e:
                    last_exc_text = _describe_exception(e)
                    logger.debug("🛡️ [WebSearch] DDG noticias agotó reintentos: %s", last_exc_text)
                    _emit_log(
                        log_cb,
                        _msg(
                            lang,
                            f"[WEB_SEARCH] DuckDuckGo (noticias) falló: {last_exc_text}",
                            f"[WEB_SEARCH] DuckDuckGo (news) failed: {last_exc_text}",
                        ),
                    )
                    _ddg_news_circuit_record(success=False)

            if len(collected) < max_results and _budget_left() > 1.0:
                needed = max_results - len(collected)
                if _ddg_text_circuit.is_open():
                    _emit_log(
                        log_cb,
                        _msg(
                            lang,
                            "[WEB_SEARCH] DuckDuckGo (resultados web) omitido — falló "
                            "repetidamente esta sesión, en cooldown.",
                            "[WEB_SEARCH] DuckDuckGo (web results) skipped — failed "
                            "repeatedly this session, in cooldown.",
                        ),
                    )
                else:
                    _emit_log(
                        log_cb,
                        _msg(
                            lang,
                            "[WEB_SEARCH] Consultando DuckDuckGo (resultados web)...",
                            "[WEB_SEARCH] Querying DuckDuckGo (web results)...",
                        ),
                    )
                    try:
                        text_items = _call_with_backoff(
                            lambda: list(ddgs.text(clean_q, max_results=needed, region=region)),
                            budget_left_fn=_budget_left,
                            op_name="DDG web",
                            log_cb=log_cb,
                            lang=lang,
                        )
                        add_results(text_items, "Web")
                        _ddg_text_circuit.record(success=True)
                    except Exception as e:
                        last_exc_text = _describe_exception(e)
                        logger.debug("🛡️ [WebSearch] DDG web agotó reintentos: %s", last_exc_text)
                        _emit_log(
                            log_cb,
                            _msg(
                                lang,
                                f"[WEB_SEARCH] DuckDuckGo (web) falló: {last_exc_text}",
                                f"[WEB_SEARCH] DuckDuckGo (web) failed: {last_exc_text}",
                            ),
                        )
                        _ddg_text_circuit.record(success=False)

            if (
                not collected and clean_q != raw_q and _budget_left() > 1.0
                and not _ddg_text_circuit.is_open()
            ):
                _emit_log(
                    log_cb,
                    _msg(
                        lang,
                        "[WEB_SEARCH] Sin resultados, reintentando con la consulta original...",
                        "[WEB_SEARCH] No results, retrying with the original query...",
                    ),
                )
                try:
                    fallback_items = _call_with_backoff(
                        lambda: list(ddgs.text(raw_q, max_results=max_results, region=region)),
                        budget_left_fn=_budget_left,
                        op_name="DDG fallback",
                        log_cb=log_cb,
                        lang=lang,
                    )
                    add_results(fallback_items, "Web-Fallback")
                    _ddg_text_circuit.record(success=True)
                except Exception as e:
                    last_exc_text = _describe_exception(e)
                    logger.debug("🛡️ [WebSearch] DDG fallback agotó reintentos: %s", last_exc_text)
                    _emit_log(
                        log_cb,
                        _msg(
                            lang,
                            f"[WEB_SEARCH] DuckDuckGo (fallback) falló: {last_exc_text}",
                            f"[WEB_SEARCH] DuckDuckGo (fallback) failed: {last_exc_text}",
                        ),
                    )
                    _ddg_text_circuit.record(success=False)

    except Exception as exc:
        last_exc_text = _describe_exception(exc)

    if not collected and _budget_left() > 1.0:
        with contextlib.suppress(Exception):
            html_results = _scrape_duckduckgo_html(
                clean_q,
                max_results,
                timeout=min(_effective_timeout(SEARCH_ENGINE_TIMEOUT_SECONDS), max(1.0, _budget_left())),
                log_cb=log_cb,
                budget_left_fn=_budget_left,
                lang=lang,
            )
            for item in html_results:
                if item.get("url") not in seen_urls:
                    seen_urls.add(item.get("url"))
                    collected.append(item)

    if not collected and last_exc_text:
        global _LAST_SEARCH_ERROR
        _LAST_SEARCH_ERROR = last_exc_text

    _ddg_cache_put(cache_key, collected)
    return collected


def _http_get_json(
    url: str, params: Dict[str, str], timeout: float, log_cb: LogCallback = None,
    lang: Optional[str] = None,
) -> Dict[str, Any]:
    def _do_request() -> Dict[str, Any]:
        if requests is not None:
            resp = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers={
                    "User-Agent": random.choice(_DDG_USER_AGENTS),
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            return resp.json()

        query_string = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{url}?{query_string}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SovNode/2.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)

    return _call_with_backoff(_do_request, attempts=2, op_name="SearXNG", log_cb=log_cb, lang=lang)


def _search_via_searxng(
    clean_q: str,
    max_results: int,
    lang: Optional[str] = None,
    log_cb: LogCallback = None,
) -> List[Dict[str, Any]]:
    """
    Motor SearXNG — corrección del bug real: la versión previa
    referenciaba `domain`, `date` y `source_label` sin definirlos
    (NameError garantizado en cuanto había un resultado). Se calcula
    `domain` desde la URL, `date` desde los campos de metadata que
    SearXNG puede traer (o cadena vacía si no hay), y `type` se fija a
    "SearXNG" en vez de una variable inexistente.
    """
    _emit_log(
        log_cb,
        _msg(lang, "[WEB_SEARCH] Consultando SearXNG...", "[WEB_SEARCH] Querying SearXNG..."),
    )
    data = _http_get_json(
        f"{SEARXNG_ENDPOINT}/search",
        {"q": clean_q, "format": "json", "language": lang or "es"},
        timeout=_effective_timeout(SEARCH_ENGINE_TIMEOUT_SECONDS),
        log_cb=log_cb,
        lang=lang,
    )

    collected: List[Dict[str, Any]] = []
    seen_urls: set = set()

    for item in data.get("results", []):
        url = str(item.get("url") or "").strip()
        if not url or url in seen_urls:
            continue

        domain_score = score_domain(url)
        if domain_score is None:
            continue

        title = clean_headline(str(item.get("title", "")).strip())
        snippet = str(item.get("content", "")).strip()
        if not title or not snippet:
            continue

        seen_urls.add(url)

        domain = urlparse(url).netloc.replace("www.", "")
        date = str(item.get("publishedDate") or item.get("date") or "").strip()

        collected.append({
            "title": title,
            "url": url,
            "domain": domain,
            "snippet": snippet,
            "content": snippet,
            "raw_content": snippet,
            "content_source": "snippet",
            "date": date,
            "type": "SearXNG",
            "score": domain_score,
            "image": item.get("img_src") or item.get("thumbnail") or None,
            "metadata": {
                "authoritative": is_hard_data_source(url),
            },
        })

        if len(collected) >= max_results:
            break

    return collected


# BLINDAJE (bug real, MEDIDO contra la API real, no supuesto): cada
# petición de un solo pageid ronda 0.3-0.5s de red — con 4s de
# presupuesto hay margen real para el rankeo + varios extractos.
_WIKI_RANK_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_WIKI_RANK_CACHE_LOCK = threading.Lock()
_WIKI_EXTRACT_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_WIKI_EXTRACT_CACHE_LOCK = threading.Lock()
WIKI_CACHE_TTL_SECONDS = float(os.getenv("SOVNODE_WIKI_CACHE_TTL", "600"))


def _wiki_cache_get(
    cache: Dict[str, Tuple[float, Any]], lock: threading.Lock, key: str,
) -> Any:
    with lock:
        entry = cache.get(key)
        if entry is None:
            return None
        cached_at, value = entry
        if (time.monotonic() - cached_at) >= WIKI_CACHE_TTL_SECONDS:
            del cache[key]
            return None
        return value


def _wiki_cache_put(
    cache: Dict[str, Tuple[float, Any]], lock: threading.Lock, key: str, value: Any,
) -> None:
    if not value:
        return
    with lock:
        cache[key] = (time.monotonic(), value)


def _suggestion_drops_proper_noun(original_query: str, suggestion: str) -> bool:
    proper_nouns = re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", original_query or "")
    if not proper_nouns:
        return False
    suggestion_lower = (suggestion or "").lower()
    return any(word.lower() not in suggestion_lower for word in proper_nouns)


def wiki_rank_search_candidates(
    wiki_domain: str,
    encoded_q: str,
    rank_limit: int,
    timeout: float,
    log_cb: LogCallback = None,
    budget_left_fn: Optional[Callable[[], float]] = None,
    lang: Optional[str] = None,
) -> List[Dict[str, Any]]:
    cache_key = f"{wiki_domain}|{encoded_q}|{rank_limit}"
    cached = _wiki_cache_get(_WIKI_RANK_CACHE, _WIKI_RANK_CACHE_LOCK, cache_key)
    if cached is not None:
        _emit_log(
            log_cb,
            _msg(
                lang,
                "[WEB_SEARCH] Wikipedia (rankeo): resultado servido desde caché.",
                "[WEB_SEARCH] Wikipedia (ranking): result served from cache.",
            ),
        )
        return cached

    def _rank_url_for(query_text: str) -> str:
        return (
            f"https://{wiki_domain}/w/api.php?action=query&generator=search"
            f"&gsrlimit={rank_limit}&gsrsearch={query_text}&gsrinfo=suggestion&format=json"
        )

    def _do_rank_request(url: str) -> Dict[str, Any]:
        headers = {"User-Agent": random.choice(_DDG_USER_AGENTS)}
        if requests is not None:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    rank_url = _rank_url_for(encoded_q)
    rank_data = _call_with_backoff(
        lambda: _do_rank_request(rank_url), attempts=2, budget_left_fn=budget_left_fn,
        op_name="Wikipedia ranking", log_cb=log_cb, lang=lang,
    )
    rank_pages = (rank_data.get("query") or {}).get("pages") or {}
    candidates = sorted(
        (p for p in rank_pages.values() if isinstance(p, dict) and p.get("pageid")),
        key=lambda p: p.get("index", 999),
    ) if isinstance(rank_pages, dict) else []

    if not candidates:
        suggestion = (
            (rank_data.get("query") or {}).get("searchinfo") or {}
        ).get("suggestion")
        has_budget = budget_left_fn is None or budget_left_fn() > 0.3
        original_query = urllib.parse.unquote(encoded_q)
        if suggestion and _suggestion_drops_proper_noun(original_query, suggestion):
            _emit_log(
                log_cb,
                _msg(
                    lang,
                    f"[WEB_SEARCH] Wikipedia sin candidatos — sugerencia ortográfica "
                    f"{suggestion!r} cambia un nombre propio del original "
                    f"({original_query!r}), se descarta por seguridad.",
                    f"[WEB_SEARCH] Wikipedia had no candidates — spelling suggestion "
                    f"{suggestion!r} changes a proper noun from the original "
                    f"({original_query!r}), discarding it for safety.",
                ),
            )
            suggestion = None
        if suggestion and has_budget:
            _emit_log(
                log_cb,
                _msg(
                    lang,
                    f"[WEB_SEARCH] Wikipedia sin candidatos — reintentando con "
                    f"sugerencia ortográfica: {suggestion!r}",
                    f"[WEB_SEARCH] Wikipedia had no candidates — retrying with "
                    f"spelling suggestion: {suggestion!r}",
                ),
            )
            suggested_url = _rank_url_for(urllib.parse.quote(suggestion))
            suggested_data = _call_with_backoff(
                lambda: _do_rank_request(suggested_url), attempts=1,
                budget_left_fn=budget_left_fn, op_name="Wikipedia ranking (sugerido)",
                log_cb=log_cb, lang=lang,
            )
            suggested_pages = (suggested_data.get("query") or {}).get("pages") or {}
            if isinstance(suggested_pages, dict):
                candidates = sorted(
                    (p for p in suggested_pages.values() if isinstance(p, dict) and p.get("pageid")),
                    key=lambda p: p.get("index", 999),
                )

        if not candidates:
            reason = _msg(
                lang,
                (
                    "sugerencia ortográfica disponible pero sin presupuesto de tiempo "
                    f"restante para perseguirla: {suggestion!r}"
                    if suggestion and not has_budget else
                    "sin sugerencia ortográfica que perseguir"
                ),
                (
                    f"spelling suggestion available but no time budget left "
                    f"to pursue it: {suggestion!r}"
                    if suggestion and not has_budget else
                    "no spelling suggestion to pursue"
                ),
            )
            _emit_log(
                log_cb,
                _msg(
                    lang,
                    f"[WEB_SEARCH] Wikipedia: sin candidatos para "
                    f"{urllib.parse.unquote(encoded_q)!r} ({reason}).",
                    f"[WEB_SEARCH] Wikipedia: no candidates for "
                    f"{urllib.parse.unquote(encoded_q)!r} ({reason}).",
                ),
            )

    _wiki_cache_put(_WIKI_RANK_CACHE, _WIKI_RANK_CACHE_LOCK, cache_key, candidates)
    return candidates


def wiki_fetch_single_extract(
    wiki_domain: str,
    pageid: Any,
    exintro_param: str,
    exchars: int,
    thumb_size: int,
    timeout: float,
    log_cb: LogCallback = None,
    budget_left_fn: Optional[Callable[[], float]] = None,
    lang: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    cache_key = f"{wiki_domain}|{pageid}|{exintro_param}|{exchars}|{thumb_size}"
    cached = _wiki_cache_get(_WIKI_EXTRACT_CACHE, _WIKI_EXTRACT_CACHE_LOCK, cache_key)
    if cached is not None:
        _emit_log(
            log_cb,
            _msg(
                lang,
                f"[WEB_SEARCH] Wikipedia (pageid={pageid}): extracto servido desde caché.",
                f"[WEB_SEARCH] Wikipedia (pageid={pageid}): extract served from cache.",
            ),
        )
        return cached

    extract_url = (
        f"https://{wiki_domain}/w/api.php?action=query&pageids={pageid}"
        f"&prop=extracts%7Cpageimages"
        f"{exintro_param}&explaintext&exchars={exchars}"
        f"&piprop=thumbnail&pithumbsize={thumb_size}&format=json"
    )

    def _do_extract_request() -> Dict[str, Any]:
        headers = {"User-Agent": random.choice(_DDG_USER_AGENTS)}
        if requests is not None:
            resp = requests.get(extract_url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        req = urllib.request.Request(extract_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    data = _call_with_backoff(
        _do_extract_request, attempts=1, budget_left_fn=budget_left_fn,
        op_name="Wikipedia extract", log_cb=log_cb, lang=lang,
    )
    pages = (data.get("query") or {}).get("pages") or {}
    page_info = next(iter(pages.values()), None)
    if not isinstance(page_info, dict):
        return None
    title = str(page_info.get("title") or "").strip()
    extract = str(page_info.get("extract") or "").strip()
    if not title or not extract:
        return None
    thumbnail = (page_info.get("thumbnail") or {}).get("source") or None
    result = {"title": title, "extract": extract, "thumbnail": thumbnail}
    _wiki_cache_put(_WIKI_EXTRACT_CACHE, _WIKI_EXTRACT_CACHE_LOCK, cache_key, result)
    return result


def _search_via_wikipedia(
    clean_q: str,
    max_results: int,
    lang: Optional[str] = None,
    log_cb: LogCallback = None,
) -> List[Dict[str, Any]]:
    wiki_domain = "en.wikipedia.org" if lang == "en" else "es.wikipedia.org"
    encoded_q = urllib.parse.quote(clean_q)
    wants_full_body = requires_precise_fact(clean_q)
    exintro_param = "" if wants_full_body else "&exintro"
    if wants_full_body and asks_about_final(clean_q):
        exchars = WIKI_FINAL_EXTRACT_CHARS
    elif wants_full_body:
        exchars = WIKI_PRECISE_FACT_EXTRACT_CHARS
    else:
        exchars = WIKI_API_EXTRACT_CHARS

    sequence_start = time.monotonic()

    def _budget_left() -> float:
        return WIKI_SEQUENCE_BUDGET_SECONDS - (time.monotonic() - sequence_start)

    timeout = _effective_timeout(SEARCH_ENGINE_TIMEOUT_SECONDS)
    rank_limit = max(1, min(max_results * 2, 10))

    _emit_log(
        log_cb,
        _msg(
            lang,
            "[WEB_SEARCH] Consultando Wikipedia (motor paralelo)...",
            "[WEB_SEARCH] Querying Wikipedia (parallel engine)...",
        ),
    )
    try:
        candidates = wiki_rank_search_candidates(
            wiki_domain, encoded_q, rank_limit, timeout,
            log_cb=log_cb, budget_left_fn=_budget_left, lang=lang,
        )
    except Exception as exc:
        logger.debug("🛡️ [WebSearch] Wikipedia (motor paralelo) falló: %s", exc)
        _emit_log(
            log_cb,
            _msg(
                lang,
                f"[WEB_SEARCH] Wikipedia también falló: {exc}",
                f"[WEB_SEARCH] Wikipedia also failed: {exc}",
            ),
        )
        return []

    if not candidates:
        _emit_log(
            log_cb,
            _msg(
                lang,
                "[WEB_SEARCH] Wikipedia: sin candidatos para esta consulta.",
                "[WEB_SEARCH] Wikipedia: no candidates for this query.",
            ),
        )
        return []

    collected: List[Dict[str, Any]] = []
    for candidate in candidates:
        if len(collected) >= max_results:
            break
        if _budget_left() <= 0.3:
            break

        pageid = candidate.get("pageid")
        try:
            page = wiki_fetch_single_extract(
                wiki_domain, pageid, exintro_param, exchars, WIKI_API_THUMB_SIZE,
                timeout, log_cb=log_cb, budget_left_fn=_budget_left, lang=lang,
            )
        except Exception as exc:
            logger.debug("Extracto de Wikipedia (pageid=%s) falló: %s", pageid, exc)
            continue
        if page is None or len(page["extract"]) < 30:
            continue

        url = f"https://{wiki_domain}/wiki/{urllib.parse.quote(page['title'])}"
        domain_score = score_domain(url)
        if domain_score is None:
            continue
        collected.append({
            "title": page["title"],
            "url": url,
            "domain": wiki_domain,
            "snippet": page["extract"],
            "content": page["extract"],
            "raw_content": page["extract"],
            "content_source": "wikipedia_api",
            "date": "",
            "type": "Wikipedia",
            "score": domain_score,
            "image": page["thumbnail"],
            "metadata": {
                "pageid": pageid,
                "authoritative": True,
                "title_names_final": title_names_the_final(page["title"]),
            },
        })

    _emit_log(
        log_cb,
        _msg(
            lang,
            f"[WEB_SEARCH] Wikipedia: {len(collected)} resultado(s) recuperado(s).",
            f"[WEB_SEARCH] Wikipedia: {len(collected)} result(s) retrieved.",
        ),
    )
    return collected


# =====================================================================
# ORQUESTACIÓN MULTIHILO CON TECHO GLOBAL
# =====================================================================


def _collect_raw_results(
    clean_q: str,
    raw_q: str,
    max_results: int,
    lang: Optional[str] = None,
    log_cb: LogCallback = None,
) -> List[Dict[str, Any]]:
    global _LAST_SEARCH_ERROR

    engine_calls: List[Tuple[str, Any, tuple, float]] = []
    if SEARCH_BACKEND == "searxng" or SEARXNG_AUXILIARY_ENABLED:
        engine_calls.append((
            "SearXNG", _search_via_searxng, (clean_q, max_results, lang, log_cb),
            _effective_timeout(SEARCH_ENGINE_TIMEOUT_SECONDS),
        ))
    engine_calls.append((
        "DuckDuckGo", _search_via_duckduckgo, (clean_q, raw_q, max_results, lang, log_cb),
        DDG_SEQUENCE_BUDGET_SECONDS,
    ))
    engine_calls.append((
        "Wikipedia", _search_via_wikipedia, (clean_q, max_results, lang, log_cb),
        WIKI_SEQUENCE_BUDGET_SECONDS,
    ))

    collected: List[Dict[str, Any]] = []
    seen_urls: set = set()
    start = time.monotonic()

    effective_global_timeout = max(
        _effective_timeout(GLOBAL_SEARCH_TIMEOUT_SECONDS),
        max(budget for *_, budget in engine_calls) + 1.0,
    )

    with ThreadPoolExecutor(
        max_workers=max(1, len(engine_calls)), thread_name_prefix="WebSearchOrchestrator"
    ) as pool:
        future_to_name = {
            pool.submit(
                safe_execute, fn, *args, timeout_sec=guard_timeout, engine_name=name
            ): name
            for name, fn, args, guard_timeout in engine_calls
        }
        try:
            for future in as_completed(future_to_name, timeout=effective_global_timeout):
                name = future_to_name[future]
                try:
                    items = future.result(timeout=0.1) or []
                except Exception as exc:
                    logger.warning("🛡️ [WebSearch-Orchestrator] '%s' no entregó resultados: %s", name, exc)
                    items = []
                for item in items:
                    url = item.get("url")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    collected.append(item)
                _emit_log(
                    log_cb,
                    _msg(
                        lang,
                        f"[WEB_SEARCH] {name}: {len(items)} resultado(s) recibido(s).",
                        f"[WEB_SEARCH] {name}: {len(items)} result(s) received.",
                    ),
                )
        except FuturesTimeoutError:
            elapsed = time.monotonic() - start
            msg = _msg(
                lang,
                f"Timeout global de búsqueda ({effective_global_timeout:.1f}s) alcanzado tras {elapsed:.1f}s.",
                f"Global search timeout ({effective_global_timeout:.1f}s) reached after {elapsed:.1f}s.",
            )
            logger.warning("⏱️ [WebSearch-Orchestrator] %s", msg)
            _emit_log(log_cb, f"[WEB_SEARCH] {msg}")
            if not collected:
                _LAST_SEARCH_ERROR = msg

    return collected


# =====================================================================
# EXTRACCIÓN Y ENRIQUECIMIENTO RÁPIDO
# =====================================================================
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ0-9¡¿\"'])")
_SENTENCE_POSITION_DECAY_LAMBDA = 0.08

_MARKDOWN_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)


def _strip_markdown_tables(text: str) -> str:
    if "|" not in text:
        return text
    lines = [
        line for line in text.splitlines()
        if not _MARKDOWN_TABLE_ROW_RE.match(line)
    ]
    return "\n".join(lines)


_RESULT_HEADER_TERMS_RE: Pattern[str] = re.compile(
    r"\b("
    r"final|score|scored|scoreline|result|penalty\s+shoot-?out|shoot-?out|"
    r"penalt(?:y|ies)|won|winner|champion(?:s)?|title|"
    r"resultado|marcador|goles?|tanda\s+de\s+penales|penales|"
    r"gan[oó]|ganador(?:es)?|campe[oó]n(?:es)?|t[ií]tulo|"
    r"defeated|beat|derrot[oó]|venci[oó]"
    r")\b",
    re.IGNORECASE,
)
_RESULT_HEADER_TERM_BOOST: float = 1.8


def _extract_relevant_sentences(text: str, query: str, max_chars: int) -> str:
    # Solo eliminar tablas si NO se están pidiendo datos puntuales o finales deportivas
    if not (requires_precise_fact(query) or asks_about_final(query)):
        text = _strip_markdown_tables((text or "").strip()).strip()
    else:
        text = (text or "").strip()
        
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) < 2:
        return text[:max_chars].rstrip()

    query_words = _significant_words(query) or set()
    final_aware = asks_about_final(query)
    wants_precise_fact = requires_precise_fact(query)

    scored = []
    cursor = 0
    for idx, sentence in enumerate(sentences):
        start = text.find(sentence, cursor)
        if start == -1:
            start = cursor
        end = start + len(sentence)
        cursor = end

        overlap = _keyword_overlap(query, sentence)
        keyword_factor = (1.0 + (overlap / max(1, len(query_words)))) if query_words else 1.0
        position_factor = math.exp(-_SENTENCE_POSITION_DECAY_LAMBDA * idx)

        round_factor = 1.0
        if final_aware:
            if round_context_matches_final(text, start, end):
                round_factor = 3.0
            elif mentions_non_final_round(sentence):
                round_factor = 0.15

        header_term_factor = (
            _RESULT_HEADER_TERM_BOOST
            if wants_precise_fact and _RESULT_HEADER_TERMS_RE.search(sentence)
            else 1.0
        )

        scored.append((
            idx, sentence,
            keyword_factor * position_factor * round_factor * header_term_factor,
        ))

    scored.sort(key=lambda item: item[2], reverse=True)

    selected_indices = []
    budget = max_chars
    for idx, sentence, _score in scored:
        cost = len(sentence) + 1
        if cost <= budget:
            selected_indices.append(idx)
            budget -= cost

    if not selected_indices:
        return text[:max_chars].rstrip()

    selected_indices.sort()
    return " ".join(sentences[i] for i in selected_indices)


def _fetch_article_content_and_image(url: str, query: str = "") -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {"text": None, "image": None}
    if not TRAFILATURA_AVAILABLE or not url:
        return result
    try:
        downloaded = trafilatura.fetch_url(url, config=_TRAFILATURA_CONFIG)
        if downloaded:
            # output_format="markdown" + include_formatting=True: preserva
            # encabezados (#/##), listas y negritas del artículo original
            # en vez de aplanar todo a texto plano (el default "txt" de
            # trafilatura pierde esa estructura — medido: un <h2> y un
            # <strong> quedan indistinguibles de un párrafo normal). Sigue
            # descartando <script>/<style>/nav/boilerplate igual que antes
            # — eso es trabajo de trafilatura, no de esta llamada.
            text = trafilatura.extract(
                downloaded,
                include_comments=False,
                favor_precision=True,
                output_format="markdown",
                include_formatting=True,
            )
            if text:
                result["text"] = _extract_relevant_sentences(text.strip(), query, ARTICLE_MAX_CHARS)
    except Exception as exc:
        logger.debug("Error en scraping de %s: %s", url, exc)
    return result


_WIKI_API_EXTRACT_MIN_CHARS = 200


def _has_usable_api_extract(result: Dict[str, Any], query: str = "") -> bool:
    if "wikipedia.org" not in str(result.get("url") or ""):
        return False
    # Si la consulta requiere datos precisos o eventos/partidos, FORZAMOS el scraping
    # para no perder las infoboxes/tablas con marcadores.
    if requires_precise_fact(query) or asks_about_final(query) or is_live_event_query(query):
        return False
    return len(str(result.get("content") or "")) >= _WIKI_API_EXTRACT_MIN_CHARS

def _enrich_with_full_articles(
    results: List[Dict[str, Any]], limit: int, query: str = "", log_cb: LogCallback = None,
    lang: Optional[str] = None,
) -> None:
    if not TRAFILATURA_AVAILABLE or not results:
        return
    scrapeable = [r for r in results if not _has_usable_api_extract(r, query=query)]
    skipped = len(results) - len(scrapeable)
    if skipped:
        _emit_log(
            log_cb,
            _msg(
                lang,
                f"[WEB_SEARCH] {skipped} fuente(s) de Wikipedia omitida(s) del scraping "
                f"(extracto ya provisto por la API).",
                f"[WEB_SEARCH] {skipped} Wikipedia source(s) skipped for scraping "
                f"(extract already provided by the API).",
            ),
        )
    targets = scrapeable[:max(0, limit)]
    if not targets:
        return

    pool = ThreadPoolExecutor(max_workers=min(5, len(targets)), thread_name_prefix="WebSearchScrape")
    future_map = {pool.submit(_fetch_article_content_and_image, r["url"], query): r for r in targets}
    effective_article_timeout = _effective_timeout(ARTICLE_FETCH_TIMEOUT_SECONDS)

    for r in targets:
        _emit_log(
            log_cb,
            _msg(
                lang,
                f"[WEB_SEARCH] Extrayendo HTML de {r.get('url', '')}...",
                f"[WEB_SEARCH] Extracting HTML from {r.get('url', '')}...",
            ),
        )

    try:
        for future in as_completed(future_map, timeout=effective_article_timeout):
            result = future_map[future]
            try:
                extracted = future.result(timeout=0.1)
                article_text = extracted.get("text")
                if article_text and len(article_text) > len(result.get("snippet", "")):
                    result["raw_content"] = article_text
                    result["content"] = article_text
                    result["content_source"] = "full_article"
                else:
                    result.setdefault(
                        "raw_content",
                        result.get("content") or result.get("snippet") or "",
                    )
            except Exception:
                result.setdefault(
                    "raw_content",
                    result.get("content") or result.get("snippet") or "",
                )
                continue
    except FuturesTimeoutError:
        logger.warning("Scraping acotado por timeout duro de %ss.", effective_article_timeout)
    finally:
        for r in targets:
            r.setdefault(
                "raw_content",
                r.get("content") or r.get("snippet") or "",
            )
        pool.shutdown(wait=False, cancel_futures=True)


# =====================================================================
# PUNTOS DE ENTRADA PRINCIPALES
# =====================================================================
MIN_SOURCE_CONTENT_CHARS: int = 400


def _cap_total_content_chars(results: List[Dict[str, Any]], max_chars: int) -> List[Dict[str, Any]]:
    total = sum(len(r.get("content") or "") for r in results)
    if total <= max_chars:
        return results

    effective_floor = min(MIN_SOURCE_CONTENT_CHARS, max_chars // max(1, len(results)))
    reserved = [min(len(r.get("content") or ""), effective_floor) for r in results]
    budget = max_chars - sum(reserved)

    def _truncate_to(content: str, keep: int) -> str:
        if keep <= 0:
            return ""
        return content[:max(0, keep - 1)].rstrip() + "…"

    if budget <= 0:
        for r, floor in zip(results, reserved):
            content = r.get("content") or ""
            if floor < len(content):
                r["content"] = _truncate_to(content, floor)
        return results

    for r, floor in zip(results, reserved):
        content = r.get("content") or ""
        extra_needed = len(content) - floor
        if extra_needed <= 0:
            continue
        grant = min(extra_needed, budget)
        keep = floor + grant
        budget -= grant
        if keep < len(content):
            r["content"] = _truncate_to(content, keep)
    return results


def search_web(
    query: str,
    max_results: int = 2,
    lang: Optional[str] = None,
    log_cb: LogCallback = None,
) -> List[Dict[str, Any]]:
    global _LAST_SEARCH_ERROR
    _LAST_SEARCH_ERROR = None

    clean_q = sanitize_query(query)
    if not clean_q or not clean_q.strip():
        return []

    raw_q = str(query or "").strip()

    _emit_log(
        log_cb,
        _msg(
            lang,
            f"[WEB_SEARCH] Consultando motor de búsqueda ({SEARCH_BACKEND})...",
            f"[WEB_SEARCH] Querying search engine ({SEARCH_BACKEND})...",
        ),
    )
    try:
        collected = _collect_raw_results(clean_q, raw_q, max_results, lang=lang, log_cb=log_cb)
    except Exception as exc:
        logger.warning("🛡️ [WebSearch] Fallo inesperado en la orquestación: %s", exc)
        _LAST_SEARCH_ERROR = _LAST_SEARCH_ERROR or str(exc)
        _emit_log(
            log_cb,
            _msg(
                lang,
                f"[WEB_SEARCH] Error inesperado en la búsqueda: {exc}",
                f"[WEB_SEARCH] Unexpected error during search: {exc}",
            ),
        )
        return []

    if not collected:
        _emit_log(
            log_cb,
            _msg(
                lang,
                "[WEB_SEARCH] Sin resultados de ningún motor.",
                "[WEB_SEARCH] No results from any engine.",
            ),
        )
        return []

    cleaned: List[Dict[str, Any]] = []
    seen_urls: set = set()
    for item in collected:
        url = str(item.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        item["title"] = _clean_text(item.get("title"))
        item["snippet"] = _clean_text(item.get("snippet"))
        item["content"] = _clean_text(item.get("content")) or item["snippet"]
        item.setdefault("raw_content", item.get("content") or item.get("snippet") or "")
        if item["title"] and item["snippet"]:
            cleaned.append(item)

    cleaned = _filter_relevance(cleaned, clean_q, log_cb=log_cb, lang=lang)
    if not cleaned:
        _emit_log(
            log_cb,
            _msg(
                lang,
                "[WEB_SEARCH] Ningún resultado pasó el filtro de relevancia.",
                "[WEB_SEARCH] No result passed the relevance filter.",
            ),
        )
        return []

    if requires_precise_fact(clean_q):
        wants_final = asks_about_final(clean_q)
        cleaned.sort(
            key=lambda r: (
                wants_final and title_names_the_final(r.get("title", "")),
                is_hard_data_source(r.get("url", "")),
                r.get("score", 1.0),
            ),
            reverse=True,
        )
        if cleaned and is_hard_data_source(cleaned[0].get("url", "")):
            _emit_log(
                log_cb,
                _msg(
                    lang,
                    "[WEB_SEARCH] Consulta de dato puntual: se prioriza fuente estructurada "
                    f"({urlparse(cleaned[0].get('url', '')).netloc}).",
                    "[WEB_SEARCH] Precise-fact query: prioritizing structured source "
                    f"({urlparse(cleaned[0].get('url', '')).netloc}).",
                ),
            )
        if wants_final and cleaned and title_names_the_final(cleaned[0].get("title", "")):
            _emit_log(
                log_cb,
                _msg(
                    lang,
                    "[WEB_SEARCH] Consulta sobre la final: se prioriza la fuente que nombra "
                    f"la final en su título ({cleaned[0].get('title', '')!r}).",
                    "[WEB_SEARCH] Query about the final: prioritizing the source that "
                    f"names the final in its title ({cleaned[0].get('title', '')!r}).",
                ),
            )

        # BLINDAJE (bug real, MEDIDO — "the final of world cup 2022"
        # trajo "2022 FIFA World Cup final", "2022 FIFA World Cup" Y
        # "2022 Men's T20 World Cup" como fuentes — la final de
        # CRICKET, un torneo totalmente distinto): "world"/"cup"/
        # "final" son palabras GENÉRICAS a propósito (ver
        # _GENERIC_CONTEXT_WORDS) para no exigir de más en la mayoría
        # de los casos, pero "World Cup" es un término que decenas de
        # deportes distintos usan para su propio torneo — sin ninguna
        # palabra de la consulta que indique el deporte (ni "FIFA" ni
        # "fútbol" ni "soccer"), `is_topically_relevant()` ya aceptó
        # la fuente de cricket arriba (comparte "world"+"cup" con la
        # consulta, que es todo lo que exige). El modelo, al sintetizar
        # sobre fuentes de DOS torneos distintos mezcladas como si
        # fueran la misma, terminó respondiendo sobre el Mundial de
        # Cricket T20 en vez del Mundial de fútbol.
        #
        # Esta pasada es la protección real: una vez ordenado `cleaned`
        # (la fuente #1 ya es la más confiable, con el boost de
        # `title_names_the_final` arriba), cualquier fuente SIGUIENTE
        # que no comparta NINGUNA palabra distintiva (no genérica, ver
        # `distinctive_words()`) con la fuente #1 se descarta — no
        # aporta corroboración del MISMO evento, aporta contaminación
        # de un evento homónimo pero distinto. `anchor_words` vacío
        # (fuente #1 sin nada distintivo más allá de "world cup") no
        # filtra nada — sin ancla real, no hay con qué comparar.
        if len(cleaned) >= 2:
            def _source_blob(item: Dict[str, Any]) -> str:
                return f"{item.get('title', '')} {item.get('snippet') or item.get('content') or ''}"

            anchor_words = distinctive_words(_source_blob(cleaned[0]))
            if anchor_words:
                consistent = [cleaned[0]]
                dropped_titles = []
                for item in cleaned[1:]:
                    item_words = distinctive_words(_source_blob(item))
                    if not item_words or (item_words & anchor_words):
                        consistent.append(item)
                    else:
                        dropped_titles.append(item.get("title", "(sin título)"))
                if dropped_titles:
                    _emit_log(
                        log_cb,
                        _msg(
                            lang,
                            f"[WEB_SEARCH] {len(dropped_titles)} fuente(s) descartada(s) por no "
                            f"compartir tema con la fuente principal ({cleaned[0].get('title', '')!r}): "
                            f"{dropped_titles[:5]}",
                            f"[WEB_SEARCH] {len(dropped_titles)} source(s) discarded for not "
                            f"sharing topic with the primary source ({cleaned[0].get('title', '')!r}): "
                            f"{dropped_titles[:5]}",
                        ),
                    )
                    cleaned = consistent
    elif is_live_event_query(clean_q):
        def _live_event_sort_key(r: Dict[str, Any]) -> float:
            base = r.get("score", 1.0)
            if _is_slow_moving_encyclopedic(r.get("url", "")):
                return base * _LIVE_EVENT_ENCYCLOPEDIC_DAMPING
            return base

        cleaned.sort(key=_live_event_sort_key, reverse=True)
    else:
        cleaned.sort(key=lambda r: r.get("score", 1.0), reverse=True)
    top_results = cleaned[:max_results]

    _emit_log(
        log_cb,
        _msg(
            lang,
            f"[WEB_SEARCH] {len(top_results)} fuente(s) seleccionada(s), extrayendo contenido completo...",
            f"[WEB_SEARCH] {len(top_results)} source(s) selected, extracting full content...",
        ),
    )
    with contextlib.suppress(Exception):
        _enrich_with_full_articles(
            top_results, limit=MAX_ARTICLES_TO_SCRAPE, query=clean_q, log_cb=log_cb, lang=lang,
        )

    for r in top_results:
        if not r.get("image"):
            with contextlib.suppress(Exception):
                r["image"] = _favicon_url(r.get("url", ""))

    effective_context_cap = MAX_TOTAL_CONTEXT_CHARS
    if requires_precise_fact(clean_q):
        effective_context_cap = max(
            MAX_TOTAL_CONTEXT_CHARS, WIKI_PRECISE_FACT_EXTRACT_CHARS + 500,
        )
        if asks_about_final(clean_q):
            effective_context_cap = max(
                effective_context_cap, WIKI_FINAL_EXTRACT_CHARS + 500,
            )
    top_results = _cap_total_content_chars(top_results, effective_context_cap)
    _emit_log(
        log_cb,
        _msg(
            lang,
            f"[WEB_SEARCH] Búsqueda completa: {len(top_results)} fuente(s) lista(s) para el modelo.",
            f"[WEB_SEARCH] Search complete: {len(top_results)} source(s) ready for the model.",
        ),
    )

    return top_results


def format_search_results(results: List[Dict[str, Any]], max_results: int = 2) -> str:
    if not results:
        return ""
    formatted_context = ["--- RESULTADOS DE BÚSQUEDA WEB EN TIEMPO REAL ---"]
    for i, r in enumerate(results[:max_results], 1):
        try:
            date_str = f" ({r.get('date')})" if r.get("date") else ""
            body = r.get("content") or r.get("snippet") or ""
            title = r.get("title") or "(sin título)"
            url = r.get("url") or ""
            formatted_context.append(f"[{i}] {title}{date_str}\nFuente: {url}\nResumen: {body}\n")
        except Exception as exc:
            logger.debug("Resultado #%d omitido por formato inesperado: %s", i, exc)
            continue
    return "\n".join(formatted_context)


def search_web_context(
    query: str, max_results: int = 2, lang: Optional[str] = None, log_cb: LogCallback = None
) -> str:
    try:
        results = search_web(query, max_results=max_results, lang=lang, log_cb=log_cb)
        _emit_log(
            log_cb,
            _msg(
                lang,
                "[WEB_SEARCH] Formateando contexto para el modelo...",
                "[WEB_SEARCH] Formatting context for the model...",
            ),
        )
        return format_search_results(results, max_results=max_results)
    except Exception as exc:
        logger.warning("🛡️ [WebSearch] search_web_context degradado: %s", exc)
        return ""


def search(
    query: str,
    max_results: int = 2,
    lang: Optional[str] = None,
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    safe_query = str(query or "").strip()

    try:
        clean_q = sanitize_query(query)
    except Exception as exc:
        logger.warning("🛡️ [WebSearch] sanitize_query falló en search(): %s", exc)
        return {"results": [], "status": "failed", "query": safe_query}

    if not clean_q or not clean_q.strip():
        return {"results": [], "status": "failed", "query": safe_query}

    try:
        raw_results = search_web(query, max_results=max_results, lang=lang, log_cb=log_cb)
    except Exception as exc:
        logger.warning("🛡️ [WebSearch] search() no pudo completar: %s", exc)
        return {"results": [], "status": "failed", "query": clean_q}

    standardized: List[Dict[str, Any]] = []
    for r in raw_results:
        try:
            standardized.append({
                "title": str(r.get("title") or ""),
                "snippet": str(r.get("content") or r.get("snippet") or ""),
                "url": str(r.get("url") or ""),
                "source": str(r.get("domain") or r.get("type") or "web"),
                "score": float(r.get("score") or 1.0),
            })
        except Exception as exc:
            logger.debug("Resultado omitido al normalizar salida de search(): %s", exc)
            continue

    status = "success" if standardized else "degraded"
    return {"results": standardized, "status": status, "query": clean_q}


def get_web_results(
    query: str,
    max_results: int = 2,
    lang: Optional[str] = None,
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    return search(query, max_results=max_results, lang=lang, log_cb=log_cb)