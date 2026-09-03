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
Contrato de eventos entre el motor (Orchestrator) y cualquier capa de
presentación (Qt, CLI, API web, etc). Ningún módulo de UI debe importar
nada de orchestrator.py salvo esto y la clase Orchestrator.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class EventType(Enum):
    STATUS = auto()               # mensaje de progreso humano-legible
    INTENT = auto()                # (icon, message) -> intent_changed en Qt
    LOG = auto()                    # traza de diagnóstico -> log_message en Qt
    ROUTE_DECIDED = auto()
    CACHE_HIT = auto()
    WEB_RESULTS = auto()           # dict crudo de fetch_rich_web_search()
    TOOL_CALL_START = auto()
    TOOL_CALL_RESULT = auto()
    REASONING_TOKEN = auto()       # texto del <thought> ya saneado (Pasada 1)
    TOKEN = auto()                   # (chunk, ast_error) de la respuesta visible
    VERIFICATION = auto()          # {"name": str, "triggered": bool, "detail": Any}
    ERROR = auto()
    DONE = auto()                    # {"trace": TurnTrace|None, "error": str}


@dataclass
class PipelineEvent:
    type: EventType
    payload: Any = None
    meta: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"PipelineEvent({self.type.name}, payload={self.payload!r}, meta={self.meta!r})"