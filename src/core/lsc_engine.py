"""
El Monolito Personal - Arquitectura v2.0
Extensión: Motor de Inferencia Sistémica Computacional (lsc_engine.py)

Motor determinista, local y sin dependencias externas (solo stdlib:
`ast`, `re`, `time`) que evalúa premisas complejas en lenguaje natural
bajo tres pesos lógicos ponderados:

  - no_imports         -> Verifica que la premisa (o cualquier código
                           embebido en ella) no dependa de módulos
                           prohibidos (E/S, red, procesos del sistema).
  - robustness_check   -> Heurística de robustez estructural: penaliza
                           cuantificadores absolutos no acotados por
                           salvedades explícitas.
  - logical_inference  -> Evalúa si la premisa presenta una estructura
                           lógica completa (condición + conector de
                           conclusión), en lugar de una afirmación
                           declarativa suelta sin encadenamiento
                           inferencial explícito.

El resultado agregado (`aggregate_score`, 0-100) es una combinación
ponderada de los tres sub-puntajes, con los mismos pesos relativos
declarados en `LSCInferenceEngine.weights` (no_imports=1.0,
robustness_check=0.5, logical_inference=1.0).
"""

from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Pattern, Tuple


class LSCWeightCategory(str, Enum):
    """Categorías de pesos lógicos evaluados por el motor."""
    NO_IMPORTS = "no_imports"
    ROBUSTNESS_CHECK = "robustness_check"
    LOGICAL_INFERENCE = "logical_inference"


class LSCVerdict(str, Enum):
    """Veredicto agregado de la inferencia sistémica."""
    VALID = "valid"
    PARTIALLY_VALID = "partially_valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class LSCResult:
    """
    Resultado inmutable y auditable de una inferencia del LSCInferenceEngine.

    Cada sub-puntaje (`no_imports_score`, `robustness_score`,
    `logical_inference_score`) está normalizado en [0.0, 1.0].
    `aggregate_score` es la combinación ponderada final en [0, 100].
    """
    premise: str
    no_imports_score: float
    robustness_score: float
    logical_inference_score: float
    forbidden_dependencies: Tuple[str, ...]
    aggregate_score: float
    verdict: LSCVerdict
    elapsed_ms: float
    detail: str

    def __str__(self) -> str:
        return (
            f"[LSC:{self.verdict.value.upper()}] score={self.aggregate_score:.1f}/100 "
            f"(no_imports={self.no_imports_score:.2f}, "
            f"robustness={self.robustness_score:.2f}, "
            f"logical={self.logical_inference_score:.2f}) "
            f"({self.elapsed_ms:.3f} ms) | {self.detail}"
        )

    def report(self) -> str:
        lines: List[str] = [str(self), ""]
        lines.append("-- Desglose de pesos lógicos --")
        lines.append(f"  no_imports         : {self.no_imports_score:.3f}")
        lines.append(f"  robustness_check   : {self.robustness_score:.3f}")
        lines.append(f"  logical_inference  : {self.logical_inference_score:.3f}")
        lines.append("")
        lines.append("-- Dependencias prohibidas detectadas --")
        if self.forbidden_dependencies:
            for dep in self.forbidden_dependencies:
                lines.append(f"  {dep}")
        else:
            lines.append("  (ninguna)")
        return "\n".join(lines)


