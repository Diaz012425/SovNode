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
router.py — Enrutador determinista de intención con exclusión mutua
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Pattern, Tuple


class RoutePath(str, Enum):
    FAST_PATH = "fast_path"
    SLOW_PATH = "slow_path"


class SignalTag(str, Enum):
    TRIVIAL_GREETING = "trivial_greeting"
    SHORT_QUERY = "short_query"
    EMPTY_INPUT = "empty_input"
    CODE_BOILERPLATE = "code_boilerplate"
    CODE_COMPLEX = "code_complex"
    MATH_EXPRESSION = "math_expression"
    FACTUAL_ENUMERATION = "factual_enumeration"
    QUANT_CLAIM = "quant_claim"
    LOGIC_AUDIT = "logic_audit"
    CONCEPTUAL_DENSE = "conceptual_dense"
    EXISTENTIAL_SELF = "existential_self"
    LONG_TEXT = "long_text"
    LSC_INFERENCE = "lsc_inference"
    WEB_SEARCH_INTENT = "web_search_intent"
    CONVERSATIONAL_FOLLOWUP = "conversational_followup"
    FORMULA_DISCOVERY = "formula_discovery"
    # BLINDAJE (2026-09-19, patch_router2): ver `_GENERIC_EDIT_CONTINUATION_RE`
    # más abajo -- cuarto eje de neutralización de WEB_SEARCH_INTENT.
    GENERIC_EDIT_CONTINUATION = "generic_edit_continuation"


@dataclass(frozen=True)
class RoutingDecision:
    path: RoutePath
    tags: Tuple[SignalTag, ...]
    score: float
    reason: str
    elapsed_ms: float
    text_length: int


