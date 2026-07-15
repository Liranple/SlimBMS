"""The application icon, drawn programmatically (no bundled image needed).

A small BMS-editor mark: a dark rounded tile with a few note lanes and falling
note blocks in the app's white/blue accent colours. Used for the window/taskbar
icon at runtime; ``python -m slimbms.ui.appicon`` also writes ``slimbms.ico`` for
the Windows build.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPixmap,
)

# Palette (matches the editor's dark theme / note colours).
_BG_TOP = QColor("#2a2b36")
_BG_BOT = QColor("#191920")
_ACCENT = QColor("#6fd0ff")
_LANE = QColor(255, 255, 255, 18)
_WHITE = QColor("#eef0f4")
_BLUE = QColor("#5aa0ff")


def draw_pixmap(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    s = float(size)
    radius = s * 0.22
    margin = s * 0.06
    tile = QRectF(margin, margin, s - 2 * margin, s - 2 * margin)

    # Rounded tile with a vertical gradient.
    grad = QLinearGradient(0, tile.top(), 0, tile.bottom())
    grad.setColorAt(0.0, _BG_TOP)
    grad.setColorAt(1.0, _BG_BOT)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(tile, radius, radius)

    # Lane band across the middle third.
    lanes = 4
    band_w = tile.width() * 0.62
    band_x = tile.left() + (tile.width() - band_w) / 2
    band_top = tile.top() + tile.height() * 0.10
    band_bot = tile.bottom() - tile.height() * 0.10
    lane_w = band_w / lanes

    # Faint lane columns + separators.
    p.setBrush(_LANE)
    for i in range(lanes):
        x = band_x + i * lane_w
        p.drawRect(QRectF(x, band_top, lane_w - max(1.0, s * 0.012), band_bot - band_top))

    # Falling note blocks (rounded), a couple of white and blue, staggered.
    note_h = max(2.0, s * 0.11)
    note_r = note_h * 0.32
    notes = [
        (0, 0.62, _BLUE),
        (1, 0.30, _WHITE),
        (2, 0.72, _WHITE),
        (3, 0.44, _BLUE),
        (1, 0.86, _BLUE),
    ]
    pad = max(1.0, s * 0.018)
    for lane, ypos, color in notes:
        x = band_x + lane * lane_w + pad
        w = lane_w - 2 * pad - max(1.0, s * 0.012)
        y = band_top + (band_bot - band_top - note_h) * ypos
        glow = QColor(color)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(x, y, w, note_h), note_r, note_r)
        # a soft top highlight cap
        glow.setAlpha(70)
        p.setBrush(glow)
        p.drawRoundedRect(QRectF(x, y, w, note_h * 0.42), note_r, note_r)

    # Accent playhead line near the bottom of the lanes.
    p.setBrush(Qt.NoBrush)
    pen_w = max(1.0, s * 0.03)
    from PySide6.QtGui import QPen

    p.setPen(QPen(_ACCENT, pen_w))
    ph_y = band_bot - (band_bot - band_top) * 0.14
    p.drawLine(int(band_x - lane_w * 0.15), int(ph_y),
               int(band_x + band_w + lane_w * 0.15), int(ph_y))

    p.end()
    return pm


def build_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(draw_pixmap(size))
    return icon


def _png_bytes(size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray

    ba = QByteArray()               # keep alive for the buffer's lifetime
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    draw_pixmap(size).toImage().save(buf, "PNG")
    buf.close()
    return bytes(ba)


def write_ico(path: str, sizes=(16, 24, 32, 48, 64, 128, 256)) -> None:
    """Assemble a PNG-based .ico (Vista+) for the Windows build."""
    import struct

    pngs = [(sz, _png_bytes(sz)) for sz in sizes]
    header = struct.pack("<HHH", 0, 1, len(pngs))
    entries = b""
    offset = 6 + 16 * len(pngs)
    for sz, data in pngs:
        w = 0 if sz >= 256 else sz
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open(path, "wb") as fh:
        fh.write(header + entries + b"".join(d for _, d in pngs))


if __name__ == "__main__":
    import os
    import sys

    from PySide6.QtGui import QGuiApplication

    QGuiApplication(sys.argv)
    out = sys.argv[1] if len(sys.argv) > 1 else "slimbms.ico"
    write_ico(out)
    # Also drop a PNG preview next to it.
    draw_pixmap(256).save(os.path.splitext(out)[0] + "_preview.png")
    print("wrote", out)
