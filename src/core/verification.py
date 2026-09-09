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
verification.py

ES: Verificación factual determinista basada en eventos deportivos tipados
y evidencia cruda. Resuelve errores de coincidencia superficial, falsos
positivos por resúmenes y bypasses.

EN: Deterministic factual verification based on typed sports events and
raw evidence. Addresses surface-match errors, false positives caused by
summaries, and bypasses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Pattern, Sequence

from relevance import (
    asks_about_final,
    classify_round_context,
    distinctive_words,
    extract_score_events,
    requires_precise_fact,
    title_names_the_final,
)


class EventRound(str, Enum):
    FINAL = "final"
    NON_FINAL = "non_final"
    UNKNOWN = "unknown"


class ScorePhase(str, Enum):
    REGULATION = "regulation"
    AFTER_EXTRA_TIME = "after_extra_time"
    PENALTY_SHOOTOUT = "penalty_shootout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EvidenceSource:
    title: str
    url: str
    domain: str
    raw_text: str
    source_type: str = ""
    authoritative: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreEvent:
    score: str
    context: str
    source_title: str
    source_url: str
    source_index: int
    round: EventRound
    phase: ScorePhase
    entities: frozenset[str]
    abs_start: int
    abs_end: int


@dataclass(frozen=True)
class ClaimedScore:
    score: str
    context: str
    phase: ScorePhase
    entities: frozenset[str]


@dataclass(frozen=True)
class VerificationIssue:
    verifier: str  # "score", "victory", "contradiction"
    code: str
    message: str
    value: str = ""
    context: str = ""


@dataclass
class VerificationReport:
    claimed_scores: list[ClaimedScore] = field(default_factory=list)
    evidence_events: list[ScoreEvent] = field(default_factory=list)
    target_entities: set[str] = field(default_factory=set)
    issues: list[VerificationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def unsupported_scores(self) -> set[str]:
        return {
            issue.value
            for issue in self.issues
            if issue.verifier == "score"
            and issue.code == "unsupported_score"
            and issue.value
        }


# ES: Regexes para clasificar fases del partido.
# EN: Regexes to classify match phases.
_PENALTY_RE = re.compile(
    r"\b("
    r"penalt(?:y|ies)|penalty\s+shoot-?out|shoot-?out|"
    r"penales|penaltis|tanda\s+de\s+penales"
    r")\b",
    re.IGNORECASE,
)

_EXTRA_TIME_RE = re.compile(
    r"\b("
    r"after\s+extra\s+time|a\.?e\.?t\.?|extra\s+time|"
    r"tras\s+la\s+pr[oó]rroga|despu[eé]s\s+de\s+la\s+pr[oó]rroga|"
    r"pr[oó]rroga"
    r")\b",
    re.IGNORECASE,
)

_REGULATION_RE = re.compile(
    r"\b("
    r"regular\s+time|regulation\s+time|normal\s+time|90\s+minutes|"
    r"tiempo\s+reglamentario|tiempo\s+normal|90\s+minutos"
    r")\b",
    re.IGNORECASE,
)

# ES: Patrones para detectar reclamos de victoria en la respuesta.
# EN: Patterns to detect victory claims in the response.
_VICTORY_CLAIM_RE = re.compile(
    r"\b([A-ZÁÉÍÓÚÑa-záéíóúñ]+)\s+(?:gan[oó]|venci[oó]|derrot[oó]|se\s+coron[oó]\s+campe[oó]n|conquist[oó])\b",
    re.IGNORECASE,
)


def classify_score_phase(context: str) -> ScorePhase:
    """ES: Clasifica si el marcador corresponde a tiempo reglamentario,
    prórroga o penaltis.
    EN: Classifies whether the score corresponds to regulation time, extra
    time, or a penalty shootout."""
    context = context or ""

    if _PENALTY_RE.search(context):
        return ScorePhase.PENALTY_SHOOTOUT
    if _EXTRA_TIME_RE.search(context):
        return ScorePhase.AFTER_EXTRA_TIME
    if _REGULATION_RE.search(context):
        return ScorePhase.REGULATION
    return ScorePhase.UNKNOWN


def _normalise_source(source: Mapping[str, Any]) -> EvidenceSource:
    """ES: Normaliza fuentes priorizando SIEMPRE el texto original/crudo
    sobre resúmenes.
    EN: Normalizes sources, ALWAYS prioritizing original/raw text over
    summaries."""
    raw_text = str(
        source.get("raw_content")
        or source.get("original_text")
        or source.get("content")
        or source.get("snippet")
        or ""
    ).strip()

    title = str(source.get("title") or "").strip()
    url = str(source.get("url") or "").strip()
    domain = str(source.get("domain") or source.get("source") or "").strip()

    raw_metadata = source.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}

    domain_lower = domain.lower()
    url_lower = url.lower()

    authoritative = bool(
        source.get("authoritative")
        or metadata.get("authoritative")
        or any(
            domain_key in domain_lower or domain_key in url_lower
            for domain_key in ("wikipedia.org", "fifa.com", "uefa.com", "rsssf.org")
        )
    )

    return EvidenceSource(
        title=title,
        url=url,
        domain=domain,
        raw_text=raw_text,
        source_type=str(source.get("type") or ""),
        authoritative=authoritative,
        metadata=metadata,
    )


