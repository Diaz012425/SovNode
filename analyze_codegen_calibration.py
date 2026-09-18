#!/usr/bin/env python3
"""
analyze_codegen_calibration.py

ES: Script standalone (no forma parte de la app, no se importa desde
orchestrator.py) para cerrar de verdad -- con datos reales, no con otra
suposición -- el pendiente de calibración documentado en ARCHITECTURE.md
§10 sobre las constantes del negociador de alcance de codegen:

    - _MIN_VIABLE_FLOOR_PATTERNS (piso de andamiaje por categoría:
      juego=450, gui=400, servidor=300, default=120)
    - _CODEGEN_EXTRA_TOKENS_PER_ENTITY = 150 / _CODEGEN_MAX_ENTITY_HINT = 3
    - _CODEGEN_SOFT_MARGIN_FRACTION = 0.15

Desde el 2026-09-16, cada turno de codegen que pasa por el negociador de
alcance escribe un evento "codegen_budget_plan" al WAL (ver
`_wal_phase(turn_id, "codegen_budget_plan", ...)` en orchestrator.py, cerca
de `_estimate_min_viable_codegen_tokens`) con el PLAN completo: categoría
detectada, techo duro, piso de andamiaje, objetivo con margen y tope de
entidades que se le comunicaron al modelo para ese turno. El WAL ya
guardaba por separado, para cada turno, el evento "response" con el texto
final real. Este script cruza ambos por turn_id y aproxima tokens reales
usados como len(response) / 3.3 (mismo factor caracteres-por-token que ya
usa orchestrator.py en el propio prompt, ver "_codegen_char_budget = int(
gen_predict * 3.3)") para poder comparar:

    - ¿el objetivo con margen (soft_target) se queda corto casi siempre
      (el modelo entrega MENOS de lo que el presupuesto permitiría -- deja
      "plata sobre la mesa") o se pasa seguido (el margen de 15% no
      alcanza)?
    - ¿el piso de andamiaje por categoría es realista, o sistemáticamente
      alto/bajo comparado con lo que un `write_file` real de esa categoría
      termina costando?
    - ¿el tope de entidades sugerido resultó en generaciones completas
      (dentro del techo, sin circuit-breaker) o se siguió cortando?

USO:
    python3 analyze_codegen_calibration.py [ruta/a/sovnode.wal]

    Sin argumento, busca "sovnode.wal" en el directorio actual (donde
    normalmente vive, junto a la instalación/lanzamiento de la app).

No modifica nada -- solo lee el WAL (JSONL, un WALEntry por línea) y
imprime un resumen. Cuantos más turnos de codegen reales se acumulen,
más confiable el resumen -- con pocos turnos, tratar los números como
orientativos, no como una recalibración definitiva.

EN: Standalone script (not imported by the app) to actually close --
with real data, not another guess -- the calibration pending item
documented in ARCHITECTURE.md §10 for the codegen scope-negotiator
constants. Joins the new "codegen_budget_plan" WAL events (written since
2026-09-16 for every codegen turn) against the existing "response" events
by turn_id, and reports actual-vs-predicted stats per category so the
constants above can eventually be tuned from real usage instead of a
first-guess.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

CHARS_PER_TOKEN = 3.3  # mismo factor que usa orchestrator.py en el prompt


def load_wal_lines(wal_path: Path):
    with open(wal_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                print(f"  (línea {line_no}: JSON inválido, se ignora)", file=sys.stderr)


def main() -> int:
    wal_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sovnode.wal")
    if not wal_path.exists():
        print(f"No se encontró el WAL en '{wal_path}'. Pasalo como argumento:")
        print("  python3 analyze_codegen_calibration.py C:\\ruta\\a\\sovnode.wal")
        return 1

    plans: dict[str, dict] = {}
    responses: dict[str, str] = {}

    for record in load_wal_lines(wal_path):
        event_type = record.get("event_type")
        payload = record.get("payload") or {}
        turn_id = payload.get("turn_id")
        if not turn_id:
            continue
        if event_type == "turn_phase" and payload.get("phase") == "codegen_budget_plan":
            plans[turn_id] = payload
        elif event_type == "response":
            responses[turn_id] = payload.get("response") or ""

    joined = []
    for turn_id, plan in plans.items():
        response = responses.get(turn_id)
        if response is None:
            continue  # turno sin respuesta registrada todavía (o descartado)
        actual_tokens_approx = len(response) / CHARS_PER_TOKEN
        joined.append((turn_id, plan, actual_tokens_approx))

    print(f"WAL: {wal_path}")
    print(f"Eventos 'codegen_budget_plan' encontrados: {len(plans)}")
    print(f"De esos, con una respuesta 'response' para cruzar: {len(joined)}\n")

    if not joined:
        print(
            "Todavía no hay datos suficientes para calibrar nada. Esto es "
            "esperable si corriste esto poco después de aplicar el fix -- "
            "volvé a correr el script más adelante, después de usar la app "
            "normalmente para generar código un tiempo."
        )
        return 0

    by_category: dict[str, list] = defaultdict(list)
    for turn_id, plan, actual in joined:
        by_category[plan.get("category", "?")].append((plan, actual))

    for category, entries in sorted(by_category.items()):
        print(f"--- Categoría: {category} ({len(entries)} turnos) ---")
        floors = [p["min_viable_floor"] for p, _ in entries]
        soft_targets = [p["soft_target"] for p, _ in entries]
        hard_ceilings = [p["hard_ceiling"] for p, _ in entries]
        actuals = [a for _, a in entries]
        ratios_vs_soft = [a / p["soft_target"] for p, a in entries if p["soft_target"]]
        ratios_vs_hard = [a / p["hard_ceiling"] for p, a in entries if p["hard_ceiling"]]

        print(f"  piso de andamiaje usado: {floors[0]} (constante fija por categoría)")
        print(
            f"  objetivo con margen (soft_target): min={min(soft_targets)} "
            f"med={median(soft_targets):.0f} max={max(soft_targets)}"
        )
        print(
            f"  techo duro (hard_ceiling): min={min(hard_ceilings)} "
            f"med={median(hard_ceilings):.0f} max={max(hard_ceilings)}"
        )
        print(
            f"  tokens reales aprox. (len(respuesta)/{CHARS_PER_TOKEN}): "
            f"min={min(actuals):.0f} med={median(actuals):.0f} "
            f"max={max(actuals):.0f} prom={mean(actuals):.0f}"
        )
        if ratios_vs_soft:
            print(
                f"  real / objetivo_con_margen: prom={mean(ratios_vs_soft):.0%} "
                "(cerca de 100% = objetivo bien calibrado; muy por debajo = "
                "deja presupuesto sin usar; por encima de 100% = el modelo "
                "sigue pasándose del objetivo con margen, aunque no del techo)"
            )
        if ratios_vs_hard:
            over_hard = sum(1 for r in ratios_vs_hard if r >= 0.98)
            print(
                f"  real / techo_duro: prom={mean(ratios_vs_hard):.0%} -- "
                f"{over_hard}/{len(ratios_vs_hard)} turnos terminaron a 98%+ "
                "del techo duro (candidatos a haberse cortado a mitad, "
                "revisar esos turn_id específicos en el WAL con más detalle)"
            )
        print()

    print(
        "Recordatorio: esto es una aproximación (chars/3.3), no el conteo "
        "exacto de tokens que factura la API -- alcanza para ver tendencias "
        "(¿el margen sobra o falta?, ¿el piso por categoría es realista?), "
        "no para ajustar la cuarta cifra decimal de una constante."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
