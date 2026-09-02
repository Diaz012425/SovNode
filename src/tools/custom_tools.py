"""
custom_tools.py — Motor de herramientas EXTENSIBLE por el usuario, sin
tocar Python.
=====================================================================

Qué es esto
-----------
Un usuario (o el propio modelo, si se le pide) puede definir una
herramienta nueva escribiendo un archivo `.json` dentro de la carpeta
`custom_tools/` (al lado de la app) — sin editar ni una línea de código.
Este módulo lee esos archivos, los valida estrictamente y registra cada
uno como una tool más sobre el `LocalToolDispatcher` de tools.py: el
modelo la ve en `TOOLS_SCHEMA` exactamente igual que `run_cmd` o
`list_dir`, y puede invocarla con el mismo protocolo de tool-calling.

Deliberadamente NO se inventa un camino de ejecución nuevo: cada
custom tool, en el fondo, arma un comando y lo corre a través de
`ToolSandbox.run_cmd_safely()` (tools.py) — la MISMA sandbox, con los
mismos `BLOCKED_COMMANDS`/`DANGEROUS_PATTERNS`/timeout/truncado que ya
protegen `run_cmd`. Este módulo agrega una capa de validación PROPIA y
más estricta sobre los VALORES sustituidos (ver `_SAFE_PARAM_VALUE_RE`
más abajo) — run_cmd_safely es una segunda línea de defensa, no la única.

Formato de un archivo custom_tools/<algo>.json
-----------------------------------------------
    {
      "name": "git_status",
      "description": "Descripción en lenguaje natural — esto es lo que",
      "command_template": "git -C \\"{repo_path}\\" status --short",
      "parameters": {
        "repo_path": {
          "description": "Ruta a la carpeta del repositorio git.",
          "required": true
        }
      },
      "timeout_sec": 15
    }

Reglas de validación (un archivo que no las cumple se IGNORA por
completo, con un warning en el log — nunca tira abajo el arranque de la
app):

  - "name": minúsculas/números/guión bajo, debe empezar con una letra
    (regex `_VALID_NAME_RE`). No puede repetir el nombre de una
    herramienta integrada (run_cmd, read_file, write_file, list_dir,
    system_telemetry) ni el de otra custom tool ya cargada.
  - "command_template": cualquier texto; cada `{nombre}` dentro de él es
    un placeholder que DEBE tener una entrada correspondiente en
    "parameters", y viceversa — no se permite un parámetro declarado que
    no se usa, ni un placeholder sin declarar (evita sorpresas).
  - Si un valor sustituido podría contener espacios (una ruta, por
    ejemplo), envolvé el placeholder en comillas en el TEMPLATE, como en
    el ejemplo de arriba — el motor nunca agrega comillas por su cuenta.
  - "timeout_sec": opcional (default 20s), tope duro de 120s.
  - Cada entrada de "parameters" acepta "description" (string) y
    "required" (bool, default true). Todo parámetro se trata como texto.

Por qué los valores son tan restrictivos
-----------------------------------------
El "command_template" lo escribe una persona de confianza (vos, a mano,
en tu propio disco) — pero los VALORES que rellenan cada `{placeholder}`
en tiempo real pueden venir del modelo local (una tool call que él arma).
`ToolSandbox.run_cmd_safely()` en Windows corre el comando con
`shell=True` sobre una cadena — así que un valor con `;`, `&`, `|`, `$`,
comillas, etc. podría alterar el comando ejecutado más allá de lo que el
template pretendía. `_SAFE_PARAM_VALUE_RE` es un ALLOWLIST (no un
blocklist): un valor que no calce exactamente con letras/números/espacios
y un puñado de signos de puntuación comunes en rutas y nombres
(`. , : @ / \\ -`) se RECHAZA de plano, con un error explícito devuelto
al modelo — nunca se sanitiza en silencio ni se intenta "arreglar".
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # Solo para chequeo de tipos - ver el docstring de _register_custom_
    # tools() en tools.py sobre por qué esto nunca se importa en tiempo
    # de ejecución al nivel de módulo (evitaría el import circular).
    from tools import LocalToolDispatcher

logger = logging.getLogger("SovNode.CustomTools")


# =====================================================================
# Constantes de validación
# =====================================================================
_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

#: Allowlist de caracteres permitidos en un VALOR de parámetro ya
#: sustituido. Ver "Por qué los valores son tan restrictivos" arriba.
#: Nota: \w es unicode-aware en Python 3 (cubre letras acentuadas/ñ),
#: y deliberadamente NO se usa \s (que incluiría \n/\r) - solo espacio
#: y tab explícitos.
_SAFE_PARAM_VALUE_RE = re.compile(r"^[\w @.,:/\\\t-]{0,300}$")
_MAX_PARAM_VALUE_LEN = 300

_DEFAULT_TIMEOUT_SEC = 20
_MAX_TIMEOUT_SEC = 120

#: Nombres de las herramientas integradas de _register_default_tools()
#: (tools.py) - una custom tool no puede reusar ninguno de estos. Esto es
#: solo la VALIDACIÓN TEMPRANA (a nivel de archivo individual, antes de
#: tener un dispatcher a mano); register_custom_tools() además valida en
#: caliente contra dispatcher.has_tool() como defensa en profundidad, por
#: si esta lista queda desactualizada en el futuro.
_RESERVED_TOOL_NAMES = {"run_cmd", "read_file", "write_file", "list_dir", "system_telemetry"}


@dataclass(frozen=True)
class CustomToolParam:
    description: str
    required: bool = True


@dataclass(frozen=True)
class CustomToolSpec:
    name: str
    description: str
    command_template: str
    parameters: Dict[str, CustomToolParam] = field(default_factory=dict)
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC
    source_file: str = "<desconocido>"


# =====================================================================
# Ubicación por defecto de custom_tools/
# =====================================================================
def _default_custom_tools_dir() -> Path:
    """
    BLINDAJE (mismo motivo ya documentado en dynamic_tool_engine.py,
    _resolve_python_executable): en un build congelado (PyInstaller —
    este proyecto tiene SovNode.spec/sovnode_qt.build/.dist),
    `sys.executable` apunta al propio .exe de la app, y su carpeta es
    donde un usuario esperaría encontrar o crear "custom_tools" al lado
    del ejecutable — NUNCA la carpeta temporal de extracción de
    `sys._MEIPASS`, que se borra entre ejecuciones y jamás debe usarse
    para algo que el usuario edita a mano y espera que persista.
    Corriendo desde código fuente (el caso normal durante desarrollo),
    se usa la carpeta de este propio archivo.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "custom_tools"
    return Path(__file__).resolve().parent / "custom_tools"


