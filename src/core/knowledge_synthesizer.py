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
SovNode - Knowledge Synthesizer

ES: Hilo de fondo (mismo patrón que CognitiveGovernor) que, durante la
inactividad del usuario, compara pares de nodos de conocimiento
semánticamente próximos (resultados web ya persistidos, lecciones
meta-cognitivas) y propone conexiones sintéticas entre ellos mediante un
juez LLM. Reutiliza infraestructura existente en vez de duplicarla: mismo
patrón de espera por inactividad, el mismo modelo router como juez,
persistencia partida en una capa inmutable (WAL) y una capa mutable
(MemoryGraph), e indexación en un tercer índice FAISS
(`longterm_vector_rag`) para recuperación futura.

La validación es en capas, de más barata a más cara: primero un vecino
geométrico por similitud coseno dentro de un pool acotado; luego un
pre-filtro léxico que exige vocabulario compartido; recién ahí se invoca al
juez LLM (temperatura 0); y por último un grounding determinista: las citas
del juez deben aparecer TEXTUALMENTE en el fragmento de origen, y el axioma
propuesto debe quedar semánticamente cerca de ambos fragmentos fuente. El
muestreo es incremental (solo nodos nuevos desde el último ciclo) para
evitar un costo O(N²) sobre todo el historial acumulado. Ninguna excepción
se propaga al hilo llamador.

EN: Background thread (same pattern as CognitiveGovernor) that, during user
idle time, compares pairs of semantically close knowledge nodes (already
persisted web search results, meta-cognitive lessons) and proposes
synthetic connections between them via an LLM judge. Reuses existing
infrastructure rather than duplicating it: same idle-wait pattern, the same
router model as judge, persistence split into an immutable layer (WAL) and
a mutable layer (MemoryGraph), and indexing into a third FAISS index
(`longterm_vector_rag`) for future retrieval.

Validation runs in layers, cheapest first: a geometric nearest neighbor by
cosine similarity within a bounded pool; then a lexical pre-filter
requiring shared vocabulary; only then the LLM judge (temperature 0); and
finally deterministic grounding: the judge's quotes must appear VERBATIM in
the source fragment, and the proposed axiom must stay semantically close to
both source fragments. Sampling is incremental (only nodes new since the
last cycle) to avoid O(N²) cost over the whole accumulated history. No
exception ever escapes to the calling thread.
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

# ES: Vocabulario sin valor discriminativo para el pre-filtro léxico (paso 2)
#     — deliberadamente chico y aproximado: un falso negativo aquí solo
#     significa que no se gastó una llamada al juez en ese par, el fallo
#     seguro para un sistema que debe preferir no sintetizar antes que
#     sintetizar de más.
# EN: Vocabulary with no discriminative value for the lexical pre-filter
#     (step 2) — deliberately small and approximate: a false negative here
#     only means a judge call was skipped for that pair, the safe failure
#     mode for a system that should prefer under- over over-synthesizing.
_STOPWORDS = {
    "para", "como", "pero", "esto", "esta", "estos", "estas", "sobre",
    "entre", "cuando", "donde", "porque", "también", "hacer", "puede",
    "tiene", "desde", "hasta", "cada", "otro", "otra", "mismo", "misma",
    "the", "and", "for", "with", "that", "this", "from", "have", "has",
    "are", "was", "were", "been", "their", "which", "about", "into",
    "than", "then", "when", "where", "what", "your", "these", "those",
}


