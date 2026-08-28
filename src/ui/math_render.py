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
El Monolito Personal - Arquitectura v2.0
math_render.py — Detección y renderizado de ecuaciones LaTeX embebidas en
la respuesta del modelo, como imágenes PNG (matplotlib mathtext) listas
para insertar en los QTextBrowser de la UI.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import struct
import threading
from typing import Dict, List, Optional, Pattern, Tuple

logger = logging.getLogger("SovNode.MathRender")

try:
    import matplotlib
    matplotlib.use("Agg")  # backend sin GUI - obligatorio: esta app ya tiene su propio event loop (Qt)
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    logger.warning(
        "⚠️ [MathRender] matplotlib no está instalado — las ecuaciones se "
        "mostrarán como texto plano (sin renderizar). `pip install matplotlib`."
    )

# =====================================================================
# COLOR ACTIVO - sincronizado con el tema visual de la UI
# =====================================================================
# Los tres temas de sovnode_qt.py (THEMES: Cyberpunk Dark/OLED Pure
# Black/Nordic Slate) son variantes oscuras con texto claro - no hay
# tema claro hoy. Se inicializa con el "text" de Cyberpunk Dark (tema
# por defecto) y MainWindow._apply_theme() lo actualiza en cada cambio
# de tema real, así la ecuación siempre queda legible contra el fondo
# activo en vez de quedar pinneada a un solo tema.
_active_color_lock = threading.Lock()
_active_equation_color = "#E6E8EC"


def set_equation_color(hex_color: str) -> None:
    """Actualiza el color con el que se renderizan las próximas ecuaciones — llamar desde MainWindow._apply_theme()."""
    global _active_equation_color
    with _active_color_lock:
        _active_equation_color = hex_color
    # El caché de render está indexado por (color, expresión) - ver
    # _cache_key() - así que un cambio de tema simplemente empieza a
    # generar entradas nuevas; las ecuaciones ya renderizadas con el
    # color anterior quedan servidas desde ahí hasta que el proceso de
    # los mensajes viejos las vuelva a pedir (no hace falta invalidar
    # nada a mano).


def _current_color() -> str:
    with _active_color_lock:
        return _active_equation_color


# =====================================================================
# DETECCIÓN - ¿este tramo entre [ ]/( )/$ $ es realmente una ecuación?
# =====================================================================
# Señal de que el contenido es LaTeX real y no texto entre paréntesis o
# corchetes cualquiera (ej. "(por ejemplo, en la Tierra)" o un link de
# Markdown "[ver fuente](url)"): un comando LaTeX (\frac, \mu, \pi...) o
# un sub/superíndice estilo TeX pegado a una llave (G_{...}, c^{4}) o a
# un solo dígito (E^2). Deliberadamente MÁS estricto que "cualquier _ o
# ^" - un guion bajo suelto en una URL o un nombre_de_variable de la
# respuesta NO debe disparar un render de ecuación.
_LATEX_SIGNAL_RE: Pattern[str] = re.compile(r"\\[a-zA-Z]+|[_^]\{|\b[A-Za-z]\^[0-9]")

