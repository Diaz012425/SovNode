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