def _cosine(a: Optional[List[float]], b: Optional[List[float]]) -> float:
    """ES: Similitud coseno entre dos vectores; 0.0 si falta alguno o
    difieren en longitud.
    EN: Cosine similarity between two vectors; 0.0 if either is missing or
    their lengths differ."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class KnowledgeSynthesizer(threading.Thread):
    """ES: Hilo daemon que ejecuta, en segundo plano y durante la
    inactividad del usuario, el ciclo de síntesis de conocimiento descrito
    en el docstring del módulo.
    EN: Daemon thread that runs, in the background during user idle time,
    the knowledge-synthesis cycle described in the module docstring."""

    DOMAIN = "synthetic_knowledge"

    # ES: Cuántos nodos NUEVOS (desde el último ciclo) se procesan como
    #     máximo por pasada — cota dura para que un pico de actividad no
    #     convierta un solo ciclo en un maratón de llamadas al juez.
    # EN: Max NEW nodes (since the last cycle) processed per pass — a hard
    #     cap so an activity spike doesn't turn a single cycle into a judge
    #     call marathon.
    MAX_CANDIDATES_PER_CYCLE = 20
    # ES: Llamadas al juez (el paso caro, HTTP bloqueante) permitidas por
    #     ciclo, aunque más candidatos hayan pasado el pre-filtro léxico.
    # EN: Judge calls (the expensive, blocking HTTP step) allowed per
    #     cycle, even if more candidates passed the lexical pre-filter.
    MAX_JUDGE_CALLS_PER_CYCLE = 5
    # ES: Tamaño del pool de comparación — acota el costo por candidato a
    #     O(MAX_POOL_SIZE) en vez de O(N) sobre todo el historial.
    # EN: Comparison pool size — bounds the per-candidate cost to
    #     O(MAX_POOL_SIZE) instead of O(N) over the whole history.
    MAX_POOL_SIZE = 200

    MIN_GEOMETRIC_SIMILARITY = float(
        os.getenv("SOVNODE_SYNTH_MIN_SIMILARITY", "0.55")
    )
    MIN_SHARED_TOKENS = 2
    # ES: Piso de similitud coseno entre el axioma propuesto por el juez y
    #     CADA fragmento fuente (validación geométrica, paso 3): un juez
    #     que respeta el formato JSON pero inventa una conexión sin
    #     relación semántica real con alguno de los dos fragmentos no pasa.
    # EN: Cosine-similarity floor between the judge's proposed axiom and
    #     EACH source fragment (geometric validation, step 3): a judge that
    #     follows the JSON format but invents a connection with no real
    #     semantic relation to either fragment does not pass.
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
        """ES: Guarda las referencias al orquestador y arranca el pool de
        comparación vacío y el cursor de ciclo en el momento actual.
        EN: Stores the orchestrator reference and starts the comparison
        pool empty with the cycle cursor at the current time."""
        super().__init__(daemon=True, name="KnowledgeSynthesizer")
        self.orchestrator = orchestrator
        self.interval = interval_seconds
        self._running = True
        # ES: Pool de comparación: lista de {key, domain, text, embedding}.
        #     Vive solo en memoria de este hilo (como
        #     WorkspaceScanner._known) — se reconstruye vacío en cada
        #     arranque de la app; no hace falta persistir el pool en sí,
        #     solo lo que ya pasó las capas de validación.
        # EN: Comparison pool: list of {key, domain, text, embedding}.
        #     Lives only in this thread's memory (like
        #     WorkspaceScanner._known) — rebuilt empty on every app start;
        #     only what already passed the validation layers needs to be
        #     persisted, not the pool itself.
        self._pool: List[Dict[str, Any]] = []
        # ES: Cursor de "último ciclo": arranca en el momento de
        #     construcción, no en 0.0 — no se busca un backfill de todo el
        #     historial ya persistido, sino reaccionar a conocimiento nuevo
        #     desde que la app arrancó (muestreo incremental).
        # EN: "Last cycle" cursor: starts at construction time, not 0.0 —
        #     the goal is not to backfill the whole already-persisted
        #     history, but to react to knowledge that is new since the app
        #     started (incremental sampling).
        self._last_cycle_ts = time.time()
        # ES: Pares (clave no ordenada) ya evaluados EN EL CICLO ACTUAL —
        #     se reinicia al arrancar cada _run_cycle(). Sin esto, un par
        #     mutuamente-vecino-más-cercano gastaría dos llamadas al juez
        #     sobre el mismo par en vez de una.
        # EN: Pairs (unordered key) already evaluated IN THE CURRENT CYCLE
        #     — reset at the start of each _run_cycle(). Without this, a
        #     mutually-nearest-neighbor pair would spend two judge calls on
        #     the same pair instead of one.
        self._judged_pairs_this_cycle: set = set()

    def stop(self) -> None:
        """ES: Señala al hilo que termine su ciclo actual y no arranque otro.
        EN: Signals the thread to finish its current cycle and not start
        another."""
        self._running = False

    # ------------------------------------------------------------------
    # Paso 1: disparo por inactividad (idéntico a CognitiveGovernor.run())
    # ------------------------------------------------------------------
    def run(self) -> None:
        """ES: Bucle principal del hilo: espera por inactividad, cede si
        hay un turno real en curso o si no puede tomar el lock del LLM sin
        bloquear, y corre un ciclo de síntesis; cualquier excepción queda
        contenida y logueada.
        EN: Thread main loop: waits for idle time, yields if a real turn is
        in progress or the LLM lock can't be taken non-blockingly, and runs
        one synthesis cycle; any exception is caught and logged."""
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
        """ES: Purga nodos sintéticos viejos, recolecta candidatos nuevos,
        refresca el pool y le da a cada candidato una oportunidad de pasar
        por el juez, hasta el tope de llamadas del ciclo o hasta que
        empiece un turno real.
        EN: Purges stale synthetic nodes, collects new candidates, refreshes
        the pool, and gives each candidate a chance to reach the judge, up
        to the cycle's call cap or until a real turn starts."""
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
        # ES: Marca de avance siempre, incluso sin candidatos nuevos, para
        #     no reprocesar la misma ventana vacía de tiempo en el próximo
        #     ciclo.
        # EN: Always advance the cursor, even with no new candidates, so
        #     the same empty time window isn't reprocessed next cycle.
        self._last_cycle_ts = cycle_start

        if len(self._pool) < 2 or not candidates:
            return

        judge_calls = 0
        for candidate in candidates:
            if getattr(self.orchestrator, "_is_processing_turn", False):
                # ES: Un turno real empezó a mitad del ciclo: ceder de
                #     inmediato, el resto de los candidatos se procesan en
                #     el próximo ciclo (no se pierden, ya quedaron en el
                #     pool).
                # EN: A real turn started mid-cycle: yield immediately, the
                #     rest of the candidates are processed next cycle (not
                #     lost, they're already in the pool).
                break
            if judge_calls >= self.MAX_JUDGE_CALLS_PER_CYCLE:
                break
            if self._process_candidate(candidate, memory_graph):
                judge_calls += 1

    def _collect_new_candidates(self, memory_graph) -> List[Dict[str, Any]]:
        """ES: Junta nodos de web_knowledge y reasoning_lessons agregados
        desde el último ciclo, hasta el tope MAX_CANDIDATES_PER_CYCLE.
        EN: Gathers web_knowledge and reasoning_lessons nodes added since
        the last cycle, up to the MAX_CANDIDATES_PER_CYCLE cap."""
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
        """ES: Calcula embeddings para los candidatos nuevos y los agrega
        al pool, recortándolo a MAX_POOL_SIZE conservando los más recientes.
        EN: Computes embeddings for new candidates and adds them to the
        pool, trimming it to MAX_POOL_SIZE keeping the most recent ones."""
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
        """ES: Devuelve el vecino más cercano de `node` en el pool por
        similitud coseno, o None si ninguno supera MIN_GEOMETRIC_SIMILARITY.
        EN: Returns `node`'s nearest neighbor in the pool by cosine
        similarity, or None if none clears MIN_GEOMETRIC_SIMILARITY."""
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
        """ES: Devuelve True si se gastó una llamada al juez (para el tope
        MAX_JUDGE_CALLS_PER_CYCLE), sin importar si terminó persistiendo un
        nodo o no.
        EN: Returns True if a judge call was spent (for the
        MAX_JUDGE_CALLS_PER_CYCLE cap), regardless of whether it ended up
        persisting a node."""
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
        """ES: Extrae palabras de 4+ letras en minúsculas, excluyendo las
        de _STOPWORDS.
        EN: Extracts lowercase words of 4+ letters, excluding those in
        _STOPWORDS."""
        words = re.findall(r"[a-záéíóúñü]{4,}", (text or "").lower())
        return {w for w in words if w not in _STOPWORDS}

    def _passes_lexical_grounding(self, text_a: str, text_b: str) -> bool:
        """ES: True si los dos fragmentos comparten al menos
        MIN_SHARED_TOKENS palabras significativas.
        EN: True if the two fragments share at least MIN_SHARED_TOKENS
        significant words."""
        shared = self._significant_tokens(text_a) & self._significant_tokens(text_b)
        return len(shared) >= self.MIN_SHARED_TOKENS

    def _ask_judge(self, text_a: str, text_b: str) -> Optional[Dict[str, Any]]:
        """ES: Invoca al modelo router como juez (T=0) sobre el par de
        fragmentos y devuelve el veredicto parseado, o None si la llamada
        falla o la respuesta no es un JSON usable.
        EN: Invokes the router model as judge (T=0) over the fragment pair
        and returns the parsed verdict, or None if the call fails or the
        response isn't usable JSON."""
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
        ES: Extrae el veredicto JSON del juez, tolerando texto alrededor o
        comillas simples. Misma estrategia en capas que
        RobustJSONParser.extract_and_repair (bloque ```json```, luego
        primer '{' a último '}', luego reparación liviana de comillas y
        comas colgantes) pero generalizada: el veredicto usa la clave
        'connected', no 'tool', así que RobustJSONParser tal cual no aplica.

        EN: Extracts the judge's JSON verdict, tolerating surrounding text
        or single quotes. Same layered strategy as
        RobustJSONParser.extract_and_repair (```json``` block, then first
        '{' to last '}', then light quote/trailing-comma repair) but
        generalized: the verdict uses the key 'connected', not 'tool', so
        RobustJSONParser as-is doesn't apply here.
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
        """ES: Grounding determinista (sin LLM): las citas y el axioma del
        juez deben existir y las citas deben aparecer como substring
        literal del fragmento fuente correspondiente.
        EN: Deterministic grounding (no LLM): the judge's quotes and axiom
        must exist, and the quotes must appear as a literal substring of
        their corresponding source fragment."""
        if not verdict.get("connected"):
            return False
        quote_a = str(verdict.get("quote_n1") or "").strip()
        quote_b = str(verdict.get("quote_n2") or "").strip()
        axiom = str(verdict.get("axiom") or "").strip()
        if not quote_a or not quote_b or not axiom:
            return False
        return quote_a in text_a and quote_b in text_b

    def _passes_geometric_validation(
        self, verdict: Dict[str, Any], n1: Dict[str, Any], n2: Dict[str, Any]
    ) -> bool:
        """ES: Embebe el axioma propuesto y exige que quede
        semánticamente cerca de AMBOS fragmentos fuente (no solo que el
        juez haya respetado el formato).
        EN: Embeds the proposed axiom and requires it to stay semantically
        close to BOTH source fragments (not just that the judge followed
        the format)."""
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
        """ES: Escribe el axioma verificado en el WAL (inmutable), registra
        su estado vivo en MemoryGraph y lo indexa para recuperación futura.
        EN: Writes the verified axiom to the WAL (immutable), records its
        live state in MemoryGraph, and indexes it for future retrieval."""
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
        """ES: Agrega el axioma al índice FAISS de largo plazo bajo el
        mismo lock que serializa las escrituras a los otros índices
        vectoriales.
        EN: Adds the axiom to the long-term FAISS index under the same
        lock that serializes writes to the other vector indexes."""
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
        """ES: Complemento de la purga mensual de MemoryGraph: retira
        también el vector correspondiente de `longterm_vector_rag` para que
        el índice FAISS no acumule vectores de nodos ya purgados del estado
        vivo — el mismo problema que `LocalVectorRAG.remove_source` ya
        resuelve para archivos de workspace reindexados.
        EN: Complement to MemoryGraph's monthly purge: also removes the
        matching vector from `longterm_vector_rag` so the FAISS index
        doesn't accumulate vectors for nodes already purged from live
        state — the same problem `LocalVectorRAG.remove_source` already
        solves for reindexed workspace files."""
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
        """ES: Promedia similitud geométrica del par, similitud del axioma
        contra cada fragmento y (si el juez la dio) su propia confianza.
        EN: Averages the pair's geometric similarity, the axiom's
        similarity to each fragment, and (if the judge gave one) its own
        confidence value."""
        parts = [
            float(n2.get("similarity") or 0.0),
            float(verdict.get("_sim_a") or 0.0),
            float(verdict.get("_sim_b") or 0.0),
        ]
        judge_conf = verdict.get("confidence")
        if isinstance(judge_conf, (int, float)):
            parts.append(max(0.0, min(1.0, float(judge_conf))))
        return sum(parts) / len(parts) if parts else 0.0