def build_evidence_sources(
    sources: Iterable[Mapping[str, Any]],
) -> list[EvidenceSource]:
    """ES: Construye la lista estructurada de fuentes de evidencia cruda.
    EN: Builds the structured list of raw evidence sources."""
    result: list[EvidenceSource] = []
    for source in sources or []:
        if not isinstance(source, Mapping):
            continue
        normalised = _normalise_source(source)
        if normalised.raw_text:
            result.append(normalised)
    return result


def build_raw_evidence_text(
    sources: Iterable[Mapping[str, Any] | EvidenceSource],
) -> str:
    """ES: Construye un bloque unificado de texto crudo para verificación
    o prompts.
    EN: Builds a unified block of raw text for verification or prompts."""
    blocks: list[str] = []
    for index, source in enumerate(sources or [], start=1):
        item = source if isinstance(source, EvidenceSource) else _normalise_source(source)
        if not item.raw_text:
            continue
        blocks.append(
            f"[SOURCE {index}]\n"
            f"Title: {item.title}\n"
            f"URL: {item.url}\n"
            f"Original text:\n{item.raw_text}"
        )
    return "\n\n".join(blocks)


def extract_typed_score_events(
    sources: Sequence[EvidenceSource],
) -> list[ScoreEvent]:
    """ES: Extrae todos los marcadores de la evidencia cruda, asociándoles
    ronda, fase y entidades.
    EN: Extracts every score from the raw evidence, tagging each with a
    round, phase, and set of entities."""
    result: list[ScoreEvent] = []

    for source_index, source in enumerate(sources):
        for event in extract_score_events(source.raw_text):
            round_value = classify_round_context(
                source.raw_text,
                event.get("abs_start", 0),
                event.get("abs_end", 0),
            )

            if round_value == "final":
                event_round = EventRound.FINAL
            elif round_value == "non_final":
                event_round = EventRound.NON_FINAL
            else:
                event_round = EventRound.UNKNOWN

            context = str(event.get("context") or "")

            result.append(
                ScoreEvent(
                    score=str(event.get("score") or ""),
                    context=context,
                    source_title=source.title,
                    source_url=source.url,
                    source_index=source_index,
                    round=event_round,
                    phase=classify_score_phase(context),
                    entities=frozenset(distinctive_words(context)),
                    abs_start=int(event.get("abs_start", 0)),
                    abs_end=int(event.get("abs_end", 0)),
                )
            )

    return result


def extract_claimed_scores(response_text: str) -> list[ClaimedScore]:
    """ES: Extrae los marcadores afirmados en la respuesta generada.
    EN: Extracts the scores claimed in the generated response."""
    claims: list[ClaimedScore] = []
    for event in extract_score_events(response_text or ""):
        context = str(event.get("context") or "")
        claims.append(
            ClaimedScore(
                score=str(event.get("score") or ""),
                context=context,
                phase=classify_score_phase(context),
                entities=frozenset(distinctive_words(context)),
            )
        )
    return claims


