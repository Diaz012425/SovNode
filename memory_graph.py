"""
SovNode — Motor de Memoria Graph-Lite en SQLite (memory_graph.py)
Gestión de contexto con persistencia local y búsqueda FTS5.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import List, Dict, Any, Optional


def _sanitize_fts_query(query: str) -> str:
    """
    Normaliza texto libre para usarlo como argumento de MATCH en FTS5.

    Sustituir la puntuación por espacios (en vez de eliminarla) es
    importante: FTS5 no tolera símbolos sueltos como '.' en la sintaxis de
    consulta (lanza OperationalError), pero borrarlos sin más pega dígitos
    separados por ellos — "3.14" se convertía en "314", que ya no coincide
    con los tokens "3" y "14" que el propio índice generó al indexar ese
    mismo texto. Reemplazar por espacio conserva ambos tokens intactos.
    """
    clean = re.sub(r"[^\w\s]", " ", query or "", flags=re.UNICODE)
    return re.sub(r"\s+", " ", clean).strip()


def _semantic_cache_entry_id(query: str) -> str:
    """
    Id canónico de una entrada del caché semántico. Derivado solo del
    texto de la consulta (normalizado), así que sirve tanto para escribir
    (`store_semantic_cache_entry`) como para el lookup exacto sin vectores
    (`find_exact_cache_hit`) — ambos tienen que calcularlo igual.
    """
    return hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()[:24]

            
class MemoryGraph:
    def __init__(self, db_path: str | Path = "sovnode_memory.db") -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def clear(self) -> None:
        """
        Borra todo el historial de la base de datos para iniciar una
        sesión completamente limpia — incluye `semantic_cache`
        (respuestas COMPLETAS cacheadas por similitud coseno, ver
        `find_semantic_cache_hit`/`Orchestrator.check_semantic_cache`):
        sin esto, una consulta nueva semánticamente parecida a una vieja
        podía devolver la respuesta cacheada de una sesión anterior tal
        cual, sin pasar por una inferencia nueva ni por las reglas del
        prompt vigentes — la forma más directa posible de que una
        "conversación nueva" arrastre un error del pasado.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM conversation_turns")
            cursor.execute("DELETE FROM turns_fts")
            cursor.execute("DELETE FROM web_knowledge")
            cursor.execute("DELETE FROM web_knowledge_fts")
            cursor.execute("DELETE FROM reasoning_lessons")
            cursor.execute("DELETE FROM reasoning_lessons_fts")
            cursor.execute("DELETE FROM semantic_cache")
            conn.commit()

    def _get_connection(self) -> sqlite3.Connection:
        """
        Nueva conexión SQLite por llamada (sqlite3.Connection no es
        segura para compartir entre hilos). WAL/NORMAL se fijan en
        CADA conexión nueva, no solo una vez en `_init_db()`:
        `journal_mode=WAL` es una propiedad PERSISTENTE del archivo de
        base de datos (una vez fijada, sobrevive a conexiones futuras
        sin necesidad de repetirla — fijarla aquí de nuevo es un no-op
        barato, no un error), pero `synchronous` es una propiedad POR
        CONEXIÓN que vuelve al default de SQLite (FULL, fsync
        completo en cada escritura) en cada `sqlite3.connect()` nuevo
        — sin fijarla aquí, solo la conexión usada dentro de
        `_init_db()` quedaba en NORMAL y el resto de conexiones de la
        vida del proceso (incluidas las de hilos secundarios — un
        QThread de persistencia web, el hilo de fsync por lotes, etc.)
        operaban con el fsync completo de FULL sin necesidad real, ya
        que WAL por sí solo ya da la durabilidad adecuada para este
        caso de uso. Juntas habilitan escrituras concurrentes desde
        hilos secundarios sin bloquear lecturas desde el hilo de la UI.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Tabla principal de turnos conversacionales
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id TEXT PRIMARY KEY,
                    turn_number INTEGER,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            # Índice FTS5 para búsqueda rápida de relevancia semántica
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                    turn_id UNINDEXED,
                    content
                )
            """)

            # Caché persistente de conocimiento obtenido de la web
            # (Web-to-RAG): cada resultado de búsqueda exitoso se guarda
            # aquí para que consultas futuras (aunque sean días después)
            # puedan reutilizarlo sin volver a golpear la red.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS web_knowledge (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    url TEXT NOT NULL,
                    domain TEXT,
                    title TEXT,
                    content TEXT NOT NULL,
                    summary TEXT DEFAULT '',
                    score REAL DEFAULT 0,
                    fetched_at REAL NOT NULL
                )
            """)
            # Migración defensiva para bases ya existentes creadas antes de
            # que `summary` formara parte del CREATE TABLE de arriba (ese
            # CREATE TABLE IF NOT EXISTS no toca una tabla que ya existe,
            # así que un sovnode_memory.db de una instalación previa se
            # queda sin la columna nueva sin este ALTER TABLE). SQLite no
            # ofrece "ADD COLUMN IF NOT EXISTS": se intenta y se ignora el
            # único error esperable ("duplicate column name") cuando la
            # columna ya existe (bases nuevas, o una segunda ejecución).
            try:
                cursor.execute("ALTER TABLE web_knowledge ADD COLUMN summary TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS web_knowledge_fts USING fts5(
                    entry_id UNINDEXED,
                    query,
                    title,
                    content
                )
            """)


            # Meta-cognición histórica (Genialidad #2): turnos que terminaron
            # en error, y parches que CognitiveGovernor sugirió tras
            # auto-analizarlos. Consultado antes de razonar en el SLOW_PATH
            # para que el modelo evite repetir un fallo ya conocido, sin
            # necesidad de fine-tuning.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reasoning_lessons (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT,
                    outcome TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS reasoning_lessons_fts USING fts5(
                    entry_id UNINDEXED,
                    content
                )
            """)

            # Caché semántico de respuesta directa (Optimización #2): cada
            # turno resuelto por el modelo se guarda junto a su embedding.
            # Un turno nuevo cuya consulta sea coseno-similar (>= umbral) a
            # una ya cacheada se responde de inmediato desde aquí, sin
            # pagar una nueva inferencia. `embedding` se guarda como JSON
            # (no BLOB binario) porque el vector ya es una lista de floats
            # pequeña (384 dim) y JSON evita depender de numpy/struct para
            # (de)serializar en esta capa de persistencia.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS semantic_cache (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    response TEXT NOT NULL,
                    model TEXT,
                    created_at REAL NOT NULL,
                    hits INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_semantic_cache_created
                ON semantic_cache (created_at)
            """)

            # Persistencia de herramientas dinámicas validadas (Optimización
            # #5): un script generado por DynamicToolEngine que ya pasó la
            # auditoría AST y se ejecutó con éxito se guarda aquí, indexado
            # por la descripción de la tarea que lo originó. Una tarea
            # futura léxicamente similar reutiliza el script directamente
            # (fetch_validated_tool) en vez de volver a pagar una llamada
            # al modelo coder para regenerarlo.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS validated_tools (
                    id TEXT PRIMARY KEY,
                    task_description TEXT NOT NULL,
                    code TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    use_count INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS validated_tools_fts USING fts5(
                    entry_id UNINDEXED,
                    task_description
                )
            """)
            conn.commit()

    def store_turn(self, turn_id: str, role: str, content: str) -> None:
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO conversation_turns (id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (turn_id, role, content, now)
            )
            cursor.execute(
                "INSERT INTO turns_fts (turn_id, content) VALUES (?, ?)",
                (turn_id, content)
            )
            conn.commit()

    def fetch_relevant_context(self, query: str, limit: int = 3) -> List[str]:
        """Recupera los turnos pasados más relevantes utilizando FTS5."""
        clean_query = _sanitize_fts_query(query)
        if not clean_query:
            return []

        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT t.role, t.content 
                    FROM conversation_turns t
                    JOIN turns_fts f ON t.id = f.turn_id
                    WHERE turns_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (clean_query, limit))
                results = cursor.fetchall()
                return [f"{row['role'].capitalize()}: {row['content']}" for row in results]
            except sqlite3.OperationalError:
                return []

    def get_recent_history(self, limit: int = 4) -> List[str]:
        """Obtiene el contexto conversacional reciente."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT role, content FROM conversation_turns
                ORDER BY timestamp DESC LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [f"{row['role'].capitalize()}: {row['content']}" for row in reversed(rows)]

    def store_web_knowledge(self, query: str, results: List[Dict[str, Any]]) -> int:
        """
        Persiste resultados de búsqueda web exitosos como conocimiento
        reutilizable. Cada entrada se identifica por el hash de su URL, así
        que investigar la misma fuente de nuevo actualiza el registro en
        lugar de duplicarlo. Devuelve cuántas entradas se guardaron.

        `r.get("summary")`, si viene presente y no vacío, persiste el
        resumen extractivo de esa fuente (ver
        `Orchestrator.summarize_sources_map_reduce`) junto al contenido
        crudo — así una URL ya resumida en una investigación previa no
        necesita pagar otra mini-llamada al modelo la próxima vez que
        aparece (ver `fetch_web_knowledge_by_url`). El UPSERT preserva el
        `summary` ya guardado cuando el llamador NO trae uno nuevo (p. ej.
        `Orchestrator._persist_web_knowledge`, que persiste resultados
        crudos sin resumir): sin ese resguardo, cualquier re-persistencia
        posterior de la MISMA URL sin resumen borraría en silencio un
        resumen ya calculado.
        """
        if not results:
            return 0

        now = time.time()
        stored = 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for r in results:
                url = str(r.get("url") or "").strip()
                content = str(r.get("content") or r.get("snippet") or "").strip()
                if not url or not content:
                    continue

                entry_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
                title = str(r.get("title") or "")
                domain = str(r.get("domain") or "")
                score = float(r.get("score") or 0.0)
                summary = str(r.get("summary") or "").strip()

                cursor.execute("""
                    INSERT INTO web_knowledge (id, query, url, domain, title, content, summary, score, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        query = excluded.query,
                        title = excluded.title,
                        content = excluded.content,
                        summary = COALESCE(NULLIF(excluded.summary, ''), summary),
                        score = excluded.score,
                        fetched_at = excluded.fetched_at
                """, (entry_id, query, url, domain, title, content, summary, score, now))

                # FTS5 no soporta UPSERT: se reindexa manualmente la entrada.
                cursor.execute("DELETE FROM web_knowledge_fts WHERE entry_id = ?", (entry_id,))
                cursor.execute(
                    "INSERT INTO web_knowledge_fts (entry_id, query, title, content) VALUES (?, ?, ?, ?)",
                    (entry_id, query, title, content),
                )
                stored += 1

            conn.commit()
        return stored

    def fetch_web_knowledge(
        self, query: str, limit: int = 3, max_age_seconds: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Recupera conocimiento web previamente investigado y aún vigente
        (dentro de `max_age_seconds`) que coincida léxicamente con `query`.
        Es el paso que el orquestador consulta ANTES de forzar una nueva
        petición de red: si hay hits frescos, la búsqueda web se omite.
        """
        clean_query = _sanitize_fts_query(query)
        if not clean_query:
            return []

        cutoff = (time.time() - max_age_seconds) if max_age_seconds else 0.0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT w.title, w.url, w.domain, w.content, w.summary, w.score, w.fetched_at
                    FROM web_knowledge w
                    JOIN web_knowledge_fts f ON w.id = f.entry_id
                    WHERE web_knowledge_fts MATCH ? AND w.fetched_at >= ?
                    ORDER BY rank
                    LIMIT ?
                """, (clean_query, cutoff, limit))
                return [dict(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                return []

    def fetch_web_knowledge_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Lookup directo por URL (no por similitud léxica con una query,
        como `fetch_web_knowledge`) — `id` en `web_knowledge` YA es el
        hash de la URL (ver `store_web_knowledge`), así que esto es una
        búsqueda por clave primaria, O(1) e independiente del texto de
        ninguna consulta.

        Usado por `Orchestrator.summarize_sources_map_reduce` (punto 5
        del pipeline de resúmenes extractivos): antes de pagar una
        mini-llamada al modelo para resumir una fuente, se comprueba si
        esa MISMA URL ya trae un `summary` persistido de una
        investigación anterior (de este turno o de una sesión pasada).
        Devuelve `None` si la URL nunca se persistió o si no hay
        conexión disponible — nunca lanza.
        """
        url = (url or "").strip()
        if not url:
            return None

        entry_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT title, url, domain, content, summary, score, fetched_at "
                    "FROM web_knowledge WHERE id = ?",
                    (entry_id,),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
            except sqlite3.OperationalError:
                return None

    def store_reasoning_lesson(self, turn_id: str, outcome: str, content: str) -> bool:
        """
        Persiste una lección meta-cognitiva: un turno que terminó en error
        (`outcome="error"`) o un parche auto-sugerido por CognitiveGovernor
        tras analizar un fallo (`outcome="suggested_patch"`). `content` se
        recorta a 2000 caracteres — es evidencia de apoyo para el prompt,
        no un archivo a preservar íntegro.
        """
        content = (content or "").strip()
        if not content:
            return False

        content = content[:2000]
        now = time.time()
        entry_id = hashlib.sha256(f"{turn_id}:{outcome}:{content}".encode("utf-8")).hexdigest()[:24]

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO reasoning_lessons (id, turn_id, outcome, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET created_at = excluded.created_at
            """, (entry_id, turn_id, outcome, content, now))

            cursor.execute("DELETE FROM reasoning_lessons_fts WHERE entry_id = ?", (entry_id,))
            cursor.execute(
                "INSERT INTO reasoning_lessons_fts (entry_id, content) VALUES (?, ?)",
                (entry_id, content),
            )
            conn.commit()
        return True

    def fetch_reasoning_lessons(self, query: str, limit: int = 2) -> List[Dict[str, Any]]:
        """
        Busca lecciones meta-cognitivas (errores pasados / parches
        auto-sugeridos) léxicamente relacionadas con `query`, para
        inyectarlas como advertencia antes de razonar sobre una petición
        similar (Genialidad #2 — Meta-Cognición Histórica).
        """
        clean_query = _sanitize_fts_query(query)
        if not clean_query:
            return []

        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT r.outcome, r.content, r.created_at
                    FROM reasoning_lessons r
                    JOIN reasoning_lessons_fts f ON r.id = f.entry_id
                    WHERE reasoning_lessons_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (clean_query, limit))
                return [dict(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                return []

    # =====================================================================
    # Caché semántico de respuesta directa (Optimización #2)
    # =====================================================================
    @staticmethod
    def semantic_cache_entry_id(query: str) -> str:
        """Expone el id canónico de una entrada para lookups exactos."""
        return _semantic_cache_entry_id(query)

    def store_semantic_cache_entry(
        self, query: str, embedding: List[float], response: str, model: str = "",
    ) -> None:
        """Persiste un par (consulta, embedding, respuesta) reutilizable.
        Pensado para llamarse en un hilo de fondo tras responderle al
        usuario — nunca debe bloquear el turno que ya se completó."""
        if not query or not embedding or not response:
            return

        entry_id = _semantic_cache_entry_id(query)
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO semantic_cache (id, query, embedding, response, model, created_at, hits)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(id) DO UPDATE SET
                    response = excluded.response,
                    embedding = excluded.embedding,
                    model = excluded.model,
                    created_at = excluded.created_at
            """, (entry_id, query, json.dumps(embedding), response, model, now))
            conn.commit()

    def find_semantic_cache_hit(
        self,
        query_embedding: List[float],
        threshold: float = 0.93,
        max_age_seconds: Optional[float] = None,
        candidate_limit: int = 500,
    ) -> Optional[Dict[str, Any]]:
        """
        Recorre las entradas cacheadas más recientes y devuelve la de mayor
        similitud coseno si supera `threshold`. `candidate_limit` acota el
        escaneo (SQLite no tiene un índice vectorial nativo; a la escala de
        un monolito personal — miles de turnos, no millones — un escaneo
        lineal en Python sobre los últimos N registros es más simple y
        suficientemente rápido que añadir una dependencia de índice
        vectorial externo solo para esta caché).
        """
        if not query_embedding:
            return None

        cutoff = (time.time() - max_age_seconds) if max_age_seconds else 0.0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, query, embedding, response, model, hits
                FROM semantic_cache
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (cutoff, candidate_limit))
            rows = cursor.fetchall()

            best_row = None
            best_score = 0.0
            for row in rows:
                try:
                    candidate_vec = json.loads(row["embedding"])
                except (json.JSONDecodeError, TypeError):
                    continue
                score = _cosine_similarity(query_embedding, candidate_vec)
                if score > best_score:
                    best_score = score
                    best_row = row

            if best_row is None or best_score < threshold:
                return None

            cursor.execute(
                "UPDATE semantic_cache SET hits = hits + 1 WHERE id = ?",
                (best_row["id"],),
            )
            conn.commit()

            return {
                "query": best_row["query"],
                "response": best_row["response"],
                "model": best_row["model"],
                "similarity": best_score,
                "hits": best_row["hits"] + 1,
            }

    def find_exact_cache_hit(
        self,
        query: str,
        max_age_seconds: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Variante SIN vectores de `find_semantic_cache_hit`: busca por el id
        canónico (sha256 de la consulta normalizada), así que solo acierta
        cuando el texto es el mismo módulo espacios y mayúsculas.

        Existe para el modo degradado de embeddings: cuando los vectores
        vienen del fallback bag-of-words hasheado, su similitud coseno no
        mide significado (es invariante al orden de las palabras, ver
        `embeddings.get_embedding_with_mode`), así que ningún umbral la
        hace segura para servir una respuesta cacheada. La coincidencia
        exacta conserva la ganancia en repeticiones literales — que es de
        donde viene la mayor parte del ahorro real — sin poder responder
        nunca a la consulta equivocada.
        """
        if not query or not query.strip():
            return None

        cutoff = (time.time() - max_age_seconds) if max_age_seconds else 0.0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, query, response, model, hits
                FROM semantic_cache
                WHERE id = ? AND created_at >= ?
            """, (_semantic_cache_entry_id(query), cutoff))
            row = cursor.fetchone()

            if row is None:
                return None

            cursor.execute(
                "UPDATE semantic_cache SET hits = hits + 1 WHERE id = ?",
                (row["id"],),
            )
            conn.commit()

            return {
                "query": row["query"],
                "response": row["response"],
                "model": row["model"],
                "similarity": 1.0,
                "hits": row["hits"] + 1,
                "match": "exact",
            }

    # =====================================================================
    # Persistencia de herramientas dinámicas validadas (Optimización #5)
    # =====================================================================
    def store_validated_tool(self, task_description: str, code: str) -> None:
        if not task_description or not code:
            return

        entry_id = hashlib.sha256(task_description.strip().lower().encode("utf-8")).hexdigest()[:24]
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO validated_tools (id, task_description, code, created_at, use_count)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(id) DO UPDATE SET
                    code = excluded.code,
                    created_at = excluded.created_at
            """, (entry_id, task_description, code, now))

            cursor.execute("DELETE FROM validated_tools_fts WHERE entry_id = ?", (entry_id,))
            cursor.execute(
                "INSERT INTO validated_tools_fts (entry_id, task_description) VALUES (?, ?)",
                (entry_id, task_description),
            )
            conn.commit()

    def fetch_validated_tool(self, task_description: str) -> Optional[Dict[str, Any]]:
        """Busca una herramienta ya validada cuya descripción de tarea
        original coincida léxicamente (FTS5) con la nueva petición. Si hay
        hit, el llamador puede ejecutar `code` directamente sin volver a
        invocar al modelo coder."""
        clean_query = _sanitize_fts_query(task_description)
        if not clean_query:
            return None

        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT v.id, v.task_description, v.code, v.use_count
                    FROM validated_tools v
                    JOIN validated_tools_fts f ON v.id = f.entry_id
                    WHERE validated_tools_fts MATCH ?
                    ORDER BY rank
                    LIMIT 1
                """, (clean_query,))
                row = cursor.fetchone()
                if row is None:
                    return None

                cursor.execute(
                    "UPDATE validated_tools SET use_count = use_count + 1 WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()

                return {
                    "task_description": row["task_description"],
                    "code": row["code"],
                    "use_count": row["use_count"] + 1,
                }
            except sqlite3.OperationalError:
                return None


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)