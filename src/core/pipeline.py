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
pipeline.py
ES: Contrato de eventos entre el motor (Orchestrator) y cualquier capa de
presentación (Qt, CLI, API web, etc). Ningún módulo de UI debe importar
nada de orchestrator.py salvo esto y la clase Orchestrator.
EN: Event contract between the engine (Orchestrator) and any presentation
layer (Qt, CLI, web API, etc). No UI module should import anything from
orchestrator.py except this and the Orchestrator class.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class EventType(Enum):
    STATUS = auto()                # ES: mensaje de progreso legible / EN: human-readable progress message
    INTENT = auto()                # ES: (icon, message) -> intent_changed en Qt / EN: (icon, message) -> Qt's intent_changed
    LOG = auto()                   # ES: traza de diagnóstico -> log_message en Qt / EN: diagnostic trace -> Qt's log_message
    ROUTE_DECIDED = auto()
    CACHE_HIT = auto()
    WEB_RESULTS = auto()           # ES: dict crudo de fetch_rich_web_search() / EN: raw dict from fetch_rich_web_search()
    TOOL_CALL_START = auto()
    TOOL_CALL_RESULT = auto()
    REASONING_TOKEN = auto()       # ES: texto de <thought> ya saneado (Pasada 1) / EN: sanitized <thought> text (Pass 1)
    TOKEN = auto()                 # ES: (chunk, ast_error) de la respuesta visible / EN: (chunk, ast_error) of the visible response
    VERIFICATION = auto()          # {"name": str, "triggered": bool, "detail": Any}
    ERROR = auto()
    DONE = auto()                  # ES: {"trace": TurnTrace|None, "error": str} / EN: same shape


@dataclass
class PipelineEvent:
    """ES: Un evento individual del pipeline, con su tipo, payload y metadatos opcionales.
    EN: A single pipeline event, with its type, payload, and optional metadata."""
    type: EventType
    payload: Any = None
    meta: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"PipelineEvent({self.type.name}, payload={self.payload!r}, meta={self.meta!r})"
