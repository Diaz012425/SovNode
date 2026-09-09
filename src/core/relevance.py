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
SovNode — relevance.py

ES: ÚNICA fuente de verdad de los criterios de relevancia consulta-vs-evidencia
compartidos por web_search.py, sovnode_qt.py, verification.py y orchestrator.py.
Este módulo no importa nada del proyecto a propósito, para no crear un ciclo
con las demás capas que lo importan.
EN: The SINGLE source of truth for query-vs-evidence relevance criteria,
shared by web_search.py, sovnode_qt.py, verification.py, and orchestrator.py.
This module deliberately imports nothing from the project, to avoid a cycle
with the other layers that import it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Pattern, Set, Tuple

# =====================================================================
# EJE 1 - ¿La consulta pide datos FRESCOS? / AXIS 1 - Does the query ask for FRESH data?
# =====================================================================
_LIVE_EVENT_QUERY_RE: Pattern[str] = re.compile(
    r"\b(hoy|ahora|en\s+vivo|[uú]ltim[oa]s?|resultado|marcador|"
    r"fichaje|fichajes|traspaso|traspasos|rumor|rumores|"
    r"today|now|live|latest|last|breaking|score|current|"
    r"transfer|transfers|rumou?rs?|signing|signed)\b",
    re.IGNORECASE,
)

# =====================================================================
# EJE 2 - ¿La consulta pide un HECHO PUNTUAL Y VERIFICABLE? / AXIS 2 - Does it ask for a PRECISE, VERIFIABLE fact?
# =====================================================================
_PRECISE_FACT_QUERY_RE: Pattern[str] = re.compile(
    r"\b(final|finales|gan[oó]|ganaron|ganador(?:es)?|campe[oó]n(?:es)?|"
    r"resultados?|marcador(?:es)?|puntajes?|goles|"
    r"score|scores|winner|winners|won|champion|champions|results?)\b",
    re.IGNORECASE,
)


def is_live_event_query(query: str) -> bool:
    """ES: True si la consulta pide datos en vivo/recientes. / EN: True if the query asks for live/recent data."""
    return bool(_LIVE_EVENT_QUERY_RE.search(query or ""))


def requires_precise_fact(query: str) -> bool:
    """ES: True si la consulta pide un dato puntual y verificable. / EN: True if the query asks for a precise, verifiable fact."""
    return bool(_PRECISE_FACT_QUERY_RE.search(query or ""))


def needs_strict_relevance(query: str) -> bool:
    """ES: Gate único de las defensas de relevancia. / EN: Single gate for the relevance defenses."""
    return is_live_event_query(query) or requires_precise_fact(query)


# =====================================================================
# SOLAPAMIENTO LÉXICO Y FILTRADO DE ENTIDADES / LEXICAL OVERLAP AND ENTITY FILTERING
# =====================================================================
_RELEVANCE_WORD_RE: Pattern[str] = re.compile(r"[a-záéíóúñü0-9]+", re.IGNORECASE)

SIGNIFICANT_WORD_MIN_LEN: int = 3


def significant_words(text: str) -> Set[str]:
    return {
        w for w in _RELEVANCE_WORD_RE.findall((text or "").lower())
        if len(w) >= SIGNIFICANT_WORD_MIN_LEN
    }


_GENERIC_CONTEXT_WORDS: frozenset = frozenset({
    "final", "finales", "partido", "partidos", "match", "game", "juego",
    "resultado", "resultados", "marcador", "score", "scores", "result",
    "results", "goles", "goals", "gol", "goal", "mundial", "world", "cup",
    "copa", "torneo", "tournament", "campeonato", "championship", "liga",
    "league", "penales", "penaltis", "penalties", "shootout", "tanda",
    "minuto", "minute", "tiempo", "half", "extra", "prorroga", "prórroga",
    "venci", "venció", "vencio", "gano", "ganó", "ganar", "beat", "won",
    "win", "defeat", "against", "contra", "frente", "ante", "sobre",
    "equipo", "equipos", "team", "teams", "seleccion", "selección",
    "jugador", "jugadores", "player", "players", "estadio", "stadium",
    "grupo", "group", "fase", "stage", "round", "ronda", "semifinal",
    "semifinales", "cuartos", "octavos", "wikipedia", "https", "http",
    "tercer", "tercero", "puesto", "lugar", "third", "place", "bronce",
    "bronze", "consolacion", "consolación", "medal", "medalla",
    "tras", "empatar", "empato", "empató", "empate", "definio", "definió",
    "termino", "terminó", "disputo", "disputó", "played", "after",
    "defeated", "defeats", "defeating", "beats", "beating", "winning",
    "wins", "según", "segun", "according", "fuentes", "sources", "title",
    "titulo", "título", "their", "for", "fifa", "uefa", "conmebol",
})


