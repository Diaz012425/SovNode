# AGENTS.md — guía para agentes de IA que trabajen en este repo

Este archivo es para vos, agente de IA (Claude Code, Cowork, Cursor, Copilot,
lo que sea), no para humanos — el README.md es la puerta de entrada humana.
Leé esto ANTES de tocar código. Para un mapa completo módulo por módulo,
con historial de bugs reales y por qué cada decisión de diseño es como es,
leé [`ARCHITECTURE.md`](ARCHITECTURE.md) — este archivo es el resumen
ejecutable, ese es el contexto profundo.

## Qué es esto

Cliente de escritorio Windows (PyQt6) que corre un asistente de IA 100%
local sobre Ollama. Sin llamadas a la nube para inferencia. Memoria híbrida
(SQLite FTS5 + FAISS), sandboxing de herramientas, verificación
anti-alucinación post-hoc, y dos daemons de "descubrimiento" que corren en
background. Licencia AGPLv3. Nombre público: **SovNode**.

## Cómo correrlo / testear

```bash
pip install -r requirements.txt
python src/ui/sovnode_qt.py      # cliente principal PyQt6
streamlit run app.py             # UI alternativa (Streamlit)
```

Requiere Ollama corriendo en `localhost:11434` con `qwen2.5:7b` (general) y
`qwen2.5:0.5b` (router) descargados como mínimo.

**Tests:** `python tests/test_regressions.py` — es la suite real
(aserciones + exit code != 0 si falla), organizada en secciones numeradas
donde cada una documenta el bug real que la motivó. Antes de correrla en
un workspace nuevo/descartable hacen falta dos parches de entorno que NO
son bugs de producto (ver ARCHITECTURE.md §6):

1. `_rag.documents` debe ser un `dict`, no una lista.
2. Un `Orchestrator` construido con `object.__new__` necesita
   `_o._vector_rag_lock = threading.Lock()` seteado a mano.

Aplicá esos parches SOLO en la copia de test, nunca en el código de
producto.

## Antes de tocar código, en cualquier tarea

- **Verificá cualquier fix en un workspace descartable** — copiá `src/`,
  `tests/`, `SovNode.spec`, `build.py`, `app.py` a un directorio temporal,
  aplicá los 2 parches de arriba SOLO ahí, corré la suite completa — antes
  de tocar la copia real del usuario.
- **Puede haber otra sesión de IA editando el mismo repo en paralelo.**
  Si trabajás a través de un puente remoto sin shell (solo lectura/escritura
  de archivos puntuales, sin `git`/`bash` directo): re-listá mtime/tamaño
  del archivo justo antes de editar, re-leé una copia fresca, y diffeá
  contra la base sobre la que ibas a escribir — un mtime más nuevo no
  implica necesariamente contenido distinto, pero confirmalo con diff,
  nunca asumas.
- **Nunca agregues una dependencia nueva sin sumarla a `requirements.txt`.**
  Si importás algo que no está ahí, agregalo en el mismo commit.

## Qué NO tocar / no borrar

- `sovnode_memory.db`, `sovnode.wal`, `*.faiss`, `*.meta.json` — memoria e
  índices reales del usuario. Están gitignored a propósito: nunca los
  commitees, y no los edites a mano.
- `src/core/workspace/` — es la raíz por defecto del sandbox de
  herramientas (`ToolSandbox._default_isolated_root()`); el modelo puede
  escribir ahí en runtime. No asumas que su contenido es basura ni lo
  limpies sin revisar primero.
- `SovNode.zip` — se sube a mano a GitHub Releases, nunca vía `git add`
  (es demasiado grande para un repo normal).
- `MonolitoPersonal.spec` fue removido por legado (apuntaba a un
  `monolito_qt.py` que ya no existe). El spec vigente es `SovNode.spec`.
  Si ves referencias sueltas al nombre viejo "monolito"/"Monolito
  Personal" en comentarios, es el working-title original del proyecto
  antes de rebautizarse SovNode — no un bug.

## Clases de bug a las que este proyecto es estructuralmente vulnerable

- **Saneadores de expresiones que corrompen en silencio.** `CASEngine`
  (`src/core/cas_sandbox.py`) colapsa rachas de operadores repetidos
  (`_collapse_operator_run`) para limpiar typos — pero tiene una excepción
  explícita para `**` (potencia) que en algún momento no existía y colapsó
  `**` a `*` sin lanzar ninguna excepción, produciendo resultados
  "VERIFIED" pero matemáticamente absurdos. Cualquier módulo nuevo que
  persista y luego relea expresiones simbólicas debería auditarse contra
  este mismo patrón.
- **Morfología del español no cubierta por regex.** `router.py` tuvo
  varios bugs reales por modo subjuntivo y verbos irregulares. Si tocás
  el router, corré la suite completa — tiene una sección dedicada a esto.
- **`os.getcwd()` como fallback silencioso de sandbox root.** Ya está
  blindado (`ToolSandbox`), pero si agregás una ruta nueva de ejecución de
  herramientas, asegurate de que pase por la misma lista negra
  (`_is_blacklisted_target`) en vez de inventar una raíz nueva.

## Estilo / convenciones

- ~95% de comentarios y docstrings del código están en **español** — seguí
  esa convención al editar módulos existentes.
- El README.md (cara pública, en inglés) y ARCHITECTURE.md (mapa técnico,
  en español) deben mantenerse consistentes entre sí si cambiás algo
  estructural (nuevo módulo, variable de entorno, dependencia).
- No hay linter/formatter configurado todavía — mantené el estilo del
  archivo que estés editando.

## Variables de entorno relevantes

Ver README.md → "Model Setup" y ARCHITECTURE.md §8 para la lista completa
(`OLLAMA_MODEL`, `OLLAMA_ROUTER_MODEL`, `SOVNODE_ENABLE_FAST_WRITE_MODEL`,
etc.). Evitar `OLLAMA_KV_CACHE_TYPE=q8_0` + flash attention sin probarlo a
mano primero — causó un apagón total de Ollama en la GPU AMD de referencia
(RDNA1, soporte limitado).
