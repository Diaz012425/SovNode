"""
SovNode — Local FAISS Vector Store
==================================
Sistema RAG ligero para indexación y recuperación semántica local.
"""

from __future__ import annotations

import numpy as np
from typing import List, Tuple, Union

try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False


class LocalVectorRAG:
    def __init__(self, vector_dim: int = 384) -> None:
        self.vector_dim = vector_dim
        self.documents: List[str] = []
        
        if FAISS_AVAILABLE:
            self.index = faiss.IndexFlatL2(vector_dim)
        else:
            self.index = None

    def search(self, query_input: Union[List[float], str], top_k: int = 3) -> List[str]:
        """Método de compatibilidad para el orquestador.
        Filtra las búsquedas vectoriales y devuelve solo las cadenas de texto.
        """
        if not FAISS_AVAILABLE or self.index is None or self.index.ntotal == 0:
            return []

        # Si el orquestador envía el vector de embedding
        if isinstance(query_input, list):
            raw_results = self.query(query_input, top_k=top_k)
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
        self.documents = []
        self.index = faiss.IndexFlatL2(self.vector_dim) if FAISS_AVAILABLE else None

    def add_documents(self, docs: List[str], embeddings: List[List[float]]) -> None:
        if not FAISS_AVAILABLE or not docs or not embeddings:
            return

        matrix = np.array(embeddings, dtype=np.float32)
        if matrix.shape[1] != self.vector_dim:
            raise ValueError(f"Dimensión incorrecta: se esperaba {self.vector_dim}, se obtuvo {matrix.shape[1]}")

        self.index.add(matrix)
        self.documents.extend(docs)

    def query(self, query_embedding: List[float], top_k: int = 3) -> List[Tuple[str, float]]:
        if not FAISS_AVAILABLE or self.index is None or self.index.ntotal == 0:
            return []

        query_matrix = np.array([query_embedding], dtype=np.float32)
        distances, indices = self.index.search(query_matrix, min(top_k, self.index.ntotal))

        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx != -1 and idx < len(self.documents):
                results.append((self.documents[idx], float(dist)))

        return results