# ES: _GENERIC_CONTEXT_WORDS cubre jerga futbolística, no palabras
# funcionales comunes. Sin _STOPWORDS, dos marcadores SIN ningún
# participante en común podrían pasar por "el mismo evento" solo por
# compartir una palabra como "the" — un frozenset({"the"}) no identifica
# nada, pero superaba el chequeo de solapamiento igual que un nombre de
# equipo real.
# EN: _GENERIC_CONTEXT_WORDS covers football jargon, not common function
# words. Without _STOPWORDS, two scoreboards with no participant in common
# could pass as "the same event" just by sharing a word like "the" — a
# frozenset({"the"}) identifies nothing, but used to pass the overlap check
# just like a real team name would.
_STOPWORDS: frozenset = frozenset({
    "the", "and", "for", "are", "was", "were", "with", "that", "this",
    "from", "have", "has", "had", "not", "but", "you", "your", "they",
    "them", "his", "her", "its", "our", "who", "which", "what", "when",
    "where", "how", "than", "then", "also", "into", "onto", "out", "over",
    "under", "more", "most", "some", "such", "only", "just", "very", "can",
    "could", "would", "should", "will", "shall", "may", "might", "did",
    "does", "been", "being", "each", "any", "all", "one", "two", "there",
    "here", "these", "those", "los", "las", "una", "unos", "unas", "que",
    "con", "para", "por", "del", "esta", "este", "estos", "esas", "esos",
    "como", "pero", "porque", "cuando", "donde", "sobre", "entre", "hasta",
    "desde", "hay", "fue", "era", "son", "sus", "muy", "mas", "más", "les",
    "nos", "eso", "esa",
})


def distinctive_words(text: str) -> Set[str]:
    """ES: Palabras del contexto que identifican a los participantes o evento específico.
    EN: Words in the context that identify the specific participants or event."""
    return {
        w for w in significant_words(text)
        if w not in _GENERIC_CONTEXT_WORDS
        and w not in _STOPWORDS
        and not w.isdigit()
    }


def contexts_describe_same_event(query: str, context_a: str, context_b: str) -> bool:
    """ES: True si dos contextos de marcador hablan del mismo partido/evento.
    EN: True if two scoreboard contexts talk about the same match/event."""
    words_a = distinctive_words(context_a)
    words_b = distinctive_words(context_b)
    if not words_a or not words_b:
        return True
    return bool(words_a & words_b)


def entities_supported_by_context(entities: Set[str], context: str) -> bool:
    """ES: True si `context` menciona al menos una palabra distintiva en `entities`.
    EN: True if `context` mentions at least one distinctive word from `entities`."""
    if not entities:
        return True
    return bool(entities & distinctive_words(context))


def extract_query_matchup_entities(
    query: str,
    fallback_evidence: str = "",
) -> Set[str]:
    """
    ES: Extrae entidades nombradas en la consulta. Si la consulta es
    genérica y no nombra directamente a los participantes, intenta
    inferirlos desde los enfrentamientos en la evidencia autoritativa.
    EN: Extracts named entities from the query. If the query is generic and
    doesn't directly name the participants, tries to infer them from
    matchups in the authoritative evidence.
    """
    direct_entities = distinctive_words(query)
    if len(direct_entities) >= 2:
        return direct_entities

    if fallback_evidence:
        inferred = _matchup_entities(fallback_evidence)
        if len(inferred) >= 2:
            return inferred

    return direct_entities


