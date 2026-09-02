class NodeContextManager:
    """Gestiona la ventana de contexto y la poda de memoria para evitar saturación."""
    def __init__(self, max_turns: int = 10):
        self.max_turns = max_turns
        self.history = []

    def add_turn(self, role: str, content: str):
        self.history.append({"role": role, "content": content})
        # Poda inteligente: mantenemos los turnos recientes
        if len(self.history) > self.max_turns * 2:
            # Conservamos siempre el system prompt (index 0) y los últimos N turnos
            self.history = [self.history[0]] + self.history[-(self.max_turns * 2):]

    def get_context_payload(self) -> str:
        return "\n".join([f"{h['role']}: {h['content']}" for h in self.history])