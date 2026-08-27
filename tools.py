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

import json
import os
import re
import subprocess
import shlex
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import time
import functools
import traceback
import logging

logger = logging.getLogger("SovNode.Tools")


# =====================================================================
# SCRATCHPAD VERIFICADOR - utilidades compartidas para <thought_code>
# =====================================================================
# Parsing y formateo del bloque <thought_code> que el modelo puede emitir
# dentro de su <thought> para pedir una verificación real en sandbox
# (cálculos, conteos, aserciones lógicas) antes de redactar la respuesta
# final. Viven aquí - no en orchestrator.py ni en sovnode_qt.py - porque
# ambos consumidores (el turno síncrono de process_turn() y el turno en
# streaming de StreamTurnWorker) necesitan el mismo parseo y el mismo
# formato de evidencia; centralizarlo evita que las dos implementaciones
# diverjan con el tiempo, igual que ya se hace con _split_thought_and_content.

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

# Nota (bucles de herramientas / saturación de contexto): antes
# read_file_safely() volcaba el archivo COMPLETO sin límite y
# run_cmd_safely() hacía lo mismo con stdout/stderr - un archivo de
# varios MB o un comando verboso (p. ej. un build log) satura la
# ventana de contexto del modelo local (3B/7B) y puede degenerar en
# llamadas recursivas a herramientas (el modelo, sin espacio para
# razonar tras ver un volcado enorme, tiende a reintentar la misma
# acción en vez de progresar). MAX_TOOL_OUTPUT_CHARS acota la salida
# de cualquier herramienta de ToolSandbox antes de que salga de este
# módulo - ver también el cap espejo en Orchestrator.execute_tool_
# from_call() (orchestrator.py), defensa en profundidad para
# herramientas que no pasan por aquí (p. ej. el motor dinámico).
MAX_TOOL_OUTPUT_CHARS = 4000


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
    "powershell -encodedcommand", "bash -c"
}

DANGEROUS_PATTERNS = [
    r"rm\s+-rf",
    r">\s*/dev/null",
    r";\s*rm",
    r"&&\s*rm",
    r"\|\s*bash",
    r"\|\s*powershell"
]

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
        self.root_dir = Path(allowed_directory or os.getcwd()).resolve()

    def validate_path(self, target_path: str) -> Path:
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
        """
        resolved = (self.root_dir / target_path).resolve()
        resolved_cmp = os.path.normcase(str(resolved))
        root_cmp = os.path.normcase(str(self.root_dir))
        if resolved_cmp != root_cmp and not resolved_cmp.startswith(root_cmp + os.sep):
            raise PermissionError(f"Acceso denegado fuera de la raíz de trabajo: {target_path}")
        return resolved

    @retry_on_failure(retries=2, delay=0.5)
    def run_cmd_safely(self, command: str, timeout_sec: int | None = None) -> str:
        cmd_lower = command.lower().strip()

        # Validaciones de seguridad pre-ejecución
        for blocked in BLOCKED_COMMANDS:
            if blocked in cmd_lower:
                return f"[SANDBOX ERROR]: Comando bloqueado por política de seguridad ('{blocked}')."

        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, cmd_lower):
                return "[SANDBOX ERROR]: Patrón de ejecución inseguro detectado."

        try:
            # Tokenización segura para evitar inyecciones a través del intérprete de comandos
            if os.name != 'nt':
                args = shlex.split(command)
                use_shell = False
            else:
                args = command
                use_shell = True  # Compatibilidad controlada en entorno Windows[cite: 33]

            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                shell=use_shell
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
            return f"[SANDBOX TIMEOUT]: La instrucción excedió el límite de {timeout_sec}s."
        except Exception as exc:
            return f"[SANDBOX EXEC ERROR]: {exc}"

    @retry_on_failure(retries=2, delay=0.5)
    def read_file_safely(self, path: str) -> str:
        try:
            target = self.validate_path(path)
            if not target.exists():
                return f"[SANDBOX ERROR]: El archivo '{path}' no existe."
            content = target.read_text(encoding="utf-8", errors="replace")
            return _truncate_tool_output(content, label=f"el archivo '{path}'")
        except Exception as exc:
            return f"[SANDBOX READ ERROR]: {exc}"

    @retry_on_failure(retries=2, delay=0.5)
    def write_file_safely(self, path: str, content: str) -> str:
        try:
            target = self.validate_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Archivo escrito exitosamente en '{target.name}' ({len(content)} bytes)."
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
    # TTL de la caché de telemetría del sistema: evita recalcular CPU/RAM/
    # procesos (o, sin psutil, relanzar subprocess.run(["powershell",...])
    # - un proceso nuevo por cada llamada) cuando el modelo invoca la
    # herramienta system_telemetry varias veces seguidas en el mismo
    # turno (p. ej. tras encadenar otra herramienta autónoma).
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

    def _tool_list_dir(self, path: str = ".") -> str:
        try:
            target = Path(path).resolve()
            if not target.exists():
                return f"Error: La ruta '{path}' no existe."
            items = os.listdir(target)
            return json.dumps({"directory": str(target), "items": items}, indent=2)
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
                res = subprocess.run(["powershell", "-Command", cmd], capture_output=True, text=True, timeout=None)
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
    }
]