def score_context_matches_query_entities(
    query_entities: Set[str],
    context: str,
    min_required: int = 2,
) -> bool:
    """ES: True si el contexto de un marcador candidato contiene las entidades solicitadas.
    EN: True if a candidate scoreboard's context contains the requested entities."""
    if not query_entities:
        return True

    context_entities = distinctive_words(context)
    overlap = query_entities & context_entities
    required = min(min_required, len(query_entities))
    return len(overlap) >= required


def keyword_overlap(query: str, text: str) -> int:
    return len(significant_words(query) & significant_words(text))


def text_is_relevant(query: str, text: str) -> bool:
    """ES: True si `text` comparte al menos un término significativo con `query`.
    EN: True if `text` shares at least one significant term with `query`."""
    return keyword_overlap(query, text) > 0


_CAPITALIZED_TOKEN_RE: Pattern[str] = re.compile(r"[A-ZÀ-Ý][a-zà-ÿ]*")


def _entity_phrase_word_groups(text: str) -> List[Set[str]]:
    """
    ES: Runs de 1+ palabras con mayúscula inicial consecutivas ("Real
    Madrid", pero también "Barcelona" sola) como UN solo grupo de
    significado — ver is_topically_relevant(). Un nombre propio de una sola
    palabra también debe formar grupo: exigir 2+ palabras dejaba consultas
    de una entidad ("Barcelona recent signing") sin ninguna entidad
    detectada, colando fuentes sin relación real (p. ej. Barcelona S.C.,
    un club distinto que solo comparte nombre de ciudad).
    EN: Runs of 1+ consecutive capitalized words ("Real Madrid", but also
    "Barcelona" alone) as ONE meaning group — see is_topically_relevant().
    A single-word proper noun must also form a group: requiring 2+ words
    left single-entity queries ("Barcelona recent signing") with no
    detected entity, letting through unrelated sources (e.g. Barcelona
    S.C., an unrelated club sharing only the city name).
    """
    groups: List[Set[str]] = []
    current: Set[str] = set()
    for token in (text or "").split():
        core = "".join(ch for ch in token if ch.isalpha())
        if core and _CAPITALIZED_TOKEN_RE.fullmatch(core) and len(core) >= 3:
            current.add(core.lower())
        else:
            if current:
                groups.append(current)
            current = set()
    if current:
        groups.append(current)
    return groups


def is_topically_relevant(query: str, text: str) -> bool:
    """ES: Filtro de relevancia MÁS ESTRICTO que text_is_relevant().
    EN: A STRICTER relevance filter than text_is_relevant()."""
    query_words = significant_words(query)
    shared = query_words & significant_words(text)
    if not shared:
        return False

    # ES: Compartir solo el nombre de una entidad compuesta de la consulta
    # ("real"+"madrid") no prueba que el texto trate lo que se pregunta
    # SOBRE esa entidad — cualquier artículo sobre el club la menciona sin
    # necesariamente hablar, p. ej., de un fichaje reciente. Solo rechaza
    # cuando la consulta pide algo sustantivo MÁS ALLÁ de la entidad,
    # descartando palabras genéricas de dominio (_GENERIC_CONTEXT_WORDS)
    # al decidir si "hay algo más que exigir".
    # EN: Sharing only a compound query entity's name ("real"+"madrid")
    # doesn't prove the text is about what's asked ABOUT that entity — any
    # article on the club mentions it without necessarily covering, say, a
    # recent signing. Only rejects when the query asks for something
    # substantive BEYOND the entity, discounting generic domain words
    # (_GENERIC_CONTEXT_WORDS) when deciding "is there more to require".
    entity_groups = _entity_phrase_word_groups(query)
    entity_only_overlap = any(shared and shared <= group for group in entity_groups)
    if entity_only_overlap and (query_words - shared - _GENERIC_CONTEXT_WORDS):
        return False

    if len(shared) >= 2:
        return True

    (single_word,) = shared
    if not single_word.isdigit() and single_word not in _GENERIC_CONTEXT_WORDS:
        return True

    if single_word.isdigit():
        if any(not qw.isdigit() for qw in query_words):
            return False
    else:
        if any(qw not in _GENERIC_CONTEXT_WORDS for qw in query_words):
            return False
    return True


