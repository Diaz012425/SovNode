import html
from datetime import datetime

# Paleta de colores para la consola interna de SovNode
TERMINAL_COLORS = {
    "info": "#4C8BF5",
    "ok": "#3DDC97",
    "warn": "#F2C14E",
    "error": "#FF6B6B",
    "system": "#7C9CFF",
}

def format_terminal_log(message: str, level: str = "info") -> str:
    """Genera una línea HTML formateada con timestamp y color para el QTextEdit de la terminal."""
    color = TERMINAL_COLORS.get(level, "#3DDC97")
    timestamp = datetime.now().strftime("%H:%M:%S")
    safe_message = html.escape(message)
    return (
        f'<span style="color:#4B5563;">[{timestamp}]</span> '
        f'<span style="color:{color};">{safe_message}</span>'
    )