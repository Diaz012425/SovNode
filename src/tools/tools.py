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
tools.py — Despachador de herramientas locales para SovNode
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import shlex
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple
import time
import functools
import traceback
import logging

logger = logging.getLogger("SovNode.Tools")



_THOUGHT_CODE_RE = re.compile(
    r"<thought_code>\s*(.*?)\s*</thought_code>", re.DOTALL | re.IGNORECASE
)
MAX_THOUGHT_CODE_BLOCKS = 3
MAX_VERIFICATION_OUTPUT_CHARS = 800


def extract_thought_code_blocks(text: str) -> List[str]:
    """Extrae hasta MAX_THOUGHT_CODE_BLOCKS fragmentos <thought_code> de un texto crudo del modelo."""
    if not text:
        return []
    return _THOUGHT_CODE_RE.findall(text)[:MAX_THOUGHT_CODE_BLOCKS]


def format_sandbox_verification(results: List[Tuple[str, Any]]) -> str:
    """
    Formatea los resultados de ejecutar bloques <thought_code> en el
    sandbox como un único bloque de evidencia listo para reinyectarse en
    un prompt de seguimiento. `results` es una lista de (código, resultado)
    donde `resultado` es un SandboxResult de cas_sandbox.ExecutionSandbox
    (duck-typed aquí a propósito, para no obligar a tools.py a importar
    cas_sandbox.py — solo usa los atributos .success/.status/.stdout/.stderr).
    """
    if not results:
        return ""

    lines = ["[VERIFICACIÓN EN TIEMPO REAL DEL SANDBOX]"]
    for i, (code, result) in enumerate(results, 1):
        success = bool(getattr(result, "success", False))
        status = "OK" if success else f"ERROR ({getattr(getattr(result, 'status', None), 'value', 'desconocido')})"
        raw_output = str(getattr(result, "stdout", "") or getattr(result, "stderr", "") or "(sin salida)").strip()
        output = raw_output[:MAX_VERIFICATION_OUTPUT_CHARS]
        lines.append(
            f"[{i}] Código verificado:\n```python\n{code.strip()}\n```\n"
            f"Resultado ({status}):\n{output}\n"
        )
    return "\n".join(lines)

MAX_TOOL_OUTPUT_CHARS = 4000

# BLINDAJE (pedido explícito 2026-09-03: "que los pueda leer completos y los
# pueda analizar"): `read_file` es el único caso donde el volcado grande es
# DESEADO — el usuario quiere que el modelo vea el archivo entero para
# analizarlo, no un recorte de 4000 chars (~1000 tokens, apenas ~120 líneas
# de código). `run_cmd` sigue en MAX_TOOL_OUTPUT_CHARS porque ahí el volcado
# grande casi siempre es ruido (un build log, un `pip install` verboso) que
# solo satura el contexto. 12000 chars (~3000-3400 tokens) entra de sobra en
# el num_ctx pinneado de 8192 junto con el prompt de seguimiento, que es
# corto (petición + resultado + instrucción de cierre, sin historial ni
# andamiaje de razonamiento). El cap espejo vive en
# Orchestrator.MAX_READ_FILE_RESULT_CHARS_IN_PROMPT.
MAX_READ_FILE_OUTPUT_CHARS = 12000

_CREATE_NO_WINDOW = 0x08000000

# BLINDAJE (bug real, MEDIDO en captura de pantalla 2026-09-06): "Hazme un
# juego de snake en python." -> el esqueleto local escribió `snake.py` y el
# propio modelo de código, dentro del bucle normal de tool-calling (ver
# Orchestrator.run_turn, sección "while tool_call"), decidió por su cuenta
# correr `python snake.py` para probarlo antes de responder — secuencia
# que el usuario reportó que le encantó (verificar el código antes de
# entregarlo). El problema no es esa idea, sino la ejecución:
# `run_cmd_safely` nunca recibía ningún `timeout_sec` real — ni
# TOOLS_SCHEMA (más abajo) expone ese parámetro al modelo, ni
# `LocalToolDispatcher._register_default_tools` lo reenviaba
# (`lambda command: self.sandbox.run_cmd_safely(command)`, sin
# `timeout_sec`) — así que TODO comando corría con
# `subprocess.run(..., timeout=None)`: sin ninguna red de seguridad. Para
# un comando de una sola pasada (un build, un `pip list`) eso rara vez se
# nota; para lanzar una app GRÁFICA con su propio bucle de eventos
# (pygame/tkinter/Qt), `subprocess.run` (que ESPERA a que el proceso
# termine) bloquea el turno ENTERO hasta que el usuario cierre la
# ventana — y sin timeout, si algo más queda enganchado (un grandchild de
# Windows que retiene el handle del pipe de `capture_output`, o el propio
# comando quedando colgado) el turno queda esperando para siempre,
# "Sintetizando respuesta..." sin límite, sin ningún camino de
# recuperación. Fix de dos partes:
#
#   1) Un comando que lanza un script Python cuyo CONTENIDO importa una
#      librería GUI/de bucle bloqueante (pygame/tkinter/PyQt/PySide) se
#      detecta (`_detect_gui_script_launch`) y se lanza DESATADO
#      (`subprocess.Popen`, sin esperar) — "probarlo" pasa a significar
#      "confirmar que arrancó sin crashear en el primer par de segundos"
#      (`GUI_LAUNCH_GRACE_SECONDS`), nunca "esperar a que el usuario
#      termine de jugar/usarlo".
#   2) CUALQUIER otro comando sigue corriendo bloqueante como antes, pero
#      ahora con un timeout por defecto real
#      (`DEFAULT_RUN_CMD_TIMEOUT_SECONDS`) cuando el llamador no pasa uno
#      explícito — un comando no-GUI que de verdad se cuelga ya no puede
#      trabar el turno para siempre, aunque tarde en reportarlo.
DEFAULT_RUN_CMD_TIMEOUT_SECONDS = 45

GUI_LAUNCH_GRACE_SECONDS = 2.0

_PY_SCRIPT_CMD_RE = re.compile(
    r"^\s*(?:py|pythonw?3?)\b\s+([^\s\"']+\.pyw?)\b", re.IGNORECASE
)

_GUI_BLOCKING_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+(?:pygame|tkinter|Tkinter|PyQt5|PyQt6|PySide2|PySide6)\b"
    r"|from\s+(?:pygame|tkinter|Tkinter|PyQt5|PyQt6|PySide2|PySide6)\b)",
    re.IGNORECASE | re.MULTILINE,
)


def _truncate_tool_output(text: str, label: str, max_chars: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """
    Recorta `text` a `max_chars` con un aviso EXPLÍCITO de cuánto se
    cortó — nunca un corte silencioso: un modelo local pequeño que ve
    un archivo/salida cortada sin aviso puede asumir que llegó al
    final y alucinar sobre el resto, o peor, reinvocar la misma
    herramienta en bucle intentando "ver más" sin saber que ya vio
    todo lo que cabía.
    """
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + f"\n\n[...TRUNCADO: {label} tiene {len(text)} caracteres en total, "
        f"se muestran los primeros {max_chars}...]"
    )


BLOCKED_COMMANDS = {
    "rmdir", "del", "erase", "format", "diskpart",
    "shutdown", "restart", "reg", "icacls", "takeown",
    "powershell -encodedcommand", "bash -c",
    # BLINDAJE 2026-09-16 (auditoría de seguridad, ver el comentario grande
    # junto a `_command_escapes_root` más abajo): "powershell -encodedcommand"
    # y "bash -c" ya bloqueaban la variante más obvia de "intérprete anidado
    # recibe un script entero como un solo string entrecomillado" -- pero
    # "powershell -Command"/"-c" SIN encodear, "pwsh" (PowerShell Core) y
    # "cmd /c" quedaban completamente libres, y son la forma más común de
    # esconder una ruta arbitraria DENTRO de un string que el confinamiento
    # por tokens de `_command_escapes_root` no puede inspeccionar. Mitigación
    # por palabra clave -- no una garantía completa (ver riesgo residual
    # documentado en ese método).
    "powershell -command", "powershell -c ", "pwsh -command", "pwsh -c ",
    "cmd /c", "cmd.exe /c",
}

