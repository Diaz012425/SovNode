"""
DynamicToolEngine — Motor de Síntesis y Ejecución Dinámica de Herramientas.

Ejecuta scripts de Python generados en caliente por el LLM. Como ese
código NO es de confianza por definición, el aislamiento se construye por
DEFENSA EN PROFUNDIDAD, en tres capas independientes:

  Capa 1 — Validación AST por LISTA BLANCA (`SecurityASTVisitor`):
      solo se permiten construcciones explícitamente autorizadas. El
      motor anterior usaba listas NEGRAS (`UNSAFE_MODULES`/`UNSAFE_CALLS`),
      un enfoque históricamente frágil: bastaba no nombrar ninguno de los
      términos vetados para pasar limpio.

  Capa 2 — Bloqueo TOTAL de atributos y nombres dunder:
      es lo que corta de raíz la familia de evasiones por introspección
      (`().__class__.__base__.__subclasses__()`, `__globals__`,
      `__code__`, `__mro__`, `__builtins__`...). Sin esta capa, un script
      puede reconstruir los builtins reales y de ahí abrir archivos o
      importar `os` sin escribir jamás una palabra de la lista negra.

  Capa 3 — Ejecución en SUBPROCESO aislado, con timeout duro y límite de
      memoria: aunque las capas 1-2 fueran evadidas, el daño queda
      acotado a un proceso desechable que se mata solo, en vez de correr
      dentro del proceso de la aplicación.

VULNERABILIDAD REAL QUE MOTIVÓ ESTA REESCRITURA (verificada, no teórica):
el validador anterior aprobaba y ejecutaba con éxito este script, que no
menciona ni un solo término de sus listas negras — recuperaba los 160
builtins reales, leía archivos arbitrarios del disco (incluido
`license_manager.py`) e importaba `os`:

    bi = [c for c in ().__class__.__base__.__subclasses__()
          if c.__name__ == "catch_warnings"][0]()._module.__builtins__
    os = bi["__import__"]("os")

Además, `exec()` corría EN PROCESO y sin cota temporal: un `while True:
pass` congelaba el hilo llamador de forma indefinida.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("SovNode.DynamicToolEngine")


# =====================================================================
# CAPA 1 - LISTA BLANCA DE MÓDULOS
# =====================================================================
# Solo utilidades de cálculo puro: nada de I/O, red, procesos ni
# reflexión. Un script de herramienta dinámica resuelve problemas
# deterministas, así que no necesita más que esto. Cualquier import
# fuera de esta lista se rechaza - a diferencia del enfoque anterior,
# donde `import os` pasaba simplemente porque "os" no figuraba en la
# lista negra.
ALLOWED_MODULES: frozenset[str] = frozenset({
    "math", "statistics", "random", "json", "re", "datetime", "decimal",
    "fractions", "itertools", "functools", "collections", "string",
    "textwrap", "heapq", "bisect", "copy", "enum", "unicodedata",
    "calendar", "operator", "array", "numbers", "typing",
})

# Builtins expuestos al script. Nótese la ausencia deliberada de `open`,
# `eval`, `exec`, `compile`, `__import__`, `globals`, `locals`, `vars`,
# `getattr`, `setattr`, `delattr`, `input`, `breakpoint` y `help`: todos
# son primitivas de escape o de I/O.
ALLOWED_BUILTINS: frozenset[str] = frozenset({
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
    "callable", "chr", "complex", "dict", "divmod", "enumerate", "filter",
    "float", "format", "frozenset", "hash", "hex", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "oct", "ord", "pow", "print", "range", "repr", "reversed", "round",
    "set", "slice", "sorted", "str", "sum", "tuple", "zip",
    "True", "False", "None",
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "ZeroDivisionError", "ArithmeticError", "StopIteration",
    "OverflowError", "RuntimeError", "NotImplementedError",
})

# Nombres que jamás deben aparecer, ni como variable ni como atributo.
# Se comprueban ADEMÁS del bloqueo dunder genérico de la Capa 2.
BLOCKED_NAMES: frozenset[str] = frozenset({
    "eval", "exec", "compile", "open", "input", "breakpoint", "help",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "memoryview", "exit", "quit", "license", "credits", "copyright",
})


class SecurityViolation(Exception):
    """Marca un rechazo de la política de seguridad durante el análisis AST."""


class SecurityASTVisitor(ast.NodeVisitor):
    """
    Valida el AST contra una LISTA BLANCA de construcciones permitidas.

    Invierte por completo el criterio del visitante anterior: en vez de
    enumerar lo prohibido (imposible de mantener exhaustivo en Python),
    enumera lo permitido y rechaza todo lo demás por defecto. Un
    constructo nuevo o inesperado falla CERRADO, no abierto.
    """

    # Nodos permitidos: expresiones, literales, operadores, control de
    # flujo y definiciones. Todo lo que no esté aquí se rechaza.
    _ALLOWED_NODES: Tuple[type, ...] = (
        ast.Module, ast.Expr, ast.Constant, ast.Name, ast.Load, ast.Store,
        ast.Del, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.NamedExpr,
        ast.Tuple, ast.List, ast.Dict, ast.Set, ast.Starred, ast.Slice,
        ast.Subscript, ast.Attribute, ast.Call, ast.keyword,
        ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
        ast.Pow, ast.LShift, ast.RShift, ast.BitOr, ast.BitXor,
        ast.BitAnd, ast.MatMult, ast.UAdd, ast.USub, ast.Not, ast.Invert,
        ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt,
        ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn,
        ast.If, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass,
        ast.Return, ast.FunctionDef, ast.Lambda, ast.arguments, ast.arg,
        ast.ClassDef, ast.Try, ast.ExceptHandler, ast.Raise, ast.Assert,
        ast.With, ast.withitem,
        ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
        ast.comprehension, ast.JoinedStr, ast.FormattedValue,
        ast.Import, ast.ImportFrom, ast.alias,
    )

    def __init__(self) -> None:
        self.violations: List[str] = []

    # -- Capa 2: bloqueo dunder ---------------------------------------
    @staticmethod
    def _is_dunder(name: str) -> bool:
        """
        True para cualquier identificador que empiece por doble guion
        bajo. Se comprueba solo el PREFIJO (no `__x__` estricto) porque
        los atributos internos de CPython que habilitan las cadenas de
        escape (`__class__`, `__base__`, `__subclasses__`, `__globals__`,
        `__builtins__`, `__mro__`, `__code__`, `__reduce__`...) comparten
        ese prefijo, y no hay ningún uso legítimo de ellos en un script
        de cálculo generado automáticamente.
        """
        return name.startswith("__")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._is_dunder(node.attr):
            self.violations.append(
                f"Acceso a atributo interno prohibido: '.{node.attr}' "
                f"(vía de escape por introspección)"
            )
        elif node.attr in BLOCKED_NAMES:
            self.violations.append(f"Acceso a atributo restringido: '.{node.attr}'")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if self._is_dunder(node.id):
            self.violations.append(f"Uso de nombre interno prohibido: '{node.id}'")
        elif node.id in BLOCKED_NAMES:
            self.violations.append(f"Uso de nombre restringido: '{node.id}'")
        self.generic_visit(node)

    # -- Capa 1: imports por lista blanca ------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            base = alias.name.split(".")[0]
            if base not in ALLOWED_MODULES:
                self.violations.append(
                    f"Importación no autorizada: '{alias.name}' "
                    f"(solo se permiten módulos de cálculo puro)"
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = (node.module or "").split(".")[0]
        if base not in ALLOWED_MODULES:
            self.violations.append(
                f"Importación no autorizada desde: '{node.module}'"
            )
        self.generic_visit(node)

    # -- Rechazo por defecto -------------------------------------------
    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, self._ALLOWED_NODES):
            self.violations.append(
                f"Construcción no permitida: '{type(node).__name__}'"
            )
            return  # no se desciende en un nodo ya rechazado
        super().generic_visit(node)


def validate_code_security(code: str) -> Tuple[bool, str]:
    """
    Valida `code` contra la política de listas blancas (Capas 1 y 2).

    Función de módulo (no método) para que otros sandboxes del proyecto
    —en particular `cas_sandbox.ExecutionSandbox`, que ejecuta los
    bloques <thought_code> del modelo— puedan aplicar EXACTAMENTE la
    misma política sin duplicarla y sin que ambas copias diverjan con el
    tiempo (mismo criterio ya usado para `extract_thought_code_blocks`
    en tools.py).
    """
    if not code or not code.strip():
        return False, "El script está vacío."

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"SyntaxError en línea {exc.lineno}: {exc.msg}"

    visitor = SecurityASTVisitor()
    visitor.visit(tree)

    if visitor.violations:
        # Se deduplican conservando orden: un mismo patrón repetido en
        # 20 líneas no debe producir un mensaje de 20 renglones.
        unique = list(dict.fromkeys(visitor.violations))
        return False, "Violación de seguridad AST: " + "; ".join(unique[:5])

    return True, "AST verificado correctamente."


# =====================================================================
# CAPA 3 - AISLAMIENTO EN SUBPROCESO
# =====================================================================
# Programa que corre DENTRO del subproceso desechable. Aplica sus
# propios límites de recursos donde la plataforma lo permite, ejecuta el
# script con builtins recortados y devuelve el resultado al padre como
# un sobre JSON por stdout. La salida del propio script se captura
# aparte para que no se mezcle con ese sobre.
_CHILD_BOOTSTRAP = r'''
import ast, builtins, io, json, sys

payload = json.loads(sys.stdin.read())
code = payload["code"]
allowed = set(payload["allowed_builtins"])
allowed_modules = set(payload["allowed_modules"])
mem_bytes = payload["max_memory_bytes"]
cpu_secs = payload["max_cpu_seconds"]

# Límites duros de recursos en POSIX. En Windows no existe `resource`:
# el límite de memoria lo aplica el proceso PADRE con un Job Object, y
# el de tiempo, el timeout del propio subprocess (ver _run_in_subprocess).
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_secs, cpu_secs))
    resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
except Exception:
    pass

safe_builtins = {n: getattr(builtins, n) for n in allowed if hasattr(builtins, n)}

# `__build_class__` es obligatorio para que funcione cualquier `class`:
# no lo escribe el script (el AST le prohibe nombres dunder), lo invoca
# CPython internamente al compilar una definicion de clase.
safe_builtins["__build_class__"] = builtins.__build_class__


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    """
    `__import__` restringido a la lista blanca. Es la contraparte en
    TIEMPO DE EJECUCION del chequeo estatico del AST: sin el, ningun
    `import` permitido (math, json, re...) llegaria a funcionar, porque
    unos builtins recortados no traen `__import__`. El script no puede
    invocarlo directamente (el AST bloquea el nombre `__import__` por
    dunder); solo lo alcanza la sentencia `import`, ya validada.
    """
    base = (name or "").split(".")[0]
    if base not in allowed_modules:
        raise ImportError("Importacion no autorizada: %r" % name)
    return _real_import(name, globals, locals, fromlist, level)


_real_import = builtins.__import__
safe_builtins["__import__"] = _guarded_import

# UN SOLO espacio de nombres para globals y locals. Con dos dicts
# distintos, `exec` mete las definiciones en `locals` mientras que el
# cuerpo de una funcion resuelve nombres contra `globals`: una funcion
# recursiva no se encuentra a si misma ("NameError: name 'fib' is not
# defined") y las clases fallan al resolver su propio ambito.
ns = {"__builtins__": safe_builtins, "__name__": "__sovnode_tool__"}
ns.update(payload.get("context_vars") or {})

buf = io.StringIO()
real_stdout = sys.stdout
sys.stdout = buf
ok, err = True, ""
try:
    exec(compile(code, "<herramienta_dinamica>", "exec"), ns, ns)
except BaseException as exc:
    ok, err = False, "%s: %s" % (type(exc).__name__, exc)
finally:
    sys.stdout = real_stdout

result_repr = None
if "result" in ns:
    try:
        result_repr = repr(ns["result"])[:2000]
    except Exception:
        result_repr = "<no representable>"

real_stdout.write(json.dumps({
    "ok": ok, "error": err,
    "stdout": buf.getvalue()[:8000],
    "result": result_repr,
}))
'''


def _resolve_python_executable() -> Optional[str]:
    """
    Ruta a un intérprete de Python REAL con el que lanzar el subproceso.

    En un build congelado (PyInstaller/Nuitka — este proyecto tiene
    SovNode.spec y sovnode_qt.build/), `sys.executable` apunta al .exe de
    la propia aplicación, NO a python: lanzarlo relanzaría la GUI entera
    en vez de ejecutar el script. Por eso se prefiere `sys._base_executable`
    y, si tampoco sirve, se busca un python del sistema.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable

    base = getattr(sys, "_base_executable", None)
    if base and os.path.isfile(base) and "python" in os.path.basename(base).lower():
        return base

    for candidate in ("python", "python3"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _apply_windows_memory_limit(handle: int, max_memory_bytes: int) -> Optional[Any]:
    """
    Confina el subproceso en un Job Object de Windows con tope de memoria
    y `KILL_ON_JOB_CLOSE`, que es el mecanismo nativo equivalente a
    `RLIMIT_AS` de POSIX (Windows no expone `resource`).

    `KILL_ON_JOB_CLOSE` es la parte importante para el caso "bucle
    infinito": si el proceso padre muere sin poder limpiar, el sistema
    operativo mata igualmente al hijo al cerrarse el último handle del
    job, en vez de dejar un proceso huérfano quemando CPU.

    Devuelve el handle del job (hay que mantenerlo vivo mientras corra el
    hijo) o None si no se pudo aplicar — en ese caso queda vigente el
    timeout, que es la protección principal.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
        JobObjectExtendedLimitInformation = 9

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
        info.BasicLimitInformation.ActiveProcessLimit = 1  # no puede forkear
        info.ProcessMemoryLimit = max_memory_bytes

        if not kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None

        if not kernel32.AssignProcessToJobObject(job, int(handle)):
            kernel32.CloseHandle(job)
            return None

        return job
    except Exception as exc:
        logger.debug("No se pudo aplicar el Job Object de Windows: %s", exc)
        return None


class DynamicToolEngine:
    """Motor de síntesis de herramientas dinámicas con aislamiento real de ejecución."""

    #: Cota temporal dura. Un `while True: pass` muere aquí en vez de
    #: congelar al hilo llamador, que era el comportamiento anterior.
    DEFAULT_TIMEOUT_SECONDS: float = 10.0
    #: Tope de memoria del subproceso (RLIMIT_AS en POSIX / Job Object en
    #: Windows). Corta en seco un `bytearray(10**9)`.
    MAX_MEMORY_BYTES: int = 256 * 1024 * 1024
    MAX_CPU_SECONDS: int = 10
    MAX_OUTPUT_CHARS: int = 8000

    def __init__(self, orchestrator: Any = None) -> None:
        self.orchestrator = orchestrator

    def validate_ast_security(self, code: str) -> Tuple[bool, str]:
        """Auditoría de seguridad del código (Capas 1 y 2). Interfaz histórica conservada."""
        return validate_code_security(code)

    def execute_sandboxed(
        self,
        code: str,
        context_vars: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """
        Valida y ejecuta el script aislado. Devuelve `(éxito, salida)` —
        misma firma y contrato que la versión anterior, para no tocar a
        `Orchestrator.synthesize_and_run_dynamic_tool()`.
        """
        is_safe, message = self.validate_ast_security(code)
        if not is_safe:
            logger.warning("Script rechazado por la política de seguridad: %s", message)
            return False, message

        return self._run_in_subprocess(code, context_vars)

    def _run_in_subprocess(
        self,
        code: str,
        context_vars: Optional[Dict[str, Any]],
    ) -> Tuple[bool, str]:
        python_exe = _resolve_python_executable()
        if not python_exe:
            # Build congelado sin intérprete disponible: se degrada de
            # forma explícita en vez de caer en silencio a una ejecución
            # en proceso (que es justo lo que esta reescritura elimina).
            return False, (
                "No hay un intérprete de Python disponible para aislar la ejecución; "
                "la herramienta dinámica queda deshabilitada en este build."
            )

        # Solo se propagan variables serializables: el canal con el hijo
        # es JSON, no pickle (pickle sobre datos influidos por el modelo
        # sería, en sí mismo, una primitiva de ejecución de código).
        safe_context: Dict[str, Any] = {}
        for key, value in (context_vars or {}).items():
            try:
                json.dumps(value)
                safe_context[key] = value
            except (TypeError, ValueError):
                logger.debug("Variable de contexto '%s' omitida (no serializable).", key)

        payload = json.dumps({
            "code": code,
            "allowed_builtins": sorted(ALLOWED_BUILTINS),
            "allowed_modules": sorted(ALLOWED_MODULES),
            "max_memory_bytes": self.MAX_MEMORY_BYTES,
            "max_cpu_seconds": self.MAX_CPU_SECONDS,
            "context_vars": safe_context,
        })

        # Entorno mínimo: sin variables heredadas del proceso padre, que
        # podrían llevar tokens/rutas sensibles al script no confiable.
        child_env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }

        creationflags = 0
        if sys.platform == "win32":
            # CREATE_NO_WINDOW evita que parpadee una consola sobre la
            # GUI; CREATE_SUSPENDED no se usa porque se asigna el Job
            # Object inmediatamente después de arrancar.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        job_handle = None
        process = None
        workdir = tempfile.mkdtemp(prefix="sovnode_tool_")
        try:
            # `-I` (aislado: ignora PYTHON*, no añade cwd al path), `-S`
            # (sin site-packages: el script no alcanza dependencias del
            # proyecto) y `-B` (sin .pyc).
            process = subprocess.Popen(
                [python_exe, "-I", "-S", "-B", "-c", _CHILD_BOOTSTRAP],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
                cwd=workdir,
                creationflags=creationflags,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            if sys.platform == "win32":
                job_handle = _apply_windows_memory_limit(
                    process._handle, self.MAX_MEMORY_BYTES  # type: ignore[attr-defined]
                )

            try:
                stdout, stderr = process.communicate(
                    payload, timeout=self.DEFAULT_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                return False, (
                    f"Timeout: el script superó el límite de "
                    f"{self.DEFAULT_TIMEOUT_SECONDS:.0f}s y fue terminado."
                )

            if process.returncode != 0 and not stdout.strip():
                # Muerte abrupta sin sobre JSON: típicamente el Job
                # Object o RLIMIT_AS cortando por memoria, o un fallo del
                # intérprete.
                detail = (stderr or "").strip()[:200] or f"código de salida {process.returncode}"
                return False, f"El script fue terminado por el aislamiento ({detail})."

            return self._parse_child_output(stdout, stderr)
        except Exception as exc:
            logger.error("Fallo lanzando el sandbox de herramienta dinámica: %s", exc)
            return False, f"No se pudo ejecutar el script aislado: {exc}"
        finally:
            if process is not None and process.poll() is None:
                with _suppress():
                    process.kill()
            if job_handle is not None:
                with _suppress():
                    import ctypes
                    ctypes.WinDLL("kernel32").CloseHandle(job_handle)
            with _suppress():
                shutil.rmtree(workdir, ignore_errors=True)

    def _parse_child_output(self, stdout: str, stderr: str) -> Tuple[bool, str]:
        try:
            envelope = json.loads(stdout.strip() or "{}")
        except json.JSONDecodeError:
            detail = (stderr or stdout or "").strip()[:200]
            return False, f"El sandbox devolvió una salida ilegible: {detail}"

        if not envelope.get("ok", False):
            return False, f"RuntimeError en Sandbox: {envelope.get('error', 'desconocido')}"

        output = str(envelope.get("stdout", "")).strip()
        if envelope.get("result") is not None:
            output += f"\n[Resultado Variable 'result']: {envelope['result']}"

        output = output.strip()
        if len(output) > self.MAX_OUTPUT_CHARS:
            output = output[: self.MAX_OUTPUT_CHARS] + "\n...[salida truncada]"

        return True, output or "Script ejecutado con éxito (sin salida por consola)."


class _suppress:
    """`contextlib.suppress(Exception)` mínimo, para no importar contextlib solo por esto."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info: Any) -> bool:
        return True
