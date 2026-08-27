"""
SovNode — embeddings.py
Generación de vectores de embedding LOCALES (en proceso, vía fastembed/ONNX),
con degradación a un hash determinista cuando el modelo local no está disponible.

BLINDAJE (bug real, MEDIDO — reportado por el usuario): la versión anterior
de este módulo le pegaba a `POST {endpoint}/api/embeddings` (Ollama) por
HTTP — y esa llamada era la ÚNICA de todo orchestrator.py que tocaba Ollama
sin pasar por `self._llm_lock` (los ~10 call sites restantes sí lo respetan,
a propósito, para no competir por la misma GPU/VRAM). Como el chequeo de
caché semántico (que usa este módulo) corre en el paso MÁS temprano de
CADA turno — antes del router, antes de todo — cada mensaje del usuario
disparaba una petición HTTP a Ollama sin cerrojo, capaz de pisarse con una
generación en curso (turno de otro usuario o el CognitiveGovernor en
background). Sacar el embedding de Ollama por completo resuelve el bug de
raíz para los tres call sites que lo tenían (no solo uno) Y de paso elimina
el round-trip HTTP+carga de modelo que pagaba TODO turno, incluso los que
terminan siendo un cache hit — el camino que más importa que sea rápido,
porque en teoría no debería tocar al LLM para nada. Medido en esta máquina:
~12ms por embedding en caliente vs. cualquier ida y vuelta HTTP a Ollama.
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

# Modo con el que se produjo un vector. El llamador NECESITA distinguirlos:
# un vector `SEMANTIC` viene de un modelo de embeddings real y su similitud
# coseno significa proximidad de significado; un vector `HASH_FALLBACK` es
# un bag-of-words hasheado y su similitud significa solamente "comparten
# palabras" - es INVARIANTE AL ORDEN, así que dos consultas con las mismas
# palabras en distinto orden ("¿es A mejor que B?" / "¿es B mejor que A?")
# dan similitud exactamente 1.0. Cualquier decisión que dependa de un
# umbral de similitud (caché semántico) tiene que consultar este modo
# antes de confiar en el número.
EMBEDDING_MODE_SEMANTIC = "semantic"
EMBEDDING_MODE_HASH_FALLBACK = "hash_fallback"
EMBEDDING_MODE_UNAVAILABLE = "unavailable"

# all-MiniLM-L6-v2: 384 dimensiones (coincide con LocalVectorRAG(vector_dim=384)
# y con el `dim` default de todo este módulo - no es casualidad, el resto del
# código ya estaba dimensionado para este modelo), liviano (ONNX, sin
# dependencia de PyTorch - este entorno ya trae onnxruntime), y sub-15ms por
# embedding en CPU una vez cargado.
_LOCAL_EMBEDDING_MODEL_NAME = os.getenv(
    "SOVNODE_LOCAL_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
# Cache persistente fuera de carpetas temporales: el default de fastembed
# (carpeta temp del SO) puede limpiarse en cualquier reinicio de Windows,
# forzando una re-descarga de ~90MB en el peor momento posible (mitad de
# un turno real). Un directorio bajo el perfil del usuario sobrevive a
# limpiezas de temp y a reinicios del equipo.
_LOCAL_EMBEDDING_CACHE_DIR = os.getenv(
    "SOVNODE_LOCAL_EMBED_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "sovnode", "fastembed"),
)

_local_model_lock = threading.Lock()
_local_model_instance = None  # type: ignore[var-annotated]
_local_model_load_failed = False


def _get_local_model():
    """
    Carga perezosa (y memoizada) del modelo local de embeddings — la carga
    real (~unos segundos la primera vez que corre en esta máquina, casi
    instantánea después gracias a `_LOCAL_EMBEDDING_CACHE_DIR`) solo se
    paga una vez por proceso. Si `fastembed` no está instalado o el modelo
    no se puede cargar (sin red la primera vez, disco lleno, etc.), se
    memoriza el fallo para no reintentar en cada llamada — degrada a hash
    de forma silenciosa y estable durante toda la sesión.
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
    """
    Fuerza la carga del modelo local ANTES del primer turno real — pensado
    para llamarse desde un hilo de fondo al arrancar la app (mismo espíritu
    que el precalentado de Ollama que ya existe para el modelo de chat).
    Sin esto, el primer chequeo de caché semántico del primer turno paga
    la carga completa del modelo (unos segundos la primera vez en esta
    máquina) en vez de los ~12ms de una llamada ya caliente.
    """
    with contextlib.suppress(Exception):
        _get_local_model()


def get_embedding(text: str, dim: int = 384) -> Optional[List[float]]:
    """
    Obtiene un vector de embedding para `text`, sin informar de qué modo
    salió. Válido para consumidores que solo ordenan candidatos por
    proximidad relativa (RAG vectorial). Si vas a comparar la similitud
    contra un umbral absoluto, usa `get_embedding_with_mode`.
    """
    vector, _mode = get_embedding_with_mode(text, dim)
    return vector


def get_embedding_with_mode(text: str, dim: int = 384) -> Tuple[Optional[List[float]], str]:
    """
    Igual que `get_embedding`, pero devuelve `(vector, modo)` con el modo
    que realmente produjo el vector (ver constantes EMBEDDING_MODE_*).

    1) Intenta el modelo local (`all-MiniLM-L6-v2` vía fastembed/ONNX, en
       proceso, sin red) → `SEMANTIC`.
    2) Si `fastembed` no está instalado o el modelo no pudo cargarse, cae a
       un hash determinista bag-of-words de baja fidelidad → `HASH_
       FALLBACK`: mantiene la memoria vectorial funcional (agrupa
       repeticiones exactas/similares) sin exigir la instalación adicional,
       pero NO captura semántica.
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
    if len(vector) == dim:
        return vector
    if len(vector) > dim:
        return vector[:dim]
    return vector + [0.0] * (dim - len(vector))


def _hash_fallback_embedding(text: str, dim: int) -> List[float]:
    """
    Vector pseudo-semántico determinista: cada palabra se hashea a un
    bucket con signo y el vector resultante se normaliza. No captura
    semántica real, pero es estable, gratuito y suficiente para que textos
    idénticos o muy similares se agrupen cuando no hay modelo de
    embeddings real instalado.
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