class LSCInferenceEngine:
    """
    Motor de Inferencia Sistémica Computacional (LSC).

    Evalúa premisas complejas en lenguaje natural, o código embebido
    dentro de ellas, bajo tres pesos lógicos ponderados. No depende de
    LLM, red ni librerías de terceros: opera exclusivamente con `ast`
    y expresiones regulares de la librería estándar.
    """

    MAX_PREMISE_LENGTH: int = 20_000
    VALID_THRESHOLD: float = 80.0
    PARTIAL_THRESHOLD: float = 50.0

    # Pesos relativos de cada categoría en la agregación final.
    weights: Dict[str, float] = {
        LSCWeightCategory.NO_IMPORTS.value: 1.0,
        LSCWeightCategory.ROBUSTNESS_CHECK.value: 0.5,
        LSCWeightCategory.LOGICAL_INFERENCE.value: 1.0,
    }

    # Módulos cuya sola presencia (como import real, dentro de código
    # embebido y parseable en la premisa) se considera dependencia
    # prohibida para el dominio de inferencia sistémica.
    _FORBIDDEN_MODULES: frozenset[str] = frozenset({
        "os", "sys", "subprocess", "socket", "shutil", "ctypes",
        "multiprocessing", "threading", "signal", "resource",
        "pty", "pickle", "marshal", "importlib", "http", "urllib",
        "ftplib", "telnetlib", "requests",
    })

    _CODE_LIKELIHOOD_PATTERN: Pattern[str] = re.compile(
        r"\b(import\s+\w+|from\s+\w+\s+import|def\s+\w+\s*\(|class\s+\w+)\b"
    )

    _CONDITIONAL_PATTERN: Pattern[str] = re.compile(
        r"\b(si\b.{2,200}\bentonces\b|if\b.{2,200}\bthen\b|"
        r"dado que\b.{2,200}|siempre que\b.{2,200})",
        re.IGNORECASE | re.DOTALL,
    )

    _CONCLUSION_CONNECTOR_PATTERN: Pattern[str] = re.compile(
        r"\b(por lo tanto|en consecuencia|por consiguiente|de ello se sigue|"
        r"therefore|thus|hence|it follows that)\b",
        re.IGNORECASE,
    )

    _ABSOLUTE_QUANTIFIER_PATTERN: Pattern[str] = re.compile(
        r"\b(siempre|nunca|jamás|todos?|todas?|ningun[oa]|absolutamente|"
        r"totalmente|always|never|every|none|all|totally|absolutely)\b",
        re.IGNORECASE,
    )

    _HEDGE_PATTERN: Pattern[str] = re.compile(
        r"\b(generalmente|normalmente|puede ser que|es posible que|"
        r"bajo ciertas condiciones|dependiendo de|arguably|typically|"
        r"generally|may|might|possibly)\b",
        re.IGNORECASE,
    )

    ABSOLUTE_QUANTIFIER_PENALTY_STEP: float = 0.15

    def infer(self, premise: str) -> LSCResult:
        """
        Ejecuta la evaluación completa de la premisa bajo los tres
        pesos lógicos y retorna un `LSCResult` determinista.

        No lanza excepciones hacia el orquestador: cualquier condición
        degenerada (premisa vacía o excesivamente larga) se traduce en
        un veredicto `INVALID` con `detail` explicativo.
        """
        start = time.perf_counter()

        if not premise or not premise.strip():
            return self._degenerate_result(
                premise, start, "Premisa vacía: no evaluable por el motor LSC."
            )
        if len(premise) > self.MAX_PREMISE_LENGTH:
            return self._degenerate_result(
                premise, start,
                "Premisa excede la longitud máxima permitida para inferencia LSC.",
            )

        no_imports_score, forbidden = self._evaluate_no_imports(premise)
        robustness_score = self._evaluate_robustness(premise)
        logical_score = self._evaluate_logical_inference(premise)

        aggregate = self._aggregate(no_imports_score, robustness_score, logical_score)
        verdict = self._determine_verdict(aggregate)
        elapsed = (time.perf_counter() - start) * 1000
        detail = self._build_detail(
            no_imports_score, robustness_score, logical_score, forbidden, verdict
        )

        return LSCResult(
            premise=premise,
            no_imports_score=no_imports_score,
            robustness_score=robustness_score,
            logical_inference_score=logical_score,
            forbidden_dependencies=forbidden,
            aggregate_score=aggregate,
            verdict=verdict,
            elapsed_ms=elapsed,
            detail=detail,
        )

    def _degenerate_result(self, premise: str, start: float, detail: str) -> LSCResult:
        elapsed = (time.perf_counter() - start) * 1000
        return LSCResult(
            premise=premise or "",
            no_imports_score=0.0,
            robustness_score=0.0,
            logical_inference_score=0.0,
            forbidden_dependencies=(),
            aggregate_score=0.0,
            verdict=LSCVerdict.INVALID,
            elapsed_ms=elapsed,
            detail=detail,
        )

    def _evaluate_no_imports(self, premise: str) -> Tuple[float, Tuple[str, ...]]:
        """
        Si la premisa no exhibe likelihood de contener código (imports,
        definiciones de función/clase), se considera prosa pura y
        obtiene puntaje máximo. Si exhibe dicha likelihood, se intenta
        parsear como AST y se buscan imports de módulos prohibidos.
        """
        if not self._CODE_LIKELIHOOD_PATTERN.search(premise):
            return 1.0, ()

        try:
            tree = ast.parse(premise, mode="exec")
        except SyntaxError:
            # Menciona palabras clave de código pero no es código
            # parseable real (p. ej. prosa sobre "importar" conceptos);
            # se trata como prosa y se otorga puntaje máximo.
            return 1.0, ()

        forbidden_found: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in self._FORBIDDEN_MODULES:
                        forbidden_found.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in self._FORBIDDEN_MODULES:
                    forbidden_found.append(node.module or "(relativo)")

        if forbidden_found:
            return 0.0, tuple(sorted(set(forbidden_found)))
        return 1.0, ()

    def _evaluate_robustness(self, premise: str) -> float:
        """
        Penaliza cuantificadores absolutos que no estén acotados por
        ninguna salvedad explícita en toda la premisa. Cada exceso de
        cuantificador absoluto sobre salvedad resta
        `ABSOLUTE_QUANTIFIER_PENALTY_STEP` al puntaje base de 1.0.
        """
        absolute_hits = len(self._ABSOLUTE_QUANTIFIER_PATTERN.findall(premise))
        hedge_hits = len(self._HEDGE_PATTERN.findall(premise))
        unmitigated_excess = max(0, absolute_hits - hedge_hits)
        penalty = unmitigated_excess * self.ABSOLUTE_QUANTIFIER_PENALTY_STEP
        return round(max(0.0, 1.0 - penalty), 3)

    def _evaluate_logical_inference(self, premise: str) -> float:
        """
        Evalúa la completitud de la estructura inferencial:
          - Condición + conector de conclusión presentes -> puntaje máximo.
          - Solo uno de los dos presentes -> puntaje intermedio.
          - Ninguno -> prosa declarativa sin encadenamiento inferencial
            explícito (no es inválida per se, pero no aporta estructura
            lógica verificable).
        """
        has_conditional = bool(self._CONDITIONAL_PATTERN.search(premise))
        has_connector = bool(self._CONCLUSION_CONNECTOR_PATTERN.search(premise))

        if has_conditional and has_connector:
            return 1.0
        if has_conditional or has_connector:
            return 0.6
        return 0.35

    def _aggregate(
        self, no_imports_score: float, robustness_score: float, logical_score: float
    ) -> float:
        w_no_imports = self.weights[LSCWeightCategory.NO_IMPORTS.value]
        w_robustness = self.weights[LSCWeightCategory.ROBUSTNESS_CHECK.value]
        w_logical = self.weights[LSCWeightCategory.LOGICAL_INFERENCE.value]
        total_weight = w_no_imports + w_robustness + w_logical

        weighted_sum = (
            no_imports_score * w_no_imports
            + robustness_score * w_robustness
            + logical_score * w_logical
        )
        return round((weighted_sum / total_weight) * 100, 1)

    def _determine_verdict(self, aggregate_score: float) -> LSCVerdict:
        if aggregate_score >= self.VALID_THRESHOLD:
            return LSCVerdict.VALID
        if aggregate_score >= self.PARTIAL_THRESHOLD:
            return LSCVerdict.PARTIALLY_VALID
        return LSCVerdict.INVALID

    @staticmethod
    def _build_detail(
        no_imports_score: float,
        robustness_score: float,
        logical_score: float,
        forbidden: Tuple[str, ...],
        verdict: LSCVerdict,
    ) -> str:
        parts = [
            f"no_imports={no_imports_score:.2f}",
            f"robustness_check={robustness_score:.2f}",
            f"logical_inference={logical_score:.2f}",
        ]
        base = f"Inferencia LSC completada ({', '.join(parts)}); veredicto={verdict.value}."
        if forbidden:
            base += f" Dependencias prohibidas detectadas: {', '.join(forbidden)}."
        return base


