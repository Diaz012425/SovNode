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
    STATUS = auto()
    INTENT = auto()
    LOG = auto()
    ROUTE_DECIDED = auto()
    CACHE_HIT = auto()
    WEB_RESULTS = auto()
    TOOL_CALL_START = auto()
    TOOL_CALL_RESULT = auto()
    REASONING_TOKEN = auto()
    TOKEN = auto()
    VERIFICATION = auto()
    ERROR = auto()
    DONE = auto()


@dataclass
class PipelineEvent:
    """ES: Un evento individual del pipeline, con su tipo, payload y metadatos opcionales.
    EN: A single pipeline event, with its type, payload, and optional metadata."""
    type: EventType
    payload: Any = None
    meta: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"PipelineEvent({self.type.name}, payload={self.payload!r}, meta={self.meta!r})"
