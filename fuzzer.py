"""
El Monolito Personal - Arquitectura v2.0
Paso 4: Motor Adversarial y Fuzzing Lógico (Dominio Conceptual)

Componente de segundo pase (actor-crítico) invocado por el orquestador
cuando el Router deriva una consulta al SLOW_PATH conceptual: hipótesis,
arquitecturas, afirmaciones estratégicas o ideas abstractas donde NO
existe un árbitro formal externo como el CAS.

Audita la ESTRUCTURA LÓGICA de una premisa en lenguaje natural buscando
patrones de fragilidad argumental conocidos, y devuelve un veredicto
determinista, reproducible y 100% local: sin llamadas a LLM ni a APIs
externas, basado en heurísticas léxico-sintácticas explícitas.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Pattern, Sequence, Tuple


class FragilityCategory(str, Enum):
    """Categorías de debilidad estructural detectables sobre la premisa."""
    UNBOUNDED_QUANTIFIER = "unbounded_quantifier"
    CAUSAL_LEAP = "causal_leap"
    UNFALSIFIABLE_CLAIM = "unfalsifiable_claim"
    VAGUE_TERMINOLOGY = "vague_terminology"
    SINGLE_POINT_OF_FAILURE = "single_point_of_failure"
    MISSING_COUNTEREVIDENCE = "missing_counterevidence"


class CounterArgumentStrength(str, Enum):
    """Fuerza relativa de un contraargumento generado, derivada de la severidad."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FuzzingVerdict(str, Enum):
    """Veredicto agregado sobre la robustez estructural de la premisa."""
    ROBUST = "robust"
    FRAGILE = "fragile"
    CRITICALLY_FRAGILE = "critically_fragile"


@dataclass(frozen=True)
class FragilePoint:
    """Un punto frágil concreto localizado en la premisa."""
    category: FragilityCategory
    excerpt: str
    sentence_index: int
    severity: float
    rationale: str

    def __str__(self) -> str:
        return (
            f"[{self.category.value.upper()}] (sev={self.severity:.2f}) "
            f"'{self.excerpt}' -> {self.rationale}"
        )


@dataclass(frozen=True)
class CounterArgument:
    """Contraargumento lógico templado, generado a partir de un `FragilePoint`."""
    target_category: FragilityCategory
    target_excerpt: str
    argument: str
    strength: CounterArgumentStrength

    def __str__(self) -> str:
        return (
            f"[CONTRA:{self.strength.value.upper()}] vs '{self.target_excerpt}' "
            f"-> {self.argument}"
        )


@dataclass(frozen=True)
class SearchRequirement:
    """Requerimiento de búsqueda o precedente obligatorio antes de aceptar la premisa."""
    query: str
    justification: str
    priority: int

    def __str__(self) -> str:
        return f"[BUSQUEDA P{self.priority}] '{self.query}' — {self.justification}"


@dataclass(frozen=True)
class FuzzingResult:
    """Resultado inmutable y auditable de una pasada de fuzzing lógico."""
    premise: str
    fragile_points: Tuple[FragilePoint, ...]
    counterarguments: Tuple[CounterArgument, ...]
    search_requirements: Tuple[SearchRequirement, ...]
    robustness_score: float
    verdict: FuzzingVerdict
    elapsed_ms: float
    detail: str

    def __str__(self) -> str:
        return (
            f"[FUZZ:{self.verdict.value.upper()}] score={self.robustness_score:.1f}/100 "
            f"puntos_fragiles={len(self.fragile_points)} "
            f"contraargumentos={len(self.counterarguments)} "
            f"busquedas={len(self.search_requirements)} "
            f"({self.elapsed_ms:.3f} ms) | {self.detail}"
        )

    def report(self) -> str:
        lines: List[str] = [str(self), ""]
        lines.append("-- Puntos frágiles detectados --")
        if self.fragile_points:
            for fp in self.fragile_points:
                lines.append(f"  {fp}")
        else:
            lines.append("  (ninguno)")
        lines.append("")
        lines.append("-- Contraargumentos generados --")
        if self.counterarguments:
            for ca in self.counterarguments:
                lines.append(f"  {ca}")
        else:
            lines.append("  (ninguno)")
        lines.append("")
        lines.append("-- Requerimientos de búsqueda / precedentes --")
        if self.search_requirements:
            for sr in self.search_requirements:
                lines.append(f"  {sr}")
        else:
            lines.append("  (ninguno)")
        return "\n".join(lines)


