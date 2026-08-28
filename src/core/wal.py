"""
El Monolito Personal - Arquitectura v2.0
Paso 1: Write-Ahead Log (WAL) con durabilidad síncrona y Nodos de Conocimiento

Corrección aplicada (auditoría v3.8.1):
    - append_user_input() y append_response() tenían el orden de
      parámetros invertido respecto a como los invoca
      Orchestrator.process_turn(), que llama:
          append_user_input(turn_id, user_input)
          append_response(turn_id, final_response, outcome.value)
      Con la firma original (prompt/response primero, turn_id después)
      cada registro persistía los valores de turn_id y contenido
      intercambiados, corrompiendo silenciosamente la trazabilidad del
      log de auditoría. Se realinea la firma a (turn_id, contenido, ...)
      y se añade el campo 'outcome', antes descartado sin usarse.
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
    """Registro inmutable de un evento persistido en el WAL."""
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
    Unidad atómica e inmutable de conocimiento verificado.

    Contiene únicamente el axioma destilado junto con su metadata de
    verificación y procedencia. `node_id` es un hash SHA-256 sobre
    (dominio, axioma, verificación): dos análisis independientes que
    lleguen al mismo axioma verificado producen el mismo node_id.
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
    Write-Ahead Log append-only en formato JSONL.

    Cada escritura se vacía al buffer del SO de inmediato (flush(),
    barato — sobrevive a un crash del PROCESO y es visible de inmediato
    a cualquier lector del archivo), pero el fsync() que además garantiza
    supervivencia ante un corte de energía/crash del propio SO ya NO se
    hace de forma síncrona en cada append() — eso bloqueaba con I/O real
    de disco el hilo llamante en cada turno (user_input + response +,
    a veces, knowledge_node: varios fsync síncronos por turno). En su
    lugar, un hilo de fondo hace fsync() por lotes: cada
    `FSYNC_INTERVAL_SECONDS`, o de inmediato si se acumulan
    `FSYNC_EVENT_THRESHOLD` escrituras sin sincronizar — lo que ocurra
    primero. Ventana de pérdida posible ante un corte de energía real:
    como mucho `FSYNC_INTERVAL_SECONDS` de eventos (flush() ya los hizo
    sobrevivir a un crash de proceso; solo un apagón/panic del kernel
    dentro de esa ventana podría perderlos). `close()` siempre hace un
    último fsync síncrono antes de cerrar el archivo.
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
        """
        Registra la entrada del usuario en el WAL.

        Firma alineada con la convención (turn_id, contenido) usada por
        Orchestrator.process_turn(): append_user_input(turn_id, prompt).
        """
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
        """
        Registra la respuesta del modelo/sistema en el WAL.

        Firma alineada con la convención (turn_id, contenido, outcome)
        usada por Orchestrator.process_turn():
            append_response(turn_id, final_response, outcome.value)

        El campo `outcome` (p. ej. 'fast_path_direct', 'slow_path_cas',
        'error') se persiste explícitamente para permitir auditorías
        posteriores sobre qué ruta de razonamiento produjo cada
        respuesta.
        """
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

        # fsync() real se hace FUERA del lock (excepto por la sección
        # crítica breve dentro de _do_fsync) para no bloquear otros
        # hilos que solo necesitan un write()+flush() barato mientras
        # este evento dispara la sincronización física por umbral.
        if should_sync_now:
            self._do_fsync()
        return entry

    def _do_fsync(self) -> None:
        """Vacía físicamente a disco lo pendiente. Llamada por el hilo de fondo, por umbral, o desde close()."""
        with self._lock:
            if self._file_handle is None or self._pending_fsync == 0:
                return
            try:
                os.fsync(self._file_handle.fileno())
            except OSError:
                pass
            self._pending_fsync = 0

    def _fsync_loop(self) -> None:
        """Hilo de fondo: fsync() por lotes cada FSYNC_INTERVAL_SECONDS mientras el WAL siga abierto."""
        while not self._stop_event.wait(self.FSYNC_INTERVAL_SECONDS):
            self._do_fsync()

    def append_knowledge_node(self, node: KnowledgeNode) -> WALEntry:
        """Persiste un KnowledgeNode de forma inmutable en el WAL."""
        if not node.verify_integrity():
            raise ValueError(
                f"Integridad violada: node_id={node.node_id!r} no corresponde "
                f"al contenido (esperado={node.content_hash()!r}). "
                "Se rehúsa persistir un nodo de conocimiento corrupto."
            )
        return self.append(self.KNOWLEDGE_EVENT_TYPE, node.to_payload())

    def iter_knowledge_nodes(self) -> Iterator[KnowledgeNode]:
        """Reproduce el WAL en disco y genera los KnowledgeNode persistidos."""
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
        # Detiene el hilo de fsync por lotes y hace un último fsync
        # síncrono antes de cerrar - así el cierre explícito (o el
        # `with WriteAheadLog(...)`) nunca pierde la cola de eventos que
        # el batching aún no había sincronizado a disco.
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

