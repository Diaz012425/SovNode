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
class NodeContextManager:
    """ES: Gestiona la ventana de contexto y poda el historial para evitar saturación de memoria.
    EN: Manages the context window and prunes conversation history to avoid memory bloat."""
    def __init__(self, max_turns: int = 10):
        self.max_turns = max_turns
        self.history = []

    def add_turn(self, role: str, content: str):
        """ES: Agrega un turno al historial; si excede el límite, poda los más viejos conservando
        siempre el system prompt (índice 0) y los últimos N turnos.
        EN: Appends a turn to history; if it exceeds the limit, prunes the oldest ones while
        always keeping the system prompt (index 0) and the last N turns."""
        self.history.append({"role": role, "content": content})
        if len(self.history) > self.max_turns * 2:
            self.history = [self.history[0]] + self.history[-(self.max_turns * 2):]

    def get_context_payload(self) -> str:
        """ES: Serializa el historial completo en un único bloque de texto plano.
        EN: Serializes the full history into a single plain-text block."""
        return "\n".join([f"{h['role']}: {h['content']}" for h in self.history])