# =====================================================================
# AÑOS EXPLÍCITOS / EXPLICIT YEARS
# =====================================================================
YEAR_RE: Pattern[str] = re.compile(r"\b(?:19|20)\d{2}\b")


def extract_years(text: str) -> Set[str]:
    return set(YEAR_RE.findall(text or ""))


def has_conflicting_year(query: str, text: str) -> bool:
    """ES: True si `text` trae un año explícito que no coincide con la consulta.
    EN: True if `text` carries an explicit year that doesn't match the query."""
    query_years = extract_years(query)
    if not query_years:
        return False
    text_years = extract_years(text)
    if not text_years:
        return False
    return not (query_years & text_years)


# =====================================================================
# TÍTULOS RETROSPECTIVOS / RETROSPECTIVE TITLES
# =====================================================================
RETROSPECTIVE_TITLE_RE: Pattern[str] = re.compile(
    r"(historia\s+(?:del|de\s+la|de)\s+\w|history\s+of\s+\w)|"
    r"\b(?:19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2}\b",
    re.IGNORECASE,
)


def is_retrospective_title(title: str) -> bool:
    return bool(RETROSPECTIVE_TITLE_RE.search(title or ""))


# =====================================================================
# MARCADORES / RESULTADOS PUNTUALES / SCOREBOARDS - PRECISE RESULTS
# =====================================================================
SCORE_PATTERN_RE: Pattern[str] = re.compile(
    r"(?<!\d{4}[-–—:])\b\d{1,3}\s*[-–—:]\s*\d{1,3}\b(?![-–—:]\s*\d{4})"
)

_SCORE_SEPARATOR_RE: Pattern[str] = re.compile(r"[-–—:]")


def _normalize_score_value(raw: str) -> str:
    """ES: Espacios fuera y separador canonicalizado a '-'. / EN: Strips spaces and canonicalizes the separator to '-'."""
    return _SCORE_SEPARATOR_RE.sub("-", re.sub(r"\s+", "", raw or ""))


def extract_score_patterns(text: str) -> Set[str]:
    """ES: Bolsa plana de números normalizados, sin contexto. / EN: Flat bag of normalized numbers, no context."""
    return {_normalize_score_value(m) for m in SCORE_PATTERN_RE.findall(text or "")}


SCORE_CONTEXT_WINDOW: int = 120


def extract_score_events(text: str, window: int = SCORE_CONTEXT_WINDOW) -> List[Dict[str, Any]]:
    """ES: Cada marcador junto con el texto que lo rodea, recortado al límite de oración/párrafo.
    EN: Each scoreboard together with its surrounding text, trimmed to the sentence/paragraph boundary."""
    events: List[Dict[str, Any]] = []
    text = text or ""
    for m in SCORE_PATTERN_RE.finditer(text):
        start = max(0, m.start() - window)
        end = min(len(text), m.end() + window)

        boundary_re = re.compile(r"[.!?¡¿]|\n\n")

        left = text[start:m.start()]
        left_matches = list(boundary_re.finditer(left))
        if left_matches:
            start += left_matches[-1].end()

        right = text[m.end():end]
        right_match = boundary_re.search(right)
        if right_match:
            end = m.end() + right_match.start()

        raw_context = text[start:end]
        stripped_context = raw_context.strip()
        leading_trim = len(raw_context) - len(raw_context.lstrip())
        score_start = max(0, min(len(stripped_context), (m.start() - start) - leading_trim))
        score_end = max(score_start, min(len(stripped_context), (m.end() - start) - leading_trim))

        events.append({
            "score": _normalize_score_value(m.group()),
            "context": stripped_context,
            "score_start": score_start,
            "score_end": score_end,
            "abs_start": m.start(),
            "abs_end": m.end(),
        })
    return events