class AdversarialFuzzer:
    """
    Motor de fuzzing lógico determinista para hipótesis y premisas
    conceptuales sin árbitro formal externo.
    """

    MAX_PREMISE_LENGTH: int = 20_000
    ROBUST_THRESHOLD: float = 70.0
    FRAGILE_THRESHOLD: float = 40.0
    EXCERPT_RADIUS: int = 30

    _CATEGORY_WEIGHT: Dict[FragilityCategory, float] = {
        FragilityCategory.UNBOUNDED_QUANTIFIER: 22.0,
        FragilityCategory.CAUSAL_LEAP: 26.0,
        FragilityCategory.UNFALSIFIABLE_CLAIM: 24.0,
        FragilityCategory.VAGUE_TERMINOLOGY: 10.0,
        FragilityCategory.SINGLE_POINT_OF_FAILURE: 18.0,
        FragilityCategory.MISSING_COUNTEREVIDENCE: 16.0,
    }

    _SENTENCE_SPLIT_PATTERN: Pattern[str] = re.compile(r"(?<=[.!?;\n])\s+")

    _UNPRECEDENTED_CLAIM_PATTERN: Pattern[str] = re.compile(
        r"\b("
        r"descubrimiento\s+(?:completamente\s+)?nuevo|hallazgo\s+in[eé]dito|"
        r"observacio(?:n|nes)\s+in[eé]ditas?|nunca\s+antes\s+vist[oa]s?|"
        r"territorios?\s+del\b.+\binexplorados?|completamente\s+in[eé]dit[oa]"
        r")\b",
        re.IGNORECASE,
    )

    _UNBOUNDED_PATTERN: Pattern[str] = re.compile(
        r"\b(siempre|nunca|jamás|todos?|todas?|ningun[oa]|cualquier|"
        r"cada uno|garantiz\w+|imposible|inevitabl\w*|absolutamente|"
        r"totalmente|completamente|perfect[oa]|100\s?%|sin excepci[oó]n|"
        r"en todos los casos|universalmente|always|never|every|all|none|"
        r"guarantee[sd]?|impossible|totally|absolutely|perfect(?:ly)?)\b",
        re.IGNORECASE,
    )

    _HEDGE_PATTERN: Pattern[str] = re.compile(
        r"\b(excepto|salvo|a menos que|con la excepci[oó]n|en algunos casos|"
        r"generalmente|normalmente|suele[n]?|tiende[n]? a|puede ser que|"
        r"es posible que|bajo ciertas condiciones|dependiendo de|"
        r"except|unless|in some cases|generally|typically|may|might|"
        r"arguably|depending on)\b",
        re.IGNORECASE,
    )

    _CAUSAL_CONNECTOR_PATTERN: Pattern[str] = re.compile(
        r"\b(por lo tanto|por consiguiente|en consecuencia|"
        r"esto significa que|esto implica que|así que|de ahí que|"
        r"lo cual conduce a|conduce a que|therefore|thus|hence|"
        r"implies that|this means that|leads to)\b",
        re.IGNORECASE,
    )

    _MECHANISM_PATTERN: Pattern[str] = re.compile(
        r"\b(mecanismo\w*|proceso\w*|mediante|a trav[eé]s de|debido a|"
        r"porque|dado que|ya que|puesto que|se debe a|via|through|"
        r"because|mechanism\w*|process\w*|due to|owing to)\b",
        re.IGNORECASE,
    )

    _VAGUE_PATTERN: Pattern[str] = re.compile(
        r"\b(sinergia\w*|sinerg\w*|paradigma\w*|paradigm\w*|disruptiv\w*|"
        r"disrupt\w*|escalable\w*|scalable|revolucionari\w*|"
        r"revolutionary|innovador\w*|innovative|[oó]ptim\w*|optimal\w*|"
        r"eficient\w*|efficient\w*|hol[ií]stic\w*|holistic|"
        r"world[\s-]?class|game[\s-]?chang\w*)\b",
        re.IGNORECASE,
    )

    _DEFINITION_PATTERN: Pattern[str] = re.compile(
        r"\b(que significa|definido como|se refiere a|entendido como|"
        r"is defined as|means that|refers to)\b",
        re.IGNORECASE,
    )

    _SINGLE_POINT_PATTERN: Pattern[str] = re.compile(
        r"\b(el [uú]nico|la [uú]nica (?:forma|manera|opci[oó]n|v[ií]a)|"
        r"no hay alternativa\w*|sin alternativa\w*|no existe otra|"
        r"the only way|the only option|the only solution|no other option)\b",
        re.IGNORECASE,
    )

    _LIMITATION_PATTERN: Pattern[str] = re.compile(
        r"\b(limitaci[oó]n\w*|riesgo\w*|desventaja\w*|contraejemplo\w*|"
        r"salvedad\w*|excepci[oó]n\w*|caso l[ií]mite|"
        r"limitation\w*|risk\w*|drawback\w*|caveat\w*|counterexample\w*|"
        r"edge case\w*|trade[\s-]?off\w*)\b",
        re.IGNORECASE,
    )

    _COUNTER_TEMPLATES: Dict[FragilityCategory, str] = {
        FragilityCategory.UNBOUNDED_QUANTIFIER: (
            "¿Existe al menos un caso, contexto o condición documentada "
            "donde '{excerpt}' no se cumple? De existir, la premisa "
            "requiere acotación explícita de su dominio de validez."
        ),
        FragilityCategory.CAUSAL_LEAP: (
            "¿Cuál es el mecanismo intermedio que conecta '{excerpt}' con "
            "la consecuencia declarada? Sin ese eslabón explícito, la "
            "relación es una correlación asumida, no una causalidad "
            "demostrada."
        ),
        FragilityCategory.UNFALSIFIABLE_CLAIM: (
            "La premisa, formulada en torno a '{excerpt}', no define "
            "ninguna observación posible que pudiera refutarla. Toda "
            "afirmación no falsificable debe tratarse como no verificada "
            "hasta definir un criterio explícito de refutación."
        ),
        FragilityCategory.VAGUE_TERMINOLOGY: (
            "El término usado en '{excerpt}' carece de definición "
            "operacional o métrica verificable. ¿Qué valor, umbral o "
            "criterio observable delimita su cumplimiento?"
        ),
        FragilityCategory.SINGLE_POINT_OF_FAILURE: (
            "Afirmar en '{excerpt}' que no existen alternativas es en sí "
            "mismo un cuantificador absoluto: exige haber descartado "
            "explícitamente todas las vías conocidas, no solo las "
            "consideradas por el autor."
        ),
        FragilityCategory.MISSING_COUNTEREVIDENCE: (
            "Ninguna premisa robusta debería carecer de limitaciones "
            "reconocidas. La ausencia total de salvedades en '{excerpt}' "
            "sugiere que no se buscó activamente evidencia en contra "
            "antes de formularla."
        ),
    }

    def audit(self, premise: str) -> FuzzingResult:
        """Ejecuta la pasada adversarial completa sobre `premise`."""
        start = time.perf_counter()

        if not premise or not premise.strip():
            return self._degenerate_result(
                premise, start, "Premisa vacía: no auditable estructuralmente."
            )
        if len(premise) > self.MAX_PREMISE_LENGTH:
            return self._degenerate_result(
                premise, start,
                "Premisa excede la longitud máxima permitida para auditoría.",
            )

        sentences = self._split_sentences(premise)

        fragile_points: List[FragilePoint] = []
        fragile_points.extend(self._detect_unprecedented_claims(sentences))  # <--- Integración correcta
        fragile_points.extend(self._detect_unbounded_and_unfalsifiable(sentences, premise))
        fragile_points.extend(self._detect_causal_leaps(sentences))
        fragile_points.extend(self._detect_vague_terminology(sentences))
        fragile_points.extend(self._detect_single_point_of_failure(sentences))
        fragile_points.extend(self._detect_missing_counterevidence(premise))

        fragile_points.sort(key=lambda fp: fp.severity, reverse=True)


        score = self._compute_robustness_score(fragile_points)
        verdict = self._determine_verdict(score)
        counterarguments = self._build_counterarguments(fragile_points)
        search_requirements = self._build_search_requirements(fragile_points, verdict)

        elapsed = (time.perf_counter() - start) * 1000
        detail = (
            f"Se analizaron {len(sentences)} oración(es); "
            f"{len(fragile_points)} punto(s) frágil(es) detectado(s)."
        )

        return FuzzingResult(
            premise=premise,
            fragile_points=tuple(fragile_points),
            counterarguments=tuple(counterarguments),
            search_requirements=tuple(search_requirements),
            robustness_score=score,
            verdict=verdict,
            elapsed_ms=elapsed,
            detail=detail,
        )

    def _degenerate_result(self, premise: str, start: float, detail: str) -> FuzzingResult:
        elapsed = (time.perf_counter() - start) * 1000
        return FuzzingResult(
            premise=premise or "",
            fragile_points=(),
            counterarguments=(),
            search_requirements=(),
            robustness_score=0.0,
            verdict=FuzzingVerdict.CRITICALLY_FRAGILE,
            elapsed_ms=elapsed,
            detail=detail,
        )

    def _split_sentences(self, text: str) -> List[str]:
        raw = self._SENTENCE_SPLIT_PATTERN.split(text.strip())
        return [s.strip() for s in raw if s.strip()]

    def _excerpt(self, sentence: str, match: "re.Match[str]") -> str:
        radius = self.EXCERPT_RADIUS
        start = max(0, match.start() - radius)
        end = min(len(sentence), match.end() + radius)
        snippet = sentence[start:end].strip()
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(sentence) else ""
        return f"{prefix}{snippet}{suffix}"

    def _excerpt_prefix(self, text: str, length: int = 80) -> str:
        t = text.strip()
        return t[:length] + ("…" if len(t) > length else "")

    def _detect_unprecedented_claims(self, sentences: Sequence[str]) -> List[FragilePoint]:
        points: List[FragilePoint] = []
        for idx, sentence in enumerate(sentences):
            match = self._UNPRECEDENTED_CLAIM_PATTERN.search(sentence)
            if match:
                points.append(FragilePoint(
                    category=FragilityCategory.UNFALSIFIABLE_CLAIM,
                    excerpt=self._excerpt(sentence, match),
                    sentence_index=idx,
                    severity=0.85,
                    rationale=(
                        "Se atribuye un descubrimiento empírico inédito sin "
                        "mecanismo matemático, evidencia de laboratorio ni "
                        "revisión por pares que lo respalde."
                    ),
                ))
        return points

    def _detect_unbounded_and_unfalsifiable(
        self, sentences: Sequence[str], full_text: str
    ) -> List[FragilePoint]:
        points: List[FragilePoint] = []
        quantifier_found = False
        hedge_found_globally = bool(self._HEDGE_PATTERN.search(full_text))

        for idx, sentence in enumerate(sentences):
            for match in self._UNBOUNDED_PATTERN.finditer(sentence):
                quantifier_found = True
                has_local_hedge = bool(self._HEDGE_PATTERN.search(sentence))
                severity = 0.35 if has_local_hedge else 0.55
                rationale = (
                    "Cuantificador absoluto mitigado parcialmente por una "
                    "salvedad presente en la misma oración."
                    if has_local_hedge
                    else "Cuantificador absoluto sin acotación explícita de "
                    "alcance, contexto o excepciones."
                )
                points.append(FragilePoint(
                    category=FragilityCategory.UNBOUNDED_QUANTIFIER,
                    excerpt=self._excerpt(sentence, match),
                    sentence_index=idx,
                    severity=severity,
                    rationale=rationale,
                ))

        if quantifier_found and not hedge_found_globally:
            points.append(FragilePoint(
                category=FragilityCategory.UNFALSIFIABLE_CLAIM,
                excerpt=self._excerpt_prefix(full_text),
                sentence_index=-1,
                severity=0.70,
                rationale=(
                    "La premisa emplea cuantificadores absolutos sin ninguna "
                    "salvedad, condición o excepción en todo el texto, lo "
                    "cual la vuelve no falsificable."
                ),
            ))
        return points

    def _detect_causal_leaps(self, sentences: Sequence[str]) -> List[FragilePoint]:
        points: List[FragilePoint] = []
        for idx, sentence in enumerate(sentences):
            match = self._CAUSAL_CONNECTOR_PATTERN.search(sentence)
            if match is None:
                continue
            window = sentence if idx == 0 else f"{sentences[idx - 1]} {sentence}"
            has_mechanism = bool(self._MECHANISM_PATTERN.search(window))
            if has_mechanism:
                continue
            points.append(FragilePoint(
                category=FragilityCategory.CAUSAL_LEAP,
                excerpt=self._excerpt(sentence, match),
                sentence_index=idx,
                severity=0.65,
                rationale=(
                    "Se afirma una relación causal/consecuencial sin "
                    "describir mecanismo, proceso intermedio o "
                    "justificación explícita."
                ),
            ))
        return points

    def _detect_vague_terminology(self, sentences: Sequence[str]) -> List[FragilePoint]:
        points: List[FragilePoint] = []
        for idx, sentence in enumerate(sentences):
            if self._DEFINITION_PATTERN.search(sentence):
                continue
            for match in self._VAGUE_PATTERN.finditer(sentence):
                points.append(FragilePoint(
                    category=FragilityCategory.VAGUE_TERMINOLOGY,
                    excerpt=self._excerpt(sentence, match),
                    sentence_index=idx,
                    severity=0.30,
                    rationale=(
                        "Terminología persuasiva/ambigua sin definición "
                        "operacional ni métrica verificable asociada."
                    ),
                ))
        return points

    def _detect_single_point_of_failure(self, sentences: Sequence[str]) -> List[FragilePoint]:
        points: List[FragilePoint] = []
        for idx, sentence in enumerate(sentences):
            match = self._SINGLE_POINT_PATTERN.search(sentence)
            if match is None:
                continue
            points.append(FragilePoint(
                category=FragilityCategory.SINGLE_POINT_OF_FAILURE,
                excerpt=self._excerpt(sentence, match),
                sentence_index=idx,
                severity=0.60,
                rationale=(
                    "Se descarta la existencia de alternativas o vías "
                    "redundantes, concentrando el riesgo argumental en un "
                    "único punto de fallo."
                ),
            ))
        return points

    def _detect_missing_counterevidence(self, full_text: str) -> List[FragilePoint]:
        if self._LIMITATION_PATTERN.search(full_text):
            return []
        return [FragilePoint(
            category=FragilityCategory.MISSING_COUNTEREVIDENCE,
            excerpt=self._excerpt_prefix(full_text),
            sentence_index=-1,
            severity=0.50,
            rationale=(
                "La premisa no reconoce limitaciones, riesgos, salvedades "
                "ni contraejemplos propios, lo cual sugiere ausencia de "
                "autocrítica o revisión adversarial previa."
            ),
        )]

    def _compute_robustness_score(self, fragile_points: Sequence[FragilePoint]) -> float:
        if not fragile_points:
            return 100.0

        severity_by_category: Dict[FragilityCategory, float] = {}
        for fp in fragile_points:
            severity_by_category[fp.category] = (
                severity_by_category.get(fp.category, 0.0) + fp.severity
            )

        deduction = 0.0
        for category, severity_sum in severity_by_category.items():
            weight = self._CATEGORY_WEIGHT.get(category, 10.0)
            saturation_ratio = min(severity_sum / 1.5, 1.0)
            deduction += weight * saturation_ratio

        score = max(0.0, 100.0 - deduction)
        return round(score, 1)

    def _determine_verdict(self, score: float) -> FuzzingVerdict:
        if score >= self.ROBUST_THRESHOLD:
            return FuzzingVerdict.ROBUST
        if score >= self.FRAGILE_THRESHOLD:
            return FuzzingVerdict.FRAGILE
        return FuzzingVerdict.CRITICALLY_FRAGILE

    def _build_counterarguments(self, fragile_points: Sequence[FragilePoint]) -> List[CounterArgument]:
        counterarguments: List[CounterArgument] = []
        seen: set[Tuple[FragilityCategory, str]] = set()

        for fp in fragile_points:
            key = (fp.category, fp.excerpt)
            if key in seen:
                continue
            seen.add(key)

            template = self._COUNTER_TEMPLATES.get(fp.category)
            if template is None:
                continue

            if fp.severity >= 0.6:
                strength = CounterArgumentStrength.HIGH
            elif fp.severity >= 0.4:
                strength = CounterArgumentStrength.MEDIUM
            else:
                strength = CounterArgumentStrength.LOW

            counterarguments.append(CounterArgument(
                target_category=fp.category,
                target_excerpt=fp.excerpt,
                argument=template.format(excerpt=fp.excerpt),
                strength=strength,
            ))
        return counterarguments

    def _build_search_requirements(
        self, fragile_points: Sequence[FragilePoint], verdict: FuzzingVerdict
    ) -> List[SearchRequirement]:
        requirements: List[SearchRequirement] = []
        seen_queries: set[str] = set()

        for fp in fragile_points:
            query: Optional[str] = None
            justification: str = ""

            if fp.category == FragilityCategory.CAUSAL_LEAP:
                query = (
                    "Casos documentados o estudios que refuten/cuestionen "
                    f"la relación causal implícita en: \"{fp.excerpt}\""
                )
                justification = (
                    "Salto causal sin mecanismo intermedio: se requiere "
                    "evidencia externa antes de aceptar la inferencia."
                )
            elif fp.category in (
                FragilityCategory.UNBOUNDED_QUANTIFIER,
                FragilityCategory.UNFALSIFIABLE_CLAIM,
            ):
                query = (
                    "Contraejemplos o excepciones documentadas a la "
                    f"afirmación absoluta: \"{fp.excerpt}\""
                )
                justification = (
                    "Los cuantificadores absolutos son estadísticamente "
                    "improbables sin excepciones; deben buscarse casos límite."
                )
            elif fp.category == FragilityCategory.SINGLE_POINT_OF_FAILURE:
                query = (
                    "Alternativas o enfoques equivalentes no considerados "
                    f"frente a: \"{fp.excerpt}\""
                )
                justification = (
                    "Descartar alternativas sin evidencia exhaustiva es un "
                    "riesgo argumental de punto único de fallo."
                )
            elif fp.category == FragilityCategory.MISSING_COUNTEREVIDENCE:
                query = (
                    "Críticas, limitaciones o fallos históricos documentados "
                    "de premisas estructuralmente análogas a la propuesta."
                )
                justification = (
                    "No se reconoce ninguna limitación propia; se requiere "
                    "búsqueda deliberada de evidencia adversarial externa."
                )
            else:
                continue

            if query is None or query in seen_queries:
                continue
            seen_queries.add(query)

            priority = 1 if fp.severity >= 0.6 else 2 if fp.severity >= 0.4 else 3
            requirements.append(SearchRequirement(
                query=query, justification=justification, priority=priority
            ))

        if verdict == FuzzingVerdict.CRITICALLY_FRAGILE and not any(
            r.priority == 1 for r in requirements
        ):
            requirements.append(SearchRequirement(
                query=(
                    "Precedentes históricos o casos de estudio donde una "
                    "tesis estructuralmente similar haya fallado."
                ),
                justification=(
                    "El puntaje de robustez es crítico; ninguna afirmación en "
                    "este estado debe difundirse sin validación externa "
                    "exhaustiva."
                ),
                priority=1,
            ))

        requirements.sort(key=lambda r: r.priority)
        return requirements


