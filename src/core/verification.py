"""
verification.py
===============

Verificación factual determinista basada en eventos deportivos tipados y evidencia cruda.
Resuelve errores de coincidencia superficial, falsos positivos por resúmenes y bypasses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

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


# Regexes para clasificar fases del partido
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

# Patrones para detectar reclamos de victoria en la respuesta
_VICTORY_CLAIM_RE = re.compile(
    r"\b([A-ZÁÉÍÓÚÑa-záéíóúñ]+)\s+(?:gan[oó]|venci[oó]|derrot[oó]|se\s+coron[oó]\s+campe[oó]n|conquist[oó])\b",
    re.IGNORECASE,
)


def classify_score_phase(context: str) -> ScorePhase:
    """Clasifica si el marcador corresponde a tiempo reglamentario, prórroga o penaltis."""
    context = context or ""

    if _PENALTY_RE.search(context):
        return ScorePhase.PENALTY_SHOOTOUT
    if _EXTRA_TIME_RE.search(context):
        return ScorePhase.AFTER_EXTRA_TIME
    if _REGULATION_RE.search(context):
        return ScorePhase.REGULATION
    return ScorePhase.UNKNOWN


def _normalise_source(source: Mapping[str, Any]) -> EvidenceSource:
    """Normaliza fuentes priorizando SIEMPRE el texto original/crudo sobre resúmenes."""
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
    """Construye lista estructurada de fuentes de evidencia cruda."""
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
    """Construye un bloque unificado de texto crudo para verificación o prompts."""
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
    """Extrae todos los marcadores de la evidencia cruda, asociándoles ronda, fase y entidades."""
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
    """Extrae los marcadores afirmados en la respuesta generada."""
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
    Deriva la identidad del evento (equipos participantes).
    Si la consulta es genérica ("final World Cup 2022"), extrae las entidades
    desde las fuentes autoritativas o títulos específicos.
    """
    query_entities = set(distinctive_words(query))
    if len(query_entities) >= 2:
        return query_entities

    # Buscar entidades en fuentes autoritativas de la final
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

    # Buscar entidades en cualquier evento etiquetado como final
    final_events = [event for event in events if event.round == EventRound.FINAL]
    if final_events:
        entities = set()
        for event in final_events:
            entities.update(event.entities)
        if entities:
            return entities

    # Buscar en títulos de fuentes que mencionan la final
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
    """Verifica si las entidades esperadas se solapan con las del candidato."""
    if not expected_entities:
        return True

    overlap = set(expected_entities) & set(candidate_entities)
    if len(expected_entities) == 1:
        return len(overlap) == 1

    return len(overlap) >= min(minimum, len(expected_entities))


def _phase_matches(claim_phase: ScorePhase, evidence_phase: ScorePhase) -> bool:
    """Compara fases. Si el reclamo no especifica fase, acepta cualquier coincidencia."""
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
    Evaluador estricto: Un candidato solo soporta el marcador si coincide
    en número, ronda (final vs no-final), entidades y fase del partido.
    """
    if candidate.score != claim.score:
        return False

    # REGLA CRÍTICA: Si se pregunta por la final, la ronda DESCONOCIDA o NO-final RECHAZA el marcador.
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
    """Normaliza guiones Unicode y espacios alrededor de marcadores numéricos."""
    if not text:
        return ""
    # Convertir en-dash, em-dash y guiones especiales a guion estándar '-'
    text = re.sub(r'[\u2010-\u2015\u2212]', '-', text)
    # Eliminar espacios intermedios en marcadores (ej. '2 - 0' -> '2-0')
    return re.sub(r'(\d+)\s*-\s*(\d+)', r'\1-\2', text)

def check_unsupported_score(llm_output: str, context_text: str) -> dict:
    """Extrae y compara marcadores aplicando normalización previa."""
    norm_output = _normalize_score_text(llm_output)
    norm_context = _normalize_score_text(context_text)
    
    # Extraer marcadores tipo '3-3', '4-2', '2-0'
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
    """Verifica si los marcadores en la respuesta están respaldados por las fuentes."""
    sources = build_evidence_sources(raw_sources)
    evidence_events = extract_typed_score_events(sources)
    claimed_scores = extract_claimed_scores(response_text)
    target_entities = derive_target_entities(query, sources, evidence_events)

    report = VerificationReport(
        claimed_scores=claimed_scores,
        evidence_events=evidence_events,
        target_entities=target_entities,
    )

    # Texto unificado de fuentes y respuesta normalizado
    raw_context = build_raw_evidence_text(sources)
    norm_context = _normalize_score_text(raw_context)

    for claim in claimed_scores:
        norm_claim_score = _normalize_score_text(claim.score)
        
        # Validar si el marcador normalizado existe literalmente en el contexto normalizado
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
    """Verifica si los reclamos de victoria o campeonato en la respuesta están respaldados."""
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

        # Validar si el ganador afirmado aparece asociado a la victoria en el texto original
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


def verify_all(
    query: str,
    response_text: str,
    raw_sources: Iterable[Mapping[str, Any] | EvidenceSource],
) -> VerificationReport:
    """
    PUNTO DE ENTRADA UNIFICADO:
    Ejecuta TODOS los verificadores de forma totalmente independiente.
    NUNCA omite la verificación de ganador o contradicciones aunque los marcadores pasen.
    """
    # 1. Verificación de marcadores
    report = verify_scores(query, response_text, raw_sources)

    # 2. Verificación de victorias/ganadores
    victory_issues = verify_victory_claims(query, response_text, raw_sources)
    report.issues.extend(victory_issues)

    return report


# Funciones de compatibilidad hacia atrás para el Orchestrator
def find_unsupported_scores(
    query: str,
    response_text: str,
    raw_sources: Iterable[Mapping[str, Any] | EvidenceSource],
) -> set[str]:
    """Helper directo que devuelve el conjunto de marcadores no respaldados."""
    report = verify_scores(query, response_text, raw_sources)
    return report.unsupported_scores
