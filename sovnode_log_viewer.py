#!/usr/bin/env python3
# Copyright (c) 2026 Stephen Díaz
# SovNode - Local Desktop AI Application
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""
sovnode_log_viewer.py

ES: Visor de logs EN VIVO para SovNode, standalone -- no se importa
desde la app y no toca ningún archivo del proyecto. Sigue ("tail -f")
el archivo que `sovnode_qt.py::_configure_logging()` empieza a escribir
desde el arranque de la app (ver el docstring de ese método para el
porqué del fix: antes de esa corrección no existía NINGÚN handler de
logging configurado en el punto de arranque real -- `app.py` sí tenía
uno, pero es el prototipo Streamlit muerto que nadie lanza -- así que
esta información, aunque siempre viajó en cada registro de logging
(`record.name`, `record.funcName`, `record.lineno`), nunca llegaba a
ningún lado).

Colorea cada línea por NIVEL (INFO/WARNING/ERROR/...) y por MÓDULO de
origen (un color estable por módulo, calculado por hash -- así
"SovNode.DynamicToolEngine" siempre se ve del mismo color de una
corrida a otra), y permite filtrar en vivo con --module / --level.
Los tracebacks multilínea (logger.exception, logger.error con
exc_info=True) se agrupan visualmente con la línea que los originó, en
vez de perderse como líneas sueltas sin contexto.

USO:
    python sovnode_log_viewer.py
    python sovnode_log_viewer.py --module DynamicToolEngine
    python sovnode_log_viewer.py --module Orchestrator,Router,UI
    python sovnode_log_viewer.py --level WARNING
    python sovnode_log_viewer.py --file C:\\ruta\\a\\sovnode_debug.log

Requiere `rich` (pip install rich). Corré esto en una terminal APARTE,
en paralelo a la app -- es de solo lectura sobre el archivo de log, así
que no hay ninguna forma en que este visor pueda romper algo de la app
real.

EN: Standalone LIVE log viewer for SovNode -- not imported by the app,
touches no project file. Follows ("tail -f") the file that
`sovnode_qt.py::_configure_logging()` starts writing at app startup
(see that method's docstring for why: before that fix no logging
handler was ever configured at the real entry point -- `app.py` does
have one, but it's the dead Streamlit prototype nobody launches -- so
this information, though it always traveled on every log record
(`record.name`, `record.funcName`, `record.lineno`), never reached
anywhere).

Colors each line by LEVEL and by origin MODULE (a stable per-module
color via hashing, so "SovNode.DynamicToolEngine" always looks the same
color run to run), and supports live filtering with --module / --level.
Multi-line tracebacks are visually grouped with the line that triggered
them, instead of getting lost as context-free stray lines.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import zlib
from pathlib import Path
from typing import Optional

try:
    from rich.console import Console
    from rich.text import Text
except ImportError:
    print(
        "Falta la librería 'rich'. Instalala con:\n"
        "    pip install rich\n"
        "y volvé a correr este script.",
        file=sys.stderr,
    )
    raise SystemExit(1)


DEFAULT_LOG_FILE = "sovnode_debug.log"

# Mismo formato que arma _configure_logging() en sovnode_qt.py:
#   "%(asctime)s [%(levelname)s] %(name)s:%(funcName)s:%(lineno)d — %(message)s"
_LINE_RE = re.compile(
    r"^(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"\[(?P<level>[A-Z]+)\]\s+"
    # `func` acepta también '<...>' -- Python usa nombres como
    # '<module>', '<lambda>', '<listcomp>', '<genexpr>' como funcName
    # cuando el log se llama fuera de una función con nombre. Probado
    # en vivo: sin esto, CUALQUIER logger.info/warning/error llamado a
    # nivel de módulo (fuera de una función) no matcheaba nunca y caía
    # como "continuación sin formato", perdiendo color y atribución de
    # módulo -- justo la información que este visor existe para mostrar.
    r"(?P<name>[\w.]+):(?P<func>[\w<>]+):(?P<line>\d+)\s+—\s+"
    r"(?P<msg>.*)$"
)

