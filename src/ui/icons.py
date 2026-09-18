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
icons.py — Iconografía vectorial propia de SovNode (sin dependencias nuevas).

ES: Dibuja un set pequeño de iconos de línea en tiempo de ejecución con QPainter
puro (QPainterPath + QPen), sin QtSvg ni archivos de recurso — así cada ícono
sigue automáticamente el color de acento del tema activo, sin generar una
copia estática por tema.
EN: Draws a small set of line icons at runtime with pure QPainter
(QPainterPath + QPen), no QtSvg or resource files — so each icon automatically
follows the active theme's accent color, without generating a static copy per theme.

Uso:
    from icons import icon
    boton.setIcon(icon("gear", theme["secondary"]))
    boton.setIconSize(QSize(18, 18))

`icon()` cachea por (kind, color, size) — construir el mismo ícono varias
veces no repite el trabajo de dibujo.
"""

from __future__ import annotations

from typing import Callable, Dict, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

__all__ = ["icon", "ICON_KINDS"]

_STROKE_FRACTION = 0.09


def _pen(color: str, size: int) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.2, size * _STROKE_FRACTION))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _pt(rect: QRectF, x: float, y: float) -> QPointF:
    """ES: Punto normalizado (0..1, 0..1) -> coordenadas reales dentro de `rect`.
    EN: Normalized point (0..1, 0..1) -> real coordinates within `rect`."""
    return QPointF(rect.x() + x * rect.width(), rect.y() + y * rect.height())


def _draw_gear(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Engranaje (Config): anillo + 8 dientes + eje central. / EN: Gear (Settings): ring + 8 teeth + center hub."""
    p.setPen(_pen(color, size))
    cx, cy = r.center().x(), r.center().y()
    radius = r.width() * 0.30
    p.drawEllipse(QPointF(cx, cy), radius, radius)
    for i in range(8):
        import math

        ang = math.radians(i * 45)
        x1 = cx + radius * 0.92 * math.cos(ang)
        y1 = cy + radius * 0.92 * math.sin(ang)
        x2 = cx + radius * 1.34 * math.cos(ang)
        y2 = cy + radius * 1.34 * math.sin(ang)
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    hole = radius * 0.38
    p.drawEllipse(QPointF(cx, cy), hole, hole)