DANGEROUS_PATTERNS = [
    r"rm\s+-rf",
    r">\s*/dev/null",
    r";\s*rm",
    r"&&\s*rm",
    r"\|\s*bash",
    r"\|\s*powershell"
]

# BLINDAJE (2026-09-17, bug real reportado por el usuario: "simulá 1000
# tiradas de dos dados" entraba en bucle de run_cmd 3 veces y abortaba).
# Diagnóstico con el comando REAL capturado en el log (auditoría byte a
# byte, no una suposición): el modelo compuso
#     python3 -c "\nimport random\ncount = 0\nfor _ in range(1000):\n..."
# donde cada "\n" es la secuencia de DOS CARACTERES de texto barra+ene
# (confirmado con hexdump), no un salto de línea real -- probablemente
# el modelo "sobre-escapó" pensando que necesitaba escapar los saltos de
# línea a mano para el shell. Un bloque `for` con cuerpo indentado
# requiere saltos de línea REALES para ser Python válido, así que este
# patrón produce SIEMPRE el mismo `SyntaxError: unexpected character
# after line continuation character` -- reproducido en aislado antes de
# este fix. `run_cmd_safely` ejecutaba el comando tal cual llegaba, sin
# detectar ni corregir este patrón, así que el modelo reintentaba (con
# el mismo resultado) hasta que el circuit-breaker de bucles lo frenaba.
_PY_INTERPRETER_NAME_RE: Pattern[str] = re.compile(
    r"^(?:.*[\\/])?(python3?|py)(\.exe)?$", re.IGNORECASE
)
_PY_DASH_C_CODE_RE: Pattern[str] = re.compile(
    r'(?P<flag>-c\s+)(?P<quote>["\'])(?P<code>.*)(?P=quote)\s*$', re.DOTALL
)


def _normalize_overescaped_python_dash_c(command: str) -> str:
    """
    ES: Si `command` es una invocación `python`/`python3`/`py -c "..."`
    (con o sin ruta completa al intérprete) cuyo código capturado NO
    tiene ni un solo salto de línea real pero sí contiene la secuencia
    de texto `\\n` (o `\\t`), se asume que el modelo sobre-escapó un
    script multilínea y se reemplazan esas secuencias por saltos de
    línea/tabs reales antes de ejecutar. Deliberadamente conservador:
    si YA hay al menos un salto de línea real en el código, o si no hay
    ninguna secuencia `\\n` de texto, se devuelve `command` sin tocar --
    la señal de "cero saltos de línea reales + al menos un '\\n' de
    texto" es casi inequívoca (un `for`/`if` con cuerpo indentado no es
    Python válido en una sola línea física sin ellos), así que el riesgo
    de corromper un comando que de verdad quería un backslash literal
    (p. ej. una ruta de Windows dentro del código) es bajo. Nunca lanza.

    EN: If `command` is a `python`/`python3`/`py -c "..."` invocation
    (bare or with a full interpreter path) whose captured code has NOT
    ONE real newline but DOES contain the literal text sequence `\\n`
    (or `\\t`), assumes the model over-escaped a multi-line script and
    replaces those sequences with real newlines/tabs before executing.
    Deliberately conservative: if the code already has at least one real
    newline, or has no literal `\\n` sequence at all, `command` is
    returned untouched -- the "zero real newlines + at least one literal
    '\\n'" signal is nearly unambiguous (an indented `for`/`if` body
    isn't valid Python on a single physical line without them), so the
    risk of mangling a command that genuinely wanted a literal backslash
    (e.g. a Windows path inside the code) is low. Never raises.
    """
    try:
        stripped = command.strip()
        if not stripped:
            return command
        parts = stripped.split(None, 1)
        first_token = parts[0].strip("\"'") if parts else ""
        if not _PY_INTERPRETER_NAME_RE.match(first_token):
            return command

        match = _PY_DASH_C_CODE_RE.search(stripped)
        if match is None:
            return command

        code = match.group("code")
        if "\n" in code or "\\n" not in code:
            return command

        fixed_code = (
            code.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
        )
        return (
            stripped[: match.start()] + match.group("flag")
            + match.group("quote") + fixed_code + match.group("quote")
        )
    except Exception:
        return command


