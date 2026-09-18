#!/usr/bin/env python3
# Copyright (c) 2026 Stephen Díaz
# SovNode - Local Desktop AI Application
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""
sovnode_wal_monitor.py

ES: Visor EN VIVO del WAL (`sovnode.wal`) para SovNode, standalone --
mismo espíritu que `sovnode_log_viewer.py` (rich, de solo lectura, se
corre en una terminal aparte mientras la app sigue viva, no importa
ningún módulo del proyecto y no puede romper nada real) pero mirando una
fuente distinta: en vez de líneas de `logging` sueltas, el WAL trae
eventos ESTRUCTURADOS por turno (`turn_id`), que es exactamente lo que
hace falta para responder "¿qué pasó en este turno, y qué falló?" sin
tener que pedir un dump del WAL y leerlo a mano cada vez.

Agrupa los eventos por turno y los muestra en vivo, en orden, bajo un
encabezado por turno: entrada del usuario -> ruteo (fast/slow path,
modelo) -> presupuesto de codegen si aplica -> cada tool_call/tool_result
-> cualquier red de seguridad que haya disparado (circuit-breakers,
rescates, bloqueos de falso-éxito, etc. -- ver PROBLEM_PHASES más abajo,
curada a mano desde cada `self._wal_phase(...)` real de
`orchestrator.py`) -- resaltada en rojo/amarillo, sin importar el resto
del ruido -- y por último la respuesta final con su `outcome`. Cierra
cada turno con una línea de resumen: cuántas red de seguridad dispararon,
si alguna.

LIMITACIÓN CONOCIDA, a propósito no ocultada: el costo real en USD de
cada llamada Cloud NO viaja por el WAL hoy -- `_account_cloud_usage` solo
lo anuncia por `log_cb` (que termina en la consola gráfica de la app,
`_terminal_log`, nunca en un archivo) y lo acumula en memoria
(`cloud_usage_totals`, se pierde al cerrar la app). Este visor sí muestra
el TECHO de presupuesto en tokens (`codegen_budget_plan`), que es la
mejor aproximación disponible sin tocar `_call_claude_api_raw` -- la
única función que factura contra la API real, deliberadamente no tocada
acá. Si en algún momento se quiere costo en vivo también, hace falta un
`_wal_phase(turn_id, "cloud_cost", ...)` nuevo en `_account_cloud_usage`
-- una sola línea, pero toca esa función, así que se dejó para cuando se
pida explícitamente.

USO:
    python sovnode_wal_monitor.py
    python sovnode_wal_monitor.py --file C:\\ruta\\a\\sovnode.wal
    python sovnode_wal_monitor.py --only-problems
    python sovnode_wal_monitor.py --turn 3f9a
    python sovnode_wal_monitor.py --history 10
    python sovnode_wal_monitor.py --raw

Requiere `rich` (pip install rich). Corré esto en una terminal APARTE, en
paralelo a la app.

EN: Standalone LIVE viewer for SovNode's WAL (`sovnode.wal`) -- same
spirit as `sovnode_log_viewer.py` (rich, read-only, runs in a separate
terminal, imports no project module, cannot break anything real) but
watching a different source: instead of loose `logging` lines, the WAL
carries STRUCTURED per-turn events (`turn_id`), which is exactly what's
needed to answer "what happened in this turn, and what failed?" without
asking for a WAL dump and reading it by hand every time.

Groups events by turn and shows them live, in order, under a per-turn
header. Known limitation, not hidden on purpose: real USD cost per call
does not travel through the WAL today (see the long comment above) --
this viewer shows the token BUDGET CEILING instead, the best available
proxy without touching the only function that bills the real API.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

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


DEFAULT_WAL_FILE = "sovnode.wal"
MAX_TRACKED_TURNS = 200  # evita crecer sin límite en una sesión muy larga

# Curado a mano, 2026-09-18, leyendo cada self._wal_phase(...) real de
# orchestrator.py: fases que significan "una red de seguridad tuvo que
# intervenir" -- el turno probablemente no salió como se esperaba a la
# primera. Si se agrega un self._wal_phase(...) nuevo en orchestrator.py
# que sea otra red de seguridad, sumarlo acá a mano (no hay forma
# automática de saberlo desde afuera del código real).
PROBLEM_PHASES = {
    "codegen_budget_infeasible",
    "codegen_budget_infeasible_bailout",
    "fastpath_circuit_breaker",
    "slowpath_circuit_breaker",
    "file_op_false_success_blocked",
    "file_op_stub_guard",
    "file_op_stub_guard_inloop",
    "file_op_stub_rescued_from_prose_inloop",
    "tool_loop_aborted",
    "file_write_ceiling_regen",
    "file_write_ceiling_regen_inloop",
    "file_write_ceiling_local_continuation",
    "file_write_ceiling_local_continuation_inloop",
    "edit_file_ceiling_local_rescue",
    "edit_file_ceiling_local_rescue_inloop",
    "code_syntax_fix_call_failed",
    "code_syntax_fix_empty_result",
    "code_syntax_fix_applied",
    "post_correction_fallback",
    "content_audit_fragile",
    "trailing_tool_call_echo_stripped",
    "file_op_salvage",
}