# =====================================================================
# Carga y validación de specs
# =====================================================================
def _validate_and_build_spec(
    raw: Dict[str, Any], source_file: str, existing_names: "set[str]"
) -> CustomToolSpec:
    """
    Lanza ValueError con un mensaje claro ante cualquier spec inválido.
    El llamador (load_custom_tool_specs) atrapa esto y SALTEA el
    archivo — un JSON mal escrito en custom_tools/ nunca debe tirar
    abajo el arranque de la app.
    """
    name = raw.get("name")
    if not isinstance(name, str) or not _VALID_NAME_RE.match(name):
        raise ValueError(
            f"'name' inválido o ausente (minúsculas/números/guión bajo, "
            f"debe empezar con una letra): {name!r}"
        )
    if name in _RESERVED_TOOL_NAMES:
        raise ValueError(f"'{name}' ya es el nombre de una herramienta integrada.")
    if name in existing_names:
        raise ValueError(
            f"'{name}' ya fue registrado por otro archivo de custom_tools/ "
            f"— los nombres deben ser únicos."
        )

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("'description' inválida o ausente.")

    command_template = raw.get("command_template")
    if not isinstance(command_template, str) or not command_template.strip():
        raise ValueError("'command_template' inválido o ausente.")

    params_raw = raw.get("parameters", {})
    if not isinstance(params_raw, dict):
        raise ValueError("'parameters' debe ser un objeto (puede estar vacío).")

    parameters: Dict[str, CustomToolParam] = {}
    for pname, pspec in params_raw.items():
        if not _VALID_NAME_RE.match(pname):
            raise ValueError(f"nombre de parámetro inválido: {pname!r}")
        if not isinstance(pspec, dict):
            raise ValueError(f"la especificación del parámetro '{pname}' debe ser un objeto.")
        parameters[pname] = CustomToolParam(
            description=str(pspec.get("description", "")),
            required=bool(pspec.get("required", True)),
        )

    # Nota: los placeholders del template y los parámetros declarados
    # deben coincidir EXACTAMENTE. Un parámetro declarado sin placeholder
    # es casi siempre un error de tipeo (¿el template usa otro nombre?);
    # un placeholder sin declarar quedaría SIN VALIDAR en tiempo de
    # ejecución si alguna vez se permitiera - más vale rechazar el
    # archivo entero en la carga que arriesgar esa laguna.
    placeholders = set(_PLACEHOLDER_RE.findall(command_template))
    declared = set(parameters.keys())
    if placeholders != declared:
        detalle = []
        faltantes = declared - placeholders
        sobrantes = placeholders - declared
        if faltantes:
            detalle.append(f"declarados sin usar en el template: {sorted(faltantes)}")
        if sobrantes:
            detalle.append(f"placeholders sin declarar como parámetro: {sorted(sobrantes)}")
        raise ValueError(
            "los parámetros declarados y los placeholders de 'command_template' "
            "no coinciden — " + "; ".join(detalle)
        )

    timeout_sec = raw.get("timeout_sec", _DEFAULT_TIMEOUT_SEC)
    if not isinstance(timeout_sec, (int, float)) or isinstance(timeout_sec, bool) or not (0 < timeout_sec <= _MAX_TIMEOUT_SEC):
        raise ValueError(f"'timeout_sec' debe ser un número entre 0 y {_MAX_TIMEOUT_SEC}: {timeout_sec!r}")

    return CustomToolSpec(
        name=name,
        description=description.strip(),
        command_template=command_template,
        parameters=parameters,
        timeout_sec=int(timeout_sec),
        source_file=source_file,
    )