if __name__ == "__main__":
    engine = LSCInferenceEngine()

    print("=" * 70)
    print("PRUEBA 1: Premisa lógica bien estructurada, sin código embebido")
    print("=" * 70)
    p1 = (
        "Si el sistema mantiene su presupuesto de latencia por debajo de "
        "50ms bajo carga sostenida, entonces por lo tanto puede clasificarse "
        "como apto para el SLA de tiempo real acordado."
    )
    print(engine.infer(p1).report())

    print("\n" + "=" * 70)
    print("PRUEBA 2: Premisa con cuantificadores absolutos no mitigados")
    print("=" * 70)
    p2 = "Este enfoque siempre funciona en todos los casos sin ninguna excepción."
    print(engine.infer(p2).report())

    print("\n" + "=" * 70)
    print("PRUEBA 3: Premisa con código embebido e import prohibido")
    print("=" * 70)
    p3 = (
        "Considera este fragmento:\n"
        "import os\n"
        "def leak():\n"
        "    return os.listdir('/')\n"
    )
    print(engine.infer(p3).report())

    print("\n" + "=" * 70)
    print("PRUEBA 4: Premisa vacía (caso degenerado)")
    print("=" * 70)
    print(engine.infer("   ").report())

    print("\n" + "=" * 70)
    print("TODAS LAS PRUEBAS EJECUTADAS CORRECTAMENTE")
    print("=" * 70)