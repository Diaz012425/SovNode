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
from __future__ import annotations
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QScrollArea,
    QTextBrowser,
    QTextEdit,
    QWidget,
)

# ES: Diccionario de temas visuales (paleta de colores por tema).
# EN: Visual theme dictionary (color palette per theme).
THEMES = {
    "Cyberpunk Dark": {
        "bg": "#0E1117", "sidebar": "#14171F", "card": "#171B24", "input": "#1B1F2A",
        "assistant": "#1E222C", "user": "#1E3A8A", "accent": "#4C8BF5", "accent_soft": "#1E2A44",
        "text": "#E6E8EC", "secondary": "#8B92A5", "border": "#262B36", "success": "#3DDC97",
        "warning": "#F2C14E", "danger": "#F2555A", "code": "#10141D", "coder_model": "#F2C14E",
        "general_model": "#4C8BF5",
    },
    "OLED Pure Black": {
        "bg": "#000000", "sidebar": "#080808", "card": "#101010", "input": "#151515",
        "assistant": "#171717", "user": "#312E81", "accent": "#7C9CFF", "accent_soft": "#1D2450",
        "text": "#FFFFFF", "secondary": "#B4B4B4", "border": "#303030", "success": "#5AFFAA",
        "warning": "#FFD166", "danger": "#FF6B6B", "code": "#070707", "coder_model": "#FFD166",
        "general_model": "#7C9CFF",
    },
    "Nordic Slate": {
        "bg": "#2E3440", "sidebar": "#252B36", "card": "#3B4252", "input": "#434C5E",
        "assistant": "#3B4252", "user": "#4C566A", "accent": "#88C0D0", "accent_soft": "#3D5167",
        "text": "#ECEFF4", "secondary": "#D8DEE9", "border": "#4C566A", "success": "#A3BE8C",
        "warning": "#EBCB8B", "danger": "#BF616A", "code": "#272D38", "coder_model": "#EBCB8B",
        "general_model": "#88C0D0",
    },
}

class PromptTextEdit(QTextEdit):
    """ES: Caja de texto del prompt: Enter envía el mensaje, Shift+Enter agrega una línea nueva.
    EN: Prompt text box: Enter sends the message, Shift+Enter inserts a newline."""
    send_requested = pyqtSignal()
    file_dropped = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                event.accept()
                self.send_requested.emit()
            return
        super().keyPressEvent(event)

class ChatDropArea(QScrollArea):
    """ES: Área de scroll del chat que acepta archivos arrastrados (drag & drop).
    EN: Chat scroll area that accepts drag-and-drop file drops."""
    files_dropped = pyqtSignal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

class AutoResizingTextBrowser(QTextBrowser):
    """ES: QTextBrowser sin scrollbars propias que ajusta su alto al contenido renderizado.
    EN: QTextBrowser with no scrollbars of its own that resizes its height to fit rendered content."""
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(0)
        self.setStyleSheet("background: transparent; border: none; color: #FFFFFF; font-size: 14px;")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update_height()

    def update_height(self) -> None:
        """ES: Recalcula el alto fijo del widget según el tamaño del documento renderizado.
        EN: Recalculates the widget's fixed height based on the rendered document size."""
        if self.document():
            w = self.viewport().width()
            if w > 0:
                self.document().setTextWidth(w)
            doc_height = int(self.document().size().height())
            self.setFixedHeight(max(18, doc_height + 6))
