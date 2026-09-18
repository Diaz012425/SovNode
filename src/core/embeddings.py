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
SovNode — embeddings.py
ES: Generación de vectores de embedding LOCALES (en proceso, vía fastembed/ONNX),
con degradación a un hash determinista cuando el modelo local no está disponible.
Se calculan en proceso (sin pasar por Ollama) para no competir por el lock del LLM
ni pagar un round-trip HTTP en el paso más temprano de cada turno (caché semántico).
EN: LOCAL (in-process) embedding vector generation via fastembed/ONNX, degrading to
a deterministic hash when the local model isn't available. Computed in-process
(never through Ollama) to avoid competing for the LLM lock or paying an HTTP
round-trip on the earliest step of every turn (semantic cache).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import os
import threading
from typing import List, Optional, Tuple

logger = logging.getLogger("SovNode.Embeddings")

EMBEDDING_MODE_SEMANTIC = "semantic"
EMBEDDING_MODE_HASH_FALLBACK = "hash_fallback"
EMBEDDING_MODE_UNAVAILABLE = "unavailable"

_LOCAL_EMBEDDING_MODEL_NAME = os.getenv(
    "SOVNODE_LOCAL_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
_LOCAL_EMBEDDING_CACHE_DIR = os.getenv(
    "SOVNODE_LOCAL_EMBED_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "sovnode", "fastembed"),
)

_local_model_lock = threading.Lock()
_local_model_instance = None
_local_model_load_failed = False


def _get_local_model():
    """
    ES: Carga perezosa y memoizada del modelo local de embeddings. Si falla
    (fastembed no instalado, sin red, disco lleno), memoriza el fallo para
    no reintentar en cada llamada y degrada a hash de forma estable.
    EN: Lazy, memoized load of the local embedding model. On failure
    (fastembed missing, no network, disk full), memoizes the failure to
    avoid retrying every call and degrades to the hash fallback.
    """
    global _local_model_instance, _local_model_load_failed
    if _local_model_instance is not None or _local_model_load_failed:
        return _local_model_instance

    with _local_model_lock:
        if _local_model_instance is not None or _local_model_load_failed:
            return _local_model_instance
        try:
            from fastembed import TextEmbedding
            os.makedirs(_LOCAL_EMBEDDING_CACHE_DIR, exist_ok=True)
            _local_model_instance = TextEmbedding(
                model_name=_LOCAL_EMBEDDING_MODEL_NAME,
                cache_dir=_LOCAL_EMBEDDING_CACHE_DIR,
            )
        except Exception as exc:
            logger.warning(
                "No se pudo cargar el modelo local de embeddings '%s' (%s); "
                "se usará el fallback hash. Instalalo con: pip install fastembed",
                _LOCAL_EMBEDDING_MODEL_NAME, exc,
            )
            _local_model_load_failed = True
            return None

    return _local_model_instance


def prewarm_local_embedding_model() -> None:
    """ES: Fuerza la carga del modelo local antes del primer turno real (llamar desde un hilo de fondo al arrancar).
    EN: Forces the local model to load before the first real turn (call from a background thread on startup)."""
    with contextlib.suppress(Exception):
        _get_local_model()


def get_embedding(text: str, dim: int = 384) -> Optional[List[float]]:
    """ES: Vector de embedding para `text` sin informar el modo. Válido si solo se ordenan
    candidatos por proximidad relativa; para comparar contra un umbral absoluto usar get_embedding_with_mode.
    EN: Embedding vector for `text` without reporting the mode. Fine when only ranking
    candidates by relative proximity; use get_embedding_with_mode for an absolute similarity threshold."""
    vector, _mode = get_embedding_with_mode(text, dim)
    return vector


def get_embedding_with_mode(text: str, dim: int = 384) -> Tuple[Optional[List[float]], str]:
    """
    ES: Igual que get_embedding, pero devuelve (vector, modo): intenta el modelo local
    (SEMANTIC) y, si no está disponible, cae a un hash bag-of-words determinista
    (HASH_FALLBACK) que mantiene la memoria vectorial funcional sin capturar semántica real.
    EN: Same as get_embedding but returns (vector, mode): tries the local model
    (SEMANTIC) first, falling back to a deterministic bag-of-words hash
    (HASH_FALLBACK) that keeps vector memory functional without real semantics.
    """
    text = (text or "").strip()
    if not text:
        return None, EMBEDDING_MODE_UNAVAILABLE

    model = _get_local_model()
    if model is not None:
        try:
            vector = next(iter(model.embed([text[:4000]])))
            return _fit_dimension([float(v) for v in vector], dim), EMBEDDING_MODE_SEMANTIC
        except Exception as exc:
            logger.debug("Embedding local falló (%s); usando fallback hash.", exc)

    return _hash_fallback_embedding(text, dim), EMBEDDING_MODE_HASH_FALLBACK


def _fit_dimension(vector: List[float], dim: int) -> List[float]:
    """ES: Recorta o rellena con ceros el vector para que tenga exactamente `dim` componentes.
    EN: Truncates or zero-pads the vector so it has exactly `dim` components."""
    if len(vector) == dim:
        return vector
    if len(vector) > dim:
        return vector[:dim]
    return vector + [0.0] * (dim - len(vector))


def _hash_fallback_embedding(text: str, dim: int) -> List[float]:
    """
    ES: Vector pseudo-semántico determinista: cada palabra se hashea a un bucket con
    signo y el vector resultante se normaliza. No captura semántica real, pero agrupa
    textos idénticos o muy similares sin requerir un modelo de embeddings instalado.
    EN: Deterministic pseudo-semantic vector: each word hashes into a signed bucket
    and the result is normalized. No real semantics, but groups identical or very
    similar texts without requiring an installed embedding model.
    """
    vector = [0.0] * dim
    words = text.lower().split()
    if not words:
        return vector

    for word in words:
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign

    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def get_embeddings_batch(texts: List[str], dim: int = 384) -> List[Optional[List[float]]]:
    """ES: Versión en LOTE de get_embedding — un único embed() al modelo local para
    TODOS los textos en vez de uno por texto. fastembed/ONNX corre la inferencia de
    un lote entero de una sola pasada por el runtime; llamarlo N veces con un solo
    texto cada vez (como hacía antes cada chunk de un documento al indexarse) paga
    N veces el overhead de por-llamada sin aprovechar el vectorizado interno del
    modelo. Pensado para reindexar un documento entero (rag_faiss.index_document_for_rag):
    todos los chunks de un mismo archivo se embeben en una sola pasada.
    EN: BATCHED version of get_embedding — a single embed() call to the local model
    for ALL texts instead of one call per text. fastembed/ONNX runs inference for a
    whole batch in one pass through the runtime; calling it N times with one text
    each (as document reindexing used to do, once per chunk) pays the per-call
    overhead N times without benefiting from the model's internal vectorization.
    Meant for reindexing a whole document (rag_faiss.index_document_for_rag): every
    chunk of the same file gets embedded in a single pass.
    """
    vectors, _modes = zip(*get_embeddings_with_mode_batch(texts, dim)) if texts else ((), ())
    return list(vectors)


def get_embeddings_with_mode_batch(
    texts: List[str], dim: int = 384
) -> List[Tuple[Optional[List[float]], str]]:
    """ES: Igual que get_embedding_with_mode pero en lote — ver get_embeddings_batch
    para el motivo. Preserva el orden y largo de `texts`: una entrada vacía devuelve
    (None, EMBEDDING_MODE_UNAVAILABLE) en su misma posición, sin correrle el índice
    a las demás. Si el modelo local falla a mitad del lote, TODO el lote degrada
    junto a hash fallback (mismo criterio que get_embedding_with_mode: nunca mezcla
    modos dentro de una misma llamada, para no falsear una comparación de similitud).
    EN: Same as get_embedding_with_mode but batched — see get_embeddings_batch for
    why. Preserves the order and length of `texts`: an empty entry returns
    (None, EMBEDDING_MODE_UNAVAILABLE) at its own position, without shifting the
    others. If the local model fails partway through the batch, the WHOLE batch
    degrades together to the hash fallback (same rule as get_embedding_with_mode:
    never mixes modes within one call, so a similarity comparison isn't skewed).
    """
    cleaned = [(t or "").strip() for t in texts]
    results: List[Tuple[Optional[List[float]], str]] = [
        (None, EMBEDDING_MODE_UNAVAILABLE) for _ in cleaned
    ]
    non_empty_idx = [i for i, t in enumerate(cleaned) if t]
    if not non_empty_idx:
        return results

    model = _get_local_model()
    if model is not None:
        try:
            batch_vectors = list(model.embed([cleaned[i][:4000] for i in non_empty_idx]))
            for idx, vector in zip(non_empty_idx, batch_vectors):
                results[idx] = (
                    _fit_dimension([float(v) for v in vector], dim),
                    EMBEDDING_MODE_SEMANTIC,
                )
            return results
        except Exception as exc:
            logger.debug("Embedding local en lote falló (%s); usando fallback hash.", exc)

    for idx in non_empty_idx:
        results[idx] = (_hash_fallback_embedding(cleaned[idx], dim), EMBEDDING_MODE_HASH_FALLBACK)
    return results