# =====================================================================
# DESAMBIGUACIÓN final vs. OTRAS RONDAS / final vs. OTHER ROUNDS DISAMBIGUATION
# =====================================================================
_FINAL_MATCH_QUERY_RE: Pattern[str] = re.compile(
    r"\bfinal(?:es|s)?\b", re.IGNORECASE,
)
_NON_FINAL_ROUND_RE: Pattern[str] = re.compile(
    r"tercer\s+(?:puesto|lugar)|third[\s-]place|3(?:rd|er)[\s-]place|"
    r"consolaci[oó]n|bronze\s+medal\s+match|"
    r"semi[\s-]?final(?:es|s)?|"
    r"cuartos(?:\s+de\s+final)?|quarter[\s-]?final(?:s)?|"
    r"octavos(?:\s+de\s+final)?|round\s+of\s+(?:16|32)|"
    r"fase\s+de\s+grupos|group\s+stage|grupo\s+[a-h]\b|group\s+[a-h]\b|"
    r"clasificatori[ao]s?|qualif(?:y|ying|ication)",
    re.IGNORECASE,
)


def asks_about_final(query: str) -> bool:
    """ES: True si la consulta pregunta por la final en concreto. / EN: True if the query asks about the final specifically."""
    return bool(_FINAL_MATCH_QUERY_RE.search(query or ""))


def mentions_non_final_round(text: str) -> bool:
    """ES: True si el texto nombra explícitamente una ronda que no es la final.
    EN: True if the text explicitly names a round other than the final."""
    return bool(_NON_FINAL_ROUND_RE.search(text or ""))


_BARE_FINAL_TOKEN_RE: Pattern[str] = re.compile(r"\bfinal(?:es|s)?\b", re.IGNORECASE)

_FINAL_QUALIFICATION_PHRASE_RE: Pattern[str] = re.compile(
    r"\b(?:reach(?:ed|ing)?|advanc(?:e|ed|ing)|progress(?:ed|ing)?|"
    r"qualif(?:y|ied|ying))\s+(?:to\s+|for\s+|into\s+)?the\s+"
    r"final(?:es|s)?\b",
    re.IGNORECASE,
)


def _round_indicator_spans(text: str) -> List[Tuple[int, int, bool]]:
    """ES: Todas las menciones de ronda en `text`: cada una como (inicio, fin, es_final).
    EN: Every round mention in `text`: each as (start, end, is_final)."""
    text = text or ""
    non_final_spans = [m.span() for m in _NON_FINAL_ROUND_RE.finditer(text)]
    qualification_spans = [m.span() for m in _FINAL_QUALIFICATION_PHRASE_RE.finditer(text)]

    def _inside_non_final(span: Tuple[int, int]) -> bool:
        return any(nf[0] <= span[0] and span[1] <= nf[1] for nf in non_final_spans)

    def _inside_qualification_phrase(span: Tuple[int, int]) -> bool:
        return any(qs[0] <= span[0] and span[1] <= qs[1] for qs in qualification_spans)

    indicators: List[Tuple[int, int, bool]] = [(s, e, False) for s, e in non_final_spans]
    for m in _BARE_FINAL_TOKEN_RE.finditer(text):
        if _inside_non_final(m.span()) or _inside_qualification_phrase(m.span()):
            continue
        indicators.append((m.start(), m.end(), True))
    return indicators


def title_names_the_final(title: str) -> bool:
    """ES: True si `title` nombra la final en sí (no una ronda previa).
    EN: True if `title` names the final itself (not an earlier round)."""
    return any(is_final for _, _, is_final in _round_indicator_spans(title or ""))


_ROUND_SOURCE_BOUNDARY_RE: Pattern[str] = re.compile(r"\n-\s|\n\n")
_SENTENCE_OR_SOURCE_BOUNDARY_RE: Pattern[str] = re.compile(r"[.!?¡¿]|\n\n|\n-\s")


def _sentence_span(text: str, start: int, end: int) -> Tuple[int, int]:
    left_matches = list(_SENTENCE_OR_SOURCE_BOUNDARY_RE.finditer(text, 0, start))
    sentence_start = left_matches[-1].end() if left_matches else 0
    right_match = _SENTENCE_OR_SOURCE_BOUNDARY_RE.search(text, end)
    sentence_end = right_match.start() if right_match else len(text)
    return sentence_start, sentence_end