# Mecanismos que corrigieron algo en silencio -- no son necesariamente un
# bug, pero vale la pena verlos resaltados aparte (ver ARCHITECTURE.md
# §10 sobre el riesgo de fondo de `correction_pair` sobre código).
CAUTION_PHASES = {"correction_pair", "file_write_model_override"}


def _trunc(value: Any, limit: int = 90) -> str:
    text = str(value).replace("\n", " ⏎ ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _short_turn(turn_id: Optional[str]) -> str:
    if not turn_id:
        return "??????"
    return str(turn_id)[:8]


class _Turn:
    def __init__(self, turn_id: str, prompt: str, start_seq: int) -> None:
        self.turn_id = turn_id
        self.prompt = prompt
        self.start_seq = start_seq
        self.tool_calls = 0
        self.problem_phases: list[str] = []
        self.closed = False


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visor en vivo del WAL de SovNode -- qué pasó (y qué falló) en cada turno."
    )
    parser.add_argument(
        "--file", default=DEFAULT_WAL_FILE,
        help=f"Ruta al WAL (default: {DEFAULT_WAL_FILE}, en el directorio de trabajo de la app).",
    )
    parser.add_argument(
        "--history", type=int, default=3,
        help="Cuántos turnos YA COMPLETADOS mostrar antes de empezar a seguir en vivo (default: 3, 0 para arrancar en blanco).",
    )
    parser.add_argument(
        "--turn", default=None,
        help="Mostrar solo turnos cuyo turn_id contenga este texto (útil para seguir un turno puntual).",
    )
    parser.add_argument(
        "--only-problems", action="store_true",
        help="Ocultar turnos que terminaron sin disparar ninguna red de seguridad (PROBLEM_PHASES) -- para dejarlo corriendo de fondo y que solo hable cuando algo falla.",
    )
    parser.add_argument(
        "--raw", action="store_true",
        help="Además de la línea resumida, imprimir el payload JSON completo de cada evento (para cuando el resumen no alcanza).",
    )
    return parser.parse_args()


def _follow(path: Path, start_pos: int = 0):
    """
    ES: Generador tail -f robusto a truncado/reinicio (mismo patrón que
    sovnode_log_viewer.py). `start_pos` es DELIBERADAMENTE el tamaño
    actual del archivo por default en `main()` (no 0): este visor ya
    reconstruye el historial reciente con `preload_history` leyendo el
    archivo entero una vez -- si `_follow` también arrancara desde el
    principio, repetiría en "modo vivo" cada línea que el historial ya
    mostró, potencialmente miles de líneas de un WAL que puede pesar
    varios MB. Arrancar en el tamaño actual del archivo es lo que hace
    que "vivo" signifique de verdad "lo nuevo a partir de ahora".

    EN: tail -f generator, robust to truncation/rotation. `start_pos`
    deliberately defaults to the file's CURRENT size in `main()`, not 0:
    `preload_history` already replays recent history by reading the
    whole file once -- if `_follow` also started at 0 it would replay
    every line again under "live" mode, potentially thousands of lines
    on a multi-MB WAL. Starting at the current size is what makes "live"
    actually mean "new from now on".
    """
    pos = start_pos
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
            pos = 0  # truncado/reiniciado -- releer desde el principio

        if size > pos:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(pos)
                for raw_line in handle:
                    yield raw_line.rstrip("\n"), None
                pos = handle.tell()
        else:
            time.sleep(0.3)


