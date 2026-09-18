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
SovNode — ocr_reader.py
ES: Lectura de texto (OCR) sobre capturas de pantalla adjuntadas por el
usuario, para el flujo de "corregir un error de código a partir de una
imagen" (mejora pedida 2026-09-09, ver orchestrator.py::
_maybe_prepare_code_fix_from_image). Corre 100% en CPU vía ONNX
(rapidocr-onnxruntime) — el mismo enfoque que ya usa embeddings.py con
fastembed — a propósito: la GPU del usuario (RX 5500 XT, RDNA1, NO
soportada oficialmente por ROCm) ya obligó a elegir un modelo de visión
chico (moondream) para no arriesgar un crash del servidor de Ollama con
un modelo más grande; el OCR no debe sumar NINGÚN riesgo nuevo sobre esa
misma GPU, así que corre aparte, sin pasar por Ollama ni por HIP/ROCm en
absoluto.

Degrada con gracia (igual que `_get_local_model` en embeddings.py): si el
paquete no está instalado o falla al cargar, memoriza el fallo y todas
las funciones de este módulo devuelven `None`/`False` en vez de lanzar —
el llamador simplemente cae al carril de visión existente (moondream)
como si este módulo no existiera.

EN: Text reading (OCR) over screenshots the user attaches, for the "fix a
code error from an image" flow. Runs 100% on CPU via ONNX
(rapidocr-onnxruntime), same approach embeddings.py already uses with
fastembed — deliberately: the user's GPU (RX 5500 XT, RDNA1, not
officially supported by ROCm) already forced picking a small vision
model (moondream) to avoid crashing the Ollama server with a bigger one;
OCR must not add any new risk on that same GPU, so it never touches
Ollama or HIP/ROCm at all. Degrades gracefully like embeddings.py's
`_get_local_model`: a missing/failed package just makes every function
here return `None`/`False`, and the caller falls back to the existing
vision lane (moondream) as if this module didn't exist.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
from typing import Any, List, Optional

logger = logging.getLogger("SovNode.OCR")

_engine_lock = threading.Lock()
_engine_instance = None
_engine_load_failed = False

_UPSCALE_FACTOR = 2.0

_MAX_SIDE_PX = 2400


def _get_ocr_engine() -> Optional[Any]:
    """ES: Carga perezosa y memoizada del motor de OCR. Si falla (paquete
    no instalado, modelo corrupto), memoriza el fallo para no reintentar
    en cada llamada.
    EN: Lazy, memoized load of the OCR engine. On failure (package
    missing, corrupt model), memoizes the failure to avoid retrying on
    every call."""
    global _engine_instance, _engine_load_failed
    if _engine_instance is not None or _engine_load_failed:
        return _engine_instance

    with _engine_lock:
        if _engine_instance is not None or _engine_load_failed:
            return _engine_instance
        try:
            from rapidocr_onnxruntime import RapidOCR
            _engine_instance = RapidOCR()
        except Exception as exc:
            logger.warning(
                "No se pudo cargar el motor de OCR local (%s); el flujo de "
                "'corregir error desde una captura' se salteará y el turno "
                "seguirá por el carril de visión normal. Instalalo con: "
                "pip install rapidocr-onnxruntime",
                exc,
            )
            _engine_load_failed = True
            return None

    return _engine_instance


def is_ocr_available() -> bool:
    """ES: ¿Está el motor de OCR listo para usarse (paquete instalado y
    modelo cargado)? Fuerza el intento de carga perezosa si todavía no
    se intentó — así el llamador (`orchestrator._maybe_prepare_code_fix_
    from_image`) puede distinguir "no instalado" de "instalado pero no
    detectó texto", en vez de que ambos casos degraden igual a `None`.
    Nunca lanza.
    EN: Is the OCR engine ready to use (package installed and model
    loaded)? Forces the lazy-load attempt if it hasn't happened yet, so
    the caller can tell "not installed" apart from "installed but found
    no text" instead of both collapsing into `None`. Never raises."""
    try:
        return _get_ocr_engine() is not None
    except Exception:
        return False


