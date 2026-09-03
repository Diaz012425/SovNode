"""
SovNode — Knowledge Synthesizer (síntesis de conocimiento en inactividad)
==========================================================================
Hilo de fondo, análogo a CognitiveGovernor, que durante la INACTIVIDAD del
usuario mina pares de nodos de conocimiento semánticamente próximos
(resultados de búsqueda web ya persistidos, lecciones meta-cognitivas) y
propone conexiones sintéticas entre ellos — axiomas que ningún turno real
pidió explícitamente, pero que se desprenden de cruzar dos fragmentos que
el sistema ya conoce por separado.

Diseño (resolución de la propuesta del usuario, 2026-09-02): reusa TODO lo
que ya existe en vez de construir un sistema de persistencia paralelo.

  - Disparo por inactividad: mismo patrón exacto que CognitiveGovernor.run()
    — `orchestrator._pause_governor_event.wait(timeout=interval)` (retorna
    de inmediato apenas arranca un turno real, en vez de esperar el
    intervalo completo) + chequeo de `_is_processing_turn` + acquire NO
    bloqueante de `_llm_lock` antes de invocar al juez, para nunca competir
    por la GPU/VRAM con un turno de usuario ni desalojar el `keep_alive`
    del modelo de respuesta.

  - Juez: `orchestrator.router_model` (qwen2.5:0.5b) a `ROUTER_LLM_
    TEMPERATURE` (0.0, determinista) — el mismo modelo ya usado para
    enrutar cada turno (`_llm_router_classify`), así que no suma un modelo
    nuevo a mantener caliente en VRAM.

  - Persistencia partida en dos capas, mismo principio que ya usa
    `Orchestrator._persist_knowledge_node_if_robust` +
    `MemoryGraph.validated_tools.use_count`:
      * INMUTABLE (el axioma y su verificación): `KnowledgeNode` en el WAL
        (`domain="synthetic_knowledge"`), vía `orchestrator._wal.
        append_knowledge_node()` — auditoría para siempre, nunca se edita.
      * MUTABLE (estado vivo — confidence_score, reuso, purga):
        `MemoryGraph.synthetic_knowledge`, referenciando el `node_id` del
        WAL por clave primaria (`node_id_ref`) en vez de duplicar el
        axioma.

  - Búsqueda futura: el axioma se indexa además en `orchestrator.
    longterm_vector_rag` (tercer índice FAISS, separado de `vector_rag`
    scope-sesión y `workspace_vector_rag` scope-archivos) bajo el mismo
    `_vector_rag_lock` que ya serializa las escrituras a los otros dos
    stores — para que `fetch_hybrid_context()` pueda recuperarlo en un
    turno real posterior, igual que cualquier otro documento vectorizado.

  - Validación en capas, barata-antes-que-cara (mismo principio ya
    documentado en la Sección 24 de tests/test_regressions.py: "las
    DETECCIONES son deterministas y baratas [...]; lo caro es la
    corrección" — acá, invocar al juez):
      1. Vecino geométrico por similitud coseno dentro de un pool acotado
         (ver `MAX_POOL_SIZE`) — sin FAISS, en Python puro, porque el pool
         es chico a propósito (ver más abajo el porqué del muestreo
         incremental).
      2. Pre-filtro léxico determinista ("grounding"): exige vocabulario
         sustantivo compartido entre los dos fragmentos ANTES de gastar
         una llamada al juez — mismo espíritu que el piso de similitud de
         `rag_faiss.DEFAULT_RAG_MIN_SIMILARITY` (evitar el falso vecino
         "cercano en el vector pero sin relación real", documentado ahí
         con un bug real medido).
      3. Juez LLM (T=0): responde JSON estricto con un axioma y una cita
         VERBATIM de cada fragmento. Validación geométrica: el axioma
         propuesto se embebe y se exige que quede semánticamente cerca de
         AMBOS fragmentos fuente (no solo que el juez haya obedecido el
         formato).
      4. Grounding por cita EXACTA (determinista, sin LLM): las citas del
         juez tienen que aparecer TEXTUALMENTE en su fragmento de origen —
         una cita parafraseada o alucinada no sobrevive un `in` de Python.

  - Muestreo incremental: en vez de re-comparar TODO el historial en cada
    ciclo (costo O(N²) que crece sin cota con el uso del programa — el
    problema concreto que este diseño evita), cada ciclo solo procesa los
    nodos agregados a `web_knowledge`/`reasoning_lessons` DESDE el ciclo
    anterior (`MemoryGraph.fetch_web_knowledge_since` /
    `fetch_reasoning_lessons_since`, cursor de timestamp propio de esta
    instancia) y los compara contra un pool acotado de los últimos
    `MAX_POOL_SIZE` nodos vistos — mismo espíritu que el debounce de
    `WorkspaceScanner` (workspace_watcher.py): reaccionar a lo nuevo, no
    reescanear todo history cada vez.

Nunca lanza hacia el hilo llamador: cualquier excepción en un ciclo queda
contenida y logueada (mismo criterio "nunca tumba el proceso" que
CognitiveGovernor y el resto de los verificadores locales de este
proyecto).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from embeddings import get_embedding
from wal import KnowledgeNode

logger = logging.getLogger("SovNode.KnowledgeSynthesizer")

# Vocabulario sin valor discriminativo para el pre-filtro léxico de
# grounding (Paso 2) — deliberadamente chico y aproximado: un falso
# negativo acá solo significa "no se gastó una llamada al juez en este
# par", el fallo seguro para un sistema que debe preferir NO sintetizar
# antes que sintetizar de más.
_STOPWORDS = {
    "para", "como", "pero", "esto", "esta", "estos", "estas", "sobre",
    "entre", "cuando", "donde", "porque", "también", "hacer", "puede",
    "tiene", "desde", "hasta", "cada", "otro", "otra", "mismo", "misma",
    "the", "and", "for", "with", "that", "this", "from", "have", "has",
    "are", "was", "were", "been", "their", "which", "about", "into",
    "than", "then", "when", "where", "what", "your", "these", "those",
}


def _cosine(a: Optional[List[float]], b: Optional[List[float]]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class KnowledgeSynthesizer(threading.Thread):
    """Ver el docstring del módulo para el diseño completo."""

    DOMAIN = "synthetic_knowledge"

    # Cuántos nodos NUEVOS (desde el último ciclo) se procesan como máximo
    # por pasada — cota dura independiente de cuántos hubo realmente, para
    # que un pico de actividad (muchas búsquedas web en poco tiempo) no
    # convierta un solo ciclo en un maratón de llamadas al juez.
    MAX_CANDIDATES_PER_CYCLE = 20
    # Llamadas al juez (el paso caro — HTTP bloqueante) que se permiten en
    # UN ciclo, aunque más candidatos hayan pasado el pre-filtro léxico.
    MAX_JUDGE_CALLS_PER_CYCLE = 5
    # Tamaño del pool de comparación (nodos recientes, con embedding ya
    # calculado) contra el que se busca el vecino geométrico de cada
    # candidato nuevo — acota el costo por candidato a O(MAX_POOL_SIZE)
    # en vez de O(N) sobre TODO el historial.
    MAX_POOL_SIZE = 200

    MIN_GEOMETRIC_SIMILARITY = float(
        os.getenv("SOVNODE_SYNTH_MIN_SIMILARITY", "0.55")
    )
    MIN_SHARED_TOKENS = 2
    # Piso de similitud coseno entre el axioma propuesto por el juez y
    # CADA fragmento fuente — parte de la "validación geométrica" (Paso 3):
    # un juez que obedece el formato JSON pero inventa una conexión sin
    # relación semántica real con alguno de los dos fragmentos no pasa.
    MIN_AXIOM_GROUNDING_SIMILARITY = 0.35
    JUDGE_NUM_PREDICT = 300
    PURGE_MAX_AGE_DAYS = 30.0

    _JUDGE_SYSTEM_PROMPT = (
        "Sos un juez lógico, estricto y conservador. Te muestro dos "
        "fragmentos de conocimiento (FRAGMENTO_A y FRAGMENTO_B) que un "
        "sistema de recuperación semántica marcó como próximos entre sí. "
        "Tu única tarea es decidir si existe una conexión conceptual REAL "
        "y no trivial entre ambos, y si la hay, expresarla en un axioma "
        "breve citando evidencia EXACTA (verbatim, copiada palabra por "
        "palabra) de cada fragmento.\n\n"
        "Respondé ÚNICAMENTE con un objeto JSON, sin texto alrededor, con "
        "esta forma exacta si hay conexión:\n"
        '{"connected": true, "axiom": "conexión breve en una oración", '
        '"quote_n1": "cita textual EXACTA copiada de FRAGMENTO_A", '
        '"quote_n2": "cita textual EXACTA copiada de FRAGMENTO_B", '
        '"confidence": 0.0}\n\n'
        "Si NO hay una conexión real (solo se parecen temáticamente pero "
        "no se relacionan de forma sustantiva), respondé exactamente:\n"
        '{"connected": false}\n\n'
        "Las citas tienen que ser copiadas TEXTUALMENTE, ni una palabra "
        "distinta, porque se validan por coincidencia EXACTA contra el "
        "fragmento original — una cita parafraseada hace que tu respuesta "
        "se descarte entera. Ante la duda, respondé connected: false."
    )

    def __init__(self, orchestrator, interval_seconds: int = 300) -> None:
        super().__init__(daemon=True, name="KnowledgeSynthesizer")
        self.orchestrator = orchestrator
        self.interval = interval_seconds
        self._running = True
        # Pool de comparación: lista de {key, domain, text, embedding}.
        # Vive SOLO en memoria de este hilo (como WorkspaceScanner._known)
        # — se reconstruye desde cero (vacío) en cada arranque de la app,
        # lo cual es intencional: no hace falta persistir el pool en sí,
        # solo lo que ya pasó las 4 capas de validación.
        self._pool: List[Dict[str, Any]] = []
        # Cursor de "último ciclo": arranca en el momento de construcción,
        # no en 0.0 — un backfill retroactivo de TODO el historial ya
        # persistido no es el objetivo (ver docstring del módulo, "muestreo
        # incremental"); este daemon reacciona a conocimiento NUEVO desde
        # que la app arrancó, igual que WorkspaceScanner solo reacciona a
        # eventos de archivo nuevos, no a un escaneo retroactivo completo.
        self._last_cycle_ts = time.time()
        # Pares (par no-ordenado de keys) ya evaluados EN EL CICLO ACTUAL
        # — se reinicia al arrancar cada _run_cycle(). Sin esto, un par
        # mutuamente-vecino-más-cercano (A elige a B como su vecino Y B
        # elige a A como el suyo, el caso común cuando ambos son muy
        # afines entre sí) gastaría DOS llamadas al juez sobre el mismo
        # par en vez de una — el mismo desperdicio, sobre el paso más
        # caro del pipeline, que MAX_JUDGE_CALLS_PER_CYCLE existe para
        # acotar.
        self._judged_pairs_this_cycle: set = set()

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Paso 1: disparo por inactividad (idéntico a CognitiveGovernor.run())
    # ------------------------------------------------------------------
    def run(self) -> None:
        logger.info("🧠 [KnowledgeSynthesizer] Bucle de síntesis en inactividad en línea.")
        pause_event = getattr(self.orchestrator, "_pause_governor_event", None)

        while self._running:
            if pause_event is not None:
                pause_event.wait(timeout=self.interval)
            else:
                time.sleep(self.interval)

            if getattr(self.orchestrator, "_is_processing_turn", False):
                continue

            llm_lock = getattr(self.orchestrator, "_llm_lock", None)
            if llm_lock is not None:
                if not llm_lock.acquire(blocking=False):
                    continue
                llm_lock.release()

            try:
                self._run_cycle()
            except Exception as exc:
                logger.error("Error en KnowledgeSynthesizer: %s", exc)

    # ------------------------------------------------------------------
    # Paso 2: muestreo incremental + emparejamiento geométrico
    # ------------------------------------------------------------------
    def _run_cycle(self) -> None:
        cycle_start = time.time()
        memory_graph = getattr(self.orchestrator, "memory_graph", None)
        if memory_graph is None:
            return

        with contextlib.suppress(Exception):
            purged = memory_graph.purge_stale_synthetic_knowledge(self.PURGE_MAX_AGE_DAYS)
            if purged:
                logger.info(
                    "🧹 [KnowledgeSynthesizer] %d nodo(s) sintético(s) purgado(s) "
                    "(>%.0f días sin reusarse).", len(purged), self.PURGE_MAX_AGE_DAYS,
                )
                self._purge_vectors(purged)

        self._judged_pairs_this_cycle = set()
        candidates = self._collect_new_candidates(memory_graph)
        self._refresh_pool(candidates)
        # Marca de avance SIEMPRE — incluso sin candidatos nuevos, para no
        # reprocesar la misma ventana vacía de tiempo en el próximo ciclo.
        self._last_cycle_ts = cycle_start

        if len(self._pool) < 2 or not candidates:
            return

        judge_calls = 0
        for candidate in candidates:
            if getattr(self.orchestrator, "_is_processing_turn", False):
                # Un turno real empezó a mitad del ciclo: ceder de
                # inmediato, el resto de los candidatos se procesan en el
                # próximo ciclo (no se pierden — ya quedaron en el pool).
                break
            if judge_calls >= self.MAX_JUDGE_CALLS_PER_CYCLE:
                break
            if self._process_candidate(candidate, memory_graph):
                judge_calls += 1

    def _collect_new_candidates(self, memory_graph) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        with contextlib.suppress(Exception):
            for row in memory_graph.fetch_web_knowledge_since(self._last_cycle_ts):
                candidates.append({
                    "key": f"web:{row['id']}", "domain": "web_knowledge",
                    "text": row["content"],
                })
        with contextlib.suppress(Exception):
            for row in memory_graph.fetch_reasoning_lessons_since(self._last_cycle_ts):
                candidates.append({
                    "key": f"lesson:{row['id']}", "domain": "reasoning_lessons",
                    "text": row["content"],
                })
        return candidates[: self.MAX_CANDIDATES_PER_CYCLE]

    def _refresh_pool(self, candidates: List[Dict[str, Any]]) -> None:
        existing_keys = {p["key"] for p in self._pool}
        for candidate in candidates:
            if candidate["key"] in existing_keys or not candidate.get("text"):
                continue
            vector = get_embedding(candidate["text"])
            if vector is None:
                continue
            self._pool.append({**candidate, "embedding": vector})
            existing_keys.add(candidate["key"])
        if len(self._pool) > self.MAX_POOL_SIZE:
            self._pool = self._pool[-self.MAX_POOL_SIZE:]

    def _find_best_neighbor(self, node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        best: Optional[Dict[str, Any]] = None
        best_sim = 0.0
        for other in self._pool:
            if other["key"] == node["key"]:
                continue
            sim = _cosine(node.get("embedding"), other.get("embedding"))
            if sim > best_sim:
                best_sim, best = sim, other
        if best is None or best_sim < self.MIN_GEOMETRIC_SIMILARITY:
            return None
        return {**best, "similarity": best_sim}

    # ------------------------------------------------------------------
    # Paso 3: pre-filtro léxico (grounding barato) + juez + validación
    # geométrica del axioma + grounding por cita exacta
    # ------------------------------------------------------------------
    def _process_candidate(self, candidate: Dict[str, Any], memory_graph) -> bool:
        """Devuelve True si se gastó una llamada al juez (para el tope
        MAX_JUDGE_CALLS_PER_CYCLE), independientemente de si terminó
        persistiendo un nodo o no."""
        n1 = next((p for p in self._pool if p["key"] == candidate["key"]), None)
        if n1 is None or n1.get("embedding") is None:
            return False

        n2 = self._find_best_neighbor(n1)
        if n2 is None:
            return False

        pair_key = frozenset((n1["key"], n2["key"]))
        if pair_key in self._judged_pairs_this_cycle:
            return False
        self._judged_pairs_this_cycle.add(pair_key)

        if not self._passes_lexical_grounding(n1["text"], n2["text"]):
            return False

        verdict = self._ask_judge(n1["text"], n2["text"])
        if verdict is None:
            return True  # llamada gastada aunque no haya dado veredicto usable

        if not self._passes_citation_check(verdict, n1["text"], n2["text"]):
            return True

        if not self._passes_geometric_validation(verdict, n1, n2):
            return True

        self._persist(verdict, n1, n2, memory_graph)
        return True

    @classmethod
    def _significant_tokens(cls, text: str) -> set:
        words = re.findall(r"[a-záéíóúñü]{4,}", (text or "").lower())
        return {w for w in words if w not in _STOPWORDS}

    def _passes_lexical_grounding(self, text_a: str, text_b: str) -> bool:
        shared = self._significant_tokens(text_a) & self._significant_tokens(text_b)
        return len(shared) >= self.MIN_SHARED_TOKENS

    def _ask_judge(self, text_a: str, text_b: str) -> Optional[Dict[str, Any]]:
        orch = self.orchestrator
        prompt = (
            f"FRAGMENTO_A:\n{text_a[:800]}\n\n"
            f"FRAGMENTO_B:\n{text_b[:800]}\n\n"
            "¿Existe una conexión conceptual real entre A y B? "
            "Respondé con el JSON indicado, nada más."
        )
        try:
            raw = orch._call_llm(
                prompt,
                target_model=getattr(orch, "router_model", None),
                temperature_override=getattr(orch, "ROUTER_LLM_TEMPERATURE", 0.0),
                num_predict_override=self.JUDGE_NUM_PREDICT,
                system_override=self._JUDGE_SYSTEM_PROMPT,
                perf_label="KnowledgeSynthesizer-Judge",
            )
        except Exception as exc:
            logger.debug("🧠 [KnowledgeSynthesizer] juez falló: %s", exc)
            return None

        if not raw or raw.lstrip().startswith("[ERROR"):
            return None
        return self._extract_verdict_json(raw)

    @staticmethod
    def _extract_verdict_json(raw: str) -> Optional[Dict[str, Any]]:
        """
        Extrae el veredicto JSON del juez, tolerando texto alrededor o
        comillas simples. Misma estrategia en capas que
        RobustJSONParser.extract_and_repair (bloque ```json```, luego
        primer '{' a último '}', luego reparación liviana de comillas y
        comas colgantes) pero generalizada: el veredicto del juez usa la
        clave 'connected', no 'tool', así que RobustJSONParser (que exige
        'tool' explícitamente) no aplica tal cual acá.
        """
        if not raw or not raw.strip():
            return None

        candidates = []
        code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if code_block:
            candidates.append(code_block.group(1))
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidates.append(raw[start : end + 1])

        for candidate in candidates:
            for text in (candidate, candidate.replace("'", '"')):
                repaired = re.sub(r",\s*\}", "}", text)
                try:
                    data = json.loads(repaired)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and "connected" in data:
                    return data
        return None

    def _passes_citation_check(
        self, verdict: Dict[str, Any], text_a: str, text_b: str
    ) -> bool:
        if not verdict.get("connected"):
            return False
        quote_a = str(verdict.get("quote_n1") or "").strip()
        quote_b = str(verdict.get("quote_n2") or "").strip()
        axiom = str(verdict.get("axiom") or "").strip()
        if not quote_a or not quote_b or not axiom:
            return False
        # Coincidencia EXACTA, no difusa: una cita parafraseada o
        # alucinada no aparece como substring literal del fragmento
        # fuente — este es el "Paso de grounding determinista" pedido
        # por el usuario, sin ningún llamado adicional al modelo.
        return quote_a in text_a and quote_b in text_b

    def _passes_geometric_validation(
        self, verdict: Dict[str, Any], n1: Dict[str, Any], n2: Dict[str, Any]
    ) -> bool:
        axiom = str(verdict.get("axiom") or "")
        axiom_vector = get_embedding(axiom)
        if axiom_vector is None:
            return False
        sim_a = _cosine(axiom_vector, n1.get("embedding"))
        sim_b = _cosine(axiom_vector, n2.get("embedding"))
        verdict["_axiom_embedding"] = axiom_vector
        verdict["_sim_a"] = sim_a
        verdict["_sim_b"] = sim_b
        return (
            sim_a >= self.MIN_AXIOM_GROUNDING_SIMILARITY
            and sim_b >= self.MIN_AXIOM_GROUNDING_SIMILARITY
        )

    # ------------------------------------------------------------------
    # Paso 4: cuarentena — WAL (inmutable) + MemoryGraph (estado vivo) +
    # indexación en longterm_vector_rag para recuperación futura
    # ------------------------------------------------------------------
    def _persist(
        self, verdict: Dict[str, Any], n1: Dict[str, Any], n2: Dict[str, Any], memory_graph
    ) -> None:
        orch = self.orchestrator
        axiom = str(verdict.get("axiom") or "").strip()
        if not axiom:
            return

        verification: Dict[str, Any] = {
            "geometric_similarity_pair": n2.get("similarity"),
            "axiom_similarity_n1": verdict.get("_sim_a"),
            "axiom_similarity_n2": verdict.get("_sim_b"),
            "judge_confidence": verdict.get("confidence"),
            "quote_n1": verdict.get("quote_n1"),
            "quote_n2": verdict.get("quote_n2"),
        }
        provenance: Dict[str, Any] = {
            "engine": "KnowledgeSynthesizer",
            "produced_by": getattr(orch, "router_model", "?"),
            "source_keys": [n1["key"], n2["key"]],
            "persisted_at": time.time(),
        }

        node = KnowledgeNode.create(
            domain=self.DOMAIN, axiom=axiom,
            verification=verification, provenance=provenance,
        )

        wal = getattr(orch, "_wal", None)
        if wal is None:
            return
        try:
            wal.append_knowledge_node(node)
        except Exception as exc:
            logger.warning("🧠 [KnowledgeSynthesizer] no se pudo persistir en WAL: %s", exc)
            return

        confidence = self._compute_confidence(verdict, n2)
        with contextlib.suppress(Exception):
            memory_graph.add_synthetic_knowledge(node.node_id, confidence)

        self._index_for_retrieval(node, verdict.get("_axiom_embedding"))

        logger.info("🧠 [KnowledgeSynthesizer] nodo sintético persistido: %s", node)

    def _index_for_retrieval(self, node: KnowledgeNode, axiom_vector) -> None:
        if axiom_vector is None:
            return
        orch = self.orchestrator
        longterm = getattr(orch, "longterm_vector_rag", None)
        if longterm is None:
            return
        lock = getattr(orch, "_vector_rag_lock", None)
        ctx = lock if lock is not None else contextlib.nullcontext()
        with ctx, contextlib.suppress(Exception):
            longterm.add_documents([node.axiom], [axiom_vector], source_id=node.node_id)

    def _purge_vectors(self, purged_node_id_refs: List[str]) -> None:
        """Complemento de la purga mensual de MemoryGraph: retira también
        el vector correspondiente de `longterm_vector_rag` (indexado con
        `source_id=node_id` en `_index_for_retrieval`), para que el índice
        FAISS no acumule para siempre vectores de nodos ya purgados del
        estado vivo — el mismo problema de "basura semántica flotando"
        que `LocalVectorRAG.remove_source` ya resuelve para archivos de
        workspace reindexados."""
        orch = self.orchestrator
        longterm = getattr(orch, "longterm_vector_rag", None)
        if longterm is None:
            return
        lock = getattr(orch, "_vector_rag_lock", None)
        ctx = lock if lock is not None else contextlib.nullcontext()
        with ctx:
            for node_id_ref in purged_node_id_refs:
                with contextlib.suppress(Exception):
                    longterm.remove_source(node_id_ref)

    @staticmethod
    def _compute_confidence(verdict: Dict[str, Any], n2: Dict[str, Any]) -> float:
        parts = [
            float(n2.get("similarity") or 0.0),
            float(verdict.get("_sim_a") or 0.0),
            float(verdict.get("_sim_b") or 0.0),
        ]
        judge_conf = verdict.get("confidence")
        if isinstance(judge_conf, (int, float)):
            parts.append(max(0.0, min(1.0, float(judge_conf))))
        return sum(parts) / len(parts) if parts else 0.0