def derive_target_entities(
    query: str,
    sources: Sequence[EvidenceSource],
    events: Sequence[ScoreEvent],
) -> set[str]:
    """
    ES: Deriva la identidad del evento (equipos participantes). Si la
    consulta es genérica ("final World Cup 2022"), extrae las entidades
    desde las fuentes autoritativas o títulos específicos.

    EN: Derives the event's identity (participating teams). If the query
    is generic ("final World Cup 2022"), extracts the entities from
    authoritative sources or specific titles instead.
    """
    query_entities = set(distinctive_words(query))
    if len(query_entities) >= 2:
        return query_entities

    # ES: Buscar entidades en fuentes autoritativas de la final.
    # EN: Look for entities in authoritative sources about the final.
    final_authoritative = [
        event for event in events
        if event.round == EventRound.FINAL and sources[event.source_index].authoritative
    ]
    if final_authoritative:
        entities: set[str] = set()
        for event in final_authoritative:
            entities.update(event.entities)
        if entities:
            return entities

    # ES: Buscar entidades en cualquier evento etiquetado como final.
    # EN: Look for entities in any event tagged as the final.
    final_events = [event for event in events if event.round == EventRound.FINAL]
    if final_events:
        entities = set()
        for event in final_events:
            entities.update(event.entities)
        if entities:
            return entities

    # ES: Buscar en títulos de fuentes que mencionan la final.
    # EN: Look in the titles of sources that mention the final.
    for index, source in enumerate(sources):
        if not title_names_the_final(source.title):
            continue
        source_events = [e for e in events if e.source_index == index]
        entities = set()
        for event in source_events:
            entities.update(event.entities)
        if entities:
            return entities

    return query_entities


def _entity_match(
    expected_entities: set[str] | frozenset[str],
    candidate_entities: set[str] | frozenset[str],
    minimum: int = 2,
) -> bool:
    """ES: Verifica si las entidades esperadas se solapan con las del
    candidato.
    EN: Checks whether the expected entities overlap with the
    candidate's."""
    if not expected_entities:
        return True

    overlap = set(expected_entities) & set(candidate_entities)
    if len(expected_entities) == 1:
        return len(overlap) == 1

    return len(overlap) >= min(minimum, len(expected_entities))


def _phase_matches(claim_phase: ScorePhase, evidence_phase: ScorePhase) -> bool:
    """ES: Compara fases. Si el reclamo no especifica fase, acepta
    cualquier coincidencia.
    EN: Compares phases. If the claim doesn't specify a phase, any match
    is accepted."""
    if claim_phase == ScorePhase.UNKNOWN:
        return True
    return claim_phase == evidence_phase


def _candidate_supports_claim(
    query: str,
    claim: ClaimedScore,
    candidate: ScoreEvent,
    target_entities: set[str],
) -> bool:
    """
    ES: Evaluador estricto — un candidato solo soporta el marcador si
    coincide en número, ronda (final vs. no final), entidades y fase del
    partido.

    EN: Strict evaluator — a candidate only supports the score if it
    matches on number, round (final vs. non-final), entities, and match
    phase.
    """
    if candidate.score != claim.score:
        return False

    # ES: Regla crítica — si se pregunta por la final, una ronda
    #     desconocida o no-final rechaza el marcador.
    # EN: Critical rule — if the query asks about the final, an unknown or
    #     non-final round rejects the score.
    if asks_about_final(query):
        if candidate.round != EventRound.FINAL:
            return False

    if not _entity_match(target_entities, candidate.entities):
        return False

    if claim.entities and not _entity_match(set(claim.entities), candidate.entities, minimum=1):
        return False

    if not _phase_matches(claim.phase, candidate.phase):
        return False

    return True

def _normalize_score_text(text: str) -> str:
    """ES: Normaliza guiones Unicode y espacios alrededor de marcadores
    numéricos.
    EN: Normalizes Unicode dashes and spacing around numeric scores."""
    if not text:
        return ""
    # ES: Convertir en-dash, em-dash y guiones especiales a guion estándar '-'.
    # EN: Convert en-dash, em-dash, and special dashes to a plain '-'.
    text = re.sub(r'[‐-―−]', '-', text)
    # ES: Eliminar espacios intermedios en marcadores (ej. '2 - 0' -> '2-0').
    # EN: Remove inner spacing in scores (e.g. '2 - 0' -> '2-0').
    return re.sub(r'(\d+)\s*-\s*(\d+)', r'\1-\2', text)