class Monitor:
    def __init__(self, console: Console, turn_filter: Optional[str], only_problems: bool, raw: bool) -> None:
        self.console = console
        self.turn_filter = turn_filter
        self.only_problems = only_problems
        self.raw = raw
        self.turns: "OrderedDict[str, _Turn]" = OrderedDict()
        # Turnos ya cerrados y con --only-problems activo: se bufferean
        # sus líneas hasta saber si hubo problema o no, para no mostrar
        # (y después tener que "retractarse" de) un turno limpio.
        self._pending_lines: dict[str, list[Text]] = {}

    def _turn_visible(self, turn_id: str) -> bool:
        if self.turn_filter and self.turn_filter not in turn_id:
            return False
        return True

    def _emit(self, turn_id: str, line: Text) -> None:
        if not self._turn_visible(turn_id):
            return
        if self.only_problems:
            self._pending_lines.setdefault(turn_id, []).append(line)
        else:
            self.console.print(line)

    def _flush_turn(self, turn: _Turn) -> None:
        if not self.only_problems:
            return
        buffered = self._pending_lines.pop(turn.turn_id, [])
        if turn.problem_phases:
            for line in buffered:
                self.console.print(line)
        # turno limpio con --only-problems: se descarta en silencio.

    def handle_line(self, raw_line: str) -> None:
        raw_line = raw_line.strip()
        if not raw_line:
            return
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            return

        event_type = record.get("event_type")
        payload = record.get("payload") or {}
        timestamp = str(record.get("timestamp", ""))[-8:]  # HH:MM:SS
        turn_id = payload.get("turn_id")

        if event_type == "user_input" and turn_id:
            turn = _Turn(turn_id, str(payload.get("prompt", "")), record.get("sequence", 0))
            self.turns[turn_id] = turn
            while len(self.turns) > MAX_TRACKED_TURNS:
                self.turns.popitem(last=False)
            header = Text()
            header.append(f"\n{timestamp} ", style="bright_black")
            header.append(f"┌─ turno {_short_turn(turn_id)} ", style="bold cyan")
            header.append(f'"{_trunc(turn.prompt, 100)}"', style="white")
            self._emit(turn_id, header)
            if self.raw:
                self._emit(turn_id, Text(f"     {json.dumps(payload, ensure_ascii=False)}", style="dim"))
            return

        if not turn_id or turn_id not in self.turns:
            # Evento sin turno abierto conocido (arrancamos el visor a
            # mitad de un turno, o un evento de fondo sin turn_id) -- se
            # ignora para no mostrar líneas huérfanas sin contexto.
            return

        turn = self.turns[turn_id]

        if event_type == "turn_phase":
            phase = str(payload.get("phase", "?"))
            details = {k: v for k, v in payload.items() if k not in ("turn_id", "phase")}
            self._render_phase(turn, timestamp, phase, details)
            if self.raw:
                self._emit(turn_id, Text(f"     {json.dumps(payload, ensure_ascii=False)}", style="dim"))
            return

        if event_type == "response":
            self._render_close(turn, timestamp, str(payload.get("response", "")), payload.get("outcome"))
            turn.closed = True
            self._flush_turn(turn)
            return

    def _render_phase(self, turn: _Turn, timestamp: str, phase: str, details: dict[str, Any]) -> None:
        line = Text()
        line.append(f"{timestamp} ", style="bright_black")

        if phase in PROBLEM_PHASES:
            turn.problem_phases.append(phase)
            line.append("│  ⚠ ", style="bold red")
            line.append(f"{phase}", style="bold red")
            extra = self._format_details(phase, details)
            if extra:
                line.append(f"  {extra}", style="red")
            self._emit(turn.turn_id, line)
            return

        if phase in CAUTION_PHASES:
            line.append("│  ◆ ", style="bold yellow")
            line.append(f"{phase}", style="bold yellow")
            extra = self._format_details(phase, details)
            if extra:
                line.append(f"  {extra}", style="yellow")
            self._emit(turn.turn_id, line)
            return

        if phase == "routed":
            line.append("│  → ", style="bright_black")
            line.append(f"ruteo: {details.get('path', '?')}", style="bold magenta")
            line.append(f"  modelo={details.get('model', '?')}", style="magenta")
            self._emit(turn.turn_id, line)
            return

        if phase == "codegen_budget_plan":
            line.append("│  ⛁ ", style="bright_black")
            line.append("presupuesto de codegen", style="bold blue")
            line.append(
                f"  categoría={details.get('category', '?')} "
                f"techo={details.get('hard_ceiling', '?')}tok "
                f"piso={details.get('min_viable_floor', '?')}tok "
                f"objetivo={details.get('soft_target', '?')}tok "
                f"tope_entidades={details.get('entity_cap', '?')}",
                style="blue",
            )
            self._emit(turn.turn_id, line)
            return

        if phase in ("tool_call", "tool_result"):
            turn.tool_calls += 1 if phase == "tool_call" else 0
            arrow = "▶" if phase == "tool_call" else "◀"
            style = "bold green" if phase == "tool_call" else "green"
            line.append(f"│  {arrow} ", style="bright_black")
            tool = details.get("tool", "?")
            if phase == "tool_call":
                line.append(f"{tool}", style=style)
                line.append(f"  (pasada {details.get('pass_number', '?')})", style="dim green")
            else:
                line.append(f"{tool} →", style=style)
                line.append(f" {details.get('chars', '?')} caracteres", style="dim green")
            self._emit(turn.turn_id, line)
            return

        # Cualquier otra fase (generation_done, semantic_cache_hit, etc.):
        # una línea genérica, sin ocultar nada, solo sin formato especial.
        line.append("│  · ", style="bright_black")
        line.append(f"{phase}", style="cyan")
        extra = self._format_details(phase, details)
        if extra:
            line.append(f"  {extra}", style="dim cyan")
        self._emit(turn.turn_id, line)

    def _format_details(self, phase: str, details: dict[str, Any]) -> str:
        if phase == "correction_pair":
            original = _trunc(details.get("original", ""), 60)
            corrected = _trunc(details.get("corrected", ""), 60)
            return f"[{details.get('pair_type', '?')}] antes=\"{original}\" → después=\"{corrected}\""
        parts = []
        for key, value in details.items():
            if key in ("original", "corrected", "stats"):
                continue
            parts.append(f"{key}={_trunc(value, 50)}")
        return " ".join(parts)

    def _render_close(self, turn: _Turn, timestamp: str, response: str, outcome: Optional[str]) -> None:
        is_error = outcome == "error" or bool(turn.problem_phases)
        style = "bold red" if outcome == "error" else ("bold yellow" if turn.problem_phases else "bold green")

        line = Text()
        line.append(f"{timestamp} ", style="bright_black")
        line.append("└─ respuesta ", style="bright_black")
        line.append(f"[outcome={outcome or '?'}]", style=style)
        self._emit(turn.turn_id, line)

        preview = Text()
        preview.append("     ", style="bright_black")
        preview.append(f'"{_trunc(response, 140)}"', style="white" if not is_error else "red")
        self._emit(turn.turn_id, preview)

        summary = Text()
        summary.append("     ", style="bright_black")
        if turn.problem_phases:
            summary.append(
                f"⚠ {len(turn.problem_phases)} red(es) de seguridad disparó/dispararon: "
                f"{', '.join(turn.problem_phases)}",
                style="bold red",
            )
        else:
            summary.append(f"✓ sin problemas detectados · {turn.tool_calls} tool_call(s)", style="dim green")
        self._emit(turn.turn_id, summary)

    def preload_history(self, path: Path, count: int) -> None:
        if count <= 0 or not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            return

        # Encontrar los últimos `count` turn_id con un evento "response"
        # (turno completo), preservando el orden de aparición.
        completed_ids: list[str] = []
        seen: set[str] = set()
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if record.get("event_type") == "response":
                tid = (record.get("payload") or {}).get("turn_id")
                if tid and tid not in seen:
                    seen.add(tid)
                    completed_ids.append(tid)
                    if len(completed_ids) >= count:
                        break
        wanted = set(completed_ids)
        if not wanted:
            return

        self.console.print(
            f"[bright_black]── historial: últimos {len(wanted)} turno(s) completados ──[/bright_black]"
        )
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            tid = (record.get("payload") or {}).get("turn_id")
            if tid in wanted:
                self.handle_line(raw)
        self.console.print("[bright_black]── fin del historial, siguiendo en vivo ──[/bright_black]")


def main() -> int:
    args = _parse_args()
    console = Console()

    wal_path = Path(args.file)
    console.print(
        f"[bold]SovNode — visor de WAL en vivo[/bold]  "
        f"(archivo: {wal_path}"
        + (f", turno: {args.turn}" if args.turn else "")
        + (", solo problemas" if args.only_problems else "")
        + ")",
        style="bright_black",
    )
    console.print(
        "Costo real en USD no viaja por el WAL todavía -- se muestra el techo de "
        "presupuesto en tokens (ver docstring del script). Ctrl+C para salir.\n",
        style="italic bright_black",
    )

    monitor = Monitor(console, turn_filter=args.turn, only_problems=args.only_problems, raw=args.raw)
    monitor.preload_history(wal_path, args.history)

    try:
        start_pos = wal_path.stat().st_size
    except OSError:
        start_pos = 0

    try:
        for raw_line, status_msg in _follow(wal_path, start_pos=start_pos):
            if status_msg is not None:
                console.print(status_msg, style="italic bright_black")
                continue
            monitor.handle_line(raw_line)
    except KeyboardInterrupt:
        console.print("\nVisor detenido.", style="bright_black")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
