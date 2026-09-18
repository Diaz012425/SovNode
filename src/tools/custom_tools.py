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
custom_tools.py — Motor de herramientas EXTENSIBLE por el usuario, sin tocar Python.
=====================================================================

ES: Un usuario (o el modelo, si se le pide) puede definir una herramienta
nueva escribiendo un `.json` en la carpeta `custom_tools/`, sin editar código.
Este módulo lee esos archivos, los valida estrictamente y registra cada uno
como una tool más sobre LocalToolDispatcher (tools.py): el modelo la ve en
TOOLS_SCHEMA igual que run_cmd o list_dir. Cada custom tool arma un comando y
lo corre a través de ToolSandbox.run_cmd_safely() — la MISMA sandbox que ya
protege run_cmd — más una validación propia y más estricta sobre los valores
sustituidos (ver _SAFE_PARAM_VALUE_RE).

Formato de un archivo custom_tools/<algo>.json:
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

Reglas de validación (un archivo que no las cumple se ignora por completo,
con un warning en el log — nunca tira abajo el arranque de la app):
  - "name": minúsculas/números/guión bajo, empieza con letra; no puede
    repetir el nombre de una herramienta integrada ni de otra custom tool.
  - "command_template": cada `{nombre}` debe tener una entrada
    correspondiente en "parameters", y viceversa (sin sorpresas).
  - Si un valor sustituido puede tener espacios, el placeholder va entre
    comillas en el template — el motor nunca agrega comillas por su cuenta.
  - "timeout_sec": opcional (default 20s), tope duro de 120s.

Por qué los valores son tan restrictivos: el "command_template" lo escribe
una persona de confianza, pero los VALORES que rellenan cada placeholder
pueden venir del modelo. run_cmd_safely() corre en Windows con shell=True
sobre una cadena, así que un valor con `;`, `&`, `|`, `$`, comillas, etc.
podría alterar el comando ejecutado. _SAFE_PARAM_VALUE_RE es un ALLOWLIST: un
valor que no calce con letras/números/espacios y puntuación común de rutas
(`. , : @ / \\ -`) se rechaza de plano, nunca se sanitiza en silencio.

EN: A user (or the model, if asked) can define a new tool by writing a
`.json` file under `custom_tools/`, with no code editing. This module reads
those files, validates them strictly, and registers each one as another tool
on LocalToolDispatcher (tools.py): the model sees it in TOOLS_SCHEMA exactly
like run_cmd or list_dir. Every custom tool builds a command and runs it
through ToolSandbox.run_cmd_safely() — the SAME sandbox that already guards
run_cmd — plus its own stricter validation on the substituted values (see
_SAFE_PARAM_VALUE_RE).

Validation rules (a file that fails any of them is skipped entirely, with a
log warning — never crashes app startup):
  - "name": lowercase/digits/underscore, starts with a letter; can't reuse a
    built-in tool name or another custom tool's name.
  - "command_template": every `{name}` placeholder needs a matching entry in
    "parameters", and vice versa (no surprises).
  - If a substituted value may contain spaces, quote the placeholder in the
    template — the engine never adds quotes on its own.
  - "timeout_sec": optional (default 20s), hard cap of 120s.

Why values are so restrictive: the "command_template" is written by a
trusted person, but the VALUES filling each placeholder can come from the
model. run_cmd_safely() runs on Windows with shell=True over a string, so a
value containing `;`, `&`, `|`, `$`, quotes, etc. could alter the executed
command. _SAFE_PARAM_VALUE_RE is an ALLOWLIST: a value that doesn't match
letters/digits/spaces and common path punctuation (`. , : @ / \\ -`) is
rejected outright, never silently sanitized.
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
    from tools import LocalToolDispatcher

logger = logging.getLogger("SovNode.CustomTools")


_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_SAFE_PARAM_VALUE_RE = re.compile(r"^[\w @.,:/\\\t-]{0,300}$")
_MAX_PARAM_VALUE_LEN = 300

_DEFAULT_TIMEOUT_SEC = 20
_MAX_TIMEOUT_SEC = 120

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