def load_custom_tool_specs(directory: Optional[Path] = None) -> List[CustomToolSpec]:
    """
    Lee y valida todos los `*.json` de `directory` (default:
    _default_custom_tools_dir()). Un directorio ausente devuelve lista
    vacía sin error — es el estado normal para un usuario que todavía no
    definió ninguna custom tool.
    """
    directory = directory or _default_custom_tools_dir()
    if not directory.is_dir():
        return []

    specs: List[CustomToolSpec] = []
    seen_names: set = set()
    for json_path in sorted(directory.glob("*.json")):
        try:
            with json_path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("el archivo debe contener un objeto JSON en la raíz.")
            spec = _validate_and_build_spec(raw, source_file=json_path.name, existing_names=seen_names)
        except Exception as exc:
            # Nota: un JSON mal escrito en custom_tools/ nunca debe
            # impedir que el resto de la app arranque - mismo espíritu
            # que clear_conversation_memory() en orchestrator.py, que
            # explícitamente prefiere seguir funcionando ante un fallo
            # no crítico antes que interrumpir al usuario.
            logger.warning("[custom_tools] Ignorando '%s': %s", json_path.name, exc)
            continue
        seen_names.add(spec.name)
        specs.append(spec)
    return specs


# =====================================================================
# Sustitución segura y ejecución
# =====================================================================
def validate_param_value(name: str, value: Any) -> str:
    """
    Convierte `value` a texto y lo valida contra el allowlist. Lanza
    ValueError (nunca silencia ni recorta) si no pasa — ver "Por qué los
    valores son tan restrictivos" en el docstring del módulo.
    """
    text = str(value)
    if len(text) > _MAX_PARAM_VALUE_LEN:
        raise ValueError(
            f"el parámetro '{name}' excede el largo máximo permitido "
            f"({_MAX_PARAM_VALUE_LEN} caracteres)."
        )
    if not _SAFE_PARAM_VALUE_RE.match(text):
        raise ValueError(
            f"el parámetro '{name}' contiene caracteres no permitidos "
            f"(solo se aceptan letras, números, espacios y . , : @ / \\ -)."
        )
    return text


def _substitute_command(spec: CustomToolSpec, values: Dict[str, str]) -> str:
    """
    Reemplazo de subcadena LITERAL — deliberadamente NUNCA
    `command_template.format(**values)`: el mini-lenguaje de `str.format`
    admite sub-sintaxis de acceso a atributos/índices sobre el propio
    valor (p. ej. `{0.__class__...}`), y los valores acá, en última
    instancia, los elige el modelo al armar la tool call. Un `.replace()`
    por cada placeholder no interpreta nada de eso — es sustitución de
    texto, punto.
    """
    command = spec.command_template
    for pname, value in values.items():
        command = command.replace("{" + pname + "}", value)
    return command