def check_unsupported_score(llm_output: str, context_text: str) -> dict:
    """ES: Extrae y compara marcadores aplicando normalización previa.
    EN: Extracts and compares scores after applying normalization."""
    norm_output = _normalize_score_text(llm_output)
    norm_context = _normalize_score_text(context_text)

    # ES: Extraer marcadores tipo '3-3', '4-2', '2-0'.
    # EN: Extract scores of the form '3-3', '4-2', '2-0'.
    found_scores = set(re.findall(r'\b\d+-\d+\b', norm_output))
    unsupported = set()

    for score in found_scores:
        if score not in norm_context:
            unsupported.add(score)

    return {
        "name": "unsupported_score",
        "triggered": len(unsupported) > 0,
        "detail": list(unsupported)
    }


def verify_scores(
    query: str,
    response_text: str,
    raw_sources: Iterable[Mapping[str, Any] | EvidenceSource],
) -> VerificationReport:
    """ES: Verifica si los marcadores en la respuesta están respaldados
    por las fuentes.
    EN: Verifies whether the scores in the response are backed by the
    sources."""
    sources = build_evidence_sources(raw_sources)
    evidence_events = extract_typed_score_events(sources)
    claimed_scores = extract_claimed_scores(response_text)
    target_entities = derive_target_entities(query, sources, evidence_events)

    report = VerificationReport(
        claimed_scores=claimed_scores,
        evidence_events=evidence_events,
        target_entities=target_entities,
    )

    # ES: Texto unificado de fuentes y respuesta, normalizado.
    # EN: Unified, normalized text of sources and response.
    raw_context = build_raw_evidence_text(sources)
    norm_context = _normalize_score_text(raw_context)

    for claim in claimed_scores:
        norm_claim_score = _normalize_score_text(claim.score)

        # ES: Validar si el marcador normalizado existe literalmente en el
        #     contexto normalizado.
        # EN: Check whether the normalized score exists literally in the
        #     normalized context.
        supported_by_text = norm_claim_score in norm_context
        supported_by_event = any(
            _candidate_supports_claim(query, claim, candidate, target_entities)
            for candidate in evidence_events
        )

        if not (supported_by_text or supported_by_event):
            report.issues.append(
                VerificationIssue(
                    verifier="score",
                    code="unsupported_score",
                    value=claim.score,
                    context=claim.context,
                    message=f"El marcador '{claim.score}' no tiene respaldo suficiente en las fuentes.",
                )
            )

    return report


def verify_victory_claims(
    query: str,
    response_text: str,
    raw_sources: Iterable[Mapping[str, Any] | EvidenceSource],
) -> list[VerificationIssue]:
    """ES: Verifica si los reclamos de victoria o campeonato en la
    respuesta están respaldados.
    EN: Verifies whether victory or championship claims in the response
    are backed by evidence."""
    issues: list[VerificationIssue] = []
    if not requires_precise_fact(query):
        return issues

    raw_text_combined = " ".join(
        (s.raw_text if isinstance(s, EvidenceSource) else _normalise_source(s).raw_text)
        for s in (raw_sources or [])
    ).lower()

    for match in _VICTORY_CLAIM_RE.finditer(response_text or ""):
        claimed_winner = match.group(1).strip()
        winner_words = distinctive_words(claimed_winner)

        if not winner_words:
            continue

        # ES: Validar si el ganador afirmado aparece asociado a la victoria
        #     en el texto original.
        # EN: Check whether the claimed winner appears associated with the
        #     victory in the original text.
        supported = any(word.lower() in raw_text_combined for word in winner_words)
        if not supported:
            issues.append(
                VerificationIssue(
                    verifier="victory",
                    code="unsupported_winner",
                    value=claimed_winner,
                    context=match.group(0),
                    message=f"La afirmación de victoria para '{claimed_winner}' no tiene respaldo en la evidencia cruda.",
                )
            )

    return issues