_LEVEL_STYLES = {
    "DEBUG": "dim white",
    "INFO": "bright_cyan",
    "WARNING": "bold yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}

# Paleta de colores para módulos -- se elige uno por hash estable del
# nombre completo (SovNode.<Módulo>), así el mismo módulo siempre queda
# con el mismo color, corrida tras corrida (no depende del orden de
# aparición en esta ejecución particular).
_MODULE_PALETTE = [
    "cyan", "magenta", "green", "blue", "bright_magenta",
    "bright_green", "bright_blue", "orange3", "turquoise2", "purple",
    "spring_green3", "deep_pink3", "gold3", "medium_purple1",
]


def _module_color(name: str) -> str:
    digest = zlib.crc32(name.encode("utf-8"))
    return _MODULE_PALETTE[digest % len(_MODULE_PALETTE)]


def _short_module(name: str) -> str:
    # "SovNode.DynamicToolEngine" -> "DynamicToolEngine"
    return name.rsplit(".", 1)[-1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visor de logs en vivo para SovNode (tail -f coloreado por módulo/nivel)."
    )
    parser.add_argument(
        "--file", default=DEFAULT_LOG_FILE,
        help=f"Ruta al archivo de log (default: {DEFAULT_LOG_FILE}, en el directorio de trabajo de la app).",
    )
    parser.add_argument(
        "--module", default=None,
        help="Filtrar por módulo(s), separados por coma (ej: DynamicToolEngine,Orchestrator). "
             "Coincide con el nombre corto (sin el prefijo 'SovNode.'). Sin este flag, muestra todo.",
    )
    parser.add_argument(
        "--level", default="DEBUG",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Nivel mínimo a mostrar (default: DEBUG, es decir todo lo que el archivo tenga).",
    )
    return parser.parse_args()


def _follow(path: Path):
    """
    ES: Generador tipo `tail -f`, robusto a la ROTACIÓN del
    RotatingFileHandler (cuando el archivo supera su tamaño máximo,
    logging lo renombra a .1 y abre uno nuevo vacío en la misma ruta --
    si no se detecta esto, el visor se queda mirando un archivo viejo
    que ya no recibe escrituras). Se detecta comparando el tamaño actual
    contra la última posición leída: si el tamaño es MENOR, el archivo
    fue rotado o truncado y hay que reabrir desde el principio.

    EN: `tail -f`-style generator, robust to RotatingFileHandler
    ROTATION (when the file exceeds its max size, logging renames it to
    .1 and opens a fresh empty one at the same path -- without
    detecting this, the viewer keeps watching a stale file that no
    longer receives writes). Detected by comparing the current size
    against the last read position: if the size is SMALLER, the file
    was rotated or truncated and must be reopened from the start.
    """
    pos = 0
    warned_missing = False
    while True:
        try:
            size = path.stat().st_size
        except OSError:
            if not warned_missing:
                yield None, (
                    f"(esperando a que la app cree '{path}' -- "
                    "asegurate de que SovNode esté corriendo)"
                )
                warned_missing = True
            time.sleep(0.5)
            continue
        warned_missing = False

        if size < pos:
            pos = 0  # rotado o truncado -- reabrir desde el principio

        if size > pos:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(pos)
                for raw_line in handle:
                    yield raw_line.rstrip("\n"), None
                pos = handle.tell()
        else:
            time.sleep(0.3)


_LEVEL_ORDER = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def main() -> int:
    args = _parse_args()
    console = Console()

    module_filter: Optional[set[str]] = None
    if args.module:
        module_filter = {m.strip() for m in args.module.split(",") if m.strip()}

    min_level_idx = _LEVEL_ORDER.index(args.level)

    log_path = Path(args.file)
    console.print(
        f"[bold]SovNode — visor de logs en vivo[/bold]  "
        f"(archivo: {log_path}"
        + (f", módulos: {', '.join(sorted(module_filter))}" if module_filter else "")
        + f", nivel mínimo: {args.level})",
        style="bright_black",
    )
    console.print("Ctrl+C para salir.\n", style="bright_black")

    last_shown = False  # si la última línea pasó los filtros (para agrupar continuaciones de traceback)

    try:
        for raw_line, status_msg in _follow(log_path):
            if status_msg is not None:
                console.print(status_msg, style="italic bright_black")
                continue
            if not raw_line.strip():
                continue

            match = _LINE_RE.match(raw_line)
            if match is None:
                # Continuación de un traceback / línea sin el formato
                # esperado: se agrupa con lo último mostrado en vez de
                # descartarla -- suele ser la parte más útil para saber
                # "de dónde vino" un bug real.
                if last_shown:
                    console.print(f"    {raw_line}", style="dim red")
                continue

            level = match.group("level")
            if level not in _LEVEL_ORDER or _LEVEL_ORDER.index(level) < min_level_idx:
                last_shown = False
                continue

            module_name = match.group("name")
            short_name = _short_module(module_name)
            if module_filter and short_name not in module_filter:
                last_shown = False
                continue

            line = Text()
            line.append(f"{match.group('time')} ", style="bright_black")
            line.append(f"[{level:<8}] ", style=_LEVEL_STYLES.get(level, "white"))
            line.append(f"{short_name}", style=f"bold {_module_color(module_name)}")
            line.append(
                f":{match.group('func')}:{match.group('line')}  ",
                style=_module_color(module_name),
            )
            line.append(match.group("msg"))
            console.print(line)
            last_shown = True

    except KeyboardInterrupt:
        console.print("\nVisor detenido.", style="bright_black")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
