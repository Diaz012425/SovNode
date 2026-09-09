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
SovNode — Formula Synthesizer (descubrimiento formal 100% falseable)
=========================================================================
Pedido explícito del usuario (2026-09-05, tras diagnosticar cuellos de
botella): "¿qué idea podemos implementar para descubrir cosas con los
ladrillos de conocimiento actuales, verificándolo y que sea 100%
falseable?". Este módulo es la respuesta concreta.

Por qué NO alcanza con pedirle esto a un LLM directamente: la captura que
motivó el pedido muestra a SovNode negándose (correctamente) a "inventar"
una ecuación nueva — cualquier otra respuesta hubiera sido texto plausible
sin ninguna garantía real detrás, exactamente lo que el resto de este
proyecto lleva toda la sesión blindando (`find_unsupported_scores`,
`_should_force_web_search`, `LogicalCoherenceValidator`, etc.). "100%
falseable" solo es alcanzable si la verificación la hace un motor
DETERMINISTA, nunca el juicio del modelo.

Diseño: hermano directo de `knowledge_synthesizer.py` (mismo patrón de
disparo por inactividad, misma separación WAL-inmutable +
MemoryGraph-mutable + indexación en `longterm_vector_rag`), pero para el
dominio FORMAL en vez del textual:

  - `KnowledgeSynthesizer` verifica axiomas de PROSA con una validación
    "grounded" (similitud geométrica + cita textual EXACTA) — sólida
    contra alucinación de citas, pero no es una prueba: el juez LLM
    todavía decide SI existe una conexión real.
  - `FormulaSynthesizer` (este módulo) verifica ECUACIONES: el LLM
    (`orchestrator.router_model`, el mismo 0.5B ya residente que usa el
    router — no suma un modelo nuevo a mantener caliente en VRAM) SOLO
    elige qué dos fórmulas de una base curada combinar y qué variable
    eliminar por sustitución. El álgebra la hace `sympy` (vía
    `derive_and_verify`, pura y sin estado — ver más abajo), y el
    resultado se somete además a una tanda de chequeos NUMÉRICOS al azar,
    independientes de la cadena simbólica. Si cualquiera de los dos
    falla, se descarta en silencio — el modelo nunca decide qué es
    verdad, solo qué vale la pena intentar.

Alcance honesto (deliberadamente chico, para no prometer de más):
  - La base de fórmulas conocidas (`KNOWN_FORMULAS`) es curada a mano,
    de mecánica clásica newtoniana de curso básico — cada una
    indiscutible por sí sola. Este módulo NO ingiere fórmulas de la web
    ni de texto libre: a diferencia de los axiomas de
    `KnowledgeSynthesizer`, acá el riesgo de partir de un "ladrillo" ya
    equivocado tiene que ser cero, porque toda la garantía de
    "descubrimiento" depende de que las entradas sean ciertas por
    construcción, no verificadas en el momento.
  - La fórmula DERIVADA siempre usa la rama PRINCIPAL de `sympy.solve`
    (la primera solución) cuando hay varias — una ecuación con signo ±
    (p. ej. una raíz cuadrada) no genera dos axiomas separados, solo
    uno. La verificación numérica SÍ empareja esa rama consigo misma
    correctamente sin importar el orden en que `sympy` devuelva las
    raíces (ver la nota extensa junto a "bug real #2" en
    `derive_and_verify`) — así que una fórmula con ambigüedad de signo
    no se rechaza por eso solo, pero el signo opuesto (igual de válido
    físicamente) nunca se ofrece como axioma separado.
  - El chequeo de "novedad" (que el resultado no sea ya una fórmula
    conocida disfrazada) es una comparación simbólica exacta contra la
    base conocida — no detecta un resultado "técnicamente distinto pero
    aburrido" (p. ej. multiplicado por una constante rara).
  - "Descubrimiento" acá significa recombinación mecánica y verificada
    de fórmulas YA conocidas — nunca investigación de física nueva.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import math
import random
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import sympy
from sympy import Eq, Symbol

from cas_sandbox import CASEngine
from embeddings import get_embedding
from wal import KnowledgeNode

logger = logging.getLogger("SovNode.FormulaSynthesizer")