# =====================================================================
# AUTOVERIFICACIÓN GENÉRICA DE HALLAZGOS WEB (no deportivos)
# GENERIC WEB-FINDING SELF-VERIFICATION (non-sports)
# =====================================================================
# ES: Generaliza verify_scores() / verify_victory_claims() (mismo pipeline:
#     build_evidence_sources -> texto crudo de fuentes -> chequeo de
#     respaldo léxico vía distinctive_words) a CUALQUIER afirmación
#     cuantitativa o fáctica puntual extraída de una búsqueda web genérica
#     — precio, fecha, cifra, versión, nombre propio — no solo
#     marcadores/ganadores de deporte, que es el único dominio que
#     verify_scores/verify_victory_claims cubren.
#
#     Deliberadamente NO se pliega dentro de verify_all(): verify_all() es
#     el punto de entrada ya usado por Orchestrator para el dominio
#     deportivo con su propia semántica ("nunca omite ganador ni
#     contradicciones"); mezclar ambos dominios ahí cambiaría el contrato
#     para callers existentes. Este verificador se invoca aparte, desde el
#     flujo de conocimiento web general (ver
#     Orchestrator._persist_web_knowledge / fetch_hybrid_context).
# EN: Generalizes verify_scores() / verify_victory_claims() (same
#     pipeline: build_evidence_sources -> raw source text -> lexical
#     backing check via distinctive_words) to ANY quantitative or factual
#     claim extracted from a generic web search — price, date, figure,
#     version, proper name — not just sports scores/winners, the only
#     domain verify_scores/verify_victory_claims cover.
#
#     Deliberately NOT folded into verify_all(): verify_all() is the entry
#     point Orchestrator already uses for the sports domain with its own
#     semantics ("never skips winner or contradiction checks"); mixing
#     both domains there would change the contract for existing callers.
#     This verifier is invoked separately, from the general web-knowledge
#     flow (see Orchestrator._persist_web_knowledge / fetch_hybrid_context).
_GENERIC_CLAIM_RE: Pattern[str] = re.compile(
    r"([A-Za-zÁÉÍÓÚÑáéíóúñ0-9][\w.,%$€/-]*(?:\s+[A-Za-zÁÉÍÓÚÑáéíóúñ0-9][\w.,%$€/-]*){0,6}"
    r"\s+(?:cuesta|vale|es de|son|fue de|alcanz[oó]|lleg[oó] a|equivale a|"
    r"costs?|is|was|are|reached|equals?)\s+"
    r"[\d][\d.,]*\s*(?:%|USD|EUR|MXN|COP|ARS|CLP|\$|€)?)",
    re.IGNORECASE,
)
_NUMERIC_TOKEN_RE: Pattern[str] = re.compile(r"\d[\d.,]*")


