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
orchestrator.py — Orquestador local con blindaje anti-evasión, observabilidad, trazabilidad WAL, MemoryGraph SQLite/FTS5 y Function Calling Autónomo.
"""

from __future__ import annotations
from verification import (
    ScorePhase,
    build_raw_evidence_text,
    classify_score_phase,
    verify_scores,
)
import ast
import contextlib
import difflib
import hashlib
import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pipeline import PipelineEvent, EventType
import requests
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Pattern, Set, Tuple
from tools import (
    LocalToolDispatcher,
    TOOLS_SCHEMA,
    extract_thought_code_blocks,
    format_sandbox_verification,
)
from robust_json_parser import RobustJSONParser
from memory_graph import MemoryGraph
from rag_faiss import LocalVectorRAG
from dynamic_tool_engine import DynamicToolEngine
# Logger unificado para el orquestador
logger = logging.getLogger("SovNode.Orchestrator")

# En consolas Windows (cp1252) los prints con emoji lanzan UnicodeEncodeError
# y matan hilos como CognitiveGovernor. Forzamos UTF-8 si el stream lo permite.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    requests = None
    HTTPAdapter = None

try:
    from spellchecker import SpellChecker
except ImportError:
    SpellChecker = None

from wal import KnowledgeNode, WriteAheadLog  # type: ignore

from cas_sandbox import (  # type: ignore
    CASEngine,
    ExecutionSandbox,
    LogicalCoherenceValidator,
    LogicalStatus,
)


try:
    from fuzzer import (  # type: ignore
        AdversarialFuzzer,
        FuzzingResult,
        FuzzingVerdict,
        SearchRequirement,
    )
except ImportError:

    class FuzzingVerdict(str, Enum):
        ROBUST = "robust"
        FRAGILE = "fragile"
        CRITICALLY_FRAGILE = "critically_fragile"

    @dataclass(frozen=True)
    class SearchRequirement:
        query: str = ""
        justification: str = ""
        priority: int = 2

    @dataclass(frozen=True)
    class FuzzingResult:
        premise: str
        verdict: FuzzingVerdict
        summary: str = ""
        attempts: int = 1
        search_requirements: Tuple[SearchRequirement, ...] = field(
            default_factory=tuple
        )

        def __str__(self) -> str:
            return (
                f"[FUZZ:{self.verdict.value.upper()}] "
                f"attempts={self.attempts} | {self.summary}"
            )

    _HEDGE_RE: Pattern[str] = re.compile(
        r"\b("
        r"quiz[aá]s|tal\s+vez|probablemente|no\s+estoy\s+seguro|"
        r"podr[ií]a\s+ser|posiblemente|se\s+dice\s+que|dicen\s+que"
        r")\b",
        re.IGNORECASE,
    )

    class AdversarialFuzzer:
        def audit(self, premise: str) -> FuzzingResult:
            if _HEDGE_RE.search(premise or ""):
                return FuzzingResult(
                    premise=premise,
                    verdict=FuzzingVerdict.FRAGILE,
                    summary=(
                        "Se detectaron marcadores de incertidumbre sin "
                        "evidencia externa adjunta."
                    ),
                    search_requirements=(
                        SearchRequirement(
                            query=premise[:180],
                            justification=(
                                "La hipótesis requiere evidencia externa "
                                "antes de aceptarse."
                            ),
                            priority=1,
                        ),
                    ),
                )

            return FuzzingResult(
                premise=premise,
                verdict=FuzzingVerdict.ROBUST,
                summary=(
                    "Sin vulnerabilidades detectadas por la heurística "
                    "de respaldo."
                ),
            )


try:
    from lsc_engine import LSCInferenceEngine  # type: ignore
except ImportError:

    class LSCResult:
        def __init__(self, conclusion: str) -> None:
            self.conclusion = conclusion

        def __str__(self) -> str:
            return f"[LSC] {self.conclusion}"

    class LSCInferenceEngine:
        def infer(self, text: str) -> LSCResult:
            return LSCResult(
                "Estructura lógico-sistémica detectada; el motor LSC "
                f"dedicado no está disponible. Entrada: {text[:180]}"
            )


try:
    from web_search import (  # type: ignore
        search_web_context,
        sanitize_query,
        search_web,
        format_search_results,
    )
except ImportError:

    def search_web_context(query: str, max_results: int = 4, lang: Optional[str] = None) -> Optional[str]:
        return None

    def sanitize_query(query: str) -> str:
        return (query or "").strip()

    def search_web(query: str, max_results: int = 4, lang: Optional[str] = None) -> List[Dict[str, Any]]:
        return []

    def format_search_results(results: List[Dict[str, Any]], max_results: int = 4) -> str:
        return ""

try:
    from relevance import (
        YEAR_RE as _rel_YEAR_RE,
        asks_about_final,
        contexts_describe_same_event,
        distinctive_words,
        drop_non_final_round_events,
        entities_supported_by_context,
        extract_query_matchup_entities,
        extract_score_events,
        mentions_non_final_round,
        round_context_matches_final,
        score_context_matches_query_entities,
        extract_score_patterns as _rel_extract_score_patterns,
        extract_years as _rel_extract_years,
        needs_strict_relevance,
        requires_precise_fact,
    )
except ImportError:
    _rel_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

    def _rel_extract_years(text: str) -> Set[str]:
        return set(_rel_YEAR_RE.findall(text or ""))

    _rel_SCORE_RE = re.compile(r"\b\d{1,3}\s*[-–—:]\s*\d{1,3}\b")

    def _rel_extract_score_patterns(text: str) -> Set[str]:
        return {re.sub(r"\s+", "", m) for m in _rel_SCORE_RE.findall(text or "")}

    def extract_score_events(text: str, window: int = 60) -> List[Dict[str, str]]:
        return [
            {"score": re.sub(r"\s+", "", m.group()), "context": text[max(0, m.start() - window):m.end() + window]}
            for m in _rel_SCORE_RE.finditer(text or "")
        ]

    def drop_non_final_round_events(
        events: List[Dict[str, str]], query: str, full_text: str = "",
    ) -> List[Dict[str, str]]:
        return events

    def contexts_describe_same_event(query: str, context_a: str, context_b: str) -> bool:
        return True

    def distinctive_words(text: str) -> Set[str]:
        return set()

    def entities_supported_by_context(entities: Set[str], context: str) -> bool:
        return True

    def extract_query_matchup_entities(query: str, fallback_evidence: str = "") -> Set[str]:
        return set()

    def score_context_matches_query_entities(
        query_entities: Set[str], context: str, min_required: int = 2,
    ) -> bool:
        return True

    def asks_about_final(query: str) -> bool:
        return False

    def mentions_non_final_round(text: str) -> bool:
        return False

    def round_context_matches_final(full_text: str, abs_start: int = 0, abs_end: int = 0) -> bool:
        return True

    def needs_strict_relevance(query: str) -> bool:
        return False

    def requires_precise_fact(query: str) -> bool:
        return False

try:
    from embeddings import (  # type: ignore
        EMBEDDING_MODE_HASH_FALLBACK,
        EMBEDDING_MODE_SEMANTIC,
        EMBEDDING_MODE_UNAVAILABLE,
        get_embedding,
        get_embedding_with_mode,
        prewarm_local_embedding_model,
    )
except ImportError:
    EMBEDDING_MODE_SEMANTIC = "semantic"
    EMBEDDING_MODE_HASH_FALLBACK = "hash_fallback"
    EMBEDDING_MODE_UNAVAILABLE = "unavailable"

    def get_embedding(text: str, dim: int = 384) -> Optional[List[float]]:
        return None

    def get_embedding_with_mode(
        text: str, dim: int = 384,
    ) -> Tuple[Optional[List[float]], str]:
        return None, EMBEDDING_MODE_UNAVAILABLE

    def prewarm_local_embedding_model() -> None:
        return None


try:
    from router import (  # type: ignore
        IntentRouter,
        RoutePath,
        RoutingDecision,
        SignalTag,
    )
except ImportError:

    class RoutePath(str, Enum):
        FAST_PATH = "fast_path"
        SLOW_PATH = "slow_path"

    class SignalTag(str, Enum):
        EMPTY_INPUT = "empty_input"
        MATH_EXPRESSION = "math_expression"
        CODE_COMPLEX = "code_complex"
        LOGIC_AUDIT = "logic_audit"
        CONCEPTUAL_DENSE = "conceptual_dense"
        LSC_INFERENCE = "lsc_inference"
        WEB_SEARCH_INTENT = "web_search_intent"
        CONVERSATIONAL_FOLLOWUP = "conversational_followup"

    @dataclass(frozen=True)
    class RoutingDecision:
        path: RoutePath
        tags: Tuple[SignalTag, ...]
        score: float
        reason: str
        elapsed_ms: float
        text_length: int

    class IntentRouter:
        def classify(self, text: str) -> RoutingDecision:
            normalized = (text or "").strip()

            if not normalized:
                return RoutingDecision(
                    path=RoutePath.FAST_PATH,
                    tags=(SignalTag.EMPTY_INPUT,),
                    score=-5.0,
                    reason="Entrada vacía.",
                    elapsed_ms=0.0,
                    text_length=0,
                )

            tags: List[SignalTag] = []
            score = 0.0
            lowered = normalized.lower()
            code_detected = bool(
                re.search(
                    r"\b(implementa|refactoriza|depura|c[oó]digo|python|"
                    r"phyton|pyton|pygame|script)\b",
                    lowered,
                )
            )

            if re.search(r"[\d\w\)]\s*[\+\-\*/\^]\s*[\d\w\(]", normalized):
                tags.append(SignalTag.MATH_EXPRESSION)
                score += 3.0

            if re.search(r"\b(simplifica|resuelve|ecuaci[oó]n|derivada|integral)\b", lowered):
                if SignalTag.MATH_EXPRESSION not in tags:
                    tags.append(SignalTag.MATH_EXPRESSION)
                score += 3.0

            if code_detected:
                tags.append(SignalTag.CODE_COMPLEX)
                score += 3.0

            if re.search(r"\b(demuestra|audita|falacia|contradicci[oó]n)\b", lowered):
                tags.append(SignalTag.LOGIC_AUDIT)
                score += 4.0

            if re.search(r"\b(teor[ií]a|axioma|hip[oó]tesis|paradigma|ontolog[ií]a)\b", lowered):
                tags.append(SignalTag.CONCEPTUAL_DENSE)
                score += 3.0

            # Mismo blindaje que router.py: un seguimiento conversacional
            # simple ("explain more about that match") no debe activar
            # WEB_SEARCH_INTENT solo por una palabra clave de actualidad,
            # si no hay ya una señal de razonamiento complejo detectada.
            complex_reasoning_present = bool(
                {SignalTag.MATH_EXPRESSION, SignalTag.LOGIC_AUDIT, SignalTag.CODE_COMPLEX} & set(tags)
            )
            followup_detected = not complex_reasoning_present and bool(
                re.search(
                    r"\b(explain\s+more|tell\s+me\s+more|elaborate\s+on|expand\s+on|"
                    r"explica\w*\s+m[aá]s|cu[eé]ntame\s+m[aá]s|dime\s+m[aá]s|profundiza\w*|ampl[ií]a\w*)\b",
                    lowered,
                )
            )
            if followup_detected:
                tags.append(SignalTag.CONVERSATIONAL_FOLLOWUP)
                score -= 3.0

            if not code_detected and not followup_detected and re.search(
                r"\b(hoy|noticia|resultado|precio|internet|busca|search|find|latest|news|who won|world cup|final|2026)\b", lowered
            ):
                tags.append(SignalTag.WEB_SEARCH_INTENT)
                score += 2.5

            path = RoutePath.SLOW_PATH if score >= 1.5 else RoutePath.FAST_PATH

            return RoutingDecision(
                path=path,
                tags=tuple(tags),
                score=score,
                reason="Router de respaldo en uso.",
                elapsed_ms=0.0,
                text_length=len(normalized),
            )


EngineResult = Any

_WORD_SPLIT_RE: Pattern[str] = re.compile(r"\W+", re.UNICODE)
_MULTI_SPACE_RE: Pattern[str] = re.compile(r"\s{2,}")

# Blindaje determinista compartido: detecta años explícitos de 4 dígitos
# (1900-2099) en cualquier texto. Es la base tanto del cortocircuito de
# auto-contención en _build_contextual_search_query (Capa 2) como del
# aviso de desajuste año-evidencia en build_year_mismatch_warning() -
# ambos abordan la misma clase de fallo real observado: un mensaje que
# ya trae su propio año explícito ("world cup 2022 final") termina
# perdiéndolo por sobre-priorizar el contexto conversacional reciente
# (saturado de un año DISTINTO), y el modelo termina fusionando el año
# pedido con hechos de otro año.
#
# La definición vive en relevance.py - junto al resto del criterio de
# relevancia consulta-vs-evidencia que comparten esta capa, web_search.py
# y el gate de Wikipedia en sovnode_qt.py. Estos alias mantienen los
# nombres privados que ya usa el resto del archivo.
_YEAR_RE: Pattern[str] = _rel_YEAR_RE
_extract_years = _rel_extract_years


# Heurística ligera de detección de idioma por solapamiento de palabras
# funcionales (stopwords) - deliberadamente simple (no es NLP real),
# mismo principio ya usado en sovnode_qt.py (_detect_query_language_
# confident) sin poder cross-importar entre ambos módulos: sovnode_qt.py
# importa de orchestrator.py, nunca al revés.
#
# Nota (mirroring de idioma - "Search on internet..." con la UI en
# Español devolvía respuesta en español): antes, _get_base_system_prompt()
# se construía siempre a partir de self.current_language (el selector de
# idioma de la UI), nunca del idioma real del prompt de este turno - así
# que un mensaje en inglés con la UI en Español terminaba con "Responde
# siempre EN ESPAÑOL" (texto de rol, cerca del inicio del system prompt)
# contradiciendo directamente a LANG_ENFORCE_DIRECTIVE (texto añadido al
# final del mismo system prompt) en la misma llamada - dos instrucciones
# opuestas compitiendo por la atención de un modelo local de 3B/7B. Esta
# función es la pieza que resuelve esa contradicción en el origen: el
# idioma real del prompt actual, no el de sesión, decide qué copia de
# _get_base_system_prompt() se usa (ver Orchestrator._resolve_turn_language).
_EN_STOPWORDS: frozenset = frozenset({
    "the", "is", "are", "was", "were", "what", "who", "how", "where",
    "when", "why", "and", "of", "in", "on", "for", "with", "that",
    "this", "did", "does", "will", "would", "can", "could", "last",
})
_ES_STOPWORDS: frozenset = frozenset({
    "el", "la", "los", "las", "es", "son", "era", "eran", "qué", "que",
    "quién", "quien", "cómo", "como", "dónde", "donde", "cuándo",
    "cuando", "por", "para", "con", "del", "y", "en", "última",
    "último", "fue", "fueron",
})
_LANG_WORD_RE: Pattern[str] = re.compile(r"[a-záéíóúñ]+")


def _detect_prompt_language(text: str) -> Optional[str]:
    """
    Heurística determinista (sin llamada al modelo, sin costo de
    inferencia) para detectar si `text` está escrito en inglés o
    español, por solapamiento de palabras funcionales cortas y muy
    frecuentes. Devuelve "English", "Spanish", o `None` si la señal es
    ambigua (texto corto, sin stopwords reconocibles, o un empate real
    entre ambos idiomas) — un `None` deliberadamente NO fuerza ningún
    idioma, para que el llamador conserve el idioma de sesión como
    fallback en vez de adivinar a ciegas.
    """
    words = set(_LANG_WORD_RE.findall((text or "").lower()))
    en_hits = len(words & _EN_STOPWORDS)
    es_hits = len(words & _ES_STOPWORDS)
    if en_hits == es_hits:
        return None
    return "English" if en_hits > es_hits else "Spanish"


# Punto de fallo 5 (bug observado): "Now search the last one in
# China" - un mensaje de seguimiento cuyo sujeto real ("earthquake")
# vive en el turno anterior, referenciado aquí solo como "the last
# one". Ninguna heurística existente lo detectaba: "one" no está en
# deictic_tokens, "last"/"now" no disparan starts_with_continuation, y
# el mensaje tiene 6 palabras (no < 5) - así que needs_context daba
# False y la consulta salía literal ("the last one in China"),
# trayendo resultados sobre apellidos/demografía en vez de terremotos.
_SUBSTITUTION_PATTERN_RE: Pattern[str] = re.compile(
    r"\b(the\s+last\s+one|the\s+same\s+one|another\s+one|one\s+more|"
    r"el\s+[uú]ltimo|la\s+[uú]ltima|el\s+mismo|la\s+misma|"
    r"otro|otra|uno\s+similar|uno\s+igual|una\s+similar)\b",
    re.IGNORECASE,
)

# Diccionario de sustantivos-tema comunes en consultas noticiosas/de
# actualidad - no es NLP real, es una lista curada deliberadamente
# amplia para cubrir los temas de seguimiento más frecuentes (eventos,
# desastres, lanzamientos, resultados) sin depender de ninguna
# librería adicional. Extiende (no reemplaza) los `category_nouns` ya
# usados en Capa 2 más abajo.
_TOPIC_CATEGORY_NOUNS: frozenset = frozenset({
    "final", "match", "game", "winner", "score", "result", "player", "team", "price", "event",
    "partido", "resultado", "ganador", "precio", "jugador", "equipo", "campeon", "campeón", "evento",
    "earthquake", "terremoto", "storm", "tormenta", "hurricane", "huracán", "huracan",
    "election", "elección", "eleccion", "war", "guerra", "attack", "ataque",
    "accident", "accidente", "fire", "incendio", "flood", "inundación", "inundacion",
    "movie", "película", "pelicula", "book", "libro", "song", "canción", "cancion",
    "album", "álbum", "product", "producto", "phone", "teléfono", "telefono",
    "launch", "lanzamiento", "update", "actualización", "actualizacion",
    "version", "versión", "news", "noticia", "flight", "vuelo",
    "protest", "protesta", "scandal", "escándalo", "escandalo",
    "outbreak", "brote", "crisis", "summit", "cumbre",
})

# Palabras a ignorar al extraer sustantivos "a mano" del último turno
# del usuario (último recurso si ni la sustitución por diccionario ni
# la minillamada al LLM lograron identificar el sujeto).
#
# Nota (bug relacionado, misma familia que el de
# _build_contextual_search_query/Capa 3 - ver el comentario ahí): a
# diferencia de esa función, esta sí filtra por rol (`if not turn...
# startswith("user")`, nunca mira turnos del asistente), pero no
# filtraba saludos puros del propio usuario - si el último turno de
# usuario en el historial era literalmente "hola"/"hello" (p. ej. el
# saludo inicial de la sesión), ese texto sobrevivía como si fuera el
# sujeto real a sustituir en un mensaje tipo "cuánto cuesta el mismo".
# Alcance más chico que el bug principal (solo dispara si además
# fallaron el diccionario de temas Y la escalada al LLM), pero misma
# causa raíz: aceptar relleno conversacional como si tuviera contenido.
_GENERIC_NOUN_EXTRACTION_STOPWORDS: frozenset = frozenset({
    "the", "a", "an", "in", "on", "at", "of", "for", "to", "is", "are", "was", "were",
    "this", "that", "it", "el", "la", "los", "las", "un", "una", "de", "en", "por", "para",
    "es", "son", "era", "eran", "eso", "esto", "esta", "este", "now", "search", "find",
    "tell", "me", "show", "give", "also", "and", "y", "también", "tambien", "ahora",
    "busca", "buscar", "dime", "one", "ones", "last", "same", "another", "more",
    "uno", "otro", "otra", "último", "ultimo", "última", "ultima", "mismo", "misma",
    "hola", "hello", "hi", "hey", "gracias", "thanks", "thank", "you",
})


def _extract_subject_noun(recent_history: List[str]) -> str:
    """
    Recorre el historial del turno más reciente al más antiguo y
    devuelve la primera palabra que coincida con `_TOPIC_CATEGORY_NOUNS`
    — el sujeto/tema real que una frase de sustitución ("the last
    one") debería reemplazar. Cadena vacía si no se encuentra ninguna.
    """
    for turn in reversed(recent_history or []):
        clean_turn = re.sub(r"^(user|assistant):\s*", "", turn, flags=re.IGNORECASE).strip().lower()
        for word in re.findall(r"[a-záéíóúñ]+", clean_turn):
            if word in _TOPIC_CATEGORY_NOUNS:
                return word
    return ""


def _extract_last_user_turn_nouns(recent_history: List[str]) -> str:
    """
    Último recurso cuando ni el diccionario de temas ni la minillamada
    al LLM identifican el sujeto: toma el último turno del USUARIO
    (no del asistente) y devuelve sus palabras de contenido, quitando
    conectores/pronombres/verbos de comando conocidos.
    """
    for turn in reversed(recent_history or []):
        if not turn.strip().lower().startswith("user"):
            continue
        clean_turn = re.sub(r"^user:\s*", "", turn, flags=re.IGNORECASE).strip()
        words = [
            w for w in re.findall(r"[a-záéíóúñA-ZÁÉÍÓÚÑ]+", clean_turn)
            if w.lower() not in _GENERIC_NOUN_EXTRACTION_STOPWORDS
        ]
        if words:
            return " ".join(words[:4])
    return ""


class LexicalSafetyNet:
    """
    Última línea de defensa determinista (sin costo de inferencia extra):
    intercepta y limpia en caliente frases de rechazo, disculpa o
    negación de capacidades prefabricadas que modelos pequeños (3B)
    a veces emiten pese al system prompt — en español e inglés — sin
    tocar el resto del contenido útil de la respuesta.
    """

    _REFUSAL_TRIGGERS_EN: Tuple[str, ...] = (
        r"as an ai(?: language model)?",
        r"as a (?:large )?language model",
        r"i(?:'m| am) (?:sorry|unable|not able)(?: to)?",
        r"i (?:will not|won'?t|cannot|can'?t) (?:perform|conduct|carry out|run|do) an? "
        r"(?:internet|web)?\s*search",
        r"i (?:cannot|can'?t|will not|won'?t) (?:assist|help|fulfill|comply with|provide|"
        r"proceed with) (?:this|that|your)? ?request",
        r"i do not have (?:access to|the ability to|the capability to)",
        r"i don'?t have (?:access to|the ability to|the capability to)",
        r"per the instructions provided",
        r"i must decline",
        r"i'?m not able to",
        r"i'?m just an ai",
    )
    _REFUSAL_TRIGGERS_ES: Tuple[str, ...] = (
        r"como (?:un |una )?(?:modelo de lenguaje|ia|inteligencia artificial)",
        r"(?:lo siento|disculpa|disculpas),? pero no puedo",
        r"no puedo (?:realizar|efectuar|llevar a cabo|ejecutar) (?:una|la) b[uú]squeda",
        r"no (?:tengo|dispongo de) acceso a internet",
        r"no estoy autorizad[oa]s? (?:a|para)",
        r"como sistema de ia",
        r"no cuento con (?:acceso a|la capacidad de)",
        r"no tengo la capacidad de",
    )

    _REFUSAL_RE: Pattern[str] = re.compile(
        r"(?i)\b(?:"
        + "|".join(_REFUSAL_TRIGGERS_EN + _REFUSAL_TRIGGERS_ES)
        + r")\b[^.!?\n]*[.!?]?\s*"
    )

    #: Bloque de código con fences (```...```). El colapso de espacios de
    #: abajo NUNCA debe tocar el interior: destruye la indentación de
    #: Python. Antes esto quedaba enmascarado porque los turnos de código
    #: pasaban además por `_validate_and_fix_python_code` (reparación AST),
    #: que reindentaba; con la arquitectura de modelo único ese paso ya no
    #: corre (is_coder es siempre False — ver RESPONSE_MODEL), así que el
    #: colapso tiene que respetar los fences por su cuenta.
    _CODE_FENCE_SPLIT_RE: Pattern[str] = re.compile(r"(```.*?```)", re.DOTALL)

    def sanitize(self, text: str) -> str:
        if not text:
            return text or ""

        cleaned = self._REFUSAL_RE.sub("", text)
        # Colapsa espacios/tabs de sobra SOLO fuera de los bloques ```...```.
        cleaned = "".join(
            seg if seg.startswith("```") else re.sub(r"[ \t]{2,}", " ", seg)
            for seg in self._CODE_FENCE_SPLIT_RE.split(cleaned)
        )
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.strip()

        # Si la limpieza vació la respuesta por completo (falso positivo
        # extremo), se prefiere devolver el texto original a dejar al
        # usuario sin respuesta alguna.
        return cleaned if cleaned else text.strip()


class TurnOutcome(str, Enum):
    FAST_PATH_DIRECT = "fast_path_direct"
    SLOW_PATH_CAS = "slow_path_cas"
    SLOW_PATH_SANDBOX = "slow_path_sandbox"
    SLOW_PATH_LSC = "slow_path_lsc"
    SLOW_PATH_FUZZER = "slow_path_fuzzer"
    SLOW_PATH_GENERIC_REASONING = "slow_path_generic_reasoning"
    ERROR = "error"


@dataclass
class TurnTrace:
    turn_id: str
    user_input: str
    routing_decision: RoutingDecision
    outcome: TurnOutcome
    engine_results: List[str]
    web_context_used: bool
    knowledge_node_persisted: bool
    logical_status: str
    final_response: str
    total_elapsed_ms: float
    model_used: str = ""
    syntax_repairs_applied: int = 0
    confidence_score: float = 0.55
    confidence_label: str = "N/D"
    thought_code_verified: bool = False
    tot_used: bool = False
    tot_agreement: float = 1.0
    epistemic_drift_detected: bool = False
    web_search_attempted: bool = False
    verification_results: Dict[str, Any] = field(default_factory=dict)
    executed_engines: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"[TURN {self.turn_id[:8]}] "
            f"outcome={self.outcome.value} "
            f"path={self.routing_decision.path.value} "
            f"model={self.model_used} "
            f"web_attempted={self.web_search_attempted} "
            f"web_context={self.web_context_used} "
            f"knowledge_node={self.knowledge_node_persisted} "
            f"logic={self.logical_status} "
            f"repairs={self.syntax_repairs_applied} "
            f"confidence={self.confidence_label}"
            f"({self.confidence_score:.2f}) "
            f"verifiers={self.verification_results} "
            f"engines={self.executed_engines} "
            f"elapsed={self.total_elapsed_ms:.1f}ms"
        )


@dataclass
class ProactiveAlert:
    message: str
    level: str = "info"

    def report(self) -> str:
        return f"[{self.level.upper()}] {self.message}"


class _ThoughtStreamGate:
    """
    Filtro con estado para el streaming token a token: retiene los
    fragmentos mientras el modelo está escribiendo su bloque `<thought>`
    interno (el plan de 6 pasos que el usuario NO debe ver) y recién
    empieza a dejar pasar texto cuando aparece `</thought>` / `[/thought]`.

    Reglas:
      - Si el texto acumulado (sin espacios iniciales) empieza con
        `<thought>` o `[thought]` → modo "retenido": no emite nada hasta
        ver la etiqueta de cierre; al verla, emite todo lo que venga
        DESPUÉS y de ahí en más pasa cada fragmento en vivo.
      - Si los primeros ~32 caracteres no abren un `<thought>` → modo
        "transparente" desde el arranque (caso del carril trivial, que
        no lleva protocolo `<thought>`, y de cualquier respuesta que
        arranque directo).
      - Si el modelo nunca cierra `</thought>` (fallo conocido de modelos
        chicos), no se emite nada por streaming — la reconciliación
        final con `trace.final_response` en la UI
        (`_on_turn_completed`) muestra el texto correcto igual.
    """

    _OPEN_RE: Pattern[str] = re.compile(r"^\s*(?:<thought\b[^>]*>|\[thought\])", re.IGNORECASE)
    _CLOSE_RE: Pattern[str] = re.compile(r"</thought\s*>|\[/thought\]", re.IGNORECASE)

    def __init__(self) -> None:
        self._raw = ""
        self._decided = False          # ¿ya sabemos si hay <thought> o no?
        self._withholding = False      # ¿estamos reteniendo hasta </thought>?
        self._passthrough = False      # ¿ya pasamos a modo transparente?

    def raw_so_far(self) -> str:
        return self._raw

    @staticmethod
    def _could_still_open(stripped: str) -> bool:
        """
        ¿El texto acumulado (sin espacios iniciales) todavía podría
        convertirse en una etiqueta de apertura `<thought>` / `[thought]`
        si llegan más caracteres? True solo mientras es un PREFIJO de una
        de esas etiquetas — así una respuesta corta que arranca directo
        ("Hola, ¿qué tal?") pasa a modo transparente ya en el primer
        fragmento, en vez de esperar a acumular N caracteres.
        """
        head = stripped[:9].lower()
        return (
            "<thought>".startswith(head)
            or "[thought]".startswith(head)
            or head.startswith("<thought")
            or head.startswith("[thought")
        )

    def feed(self, chunk: str) -> str:
        """Devuelve la porción VISIBLE de `chunk` (puede ser "")."""
        if not chunk:
            return ""
        self._raw += chunk

        if self._passthrough:
            return chunk

        if not self._decided:
            stripped = self._raw.lstrip()
            if not stripped:
                return ""  # solo espacios hasta acá
            if self._OPEN_RE.match(stripped):
                self._decided = True
                self._withholding = True
            elif self._could_still_open(stripped):
                return ""  # la etiqueta <thought> podría estar formándose
            else:
                # El texto NO abre <thought> y ya no puede hacerlo → no hay
                # plan interno. Todo lo acumulado se retuvo (se devolvió
                # ""), así que se emite entero de una y de acá en más pasa
                # cada fragmento en vivo.
                self._decided = True
                self._passthrough = True
                return self._raw

        if self._withholding:
            m = self._CLOSE_RE.search(self._raw)
            if not m:
                return ""
            self._withholding = False
            self._passthrough = True
            tail = self._raw[m.end():]
            return tail.lstrip("\n") if tail else ""

        return chunk


class MemoryGovernor:
    # Presupuesto de RECORTE del prompt (lo usa _trim_context_to_budget),
    # deliberadamente POR DEBAJO de la ventana real fijada por
    # PINNED_NUM_CTX_* (8192). Esa distancia ES el margen que evita que el
    # prompt recortado + num_predict lleguen a tocar el techo de num_ctx.
    # Subio de 4096 a 6144 junto con _GENERATION_TOKEN_RESERVE: agregar el
    # bloque _FINAL_ANSWER_STYLE_ES a la cabecera 'system' la engordo de
    # ~2341 a ~2706 tokens, y con el techo viejo de 4096 eso le habria
    # robado contexto a la evidencia web (947 -> 582 tokens disponibles).
    # Con 6144 y reserva 2048 quedan ~1350 tokens de contexto: mas que los
    # 947 de antes, no menos.
    MAX_NUM_CTX: int = 6144
    MIN_NUM_CTX: int = 1024
    # BASE_NUM_PREDICT 1024 -> 2048 (general y web_evidence).
    # medido, no supuesto: con el prompt viejo el modelo generaba ~114-390
    # tokens y paraba SOLO, sin acercarse al techo de 1024 - subirlo
    # entonces no habria cambiado nada. Recien con el bloque de estilo
    # (_FINAL_ANSWER_STYLE_ES + la cola de _build_reasoning_prompt) el
    # techo pasa a morder de verdad: con num_predict=1024 Ollama devuelve
    # done_reason='length' y la respuesta se corta a mitad de frase; con
    # 2048 devuelve done_reason='stop' y termina sola. O sea: el bloque de
    # estilo es la causa del cambio de longitud, y este techo es lo que
    # evita que esa longitud se trunque.
    # OJO con el nombre: REDUCED_NUM_PREDICT es MAYOR que BASE, no menor.
    # Es el techo del rol CODER, que puede permitirselo porque su cabecera
    # (CODER_SYSTEM_PROMPT, ~1035 tokens) es mucho mas chica que la general
    # (~2706), asi que le sobra ventana. Truncar codigo a la mitad es peor
    # que truncar prosa.
    BASE_NUM_PREDICT: int = 4096
    REDUCED_NUM_PREDICT: int = 3072

    # Nota (medido - turno "dime ecuaciones importantes de
    # fisica", screenshot + log del usuario, path=fast_path score=+0.00,
    # decode=4096tok - el techo completo de BASE_NUM_PREDICT): la
    # respuesta enumeró leyes reales al principio pero, pasado cierto
    # punto, degeneró en repetir el mismo ítem ("Ley de Newton de la
    # Tensión en Paredes (sobre un ángulo)") más de una decena de veces
    # seguidas, variando solo la función trigonométrica de la fórmula
    # (sin, cos, tan, cot, sec, csc, y otra vez cot, csc, sec...) hasta
    # cortarse a mitad de palabra al llegar al techo de tokens.
    #
    # Ollama ya aplica un repeat_penalty por defecto (1.1) con una
    # ventana repeat_last_n por defecto de 64 tokens - pero cada ítem
    # repetido acá mide ~60-90 tokens él solo (título en negrita +
    # explicación + fórmula LaTeX), así que para cuando el modelo vuelve
    # a escribir el mismo bloque, la ocurrencia anterior YA salió de esa
    # ventana de 64 tokens: el repeat_penalty por defecto es ciego a
    # este patrón - no es que falte, es que su ventana es demasiado
    # corta para el tamaño real de lo que se repite acá.
    #
    # Mitigación en dos capas, no una sola:
    #   1. Acá: ensanchar la ventana (REPEAT_LAST_N) muy por encima del
    #      tamaño de un ítem típico, y subir la penalización
    #      (REPEAT_PENALTY) por sobre el default de Ollama.
    #   2. `_dedupe_enumeration_items` (ver la nota junto a esa función,
    #      cerca de `_strip_leaked_reasoning`): una red de seguridad
    #      determinística que recorta la respuesta si el título de un
    #      ítem se repite igual, sin depender de que el ajuste de
    #      decodificación alcance por sí solo.
    #
    # Nota: este entorno no tiene forma de levantar Ollama con el
    # modelo real del usuario (qwen2.5:3b) para confirmar en vivo que
    # este cambio de parámetros por sí solo elimina el bucle - es una
    # mitigación razonada a partir de la evidencia del log, no una
    # medición end-to-end como la mayoría de las notas de este
    # archivo. La capa 2 (`_dedupe_enumeration_items`) sí está
    # verificada directamente, reproduciendo el texto exacto de la
    # captura, y es la que garantiza que el usuario nunca vea el bucle
    # aunque esta capa 1 no alcance.
    REPEAT_PENALTY: float = 1.3
    REPEAT_LAST_N: int = 512

    # Carril SUAVE de anti-repetición para modelos NO-qwen (hoy el general
    # es phi3.5:3.8b - swap pedido por el usuario, ver GENERAL_MODEL_
    # VARIANTS). Antes `pinned_options` solo tocaba repeat_penalty/
    # repeat_last_n para la familia qwen y dejaba a phi3.5 con los
    # defaults de Ollama (1.1 / 64) - y hay un fallo medido con esa
    # config: "hola" contra phi3.5:3.8b devolvió 1500+ tokens de prosa
    # incoherente con palabras pegadas sin espacio. El valor qwen (1.3)
    # es deliberadamente agresivo para el bucle de enumeración de ítems
    # largos de qwen2.5 y puede sobre-penalizar en otro tokenizador
    # (repite estructuras normales del idioma), así que este carril usa
    # un apretón más leve: por encima del default de Ollama, pero lejos
    # del 1.3. Hipótesis razonada a partir de la evidencia del log, sin
    # medición end-to-end desde esta sesión - si la prosa de phi3.5
    # sigue degenerando, subir SOFT_REPEAT_PENALTY hacia 1.25.
    _SOFT_REPEAT_PENALTY_ENV_VAR: str = "SOVNODE_SOFT_REPEAT_PENALTY"
    SOFT_REPEAT_PENALTY: float = 1.18
    SOFT_REPEAT_LAST_N: int = 256

    # Techo de `num_predict` para el carril fast_path (el router ya
    # decidió que el turno es SIMPLE).
    #
    # HISTÓRICO (phi3.5:3.8b): BASE_NUM_PREDICT=4096 le daba a un phi3.5
    # descarrilado 4096 tokens de cuerda — medido (screenshot 2026-08-27,
    # "tell me the most important equations of math" con búsqueda web):
    # `decode=4096tok/100s` de basura. 900 alcanzaba para 3-4 párrafos.
    #
    # RECALIBRADO para gpt-oss:20b (arquitectura de modelo único, PASO 0 —
    # ver STEP0_HARMONY_FINDINGS.md). Con `think=low` (ver THINK_LEVEL) el
    # canal `analysis` de gpt-oss consume ~15 tokens del techo, así que
    # `num_predict` es casi todo respuesta visible — a diferencia de phi3.5.
    # MEDIDO: una respuesta fast_path buena de gpt-oss ronda 300-450 tokens
    # visibles; 900 sigue dando margen holgado y acota el peor caso. El
    # decode real de esta máquina (~5.6 tok/s en iGPU) hace que subir este
    # techo cueste caro en latencia, así que se mantiene ajustado.
    _FASTPATH_NUM_PREDICT_ENV_VAR: str = "SOVNODE_FASTPATH_NUM_PREDICT"
    FASTPATH_NUM_PREDICT: int = 900

    # Techo de `num_predict` para el carril slow_path. Con la arquitectura
    # de modelo único slow_path también se genera en UNA sola pasada por el
    # carril lean (ver run_turn/process_turn) — ya no hay reparto 60/40 de
    # `_call_llm_two_pass`. Un turno slow_path legítimo (conceptual denso,
    # enumeración larga con desarrollo) necesita más margen que fast_path
    # para desarrollar cada punto sin cortarse. MEDIDO (PASO 0, gpt-oss +
    # think=low): una respuesta conceptual densa ronda 500-800 tokens
    # visibles; una enumeración de 10 ítems con explicación, ~700-1000.
    # 1800 cubre ambos con margen y acota el peor caso a ~5 min de decode.
    _SLOWPATH_NUM_PREDICT_ENV_VAR: str = "SOVNODE_SLOWPATH_NUM_PREDICT"
    SLOWPATH_NUM_PREDICT: int = 1800

    @classmethod
    def _num_predict_from_env(cls, env_var: str, default: int) -> int:
        override = os.environ.get(env_var)
        if override:
            try:
                parsed = int(override)
                if parsed > 0:
                    return parsed
            except ValueError:
                pass
        return default

    @classmethod
    def fastpath_num_predict(cls) -> int:
        return cls._num_predict_from_env(cls._FASTPATH_NUM_PREDICT_ENV_VAR, cls.FASTPATH_NUM_PREDICT)

    @classmethod
    def slowpath_num_predict(cls) -> int:
        return cls._num_predict_from_env(cls._SLOWPATH_NUM_PREDICT_ENV_VAR, cls.SLOWPATH_NUM_PREDICT)

    # =================================================================
    # SPLIT MECÁNICO RAZONAMIENTO/RESPUESTA (dos pasadas separadas)
    # =================================================================
    # medido, no supuesto (turnos reales contra qwen2.5:3b, español, con
    # y sin contexto web, prompt actual incluyendo el arreglo de
    # supresión de <thought> - ver el recordatorio agregado al final de
    # `[CRITICAL LANGUAGE RULE]` en `_build_reasoning_prompt`):
    #
    #   turno factual simple, sin web .... thought ~67% del total
    #   turno con contexto web ........... thought ~81% del total,
    #                                       respuesta visible apenas
    #                                       ~380 caracteres
    #
    # Es decir: el <thought> sí se come el presupuesto en el escenario
    # dominante de este proyecto (español + búsqueda web), una vez que
    # <thought> se emite de forma confiable - un bug INDEPENDIENTE (la
    # regla de idioma al final del prompt) lo estaba suprimiendo por
    # completo, lo que enmascaraba este problema en las mediciones
    # anteriores a esa corrección. Ya arreglado ese bug, el problema
    # reaparece con fuerza y este split es la respuesta: un TECHO real
    # impuesto por `num_predict` en llamadas HTTP separadas, no una
    # instrucción de prompt que el modelo puede decidir ignorar.
    #
    # 60/40 en vez de un reparto más generoso para el razonamiento: en
    # la prueba de dos pasadas real (pregunta factual, ES), la Pasada 1
    # cerró un <thought> de 6 pasos COMPLETO usando solo 715 de 2458
    # tokens disponibles (29%) - hay margen de sobra incluso para
    # turnos con contexto web, cuyo paso 3 (checklist por fuente) es el
    # que más infla el plan.
    REASONING_SHARE: float = 0.60
    ANSWER_SHARE: float = 0.40

    # Piso en tokens para la Pasada 2, NO una cuota obligatoria: el
    # modelo sigue pudiendo responder corto cuando la pregunta es
    # puramente factual (ver _FINAL_ANSWER_STYLE_ES, que exime
    # explícitamente ese caso). Este piso solo protege contra que el
    # 40% del presupuesto TOTAL resulte insuficiente incluso para una
    # respuesta normal - p. ej. si BASE_NUM_PREDICT se bajara en el
    # futuro vía entorno. Con BASE_NUM_PREDICT=4096 (40% = 1638) NO es
    # vinculante hoy; sugerido ~300, ajustar si BASE_NUM_PREDICT cambia.
    ANSWER_MIN_FLOOR: int = 300

    DEFAULT_NUM_GPU: int = -1
    KEEP_ALIVE: str = "30m"
    RELIEF_COOLDOWN_SECONDS: float = 90.0
    CONSECUTIVE_PRESSURE_THRESHOLD: int = 3

    # Optimización #1 - Prefix Alignment & KV-Cache Persistente: num_ctx
    # FIJO por rol, nunca derivado de len(prompt). calibrate_options()
    # (abajo) recalculaba num_ctx en cada llamada según el tamaño del
    # prompt - dos llamadas sucesivas al mismo modelo dentro de un mismo
    # turno (p. ej. la respuesta inicial y el followup de verificación de
    # <thought_code> en _call_llm) casi nunca piden el mismo num_ctx, y
    # Ollama/llama.cpp reasigna el contexto del "runner" del modelo cada
    # vez que ese parámetro cambia - invalidando cualquier prefijo KV que
    # pudiera reutilizarse entre esas llamadas, aunque el SYSTEM_PROMPT +
    # TOOLS_SCHEMA sean byte-idénticos (ver _build_system_header). Fijar
    # num_ctx por rol mantiene el runner estable entre llamadas
    # sucesivas al mismo modelo, que es el requisito real para que el
    # backend reutilice esa cabecera cacheada.
    PINNED_NUM_CTX_GENERAL: int = 8192
    PINNED_NUM_CTX_WEB_EVIDENCE: int = 8192
    PINNED_NUM_CTX_CODER: int = 8192

    # Optimización - Throughput objetivo 15 tok/s en CPU: Ollama nunca
    # recibía `num_thread` explícito en ninguna llamada de este archivo,
    # así que quedaba en su default interno (a veces detecta mal núcleos
    # SMT/lógicos en Windows y sobre-asigna hilos, lo que en CPUs de
    # pocos núcleos reales como el Ryzen 5700G - 8 núcleos físicos, 16
    # lógicos - puede DEGRADAR el decode por contención en vez de
    # acelerarlo). `_default_num_thread()` fija un valor razonado en
    # núcleos FÍSICOS (no lógicos): usar los 16 hilos lógicos completos
    # para un solo stream de decode secuencial no aporta más tok/s más
    # allá de los núcleos físicos (el decode autoregresivo es
    # ancho-de-banda-de-memoria-bound, no compute-bound, así que SMT no
    # ayuda aquí) y le resta CPU a la UI de Qt y al resto del proceso.
    # Override manual vía entorno para no tener que tocar código si la
    # medición real en la máquina del usuario sugiere otro número.
    #
    # Nota (medido - regresión propia de esta sesión): se
    # probó hacer este valor DINÁMICO (bajarlo a la mitad cuando había
    # 2 llamadas concurrentes en vuelo bajo el semáforo de _llm_lock),
    # con la misma lógica que ya protege a `PINNED_NUM_CTX_*` de
    # cambiar entre llamadas sucesivas al mismo modelo. Medido en vivo:
    # cambiar `num_thread` entre dos peticiones sucesivas al mismo
    # modelo fuerza una recarga COMPLETA del runner de Ollama - igual
    # de cara que cambiar `num_ctx` (~7-10s por cambio, no solo un
    # ajuste de hilos). Un turno normal encadena varias llamadas al
    # mismo modelo (Pasada 1, Pasada 2, tool-calling, hasta 3
    # correcciones de verificación, LangFix) - si el valor de
    # concurrencia fluctuaba entre esas llamadas (algo que sí pasa: el
    # CognitiveGovernor sondea el cerrojo cada 5s en background), cada
    # cambio pagaba una recarga completa, acumulando decenas de
    # segundos de nada en un turno tan simple como "hola". Por eso
    # `_num_thread` se calcula UNA sola vez y se queda fijo para toda
    # la vida del proceso - igual que `PINNED_NUM_CTX_*`, nunca debe
    # variar entre llamadas sucesivas al mismo modelo dentro de un
    # turno.
    _NUM_THREAD_ENV_VAR: str = "SOVNODE_OLLAMA_NUM_THREAD"

    @classmethod
    def _default_num_thread(cls) -> int:
        override = os.environ.get(cls._NUM_THREAD_ENV_VAR)
        if override:
            try:
                parsed = int(override)
                if parsed > 0:
                    return parsed
            except ValueError:
                pass
        logical = os.cpu_count() or 8
        # Estimación de núcleos físicos a partir de lógicos (asume SMT/
        # Hyper-Threading 2x, el caso común en desktop AMD/Intel). Con
        # mínimo 4 para no castigar máquinas de pocos núcleos.
        physical_estimate = max(4, logical // 2)
        return physical_estimate

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_relief_ts = 0.0
        self._consecutive_pressure_turns = 0
        self._num_thread = self._default_num_thread()

    def pinned_options(self, is_coder: bool, has_web_evidence: bool = False, model: str = "") -> Dict[str, Any]:
        if is_coder:
            num_ctx = self.PINNED_NUM_CTX_CODER
        elif has_web_evidence:
            num_ctx = self.PINNED_NUM_CTX_WEB_EVIDENCE
        else:
            num_ctx = self.PINNED_NUM_CTX_GENERAL
        options = {
            "num_ctx": num_ctx,
            "num_predict": self.REDUCED_NUM_PREDICT if is_coder else self.BASE_NUM_PREDICT,
            "num_gpu": self.DEFAULT_NUM_GPU,
            "num_thread": self._num_thread,
            "keep_alive": self.KEEP_ALIVE,
        }
        # Bug real, medido - captura del usuario: "hola" contra
        # phi3.5:3.8b devolvió 1500+ tokens de prosa incoherente,
        # palabras pegadas sin espacio ("simultáneamentecriterio",
        # "arribazo"). REPEAT_PENALTY/REPEAT_LAST_N (ver la nota
        # junto a esas constantes) se razonó específicamente para el
        # bucle degenerativo de qwen2.5 (ítems largos repetidos) - el
        # 1.3 es agresivo y en otro tokenizador puede sobre-penalizar
        # estructuras normales del idioma. Por eso hay dos carriles:
        #   - familia qwen: el apretón fuerte (REPEAT_PENALTY / REPEAT_
        #     LAST_N), como antes.
        #   - cualquier otro modelo (phi3.5 incluido): un carril SUAVE
        #     (SOFT_REPEAT_*), por encima del default de Ollama (1.1/64)
        #     pero lejos del 1.3 - ver la nota junto a SOFT_REPEAT_
        #     PENALTY. Antes este `else` no existía y phi3.5 quedaba con
        #     el default, que es justo la config bajo la que se midió la
        #     prosa incoherente de arriba.
        if "qwen" in (model or "").lower():
            options["repeat_penalty"] = self.REPEAT_PENALTY
            options["repeat_last_n"] = self.REPEAT_LAST_N
        else:
            options["repeat_penalty"] = self._soft_repeat_penalty()
            options["repeat_last_n"] = self.SOFT_REPEAT_LAST_N
        return options

    @classmethod
    def _soft_repeat_penalty(cls) -> float:
        """
        `SOFT_REPEAT_PENALTY` con override por entorno
        (`SOVNODE_SOFT_REPEAT_PENALTY`) — mismo patrón que
        `_default_num_thread`/`_NUM_THREAD_ENV_VAR`: permite ajustar el
        apretón anti-repetición del carril no-qwen en la máquina del
        usuario, con medición real, sin tocar código. Un valor <= 1.0
        (que desactivaría de hecho la penalización) o no numérico se
        ignora y se usa el default de clase.
        """
        override = os.environ.get(cls._SOFT_REPEAT_PENALTY_ENV_VAR)
        if override:
            try:
                parsed = float(override)
                if parsed > 1.0:
                    return parsed
            except ValueError:
                pass
        return cls.SOFT_REPEAT_PENALTY

    def split_budget(self, is_coder: bool, has_web_evidence: bool = False) -> Tuple[int, int]:
        """
        Reparto MECÁNICO del presupuesto de tokens entre razonamiento
        (`<thought>`) y respuesta visible, para el modo de dos pasadas
        (ver `Orchestrator._call_llm_two_pass`). Devuelve
        `(reasoning_budget, answer_budget)`, ya redondeados y con el
        piso aplicado.

        `is_coder=True` devuelve `(0, REDUCED_NUM_PREDICT)` — CODER_
        SYSTEM_PROMPT NO define el protocolo `<thought>` de 6 pasos (solo
        usa `<thought_code>`, la etiqueta de verificación en sandbox, que
        es otra cosa). Sin bloque de razonamiento que dividir, no hay
        nada que repartir 60/40: todo el presupuesto es para el código.
        Esta función no debería llamarse siquiera para turnos coder (el
        llamador debe quedarse en `_call_llm`/una sola pasada), pero
        degrada de forma segura si ocurre igual.

        `has_web_evidence` no cambia el reparto hoy — se acepta por
        paridad de firma con `pinned_options()` y porque la medición que
        motivó 60/40 mostró que el turno con contexto web es el que MÁS
        presiona el <thought> (81% del total, ver comentario de
        REASONING_SHARE), así que si algún día se diferencia el reparto
        por rol, ese caso necesita MÁS margen de razonamiento, no menos.
        """
        if is_coder:
            return 0, self.REDUCED_NUM_PREDICT

        total = self.BASE_NUM_PREDICT
        reasoning_budget = round(total * self.REASONING_SHARE)
        # El resto (no un round() independiente de ANSWER_SHARE) evita
        # que dos redondeos por separado sumen distinto de `total` - ver
        # nota de ANSWER_MIN_FLOOR sobre por qué el piso no es vinculante
        # con los valores actuales.
        answer_budget = max(self.ANSWER_MIN_FLOOR, total - reasoning_budget)
        return reasoning_budget, answer_budget

    def calibrate_options(self, context_char_length: int) -> Dict[str, Any]:
        approximate_tokens = max(1, context_char_length // 4)
        num_ctx = max(
            self.MIN_NUM_CTX,
            min(self.MAX_NUM_CTX, approximate_tokens + 256),
        )

        num_predict = (
            self.BASE_NUM_PREDICT
            if num_ctx <= 1536
            else self.REDUCED_NUM_PREDICT
        )

        return {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "num_gpu": self.DEFAULT_NUM_GPU,
            "num_thread": self._num_thread,
            "keep_alive": self.KEEP_ALIVE,
        }

    def should_relieve_memory(self, memory_pressure_detected: bool) -> bool:
        with self._lock:
            if not memory_pressure_detected:
                self._consecutive_pressure_turns = 0
                return False

            self._consecutive_pressure_turns += 1
            now = time.monotonic()

            cooldown_elapsed = (
                now - self._last_relief_ts
                >= self.RELIEF_COOLDOWN_SECONDS
            )

            should_relieve = (
                self._consecutive_pressure_turns
                >= self.CONSECUTIVE_PRESSURE_THRESHOLD
                and cooldown_elapsed
            )

            if should_relieve:
                self._last_relief_ts = now
                self._consecutive_pressure_turns = 0

            return should_relieve


_EQUATION_CANDIDATE_RE: Pattern[str] = re.compile(
    r"(?<![A-Za-z_])"
    r"[\dA-Za-z_\.\+\-\*/\^\(\),\s]{1,300}"
    r"(?:==|=)"
    r"[\dA-Za-z_\.\+\-\*/\^\(\),\s]{1,300}",
    re.IGNORECASE,
)

_FUNCTION_EXPRESSION_RE: Pattern[str] = re.compile(
    r"(?<![A-Za-z_])"
    r"(?:sin|cos|tan|asin|acos|atan|log|ln|sqrt|exp|"
    r"diff|integrate|factor|expand)"
    r"\s*\([^()]{1,220}\)"
    r"(?:\s*[\+\-\*/\^]\s*[\dA-Za-z_\.\(\),]+)*",
    re.IGNORECASE,
)

_ALGEBRAIC_EXPRESSION_RE: Pattern[str] = re.compile(
    r"(?<![A-Za-z_])"
    r"(?:"
    r"[\d]+(?:\.\d+)?|"
    r"[A-Za-z_]\w*|"
    r"\([^()]{1,180}\)"
    r")"
    r"(?:\s*[\+\-\*/\^]\s*"
    r"(?:[\d]+(?:\.\d+)?|[A-Za-z_]\w*|\([^()]{1,180}\)))+",
    re.IGNORECASE,
)

_MATH_COMMAND_RE: Pattern[str] = re.compile(
    r"\b("
    r"simplifica|simplificar|resuelve|resolver|factoriza|factorizar|"
    r"calcula|calcular|cu[aá]nto\s+es|eval[uú]a|evaluar|"
    r"deriva|derivar|integra|integrar"
    r")\b",
    re.IGNORECASE,
)

_MATH_LEADING_NOISE_RE: Pattern[str] = re.compile(
    r"^\s*("
    r"simplifica(?:r)?|resuelve(?:r)?|factoriza(?:r)?|"
    r"calcula(?:r)?|cu[aá]nto\s+es|eval[uú]a(?:r)?|"
    r"deriva(?:r)?|integra(?:r)?|"
    r"seg[uú]n\s+el\s+enunciado|de\s+acuerdo(?:\s+con)?|"
    r"sabemos(?:\s+que)?|considerando|dado(?:\s+que)?"
    r")\b\s*[:,-]?\s*",
    re.IGNORECASE,
)

_MATH_TRAILING_NOISE_RE: Pattern[str] = re.compile(
    r"\s*\b("
    r"aproximadamente|m[aá]s\s+o\s+menos|"
    r"por\s+favor|gracias|verdad|cierto"
    r")\b.*$",
    re.IGNORECASE,
)

_CODE_FENCE_RE: Pattern[str] = re.compile(
    r"```(?:python)?\s*(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


class ScopeValidator(ast.NodeVisitor):
    """Analizador estático ligero que detecta nombres y variables no definidas en el código."""

    def __init__(self) -> None:
        import builtins

        self.defined_names = set(dir(builtins))
        self.undefined_errors: List[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.defined_names.add(alias.asname or alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.defined_names.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.defined_names.add(node.name)
        for arg in node.args.args:
            self.defined_names.add(arg.arg)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.defined_names.add(node.name)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.defined_names.add(node.id)
        elif isinstance(node.ctx, ast.Load):
            if node.id not in self.defined_names:
                self.undefined_errors.append(
                    f"Línea {node.lineno}: La variable o módulo '{node.id}' no está definido."
                )
        self.generic_visit(node)


class CognitiveGovernor(threading.Thread):
    """
    Motor de Introspección Continua (Bucle Asíncrono).
    Audita el estado del sistema en segundo plano y propone refactorizaciones.

    Optimización #5 — Preemción cooperativa: el bucle dormía con
    `time.sleep(interval)`, que NO puede interrumpirse — si el usuario
    escribía justo después de que el gobernador empezara a dormir, el
    bucle seguía dormido hasta agotar el intervalo completo (hasta 5s)
    antes de volver a comprobar `_is_processing_turn`. Ahora duerme con
    `threading.Event.wait(timeout=interval)`: en cuanto
    `orchestrator._pause_governor_event.set()` se llama (primera línea de
    process_turn/StreamTurnWorker.run()), `wait()` retorna de inmediato
    — típicamente en menos de 5ms, acotado solo por el scheduler del SO
    — en vez de esperar a que el timeout se agote.

    Ese Event cubre el caso "el gobernador está dormido, entre ciclos".
    El caso "el gobernador YA está en medio de una llamada de
    auto-reparación" es distinto: `_call_llm` es una petición HTTP
    síncrona y bloqueante — no puede interrumpirse a mitad de vuelo sin
    cerrar el socket desde otro hilo. En vez de fingir una cancelación
    que no es real, aquí se acota el daño de esa ventana de dos formas:
    (a) antes de empezar CUALQUIER auto-reparación se comprueba el Event
    Y se intenta un acquire NO bloqueante de `_llm_lock` — si el usuario
    ya está activo o el cerrojo está ocupado, el ciclo se salta por
    completo sin encolarse a esperar; y (b) la propia auto-reparación se
    acota a una sola iteración (`max_iterations=1`, ver _self_repair) en
    vez de las 3 por defecto, para que el peor caso de una carrera
    (el gobernador arranca una fracción de segundo antes que el usuario)
    sea una única llamada acotada, no una cadena de 3.
    """
    def __init__(self, orchestrator: Orchestrator, interval_seconds: int = 5):
        super().__init__(daemon=True, name="CognitiveGovernor")
        self.orchestrator = orchestrator
        self.interval = interval_seconds
        self._running = True
        self._processed_error_hashes = set()  # Registro deduplicado de fallos analizados

    def run(self):
        print("🚀 [CognitiveGovernor] ¡Bucle de razonamiento autónomo en línea!")
        logger.info("🚀 [CognitiveGovernor] Iniciando bucle de razonamiento autónomo.")
        pause_event = getattr(self.orchestrator, "_pause_governor_event", None)

        while self._running:
            # Event.wait(timeout) retorna apenas otro hilo llama a .set()
            # (turno de usuario iniciando) en vez de esperar el intervalo
            # completo - esa es la ganancia real de latencia de reacción.
            if pause_event is not None:
                pause_event.wait(timeout=self.interval)
            else:
                time.sleep(self.interval)

            # Pausa el bucle asíncrono si el usuario interactúa activamente
            # para liberar la GPU/VRAM - comprobación final antes de
            # arrancar cualquier trabajo nuevo.
            if getattr(self.orchestrator, "_is_processing_turn", False):
                continue

            llm_lock = getattr(self.orchestrator, "_llm_lock", None)
            if llm_lock is not None:
                # Acquire NO bloqueante: si el cerrojo está ocupado (el
                # usuario ganó la carrera por una fracción de segundo),
                # el gobernador se retira de inmediato en vez de
                # encolarse detrás de esa llamada.
                if not llm_lock.acquire(blocking=False):
                    continue
                llm_lock.release()

            try:
                self._introspect()
            except Exception as e:
                logger.error(f"Error en CognitiveGovernor: {e}")

    def _introspect(self):
        if getattr(self.orchestrator, "_is_processing_turn", False):
            return  # Cede prioridad absoluta al turno activo del usuario
            
        wal = getattr(self.orchestrator, "_wal", None)
        if not wal:
            return
        
        # PARCHE 1: Extracción segura bajo cerrojo de concurrencia
        if hasattr(wal, "get_recent_entries"):
            recent_entries = wal.get_recent_entries(5)
        else:
            entries = getattr(wal, "_entries", None)
            if entries is None and hasattr(wal, "get_entries"):
                entries = wal.get_entries()
            recent_entries = entries[-5:] if entries else []

        for entry in recent_entries:
            if isinstance(entry, dict) and entry.get("outcome") == "error":
                content = entry.get("content", "")
                if not content:
                    continue
                
                # PARCHE 3: Evita bucles redundantes procesando cada error una sola vez
                error_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if error_hash not in self._processed_error_hashes:
                    self._processed_error_hashes.add(error_hash)
                    logger.info("🔍 [CognitiveGovernor] Fallo detectado en WAL, iniciando auto-análisis.")
                    self._self_repair(content)

    def _self_repair(self, error_log: str):
        fuzz_result = self.orchestrator._fuzzer.audit(error_log)
        if fuzz_result.verdict == FuzzingVerdict.CRITICALLY_FRAGILE:
            logger.info("🛠️ [CognitiveGovernor] Fragilidad crítica detectada, proponiendo refactorización.")
            hyp, _, _ = self.orchestrator._recursive_self_critique(
                hypothesis=f"El código ha fallado con error: {error_log}. Propón una corrección robusta.",
                context="Auto-reparación sistémica",
                max_iterations=1,
                target_model=self.orchestrator.coder_model
            )
            if hasattr(self.orchestrator._wal, "append_response"):
                self.orchestrator._wal.append_response("AUTO_REPAIR", hyp, "suggested_patch")

            # Genialidad #2: el mismo parche auto-sugerido se indexa también
            # en MemoryGraph (WAL es solo un log append-only sin búsqueda),
            # para que futuros turnos con un error léxicamente similar lo
            # encuentren vía _fetch_metacognitive_lessons().
            memory_graph = getattr(self.orchestrator, "memory_graph", None)
            if memory_graph is not None:
                with contextlib.suppress(Exception):
                    memory_graph.store_reasoning_lesson(
                        "AUTO_REPAIR", "suggested_patch",
                        f"Error original: {error_log}\nParche sugerido: {hyp}",
                    )


# =====================================================================
# ESTILO DE LA RESPUESTA final AL usuario
# =====================================================================
# Motivo (medido contra el modelo real, no supuesto): con el prompt
# anterior, qwen2.5:3b generaba ~114-390 tokens y se detenia SOLO, muy
# por debajo del techo de num_predict (que entonces era 1024). Es
# decir: la respuesta corta NO venia de un limite de tokens, sino de
# que nada en el prompt pedia desarrollo. El bloque <thought> (6 pasos)
# absorbia casi todo el esfuerzo: en una medicion tipica el pensamiento
# INTERNO ocupaba 3059 caracteres y la respuesta VISIBLE solo 630. Este
# bloque reencuadra el paso 4 del <thought> como un GUION que todavia
# hay que desarrollar, no como la respuesta ya terminada.
#
# ADVERTENCIAS DE DISENO (las dos verificadas empiricamente contra el
# modelo; no las relajes sin volver a medir):
#   * LA POSICION IMPORTA. Colocado al final de la cabecera "system",
#     este bloque le roba la posicion de arranque al protocolo
#     <thought>: el modelo deja de emitir <thought> POR COMPLETO, lo
#     que rompe _split_thought_and_content y la verificacion de
#     <thought_code>. Va antes de "REGLAS DE EJECUCION DE
#     HERRAMIENTAS", nunca al final del prompt.
#   * NO le pongas un tope duro de palabras al <thought> (p. ej.
#     "maximo ~120 palabras"). Probado: desestabiliza el contrato de
#     formato en un modelo de 3B y los pasos 1-6 del plan interno se
#     FILTRAN a la respuesta visible, que es exactamente lo que el
#     propio SYSTEM_PROMPT prohibe unas lineas mas arriba.
#
# ESTADO (arquitectura de modelo único, 2026-08-27): estos dos bloques
# quedaron SIN USO. Solo entraban al prompt vía `_get_base_system_prompt`
# / `_final_answer_instruction_tail`, que ahora solo se usan en el carril
# NO-lean (dos pasadas), retenido sin invocar. Con gpt-oss el carril lean
# usa `_get_fastpath_system_prompt` + `_fastpath_answer_tail`, cuyo estilo
# ya se midió adecuado en el PASO 0 (respuestas BLUF, con desarrollo,
# `done_reason='stop'` natural — ver STEP0_HARMONY_FINDINGS.md). Se
# conservan por si el carril de dos pasadas vuelve a usarse (rollback) y
# porque la sección 20 de test_regressions.py verifica su regla de LaTeX.
# NO copiar estas instrucciones al prompt lean sin volver a medir: gpt-oss
# es más verboso que phi3.5 y forzar "desarrolla cada punto en un párrafo"
# lo empuja hacia la longitud que dispara los circuit-breakers.
# =====================================================================
_FINAL_ANSWER_STYLE_ES: str = (
    "=================================================================\n"
    "ESTILO OBLIGATORIO DE LA RESPUESTA FINAL (el texto tras </thought>)\n"
    "=================================================================\n"
    "Esta sección NO altera el protocolo <thought>: ese bloque sigue siendo "
    "obligatorio, sigue yendo primero y sigue siendo un plan interno que el "
    "usuario nunca ve. Todo lo que sigue aplica ÚNICAMENTE al texto que "
    "escribes DESPUÉS de </thought>.\n"
    "Ese texto es lo ÚNICO que el usuario lee: el plan del paso 4 no es la "
    "respuesta, es el guion que ahora debes DESARROLLAR. Entregar solo la "
    "conclusión del plan es dejar la respuesta a medias.\n"
    # Tono directo tipo BLUF (pedido del usuario): abrir con la respuesta
    # antes de desarrollarla.
    "- ARRANCÁ por la idea central: abrí con la respuesta concreta o la "
    "conclusión principal en la primera oración, con seguridad y sin rodeos "
    "— nada de \"como asistente de IA\" ni relleno tipo \"es importante notar "
    "que\". Recién después desarrollá el porqué, el contexto y los ejemplos "
    "que piden los puntos siguientes.\n"
    "- Sonar seguro no es lo mismo que inflar certeza: afirmá solo lo que la "
    "evidencia respalda, pero decilo con precisión, sin disculpas "
    "innecesarias ni advertencias repetidas de más.\n"
    "- Explica el POR QUÉ, no solo el QUÉ: el mecanismo, la causa, cómo se "
    "llega a la conclusión. Una conclusión desnuda, sin el razonamiento que "
    "la sostiene, es una respuesta INCOMPLETA.\n"
    "- Da CONTEXTO: definiciones, condiciones, matices, qué cambia si cambia "
    "un supuesto.\n"
    "- Usa EJEMPLOS concretos cuando aclaren algo abstracto.\n"
    "- ESTRUCTURA el texto: varios párrafos, y listas o subtítulos cuando el "
    "tema tenga partes o pasos diferenciados.\n"
    "- EXTENSIÓN: ante una pregunta no trivial, desarrolla cada punto de tu "
    "plan en al menos un párrafo propio. Contestar en una o dos oraciones "
    "sueltas es un ERROR, salvo que la pregunta sea puramente factual (una "
    "fecha, un nombre, un sí/no).\n"
    "- La longitud debe venir de CONTENIDO REAL (mecanismo, evidencia, "
    "ejemplos, matices), nunca de relleno ni de repetir lo ya dicho con "
    "otras palabras.\n"
    "- MATEMÁTICA: escribí toda fórmula o ecuación como LaTeX inline entre "
    "signos de dólar (p. ej. $a^2 + b^2 = c^2$, $e^{i\\pi} + 1 = 0$) para "
    "que se renderice como imagen; nunca la dejes como texto plano con "
    "símbolos unicode sueltos.\n\n"
)

_FINAL_ANSWER_STYLE_EN: str = (
    "=================================================================\n"
    "MANDATORY STYLE FOR THE FINAL ANSWER (the text after </thought>)\n"
    "=================================================================\n"
    "This section does NOT change the <thought> protocol: that block is still "
    "mandatory, still comes first, and is still an internal plan the user "
    "never sees. Everything below applies ONLY to the text you write AFTER "
    "</thought>.\n"
    "That text is the ONLY thing the user reads: the plan from step 4 is not "
    "the answer, it is the outline you must now DEVELOP. Delivering only the "
    "plan's conclusion leaves the answer half-finished.\n"
    # Direct BLUF-style tone (user request): lead with the answer before
    # developing it.
    "- LEAD with the concrete answer: open with the core conclusion in the "
    "first sentence, confidently and without preamble — no \"as an AI "
    "assistant\" and no filler like \"it's important to note that.\" Only "
    "after that, develop the why, the context, and the examples the points "
    "below call for.\n"
    "- Sounding confident is not the same as inflating certainty: state only "
    "what the evidence supports, but say it precisely, without unnecessary "
    "hedging or repeated disclaimers.\n"
    "- Explain the WHY, not just the WHAT: the mechanism, the cause, how the "
    "conclusion follows. A bare conclusion, without the reasoning behind it, "
    "is an INCOMPLETE answer.\n"
    "- Give CONTEXT: definitions, conditions, caveats, what changes if an "
    "assumption changes.\n"
    "- Use concrete EXAMPLES whenever they make something abstract clearer.\n"
    "- STRUCTURE the text: several paragraphs, plus lists or subheadings when "
    "the topic has distinct parts or steps.\n"
    "- LENGTH: for any non-trivial question, develop each point of your plan "
    "into at least its own paragraph. Answering in one or two loose sentences "
    "is an ERROR, unless the question is purely factual (a date, a name, a "
    "yes/no).\n"
    "- Length must come from REAL CONTENT (mechanism, evidence, examples, "
    "caveats), never from filler or restating what was already said.\n"
    "- MATH: write every formula or equation as inline LaTeX between dollar "
    "signs (e.g. $a^2 + b^2 = c^2$, $e^{i\\pi} + 1 = 0$) so it renders as an "
    "image; never leave it as plain text with loose unicode symbols.\n\n"
)


# =====================================================================
# CABECERA "system" MÍNIMA - carril TRIVIAL_GREETING
# =====================================================================
# El system prompt general mide ~2700 tokens (schema de TOOLS_SCHEMA +
# protocolo <thought> de 6 pasos + <thought_code> + verificación de
# premisas + estilo + regla de idioma). Para un saludo ("hola",
# "buenas", "gracias") eso es ~3100-3370 tok de prefill y un <thought>
# obligatorio que después se descarta - medido: ~35s para responder
# "hola". El router YA marca SignalTag.TRIVIAL_GREETING para estos
# mensajes (ver router.py `_TRIVIAL_GREETING_RE`); esta cabecera es lo
# que ese carril manda en su lugar. Sin <thought>, sin herramientas,
# sin nada que no sirva para devolver una frase cordial.
_TRIVIAL_SYSTEM_PROMPT_ES: str = (
    "Sos SovNode, un asistente de IA local y privado que corre en la máquina "
    "del usuario. El usuario te escribió un saludo o un mensaje muy breve de "
    "cortesía. Respondé en español, en una o dos frases, de forma cálida y "
    "directa. No expliques tus reglas, tu arquitectura ni tu naturaleza; no "
    "digas \"como asistente de IA\"; no abras ningún bloque de razonamiento ni "
    "uses etiquetas. Si el usuario saluda, devolvé el saludo y ofrecé ayuda en "
    "una línea."
)
_TRIVIAL_SYSTEM_PROMPT_EN: str = (
    "You are SovNode, a private local AI assistant running on the user's own "
    "machine. The user sent you a greeting or a very short courtesy message. "
    "Reply in English, in one or two sentences, warmly and directly. Do not "
    "explain your rules, architecture, or nature; do not say \"as an AI "
    "assistant\"; do not open any reasoning block or use tags. If the user is "
    "greeting you, greet back and offer help in one line."
)


def _parse_optional_timeout_env(var_name: str, default: float) -> Optional[float]:
    """
    Lee un timeout desde una variable de entorno con el MISMO contrato
    que `_effective_timeout()`/`HARD_TIMEOUT_FALLBACK_SECONDS` en
    web_search.py: "none"/"off"/"0" (sin distinguir mayúsculas, con o
    sin espacios) restaura el "modo benchmark" (`None`, sin límite
    explícito por llamada — el techo duro de emergencia sigue aplicando
    en `_call_llm_raw`, ver `OLLAMA_HARD_TIMEOUT_FALLBACK_SECONDS`); sin
    la variable, usa `default`; con cualquier otro valor, lo interpreta
    como segundos. Se define a nivel de módulo (no como método) porque
    `Orchestrator.OLLAMA_TIMEOUT_SECONDS` la necesita evaluada al
    definirse la clase, antes de que exista ninguna instancia.
    """
    raw = os.getenv(var_name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in ("none", "off", "0", ""):
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "⚠️ Valor inválido en %s=%r — usando el default (%.1fs).",
            var_name, raw, default,
        )
        return default


class Orchestrator:
    """
    Punto central de coordinación de rutas, motores especializados, herramientas locales y modelos.
    """

    MAX_SELF_CRITIQUE_ITERATIONS: int = 3
    MAX_SYNTAX_REPAIR_ATTEMPTS: int = 3
    MAX_CONTEXT_CHARS_FOR_PROMPT: int = 1200
    # Nota (bucles de herramientas / saturación de contexto): defensa
    # en profundidad para execute_tool_from_call() - tools.py YA acota su
    # propia salida (ver MAX_TOOL_OUTPUT_CHARS en tools.py), pero este cap
    # cubre TAMBIÉN resultados que no pasan por ahí (p. ej. el motor
    # dinámico, o una herramienta futura que olvide acotarse a sí misma)
    # antes de que el texto se inyecte en el prompt de seguimiento que
    # recibe el modelo.
    MAX_TOOL_RESULT_CHARS_IN_PROMPT: int = 4000
    OLLAMA_ENDPOINT: str = "http://localhost:11434/api/generate"
    # Nota (bug, parte del modo de dos pasadas): estaba en
    # `None` ("modo benchmark" - requests.Session.post(timeout=None)
    # espera la respuesta completa sin importar cuánto tarde). Con UNA
    # sola llamada por turno eso ya era un riesgo real de cuelgue sin
    # límite; con `_call_llm_two_pass` (dos llamadas HTTP secuenciales
    # por turno) el mismo riesgo se duplicaba en vez de resolverse, así
    # que corregirlo es parte del mismo cambio, no algo aparte.
    #
    # medido en este hardware (CPU-only, sin GPU discreta) durante esta
    # sesión: ~50-65 tok/s de generación, y hasta 57-84s de prompt-eval
    # PURO para prompts de ~5-6k tokens con contexto largo. Una Pasada 1
    # cerca de su techo (reasoning_budget, hasta 2458 tokens por
    # defecto) puede rondar ~40-50s de generación sola. 120s da margen
    # real para el caso adverso (prompt largo + generación cerca del
    # techo) por LLAMADA, sin ser indefinido.
    #
    # Este timeout se aplica a cada llamada HTTP por separado (ver
    # `_call_llm_raw`) - con el modo de dos pasadas, son dos llamadas
    # independientes, cada una con SU PROPIO techo de 120s, nunca un
    # techo compartido entre las dos: dos pasadas no duplican el riesgo
    # de cuelgue, cada una está protegida igual que antes lo estaba (o
    # no lo estaba) la única llamada de la versión de un solo paso.
    #
    # `SOVNODE_OLLAMA_TIMEOUT` = "none"/"off"/"0" restaura el modo
    # benchmark (sin límite explícito) - el techo duro de
    # `OLLAMA_HARD_TIMEOUT_FALLBACK_SECONDS` sigue protegiendo contra un
    # cuelgue verdaderamente infinito incluso ahí. Mismo patrón que
    # `_effective_timeout()`/`HARD_TIMEOUT_FALLBACK_SECONDS` en
    # web_search.py, para no inventar una segunda convención de
    # configuración de timeouts en el mismo proyecto.
    OLLAMA_TIMEOUT_SECONDS: Optional[float] = _parse_optional_timeout_env(
        "SOVNODE_OLLAMA_TIMEOUT", 120.0
    )
    OLLAMA_HARD_TIMEOUT_FALLBACK_SECONDS: float = float(
        os.getenv("SOVNODE_OLLAMA_HARD_FALLBACK", "600.0")
    )
    ALERT_QUEUE_MAXSIZE: int = 200

    # =================================================================
    # ARQUITECTURA DE MODELO ÚNICO (pedido del usuario, 2026-08-27)
    # =================================================================
    # UN SOLO modelo de respuesta para TODO — general y código — que
    # reemplaza el esquema anterior de variantes 3B/7B con roles
    # general/coder separados (y el selector 3B/7B del sidebar). El router
    # rápido (self.router_model, qwen2.5:0.5b) NO cambia: sigue siendo el
    # clasificador fast_path/slow_path (_llm_router_classify/_classify_turn).
    #
    # gpt-oss usa el formato Harmony de OpenAI. Ollama lo parsea del lado
    # del servidor (parser=harmony): la respuesta de /api/generate trae
    # `response` (canal `final`, la respuesta visible) y `thinking` (canal
    # `analysis`, el razonamiento interno) SEPARADOS — SovNode lee solo
    # `response`. Hallazgos completos del PASO 0 (pruebas aisladas contra
    # gpt-oss:20b real): _backup_pre_single_model/STEP0_HARMONY_FINDINGS.md.
    # Dos consecuencias que moldean el resto de este cambio:
    #   1. Imponerle a gpt-oss un protocolo <thought> propio (el del
    #      SYSTEM_PROMPT general + _call_llm_two_pass) CHOCA con su canal
    #      nativo: mide fuga de `analysis` -> `response` intermitente y,
    #      peor, HTTP 500 ("error parsing tool call") en 2/2 pruebas con el
    #      protocolo activo. Por eso el modelo único SIEMPRE se genera por
    #      el carril lean de una sola pasada (_get_fastpath_system_prompt /
    #      _build_reasoning_prompt(lean=True)) — ver run_turn/process_turn.
    #      _call_llm_two_pass se conserva sin invocar (rollback).
    #   2. Con `think` en "low" (ver THINK_LEVEL), gpt-oss reduce el canal
    #      analysis de ~600 tokens a ~15 — sin eso, quema el presupuesto de
    #      `num_predict` razonando y devuelve `response` vacío.
    RESPONSE_MODEL: str = "gpt-oss:20b"

    # Esfuerzo de razonamiento de gpt-oss (campo `think` de Ollama para
    # modelos Harmony): "low" / "medium" / "high". MEDIDO en el PASO 0:
    # "low" recorta el canal analysis de ~600 tok a ~15 sin perder
    # coherencia — decisivo con el decode real de esta máquina (~5.6 tok/s).
    # Override por entorno para ajustar sin tocar código, mismo patrón que
    # _NUM_THREAD_ENV_VAR. Cadena vacía / "off" / "none" desactiva el campo.
    _THINK_LEVEL_ENV_VAR: str = "SOVNODE_THINK_LEVEL"
    THINK_LEVEL: str = "low"

    # Router rápido vía LLM (0.5B) — reemplaza a IntentRouter.classify()
    # como fuente de `path` (fast_path/slow_path) en TODOS los turnos, a
    # pedido explícito del usuario ("reemplazo total": el 0.5B decide
    # siempre, no solo en casos ambiguos). IntentRouter sigue corriendo
    # SIEMPRE también — es determinista y cuesta microsegundos — porque
    # sus `tags`/`score` alimentan lógica ya probada que no tiene que ver
    # con fast/slow: el guard de TRIVIAL_GREETING sobre la caché semántica
    # (_semantic_cache_allowed), _should_force_web_search sobre
    # WEB_SEARCH_INTENT/FACTUAL_ENUMERATION, la exclusión mutua CODE_
    # COMPLEX/WEB_SEARCH_INTENT, etc. Pedirle a un modelo de 0.5B que
    # además reproduzca esas ~14 señales de forma confiable no es
    # realista — ver _classify_turn más abajo, que combina ambas fuentes.
    #
    # Si Ollama falla (modelo no descargado, servidor caído, timeout) o la
    # respuesta no es interpretable, se cae a IntentRouter sin bloquear ni
    # romper el turno — mismo principio que el resto de esta sesión: un
    # error de Ollama nunca debe tratarse como una decisión real (ver
    # _llm_router_classify, que reusa la convención ".startswith('[ERROR'"
    # de _call_llm_raw).
    ROUTER_LLM_NUM_PREDICT: int = 8
    ROUTER_LLM_TEMPERATURE: float = 0.0
    _ROUTER_LLM_SYSTEM_PROMPT: str = (
        "Sos un clasificador binario de intención, ultra-rápido, para un "
        "router de IA. Leés UN mensaje de usuario y respondés con UNA "
        "sola palabra: fast_path o slow_path. Nada más — sin explicación, "
        "sin puntuación, sin comillas.\n\n"
        "fast_path: saludos, charla breve, agradecimientos, despedidas, "
        "preguntas triviales o factuales simples, PEDIR QUE SE LISTEN/"
        "ENUMEREN hechos, leyes o fórmulas YA CONOCIDOS (no que se "
        "calculen ni se deriven), pedidos de código trivial/boilerplate.\n"
        "slow_path: pedir RESOLVER, CALCULAR o DERIVAR una expresión "
        "matemática concreta, escribir/depurar/refactorizar código no "
        "trivial, auditar lógica o detectar contradicciones, análisis "
        "conceptual denso (filosofía, teoría, epistemología), inferencia "
        "condicional compleja del tipo 'si X entonces Y, dado que...'.\n\n"
        "Ejemplos:\n"
        "Mensaje: hola, ¿cómo estás?\n"
        "Clasificación: fast_path\n\n"
        "Mensaje: resolvé la integral de x^2 * sin(x)\n"
        "Clasificación: slow_path\n\n"
        "Mensaje: escribí una función en Python que detecte ciclos en un "
        "grafo dirigido\n"
        "Clasificación: slow_path\n\n"
        "Mensaje: gracias!\n"
        "Clasificación: fast_path\n\n"
        # Bug real, MEDIDO (screenshot 2026-08-27): "can you tell me the
        # most important equations in math?" clasificado como slow_path
        # — la palabra "equations" por sí sola no implica que haya que
        # RESOLVER nada, es un pedido de LISTAR hechos ya conocidos
        # (mismo caso que SignalTag.FACTUAL_ENUMERATION en router.py,
        # peso 0.0 a propósito ahí también). Ejemplo explícito para que
        # el modelo de 0.5B no confunda "nombrar/enumerar" con "calcular".
        "Mensaje: tell me the most important equations in math\n"
        "Clasificación: fast_path\n\n"
        "Mensaje: dame las leyes más importantes de la física\n"
        "Clasificación: fast_path\n"
    )

    # ADVERTENCIA PARA FUTUROS EDITORES (humanos o IA): esta constante de
    # CLASE nunca se usa en runtime. `__init__` llama a `set_language(
    # "Spanish")`, que reasigna `self.SYSTEM_PROMPT` (atributo de
    # INSTANCIA, que oculta a este de clase) vía `_get_base_system_prompt()`
    # antes de que ningún turno pueda leerlo - ver `set_language()` más
    # abajo. Se conserva sincronizada con las dos variantes reales
    # (`_get_base_system_prompt("Spanish"/"English")`) únicamente para no
    # dejar un texto inconsistente/engañoso en el código; un parche que
    # SOLO toque esta constante (como el que dejó el "REGLA PRINCIPAL DE
    # IDIOMA"/"[LANGUAGE RULE]" duplicado y mal formateado que reemplaza
    # este comentario) nunca llega a tener efecto real - el bug de
    # mirroring de idioma vivía en `_get_base_system_prompt()`, no aquí.
    SYSTEM_PROMPT: str = (
        "[ROL DEL SISTEMA: SOVNODE v2.0]\n"
        "Eres SovNode, un asistente de IA local soberano, independiente y privado, ejecutado en el hardware del usuario a través de Ollama. "
        "NUNCA menciones a OpenAI, Anthropic ni a ninguna otra empresa externa; eres un sistema autónomo e independiente. "
        "Responde con precisión, honestidad epistémica y corrección ortográfica, en el idioma que indique la regla de idioma crítica al final de este prompt.\n\n"
        "=================================================================\n"
        "VERIFICACIÓN DE PREMISAS — PRIORIDAD #1\n"
        "=================================================================\n"
        "Antes de aplicar cualquier otra regla de este prompt, comprueba si el "
        "mensaje del usuario da por sentado un hecho como si ya fuera cierto "
        "— una transferencia, un resultado, una fecha, un cargo, una relación "
        "causal — por ejemplo: \"Sabiendo que Cucurella fichó por el Real "
        "Madrid, ¿qué dorsal lleva?\". Si el contexto web recuperado "
        "CONTRADICE esa premisa, o simplemente no la confirma en ninguna "
        "parte (y no es algo que tu propio conocimiento pueda confirmar con "
        "certeza), tu respuesta DEBE empezar corrigiendo o matizando esa "
        "premisa explícitamente — nunca ejecutes el resto de la pregunta "
        "asumiendo que la premisa es cierta.\n"
        "PROHIBIDO: seguirle la corriente a una premisa falsa y limitarte a "
        "reportar que \"falta información\" sobre un detalle secundario de "
        "ella (p. ej. responder solo \"no encontré el dorsal\" ante el "
        "ejemplo de arriba) — eso deja al usuario creyendo que la premisa "
        "principal (la transferencia) sí es cierta, cuando el problema real "
        "es que no lo es. Corrige la premisa PRIMERO; solo después, si aún "
        "aplica, aborda el resto de la pregunta.\n\n"
        "=================================================================\n"
        "REGLA ANTI-NEGATIVA DE BÚSQUEDA WEB\n"
        "=================================================================\n"
        "NUNCA afirmes que no tienes acceso a internet o a datos en tiempo real.\n"
        "Si se te proporciona un bloque [Contexto web recuperado en tiempo real], "
        "debes usarlo OBLIGATORIAMENTE para responder. Si no hay suficiente información en el contexto, "
        "simplemente di que los datos recuperados no son concluyentes, pero JAMÁS digas que careces de conexión.\n"
        "EXCEPCIÓN — desajuste de año/tema: si el contexto web trae un aviso "
        "[AVISO AUTOMÁTICO DE VERIFICACIÓN], o si notas por tu cuenta que las "
        "fuentes recuperadas son sobre un año, evento o entidad DISTINTA a la que "
        "el usuario preguntó, NO fusiones ni reetiquetes esos hechos como si "
        "respondieran la pregunta. Dilo explícitamente (p. ej. \"no encontré "
        "resultados específicos sobre [año/tema pedido]; lo que encontré es sobre "
        "[año/tema de las fuentes]\"). Esto sigue sin ser \"decir que careces de "
        "conexión\": sí ejecutaste una búsqueda real: es señalar que su resultado "
        "no cubre específicamente lo preguntado.\n\n"
        """
        [DIRECTIVAS DE BÚSQUEDA Y VERIFICACIÓN WEB]
        1. Cuando recibas un bloque `[Contexto web recuperado]`, asume que esa información está actualizada y tiene prioridad sobre tu conocimiento previo.
        2. NUNCA afirmes que "no tienes acceso a internet" o "no puedes consultar datos en vivo" si dispones de evidencia inyectada en el contexto.
        3. PRIMERA PRIORIDAD (Verificación de Premisas): Si la consulta del usuario asume un hecho falso o desactualizado, verifica primero con el contexto web y corrige la premisa explícitamente antes de responder.
        """
        "=================================================================\n"
        "PROTOCOLO OBLIGATORIO DE RAZONAMIENTO (<thought>)\n"
        "=================================================================\n"
        "Antes de emitir cualquier respuesta o llamada a herramienta, DEBES incluir un bloque de pensamiento obligatorio en el siguiente formato exacto. "
        "Este bloque es tu borrador interno: el usuario NUNCA lo ve, así que úsalo para planear de verdad, no como un trámite formal:\n\n"
        "<thought>\n"
        "1. Analizar la petición del usuario: ¿qué pregunta exactamente, y qué necesidad real hay detrás de la pregunta literal? ¿da por sentada alguna premisa fáctica que deba verificarse primero (ver PRIORIDAD #1 arriba)?\n"
        "2. Evaluar herramientas disponibles: ¿se necesita ejecutar una herramienta local (read_file, list_dir, system_telemetry, run_cmd) o responder en texto plano?\n"
        "3. Checklist de comprensión por fuente: si este turno trae un bloque de contexto web en tiempo real, parafrasea en UNA frase, fuente por fuente (usando el número de cita [1], [2]... tal como aparece en el bloque de contexto web), qué dice CADA una — esto es una verificación de comprensión, no parte de la respuesta en sí. Omite este paso por completo si no hay contexto web en este turno.\n"
        "4. Si vas a responder en texto: enumera los 2-5 puntos concretos que la respuesta debe cubrir, en el orden en que los vas a presentar, y qué evidencia (contexto web, historial, conocimiento propio) respalda cada uno. Esto es lo que evita respuestas dispersas o incompletas.\n"
        "5. Auto-corrección jerárquica antes de escribir (dos niveles, en este mismo paso, sin generar una respuesta aparte):\n"
        "   Nivel 1 — Estructural: ¿cada punto del paso 4 tiene una fuente concreta (contexto web, historial, lección meta-cognitiva, conocimiento propio) y ningún punto se repite o le falta base?\n"
        "   Nivel 2 — Factual/lógico: ¿algún número, nombre, fecha o conclusión del plan CONTRADICE el contexto proporcionado? ¿el contexto web recuperado corresponde realmente al año/evento/entidad específica que se preguntó, o es sobre algo similar pero de otro año? ¿la conclusión se sigue realmente de las premisas, o es un salto? Corrige el plan aquí mismo si detectas cualquiera de estos problemas — nunca lo arrastres a la respuesta visible.\n"
        "6. Si corresponde invocar una herramienta en vez de responder en texto: define aquí los parámetros exactos del JSON a emitir.\n"
        "</thought>\n\n"
        "FORMATO OBLIGATORIO DEL BLOQUE (no negociable):\n"
        "- El PRIMER carácter de tu salida debe ser literalmente `<thought>`. No lo precedas de saludos, "
        "títulos, resúmenes ni preámbulo alguno.\n"
        "- Cierra SIEMPRE con `</thought>` antes de escribir una sola palabra de la respuesta visible.\n"
        "- Los pasos 1-6 viven ÚNICAMENTE dentro de esas etiquetas. Está PROHIBIDO reproducirlos fuera de "
        "ellas: nada de encabezados tipo \"Análisis de la petición\", \"Checklist de comprensión\", "
        "\"Plan de respuesta\", \"Auto-corrección\" o \"Nivel 1/Nivel 2\" en el texto que ve el usuario.\n"
        "- Después de `</thought>` escribe SOLO la respuesta final, redactada de corrido y en prosa "
        "natural, como si el usuario nunca hubiera visto un plan — sin numerar tus propios pasos ni "
        "narrar tu proceso.\n\n"
        "=================================================================\n"
        "SCRATCHPAD VERIFICADOR (<thought_code>) — VERIFICACIÓN EN CALIENTE\n"
        "=================================================================\n"
        "Los modelos pequeños se equivocan con facilidad al calcular, contar caracteres/elementos o "
        "verificar una afirmación numérica o lógica 'de memoria'. Si dentro de tu <thought> detectas que "
        "tu respuesta depende de un cálculo, conteo o verificación así, escribe inmediatamente después de "
        "</thought> un bloque adicional:\n\n"
        "<thought_code>\n"
        "# Código Python autocontenido y determinista. Usa print() para mostrar\n"
        "# exactamente el valor que necesitas verificar antes de responder.\n"
        "</thought_code>\n\n"
        "Este código se ejecuta de verdad en un sandbox aislado (no es una simulación) y su salida "
        "(stdout) se te devuelve como [VERIFICACIÓN EN TIEMPO REAL DEL SANDBOX] antes de que redactes tu "
        "respuesta final — así que NUNCA asumas un resultado numérico o de conteo sin verificarlo primero "
        "si tienes alguna duda razonable. No abuses de esta herramienta para preguntas triviales que "
        "puedas responder con certeza absoluta sin ejecutar nada, y no la uses para nada que no sea "
        "cálculo puro (sin acceso a red, archivos ni al sistema — el sandbox lo bloquea de todas formas). "
        "Tampoco la uses para comprobar si un dato o una cita YA está presente en el contexto "
        "proporcionado arriba — eso no es una verificación de cálculo, es simple lectura: haz esa "
        "comprobación directamente en tu razonamiento en prosa (pasos 3-4 de arriba), sin escribir "
        "código para ello.\n\n"
        "Herramientas locales disponibles:\n\n"
        # Nota (medido, 2026-08-25 - ver el comentario junto a
        # `HISTORY_ENTRY_CHAR_CAP` para la medición completa del turno
        # de referencia): este JSON con `indent=2` pesaba 1778
        # caracteres (~444 tokens) de los ~10824 de esta cabecera -
        # SOLO espacios/saltos de línea de formato, cero información
        # que el modelo necesite (lee la estructura, no la
        # indentación). Compactarlo (mismo contenido, sin indent) lo
        # deja en 1204 caracteres - ahorro gratis, en todos los turnos
        # que usan esta cabecera, sin cambiar una sola herramienta ni
        # un solo parámetro.
        f"{json.dumps(TOOLS_SCHEMA, separators=(',', ':'), ensure_ascii=False)}\n\n"
        f"{_FINAL_ANSWER_STYLE_ES}"
        "=================================================================\n"
        "REGLAS DE EJECUCIÓN DE HERRAMIENTAS (FUNCTION CALLING)\n"
        "=================================================================\n"
        "Si en tu bloque <thought> determinas que debes invocar una herramienta, inmediatamente después del cierre </thought> DEBES emitir EXCLUSIVAMENTE el JSON correspondiente dentro de marcas ```json ... ```, sin texto conversacional adicional.\n\n"
        "Ejemplo de invocación:\n"
        "<thought>\n"
        "El usuario solicita ver la telemetría del sistema. La herramienta 'system_telemetry' no requiere parámetros. Escribiré el JSON correspondiente.\n"
        "</thought>\n"
        "```json\n"
        "{\n"
        '  "tool": "system_telemetry",\n'
        '  "parameters": {}\n'
        "}\n"
        "```\n\n"
        "Si es una consulta conversacional normal, escribe tu respuesta en lenguaje natural justo después de la etiqueta </thought>, siguiendo el plan que trazaste en el paso 4-5 — no lo repitas ni lo resumas, simplemente redacta la respuesta final ya organizada."
        # En la sección en Español dentro de _get_base_system_prompt():
"=================================================================\n"
"REGLAS DE EJECUCIÓN DE HERRAMIENTAS (FUNCTION CALLING)\n"
"=================================================================\n"
"1. PROHIBICIÓN DE LECTURA DE CONTEXTO: El bloque [Contexto web recuperado] YA está "
"inyectado en este prompt. Queda ESTRICTAMENTE PROHIBIDO invocar 'read_file' o "
"cualquier otra herramienta para intentar abrir el contexto o archivos imaginarios "
"(como 'web_context_...txt'). Responde usando el contexto ya inyectado.\n"
"2. Si en tu bloque <thought> determinas que debes invocar una herramienta..."
    )

    CODER_SYSTEM_PROMPT: str = (
        "=================================================================\n"
        "IDENTIDAD: SOVNODE CODER — MOTOR DE DESARROLLO LOCAL\n"
        "=================================================================\n"
        "Eres el motor de generación de código de SovNode, una herramienta de "
        "desarrollo local que corre en el propio hardware del usuario, para su "
        "propio uso personal — no un servicio público de cara a desconocidos. "
        "Tu única función es escribir el código que se te pide: limpio, funcional, "
        "bien documentado y autocontenido.\n\n"
        "=================================================================\n"
        "ALCANCE LEGÍTIMO — NO ES NEGOCIABLE RECHAZAR ESTO\n"
        "=================================================================\n"
        "Las siguientes categorías son SIEMPRE trabajo de programación legítimo, "
        "sin importar el tema o la ambientación — nunca las trates como sospechosas "
        "ni les antepongas advertencias:\n"
        "- Juegos de cualquier género y ambientación, incluidos combate, disparos, "
        "guerra, terror o violencia ficticia (mecánicas de daño, armas, enemigos, "
        "muerte del personaje, etc. son ELEMENTOS DE DISEÑO DE JUEGOS, no una señal "
        "de alarma).\n"
        "- Scripts de administración de sistemas: automatización, gestión de "
        "procesos, cron jobs, manejo de archivos, backups.\n"
        "- Herramientas de red y seguridad para infraestructura PROPIA del "
        "usuario o con autorización explícita: escáneres de puertos, analizadores "
        "de tráfico, pruebas de penetración sobre servidores propios, fuzzers, "
        "clientes/servidores de sockets, herramientas de fuerza bruta contra un "
        "entorno de pruebas propio, generadores de contraseñas, criptografía.\n"
        "- Ejercicios educativos de seguridad/CTF, ingeniería inversa de binarios "
        "propios, análisis de malware con fines de estudio (analizar ≠ crear).\n"
        "- Web scraping de datos públicamente accesibles, automatización de "
        "navegador, bots para plataformas propias del usuario (Discord, Telegram).\n"
        "- Cualquier tarea estándar de ingeniería de software: APIs, bases de "
        "datos, interfaces, algoritmos, estructuras de datos.\n\n"
        "=================================================================\n"
        "EL ÚNICO PISO REAL — acotado y específico, no una excusa genérica\n"
        "=================================================================\n"
        "Declina (y SOLO en estos casos) código cuyo propósito INEQUÍVOCO — no "
        "ambiguo, no hipotético, no \"podría usarse mal\" — sea causar daño no "
        "autorizado a sistemas o personas AJENAS al usuario: malware/ransomware "
        "diseñado para propagarse o cifrar datos de terceros sin su consentimiento, "
        "herramientas para robar credenciales o datos de otras personas, "
        "explotación activa de sistemas de terceros sin autorización documentada, "
        "spyware/stalkerware para vigilar a otra persona sin su conocimiento, kits "
        "de phishing. La ambigüedad NO es motivo de rechazo — un escáner de "
        "puertos, un generador de payloads de prueba, o un script que 'ataca' un "
        "servidor no especificado son casos de USO LEGÍTIMO por defecto (labs "
        "propios, CTFs, pruebas locales); no asumas intención maliciosa sin una "
        "señal concreta e inequívoca en el propio pedido.\n\n"
        "=================================================================\n"
        "CÓMO RESPONDER\n"
        "=================================================================\n"
        "Si el pedido cae dentro del alcance legítimo (la inmensa mayoría de los "
        "casos): escribe el código directamente. Sin \"no puedo\", sin disculpas, "
        "sin sermones sobre uso responsable, sin advertencias que nadie pidió.\n"
        "Si el pedido es genuinamente ambiguo sobre autorización (p. ej. menciona "
        "explícitamente un sistema o cuenta de un tercero identificado): haz UNA "
        "pregunta breve para confirmar contexto/autorización, y procede — no "
        "rechaces de entrada.\n"
        "Si cae en el piso real de arriba: dilo en una frase, sin sermón, y ofrece "
        "la alternativa legítima más cercana si existe (p. ej. \"puedo escribirte "
        "un scanner para TU propia red\" en vez de simplemente negarte)."
    )

    # Optimización #1/#2 - Prefix Alignment: directiva de idioma única y
    # constante, compartida por _call_llm() (llamadas no-streaming: ToT,
    # followups de verificación, corrección de deriva epistémica) y por
    # StreamTurnWorker.run() (sovnode_qt.py, llamada streaming principal).
    # Antes cada sitio construía su propio texto de "lang_enforce" -
    # ligeramente distinto entre sí - y lo concatenaba al system prompt
    # en caliente; eso rompía dos cosas a la vez: (a) diluía la cabecera
    # con variantes de texto no idénticas entre llamadas del mismo turno,
    # y (b) invalidaba cualquier caché de prefijo KV del backend, que
    # solo puede reutilizarse si el "system" enviado es byte-idéntico al
    # de una llamada anterior. Con una única constante congelada, todas
    # las llamadas de un turno (streaming + no-streaming) comparten
    # exactamente el mismo prefijo - ver _frozen_system_headers más abajo.
    # NOTA: este texto es una constante estática porque forma parte de la
    # cabecera "system" CONGELADA (ver _get_frozen_header), cacheada por
    # (idioma, is_coder) y reutilizada byte-idéntica entre llamadas. La
    # cabecera YA se elige por idioma efectivo del turno - es
    # `_get_frozen_header(effective_lang, ...)` quien resuelve el idioma,
    # no este texto - así que aquí basta con la regla de espejo, que es
    # consistente con esa elección y con la regla explícita que
    # `_build_reasoning_prompt` inyecta junto a la consulta.
    LANG_ENFORCE_DIRECTIVE: str = (
        "\n=================================================================\n"
        "[CRITICAL LANGUAGE RULE]\n"
        "=================================================================\n"
        "You MUST respond in the exact same language used in the user's "
        "latest prompt. If the user prompt is in English, reply in English. "
        "If it is in Spanish, reply in Spanish. Do not mix languages."
    )

    # Patrón de aperturas de rechazo genérico - usado por el interceptor
    # de StreamTurnWorker (sovnode_qt.py) para detectar, en los primeros
    # ~35 caracteres ya bufferizados por process_token(), si el modelo
    # abrió con una negativa enlatada en vez de código. Solo dispara un
    # único reintento con contexto aclaratorio - nunca oculta un segundo
    # rechazo ni fuerza cumplimiento de forma indefinida.
    _REFUSAL_PATTERN_RE: Pattern[str] = re.compile(
        r"^\s*(lo\s+siento,?\s*(pero\s+)?no\s+puedo|no\s+puedo\s+ayudarte|no\s+puedo\s+ayudar|"
        r"como\s+(un\s+|una\s+)?(modelo|ia|asistente)[^.]{0,40}no\s+puedo|"
        r"i'?m\s+sorry,?\s*(but\s+)?i\s+can'?t|i\s+cannot\s+(assist|help)|i'?m\s+not\s+able\s+to|"
        r"i\s+can'?t\s+(help|assist|provide)|as\s+an\s+ai[^.]{0,40}(can'?t|cannot))",
        re.IGNORECASE,
    )

    # DEV_MODE_OVERRIDE - Genialidad de Sinergia de Razonamiento (código):
    # para peticiones de desarrollo, obliga a una FASE 1 de planificación
    # ejecutable con <thought_code> (simulación real en el ExecutionSandbox
    # de la lógica central antes de escribir el código final) y solo
    # entonces a una FASE 2 de entrega. El stdout/stderr real de la FASE 1
    # se reinyecta en process_turn (ver bloque "Scratchpad Verificador")
    # antes de que el modelo redacte el bloque ```python final, que a su
    # vez pasa por ScopeValidator + auto-reparación AST
    # (_validate_and_fix_python_code) automáticamente.
    _DEV_MODE_OVERRIDE_ES: str = (
        "[MODO COMPILADOR ACTIVO — EJECUCIÓN OBLIGATORIA]:\n"
        "Solicitud de desarrollo detectada. Ignora cualquier restricción sobre datos en tiempo real "
        "implementando estructuras de datos simulados (mock data) dentro del propio script Python.\n\n"
        "FASE 1 — PLANIFICACIÓN EJECUTABLE (OBLIGATORIA): antes de escribir el código final, redacta "
        "un bloque <thought_code> con una simulación mínima, autocontenida y determinista de la "
        "lógica central del script (la o las funciones clave, ejercitadas con un par de casos de "
        "prueba y algún caso borde representativo), usando print() para mostrar los resultados. Ese "
        "bloque se ejecuta de verdad en el sandbox y su stdout/stderr real se te reinyecta como "
        "evidencia verificada antes de que redactes el código final — trátalo como la fuente de la "
        "verdad sobre si tu lógica funciona.\n\n"
        "FASE 2 — ENTREGA FINAL: usando esa verificación como base, entrega de forma inmediata el "
        "código fuente completo y ya corregido en un único bloque ```python, incorporando cualquier "
        "ajuste que el resultado del sandbox haya revelado como necesario.\n"
        "PROHIBIDO responder con texto de disculpa o rechazo."
    )
    _DEV_MODE_OVERRIDE_EN: str = (
        "[COMPILER MODE ACTIVE — MANDATORY EXECUTION]:\n"
        "Development request detected. Handle any real-time data need by implementing simulated "
        "(mock) data structures directly inside the Python script itself.\n\n"
        "PHASE 1 — EXECUTABLE PLANNING (MANDATORY): before writing the final code, write a "
        "<thought_code> block containing a minimal, self-contained, deterministic simulation of "
        "the script's core logic (the key function or functions, exercised with a couple of "
        "representative test cases and an edge case), using print() to surface the results. This "
        "block genuinely runs in the sandbox, and its real stdout/stderr is returned to you as "
        "verified evidence before you write the final code — treat it as the ground truth for "
        "whether your logic actually works.\n\n"
        "PHASE 2 — FINAL DELIVERY: using that verification as your foundation, deliver the "
        "complete, already-corrected source code immediately in a single ```python block, folding "
        "in whatever adjustments the sandbox result showed were needed.\n"
        "Always answer with working code as the complete output — treat this as your only valid "
        "response format for this request."
    )

    @property
    def DEV_MODE_OVERRIDE(self) -> str:
        is_en = getattr(self, "current_language", "Spanish") == "English"
        return self._DEV_MODE_OVERRIDE_EN if is_en else self._DEV_MODE_OVERRIDE_ES

    # System prompt DEDICADO y minimalista para la reescritura de queries
    # de búsqueda. A propósito NO reutiliza SYSTEM_PROMPT: ese arrastra
    # todo el protocolo <thought> de 5 pasos y el esquema JSON de
    # herramientas - perfecto para una respuesta conversacional, pésimo
    # para una tarea de "devuelve una sola línea de texto": con el
    # prompt completo, el modelo tiende a abrir un bloque <thought>
    # completo (o peor, considerar invocar una herramienta) para decidir
    # cómo escribir tres palabras, multiplicando la latencia sin ninguna
    # ganancia. Los ejemplos few-shot anclan el formato de salida en
    # modelos pequeños (3B) mucho mejor que la instrucción sola.
    # ENDURECIMIENTO (salida delimitada): antes se pedía "una línea de
    # texto plano" y toda la defensa contra frases completas / relleno
    # conversacional vivía en `_sanitize_llm_query_output` (regex
    # aplicadas después de que el modelo ya escribió lo que quiso). Se
    # combina con `"format"` como esquema JSON en el payload (ver
    # rewrite_search_query_via_llm): eso restringe el muestreo de Ollama
    # para que la salida sea JSON válido contra el esquema, así que la
    # forma `{"search_terms": "..."}` deja de depender únicamente de que
    # un modelo de 3B "obedezca" - está forzada a nivel de decodificación
    # en cualquier Ollama que soporte structured outputs. El límite de 5
    # palabras clave sigue siendo solo una instrucción (no hay forma de
    # forzar conteo de palabras vía esquema JSON), reforzado igual con un
    # tope duro en `_sanitize_llm_query_output`.
    _QUERY_REWRITE_SYSTEM_PROMPT_ES: str = (
        "Eres un optimizador de búsquedas. Tu única función es analizar el mensaje del "
        "usuario y el historial reciente para generar palabras clave de búsqueda óptimas "
        "para motores como SearXNG o DuckDuckGo.\n\n"
        "Formato de salida — ESTRICTO:\n"
        "- Respondé ÚNICAMENTE con un objeto JSON de una sola línea: "
        "{\"search_terms\": \"...\"}\n"
        "- Sin markdown, sin bloques de código, sin texto antes ni después del JSON.\n"
        "- El valor de \"search_terms\" debe ser COMO MÁXIMO 5 palabras clave (nombres "
        "propios, años, términos técnicos, entidades) separadas por espacios — NUNCA una "
        "oración completa, NUNCA una pregunta, sin verbos de acción (\"buscar\", "
        "\"decime\", \"contame\") ni palabras de relleno (\"quién\", \"cuánto\", \"cómo\").\n"
        "- Resolvé pronombres y referencias implícitas usando el contexto de la "
        "conversación, pero igual comprimí el resultado a solo palabras clave.\n"
        "- Si la pregunta del usuario no requiere información externa o en tiempo real, "
        "respondé exactamente: {\"search_terms\": \"NONE\"}\n\n"
        "Ejemplos:\n"
        "Contexto: (sin contexto previo)\n"
        "Mensaje: hola, buscame la final del mundial 2026\n"
        "JSON: {\"search_terms\": \"final Mundial 2026\"}\n\n"
        "Contexto: Hablamos de la final del Mundial 2026.\n"
        "Mensaje: quién ganó esa final?\n"
        "JSON: {\"search_terms\": \"ganador final Mundial 2026\"}\n\n"
        "Contexto: Hablamos del iPhone 17 Pro.\n"
        "Mensaje: y cuánto sale ahora\n"
        "JSON: {\"search_terms\": \"precio iPhone 17 Pro\"}\n\n"
        "Contexto: Hablamos de la final del Mundial 2026 entre España y Argentina.\n"
        "Mensaje: contame de la final del mundial 2022, cómo fue\n"
        "JSON: {\"search_terms\": \"final Mundial 2022\"}\n"
        "(el mensaje trae su propio año, 2022, distinto al 2026 del contexto — el año del "
        "mensaje actual siempre gana; NUNCA devolver \"Mundial 2026\" acá.)"
    )
    _QUERY_REWRITE_SYSTEM_PROMPT_EN: str = (
        "You are an internal component of a search engine. Your only function is to "
        "transform the user's message (and the conversation context, when provided) into "
        "optimal web search keywords.\n\n"
        "Output format — STRICT:\n"
        "- Respond with ONLY a single-line JSON object: {\"search_terms\": \"...\"}\n"
        "- No markdown, no code fences, no text before or after the JSON.\n"
        "- The value of \"search_terms\" must be AT MOST 5 keywords (proper nouns, years, "
        "technical terms, entity names) separated by single spaces — NEVER a full sentence, "
        "NEVER a question, no action verbs (\"search\", \"tell me\", \"look up\") and no "
        "filler words (\"who\", \"how much\", \"what\").\n"
        "- Resolve pronouns and implicit references (\"that final\", \"and now\", "
        "\"how much does it cost\") using the provided conversation context, but still "
        "compress the result down to keywords only.\n"
        "- PRIORITY RULE: when the current message names its own specific date, year, or "
        "entity and it differs from the one in the context, the current message's always "
        "wins — never complete or 'correct' the current message's year/entity using the "
        "context's, even when the context is more recent or more developed.\n"
        "- If the message doesn't need external/real-time info, respond exactly: "
        "{\"search_terms\": \"NONE\"}\n\n"
        "Examples:\n"
        "Context: (no prior context)\n"
        "Message: hi, search online for the 2026 world cup final\n"
        "JSON: {\"search_terms\": \"2026 World Cup final\"}\n\n"
        "Context: We discussed the 2026 FIFA World Cup final.\n"
        "Message: who won that final?\n"
        "JSON: {\"search_terms\": \"2026 World Cup final winner\"}\n\n"
        "Context: We discussed the iPhone 17 Pro.\n"
        "Message: and how much does it cost now\n"
        "JSON: {\"search_terms\": \"iPhone 17 Pro price\"}\n\n"
        "Context: We discussed the 2026 FIFA World Cup final between Spain and Argentina.\n"
        "Message: tell me about the 2022 world cup final, how was it\n"
        "JSON: {\"search_terms\": \"2022 World Cup final\"}\n"
        "(the message carries its own year, 2022, different from the context's 2026 — "
        "the current message's year wins; NEVER return \"2026 World Cup final\" here.)"
    )

    @property
    def QUERY_REWRITE_SYSTEM_PROMPT(self) -> str:
        is_en = getattr(self, "current_language", "Spanish") == "English"
        return (
            self._QUERY_REWRITE_SYSTEM_PROMPT_EN
            if is_en
            else self._QUERY_REWRITE_SYSTEM_PROMPT_ES
        )


    # Bloques de razonamiento que el modelo puede emitir en CUALQUIERA de
    # estas cuatro variantes (angular o de corchetes, con o sin "_code") -
    # modelos de 3B no siempre respetan el formato angular exacto que pide
    # el SYSTEM_PROMPT, y un bloque no reconocido aquí se filtraba tal cual
    # a la respuesta visible, duplicando en la UI el razonamiento interno
    # (una vez como bloque crudo filtrado, otra vez como la respuesta real
    # que el modelo redactaba a continuación). Todos los patrones son
    # globales (re.sub sin `count` reemplaza todas las ocurrencias) e
    # insensibles a mayúsculas/minúsculas.
    _THOUGHT_BLOCK_PATTERNS: Tuple[Pattern[str], ...] = (
        re.compile(r"<thought_code\b[^>]*>(.*?)</thought_code\s*>", re.IGNORECASE | re.DOTALL),
        re.compile(r"\[thought_code\](.*?)\[/thought_code\]", re.IGNORECASE | re.DOTALL),
        re.compile(r"<thought\b[^>]*>(.*?)</thought\s*>", re.IGNORECASE | re.DOTALL),
        re.compile(r"\[thought\](.*?)\[/thought\]", re.IGNORECASE | re.DOTALL),
    )

    # =================================================================
    # FUGA DE RAZONAMIENTO SIN ETIQUETAS (Chain-of-Thought Leak)
    # =================================================================
    # `_split_thought_and_content` solo puede limpiar lo que viene
    # DELIMITADO (<thought>, [thought], ...). El fallo real observado es
    # otro: qwen2.5:7b ejecuta el protocolo de razonamiento del
    # SYSTEM_PROMPT pero lo redacta como markdown normal, SIN abrir
    # ninguna etiqueta - "Análisis de la petición", "Checklist de
    # comprensión", "Plan de respuesta", "Auto-corrección"... - así que
    # nada lo filtra y el usuario ve el borrador interno completo.
    #
    # Estos marcadores son la FIRMA de los pasos 1-6 del propio
    # SYSTEM_PROMPT (ver `_get_base_system_prompt`), en sus dos idiomas,
    # tolerando la paráfrasis del modelo ("Analizar"/"Análisis de").
    _REASONING_LEAK_MARKERS: Tuple[Pattern[str], ...] = (
        re.compile(r"an[aá]l(?:isis|izar)\s+(?:de\s+)?la\s+petici[oó]n", re.IGNORECASE),
        re.compile(r"evaluar?\s+(?:las\s+)?herramientas\s+disponibles", re.IGNORECASE),
        re.compile(r"checklist\s+de\s+comprensi[oó]n", re.IGNORECASE),
        re.compile(r"auto[\s-]?correcci[oó]n(?:\s+jer[aá]rquica)?", re.IGNORECASE),
        re.compile(r"plan\s+de\s+respuesta", re.IGNORECASE),
        re.compile(r"nivel\s+[12]\s*[—\-:]\s*(?:estructural|f[aá]ctico|l[oó]gico)", re.IGNORECASE),
        re.compile(r"toque\s+de\s+atenci[oó]n", re.IGNORECASE),
        re.compile(r"analyz\w*\s+the\s+user'?s?\s+request", re.IGNORECASE),
        re.compile(r"evaluate\s+available\s+tools", re.IGNORECASE),
        re.compile(r"(?:per[\s-]source\s+)?comprehension\s+checklist", re.IGNORECASE),
        re.compile(r"hierarchical\s+self[\s-]?check", re.IGNORECASE),
        re.compile(r"level\s+[12]\s*[—\-:]\s*(?:structural|factual|logical)", re.IGNORECASE),
    )

    # SEGUNDO EJE - METACOMENTARIO GENERICO (independiente del protocolo)
    #
    # Los marcadores de arriba son la firma LITERAL de los pasos 1-6 del
    # SYSTEM_PROMPT ("Analizar la peticion", "Checklist de comprension").
    # Cubren el caso en que el modelo copia los encabezados del protocolo,
    # pero NO cuando lo parafrasea con vocabulario propio. Caso real de
    # produccion (qwen2.5:7b) que atravesaba el filtro con cero marcadores:
    #
    #   "The user is asking for all the details of the match. [...] To
    #    properly address the user's request, I need to clarify that.
    #    Therefore, I will focus on what is available."
    #
    # Este eje ataca la FORMA, no el vocabulario: narracion en primera
    # persona sobre el propio proceso de responder, o referencia al
    # usuario en tercera persona. Un texto dirigido AL usuario no habla
    # de "la peticion del usuario" ni anuncia "por lo tanto voy a".
    #
    # DELIBERADAMENTE NO incluye frases sobre la EVIDENCIA ("el contexto
    # web no contiene...", "las fuentes no son concluyentes"): el propio
    # SYSTEM_PROMPT ORDENA decir eso cuando las fuentes no alcanzan, asi
    # que marcarlas romperia la defensa anti-alucinacion. La distincion
    # es metacomentario sobre RESPONDER (fuga) vs. afirmacion sobre la
    # EVIDENCIA (respuesta legitima).
    #
    # Umbral propio e INDEPIENTE de 2 coincidencias: los dos ejes se
    # cuentan por separado, de modo que una mencion suelta de cada tipo
    # nunca suma para disparar el recorte.
    _METACOMMENTARY_LEAK_MARKERS: Tuple[Pattern[str], ...] = (
        re.compile(r"\bthe user (?:is asking|asks|wants to know|is requesting)\b", re.IGNORECASE),
        re.compile(r"\bto (?:properly |correctly )?address the user'?s? (?:request|question|query)\b", re.IGNORECASE),
        re.compile(r"\btherefore,?\s+I will\b", re.IGNORECASE),
        re.compile(r"\bI (?:will|need to) (?:focus on|clarify that|structure|explain that|start by)\b", re.IGNORECASE),
        re.compile(r"\bmy (?:answer|response) (?:should|must|will|needs to)\b", re.IGNORECASE),
        re.compile(r"\bel usuario (?:pregunta|est[aá] preguntando|pide|solicita|quiere saber)\b", re.IGNORECASE),
        re.compile(r"\bpara (?:responder|abordar)\s+(?:adecuadamente\s+)?(?:a\s+)?la (?:petici[oó]n|consulta|pregunta) del usuario\b", re.IGNORECASE),
        re.compile(r"\bpor lo tanto,?\s+(?:voy a|me centrar[eé]|explicar[eé])\b", re.IGNORECASE),
        re.compile(r"\b(?:voy a|me voy a) (?:centrarme|enfocarme|estructurar|explicar)\b", re.IGNORECASE),
        re.compile(r"\bmi (?:respuesta|plan) (?:debe|deber[ií]a|ser[aá])\b", re.IGNORECASE),
    )
    #: Se exigen dos marcadores distintos para actuar. Con uno solo, el
    #: texto podría ser una respuesta legítima que menciona de pasada
    #: "el análisis de la petición" - recortar ahí sería peor que la
    #: propia fuga. Falso negativo antes que mutilar una respuesta buena.
    _REASONING_LEAK_MIN_MARKERS: int = 2

    #: Una línea que continúa el borrador: paso numerado, sub-nivel,
    #: viñeta o encabezado en negrita. Se usa para consumir el resto de
    #: la sección filtrada tras el último marcador.
    # El alternante de citas cubre las lineas del paso 3 del protocolo
    # (" [1] The sources mention..."). Sin el, el consumo se detenia
    # en la PRIMERA de esas lineas y los pasos 4-6 del borrador - que no
    # llevan marcador propio porque el modelo los parafrasea ("The
    # response should cover:", "Self-correction:") - sobrevivian dentro
    # de la respuesta "limpia": 450 caracteres de borrador crudo en un
    # El modelo a veces escribe la palabra suelta `thought` (sin < > ni
    # corchetes) como si fuera la etiqueta de apertura. No es etiqueta,
    # asi que _split_thought_and_content no la toca, y no es marcador de
    # protocolo, asi que tampoco dispara el recorte: queda pegada al
    # arranque de la respuesta ("thought The 2026 FIFA World Cup...").
    #
    # Se limpia en dos puntos (ver _strip_leaked_reasoning): sobre el
    # texto de ENTRADA, porque el caso mas frecuente es `thought` +
    # respuesta ya limpia SIN borrador (cero marcadores: el recorte ni
    # siquiera corre); y sobre el CANDIDATO ya recortado, porque el
    # modelo repite la palabra al cerrar el borrador y queda de residuo.
    _LEADING_BARE_THOUGHT_RE: Pattern[str] = re.compile(
        r"^\s*(?:thought|pensamiento)\s*:?\s*(?:\n|\s)",
        re.IGNORECASE,
    )

    # Nota (medido - captura de pantalla del usuario, turno
    # "dime ecuaciones importantes de la física", qwen2.5:3b): la primera
    # línea de la respuesta VISIBLE arrancó con
    #   "[Dado que no hay contexto web disponible, no es necesario
    #    realizar la verificación de las fuentes. La respuesta va a
    #    basarse en mi conocimiento propio del tema, sin recurrir a
    #    fuentes externas.]"
    # - el modelo narrando, entre corchetes y FUERA de <thought>, que
    # está cumpliendo el paso 3 del protocolo ("Checklist de comprensión
    # por fuente... Omite este paso por completo si no hay contexto web
    # en este turno", ver SYSTEM_PROMPT más abajo) en vez de omitirlo en
    # silencio. NO dispara _REASONING_LEAK_MARKERS (no repite vocabulario
    # literal del protocolo, tipo "checklist de comprensión") ni
    # _METACOMMENTARY_LEAK_MARKERS (no menciona "el usuario" ni usa "mi
    # respuesta debe/mi plan será" - dice "la respuesta va a basarse",
    # una construcción distinta) - un TERCER estilo de fuga, narrar el
    # propio cumplimiento de una instrucción de OMISIÓN, que ninguno de
    # los dos ejes de _strip_leaked_reasoning anticipaba, así que
    # protocol_hits=meta_hits=0 y el recorte de esa función nunca llega a
    # ejecutarse.
    #
    # Se recorta siempre, igual que el token suelto "thought" de arriba,
    # sin exigir el umbral de 2 coincidencias de _strip_leaked_reasoning:
    # es un patrón de alta precisión (exige mencionar la ausencia de
    # contexto web Y la consecuencia - apoyarse en conocimiento propio o
    # no recurrir a fuentes externas - en la misma cláusula entre
    # corchetes) sobre una fuga que nunca es, en sí misma, una respuesta
    # legítima. ACOTADO al caso medido (igual que _KNOWN_MODEL_TYPOS en
    # math_render.py): si aparece una variante sin corchetes, se agrega
    # entonces con su propia evidencia, no se generaliza a ciegas ahora.
    _LEADING_CONTEXT_SKIP_EXPLANATION_RE: Pattern[str] = re.compile(
        r"^\s*\[\s*(?:dado|ya)\s+que\s+no\s+(?:hay|existe|se\s+dispone\s+de)"
        r"\s+contexto\s+web[^\]\n]{0,300}?"
        r"(?:conocimiento\s+propio|fuentes\s+externas)[^\]\n]{0,80}?\]\s*",
        re.IGNORECASE | re.DOTALL,
    )

    # Nota (medido - turno "Calcula el volumen de un toroide
    # con radio mayor R = 5 y radio menor r = 2..." ruteado a slow_path/
    # qwen2.5:3b, respuesta guardada en conversation_turns): la respuesta
    # VISIBLE arrancó con un párrafo entero narrando el PASO 2 del
    # protocolo ("Evaluar herramientas disponibles"), parafraseado en
    # prosa en vez de omitido:
    #   "El problema que planteas no requiere de una herramienta local ni
    #    de información del contexto web. [...] no necesitamos incluir la
    #    verificación de contexto ni la ejecución de una herramienta."
    # Es la variante SIN corchetes que el comentario de
    # `_LEADING_CONTEXT_SKIP_EXPLANATION_RE` (arriba) anticipaba ("si
    # aparece una variante sin corchetes, se agrega entonces con su propia
    # evidencia") - y ahora hay evidencia. NO dispara los dos ejes de
    # `_strip_leaked_reasoning`: `_REASONING_LEAK_MARKERS` exige el
    # vocabulario LITERAL del protocolo ("Evaluar herramientas
    # disponibles") y `_METACOMMENTARY_LEAK_MARKERS` exige narración en
    # primera persona sobre el propio proceso ("voy a", "mi respuesta
    # debe") - este párrafo no tiene ni lo uno ni lo otro, así que
    # protocol_hits = meta_hits = 0 y el recorte por umbral nunca corre.
    #
    # Alta precisión (y por eso se recorta siempre, sin umbral, igual que
    # los otros dos strips de inicio): exige una cláusula de "NO se
    # necesita / NO requiere / sin necesidad de" pegada -dentro de la
    # misma frase- a una pieza de la MAQUINARIA INTERNA de SovNode
    # ("herramienta local", "ejecución/invocación de una herramienta",
    # "verificación de contexto/fuentes", "búsqueda web", "contexto web").
    # Una respuesta dirigida al usuario nunca discute esas piezas. Cubre
    # también la variante ENTRE corchetes (`\[?` … `\]?`), así que absorbe
    # los `[Dado que no hay contexto web...]` que se le escapan al patrón
    # de arriba por no decir "conocimiento propio"/"fuentes externas".
    # Deliberadamente NO marca frases sobre lo que la EVIDENCIA no
    # contiene ("no encontré información en el contexto web sobre X"): eso
    # el SYSTEM_PROMPT lo ORDENA y es respuesta legítima - la distinción
    # es narrar el PROCESO ("no ejecuto una herramienta") vs. afirmar
    # sobre la EVIDENCIA ("las fuentes no dicen").
    _LEADING_TOOL_DECISION_LEAK_RE: Pattern[str] = re.compile(
        r"^\s*\[?\s*"
        r"[^\n]*?\b(?:no\s+(?:se\s+)?(?:requiere|necesita|necesitamos|precisa|amerita)"
        r"|no\s+es\s+necesari[oa]|no\s+hace\s+falta|sin\s+(?:necesidad\s+de|recurrir\s+a))\b"
        r"[^\n]{0,140}?\b(?:"
        r"herramienta(?:s)?\s+local(?:es)?"
        r"|(?:ejecuci[oó]n|ejecutar|invocar|invocaci[oó]n|uso|usar)\s+(?:de\s+)?(?:una\s+|la\s+|m[aá]s\s+)?herramienta"
        r"|verificaci[oó]n\s+de(?:l|\s+las?)?\s+(?:contexto|fuentes)"
        r"|b[uú]squeda\s+web"
        r"|contexto\s+web"
        r")\b[^\n]*"
        r"(?:\n(?![ \t]*(?:#{1,6}\s|\*{2}|\d+[.)]\s|[-*•]\s|>\s|\s*$))[^\n]*)*"
        r"\]?[ \t]*(?:\r?\n)+",
        re.IGNORECASE,
    )

    # caso real capturado en produccion.
    _REASONING_CONTINUATION_RE: Pattern[str] = re.compile(
        r"^\s*(?:\d+[.)]\s|\[\d+\]|[-*•]\s|#{1,6}\s|\*\*|nivel\s+\d|level\s+\d|>\s)",
        re.IGNORECASE,
    )

    def verify_response_against_sources(
        self,
        verification_query: str,
        response_text: str,
        raw_sources: list[dict],
    ) -> Dict[str, Any]:
        """
        Ejecuta todos los verificadores independientemente.

        Ningún resultado aprobado omite los demás.
        """
        score_report = verify_scores(
            verification_query,
            response_text,
            raw_sources,
        )

        raw_evidence_text = build_raw_evidence_text(raw_sources)

        contradictions = self.find_unattributed_contradiction(
            verification_query,
            response_text,
            raw_sources,
        )

        unsupported_victories = self.find_unsupported_victory_claims(
            verification_query,
            response_text,
            raw_evidence_text,
        )

        logical_result = self._logic_validator.validate(response_text)

        return {
            "score_report": score_report,
            "unsupported_scores": score_report.unsupported_scores,
            "contradictions": contradictions,
            "unsupported_victories": unsupported_victories,
            "logical_result": logical_result,
            "valid": (
                score_report.valid
                and not contradictions
                and not unsupported_victories
                and logical_result.status == LogicalStatus.COHERENT
            ),
        }

    def build_combined_verification_correction_prompt(
        self,
        user_query: str,
        response_text: str,
        verification: Dict[str, Any],
        raw_sources: list[dict],
        lang: Optional[str] = None,
    ) -> str:
        if verification.get("valid"):
            return ""

        is_en = (
            lang or getattr(self, "current_language", "Spanish")
        ) == "English"

        problems: list[str] = []

        unsupported_scores = verification.get("unsupported_scores") or set()
        if unsupported_scores:
            problems.append(
                "Unsupported score(s): "
                + ", ".join(sorted(unsupported_scores))
            )

        contradictions = verification.get("contradictions") or []
        if contradictions:
            problems.append(
                "The response silently selected one value from "
                "contradictory sources."
                if is_en else
                "La respuesta eligió silenciosamente un valor entre "
                "fuentes contradictorias."
            )

        victories = verification.get("unsupported_victories") or set()
        if victories:
            problems.append(
                "The claimed winner is not supported by the sources."
                if is_en else
                "El ganador afirmado no está respaldado por las fuentes."
            )

        logical_result = verification.get("logical_result")
        if (
            logical_result is not None
            and logical_result.status != LogicalStatus.COHERENT
        ):
            problems.append(str(logical_result))

        evidence_text = build_raw_evidence_text(raw_sources)
        problem_text = "\n".join(f"- {problem}" for problem in problems)

        if is_en:
            return (
                f"Original user question:\n{user_query}\n\n"
                f"Answer to correct:\n{response_text}\n\n"
                f"Deterministic verification found:\n{problem_text}\n\n"
                "Original source evidence follows. It is the only factual "
                "source of truth; do not use summaries or prior memory:\n\n"
                f"{evidence_text}\n\n"
                "Rewrite only the final answer. Every score, winner, "
                "participant, round and score phase must match the evidence. "
                "Distinguish regulation time, extra time and penalty shootout. "
                "Do not mention the correction process."
            )

        return (
            f"Pregunta original:\n{user_query}\n\n"
            f"Respuesta que debe corregirse:\n{response_text}\n\n"
            f"La verificación determinista detectó:\n{problem_text}\n\n"
            "A continuación está la evidencia ORIGINAL. Es la única fuente "
            "de verdad factual; no uses resúmenes ni memoria previa:\n\n"
            f"{evidence_text}\n\n"
            "Reescribe únicamente la respuesta final. Todo marcador, ganador, "
            "participante, ronda y fase del marcador debe coincidir con la "
            "evidencia. Distingue tiempo reglamentario, prórroga y tanda de "
            "penaltis. No menciones el proceso de corrección."
        )
    
        # -----------------------------------------------------------------
    # WAL por fases - bajado desde StreamTurnWorker (sovnode_qt.py):
    # es trazabilidad de negocio, no algo que deba vivir en la capa Qt.
    # -----------------------------------------------------------------
    def _wal_open_turn(self, turn_id: str, user_input: str) -> None:
        wal = getattr(self, "_wal", None)
        if wal is None:
            return
        with contextlib.suppress(Exception):
            wal.append_user_input(turn_id, user_input)

    def _wal_close_turn(self, turn_id: str, response: str, outcome: str) -> None:
        wal = getattr(self, "_wal", None)
        if wal is None or not turn_id:
            return
        with contextlib.suppress(Exception):
            wal.append_response(turn_id, response, outcome=outcome)

    def _wal_phase(self, turn_id: str, phase: str, **details: Any) -> None:
        wal = getattr(self, "_wal", None)
        if wal is None or not turn_id:
            return
        with contextlib.suppress(Exception):
            wal.append("turn_phase", {"turn_id": turn_id, "phase": phase, **details})

    # -----------------------------------------------------------------
    # API pública única: generador de eventos de un turno completo.
    # Toda capa de presentación (Qt, CLI, API web) consume ESTO, nunca
    # los métodos internos por separado.
    # -----------------------------------------------------------------
    def run_turn(
        self,
        user_input: str,
        force_web_search: bool = False,
        web_search_fn: Optional[Callable[[str, Optional[str], Optional[Callable]], dict]] = None,
        cancel_flag: Optional[Any] = None,   # objeto con .is_set()
    ):
        """
        Generador único de PipelineEvent para un turno. `web_search_fn`
        se inyecta desde afuera (típicamente fetch_rich_web_search de
        sovnode_qt.py) hasta que esa función se relocalice a web_search.py
        — mientras tanto, Orchestrator no depende de Qt en absoluto:
        recibe la función lista para usar, no la importa.
        """
        turn_id = str(uuid.uuid4())

        def cancelled() -> bool:
            return bool(cancel_flag and cancel_flag.is_set())

        def emit_log(msg: str) -> None:
            # se usa como log_cb en las llamadas internas que ya lo aceptan
            pass  # se sobreescribe más abajo con una closure real

        self._is_processing_turn = True
        self._pause_governor_event.set()
        self._wal_open_turn(turn_id, user_input)

        # closure real de log_cb: cada _call_llm/_call_llm_raw que reciba
        # esto emitirá su traza como evento LOG en vez de solo loggear
        log_buffer: list = []
        def log_cb(msg: str) -> None:
            log_buffer.append(msg)

        try:
            yield PipelineEvent(EventType.INTENT, ("🧠", "Analizando la intención..."))

            decision = self._classify_turn(user_input)
            active_model = self._select_model_for_decision(decision)
            # Arquitectura de modelo único: no hay rol coder separado — ver
            # RESPONSE_MODEL y sección 23 de test_regressions.py. Se conserva
            # la variable (y el branching `if is_coder` / `not is_coder` de
            # abajo, ahora estático) para acotar el diff; queda documentado.
            is_coder = False
            effective_lang = self._resolve_turn_language(user_input)

            # ver la nota en _should_force_web_search: FACTUAL_ENUMERATION
            # fuerza grounding web real (medido - captura "hola,
            # dime ecuaciones matematicas").
            force_web_search = self._should_force_web_search(force_web_search, decision)

            self._wal_phase(turn_id, "routed", path=decision.path.value, model=active_model)
            yield PipelineEvent(EventType.ROUTE_DECIDED, decision, meta={"model": active_model})

            if cancelled():
                yield PipelineEvent(EventType.DONE, {"trace": None, "error": "cancelled"})
                return

            # ---------- Caché semántico ----------
            if not force_web_search:
                cached = None
                with contextlib.suppress(Exception):
                    cached = self.check_semantic_cache(user_input, decision=decision)
                if cached:
                    resp = cached["response"]
                    self._wal_phase(turn_id, "semantic_cache_hit", chars=len(resp))
                    yield PipelineEvent(EventType.CACHE_HIT, cached)
                    yield PipelineEvent(EventType.TOKEN, (resp, ""))
                    self.memory_graph.store_turn(turn_id, "user", user_input)
                    self.memory_graph.store_turn(str(uuid.uuid4()), "assistant", resp)
                    self._wal_close_turn(turn_id, resp, decision.path.value)
                    trace = TurnTrace(
                        turn_id=turn_id, user_input=user_input, routing_decision=decision,
                        outcome=TurnOutcome.FAST_PATH_DIRECT, engine_results=[],
                        web_context_used=False, knowledge_node_persisted=False,
                        logical_status="coherent", final_response=resp,
                        total_elapsed_ms=0.0, model_used="semantic-cache",
                    )
                    yield PipelineEvent(EventType.DONE, {"trace": trace, "error": ""})
                    return

            # ---------- Carril trivial (saludos / cortesía breve) ----------
            # El router marca SignalTag.TRIVIAL_GREETING para "hola",
            # "buenas", "gracias", etc. (router.py `_TRIVIAL_GREETING_RE`).
            # Sin este carril un saludo pagaba el system prompt general
            # entero (~3100-3370 tok de prefill) + un <thought>
            # obligatorio que después se descarta - ~35s MEDIDOS para
            # responder "hola". Acá: cabecera "system" mínima
            # (`_get_trivial_system_prompt`), prompt de usuario pelado
            # (`_build_trivial_prompt`), `num_predict` 200, streaming, y
            # NADA de fetch_hybrid_context / _fetch_metacognitive_lessons
            # / _build_reasoning_prompt / _trim_context_to_budget.
            # `not force_web_search`: si el usuario tildó 🌐 con un "hola",
            # se respeta esa intención y el turno cae al camino normal.
            # `num_ctx` NO se baja (queda pineado en 8192): cambiarlo
            # entre este turno y el siguiente forzaría una recarga del
            # runner de Ollama (~7-10s, ver MemoryGovernor), y con el
            # prompt mínimo el prefill ya es ~80 tok igual.
            if (
                decision.path == RoutePath.FAST_PATH
                and not force_web_search
                and SignalTag.TRIVIAL_GREETING in decision.tags
            ):
                yield PipelineEvent(EventType.INTENT, ("💬", "Respondiendo..."))
                trivial_stream = self._stream_llm_raw(
                    self._build_trivial_prompt(user_input, effective_lang),
                    target_model=active_model,
                    lang_override=effective_lang,
                    num_predict_override=200,
                    system_override=self._get_trivial_system_prompt(effective_lang),
                    stop=self._ANSWER_RESTART_STOP_SEQUENCES,
                    log_cb=log_cb,
                    perf_label="Trivial",
                )
                trivial_sink: Dict[str, Any] = {}
                trivial_streamed = False
                for visible in self._iter_visible_tokens(trivial_stream, cancelled, trivial_sink):
                    trivial_streamed = True
                    yield PipelineEvent(EventType.TOKEN, (visible, ""))
                for pending_log in log_buffer:
                    yield PipelineEvent(EventType.LOG, pending_log)
                log_buffer.clear()

                if trivial_sink.get("cancelled"):
                    yield PipelineEvent(EventType.DONE, {"trace": None, "error": "cancelled"})
                    return

                raw_trivial = trivial_sink.get("raw", "")
                if raw_trivial.lstrip().startswith("[ERROR"):
                    self._wal_close_turn(turn_id, raw_trivial, outcome="error")
                    yield PipelineEvent(EventType.DONE, {"trace": None, "error": raw_trivial})
                    return

                _, trivial_clean = self._split_thought_and_content(raw_trivial)
                trivial_clean, _ = self._strip_system_prompt_echo(trivial_clean)
                trivial_final = (trivial_clean or raw_trivial).strip()

                # Si el streaming ya pintó texto en el globo, NO se
                # reemite el texto completo (la UI lo reconcilia con
                # `trace.final_response` en `_on_turn_completed`,
                # sovnode_qt.py:~5600 - `update_content` reemplaza, no
                # concatena). Solo se emite un TOKEN si el gate no dejó
                # pasar nada (mismo patrón que el hit de caché semántico).
                if not trivial_streamed:
                    yield PipelineEvent(EventType.TOKEN, (trivial_final, ""))
                self.memory_graph.store_turn(turn_id, "user", user_input)
                self.memory_graph.store_turn(str(uuid.uuid4()), "assistant", trivial_final)
                self._wal_close_turn(turn_id, trivial_final, decision.path.value)
                trace = TurnTrace(
                    turn_id=turn_id, user_input=user_input, routing_decision=decision,
                    outcome=TurnOutcome.FAST_PATH_DIRECT, engine_results=[],
                    web_context_used=False, knowledge_node_persisted=False,
                    logical_status="coherent", final_response=trivial_final,
                    total_elapsed_ms=0.0, model_used=active_model,
                )
                yield PipelineEvent(EventType.DONE, {"trace": trace, "error": ""})
                return

            # ---------- Búsqueda web ----------
            web_context_str = ""
            web_success = False
            web_sources: list = []
            if force_web_search:
                yield PipelineEvent(EventType.INTENT, ("🔍", "Consultando fuentes en internet..."))
                search_query = self._build_contextual_search_query(
                    user_input, log_cb=log_cb, lang=effective_lang,
                )
                for pending_log in log_buffer:
                    yield PipelineEvent(EventType.LOG, pending_log)
                log_buffer.clear()

                rich_data = {}
                if web_search_fn is not None:
                    rich_data = web_search_fn(search_query, "en" if effective_lang == "English" else "es", log_cb) or {}
                if cancelled():
                    yield PipelineEvent(EventType.DONE, {"trace": None, "error": "cancelled"})
                    return

                # Nota (medido - reportado por el usuario:
                # una ráfaga de logs "[WEB_SEARCH]" apareciendo todos
                # juntos, con "Formateando contexto..." y otros pasos
                # finales mezclados fuera de orden, después de un
                # silencio de ~65s sin ningún log en pantalla): antes de
                # este fix, `log_buffer` (todo lo que `web_search_fn`
                # acumuló vía `log_cb` durante la búsqueda completa) NO
                # se vaciaba acá - quedaba esperando hasta el PRÓXIMO
                # flush, más abajo, que corre después de
                # `_call_llm_two_pass`/`_call_llm` (la generación real de
                # la respuesta). El usuario veía la búsqueda web
                # "colgada" en silencio durante toda la generación del
                # modelo (el tramo real más lento del turno), y luego
                # todos los logs de la búsqueda aparecían de golpe,
                # mezclados con el cierre de la búsqueda - dando la
                # falsa impresión de que la búsqueda en sí tardó
                # decenas de segundos. Vaciar acá, justo después de que
                # `web_search_fn` retorna y antes de WEB_RESULTS (que
                # dispara la tarjeta visual en la UI), respeta el orden
                # cronológico real: la búsqueda se ve terminar cuando
                # termina, no cuando termina la generación que viene
                # después.
                for pending_log in log_buffer:
                    yield PipelineEvent(EventType.LOG, pending_log)
                log_buffer.clear()

                yield PipelineEvent(EventType.WEB_RESULTS, rich_data)
                web_success = bool(rich_data.get("success"))
                web_sources = list(rich_data.get("sources") or [])
                self._wal_phase(turn_id, "web_search", success=web_success, sources=len(web_sources))

                if web_success and web_sources:
                    threading.Thread(
                        target=self._persist_web_knowledge,
                        args=(search_query, web_sources), daemon=True,
                    ).start()

                if web_success and rich_data.get("snippets"):
                    # avisos deterministas ya existentes (año, contexto pobre, contradicción...)
                    warnings = "".join(filter(None, [
                        self.build_year_mismatch_warning(search_query, rich_data["snippets"]),
                        self.build_thin_context_warning(search_query, rich_data["snippets"], lang=effective_lang),
                        self.build_source_contradiction_warning(web_sources, user_query=search_query),
                        self.build_scheduled_event_grounding_rule(lang=effective_lang, user_query=search_query),
                    ]))
                    web_context_str = warnings + "\n".join(f"- {s}" for s in rich_data["snippets"])
                else:
                    web_context_str = (
                        "[SYSTEM NOTICE — NO REAL-TIME DATA] No hay resultados verificables."
                    )

            # ---------- Contexto conversacional ----------
            # BALANCE DE VELOCIDAD (medido: turno fast_path real, 7273 tok
            # de prefill = 85.10s, contra apenas 33.79s de decode - el 71%
            # del tiempo total se fue en re-procesar contexto YA existente,
            # no en generar nada nuevo). 8 turnos de historial CRUDO (sin
            # resumir) es sospechoso #1: en esta conversación en particular
            # los turnos previos incluyen respuestas largas multi-sección
            # (ecuaciones, listas), y ese texto se re-prellena entero en
            # cada turno nuevo (ver `_get_frozen_header`: solo la cabecera
            # "system" se beneficia de cache de prefijo entre llamadas, el
            # historial conversacional NO). fast_path recorta a 4 turnos
            # (el propio default de `get_recent_history`); slow_path se
            # queda en 8 sin tocar - son turnos donde el router ya decidió
            # que la consulta es lo bastante compleja como para que valga
            # la pena pagar más contexto.
            history_limit = 4 if decision.path == RoutePath.FAST_PATH else 8
            recent_turns = self.memory_graph.get_recent_history(limit=history_limit)
            recent_turns = self._truncate_history_entries(recent_turns)
            compacted_context = "\n".join(recent_turns)
            metacognitive_context = ""
            if not web_success:
                hybrid = self.fetch_hybrid_context(user_input, limit=3)
                if hybrid:
                    compacted_context += f"\n--- RAG HISTÓRICO ---\n{hybrid}"
                metacognitive_context = self._fetch_metacognitive_lessons(user_input)

            compacted_context, web_context_str, metacognitive_context = self._trim_context_to_budget(
                user_input, compacted_context, web_context_str, metacognitive_context
            )

            if cancelled():
                yield PipelineEvent(EventType.DONE, {"trace": None, "error": "cancelled"})
                return

            # ---------- Generación ----------
            yield PipelineEvent(EventType.INTENT, ("⚡", "Sintetizando respuesta..."))

            # Streaming token a token para la respuesta VISIBLE. `run_turn`
            # es un generador, así que delega con `yield from` en este
            # generador anidado, que reemite cada fragmento de
            # `_stream_llm_raw` como `EventType.TOKEN` (la UI de Qt ya lo
            # pinta en vivo - ver `_on_chunk_received`/`_flush_stream_buffer`).
            # El `_ThoughtStreamGate` dentro de `_iter_visible_tokens` oculta
            # un bloque `<thought>` interno si el modelo llegara a emitir uno
            # (con gpt-oss no pasa: razona en su canal `analysis` nativo, que
            # Ollama separa a `thinking` — SovNode lee solo `response`).
            # `gen_sink` recibe el resultado (`raw`, `done_reason`,
            # `cancelled`) al agotarse el stream.
            gen_sink: Dict[str, Any] = {}

            def _stream_visible(gen_prompt, gen_label, *, gen_web_evidence=False,
                                gen_system=None, gen_num_predict=None):
                gs = self._stream_llm_raw(
                    gen_prompt,
                    target_model=active_model,
                    lang_override=effective_lang,
                    has_web_evidence=gen_web_evidence,
                    system_override=gen_system,
                    num_predict_override=gen_num_predict,
                    stop=self._ANSWER_RESTART_STOP_SEQUENCES,
                    log_cb=log_cb,
                    perf_label=gen_label,
                )
                for _vis in self._iter_visible_tokens(gs, cancelled, gen_sink):
                    yield PipelineEvent(EventType.TOKEN, (_vis, ""))

            done_reason = ""
            # ---------- Carril LEAN de una sola pasada (TODOS los turnos) ----
            # Arquitectura de modelo único (ver RESPONSE_MODEL): fast_path Y
            # slow_path se generan igual — `_get_fastpath_system_prompt`
            # (header lean, sin protocolo <thought>/<thought_code>) +
            # `_build_reasoning_prompt(lean=True)`, UNA sola llamada.
            #
            # Por qué NO se usa más `_call_llm_two_pass` / el protocolo
            # <thought> (MEDIDO, PASO 0, gpt-oss:20b real — ver
            # STEP0_HARMONY_FINDINGS.md): gpt-oss razona en su canal Harmony
            # `analysis` nativo, que Ollama YA separa a `thinking` (SovNode
            # lee solo `response`). Imponerle además un <thought> propio (a)
            # filtra narración de analysis como prefijo de `response` de
            # forma intermitente y (b) hace fallar el request con HTTP 500
            # "error parsing tool call" en 3/3 pruebas. El campo `think=low`
            # (ver THINK_LEVEL, inyectado en `_prepare_ollama_payload`)
            # mantiene el canal analysis en ~15 tokens en vez de ~600.
            # `_call_llm_two_pass` se conserva SIN INVOCAR (rollback).
            #
            # fast vs slow se conserva SOLO en dos cosas: el techo de
            # `num_predict` (slow necesita más margen para desarrollar) y
            # cuál circuit-breaker corre después (ver más abajo: fast tiene
            # heurísticas de longitud + regeneración; slow solo el chequeo
            # de eco de andamiaje).
            is_fast_gen = decision.path == RoutePath.FAST_PATH

            # ver la nota junto a _factual_enumeration_caution: contexto
            # APARTE solo para este prompt — nunca se reasigna
            # `compacted_context`, así que el aviso no persiste al historial
            # ni a llamadas posteriores del turno (followup de tool-calling).
            gen_context = compacted_context
            if SignalTag.FACTUAL_ENUMERATION in decision.tags:
                gen_context += self._factual_enumeration_caution(effective_lang)

            prompt = self._build_reasoning_prompt(
                user_input, gen_context, web_context_str, False,
                inject_dev_override=False, lang=effective_lang, lean=True,
            )
            gen_system = self._get_fastpath_system_prompt(effective_lang)
            gen_predict = (
                MemoryGovernor.fastpath_num_predict() if is_fast_gen
                else MemoryGovernor.slowpath_num_predict()
            )
            # Turnos propensos a divagar (enumeración factual, búsqueda web
            # forzada) o slow_path (respuestas largas por naturaleza): NO se
            # streamea token a token — se genera bloqueante y se muestra la
            # respuesta ya post-procesada (screenshot 2026-08-27: el usuario
            # veía la parrafada escribirse entera antes de que el breaker la
            # reemplazara). `stop=_ANSWER_RESTART_STOP_SEQUENCES`: confirmado
            # en sovnode_memory.db — sin él, "dime ecuaciones matematicas"
            # generaba la respuesta y la repetía resumida reabriendo con el
            # ancla "Respuesta:".
            ramble_prone = (
                not is_fast_gen
                or SignalTag.FACTUAL_ENUMERATION in decision.tags
                or force_web_search
            )
            if ramble_prone:
                raw_response, _fp_ec, done_reason = self._call_llm_raw(
                    prompt, target_model=active_model, lang_override=effective_lang,
                    has_web_evidence=web_success, system_override=gen_system,
                    num_predict_override=gen_predict,
                    stop=self._ANSWER_RESTART_STOP_SEQUENCES,
                    log_cb=log_cb, perf_label="LeanSingle",
                )
            else:
                yield from _stream_visible(
                    prompt, "LeanSingle", gen_web_evidence=web_success,
                    gen_system=gen_system, gen_num_predict=gen_predict,
                )
                raw_response = gen_sink.get("raw", "")
                done_reason = gen_sink.get("done_reason", "")
                if gen_sink.get("cancelled"):
                    yield PipelineEvent(EventType.DONE, {"trace": None, "error": "cancelled"})
                    return
            stats = {}
            for pending_log in log_buffer:
                yield PipelineEvent(EventType.LOG, pending_log)
            log_buffer.clear()
            self._wal_phase(turn_id, "generation_done", stats=stats)

            # Ollama falló (ver _call_llm_raw/_call_llm_two_pass: en error
            # devuelven "[ERROR] ..." como si fuera texto normal). Cortar
            # acá para no mostrarlo como respuesta ni guardarlo en
            # memoria/caché como si el modelo hubiera contestado de
            # verdad. outcome="error" además engancha con el escaneo de
            # WAL que ya hace CognitiveGovernor._introspect().
            if raw_response.lstrip().startswith("[ERROR"):
                self._wal_close_turn(turn_id, raw_response, outcome="error")
                yield PipelineEvent(EventType.DONE, {"trace": None, "error": raw_response})
                return

            # ---------- Tool calling ----------
            tool_call = self.extract_tool_call(raw_response)
            if tool_call and not cancelled():
                tool_name = tool_call.get("tool", "?")
                yield PipelineEvent(EventType.TOOL_CALL_START, tool_call, meta={"name": tool_name})
                self._wal_phase(turn_id, "tool_call", tool=tool_name)
                tool_result = self.execute_tool_from_call(tool_call)
                self._wal_phase(turn_id, "tool_result", tool=tool_name, chars=len(str(tool_result)))
                yield PipelineEvent(EventType.TOOL_CALL_RESULT, tool_result, meta={"name": tool_name})

                # Nota (medido - turno "quién ganó la final de
                # la Champions League [año]", capturas de pantalla
                # adjuntas): ver los docstrings de `_is_internal_toolguard_
                # notice` y `_build_toolcall_followup_context` (justo
                # después de `execute_tool_from_call`, más abajo en este
                # archivo) para el detalle completo de los dos bugs
                # compuestos que este bloque corrige. Factorizados a
                # métodos aparte - en vez de lógica inline acá - para que
                # test_regressions.py pueda verificarlos directamente
                # contra el código real, no contra una copia reescrita a
                # mano que podría divergir en silencio.
                is_internal_toolguard_notice = self._is_internal_toolguard_notice(tool_result)
                followup_context = self._build_toolcall_followup_context(
                    web_success, web_context_str
                )
                followup = (
                    f"Petición original: {user_input}\n\n{tool_result}"
                    f"{followup_context}\n"
                    "Redacta únicamente una explicación en lenguaje natural. "
                    "Prohibido generar más JSON."
                )
                explanation = self._call_llm(
                    followup, target_model=active_model, lang_override=effective_lang,
                    log_cb=log_cb, perf_label="ToolCall-P2",
                )
                raw_response = (
                    explanation if is_internal_toolguard_notice
                    else f"{tool_result}\n\n{explanation}"
                )

            _, clean_response = self._split_thought_and_content(raw_response)
            # Fuga del canal `analysis` de Harmony (gpt-oss) — ver
            # `_strip_harmony_leak` y STEP0_HARMONY_FINDINGS.md. Va PRIMERO:
            # el prefijo de narración analysis vive fuera de cualquier
            # etiqueta <thought>, así que los strippers de abajo no lo ven.
            clean_response, harmony_leaked = self._strip_harmony_leak(clean_response)
            if harmony_leaked:
                yield PipelineEvent(EventType.LOG, "Fuga del canal analysis de Harmony depurada.")
            clean_response, leaked = self._strip_leaked_reasoning(clean_response)
            if leaked:
                yield PipelineEvent(EventType.LOG, "Fuga de razonamiento sin etiquetar depurada.")

            # Nota (medido - ver la nota junto a
            # `_strip_system_prompt_echo`, cerca de
            # `_strip_leaked_reasoning`): eco LITERAL del prompt de
            # sistema (rótulos entre corchetes, separadores de sección)
            # en vez de parafraseo del propio razonamiento - mismo punto
            # de la cadena de limpieza, eje de detección distinto.
            clean_response, prompt_echoed = self._strip_system_prompt_echo(clean_response)
            if prompt_echoed:
                yield PipelineEvent(EventType.LOG, "Eco del prompt de sistema depurado.")

            # Nota (medido - ver la nota completa junto
            # a `_dedupe_enumeration_items`, cerca de
            # `_strip_leaked_reasoning`): misma idea que el bloque de
            # arriba pero para el bucle degenerativo de ítems repetidos
            # en vez de razonamiento filtrado - universal a cualquier
            # `raw_response`, por eso corre en el mismo punto.
            clean_response, deduped = self._dedupe_enumeration_items(clean_response)
            if deduped:
                yield PipelineEvent(EventType.LOG, "Ítems de enumeración duplicados/degenerados recortados.")

            # ---------- Circuit-breaker anti-alucinación (SOLO slow_path
            # — ver _slowpath_response_looks_broken) ----------
            # fast_path tiene el suyo más abajo, con regeneración incluida.
            # Va ANTES de ese bloque a propósito: son carriles mutuamente
            # excluyentes (decision.path es uno u otro), así que el orden
            # entre ambos no importa en la práctica, pero mantenerlos
            # juntos deja clara la simetría entre los dos breakers.
            if decision.path != RoutePath.FAST_PATH and clean_response:
                slow_broken_reason = self._slowpath_response_looks_broken(clean_response)
                if slow_broken_reason:
                    self._wal_phase(turn_id, "slowpath_circuit_breaker", reason=slow_broken_reason)
                    yield PipelineEvent(
                        EventType.LOG,
                        f"Circuit-breaker slow_path: {slow_broken_reason} — "
                        f"respuesta reemplazada por fallback seguro.",
                    )
                    clean_response = (
                        self._SAFE_FALLBACK_EN if effective_lang == "English"
                        else self._SAFE_FALLBACK_ES
                    )

            # Recorte del relleno de seguimiento de phi3.5 en fast_path
            # (screenshot 2026-08-27: buena respuesta a "3 equations" +
            # "Note that while these..." + "Example Question with Specific
            # Answer Request:" cortado a mitad). Va antes del breaker: si
            # el recorte deja una respuesta sana, el breaker no dispara y
            # nos ahorramos la regeneración.
            if decision.path == RoutePath.FAST_PATH and not is_coder and clean_response:
                trimmed = self._trim_fastpath_padding(clean_response)
                if trimmed != clean_response:
                    clean_response = trimmed
                    yield PipelineEvent(EventType.LOG, "Relleno de seguimiento recortado.")

            # ---------- Circuit-breaker anti-alucinación (SOLO fast_path) ----------
            # Va antes de la continuación, los verificadores y el LangFix:
            # si la respuesta ya es basura de un modelo descarrilado (eco
            # del prompt, parrafada desproporcionada para un "hi"), no
            # tiene sentido gastar 40-50s en un LangFix sobre esa basura
            # (medido, screenshot 2026-08-27: LangFix `prefill=3237tok`,
            # 49s, 1597 tok de MÁS basura, antes de que el breaker la
            # cortara igual). Acá se regenera UNA vez con el prompt
            # mínimo; si eso también sale roto, `_SAFE_FALLBACK`. Lo que
            # venga después (continuación/verificadores/LangFix) corre
            # sobre texto ya limpio y no dispara.
            breaker_fired = False
            if decision.path == RoutePath.FAST_PATH and not is_coder and clean_response:
                broken_reason = self._fastpath_response_looks_broken(
                    user_input, clean_response, web_success,
                    hit_ceiling=(done_reason == "length"),
                )
                if broken_reason:
                    breaker_fired = True
                    self._wal_phase(turn_id, "fastpath_circuit_breaker", reason=broken_reason)
                    yield PipelineEvent(
                        EventType.LOG,
                        f"Circuit-breaker fast_path: {broken_reason} — regenerando con prompt mínimo.",
                    )
                    regen = self._call_llm(
                        self._build_fastpath_regen_prompt(user_input, effective_lang),
                        target_model=active_model, lang_override=effective_lang,
                        system_override=self._get_trivial_system_prompt(effective_lang),
                        # 200 tok ≈ 2-3 frases; techo bajo a propósito para
                        # que un phi3.5 que ignore "2-3 frases" igual no
                        # tenga margen de descarrilar (y para no volver a
                        # cruzar el umbral de "parrafada" del propio breaker).
                        num_predict_override=200,
                        stop=self._ANSWER_RESTART_STOP_SEQUENCES,
                        log_cb=log_cb, perf_label="FastPath-regen",
                    )
                    regen_ok = bool(regen) and not regen.lstrip().startswith("[ERROR")
                    if regen_ok:
                        _, regen_clean = self._split_thought_and_content(regen)
                        regen_clean, _ = self._strip_system_prompt_echo(regen_clean)
                        regen_clean = self._trim_fastpath_padding((regen_clean or "").strip())
                    else:
                        regen_clean = ""
                    if regen_clean and not self._fastpath_response_looks_broken(
                        user_input, regen_clean, web_success, is_regen=True
                    ):
                        clean_response = regen_clean
                    else:
                        clean_response = (
                            self._SAFE_FALLBACK_EN if effective_lang == "English"
                            else self._SAFE_FALLBACK_ES
                        )
                        yield PipelineEvent(
                            EventType.LOG,
                            "Circuit-breaker fast_path: la regeneración también falló — fallback seguro.",
                        )

            # ---------- Guarda de respuesta completa (SOLO fast_path) ----------
            # En una sola pasada, el <thought> obligatorio + el estilo
            # que exige desarrollar cada punto pueden llegar al techo de
            # `num_predict` (`done_reason == "length"`) y cortar la
            # respuesta a mitad - turno 2 del video que motivó esto:
            # terminaba en "...Por ejemplo:" y nada más. Si Ollama
            # reportó "length" Y `_looks_truncated`, se dispara UNA
            # continuación acotada (mismo criterio de coste que
            # `resolve_visible_answer`: llamada extra solo en este fallo
            # concreto y raro). El slow path ya tiene piso vía
            # `split_budget`/`ANSWER_MIN_FLOOR`, así que no aplica acá.
            # `len(clean_response) < 1400`: una respuesta que llegó al
            # techo PERO es corta se cortó de verdad (vale continuarla);
            # una que llegó al techo con 1400+ chars es un
            # descarrilamiento, no una truncación - continuarla solo
            # agrega más basura (medido, screenshot 2026-08-27: la
            # continuación sumó 890 tok a una respuesta ya desbocada).
            # Ese caso lo agarra el circuit-breaker de arriba.
            if (
                decision.path == RoutePath.FAST_PATH
                and not is_coder
                and not breaker_fired
                and done_reason == "length"
                and clean_response.strip()
                and len(clean_response) < 1400
                and self._looks_truncated(clean_response)
            ):
                self._wal_phase(turn_id, "fast_path_truncated_continuation")
                cont = self._call_llm(
                    self._build_continuation_prompt(clean_response[-800:], effective_lang),
                    target_model=active_model, lang_override=effective_lang,
                    log_cb=log_cb, perf_label="FastSingle-cont",
                )
                if cont and not cont.lstrip().startswith("[ERROR"):
                    _, cont_clean = self._split_thought_and_content(cont)
                    cont_clean, _ = self._strip_leaked_reasoning(cont_clean)
                    cont_clean, _ = self._strip_system_prompt_echo(cont_clean)
                    cont_clean = cont_clean.strip()
                    if cont_clean:
                        clean_response = (clean_response.rstrip() + " " + cont_clean.lstrip()).strip()
                        yield PipelineEvent(EventType.LOG, "Respuesta cortada por límite de tokens — continuada.")

            # ---------- Cadena de verificación post-hoc ----------
            # Patrón para agregar más verificadores sin duplicar código:
            # cada entrada es (nombre, detect_fn -> Any|None, build_prompt_fn).
            # `Any|None` de detect_fn: None/vacío = no dispara.
            # Se saltea si el circuit-breaker ya reemplazó la respuesta
            # (regen mínimo o fallback): no hay nada que verificar contra
            # fuentes en un "no pude, reformulá".
            if web_context_str and not breaker_fired:
                verifiers = [
                    (
                        "unsupported_score",
                        lambda: self.find_unsupported_scores(user_input, clean_response, web_context_str),
                        lambda hit: self.build_unsupported_score_correction_prompt(
                            user_input, clean_response, hit, web_context_str, lang=effective_lang,
                        ),
                    ),
                    (
                        "unattributed_contradiction",
                        lambda: self.find_unattributed_contradiction(user_input, clean_response, web_sources),
                        lambda hit: self.build_contradiction_enforcement_prompt(
                            user_input, clean_response, hit, lang=effective_lang,
                        ),
                    ),
                    (
                        "unsupported_victory",
                        lambda: self.find_unsupported_victory_claims(user_input, clean_response, web_context_str),
                        lambda hit: self.build_unsupported_victory_correction_prompt(
                            user_input, clean_response, hit,
                            evidence_text=web_context_str, lang=effective_lang,
                        ),
                    ),
                ]
                for name, detect_fn, build_fn in verifiers:
                    if cancelled():
                        break
                    hit = detect_fn()
                    yield PipelineEvent(EventType.VERIFICATION, {"name": name, "triggered": bool(hit), "detail": hit})
                    if not hit:
                        continue
                    correction_prompt = build_fn(hit)
                    if not correction_prompt:
                        continue
                    self._wal_phase(turn_id, f"{name}_detected")
                    corrected = self._call_llm(
                        correction_prompt, target_model=active_model,
                        lang_override=effective_lang, log_cb=log_cb, perf_label=name,
                    )
                    if corrected and not corrected.lstrip().startswith("[ERROR"):
                        _, corrected_clean = self._split_thought_and_content(corrected)
                        clean_response = self._strip_correction_prompt_echo(corrected_clean or corrected)
                        # Nota: ver `_strip_system_prompt_echo` - esta
                        # llamada de corrección también manda el header
                        # congelado (`[CRITICAL LANGUAGE RULE]` incluido),
                        # así que su salida hereda el mismo riesgo de eco
                        # que la generación principal - independiente del
                        # eco del PROMPT DE CORRECCIÓN que la línea de
                        # arriba ya cubre.
                        clean_response, _ = self._strip_system_prompt_echo(clean_response)
                        self._wal_phase(turn_id, "correction_pair", pair_type=name)

            # verificación de idioma - independiente de la cadena anterior.
            # `not breaker_fired`: el regen/fallback del breaker ya sale en
            # el idioma correcto, no hace falta re-chequear.
            if (clean_response and not breaker_fired
                    and self.find_language_mismatch(clean_response, effective_lang)):
                lang_prompt = self.build_language_correction_prompt(clean_response, effective_lang)
                # En fast_path, la corrección de idioma usa el header
                # LIGERO (no el general de ~3200 tok): medido - con el
                # header pesado el LangFix de un turno fast_path costaba
                # `prefill=3237tok` y ~49s, sobre una respuesta que el
                # breaker iba a descartar igual.
                lang_system = (
                    self._get_fastpath_system_prompt(effective_lang)
                    if decision.path == RoutePath.FAST_PATH and not is_coder
                    else None
                )
                corrected = self._call_llm(
                    lang_prompt, target_model=active_model, lang_override=effective_lang,
                    system_override=lang_system, num_predict_override=700,
                    log_cb=log_cb, perf_label="LangFix",
                )
                if corrected and not corrected.lstrip().startswith("[ERROR"):
                    _, corrected_clean = self._split_thought_and_content(corrected)
                    clean_response = self._strip_correction_prompt_echo(corrected_clean or corrected)
                    # Nota: ver `_strip_system_prompt_echo` - mismo
                    # motivo que en la corrección de arriba (misma
                    # llamada a `_call_llm`, mismo header congelado).
                    clean_response, _ = self._strip_system_prompt_echo(clean_response)

            for pending_log in log_buffer:
                yield PipelineEvent(EventType.LOG, pending_log)

            final_response, recovered = self.resolve_visible_answer(
                raw_response, clean_response, active_model=active_model,
                lang=effective_lang, has_web_evidence=web_success,
            )
            if recovered:
                yield PipelineEvent(EventType.LOG, "Respuesta visible vacía — recuperada.")

            final_response = self._strip_duplicate_answer_restart(final_response)
            final_response = self._strip_trailing_empty_list_stubs(final_response)

            yield PipelineEvent(EventType.TOKEN, (final_response, ""))

            self.memory_graph.store_turn(turn_id, "user", user_input)
            self.memory_graph.store_turn(str(uuid.uuid4()), "assistant", final_response)
            if not force_web_search:
                self.store_semantic_cache_async(user_input, final_response, active_model, decision=decision)
            self._wal_close_turn(turn_id, final_response, decision.path.value)

            trace = TurnTrace(
                turn_id=turn_id, user_input=user_input, routing_decision=decision,
                outcome=TurnOutcome.FAST_PATH_DIRECT, engine_results=[],
                web_context_used=web_success, knowledge_node_persisted=False,
                logical_status="coherent", final_response=final_response,
                total_elapsed_ms=0.0, model_used=active_model,
            )
            trace.web_search_attempted = force_web_search
            yield PipelineEvent(EventType.DONE, {"trace": trace, "error": ""})

        except Exception as exc:
            yield PipelineEvent(EventType.ERROR, str(exc))
            yield PipelineEvent(EventType.DONE, {"trace": None, "error": str(exc)})
        finally:
            self._is_processing_turn = False
            self._pause_governor_event.clear()

    @classmethod
    def _strip_leaked_reasoning(cls, text: str) -> Tuple[str, bool]:
        """
        Elimina un volcado de razonamiento SIN ETIQUETAS del principio de
        la respuesta visible. Devuelve `(texto_limpio, hubo_fuga)`.

        Deliberadamente conservadora, en este orden:
          1. Exige al menos `_REASONING_LEAK_MIN_MARKERS` marcadores
             DISTINTOS del protocolo (uno solo puede ser una mención
             legítima de paso).
          2. Ancla el recorte en el ÚLTIMO marcador y consume el resto de
             esa sección; nunca corta por el primero, que dejaría fuera
             los pasos intermedios.
          3. Si lo que quedaría es vacío o demasiado corto para ser una
             respuesta real, DEVUELVE EL ORIGINAL: ante la duda, es
             preferible mostrar el borrador (feo pero completo) que
             entregar una respuesta mutilada o en blanco.
        """
        if not text or not text.strip():
            return text, False

        # Token suelto `thought` al inicio: se quita siempre, incluso si
        # despues no se detecta ninguna fuga - el caso mas frecuente es
        # `thought` seguido de una respuesta ya limpia, donde ningun
        # marcador dispara y el recorte de abajo no llega a ejecutarse.
        text = cls._LEADING_BARE_THOUGHT_RE.sub("", text, count=1).lstrip()

        # Mismo trato incondicional para la explicación-de-omisión entre
        # corchetes (ver la nota junto a
        # _LEADING_CONTEXT_SKIP_EXPLANATION_RE): no pasa el umbral de 2
        # marcadores de abajo porque no es ninguno de los dos ejes que
        # ese umbral cubre, así que se recorta acá, antes de que ese
        # chequeo siquiera corra.
        text = cls._LEADING_CONTEXT_SKIP_EXPLANATION_RE.sub("", text, count=1).lstrip()

        # Y para la variante EN PROSA de esa misma fuga - el párrafo
        # inicial que narra "no hace falta herramienta / contexto web /
        # verificación" para este turno (paso 2 del protocolo). Mismo
        # criterio: alta precisión, recorte incondicional, sin umbral.
        # ver la nota junto a _LEADING_TOOL_DECISION_LEAK_RE.
        text = cls._LEADING_TOOL_DECISION_LEAK_RE.sub("", text, count=1).lstrip()

        # Dos ejes con umbral propio cada uno: el del protocolo (firma
        # literal de los pasos 1-6) y el de metacomentario generico
        # (parafraseo libre). Se cuentan POR SEPARADO a proposito - una
        # coincidencia suelta de cada tipo no debe sumar para disparar.
        protocol_hits = [m for m in cls._REASONING_LEAK_MARKERS if m.search(text)]
        meta_hits = [m for m in cls._METACOMMENTARY_LEAK_MARKERS if m.search(text)]
        if (len(protocol_hits) < cls._REASONING_LEAK_MIN_MARKERS
                and len(meta_hits) < cls._REASONING_LEAK_MIN_MARKERS):
            return text, False

        _ALL_LEAK_MARKERS = cls._REASONING_LEAK_MARKERS + cls._METACOMMENTARY_LEAK_MARKERS
        lines = text.splitlines()
        last_marker_idx = -1
        for idx, line in enumerate(lines):
            if any(pattern.search(line) for pattern in _ALL_LEAK_MARKERS):
                last_marker_idx = idx

        if last_marker_idx == -1:
            return text, False

        # Consume el cuerpo de la última sección filtrada: líneas de
        # continuación (numeradas, sub-niveles, viñetas) y las líneas en
        # blanco que las separan.
        cursor = last_marker_idx + 1
        while cursor < len(lines):
            stripped = lines[cursor].strip()
            if not stripped:
                cursor += 1
                continue
            if cls._REASONING_CONTINUATION_RE.match(lines[cursor]):
                cursor += 1
                continue
            break

        candidate = "\n".join(lines[cursor:]).strip()

        # Segunda pasada sobre el residuo: el modelo cierra el borrador
        # repitiendo `thought`, y esa linea no matchea
        # _REASONING_CONTINUATION_RE, asi que el consumo se detiene justo
        # antes y la palabra encabeza el candidato.
        candidate = cls._LEADING_BARE_THOUGHT_RE.sub("", candidate, count=1).strip()

        # Salvaguarda: si el recorte no deja una respuesta plausible, se
        # prefiere el texto íntegro antes que devolver algo vacío.
        if len(candidate) < 40:
            return text, False

        return candidate, True

    # Nota (medido - capturas del usuario, dos turnos
    # distintos en la misma sesión: "hola" a secas, y "busca en internet
    # la historia de christian de lugano"): la respuesta VISIBLE
    # reprodujo, LITERAL, fragmentos del propio PROMPT DE SISTEMA que se
    # envía en todo turno - "[CRITICAL LANGUAGE RULE]" (de
    # `LANG_ENFORCE_DIRECTIVE`, que `_get_frozen_header()` concatena
    # siempre al final del header, sin excepción de idioma/modelo/tipo de
    # turno - ver esa función) y "[VERIFICACIÓN EN TIEMPO REAL DEL
    # SANDBOX]" (de la sección "SCRATCHPAD VERIFICADOR" que
    # `_get_base_system_prompt()` inyecta igual de incondicional, en todo
    # turno tenga o no `<thought_code>` real). En el turno de Lugano el
    # log de verificación post-hoc mostró las tres comprobaciones
    # (unsupported_score/unattributed_contradiction/unsupported_victory)
    # en `triggered: false` - ninguna disparó, así que NINGÚN prompt de
    # corrección llegó siquiera a construirse: la fuga no viene de ahí.
    # `_strip_correction_prompt_echo` (ver su nota, 2026-08-19) ya
    # documentó esta misma familia de fallo - el modelo repite de vuelta
    # un prompt de instrucción completo antes de recién ahí escribir la
    # respuesta real - pero acotado a los 4 prompts de corrección
    # post-hoc. Esta fuga ocurre en la llamada de GENERACIÓN PRINCIPAL,
    # que ese fix nunca cubrió.
    #
    # Ninguno de los dos ejes de `_strip_leaked_reasoning` de arriba
    # aplica acá: `_REASONING_LEAK_MARKERS`/`_METACOMMENTARY_LEAK_MARKERS`
    # exigen que el modelo PARAFRASEE su propio proceso ("voy a", "the
    # user is asking") - acá no hay parafraseo, hay cita LITERAL de texto
    # que el modelo jamás redactaría por sí mismo dirigiéndose al
    # usuario.
    #
    # Detección en dos frentes independientes - CUALQUIERA de los dos ya
    # es prueba suficiente por sí solo, sin exigir un segundo marcador
    # corroborante (a diferencia de los ejes de metacomentario de arriba,
    # que sí lo exigen): una respuesta dirigida al usuario jamás produce
    # ninguna de las dos cosas por su cuenta.
    #   1. Las etiquetas entre corchetes EXACTAS que
    #      `_get_base_system_prompt()`/`LANG_ENFORCE_DIRECTIVE` insertan
    #      en todo turno (español e inglés - ambas formas conviven en el
    #      mismo método; no es una variante nueva sin medir, es la misma
    #      construcción ya visible en el otro idioma). Mismo criterio que
    #      ya usa `_LEADING_TOOL_DECISION_LEAK_RE`: una respuesta real
    #      reporta el VALOR verificado, nunca el NOMBRE del protocolo
    #      interno que lo produjo.
    #   2. Una tira de 40+ signos "=" - el separador que encabeza toda
    #      sección de ese mismo prompt de sistema (la forma real mide 65,
    #      ver `LANG_ENFORCE_DIRECTIVE`/`_get_base_system_prompt`; el
    #      piso de 40 tolera que el modelo "recuerde" el separador con
    #      alguna deriva de longitud sin dejar de exigir que sea
    #      claramente ese separador y no un simple "----" de formato
    #      normal). Deliberadamente ESTRUCTURAL en vez de listar cada
    #      título de sección a mano: cubre cualquier encabezado -
    #      incluidos los que se agreguen después - sin mantener una lista
    #      sincronizada con el prompt.
    #
    # Misma mecánica que `_strip_correction_prompt_echo`: se recorta todo
    # lo que viene HASTA la última aparición de cualquiera de los dos
    # frentes (no la primera - un eco puede repetirse más de una vez), y
    # se conserva solo la cola. Misma salvaguarda que el resto del
    # archivo: un candidato de menos de 40 caracteres se descarta a favor
    # del texto íntegro - mejor una respuesta fea pero completa que una
    # mutilada o vacía.
    _SYSTEM_PROMPT_ECHO_MARKERS_RE: Pattern[str] = re.compile(
        r"\[CRITICAL LANGUAGE RULE\]"
        r"|\[VERIFICACI[OÓ]N EN TIEMPO REAL DEL SANDBOX\]"
        r"|\[REAL-TIME SANDBOX VERIFICATION\]"
        r"|={40,}",
        re.IGNORECASE,
    )

    @classmethod
    def _strip_system_prompt_echo(cls, text: str) -> Tuple[str, bool]:
        """
        Elimina un eco LITERAL del prompt de sistema (rótulos entre
        corchetes o separadores de sección) del principio de la
        respuesta visible. Distinto de `_strip_leaked_reasoning`: ese
        cubre PARAFRASEO del propio proceso de razonar; este cubre CITA
        TEXTUAL del prompt que el modelo recibió. Devuelve `(texto_
        limpio, hubo_fuga)`. Ver BLINDAJE junto a
        `_SYSTEM_PROMPT_ECHO_MARKERS_RE`.

        BLINDAJE (bug real, MEDIDO al escribir el test de esta función
        contra el turno "hola" — ver sección 15 de test_regressions.py):
        el marcador casi nunca cae al FINAL de la frase que lo contiene
        — "[CRITICAL LANGUAGE RULE]" abre su propio párrafo de
        instrucción ("You MUST respond in the exact same language..."),
        y "[VERIFICACIÓN EN TIEMPO REAL DEL SANDBOX]" vive A MITAD de
        una oración ("...se te devuelve como [...] antes de que
        redactes tu respuesta final"). Cortar justo en
        `matches[-1].end()` dejaba esa cola de instrucción — todavía
        prompt, no respuesta — pegada al principio del candidato. Se
        extiende el corte hasta el PRÓXIMO salto de párrafo ("\\n\\n"):
        tanto `_get_base_system_prompt()` como `LANG_ENFORCE_DIRECTIVE`
        cierran CADA bloque con "\\n\\n" antes del siguiente (es la
        convención de todo este prompt, no una suposición nueva), así
        que ese salto es un límite confiable entre "todavía scaffolding"
        y "ya es la respuesta". Si no hay ningún "\\n\\n" después del
        último marcador (el eco fue lo último que escribió el modelo),
        se conserva `matches[-1].end()` tal cual antes que arriesgar
        recortar de más.
        """
        if not text or not text.strip():
            return text, False

        matches = list(cls._SYSTEM_PROMPT_ECHO_MARKERS_RE.finditer(text))
        if not matches:
            return text, False

        cut_from = matches[-1].end()
        next_paragraph_break = text.find("\n\n", cut_from)
        if next_paragraph_break != -1:
            cut_from = next_paragraph_break + 2

        candidate = text[cut_from:].strip()
        if len(candidate) < 40:
            return text, False

        return candidate, True

    # =================================================================
    # Fuga del canal `analysis` de Harmony (gpt-oss) -> `response`
    # =================================================================
    # Bug real, MEDIDO (PASO 0, 2026-08-27, scripts aislados contra
    # gpt-oss:20b real vía /api/generate — ver STEP0_HARMONY_FINDINGS.md).
    # Ollama parsea el formato Harmony del lado del servidor y separa la
    # respuesta en `response` (canal `final`) y `thinking` (canal
    # `analysis`); SovNode lee solo `response`. PERO cuando la disciplina
    # de canales del modelo falla, parte de la narración de `analysis` se
    # filtra como PREFIJO de `response`, casi siempre pegada SIN espacio a
    # la respuesta real:
    #   'The user asks: "..." They want... We need Spanish. Must start with
    #    the central idea... No tools needed.Un vector es una magnitud...'
    #                                        ^--- acá arranca la respuesta
    # Con el carril lean (sin protocolo <thought>) + think="low" la fuga
    # fue 0/16 en las pruebas, pero se mantiene este stripper como red de
    # seguridad — es no-determinista y la comunidad la reporta bajo otras
    # condiciones (ollama/ollama#12203, #12741).
    #
    # NO lo cubren `_strip_leaked_reasoning` (trabaja por LÍNEAS; esta fuga
    # es una sola línea pegada) ni `_SYSTEM_PROMPT_ECHO_MARKERS_RE` /
    # `_FASTPATH_ECHO_RE` (diseñados contra phi3.5: eco LITERAL del prompt,
    # no narración de un canal de razonamiento).
    #
    # Firma inequívoca: deliberación sobre la TAREA en 1ª/3ª persona,
    # SIEMPRE en inglés sin importar el idioma de la respuesta. La 1ª
    # cláusula tiene que matchear un arranque FUERTE (los que jamás abren
    # una respuesta real); las siguientes se consumen mientras sigan
    # pareciendo deliberación en inglés (ASCII, vocabulario de meta-tarea).
    _HARMONY_CONTROL_TOKEN_RE: Pattern[str] = re.compile(
        r"<\|(?:channel|message|start|end|return|constrain|call)\|>", re.IGNORECASE
    )
    _HARMONY_STRONG_LEAD_RE: Pattern[str] = re.compile(
        r"^\s*(?:"
        r"the user\b|user (?:asks?|wants?|is asking|says?|writes?)\b|"
        r"no tools?\s+(?:needed|required)\b|"
        r"let'?s\s+(?:craft|do|write|produce|answer|start|give)\b|"
        r"we\s+(?:need to|must|should|have to|will|can|could)\s+"
        r"(?:comply|respond|answer|produce|write|start|give|provide|explain)\b|"
        r"we\s+need\s+(?:spanish|english|to\b|a\b)|"
        r"need(?:s)?\s+(?:spanish|english|to\s+(?:respond|answer|comply|produce|explain))\b|"
        r"must\s+(?:start|answer|respond|comply|produce|give|include)\b|"
        r"the\s+(?:developer|system)\s+(?:instruction|instructions|prompt|says?|wants?)\b|"
        r"okay,?\s+so\b|first,?\s+(?:i|we)\s+(?:need|must|should)\b"
        r")",
        re.IGNORECASE,
    )
    _HARMONY_CLAUSE_RE: Pattern[str] = re.compile(r"[^.!?\n]*[.!?]|[^.!?\n]+$")
    _HARMONY_DELIB_WORD_RE: Pattern[str] = re.compile(
        r"\b(?:we|user|they|i|instruction|instructions|language|spanish|english|"
        r"latex|inline|formula|formulas|answer|respond|response|comply|tool|tools|"
        r"central|no mention|system|developer|prompt|need|needs|must|should|"
        r"provide|explain|concise|brief|direct|start with)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _strip_harmony_leak(cls, text: str) -> Tuple[str, bool]:
        """
        Recorta del PRINCIPIO de `response` una fuga del canal `analysis`
        de Harmony (gpt-oss). Devuelve `(texto_limpio, hubo_fuga)`. Ver
        BLINDAJE arriba.

        Dos frentes independientes, cualquiera basta:
          1. Tokens de control Harmony crudos (`<|channel|>`, `<|message|>`,
             `<|start|>`, …) en cualquier parte -> se recorta hasta el
             último (o el próximo "\\n\\n") y se conserva la cola.
          2. La respuesta ARRANCA con narración de `analysis` en inglés ->
             se consume ese bloque deliberativo inicial (>= 2 cláusulas,
             hasta 12) y se conserva lo que sigue.

        Salvaguarda estándar de este archivo: si el recorte deja < 40
        caracteres, se devuelve el texto íntegro (mejor feo pero completo
        que mutilado).
        """
        if not text or not text.strip():
            return text, False

        # --- Frente 1: tokens de control Harmony crudos ---
        control = list(cls._HARMONY_CONTROL_TOKEN_RE.finditer(text))
        if control:
            cut = control[-1].end()
            brk = text.find("\n\n", cut)
            if brk != -1:
                cut = brk + 2
            cand = text[cut:].strip()
            if len(cand) >= 40:
                return cand, True

        # --- Frente 2: narración analysis como prefijo ---
        body = text.lstrip("﻿ \t\r\n")
        if not cls._HARMONY_STRONG_LEAD_RE.match(body):
            return text, False

        pos = 0
        consumed = 0
        while consumed < 12:
            m = cls._HARMONY_CLAUSE_RE.match(body, pos)
            if not m or m.end() == pos:
                break
            clause = body[pos:m.end()]
            if consumed > 0:
                mostly_ascii = sum(1 for c in clause if c > "\x7f") <= 1
                looks_delib = bool(
                    cls._HARMONY_STRONG_LEAD_RE.match(clause.strip())
                    or cls._HARMONY_DELIB_WORD_RE.search(clause)
                )
                if not (mostly_ascii and looks_delib):
                    break
            pos = m.end()
            consumed += 1

        # >= 2 cláusulas: una sola coincidencia no es prueba suficiente de
        # fuga (la narración analysis medida tiene 5-8 cláusulas).
        if consumed < 2:
            return text, False
        candidate = body[pos:].lstrip()
        if len(candidate) >= 40 and candidate != body:
            return candidate, True
        return text, False

    # =================================================================
    # Bucle degenerativo a nivel de caracteres (gpt-oss, num_predict lleno)
    # =================================================================
    # Bug real, MEDIDO (PASO 0, probe4 C3, gpt-oss:20b + think=low): la
    # misma consulta que otras dos veces salió bien, la tercera degeneró en
    # 'Sadi Carnot y R. C. G. H. R. K. K. K. K. ...'
    # repitiendo 'K.<espacio-fino>' cientos de veces hasta llenar el techo.
    # `_dedupe_enumeration_items` NO lo agarra (ataca la repetición del
    # TÍTULO de un ítem de lista, con estructura "**Título**:"); esto es
    # repetición a nivel de caracteres, sin estructura.
    _DEGENERATE_REPEAT_RE: Pattern[str] = re.compile(r"(.{2,24}?)\1{12,}", re.DOTALL)

    @classmethod
    def _looks_degenerate_repetition(cls, text: str) -> bool:
        """¿El texto entró en un bucle de repetición de una subcadena corta
        (>= 13 repeticiones seguidas, unidad de 2-24 chars)? Ver BLINDAJE."""
        return bool(text) and cls._DEGENERATE_REPEAT_RE.search(text) is not None

    # Nota (medido - turno "dime ecuaciones importantes de
    # fisica", screenshot + log del usuario adjuntos, mismo turno
    # documentado junto a REPEAT_PENALTY/REPEAT_LAST_N en
    # `MemoryGovernor`): la respuesta enumeró leyes reales al principio,
    # pero pasado cierto punto degeneró en repetir el mismo ítem - mismo
    # título en negrita, "**Ley de Newton de la Tensión en Paredes
    # (sobre un ángulo)**:" - más de una decena de veces seguidas,
    # variando solo la función trigonométrica de la fórmula interna,
    # hasta cortarse a mitad de palabra al llegar al techo de
    # `num_predict`. El aviso de `_factual_enumeration_caution` (ver
    # nota 1/#2 ahí) YA le pide al modelo no rellenar por
    # duplicado - este turno prueba que un modelo de 3B puede ignorar esa
    # instrucción de prompt en medio de una lista larga, así que hace
    # falta una red de seguridad determinística río abajo, igual que
    # `_strip_leaked_reasoning` es la red de seguridad de código para la
    # instrucción de "no muestres tu razonamiento" que el prompt también
    # pide pero no siempre alcanza a cumplir.
    #
    # Ancla de detección: el título en negrita seguido de dos puntos
    # ("**Título**:") es el formato que este proyecto ya usa para cada
    # ítem de una enumeración (ver el comentario "viñeta o encabezado en
    # negrita" junto a _REASONING_CONTINUATION_RE) - no depende de qué
    # viñeta lo precede (*, -, número), así que sobrevive a variaciones
    # de formato del modelo.
    #
    # Deliberadamente estricto en la comparación (título normalizado
    # IGUAL, no similitud difusa): el caso medido repite el título
    # exacto, solo la fórmula interna varía. Una comparación difusa
    # arriesgaría falsos positivos contra ítems legítimos con nombres
    # parecidos pero distintos (p. ej. "Ley de Newton de la Inversión" vs
    # "Ley de Newton de la Inversión en Velocidades", que sí son títulos
    # distintos aunque compartan la mayoría de las palabras) - esos
    # quedan fuera del alcance de esta función a propósito; se atacan
    # solo por prompt (ver el tercer párrafo de
    # `_factual_enumeration_caution`), no por código, porque distinguir
    # "ley real distinta" de "variación inventada del mismo tema" exige
    # conocimiento de dominio que esta función no tiene.
    #
    # Al primer título repetido, se corta todo lo que sigue desde ahí -
    # no se intenta rescatar contenido único intercalado más adelante: en
    # el turno medido, una vez que el modelo empieza a repetir, el resto
    # de la respuesta es puro ruido, y es más seguro cortar limpio que
    # arriesgar dejar pasar más basura por tratar de ser quirúrgico. Esto
    # además resuelve gratis el corte a mitad de palabra visible en la
    # captura: ese fragmento vive dentro de la cola descartada.
    #
    # Misma salvaguarda que _strip_leaked_reasoning: si el recorte deja
    # menos de 40 caracteres, se prefiere devolver el texto íntegro
    # (mejor una respuesta fea pero completa que una mutilada).
    _ENUMERATION_ITEM_TITLE_RE: Pattern[str] = re.compile(r"\*\*([^*\n]{2,120})\*\*\s*:")

    #: Prefijo de viñeta puro en lo que va de línea hasta el título en
    #: negrita ("* ", "- ", "1. ", "12) ") - se usa para arrastrar el
    #: recorte al inicio de la línea en vez de dejar una viñeta colgada
    #: sin contenido justo antes del corte (p. ej. un "*" suelto).
    _BULLET_PREFIX_RE: Pattern[str] = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])[ \t]*$")

    @classmethod
    def _dedupe_enumeration_items(cls, text: str) -> Tuple[str, bool]:
        """
        Trunca una enumeración en el punto donde el título en negrita de
        un ítem ("**Título**:") se repite igual a uno anterior — señal de
        que el modelo entró en un bucle degenerativo (ver BLINDAJE
        arriba). Devuelve `(texto_resultante, hubo_recorte)`.
        """
        if not text or not text.strip():
            return text, False

        matches = list(cls._ENUMERATION_ITEM_TITLE_RE.finditer(text))
        if len(matches) < 2:
            return text, False

        seen = set()
        for match in matches:
            normalized = " ".join(match.group(1).split()).casefold()
            if normalized in seen:
                cut_at = match.start()
                line_start = text.rfind("\n", 0, cut_at) + 1
                if cls._BULLET_PREFIX_RE.match(text[line_start:cut_at]):
                    cut_at = line_start
                candidate = text[:cut_at].rstrip()
                if len(candidate) < 40:
                    return text, False
                return candidate, True
            seen.add(normalized)

        return text, False

    # Nota (medido probando el modo de dos pasadas): el
    # modelo a veces abre el bloque de razonamiento con corchetes
    # ("[thought]") en vez de ángulos ("<thought>") - formato que
    # `_THOUGHT_BLOCK_PATTERNS`/`_split_thought_and_content` YA reconocen
    # y manejan bien. Pero `_call_llm_two_pass` (y su equivalente en el
    # streaming de sovnode_qt.py) chequeaban la apertura/cierre con un
    # `.startswith("<thought>")`/`.endswith("</thought>")` literal, que
    # NO reconoce la variante de corchetes - así que un turno donde el
    # modelo sí siguió el protocolo (solo que con corchetes) se trataba
    # como "no abrió <thought>" y disparaba el fallback de respuesta
    # directa sin necesidad, saltándose la Pasada 2 en casos que
    # perfectamente podían tenerla. Medido: en una tanda de 6 pruebas
    # reales, esto por sí solo explicaba al menos 1 de los "fallos".
    _THOUGHT_OPEN_RE: Pattern[str] = re.compile(r"^\s*(?:<thought\b[^>]*>|\[thought\])", re.IGNORECASE)
    _THOUGHT_CLOSE_RE: Pattern[str] = re.compile(r"(?:</thought\s*>|\[/thought\])\s*$", re.IGNORECASE)

    # Igual que `_THOUGHT_CLOSE_RE` pero SIN el ancla `$` - encuentra el
    # cierre esté donde esté, no solo al final. `_call_llm_two_pass` lo
    # usa para detectar que la Pasada 1 cerró su <thought> y SIGUIÓ
    # escribiendo (ver ahí). Deliberadamente NO matchea `</thought_code>`
    # (ese `_code` después de `thought` rompe tanto `</thought\s*>` como
    # `\[/thought\]`): el bloque de verificación en sandbox es otra cosa y
    # va, por protocolo, después del </thought> del razonamiento.
    _THOUGHT_CLOSE_ANYWHERE_RE: Pattern[str] = re.compile(r"</thought\s*>|\[/thought\]", re.IGNORECASE)

    # Umbral para "la cola que la Pasada 1 escribió después de </thought>
    # es una respuesta de verdad, no un fragmento suelto" - ver
    # `_call_llm_two_pass`. Sobre este piso (y con cierre natural, no por
    # techo de tokens) esa cola se usa tal cual y se omite la Pasada 2.
    # 160: ~2-3 frases; por debajo casi siempre es un arranque abandonado
    # o una sola línea, donde la Pasada 2 sí aporta la respuesta completa.
    _TWO_PASS_PASS1_LEAK_MIN_CHARS: int = 160

    # Nota - SEGUNDA línea de defensa del bug de los "<li> vacíos"
    # (medido - turno "Tengo tres cajas: una contiene solo
    # manzanas, otra solo naranjas..." ruteado a slow_path/qwen2.5:3b,
    # captura del usuario, respuesta guardada en conversation_turns).
    #
    # El fix PRIMARIO vive en `_call_llm_two_pass`: cuando la Pasada 1
    # cierra su <thought> y sigue escribiendo, esa cola se recorta como
    # respuesta y NO se fabrica un segundo </thought> ni se corre una
    # Pasada 2 que dejaba una respuesta casi idéntica duplicada. Pero si
    # por cualquier otra vía (una pasada única, un modelo que emite el
    # cierre partido, una corrección posterior) un </thought> huérfano
    # llega igual al texto visible, QTextDocument.setMarkdown() (ver
    # sovnode_qt.py) ante esa etiqueta de cierre suelta -cualquiera,
    # medido igual con </div>- entra en modo HTML crudo y se COME todo el
    # texto que sigue: párrafos y contenido de ítems de lista desaparecen,
    # pero los MARCADORES (1., -) igual se renderizan, como <li> vacíos
    # ("1.\n2.\n3." en pantalla sin nada al lado - el síntoma exacto de la
    # captura).
    #
    # Este barrido (en `_split_thought_and_content`) quita SOLO etiquetas
    # de CIERRE de razonamiento (angulares o de corchetes, con o sin
    # _code), en cualquier posición, que hayan sobrevivido a la
    # eliminación de pares balanceados. NO toca las de apertura: un
    # <thought> huérfano al principio ya lo maneja `orphan_open`, y uno en
    # medio del texto es un caso distinto que necesita su propia medición.
    _ORPHAN_THOUGHT_CLOSE_RE: Pattern[str] = re.compile(
        r"</thought(?:_code)?\s*>|\[/thought(?:_code)?\]", re.IGNORECASE
    )

    @classmethod
    def _thought_close_tag_for(cls, text: str) -> str:
        """Etiqueta de cierre que corresponde al estilo de apertura de `text` — `</thought>` por defecto si no se detecta corchetes."""
        return "[/thought]" if re.match(r"^\s*\[thought\]", text, re.IGNORECASE) else "</thought>"

    @classmethod
    def _split_pass1_leak(
        cls, thought_raw: str, reasoning_done_reason: str
    ) -> Tuple[str, Optional[str]]:
        """
        Decisión pura para el modo de dos pasadas (ver `_call_llm_two_
        pass`). Dada la salida CRUDA de la Pasada 1, devuelve
        `(thought_text, leaked_answer)`:

          - `thought_text`: el plan. Si el modelo cerró su bloque en
            algún lado, se recorta en el PRIMER cierre — así el "cierre
            forzado" del llamador nunca agrega un SEGUNDO </thought> que
            terminaría huérfano en el texto visible (bug real, MEDIDO —
            ver BLINDAJE junto a `_ORPHAN_THOUGHT_CLOSE_RE`). Si no cerró
            en ningún lado, se devuelve `thought_raw` intacto (el llamador
            fuerza el cierre).

          - `leaked_answer`: la respuesta que la Pasada 1 escribió por su
            cuenta DESPUÉS de cerrar </thought> — pero SOLO si es lo
            bastante larga (`_TWO_PASS_PASS1_LEAK_MIN_CHARS`) y el modelo
            cerró solo (`reasoning_done_reason != "length"`, no truncada
            por techo de tokens). En ese caso el llamador la usa y OMITE
            la Pasada 2 — sin esto, la Pasada 2 generaba una segunda
            respuesta casi idéntica que terminaba DUPLICADA en pantalla.
            `None` si no hay una cola aprovechable (no cerró, cola vacía,
            cola corta, o cola truncada por techo).
        """
        close = cls._THOUGHT_CLOSE_ANYWHERE_RE.search(thought_raw)
        if not close:
            return thought_raw, None
        thought_text = thought_raw[: close.end()]
        tail = thought_raw[close.end():].strip()
        usable = (
            len(tail) >= cls._TWO_PASS_PASS1_LEAK_MIN_CHARS
            and reasoning_done_reason != "length"
        )
        return thought_text, (tail if usable else None)

    @classmethod
    def _split_thought_and_content(cls, response_text: str) -> Tuple[str, str]:
        """
        Separa TODOS los bloques de razonamiento (<thought>, <thought_code>
        y sus variantes de corchetes [thought]/[thought_code]) de la
        respuesta final o llamada a herramienta. Devuelve (razonamiento
        concatenado, contenido limpio) — el contenido limpio es lo ÚNICO
        que debe llegar a la vista principal del chat.

        Tras eliminar los pares balanceados, hace un barrido final que
        quita cualquier etiqueta de CIERRE de razonamiento huérfana que
        quede suelta en el texto visible — ver el BLINDAJE completo junto
        a `_ORPHAN_THOUGHT_CLOSE_RE`: una sola de esas rompe el render de
        TODO lo que venga después en QTextDocument.setMarkdown().
        """
        if not response_text:
            return "", ""

        thoughts: List[str] = []
        clean_content = response_text
        for pattern in cls._THOUGHT_BLOCK_PATTERNS:
            for match in pattern.finditer(clean_content):
                captured = match.group(1).strip()
                if captured:
                    thoughts.append(captured)
            clean_content = pattern.sub("", clean_content)

        # Nota (medido - turno "dame las ecuaciones de
        # einstein" ruteado a qwen2.5-coder:3b, camino de UNA sola
        # pasada de _call_llm_two_pass cuando target_model resuelve a
        # coder): _THOUGHT_BLOCK_PATTERNS solo captura bloques con
        # apertura Y CIERRE - `pattern.sub()` no toca nada si falta
        # cualquiera de los dos. El modelo abrió "[thought]" pero nunca
        # escribió "[/thought]" (CODER_SYSTEM_PROMPT no define el
        # protocolo de 6 pasos que enseña a cerrarlo, ver docstring de
        # _call_llm_two_pass - degrada a una sola llamada sin la Pasada
        # 2 que normalmente separa plan de respuesta), así que
        # clean_content quedó IDÉNTICO al crudo: la etiqueta literal
        # "[thought]" visible al usuario, seguida - en el mismo
        # aliento, sin cierre - de lo que en la práctica ES la
        # respuesta real (ecuaciones, definiciones, todo). Tratar esto
        # como "sin respuesta visible" (_NO_VISIBLE_ANSWER_ES) tiraría
        # una respuesta buena a la basura; tratarlo como razonamiento
        # oculto también, por el mismo motivo. Lo único seguro es
        # recortar SOLO la etiqueta de apertura huérfana y conservar
        # todo lo demás tal cual - sin tocar la construcción del
        # prompt (que si el patrón se repite en otros modelos/turnos,
        # necesita su propia medición, no un ajuste a ciegas acá).
        orphan_open = cls._THOUGHT_OPEN_RE.match(clean_content)
        if orphan_open:
            logger.warning(
                "⚠️ [ThoughtSplit] Bloque de razonamiento abierto sin cerrar "
                "(%r) — se recorta solo la etiqueta huérfana, se conserva el "
                "resto como respuesta visible.",
                orphan_open.group(0).strip(),
            )
            clean_content = clean_content[orphan_open.end():]

        # Barrido final: cualquier etiqueta de CIERRE de razonamiento que
        # haya sobrevivido a la eliminación de pares balanceados es
        # huérfana por definición (no le quedó apertura que la reclame) -
        # ver la nota junto a `_ORPHAN_THOUGHT_CLOSE_RE`. Se
        # elimina sí o sí: dejarla pasar hace que QTextDocument.
        # setMarkdown() se coma todo el contenido posterior y lo deje como
        # marcadores de lista vacíos en pantalla.
        clean_content, orphan_closes = cls._ORPHAN_THOUGHT_CLOSE_RE.subn("", clean_content)
        if orphan_closes:
            logger.warning(
                "⚠️ [ThoughtSplit] %d etiqueta(s) de cierre de razonamiento "
                "huérfana(s) en el texto visible — eliminadas para no romper "
                "el render del contenido que las sigue.",
                orphan_closes,
            )
            # Quitar una etiqueta que estaba sola en su línea deja un hueco
            # de 3+ saltos; se colapsa a un corte de párrafo normal.
            clean_content = re.sub(r"\n{3,}", "\n\n", clean_content)

        return "\n\n".join(thoughts).strip(), clean_content.strip()

    # =================================================================
    # RESPUESTA VISIBLE VACIA (turno consumido dentro de <thought>)
    # =================================================================
    # Fallo REAL observado (qwen2.5:7b, turno guardado en
    # conversation_turns): el modelo emitio un unico bloque <thought>
    # de 2308 caracteres, correctamente abierto Y cerrado, y no escribio
    # absolutamente nada despues de </thought>. _split_thought_and_content
    # hizo bien su trabajo y devolvio contenido limpio VACIO.
    #
    # El bug no estaba ahi sino en el resguardo de las dos rutas, que
    # ante un limpio vacio caian al texto CRUDO:
    #     orchestrator : if clean_response: ... elif not final_response.strip()
    #                    -> con crudo no vacio NINGUNA rama corregia
    #     sovnode_qt : full_response = full_response_clean or full_response
    # Como QTextDocument.setMarkdown() se traga <thought> por ser una
    # etiqueta HTML desconocida, el usuario veia el borrador interno
    # completo SIN las etiquetas, es decir sin ninguna pista de que
    # estaba leyendo razonamiento interno. Ese texto crudo ademas
    # quedaba persistido en memory_graph como si fuera la respuesta.
    #
    # NOTA: esto NO es la fuga que cubre _strip_leaked_reasoning. Aquella
    # es "razonamiento SIN etiquetar mezclado con la respuesta"; esta es
    # "razonamiento BIEN etiquetado y respuesta inexistente". Sus
    # marcadores (encabezados del protocolo: "Analisis de la peticion",
    # "Checklist de comprension"...) no disparan aca, porque el volcado
    # es prosa narrada en primera persona sin ningun encabezado.
    _NO_VISIBLE_ANSWER_ES: str = (
        "No pude redactar una respuesta final para este turno: el modelo consumió "
        "la generación en su borrador interno y no llegó a escribir la respuesta. "
        "Volvé a preguntar, preferentemente acotando la consulta."
    )
    _NO_VISIBLE_ANSWER_EN: str = (
        "I couldn't produce a final answer for this turn: the model spent its "
        "generation on the internal draft and never wrote the answer itself. "
        "Please ask again, ideally narrowing the question."
    )

    # =================================================================
    # STUBS DE LISTA VACÍOS AL final DE LA RESPUESTA
    # =================================================================
    # BUG REAL (medido - captura del usuario, turno fast_path 113-115s,
    # "most important equations in math"): la respuesta terminaba en
    # "1.\n2.\n3." - tres marcadores numerados SIN contenido después. En
    # el mismo turno apareció contenido de Navier-Stokes duplicado
    # dentro de la sección de Maxwell's Equations. Ambos síntomas son
    # consistentes con quedarse sin presupuesto de tokens a mitad de una
    # respuesta larga multi-sección y abrir un ítem de lista que nunca
    # se llega a redactar (ver el techo de Pasada 2 en
    # `MemoryGovernor.split_budget`, y la conversión a generación de una
    # sola pasada para fast_path en `run_turn`, que le da a la respuesta
    # visible hasta `BASE_NUM_PREDICT` completo en vez de la fracción
    # que separaba el modo de dos pasadas).
    #
    # Este patrón no decide si la respuesta se truncó - es un cosmético
    # de salida: si el texto final TERMINA en uno o más marcadores
    # (numerados o con viñeta) sin contenido propio, esos marcadores no
    # le sirven de nada al usuario y se recortan. Deliberadamente
    # conservador: cada unidad exige que, tras el marcador, solo haya
    # espacios/tabs antes del salto de línea o del fin de texto - un
    # ítem con texto real ("3. Tercer punto final") rompe el patrón en
    # esa unidad y NADA se toca, ni ese ítem ni los anteriores.
    # Prefijo `(?:\n[ \t]*)+` (uno o más saltos, no solo uno) en vez de
    # `\n[ \t]*`: markdown "loose list" (ítems separados por línea en
    # blanco, lo que QTextDocument.setMarkdown() suele producir/aceptar)
    # deja una línea vacía ENTRE cada marcador - con un solo `\n`
    # exigido, la unidad no encuentra el marcador justo después y el
    # patrón entero no matchea. medido con la bateria de pruebas: sin
    # este prefijo, "1.\n\n2.\n\n3." (vacíos, separados por línea en
    # blanco) sobrevive intacto; con él, se recorta igual que la
    # variante compacta "1.\n2.\n3.".
    _TRAILING_EMPTY_LIST_RE: Pattern[str] = re.compile(
        r"(?:(?:\n[ \t]*)+(?:[-•*]|\d+[.)])[ \t]*)+\Z"
    )

    @classmethod
    def _strip_trailing_empty_list_stubs(cls, text: str) -> str:
        """
        Recorta marcadores de lista (numerados o con viñeta) que quedan
        colgando al FINAL del texto sin contenido propio — ver el
        BLINDAJE en `_TRAILING_EMPTY_LIST_RE`. No toca listas con
        contenido real, ni marcadores que no estén pegados al final.

        BLINDAJE (bug real, encontrado en la propia batería de pruebas
        de este método): el `\\Z` del patrón exige que el ÚLTIMO
        marcador vacío sea literalmente el último carácter del texto.
        Un `\\n` colgando DESPUÉS del último marcador (lo habitual: el
        modelo cierra su generación con un salto de línea final) rompe
        esa condición y el patrón no matchea nada — el `.rstrip()` de
        abajo, aplicado DESPUÉS de la sustitución, llega tarde para
        arreglarlo. Por eso se aplica `.rstrip()` ANTES también: alinea
        el `\\Z` contra el último carácter real, no contra un salto de
        línea sobrante.
        """
        if not text:
            return text
        return cls._TRAILING_EMPTY_LIST_RE.sub("", text.rstrip()).rstrip()

    # Nota (segunda capa de defensa - ver `_ANSWER_RESTART_STOP_
    # SEQUENCES` para el fix primario, en la llamada a Ollama): ese
    # `stop` corta la generación apenas el modelo intenta reabrir el
    # ancla, así que ahorra tiempo y tokens en el caso normal - pero es
    # una lista de strings exactos, y no cubre variantes que el modelo
    # pueda escribir distinto (mayúscula distinta, negrita markdown
    # "**Respuesta:**", o cualquier forma que Ollama por alguna razón
    # no llegue a cortar). Este stripper es la red de seguridad: si un
    # reinicio duplicado igual llega a la respuesta guardada, se
    # recorta ACÁ antes de mostrarla - no ahorra el tiempo de
    # generación de esa mitad (para eso está el `stop`), pero sí
    # garantiza que el usuario nunca vea el texto duplicado.
    #
    # Exige salto de párrafo (línea en blanco) antes del ancla, no solo
    # un `\n` - así una respuesta que legítimamente ABRE con
    # "Respuesta:"/"Answer:" (el ancla del prompt, no algo que el
    # modelo repite) nunca cae acá: no hay párrafo previo que recortar.
    # El piso de 40 caracteres de contenido previo es la misma idea:
    # descarta coincidencias triviales que no son un reinicio real.
    _ANSWER_RESTART_RE: Pattern[str] = re.compile(
        r"\n[ \t]*\n[ \t]*\**(?:Respuesta|Answer)\**:[ \t]*"
    )

    @classmethod
    def _strip_duplicate_answer_restart(cls, text: str) -> str:
        """
        Recorta un reinicio duplicado de la respuesta (ver el BLINDAJE
        en `_ANSWER_RESTART_RE`): si el texto ya tiene contenido real y
        MÁS ADELANTE aparece un nuevo párrafo que arranca con
        "Respuesta:"/"Answer:", todo desde ahí en adelante es la
        segunda pasada duplicada — se descarta, quedándose con la
        primera respuesta completa.
        """
        if not text:
            return text
        match = cls._ANSWER_RESTART_RE.search(text)
        if not match:
            return text
        prefix = text[:match.start()].rstrip()
        if len(prefix) < 40:
            return text
        return prefix

    def resolve_visible_answer(
        self,
        raw_response: str,
        clean_response: str,
        *,
        active_model: Optional[str] = None,
        lang: Optional[str] = None,
        has_web_evidence: bool = False,
    ) -> Tuple[str, bool]:
        """
        Decide el texto VISIBLE del turno. Devuelve `(texto, hubo_recuperacion)`.

        Garantia dura: NUNCA devuelve el crudo cuando ese crudo es un
        bloque de razonamiento. Antes que mostrar el borrador interno,
        prefiere un mensaje honesto de fallo.

        Cuesta una llamada extra al modelo, pero SOLO en este fallo
        concreto (limpio vacio + bloque <thought> presente), que es raro:
        en el camino normal retorna en la primera linea, sin tocar el
        modelo, asi que no alarga la cadena serial del turno tipico.
        """
        if clean_response and clean_response.strip():
            return clean_response, False

        thought, _ = self._split_thought_and_content(raw_response or "")
        if not thought:
            # Sin bloque de razonamiento no es este fallo: si el crudo
            # trae algo, es una respuesta legitima (o vacia de verdad).
            return (raw_response or "").strip(), False

        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"

        # Reintento acotado: el modelo YA hizo el trabajo de planificar,
        # asi que se le devuelve su propio plan y se le pide unicamente
        # la redaccion que falto. No es un prompt de correccion post-hoc
        # (no corrige nada): produce la respuesta VISIBLE, y por eso
        # lleva la misma instruccion de estilo que _build_reasoning_prompt.
        if is_en:
            retry_prompt = (
                "You already planned this answer internally. This is your own plan:\n\n"
                f"{thought[-1500:]}\n\n"
                "Now write ONLY the final answer for the user. Do not emit any "
                "<thought> block, and do not mention that a plan existed. Develop "
                "each point into at least its own paragraph, explaining the why and "
                "giving context or concrete examples."
            )
        else:
            retry_prompt = (
                "Ya planificaste internamente esta respuesta. Este es tu propio plan:\n\n"
                f"{thought[-1500:]}\n\n"
                "Ahora escribe ÚNICAMENTE la respuesta final para el usuario. No emitas "
                "ningún bloque <thought> ni menciones que hubo un plan. Desarrolla cada "
                "punto en al menos un párrafo propio, explicando el porqué y dando "
                "contexto o ejemplos concretos."
            )

        recovered = self._call_llm(
            retry_prompt,
            target_model=active_model,
            lang_override=lang,
            has_web_evidence=has_web_evidence,
        )
        if recovered and not recovered.lstrip().startswith("[ERROR"):
            _, recovered_clean = self._split_thought_and_content(recovered)
            recovered_clean, _ = self._strip_leaked_reasoning(recovered_clean)
            # Nota: ver `_strip_system_prompt_echo` - este reintento
            # llama a `_call_llm()`, que manda el mismo header congelado
            # (con `[CRITICAL LANGUAGE RULE]` y el bloque del sandbox) que
            # cualquier otra llamada, así que hereda el mismo riesgo de
            # eco.
            recovered_clean, _ = self._strip_system_prompt_echo(recovered_clean)
            if recovered_clean and recovered_clean.strip():
                return recovered_clean.strip(), True

        return (self._NO_VISIBLE_ANSWER_EN if is_en else self._NO_VISIBLE_ANSWER_ES), True

    # Cierres de frase válidos para `_looks_truncated`: puntuación
    # terminal + comillas/paréntesis/corchete de cierre + fin de fórmula
    # LaTeX/código. Si la respuesta (rstrip) NO termina en alguno de
    # estos, probablemente quedó cortada contra el techo de num_predict.
    # OJO: ":" y ";" NO cuentan como cierre - una respuesta que termina
    # en ":" es justo la señal de corte (el "...Por ejemplo:" colgando
    # del video que motivó esto).
    _SENTENCE_END_RE: Pattern[str] = re.compile(r"[.!?…)\]}\"'»”’`*_]$")

    @classmethod
    def _looks_truncated(cls, text: str) -> bool:
        """
        Heurística barata: ¿esta respuesta parece cortada a mitad?
        Se usa SOLO cuando Ollama ya reportó `done_reason == "length"`
        (llegó al techo de `num_predict`) — no intenta adivinar por su
        cuenta. `True` dispara UNA llamada de continuación en `run_turn`
        (rama FAST_PATH). Ver el turno 2 del video que motivó esto: la
        respuesta terminaba en "...Por ejemplo:" y nada más.
        """
        if not text:
            return False
        stripped = text.rstrip()
        if not stripped:
            return False
        # Quedó un marcador de lista vacío colgando ("Por ejemplo:\n1.\n2.")
        if cls._strip_trailing_empty_list_stubs(stripped) != stripped:
            return True
        last_line = stripped.splitlines()[-1].strip()
        # Una línea que abre lista/encabezado sin contenido después.
        if re.fullmatch(r"(?:[-*+]|\d+[.)]|#{1,6})\s*.{0,3}", last_line):
            return True
        # No cierra con puntuación ni con un delimitador de cierre.
        if not cls._SENTENCE_END_RE.search(stripped):
            return True
        return False

    @staticmethod
    def _build_continuation_prompt(tail: str, lang: str) -> str:
        """
        Prompt para terminar una respuesta que se cortó contra el techo
        de tokens (ver `_looks_truncated`). Se le devuelve al modelo la
        cola de lo ya escrito y se le pide SOLO el cierre, sin repetir.
        """
        if lang == "English":
            return (
                "This answer was cut off at the token limit. Continue it from "
                "EXACTLY where it stops, without repeating anything already "
                "written and without re-introducing the topic. Finish the "
                "current thought and close cleanly:\n\n"
                f"{tail}"
            )
        return (
            "Esta respuesta quedó cortada por el límite de tokens. Continuála "
            "EXACTAMENTE desde donde termina, sin repetir nada de lo ya escrito "
            "y sin reintroducir el tema. Terminá la idea en curso y cerrá de "
            "forma limpia:\n\n"
            f"{tail}"
        )

    # Marcas de que el modelo se descarriló y está volcando el andamiaje
    # del prompt como si fuera respuesta (además de las de
    # `_SYSTEM_PROMPT_ECHO_MARKERS_RE`): la firma `Verificación: print(`
    # del protocolo <thought_code>, una etiqueta de razonamiento suelta,
    # o la palabra `thought_code` a secas. Video 2026-08-27, "hi".
    _FASTPATH_ECHO_RE: Pattern[str] = re.compile(
        r"verificaci[oó]n\s*:\s*print\s*\(|verification\s*:\s*print\s*\(|"
        r"</?thought(?:_code)?\b|\[/?thought(?:_code)?\]|"
        r"</?response_code>|"  # etiqueta inventada por phi3.5, ver nota de abajo
        r"REAL-TIME SANDBOX VERIFICATION|VERIFICACI[OÓ]N EN TIEMPO REAL|"
        # Eco del SCHEMA de herramientas volcado como prosa (screenshot
        # 2026-08-27, "tell me the most important equations of math"):
        # el modelo repitió `{"tool": "run_cmd"...}`, `{"name": 'listDir'...}`,
        # `"commandParams"`, `"requiredFields"`, y alucinó transcripts de
        # shell falsos (`bash $ ls -la`, `bash python -m sympy console`).
        r"\{\s*[\"']tool[\"']\s*:.*[\"']tool[\"']\s*:|"     # 2+ objetos {"tool":...}
        # Bug real, MEDIDO (segunda captura, screenshot 2026-08-27, MISMA
        # pregunta pero en slow_path): un solo `{"tool": null ...}` — sin
        # un segundo objeto — seguido de un comentario `//` explicando por
        # qué no hace falta herramienta, que después se degradaba en
        # cientos de palabras sin relación. Una respuesta real NUNCA
        # contiene el fragmento crudo `{"tool":` — o se extrae como llamada
        # real más arriba (`extract_tool_call`), o no debería aparecer en
        # el texto visible en absoluto. Un solo objeto ya alcanza.
        r"\{\s*[\"']tool[\"']\s*:|"
        r"[\"']commandParams[\"']|[\"']requiredFields[\"']|"
        r"[\"']name[\"']\s*:\s*[\"']list[Dd]ir|"
        # transcript de shell falso: una palabra-shell seguida de `$` o de
        # un comando, sin puntuación (forma típica de la alucinación; una
        # respuesta real escribe `sudo apt-get` entre backticks, no
        # "bash $ sudo"):
        r"\b(?:bash|shell|sh|cmd|powershell)\s+(?:\$\s|python\b|pip3?\b|conda\b|"
        r"anaconda\b|sudo\b|apt(?:-get)?\b|npm\b|git\b)|"
        r"knowledge (?:being )?cut off in early 20\d\d|"
        r"cannot provide latest updates post facto",
        re.IGNORECASE | re.DOTALL,
    )

    _SAFE_FALLBACK_ES: str = (
        "Perdón, me perdí generando la respuesta a eso. ¿Podés reformular la "
        "pregunta de otra forma, un poco más específica?"
    )
    _SAFE_FALLBACK_EN: str = (
        "Sorry, I lost the thread generating that answer. Could you rephrase the "
        "question, ideally a bit more specifically?"
    )

    @classmethod
    def _fastpath_response_looks_broken(
        cls, user_input: str, response: str, web_success: bool, *,
        is_regen: bool = False, hit_ceiling: bool = False,
    ) -> Optional[str]:
        """
        Circuit-breaker de fast_path: ¿esta respuesta es basura de un
        modelo que se descarriló (repite el system prompt/el schema de
        herramientas, alucina comandos de shell, o llena todo el
        presupuesto de tokens)? Devuelve el motivo (str) o `None`.
        Solo se llama en el carril fast_path — el slow_path tiene sus
        propios verificadores. Umbrales conservadores: un falso positivo
        dispara UNA regeneración con prompt mínimo, no un fallo del turno.

        `hit_ceiling=True` — Ollama reportó `done_reason == "length"`: la
        generación llenó `num_predict` entero. En fast_path (carril
        SIMPLE, con techo bajo `FASTPATH_NUM_PREDICT`) eso es señal de
        descarrilamiento, INDEPENDIENTE de si hubo búsqueda web —
        screenshot 2026-08-27: `decode=4096tok/100s` de basura CON
        contexto web de 5 fuentes.

        `is_regen=True` — se re-chequea la salida de la propia
        regeneración (ya acotada por prompt + `num_predict`): solo
        importa el eco, no la longitud.
        """
        if not response or not response.strip():
            return None
        body = response.strip()

        if cls._SYSTEM_PROMPT_ECHO_MARKERS_RE.search(body) or cls._FASTPATH_ECHO_RE.search(body):
            return "eco del prompt de sistema / schema de herramientas"

        # Modo de descarrilamiento REAL de gpt-oss (MEDIDO, PASO 0 probe4
        # C3): un bucle de repetición de una subcadena corta hasta llenar
        # el techo. Se chequea SIEMPRE — incluso en is_regen: una regen que
        # degenera igual debe caer al fallback seguro.
        if cls._looks_degenerate_repetition(body):
            return "bucle de repetición degenerativo"

        # Narración del canal analysis de Harmony que sobrevivió a
        # `_strip_harmony_leak` (arranque fuerte al principio).
        if cls._HARMONY_STRONG_LEAD_RE.match(body):
            return "fuga del canal analysis de Harmony"

        if is_regen:
            return None

        # RECALIBRADO para gpt-oss:20b (PASO 0 — ver STEP0_HARMONY_
        # FINDINGS.md). gpt-oss es bastante MÁS verboso que phi3.5: una
        # respuesta fast_path legítima a una pregunta conceptual corta
        # ronda 1800 chars (MEDIDO: "¿qué es la entropía?" -> 1793). Los
        # techos de abajo se subieron en consecuencia; el descarrilamiento
        # real de gpt-oss lo agarran los dos chequeos de arriba (bucle,
        # fuga analysis) y `_FASTPATH_ECHO_RE`, no la longitud.

        # Llenó el presupuesto entero de tokens en un carril "simple" Y la
        # respuesta es enorme → descarrilado (aplica con o sin web).
        if hit_ceiling and len(body) > 2600:
            return "generación desbocada (llenó el techo de tokens)"

        # input muy corto, sin evidencia web, y una parrafada absurda.
        if not web_success and len(body) > 3200 and len(user_input.strip()) < 40:
            return "respuesta desproporcionada para una consulta breve"

        # relación grosera entre pregunta y respuesta.
        if not web_success and len(body) > 130 * max(len(user_input.strip()), 12):
            return "relación pregunta/respuesta desproporcionada"

        return None

    @classmethod
    def _slowpath_response_looks_broken(cls, response: str) -> Optional[str]:
        """
        Circuit-breaker de slow_path — versión mínima de
        `_fastpath_response_looks_broken`, para el otro carril.

        Bug real, MEDIDO (screenshot 2026-08-27, "can you tell me the
        most important[s] equations in math?", ruteado a slow_path): la
        respuesta visible fue enteramente `<response_code> { "tool":
        null // ... }` seguido de cientos de palabras de relleno sin
        relación con la pregunta — nunca llegó a responder nada. Los
        verificadores propios de slow_path (verify_response_against_
        sources, find_unattributed_contradiction, etc.) NO detectan
        esto: chequean precisión FACTUAL contra fuentes, no si la
        respuesta es coherente en absoluto — una respuesta que no
        afirma ningún hecho verificable simplemente no dispara ninguno,
        y el turno se loguea como exitoso.

        A propósito NO reusa los umbrales de LONGITUD de
        `_fastpath_response_looks_broken` (desproporción respuesta/
        pregunta, techo de tokens lleno): una respuesta larga es NORMAL
        y esperada en slow_path (`_FINAL_ANSWER_STYLE_ES/EN` la exige),
        así que aplicar esos umbrales acá dispararía sobre respuestas
        buenas todo el tiempo. Chequea:
          - el mismo eco de andamiaje/schema que `_FASTPATH_ECHO_RE` —
            inequívoco en cualquier carril;
          - el bucle de repetición degenerativo y la fuga del canal
            analysis de Harmony (`_strip_harmony_leak` corre antes, pero
            un arranque fuerte que sobreviva es señal de turno roto) —
            ambos MEDIDOS con gpt-oss:20b en el PASO 0, ninguno depende de
            la longitud.

        Sin regeneración propia todavía (a diferencia del breaker de
        fast_path): cae directo al mensaje seguro. Un prompt de
        regeneración a medida para slow_path (que reinyecte el contexto
        web ya recuperado) queda como mejora futura si hace falta.
        """
        if not response or not response.strip():
            return None
        if cls._FASTPATH_ECHO_RE.search(response):
            return "eco de schema de herramientas / etiqueta interna en slow_path"
        if cls._looks_degenerate_repetition(response):
            return "bucle de repetición degenerativo en slow_path"
        if cls._HARMONY_STRONG_LEAD_RE.match(response.strip()):
            return "fuga del canal analysis de Harmony en slow_path"
        return None

    @staticmethod
    def _build_fastpath_regen_prompt(user_input: str, lang: str) -> str:
        """
        Prompt mínimo para la regeneración del circuit-breaker.
        Deliberadamente MUY acotado: la generación normal ya se
        descarriló, así que se le pide al modelo lo más chico que puede
        entregar sin volver a irse — 2-3 frases, prosa plana, sin listas
        (una lista de 10 ítems es justo lo que phi3.5:3.8b no sostiene).
        """
        if lang == "English":
            return (
                "Your previous attempt went off track. Answer the question below in "
                "AT MOST 3 plain sentences, in English. No lists, no numbered "
                "items, no headings, no tags, no preamble, no example questions, "
                "no \"for deeper understanding\" note. Write math as inline LaTeX "
                "between $ signs (e.g. $E = mc^2$). If it is a broad or subjective "
                "question, give the 2-3 best-known examples in one sentence and "
                "stop. Then STOP — do not add anything else.\n\n"
                f"{user_input}"
            )
        return (
            "Tu intento anterior se descarriló. Respondé la pregunta de abajo en "
            "COMO MÁXIMO 3 frases planas, en español. Sin listas, sin ítems "
            "numerados, sin encabezados, sin etiquetas, sin preámbulo, sin "
            "preguntas de ejemplo, sin nota de \"para profundizar\". Escribí la "
            "matemática como LaTeX inline entre signos $ (p. ej. $E = mc^2$). Si "
            "es una pregunta amplia o subjetiva, dá los 2-3 ejemplos más conocidos "
            "en una frase y terminá. Después PARÁ — no agregues nada más.\n\n"
            f"{user_input}"
        )

    # Frases con las que phi3.5:3.8b arranca a rellenar en fast_path
    # después de haber respondido (screenshot 2026-08-27: la respuesta
    # buena a "3 most important equations" venía seguida de "Note that
    # while these hold wide significance..." y de un "Example Question
    # with Specific Answer Request:" cortado a mitad). Si aparece una de
    # estas al principio de una oración/línea, se corta todo desde ahí.
    _FASTPATH_PADDING_RE: Pattern[str] = re.compile(
        r"(?:^|\n|(?<=[.!?])\s)\s*(?:"
        r"example question(?:s)?\b|pregunta(?:s)? de ejemplo\b|"
        r"for (?:a )?deeper understanding\b|para (?:profundizar|un entendimiento)\b|"
        r"note that while\b|(?:ten[eé] en cuenta|nota) que si bien\b|"
        r"herein lies\b|moreover,? this\b|as (?:previously |already )?mentioned\b|"
        r"to elaborate (?:further|on)\b|para elaborar (?:m[aá]s|sobre)\b|"
        r"it(?:'s| is) (?:also )?worth noting\b|vale (?:la pena )?(?:notar|aclarar)\b|"
        r"further (?:reading|exploration|advancement)\b|"
        r"if you(?:'d| would)? like (?:me )?to (?:elaborate|expand|go deeper)\b"
        r")",
        re.IGNORECASE,
    )

    @classmethod
    def _trim_fastpath_padding(cls, text: str) -> str:
        """
        Recorta el relleno de seguimiento que phi3.5 agrega en fast_path
        DESPUÉS de haber respondido: "Example Question with...", "Note
        that while these hold...", "For deeper understanding...", etc.
        (ver `_FASTPATH_PADDING_RE`). SOLO corta en esos marcadores
        explícitos — una respuesta cortada a mitad SIN marcador es trabajo
        de la guarda de continuación, no de esto. Conservador: si al
        recortar quedaría muy poco (<20 chars), devuelve el original.
        """
        if not text or not text.strip():
            return text
        m = cls._FASTPATH_PADDING_RE.search(text)
        if not m or m.start() < 20:
            return text
        trimmed = text[:m.start()].rstrip()
        return trimmed if len(trimmed) >= 20 else text

    def fetch_hybrid_context(self, user_input: str, limit: int = 3) -> str:
        """Combina recuperación FTS5 y vectorial recortando el contexto antes del desbordamiento."""
        # 1. Recuperación por palabras clave en SQLite (FTS5)
        lexical_results = []
        if hasattr(self, 'memory_graph') and self.memory_graph:
            lexical_results = self.memory_graph.fetch_relevant_context(user_input, limit=limit)
        
        # 2. Búsqueda semántica si el módulo vectorial está presente.
        # LocalVectorRAG.search() espera un vector de embedding, no texto
        # crudo: hay que vectorizar la consulta antes de invocarlo.
        vector_results = []
        if hasattr(self, 'vector_rag') and self.vector_rag and getattr(self.vector_rag, 'index', None) is not None:
            query_vector = get_embedding(user_input)
            if query_vector is not None:
                vector_results = self.vector_rag.search(query_vector, top_k=limit)


        # 3. Deduplicación conservando orden
        combined = list(dict.fromkeys(lexical_results + vector_results))
        context_text = "\n---\n".join(combined)
        
        # 4. Poda adaptativa según límite de caracteres
        max_chars = getattr(self, 'MAX_CONTEXT_CHARS_FOR_PROMPT', 1200)
        if len(context_text) > max_chars:
            return context_text[:max_chars] + "\n...[Contexto truncado por límite de ventana]"

        return context_text

    @staticmethod
    def _format_cached_web_knowledge(entries: List[Dict[str, Any]]) -> str:
        """
        Formatea hits de memoria web persistida en el mismo estilo que
        format_search_results(), para que el prompt no distinga si la
        evidencia vino de la red ahora mismo o de una investigación previa.

        Prioriza `summary` (resumen extractivo ya calculado, ver
        `Orchestrator.summarize_sources_map_reduce` / punto 5 del
        pipeline de resúmenes en memory_graph.py) sobre `content` (texto
        crudo completo) cuando esa entrada ya tiene uno persistido — así
        una fuente reutilizada desde caché llega al prompt tan corta y
        enfocada como si se hubiera resumido en este mismo turno, sin
        pagar otra mini-llamada al modelo.
        """
        lines = ["--- CONOCIMIENTO WEB PREVIAMENTE VERIFICADO (MEMORIA LOCAL) ---"]
        for i, entry in enumerate(entries, 1):
            body = str(entry.get("summary") or "").strip() or str(entry.get("content", ""))
            lines.append(
                f"[{i}] {entry.get('title', '')}\n"
                f"Fuente: {entry.get('url', '')}\n"
                f"Contenido: {body[:1500]}\n"
            )
        return "\n".join(lines)

    def _persist_web_knowledge(self, query: str, results: List[Dict[str, Any]]) -> None:
        """
        Corre en un hilo daemon aparte del turno de chat: guarda los
        resultados web exitosos en MemoryGraph (durable, siempre
        disponible) y, si hay un índice vectorial disponible, también los
        vectoriza para habilitar recuperación semántica futura vía
        fetch_hybrid_context(). Cualquier fallo aquí es solo un log: nunca
        debe afectar al turno de chat que ya se le devolvió al usuario.
        """
        try:
            stored = self.memory_graph.store_web_knowledge(query, results)
            if stored:
                logger.info("💾 [Web-RAG] %d resultado(s) web persistido(s) en memoria local.", stored)
        except Exception as exc:
            logger.warning("No se pudo persistir conocimiento web en MemoryGraph: %s", exc)

        if not (hasattr(self, 'vector_rag') and self.vector_rag and getattr(self.vector_rag, 'index', None) is not None):
            return

        try:
            texts: List[str] = []
            vectors: List[List[float]] = []
            for r in results:
                content = str(r.get("content") or r.get("snippet") or "").strip()
                if not content:
                    continue
                vector = get_embedding(content)
                if vector is None:
                    continue
                texts.append(f"[WEB] {r.get('domain', '')} | {r.get('title', '')}\n{content[:1000]}")
                vectors.append(vector)

            if texts:
                self.vector_rag.add_documents(texts, vectors)
                logger.info("🧠 [Vector-RAG] %d documento(s) web vectorizado(s).", len(texts))
        except Exception as exc:
            logger.debug("Vectorización de conocimiento web omitida: %s", exc)

    def synthesize_and_run_dynamic_tool(self, task_description: str) -> str:
        """
        Solicita al modelo coder la generación de un script de Python y lo
        ejecuta en el Sandbox.

        Optimización #5 — Persistencia de herramientas validadas: antes de
        pagar una llamada al modelo coder, se busca en
        MemoryGraph.validated_tools un script YA validado (pasó AST +
        ejecución exitosa) para una tarea léxicamente similar. Si hay hit,
        se reejecuta directamente — 0 tokens, latencia de milisegundos en
        vez de una generación completa. Solo si no hay hit se genera desde
        cero, y si el resultado es exitoso, se persiste para reutilizarse
        en el futuro sin volver a regenerarlo.
        """
        cached_tool = None
        with contextlib.suppress(Exception):
            cached_tool = self.memory_graph.fetch_validated_tool(task_description)

        if cached_tool:
            success, result = self.dynamic_tool_engine.execute_sandboxed(cached_tool["code"])
            if success:
                logger.info(
                    "♻️ [DynamicToolEngine] Herramienta validada reutilizada sin regenerar (uso #%d).",
                    cached_tool["use_count"],
                )
                return f"**[HERRAMIENTA DINÁMICA REUTILIZADA]**\n```text\n{result}\n```"
            # El script cacheado falló en este contexto (p. ej. dependía de
            # datos que ya no aplican): se descarta el atajo y se cae al
            # camino normal de generación más abajo, sin abortar la tarea.

        prompt = (
            "[MODO GENERACIÓN DE HERRAMIENTA DINÁMICA]:\n"
            f"La siguiente tarea requiere una herramienta que no existe en el catálogo: {task_description}\n\n"
            "Escribe un script de Python autónomo que resuelva el problema. Imprime el resultado con `print()` "
            "o asígnalo a una variable llamada `result`.\n"
            "REGLAS:\n"
            "1. NO importes módulos del sistema como 'subprocess', 'shutil' o 'socket'.\n"
            "2. Devuelve ÚNICAMENTE el código dentro de un bloque ```python ... ```."
        )

        code_response = self._call_llm(prompt, target_model=self.coder_model)
        code_blocks = _CODE_FENCE_RE.findall(code_response)

        if not code_blocks:
            return "No se pudo extraer un bloque de código válido para la herramienta dinámica."

        script = code_blocks[0].strip()
        success, result = self.dynamic_tool_engine.execute_sandboxed(script)

        if success:
            with contextlib.suppress(Exception):
                self.memory_graph.store_validated_tool(task_description, script)
            return f"**[HERRAMIENTA DINÁMICA EJECUTADA]**\n```text\n{result}\n```"
        else:
            return f"**[FALLO EN HERRAMIENTA DINÁMICA]**\n{result}"

    def extract_tool_call(self, response_text: str) -> Optional[dict]:
        """Extrae y normaliza la llamada a herramienta aislando previamente el bloque <thought>."""
        if not response_text:
            return None
            
        # Extraer únicamente el contenido fuera del bloque de pensamiento
        _, clean_text = self._split_thought_and_content(response_text)
        target_text = clean_text if clean_text else response_text
            
        raw_dict = None
        
        # 1. Intentar extraer un bloque JSON dentro de marcas Markdown (```json ... ```)
        json_fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", target_text, re.DOTALL)
        if json_fence_match:
            candidate = json_fence_match.group(1)
            with contextlib.suppress(Exception):
                raw_dict = json.loads(candidate)
                
        # 2. Buscar cualquier estructura entre llaves '{ ... }' en el texto limpio
        if not raw_dict:
            brace_match = re.search(r"(\{.*\})", target_text, re.DOTALL)
            if brace_match:
                candidate = brace_match.group(1)
                with contextlib.suppress(Exception):
                    raw_dict = json.loads(candidate)
                    
        # 3. Recurrir al parser robusto general como respaldo
        if not raw_dict:
            raw_dict = RobustJSONParser.extract_and_repair(target_text)
            
        return self.normalize_tool_call(raw_dict)

    @staticmethod
    def normalize_tool_call(tool_call: Any) -> Optional[dict]:
        """
        Versión Super Reforzada de normalize_tool_call.
        Desempaqueta estructuras anidadas complejas, normaliza alias masivos de herramientas 
        para modelos de 3B y repara estructuras de parámetros corruptas o serializadas.
        """
        if not tool_call:
            return None

        # Si el modelo devolvió una lista con un solo elemento
        if isinstance(tool_call, list) and len(tool_call) > 0:
            tool_call = tool_call[0]

        if not isinstance(tool_call, dict):
            return None

        # 1. Desempaquetado recursivo de contenedores de envoltura profundos
        wrappers = ["tool_call", "function_call", "call", "execution", "function", "tool_use", "action_input"]
        for wrapper in wrappers:
            if wrapper in tool_call and isinstance(tool_call[wrapper], dict):
                tool_call = tool_call[wrapper]

        # 2. Mapear claves alternativas para el NOMBRE de la herramienta
        tool_name_keys = ["tool", "name", "action", "function", "function_name", "tool_name", "command", "func", "method"]
        tool_name = None
        for key in tool_name_keys:
            if key in tool_call and tool_call[key]:
                tool_name = str(tool_call[key]).strip().lower()
                break

        if not tool_name:
            return None

        # Limpiar cualquier caracter de marcado o puntuación en el nombre
        tool_name = re.sub(r"[^a-z0-9_]", "", tool_name)

        # 3. Mapear claves alternativas para los PARÁMETROS
        param_keys = ["parameters", "arguments", "params", "args", "input", "payload", "kwargs"]
        raw_params = None
        for key in param_keys:
            if key in tool_call:
                raw_params = tool_call[key]
                break

        # Si los parámetros venían codificados como un string JSON, parsearlo
        if isinstance(raw_params, str):
            raw_params_str = raw_params.strip()
            if raw_params_str.startswith("{") and raw_params_str.endswith("}"):
                with contextlib.suppress(Exception):
                    raw_params = json.loads(raw_params_str)

        # Si no hay bloque explícito, recoger los argumentos sueltos en la raíz
        if raw_params is None:
            raw_params = {
                k: v for k, v in tool_call.items() 
                if k not in tool_name_keys and k not in wrappers
            }

        # Garantizar que params sea un diccionario
        if isinstance(raw_params, dict):
            params_dict = raw_params
        elif isinstance(raw_params, list):
            params_dict = {"args": raw_params}
        else:
            params_dict = {"value": raw_params} if raw_params is not None else {}

        # 4. MAPEO DE ALIAS MASIVO Y NORMALIZACIÓN DE PARÁMETROS

        # --- A. SYSTEM_TELEMETRY ---
        telemetry_aliases = {
            "hardware_health_monitor", "hardware_monitor", "system_health", 
            "sys_info", "telemetry", "system_info", "get_telemetry", 
            "system_telemetry", "pc_status", "cpu_info", "ram_info", 
            "hardware_status", "check_system", "telemetria", "telemetría"
        }
        if tool_name in telemetry_aliases:
            return {
                "tool": "system_telemetry",
                "parameters": {}
            }

        # --- B. READ_FILE ---
        read_file_aliases = {
            "file_read", "file_inspect", "inspect_file", "leer_archivo", 
            "read", "read_file", "get_file", "open_file", "cat", "view_file"
        }
        if tool_name in read_file_aliases:
            target_path = "sovnode_qt.py"
            path_val = (
                params_dict.get("path") 
                or params_dict.get("filename") 
                or params_dict.get("file") 
                or params_dict.get("filepath") 
                or params_dict.get("target")
            )
            if isinstance(path_val, str) and path_val.strip() and path_val.strip() not in [".", "./"]:
                target_path = path_val.strip()
            else:
                for v in params_dict.values():
                    if isinstance(v, str) and ("." in v or "/" in v) and v.strip() not in [".", "./"]:
                        target_path = v.strip()
                        break

            # Nota ANTI-ALUCINACIÓN: Bloquea intentos de leer archivos fantasma de contexto
            path_lower = target_path.lower()
            if any(pat in path_lower for pat in ["web_context", "context_web", "web_results", "search_results"]):
                logger.warning("🛡️ [ToolGuard] Interceptada alucinación de read_file sobre contexto web: %s", target_path)
                return None

            return {
                "tool": "read_file",
                "parameters": {"path": target_path}
            }

        # --- C. WRITE_FILE ---
        write_file_aliases = {
            "file_write", "write_file", "save_file", "create_file", 
            "escribir_archivo", "write"
        }
        if tool_name in write_file_aliases:
            path_val = params_dict.get("path") or params_dict.get("filename") or params_dict.get("file") or params_dict.get("filepath") or "output.txt"
            content_val = params_dict.get("content") or params_dict.get("text") or params_dict.get("code") or params_dict.get("data") or ""
            return {
                "tool": "write_file",
                "parameters": {
                    "path": str(path_val).strip(),
                    "content": str(content_val)
                }
            }

        # --- D. LIST_DIR ---
        list_dir_aliases = {
            "dir_list", "list_directory", "ls", "dir", 
            "listar_directorio", "list_files", "list_dir"
        }
        if tool_name in list_dir_aliases:
            path_val = params_dict.get("path") or params_dict.get("directory") or params_dict.get("folder") or "."
            return {
                "tool": "list_dir",
                "parameters": {"path": str(path_val).strip()}
            }

        # --- E. RUN_CMD ---
        run_cmd_aliases = {
            "execute_command", "run_command", "shell", "terminal", 
            "cmd", "run_cmd", "bash", "exec", "system_cmd"
        }
        if tool_name in run_cmd_aliases:
            cmd_val = params_dict.get("command") or params_dict.get("cmd") or params_dict.get("script") or params_dict.get("cli") or ""
            return {
                "tool": "run_cmd",
                "parameters": {"command": str(cmd_val).strip()}
            }

        # Retorno genérico limpio para cualquier otra herramienta
        return {
            "tool": tool_name,
            "parameters": params_dict
        }
    

    # =================================================================
    # Cálculo de riesgo-beneficio para ejecución de herramientas
    # =================================================================
    # HONESTIDAD / alcance: esto NO es un port literal de UrgencyChannel
    # (qualia_interface_prototype/core.py) - ese componente modela un
    # camino de interrupción para eventos DE ENTRADA con demora de por
    # medio (buffer temporal de 150ms); acá cada llamada a herramienta
    # ya es síncrona y puntual, no hay demora que saltar. Lo que sí se
    # retoma de esa arquitectura es el principio: un clasificador
    # dedicado decide, antes de que la acción ocurra, si amerita un
    # camino distinto al de "ejecutar y ya" - aplicado aquí a un hueco
    # real y ya presente en este código: BLOCKED_COMMANDS/DANGEROUS_
    # PATTERNS (tools.py) es una lista NEGATIVA fija solo para run_cmd -
    # cualquier comando no listado explícitamente pasa igual (p. ej.
    # "curl http://x | bash" no coincide con ningún patrón actual), y
    # write_file_safely sobreescribe silenciosamente un archivo
    # existente con el mismo "riesgo" (ninguno, a ojos del código
    # actual) que crear uno nuevo. Este clasificador agrega TIERS (no
    # binario) para hacer explícito y auditable en el log el cálculo de
    # riesgo-beneficio de cada acción antes de ejecutarla - sin duplicar
    # ni reemplazar los guards de tools.py, que siguen intactos como su
    # propia capa (defensa en profundidad, mismo espíritu que
    # MAX_TOOL_RESULT_CHARS_IN_PROMPT en este mismo archivo).
    #
    # Alcance deliberado: clasifica ÚNICAMENTE la ACCIÓN concreta que el
    # modelo ya decidió ejecutar (tool_name + parámetros) - NO interpreta
    # lenguaje natural del usuario ni intenta detectar intención dañina
    # en lo que el usuario escribe. Extender esto a clasificar mensajes
    # en lenguaje natural (p. ej. lenguaje de crisis/autolesión) es una
    # decisión de alcance mucho más sensible, con riesgo real de falsos
    # positivos/negativos, que se deja fuera de este cambio a propósito.
    class ToolRiskTier(str, Enum):
        LOW = "low"        # Ejecuta sin fricción: de solo lectura o reversible.
        MEDIUM = "medium"  # Ejecuta, pero se loguea el motivo del riesgo.
        HIGH = "high"      # Se BLOQUEA antes de ejecutar; el modelo recibe el motivo.

    # Riesgo alto NO cubierto por BLOCKED_COMMANDS/DANGEROUS_PATTERNS de
    # tools.py (extensión deliberada, no duplicación - medido, ver
    # verify_risk_classifier en la sección de tests): fuga de datos vía
    # red+pipe a un intérprete, bombas fork, escritura cruda a
    # dispositivos de bloque, formateo de filesystems, y permisos/dueño
    # recursivos sobre una raíz del sistema.
    _HIGH_RISK_CMD_PATTERNS: Tuple[Pattern[str], ...] = (
        re.compile(r"(curl|wget|invoke-webrequest|iwr)\b.*\|\s*(sh|bash|zsh|powershell|iex)\b", re.IGNORECASE),
        re.compile(r":\(\)\s*\{\s*:\s*\|\s*:&\s*\}\s*;\s*:"),
        re.compile(r"\bdd\b.*\bof=\s*/dev/(sd|nvme|hd)", re.IGNORECASE),
        re.compile(r"\b(chmod|chown|chattr)\b\s+-R\b.*(\s/\s*$|\s[A-Za-z]:\\\s*$)", re.IGNORECASE),
        re.compile(r"\bmkfs(\.\w+)?\b", re.IGNORECASE),
    )

    # Riesgo medio: muta estado del sistema o toca la red, pero no es
    # destructivo por sí mismo - se ejecuta igual, solo se hace visible
    # en el log para que el cálculo de riesgo-beneficio sea auditable en
    # vez de invisible.
    _MEDIUM_RISK_CMD_PATTERNS: Tuple[Pattern[str], ...] = (
        re.compile(r"\b(curl|wget|invoke-webrequest|iwr)\b", re.IGNORECASE),
        re.compile(r"\b(pip|pip3)\s+(install|uninstall)\b", re.IGNORECASE),
        re.compile(r"\b(npm|yarn|pnpm)\s+(install|uninstall|remove)\b", re.IGNORECASE),
        re.compile(r"\bgit\s+push\b", re.IGNORECASE),
        re.compile(r"[|>]"),
    )

    @classmethod
    def _classify_tool_risk(cls, tool_name: str, params: Dict[str, Any]) -> Tuple["Orchestrator.ToolRiskTier", str]:
        """
        Clasifica la acción (tool_name + parámetros) ya decidida por el
        modelo, ANTES de ejecutarla. Devuelve (tier, motivo) — motivo
        siempre en texto legible, para loguear o para explicarle al
        modelo por qué se bloqueó, nunca un código opaco.

        MEDIBLE: dado un tool_name y params fijos, el resultado es
        determinístico — mismo input, mismo tier, siempre (ver tests).
        """
        if tool_name == "run_cmd":
            command = str(params.get("command", ""))
            for pattern in cls._HIGH_RISK_CMD_PATTERNS:
                if pattern.search(command):
                    return cls.ToolRiskTier.HIGH, (
                        f"comando coincide con patrón de alto riesgo no cubierto "
                        f"por el sandbox: {pattern.pattern}"
                    )
            for pattern in cls._MEDIUM_RISK_CMD_PATTERNS:
                if pattern.search(command):
                    return cls.ToolRiskTier.MEDIUM, (
                        f"comando muta estado del sistema o usa red/redirección: {command!r}"
                    )
            return cls.ToolRiskTier.LOW, "comando no reconocido como mutante ni riesgoso"

        if tool_name == "write_file":
            path_val = str(params.get("path", ""))
            try:
                exists_already = bool(path_val) and os.path.exists(os.path.expanduser(path_val))
            except Exception:
                exists_already = True  # No se pudo verificar: se asume el caso más cauto.
            if exists_already:
                return cls.ToolRiskTier.MEDIUM, f"sobrescribe un archivo ya existente: {path_val!r}"
            return cls.ToolRiskTier.LOW, "crea un archivo nuevo, no sobrescribe nada"

        if tool_name in ("read_file", "list_dir", "system_telemetry"):
            return cls.ToolRiskTier.LOW, "herramienta de solo lectura / informativa"

        # Herramienta no reconocida (típicamente una personalizada de
        # custom_tools.py): este clasificador no tiene visibilidad del
        # comando real que esa herramienta arma internamente antes de
        # llamar a sandbox.run_cmd_safely() (ver la nota en
        # LocalToolDispatcher._register_custom_tools, tools.py) -
        # pretender clasificarla con precisión sería falso. MEDIO es el
        # punto medio honesto: ni se bloquea a ciegas (HIGH) ni se asume
        # inocua sin poder verla (LOW).
        return cls.ToolRiskTier.MEDIUM, (
            "herramienta no reconocida por el clasificador (posible tool "
            "personalizada) — sin visibilidad del comando real"
        )

    def execute_tool_from_call(self, tool_call: dict) -> str:
        tool_name = tool_call.get("tool")
        params = tool_call.get("parameters", {})

        # Intercepción temprana si la herramienta fue anulada por el filtro
        if not tool_name:
            return "[AVISO DEL SISTEMA]: La información web solicitada ya está inyectada en el prompt. No requieres usar herramientas."

        # ---------- Cálculo de riesgo-beneficio pre-ejecución ----------
        risk_tier, risk_reason = self._classify_tool_risk(tool_name, params)
        if risk_tier == self.ToolRiskTier.HIGH:
            logger.warning(
                "🛡️ [RiskGate] BLOQUEADA ejecución de %s (alto riesgo): %s",
                tool_name, risk_reason,
            )
            return (
                "[INSTRUCCIÓN DEL SISTEMA]: Esta acción fue bloqueada antes de "
                f"ejecutarse por riesgo alto ({risk_reason}). No la reintentes de "
                "una forma distinta para evadir el bloqueo — explicale al usuario "
                "qué querías hacer y por qué no se ejecutó, y proponé una "
                "alternativa más segura si existe."
            )
        elif risk_tier == self.ToolRiskTier.MEDIUM:
            logger.info(
                "⚠️ [RiskGate] Ejecutando %s con riesgo medio: %s",
                tool_name, risk_reason,
            )

        result = self.tools.execute(tool_name, **params)

        # Nota (medido): "web_search" nunca estuvo
        # registrada en LocalToolDispatcher/TOOLS_SCHEMA (tools.py) - la
        # búsqueda web real corre antes del turno, vía el pipeline del
        # router (`web_search_fn`/`fetch_rich_web_search`), no como una
        # herramienta invocable por el modelo. Un modelo de 3B/7B, ante
        # resultados de búsqueda pobres o irrelevantes ya inyectados en
        # el prompt, puede igual "inventar" una llamada a una herramienta
        # de nombre genérico ("web_search", muy común en datasets de
        # tool-calling) esperando que exista. Sin este guard, el
        # dispatcher devuelve el error genérico "Herramienta 'web_search'
        # no disponible" tal cual, y el modelo lo reporta VERBATIM al
        # usuario ("la herramienta... no está disponible en este
        # momento") - descartando el contexto web real que sí se había
        # recuperado. Mismo espíritu que el guard de `read_file` de
        # arriba: redirigir al modelo de vuelta al contexto ya inyectado
        # en vez de dejar que propague el error crudo.
        _WEB_SEARCH_TOOL_ALIASES = {
            "web_search", "search_web", "websearch", "internet_search",
            "search_internet", "browse", "browse_web", "google_search",
            "search", "buscar_en_internet", "busqueda_web",
        }
        if (
            tool_name in _WEB_SEARCH_TOOL_ALIASES
            and isinstance(result, dict)
            and result.get("status") == "error"
        ):
            logger.warning("🛡️ [ToolGuard] Interceptada alucinación de tool-call web_search: %s", tool_name)
            return (
                "[INSTRUCCIÓN DEL SISTEMA]: No existe una herramienta de búsqueda web invocable — "
                "la información de internet relevante ya fue recuperada e inyectada en el prompt "
                "original de este turno. Procede inmediatamente a responder al usuario utilizando "
                "ÚNICAMENTE ese contexto ya disponible, sin invocar más herramientas ni afirmar que "
                "la búsqueda web no está disponible."
            )

        # Nota (medido - capturado con list_dir apuntando a
        # "/path/to/retrieved/web/context/files"): esta protección
        # existía SOLO para `read_file` y SOLO detectaba un resultado en
        # forma de dict (`{"status": "error", ...}`) - pero
        # `read_file_safely`/`_tool_list_dir` (tools.py) devuelven un
        # STRING plano ("[SANDBOX ERROR]: El archivo '...' no existe." /
        # "Error: La ruta '...' no existe.") cuando el path no existe;
        # solo pasan por la forma dict si `safe_tool_execution` atrapa
        # una excepción que se les escape, algo que ninguna de las dos
        # deja pasar (ambas atrapan sus propios errores internamente).
        # En la práctica, el guard nunca disparaba para el caso real que
        # dice proteger. Generalizado para: (a) aceptar resultado string
        # O dict, (b) cubrir `list_dir` además de `read_file` - el mismo
        # patrón de alucinación (una ruta con pinta de "acá vive el
        # contexto web ya recuperado") no se limita a un solo tool.
        _HALLUCINATED_CONTEXT_PATH_HINTS = (
            "web_context", "web/context", "context_web", "web_results",
            "search_results", "retrieved/web", "retrieved\\web",
        )
        if tool_name in ("read_file", "write_file", "list_dir"):
            path_val = str(
                params.get("path") or params.get("directory") or params.get("folder") or ""
            ).lower().replace("\\", "/")
            result_text = str(
                result.get("message", "") if isinstance(result, dict) else result
            ).lower()
            failed = (
                (isinstance(result, dict) and result.get("status") == "error")
                or "no existe" in result_text
            )
            looks_hallucinated = any(
                hint.replace("\\", "/") in path_val or hint.replace("\\", "/") in result_text
                for hint in _HALLUCINATED_CONTEXT_PATH_HINTS
            )
            if failed and looks_hallucinated:
                logger.warning(
                    "🛡️ [ToolGuard] Abortada ejecución de %s sobre ruta alucinada: %s",
                    tool_name, path_val,
                )
                return (
                    "[INSTRUCCIÓN DEL SISTEMA]: La ruta especificada no existe porque la información "
                    "web ya fue recuperada e inyectada en el prompt anterior — no vive en el sistema "
                    "de archivos local. Procede inmediatamente a responder al usuario utilizando el "
                    "contexto ya disponible, sin invocar más herramientas de archivos."
                )

        # Sanitizar tumba internos para evitar desbordamiento del bloque Markdown en la UI
        safe_result = str(result).replace("```", "`\u200b``")

        # Nota (bucles de herramientas / saturaci\u00f3n de contexto): cap
        # con aviso EXPL\u00cdCITO de cu\u00e1nto se cort\u00f3 \u2014 nunca silencioso, para
        # que el modelo no asuma que vio el resultado completo. tools.py
        # ya acota internamente lo que puede (ver MAX_TOOL_OUTPUT_CHARS),
        # pero este es el punto de paso \u00daNICO de todo resultado de
        # herramienta hacia el prompt (incluido el motor din\u00e1mico), as\u00ed
        # que es la garant\u00eda real de que nada sin acotar llega hasta aqu\u00ed.
        if len(safe_result) > self.MAX_TOOL_RESULT_CHARS_IN_PROMPT:
            safe_result = (
                safe_result[: self.MAX_TOOL_RESULT_CHARS_IN_PROMPT]
                + f"\n\n[...TRUNCADO: la salida de '{tool_name}' ten\u00eda {len(safe_result)} "
                f"caracteres, se muestran los primeros {self.MAX_TOOL_RESULT_CHARS_IN_PROMPT}...]"
            )

        return (
            f"**[RESULTADO DE HERRAMIENTA (`{tool_name}`)]**\n"
            f"```text\n{safe_result}\n```\n"
        )

    #: Prefijos EXACTOS y exhaustivos con los que `execute_tool_from_call`
    #: (arriba) abre cualquiera de sus 3 redirecciones internas de
    #: ToolGuard (ver sus `return "[AVISO DEL SISTEMA]..."` / `return
    #: "[INSTRUCCIÓN DEL SISTEMA]..."`) - texto pensado para que el propio
    #: modelo lo lea y corrija el rumbo, nunca para que lo vea el usuario.
    #: Si se agrega un guard nuevo ahí con un prefijo distinto, hay que
    #: sumarlo también acá.
    _TOOLGUARD_NOTICE_PREFIXES: Tuple[str, str] = (
        "[AVISO DEL SISTEMA]", "[INSTRUCCIÓN DEL SISTEMA]",
    )

    @classmethod
    def _is_internal_toolguard_notice(cls, tool_result: Any) -> bool:
        """
        BLINDAJE (bug real, MEDIDO — turno "quién ganó la final de la
        Champions League [año]", capturas de pantalla adjuntas):
        `execute_tool_from_call` alucinó un tool-call de búsqueda web
        inexistente (el modelo generó JSON para una herramienta no
        registrada) y su ToolGuard devolvió, correctamente, una de las
        redirecciones internas de arriba. Pero en `run_turn`,
        `raw_response` se armaba SIEMPRE como
        `f"{tool_result}\\n\\n{explanation}"`, sin distinguir ese caso —
        la redirección interna quedaba concatenada delante de la
        respuesta y terminaba VISIBLE en pantalla ("[INSTRUCCIÓN DEL
        SISTEMA]: No existe una herramienta..."). Factorizado a un
        método aparte (en vez de un chequeo inline en `run_turn`) para
        que test_regressions.py pueda verificarlo directamente contra
        el código real, no contra una copia reescrita a mano que podría
        divergir en silencio.
        """
        return str(tool_result).lstrip().startswith(cls._TOOLGUARD_NOTICE_PREFIXES)

    @staticmethod
    def _build_toolcall_followup_context(web_success: bool, web_context_str: str) -> str:
        """
        BLINDAJE (mismo bug, segunda mitad — MEDIDO comparando contra la
        respuesta real: Man City 1-0 Inter, Estambul, 10 de junio de
        2023): incluso cuando el modelo obedecía la redirección de
        `_is_internal_toolguard_notice` ("responde usando el contexto ya
        disponible"), el prompt `followup` de la Pasada 2 del
        tool-calling nunca le MOSTRABA ese contexto — solo la orden de
        usarlo, sin los datos reales delante. Resultado medido: el
        modelo inventó de cero "Real Madrid 3-1 Liverpool en París" en
        vez de la respuesta real, que SÍ estaba en `web_context_str`
        para ese turno — simplemente nunca se la pasamos a esta segunda
        pasada. `web_context_str` ya pasó por `_trim_context_to_budget`
        más arriba en el mismo turno, así que incluirla acá entera, sin
        recortarla de nuevo, es seguro en tamaño. Devuelve cadena vacía
        cuando no hay contexto real que ofrecer (`web_success` False o
        `web_context_str` vacío) — nunca un bloque vacío que solo
        agregaría ruido al prompt.
        """
        if not (web_success and web_context_str):
            return ""
        return (
            "\n\nContexto recuperado para este turno (única fuente de "
            f"hechos permitida, no inventes nada fuera de esto):\n"
            f"{web_context_str}\n"
        )

    def _validate_and_fix_python_code(
        self,
        response_text: str,
        target_model: str,
        max_retries: Optional[int] = None,
    ) -> Tuple[str, int]:

        current_text = response_text
        retries = max_retries or self.MAX_SYNTAX_REPAIR_ATTEMPTS
        repairs_applied = 0

        for attempt in range(1, retries + 1):
            code_blocks = _CODE_FENCE_RE.findall(current_text)
            detected_error: Optional[str] = None
            failing_code: Optional[str] = None

            for code in code_blocks:
                try:
                    tree = ast.parse(code)
                except SyntaxError as exc:
                    failing_line_text = exc.text.strip() if exc.text else ""
                    detected_error = (
                        f"SyntaxError en línea {exc.lineno}, columna {exc.offset}: {exc.msg} "
                        f"-> '{failing_line_text}'"
                    )
                    failing_code = code
                    break

                validator = ScopeValidator()
                validator.visit(tree)
                if validator.undefined_errors:
                    detected_error = "Error de alcance (NameError):\n" + "\n".join(validator.undefined_errors[:3])
                    failing_code = code
                    break

            if not detected_error:
                return current_text, repairs_applied

            logger.warning(
                "Error estático detectado en código generado (intento %d/%d): %s",
                attempt,
                retries,
                detected_error,
            )

            fix_prompt = (
                "[MODO DESARROLLADOR ACTIVO — AUTO-CORRECCIÓN ESTÁTICA]:\n"
                "El código Python generado contiene errores que impedirán su ejecución:\n"
                f"{detected_error}\n\n"
                f"Código con error:\n```python\n{failing_code}\n```\n\n"
                "REGLAS OBLIGATORIAS:\n"
                "1. Asegúrate de declarar todas las variables antes de usarlas.\n"
                "2. Si usas módulos como 'random', 'sys' o 'os', inclúyelos en las importaciones iniciales.\n"
                "3. Devuelve únicamente el código corregido dentro de un bloque ```python ... ```."
            )

            corrected = self._call_llm(fix_prompt, target_model=target_model)

            if corrected and not corrected.lstrip().startswith("[ERROR"):
                current_text = corrected
                repairs_applied += 1
            else:
                break

        return current_text, repairs_applied

    def _get_base_system_prompt(self, lang: str = "Spanish") -> str:
        is_en = (lang == "English")
        # Ver la nota junto a `HISTORY_ENTRY_CHAR_CAP` para la
        # medición completa: este JSON con `indent=2` es ~444 de los
        # ~2706 tokens de esta cabecera (16%), solo por el formato
        # bonito - compactarlo no cambia una sola herramienta ni
        # parámetro, el modelo lee la estructura, no la indentación.
        tools_schema_json = json.dumps(TOOLS_SCHEMA, separators=(",", ":"), ensure_ascii=False)

        if is_en:
            return (
                "[SYSTEM ROLE: SOVNODE v2.0]\n"
                "You are SovNode, a sovereign, independent, private local AI assistant running "
                "on the user's own hardware via Ollama. Describe yourself only as this "
                "autonomous local system. Reply with precision, epistemic honesty, and correct "
                "spelling, in the language the critical language rule at the end requires.\n\n"
                "=================================================================\n"
                "PREMISE VERIFICATION — PRIORITY #1\n"
                "=================================================================\n"
                "Before anything else, check whether the user's message takes some fact for "
                "granted as already true — a transfer, a result, a date, a causal link — e.g. "
                "\"Given that Cucurella signed for Real Madrid, what shirt number does he "
                "wear?\". If the retrieved web context CONTRADICTS that premise, or never "
                "confirms it (and you cannot confirm it yourself with certainty), your answer "
                "MUST open by correcting or qualifying that premise — do not just answer the "
                "rest of the question assuming it is true.\n"
                "FORBIDDEN: going along with a false premise and only reporting that "
                "'information is missing' on a secondary detail (e.g. only \"I couldn't find "
                "the shirt number\") — that leaves the user believing the false premise is "
                "true. Correct the premise FIRST, then address the rest if it still applies.\n\n"
                "=================================================================\n"
                "WEB SEARCH AND VERIFICATION DIRECTIVES\n"
                "=================================================================\n"
                "1. A `[Real-time retrieved web context]` block is updated information — it takes priority over prior knowledge.\n"
                "2. NEVER claim you 'lack internet access' if evidence is provided in context.\n"
                "3. If the retrieved context is insufficient, just state the search results were inconclusive.\n"
                "4. Year/topic mismatch: if sources cover a different year or topic, say plainly what they actually cover instead of blending it with what was requested.\n\n"
                "=================================================================\n"
                "MANDATORY REASONING PROTOCOL (<thought>)\n"
                "=================================================================\n"
                "Before any response or tool call, always include a mandatory thinking block in "
                "the exact format below. This is your own internal plan — the user never sees "
                "it — so think through the problem for real, not as a formality:\n\n"
                "<thought>\n"
                "1. Analyze the request: what is really being asked, and does it take a factual "
                "premise for granted that needs checking first (PRIORITY #1 above)?\n"
                "2. Evaluate tools: does this need a local tool (read_file, list_dir, "
                "system_telemetry, run_cmd), or a plain-text answer?\n"
                "3. Per-source check: if web context was provided, paraphrase in ONE sentence per "
                "source (by citation number [1], [2]...) what THAT source actually says — a "
                "comprehension check, not the answer. This catches small models blurring several "
                "sources into one averaged impression. Skip if no web context this turn.\n"
                "4. For a text answer: list the 2-5 concrete points to cover, in order, with the "
                "evidence (web context, history, own knowledge) backing each — this keeps answers "
                "focused and complete.\n"
                "5. Hierarchical self-check, right here, before writing:\n"
                "   Level 1 — Structural: every point from step 4 has a concrete source (web "
                "context, history, lesson, own knowledge) and is distinct.\n"
                "   Level 2 — Factual/logical: does any number, name, date, or conclusion "
                "conflict with the given context? does the web context actually cover the "
                "specific year/event/entity asked, not just something similar? does the "
                "conclusion really follow? Fix the plan here — only the corrected version "
                "reaches the visible response.\n"
                "6. If a tool call is the right move: define the exact JSON parameters here.\n"
                "</thought>\n\n"
                "MANDATORY BLOCK FORMAT (non-negotiable):\n"
                "- Output must START literally with `<thought>` — no greeting, title, or preamble.\n"
                "- ALWAYS close `</thought>` before the visible answer's first word.\n"
                "- Steps 1-6 live ONLY inside the tags — reproducing them outside (headings like "
                "\"Analysis of the request\", \"Response plan\", \"Level 1/Level 2\") is FORBIDDEN.\n"
                "- After `</thought>`, write ONLY the final answer in flowing prose, as if the "
                "user never saw a plan — do not number your steps or narrate your process.\n\n"
                "=================================================================\n"
                "VERIFIER SCRATCHPAD (<thought_code>) — LIVE VERIFICATION\n"
                "=================================================================\n"
                "Small models often err at calculating, counting, or verifying a numeric/logical "
                "claim from memory alone. When your <thought> shows the answer depends on one, "
                "write an extra block right after </thought>:\n\n"
                "<thought_code>\n"
                "# Self-contained, deterministic Python code. Use print() to show\n"
                "# exactly the value you need to verify before answering.\n"
                "</thought_code>\n\n"
                "This runs for real in an isolated sandbox (not a simulation); its stdout comes "
                "back to you as [REAL-TIME SANDBOX VERIFICATION] before your final answer — "
                "treat it as ground truth whenever there's reasonable doubt. Reserve it for cases "
                "that truly need it, and keep it to pure computation — the sandbox blocks "
                "network, file, and system access regardless.\n\n"
                "Available local tools:\n\n"
                f"{tools_schema_json}\n\n"
                f"{_FINAL_ANSWER_STYLE_EN}"
                "=================================================================\n"
                "TOOL EXECUTION RULES (FUNCTION CALLING)\n"
                "=================================================================\n"
                "When <thought> determines a tool call is right, emit ONLY the corresponding "
                "JSON right after </thought>, inside ```json ... ``` fences, as the complete "
                "output.\n\n"
                "Example:\n"
                "<thought>\n"
                "The user wants system telemetry. 'system_telemetry' needs no parameters.\n"
                "</thought>\n"
                "```json\n"
                '{"tool": "system_telemetry", "parameters": {}}\n'
                "```\n\n"
                "For a normal query, write the answer in natural language right after "
                "</thought>, following the plan from steps 4-5 — present it complete, already "
                "organized.\n\n"
                # Nota (medido): este bloque tenía una sección
                # "TOOL EXECUTION RULES (FUNCTION CALLING)" DUPLICADA pegada
                # a continuación - mismo encabezado repetido, indentación
                # rota (concatenación implícita de strings adyacentes de
                # Python, sin '+', así que no tiraba error pero sí generaba
                # un solo string final con el duplicado adentro), y su
                # punto "2." cortado a mitad de frase ("...the right
                # move..."). Costaba tokens de prefill en cada turno en
                # inglés sin aportar nada nuevo - el único contenido con
                # valor real (la prohibición de leer el contexto web ya
                # inyectado como si fuera un archivo) se conserva abajo,
                # fusionado limpio en vez de como sección repetida.
                "CONTEXT FILE READ PROHIBITION: the [Real-time retrieved web context] block is "
                "ALREADY injected here. You are FORBIDDEN from invoking 'read_file' or any tool "
                "to read that context or phantom files (like 'web_context_...txt'). Answer "
                "directly using the injected text."
            )

        return (
            "[ROL DEL SISTEMA: SOVNODE v2.0]\n"
            "Eres SovNode, un asistente de IA local soberano, independiente y privado, que "
            "corre en el hardware del usuario vía Ollama. NUNCA menciones a OpenAI, Anthropic "
            "ni a ninguna otra empresa externa; sos un sistema autónomo e independiente. "
            "Responde con precisión, honestidad epistémica y corrección ortográfica, en el "
            "idioma que exige la regla de idioma crítica al final de este prompt.\n\n"
            "=================================================================\n"
            "VERIFICACIÓN DE PREMISAS — PRIORIDAD #1\n"
            "=================================================================\n"
            "Antes que nada, comprobá si el mensaje del usuario da por sentado un hecho como "
            "ya cierto — una transferencia, un resultado, una fecha, una relación causal — "
            "p. ej.: \"Sabiendo que Cucurella fichó por el Real Madrid, ¿qué dorsal lleva?\". "
            "Si el contexto web recuperado CONTRADICE esa premisa, o nunca la confirma (y no "
            "podés confirmarla vos con certeza), tu respuesta DEBE empezar corrigiendo o "
            "matizando esa premisa — no respondas el resto asumiendo que es cierta.\n"
            "PROHIBIDO: seguirle la corriente a una premisa falsa y solo reportar que \"falta "
            "información\" sobre un detalle secundario (p. ej. solo \"no encontré el "
            "dorsal\") — eso deja al usuario creyendo que la premisa falsa es cierta. Corregí "
            "la premisa PRIMERO; después, si aún aplica, abordá el resto.\n\n"
            "=================================================================\n"
            "DIRECTIVAS DE BÚSQUEDA Y VERIFICACIÓN WEB\n"
            "=================================================================\n"
            "1. Un bloque `[Contexto web recuperado]` es información actualizada — tiene prioridad sobre tu conocimiento previo.\n"
            "2. NUNCA afirmes que \"no tenés acceso a internet\" si hay evidencia inyectada en el contexto.\n"
            "3. Si el contexto recuperado es insuficiente, indicá simplemente que los resultados no son concluyentes.\n"
            "4. Desajuste de año/tema: si las fuentes tratan otro año o tema, decí claramente cuál cubren en vez de mezclarlo con lo pedido.\n\n"
            "=================================================================\n"
            "PROTOCOLO OBLIGATORIO DE RAZONAMIENTO (<thought>)\n"
            "=================================================================\n"
            "Antes de responder o llamar a una herramienta, DEBES incluir un bloque de "
            "pensamiento obligatorio en este formato exacto. Es tu borrador interno — el "
            "usuario NUNCA lo ve — así que planeá de verdad, no como trámite:\n\n"
            "<thought>\n"
            "1. Analizar la petición: ¿qué se pregunta realmente, y da por sentada alguna "
            "premisa fáctica que verificar primero (PRIORIDAD #1 arriba)?\n"
            "2. Evaluar herramientas: ¿hace falta una herramienta local (read_file, list_dir, "
            "system_telemetry, run_cmd), o alcanza con texto plano?\n"
            "3. Checklist por fuente: si hay contexto web, parafraseá en UNA frase por fuente "
            "(por número de cita [1], [2]...) qué dice ESA fuente — verificación de "
            "comprensión, no la respuesta. Esto atrapa a los modelos chicos difuminando varias "
            "fuentes en una impresión promediada. Omití este paso si no hay contexto web.\n"
            "4. Para una respuesta en texto: enumerá los 2-5 puntos a cubrir, en orden, con la "
            "evidencia (contexto web, historial, conocimiento propio) de cada uno — esto evita "
            "respuestas dispersas o incompletas.\n"
            "5. Auto-chequeo jerárquico, acá mismo, antes de escribir:\n"
            "   Nivel 1 — Estructural: cada punto del paso 4 tiene una fuente concreta "
            "(contexto web, historial, lección, conocimiento propio) y es distinto.\n"
            "   Nivel 2 — Factual/lógico: ¿algún número, nombre, fecha o conclusión "
            "CONTRADICE el contexto? ¿el contexto web cubre de verdad el año/evento/entidad "
            "preguntado, o es algo parecido de otro año? ¿la conclusión se sigue de verdad? "
            "Corregí el plan acá — solo la versión corregida llega a la respuesta visible.\n"
            "6. Si corresponde una herramienta: definí acá los parámetros exactos del JSON.\n"
            "</thought>\n\n"
            "=================================================================\n"
            "SCRATCHPAD VERIFICADOR (<thought_code>) — VERIFICACIÓN EN CALIENTE\n"
            "=================================================================\n"
            "Los modelos chicos se equivocan fácil al calcular, contar o verificar una "
            "afirmación numérica o lógica 'de memoria'. Si tu <thought> muestra que la "
            "respuesta depende de eso, escribí un bloque extra justo después de </thought>:\n\n"
            "<thought_code>\n"
            "# Código Python autocontenido y determinista. Usa print() para mostrar\n"
            "# exactamente el valor que necesitas verificar antes de responder.\n"
            "</thought_code>\n\n"
            "Esto corre de verdad en un sandbox aislado (no es simulación); su salida vuelve "
            "como [VERIFICACIÓN EN TIEMPO REAL DEL SANDBOX] antes de tu respuesta final — "
            "tratala como la verdad ante cualquier duda razonable. Reservala para casos que de "
            "verdad la necesiten, y usala solo para cálculo puro — el sandbox bloquea red, "
            "archivos y sistema de todos modos.\n\n"
            "Herramientas locales disponibles:\n\n"
            f"{tools_schema_json}\n\n"
            f"{_FINAL_ANSWER_STYLE_ES}"
            "=================================================================\n"
            "REGLAS DE EJECUCIÓN DE HERRAMIENTAS (FUNCTION CALLING)\n"
            "=================================================================\n"
            "Si tu <thought> determina que corresponde invocar una herramienta, emití SOLO el "
            "JSON correspondiente justo después de </thought>, dentro de marcas ```json ... "
            "```, como salida completa.\n\n"
            "Ejemplo:\n"
            "<thought>\n"
            "El usuario quiere ver telemetría del sistema. 'system_telemetry' no requiere "
            "parámetros.\n"
            "</thought>\n"
            "```json\n"
            '{"tool": "system_telemetry", "parameters": {}}\n'
            "```\n\n"
            "Para una consulta normal, escribí la respuesta en lenguaje natural justo después "
            "de </thought>, siguiendo el plan del paso 4-5 — ya completa y organizada, sin "
            "repetirlo ni resumirlo."
        )

    def set_language(self, lang: str) -> None:
        """
        Actualiza el idioma de SESIÓN del orquestador (el que usan los
        turnos cuando el prompt actual no da ninguna pista de idioma
        propia — ver `_resolve_turn_language`), manteniendo intacta
        toda la estructura del prompt.

        No hace falta invalidar `_frozen_system_headers` aquí: desde
        que ese caché se indexa por (idioma, is_coder) — ver
        `_get_frozen_header` — en vez de solo por `is_coder` como
        antes, cada entrada es content-addressed (`_get_base_system_
        prompt(lang)` es una función pura del idioma) y por lo tanto
        siempre válida sin importar cuándo se llamó a `set_language()`.
        Antes SÍ hacía falta: `_frozen_system_headers` era un dict fijo
        de 2 entradas (una por rol) construido una única vez en
        `__init__`, así que un cambio de idioma posterior desde la UI
        (ver sovnode_qt.py, llamadas a `set_language()` en el selector
        de idioma) actualizaba `self.SYSTEM_PROMPT` pero `_call_llm()`
        seguía usando la cabecera congelada del idioma ANTERIOR
        indefinidamente.
        """
        self.current_language = lang
        self.SYSTEM_PROMPT = self._get_base_system_prompt(lang)
        logger.info("Idioma del orquestador actualizado a: %s", lang)

    def _get_frozen_header(self, lang: str, is_coder: bool) -> str:
        """
        Cabecera "system" para Ollama, cacheada por (idioma, is_coder)
        — no solo por SESIÓN como antes (ver docstring de
        `set_language`). Esto es lo que permite que UN turno pida el
        idioma detectado en SU PROPIO prompt (ver
        `_resolve_turn_language`) sin depender de qué idioma tenga
        seleccionado el selector de la UI en ese momento, mientras
        conserva la Optimización #1 (Prefix Alignment/KV-Cache): dentro
        de un mismo turno, todas las llamadas a `_call_llm()` piden la
        MISMA clave (mismo idioma efectivo, mismo rol), así que siguen
        recibiendo el mismo objeto `str` ya construido — el prefijo
        "system" enviado a Ollama sigue siendo byte-idéntico entre
        llamadas sucesivas del mismo turno, que es la condición real
        que esa optimización necesita (nunca dependió de que el
        idioma fuera fijo para TODO el proceso).

        El texto del CODER no varía por idioma (CODER_SYSTEM_PROMPT es
        un único bloque en español; el mirroring de sus respuestas lo
        hace LANG_ENFORCE_DIRECTIVE solo) — se cachea igual por
        (idioma, True) por simplicidad de tener una única función de
        acceso, al costo aceptable de guardar el mismo texto bajo dos
        claves como mucho.
        """
        key = (lang, is_coder)
        cached = self._frozen_system_headers.get(key)
        if cached is not None:
            return cached
        header = (
            self.CODER_SYSTEM_PROMPT if is_coder else self._get_base_system_prompt(lang)
        ) + self.LANG_ENFORCE_DIRECTIVE
        self._frozen_system_headers[key] = header
        return header

    def _get_trivial_system_prompt(self, lang: str) -> str:
        """
        Cabecera "system" MÍNIMA para el carril `TRIVIAL_GREETING` de
        `run_turn` (ver `_TRIVIAL_SYSTEM_PROMPT_ES/EN`). Cacheada en el
        mismo dict que `_get_frozen_header`, bajo una clave que no puede
        colisionar con las `(idioma, bool)` de ese método
        (`("__trivial__", lang)`), así que el saludo y un turno normal
        del mismo idioma conservan cada uno su prefijo "system" estable
        entre llamadas (Optimización #1).
        """
        key = ("__trivial__", lang)
        cached = self._frozen_system_headers.get(key)
        if cached is not None:
            return cached
        header = (
            _TRIVIAL_SYSTEM_PROMPT_EN if lang == "English" else _TRIVIAL_SYSTEM_PROMPT_ES
        )
        self._frozen_system_headers[key] = header
        return header

    def _build_trivial_prompt(self, user_input: str, lang: str) -> str:
        """
        Prompt de usuario mínimo para el carril `TRIVIAL_GREETING`: el
        mensaje del usuario y el ancla de arranque, nada más (ni
        contexto conversacional, ni RAG, ni la cola de estilo/idioma de
        `_build_reasoning_prompt`). Mantiene el mismo ancla final
        ("Respuesta:" / "Answer:") que el resto del pipeline, por lo que
        `_ANSWER_RESTART_STOP_SEQUENCES` sigue sirviendo como corte.
        """
        if lang == "English":
            return f"User: {user_input}\n\nAnswer:"
        return f"Usuario: {user_input}\n\nRespuesta:"

    def _get_fastpath_system_prompt(self, lang: str) -> str:
        """
        Cabecera "system" LIGERA para TODO el carril fast_path (no solo
        saludos). El `_get_base_system_prompt` general (~2700 tok) mete el
        protocolo `<thought>` de 6 pasos, el protocolo `<thought_code>`
        (fuente literal del `[REAL-TIME SANDBOX VERIFICATION]` que
        phi3.5:3.8b repitió en el video del 2026-08-27) y
        `_FINAL_ANSWER_STYLE` — todo eso, en un modelo de 3.8B con una
        consulta simple, es material para alucinar en vez de una guía. El
        router YA decidió que este turno es simple; este prompt le da al
        modelo lo mínimo para responder bien y llamar herramientas, sin
        el andamiaje de razonamiento que descarrila.

        Conserva: identidad, directivas web (fast_path puede traer
        `web_context` vía `force_web_search`), verificación de premisa en
        una línea, el schema de herramientas + reglas de function-calling
        compactas (la salida pasa por `extract_tool_call`), estilo breve
        y regla de idioma. Cacheada en `_frozen_system_headers` bajo
        `("__fastpath__", lang)` — mismo patrón que `_get_frozen_header`
        / `_get_trivial_system_prompt`, sin colisión de clave.

        El slow_path y la rama coder NO usan esto: slow_path necesita el
        `<thought>` completo para el modo de dos pasadas, y coder ya usa
        `CODER_SYSTEM_PROMPT` (más chico, sin el protocolo de 6 pasos).
        """
        key = ("__fastpath__", lang)
        cached = self._frozen_system_headers.get(key)
        if cached is not None:
            return cached

        tools_schema_json = json.dumps(TOOLS_SCHEMA, separators=(",", ":"), ensure_ascii=False)
        is_en = (lang == "English")

        if is_en:
            header = (
                "[SYSTEM ROLE: SOVNODE v2.0]\n"
                "You are SovNode, a sovereign, private, local AI assistant running on "
                "the user's own hardware via Ollama. Never mention OpenAI, Anthropic, "
                "or any external company; you are an autonomous, independent system. "
                "Answer with precision and epistemic honesty.\n\n"
                "PREMISE CHECK: if the user's message assumes a fact as already true "
                "and you cannot confirm it (or the web context contradicts it), open "
                "your answer by correcting that premise before anything else.\n\n"
                "WEB CONTEXT: a `[Real-time retrieved web context]` block, when "
                "present, is updated information and takes priority over prior "
                "knowledge. Never claim you 'have no internet access' if such a block "
                "is present. If it is insufficient, just say the results were "
                "inconclusive. If it covers a different year/topic, say so plainly.\n\n"
                "LOCAL TOOLS: if — and only if — the request needs a local tool, emit "
                "ONLY the JSON call, nothing else, inside a ```json ... ``` fence:\n"
                '```json\n{"tool": "system_telemetry", "parameters": {}}\n```\n'
                "Otherwise just answer in natural language. Available tools:\n"
                f"{tools_schema_json}\n\n"
                "STYLE: answer directly and only as long as the question needs — a "
                "greeting gets one sentence, a simple question a few sentences. Lead "
                "with the answer, no preamble, no \"as an AI assistant\". State only "
                "what you are sure of; never invent a fact, a name, a formula, or a "
                "connection between concepts to pad the answer. Do not output any "
                "reasoning block, angle-bracket or square-bracket protocol tag, "
                "section separator, or plan — write only the final answer. Do not "
                "append example questions, \"for deeper understanding\" notes, or "
                "any follow-up material the user did not ask for; stop when the "
                "answer is done.\n\n"
                "MATH: write every mathematical expression as inline LaTeX between "
                "single dollar signs so it renders — e.g. $a^2 + b^2 = c^2$, "
                "$e^{i\\pi} + 1 = 0$, $x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}$. "
                "Never leave a formula as plain text with unicode symbols.\n\n"
                "LANGUAGE: reply in English, matching the user's message, unless they "
                "explicitly ask otherwise."
            )
        else:
            header = (
                "[ROL DEL SISTEMA: SOVNODE v2.0]\n"
                "Sos SovNode, un asistente de IA local, soberano y privado, que corre "
                "en el hardware del usuario vía Ollama. Nunca menciones a OpenAI, "
                "Anthropic ni a ninguna empresa externa; sos un sistema autónomo e "
                "independiente. Respondé con precisión y honestidad epistémica.\n\n"
                "CHEQUEO DE PREMISA: si el mensaje del usuario da por sentado un hecho "
                "como cierto y no podés confirmarlo (o el contexto web lo contradice), "
                "abrí tu respuesta corrigiendo esa premisa antes que nada.\n\n"
                "CONTEXTO WEB: un bloque `[Contexto web recuperado]`, cuando está "
                "presente, es información actualizada y tiene prioridad sobre tu "
                "conocimiento previo. Nunca digas que \"no tenés acceso a internet\" si "
                "hay un bloque así. Si es insuficiente, decí simplemente que los "
                "resultados no son concluyentes. Si cubre otro año o tema, aclaralo.\n\n"
                "HERRAMIENTAS LOCALES: si —y solo si— la petición necesita una "
                "herramienta local, emití SOLO el JSON de la llamada, nada más, dentro "
                "de marcas ```json ... ```:\n"
                '```json\n{"tool": "system_telemetry", "parameters": {}}\n```\n'
                "En cualquier otro caso, respondé en lenguaje natural. Herramientas "
                "disponibles:\n"
                f"{tools_schema_json}\n\n"
                "ESTILO: respondé directo y solo con la extensión que pida la "
                "pregunta — un saludo se contesta en una frase; una pregunta simple "
                "en pocas frases. Arrancá por la respuesta, sin preámbulo, sin \"como "
                "asistente de IA\". Afirmá solo lo que sabés con certeza; nunca "
                "inventes un dato, un nombre, una fórmula ni una conexión entre "
                "conceptos para rellenar. No emitas ningún bloque de razonamiento, "
                "etiqueta de protocolo entre paréntesis angulares o corchetes, "
                "separador de sección ni plan — escribí solo la respuesta final. No "
                "agregues preguntas de ejemplo, notas de \"para profundizar\" ni "
                "material de seguimiento que el usuario no pidió; terminá cuando la "
                "respuesta esté completa.\n\n"
                "MATEMÁTICA: escribí toda expresión matemática como LaTeX inline "
                "entre signos de dólar para que se renderice — p. ej. $a^2 + b^2 = "
                "c^2$, $e^{i\\pi} + 1 = 0$, $x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}"
                "{2a}$. Nunca dejes una fórmula como texto plano con símbolos "
                "unicode.\n\n"
                "IDIOMA: respondé en español, igualando el mensaje del usuario, salvo "
                "que pida explícitamente otra cosa."
            )

        self._frozen_system_headers[key] = header
        return header

    def _fastpath_answer_tail(self, lang: Optional[str] = None) -> str:
        """
        Cola de instrucción para `_build_reasoning_prompt(lean=True)` — la
        versión LIGERA de `_final_answer_instruction_tail`, sin el bloque
        `[CALIBRACIÓN]` que contiene ejemplos de ecuaciones de física
        (semilla de alucinación medida con phi3.5:3.8b) y sin el
        recordatorio de `<thought>` (el prompt ligero de fast_path no
        define ese protocolo). Solo estilo breve + regla de idioma, en la
        posición de mayor peso del prompt (justo antes del ancla).
        """
        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"
        turn_language = "English" if is_en else "Spanish"
        if is_en:
            return (
                "[ANSWER NOW]: reply directly, only as long as the question needs, "
                "in flowing prose. Do not invent facts or connections; if unsure, say "
                "so or leave it out. Do not emit any plan, protocol tag, or section "
                "separator, and do not append example questions or extra follow-up "
                "material — stop when the answer is done.\n"
                "[MATH]: every formula as inline LaTeX between single $ signs "
                "(e.g. $E = mc^2$), never plain unicode text.\n"
                f"[LANGUAGE]: write your answer in {turn_language}, matching the "
                "user's message above.\n\n"
            )
        return (
            "[RESPONDÉ AHORA]: contestá directo, solo con la extensión que pida la "
            "pregunta, en prosa corrida. No inventes datos ni conexiones; si no "
            "estás seguro, decilo u omitilo. No emitas ningún plan, etiqueta de "
            "protocolo ni separador de sección, y no agregues preguntas de ejemplo "
            "ni material de seguimiento extra — terminá cuando la respuesta esté "
            "completa.\n"
            "[MATEMÁTICA]: cada fórmula como LaTeX inline entre signos $ "
            "(p. ej. $E = mc^2$), nunca texto plano con unicode.\n"
            f"[IDIOMA]: escribí tu respuesta en {turn_language}, igualando el "
            "mensaje del usuario de arriba.\n\n"
        )

    def _resolve_turn_language(self, user_input: str) -> str:
        """
        Idioma EFECTIVO para este turno: el del prompt actual cuando
        `_detect_prompt_language()` (determinista, sin costo de
        inferencia) da una señal inequívoca, o el de sesión
        (`self.current_language`) cuando el prompt es ambiguo/corto y
        no da ninguna pista propia. Mismo criterio "el mensaje actual
        manda sobre el contexto/sesión" que ya usa la REGLA DE
        PRIORIDAD de `QUERY_REWRITE_SYSTEM_PROMPT` para años, y
        `_detect_query_language_confident` en sovnode_qt.py para
        decidir el idioma de la búsqueda web — aquí aplicado a la
        decisión de en qué idioma responde el modelo.
        """
        return _detect_prompt_language(user_input) or self.current_language

    def clear_conversation_memory(self) -> None:
        """
        Aísla por completo una "Nueva conversación" de cualquier sesión
        anterior dentro del mismo proceso — dos fuentes de contaminación
        distintas, ambas necesarias:

        1. `memory_graph.clear()` (MemoryGraph, sovnode_memory.db en
           disco): vacía `conversation_turns`/`turns_fts` (historial de
           turnos, ver `get_recent_history()`/`fetch_relevant_context()`)
           Y TAMBIÉN `web_knowledge`/`web_knowledge_fts` (caché de
           búsquedas web persistidas, ver `fetch_web_knowledge()` en
           `process_turn()`) y `reasoning_lessons*`. Antes este método
           reimplementaba un DELETE manual limitado solo a
           `conversation_turns`/`turns_fts` — sin darse cuenta de que
           MemoryGraph.clear() ya existía y cubre las demás tablas —
           así que una investigación web vieja seguía siendo reutilizable
           por `process_turn()` en una conversación "nueva".

        2. `vector_rag.reset()` (LocalVectorRAG, FAISS EN MEMORIA DE
           PROCESO, no en el archivo SQLite de arriba): sin esto, un
           hecho vectorizado en una sesión anterior (p. ej. un resultado
           de búsqueda web ya vencido o incorrecto, vectorizado por
           `_persist_web_knowledge()`) seguía siendo recuperable por
           `fetch_hybrid_context()` — usada tanto por `process_turn()`
           como por `StreamTurnWorker.run()` (sovnode_qt.py) en los
           turnos sin búsqueda web activa — en cualquier conversación
           "nueva" posterior, porque ningún DELETE sobre el archivo
           SQLite toca este índice en memoria.

        Por qué existe en general: "Nueva conversación" en la UI
        (`_clear_chat()`) solo borra los widgets del chat visible; sin
        este método, el historial/RAG de la sesión anterior —incluso de
        un reinicio previo de la app, porque MemoryGraph persiste en
        disco, no en RAM— seguía viviendo y contaminando el turno
        "nuevo".
        """
        try:
            self.memory_graph.clear()
            logger.info("Historial de conversación (MemoryGraph) vaciado — nueva sesión iniciada.")
        except Exception as exc:
            # Nunca debe impedir que el usuario inicie una conversación
            # nueva: en el peor caso, el historial viejo persiste una
            # vez más y se seguirá intentando limpiar en el próximo
            # intento.
            logger.warning("No se pudo vaciar MemoryGraph al iniciar nueva conversación: %s", exc)

        if hasattr(self, "vector_rag") and self.vector_rag is not None:
            try:
                self.vector_rag.reset()
                logger.info("Índice vectorial (vector_rag) reiniciado — nueva sesión iniciada.")
            except Exception as exc:
                logger.warning(
                    "No se pudo reiniciar el índice vectorial al iniciar nueva conversación: %s", exc
                )

    def _final_answer_instruction_tail(
        self, lang: Optional[str] = None, include_thought_reminder: bool = True,
        thin_context_reminder: bool = False,
    ) -> str:
        """
        Bloque de ESTILO + REGLA DE IDIOMA que instruye la respuesta
        VISIBLE final. Factorizado fuera de `_build_reasoning_prompt`
        para que lo comparta también `_call_llm_two_pass` (Pasada 2, ver
        más abajo) sin mantener dos copias del mismo texto — una
        divergencia entre copias es exactamente el tipo de bug que este
        refactor evita.

        `include_thought_reminder=False` omite el recordatorio de que la
        salida debe abrir con `<thought>` (ver el BLINDAJE documentado
        más abajo). Usalo SOLO para la Pasada 2 del modo de dos pasadas:
        ahí el `<thought>` YA se generó en la Pasada 1, y volver a
        pedirlo contradiría la instrucción explícita de esa pasada de
        "no emitas otro bloque <thought>" — confundiría al modelo con
        dos órdenes opuestas en el mismo prompt, el mismo problema de
        fondo que motivó mover la regla de idioma al final en primer
        lugar (ver más abajo).

        `thin_context_reminder=True` — BLINDAJE (bug real, MEDIDO,
        pedido explícito: "arregla esos problemitas" — turno "último
        terremoto en China", agosto 2026 con fuentes de enero 2025): el
        aviso completo de `build_thin_context_warning()` ("las fuentes
        no cubren lo específico consultado, abrí tu respuesta
        diciéndolo") vive al PRINCIPIO de `web_context_str`, a miles de
        tokens de distancia del punto donde el modelo genera. Con
        qwen2.5:3b eso no alcanza — MEDIDO: el modelo listó 3 terremotos
        con detalles concretos como si respondiera la pregunta, y
        recién en la última frase agregó "no puedo dar detalles sobre
        el último terremoto" — CONTRADICIÉNDOSE a sí mismo, en vez de
        abrir con esa aclaración como pedía el aviso. Mismo motivo, de
        nuevo, por el que la regla de idioma se movió al final: un
        recordatorio CORTO acá, en la posición de mayor peso del
        prompt, es lo que realmente se respeta. Además esto cubre la
        Pasada 2 del modo de dos pasadas, que NUNCA ve `web_context_str`
        en absoluto — solo el plan de la Pasada 1 (ver `_call_llm_two_
        pass`/pass2_prompt en sovnode_qt.py) — así que sin este
        parámetro, la Pasada 2 no tenía NINGUNA oportunidad de conocer
        el aviso salvo que el plan de la Pasada 1 ya lo hubiera
        capturado bien.
        """
        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"

        # Recordatorio de ESTILO en la penultima posicion del prompt: justo
        # antes de la regla de idioma, que conserva a proposito el ultimo
        # lugar (ver la nota del bug de mirroring de idioma mas abajo).
        #
        # Por que repetirlo aca si ya esta en la cabecera "system"
        # (_FINAL_ANSWER_STYLE_ES/EN): medido sobre qwen2.5:3b con la forma
        # REAL de este prompt, 5 semillas por configuracion, longitud media
        # de la respuesta VISIBLE al usuario:
        #     sin estilo (comportamiento previo) .... 726 caracteres
        #     solo estilo en la cabecera "system" ... 1039 caracteres
        #     cabecera + este recordatorio .......... 2209 caracteres
        # La cabecera sola mejora poco porque queda a ~2700 tokens de
        # distancia del punto de generacion; repetir la instruccion cerca
        # del final es lo que realmente pesa en un modelo de 3B - el mismo
        # motivo por el que la regla de idioma se movio al final.
        #
        # Tambien se probo colocarlo DESPUES de la regla de idioma: sin
        # diferencia medible (1818 vs 1795 caracteres), asi que se deja
        # antes, para no quitarle a la regla de idioma la ultima posicion.
        tail = (
            "[STYLE FOR THIS ANSWER]: develop each point of your plan into at "
            "least its own paragraph, explaining the why and giving context or "
            "concrete examples. Do not deliver only the conclusion.\n\n"
            if is_en else
            "[ESTILO DE ESTA RESPUESTA]: desarrolla cada punto de tu plan en al "
            "menos un párrafo propio, explicando el porqué y dando contexto o "
            "ejemplos concretos. No entregues solo la conclusión.\n\n"
        )
        # Nota (hipótesis razonada, NO medida en vivo - no hay forma de
        # correr el modelo real desde esta sesión; pedido explícito del
        # usuario tras confirmar el caso con un video real): turno real,
        # "the most important equation in physics" -> el modelo eligió la
        # Identidad de Euler (e^{iπ}+1=0, correcta) y después, forzado por
        # el [STYLE] de arriba a dar "contexto y ejemplos concretos" para
        # cada punto, afirmó como hecho que el intervalo espacio-temporal
        # de relatividad especial ds²=c²t²−x²−y²−z² "can be rewritten using
        # Euler's identity in the complex plane" - falso, dos conceptos
        # matemáticos sin relación real. La exigencia de desarrollar y
        # ejemplificar cada punto (necesaria, resuelve el bug de
        # respuestas demasiado cortas) no tiene contrapeso alguno que
        # distinga "ejemplo real" de "conexión inventada para cumplir la
        # cuota" - un modelo de 3B, sin ese contrapeso, puede preferir
        # rellenar con una afirmación falsa antes que dejar un punto sin
        # desarrollar. Este bloque agrega ese contrapeso, sin tocar ni
        # debilitar el [STYLE] de arriba: sigue exigido dar contexto y
        # ejemplos, pero ahora con la condición explícita de que sean
        # ciertos, o se marquen como analogía y no como hecho.
        tail += (
            "[CALIBRATION — DO NOT INVENT CONNECTIONS]: when developing a "
            "point above requires claiming that one concept relates to, can "
            "be rewritten as, or is a special case of another, only make "
            "that claim if you are sure it is mathematically or physically "
            "correct. If you are not sure, prefer to omit the connection "
            "entirely, or present it explicitly as a loose analogy "
            "('this resembles...') rather than stating it as fact ('this "
            "is...'). A shorter answer that stays correct beats a longer "
            "one that pads a point with an invented connection. Same care "
            "applies to citing a named equation or law: if you are not sure "
            "of its EXACT form, say so or prefer to omit it — never "
            "assemble a plausible-looking formula by combining fragments of "
            "two different real ones (e.g. taking one law's left-hand side "
            "and attaching another law's right-hand side).\n\n"
            if is_en else
            "[CALIBRACIÓN — NO INVENTES CONEXIONES]: cuando desarrollar un "
            "punto de arriba te lleve a afirmar que un concepto se "
            "relaciona con otro, se puede reescribir como otro, o es un "
            "caso particular de otro, hacé esa afirmación SOLO si estás "
            "seguro de que es matemática o físicamente correcta. Si no "
            "estás seguro, preferí omitir la conexión por completo, o "
            "presentarla explícitamente como una analogía suelta ('esto se "
            "parece a...') y no como un hecho ('esto es...'). Una respuesta "
            "más corta pero correcta es mejor que una más larga que rellena "
            "un punto con una conexión inventada. El mismo cuidado aplica a "
            "citar una ecuación o ley por su nombre: si no estás seguro de "
            "su forma EXACTA, decilo o preferí omitirla — nunca armes una "
            "fórmula que parezca plausible combinando fragmentos de dos "
            "fórmulas reales distintas (por ejemplo, el lado izquierdo de "
            "una ley pegado al lado derecho de otra).\n\n"
        )
        # Ver la nota de `thin_context_reminder` en el docstring: un
        # recordatorio CORTO, en esta posición de máximo peso, en vez de
        # depender únicamente del aviso completo (mucho más largo, y
        # enterrado al principio del prompt) que ya viajó dentro de
        # `web_context_str`. Deliberadamente no repite el texto completo
        # del aviso - el punto es que sea breve para no diluirse entre
        # el resto del bloque final.
        if thin_context_reminder:
            tail += (
                "[SOURCES ARE THIN — REMINDER]: the sources retrieved for this "
                "turn do NOT specifically cover what was asked. Your answer must "
                "OPEN by saying so plainly, and only then describe what the "
                "sources DO cover. Do not present those general details as if "
                "they had answered the specific question.\n\n"
                if is_en else
                "[FUENTES INSUFICIENTES — RECORDATORIO]: las fuentes recuperadas "
                "en este turno NO cubren específicamente lo que se preguntó. Tu "
                "respuesta debe ABRIR diciendo eso claramente, y recién después "
                "describir de qué sí hablan las fuentes. No presentes esos "
                "detalles generales como si hubieran respondido la pregunta "
                "específica.\n\n"
            )
        # Nota (medido): este bloque, en su version anterior
        # (sin la ultima oracion de abajo), suprimia la emision de
        # <thought> por COMPLETO en qwen2.5:3b - 0/9 en tres escenarios
        # distintos (factual, complejo, con contexto web), aun con el
        # SYSTEM_PROMPT exigiendolo como obligatorio unas lineas arriba.
        # Con 7b no pasaba (3/3 correcto) - es un fallo especifico de
        # modelos chicos: la ULTIMA instruccion que leen antes de generar
        # habla solo de "tu respuesta visible", asi que un 3B salta
        # directo a la respuesta sin pasar por el plan interno.
        #
        # Se probo primero "primear" el prompt terminando literalmente en
        # "<thought>" en vez de "Respuesta:" (forzar al modelo a
        # continuar desde dentro del bloque) - funciona para REABRIR el
        # bloque (3/3), pero sin un stop-sequence que lo acote, el modelo
        # completa los 6 pasos y despues SE DESCARRILA: emite una
        # etiqueta mal formada ("(thought_code)" en vez de
        # "<thought_code>") y alucina una llamada a herramienta
        # (system_telemetry) sin relacion con la pregunta, sin cerrar
        # </thought> ni escribir nunca una respuesta real. Descartado por
        # peligroso para una llamada sin stop-sequence.
        #
        # El fix real: un simple RECORDATORIO explicito, en el mismo
        # lugar de maximo peso (el final del prompt), de que el bloque
        # <thought> sigue siendo obligatorio. Probado 3/3: abre, cierra y
        # escribe una respuesta real, en una sola pasada, sin descarrilar
        # - y sin afectar el mirroring de idioma que este bloque corrige
        # (verificado en ambos idiomas).
        turn_language = "English" if is_en else "Spanish"
        reminder = ""
        if include_thought_reminder:
            reminder = (
                " Remember: your output must ALWAYS start with the mandatory "
                "<thought>...</thought> block defined above, before the visible response."
                if is_en else
                " Recuerda: tu salida debe empezar SIEMPRE con el bloque "
                "<thought>...</thought> obligatorio definido arriba, antes de la "
                "respuesta visible."
            )
        # BALANCE DE VELOCIDAD / TASA DE LangFix (medido - turno real: la
        # respuesta de FastSingle salió en el idioma incorrecto y disparó
        # LangFix, ~40s extra de los ~160s totales del turno). Esta regla,
        # la última instrucción que el modelo lee antes de generar (la
        # posición de mayor peso, según el resto de las notas de esta
        # función), estaba escrita siempre en inglés - incluso cuando
        # `turn_language` == "Spanish", el texto que la RODEA (su forma de
        # superficie) seguía en inglés, aunque su CONTENIDO pidiera
        # responder en español. Es la misma clase de señal contradictoria
        # que ya causó problemas documentados en esta función (ver el
        # Nota de <thought> más arriba: un modelo de 3B pesa mucho la
        # forma de lo último que lee, no solo su significado). No es un
        # refuerzo nuevo que compita con otra instrucción - es hacer que
        # la misma regla ya existente hable en el idioma que pide, como ya
        # hace el bloque [STYLE] un poco más arriba en esta misma función.
        # Hipótesis razonada a partir de evidencia ya documentada en este
        # archivo, no verificada en vivo (no hay forma de correr el modelo
        # real desde esta sesión) - si la tasa de LangFix no baja, revisar.
        tail += (
            "[CRITICAL LANGUAGE RULE]: The language for this turn is "
            f"{turn_language}. You MUST generate your visible response in "
            f"{turn_language}, matching the language of the user's query above, "
            "unless the user explicitly asks for a translation or language switch."
            f"{reminder}\n\n"
            if is_en else
            "[REGLA CRÍTICA DE IDIOMA]: El idioma de este turno es "
            f"{turn_language}. DEBES generar tu respuesta visible en "
            f"{turn_language}, igualando el idioma de la consulta del usuario de "
            "arriba, salvo que el usuario pida explícitamente una traducción o "
            "un cambio de idioma."
            f"{reminder}\n\n"
        )
        return tail

    def _build_reasoning_prompt(
        self,
        user_input: str,
        compacted_context: str,
        web_context: str,
        complex_reasoning: bool,
        inject_dev_override: bool = False,
        metacognitive_context: str = "",
        lang: Optional[str] = None,
        include_final_answer_tail: bool = True,
        thin_context_active: bool = False,
        lean: bool = False,
    ) -> str:
        """
        `lean=True` — carril fast_path: usa la cola LIGERA
        `_fastpath_answer_tail` (estilo breve + regla de idioma, SIN el
        bloque `[CALIBRACIÓN]` de ejemplos de física ni el recordatorio
        de `<thought>`) en vez de `_final_answer_instruction_tail`. Va de
        la mano con `system_override=_get_fastpath_system_prompt(...)` en
        la llamada — juntos le sacan a phi3.5:3.8b el andamiaje de
        razonamiento que lo hace alucinar en consultas simples. El resto
        del cuerpo (contexto, web, `_factual_enumeration_caution`, ancla)
        no cambia.

        `thin_context_active=True` — pásalo cuando `web_context` incluye
        el aviso de `build_thin_context_warning()` (fuentes que no
        cubren lo específico consultado). Agrega un recordatorio CORTO
        cerca del final del prompt (ver `_final_answer_instruction_tail`
        y el BLINDAJE completo ahí) en vez de confiar solo en el aviso
        completo, que queda enterrado varios miles de tokens antes —
        MEDIDO insuficiente por sí solo con qwen2.5:3b (ver ese
        BLINDAJE para el caso real).

        `lang` ("English"/"Spanish"), cuando se provee, decide el
        idioma de las ETIQUETAS de esta función ("Consulta del
        usuario:"/"User query:", etc.) — típicamente el idioma efectivo
        del turno (ver `_resolve_turn_language`), no necesariamente
        `self.current_language`. Si se omite, cae al de sesión — mismo
        comportamiento que antes de que existiera este parámetro.

        `include_final_answer_tail=False` omite el bloque de ESTILO +
        REGLA DE IDIOMA (ver `_final_answer_instruction_tail`) — solo
        para la Pasada 1 del modo de dos pasadas (ver
        `_call_llm_two_pass`), cuya salida es el `<thought>` interno,
        nunca la respuesta visible: ese bloque no le sirve de nada ahí
        (existe para moldear una respuesta que esta pasada no escribe) y
        en la práctica activamente ESTORBA — es la MISMA regla de idioma
        que, sin esta exclusión, suprimía la emisión de `<thought>` por
        completo (ver el BLINDAJE en `_final_answer_instruction_tail`).

        Pero omitir el bloque COMPLETO se llevó puesto también el
        recordatorio de `<thought>` que vive dentro de él — la Pasada 1
        es justo la que MÁS lo necesita, porque es la que debe abrir
        `<thought>`. Por eso, en este caso, se agrega esa misma oración
        SOLA (sin el resto del bloque de estilo/idioma, que sigue sin
        aplicar aquí) antes del ancla final. BLINDAJE (bug real, MEDIDO
        con la forma real de este prompt — system prompt completo +
        contexto web real): sin esta línea, `<thought>` abría 1/5 veces;
        con ella, 5/5.

        Sigue apareciendo el ancla final ("Respuesta:"/"Answer:") — la
        generación necesita algún punto de arranque igual.
        """
        prompt = ""
        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"

        if inject_dev_override:
            prompt += f"{self.DEV_MODE_OVERRIDE}\n\n"

        if compacted_context:
            header = "Recent conversation context:" if is_en else "Contexto conversacional reciente:"
            prompt += f"{header}\n{compacted_context}\n\n"

            # Nota (medido - turno "World Cup 2022 final"
            # en inglés, INMEDIATAMENTE después del turno "final Mundial
            # 2022 detalles" que había alucinado la biografía del
            # luchador Dragon Lee / Emmanuel Muñoz González): esta vez
            # la búsqueda web sí trajo los 3 artículos correctos de
            # Wikipedia sobre el Mundial 2022 (visible en las tarjetas
            # de la UI, carga=0.18s, sin reasignación de runner) - pero
            # la respuesta final volvió a citar al luchador. Causa
            # real, encontrada leyendo `get_recent_history()` en
            # memory_graph.py: devuelve los últimos N turnos por
            # TIMESTAMP global, sin ningún filtro de relevancia ni de
            # corrección posterior - así que la respuesta alucinada del
            # turno ANTERIOR (guardada tal cual la generó el modelo)
            # queda en "Contexto conversacional reciente:" del turno
            # SIGUIENTE, y nada le indicaba al modelo que ese texto
            # podía estar mal. Con un modelo de 3B, que no arbitra bien
            # entre bloques de contexto que se contradicen, el bloque
            # de arriba (su propia afirmación previa, dicha con
            # confianza) pesó más que el bloque web de abajo (datos
            # frescos y correctos) - el modelo terminó citándose a sí
            # mismo en vez de a la fuente nueva. Arreglar
            # `get_recent_history()` para que filtre por relevancia
            # exigiría heurísticas frágiles (¿cómo saber automáticamente
            # que un turno pasado "fue corregido"?); esta regla explícita
            # es más simple, más segura, y sigue el mismo patrón usado en
            # el resto de este prompt (reglas de prioridad textuales
            # antes que lógica de filtrado implícita). Solo se agrega
            # cuando HAY web_context real con el que priorizar - si no
            # hubo búsqueda web exitosa en este turno, no hay nada nuevo
            # que deba pesar más que el historial, así que la regla no
            # aplica.
            if web_context:
                priority_rule = (
                    "[PRIORITY RULE: the conversation context above may "
                    "include one of YOUR OWN earlier answers that was "
                    "wrong or has since been corrected. The real-time web "
                    "context below comes from a fresh search done "
                    "specifically for THIS question. If the web context "
                    "contradicts anything stated in the conversation "
                    "context above, trust the web context — do not repeat "
                    "or defend your own earlier statement.]\n\n"
                    if is_en else
                    "[REGLA DE PRIORIDAD: el contexto conversacional de "
                    "arriba puede incluir una respuesta TUYA anterior que "
                    "haya sido incorrecta o que ya fue corregida. El "
                    "contexto web de abajo viene de una búsqueda en "
                    "tiempo real hecha específicamente para ESTA "
                    "pregunta. Si el contexto web contradice algo dicho "
                    "en el contexto conversacional de arriba, confía en "
                    "el contexto web — no repitas ni defiendas tu propia "
                    "afirmación anterior.]\n\n"
                )
                prompt += priority_rule

        if web_context:
            header = (
                "[Contexto web recuperado en tiempo real]"
                if not is_en else
                "[Real-time retrieved web context]"
            )
            prompt += f"{header}\n{web_context}\n\n"

        if metacognitive_context:
            prompt += f"{metacognitive_context}\n\n"

        user_label = "User query:" if is_en else "Consulta del usuario:"
        prompt += f"{user_label}\n{user_input}\n\n"

        # Regla de idioma explícita, construida a partir del idioma
        # EFECTIVO del turno (`lang` == `effective_lang`, que viene de
        # `_resolve_turn_language`): el idioma detectado en este mensaje
        # cuando la señal es inequívoca, y el del selector de la UI
        # (`self.current_language`) solo como respaldo para mensajes
        # cortos/ambiguos.
        #
        # Deliberadamente NO usa `self.current_language` de forma
        # absoluta: hacerlo obligaba a responder en el idioma del
        # selector "regardless of the user's input", así que un mensaje
        # en inglés con el selector en Español se contestaba en español.
        # Peor aún, contradecía a la cabecera "system" de este mismo
        # turno, que `_call_llm(lang_override=effective_lang)` ya elige
        # según el idioma detectado - el modelo recibía dos órdenes de
        # idioma opuestas en un mismo prompt.
        #
        # Se coloca al final, inmediatamente antes de la respuesta, para
        # maximizar su peso frente a un modelo de 3B - ver
        # `_final_answer_instruction_tail` para el texto completo
        # (estilo + regla de idioma) y la nota de supresión de
        # <thought> que motiva el parámetro `include_final_answer_tail`.
        if lean:
            # Carril fast_path: cola LIGERA, sin ejemplos de física ni
            # recordatorio de <thought> (ver `_fastpath_answer_tail`).
            prompt += self._fastpath_answer_tail(lang)
        elif include_final_answer_tail:
            prompt += self._final_answer_instruction_tail(
                lang, thin_context_reminder=thin_context_active,
            )
        else:
            # Nota (medido - regresión introducida por el
            # modo de dos pasadas): excluir la cola completa de arriba
            # también se llevó puesto el recordatorio de <thought> que
            # esa misma cola contiene (ver la nota en
            # `_final_answer_instruction_tail` - SIN él, la emisión de
            # <thought> caía a 0/9 en qwen2.5:3b). La Pasada 1 es
            # EXACTAMENTE el único lugar donde ese recordatorio hace
            # falta de verdad - es la pasada que debe abrir <thought>
            # - así que quitárselo reintrodujo la supresión que el
            # recordatorio existía para arreglar. Medido de nuevo aquí
            # con la forma REAL de este prompt (system prompt completo +
            # contexto web real): 1/5 abre <thought> sin esta línea,
            # 5/5 con ella. No se reusa el bloque de estilo/idioma
            # completo - sigue sin aplicar a esta pasada (ver docstring)
            # - solo esta oración suelta.
            prompt += (
                " Remember: your output must ALWAYS start with the mandatory "
                "<thought>...</thought> block defined above, before anything else.\n\n"
                if is_en else
                " Recuerda: tu salida debe empezar SIEMPRE con el bloque "
                "<thought>...</thought> obligatorio definido arriba, antes que "
                "cualquier otra cosa.\n\n"
            )
            # Mismo motivo que `thin_context_reminder` en
            # `_final_answer_instruction_tail` (ver ese nota): esta
            # es la Pasada 1, cuyo plan alimenta directamente a la
            # Pasada 2 ("Ya planificaste... este es tu propio plan").
            # Reforzar el aviso acá, cerca del final de este prompt
            # también, aumenta la chance de que el plan mismo ya
            # incorpore la aclaración - en vez de depender únicamente
            # de que la Pasada 2 la agregue de nuevo sobre un plan que
            # nunca la mencionó.
            if thin_context_active:
                prompt += (
                    " Also remember: the sources for this turn do NOT "
                    "specifically cover what was asked — your plan must "
                    "include stating that plainly before listing what the "
                    "sources DO cover.\n\n"
                    if is_en else
                    " Recuerda también: las fuentes de este turno NO cubren "
                    "específicamente lo que se preguntó — tu plan debe incluir "
                    "decirlo claramente antes de listar de qué sí hablan las "
                    "fuentes.\n\n"
                )

        # La etiqueta final es lo último que lee el modelo antes de
        # generar - la posición de mayor peso de todo el prompt. Estaba
        # hardcodeada como "Respuesta:" incluso en turnos en inglés, así
        # que empujaba al español justo en el punto de arranque de la
        # generación, en contra de la regla de idioma de arriba. Se
        # mantiene incluso sin la cola de arriba (Pasada 1 del modo de
        # dos pasadas): la generación necesita un ancla de arranque igual.
        prompt += "Answer:" if is_en else "Respuesta:"
        return prompt

    # Nota (confirmado con sovnode_memory.db - turno
    # "dime ecuaciones matematicas" del 2026-08-25, id
    # cf87d5dd-2e0d-4783-b18e-d07e335cdbf5): la respuesta almacenada
    # trae la explicación completa y correcta, seguida de
    # "\n\nRespuesta: <la misma lista, resumida y renumerada desde 1>".
    # Causa raíz: el ancla de arranque de arriba ("Respuesta:"/
    # "Answer:", la línea `prompt += ...` justo antes de este comentario)
    # es texto LITERAL que el modelo ve como parte del prompt. Con
    # `num_predict` generoso (`MemoryGovernor.BASE_NUM_PREDICT`) y SIN
    # ningún `stop`, un modelo de 3B que ya terminó una respuesta
    # elaborada (obligatoria por `_FINAL_ANSWER_STYLE_ES/EN`) tiene
    # margen de sobra para re-disparar el mismo patrón "explicar, anclar
    # con 'Respuesta:', responder" que vio como prefijo de SU PROPIO
    # turno - generando una segunda pasada resumida de la misma
    # respuesta dentro de la misma llamada HTTP, no un turno aparte.
    #
    # Esto NO es (solo) un problema de longitud de `num_predict`: el
    # turno de control inmediatamente anterior en el mismo log
    # ("dame ecuaciones de fisica", mismo presupuesto, mismo modelo,
    # 69s antes) no lo mostró. Es la ausencia de una frontera explícita
    # de "ya terminaste". Un `stop` en Ollama corta la generación en el
    # instante en que el modelo empieza a escribir el ancla por segunda
    # vez - ahorra el tiempo Y los tokens de la mitad duplicada, no
    # solo la limpia después de generarla entera (que es justo lo que
    # costaba la lentitud que se viene reportando).
    #
    # Los dos variantes (español e inglés) van siempre juntos, sin
    # condicionar por idioma: barato incluir ambos, y cubre el caso
    # borde de un turno cuyo idioma cambia a mitad de pipeline (ver
    # `LangFix` en el comentario de balance de velocidad más abajo).
    _ANSWER_RESTART_STOP_SEQUENCES: List[str] = ["\nRespuesta:", "\nAnswer:"]

    def __init__(
        self,
        wal: Optional[WriteAheadLog] = None,
        router: Optional[IntentRouter] = None,
        model_name: Optional[str] = None,
        ollama_endpoint: Optional[str] = None,
    ) -> None:
        # 🟢 Inicializar el cerrojo antes de arrancar hilos secundarios
        #
        # Nota (propuesto y verificado por el usuario contra el
        # código real de este archivo): antes un `RLock` - como máximo
        # UNA llamada a Ollama en vuelo, sin importar cuántas se
        # despacharan "en paralelo" (ver el comentario de
        # `_tree_of_thought_reasoning`, que documentaba explícitamente
        # que las ramas A/B quedaban serializadas igual esperando este
        # mismo cerrojo). Ahora que `OllamaProcessManager` arranca el
        # servidor con `OLLAMA_NUM_PARALLEL=2` (ver ollama_manager.py),
        # el servidor sí puede atender 2 requests concurrentes de
        # verdad - así que el cerrojo pasa a `BoundedSemaphore(2)` para
        # dejar pasar hasta 2 llamadas en simultáneo (mismo número que
        # `OLLAMA_NUM_PARALLEL`; si se sube uno, subir el otro junto).
        # Seguro frente a los ~10 call sites existentes de `_llm_lock`
        # en este archivo: todos hacen `with self._llm_lock:` alrededor
        # de UNA sola petición HTTP directa (nunca llaman, desde
        # DENTRO de ese bloque, a otra función que vuelva a tomar el
        # mismo cerrojo) - cero reentrancia real en el código actual,
        # así que perder la propiedad de RLock (mismo hilo puede
        # reacquirir sin bloquearse) no rompe nada existente. Un
        # Semaphore sí carece de esa propiedad: si algún llamador
        # futuro anida un `with self._llm_lock:` dentro de otro en el
        # mismo hilo, se bloquearía esperando un cupo que él mismo ya
        # está ocupando - a diferir con cuidado si se agrega ese
        # patrón más adelante.
        self._llm_lock = threading.BoundedSemaphore(2)

        self.dynamic_tool_engine = DynamicToolEngine(self)
        self.memory_graph = MemoryGraph("sovnode_memory.db")
        self.vector_rag = LocalVectorRAG(vector_dim=384)
        self._history_lock = threading.Lock()
        self._wal = wal or WriteAheadLog()
        self.tools = LocalToolDispatcher()
        self._router = router or IntentRouter()
        self._cas = CASEngine()
        self._sandbox = ExecutionSandbox()
        self._logic_validator = LogicalCoherenceValidator()
        self._fuzzer = AdversarialFuzzer()
        self._lsc_engine = LSCInferenceEngine()
        self._lexical_guard = LexicalSafetyNet()
        self._memory_governor = MemoryGovernor()
        self.current_language = "Spanish"
        self.set_language("Spanish")

        # Bandera para evitar colisiones de GPU/CPU entre el chat del usuario y el CognitiveGovernor
        self._is_processing_turn = False

        self.governor = CognitiveGovernor(self)
        self.governor.start()

        self.alert_queue: "queue.Queue[ProactiveAlert]" = queue.Queue(
            maxsize=self.ALERT_QUEUE_MAXSIZE
        )

        # Modelo ÚNICO de respuesta (ver RESPONSE_MODEL). `self.model` es el
        # nombre canónico nuevo; `self.general_model` / `self.coder_model` /
        # `self.ollama_model` se conservan como ALIAS que apuntan al MISMO
        # valor — con un solo modelo de propósito general no hay rol coder
        # separado, y mantener los alias evita reescribir los ~25 call sites
        # que ya los leen (cambio mínimo, sin churn en un archivo de 588 KB
        # sin cobertura de modelo en vivo). `OLLAMA_GENERAL_MODEL` se sigue
        # aceptando por compatibilidad con configs viejas.
        self.model = (
            os.getenv("OLLAMA_MODEL")
            or os.getenv("OLLAMA_GENERAL_MODEL")
            or model_name
            or self.RESPONSE_MODEL
        ).strip()
        self.general_model = self.coder_model = self.model
        self.embed_model = (
            os.getenv("OLLAMA_EMBED_MODEL")
            or "nomic-embed-text"
        ).strip()
        # Router rápido vía LLM — ver la nota junto a ROUTER_LLM_NUM_PREDICT
        # y _classify_turn. qwen2.5:0.5b elegido por el usuario: ya existe
        # el carril de sampling de la familia qwen en MemoryGovernor.
        # pinned_options, así que lo hereda gratis y consistente con el
        # resto del sistema.
        self.router_model = (
            os.getenv("OLLAMA_ROUTER_MODEL")
            or "qwen2.5:0.5b"
        ).strip()
        self.ollama_model = self.model

        # Esfuerzo de razonamiento efectivo de gpt-oss (campo `think`),
        # resuelto una vez — ver THINK_LEVEL. "" / "off" / "none" -> None
        # (no se manda el campo).
        _think = os.getenv(self._THINK_LEVEL_ENV_VAR, self.THINK_LEVEL).strip().lower()
        self.think_level: Optional[str] = None if _think in ("", "off", "none", "0") else _think

        # Cuánto tiempo se considera "vigente" el conocimiento web cacheado
        # antes de forzar una nueva búsqueda en internet (Web-to-RAG).
        self.web_knowledge_ttl_seconds = float(
            os.getenv("WEB_KNOWLEDGE_TTL_SECONDS", str(24 * 3600))
        )

        configured_endpoint = (
            ollama_endpoint
            or os.getenv("OLLAMA_ENDPOINT")
            or self.OLLAMA_ENDPOINT
        )
        self.ollama_endpoint = configured_endpoint.rstrip("/")

        self._session = None

        if requests is not None:
            with contextlib.suppress(Exception):
                self._session = requests.Session()

                if HTTPAdapter is not None:
                    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
                    self._session.mount("http://", adapter)
                    self._session.mount("https://", adapter)

        # Optimización #1 - Prefix Alignment & KV-Cache Persistente:
        # caché de cabeceras "system" (SYSTEM_PROMPT + LANG_ENFORCE_
        # DIRECTIVE), indexada por (idioma, ...) - ver `_get_frozen_header`
        # y `_get_fastpath_system_prompt`. Antes era un dict FIJO de 2
        # entradas (una por rol, ignorando el idioma) construido una única
        # vez aquí; con turnos que ahora piden dinámicamente el idioma
        # detectado en cada prompt (ver `_resolve_turn_language`, no
        # solo el de sesión), ese dict fijo habría servido siempre la
        # cabecera del idioma con el que arrancó el proceso. Sigue
        # cumpliendo la misma garantía de KV-cache: cada objeto `str`
        # se construye una sola vez por clave y se reutiliza byte-idéntico
        # en cada acceso posterior.
        self._frozen_system_headers: Dict[Tuple[str, bool], str] = {}
        # Precalentado para la sesión inicial - evita que el PRIMER turno
        # pague la construcción de estas cabeceras (incluyen
        # json.dumps(TOOLS_SCHEMA, ...)) de forma perceptible. Con la
        # arquitectura de modelo único la generación principal usa el
        # header LEAN (ver run_turn/process_turn); el header base se sigue
        # usando en las llamadas secundarias que pasan por `_call_llm`
        # (followup de tool-calling, verificadores, LangFix, continuación).
        self._get_frozen_header(self.current_language, False)
        self._get_fastpath_system_prompt(self.current_language)

        # Optimización #2 - Caché Semántico de Respuesta Directa: umbral
        # de similitud coseno y TTL configurables por entorno sin tocar
        # código. Un turno nuevo cuya consulta sea >= threshold similar a
        # una ya resuelta se responde de inmediato desde MemoryGraph.
        self.semantic_cache_threshold = float(
            os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.93")
        )
        self.semantic_cache_ttl_seconds = float(
            os.getenv("SEMANTIC_CACHE_TTL_SECONDS", str(7 * 24 * 3600))
        )
        self.semantic_cache_enabled = (
            os.getenv("SEMANTIC_CACHE_ENABLED", "1").strip() != "0"
        )
        # Último modo devuelto por embeddings.get_embedding_with_mode (ver
        # compute_query_embedding_with_mode). El umbral de similitud de
        # arriba solo es válido para EMBEDDING_MODE_SEMANTIC; con el
        # fallback hash el caché se degrada a coincidencia exacta. Arranca
        # en None = todavía no se ha vectorizado nada en este proceso.
        self._last_embedding_mode: Optional[str] = None
        self._embedding_fallback_warned = False

        # Optimización #5 - Gobernanza cooperativa: Event en vez de un
        # bool plano. CognitiveGovernor duerme con Event.wait(timeout=...)
        # en vez de time.sleep(...): un time.sleep NO puede interrumpirse,
        # así que si el usuario mandaba un mensaje justo después de que el
        # gobernador empezara a dormir, el gobernador seguía dormido hasta
        # agotar el intervalo completo (hasta 5s) antes de volver a
        # revisar. Event.wait(timeout=...) retorna en cuanto otro hilo
        # llama a .set() - típicamente en menos de 5ms - sin esperar a que
        # el timeout se agote.
        self._pause_governor_event = threading.Event()

        logger.info(
            "Orchestrator inicializado: model=%s (único) router=%s think=%s endpoint=%s",
            self.model,
            self.router_model,
            self.think_level or "off",
            self.ollama_endpoint,
        )

    # Nota (medido - ver la nota junto a
    # SignalTag.FACTUAL_ENUMERATION / _FACTUAL_ENUMERATION_PATTERN en
    # router.py): un pedido de enumerar N hechos técnicos establecidos
    # ("dime ecuaciones importantes de la física") no tiene ninguna señal
    # hoy - cae en fast_path/general_model (típicamente el modelo 3B) sin
    # ningún aviso especial, y un modelo chico, una vez que se le acaban
    # los 2-3 hechos que sí tiene memorizados con confianza, rellena el
    # resto por continuación de patrón en vez de admitir que no sabe un
    # cuarto o quinto elemento. medido: "Ley de la Centrífuga" (fórmula y
    # variable inventadas) y "Ley de los Ferromagnetismo de Maxwell"
    # (fórmula real, nombre/atribución inventados) en el mismo turno.
    #
    # No se resuelve enrutando a slow_path (ver por qué en la nota de
    # router.py) ni forzando el modelo 7B (no hay forma de confirmar
    # desde acá que esté descargado en el Ollama del usuario - forzarlo a
    # ciegas cambiaría una respuesta rápida-pero-arriesgada por un fallo
    # de red o una descarga de varios GB en medio del turno). La
    # mitigación que sí es segura para cualquier modelo activo: una
    # advertencia corta agregada al contexto de este turno, nunca al
    # historial persistido, pidiendo priorizar precisión sobre completar
    # la cantidad pedida.
    #
    # nota 2 (medido - turno "hola dime las ecuaciones mas
    # improtantes de la matematicas", screenshot del usuario, mismo router
    # path=fast_path score=+0.00 que confirma que esta misma advertencia
    # sí se estaba inyectando): la versión original de este aviso solo
    # cubre invención total (una ley que no existe). Acá el modelo no
    # inventó nada de la nada - de 10 "ecuaciones importantes de la
    # MATEMÁTICA", incluyó Conservación de la Energía, Conservación de la
    # Masa (dos veces, con fórmulas distintas) y la Ley de Continuidad:
    # las tres son leyes REALES y genuinas... de FÍSICA, no de
    # matemática. Ninguna de las dos verificaciones que ya pedía el aviso
    # ("¿es real?", "¿está reconocida?") detecta esto, porque la
    # respuesta a ambas es sí - el fallo es de DOMINIO, no de veracidad.
    # Mismo turno, segundo síntoma: la Ley de Continuidad y una segunda
    # "Ley de Conservación de la Masa" son, en el fondo, la misma
    # ecuación (la forma diferencial de conservación de masa en mecánica
    # de fluidos) presentada dos veces bajo nombres distintos - relleno
    # para llegar a la cantidad de 10 pedida, la env práctica del mismo
    # problema de fondo que ya motivó este método (preferir menos
    # elementos genuinamente distintos antes que completar el número).
    # Se agrega un segundo párrafo, separado del original para no diluir
    # ninguna de las dos advertencias entre sí - MIN_MARKERS y el resto
    # del pipeline de fuga no tocan este texto, así que no hay riesgo de
    # que un párrafo más largo dispare _strip_leaked_reasoning() por error.
    #
    # nota 3 (medido - turno "dime ecuaciones importantes
    # de fisica", screenshot + log del usuario, mismo path=fast_path
    # score=+0.00 que confirma que los dos párrafos de arriba sí se
    # estaban inyectando): esta vez el fallo no fue mezcla de dominio ni
    # duplicado aislado - el modelo enumeró leyes reales de física al
    # principio (Boyle-Mariotte, Ohm, Bernoulli...) pero, ya sin hechos
    # genuinos que agregar, empezó a inventar nombres que SUENAN a leyes
    # de Newton reales pero no existen ("Ley de Newton de la Repulsión
    # Gravitacional" - la gravedad es atractiva, no repulsiva; "Ley de
    # Newton de la Tensión en Paredes") y terminó en un bucle degenerativo
    # repitiendo el mismo ítem más de una decena de veces, variando solo
    # la función trigonométrica de la fórmula, hasta cortarse a mitad de
    # palabra contra el techo de tokens. El párrafo original ya cubre
    # "no inventes para completar la cantidad", pero no es específico
    # sobre inventar NOMBRES que suenan plausibles ni sobre qué hacer al
    # notar que la estructura se está por repetir - se agrega un tercer
    # párrafo con ambas instrucciones, más una red de seguridad
    # determinística en código (`_dedupe_enumeration_items`, ver su
    # Nota) para el caso - ya medido - de que el modelo ignore
    # también esta instrucción de prompt.
    @staticmethod
    def _factual_enumeration_caution(lang: Optional[str]) -> str:
        is_en = (lang or "Spanish") == "English"
        if is_en:
            return (
                "\n\n[NOTICE — UNVERIFIED FACTUAL RECALL]: this request asks you to "
                "enumerate several established technical facts (laws, formulas, "
                "principles) with no web context or verification tool available this "
                "turn. Before writing each item, confirm internally that it is real "
                "and genuinely recognized in the field. If you are not confident an "
                "additional item exists as you would describe it, it is better to "
                "give FEWER items, all correct, than to reach the requested count by "
                "inventing a name, formula, or attribution that sounds plausible.\n\n"
                "If the request names a specific FIELD or DISCIPLINE (e.g. "
                "'of mathematics', 'of physics'), every item must strictly belong to "
                "THAT discipline — a real, correctly-named law from a DIFFERENT "
                "discipline is still the wrong answer, even though it is a genuine "
                "fact. Also never restate the same underlying law or equation twice "
                "under a different name just to reach the requested count — each "
                "item must be substantively distinct from every other item you give."
                "\n\nAlso, never produce more than 8 to 10 items in total, even if "
                "the request does not specify a count. If you notice you are about "
                "to repeat the same title or structure while only changing a "
                "symbol, a trigonometric function, or a subscript, STOP the list "
                "right there instead of continuing — that is a sign no genuinely "
                "distinct real items remain, not an invitation to keep generating "
                "variants. Never invent a law name that merely sounds plausible "
                "(for example attributing to a well-known scientist a law they did "
                "not formulate) just to make the list look more complete."
            )
        return (
            "\n\n[AVISO — RECUPERACIÓN FACTUAL SIN VERIFICACIÓN]: este pedido es "
            "enumerar varios hechos técnicos establecidos (leyes, fórmulas, "
            "principios) sin contexto web ni herramienta de verificación "
            "disponible en este turno. Antes de escribir cada elemento, confirmá "
            "internamente que es real y está genuinamente reconocido en la "
            "disciplina. Si no estás seguro de que un elemento adicional exista tal "
            "como lo ibas a describir, es preferible entregar MENOS elementos, "
            "todos correctos, que completar la cantidad pedida inventando un "
            "nombre, una fórmula o una atribución que suene plausible.\n\n"
            "Si el pedido nombra una DISCIPLINA concreta (por ejemplo 'de la "
            "matemática', 'de la física'), cada elemento debe pertenecer "
            "estrictamente a ESA disciplina — una ley real y bien nombrada pero de "
            "OTRA disciplina sigue siendo una respuesta incorrecta, aunque sea un "
            "hecho genuino. Tampoco repitas la misma ley o ecuación de fondo dos "
            "veces bajo un nombre distinto solo para alcanzar la cantidad pedida — "
            "cada elemento debe ser sustancialmente distinto de los demás que des."
            "\n\nAdemás, no generes más de 8 a 10 elementos en total, incluso si el "
            "pedido no aclara una cantidad. Si notás que estás por repetir el mismo "
            "título o estructura variando solo un símbolo, una función "
            "trigonométrica o un subíndice, DETENÉ la lista ahí mismo en vez de "
            "continuar — esa es una señal de que ya no quedan elementos reales "
            "distintos, no una invitación a seguir generando variantes. Nunca "
            "inventes un nombre de ley que suene plausible (por ejemplo atribuyendo "
            "a un científico conocido una ley que no formuló) solo para que la "
            "lista parezca más completa."
        )

    def _llm_router_classify(self, user_input: str) -> Optional[RoutePath]:
        """
        Clasificación fast/slow vía LLM (self.router_model, 0.5B por
        defecto) — ver la nota junto a ROUTER_LLM_NUM_PREDICT.

        A propósito NO se le pasa historial de conversación ni contexto
        web — solo el mensaje crudo de este turno. Ver Sección 18 (bug
        real de historial envenenado reinyectado como contexto): un
        router que solo ve el turno actual no puede sufrir esa misma
        clase de bug.

        Devuelve None ante CUALQUIER fallo — HTTP, conexión, o una
        respuesta que no contenga 'fast_path'/'slow_path' reconocible —
        para que el llamador (_classify_turn) caiga a IntentRouter
        determinista sin romper el turno. Reusa la convención
        `.startswith("[ERROR")` que _call_llm_raw ya documenta y que el
        resto de este archivo ya chequea.

        Todo el cuerpo va además envuelto en un try/except amplio, a
        propósito: cualquier excepción cruda acá (no solo el sentinel
        "[ERROR" que _call_llm_raw devuelve en su propio try/except)
        también debe degradar a IntentRouter, nunca tumbar el turno
        entero — mismo principio que _call_llm_raw ya aplica un nivel más
        abajo. Esto también es lo que deja pasar, sin romper, a un
        Orchestrator armado a mano en un test (object.__new__) al que le
        falte algún atributo de la cadena de _call_llm_raw — un entorno
        de test sin Ollama real de por medio debe comportarse igual que
        Ollama caído: cae al router determinista, no revienta.
        """
        try:
            raw, _tokens, _reason = self._call_llm_raw(
                user_input,
                target_model=self.router_model,
                temperature_override=self.ROUTER_LLM_TEMPERATURE,
                num_predict_override=self.ROUTER_LLM_NUM_PREDICT,
                system_override=self._ROUTER_LLM_SYSTEM_PROMPT,
                stop=["\n"],
                keep_alive_override="30m",
                perf_label="Router0.5B",
            )
        except Exception as exc:
            logger.warning(
                "🧭 [Router0.5B] excepción inesperada clasificando con %s "
                "(%s) — usando IntentRouter determinista.",
                getattr(self, "router_model", "?"), exc,
            )
            return None

        if raw.lstrip().startswith("[ERROR"):
            logger.warning(
                "🧭 [Router0.5B] Ollama falló clasificando con %s (%s) — "
                "usando IntentRouter determinista.",
                self.router_model, raw,
            )
            return None

        normalized = raw.strip().lower()
        if "slow_path" in normalized or normalized.startswith("slow"):
            return RoutePath.SLOW_PATH
        if "fast_path" in normalized or normalized.startswith("fast"):
            return RoutePath.FAST_PATH

        logger.warning(
            "🧭 [Router0.5B] respuesta no interpretable de %s: %r — "
            "usando IntentRouter determinista.",
            self.router_model, raw,
        )
        return None

    def _classify_turn(self, user_input: str) -> RoutingDecision:
        """
        Decisión de ruteo real usada por run_turn/process_turn.

        Corre SIEMPRE IntentRouter.classify() (determinista, cuesta
        microsegundos) para obtener tags/score/reason — ver la nota junto
        a ROUTER_LLM_NUM_PREDICT sobre por qué esas señales NO vienen del
        modelo de 0.5B. Además, SIEMPRE intenta una clasificación vía LLM
        (_llm_router_classify): si responde con éxito, su veredicto de
        `path` reemplaza al determinista (coincida o no — "reemplazo
        total" pedido por el usuario, no solo para casos ambiguos); si
        falla o no es interpretable, se usa el `path` determinista sin
        cambios. `reason` documenta siempre qué pasó, para que quede
        visible en la consola de logs de la UI.
        """
        start = time.perf_counter()
        deterministic = self._router.classify(user_input)
        llm_path = self._llm_router_classify(user_input)
        elapsed = (time.perf_counter() - start) * 1000
        # getattr con default: mismo motivo que en _llm_router_classify —
        # un Orchestrator armado a mano en un test (object.__new__) puede
        # no tener router_model seteado; esto es solo para el string de
        # log/reason, nunca debe poder tumbar la decisión de ruteo en sí.
        router_model_label = getattr(self, "router_model", "?")

        if llm_path is None:
            return replace(
                deterministic,
                elapsed_ms=elapsed,
                reason=(
                    f"{deterministic.reason} [Router0.5B ({router_model_label}) "
                    f"no disponible o respuesta no interpretable — se usó "
                    f"IntentRouter determinista]"
                ),
            )

        agreement = "coincide con" if llm_path == deterministic.path else "SOBRESCRIBE"
        return replace(
            deterministic,
            path=llm_path,
            elapsed_ms=elapsed,
            reason=(
                f"{deterministic.reason} [Router0.5B ({router_model_label}): "
                f"'{llm_path.value}' {agreement} IntentRouter "
                f"'{deterministic.path.value}']"
            ),
        )

    def _select_model_for_decision(self, decision: RoutingDecision) -> str:
        """
        Arquitectura de modelo único (2026-08-27): hay UN solo modelo de
        respuesta (`self.model`, gpt-oss:20b por defecto — ver
        RESPONSE_MODEL). No hay rol coder separado que seleccionar, así
        que esta función devuelve siempre ese modelo.

        Antes elegía entre general_model y coder_model según
        SignalTag.CODE_COMPLEX. El BLINDAJE que llevaba (no mandar
        MATH_EXPRESSION en solitario al coder para que un turno de física
        no heredara CODER_SYSTEM_PROMPT + DEV_MODE_OVERRIDE y terminara con
        un bloque ```python colgado — bug real, MEDIDO) deja de aplicar:
        con un único modelo de propósito general no hay CODER_SYSTEM_PROMPT
        ni DEV_MODE_OVERRIDE que contaminen nada. `firma(decision)` se
        mantiene por compatibilidad con los ~4 call sites.
        """
        return self.model

    def _emit_alert(self, message: str, level: str = "info") -> None:
        alert = ProactiveAlert(message=message, level=level)
        try:
            self.alert_queue.put_nowait(alert)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self.alert_queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self.alert_queue.put_nowait(alert)

    def _extract_math_candidate(self, text: str) -> Optional[str]:
        if not text or not text.strip():
            return None

        normalized = _MULTI_SPACE_RE.sub(" ", text.strip())

        equation_match = _EQUATION_CANDIDATE_RE.search(normalized)
        if equation_match:
            candidate = equation_match.group(0)
            return self._sanitize_math_candidate(candidate)

        command_stripped = _MATH_LEADING_NOISE_RE.sub("", normalized)

        function_match = _FUNCTION_EXPRESSION_RE.search(command_stripped)
        if function_match:
            candidate = function_match.group(0)
            return self._sanitize_math_candidate(candidate)

        algebraic_match = _ALGEBRAIC_EXPRESSION_RE.search(command_stripped)
        if algebraic_match:
            candidate = algebraic_match.group(0)
            return self._sanitize_math_candidate(candidate)

        if _MATH_COMMAND_RE.search(normalized):
            fallback = _MATH_LEADING_NOISE_RE.sub("", normalized)
            fallback = self._sanitize_math_candidate(fallback)

            if fallback and re.search(r"[\d\+\-\*/\^\(\)]", fallback):
                return fallback

        return None

    def _sanitize_math_candidate(self, candidate: str) -> str:
        if not candidate:
            return ""

        sanitized = _MATH_LEADING_NOISE_RE.sub("", candidate)
        sanitized = _MATH_TRAILING_NOISE_RE.sub("", sanitized)
        sanitized = _MULTI_SPACE_RE.sub(" ", sanitized).strip()
        sanitized = sanitized.strip(" \t\n.,;:!?")

        return sanitized

    def _dispatch_slow_path(
        self,
        text: str,
        decision: RoutingDecision,
    ) -> Tuple[TurnOutcome, List[EngineResult], bool]:
        tags = set(decision.tags)
        results: List[EngineResult] = []
        web_context_used = False

        if (
            SignalTag.LOGIC_AUDIT in tags
            or SignalTag.CONCEPTUAL_DENSE in tags
        ):
            fuzz_result = self._fuzzer.audit(text)
            results.append(fuzz_result)
            return TurnOutcome.SLOW_PATH_FUZZER, results, web_context_used

        if SignalTag.LSC_INFERENCE in tags:
            lsc_result = self._lsc_engine.infer(text)
            results.append(lsc_result)
            return TurnOutcome.SLOW_PATH_LSC, results, web_context_used

        if SignalTag.MATH_EXPRESSION in tags and SignalTag.CODE_COMPLEX not in tags:
            candidate = self._extract_math_candidate(text)

            if candidate:
                if "=" in candidate:
                    cas_result = self._cas.solve_equation(candidate)
                else:
                    cas_result = self._cas.simplify_expression(candidate)

                results.append(cas_result)
                return TurnOutcome.SLOW_PATH_CAS, results, web_context_used

        if SignalTag.CODE_COMPLEX in tags:
            code_match = _CODE_FENCE_RE.search(text)

            if code_match:
                sandbox_result = self._sandbox.run(code_match.group(1).strip())
                results.append(sandbox_result)
                return TurnOutcome.SLOW_PATH_SANDBOX, results, web_context_used

        return TurnOutcome.SLOW_PATH_GENERIC_REASONING, results, web_context_used

    def _get_fuzz_summary(self, result: FuzzingResult) -> str:
        summary = getattr(result, "summary", None)
        if summary:
            return str(summary)

        detail = getattr(result, "detail", None)
        if detail:
            return str(detail)

        return "El auditor no entregó detalles adicionales."

    def _is_critical_search_requirement(self, requirement: Any) -> bool:
        priority = getattr(requirement, "priority", None)
        return priority in (1, "1", "P1", "p1")

    def _recursive_self_critique(
        self,
        hypothesis: str,
        context: str,
        max_iterations: Optional[int] = None,
        target_model: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> Tuple[str, Optional[FuzzingResult], bool]:
        iterations = max_iterations or self.MAX_SELF_CRITIQUE_ITERATIONS
        current_hypothesis = hypothesis.strip()
        last_fuzz_result: Optional[FuzzingResult] = None
        web_context_used = False

        for _ in range(max(1, iterations)):
            fuzz_result = self._fuzzer.audit(current_hypothesis)
            last_fuzz_result = fuzz_result

            if fuzz_result.verdict == FuzzingVerdict.ROBUST:
                break

            requirements = getattr(fuzz_result, "search_requirements", ())
            critical_requirements = [
                requirement
                for requirement in requirements
                if self._is_critical_search_requirement(requirement)
            ]

            web_evidence = ""

            if critical_requirements:
                query = " ".join(
                    (
                        getattr(requirement, "query", "")
                        or getattr(requirement, "claim", "")
                    ).strip()
                    for requirement in critical_requirements
                ).strip()

                if query:
                    try:
                        # Bug real, MEDIDO: esta llamada nunca pasaba `lang`,
                        # así que search_web_context() siempre caía a su
                        # default (None -> español) sin importar el idioma
                        # de la UI. `lang` (parámetro de este método) casi
                        # nunca llega con valor real desde el único call
                        # site actual (_self_repair, más arriba en este
                        # archivo, no lo pasa) — por eso se usa como
                        # override opcional y se cae a self.current_language
                        # (idioma de sesión, siempre disponible), igual que
                        # el patrón ya establecido en otros métodos de esta
                        # clase (ver, por ejemplo, `lang_override or
                        # self.current_language`).
                        effective_lang = lang or self.current_language
                        web_evidence = search_web_context(
                            query,
                            lang=("en" if effective_lang == "English" else "es"),
                        ) or ""
                        if web_evidence:
                            web_context_used = True
                    except Exception:
                        web_evidence = ""

            fuzz_summary = self._get_fuzz_summary(fuzz_result)
            critique_prompt = (
                f"Hipótesis actual:\n{current_hypothesis}\n\n"
                f"Auditoría de fragilidad/fuzzer:\n{fuzz_summary}\n\n"
            )
            if web_evidence:
                critique_prompt += f"Evidencia web complementaria:\n{web_evidence}\n\n"
            critique_prompt += (
                "Refina y corrige la hipótesis para solucionar las vulnerabilidades o "
                "falta de sustento detectadas. Devuelve ÚNICAMENTE la versión corregida."
            )

            refined = self._call_llm(
                critique_prompt,
                target_model=target_model or self.general_model,
                lang_override=lang,
            )
            if refined and not refined.lstrip().startswith("[ERROR"):
                _, clean_refined = self._split_thought_and_content(refined)
                current_hypothesis = clean_refined or refined
                # Nota: ver `_strip_system_prompt_echo` - este
                # `_call_llm()` también manda el header congelado. La
                # hipótesis resultante se persiste como "lección" en
                # MemoryGraph (ver `_self_repair`, más arriba en este
                # archivo) y puede reinyectarse en prompts FUTUROS vía
                # `_fetch_metacognitive_lessons` - un eco acá no queda
                # contenido a este turno, se arrastra a turnos
                # siguientes, así que amerita la misma defensa aunque
                # este camino sea autónomo/en segundo plano
                # (CognitiveGovernor corre como hilo propio, ver
                # `self.governor.start()`) y no una respuesta directa
                # al usuario.
                current_hypothesis, _ = self._strip_system_prompt_echo(current_hypothesis)
            else:
                break

        return current_hypothesis, last_fuzz_result, web_context_used

    def _persist_knowledge_node_if_robust(
        self,
        hypothesis: str,
        fuzz_result: Optional[FuzzingResult],
    ) -> bool:
        if fuzz_result is None:
            return False

        if fuzz_result.verdict != FuzzingVerdict.ROBUST:
            return False

        try:
            verification: Dict[str, Any] = {
                "verdict": getattr(
                    fuzz_result.verdict, "value", str(fuzz_result.verdict)
                ),
                "robustness_score": getattr(fuzz_result, "robustness_score", None),
                "elapsed_ms": getattr(fuzz_result, "elapsed_ms", None),
                "detail": self._get_fuzz_summary(fuzz_result),
            }
            provenance: Dict[str, Any] = {
                "engine": "AdversarialFuzzer",
                "produced_by": "Orchestrator._recursive_self_critique",
                "model": self.ollama_model,
                "persisted_at": time.time(),
            }

            node = KnowledgeNode.create(
                domain="conceptual_hypothesis",
                axiom=hypothesis.strip(),
                verification=verification,
                provenance=provenance,
            )
            self._wal.append_knowledge_node(node)
            return True
        except Exception as exc:
            logger.warning("No se pudo persistir KnowledgeNode: %s", exc)
            return False
        
    # IDEA DE ARQUITECTURA (2026-08-19) - diagnóstico de throughput real:
    # antes de tocar ningún parámetro de velocidad "a ciegas", cada
    # llamada real a Ollama loguea cuánto de su tiempo total fue PREFILL
    # (procesar el prompt) vs. DECODE (generar tokens de salida), y los
    # tokens/segundo de cada fase por separado. Motivo, medido en vivo
    # (Ryzen 5700G + RX 5500 XT - RDNA1, fuera de la lista de tarjetas
    # que ROCm soporta en Windows, así que Ollama muy probablemente cae
    # a CPU sin avisar): un turno con contexto web reportó ~343 tokens
    # totales (razonamiento + respuesta) en ~73s - unos 4.7 tok/s
    # agregados, sin saber si ese tiempo se fue generando o solo
    # "leyendo" un prompt largo (system + evidencia web) antes de
    # escribir la primera palabra. Sin esta separación, subir
    # `num_predict` o ajustar hilos es adivinar; con ella, el propio log
    # dice cuál de las dos fases mejora (o no) con cada cambio.
    def _log_generation_perf(
        self,
        data: Dict[str, Any],
        eval_count: int,
        log_cb: Optional[Callable[[str], None]] = None,
        label: str = "LLM",
    ) -> None:
        """
        BLINDAJE (bug real, MEDIDO): esto SIEMPRE logueó vía `logger.info`
        únicamente — invisible en la terminal gráfica de sovnode_qt.py
        (no existe ningún puente logging->Qt, ver logger.py/_terminal_log:
        la terminal solo recibe lo que se emite explícitamente por la
        señal `log_message`). Para las llamadas de `StreamTurnWorker` que
        SÍ pasan por streaming (`ast_stream.py`) esto ya no importa —
        tienen su propio `_log_stream_perf` — pero cualquier ruta que
        llegue aquí vía `_call_llm_raw` NO-streaming (p.ej. las 3
        llamadas secuenciales/paralelas de `_tree_of_thought_reasoning`,
        el "slow path" — que es exactamente donde más tiempo se gasta y
        menos visibilidad había) seguía sin aparecer nunca en pantalla.
        `log_cb` (opcional, retrocompatible — default None no cambia
        nada para los ~10 llamadores existentes de `_call_llm`/
        `_call_llm_raw` que no lo pasan) permite que el llamador (p.ej.
        StreamTurnWorker, pasando `self.log_message.emit`) reciba el
        mismo mensaje que ya iba solo al log de archivo/consola Python.
        `label` distingue entre llamadas cuando varias comparten turno
        (p.ej. "ToT-A"/"ToT-B"/"ToT-Synthesis").
        """
        try:
            prompt_eval_count = int(data.get("prompt_eval_count", 0) or 0)
            prompt_eval_ns = int(data.get("prompt_eval_duration", 0) or 0)
            eval_ns = int(data.get("eval_duration", 0) or 0)
            load_ns = int(data.get("load_duration", 0) or 0)
        except (TypeError, ValueError):
            return

        if prompt_eval_count <= 0 and eval_count <= 0:
            return

        prefill_s = prompt_eval_ns / 1e9
        decode_s = eval_ns / 1e9
        load_s = load_ns / 1e9

        prefill_tok_s = (prompt_eval_count / prefill_s) if prefill_s > 0 else 0.0
        decode_tok_s = (eval_count / decode_s) if decode_s > 0 else 0.0

        message = (
            f"⚡ [{label}] prefill={prompt_eval_count}tok/{prefill_s:.2f}s "
            f"({prefill_tok_s:.1f}tok/s) | decode={eval_count}tok/{decode_s:.2f}s "
            f"({decode_tok_s:.1f}tok/s) | load={load_s:.2f}s"
        )
        logger.info(message)
        if log_cb is not None:
            with contextlib.suppress(Exception):
                log_cb(message)

    def _prepare_ollama_payload(
        self,
        prompt: str,
        *,
        target_model: Optional[str],
        lang_override: Optional[str],
        has_web_evidence: bool,
        temperature_override: Optional[float],
        num_predict_override: Optional[int],
        keep_alive_override: Optional[str],
        stop: Optional[List[str]],
        system_override: Optional[str],
        stream: bool,
    ) -> Tuple[Dict[str, Any], Optional[float], str]:
        """
        Arma el `payload` de `/api/generate` y el timeout efectivo,
        compartido byte-a-byte entre `_call_llm_raw` (no streaming) y
        `_stream_llm_raw` (streaming) — así las dos rutas piden
        exactamente las mismas `options` y la misma cabecera `system`,
        preservando la Optimización #1 (Prefix Alignment/KV-Cache) sin
        mantener dos copias de esta lógica que podrían divergir.

        `system_override` (opcional): reemplaza la cabecera congelada de
        `_get_frozen_header` — lo usa el carril `TRIVIAL_GREETING` de
        `run_turn`, que manda un system prompt mínimo (sin schema de
        tools ni protocolo <thought>) en vez del general de ~2700 tokens.
        Ninguno de los ~10 llamadores existentes lo pasa: default `None`
        = comportamiento idéntico al de antes.

        Devuelve `(payload, effective_timeout, model)`.
        """
        model = target_model or self.model
        is_coder = False  # modelo único: sin rol coder — ver RESPONSE_MODEL
        lang = lang_override or self.current_language

        options = self._memory_governor.pinned_options(
            is_coder=is_coder, has_web_evidence=has_web_evidence, model=model
        )
        if temperature_override is not None:
            options["temperature"] = max(0.0, min(2.0, temperature_override))
        if num_predict_override is not None:
            options["num_predict"] = num_predict_override
        if stop:
            options["stop"] = stop

        payload = {
            "model": model,
            "system": system_override if system_override is not None else self._get_frozen_header(lang, is_coder),
            "prompt": prompt,
            "stream": stream,
            "options": options,
            "keep_alive": keep_alive_override if keep_alive_override is not None else "30m",
        }

        # Campo `think` de Ollama para modelos Harmony (gpt-oss): fija el
        # esfuerzo de razonamiento (ver THINK_LEVEL — MEDIDO en el PASO 0:
        # "low" recorta el canal analysis de ~600 tok a ~15). Gateado por
        # nombre de modelo: NO se manda al router (qwen2.5:0.5b, sin canal
        # de razonamiento) ni a un OLLAMA_MODEL override que no sea gpt-oss.
        if self.think_level and "gpt-oss" in model.lower():
            payload["think"] = self.think_level

        # `self.OLLAMA_TIMEOUT_SECONDS` puede ser `None` ("modo
        # benchmark", vía SOVNODE_OLLAMA_TIMEOUT=none) - pasar `None`
        # directo a requests.post(timeout=...) es "esperar para
        # siempre", exactamente el riesgo que este nota corrige. En
        # ese caso se usa el techo duro de emergencia en su lugar, nunca
        # ausencia total de límite.
        effective_timeout = (
            self.OLLAMA_TIMEOUT_SECONDS
            if self.OLLAMA_TIMEOUT_SECONDS is not None
            else self.OLLAMA_HARD_TIMEOUT_FALLBACK_SECONDS
        )
        return payload, effective_timeout, model

    @staticmethod
    def _harmony_tool_call_to_text(data: Dict[str, Any]) -> str:
        """
        gpt-oss (formato Harmony) NO emite la llamada a herramienta como
        JSON dentro de `response`: Ollama la parsea del canal `commentary`
        y la devuelve en un campo `tool_calls` de nivel superior, dejando
        `response` VACÍO. MEDIDO en el PASO 0 (probe5) — sin este puente,
        `extract_tool_call("")` devuelve None y el turno de function-calling
        muestra una respuesta vacía.

        Sintetiza el string JSON que `extract_tool_call` / `normalize_tool_
        call` ya saben parsear (`{"tool": ..., "parameters": {...}}`).
        Devuelve "" si no hay ninguna tool call que convertir.

        Ollama anida los args como `tool_calls[0].function.arguments`; con
        el system prompt de SovNode (que muestra el schema `{"tool": ...,
        "parameters": {}}` como ejemplo) gpt-oss suele poner EXACTAMENTE
        ese dict como `arguments`, así que en la práctica se pasa tal cual.
        """
        calls = data.get("tool_calls")
        if not calls or not isinstance(calls, list):
            return ""
        fn = (calls[0] or {}).get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            with contextlib.suppress(Exception):
                args = json.loads(args)
        if isinstance(args, dict) and "tool" in args:
            payload = args
        else:
            payload = {
                "tool": fn.get("name"),
                "parameters": args if isinstance(args, dict) else {},
            }
        if not payload.get("tool"):
            return ""
        return json.dumps(payload, ensure_ascii=False)

    def _call_llm_raw(
        self,
        prompt: str,
        target_model: Optional[str] = None,
        extra_context_chars: int = 0,
        lang_override: Optional[str] = None,
        has_web_evidence: bool = False,
        temperature_override: Optional[float] = None,
        num_predict_override: Optional[int] = None,
        keep_alive_override: Optional[str] = None,
        stop: Optional[List[str]] = None,
        system_override: Optional[str] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        perf_label: str = "LLM",
    ) -> Tuple[str, int, str]:
        """
        Núcleo real de la llamada HTTP a Ollama. Devuelve (texto_saneado,
        eval_count, done_reason). `_call_llm()` (abajo) es un envoltorio
        de compatibilidad sobre esta función que solo devuelve el texto
        — el contrato histórico que ya consumen ~10 call sites de este
        archivo. `eval_count`/`done_reason` no se podían exponer ahí sin
        romper a TODOS esos llamadores (esperan `str`, no una tupla), así
        que se separó el núcleo en vez de cambiar el contrato existente.

        Necesarios para el modo de dos pasadas razonamiento/respuesta
        (ver `_call_llm_two_pass`): `done_reason` distingue si la Pasada
        1 cerró `</thought>` por su cuenta ("stop") o si llegó al techo
        de `num_predict_override` a mitad del plan ("length" — el caso
        borde que exige forzar el cierre, ver docstring de
        `_call_llm_two_pass`), y `eval_count` es lo que permite instru-
        mentar el reparto real de tokens (log %techo usado por pasada)
        en vez de asumirlo.

        `keep_alive_override` (opcional, default `None` — cero cambio
        para los ~10 llamadores existentes, que siguen recibiendo los
        "30m" de siempre): pedido explícito de optimizar el consumo de
        recursos de `generate_spontaneous_reflection` — esa llamada
        corre en segundo plano, sin que el usuario haya pedido nada, así
        que NO debería ser la responsable de mantener el modelo cargado
        en RAM/VRAM más tiempo del que ya estaba. Pasa "0" (descargar
        apenas termina esta respuesta) para que un chequeo periódico en
        medio de una sesión inactiva no reinicie el reloj de los 30
        minutos una y otra vez de forma indefinida.

        En fallo (excepción, HTTP != 200) devuelve (mensaje_de_error, 0,
        "error") — el mismo string "[ERROR] ..." que ya devolvía
        `_call_llm`, para que el chequeo `.lstrip().startswith("[ERROR")`
        que usan todos los llamadores actuales siga funcionando igual.

        `system_override` (opcional, default `None`): ver
        `_prepare_ollama_payload` — reemplaza la cabecera congelada por
        un system prompt a medida (carril `TRIVIAL_GREETING`).
        """
        payload, effective_timeout, _model = self._prepare_ollama_payload(
            prompt,
            target_model=target_model,
            lang_override=lang_override,
            has_web_evidence=has_web_evidence,
            temperature_override=temperature_override,
            num_predict_override=num_predict_override,
            keep_alive_override=keep_alive_override,
            stop=stop,
            system_override=system_override,
            stream=False,
        )
        with self._llm_lock:
            try:
                if self._session is not None:
                    resp = self._session.post(
                        self.ollama_endpoint,
                        json=payload,
                        timeout=effective_timeout,
                    )
                else:
                    resp = requests.post(
                        self.ollama_endpoint,
                        json=payload,
                        timeout=effective_timeout,
                    )

                if resp.status_code == 200:
                    data = resp.json()
                    raw_text = data.get("response", "")
                    # gpt-oss/Harmony: la tool call llega en `tool_calls`,
                    # no en `response` (ver _harmony_tool_call_to_text).
                    if not (raw_text or "").strip():
                        raw_text = self._harmony_tool_call_to_text(data) or raw_text
                    clean_text = self._lexical_guard.sanitize(raw_text)
                    eval_count = int(data.get("eval_count", 0) or 0)
                    done_reason = str(data.get("done_reason", "") or "")
                    self._log_generation_perf(data, eval_count, log_cb=log_cb, label=perf_label)
                    return clean_text, eval_count, done_reason
                return f"[ERROR] Ollama devolvió el código HTTP {resp.status_code}", 0, "error"
            except Exception as exc:
                return f"[ERROR] Fallo de conexión con Ollama: {exc}", 0, "error"

    def _call_llm(
        self,
        prompt: str,
        target_model: Optional[str] = None,
        extra_context_chars: int = 0,
        lang_override: Optional[str] = None,
        has_web_evidence: bool = False,
        temperature_override: Optional[float] = None,
        num_predict_override: Optional[int] = None,
        system_override: Optional[str] = None,
        stop: Optional[List[str]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        perf_label: str = "LLM",
    ) -> str:
        """
        Contrato histórico (texto plano) sobre `_call_llm_raw` — cero
        cambio de comportamiento para ningún llamador existente.

        BLINDAJE (bug real, confirmado con `inspect.signature().bind()`,
        que fallaba con `TypeError`): `temperature_override` faltaba en
        esta firma pese a que `_tree_of_thought_reasoning` YA lo usa
        (ramas A/B, temperaturas 0.2/0.7) — cualquier turno que entrara a
        SLOW_PATH_GENERIC_REASONING crasheaba antes de esta corrección.

        `stop` (opcional, default `None` — cero cambio para los
        llamadores que no lo pasan): `_call_llm_raw` ya lo soportaba
        (lo usa `_call_llm_two_pass` internamente, llamando a
        `_call_llm_raw` directo), pero esta envoltura no lo exponía —
        así que ningún llamador de UNA sola pasada (rama coder, rama
        FAST_PATH) tenía forma de cortar la generación en un ancla
        conocida. Ver `_ANSWER_RESTART_STOP_SEQUENCES` para el bug real
        que esto arregla.

        `log_cb`/`perf_label` (opcionales, default None/"LLM" — cero
        cambio para los llamadores que no los pasan): mismo puente hacia
        la terminal gráfica que ya usa `_log_stream_perf` en
        sovnode_qt.py para la ruta streaming, pero para esta ruta
        no-streaming — ver el BLINDAJE en `_log_generation_perf`.
        """
        text, _eval_count, _done_reason = self._call_llm_raw(
            prompt,
            target_model=target_model,
            extra_context_chars=extra_context_chars,
            lang_override=lang_override,
            has_web_evidence=has_web_evidence,
            temperature_override=temperature_override,
            num_predict_override=num_predict_override,
            system_override=system_override,
            stop=stop,
            log_cb=log_cb,
            perf_label=perf_label,
        )
        return text

    def _stream_llm_raw(
        self,
        prompt: str,
        *,
        target_model: Optional[str] = None,
        lang_override: Optional[str] = None,
        has_web_evidence: bool = False,
        temperature_override: Optional[float] = None,
        num_predict_override: Optional[int] = None,
        keep_alive_override: Optional[str] = None,
        stop: Optional[List[str]] = None,
        system_override: Optional[str] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        perf_label: str = "LLM",
    ):
        """
        Variante STREAMING de `_call_llm_raw`. Generador que:
          - `yield`-ea cada fragmento de texto (`str`) tal como llega de
            Ollama (`"stream": true`), para que `run_turn` lo reemita
            como `EventType.TOKEN` y la UI de Qt lo muestre escribiéndose
            token a token (`_on_chunk_received`/`_flush_stream_buffer`,
            que YA existen);
          - al terminar, `return`-ea `(texto_saneado, eval_count,
            done_reason)` — MISMO contrato de 3-tupla que `_call_llm_raw`,
            leído por el llamador vía `StopIteration.value`.

        Comparte `_prepare_ollama_payload` con `_call_llm_raw`, así que
        pide EXACTAMENTE las mismas `options`/`system` (Prefix Alignment/
        KV-Cache intacto entre una llamada streaming y una no-streaming
        del mismo turno). Respeta `self._llm_lock` durante todo el
        stream — si el llamador abandona el generador (`gen.close()` al
        cancelar), el `with` lo libera vía `GeneratorExit`.

        En fallo:
          - antes del primer chunk útil → `return ("[ERROR] ...", 0,
            "error")` (mismo string que `_call_llm_raw`, para que el
            chequeo `.lstrip().startswith("[ERROR")` del llamador corte
            el turno igual);
          - corte de conexión con contenido parcial ya recibido → se
            devuelve ese parcial saneado con `done_reason="error"` (mejor
            una respuesta trunca que tirar lo generado); la guarda de
            continuación de `run_turn` no dispara sobre `"error"`, así
            que no se encadena otra llamada sobre una conexión caída.
        """
        payload, effective_timeout, _model = self._prepare_ollama_payload(
            prompt,
            target_model=target_model,
            lang_override=lang_override,
            has_web_evidence=has_web_evidence,
            temperature_override=temperature_override,
            num_predict_override=num_predict_override,
            keep_alive_override=keep_alive_override,
            stop=stop,
            system_override=system_override,
            stream=True,
        )

        accumulated = ""
        final_data: Dict[str, Any] = {}
        session = self._session

        with self._llm_lock:
            try:
                poster = session.post if session is not None else requests.post
                with poster(
                    self.ollama_endpoint,
                    json=payload,
                    stream=True,
                    timeout=effective_timeout,
                ) as resp:
                    if resp.status_code != 200:
                        return (
                            f"[ERROR] Ollama devolvió el código HTTP {resp.status_code}",
                            0,
                            "error",
                        )
                    for line in resp.iter_lines(decode_unicode=True):
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        chunk = data.get("response", "")
                        if chunk:
                            accumulated += chunk
                            yield chunk
                        if data.get("done"):
                            final_data = data
            except Exception as exc:
                if len(accumulated.strip()) > 40:
                    clean_partial = self._lexical_guard.sanitize(accumulated)
                    eval_count = int(final_data.get("eval_count", 0) or 0)
                    return clean_partial, eval_count, "error"
                return f"[ERROR] Fallo de streaming con Ollama: {exc}", 0, "error"

        eval_count = int(final_data.get("eval_count", 0) or 0)
        done_reason = str(final_data.get("done_reason", "") or "")
        # `final_data` trae los mismos campos (`prompt_eval_count`,
        # `prompt_eval_duration`, `eval_count`, `eval_duration`,
        # `load_duration`) que el JSON no-streaming, así que
        # `_log_generation_perf` sirve sin cambios - mantiene la línea
        # `⚡ [FastSingle] prefill=...tok/...s | decode=...` en la
        # terminal gráfica.
        if final_data:
            self._log_generation_perf(final_data, eval_count, log_cb=log_cb, label=perf_label)
        # gpt-oss/Harmony: si no se streameó texto pero el turno terminó
        # con una tool call, ésta llega en `tool_calls` del chunk final —
        # ver _harmony_tool_call_to_text. Nada se emitió como TOKEN (bien:
        # una tool call no es respuesta visible), pero el llamador necesita
        # el JSON para extract_tool_call.
        text = accumulated
        if not text.strip():
            text = self._harmony_tool_call_to_text(final_data) or text
        clean_text = self._lexical_guard.sanitize(text)
        return clean_text, eval_count, done_reason

    def _iter_visible_tokens(self, stream, cancelled, sink: Dict[str, Any]):
        """
        Puente entre `_stream_llm_raw` (generador que yield-ea `str` y
        return-ea una 3-tupla) y `run_turn` (un generador que necesita
        reemitir cada fragmento como `EventType.TOKEN`).

        `yield`-ea solo la porción VISIBLE de cada fragmento (el
        `_ThoughtStreamGate` oculta el bloque `<thought>` interno
        mientras se genera). Al agotarse, deja en `sink`:
          - `sink["raw"]`       texto CRUDO completo (con `<thought>` si
                                lo hubo) — el post-procesado río abajo
                                (`_split_thought_and_content`, etc.) no
                                cambia;
          - `sink["eval_count"]`, `sink["done_reason"]`;
          - `sink["cancelled"]` `True` si el turno se canceló a mitad.

        Uso en `run_turn`:
            sink = {}
            for visible in self._iter_visible_tokens(stream, cancelled, sink):
                yield PipelineEvent(EventType.TOKEN, (visible, ""))
            raw_response = sink["raw"]; done_reason = sink["done_reason"]
        """
        gate = _ThoughtStreamGate()
        result: Tuple[str, int, str] = ("", 0, "")
        sink["cancelled"] = False
        gen = iter(stream)
        while True:
            if cancelled():
                gen.close()
                sink["cancelled"] = True
                break
            try:
                chunk = next(gen)
            except StopIteration as si:
                if si.value:
                    result = si.value
                break
            visible = gate.feed(chunk)
            if visible:
                yield visible
        raw_response, eval_count, done_reason = result
        if not raw_response:
            # cancelado antes del chunk final, o stream vacío: reconstruir
            # lo acumulado en el gate para no perderlo.
            raw_response = gate.raw_so_far()
        sink["raw"] = raw_response
        sink["eval_count"] = eval_count
        sink["done_reason"] = done_reason

    def _call_llm_two_pass(
        self,
        user_input: str,
        compacted_context: str,
        web_context: str,
        inject_dev_override: bool = False,
        metacognitive_context: str = "",
        target_model: Optional[str] = None,
        lang_override: Optional[str] = None,
        has_web_evidence: bool = False,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        RETENIDO SIN INVOCAR (arquitectura de modelo único, 2026-08-27):
        gpt-oss razona en su canal Harmony `analysis` nativo, así que
        imponerle un <thought> propio con reparto de `num_predict` no solo
        no ayuda — lo rompe (fuga analysis->response, y HTTP 500 "error
        parsing tool call" en 3/3 pruebas del PASO 0). run_turn/process_turn
        ya NO llaman a esta función; el modelo único se genera siempre por
        el carril lean de una sola pasada. Se conserva intacta como camino
        de rollback si algún día el modelo vuelve a ser uno sin canal de
        razonamiento nativo. Ver RESPONSE_MODEL y STEP0_HARMONY_FINDINGS.md.

        Genera la respuesta del turno en DOS llamadas HTTP secuenciales a
        Ollama en vez de una — reparte MECÁNICAMENTE el presupuesto de
        tokens entre razonamiento (`<thought>`) y respuesta visible
        mediante `num_predict` en cada llamada por separado, no una
        instrucción de prompt que el modelo puede decidir ignorar (ver
        `MemoryGovernor.split_budget` para el motivo, MEDIDO, de por qué
        hace falta: con el <thought> emitiéndose de forma confiable, un
        turno con contexto web se comía ~81% del presupuesto en el plan
        interno, dejando ~380 caracteres de respuesta visible).

        Devuelve `(raw_response, stats)`:
          - `raw_response` = "<thought>...</thought>\\n" + respuesta
            visible — MISMO formato que ya consume el resto del pipeline
            de una sola pasada (`_split_thought_and_content`,
            `_verify_thought_code`, WAL, sanitización léxica). Ningún
            llamador río abajo necesita saber que esto vino de dos
            llamadas, no una.
          - `stats`: dict con `reasoning_tokens`, `reasoning_budget`,
            `reasoning_done_reason`, `answer_tokens`, `answer_budget`,
            `answer_done_reason`, `thought_forced_close` (bool) — para
            instrumentar el reparto REAL contra el objetivo 60/40, no
            asumirlo.

        PRECONDICIÓN: solo tiene sentido para turnos NO-coder —
        CODER_SYSTEM_PROMPT no define el protocolo `<thought>` de 6
        pasos (ver `MemoryGovernor.split_budget`), así que no hay nada
        que repartir. Si `target_model` resuelve a un modelo coder,
        degrada a UNA sola llamada (mismo resultado que `_call_llm`) con
        advertencia — blindaje defensivo; el llamador NO debería
        alcanzar esta rama para turnos coder (ver la auditoría de call
        sites: los turnos coder siguen usando `_call_llm` directamente).

        Ambas pasadas comparten el MISMO `system` header (mismo `lang`,
        mismo `is_coder=False`, vía `_get_frozen_header` dentro de
        `_call_llm_raw`) — preserva la Optimización #1 (Prefix
        Alignment/KV-Cache) documentada en `MemoryGovernor`: el prefijo
        "system" enviado a Ollama es byte-idéntico entre ambas llamadas.
        """
        model = target_model or self.model
        is_coder = False  # modelo único: sin rol coder — ver RESPONSE_MODEL

        reasoning_budget, answer_budget = self._memory_governor.split_budget(
            is_coder, has_web_evidence=has_web_evidence
        )

        if is_coder:
            logger.warning(
                "⚠️ [TwoPass] _call_llm_two_pass llamado con un modelo coder (%s) — "
                "CODER_SYSTEM_PROMPT no tiene protocolo <thought>, degradando a una "
                "sola llamada. Revisar el call site: esta ruta no debería alcanzar "
                "turnos coder.",
                model,
            )
            single_prompt = self._build_reasoning_prompt(
                user_input, compacted_context, web_context, True,
                inject_dev_override, metacognitive_context, lang=lang_override,
            )
            text, eval_count, done_reason = self._call_llm_raw(
                single_prompt, target_model=target_model, lang_override=lang_override,
                has_web_evidence=has_web_evidence,
            )
            stats = {
                "reasoning_tokens": 0, "reasoning_budget": 0,
                "reasoning_done_reason": "n/a", "answer_tokens": eval_count,
                "answer_budget": answer_budget, "answer_done_reason": done_reason,
                "thought_forced_close": False,
            }
            return text, stats

        # ---------------- PASADA 1: razonamiento (<thought>) ----------------
        pass1_prompt = self._build_reasoning_prompt(
            user_input, compacted_context, web_context, True,
            inject_dev_override, metacognitive_context, lang=lang_override,
            include_final_answer_tail=False,
        )
        # Nota (bug, descubierto probando esta misma función):
        # SIN `stop` en </thought> a propósito. El SYSTEM_PROMPT exige
        # que el JSON de function-calling se emita "inmediatamente
        # después del cierre </thought>" - con un stop-sequence ahí, la
        # generación se corta exacto en la etiqueta y el modelo nunca
        # llega a escribir el JSON, rompiendo el function-calling por
        # completo para cualquier turno que pase por dos pasadas.
        # Verificado que esto es seguro: en NINGUNA de las corridas de
        # validación (turnos sin tool-call) el stop-sequence llegó a
        # dispararse - el modelo siempre se detenía solo, mucho antes
        # del techo (13-38% de `reasoning_budget` usado), así que
        # quitarlo no cambia el comportamiento del caso normal, solo
        # habilita el caso de herramienta.
        thought_raw, reasoning_tokens, reasoning_done_reason = self._call_llm_raw(
            pass1_prompt, target_model=target_model, lang_override=lang_override,
            has_web_evidence=has_web_evidence, num_predict_override=reasoning_budget,
        )

        if thought_raw.lstrip().startswith("[ERROR"):
            stats = {
                "reasoning_tokens": reasoning_tokens, "reasoning_budget": reasoning_budget,
                "reasoning_done_reason": reasoning_done_reason, "answer_tokens": 0,
                "answer_budget": answer_budget, "answer_done_reason": "n/a",
                "thought_forced_close": False,
            }
            return thought_raw, stats

        # CASO BORDE - turno de function-calling: el JSON de la
        # herramienta queda en la Pasada 1, junto al <thought>, nunca en
        # la Pasada 2 (esa es específicamente para prosa de respuesta al
        # usuario). `extract_tool_call` ya sabe aislar el <thought> antes
        # de buscar el JSON, así que detecta esto sea cual sea la forma
        # exacta en que el modelo lo estructuró - medido: para consultas
        # que disparan una herramienta, el modelo a veces omite el
        # <thought> narrativo por completo y escribe el JSON directo
        # (con un `</thought>` suelto después, sin abrir uno antes);
        # otras veces sigue el formato documentado en el SYSTEM_PROMPT
        # (<thought> completo, cierre, JSON). Ambas formas las resuelve
        # este único chequeo, reutilizando el parser ya existente en vez
        # de reinventar la detección.
        if self.extract_tool_call(thought_raw):
            stats = {
                "reasoning_tokens": reasoning_tokens, "reasoning_budget": reasoning_budget,
                "reasoning_done_reason": reasoning_done_reason, "answer_tokens": 0,
                "answer_budget": answer_budget, "answer_done_reason": "n/a",
                "thought_forced_close": False,
            }
            return thought_raw, stats

        if not self._THOUGHT_OPEN_RE.match(thought_raw):
            # El modelo nunca abrió el bloque (ni con <thought> ni con
            # [thought] - ver _THOUGHT_OPEN_RE) NI emitió una herramienta
            # (ya descartado arriba). Con la Pasada 1 ya SIN la cola de
            # estilo/idioma que lo suprimía (ver
            # `include_final_answer_tail=False` arriba), esto debería
            # ser raro - pero si ocurre, tratar la salida cruda como si
            # YA fuera la respuesta final es más seguro que fabricar una
            # estructura <thought> que nunca existió. Mismo criterio que
            # `resolve_visible_answer` usa para el caso simétrico ("sin
            # bloque de razonamiento no es este fallo").
            logger.warning(
                "⚠️ [TwoPass] Pasada 1 no abrió <thought> — tratando su salida como "
                "respuesta directa, sin Pasada 2."
            )
            stats = {
                "reasoning_tokens": reasoning_tokens, "reasoning_budget": reasoning_budget,
                "reasoning_done_reason": reasoning_done_reason, "answer_tokens": 0,
                "answer_budget": answer_budget, "answer_done_reason": "n/a",
                "thought_forced_close": False,
            }
            return thought_raw, stats

        thought_text, leaked_answer = self._split_pass1_leak(thought_raw, reasoning_done_reason)
        thought_forced_close = False

        if leaked_answer is not None:
            # La Pasada 1 cerró su <thought> y siguió con una respuesta
            # COMPLETA y terminada (cerró sola, no por techo de tokens) -
            # el modelo ya planificó Y ya redactó. Se usa TAL CUAL y se
            # OMITE la Pasada 2: sin esto, la Pasada 2 generaba una segunda
            # respuesta casi idéntica que terminaba DUPLICADA en pantalla,
            # y encima costaba una llamada HTTP entera a Ollama (backend
            # CPU, ~15 tok/s - ver `_default_num_thread`). El resto del
            # pipeline separa plan de respuesta igual que siempre, vía
            # `_split_thought_and_content` sobre el `raw_response`
            # `<thought>…</thought>\n<respuesta>` que se devuelve acá.
            logger.info(
                "ℹ️ [TwoPass] La Pasada 1 cerró </thought> y redactó una "
                "respuesta completa (%d car., done=%s) — se usa esa y se "
                "omite la Pasada 2.",
                len(leaked_answer), reasoning_done_reason,
            )
            stats = {
                "reasoning_tokens": reasoning_tokens, "reasoning_budget": reasoning_budget,
                "reasoning_done_reason": reasoning_done_reason,
                "answer_tokens": 0, "answer_budget": answer_budget,
                "answer_done_reason": "pass1_leak",
                "thought_forced_close": False,
            }
            return f"{thought_text}\n{leaked_answer}", stats

        if not self._THOUGHT_CLOSE_ANYWHERE_RE.search(thought_raw):
            # CASO BORDE documentado (medido, no hipotético): el modelo
            # completa el CONTENIDO del plan (los 6 pasos) pero no
            # siempre escribe la etiqueta de cierre LITERAL antes de su
            # propio EOS - ocurre incluso cuando reasoning_done_reason
            # == "stop" (el modelo se detuvo solo, no por el
            # stop-sequence ni por el techo). Se normaliza siempre que
            # falte la etiqueta; se advierte SOLO cuando de verdad tocó
            # el techo de tokens (done_reason == "length"), porque ahí
            # el plan puede haber quedado incompleto a mitad de un paso,
            # no solo sin la etiqueta de cierre.
            #
            # (Si el modelo sí cerró pero dejó una cola corta/truncada,
            # `_split_pass1_leak` ya recortó `thought_text` en ese cierre
            # y descartó la cola - no hay nada que forzar, y la Pasada 2
            # de abajo produce la respuesta real.)
            thought_forced_close = True
            if reasoning_done_reason == "length":
                logger.warning(
                    "⚠️ [TwoPass] Pasada 1 llegó al techo de %d tokens sin cerrar "
                    "</thought> — forzando cierre; el plan interno pudo quedar "
                    "incompleto.",
                    reasoning_budget,
                )
            thought_text = thought_text.rstrip() + "\n" + self._thought_close_tag_for(thought_text)

        # ---------------- PASADA 2: respuesta visible ----------------
        is_en = (lang_override or getattr(self, "current_language", "Spanish")) == "English"
        if is_en:
            pass2_instruction = (
                "You already planned this answer internally. This is your own plan:\n\n"
                f"{thought_text}\n\n"
                "Now write ONLY the final answer for the user, following your own "
                "plan. Do not repeat or summarize the plan, and do not emit another "
                "<thought> block.\n\n"
            )
        else:
            pass2_instruction = (
                "Ya planificaste internamente esta respuesta. Este es tu propio "
                f"plan:\n\n{thought_text}\n\n"
                "Ahora escribe ÚNICAMENTE la respuesta final para el usuario, "
                "siguiendo tu propio plan. No repitas ni resumas el plan, y no "
                "emitas otro bloque <thought>.\n\n"
            )
        pass2_prompt = (
            pass2_instruction
            + self._final_answer_instruction_tail(lang_override, include_thought_reminder=False)
            + ("Answer:" if is_en else "Respuesta:")
        )

        answer_text, answer_tokens, answer_done_reason = self._call_llm_raw(
            pass2_prompt, target_model=target_model, lang_override=lang_override,
            has_web_evidence=has_web_evidence, num_predict_override=answer_budget,
        )

        stats = {
            "reasoning_tokens": reasoning_tokens, "reasoning_budget": reasoning_budget,
            "reasoning_done_reason": reasoning_done_reason,
            "answer_tokens": answer_tokens, "answer_budget": answer_budget,
            "answer_done_reason": answer_done_reason,
            "thought_forced_close": thought_forced_close,
        }

        if answer_text.lstrip().startswith("[ERROR"):
            # La Pasada 2 falló pero la 1 sí produjo un plan real: se
            # devuelve el thought solo (mejor que nada) - el resto del
            # pipeline lo trata como si el modelo se hubiera "quedado"
            # en el thought, camino YA cubierto por `resolve_visible_
            # answer` (ver process_turn / StreamTurnWorker).
            return thought_text, stats

        raw_response = f"{thought_text}\n{answer_text}"
        return raw_response, stats

    def _final_lexical_safety_net(self, text: str) -> str:
        return self._lexical_guard.sanitize(text)

    # =================================================================
    # OPTIMIZACIÓN #2 - Caché Semántico de Respuesta Directa (0ms)
    # =================================================================
    def compute_query_embedding(self, text: str) -> Optional[List[float]]:
        """Envoltorio fino sobre embeddings.get_embedding con el endpoint
        y modelo de embeddings configurados para esta instancia. Descarta
        el modo: úsalo solo donde el vector sirva para ordenar candidatos
        por proximidad relativa (RAG), nunca para comparar la similitud
        contra un umbral absoluto — para eso está la variante _with_mode."""
        vector, _mode = self.compute_query_embedding_with_mode(text)
        return vector

    def compute_query_embedding_with_mode(
        self, text: str,
    ) -> Tuple[Optional[List[float]], str]:
        """
        Igual que `compute_query_embedding` pero devuelve `(vector, modo)`,
        donde modo es EMBEDDING_MODE_SEMANTIC (modelo de embeddings real)
        o EMBEDDING_MODE_HASH_FALLBACK (bag-of-words hasheado). Registra el
        último modo observado en `self._last_embedding_mode` para que un
        llamador que reutilice un vector ya calculado pueda recuperar con
        qué modo se produjo, y avisa una única vez por proceso cuando la
        instalación está degradada.
        """
        if not text or not text.strip():
            return None, EMBEDDING_MODE_UNAVAILABLE

        vector, mode = get_embedding_with_mode(text.strip())
        self._last_embedding_mode = mode

        if mode == EMBEDDING_MODE_HASH_FALLBACK and not self._embedding_fallback_warned:
            self._embedding_fallback_warned = True
            logger.warning(
                "⚠️ [Embeddings] El modelo local de embeddings no está disponible: los "
                "vectores vienen del fallback bag-of-words hasheado. El caché semántico "
                "queda restringido a coincidencia EXACTA y el RAG vectorial pierde "
                "precisión. Instalalo con: pip install fastembed",
            )

        return vector, mode

    @classmethod
    def _should_force_web_search(cls, requested: bool, decision: "RoutingDecision") -> bool:
        """
        BLINDAJE (bug real, MEDIDO — captura "hola, dime ecuaciones
        matematicas": qwen2.5:3b describió mal el principio de Arquímedes
        y llamó a la ecuación de Euler-Lagrange "una especie de primera
        ley de conservación" — ninguna de las dos cosas es correcta):
        SignalTag.FACTUAL_ENUMERATION nunca escala a slow_path
        (WEIGHT_FACTUAL_ENUMERATION=0.0, ver router.py — con razón:
        ningún dispatcher de slow_path sabe qué hacer con "enumerá N
        leyes de un dominio"). Pero el problema real no es falta de
        RAZONAMIENTO — es falta de HECHOS correctos en la memoria
        paramétrica de un modelo de 3B. Más tokens de "pensar" no
        inventan el dato correcto que el modelo nunca tuvo bien
        memorizado; una búsqueda web real sí puede. Por eso esta señal
        fuerza búsqueda web (como si el usuario hubiera tildado el botón
        🌐 manualmente) en vez de escalar a slow_path — reutiliza el
        pipeline de force_web_search que ya existe, no inventa uno
        nuevo. Esto también hace que la caché semántica se saltee para
        este turno (mismo `if not force_web_search:` en ambas rutas),
        evitando servir una respuesta vieja sin el grounding nuevo.

        Único punto de verdad para este criterio: run_turn y
        process_turn llaman a este mismo método, para que las dos rutas
        del pipeline nunca diverjan en cuándo forzar el grounding.
        """
        return bool(requested) or (SignalTag.FACTUAL_ENUMERATION in decision.tags)

    @classmethod
    def _semantic_cache_allowed(cls, decision: "RoutingDecision") -> bool:
        """
        BLINDAJE (bug real, MEDIDO — capturas "hola contaminada"): la
        caché semántica compara ÚNICAMENTE el embedding del texto de
        `user_input` contra entradas previas (ver `check_semantic_cache`
        / MemoryGraph.find_semantic_cache_hit, justo abajo) — el
        historial de conversación NUNCA es parte de la clave de caché.
        Para saludos triviales ("hola", "buenas", "hey") eso es
        especialmente peligroso: son cortos, casi idénticos entre sí en
        texto Y en embedding, y se repiten en CADA conversación nueva —
        así que un "hola" de hace días, con TTL de una semana
        (`semantic_cache_ttl_seconds`, por defecto 7*24*3600s), puede
        servirse tal cual como respuesta a un "hola" de una conversación
        totalmente distinta, arrastrando una respuesta que no tiene nada
        que ver con el turno actual.

        Único punto de verdad para esta exclusión: se consulta tanto
        antes de leer la caché (`check_semantic_cache`) como antes de
        escribir en ella (`store_semantic_cache_async`), en las dos
        rutas del pipeline (el generador de run_turn y process_turn),
        para que ninguna de las dos quede desincronizada entre sí ni con
        un futuro call site que se agregue más adelante y se olvide del
        guard.
        """
        return SignalTag.TRIVIAL_GREETING not in decision.tags

    def check_semantic_cache(
        self, user_input: str, query_embedding: Optional[List[float]] = None,
        embedding_mode: Optional[str] = None,
        decision: Optional["RoutingDecision"] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Busca en MemoryGraph.semantic_cache una consulta previa cuya
        similitud coseno con `user_input` sea >= self.semantic_cache_threshold.
        Si hay hit, devuelve {"response", "similarity", "model", ...} para
        que el llamador (process_turn / StreamTurnWorker) responda de
        inmediato sin invocar al modelo. None si no hay hit o si la
        caché semántica está deshabilitada.

        La búsqueda por similitud SOLO se usa con embeddings semánticos
        reales. Si el vector salió del fallback hash, se degrada a
        coincidencia exacta (`find_exact_cache_hit`): esa similitud no mide
        significado y es invariante al orden de las palabras, así que
        "¿es A mejor que B?" y "¿es B mejor que A?" puntúan 1.0 — ningún
        umbral, ni 0.99, evita servir la respuesta contraria a la que se
        pidió. Y este es el paso más temprano del turno (antes del router,
        del fuzzer adversarial y de cualquier grounding web), así que un
        acierto equivocado aquí sortea TODA la verificación posterior.

        Si se reutiliza un `query_embedding` ya calculado sin pasar su
        `embedding_mode`, se asume el último modo observado por esta
        instancia; a falta de él, se asume el caso degradado.

        Si se pasa `decision` (la RoutingDecision ya clasificada por el
        router para este turno) y trae SignalTag.TRIVIAL_GREETING, se
        salta la caché por completo (ver `_semantic_cache_allowed` — bug
        real de "hola contaminada"). `decision=None` preserva el
        comportamiento previo para cualquier llamador que todavía no lo
        pase.
        """
        if not self.semantic_cache_enabled:
            return None
        if decision is not None and not self._semantic_cache_allowed(decision):
            return None

        mode = embedding_mode
        if query_embedding is not None:
            embedding = query_embedding
            if mode is None:
                mode = self._last_embedding_mode or EMBEDDING_MODE_HASH_FALLBACK
        else:
            embedding, mode = self.compute_query_embedding_with_mode(user_input)

        if embedding is None:
            return None

        try:
            if mode != EMBEDDING_MODE_SEMANTIC:
                return self.memory_graph.find_exact_cache_hit(
                    user_input,
                    max_age_seconds=self.semantic_cache_ttl_seconds,
                )
            return self.memory_graph.find_semantic_cache_hit(
                embedding,
                threshold=self.semantic_cache_threshold,
                max_age_seconds=self.semantic_cache_ttl_seconds,
            )
        except Exception as exc:
            logger.debug("Caché semántico no disponible: %s", exc)
            return None

    def store_semantic_cache_async(
        self, user_input: str, response: str, model: str = "",
        query_embedding: Optional[List[float]] = None,
        decision: Optional["RoutingDecision"] = None,
    ) -> None:
        """
        Vectoriza (si hace falta) y persiste la respuesta ya entregada al
        usuario en segundo plano, en un hilo daemon — nunca bloquea el
        turno que ya se completó. Respuestas de error o vacías no se
        cachean, para no servir un fallo transitorio como si fuera la
        respuesta correcta a futuras consultas similares.

        Mismo guard que `check_semantic_cache`: si `decision` trae
        SignalTag.TRIVIAL_GREETING, no se persiste nada (ver
        `_semantic_cache_allowed`) — si no se guardara, tampoco haría
        falta bloquear la lectura, así que ambos lados de la caché deben
        aplicar el mismo criterio siempre.
        """
        if not self.semantic_cache_enabled:
            return
        if decision is not None and not self._semantic_cache_allowed(decision):
            return
        if not response or not response.strip() or response.lstrip().startswith("[ERROR"):
            return

        def _persist() -> None:
            try:
                embedding = query_embedding if query_embedding is not None else self.compute_query_embedding(user_input)
                if embedding is None:
                    return
                self.memory_graph.store_semantic_cache_entry(user_input, embedding, response, model)
            except Exception as exc:
                logger.debug("No se pudo persistir en caché semántico: %s", exc)

        threading.Thread(target=_persist, daemon=True, name="SemanticCachePersist").start()

    # =================================================================
    # Reflexión espontánea en segundo plano (opción pedida explícitamente:
    # "que pueda escribir cuando quiera o solo cuando se le hable")
    # =================================================================
    # Nota: esto NO modela que el sistema "quiera" hablar en ningún
    # sentido experiencial - es la misma arquitectura que cualquier otro
    # turno (una llamada más a _call_llm, con un prompt distinto), solo
    # que el disparador es un QTimer de sovnode_qt.py en vez de un
    # mensaje del usuario. El modelo recibe una instrucción explícita de
    # responder con un token exacto si no hay nada real que agregar - la
    # "decisión de hablar" es una salida de texto determinística sobre un
    # prompt, no un proceso de deseo o intención. Mismo principio de
    # honestidad que ya se sostuvo para qualia_interface_prototype/.
    #
    # No verificado en vivo en esta sesión (no hay Ollama en este
    # entorno de pruebas) - sí verificado con stubs: que no llama al LLM
    # sin historial previo, que reconoce el token de "nada que aportar",
    # y que limpia la respuesta con el mismo pipeline que un turno común
    # (ver sección de tests correspondiente).
    _NADA_QUE_APORTAR_TOKEN: str = "[SIN_APORTE]"

    def build_reflection_prompt(self, recent_history: List[str], lang: str) -> str:
        """
        Arma el prompt de la reflexión espontánea. Deliberadamente NO
        reutiliza `_build_reasoning_prompt` (pensado para turnos con una
        pregunta concreta del usuario) — acá no hay pregunta, hay
        historial reciente y una sola instrucción: decidir si aportar
        algo o no.
        """
        historial = "\n".join(recent_history) if recent_history else "(sin historial)"
        if lang == "English":
            return (
                "You are reviewing the conversation below DURING IDLE TIME — "
                "the user has not sent a new message, this is a periodic "
                "background check. Decide honestly if there is something "
                "genuinely worth telling them right now: a real follow-up "
                "thought, a caveat you missed earlier, noticing an unresolved "
                "thread. Do NOT manufacture small talk, filler, or busywork "
                "just to seem present — most of the time the right answer is "
                "that there is nothing to add. If there is nothing genuinely "
                f"useful, respond with EXACTLY this token and nothing else: "
                f"{self._NADA_QUE_APORTAR_TOKEN}\n\n"
                f"--- RECENT CONVERSATION ---\n{historial}\n--- END ---\n\n"
                "Your response (or the token above), nothing else:"
            )
        return (
            "Estás revisando la conversación de abajo EN TIEMPO OCIOSO — el "
            "usuario no mandó un mensaje nuevo, esto es un chequeo periódico "
            "de fondo. Decidí con honestidad si hay algo que valga la pena "
            "decirle ahora mismo: una idea de seguimiento real, una salvedad "
            "que se te pasó antes, notar un hilo sin resolver. NO inventes "
            "charla trivial ni relleno solo para parecer presente — la "
            "mayoría de las veces la respuesta correcta es que no hay nada "
            "que agregar. Si no hay nada genuinamente útil, respondé "
            f"EXACTAMENTE con este token y nada más: "
            f"{self._NADA_QUE_APORTAR_TOKEN}\n\n"
            f"--- CONVERSACIÓN RECIENTE ---\n{historial}\n--- FIN ---\n\n"
            "Tu respuesta (o el token de arriba), nada más:"
        )

    # Pedido explícito ("optimiza ese sistema... que consuma menos en
    # potencia de la PC"): un turno normal usa BASE_NUM_PREDICT (2048)
    # porque puede necesitar razonar y redactar una respuesta larga. Una
    # reflexión espontánea, en el caso esperado MÁS COMÚN, solo necesita
    # emitir el token de "nada que aportar" (unos pocos tokens); en el
    # caso menos común donde sí hay algo que decir, es un comentario
    # corto, no un ensayo - nunca hace falta el mismo techo. Bajar
    # num_predict acorta directamente la fase de generación (la más cara
    # en cómputo/energía de una inferencia local), sin importar si la
    # GPU/CPU del usuario es rápida o lenta.
    _REFLECTION_NUM_PREDICT_CAP: int = 220

    def generate_spontaneous_reflection(
        self,
        history_limit: int = 4,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """
        Punto de entrada único para el chequeo periódico de "¿hay algo
        que valga la pena decir sin que me hablen?". Devuelve el texto
        listo para mostrar, o None si no corresponde escribir nada (sin
        historial todavía, error de LLM, o el modelo devolvió el token
        de "nada que aportar"). El llamador (sovnode_qt.py) es responsable
        de no invocar esto mientras haya un turno normal en curso, y de
        no invocarlo más de una vez por período de inactividad (ver
        BLINDAJE de consumo en sovnode_qt.py::_maybe_run_reflection).

        BLINDAJE (pedido explícito de optimización de recursos):
        `history_limit` bajó de 8 a 4 turnos — menos texto de entrada,
        menos tokens de prefill por chequeo, sin perder la señal (4
        turnos alcanzan de sobra para notar "algo quedó sin resolver").
        Y esta función llama a `_call_llm_raw` DIRECTAMENTE (no al
        envoltorio `_call_llm`) para poder pasarle `num_predict_override`
        (techo de generación mucho más bajo que un turno real, ver
        `_REFLECTION_NUM_PREDICT_CAP`) y `keep_alive_override="0"` (que
        el modelo se descargue apenas responde, en vez de quedar
        cargado en RAM/VRAM otros 30 minutos por un chequeo que ni
        siquiera pidió el usuario).
        """
        try:
            recent_history = self.memory_graph.get_recent_history(limit=history_limit)
        except Exception as exc:
            logger.debug("Reflexión espontánea: no se pudo leer el historial: %s", exc)
            return None

        if not recent_history:
            return None  # sesión recién abierta, sin nada de qué reflexionar

        lang = getattr(self, "current_language", "Spanish") or "Spanish"
        prompt = self.build_reflection_prompt(recent_history, lang)

        try:
            raw, _eval_count, _done_reason = self._call_llm_raw(
                prompt,
                temperature_override=0.3,
                num_predict_override=self._REFLECTION_NUM_PREDICT_CAP,
                keep_alive_override="0",
                lang_override=lang,
                log_cb=log_cb,
                perf_label="Reflexion",
            )
        except Exception as exc:
            logger.debug("Reflexión espontánea: fallo la llamada al LLM: %s", exc)
            return None

        if not raw or raw.lstrip().startswith("[ERROR"):
            return None

        _, clean = self._split_thought_and_content(raw)
        clean = clean if clean is not None else raw
        clean, _ = self._strip_leaked_reasoning(clean)
        # Nota: ver `_strip_system_prompt_echo` - `_call_llm_raw` de
        # arriba manda el mismo header congelado que cualquier otra
        # llamada (`[CRITICAL LANGUAGE RULE]` incluido), así que esta
        # reflexión hereda el mismo riesgo de eco que una respuesta
        # normal, pese a no tener usuario esperando del otro lado.
        clean, _ = self._strip_system_prompt_echo(clean)
        clean = clean.strip()

        if not clean or self._NADA_QUE_APORTAR_TOKEN in clean:
            return None
        if len(clean) < 12:
            # Respuesta irrisoriamente corta: más probable que sea un eco
            # roto del prompt que un aporte real - mismo criterio de
            # cautela que el resto del pipeline usa contra basura.
            return None

        clean, _ = self._dedupe_enumeration_items(clean)

        # Persistencia acá adentro (no en sovnode_qt.py): mismo principio
        # que el resto del archivo ("Orchestrator no depende de Qt en
        # absoluto") - la capa de presentación solo debe mostrar el
        # texto, no decidir cómo se guarda. Si falla, no debe tirar abajo
        # el mensaje que el usuario ya está por ver.
        try:
            self.memory_graph.store_turn(str(uuid.uuid4()), "assistant", clean)
        except Exception as exc:
            logger.debug("Reflexión espontánea: no se pudo persistir en memory_graph: %s", exc)

        return clean

    # =================================================================
    # MEJORA DE RENDIMIENTO #1 - Dynamic Context Trimming & Token Budgeting
    # =================================================================
    _CHARS_PER_TOKEN_ESTIMATE: int = 4
    # Reserva de tokens para la GENERACION dentro del presupuesto de
    # recorte. Debe seguir a BASE_NUM_PREDICT (2048): si se queda corta,
    # _trim_context_to_budget mete mas contexto del que cabe y el prompt
    # + la generacion desbordan num_ctx. No la subas a 3072 (el techo del
    # coder) sin subir tambien MAX_NUM_CTX: con 6144 el contexto
    # disponible colapsaria al piso de 256 tokens.
    _GENERATION_TOKEN_RESERVE: int = 2048

    # =====================================================================
    # Selección de historial por decaimiento exponencial + relevancia
    # =====================================================================
    # Reemplaza, específicamente para el bloque de HISTORIAL, el truncado
    # ciego por caracteres que hacía _trim_context_to_budget (más abajo)
    # antes de esta integración. Motivo concreto: get_recent_history()
    # devuelve los turnos en orden cronológico ascendente (más viejo
    # primero), se unían con "\n".join(...), y el truncado original
    # cortaba con `text[:budget]` - es decir, conservaba el PRINCIPIO de
    # esa cadena ya unida (los turnos MÁS VIEJOS) y descartaba el final
    # (los turnos MÁS RECIENTES) cuando no entraba todo. Exactamente al
    # revés de lo deseable: el turno más reciente es casi siempre el más
    # relevante para la respuesta actual. Este motor selecciona por
    # relevancia turno por turno antes de unir, así que el problema no
    # llega a producirse; _trim_context_to_budget se ajustó además para
    # conservar el final (no el principio) si aun así necesita recortar
    # más (ver `keep_tail` en su `_trim` interno).
    _CONTEXT_STOPWORDS: frozenset = frozenset({
        "el", "la", "los", "las", "un", "una", "unos", "unas", "y", "o", "pero",
        "de", "del", "a", "ante", "con", "en", "para", "por", "que", "es", "son",
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "is",
    })

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)

    @classmethod
    def _extract_context_keywords(cls, text: str) -> Set[str]:
        words = re.findall(r"\b\w+\b", text.lower())
        return {w for w in words if w not in cls._CONTEXT_STOPWORDS and len(w) > 2}

    @classmethod
    def _trim_context_exponential_decay(
        cls,
        messages: List[Dict[str, str]],
        token_limit: int,
        decay_lambda: float = 0.12,
        query_text: str = "",
    ) -> List[Dict[str, str]]:
        """
        Selecciona qué turnos de `messages` conservar dentro de
        `token_limit`, ponderando cada uno por dos factores:
        - `time_factor`: decaimiento exponencial por distancia al turno
          más reciente (los turnos viejos pesan cada vez menos).
        - `keyword_factor`: bonus si el turno comparte palabras clave con
          `query_text` (la consulta actual) — así un turno viejo pero
          temáticamente relevante puede ganarle a uno reciente e
          irrelevante, algo que un truncado puramente cronológico nunca
          puede capturar.
        El último mensaje SIEMPRE se conserva completo (es la consulta
        actual): se reserva su presupuesto antes de puntuar el resto.

        Nota de complejidad: el docstring original de referencia la
        describía como O(N); en rigor no lo es — hay un `sort()` sobre
        los candidatos, que es O(N log N) — pero se deja así a propósito:
        N aquí es la cantidad de turnos de historial reciente
        (`get_recent_history(limit=...)`, típicamente ≤ 8 en este
        proyecto), así que la diferencia entre O(N) y O(N log N) es
        cero en la práctica. Reemplazar el `sort()` por una selección
        basada en heap (`heapq.nlargest`) bajaría la constante para N
        grande, pero sería complejidad añadida sin ningún beneficio
        medible para listas de historial de un puñado de turnos —
        optimización prematura que no vale la pena aquí.
        """
        if not messages:
            return []

        n = len(messages)
        query_keywords = cls._extract_context_keywords(query_text) if query_text else set()

        last_msg = messages[-1]
        last_tokens = cls._estimate_tokens(last_msg.get("content", ""))
        budget_remaining = max(0, token_limit - last_tokens)

        scored_candidates = []
        for idx, msg in enumerate(messages[:-1]):
            content = msg.get("content", "")
            tokens = cls._estimate_tokens(content)

            distance = (n - 1) - idx
            time_factor = math.exp(-decay_lambda * distance)

            msg_keywords = cls._extract_context_keywords(content)
            if query_keywords and msg_keywords:
                overlap = len(query_keywords & msg_keywords)
                keyword_factor = 1.0 + (overlap / max(1, len(query_keywords)))
            else:
                keyword_factor = 1.0

            score = time_factor * keyword_factor
            scored_candidates.append((idx, msg, tokens, score))

        scored_candidates.sort(key=lambda c: c[3], reverse=True)

        selected_indices = []
        current_tokens = 0
        for idx, msg, tokens, score in scored_candidates:
            if current_tokens + tokens <= budget_remaining:
                selected_indices.append(idx)
                current_tokens += tokens

        selected_indices.sort()  # de vuelta al orden cronológico original
        pruned_history = [messages[i] for i in selected_indices]
        pruned_history.append(last_msg)
        return pruned_history

    # Nota (medido, turno real "dime las ecuaciones mas importantes
    # de la fisica", 2026-08-25): reconstruyendo el prompt exacto que
    # vio ese turno contra `_build_reasoning_prompt`/`_get_base_system_
    # prompt` reales (no una reimplementación), la cabecera "system"
    # pesó 2706 tokens (66% del total) y el resto del prompt 1378
    # tokens (34%) - de esos 1378, el historial conversacional
    # (`compacted_context`, apenas 4 filas = 2 intercambios) ya se
    # llevaba 1062 tokens (77% del prompt, 26% del turno completo),
    # casi todo por incluir el texto COMPLETO de 2 respuestas
    # anteriores, obligadas a ser largas por `_FINAL_ANSWER_STYLE_ES/
    # EN`, sin ningún recorte. El total reconstruido (~4084 tok,
    # heurístico 4 char/tok) coincidió con el prefill REAL reportado
    # por consola (4233 tok) dentro de un 3.6% - confirma que esto no
    # es un misterio de caché de Ollama sin medir: es, casi en su
    # totalidad, tokens crudos que se reprocesan turno a turno.
    #
    # El historial no necesita el texto entero de cada turno pasado
    # para cumplir su función (que el modelo sepa de qué se venía
    # hablando) - un extracto acotado alcanza para eso, y cuesta una
    # fracción. El corte es una decisión de compromiso, no un número
    # medido como "óptimo": bastante generoso como para no cortar a
    # mitad del primer punto de una respuesta numerada típica (~800
    # caracteres cubre normalmente el primer punto completo más el
    # arranque del segundo), bastante chico como para que 4-8 filas de
    # historial no vuelvan a pesar más que el resto del prompt junto.
    # Si un turno de seguimiento necesita un detalle que quedó
    # recortado, el usuario puede repetirlo - ese costo puntual es
    # mucho menor que pagar el historial completo en cada turno. El
    # marcador " [...]" se agrega SOLO cuando de verdad se cortó algo,
    # para que el propio modelo sepa que ese turno de historial está
    # incompleto (mismo espíritu que el resto de los avisos explícitos
    # de esta clase - ver `thin_context_reminder` - en vez de
    # presentarle un fragmento cortado como si fuera la respuesta
    # completa).
    HISTORY_ENTRY_CHAR_CAP: int = 800

    @classmethod
    def _truncate_history_entries(cls, entries: List[str]) -> List[str]:
        cap = cls.HISTORY_ENTRY_CHAR_CAP
        truncated = []
        for entry in entries:
            # Bug real, medido: un turno viejo guardado como "[ERROR]
            # Ollama devolvió el código HTTP 404" (de antes de que
            # run_turn/process_turn dejaran de persistir errores como
            # respuesta real) seguía reapareciendo como "historial
            # reciente" en turnos nuevos - el modelo terminaba
            # razonando sobre ese error en vez de responder al mensaje
            # actual. Se descarta acá, no solo en el punto de guardado,
            # para cubrir también filas viejas ya existentes en la DB.
            if "[ERROR" in entry:
                continue
            if len(entry) > cap:
                truncated.append(entry[:cap].rstrip() + " [...]")
            else:
                truncated.append(entry)
        return truncated

    def _trim_context_to_budget(
        self,
        user_input: str,
        compacted_context: str,
        web_context: str,
        metacognitive_context: str,
    ) -> Tuple[str, str, str]:
        max_chars = self._memory_governor.MAX_NUM_CTX * 4
        fixed_chars = len(user_input) + 1500

        if len(compacted_context) + len(web_context) + len(metacognitive_context) + fixed_chars <= max_chars:
            return compacted_context, web_context, metacognitive_context

        available = max(500, max_chars - fixed_chars)
        
        web_budget = int(available * 0.50)
        conv_budget = int(available * 0.30)
        meta_budget = int(available * 0.20)

        trimmed_web = web_context[:web_budget] if len(web_context) > web_budget else web_context
        trimmed_conv = compacted_context[-conv_budget:] if len(compacted_context) > conv_budget else compacted_context
        trimmed_meta = metacognitive_context[:meta_budget] if len(metacognitive_context) > meta_budget else metacognitive_context

        return trimmed_conv, trimmed_web, trimmed_meta

    # =================================================================
    # GENIALIDAD #2 - Meta-Cognición Histórica (RAG de lecciones del WAL)
    # =================================================================
    def _fetch_metacognitive_lessons(self, query: str, limit: int = 2) -> str:
        """
        Consulta MemoryGraph.reasoning_lessons buscando turnos que antes
        terminaron en error, o parches que CognitiveGovernor auto-sugirió
        tras analizar un fallo, léxicamente relacionados con `query`.
        Se llama solo al entrar al SLOW_PATH (ver process_turn): es una
        consulta SQLite/FTS5, no una llamada al modelo, así que es barata,
        pero no tiene sentido pagarla en el FAST_PATH de alta frecuencia.
        """
        try:
            lessons = self.memory_graph.fetch_reasoning_lessons(query, limit=limit)
        except Exception as exc:
            logger.debug("Consulta de lecciones meta-cognitivas omitida: %s", exc)
            return ""

        if not lessons:
            return ""

        lines = ["[MEMORIA META-COGNITIVA — ERRORES Y LECCIONES PASADAS A EVITAR]"]
        for lesson in lessons:
            tag = "ERROR PREVIO" if lesson.get("outcome") == "error" else "PARCHE AUTO-SUGERIDO"
            lines.append(f"- ({tag}) {str(lesson.get('content', ''))[:400]}")
        return "\n".join(lines)

    # =================================================================
    # GENIALIDAD #1 - Scratchpad Verificador (<thought_code> en sandbox)
    # =================================================================
    def _verify_thought_code(self, response_text: str) -> str:
        """
        Busca bloques <thought_code> en la respuesta cruda del modelo
        (todavía SIN separar el <thought>), los ejecuta en
        ExecutionSandbox — el mismo motor que ya usa SLOW_PATH_SANDBOX
        para bloques de código explícitos del usuario, con auditoría AST
        previa y subprocess aislado — y devuelve el bloque de evidencia
        formateado listo para reinyectarse en un prompt de seguimiento.
        Nunca lanza: ante cualquier fallo degrada a "" (sin verificación),
        y el turno continúa con la respuesta original sin cambios.
        """
        try:
            blocks = extract_thought_code_blocks(response_text)
            if not blocks:
                return ""

            results = []
            for code in blocks:
                sandbox_result = self._sandbox.run(code)
                results.append((code, sandbox_result))
                logger.info("🧪 [Scratchpad] Verificación en sandbox: %s", sandbox_result)

            return format_sandbox_verification(results)
        except Exception as exc:
            logger.warning("Fallo verificando <thought_code>: %s", exc)
            return ""

    # =================================================================
    # GENIALIDAD #3 - Tree-of-Thoughts ligero (temperaturas alternadas)
    # =================================================================
    def _tree_of_thought_reasoning(
        self,
        prompt: str,
        active_model: str,
        context_chars: int = 0,
        lang: Optional[str] = None,
        has_web_evidence: bool = False,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> Tuple[str, float]:
        """
        Reservado a TurnOutcome.SLOW_PATH_GENERIC_REASONING — la ruta que
        el router ya reserva para las consultas más ambiguas/complejas, así
        que es donde el costo de 3 llamadas secuenciales al modelo se
        justifica. Genera dos borradores independientes con temperaturas
        opuestas:
            Rama A: temperature=0.2 (lógica deductiva estricta)
            Rama B: temperature=0.7 (análisis lateral / casos borde)
        y una síntesis final que compara ambas y redacta la respuesta
        definitiva. Las tres llamadas pasan por _call_llm(), así que las
        tres respetan _llm_lock igual que cualquier otra inferencia —
        se ejecutan en SECUENCIA, nunca en paralelo, para no competir por
        la misma GPU/VRAM que el chat del usuario y el CognitiveGovernor.

        Devuelve (respuesta_final, acuerdo_entre_ramas) — el segundo valor
        es una similitud léxica [0..1] entre ambos borradores (difflib,
        sin costo de otra llamada al modelo) que alimenta la Genialidad #5
        (Métrica de Confianza Compuesta): dos ramas que convergen solas
        son una señal de mayor confianza que dos que divergen mucho.

        Rama A y Rama B se DESPACHAN de forma concurrente (dos hilos, vía
        ThreadPoolExecutor) en vez de llamarse una tras otra en el hilo
        principal. ADVERTENCIA DE DISEÑO: `_call_llm()` mantiene
        `self._llm_lock` tomado durante TODA la llamada (incluida la
        petición HTTP completa a Ollama) precisamente para no competir
        por la misma GPU/VRAM — así que, con la configuración por
        defecto de Ollama (sin `OLLAMA_NUM_PARALLEL` > 1), ambos hilos
        igual terminan sirializados esperando el mismo lock, y esto por
        sí solo NO reduce la latencia de pared. Sí evita cualquier
        oportunidad perdida de solapamiento en trabajo Python previo a
        la llamada (serialización de payload, etc.) y deja el código
        listo para beneficiarse de inmediato si el usuario habilita
        inferencia paralela real en Ollama — sin volver a tocar esta
        función.
        """
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ToT-Branch") as branch_pool:
            future_a = branch_pool.submit(
                self._call_llm, prompt, target_model=active_model,
                extra_context_chars=context_chars, temperature_override=0.2,
                lang_override=lang, has_web_evidence=has_web_evidence,
                log_cb=log_cb, perf_label="ToT-A",
            )
            future_b = branch_pool.submit(
                self._call_llm, prompt, target_model=active_model,
                extra_context_chars=context_chars, temperature_override=0.7,
                lang_override=lang, has_web_evidence=has_web_evidence,
                log_cb=log_cb, perf_label="ToT-B",
            )
            branch_a_raw = future_a.result()
            branch_b_raw = future_b.result()

        _, branch_a_clean = self._split_thought_and_content(branch_a_raw)
        _, branch_b_clean = self._split_thought_and_content(branch_b_raw)
        branch_a = branch_a_clean or branch_a_raw
        branch_b = branch_b_clean or branch_b_raw

        agreement = difflib.SequenceMatcher(
            None, branch_a.lower().strip(), branch_b.lower().strip()
        ).ratio()

        # Si alguna rama falló abiertamente, no tiene sentido "sintetizar"
        # entre una respuesta real y un mensaje de error: se usa la que sí
        # sirve directamente y se marca acuerdo neutro.
        a_failed = branch_a.lstrip().startswith("[ERROR")
        b_failed = branch_b.lstrip().startswith("[ERROR")
        if a_failed and b_failed:
            return branch_a_raw, 0.0
        if a_failed:
            return branch_b_raw, 0.5
        if b_failed:
            return branch_a_raw, 0.5

        synthesis_prompt = (
            "Combina la mejor información de estas dos versiones para responder la pregunta del usuario.\n\n"
            f"--- OPCIÓN 1 ---\n{branch_a}\n\n"
            f"--- OPCIÓN 2 ---\n{branch_b}\n\n"
            "REGLAS STRICTAS:\n"
            "1. Redacta la respuesta final directamente al usuario.\n"
            "2. PROHIBIDO usar las palabras 'borrador', 'opción', 'versión', 'borrador A' o 'borrador B'.\n"
            "3. NO expliques qué borrador es mejor, solo entrega la respuesta sintetizada.\n"
            # 4 es la UNICA regla de estilo que se agrega en toda la cadena de
            # ToT: esta tercera llamada NO es un prompt de correccion interna,
            # es la que produce la respuesta VISIBLE del SLOW_PATH. Sin ella,
            # la sintesis colapsaba las dos ramas en un resumen mas corto que
            # cualquiera de los dos borradores, anulando el efecto del bloque
            # de estilo en las ramas A y B. Las reglas 1-3 (anti-metatexto) se
            # conservan intactas: acotan el VOCABULARIO, no la extension.
            "4. Desarrolla la respuesta: explica el porqué además del qué, da contexto y "
            "ejemplos concretos cuando ayuden, y estructúrala en varios párrafos o listas "
            "si el tema lo amerita. No te limites a la conclusión."
        )
        # Split mecánico (ver `MemoryGovernor.split_budget`), pero SOLO
        # la mitad "respuesta": esta llamada de síntesis no genera su
        # propio <thought> - las ramas A/B YA cumplieron el papel de
        # "razonamiento" (cada una con su propia llamada completa) - así
        # que aplicar el split 60/40 aquí no tiene con qué dividir; lo
        # único que corresponde es acotar esta llamada al presupuesto de
        # RESPUESTA, no al total (`BASE_NUM_PREDICT` completo), para que
        # la síntesis reciba el mismo techo mecánico que cualquier otra
        # respuesta visible del turno.
        _is_coder_synthesis = False  # modelo único: sin rol coder
        _, synthesis_budget = self._memory_governor.split_budget(
            _is_coder_synthesis, has_web_evidence=has_web_evidence
        )
        synthesis, synthesis_tokens, synthesis_done_reason = self._call_llm_raw(
            synthesis_prompt, target_model=active_model,
            extra_context_chars=len(branch_a) + len(branch_b),
            lang_override=lang, has_web_evidence=has_web_evidence,
            num_predict_override=synthesis_budget,
            log_cb=log_cb, perf_label="ToT-Synthesis",
        )
        logger.info(
            "📊 [TwoPass/ToT-Synthesis] respuesta=%d/%d tok (%.0f%%) done=%s",
            synthesis_tokens, synthesis_budget,
            100 * synthesis_tokens / max(1, synthesis_budget), synthesis_done_reason,
        )

        if synthesis and not synthesis.lstrip().startswith("[ERROR"):
            return self._strip_tot_metatext(synthesis), agreement
        fallback = branch_a_raw if len(branch_a) >= len(branch_b) else branch_b_raw
        return self._strip_tot_metatext(fallback), agreement

    # Cualquier oración que mencione "borrador"/"draft" es, casi con
    # certeza, metatexto sobre el proceso interno de ToT filtrándose a la
    # respuesta visible - nunca vocabulario legítimo de una respuesta real
    # dirigida al usuario. Red de seguridad determinista para cuando un
    # modelo de 3B ignora las reglas explícitas del synthesis_prompt.
    _TOT_METATEXT_TRIGGER_RE: Pattern[str] = re.compile(r"\bborrador(?:es)?\b|\bdraft\b", re.IGNORECASE)

    # =================================================================
    # CORRECCIÓN EN ORCHESTRATOR.PY
    # =================================================================

    # =================================================================
    # CORRECCIÓN EN ORCHESTRATOR.PY
    # =================================================================

    @classmethod
    def _strip_tot_metatext(cls, text: str) -> str:
        """
        Elimina metadatos y bloques de pensamiento interno (ToT)
        sin alterar el contenido legítimo generado por el usuario.
        """
        if not text:
            return ""

        # 1. Eliminar bloques de razonamiento en cualquiera de sus 4
        # variantes (angular/corchetes, con o sin "_code")
        _, text = cls._split_thought_and_content(text)

        # 2. Eliminar cabeceras o metadatos de sistema con sintaxis fija de anclaje
        text = re.sub(r'(?m)^\[SYSTEM_METADATA:.*?\]$', '', text)
        text = re.sub(r'(?m)^\[TOT_REASONING_START\].*?\[TOT_REASONING_END\]$', '', text)

        # 3. Limpieza de saltos de línea residuales tras la remoción
        text = re.sub(r'\n{3,}', '\n\n', text)

        return text.strip()
    _DRIFT_CONCLUSION_RE: Pattern[str] = re.compile(
        r"(?:la\s+respuesta\s+es|la\s+conclusi[oó]n\s+es|por\s+lo\s+tanto[,:]?\s*"
        r"|entonces\s+la\s+respuesta\s+es|el\s+resultado\s+es|en\s+resumen[,:]?\s*)"
        r"[:\-]?\s*(.+)",
        re.IGNORECASE,
    )

    def _detect_epistemic_drift(self, thought: str, visible_response: str) -> bool:
        """
        Heurística determinista y barata (CERO llamadas al modelo) que
        compara la conclusión explícita del <thought> (si el modelo se
        comprometió con una, p. ej. "la respuesta es X") contra la
        apertura de la respuesta visible. Si el <thought> fijó una
        conclusión concreta y sus términos clave NO aparecen de ninguna
        forma en lo que el modelo redactó, hay deriva epistémica: planeó
        una cosa y escribió otra.

        Deliberadamente conservador: si el <thought> no contiene un patrón
        de conclusión explícito, no hay nada que comparar y se asume que
        no hay deriva (evita falsos positivos sobre razonamiento abierto).
        """
        if not thought or not visible_response:
            return False

        match = self._DRIFT_CONCLUSION_RE.search(thought)
        if not match:
            return False

        planned_conclusion = match.group(1).strip(" .:\n")[:120]
        if len(planned_conclusion) < 2:
            return False

        visible_head = " ".join(re.split(r"(?<=[.!?])\s+", visible_response.strip())[:2])

        planned_tokens = {t for t in re.findall(r"\w{4,}", planned_conclusion.lower())}
        visible_tokens = {t for t in re.findall(r"\w{4,}", visible_head.lower())}
        if not planned_tokens:
            return False

        overlap_ratio = len(planned_tokens & visible_tokens) / len(planned_tokens)
        return overlap_ratio < 0.2

    # =================================================================
    # VALIDADOR DE ESQUEMA DE RAZONAMIENTO (nueva funcionalidad):
    # complementa a _detect_epistemic_drift() de arriba, que compara el
    # <thought> contra la RESPUESTA final - esto valida la COHERENCIA
    # INTERNA del propio <thought> antes de que su plan se ejecute
    # (herramientas) o se redacte la respuesta a partir de él.
    # Deliberadamente determinista (regex, cero llamadas al modelo salvo
    # cuando sí encuentra un problema real) - mismo principio de costo
    # que _detect_epistemic_drift y el resto de los chequeos "gratuitos"
    # del pipeline (build_year_mismatch_warning, build_source_
    # contradiction_warning): auditar la ESTRUCTURA de un texto ya
    # generado es determinista; solo la CORRECCIÓN, cuando hace falta,
    # paga una llamada real.
    # =================================================================
    _THOUGHT_CITATION_RE: Pattern[str] = re.compile(
        r"\[\d+\]|fuente\s*(?:n[uú]mero\s*)?\d|seg[uú]n\s+la\s+fuente|"
        r"according to source|per source|source\s*\[?\d",
        re.IGNORECASE,
    )

    def _validate_thought_schema(
        self, thought_text: str, has_web_context: bool
    ) -> List[str]:
        """
        Audita la coherencia estructural de UN bloque <thought> ya
        generado. Devuelve una lista de problemas detectados (vacía si
        no hay ninguno) — deliberadamente conservador, con solo dos
        chequeos de ALTA confianza (bajo riesgo de falso positivo) en
        vez de exigir que seguir el protocolo de 6 pasos al pie de la
        letra, formato que un modelo local de 3B/7B no siempre respeta
        aunque su razonamiento sea válido:

        1. <thought> prácticamente vacío: el modelo abrió el bloque
           obligatorio pero no llegó a razonar nada real dentro — una
           señal fuerte de que se saltó el paso, no una elección
           legítima de estilo.
        2. Cita/parafrasea fuentes web (números de cita "[1]", "según
           la fuente"...) cuando este turno NO tuvo contexto web real
           — una alucinación estructural concreta: no puede haber
           parafraseado fuentes que nunca existieron (ver el paso 3 —
           checklist de comprensión por fuente — del protocolo
           <thought>, que el propio SYSTEM_PROMPT instruye omitir por
           completo cuando no hay contexto web).
        """
        problems: List[str] = []
        text = (thought_text or "").strip()
        if not text:
            return problems

        if len(text) < 15:
            problems.append(
                "El bloque <thought> es prácticamente vacío — no hay razonamiento real que auditar."
            )

        if not has_web_context and self._THOUGHT_CITATION_RE.search(text):
            problems.append(
                "El <thought> parafrasea o cita fuentes web (p. ej. \"[1]\", \"según la fuente\") "
                "pero este turno NO tuvo contexto web real — son fuentes inexistentes."
            )

        return problems

    def build_thought_schema_correction_note(
        self, problems: List[str], lang: Optional[str] = None
    ) -> str:
        """Nota correctiva breve para inyectar en el prompt de corrección cuando `_validate_thought_schema` detecta inconsistencias."""
        problems_str = "\n".join(f"- {p}" for p in problems)
        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"
        if is_en:
            return (
                "[REASONING SCHEMA WARNING]: Your internal <thought> plan has structural "
                f"problems detected automatically:\n{problems_str}\n"
                "Correct your final answer accordingly — do not reference sources or evidence "
                "that were never actually provided this turn."
            )
        return (
            "[AVISO DE ESQUEMA DE RAZONAMIENTO]: Tu plan interno <thought> tiene problemas "
            f"estructurales detectados automáticamente:\n{problems_str}\n"
            "Corrige tu respuesta final en consecuencia — no referencies fuentes o evidencia "
            "que en realidad nunca se proveyeron en este turno."
        )

    def _compute_confidence_score(
        self,
        web_context_used: bool,
        web_evidence_scores: List[float],
        tot_used: bool,
        tot_agreement: float,
        epistemic_drift_detected: bool,
        thought_code_verified: bool,
    ) -> Tuple[float, str]:
        """
        Elección libre de la 5ª genialidad: en vez de otra fuente de
        evidencia aislada, esta sintetiza —SIN ninguna llamada adicional al
        modelo— las señales que las genialidades 1, 2, 3 y 4 de ESTE MISMO
        turno ya calcularon, en un único puntaje de confianza visible en
        TurnTrace:
            - Densidad de evidencia web: promedio del score de reputación
              de dominio (web_search.score_domain) de las fuentes usadas.
            - Acuerdo del Tree-of-Thoughts: ramas que convergen solas suben
              la confianza; ramas muy divergentes la bajan.
            - Bono si una afirmación numérica se verificó de verdad en
              sandbox (Genialidad #1) en vez de asumirse.
            - Penalización si hubo que corregir deriva epistémica
              (Genialidad #4): la respuesta final es una corrección de
              emergencia, no el razonamiento original coherente.
        """
        score = 0.55  # base neutral: sin evidencia web ni ToT, confianza media

        if web_context_used:
            if web_evidence_scores:
                avg_domain_score = sum(web_evidence_scores) / len(web_evidence_scores)
                # normaliza aprox. el rango típico de score_domain [0.4 .. 2.0] a un aporte [0 .. 0.3]
                score += max(0.0, min(0.3, (avg_domain_score - 0.6) * 0.25))
            else:
                score += 0.05

        if tot_used:
            score += (tot_agreement - 0.5) * 0.3

        if thought_code_verified:
            score += 0.1

        if epistemic_drift_detected:
            score -= 0.25

        score = max(0.0, min(1.0, score))
        label = "alta" if score >= 0.7 else ("media" if score >= 0.4 else "baja")
        return score, label

    def process_turn(self, user_input: str, force_web_search: bool = False) -> TurnTrace:
        turn_id = str(uuid.uuid4())
        start_time = time.time()
        self._is_processing_turn = True
        self._pause_governor_event.set()

        try:
            self._wal.append_user_input(turn_id, user_input)
            decision = self._classify_turn(user_input)
            active_model = self._select_model_for_decision(decision)

            # Modelo único: sin rol coder — ver RESPONSE_MODEL y sección 23.
            is_coder = False
            effective_lang = self._resolve_turn_language(user_input)

            # ver la nota en _should_force_web_search: FACTUAL_ENUMERATION
            # fuerza grounding web real (medido - captura "hola,
            # dime ecuaciones matematicas").
            force_web_search = self._should_force_web_search(force_web_search, decision)

            if not force_web_search and self.semantic_cache_enabled:
                cached = self.check_semantic_cache(user_input, decision=decision)
                if cached:
                    resp_text = cached["response"]
                    self.memory_graph.store_turn(turn_id, "user", user_input)
                    self.memory_graph.store_turn(str(uuid.uuid4()), "assistant", resp_text)
                    self._wal.append_response(turn_id, resp_text, outcome="semantic_cache")

                    return TurnTrace(
                        turn_id=turn_id,
                        user_input=user_input,
                        routing_decision=decision,
                        outcome=TurnOutcome.FAST_PATH_DIRECT,
                        engine_results=[],
                        web_context_used=False,
                        knowledge_node_persisted=False,
                        logical_status="coherent",
                        final_response=resp_text,
                        total_elapsed_ms=(time.time() - start_time) * 1000,
                        model_used="semantic-cache",
                    )

            web_context_str = ""
            web_search_used = False

            if force_web_search or SignalTag.WEB_SEARCH_INTENT in decision.tags:
                search_query = self._build_contextual_search_query(user_input, lang=effective_lang)
                web_results = search_web(search_query, max_results=4, lang="en" if effective_lang == "English" else "es")
                if web_results:
                    web_context_str = format_search_results(web_results)
                    web_search_used = True
                    threading.Thread(
                        target=self._persist_web_knowledge,
                        args=(search_query, web_results),
                        daemon=True,
                    ).start()

            recent_turns = self.memory_graph.get_recent_history(limit=8)
            # Mismo filtro que run_turn (ver la nota junto a
            # _truncate_history_entries): no reinyectar turnos viejos
            # guardados como "[ERROR] ..." como si fueran historial real.
            recent_turns = self._truncate_history_entries(recent_turns)
            compacted_context = "\n".join(recent_turns)
            
            if not web_search_used:
                rag_context = self.fetch_hybrid_context(user_input)
                if rag_context:
                    compacted_context += f"\n--- RAG HISTÓRICO ---\n{rag_context}"

            metacognitive_context = self._fetch_metacognitive_lessons(user_input)

            compacted_context, web_context_str, metacognitive_context = self._trim_context_to_budget(
                user_input, compacted_context, web_context_str, metacognitive_context
            )

            # Carril LEAN de una sola pasada — mismo criterio que run_turn
            # (ver la nota extensa allí y STEP0_HARMONY_FINDINGS.md): con la
            # arquitectura de modelo único, gpt-oss razona en su canal
            # Harmony `analysis` nativo, así que `_call_llm_two_pass` + el
            # protocolo <thought> no aplican (y de hecho rompen el turno con
            # HTTP 500). `_call_llm_two_pass` se conserva sin invocar.
            # process_turn es la ruta síncrona (tests, callers no-streaming):
            # se comporta "de forma slow_path" siempre, así que usa el techo
            # de slow_path.
            is_fast_gen = decision.path == RoutePath.FAST_PATH
            gen_context = compacted_context
            if SignalTag.FACTUAL_ENUMERATION in decision.tags:
                gen_context += self._factual_enumeration_caution(effective_lang)
            gen_prompt = self._build_reasoning_prompt(
                user_input, gen_context, web_context_str, False,
                inject_dev_override=False, metacognitive_context=metacognitive_context,
                lang=effective_lang, lean=True,
            )
            raw_response, _pt_eval, _pt_done = self._call_llm_raw(
                gen_prompt,
                target_model=active_model,
                lang_override=effective_lang,
                has_web_evidence=web_search_used,
                system_override=self._get_fastpath_system_prompt(effective_lang),
                num_predict_override=(
                    MemoryGovernor.fastpath_num_predict() if is_fast_gen
                    else MemoryGovernor.slowpath_num_predict()
                ),
                stop=self._ANSWER_RESTART_STOP_SEQUENCES,
            )

            # Mismo criterio que run_turn (ver la nota junto a su
            # propio chequeo de "[ERROR"): no tratar un error de Ollama
            # como si fuera la respuesta del modelo.
            if raw_response.lstrip().startswith("[ERROR"):
                self._wal.append_response(turn_id, raw_response, outcome="error")
                return TurnTrace(
                    turn_id=turn_id,
                    user_input=user_input,
                    routing_decision=decision,
                    outcome=TurnOutcome.ERROR,
                    engine_results=[],
                    web_context_used=web_search_used,
                    knowledge_node_persisted=False,
                    logical_status="error",
                    final_response=raw_response,
                    total_elapsed_ms=(time.time() - start_time) * 1000,
                    model_used=active_model,
                )

            if is_coder and "```python" in raw_response:
                raw_response, repairs = self._validate_and_fix_python_code(
                    raw_response, target_model=active_model
                )
            else:
                repairs = 0

            thought_process, clean_response = self._split_thought_and_content(raw_response)
            # Fuga del canal analysis de Harmony (gpt-oss) — mismo criterio
            # que run_turn, ver `_strip_harmony_leak`.
            clean_response, _ = self._strip_harmony_leak(clean_response)
            clean_response, _ = self._strip_leaked_reasoning(clean_response)
            # Nota: ver `_strip_system_prompt_echo`, cerca de
            # `_strip_leaked_reasoning` - mismo riesgo de eco literal del
            # header congelado, aplicado acá para no dejar process_turn
            # (slow_path) divergiendo de run_turn en esta defensa.
            clean_response, _ = self._strip_system_prompt_echo(clean_response)
            clean_response, _ = self._dedupe_enumeration_items(clean_response)

            # Nota: ver _slowpath_response_looks_broken, junto a
            # _fastpath_response_looks_broken - misma defensa que run_turn
            # agrega para su carril slow_path (process_turn es siempre
            # "de forma slow_path": no tiene el breaker de fast_path por
            # separado, así que corre siempre acá, sin condicionar por
            # decision.path).
            broken_reason = self._slowpath_response_looks_broken(clean_response)
            if broken_reason:
                clean_response = (
                    self._SAFE_FALLBACK_EN if effective_lang == "English"
                    else self._SAFE_FALLBACK_ES
                )

            final_response, _ = self.resolve_visible_answer(
                raw_response, clean_response,
                active_model=active_model, lang=effective_lang,
                has_web_evidence=web_search_used,
            )

            self.memory_graph.store_turn(turn_id, "user", user_input)
            self.memory_graph.store_turn(str(uuid.uuid4()), "assistant", final_response)
            self._wal.append_response(turn_id, final_response, outcome=decision.path.value)

            if not web_search_used:
                self.store_semantic_cache_async(user_input, final_response, active_model, decision=decision)

            elapsed_ms = (time.time() - start_time) * 1000

            return TurnTrace(
                turn_id=turn_id,
                user_input=user_input,
                routing_decision=decision,
                outcome=TurnOutcome.FAST_PATH_DIRECT if decision.path.value == "fast_path" else TurnOutcome.SLOW_PATH_GENERIC_REASONING,
                engine_results=[],
                web_context_used=web_search_used,
                knowledge_node_persisted=False,
                logical_status="coherent",
                final_response=final_response,
                total_elapsed_ms=elapsed_ms,
                model_used=active_model,
                syntax_repairs_applied=repairs,
            )

        finally:
            self._is_processing_turn = False
            self._pause_governor_event.clear()

    # Nota (freno de emergencia contra bucles de herramientas): si el
    # modelo invoca la misma herramienta más de este número de veces
    # SEGUIDAS (p. ej. read_file -> read_file -> read_file sin variar
    # parámetros ni progresar), es una señal fuerte de que quedó atascado
    # repitiendo la misma acción en vez de avanzar hacia una respuesta.
    # max_iterations por sí solo no distingue esto de una cadena
    # LEGÍTIMA de herramientas DISTINTAS (p. ej. list_dir -> read_file ->
    # system_telemetry, tres pasos razonables) - este contador es
    # específico a la REPETICIÓN, no al conteo total.
    MAX_CONSECUTIVE_SAME_TOOL_CALLS: int = 2
    # Nota (freno de emergencia - límite TOTAL de llamadas a
    # herramientas en un mismo turno, distinto del freno de arriba que
    # solo mira REPETICIONES de la misma herramienta): tope duro a la
    # longitud de la cadena completa. Antes era un `3` sin nombre,
    # embebido directo en la firma del método - ahora es una constante
    # nombrada para que su valor sea legible/ajustable en un solo lugar
    # y para que el mensaje de "límite alcanzado" (ver el `else` del
    # `for` más abajo) pueda referenciarlo por nombre.
    MAX_TOOL_CALLS: int = 5

    def _handle_autonomous_tool_loop(
        self,
        initial_response: str,
        user_input: str,
        active_model: str,
        max_iterations: Optional[int] = None,
        lang: Optional[str] = None,
    ) -> str:
        max_iterations = self.MAX_TOOL_CALLS if max_iterations is None else max_iterations
        current_response = initial_response
        last_tool_name: Optional[str] = None
        consecutive_same_tool_calls = 0

        for iteration in range(max_iterations):
            tool_call = self.extract_tool_call(current_response)
            if not tool_call or "tool" not in tool_call:
                break

            tool_name = tool_call.get("tool")

            consecutive_same_tool_calls = (
                consecutive_same_tool_calls + 1 if tool_name == last_tool_name else 1
            )
            last_tool_name = tool_name

            if consecutive_same_tool_calls > self.MAX_CONSECUTIVE_SAME_TOOL_CALLS:
                logger.warning(
                    "🛡️ [Control de Bucles] Freno de emergencia: '%s' invocada %d veces seguidas — "
                    "abortando el bucle sin ejecutarla de nuevo.",
                    tool_name, consecutive_same_tool_calls,
                )
                is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"
                current_response = (
                    f"I called the '{tool_name}' tool {consecutive_same_tool_calls} times in a row "
                    "without making progress, so I'm stopping here to avoid a loop. Could you "
                    "clarify or rephrase the request?"
                    if is_en else
                    f"Invoqué la herramienta '{tool_name}' {consecutive_same_tool_calls} veces "
                    "seguidas sin avanzar, así que me detengo aquí para evitar un bucle. ¿Podrías "
                    "aclarar o reformular la solicitud?"
                )
                break

            logger.info("🛡️ [Control de Bucles] Iteración autónoma %d/%d — Ejecutando: %s", iteration + 1, max_iterations, tool_name)

            tool_output = self.execute_tool_from_call(tool_call)
            
            if "FALLO DE HERRAMIENTA" in tool_output:
                follow_up_prompt = (
                    f"Petición original del usuario: {user_input}\n\n"
                    f"Resultado obtenido de la herramienta local '{tool_name}':\n{tool_output}\n\n"
                    "INSTRUCCIÓN: Redacta una respuesta clara en lenguaje natural basándote en "
                    "este resultado. Queda prohibido generar más llamadas a herramientas."
                )
            else:
                follow_up_prompt = (
                    f"Petición original del usuario: {user_input}\n\n"
                    f"Resultado obtenido de la herramienta local '{tool_name}':\n{tool_output}\n\n"
                    "INSTRUCCIÓN ESTRICTA: Redacta una respuesta final clara y directa para el usuario basada exclusivamente en este resultado. "
                    "PROHIBIDO generar nuevos bloques JSON o reintentar llamadas a herramientas."
                )

            current_response = self._call_llm(
                follow_up_prompt,
                target_model=active_model,
                lang_override=lang,
            )
        else:
            if self.extract_tool_call(current_response):
                logger.warning(
                    "🛡️ [Control de Bucles] Límite de %d llamadas a herramientas alcanzado sin "
                    "respuesta definitiva — interrumpiendo la cadena.",
                    max_iterations,
                )
                is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"
                current_response = (
                    f"[SYSTEM NOTICE]: Reached the {max_iterations}-tool-call limit for this turn "
                    "without producing a final answer. Stopping here to avoid an infinite loop — "
                    "please rephrase your request or break it into smaller steps."
                    if is_en else
                    f"[AVISO DEL SISTEMA]: Se alcanzó el límite de {max_iterations} llamadas a "
                    "herramientas de este turno sin llegar a una respuesta definitiva. Me detengo "
                    "aquí para evitar un bucle infinito — por favor reformula la solicitud o "
                    "divídela en pasos más pequeños."
                )

        return current_response

    #: Cobertura mínima de los términos distintivos de la consulta que
    #: debe aparecer en la evidencia recuperada para considerarla "con
    #: sustancia". Por debajo de esto, las fuentes hablan de otra cosa.
    THIN_CONTEXT_COVERAGE_THRESHOLD: float = 0.34

    def build_thin_context_warning(
        self, user_query: str, evidence_items: list, lang: Optional[str] = None
    ) -> str:
        """
        Avisa cuando la búsqueda SÍ devolvió fuentes, pero esas fuentes
        no cubren los términos distintivos de la consulta.

        Cubre el hueco entre los dos casos que ya estaban contemplados:
        `[SIN DATOS EN TIEMPO REAL]` (cero resultados) y
        `build_year_mismatch_warning()` (año equivocado). El fallo
        observado no es ninguno de los dos: la búsqueda tiene éxito y
        trae artículos legítimos, pero genéricos respecto a lo que se
        preguntó, así que el modelo los usa como excusa para redactar
        generalidades de su conocimiento paramétrico como si vinieran de
        las fuentes — sin decirle nunca al usuario que la evidencia no
        contenía la respuesta.

        Determinista y de coste cero (solapamiento léxico, sin llamadas
        al modelo), igual que el resto de chequeos de este pipeline.
        Devuelve "" cuando la cobertura es aceptable.
        """
        significant = {
            w for w in _WORD_SPLIT_RE.split((user_query or "").lower())
            if len(w) > 3
        }
        if len(significant) < 2:
            return ""  # consulta demasiado corta para medir cobertura

        evidence_blob = " ".join(str(item) for item in (evidence_items or [])).lower()
        if not evidence_blob.strip():
            return ""  # sin evidencia: lo cubre la rama de "cero resultados"

        evidence_words = set(_WORD_SPLIT_RE.split(evidence_blob))
        covered = significant & evidence_words
        coverage = len(covered) / len(significant)
        if coverage >= self.THIN_CONTEXT_COVERAGE_THRESHOLD:
            return ""

        missing = ", ".join(sorted(significant - evidence_words)[:6])
        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"
        if is_en:
            return (
                "[VERIFICATION WARNING — SOURCES LACK THE REQUESTED SPECIFICS]: A real search "
                "WAS executed and returned sources, but they do not actually cover the specific "
                f"thing that was asked (no substantive coverage of: {missing}). "
                "You MUST open your answer by stating plainly that the retrieved sources do not "
                "contain concrete information about the specific question, and then say what "
                "they DO cover. If you add anything from your own general knowledge afterwards, "
                "label it explicitly as your own knowledge and NOT as something the sources say. "
                "It is STRICTLY FORBIDDEN to present general background as if it answered the "
                "specific question.\n\n"
            )
        return (
            "[AVISO DE VERIFICACIÓN — LAS FUENTES NO TRAEN LO ESPECÍFICO QUE SE PIDIÓ]: SÍ se "
            "ejecutó una búsqueda real y devolvió fuentes, pero esas fuentes no cubren lo "
            f"concreto que se preguntó (sin cobertura sustantiva de: {missing}). "
            "DEBES abrir tu respuesta diciendo claramente que las fuentes recuperadas no "
            "contienen información concreta sobre la pregunta específica, y a continuación "
            "indicar de qué SÍ hablan. Si después añades algo de tu conocimiento general, "
            "etiquétalo explícitamente como conocimiento propio y NO como algo que digan las "
            "fuentes. Queda ESTRICTAMENTE PROHIBIDO presentar contexto general como si "
            "respondiera la pregunta específica.\n\n"
        )

    def build_year_mismatch_warning(
        self, user_query: str, evidence_items: list, lang: Optional[str] = None
    ) -> str:
        query_years = _extract_years(user_query)
        if not query_years:
            return ""

        evidence_blob = " ".join(str(item) for item in (evidence_items or []))
        evidence_years = _extract_years(evidence_blob)
        missing_years = query_years - evidence_years
        if not missing_years:
            return ""

        years_str = ", ".join(sorted(missing_years))
        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"
        if is_en:
            return (
                f"[VERIFICATION WARNING]: Web results appear to belong to a different year or event than queried. "
                f"Strictly limit yourself to what is reported in the sources.\n"
                f"You mentioned the year {years_str} in your query, but NONE of the retrieved sources "
                f"match this timeframe. Do not fabricate facts to force alignment with the requested "
                f"year with the facts in these sources: if you don't find specific evidence "
                f"about {years_str} in the content below, say so explicitly in your answer "
                f"(e.g. \"I didn't find results specific to {years_str}; what I retrieved is "
                f"about [the year that does appear]\") instead of presenting it as the answer "
                f"to what was asked. This is NOT the same as claiming you lack internet "
                f"access — a real search WAS executed; this is flagging that its results "
                f"don't specifically cover the requested year.\n\n"
            )
        return (
            f"[AVISO DE VERIFICACIÓN]: Los resultados web parecen pertenecer a un año o evento diferente al consultado. Limítate estrictamente a lo reportado en las fuentes.\n"
            f"Mencionaste el año {years_str} en tu consulta, pero NINGUNA de las fuentes recuperadas "
            f"abajo lo menciona explícitamente. Es probable que estas fuentes sean sobre un "
            f"evento/año DISTINTO al que preguntaste. NO relabels ni mezcles el año que "
            f"preguntaste con los hechos de estas fuentes: si no encuentras evidencia "
            f"específica sobre {years_str} en el contenido de abajo, dilo explícitamente en "
            f"tu respuesta (p. ej. \"no encontré resultados específicos de {years_str}; lo "
            f"que recuperé es sobre [el año que sí aparece]\") en vez de presentarlo como si "
            f"fuera la respuesta a lo que preguntaste. Esto NO es decir que careces de "
            f"acceso a internet — sí se ejecutó una búsqueda real; es señalar que sus "
            f"resultados no cubren específicamente el año solicitado.\n\n"
        )

    # Patrón de marcador/resultado tipo "3-1", "2 - 0", "3–1" - el mismo
    # tipo de dato puntual y verificable que un modelo pequeño no debe
    # promediar ni elegir al azar entre fuentes que lo reportan distinto,
    # ni INVENTAR cuando ninguna fuente lo trae (ver
    # find_unsupported_scores). Definición en relevance.py, compartida.
    @staticmethod
    def _extract_score_patterns(text: str) -> set:
        return _rel_extract_score_patterns(text)

    # =================================================================
    # AFIRMACION DE GANADOR SIN MARCADOR NUMERICO
    # =================================================================
    # Hueco real observado en produccion: "The match will feature Spain
    # and Argentina, with Spain emerging as the victors" - una afirmacion
    # de RESULTADO que las fuentes recuperadas (paginas genericas de
    # estructura del torneo) no confirman en ninguna parte.
    #
    # Ninguna defensa existente la cubria, por razones distintas:
    #   * find_unsupported_scores solo mira patrones de marcador
    #     numerico. "emerging as the victors" no tiene un solo digito,
    #     asi que esa funcion ni siquiera la evalua.
    #   * source_names_unconfirmed_participant (relevance.py) escanea el
    #     TITULO de las fuentes para descartar una fuente contaminada
    #     antes de que entre al contexto - nunca mira lo que el modelo
    #     escribio en su propia prosa.
    #
    # Este verificador es el analogo NO numerico de find_unsupported_
    # scores y comparte su estrategia: comparar por EVENTO (entidades
    # alrededor de la afirmacion), no como bolsa plana de palabras.
    # Nota (medido - capturado dos veces en la misma
    # sesión con la misma consulta): "with France winning the
    # championship for the second time" - una alucinación de resultado
    # invertido (Argentina ganó, no Francia) que este verificador
    # DEBERÍA haber atrapado, pero "winning" (gerundio) no matcheaba
    # ninguna de las formas listadas - solo cubría "won"/"wins", no la
    # conjugación en -ing que el modelo usó las dos veces.
    #
    # Los verbos viven en UN fragmento compartido (`_VICTORY_VERBS`),
    # reusado tanto en `_VICTORY_CLAIM_RE` (qué cuenta como afirmación de
    # victoria en la RESPUESTA) como en `_FINAL_OUTCOME_RE` (qué cuenta
    # como desenlace confirmado en la EVIDENCIA) - precisamente para que
    # esta clase de bug (una forma verbal que solo se agrega de un lado)
    # no pueda volver a pasar: agregar "winning" acá lo agrega a las dos
    # regex a la vez, no a una sola.
    _VICTORY_VERBS: str = (
        r"won|wins|winning|beat|beats|beating|defeated|defeats|defeating|"
        r"victorious|victors?|victory|champions?|"
        r"lifted the trophy|lifting the trophy|"
        r"clinched|clinching|claimed the title|claiming the title|"
        r"secured|securing|triumphed|triumphing|prevailed|prevailing|"
        r"crowned|took the title|taking the title|"
        r"gan[oó]|ganaron|ganando|venci[oó]|vencieron|venciendo|"
        r"derrot[oó]|derrotaron|derrotando|"
        r"se impuso|se impusieron|imponi[eé]ndose|triunf[oó]|triunfando|"
        r"se coron[oó]|coron[aá]ndose|"
        r"campe[oó]n|campeona|campeones"
    )
    _VICTORY_CLAIM_RE: Pattern[str] = re.compile(
        r"\b(?:" + _VICTORY_VERBS + r")\b",
        re.IGNORECASE | re.UNICODE,
    )

    # Un titulo ANTERIOR no respalda el resultado de este evento. Es la
    # trampa exacta del caso real: la evidencia decia "contested by Spain
    # and defending champion Argentina", donde "champion" describe el
    # titulo previo de Argentina, no quien gano esta final. Sin esta
    # exclusion, esa frase habria "respaldado" la invencion.
    # Mismo espiritu que el descarte de mentions_non_final_round en
    # find_unsupported_scores.
    _PRIOR_TITLE_RE: Pattern[str] = re.compile(
        r"\b(?:defending|reigning|former|previous|past)\s+"
        r"(?:champions?|winners?)\b"
        r"|\bcampe(?:on|ona|ones)\s+(?:defensor(?:a|es)?|vigente|anterior)\b"
        r"|\bvigente\s+campe(?:on|ona|ones)\b",
        re.IGNORECASE | re.UNICODE,
    )

    # Para RESPALDAR una afirmacion de ganador no alcanza con que la
    # evidencia contenga un verbo de victoria: tiene que enunciar el
    # DESENLACE del evento preguntado. Falla real que lo motiva: la
    # afirmacion "Spain emerging as the victors" (la final) quedaba
    # "respaldada" por "En route to the final, Spain finished first in
    # Group H with two wins" - una frase de fase de grupos. El solape de
    # contexto que usa find_unsupported_scores no alcanza aca porque en
    # ese verificador el MARCADOR ya tiene que coincidir primero; en una
    # afirmacion sin numero el contexto es el unico criterio, y basta una
    # palabra vacia compartida ("with") para dar un respaldo falso.
    #
    # Por eso se exige ADYACENCIA entre el verbo de victoria y la
    # referencia al evento: "won the 2026 World Cup final" si respalda;
    # "final, Spain finished first in Group H with two wins" no, porque
    # entre "final" y "wins" hay 8 palabras, fuera de la ventana. Usa el
    # mismo `_VICTORY_VERBS` que `_VICTORY_CLAIM_RE` (ver arriba).
    _FINAL_OUTCOME_RE: Pattern[str] = re.compile(
        r"(?:" + _VICTORY_VERBS + r")"
        r"(?:\W+\w+){0,6}?\W+(?:final|title|trophy|titulo|título|campeonato)"
        r"|(?:final|title|trophy|titulo|título|campeonato)(?:\W+\w+){0,4}?\W+"
        r"(?:" + _VICTORY_VERBS + r")"
        r"|champions?\s+of\s+the\s+(?:\d{4}\s+)?(?:FIFA\s+)?world\s+cup"
        r"|lifted\s+the\s+trophy",
        re.IGNORECASE | re.UNICODE,
    )
    # Nota (medido): con 90 caracteres, una oración larga
    # y compuesta ("Argentina ultimately secured a convincing victory
    # with a score of 3-0, marking their third World Cup title and
    # celebrating captain Lionel Messi's third World Cup victory") deja
    # el segundo verbo de victoria ("victory", al final) a más de 90
    # caracteres del nombre del equipo ("Argentina", al principio) - la
    # ventana recorta el nombre y la afirmación pierde su único punto
    # de comparación con la evidencia, marcándose "sin respaldo" por un
    # artefacto de recorte, no porque falte evidencia real. Más
    # frecuente con modelos que redactan oraciones más largas (7B) que
    # uno más lacónico (3B). 90 -> 220 (medido: la oración real de este
    # bug necesita ~175 caracteres hacia atrás para alcanzar el sujeto).
    _VICTORY_CONTEXT_WINDOW: int = 220

    @classmethod
    def _extract_victory_events(cls, text: str) -> List[Dict[str, str]]:
        """
        Cada afirmacion de victoria junto al texto que la rodea, recortado
        al limite de oracion mas cercano — misma tecnica que
        relevance.extract_score_events y por el mismo motivo: sin recortar
        en el punto, la ventana cruza a la oracion siguiente y arrastra
        entidades de otro partido.
        """
        events: List[Dict[str, str]] = []
        text = text or ""
        for m in cls._VICTORY_CLAIM_RE.finditer(text):
            start = max(0, m.start() - cls._VICTORY_CONTEXT_WINDOW)
            end = min(len(text), m.end() + cls._VICTORY_CONTEXT_WINDOW)

            left = text[start:m.start()]
            boundary = max(left.rfind(ch) for ch in ".!?\n")
            if boundary != -1:
                start += boundary + 1
            elif start > 0 and text[start - 1].isalnum():
                # Nota (medido): sin límite de oración
                # dentro de la ventana, el recorte cae a mitad de
                # palabra ("gentina" en vez de "Argentina") y la
                # afirmación pierde el único identificador que
                # `contexts_describe_same_event()` necesita para
                # emparejarla con la evidencia - se marca "sin
                # respaldo" por el corte, no por falta de evidencia
                # real. Avanza al siguiente espacio para no partir una
                # palabra.
                next_space = text.find(" ", start)
                if next_space != -1 and next_space < m.start():
                    start = next_space + 1

            right = text[m.end():end]
            cuts = [i for i in (right.find(ch) for ch in ".!?\n") if i != -1]
            if cuts:
                end = m.end() + min(cuts)

            events.append({
                "claim": m.group(),
                "context": text[start:end].strip(),
            })
        return events

    def find_unsupported_victory_claims(
        self, user_query: str, response_text: str, evidence_text: str,
    ) -> set:
        """
        Afirmaciones de ganador que la RESPUESTA hace y que la evidencia
        no respalda. Devuelve el conjunto de contextos ofensores, vacío
        cuando no hay nada que objetar — mismo contrato que
        find_unsupported_scores.
        """
        if not requires_precise_fact(user_query):
            return set()
        if not response_text or not evidence_text or not evidence_text.strip():
            return set()

        # Nota (medido): `claimed` no filtraba por ronda,
        # así que una respuesta detallada ("tell me all the details")
        # que narra todo el recorrido del equipo (fase de grupos, octavos,
        # cuartos, semifinal) generaba una afirmación de "victoria" por
        # cada ronda previa - cada una 100% cierta y respaldada por la
        # evidencia, pero sobre un RIVAL distinto al de la final. Como
        # `supporting` exige evidencia de la final específicamente, esas
        # afirmaciones de rondas previas nunca podían encontrar
        # respaldo y quedaban marcadas como "no respaldadas" en masa -
        # más visible con modelos que redactan respuestas más largas
        # (7B), que mencionan más rondas previas que un modelo más
        # lacónico (3B). Mismo criterio que ya usa `find_unsupported_
        # scores`/`_score_conflict_groups` para esta distinción.
        claimed = [
            c for c in self._extract_victory_events(response_text)
            if not mentions_non_final_round(c["context"])
        ]
        if not claimed:
            return set()

        supporting = [
            e for e in self._extract_victory_events(evidence_text)
            if not self._PRIOR_TITLE_RE.search(e["context"])
            and self._FINAL_OUTCOME_RE.search(e["context"])
        ]

        unsupported: set = set()
        for claim in claimed:
            if self._PRIOR_TITLE_RE.search(claim["context"]):
                continue
            if not distinctive_words(claim["context"]):
                continue
            supported = any(
                contexts_describe_same_event(user_query, claim["context"], e["context"])
                for e in supporting
            )
            if not supported:
                unsupported.add(claim["context"])

        return unsupported

    def build_unsupported_victory_correction_prompt(
        self, user_query: str, response_text: str, unsupported: set,
        evidence_text: str = "", lang: Optional[str] = None,
    ) -> str:
        """
        Prompt de correccion para afirmaciones de ganador sin respaldo.
        Mismo contrato que el resto de constructores del pipeline: ""
        cuando no hay nada que corregir, para usarlo como condicion.

        BLINDAJE (bug real, MEDIDO — final del Mundial 2014): el modelo
        afirmó "Germany won the match 7-1" — el marcador REAL es el de
        la SEMIFINAL (Alemania 7-1 Brasil), confundido con la final
        (que Alemania ganó 1-0 a Argentina). `find_unsupported_victory_
        claims()` atrapó bien la alucinación, pero esta corrección le
        ordenaba al modelo negar CUALQUIER resultado ("NO nombres a
        ningún ganador") sin darle la chance de revisar las fuentes y
        usar el resultado CORRECTO — que sí estaba ahí. La respuesta
        final terminó siendo un evasivo "las fuentes no reportan el
        resultado" para un partido cuyo resultado SÍ estaba en la
        evidencia. Mismo criterio que ya usa `build_unsupported_score_
        correction_prompt` (arriba): extraer lo que las fuentes SÍ
        confirman sobre el desenlace (vía `_extract_victory_events` +
        `_FINAL_OUTCOME_RE`, el mismo filtro que usa `find_unsupported_
        victory_claims` para construir `supporting`) y ofrecerlo como
        candidato en vez de una negación en blanco.
        """
        if not unsupported:
            return ""

        quoted = "\n".join("- " + c.strip() for c in sorted(unsupported))

        supporting = [
            e for e in self._extract_victory_events(evidence_text)
            if not self._PRIOR_TITLE_RE.search(e["context"])
            and self._FINAL_OUTCOME_RE.search(e["context"])
        ]
        supported_str = "; ".join(sorted({e["context"] for e in supporting})[:3])

        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"

        if is_en:
            found_line = (
                f"What the sources DO confirm about the outcome: {supported_str}\n\n"
                if supported_str else ""
            )
            return (
                f"Original user question: {user_query}\n\n"
                "Your answer asserted who won, but the retrieved sources do NOT confirm "
                "that specific claim:\n"
                f"{quoted}\n\n"
                f"{found_line}"
                "Rewrite your final answer now. If the outcome above is the correct one, "
                "use it instead of your original claim. Only if the sources genuinely do "
                "not report any outcome, state that plainly and do NOT name a winner. "
                "Keep everything else the sources DO confirm (date, venue, participants). "
                "Write only the corrected answer, without explaining the correction."
            )

        found_line = (
            f"Lo que las fuentes SÍ confirman sobre el desenlace: {supported_str}\n\n"
            if supported_str else ""
        )
        return (
            f"Pregunta original del usuario: {user_query}\n\n"
            "Tu respuesta afirmo quien gano, pero las fuentes recuperadas NO confirman "
            "esa afirmacion puntual:\n"
            f"{quoted}\n\n"
            f"{found_line}"
            "Reescribe ahora tu respuesta final. Si el desenlace de arriba es el "
            "correcto, usalo en vez de tu afirmacion original. Solo si las fuentes "
            "genuinamente no reportan ningun desenlace, dilo con claridad y NO nombres "
            "a ningun ganador. Conserva todo lo demas que las fuentes SI confirman "
            "(fecha, sede, participantes). Escribe unicamente la respuesta corregida, "
            "sin explicar la correccion."
        )
    def find_unsupported_scores(
        self,
        user_query: str,
        response_text: str,
        evidence: Any,
    ) -> set:
        """
        Compatibilidad con la API anterior.

        La forma recomendada de `evidence` es una lista de fuentes originales.
        También acepta texto plano para no romper llamadas antiguas, aunque
        esa forma pierde metadatos de procedencia.
        """
        if not requires_precise_fact(user_query):
            return set()

        if isinstance(evidence, str):
            raw_sources = [{
                "title": "Evidencia heredada",
                "url": "",
                "domain": "",
                "raw_content": evidence,
            }]
        else:
            raw_sources = list(evidence or [])

        report = verify_scores(
            user_query,
            response_text,
            raw_sources,
        )

        return report.unsupported_scores

    def build_unsupported_score_correction_prompt(
        self, user_query: str, response_text: str, unsupported: set,
        evidence_text: str, lang: Optional[str] = None,
    ) -> str:
        """
        Prompt de corrección para los marcadores que `find_unsupported_
        scores()` marcó como no respaldados. Mismo contrato que el resto
        de constructores de este pipeline: devuelve "" cuando no hay nada
        que corregir, así que el llamador puede usarlo como condición.
        """
        if not unsupported:
            return ""

        values = ", ".join(sorted(unsupported))
        supported = self._extract_score_patterns(evidence_text)
        supported_str = ", ".join(sorted(supported)) if supported else ""
        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"

        if is_en:
            found_line = (
                f"The only score values that DO appear in the sources are: {supported_str}.\n"
                if supported_str else
                "NO score value appears anywhere in the retrieved sources.\n"
            )
            return (
                f"Original user question: {user_query}\n\n"
                f"Your answer stated the score/result {values}, but that value does NOT "
                f"appear in any of the retrieved sources.\n"
                f"{found_line}"
                "Rewrite your final answer now. If a value from the sources is the correct "
                "one, use it. If the sources do not contain the result at all, say plainly "
                "that the retrieved sources do not report it, and do NOT state any score. "
                "Write only the corrected answer, without explaining the correction."
            )

        found_line = (
            f"Los únicos marcadores que SÍ aparecen en las fuentes son: {supported_str}.\n"
            if supported_str else
            "NINGÚN marcador aparece en las fuentes recuperadas.\n"
        )
        return (
            f"Pregunta original del usuario: {user_query}\n\n"
            f"Tu respuesta afirmó el marcador/resultado {values}, pero ese valor NO "
            f"aparece en ninguna de las fuentes recuperadas.\n"
            f"{found_line}"
            "Reescribe ahora tu respuesta final. Si un valor de las fuentes es el "
            "correcto, úsalo. Si las fuentes no traen el resultado, di claramente que "
            "las fuentes recuperadas no lo reportan, y NO afirmes ningún marcador. "
            "Escribe únicamente la respuesta corregida, sin explicar la corrección."
        )

    # Nota (medido 2026-08-19): las cuatro correcciones
    # post-hoc de este pipeline (idioma, marcador sin respaldo, ganador
    # sin respaldo, contradicción sin atribuir) comparten el mismo
    # patrón - se le manda al modelo un prompt de INSTRUCCIÓN (que
    # incluye, textualmente, la respuesta ORIGINAL incorrecta a
    # corregir) y se espera de vuelta SOLO la respuesta corregida. Con
    # qwen2.5:3b eso no siempre pasa: el modelo a veces repite de vuelta
    # el prompt completo (instrucción + respuesta original citada)
    # antes de recién ahí escribir la corrección real - caso real
    # capturado en producción: para una corrección de idioma a inglés,
    # la respuesta visible en el chat terminó siendo literalmente "The
    # response below was written in the wrong language for this turn.
    # Rewrite it ENTIRELY in English [...] Response to translate:
    # Basándome en la información [...]" seguido recién después del
    # texto realmente corregido - el eco completo del prompt se mostró
    # al usuario como si fuera la respuesta, porque nada lo separaba de
    # la corrección real.
    #
    # Cada uno de los 4 prompts de corrección termina en una línea de
    # cierre fija y reconocible que un usuario real nunca escribiría
    # ("Write only the corrected answer, without explaining the
    # correction." / su equivalente en español, o el cierre específico
    # de idioma). Si esa línea aparece en la salida del modelo, todo lo
    # que viene antes de su última aparición es eco del prompt - se
    # descarta y solo se conserva lo que sigue después, que es la
    # corrección real.
    _CORRECTION_PROMPT_ECHO_TAIL_RE: Pattern[str] = re.compile(
        r"(?:"
        r"corrected response,?\s*(?:in english only,?\s*)?with no explanation of the correction:?|"
        r"respuesta corregida,?\s*(?:solo en espa[ñn]ol,?\s*)?sin explicar la correcci[oó]n:?|"
        r"write only the corrected answer,?\s*without explaining the correction\.?|"
        r"escribe [uú]nicamente la respuesta corregida,?\s*sin explicar la correcci[oó]n\.?"
        r")",
        re.IGNORECASE,
    )

    @classmethod
    def _strip_correction_prompt_echo(cls, raw: str) -> str:
        """
        Recorta un eco del prompt de corrección si el modelo lo repitió
        antes de la respuesta real — ver el BLINDAJE arriba. Idempotente
        y segura sobre texto que nunca tuvo eco (`raw` vuelve intacto,
        solo con `.strip()`).
        """
        if not raw:
            return raw
        matches = list(cls._CORRECTION_PROMPT_ECHO_TAIL_RE.finditer(raw))
        if not matches:
            return raw.strip()
        tail = raw[matches[-1].end():].strip()
        return tail or raw.strip()

    def _count_verifiable_violations(
        self,
        user_query: str,
        response_text: str,
        web_context_str: str,
        web_sources_for_rag: Optional[list] = None,
    ) -> int:
        """
        IDEA DE ARQUITECTURA (2026-08-19) — "verificación por consenso":
        cuenta cuántas violaciones DETERMINISTAS contra la evidencia real
        tiene `response_text` — el mismo trío de verificadores que ya
        alimentaba, uno por uno, la cadena secuencial de corrección
        (`find_unsupported_scores`, `find_unsupported_victory_claims`,
        `find_unattributed_contradiction`). Convertirlos en un único
        contador es lo que permite comparar DOS candidatos (la respuesta
        ya generada vs. una muestra fresca independiente) y elegir el que
        tenga menos violaciones, en vez de pedirle al mismo modelo que
        "arregle" su propio texto — el patrón que producía el eco del
        prompt de corrección (ver `_strip_correction_prompt_echo`).

        Nunca lanza: se usa en el camino caliente de streaming de la UI,
        donde un fallo en el conteo no debe impedir que el turno
        continúe con el candidato que ya tenía.
        """
        if not response_text or not web_context_str:
            return 0
        violations = 0
        try:
            violations += len(
                self.find_unsupported_scores(user_query, response_text, web_context_str)
            )
        except Exception:
            logger.debug("Conteo de violaciones (marcadores) falló", exc_info=True)
        try:
            violations += len(
                self.find_unsupported_victory_claims(user_query, response_text, web_context_str)
            )
        except Exception:
            logger.debug("Conteo de violaciones (ganador) falló", exc_info=True)
        try:
            violations += len(
                self.find_unattributed_contradiction(
                    user_query, response_text, web_sources_for_rag or []
                )
            )
        except Exception:
            logger.debug("Conteo de violaciones (contradicción) falló", exc_info=True)
        return violations

    def find_language_mismatch(self, response_text: str, expected_lang: str) -> bool:
        """
        True si la respuesta VISIBLE no está en el idioma que el turno
        exige (`expected_lang`, típicamente `effective_lang` de
        `_resolve_turn_language`).

        BLINDAJE (bug real, MEDIDO): las instrucciones de idioma en el
        prompt ("[CRITICAL LANGUAGE RULE]", el aviso "MIRROR LANGUAGE"
        del contexto web) no llegan al 100% con qwen2.5:3b — medido en
        vivo con la forma REAL del prompt (mismo turno, 3 semillas): 1
        de 3 respondió en español a un mensaje en inglés, a pesar de
        tener AMBAS instrucciones presentes. Ninguna instrucción de
        prompt por sí sola es confiable con un modelo de 3B — el mismo
        principio que ya motivó `find_unsupported_scores` y compañía:
        verificar el resultado DESPUÉS es determinista y barato, en vez
        de confiar en que el modelo obedezca siempre. Reutiliza
        `_detect_prompt_language` (solapamiento de stopwords cortas y
        frecuentes) — funciona igual de bien sobre una respuesta larga
        que sobre una consulta corta, y de hecho MEJOR: más texto da
        una señal más fuerte, no más ruido.

        Devuelve False (no corregir) si la señal es ambigua
        (`_detect_prompt_language` devuelve `None`) — un texto corto o
        sin stopwords reconocibles (una sola cifra, un nombre propio)
        no amerita una llamada de corrección completa por una lectura
        dudosa.
        """
        if not response_text or not response_text.strip():
            return False
        if expected_lang not in ("English", "Spanish"):
            return False
        detected = _detect_prompt_language(response_text)
        if detected is None:
            return False
        return detected != expected_lang

    def build_language_correction_prompt(self, response_text: str, expected_lang: str) -> str:
        """
        Prompt de corrección para `find_language_mismatch()`. Mismo
        contrato que el resto de constructores de este pipeline: pide
        una traducción fiel, no una regeneración desde cero — preserva
        números, nombres y estructura exactamente, para no arriesgar
        introducir una alucinación nueva en el proceso de corregir el
        idioma.
        """
        if expected_lang == "English":
            return (
                "The response below was written in the wrong language for this turn. "
                "Rewrite it ENTIRELY in English — translate only, do not add, remove, or "
                "change any fact, number, or name. Preserve the same structure and level "
                "of detail.\n\n"
                f"Response to translate:\n{response_text}\n\n"
                "Corrected response, in English only, with no explanation of the correction:"
            )
        return (
            "La siguiente respuesta se escribió en el idioma equivocado para este turno. "
            "Reescríbela COMPLETAMENTE en español — traduce únicamente, sin agregar, quitar "
            "ni cambiar ningún dato, número o nombre. Conserva la misma estructura y nivel "
            "de detalle.\n\n"
            f"Respuesta a traducir:\n{response_text}\n\n"
            "Respuesta corregida, solo en español, sin explicar la corrección:"
        )

    #: Marcadores que la respuesta atribuye explícitamente a una fuente
    #: ("según Reuters...", "according to BBC..."). Una atribución
    #: explícita es exactamente lo que el aviso de contradicción pide, así
    #: que la presencia de estos marcadores desactiva el enforcement.
    _ATTRIBUTION_RE: Pattern[str] = re.compile(
        r"\b(seg[uú]n|de\s+acuerdo\s+con|conforme\s+a|cita|citando|reporta|"
        r"informa|indica|seg[uú]n\s+la\s+fuente|una\s+fuente|otra\s+fuente|"
        r"according\s+to|per\s+\w+|reports?|states?|cites?|one\s+source|"
        r"another\s+source|sources?\s+(?:say|report|differ|disagree))\b",
        re.IGNORECASE,
    )

    def _score_conflict_groups(
        self, evidence_items: list, user_query: str = "",
    ) -> List[Dict[str, Any]]:
        """
        Agrupa los marcadores de la evidencia POR PARTIDO y devuelve solo
        los grupos donde fuentes distintas dan valores distintos.
        """
        entries: List[Tuple[str, Dict[str, str]]] = []
        for item in evidence_items or []:
            if isinstance(item, dict):
                label = str(item.get("title") or item.get("domain") or item.get("url") or "fuente")
                blob = f"{item.get('title', '')} {item.get('content') or item.get('snippet', '')}"
            else:
                text = str(item)
                label = text[:40] + ("…" if len(text) > 40 else "")
                blob = text

            for event in drop_non_final_round_events(extract_score_events(blob), user_query, blob):
                entries.append((label, event))

        groups: List[Dict[str, Any]] = []
        for label, event in entries:
            for group in groups:
                if any(
                    contexts_describe_same_event(user_query, event["context"], ctx)
                    for ctx in group["_contexts"]
                ):
                    group["_contexts"].append(event["context"])
                    group["values"].add(event["score"])
                    group["sources"].setdefault(label, set()).add(event["score"])
                    group["teams"] |= distinctive_words(event["context"])
                    break
            else:
                groups.append({
                    "_contexts": [event["context"]],
                    "values": {event["score"]},
                    "sources": {label: {event["score"]}},
                    "teams": distinctive_words(event["context"]),
                })

        conflicts = [
            g for g in groups
            if len(g["values"]) >= 2 and len(g["sources"]) >= 2
            and not self._is_within_match_progression(g)
        ]

        for group in conflicts:
            per_context = [distinctive_words(c) for c in group["_contexts"]]
            common = set.intersection(*per_context) if per_context else set()
            group["teams"] = common or group["teams"]

        return conflicts

    @staticmethod
    def _is_within_match_progression(group: Dict[str, Any]) -> bool:
        """
        BLINDAJE (bug real, MEDIDO): la final de 1966 terminó 2-2 en
        tiempo reglamentario, 3-2 tras el primer gol de Hurst en el
        alargue y 4-2 tras su tercer gol — tres marcadores CORRECTOS y
        no contradictorios del MISMO partido, que distintas fuentes
        mencionan en distintos momentos de su relato. `_score_conflict_
        groups()` los agrupaba por equipo (correcto) pero marcaba
        cualquier grupo con 2+ valores distintos como "las fuentes se
        contradicen" — sin distinguir eso de un marcador que
        simplemente avanza gol a gol dentro de un único partido.

        Un grupo se descarta como conflicto (retorna True) solo si
        CUMPLE AMBAS condiciones:
        1. Los valores forman una única secuencia donde los goles de
           NINGÚN equipo bajan nunca (2-2 -> 3-2 -> 4-2 sí; 4-1 junto
           a 4-2 también encajaría numéricamente, así que esto NO
           basta por sí solo).
        2. El contexto combinado del grupo menciona explícitamente
           prórroga/alargue o penales — evidencia textual real de que
           el partido tuvo más de una fase, y no una coincidencia
           numérica entre dos fuentes que simplemente discrepan sobre
           el resultado final (p. ej. "4-2" vs "4-1" sin ninguna
           mención de prórroga es un desacuerdo real y debe seguir
           marcándose).
        """
        values = group["values"]
        parsed: List[Tuple[int, int]] = []
        for value in values:
            parts = value.split("-")
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                return False
            parsed.append((int(parts[0]), int(parts[1])))

        parsed.sort()
        is_monotonic_chain = all(
            parsed[i][0] <= parsed[i + 1][0] and parsed[i][1] <= parsed[i + 1][1]
            for i in range(len(parsed) - 1)
        )
        if not is_monotonic_chain:
            return False

        combined_context = " ".join(group.get("_contexts") or [])
        return classify_score_phase(combined_context) != ScorePhase.UNKNOWN

    def build_source_contradiction_warning(
        self, evidence_items: list, lang: Optional[str] = None,
        user_query: str = "",
    ) -> str:
        """
        Generalización de `build_year_mismatch_warning()`: ese chequeo
        compara consulta-vs-evidencia (UN solo desajuste posible, año
        pedido vs. año cubierto); este compara FUENTE-vs-FUENTE dentro
        de la MISMA evidencia recuperada — el fallo real que motiva esto
        es distinto: no que las fuentes sean de otro año/evento, sino
        que dos fuentes sobre el MISMO evento traen marcadores/resultados
        que se CONTRADICEN entre sí (p. ej. una fuente dice "3-1" y otra
        "2-0" para lo que el usuario entiende como la misma final) — un
        modelo pequeño tiende a promediar o elegir uno al azar sin
        señalar el conflicto, en vez de reportarlo como tal.

        `evidence_items` acepta tanto una lista de dicts de fuente
        (title/domain/url + content/snippet, el esquema que ya devuelve
        search_web()/sources en sovnode_qt.py) como una lista de strings
        ya formateados — cada ítem se trata como UNA fuente separada (a
        diferencia de build_year_mismatch_warning(), que junta todo en
        un solo blob de texto).

        Deliberadamente conservador, mismo espíritu que el resto de
        estos chequeos:
        - Exige al menos 2 fuentes DISTINTAS reportando el MISMO partido
          — con una sola fuente no hay nada que contrastar.
        - Solo dispara si esas fuentes dan valores distintos para ese
          partido. La comparación es por partido y no por bolsa plana de
          números (ver `_score_conflict_groups`): antes, una evidencia
          que traía la final Y el tercer puesto se auto-reportaba como
          "fuentes que se contradicen" aunque coincidieran del todo.
        - No intenta decidir cuál marcador es el correcto: solo señala
          el conflicto y dictamina no elegir uno en silencio, dejando
          que el propio texto de la respuesta cite qué dice cada fuente.

        Devuelve una cadena vacía cuando no hay conflicto detectable —
        mismo contrato que build_year_mismatch_warning().
        """
        groups = self._score_conflict_groups(evidence_items, user_query)
        if not groups:
            return ""

        detail_lines: List[str] = []
        for group in groups:
            teams = ", ".join(sorted(group["teams"])[:4])
            detail_lines.append(f"  · {teams or 'mismo partido'}:")
            for label, scores in group["sources"].items():
                detail_lines.append(f"      - {label}: {', '.join(sorted(scores))}")
        detail = "\n".join(detail_lines)

        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"
        if is_en:
            return (
                "[SOURCE CONTRADICTION WARNING]: The retrieved sources report DIFFERENT "
                "score/result values for what appears to be the same match or event:\n"
                f"{detail}\n"
                "Do not silently pick one or average them. Explicitly flag the discrepancy in "
                "your answer, citing which source says what, instead of presenting a single "
                "value as the confirmed result.\n\n"
            )
        return (
            "[AVISO DE CONTRADICCIÓN ENTRE FUENTES]: Las fuentes recuperadas reportan "
            "valores de marcador/resultado DISTINTOS para lo que parece ser el mismo "
            "partido o evento:\n"
            f"{detail}\n"
            "No elijas uno en silencio ni los promedies. Señala explícitamente la "
            "discrepancia en tu respuesta, citando qué dice cada fuente, en vez de "
            "presentar un solo valor como el resultado confirmado.\n\n"
        )

    def find_unattributed_contradiction(
        self, user_query: str, response_text: str, evidence_items: list,
    ) -> List[Dict[str, Any]]:
        """
        Contraparte POST-HOC de `build_source_contradiction_warning()`.

        Ese aviso se inyecta antes de generar y dice "no elijas uno en
        silencio, cita qué dice cada fuente". Un modelo de 3B puede
        ignorarlo y afirmar un valor a secas igual — que es exactamente lo
        que se observó. Este chequeo mira la respuesta YA escrita: si las
        fuentes se contradecían sobre un partido y la respuesta afirma UNO
        solo de esos valores sin atribuirlo a ninguna fuente, la
        instrucción no se cumplió y hay que forzar la corrección.

        No dispara cuando la respuesta:
        - menciona TODOS los valores en conflicto (está reportando la
          discrepancia, que es lo pedido), o
        - atribuye explícitamente ("según X", "according to Y", "una
          fuente ... otra fuente"), o
        - no menciona ninguno de los valores en conflicto.

        Devuelve la lista de grupos incumplidos (vacía si no hay nada que
        forzar), cada uno con "values", "claimed" y "teams".
        """
        if not response_text or not response_text.strip():
            return []

        groups = self._score_conflict_groups(evidence_items, user_query)
        if not groups:
            return []

        attributed = bool(self._ATTRIBUTION_RE.search(response_text))
        claimed_in_response = self._extract_score_patterns(response_text)

        offending: List[Dict[str, Any]] = []
        for group in groups:
            claimed = group["values"] & claimed_in_response
            if len(claimed) != 1:
                # 0 = no eligió ninguno; 2+ = está reportando la
                # discrepancia, que es justo lo que se le pidió.
                continue
            if attributed:
                continue
            offending.append({
                "values": group["values"],
                "claimed": claimed,
                "teams": group["teams"],
            })

        return offending

    def build_contradiction_enforcement_prompt(
        self, user_query: str, response_text: str,
        offending_groups: List[Dict[str, Any]], lang: Optional[str] = None,
    ) -> str:
        """
        Prompt de corrección para los conflictos que
        `find_unattributed_contradiction()` marcó como resueltos en
        silencio. Mismo contrato que el resto: "" si no hay nada.
        """
        if not offending_groups:
            return ""

        detail_lines: List[str] = []
        for group in offending_groups:
            teams = ", ".join(sorted(group["teams"])[:4]) or "el mismo partido"
            values = ", ".join(sorted(group["values"]))
            chosen = ", ".join(sorted(group["claimed"]))
            detail_lines.append(f"  · {teams}: las fuentes dicen {values}; tú afirmaste solo {chosen}")
        detail = "\n".join(detail_lines)

        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"
        if is_en:
            return (
                f"Original user question: {user_query}\n\n"
                "The retrieved sources DISAGREE about the result, and your answer picked "
                "one value silently, presenting it as the confirmed result:\n"
                f"{detail}\n"
                "Rewrite your final answer. You MUST state that the sources disagree and "
                "say which value each one reports, instead of asserting a single result as "
                "confirmed. Write only the corrected answer, without explaining the correction."
            )
        return (
            f"Pregunta original del usuario: {user_query}\n\n"
            "Las fuentes recuperadas NO coinciden sobre el resultado, y tu respuesta eligió "
            "un valor en silencio, presentándolo como el resultado confirmado:\n"
            f"{detail}\n"
            "Reescribe tu respuesta final. DEBES decir que las fuentes discrepan e indicar "
            "qué valor reporta cada una, en vez de afirmar un único resultado como "
            "confirmado. Escribe únicamente la respuesta corregida, sin explicar la corrección."
        )

    def build_scheduled_event_grounding_rule(
        self, lang: Optional[str] = None, user_query: Optional[str] = None,
    ) -> str:
        """
        Regla de grounding contra el fallo real observado: el usuario
        pregunta por la final de un torneo FUTURO/programado ("final del
        Mundial 2026") con fuentes web que solo confirman sede/fechas —
        el modelo (qwen2.5:7b) inventó una "Copa del Mundo 2023" ganada
        por España 1-0 a Argentina: un torneo, una edición y un
        marcador que no existen en ninguna fuente.

        A diferencia de `build_year_mismatch_warning()` (dispara solo
        cuando el año pedido NO aparece en la evidencia — un chequeo
        determinista por regex), este caso no es un desajuste de año:
        "2026" sí puede aparecer en las fuentes (sede, calendario,
        fase clasificatoria) sin que eso implique que el torneo ya
        terminó. Determinar de forma determinista "¿ya se jugó la
        final?" a partir de texto libre no es viable con regex, así que
        esta regla es una instrucción condicional para el propio
        modelo — se antepone SIEMPRE que hay contexto web (sin
        detección previa), y es el modelo quien decide si aplica según
        lo que efectivamente diga la evidencia.

        BLINDAJE (bug real, MEDIDO): la versión ESTRICTA de esta regla
        (clasificar PASADO/EN CURSO/FUTURO, "DEBES declarar que el
        evento está programado" si las fuentes no confirman un partido
        jugado) se antepone SIEMPRE, sin importar cuán obviamente
        histórico sea el evento — turno real capturado: "final del
        Mundial 2014" (evento de hace más de una década, con marcador
        confirmado en la evidencia) produjo de todos modos "las fuentes
        no dan detalles específicos sobre la fecha", seguido en la
        MISMA respuesta por la fecha exacta — el modelo aplicó
        mecánicamente el hábito de cobertura de esta regla incluso
        cuando no aplicaba. `user_query` (opcional, default None — si
        no se pasa, cae al comportamiento anterior) permite calcular si
        TODOS los años que el propio usuario mencionó ya quedaron
        claramente atrás (con 1 año de margen, para no tropezar con "el
        Mundial de este año" a mitad de un torneo en curso); en ese
        caso se usa una versión LIGERA que conserva la prohibición de
        inventar datos pero sin la instrucción de "declarar programado/
        futuro", que es la que generaba la contradicción. Sin año
        explícito en la consulta (o con un año ambiguo/futuro), se
        mantiene la versión ESTRICTA completa — el caso original que
        motivó esta regla (Mundial 2026 sin marcador real todavía) NO
        se debilita.

        Devuelve el bloque de texto listo para anteponer al resto de
        instrucciones de contexto web — nunca vacío (a diferencia de
        `build_year_mismatch_warning()`, que sí puede devolver "").
        """
        is_en = (lang or getattr(self, "current_language", "Spanish")) == "English"

        # La fecha de HOY, tomada del reloj del sistema en cada llamada.
        # Sin este dato el modelo no tiene forma de decidir si la fecha
        # consultada ya pasó: su único referente temporal es su corte de
        # entrenamiento, así que trata como "futuro" cualquier año
        # posterior a ese corte (y, simétricamente, acepta como
        # "histórico" un artículo especulativo sobre un evento que aún no
        # ocurrió). Las reglas de abajo eran puramente cualitativas
        # ("si el evento es futuro...") y le pedían al modelo una
        # comparación temporal que no podía hacer con los datos que
        # tenía. Ahora se le entrega el dato duro y la comparación pasa a
        # ser trivial.
        today = datetime.now()
        today_iso = today.strftime("%Y-%m-%d")
        current_year = today.year

        # ver la nota arriba: solo se ablanda cuando todos los años que
        # el usuario mencionó explícitamente ya quedaron atrás, con 1
        # año de margen (evita ablandar por error un evento "de este
        # año" que podría seguir en curso). Sin año en la consulta -
        # caso más común y también el más ambiguo - se mantiene la
        # regla ESTRICTA sin cambios.
        query_years: Set[str] = _extract_years(user_query) if user_query else set()
        is_unambiguously_past = bool(query_years) and all(
            int(y) < current_year for y in query_years
        )
        if is_unambiguously_past:
            if is_en:
                return (
                    "[GROUNDING RULE — CONFIRMED PAST EVENT]:\n"
                    f"0. Today's date is {today_iso}. The event asked about already happened.\n"
                    "1. It is STRICTLY FORBIDDEN to invent scores, final results, winners, or "
                    "non-existent editions.\n"
                    "2. Use only the facts actually confirmed in the sources below. If a "
                    "specific detail you need genuinely is NOT in the sources, say so plainly "
                    "for that detail only — do not add a blanket disclaimer about missing "
                    "information when the sources do contain the answer.\n\n"
                )
            return (
                "[REGLA DE GROUNDING — EVENTO PASADO CONFIRMADO]:\n"
                f"0. La fecha de hoy es {today_iso}. El evento consultado ya ocurrió.\n"
                "1. Queda ESTRICTAMENTE PROHIBIDO inventar marcadores, resultados finales, "
                "ganadores o ediciones inexistentes.\n"
                "2. Usa únicamente los hechos efectivamente confirmados en las fuentes de "
                "abajo. Si un dato puntual que necesitás genuinamente NO está en las fuentes, "
                "decilo con claridad solo para ese dato — no agregues una advertencia general "
                "sobre falta de información cuando las fuentes sí contienen la respuesta.\n\n"
            )

        if is_en:
            return (
                "[TRUTHFULNESS RULE — SCHEDULED / FUTURE EVENTS]:\n"
                f"0. TODAY'S DATE IS {today_iso} (current year: {current_year}). This is "
                "authoritative and overrides any assumption from your training data. Before "
                "answering anything time-sensitive, FIRST compare the date of the event in "
                "question against today's date and classify it as PAST, ONGOING, or FUTURE.\n"
                "1. If the event is in the FUTURE relative to today, or the sources below do "
                "NOT contain a final match actually played or a declared champion, you MUST "
                "explicitly state that the event is scheduled or still in a "
                "qualifying/upcoming stage.\n"
                "2. It is STRICTLY FORBIDDEN to invent scores, final results, winners, or "
                "non-existent editions (e.g. a 'World Cup 2023' that does not exist).\n"
                "3. Treat previews, predictions, projections, simulations, and speculative "
                "articles as SPECULATION, never as a record of something that happened — even "
                "if they are written in the past tense. If a source describes a result for an "
                "event that has not occurred as of today's date, say so explicitly instead of "
                "reporting it as fact.\n"
                "4. Limit yourself ONLY to venue, date, stadium, or current competition status "
                "data actually confirmed in the sources below.\n\n"
            )
        return (
            "[REGLA DE VERACIDAD Y EVENTOS PROGRAMADOS]:\n"
            f"0. LA FECHA DE HOY ES {today_iso} (año actual: {current_year}). Este dato es "
            "autoritativo y prevalece sobre cualquier suposición de tus datos de "
            "entrenamiento. Antes de responder algo sensible al tiempo, PRIMERO compara la "
            "fecha del evento consultado contra la fecha de hoy y clasifícalo como PASADO, EN "
            "CURSO o FUTURO.\n"
            "1. Si el evento es FUTURO respecto a hoy, o las fuentes de abajo no contienen un "
            "partido final efectivamente celebrado ni un campeón declarado, DEBES declarar "
            "explícitamente que el evento está programado o en fase clasificatoria.\n"
            "2. Queda ESTRICTAMENTE PROHIBIDO inventar marcadores, resultados finales, "
            "ganadores o ediciones inexistentes (como 'Copa del Mundo 2023').\n"
            "3. Trata las previas, predicciones, proyecciones, simulaciones y artículos "
            "especulativos como ESPECULACIÓN, nunca como registro de algo ocurrido — aunque "
            "estén redactados en pasado. Si una fuente describe un resultado de un evento que "
            "a fecha de hoy todavía no ha ocurrido, dilo explícitamente en vez de reportarlo "
            "como un hecho.\n"
            "4. Limítate a responder ÚNICAMENTE con los datos de sede, fechas, estadio o "
            "estado actual de la competición efectivamente confirmados en las fuentes de "
            "abajo.\n\n"
        )

    # Nota (medido 2026-08-19): tope de palabras para que
    # Capa 3 acepte un turno anterior como topic_anchor - ver el
    # comentario en el bucle de extracción más abajo. Un tema real
    # ("2013 Jiuzhaigou earthquake", "world cup 2026 final") nunca
    # necesita más de esto; una respuesta redactada completa sí.
    _TOPIC_ANCHOR_MAX_WORDS: int = 12

    def _build_contextual_search_query(
        self,
        user_input: str,
        log_cb: Optional[Callable[[str], None]] = None,
        lang: Optional[str] = None,
    ) -> str:
        """
        Motor de Reescritura Contextual v3.0 (3 Capas):
        1. Resolución Semántica vía LLM (Inferencia ultra-rápida).
        2. Análisis Gramatical & Deíctico (Fallback heurístico avanzado).
        3. Fusión de Ancla de Entidad (Limpieza de contexto previo).

        `lang` (opcional, default None -> cae a `self.current_language`):
        BLINDAJE (bug real, MEDIDO): la reescritura vía LLM (Capa 1) usaba
        SIEMPRE `self.current_language` — el idioma de SESIÓN/UI — para
        elegir el system prompt de `rewrite_search_query_via_llm`, sin
        importar en qué idioma estuviera escrito el mensaje de ESTE
        turno. Con la UI en español y un mensaje en inglés ("Search in
        internet the final of world cup 2022 tell me all details"), el
        modelo recibía un system prompt en español para reescribir un
        mensaje en inglés y producía una query mezclada ("final World
        Cup 2022 detalles") — el término suelto en español degrada el
        ranking de Wikipedia/DuckDuckGo (ver bug real: la búsqueda
        devolvió Alejandro Garnacho / Lionel Scaloni / Ángel Correa /
        Independiente del Valle en vez del artículo de la final). El
        idioma EFECTIVO del turno (`_resolve_turn_language()`, ya
        calculado por el llamador) es la señal correcta acá — mismo
        criterio que ya se usa para el idioma de la respuesta final.

        `log_cb` (opcional, default None — cero cambio para llamadores
        que no lo pasan): BLINDAJE de un hueco real de diagnóstico — la
        query FINAL que efectivamente se le manda a Wikipedia/DuckDuckGo
        solo se logueaba vía `logger.info`, invisible en la terminal
        gráfica (ver logger.py). Sin verla, un "0 resultados" de
        Wikipedia para un mensaje que minutos antes SÍ encontró
        resultados es indistinguible entre "el proveedor externo falló"
        y "la query reescrita cambió/se rompió" — este puente resuelve
        esa ambigüedad mostrando el texto real enviado.
        """
        def _emit(msg: str) -> None:
            logger.info(msg)
            if log_cb is not None:
                with contextlib.suppress(Exception):
                    log_cb(msg)
        # Nota (requisito de higiene de queries): antes esto era una
        # regex ad-hoc que solo cubría un puñado de palabras sueltas
        # ("search", "busca", "hoy"...) y no manejaba saludos encadenados
        # ni ruido en medio de la frase. sanitize_query() (web_search.py)
        # ya resuelve todo eso - bucle iterativo de rellenos iniciales,
        # ruido de cierre, muletillas internas ("busca en internet"/"por
        # favor" en cualquier posición) - y es la misma función que ya
        # usa Capa 3 al final (sanitize_query(fused_query)); usarla aquí
        # también evita tener dos niveles de limpieza distintos y
        # potencialmente inconsistentes dentro de la misma función.
        clean_input = sanitize_query(user_input)

        # =================================================================
        # Nota (Punto de fallo 5): sustitución referencial
        # =================================================================
        # Se resuelve antes que la Capa 1 porque es una señal
        # inequívoca de que el sujeto real vive en el historial, no en
        # el mensaje actual - y en la práctica la Capa 1 (LLM) ya
        # demostró fallar exactamente en este patrón (ver bug en
        # el docstring de _SUBSTITUTION_PATTERN_RE arriba).
        substitution_match = _SUBSTITUTION_PATTERN_RE.search(clean_input)
        if substitution_match:
            recent_history_sub = self.memory_graph.get_recent_history(limit=4)
            subject_noun = _extract_subject_noun(recent_history_sub)

            if subject_noun:
                substituted = sanitize_query(
                    _SUBSTITUTION_PATTERN_RE.sub(subject_noun, clean_input, count=1)
                )
                if substituted:
                    _emit(f"🔁 [QueryRewrite-Substitution] '{clean_input}' -> '{substituted}'")
                    return substituted

            # La regla no encontró un sujeto claro en el diccionario de
            # temas: se fuerza una minillamada de reformulación al
            # modelo antes de resignarse a la fusión cruda.
            try:
                llm_query = self.rewrite_search_query_via_llm(user_input, log_cb=log_cb, lang=lang)
                if llm_query:
                    # Nota (medido): esta llamada nunca
                    # pasaba por sanitize_query() antes de devolverse -
                    # a diferencia de la Capa 3 más abajo, que sí sanea
                    # su fusión antes de retornarla. Un modelo de 3B con
                    # `num_ctx=768` y hasta 3 turnos de historial real
                    # como contexto no siempre sigue al pie de la letra
                    # la instrucción de "quitar verbos de acción" de su
                    # propio system prompt - la consulta reescrita puede
                    # arrastrar "search"/"internet" sueltos, exactamente
                    # el ruido que degrada el ranking de Wikipedia (ver
                    # sanitize_query en web_search.py). Sanear la salida
                    # del LLM es una red de seguridad barata: si ya
                    # estaba limpia, sanitize_query no le cambia nada.
                    llm_query = sanitize_query(llm_query)
                if llm_query:
                    _emit(f"🧠 [QueryRewrite-LLM-Escalation] Query tras sustitución fallida: '{llm_query}'")
                    return llm_query
            except Exception as exc:
                logger.debug("Escalamiento LLM tras sustitución fallida también falló: %s", exc)

            # Último recurso: sustantivos crudos del último turno del
            # usuario - nunca se deja la frase de sustitución sin
            # resolver en la query final.
            fallback_subject = _extract_last_user_turn_nouns(recent_history_sub)
            if fallback_subject:
                substituted = sanitize_query(
                    _SUBSTITUTION_PATTERN_RE.sub(fallback_subject, clean_input, count=1)
                )
                if substituted:
                    _emit(f"📌 [QueryRewrite-LastUserTurn] '{clean_input}' -> '{substituted}'")
                    return substituted

        # =================================================================
        # CAPA 1: Reescritura Semántica por LLM Local
        # =================================================================
        try:
            llm_query = self.rewrite_search_query_via_llm(user_input, log_cb=log_cb, lang=lang)
            if llm_query:
                # Nota (medido): mismo motivo que el
                # escalamiento de arriba - esta era la ruta que de
                # verdad tomaba un mensaje típico ("Search on internet
                # all the details of..."), y su salida nunca se saneaba
                # antes de mandarse tal cual a Wikipedia/DuckDuckGo.
                llm_query = sanitize_query(llm_query)
            if llm_query:
                _emit(f"🧠 [QueryRewrite-LLM] Query contextualizada: '{llm_query}'")
                return llm_query
        except Exception as exc:
            logger.debug("Fallo en reescritura LLM, activando Capa 2 (Heurística Avanzada): %s", exc)

        # =================================================================
        # CAPA 2: Detección Gramatical y Deíctica (Fallback Ultra-Rápido)
        # =================================================================
        # Pronombres demostrativos, posesivos y neutros
        deictic_tokens = {
            "this", "that", "it", "they", "them", "these", "those",
            "este", "esta", "esto", "ese", "esa", "eso", "aquel", "aquella",
            "estos", "estas", "esos", "esas", "su", "sus"
        }

        # Sustantivos categóricos que carecen de sentido sin entidad propia
        category_nouns = {
            "final", "match", "game", "winner", "score", "result", "player", "team", "price", "event",
            "partido", "resultado", "ganador", "precio", "jugador", "equipo", "campeon", "campeón", "evento"
        }

        # Aperturas que indican dependencia conversacional o pregunta de seguimiento
        continuation_starters = (
            "how", "why", "who", "where", "and", "what about", "tell me about", "now", "also",
            "cómo", "como", "por qué", "porque", "quién", "quien", "dónde", "donde", "y", "qué tal",
            "cuéntame de", "ahora", "también", "tambien"
        )

        words = [w.lower().strip("?,.!") for w in clean_input.split()]
        lowered_input = clean_input.lower()

        has_deictic = any(w in deictic_tokens for w in words)
        has_category_noun = any(w in category_nouns for w in words)
        starts_with_continuation = lowered_input.startswith(continuation_starters)

        # Nota (bug observado): "final" está en category_nouns
        # y por sí solo dispara needs_context=True - correcto para "¿cómo
        # quedó esa final?", pero catastrófico para "world cup 2022
        # final, how was": ese mensaje YA es autocontenido (el año
        # resuelve toda la ambigüedad que "final" solo no resolvía), y
        # fusionarlo igual con el historial reciente (saturado de OTRO
        # año, p. ej. 2026) es precisamente lo que producía la consulta
        # de búsqueda equivocada. Un año explícito en el mensaje actual
        # gana siempre sobre la heurística de sustantivo ambiguo.
        has_explicit_year = bool(_YEAR_RE.search(clean_input))
        if has_explicit_year:
            has_category_noun = False

        # Si hay pronombres, sustantivos ambiguos, inicio conversacional o es muy corto, EXIGE CONTEXTO
        needs_context = has_deictic or has_category_noun or starts_with_continuation or len(words) < 5

        if has_explicit_year and not (has_deictic or starts_with_continuation):
            # Autocontenido de verdad: tiene año propio y ningún pronombre
            # ni apertura conversacional que dependa del turno anterior.
            needs_context = False

        if not needs_context:
            return clean_input

        # =================================================================
        # CAPA 3: Extracción Limpia y Fusión de Entidades del Historial
        # =================================================================
        recent_history = self.memory_graph.get_recent_history(limit=3)
        if not recent_history:
            return clean_input

        topic_anchor = ""
        for turn in reversed(recent_history):
            clean_turn = re.sub(r"^(user|assistant):\s*", "", turn, flags=re.IGNORECASE).strip()
            if not clean_turn:
                continue

            # Nota (medido): un turno del ASISTENTE que es
            # solo relleno conversacional o un mensaje de fallback del
            # propio sistema no es un TEMA, pero antes de este chequeo
            # se aceptaba igual como topic_anchor apenas sobrevivía
            # no-vacío a la limpieza de palabras sueltas de abajo. Caso
            # real observado: el usuario saludó ("hola"), el asistente
            # respondió "¡Hola! ¿Cómo estás? ¿En qué puedo ayudarte
            # hoy?", y el SIGUIENTE turno ("Search in internet the last
            # world cup final") fusionó esa respuesta genérica como si
            # fuera el tema - la query que llegó a DuckDuckGo/Wikipedia
            # terminó siendo "Cómo estás? ¿qué puedo ayudarte hoy? [...]
            # world cup final", que ninguna fuente real podía responder
            # (de ahí el "No results found" que el log le atribuía al
            # proveedor, cuando la causa real era la query).
            #
            # _CONVERSATIONAL_QUERY_OPENER_RE/_CONVERSATIONAL_QUERY_
            # MARKER_RE ya existían y ya estaban probados - pero SOLO se
            # aplicaban en _sanitize_llm_query_output (Capa 1, salida del
            # LLM). Se reutilizan aquí para Capa 3 (fusión heurística con
            # el historial), sumando los fallbacks de sistema que esas
            # dos regex no cubren (mensajes de error, el aviso de "no
            # pude producir una respuesta") - mismo criterio en los tres
            # casos: nunca aceptar un turno del asistente que no tiene
            # tema real. Se evalúa sobre el texto SIN limpiar todavía
            # (antes de la remoción de palabras sueltas de abajo), porque
            # _NO_VISIBLE_ANSWER_ES/EN son oraciones completas que una
            # comparación exacta post-limpieza ya no reconocería.
            if clean_turn.startswith("[ERROR") or clean_turn in (
                self._NO_VISIBLE_ANSWER_ES, self._NO_VISIBLE_ANSWER_EN,
            ):
                continue

            # Removemos verbos de acción y comandos de búsqueda para dejar solo el Tema
            clean_turn = re.sub(
                r"(?i)\b(search|internet|busca|buscar|en|hola|hello|please|por favor|dime|show|find)\b",
                "",
                clean_turn
            ).strip()
            if not clean_turn:
                continue
            # Nota (medido): un turno del asistente puede
            # ser sustantivo (no "conversacional" en el sentido de las
            # dos regex de arriba) y aun así ser sobre un tema TOTALMENTE
            # distinto al del mensaje actual - el caso real es "The
            # sources do not contain information about the last
            # earthquake in Austria...", que no matchea ningún opener/
            # marker conversacional (es una oración factual real) pero
            # tampoco es un TEMA para fusionar con "how the last nintendo
            # live was": es la respuesta de un turno anterior sobre otro
            # asunto. `_CONVERSATIONAL_QUERY_NARRATION_RE` reconoce esta
            # familia de frases ("las fuentes no contienen...", "no
            # information available"...) y las descarta igual que a las
            # conversacionales - mismo criterio, un caso más.
            if (
                self._CONVERSATIONAL_QUERY_OPENER_RE.match(clean_turn)
                or self._CONVERSATIONAL_QUERY_MARKER_RE.search(clean_turn)
                or self._CONVERSATIONAL_QUERY_NARRATION_RE.search(clean_turn)
            ):
                continue

            # Nota (medido 2026-08-19): un turno del
            # asistente puede ser una respuesta factual COMPLETA y
            # correcta (no dispara ningún opener/marker/narration de
            # arriba porque no es una disculpa ni una plantilla de "sin
            # información") y aun así ser inservible como topic_anchor.
            # Caso real: pregunta del usuario "the last earthquake in
            # china", respuesta del asistente "The last earthquake in
            # China that I can confirm from my knowledge is the 2013
            # Jiuzhaigou earthquake...". El siguiente turno del usuario
            # ("tell me how the water is composed for") es corto (< 5
            # palabras) y dispara needs_context, así que esa respuesta
            # completa se fusionó tal cual con el mensaje nuevo - la
            # query que llegó al buscador terminó siendo sobre el
            # terremoto de 2013, no sobre agua. Un TEMA real cabe en
            # pocas palabras (un nombre, un evento, una entidad); una
            # ORACIÓN de más de _TOPIC_ANCHOR_MAX_WORDS palabras es una
            # respuesta redactada, no un tema - se descarta en vez de
            # fusionarse cruda.
            if len(clean_turn.split()) > self._TOPIC_ANCHOR_MAX_WORDS:
                continue

            topic_anchor = clean_turn[:100]
            break

        if topic_anchor:
            # Nota: si el mensaje actual ya trae su propio año
            # explícito, cualquier año DISTINTO que venga en el
            # topic_anchor (arrastrado del historial reciente) se
            # descarta antes de fusionar - evita que la query resultante
            # traiga dos años en conflicto y el buscador (o el propio
            # LLM aguas abajo) termine privilegiando el año equivocado.
            if has_explicit_year:
                current_years = _extract_years(clean_input)
                for stale_year in _extract_years(topic_anchor) - current_years:
                    topic_anchor = re.sub(rf"\b{stale_year}\b", "", topic_anchor).strip()
                topic_anchor = _MULTI_SPACE_RE.sub(" ", topic_anchor).strip()

            fused_query = f"{topic_anchor} {clean_input}".strip()
            _emit(f"🔗 [QueryRewrite-Heuristic] Tema fusionado: '{fused_query}'")
            return sanitize_query(fused_query)

        return clean_input

    # =================================================================
    # TAMAÑO DEL MODELO ACTIVO - CALIBRACIÓN DE RIGOR (pedido explícito)
    # =================================================================
    # Varias reglas defensivas de este archivo (la regla de confianza del
    # bloque de instrucciones web, ver sovnode_qt.py, y en su momento
    # `_final_answer_instruction_tail`/`build_scheduled_event_grounding_
    # rule`) documentan el mismo patrón medido repetidas veces en este
    # proyecto: un modelo de 3B, bajo un prompt con varias reglas de
    # "prohibido inventar", tiende a agregar dudas/disclaimers reflexivos
    # incluso cuando el dato sí está confirmado en la evidencia - un
    # modelo más grande (7B) no muestra el mismo problema con el mismo
    # prompt. Este helper centraliza la detección de "modelo chico" a
    # partir del tag de Ollama, para que cualquier ajuste de calibración
    # por tamaño (presente o futuro) lo use en vez de reinventar el
    # parseo del tag en cada sitio.
    _MODEL_SIZE_SUFFIX_RE: Pattern[str] = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)

    @classmethod
    def _model_param_size_billions(cls, model_tag: str) -> Optional[float]:
        """
        Extrae el tamaño de parámetros (en miles de millones) del tag de
        un modelo de Ollama — "qwen2.5:3b" -> 3.0, "qwen2.5-coder:7b" ->
        7.0, "llama3.1:70b-instruct" -> 70.0 (toma el ÚLTIMO sufijo "Nb"
        del tag completo, por si hay más de uno).

        None si no se encuentra ningún sufijo de tamaño — un tag
        desconocido/no reconocido NUNCA se trata como "modelo chico" por
        el llamador (ver `_is_small_model`): conservador por diseño, no
        relaja rigor a ciegas ante un tag que no se pudo interpretar.
        """
        if not model_tag:
            return None
        matches = cls._MODEL_SIZE_SUFFIX_RE.findall(model_tag)
        if not matches:
            return None
        try:
            return float(matches[-1])
        except ValueError:
            return None

    @classmethod
    def _is_small_model(cls, model_tag: str, threshold_billions: float = 3.0) -> bool:
        """
        True si el modelo activo tiene <= `threshold_billions` de
        parámetros según su propio tag. Tamaño desconocido (sin sufijo
        "Nb" reconocible) devuelve False — ver el docstring de
        `_model_param_size_billions`.
        """
        size = cls._model_param_size_billions(model_tag)
        return size is not None and size <= threshold_billions

    # Cota de longitud para la salida de la reescritura vía LLM: una
    # consulta de búsqueda real rara vez supera este largo. Si el modelo
    # la excede, es señal de que ignoró las instrucciones y escribió
    # una oración/párrafo completo - mejor no confiar en esa salida que
    # mandarle un párrafo entero al buscador.
    _QUERY_REWRITE_MAX_CHARS: int = 150
    _QUERY_REWRITE_MIN_CHARS: int = 2

    # ENDURECIMIENTO (tope de palabras clave, pedido explícito): el
    # system prompt pide "como máximo 5 palabras clave", pero no hay
    # forma de forzar un conteo de palabras vía el esquema JSON de
    # `format` (ese solo garantiza forma, no longitud del valor) - un
    # 3B puede seguir devolviendo una entidad larga bajo presión de
    # contexto. Se permite algo de margen (entidades reales como "final
    # Copa Mundial de la FIFA 2026" ya son 5 palabras por sí solas antes
    # de sumar un calificador como "resultado" o "ganador") pero por
    # encima del margen se rechaza entero en vez de truncar - truncar a
    # ciegas podría cortar una entidad a la mitad ("2022 FIFA World" sin
    # "Cup final") y mandar una búsqueda peor que la determinista.
    _QUERY_REWRITE_MAX_KEYWORDS: int = 5
    _QUERY_REWRITE_KEYWORD_SLACK: int = 3
    # Timeout corto y deliberadamente distinto de OLLAMA_TIMEOUT_SECONDS
    # (180s): esta es una llamada utilitaria de una sola línea de salida,
    # no una generación completa - si tarda más que esto, algo anda mal
    # y conviene degradar rápido al método determinista en vez de hacer
    # esperar al usuario por una consulta de búsqueda.
    QUERY_REWRITE_TIMEOUT_SECONDS: float = 15.0

    # Patrones que delatan que el modelo respondió como un asistente
    # conversacional en vez de emitir una consulta de búsqueda - el bug
    # real observado: para "search on internet how the world cup final
    # 2026 was", el modelo devolvió "How can I assist you today
    # regarding the..." Esa cadena tiene 44 caracteres, así que pasaba
    # limpio por el único chequeo que existía (longitud) y se mandaba
    # tal cual al buscador como si fuera la consulta.
    _CONVERSATIONAL_QUERY_OPENER_RE: Pattern[str] = re.compile(
        r"^(?:"
        r"how can i|how may i|how could i|i can help|i'd be happy|i would be happy|"
        r"i'm happy to|i am happy to|sure[,!.]|of course[,!.]|certainly[,!.]|"
        r"claro[,!.]|por supuesto[,!.]|con gusto|"
        r"hola[,!.¡]|hi[,!.]|hello[,!.]|hey[,!.]|"
        r"¡?en qué puedo|puedo ayudarte|te puedo ayudar|"
        r"as an ai|i'?m an ai|como (?:un |una )?(?:modelo|ia|asistente)|"
        # Nota (medido): cuarta variante - el modelo no
        # ofrece ayuda ni narra el contexto (las dos ya cubiertas), sino
        # que se DISCULPA, generalmente al notar algo raro en el propio
        # mensaje del usuario (un typo real o percibido) y queda
        # divagando sobre eso en vez de emitir la consulta. Caso real
        # capturado: para "the last earthquarke in china", la
        # reescritura devolvió "Lo siento, pero parece que hubo un
        # pequeño error en tu [...]. Parece que has escrito 'earthquake'
        # (terremoto) en lugar de 'earthquake' (ter[...]" - se mandó tal
        # cual a DuckDuckGo como si fuera la consulta de búsqueda.
        r"lo siento|perd[oó]n|disculpa|i'?m sorry|i apologi[sz]e|apologies|"
        # Nota (medido 2026-08-19): quinta variante - el
        # modelo no conversa, no narra el historial ni se disculpa, sino
        # que ACUSA RECIBO de la instrucción y anuncia la acción que va a
        # tomar, en vez de emitir directamente la consulta. Caso real
        # capturado: para "Search in internet the last earthquarke in
        # china", la reescritura devolvió "Understood. I will perform an
        # internet [search] to the most recent earthquake in China. Here
        # is the information I [...]" - no matchea ningún patrón previo
        # (no es saludo, disculpa ni oferta de ayuda) y se mandó tal cual
        # como query, tanto a los motores de búsqueda como, arrastrada en
        # el turno siguiente, al fusionador de Capa 3.
        r"understood[,.!]|entendido[,.!]|i will (?:perform|search|look|check|do|conduct)|"
        r"voy a (?:buscar|realizar|hacer|consultar)|let me (?:search|check|look|find)|"
        r"permite(?:me)?|d[ée]jame (?:buscar|revisar|consultar)|"
        r"here is the information|aqu[ií] (?:est[aá]|tienes) la informaci[oó]n"
        r")",
        re.IGNORECASE,
    )
    _CONVERSATIONAL_QUERY_MARKER_RE: Pattern[str] = re.compile(
        r"\b(assist you|help you|ayudarte|ayudarle|puedo ayudar)\b",
        re.IGNORECASE,
    )

    # Nota (medido): tercera variante del mismo problema -
    # el modelo no "conversa/ofrece ayuda" (eso ya lo cubre el patrón de
    # arriba) sino que NARRA, citando un fragmento de un turno ANTERIOR
    # de `context_block` (el historial reciente que se le pasa a esta
    # reescritura) en vez de transformar el mensaje ACTUAL. Caso real
    # capturado: para "how the last nintendo live was" -con turnos
    # previos de la misma conversación sobre terremotos, de un tema
    # totalmente distinto- la reescritura devolvió "Based on the
    # information provided in the web context, there is no specific
    # mention of the last earth[quake] how the last nintendo live
    # was": una mezcla del cierre de la respuesta anterior con el
    # mensaje nuevo. 133 caracteres, bajo _QUERY_REWRITE_MAX_CHARS, y no
    # empieza con ningún opener conversacional - pasaba limpio por los
    # dos chequeos existentes y se mandaba tal cual al buscador. La
    # memoria conversacional persiste a propósito entre reinicios de la
    # app (perder el hilo al reiniciar sería peor), así que el fix va
    # acá, no en acortar esa persistencia: cualquier fragmento de prosa
    # EXPLICATIVA (en vez de una consulta corta de palabras clave) es
    # sospechoso sin importar de dónde salió.
    # Ensanchado (nota) para cubrir también la familia de frases "no
    # tengo/no encontré información" que el propio SYSTEM_PROMPT instruye
    # usar cuando una búsqueda falla (ver la sección "REGLA ANTI-
    # NEGATIVA DE BÚSQUEDA WEB" / "[SYSTEM NOTICE - NO REAL-TIME DATA]")
    # - precisamente el tipo de turno del ASISTENTE que más se repite en
    # una sesión con fallos de búsqueda reales, y por tanto el candidato
    # más probable a colarse como `topic_anchor` en la Capa 3 de abajo.
    _CONVERSATIONAL_QUERY_NARRATION_RE: Pattern[str] = re.compile(
        r"\b("
        r"based on the (?:information|context|sources)|"
        r"there is no (?:specific )?mention|"
        r"the (?:provided|web) context (?:does not|doesn'?t|covers)|"
        r"the sources (?:do not|don'?t) (?:contain|report|confirm|mention)|"
        r"according to the (?:sources|context|information)|"
        r"no (?:real-time |specific |precise )?information (?:is )?available|"
        r"(?:i )?(?:do not|don'?t) have (?:real-time |specific |precise )?information|"
        r"seg[uú]n la informaci[oó]n proporcionada|"
        r"no (?:hay|existe|tengo|dispongo de) (?:informaci[oó]n|menci[oó]n)|"
        r"el contexto (?:proporcionado|web) no|"
        r"las fuentes (?:no contienen|no reportan|no confirman)|"
        # Nota (medido 2026-08-19): respuesta factual
        # completa apoyada en conocimiento propio del modelo (no en
        # fuentes web) - el mismo problema que las frases de "sin
        # información" de arriba, pero en la dirección opuesta: el
        # modelo sí contestó, con confianza, y esa respuesta completa
        # terminó fusionada como topic_anchor del turno siguiente.
        r"(?:that |which )?i can confirm|from my knowledge|based on my knowledge|"
        r"seg[uú]n mi conocimiento|de acuerdo a mi conocimiento"
        r")\b",
        re.IGNORECASE,
    )

    @classmethod
    def _sanitize_llm_query_output(cls, raw: str) -> Optional[str]:
        if not raw:
            return None

        text = raw.strip()
        text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
        text = re.sub(r"^```(?:\w+)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

        # ENDURECIMIENTO (salida delimitada, pedido explícito): con
        # `format` fijado a un esquema JSON en el payload (ver
        # rewrite_search_query_via_llm), `raw` debería ser directamente
        # `{"search_terms": "..."}` - se intenta parsear PRIMERO. Si
        # falla (Ollama sin soporte de structured outputs, que ignora
        # `format` por completo y devuelve texto libre igual que antes),
        # se cae al parseo de texto libre de siempre, sin romper
        # compatibilidad hacia atrás.
        candidate: Optional[str] = None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            raw_terms = parsed.get("search_terms")
            if isinstance(raw_terms, str):
                candidate = raw_terms.strip()

        if candidate is not None:
            text = candidate
        else:
            text = text.strip("\"'“”‘’`").strip()
            for line in text.splitlines():
                line = line.strip().strip("\"'“”‘’`").strip()
                if line:
                    text = line
                    break
            else:
                return None
            text = re.sub(r"^(consulta|query|json)\s*:\s*", "", text, flags=re.IGNORECASE).strip()

        # Intercepta el token NONE (tanto suelto como valor del JSON)
        # para evitar búsquedas web basura.
        if text.upper() == "NONE":
            return None

        if len(text) < cls._QUERY_REWRITE_MIN_CHARS or len(text) > cls._QUERY_REWRITE_MAX_CHARS:
            return None

        # ENDURECIMIENTO (tope de palabras clave): ver comentario junto a
        # _QUERY_REWRITE_MAX_KEYWORDS arriba - se rechaza entero en vez
        # de truncar.
        if len(text.split()) > cls._QUERY_REWRITE_MAX_KEYWORDS + cls._QUERY_REWRITE_KEYWORD_SLACK:
            return None

        if (
            cls._CONVERSATIONAL_QUERY_OPENER_RE.match(text)
            or cls._CONVERSATIONAL_QUERY_MARKER_RE.search(text)
            or cls._CONVERSATIONAL_QUERY_NARRATION_RE.search(text)
        ):
            return None

        return text

    def rewrite_search_query_via_llm(
        self,
        user_input: str,
        target_model: Optional[str] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        lang: Optional[str] = None,
    ) -> Optional[str]:
        """
        Usa el propio modelo local (no regex) para producir la consulta
        de búsqueda óptima a partir del mensaje del usuario y el
        contexto reciente de la conversación.

        Por qué esto es una mejora real sobre `_build_contextual_search_query`:
        esa función decide con una heurística fija (longitud del mensaje)
        si hace falta combinar con el turno anterior — no entiende el
        contenido. Cualquier caso donde un mensaje corto SÍ sea autocontenido,
        o uno largo SÍ necesite una referencia implícita ("y ahora",
        "cuánto cuesta", contexto de hace 3 turnos en vez de 1), se le
        escapa por diseño. Un LLM no tiene esa limitación: entiende la
        referencia por significado, no por una heurística de longitud.

        Por qué NO reemplaza al método determinista: es una llamada de
        red a un proceso externo (Ollama) que puede fallar, tardar, o
        (con un modelo de 3B) simplemente no seguir el formato pedido.
        Ninguno de esos casos debe bloquear la búsqueda — se degrada a
        `_build_contextual_search_query` de inmediato y en silencio; el
        llamador es quien decide ese fallback (ver StreamTurnWorker).
        """
        if requests is None or self._session is None:
            return None

        try:
            recent_turns = self.memory_graph.get_recent_history(limit=3)
        except Exception:
            recent_turns = []
        context_block = "\n".join(recent_turns) if recent_turns else "(sin contexto previo)"

        rewrite_prompt = (
            f"Contexto: {context_block}\n"
            f"Mensaje: {user_input.strip()}\n"
            f"JSON:"
        )

        # Nota (medido): usar siempre `self.current_language`
        # (idioma de sesión/UI) acá ignoraba en qué idioma estaba escrito
        # el mensaje de este turno - con la UI en español y un mensaje en
        # inglés, el modelo recibía instrucciones en español para
        # reescribir texto en inglés y devolvía queries mezcladas ("final
        # World Cup 2022 detalles"), degradando el ranking de búsqueda.
        # `lang` (pasado por el llamador vía `_resolve_turn_language()`)
        # es la señal correcta; solo cae a la sesión si no se pasó nada.
        is_en = (lang or self.current_language) == "English"
        system_prompt = (
            self._QUERY_REWRITE_SYSTEM_PROMPT_EN if is_en
            else self._QUERY_REWRITE_SYSTEM_PROMPT_ES
        )
        payload = {
            "model": target_model or self.general_model,
            "system": system_prompt,
            "prompt": rewrite_prompt,
            "stream": False,
            # ENDURECIMIENTO (salida delimitada, pedido explícito): en vez
            # de confiar solo en que el modelo "obedezca" la instrucción
            # de texto plano de una línea, se restringe el muestreo de
            # Ollama con un esquema JSON - la respuesta cruda queda
            # garantizada como JSON válido contra este esquema en
            # cualquier Ollama con soporte de structured outputs (>=0.5).
            # `_sanitize_llm_query_output` intenta parsear esto primero y
            # solo cae al parseo de texto libre de antes si `raw` no es
            # JSON válido (Ollama viejo que ignora `format` por completo).
            "format": {
                "type": "object",
                "properties": {"search_terms": {"type": "string"}},
                "required": ["search_terms"],
            },
            "options": {
                # Salida corta y determinista a propósito: esto no es
                # una respuesta creativa, es un único objeto JSON de una
                # línea que debe ser reproducible. 60 en vez de 40 para
                # dejar margen a la envoltura `{"search_terms": "..."}`
                # (llaves, comillas, la clave) sobre las mismas ~150
                # letras de consulta que antes.
                "num_predict": 60,
                "temperature": 0.1,
                # Nota (medido vía _log_stream_perf): antes
                # 768, un num_ctx MENOR al pinneado para las llamadas de
                # turno principal (8192 - ver MemoryGovernor.PINNED_NUM_
                # CTX_*). Ollama/llama.cpp reasigna el runner COMPLETO
                # cada vez que num_ctx cambia entre llamadas sucesivas al
                # mismo modelo (mismo motivo documentado en
                # MemoryGovernor sobre Prefix Alignment), así que esta
                # mini-llamada - que corre justo antes de la Pasada 1 del
                # turno, ver _build_contextual_search_query - invalidaba
                # el runner cada vez, forzando que la Pasada 1 pagara
                # prefill completo del prompt entero (medido: 4531 tok a
                # 115.6 tok/s = 39.18s) en vez de reutilizar cualquier
                # cache. Iguala el num_ctx al pinneado para dejar de
                # pagar esa reasignación - el system prompt SIGUE siendo
                # distinto a propósito (ver comentario arriba de
                # QUERY_REWRITE_SYSTEM_PROMPT: minimalista para no
                # arrastrar el protocolo <thought>/tools a una tarea de
                # una sola línea), así que el prefijo no se comparte
                # completo entre esta llamada y la Pasada 1 - pero al
                # menos deja de forzar la reasignación de buffer que sí
                # es evitable.
                "num_ctx": self._memory_governor.PINNED_NUM_CTX_GENERAL,
                "num_thread": self._memory_governor._num_thread,
                "num_gpu": self._memory_governor.DEFAULT_NUM_GPU,
            },
            # Nota (bug, pedido explícito - medido: 9.55s de
            # QueryRewrite con 6.10s solo de `load`): esta llamada usa el
            # mismo modelo (`self.general_model`) que la Pasada 1/2 del
            # turno, pero antes era la única de las llamadas a ese modelo
            # sin `keep_alive` en su payload - Ollama aplica el
            # `keep_alive` de la petición MÁS RECIENTE como temporizador
            # de descarga del runner, así que esta llamada (que corre
            # antes de la Pasada 1, al arrancar el turno) podía dejar el
            # modelo con el keep_alive por defecto de Ollama (5m) en vez
            # del pinneado de esta app. Igualado a `self._memory_governor.
            # KEEP_ALIVE` ("30m" - vive en MemoryGovernor, NO en
            # Orchestrator; `_call_llm_raw` lo hardcodea aparte como
            # literal "30m" en vez de leerlo de acá, así que esta es la
            # única referencia real a esa constante fuera de
            # MemoryGovernor.pinned_options()) en vez de un literal propio.
            "keep_alive": self._memory_governor.KEEP_ALIVE,
        }

        # Nota (medido - ver el `⏱️ [QueryRewrite]` mal
        # etiquetado en sovnode_qt.py que motivó esto): el llamador
        # envolvía toda `_build_contextual_search_query` con un timer y
        # la etiquetaba "trabajo Python local, sin llamada a Ollama en
        # curso" - falso, porque esta función sí hace una petición HTTP
        # bloqueante a Ollama cuando llega hasta acá. Se mide la llamada
        # real por separado para que quede claro cuánto de ese tiempo es
        # red/inferencia y cuánto es overhead de Python alrededor.
        _http_call_start = time.time()
        try:
            # Mismo lock que _call_llm: esta llamada comparte la GPU/VRAM
            # con la generación principal y no debe pisarla.
            with self._llm_lock:
                response = self._session.post(
                    self.ollama_endpoint,
                    json=payload,
                    timeout=self.QUERY_REWRITE_TIMEOUT_SECONDS,
                )
            response.raise_for_status()
            data = response.json()
            raw = str(data.get("response", ""))
        except Exception as exc:
            logger.debug(
                "Reescritura de consulta vía LLM falló, se usará el método determinista: %s",
                exc,
            )
            return None
        finally:
            _http_elapsed = time.time() - _http_call_start
            _http_msg = f"⏱️ [QueryRewrite-LLM-Call] {_http_elapsed:.2f}s (llamada HTTP real a Ollama)"
            logger.debug(_http_msg)
            if log_cb is not None:
                with contextlib.suppress(Exception):
                    log_cb(_http_msg)

        # Desglose real prefill/decode de esta llamada (mismo mecanismo
        # que `_call_llm_raw`/`_log_generation_perf`, no reutilizado
        # directamente porque esta función hace su propio POST crudo en
        # vez de pasar por `_call_llm_raw`) - solo llega hasta acá en el
        # camino de éxito (una excepción arriba ya retornó None dentro
        # del `except`, así que este bloque nunca corre tras un fallo).
        with contextlib.suppress(Exception):
            self._log_generation_perf(
                data, int(data.get("eval_count", 0) or 0),
                log_cb=log_cb, label="QueryRewrite-LLM",
            )

        clean_query = self._sanitize_llm_query_output(raw)
        if clean_query is None:
            return None

        # Nota DETERMINISTA (última línea de defensa de Capa 1): la
        # regla de prioridad del system prompt de arriba es una
        # instrucción - un modelo de 3B puede ignorarla bajo suficiente
        # presión de contexto, que es exactamente lo que pasó en el bug
        # real ("world cup 2022 final" con contexto saturado de 2026
        # produjo una query sin el 2022). Esta verificación no depende de
        # que el modelo obedezca: si el mensaje del usuario trae un año
        # explícito y la query que devolvió NO lo contiene, se rechaza
        # sin excepción - el llamador cae a _build_contextual_search_query,
        # que ya tiene su propio blindaje de años (ver Capa 2/3 arriba).
        user_years = _extract_years(user_input)
        if user_years and not (user_years & _extract_years(clean_query)):
            _reject_msg = (
                f"🛡️ [QueryRewrite-LLM] Rechazada: el mensaje pedía año(s) "
                f"{sorted(user_years)} pero la query devuelta ('{clean_query}') "
                f"no los contiene — cae a Capa 2/3."
            )
            logger.warning(_reject_msg)
            if log_cb is not None:
                with contextlib.suppress(Exception):
                    log_cb(_reject_msg)
            return None

        return clean_query

    def warm_up_general_model(self, log_cb: Optional[Callable[[str], None]] = None) -> None:
        """
        Precalienta `self.general_model` en Ollama — pensada para
        dispararse UNA VEZ, desde un hilo de fondo, apenas arranca la
        app (ver MainWindow.__init__ en sovnode_qt.py), antes de que el
        usuario escriba su primer mensaje.

        BLINDAJE (pedido explícito, "recortar segundos reales de
        generación"): el runner de Ollama para un modelo no existe hasta
        la PRIMERA inferencia — esa carga (`load`) es inevitable, pero no
        tiene por qué pagarla el usuario esperando su primera respuesta.
        MEDIDO en un log real: 7.36s de `load` dentro de QueryRewrite del
        primer turno de una sesión recién abierta, con la ventana
        recién mostrada y el usuario todavía leyendo/escribiendo. Ese
        mismo costo, pagado en paralelo mientras la UI termina de armarse
        (en vez de dentro del camino crítico del primer turno real), es
        tiempo que el usuario nunca percibe como espera.

        CRÍTICO — mismas opciones que el resto del turno: `num_ctx`,
        `num_thread` y `num_gpu` acá son EXACTAMENTE los mismos valores
        pinneados que usan `rewrite_search_query_via_llm` y el
        `options_dict` de la Pasada 1/2 (ver MemoryGovernor.PINNED_NUM_
        CTX_GENERAL/_num_thread/DEFAULT_NUM_GPU). Si cualquiera de los
        tres difiriera, la PRIMERA llamada real del turno igual forzaría
        una reasignación completa del runner (mismo mecanismo, medido,
        que motivó agregar `num_thread` al `options_dict` de
        sovnode_qt.py — ver ese BLINDAJE) y este precalentado no habría
        comprado nada: habría cargado el modelo con un perfil de opciones
        que el turno real de todos modos iba a invalidar.

        `num_predict=1`: no interesa el contenido generado, solo forzar
        a Ollama a cargar los pesos en memoria con estas opciones — la
        salida real se descarta sin leerla.

        Se ejecuta bajo `_llm_lock`, igual que cualquier otra inferencia
        (evita competir por GPU/VRAM si el usuario llega a escribir
        antes de que este precalentado termine, o si el CognitiveGovernor
        ya está corriendo) — como corre en un hilo de fondo dedicado, no
        bloquea el hilo de Qt ni la aparición de la ventana en ningún
        caso.

        Cualquier fallo (Ollama todavía no aceptando conexiones, timeout,
        lo que sea) se traga en silencio vía `logger.debug`: esto es una
        optimización de latencia estrictamente opcional, nunca un paso
        obligatorio de arranque — si falla, el primer turno real
        simplemente paga la carga completa, exactamente igual que antes
        de que este método existiera.
        """
        if requests is None or self._session is None:
            return
        try:
            with self._llm_lock:
                self._session.post(
                    self.ollama_endpoint,
                    json={
                        "model": self.general_model,
                        "prompt": "Hola",
                        "stream": False,
                        "options": {
                            "num_predict": 1,
                            "temperature": 0.1,
                            "num_ctx": self._memory_governor.PINNED_NUM_CTX_GENERAL,
                            "num_thread": self._memory_governor._num_thread,
                            "num_gpu": self._memory_governor.DEFAULT_NUM_GPU,
                        },
                        "keep_alive": self._memory_governor.KEEP_ALIVE,
                    },
                    # Timeout generoso: una carga fría en disco lento puede
                    # superar los timeouts cortos del resto de las
                    # llamadas utilitarias - acá no hay ningún turno de
                    # usuario esperando esta respuesta.
                    timeout=90,
                )
            _msg = "[WARM-UP] Modelo general precalentado en segundo plano — el primer turno ya no paga la carga completa."
            logger.debug(_msg)
            if log_cb is not None:
                with contextlib.suppress(Exception):
                    log_cb(_msg)
        except Exception as exc:
            logger.debug("Precalentado de modelo en segundo plano falló (no crítico, se degrada a carga normal): %s", exc)

    # =================================================================
    # RESÚMENES EXTRACTIVOS POR FUENTE (MAP-REDUCE) - mismo patrón que
    # rewrite_search_query_via_llm arriba: system prompt DEDICADO y
    # minimalista (no el SYSTEM_PROMPT completo, que arrastra el
    # protocolo <thought> de 6 pasos y el esquema de herramientas -
    # inútil y contraproducente para "resume este texto"), stream=False,
    # temperatura baja, num_predict chico, mismo _llm_lock que protege
    # la GPU/VRAM de colisiones con la generación principal.
    # =================================================================
    _SOURCE_SUMMARY_SYSTEM_PROMPT_ES: str = (
        "Eres un componente interno de resumen extractivo. Tu ÚNICA función es "
        "condensar el texto de UNA fuente web en un resumen breve y fiel, priorizando "
        "lo relevante para la consulta dada.\n\n"
        "Reglas estrictas:\n"
        "- Responde ÚNICAMENTE con el resumen, en 2-4 oraciones, texto plano.\n"
        "- Nunca agregues opiniones, conclusiones propias ni datos que no estén en el "
        "texto original — esto es EXTRACCIÓN, no generación de conocimiento nuevo.\n"
        "- Prioriza los hechos concretos (números, fechas, nombres, resultados) por "
        "sobre el lenguaje de relleno o el contexto de fondo.\n"
        "- Si el texto no tiene relación real con la consulta, dilo en una frase en vez "
        "de forzar un resumen (\"El texto no trata directamente sobre la consulta\").\n"
        "- Nunca uses markdown, comillas envolventes ni prefijos como \"Resumen:\"."
    )
    _SOURCE_SUMMARY_SYSTEM_PROMPT_EN: str = (
        "You are an internal extractive summarization component. Your only function is "
        "to condense the text of ONE web source into a short, faithful summary, "
        "prioritizing what is relevant to the given query.\n\n"
        "Strict rules:\n"
        "- Respond with only the summary, in 2-4 sentences, plain text.\n"
        "- Never add opinions, your own conclusions, or facts absent from the original "
        "text — this is EXTRACTION, not new knowledge generation.\n"
        "- Prioritize concrete facts (numbers, dates, names, results) over filler "
        "language or background context.\n"
        "- If the text has no real relation to the query, say so in one sentence "
        "instead of forcing a summary (\"The text is not directly about the query\").\n"
        "- Never use markdown, wrapping quotes, or prefixes like \"Summary:\"."
    )

    @property
    def SOURCE_SUMMARY_SYSTEM_PROMPT(self) -> str:
        is_en = getattr(self, "current_language", "Spanish") == "English"
        return self._SOURCE_SUMMARY_SYSTEM_PROMPT_EN if is_en else self._SOURCE_SUMMARY_SYSTEM_PROMPT_ES

    # Timeout corto y deliberadamente distinto de OLLAMA_TIMEOUT_SECONDS:
    # esta es una llamada utilitaria de salida corta (2-4 oraciones), no
    # una generación completa - si tarda más que esto, se degrada al
    # contenido crudo sin resumir en vez de bloquear el turno.
    SOURCE_SUMMARY_TIMEOUT_SECONDS: float = 20.0
    # Gate del paso completo (map-reduce): por debajo de este total
    # combinado, el contexto ya es corto - pagar N llamadas extra al
    # modelo no compensa el ahorro de contexto que lograrían. Mismo
    # umbral pedido explícitamente en el diagnóstico ("> 3000 caracteres").
    SOURCE_SUMMARY_LENGTH_THRESHOLD_CHARS: int = 3000
    # Por debajo de esto, una fuente individual ya es lo bastante corta
    # como para que resumirla no ahorre nada real - se deja intacta.
    SOURCE_SUMMARY_MIN_CHARS: int = 400

    # =================================================================
    # Nota (pedido explícito, "recortar segundos reales de
    # generación" - medido: ~6s invisibles en el log de un turno real,
    # entre "Formateando contexto" y el arranque de la Pasada 1, con
    # cero línea de log que explicara por qué): `summarize_sources_map_
    # reduce` pagaba UNA llamada HTTP completa a Ollama POR FUENTE que
    # necesitara resumen, en SECUENCIA - cada una con su propio prefill
    # del system prompt + texto de esa fuente. Paralelizar esas llamadas
    # con hilos de Python NO habría ayudado: `_llm_lock` se mantiene
    # tomado durante toda la llamada HTTP a propósito (evitar competir
    # por la misma GPU/VRAM - ver el mismo trade-off ya documentado y
    # medido en `_tree_of_thought_reasoning`), así que con la
    # configuración por defecto de Ollama (sin `OLLAMA_NUM_PARALLEL` > 1)
    # los hilos igual terminan sirializados esperando el mismo cerrojo.
    #
    # La reducción real de latencia viene de bajar el NÚMERO de
    # llamadas, no de paralelizarlas: `summarize_sources_batch_via_llm`
    # combina hasta `SOURCE_SUMMARY_BATCH_MAX_ITEMS` fuentes en un único
    # prompt y pide un array JSON con un resumen por fuente, en el mismo
    # orden - una llamada en vez de N. Con validación estricta de que la
    # cantidad devuelta coincide exactamente con la pedida: un modelo de
    # 3B puede fusionar, saltear o agregar un ítem de más en una salida
    # de array, y asignar resúmenes a la fuente equivocada por un
    # desalineamiento de índice sería un bug MUCHO peor que simplemente
    # no resumir (contenido correcto pero verboso vs. contenido
    # atribuido a la fuente incorrecta) - ver `summarize_sources_map_
    # reduce` más abajo, que cae al camino secuencial de siempre (uno
    # por uno, ya blindado y probado) para todo el lote si el conteo no
    # cierra o la llamada combinada falla por cualquier motivo.
    # =================================================================
    #: Tope de fuentes combinadas en una sola llamada de resumen. Un lote
    #: más grande alarga el prompt/`num_predict` sin límite y aumenta la
    #: chance de que el modelo pierda la cuenta del array - con esto, un
    #: turno con más fuentes que el tope simplemente paga un segundo lote
    #: (todavía muchas menos llamadas que una por fuente).
    SOURCE_SUMMARY_BATCH_MAX_ITEMS: int = 6
    #: Recorte POR FUENTE dentro de un lote - más chico que los 4000
    #: caracteres de `summarize_source_via_llm` (llamada individual) a
    #: propósito: con varias fuentes compartiendo el mismo `num_ctx`
    #: pinneado, cada una necesita un cupo más chico para que el lote
    #: completo (texto de las N fuentes + system prompt + scaffolding)
    #: siga entrando cómodo.
    SOURCE_SUMMARY_BATCH_PER_ITEM_CHARS: int = 2000

    _SOURCE_SUMMARY_BATCH_SYSTEM_PROMPT_ES: str = (
        "Eres un componente interno de resumen extractivo. Vas a recibir VARIAS "
        "fuentes web numeradas, cada una con su propio texto, y una consulta de "
        "referencia. Tu única función es condensar CADA fuente en un resumen breve "
        "y fiel, priorizando lo relevante para la consulta.\n\n"
        "Reglas estrictas:\n"
        "- Responde ÚNICAMENTE con un objeto JSON de la forma "
        "{\"summaries\": [\"...\", \"...\"]} — un string de resumen por fuente, "
        "EN EL MISMO ORDEN en que se te dieron las fuentes.\n"
        "- El array \"summaries\" debe tener EXACTAMENTE la misma cantidad de "
        "elementos que fuentes recibiste — ni uno menos, ni uno más. Nunca fusiones "
        "dos fuentes en un solo resumen ni omitas ninguna.\n"
        "- Cada resumen individual: 2-4 oraciones, texto plano, sin markdown.\n"
        "- Nunca agregues opiniones, conclusiones propias ni datos que no estén en "
        "el texto original de ESA fuente — esto es EXTRACCIÓN, no generación de "
        "conocimiento nuevo.\n"
        "- Prioriza hechos concretos (números, fechas, nombres, resultados) por "
        "sobre lenguaje de relleno.\n"
        "- Si una fuente no tiene relación real con la consulta, su resumen debe "
        "decir eso en una frase (\"El texto no trata directamente sobre la "
        "consulta\") en vez de forzar contenido."
    )
    _SOURCE_SUMMARY_BATCH_SYSTEM_PROMPT_EN: str = (
        "You are an internal extractive summarization component. You will receive "
        "SEVERAL numbered web sources, each with its own text, and one reference "
        "query. Your only function is to condense EACH source into a short, "
        "faithful summary, prioritizing what is relevant to the query.\n\n"
        "Strict rules:\n"
        "- Respond with ONLY a JSON object of the form "
        "{\"summaries\": [\"...\", \"...\"]} — one summary string per source, IN "
        "THE SAME ORDER the sources were given to you.\n"
        "- The \"summaries\" array must have EXACTLY as many elements as sources "
        "you received — not one fewer, not one more. Never merge two sources into "
        "one summary or skip any of them.\n"
        "- Each individual summary: 2-4 sentences, plain text, no markdown.\n"
        "- Never add opinions, your own conclusions, or facts absent from that "
        "SPECIFIC source's original text — this is EXTRACTION, not new knowledge "
        "generation.\n"
        "- Prioritize concrete facts (numbers, dates, names, results) over filler "
        "language.\n"
        "- If a source has no real relation to the query, its summary should say "
        "so in one sentence (\"The text is not directly about the query\") instead "
        "of forcing content."
    )

    @property
    def SOURCE_SUMMARY_BATCH_SYSTEM_PROMPT(self) -> str:
        is_en = getattr(self, "current_language", "Spanish") == "English"
        return (
            self._SOURCE_SUMMARY_BATCH_SYSTEM_PROMPT_EN if is_en
            else self._SOURCE_SUMMARY_BATCH_SYSTEM_PROMPT_ES
        )

    def summarize_sources_batch_via_llm(
        self, texts: List[str], query: str, target_model: Optional[str] = None,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> Optional[List[str]]:
        """
        Resume VARIAS fuentes en UNA sola llamada al modelo — el "map"
        agrupado que `summarize_sources_map_reduce` usa en vez de llamar
        a `summarize_source_via_llm` una vez por fuente (ver el BLINDAJE
        arriba de `SOURCE_SUMMARY_BATCH_MAX_ITEMS` para el motivo
        completo: menos llamadas, no llamadas paralelas, es lo que
        reduce la latencia real bajo `_llm_lock`).

        Devuelve una lista de resúmenes con EXACTAMENTE `len(texts)`
        elementos, en el mismo orden que `texts`, o `None` si CUALQUIER
        cosa sale mal — red, timeout, JSON inválido, o (crítico) el
        modelo devolvió una cantidad de resúmenes distinta a la
        cantidad de fuentes pedidas. `None` es una señal explícita para
        el llamador de "no confíes en nada de esta respuesta, usa el
        camino de respaldo" — nunca se devuelve una lista parcial o
        desalineada, porque asignar el resumen de una fuente a otra
        distinta sería silencioso y mucho peor que no resumir.
        """
        if requests is None or self._session is None or not texts:
            return None

        is_en = getattr(self, "current_language", "Spanish") == "English"
        label = "Source" if is_en else "Fuente"
        parts = [
            f"{label} {i}:\n{text[: self.SOURCE_SUMMARY_BATCH_PER_ITEM_CHARS]}"
            for i, text in enumerate(texts, start=1)
        ]
        sources_block = "\n\n".join(parts)
        query_label = "Query" if is_en else "Consulta"
        count_reminder = (
            f"(Return exactly {len(texts)} summaries, same order as above.)" if is_en
            else f"(Devolvé exactamente {len(texts)} resúmenes, mismo orden que arriba.)"
        )
        prompt = f"{query_label}: {query.strip()}\n\n{sources_block}\n\n{count_reminder}\nJSON:"

        # `num_predict` escala con la cantidad de fuentes del lote (mismo
        # presupuesto por fuente que la llamada individual, 180, más un
        # margen para la envoltura JSON) - nunca menos que eso alcanzaría
        # para que las N fuentes de la cola de este lote completen su
        # resumen.
        num_predict = min(180 * len(texts) + 80, 1200)

        payload = {
            "model": target_model or self.general_model,
            "system": self.SOURCE_SUMMARY_BATCH_SYSTEM_PROMPT,
            "prompt": prompt,
            "stream": False,
            "format": {
                "type": "object",
                "properties": {
                    "summaries": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summaries"],
            },
            "options": {
                "num_predict": num_predict,
                "temperature": 0.1,
                "num_ctx": self._memory_governor.PINNED_NUM_CTX_GENERAL,
                "num_thread": self._memory_governor._num_thread,
                "num_gpu": self._memory_governor.DEFAULT_NUM_GPU,
            },
            "keep_alive": self._memory_governor.KEEP_ALIVE,
        }

        _start = time.time()
        try:
            with self._llm_lock:
                response = self._session.post(
                    self.ollama_endpoint,
                    json=payload,
                    # Timeout del lote escalado igual que num_predict -
                    # el timeout fijo de la llamada individual (20s) le
                    # quedaría corto a un lote de varias fuentes.
                    timeout=max(self.SOURCE_SUMMARY_TIMEOUT_SECONDS, 8.0 * len(texts)),
                )
            response.raise_for_status()
            data = response.json()
            raw = str(data.get("response", "")).strip()
        except Exception as exc:
            logger.debug("Resumen extractivo por lote vía LLM falló: %s", exc)
            return None
        finally:
            _elapsed = time.time() - _start
            _msg = f"⏱️ [SourceSummary-Batch] {len(texts)} fuente(s) en {_elapsed:.2f}s (1 llamada en vez de {len(texts)})"
            logger.debug(_msg)
            if log_cb is not None:
                with contextlib.suppress(Exception):
                    log_cb(_msg)

        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # Ollama viejo sin soporte de `format`, o el modelo no
            # devolvió JSON pese al esquema - se degrada al camino de
            # respaldo secuencial en vez de intentar rescatar texto
            # libre (a diferencia de `_sanitize_llm_query_output`, acá
            # no hay forma segura de partir texto libre en N resúmenes
            # alineados a sus fuentes sin arriesgar un desalineamiento).
            return None

        summaries = parsed.get("summaries") if isinstance(parsed, dict) else None
        if not isinstance(summaries, list) or len(summaries) != len(texts):
            logger.debug(
                "Resumen por lote: cantidad devuelta (%s) no coincide con la pedida (%s), se descarta el lote.",
                len(summaries) if isinstance(summaries, list) else type(summaries).__name__,
                len(texts),
            )
            return None

        cleaned = [str(s or "").strip() for s in summaries]
        if any(len(s) < 10 for s in cleaned):
            # Mismo umbral que la llamada individual ("menos de 10
            # caracteres no es un resumen real") - si cualquier elemento
            # del lote es basura/vacío, se descarta el lote entero por
            # la misma razón que un conteo incorrecto: preferible que
            # esa fuente puntual pase por el camino de respaldo
            # individual (que si sabe distinguir cuál de las N falló)
            # a inventar qué posición era la vacía acá.
            return None

        return cleaned

    def summarize_source_via_llm(
        self, source_text: str, query: str, target_model: Optional[str] = None
    ) -> Optional[str]:
        """
        Resume UNA fuente vía una mini-llamada no-streaming al modelo
        local — el paso "map" del map-reduce (ver
        `summarize_sources_map_reduce` más abajo, que decide CUÁNDO
        vale la pena pagar esta llamada y qué hacer con el resultado).

        Nunca lanza ni bloquea el turno: cualquier fallo de red, timeout,
        o una respuesta vacía/demasiado corta para ser un resumen real
        degrada a `None` — el llamador conserva el texto original sin
        resumir en ese caso, exactamente igual que
        `rewrite_search_query_via_llm` degrada a la reescritura
        determinista.
        """
        if requests is None or self._session is None:
            return None

        text = (source_text or "").strip()
        if not text:
            return None

        # Recorte defensivo antes de mandarlo al modelo: num_ctx de esta
        # mini-llamada es chico a propósito (ver options abajo) - no
        # tiene sentido mandarle más texto del que ese contexto puede
        # absorber junto con el system prompt y la propia consulta.
        prompt = f"Consulta: {query.strip()}\nTexto de la fuente:\n{text[:4000]}\nResumen:"

        payload = {
            "model": target_model or self.general_model,
            "system": self.SOURCE_SUMMARY_SYSTEM_PROMPT,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": 180,
                "temperature": 0.1,
                # Nota: mismo motivo que rewrite_search_query_via_llm
                # (ver su comentario, arriba) - 2048 forzaba una
                # reasignación completa del runner de Ollama cada vez que
                # esta mini-llamada de resumen se intercalaba con las
                # llamadas de turno principal (pinneadas a 8192), pagando
                # ese costo potencialmente varias veces por turno (una
                # por fuente en el map-reduce). Igualado al mismo pin.
                "num_ctx": self._memory_governor.PINNED_NUM_CTX_GENERAL,
                "num_thread": self._memory_governor._num_thread,
                "num_gpu": self._memory_governor.DEFAULT_NUM_GPU,
            },
            # Nota: mismo motivo que rewrite_search_query_via_llm (ver
            # su comentario) - sin esto, cada llamada de resumen (una por
            # fuente en el map-reduce, potencialmente varias por turno)
            # dejaba el modelo con el keep_alive por defecto de Ollama en
            # vez del pinneado de esta app. `self._memory_governor.
            # KEEP_ALIVE`, no `self.KEEP_ALIVE` - esa constante vive en
            # MemoryGovernor, no en Orchestrator.
            "keep_alive": self._memory_governor.KEEP_ALIVE,
        }

        try:
            # Mismo lock que _call_llm/rewrite_search_query_via_llm: esta
            # llamada comparte la GPU/VRAM con la generación principal y
            # no debe pisarla.
            with self._llm_lock:
                response = self._session.post(
                    self.ollama_endpoint,
                    json=payload,
                    timeout=self.SOURCE_SUMMARY_TIMEOUT_SECONDS,
                )
            response.raise_for_status()
            data = response.json()
            raw = str(data.get("response", "")).strip()
        except Exception as exc:
            logger.debug("Resumen extractivo de fuente vía LLM falló: %s", exc)
            return None

        # Menos de 10 caracteres no es un resumen real (respuesta vacía,
        # o el modelo devolvió solo puntuación/ruido) - se trata igual
        # que un fallo de red: degradar, no propagar basura al contexto.
        if not raw or len(raw) < 10:
            return None
        return raw

    def summarize_sources_map_reduce(
        self,
        results: List[Dict[str, Any]],
        query: str,
        target_model: Optional[str] = None,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Paso "reduce" implícito: no combina las fuentes en un solo texto
        — las deja separadas, una por fuente — pero al reemplazar cada
        `content` largo por su resumen ANTES de que el contexto llegue a
        la generación principal, el modelo principal sintetiza sobre N
        resúmenes cortos y enfocados en vez de sobre el contenido crudo
        completo de cada fuente (el fallo real que motiva esto: modelos
        locales chicos —qwen2.5:3b— se pierden en ruido/boilerplate y no
        sintetizan bien contenido largo o de múltiples fuentes).

        Gateado por longitud TOTAL combinada (`SOURCE_SUMMARY_LENGTH_
        THRESHOLD_CHARS`): con poco contexto, resumir no compensa la
        latencia de llamadas extra al modelo — se hace un chequeo barato
        de longitud ANTES de intentar nada.

        Punto 5 (caché de resúmenes): antes de pagar una llamada nueva
        para una fuente, se consulta `MemoryGraph.fetch_web_knowledge_
        by_url()` — si esa URL ya fue resumida en una investigación
        previa (misma sesión o una anterior, persiste en disco), se
        reutiliza ese resumen sin tocar el modelo ni la red. Un resumen
        nuevo se persiste de inmediato vía `store_web_knowledge()`
        (extendido con la columna `summary`, ver memory_graph.py) para
        que la PRÓXIMA vez que aparezca esta URL —en este turno o en
        uno futuro— no haga falta resumirla de nuevo.

        BLINDAJE (pedido explícito, "recortar segundos reales" — ver el
        BLINDAJE completo arriba de `SOURCE_SUMMARY_BATCH_MAX_ITEMS`):
        las fuentes que SÍ necesitan una llamada nueva (sin caché, no
        demasiado cortas) ya no se resumen una por una en secuencia —
        se agrupan en lotes de hasta `SOURCE_SUMMARY_BATCH_MAX_ITEMS` y
        cada lote se resuelve con UNA sola llamada
        (`summarize_sources_batch_via_llm`). Si un lote falla por
        cualquier motivo (red, timeout, JSON inválido, o el modelo
        devolvió una cantidad de resúmenes que no coincide con la
        cantidad pedida), ESE lote específico cae al camino secuencial
        de siempre —`summarize_source_via_llm` una vez por fuente— en
        vez de perder el resumen de todo el lote: el camino nuevo nunca
        es más lento ni menos resiliente que el anterior, solo más
        rápido cuando el lote sale bien (el caso común).

        No muta `results` in-place: devuelve una lista NUEVA de dicts
        con `content` reemplazado por el resumen en los ítems que se
        resumieron (o reutilizaron de caché); el resto de campos
        (incluido `snippet`, si el ítem lo trae) se preserva sin tocar,
        así cualquier consumidor que siga leyendo `snippet` (p. ej. las
        tarjetas visuales de la UI) no se ve afectado por este resumen —
        solo el texto que efectivamente llega al modelo cambia. El
        ORDEN/ÍNDICE de `results` se preserva exactamente en el
        resultado devuelto — invariante del que depende el llamador en
        sovnode_qt.py (ver el comentario "Punto 4" ahí, que empareja
        `sources_for_summary[i]` con el resultado de esta función por
        índice) — el agrupamiento en lotes es un detalle interno, nunca
        reordena la lista final.

        Cualquier fallo (de resumen o de caché) para UNA fuente nunca
        bloquea a las demás: esa fuente en particular simplemente
        conserva su contenido original sin resumir.
        """
        if not results:
            return results

        def _text_of(item: Dict[str, Any]) -> str:
            return str(item.get("content") or item.get("snippet") or "").strip()

        total_chars = sum(len(_text_of(r)) for r in results if isinstance(r, dict))
        if total_chars <= self.SOURCE_SUMMARY_LENGTH_THRESHOLD_CHARS:
            return results

        # Primera pasada - SIN llamadas al modelo: resuelve lo barato
        # (passthrough, demasiado corto, caché) y junta el resto en
        # `pending` para el resumen por lotes. `summarized_results[idx]`
        # arranca en `None` para cada pendiente y se completa más abajo
        # - así el orden final queda garantizado sin importar en qué
        # lote/orden se resuelva cada pendiente.
        summarized_results: List[Optional[Dict[str, Any]]] = [None] * len(results)
        pending: List[Tuple[int, str, str]] = []  # (índice, texto, url)

        for idx, item in enumerate(results):
            if not isinstance(item, dict):
                summarized_results[idx] = item
                continue

            text = _text_of(item)
            new_item = dict(item)
            if len(text) <= self.SOURCE_SUMMARY_MIN_CHARS:
                summarized_results[idx] = new_item
                continue

            url = str(item.get("url") or "")
            cached_entry = None
            if url:
                with contextlib.suppress(Exception):
                    cached_entry = self.memory_graph.fetch_web_knowledge_by_url(url)
            cached_summary = str((cached_entry or {}).get("summary") or "").strip()

            if cached_summary:
                new_item["content"] = cached_summary
                new_item["content_source"] = "cached_summary"
                summarized_results[idx] = new_item
                continue

            summarized_results[idx] = new_item  # placeholder, se completa abajo
            pending.append((idx, text, url))

        def _apply_summary(idx: int, text: str, url: str, summary: Optional[str]) -> None:
            if not summary:
                return
            summarized_results[idx]["content"] = summary
            summarized_results[idx]["content_source"] = "llm_summary"
            if url:
                with contextlib.suppress(Exception):
                    self.memory_graph.store_web_knowledge(query, [{
                        "url": url,
                        "domain": results[idx].get("domain", ""),
                        "title": results[idx].get("title", ""),
                        "content": text,
                        "summary": summary,
                        "score": results[idx].get("score", 0.0),
                    }])

        for chunk_start in range(0, len(pending), self.SOURCE_SUMMARY_BATCH_MAX_ITEMS):
            chunk = pending[chunk_start: chunk_start + self.SOURCE_SUMMARY_BATCH_MAX_ITEMS]
            if len(chunk) == 1:
                # Un solo pendiente: el camino de lote no ahorra nada
                # (sigue siendo una llamada), así que se salta
                # directo al camino individual de siempre.
                idx, text, url = chunk[0]
                summary = self.summarize_source_via_llm(text, query, target_model=target_model)
                _apply_summary(idx, text, url, summary)
                continue

            batch_summaries = self.summarize_sources_batch_via_llm(
                [text for _, text, _ in chunk], query,
                target_model=target_model, log_cb=log_cb,
            )
            if batch_summaries is not None:
                for (idx, text, url), summary in zip(chunk, batch_summaries):
                    _apply_summary(idx, text, url, summary)
                continue

            # El lote falló o vino desalineado: respaldo secuencial de
            # siempre, fuente por fuente, SOLO para este lote - el resto
            # de lotes ya resueltos (o los siguientes) no se ven
            # afectados por este fallo puntual.
            for idx, text, url in chunk:
                summary = self.summarize_source_via_llm(text, query, target_model=target_model)
                _apply_summary(idx, text, url, summary)

        return summarized_results

    def run_system_tool(self, tool_name: str, **kwargs) -> str:
        return self.tools.execute(tool_name, **kwargs)


SovNodeOrchestrator = Orchestrator
MonolithOrchestrator = Orchestrator


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    orchestrator = Orchestrator()
    print("=" * 70)
    print("SovNode — Orchestrator con blindaje anti-evasión y observabilidad avanzada")
    print("=" * 70)
    print(f"Modelo (único): {orchestrator.model}")
    print(f"Router (0.5B):  {orchestrator.router_model}")
    print(f"think:          {orchestrator.think_level or 'off'}")
    print(f"Endpoint:       {orchestrator.ollama_endpoint}")
    print(f"Reintentos AST: {orchestrator.MAX_SYNTAX_REPAIR_ATTEMPTS}")
    print("=" * 70)