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
    # Deliberadamente 0.0 - ver la nota junto a
    # _FACTUAL_ENUMERATION_PATTERN más abajo: esta señal es puramente
    # informativa para el orquestador, nunca debe empujar a slow_path.
    WEIGHT_FACTUAL_ENUMERATION: float = 0.0
    WEIGHT_CODE_COMPLEX: float = 3.0
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
        # 1. Verbos de Búsqueda / Search Verbs
        r"busca|b[uú]scam?e?|investiga\w*|averigua\w*|googlea\w*|consulta\w*|rastrea\w*|encuentra\w*|chequea\w*|inf[oó]rmam?e?|"
        r"search|find|google|lookup|check|investigate|track|inform|"
        # 2. Marcadores Temporales / Time Markers
        r"hoy|ayer|ma[nñ]ana|reciente\w*|actual\w*|últim[oa]s?|ultim[oa]s?|[uú]ltima\s+hora|ahora|en\s+vivo|live|al\s+momento|en\s+tiempo\s+real|"
        r"today|yesterday|tomorrow|recent|latest|current|breaking|now|real\s*time|"
        r"este\s+a[nñ]o|esta\s+semana|este\s+mes|2024|2025|2026|2027|"
        r"this\s+year|this\s+week|this\s+month|"
        # 3. Sucesos y Catástrofes / Events & Natural Disasters
        r"noticia\w*|suceso\w*|acontecimiento\w*|pas[oó]|sucedi[oó]|ocurri[oó]|reporte\w*|declaraci[oó]n\w*|comunicado\w*|"
        r"news|event|happened|occurred|report|statement|announcement|"
        r"terremoto\w*|sismo\w*|temblor\w*|tsunami\w*|hurac[aá]n\w*|tormenta\w*|tragedia\w*|accidente\w*|erupci[oó]n\w*|incendio\w*|inundaci[oó]n\w*|cat[aá]strofe\w*|"
        r"earthquake\w*|quake\w*|tremor\w*|tsunami\w*|hurricane\w*|storm\w*|tragedy|accident|eruption|fire|flood\w*|disaster\w*|"
        # 4. Interrogativos / Interrogatives
        r"qui[eé]n\w*|qu[eé]\s+(?:pas[oó]|sucedi[oó]|ocurri[oó]|pasaba|dijo|fue)|cu[aá]ndo\w*|a\s+qu[eé]\s+hora|d[oó]nde\s+(?:es|fue|ocurri[oó]|queda)|"
        r"cu[aá]nto\w*|cuanto\w*|cu[aá]l\s+(?:es|fue|ser[aá])|qu[eé]\s+se\s+sabe|qu[eé]\s+hay\s+de|"
        r"who|what\s+(?:happened|occurred|is|was)|when|where|how\s+much|how\s+many|which|"
        # 5. Deportes / Sports
        r"mundial\w*|final\w*|semifinal\w*|cuartos|partido\w*|juegos\s+ol[ií]mpicos|olimpiadas|torneo\w*|copa\w*|champions|liga\w*|eliminatorias|"
        r"marcador\w*|resultado\w*|goles|goleador\w*|alineaci[oó]n\w*|clasificaci[oó]n\w*|posiciones|f[uú]tbol|baloncesto|nba|f1|gran\s+premio|carrera|super\s+bowl|"
        r"campe[oó]n\w*|ganador\w*|derrota\w*|victoria\w*|empate\w*|vs|contra|"
        r"world\s+cup|match|game|score|standings|winner|champion|loss|victory|tie|draw|soccer|football|"
        # 6. Economía y Finanzas / Economy & Markets
        r"precio\w*|cotizaci[oó]n\w*|d[oó]lar|euro|bitcoin|btc|crypto|cripto|bolsa|acciones|inflaci[oó]n\w*|costo\w*|tarifa\w*|"
        r"cu[aá]nto\s+(?:cuesta|vale|cotiza|est[aá])|"
        r"price|rate|dollar|euro|stock|shares|inflation|cost|fee|how\s+much\s+is|"
        # 7. Clima, Entretenimiento y Software / Weather & Software
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

    # Saludos, despedidas y acuses de cortesía en ES + EN. Dos ramas:
    #   1. Saludo/despedida ancorado en `^` con `\b` - permite texto
    #      después ("hola, cómo estás", "hi there").
    #   2. Acuse breve ("ok", "thanks", "de nada", "cool") - debe ser
    #      casi todo el mensaje (`[\s.!¡¿?]*$`), para que "ok what is 2+2"
    #      NO caiga en el carril trivial.
    # Solo se evalúa cuando `length <= SHORT_QUERY_CHARS` (20), así que el
    # riesgo de un falso positivo largo ya está acotado por longitud.
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

    # Nota (medido - turno real del usuario: "dame
    # ecuaciones matematicas", 27 caracteres, cero contenido matemático
    # real: sin números, sin operadores, sin pedir resolver/calcular
    # nada - score=+3.00, SOLO por la palabra "ecuaciones", disparando
    # SLOW_PATH ella sola: WEIGHT_MATH=3.0 ya supera SLOW_PATH_THRESHOLD
    # =1.5 sin ninguna otra señal activa. 111s de dos pasadas completas
    # para un pedido de LISTAR ecuaciones, no de trabajar con una.
    #
    # "\becuaci[oó]n\w*\b" detectaba la PALABRA "ecuación" como
    # sustantivo - confunde "el tema es matemática" con "la tarea
    # requiere razonamiento matemático". Un pedido de listar/recordar
    # ecuaciones no necesita el plan de dos pasadas de slow_path; pedir
    # que se TRABAJE una ecuación sí, y eso ya lo cubren, sin tocar, los
    # patrones de verbo/acción de abajo ("resuelve", "simplifica",
    # "factoriza", "calcula...valor/resultado/raíz/solución", "cuánto
    # es", o una expresión numérica real). Se quita el sustantivo suelto
    # de la alternancia - mismo patrón de fix ya aplicado en este mismo
    # archivo para "explain more about that match" (ver
    # _CONVERSATIONAL_FOLLOWUP_RE arriba: una palabra de dominio, sin
    # intención real detrás, empujando el score ella sola).
    #
    # NO se tocaron "derivada"/"integral" en esta pasada: mismo defecto
    # estructural sospechado (sustantivo suelto, sin verbo), pero sin un
    # caso real medido que lo confirme - y a diferencia de "ecuación",
    # sacarlos sin más sí arriesga una regresión real: "calcula la
    # derivada de f(x)" no cae en ningún otro patrón de abajo (el combo
    # calcul+valor/resultado/raíz/solución no incluye "derivada"), así
    # que perdería la detección de un pedido de cálculo legítimo. Tocar
    # eso a ciegas, sin poder correr el modelo real desde esta sesión,
    # cambia un problema no confirmado por uno nuevo sí seguro. Si "dame
    # las derivadas/integrales más importantes" también dispara
    # slow_path sin necesidad, avisá y se aplica el mismo criterio con
    # el patrón de verbo/combo correspondiente ya resuelto de antemano.
    #
    # Ampliación (medido - video 2026-09-02, capturas adjuntas): "State
    # the Einstein Field Equations..." y "Derive the time-independent
    # Schrödinger equation..." se ruteaban a fast_path con score=+0.00
    # ("sin señales activas"). El fix de consenso router-determinista /
    # router-0.5B NO entra en juego acá: ambos coinciden en fast_path, no
    # hay downgrade que vetar. La causa real es que la alternancia de
    # verbos de arriba es 100% española (resuelve/simplifica/factoriza) -
    # cero equivalente en inglés (derive/solve/state/prove/evaluate) - y
    # ni siquiera cubre la forma VERBAL española "deriva"/"plantea" (solo
    # el sustantivo "derivada"). La rama nueva de abajo es un verbo de
    # TRABAJAR/PRODUCIR una expresión + un objeto matemático explícito
    # dentro de ~40 chars. Distinta de _FACTUAL_ENUMERATION_PATTERN
    # (listar/recordar N leyes de un dominio, peso 0.0): "derivá LA
    # ecuación de Schrödinger" pide construir un resultado formal
    # concreto, y eso sí amerita el plan de dos pasadas. El objeto
    # obligatorio (ecuación/fórmula/teorema/...) es lo que evita el falso
    # positivo con "state your name", "solve my problem" o "la palabra
    # deriva del latín" - ninguno lleva un sustantivo matemático detrás.
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

    # Nota (medido - captura del usuario, turno "dime
    # ecuaciones importantes de la física", ruteado a fast_path/
    # qwen2.5:3b con score=+0.00, tag_summary="(sin señales activas)":
    # ninguna señal existente lo cubre, porque "ecuación" como sustantivo
    # suelto se retiró a propósito de _MATH_PATTERN arriba (ver ese
    # mismo nota) y _CONCEPTUAL_PATTERN solo cubre vocabulario
    # meta-disciplinario ("teoría", "epistemología"), no el nombre de una
    # disciplina como "física"). El modelo completó bien los primeros 3
    # puntos (gravitación de Newton, Ley de Ohm, conservación de
    # energía) y a partir del 4to inventó una "Ley de la Centrífuga"
    # (fórmula y variable inexistentes) y rebautizó una relación real
    # como "Ley de los Ferromagnetismo de Maxwell" (no es de Maxwell, no
    # es específica de ferromagnetismo) - el patrón típico de un modelo
    # chico rellenando por continuación de patrón una vez que se le
    # acaban los hechos que sí tiene memorizados con confianza.
    #
    # Esta señal detecta el pedido de ENUMERAR varios hechos técnicos
    # establecidos (leyes/fórmulas/ecuaciones/principios/teoremas) -
    # distinto de _MATH_PATTERN, que detecta pedir que se TRABAJE una
    # expresión concreta (resolver, derivar, simplificar). Lleva peso
    # 0.0 a propósito (ver WEIGHT_FACTUAL_ENUMERATION): esto NO debe
    # empujar a slow_path - listar/recordar hechos no necesita el plan
    # de dos pasadas (mismo razonamiento que ya establece la nota de
    # _MATH_PATTERN), y además ningún dispatcher de slow_path sabe qué
    # hacer con "enumerá N leyes de un dominio" (CAS espera una expresión
    # con "=", el fuzzer audita lógica, LSC infiere condicionales).
    # Es puramente informativa para el orquestador - ver su uso en
    # orchestrator.py, rama FAST_PATH, justo antes de
    # _build_reasoning_prompt: agrega una advertencia corta pidiendo
    # entregar MENOS elementos correctos antes que inventar uno para
    # completar la cantidad pedida.
    # ES + EN. Verbo de "enumerá/recordá varios" seguido, dentro de 60
    # chars, de un sustantivo técnico establecido. Ampliado tras
    # screenshot 2026-08-27: "tell me the best equations in math" contra
    # phi3.5:3.8b devolvió word-salad - no disparaba porque el patrón era
    # solo español, así que `_should_force_web_search` no forzaba
    # grounding y `_factual_enumeration_caution` no se inyectaba.
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
    _CODE_COMPLEX_PATTERN: Pattern[str] = re.compile(
        r"\b("
        r"implementa\w*|refactoriza\w*|depura\w*|debug\w*|optimiza\w*|optimizaci[oó]n\w*|"
        r"escribe\s+c[oó]digo|crea\s+c[oó]digo|desarrolla\w*|construye\s+c[oó]digo|"
        r"c[oó]digo|program\w*|script\w*|scrip\w*|algoritmo\w*|algoritm\w*|funci[oó]n|clase|m[eé]todo|"
        r"python|phyton|pyton|piton|pytnon|phyt[oó]n|"
        r"javascript|javascrip\w*|jscript|j\s*script|"
        r"html|css|cpp|c\+\+|java|rust|go\b|php|typescript|"
        r"pygame|pigaem|pygaim|pygme|pandas|numpy|tkinter|flask|django|"
        r"matplotlib|selenium|sqlite3?|opencv|kivy|fastapi|"
        r"telemetr[ií]a\w*|hardware|cpu|ram|procesos|"
        r"discord\.?py|"
        r"flappy\w*|game\s+code|estructura\s+de\s+datos|arquitectura\s+de\s+software"
        r")\b",
        re.IGNORECASE,
    )

    # Extensiones de archivo explícitas: señal inequívoca de desarrollo.
    _CODE_EXTENSION_RE: Pattern[str] = re.compile(
        r"\.(py|html|htm|js|jsx|ts|tsx|css|json|cpp|cc|h|hpp|java|"
        r"php|rb|go|rs|cs|sql|sh|bat|ipynb)\b",
        re.IGNORECASE,
    )

    # Librerías/frameworks como señal reforzada e independiente del verbo.
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

    def check_web_intent(self, text: str) -> bool:
        """Detecta consultas probablemente dependientes de actualidad."""
        return bool(self._WEB_KEYWORDS_RE.search(text))

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

        if self._QUANT_CLAIM_PATTERN.search(lowered):
            tags.append(SignalTag.QUANT_CLAIM)
            score += self.WEIGHT_QUANT_CLAIM

        # --- Paso 1: resolución aislada de intención de desarrollo ---
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

        if self._CONCEPTUAL_PATTERN.search(lowered):
            tags.append(SignalTag.CONCEPTUAL_DENSE)
            score += self.WEIGHT_CONCEPTUAL

        if self._EXISTENTIAL_RE.search(stripped):
            tags.append(SignalTag.EXISTENTIAL_SELF)
            score += self.WEIGHT_EXISTENTIAL

        lsc_hits = self._evaluate_lsc_density(stripped, lowered)

        # --- Paso 1.5: seguimiento conversacional simple ---
        # Solo cuenta como "seguimiento simple" (y por lo tanto candidato a
        # neutralizar WEB_SEARCH_INTENT más abajo) si NINGUNA señal de
        # razonamiento lógico/matemático/de código complejo ya está
        # presente - si el usuario pide "explica más" sobre algo que sí
        # requiere ese razonamiento, no se fuerza el atajo.
        complex_reasoning_present = math_detected or logic_audit_detected or code_complex_detected or lsc_hits >= 2
        conversational_followup_detected = (
            not complex_reasoning_present
            and bool(self._CONVERSATIONAL_FOLLOWUP_RE.search(lowered))
        )
        if conversational_followup_detected:
            tags.append(SignalTag.CONVERSATIONAL_FOLLOWUP)
            score += self.WEIGHT_FOLLOWUP_PULL

        # --- Paso 2: WEB_SEARCH_INTENT queda neutralizado a nivel de
        # matriz de decisión si ya se confirmó intención de desarrollo, o
        # si es un seguimiento conversacional simple sin razonamiento
        # complejo real que lo justifique (p. ej. "explain more about
        # that match" no debe activar búsqueda web ni Tree-of-Thoughts
        # solo porque "match" es una palabra clave deportiva) ---
        if (
            not code_complex_detected
            and not conversational_followup_detected
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