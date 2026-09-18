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
El Monolito Personal / SovNode - Exportador de corpus de entrenamiento
=======================================================================

ES: Cada vez que el pipeline detecta y corrige un error del modelo local
(marcador sin respaldo, idioma equivocado, etc. — ver la verificación post-hoc
en run_turn), el par (texto original, texto corregido) queda registrado en el
WAL como un evento `correction_pair`. Este módulo NO entrena nada: recorre el
WAL, une cada `correction_pair` con el prompt original del usuario, y exporta
dos formatos JSONL listos para fine-tuning local (p. ej. trl/Unsloth/Axolotl):

  - `dpo_pairs.jsonl`: {"prompt", "chosen", "rejected", "meta"} — formato DPO
    nativo (ya tenemos, para el mismo prompt, una respuesta rechazada y una preferida).
  - `sft_pairs.jsonl`: {"prompt", "response", "meta"} — solo el lado bueno,
    en formato de fine-tuning supervisado estándar.

EN: Every time the pipeline detects and corrects a local-model error (unbacked
marker, wrong language, etc. — see the post-hoc check in run_turn), the pair
(original text, corrected text) gets logged to the WAL as a `correction_pair`
event. This module trains nothing: it walks the WAL, joins each
`correction_pair` with the user's original prompt, and exports two JSONL
formats ready for local fine-tuning (e.g. trl/Unsloth/Axolotl):

  - `dpo_pairs.jsonl`: {"prompt", "chosen", "rejected", "meta"} — native DPO
    format (we already have, for the same prompt, a rejected and a preferred answer).
  - `sft_pairs.jsonl`: {"prompt", "response", "meta"} — just the good side,
    in standard supervised fine-tuning format.

Uso:
    python training_export.py [--wal sovnode.wal] [--out training_data]

O programáticamente:
    from training_export import export_wal_to_training_data
    stats = export_wal_to_training_data("sovnode.wal", "training_data")
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger("monolith.training_export")


@dataclass(frozen=True)
class CorrectionPair:
    turn_id: str
    timestamp: str
    pair_type: str
    lang: Optional[str]
    prompt: str
    original: str
    corrected: str