def _make_tool_function(dispatcher: "LocalToolDispatcher", spec: CustomToolSpec) -> Callable[..., str]:
    def _custom_tool(**kwargs: Any) -> str:
        values: Dict[str, str] = {}
        for pname, pspec in spec.parameters.items():
            raw_value = kwargs.get(pname)
            if raw_value is None:
                if pspec.required:
                    return f"[CUSTOM TOOL ERROR] ({spec.name}): falta el parámetro requerido '{pname}'."
                values[pname] = ""
                continue
            try:
                values[pname] = validate_param_value(pname, raw_value)
            except ValueError as exc:
                return f"[CUSTOM TOOL ERROR] ({spec.name}): {exc}"

        command = _substitute_command(spec, values)
        # Único camino de ejecución real: la misma sandbox que run_cmd,
        # con sus mismas defensas (BLOCKED_COMMANDS/DANGEROUS_PATTERNS/
        # timeout/truncado) corriendo sobre el comando YA sustituido.
        return dispatcher.sandbox.run_cmd_safely(command, timeout_sec=spec.timeout_sec)

    _custom_tool.__name__ = f"custom_tool_{spec.name}"
    _custom_tool.__doc__ = spec.description
    return _custom_tool


def _make_schema_dict(spec: CustomToolSpec) -> Dict[str, Any]:
    """Mismo formato que las 5 entradas ya existentes en TOOLS_SCHEMA (tools.py)."""
    properties = {
        pname: {"type": "string", "description": pspec.description or pname}
        for pname, pspec in spec.parameters.items()
    }
    required = [pname for pname, pspec in spec.parameters.items() if pspec.required]
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def register_custom_tools(dispatcher: "LocalToolDispatcher", directory: Optional[Path] = None) -> int:
    """
    Carga las custom tools válidas de `directory` y las registra sobre
    `dispatcher` — mismo `register_tool()` que usan las herramientas
    integradas, y agrega cada schema EN EL LUGAR (`.append()`, nunca
    reasignación) a `tools.TOOLS_SCHEMA`, la misma lista que
    orchestrator.py ya importa y embebe en el prompt — así ambos puntos
    de embebido existentes ven las custom tools automáticamente, sin
    tocar orchestrator.py.

    Pensado para llamarse UNA VEZ desde LocalToolDispatcher.__init__() —
    en el uso normal de la app (un solo Orchestrator/LocalToolDispatcher
    por proceso, ver sovnode_qt.py) alcanza y sobra. Pero como TOOLS_
    SCHEMA es un módulo global compartido, un segundo dispatcher creado
    en el MISMO proceso (por ejemplo en un test, o una futura función de
    "recargar herramientas") no debe volver a `.append()` el mismo
    schema — quedaría duplicado en el prompt del modelo, cada vez que se
    construya un dispatcher nuevo. `existing_schema_names` hace esa
    parte idempotente: cada dispatcher SÍ registra su propia función
    ejecutable (`dispatcher.register_tool`, siempre necesario porque
    cada instancia tiene su propio diccionario `_tools`), pero el
    `.append()` a la lista compartida ocurre como máximo una vez por
    nombre en la vida del proceso.

    Devuelve la cantidad efectivamente registrada en ESTE dispatcher.
    Nunca lanza: ver BLINDAJE de _register_custom_tools() en tools.py.
    """
    from tools import TOOLS_SCHEMA  # import diferido, ver docstring del módulo

    existing_schema_names = {t.get("name") for t in TOOLS_SCHEMA if isinstance(t, dict)}

    specs = load_custom_tool_specs(directory)
    registered = 0
    for spec in specs:
        if dispatcher.has_tool(spec.name):
            logger.warning(
                "[custom_tools] Ignorando '%s' (de %s): el nombre ya está en uso "
                "por otra herramienta ya registrada.",
                spec.name, spec.source_file,
            )
            continue
        dispatcher.register_tool(spec.name, _make_tool_function(dispatcher, spec))
        if spec.name not in existing_schema_names:
            TOOLS_SCHEMA.append(_make_schema_dict(spec))
            existing_schema_names.add(spec.name)
        logger.info("[custom_tools] Herramienta registrada: '%s' (%s)", spec.name, spec.source_file)
        registered += 1
    return registered