def classify_round_context(
    full_text: str,
    abs_start: int = 0,
    abs_end: int = 0,
) -> str:
    """ES: Clasifica la ronda del evento situado en abs_start/abs_end. Retorna "final", "non_final" o "unknown".
    EN: Classifies the round of the event at abs_start/abs_end. Returns "final", "non_final", or "unknown"."""
    text = full_text or ""
    sentence_start, sentence_end = _sentence_span(text, abs_start, abs_end)

    sentence_indicators = [
        (
            sentence_start + indicator_start,
            sentence_start + indicator_end,
            is_final,
        )
        for indicator_start, indicator_end, is_final in
        _round_indicator_spans(text[sentence_start:sentence_end])
    ]

    if sentence_indicators:
        def distance(indicator: Tuple[int, int, bool]) -> int:
            indicator_start, indicator_end, _ = indicator
            if indicator_end <= abs_start:
                return abs_start - indicator_end
            if indicator_start >= abs_end:
                return indicator_start - abs_end
            return 0

        nearest = min(sentence_indicators, key=distance)
        return "final" if nearest[2] else "non_final"

    return "unknown"


def round_context_matches_final(
    full_text: str,
    abs_start: int = 0,
    abs_end: int = 0,
    *,
    allow_unknown: bool = False,
) -> bool:
    """ES: Compara si el contexto pertenece a la final; si es unknown y se evalúa una final, retorna False por defecto.
    EN: Checks whether the context belongs to the final; if unknown while evaluating a final, defaults to False."""
    classification = classify_round_context(full_text, abs_start, abs_end)
    if classification == "final":
        return True
    if classification == "non_final":
        return False
    return allow_unknown


def drop_non_final_round_events(
    events: List[Dict[str, Any]],
    query: str,
    full_text: str = "",
) -> List[Dict[str, Any]]:
    if not asks_about_final(query):
        return events

    text_for_tracking = full_text if full_text else " ".join(
        event.get("context", "") for event in events
    )

    return [
        event
        for event in events
        if round_context_matches_final(
            text_for_tracking,
            event.get("abs_start", 0),
            event.get("abs_end", 0),
            allow_unknown=False,
        )
    ]


# =====================================================================
# PARTICIPANTE NO CONFIRMADO POR LA FUENTE ESTRUCTURADA
# UNCONFIRMED PARTICIPANT AGAINST THE STRUCTURED SOURCE
# =====================================================================
_VS_MATCHUP_RE: Pattern[str] = re.compile(
    r"\b([A-ZÀ-Ý][a-zà-ÿ]{2,})\s+(?:vs\.?|versus|v\.|against)\s+([A-ZÀ-Ý][a-zà-ÿ]{2,})\b",
    re.IGNORECASE,
)
_AND_MATCHUP_RE: Pattern[str] = re.compile(
    r"\b([A-ZÀ-Ý][a-zà-ÿ]{2,})\s+and\s+([A-ZÀ-Ý][a-zà-ÿ]{2,})\s+"
    r"(?:face[\s-]off|meet|clash|battle|collide|play|take on|square off)\b",
    re.IGNORECASE,
)


def _matchup_entities(title: str) -> Set[str]:
    """ES: Extrae nombres propios que el título afirma que se enfrentan entre sí.
    EN: Extracts proper nouns the title claims are facing off against each other."""
    title = title or ""
    entities: Set[str] = set()
    for pattern in (_VS_MATCHUP_RE, _AND_MATCHUP_RE):
        for match in pattern.finditer(title):
            for word in match.groups():
                lw = word.lower()
                if lw not in _GENERIC_CONTEXT_WORDS:
                    entities.add(lw)
    return entities


def source_names_unconfirmed_participant(
    candidate_text: str,
    authoritative_text: str,
) -> bool:
    """ES: True si `candidate_text` afirma un enfrentamiento y ese participante no aparece en `authoritative_text`.
    EN: True if `candidate_text` claims a matchup and that participant doesn't appear in `authoritative_text`."""
    entities = _matchup_entities(candidate_text)
    auth_words = significant_words(authoritative_text)
    if not entities or not auth_words:
        return False
    return not entities.issubset(auth_words)