if __name__ == "__main__":
    fuzzer = AdversarialFuzzer()

    print("=" * 70)
    print("PRUEBA 1: Premisa relativamente robusta (con salvedades y mecanismo)")
    print("=" * 70)
    p1 = (
        "En la mayoría de los despliegues productivos que hemos observado, "
        "adoptar despliegues canary reduce el riesgo de incidentes graves, "
        "porque limita el radio de impacto a un subconjunto pequeño de "
        "tráfico antes de la promoción total. Esto no aplica en sistemas "
        "sin capacidad de rollback automático, una limitación conocida "
        "de este enfoque."
    )
    print(fuzzer.audit(p1).report())

    print("\n" + "=" * 70)
    print("PRUEBA 2: Cuantificadores absolutos sin acotación (no falsificable)")
    print("=" * 70)
    p2 = (
        "Este framework siempre garantiza cero downtime en cualquier "
        "escenario de producción, sin excepción alguna."
    )
    print(fuzzer.audit(p2).report())

    print("\n" + "=" * 70)
    print("PRUEBA 3: Salto causal abrupto sin mecanismo intermedio")
    print("=" * 70)
    p3 = (
        "Adoptamos microservicios en el equipo. Por lo tanto, la velocidad "
        "de entrega se triplicará el próximo trimestre."
    )
    print(fuzzer.audit(p3).report())

    print("\n" + "=" * 70)
    print("PRUEBA 4: Terminología vacía / pitch de startup")
    print("=" * 70)
    p4 = (
        "Nuestra plataforma es una solución disruptiva, escalable y "
        "sinérgica que revoluciona por completo el paradigma del mercado."
    )
    print(fuzzer.audit(p4).report())

    print("\n" + "=" * 70)
    print("PRUEBA 5: Punto único de fallo argumental + cuantificador absoluto")
    print("=" * 70)
    p5 = (
        "La única forma de escalar esta arquitectura es migrar todo a "
        "Kubernetes; no hay ninguna alternativa viable en ningún escenario."
    )
    print(fuzzer.audit(p5).report())

    print("\n" + "=" * 70)
    print("PRUEBA 6: Premisa vacía (caso degenerado)")
    print("=" * 70)
    print(fuzzer.audit("   ").report())

    print("\n" + "=" * 70)
    print("TODAS LAS PRUEBAS EJECUTADAS CORRECTAMENTE")
    print("=" * 70)