def _iter_wal_records(wal_path: Path) -> Iterator[Dict[str, Any]]:
    """ES: Recorre el WAL línea por línea (JSONL) tolerando líneas corruptas — un WAL
    append-only puede tener una última línea truncada si el proceso murió a mitad de un write().
    EN: Walks the WAL line by line (JSONL) tolerating corrupt lines — an append-only
    WAL can have a truncated last line if the process died mid-write()."""
    if not wal_path.exists():
        return
    with open(wal_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Línea %s del WAL no es JSON válido, se omite.", line_no)
                continue


def _collect_user_prompts(wal_path: Path) -> Dict[str, str]:
    """ES: turn_id -> prompt del usuario, tomado de los eventos `user_input`.
    EN: turn_id -> user prompt, taken from `user_input` events."""
    prompts: Dict[str, str] = {}
    for record in _iter_wal_records(wal_path):
        if record.get("event_type") != "user_input":
            continue
        payload = record.get("payload") or {}
        turn_id = payload.get("turn_id")
        prompt = payload.get("prompt")
        if turn_id and prompt:
            prompts[str(turn_id)] = str(prompt)
    return prompts


def collect_correction_pairs(wal_path: str | Path) -> list[CorrectionPair]:
    """ES: Extrae todos los `correction_pair` del WAL, unidos con el prompt original.
    Un par sin turn_id reconocible en los eventos user_input se descarta.
    EN: Extracts every `correction_pair` from the WAL, joined with the original
    prompt. A pair with no matching turn_id in the user_input events is discarded."""
    wal_path = Path(wal_path)
    prompts_by_turn = _collect_user_prompts(wal_path)

    pairs: list[CorrectionPair] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for record in _iter_wal_records(wal_path):
        if record.get("event_type") != "turn_phase":
            continue
        payload = record.get("payload") or {}
        if payload.get("phase") != "correction_pair":
            continue

        turn_id = str(payload.get("turn_id") or "")
        original = str(payload.get("original") or "").strip()
        corrected = str(payload.get("corrected") or "").strip()
        if not turn_id or not original or not corrected:
            continue
        if original == corrected:
            continue

        prompt = prompts_by_turn.get(turn_id)
        if not prompt:
            continue

        dedup_key = (turn_id, original, corrected)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        pairs.append(CorrectionPair(
            turn_id=turn_id,
            timestamp=str(record.get("timestamp") or ""),
            pair_type=str(payload.get("pair_type") or "unknown"),
            lang=payload.get("lang"),
            prompt=prompt,
            original=original,
            corrected=corrected,
        ))
    return pairs


def export_wal_to_training_data(
    wal_path: str | Path = "sovnode.wal",
    out_dir: str | Path = "training_data",
) -> Dict[str, int]:
    """
    ES: Escribe dpo_pairs.jsonl y sft_pairs.jsonl en out_dir a partir del WAL.
    Idempotente: siempre reescribe los dos archivos completos desde cero.
    EN: Writes dpo_pairs.jsonl and sft_pairs.jsonl into out_dir from the WAL.
    Idempotent: always rewrites both files from scratch.
    """
    wal_path = Path(wal_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pairs = collect_correction_pairs(wal_path)

    dpo_path = out_dir / "dpo_pairs.jsonl"
    sft_path = out_dir / "sft_pairs.jsonl"

    by_type: Dict[str, int] = {}
    with open(dpo_path, "w", encoding="utf-8") as dpo_f, \
         open(sft_path, "w", encoding="utf-8") as sft_f:
        for pair in pairs:
            by_type[pair.pair_type] = by_type.get(pair.pair_type, 0) + 1
            meta = {
                "turn_id": pair.turn_id,
                "timestamp": pair.timestamp,
                "pair_type": pair.pair_type,
                "lang": pair.lang,
            }
            dpo_f.write(json.dumps({
                "prompt": pair.prompt,
                "chosen": pair.corrected,
                "rejected": pair.original,
                "meta": meta,
            }, ensure_ascii=False) + "\n")
            sft_f.write(json.dumps({
                "prompt": pair.prompt,
                "response": pair.corrected,
                "meta": meta,
            }, ensure_ascii=False) + "\n")

    logger.info(
        "Exportación de entrenamiento: %d par(es) -> %s / %s",
        len(pairs), dpo_path, sft_path,
    )
    return {"pairs": len(pairs), "by_type": by_type}


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Exporta pares (respuesta mala, respuesta corregida) del WAL de SovNode "
                    "a formato DPO/SFT JSONL para fine-tuning local."
    )
    parser.add_argument("--wal", default="sovnode.wal", help="Ruta al archivo WAL (default: sovnode.wal)")
    parser.add_argument("--out", default="training_data", help="Directorio de salida (default: training_data)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stats = export_wal_to_training_data(args.wal, args.out)
    if stats["pairs"] == 0:
        print(
            "No se encontraron pares de corrección en el WAL todavía. "
            "Esto es normal en una instalación nueva o si el pipeline no ha "
            "disparado ninguna corrección — vuelve a intentar tras usar SovNode "
            "un tiempo más."
        )
        return

    print(f"Exportados {stats['pairs']} par(es) de entrenamiento a '{args.out}/':")
    for pair_type, count in sorted(stats["by_type"].items(), key=lambda kv: -kv[1]):
        print(f"  - {pair_type}: {count}")
    print(f"  -> {args.out}/dpo_pairs.jsonl  (formato DPO: prompt/chosen/rejected)")
    print(f"  -> {args.out}/sft_pairs.jsonl  (formato SFT: prompt/response)")


if __name__ == "__main__":
    _main()
