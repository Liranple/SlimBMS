"""Toolbar icons drawn in code, so the whole row is one consistent monochrome
set (no mix of colour emoji and glyph symbols) and renders the same everywhere.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)

_INK = QColor("#d6d6de")     # matches theme TEXT


def _tri(pts, s):
    return QPolygonF([QPointF(x * s, y * s) for x, y in pts])


def _draw(name: str, p: QPainter, s: float) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(_INK))

    if name == "play":
        p.drawPolygon(_tri([(0.30, 0.20), (0.30, 0.80), (0.80, 0.50)], s))
    elif name == "pause":
        p.drawRoundedRect(QRectF(0.30 * s, 0.22 * s, 0.14 * s, 0.56 * s), s * 0.03, s * 0.03)
        p.drawRoundedRect(QRectF(0.56 * s, 0.22 * s, 0.14 * s, 0.56 * s), s * 0.03, s * 0.03)
    elif name == "stop":
        p.drawRoundedRect(QRectF(0.27 * s, 0.27 * s, 0.46 * s, 0.46 * s), s * 0.06, s * 0.06)
    elif name == "first":   # |◀  skip to start
        p.drawRoundedRect(QRectF(0.22 * s, 0.24 * s, 0.09 * s, 0.52 * s), s * 0.03, s * 0.03)
        p.drawPolygon(_tri([(0.78, 0.24), (0.78, 0.76), (0.36, 0.50)], s))
    elif name == "back":    # ◀◀  rewind
        p.drawPolygon(_tri([(0.50, 0.24), (0.50, 0.76), (0.14, 0.50)], s))
        p.drawPolygon(_tri([(0.86, 0.24), (0.86, 0.76), (0.50, 0.50)], s))
    elif name == "forward":  # ▶▶  fast-forward
        p.drawPolygon(_tri([(0.14, 0.24), (0.14, 0.76), (0.50, 0.50)], s))
        p.drawPolygon(_tri([(0.50, 0.24), (0.50, 0.76), (0.86, 0.50)], s))
    elif name == "open":     # folder
        path = QPainterPath()
        path.moveTo(0.13 * s, 0.30 * s)
        path.lineTo(0.42 * s, 0.30 * s)
        path.lineTo(0.50 * s, 0.38 * s)
        path.lineTo(0.87 * s, 0.38 * s)
        path.lineTo(0.87 * s, 0.74 * s)
        path.lineTo(0.13 * s, 0.74 * s)
        path.closeSubpath()
        p.fillPath(path, _INK)
    elif name == "save":     # floppy disk
        body = QPainterPath()
        body.moveTo(0.18 * s, 0.18 * s)
        body.lineTo(0.70 * s, 0.18 * s)
        body.lineTo(0.82 * s, 0.30 * s)
        body.lineTo(0.82 * s, 0.82 * s)
        body.lineTo(0.18 * s, 0.82 * s)
        body.closeSubpath()
        p.fillPath(body, _INK)
        # label + shutter cut out in the background colour
        p.setBrush(QColor("#212129"))
        p.drawRect(QRectF(0.34 * s, 0.18 * s, 0.22 * s, 0.16 * s))   # shutter
        p.drawRect(QRectF(0.30 * s, 0.52 * s, 0.40 * s, 0.24 * s))   # label
    elif name in ("expand_all", "collapse_all"):
        # Two chevrons around a centre rule: pointing apart = expand every
        # sidebar section, pointing inward = collapse them.
        p.setPen(QPen(_INK, s * 0.07, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(0.24 * s, 0.50 * s), QPointF(0.76 * s, 0.50 * s))
        p.setPen(Qt.NoPen)
        if name == "expand_all":
            p.drawPolygon(_tri([(0.34, 0.34), (0.66, 0.34), (0.50, 0.14)], s))
            p.drawPolygon(_tri([(0.34, 0.66), (0.66, 0.66), (0.50, 0.86)], s))
        else:
            p.drawPolygon(_tri([(0.34, 0.16), (0.66, 0.16), (0.50, 0.36)], s))
            p.drawPolygon(_tri([(0.34, 0.84), (0.66, 0.84), (0.50, 0.64)], s))
    elif name in ("import", "export"):
        # A tray with an arrow: down into it (import) or up out of it (export).
        p.setPen(QPen(_INK, s * 0.08, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawLine(QPointF(0.22 * s, 0.80 * s), QPointF(0.78 * s, 0.80 * s))
        p.setBrush(QBrush(_INK))
        if name == "import":
            p.drawLine(QPointF(0.50 * s, 0.18 * s), QPointF(0.50 * s, 0.52 * s))
            p.setPen(Qt.NoPen)
            p.drawPolygon(_tri([(0.34, 0.46), (0.66, 0.46), (0.50, 0.68)], s))
        else:
            p.drawLine(QPointF(0.50 * s, 0.32 * s), QPointF(0.50 * s, 0.66 * s))
            p.setPen(Qt.NoPen)
            p.drawPolygon(_tri([(0.34, 0.40), (0.66, 0.40), (0.50, 0.18)], s))


def make_icon(name: str) -> QIcon:
    icon = QIcon()
    for size in (20, 40):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        _draw(name, p, float(size))
        p.end()
        icon.addPixmap(pm)
    return icon