def _load_and_upscale(image_path: str):
    """ES: Abre la imagen y la reescala según `_UPSCALE_FACTOR`/`_MAX_SIDE_PX`
    (ver su comentario). Devuelve un objeto PIL.Image, o lanza si Pillow
    no está disponible o el archivo no se puede abrir — el llamador
    (`extract_text_from_image`) ya envuelve esto en un try/except amplio.
    EN: Opens the image and rescales it per `_UPSCALE_FACTOR`/`_MAX_SIDE_PX`.
    Returns a PIL.Image, or raises if Pillow is unavailable or the file
    can't be opened — the caller already wraps this in a broad
    try/except."""
    from PIL import Image

    img = Image.open(image_path)
    img = img.convert("RGB")
    target_w = min(int(img.width * _UPSCALE_FACTOR), _MAX_SIDE_PX)
    target_h = min(int(img.height * _UPSCALE_FACTOR), _MAX_SIDE_PX)
    if target_w > img.width and target_h > img.height:
        img = img.resize((target_w, target_h), Image.LANCZOS)
    return img


def extract_text_from_image(image_path: str) -> Optional[str]:
    """
    ES: Texto detectado en `image_path`, en orden de lectura (arriba hacia
    abajo), o `None` si el motor no está disponible, la imagen no se
    pudo abrir, o no se detectó ningún texto. Nunca lanza — cualquier
    excepción (paquete faltante, imagen corrupta, formato no soportado)
    degrada a `None`, el mismo contrato que `get_embedding` en
    embeddings.py cuando el modelo local no está disponible.

    EN: Text detected in `image_path`, in reading order (top to bottom),
    or `None` if the engine is unavailable, the image couldn't be
    opened, or no text was detected. Never raises — any exception
    (missing package, corrupt image, unsupported format) degrades to
    `None`, the same contract as embeddings.py's `get_embedding` when
    the local model is unavailable.
    """
    engine = _get_ocr_engine()
    if engine is None:
        return None

    try:
        img = _load_and_upscale(image_path)
        result, _elapse = engine(img)
    except Exception as exc:
        logger.debug("OCR falló sobre '%s' (%s).", image_path, exc)
        return None

    if not result:
        return None

    try:
        text = _reading_order_text(result)
    except Exception as exc:
        logger.debug("No se pudo ordenar/unir el resultado de OCR (%s).", exc)
        return None

    return text.strip() or None


def _reading_order_text(result: List[Any]) -> str:
    """
    ES: Reconstruye el texto en orden de lectura real (arriba a abajo,
    izquierda a derecha DENTRO de cada renglón) a partir de `result`
    (lista de [caja, texto, confianza] de RapidOCR). MEDIDO (prueba
    aislada con un traceback sintético): un simple `sorted(..., key=y)`
    no alcanza — dos fragmentos del MISMO renglón ("NameError: name" y
    "'ball_speed' is not defined", lado a lado) pueden tener un `y`
    superior-izquierdo apenas distinto por ruido del detector (102 vs
    103), y ese ruido bastaba para invertir su orden y mezclar dos
    mitades de líneas distintas — crítico en un traceback, donde
    "File ... línea N" tiene que preceder al tipo de excepción para que
    el texto siga teniendo sentido.

    Agrupa cajas cuyo centro vertical cae dentro de un margen de
    tolerancia (60% de la altura de la caja) en el mismo "renglón",
    ordena los renglones por altura y, DENTRO de cada uno, las cajas por
    `x` (izquierda a derecha) antes de unirlas con un espacio.

    EN: Reconstructs text in real reading order (top-to-bottom,
    left-to-right WITHIN each row) from `result` (RapidOCR's list of
    [box, text, confidence]). MEASURED (isolated test with a synthetic
    traceback): a plain `sorted(..., key=y)` isn't enough — two
    fragments on the SAME row (side by side) can have top-left `y`
    values that differ by a hair of detector noise, which was enough to
    flip their order and interleave two different lines' halves —
    critical in a traceback, where "File ... line N" must precede the
    exception type for the text to still make sense.

    Groups boxes whose vertical center falls within a tolerance margin
    (60% of box height) into the same "row", sorts rows top-to-bottom,
    and sorts boxes WITHIN each row by `x` (left to right) before
    joining them with a space.
    """
    items = []
    for box, text, _score in result:
        if not text:
            continue
        ys = [pt[1] for pt in box]
        xs = [pt[0] for pt in box]
        items.append((min(ys), max(ys), min(xs), str(text)))
    items.sort(key=lambda it: it[0])

    rows: List[dict] = []
    for y0, y1, x0, text in items:
        y_center = (y0 + y1) / 2.0
        height = max(y1 - y0, 1.0)
        for row in rows:
            if abs(row["y"] - y_center) <= height * 0.6:
                row["items"].append((x0, text))
                row["y"] = (row["y"] * row["n"] + y_center) / (row["n"] + 1)
                row["n"] += 1
                break
        else:
            rows.append({"y": y_center, "n": 1, "items": [(x0, text)]})

    rows.sort(key=lambda row: row["y"])
    return "\n".join(
        " ".join(text for _x, text in sorted(row["items"], key=lambda it: it[0]))
        for row in rows
    )