def _draw_mic(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Micrófono: cápsula + soporte en U + pie. / EN: Microphone: capsule + U-shaped cradle + stand."""
    pen = _pen(color, size)
    p.setPen(pen)
    body = QRectF(_pt(r, 0.37, 0.10), _pt(r, 0.63, 0.54))
    p.drawRoundedRect(body, body.width() * 0.5, body.width() * 0.5)
    cradle = QRectF(_pt(r, 0.24, 0.34), _pt(r, 0.76, 0.78))
    p.drawArc(cradle, 200 * 16, 140 * 16)
    p.drawLine(_pt(r, 0.5, 0.72), _pt(r, 0.5, 0.88))
    p.drawLine(_pt(r, 0.34, 0.88), _pt(r, 0.66, 0.88))


def _draw_speaker(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Altavoz (modo voz/TTS): caja + cono + 2 ondas de sonido. / EN: Speaker (voice/TTS mode): box + cone + 2 sound waves."""
    pen = _pen(color, size)
    p.setPen(pen)
    p.setBrush(QColor(color))
    box = QRectF(_pt(r, 0.12, 0.38), _pt(r, 0.34, 0.62))
    p.drawRect(box)
    cone = QPainterPath()
    cone.moveTo(_pt(r, 0.34, 0.38))
    cone.lineTo(_pt(r, 0.58, 0.16))
    cone.lineTo(_pt(r, 0.58, 0.84))
    cone.lineTo(_pt(r, 0.34, 0.62))
    cone.closeSubpath()
    p.drawPath(cone)
    p.setBrush(Qt.BrushStyle.NoBrush)
    wave_near = QRectF(_pt(r, 0.60, 0.32), _pt(r, 0.76, 0.68))
    p.drawArc(wave_near, -55 * 16, 110 * 16)
    wave_far = QRectF(_pt(r, 0.62, 0.18), _pt(r, 0.90, 0.82))
    p.drawArc(wave_far, -50 * 16, 100 * 16)


def _draw_globe(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Globo: círculo + meridiano + 2 paralelos. / EN: Globe: circle + meridian + 2 parallels."""
    p.setPen(_pen(color, size))
    circle = QRectF(_pt(r, 0.12, 0.12), _pt(r, 0.88, 0.88))
    p.drawEllipse(circle)
    p.drawLine(_pt(r, 0.5, 0.12), _pt(r, 0.5, 0.88))
    p.drawLine(_pt(r, 0.13, 0.5), _pt(r, 0.87, 0.5))
    p.drawArc(circle, 20 * 16, 140 * 16)
    p.drawArc(circle, 200 * 16, 140 * 16)


def _draw_terminal(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Terminal/consola: marco redondeado + prompt ">" + cursor. / EN: Terminal/console: rounded frame + ">" prompt + cursor."""
    p.setPen(_pen(color, size))
    frame = QRectF(_pt(r, 0.10, 0.16), _pt(r, 0.90, 0.84))
    p.drawRoundedRect(frame, frame.width() * 0.10, frame.width() * 0.10)
    path = QPainterPath()
    path.moveTo(_pt(r, 0.24, 0.38))
    path.lineTo(_pt(r, 0.38, 0.5))
    path.lineTo(_pt(r, 0.24, 0.62))
    p.drawPath(path)
    p.drawLine(_pt(r, 0.44, 0.62), _pt(r, 0.62, 0.62))


def _draw_stop(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Detener: cuadrado sólido redondeado. / EN: Stop: solid rounded square."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    box = QRectF(_pt(r, 0.28, 0.28), _pt(r, 0.72, 0.72))
    p.drawRoundedRect(box, box.width() * 0.16, box.width() * 0.16)


def _draw_download(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Descargar: flecha hacia abajo + bandeja. / EN: Download: downward arrow + tray."""
    p.setPen(_pen(color, size))
    p.drawLine(_pt(r, 0.5, 0.14), _pt(r, 0.5, 0.56))
    path = QPainterPath()
    path.moveTo(_pt(r, 0.34, 0.42))
    path.lineTo(_pt(r, 0.5, 0.60))
    path.lineTo(_pt(r, 0.66, 0.42))
    p.drawPath(path)
    p.drawLine(_pt(r, 0.20, 0.80), _pt(r, 0.80, 0.80))
    p.drawLine(_pt(r, 0.20, 0.80), _pt(r, 0.20, 0.68))
    p.drawLine(_pt(r, 0.80, 0.80), _pt(r, 0.80, 0.68))


def _draw_save(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Guardar/exportar: disquete simplificado. / EN: Save/export: simplified floppy disk."""
    p.setPen(_pen(color, size))
    body = QRectF(_pt(r, 0.18, 0.14), _pt(r, 0.82, 0.86))
    p.drawRoundedRect(body, body.width() * 0.09, body.width() * 0.09)
    p.drawLine(_pt(r, 0.30, 0.14), _pt(r, 0.30, 0.34))
    p.drawLine(_pt(r, 0.70, 0.14), _pt(r, 0.70, 0.34))
    slot = QRectF(_pt(r, 0.38, 0.56), _pt(r, 0.62, 0.80))
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(slot, 1.5, 1.5)


def _draw_dna(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Dataset de entrenamiento: icono de base de datos (cilindro apilado). / EN: Training dataset: database icon (stacked cylinder)."""
    p.setPen(_pen(color, size))
    top = QRectF(_pt(r, 0.20, 0.14), _pt(r, 0.80, 0.30))
    mid_h = r.height() * 0.28
    p.drawEllipse(top)
    p.drawLine(_pt(r, 0.20, 0.22), _pt(r, 0.20, 0.78))
    p.drawLine(_pt(r, 0.80, 0.22), _pt(r, 0.80, 0.78))
    bottom = QRectF(_pt(r, 0.20, 0.70), _pt(r, 0.80, 0.86))
    p.drawArc(bottom, 0, -180 * 16)
    mid = QRectF(_pt(r, 0.20, 0.42), _pt(r, 0.80, 0.58))
    p.drawArc(mid, 0, -180 * 16)


def _draw_coffee(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Apoyar el proyecto: taza + asa + vapor. / EN: Support the project: cup + handle + steam."""
    pen = _pen(color, size)
    p.setPen(pen)
    cup = QRectF(_pt(r, 0.20, 0.36), _pt(r, 0.66, 0.80))
    p.drawRoundedRect(cup, cup.width() * 0.12, cup.width() * 0.12)
    handle = QRectF(_pt(r, 0.62, 0.44), _pt(r, 0.86, 0.68))
    p.drawArc(handle, -90 * 16, 180 * 16)
    path = QPainterPath()
    path.moveTo(_pt(r, 0.34, 0.28))
    path.cubicTo(_pt(r, 0.28, 0.22), _pt(r, 0.34, 0.16), _pt(r, 0.30, 0.10))
    p.drawPath(path)
    path2 = QPainterPath()
    path2.moveTo(_pt(r, 0.50, 0.28))
    path2.cubicTo(_pt(r, 0.44, 0.22), _pt(r, 0.50, 0.16), _pt(r, 0.46, 0.10))
    p.drawPath(path2)


def _draw_warning(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Advertencia/error: triángulo + signo de exclamación. / EN: Warning/error: triangle + exclamation mark."""
    pen = _pen(color, size)
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(_pt(r, 0.5, 0.12))
    path.lineTo(_pt(r, 0.90, 0.84))
    path.lineTo(_pt(r, 0.10, 0.84))
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(_pt(r, 0.5, 0.40), _pt(r, 0.5, 0.62))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(_pt(r, 0.5, 0.72), r.width() * 0.025, r.width() * 0.025)


def _draw_user(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Remitente humano: cabeza + hombros. / EN: Human sender: head + shoulders."""
    p.setPen(_pen(color, size))
    head_r = r.width() * 0.15
    p.drawEllipse(_pt(r, 0.5, 0.32), head_r, head_r)
    shoulders = QRectF(_pt(r, 0.20, 0.56), _pt(r, 0.80, 1.10))
    p.drawArc(shoulders, 20 * 16, 140 * 16)


def _draw_wrench(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Auto-corregido: llave inglesa simplificada. / EN: Self-corrected: simplified wrench."""
    p.setPen(_pen(color, size))
    p.drawLine(_pt(r, 0.30, 0.78), _pt(r, 0.62, 0.42))
    head = QRectF(_pt(r, 0.56, 0.14), _pt(r, 0.86, 0.44))
    p.drawArc(head, 30 * 16, 220 * 16)
    p.drawEllipse(_pt(r, 0.24, 0.84), r.width() * 0.06, r.width() * 0.06)


def _draw_flask(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Verificado en sandbox: matraz Erlenmeyer + línea de nivel. / EN: Sandbox-verified: Erlenmeyer flask + fill line."""
    p.setPen(_pen(color, size))
    path = QPainterPath()
    path.moveTo(_pt(r, 0.42, 0.12))
    path.lineTo(_pt(r, 0.42, 0.38))
    path.lineTo(_pt(r, 0.18, 0.82))
    path.quadTo(_pt(r, 0.20, 0.88), _pt(r, 0.28, 0.88))
    path.lineTo(_pt(r, 0.72, 0.88))
    path.quadTo(_pt(r, 0.80, 0.88), _pt(r, 0.82, 0.82))
    path.lineTo(_pt(r, 0.58, 0.38))
    path.lineTo(_pt(r, 0.58, 0.12))
    p.drawPath(path)
    p.drawLine(_pt(r, 0.32, 0.12), _pt(r, 0.68, 0.12))
    p.drawLine(_pt(r, 0.30, 0.64), _pt(r, 0.70, 0.64))


def _draw_attach(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Adjuntar archivo: círculo + cruz "+", mismo lenguaje visual que el resto del set.
    EN: Attach file: circle + "+" cross, same visual language as the rest of the set."""
    p.setPen(_pen(color, size))
    circle = QRectF(_pt(r, 0.10, 0.10), _pt(r, 0.90, 0.90))
    p.drawEllipse(circle)
    p.drawLine(_pt(r, 0.5, 0.32), _pt(r, 0.5, 0.68))
    p.drawLine(_pt(r, 0.32, 0.5), _pt(r, 0.68, 0.5))


def _draw_clipboard(p: QPainter, r: QRectF, color: str, size: int) -> None:
    """ES: Copiar texto: portapapeles + líneas de texto. / EN: Copy text: clipboard + text lines."""
    p.setPen(_pen(color, size))
    body = QRectF(_pt(r, 0.22, 0.16), _pt(r, 0.78, 0.88))
    p.drawRoundedRect(body, body.width() * 0.10, body.width() * 0.10)
    clip = QRectF(_pt(r, 0.38, 0.10), _pt(r, 0.62, 0.22))
    p.setBrush(QColor(color))
    p.drawRoundedRect(clip, clip.width() * 0.2, clip.width() * 0.2)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for y in (0.38, 0.54, 0.70):
        p.drawLine(_pt(r, 0.34, y), _pt(r, 0.66, y))


_DRAW_FUNCS: Dict[str, Callable[[QPainter, QRectF, str, int], None]] = {
    "gear": _draw_gear,
    "mic": _draw_mic,
    "speaker": _draw_speaker,
    "globe": _draw_globe,
    "terminal": _draw_terminal,
    "stop": _draw_stop,
    "download": _draw_download,
    "save": _draw_save,
    "dna": _draw_dna,
    "coffee": _draw_coffee,
    "warning": _draw_warning,
    "user": _draw_user,
    "wrench": _draw_wrench,
    "flask": _draw_flask,
    "clipboard": _draw_clipboard,
    "attach": _draw_attach,
}

ICON_KINDS = tuple(_DRAW_FUNCS.keys())

_cache: Dict[Tuple[str, str, int], QIcon] = {}


def icon(kind: str, color: str, size: int = 18) -> QIcon:
    """
    ES: Devuelve (y cachea) un QIcon dibujado en vector para `kind`, teñido de
    `color` — el mismo ícono sirve para cualquier tema, sin generar variantes
    a mano. Si `kind` no está registrado, devuelve un QIcon vacío en vez de
    lanzar una excepción (degrada a "sin ícono", nunca tumba la ventana).
    EN: Returns (and caches) a vector-drawn QIcon for `kind`, tinted with
    `color` — the same icon works for any theme with no hand-made variants.
    If `kind` isn't registered, returns an empty QIcon instead of raising
    (degrades to "no icon", never crashes the window).
    """
    key = (kind, color, size)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    draw_fn = _DRAW_FUNCS.get(kind)
    if draw_fn is None:
        empty = QIcon()
        _cache[key] = empty
        return empty

    dpr = 2
    pixmap = QPixmap(size * dpr, size * dpr)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    rect = QRectF(0.0, 0.0, float(size), float(size))
    try:
        draw_fn(painter, rect, color, size)
    finally:
        painter.end()

    result = QIcon(pixmap)
    _cache[key] = result
    return result
