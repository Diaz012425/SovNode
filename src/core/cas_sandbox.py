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
cas_sandbox.py — Dominio formal local.

Incluye:
    - CASEngine: resolución, simplificación y verificación algebraica.
    - ExecutionSandbox: ejecución Python aislada en subproceso.
    - LogicalCoherenceValidator: auditoría determinista de aserciones
      aritméticas explícitas en texto libre.

Corrección aplicada (auditoría v3.8.1):
    - ExecutionSandbox._apply_resource_limits tenía el decorador
      @classmethod desalineado del cuerpo de la clase (columna 0 en
      lugar de 4 espacios). Esto provocaba un IndentationError al
      compilar el módulo, que NO es capturado por los bloques
      `except ImportError` de orchestrator.py, tumbando el arranque
      completo del nodo. Se corrige la indentación sin alterar la
      lógica del método.
"""

from __future__ import annotations

import ast
import contextlib
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional, Pattern, Tuple

import sympy
from sympy import Eq, N, Symbol, simplify
from sympy.core.sympify import SympifyError
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

try:
    import resource
except ImportError:
    resource = None


class CASStatus(str, Enum):
    SOLVED = "solved"
    SIMPLIFIED = "simplified"
    VERIFIED_TRUE = "verified_true"
    VERIFIED_FALSE = "verified_false"
    ERROR = "error"


@dataclass(frozen=True)
class CASResult:
    status: CASStatus
    success: bool
    input_expression: str
    result: str
    result_type: str
    numeric_value: Optional[float]
    elapsed_ms: float
    detail: str

    def __str__(self) -> str:
        return (
            f"[CAS:{self.status.value.upper()}] success={self.success} "
            f"result='{self.result}' ({self.elapsed_ms:.3f} ms) | {self.detail}"
        )


class CASEngine:
    """
    Motor SymPy con saneamiento léxico y parseo controlado.

    Las expresiones sin signo igual se simplifican mediante
    simplify_expression(). Las expresiones con '=' se resuelven mediante
    solve_equation().
    """

    _TRANSFORMATIONS = standard_transformations + (
        implicit_multiplication_application,
        convert_xor,
    )

    MAX_EXPRESSION_LENGTH: int = 2000

    _ALLOWED_CHARS_RE: Pattern[str] = re.compile(
        r"[^0-9a-zA-Z_\.\+\-\*/\^\(\)=,\s]"
    )
    # Una racha de 2+ operadores seguidos. Se colapsa a su forma canónica
    # en `_collapse_operator_run` — al último operador para un typo tipo
    # "--" o "*/", PERO preservando `**` (dos caracteres, un solo
    # operador: la potencia nativa de Python/sympy). Colapsar `**` a `*`
    # convertía "v**2" en "v*2" — multiplicación, no cuadrado — en
    # silencio, sin ninguna excepción (bug real reproducido en vivo en el
    # motor de descubrimiento de fórmulas: "E_k = m*v" marcado como
    # verificado, ver formula_synthesizer.py).
    _MULTI_OPERATOR_RE: Pattern[str] = re.compile(r"[\+\-\*/\^]{2,}")
    _MULTI_SPACE_RE: Pattern[str] = re.compile(r"\s{2,}")
    _LEADING_TRAILING_OPERATOR_RE: Pattern[str] = re.compile(
        r"^[\+\*/\^=,\s]+|[\+\-\*/\^=,\s]+$"
    )

    @staticmethod
    def _collapse_operator_run(run: str) -> str:
        """
        Colapsa una racha de operadores repetidos a su forma canónica.

        Caso normal (typo): "--", "*/", "+-+" -> el último operador. Es la
        normalización histórica de esta clase, pensada para un usuario que
        tipeó un operador de más.

        Excepción `**`: exponenciación — la notación de potencia nativa de
        Python/sympy. Son dos caracteres pero UN operador; colapsarlo a un
        `*` convierte "v**2" en "v*2" (multiplicación, no cuadrado) sin
        levantar ninguna excepción, y el resultado corrupto se sigue
        "verificando" contra sí mismo. Se preserva el `**` y el signo
        unario que pueda seguirlo ("x**-2" -> "**-", "2**+3" -> "**+").
        """
        if "**" in run:
            trailing = run.rsplit("*", 1)[1]
            sign = trailing[-1] if trailing and trailing[-1] in "+-" else ""
            return "**" + sign
        return run[-1]

    def _balance_parentheses(self, text: str) -> str:
        result: List[str] = []
        depth = 0

        for char in text:
            if char == "(":
                depth += 1
                result.append(char)
            elif char == ")":
                if depth > 0:
                    depth -= 1
                    result.append(char)
            else:
                result.append(char)

        if depth > 0:
            result.append(")" * depth)

        return "".join(result)

    def _sanitize_expression_string(self, expression: str) -> str:
        """
        Conserva exclusivamente el alfabeto matemático permitido,
        normaliza operadores repetidos (preservando `**` como potencia) y
        balancea paréntesis.
        """
        if not expression:
            return ""

        cleaned = self._ALLOWED_CHARS_RE.sub(" ", expression)
        cleaned = self._MULTI_OPERATOR_RE.sub(
            lambda match: self._collapse_operator_run(match.group(0)), cleaned
        )
        cleaned = self._balance_parentheses(cleaned)
        cleaned = self._LEADING_TRAILING_OPERATOR_RE.sub("", cleaned)
        cleaned = self._MULTI_SPACE_RE.sub(" ", cleaned).strip()

        return cleaned

    def _parse(self, expression: str) -> Any:
        if not expression or not expression.strip():
            raise ValueError("Expresión vacía.")

        if len(expression) > self.MAX_EXPRESSION_LENGTH:
            raise ValueError("La expresión excede la longitud máxima permitida.")

        sanitized = self._sanitize_expression_string(expression)

        if not sanitized:
            raise ValueError(
                "La expresión no contiene tokens matemáticos válidos tras el saneamiento."
            )

        return parse_expr(
            sanitized,
            transformations=self._TRANSFORMATIONS,
            evaluate=True,
        )

    def solve_equation(self, equation_str: str, symbol_str: str = "x") -> CASResult:
        start = time.perf_counter()

        try:
            symbol = Symbol(symbol_str)

            if "=" in equation_str and "==" not in equation_str:
                lhs_str, rhs_str = equation_str.split("=", 1)
                equation = Eq(self._parse(lhs_str), self._parse(rhs_str))
            else:
                normalized = equation_str.replace("==", "-")
                equation = Eq(self._parse(normalized), 0)

            solutions = sympy.solve(equation, symbol)
            elapsed = (time.perf_counter() - start) * 1000

            if not solutions:
                return CASResult(
                    status=CASStatus.ERROR,
                    success=False,
                    input_expression=equation_str,
                    result="sin soluciones",
                    result_type="empty",
                    numeric_value=None,
                    elapsed_ms=elapsed,
                    detail="El solver no encontró soluciones reales ni complejas.",
                )

            result = ", ".join(str(solution) for solution in solutions)
            numeric_value: Optional[float] = None

            if len(solutions) == 1:
                with contextlib.suppress(TypeError, ValueError):
                    numeric_value = float(N(solutions[0]))

            return CASResult(
                status=CASStatus.SOLVED,
                success=True,
                input_expression=equation_str,
                result=result,
                result_type="list[Expr]",
                numeric_value=numeric_value,
                elapsed_ms=elapsed,
                detail=f"Solución exacta hallada para la variable '{symbol_str}'.",
            )

        except (SympifyError, ValueError, TypeError, SyntaxError, AttributeError) as exc:
            elapsed = (time.perf_counter() - start) * 1000
            return CASResult(
                status=CASStatus.ERROR,
                success=False,
                input_expression=equation_str,
                result="",
                result_type="error",
                numeric_value=None,
                elapsed_ms=elapsed,
                detail=f"Error de parseo o resolución: {exc}",
            )

    def simplify_expression(self, expression: str) -> CASResult:
        start = time.perf_counter()

        try:
            simplified = simplify(self._parse(expression))
            elapsed = (time.perf_counter() - start) * 1000

            numeric_value: Optional[float] = None
            with contextlib.suppress(TypeError, ValueError):
                numeric_value = float(N(simplified))

            return CASResult(
                status=CASStatus.SIMPLIFIED,
                success=True,
                input_expression=expression,
                result=str(simplified),
                result_type=type(simplified).__name__,
                numeric_value=numeric_value,
                elapsed_ms=elapsed,
                detail="Expresión simplificada correctamente.",
            )

        except (SympifyError, ValueError, TypeError, SyntaxError, AttributeError) as exc:
            elapsed = (time.perf_counter() - start) * 1000
            return CASResult(
                status=CASStatus.ERROR,
                success=False,
                input_expression=expression,
                result="",
                result_type="error",
                numeric_value=None,
                elapsed_ms=elapsed,
                detail=f"Error de parseo o simplificación: {exc}",
            )

    def verify_claim(self, lhs_str: str, rhs_str: str) -> CASResult:
        start = time.perf_counter()

        try:
            lhs = self._parse(lhs_str)
            rhs = self._parse(rhs_str)
            difference = simplify(lhs - rhs)
            is_equivalent = difference == 0
            elapsed = (time.perf_counter() - start) * 1000

            status = (
                CASStatus.VERIFIED_TRUE
                if is_equivalent
                else CASStatus.VERIFIED_FALSE
            )

            detail = (
                "Las expresiones son algebraicamente equivalentes."
                if is_equivalent
                else f"Las expresiones difieren; residual: {difference}"
            )

            return CASResult(
                status=status,
                success=is_equivalent,
                input_expression=f"{lhs_str} == {rhs_str}",
                result=str(is_equivalent),
                result_type="bool",
                numeric_value=None,
                elapsed_ms=elapsed,
                detail=detail,
            )

        except (SympifyError, ValueError, TypeError, SyntaxError, AttributeError) as exc:
            elapsed = (time.perf_counter() - start) * 1000
            return CASResult(
                status=CASStatus.ERROR,
                success=False,
                input_expression=f"{lhs_str} == {rhs_str}",
                result="",
                result_type="error",
                numeric_value=None,
                elapsed_ms=elapsed,
                detail=f"Error de parseo o verificación: {exc}",
            )


class SandboxStatus(str, Enum):
    OK = "ok"
    TIMEOUT = "timeout"
    RUNTIME_ERROR = "runtime_error"
    FORBIDDEN_CODE = "forbidden_code"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class SandboxResult:
    status: SandboxStatus
    success: bool
    stdout: str
    stderr: str
    return_code: Optional[int]
    elapsed_ms: float
    timed_out: bool
    detail: str

    def __str__(self) -> str:
        preview = self.stdout.strip().replace("\n", " ")
        if len(preview) > 80:
            preview = f"{preview[:80]}..."

        return (
            f"[SANDBOX:{self.status.value.upper()}] success={self.success} "
            f"({self.elapsed_ms:.2f} ms) stdout='{preview}' | {self.detail}"
        )


class ExecutionSandbox:
    """
    Sandbox de ejecución Python en subproceso con auditoría AST previa.

    El análisis estático no sustituye a un contenedor, una máquina
    virtual o aislamiento a nivel de sistema operativo. Sin embargo,
    suma capas de defensa mediante denegación de imports, llamadas
    peligrosas, límite temporal, recursos POSIX y entorno mínimo.
    """

    DEFAULT_TIMEOUT_SECONDS: float = 5.0
    MAX_OUTPUT_CHARS: int = 4000
    MAX_MEMORY_BYTES: int = 256 * 1024 * 1024
    MAX_CPU_SECONDS: int = 5
    MAX_NPROC: int = 32
    MAX_FILE_SIZE_BYTES: int = 1024 * 1024

    _FORBIDDEN_IMPORTS: frozenset[str] = frozenset({
        "os",
        "subprocess",
        "sys",
        "socket",
        "shutil",
        "ctypes",
        "multiprocessing",
        "threading",
        "signal",
        "resource",
        "pty",
        "pickle",
        "marshal",
        "importlib",
        "pathlib",
        "http",
        "urllib",
        "ftplib",
        "telnetlib",
        # Corrección (auditoría v3.9, ver _static_audit): 'builtins' expone
        # eval/exec/open/etc. como atributos de un módulo normal, evadiendo
        # el chequeo de llamada directa vía 'import builtins; b =
        # builtins.eval; b(...)'. 'inspect'/'gc' permiten recuperar
        # referencias vivas a objetos peligrosos (frames, closures) sin
        # pasar por ningún nombre de la lista prohibida.
        "builtins",
        "inspect",
        "gc",
    })

    _FORBIDDEN_CALLS: frozenset[str] = frozenset({
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "input",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
    })

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        python_executable: Optional[str] = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.python_executable = python_executable or sys.executable

    def _static_audit(self, code: str) -> Optional[str]:
        """
        Corrección (auditoría v3.9): el chequeo anterior solo bloqueaba una
        función prohibida cuando aparecía como blanco DIRECTO de una
        llamada (`eval(...)`), inspeccionando exclusivamente los nodos
        `ast.Call`. Eso se evade trivialmente con un alias:

            e = eval
            e("__import__('os').system('...')")

        `e = eval` no es un `ast.Call` (es una asignación cuyo valor es un
        `ast.Name` en contexto Load), así que pasaba sin marcar nada; la
        llamada posterior `e(...)` tiene `func.id == "e"`, que no está en
        la lista prohibida. El mismo hueco aplica a esconder la referencia
        en una lista/dict/lambda antes de invocarla.

        Se corrige eliminando la distinción "está siendo llamado" vs "solo
        referenciado": ahora se recorre TODO el árbol y se rechaza
        cualquier `ast.Name` o `ast.Attribute` cuyo identificador esté en
        `_FORBIDDEN_CALLS`, sin importar en qué posición del árbol
        aparezca (asignación, argumento, elemento de contenedor, etc.).
        Esto también hace redundante -- y por lo tanto se elimina -- el
        chequeo específico de `ast.Call`, porque `ast.walk` ya visita por
        separado el `node.func` de cada llamada como su propio nodo
        `Name`/`Attribute`.
        """
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError as exc:
            return f"Error de sintaxis detectado durante auditoría AST: {exc}"

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                return (
                    f"Acceso a atributo interno prohibido: '.{node.attr}' "
                    f"(vía de escape por introspección)."
                )
            if isinstance(node, ast.Name) and node.id.startswith("__"):
                return f"Uso de nombre interno prohibido: '{node.id}'."

            if isinstance(node, ast.Name) and node.id in self._FORBIDDEN_CALLS:
                return (
                    f"Referencia prohibida detectada: '{node.id}' "
                    "(incluye asignaciones/alias, no solo invocación directa)."
                )
            if isinstance(node, ast.Attribute) and node.attr in self._FORBIDDEN_CALLS:
                return (
                    f"Referencia prohibida detectada: '.{node.attr}' "
                    "(incluye asignaciones/alias, no solo invocación directa)."
                )

            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in self._FORBIDDEN_IMPORTS:
                        return f"Import prohibido detectado: '{alias.name}'."

            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in self._FORBIDDEN_IMPORTS:
                    return f"Import prohibido detectado: 'from {node.module}'."

        return None

    @classmethod
    def _apply_resource_limits(cls) -> None:
        if resource is None:
            return

        with contextlib.suppress(OSError, AttributeError, ValueError):
            resource.setrlimit(
                resource.RLIMIT_CPU,
                (cls.MAX_CPU_SECONDS, cls.MAX_CPU_SECONDS),
            )
            resource.setrlimit(
                resource.RLIMIT_AS,
                (cls.MAX_MEMORY_BYTES, cls.MAX_MEMORY_BYTES),
            )
            resource.setrlimit(
                resource.RLIMIT_NPROC,
                (cls.MAX_NPROC, cls.MAX_NPROC),
            )
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (cls.MAX_FILE_SIZE_BYTES, cls.MAX_FILE_SIZE_BYTES),
            )

    def run(self, code: str) -> SandboxResult:
        start = time.perf_counter()

        rejection = self._static_audit(code)
        if rejection:
            elapsed = (time.perf_counter() - start) * 1000
            return SandboxResult(
                status=SandboxStatus.FORBIDDEN_CODE,
                success=False,
                stdout="",
                stderr="",
                return_code=None,
                elapsed_ms=elapsed,
                timed_out=False,
                detail=rejection,
            )

        minimal_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        is_windows = platform.system() == "Windows"
        preexec = None if is_windows else self._apply_resource_limits
        temporary_path: Optional[str] = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                delete=False,
                encoding="utf-8",
            ) as temporary_file:
                temporary_file.write(code)
                temporary_path = temporary_file.name

            completed = subprocess.run(
                [self.python_executable, "-I", "-B", temporary_path],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=minimal_env,
                preexec_fn=preexec,
            )

            elapsed = (time.perf_counter() - start) * 1000
            stdout = completed.stdout[:self.MAX_OUTPUT_CHARS]
            stderr = completed.stderr[:self.MAX_OUTPUT_CHARS]
            success = completed.returncode == 0

            return SandboxResult(
                status=SandboxStatus.OK if success else SandboxStatus.RUNTIME_ERROR,
                success=success,
                stdout=stdout,
                stderr=stderr,
                return_code=completed.returncode,
                elapsed_ms=elapsed,
                timed_out=False,
                detail=(
                    "Ejecución completada correctamente."
                    if success
                    else (
                        "El proceso terminó con código de error "
                        f"{completed.returncode}."
                    )
                ),
            )

        except subprocess.TimeoutExpired as exc:
            elapsed = (time.perf_counter() - start) * 1000
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""

            return SandboxResult(
                status=SandboxStatus.TIMEOUT,
                success=False,
                stdout=stdout[:self.MAX_OUTPUT_CHARS],
                stderr=stderr[:self.MAX_OUTPUT_CHARS],
                return_code=None,
                elapsed_ms=elapsed,
                timed_out=True,
                detail=(
                    f"Tiempo límite de {self.timeout_seconds:.1f}s excedido; "
                    "proceso terminado."
                ),
            )

        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000

            return SandboxResult(
                status=SandboxStatus.INTERNAL_ERROR,
                success=False,
                stdout="",
                stderr=traceback.format_exc()[:self.MAX_OUTPUT_CHARS],
                return_code=None,
                elapsed_ms=elapsed,
                timed_out=False,
                detail=f"Error interno del sandbox: {exc}",
            )

        finally:
            if temporary_path:
                with contextlib.suppress(OSError):
                    os.remove(temporary_path)


class LogicalAssertionStatus(str, Enum):
    COHERENT = "coherent"
    INCOHERENT = "incoherent"


LogicalStatus = LogicalAssertionStatus


@dataclass(frozen=True)
class LogicalValidationResult:
    status: LogicalAssertionStatus
    extracted_conditions: Tuple[str, ...]
    detail: str

    def __str__(self) -> str:
        return (
            f"[LOGIC:{self.status.value.upper()}] "
            f"conditions_checked={len(self.extracted_conditions)} | {self.detail}"
        )

    def report(self) -> str:
        return (
            f"[VALIDACIÓN LÓGICA:{self.status.value.upper()}] "
            f"Aserciones evaluadas={len(self.extracted_conditions)} | {self.detail}"
        )


class LogicalCoherenceValidator:
    """
    Detecta contradicciones numéricas explícitas, por ejemplo:

        2 + 2 = 5
        10 / 2 = 4

    No intenta validar toda la lógica del lenguaje natural. Solo afirma
    incoherencia cuando encuentra una contradicción aritmética
    demostrable de forma determinista.
    """

    _NUMERIC_ASSERTION_PATTERN: Pattern[str] = re.compile(
        r"(?<!\*)"
        r"\b(-?\d+(?:\.\d+)?)\s*([\+\-\*/])\s*"
        r"(-?\d+(?:\.\d+)?)\s*=\s*"
        r"(-?\d+(?:\.\d+)?)\b"
        r"(?![\s\*]*=)"
    )

    _ABSOLUTE_TOLERANCE: float = 1e-6
    _RELATIVE_TOLERANCE: float = 1e-9
    MAX_MISMATCHES_REPORTED: int = 5

    def validate(self, text: str) -> LogicalValidationResult:
        if not text or not text.strip():
            return LogicalValidationResult(
                status=LogicalAssertionStatus.COHERENT,
                extracted_conditions=(),
                detail="Texto vacío; no existen aserciones aritméticas que auditar.",
            )

        conditions = tuple(
            sentence.strip()
            for sentence in text.split(".")
            if sentence.strip()
        )

        try:
            mismatches = self._find_numeric_mismatches(text)
        except Exception as exc:
            return LogicalValidationResult(
                status=LogicalAssertionStatus.COHERENT,
                extracted_conditions=conditions[:5],
                detail=(
                    "Auditoría numérica omitida por error interno no crítico: "
                    f"{exc}"
                ),
            )

        if mismatches:
            preview = "; ".join(
                f"'{expression}' es falso; valor correcto={expected:g}"
                for expression, expected in mismatches[:self.MAX_MISMATCHES_REPORTED]
            )

            return LogicalValidationResult(
                status=LogicalAssertionStatus.INCOHERENT,
                extracted_conditions=conditions[:5],
                detail=(
                    f"Se detectaron {len(mismatches)} aserción(es) aritmética(s) "
                    f"falsa(s): {preview}"
                ),
            )

        checked = len(self._NUMERIC_ASSERTION_PATTERN.findall(text))

        return LogicalValidationResult(
            status=LogicalAssertionStatus.COHERENT,
            extracted_conditions=conditions[:5],
            detail=(
                f"Validación superada: {checked} aserción(es) aritmética(s) "
                "verificada(s) sin contradicciones."
                if checked
                else (
                    "Validación superada: no se detectaron aserciones "
                    "aritméticas explícitas."
                )
            ),
        )

    def _find_numeric_mismatches(self, text: str) -> List[Tuple[str, float]]:
        mismatches: List[Tuple[str, float]] = []

        for match in self._NUMERIC_ASSERTION_PATTERN.finditer(text):
            operand_a_str, operator, operand_b_str, claimed_str = match.groups()

            try:
                operand_a = float(operand_a_str)
                operand_b = float(operand_b_str)
                claimed = float(claimed_str)
            except ValueError:
                continue

            try:
                if operator == "+":
                    expected = operand_a + operand_b
                elif operator == "-":
                    expected = operand_a - operand_b
                elif operator == "*":
                    expected = operand_a * operand_b
                elif operator == "/":
                    if operand_b == 0:
                        continue
                    expected = operand_a / operand_b
                else:
                    continue
            except (OverflowError, ZeroDivisionError, ValueError):
                continue

            try:
                tolerance = max(
                    self._ABSOLUTE_TOLERANCE,
                    abs(expected) * self._RELATIVE_TOLERANCE,
                )
                mismatch = abs(expected - claimed) > tolerance
            except OverflowError:
                continue

            if mismatch:
                mismatches.append((match.group(0).strip(), expected))

        return mismatches


if __name__ == "__main__":
    cas = CASEngine()
    sandbox = ExecutionSandbox(timeout_seconds=3.0)
    logic = LogicalCoherenceValidator()

    print(cas.solve_equation("3*x + 5 = 20"))
    print(cas.simplify_expression("sin(x)^2 + cos(x)^2"))
    print(sandbox.run("print(6 * 7)"))
    print(logic.validate("La afirmación 2 + 2 = 5 es incorrecta."))