def _cosine(a: "Optional[list]", b: "Optional[list]") -> float:
    """Mismo helper que `knowledge_synthesizer._cosine` (duplicado a
    propósito, no importado -- ambos módulos son hermanos deliberadamente
    independientes, ver el docstring del módulo). Similitud coseno entre
    dos vectores; 0.0 ante cualquier entrada inválida (None, largos
    distintos, norma cero) en vez de lanzar -- este helper se usa para
    ORDENAR candidatos, nunca para decidir qué es verdad, así que un 0.0
    conservador ante datos raros es preferible a una excepción que corte
    el barrido entero."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)

# =====================================================================
# Base de fórmulas de confianza (mecánica clásica newtoniana, curso
# básico, sin ambigüedad). Cada símbolo compartido entre dos o más
# fórmulas es un candidato válido de combinación — la superposición de
# variables (v, m, F, t, ...) es intencional, no un accidente.
# =====================================================================
KNOWN_FORMULAS: Dict[str, str] = {
    "velocidad_final": "v = u + a*t",
    "posicion_uniforme": "s = u*t + (1/2)*a*t^2",
    # `^` para potencias en esta tabla curada a mano (estilo de la casa:
    # se lee como "elevado a"). `CASEngine` lo interpreta como potencia
    # vía `convert_xor`. `**` también funciona desde que
    # `CASEngine._collapse_operator_run` lo preserva (ver cas_sandbox.py) —
    # antes lo colapsaba a `*` en silencio, "v**2" -> "v*2"; ese era un
    # bug real reproducido en vivo (turno 3: "E_k = m*v" dado por
    # verificado). Las fórmulas DESCUBIERTAS se reinsertan con la notación
    # `**` nativa de sympy sin traducir.
    "torricelli": "v^2 = u^2 + 2*a*s",
    "fuerza": "F = m*a",
    # `F_g`/`E_k` con guion bajo A PROPÓSITO, no `Fg`/`Ek`: otro bug real
    # encontrado al probar este módulo -- `parse_expr` con
    # `implicit_multiplication_application` (ver CASEngine._TRANSFORMATIONS
    # en cas_sandbox.py) interpreta dos letras pegadas como una
    # MULTIPLICACIÓN ("Fg" -> F*g, "Ek" -> E*k), no como un único símbolo
    # de dos letras. El guion bajo no se ve afectado por esa
    # transformación y sigue siendo un identificador válido para sympy.
    "peso": "F_g = m*g",
    "momento_lineal": "p = m*v",
    "energia_cinetica": "E_k = (1/2)*m*v^2",
    "trabajo": "W = F*d",
    "potencia": "P = W/t",
    "impulso": "J = F*t",

    # ---- Ampliación 2026-09-05 (pedido explícito: "aplica las mejoras
    # 1 2 3 y 4" -- la #2 es "ampliá la base de fórmulas, drásticamente":
    # cada fórmula nueva no suma combinaciones linealmente, las MULTIPLICA
    # contra toda la base ya existente. Mismo cuidado de nomenclatura que
    # arriba, con dos bugs adicionales encontrados al validar ESTA
    # ampliación (ver el script de verificación, no solo comentario):
    #   - Una palabra española/inglesa de 3+ letras que NO es un nombre de
    #     letra griega reconocido por sympy (p. ej. "presion", "torque")
    #     se hace pedazos letra por letra bajo implicit_multiplication
    #     ("presion" -> e*i*n*o*p*r*s). Los nombres de letras griegas
    #     (tau, rho, Delta, pi...) SÍ sobreviven enteros -- sympy los
    #     reconoce como símbolo/constante antes de intentar partirlos.
    #     Por eso "torque" usa el símbolo griego `tau`, no la palabra.
    #   - Una letra pegada a un dígito ("q1", "M1", "n1") NO es un
    #     identificador de dos caracteres: el dígito se interpreta como
    #     COEFICIENTE numérico ("q1" -> q*1 -> q), así que "q1*q2"
    #     colapsaba a "2*q**2" en vez de dos cargas distintas. Fix: separar
    #     con guion bajo ("q_1", "q_2"), igual que F_g/E_k arriba.
    # Superposición deliberada con símbolos ya usados arriba (F, m, g, t,
    # r, v) para habilitar combinaciones cruzadas nuevas; los casos donde
    # el mismo símbolo significa la MISMA magnitud física en dos dominios
    # (p. ej. F_g compartido entre "peso" -de superficie- y
    # "gravitacion_universal" -general-, que juntos derivan g=G*M/r^2,
    # un resultado real y conocido) son intencionales, no un accidente de
    # nomenclatura.
    "energia_potencial": "E_p = m*g*h",
    "fuerza_centripeta": "F_c = m*v^2/r",
    "torque": "tau = F*r",
    "presion": "P_p = F/A",
    "densidad": "rho = m/V_vol",
    "ley_hooke": "F = k*x",
    "energia_potencial_elastica": "E_ep = (1/2)*k*x^2",
    "gravitacion_universal": "F_g = G*M*m/r^2",
    # `I_c`, NO `I`: bug real encontrado al validar esta ampliación --
    # "I" suelta no es un Symbol, es la unidad IMAGINARIA de sympy
    # (`sympy.I`, √-1, una constante fija) -- exactamente el mismo tipo de
    # colisión que "Q" (arriba) con el sistema de supuestos. `free_symbols`
    # la excluye en silencio, así que la corriente eléctrica "desaparecía"
    # de la ecuación sin ningún error visible hasta la verificación
    # numérica (que fallaba 0/180 al necesitar sustituir un símbolo que
    # nunca estuvo ahí).
    "ley_ohm": "V = I_c*R",
    "potencia_electrica": "P = V*I_c",
    "energia_electrica": "E_elec = P*t",
    # `Q_c`, NO `Q`: bug real encontrado al validar esta ampliación --
    # sympy usa el nombre "Q" globalmente para su sistema de supuestos
    # (`sympy.Q.even`, `sympy.Q.positive`, el objeto `AssumptionKeys`), así
    # que un "Q" suelto no se convierte en un Symbol nuevo -- `parse_expr`
    # tira `SympifyError` al intentar operar con ese objeto. `Q_cal` más
    # abajo (calor) ya usaba guion bajo por otra razón (evitar colisión
    # con esta misma "Q" de carga) -- ahora la propia carga también lo
    # necesita, por un motivo distinto (esta vez para escapar de sympy,
    # no de otra fórmula).
    "capacitor_carga": "Q_c = C*V",
    "energia_capacitor": "E_c = (1/2)*C*V^2",
    "ley_coulomb": "F_e = k_e*q_1*q_2/r^2",
    # `R_gas`, NO `R`: "R" ya se usa arriba para resistencia eléctrica
    # (ley_ohm) -- son dos constantes físicas DISTINTAS (resistencia vs.
    # constante universal de los gases) que casualmente comparten letra
    # en la notación de manual; a diferencia de F_g/P (mismo símbolo,
    # misma magnitud física real), acá reusar "R" sería una colisión de
    # nombres accidental y confusa, no una superposición intencional.
    "gas_ideal": "P_p*V_vol = n*R_gas*T",
    "presion_hidrostatica": "P_p = rho*g*h",
    "capacidad_calorifica": "Q_cal = m*c*Delta_T",
    # `T_p`, NO `T`: "T" ya se usa arriba para temperatura (gas_ideal) --
    # mismo motivo que R_gas: periodo y temperatura no son la misma
    # magnitud, aunque ambas se abrevien "T" en manuales distintos.
    "periodo_pendulo": "T_p = 2*pi*(L/g)^(1/2)",

    # ---- Ampliación 2026-09-05 (b) -- dominio de CÓMPUTO/HARDWARE ----
    # Pedido explícito del usuario: "enfocate en armarlo" (armar la base
    # de cómputo real -- FLOPS, ancho de banda de memoria, TDP -- para que
    # las combinaciones sean útiles para SU hardware, no solo física de
    # manual). Reutilización deliberada de C y V con capacitor_carga /
    # energia_capacitor: la potencia dinámica de un chip CMOS es
    # literalmente la misma C (capacitancia) y la misma V (voltaje) que ya
    # están en la base -- el puente electrónica/cómputo es una relación
    # física real, no un accidente de nombres.
    #
    # `f`, NO `F`: sympy distingue mayúscula/minúscula -- "f" (frecuencia
    # de reloj) es un símbolo DISTINTO de "F" (fuerza, ya en uso arriba),
    # sin colisión.
    "potencia_dinamica_cmos": "P = C*V^2*f",
    # `N_c`, NO `N`: bug real encontrado al validar esta ampliación --
    # sympy usa "N" globalmente como función de evaluación numérica
    # (`sympy.N(expr)`, equivalente a `evalf`), así que un "N" suelto no
    # se parsea como Symbol -- CASEngine revienta con un TypeError al
    # intentar multiplicarlo (`'function' and 'Symbol'`). Mismo tipo de
    # colisión que "Q" (AssumptionKeys) e "I" (unidad imaginaria) de
    # arriba, tercer caso de este bug. `k_i` (no `IPC`/`I_pc`): evita
    # confundir con `I_c` (corriente eléctrica, ya en uso) -- acá es un
    # factor adimensional de instrucciones por ciclo, otra magnitud.
    "rendimiento_computo": "R_c = N_c*f*k_i",
    # `f_m`, NO `f`: el reloj de memoria es una señal física DISTINTA del
    # reloj de cómputo de `potencia_dinamica_cmos` -- compartir símbolo acá
    # sería una colisión accidental (como R/R_gas arriba), no una
    # superposición real.
    "ancho_banda_memoria": "B_m = W_b*f_m*n_ddr",
    # `I_a`, NO `AI`: bug real encontrado al validar esta ampliación --
    # "AI" (intensidad aritmética, "arithmetic intensity") no es un
    # nombre de letra griega reconocido, así que se hace pedazos por
    # multiplicación implícita ("AI" -> A*I) -- y automáticamente arrastra
    # también la colisión de "I" bare (unidad imaginaria) de paso. Modelo
    # de "roofline" en régimen limitado por memoria: el rendimiento
    # alcanzable es intensidad aritmética × ancho de banda -- la palanca
    # real de optimización de software (más intensidad aritmética por
    # byte movido) frente a la palanca de hardware (`ancho_banda_
    # memoria` de arriba).
    "potencia_memoria_limitada": "P_mem = I_a*B_m",
}


class DerivationStatus(str, Enum):
    VERIFIED = "verified"
    REJECTED_INVALID_INPUT = "rejected_invalid_input"
    REJECTED_NO_SOLUTION = "rejected_no_solution"
    REJECTED_NOT_NOVEL = "rejected_not_novel"
    REJECTED_NUMERIC_MISMATCH = "rejected_numeric_mismatch"
    REJECTED_SELF_REFERENTIAL = "rejected_self_referential"
    ERROR = "error"


@dataclass
class DerivationResult:
    status: DerivationStatus
    success: bool
    formula_a: str = ""
    formula_b: str = ""
    eliminate_symbol: str = ""
    new_lhs: str = ""
    new_rhs: str = ""
    detail: str = ""
    numeric_trials: int = 0
    numeric_trials_passed: int = 0

    def __str__(self) -> str:
        if not self.success:
            return f"[{self.status.value}] {self.detail}"
        return f"{self.new_lhs} = {self.new_rhs}  ({self.detail})"


_NUMERIC_TRIALS = 6
_NUMERIC_TOLERANCE = 1e-6
# Rango positivo y lejos de cero a propósito: los símbolos de la base de
# arriba son todos magnitudes físicas (masa, tiempo, velocidad...) y un
# valor negativo o nulo al azar arriesgaría dominios inválidos (raíces,
# divisiones) por una razón ajena a si la fórmula derivada es correcta.
_NUMERIC_SAMPLE_RANGE = (1.0, 12.0)


def _parse_known_formula(cas: CASEngine, formula_str: str) -> "sympy.Eq":
    if "=" not in formula_str:
        raise ValueError("La fórmula no tiene el formato 'lhs = rhs'.")
    lhs_str, rhs_str = formula_str.split("=", 1)
    return Eq(cas._parse(lhs_str), cas._parse(rhs_str))


def derive_and_verify(
    formula_a_name: str,
    formula_b_name: str,
    eliminate_symbol: str,
    known_formulas: Optional[Dict[str, str]] = None,
    cas: Optional[CASEngine] = None,
    rng: Optional[random.Random] = None,
) -> DerivationResult:
    """
    Núcleo puro y determinista de este módulo (sin I/O, sin LLM, sin
    threading — ver el docstring del módulo para el diseño completo).
    Deriva una nueva relación sustituyendo `eliminate_symbol` (despejado
    de `formula_a_name` vía `formula_b_name`) y la verifica en dos capas
    independientes antes de aceptarla:

      1. Simbólica: `sympy.solve` + sustitución + `sympy.simplify`, más
         un chequeo de que el resultado no sea ya una fórmula conocida
         disfrazada (si no, no "descubrió" nada).
      2. Numérica adversarial: varias muestras de valores al azar,
         evaluadas en el resultado FINAL de forma independiente de la
         cadena simbólica que lo produjo — atrapa un error de rama o de
         signo que `simplify()` por sí solo podría no exponer.

    Cualquier fallo en cualquiera de las dos capas rechaza el candidato
    entero; nunca se "confía" en una sola de las dos.
    """
    known_formulas = known_formulas if known_formulas is not None else KNOWN_FORMULAS
    cas = cas or CASEngine()
    rng = rng or random.Random()

    if formula_a_name == formula_b_name:
        return DerivationResult(
            status=DerivationStatus.REJECTED_INVALID_INPUT, success=False,
            formula_a=formula_a_name, formula_b=formula_b_name,
            eliminate_symbol=eliminate_symbol,
            detail="Las dos fórmulas elegidas son la misma — no hay nada que combinar.",
        )
    if formula_a_name not in known_formulas or formula_b_name not in known_formulas:
        return DerivationResult(
            status=DerivationStatus.REJECTED_INVALID_INPUT, success=False,
            formula_a=formula_a_name, formula_b=formula_b_name,
            eliminate_symbol=eliminate_symbol,
            detail="Una de las dos fórmulas no existe en la base de conocimiento.",
        )

    try:
        eq_a = _parse_known_formula(cas, known_formulas[formula_a_name])
        eq_b = _parse_known_formula(cas, known_formulas[formula_b_name])
    except Exception as exc:
        return DerivationResult(
            status=DerivationStatus.ERROR, success=False,
            formula_a=formula_a_name, formula_b=formula_b_name,
            eliminate_symbol=eliminate_symbol,
            detail=f"No se pudo parsear alguna de las dos fórmulas: {exc}",
        )

    try:
        symbol = Symbol(eliminate_symbol) if eliminate_symbol else None
    except Exception:
        symbol = None
    if symbol is None or symbol not in eq_a.free_symbols or symbol not in eq_b.free_symbols:
        return DerivationResult(
            status=DerivationStatus.REJECTED_INVALID_INPUT, success=False,
            formula_a=formula_a_name, formula_b=formula_b_name,
            eliminate_symbol=eliminate_symbol,
            detail=(
                f"'{eliminate_symbol}' no es una variable COMPARTIDA por ambas "
                "fórmulas — no hay sustitución posible entre ellas."
            ),
        )

    try:
        solutions = sympy.solve(eq_a, symbol)
    except Exception as exc:
        return DerivationResult(
            status=DerivationStatus.ERROR, success=False,
            formula_a=formula_a_name, formula_b=formula_b_name,
            eliminate_symbol=eliminate_symbol,
            detail=f"sympy.solve falló: {exc}",
        )
    if not solutions:
        return DerivationResult(
            status=DerivationStatus.REJECTED_NO_SOLUTION, success=False,
            formula_a=formula_a_name, formula_b=formula_b_name,
            eliminate_symbol=eliminate_symbol,
            detail=f"No se pudo despejar '{eliminate_symbol}' de '{formula_a_name}'.",
        )
    # Solo la rama principal — ver "Alcance honesto" en el docstring del
    # módulo: no cubre ecuaciones con varias ramas físicas relevantes.
    substitution = solutions[0]

    try:
        new_eq = Eq(
            sympy.simplify(eq_b.lhs.subs(symbol, substitution)),
            sympy.simplify(eq_b.rhs.subs(symbol, substitution)),
        )
    except Exception as exc:
        return DerivationResult(
            status=DerivationStatus.ERROR, success=False,
            formula_a=formula_a_name, formula_b=formula_b_name,
            eliminate_symbol=eliminate_symbol,
            detail=f"La sustitución simbólica falló: {exc}",
        )

    if symbol in new_eq.free_symbols:
        return DerivationResult(
            status=DerivationStatus.ERROR, success=False,
            formula_a=formula_a_name, formula_b=formula_b_name,
            eliminate_symbol=eliminate_symbol,
            detail=f"'{eliminate_symbol}' sigue presente tras la sustitución (no se eliminó).",
        )

    # ---- Capa 1a: auto-referencia — ¿la variable ÚNICA del lado
    # izquierdo reaparece del lado derecho? (bug real, MEDIDO en captura
    # 2026-09-06: "Q_cal = Q_cal*V_vol*rho/m" al combinar
    # `capacidad_calorifica` -Q_cal = m*c*Delta_T- con una fórmula
    # DESCUBIERTA que a su vez había sido derivada DE
    # `capacidad_calorifica` en un turno anterior y por eso todavía
    # cargaba el símbolo `Q_cal` sin haberlo eliminado. Cuando las dos
    # fórmulas de un combo comparten un ancestro común Y el lado
    # izquierdo de `formula_b` es un símbolo SUELTO (no una expresión
    # compuesta -- `Q_cal`, no `v^2`), sustituir una en la otra puede
    # devolver esa misma variable definida a los dos lados -- una
    # identidad disfrazada de "Q_cal" cancelable en ambos lados (acá:
    # 1 = V_vol*rho/m, una reformulación de `densidad` con `Q_cal` de
    # más), NUNCA una relación nueva de verdad. `sympy.simplify` no
    # cancela ese factor común automáticamente entre lhs y rhs de una
    # `Eq`, así que sin este chequeo pasaba las capas 1b/2 igual (ambos
    # lados escalan junto con `Q_cal`, así que hasta la verificación
    # numérica adversarial "confirma" la identidad vacía). Se rechaza
    # acá, ANTES de gastar la verificación numérica.
    #
    # OJO -- por qué el chequeo se restringe a un COCIENTE que cancela el
    # símbolo por completo (bug real encontrado al validar ESTE mismo fix,
    # con una combinación YA existente y deliberadamente aceptada en la
    # base curada): "eliminar u entre torricelli y velocidad_final" da
    # `v = sqrt(v**2 - 2*a*s) + a*t` -- 'v' (símbolo suelto del lado
    # izquierdo) TAMBIÉN reaparece del lado derecho, pero acá NO hay
    # ninguna identidad vacía escondida: es una relación real (equivale,
    # despejando, a la fórmula ya conocida `posicion_uniforme`) que
    # simplemente quedó en forma implícita, con 'v' de los dos lados de
    # un modo genuinamente irreductible (dentro de una raíz). La
    # diferencia real con el bug de `Q_cal` no es SI el símbolo
    # reaparece, sino CÓMO: en `Q_cal = Q_cal*V_vol*rho/m` el símbolo es
    # un factor multiplicativo que se cancela ENTERO al dividir ambos
    # lados por él (rhs/lhs = V_vol*rho/m, sin rastro de `Q_cal` --  la
    # ecuación es literalmente "1 = V_vol*rho/m" disfrazada); en el caso
    # de `v` el cociente rhs/lhs = (a*t + sqrt(v**2-2*a*s))/v SIGUE
    # conteniendo 'v' después de simplificar -- no hay ningún factor que
    # cancelar, es una ecuación implícita legítima. Por eso el chequeo
    # real es: ¿`sympy.simplify(rhs/lhs)` sigue dependiendo del símbolo
    # del lado izquierdo? Si NO depende más de él, es la cancelación
    # vacía del bug de `Q_cal` -- se rechaza. Si SIGUE presente (como acá
    # con 'v'), es una relación genuina, aunque implícita, y se deja
    # pasar a las capas 1b/2 como antes.
    if new_eq.lhs.is_Symbol and new_eq.lhs in new_eq.rhs.free_symbols:
        with contextlib.suppress(Exception):
            ratio = sympy.simplify(new_eq.rhs / new_eq.lhs)
            if new_eq.lhs not in ratio.free_symbols:
                return DerivationResult(
                    status=DerivationStatus.REJECTED_SELF_REFERENTIAL, success=False,
                    formula_a=formula_a_name, formula_b=formula_b_name,
                    eliminate_symbol=eliminate_symbol,
                    detail=(
                        f"El resultado es circular: '{new_eq.lhs}' es un "
                        "factor que se cancela por completo en ambos lados "
                        f"(dividiendo, queda '{ratio}', sin rastro de "
                        f"'{new_eq.lhs}') -- señal de que las dos fórmulas "
                        "comparten un ancestro común (una fue derivada de "
                        "la otra en algún momento) y esto es una identidad "
                        "disfrazada, no una relación nueva."
                    ),
                )

    # ---- Capa 1b: novedad — ¿ya es una fórmula conocida disfrazada? ----
    try:
        residual_new = sympy.simplify(new_eq.lhs - new_eq.rhs)
    except Exception as exc:
        return DerivationResult(
            status=DerivationStatus.ERROR, success=False,
            formula_a=formula_a_name, formula_b=formula_b_name,
            eliminate_symbol=eliminate_symbol,
            detail=f"No se pudo simplificar el residual del resultado: {exc}",
        )
    for other_name, other_str in known_formulas.items():
        try:
            other_eq = _parse_known_formula(cas, other_str)
        except Exception:
            continue
        if other_eq.free_symbols != new_eq.free_symbols:
            continue
        with contextlib.suppress(Exception):
            other_residual = other_eq.lhs - other_eq.rhs
            if (
                sympy.simplify(residual_new - other_residual) == 0
                or sympy.simplify(residual_new + other_residual) == 0
            ):
                return DerivationResult(
                    status=DerivationStatus.REJECTED_NOT_NOVEL, success=False,
                    formula_a=formula_a_name, formula_b=formula_b_name,
                    eliminate_symbol=eliminate_symbol,
                    detail=f"El resultado es equivalente a la fórmula ya conocida '{other_name}'.",
                )

    # ---- Capa 2: verificación numérica adversarial ----
    # OJO (bug real #1, encontrado al probar este mismo módulo): asignarle
    # un valor al azar a CADA símbolo libre de `new_eq` -- incluido el
    # lado izquierdo, típicamente una única variable "resultado" como `v`
    # -- y comparar lhs contra rhs está mal planteado: `v` no es
    # independiente de las demás, ES la incógnita que la ecuación define,
    # así que un valor al azar para `v` casi nunca va a coincidir con el
    # rhs calculado a partir de OTROS valores al azar, sin importar si la
    # derivación fue correcta. El chequeo real y no-circular es otro:
    # resolver `eq_a` para `symbol` de NUEVO, de forma independiente (sin
    # reusar `solutions` de arriba), sustituirlo en `eq_b.rhs` -esa es la
    # "verdad de referencia", calculada por el camino largo- y comparar
    # contra `new_eq.rhs` evaluado en el mismo punto -el resultado del
    # atajo simbólico.
    #
    # OJO (bug real #2, MEDIDO en un barrido combinatorio manual sobre las
    # 64 combinaciones posibles de la base curada: 12/64 rechazadas por
    # "mismatch numérico" con 0 o 2 de 6 muestras pasando -- en su momento
    # se documentó como "ambigüedad de rama real" (`sympy.solve` de una
    # cuadrática, rama ± no determinista), pero al investigar 10 de esos
    # 12 casos NO tenían ninguna ecuación cuadrática de por medio: el
    # verdadero bug era que `input_syms` salía solo de
    # `new_eq.rhs.free_symbols`, que puede perder variables que SÍ hacen
    # falta para resolver `eq_a` de nuevo. Ejemplo real: eliminando 'v'
    # entre velocidad_final (v=u+a*t) y torricelli (v²=u²+2*a*s),
    # new_eq.rhs=u²+2*a*s nunca menciona 't' -- pero `eq_a.subs(env)`
    # necesita un valor para 't' para poder resolver 'v' de nuevo; sin él
    # queda una expresión simbólica, `complex(...)` explota, la muestra se
    # descarta en silencio, y las 6 fallan siempre (0/6). Fix: `input_syms`
    # sale de la UNIÓN entre lo que `eq_a` necesita (sus símbolos propios
    # menos el que se está despejando) y lo que `new_eq.rhs` necesita --
    # así ninguna de las dos resoluciones se queda corta de variables.
    #
    # De los 12 casos rechazados, solo 2 eran ambigüedad de rama de
    # verdad (`torricelli` despejado para 'v' o 'u', ambos con grado 2 --
    # dos raíces ±). Para esos, el segundo fix real es no comparar contra
    # `fresh_solutions[0]` a ciegas (ese índice es arbitrario, no
    # necesariamente la misma rama que `substitution` eligió más arriba)
    # sino contra la raíz de `fresh_solutions` más CERCANA en valor a lo
    # que la propia rama elegida (`substitution`) predice en ese mismo
    # punto -- así la comparación siempre empareja la MISMA rama consigo
    # misma, sin importar en qué orden arbitrario `sympy.solve` devuelva
    # las raíces. Con los dos fixes, las 12 combinaciones antes
    # rechazadas ahora verifican 6/6 (confirmado empíricamente).
    input_syms_set = (eq_a.free_symbols - {symbol}) | new_eq.rhs.free_symbols
    if not input_syms_set:
        holds = False
        with contextlib.suppress(Exception):
            holds = bool(sympy.simplify(new_eq.lhs - new_eq.rhs) == 0)
        if not holds:
            return DerivationResult(
                status=DerivationStatus.REJECTED_NUMERIC_MISMATCH, success=False,
                formula_a=formula_a_name, formula_b=formula_b_name,
                eliminate_symbol=eliminate_symbol,
                detail="El resultado no depende de ninguna variable y no se sostiene como identidad.",
            )
        trials_passed = _NUMERIC_TRIALS
    else:
        input_syms = sorted(input_syms_set, key=str)
        trials_passed = 0
        trials_attempted = 0
        # OJO (bug real #3): una rama de grado >= 2 (p. ej. u=±sqrt(v²-2as)
        # de `torricelli`) solo es REAL para una parte del rango de
        # muestreo -- para 'u' concretamente, hace falta v² >= 2*a*s, algo
        # que NO se cumple para la mayoría de los (v,a,s) al azar en
        # _NUMERIC_SAMPLE_RANGE. Un bucle de exactamente _NUMERIC_TRIALS
        # muestras, donde cada muestra fuera de dominio se salta con
        # `continue` SIN reintentar, termina el bucle entero con muy pocas
        # comparaciones válidas (2/6 MEDIDO) -- rechazando una derivación
        # correcta solo porque el rango de muestreo pisó el dominio
        # inválido casi siempre, no porque la fórmula esté mal. Fix seguro
        # sin costo real (todas las fórmulas de esta base son baratas de
        # evaluar): seguir tomando muestras nuevas hasta acumular
        # _NUMERIC_TRIALS comparaciones VÁLIDAS (dominio real, rama
        # emparejada), con un tope de intentos para no colgarse si una
        # fórmula genuinamente casi nunca cae en el dominio real.
        _MAX_SAMPLE_ATTEMPTS = _NUMERIC_TRIALS * 30
        for _ in range(_MAX_SAMPLE_ATTEMPTS):
            if trials_attempted >= _NUMERIC_TRIALS:
                break
            env = {s: rng.uniform(*_NUMERIC_SAMPLE_RANGE) for s in input_syms}
            try:
                # Rama elegida (la misma que ya se usó para construir
                # `new_eq` más arriba), evaluada en este punto -- es
                # contra ESTE valor, no contra un índice arbitrario, que
                # hay que emparejar la resolución fresca de abajo.
                candidate_val = complex(substitution.subs(env))
                if abs(candidate_val.imag) > 1e-6:
                    continue  # esta rama no es real en este punto -- se descarta la muestra, no cuenta
                # "Verdad de referencia": resolver eq_a para `symbol` DE
                # NUEVO en este punto numérico (independiente de
                # `solutions`, calculado antes con álgebra simbólica) y
                # sustituirlo en eq_b.rhs sin pasar nunca por new_eq.
                fresh_solutions = sympy.solve(eq_a.subs(env), symbol)
                if not fresh_solutions:
                    continue
                # Empareja por VALOR más cercano a `candidate_val`, no por
                # índice -- ver la nota extensa de arriba (bug real #2).
                best = min(fresh_solutions, key=lambda s: abs(complex(s) - candidate_val))
                symbol_val = complex(best)
                if abs(symbol_val.imag) > 1e-6:
                    continue
                if abs(symbol_val - candidate_val) > 1e-4 * max(1.0, abs(candidate_val)):
                    continue  # ninguna raíz fresca corresponde a esta rama en este punto
                ground_truth_val = float(eq_b.rhs.subs({**env, symbol: symbol_val.real}))
                # "Atajo simbólico": new_eq.rhs ya tiene la sustitución
                # hecha -- evaluarlo en el MISMO punto no debería
                # necesitar el valor de `symbol` en absoluto.
                shortcut_val = float(new_eq.rhs.subs(env))
            except (TypeError, ValueError, ZeroDivisionError, OverflowError, IndexError):
                continue
            trials_attempted += 1
            if abs(ground_truth_val - shortcut_val) <= _NUMERIC_TOLERANCE * max(1.0, abs(ground_truth_val)):
                trials_passed += 1
        if trials_attempted < _NUMERIC_TRIALS or trials_passed < _NUMERIC_TRIALS:
            return DerivationResult(
                status=DerivationStatus.REJECTED_NUMERIC_MISMATCH, success=False,
                formula_a=formula_a_name, formula_b=formula_b_name,
                eliminate_symbol=eliminate_symbol,
                numeric_trials=_NUMERIC_TRIALS, numeric_trials_passed=trials_passed,
                detail=(
                    f"Solo {trials_passed}/{_NUMERIC_TRIALS} muestras numéricas al "
                    "azar confirmaron la igualdad — se descarta por seguridad."
                    if trials_attempted >= _NUMERIC_TRIALS else
                    f"No se alcanzaron {_NUMERIC_TRIALS} muestras en el dominio real "
                    f"tras {_MAX_SAMPLE_ATTEMPTS} intentos (solo {trials_attempted} "
                    "cayeron en el dominio válido) — se descarta por seguridad."
                ),
            )

    return DerivationResult(
        status=DerivationStatus.VERIFIED, success=True,
        formula_a=formula_a_name, formula_b=formula_b_name,
        eliminate_symbol=eliminate_symbol,
        new_lhs=str(new_eq.lhs), new_rhs=str(new_eq.rhs),
        numeric_trials=_NUMERIC_TRIALS, numeric_trials_passed=trials_passed,
        detail=(
            f"Derivado de '{formula_a_name}' + '{formula_b_name}' eliminando "
            f"'{eliminate_symbol}'; verificado simbólicamente (sympy.simplify) "
            f"y con {trials_passed}/{_NUMERIC_TRIALS} muestras numéricas independientes."
        ),
    )


# =====================================================================
# Capa de proposición (LLM) + ciclo de fondo — mismo patrón de disparo
# por inactividad que KnowledgeSynthesizer.
# =====================================================================
_PROPOSER_SYSTEM_PROMPT = (
    "Sos un asistente de física/matemática que PROPONE, nunca calcula. Se "
    "te muestra una lista de fórmulas conocidas, cada una con su nombre y "
    "su expresión. Tu única tarea es elegir DOS fórmulas DISTINTAS que "
    "compartan al menos una variable, y esa variable compartida a "
    "eliminar por sustitución. NO hagas el álgebra vos — un motor "
    "simbólico separado la hace y la verifica; si tu elección no produce "
    "algo válido, simplemente se descarta sin ningún efecto.\n\n"
    "Respondé ÚNICAMENTE con un objeto JSON, sin texto alrededor, con "
    "esta forma exacta:\n"
    '{"formula_a": "<nombre_exacto>", "formula_b": "<nombre_exacto>", '
    '"eliminate": "<variable_compartida>"}'
)


class FormulaSynthesizer(threading.Thread):
    """Ver el docstring del módulo para el diseño completo."""

    DOMAIN = "synthetic_formula"
    # Candidatos LLM que se prueban por ciclo antes de rendirse — un
    # techo bajo a propósito: cada intento fallido solo cuesta una
    # llamada al 0.5B (barata), pero no tiene sentido insistir
    # indefinidamente en un solo ciclo de inactividad.
    MAX_ATTEMPTS_PER_CYCLE = 3
    PROPOSER_NUM_PREDICT = 120
    # Mejora #1 (2026-09-05, "aplica las mejoras 1 2 3 y 4"): presupuesto
    # de TIEMPO, no de cantidad de intentos, para el barrido en vivo que
    # dispara un turno del usuario -- cada combinación es barata (sympy
    # puro, sin red ni LLM) pero la base crece con el tiempo, así que
    # limitar por tiempo mantiene la respuesta del turno acotada sin
    # importar cuánto haya crecido `known_formulas`.
    LIVE_DISCOVERY_TIME_BUDGET_SECONDS = 6.0
    # Tope de fórmulas descubiertas que se listan en el prompt del
    # proposer (mejora #3): la base curada original siempre entra
    # entera; de las descubiertas dinámicamente solo se listan las más
    # recientes, para que el prompt no crezca sin límite turno tras turno.
    MAX_DISCOVERED_IN_PROPOSER_PROMPT = 15

    def __init__(
        self,
        orchestrator,
        interval_seconds: int = 600,
        known_formulas: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(daemon=True, name="FormulaSynthesizer")
        self.orchestrator = orchestrator
        self.interval = interval_seconds
        self._running = True
        # SIEMPRE una copia propia y mutable -- nunca una referencia
        # directa al dict del MÓDULO (antes: `known_formulas or
        # KNOWN_FORMULAS`, que aliasaba el global). Mejora #3
        # ("derivaciones encadenadas"): cada fórmula nueva verificada se
        # reinserta acá bajo un nombre generado (ver `_persist`), así la
        # próxima ronda de combinaciones -- en vivo o de fondo -- puede
        # usarla como ladrillo (A+B -> C, después C+D -> E) sin necesitar
        # un algoritmo de eliminación multi-fórmula aparte: el barrido
        # pairwise ya existente hace el trabajo solo, iteración tras
        # iteración. Sin la copia propia, dos instancias (p. ej. un test
        # y el daemon real) terminarían mutando el mismo dict global.
        self.known_formulas: Dict[str, str] = (
            dict(known_formulas) if known_formulas is not None else dict(KNOWN_FORMULAS)
        )
        self._cas = CASEngine()
        self._discovered_count = 0
        # Combos (formula_a, formula_b, símbolo) ya intentados sin éxito
        # en ESTE proceso -- evita repetir el mismo trabajo indefinidamente
        # turno tras turno/ciclo tras ciclo. Una fórmula NUEVA agregada más
        # tarde abre combos que todavía no están acá, así que esta memoria
        # nunca puede bloquear un descubrimiento genuino, solo evita
        # relitigar uno ya descartado sin que nada haya cambiado.
        self._exhausted_combo_keys: set = set()
        self._seed_known_formulas_from_wal()

    def _seed_known_formulas_from_wal(self) -> None:
        """
        Mejora #3: repone `known_formulas` con los axiomas ya
        descubiertos y persistidos en SESIONES ANTERIORES (WAL,
        domain=DOMAIN) -- sin esto, cada reinicio del proceso "olvidaría"
        toda fórmula descubierta antes y el espacio de combinación nunca
        crecería de una sesión a la siguiente. Nombre generado desde
        `node_id` para que sea estable y determinista entre reinicios
        (el mismo axioma persistido siempre recupera el mismo nombre).
        """
        wal = getattr(self.orchestrator, "_wal", None)
        if wal is None:
            return
        iter_nodes = getattr(wal, "iter_knowledge_nodes", None)
        if iter_nodes is None:
            return
        try:
            nodes = list(iter_nodes())
        except Exception as exc:
            logger.warning(
                "🧮 [FormulaSynthesizer] no se pudo leer el WAL al arrancar: %s", exc
            )
            return
        for node in nodes:
            if getattr(node, "domain", None) != self.DOMAIN:
                continue
            axiom = node.axiom
            try:
                _parse_known_formula(self._cas, axiom)  # valida que siga parseando
            except Exception:
                continue
            name = self._discovered_formula_name(node.node_id)
            if name not in self.known_formulas:
                self.known_formulas[name] = axiom
                self._discovered_count += 1

    @staticmethod
    def _discovered_formula_name(node_id: str) -> str:
        return f"descubierta_{node_id[:10]}"

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        logger.info("🧮 [FormulaSynthesizer] Bucle de descubrimiento formal en línea.")
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
                logger.error("Error en FormulaSynthesizer: %s", exc)

    def _run_cycle(self) -> None:
        for _ in range(self.MAX_ATTEMPTS_PER_CYCLE):
            if getattr(self.orchestrator, "_is_processing_turn", False):
                return
            proposal = self._ask_proposer()
            if proposal is None:
                continue
            formula_a = proposal.get("formula_a", "")
            formula_b = proposal.get("formula_b", "")
            eliminate = proposal.get("eliminate", "")
            key = (formula_a, formula_b, eliminate)
            if key in self._exhausted_combo_keys:
                continue  # el 0.5B ya propuso esto antes y no dio nada nuevo
            result = derive_and_verify(
                formula_a, formula_b, eliminate,
                known_formulas=self.known_formulas,
                cas=self._cas,
            )
            if result.success:
                self._persist(result)
                return  # un descubrimiento verificado por ciclo alcanza
            # Memoria compartida con `attempt_live_discovery` (mejora #1):
            # este par ya se probó y no valió -- no tiene sentido que ni
            # el ciclo de fondo ni un pedido en vivo del usuario lo
            # vuelvan a intentar mientras la base no cambie.
            self._exhausted_combo_keys.add(key)

    # Fórmulas curadas por nombre real (nunca "descubierta_...", el
    # usuario no puede nombrar esas por su hash): alias de tildes para
    # emparejar tokens del nombre contra texto libre en
    # `extract_named_formula_hints` sin depender de que el usuario haya
    # escrito la tilde correcta (se comparan ya sin acentos igual, este
    # dict es solo documentación de qué tokens existen).
    _NAME_TOKEN_ALIASES: Dict[str, str] = {
        "computo": "cómputo",
        "electrica": "eléctrica",
        "calorifica": "calorífica",
    }

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        """minúsculas + sin acentos, para comparar contra los nombres de
        `KNOWN_FORMULAS` (con guión bajo) sin depender de que el usuario
        haya escrito la tilde correcta."""
        decomposed = unicodedata.normalize("NFKD", text.lower())
        return "".join(ch for ch in decomposed if not unicodedata.combining(ch))

    def extract_named_formula_hints(self, user_input: str) -> "frozenset[str]":
        """
        Bug real (MEDIDO en captura 2026-09-06, "porque pasa?"): un pedido
        que nombra explícitamente dos fórmulas curadas por su dominio
        ("vincula potencia_memoria_limitada con rendimiento_computo...")
        no tenía NINGÚN efecto sobre qué combinación probaba el barrido --
        `attempt_live_discovery` recorre TODA la base (curada + cada
        `descubierta_...` de turnos anteriores) en orden fijo y devuelve
        el PRIMER combo que verifique, sea o no el que el usuario pidió.
        El resultado se envolvía igual en la plantilla fija de "Fórmula
        nueva verificada", dando la impresión falsa de que SÍ se atendió
        el pedido específico.

        Esta función NO decide el resultado -- solo identifica, por
        coincidencia de tokens (sin acentos, orden libre), cuáles de las
        fórmulas CURADAS (nunca las `descubierta_...`, que el usuario no
        puede nombrar por su hash) aparecen mencionadas en el texto del
        pedido. `attempt_live_discovery` las usa para intentar ESAS
        combinaciones primero; si ninguna da resultado, sigue con el
        barrido general y `_synthesize_formula_discovery_response` avisa
        con honestidad que el pedido específico no dio nada nuevo.

        Bug real #2 (MEDIDO, mismo día -- "Descubre una fórmula que
        relacione el ancho de banda de memoria con la potencia dinámica
        CMOS, eliminando la frecuencia..."): la palabra suelta "potencia"
        es TAMBIÉN el nombre de una fórmula curada propia (`potencia`,
        `P = W/t`, mecánica clásica) -- y aparece, inevitablemente, como
        substring literal dentro de "potencia dinámica CMOS". Sin más
        cuidado, `extract_named_formula_hints` matcheaba las TRES:
        `ancho_banda_memoria`, `potencia_dinamica_cmos` Y `potencia`. Como
        `ancho_banda_memoria` y `potencia_dinamica_cmos` NO comparten
        ningún símbolo entre sí (a propósito -- ver el comentario junto a
        `f_m` más arriba: el reloj de memoria y el de cómputo son señales
        físicas DISTINTAS), pero `potencia` SÍ comparte el símbolo `P` con
        `potencia_dinamica_cmos` (ambos lo usan para "potencia", aunque
        una sea mecánica y la otra dinámica-CMOS), el barrido priorizado
        terminaba combinando `potencia` + `potencia_dinamica_cmos` --
        ¡ninguna de las dos siquiera la fórmula de memoria que se pidió!
        -- y como ese par SÍ es un subconjunto válido de los hints (por
        la contaminación de `potencia`), el aviso de "no es lo que
        pediste" ni siquiera se disparaba. Fix: cuando el conjunto de
        tokens de una fórmula matcheada es subconjunto ESTRICTO del de
        otra fórmula TAMBIÉN matcheada (acá: {potencia} ⊂ {potencia,
        dinamica, cmos}), se descarta la más corta -- el nombre compuesto
        más específico es casi siempre a lo que se refería el usuario
        cuando ambos aparecen en el mismo pedido.
        """
        normalized_input = self._normalize_for_match(user_input or "")
        raw_hints: set = set()
        for name in self.known_formulas:
            if name.startswith("descubierta_"):
                continue
            tokens = name.split("_")
            if not tokens:
                continue
            if all(
                self._normalize_for_match(self._NAME_TOKEN_ALIASES.get(tok, tok)) in normalized_input
                for tok in tokens
            ):
                raw_hints.add(name)
        token_sets = {name: frozenset(name.split("_")) for name in raw_hints}
        hints = {
            name
            for name in raw_hints
            if not any(
                other != name and token_sets[name] < token_sets[other]
                for other in raw_hints
            )
        }
        return frozenset(hints)

    def _relevance_scores_for(
        self, names: "list", context_text: str
    ) -> "Dict[str, float]":
        """
        Fase 1 (propuesta técnica 2026-09-07, "guiado semántico en la
        síntesis formal"): puntaje de relevancia [-1, 1] de cada fórmula
        CURADA/descubierta contra `context_text` (el pedido del turno
        actual + un poco de historial reciente -- ver
        `Orchestrator._build_formula_discovery_context_text`), vía
        similitud coseno de embeddings (`embeddings.get_embedding`, el
        mismo modelo local que ya usa el caché semántico y
        `KnowledgeSynthesizer` -- nunca un modelo nuevo que mantener en
        VRAM). El texto que se embebe por fórmula es su nombre con
        guiones bajos cambiados por espacios ("ancho_banda_memoria" ->
        "ancho banda memoria") -- más parecido a lenguaje natural que el
        nombre crudo o la expresión simbólica, para que el modelo de
        embeddings (entrenado en oraciones, no en identificadores de
        código) tenga algo razonable que vectorizar.

        Best-effort y nunca decide qué es verdad -- solo el ORDEN en que
        se prueban combinaciones (`_iter_candidate_combos` de abajo). Si
        el embedding de `context_text` falla o viene vacío, devuelve un
        dict vacío y el llamador cae de vuelta al orden alfabético fijo
        de siempre (mismo comportamiento que antes de este fix cuando no
        hay contexto disponible).
        """
        context_vector = get_embedding(context_text) if context_text else None
        if context_vector is None:
            return {}
        scores: Dict[str, float] = {}
        for name in names:
            name_vector = get_embedding(name.replace("_", " "))
            scores[name] = _cosine(context_vector, name_vector)
        return scores

    def _iter_candidate_combos(
        self,
        preferred_names: "Optional[frozenset]" = None,
        context_text: "Optional[str]" = None,
    ):
        """
        Generador determinista de (formula_a, formula_b, símbolo) sobre
        TODA la base actual (curada + descubierta), salteando los que ya
        están en `_exhausted_combo_keys`. Orden fijo (nombres ordenados
        alfabéticamente, o por relevancia semántica si se pasa
        `context_text` -- ver abajo) para que un barrido repetido sin
        cambios en la base recorra siempre los mismos candidatos en el
        mismo orden -- importante para que el presupuesto de tiempo de
        `attempt_live_discovery` sea predecible entre llamadas.

        `preferred_names`: si el pedido del usuario nombró fórmulas
        curadas concretas (ver `extract_named_formula_hints`), los combos
        entre ELLAS se yieldean primero -- el resto del barrido sigue
        exactamente igual que antes (mismo orden, mismo conjunto total),
        solo se reordena para que lo pedido se intente antes que lo
        genérico dentro del mismo presupuesto de tiempo.

        `context_text` (Fase 1, propuesta 2026-09-07): cuando se pasa, los
        combos del barrido GENERAL (los que no son `preferred_names`, que
        ya van primero por mención EXPLÍCITA -- una señal más fuerte que
        cualquier similitud de embeddings) se ordenan por relevancia
        semántica descendente contra este texto, en vez de alfabético
        puro -- desempate alfabético para mantener el barrido 100%
        determinista entre llamadas con el mismo `context_text`. Mismo
        conjunto total de combos que siempre: esto SOLO reordena, nunca
        agrega ni saca nada del barrido, ni cambia qué se acepta como
        válido (eso lo sigue decidiendo únicamente `derive_and_verify`).
        Sin `context_text` (turno de fondo sin turno de usuario asociado,
        o embeddings no disponibles), el comportamiento es IDÉNTICO al de
        antes de este fix.
        """
        parsed: Dict[str, "sympy.Eq"] = {}
        for name in sorted(self.known_formulas):
            try:
                parsed[name] = _parse_known_formula(self._cas, self.known_formulas[name])
            except Exception:
                continue  # una fórmula que dejó de parsear no participa, no rompe el barrido
        valid_names = sorted(parsed)
        emitted: set = set()

        def _combos_over(names):
            for formula_a, formula_b in itertools.permutations(names, 2):
                shared = parsed[formula_a].free_symbols & parsed[formula_b].free_symbols
                for sym in sorted((str(s) for s in shared)):
                    key = (formula_a, formula_b, sym)
                    if key not in self._exhausted_combo_keys and key not in emitted:
                        emitted.add(key)
                        yield key

        if preferred_names:
            preferred_valid = sorted(set(preferred_names) & set(valid_names))
            if len(preferred_valid) >= 2:
                yield from _combos_over(preferred_valid)

        relevance = self._relevance_scores_for(valid_names, context_text) if context_text else {}
        if not relevance:
            yield from _combos_over(valid_names)
            return

        # Ordena los COMBOS (no solo los nombres) por relevancia
        # combinada de sus dos fórmulas -- "priorizar las ecuaciones que
        # tengan mayor relevancia temática antes de ejecutarlas", tal
        # como pide la propuesta. Materializar la lista completa es
        # seguro: la base tiene decenas de fórmulas, nunca miles.
        general_combos = list(_combos_over(valid_names))

        def _combo_relevance(key):
            formula_a, formula_b, _sym = key
            return (relevance.get(formula_a, 0.0) + relevance.get(formula_b, 0.0)) / 2.0

        general_combos.sort(key=lambda key: (-_combo_relevance(key), key))
        yield from general_combos

    def attempt_live_discovery(
        self,
        preferred_names: "Optional[frozenset]" = None,
        context_text: "Optional[str]" = None,
    ):
        """
        Mejora #1 (2026-09-05, "aplica las mejoras 1 2 3 y 4"): barrido
        DETERMINISTA y SIN LLM sobre toda la base actual, para un pedido
        EN VIVO del usuario ("descubrí una ecuación nueva") -- ver
        `_resolve_formula_discovery_match`/`_synthesize_formula_discovery_
        response` en orchestrator.py. A diferencia de `_run_cycle` (que
        deja que el 0.5B proponga UN par por ciclo de fondo, pensado para
        correr desatendido durante la inactividad), acá no hace falta
        ningún LLM en absoluto: se recorren las combinaciones no
        exploradas todavía, en orden fijo, acotado por TIEMPO (no por
        cantidad) para que el turno del usuario no se cuelgue si la base
        ya creció mucho.

        `preferred_names` (bug real 2026-09-06, ver
        `extract_named_formula_hints`): cuando el pedido nombra fórmulas
        curadas concretas, esas combinaciones se intentan PRIMERO -- el
        conjunto total recorrido y `combos_available` no cambian, solo el
        ORDEN, así que un pedido genérico ("descubrí una fórmula nueva",
        sin nombrar nada) se comporta exactamente igual que antes.

        `context_text` (Fase 1, propuesta técnica 2026-09-07): texto de
        contexto (turno actual + historial reciente) para ordenar por
        relevancia SEMÁNTICA el resto del barrido, el que no cubre
        `preferred_names` -- ver `_iter_candidate_combos`. Mismo
        conjunto total, mismo `combos_available`; solo cambia en qué
        orden se prueban dentro del presupuesto de tiempo.

        Devuelve una tupla `(DerivationResult | None, combos_tried:
        int, combos_available: int)`:
          - El primer resultado VERIFICADO encontrado (ya persistido), o
            None si se agotó el presupuesto de tiempo sin encontrar
            ninguno nuevo.
          - Cuántas combinaciones se llegaron a intentar en esta llamada.
          - Cuántas combinaciones NO exploradas quedaban disponibles al
            empezar (para poder decirle al usuario, con honestidad, si
            el barrido fue exhaustivo o quedó a mitad de camino).
        """
        deadline = time.monotonic() + self.LIVE_DISCOVERY_TIME_BUDGET_SECONDS
        combos_tried = 0
        combos_available = 0
        pending = list(
            self._iter_candidate_combos(
                preferred_names=preferred_names, context_text=context_text
            )
        )
        combos_available = len(pending)
        for formula_a, formula_b, sym in pending:
            if time.monotonic() >= deadline:
                break
            combos_tried += 1
            key = (formula_a, formula_b, sym)
            result = derive_and_verify(
                formula_a, formula_b, sym,
                known_formulas=self.known_formulas, cas=self._cas,
            )
            if result.success:
                self._persist(result)
                return result, combos_tried, combos_available
            self._exhausted_combo_keys.add(key)
        return None, combos_tried, combos_available

    def _build_proposal_prompt(self) -> str:
        # Mejora #3: la base curada original entra ENTERA siempre; de las
        # descubiertas dinámicamente (prefijo "descubierta_") solo se
        # listan las más recientes hasta el tope -- si no, el prompt del
        # proposer crecería sin límite turno tras turno a medida que la
        # base se expande, sin necesidad real (el 0.5B solo tiene que
        # PROPONER un par, no ver el historial completo).
        curated = {
            n: e for n, e in self.known_formulas.items() if not n.startswith("descubierta_")
        }
        discovered = [
            n for n in self.known_formulas if n.startswith("descubierta_")
        ][-self.MAX_DISCOVERED_IN_PROPOSER_PROMPT:]
        visible = dict(curated)
        for n in discovered:
            visible[n] = self.known_formulas[n]
        listing = "\n".join(f"- {name}: {expr}" for name, expr in visible.items())
        return (
            f"Fórmulas conocidas:\n{listing}\n\n"
            "Elegí dos distintas que compartan una variable, y esa variable "
            "a eliminar. Respondé solo el JSON pedido."
        )

    def _ask_proposer(self) -> Optional[Dict[str, str]]:
        orch = self.orchestrator
        try:
            raw = orch._call_llm(
                self._build_proposal_prompt(),
                target_model=getattr(orch, "router_model", None),
                temperature_override=getattr(orch, "ROUTER_LLM_TEMPERATURE", 0.0),
                num_predict_override=self.PROPOSER_NUM_PREDICT,
                system_override=_PROPOSER_SYSTEM_PROMPT,
                perf_label="FormulaSynthesizer-Proposer",
            )
        except Exception as exc:
            logger.debug("🧮 [FormulaSynthesizer] proposer falló: %s", exc)
            return None

        if not raw or raw.lstrip().startswith("[ERROR"):
            return None
        return self._extract_proposal_json(raw)

    @staticmethod
    def _extract_proposal_json(raw: str) -> Optional[Dict[str, str]]:
        """
        Misma estrategia en capas que
        `KnowledgeSynthesizer._extract_verdict_json` (bloque ```json```,
        luego primer '{' a último '}', luego comillas simples/comas
        colgantes) generalizada a las claves de este módulo.
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
                if isinstance(data, dict) and all(
                    k in data for k in ("formula_a", "formula_b", "eliminate")
                ):
                    return {
                        "formula_a": str(data["formula_a"]),
                        "formula_b": str(data["formula_b"]),
                        "eliminate": str(data["eliminate"]),
                    }
        return None

    def _persist(self, result: DerivationResult) -> None:
        orch = self.orchestrator
        axiom = f"{result.new_lhs} = {result.new_rhs}"
        verification: Dict[str, Any] = {
            "formula_a": result.formula_a,
            "formula_a_expr": self.known_formulas.get(result.formula_a),
            "formula_b": result.formula_b,
            "formula_b_expr": self.known_formulas.get(result.formula_b),
            "eliminated_symbol": result.eliminate_symbol,
            "numeric_trials": result.numeric_trials,
            "numeric_trials_passed": result.numeric_trials_passed,
            "detail": result.detail,
        }
        provenance: Dict[str, Any] = {
            "engine": "FormulaSynthesizer",
            "proposed_by": getattr(orch, "router_model", "?"),
            "verified_by": "sympy (CASEngine._parse + sympy.solve/simplify) "
                           "+ verificación numérica adversarial",
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
            logger.warning("🧮 [FormulaSynthesizer] no se pudo persistir en WAL: %s", exc)
            return

        # Mejora #3 ("derivaciones encadenadas"): recién AHORA que quedó
        # durable en el WAL, la fórmula nueva se reinserta en
        # `known_formulas` bajo un nombre generado desde su propio
        # `node_id` (mismo esquema que `_seed_known_formulas_from_wal`,
        # para que el nombre sea estable entre sesiones) -- la próxima
        # ronda de combinaciones (en vivo o de fondo) ya puede usarla como
        # ladrillo nuevo.
        discovered_name = self._discovered_formula_name(node.node_id)
        # `axiom` queda tal cual sale de sympy (notación "**" para
        # potencias) -- se reutiliza sin traducir como ladrillo en
        # `_parse_known_formula`. `CASEngine._parse` ahora atraviesa "**"
        # intacto (ver `CASEngine._collapse_operator_run`); antes había que
        # convertirlo a "^" acá porque el saneador de CASEngine colapsaba
        # "v**2" -> "v*2" en silencio.
        self.known_formulas[discovered_name] = axiom
        self._discovered_count += 1

        memory_graph = getattr(orch, "memory_graph", None)
        if memory_graph is not None:
            with contextlib.suppress(Exception):
                # Confianza fija en 1.0: a diferencia del axioma textual de
                # KnowledgeSynthesizer (un veredicto de juez, con
                # similitudes geométricas como proxy de confianza), esto es
                # una prueba simbólica + numérica — no hay una "confianza
                # parcial" que promediar.
                memory_graph.add_synthetic_knowledge(node.node_id, 1.0)

        self._index_for_retrieval(node)
        logger.info("🧮 [FormulaSynthesizer] fórmula verificada persistida: %s", axiom)

    def _index_for_retrieval(self, node: KnowledgeNode) -> None:
        orch = self.orchestrator
        longterm = getattr(orch, "longterm_vector_rag", None)
        if longterm is None:
            return
        from embeddings import get_embedding  # import diferido: evita ciclo en frío
        vector = get_embedding(node.axiom)
        if vector is None:
            return
        lock = getattr(orch, "_vector_rag_lock", None)
        ctx = lock if lock is not None else contextlib.nullcontext()
        with ctx, contextlib.suppress(Exception):
            longterm.add_documents([node.axiom], [vector], source_id=node.node_id)
