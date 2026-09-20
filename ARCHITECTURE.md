# SovNode / "El Monolito Personal" — mapa de arquitectura para otra IA

> Generado el 2026-09-06, actualizado el 2026-09-08, 2026-09-11,
> 2026-09-14, 2026-09-16, 2026-09-17 y 2026-09-18 (esta última
> actualización, sesión de Cowork), a partir de
> una lectura directa del código real en
> `C:\Users\steph\Desktop\MonolitoPersonal` (no es un diseño
> aspiracional: cada módulo, clase y decisión listada abajo existe en el
> repo tal cual se describe). Este documento es para que OTRA sesión de
> IA (Cowork, Claude Code, o cualquier otra) entienda el sistema completo
> sin tener que releer ~1.4 MB de código Python fuente. Está escrito en
> español porque así están el 95% de los comentarios y docstrings del
> propio proyecto.
>
> **Cambio de fondo desde la revisión del 09-08**: el proyecto pasó de
> ser 100% local (Ollama) a tener un **motor dual** — Local (Ollama, sin
> cambios de fondo) y **Cloud** (API real de Claude, `api.anthropic.com`,
> con el crédito propio del usuario). Ver la sección 5, nueva, para el
> detalle completo. El resto del documento (routing determinista,
> memoria híbrida, daemons de descubrimiento, sandboxing) es
> independiente de qué motor esté activo y sigue aplicando igual.
>
> **Revisión 2026-09-16**: cinco blindajes puntuales sobre routing
> determinista y corrección de idioma (NO tocan el motor dual de
> §5) -- un bug real de misenrutamiento de pedidos de código a
> slow_path, memoria de dependencias cruzadas entre sugerencias,
> memoria del último código generado para turnos de seguimiento,
> timeout acotado del Router0.5B, y un blindaje de fidelidad nuevo
> que permitió activar por default la corrección de idioma
> liviana. Detalle completo al final de §8; menciones puntuales en
> §3.1 (router.py), §4 (flujo de turno), §6 (router_model), §7
> (testing) y §9 (variables de entorno).
>
> **Revisión 2026-09-17**: siete parches puntuales sobre `edit_file` y el
> presupuesto de salida del motor Cloud (patches `orchestrator57` a
> `orchestrator63`, NO tocan el routing determinista de la revisión
> anterior) -- feedback de error de sintaxis en reintentos, presupuesto
> dinámico propio para `edit_file` (2.75x vs. 1.5x de `write_file`),
> texto de `[OUTPUT BUDGET]` específico para `edit_file` + renombre de
> las etiquetas Bajo/Medio/Alto/Extra del selector de la UI (se les sacó
> la promesa de "¢/turno", que dejó de ser literal apenas el techo real
> pasó a variar según la herramienta), rescate local ($0, motor Local)
> para un `edit_file` cortado por el techo en vez de intentar empalmar
> JSON parcial, un aviso de "el archivo ya existe" para evitar un segundo
> `write_file` completo en el mismo turno, población reactiva del
> archivo-objetivo desde un `read_file` exitoso (arregla un turno que no
> editaba nada y mostraba el volcado crudo de la herramienta), y la
> eliminación de un gate de tamaño redundante que le ocultaba el nombre
> de archivo resuelto al modelo en archivos grandes. Detalle completo al
> final de §8; menciones puntuales en §5.5, §7 y §10.
>
> **Revisión 2026-09-18**: diagnóstico en vivo de "qué falló" en un
> turno, pedido explícito del usuario. Dos entregas: `sovnode_wal_
> monitor.py` (script standalone nuevo, junto a `sovnode_log_viewer.py`,
> agrupa el WAL por turno) y, tras una corrección del usuario ("lo
> quiero como botón EN la terminal de la app"), "Terminal avanzada" --
> un toggle nuevo en el panel de consola de `sovnode_qt.py` que
> reclasifica y colorea el mismo stream de eventos que la UI ya recibía,
> resaltando en rojo cualquier red de seguridad que dispare, con costo
> real en $ por turno. Un solo `yield` nuevo en `orchestrator.py`
> (`codegen_budget_plan` ahora también llega a la consola, no solo al
> WAL) -- ninguna de las dos entregas toca `_call_claude_api_raw`.
> Detalle completo al final de §8.

## 0. Qué es esto, en una frase

Un cliente de escritorio Windows (PyQt6) que corre un asistente de IA
con **dos motores de generación intercambiables** — 100% local sobre
Ollama, o la API de pago de Claude — con enrutamiento de intención
determinista, memoria híbrida (SQLite FTS5 + FAISS), ejecución de
herramientas en sandbox (con soporte tanto para reescritura completa de
archivo como para parches quirúrgicos tipo SEARCH/REPLACE), verificación
anti-alucinación post-hoc, lectura de imágenes (visión + OCR), y dos
motores propios de "descubrimiento" de conocimiento que corren en
background durante la inactividad del usuario. Licencia AGPLv3. Nombre
público: **SovNode** ("Sovereign AI Node"); nombre de working-title
original en las notas de diseño: "El Monolito Personal".

Perfil de hardware de referencia del usuario: Windows 10/11, GPU AMD
Radeon RX 5500 XT (RDNA1, 8GB VRAM — **sin soporte confiable de flash
attention**, ver §8), 16GB RAM. Decode local medido: ~15-19 tok/s. El
motor Cloud existe, en parte, precisamente para no depender de ese techo
de hardware cuando el usuario elige pagar por velocidad/calidad en vez
de usar su propia GPU.

## 1. Cómo correrlo

```bash
pip install -r requirements.txt
python src/ui/sovnode_qt.py      # cliente principal PyQt6
streamlit run app.py             # cliente alternativo más liviano (Streamlit)
```

Requiere Ollama corriendo en `localhost:11434` con al menos:
`ollama pull qwen2.5:7b` (modelo general) y `ollama pull qwen2.5:0.5b`
(router rápido) — esto sigue siendo necesario AUNQUE el usuario elija el
motor Cloud como principal, porque Router0.5B, VerifyLite y Vision
(moondream) corren **siempre en local** sin importar el toggle (ver §5.3).
Todos los nombres de modelo local son overrideables por variable de
entorno (§9) sin tocar código.

Para usar el motor Cloud: sidebar → tarjeta "Motor de Generación" →
elegir "Cloud" en el combo, pegar una API key de
`console.anthropic.com` (créditos de la API, NO la suscripción de
Claude.ai — son cosas separadas) en el campo correspondiente. La key se
persiste en `QSettings` (no hay que retipearla en cada arranque).

Build del ejecutable Windows: `python build.py` (usa `SovNode.spec` /
PyInstaller; ver §7).

Tests: `python tests/test_regressions.py` (ver §7 — **importante leer
antes de correrlo en un workspace nuevo**, requiere dos parches de
entorno no relacionados con el código en sí). El trabajo del motor Cloud
(§5) NO tiene secciones numeradas dentro de esta suite — se verificó con
scripts `verify_*.py` dedicados y desechables, fuera del repo (ver nota
en §7).

## 2. Mapa de directorios

```
MonolitoPersonal/
├── app.py                  # UI alternativa Streamlit
├── build.py                # empaquetador de un comando (PyInstaller)
├── SovNode.spec             # spec de PyInstaller (el bueno; MonolitoPersonal.spec es legado)
├── modelfile                 # Modelfile de Ollama (personalización de modelo, si aplica)
├── logo.ico / src/logo.ico / src/logo.png
├── README.md                # overview público (en inglés, cara al usuario final)
├── sovnode_memory.db          # SQLite (WAL history + FTS5) — vive TAMBIÉN en src/core/, ver nota
├── sovnode.wal                 # write-ahead log — idem
├── src/
│   ├── core/                # el motor: orquestación, memoria, verificación, daemons, OCR
│   ├── tools/                # despachador de herramientas + sandbox de ejecución
│   └── ui/                   # PyQt6 (cliente principal) + renderizado de ecuaciones
├── tests/
│   ├── test_regressions.py   # suite real de regresión (aserciones, exit code != 0 si falla)
│   └── test_lsc.py           # script exploratorio viejo, no hace aserciones
└── docs/                     # GIFs de demo para el README
```

**Nota importante de rutas**: `sovnode_memory.db` / `sovnode.wal` /
los índices FAISS (`*_vector_index.faiss` / `.meta.json`) se persisten
junto a donde vive `MemoryGraph("sovnode_memory.db")` en tiempo de
ejecución — que resulta ser `src/core/` cuando se corre `sovnode_qt.py`
desde la raíz del repo (cwd-dependiente). Por eso aparecen copias de esos
archivos TANTO en la raíz (corridas viejas / otro cwd) como en `src/core/`
(la copia viva, más reciente). Al inspeccionar estado real del sistema,
la copia de `src/core/` es la que importa.

## 3. Los tres directorios de código, módulo por módulo

### 3.1 `src/core/` — el motor

- **`orchestrator.py`** (~896 KB, el archivo más grande con diferencia).
  Contiene la clase `Orchestrator`, que es el corazón de todo: recibe un
  turno de usuario (`run_turn`/`process_turn`), lo clasifica, decide
  fast-path vs. slow-path, arma el prompt, llama al motor activo (Local
  u Cloud, ver §5), verifica la respuesta, la corrige si hace falta, y la
  persiste en el WAL. También vive acá: `MemoryGovernor` (calibra
  `num_predict`/repeat_penalty/etc. por familia de modelo y GPU),
  `CognitiveGovernor` (daemon de auto-introspección/auto-reparación en
  inactividad), `LexicalSafetyNet`, `ScopeValidator` (AST), el pipeline
  completo de "verificación post-hoc consolidada", TODA la lógica del
  motor Cloud (`set_cloud_backend`, `_call_claude_api_raw`/
  `_stream_claude_api_raw`, `CLOUD_TOOLS_SCHEMA`, contabilidad de costo
  con prompt caching) descrita en detalle en §5, y
  `_maybe_prepare_code_fix_from_image` (redirect de un turno con imagen
  desde el carril de visión hacia el pipeline normal de código cuando la
  imagen es una captura de error/traceback — ver `ocr_reader.py` abajo).
  Ver §4 para el flujo de un turno completo.

- **`ocr_reader.py`** (nuevo, 2026-09-09) — OCR 100% en CPU (ONNX vía
  `rapidocr-onnxruntime`, mismo enfoque local-sin-GPU que `embeddings.py`
  con fastembed) sobre capturas de pantalla adjuntadas, para el flujo
  "corregí este error a partir de la imagen". Deliberadamente NO pasa
  por Ollama ni por HIP/ROCm — la GPU RDNA1 del usuario ya obligó a
  elegir un modelo de visión chico (moondream, ver §6) para no arriesgar
  un crash del servidor, y el OCR no debe sumar ningún riesgo nuevo sobre
  esa misma GPU. Degrada con gracia: si el paquete no está instalado,
  todas las funciones devuelven `None`/`False` y el turno cae al carril
  de visión existente como si el módulo no existiera.
  `Orchestrator._maybe_prepare_code_fix_from_image` lo usa así: (1)
  detecta intención de corrección en el texto del usuario; (2)
  `ocr_reader.extract_text_from_image` + `looks_like_code_or_error`
  confirman que la imagen parece código/traceback; (3) resuelve un
  archivo REAL en el workspace (por nombre detectado en el traceback vía
  `extract_traceback_filename`, o el `.py` más reciente); si las 3
  condiciones se cumplen, el turno se redirige al pipeline normal de
  archivos (con el texto OCR inyectado como pista NO confiable, a
  contrastar contra el contenido real del archivo) en vez de quedarse en
  el carril aislado de visión (que no tiene acceso a archivos reales).

- **`router.py`** — `IntentRouter`: enrutador determinista (sin LLM) que
  clasifica cada turno con un conjunto de `SignalTag` (16 señales:
  `TRIVIAL_GREETING`, `CODE_COMPLEX`, `FACTUAL_ENUMERATION`,
  `WEB_SEARCH_INTENT`, `FORMULA_DISCOVERY`, ...) y decide
  `RoutePath.FAST_PATH` vs `RoutePath.SLOW_PATH`. Además existe
  `OptimizedRouter`, una capa de batching/compresión sobre el router LLM
  de respaldo (`qwen2.5:0.5b`) para cuando el determinista no alcanza.
  **Blindajes de morfología del español** (varias rondas, la última
  2026-09-11): "función"/"clase"/"método" sueltas activaban
  `CODE_COMPLEX` tanto en sentido de programación como en usos
  matemáticos habituales ("la función de onda", "en función de X", "por
  este método") — un pedido real de derivar una ecuación terminó
  enrutado al modelo coder y sintetizando un `list_dir('.')` espurio en
  vez de la fórmula pedida. Fix en dos capas: (1) esas tres palabras se
  sacaron de `_CODE_COMPLEX_PATTERN` y pasaron a `_CODE_WEAK_NOUN_RE`,
  que solo cuenta si aparece sintaxis de código real pegada
  (`_CODE_WEAK_NOUN_CALL_RE`: "función(", "def foo(") o si NO hay ya una
  señal matemática fuerte cerca; (2) `_MATH_PATTERN` (que exigía verbos
  conjugados específicos) se complementó con un patrón de modismos
  matemáticos/físicos independiente de la conjugación ("función de
  onda", "en función de", ecuación/fórmula/derivada/integral/límite/
  teorema). Un pedido real de código con verbo explícito
  ("implementá/escribí/creá una función") sigue matcheando igual, por
  ese verbo.

  **Fix de enrutamiento de código puro (2026-09-16)**: además de la
  detección booleana de arriba (`_detect_code_complex`, sin
  cambios), el SCORE que ese tag suma hacia `SLOW_PATH_THRESHOLD`
  (`WEIGHT_CODE_COMPLEX`) bajó de `3.0` a `0.0` -- bug real medido
  por WAL: "dame un codigo que haga un slither.io base" enrutaba a
  slow_path por ese peso SOLO, chocaba con el circuit-breaker de
  slow_path (pensado para repetición de PROSA, no código) y su
  regeneración genérica de 200 tokens, terminando en el fallback
  genérico tras dos llamadas pagas a Cloud sin código entregado.
  Un pedido de código puro ahora clasifica `fast_path` por el
  determinista (que ya usa el presupuesto de codegen vía el TAG,
  no el peso); código combinado con una señal real de razonamiento
  complejo (matemática, auditoría lógica) sigue yendo a slow_path
  por mérito de ESA otra señal, sin cambios. El tag `CODE_COMPLEX`
  en sí no se tocó -- solo cuánto pesa hacia el umbral.

- **`formula_synthesizer.py`** — `FormulaSynthesizer`: daemon de
  descubrimiento FORMAL. Un LLM SOLO propone qué dos fórmulas de una
  base curada (`KNOWN_FORMULAS`, **32 entradas**) combinar y qué
  variable eliminar; `derive_and_verify()` hace el 100% del álgebra vía
  `CASEngine` (sympy) y verifica en dos capas independientes (simbólica
  + numérica adversarial). Cada fórmula nueva verificada se persiste en
  el WAL y se reinyecta bajo un nombre `descubierta_<node_id>` para
  encadenarse como ladrillo en la próxima ronda. Incluye guiado
  semántico (`_relevance_scores_for`, similitud coseno de embeddings)
  que ordena el barrido de combinaciones por relevancia al pedido del
  turno actual — best-effort sobre el ORDEN, nunca decide qué es verdad
  (eso lo arbitra únicamente `derive_and_verify`).

- **`knowledge_synthesizer.py`** — `KnowledgeSynthesizer`: daemon
  análogo pero para conocimiento TEXTUAL — durante inactividad, minerea
  pares de nodos semánticamente próximos (resultados web cacheados,
  lecciones metacognitivas) y le pide a un LLM-juez que proponga
  conexiones sintéticas, validadas por afinidad léxica + cita textual +
  validación geométrica antes de persistir.

- **`cas_sandbox.py`** — `CASEngine` (resolución/simplificación/
  verificación algebraica vía sympy), `ExecutionSandbox` (ejecución
  Python aislada en subproceso, con límites de recursos), y
  `LogicalCoherenceValidator` (auditoría determinista de aserciones
  aritméticas explícitas en texto libre). El saneador de expresiones
  preserva `**` como operador atómico de potencia (no lo colapsa a `*`)
  — ver la nota de §10 sobre esta clase de bug.

- **`fuzzer.py`** — `AdversarialFuzzer`: segundo pase (actor-crítico)
  para el slow-path CONCEPTUAL, con heurísticas léxico-sintácticas 100%
  locales, sin LLM.

- **`lsc_engine.py`** — `LSCInferenceEngine`: motor determinista
  (`no_imports` + `robustness_check` + `logical_inference`) para evaluar
  premisas complejas en lenguaje natural.

- **`verification.py`** — verificación factual determinista de eventos
  deportivos tipados (marcadores, ganadores) contra evidencia cruda
  scrapeada.

- **`relevance.py`** — ÚNICA fuente de verdad de criterios de relevancia
  consulta-vs-evidencia, compartida por `web_search.py`, `sovnode_qt.py`,
  `verification.py` y `orchestrator.py`.

- **`memory_graph.py`** — `MemoryGraph`: memoria persistente en SQLite
  con FTS5. Guarda turnos, conocimiento web cacheado, lecciones de
  razonamiento, caché semántico, tools validadas, carpetas de workspace,
  y conocimiento sintético.

- **`rag_faiss.py`** — `LocalVectorRAG`: RAG vectorial local con FAISS.
  Chunking AST-aware para `.py`, `fetch_hybrid_context`/
  `rerank_context_candidates` combinan FTS5 + FAISS con un piso de
  similitud coseno (`DEFAULT_RAG_MIN_SIMILARITY=0.30`).

- **`wal.py`** — `WriteAheadLog`: durabilidad síncrona (fsync real) +
  `KnowledgeNode` (unidad atómica e inmutable de conocimiento verificado).

- **`ollama_manager.py`** — `OllamaProcessManager`: ciclo de vida del
  servidor Ollama, diálogos de instalación/descarga de modelos in-app.
  Sigue siendo necesario con el motor Cloud activo, porque Router0.5B/
  VerifyLite/Vision nunca lo abandonan (§5.3).

- **`skeletons.py`** — Biblioteca local de "esqueletos" (Flappy Bird,
  Snake, Pong) para copiar directo sin pasar por ningún LLM.

- **`embeddings.py`** — Embeddings locales en proceso (fastembed/ONNX),
  con fallback a hash determinista. No usa Ollama.

- Otros: `ast_stream.py`, `async_executor.py`, `sys_optimizer.py`,
  `license_manager.py`, `workspace_watcher.py`, `context_manager.py`,
  `robust_json_parser.py`, `logger.py`, `pipeline.py` (contrato
  `EventType`/`PipelineEvent` entre el motor y cualquier capa de
  presentación).

### 3.2 `src/tools/` — herramientas y sandboxing

- **`tools.py`** — `ToolSandbox` (valida rutas contra una raíz activa
  vía `validate_path`, ejecuta comandos con lista de bloqueo/patrones
  peligrosos, lee/escribe archivos) y `LocalToolDispatcher` (registro y
  ejecución de tools). Dos formas de escribir un archivo, ambas
  registradas y en `TOOLS_SCHEMA`:
  - `write_file_safely(path, content)` — reescritura completa; red de
    seguridad AST para `.py` (si el `content` no parsea, no se escribe
    nada, se devuelve `"[SANDBOX WRITE ERROR]: ..."`).
  - **`edit_file_safely(path, old_str, new_str)`** (nuevo, 2026-09-14,
    ver §5.5) — parche quirúrgico tipo SEARCH/REPLACE, mismo contrato de
    unicidad que la herramienta Edit de Claude Code/Cowork: `old_str`
    debe matchear EXACTAMENTE una vez en el archivo (0 o 2+ matches →
    error, nada se escribe); comparte `validate_path` y la misma red de
    seguridad AST sobre el resultado, y el mismo prefijo de error
    `"[SANDBOX WRITE ERROR]"` — el resto del pipeline (guards de
    falso-éxito, reindexado RAG) trata un fallo de `edit_file` idéntico
    a uno de `write_file` sin tener que tocar cada chequeo por separado.
  - Aislamiento: en ausencia de un Workspace activo, la raíz por defecto
    es una carpeta `workspace/` dedicada (nunca `os.getcwd()`, que en
    desarrollo/build empaquetado es la carpeta del PROYECTO); además una
    lista negra (`_is_blacklisted_target`) rechaza cualquier operación
    dentro del propio código fuente de SovNode aunque alguien apunte
    `allowed_directory` ahí a mano.
- **`custom_tools.py`** — Tools nuevas por `.json` en `custom_tools/`
  sin tocar Python, ejecutadas a través del MISMO
  `ToolSandbox.run_cmd_safely()`.
- **`dynamic_tool_engine.py`** — `DynamicToolEngine`: ejecuta scripts
  Python generados en caliente con defensa en profundidad de 3 capas
  (lista blanca AST, bloqueo de dunder, subproceso aislado con límites).
- **`web_search.py`** (~85 KB) — Motor de búsqueda web multi-fuente
  (DuckDuckGo → Wikipedia → SearXNG) con circuit breaker, backoff,
  caché, y `search_topic_images()` (búsqueda de imágenes dedicada,
  independiente del scraping de artículos, hasta 3 fotos garantizadas
  por tema).
- **`training_export.py`** — Exporta pares (respuesta mala, respuesta
  corregida) desde el WAL como JSONL para fine-tuning DPO/SFT local.

### 3.3 `src/ui/` — cliente de escritorio

- **`sovnode_qt.py`** (~373 KB) — `MainWindow` (PyQt6), el cliente
  principal. Streaming de respuestas, consola de sistema en vivo,
  theming cyberpunk dark, sidebar de sesiones con pestañas, workers
  async (`StreamTurnWorker`, `HealthCheckWorker`,
  `WorkspaceWatcherWorker`, `VoiceRecorderWorker`, `TTSWorker`), tarjetas
  de resultados de búsqueda web, burbujas de chat con Markdown+LaTeX,
  voz completa (Whisper cacheado + TTS), visión (botón "+" de adjuntar
  imagen), y export de chat/dataset de entrenamiento.
  **Tarjeta "Motor de Generación" (2026-09-14)**: `combo_engine`
  (Local/Cloud), `cloud_key_input` (API key, `EchoMode.Password`,
  persistida en `QSettings` vía `_load_cloud_settings`/
  `_on_cloud_key_edited` — nunca se le vuelve a pedir al usuario que la
  retipee), `btn_test_cloud_key` (valida la key contra la API real antes
  de confiar en ella), y `cloud_usage_label` (contador en vivo de
  input/output/costo, ver §5.4). El backend real vive en
  `orchestrator.py::set_cloud_backend`/`_call_claude_api_raw`/
  `_stream_claude_api_raw` (§5).
  **`_AutoHeightTextEdit` (2026-09-13)**: widget que ajusta su alto real
  al contenido (documento → alto real, acotado entre mínimo y máximo,
  scroll interno más allá del máximo) disparado desde `resizeEvent` en
  vez de `textChanged`, porque el contenido de estas tarjetas se fija
  una sola vez al construirlas y el wrap real solo se conoce cuando Qt
  las agrega al layout del padre.
- **`math_render.py`** — Detección de ecuaciones LaTeX y renderizado
  como PNG (matplotlib mathtext). Investigación en curso, no concluida:
  glitches visuales reportados (términos faltantes, `**` sueltos) cuya
  causa raíz exacta todavía no está confirmada empíricamente.
- **`icons.py`** — Iconografía vectorial con `QPainter` puro, sin
  `QtSvg`.
- **`ui.py`** — Widgets Qt genéricos más chicos y viejos, legado parcial.

## 4. Flujo de un turno (alto nivel)

```
mensaje del usuario
  → Orchestrator.process_turn() / run_turn()
  → si hay imagen adjunta: _maybe_prepare_code_fix_from_image() intenta
    redirigir de visión aislada a pipeline normal de archivos (OCR)
  → _classify_turn(): IntentRouter (determinista) decide RoutePath +
    SignalTag(s) (score con WEIGHT_CODE_COMPLEX=0.0 desde 2026-09-16,
    ver §3.1); casi siempre escala DESPUÉS a _llm_router_classify()
    (qwen2.5:0.5b, SIEMPRE local, timeout propio de 10s desde
    2026-09-16 — ver §5.3/§6) para poder upgradear fast→slow ante un
    falso negativo del determinista — salvo 3 atajos que se saltan esa
    ronda por completo porque no puede cambiar nada: esqueleto local,
    descubrimiento de fórmula, o el determinista YA dio slow_path (regla
    de consenso: el 0.5B nunca puede DEGRADAR un slow_path determinista)
  → fetch_hybrid_context(): FTS5 + FAISS, con re-rank y piso de similitud
  → si WEB_SEARCH_INTENT: web_search.py → persist_web_knowledge (TTL 24h)
  → si turno de archivo: tools.py (write_file o edit_file, según
    prefiera el modelo — ver §5.5) / DynamicToolEngine, dentro del
    sandbox activo
  → _call_llm / _call_llm_two_pass: llamada real al motor ACTIVO — Local
    (Ollama, bajo self._llm_lock) o Cloud (API de Claude, tool_use
    nativo) según `cloud_backend_enabled` Y el flag `force_local` del
    call site (§5.3); ambos caminos convergen al mismo formato interno
    de tool call, así que el resto del pipeline no distingue cuál motor
    respondió
  → verificación post-hoc CONSOLIDADA: detectores deterministas baratos
    primero; si algo disparó, UNA sola llamada de corrección combinada
    (nunca 1 por detector)
  → WAL: user_input + response + outcome + (si hubo corrección)
    correction_pair
  → respuesta transmitida a la UI vía PipelineEvent (TOKEN/DONE/...)
```

En paralelo, tres daemons corren en background disparados por
inactividad del usuario: `CognitiveGovernor`, `KnowledgeSynthesizer` y
`FormulaSynthesizer` — los tres corren SIEMPRE contra el motor Local
(nunca gastan crédito del motor Cloud).

## 5. Motor dual: Local (Ollama) vs Cloud (API de Claude)

Sección nueva (2026-09-14). El proyecto arrancó 100% local; esta sesión
de Cowork agregó un motor alternativo que usa la API real de Claude
(`api.anthropic.com`) con el crédito propio del usuario, pensado como
alternativa de velocidad/calidad cuando el usuario elige pagar en vez de
usar su GPU RDNA1 de 8GB. El diseño completo se pensó en 5 prioridades
(razonadas junto con el usuario y Gemini, re-priorizadas por Cowork),
todas implementadas y verificadas en esta sesión:

### 5.1 Toggle y estado (`set_cloud_backend`)

`Orchestrator.cloud_backend_enabled` (bool), `cloud_api_key`,
`cloud_model_id` (default `CLOUD_DEFAULT_MODEL = "claude-sonnet-5"`), y
`cloud_usage_totals` (contador acumulado de tokens/costo). Togglear el
motor NO reinicia nada más — es un simple flag que decide, en cada
llamada, si `_call_llm_raw`/`_stream_llm_raw` van a Ollama o a
`_call_claude_api_raw`/`_stream_claude_api_raw`.

### 5.2 Tool-calling nativo (en vez de imitar texto)

Antes de esta sesión, cualquier motor tenía que emitir el bloque de
texto `` ```json {"tool": ..., "parameters": ...} ``` `` para que
`extract_tool_call` lo parseara — una convención pensada para modelos
locales sin API de tools real. Ahora, cuando el motor Cloud está activo,
se le manda el schema NATIVO de la Messages API de Anthropic
(`CLOUD_TOOLS_SCHEMA`: `tools` + `input_schema` JSON Schema) y Claude
devuelve bloques `tool_use` reales. `_call_claude_api_raw`/
`_stream_claude_api_raw` traducen ese bloque `tool_use` de vuelta al
mismo formato interno `` ```json {"tool": ...} ``` `` — así el resto del
pipeline (el mismo `execute_tool_from_call`, los mismos guards de
riesgo/falso-éxito/ruta-alucinada) no necesita saber ni le importa qué
motor respondió. `CLOUD_TOOLS_SCHEMA` hoy tiene 7 herramientas:
`write_file`, `edit_file`, `read_file`, `list_dir`, `run_cmd`,
`system_telemetry`, `web_search` (el comentario del código que dice "5"
quedó desactualizado tras agregar `edit_file`, ver §10). `web_search`
(`patch_orchestrator104`, §8 ítem 43) es la incorporación más reciente:
antes de ese patch, la búsqueda web NUNCA fue una herramienta agéntica
real -- vivía en un pipeline aparte que decidía buscar o no ANTES de que
el modelo viera el mensaje del usuario (`_should_force_web_search`,
gateado por el router determinista de `router.py`). Ahora es una
herramienta más, exactamente igual que `read_file`/`write_file`: el
modelo la pide cuando la necesita, con la conversación completa ya en la
cabeza.

### 5.3 Routing granular Local/Cloud (`force_local`)

Togglear Cloud en la UI es una decisión GLOBAL del usuario, pero no
todas las llamadas internas deberían obedecerla ciegamente. Se agregó un
parámetro `force_local: bool = False` a `_call_llm_raw`/`_call_llm`/
`_stream_llm_raw` (gate: `if self.cloud_backend_enabled and
self.cloud_api_key and not force_local`), puesto en `True` en
exactamente 3 call sites reales (de ~25 auditados):

- **Vision** (`perf_label="Vision"`, moondream): `_call_claude_api_raw`/
  `_stream_claude_api_raw` NO tienen parámetro `images` — sin este fix,
  una imagen adjunta se perdía en silencio con Cloud activo y Claude
  recibía un prompt pensado para un modelo de visión sin ninguna imagen
  real. Esto era un bug de CORRECTITUD, no solo de costo.
- **Router0.5B** (`perf_label="Router0.5B"`, `_llm_router_classify`) y
  **VerifyLite** (`perf_label="VerifyLite"`, `_correct_response`): ambos
  usan `router_model` (qwen2.5:0.5b) precisamente PORQUE son baratos y
  chicos — con Cloud activo se estaban upgradeando en silencio a Claude
  Sonnet a precio completo, un leak de costo que además derrotaba el
  propósito de tenerlos.

Los ~22 call sites restantes (generación principal, Tree-of-Thought,
salvataje de archivos, etc.) quedaron sin tocar — SÍ deben respetar el
toggle del usuario.

### 5.4 Prompt caching y contabilidad de costo de 3 niveles

El `system` prompt y el array `CLOUD_TOOLS_SCHEMA` (100% estático entre
llamadas) llevan `"cache_control": {"type": "ephemeral"}` — Anthropic
cachea todo desde el principio del request hasta ese punto inclusive,
con hit garantizado después de la primera llamada de la sesión.
`_account_cloud_usage` distingue 3 tipos de input token, cada uno con su
propio precio (`CLOUD_PRICING_USD_PER_MTOK` por modelo: input normal,
`CLOUD_CACHE_WRITE_MULTIPLIER = 1.25×` sobre el precio de input la
primera vez que se escribe ese prefijo al caché, `CLOUD_CACHE_READ_MULTIPLIER
= 0.1×` en cada hit posterior) — la contabilidad anterior trataba todo
input igual, lo cual subestimaba el costo real de un cache-write y lo
sobreestimaba brutalmente en un cache-hit. `cloud_usage_label` en la UI
muestra el contador acumulado en vivo.

### 5.5 `edit_file`: parches SEARCH/REPLACE en vez de reescribir todo

El problema medido: `_current_file_context` inyectaba el archivo entero
en el prompt y pedía el archivo COMPLETO de vuelta dentro de
`write_file` para CUALQUIER modificación — el costo de output es
proporcional al tamaño del archivo, no al tamaño del cambio; con el
motor Cloud eso es dinero real por cada línea que no cambió. Fix:
`edit_file(path, old_str, new_str)` (ver §3.2 para el contrato de
unicidad), expuesta en ambos schemas (`TOOLS_SCHEMA` local y
`CLOUD_TOOLS_SCHEMA`), clasificada como riesgo MEDIUM en
`_classify_tool_risk` (por definición opera sobre un archivo ya
existente), cubierta por los mismos guards que `write_file`
(falso-éxito-bloqueado, ruta-alucinada) y por el reindexado RAG
inmediato (releyendo el archivo del disco después de la edición, ya que
`edit_file` no trae el contenido completo en sus parámetros).
`_current_file_context`, `_file_write_tool_reminder` y
`_get_file_ops_system_prompt` (los 3, en ambos idiomas) fueron
reescritos para instruir: preferí `edit_file` con un `old_str`/`new_str`
chico y único; caé en `write_file` completo solo si el cambio es
demasiado grande o disperso para un único par.

**2026-09-17**: cuatro ajustes puntuales sobre este mecanismo, detalle
completo en §8 (`orchestrator58`/`60`/`63`). (a) `edit_file` tiene su
propio techo dinámico, `_dynamic_edit_budget_tokens` (multiplicador
`_EDIT_FILE_BUDGET_MULTIPLIER = 2.75`, sin calibrar todavía -- ver §10),
separado de `_dynamic_write_budget_tokens` (1.5x): el par `old_str`+
`new_str` duplica la región cambiada más el overhead estructural del
JSON, así que el mismo techo que alcanza para `write_file` puede quedarse
corto para `edit_file`. (b) el texto de `[OUTPUT BUDGET]` para la pasada
inicial y para los reintentos ahora tiene una rama específica para
`edit_file` (diff mínimo, usar `edits` para varios cambios no
relacionados, aviso de costo doble si se repite código sin cambios). (c)
si un `edit_file` se corta por el techo (`done_reason == "length"`), ya
no se intenta empalmar el JSON parcial (inseguro para una estructura) --
`_rescue_truncated_edit_file_locally` descarta el intento y regenera
completo en el motor Local ($0), separado del mecanismo de
`write_file` (`_continue_truncated_file_write_locally`, que sí empalma
texto plano). (d) el gate `len(_cur) <= 4500` que evitaba llamar a
`_current_file_context` para archivos grandes se sacó de sus dos
call-sites -- la función ya trunca sola su propio preview a 4500
caracteres, así que el gate solo lograba ocultarle el NOMBRE del archivo
resuelto al modelo en archivos grandes, sin ahorrar nada.

### 5.6 "Zero-Chatter" (brevedad tras una herramienta exitosa)

Menor en alcance pero aplica a AMBOS motores: cuando una tool se ejecutó
bien y no hay nada más relevante que decir, la instrucción de cierre
(`_zero_chatter`, dentro del bucle de tool-calling de `run_turn`) pide
una confirmación de 1-2 frases — prohibido agregar preguntas de
seguimiento o sugerencias no pedidas. Si hubo un error o algo relevante,
se mantiene la explicación completa de siempre.

## 6. Modelos y por qué son varios

- `general_model` (`qwen2.5:7b` uncensored por defecto, override
  `OLLAMA_MODEL`/`OLLAMA_GENERAL_MODEL`) — el modelo por defecto para
  TODO turno en el motor Local; su equivalente en el motor Cloud es
  `cloud_model_id` (default `claude-sonnet-5`, ver §5.1).
- `coder_model` (override `OLLAMA_CODER_MODEL`) — activo solo cuando
  `SignalTag.CODE_COMPLEX`, motor Local.
- `fast_write_model` (override `OLLAMA_FAST_WRITE_MODEL`) — **desactivado
  por defecto** (`SOVNODE_ENABLE_FAST_WRITE_MODEL=0`): con
  `OLLAMA_MAX_LOADED_MODELS=2`, un tercer modelo fuerza recarga de VRAM
  (~12s medidos).
- `router_model` (`qwen2.5:0.5b`, override `OLLAMA_ROUTER_MODEL`) —
  clasificación rápida y (2026-09-16: **activado por default**, antes
  opt-in — ver más abajo) primer intento de corrección de idioma.
  **Siempre local, con Cloud activo o no** (`force_local=True`, ver
  §5.3), forzado a `num_gpu=0` (CPU) para no competir por VRAM con
  `general_model`/`coder_model`. Su llamada de clasificación tiene un
  timeout propio de 10s desde 2026-09-16 (`ROUTER_LLM_TIMEOUT_SECONDS`,
  vía un `timeout_override` explícito en `_prepare_ollama_payload`) —
  antes quedaba atado al `OLLAMA_TIMEOUT_SECONDS` general (120s) pese a
  generar solo 8 tokens, así que un Ollama colgado podía bloquear un
  turno entero por un chequeo semántico que ya tolera fallar con
  gracia.
- `embed_model` (`nomic-embed-text`, no usado directamente — embeddings
  reales corren locales en proceso vía `embeddings.py`).
- `vision_model` (`moondream` por defecto, override `OLLAMA_VISION_MODEL`)
  — activo solo con imagen adjunta. **Siempre local** (`force_local=True`,
  ver §5.3 — `_call_claude_api_raw` no soporta imágenes). Carril
  dedicado, temprano en `run_turn`, con cabecera y prompt propios y
  mínimos, sin protocolo `<thought>`; puede redirigirse al pipeline
  normal de archivos vía OCR si la imagen es un error de código (§3.1,
  `ocr_reader.py`).

Family switch: si el modelo Local apunta a la familia `gpt-oss`
(Harmony), `think_level` activa el budget de razonamiento; inerte con
`qwen2.5`. No aplica al motor Cloud.

## 7. Testing

`tests/test_regressions.py` es la suite real (aserciones + exit code),
organizada en **63 secciones numeradas**, cada una documentando el bug
REAL que la motivó — en la práctica, un changelog ejecutable. Cubre
hasta la sección 63 (rediseño visual de la UI); **el trabajo del motor
Cloud (§5) NO tiene secciones ahí** — se verificó con scripts
`verify_*.py` dedicados y desechables (uno por prioridad:
tool-calling nativo, prompt caching, routing granular, `edit_file`),
corridos fuera del repo y luego descartados, siguiendo el mismo patrón
de aserciones pero sin integrarse a la numeración de la suite principal.

**Al correr la suite en un workspace nuevo/descartable**, hacen falta
dos parches de entorno NO relacionados con bugs de producto:
1. `_rag.documents` debe ser un dict, no una lista — si no,
   `LocalVectorRAG.query` explota con `AttributeError`.
2. Un `Orchestrator` construido con `object.__new__` necesita
   `_o._vector_rag_lock = threading.Lock()` seteado a mano antes de
   `fetch_hybrid_context`.

Con esos dos parches, la suite corre limpia salvo 2 fallas preexistentes
y no relacionadas (specs de ejemplo `git_status`/`ping_host`; un mensaje
sobre el evento VERIFICATION por detector en vez de uno solo) — ninguna
indica una regresión real.

**Sesión 2026-09-16**: los cinco fixes de esa fecha (§3.1, §4, §6, §8)
se verificaron igual que los del motor Cloud — `verify_router1.py`,
`verify_orchestrator36.py`, `verify_orchestrator37.py` (este último
reproduce el turno real que falló, leyendo el WAL de producción) y
`verify_orchestrator38.py`, todos con ejecución real (no solo lectura
de texto fuente), corridos y descartados. La ÚNICA excepción a "descartable" de esta sesión: el cambio de default de
`lang_fix_light_model_enabled` sí tocó `test_regressions.py` en serio
(el bloque de la Sección 40 que verificaba el default VIEJO,
`SOVNODE_ENABLE_LANG_FIX_LIGHT_MODEL` default `"0"`, se actualizó al
default nuevo `"1"` — un cambio de comportamiento intencional necesita
que la suite lo refleje, no solo un script desechable aparte).

**Sesión 2026-09-17** (patches `orchestrator57` a `orchestrator63`,
detalle en §8/§5.5): a diferencia de casi toda la sesión anterior, estos
siete parches NO se corrieron contra ningún script `verify_*.py`
dedicado ni contra la suite de regresión -- se verificaron con `ast.parse`
sobre el archivo completo tras cada edición (vía el flujo de
stage/edit/commit del puente remote-devices, sin `device_bash`
disponible en ningún momento de la sesión) más lectura directa del WAL de
producción (`sovnode.wal`) para confirmar el diagnóstico de cada bug
(`turn_id`, `codegen_budget_plan`, `response`) antes y para confirmar la
corrección después, turno por turno, tal como reportaba el usuario cada
prueba real. Es una verificación más liviana que la de rounds anteriores
(sin regeneración aislada de escenarios) -- queda pendiente sumar
secciones a `test_regressions.py` o al menos un `verify_*.py` desechable
para estos siete parches si se quiere blindarlos contra una regresión
futura.

## 8. Historial resumido de decisiones de arquitectura

(Cronológico, condensado — ver memoria de usuario para el detalle
turno-a-turno.) El proyecto arrancó como especificación puramente
conceptual ("El Monolito Personal"), revisado varias veces antes de
convertirse en código real, primero en Flet, después reimplementado en
PyQt6 y rebautizado **SovNode**. Iteraciones tempranas: re-ranking de
contexto híbrido + chunking AST, panel de métricas en vivo, rediseño de
UI, TTS en español, timeout HTTP dinámico, las "palancas de rendimiento"
(esqueletos locales / KV-cache — revertida por incompatibilidad RDNA1),
verificación post-hoc consolidada, piso de similitud coseno en el RAG,
motor de descubrimiento formal falseable con su bug de notación
corregido de raíz, guiado semántico. Ronda de blindajes de seguridad
2026-09-07: fuga de aislamiento en `ToolSandbox`, exclusiones de router
(superlativo histórico, "función" matemática), búsqueda de imágenes
dedicada. 2026-09-08: respuestas con evidencia web más desarrolladas,
header fast-path liviano, modo voz completo, estructura moderada tipo
Gemini, visión local (Moondream) + botón de adjuntar imagen. 2026-09-09:
OCR sobre capturas para corregir código desde una imagen
(`ocr_reader.py`). 2026-09-11: segunda ronda de blindajes de router
(morfología de "función"/"clase"/"método" en contextos matemáticos).
2026-09-13: auto-ajuste de alto en tarjetas de la UI. **2026-09-14
(esta sesión de Cowork): motor Cloud completo** — toggle Local/Cloud en
la UI con persistencia de API key, tool-calling nativo de la Messages
API (reemplazando la imitación por texto), prompt caching con
contabilidad de costo de 3 niveles, routing granular (`force_local`)
para no upgradear en silencio llamadas baratas ni perder imágenes
adjuntas, `edit_file` (parches SEARCH/REPLACE en vez de reescritura
completa), y el protocolo "Zero-Chatter" — las 5 prioridades del plan
razonado con el usuario y Gemini, todas verificadas contra la suite de
regresión existente (sin romper el motor Local) más scripts `verify_*.py`
dedicados por prioridad. **2026-09-16 (sesión de Cowork, cinco fixes
puntuales sobre routing y corrección de idioma, NADA de motor dual)**:
(a) `WEIGHT_CODE_COMPLEX` 3.0→0.0 en `router.py` — un pedido de código
puro dejó de enrutar a slow_path por sí solo, arreglando un bug real
medido por WAL (circuit-breaker de slow_path + fallback genérico tras
dos llamadas pagas a Cloud sin código entregado; el propio usuario
eligió esta opción — sacar el código de slow_path directamente — sobre
arreglar la regeneración de slow_path); (b) detección de dependencias
cruzadas entre sugerencias de una misma respuesta en modo sugerencia
(código pegado >100 líneas), más un chequeo de sintaxis específico para
ese modo que ya no confunde un fragmento parcial válido con uno roto;
(c) memoria del último código GENERADO por el modelo
(`_last_generated_code_text`, mismo patrón que la memoria de código
PEGADO que ya existía) — un turno de seguimiento tipo "mejorá ese
código" ya no depende del historial recortado a 800 caracteres
(`HISTORY_ENTRY_CHAR_CAP`) y por lo tanto ya no le dice al usuario que
el código anterior "se cortó" cuando en realidad estaba completo; (d)
timeout propio de 10s para el Router0.5B (antes expuesto a los 120s de
`OLLAMA_TIMEOUT_SECONDS` pese a generar 8 tokens); (e)
`lang_fix_light_model_enabled` activado por default, recién después de
agregar `_light_lang_fix_looks_unfaithful` — un blindaje que compara
números/nombres propios entre el original y la traducción del modelo
liviano, dirigido puntualmente contra un bug YA MEDIDO en este código
(corrección de visión, 2026-09-08: una foto de un globo aerostático
"corregida" a una alucinación fluida de una nave espacial, que pasaba
`find_language_mismatch` sin problema porque ese detector mide idioma,
no fidelidad).

**2026-09-16, SEGUNDA sesión de Cowork ese mismo día** (posterior a los
cinco fixes de arriba, conversación distinta): negociación proactiva de
alcance para escritura de archivos NUEVOS (`is_file_write and not
mod_is_modify`), caso que antes no tenía ninguna -- solo la reactiva
`_continue_truncated_file_write_locally` (completa gratis en Local lo
que Cloud cortó, DESPUÉS de haber pagado el intento entero). Dos piezas
nuevas junto a `_wants_codegen_budget` en `run_turn` (buscar
`_codegen_budget_infeasible`): (a) `_estimate_min_viable_codegen_tokens`
(clasificación determinista por palabras clave -- pygame/juego=450,
tkinter/gui=400, flask/servidor=300, default=120 -- **heurística sin
calibrar contra WAL real todavía**, a diferencia de la mayoría de las
constantes de este archivo) estima el piso técnico de CUALQUIER versión
mínima viable de un pedido, sin importar cuánto se recorte el alcance;
si el techo real (`gen_predict`) queda por debajo de ese piso, el turno
corta ANTES de llamar al LLM (mismo patrón zero-tokens que
`skeleton_match`/`formula_discovery_match`) y se lo dice al usuario en
vez de intentar igual; (b) si el techo alcanza pero puede no sobrar, se
inyecta al `prompt` un aviso `[OUTPUT BUDGET]`/`[PRESUPUESTO DE SALIDA]`
adaptado a `write_file`/`edit_file` -- mismo texto y mecanismo que ya
usaba el carril de código-en-chat, pero ese SOLO corría con `if not
is_file_write`. Verificado con un script desechable fuera del repo
(mismo patrón `verify_*.py`) que confirmó que, con los techos reales
actuales del proyecto (900 Cloud Bajo hasta 6144 Local), el piso más
exigente modelado (450, juegos) SIEMPRE entra -- hoy el corte en cero
tokens es una red de seguridad correcta pero que casi nunca dispara con
la configuración actual; dispararía ante un techo futuro más bajo que
450, o un pedido de categoría no modelada (BD, multijugador/red) que
caiga en el default de 120 siendo en realidad más exigente.
**Pendiente**: sumar esas categorías y calibrar los cuatro números
contra generaciones reales medidas por WAL. No se tocó
`_suggestion_mode_active` (patch_orchestrator44.py, mismo día, otra
sesión -- cubre un problema DISTINTO: código EXISTENTE grande a
modificar, no un pedido nuevo desde cero) ni
`_continue_truncated_file_write_locally` (sigue como red de seguridad
si esta estimación nueva se queda corta). Backup manual antes del
cambio: `orchestrator.py.bak46` (git seguía sin inicializarse en esta
carpeta al momento de este cambio).

**2026-09-16, mismo día, DOS bugs reales medidos en vivo por el usuario
probando el patch anterior** (pedido: "dame un codigo de agar.io con
bots", motor Cloud, presupuesto "Bajo" ~900 tok elegido -- resultado:
pagó 1800 tok de salida Y recibió un archivo roto, sin loop del juego,
con un `bots.remove(jugador)` que iba a tirar `ValueError` porque
`jugador` nunca estuvo en `bots`):

1. **Precedencia de presupuesto rota** (independiente del patch anterior
   de esta sesión -- ya existía): `gen_predict = max(gen_predict,
   self._effective_codegen_num_predict())`, junto a `_wants_codegen_
   budget`, solo puede SUBIR `gen_predict`, nunca bajarlo. Con Cloud
   activo, `_effective_codegen_num_predict()` YA es el techo real en
   centavos que el usuario eligió (`_cloud_output_ceiling_tokens`,
   garantía "nunca más de N centavos") -- pero si el turno cae en
   slow_path (`SLOWPATH_NUM_PREDICT=1800`), `max(1800, 900)` gana el
   default de slow_path e ignora en silencio la elección real del
   usuario. Fix: con Cloud activo, `gen_predict` pasa a ser
   `_effective_codegen_num_predict()` directamente (nunca se sube);
   Local mantiene el `max()` de siempre (ahí no hay costo real que
   cuidar).
2. **`find_language_mismatch` sigue con un hueco tras el fix de hoy
   temprano** (el de "función"/prosa corta, ver más arriba en este
   mismo §8): ese fix cubre "hay algo de prosa española real alrededor
   del código"; NO cubre "la respuesta es CASI TODO código, sin
   prácticamente ninguna prosa propia" (exactamente un pedido de código
   puro, sin nada más). En ese caso, tras recortar el bloque \`\`\`,
   `detection_text` queda vacío/ambiguo y el código caía al fallback de
   detectar sobre el texto SIN recortar -- dominado por palabras clave
   en inglés del propio código, falso positivo de "inglés". Ese falso
   positivo dispara `_correct_response`, que con
   `lang_fix_light_model_enabled` activo por default (ver arriba)
   manda el archivo COMPLETO a `router_model` (qwen2.5:0.5b, NO un
   modelo de código) con apenas 900 tokens para "corregir el idioma" --
   el resultado observado (texto "En inglés traduciré su respuesta:"
   seguido de un juego mutilado) es ese modelo chico fallando en la
   tarea. `_light_lang_fix_looks_unfaithful` no lo atajó porque compara
   números/nombres propios (pensado para texto/visión), no estructura
   de código. Fix aplicado: si `detection_text` (tras sacar el código)
   queda por debajo de 12 caracteres, se trata como "sin señal de
   idioma que verificar" y se devuelve `False` directamente, SIN caer
   al fallback de texto completo -- no se tocó el fallback en sí para
   el caso de prosa corta-pero-presente, que sigue funcionando igual
   que desde el fix de esta mañana. **No se tocó** el pipeline de
   `_correct_response`/`_light_lang_fix_looks_unfaithful` en sí --
   sigue siendo una superficie de riesgo real para CUALQUIER futuro
   caso donde `find_language_mismatch` dé un verdadero positivo sobre
   una respuesta de código (el fidelity-check no distingue "perdí un
   nombre propio" de "perdí el loop del juego"); quedó marcado como
   nota para quien continúe (ver §10) en vez de resuelto de fondo.

Verificado con dos scripts desechables fuera del repo (mismo patrón
`verify_*.py`): uno reproduce la aritmética de `gen_predict` para los 5
casos reales (fast/slow × techo elegido × Cloud/Local); otro aísla la
rama de decisión de `find_language_mismatch` con detectores simulados
para forzar exactamente el escenario medido (texto recortado
ambiguo/vacío + texto completo detectado "English") y confirma que el
fix lo suprime sin tapar un mismatch real (intro genuina en el idioma
equivocado, con prosa real). No se corrió el turno real contra Ollama/
Claude desde acá (sin acceso a shell en la compu del usuario en el
momento de este cambio) -- pendiente de confirmar en vivo quedó
SUPERADO por lo que sigue: el usuario sí lo probó, y el fix de arriba
NO alcanzó.

**2026-09-16, mismo día, tercer round -- el usuario confirmó en vivo
que el fix de `find_language_mismatch` de arriba NO alcanzó.** Repitió
el mismo pedido dos veces más; la segunda (fast_path esta vez, 900 tok
respetados -- Bug A parece resuelto, aunque fast_path==techo elegido
por coincidencia numérica, no confirma el fix de forma concluyente)
igual disparó VerifyLite otra vez y el archivo salió PEOR que antes
(variables nunca definidas: `bots`, `bot`, `num_bots` en minúscula,
`math` directamente desaparecido del import). Diagnóstico esta vez con
evidencia real, no inferida: se leyó `sovnode.wal` directamente (evento
`correction_pair`, 17:09:34, campos `original` y `corrected` -- el WAL
los guarda literales) en vez de asumir. El `original` (la respuesta
REAL de Sonnet, 900 tok) es un script bastante mejor estructurado que
el resultado final -- imports correctos, `bots = [...]` presente,
`ia_bot(bot)` con el parámetro bien usado -- cortado LIMPIAMENTE a
mitad de `ia_bot` por el techo de tokens (900 no alcanzó para "agar.io
con bots" ni siquiera recortado: el propio usuario ya había sospechado
esto -- "aunque yo creo que incluso eso sería demasiado" -- y con
bots/IA parece tener razón; dato real para calibrar el piso de 450 de
`_estimate_min_viable_codegen_tokens`, que modela "juego" como una sola
categoría sin distinguir "con IA/bots" de un Snake/Pong -- **pendiente**,
no se tocó el piso todavía, hace falta más de un solo dato antes de
mover ese número). El BUG real: el bloque \`\`\`python nunca cierra (se
cortó a mitad, sin \`\`\` de cierre) -- el fix anterior
(`_strip_code_for_lang_detection` + el umbral de 12 caracteres en
`find_language_mismatch`) daba por sentado que un bloque de código sin
cerrar simplemente NO EXISTE para `_FENCED_CODE_BLOCK_RE` (que exige
apertura Y cierre) -- así que el código entero sin cerrar (1782
caracteres reales, medidos) sobrevivía intacto como `detection_text`,
con señal de "inglés" clarísima e inmediata (no ambigua), así que el
chequeo de "menos de 12 caracteres" nunca se alcanzaba a evaluar. Fix
real: `_strip_code_for_lang_detection` ahora, después de sacar los
bloques bien cerrados, busca un \`\`\` SUELTO (abre sin cerrar) y corta
ahí -- todo lo que sigue se trata como código también, se conserva solo
la prosa que vino ANTES de ese \`\`\` suelto. Re-verificado con el TEXTO
REAL de este turno (leído del WAL, no un ejemplo inventado): antes del
fix, 1782 caracteres sobrevivían para detectar; después, 0 -- cae
correctamente al camino "sin señal de idioma, no corregir". Backup
manual antes del cambio: `orchestrator.py.bak48`.

**2026-09-16, mismo día, CONFIRMADO EN VIVO (cuarta prueba, con
video).** El usuario grabó un video (sin audio, 28s) de repetir el
mismo pedido ("dame un código de agar.io con bots", Cloud, Bajo) desde
cero. Resultado, leído frame a frame del video ya que no tiene audio:
la generación original de Sonnet se cortó de nuevo a mitad de una
expresión (`objetivo = min(comidas, key=lambda c: dist(bot...`, mismo
patrón de siempre con este pedido concreto a 900 tok), pero esta vez
**no disparó ni Router0.5B ni VerifyLite** -- el desglose de la consola
del turno final muestra `Generación 17.4s (LeanSingle 7.4s •
CodeSyntaxFix 6.9s • Router0.5B 3.0s) (total 17.5s)`, con
`CodeSyntaxFix` en vez de `VerifyLite`. `CodeSyntaxFix` es un mecanismo
YA EXISTENTE en el código (línea ~5690-5799, no agregado por ninguno de
estos rounds) que corre `_find_python_syntax_error` (basado en
`ast.parse`) sobre CUALQUIER respuesta de chat después de
`_close_unbalanced_code_fence`, y si detecta Python roto hace UNA
llamada correctiva con el modelo COMPLETO activo (no el 0.5B), pidiendo
la respuesta reescrita entera. Con el falso positivo de idioma ya
resuelto por el fix de arriba, este mecanismo -- más seguro que
VerifyLite porque usa el modelo real y no un modelo de 0.5B para tareas
de idioma -- pudo actuar limpio: el chat final mostró "Tenías razón, el
código se cortó justo en la línea del `min(comidas, key=lambda c:
dist(bot...` y el paréntesis quedó abierto. Acá va el código completo y
funcional:" seguido de un script reescrito completo, con imports
correctos (`import pygame, random, math`), función `dist` bien resuelta,
loop principal real (`while corriendo: ... reloj.tick(60) ...`), sin
variables indefinidas visibles. Costo total: 2 llamadas Cloud · 6090 tok
in / 1800 tok out · $0.0318 -- dentro de lo esperable para dos pasadas
de 900 tok cada una. **Con esto, el fix de Round 3 queda confirmado en
vivo**: ya no alcanza a corromper el código porque el falso positivo de
idioma que lo disparaba está resuelto, y el mecanismo de respaldo que
sí corre (`CodeSyntaxFix`) es estructuralmente más seguro que el que
disparaba antes (`VerifyLite`/`_correct_response` con el modelo
liviano). Sigue pendiente, sin cambios desde el round anterior: (a)
calibrar el piso de 450 para "juego con IA/bots" con más datos, y (b)
la superficie de riesgo de fondo en `_correct_response`/
`_light_lang_fix_looks_unfaithful` para el caso de un mismatch de
idioma VERDADERO (no falso positivo) sobre una respuesta de código --
ver §10, no se tocó en ningún round.

**2026-09-16, mismo día, cuarto round -- calibración del negociador de
alcance (`[OUTPUT BUDGET]`/`[PRESUPUESTO DE SALIDA]`).** El usuario
probó un segundo pedido con el mismo patrón ("dame un juego de tank.io
con bots", Cloud, Bajo) y el resultado repitió la forma del problema:
la primera pasada se cortó de nuevo (2 llamadas · 5814 tok in / 1800
tok out · $0.0312 -- otra vez el doble del techo elegido, salvado por
`CodeSyntaxFix` y no corrompido, pero pagando dos pasadas completas en
vez de una). Diagnóstico: el bloque de negociación de alcance ya
existente le pedía al modelo "si no entra, recortá el alcance (menos
bots/enemigos, IA más simple)" en texto genérico y CONDICIONAL -- pero
sin un número concreto, evidencia real (WAL, ambos turnos) mostró que
el modelo igual escribía `NUM_BOTS = 6`, `NUM_COMIDA = 400` o similar,
con varias líneas de IA por entidad, y recién ahí se quedaba sin
presupuesto. Un límite blando sin número no alcanza.

Fix: se agregó `_suggest_codegen_entity_cap(extra_budget_tokens)`
(cerca de `_estimate_min_viable_codegen_tokens`, mismo estilo
determinista sin LLM) que traduce el presupuesto que sobra por encima
del piso de andamiaje (`extra = gen_predict - _min_viable_floor`) en
un tope CONCRETO de entidades/bots: `max(1, min(3, extra // 150))`
(`_CODEGEN_EXTRA_TOKENS_PER_ENTITY = 150`,
`_CODEGEN_MAX_ENTITY_HINT = 3` -- primer valor razonable, NO calibrado
todavía contra generaciones reales, igual que el piso de
`_estimate_min_viable_codegen_tokens`). `_min_viable_floor` ahora se
calcula SIEMPRE que se quiere presupuesto de codegen (antes solo para
`is_file_write` nuevo), para poder derivar el "extra" en las DOS ramas
del prompt (carril de chat y write_file/edit_file) -- el corte
temprano por infeasibilidad (`_codegen_budget_infeasible`) sigue
exactamente igual que antes, solo para archivo nuevo. El texto de
`[OUTPUT BUDGET]`/`[PRESUPUESTO DE SALIDA]` pasó de condicional y sin
números ("si no entra, recortá") a imperativo y con desglose concreto:
"~{floor} tokens van al andamiaje -- te quedan ~{extra} para el resto;
si hay bots/enemigos, usá COMO MÁXIMO {N} de ellos, con la IA más
simple posible (una línea, sin pathfinding); NO agregues menús,
sonido, HUD, múltiples estados, ni comentarios largos; si dudás si
algo entra, recortá MÁS, no menos". Backup manual antes del cambio:
`orchestrator.py.bak49`.

**2026-09-16, mismo día, quinto round -- el circuit-breaker de
fast_path también corrompía turnos de código (bug distinto,
encontrado probando el fix del cuarto round).** El usuario probó
"dame un codigo de tank.io con bots" (Cloud, Bajo) y esta vez NI
SIQUIERA llegó a ver código: la consola mostró `Circuit-breaker
fast_path: bucle de repetición degenerativo — regenerando con prompt
mínimo`, y la respuesta final fue un texto genérico ("No puedo
compartir un código completo... decime en qué lenguaje o motor") de
solo 200 tokens, tras haber pagado 900 tokens completos en la primera
pasada real (2 llamadas · 4257 tok in / 1100 tok out · $0.0202 --
plata gastada, cero código entregado). Diagnóstico por WAL directo
(`sovnode.wal`, turno `fc0f7d60`, evento `fastpath_circuit_breaker`
con `reason: "bucle de repetición degenerativo"`, seguido del evento
`response` final con el texto de rechazo): el circuit-breaker de
fast_path (`_fastpath_response_looks_broken`, pensado para prosa que
se descarriló) corre siempre que `decision.path == FAST_PATH and not
is_coder` -- y `is_coder` está fijo en `False` en toda la arquitectura
de motor único, así que en la práctica corre sobre CUALQUIER turno de
código en fast_path también. Dos de sus chequeos son falsos positivos
reales para código: (a) `_looks_degenerate_repetition`
(`(.{2,24}?)\1{12,}`, la misma subcadena corta repetida 12+ veces
seguidas) puede matchear estructuras de código perfectamente válidas
-- verificado con un ejemplo concreto: una fila de mapa tipo
`"####################################"` (común en un juego con
paredes/laberinto como tank.io) dispara la regex sin que haya nada
roto; (b) `hit_ceiling and len(body) > 2600` asume que llenar el techo
es señal de descarrilamiento, pero con presupuesto de codegen angosto
a propósito (900 tok ~ hasta ~2970 caracteres) un código real cortado
por el presupuesto dispara esto con total normalidad. Confirmado por
WAL que esto NO es exclusivo de hoy ni de la negociación de alcance
del round anterior: el mismo motivo ("bucle de repetición
degenerativo") ya había disparado esa misma mañana (09:07:18) sobre un
turno totalmente distinto ("mejora ese codigo") -- es un riesgo
estructural preexistente del breaker sobre cualquier turno de código
en fast_path, no algo introducido por el round 4. El resultado medido
en ambos casos fue peor que no hacer nada: se descartó código real
(bueno o cortado) y se lo reemplazó por una regeneración de 200 tokens
con `_build_fastpath_regen_prompt` (prompt trivial, sin contexto de
código ni de presupuesto), que en la práctica es casi siempre un "no
puedo compartir el código completo acá" -- cero código entregado, y
más caro (la primera pasada ya se había pagado completa). Fix: el gate
que activa este breaker ahora excluye turnos de codegen
(`not _wants_codegen_budget`, el mismo patrón que ya usaba el gate de
`fast_path_truncated_continuation` un poco más abajo) -- los turnos de
código ya tienen sus propias redes de seguridad específicas para
código (`_continue_truncated_file_write_locally` para write_file,
`CodeSyntaxFix` con `ast.parse` para cualquier respuesta de chat,
confirmado funcionando en el round 3), así que se les deja pasar sin
la interferencia de un breaker pensado para prosa. Backup manual antes
del cambio: `orchestrator.py.bak50`. **Este fix quedó luego REEMPLAZADO
por el del round 8, más abajo -- ver ahí el porqué.**

**2026-09-16, mismo día, sexto round -- el MISMO bug en el
circuit-breaker de slow_path.** Repitiendo la prueba con "dame un
codigo de tank.io" (sin "con bots") el turno ruteó a SLOW_PATH y
disparó `_slowpath_response_looks_broken` con el mismo motivo sobre
código real de 900 tok -- mismo patrón, mismo resultado (rechazo
genérico de 200 tok tras pagar la pasada completa). El fix del round 5
solo tocaba el breaker de fast_path; `_slowpath_response_looks_broken`
es un breaker DISTINTO (`decision.path != FAST_PATH`) que reusa el
mismo `_looks_degenerate_repetition`. **Este fix también quedó
reemplazado por el del round 8.** Nota que sigue vigente: hay un
SEGUNDO call-site de `_slowpath_response_looks_broken` en
`process_turn` (~línea 13168, método separado, no-generador, parece
ser el usado por `app.py` Streamlit en vez del cliente PyQt6
principal) que NO se tocó en ningún round -- no es el camino probado en
vivo esta sesión, y no tiene `_wants_codegen_budget` en su scope.

**2026-09-16, mismo día, séptimo round -- idea del usuario: techo duro
real, pero un OBJETIVO con margen (rango) en vez de pedirle al modelo
que llene el presupuesto exacto.** Tras ver la generación cortarse
justo en el borde exacto del techo repetidamente, el usuario propuso
un rango de tokens según el esfuerzo. El `num_predict` real que se le
paga a la API NO puede volverse un rango -- es la garantía de costo de
`_cloud_output_ceiling_tokens` (§5), tiene que quedar fijo. Lo que SÍ
se volvió más flexible es el OBJETIVO comunicado en el prompt: se
agregó `_codegen_soft_target(hard_ceiling, min_viable_floor)` --
calcula un objetivo más chico (`_CODEGEN_SOFT_MARGIN_FRACTION = 0.15`,
apuntar al 85% del techo real, sin calibrar todavía) y el prompt ahora
dice explícitamente DOS números: "TECHO DURO ~900 tok (pasarte de acá
corta la respuesta, sin excepción) -- pero APUNTÁ a terminar cómodo
alrededor de ~765 tok o menos, dejando margen". El desglose de
andamiaje/tope de entidades (round 4) ahora se calcula sobre ese
objetivo con margen, no sobre el techo duro crudo. Verificado con un
script desechable aislado. Backup: `orchestrator.py.bak51`.
**Confirmado en vivo el mismo día**: el usuario repitió "dame un
codigo de tank.io con bots" y el turno salió en **UNA sola llamada**
(antes siempre 2) -- `755 out tok`, por debajo del objetivo con margen
calculado (~765) y del techo duro (900), sin ningún circuit-breaker.
Código completo y coherente: 2 bots (dentro del tope de 3), movimiento
simple por fórmula de dirección normalizada, sin variables
indefinidas. Este es el primer turno de la sesión que resuelve
"código con bots a presupuesto Bajo" de punta a punta sin ninguna red
de seguridad reactiva.

**2026-09-16, mismo día, octavo round -- autorrevisión de los fixes de
los rounds 5 y 6: eran demasiado brutos, apagaban el chequeo de eco de
schema junto con los falsos positivos.** Al revisar el propio código
de los rounds 5/6 (a pedido del usuario de "detectar los bugs que
seguro existen"), se encontró que ambos fixes sacaban los turnos de
codegen del breaker COMPLETO vía un gate (`not _wants_codegen_budget`
antes de siquiera llamar a la función) -- pero eso también apagaba el
chequeo de eco de schema/prompt de sistema (`_SYSTEM_PROMPT_ECHO_
MARKERS_RE`/`_FASTPATH_ECHO_RE`), que SIGUE siendo una señal válida
para cualquier tipo de respuesta, código incluido: un
`{"tool": null // ...}` crudo filtrado al chat nunca es código
legítimo, y los turnos de código en chat (sin `write_file` real) no
están protegidos contra esto por ningún otro lado. Fix: en vez de
gatear la LLAMADA a `_fastpath_response_looks_broken`/`_slowpath_
response_looks_broken`, ahora se les pasa `is_codegen=_wants_codegen_
budget` como parámetro -- adentro de cada función, `is_codegen=True`
salta SOLO los chequeos de forma/longitud pensados para prosa
(repetición degenerativa, techo lleno, y -- recién detectado en esta
misma revisión -- las dos comparaciones de "desproporción pregunta/
respuesta corta", que también son un falso positivo real: "dame un
codigo de tank.io con bots" son ~35 caracteres, así que CUALQUIER
código de más de ~3200 caracteres, es decir cualquier presupuesto por
encima de "Bajo", dispararía "respuesta desproporcionada para una
consulta breve" sin que hubiera nada roto -- este último no se había
disparado todavía en las pruebas de la sesión porque todas fueron a
presupuesto "Bajo"). El eco de schema y la fuga de Harmony se siguen
chequeando SIEMPRE, código o no. Reemplaza los fixes de los rounds 5 y
6 (mismo call-site, criterio más preciso). Verificado con un script
aislado: código legítimo con repetición estructural ya no dispara nada
con `is_codegen=True`, pero un eco de schema real sigue disparando
igual con `is_codegen=True` o `False`. Backup:
`orchestrator.py.bak52`. **Pendiente de confirmar en vivo**: no hay
forma fácil de reproducir a propósito el caso que este round protege
(un eco de schema real es infrecuente) -- este round se apoya en
verificación aislada, no en una reproducción en vivo, a diferencia de
casi todo lo demás de esta sesión.

**2026-09-17, NUEVA sesión de Cowork -- siete parches (`orchestrator57`
a `orchestrator63`) sobre `edit_file` y el presupuesto de salida,
disparados por pedidos directos del usuario de seguir bajando el costo
real por turno, no solo subir techos de seguridad.**

1. **`orchestrator57` -- feedback de error de sintaxis en reintentos de
   `write_file`/`edit_file`.** Antes, si un intento fallaba con
   `[SANDBOX WRITE ERROR]` (sintaxis rota vía `ast.parse`, o `old_str`
   sin match/ambiguo -- mensajes exactos confirmados en
   `edit_file_safely`, `src/tools/tools.py`), el reintento no tenía
   ninguna instrucción específica sobre QUÉ salió mal, más allá del
   propio texto del error ya visible en el tool_result. Fix: se agregó
   un chequeo de `_write_error_in_result` que extiende
   `closing_instruction` en dos ramas -- si es la última pasada
   permitida, se le pide al modelo una explicación honesta del fallo en
   vez de reintentar a ciegas; si no, una instrucción de arreglo mínimo y
   dirigido (con un `read_file` primero si el error es de match, para no
   volver a adivinar `old_str` a ciegas).

2. **`orchestrator58` -- presupuesto dinámico propio para `edit_file`
   (2.75x vs. 1.5x de `write_file`).** `_effective_codegen_num_predict`
   solo tenía un multiplicador (`_dynamic_write_budget_tokens`, 1.5x)
   pensado para reescribir el archivo completo -- pero un parche
   `edit_file` paga por `old_str` Y `new_str` (la región vieja Y la
   nueva, no solo el cambio neto) más el overhead estructural del JSON,
   así que el mismo techo dejaba a `edit_file` sistemáticamente más
   corto de presupuesto que a `write_file` para un cambio equivalente.
   Fix: nueva constante `_EDIT_FILE_BUDGET_MULTIPLIER = 2.75` (sin
   calibrar contra generaciones reales, mismo estado que otras
   constantes de este estilo en el documento -- ver §10), nuevo método
   `_dynamic_edit_budget_tokens(existing_content)` (mismo esqueleto que
   la versión de `write_file`), y un parámetro nuevo
   `for_edit_file: bool = False` en `_effective_codegen_num_predict` que
   elige el multiplicador correcto -- cableado en los dos call-sites que
   importan: el cálculo inicial del techo (`_wants_codegen_budget`) y el
   override de la pasada de reintento del bucle de herramientas, ambos
   gateados por `_suppress_write_file_for_turn` (la señal pre-existente
   que indica que el turno es edit_file-only).

3. **`orchestrator59` -- texto de `[OUTPUT BUDGET]` específico para
   `edit_file` (inicial y en reintentos) + renombre del selector Bajo/
   Medio/Alto/Extra en la UI.** El texto de negociación de alcance
   (§8, cuarto/séptimo round de la sesión anterior) estaba escrito
   pensando en `write_file`/andamiaje de archivo nuevo/tope de
   entidades -- no aplica a un parche `edit_file`, donde el objetivo es
   el opuesto (diff lo más chico posible, no "cuánto entra"). Fix: el
   bloque de la pasada inicial (`elif not _codegen_budget_infeasible:`)
   se separó en dos ramas vía `if _suppress_write_file_for_turn:` -- la
   nueva rama para `edit_file` pide explícitamente el cambio más chico
   posible, usar la lista `edits` si hay varios cambios NO relacionados
   en vez de forzarlos a un solo par, y advierte que repetir código sin
   cambiar sale el doble de caro (se paga como output). Se agregó
   además un bloque nuevo, solo para reintentos
   (`if wants_file_tools and _suppress_write_file_for_turn and not
   is_last_allowed_pass:`), con el mismo criterio. Aparte, y motivado por
   la pregunta directa del usuario de si el presupuesto sigue "ajustado
   al centavo" -- ya no lo está, exactamente, desde que `edit_file` tiene
   su propio multiplicador (punto 2): se renombraron las 8 entradas
   I18N del selector (`cloud_budget_option_1c/2c/4c/8c`, ES y EN,
   `sovnode_qt.py`) sacando la promesa literal "1¢/2¢/4¢/8¢ por turno",
   dejando solo el nombre del nivel (Bajo/Medio/Alto/Extra) y el conteo
   aproximado de tokens -- la etiqueta ya no afirma un costo exacto que
   el propio sistema puede superar a propósito para `edit_file`.

4. **`orchestrator60` -- rescate local para un `edit_file` cortado por
   el techo, sin intentar empalmar JSON parcial.** El mecanismo
   existente para `write_file` cortado (`_continue_truncated_file_write_
   locally`) funciona porque el contenido es texto plano -- empalmar la
   parte que falta es seguro. `edit_file` no tiene ese lujo: su
   `old_str`/`new_str` es una estructura, y un corte a mitad de camino
   dentro de esa estructura no se puede completar por empalme sin
   arriesgar un patch inválido o, peor, uno que aplique pero corrompa el
   archivo en silencio. Fix: nuevo método
   `_rescue_truncated_edit_file_locally(tool_call, *, prompt_text,
   active_model, lang_override, gen_system, log_cb=None)` que DESCARTA
   el intento cortado entero y regenera desde cero en el motor Local
   (`force_local=True`, `MemoryGovernor.codegen_num_predict()` = 6144
   tokens, costo $0). Los dos puntos existentes de detección de "techo
   alcanzado" (antes solo para `write_file`, uno antes del bucle y uno
   adentro) se generalizaron a `in ("write_file", "edit_file")` vía
   nuevas variables `_ceiling_tool_name`/`_ceiling_tool_name_inloop`, con
   una rama nueva `elif _cloud_active and _ceiling_tool_name ==
   "edit_file":` que llama al rescate.

5. **`orchestrator61` -- aviso de "el archivo ya existe" para evitar un
   segundo `write_file` completo en el mismo turno.** Bug real medido en
   vivo: un turno de creación de `agario_basico.py` (motor Cloud, $0.0672
   total) pagó DOS llamadas `write_file` completas -- la primera creó el
   archivo, la segunda reescribió TODO el archivo de nuevo solo para
   agregarle lógica de bots, en vez de usar `edit_file` ahora que el
   archivo ya existía. Se consideró y se DESCARTÓ, antes de comitear
   nada al dispositivo, forzar `_suppress_write_file_for_turn = True` en
   caliente apenas hay un `write_file` exitoso en el bucle (mismo patrón
   que `_should_suppress_write_file_tool`) -- el riesgo real es que
   `edit_file_safely` rechaza de plano un archivo que todavía no existe,
   así que un turno legítimo que crea DOS archivos nuevos distintos se
   habría roto. Se optó por un empujón de puro texto, aditivo, no
   restrictivo: nueva variable de turno `_file_created_this_turn_path`
   (se popula junto a `_last_file_op_confirmation` cada vez que un
   `write_file`/`edit_file` exitoso deja contenido nuevo) y un bloque
   nuevo en `closing_instruction`
   (`if _file_created_this_turn_path and wants_file_tools and not
   is_last_allowed_pass:`) que le dice explícitamente al modelo, vía
   `[FILE STATE]`/`[ESTADO DEL ARCHIVO]`, que ese archivo puntual ya
   existe, que use `edit_file` para tocarlo, y que reserve `write_file`
   para un archivo genuinamente distinto.

6. **`orchestrator62` -- población reactiva de
   `_modify_target_current_content` desde un `read_file` exitoso.**
   Bug real reportado por el usuario ("porque nunca edito el archivo?"),
   diagnosticado por WAL directo: turno "mejora el codigo de agar io en
   el workpsace" (typo). `codegen_budget_plan` mostró `hard_ceiling: 900`
   (el default de chat, no el de edición de archivo) -- la detección de
   intención de escritura (`_has_file_write_intent`/`is_file_write`) dio
   `False` porque el typo "workpsace" no matcheaba
   `_WORKSPACE_KEYWORD_RE`, y la frase coloquial "agar io" tampoco
   matcheaba "agario_basico" (el nombre real, con guion bajo) vía el
   fallback de coincidencia de frase. Con `is_file_write=False`,
   `_modify_target_current_content` quedó vacío y
   `_wants_codegen_budget=False` para TODO el turno -- aun cuando el
   modelo, por su cuenta, navegó bien con `list_dir`→`read_file` dentro
   del bucle. El resultado fue una pasada de cierre famélica (900 tokens,
   sin ningún contexto de archivo ni instrucción de presupuesto) que
   terminó mostrando el volcado crudo del `tool_result` de `read_file`
   como respuesta final -- cero ediciones, $0.0608 gastados. Fix: cuando
   un `read_file` del bucle tiene éxito (no es un aviso interno de
   toolguard, no contiene `[SANDBOX WRITE ERROR]`) y
   `_modify_target_current_content` sigue vacío, se lo popula con el
   contenido real de ese archivo. Es un fix general y a prueba de typos
   -- en vez de depender de adivinar bien la frase del usuario de
   antemano, confía en la propia acción exitosa del modelo dentro del
   turno como evidencia de cuál es el archivo real.

7. **`orchestrator63` -- gate de tamaño redundante eliminado en
   `_current_file_context`.** Bug relacionado, reportado por el usuario
   con una captura sin texto (mismo pedido, esta vez bien escrito:
   "mejora el codigo de agar io en el workspace"): `codegen_budget_plan`
   mostró `hard_ceiling: 5120`, prueba de que `_resolve_modify_target`
   SÍ resolvió bien `mod_target='agario_basico.py'` esta vez -- pero el
   propio tool call del modelo adivinó un nombre de archivo DISTINTO
   (`agar_io.py`, derivado de cómo lo escribió el usuario, no del
   target ya resuelto por el sistema), el `read_file` falló ("el archivo
   no existe"), y el modelo, con honestidad, le pidió al usuario que
   aclare el nombre en vez de alucinar. Causa raíz: los dos call-sites
   de `_current_file_context` (la función que le comunica al modelo el
   nombre de archivo YA resuelto) tenían un gate
   `if _cur and len(_cur) <= 4500:` -- y el archivo real superaba ese
   tamaño, así que la función nunca se llamaba, ni siquiera para mandar
   el nombre del archivo (el dato barato). El gate era puramente
   redundante: `_current_file_context` YA trunca sola su propio preview
   de contenido a 4500 caracteres internamente, sin importar qué le pase
   el caller. Fix: los dos call-sites cambiaron de
   `if _cur and len(_cur) <= 4500:` a `if _cur:`.

Los siete parches se comitearon al dispositivo vía el flujo
stage/edit/commit del puente remote-devices (`device_bash` no estuvo
disponible en ningún momento de esta sesión -- ver §10 sobre el patrón
de `device_commit_files` fallando en silencio la primera vez, repetido
en los siete). Ninguno tocó `_call_claude_api_raw`/
`_stream_claude_api_raw` (la única superficie que factura contra la API
real) -- el usuario preguntó explícitamente por la palanca de mayor
impacto restante (cachear incrementalmente el historial de herramientas
entre pasadas del bucle, en vez de reconstruir un único mensaje de
usuario plano desde cero en cada pasada) y la declinó por ahora
("Por ahora no, ya alcanza") -- queda sin implementar, ver §10.

**2026-09-18 -- diagnóstico en vivo: visor de WAL standalone +
"Terminal avanzada" en la app (`patch_orchestrator64`, `patch_qt64`).**
Pedido explícito del usuario: "un botón... que muestre todos los datos
en tiempo real para que sepas exactamente qué falló" -- en la práctica,
automatizar el propio flujo de diagnóstico de esta sesión (leer el WAL a
mano, correlacionar `turn_id`, buscar qué red de seguridad disparó) en
vez de repetirlo manualmente cada vez que algo sale mal.

1. **`sovnode_wal_monitor.py`** (nuevo, raíz del proyecto, standalone --
   mismo espíritu que `sovnode_log_viewer.py`: `rich`, de solo lectura,
   se corre en una terminal aparte, no importa ningún módulo del
   proyecto). Primera versión pedida por el usuario; DESCARTADA como
   entrega final a favor del punto 2 de abajo, pero se dejó comiteada
   porque sigue siendo útil por su cuenta (agrupa por turno TODO lo que
   ya vivía en el WAL, no solo lo que llega a la UI -- ver limitación
   del punto 2). Tail-f robusto a truncado/reinicio del archivo,
   `--history N` para precargar los últimos N turnos completos antes de
   engancharse en vivo, `--only-problems` para dejarlo corriendo de
   fondo, `--turn <id>` para seguir un turno puntual, `--raw` para el
   JSON crudo. 21 fases curadas a mano desde cada `_wal_phase(...)` real
   de `orchestrator.py` como "red de seguridad disparó" (circuit-
   breakers, rescates, bloqueos de falso-éxito, etc.), resaltadas en
   rojo con un resumen al cerrar cada turno. Bug real encontrado y
   corregido ANTES de comitear (verificado con un WAL sintético, tres
   turnos, tres modos): `_follow()` arrancaba siempre desde el byte 0 del
   archivo, así que el modo "en vivo" repetía TODO lo que `--history` ya
   había mostrado -- fix: `_follow()` ahora arranca en el tamaño actual
   del archivo al momento de conectarse, no en 0. Limitación documentada
   en el propio script: el costo real en USD no viaja por el WAL hoy
   (`_account_cloud_usage` solo lo anuncia por `log_cb`, que en la app
   termina en `_terminal_log`, nunca en un archivo) -- el script muestra
   el techo de presupuesto en tokens como mejor proxy sin tocar
   `_call_claude_api_raw`.

2. **"Terminal avanzada"** (`sovnode_qt.py`, checkbox nuevo en el header
   del panel de consola, junto a "Limpiar"; QSettings
   `ui/advanced_terminal_enabled`, default apagado). El usuario pidió
   explícitamente que viviera DENTRO de la app ("un botón en la
   terminal de sovnode"), no como script aparte -- corrección sobre el
   punto 1. A diferencia del script standalone, esto NO lee el WAL: se
   engancha al MISMO stream `StreamTurnWorker.log_message` (un solo
   `pyqtSignal(str)` por el que ya pasan TODOS los `PipelineEvent` --
   `EventType.LOG`, `ROUTE_DECIDED`, `TOOL_CALL_START/RESULT`,
   `VERIFICATION`, `ERROR`) que la UI consumía antes en un único color
   plano ("system") vía `_on_worker_log_message`. Con el toggle
   activado, cada mensaje se reclasifica en `_advanced_terminal_log`
   (nuevo) por PREFIJO/palabra clave -- `[ROUTER]`/`[TOOL] Inicio:`/
   `[TOOL] Resultado:`/`[VERIFICATION]`/`[BUDGET]`·`[PRESUPUESTO]`
   (nuevo, ver más abajo) con su propio color, y "circuit-breaker" /
   "blindaje de archivos" / "guard:" (las 3 palabras clave que
   `orchestrator.py` deja fijas a propósito en ambos idiomas, ver el
   comentario junto a `_ADV_TERMINAL_PROBLEM_KEYWORDS`) resaltados en
   rojo como problema -- sin agregar NINGÚN dato nuevo al pipeline, es
   puramente una rama de presentación sobre mensajes que ya existían.
   `_send_message` marca el inicio de turno (`_adv_terminal_turn_header`,
   con el prompt) y `_on_turn_completed` el cierre
   (`_adv_terminal_turn_footer`) -- no hace falta un `turn_id` en la UI
   porque el "turno actual" ya está acotado por el ciclo de vida del
   propio `StreamTurnWorker` (uno por turno). El cierre reutiliza el
   cálculo de costo real que YA existía para el badge del header
   (`cloud_usage_totals["cost_usd"] - self._turn_cost_snapshot_usd`) --
   a diferencia del script standalone del punto 1, la Terminal avanzada
   SÍ puede mostrar el costo real en $ de cada turno, porque corre
   dentro del mismo proceso que factura, en vez de leer un archivo desde
   afuera. Estilo QSS propio agregado al selector combinado ya
   existente de `chk_workspace_tools`/`chk_run_cmd` (mismo criterio: los
   tres toggles del panel se ven siempre idénticos entre sí).

3. **`codegen_budget_plan` ahora también llega a la UI/consola**, no
   solo al WAL (`orchestrator.py`, mismo call-site del punto 7 de la
   entrada anterior de este §8). Antes de este cambio la app no mostraba
   NINGÚN dato de presupuesto en ningún lado -- es el dato que más veces
   explicó "por qué salió así" esta sesión (patches 58/59/62/63). Un
   solo `yield PipelineEvent(EventType.LOG, ...)` adicional junto al
   `_wal_phase(...)` ya existente, mismo patrón que cualquier otro punto
   del generador -- NO toca `_call_claude_api_raw` ni ninguna función
   que factura contra la API real. Aparece como `[BUDGET]`/
   `[PRESUPUESTO]` en la consola (clásica o avanzada) y en
   `sovnode_wal_monitor.py`.

4. **El rescate local desde cero de `patch_orchestrator65` ahora respeta
   el selector "Presupuesto de Sonnet" (`patch_orchestrator66`)**.
   Pedido explícito del usuario inmediatamente después de ver patch65
   comiteado: "me gustaría que el código siempre se ajustase al esfuerzo
   elegido en sovnode". Diagnóstico: la rama `elif user_input:` agregada
   en patch65 (`_continue_truncated_file_write_locally`) pedía la
   reescritura completa con `MemoryGovernor.codegen_num_predict()` --
   6144 tokens fijos, SIEMPRE, sin mirar el selector "Bajo/Medio/Alto/
   Extra" (1/2/4/8 centavos -> `_cloud_output_ceiling_tokens`) que sí
   gobierna la Pasada 1 original vía `_effective_codegen_num_predict` +
   el bloque [OUTPUT BUDGET]/[PRESUPUESTO DE SALIDA] (negociador de
   alcance: piso de andamiaje, objetivo con margen, tope de entidades --
   ver §8 de una entrada anterior y los BLINDAJE de
   `_suggest_codegen_entity_cap`/`_codegen_soft_target`). Resultado
   medible: si el usuario elegía "Bajo" esperando un juego mínimo de 1
   bot, el rescate gratis en Local podía terminar escribiendo algo mucho
   más grande que lo pedido -- no por costo (Local es gratis), sino por
   INCONSISTENCIA de alcance entre el intento pagado y el rescate que lo
   completa.

   Fix: la rama de patch65 ahora calcula su propio techo con
   `self._effective_codegen_num_predict()` (sin `existing_file_content`,
   porque acá no hay archivo previo) y reusa el mismo negociador de
   alcance que `run_turn` (`_estimate_min_viable_codegen_tokens`,
   `_codegen_soft_target`, `_suggest_codegen_entity_cap`) para inyectar
   el mismo tipo de bloque [OUTPUT BUDGET]/[PRESUPUESTO DE SALIDA] --
   mismo estilo de texto que el de "archivo nuevo" en `run_turn`, con su
   propio desglose piso/objetivo/tope-de-entidades calculado sobre el
   techo real de este rescate. Con Cloud apagado, `_effective_codegen_
   num_predict()` sigue devolviendo el mismo 6144 fijo de siempre --
   sesiones 100% Local no cambian de comportamiento (el selector de
   presupuesto no existe/no aplica sin Cloud). No toca
   `_call_claude_api_raw`/`_stream_claude_api_raw` (sigue corriendo
   100% en Local, `force_local=True`, costo $0). Verificado con
   `ast.parse` antes de comitear; backup del archivo pre-fix guardado
   como `orchestrator.py.bak55`.

   Nota de alcance: la otra rama de rescate local de esta misma función
   (`if _diag_existing and user_input:`, "actualización dirigida" sobre
   un archivo que YA existe, `perf_label="LocalTargetedUpdate"`,
   pre-existente a patch65) sigue usando el 6144 fijo A PROPÓSITO, sin
   tocar -- ese rescate tiene que reescribir el archivo existente
   COMPLETO más el cambio pedido, y el propio selector de presupuesto ya
   está diseñado para nunca poder achicar ese piso (ver el docstring de
   `_effective_codegen_num_predict`: "el techo real nunca baja de lo que
   ESE archivo necesita para reescribirse completo, sin importar el
   selector") -- capearlo al valor chico de "Bajo" rompería la
   actualización de cualquier archivo más grande que ~900 tokens, un
   caso mucho más común que el de escritura desde cero. La
   inconsistencia que pedía arreglar el usuario era específicamente la
   de la rama de archivo NUEVO (patch65), que sí comparte exactamente el
   mismo caso de uso que la Pasada 1 original.

5. **patch66 rompía exactamente el caso que patch65 arreglaba --
   corregido por `patch_orchestrator67`, más un bug de categorización
   real encontrado en el camino.** El usuario probó patch66 en vivo con
   el MISMO pedido investigado para patch65 ("tank io con bots",
   Presupuesto "Bajo") y reportó "sigue pasando lo mismo". Diagnóstico
   completo por WAL + `sovnode_debug.log` (turn_id `5161acd8-...`):

   - **Bug de categorización, real, independiente del bug de arriba**:
     `codegen_budget_plan` mostró `category=default` (piso=120 tokens)
     para un pedido que claramente es un juego -- `_MIN_VIABLE_FLOOR_
     PATTERNS` no reconocía "tank io"/"tank.io" (el patrón existente
     `agar\.?io` no tiene espacio opcional, así que tampoco matchea
     "agar io" escrito con espacio). Con el piso equivocado (120 en vez
     de los 450 reales de la categoría "juego"), TODO el negociador de
     alcance calculaba mal para este pedido, tanto en la Pasada 1
     original como en el rescate. Fix acotado: se agregó `tank[.\s]?io`
     a la misma alternancia regex (mismo estilo que `agar\.?io`),
     aceptando espacio, punto, o nada entre "tank" e "io".

   - **Bug real de patch66**: al pasar de `MemoryGovernor.codegen_num_
     predict()` (6144 fijo) a `_effective_codegen_num_predict()` (900,
     el mismo techo REAL de Cloud con "Bajo") como `num_predict_
     override` del propio rescate Local, patch66 no solo ajustó el
     ALCANCE que se le pide al modelo -- también le quitó el margen
     TÉCNICO que patch65 necesitaba para poder terminar de escribir un
     archivo válido. `[FileRescueDiag]` mostraba las 2 reescrituras como
     "EXITOSA" (`is_stub=False`, 552 y 673 caracteres) -- pero el WAL
     registra `tool_result chars=307`/`chars=270` para esas mismas
     pasadas, mucho más chico que el contenido rescatado y consistente
     con el formato de `[SANDBOX WRITE ERROR]` de `write_file_safely`
     (fallo de `ast.parse`), no con el mensaje corto de éxito -- es
     decir, el archivo rescatado tampoco llegó a guardarse: 552/673
     caracteres (~130-190 tokens reales, con `entity_cap` ya reducido
     por el piso mal calculado) es estructuralmente muy poco para un
     juego con pygame + clase de tanque + IA de bots sintácticamente
     completo, y el motor Local terminaba cerrando mal el archivo dentro
     de un margen tan chico. La pasada 3 nunca llegó a intentar
     `write_file` -- el circuit-breaker `tool_loop_aborted` (`reason:
     repeated_without_progress`) cortó el turno antes, con el mensaje
     genérico "Invoqué write_file 3 veces sin avanzar" que no refleja
     que sí hubo intentos de contenido real, solo que ninguno persistió.

   Fix, sin revertir patch66: separar "objetivo de alcance" (lo que se
   le PIDE al modelo que apunte a escribir, sigue atado 1:1 al selector
   del usuario vía `_fresh_scope_ceiling = self._effective_codegen_
   num_predict()`, sin cambios en `_fresh_soft_target`/`_fresh_entity_
   cap`) de "techo técnico real" (lo que de verdad se le pasa a
   `_call_llm` como `num_predict_override`, ahora `_fresh_hard_ceiling =
   max(_fresh_scope_ceiling, MemoryGovernor.codegen_num_predict())` --
   nunca menor que el fijo de siempre para Local). El texto del bloque
   [OUTPUT BUDGET]/[PRESUPUESTO DE SALIDA] se reescribió para reflejar
   la distinción: "APUNTÁ a un alcance de ~N tokens (lo que pediste con
   tu selector) -- técnicamente tenés margen hasta ~M tokens antes de
   cortarte, pero acercarte a M significa que agregaste más de lo
   pedido". El selector del usuario sigue gobernando fielmente CUÁNTO
   programa se le pide al modelo que escriba (mismo pedido explícito de
   la conversación anterior: "que el código se ajuste al esfuerzo
   elegido"); lo que cambió es que el margen TÉCNICO para terminarlo sin
   romper sintaxis vuelve a ser generoso, gratis, como ya lo era antes
   de patch66 -- exactamente el mismo criterio que `_effective_codegen_
   num_predict` ya aplica en el resto de `run_turn` para contenido
   existente ("el techo real nunca baja de lo que el contenido necesita
   para ser válido, el selector decide el alcance, no la posibilidad
   técnica de terminar sin cortarse"). No toca `_call_claude_api_raw`/
   `_stream_claude_api_raw`. Verificado con `ast.parse` antes de
   comitear; backup del archivo pre-fix guardado como
   `orchestrator.py.bak56`.

6. **Señal genérica de complejidad para el piso de presupuesto, en vez de
   perseguir nombres de género uno por uno (`patch_orchestrator67`
   arregló "tank io" puntualmente; `patch_orchestrator68` ataca el
   problema de fondo)**. Pregunta explícita del usuario tras ver el fix
   anterior: "¿cómo podemos abarcar todas las posibilidades?". Respuesta
   honesta: una lista de nombres de género NUNCA puede cubrir todas las
   posibilidades -- este mismo proyecto ya tiene el problema sin resolver
   en otros dos lugares (`router._CODE_COMPLEX_PATTERN` y
   `Orchestrator._RUNNABLE_PROGRAM_NOUN_RE`, cada uno con su propia lista
   de nombres de juego mantenida a mano, con comentarios propios
   admitiendo que hay que mantenerlas sincronizadas).

   En vez de agregar cada género posible, se agregó un CUARTO patrón a
   `_MIN_VIABLE_FLOOR_PATTERNS` que no busca nombres de franquicia sino
   palabras que indican complejidad real sin importar el género: "bots",
   "enemigos"/"enemies", "multijugador"/"multiplayer", "inteligencia
   artificial", "colisiones"/"collision", "física"/"physics",
   "animación"/"animation", "npc", "waypoints", "pathfinding". Mismo
   criterio que ya usa `_RUNNABLE_PROGRAM_NOUN_RE` (que reconoce "bot" de
   forma genérica, para otro propósito) aplicado acá al cálculo de piso.
   Piso intermedio (380, entre "servidor" 300 y "gui"/"juego" 400/450) a
   propósito -- es una señal más débil que un género reconocido
   explícitamente, así que no se le da el piso más alto de "juego", pero
   tampoco cae al "default" de 120 (ya medido como demasiado optimista
   para cualquier cosa con lógica de entidades propia). Si ADEMÁS
   matchea un género específico, gana ese (la selección ya toma el
   máximo entre todos los patrones que matcheen, sin cambios en esa
   lógica). Nueva etiqueta en `_MIN_VIABLE_FLOOR_CATEGORY_LABELS`:
   `380: "complejidad_generica"`, para poder distinguirla de "gui" en la
   telemetría del WAL (`codegen_budget_plan`).

   Esto no "resuelve" el problema de cobertura -- nada basado en
   palabras clave lo resuelve del todo -- pero cubre géneros nuevos sin
   depender de que alguien se acuerde de agregarlos a mano. Además, con
   `patch_orchestrator67` ya en pie, aunque esta categorización siga
   siendo imperfecta el rescate gratis en Local no se rompe (su techo
   técnico real ya no depende de la categoría) -- el peor caso de una
   categorización que no reconoce el género es un aviso de alcance menos
   preciso en el prompt, no un archivo que nunca se guarda. Verificado
   con `ast.parse`; backup pre-fix guardado como `orchestrator.py.bak57`.

7. **Negociador de alcance para `edit_file` (mejorar/arreglar/modificar/
   reemplazar): cuántas funciones o clases nuevas entran en el
   presupuesto (`patch_orchestrator69`, "versión liviana")**. El usuario
   propuso una "ecuación" que calculara de antemano el costo exacto en
   tokens de cada pieza de código a agregar, y que para pedidos de
   modificar hiciera el cambio "en secciones" (agregar una clase, después
   otra función, cada una con su gasto). Se evaluaron dos versiones con
   el usuario -- una completa (plan de secciones ejecutado en VARIAS
   llamadas a Cloud, con seguimiento de presupuesto entre pasadas) y una
   liviana (mismo cálculo estadístico que ya usa `_codegen_entity_cap`
   para bots/enemigos, aplicado a funciones/clases, en UNA sola llamada)
   -- el usuario eligió la liviana explícitamente por costo/riesgo: la
   versión completa multiplica las llamadas a Cloud por turno, lo que
   puede terminar gastando más centavos que los que elige el selector
   "Presupuesto de Sonnet" (rompiendo la garantía de "nunca más de N
   centavos" que este mismo proyecto pasó dos días protegiendo).

   Nota honesta, ya discutida con el usuario: una ecuación EXACTA no es
   posible -- el costo real en tokens de "una función nueva" depende de
   cómo la termine escribiendo el modelo (nombres, comentarios,
   complejidad), dato que no existe hasta que se genera. Lo que sí es
   viable es una estimación estadística, la misma que ya usa
   `_suggest_codegen_entity_cap` (presupuesto extra ÷
   `_CODEGEN_EXTRA_TOKENS_PER_ENTITY`=150 tok/pieza).

   Cambio real: el bloque [OUTPUT BUDGET]/[PRESUPUESTO DE SALIDA] de
   `edit_file` (rama `_suppress_write_file_for_turn`, `run_turn`) antes
   solo hablaba del TAMAÑO del diff (old_str/new_str chico) -- no decía
   nada de CUÁNTAS piezas nuevas entran cuando el pedido es "agregá X, Y,
   Z". Se agregó una oración final (bilingüe, ambas ramas del ternario
   `effective_lang`) que reusa `_codegen_entity_cap`/
   `_codegen_extra_budget` (ya calculados antes en el mismo bloque, sin
   variables nuevas) para decirle al modelo: si el presupuesto extra es
   > 0, "con este presupuesto entran como máximo ~N piezas chicas nuevas
   -- si el pedido pide más, agregá las más importantes ahora (una por
   entrada en 'edits') y dejá el resto para un próximo mensaje"; si el
   presupuesto extra es 0 (ceiling apenas por encima del piso de
   andamiaje), en cambio le dice que cualquier función/clase nueva
   arriesga cortarse, y que la reduzca a 1-2 líneas o la saltee. No toca
   `_call_claude_api_raw`. Verificado con `ast.parse` y con una prueba de
   consistencia aparte (concatenación string + ternario, ambas ramas de
   `_codegen_extra_budget`); backup pre-fix guardado como
   `orchestrator.py.bak58`.

8. **Techo chico dedicado para la pasada de "¿sigo o ya está?" del bucle
   de herramientas, en vez de heredar el mismo techo grande que la
   escritura real (`patch_orchestrator70`)**. El usuario preguntó por
   qué un turno de "tank io" terminaba pagando 3 llamadas a Cloud
   (§ap.7 de este changelog) y, al indagar si hacía falta gastar tantos
   tokens de SALIDA en cada pasada, llegó él mismo a la razón de fondo:
   "no tiene sentido que gaste tokens de salida en acciones donde no
   toca el código". Diagnóstico: Anthropic cobra por los tokens que el
   modelo REALMENTE genera, no por el techo reservado (`num_predict`
   solo pone un límite) -- así que el costo real de las pasadas 2 y 3 no
   viene de que el techo sea grande, sino de que el modelo, TENIENDO ese
   techo grande disponible, lo usaba para reintentar reescribir el
   archivo entero de nuevo en vez de aceptar el que ya se había escrito
   con éxito en una pasada anterior del mismo turno. `_effective_codegen_
   num_predict()` nunca baja del techo elegido por el usuario ("Bajo"=
   900 tok) por diseño de una sesión anterior -- ni siquiera la pasada de
   SEGUIMIENTO (`followup`, la que decide "¿qué hago ahora?" DESPUÉS de
   que `execute_tool_from_call` ya corrió el tool_call anterior) se
   salvaba de heredar ese mismo techo grande, pese a que su trabajo
   esperado -- una vez que `_file_created_this_turn_path` ya está fijado
   (ver patch61) -- es mucho más chico: confirmar que ya está, o pedir
   como mucho un ajuste puntual con `edit_file`.

   Fix: nueva constante `FOLLOWUP_DECISION_NUM_PREDICT = 250` (fija,
   NO calculada a partir del selector) -- única excepción explícita
   documentada a la regla "el techo real nunca baja de lo que eligió el
   usuario", justificada porque esta pasada puntual no escribe código
   desde cero. Se usa como `num_predict_override` de la llamada de
   seguimiento (`_call_llm_raw(followup, ...)`, dentro del `while
   tool_call` de `run_turn`) exactamente cuando `_file_created_this_
   turn_path` está fijado Y `wants_file_tools` -- en cualquier otro caso
   (todavía no se escribió nada este turno, o la herramienta de archivo
   ni siquiera está disponible) el comportamiento queda IGUAL que antes
   (`_effective_codegen_num_predict(...)` o `None`). Con ese techo chico,
   si el modelo igual intenta reescribir todo el archivo, se corta rápido
   y barato en vez de agotar otra vez el presupuesto completo -- el
   rescate gratis en Local (patch67) sigue disponible como red de
   seguridad si hiciera falta más para terminar algo real. No toca
   `_call_claude_api_raw`/`_stream_claude_api_raw`. Verificado con
   `ast.parse` y con una simulación aparte de las 4 combinaciones de la
   condición (con/sin archivo creado × con/sin herramientas de archivo);
   backup pre-fix guardado como `orchestrator.py.bak59`.

   Nota de alcance, aclarada con el usuario en la misma conversación: la
   regla de "recortar alcance antes que romper funcionalidad" para
   ARCHIVO NUEVO (patch65-68, "que sea funcionable es la única regla")
   ya estaba implementada desde antes -- el texto "si dudás, cortá MÁS,
   no menos: una versión chica que corre entera de principio a fin
   siempre le gana a una más grande que se corta a mitad de línea" ya
   vive en el bloque [OUTPUT BUDGET] de `write_file` nuevo. No hizo falta
   ningún cambio de código para esa parte, solo confirmarlo.

9. **Garantía de piso para funciones pedidas explícitamente por nombre
   (bots, enemigos, colisiones, etc.) en vez de solo un techo máximo
   sobre ellas (`patch_orchestrator71`)**. Evidencia concreta que motivó
   el fix: se probó en vivo un turno "crea un juego de tank io con bot en
   el workspace" (rescate LocalFreshWrite de patch67-70 funcionando,
   $0.0308 total, pasada de seguimiento en 250 tok como diseñaba
   patch70) y el archivo entregado (`tank_io.py`, 49 líneas, sintaxis
   válida, corre) tenía CERO bots -- una clase `Tank` que solo sube y
   baja con las flechas, sin ninguna entidad de IA. El usuario lo resumió
   así: "necesito que priorice lo que se le pide... si yo le pido un
   tank.io con bots, que al menos el código pueda tener al menos un bot...
   tiene que ser obediente al prompt y que el código funcione... si le
   pido muchas cosas, tendría que comprimir todo eso al techo ese que ya
   tiene". Diagnóstico: el texto de negociación de alcance (patch66-69)
   ya le decía al modelo un TECHO ("usá A LO SUMO N bots/enemigos") pero
   nunca un PISO -- nada impedía que, bajo presión de presupuesto, el
   modelo cortara una característica pedida EXPLÍCITAMENTE por nombre
   hasta cero, mientras el archivo siguiera siendo sintácticamente válido
   y "funcional" en el sentido más literal (corre, no tira excepción).
   "Funcional" y "obediente al prompt" no son lo mismo, y hasta este
   patch solo se garantizaba lo primero.

   Fix, en dos partes:

   a) Deduplicación previa: la regex de "señal de complejidad genérica"
   (bots, enemigos, colisiones, físicas, animación, NPCs, pathfinding,
   etc.), que ya existía inline dentro de `_MIN_VIABLE_FLOOR_PATTERNS`
   desde patch68, se extrajo a una constante de clase nombrada
   `_GENERIC_COMPLEXITY_SIGNAL_RE`, con `_MIN_VIABLE_FLOOR_PATTERNS`
   ahora referenciándola en vez de repetirla. Esto es la MISMA regex, sin
   cambios de comportamiento -- el único propósito es tener una sola
   fuente de verdad para reusarla en el punto (b), en vez de sumar una
   cuarta lista de palabras clave hand-maintained al problema ya
   documentado (§ap., "tres listas separadas de géneros/nombres de
   programa").

   b) Nuevo classmethod `_explicit_feature_keywords(cls, user_input)`:
   corre `_GENERIC_COMPLEXITY_SIGNAL_RE.findall()` sobre el pedido del
   usuario en minúsculas y devuelve la lista de coincidencias únicas, en
   orden de aparición (ej. `"crea un tank io con bot en el workspace"` →
   `['bot']`). Se llama en DOS puntos, calculando `_codegen_explicit_
   features`/`_fresh_explicit_features` una sola vez cada uno y
   reusándolos:
     - En `run_turn`, bloque [OUTPUT BUDGET]/[PRESUPUESTO DE SALIDA] de
       `write_file` para archivo NUEVO (rama `else`, no la de
       `_suppress_write_file_for_turn` -- esa ya tenía su propio fix en
       patch69 con un problema distinto, tamaño de diff en vez de
       features).
     - En `_continue_truncated_file_write_locally`, rama `elif
       user_input:` (el rescate LocalFreshWrite) -- el punto EXACTO donde
       se midió el bug real de `tank_io.py`.
   En ambos, si `_codegen_explicit_features`/`_fresh_explicit_features`
   no está vacío, se agrega una oración final (bilingüe) al prompt:
   "el usuario pidió explícitamente, por su nombre: {lista}. Aunque
   tengas que recortar todo lo demás para entrar en este presupuesto,
   dejá al menos UNA instancia mínima y lo más simple posible de cada una
   de estas -- comprimí su complejidad, nunca su existencia. Recortá
   primero lo que el usuario NO pidió explícitamente antes de sacar algo
   que sí pidió por su nombre." Si la lista está vacía (pedido sin
   ninguna palabra clave de esta categoría), la oración se omite del todo
   -- no se le agrega ruido al prompt para pedidos que no la necesitan.

   Esto no reemplaza el techo de patch68 (`_codegen_entity_cap`/
   `_fresh_entity_cap`, "usá A LO SUMO N") -- ambas instrucciones
   conviven en el mismo prompt: el techo evita que el modelo se pase de
   presupuesto agregando demasiadas entidades: el piso evita que
   directamente las omita. Juntas expresan la regla que pidió el
   usuario: comprimir la complejidad de TODO lo pedido para que entre en
   el techo, en vez de mantener algunas cosas a full tamaño y descartar
   otras enteras. No toca `_call_claude_api_raw`/
   `_stream_claude_api_raw` ni el cálculo de los techos existentes
   (`_fresh_scope_ceiling`, `_fresh_hard_ceiling`, `_codegen_soft_target`,
   etc.) -- es puramente una instrucción de PRIORIZACIÓN agregada al
   texto del prompt, cero cambio en los números de presupuesto en sí.

   Verificado: `_explicit_feature_keywords` probado standalone con casos
   de una sola feature, varias features, y ninguna (lista vacía → rama
   condicional entera se omite, confirmado con `ast.parse` limpio);
   censo de grep post-cambio confirmó exactamente 1 definición + 1 uso
   interno de `_GENERIC_COMPLEXITY_SIGNAL_RE`, 1 definición + 2 sitios de
   llamada de `_explicit_feature_keywords` (uno en `run_turn`, uno en el
   rescate Local), y ninguna regex duplicada huérfana. `ast.parse` sobre
   el archivo completo (979869 bytes) limpio. Backup pre-fix guardado
   como `orchestrator.py.bak60`.

10. **`closing_instruction` del bucle de herramientas le daba al modelo
    una salida ambigua para cerrar un pedido de EDICIÓN de archivo sin
    llamar a `edit_file` -- pegar el código sin cambios como "prosa"
    quedaba, sin querer, aceptado como respuesta válida
    (`patch_orchestrator72`)**. Evidencia MEDIDA vía `sovnode.wal` +
    `sovnode_debug.log` (turno real, turn_id
    `b4894dfd-8295-420c-801e-2d6f9b6e13ad`, "mejora el codigo de tank io
    en el workspace", `tank_io.py` ya existente, Presupuesto de Sonnet
    "Bajo"): pasada 1 no emitió ninguna llamada (el blindaje de archivos
    sintetizó `read_file` como de costumbre, `phase=file_op_salvage`);
    pasada 2 (`ToolCall-P1`, 1001 out tok, $0.0131, total turno $0.04)
    recibió el archivo completo recién leído en el historial y, en vez
    de llamar a `edit_file`, "respondió" pegando el archivo ENTERO sin
    ningún cambio como si fuera su explicación en lenguaje natural --
    `[EmptyResponseDiag]` capturó `raw_tail` terminando en el mismo
    código fuente que ya se había leído. Sin ningún `tool_call` de por
    medio, ninguno de los rescates de techo existentes pudo intervenir
    (todos, desde `patch_orchestrator60` hasta `70`, están gateados a
    "hubo un `tool_call` de `write_file`/`edit_file`, aunque truncado" --
    acá no hubo NINGÚN `tool_call`, ni truncado). El turno terminó
    mostrando el volcado del propio `read_file` como respuesta final,
    sin haber tocado el archivo ni una vez. El usuario lo resumió así:
    "esto esta empezando a ser frustrante, porque nunca lo edito y
    porque se gasto 1000 tokens de salida en nada".

    Causa raíz, en el propio texto del prompt: el `closing_instruction`
    base (compartido por CUALQUIER pasada de seguimiento del bucle,
    scueta y genérica) le da al modelo dos ramas: "si con este resultado
    ya podés responder al usuario, redactá la explicación final en
    lenguaje natural" o "si todavía necesitás otra herramienta, generá
    ÚNICAMENTE el JSON de esa llamada". Nada en esa rama de prosa aclara
    que, para un pedido de EDICIÓN de archivo, "responder" significa
    haber hecho el cambio -- pegar el código (leído recién, "mejorado" o
    no) en la propia explicación es, en la superficie, indistinguible de
    "ya puedo responder" para el modelo, así que tomó esa rama en vez de
    la de la herramienta.

    Fix: nuevo bloque condicional agregado a `closing_instruction`
    (bilingüe, mismo patrón que los bloques de patch57/58/61 ya
    existentes en el mismo lugar), que se activa solo cuando las TRES
    condiciones se cumplen a la vez: `wants_file_tools` (Cloud activo),
    `is_file_write` (el turno tiene intención de escritura/edición de
    archivo, ya calculado arriba en `run_turn`), `not
    is_last_allowed_pass` (todavía queda una pasada de herramienta
    disponible -- en la ÚLTIMA pasada el `closing_instruction` base ya
    PROHÍBE JSON, así que pedir una llamada real sería contradictorio),
    y `not _last_file_op_confirmation` (variable ya existente desde
    antes de este patch, que trackea si YA hubo un `write_file`/
    `edit_file` exitoso en cualquier pasada anterior de este mismo turno
    -- si ya se aplicó el cambio real, no hace falta este recordatorio
    de nuevo). El texto agregado dice sin ambigüedad: pegar/citar/
    repetir el código en la explicación NO cuenta como respuesta válida
    acá; hace falta una llamada real a `edit_file` (o `write_file` solo
    si de verdad hace falta un archivo nuevo) con el cambio aplicado; si
    el pedido es amplio o no especifica qué mejorar, el modelo debe
    elegir UNA mejora concreta y mínima por su cuenta y aplicarla, en
    vez de devolver el archivo sin tocar -- esto último conecta a
    propósito con la filosofía ya establecida en `patch_orchestrator71`
    (comprimir el alcance, nunca abandonarlo).

    No toca ninguno de los rescates de techo existentes (patch60/67-70)
    ni el resto de `closing_instruction` (las instrucciones de
    JSON-only/no-JSON, el bloque de error de `[SANDBOX WRITE ERROR]`,
    el `[OUTPUT BUDGET]`/diff chico de patch59, ni el `[FILE STATE]` de
    patch61) -- solo cierra el hueco puntual de "prosa que actúa como si
    fuera la respuesta" para un pedido de archivo todavía sin resolver.
    Verificado con `ast.parse` sobre el archivo completo (985078 bytes)
    y censo de grep confirmando 1 sola inserción del bloque, correctamente
    enganchado a `_last_file_op_confirmation` (variable pre-existente,
    sin cambios en su propia lógica de asignación). Backup pre-fix
    guardado como `orchestrator.py.bak61`.

    Nota de alcance (ya resuelta por `patch_orchestrator73`, ver el ítem
    siguiente): este fix atacaba el síntoma raíz (el texto del prompt
    que hacía posible la ambigüedad) pero no agregaba ningún rescate
    estructural para el caso en que esta MISMA instrucción, pese a
    todo, no alcanzara y el modelo volviera a responder sin `tool_call`.

11. **Red de seguridad estructural DENTRO del bucle de herramientas para
    "el modelo no emitió ninguna llamada", agregada junto a
    `patch_orchestrator72` en vez de confiar solo en la instrucción de
    prompt (`patch_orchestrator73`)**. Pedido explícito del usuario,
    inmediatamente después de `patch_orchestrator72`: "si se le pide un
    prompt similar diferente, va a seguir cometiendo el mismo error,
    ¿cierto?" -- la respuesta honesta fue que sí, parcialmente: la
    instrucción nueva de patch72 generaliza a CUALQUIER pedido
    clasificado como `is_file_write` (no está atada a "tank io" ni a
    "mejora el código" literalmente), pero sigue siendo una instrucción
    de prompt, no una garantía dura -- ningún modelo de lenguaje obedece
    instrucciones al 100%, y si las vuelve a ignorar el resultado sería
    idéntico al bug original.

    El hueco concreto: ANTES del bucle de herramientas ya existía
    `_salvage_file_operation` (desde `patch_orchestrator52` y anteriores)
    como red de seguridad ESTRUCTURAL -- si la pasada INICIAL no emite
    ningún `tool_call`, esta función sintetiza uno real (leyendo el
    archivo, o capturando código pegado en prosa como `write_file`) sin
    depender de que el modelo "coopere" mejor la próxima vez. Pero
    DENTRO del bucle -- exactamente donde se midió el bug real de
    `tank_io.py` (la pasada de seguimiento tras el `read_file` inicial)
    -- no existía ningún equivalente: si `next_tool_call` quedaba en
    `None` tras una pasada de seguimiento, el turno terminaba sin más,
    mostrando lo que hubiera en `explanation_or_next` (en el caso
    medido, el archivo entero repetido) como respuesta final.

    Fix: se reutiliza la MISMA `_salvage_file_operation` también dentro
    del bucle (`run_turn`, justo después de la cadena de rescates de
    techo de `patch_orchestrator60`/49, antes del blindaje de 2026-09-15
    que recorta el JSON no ejecutado en la última pasada). Se activa
    cuando: `next_tool_call is None` (no se extrajo ninguna llamada de
    esta pasada), `not is_last_allowed_pass` (todavía queda una pasada
    disponible -- en la última no tiene sentido sintetizar una llamada
    más, ya está prohibido el JSON), `wants_file_tools and is_file_write`
    (mismo criterio que patch72), y `not _last_file_op_confirmation`
    (variable pre-existente: todavía no hubo ningún `write_file`/
    `edit_file` exitoso en este turno -- si ya se aplicó el cambio real,
    esta pasada es solo de cierre y no hace falta insistir).

    Dentro de esa condición, dos resultados posibles según lo que
    `_salvage_file_operation` logre extraer de `explanation_or_next`:

    a) Si extrae un `write_file` con contenido REALMENTE distinto al
    que ya existe en el archivo (comparado con `.strip()` en ambos
    lados) -- el caso donde el modelo SÍ escribió una versión mejorada
    en prosa pero olvidó envolverla en el JSON de la herramienta -- se
    aplica como cualquier otro `next_tool_call` normal: se ejecuta de
    verdad en la próxima vuelta del `while`, capturando la mejora real
    que si no se hubiera perdido.

    b) Si el contenido extraído es IDÉNTICO al que ya está en disco --
    el caso EXACTO medido en `tank_io.py`, donde el modelo pegó el
    archivo sin ningún cambio real -- NO se escribe nada. Escribir un
    archivo idéntico y reportar "éxito" sería una falsa confirmación de
    que se mejoró algo cuando en realidad no se tocó nada, exactamente
    el tipo de respuesta deshonesta que este proyecto evita en todos
    los demás guardias (`[SANDBOX WRITE ERROR]`, patch_orchestrator48,
    etc.) -- en vez de eso se arma un mensaje final honesto: "leí el
    archivo pero el cambio que iba a hacer no era distinto al que ya
    tenía, así que no lo reescribí -- ¿podés decirme más específicamente
    qué te gustaría que cambie?".

    Si `_salvage_file_operation` no logra rescatar nada en absoluto (ni
    siquiera código pegado en prosa -- por ejemplo el modelo se negó
    directamente), el comportamiento queda EXACTAMENTE igual que antes
    de este patch: el guardia anti-borrado existente (patch_orchestrator
    48/56) sigue siendo la última red de seguridad. No toca
    `_salvage_file_operation` en sí (se reutiliza tal cual, sin
    modificar su lógica interna ni su firma) ni el flujo ANTES del
    bucle -- es puramente la contraparte simétrica DENTRO del bucle que
    faltaba.

    Verificado con `ast.parse` sobre el archivo completo (994156 bytes)
    y censo de grep: exactamente 1 inserción del bloque, 13 referencias
    a variables `_inloop_salvage_*` (todas dentro del bloque nuevo, sin
    colisión con nombres ya usados en otras partes del bucle como `_np`/
    `_nc`/`_wp`/`_wc`), 2 fases WAL nuevas (`file_op_salvage_inloop`,
    `file_op_salvage_inloop_no_change`) sin reemplazar ninguna fase
    existente. Backup pre-fix guardado como `orchestrator.py.bak62`.

12. **La pasada INICIAL (no solo la de seguimiento) también se truncaba
    sin producir ningún `tool_call` para un `edit_file` sobre un pedido
    amplio/vago -- ahí es donde de verdad se gasta el dinero, y
    patch72/73 no la tocaban (`patch_orchestrator74`)**. Pedido explícito
    del usuario, tras confirmar que patch72/73 no bajaban el costo: "pero
    aun sigue gastandose esos 1000 tokens por pasada?". Respuesta
    honesta que motivó la investigación: NO, patch72/73 no reducen el
    gasto -- el rescate/instrucción actúan sobre una respuesta que Cloud
    YA generó y YA se cobró; no hay forma de "devolver" tokens ya
    generados. Para bajar el costo de verdad hacía falta mirar la pasada
    1 (la generación inicial, ANTES de que exista cualquier pasada de
    seguimiento).

    Diagnóstico, mismo turno de referencia (turn_id
    `b4894dfd-8295-420c-801e-2d6f9b6e13ad`): la pasada 1 YA tenía el
    contenido completo de `tank_io.py` inyectado en su propio contexto
    (`_current_file_context`, confirmado por los 7245 tokens de entrada
    de esa llamada) -- no le faltaba información. Aun así se truncó
    (`done_reason=length`, confirmado leyendo el `done_reason` real de
    esa pasada -- ver la nota de abajo sobre un bug de logging aparte
    encontrado en el camino) gastando el techo completo de 1001 tokens
    sin producir ningún `tool_call` parseable, así que ningún rescate de
    techo existente (patch60/67-70, todos gateados a "hubo un `tool_call`
    ya extraído") pudo intervenir -- cayó directo en
    `_salvage_file_operation` (comportamiento correcto, ya existente).

    Causa raíz: el bloque `[OUTPUT BUDGET]`/`[PRESUPUESTO DE SALIDA]` que
    ya recibe la pasada 1 para `edit_file` sobre un archivo existente
    (`_suppress_write_file_for_turn`, calibrado en patch58/59) le dice
    CÓMO hacer un diff chico (old_str/new_str quirúrgico, lista 'edits'
    para varios lugares, nunca reincluir código sin cambios) pero nunca
    le dice QUÉ cambiar cuando el pedido es amplio o no especifica nada
    ("mejora el código", sin más). Sin esa decisión, el modelo tiende a
    tratar el pedido como "reescribir todo mejor" -- lo que en la
    práctica empuja a `old_str` a cubrir CASI TODO el archivo (se cuenta
    DOBLE: una vez en `old_str`, otra en `new_str`) en vez de un diff
    realmente chico -- exactamente el patrón que el bloque YA advierte
    que hay que evitar, pero sin decirle al modelo cómo no caer en él
    cuando no hay un objetivo concreto. Es el mismo hueco conceptual que
    `patch_orchestrator72` ya había cerrado para la pasada de
    SEGUIMIENTO -- acá faltaba la versión para la pasada INICIAL, que es
    justo donde se paga la cuenta cuando el techo no alcanza.

    Fix: se agrega, al final del mismo bloque `[OUTPUT BUDGET]`/
    `[PRESUPUESTO DE SALIDA]` de la pasada 1 (rama `_suppress_write_file_
    for_turn`, las dos ramas de idioma), una oración más: si el pedido es
    amplio o no dice exactamente qué cambiar, NO intentar reescribir
    partes grandes del archivo ni usar la mayoría/todo el archivo como
    `old_str` (dobla el costo y es muy probable que se corte antes de
    terminar) -- en cambio, elegir UNA mejora concreta, chica y de alto
    valor (una corrección real de un bug, una pieza de funcionalidad
    central que falte, una ineficiencia clara) y hacer SOLO ese cambio,
    con `old_str` cubriendo lo mínimo necesario. No toca el resto del
    bloque (`gen_predict`, `_codegen_soft_target_tokens`, el tope de
    entidades de patch69) ni la rama `write_file` de archivo nuevo (ya
    tenía su propia guía de alcance desde patch65-68) -- solo agrega el
    "qué" que le faltaba al "cómo" ya existente, en el único lugar donde
    todavía no estaba.

    Nota de logging encontrada en el camino, sin corregir todavía (queda
    para una próxima sesión si hace falta depurar de nuevo con esto):
    el log `[EmptyResponseDiag]` (línea ~5991 de `run_turn`) imprime la
    variable `done_reason`, que se asigna UNA sola vez a partir de la
    llamada ANTES del bucle de herramientas y nunca se vuelve a
    actualizar dentro del bucle (`_toolcall_done_reason`, la variable que
    SÍ refleja cada pasada de seguimiento, es local a cada iteración y
    nunca se copia de vuelta a `done_reason`). Como `raw_response` SÍ se
    reasigna correctamente pasada a pasada, ese log termina mezclando
    datos de dos pasadas distintas: el contenido de la ÚLTIMA pasada con
    el `done_reason` de la PRIMERA -- en el turno de referencia esto
    llevó a leer inicialmente "done_reason=length" pensando que
    describía la pasada 2 (la del echo de archivo), cuando en realidad
    describía la pasada 1 (que sí se truncó de verdad) y la pasada 2
    probablemente terminó con `stop_reason` normal. No se corrigió en
    este patch para no mezclar dos cambios distintos en el mismo commit
    -- si hace falta depurar con este log de nuevo, tenerlo en cuenta.

    Verificado con `ast.parse` sobre el archivo completo (999626 bytes)
    y censo de grep confirmando exactamente 1 inserción del marcador
    `patch_orchestrator74`, una por cada rama de idioma, sin tocar
    ninguna otra parte del bloque `[OUTPUT BUDGET]` existente. Backup
    pre-fix guardado como `orchestrator.py.bak63`.

13. **Aun con patch74 funcionando (el modelo SÍ elige una mejora
    concreta), la pasada 1 puede seguir pegando contra el techo de
    `edit_file` si esa mejora toca varios lugares no adyacentes del
    mismo archivo -- guía para usar `edits` con pares chicos POR lugar,
    aunque sean parte de UNA sola mejora, más logging de diagnóstico
    del diff descartado para dejar de adivinar la causa
    (`patch_orchestrator75`)**. Turno de prueba en vivo, inmediatamente
    después de subir patch74: "mejora el codigo de tank io en el
    workspace" -- esta vez SÍ funcionó (patch74 en acción: el modelo
    eligió agregar "un tanque bot con color verde" en vez de dar
    vueltas), pero la pasada 1 volvió a gastar el techo completo (7821
    in / 1001 out) y se truncó, disparando el rescate Local ya existente
    (`patch_orchestrator60`) -- que tardó 70 segundos reales por el
    prefill lento de Ollama en este hardware (4145 tokens a 83 tok/s).
    El usuario preguntó, con razón: si el cambio final terminó siendo de
    apenas 44 bytes (1199 -> 1243), ¿por qué la pasada 1 necesitó más de
    1001 tokens de salida para intentarlo?

    Diagnóstico, con una limitación honesta: `_rescue_truncated_edit_
    file_locally` descarta el diff cortado de Cloud SIN loguear su
    contenido en ningún lado (correcto por seguridad -- no es seguro
    empalmar un JSON de edición roto), así que no había forma de
    confirmar con datos reales por qué se pasó del presupuesto. La
    hipótesis más plausible, explicada al usuario como tal (no como
    hecho confirmado): agregar UNA entidad nueva (un bot) típicamente
    toca 3-4 lugares NO adyacentes de un archivo de juego -- la
    definición de su clase, su instanciación, el loop de actualización
    por cuadro, el dibujo por cuadro. La guía existente (patch58/59) ya
    le dice al modelo "si el pedido toca varios lugares SIN RELACIÓN,
    usá `edits` con pares chicos" -- pero esas 4 partes SÍ están
    relacionadas entre sí (son toda la MISMA mejora), así que el modelo
    puede razonablemente no aplicarse esa instrucción a sí mismo y en
    cambio armar un solo par `old_str`/`new_str` que abarca desde la
    definición de la clase hasta el dibujo, duplicando cada línea sin
    cambios que hay en el medio (se cuenta en `old_str` Y en `new_str`).

    Fix, en dos partes:

    a) Cierre de la ambigüedad de "sin relación": se agrega, al final
    del mismo bloque `[OUTPUT BUDGET]`/`[PRESUPUESTO DE SALIDA]` de la
    pasada 1 que ya tocó `patch_orchestrator74` (mismas dos ramas de
    idioma, `_suppress_write_file_for_turn`), una oración más: la
    separación en `edits` aplica IGUAL aunque todas las partes sean de
    UNA sola mejora -- si agregarla toca varios lugares no adyacentes
    del archivo, igual usar un par `old_str`/`new_str` chico POR lugar,
    nunca un solo par que abarque del primero al último solo porque son
    parte de la misma mejora, porque abarcar entre medio es "la forma
    más común en que este presupuesto se termina gastando de más" (cita
    textual del prompt, a propósito, para que quede clarísimo el porqué
    y no solo el qué).

    b) Logging de diagnóstico, sin cambiar ningún comportamiento: nuevo
    método `_log_edit_file_ceiling_diag(tool_call, turn_id, *, inloop)`,
    llamado justo ANTES de descartar el `tool_call`/`next_tool_call`
    truncado en los DOS lugares donde eso pasa (antes del bucle de
    herramientas y dentro de él, los mismos dos puntos que ya loguean
    `edit_file_ceiling_local_rescue`/`_inloop`). Cuenta los caracteres
    totales de `old_str`+`new_str` del intento cortado -- sumando todos
    los pares si el modelo ya venía usando `edits`, o el par único si
    no -- y cuántos pares intentó, y lo manda al WAL como una fase nueva
    (`edit_file_ceiling_diag`/`_inloop`). Puro logging (`contextlib.
    suppress(Exception)`, nunca puede romper el turno) -- la próxima vez
    que esto pase, se va a poder confirmar o descartar la hipótesis de
    "un solo par gigante" con datos reales del WAL en vez de inferencia
    indirecta a partir del tamaño del diff final aplicado.

    No toca `_rescue_truncated_edit_file_locally` en sí (sigue
    descartando el diff cortado exactamente igual, solo se agrega una
    línea de logging ANTES de esa llamada) ni ningún otro bloque de
    `[OUTPUT BUDGET]` (el tope de entidades de patch69, el objetivo con
    margen de patch58/59, la guía de patch74 sobre pedidos amplios --
    todos quedan intactos, esto se agrega COMO UN PASO MÁS después).
    Verificado con `ast.parse` sobre el archivo completo (1008189
    bytes) y censo de grep: 1 definición + 2 sitios de llamada de
    `_log_edit_file_ceiling_diag` (antes del bucle, dentro del bucle,
    con `inloop=False`/`True` respectivamente), 2 fases WAL nuevas
    (`edit_file_ceiling_diag`, `edit_file_ceiling_diag_inloop`) sin
    reemplazar ninguna fase existente, 1 inserción del bloque de guía
    nueva en cada rama de idioma del `[OUTPUT BUDGET]` de la pasada 1.
    Backup pre-fix guardado como `orchestrator.py.bak64`.

14. **Un seguimiento conversacional sin nombre de archivo ("mejoralo aun
    mas y añade cosas") perdía TODO el contexto del turno anterior en vez
    de heredarlo, cayendo en una clasificación completamente distinta y
    más barata que rompía el turno** (`patch_orchestrator76`). Pedido del
    usuario tras ver, en la misma tanda de pruebas de patch72-75, DOS
    turnos consecutivos de la misma conversación: "mejora el codigo de
    tank io en el workspace" (resolvió bien, ver ítem 13) seguido de
    "mejoralo aun mas y añade cosas" (roto de una forma nueva) -- "
    identifica que esta bien y que esta mal".

    Diagnóstico, confirmado línea por línea (no solo por WAL): el segundo
    mensaje no menciona ni el nombre del archivo, ni una extensión, ni
    "workspace", ni ninguna palabra descriptiva matcheable por frase
    contra un nombre de archivo existente -- las TRES ramas existentes de
    `_has_file_write_intent` daban `False` sin excepción (confirmado
    trazando `_FILE_WRITE_FNAME_RE`, `_FILE_WRITE_INTENT_RE`+`_WS_FILE_
    EXT_RE`/`_WORKSPACE_KEYWORD_RE`, y `_FILE_MODIFY_VERB_RE`+`_match_
    workspace_file_by_phrase` una por una contra el texto real). Eso deja
    `is_file_write=False`, y `_resolve_modify_target` empieza con `if not
    is_file_write: return None, False` (línea ~10803) -- un corte total
    que vuelve INALCANZABLE el resto de su propia cascada de fallbacks,
    incluido su ÚLTIMO fallback ya existente (`_most_recent_workspace_
    file()`, gateado por el mismo `_FILE_MODIFY_VERB_RE` que SÍ matchea
    "mejoralo"/"añade") -- ese fallback casi con certeza habría resuelto
    bien este caso puntual (era, de hecho, el archivo modificado más
    recientemente), pero nunca llegaba a ejecutarse. Confirmado también
    vía WAL del turno real: `codegen_budget_plan` cayó en categoría
    `'default'` (techo 900, piso 120) en vez de `'juego'` (piso 450), sin
    ninguna fase `file_write_model_override`, con secuencia de
    herramientas `read_file` -> `write_file` (cortado) -> rescate Local
    -> `write_file` (éxito, 1360 bytes) y respuesta final reducida al
    volcado crudo del `tool_result`, sin ninguna explicación -- porque
    NINGUNA de las guías de patch72/74/75 llegó a inyectarse (viven
    únicamente en la rama `_suppress_write_file_for_turn == True`, que
    tampoco se activó).

    Fix: nuevo atributo de instancia `self._last_conversational_file_
    target: Optional[str]` (inicializado en `None` en `__init__`, junto a
    `_pause_governor_event`) que guarda el último archivo que un turno
    resolvió como objetivo real de modificación (`mod_target` cuando
    `mod_is_modify` fue `True`) o que un turno creó de cero (`write_file`
    exitoso). Tres puntos de integración:

    a) `_has_file_write_intent` gana una CUARTA rama: verbo de modificar
    angosto (`_FILE_MODIFY_VERB_RE`, el mismo de siempre -- nunca "crea"/
    "hace") combinado con este estado todavía vigente (el archivo sigue
    existiendo en el sandbox, verificado con `_sandbox_file_exists`) es
    señal suficiente de intención de escritura, sin necesitar nombre,
    extensión ni "workspace" en el mensaje actual.

    b) `_resolve_modify_target` gana un fallback nuevo, ANTES del
    fallback de mtime puro (`_most_recent_workspace_file`): si el pedido
    matchea `_FILE_MODIFY_VERB_RE` y este estado apunta a un archivo que
    sigue existiendo, se prefiere sobre la fecha de modificación -- es
    una señal más confiable (el propio usuario lo referenció
    explícitamente hace un turno) que "es el archivo tocado más
    recientemente" (que puede apuntar a un archivo basura dejado por un
    rescate fallido, ver el BLINDAJE de `patch_orchestrator53`).

    c) `run_turn` actualiza el estado en dos puntos: justo después de
    calcular `mod_target`/`mod_is_modify` (lo fija si el turno resolvió
    bien un objetivo de modificación; lo limpia a `None` si el turno no
    tiene NINGUNA relación con archivos -- ni verbo de modificar, ni
    intención de escritura por ninguna otra vía, para no arrastrarlo
    stale a una conversación ya desviada a otro tema) y junto a la
    asignación existente de `_file_created_this_turn_path` (un archivo
    recién creado en la misma pasada también queda disponible para un
    seguimiento futuro).

    Riesgo aceptado, mismo perfil que el fallback de mtime que ya
    convivía en el código: un mensaje con verbo de modificar que en
    realidad NO es sobre archivos ("corrige eso en tu razonamiento"),
    enviado justo después de una racha de turnos de archivo sin ningún
    turno intermedio que limpie el estado, podría secuestrarse
    incorrectamente hacia ese archivo. Se considera un riesgo menor y
    aceptable frente al bug que arregla (perder por completo un
    seguimiento natural en español), y la limpieza agresiva del punto
    (c) acota la ventana en la que puede pasar.

    Verificado con `ast.parse` sobre el archivo completo (1015508 bytes)
    y censo de grep: 13 apariciones de `_last_conversational_file_target`
    en total -- 1 declaración en `__init__`, 2 usos en la nueva rama de
    `_has_file_write_intent`, 3 usos en el nuevo fallback de
    `_resolve_modify_target` (más comentarios), 3 en la actualización
    junto a `_resolve_modify_target` en `run_turn`, 1 en la actualización
    junto a `_file_created_this_turn_path`, el resto comentarios de
    referencia cruzada -- sin duplicados ni huérfanos. Backup pre-fix
    guardado como `orchestrator.py.bak65`.

15. **Mitigación del riesgo residual que `patch_orchestrator76` había
    dejado documentado a propósito, sin resolver: un verbo de modificar
    sin relación real con archivos, enviado justo después de una racha
    de turnos de archivo, podía secuestrarse hacia el último archivo
    tocado** (`patch_orchestrator77`). Pedido explícito del usuario tras
    revisar el patch anterior: "y como resolvemos ese riesgo residual".

    Se le presentaron tres opciones, con sus tradeoffs explícitos: (a)
    lista de exclusión de temas no-archivo, barata y dirigida pero nunca
    100% exhaustiva; (b) TTL de un solo turno, que reduce la ventana de
    riesgo al mínimo pero corta cadenas legítimas de 2+ seguimientos
    seguidos sobre el mismo archivo ("mejoralo..." → "y tambien
    cambiale el color..."); (c) combinar ambas. Eligió (a).

    Fix: nueva constante `_NON_FILE_TOPIC_RE`, junto a `_FULL_REWRITE_
    SIGNAL_RE`, con un regex bilingüe (español/inglés) de temas de
    conversación claramente NO relacionados con código/archivos --
    razonamiento, explicación, respuesta escrita, argumento, lógica,
    opinión, punto de vista, poema, cuento, ensayo, resumen, traducción,
    correo/email, gramática, ortografía. Deliberadamente NO incluye
    palabras ambiguas que también pueden referirse a un archivo real
    ("estilo"/"tono" -- también el estilo visual de un juego;
    "historia" -- también la trama de un juego) -- se prefiere un falso
    negativo (un caso raro no cubierto) antes que un falso positivo
    (bloquear un seguimiento de archivo legítimo). Lista deliberadamente
    NO exhaustiva -- si aparece un caso real no cubierto, sumarlo a la
    lista en vez de rediseñar el mecanismo.

    Cableado como condición negativa adicional en los DOS puntos que
    `patch_orchestrator76` había agregado: la cuarta rama de `_has_file_
    write_intent` (ahora exige `_FILE_MODIFY_VERB_RE` Y `_last_
    conversational_file_target` vigente Y **no** `_NON_FILE_TOPIC_RE`) y
    el nuevo fallback de `_resolve_modify_target` (misma condición
    negativa agregada). Probado en aislamiento con los dos regex reales
    contra 7 frases (el caso original del bug, dos variantes del riesgo
    residual -- "corrige eso en tu razonamiento"/"corrige eso en tu
    explicacion", "mejora esa respuesta que me diste" -- y tres
    seguimientos legítimos con palabras parecidas pero NO en la lista de
    exclusión -- "arreglalo y sube la dificultad", "cambia el estilo del
    juego", "mejora la historia del juego"): los tres casos de riesgo
    quedaron bloqueados, los cuatro casos legítimos (el original más los
    tres de estilo/historia) siguieron heredando el archivo sin cambios.

    Verificado con `ast.parse` sobre el archivo completo (1019022 bytes)
    y censo de grep: 5 apariciones de `_NON_FILE_TOPIC_RE` -- 1
    declaración, 2 usos reales (uno por punto de integración), 2
    referencias en comentarios -- sin duplicados ni huérfanos. Backup
    pre-fix guardado como `orchestrator.py.bak66`.

16. **Un pedido de ARCHIVO NUEVO con un título largo ("the binding of
    isaac") nunca activaba NINGUNA de las herramientas de presupuesto/
    alcance construidas en toda esta sesión, porque el verbo y el
    sustantivo que confirman intención de escritura tenían que estar a
    6 palabras o menos de distancia** (`patch_orchestrator78`). Reporte
    del usuario con capturas de la Consola de comandos: "crea un the
    binding of isaac basico en el workspace" terminó en un archivo de
    116 bytes, prácticamente vacío, tras gastar el techo completo de
    900 tokens ("Bajo") sin producir nada usable.

    Diagnóstico por comparación directa en `sovnode.wal`: los dos turnos
    de tank_io.py de la misma sesión (ítems 13/14) loguean una fase
    `codegen_budget_plan` con categoría/techo/objetivo/tope de
    entidades; el turno de Isaac salta de `routed` directo a
    `generation_done` -- SIN esa fase, SIN `file_write_model_override`.
    Eso solo pasa cuando `is_file_write` da `False`. Causa raíz aislada
    y confirmada con el regex real en Python: `_FILE_WRITE_INTENT_RE`
    exigía que el verbo ("crea") y el sustantivo de artefacto
    ("workspace") estuvieran separados por `{0,6}` palabras como máximo
    -- "crea un THE BINDING OF ISAAC BASICO EN EL workspace" tiene 8
    palabras en el medio, así que no matcheaba, y CERO de las guías de
    presupuesto/alcance de patch58/59/69/71/74/75 llegaban a
    inyectarse. El modelo escribió sin ninguna guía de alcance, no logró
    cerrar un JSON válido dentro del techo (contenido final: 0
    caracteres útiles, confirmado por `_continue_truncated_file_write_
    locally`), y el rescate local de emergencia produjo el archivo de
    116 caracteres que vio el usuario.

    Primera propuesta (presentada al usuario con su tradeoff): ampliar
    el tope de 6 a 8 palabras -- alcanzaba exactamente para este caso.
    El usuario pidió ir más lejos: "que la distancia sea irrelevante?
    que siempre matche no importa cuantas distancia de palabra este".

    Fix aplicado: se saca el tope de `_FILE_WRITE_INTENT_RE` por
    completo (`(?:\W+\w+){0,6}?` -> `(?:\W+\w+)*?`) -- el verbo y el
    sustantivo matchean sin importar cuántas palabras haya en el medio,
    mientras aparezcan en ese orden en el mismo mensaje. Único call site
    de esta regex (`_has_file_write_intent`, rama 2), así que el cambio
    no tiene efectos ocultos en otro lado.

    Riesgo aceptado, EXPLÍCITAMENTE más amplio que el de la propuesta de
    8 palabras -- verificado con regex reales extraídos del propio
    archivo antes de aplicar: varios verbos de la lista son de uso muy
    común en español conversacional fuera de contexto de archivos
    (`hac[eé]\w*`, `arregl\w*`, `actualiz\w*`...), así que un mensaje
    largo, sin relación real con archivos, que mencione uno de estos
    verbos en cualquier parte Y "workspace" (u otro sustantivo de la
    lista) en cualquier otra parte, ahora matchea igual -- medido con un
    caso adversarial real: "no se si debería HACER ese cambio de
    trabajo... quiero limpiar mi WORKSPACE físico del escritorio"
    matchea, pese a no pedir nada de archivos. Mitigación parcial
    agregada en el mismo patch (no reduce el riesgo de distancia, que
    el usuario pidió eliminar a propósito, pero sí descarta un
    subconjunto): se suma `and not self._NON_FILE_TOPIC_RE.search(text)`
    (la misma lista de exclusión de patch77) a la rama 2 de `_has_file_
    write_intent` -- un mensaje que además menciona razonamiento/
    explicación/respuesta/etc. sigue bloqueado, verificado en el mismo
    caso de prueba ("corrige eso en tu razonamiento sobre el
    workspace" -> bloqueado). No cubre charla cotidiana sin ningún tema
    de esa lista -- aceptado a pedido explícito del usuario, con la
    lista disponible para sumarle casos puntuales si aparece un falso
    positivo real en el uso.

    Verificado con `ast.parse` sobre el archivo completo (1022763
    bytes), censo de grep (`patch_orchestrator78`: 2 apariciones, 1
    docstring + 1 comentario corto, sin duplicados) y prueba con los
    regex REALES extraídos por script del propio archivo (no
    reconstruidos a mano) contra 4 casos: el bug original (matchea ✓),
    un título largo similar de control (matchea ✓), el caso bloqueado
    por `_NON_FILE_TOPIC_RE` (bloqueado ✓) y el caso adversarial de
    riesgo aceptado (matchea, como se documentó arriba). Backup pre-fix
    guardado como `orchestrator.py.bak67`.

17. **Con patch78 ya haciendo que "crea un binding of isaac basico en el
    workspace" active toda la maquinaria de presupuesto, salió a la luz
    un bug DISTINTO y más caro: el modelo volvía a llamar `write_file`
    sobre el MISMO archivo recién creado, repitiendo el ciclo completo
    de techo-de-Cloud + rescate Local hasta que el guardia anti-bucle
    abortaba el turno** (`patch_orchestrator79`). Reporte del usuario
    con capturas nuevas tras probar patch78 en vivo: "explica porque
    volvio a pasar y peor" -- el turno terminó gastando $0.0549 en 3
    llamadas a Cloud (vs $0.0202 en 1 llamada, antes de patch78) y
    cerró con "me detengo, aclará" en vez de confirmar el archivo. El
    usuario también compartió un diagnóstico externo (de otra sesión de
    IA) que coincidía en lo esencial con el análisis propio hecho acá
    contra `sovnode.wal`/`sovnode_debug.log` del turno real
    (`0fa75fcf-5489-4017-b5d3-405e043167cb`).

    Diagnóstico confirmado con los logs reales, en dos partes
    independientes:

    a) **Por qué "otra vez" (el techo de 900 sigue sin alcanzar):**
    Sonnet sigue sin poder cerrar un `write_file` completo de un juego
    tipo Isaac dentro de 900 tokens, incluso con toda la guía de
    alcance ya construida (patch58/59/69/71/74/75). Hipótesis, no
    confirmada con certeza absoluta pero consistente con el patrón
    medido (`fresh_len`/`salvaged_len` de los rescates, siempre unos
    pocos cientos de caracteres tras agotar 900 tokens completos): el
    modelo abre con andamiaje de clases (`class Player`, `class Enemy`,
    `class Tear`) que consume 500-700 tokens antes de llegar a lógica
    real de juego.

    b) **Por qué "peor" -- la causa raíz real de la regresión, con
    evidencia dura del WAL:** la pasada 1 SÍ se rescató bien en Local
    (gratis) y escribió contenido real en `isaac_basico.py`
    (confirmado: `RiskGate` bajó a "riesgo low: crea un archivo nuevo"
    solo en la pasada 1; para la pasada 2 el mismo mensaje se repite
    IDÉNTICO, señal de que el sistema todavía no registra el archivo
    como existente). El aviso `[FILE STATE]` que ya existía ("ese
    archivo ya existe, preferí edit_file... usá write_file de nuevo
    SOLO SI hace falta un archivo NUEVO y distinto") es una sugerencia
    de texto, no una restricción real -- `write_file` seguía en el
    schema de herramientas real que Cloud recibe, porque
    `_suppress_write_file_for_turn` (la variable que controla
    `exclude_tools={"write_file"}` en `_cloud_tools_schema_for_prompt`)
    se calcula UNA SOLA VEZ al principio del turno, antes de que exista
    `_file_created_this_turn_path` -- para un archivo NUEVO (el caso de
    Isaac) arranca en `False` y NUNCA se reevalúa. El modelo, viendo
    `write_file` todavía disponible, lo volvió a llamar dos veces
    seguidas pese al aviso de texto -- cada intento repite el ciclo
    completo (pasada a Cloud, techo de 900, rescate Local de 15-50s) --
    hasta que el guardia `repeated_without_progress` (ya existente,
    detecta 3 llamadas idénticas seguidas) abortó el turno.

    Fix, en tres partes:

    1. `suppress_write_file` en la llamada que genera cada pasada
    siguiente del bucle (`_call_llm_raw`, la que produce `next_tool_
    call`) pasa de `_suppress_write_file_for_turn` (fijo) a
    `_suppress_write_file_for_turn or bool(_file_created_this_turn_
    path)` -- en cuanto CUALQUIER archivo (nuevo o existente) se
    escribió con éxito en este turno, `write_file` se saca del schema
    real de Cloud para el resto del turno, la misma garantía a nivel
    API que ya usa la supresión original para archivos existentes.
    Revierte a propósito la decisión de `patch_orchestrator61` de
    dejar `write_file` siempre disponible "por si hace falta un
    segundo archivo nuevo genuino" -- ese caso sigue siendo posible en
    teoría pero ahora requiere un turno de seguimiento, a cambio de
    eliminar un patrón de fallo ya medido DOS veces (turno de tank_io
    con bots, item previo de esta sesión, y este de Isaac) y cada vez
    más caro.

    2. Texto del aviso `[FILE STATE]`/`[ESTADO DEL ARCHIVO]`
    actualizado de "preferí edit_file... usá write_file de nuevo SOLO
    SI..." a "write_file YA NO ESTÁ DISPONIBLE por el resto de este
    turno -- volver a llamarlo va a fallar" -- para que el modelo no
    pierda una pasada entera intentando una herramienta que ya no
    existe en su menú real.

    3. Instrucción explícita, SOLO para el nivel de presupuesto más
    bajo (`self.cloud_output_budget_cents <= 1`, "Bajo" -- 1 centavo,
    ~900 tok; se usa el nivel de centavos directamente, no un número de
    tokens hardcodeado, para no depender de que el precio del modelo
    Cloud no cambie), agregada al final del mismo bloque `[OUTPUT
    BUDGET]`/`[PRESUPUESTO DE SALIDA]` de la generación inicial que ya
    tocaron patch58/59/69/71/74/75/78: prohíbe directamente la palabra
    clave `class` y exige código procedimental plano (variables
    sueltas, listas/tuplas para el estado, como máximo una función
    `main()`). Apunta al motivo (a) de arriba -- ataca la causa
    probable del corte inicial, no solo sus síntomas. Sin garantía de
    resultado medida todavía (a diferencia del punto 1, que es
    determinístico a nivel código): depende de que el modelo respete
    la instrucción, igual que el resto de las guías de `[OUTPUT
    BUDGET]`.

    Riesgo/alcance no cubierto en este patch, documentado a propósito:
    el bloque de "apuntá a un diff chico" que ya existe para pasadas de
    reintento (`if wants_file_tools and _suppress_write_file_for_turn
    and not is_last_allowed_pass:`, unas líneas más abajo del fix 1) NO
    se extendió para cubrir también el caso de supresión DINÁMICA de
    este patch -- el modelo forzado a `edit_file` tras este fix recibe
    el aviso `[FILE STATE]` pero no el objetivo numérico de "diff más
    chico" que sí tienen los turnos que arrancaron suprimidos desde el
    principio. No debería romper nada (el techo real de `edit_file` se
    sigue calculando bien por otra vía), es una oportunidad de mejora
    menor dejada para una próxima revisión si hace falta.

    Verificado con `ast.parse` sobre el archivo completo (1030364
    bytes) y censo de grep: 5 apariciones de `patch_orchestrator79` (2
    en el bloque de prohibición de POO -- inglés/español --, 2 en el
    aviso `[FILE STATE]` actualizado, 1 en el `suppress_write_file`
    dinámico), sin duplicados ni huérfanos. Backup pre-fix guardado
    como `orchestrator.py.bak68`.

18. **Recalibración del "piso mínimo viable" (`_estimate_min_viable_
    codegen_tokens`) tras confirmar por WAL que el propio turno de Isaac
    (con patch78 ya activo) pasó el chequeo de infeasibilidad por error**
    (`patch_orchestrator80`). El usuario compartió capturas de una
    conversación con otra IA que, independientemente, propuso un "Pre-
    flight Feasibility Check" -- un cortador temprano a costo $0 basado en
    una matriz de pisos por categoría, para requests claramente
    infeasibles con el presupuesto elegido. Antes de implementarlo, se
    verificó si esto ya existía: SÍ, desde `patch_orchestrator58`-`71`
    (antes de esta sesión) -- `_estimate_min_viable_codegen_tokens` +
    `_codegen_budget_infeasible` (WAL phase `codegen_budget_infeasible`,
    `ceiling`/`floor`), exactamente el mismo diseño. Construir una función
    nueva en paralelo hubiera duplicado esta lógica (mismo problema que ya
    admiten los comentarios de `router._CODE_COMPLEX_PATTERN` vs.
    `Orchestrator._RUNNABLE_PROGRAM_NOUN_RE`: dos listas de géneros de
    juego mantenidas a mano por separado).

    Cruzando el WAL real del turno `0fa75fcf-5489-4017-b5d3-405e043167cb`
    ("crea un binding of isaac basico en el workspace", ya con patch78
    activo): `codegen_budget_plan` registró `category=default
    hard_ceiling=900 min_viable_floor=120` -- el chequeo SÍ corrió, pero
    dio "factible" (900>120) porque ninguna categoría reconocida (`juego`/
    `gui`/`servidor`/`complejidad_generica`) matcheaba "isaac", y el prompt
    del usuario nunca dijo "juego"/"game" explícitamente. Resultado real:
    NO fue factible (terminó en 3 llamadas a Cloud, $0.0549, turno
    abortado) -- el piso de 120 (el default genérico, pensado para un
    script plano de andamiaje mínimo) es la asunción MÁS optimista
    posible, y es justo la que falló acá.

    Dos cambios, ambos en `_MIN_VIABLE_FLOOR_PATTERNS`/
    `_estimate_min_viable_codegen_tokens`/`_min_viable_floor_category`,
    pedidos explícitamente por el usuario ("Dile a Claude Code que proceda
    con su propuesta", tras aprobar los dos puntos vía esa otra IA):

    1. **Recalibración del piso de `juego`**: de 450 a 1300 tokens (dentro
       del rango 1200-1400 que pidió el usuario, que a su vez coincide con
       lo medido en vivo en patches anteriores -- agar.io/tank.io con bots
       pagaron ~1800 tokens reales de salida antes de cortarse, muy por
       encima del viejo piso de 450). Efecto directo: con Presupuesto
       "Bajo" (techo=900), cualquier pedido que SÍ mencione un género de
       juego reconocido ahora corta antes de llamar a Cloud (900<1300) en
       vez de intentarlo y fallar después.
    2. **Cierre del agujero de títulos no reconocidos** (el caso real de
       Isaac, que ninguna lista de géneros iba a cubrir jamás -- mismo
       problema, sin resolver, que ya admite el comentario de
       `patch_orchestrator68`): en vez de agregar nombres de juegos uno
       por uno a una lista infinita, se invirtió el criterio para pedidos
       de ARCHIVO NUEVO. Si NINGUNA categoría reconocida matchea, ya NO se
       asume el caso más barato posible (120 tokens) -- se asume
       `_MIN_VIABLE_FLOOR_UNRECOGNIZED_NAME` (1300, mismo orden de
       magnitud que "juego"), SALVO que el pedido matchee una señal
       explícita y chica de utilidad genérica tipo CS-101
       (`_TRIVIAL_UTILITY_SIGNAL_RE`: calculadora, conversor de
       temperatura, suma/resta, fibonacci, ordenar una lista, etc. -- una
       lista sí sostenible a mano, porque a diferencia de nombres de
       juegos/franquicias, el vocabulario de "operación trivial" es chico
       y estable). Costo asimétrico a propósito: un falso positivo acá
       (pedido trivial que cae en el piso alto) cuesta, como mucho, un
       aviso de "subí el presupuesto" a costo $0; un falso negativo (lo
       que pasaba con Isaac) cuesta varias llamadas reales a Cloud que
       terminan fallando igual.

       Verificado con una simulación standalone de la regex/lógica antes
       de tocar el archivo real (no solo `ast.parse` después): "the
       binding of isaac básico" y "mario bros básico" → 1300; "conversor
       de celsius a fahrenheit" y "sumar dos números" → 120 (sin
       regresión); "restaurante"/"gestión de restaurante" NO disparan la
       señal de "resta" por accidente (se usó una lista explícita de
       conjugaciones para "resta", no `rest\w*`, después de que la primera
       versión de la regex sí tuviera ese falso positivo en la simulación).

    Riesgo residual documentado (no medido todavía, mismo criterio de
    calibración que el resto de esta sección): un pedido de archivo nuevo
    genuinamente trivial que no esté en `_TRIVIAL_UTILITY_SIGNAL_RE` ni
    mencione ninguna categoría reconocida ahora también cae en el piso
    alto (1300) y podría rebotar en Presupuesto "Bajo" sin necesitarlo de
    verdad -- pendiente ampliar `_TRIVIAL_UTILITY_SIGNAL_RE` con casos
    reales medidos por WAL (misma etiqueta nueva de telemetría,
    `"nombre_no_reconocido"`, agregada a `_min_viable_floor_category` para
    poder identificar estos casos después).

    Verificado con `ast.parse` sobre el archivo completo (1036431 bytes) y
    censo de grep: 4 apariciones de `patch_orchestrator80` (comentarios de
    recalibración del piso de juego, cierre del agujero de nombres no
    reconocidos y su docstring), sin duplicados ni huérfanos. Backup
    pre-fix guardado como `orchestrator.py.bak69`, commit al dispositivo
    exitoso al primer intento (sin necesitar `force: true`).

19. **El bailout de infeasibilidad ($0) de patch80 cortaba la generación
    pero igual terminaba disparando todo el circuito caro de rescate de
    archivos** (`patch_orchestrator81`). El usuario retesteó el mismo
    pedido de Isaac tras patch80 y compartió el log completo: "salio
    fatal" -- mismo resultado que antes de patch80 ($0.0575, 3 llamadas a
    Cloud, turno abortado por bucle). Diagnóstico por WAL (turno
    `1cf8b7c8-8277-48b9-b8ae-fe4433a5d900`): `codegen_budget_infeasible`
    (`ceiling=900 floor=1300`) y `codegen_budget_infeasible_bailout`
    SÍ dispararon correctamente -- `generation_done` con `stats={}`
    inmediatamente después confirma que la generación se saltó del todo,
    exactamente como diseñó patch80. El problema apareció 20 segundos
    después: `file_op_salvage`.

    Causa raíz: el "Blindaje de operaciones de archivo" (2026-09-03,
    mucho más viejo que todo el mecanismo de presupuesto de codegen --
    patches 58 en adelante) dispara siempre que `tool_call is None and
    wants_file_tools` -- sin distinguir POR QUÉ `tool_call` es `None`. El
    bailout de infeasibilidad deja `tool_call = None` a propósito (así es
    como corta gratis, con un mensaje explicativo en texto plano en vez de
    una llamada a herramienta) -- pero este guardia interpreta cualquier
    `tool_call is None` en un turno de archivo como "el modelo se negó o
    no contestó", y sintetiza una llamada real igual, re-disparando
    exactamente el mismo circuito caro (regenerar con modelo sin censura,
    escalar de modelo, pegar contra el techo, rescate Local, abortar por
    bucle) que el bailout de $0 existía para evitar. El propio mecanismo
    de infeasibilidad de `patch_orchestrator58`-`71` tenía este bug desde
    su creación -- nunca se había medido un caso real donde el bailout
    disparara Y el turno fuera además un pedido de archivo con
    `wants_file_tools=True`, hasta este turno de Isaac.

    Fix, una sola condición agregada al guardia de `file_op_salvage`
    (`if tool_call is None and wants_file_tools and not cancelled():` →
    se le suma `and not _codegen_budget_infeasible`): un bailout
    intencional (`_codegen_budget_infeasible=True`) ya no activa el
    rescate de archivos -- solo lo activa un silencio real del modelo.
    `_codegen_budget_infeasible` ya estaba en el mismo scope de `run_turn`
    (inicializado en `False` antes de cualquier rama de generación), así
    que no hizo falta pasarlo como parámetro ni tocar la firma de ninguna
    función.

    Verificado con `ast.parse` sobre el archivo completo (1038133 bytes) y
    censo de grep: 1 aparición de `patch_orchestrator81` (el comentario
    BLINDAJE junto a la condición modificada), sin duplicados ni
    huérfanos. Backup pre-fix guardado como `orchestrator.py.bak70`,
    commit al dispositivo exitoso al primer intento.

    Pendiente de retest en vivo (no verificado todavía con una llamada
    real del usuario después de este fix): confirmar que el turno de
    Isaac ahora sí corta a `$0.0000` de punta a punta, sin ningún
    `file_op_salvage` ni llamada a Cloud.

    **Retest confirmado por el usuario**: 4 turnos seguidos ("isaac
    basico", "DOOM", "DOOM basico" x2) cortaron los cuatro en
    `$0.0000`, `0 tool_call(s)`, sin `file_op_salvage` -- el fix
    funcionó de punta a punta.

20. **Techo elástico por tier: "Bajo" (y los demás niveles) ya no
    rebotan siempre en pedidos de juego, ahora pueden subir un escalón
    de presupuesto (sin tocar la selección guardada del usuario) cuando
    hace falta** (`patch_orchestrator82`). Tras confirmar el retest de
    patch81, el usuario notó que CUALQUIER pedido de juego en "Bajo"
    rebotaba siempre (900tok < piso de 1300 para "juego"/"nombre_no_
    reconocido", sin excepción) y preguntó "porque mejor no hacemos mas
    flexible el techo... 700 a 1100 tokens". Antes de implementar se le
    señaló que ESE rango específico no resolvía nada (1100 sigue por
    debajo de 1300) y se le presentó la disyuntiva real: el "techo"
    (tope real pagado a Cloud) y el "piso" (mínimo estimado necesario)
    son dos números distintos con propósitos distintos, y la garantía de
    costo fijo por tier (`patch_orchestrator16`, "el techo del usuario
    manda tal cual, sin subirlo nunca") es la que hace que "Bajo" rebote
    siempre en vez de intentar. Se le ofrecieron 3 opciones (mantener el
    costo fijo y listo; suavizar solo el chequeo con tolerancia; o
    permitir que el techo real suba de tier cuando hace falta) -- eligió
    expresamente la tercera, aceptando que "Bajo" deja de costar 1¢ fijo
    y puede llegar a costar hasta lo que cueste el próximo escalón que
    alcance, y pidió que aplique a los 4 niveles del selector, no solo a
    "Bajo".

    Implementación: nuevo método `_cloud_output_ceiling_tokens_for_cents
    (cents)` (refactor sin cambio de comportamiento de `_cloud_output_
    ceiling_tokens`, que ahora es un wrapper de una línea sobre este con
    el `cents` actual del usuario) para poder cotizar el techo real de
    CUALQUIER nivel sin tocar `self.cloud_output_budget_cents`. Nuevo
    método `_elevate_codegen_ceiling_if_needed(gen_predict, floor)`: si
    el techo actual ya alcanza el piso, no hace nada; si no alcanza,
    sube un escalón a la vez por la escalera 1¢→2¢→4¢→8¢ (`_CLOUD_
    OUTPUT_BUDGET_CENTS_LADDER`) y se queda en el PRIMERO que sí alcanza
    -- nunca salta directo al más caro, mínimo gasto extra necesario. Si
    ni "Extra" (8¢, el techo más alto que existe) alcanza, no hay
    elevación posible y el turno sigue cortando a `$0` exactamente como
    antes de este patch (pedidos genuinamente enormes siguen sin
    intentarse a ciegas). Call site: dentro del chequeo de infeasibilidad
    existente (`is_file_write and not mod_is_modify`, mismo bloque de
    patch80/81) -- SOLO se invoca cuando el turno YA iba a cortar a `$0`
    sin la elevación (`gen_predict < _min_viable_floor`), nunca sube el
    techo de un turno que igual iba a andar con el techo normal, así que
    la garantía de costo de `patch_orchestrator16` sigue intacta para
    todos los demás casos -- esta es la única excepción, explícita y
    documentada, no un debilitamiento general de esa garantía.

    Transparencia: se agrega un `_wal_phase` nuevo (`codegen_budget_
    tier_elevated`, con `from_cents`/`to_cents`/`old_ceiling`/
    `new_ceiling`/`floor`) y un mensaje visible en el log del turno
    ("Presupuesto elevado solo para este turno (tu selección guardada no
    cambia): 'Bajo' (900tok) no alcanza el piso mínimo de este pedido
    (1300tok) -- subiendo a 'Medio' (1800tok) para este turno.") -- el
    usuario ve exactamente cuándo y por qué se gastó más de lo que su
    selector normalmente costaría, nunca en silencio.

    Riesgo residual, aceptado explícitamente por el usuario: un pedido
    en "Bajo" que antes constaba $0 (por infeasibilidad, sin excepción)
    ahora puede terminar costando hasta lo que cueste "Extra" en el peor
    caso, si el piso estimado lo exige -- ya no hay una garantía de "como
    mucho 1¢" para pedidos de archivo nuevo en "Bajo", solo "como mucho
    lo que cueste el tier más barato que alcance, o $0 si ninguno
    alcanza". Documentado acá porque es un cambio deliberado a una
    garantía que `patch_orchestrator16` había establecido como
    invariante -- cualquier lectura futura de ese BLINDAJE debe saber que
    ya no es absoluto, tiene esta excepción puntual y documentada.

    Verificado con una simulación standalone de la lógica de escalera
    antes de tocar el archivo real (6 casos: elevar Bajo→Medio, Bajo→
    Extra saltando Medio/Alto, sin cobertura en ningún tier, ya cubierto
    sin elevar, elevar Medio→Alto, sin cobertura en Extra) -- los 6 dieron
    el resultado esperado. `ast.parse` sobre el archivo completo (1045223
    bytes) y censo de grep: 3 apariciones de `patch_orchestrator82` (el
    BLINDAJE de `_elevate_codegen_ceiling_if_needed`/`_cloud_output_
    ceiling_tokens_for_cents` y el call-site en el chequeo de
    infeasibilidad), sin duplicados ni huérfanos. Backup pre-fix guardado
    como `orchestrator.py.bak71`, commit al dispositivo exitoso al primer
    intento. Pendiente de retest en vivo con un pedido de juego en "Bajo"
    para confirmar que ahora sí eleva y produce un archivo real, en vez
    de solo cortar a $0.

    **Retest en vivo, resultado mixto**: el usuario probó "crea un juego
    de doom basico en el workspace" en "Bajo" -- la elevación disparó
    correctamente (`codegen_budget_tier_elevated` 1→2, 900→1800,
    `codegen_budget_plan categoria=juego techo=1800`), y esta vez el
    modelo SÍ intentó escribir contenido real (a diferencia de Isaac, sin
    ningún rechazo de "Blindaje de archivos" en el log). Pero el
    `write_file` llegó EXACTO al techo de 1800 sin terminar (JSON cortado
    a mitad de un string -- `raw_response_len=76 salvaged_len=0` en
    `sovnode_debug.log`), disparando el rescate Local gratis
    (`_continue_truncated_file_write_locally`, "escritura local desde
    cero"). El usuario canceló la generación manualmente 12 segundos
    después de que arrancara ese rescate, antes de que pudiera terminar
    (turno `0f02ec00-9052-4f03-b290-fd444fcbdea4`). Diagnóstico: no es un
    bug nuevo -- es la misma limitación de siempre (el bloque `[OUTPUT
    BUDGET]` es una GUÍA en el prompt, nunca una restricción técnica dura,
    y un motor tipo DOOM con lógica de raycasting es más exigente que
    Isaac o agar.io/tank.io) combinada con el hecho de que patch82 saltaba
    a un número FIJO por nivel (1800 para "Medio") en vez de dar margen
    real por encima del piso estimado.

20. **El salto discreto de patch82 (900→1800 fijo) dejaba los pedidos
    exigentes pegados exacto al techo elevado -- se generaliza a rangos
    por nivel con margen real, no solo un salto a otro número fijo**
    (`patch_orchestrator83`). Tras el retest mixto de DOOM, el usuario
    volvió a pedir explícitamente "un rango... por ejemplo 700 a 1200
    tokens en vez de un techo fijo de 900", esta vez aclarando que lo
    quería "estricto" (sin relajar el límite duro) y que aplicara a los 4
    niveles.

    Diseño (dos multiplicadores separados, cada uno verificado por
    simulación standalone ANTES de tocar el código real -- la primera
    versión que se probó tenía un bug real: usar el MISMO multiplicador
    para decidir qué rango califica y para el techo final dejaba la
    resolución "dentro del mismo nivel" matemáticamente INALCANZABLE,
    detectado por la simulación antes de subir nada al dispositivo, no
    después):
    - Cada nivel pasa de ser un número fijo a un rango
      `[nominal*0.78, nominal*1.33]` -- con nominal=900 para "Bajo" da
      exactamente 700-1200, el ejemplo literal del usuario, aplicado por
      igual a los 4 niveles (`_codegen_range_for_cents`).
    - `_CODEGEN_ESCALATION_SEARCH_MARGIN_MULTIPLIER` (1.1x del piso, chico
      a propósito) decide SOLO qué rango califica -- deja una ventana real
      donde un desborde chico se resuelve ensanchando el rango del MISMO
      nivel (sin cambiar de nombre/centavos): p. ej. piso=1000 en "Bajo"
      (rango 700-1200) usa 1100, sigue siendo "Bajo".
    - Cuando SÍ hace falta subir de nivel, se usa el TECHO COMPLETO del
      rango de destino, no un número a mitad de camino -- no hay motivo
      para contenerse ahí: el costo real depende de los tokens que el
      modelo termine usando de verdad, no del techo, así que dar más aire
      dentro del rango ya elegido nunca cuesta más, solo baja el riesgo de
      pegar contra el techo. Para el caso real de DOOM (piso=1300): el
      rango de "Bajo" (700-1200) no alcanza el objetivo (1430), sube a
      "Medio" y usa su techo COMPLETO (2394) -- más margen real que el
      1800 fijo de antes.

    Sigue siendo "estricto" en el sentido que pidió el usuario: el techo
    más alto posible sigue siendo el máximo del rango de "Extra" (~9600,
    antes 7200 fijo) -- un límite duro, no elástico sin fondo. Si ni ESE
    rango alcanza, no hay elevación posible y el turno sigue cortando a
    $0 exactamente igual que con patch82 (pedidos genuinamente
    descomunales siguen sin intentarse a ciegas). Mismos mensajes de
    transparencia que patch82 (`codegen_budget_tier_elevated` en el WAL,
    aviso visible en el log), con dos variantes de texto ahora --
    "ampliado dentro de 'Bajo'" cuando se resuelve sin cambiar de nivel,
    "elevado a 'Medio'" cuando sí cambia.

    Verificado con una simulación standalone de la lógica final (6 casos,
    incluyendo el caso real de DOOM: 1→2, techo final 2394) antes de tocar
    el archivo real -- los 6 dieron el resultado esperado, incluyendo
    confirmar que la resolución "mismo nivel" ahora SÍ es alcanzable
    (a diferencia del primer intento). `ast.parse` sobre el archivo
    completo (1052342 bytes) y censo de grep: 2 apariciones de
    `patch_orchestrator83`, sin duplicados ni huérfanos. Backup pre-fix
    guardado como `orchestrator.py.bak72`, commit al dispositivo exitoso
    al primer intento. Pendiente de retest en vivo: confirmar que "DOOM
    básico" en "Bajo" ahora aterriza en 2394tok (no 1800) y tiene mejores
    chances de terminar sin pegar contra el techo.

Limitación conocida, no calibrada todavía (ver §10): la clasificación de
"problema" de la Terminal avanzada depende de que `orchestrator.py`
siga usando esos 3 prefijos/palabras clave EXACTOS en sus mensajes de
log -- si algún mensaje nuevo de una red de seguridad futura usa otra
palabra, no se resalta en rojo a menos que se sume a mano a
`_ADV_TERMINAL_PROBLEM_KEYWORDS` (mismo problema de mantenimiento que
`PROBLEM_PHASES` en `sovnode_wal_monitor.py`, pero ahí la clasificación
es sobre el nombre de fase ESTABLE del WAL, no sobre prosa traducida --
más frágil acá).

21. **Turno de reparación ("soluciona los errores que tiene el codigo de
    doom") terminó devolviendo, tal cual, el resultado crudo del último
    `run_cmd` ("OK_SYNTAX") como si fuera la respuesta -- sin haber
    llamado `edit_file`/`write_file` ni una sola vez** (`patch_orchestrator84`,
    3 partes; pedido explícito del usuario tras revisar el log completo del
    turno: "quiero que responda bien que soluciono y si de verdad edito...
    y que identifique donde tiene que poner la linea de codigo y ponerla y
    despues con otra llamada mas pequeña explica lo que metio o reparo o
    hizo"). Diagnóstico completo vía `sovnode.wal` + `sovnode_debug.log`
    (turno_id `60f9f77d-b26f-443d-ab05-99353934368a`, Presupuesto "Bajo"),
    secuencia exacta reconstruida entrada por entrada: pasada 1 `read_file`
    (3773 caracteres, bien); pasada 2 `run_cmd` con un chequeo de sintaxis
    estilo Linux (`cd /root...`) bloqueado por el sandbox -- pasada
    desperdiciada; pasada 3 (la ÚLTIMA con tool-call permitida,
    `MAX_AUTONOMOUS_TOOL_PASSES=3`) reintentó `run_cmd` con sintaxis
    Windows válida → `OK_SYNTAX` (solo confirma que compila, nunca que el
    juego funcione); pasada final forzada (JSON prohibido por diseño,
    `done_reason=stop`, no se cortó por techo) literalmente repitió el
    resultado crudo de la herramienta como respuesta. Confirmado con
    `EmptyResponseDiag`: `raw_tail='```toolresult-run_cmd\nOK_SYNTAX\n```'`.
    Ninguna de las 3 pasadas disponibles se usó para intentar un arreglo
    real -- 2 de 3 se fueron en diagnóstico de sintaxis (el "bucle de run
    cmd" que notó el usuario), y la tercera (forzada) no pudo hacer nada
    mejor que citar el último tool_result.

    Dos huecos de diseño distintos, ambos con causa raíz confirmada en el
    código (no solo en el log):

    - **Hueco A**: el único guardia que empuja hacia `edit_file`
      (`patch_orchestrator72`, bloque `[FILE ACTION REQUIRED]`) solo se
      activa cuando el modelo está por responder con PROSA en vez de
      tool-call -- no hace nada si el modelo elige llamar OTRA
      herramienta de diagnóstico (`run_cmd`/`read_file`/`list_dir`) en
      vez de `edit_file`, que es justo lo que pasó acá dos pasadas
      seguidas.
    - **Hueco B**: `_last_file_op_confirmation` (variable ya existente,
      se setea SOLO cuando un `write_file`/`edit_file` real tiene éxito)
      quedaba en `None` en este turno, pero nada lo chequeaba antes de
      aceptar el texto/eco del modelo como respuesta final -- por eso
      pudo "responder sin error visible" sin haber tocado el archivo
      para nada.

    Fix, en 3 partes, sobre la MISMA función (`run_turn`, bucle de
    herramientas):

    - **Parte B** (la de mayor apalancamiento, un solo `or` agregado):
      `is_last_allowed_pass` ahora también da `True` en cuanto
      `_last_file_op_confirmation` es verdadero (un `write_file`/
      `edit_file` real tuvo éxito, en ESTA pasada o en una anterior).
      Esto reutiliza TODA la maquinaria que ya existía para "última
      pasada" -- `closing_instruction` prohíbe JSON de ahí en más,
      `next_tool_call` nunca se vuelve a fijar, y el techo de la propia
      pasada de cierre ya caía a `FOLLOWUP_DECISION_NUM_PREDICT` (250
      tokens) vía `_file_created_this_turn_path` (que YA se setea para
      `write_file` **y** `edit_file`, no solo archivos nuevos -- una
      pieza que ya estaba pero no garantizaba nada por sí sola). Efecto
      neto exactamente como lo pidió el usuario: la pasada que hace el
      `edit_file`/`write_file` paga el diff completo sin cambios (sigue
      siendo codegen normal); la pasada INMEDIATA siguiente -- antes
      podía volver a pedir otra herramienta o repetir el archivo entero
      como "explicación" -- ahora es SIEMPRE una llamada aparte, chica
      (250 tok) y de solo-explicación. Mismo trade-off que
      `patch_orchestrator79` ya aceptó para `write_file` ("un segundo
      archivo nuevo genuino en el mismo turno ahora requiere un turno de
      seguimiento"), extendido de "no más write_file" a "no más
      tool-calls en absoluto" tras un cambio real.
    - **Parte A** (instrucción de texto, con un trade-off real
      -- confirmado explícitamente con el usuario vía pregunta antes de
      implementar): en la PENÚLTIMA pasada permitida
      (`tool_pass + 1 >= MAX_AUTONOMOUS_TOOL_PASSES`) de un turno de
      modificación (`mod_is_modify`) sin ningún cambio real aplicado
      todavía, se agrega una instrucción sin ambigüedad al
      `closing_instruction`: prohibido pedir `read_file`/`list_dir`/
      `run_cmd` de nuevo -- la próxima llamada TIENE que ser `edit_file`
      (o `write_file` si de verdad hace falta reemplazar todo) con el
      arreglo ya aplicado; y si ningún error concreto apareció todavía
      (como el caso medido: un chequeo de sintaxis que nunca podía
      detectar un bug de lógica/runtime), instruye revisar el código ya
      leído y corregir el bug más probable en vez de declarar "sin
      errores". Es una instrucción de texto, no una restricción dura a
      nivel de schema de herramientas (el modelo técnicamente todavía
      puede ignorarla) -- generalizar `suppress_write_file` (hoy
      `bool`) a un `exclude_tools: Set[str]` real para bloquear
      `read_file`/`list_dir`/`run_cmd` a nivel de API en esta pasada
      quedó evaluado pero fuera de alcance de este patch (toca
      `_call_llm_raw`/`_call_claude_api_raw` y el camino Local, más
      riesgo del que ameritaba un patch de una sola sesión) -- si este
      patrón se vuelve a medir pese a la instrucción de texto, ese es el
      siguiente paso natural.
    - **Parte C** (respaldo determinístico para cuando la Parte A se
      ignora): en el armado final de `raw_response` (cuando la pasada
      termina sin más tool-calls pendientes), si el turno es de
      modificación de archivo y NINGÚN `write_file`/`edit_file` tuvo
      éxito real en ninguna pasada (`not _last_file_op_confirmation`),
      se descarta lo que sea que el modelo haya generado como
      "respuesta" y se arma un mensaje honesto y determinístico en su
      lugar -- mismo patrón ya establecido para "el write_file falló"
      (`file_op_false_success_blocked`) y "se acabó el presupuesto a
      mitad de escribir" (`_toolguard_lost_content_path`), ahora también
      para "se acabaron las pasadas sin haber intentado nada". El
      mensaje cita la última herramienta ejecutada y su resultado
      (recortado a 300 caracteres) para que quede claro QUÉ se revisó, y
      pide el error/comportamiento exacto en vez de dejar que el usuario
      asuma que ya está arreglado. Nueva fase de WAL:
      `modify_turn_ended_without_real_edit`.

    Verificado con simulación de control de flujo sobre el caso real
    (secuencia exacta de la Parte 1) antes de tocar el archivo: con el
    fix, la Parte A dispara en la pasada 2 (donde antes el modelo pidió
    el segundo `run_cmd`); si el modelo la ignora igual y repite
    exactamente la secuencia vieja, la Parte C reemplaza el eco crudo
    final por el mensaje honesto -- en NINGÚN camino posible el turno
    puede terminar pareciendo "resuelto sin error" sin que
    `_last_file_op_confirmation` esté realmente seteado. `ast.parse`
    sobre el archivo completo (1066400 bytes, antes 1052342) y censo de
    grep: 1 aparición de `patch_orchestrator84-A`, 1 de `-B`, 2 de `-C`
    (comentario + variable), sin duplicados ni huérfanos. Backup pre-fix
    guardado como `orchestrator.py.bak73`, commit al dispositivo exitoso
    al primer intento, bytes re-verificados post-commit. Pendiente de
    retest en vivo: repetir "soluciona los errores que tiene el codigo de
    doom" (u otro pedido de reparación real) y confirmar que ahora sí
    llega a `edit_file` en la penúltima pasada, o -- en el peor caso --
    que la respuesta final sea el mensaje honesto de la Parte C en vez del
    eco crudo.

    **Retest en vivo (mismo día, turno_id
    `e68bf945-7f71-45ef-ae71-f1fa918cdb00`, "mejora el codigo de doom en
    el workspace"): confirmado -- patch84 funcionó.** 2 `edit_file`
    reales aplicados (3744→4162 bytes, 4 ediciones), explicación final
    coherente sobre lo agregado (vida, daño, HUD, condición de fin), sin
    ningún eco crudo ni "loop de run_cmd". El turno costó $0.0633 —
    similar al turno roto original — pero esta vez por un motivo
    DISTINTO y ya identificado (ver ítem 23, `patch_orchestrator86`): no
    es que no se intentara el arreglo, es que el primer `edit_file`
    falló por `old_str` no coincidente y hubo que reintentar. Ver ítem 23
    para el fix de esto.

22. **Los selectores de "Presupuesto de Sonnet por turno" en la UI
    (`sovnode_qt.py`) seguían mostrando el techo fijo viejo ("Bajo —
    ~900 tok", etc.) después de que patch_orchestrator83 lo volviera un
    RANGO elástico por tier** (pedido explícito del usuario: "quitale a
    los esfuerzos el indicativo de los tokens porque ya no esta ese
    techo"). Fix cosmético, sin lógica: se sacó el número de tokens fijo
    de las 4 etiquetas (`cloud_budget_option_1c/2c/4c/8c`), ES y EN,
    dejando solo el nombre del tier y la descripción breve ("Bajo —
    respuestas cortas", etc.) -- nada más en el archivo mencionaba un
    conteo de tokens fijo (verificado con grep, cero apariciones
    restantes de "900 tok"/"1800 tok"/etc.). `ast.parse` OK (353443
    bytes, antes 353537). Backup `sovnode_qt.py.bak1_pre_patch85_label`.
    Commit al dispositivo: el primer intento devolvió
    `{"written":[...],"rejected":[]}` pero el `device_stage_files` de
    verificación mostró el tamaño VIEJO sin cambios -- el mismo bug de
    mtime stale ya documentado más abajo en este documento (ver la nota
    sobre `device_commit_files`). Reintentado con `force: true`,
    confirmado con bytes correctos en la segunda verificación.

23. **El primer `edit_file` de un turno de modificación falla con
    frecuencia notable por `old_str` que no matchea EXACTO -- cada
    intento fallido cuesta lo mismo en tokens que uno exitoso, porque el
    modelo ya compuso el diff completo antes de que `edit_file_safely`
    lo rechace** (`patch_orchestrator86`; investigado a pedido del
    usuario tras el retest de patch84 de arriba: "porque cobro tanto?").
    Medido en el mismo turno `e68bf945-7f71-45ef-ae71-f1fa918cdb00`: la
    pasada 1 (`edit_file`, 1035 tokens de salida, $0.0307 -- casi la
    mitad del costo total del turno) falló con `[SANDBOX WRITE ERROR]`
    (chars=130 en el WAL; el texto exacto no quedó registrado -- el WAL
    solo guarda longitud de `tool_result`, no el contenido, y a
    diferencia de `run_cmd` no hay un log dedicado tipo "EditFileResult"
    para `write_file`/`edit_file` -- limitación de observabilidad
    anotada acá para la próxima sesión, no resuelta todavía). Descartado
    "contenido viejo" como causa: `_current_file_context` ya inyecta el
    contenido REAL y completo de `doom.py` (3744 bytes, sin recortar) en
    el prompt de la primera generación -- el modelo tenía el texto
    correcto delante y aun así no lo citó exacto (mismo problema
    conocido de los LLM para reproducir texto carácter por carácter,
    especialmente indentación/espacios, incluso con el original a la
    vista). La instrucción existente sobre `old_str` (tanto en
    `_current_file_context` como en el header general) solo pedía que
    fuera "chico y ÚNICO" -- nunca decía explícitamente "citá el texto
    EXACTO, no lo reescribas de memoria". Fix: se agregó esa instrucción
    -- carácter por carácter, misma indentación/espacios/saltos de
    línea, "copialo, no lo reescribas" -- pegada al bloque de contenido
    real en `_current_file_context` (ES y EN), en vez de en el header
    general (que no siempre tiene contenido visible delante, así que ahí
    sería menos preciso). No es una garantía dura -- sigue siendo una
    instrucción de texto, el modelo puede seguir fallando el match
    ocasionalmente -- pero apunta al motivo real medido (fidelidad de la
    cita, no unicidad, que ya se cumplía) en vez de repetir una
    instrucción que no cubría el problema. El respaldo existente
    (patch_orchestrator57, guía de reintento dirigido tras un
    `[SANDBOX WRITE ERROR]`) sigue siendo la red de seguridad si aun así
    falla. `ast.parse` OK (1069321 bytes, antes 1066400), 1 aparición de
    `patch_orchestrator86`, sin duplicados. Backup
    `orchestrator.py.bak74_pre_patch86`, commit al dispositivo exitoso
    al primer intento, bytes re-verificados. Pendiente de retest en
    vivo: repetir un pedido de modificación real y ver si el primer
    `edit_file` tiene más chances de aplicarse sin necesitar reintento.

    **Retest en vivo (turnos `mejora doom` x3 seguidos, mismo día):
    patch84 y patch86 confirmados funcionando a la perfección** -- los
    3 turnos resolvieron en 1 solo `edit_file` cada uno (cero
    reintentos), seguidos de la pasada chica de cierre con 4796 tokens
    de caché reusados dentro del mismo turno. $0.15 por 3 mejoras
    reales, contra ~$0.06 por UN turno que antes no arreglaba nada.

24. **Investigado a pedido del usuario ("si podemos aumentar el
    rendimiento y eficiencia") por qué, en esa misma sesión de 3
    turnos, la llamada GRANDE inicial de CADA turno paga el input
    completo (~10000 tok) sin ningún hit de caché, mientras que la
    pasada chica de cierre DENTRO de cada turno sí reusa 4796 tokens
    cacheados -- sin explicación evidente, porque `gen_system` es
    demostrablemente el mismo string en los 3 turnos.** Se rastreó a
    fondo antes de tocar nada: `_get_file_ops_system_prompt` está
    cacheado en memoria de proceso (`_frozen_system_headers`, clave
    `("__file_ops__", lang)`) y no interpola nada turno-específico;
    `_write_file_suppression_prompt_override` (el texto que se le suma
    cuando `_suppress_write_file_for_turn` es `True`, el caso de los 3
    turnos sobre `doom.py` ya existente) tampoco interpola nada --
    ambos deberían dar exactamente el mismo `gen_system`, byte a byte,
    en los 3 turnos. `_stream_claude_api_raw` (el camino real usado
    para la generación inicial, "LeanSingle") arma el `cache_control`
    igual que la variante no-streaming, y sí parsea
    `cache_read_input_tokens`/`cache_creation_input_tokens` del evento
    `message_start` correctamente. No se pudo confirmar ni descartar la
    causa real de raíz porque el propio log de `_account_cloud_usage`
    tenía un hueco: `cache_note` solo se arma a partir de
    `cache_read_tokens` -- si una llamada ESCRIBE caché nuevo
    (`cache_creation_tokens > 0`) pero no encuentra nada que LEER
    (`cache_read_tokens == 0`), el mensaje queda indistinguible de una
    llamada que nunca intentó cachear. Dos causas raíz posibles, con
    arreglos completamente distintos, y hoy no hay forma de saber cuál
    de las dos es sin ese dato (`patch_orchestrator87`, solo logging,
    cero cambio de comportamiento ni de costo real): se agregó
    "N nuevos en caché" al mensaje cuando `cache_creation_tokens > 0`,
    además de renombrar "N de caché" a "N leídos de caché" para no
    confundir ambos números. `ast.parse` OK (1071265 bytes, antes
    1069321). Backup `orchestrator.py.bak75_pre_patch87`, commit
    exitoso al primer intento, bytes re-verificados. Pendiente de
    retest en vivo: repetir varios turnos de modificación seguidos
    sobre el mismo archivo y mirar si la llamada grande inicial de cada
    turno (después de la primera) ahora muestra "N nuevos en caché"
    (confirmaría que SÍ está cacheando pero nunca logra reusar entre
    turnos -- el problema sería reutilización, no ausencia de caché) o
    ningún indicador de caché en absoluto (indicaría que esas llamadas
    no están cacheando de ninguna forma, un problema distinto y más
    profundo). Recién con esa evidencia tiene sentido proponer un fix
    de fondo -- no antes.

25. **Evidencia recolectada (ítem 24) y fix de fondo aplicado en la
    misma sesión (`patch_orchestrator88`) -- pedido explícito del
    usuario tras confirmar que el caching entre turnos funcionaba
    ("verdaderamente impresionante ya funciona solo hay que
    optimizarlo").** Turno de prueba real (clon de agar.io, 3 turnos
    seguidos, `patch_orchestrator87` ya activo para poder leer el
    detalle): turno 1, pasada inicial -- `6549 in (4695 nuevos en
    caché) / 1505 out`; la pasada de cierre DEL MISMO TURNO, ms
    después, mismo `gen_system` (ver `system_override=gen_system` en
    el llamado a `_call_llm_raw` del `followup` del bucle de
    herramientas) -- `6300 in (1277 leídos de caché, 3212 nuevos en
    caché) / 80 out`: debería haber leído los 4695 completos, pero
    solo reaprovechó 1277. Turnos 2 y 3 SÍ mostraron el hit completo
    entre turnos (`4796 leídos de caché` en la pasada inicial de
    ambos) -- confirmando que el mecanismo de caching entre turnos
    funciona bien; el problema medido es puntual, DENTRO de un mismo
    turno, en la transición entre la pasada que escribe el archivo y
    la que redacta la explicación.

    Causa raíz identificada por lectura de código (no solo
    inferencia): `CLOUD_TOOLS_SCHEMA` tenía a `write_file` como
    PRIMERA entrada de la lista, y es la ÚNICA herramienta que
    `_cloud_tools_schema_for_prompt(exclude_tools=...)` llega a sacar
    de esa lista (`{"write_file"} if suppress_write_file else None`,
    ver los dos call sites en `_call_claude_api_raw`/
    `_stream_claude_api_raw` y el tercero en el `followup` del bucle,
    éste vía `_file_created_this_turn_path`). El prompt caching de
    Anthropic es por PREFIJO: en cuanto la pasada de cierre excluye
    `write_file` (porque el archivo ya se escribió en una pasada
    anterior de ese mismo turno -- ver patch_orchestrator79), TODO lo
    que en la llamada anterior venía después de `write_file` (el
    resto de herramientas + el bloque `system` completo) queda en una
    posición distinta, y el único `cache_control` que existía
    (anclado en `system_telemetry`, la última entrada) deja de
    matchear -- se pierde el checkpoint completo, no solo el tamaño
    de `write_file`.

    Fix: `write_file` se reubicó al FINAL de `CLOUD_TOOLS_SCHEMA`
    (justo antes de `system_telemetry`, la única otra entrada
    filtrable) y se agregó un SEGUNDO `cache_control` en `run_cmd`
    -- ahora la última herramienta que NUNCA se excluye. Con dos
    checkpoints en vez de uno, el prefijo estable
    (`edit_file`+`read_file`+`list_dir`+`run_cmd`) sigue sirviéndose
    de caché aunque `write_file` desaparezca de la cola más adelante;
    como mucho se vuelve a pagar la propia entrada de `write_file`
    (cuando está) más lo que la siga. Mismo set de herramientas,
    mismo contenido de cada una, cero cambio de comportamiento del
    modelo -- solo reordenadas, así que es un cambio de costo/latencia
    sin riesgo funcional nuevo (no toca `exclude_tools`, no toca qué
    se ofrece ni cuándo, solo el ORDEN). `ast.parse` OK (1073853
    bytes, antes 1071265), 1 sola aparición de `"name": "write_file"`
    (sin duplicados por el corte/pegado) y 4 apariciones de
    `"cache_control": {"type": "ephemeral"}` (las 2 de siempre sobre
    el bloque `system` de `_call_claude_api_raw`/
    `_stream_claude_api_raw`, más la nueva de `run_cmd` y la ya
    existente de `system_telemetry`). Backup
    `orchestrator.py.bak73_pre_patch88`, commit al dispositivo exitoso
    al primer intento (dos archivos, verificados por bytes ambos).
    Pendiente de retest en vivo: repetir un turno donde el modelo
    escriba un archivo y luego necesite otra pasada de cierre sobre
    el MISMO turno, y confirmar que esa segunda pasada ahora muestra
    "N leídos de caché" cercano al total de la primera en vez del
    split parcial (1277/3212) medido acá.

**2026-09-18 -- fix real encontrado usando la Terminal avanzada:
rescate local para write_file truncado sin archivo previo
(`patch_orchestrator65`).** Motivado directamente por un turno real que
el usuario capturó con la Terminal avanzada (2 capturas de pantalla) y
pidió investigar: pedido "crea un juego de tank io basico en el
workspace", $0.0527 gastados, 3 llamadas a Cloud, 4 "problemas"
marcados, resultado final "no se guardó nada, así que el archivo sigue
exactamente como estaba antes de este mensaje" -- exactamente el tipo
de fallo que esta sesión llevaba dos días intentando hacer visible, y
ahora por fin visible EN EL MOMENTO en vez de reconstruido después.

Diagnóstico (WAL + `sovnode_debug.log`, filtrando por
`turn_id=250e68ae-5386-4b52-a2e3-045e07cb41c1`): las 3 pasadas de
`write_file` sobre el mismo turno tocaron el techo de presupuesto
Cloud (`file_write_ceiling_regen` / `..._inloop`) y cada una disparó
`_continue_truncated_file_write_locally` para rescatar en Local -- pero
las 3 rescataron sobre `content=""`, es decir la API de Claude cortó el
`tool_use` de `write_file` ANTES de emitir un solo carácter del campo
`content` (el streaming de un `tool_use` manda `path` primero y
`content` después; si `max_tokens` se agota entre medio, el campo llega
vacío/ausente, no parcial -- mecánica distinta al motor Local, que sí
deja prosa parcial recuperable). Las líneas `[FileRescueDiag]` del log
confirman `existing_len=0` en las 3 pasadas: nunca hubo un archivo
previo en disco del que partir. La función de rescate YA tenía una
rama para "content vacío + SÍ hay archivo previo que actualizar"
(`if _diag_existing and user_input:`), pero ninguna rama para "content
vacío + NO hay archivo previo" -- ese hueco exacto es el que dejaba
caer las 3 pasadas al `return tool_call` final sin hacer nada, y por
eso el turno terminaba gastando 3 llamadas a Cloud sin escribir una
sola línea.

Fix, acotado a ese hueco: nueva rama `elif user_input:` en
`_continue_truncated_file_write_locally` (después de la rama de
"archivo existente", antes del `return tool_call` final) que dispara
una generación Local DESDE CERO (`_call_llm(..., force_local=True,
num_predict_override=MemoryGovernor.codegen_num_predict(),
perf_label="LocalFreshWrite")`) pidiéndole al modelo Local el archivo
completo a partir del pedido original del usuario -- sin nada previo
que preservar, no tiene sentido intentar "continuar" nada, hay que
escribirlo entero. Igual que el resto de los rescates de este archivo,
valida el resultado antes de usarlo (`_looks_like_partial_stub` +
mínimo de 40 caracteres) antes de devolver el `tool_call` con el
`content` rescatado; si la generación Local también falla o da un stub,
cae al `return tool_call` original sin inventar un "éxito" falso. Los
dos call-sites de `_continue_truncated_file_write_locally` (el
pre-bucle en `run_turn` y el que corre dentro del bucle de
herramientas, que es justo donde fallaron las pasadas 2 y 3 del turno
de "tank io") YA pasaban `user_input=user_input` desde antes -- no hizo
falta tocarlos, alcanzó con la rama nueva adentro de la función. No
toca `_call_claude_api_raw`/`_stream_claude_api_raw` (el rescate corre
100% en Local, sin costo extra de API). Verificado con `ast.parse`
antes de comitear; backup del archivo pre-fix guardado como
`orchestrator.py.bak54`.

26. **Bug real encontrado investigando un cuelgue en vivo que el
    usuario terminó cancelando (`patch_orchestrator89`) -- turno "añade
    cosas al codigo agar io y explica que añadiste" sobre
    `agario_basico.py` (ya existente, 3 mejoras previas aplicadas en la
    misma sesión).** Capturas de UI + `sovnode.wal`
    (`turn_id=2b62f232-326f-4edd-9a27-6db0863b8657`): el turno cayó en
    un ciclo de `file_write_ceiling_local_continuation_inloop` (rescate
    en Local, ~2 minutos cada uno) DOS veces seguidas sin terminar
    nunca, hasta que el usuario canceló la generación a mano
    ("lo termine cancelando porque es claro el problema").

    Diagnóstico, leyendo código en vez de solo inferir del log:
    `codegen_budget_plan` de este turno mostró `hard_ceiling: 900`
    (el plano de "Bajo") pese a modificar un archivo de ~3193
    caracteres -- los 3 turnos anteriores sobre el MISMO archivo
    habían dado 2294-2555 tokens, escalando con el tamaño real (ver
    `_effective_codegen_num_predict`/`_dynamic_edit_budget_tokens`).
    Causa raíz: `_resolve_modify_target` nunca llegó a resolver
    `mod_target` para este pedido. `_match_workspace_file_by_phrase`
    no matcheaba ("agar io", con espacio, no es substring de
    "agario"), así que la única vía que quedaba era el fallback a
    `_last_conversational_file_target` (patch76) -- pero ese fallback
    exige `not self._NON_FILE_TOPIC_RE.search(user_input)`, y el
    regex incluía `\bexplica(?:me|ci[oó]n)?\b` (verbo SUELTO), que
    matcheaba la cláusula coordinada "y explica que añadiste" del
    mismo pedido -- pensado para bloquear casos como "mejora ESA
    EXPLICACIÓN" (donde "explicación" es sustantivo, objeto del verbo
    modificador), pero demasiado amplio: bloqueaba cualquier pedido de
    archivo que además pidiera una explicación de cierre, un patrón de
    uso común. Con `mod_target=None` -> `mod_is_modify=False`, el
    bloque que lee el contenido actual del archivo
    (`_modify_target_current_content`) nunca corrió, así que ni se
    inyectó el archivo en el prompt (el modelo tuvo que pedir
    `read_file` él mismo) ni el techo de salida escaló con su tamaño
    real -- quedó en el plano "Bajo" de 900 tokens, insuficiente para
    reescribir con `write_file` un archivo de ese tamaño, de ahí el
    ciclo de truncado + rescate Local repetido.

    Fix: se saca `\bexplica(?:me|ci[oó]n)?\b` de `_NON_FILE_TOPIC_RE`
    -- el caso que de verdad hacía falta cubrir ("esa explicación",
    "mejora la explicación") sigue cubierto por `\bexplicaci[oó]n\b`
    (el sustantivo, ya estaba en la lista sin tocar). Mismo criterio
    que ya usaba la lista en inglés, que nunca tuvo un verbo suelto
    equivalente (solo `\bexplanation\b`) -- esa asimetría entre
    idiomas era la pista de que el verbo suelto en español sobraba.
    Verificado con un script standalone reproduciendo el regex antes/
    después contra 4 casos (el pedido real que falló, "mejora esa
    explicación", "corrige el razonamiento...", "explicame qué hiciste
    en el archivo") -- los 3 primeros se comportan como se esperaba
    (el real ya no bloquea, los dos de guarda siguen bloqueando o
    dejan de hacerlo de forma aceptable). `ast.parse` OK (1076370
    bytes, antes 1073853). Backup `orchestrator.py.bak74_pre_patch89`,
    commit al dispositivo exitoso al primer intento, bytes
    re-verificados. Pendiente de retest en vivo: repetir el mismo
    pedido ("añade cosas al codigo X y explica qué añadiste" sobre un
    archivo ya existente) y confirmar que ya no dispara
    `file_write_ceiling_local_continuation_inloop` -- que el techo de
    `codegen_budget_plan` escale con el tamaño real del archivo en vez
    de quedar en 900tok.

27. **Auditoría preventiva de listas de palabras fijas (`patch_orchestrator90`,
    `91`, `92`) -- pedido explícito del usuario tras patch89: "eso
    significa que hay muchas palabras que no estan ahi y que deberian
    estarlo no?".** SovNode clasifica intención casi enteramente con
    regex de listas de palabras curadas a mano (más de 80 en
    `orchestrator.py`) -- confirmado que es un patrón recurrente de bugs,
    no uno aislado (26 menciones de "no reconocía"/"no matcheaba"/"no
    estaba en la lista" ya en este mismo documento antes de esta
    entrada). Se auditaron las 3 listas con más impacto directo sobre
    qué archivo se modifica (la categoría de bug que motivó patch89):

    - **`patch_orchestrator90`** (`_match_workspace_file_by_phrase`,
      único llamador de peso: `_resolve_modify_target` y
      `_has_file_write_intent`): el chequeo original exige la frase
      COMPLETA del nombre de archivo ("agario basico") TEXTUAL y
      seguida en el pedido -- el usuario casi nunca la repite así. Se
      agregan dos fallbacks EN ORDEN, solo si el match exacto no
      encuentra nada: (1) mismo chequeo con espacios/puntuación común
      sacados de ambos lados (nueva `_squish_for_phrase_match`) --
      cubre "agar.io"/"agar io" contra el archivo `agario_basico.py`;
      (2) si tras sacar calificadores genéricos (nueva
      `_GENERIC_FILENAME_QUALIFIER_WORDS`: basico/simple/demo/etc.)
      queda EXACTAMENTE una palabra distintiva (>= 4 caracteres),
      alcanza con que ESA sola palabra aparezca (mismo chequeo sin
      espacios). Sigue exigiendo unicidad (`len(matches) == 1`) como
      única red de seguridad -- ese criterio no se tocó. Verificado con
      un script standalone (6 casos, incluido el pedido real que
      falló) antes de aplicar. `ast.parse` OK (1080269 bytes, antes
      1073853). Backup `orchestrator.py.bak75_pre_patch90`.

    - **`patch_orchestrator91`** (`_WRITE_ARTIFACT_NOUN`, la lista real
      que usa `_FILE_WRITE_INTENT_RE` para reconocer "hazme un juego de
      X" sin más contexto -- `_RUNNABLE_PROGRAM_NOUN_RE`, una lista
      hermana, no aparece referenciada con `.search(` en ningún lugar
      del archivo, posible remanente muerto de una iteración anterior,
      actualizada igual por consistencia sin riesgo): ni "doom" ni
      "agario" estaban en la lista pese a haberse usado constantemente
      en esta misma sesión (`doom.py`, `agario_basico.py`) -- un pedido
      limpio como "hazme un doom sencillo" (sin "juego"/"workspace")
      habría dado `_has_file_write_intent=False`, el mismo bug de fondo
      ya documentado para "snake" (BLINDAJE 2026-09-09). Se suman
      doom/agario/agar.io/mario/minecraft/buscaminas/ajedrez/asteroids/
      invaders -- títulos/géneros sin ambigüedad real fuera de contexto
      de código, y que de todas formas exigen un verbo de escritura
      cerca (`_FILE_WRITE_VERB_INNER`) para disparar algo. Verificado
      con script standalone (4 casos) antes/después. `ast.parse` OK
      (1082050 bytes, antes 1080269). Backup
      `orchestrator.py.bak76_pre_patch91`.

    - **`patch_orchestrator92`** (`_FILE_MODIFY_VERB_RE`, gatea
      `mod_is_modify` y el fallback a `_last_conversational_file_target`
      de patch76): agrega dos categorías, evaluadas por separado por
      riesgo de falso positivo -- (a) verbos NO sobrecargados en charla
      cotidiana argentina (ajustar/retocar/pulir/ampliar/expandir/
      completar) como raíz suelta, igual que el resto de la lista; (b)
      formas coloquiales con pronombre pegado (ponele/sumale/metele/
      hacele y variantes) SOLO para los verbos que sí son genéricos
      ("poner"/"sumar"/"meter"/"hacer") -- deliberadamente NUNCA la raíz
      sola ("pon"/"hac"), porque esa raíz aparece todo el tiempo en
      charla no relacionada con archivos y el fallback de
      `_last_conversational_file_target` podría secuestrar turnos sin
      relación. Verificado extrayendo el regex REAL del archivo ya
      parcheado (no una copia reescrita a mano) y corriéndolo contra 16
      casos (10 positivos, incluido "ponele un power-up nuevo al
      juego"; 5 negativos de control, incluido "no sé qué hacer con mi
      vida"; y el pedido real de patch89) -- los 16 se comportaron como
      se esperaba. `ast.parse` OK (1083736 bytes, antes 1082050).
      Backup `orchestrator.py.bak77_pre_patch92`.

    Las tres siguen el mismo criterio ya establecido en esta sesión
    ("preferir un falso negativo antes que inflar el riesgo de falso
    positivo") -- ninguna afloja una guarda existente, todas SUMAN
    cobertura nueva. Commits al dispositivo exitosos al primer intento
    en los 3 casos, bytes re-verificados. Pendiente de retest en vivo:
    repetir un pedido de creación limpio con "doom"/"agario" sin
    "juego"/"workspace" cerca, un seguimiento con "ponele"/"sumale"/
    "hacele" sobre un archivo ya existente, y una referencia con
    espacio/punto distinto al nombre real del archivo ("agar io"/
    "agar.io") -- confirmar que las tres resuelven al archivo/intención
    correctos en vez de perderse como el turno de patch89.

28. **Dos hallazgos reales reportados por el usuario mirando una captura de
    consola grabada para un video de showcase, con la UI cambiada a
    inglés a mitad de sesión** (`[16:37:06] Language changed to:
    English`): (a) varias líneas de la consola/UI seguían en español
    pese al cambio de idioma, y (b) al abrir el build empaquetado en
    `dist/` la API key de Cloud ya aparecía cargada, y el usuario pidió
    "que no se guarde mi api de claude" -- investigado y se confirmó que
    NO es una fuga a otros archivos/usuarios: `QSettings("SovNode",
    "SovNode")` en `sovnode_qt.py` usa el formato nativo de Qt en
    Windows (Registro, bajo el usuario de Windows actual), compartido
    por cualquier build/copia de la app corrida en esa misma cuenta --
    pero sí se confirmó que hoy no hay forma de usar la key sin que
    quede guardada (`cloud_key_input.editingFinished` dispara
    `settings.setValue("cloud/api_key", ...)` sin pedir confirmación ni
    ofrecer opt-out). Decisión del usuario (vía pregunta explícita):
    mantener el guardado automático (más cómodo siendo el único usuario
    de ese equipo), pero agregar un botón para borrarla.

    - **Idioma (`patch_orchestrator93` + `patch_qt65`)**: alcance
      acotado a lo VISIBLE en el panel de consola/chat (no auditoría
      completa del codebase, decisión explícita del usuario por tener
      la grabación cerca). Se encontraron tres causas distintas, no una
      sola:
      1. `_account_cloud_usage` (orchestrator.py, ~L14996) armaba el
         resumen `☁ Claude API (...): N in (X leídos de caché, Y nuevos
         en caché) / Z out tok — $costo (total sesión: $total)` sin
         NINGUNA rama de idioma -- a diferencia de casi todo el resto
         del archivo, que ya usa el patrón `is_en = getattr(self,
         "current_language", "Spanish") == "English"`. Se le agregó esa
         misma rama. Backup `orchestrator.py.bak78_pre_patch93`
         (1083736→1085013 bytes).
      2. Tres líneas de log en `sovnode_qt.py` (dentro de la búsqueda
         Wikipedia embebida ahí mismo, no en `src/tools/web_search.py`):
         `"[WEB_SEARCH] Consultando Wikipedia (dato puntual/respaldo)..."`
         (~L644) y dos ocurrencias idénticas de
         `"[WEB_SEARCH] Wikipedia sin fuente específica.../sin resultado
         útil..."` (~L771 y ~L943, mismo bloque duplicado dos veces en la
         función) nunca tuvieron rama de idioma, a diferencia de
         `search_web()`/`search_topic_images()` (que sí reciben `lang=`
         y arman el mensaje ya traducido -- por eso ESAS líneas sí
         aparecían en inglés en la captura del usuario, mezcladas con
         estas otras tres que no). Se les agregó rama usando
         `resolved_lang`, ya en scope en el mismo punto (se usa dos
         líneas después para elegir `wiki_domain`). Existía además una
         tabla de traducción por regex (`_WEB_SEARCH_LOG_TRANSLATIONS` /
         `_translate_web_search_log`, ~L470-550) pensada aparentemente
         para resolver este mismo problema de forma genérica -- pero
         **nunca se llama desde ningún lado del archivo** (código
         muerto, confirmado por grep). No se activó ni se depende de
         ella en este fix; queda documentada acá por si una sesión
         futura la encuentra y asume que está en uso.
      3. Tres widgets del panel de consola creados una sola vez en
         `_create_ui()` con texto hardcodeado en español y nunca
         agregados a `_on_lang_changed` (la función que sí re-traduce
         otros widgets estáticos al cambiar idioma -- tiene un
         comentario `BLINDAJE (bug real reportado)` de un fix anterior
         para ese mismo patrón, aplicado a OTRO grupo de widgets, que
         se olvidó de incluir estos tres): el título `QLabel("🖥️
         CONSOLA DE SISTEMA / LOGS")`, el botón `QPushButton("Limpiar")`
         y el checkbox `chk_advanced_terminal` (este último SÍ tenía
         entrada en `I18N` para ambos idiomas, solo le faltaba la
         llamada `.setText()`/`.setToolTip()` en `_on_lang_changed`).
         Se guardaron título y botón como `self.term_title`/
         `self.btn_clear_term` (antes variables locales, no
         referenciables después), se agregaron las claves `I18N`
         `terminal_console_title`/`btn_clear_terminal` en ambos idiomas,
         y se sumaron los tres al bloque de re-traducción existente.
         Backup `sovnode_qt.py.bak13_pre_patch_qt65` (353443→359734
         bytes, incluye también el fix de API key de abajo).

    - **API key (`patch_qt65`, mismo commit)**: se agregó un botón
      "Olvidar key guardada" / "Forget saved key" (`btn_forget_cloud_key`,
      junto a "Probar conexión") que llama a
      `_on_forget_cloud_key_clicked`: `settings.remove("cloud/api_key")`
      sobre el mismo `QSettings("SovNode","SovNode")`, limpia
      `cloud_key_input` y reaplica `set_cloud_backend(..., api_key=None)`
      para que tampoco quede la key vieja en memoria durante la sesión
      actual. No cambia el guardado automático existente (decisión
      explícita del usuario), no toca el resto de la config (presupuesto
      Cloud, etc.). El botón y su tooltip también se agregaron a
      `_on_lang_changed`.

    Ambos archivos verificados con `ast.parse` en el sandbox y de nuevo
    con el Python del propio dispositivo (3.10.12) después de comitear,
    tamaños re-verificados byte a byte en ambos lados. Commit al
    dispositivo exitoso al primer intento (backups + archivos
    principales, 4 archivos en un solo `device_commit_files`). Pendiente
    de retest en vivo: repetir un turno con la UI en inglés y confirmar
    que la consola queda 100% en inglés (contadores de caché incluidos),
    y probar el botón "Forget saved key" seguido de reabrir la app para
    confirmar que el campo queda vacío.

29. **`patch_orchestrator94` + `patch_router1` (2026-09-18) — bug real,
    MEDIDO por el usuario en una grabación de showcase**: el turno
    "create a playable asteroids with a ship, shooting, and score in the
    workspace" disparó una búsqueda web completa (DuckDuckGo + Wikipedia,
    ~2.5-4s de timeouts de scraping) que devolvió fuentes sin ninguna
    relación ("List of Google Easter eggs", "Space Invaders") -- el
    propio modelo lo notó y las ignoró ("Note: the retrieved sources are
    unrelated to this request... so they weren't used"), pero el turno
    igual pagó el costo completo de la búsqueda para nada. El usuario
    interpretó esto (y la consola del ítem 28 mezclada en la misma
    sesión) como "bugs que pasan cuando se manda en inglés" y pidió
    identificar la causa.

    Causa real, confirmada leyendo `router.py` (no es un problema de
    idioma en sí, aunque el caso concreto ocurrió en inglés):
    `SignalTag.CODE_COMPLEX` y `SignalTag.WEB_SEARCH_INTENT` tienen
    exclusión mutua a propósito (`_should_force_web_search`/
    `IntentRouter.classify`, ver docstrings ahí) -- un pedido de
    desarrollo de juego NUNCA debería disparar búsqueda web. Pero
    `_CODE_COMPLEX_PATTERN` (router.py) es, según su propio comentario
    ya existente, un ESPEJO manual de
    `Orchestrator._RUNNABLE_PROGRAM_NOUN_RE`/`_WRITE_ARTIFACT_NOUN`
    (orchestrator.py) -- las dos listas de "nombres de juego que cuentan
    como código sin ambigüedad" tienen que coincidir o divergen. Y
    divergieron: el ítem 27 de este documento (`patch_orchestrator91`,
    sesión anterior, mismo día) agregó `doom|agario|agar.io|mario|
    minecraft|buscaminas|minesweeper|ajedrez|chess|asteroids?|invaders?`
    a las dos listas de orchestrator.py, pero nadie las volvió a copiar a
    router.py -- quedó con la lista vieja (`snake|tetris|pong|pac-man|
    breakout|arkanoid|2048|flappy`, sin "asteroids"), así que
    `_CODE_COMPLEX_PATTERN` no matcheaba, `WEB_SEARCH_INTENT` quedaba sin
    neutralizar, y el turno buscaba en la web como si "asteroids" fuera
    una consulta factual cualquiera. Mismo patrón EXACTO al bug ya
    documentado en el comentario BLINDAJE de esa misma función (snake/
    pong/tetris, 2026-09-09) -- la lección de "mantener las dos listas
    sincronizadas" ya estaba escrita ahí mismo y no se siguió al hacer
    el patch91.

    Al revisar el espejo se encontró además que NINGUNA de las dos
    listas tenía la forma en español de "asteroids"/"invaders"
    ("asteroides"/"invasores") -- confirmado con `crea un asteroides
    jugable` fallando el mismo patrón antes del fix. No es que el bug
    sea específico de inglés: es que la palabra que faltaba esta vez
    resultó ser la forma en inglés, pero el hueco en español ya estaba
    ahí también, sin haber sido ejercitado todavía. Se agregó
    `asteroides`/`invasores` a las tres listas (las dos de
    orchestrator.py + la de router.py) para cerrar ambos huecos de una.

    Verificado con un script standalone (4 casos: la frase real que
    falló, su equivalente en español, y dos variantes de "invaders" en
    cada idioma) -- las 4 matchean después del fix, ninguna antes.
    `ast.parse` OK en los dos archivos. Backups
    `orchestrator.py.bak79_pre_patch94` (1085013→1085055 bytes) y
    `router.py.bak1_pre_patch_router1` (45277→46553 bytes, primera vez
    que se toca este archivo en esta sesión). Commit al dispositivo
    exitoso al primer intento, bytes re-verificados con el Python del
    propio dispositivo. Pendiente de retest en vivo: repetir el turno
    real de asteroids en inglés y confirmar que NO dispara búsqueda web
    esta vez (router=slow_path por otras señales está bien, lo que no
    debe pasar es el paso de WEB_SEARCH); repetir el equivalente en
    español ("crea un asteroides jugable") para confirmar el mismo
    resultado. Nota aparte para la próxima sesión que toque cualquiera
    de estas dos listas de nombres de juego: **son DOS copias
    independientes en DOS archivos, actualizar una sin la otra
    reintroduce este mismo bug** -- valdría la pena, en algún momento,
    extraerla a una sola constante compartida importada por ambos
    módulos en vez de seguir confiando en que el próximo patch se
    acuerde de espejarla a mano.

30. **`patch_orchestrator95` (2026-09-18) — bug real, MEDIDO**: turno
    "improve that code and tell me what you implemented" sobre
    `minesweeper.py` (3340 bytes) devolvió un `edit_file` de apenas 9
    bytes de diferencia pese a reportar "Done" -- el usuario preguntó
    directamente por qué no podía completar ese pedido en inglés.
    Diagnóstico: no es un bug de idioma (no se encontró ninguna
    ramificación por idioma en el camino de `edit_file`/budget), es un
    techo de tokens demasiado ajustado. Con el `_EDIT_FILE_BUDGET_
    MULTIPLIER` viejo (2.75x) el techo dinámico para ese archivo daba
    2786 tokens; la pasada de Sonnet gastó exactamente esos 2786 tokens
    (`done_reason == "length"`, corte a mitad de generación) -- $0.0426
    pagados por un JSON de `edit_file` truncado que se tuvo que
    descartar entero por no ser seguro de empalmar. `_rescue_truncated_
    edit_file_locally` reintentó la misma pasada en Ollama local (sin
    costo, presupuesto grande) pero, al ser un modelo mucho más chico,
    no reprodujo la mejora ambiciosa que Sonnet había arrancado a
    escribir -- devolvió un cambio casi nulo, reportado como "Done" sin
    avisar de la degradación. Se le presentaron al usuario 3 opciones
    (subir el techo / avisar explícitamente cuando el rescate local
    produce un resultado mucho más chico que lo truncado / investigar si
    había alguna causa específica de inglés) y eligió la primera, con la
    instrucción explícita de que el corte sea "lo menos frecuente
    posible".

    Fix: `_EDIT_FILE_BUDGET_MULTIPLIER` subido de 2.75x a 4.0x, con la
    misma lógica de margen de crecimiento que ya usa
    `_dynamic_write_budget_tokens` (ítem previo de esta sección)
    aplicada esta vez a `new_str`: old_str (1.0x, el texto viejo tal
    cual) + new_str con margen real de CRECIMIENTO para pedidos amplios
    tipo "mejora este código" (2.0x, el doble del tamaño actual, no solo
    1.0x como asumía el valor viejo) + sobrecarga estructural/escapado
    de tener dos campos en vez de uno (1.0x, más margen que el 0.75x
    anterior). Sigue sin estar calibrado contra generaciones reales
    (mismo criterio que el resto de las constantes de esta sección) --
    es una segunda estimación razonada, no una medición; solo puede
    SUBIR el techo real (nunca bajarlo) sobre lo que ya daba el selector
    de presupuesto Bajo/Medio/Alto/Extra, igual que antes. Argumento de
    costo: una pasada que ya entraba dentro del techo viejo no gasta más
    por tener un techo más alto (el modelo para cuando termina, no
    cuando se le acaba el margen) -- subir este multiplicador no le suma
    costo a los turnos que ya andaban bien, solo evita el ciclo completo
    de "pagar Sonnet truncado + descartarlo + reintentar en Local con
    peor resultado" en los turnos que SÍ se quedaban cortos, que es puro
    desperdicio de dinero y tiempo.

    `ast.parse` OK en el sandbox y de nuevo con el Python del propio
    dispositivo (3.10.12) en las dos carpetas. Backup
    `orchestrator.py.bak80_pre_patch95` (1085055 bytes, estado
    patch94/patch_router1) creado antes de comitear. Commit al
    dispositivo exitoso al primer intento en `MonolitoPersonal`
    (1085055→1087219 bytes) y sincronizado también a
    `MonolitoPersonal-GitHub` (misma carpeta que sigue quedando
    desactualizada respecto a la principal cada vez que se patchea solo
    una copia -- sigue pendiente que el usuario confirme cuál de las dos
    carpetas es la que usa para probar de ahora en más). Pendiente de
    retest en vivo: repetir el mismo turno "improve that code..." sobre
    `minesweeper.py` (o un archivo de tamaño similar) y confirmar que ya
    no corta a mitad de generación.

31. **`patch_orchestrator96` + `patch_qt66` (2026-09-19) — feature nueva,
    pedido explícito del usuario: "se me acabaron los creditos, podemos
    usar el modelo de gemini tambien?"**. Hasta este patch el motor de
    Nube solo sabía hablar el formato de la Messages API de Anthropic
    (headers `x-api-key`/`anthropic-version`, `system`/`messages`/
    `max_tokens`, `tools[].input_schema`, SSE con eventos tipados
    `message_start`/`content_block_delta`/`message_delta`,
    `usage.input_tokens`/`output_tokens`) -- agregar Gemini fue una
    integración nueva, no un cambio de URL: la Gemini Developer API
    (`generativelanguage.googleapis.com`) usa header `x-goog-api-key`,
    cuerpo `contents`/`systemInstruction`/`generationConfig`,
    `tools[].functionDeclarations` en vez de `input_schema` por
    herramienta, SSE con un objeto `candidates` COMPLETO por chunk (no
    eventos tipados -- los `parts[].text` de cada chunk son
    incrementales, se concatenan en orden) y
    `usageMetadata.promptTokenCount`/`candidatesTokenCount` en vez de
    `usage.input_tokens`/`output_tokens`.

    Arquitectura del fix (orchestrator.py): nuevo `self.cloud_provider`
    ("anthropic"|"gemini", default "anthropic") -- `set_cloud_backend`
    ahora acepta un tercer parámetro `provider`. Dos funciones nuevas,
    paralelas directas de las existentes y con el MISMO contrato de
    3-tupla/generador -- `_call_gemini_api_raw` (equivalente de
    `_call_claude_api_raw`) y `_stream_gemini_api_raw` (equivalente de
    `_stream_claude_api_raw`) -- más `_cloud_tools_schema_for_gemini`
    (reformatea `CLOUD_TOOLS_SCHEMA` al esquema de function-calling de
    Gemini, sacando `cache_control` que Gemini no entiende). Los ÚNICOS
    2 call sites reales de despacho (`_call_llm_raw`/`_stream_llm_raw`)
    ahora eligen entre las dos funciones según `self.cloud_provider` en
    vez de llamar siempre a la variante Claude -- los otros 2 lugares
    que parecían "call sites" (vision, query-rewrite) en realidad pasan
    por esos dos métodos compartidos, así que no necesitaron tocarse.
    Precios (`GEMINI_PRICING_USD_PER_MTOK`, solo gemini-2.5-flash y
    gemini-2.5-pro, verificados en ai.google.dev/gemini-api/docs/pricing
    el día de este patch -- NO calibrados contra uso real, mismo
    criterio que el resto de constantes de presupuesto) y helpers
    `_cloud_pricing_table()`/`_cloud_default_model_for_provider()`
    nuevos para que `_cloud_output_ceiling_tokens_for_cents` (el techo
    de tokens) y `_account_cloud_usage` (el costo real facturado)
    calculen con la tabla del proveedor ACTIVO en vez de asumir siempre
    Anthropic -- un modelo Gemini que no esté en la tabla cae al precio
    de `GEMINI_DEFAULT_MODEL` (gemini-2.5-flash) para el estimado, mismo
    patrón de fallback que ya existía del lado de Anthropic. El log de
    consola de cada llamada ("☁ Claude API (...)") ahora muestra
    "Gemini API" cuando corresponde -- antes decía siempre "Claude API"
    sin importar quién respondió de verdad.

    UI (sovnode_qt.py): nuevo selector "Proveedor de Nube" (Claude/
    Gemini, `combo_cloud_provider`) DENTRO del motor "Nube" ya existente
    (el combo Local/Nube de arriba elige el MOTOR, este elige cuál API
    externa). API key y modelo se guardan POR PROVEEDOR en QSettings
    (`cloud/api_key_anthropic`/`cloud/api_key_gemini`,
    `cloud/model_id_anthropic`/`cloud/model_id_gemini`) en vez de una
    sola clave compartida -- migración NO DESTRUCTIVA desde las claves
    viejas `cloud/api_key`/`cloud/model_id` (se interpretan como las de
    Anthropic) si las nuevas todavía no existen, así que una instalación
    con la key de Claude ya guardada no la pierde al actualizar; se
    sigue escribiendo también la clave legacy al editar la key de
    Anthropic, por compatibilidad hacia atrás con una copia de la app
    sin este patch. Cambiar de proveedor recarga la key/modelo guardados
    de ESE proveedor (`_on_cloud_provider_changed`) en vez de dejar en
    el campo la key vieja, que no serviría de nada contra la API nueva.
    "Olvidar key guardada" ahora borra solo la del proveedor activo.
    `CloudKeyCheckWorker` (botón "Probar conexión") ahora sabe armar el
    request de prueba (1 token de salida) contra cualquiera de las dos
    APIs según `provider`. Se generalizaron además 3 textos que asumían
    Anthropic a secas y quedaban confusos/incorrectos con Gemini activo:
    el nombre del motor "Claude API (Nube)" -> "Nube (API externa)", y
    el selector de presupuesto y su tooltip, que se llamaban "Presupuesto
    de Sonnet" (nombre del modelo de Claude) pese a gobernar el techo de
    tokens de CUALQUIER proveedor activo -> "Presupuesto de Nube".

    Elección de diseño (2 opciones ofrecidas al usuario: selector manual
    vs. fallback automático al agotarse el crédito de Claude): se eligió
    selector MANUAL por ser más simple y no depender de parsear mensajes
    de error de cuota, que Anthropic no documenta con un código estable.
    El usuario todavía no tenía la API key de Gemini a mano al pedir
    esto, así que el modelo default queda en `GEMINI_DEFAULT_MODEL`
    ("gemini-2.5-flash") pendiente de que el usuario decida cuál usar
    una vez tenga la key.

    `ast.parse` OK en los dos archivos, en el sandbox y con el Python
    del propio dispositivo (3.10.12), en las dos carpetas. `pyflakes`
    corrido sobre los dos archivos completos -- ningún warning nuevo
    introducido por este patch (los pocos que arroja son preexistentes,
    en líneas sin relación). Backups `orchestrator.py.bak81_pre_patch96`
    (1087219 bytes) y `sovnode_qt.py.bak14_pre_patch_qt66` (359734
    bytes) creados antes de comitear. Commit al dispositivo exitoso al
    primer intento en `MonolitoPersonal` (orchestrator.py
    1087219→1109573 bytes; sovnode_qt.py 359734→373123 bytes) y
    sincronizado también a `MonolitoPersonal-GitHub` (mismos bytes en
    las dos carpetas, verificado). Pendiente, todo del lado del usuario:
    conseguir una API key de Gemini (Google AI Studio,
    aistudio.google.com), elegir el modelo Gemini a usar (2.5-flash por
    default, más barato y rápido; 2.5-pro para más calidad) y probar
    "Probar conexión" con la UI real -- NINGUNA parte de este patch se
    probó en vivo contra la Gemini API de verdad, solo se verificó
    sintaxis/estructura, porque el usuario no tenía key al momento de
    pedir esto.

32. **`patch_orchestrator97` + `patch_qt67` (2026-09-19) — bug real,
    MEDIDO por el usuario a los minutos de que patch_orchestrator96
    quedara activo**: primera prueba en vivo real de Gemini ("Probar
    conexión" con una key recién cargada) devolvió `❌ Falló la
    conexión: This model models/gemini-2.5-flash is no longer available
    to new users. Please update your code to use models/gemini-3.6-
    flash for the latest features and improvements.` -- error real de
    la propia API de Google, visto en la consola de la app (screenshot
    del usuario). `gemini-2.5-flash` -- el `GEMINI_DEFAULT_MODEL` elegido
    en el patch anterior -- ya no está disponible para cuentas nuevas de
    Gemini; el catálogo de modelos de Google cambia mucho más rápido que
    el de Anthropic (confirmado además por búsqueda web: ya existe toda
    una familia Gemini 3.x -- 3.5/3.6/3.7/3.8 Flash, 3.1 Pro -- que ni
    siquiera existía al momento de escribir patch_orchestrator96 horas
    antes).

    Fix inmediato: `GEMINI_DEFAULT_MODEL` actualizado a `gemini-3.6-flash`
    (el reemplazo que la propia API sugirió en el mensaje de error), con
    su precio real agregado a `GEMINI_PRICING_USD_PER_MTOK` --
    $0.75/$3.75 por MTok in/out, verificado en ai.google.dev/gemini-api/
    docs/pricing el día de este patch -- precio PROMOCIONAL vigente
    hasta 2026-12-31, sube a $1.50/$7.50 desde 2027-01-01 (revisar esa
    fecha si este patch sigue vigente para entonces). Se mantienen
    `gemini-2.5-flash`/`gemini-2.5-pro` en la tabla de precios por si
    una cuenta VIEJA con acceso previo todavía puede usarlos (el error
    dice "no longer available to NEW users", no "no longer exists" a
    secas) -- solo se cambió cuál es el DEFAULT.

    Fix estructural (el más importante de los dos, pensado para que este
    mismo bug no vuelva a pasar la próxima vez que Google jubile un
    modelo): nuevo campo "Modelo" editable a mano en el panel de Nube
    (`cloud_model_input`, sovnode_qt.py) -- hasta este patch el
    `model_id` de cada proveedor NO tenía ningún control de UI, vivía
    fijo en código (`CLOUD_DEFAULT_MODEL`/`GEMINI_DEFAULT_MODEL`) salvo
    que alguien lo cambiara a mano en el Registro de Windows. Ahora el
    usuario puede pegar el ID que la propia API le sugiera en un mensaje
    de error como este y seguir andando sin esperar un patch de código.
    Se guarda por proveedor (`cloud/model_id_anthropic`/`cloud/model_id_
    gemini`, mismo criterio que la API key desde patch_qt66), con
    compatibilidad hacia atrás escribiendo también la clave legacy
    `cloud/model_id` cuando el proveedor activo es Anthropic. Campo
    vacío cae al default de fábrica del proveedor activo en vez de
    mandarle a la API un `model_id` vacío (`_on_cloud_model_edited`).

    `ast.parse` OK en los dos archivos, en el sandbox y con el Python
    del propio dispositivo, en las dos carpetas. `pyflakes` sin
    warnings nuevos. Backups `orchestrator.py.bak82_pre_patch97`
    (1109573 bytes) y `sovnode_qt.py.bak15_pre_patch_qt67` (373123
    bytes). Commit al dispositivo exitoso al primer intento en
    `MonolitoPersonal` (orchestrator.py 1109573→1111004 bytes;
    sovnode_qt.py 373123→378273 bytes) y sincronizado también a
    `MonolitoPersonal-GitHub` (mismos bytes verificados en las dos
    carpetas). Pendiente: que el usuario reintente "Probar conexión"
    con `gemini-3.6-flash` ya cargado por default (no debería necesitar
    tocar nada, el campo ya lo va a mostrar) y confirme si esta vez
    conecta -- si Google vuelve a cambiar el nombre del modelo antes de
    ese retest, el campo "Modelo" nuevo le permite al usuario mismo
    pegar el ID correcto sin depender de otro patch.

33. **`patch_orchestrator98` + `patch_qt68` + `patch_qt69` (2026-09-19) —
    3 bugs/pedidos reales, todos reportados por el usuario en la MISMA
    sesión de prueba en vivo de Gemini que confirmó que patch_
    orchestrator97 sí conectó** ("Turno completado exitosamente usando
    el modelo: gemini-3.6-flash", log real).

    **(a) `patch_orchestrator98` -- código incompleto al "mejorar"
    minesweeper.py, bug real MEDIDO**: con las herramientas de workspace
    desactivadas, el turno "mejora ese codigo" devolvió el código
    completo como texto plano en el chat (no vía `edit_file`, `0
    tool_call(s)` en el log), pero el bloque terminaba cortado a mitad
    de una expresión booleana (`... or` seguido de nada) -- inválido
    como Python -- y la consola igual mostró "✓ sin problemas". 677
    tokens de salida sobre un techo de 2400 descarta que fuera un corte
    por presupuesto (ese caso ya estaba cubierto desde patch95/96). Causa
    raíz: `_call_gemini_api_raw`/`_stream_gemini_api_raw` solo marcaban
    `done_reason="length"` (la señal que el pipeline usa para
    rescatar/continuar una respuesta cortada, ver `_looks_truncated` y
    la rama de continuación de `run_turn` para `FAST_PATH`) cuando
    `finishReason == "MAX_TOKENS"` -- cualquier OTRO finishReason que no
    fuera "STOP" (p. ej. "SAFETY", "RECITATION" -- un chequeo de límites
    de tablero como el que se cortó es exactamente el tipo de código
    genérico/muy común que suele disparar el filtro de recitación de
    Google --, "PROHIBITED_CONTENT", "BLOCKLIST",
    "MALFORMED_FUNCTION_CALL", "OTHER") caía al default "stop", como si
    la generación hubiera terminado bien. Fix: cualquier finishReason
    que no sea "STOP" (o vacío) se trata como corte -- reusa a propósito
    el mismo `done_reason="length"` que ya dispara el rescate/
    continuación existente (más seguro que inventar un 4to valor que
    ningún call-site interpretaría) y, además, loguea una advertencia
    explícita con el finishReason real visto, para que quede claro en
    consola que no fue un corte por presupuesto. Elección deliberada:
    seguir pidiendo más tokens no ayuda si el corte fue por un filtro de
    contenido, pero el rescate LOCAL (Ollama, sin costo) no pasa por los
    filtros de Gemini y sí puede terminar el código -- mejor eso que un
    fragmento incompleto marcado como éxito.

    **(b) `patch_qt68` -- el watcher de Workspace seguía leyendo disco
    con las herramientas desactivadas, bug real MEDIDO**: el usuario
    apagó "Habilitar herramientas de archivo" (log: "Herramientas de
    workspace DESACTIVADAS: todo pedido de código se responde directo en
    el chat, sin tocar el disco") pero un turno posterior igual mostró
    "📚 [Workspace] 'minesweeper.py' reindexado: 4 fragmento(s)" -- el
    `WorkspaceWatcherWorker` (escaneo en segundo plano para RAG/búsqueda
    semántica) seguía corriendo. Causa raíz: desde el BLINDAJE de
    2026-09-02 ("DOS conceptos de workspace totalmente desconectados
    entre sí"), el toggle "Habilitar herramientas de archivo" SOLO
    controlaba si el modelo podía usar `read_file`/`write_file`/
    `edit_file`/`list_dir` vía tool-calling -- el watcher de RAG era
    independiente A PROPÓSITO (por diseño original), pero eso contradice
    directamente el propio tooltip del control ("sin tocar el disco").
    Fix: el toggle ahora también arranca/para el hilo del watcher
    (`_on_workspace_tools_toggled`) -- apagarlo detiene CUALQUIER lectura
    de disco del lado de Workspaces, no solo la escritura vía
    tool-calling; volver a prenderlo retoma el escaneo de las carpetas ya
    agregadas sin tener que sacarlas y re-agregarlas. También se corrigió
    el arranque al iniciar la app (`_load_persisted_workspaces`, que
    corre ANTES de que se cargue el valor persistido del toggle -- ahora
    lee `QSettings` directo en vez de confiar en el atributo todavía no
    inicializado) y al agregar una carpeta nueva
    (`_on_add_workspace_clicked`, ahora sujeto al mismo gate). `add_root`
    en sí sigue siendo seguro llamarlo con el toggle apagado -- solo
    registra la ruta en memoria, el I/O real vive en `scan_once()` del
    hilo, que es lo que queda parado.

    **(c) `patch_qt69` -- panel "Motor de Generación" apretado, pedido
    explícito del usuario: "mejora las dimensiones de los cuadros en la
    interfaz para que no se aplasten entre si"**: esta tarjeta pasó de 6
    widgets (antes de patch_qt66/67) a 13 sin ajustar el espaciado --
    todo se veía como un bloque apretado. Fix visual únicamente:
    `ec_layout.setSpacing` subido de 6 a 9px, más `addSpacing(8)` extra
    antes de cada widget que arranca un subgrupo lógico nuevo (proveedor+
    modelo / credenciales / presupuesto) -- separa los subgrupos entre sí
    sin tocar el tamaño de cada control individual (ya definido en el QSS
    de QComboBox/QLineEdit) ni la lógica de ningún handler.

    `ast.parse` OK en los dos archivos, sandbox + dispositivo, las dos
    carpetas. `pyflakes` sin warnings nuevos. Backups
    `orchestrator.py.bak83_pre_patch98` (1111004 bytes) y
    `sovnode_qt.py.bak16_pre_patch_qt68_69` (378273 bytes). Commit al
    dispositivo exitoso al primer intento en `MonolitoPersonal`
    (orchestrator.py 1111004→1115652 bytes; sovnode_qt.py
    378273→383219 bytes) y sincronizado también a
    `MonolitoPersonal-GitHub` (mismos bytes verificados en las dos
    carpetas). Pendiente de retest en vivo: (a) repetir "mejora ese
    codigo" sobre minesweeper.py con Gemini y confirmar que esta vez, si
    Gemini corta la respuesta, el pipeline la completa en vez de
    devolverla trunca -- revisar la consola por el nuevo mensaje de
    advertencia "Gemini cortó la respuesta antes de tiempo
    (finishReason=...)" si vuelve a pasar; (b) apagar/prender
    "Habilitar herramientas de archivo" y confirmar en consola que
    "reindexado" deja de aparecer con el toggle apagado; (c) revisar
    visualmente el panel "Motor de Generación" con Gemini seleccionado
    (el caso con más widgets visibles a la vez).

34. **`patch_qt70` (2026-09-19) — bug real, MEDIDO por screenshot del
    usuario ("mejora esta parte de la interfaz para que todo sea
    distinguible"): el panel "Motor de Generación" se veía con varias
    filas como cajas prácticamente VACÍAS ("Probar conexión", "Olvidar
    key guardada" sin texto legible)**. Causa raíz encontrada al revisar
    el QSS: `QPushButton#secondaryButton` tenía como color de texto POR
    DEFECTO `theme["secondary"]` -- el mismo gris apagado pensado para
    etiquetas secundarias (`sectionTitle`), NO para el label principal
    de un botón interactivo -- sobre el fondo `theme["card"]` (p. ej.
    `#8B92A5` sobre `#171B24` en el tema oscuro default) el contraste es
    bajo, y a la fuente chica que ya tenía el botón (12px) en una
    pantalla de alta densidad el texto quedaba prácticamente invisible
    hasta pasar el mouse por encima (`:hover` sí lo subía a
    `theme["text"]`, pero eso no ayuda si el usuario nunca hizo hover).
    Este bug afecta a TODOS los `secondaryButton` de la app (Probar
    conexión, Olvidar key guardada, Descargar modelo, Añadir/Quitar
    carpeta, Exportar chat, Exportar dataset, engranaje de Ajustes,
    adjuntar, micrófono, TTS) -- existía desde antes de esta sesión, solo
    se hizo evidente ahora porque el panel de Motor tiene dos
    `secondaryButton` seguidos en un espacio ya apretado por los
    controles de Gemini.

    Fix: color de texto por defecto de `secondaryButton` sube a
    `theme["text"]` (el mismo que ya usaba `:hover`) -- el hover sigue
    aportando el resaltado de borde en `theme["accent"]`, pero deja de
    ser la ÚNICA forma de leer qué dice el botón. Mejoras acompañantes,
    mismo patch: sidebar subido de 280 a 300px de ancho (más aire para
    los textos más largos que trajo Gemini: "Gemini (Google)",
    "gemini-3.6-flash", "Probar conexión") y `QComboBox` con `font-size`
    de 11 a 12px + padding de 6 a 7px (mismo criterio de legibilidad).
    Ningún otro archivo de estilo (`QLineEdit`, que ya no tenía
    `font-size` explícito y hereda el default del SO, similar a los
    12px nuevos de QComboBox) se tocó.

    `ast.parse` OK en el sandbox y con el Python del propio dispositivo.
    `pyflakes` sin warnings nuevos. Backup
    `sovnode_qt.py.bak17_pre_patch_qt70` (383219 bytes). Commit al
    dispositivo exitoso al primer intento (383219→385373 bytes),
    verificado byte a byte. **A partir de este patch se deja de
    sincronizar cada cambio también a `MonolitoPersonal-GitHub`** --
    pedido explícito del usuario ("no necesito que lo hagas tambien en
    el monolitopersonal github, ya que lo puedo reemplazar con un click
    con el bat"): esa carpeta es un EXPORT desechable que el usuario
    regenera con `Crear carpeta limpia para GitHub.bat` cuando quiere
    subir al repo, no una segunda copia de trabajo -- responde
    definitivamente la pregunta que había quedado abierta desde el ítem
    29 sobre cuál carpeta es la canónica: `MonolitoPersonal`, sin
    ambigüedad. Los ítems 31 a 34 fueron los últimos en sincronizarse a
    las dos carpetas; de acá en adelante, salvo pedido explícito en
    contrario, los patches de esta sesión solo tocan `MonolitoPersonal`.
    Pendiente de retest visual en vivo: confirmar que "Probar conexión"/
    "Olvidar key guardada" y el resto de los botones secundarios ya se
    leen sin necesidad de pasar el mouse por encima.

35. **`patch_qt71` (2026-09-19) — bug real, GRAVE, autoinducido por
    `patch_qt70` en el mismo día: MEDIDO por screenshot del usuario
    mostrando TODA la interfaz caída al estilo nativo de Windows sin
    tema oscuro** ("arregla la interfaz ahora, a el estilo que tenia
    antes"). Causa raíz: el comentario BLINDAJE que documentaba el fix
    de contraste de `patch_qt70` se escribió con sintaxis Python de
    comentario de línea (`#`) pero se pegó DENTRO del string que arma
    `build_style(theme, font_family)` -- un `return f"""..."""` que
    abarca ~lines 2040-2732 y que Qt interpreta como QSS (CSS), no como
    Python. QSS/CSS solo soporta comentarios de bloque `/* ... */`; un
    `#` ahí no es un comentario para nada, es texto literal que se
    inyecta en la hoja de estilos. Eso corrompió el parseo de QSS desde
    ese punto en adelante -- sin ninguna excepción, porque el error
    ocurre en el parser C++ de Qt, no en Python -- y los widgets
    afectados (`QComboBox`, `QListWidget#workspacesList`, y la propia
    regla `QPushButton#secondaryButton` que el comentario precedía)
    perdieron todo su estilo y cayeron al render nativo del SO. Es
    decir: el fix de legibilidad de `patch_qt70` sí quedó bien escrito,
    pero el comentario que lo explicaba rompió la hoja de estilos
    completa a partir de ahí.

    Fix: el bloque de comentario se reescribió en sintaxis `/* ... */`
    válida para QSS, documentando adentro el mismo bug (para que quede
    como advertencia permanente en el propio lugar del riesgo: "todo
    comentario BLINDAJE agregado dentro de este f-string tiene que usar
    `/* */`, nunca `#`"). Nota para quien edite este archivo después: el
    primer intento de este mismo fix falló por escribir en la prosa del
    comentario nuevo la secuencia literal de comillas triples que abre
    el f-string de Python -- eso rompió el f-string EXTERIOR (el de
    Python, no el de QSS) y `ast.parse` tiró `SyntaxError: unmatched
    ')'`. Se corrigió describiendo esa sintaxis en palabras ("el return
    de build_style, cerca de la línea 2040") en vez de citarla
    literalmente.

    Verificado en el sandbox: `ast.parse` → `SYNTAX_OK`, `pyflakes` sin
    warnings nuevos (solo el ya preexistente de `_CancelToken` en la
    línea ~3709), y barrido manual con `grep`/`awk` de las líneas
    2040-2732 completas (los límites reales del f-string, confirmados
    con `awk`) sin encontrar ningún otro `#` suelto dentro del bloque.
    Backup `sovnode_qt.py.bak18_pre_patch_qt71` (385373 bytes, el
    tamaño exacto de `patch_qt70`, confirmado antes de tocar nada).
    Commit al dispositivo exitoso al primer intento (385373→387047
    bytes), verificado byte a byte y con `ast.parse` del propio Python
    del dispositivo. Solo a `MonolitoPersonal`, como desde el ítem 34.
    Pendiente de retest visual en vivo: confirmar que el tema oscuro
    completo volvió (no solo el panel de Motor de Generación, sino
    combos y la lista de carpetas de workspace, que también habían
    perdido su estilo).

    **Lección para el resto de la sesión y las que sigan**: cualquier
    comentario nuevo que se agregue dentro del `return f"""..."""` de
    `build_style()` (líneas ~2040-2732) tiene que usar `/* */`, nunca
    `#` -- ese f-string es QSS, no Python, aunque esté escrito adentro
    de una función Python. Vale la pena, antes de dar por cerrado
    cualquier futuro patch que toque esa función, correr el mismo
    barrido (`awk` para ubicar el rango exacto del f-string + `grep`
    para confirmar que no quedó ningún `#` de línea completa suelto
    adentro) como paso de verificación estándar, no solo `ast.parse`
    (que valida Python, no QSS, y por eso no detectó el bug original de
    `patch_qt70` -- el archivo era Python sintácticamente válido todo el
    tiempo).

36. **`patch_qt72` (2026-09-19) — bug real, MEDIDO por screenshot del
    usuario: dentro del panel "Motor de Generación", texto de filas
    distintas (la etiqueta "Modelo:", el campo de key enmascarado, el
    label de presupuesto y el de uso) se veía amontonado/pisándose
    entre sí** ("mejora esta parte de la interfaz para que todo sea
    distinguible" seguido, tras la confirmación de que el tema oscuro
    ya había vuelto con `patch_qt71`, de "ajusta el cuadro para que
    tenga una escala mínima donde se pueda ver, todo bien organizado,
    sin tocar el diseño -- el diseño déjalo tal cual como está").
    Causa raíz: el `QFrame sidebar` (que contiene las 3 secciones
    plegables -- Apariencia, Motor, Workspace -- con `engine_card`
    creciendo de 6 a 13 widgets entre `patch_qt66` y `patch_qt67`, ver
    ítems 31-32) se agregaba DIRECTO a `main_layout` sin ningún
    `QScrollArea` de por medio. La ventana respeta una altura MÍNIMA
    de 700px (`self.setMinimumSize(1024, 700)`) más lo que haya
    quedado persistido en `QSettings("SovNode","SovNode").value(
    "window/geometry")` de una sesión anterior a que existieran los
    campos de Gemini -- si esa altura no alcanza para el contenido
    natural del sidebar (que ya no entra en 700px con todo lo que se
    le agregó esta sesión), Qt no tiene forma de avisar salvo
    comprimir filas, lo que en la práctica se ve como texto de filas
    distintas invadiendo el espacio de la fila vecina.

    Fix, deliberadamente SOLO estructural (pedido explícito del
    usuario de no tocar el diseño): el `sidebar` ahora se envuelve en
    un `QScrollArea#sidebarScroll` nuevo, con `setWidgetResizable(
    True)` (el sidebar sigue midiendo 300px de ancho, pero ese ancho
    fijo ahora lo impone el `QScrollArea` en vez del `QFrame` de
    adentro -- mismo ancho final, un contenedor distinto lo aplica),
    scroll horizontal apagado y scroll vertical "según haga falta". El
    QSS de `QFrame#sidebar` no cambió ni un color/fuente/padding --
    solo se le movió el borde derecho de 1px al `QScrollArea` nuevo
    (para que siga dibujándose en el mismo lugar de siempre) y se le
    agregaron reglas de fondo a `QScrollArea#sidebarScroll` y su
    viewport interno para que no se asome el blanco nativo de Qt
    alrededor/detrás del sidebar (mismo motivo que ya tenía
    `QListWidget#workspacesList` con su propia regla, ítem previo).
    `self.config_panel` (la referencia que usa el botón de engranaje
    para mostrar/ocultar el panel) pasa a apuntar al `QScrollArea`
    nuevo en vez de al `QFrame` de adentro, para que ese toggle siga
    funcionando exactamente igual. Con esto, cada control del sidebar
    SIEMPRE se dibuja a su tamaño natural (el que ya definía el QSS
    existente) -- si no entra todo en la altura visible de la
    ventana, aparece una barra de scroll vertical en vez de
    amontonar/pisar texto entre filas.

    Verificado en el sandbox: `ast.parse` → `SYNTAX_OK`, `pyflakes`
    sin warnings nuevos (mismo único warning preexistente de
    `_CancelToken`), y un chequeo extra de balance de llaves sobre el
    template QSS completo de `build_style()` (sustituyendo `{{`/`}}`
    y las interpolaciones `{theme[...]}` por marcadores, para no
    reconstruir accidentalmente el mismo tipo de bug de `patch_qt71`)
    dio 90 llaves abiertas contra 90 cerradas. Backup
    `sovnode_qt.py.bak19_pre_patch_qt72` (387047 bytes, el tamaño
    exacto de `patch_qt71`, confirmado antes de tocar nada). Commit al
    dispositivo exitoso al primer intento (387047→390628 bytes),
    verificado byte a byte y con `ast.parse` del propio Python del
    dispositivo. Solo a `MonolitoPersonal`, como desde el ítem 34.
    Pendiente de retest visual en vivo: confirmar que el panel "Motor
    de Generación" ya se lee fila por fila sin superposición, con o
    sin necesidad de hacer scroll según la altura de la ventana del
    usuario.

37. **`patch_orchestrator99` (2026-09-19) — bug real, MEDIDO en vivo por
    el usuario: el saludo generado por Gemini se mostraba como
    "Â¡Hola! Â¿En quÃ© te puedo ayudar hoy?" en vez de "¡Hola! ¿En qué
    te puedo ayudar hoy?"** (pregunta del usuario, sin saber todavía
    que era un bug reproducible: "que opinas de como rinde? porque
    aveces comete errores al generar texto?"). Causa raíz: la API de
    Gemini en modo streaming (`:streamGenerateContent?alt=sse`, el
    camino real de cualquier turno normal) manda el header
    `Content-Type: text/event-stream` SIN `charset` explícito.
    `requests` adivina la codificación de la respuesta a partir de ese
    header (`get_encoding_from_headers`), y cualquier content-type que
    contenga la palabra "text" sin `charset` cae en su default
    histórico de ISO-8859-1 (heredado del RFC 2616, pensado para
    HTML/texto plano viejo, no para SSE moderno que en la práctica
    siempre es UTF-8). Con `resp.encoding` mal seteado a ISO-8859-1,
    `resp.iter_lines(decode_unicode=True)` (en `_stream_gemini_api_
    raw`) decodifica cada línea del SSE con ese codec incorrecto: cada
    caracter UTF-8 de 2 bytes (tildes, ñ, ¡, ¿) se separa en DOS
    caracteres Latin-1 distintos (p. ej. los bytes UTF-8 de "é" se leen
    como "Ã©") -- el string Python resultante queda con la corrupción
    ya "cocinada" adentro, no es un problema de fuente/rendering de la
    UI (que solo muestra fielmente el string ya roto que le llega).
    Confirma además la respuesta a la pregunta más amplia del usuario:
    esto NO es el modelo "cometiendo errores al generar texto" -- es
    un bug de transporte/decodificación puramente nuestro, nada tiene
    que ver con la calidad de Gemini.

    Fix: `resp.encoding = "utf-8"` fijado a mano justo después de
    abrir la respuesta streameada, ANTES de iterar líneas -- se ignora
    lo que `requests` haya adivinado del header. Aplicado en
    `_stream_gemini_api_raw` (donde se confirmó el bug) Y en
    `_stream_claude_api_raw` (mismo patrón exacto de
    `resp.iter_lines(decode_unicode=True)`, aunque no se confirmó el
    bug en vivo con Anthropic esta sesión -- puede que su API sí mande
    `charset=utf-8` explícito y por eso no se vio, pero corregirlo ahí
    también es gratis y evita que aparezca el mismo bug si Anthropic
    cambia ese header en el futuro). La variante NO-streaming
    (`_call_gemini_api_raw`) NO tenía este problema: usa `resp.json()`,
    y `requests` sí trata `application/json` como UTF-8 por default
    (caso especial en `get_encoding_from_headers`), a diferencia de
    `text/event-stream`. El streaming de Ollama local
    (`_stream_llm_raw`, más abajo en el archivo, mismo patrón de
    `iter_lines`) tampoco está afectado, por una razón distinta: su
    `Content-Type` es `application/x-ndjson`, que no matchea ni la
    rama "text" ni la rama "application/json" de `get_encoding_from_
    headers`, así que `resp.encoding` queda en `None` y `requests`
    directamente NO decodifica (devuelve bytes crudos), que
    `json.loads()` interpreta como UTF-8 por su cuenta (comportamiento
    por defecto del propio `json` de Python) -- correcto por
    casualidad, no se tocó.

    Nota para quien continúe: las conversaciones que ya se guardaron
    con este bug activo (turnos anteriores en el WAL/historial de chat
    de esta sesión) van a seguir mostrando el texto mojibake tal cual
    quedó guardado -- este fix solo previene la corrupción hacia
    adelante, no repara turnos ya persistidos. Si el usuario reporta
    ver el mismo patrón "Â¡"/"Ã©" en mensajes VIEJOS después de este
    patch, es esperado y no un fix incompleto.

    Verificado en el sandbox: `ast.parse` → `SYNTAX_OK`, `pyflakes` sin
    warnings nuevos (los 6 preexistentes -- import sin usar de
    `rag_faiss.chunk_document`, redefinición de `requests`, dos
    f-strings sin placeholders, `Skeleton` no definido, y
    `wants_file_tools` sin usar -- ninguno cerca de las líneas
    tocadas). Backup `orchestrator.py.bak84_pre_patch99` (1115652
    bytes, el tamaño exacto de `patch_orchestrator98`, confirmado
    antes de tocar nada). Commit al dispositivo exitoso al primer
    intento (1115652→1119265 bytes), verificado byte a byte y con
    `ast.parse` del propio Python del dispositivo. Solo a
    `MonolitoPersonal`, como desde el ítem 34.

38. **`patch_orchestrator100` (2026-09-19) — bug real, MEDIDO en vivo:
    turno "añade cosas al codigo y explica que añadiste" (Gemini, sobre
    `minesweeper.py`) terminó en "He añadido las siguientes
    funcionalidades" y nada más -- la pasada de cierre gastó apenas 5
    tokens de salida en total.** El usuario lo reportó sin saber
    todavía que era reproducible ("no explico lo que metio, creo que
    porque se corto"). Diagnóstico: NO es el bug de truncamiento de
    `patch_orchestrator98` (ítem 33) -- el `finishReason` de esa pasada
    fue `STOP` genuino, no `MAX_TOKENS` ni ningún otro corte, así que
    el mecanismo de rescate de ese patch nunca tenía por qué
    activarse. Tampoco es el bug de ruteo de `patch_orchestrator89`
    (mencionado en un BLINDAJE ya existente en el código, junto a
    `_NON_FILE_TOPIC_RE`) -- el archivo se identificó y editó
    correctamente (2981→4554 bytes, 5 ediciones aplicadas). La causa
    real: `_zero_chatter` (§5.6, dentro del bucle de tool-calling de
    `run_turn`) le pide SIEMPRE al modelo "confirmación breve de 1-2
    frases" tras una herramienta exitosa, sin importar si el usuario
    pidió explícitamente una explicación en SU PROPIO mensaje de ese
    turno -- con "...y explica que añadiste" en el pedido original, el
    modelo recibió dos señales contradictorias (el pedido del usuario
    pidiendo detalle, la instrucción de cierre pidiendo brevedad) y
    resolvió el conflicto escribiendo solo la frase introductoria de
    una lista que nunca llegó a enumerar, dentro del presupuesto de
    "sé breve" que le impusimos nosotros. Confirma, de paso, la
    pregunta más amplia que el usuario había hecho un turno antes:
    esto tampoco es el modelo "cometiendo errores al generar texto" --
    es un conflicto de instrucciones de nuestro propio prompt de
    cierre.

    Fix: nuevo regex `_EXPLICIT_EXPLANATION_REQUEST_RE` (verbos/frases
    de pedido explícito de explicación -- "explica"/"detalla"/
    "describe"/"cuéntame"/"dime qué hiciste" en español, "explain"/
    "detail"/"tell me what"/"what you did" en inglés). Si el
    `user_input` de este turno matchea, `_zero_chatter` deja de pedir
    brevedad para esa pasada de cierre y en cambio pide explícitamente
    el detalle real (sin volver a pegar el código completo del archivo
    como "explicación", mismo criterio que ya usa el resto del
    prompt). Sin ese pedido explícito, comportamiento IDÉNTICO al de
    antes -- la brevedad de Zero-Chatter sigue siendo el default para
    "mejora X"/"arregla Y" sin pedido de explicación adicional.
    Deliberadamente un regex NUEVO y no una reutilización de
    `_NON_FILE_TOPIC_RE` (que también contiene una forma de
    "explicación"): ese otro regex decide si un turno con verbo de
    "modificar" es en realidad sobre un archivo o no, y ya tuvo un
    bug real (`patch_orchestrator89`, ítem previo en este mismo
    archivo) por capturar el verbo suelto "explica" con demasiada
    amplitud para ESE uso -- acá el uso es distinto (solo decide si
    pedir brevedad o no) y no hereda ese riesgo, así que capturar el
    verbo suelto es intencional y correcto para este caso.

    Verificado en el sandbox: `ast.parse` → `SYNTAX_OK`, `pyflakes`
    sin warnings nuevos (mismos 6 preexistentes), y un script standalone
    contra el regex nuevo con la frase real del usuario ("añade cosas
    al codigo y explica que añadiste", matchea) más 3 variantes
    positivas en inglés/español y 4 negativos de control ("mejora el
    codigo...", "hola", "agrega un sistema de puntuacion", "hazlo mas
    rapido") -- los 8 se comportaron como se esperaba, sin falsos
    positivos nuevos. Backup `orchestrator.py.bak85_pre_patch100`
    (1119265 bytes, el tamaño exacto de `patch_orchestrator99`,
    confirmado antes de tocar nada). Commit al dispositivo exitoso al
    primer intento (1119265→1123349 bytes), verificado byte a byte y
    con `ast.parse` del propio Python del dispositivo. Solo a
    `MonolitoPersonal`, como desde el ítem 34. Pendiente de retest en
    vivo: repetir un turno "mejora/agrega X y explica qué hiciste"
    sobre Gemini y confirmar que la explicación final ya no se corta
    en la frase introductoria.

39. **`patch_orchestrator101` (2026-09-19) — recalibración del piso
    mínimo de la categoría "juego" (§5.5, negociador de alcance),
    pedida explícitamente por el usuario tras preguntar "porque aun
    tenemos errores con el crear un archivo, cuantos tokens minimo
    necesita para crearlo por su cuenta y que el bot local no se
    demore unos 50 segundos"**. Diagnóstico, con datos de la propia
    sesión: con Gemini y Presupuesto "Bajo", el techo real que calcula
    `_cloud_output_ceiling_tokens_for_cents` es ~2400 tokens (a partir
    del precio de salida de `gemini-3.6-flash`, $3.75/MTok). El piso
    mínimo de "juego" estaba en 1300 (recalibrado la vez anterior en
    `patch_orchestrator80`, ítem histórico ya documentado) -- 2400>1300
    dejaba pasar el turno como "factible", pero en la práctica NINGUNO
    de los dos turnos reales de esta sesión que matchearon esa
    categoría (`dame un codigo de buscaminas`, `Create an space
    invaders game in the workspace`) completó dentro de ese techo:
    ambos dispararon el file-op guard ("write_file... hit Sonnet's
    per-turn budget") y cayeron al rescate local (Ollama, ~50s+
    medidos), y el segundo terminó en un archivo de 153 bytes --
    prácticamente vacío. El piso de 1300 seguía siendo un gate de
    factibilidad razonable (evita pagar por algo condenado a fallar
    con $0 de presupuesto), pero nunca fue diseñado para garantizar
    que el pedido complete SIN el rescate local -- son dos preguntas
    distintas, y el usuario preguntó por la segunda.

    Opciones presentadas al usuario (`AskUserQuestion`): subir el piso
    en el código para que "Bajo" eleve automáticamente a un tier
    mayor en pedidos de juego, dejarlo como está y elegir manualmente
    "Medio"/"Alto" desde la UI, o medir más turnos antes de decidir.
    Eligió la primera. Fix: piso de "juego" subido de 1300 a 3000
    (`_MIN_VIABLE_FLOOR_PATTERNS`, `_MIN_VIABLE_FLOOR_CATEGORY_LABELS`
    actualizado en lockstep para que la telemetría del WAL siga
    etiquetando "juego" en vez de mostrar "3000"), y
    `_MIN_VIABLE_FLOOR_UNRECOGNIZED_NAME` (pisos de nombres de
    juego/app no reconocidos, "isaac"/"mario") subido igual, de 1300 a
    3000, porque siempre se pensó en el mismo orden de magnitud que
    "juego" (ver su propio comentario ya existente). Con esto, el
    mecanismo de elevación automática YA EXISTENTE
    (`_elevate_codegen_ceiling_if_needed`, de `patch_orchestrator82`/
    `83`, sin cambios de lógica) entra en juego para CUALQUIER pedido
    de juego en "Bajo": con Gemini eleva de "Bajo" a "Medio" (techo
    real final 6384tok, confirmado con una simulación standalone antes
    de tocar el archivo real). Nota importante para quien continúe:
    con Claude (`claude-sonnet-5`, ~2.7x más caro por token que
    Gemini), el MISMO piso de 3000 no alcanza con el rango de "Medio"
    de Claude y termina elevando hasta "Alto" (techo final 4788tok) en
    vez de "Medio" -- consecuencia matemática esperada de un piso en
    TOKENS compartido entre proveedores con precio por token distinto
    (la cantidad de tokens que necesita un juego real no depende del
    proveedor, pero cuántos centavos cuestan esos tokens sí), NO un
    bug. Sin medición en vivo de un turno de juego con Claude en
    "Bajo" todavía esta sesión -- si en la práctica ese salto a "Alto"
    resulta excesivo, recalibrar por separado en vez de bajar el
    número medido para Gemini.

    Verificado en el sandbox: `ast.parse` → `SYNTAX_OK`, `pyflakes` sin
    warnings nuevos (mismos 6 preexistentes), y una simulación
    standalone de `_elevate_codegen_ceiling_if_needed` (reimplementada
    en un script aparte con la misma fórmula, NO importando el
    archivo real) confirmando los rangos exactos de los 4 niveles para
    ambos proveedores y el resultado de la elevación con el piso
    nuevo (3000) antes de aplicar el cambio real. Backup
    `orchestrator.py.bak86_pre_patch101` (1123349 bytes, el tamaño
    exacto de `patch_orchestrator100`, confirmado antes de tocar
    nada). Commit al dispositivo exitoso al primer intento
    (1123349→1126540 bytes), verificado byte a byte y con `ast.parse`
    del propio Python del dispositivo. Solo a `MonolitoPersonal`, como
    desde el ítem 34. Pendiente de retest en vivo: repetir un pedido
    de juego con Gemini y Presupuesto "Bajo" seleccionado y confirmar
    en el log que ahora aparece "Presupuesto elevado... subiendo a
    'Medio'" ANTES de la primera llamada a Cloud, en vez de intentar
    con 2400tok y caer al rescate local después.

40. **`patch_orchestrator102` (2026-09-19) — bug real, MEDIDO vía WAL Y
    debug log en vivo (no solo por screenshot, esta vez con el archivo
    de verdad en el dispositivo): turno "improve that code and tell me
    what you implement" [turn_id ddbea5ae-aefa-4334-9e2e-113379df6938]
    terminó en "Here are the improvements implemented in **`space",
    cortado a mitad de una palabra.** El usuario preguntó
    "identifica porque no explico lo que agrego en el ultimo mensjae".
    A diferencia del ítem 38 (`patch_orchestrator100`, Zero-Chatter
    sobre-aplicado), esta vez SÍ hubo un pedido explícito de
    explicación en el turno ("tell me what you implement", matchea
    `_EXPLICIT_EXPLANATION_REQUEST_RE` del ítem 38) -- ese fix ya
    aplicó correctamente (la instrucción de cierre NO pedía brevedad
    acá). El problema es distinto y más profundo: `sovnode_debug.log`
    (`[EmptyResponseDiag]`) mostró `done_reason=length` para la pasada
    de cierre -- confirmando que `patch_orchestrator98`/`99` (detección
    correcta de truncamiento de Gemini) SÍ está funcionando -- pero
    `raw_len=179` (no vacío), así que la garantía de "nunca vacío" de
    `resolve_visible_answer` tampoco tenía motivo para intervenir (esa
    red de seguridad es solo contra respuesta VACÍA). El texto
    truncado se mostró tal cual.

    Causa raíz estructural: el bloque de rescate de truncamiento que
    YA EXISTE dentro del bucle de tool-calling (§5.5, desde
    `patch_orchestrator49`/`60`) solo dispara cuando `next_tool_call`
    es un `write_file`/`edit_file` real cortado a mitad de JSON -- pero
    en la pasada de CIERRE (la que solo escribe la explicación en
    prosa, después de que un archivo ya se escribió con éxito),
    `next_tool_call` se fija a `None` A PROPÓSITO (`None if
    is_last_allowed_pass else...`, para cortar el bucle de
    herramientas) sin importar qué tan cortada haya quedado la prosa.
    El `if` del rescate nunca podía ser verdadero para este caso --no
    es específico de Gemini ni de este turno puntual, es un hueco
    estructural que existía desde que se armó el bucle de cierre,
    simplemente nunca se había medido un turno real que lo disparara
    hasta ahora.

    Fix: nuevo bloque, mismo criterio de "reintentar una sola vez con
    más presupuesto" que ya usan los rescates de write_file/edit_file
    de este mismo bucle -- si la pasada de cierre (sin tool call,
    `is_last_allowed_pass=True`) termina con `done_reason=length`, se
    reintenta UNA vez con el doble de `FOLLOWUP_DECISION_NUM_PREDICT`
    (500 en vez de 250 -- sigue siendo chico frente al techo real de
    codegen, esto es prosa de cierre, no código nuevo) y se usa ese
    resultado si no vino vacío ni con un error explícito. Si el
    reintento TAMBIÉN se corta, se deja igual (ya es lo mejor
    conseguido con el doble de presupuesto) en vez de reintentar sin
    límite -- mismo espíritu de "un solo reintento gratuito, nunca un
    bucle sin fin" que ya usan todos los demás rescates del archivo.
    Nuevo evento de WAL, `closing_explanation_truncated_retry`, para
    poder medir con datos reales cuán seguido dispara este caso.

    Nota para quien continúe: la causa técnica EXACTA de por qué
    Gemini cortó a los ~9 tokens con `done_reason=length` (muy lejos
    del techo de 250) sigue sin confirmarse -- podría ser un
    `finishReason` real de `SAFETY`/`RECITATION` (el patch98 los trata
    como truncamiento a propósito, ver ese ítem) disparado por algo
    trivial en el texto a medio escribir, o una colisión con algún
    `stopSequence`. No se investigó más a fondo porque el fix de
    reintento resuelve el síntoma sin importar la causa exacta (mismo
    criterio pragmático que ya usa el resto de esta sección de
    rescates) -- si se repite seguido, vale la pena revisar el
    `finishReason` crudo vía el nuevo evento de WAL.

    Verificado en el sandbox: `ast.parse` → `SYNTAX_OK`, `pyflakes` sin
    warnings nuevos (mismos 6 preexistentes). Backup
    `orchestrator.py.bak87_pre_patch102` (1126540 bytes, el tamaño
    exacto de `patch_orchestrator101`, confirmado antes de tocar
    nada). Commit al dispositivo exitoso al primer intento
    (1126540→1131619 bytes), verificado byte a byte y con `ast.parse`
    del propio Python del dispositivo. Solo a `MonolitoPersonal`, como
    desde el ítem 34. Pendiente de retest en vivo: repetir un turno de
    edición con pedido explícito de explicación y confirmar que ya no
    se corta a mitad de palabra (o, si se corta igual tras el
    reintento, que al menos el WAL registre
    `closing_explanation_truncated_retry` para poder investigar el
    `finishReason` real la próxima vez).

41. **`patch_orchestrator103` (2026-09-19) — pedido explícito del usuario,
    tras medir en vivo que el propio reintento del ítem 40
    (`patch_orchestrator102`) resultó insuficiente en un turno posterior:
    "mejora ese código y decime qué implementaste" sobre
    `space_invaders_game.py` terminó de nuevo cortado, esta vez en
    "Expanded the top interface to display remaining **Lives" (markdown
    sin cerrar, dato sin completar) -- una explicación más larga y
    estructurada (varios encabezados y sub-viñetas) que el caso del
    ítem 40, para la que ni siquiera el doble de presupuesto (250→500
    vía el reintento) alcanzó. El usuario propuso, en sus palabras:
    "porque mejor no le damos unos 200 tokenes obligatorios para que se
    explique, que su explicacion queda en esos 200 tokens a 500 tokens
    que te parece?" -- es decir, subir el piso BASE de esta pasada en
    vez de seguir dependiendo de un reintento que paga una llamada
    extra cada vez que 250 no alcanza.

    Se aceptó la propuesta con un refinamiento: en vez de subir
    directamente `FOLLOWUP_DECISION_NUM_PREDICT` (que también se usa,
    sin relación con pedidos de explicación, como techo genérico de
    "¿sigo o ya está?" cuando NO hubo pedido explícito -- subirlo ahí
    gastaría de más en el caso común de "confirmación breve de 1-2
    frases", que ya funciona bien con 250), se agregó una constante
    NUEVA y separada, `FOLLOWUP_EXPLANATION_NUM_PREDICT = 500` (extremo
    alto del rango 200-500 que pidió el usuario), que solo se usa
    cuando `_wants_explicit_explanation` (la misma señal de
    `_EXPLICIT_EXPLANATION_REQUEST_RE`, ítem 38/`patch_orchestrator100`)
    es `True` para el turno. Se introdujo una variable local compartida,
    `_followup_base_num_predict`, calculada una sola vez justo antes de
    la llamada de cierre (`self.FOLLOWUP_EXPLANATION_NUM_PREDICT if
    _wants_explicit_explanation else self.FOLLOWUP_DECISION_NUM_PREDICT`)
    y reutilizada en dos lugares: (1) como techo de la propia pasada de
    cierre (en el mismo `if (_file_created_this_turn_path and
    wants_file_tools)` que ya existía desde `patch_orchestrator70`), y
    (2) como base del reintento del ítem 40, que ahora duplica
    `_followup_base_num_predict` en vez del `FOLLOWUP_DECISION_NUM_
    PREDICT` fijo de antes -- así, si el pedido explícito ya arrancó en
    500, el reintento (poco frecuente ahora) sube a 1000; si no hubo
    pedido explícito, el comportamiento queda IDÉNTICO al del ítem 40
    (250→500). El reintento del ítem 40 se mantiene sin más cambios
    como red de seguridad para el caso, ahora más raro, en que ni
    siquiera el techo base más grande alcance.

    Verificado en el sandbox: `ast.parse` → `SYNTAX_OK`, `pyflakes` sin
    warnings nuevos (mismos 6 preexistentes de siempre). Tamaño en el
    dispositivo confirmado en 1131619 bytes (el mismo baseline exacto
    del ítem 40) antes de tocar nada. Backup
    `orchestrator.py.bak88_pre_patch103`. Commit al dispositivo exitoso
    al primer intento (1131619→1135372 bytes), verificado byte a byte y
    con `ast.parse` del propio Python del dispositivo. Solo a
    `MonolitoPersonal`, como desde el ítem 34 -- nunca a
    `MonolitoPersonal-GitHub`. Pendiente de retest en vivo: repetir un
    turno de explicación larga/estructurada (idealmente similar al de
    "Lives" de más arriba) y confirmar que ya no se corta ni con el
    techo base ni necesita el reintento del ítem 40 para completarse.

42. **`patch_router2` (2026-09-19) — bug real, MEDIDO en vivo por el
    usuario, con el log completo del turno: sobre una conversación que
    ya venía editando `agar_io.py` en turnos previos (crear el juego,
    mejorarlo, agregarle bots agresivos), el turno de seguimiento "add
    random cool things to the game" disparó una búsqueda web COMPLETA
    (DuckDuckGo, Wikipedia, e imágenes de tema totalmente ajeno -- una
    hamburguesa, "search engine optimization") en vez de ir directo a
    `read_file`/`edit_file` sobre el archivo ya existente. El turno
    terminó bien igual (el modelo, con el historial de conversación
    a mano, llamó a `read_file` por su cuenta después de la búsqueda
    inútil), pero costó tokens/tiempo de más y contaminó el contexto
    del modelo con resultados irrelevantes -- posiblemente relacionado
    con el corte `finishReason=MALFORMED_FUNCTION_CALL` que sufrió la
    generación final de ese mismo turno, aunque eso no se confirmó
    puntualmente. El usuario, tras ver el log, pidió explícitamente:
    "tienes que blindar fuertemente ese aspecto porque el usuario se
    enojara cuando pase eso".

    Causa raíz: `router.py` ya tenía tres ejes de exclusión mutua contra
    `SignalTag.WEB_SEARCH_INTENT` (`CODE_COMPLEX`,
    `CONVERSATIONAL_FOLLOWUP`, `SUPERLATIVE_ACHIEVEMENT` -- ver
    `classify()`), pero ninguno cubría este caso. "game" es una de las
    `_WEB_KEYWORDS_RE` (pensada para "who won the game"/marcadores
    deportivos). `_CODE_COMPLEX_PATTERN` exige o un verbo de desarrollo
    explícito (implementa/mejora/optimiza/...) o el NOMBRE puntual del
    juego (snake/tetris/agario/...) -- "add ... to the game", sin
    nombrar el juego ni usar ninguno de esos verbos, no matcheaba nada
    de esa lista. Mismo patrón de fondo que los BLINDAJEs de "snake/
    pong/tetris" (2026-09-09) y `patch_router1` (asteroids, 2026-09-18)
    ya documentados en `router.py` -- pero esos dos se resolvieron
    agregando el NOMBRE puntual del juego a una lista cerrada, mientras
    que acá el pedido es genérico a propósito ("cosas geniales",
    "random cool things") y nunca iba a estar en ninguna lista cerrada
    de nombres.

    Fix: cuarto eje de neutralización, nuevo `SignalTag.
    GENERIC_EDIT_CONTINUATION` + `_GENERIC_EDIT_CONTINUATION_RE` en
    `router.py` -- un verbo de modificación genérico (add/put/include/
    insert/throw in, o sus equivalentes en español con pronombre
    enclítico: agrega/agregale/añade/incluye/suma/sumale/inserta/pon/
    ponle/mete/metele) seguido, a poca distancia (≤40 caracteres), de
    un objeto que es inequívocamente código/juego ya existente ("to/in/
    into the game/code/script", "al/en el juego/código/script"). A
    diferencia de `_CODE_COMPLEX_PATTERN`, este patrón NO requiere
    nombrar el juego puntual ni un verbo de programación explícito --
    por diseño, para cubrir justamente el caso genérico que una lista
    cerrada nunca va a poder enumerar por completo. Evaluado en paralelo
    a los otros tres ejes en `classify()`, con su propia nota de
    exclusión en el log (`[WEB_SEARCH_INTENT neutralizado por pedido
    genérico de edición sobre juego/código ya existente]`) para poder
    auditarlo después. No se tocó `_CODE_COMPLEX_PATTERN` ni ninguna de
    las listas de nombres de juego existentes -- señal independiente,
    sin relación con `SignalTag.CODE_COMPLEX` ni con
    `_turn_wants_file_tools`/presupuesto de codegen (`orchestrator.py`,
    ítem 39) -- alcance deliberadamente acotado al síntoma reportado
    (búsqueda web espuria), no una reingeniería del ruteo de archivos.

    Verificado con una batería de 12 casos en el sandbox antes de tocar
    el archivo real (frase reportada + variantes en inglés/español +
    5 negativos control: "add 2 and 3", "who won the game", "what's the
    score of the game", "add me to the meeting", "agregarme a la
    reunion" -- los 12 dieron el resultado esperado). `ast.parse` →
    `SYNTAX_OK`, `pyflakes` sin ningún warning (línea base de
    `router.py` ya estaba limpia, a diferencia de `orchestrator.py`).
    Backup `router.py.bak1_pre_patch_router2` (46553 bytes, tamaño
    preexistente confirmado antes de tocar nada -- este archivo no se
    había tocado en ninguna sesión previa desde `patch_router1`).
    Commit al dispositivo exitoso al primer intento (46553→51138
    bytes), verificado byte a byte y con `ast.parse` del propio Python
    del dispositivo. Solo a `MonolitoPersonal`, como desde el ítem 34.
    Pendiente de retest en vivo: repetir un turno de seguimiento
    genérico similar ("agregale cosas random al juego", "put something
    fun in the script") sobre un archivo ya existente y confirmar en el
    log que el turno va directo a `read_file`/`edit_file` sin ningún
    `[WEB_SEARCH]` de por medio.

43. **`patch_orchestrator104` (2026-09-19) — rediseño arquitectónico
    pedido explícitamente por el usuario, tras discutir 3 alternativas
    sobre el mismo bug de fondo de los ítems 41/42 (`patch_router1`/
    `patch_router2`): "hmm razona otras posibilidades" → se presentaron
    (1) seguir agregando regex al router (lo que ya se había hecho, y
    que el usuario correctamente identificó como whack-a-mole: "cada
    frase nueva que se te ocurra puede volver a fallar"), (2) un candado
    de estado de sesión usando `_most_recent_workspace_file()` para
    exigir una señal de actualidad más fuerte cuando hay un archivo
    tocado hace poco, y (3) convertir la búsqueda web en una herramienta
    agéntica real que el modelo pide por su cuenta, igual que ya hace
    con `read_file`/`write_file` -- eliminando la clase de bug de raíz
    en vez de seguir parcheando síntomas. El usuario eligió la opción 3
    sin dudar: "la 3 me parece perfecta".

    Causa raíz de fondo (documentada ya en los ítems 41/42, pero nunca
    resuelta hasta este patch): la búsqueda web NUNCA fue una
    herramienta agéntica -- vivía enteramente en un pipeline aparte
    (`_should_force_web_search`/`SignalTag.WEB_SEARCH_INTENT`, ver
    router.py) que decidía buscar o no ANTES de que el modelo viera el
    mensaje del usuario, con un router determinista y SIN memoria de la
    conversación adivinando en su lugar. Interesante nota histórica: la
    señal `WEB_SEARCH_INTENT` se había sumado a `_should_force_web_
    search` (ver el docstring de ese método, ahora reescrito) citando un
    pedido anterior del usuario, del 2026-09-18: "que la IA decida sola
    cuándo buscar, sin que yo tenga que forzarlo" -- pero la
    implementación real de aquel momento seguía siendo heurística, nunca
    el modelo decidiendo de verdad. Este patch por fin cumple ese pedido
    original de forma literal.

    Investigación previa a tocar código: se descubrió que `tools.py` y
    `web_search.py` (`src/tools/`) NO estaban mirroreados en el
    workspace de la nube de esta sesión (solo `orchestrator.py`,
    `router.py`, `ARCHITECTURE.md` y `sovnode_qt.py` lo estaban) --
    tuvieron que traerse primero vía `device_stage_files` antes de poder
    diseñar el cambio con el código real a la vista, en vez de adivinar
    su contenido.

    Cambios, todos en la MISMA sesión de trabajo:
    - **`CLOUD_TOOLS_SCHEMA`** (orchestrator.py): nueva entrada
      `web_search` (7ma herramienta), ubicada DESPUÉS de `list_dir` y
      ANTES de `run_cmd` -- dentro del prefijo ESTABLE que ningún
      `exclude_tools` saca nunca (solo `write_file` se excluye alguna
      vez, ver el BLINDAJE de `patch_orchestrator88`, §5.4) para no
      correr ningún `cache_control` existente. La descripción instruye
      explícitamente NO usarla para pedidos de código ("agregale cosas
      geniales al juego" es SIEMPRE edición, nunca búsqueda) y pedir una
      consulta concreta y autocontenida (el modelo resuelve pronombres
      usando la conversación, no un rewriter externo).
    - **`TOOLS_SCHEMA`** (tools.py): misma entrada, formato local (clave
      `parameters` en vez de `input_schema`), para que el protocolo
      JSON-en-texto del motor Local (`extract_tool_call`) también la
      vea en el dump de schema embebido en el prompt.
    - **`_get_fastpath_system_prompt`/`_get_file_ops_system_prompt`**
      (orchestrator.py): nuevo bloque de instrucción + ejemplo JSON para
      `web_search`, en los dos idiomas, en las dos cabeceras que arma el
      motor Local -- mismo formato que ya usan los bloques de
      `write_file`/`read_file`. Al editar `_get_fastpath_system_prompt`
      se detectó y corrigió un bug de copy-paste propio de este mismo
      patch (una duplicación accidental del bloque `if self.
      _workspace_tools_are_enabled(): ...` al insertar el nuevo bloque
      antes de él) -- verificado que solo queda UNA copia con
      `grep -c`.
    - **`execute_tool_from_call`** (orchestrator.py): firma ampliada con
      `lang`/`log_cb` opcionales (default `None`, cero cambio para
      cualquier llamador que no los pase); nueva rama `if tool_name ==
      "web_search": result = self._execute_web_search_tool_call(...)`
      ANTES del `self.tools.execute(tool_name, **params)` genérico --
      `web_search` no vive en `LocalToolDispatcher` (no necesita sandbox
      de disco/comandos). El guardia preexistente `_WEB_SEARCH_TOOL_
      ALIASES` (pensado para la ÉPOCA en que la búsqueda web NO era una
      herramienta real, y que de otro modo le diría al modelo "no existe
      tal herramienta") queda intacto pero inerte para un llamado real:
      solo dispara sobre `isinstance(result, dict)`, y el resultado de
      `_execute_web_search_tool_call` siempre es un string.
    - **`_execute_web_search_tool_call`** (orchestrator.py, método
      nuevo): reusa `search_web_context` (web_search.py, ya importado a
      nivel de módulo desde antes) -- a propósito NO reusa
      `_build_contextual_search_query` (el rewriter de 3 capas que
      resuelve pronombres/deixis para compensar la falta de contexto del
      router; acá el llamador YA ES el modelo con la conversación
      completa en la cabeza, pedirle una consulta autocontenida en el
      schema alcanza). Nunca lanza ni devuelve un dict -- siempre texto,
      con mensajes de error claros (falta `query`, sin resultados) que
      el modelo puede leer y actuar en consecuencia (reformular, o
      responder con lo que ya sabe aclarando que no pudo confirmarlo).
    - **`normalize_tool_call`** (orchestrator.py): nuevo bloque `web_
      search_aliases`, mismo patrón que los 4 bloques de alias
      preexistentes (`telemetry_aliases`, etc.) -- normaliza sinónimos
      razonables ("search_web", "buscar", "browse", ...) al nombre
      canónico `web_search`, para que un modelo Local que no recuerde el
      nombre exacto igual dispare la herramienta real en vez de caer en
      el guardia de "alucinación".
    - **`_should_force_web_search`** (orchestrator.py): se retira
      `SignalTag.WEB_SEARCH_INTENT` de la condición que fuerza una
      búsqueda pre-emptiva -- el modelo ya tiene la herramienta real, no
      hace falta que un router stateless siga adivinando por él, y esa
      señal era justamente la responsable de la clase de bug de los
      ítems 41/42. `SignalTag.FACTUAL_ENUMERATION` se MANTIENE forzando
      a propósito -- BLINDAJE explícito en el docstring reescrito: es un
      bug estructuralmente DISTINTO (un modelo local chico puede no
      saber que no sabe un dato mal memorizado -- ningún tool-calling
      arregla eso, porque el modelo nunca va a pedir ayuda para algo que
      cree saber bien; la única corrección posible sigue siendo inyectar
      evidencia externa de antemano, sin esperar a que la pida). El
      resto del pipeline de búsqueda pre-emptiva (prefetch en paralelo,
      inyección en el prompt antes de generar, ver §5.4) queda intacto,
      sin tocarse, para ese caso y para `requested=True` explícito.
    - `router.py` (`_CODE_COMPLEX_PATTERN`/`_GENERIC_EDIT_CONTINUATION_
      RE`, ítems 29/42) queda SIN TOCAR -- sigue siendo la señal real
      que decide fast_path/slow_path y sigue siendo útil, pero deja de
      ser el único mecanismo intentando adivinar si hace falta buscar.

    Verificado en el sandbox: `ast.parse` → `SYNTAX_OK` en
    `orchestrator.py` Y `tools.py`; `pyflakes` sin warnings nuevos en
    ninguno de los dos (orchestrator.py: mismos 6 preexistentes de
    siempre, con números de línea corridos; tools.py: cero warnings,
    igual que antes). Se extrajeron `CLOUD_TOOLS_SCHEMA` y `TOOLS_SCHEMA`
    con `ast.literal_eval` (no solo `ast.parse`) para confirmar
    programáticamente que las 7 herramientas están, en el orden
    correcto, con el schema exacto esperado -- no solo que el archivo
    "parsea", sino que el dato real quedó bien formado. Ambos archivos
    (`orchestrator.py`, `tools.py`) respaldados en el dispositivo antes
    de tocarse (`orchestrator.py.bak89_pre_patch104`,
    `tools.py.bak_pre_patch104`) y commiteados solo a `MonolitoPersonal`,
    verificados byte a byte y con `ast.parse` del propio Python del
    dispositivo tras el commit.

    Pendiente de retest en vivo (el más importante de toda esta racha de
    patches): repetir turnos como "add random cool things to the game"
    (el bug original de `patch_router2`) y confirmar en el log que el
    modelo NUNCA dispara `[WEB_SEARCH]` para ellos -- y, por separado,
    probar un pedido genuino de actualidad ("cuál es el precio del dólar
    hoy", "quién ganó el partido de anoche") DURANTE una sesión de
    edición de código activa y confirmar que el modelo SÍ llama a `web_
    search` por su cuenta cuando corresponde, sin que haga falta ningún
    botón ni forzado manual -- la prueba real de que "la IA decide sola"
    dejó de ser una heurística y pasó a ser una decisión agéntica
    genuina.

44. **`patch_orchestrator105` (2026-09-19) — código incompleto en el
    carril de chat (herramientas de workspace desactivadas), dos fixes
    complementarios pedidos explícitamente por el usuario.** Reportado
    con captura + log: "Give me a base game of slither.io with bots" con
    las herramientas de workspace DESACTIVADAS devolvió código cortado a
    mitad de una definición de clase, pese a que el propio modelo
    afirmaba en su texto que el juego estaba "complete... with full game
    logic". Pedido textual del usuario: *"porque al generar codigo
    genera un codigo imcompleto? Hagamos algo, si no puede con el
    esfuerzo bajo que lo suba a medio para generar el codigo base y si
    se le pide mejoras a ese codigo que explique detalladamente el
    codigo que hace y donde introducirlo en el codigo base porque
    obviamente si se le pide que mejore el codigo sin el workspace va a
    reescribirlo todo con la mejora y lo que yo quiero es que nos
    ajustemos a los tokens de salida"*.

    Diagnóstico (investigación, sin reproducir en vivo -- se leyó el
    código, no se corrió el motor): `run_turn` ya calcula
    `_min_viable_floor` (`_estimate_min_viable_codegen_tokens`, el mismo
    piso técnico usado en todo el resto de esta racha de patches,
    `_MIN_VIABLE_FLOOR_PATTERNS`) para CUALQUIER turno que quiera
    presupuesto de codegen, carril de chat incluido -- pero el
    mecanismo que EVALÚA ese piso contra el techo real y eleva de nivel
    si hace falta (`_elevate_codegen_ceiling_if_needed`, ya en
    producción desde `patch_orchestrator82/83` para `write_file` de
    archivo nuevo) estaba condicionado a `if is_file_write and not
    mod_is_modify:` -- una condición que, con las herramientas de
    workspace apagadas, es SIEMPRE falsa (`_is_file_write_turn` devuelve
    `False` de entrada, ver `_turn_wants_file_tools`). El carril de
    "código en el chat" nunca pasaba por la elevación: con "Bajo"
    (~900tok) seleccionado, el techo duro real que la API iba a cortar
    seguía siendo 900tok pase lo que pase -- el bloque `[OUTPUT BUDGET]`
    (línea ~4057 antes del patch) solo le pedía al modelo por texto que
    "recorte alcance", pero ningún recorte de alcance salva un techo por
    debajo del piso técnico mínimo de un juego con bots. El propio
    comentario BLINDAJE junto a la condición original ya documentaba
    esta asimetría ("(a) archivo nuevo tenía el chequeo, el carril de
    chat no") como una decisión consciente de no tocar en su momento --
    este patch es exactamente esa extensión, ahora pedida explícitamente.

    Fix parte (a) -- elevación extendida al carril de chat: se agregó
    una rama `elif not is_file_write:` (nunca toca la rama `if
    is_file_write and not mod_is_modify:` original, que sigue
    exactamente igual) inmediatamente después de ella, en `run_turn`
    (`orchestrator.py`, ~línea 3868-4116 tras el patch). Reutiliza
    literalmente el mismo mecanismo (`_elevate_codegen_ceiling_if_needed`,
    `_CLOUD_OUTPUT_BUDGET_CENTS_LABELS`, el mismo formato de mensaje
    `[LOG]` avisando "presupuesto ampliado/elevado solo este turno,
    selector guardado sin cambios") -- eleva el techo real SOLO cuando
    "Bajo" no alcanza ni para el piso mínimo de la categoría detectada
    (juego/script/etc.), nunca de forma incondicional (respeta "que nos
    ajustemos a los tokens de salida": si el pedido entra en el
    presupuesto elegido, no se toca nada). Si ni el nivel más alto
    ("Alto"/"Extra") alcanza, cae en el mismo bailout de
    `_codegen_budget_infeasible` ya existente (cero tokens de LLM
    gastados en un intento condenado a cortarse).

    Fix parte (b) -- "explicar y ubicar" en vez de reescribir todo en
    seguimientos de mejora: se descubrió que este mecanismo YA EXISTÍA
    en gran parte (`patch_orchestrator37` y refinamientos posteriores,
    §8 más arriba) -- cuando un turno reinyecta `_last_generated_code_
    text` (el código generado en la respuesta anterior) y ese código es
    grande respecto al presupuesto del turno actual (`_suggestion_mode_
    active`, umbral `_SUGGESTION_SIZE_SAFETY_RATIO = 0.6`), el prompt ya
    le instruye al modelo explícitamente: "NO reescribas el archivo
    entero... describí la implementación sugerida... especificá
    EXACTAMENTE en qué parte del código existente va". El bug real no
    era la ausencia del mecanismo sino su condición de entrada: todo el
    bloque (reinyección + evaluación de modo sugerencia) exigía
    `SignalTag.CODE_COMPLEX in decision.tags` -- la MISMA señal del
    router que `patch_router2` (ítem 42) ya demostró que NO dispara para
    un seguimiento genérico como "agrega cosas geniales al juego"
    (exige un verbo de programación explícito o un juego nombrado por
    `_CODE_COMPLEX_PATTERN`). Un pedido típico de seguimiento como
    "mejora este código" / "arreglalo" / "optimizalo" podía caer
    igual de afuera, dejando al modelo sin el código anterior
    reinyectado y sin ninguna instrucción de "no reescribas todo" --
    exactamente el riesgo que el usuario describió ("sin el workspace va
    a reescribirlo todo"). Fix: se agregó `self._FILE_MODIFY_VERB_RE.
    search(user_input)` (la misma regex ya probada en producción,
    usada para `_resolve_modify_target`/`mod_is_modify`) como condición
    ALTERNATIVA a `SignalTag.CODE_COMPLEX` (con `or`, no reemplazo --
    ningún turno que ya entraba sigue entrando igual). El umbral de
    tamaño que decide si `_suggestion_mode_active` se activa (0.6 del
    presupuesto) no se tocó -- sigue siendo el mismo cálculo ya
    calibrado; este fix solo asegura que el turno LLEGUE a evaluarlo.

    Verificación: `ast.parse` limpio en ambas ediciones (cloud y
    dispositivo, `python3` del propio dispositivo). `pyflakes` sobre
    `orchestrator.py` sigue dando exactamente los mismos 6 warnings
    preexistentes (líneas corridas por las inserciones, mismo
    contenido: `rag_faiss.chunk_document` sin usar, redefinición de
    `requests`, dos f-strings sin placeholders, `Skeleton` indefinido,
    `wants_file_tools` sin usar) -- cero warnings nuevos. `router.py` y
    `tools.py` (sin tocar este patch) siguen en cero warnings. Tamaño de
    `orchestrator.py`: 1151394 → 1160114 bytes (+8720). Backup en
    dispositivo (`orchestrator.py.pre_patch105.bak`, 1151394 bytes,
    mismo tamaño que el baseline) antes de commitear, commit solo a
    `MonolitoPersonal` (nunca `MonolitoPersonal-GitHub`), verificado
    byte a byte (`wc -c` en dispositivo: 1160114, igual al mirror de la
    nube) y con `ast.parse` del propio Python del dispositivo tras el
    commit.

    Pendiente de retest en vivo (ambas partes, con las herramientas de
    workspace DESACTIVADAS y "Bajo" seleccionado): (a) repetir "Give me
    a base game of slither.io with bots" y confirmar en el log el
    mensaje `[BUDGET]`/`[PRESUPUESTO]` mostrando la elevación de "Bajo"
    a un nivel superior cuando el piso mínimo no entra, y que el código
    resultante compile/corra completo, sin cortarse a mitad de clase;
    (b) sobre ESE mismo juego ya generado, pedir un seguimiento genérico
    tipo "mejora este código" / "agregale más cosas" y confirmar que la
    respuesta NO reescribe el archivo completo sino que explica la
    mejora en prosa y especifica exactamente dónde pegarla sobre el
    código ya mostrado.

45. **`patch_orchestrator106` (2026-09-19) — el modo sugerencia
    (parte (b) de `patch_orchestrator105`) no se activaba con "Medio"
    seleccionado, bug real MEDIDO en el primer retest en vivo del
    usuario.** Contexto: el usuario confirmó que la parte (a) de
    `patch_orchestrator105` (elevación Bajo→Medio) funcionó bien --
    "el primer codigo funciono bien" (un juego base tipo Doom, generado
    completo con Cloud/Gemini y "Medio" ya seleccionado a mano). Pero al
    pedir un seguimiento ("give me an improve of the code because there
    is no map"), la respuesta volvió a intentar reescribir el archivo
    entero (agregando un minimapa) y se cortó a mitad de una línea
    (`corrected_depth = depth * math.cos(player_angle -`), con el log
    mostrando `ceiling=4800tok`/`2228tok de salida real` -- es decir, NO
    fue un corte por techo agotado (2228 < 4800), sino que el modelo
    dejó de generar a mitad de camino igual. El usuario señaló
    correctamente que esto contradice el plan ya acordado en
    `patch_orchestrator105`: *"ya te dije como hibamos a implementar el
    mejorar codigo cuando se le pida cierto?"*.

    Causa raíz: el mecanismo de modo sugerencia SÍ se alcanzaba
    (`_last_generated_code_text` se reinyectaba, "improve" matcheó
    `_FILE_MODIFY_VERB_RE`), pero `_suggestion_mode_active` (la bandera
    que de verdad le dice al modelo "no reescribas, explicá y ubicá")
    solo se ponía en `True` si el código anterior era GRANDE respecto al
    presupuesto de ESTE turno (`_SUGGESTION_SIZE_SAFETY_RATIO=0.6` sobre
    `_codegen_char_budget_estimate`). Con "Bajo" ese mismo código
    generado hubiera activado el modo sin problema -- pero con "Medio"
    (el presupuesto sube) la MISMA cantidad de código deja de verse
    "grande" en proporción, así que el sistema decidía que una
    reescritura completa "entraba" en el presupuesto. Resultado:
    exactamente lo opuesto de lo pedido, y potencialmente PEOR cuanto
    más alto el tier elegido (más "confianza" del sistema en que puede
    reescribir todo, más riesgo real de que el modelo igual se corte por
    las razones de siempre -- longitud real del programa, no solo
    tokens).

    Fix: en `run_turn` (`orchestrator.py`, bloque `elif` de reinyección
    de `_last_generated_code_text`, el mismo que `patch_orchestrator105`
    ya había ampliado con `_FILE_MODIFY_VERB_RE`), se saca la condición
    de tamaño para este caso -- `_suggestion_mode_active` se activa
    INCONDICIONALMENTE apenas se entra a esta rama. Razonamiento: la
    rama ya sólo se alcanza cuando `not is_file_write` (este turno no
    tiene `write_file`/`edit_file` disponible) Y hay código generado
    antes que reinyectar Y el pedido suena a mejora -- en ese escenario
    exacto, cualquier intento de "reescribir todo" significa repetir el
    programa entero como texto plano de chat, sin importar cuán alto sea
    el techo de tokens elegido: el riesgo de corte no depende de la
    proporción código-viejo/presupuesto-nuevo, depende de que no hay
    ninguna herramienta de archivo para este turno. El bloque `if`
    hermano (código PEGADO por el usuario, más arriba en la misma
    función) NO se tocó -- sigue usando el umbral de tamaño de
    `_SUGGESTION_SIZE_SAFETY_RATIO`, caso distinto que no fue parte de
    este reporte.

    Verificación: `ast.parse` limpio (cloud + dispositivo). `pyflakes`
    sigue en los mismos 6 warnings preexistentes, cero nuevos.
    `orchestrator.py`: 1160114 → 1162505 bytes (+2391). Backup en
    dispositivo (`orchestrator.py.pre_patch106.bak`, 1160114 bytes,
    coincide con el baseline post-`patch_orchestrator105`) antes de
    commitear, commit solo a `MonolitoPersonal`, verificado byte a byte
    (1162505 en ambos lados) y con `ast.parse` del propio Python del
    dispositivo tras el commit.

    Pendiente de retest en vivo: repetir un seguimiento de mejora
    ("agregale un minimapa", "mejora este código") sobre un juego base
    ya generado en el chat (workspace desactivado), con CUALQUIER tier
    de presupuesto seleccionado (Bajo, Medio o Alto) -- confirmar que la
    respuesta ahora explica en prosa qué cambiar y dónde, en vez de
    reescribir el archivo completo, y que el log del turno refleja
    `large_generated_code_suggestion_mode` con `unconditional=True`.

46. **`patch_qt73` (2026-09-19) — persistir el idioma elegido en la UI
    entre sesiones, pedido explícito del usuario: *"añade que el
    lenguaje se guarde el ultimo que elegiste, es engorroso ser ingles y
    cambiar a cada rato de idioma"*.** `sovnode_qt.py` (`MainWindow.
    __init__`) arrancaba SIEMPRE con `self._current_lang = "Español"`
    hardcodeado, sin importar qué idioma hubiera elegido el usuario la
    sesión anterior en `self.combo_lang` -- y `_on_lang_changed` (el
    handler del combo) actualizaba `self._current_lang`/llamaba a
    `self.orchestrator.set_language(...)` pero nunca persistía nada.
    Fix: mismo mecanismo ya establecido para otros toggles de la UI
    (`_on_workspace_tools_toggled`/`_on_run_cmd_toggled`/`_on_advanced_
    terminal_toggled`, todos vía `QSettings("SovNode", "SovNode")`) --
    nueva clave `ui/language`. Se guarda en `_on_lang_changed` (único
    lugar donde el usuario cambia de idioma de verdad) y se lee en
    `__init__`, reemplazando el literal hardcodeado, con `type=str` +
    fallback a "Español" (cubre primera corrida sin valor guardado) y un
    chequeo de membresía contra `("Español", "English")` (cubre un
    valor corrupto o de otra versión en el Registro). No hizo falta
    tocar nada más: `self._current_lang` ya se consume tal cual más
    abajo en `__init__` (`combo_lang.setCurrentText`, `self.orchestrator.
    set_language`, todos los textos vía `I18N[self._current_lang]`), así
    que restaurar el valor ahí basta para que se propague solo.

    **Gotcha real, MEDIDO de nuevo en este mismo patch** (ya documentado
    arriba, ítem "riesgos conocidos" de `device_commit_files`): el
    PRIMER `device_commit_files` de `sovnode_qt.py` devolvió éxito
    (`{"written":[...],"rejected":[]}`) pero el archivo en el
    dispositivo quedó con el contenido VIEJO -- esta vez el chequeo de
    bytes NO lo detectó (1925 bytes de más, coincidencia: el fix
    original usaba `patch_qt67` como nombre, que YA estaba en uso en
    otras 5 BLINDAJE de este mismo archivo para un fix no relacionado de
    una sesión anterior -- renombrar a `patch_qt73` no cambió el conteo
    de bytes, mismo largo de string). Se detectó SOLO porque el paso de
    verificación buscó el marcador de texto `patch_qt73` de por sí (no
    solo el tamaño) y no lo encontró en el dispositivo pese al byte
    count coincidiendo -- recordatorio en vivo de por qué el protocolo
    exige buscar el marcador del parche, no solo el tamaño. Reintentado
    con `force: true` (como documenta el ítem de arriba), esta vez sí
    prendió: verificado `grep -c "patch_qt73"` = 2 (los dos bloques
    agregados) y `grep -c "patch_qt67"` = 5 (los preexistentes, sin
    tocar) en el dispositivo, más `ast.parse` limpio.

    Aparte, se detectó al investigar que `sovnode_qt.py` YA tiene
    patches numerados hasta `patch_qt72` (no `patch_qt65`, que era lo
    último visto/documentado en una revisión anterior de este
    documento) -- este archivo evolucionó en sesiones intermedias no
    reflejadas todavía acá. Próxima sesión que toque `sovnode_qt.py`:
    el próximo número libre es `patch_qt74` en adelante, verificar con
    `grep -oE "patch_qt[0-9]+" sovnode_qt.py | sed 's/patch_qt//' | sort
    -n -u | tail` antes de asumir cualquier número.

    Verificación: `ast.parse` limpio (cloud + dispositivo). `pyflakes`
    sobre `sovnode_qt.py` da UN solo warning preexistente, sin relación
    con este cambio (`redefinition of unused '_CancelToken' from line
    3803`, ya presente en el baseline del dispositivo antes de tocar
    nada) -- cero warnings nuevos. Tamaño: 390628 → 392553 bytes
    (+1925). Backup en dispositivo (`sovnode_qt.py.pre_patch_qt67.bak`
    -- nombre de archivo con el número viejo, cambiado de idea después,
    pero el CONTENIDO es el baseline correcto pre-patch, 390628 bytes,
    verificado) antes de commitear, commit solo a `MonolitoPersonal`.

    Pendiente de retest en vivo: cambiar el idioma con el selector,
    cerrar SovNode por completo y volver a abrirlo -- confirmar que
    arranca en el idioma recién elegido, no en "Español" por defecto.

47. **`patch_orchestrator107` (2026-09-19) — `_dedupe_enumeration_items`
    cortaba en falso el modo sugerencia de `patch_orchestrator106`, bug
    real MEDIDO por el usuario en el retest inmediato del ítem 44/45.**
    Reportado con captura + log: "improve this code" sobre `agar_io.py`
    (sin workspace) respondió con "three targeted improvements", pero la
    respuesta visible se cortaba justo al arrancar el ítem 2 ("2.
    Optimize Collision Calculations...") sin ningún texto debajo -- el
    log mostró `Duplicate/degenerate enumeration items trimmed.` y el
    turno terminó "sin problemas" pese a NO haber agotado el techo de
    tokens (1733tok de 6384tok). El usuario notó que un primer intento
    (turno anterior, antes de cerrar la conversación) había salido bien.

    Causa raíz: el fix de `patch_orchestrator106` (recién shippeado en
    este mismo turno) hizo que `_suggestion_mode_active` -- el modo
    "explicá y ubicá en vez de reescribir" -- se activara SIEMPRE para
    un seguimiento de mejora sin workspace. Ese modo le pide al modelo
    estructurar cada mejora con sub-etiquetas repetidas ("**Issue**:",
    "**Solution**:", "**Exact Location**:", una vez por ítem). Pero
    `_dedupe_enumeration_items` (blindaje preexistente, ver ítem/BLINDAJE
    original junto a `_DEGENERATE_REPEAT_RE` -- pensado para un bug
    real de modelos locales tipo gpt-oss repitiendo el título de UN
    ítem en bucle sin avanzar) cortaba la respuesta apenas el MISMO
    texto en negrita-con-dos-puntos aparecía DOS VECES en todo el
    texto, sin distinguir "el modelo se atascó repitiendo el mismo
    ítem" (el bug real que motivó este blindaje) de "el modelo avanzó a
    un ítem nuevo y reusó la misma sub-etiqueta de estructura" (este
    caso, sano) -- con el modo sugerencia ahora activado por defecto,
    CUALQUIER explicación de 2+ mejoras con ese formato iba a cortarse
    siempre en el segundo ítem. Bug preexistente, pero invisible hasta
    ahora porque antes de `patch_orchestrator106` este modo casi nunca
    se activaba para más de un ítem seguido.

    Fix: se agregó `_TOP_LEVEL_ITEM_MARKER_RE` (encabezado Markdown
    `#`-`######`, o el arranque de un ítem numerado/con viñeta) y se
    exige que, entre la aparición anterior de un título repetido y la
    actual, haya aparecido al menos UN marcador de ítem nuevo para
    NO cortar -- si no apareció ninguno (el título se repite sin que
    haya arrancado nada nuevo en el medio, el patrón real del bug
    original), corta exactamente igual que antes. Probado con un script
    standalone: (1) una estructura legítima de 2 ítems con Issue/
    Solution/Exact Location repetidos -- NO corta, texto idéntico
    preservado; (2) un bucle degenerativo real (mismo título repetido
    3 veces SIN ningún marcador de ítem nuevo en el medio) -- corta
    igual que la versión anterior del método. No se tocó
    `_DEGENERATE_REPEAT_RE`/`_looks_degenerate_repetition` (mecanismo
    de bucle a nivel de caracteres, problema distinto).

    Verificación: `ast.parse` limpio (cloud + dispositivo). `pyflakes`
    sigue en los mismos 6 warnings preexistentes de siempre, cero
    nuevos. `orchestrator.py`: 1162505 → 1166280 bytes (+3775). Backup
    en dispositivo (`orchestrator.py.pre_patch107.bak`, 1162505 bytes,
    coincide con el baseline post-`patch_orchestrator106`) antes de
    commitear, commit solo a `MonolitoPersonal`, verificado byte a byte
    Y con el marcador de texto `patch_orchestrator107` (no solo el
    tamaño -- ver el gotcha de `device_commit_files` documentado más
    arriba, que en el patch anterior coincidió en bytes con contenido
    viejo) y `ast.parse` del propio Python del dispositivo tras el
    commit.

    Pendiente de retest en vivo: repetir "improve this code" (o
    cualquier seguimiento de mejora genérico) sobre un juego ya
    generado, sin workspace, y confirmar que TODOS los ítems de la
    respuesta (no solo el primero) aparecen completos, sin cortarse al
    empezar el segundo/tercero, y que el log YA NO muestra "Duplicate/
    degenerate enumeration items trimmed" para este tipo de turno
    (seguiría apareciendo, correctamente, ante un bucle degenerativo
    real).

48. **`patch_orchestrator108` (2026-09-19) — recomendar activamente el
    workspace para seguimientos de mejora/reparación sin archivo real,
    pedido explícito del usuario tras confirmar que `patch_orchestrator107`
    funcionó.** El usuario, viendo que las 3 mejoras ya salían completas
    (screenshot confirmando el fix del ítem 47), probó un seguimiento
    AMBIGUO sobre esa misma respuesta ("i want to implement the 3rd
    point, how i do it?") -- sin repetir "mejora"/"fix"/etc. y sin
    workspace, el modelo intentó `run_cmd` dos veces sin avanzar
    ("Bucle de herramientas: 'run_cmd' se repitió sin avanzar --
    abortando") y terminó pidiéndole al usuario que reformule. El
    usuario propuso la solución él mismo: *"mejor implementa una via en
    donde siempre que se quiera mejorar, reparar, o modificar se
    sugiera guardarlo y encender el workspace y poner la carpeta donde
    esta dicho codigo para empezar a analizarlo y modificarlo"* --
    razonando correctamente que seguir estirando el modo sugerencia
    (explicar+ubicar en texto) tiene un techo real: en algún punto el
    usuario tiene que copiar/pegar código a mano turno tras turno, y un
    seguimiento ambiguo sobre ESE texto puede hacer que el modelo
    improvise con la única herramienta que tenga a mano (acá, `run_cmd`
    sin `write_file`/`edit_file`) en vez de simplemente re-explicar.

    Dos piezas, ambas en `orchestrator.py`:

    (a) **`_FILE_MODIFY_VERB_RE` no cubría "implement"**: el turno real
    que disparó el reporte ("i want to implement the 3rd point") no
    matcheaba ningún verbo de la lista -- ni `mod_is_modify` ni el
    bloque de reinyección de `_last_generated_code_text`
    (`patch_orchestrator105`/`106`) se activaban, así que el modelo ni
    siquiera tenía el código anterior en contexto para ese turno. Se
    agregó `implement\w*` a la lista (una sola raíz cubre inglés
    "implement/implementing/implementation" Y español "implementa/
    implementar/implementación", mismo prefijo compartido).

    (b) **Recomendación determinística de activar el workspace**: el
    aviso que ya existía en el system prompt (`_workspace_tools_block_
    en/es`, ítem previo de este documento) -- "mention once that the
    user can enable workspace tools" -- es una instrucción SUAVE que
    depende de que el modelo decida seguirla; el propio log de este
    reporte demuestra que NO se puede confiar en eso justo cuando más
    hace falta (el modelo optó por `run_cmd` en bucle en vez de
    sugerir nada). Se agregó `_build_workspace_upsell_tip(lang)`
    (nuevo método, cerca de `_build_suggestion_mode_note`) y se la
    engancha en `run_turn` INMEDIATAMENTE después de `_verify_and_fix_
    python_syntax` (el punto de convergencia final de cualquier turno
    antes de mostrarse) y antes de capturar `_last_generated_code_text`:
    si el turno activó modo sugerencia (`_suggestion_mode_active`) Y el
    workspace sigue apagado, se APÉNDICE texto fijo al final de
    `final_response` -- no un `[SYSTEM NOTE: ...]` (ese formato es para
    instruir al modelo en el prompt de ENTRADA), sino texto plano que
    el usuario lee, recomendando activar herramientas de archivo, decir
    la carpeta, y pedir que el código se guarde ahí para que los
    próximos seguimientos se resuelvan con `edit_file` real. Al ser
    determinístico (código, no una instrucción que el modelo puede
    ignorar), aparece SIEMPRE que corresponde, sin depender de qué tan
    cooperativo esté el modelo ese turno en particular -- exactamente
    lo que el usuario pidió con "siempre que se quiera mejorar, reparar,
    o modificar se sugiera".

    No se tocó el aviso suave preexistente en `_workspace_tools_block_
    en/es` (sigue ahí, como refuerzo adicional para turnos que ni
    siquiera llegan a modo sugerencia) ni ninguna otra parte del
    pipeline de tool-calling (`run_cmd` sigue funcionando igual para lo
    que sí sabe hacer).

    Verificación: `ast.parse` limpio (cloud + dispositivo). `pyflakes`
    sigue en los mismos 6 warnings preexistentes, cero nuevos. Probado
    con un script standalone que la nueva raíz `implement\w*` matchea
    "i want to implement the 3rd point" y variantes en español, sin
    romper los casos negativos de control ya probados en
    `patch_orchestrator92`. `orchestrator.py`: 1166280 → 1172140 bytes
    (+5860). Backup en dispositivo (`orchestrator.py.pre_patch108.bak`,
    1166280 bytes, coincide con el baseline post-`patch_orchestrator107`)
    antes de commitear, commit solo a `MonolitoPersonal`, verificado
    byte a byte Y con el marcador `patch_orchestrator108` (3 apariciones)
    y `ast.parse` del propio Python del dispositivo tras el commit --
    primer intento de `device_commit_files` sí prendió esta vez (sin
    necesidad de `force: true`).

    Pendiente de retest en vivo: repetir un seguimiento de mejora sin
    workspace (cualquier verbo, incluido "implement"/"implementa") sobre
    un código ya mostrado, y confirmar que la respuesta termina con la
    recomendación de activar el workspace -- y, aparte, confirmar que
    "i want to implement the [N] point" específicamente ya NO cae en el
    bucle de `run_cmd` sino que reinyecta el código anterior y explica/
    ubica el cambio en prosa como cualquier otro seguimiento de mejora.

49. **`patch_orchestrator109` (2026-09-19) — dejar de forzar el motor
    Local para terminar un `write_file`/`edit_file` cortado cuando Cloud
    está activo, pedido explícito del usuario tras un turno real medido
    ("Create an mario game in the workspace", workspace ON, Cloud/
    Gemini activo).** La pasada de Cloud completó en ~28s (normal) pero
    pegó contra el presupuesto de Sonnet por turno a mitad de
    `write_file`; el rescate que ya existía (`_continue_truncated_file_
    write_locally`, `patch_orchestrator43`-`60`) forzó el motor Local a
    propósito (`force_local=True`, "termina gratis") -- y en el hardware
    real del usuario (GPU no soportada por ROCm en Windows, Ollama en
    CPU) esa "mitad gratis" tardó más de 150 SEGUNDOS sin terminar. El
    usuario tuvo que detener la generación a mano: *"se demoro mas de
    150 segundos, lo detuve porque no quiero por esa razon
    estrictamente que se vaya a usar el modelo local, deberiamos
    eliminar eso la verdad, arruina la experiencia al demorarse
    mucho."*

    Esto revierte, a propósito, el pedido explícito ANTERIOR del mismo
    usuario (2026-09-15, ver el docstring de `_continue_truncated_file_
    write_locally`) que motivó construir este mecanismo en primer
    lugar: "pagar una segunda pasada completa en Cloud ... gasta el
    doble del presupuesto elegido -- el resto se termina gratis [en
    Local]". Ambos pedidos son legítimos, pero apuntan a hardware
    distinto -- en el hardware real del usuario, terminar "gratis" en
    Local no es gratis: cuesta minutos de espera con el turno colgado,
    mucho peor que la fracción de centavo extra que cuesta terminarlo
    en Cloud (15-30s medidos en la misma sesión, en llamadas de Cloud
    de este mismo turno). El pedido más reciente pesa más que el
    anterior: la prioridad pasa a ser VELOCIDAD sobre el ahorro
    marginal de esa segunda pasada.

    Un solo cambio central en `orchestrator.py`: nuevo método
    `_codegen_rescue_should_force_local()` (justo antes de
    `_continue_truncated_file_write_locally`) que devuelve `False`
    (no forzar Local) cuando Cloud está activo y configurado
    (`cloud_backend_enabled` + `cloud_api_key`, los mismos flags de
    sesión que ya usa el resto del archivo, ej. `_reported_model_used`)
    y `True` (comportamiento original, sin otra opción real) cuando no
    lo está. Reemplazados los 4 sitios reales de `force_local=True`
    (los únicos que de verdad se invocan hoy -- ver abajo) por
    `force_local=self._codegen_rescue_should_force_local()`:
    - `_continue_truncated_file_write_locally`: `LocalTargetedUpdate`
      (actualización dirigida sobre un archivo existente),
      `LocalFreshWrite` (archivo nuevo desde cero), `LocalContinuation`
      (continuar el `content` ya cortado a mitad).
    - `_rescue_truncated_edit_file_locally`: `LocalEditFileRescue`
      (rehacer un `edit_file` cortado desde cero).

    Como los 4 sitios reales solo se alcanzan hoy desde ramas de
    `run_turn` donde `_cloud_active`/`_cloud_active_inloop` ya es
    `True` (confirmado por grep de todos los call sites), el efecto
    práctico es simple: esas 4 llamadas de rescate pasan de "siempre
    Local" a "siempre Cloud" mientras Cloud esté prendido. Se
    actualizaron los 4 mensajes `[LOG]` correspondientes en `run_turn`
    (dos variantes -- antes y dentro del bucle de herramientas -- para
    `write_file` y `edit_file` cada una) de "finishing the file locally
    (Ollama, no extra API cost)" a "finishing the file with a second
    Cloud pass (fast) instead of waiting on the local engine" (y su
    equivalente en español), para que el mensaje visible coincida con
    lo que de verdad va a pasar.

    **NO se tocó** `_continue_truncated_chat_code_locally` (su propio
    `force_local=True`, `perf_label="LocalChatContinuation"`) -- ese
    método está `RETENIDO SIN INVOCAR` desde `patch_orchestrator43`
    (pedido explícito del usuario, 2026-09-16, "elimina completamente
    ese mecanismo de continuación local" para el lane de chat sin
    archivo) y no tiene ningún call site real (confirmado por grep);
    tocar código muerto no cambia nada en runtime y solo agrega riesgo.
    Tampoco se tocaron los otros dos `force_local=True` del archivo
    (`Router0.5B` ~línea 13783, `VerifyLite` ~línea 19584): son modelos
    livianos de clasificación/corrección que se fuerzan a Local A
    PROPÓSITO por diseño (nunca pasaron por Cloud, no son parte de este
    mecanismo de rescate de codegen truncado).

    Se implementó como chequeo explícito de `cloud_backend_enabled`/
    `cloud_api_key` (no una constante `False` fija) para seguir
    protegiendo a un usuario SIN Cloud configurado -- ese caso sigue
    terminando gratis en Local exactamente como antes, sin cambios.

    Verificación: `ast.parse` limpio (cloud + dispositivo). `pyflakes`
    sigue en los mismos 6 warnings preexistentes, cero nuevos.
    `orchestrator.py`: 1172140 → 1176234 bytes (+4094). Backup en
    dispositivo (`orchestrator.py.pre_patch109.bak`, 1172140 bytes,
    coincide con el baseline post-`patch_orchestrator108`) antes de
    commitear, commit solo a `MonolitoPersonal`, verificado byte a
    byte Y con los marcadores `_codegen_rescue_should_force_local`
    (7 apariciones) y `patch_orchestrator109` (3 apariciones) y
    `ast.parse` del propio Python del dispositivo tras el commit --
    primer intento de `device_commit_files` sí prendió esta vez (sin
    necesidad de `force: true`).

    Pendiente de retest en vivo: repetir el turno real que disparó el
    reporte ("crear un juego en el workspace" con Cloud activo y un
    presupuesto de Sonnet bajo, para forzar el mismo corte a mitad de
    `write_file`) y confirmar que el rescate ahora completa en
    segundos (vía Cloud) en vez de colgarse 150+ segundos en Local --
    el log visible debería decir "finishing the file with a second
    Cloud pass" en vez de mencionar Ollama/Local.

50. **`patch_orchestrator110` (2026-09-19) — cortar el bucle de
    `read_file` repetido cuando el guardia de archivos sintetiza una
    llamada para un seguimiento de mejora sobre un archivo que ya se
    había leído en el mismo turno, bug real reportado con captura +
    log en vivo justo después de confirmar `patch_orchestrator109`.**
    Turno real: "improve the graphics of the game" sobre `mario.py`
    (ya existente, con mejoras de un turno anterior). Traza medida:

    1. Pasada 1: el modelo llama `read_file` de verdad (97 tok salida) --
       normal, lee el archivo antes de tocar nada.
    2. Pasada 2: el modelo NO emite ninguna llamada. El guardia
       (`File-op guard`) sintetiza `read_file` DE NUEVO.
    3. Pasada 3: se repite exactamente lo mismo -- el guardia sintetiza
       `read_file` una tercera vez.
    4. Con 3 `read_file` seguidos se dispara `MAX_CONSECUTIVE_SAME_
       TOOL_CALLS` (=2) y el bucle aborta: *"'read_file' se repitió sin
       avanzar — abortando."*
    5. El rescate final repite la misma lógica dos veces más ("leyendo
       el archivo actual primero") sin éxito, y el turno termina en
       "podrías reformular" sin haber intentado escribir el cambio ni
       una sola vez -- pese a que el archivo YA se había leído en la
       pasada 1.

    Causa raíz, `_salvage_file_operation` (orchestrator.py, ~línea
    13710 antes de este patch): la rama "es una modificación sobre un
    archivo que ya existe -> leerlo primero" no tenía forma de saber
    si ESE archivo ya se había leído en una pasada anterior del mismo
    turno -- como `target_exists` no cambia entre pasadas y el modelo
    seguía sin producir código real, la condición volvía a ser
    verdadera en cada pasada, así que el "rescate" caía siempre en
    "leer de nuevo" en un bucle sin salida, sin nunca llegar a la rama
    de regeneración real que ya existía unas líneas más abajo (la que
    de verdad genera un `write_file`).

    Tres cambios, todos en `orchestrator.py`:

    (a) **Registro de rutas ya leídas en el turno**: nuevo set
    `_paths_read_this_turn` (declarado junto a `_modify_target_current_
    content`, antes del primer llamado a `_salvage_file_operation`),
    poblado cada vez que un `read_file` real se ejecuta con éxito
    dentro del bucle de herramientas (mismo punto donde ya se detecta
    un `read_file` exitoso para fijar `_modify_target_current_content`
    -- se separó esa condición en dos, una que registra SIEMPRE la
    ruta leída y otra que solo fija el contenido de referencia la
    PRIMERA vez, como antes). Se pasa como `already_read_paths` a los
    dos llamados existentes de `_salvage_file_operation` (antes del
    bucle y dentro de él).

    (b) **`_salvage_file_operation` ya no repite `read_file` sobre una
    ruta ya leída**: la rama de "modificación sobre archivo existente"
    ahora chequea `already_read_paths` -- si el archivo objetivo ya
    está ahí, en vez de devolver otro `read_file` sigue de largo hacia
    la regeneración real (con un log distinto: "ya se leyó en este
    turno — generando el cambio directamente"). El último fallback de
    la función (cuando ni siquiera la regeneración produjo código
    usable) también respeta esto -- si el archivo ya se leyó, se rinde
    limpio (`None, None`) en vez de insistir con otro `read_file`.

    (c) **La regeneración para un archivo existente ahora preserva su
    contenido real**: antes, `_build_codegen_salvage_prompt` armaba
    siempre el mismo prompt de "escribí el archivo completo" a partir
    SOLO del pedido del usuario -- si esa rama llegaba a ejecutarse
    para un archivo YA existente (algo que antes de este patch nunca
    pasaba, por el bug de (b)), habría reescrito el archivo desde cero
    sin ver su contenido real, arriesgando perder cualquier mejora de
    turnos anteriores. Se le agregó un parámetro opcional
    `existing_content` (mismo patrón que `_targeted_update_prompt` de
    `_continue_truncated_file_write_locally`, patch_orchestrator43-60):
    cuando el archivo existe, el prompt ahora dice explícitamente "NO
    reescribas desde cero, aplicá el cambio pedido sobre el contenido
    real, todo lo demás queda EXACTAMENTE igual". Se pasa siempre que
    `target_exists`, no solo en el caso del bug de (b) -- cualquier
    regeneración futura sobre un archivo existente se beneficia de
    esto.

    Verificación: `ast.parse` limpio (cloud + dispositivo). `pyflakes`
    sigue en los mismos 6 warnings preexistentes, cero nuevos.
    `orchestrator.py`: 1176234 → 1185311 bytes (+9077). Backup en
    dispositivo (`orchestrator.py.pre_patch110.bak`, 1176234 bytes,
    coincide con el baseline post-`patch_orchestrator109`) antes de
    commitear, commit solo a `MonolitoPersonal`, verificado byte a
    byte Y con los marcadores `patch_orchestrator110` (6 apariciones)
    y `_paths_read_this_turn` (6 apariciones) y `ast.parse` del propio
    Python del dispositivo tras el commit -- primer intento de
    `device_commit_files` sí prendió esta vez (sin necesidad de
    `force: true`).

    Pendiente de retest en vivo: repetir un seguimiento de mejora
    sobre un archivo que el modelo ya leyó en el mismo turno (ej.
    "improve the graphics of the game" después de un `read_file` real)
    y confirmar que, si el modelo se queda mudo en una pasada
    siguiente, el guardia ya NO repite `read_file` -- en vez de eso
    debería verse "ya se leyó en este turno — generando el cambio
    directamente" en el log, seguido de un `write_file` real con el
    cambio aplicado sobre el contenido existente (no un archivo
    reescrito desde cero).

51. **`patch_orchestrator111` (2026-09-19) — dos bugs reportados juntos,
    con capturas + log en vivo, sobre el turno "explain the mario code
    of the workspace" (Cloud/Gemini activo, workspace ON, presupuesto
    "Bajo").** Pedido textual: *"necesito que arregle eso que no se vea
    el pensamiento al empesarse a generar y que el analisis siempre
    quede por debajo de el techo de el esfuerzo, lo de que pueda subir
    a medium es solo cuando se pide crear un archivo desde 0 y de ves
    en cuando cuando se busca y se edita un archivo porque si no
    entonces para que esta."*

    **(a) El "pensamiento" interno de Gemini se mostraba como si fuera
    la respuesta real.** La captura mostraba, ANTES de la respuesta de
    verdad, un texto suelto tipo `"Allocation burned: 0 out of 5426
    budget tokens (~0%"` renderizado como si fuera parte de la
    conversación. Causa raíz, `_call_gemini_api_raw` y `_stream_gemini_
    api_raw` (orchestrator.py): Gemini 3.x tiene "thinking" activado
    por defecto, y la API devuelve el razonamiento interno del modelo
    como partes NORMALES del `content` con una clave `"text"` -- solo
    distinguibles de la respuesta real por un flag aparte, `"thought":
    true`, en el mismo objeto `part`. El código armaba `raw_text`
    (variante no-streaming) y hacía `yield` de cada chunk en vivo
    (variante streaming) uniendo TODA parte con `"text"`, sin chequear
    ese flag -- así que el razonamiento interno del modelo (en este
    caso, literalmente narrando su propia contabilidad de presupuesto
    de tokens) se colaba en la respuesta visible, y en el modo
    streaming aparecía primero en pantalla (el pensamiento típicamente
    se genera antes que la respuesta final). Fix: en ambas funciones,
    cualquier `part` con `thought` verdadero se descarta antes de
    sumarlo a `raw_text`/hacer `yield` -- la respuesta visible real
    vive en las partes sin ese flag. No se tocó ninguna configuración
    de `thinkingConfig` (dejar el thinking activado en el lado de la
    API, pero nunca exponerlo, es el cambio mínimo y más seguro; apagar
    el thinking del todo podría degradar la calidad de turnos
    complejos, y eso no fue lo que se pidió).

    **(b) La elevación de presupuesto de `patch_orchestrator105` se
    disparaba también para turnos de puro análisis/explicación.** El
    mismo turno, con "Bajo" elegido a propósito, terminó con `[LOG]
    Budget auto-raised... bumping to 'Medio'` pese a no ir a escribir
    ni una línea de código (el turno terminó en `list_dir` + `read_
    file` + una explicación en prosa). Causa raíz: la rama `elif not
    is_file_write:` de `patch_orchestrator105` (pensada para el carril
    de "código en el chat", ej. "Give me a base game of slither.io with
    bots" con workspace APAGADO) no distinguía ese caso de un pedido de
    LECTURA/ANÁLISIS con workspace ENCENDIDO -- ambos comparten
    `is_file_write=False`, y el router había taggeado el turno como
    `SignalTag.CODE_COMPLEX` (por mencionar "mario code"), así que
    `_wants_codegen_budget` daba `True` para los dos por igual. Dos
    cambios:

    - Se agregó `explic\w*|explain\w*` a `_FILE_READ_VERB_RE` -- "explain"/
      "explica" faltaba de esa lista, así que `_is_file_read_turn`
      (que exige un verbo de lectura + sin intención de escritura + con
      herramientas de workspace activas) daba `False` para "explain the
      mario code..." pese a ser un pedido de lectura de manual de
      libro.
    - La condición de la rama pasó de `elif not is_file_write:` a `elif
      not is_file_write and not self._is_file_read_turn(user_input,
      decision):` -- si el turno es claramente de lectura/análisis, la
      elevación entera se salta y `gen_predict` se queda en lo que el
      selector guardado del usuario ya daba, sin subir de nivel en
      silencio. El caso motivador ORIGINAL de patch_orchestrator105
      sigue intacto: con workspace apagado, `_is_file_read_turn` da
      `False` de entrada (exige `_turn_wants_file_tools`, que a su vez
      exige herramientas de workspace activas), así que "Give me a base
      game..." sigue elevando exactamente igual que antes.

    No se tocó la rama `if is_file_write and not mod_is_modify:`
    (creación de archivo nuevo, sigue elevando sin condición nueva, tal
    como pidió el usuario) ni se agregó ninguna elevación nueva para el
    caso de editar un archivo existente (`is_file_write and mod_is_
    modify`) -- el usuario mencionó ese caso como aceptable "de vez en
    cuando", no como algo roto que haya que arreglar ahora; sigue
    exactamente igual que antes de este patch (capado en 5120tok, sin
    elevación).

    Verificación: `ast.parse` limpio (cloud + dispositivo). `pyflakes`
    sigue en los mismos 6 warnings preexistentes, cero nuevos. Test
    standalone de la regex confirmando que `_FILE_READ_VERB_RE` ahora
    matchea "explain the mario code of the workspace"/"explica el
    codigo de mario" y sigue SIN matchear turnos de escritura/mejora
    ("improve the graphics...", "create a mario game...", "fix the
    errors..."). `orchestrator.py`: 1185311 → 1190206 bytes (+4895).
    Backup en dispositivo (`orchestrator.py.pre_patch111.bak`, 1185311
    bytes, coincide con el baseline post-`patch_orchestrator110`) antes
    de commitear, commit solo a `MonolitoPersonal`, verificado byte a
    byte Y con el marcador `patch_orchestrator111` (4 apariciones) y
    `ast.parse` del propio Python del dispositivo tras el commit --
    primer intento de `device_commit_files` sí prendió esta vez (sin
    necesidad de `force: true`).

    Pendiente de retest en vivo: (1) repetir un turno de explicación/
    análisis sobre un archivo del workspace (ej. "explain the mario
    code of the workspace", o "explica que hace este archivo") con
    Cloud/Gemini activo y confirmar que YA NO aparece ningún texto de
    razonamiento interno ("Allocation burned...", o similar) antes de
    la respuesta real, y que el log YA NO muestra "Budget auto-raised"
    para ese turno -- el presupuesto debería quedarse en el nivel que
    el usuario tiene guardado. (2) Confirmar que un pedido real de
    código nuevo con workspace apagado (ej. "Give me a base game of
    slither.io with bots") SIGUE elevando el presupuesto igual que
    antes, para asegurar que patch_orchestrator105 no se rompió.

52. **`patch_qt74` + `patch_bat1` (2026-09-20) — auditoría completa del
    proyecto antes de subirlo a GitHub, pedido explícito del usuario
    ("y ahora que errores tiene la estructura actual? se puede subir a
    github ya?").** Se corrió `ast.parse` + `pyflakes` sobre los 45
    archivos `.py` reales del proyecto (excluyendo `sovnode_patch/`,
    `_to_delete/`, `_backups/`, `Claude outputs/`, `build/`, `dist/`,
    `workspace/` -- carpetas de trabajo interno o generadas, nunca
    parte del código fuente real). Resultado: **cero errores de
    sintaxis** en los 45 archivos. 15 avisos de `pyflakes` en total, 6
    ya catalogados de antes en `orchestrator.py` (ver notas de sesiones
    previas -- inofensivos, sin cambios), y 9 nuevos, todos cosméticos
    salvo uno:

    - `sovnode_log_viewer.py`, `workspace_watcher.py`: imports sin usar
      (`os`, `time`) -- inofensivo, no se tocó.
    - `web_search.py`: un `global _LAST_SEARCH_ERROR` que nunca se
      asigna en ese scope -- inofensivo, no se tocó.
    - `icons.py`: variable local `mid_h` sin usar -- inofensivo, no se
      tocó.
    - `test_regressions.py`: 4 imports de test scripts viejos
      (`_inspect37`, `_fsyn41`, `_KNode52`, `pipeline.PipelineEvent`)
      sin usar -- inofensivo, no se tocó (tocar `test_regressions.py`
      fuera de agregar una sección nueva no está en el alcance de esta
      sesión).
    - **`sovnode_qt.py`: DOS definiciones seguidas de la clase
      `_CancelToken`** (línea 3803 y 3811 antes de este patch) -- la
      segunda pisaba en silencio a la primera (mismo comportamiento en
      la práctica, pero código muerto duplicado sin ninguna razón real
      para existir, probablemente un resto de un merge/parche de una
      sesión anterior). Único hallazgo con algo real que limpiar --
      se eliminó la primera definición, dejando solo la que tiene
      docstring. `sovnode_qt.py`: 392553 → 393055 bytes (+502).

    **Revisión de la estructura para GitHub**: `MonolitoPersonal` en sí
    NO es (ni debe ser) un repo git -- el flujo establecido usa `Crear
    carpeta limpia para GitHub.bat` (raíz del proyecto) para generar
    `MonolitoPersonal-GitHub`, una copia descartable y limpia, vía
    `robocopy` con exclusiones (`_backups`, `_to_delete`, `sovnode_
    patch`, `Claude outputs`, `build`, `dist`, `__pycache__`, cualquier
    `*.bak*`, DBs/logs/índices `.faiss`/`.wal`, y vacía el `workspace`
    del sandbox dejando un `.gitkeep`), generando además un `.gitignore`
    a juego en el destino y un escaneo final de API keys sueltas en
    texto plano. El script está bien diseñado y ya cubre todo el
    desorden acumulado esta sesión (201 archivos `.bak*`, ~140MB en
    total, principalmente backups de `orchestrator.py` de cada patch --
    todos correctamente excluidos por el patrón `*.bak*`, no requieren
    limpieza manual antes de subir, aunque sí valdría la pena borrarlos
    del disco en algún momento por prolijidad). Confirmado con un
    escaneo propio (`grep` sobre exactamente los mismos archivos que el
    `.bat` terminaría copiando) que no hay ninguna clave con forma de
    API key (ni formato Anthropic `sk-ant-...` ni Google/Gemini
    `AIzaSy...`) en ningún archivo que vaya a parar al repo -- las keys
    reales viven en el Registro de Windows vía `QSettings("SovNode",
    "SovNode")` (`cloud/api_key_anthropic`, `cloud/api_key_gemini`),
    nunca en un archivo del proyecto.

    **Único gap real encontrado en el `.bat`**: su escaneo final de
    seguridad (`findstr`) solo buscaba el patrón de Anthropic (`sk-
    ant-...`) -- quedó desactualizado desde que la app sumó soporte
    para Gemini (Google) como proveedor de Cloud (el proveedor
    ACTIVO en la sesión de pruebas de este mismo documento), cuyas
    keys tienen un formato totalmente distinto (`AIzaSy...`). Si
    alguna vez una key de Gemini terminara, por accidente, en un
    archivo de texto plano, este escaneo NO la hubiera detectado. Fix
    (`patch_bat1`): se agregó un segundo `findstr` con el patrón
    `AIzaSy[A-Za-z0-9_-]` (mismo estilo/rigor que el chequeo existente
    de Anthropic -- no es una validación estricta de formato completo,
    es un canario de seguridad), y se actualizaron los mensajes para
    mencionar ambos proveedores. `Crear carpeta limpia para GitHub.bat`:
    7503 → 8381 bytes (+878).

    Tamaño real de lo que efectivamente se copiaría al repo (los 45
    `.py` + `README.md` + `ARCHITECTURE.md`): ~3.1MB -- un tamaño de
    repo perfectamente razonable, nada que optimizar ahí.

    **Veredicto para el usuario**: sí, se puede subir a GitHub ya --
    cero errores de sintaxis, el único hallazgo real (código duplicado
    en `sovnode_qt.py`) ya está limpio, no hay ninguna clave filtrada
    en lo que se subiría, y el script de exportación ya cubre
    correctamente todo el desorden de backups/carpetas internas
    acumulado durante esta sesión de debugging. Recomendación: correr
    `Crear carpeta limpia para GitHub.bat` de nuevo (para regenerar
    `MonolitoPersonal-GitHub` con el fix del escaneo de keys) antes del
    primer `git push`.

    Verificación: `ast.parse` + `pyflakes` limpios (cloud + dispositivo)
    en ambos archivos tocados. Backups en dispositivo
    (`sovnode_qt.py.pre_patch_qt74.bak`, 392553 bytes; `Crear carpeta
    limpia para GitHub.bat.pre_patch_bat1.bak`, 7503 bytes) antes de
    commitear, commit solo a `MonolitoPersonal`, verificados byte a
    byte Y con marcador de texto (`patch_qt74` / `AIzaSy` +
    `patch_bat1`) tras cada commit -- ambos primeros intentos de
    `device_commit_files` prendieron sin necesidad de `force: true`.

53. **`patch_bat2` (2026-09-20) — el usuario pidió explícitamente
    "agregar en el .gitignore los archivos relacionados a la memoria de
    sovnode"; más la primera actualización del `README.md` con capturas
    reales del proyecto (gifs nuevos agregados por el usuario en
    `docs/`).**

    Los archivos de "memoria" de SovNode (identificados recorriendo
    `memory_graph.py`, `wal.py` y `rag_faiss.py`, y confirmando en el
    disco del usuario que existen hoy en la raíz del proyecto) son:
    `sovnode_memory.db` (`MemoryGraph`, historial + grafo de memoria,
    SQLite -- default `db_path="sovnode_memory.db"`), `sovnode.wal`
    (`WriteAheadLog`, JSONL append-only -- default
    `log_path="sovnode.wal"`), `sovnode_debug.log`, y los índices
    vectoriales `workspace_vector_index.faiss` /
    `workspace_vector_index.meta.json` +
    `longterm_vector_index.faiss` / `longterm_vector_index.meta.json`
    (`LocalVectorRAG.save`, ambos junto a `sovnode_memory.db` porque
    `self._rag_persist_dir = self.memory_graph.db_path.resolve().parent`
    en `orchestrator.py`). En la práctica, el `.gitignore` que genera
    `Crear carpeta limpia para GitHub.bat` YA excluía los cinco por
    patrón genérico de extensión (`*.db`, `*.wal`, `*.faiss`,
    `*.meta.json`, `*.log`) desde antes de esta sesión -- no había un
    bug de fuga real. Aun así, a pedido del usuario, se agregó una
    sección nueva y explícita "Memoria / RAG de SovNode" en el bloque
    que genera el `.gitignore`, listando los cinco nombres reales de
    archivo uno por uno (`sovnode_memory.db` + sus variantes
    `-journal`/`-shm`/`-wal`, `sovnode.wal`, `sovnode_debug.log`,
    `workspace_vector_index.*`, `longterm_vector_index.*`), además de
    (no en lugar de) los patrones genéricos que ya estaban. Doble
    cobertura intencional: los patrones genéricos siguen protegiendo si
    el nombre interno cambia algún día, y el listado explícito deja
    claro con solo mirar el `.gitignore` generado cuáles archivos son
    "memoria" del usuario y no código fuente. `Crear carpeta limpia para
    GitHub.bat`: 8381 → 9798 bytes (+1417).

    **Actualización del README (`docs/`)**: el usuario agregó dos gifs
    nuevos a `docs/` (`Sovnode UI showcase.gif`, 1,393,275 bytes;
    `Sovnode improving mario code.gif`, 4,558,011 bytes) además de los
    dos ya usados en el README (`showcaseSovnode1.gif`,
    `showcaseSovnode2.gif`). Se revisaron los cuatro (`device_stage_
    files` + `Read`; nota: el `Read` de un `.gif` solo renderiza el
    primer frame estático, no la animación completa -- suficiente para
    confirmar que cada archivo corresponde a lo que su nombre indica,
    no para juzgar el contenido animado en sí). Se agregaron los dos
    nuevos a la sección "🖼 Demo" del `README.md`, intercalados con los
    existentes en un orden narrativo (tour general de la UI → chat en
    vivo → edición real de código en el workspace → consola/telemetría),
    sin tocar el resto del documento. Los `src` de los dos gifs nuevos
    llevan el espacio de sus nombres de archivo percent-encoded
    (`%20`) porque son atributos de URL, no solo de HTML -- un espacio
    literal en un `<img src>` funciona en la mayoría de navegadores pero
    no es válido como URL; `%20` es la forma correcta y funciona en todos
    los renderers (GitHub incluido). `README.md`: 9526 → 9963 bytes
    (+437).

    **Nota para el usuario, todavía sin resolver, NO tocada en este
    patch**: todo el `README.md` (incluido el badge "data: 100% local"
    y frases como "nothing is ever sent to a cloud server" / "No cloud
    calls for inference" en la sección "🔒 Privacy & Security")
    describe a SovNode como exclusivamente 100% offline -- pero la app
    tiene, y esta misma sesión probó extensivamente, un motor Cloud
    real (Gemini/Anthropic) que el usuario activa desde el selector
    "GENERATION ENGINE" de la UI. Es una decisión de posicionamiento
    del producto, no un bug, así que no se cambió unilateralmente --
    pero vale la pena que el usuario decida si el README debería
    mencionar el modo Cloud como opcional (ej. "100% local por
    defecto, con un motor Cloud opcional") antes de subir el repo,
    para que no quede una discrepancia entre lo que dice el README y
    lo que ve cualquiera que abra la app.

    Verificación: backups en dispositivo (`Crear carpeta limpia para
    GitHub.bat.pre_patch_bat2.bak`, 8381 bytes; `README.md.pre_readme_
    gifs.bak`, 9526 bytes) antes de commitear, commit solo a
    `MonolitoPersonal`, verificados byte a byte Y con marcador de texto
    (`patch_bat2` / 5 apariciones de `sovnode_memory.db` en el `.bat`;
    `Sovnode%20UI%20showcase` / `Sovnode%20improving%20mario%20code` /
    5 `img src` en el README) tras cada commit. El commit del `.bat`
    necesitó `force: true` en el primer intento (mismo patrón ya
    documentado arriba: el archivo en disco coincidía exactamente en
    tamaño con la base esperada, confirmado por `device_bash` antes de
    forzar -- no había edición más nueva del usuario de por medio, solo
    que este archivo no se había vuelto a stagear en esta sesión y por
    lo tanto no había un `expectedMtimeMs` real para pasar); el del
    README también se hizo directamente con `force: true` por el mismo
    motivo.

## 9. Variables de entorno relevantes

`OLLAMA_MODEL` / `OLLAMA_GENERAL_MODEL`, `OLLAMA_CODER_MODEL`,
`OLLAMA_FAST_WRITE_MODEL` + `SOVNODE_ENABLE_FAST_WRITE_MODEL`,
`OLLAMA_ROUTER_MODEL`, `OLLAMA_VISION_MODEL` (default `moondream`),
`OLLAMA_EMBED_MODEL`, `OLLAMA_ENDPOINT`,
`SOVNODE_ENABLE_LANG_FIX_LIGHT_MODEL` (default **`"1"` desde
2026-09-16** — antes `"0"`; poner `"0"` vuelve al comportamiento
de siempre, sin el intento liviano, ver §6/§8), `SOVNODE_THINK_LEVEL`,
`WEB_KNOWLEDGE_TTL_SECONDS`, `SEMANTIC_CACHE_THRESHOLD` (default 0.93),
`SEMANTIC_CACHE_TTL_SECONDS`, `SEMANTIC_CACHE_ENABLED`,
`SOVNODE_RAG_MIN_SIMILARITY` (default 0.30), `OLLAMA_MAX_LOADED_MODELS`
(default 2), `OLLAMA_NUM_PARALLEL` (alineado con
`Orchestrator._llm_lock = BoundedSemaphore(2)`).
**El motor Cloud NO usa variables de entorno** — `cloud_backend_enabled`/
`cloud_api_key`/`cloud_model_id` se configuran 100% desde la UI y se
persisten en `QSettings`, nunca en el entorno del proceso.
**Evitar** (histórico, causó un apagón total en la GPU AMD RX 5500 XT
del usuario): `OLLAMA_KV_CACHE_TYPE=q8_0` + flash attention sin probarlo
a mano primero.

## 10. Notas para quien continúe este trabajo (humano o IA)

- **`_correct_response`/VerifyLite puede corromper código si
  `find_language_mismatch` da un mismatch VERDADERO sobre una respuesta
  de código -- nada lo detecta todavía** (2026-09-16, ver §8 -- bug real
  medido en vivo dos veces: un falso positivo de idioma (código sin
  cerrar por el techo de tokens) mandó un archivo de Python entero a
  `router_model` (0.5B, no es un modelo de código) para "corregir el
  idioma" con 900 tokens de presupuesto, y el resultado fue un juego con
  el loop principal faltante y variables indefinidas. El falso positivo
  puntual que lo disparaba (bloque \`\`\` sin cerrar sobreviviendo entero
  a `_strip_code_for_lang_detection`) quedó arreglado y CONFIRMADO EN
  VIVO con video el mismo día -- una cuarta prueba con el mismo pedido
  ya no disparó ni Router0.5B ni VerifyLite, y el mecanismo de respaldo
  ya existente `CodeSyntaxFix` (ast.parse + reescritura con el modelo
  completo) terminó el trabajo limpio). Pero el problema de fondo sigue
  abierto: si `find_language_mismatch` da alguna vez un
  positivo VERDADERO sobre una respuesta de código, nada impide que
  `_light_lang_fix_looks_unfaithful` (que compara números/nombres
  propios, pensado para texto/visión) deje pasar una "corrección" que
  preservó los números pero destruyó la lógica del programa. Antes de
  confiar en el pipeline de corrección para turnos de código, valdría
  la pena: (a) que `_correct_response` nunca use el modelo liviano
  (`lang_fix_light_model_enabled`) cuando la respuesta original
  contiene un bloque \`\`\`código\`\`\`, sin importar qué disparó la
  corrección -- ir directo al modelo completo (`full_raw`, más caro
  pero con más chance real de preservar la lógica); o (b) sumar un
  chequeo de fidelidad específico para código a
  `_light_lang_fix_looks_unfaithful` (conteo de `def`/`class`/`import`
  antes vs. después, como mínimo) antes de aceptar el resultado del
  modelo liviano.
- **Puede haber otra sesión editando el mismo repo en paralelo** (el
  usuario suele tener Claude Code corriendo localmente además de esta
  sesión de Cowork — de hecho el trabajo de OCR y los blindajes de
  router de 09-09/09-11 se hicieron así, entre revisiones de este
  documento). Si se llega por el puente remote-devices sin
  `device_bash`: SIEMPRE re-listar mtime/tamaño justo antes de editar,
  re-stagear una copia fresca, y diffear contra la base sobre la que se
  escribió — un mtime más nuevo no implica necesariamente contenido
  distinto, pero hay que confirmarlo, nunca asumir. **Caso real
  observado dos veces en una sola sesión (2026-09-16)**: una
  actualización de ESTE archivo se escribió y comiteó con éxito, pero
  al re-stagear más tarde en la misma sesión el contenido había vuelto
  a un estado ANTERIOR — algo más (probablemente otro proceso con este
  archivo abierto y autoguardado, no solo ediciones nuevas) sobrescribe
  el archivo completo de tanto en tanto, no solo agrega contenido. Pasó
  dos veces seguidas el mismo día, siempre perdiendo el bloque escrito
  MÁS RECIENTE (el resto del documento sobrevivía intacto). Un commit
  exitoso de este archivo NO garantiza que el contenido siga ahí más
  tarde en la misma sesión — conviene re-verificar antes de asumir que
  una nota quedó guardada, y avisarle al usuario si vuelve a pasar (algo
  con este path abierto localmente vale la pena que lo cierre mientras
  dure la sesión de Cowork).
- **`_suggest_codegen_entity_cap` (§8, cuarto round) usa una constante
  sin calibrar** (`_CODEGEN_EXTRA_TOKENS_PER_ENTITY = 150`, "150
  tokens por entidad con IA") — es una estimación a ojo, no medida
  contra generaciones reales. Antes de confiar en el tope de 3 bots
  para presupuestos medios/altos (el cap se satura en 3 a partir de
  ~450 tokens extra, o sea cualquier presupuesto por encima de "Bajo"
  sugiere el mismo tope de 3 -- puede que a presupuestos más altos
  convenga subir `_CODEGEN_MAX_ENTITY_HINT` en vez de dejarlo fijo),
  valdría la pena medir por WAL cuántos tokens le lleva a Sonnet de
  verdad una entidad con IA simple y ajustar la constante con datos
  reales.
- **Patrón recurrente esta sesión: heurísticas de calidad genéricas
  (pensadas para prosa) aplicadas sin distinción a respuestas de
  código, con falsos positivos reales** (§8, rounds 3 y 5 -- el mismo
  tipo de bug dos veces con causas distintas: el falso positivo de
  idioma sobre código sin cerrar, y ahora el circuit-breaker de
  `_looks_degenerate_repetition`/`hit_ceiling` sobre estructuras de
  código legítimas). Antes de agregar cualquier heurística NUEVA de
  "esta respuesta parece rota/descarrilada" en `orchestrator.py`, vale
  la pena preguntarse explícitamente si corre también sobre turnos de
  código (`_wants_codegen_budget`) y, si es así, si tiene sentido ahí
  -- el código tiene patrones legítimos (repetición estructural,
  respuestas que llenan el techo a propósito) que rompen supuestos
  pensados para texto en prosa.
- **Verificar cualquier fix en un workspace descartable** (o, si el
  cambio es puramente aditivo y ya viene con su propio script
  `verify_*.py`, correr ese script Y la suite de regresión completa)
  antes de dar por buena una implementación — nunca editar directo sobre
  la copia del usuario sin haber corrido ambas cosas primero.
- **Comentario desactualizado detectado en esta revisión**: el
  comentario junto a `CLOUD_TOOLS_SCHEMA` (orchestrator.py, ~línea 2084)
  todavía dice "las 5 herramientas reales" — son 6 desde que se agregó
  `edit_file` (§5.5). No afecta el comportamiento, solo la lectura del
  código; corregirlo es cosmético y de bajo riesgo si alguien quiere
  hacerlo.
- **El bug de notación `**`/`^`** (§3.1, `CASEngine._collapse_operator_run`)
  es un buen ejemplo de la CLASE de bug a la que este proyecto es
  estructuralmente vulnerable: código que construye un string de
  fórmula/expresión para volver a parsearlo después, donde el saneador
  puede corromper silenciosamente el contenido sin lanzar ninguna
  excepción. Cualquier módulo nuevo que persista y luego relea
  expresiones simbólicas debería auditarse contra el mismo patrón.
- **La investigación de los glitches visuales de `math_render.py` sigue
  abierta** (§3.3) — no inventar una causa raíz sin reproducirla
  empíricamente primero.
- **`_EDIT_FILE_BUDGET_MULTIPLIER = 2.75` (§5.5/§8, `orchestrator58`) es
  otra constante sin calibrar contra generaciones reales**, igual que
  `_CODEGEN_EXTRA_TOKENS_PER_ENTITY` (nota de arriba) y el piso de
  `_estimate_min_viable_codegen_tokens`. Es una estimación a ojo de
  cuánto más pesa `old_str`+`new_str`+overhead de JSON frente a un
  `write_file` equivalente -- valdría la pena medir por WAL, para varios
  tamaños reales de diff, cuánto le sale de más a `edit_file` sobre
  `write_file` y ajustar el multiplicador (o volverlo dependiente del
  tamaño del diff esperado, no una constante fija) con datos reales.
- **La palanca de mayor impacto restante sobre el costo real del motor
  Cloud sigue sin implementar, a pedido explícito del usuario ("Por
  ahora no, ya alcanza"), no por falta de investigación**: `_call_claude_
  api_raw`/`_stream_claude_api_raw` (§5.1/§5.4) reconstruyen
  `body["messages"]` como un ÚNICO turno de usuario plano en cada pasada
  del bucle de herramientas, aplanando `_tool_history_entries` a texto
  desde cero cada vez -- el prefijo de `system` y el array `tools` sí
  cachean (§5.4), pero todo el historial de herramientas de pasadas
  anteriores se repaga completo, a precio de input normal, en cada
  pasada adicional. Cachear eso incrementalmente (mensajes separados por
  pasada, con `cache_control` en los límites correctos) es la única
  palanca de este documento que toca la superficie que factura contra la
  API real -- alto riesgo si sale mal (romper el tool-calling o la
  contabilidad de costo), por eso se investigó, se cuantificó con datos
  de turnos reales, se le presentó la opción al usuario, y se dejó
  explícitamente afuera de esta sesión. No implementar sin que el
  usuario lo pida de nuevo explícitamente.
- **`device_commit_files` sobre este dispositivo falla en silencio la
  PRIMERA vez, de forma consistente y repetida** (confirmado en las
  sesiones 2026-09-16 y 2026-09-17, en prácticamente todos los commits
  de `orchestrator.py` de ambas -- la única excepción medida fue un
  commit de `sovnode_qt.py`): la llamada con `expectedMtimeMs` devuelve
  `{"written":[...],"rejected":[]}` (aparenta éxito) pero un
  `device_stage_files` de verificación inmediatamente después muestra el
  tamaño de archivo VIEJO, sin cambios. El patrón que funciona,
  consistentemente: reintentar el mismo commit con `force: true`
  (siempre tiene éxito), y SIEMPRE re-stagear después para confirmar
  bytes + `ast.parse` + los marcadores de parches esperados antes de
  reportarle éxito al usuario -- nunca confiar en la respuesta del
  primer intento sola. No se investigó la causa de fondo (¿un caché de
  mtime del lado del dispositivo que no se invalida a tiempo? ¿una
  condición de carrera con el propio proceso de SovNode con el archivo
  abierto?) -- documentado acá para que la próxima sesión no lo
  redescubra de cero ni lo tome como una falla puntual.
- Este documento describe el estado leído el 2026-09-16 (la revisión
  2026-09-14 quedó incorporada arriba tal cual; la de 2026-09-16 son
  cinco fixes puntuales, ver §8, ninguno sobre el motor dual). Si pasó
  mucho tiempo desde esa fecha, re-verificar contra el código real antes de
  asumir que algo sigue vigente — en particular tamaños de archivo,
  conteo de `KNOWN_FORMULAS` (32), el número de secciones de
  `test_regressions.py` (63, y sin secciones dedicadas al motor Cloud —
  ver §7), y el conjunto de variables de entorno. Esta actualización se
  hizo comparando mtimes de archivo contra la revisión anterior (09-08)
  y leyendo puntualmente lo que cambió — no es una relectura completa de
  cero, así que un módulo que no aparece mencionado como "novedad" en
  alguna sección de arriba no necesariamente está sin cambios, solo que
  su mtime no lo delató (o el cambio no tenía un comentario fechado que
  lo explicara).