def verify_web_findings(
    query: str,
    response_text: str,
    raw_sources: Iterable[Mapping[str, Any] | EvidenceSource],
) -> list[VerificationIssue]:
    """
    ES: Autoverificación de hallazgos web genéricos: coteja afirmaciones
    cuantitativas/fácticas en `response_text` contra el texto crudo de
    `raw_sources`, con el mismo criterio de respaldo léxico que ya usa
    verify_victory_claims (palabras distintivas presentes en la evidencia
    cruda), pero relajado a "al menos la mitad" en vez de "al menos una",
    porque una afirmación genérica trae más palabras propias (sujeto +
    cifra + unidad) que un nombre de ganador, y exigir 100% de solape
    penalizaría una paráfrasis razonable del modelo.

    No depende de requires_precise_fact() (ese gate es específico del
    dominio deportivo de relevance.py) — se asume que el caller ya decidió
    que esta consulta amerita verificación antes de invocar esta función.

    EN: Self-verification of generic web findings: checks quantitative/
    factual claims in `response_text` against the raw text of
    `raw_sources`, using the same lexical-backing criterion as
    verify_victory_claims (distinctive words present in the raw evidence),
    but relaxed to "at least half" instead of "at least one", because a
    generic claim carries more content words (subject + figure + unit)
    than a winner's name, and requiring 100% overlap would penalize a
    reasonable paraphrase by the model.

    Does not depend on requires_precise_fact() (that gate is specific to
    relevance.py's sports domain) — the caller is assumed to have already
    decided this query warrants verification before calling this function.
    """
    issues: list[VerificationIssue] = []
    sources = build_evidence_sources(raw_sources)
    if not sources:
        return issues

    raw_context = build_raw_evidence_text(sources).lower()

    for match in _GENERIC_CLAIM_RE.finditer(response_text or ""):
        claim_text = match.group(1).strip()
        claim_words = distinctive_words(claim_text)

        # ES: distinctive_words() excluye a propósito los tokens
        #     puramente numéricos (pensada para identificar PARTICIPANTES,
        #     no cifras) — reutilizarla tal cual dejaría los números sin
        #     verificar, por eso se extraen aparte, directo de claim_text.
        # EN: distinctive_words() deliberately excludes purely numeric
        #     tokens (it's meant to identify PARTICIPANTS, not figures) —
        #     reusing it as-is would leave numbers unverified, so they are
        #     extracted separately, straight from claim_text.
        numeric_tokens = set(_NUMERIC_TOKEN_RE.findall(claim_text))
        non_numeric_words = claim_words

        if not non_numeric_words and not numeric_tokens:
            continue

        # ES: La cifra puntual es la parte de la afirmación que un modelo
        #     alucina con más frecuencia manteniendo intacto el resto de
        #     la oración (sujeto, verbo, unidad) — un solape léxico
        #     genérico sobre solo las palabras no-numéricas la dejaría
        #     pasar igual, por eso la cifra se valida aparte y de forma
        #     estricta: si la afirmación trae un número, ese número tiene
        #     que aparecer literal en el texto crudo de las fuentes, sin
        #     excepción.
        # EN: The specific figure is the part of a claim a model most
        #     often hallucinates while keeping the rest of the sentence
        #     intact (subject, verb, unit) — a generic lexical overlap over
        #     only the non-numeric words would let it through anyway, so
        #     the figure is validated separately and strictly: if the claim
        #     carries a number, that number must appear literally in the
        #     sources' raw text, no exceptions.
        numeric_supported = all(tok in raw_context for tok in numeric_tokens)

        text_overlap = sum(1 for w in non_numeric_words if w in raw_context)
        text_supported = (
            not non_numeric_words
            or text_overlap >= max(1, len(non_numeric_words) // 2)
        )

        if not (numeric_supported and text_supported):
            issues.append(
                VerificationIssue(
                    verifier="web_finding",
                    code="unsupported_finding",
                    value=claim_text,
                    context=match.group(0),
                    message=(
                        f"El hallazgo '{claim_text}' no tiene respaldo léxico "
                        f"suficiente en las fuentes web recuperadas."
                    ),
                )
            )

    return issues


def verify_all(
    query: str,
    response_text: str,
    raw_sources: Iterable[Mapping[str, Any] | EvidenceSource],
) -> VerificationReport:
    """
    ES: Punto de entrada unificado — ejecuta todos los verificadores de
    forma totalmente independiente. Nunca omite la verificación de ganador
    o contradicciones aunque los marcadores pasen.

    EN: Unified entry point — runs all verifiers fully independently.
    Never skips the winner or contradiction check even if the scores pass.
    """
    # ES: 1. Verificación de marcadores.
    # EN: 1. Score verification.
    report = verify_scores(query, response_text, raw_sources)

    # ES: 2. Verificación de victorias/ganadores.
    # EN: 2. Victory/winner verification.
    victory_issues = verify_victory_claims(query, response_text, raw_sources)
    report.issues.extend(victory_issues)

    return report


# ES: Funciones de compatibilidad hacia atrás para el Orchestrator.
# EN: Backward-compatibility functions for the Orchestrator.
def find_unsupported_scores(
    query: str,
    response_text: str,
    raw_sources: Iterable[Mapping[str, Any] | EvidenceSource],
) -> set[str]:
    """ES: Helper directo que devuelve el conjunto de marcadores no
    respaldados.
    EN: Direct helper that returns the set of unsupported scores."""
    report = verify_scores(query, response_text, raw_sources)
    return report.unsupported_scores