# BUG REAL (medido): _LATEX_SIGNAL_RE por sí sola deja afuera ecuaciones
# comunísimas que NO usan comandos LaTeX ni llaves - "E = mc^2" no
# matchea (dos letras "mc" pegadas al "^", la regla solo acepta UNA), ni
# "F = ma" o "y = mx + b" (sin backslash, sin sub/superíndice). Esas
# ecuaciones sí renderizan bien en mathtext si se les da la oportunidad
# - el problema nunca fue el renderizado, fue que ni siquiera se
# intentaba. _looks_like_equation() agrega una segunda vía de detección:
# contenido con un operador relacional (=, <, >, ≤, ≥, ≈, ≠) Y donde
# cada tramo de letras seguidas mide ≤4 caracteres (cubre nombres de
# variable físicos típicos - F, m, a, v, t, E, mc - y abreviaturas como
# sin/cos/log/lim/sqrt). Ese último requisito es lo que sigue
# protegiendo contra falsos positivos: una frase en prosa como
# "(nota: mi_variable_local = 5)" tiene palabras de más de 4 letras
# ("nota", "variable", "local") y NO se trata como ecuación.
_RELATIONAL_OP_RE: Pattern[str] = re.compile(r"[=<>]|\\(?:le|ge|neq|approx)\b|[≤≥≈≠]")
_LONG_WORD_RE: Pattern[str] = re.compile(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{5,}")


def _looks_like_equation(expr: str) -> bool:
    if _LATEX_SIGNAL_RE.search(expr):
        return True
    if _RELATIONAL_OP_RE.search(expr) and not _LONG_WORD_RE.search(expr):
        return True
    return False


# Nota (medido - turno "dime las ecuaciones mas
# importantes de la fisica", captura de pantalla adjunta): una
# referencia SUELTA a una variable explícitamente marcada en modo
# matemático - "$p$", "$F$", "$m_1$" - no tiene operador relacional ni
# llaves ni backslash, así que _looks_like_equation() la rechaza (por
# diseño: ver su comentario arriba) y queda como texto crudo con los
# "$" literales visibles - 14 apariciones en una sola respuesta real.
#
# _looks_like_bare_variable() cubre ese hueco puntual, ANCLADA de punta
# a punta y sin permitir espacios - deliberadamente angosta. Se probó
# primero eliminar el filtro por completo para los delimitadores
# explícitos ($.../\(...\)), confiando en que mathtext fallaría solo
# ante contenido no-matemático: medido que es falso - texto como
# "cuesta $5 dolares, no $10" (dos "$" sueltos de precio, sin relación
# con LaTeX) matchea igual el patrón de delimitador, y mathtext
# RENDERIZA "5 dolares, no " sin quejarse (no es un error de parseo,
# solo texto en cursiva) en vez de fallar - así que quitar el filtro
# por completo introduce este bug nuevo. El ancla (^...$) y la
# prohibición de espacios son exactamente lo que distingue una variable
# suelta real ("p", "m_1") de esa mención de precio ("5 dolares, no ",
# que tiene espacios y una palabra de 7 letras) - nunca se aplica a los
# patrones de respaldo sin backslash (_BARE_FALLBACK_PATTERNS): "(a)"/
# "(b)" como viñetas de una enumeración son un caso real y común que
# esta regex identificaría como "variable suelta", perdiendo los
# paréntesis visibles de la viñeta al reemplazarlos por una imagen sin
# ellos - un costo que los delimitadores explícitos no tienen porque
# nadie escribe "$a$" como viñeta de lista.
_BARE_VARIABLE_RE: Pattern[str] = re.compile(r"^[A-Za-z](_[A-Za-z0-9]+)?$")


def _looks_like_bare_variable(expr: str) -> bool:
    return bool(_BARE_VARIABLE_RE.match(expr))


# Delimitadores escaneados, en orden. `[...]`/`(...)` son el formato que
# qwen2.5 viene usando de forma consistente en esta app (medido: captura
# real "[ G_{\mu\nu} = \frac{8\pi G}{c^4}... ]" para ecuaciones en
# bloque, "( G_{\mu\nu} )" para las inline dentro de la lista "Dónde:").
# $$...$$/$...$ se agregan por si el modelo cambia de convención -
# LaTeX estándar, no cuesta nada mantenerlos activos igual.
#
# Prefijo `(?:\\{1,2}\s*)?` en los dos patrones de $: medido en captura
# real que qwen2.5 a veces antepone un "\" o "\\" suelto justo antes del
# "$" de apertura - resto de un salto de línea LaTeX (\\) que el modelo
# copia aunque no haya tabla/alineación que lo justifique. Sin este
# prefijo, ese backslash queda FUERA del match y se ve como texto crudo
# pegado antes de la imagen ya renderizada ("\ [img]" en vez de "[img]").
# Se consume como parte de match.group(0) (lo que _replace() reemplaza
# por el placeholder) pero nunca entra a group(1) - la expresión que se
# le pasa a mathtext no lleva ese backslash espurio.
#
# BUG REAL (medido, screenshot del usuario - "Tell me the most importants
# equations in math"): faltaba por completo el delimitador ESTÁNDAR de
# LaTeX `\(...\)` (inline) / `\[...\]` (bloque) - qwen2.5 lo usa cuando
# responde en inglés (medido: 24/24 ecuaciones de esa respuesta real
# venían así, cero en formato `$...$` o `[...]`). Sin un patrón propio,
# el fallback de paréntesis sueltos de más abajo (`\(([^()]+?)\)`) lo
# "matcheaba" por accidente - pero mal: el backslash de APERTURA de
# `\(` queda AFUERA del match (el patrón solo busca el `(` literal, no
# lo que lo precede), y el backslash de CIERRE de `\)` queda DENTRO de
# la expresión capturada, como último carácter. Ese backslash colgante,
# pegado al `$` de cierre que arma render_equation_data_uri()
# (`f"${expr}$"`), lo convierte en `\$` - un `$` ESCAPADO para mathtext,
# no el cierre del modo matemático - así que la ecuación queda sin
# cerrar y mathtext tira una excepción. render_equation_data_uri() la
# atrapa y devuelve None (degradación silenciosa, por diseño), así que
# el síntoma es exactamente "texto LaTeX crudo, sin ningún error
# visible" - se ve como si nunca se hubiera intentado renderizar, pero
# en realidad se intentaba y fallaba en silencio en las 24 ecuaciones.
# Se agregan patrones DEDICADOS para ambos delimitadores, antes de los
# fallbacks de corchete/paréntesis sueltos - así consumen el par
# completo (los dos backslashes incluidos) antes de que el fallback
# alcance a hacer un match parcial roto. Mismo razonamiento aplica a
# `\[...\]`: aunque no se midió en este caso puntual, el fallback de
# corchetes sueltos (`\[([^\[\]]+?)\]`) tiene el mismo bug latente.
#
# Nota adicional (medido sobre la misma respuesta): cuando
# el patrón dedicado `\(...\)`/`\[...\]` de arriba sí extrae bien la
# expresión pero `render_equation_data_uri()` falla igual (típico: el
# propio modelo escribió un comando LaTeX inválido, medido con
# "\nabia" en vez de "\nabla" en una respuesta real de Navier-Stokes),
# `_replace()` deja el texto original intacto - backslashes y
# paréntesis/corchetes crudos incluidos, tal como estaban. Sin más
# resguardo, eso le da al FALLBACK de más abajo (paréntesis/corchetes
# sueltos, sin exigir backslash) una SEGUNDA oportunidad de matchear
# ese mismo tramo - y lo hace mal, con el bug ya documentado arriba
# (backslash de apertura afuera del match, de cierre adentro de la
# expresión). El síntoma visible: a veces esa segunda pasada sí
# renderiza (mathtext puede tolerar el backslash de cierre colgante
# de maneras que no siempre fallan), dejando un backslash suelto
# pegado justo antes de la imagen ("...: \[img]" en vez de "...: [img]").
# El resguardo real no es "que la segunda pasada renderice mejor" sino
# que nunca debería tener una segunda oportunidad: `(?<!\\)` al
# principio de los dos fallbacks impide que matcheen un `(`/`[` que
# venga precedido de un backslash - ese tramo ya fue evaluado (y
# aceptado o descartado) por el patrón dedicado de arriba, punto.
#
# Nota (medido - turno "dime las ecuaciones mas
# importantes de la fisica", captura de pantalla adjunta): ver el
# comentario completo junto a _looks_like_bare_variable() (arriba) para
# el detalle del bug (referencias sueltas como "$p$"/"$m_1$" quedando
# como texto crudo) y de por qué NO alcanza con eliminar el filtro de
# _looks_like_equation() del todo para los delimitadores explícitos.
#
# Lo que sí cambia acá: dos listas en vez de una. Un `$...$`, `$$...$$`,
# `\(...\)` o `\[...\]` es una marca INEQUÍVOCA de que el modelo quiso
# modo matemático - nadie escribe "$p$" en prosa suelta con otra
# intención - a diferencia de un `(...)`/`[...]` SIN backslash, que sí
# puede ser una aclaración entre paréntesis o un link de Markdown: ese
# es el caso real para el que _looks_like_equation() se diseñó (ver su
# comentario, arriba). _EXPLICIT_DELIMITER_PATTERNS acepta la unión de
# _looks_like_equation() Y _looks_like_bare_variable(); _BARE_FALLBACK_
# PATTERNS sigue exigiendo SOLO _looks_like_equation(), sin la variable
# suelta - ver ahí mismo por qué ("(a)"/"(b)" de una enumeración).
_EXPLICIT_DELIMITER_PATTERNS: List[Pattern[str]] = [
    re.compile(r"(?:\\{1,2}\s*)?\$\$(.+?)\$\$", re.DOTALL),
    re.compile(r"(?:\\{1,2}\s*)?(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", re.DOTALL),
    re.compile(r"\\\[(.+?)\\\]", re.DOTALL),
    re.compile(r"\\\((.+?)\\\)", re.DOTALL),
]
_BARE_FALLBACK_PATTERNS: List[Pattern[str]] = [
    re.compile(r"(?<!\\)\[([^\[\]]+?)\]", re.DOTALL),
    re.compile(r"(?<!\\)\(([^()]+?)\)", re.DOTALL),
]

# Placeholder: dos caracteres del Área de Uso Privado de Unicode (U+E000)
# alrededor de un índice - no son parte de ningún alfabeto real, así que
# Markdown los trata como texto plano inerte (verificado con
# QTextDocument headless) y no colisionan con contenido genuino del
# modelo en ningún idioma.
_PLACEHOLDER_MARK = "\ue000"


def _make_placeholder(index: int) -> str:
    return f"{_PLACEHOLDER_MARK}EQ{index}{_PLACEHOLDER_MARK}"


# =====================================================================
# RESGUARDO - spans de código inline (`...`) nunca deben leerse como
# ecuación
# =====================================================================
# BUG REAL (verificado leyendo el código, no solo supuesto): los bloques
# ```fenced``` YA se separan antes de llegar acá - ver
# MessageBubble._add_assistant_content/_render_content_now en
# sovnode_qt.py, que usa su propio CODE_PATTERN para armar un
# CodeBlockWidget aparte y nunca pasa ese contenido por este módulo. Pero
# un span de código INLINE con un solo backtick (`if (a == b):`) no pasa
# por ningún filtro parecido - llega a este texto tal cual, y su
# contenido entre backticks es indistinguible para _BARE_FALLBACK_PATTERNS de
# una ecuación real: "(a == b)" tiene paréntesis, un operador relacional
# (=) y nombres de una sola letra (a, b) - exactamente lo que
# _looks_like_equation() busca. Sin este resguardo, ese span de código
# se reemplaza por una imagen PNG y el texto literal que el modelo quiso
# mostrar como código desaparece.
# Mismo mecanismo de placeholder que ya usa este archivo para ecuaciones
# (Área de Uso Privado de Unicode, inerte para Markdown/QTextDocument),
# con un tag distinto ("CODE" en vez de "EQ") para no colisionar. Los
# spans enmascarados se restauran TAL CUAL (nunca se convierten en
# imagen) antes de devolver el resultado - este resguardo solo protege,
# no interpreta ese contenido.
_INLINE_CODE_RE: Pattern[str] = re.compile(r"`[^`\n]+`")


def _mask_inline_code(text: str) -> Tuple[str, Dict[str, str]]:
    spans: Dict[str, str] = {}
    counter = 0

    def _mask(match: "re.Match[str]") -> str:
        nonlocal counter
        token = f"{_PLACEHOLDER_MARK}CODE{counter}{_PLACEHOLDER_MARK}"
        counter += 1
        spans[token] = match.group(0)
        return token

    masked = _INLINE_CODE_RE.sub(_mask, text)
    return masked, spans


def _unmask_inline_code(text: str, spans: Dict[str, str]) -> str:
    for token, original in spans.items():
        text = text.replace(token, original)
    return text


# =====================================================================
# RENDER - LaTeX -> PNG transparente -> data URI, con caché
# =====================================================================
_render_cache_lock = threading.Lock()
_render_cache: Dict[str, Tuple[str, Optional[int], Optional[int]]] = {}
# Tope simple: este caché solo existe para no re-renderizar la misma
# ecuación en cada tick de streaming (~cada 90ms, ver MessageBubble.
# MIN_RENDER_INTERVAL_MS) mientras un mensaje se sigue completando - no
# necesita sobrevivir entre turnos ni ser LRU real. Si se llena, se
# vacía entero: la próxima ecuación que lo pida paga un render de más,
# nada más.
_RENDER_CACHE_MAX_ENTRIES = 500


def _cache_key(expr: str, color: str) -> str:
    return hashlib.sha1(f"{color}|{expr}".encode("utf-8")).hexdigest()


# =====================================================================
# TAMAÑO DE VISUALIZACIÓN - compensar el DPI de render contra el DPI de
# referencia de CSS/Qt
# =====================================================================
# BUG REAL (reporte del usuario, captura de pantalla - "Desproporción en
# el renderizado visual: la escala de DPI con la que Matplotlib genera
# las ecuaciones quedó muy grande en comparación con la tipografía base
# del chat... símbolos gigantescos"). Causa: dpi=170 (elegido para que
# el trazo se vea nítido en pantallas HiDPI) NO es el mismo "dpi" que
# asume una hoja de estilo CSS/Qt (96dpi, la referencia estándar de
# "1px = 1/96 pulgada"). fontsize=13 en matplotlib está pensado para
# IGUALAR visualmente el "font-size: 13px" real de la burbuja de chat
# (ver sovnode_qt.py, estilo del contenido de MessageBubble) - pero un
# carácter de 13pt renderizado a 170dpi mide en la práctica
# 13 * 170/72 ≈ 30.7px de alto, más del doble de lo que ocupa el texto
# que lo rodea. Sin ningún `width`/`height` explícito en el `<img>` (ver
# splice_images_into_html más abajo), Qt lo muestra a su tamaño nativo
# - de ahí los "símbolos gigantescos" del reporte, más notorio cuanto
# más chico es el glifo (un solo carácter suelto, con poco alrededor que
# "diluya" la desproporción visualmente - el caso exacto de la θ
# reportado).
#
# Corrección: en vez de bajar el dpi (perdiendo nitidez en HiDPI), se
# mide el tamaño real en píxeles del PNG ya renderizado - leyendo
# ancho/alto directamente del header IHDR, formato fijo de la
# especificación PNG, sin depender de ninguna librería extra - y se
# escala al equivalente en píxeles CSS (dpi de referencia 96) para
# usarlo como atributo `width`/`height` del `<img>`. Así la imagen se ve
# nítida (se renderizó a más resolución de la que ocupa en pantalla)
# pero OCUPA el espacio que le corresponde según su propio fontsize, sin
# importar cuán ajustado quede el bbox de cada expresión particular.
_CSS_REFERENCE_DPI = 96.0
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_pixel_size(png_bytes: bytes) -> Optional[Tuple[int, int]]:
    """Ancho/alto reales del PNG (píxeles nativos del render), leídos del
    header IHDR — 8 bytes de firma + 4 de longitud de chunk + 4 de tipo
    "IHDR" = offset 16, dos enteros de 4 bytes big-endian.

    None si el buffer no tiene el formato esperado — degradación
    silenciosa, igual que el resto de este módulo: preferible mostrar la
    imagen a tamaño nativo (el comportamiento de siempre, antes de esta
    corrección) que reventar el render por un cálculo de escala que
    falló.
    """
    try:
        if png_bytes[:8] != _PNG_SIGNATURE:
            return None
        width, height = struct.unpack(">II", png_bytes[16:24])
        return width, height
    except Exception:
        return None


# Nota (medido dos veces de forma independiente: la misma
# respuesta de Navier-Stokes analizada contra la DB real del usuario
# antes en este archivo, y de nuevo en un video de prueba posterior -
# mismo error exacto, misma sección, confirmado extrayendo frames del
# video directamente). "\nabia" no es un comando LaTeX que matplotlib
# no soporte: no existe en NINGÚN paquete de LaTeX real, es simplemente
# el modelo escribiendo mal "\nabla" (el operador ∇, nabla). Verificado
# con la expresión real completa: con "\nabia" el render falla (None);
# con el mismo texto y SOLO esa palabra corregida a "\nabla", renderiza
# perfecto - nada más en la expresión (\boldsymbol, \mathbf, \frac,
# \partial) le molesta a matplotlib.
#
# Corrección ACOTADA a errores confirmados y sin ninguna ambigüedad -
# ESTO NO ES un corrector ortográfico genérico de LaTeX. Un corrector
# genérico es peligroso: "arreglaría" a ciegas un comando real que el
# modelo escribió distinto de lo esperado a propósito, o un typo NUEVO
# de forma incorrecta. Esta lista solo crece cuando un typo puntual se
# repite y se confirma contra evidencia real - no se generaliza el
# mecanismo a "similaridad" ni a distancia de edición.
_KNOWN_MODEL_TYPOS: Dict[str, str] = {
    "\\nabia": "\\nabla",
}


def _fix_known_model_typos(expr: str) -> str:
    for wrong, right in _KNOWN_MODEL_TYPOS.items():
        expr = expr.replace(wrong, right)
    return expr


# =====================================================================
# COMANDOS LATEX REALES QUE MATHTEXT NO SOPORTA (distinto de un typo)
# =====================================================================
# Nota (medido - turno "hola dime las ecuaciones mas
# improtantes de la matematicas", captura de pantalla adjunta): a
# diferencia de _KNOWN_MODEL_TYPOS (arriba), acá el modelo NO se
# equivocó - \boxed{...} es un comando LaTeX real y válido (paquete
# amsmath, uso estándar para remarcar una fórmula final), pero
# matplotlib.mathtext solo implementa un SUBCONJUNTO de LaTeX y no lo
# reconoce (confirmado en vivo: ParseFatalException "Unknown symbol:
# \boxed"). medido sobre la respuesta real: de 10 ecuaciones, las 8
# envueltas en \boxed{...} fallaron todas - render_equation_data_uri()
# devuelve None y _replace() (ver extract_equations_as_placeholders)
# las deja como texto LaTeX crudo, "$\boxed{...}$" literal en pantalla
# - mientras que la única sin envolver (identidad de Euler) renderizó
# perfecto. Reproducido end-to-end con extract_equations_as_placeholders()
# antes de aplicar este fix, no solo con render_equation_data_uri() suelta.
#
# La corrección NO es "arreglar" el comando (no existe un \boxed
# equivalente dentro de mathtext) sino DESENVOLVER: extraer el
# contenido de adentro de las llaves y renderizar ESO. Se pierde el
# recuadro visual de énfasis - se probó devolverlo con
# `Text.set_bbox()` de matplotlib, y funciona con fondo blanco, pero
# con `Figure(facecolor="none")` + `savefig(bbox_inches="tight")` (la
# configuración real de este archivo) el recuadro simplemente no
# aparece en el PNG final, y no vale la pena el riesgo de tocar más el
# pipeline de tamaño/recorte ya estabilizado (ver "TAMAÑO DE
# VISUALIZACIÓN" más arriba) por un efecto puramente cosmético. Una
# ecuación legible SIN recuadro sigue siendo estrictamente mejor que
# texto LaTeX roto.
#
# Requiere conteo de llaves BALANCEADAS, no una regex no-greedy: el
# contenido de \boxed{...} casi siempre trae llaves ANIDADAS
# (\frac{a}{b}, \sum_{n=0}^\infty, etc.) - una regex del tipo
# \\boxed\{(.*?)\} cortaría en la PRIMERA "}" que encuentre. medido:
# para \boxed{f(x) = \sum_{n=0}^\infty \frac{f^{(n)}(a)}{n!}(x-a)^n},
# esa regex captura solo "f(x) = \sum_{n=0" - el resto de la expresión
# se pierde. _unwrap_brace_command() de abajo cuenta llaves en vez de
# adivinar dónde termina el contenido.
#
# _MATHTEXT_UNSUPPORTED_BOX_COMMANDS lista los alias conocidos de
# "poner un recuadro" que comparten el mismo problema y el mismo fix.
# \fbox se agrega por el mismo motivo confirmado en pruebas directas
# contra matplotlib (misma ParseFatalException), aunque todavía no se
# lo vio en una respuesta real del modelo - a diferencia de
# _KNOWN_MODEL_TYPOS, que exige evidencia real por cada entrada, acá
# generalizar a un alias de la misma familia semántica ("recuadro
# LaTeX no soportado por mathtext") con el mismo fix mecánico no tiene
# el riesgo que sí tendría un corrector ortográfico genérico.
_MATHTEXT_UNSUPPORTED_BOX_COMMANDS: Tuple[str, ...] = ("\\boxed", "\\fbox")


def _unwrap_brace_command(expr: str, command: str) -> str:
    """
    Reemplaza cada aparición de `command{contenido}` por `contenido`,
    contando llaves balanceadas para encontrar el cierre correcto —
    ver BLINDAJE arriba sobre por qué una regex no-greedy corta mal
    contenido con llaves anidadas. Si `command` aparece sin una "{"
    inmediatamente después, o con llaves que nunca cierran, esa
    aparición puntual se deja intacta (degradación silenciosa, mismo
    criterio que el resto del archivo: ante la duda, no tocar).
    Recursivo sobre el contenido desenvuelto para cubrir el caso (raro,
    pero barato de cubrir) de `command` anidado dentro de sí mismo.
    """
    result: List[str] = []
    i = 0
    n = len(expr)
    while i < n:
        if (
            expr.startswith(command, i)
            and i + len(command) < n
            and expr[i + len(command)] == "{"
        ):
            depth = 1
            j = i + len(command) + 1
            while j < n and depth > 0:
                if expr[j] == "{":
                    depth += 1
                elif expr[j] == "}":
                    depth -= 1
                j += 1
            if depth == 0:
                inner = expr[i + len(command) + 1 : j - 1]
                result.append(_unwrap_brace_command(inner, command))
                i = j
                continue
        result.append(expr[i])
        i += 1
    return "".join(result)


def _strip_unsupported_box_commands(expr: str) -> str:
    for command in _MATHTEXT_UNSUPPORTED_BOX_COMMANDS:
        expr = _unwrap_brace_command(expr, command)
    return expr


# =====================================================================
# NORMALIZACIÓN PARA MATHTEXT — familia de sintaxis de gpt-oss:20b
# =====================================================================
# Nota (medido - arquitectura de modelo único 2026-08-27, turno "dime las
# ecuaciones mas importantes de la fisica" contra gpt-oss:20b real,
# captura del usuario: de 10 ecuaciones, 7 quedaban como texto LaTeX
# crudo). gpt-oss escribe LaTeX MÁS rico que qwen2.5/phi3.5, y varias de
# sus construcciones habituales son válidas en LaTeX real pero NO en el
# subconjunto de matplotlib.mathtext de esta versión. Confirmado token a
# token contra matplotlib directo:
#
#   \displaystyle / \textstyle / \scriptstyle / \limits — pistas de
#       tamaño; mathtext las rechaza ("Unknown symbol: \displaystyle").
#       Se BORRAN: no cambian el significado, solo el estilo tipográfico.
#   \mathbf F (vector SIN llaves) — falla; \mathbf{F} (CON llaves) sí
#       renderiza. gpt-oss casi siempre lo escribe sin llaves. Se
#       envuelve el token siguiente en llaves. \boldsymbol / \bm se
#       mapean a \mathbf (mathtext no conoce los dos primeros).
#   \frac12  — mathtext exige \frac{num}{den} con llaves ("Expected
#       \\frac{num}{den}, found '12'"). Se agregan llaves cuando los dos
#       argumentos son un único token cada uno (\frac AB -> \frac{A}{B}).
#   \ge \le \ne — ESTA versión de mathtext no conoce las formas cortas
#       (sí \geq \leq \neq). Idem \implies -> \Rightarrow, \iff ->
#       \Leftrightarrow, \land -> \wedge, \lor -> \vee.
#
# Igual que _fix_known_model_typos / _strip_unsupported_box_commands:
# transformación ACOTADA a construcciones confirmadas, degradación
# silenciosa, nunca un "traductor de LaTeX" genérico. Todo lo que no
# matchee queda intacto y, si mathtext falla igual, el texto crudo
# original se conserva (mismo contrato de siempre).
_MATHTEXT_SIZE_HINT_RE: Pattern[str] = re.compile(
    r"\\(?:displaystyle|textstyle|scriptstyle|limits|nolimits)(?![A-Za-z])\s*"
)
_MATHTEXT_BOLD_BRACE_RE: Pattern[str] = re.compile(r"\\(?:boldsymbol|bm)\s*\{")
_MATHTEXT_BOLD_NOBRACE_RE: Pattern[str] = re.compile(
    r"\\(?:mathbf|boldsymbol|bm)\s+([A-Za-z0-9])(?![A-Za-z])"
)
_MATHTEXT_FRAC_BARE_RE: Pattern[str] = re.compile(
    r"\\frac\s*([0-9A-Za-z])\s*([0-9A-Za-z])(?![0-9A-Za-z{}])"
)
_MATHTEXT_ALIAS: Dict[str, str] = {
    "\\ge": "\\geq", "\\le": "\\leq", "\\ne": "\\neq",
    "\\implies": "\\Rightarrow", "\\impliedby": "\\Leftarrow",
    "\\iff": "\\Leftrightarrow", "\\land": "\\wedge", "\\lor": "\\vee",
    "\\lnot": "\\neg",
}
_MATHTEXT_ALIAS_RE: Pattern[str] = re.compile(
    r"\\(?:ge|le|ne|implies|impliedby|iff|land|lor|lnot)(?![A-Za-z])"
)


def _normalize_for_mathtext(expr: str) -> str:
    """Reescribe construcciones LaTeX válidas que matplotlib.mathtext no
    soporta a su equivalente que sí renderiza — ver BLINDAJE arriba. Si
    `expr` no contiene ninguna, es un no-op."""
    expr = _MATHTEXT_SIZE_HINT_RE.sub("", expr)
    expr = _MATHTEXT_BOLD_BRACE_RE.sub("\\\\mathbf{", expr)
    expr = _MATHTEXT_BOLD_NOBRACE_RE.sub(r"\\mathbf{\1}", expr)
    expr = _MATHTEXT_FRAC_BARE_RE.sub(r"\\frac{\1}{\2}", expr)
    expr = _MATHTEXT_ALIAS_RE.sub(lambda m: _MATHTEXT_ALIAS[m.group(0)], expr)
    return expr


def render_equation_data_uri(
    expr: str, color: Optional[str] = None, fontsize: int = 13, dpi: int = 170,
) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    """
    Renderiza una expresión LaTeX a PNG transparente y la devuelve como
    (data_uri, css_width, css_height), lista para
    `<img src="..." width=... height=...>`. None si matplotlib no está
    disponible o la expresión no compila — degradación SIEMPRE
    silenciosa: una ecuación que no renderiza debe dejar el texto
    original intacto, nunca romper el resto del mensaje.

    `css_width`/`css_height` son el tamaño en píxeles LÓGICOS (referencia
    96dpi — ver el comentario de la sección "TAMAÑO DE VISUALIZACIÓN" más
    arriba) al que debe mostrarse la imagen para que quede proporcional
    al `fontsize` pedido, en vez de su tamaño nativo de render (que
    puede duplicar o más el tamaño real del texto circundante). Vienen
    en None — nunca hace fallar la función completa — si el PNG generado
    no pudo leerse.

    Antes de intentar renderizar, normaliza errores de tipeo del modelo
    ya confirmados (ver `_KNOWN_MODEL_TYPOS`) — p. ej. "\\nabia" ->
    "\\nabla". Si `expr` no contiene ninguno de esos typos, esta
    normalización es un no-op (ni una sola sustitución ocurre).

    También desenvuelve comandos LaTeX reales que mathtext no soporta
    (ver `_MATHTEXT_UNSUPPORTED_BOX_COMMANDS`) — p. ej.
    "\\boxed{a^2+b^2=c^2}" -> "a^2+b^2=c^2". Sin esto, CUALQUIER
    ecuación envuelta en \\boxed{...} fallaba en silencio y quedaba
    como texto LaTeX crudo en pantalla (BLINDAJE completo junto a
    `_MATHTEXT_UNSUPPORTED_BOX_COMMANDS`, más arriba).
    """
    if not MATPLOTLIB_AVAILABLE:
        return None

    expr = _fix_known_model_typos(expr)
    expr = _strip_unsupported_box_commands(expr)
    expr = _normalize_for_mathtext(expr)

    resolved_color = color or _current_color()
    key = _cache_key(expr, resolved_color)

    with _render_cache_lock:
        cached = _render_cache.get(key)
    if cached is not None:
        return cached

    try:
        fig = Figure(facecolor="none")
        FigureCanvasAgg(fig)
        # fig.text en vez de un Axes con ecuación: no hay nada más en la
        # figura (sin ejes, sin ticks, sin márgenes de gráfico) - el
        # bbox_inches="tight" de abajo recorta al glifo real, así el PNG
        # resultante mide lo que mide el texto, no un lienzo de gráfico
        # de tamaño fijo con la ecuación perdida en una esquina.
        fig.text(0, 0, f"${expr}$", fontsize=fontsize, color=resolved_color)
        buf = io.BytesIO()
        fig.savefig(
            buf, format="png", dpi=dpi, transparent=True,
            bbox_inches="tight", pad_inches=0.04,
        )
    except Exception as exc:
        # Motivo típico: sintaxis que mathtext no soporta (mathtext es un
        # SUBCONJUNTO de LaTeX - sin \begin{...}, sin \text{} en versiones
        # viejas de matplotlib, etc.). No es un error de la app, es una
        # ecuación que el modelo escribió fuera de lo que este renderer
        # cubre - se deja el texto crudo tal cual, como si este módulo no
        # existiera.
        logger.debug("No se pudo renderizar la ecuación %r: %s", expr, exc)
        return None

    png_bytes = buf.getvalue()
    data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    css_width: Optional[int] = None
    css_height: Optional[int] = None
    pixel_size = _png_pixel_size(png_bytes)
    if pixel_size is not None:
        native_w, native_h = pixel_size
        scale = dpi / _CSS_REFERENCE_DPI
        # max(1, ...): un glifo minúsculo podría redondear a 0px, que es
        # indistinguible de "no se renderizó nada" - peor que quedarse
        # ligeramente grande.
        css_width = max(1, round(native_w / scale))
        css_height = max(1, round(native_h / scale))

    result: Tuple[str, Optional[int], Optional[int]] = (data_uri, css_width, css_height)

    with _render_cache_lock:
        if len(_render_cache) >= _RENDER_CACHE_MAX_ENTRIES:
            _render_cache.clear()
        _render_cache[key] = result

    return result


# =====================================================================
# INTEGRACIÓN - texto con [ecuaciones] -> placeholders -> <img> real
# =====================================================================
def extract_equations_as_placeholders(
    text: str,
) -> Tuple[str, Dict[str, Tuple[str, Optional[int], Optional[int]]]]:
    """
    Reemplaza cada expresión LaTeX detectada en `text` por un placeholder
    de texto plano y devuelve
    (texto_con_placeholders, {placeholder: (data_uri, css_width, css_height)}).

    Debe llamarse ANTES de QTextDocument.setMarkdown() — ver el
    docstring del módulo para por qué no se puede insertar el <img>
    directamente en el texto fuente. Si no hay matplotlib disponible o
    no se detecta ninguna ecuación real, devuelve `text` sin tocar y un
    dict vacío — el llamador puede entonces saltarse el paso 2
    (toHtml/splice/setHtml) por completo, sin costo extra para el caso
    común de un mensaje sin matemática.
    """
    if not MATPLOTLIB_AVAILABLE or not text:
        return text, {}

    # Ver _mask_inline_code(): protege `spans de código inline` antes de
    # que los patrones de detección de ecuaciones (más abajo) los vea.
    # Si no hay ningún backtick en `text`, esto es un no-op (dict vacío,
    # texto sin tocar).
    text, inline_code_spans = _mask_inline_code(text)

    placeholders: Dict[str, Tuple[str, Optional[int], Optional[int]]] = {}
    counter = 0

    def _try_render(expr: str, match_text: str) -> str:
        nonlocal counter
        rendered = render_equation_data_uri(expr)
        if rendered is None:
            return match_text
        token = _make_placeholder(counter)
        counter += 1
        placeholders[token] = rendered
        return token

    def _replace_explicit(match: "re.Match[str]") -> str:
        # ver la nota junto a _EXPLICIT_DELIMITER_PATTERNS/
        # _looks_like_bare_variable(): acepta una ecuación "de verdad"
        # (_looks_like_equation) O una variable suelta explícitamente
        # marcada en modo matemático ("$p$", "$m_1$") - nunca texto
        # arbitrario, aunque el delimitador sea inequívoco (ver el caso
        # medido de "$5 dolares, no $10" en el mismo nota).
        expr = match.group(1).strip()
        if not expr or not (_looks_like_equation(expr) or _looks_like_bare_variable(expr)):
            return match.group(0)
        return _try_render(expr, match.group(0))

    def _replace_fallback(match: "re.Match[str]") -> str:
        # Bare (...)/[...] sin backslash: sigue necesitando el filtro,
        # ver _looks_like_equation() y su propio comentario.
        expr = match.group(1).strip()
        if not expr or not _looks_like_equation(expr):
            return match.group(0)  # paréntesis/corchetes normales - se deja intacto
        return _try_render(expr, match.group(0))

    result = text
    for pattern in _EXPLICIT_DELIMITER_PATTERNS:
        result = pattern.sub(_replace_explicit, result)
    for pattern in _BARE_FALLBACK_PATTERNS:
        result = pattern.sub(_replace_fallback, result)

    result = _unmask_inline_code(result, inline_code_spans)

    return result, placeholders


def splice_images_into_html(
    html: str,
    placeholders: Dict[str, Tuple[str, Optional[int], Optional[int]]],
) -> str:
    """Reemplaza cada placeholder por su `<img>` real, sobre el HTML que ya produjo QTextDocument.toHtml().

    Cada valor de `placeholders` es (data_uri, css_width, css_height) —
    ver la sección "TAMAÑO DE VISUALIZACIÓN" más arriba: width/height
    vienen en píxeles lógicos ya escalados para que la imagen ocupe el
    espacio proporcional a su fontsize, no su tamaño nativo de render
    (que puede duplicar o más el tamaño del texto circundante — el bug
    real de "símbolos gigantescos" reportado). Si no se pudieron calcular
    (PNG con header inesperado), se omiten y el `<img>` cae a tamaño
    nativo — el comportamiento que esta función tenía antes de esta
    corrección, nunca peor que eso.
    """
    if not placeholders:
        return html
    for token, rendered in placeholders.items():
        data_uri, css_width, css_height = rendered
        size_attrs = (
            f' width="{css_width}" height="{css_height}"'
            if css_width is not None and css_height is not None
            else ""
        )
        html = html.replace(
            token,
            f'<img src="{data_uri}"{size_attrs} style="vertical-align: middle;" />',
        )
    return html