class IntentRouter:
    """
    Clasificador de intención determinista basado en señales
    léxico-sintácticas ponderadas.

    No depende de LLM, red ni estado externo. Las etiquetas activas se
    incluyen en RoutingDecision.tags para permitir que el orquestador
    seleccione el motor especializado adecuado.

    Matriz de exclusión mutua (blindaje v3.9):
        Si se detecta CODE_COMPLEX (por verbo, extensión de archivo o
        librería), WEB_SEARCH_INTENT queda forzosamente desactivado
        para ese turno, sin importar qué palabras temporales/de
        actualidad contenga el resto del texto. Un desarrollo de
        software nunca debe interceptarse como búsqueda web.
    """

    SLOW_PATH_THRESHOLD: float = 1.5

    WEIGHT_MATH: float = 3.0
    WEIGHT_FACTUAL_ENUMERATION: float = 0.0
    WEIGHT_FORMULA_DISCOVERY: float = 0.0
    # BLINDAJE (pedido explícito del usuario, 2026-09-16 -- bug real
    # medido por WAL, turno be343204-f6e9-4e08-bda2-b75a2f325cc4:
    # "dame un codigo que haga un slither.io base" -- CODE_COMPLEX
    # solo (sin otra señal) sumaba +3.0, superando SLOW_PATH_THRESHOLD
    # =1.5 por sí solo. slow_path está ajustado para razonamiento
    # largo/verificación factual, no para código: su circuit-breaker
    # (`_slowpath_response_looks_broken`, detector de repetición
    # degenerativa medido contra texto, no contra código real) lo
    # marcó roto, y su única regeneración usa un prompt genérico de
    # 200 tokens -- inservible para regenerar código -- así que cayó
    # al fallback genérico tras DOS llamadas pagas a Sonnet ($0.047,
    # cero código entregado). Mismo patrón medido 3 veces más en el
    # WAL de este usuario. Un turno de código PURO ahora va a
    # fast_path (que ya usa el presupuesto de codegen vía
    # `_wants_codegen_budget`/`SignalTag.CODE_COMPLEX` sin depender
    # de este peso, y tiene su propio circuit-breaker CON
    # regeneración); código combinado con una señal real de
    # razonamiento complejo (matemática, auditoría lógica, densidad
    # conceptual) sigue yendo a slow_path por mérito de ESA otra
    # señal, sin cambios acá. El tag CODE_COMPLEX en sí (usado por
    # `_wants_codegen_budget`, el modo sugerencia, etc.) no se toca --
    # solo cuánto pesa hacia el umbral de slow_path.
    WEIGHT_CODE_COMPLEX: float = 0.0
    WEIGHT_QUANT_CLAIM: float = 2.0
    WEIGHT_LOGIC_AUDIT: float = 4.0
    WEIGHT_CONCEPTUAL: float = 3.0
    WEIGHT_EXISTENTIAL: float = 2.5
    WEIGHT_LSC: float = 4.0
    WEIGHT_WEB_SEARCH: float = 3.0
    WEIGHT_LONG_TEXT_MEDIUM: float = 1.0
    WEIGHT_LONG_TEXT_HIGH: float = 2.0
    WEIGHT_TRIVIAL_PULL: float = -5.0
    WEIGHT_BOILERPLATE_PULL: float = -2.0
    WEIGHT_FOLLOWUP_PULL: float = -3.0

    SHORT_QUERY_CHARS: int = 20
    LONG_TEXT_MEDIUM_CHARS: int = 400
    LONG_TEXT_HIGH_CHARS: int = 800

    _WEB_KEYWORDS_RE: Pattern[str] = re.compile(
        r"\b("
        r"busca|b[uú]scam?e?|investiga\w*|averigua\w*|googlea\w*|consulta\w*|rastrea\w*|encuentra\w*|chequea\w*|inf[oó]rmam?e?|"
        r"search|find|google|lookup|check|investigate|track|inform|"
        r"hoy|ayer|ma[nñ]ana|reciente\w*|actual\w*|últim[oa]s?|ultim[oa]s?|[uú]ltima\s+hora|ahora|en\s+vivo|live|al\s+momento|en\s+tiempo\s+real|"
        r"today|yesterday|tomorrow|recent|latest|current|breaking|now|real\s*time|"
        r"este\s+a[nñ]o|esta\s+semana|este\s+mes|2024|2025|2026|2027|"
        r"this\s+year|this\s+week|this\s+month|"
        r"noticia\w*|suceso\w*|acontecimiento\w*|pas[oó]|sucedi[oó]|ocurri[oó]|reporte\w*|declaraci[oó]n\w*|comunicado\w*|"
        r"news|event|happened|occurred|report|statement|announcement|"
        r"terremoto\w*|sismo\w*|temblor\w*|tsunami\w*|hurac[aá]n\w*|tormenta\w*|tragedia\w*|accidente\w*|erupci[oó]n\w*|incendio\w*|inundaci[oó]n\w*|cat[aá]strofe\w*|"
        r"earthquake\w*|quake\w*|tremor\w*|tsunami\w*|hurricane\w*|storm\w*|tragedy|accident|eruption|fire|flood\w*|disaster\w*|"
        r"qui[eé]n\w*|qu[eé]\s+(?:pas[oó]|sucedi[oó]|ocurri[oó]|pasaba|dijo|fue)|cu[aá]ndo\w*|a\s+qu[eé]\s+hora|d[oó]nde\s+(?:es|fue|ocurri[oó]|queda)|"
        r"cu[aá]nto\w*|cuanto\w*|cu[aá]l\s+(?:es|fue|ser[aá])|qu[eé]\s+se\s+sabe|qu[eé]\s+hay\s+de|"
        r"who|what\s+(?:happened|occurred|is|was)|when|where|how\s+much|how\s+many|which|"
        r"mundial\w*|final\w*|semifinal\w*|cuartos|partido\w*|juegos\s+ol[ií]mpicos|olimpiadas|torneo\w*|copa\w*|champions|liga\w*|eliminatorias|"
        r"marcador\w*|resultado\w*|goles|goleador\w*|alineaci[oó]n\w*|clasificaci[oó]n\w*|posiciones|f[uú]tbol|baloncesto|nba|f1|gran\s+premio|carrera|super\s+bowl|"
        r"campe[oó]n\w*|ganador\w*|derrota\w*|victoria\w*|empate\w*|vs|contra|"
        r"world\s+cup|match|game|score|standings|winner|champion|loss|victory|tie|draw|soccer|football|"
        r"precio\w*|cotizaci[oó]n\w*|d[oó]lar|euro|bitcoin|btc|crypto|cripto|bolsa|acciones|inflaci[oó]n\w*|costo\w*|tarifa\w*|"
        r"cu[aá]nto\s+(?:cuesta|vale|cotiza|est[aá])|"
        r"price|rate|dollar|euro|stock|shares|inflation|cost|fee|how\s+much\s+is|"
        r"clima|tiempo|pron[oó]stico\w*|temperatura\w*|estreno\w*|taquilla\w*|pel[ií]cula\w*|serie\w*|parche|versi[oó]n|update|lanzamiento\w*|nerfeo|buff|"
        r"weather|forecast|temperature|release|box\s+office|movie|show|patch|version"
        r")\b",
        re.IGNORECASE,
    )

    _EXISTENTIAL_RE: Pattern[str] = re.compile(
        r"\b("
        r"existencia|sentir|consciencia|autoevaluar|disfrutar|ego|vivo|"
        r"sentimiento|alma|emoci[oó]n|consciente"
        r")\b",
        re.IGNORECASE,
    )

    _TRIVIAL_GREETING_RE: Pattern[str] = re.compile(
        r"^\s*(?:"
        r"hola|hi|hello|hey|heya|hiya|holis|buenas|saludos|"
        r"buenos?\s+d[ií]as|buen\s+d[ií]a|buenas\s+(?:tardes|noches)|"
        r"good\s+(?:morning|afternoon|evening|night)|greetings|"
        r"qu[eé]\s+tal|c[oó]mo\s+(?:est[aá]s|and[aá]s|va|te\s+va|le\s+va)|"
        r"adi[oó]s|chau|chao|hasta\s+(?:luego|pronto|ma[ñn]ana)|nos\s+vemos|"
        r"bye|goodbye|see\s+(?:you|ya)|take\s+care|cya"
        r")\b"
        r"|^\s*(?:"
        r"gracias(?:\s+(?:totales|mil))?|muchas\s+gracias|mil\s+gracias|"
        r"thanks(?:\s+a\s+lot)?|thank\s+you(?:\s+so\s+much)?|thx|ty|"
        r"de\s+nada|you'?re\s+welcome|no\s+problem|np|"
        r"ok|okay|oka|okey|vale|dale|listo|perfecto|genial|b[aá]rbaro|"
        r"cool|nice|got\s+it|entendido|understood|great|awesome"
        r")[\s.!¡¿?]*$",
        re.IGNORECASE,
    )

    _MATH_PATTERN: Pattern[str] = re.compile(
        r"("
        r"\d+\s*[\+\-\*/\^]\s*\d+|"
        r"\bresuelve\b|\bderivada\b|\bintegral\b|"
        r"\bsimplifica\w*\b|\bfactoriza\w*\b|"
        r"\bcalcul\w+\b.{0,25}\b(valor|resultado|ra[ií]z|soluci[oó]n)\b|"
        r"\bcu[aá]nto\s+es\b|"
        r"\b(sin|cos|tan|log|sqrt|exp|diff|integrate)\s*\(|"
        r"\b(?:"
        r"deriv[aá]\w*|derivar|plante[aá]\w*|plantear|formul[aá]\w*|formular|"
        r"expres[aá]\w*|expresar|"
        r"derive|solve|prove|integrate|differentiate|evaluate|compute|"
        r"calculate|simplify|factorize|factorise|expand|state|"
        r"set\s+up|write\s+(?:out\s+|down\s+)?(?:the\s+)?"
        r")\b"
        r".{0,40}?"
        r"\b(?:"
        r"ecuaci[oó]n\w*|f[oó]rmula\w*(?!\s*(?:1|one|uno)\b)|identidad\w*|"
        r"expresi[oó]n\w*|derivada\w*|l[ií]mite\w*|teorema\w*|desigualdad\w*|"
        r"equations?|formulae\b|formulas?(?!\s*(?:1|one)\b)|identit(?:y|ies)|"
        r"expressions?|derivatives?|limits?|theorems?|inequalit(?:y|ies)"
        r")\b"
        r")",
        re.IGNORECASE,
    )

    _FACTUAL_ENUMERATION_PATTERN: Pattern[str] = re.compile(
        r"\b(?:"
        r"dame|dime|enum(?:era|erame)?|lista(?:me)?|menciona(?:me)?|nombra(?:me)?|"
        r"cu[aá]les\s+son|"
        r"tell\s+me|list(?:\s+the)?|give\s+me|name(?:\s+me)?|what\s+are|show\s+me"
        r")\b"
        r".{0,60}?"
        r"\b(?:"
        r"ecuaci[oó]n\w*|leyes?|f[oó]rmulas?|principios?|teoremas?|"
        r"equations?|laws?|formulae?|formulas?|principles?|theorems?|identities|"
        r"axioms?|constants?|inequalities"
        r")\b",
        re.IGNORECASE | re.DOTALL,
    )

    _FORMULA_DISCOVERY_VERB_RE: Pattern[str] = re.compile(
        r"\b(descubr\w*|invent\w*|encontr[aá]\w*|"
        # `encuentr\w*` ADEMÁS de `encontr[aá]\w*`: bug real, MEDIDO al
        # escribir la Sección 44 -- "encontrar" es un verbo con
        # diptongación irregular (o->ue) en varias formas del presente
        # ("encuentro/encuentras/encuentra/encuentren", no
        # "encontro/encontras/encontra") -- sin esta alternativa,
        # "encuentra una relación que..." (la conjugación MÁS común de
        # este verbo) nunca activaba la señal, solo formas como
        # "encontrá"/"encontrar" la activaban.
        r"encuentr\w*|"
        r"gener[aá]\w*|cre[aá]\w*|"
        # "find" agregado (bug real, MEDIDO escribiendo la Sección 49):
        # el equivalente en inglés de "encontrar"/"encuentra" -- posiblemente
        # el verbo más natural en inglés para pedir esto ("find a formula
        # that...") -- nunca estaba cubierto, solo "discover".
        r"discover\w*|invent\w*|generate\w*|create\w*|find\w*)\b",
        re.IGNORECASE,
    )
    _FORMULA_DISCOVERY_NOVELTY_RE: Pattern[str] = re.compile(
        r"\b(nueva?s?|nunca\s+(?:antes\s+)?\w+|in[eé]dit\w*|desconocid\w*|"
        r"jam[aá]s\s+\w+|"
        r"que\s+no\s+(?:exist\w*|se\s+(?:conozca|sepa|haya))|"
        r"que\s+nadie\s+(?:conozca|sepa|haya\s+\w+)|"
        # Bug real, MEDIDO (captura de la UI, 2026-09-05): "descubre una
        # formula que MEJORE tu rendimiento exponencialmente" no traía
        # ningún marcador explícito de novedad ("nueva"/"nunca"/etc.) y
        # el turno caía sin ninguna verificación al LLM general, que
        # alucinó una fórmula con apariencia de fórmula real (exp(...) con
        # variables inventadas y una constante 10^-9 sacada de la nada) --
        # el MISMO bug de fondo que motivó toda esta mejora, con
        # vocabulario de cómputo en vez de física. El SUBJUNTIVO español
        # después de "que" (mejorE, no mejorA) es la señal lingüística
        # correcta: "una fórmula que mejorE X" describe un referente
        # HIPOTÉTICO/no específico (se busca CUALQUIER fórmula con esa
        # propiedad -- todavía no se sabe cuál es), mientras que el
        # indicativo ("la fórmula que mejorA X") refiere a una fórmula
        # YA CONOCIDA y específica -- "encuentra la fórmula que mejorA el
        # rendimiento de tu PC" NO debe disparar este atajo (es un pedido
        # de recordar/aplicar algo conocido), y de hecho no lo hace: la
        # lista de abajo son formas de subjuntivo, no de indicativo.
        # "sirva"/"sirvan" (subjuntivo de "servir") agregado: bug real,
        # MEDIDO en captura de la UI (2026-09-06) -- "descubre una fórmula
        # que SIRVA para entender mejor el rendimiento" no traía ningún
        # verbo de la lista anterior, cayó sin verificación al LLM
        # general, que alucinó una fórmula sin verificar (R = W/η). Mismo
        # patrón de fondo que el bug de "mejorE" que motivó esta lista:
        # un verbo subjuntivo más de uso corriente en español que faltaba
        # cubrir.
        #
        # Ampliación (bug real, MEDIDO en captura de la UI, 2026-09-06,
        # el mismo día que el fix de "sirva" de arriba): "descubre una
        # fórmula que RELACIONE el rendimiento de cómputo con el ancho de
        # banda de memoria" tampoco traía ningún verbo de la lista y cayó
        # de nuevo al LLM general, que respondió con una "proyección
        # conceptual" sin verificar en vez de activar el motor. Esta
        # lista SIEMPRE va a ser una curada, nunca exhaustiva -- en vez
        # de seguir parchando de a un verbo por vez, se agrega de una
        # tanda el resto de los verbos de uso corriente para pedir este
        # tipo de relación (relacione/conecte/vincule/combine, y los de
        # explicación/modelado: describa/explique/modele/represente/
        # determine/cuantifique/prediga/estime/capture/refleje).
        r"que\s+(?:\w+\s+){0,2}(?:mejore|optimice|aumente|incremente|"
        r"multiplique|dispare|maximice|acelere|potencie|eleve|duplique|"
        r"triplique|resuelva|permita|logre|explote|sirva|sirvan|"
        r"relacione|conecte|vincule|combine|describa|explique|modele|"
        r"represente|determine|cuantifique|prediga|estime|capture|"
        r"refleje)\w*|"
        r"that\s+(?:\w+\s+){0,2}(?:improves|boosts|optimizes|increases|"
        r"doubles|triples|unlocks|maximizes|accelerates|solves|enables|"
        r"multiplies|relates|connects|links|combines|describes|explains|"
        r"models|represents|determines|predicts|estimates|captures|"
        r"reflects)\w*|"
        r"new|never\s+(?:before\s+)?\w+|unknown|undiscovered)\b",
        re.IGNORECASE,
    )
    _FORMULA_DISCOVERY_NOUN_RE: Pattern[str] = re.compile(
        r"\b(ecuaci[oó]n\w*|f[oó]rmula\w*|relaci[oó]n\w*|ley(?:es)?|"
        r"equations?|formulas?|formulae?|relations?|laws?)\b",
        re.IGNORECASE,
    )

    _QUANT_CLAIM_PATTERN: Pattern[str] = re.compile(
        r"("
        r"\b\d{1,3}(?:[\.,]\d+)?\s?%|"
        r"\b(aument[oó]|redujo|creci[oó]|disminuy[oó]|se\s+multiplic[oó])\b"
        r".{0,40}\b\d"
        r")",
        re.IGNORECASE,
    )

    _CODE_BOILERPLATE_PATTERN: Pattern[str] = re.compile(
        r"\b("
        r"hola\s+mundo|hello\s+world|plantilla\s+b[aá]sica|"
        r"ejemplo\s+b[aá]sico\s+de\s+c[oó]digo|boilerplate|"
        r"c[oó]digo\s+trivial"
        r")\b",
        re.IGNORECASE,
    )

    # Preguntas de seguimiento conversacional simple ("explain more about
    # that match", "cuéntame más sobre eso") - piden profundizar sobre
    # algo YA establecido en el turno anterior, no investigar un hecho
    # nuevo. Bug real que motivó esto: "explain more about that match..."
    # contiene "match", una de las _WEB_KEYWORDS_RE (sección deportes), y
    # por sí sola empujaba el score sobre SLOW_PATH_THRESHOLD - disparando
    # Tree-of-Thoughts (y potencialmente una búsqueda web) para una simple
    # continuación conversacional sin ninguna necesidad real de
    # razonamiento complejo ni de datos frescos.
    _CONVERSATIONAL_FOLLOWUP_RE: Pattern[str] = re.compile(
        r"\b("
        r"explica\w*\s+m[aá]s|cu[eé]ntame\s+m[aá]s|dime\s+m[aá]s|"
        r"profundiza\w*|ampl[ií]a\w*|desarrolla\s+m[aá]s|sigue\s+contando|"
        r"contin[uú]a\w*\s+(?:con|sobre|contando)|"
        r"m[aá]s\s+detalles?\s+(?:sobre|de|acerca\s+de)|"
        r"qu[eé]\s+m[aá]s\s+(?:sabes|hay|puedes\s+decirme)|"
        r"explain\s+more|tell\s+me\s+more|elaborate\s+on|expand\s+on|"
        r"go\s+deeper|more\s+details?\s+(?:about|on)|"
        r"what\s+else\s+(?:do\s+you\s+know|can\s+you\s+tell\s+me)|"
        r"can\s+you\s+clarify|clarif[ií]came\w*"
        r")\b",
        re.IGNORECASE,
    )

    # Verbos/sustantivos de desarrollo, incluyendo erratas fonéticas
    # comunes de teclado en español para nombres de lenguajes/librerías.
    # BLINDAJE (bug real, MEDIDO 2026-09-07: "...exprese la energía mínima
    # de procesamiento exclusivamente en función de la temperatura...").
    # "función" a secas matcheaba tanto la acepción de programación
    # ("escribí una función") como el uso matemático corriente "en función
    # de X" (as a function of X) -- carne y hueso de cualquier pedido de
    # física/matemática. Con CODE_COMPLEX activo, el turno se enrutaba al
    # modelo "coder" Y `Orchestrator._turn_wants_file_tools` lo trataba
    # como un pedido de archivo -- que, combinado con un verbo tan común
    # como "muestra"/"mostrá" matcheando `_FILE_READ_VERB_RE`, terminaba
    # en el "Blindaje de archivos" sintetizando un `list_dir('.')` en vez
    # de la derivación simbólica pedida (visto en vivo: la consola mostró
    # el listado de `src/core` donde debía ir la ecuación final).
    #
    # BLINDAJE #2 (bug real, MEDIDO 2026-09-11, video de cuellos de
    # botella): el lookbehind negativo de arriba solo excluía "en función
    # de X" -- "la FUNCIÓN DE ONDA" ("Deriva la ecuación de Schrödinger...
    # a partir de la función de onda... Muestra la derivación con
    # claridad") no empieza con "en ", así que igual matcheaba, igual
    # enrutaba al modelo coder, e igual terminó sintetizando el mismo
    # list_dir('.') espurio. "función"/"clase"/"método" sueltas son, en
    # los hechos, tan comunes en matemática/física/lógica ("la función de
    # onda", "por este método", "esa clase de problemas") como en
    # programación -- a diferencia de "python"/"pygame"/"javascript", que
    # sí son inequívocas por sí solas. Se las saca de esta lista (dejan de
    # ser, por sí solas, señal suficiente) y se mueven a
    # `_CODE_WEAK_NOUN_RE`, evaluada aparte en `_detect_code_complex` con
    # una condición extra: alcanzan solo si aparecen con sintaxis de
    # código real pegada (paréntesis: "función(", "método(") o si el
    # texto NO tiene ya una señal matemática fuerte (`_MATH_PATTERN`) --
    # un pedido real de código con verbo explícito ("implementa/escribí/
    # creá una función") sigue matcheando por ese verbo, sin pasar por
    # acá.
    _CODE_COMPLEX_PATTERN: Pattern[str] = re.compile(
        r"\b("
        r"implementa\w*|implement\w*|refactoriza\w*|refactor\w*|depura\w*|debug\w*|optimiz\w*|"
        r"mejor[aáo]\w*|improv\w*|enhanc\w*|"
        r"escribe\s+c[oó]digo|crea\s+c[oó]digo|desarrolla\w*|construye\s+c[oó]digo|"
        r"c[oó]digo|program\w*|script\w*|scrip\w*|algoritmo\w*|algoritm\w*|"
        r"python|phyton|pyton|piton|pytnon|phyt[oó]n|"
        r"javascript|javascrip\w*|jscript|j\s*script|"
        r"html|css|cpp|c\+\+|java|php|typescript|"
        r"pygame|pigaem|pygaim|pygme|pandas|numpy|tkinter|flask|django|"
        r"matplotlib|selenium|sqlite3?|opencv|kivy|fastapi|"
        r"telemetr[ií]a\w*|hardware|cpu|ram|procesos|"
        r"discord\.?py|"
        # BLINDAJE (bug real, MEDIDO 2026-09-09 — video demo "Can you
        # improve the snake game in there"): antes solo "flappy\w*" y la
        # frase exacta "game code" contaban como señal de desarrollo acá.
        # "the snake game"/"pong"/"tetris" no matcheaban NADA de este
        # patrón, así que CODE_COMPLEX daba negativo y no neutralizaba a
        # WEB_SEARCH_INTENT — que SÍ dispara con la palabra suelta "game"
        # (ver _WEB_KEYWORDS_RE, pensada para "who won the game"/marcadores
        # deportivos). Resultado real: una edición de código local
        # terminó disparando una búsqueda web completa sobre "snake"
        # (Wikipedia, Metal Gear Solid, papers universitarios) en vez de
        # leer/modificar el snake.py ya presente en el Workspace. Esta
        # misma lista de nombres de juego clásico ya la usa
        # Orchestrator._RUNNABLE_PROGRAM_NOUN_RE (orchestrator.py) — se
        # espeja acá para que router.py y Orchestrator coincidan en qué
        # cuenta como "obviamente código", en vez de mantener dos
        # vocabularios que pueden divergir.
        r"flappy\w*|game\s+code|estructura\s+de\s+datos|arquitectura\s+de\s+software|"
        r"snake|tetris|pong|pac-?man|breakout|arkanoid|2048|"
        # BLINDAJE (2026-09-18, patch_router1 -- bug real, MEDIDO: "create
        # a playable asteroids with a ship, shooting, and score in the
        # workspace" disparó una búsqueda web completa sobre "asteroids"
        # -- Wikipedia devolvió "List of Google Easter eggs"/"Space
        # Invaders", ninguna fuente relacionada -- en vez de neutralizar
        # WEB_SEARCH_INTENT como debería cualquier pedido de creación de
        # juego, exactamente el mismo patrón de bug que el BLINDAJE de
        # arriba (snake/pong/tetris, 2026-09-09). Causa: esta lista es un
        # ESPEJO manual de `Orchestrator._RUNNABLE_PROGRAM_NOUN_RE`/
        # `_WRITE_ARTIFACT_NOUN` (orchestrator.py) -- el comentario de
        # arriba ya lo advierte ("dos vocabularios que pueden divergir")
        # -- pero `patch_orchestrator91` (mismo día, sesión anterior)
        # sumó doom/agario/mario/minecraft/buscaminas/ajedrez/asteroids/
        # invaders SOLO en orchestrator.py y se olvidó de espejarlo acá.
        # Se copia la misma lista literal, en el mismo orden, para que
        # las dos dejen de divergir otra vez.
        r"doom|agario|agar\.?io|mario|minecraft|buscaminas|minesweeper|"
        r"ajedrez|chess|asteroids?|asteroides|invaders?|invasores"
        r")\b",
        re.IGNORECASE,
    )

    _CODE_EXTENSION_RE: Pattern[str] = re.compile(
        r"\.(py|html|htm|js|jsx|ts|tsx|css|json|cpp|cc|h|hpp|java|"
        r"php|rb|go|rs|cs|sql|sh|bat|ipynb)\b",
        re.IGNORECASE,
    )

    # BLINDAJE #2 (ver el comentario largo junto a `_CODE_COMPLEX_PATTERN`,
    # 2026-09-11): "función"/"clase"/"método" sueltas, evaluadas aparte
    # porque necesitan la condición extra de `_detect_code_complex` (no
    # alcanzan solas salvo sintaxis de código real o ausencia de contexto
    # matemático) — a diferencia del resto de `_CODE_COMPLEX_PATTERN`, que
    # sigue siendo "una sola señal ya alcanza".
    _CODE_WEAK_NOUN_RE: Pattern[str] = re.compile(
        r"\b(funci[oó]n\w*|clase\w*|m[eé]todo\w*)\b", re.IGNORECASE,
    )
    _CODE_WEAK_NOUN_CALL_RE: Pattern[str] = re.compile(
        r"\b(?:funci[oó]n|m[eé]todo|function|method)\w*\s*\(|"
        r"\bdef\s+\w+\s*\(",
        re.IGNORECASE,
    )
    # BLINDAJE #3 (auditoría de seguridad/estructura, 2026-09-16): "rust" y
    # "go" vivían en `_CODE_COMPLEX_PATTERN` de arriba como señal "alcanza
    # sola", igual que "python"/"javascript"/"typescript" -- pero a
    # diferencia de esos, "go" y "rust" son palabras inglesas de uso
    # cotidiano sin ninguna relación con programación ("let's go", "I need
    # to go", "the metal will rust", literalmente cualquier oración en
    # inglés con el verbo "go"). Con esas dos sueltas en la lista, un turno
    # en inglés sin ninguna intención de código terminaba clasificado
    # CODE_COMPLEX solo por contener la palabra "go". Mismo criterio que
    # `_CODE_WEAK_NOUN_RE`/BLINDAJE #2: se sacan de la lista "alcanza sola"
    # y pasan a necesitar una palabra de contexto de programación real al
    # lado (lenguaje/language, programar/program, código/code, script,
    # compilar/compile, o vocabulario específico del ecosistema de cada
    # lenguaje: golang/goroutine para Go, cargo/rustc/crate para Rust, o la
    # extensión de archivo .go/.rs -- esta última ya la cubre
    # `_CODE_EXTENSION_RE` aparte, se repite acá porque suele aparecer en
    # la misma frase que el nombre del lenguaje: "actualizá el main.go").
    # "Necesito un script en Go"/"ayudame a programar en Rust" siguen
    # matcheando bien (ahí también matchea "script"/"programar" del resto
    # de `_CODE_COMPLEX_PATTERN`, así que en la práctica casi nunca dependen
    # solo de esto); lo que se pierde es el caso raro de "escribime algo en
    # Go" sin ninguna otra palabra de programación en la misma frase -- ese
    # trade-off es preferible a que cualquier "let's go" dispare
    # CODE_COMPLEX.
    _CODE_WEAK_LANG_RE: Pattern[str] = re.compile(
        r"\b(rust|go)\b", re.IGNORECASE,
    )
    _CODE_LANG_CONTEXT_RE: Pattern[str] = re.compile(
        r"\b(lenguaje\w*|language\w*|programa\w*|program\w*|c[oó]digo|code\w*|"
        r"script\w*|compil\w*|golang|goroutine\w*|cargo|rustc|crate\w*)\b|"
        r"\.go\b|\.rs\b",
        re.IGNORECASE,
    )
    # BLINDAJE #2b (MEDIDO 2026-09-11, mismo video de cuellos de botella):
    # `_MATH_PATTERN` exige un verbo conjugado en formas específicas
    # ("expresa", "resuelve") y no cubre "exprese"/"resolver"/otras
    # conjugaciones, ni frases sin verbo como "función de onda". Este
    # patrón cubre modismos matemáticos/físicos habituales que traen
    # "función"/"método"/"clase" sin ser código, independientemente de la
    # conjugación del verbo (o de si hay verbo):
    #   - "función de onda" (mecánica cuántica)
    #   - "en función de X" / "en función del Y" ("as a function of X")
    #   - menciones de ecuación/fórmula/derivada/integral/límite/teorema
    #     o de física/química/matemática en general
    _MATH_IDIOM_RE: Pattern[str] = re.compile(
        r"\b(?:"
        r"funci[oó]n\s+de\s+onda|"
        r"en\s+funci[oó]n\s+(?:de|del)\b|"
        r"ecuaci[oó]n\w*|f[oó]rmula\w*|derivada\w*|integral\w*|"
        r"l[ií]mite\w*|teorema\w*|"
        r"f[ií]sica\w*|qu[ií]mica\w*|matem[aá]tic\w*"
        r")\b",
        re.IGNORECASE,
    )

    _CODE_LIBRARY_RE: Pattern[str] = re.compile(
        r"\b("
        r"pygame|pigaem|pygaim|pandas|numpy|tkinter|flask|django|matplotlib|"
        r"seaborn|selenium|sqlite3?|opencv|kivy|fastapi|pytest|requests|"
        r"discord\.?py|scikit-learn|sklearn|tensorflow|pytorch|keras"
        r")\b",
        re.IGNORECASE,
    )

    _LOGIC_AUDIT_PATTERN: Pattern[str] = re.compile(
        r"\b("
        r"demuestra\w*|audita\w*|verifica\w*\s+(?:la|si)|"
        r"es\s+l[oó]gicamente|encuentra\s+la\s+falacia|"
        r"analiza\s+la\s+coherencia|"
        r"detecta\w*\s+(?:la\s+)?contradicci[oó]n\w*|"
        r"prueba\s+formal|refuta\w*|es\s+consistente\s+con"
        r")\b",
        re.IGNORECASE,
    )

    _CONCEPTUAL_PATTERN: Pattern[str] = re.compile(
        r"\b("
        r"teor[ií]a\w*|axioma\w*|paradigma\w*|epistemolog[ií]a\w*|"
        r"ontolog[ií]a\w*|ontol[oó]gic\w*|metaf[ií]sic\w*|"
        r"filosof[ií]a\w*|transdisciplinari\w*|marco\s+conceptual|"
        r"hip[oó]tesis\s+(?:original|emergente)|premisa\w*|"
        r"propuesta\s+te[oó]rica|descubrimiento\s+nuevo|hallazgo"
        r")\b",
        re.IGNORECASE,
    )

    _LSC_CONDITIONAL_PATTERN: Pattern[str] = re.compile(
        r"\b("
        r"si\b.{3,150}\bentonces\b|"
        r"dado\s+que\b.{3,150}|"
        r"siempre\s+que\b.{3,150}|"
        r"cuando\b.{3,150}\bpor\s+lo\s+tanto\b"
        r")",
        re.IGNORECASE | re.DOTALL,
    )

    _LSC_CONNECTOR_PATTERN: Pattern[str] = re.compile(
        r"\b("
        r"por\s+lo\s+tanto|en\s+consecuencia|por\s+consiguiente|"
        r"de\s+ello\s+se\s+sigue|therefore|thus|hence|it\s+follows\s+that"
        r")\b",
        re.IGNORECASE,
    )

    _LSC_SYSTEMIC_VOCAB_PATTERN: Pattern[str] = re.compile(
        r"\b("
        r"sistema\w*|arquitectura\w*|retroalimentaci[oó]n\w*|"
        r"recursiv\w*|inferencia\w*|axioma\w*|coherencia\w*|"
        r"estructura\w*|invariante\w*|acoplamiento\w*"
        r")\b",
        re.IGNORECASE,
    )

    # BLINDAJE (bug real, MEDIDO 2026-09-07: "dime cual fue el ultimo gran
    # hito de la humanidad" disparó una búsqueda web real que devolvió un
    # artículo de entretenimiento sobre un récord de taquilla — "hito" ahí
    # se usaba en sentido figurado ("récord/hito de taquilla"), y la
    # respuesta final terminó citando la biopic de Michael Jackson en vez
    # de la pregunta filosófica/histórica que realmente se hizo). El
    # patrón "último/mayor" + sustantivo de LOGRO ABSTRACTO ("hito",
    # "logro", "avance"...) + "de la humanidad/historia" es un
    # SUPERLATIVO DE IMPORTANCIA ("el más grande de todos, hasta ahora"),
    # no un pedido de ACTUALIDAD ("lo más reciente que pasó") — pese a
    # compartir la palabra "último" con las Marcadores Temporales de
    # `_WEB_KEYWORDS_RE` de arriba. No se toca esa regex ni
    # `is_live_event_query`/`needs_strict_relevance` (relevance.py):
    # "el último terremoto en Japón" sigue disparando búsqueda
    # correctamente, porque ahí "último" sí es temporal. Esta es una
    # exclusión puntual sobre la clase semántica "mayor logro histórico",
    # mismo patrón de exclusión mutua que CODE_COMPLEX/CONVERSATIONAL_
    # FOLLOWUP ya usan más abajo contra WEB_SEARCH_INTENT.
    _SUPERLATIVE_ACHIEVEMENT_RE: Pattern[str] = re.compile(
        r"\b(?:el\s+m[aá]s\s+grande|el\s+mayor|m[aá]s\s+grande|mayor|[uú]ltim[oa])\s+"
        r"(?:gran\s+)?"
        r"(?:hito|logro|avance|descubrimiento|invento|aporte|legado|acontecimiento)\w*\s+"
        r"(?:de\s+la\s+humanidad|de\s+la\s+historia|hist[oó]ric[oa])\b|"
        r"\b(?:greatest|biggest)\s+(?:milestone|achievement|invention|discovery|"
        r"advance|contribution|legacy)\s+(?:of|in)\s+"
        r"(?:humanity|human\s+history|history|mankind)\b",
        re.IGNORECASE,
    )

    # BLINDAJE (2026-09-19, patch_router2 -- bug real, MEDIDO en vivo por
    # el usuario, video/logs completos: turno "add random cool things to
    # the game" (seguimiento directo sobre `agar_io.py`, ya escrito y
    # editado en turnos PREVIOS de la MISMA sesión) disparó una búsqueda
    # web completa -- DuckDuckGo, Wikipedia, e imágenes de tema TOTALMENTE
    # ajeno (una hamburguesa, "search engine optimization") -- en vez de
    # ir directo a `read_file`/`edit_file` sobre el archivo ya existente.
    # Causa raíz: "game" es una de las `_WEB_KEYWORDS_RE` (pensada para
    # "who won the game"/marcadores deportivos, categoría 5 de arriba), y
    # NINGUNO de los tres ejes de neutralización que ya existían cubría
    # este caso: `_CODE_COMPLEX_PATTERN` exige o un verbo de desarrollo
    # explícito (implementa/mejora/optimiza/...) o el NOMBRE puntual del
    # juego (snake/tetris/agario/...) -- "add ... to the game", sin
    # nombrar el juego ni usar ninguno de esos verbos, no matcheaba nada
    # de esa lista; CONVERSATIONAL_FOLLOWUP y SUPERLATIVE_ACHIEVEMENT
    # tampoco aplican, no son pedidos de "contame más" ni de "el mayor
    # logro de la historia". El usuario, tras revisar el log completo en
    # vivo, pidió explícitamente blindar esto: "tienes que blindar
    # fuertemente ese aspecto porque el usuario se enojara cuando pase
    # eso".
    #
    # Fix: cuarto eje de neutralización, mismo criterio ESTRUCTURAL que
    # los tres anteriores (CODE_COMPLEX / CONVERSATIONAL_FOLLOWUP /
    # SUPERLATIVE_ACHIEVEMENT ya usan esto, ver la nota de arriba y
    # `classify()` más abajo) -- un verbo de modificación genérico
    # (add/put/include/insert/throw in, o sus equivalentes en español:
    # agrega/añade/incluye/suma/inserta/pon, con sus formas con
    # pronombre enclítico: agregale/ponle/sumale/metele) seguido, a poca
    # distancia, de un objeto que es inequívocamente código/juego YA
    # EXISTENTE ("to/in(to) the game/code/script", "al/en el juego/
    # código/script") es un pedido de EDICIÓN sobre algo que ya está en
    # el workspace -- nunca una pregunta sobre resultados deportivos o
    # actualidad, sin importar que contenga la palabra suelta "game". A
    # diferencia de CODE_COMPLEX, este patrón NO requiere nombrar el
    # juego puntual ni un verbo de programación explícito -- por diseño,
    # para cubrir justamente el caso genérico ("cosas geniales", "random
    # stuff", "algo divertido") que una lista cerrada de nombres de
    # juego/verbos de programación nunca va a poder enumerar por
    # completo. No se toca `_CODE_COMPLEX_PATTERN` ni las listas de
    # nombres de juego que ya mantiene (y que router.py/orchestrator.py
    # ya espejan entre sí, ver patch_router1) -- esta es una señal
    # INDEPENDIENTE, evaluada en paralelo, igual que las otras tres.
    _GENERIC_EDIT_CONTINUATION_RE: Pattern[str] = re.compile(
        r"\b(?:add\w*|throw\s+in|include\w*|insert\w*|put\w*|"
        r"agrega\w*|a[ñn]ad\w*|incluye\w*|suma\w*|inserta\w*|"
        r"met[eé]\w*|pon\w*)\b"
        r".{0,40}?"
        r"\b(?:to|in|into|al?|en)\b\s*(?:the\s+|el\s+|la\s+)?"
        r"(?:game|code|script|juego|c[oó]digo)\b",
        re.IGNORECASE,
    )

    def check_web_intent(self, text: str) -> bool:
        """Detecta consultas probablemente dependientes de actualidad."""
        return bool(self._WEB_KEYWORDS_RE.search(text))

    def _detect_superlative_achievement(self, text: str) -> bool:
        """True si el texto pregunta por el mayor logro/hito histórico en
        sentido de IMPORTANCIA, no de actualidad — ver BLINDAJE arriba."""
        return bool(self._SUPERLATIVE_ACHIEVEMENT_RE.search(text))

    def _detect_generic_edit_continuation(self, text: str) -> bool:
        """True si el texto es un pedido genérico de agregar/incluir algo
        a "el juego"/"el código"/"el script" ya existente -- ver el
        BLINDAJE de `_GENERIC_EDIT_CONTINUATION_RE` arriba."""
        return bool(self._GENERIC_EDIT_CONTINUATION_RE.search(text))

    def _detect_code_complex(self, stripped: str, lowered: str) -> bool:
        """
        Detección unificada de intención de desarrollo de software.

        Combina tres señales independientes: verbos/sustantivos de
        programación, extensiones de archivo explícitas y nombres de
        librerías/frameworks (incluyendo variantes con errata). Basta
        una sola señal positiva para considerar CODE_COMPLEX activo,
        ya que cualquiera de ellas es, por sí sola, inequívoca.
        """
        if self._CODE_COMPLEX_PATTERN.search(lowered):
            return True
        if self._CODE_EXTENSION_RE.search(stripped):
            return True
        if self._CODE_LIBRARY_RE.search(lowered):
            return True
        # BLINDAJE #2 (ver `_CODE_WEAK_NOUN_RE`): "función"/"clase"/
        # "método" sueltas ya no son señal suficiente por sí solas — hace
        # falta sintaxis de código real pegada (paréntesis/`def`), o que
        # el texto no tenga ya una señal matemática fuerte (`_MATH_PATTERN`,
        # p. ej. "deriva", "resuelve", "calculá... el valor de") ni un
        # modismo matemático/físico sin verbo conjugado (`_MATH_IDIOM_RE`,
        # p. ej. "función de onda", "en función de la temperatura",
        # "ecuación diferencial") — ver BLINDAJE #2b.
        # La sintaxis de llamada/definición real ("función(", "def foo(")
        # es inequívoca por sí sola, aparezca o no la palabra suelta
        # "función"/"clase"/"método" en el resto del texto (p. ej. un
        # snippet "def calcular_area(radio):" sin la palabra "función").
        if self._CODE_WEAK_NOUN_CALL_RE.search(lowered):
            return True
        if self._CODE_WEAK_NOUN_RE.search(lowered):
            if not (
                self._MATH_PATTERN.search(lowered)
                or self._MATH_IDIOM_RE.search(lowered)
            ):
                return True
        # BLINDAJE #3 (ver `_CODE_WEAK_LANG_RE`/`_CODE_LANG_CONTEXT_RE`
        # más arriba): "rust"/"go" sueltas ya no alcanzan solas -- hace
        # falta una palabra de contexto de programación real en el mismo
        # texto (a diferencia de "función"/"clase"/"método", acá no hay
        # una señal de "esto es matemática" que descartar -- la ambigüedad
        # es contra el inglés cotidiano, no contra otro campo técnico).
        if self._CODE_WEAK_LANG_RE.search(lowered):
            if self._CODE_LANG_CONTEXT_RE.search(lowered):
                return True
        return False

    def _evaluate_lsc_density(self, stripped: str, lowered: str) -> int:
        hits = 0

        if self._LSC_CONDITIONAL_PATTERN.search(stripped):
            hits += 1

        if self._LSC_CONNECTOR_PATTERN.search(lowered):
            hits += 1

        systemic_matches = self._LSC_SYSTEMIC_VOCAB_PATTERN.findall(lowered)
        if len(systemic_matches) >= 2:
            hits += 1

        return hits

    def classify(self, text: str) -> RoutingDecision:
        """
        Clasifica una consulta y devuelve una decisión inmutable.

        Orden de evaluación crítico para el blindaje v3.9:
            1. Se determina primero, y de forma aislada, si existe
               intención de desarrollo de software (CODE_COMPLEX).
            2. Solo si CODE_COMPLEX es negativo se evalúa
               WEB_SEARCH_INTENT. Esto implementa la exclusión mutua
               a nivel de matriz de decisión: ninguna petición de
               código puede ser interceptada por el motor de búsqueda
               web, sin importar cuántas palabras de actualidad
               contenga (p. ej. "hazme un script que muestre el
               resultado actual de la liga").
        """
        start = time.perf_counter()

        if text is None or not text.strip():
            elapsed = (time.perf_counter() - start) * 1000
            return RoutingDecision(
                path=RoutePath.FAST_PATH,
                tags=(SignalTag.EMPTY_INPUT,),
                score=self.WEIGHT_TRIVIAL_PULL,
                reason="Entrada vacía o compuesta únicamente por espacios.",
                elapsed_ms=elapsed,
                text_length=0,
            )

        stripped = text.strip()
        lowered = stripped.lower()
        length = len(stripped)

        tags: List[SignalTag] = []
        score = 0.0

        if length <= self.SHORT_QUERY_CHARS:
            if self._TRIVIAL_GREETING_RE.search(lowered):
                tags.append(SignalTag.TRIVIAL_GREETING)
                score += self.WEIGHT_TRIVIAL_PULL
            else:
                tags.append(SignalTag.SHORT_QUERY)

        math_detected = bool(self._MATH_PATTERN.search(lowered))
        if math_detected:
            tags.append(SignalTag.MATH_EXPRESSION)
            score += self.WEIGHT_MATH

        if self._FACTUAL_ENUMERATION_PATTERN.search(lowered):
            tags.append(SignalTag.FACTUAL_ENUMERATION)
            score += self.WEIGHT_FACTUAL_ENUMERATION

        if (
            self._FORMULA_DISCOVERY_VERB_RE.search(lowered)
            and self._FORMULA_DISCOVERY_NOVELTY_RE.search(lowered)
            and self._FORMULA_DISCOVERY_NOUN_RE.search(lowered)
        ):
            tags.append(SignalTag.FORMULA_DISCOVERY)
            score += self.WEIGHT_FORMULA_DISCOVERY

        if self._QUANT_CLAIM_PATTERN.search(lowered):
            tags.append(SignalTag.QUANT_CLAIM)
            score += self.WEIGHT_QUANT_CLAIM

        code_complex_detected = False

        if self._CODE_BOILERPLATE_PATTERN.search(lowered):
            tags.append(SignalTag.CODE_BOILERPLATE)
            score += self.WEIGHT_BOILERPLATE_PULL
        elif self._detect_code_complex(stripped, lowered):
            tags.append(SignalTag.CODE_COMPLEX)
            score += self.WEIGHT_CODE_COMPLEX
            code_complex_detected = True

        logic_audit_detected = bool(self._LOGIC_AUDIT_PATTERN.search(lowered))
        if logic_audit_detected:
            tags.append(SignalTag.LOGIC_AUDIT)
            score += self.WEIGHT_LOGIC_AUDIT

        conceptual_dense_detected = bool(self._CONCEPTUAL_PATTERN.search(lowered))
        if conceptual_dense_detected:
            tags.append(SignalTag.CONCEPTUAL_DENSE)
            score += self.WEIGHT_CONCEPTUAL

        if self._EXISTENTIAL_RE.search(stripped):
            tags.append(SignalTag.EXISTENTIAL_SELF)
            score += self.WEIGHT_EXISTENTIAL

        lsc_hits = self._evaluate_lsc_density(stripped, lowered)

        complex_reasoning_present = math_detected or logic_audit_detected or code_complex_detected or lsc_hits >= 2
        conversational_followup_detected = (
            not complex_reasoning_present
            and bool(self._CONVERSATIONAL_FOLLOWUP_RE.search(lowered))
        )
        if conversational_followup_detected:
            tags.append(SignalTag.CONVERSATIONAL_FOLLOWUP)
            score += self.WEIGHT_FOLLOWUP_PULL

        # BLINDAJE (ver _SUPERLATIVE_ACHIEVEMENT_RE / caso "último gran
        # hito de la humanidad"): tercer eje de neutralización de
        # WEB_SEARCH_INTENT, junto a CODE_COMPLEX y CONVERSATIONAL_
        # FOLLOWUP — una pregunta por el mayor logro/hito histórico usa
        # "último" en sentido de IMPORTANCIA, no de actualidad.
        superlative_achievement_detected = self._detect_superlative_achievement(lowered)

        # BLINDAJE #4 (bug real, MEDIDO 2026-09-09 — video "Derive the
        # time-dependent Schrödinger equation..."): `_WEB_KEYWORDS_RE`
        # tiene varias palabras sueltas MUY comunes en redacción técnica/
        # académica que solo tienen sentido de actualidad en otro
        # contexto — "show" (pensada para "show" de TV/streaming, ver
        # categoría 7) y "final" (pensada para "la final" de un torneo,
        # ver categoría 5) matchearon en "show the derivation" / "the
        # final Hamiltonian form" de un pedido de física/matemática pura,
        # sin ninguna relación con actualidad. Ninguno de los tres ejes
        # de arriba (CODE_COMPLEX, CONVERSATIONAL_FOLLOWUP,
        # SUPERLATIVE_ACHIEVEMENT) cubre este caso — un turno de
        # matemática/razonamiento conceptual denso (`math_detected` /
        # `conceptual_dense_detected`, ya calculados arriba para
        # MATH_EXPRESSION/CONCEPTUAL_DENSE) tampoco necesita búsqueda
        # web: derivar una ecuación o explicar un concepto no depende de
        # información en tiempo real, sin importar qué palabra suelta
        # aparezca en el enunciado.
        #
        # HARDENING #4 (real bug, MEASURED 2026-09-09 — "Derive the
        # time-dependent Schrödinger equation..." video): `_WEB_KEYWORDS_
        # RE` has several very common technical/academic words that only
        # carry a currency/actuality meaning in a DIFFERENT context —
        # "show" (meant for a TV/streaming show, category 7) and "final"
        # (meant for a tournament final, category 5) matched on "show
        # the derivation" / "the final Hamiltonian form" from a pure
        # physics/math request, with zero relation to current events.
        # None of the three axes above cover this — a math / dense
        # conceptual-reasoning turn (`math_detected` /
        # `conceptual_dense_detected`, already computed above for
        # MATH_EXPRESSION/CONCEPTUAL_DENSE) never needs web search
        # either: deriving an equation or explaining a concept doesn't
        # depend on real-time information, whatever stray keyword shows
        # up in the wording.
        stem_reasoning_detected = math_detected or conceptual_dense_detected

        # BLINDAJE (patch_router2, ver `_GENERIC_EDIT_CONTINUATION_RE`
        # arriba): cuarto eje de neutralización, mismo criterio que los
        # tres anteriores -- "add random cool things to the game" no
        # nombra el juego ni usa un verbo de `_CODE_COMPLEX_PATTERN`,
        # pero sí es, inequívocamente, un pedido de edición sobre algo
        # ya existente en el workspace.
        generic_edit_continuation_detected = self._detect_generic_edit_continuation(stripped)

        if (
            not code_complex_detected
            and not conversational_followup_detected
            and not superlative_achievement_detected
            and not stem_reasoning_detected
            and not generic_edit_continuation_detected
            and self.check_web_intent(stripped)
        ):
            tags.append(SignalTag.WEB_SEARCH_INTENT)
            score += self.WEIGHT_WEB_SEARCH

        if lsc_hits >= 2:
            tags.append(SignalTag.LSC_INFERENCE)
            score += self.WEIGHT_LSC

        if length >= self.LONG_TEXT_HIGH_CHARS:
            tags.append(SignalTag.LONG_TEXT)
            score += self.WEIGHT_LONG_TEXT_HIGH
        elif length >= self.LONG_TEXT_MEDIUM_CHARS:
            tags.append(SignalTag.LONG_TEXT)
            score += self.WEIGHT_LONG_TEXT_MEDIUM

        path = (
            RoutePath.SLOW_PATH
            if score >= self.SLOW_PATH_THRESHOLD
            else RoutePath.FAST_PATH
        )

        elapsed = (time.perf_counter() - start) * 1000
        tag_summary = ", ".join(tag.value for tag in tags) or "(sin señales activas)"

        if code_complex_detected and self.check_web_intent(stripped):
            exclusion_note = " [WEB_SEARCH_INTENT neutralizado por exclusión mutua con CODE_COMPLEX]"
        elif conversational_followup_detected and self.check_web_intent(stripped):
            exclusion_note = " [WEB_SEARCH_INTENT neutralizado por seguimiento conversacional simple]"
        elif superlative_achievement_detected and self.check_web_intent(stripped):
            exclusion_note = " [WEB_SEARCH_INTENT neutralizado por superlativo de importancia histórica, no actualidad]"
        elif stem_reasoning_detected and self.check_web_intent(stripped):
            exclusion_note = " [WEB_SEARCH_INTENT neutralizado por matemática/razonamiento conceptual denso]"
        elif generic_edit_continuation_detected and self.check_web_intent(stripped):
            exclusion_note = " [WEB_SEARCH_INTENT neutralizado por pedido genérico de edición sobre juego/código ya existente]"
        else:
            exclusion_note = ""

        return RoutingDecision(
            path=path,
            tags=tuple(tags),
            score=round(score, 2),
            reason=(
                f"Ruta asignada {path.value}; score={score:+.2f}; "
                f"umbral={self.SLOW_PATH_THRESHOLD}; señales={tag_summary}."
                f"{exclusion_note}"
            ),
            elapsed_ms=elapsed,
            text_length=length,
        )


