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
import html
from datetime import datetime

TERMINAL_COLORS = {
    "info": "#4C8BF5",
    "ok": "#3DDC97",
    "warn": "#F2C14E",
    "error": "#FF6B6B",
    "system": "#7C9CFF",
}

def format_terminal_log(message: str, level: str = "info") -> str:
    """ES: Genera una línea HTML con timestamp y color para el QTextEdit de la terminal.
    EN: Builds an HTML log line with timestamp and color for the terminal's QTextEdit."""
    color = TERMINAL_COLORS.get(level, "#3DDC97")
    timestamp = datetime.now().strftime("%H:%M:%S")
    safe_message = html.escape(message)
    return (
        f'<span style="color:#4B5563;">[{timestamp}]</span> '
        f'<span style="color:{color};">{safe_message}</span>'
    )
