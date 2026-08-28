"""
El Monolito Personal / SovNode - Exportador de corpus de entrenamiento
=======================================================================

IDEA DE ARQUITECTURA (2026-08-19): cada vez que el pipeline de
Orchestrator/StreamTurnWorker detecta y corrige un error del modelo local
(marcador sin respaldo, ganador sin respaldo, contradicción sin atribuir,
idioma equivocado, o un resample de consenso que le ganó al candidato
original — ver `_count_verifiable_violations` en orchestrator.py), ese
evento queda registrado en el WAL (`sovnode.wal`) como un `turn_phase`
con `phase == "correction_pair"`, junto con el texto ORIGINAL (malo) y el
texto CORREGIDO (bueno) para el mismo turno.

Ese par (malo, bueno) es exactamente la señal que un fine-tune local
necesita para que el modelo de 3B deje de cometer el mismo error de
nuevo, en vez de depender para siempre de la cadena de corrección
post-hoc para arreglarlo cada vez. Este módulo NO entrena nada — solo
recorre el WAL, junta cada `correction_pair` con el prompt original del
usuario (evento `user_input` del mismo `turn_id`), y exporta dos
formatos JSONL listos para consumir con herramientas estándar de
fine-tuning local (p. ej. `trl`/Unsloth/Axolotl):

  - `dpo_pairs.jsonl`: {"prompt", "chosen", "rejected", "meta"} — el
    formato nativo de DPO (Direct Preference Optimization). Es el más
    directo para este caso: ya tenemos, para el MISMO prompt, una
    respuesta rechazada (la original) y una preferida (la corregida) —
    exactamente lo que DPO necesita, sin tener que sintetizar negativos.

  - `sft_pairs.jsonl`: {"prompt", "response", "meta"} — solo el lado
    BUENO (la respuesta corregida), en formato de fine-tuning
    supervisado estándar, para quien prefiera SFT simple sobre DPO.

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
    """
    Recorre el WAL línea por línea (JSONL) tolerando líneas corruptas —
    un WAL es append-only y puede tener una última línea truncada si el
    proceso murió a mitad de un write(); se ignora esa línea en vez de
    abortar la exportación completa por ella.
    """
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
    """turn_id -> prompt del usuario, tomado de los eventos `user_input`."""
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
    """
    Extrae todos los `correction_pair` del WAL, ya unidos con el prompt
    original del usuario. Un `correction_pair` sin `turn_id` reconocible
    en los eventos `user_input` (WAL truncado, corrección disparada fuera
    de un turno normal) se descarta — sin el prompt original el par no
    sirve como dato de entrenamiento.
    """
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
            # Puede pasar si el propio LLM de corrección devolvió el
            # texto sin cambios - no es una preferencia real, DPO
            # necesita que chosen != rejected.
            continue

        prompt = prompts_by_turn.get(turn_id)
        if not prompt:
            continue

        # Evita duplicados exactos: varias correcciones encadenadas en
        # el mismo turno (p. ej. score -> language) pueden repetir el
        # mismo par si una no cambió nada perceptible entre medio.
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
    Escribe `dpo_pairs.jsonl` y `sft_pairs.jsonl` en `out_dir` a partir
    del WAL en `wal_path`. Devuelve un resumen {"pairs": N,
    "by_type": {...}} para reportar en la UI o en consola.

    Idempotente y segura de re-ejecutar: siempre reescribe los dos
    archivos completos desde cero a partir del WAL actual (que es
    append-only y crece con el tiempo), nunca los mezcla con una
    exportación previa a medias.
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