def _materialize_multiline_python_dash_c(
    command: str, workdir: "Path"
) -> "Tuple[str, Optional[Path]]":
    """
    ES: BLINDAJE (2026-09-17, root cause real del bucle de `run_cmd` con
    scripts multilínea -- encontrado recién al agregar logging de
    RESULTADO en `execute_tool_from_call`, ver el log real capturado:
    "Comando ejecutado con éxito (sin salida)"). Este bug es DISTINTO
    del que corrige `_normalize_overescaped_python_dash_c` de arriba (ese
    corrige un `\n` de TEXTO que el modelo a veces manda en vez de un
    salto de línea real) -- acá el código YA tiene saltos de línea
    REALES, pero eso sigue rompiendo en Windows: `run_cmd_safely` corre
    con `shell=True`, es decir `cmd.exe /c <command completo>` -- y
    `cmd.exe` NO sabe parsear un argumento entre comillas que contenga
    saltos de línea reales embebidos, a diferencia de una shell POSIX.
    El resultado observado en producción: el proceso arranca y termina
    con código 0 (éxito), pero Python nunca ve el cuerpo real del
    script -- equivalente a `python3 -c ""` -- así que el `print(...)`
    final nunca corre. Esto reproduce SIEMPRE para cualquier `-c`
    multilínea en Windows, sin importar si `_normalize_overescaped_python_dash_c`
    ya corrigió un escapado previo -- por eso el bucle de "3 intentos
    sin avanzar" persistía incluso con ese fix ya verificado.

    La solución robusta (funciona en Windows Y POSIX, no depende de
    ninguna regla de quoting de ninguna shell) es no pasar código
    multilínea como argumento de línea de comandos en absoluto: se
    escribe a un archivo .py temporal DENTRO del workspace activo
    (`workdir`, el mismo directorio ya confinado por el sandbox) y se
    reemplaza el comando por `<intérprete> "<ruta al archivo>"` --
    mismo intérprete, mismo código, sin ninguna ambigüedad de parseo
    de shell. El archivo se borra después de ejecutar (ver el `finally`
    en `run_cmd_safely`).

    IMPORTANTE: se llama DESPUÉS de que `run_cmd_safely` ya corrió
    todos los chequeos de seguridad (blacklist, `_command_escapes_root`,
    `BLOCKED_COMMANDS`, `DANGEROUS_PATTERNS`) sobre el comando ORIGINAL
    con el código todavía inline -- si se materializara ANTES, esos
    chequeos basados en texto solo verían "python3 /ruta/temp.py" y
    NUNCA el contenido real del script, un bypass de seguridad grave.

    Devuelve `(command, None)` sin tocar nada si `command` no es una
    invocación `-c` con código multilínea -- en ese caso (una sola
    línea física) el problema de arriba no aplica. Nunca lanza:
    cualquier error al escribir el archivo temporal se traga y devuelve
    el comando original sin tocar.

    EN: root cause fix for the `run_cmd` loop with multi-line scripts,
    found only after adding RESULT logging to `execute_tool_from_call`
    (see the real captured log: "Command executed successfully (no
    output)"). Distinct from `_normalize_overescaped_python_dash_c`
    above (that one fixes literal `\n` TEXT the model sometimes sends
    instead of a real newline) -- here the code already has REAL
    newlines, but that still breaks on Windows: `run_cmd_safely` runs
    with `shell=True`, i.e. `cmd.exe /c <full command>` -- and `cmd.exe`
    cannot parse a quoted argument containing embedded real newlines the
    way a POSIX shell can. Observed result in production: the process
    starts and exits with code 0 (success), but Python never sees the
    real script body -- equivalent to `python3 -c ""` -- so the final
    `print(...)` never runs. This reproduces EVERY time for any
    multi-line `-c` on Windows, regardless of the earlier normalization
    fix -- which is why the "3 attempts without progress" loop kept
    happening even with that fix already verified.

    The robust fix (works on Windows AND POSIX, depends on no shell's
    quoting rules) is to not pass multi-line code as a command-line
    argument at all: write it to a temp .py file INSIDE the active
    workspace (`workdir`, the same sandbox-confined directory) and
    replace the command with `<interpreter> "<file path>"` -- same
    interpreter, same code, zero shell-parsing ambiguity. The file is
    deleted after running (see the `finally` in `run_cmd_safely`).

    IMPORTANT: called AFTER `run_cmd_safely` already ran every security
    check (blacklist, `_command_escapes_root`, `BLOCKED_COMMANDS`,
    `DANGEROUS_PATTERNS`) against the ORIGINAL command with the code
    still inline -- materializing BEFORE those checks would mean they
    only ever see "python3 /path/temp.py", never the script's real
    content: a serious security bypass.

    Returns `(command, None)` untouched if `command` isn't a `-c`
    invocation with multi-line code -- in that case (a single physical
    line) the problem above doesn't apply. Never raises: any error
    writing the temp file is swallowed and the original command is
    returned untouched.
    """
    try:
        stripped = command.strip()
        if not stripped:
            return command, None
        parts = stripped.split(None, 1)
        first_token = parts[0].strip("\"'") if parts else ""
        if not _PY_INTERPRETER_NAME_RE.match(first_token):
            return command, None

        match = _PY_DASH_C_CODE_RE.search(stripped)
        if match is None:
            return command, None

        code = match.group("code")
        if "\n" not in code:
            return command, None

        import tempfile

        fd, tmp_path_str = tempfile.mkstemp(
            suffix=".py", prefix="sovnode_runcmd_", dir=str(workdir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(code)
        except Exception:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            return command, None

        tmp_path = Path(tmp_path_str)
        new_command = f'{parts[0]} "{tmp_path}"'
        return new_command, tmp_path
    except Exception:
        return command, None


# BLINDAJE (bug real, capturado en video 2026-09-07: "Fuga de Aislamiento en
# ToolSandbox — Escritura en src/core/"): sin ningún workspace activo,
# `ToolSandbox.root_dir` caía a `os.getcwd()` — que tanto en desarrollo como
# en la app empaquetada corriendo desde su carpeta de instalación ES la
# carpeta del PROYECTO, la misma que contiene `src/`, `tests/`,
# `SovNode.spec` y `build.py`. Visto en vivo: `write_file` creó
# `ecuacion_energia_kinectica.py` (con un error de sintaxis) directo dentro
# de `src/core/`, abierto en VS Code, sin que nada lo impidiera — el modelo
# no distinguía "workspace del usuario" de "código fuente de la app".
#
# Dos capas de defensa, independientes entre sí:
#
#   1) `_default_isolated_root()` (ver `ToolSandbox.__init__`): en ausencia
#      de un `allowed_directory` explícito, la raíz por defecto pasa a ser
#      una carpeta `workspace/` DEDICADA y aislada (se crea si no existe),
#      nunca la carpeta de instalación misma — así ninguna ruta relativa
#      puede alcanzar `src/` sin escapar de esa raíz, lo que
#      `validate_path` ya rechaza (BLINDAJE de más arriba, prefijo/case).
#
#   2) La lista negra de abajo (`_is_blacklisted_target`, usada por
#      `validate_path(..., for_write=True)` y por `run_cmd_safely`): cubre
#      el caso en que la capa 1 NO alcanza — el usuario agrega la carpeta
#      del propio proyecto SovNode como workspace (p. ej. para ayudar a
#      desarrollarlo), donde `src/`/`tests/`/`SovNode.spec`/`build.py`
#      quedan LEGÍTIMAMENTE dentro de la raíz activa y la sola contención
#      de `validate_path` no los protege. Se recalcula contra
#      `os.getcwd()` en cada llamada (no una vez a nivel de módulo) para
#      no depender de qué era el cwd al importar este archivo.
_DEFAULT_WORKSPACE_DIRNAME = "workspace"
_PROJECT_BLACKLIST_DIRNAMES = ("src", "tests")
_PROJECT_BLACKLIST_FILENAMES = ("SovNode.spec", "build.py")


def _project_blacklist_paths(base_dir: Path) -> List[Path]:
    """Rutas absolutas, bajo `base_dir` (la carpeta de instalación/lanzamiento
    de la app), que jamás deben aceptar una escritura ni ser el objetivo de
    un comando ejecutado: el propio código fuente y los artefactos de build
    de SovNode."""
    paths = [base_dir / name for name in _PROJECT_BLACKLIST_DIRNAMES]
    paths += [base_dir / name for name in _PROJECT_BLACKLIST_FILENAMES]
    return paths

def retry_on_failure(retries: int = 3, delay: float = 0.5):
    """Decorador de resiliencia ante fallos transitorios."""
    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            last_err = None
            for _ in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    time.sleep(delay)
            return f"[SANDBOX ERROR]: Fallo crítico tras {retries} intentos. Último error: {last_err}"
        return wrapper
    return decorator


class ToolSandbox:
    def __init__(self, allowed_directory: str | None = None) -> None:
        self.root_dir = (
            Path(allowed_directory).resolve()
            if allowed_directory
            else self._default_isolated_root()
        )

    @staticmethod
    def _default_isolated_root() -> Path:
        """
        Raíz por defecto cuando no hay ningún workspace configurado
        (`ToolSandbox()` sin argumentos, como hace `LocalToolDispatcher`
        antes de que la UI llame a `set_root()`): una carpeta `workspace/`
        dedicada junto a la instalación de la app, creada si no existe —
        nunca `os.getcwd()` a secas, que es la carpeta del PROYECTO
        (contiene `src/`, `tests/`, `SovNode.spec`, `build.py`). Ver el
        BLINDAJE de 2026-09-07 más arriba (junto a `_PROJECT_BLACKLIST_*`)
        para el bug real que esto corrige.
        """
        default_root = Path(os.getcwd()) / _DEFAULT_WORKSPACE_DIRNAME
        default_root.mkdir(parents=True, exist_ok=True)
        return default_root.resolve()

    def _is_blacklisted_target(self, resolved: Path) -> bool:
        """
        ¿`resolved` (ya una ruta absoluta) cae dentro del propio código
        fuente/artefactos de build de SovNode (`src/`, `tests/`,
        `SovNode.spec`, `build.py`, relativos a la carpeta de instalación
        actual)? Independiente de cuál sea `self.root_dir` en este
        momento — es la segunda capa de defensa descrita junto a
        `_project_blacklist_paths` más arriba. Nunca lanza.
        """
        try:
            base_dir = Path(os.getcwd()).resolve()
            resolved_cmp = os.path.normcase(str(resolved))
            for blacklisted in _project_blacklist_paths(base_dir):
                blacklisted_cmp = os.path.normcase(str(blacklisted.resolve()))
                if resolved_cmp == blacklisted_cmp or resolved_cmp.startswith(blacklisted_cmp + os.sep):
                    return True
        except Exception:
            return False
        return False

    def validate_path(self, target_path: str, *, for_write: bool = False) -> Path:
        r"""
        BLINDAJE (falsos PermissionError en Windows): la comparación
        anterior (`str(resolved).startswith(str(self.root_dir))`) es
        sensible a mayúsculas/minúsculas — en Windows, la letra de
        unidad puede llegar normalizada distinto entre `root_dir` (fijado
        en __init__) y `resolved` (recién calculado aquí) dependiendo de
        cómo el SO/la librería que originó cada ruta la devolvió (`C:\`
        vs `c:\`), aunque ambas apunten al MISMO directorio físico —
        Windows no distingue mayúsculas de minúsculas en rutas.
        `os.path.normcase()` normaliza ambos lados antes de comparar (en
        Windows: minúsculas + `/` -> `\`; no-op en POSIX, donde SÍ
        importa el case).

        BLINDAJE #2 (bug de prefijo hermano, ya presente antes de este
        cambio): un `startswith()` a secas sobre las rutas también
        aprobaría por error un directorio HERMANO cuyo nombre extiende
        el de la raíz como prefijo de texto — p. ej. `root_dir` =
        `C:\Users\steph` dejaría pasar `C:\Users\stephFoo\secreto.txt`,
        que NO está realmente bajo la raíz permitida. Se exige que,
        tras la raíz, siga exactamente `os.sep` (o que la ruta sea un
        match exacto) — la raíz real del bug era comparar prefijos de
        TEXTO en vez de límites de RUTA.

        BLINDAJE #3 (bug real, capturado en video 2026-09-02: `write_file`
        legítimo rechazado dos veces seguidas): el modelo a veces devuelve
        `path` con un separador inicial — `/workspace/system_health.py`,
        pensando el workspace como si fuera la raíz del filesystem.
        `pathlib` interpreta eso como una ruta ABSOLUTA: en
        `self.root_dir / target_path`, si el operando derecho ya es
        absoluto, el operador `/` de `PurePath` DESCARTA `root_dir` por
        completo (comportamiento documentado de `PurePath.__truediv__`),
        y el resultado queda anclado a la raíz real del SO (en Windows,
        la unidad actual) — bien fuera de `root_dir`. El chequeo de abajo
        entonces lo rechazaba con un `PermissionError` engañoso: parece
        "acceso denegado" cuando la intención era 100% legítima y el
        archivo nunca se llegó a escribir. Se normaliza ANTES de unir:
        separadores homogeneizados a `/`, cualquier letra de unidad tipo
        `C:` que el modelo haya agregado se descarta, y los `/` iniciales
        se recortan — así una ruta que el modelo cree "absoluta" se
        interpreta SIEMPRE relativa a `root_dir`, la única raíz que debe
        existir desde su perspectiva.
        """
        normalized = str(target_path).replace("\\", "/")
        _drive, normalized = os.path.splitdrive(normalized)
        normalized = normalized.lstrip("/")
        resolved = (self.root_dir / normalized).resolve()
        resolved_cmp = os.path.normcase(str(resolved))
        root_cmp = os.path.normcase(str(self.root_dir))
        if resolved_cmp != root_cmp and not resolved_cmp.startswith(root_cmp + os.sep):
            raise PermissionError(f"Acceso denegado fuera de la raíz de trabajo: {target_path}")
        if for_write and self._is_blacklisted_target(resolved):
            raise PermissionError(
                f"Escritura bloqueada: '{target_path}' apunta al propio código fuente de "
                "SovNode (src/, tests/, SovNode.spec o build.py) — el modelo nunca debe "
                "modificar la base de código de la app, sin importar qué carpeta esté "
                "activa como workspace."
            )
        return resolved

    def set_root(self, allowed_directory: str) -> None:
        """
        BLINDAJE (bug real, reportado 2026-09-02: "el workspace actual no
        me permite ver la carpeta ni modificarla — añadir archivos,
        modificar archivos, leer archivos, analizar archivos", con el log
        mostrando el WorkspaceScanner re-indexando la carpeta para RAG
        correctamente mientras las herramientas seguían operando sobre
        otra raíz).

        `root_dir` se fijaba UNA sola vez en `__init__`, con
        `os.getcwd()` (la carpeta de instalación de la app) si no se
        pasaba `allowed_directory` explícito — y `LocalToolDispatcher()`
        nunca lo pasaba. El panel "Workspaces" de la UI
        (sovnode_qt.py: _on_add_workspace_clicked /
        _on_remove_workspace_clicked / _load_persisted_workspaces) solo
        actualizaba `WorkspaceScanner` (indexado RAG) y `memory_graph`
        (persistencia) — nunca esta raíz — así que agregar o quitar
        carpetas desde la UI no tenía NINGÚN efecto sobre qué podían
        leer/escribir/listar/ejecutar `read_file`/`write_file`/
        `run_cmd`/`list_dir`, sin importar cuántas veces cambiara el
        "workspace actual" visible en pantalla.

        Este método permite reasignar `root_dir` en caliente; el
        llamador (`sovnode_qt.py::_sync_tool_sandbox_root`) lo invoca
        cada vez que la lista de carpetas de Workspaces cambia.
        """
        self.root_dir = Path(allowed_directory).resolve()

    def _detect_gui_script_launch(self, command: str) -> Optional[Path]:
        """
        ¿`command` lanza un script Python que arma su propia ventana con
        bucle de eventos bloqueante (pygame/tkinter/Qt)? Devuelve la ruta
        RESUELTA (dentro del sandbox) del script si es así, o `None` si
        no matchea el patrón de invocación, el archivo no existe/no se
        puede leer, o el archivo no importa ninguna de esas librerías —
        en cualquiera de esos casos el llamador debe seguir el camino
        bloqueante normal (ver `run_cmd_safely`). Nunca lanza.
        """
        match = _PY_SCRIPT_CMD_RE.match(command)
        if not match:
            return None
        try:
            script_path = self.validate_path(match.group(1))
            if not script_path.is_file():
                return None
            content = script_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        if not _GUI_BLOCKING_IMPORT_RE.search(content):
            return None
        return script_path

    def _run_gui_script_detached(self, command: str, script_path: Path) -> str:
        """
        Lanza `command` SIN esperar a que termine (a diferencia del resto
        de `run_cmd_safely`, que sí espera) — pensado para apps GUI con
        su propio bucle de eventos, donde "esperar a que termine" en la
        práctica significa "esperar a que el usuario cierre la ventana".
        stdout/stderr/stdin van a DEVNULL (no a un pipe): con
        `capture_output`, un proceso desatado que sigue vivo mucho
        después de que este método retorna deja el pipe abierto
        indefinidamente del lado del padre — el mismo tipo de fuga de
        handle que este fix busca evitar, no solo trasladarla.
        """
        try:
            if os.name != "nt":
                proc = subprocess.Popen(
                    shlex.split(command),
                    cwd=str(self.root_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=str(self.root_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
        except Exception as exc:
            return f"[SANDBOX EXEC ERROR]: {exc}"

        time.sleep(GUI_LAUNCH_GRACE_SECONDS)
        returncode = proc.poll()
        if returncode is not None and returncode != 0:
            return (
                f"[SANDBOX ERROR]: '{script_path.name}' se cerró casi de inmediato "
                f"(código de salida {returncode}) al lanzarlo con '{command}' — "
                "probablemente falló al iniciar (revisá que las dependencias, "
                "p. ej. pygame, estén instaladas, o que el archivo no tenga un "
                "error de sintaxis)."
            )
        return (
            f"'{script_path.name}' se lanzó como una aplicación gráfica "
            "independiente y sigue corriendo en su propia ventana — no se "
            "espera a que el usuario la cierre para continuar. No crasheó en "
            f"los primeros {GUI_LAUNCH_GRACE_SECONDS:.0f}s, así que arrancó bien."
        )

    def _command_targets_blacklist(self, command: str) -> Optional[str]:
        """
        Defensa en profundidad para cuando `root_dir` legítimamente ABARCA
        el propio proyecto SovNode (p. ej. el usuario agregó esa carpeta
        como workspace para ayudar a desarrollarlo) — ahí la contención de
        `validate_path` por sí sola no alcanza, porque `src/`, `tests/`,
        `SovNode.spec` y `build.py` quedan DENTRO de esa raíz legítima.
        Escanea `command` token por token (mejor esfuerzo, nunca lanza) y
        resuelve cada uno relativo a `root_dir` (o como ruta absoluta si ya
        lo es); si alguno cae en la lista negra, devuelve ese token tal
        cual apareció en el comando. Si el tokenizado falla o ninguno
        matchea, devuelve `None` y el llamador sigue el camino normal.
        """
        try:
            tokens = shlex.split(command, posix=(os.name != "nt"))
        except ValueError:
            tokens = command.split()
        for token in tokens:
            candidate = token.strip("\"'")
            if not candidate or candidate.startswith("-"):
                continue
            try:
                path_obj = Path(candidate)
                resolved = (
                    path_obj.resolve()
                    if path_obj.is_absolute()
                    else (self.root_dir / candidate).resolve()
                )
            except Exception:
                continue
            if self._is_blacklisted_target(resolved):
                return candidate
        return None

    def _command_escapes_root(self, command: str) -> Optional[str]:
        """
        BLINDAJE 2026-09-16 (fuga de aislamiento REAL en run_cmd_safely,
        detectada en auditoría de seguridad -- distinta de la del
        2026-09-07 de más arriba, que solo protegía el código fuente de
        SovNode). A diferencia de read_file/write_file/list_dir, que
        SIEMPRE pasan por `validate_path` y por lo tanto quedan
        confinados a `root_dir` (capa 1), `run_cmd_safely` nunca tuvo esa
        capa: solo tenía `_command_targets_blacklist` (protege el código
        fuente de SovNode, no rutas arbitrarias) y las listas
        `BLOCKED_COMMANDS`/`DANGEROUS_PATTERNS` (palabras/patrones
        puntuales, capa 2). Nada impedía un comando como
        `type C:\\Users\\<usuario>\\Documents\\x.txt` (lectura arbitraria
        de cualquier archivo del sistema) o `powershell -Command
        "Remove-Item C:\\ruta\\lo-que-sea"` (borrado arbitrario) --
        ninguno de los dos toca src/tests/ ni matchea las listas negras
        existentes, así que corrían sin ningún filtro.

        Este método es la capa 1 que faltaba, calcada de `validate_path`:
        tokeniza `command` igual que `_command_targets_blacklist`, expande
        variables de entorno (%USERPROFILE%, $HOME, etc. -- así el
        cmd.exe/shell real las expandiría antes de tocar el disco) y
        resuelve cada token como ruta -- absoluta tal cual, o relativa a
        `root_dir` (lo que también atrapa un "../../Documents/x.txt"
        relativo que intente escapar con ".."). Si algún token resuelto
        queda FUERA de `root_dir`, bloquea el comando ENTERO devolviendo
        ese token; si todos quedan adentro (el caso normal: "python
        snake.py", "pip install requests", "dir", "git status" -- son
        relativos y se anclan bajo `root_dir` por construcción, nunca se
        marcan), devuelve `None` y el llamador sigue normal.

        RIESGO RESIDUAL (documentado, no resuelto acá): esto inspecciona
        tokens del comando tal como los separa Python, no lo que un
        intérprete ANIDADO (powershell -Command "...", cmd /c "...")
        hace con un string de script completo pasado como UN SOLO token
        entrecomillado -- ahí la ruta puede quedar oculta dentro de ese
        string y este chequeo no la ve (el token completo, con espacios y
        comillas internas, no resuelve como una ruta real y por lo tanto
        no dispara nada). La lista `BLOCKED_COMMANDS` suma esos
        intérpretes anidados como mitigación adicional, pero no es una
        garantía completa contra un comando deliberadamente ofuscado. La
        protección fuerte real para ese caso sería no usar `shell=True`
        en Windows, lo que rompería pipes/redirects que hoy se usan
        legítimamente -- fuera de alcance de este blindaje puntual.

        EN: The "layer 1" root confinement that read_file/write_file/
        list_dir already had via `validate_path`, but run_cmd_safely
        never did -- so `type C:\\Users\\...\\passwords.txt` or
        `powershell -Command "Remove-Item C:\\..."` ran with zero
        filesystem confinement (only a narrow SovNode-source-code
        blacklist and a short keyword/pattern list). Tokenizes, expands
        env vars, resolves each token as a path (absolute as-is, relative
        anchored to root_dir -- also catching ".." traversal), and blocks
        the whole command if any resolved token lands outside root_dir.
        Residual risk: a nested interpreter that takes a whole script as
        one quoted argument (powershell -Command "...", cmd /c "...") can
        hide a path inside that string where this per-token scan can't
        see it -- mitigated, not fully solved, by the expanded
        BLOCKED_COMMANDS list. A complete fix means not using shell=True
        on Windows, which would break legitimate pipes/redirects -- out
        of scope here. Nunca lanza / never raises.
        """
        try:
            tokens = shlex.split(command, posix=(os.name != "nt"))
        except ValueError:
            tokens = command.split()
        try:
            root_cmp = os.path.normcase(str(self.root_dir.resolve()))
        except Exception:
            return None
        for token in tokens:
            candidate = token.strip("\"'")
            if not candidate or candidate.startswith("-"):
                continue
            try:
                expanded = os.path.expandvars(candidate)
                path_obj = Path(expanded)
                resolved = (
                    path_obj.resolve()
                    if path_obj.is_absolute()
                    else (self.root_dir / expanded).resolve()
                )
            except Exception:
                continue
            resolved_cmp = os.path.normcase(str(resolved))
            if resolved_cmp == root_cmp or resolved_cmp.startswith(root_cmp + os.sep):
                continue
            return candidate
        return None

    @retry_on_failure(retries=2, delay=0.5)
    def run_cmd_safely(self, command: str, timeout_sec: int | None = None) -> str:
        # BLINDAJE (2026-09-17, ver `_normalize_overescaped_python_dash_c`):
        # corrige ANTES que nada un `python -c "..."` multilínea
        # sobre-escapado por el modelo -- así todas las capas de abajo
        # (blacklist, `_command_escapes_root`, clasificador de riesgo,
        # ejecución real) ya ven el comando corregido, consistentemente.
        # No-op para cualquier comando que no matchee ese patrón exacto.
        command = _normalize_overescaped_python_dash_c(command)
        cmd_lower = command.lower().strip()

        # BLINDAJE 2026-09-07 (fuga de aislamiento en ToolSandbox): si el
        # workspace activo ES (o queda dentro de) el propio código fuente
        # de SovNode, o si el comando referencia explícitamente src/tests/
        # SovNode.spec/build.py, se rechaza ANTES de tocar el disco — ver
        # el BLINDAJE junto a `_PROJECT_BLACKLIST_*` más arriba.
        if self._is_blacklisted_target(self.root_dir):
            return (
                f"[SANDBOX ERROR]: Ejecución bloqueada — el workspace activo "
                f"('{self.root_dir}') es el propio código fuente de SovNode "
                "(src/, tests/, SovNode.spec o build.py)."
            )
        blacklisted_token = self._command_targets_blacklist(command)
        if blacklisted_token is not None:
            return (
                f"[SANDBOX ERROR]: Ejecución bloqueada — el comando apunta al propio "
                f"código fuente de SovNode ('{blacklisted_token}'); el modelo nunca debe "
                "ejecutar ni modificar la base de código de la app, sin importar qué "
                "carpeta esté activa como workspace."
            )

        # BLINDAJE 2026-09-16 (capa 1 que faltaba -- ver el comentario
        # grande junto a `_command_escapes_root`): confinamiento real a
        # `root_dir`, igual que read_file/write_file/list_dir ya tienen
        # vía `validate_path`. Antes de esto, un comando podía leer o
        # borrar CUALQUIER archivo del sistema con solo no mencionar
        # src/tests/SovNode.spec/build.py.
        escaping_token = self._command_escapes_root(command)
        if escaping_token is not None:
            return (
                f"[SANDBOX ERROR]: Ejecución bloqueada — el comando referencia "
                f"'{escaping_token}', que queda fuera del workspace activo "
                f"('{self.root_dir}'). run_cmd_safely está confinado al workspace "
                "igual que read_file/write_file/list_dir."
            )

        for blocked in BLOCKED_COMMANDS:
            if blocked in cmd_lower:
                return f"[SANDBOX ERROR]: Comando bloqueado por política de seguridad ('{blocked}')."

        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, cmd_lower):
                return "[SANDBOX ERROR]: Patrón de ejecución inseguro detectado."

        gui_script_path = self._detect_gui_script_launch(command)
        if gui_script_path is not None:
            return self._run_gui_script_detached(command, gui_script_path)

        # BLINDAJE (2026-09-17, ver `_materialize_multiline_python_dash_c`):
        # se llama DESPUÉS de todos los chequeos de seguridad de arriba
        # (blacklist / _command_escapes_root / BLOCKED_COMMANDS /
        # DANGEROUS_PATTERNS), que ya vieron el código real inline en
        # `command` -- acá recién se reescribe a "intérprete + archivo
        # temporal" para esquivar el parseo roto de `cmd.exe` con
        # argumentos multilínea en Windows.
        command, _tmp_py_file = _materialize_multiline_python_dash_c(command, self.root_dir)

        try:
            if os.name != 'nt':
                args = shlex.split(command)
                use_shell = False
            else:
                args = command
                use_shell = True

            # BLINDAJE (detectado escribiendo el fix de arriba, mismo bug de
            # fondo que `ToolSandbox.set_root` ya documenta para
            # read_file/write_file/list_dir): sin `cwd`, este subprocess
            # corre relativo al directorio de trabajo del PROCESO de la
            # app (típicamente su carpeta de instalación), NUNCA relativo
            # a `root_dir` -- que es justamente lo que cambia cuando el
            # usuario agrega/quita carpetas en el panel "Workspaces". Un
            # comando con ruta relativa ("python snake.py") solo
            # encontraba el archivo por COINCIDENCIA, cuando el workspace
            # activo resultaba ser el mismo que la carpeta de instalación
            # -- en cualquier otro workspace, `write_file`/`read_file` ya
            # operaban sobre la raíz correcta pero `run_cmd` fallaba con
            # "no such file or directory" sobre un archivo que sí existe,
            # solo que en otra carpeta.
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout_sec if timeout_sec is not None else DEFAULT_RUN_CMD_TIMEOUT_SECONDS,
                shell=use_shell,
                cwd=str(self.root_dir),
            )

            stdout = result.stdout.strip()
            stderr = result.stderr.strip()

            if stdout:
                return _truncate_tool_output(stdout, label=f"la salida del comando '{command}'")
            elif stderr:
                return f"[STDERR]\n{_truncate_tool_output(stderr, label=f'el stderr del comando {command!r}')}"
            else:
                return "Comando ejecutado con éxito (sin salida)."
        except subprocess.TimeoutExpired:
            return (
                f"[SANDBOX TIMEOUT]: La instrucción excedió el límite de "
                f"{timeout_sec if timeout_sec is not None else DEFAULT_RUN_CMD_TIMEOUT_SECONDS}s."
            )
        except Exception as exc:
            return f"[SANDBOX EXEC ERROR]: {exc}"
        finally:
            if _tmp_py_file is not None:
                try:
                    _tmp_py_file.unlink(missing_ok=True)
                except OSError:
                    pass

    @retry_on_failure(retries=2, delay=0.5)
    def read_file_safely(self, path: str) -> str:
        try:
            target = self.validate_path(path)
            if not target.exists():
                return f"[SANDBOX ERROR]: El archivo '{path}' no existe."
            content = target.read_text(encoding="utf-8", errors="replace")
            return _truncate_tool_output(
                content, label=f"el archivo '{path}'",
                max_chars=MAX_READ_FILE_OUTPUT_CHARS,
            )
        except Exception as exc:
            return f"[SANDBOX READ ERROR]: {exc}"

    @retry_on_failure(retries=2, delay=0.5)
    def write_file_safely(self, path: str, content: str) -> str:
        try:
            target = self.validate_path(path, for_write=True)
            # BLINDAJE (bug #8, MEDIDO 2026-09-09 — video demo "improve the
            # snake game"): el modelo generó un write_file de .py con
            # código roto (paréntesis sin cerrar) y SovNode lo escribió tal
            # cual al disco sin avisar — el usuario recién se enteraba al
            # intentar correr el archivo. Un archivo .py es la ÚNICA
            # herramienta que produce algo ejecutable directamente por el
            # usuario (a diferencia de un .txt/.json/.md, donde "sintaxis
            # inválida" no tiene sentido) — por eso el chequeo se limita a
            # esa extensión. ast.parse no ejecuta nada del código (a
            # diferencia de compile(..., 'exec') + exec, que si podría
            # correr efectos secundarios a nivel de módulo antes de fallar)
            # — solo valida la gramática, igual que hace Python al importar
            # el archivo. Si falla, NO se escribe nada a disco y se
            # devuelve el mismo prefijo "[SANDBOX WRITE ERROR]" que ya
            # reconoce el resto del pipeline (ver `_is_internal_toolguard_notice`
            # y el chequeo de escritura fallida en orchestrator.py) para
            # que el bucle de herramientas lo trate como un fallo real y
            # el modelo pueda corregir el código y reintentar, en vez de
            # que el usuario reciba un archivo que no corre.
            # EN: Bug #8 (MEASURED 2026-09-09) — the model produced a
            # write_file with broken Python (unclosed paren) and it landed
            # on disk unchanged; the user only found out by trying to run
            # it. Restricted to .py because "invalid syntax" is meaningless
            # for a .txt/.json/.md payload. ast.parse never executes the
            # code (unlike compile(..., 'exec') + exec, which could run
            # module-level side effects before failing) — it only checks
            # grammar, the same thing Python does on import. On failure,
            # nothing is written and the same "[SANDBOX WRITE ERROR]"
            # prefix already recognized elsewhere in the pipeline is
            # returned, so the tool loop treats it as a real failure and
            # the model can fix the code and retry instead of the user
            # getting a file that doesn't run.
            if target.suffix.lower() == ".py":
                try:
                    ast.parse(content, filename=target.name)
                except SyntaxError as exc:
                    return (
                        f"[SANDBOX WRITE ERROR]: No se escribió '{target.name}' — "
                        f"el código Python tiene un error de sintaxis en la línea "
                        f"{exc.lineno}: {exc.msg}. Corregí el código COMPLETO "
                        "(no un fragmento) y volvé a llamar a write_file; no "
                        "repitas el mismo contenido roto."
                    )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Archivo escrito exitosamente en '{target.name}' ({len(content)} bytes)."
        except Exception as exc:
            return f"[SANDBOX WRITE ERROR]: {exc}"

    @retry_on_failure(retries=2, delay=0.5)
    def edit_file_safely(
        self,
        path: str,
        old_str: str = "",
        new_str: str = "",
        edits: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """
        Prioridad #5 (2026-09-14, patches tipo SEARCH/REPLACE) --
        reemplaza UNA ocurrencia exacta y única de `old_str` por
        `new_str` en un archivo YA EXISTENTE, en vez de reescribirlo
        entero (ver la nota grande arriba de esta clase). Mismo
        contrato de unicidad que la herramienta Edit de Claude Code/
        Cowork: si `old_str` no aparece, o aparece más de una vez, no
        se escribe NADA -- mejor un error claro que adivinar una
        posición y romper el archivo. Comparte el mismo chequeo de
        `ast.parse` que `write_file_safely` para archivos .py, y el
        mismo prefijo "[SANDBOX WRITE ERROR]" para que el resto del
        pipeline (guards de éxito/fallo, reindexado RAG) trate un
        fallo de edit_file exactamente igual que uno de write_file.

        BLINDAJE (2026-09-17, patch_orchestrator55 -- pedido explícito
        del usuario: "no tiene que reescribir todo el archivo entero
        para mejorarlo... se gasta los tokens en añadir la función o
        en reemplazar una función por otra"): con `MAX_AUTONOMOUS_TOOL_
        PASSES = 3` y un `read_file` que ya consume uno de esos pases,
        un cambio que toca VARIOS lugares del archivo no entraba en un
        solo par `old_str`/`new_str`, y el modelo terminaba cayendo en
        `write_file` con el archivo entero -- el desperdicio de tokens
        que esta herramienta existe para evitar. Ahora acepta también
        `edits`: una lista de objetos `{old_str, new_str}` que se
        aplican EN ORDEN y de forma ATÓMICA -- si cualquiera no
        matchea (0 o más de 1 vez, contra el contenido ya modificado
        por las ediciones anteriores de este mismo llamado), no se
        escribe nada y el error identifica cuál (por índice) falló. La
        forma legacy de un solo `old_str`/`new_str` sigue funcionando
        exactamente igual que antes (se trata como una lista de un
        solo elemento).
        """
        try:
            target = self.validate_path(path, for_write=True)
            if not target.exists():
                return (
                    f"[SANDBOX WRITE ERROR]: No se pudo editar '{path}' -- el "
                    "archivo no existe. Para crear un archivo nuevo usá "
                    "write_file, no edit_file."
                )

            if edits:
                if not isinstance(edits, list):
                    return (
                        "[SANDBOX WRITE ERROR]: 'edits' debe ser una lista de "
                        "objetos {old_str, new_str}. No se modificó nada."
                    )
                edit_list = edits
            elif old_str:
                edit_list = [{"old_str": old_str, "new_str": new_str}]
            else:
                return (
                    "[SANDBOX WRITE ERROR]: edit_file necesita 'old_str' (o "
                    "'edits') -- el/los fragmento(s) EXACTO(s) de texto a "
                    "reemplazar. No se modificó nada."
                )

            current = target.read_text(encoding="utf-8", errors="replace")
            working = current
            for idx, item in enumerate(edit_list):
                item_old = (item or {}).get("old_str", "") if isinstance(item, dict) else ""
                item_new = (item or {}).get("new_str", "") if isinstance(item, dict) else ""
                if not isinstance(item, dict) or not item_old:
                    return (
                        f"[SANDBOX WRITE ERROR]: 'edits[{idx}]' inválido -- cada "
                        "edición necesita un objeto con 'old_str' (el fragmento "
                        "EXACTO a reemplazar) y 'new_str'. No se modificó nada."
                    )
                occurrences = working.count(item_old)
                if occurrences == 0:
                    prefix = f"'old_str' de 'edits[{idx}]'" if edits else "'old_str'"
                    return (
                        f"[SANDBOX WRITE ERROR]: {prefix} no se encontró en "
                        f"'{target.name}' -- no coincide con el contenido EXACTO "
                        "del archivo (¿espacios, indentación o saltos de línea "
                        "distintos, o ya lo cambió una edición anterior de este "
                        "mismo llamado?). No se modificó nada; releé el archivo "
                        "con read_file si no estás seguro del contenido actual."
                    )
                if occurrences > 1:
                    prefix = f"'old_str' de 'edits[{idx}]'" if edits else "'old_str'"
                    return (
                        f"[SANDBOX WRITE ERROR]: {prefix} aparece {occurrences} "
                        f"veces en '{target.name}' -- tiene que ser único. Agregá "
                        "más líneas de contexto alrededor para que coincida en un "
                        "solo lugar. No se modificó nada."
                    )
                working = working.replace(item_old, item_new, 1)

            new_content = working
            if target.suffix.lower() == ".py":
                try:
                    ast.parse(new_content, filename=target.name)
                except SyntaxError as exc:
                    return (
                        f"[SANDBOX WRITE ERROR]: No se aplicó el cambio en "
                        f"'{target.name}' -- el resultado tendría un error de "
                        f"sintaxis en la línea {exc.lineno}: {exc.msg}. Revisá "
                        "'old_str'/'new_str' y volvé a intentar."
                    )
            target.write_text(new_content, encoding="utf-8")
            n = len(edit_list)
            detail = f" ({n} ediciones aplicadas)" if n > 1 else ""
            return (
                f"Archivo '{target.name}' editado exitosamente "
                f"({len(current)} -> {len(new_content)} bytes){detail}."
            )
        except Exception as exc:
            return f"[SANDBOX WRITE ERROR]: {exc}"


try:
    import psutil
except ImportError:
    psutil = None

DEV_MODE = True

def safe_tool_execution(func):
    """Decorador global de aislamiento para herramientas."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"[SAFETY WRAPPER] Fallo en herramienta: {e}\n{tb}")
            return {"status": "error", "message": str(e)}
    return wrapper


class LocalToolDispatcher:
    TELEMETRY_CACHE_TTL_SECONDS = 5.0

    def __init__(self) -> None:
        self.sandbox = ToolSandbox()
        self._tools: Dict[str, Callable[..., Any]] = {}
        self._telemetry_cache: Optional[str] = None
        self._telemetry_cache_ts = 0.0
        self._register_default_tools()
        self._register_custom_tools()

    def register_tool(self, name: str, func: Callable[..., Any]) -> None:
        self._tools[name] = func

    def has_tool(self, name: str) -> bool:
        """Usado por custom_tools.py para chequear colisión de nombres en caliente."""
        return name in self._tools

    def _register_custom_tools(self) -> None:
        """
        BLINDAJE (extensión del motor de herramientas, pedida
        explícitamente: "extender el motor de herramientas" sin tocar
        Python): carga herramientas definidas por el usuario en archivos
        JSON dentro de custom_tools/ (al lado de la app) y las registra
        igual que las herramientas integradas de arriba — mismo
        dispatcher, mismo execute(), mismo TOOLS_SCHEMA que ve el
        modelo. Ver custom_tools.py para el formato del JSON y las
        validaciones de seguridad (los valores de los parámetros
        SIEMPRE terminan ejecutándose vía self.sandbox.run_cmd_safely()
        — nunca un camino de ejecución nuevo).

        Import DIFERIDO (no al principio de este archivo) a propósito:
        evita que tools.py dependa de custom_tools.py con un import
        circular — custom_tools.py sí puede importar cosas de tools.py
        (incluido TOOLS_SCHEMA) porque, para cuando esta función se
        ejecuta (construcción de una instancia, no import del módulo),
        tools.py ya terminó de definirse por completo.

        Nunca debe impedir que la app arranque: un custom_tools/
        ausente, vacío, o con JSONs inválidos simplemente no registra
        nada (con warnings ya logueados por custom_tools.py) — nunca una
        excepción sin atrapar que tire abajo LocalToolDispatcher().
        """
        try:
            from custom_tools import register_custom_tools
            count = register_custom_tools(self)
            if count:
                logger.info("[custom_tools] %d herramienta(s) personalizada(s) registrada(s).", count)
        except Exception as exc:
            logger.warning("[custom_tools] No se pudieron cargar herramientas personalizadas: %s", exc)

    def _register_default_tools(self) -> None:
        self.register_tool("list_dir", self._tool_list_dir)
        self.register_tool("system_telemetry", self._tool_system_telemetry)

        if DEV_MODE:
            self.register_tool("run_cmd", lambda command: self.sandbox.run_cmd_safely(command))
            self.register_tool("read_file", lambda path: self.sandbox.read_file_safely(path))
            self.register_tool("write_file", lambda path, content="": self.sandbox.write_file_safely(path, content))
            self.register_tool(
                "edit_file",
                lambda path, old_str="", new_str="", edits=None: self.sandbox.edit_file_safely(
                    path, old_str, new_str, edits
                ),
            )

    def _tool_list_dir(self, path: str = ".") -> str:
        """
        BLINDAJE (bug real, reportado 2026-09-02: "dime la estructura
        actual del workspace" respondía "el workspace actual parece
        estar vacío" con la carpeta llena de archivos, confirmado en el
        Explorador de Windows): a diferencia de `read_file`/`write_file`/
        `run_cmd`, esta herramienta NUNCA pasaba por `self.sandbox` —
        resolvía `path` directo con `Path(path).resolve()`, así que el
        valor por defecto "." resolvía contra el directorio de trabajo
        del PROCESO (la carpeta de instalación de la app), sin ninguna
        relación con la carpeta que el usuario tiene agregada en el
        panel "Workspaces" de la UI. Además quedaba sin el límite de
        seguridad que sí protegía a las otras tres herramientas (podía
        listar cualquier ruta absoluta del disco).

        Se unifica con `self.sandbox.validate_path()` — la misma raíz
        que ahora se mantiene sincronizada con la UI vía
        `ToolSandbox.set_root()` (ver sovnode_qt.py:
        _sync_tool_sandbox_root) — así "listar/analizar el workspace
        actual" siempre refleja la carpeta realmente activa, y de paso
        `list_dir` queda sujeto al mismo límite de seguridad que las
        demás herramientas.
        """
        try:
            target = self.sandbox.validate_path(path)
            if not target.exists():
                return f"Error: La ruta '{path}' no existe."
            items = os.listdir(target)
            return json.dumps({"directory": str(target), "items": items}, indent=2)
        except PermissionError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            return f"Error al leer directorio: {exc}"

    def _tool_system_telemetry(self) -> str:
        """
        Envoltorio con caché TTL sobre `_compute_system_telemetry()` — la
        telemetría real (con psutil) o su respaldo por PowerShell no se
        recalcula si ya se pidió hace menos de TELEMETRY_CACHE_TTL_SECONDS.
        """
        now = time.monotonic()
        if (
            self._telemetry_cache is not None
            and (now - self._telemetry_cache_ts) < self.TELEMETRY_CACHE_TTL_SECONDS
        ):
            return self._telemetry_cache

        result = self._compute_system_telemetry()
        self._telemetry_cache = result
        self._telemetry_cache_ts = now
        return result

    def _compute_system_telemetry(self) -> str:
        try:
            if psutil:
                cpu_usage = psutil.cpu_percent(interval=0.3)
                mem = psutil.virtual_memory()
                top_processes = []
                for proc in sorted(
                    psutil.process_iter(["pid", "name", "memory_percent"]),
                    key=lambda p: p.info["memory_percent"] or 0,
                    reverse=True,
                )[:5]:
                    name = proc.info["name"]
                    pid = proc.info["pid"]
                    ram_pct = proc.info["memory_percent"] or 0
                    top_processes.append(f"- PID {pid}: {name} ({ram_pct:.1f}% RAM)")

                return (
                    f"USO DE CPU: {cpu_usage}%\n"
                    f"USO DE MEMORIA RAM: {mem.percent}% ({mem.used // (1024**2)} MB / {mem.total // (1024**2)} MB)\n\n"
                    f"TOP 5 PROCESOS (POR CONSUMO DE RAM):\n" + "\n".join(top_processes)
                )
            else:
                cmd = "Get-CimInstance Win32_OperatingSystem | Select-Object FreePhysicalMemory, TotalVisibleMemorySize"
                # MODO BENCHMARK: sin límite de tiempo, igual que run_cmd_safely.
                # BLINDAJE (misma clase de bug real que sys_optimizer.py —
                # ver el comentario junto a su CREATE_NO_WINDOW: sin
                # `creationflags`, este spawn también destella una consola
                # de PowerShell visible en un build empaquetado con
                # console=False): esta rama solo corre cuando psutil no
                # está disponible, pero es la misma clase de fuga cada vez
                # que se dispara.
                res = subprocess.run(
                    ["powershell", "-Command", cmd],
                    capture_output=True,
                    text=True,
                    timeout=None,
                    creationflags=_CREATE_NO_WINDOW,
                )
                return res.stdout.strip() if res.returncode == 0 else "Telemetría no disponible."
        except Exception as exc:
            return f"Error al obtener telemetría: {exc}"

    def execute(self, tool_name: str, **kwargs: Any) -> Any:
        if tool_name not in self._tools:
            return {"status": "error", "message": f"Herramienta '{tool_name}' no disponible."}
        
        safe_func = safe_tool_execution(self._tools[tool_name])
        return safe_func(**kwargs)


TOOLS_SCHEMA = [
    {
        "name": "run_cmd",
        "description": "Ejecuta un comando en la consola del sistema operativo (Terminal / PowerShell).",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Comando exacto a ejecutar."}
            },
            "required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "Lee el contenido de un archivo de texto o código en el disco local.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Ruta relativa o absoluta del archivo."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Escribe o sobrescribe contenido en un archivo local.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Ruta del archivo a escribir."},
                "content": {"type": "string", "description": "Texto o código a guardar."}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": (
            "Reemplaza texto exacto en un archivo YA EXISTENTE. Preferi esto "
            "sobre write_file para modificar un archivo que ya existe. Dos "
            "formas: (1) un solo cambio: old_str/new_str (old_str debe ser "
            "unico en el archivo); (2) VARIOS cambios en un mismo llamado: "
            "'edits', una lista de objetos {old_str, new_str} que se aplican "
            "en orden -- preferi esta forma cuando hay que tocar mas de un "
            "lugar del archivo en la misma edicion, en vez de reescribirlo "
            "entero con write_file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Ruta del archivo a editar."},
                "old_str": {"type": "string", "description": "Fragmento EXACTO a reemplazar (forma de un solo cambio); debe ser unico en el archivo."},
                "new_str": {"type": "string", "description": "Texto de reemplazo (forma de un solo cambio)."},
                "edits": {
                    "type": "array",
                    "description": "Forma de MULTIPLES cambios: lista de {old_str, new_str}, aplicados en orden. Usar esto O old_str/new_str, no ambos.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_str": {"type": "string", "description": "Fragmento EXACTO a reemplazar; debe ser unico en el archivo en el momento de aplicarse."},
                            "new_str": {"type": "string", "description": "Texto de reemplazo."}
                        },
                        "required": ["old_str", "new_str"]
                    }
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_dir",
        "description": "Lista los archivos y directorios en una ruta específica.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Ruta del directorio a listar."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "system_telemetry",
        "description": "Obtiene la telemetría actual del sistema (CPU, RAM, disco, procesos).",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        # BLINDAJE (2026-09-19, patch_orchestrator104 -- ver el BLINDAJE
        # largo junto a "web_search" en CLOUD_TOOLS_SCHEMA,
        # orchestrator.py): esta entrada solo describe el schema para el
        # protocolo JSON-en-texto que usa el motor Local (lo que
        # `extract_tool_call` parsea) -- el CUERPO real de la ejecución
        # NO vive en `LocalToolDispatcher`/`self.tools.execute()` como
        # las demás (no necesita ni sandbox de disco ni de comandos):
        # `Orchestrator.execute_tool_from_call` intercepta
        # `tool_name == "web_search"` ANTES de llegar a
        # `self.tools.execute(...)` y llama directo a
        # `search_web_context` (web_search.py). Si algún día se llega a
        # invocar igual `self.tools.execute("web_search", ...)` (no
        # debería pasar en el flujo normal), simplemente no hay
        # herramienta registrada con ese nombre en `_tools` y devuelve el
        # error genérico de "Herramienta no disponible" -- mismo
        # comportamiento inofensivo que cualquier nombre no registrado.
        "name": "web_search",
        "description": (
            "Busca informacion actual/en tiempo real en internet -- precios, "
            "resultados deportivos, noticias, la version mas reciente de algo, "
            "o cualquier dato puntual del que no estes seguro y que pueda haber "
            "cambiado. NO la uses para pedidos de codigo/edicion de archivos "
            "(ni siquiera si mencionan 'juego'/'game' -- eso sigue siendo "
            "SIEMPRE un pedido de edicion), ni para matematica o preguntas "
            "sobre el workspace del usuario."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Consulta de busqueda concreta y autocontenida (resolvé vos mismo cualquier pronombre antes de llamar).",
                }
            },
            "required": ["query"]
        }
    }
]