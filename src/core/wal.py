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
Paso 1: Write-Ahead Log (WAL) con durabilidad síncrona y Nodos de Conocimiento

ES: append_user_input/append_response usan la firma (turn_id, contenido, ...)
para calzar con cómo los invoca Orchestrator.process_turn(); el campo
'outcome' de append_response se persiste para poder auditar qué ruta de
razonamiento produjo cada respuesta.
EN: append_user_input/append_response use the (turn_id, content, ...)
signature to match how Orchestrator.process_turn() calls them; the 'outcome'
field on append_response is persisted so later audits can tell which
reasoning path produced each response.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger("monolith.wal")


@dataclass(frozen=True)
class WALEntry:
    """ES: Registro inmutable de un evento persistido en el WAL.
    EN: Immutable record of one event persisted to the WAL."""
    sequence: int
    timestamp: str
    event_type: str
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps({
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "payload": self.payload,
        }, ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> WALEntry:
        return cls(
            sequence=record["sequence"],
            timestamp=record["timestamp"],
            event_type=record["event_type"],
            payload=record["payload"],
        )


@dataclass(frozen=True)
class KnowledgeNode:
    """
    ES: Unidad atómica e inmutable de conocimiento verificado. node_id es un
    hash SHA-256 sobre (dominio, axioma, verificación): dos análisis
    independientes que lleguen al mismo axioma verificado producen el mismo node_id.
    EN: Atomic, immutable unit of verified knowledge. node_id is a SHA-256 hash
    over (domain, axiom, verification): two independent analyses reaching the
    same verified axiom produce the same node_id.
    """
    node_id: str
    created_at: str
    domain: str
    axiom: str
    verification: dict[str, Any]
    provenance: dict[str, Any]

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "axiom": self.axiom,
            "verification": self.verification,
        }

    def content_hash(self) -> str:
        canonical = json.dumps(
            self._canonical_payload(), sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.node_id == self.content_hash()

    @classmethod
    def create(
        cls,
        domain: str,
        axiom: str,
        verification: dict[str, Any],
        provenance: dict[str, Any],
    ) -> KnowledgeNode:
        draft = cls(
            node_id="",
            created_at=datetime.now().isoformat(timespec="seconds"),
            domain=domain,
            axiom=axiom,
            verification=verification,
            provenance=provenance,
        )
        return dataclasses.replace(draft, node_id=draft.content_hash())

    def to_payload(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "created_at": self.created_at,
            "domain": self.domain,
            "axiom": self.axiom,
            "verification": self.verification,
            "provenance": self.provenance,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> KnowledgeNode:
        return cls(
            node_id=payload["node_id"],
            created_at=payload["created_at"],
            domain=payload["domain"],
            axiom=payload["axiom"],
            verification=payload["verification"],
            provenance=payload["provenance"],
        )

    def __str__(self) -> str:
        return f"[KNOWLEDGE::{self.domain.upper()}] {self.axiom} (node_id={self.node_id[:10]}…)"


class WriteAheadLog:
    """
    ES: Write-Ahead Log append-only en formato JSONL. Cada escritura hace
    flush() de inmediato (barato, sobrevive a un crash del proceso), pero el
    fsync() que además sobrevive a un corte de energía se hace en lotes desde
    un hilo de fondo (cada FSYNC_INTERVAL_SECONDS, o antes si se acumulan
    FSYNC_EVENT_THRESHOLD escrituras) en vez de síncrono en cada append() —
    evita bloquear el hilo llamante con I/O real de disco en cada turno.
    close() siempre hace un último fsync síncrono antes de cerrar.
    EN: Append-only Write-Ahead Log in JSONL format. Every write flush()es
    immediately (cheap, survives a process crash), but the fsync() that also
    survives a power loss is batched from a background thread (every
    FSYNC_INTERVAL_SECONDS, or sooner if FSYNC_EVENT_THRESHOLD writes pile up)
    instead of synchronous on every append() — avoids blocking the calling
    thread with real disk I/O on every turn. close() always does one final
    synchronous fsync before closing.
    """

    KNOWLEDGE_EVENT_TYPE = "knowledge_node"
    FSYNC_INTERVAL_SECONDS = 1.0
    FSYNC_EVENT_THRESHOLD = 20

    def __init__(self, log_path: str | Path = "sovnode.wal") -> None:
        self._log_path = Path(log_path)
        self._lock = threading.Lock()
        self._sequence_counter = self._recover_last_sequence()
        self._file_handle: Optional[Any] = None
        self._pending_fsync = 0
        self._stop_event = threading.Event()
        self._open_file_for_append()
        self._fsync_thread = threading.Thread(
            target=self._fsync_loop, name="WAL-FsyncBatcher", daemon=True
        )
        self._fsync_thread.start()

    def append_user_input(
        self,
        turn_id: str | None,
        prompt: str,
        *args: Any,
        **kwargs: Any,
    ) -> WALEntry:
        """ES: Registra la entrada del usuario en el WAL: append_user_input(turn_id, prompt).
        EN: Logs the user's input to the WAL: append_user_input(turn_id, prompt)."""
        return self.append(
            "user_input",
            {
                "turn_id": turn_id,
                "prompt": prompt,
            },
        )

    def append_response(
        self,
        turn_id: str | None,
        response: str,
        outcome: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> WALEntry:
        """ES: Registra la respuesta del modelo/sistema: append_response(turn_id, final_response, outcome.value).
        outcome (p. ej. 'fast_path_direct', 'error') se persiste para auditar qué ruta produjo cada respuesta.
        EN: Logs the model/system response: append_response(turn_id, final_response, outcome.value).
        outcome (e.g. 'fast_path_direct', 'error') is persisted so later audits can tell which path produced each response."""
        return self.append(
            "response",
            {
                "turn_id": turn_id,
                "response": response,
                "outcome": outcome,
            },
        )

    def _recover_last_sequence(self) -> int:
        if not self._log_path.exists():
            return 0
        max_seq = 0
        with open(self._log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    seq = int(data.get("sequence", 0))
                    if seq > max_seq:
                        max_seq = seq
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
        return max_seq

    def _open_file_for_append(self) -> None:
        self._file_handle = open(self._log_path, "a", encoding="utf-8")

    def append(self, event_type: str, payload: dict[str, Any]) -> WALEntry:
        with self._lock:
            if self._file_handle is None:
                raise RuntimeError("WAL cerrado: no se puede escribir un nuevo evento.")
            self._sequence_counter += 1
            entry = WALEntry(
                sequence=self._sequence_counter,
                timestamp=datetime.now().isoformat(timespec="seconds"),
                event_type=event_type,
                payload=payload,
            )
            self._file_handle.write(entry.to_json() + "\n")
            self._file_handle.flush()
            self._pending_fsync += 1
            should_sync_now = self._pending_fsync >= self.FSYNC_EVENT_THRESHOLD

        # ES: fsync() real fuera del lock (salvo la sección crítica breve de
        # _do_fsync) para no bloquear otros hilos que solo necesitan write()+flush().
        # EN: The real fsync() runs outside the lock (except _do_fsync's brief
        # critical section) so it doesn't block other threads that only need write()+flush().
        if should_sync_now:
            self._do_fsync()
        return entry

    def _do_fsync(self) -> None:
        """ES: Vacía físicamente a disco lo pendiente. Llamada por el hilo de fondo, por umbral, o desde close().
        EN: Physically flushes pending writes to disk. Called by the background thread, on threshold, or from close()."""
        with self._lock:
            if self._file_handle is None or self._pending_fsync == 0:
                return
            try:
                os.fsync(self._file_handle.fileno())
            except OSError:
                pass
            self._pending_fsync = 0

    def _fsync_loop(self) -> None:
        """ES: Hilo de fondo: fsync() por lotes cada FSYNC_INTERVAL_SECONDS mientras el WAL siga abierto.
        EN: Background thread: batched fsync() every FSYNC_INTERVAL_SECONDS while the WAL stays open."""
        while not self._stop_event.wait(self.FSYNC_INTERVAL_SECONDS):
            self._do_fsync()

    def append_knowledge_node(self, node: KnowledgeNode) -> WALEntry:
        """ES: Persiste un KnowledgeNode de forma inmutable en el WAL.
        EN: Persists a KnowledgeNode immutably to the WAL."""
        if not node.verify_integrity():
            raise ValueError(
                f"Integridad violada: node_id={node.node_id!r} no corresponde "
                f"al contenido (esperado={node.content_hash()!r}). "
                "Se rehúsa persistir un nodo de conocimiento corrupto."
            )
        return self.append(self.KNOWLEDGE_EVENT_TYPE, node.to_payload())

    def iter_knowledge_nodes(self) -> Iterator[KnowledgeNode]:
        """ES: Reproduce el WAL en disco y genera los KnowledgeNode persistidos.
        EN: Replays the on-disk WAL and yields the persisted KnowledgeNode records."""
        if not self._log_path.exists():
            return
        with open(self._log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event_type") != self.KNOWLEDGE_EVENT_TYPE:
                    continue
                try:
                    node = KnowledgeNode.from_payload(record["payload"])
                except (KeyError, TypeError):
                    logger.warning(
                        "Nodo de conocimiento con payload malformado, se omite (seq=%s).",
                        record.get("sequence"),
                    )
                    continue
                if not node.verify_integrity():
                    logger.warning(
                        "Nodo de conocimiento con hash inválido, se omite (node_id=%s).",
                        node.node_id,
                    )
                    continue
                yield node

    def close(self) -> None:
        # ES: Detiene el hilo de fsync por lotes y hace un último fsync síncrono
        # antes de cerrar, para no perder eventos aún no sincronizados a disco.
        # EN: Stops the batched fsync thread and does one final synchronous
        # fsync before closing, so no not-yet-synced events are lost.
        self._stop_event.set()
        if self._fsync_thread.is_alive():
            self._fsync_thread.join(timeout=self.FSYNC_INTERVAL_SECONDS + 1.0)
        self._do_fsync()
        with self._lock:
            if self._file_handle:
                self._file_handle.close()
                self._file_handle = None

    def __enter__(self) -> WriteAheadLog:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def get_recent_entries(self, count: int = 5) -> list[dict[str, Any]]:
        if not self._log_path.exists():
            return []
        entries = []
        with open(self._log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    payload = data.get("payload", {})
                    entries.append({
                        "type": data.get("event_type"),
                        "turn_id": payload.get("turn_id"),
                        "content": payload.get("response") or payload.get("prompt") or "",
                        "outcome": payload.get("outcome"),
                        "timestamp": data.get("timestamp"),
                    })
                except json.JSONDecodeError:
                    continue
        return entries[-count:]
