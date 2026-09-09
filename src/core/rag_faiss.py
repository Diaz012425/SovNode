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
SovNode — Local FAISS Vector Store
==================================
Sistema RAG ligero para indexación y recuperación semántica local.
"""

from __future__ import annotations

import ast
import contextlib
import json
import logging
import os
import re
import numpy as np
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("SovNode.RAG")

try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False

try:
    from embeddings import get_embedding  # type: ignore
except ImportError:
    def get_embedding(text: str, dim: int = 384) -> Optional[List[float]]:
        return None

# Similitud coseno mínima para que un resultado del índice vectorial
# cuente como relevante. `IndexFlatL2` sobre vectores MiniLM normalizados
# (fastembed) cumple dist² = 2 - 2·cos, así que cos = 1 - dist²/2. Por
# debajo de este piso el chunk es ruido semántico: devolverlo solo
# envenena el prompt del turno. Bug real (2026-09-01): la consulta
# "explicá cómo funciona la fotosíntesis" no tenía NADA afín en
# `sovnode_memory.db`, así que `search()` devolvió igual sus 3 vecinos
# más cercanos — una charla vieja sobre "las ecuaciones más importantes
# de matemática" — y qwen2.5:7b, al ver ese transcripto usuario/asistente
# en su contexto, lo CONTINUÓ (en chino) en vez de responder. Override
# por entorno para tunear sin tocar código.
DEFAULT_RAG_MIN_SIMILARITY: float = float(
    os.getenv("SOVNODE_RAG_MIN_SIMILARITY", "0.30")
)

# Tamaño máximo de chunk por defecto (caracteres). Comparte orden de
# magnitud con MAX_CONTEXT_CHARS_FOR_PROMPT en orchestrator.py: un chunk
# no debería por sí solo poder ahogar el presupuesto de contexto de un
# turno si fetch_hybrid_context() lo recupera como único resultado.
DEFAULT_MAX_CHUNK_CHARS = 1500


def chunk_text_generic(text: str, max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> List[str]:
    """
    Splitter de respaldo para texto NO-Python (Markdown, documentación, o
    cualquier bloque que haya excedido max_chunk_chars incluso tras el
    chunking AST): empaqueta párrafos completos (separados por línea en
    blanco) de forma voraz sin cortar ninguno a la mitad, y solo recurre a
    un corte rígido por caracteres cuando un ÚNICO párrafo ya excede el
    límite por sí solo — el caso que antes SIEMPRE se manejaba así
    (cortes rígidos por número de caracteres), ahora es el último recurso.
    """
    text = (text or "").strip()
    if not text:
        return []

    paragraphs = re.split(r"\n\s*\n", text)
    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(para) > max_chunk_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            for i in range(0, len(para), max_chunk_chars):
                chunks.append(para[i : i + max_chunk_chars])
            continue

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > max_chunk_chars:
            chunks.append(current.strip())
            current = para
        else:
            current = candidate

    if current.strip():
        chunks.append(current.strip())

    return chunks


def chunk_python_source(code: str, max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> List[str]:
    """
    Segmenta código Python en chunks delimitados por AST — funciones y
    clases COMPLETAS — en vez de cortes rígidos por número de caracteres.
    Motivación real: un corte rígido puede partir una función a la mitad
    entre dos documentos del índice vectorial; el fragmento resultante
    (sin su firma, o sin su cuerpo) recuperado meses después por
    fetch_hybrid_context() no le sirve de evidencia útil al modelo.

    Estrategia (nunca lanza — cualquier fallo degrada a chunk_text_generic
    sobre el texto crudo, igual que el resto de los verificadores locales
    de este proyecto):
      1. ast.parse(code). Si el código es inválido o un fragmento parcial
         (p. ej. un snippet pegado sin el módulo completo alrededor), cae
         directo a chunk_text_generic().
      2. Solo se segmenta por los nodos de NIVEL SUPERIOR del módulo
         (ast.iter_child_nodes, NO ast.walk) — así una función anidada
         dentro de una clase (un método) viaja junto con su clase
         contenedora en vez de separarse como chunk independiente sin
         contexto de a qué clase pertenece.
      3. Lo que queda a nivel superior FUERA de esos bloques (imports,
         constantes módulo, docstring del módulo) se agrupa en un chunk
         de "preámbulo" aparte para no perderlo silenciosamente.
      4. Un bloque individual que aun así exceda max_chunk_chars (una
         función/clase gigante) se subdivide con chunk_text_generic()
         como último recurso, para no indexar un chunk único que ahogaría
         el contexto de un solo resultado de búsqueda.
    """
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return chunk_text_generic(code, max_chunk_chars)

    top_level_defs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]

    chunks: List[str] = []
    consumed_lines: set = set()

    for node in top_level_defs:
        segment = ast.get_source_segment(code, node)
        if not segment:
            continue
        if len(segment) > max_chunk_chars:
            chunks.extend(chunk_text_generic(segment, max_chunk_chars))
        else:
            chunks.append(segment)
        end_line = getattr(node, "end_lineno", None) or node.lineno
        consumed_lines.update(range(node.lineno, end_line + 1))

    source_lines = code.splitlines()
    preamble_lines = [
        line for i, line in enumerate(source_lines, start=1)
        if i not in consumed_lines and line.strip()
    ]
    preamble = "\n".join(preamble_lines).strip()
    if preamble:
        chunks = chunk_text_generic(preamble, max_chunk_chars) + chunks

    return chunks or chunk_text_generic(code, max_chunk_chars)


def chunk_document(filename: str, content: str, max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> List[str]:
    """
    Punto de entrada único: elige AST para `.py`/`.pyw`, splitter genérico
    de párrafos para todo lo demás (Markdown, txt, docs). Pensado para
    Orchestrator.index_document_for_rag() antes de vector_rag.add_documents().
    """
    is_python = filename.lower().endswith((".py", ".pyw"))
    if is_python:
        return chunk_python_source(content, max_chunk_chars)
    return chunk_text_generic(content, max_chunk_chars)


class LocalVectorRAG:
    """
    Wrapper local sobre FAISS.

    Índice envuelto en `IndexIDMap2`, NO un `IndexFlatL2` pelado: un
    `IndexFlatL2` crudo solo soporta AGREGAR vectores, nunca borrarlos
    por id. Eso significaba que reindexar un documento (un archivo de
    workspace editado, ver Orchestrator.index_document_for_rag) solo
    podía sumar sus chunks nuevos — los viejos quedaban flotando en el
    índice para siempre, compitiendo en cada búsqueda futura con
    versiones obsoletas del mismo archivo. Con el tiempo esto degrada
    la calidad de búsqueda en silencio, sin ningún error visible — el
    mismo tipo de falla que `DEFAULT_RAG_MIN_SIMILARITY` mitiga para
    vecinos SIN relación, pero acá para vecinos VIEJOS. `IndexIDMap2`
    sí soporta `remove_ids`, a cambio de que nosotros mantengamos el
    mapeo id<->documento explícitamente (antes era implícito por
    posición en una lista).
    """

    def __init__(self, vector_dim: int = 384) -> None:
        self.vector_dim = vector_dim
        # id que nosotros asignamos (NO una posición en un array) -> texto
        # del chunk. FAISS nos devuelve estos mismos ids en `index.search()`
        # porque el índice está envuelto en IndexIDMap2.
        self.documents: Dict[int, str] = {}
        # source_id (típicamente una ruta de archivo) -> ids de sus chunks.
        # Permite `remove_source()` sin que el llamador tenga que rastrear
        # manualmente qué ids pertenecen a qué documento. Lo que se agrega
        # SIN source_id (p. ej. Orchestrator._persist_web_knowledge, que no
        # se reindexa por archivo) no aparece acá y solo se limpia vía
        # `reset()` — igual que el comportamiento de antes de este cambio.
        self.doc_source_ids: Dict[str, List[int]] = {}
        self._next_id = 0

        if FAISS_AVAILABLE:
            self.index = faiss.IndexIDMap2(faiss.IndexFlatL2(vector_dim))
        else:
            self.index = None

    def search(
        self,
        query_input: Union[List[float], str],
        top_k: int = 3,
        min_similarity: Optional[float] = None,
    ) -> List[str]:
        """Método de compatibilidad para el orquestador.
        Filtra las búsquedas vectoriales y devuelve solo las cadenas de texto.

        `min_similarity` (default `DEFAULT_RAG_MIN_SIMILARITY`): descarta
        los vecinos cuya similitud coseno con la consulta cae por debajo
        del piso — ver la nota de esa constante. `0` o negativo desactiva
        el filtro (comportamiento anterior).
        """
        if not FAISS_AVAILABLE or self.index is None or self.index.ntotal == 0:
            return []

        # Si el orquestador envía el vector de embedding
        if isinstance(query_input, list):
            raw_results = self.query(
                query_input, top_k=top_k, min_similarity=min_similarity
            )
            return [doc for doc, _score in raw_results]

        return []

    def reset(self) -> None:
        """
        Vacía el índice y los documentos acumulados, reiniciando el
        estado a como estaba justo tras __init__. Usado por
        Orchestrator.clear_conversation_memory() (ver orchestrator.py):
        sin esto, un hecho vectorizado en una sesión anterior (p. ej. un
        resultado de búsqueda web ya vencido o incorrecto, persistido
        vía Orchestrator._persist_web_knowledge) seguía siendo
        recuperable por fetch_hybrid_context() en cualquier conversación
        "nueva" posterior dentro del mismo proceso — este índice vive en
        memoria de proceso, no en el archivo SQLite de MemoryGraph, así
        que ningún DELETE sobre ese archivo lo tocaba.
        """
        self.documents = {}
        self.doc_source_ids = {}
        self._next_id = 0
        self.index = (
            faiss.IndexIDMap2(faiss.IndexFlatL2(self.vector_dim))
            if FAISS_AVAILABLE else None
        )

    def add_documents(
        self,
        docs: List[str],
        embeddings: List[List[float]],
        source_id: Optional[str] = None,
    ) -> int:
        """
        Agrega `docs` (ya chunkeados) con sus `embeddings` al índice.

        `source_id` (opcional, típicamente una ruta de archivo): si se
        pasa, los ids asignados quedan registrados bajo esa clave en
        `self.doc_source_ids`, para que una llamada posterior a
        `remove_source(source_id)` pueda sacarlos del índice — el caso
        de un archivo de workspace reindexado tras editarse. Sin
        `source_id` los chunks quedan en el índice hasta el próximo
        `reset()`, igual que el comportamiento previo a este cambio
        (usado por `_persist_web_knowledge`, que no rastrea por archivo).

        Devuelve la cantidad de chunks efectivamente agregados (0 si
        FAISS no está disponible o las listas vienen vacías/desparejas).
        """
        if not FAISS_AVAILABLE or not docs or not embeddings:
            return 0
        if len(docs) != len(embeddings):
            logger.warning(
                "add_documents: %d docs vs %d embeddings — se ignora el sobrante.",
                len(docs), len(embeddings),
            )
            docs = docs[: len(embeddings)]
            embeddings = embeddings[: len(docs)]

        matrix = np.array(embeddings, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[1] != self.vector_dim:
            raise ValueError(
                f"Dimensión incorrecta: se esperaba {self.vector_dim}, "
                f"se obtuvo {matrix.shape[1] if matrix.ndim == 2 else matrix.shape}"
            )

        ids = np.arange(self._next_id, self._next_id + len(docs), dtype=np.int64)
        self._next_id += len(docs)

        self.index.add_with_ids(matrix, ids)
        for doc_id, doc in zip(ids.tolist(), docs):
            self.documents[doc_id] = doc

        if source_id:
            self.doc_source_ids.setdefault(source_id, []).extend(ids.tolist())

        return len(docs)

    def remove_source(self, source_id: str) -> int:
        """
        Saca del índice todos los chunks agregados bajo `source_id` (ver
        `add_documents`) — el complemento de reindexar un archivo: antes
        de sumar sus chunks nuevos, `Orchestrator.index_document_for_rag`
        llama esto para que los viejos no queden como basura semántica
        flotando en el índice (ver el docstring de la clase). También es
        lo que usa `remove_document_from_rag` cuando un archivo de
        workspace se BORRA — ahí no hay nada nuevo que agregar, solo sacar
        lo viejo.

        Nunca lanza: `source_id` desconocido o FAISS no disponible
        devuelve 0 sin error, mismo criterio "nunca lanza" del resto de
        este archivo. Devuelve la cantidad de chunks removidos.
        """
        if not FAISS_AVAILABLE or self.index is None:
            return 0
        ids = self.doc_source_ids.pop(source_id, None)
        if not ids:
            return 0
        with contextlib.suppress(Exception):
            self.index.remove_ids(np.array(ids, dtype=np.int64))
        removed = 0
        for doc_id in ids:
            if self.documents.pop(doc_id, None) is not None:
                removed += 1
        return removed

    def query(
        self,
        query_embedding: List[float],
        top_k: int = 3,
        min_similarity: Optional[float] = None,
    ) -> List[Tuple[str, float]]:
        if not FAISS_AVAILABLE or self.index is None or self.index.ntotal == 0:
            return []

        thresh = (
            DEFAULT_RAG_MIN_SIMILARITY if min_similarity is None else min_similarity
        )

        query_matrix = np.array([query_embedding], dtype=np.float32)
        distances, indices = self.index.search(query_matrix, min(top_k, self.index.ntotal))

        results = []
        dropped = 0
        for dist, idx in zip(distances[0], indices[0]):
            # `idx` es el id que NOSOTROS asignamos en add_documents (no
            # una posición de array) porque el índice está envuelto en
            # IndexIDMap2 — por eso el lookup es por dict, no por lista.
            doc = self.documents.get(int(idx))
            if idx == -1 or doc is None:
                continue
            # dist es L2 al cuadrado (faiss.IndexFlatL2). Con vectores
            # normalizados, cos = 1 - dist/2. Si el índice quedó con
            # vectores sin normalizar, `similarity` sigue siendo monótona
            # (más cerca -> más alto), así que el piso igual filtra los
            # peores; solo pierde precisión de calibración.
            similarity = 1.0 - float(dist) / 2.0
            if thresh > 0 and similarity < thresh:
                dropped += 1
                continue
            results.append((doc, float(dist)))

        if dropped and not results:
            logger.debug(
                "RAG vectorial: %d vecino(s) descartado(s) por baja similitud "
                "(< %.2f) — el turno corre sin contexto vectorial.",
                dropped, thresh,
            )
        return results

    def save(self, path: Union[str, "os.PathLike[str]"]) -> bool:
        """
        Persiste el índice a disco en dos archivos junto a `path`:
        `<path>.faiss` (el índice FAISS binario, via `faiss.write_index`)
        y `<path>.meta.json` (todo lo demás que `search()`/`query()`/
        `remove_source()` necesitan para funcionar igual tras un reload:
        `documents`, `doc_source_ids`, `_next_id`, `vector_dim`).

        Sin esto el índice vive únicamente en memoria de proceso — cada
        reinicio de la app perdía la memoria sintética indexada (RAG de
        largo plazo, workspaces) aunque `sovnode_memory.db` (SQLite)
        sobreviviera intacto.

        Escritura atómica (escribe en `.tmp`, luego `os.replace`) para
        que un crash a mitad de escritura no deje un índice corrupto a
        medio guardar reemplazando uno bueno anterior. Nunca lanza —
        cualquier fallo de IO/serialización se loguea y devuelve False,
        mismo criterio "nunca lanza" del resto de este archivo: un fallo
        al guardar no debería tumbar el apagado de la app.
        """
        if not FAISS_AVAILABLE or self.index is None:
            return False

        path = str(path)
        index_path = f"{path}.faiss"
        meta_path = f"{path}.meta.json"
        tmp_index_path = f"{index_path}.tmp"
        tmp_meta_path = f"{meta_path}.tmp"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            faiss.write_index(self.index, tmp_index_path)
            meta = {
                "vector_dim": self.vector_dim,
                "next_id": self._next_id,
                # Las claves de un dict JSON son siempre str; se
                # reconvierten a int en load().
                "documents": {str(k): v for k, v in self.documents.items()},
                "doc_source_ids": self.doc_source_ids,
            }
            with open(tmp_meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            os.replace(tmp_index_path, index_path)
            os.replace(tmp_meta_path, meta_path)
            return True
        except Exception:
            logger.exception(
                "LocalVectorRAG.save: fallo al persistir índice en %s — "
                "se descarta el intento, el índice en memoria no se ve afectado.",
                path,
            )
            for tmp in (tmp_index_path, tmp_meta_path):
                with contextlib.suppress(Exception):
                    if os.path.exists(tmp):
                        os.remove(tmp)
            return False

    def load(self, path: Union[str, "os.PathLike[str]"]) -> bool:
        """
        Carga un índice previamente guardado con `save()`, reemplazando
        el estado actual (`index`, `documents`, `doc_source_ids`,
        `_next_id`) por el persistido.

        Degrada a "no hacer nada" (el objeto queda como estaba, vacío
        recién creado por `__init__`) ante CUALQUIER problema: archivos
        inexistentes (primer arranque de la app, o memoria nunca
        guardada), JSON/índice corrupto, o un `vector_dim` persistido
        que no coincide con `self.vector_dim` (p. ej. el modelo de
        embeddings cambió) — nunca lanza, nunca deja el objeto en un
        estado a medio cargar mezclando índice viejo con metadata nueva
        o viceversa. Devuelve True solo si el índice se restauró con
        éxito.
        """
        if not FAISS_AVAILABLE:
            return False

        path = str(path)
        index_path = f"{path}.faiss"
        meta_path = f"{path}.meta.json"
        if not os.path.exists(index_path) or not os.path.exists(meta_path):
            return False

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            stored_dim = int(meta.get("vector_dim", -1))
            if stored_dim != self.vector_dim:
                logger.warning(
                    "LocalVectorRAG.load: vector_dim persistido (%d) no "
                    "coincide con el actual (%d) en %s — se descarta el "
                    "índice guardado y arranca vacío.",
                    stored_dim, self.vector_dim, path,
                )
                return False
            index = faiss.read_index(index_path)
            documents = {int(k): v for k, v in meta.get("documents", {}).items()}
            doc_source_ids = {
                str(k): [int(i) for i in v]
                for k, v in meta.get("doc_source_ids", {}).items()
            }
            next_id = int(meta.get("next_id", 0))
        except Exception:
            logger.exception(
                "LocalVectorRAG.load: fallo al cargar índice desde %s — "
                "arranca vacío en vez de un estado parcial/corrupto.",
                path,
            )
            return False

        self.index = index
        self.documents = documents
        self.doc_source_ids = doc_source_ids
        self._next_id = next_id
        logger.info(
            "LocalVectorRAG.load: %d documento(s) restaurados desde %s",
            len(self.documents), path,
        )
        return True


# =============================================================================
# Orquestación multi-índice — extraído de Orchestrator (Ítem 3 del "Plan de
# Acción", 2026-09-02: modularizar orchestrator.py, primer slice = RAG).
#
# fetch_hybrid_context / index_document_for_rag / remove_document_from_rag /
# el re-ranking con penalización sintética / el guardado combinado de los 3
# índices vivían como ~230 líneas de métodos de Orchestrator, operando sobre
# self.vector_rag / self.workspace_vector_rag / self.longterm_vector_rag /
# self._vector_rag_lock. Se mueven ACÁ como funciones sueltas que reciben
# esos mismos objetos como parámetros explícitos — deliberadamente NO una
# clase con estado propio (tipo "RAGEngine"): Orchestrator sigue siendo el
# ÚNICO dueño de sus 3 índices y su lock, construidos donde siempre
# (Orchestrator.__init__), así que:
#   (a) KnowledgeSynthesizer, que los toca directo vía
#       getattr(orch, "longterm_vector_rag", None) / "_vector_rag_lock", no
#       necesita ningún cambio;
#   (b) los tests existentes que arman un Orchestrator a mano con
#       object.__new__(Orchestrator) y setean estos atributos directo sobre
#       la instancia (ver tests/test_regressions.py secciones 25/27) siguen
#       funcionando sin tocarlos — nunca dependieron de un `self.rag_engine`
#       que no existiría en un stub así;
#   (c) sovnode_qt.py sigue llamando exactamente los mismos métodos con la
#       misma firma (Orchestrator.index_document_for_rag/
#       remove_document_from_rag/save_vector_indices) — Orchestrator solo
#       pasa a tener wrappers de una línea que delegan acá.
# =============================================================================

def rerank_context_candidates(
    query: str,
    candidates: List[str],
    synthetic_texts: Optional[set] = None,
    synthetic_penalty: int = 1,
) -> List[str]:
    """
    Re-rankea por relevancia real a `query` los candidatos ya fusionados de
    fetch_hybrid_context (FTS5 + FAISS) — sin esto, el orden final era
    literalmente "todo lo de FTS5, después todo lo de FAISS", sin ninguna
    señal de qué fragmento responde mejor a ESTA consulta puntual antes de
    que la poda por max_context_chars empiece a descartar candidatos por el
    final de la lista.

    Heurística 100% local y determinista (mismo criterio de relevance.py que
    ya comparten web_search.py/verification.py/sovnode_qt.py — sin modelo
    adicional ni llamada a Ollama): solapamiento de palabras significativas
    entre la consulta y cada candidato. Empate a favor del índice original
    (orden de llegada) para que dos candidatos con exactamente el mismo
    score no salten de posición de forma inestable entre turnos.

    `synthetic_texts` (opcional — candidatos originados en
    longterm_vector_rag, ver fetch_hybrid_context): a cada uno se le resta
    `synthetic_penalty` palabras de solapamiento antes de rankear. La
    información PRIMARIA del usuario (turnos reales, archivos de workspace)
    tiene que ganar cualquier empate real de relevancia contra una conexión
    que el sistema mismo se infirió en inactividad; un axioma sintético solo
    desplaza a una fuente primaria cuando es CLARAMENTE más relevante
    (overlap suficientemente mayor para absorber la penalización), no ante
    un empate.

    Degrada a devolver `candidates` sin tocar ante cualquier fallo (p. ej.
    import circular con relevance.py en algún orden de carga inusual) —
    nunca debe romper fetch_hybrid_context().
    """
    if len(candidates) <= 1:
        return candidates

    with contextlib.suppress(Exception):
        from relevance import significant_words

        query_words = significant_words(query)
        if not query_words:
            return candidates

        penalty_texts = synthetic_texts or set()

        def _score(item: Tuple[int, str]) -> Tuple[int, int]:
            idx, text = item
            overlap = len(query_words & significant_words(text))
            if text in penalty_texts:
                overlap -= synthetic_penalty
            return (-overlap, idx)

        ranked = sorted(enumerate(candidates), key=_score)
        return [text for _idx, text in ranked]

    return candidates


def longterm_doc_to_node_id_map(longterm_vector_rag: Optional["LocalVectorRAG"]) -> Dict[str, str]:
    """
    Mapa inverso texto-de-axioma -> node_id_ref para `longterm_vector_rag`.
    Cada axioma sintético se indexó ahí con `source_id=node.node_id` (un
    chunk por nodo, sin partir — ver
    KnowledgeSynthesizer._index_for_retrieval), así que el mapeo es 1:1.
    Usado únicamente por el hook de reutilización de fetch_hybrid_context()
    para saber a qué `node_id_ref` marcarle `mark_synthetic_knowledge_reused()`
    cuando su texto sobrevive hasta el contexto final de un turno real.
    """
    if longterm_vector_rag is None:
        return {}
    mapping: Dict[str, str] = {}
    for source_id, chunk_ids in getattr(longterm_vector_rag, "doc_source_ids", {}).items():
        for chunk_id in chunk_ids:
            text = longterm_vector_rag.documents.get(chunk_id)
            if text:
                mapping[text] = source_id
    return mapping


def fetch_hybrid_context(
    user_input: str,
    *,
    memory_graph: Any,
    vector_rag: Optional["LocalVectorRAG"],
    workspace_vector_rag: Optional["LocalVectorRAG"],
    longterm_vector_rag: Optional["LocalVectorRAG"],
    lock: Any,
    limit: int = 3,
    max_context_chars: int = 1200,
) -> str:
    """Combina recuperación FTS5 y vectorial, re-rankeando por relevancia antes de recortar el contexto."""
    # 1. Recuperación por palabras clave en SQLite (FTS5)
    lexical_results = []
    if memory_graph:
        lexical_results = memory_graph.fetch_relevant_context(user_input, limit=limit)

    # 2. Búsqueda semántica si el módulo vectorial está presente.
    # LocalVectorRAG.search() espera un vector de embedding, no texto
    # crudo: hay que vectorizar la consulta antes de invocarlo.
    #
    # DOBLE FILTRO sobre los hits vectoriales (bug real 2026-09-01:
    # "explicá cómo funciona la fotosíntesis" trajo una charla vieja de
    # ecuaciones de matemática y qwen2.5:7b la continuó en chino):
    #   a) piso de similitud coseno dentro de search() — ver
    #      DEFAULT_RAG_MIN_SIMILARITY más arriba en este archivo.
    #   b) además, gate léxico determinista acá: si la consulta tiene
    #      palabras significativas, un chunk que no comparte NI UNA se
    #      descarta. Los resultados FTS5 no pasan por esto: ya
    #      matchearon por keyword (AND implícito), son de fiar.
    def _semantic_search(store, query_vector) -> List[str]:
        """
        Busca en UN store vectorial y aplica el mismo gate léxico
        determinista que ya protegía a `vector_rag` (bug real 2026-09-01,
        ver la nota de arriba) — factorizado para no duplicarlo entre
        `vector_rag` (conocimiento de sesión/web) y `workspace_vector_rag`
        (archivos de workspace, ver más abajo), que ahora se consultan
        los dos.
        """
        with lock:
            raw = store.search(query_vector, top_k=limit)
        if not raw:
            return []
        filtered = raw
        with contextlib.suppress(Exception):
            from relevance import significant_words
            query_sig = significant_words(user_input)
            if query_sig:
                filtered = [doc for doc in raw if query_sig & significant_words(doc)]
                if not filtered:
                    logger.debug(
                        "RAG: %d hit(s) vectorial(es) descartado(s) por cero "
                        "solapamiento léxico con la consulta.", len(raw),
                    )
        return filtered

    vector_results = []
    query_vector = None
    if (
        (vector_rag is not None and getattr(vector_rag, "index", None) is not None)
        or (workspace_vector_rag is not None and getattr(workspace_vector_rag, "index", None) is not None)
        or (longterm_vector_rag is not None and getattr(longterm_vector_rag, "index", None) is not None)
    ):
        query_vector = get_embedding(user_input)

    synthetic_results: List[str] = []
    if query_vector is not None:
        if vector_rag is not None and getattr(vector_rag, "index", None) is not None:
            vector_results.extend(_semantic_search(vector_rag, query_vector))
        # `workspace_vector_rag`: índice SEPARADO de `vector_rag` a
        # propósito (ver la nota junto a su creación en Orchestrator.__init__)
        # — los archivos de un workspace deben sobrevivir a
        # `clear_conversation_memory()` (nuevo chat/pestaña), que sí vacía
        # `vector_rag` (conocimiento de sesión/web, efímero por diseño).
        # Compartir un solo índice para ambos hubiera significado perder el
        # workspace indexado cada vez que se abre una pestaña nueva de chat.
        if workspace_vector_rag is not None and getattr(workspace_vector_rag, "index", None) is not None:
            vector_results.extend(_semantic_search(workspace_vector_rag, query_vector))
        # `longterm_vector_rag`: axiomas sintéticos propuestos por
        # KnowledgeSynthesizer en inactividad (ver knowledge_synthesizer.py).
        # Se consultan igual que los otros dos stores, pero se guarda aparte
        # en `synthetic_results` para (a) aplicarles una penalización de
        # ranking más abajo — la información PRIMARIA del usuario (turnos
        # reales, archivos de workspace) tiene que ganar un empate de
        # relevancia, un axioma sintético solo debería desplazarla si es
        # CLARAMENTE más relevante — y (b) marcar reuso
        # (MemoryGraph.mark_synthetic_knowledge_reused) sobre los que
        # efectivamente entren al contexto de este turno: sin eso,
        # `use_count` nunca subiría y la purga mensual terminaría borrando
        # nodos sintéticos aunque SÍ se estuvieran usando.
        if longterm_vector_rag is not None and getattr(longterm_vector_rag, "index", None) is not None:
            synthetic_results = _semantic_search(longterm_vector_rag, query_vector)
            vector_results.extend(synthetic_results)
    synthetic_texts = set(synthetic_results)

    # 3. Deduplicación conservando orden de llegada como desempate
    deduped = list(dict.fromkeys(lexical_results + vector_results))

    # 4. Re-ranking del conjunto FUSIONADO. FTS5 y FAISS ya rankean cada uno
    # DENTRO de su propia fuente, pero concatenar sin más (el orden anterior)
    # no dice nada sobre relevancia CRUZADA: un resultado #3 de FAISS puede
    # describir mejor esta consulta puntual que el #1 de FTS5. Se re-ordena
    # por solapamiento léxico real contra user_input en vez de confiar en
    # qué motor lo trajo primero — ver rerank_context_candidates().
    combined = rerank_context_candidates(
        user_input, deduped, synthetic_texts=synthetic_texts
    )

    # 4b. Hook de reutilización: cualquier axioma sintético que haya
    # sobrevivido la deduplicación + el re-ranking (penalizado) hasta acá SÍ
    # va a formar parte del contexto de este turno real — se marca como
    # reusado en MemoryGraph. Va envuelto en contextlib.suppress a
    # propósito: esto es contabilidad de soporte (alimenta la purga
    # mensual), nunca debe poder tumbar un turno real si algo sale mal acá.
    if synthetic_texts and memory_graph:
        with contextlib.suppress(Exception):
            node_id_map = longterm_doc_to_node_id_map(longterm_vector_rag)
            for text in combined:
                if text in synthetic_texts:
                    node_id = node_id_map.get(text)
                    if node_id:
                        memory_graph.mark_synthetic_knowledge_reused(node_id)

    context_text = "\n---\n".join(combined)

    # 5. Poda adaptativa según límite de caracteres
    if len(context_text) > max_context_chars:
        return context_text[:max_context_chars] + "\n...[Contexto truncado por límite de ventana]"

    return context_text


def index_document_for_rag(
    filename: str,
    content: str,
    *,
    workspace_vector_rag: Optional["LocalVectorRAG"],
    lock: Any,
) -> int:
    """
    Indexa un documento local (soltado en el chat, o descubierto por
    WorkspaceWatcherWorker en una carpeta de "Workspaces") en
    `workspace_vector_rag` para que fetch_hybrid_context() pueda
    recuperarlo en turnos FUTUROS — antes, un archivo .py arrastrado a la
    ventana solo se inyectaba tal cual en el prompt del turno actual (ver
    PromptTextEdit.dropEvent en sovnode_qt.py) y se perdía apenas terminaba
    ese turno.

    `filename` se usa como `source_id`: antes de vectorizar, se borra
    cualquier chunk previamente indexado bajo ese mismo id
    (`remove_source`). Esto vuelve la operación IDEMPOTENTE — llamarla de
    nuevo tras editar el archivo reemplaza sus chunks viejos en vez de
    acumular duplicados stale para siempre, que es justo el hueco que
    dejaba el `IndexFlatL2` original (solo append, sin borrado) y que
    motivó pasar `LocalVectorRAG` a `IndexIDMap2`. Pasar SIEMPRE una ruta
    completa y estable como `filename` (no solo el basename) para que dos
    archivos de igual nombre en carpetas distintas no compartan/pisen su
    `source_id`.

    Chunking vía chunk_document() (más arriba en este archivo): AST
    completo (función/clase enteras) para `.py`/`.pyw`, splitter de
    párrafos para todo lo demás — nunca cortes rígidos por número de
    caracteres a mitad de una función.

    Devuelve la cantidad de chunks efectivamente indexados (0 si no hay
    índice vectorial disponible, el documento viene vacío, o algo falla —
    nunca lanza, mismo criterio que el resto de este archivo).
    """
    if not workspace_vector_rag:
        return 0
    if not content or not content.strip():
        return 0

    try:
        chunks = chunk_document(filename, content)
        if not chunks:
            return 0

        vectors = []
        valid_chunks = []
        for chunk in chunks:
            vector = get_embedding(chunk)
            if vector is not None:
                vectors.append(vector)
                valid_chunks.append(chunk)

        if not valid_chunks:
            return 0

        with lock:
            workspace_vector_rag.remove_source(filename)
            workspace_vector_rag.add_documents(
                valid_chunks, vectors, source_id=filename
            )
        logger.info(
            "📚 [RAG] '%s' indexado: %d chunk(s) (%s).",
            filename, len(valid_chunks),
            "AST" if filename.lower().endswith((".py", ".pyw")) else "texto",
        )
        return len(valid_chunks)
    except Exception as exc:
        logger.warning("index_document_for_rag('%s') degradado: %s", filename, exc)
        return 0


def remove_document_from_rag(
    filename: str,
    *,
    workspace_vector_rag: Optional["LocalVectorRAG"],
    lock: Any,
) -> int:
    """
    Contraparte de index_document_for_rag() para eventos de borrado
    detectados por WorkspaceWatcherWorker (archivo eliminado o carpeta
    quitada de "Workspaces"): retira del índice vectorial todos los chunks
    indexados bajo `filename` como `source_id`, sin dejar residuo
    recuperable por fetch_hybrid_context(). Nunca lanza — mismo criterio
    que el resto de las operaciones de RAG en este archivo; un fallo aquí
    solo se loguea, nunca interrumpe el watcher.
    """
    if not workspace_vector_rag:
        return 0
    try:
        with lock:
            removed = workspace_vector_rag.remove_source(filename)
        if removed:
            logger.info(
                "🗑️ [RAG] '%s' retirado del índice de workspace: %d chunk(s).",
                filename, removed,
            )
        return removed
    except Exception as exc:
        logger.warning("remove_document_from_rag('%s') degradado: %s", filename, exc)
        return 0


def save_vector_indices(
    *,
    workspace_vector_rag: Optional["LocalVectorRAG"],
    longterm_vector_rag: Optional["LocalVectorRAG"],
    workspace_path: str,
    longterm_path: str,
    lock: Any,
) -> Dict[str, bool]:
    """
    Persiste a disco `workspace_vector_rag` y `longterm_vector_rag` (ver
    LocalVectorRAG.save/load más arriba en este archivo, y la carga
    simétrica en Orchestrator.__init__ justo después de crear
    `_vector_rag_lock`).

    DELIBERADAMENTE NO recibe `vector_rag`: ese store es de ámbito de
    SESIÓN (ver Orchestrator.clear_conversation_memory) — persistirlo
    entre reinicios del PROCESO reviviría, en cada arranque, contexto de
    una conversación que el usuario ya cerró.

    Pensado para llamarse desde el cierre ordenado de la app
    (MainWindow._quit_application, sovnode_qt.py) antes de que el proceso
    termine — sin esto, `longterm_vector_rag` en particular (axiomas
    sintéticos de KnowledgeSynthesizer) no tiene NINGÚN otro respaldo en
    disco: a diferencia de `workspace_vector_rag` (que un reinicio puede
    reconstruir re-escaneando los workspaces, con costo de re-embeber pero
    sin pérdida de datos), el texto+embedding de un axioma sintético vive
    ÚNICAMENTE en este índice FAISS en memoria de proceso — perderlo es
    irrecuperable.

    Nunca lanza (cada `.save()` individual ya nunca lanza; esta función
    además envuelve la llamada por las dudas, para que un cierre de app
    nunca se vea interrumpido por esto). Devuelve qué índices se guardaron
    con éxito, solo para logging/diagnóstico del llamador.
    """
    results: Dict[str, bool] = {"workspace": False, "longterm": False}
    try:
        with lock:
            results["workspace"] = workspace_vector_rag.save(workspace_path)
            results["longterm"] = longterm_vector_rag.save(longterm_path)
    except Exception as exc:
        logger.warning("No se pudieron persistir los índices vectoriales al cerrar: %s", exc)
        return results

    logger.info(
        "Índices vectoriales persistidos — workspace: %s, longterm: %s",
        "OK" if results["workspace"] else "FALLO",
        "OK" if results["longterm"] else "FALLO",
    )
    return results