def _default_custom_tools_dir() -> Path:
    """
    ES: En un build congelado (PyInstaller), sys.executable apunta al .exe de
    la app; su carpeta es donde el usuario espera encontrar/crear
    "custom_tools" — nunca la carpeta temporal sys._MEIPASS, que se borra
    entre ejecuciones. Corriendo desde código fuente, se usa la carpeta de
    este propio archivo.
    EN: In a frozen (PyInstaller) build, sys.executable points at the app's
    .exe; its folder is where the user expects to find/create
    "custom_tools" — never the sys._MEIPASS temp extraction folder, which
    gets wiped between runs. Running from source, this file's own folder is used.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "custom_tools"
    return Path(__file__).resolve().parent / "custom_tools"


def _validate_and_build_spec(
    raw: Dict[str, Any], source_file: str, existing_names: "set[str]"
) -> CustomToolSpec:
    """ES: Lanza ValueError con un mensaje claro ante cualquier spec inválido;
    el llamador (load_custom_tool_specs) lo atrapa y saltea el archivo.
    EN: Raises ValueError with a clear message for any invalid spec; the
    caller (load_custom_tool_specs) catches it and skips the file."""
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
    """ES: Lee y valida todos los *.json de directory (default:
    _default_custom_tools_dir()); un directorio ausente da lista vacía sin error.
    EN: Reads and validates every *.json in directory (default:
    _default_custom_tools_dir()); a missing directory returns an empty list, no error."""
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
            logger.warning("[custom_tools] Ignorando '%s': %s", json_path.name, exc)
            continue
        seen_names.add(spec.name)
        specs.append(spec)
    return specs


def validate_param_value(name: str, value: Any) -> str:
    """ES: Convierte value a texto y lo valida contra el allowlist; lanza
    ValueError (nunca silencia ni recorta) si no pasa.
    EN: Converts value to text and validates it against the allowlist;
    raises ValueError (never silently truncates) if it fails."""
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
    """ES: Reemplazo de subcadena literal — deliberadamente nunca
    command_template.format(**values): str.format admite sub-sintaxis de
    acceso a atributos/índices sobre el valor, y los valores acá los elige
    el modelo. .replace() por placeholder no interpreta nada de eso.
    EN: Literal substring replacement — deliberately never
    command_template.format(**values): str.format's mini-language allows
    attribute/index access sub-syntax on the value, and these values are
    ultimately chosen by the model. .replace() per placeholder interprets none of that."""
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
        return dispatcher.sandbox.run_cmd_safely(command, timeout_sec=spec.timeout_sec)

    _custom_tool.__name__ = f"custom_tool_{spec.name}"
    _custom_tool.__doc__ = spec.description
    return _custom_tool


def _make_schema_dict(spec: CustomToolSpec) -> Dict[str, Any]:
    """ES: Mismo formato que las entradas ya existentes en TOOLS_SCHEMA (tools.py).
    EN: Same shape as the existing entries in TOOLS_SCHEMA (tools.py)."""
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
    ES: Carga las custom tools válidas de directory y las registra sobre
    dispatcher — mismo register_tool() que las herramientas integradas, y
    agrega cada schema en el lugar (.append(), nunca reasignación) a
    tools.TOOLS_SCHEMA, la misma lista que orchestrator.py ya embebe en el
    prompt. Pensado para llamarse una vez desde LocalToolDispatcher.__init__();
    existing_schema_names evita duplicar el mismo schema si un segundo
    dispatcher se crea en el mismo proceso (TOOLS_SCHEMA es global). Devuelve
    la cantidad registrada en ESTE dispatcher. Nunca lanza.
    EN: Loads the valid custom tools from directory and registers them on
    dispatcher — the same register_tool() the built-ins use, appending each
    schema in place (.append(), never reassignment) to tools.TOOLS_SCHEMA,
    the same list orchestrator.py already embeds in the prompt. Meant to be
    called once from LocalToolDispatcher.__init__(); existing_schema_names
    prevents duplicating the same schema if a second dispatcher is created
    in the same process (TOOLS_SCHEMA is a module global). Returns the count
    registered on THIS dispatcher. Never raises.
    """
    from tools import TOOLS_SCHEMA

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