_CODE_OR_ERROR_HINTS_RE = re.compile(
    r"\b("
    r"traceback|file\s*\"|line\s*\d+|"
    r"\w*error\b|\w*exception\b|excepci[oó]n|"
    r"def\s|class\s|import\s|return\s|"
    r"syntaxerror|nameerror|typeerror|valueerror|indentationerror|"
    r"attributeerror|keyerror|indexerror|modulenotfounderror|"
    r"zerodivisionerror"
    r")\b",
    re.IGNORECASE,
)


def looks_like_code_or_error(text: str) -> bool:
    """ES: ¿`text` (ya extraído por OCR) parece código fuente o la salida
    de un error/traceback? Ver `_CODE_OR_ERROR_HINTS_RE` para el criterio
    exacto y por qué es deliberadamente laxo hacia los falsos negativos.
    EN: Does `text` (already OCR-extracted) look like source code or
    error/traceback output? See `_CODE_OR_ERROR_HINTS_RE` for the exact
    criterion and why it's deliberately lax toward false negatives."""
    if not text or len(text.strip()) < 6:
        return False
    return bool(_CODE_OR_ERROR_HINTS_RE.search(text))


_TRACEBACK_FILE_RE = re.compile(
    r"\b([A-Za-z0-9_\-./\\]+\.pyw?)\b", re.IGNORECASE
)


def extract_traceback_filename(text: str) -> Optional[str]:
    """
    ES: Nombre de archivo (solo el basename, sin ruta) de la ÚLTIMA
    mención "File ...py" en `text` — en un traceback de Python, el
    último frame listado es casi siempre el del script del propio
    usuario (los frames anteriores suelen ser de la librería estándar o
    de dependencias), así que es la mejor apuesta para el archivo que
    hay que corregir. Devuelve `None` si no hay ninguna coincidencia. El
    llamador (`Orchestrator._maybe_prepare_code_fix_from_image`) todavía
    tiene que confirmar que ese archivo existe de verdad en el sandbox
    antes de confiar en él.

    EN: Filename (basename only, no path) of the LAST "File ...py"
    mention in `text` — in a Python traceback, the last listed frame is
    almost always the user's own script (earlier frames are usually
    stdlib or dependencies), making it the best guess for the file that
    needs fixing. Returns `None` if there's no match. The caller still
    has to confirm that file actually exists in the sandbox before
    trusting it.
    """
    matches = _TRACEBACK_FILE_RE.findall(text or "")
    if not matches:
        return None
    last = matches[-1].strip().replace("\\", "/")
    basename = last.rsplit("/", 1)[-1]
    return basename or None


def prewarm_ocr_engine() -> None:
    """ES: Fuerza la carga del motor de OCR antes del primer uso real
    (mismo patrón que `prewarm_local_embedding_model` en embeddings.py —
    llamar desde un hilo de fondo al arrancar, nunca desde el hilo de UI).
    EN: Forces the OCR engine to load before first real use (same pattern
    as embeddings.py's `prewarm_local_embedding_model` — call from a
    background thread on startup, never from the UI thread)."""
    with contextlib.suppress(Exception):
        _get_ocr_engine()