class RouterState(str, Enum):
    IDLE = "idle"
    BUFFERING = "buffering"
    FLUSHED = "flushed"


class OptimizedRouter:
    """
    Fachada thread-safe de procesamiento por lotes sobre IntentRouter.

    submit() añade una consulta al lote y devuelve None mientras el lote
    no esté completo. Cuando alcanza batch_size, devuelve las decisiones
    de todo el lote y restaura el estado IDLE.

    flush() procesa manualmente las consultas pendientes, si existen.
    classify() permite clasificación inmediata sin afectar el búfer.
    """

    DEFAULT_BATCH_SIZE: int = 3

    _COMPRESSIBLE_FILLERS: frozenset[str] = frozenset({
        "por favor",
        "please",
        "eh",
        "bueno",
        "o sea",
        "digamos",
        "en fin",
        "well",
        "you know",
        "es decir",
    })

    _MULTI_SPACE_RE: Pattern[str] = re.compile(r"\s{2,}")

    def __init__(
        self,
        batch_size: int = DEFAULT_BATCH_SIZE,
        router: Optional[IntentRouter] = None,
    ) -> None:
        self._router = router or IntentRouter()
        self._batch_size = max(1, batch_size)
        self._buffer: List[str] = []
        self._state = RouterState.IDLE
        self._lock = threading.Lock()

        fillers = sorted(self._COMPRESSIBLE_FILLERS, key=len, reverse=True)
        alternation = "|".join(re.escape(filler) for filler in fillers)
        self._filler_re: Pattern[str] = re.compile(
            rf"\b(?:{alternation})\b",
            re.IGNORECASE,
        )

    def process_query(self, text: str) -> RoutingDecision:
        """Alias para mantener compatibilidad con test_lsc.py"""
        return self.classify(text)

    @property
    def state(self) -> RouterState:
        with self._lock:
            return self._state

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def buffered_count(self) -> int:
        with self._lock:
            return len(self._buffer)

    def compress_query(self, text: str) -> str:
        if not text:
            return ""

        original = text.strip()
        compressed = self._filler_re.sub("", original)
        compressed = self._MULTI_SPACE_RE.sub(" ", compressed).strip()

        return compressed or original

    def submit(self, text: str) -> Optional[List[RoutingDecision]]:
        compressed = self.compress_query(text)

        with self._lock:
            self._state = RouterState.BUFFERING
            self._buffer.append(compressed)

            if len(self._buffer) < self._batch_size:
                return None

            batch = list(self._buffer)
            self._buffer.clear()
            self._state = RouterState.FLUSHED

        try:
            return [self._router.classify(item) for item in batch]
        finally:
            with self._lock:
                self._state = RouterState.IDLE

    def flush(self) -> List[RoutingDecision]:
        with self._lock:
            if not self._buffer:
                self._state = RouterState.IDLE
                return []

            batch = list(self._buffer)
            self._buffer.clear()
            self._state = RouterState.FLUSHED

        try:
            return [self._router.classify(item) for item in batch]
        finally:
            with self._lock:
                self._state = RouterState.IDLE

    def classify(self, text: str) -> RoutingDecision:
        return self._router.classify(self.compress_query(text))