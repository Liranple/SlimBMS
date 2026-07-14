"""The scrollable chart canvas and its fixed lane-label header."""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..model import Project
from . import layout as L

# Colours (dark theme).
C_BG = QColor("#1e1e24")
C_GROUP_BG_A = QColor("#23232b")
C_GROUP_BG_B = QColor("#1b1b21")
C_MEASURE = QColor("#8a8a99")
C_BEAT = QColor("#4a4a55")
C_SNAP = QColor("#2c2c34")
C_LANE_SEP = QColor("#33333d")
C_GROUP_SEP = QColor("#55556a")
C_TEXT = QColor("#c8c8d0")
C_NOTE_WHITE = QColor("#eef0f4")
C_NOTE_BLUE = QColor("#5aa0ff")
C_NOTE_BGM = QColor("#ffb347")
C_PLAYHEAD = QColor("#ff4d6d")

BEATS_PER_MEASURE = 4


class ChartView(QWidget):
    """Paints the note grid for all key modes and edits notes on click."""

    changed = Signal()  # emitted whenever a note is added/removed

    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.project = project
        self.measure_px = 150         # zoom: vertical pixels per measure
        self.snap_div = 8             # snap grid: 1/snap_div of a measure
        self.v_pad = 24
        self.playhead: Optional[float] = None  # absolute chart pos, or None
        self.columns, self.groups, self._width = L.build_layout()
        self.setMouseTracking(True)
        self._apply_size()

    # -- geometry ----------------------------------------------------------- #

    def _apply_size(self) -> None:
        height = self.project.measures * self.measure_px + 2 * self.v_pad
        self.setFixedSize(self._width, int(height))
        self.updateGeometry()

    def set_zoom(self, measure_px: int) -> None:
        self.measure_px = max(40, min(600, measure_px))
        self._apply_size()
        self.update()

    def set_snap(self, snap_div: int) -> None:
        self.snap_div = snap_div
        self.update()

    def refresh(self) -> None:
        self._apply_size()
        self.update()

    # -- coordinate transforms --------------------------------------------- #

    def y_for(self, absolute: float) -> float:
        """Pixel y for an absolute position in measures (0 = song start)."""
        return self.v_pad + (self.project.measures - absolute) * self.measure_px

    def pos_at(self, y: float):
        """Return (measure, Fraction pos) for a pixel y, snapped to the grid."""
        absolute = self.project.measures - (y - self.v_pad) / self.measure_px
        if absolute < 0:
            absolute = 0.0
        measure = int(absolute)
        frac = absolute - measure
        k = round(frac * self.snap_div)
        if k >= self.snap_div:
            measure += 1
            k = 0
        return measure, Fraction(k, self.snap_div)

    # -- painting ----------------------------------------------------------- #

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), C_BG)
        self._paint_lane_backgrounds(p)
        self._paint_horizontal_lines(p)
        self._paint_separators(p)
        self._paint_notes(p)
        self._paint_playhead(p)
        p.end()

    def set_playhead(self, absolute: Optional[float]) -> None:
        self.playhead = absolute
        self.update()

    def _paint_playhead(self, p: QPainter) -> None:
        if self.playhead is None:
            return
        y = int(self.y_for(self.playhead))
        p.setPen(QPen(C_PLAYHEAD, 2))
        p.drawLine(L.LEFT_MARGIN, y, self.groups[-1].x1, y)

    def _paint_lane_backgrounds(self, p: QPainter) -> None:
        top = self.v_pad
        bottom = self.height() - self.v_pad
        for i, g in enumerate(self.groups):
            p.fillRect(
                QRect(g.x0, int(top), g.x1 - g.x0, int(bottom - top)),
                C_GROUP_BG_A if i % 2 == 0 else C_GROUP_BG_B,
            )

    def _paint_horizontal_lines(self, p: QPainter) -> None:
        x0 = L.LEFT_MARGIN
        x1 = self.groups[-1].x1
        font = QFont()
        font.setPointSize(8)
        p.setFont(font)
        measures = self.project.measures

        # Snap subdivisions (faintest).
        p.setPen(QPen(C_SNAP, 1))
        for m in range(measures):
            for k in range(1, self.snap_div):
                if k * BEATS_PER_MEASURE % self.snap_div == 0:
                    continue  # drawn as a beat line below
                y = self.y_for(m + k / self.snap_div)
                p.drawLine(x0, int(y), x1, int(y))

        # Beat lines.
        p.setPen(QPen(C_BEAT, 1))
        for m in range(measures):
            for b in range(1, BEATS_PER_MEASURE):
                y = self.y_for(m + b / BEATS_PER_MEASURE)
                p.drawLine(x0, int(y), x1, int(y))

        # Measure lines + numbers.
        p.setPen(QPen(C_MEASURE, 1))
        for m in range(measures + 1):
            y = self.y_for(m)
            p.drawLine(x0, int(y), x1, int(y))
            if m < measures:
                p.setPen(QPen(C_TEXT, 1))
                p.drawText(QRect(2, int(y) - 16, L.LEFT_MARGIN - 6, 14),
                           Qt.AlignRight | Qt.AlignVCenter, str(m))
                p.setPen(QPen(C_MEASURE, 1))

    def _paint_separators(self, p: QPainter) -> None:
        top = self.v_pad
        bottom = self.height() - self.v_pad
        # Lane separators inside groups.
        p.setPen(QPen(C_LANE_SEP, 1))
        for col in self.columns:
            p.drawLine(col.x, int(top), col.x, int(bottom))
        # Group boundary lines (stronger).
        p.setPen(QPen(C_GROUP_SEP, 2))
        for g in self.groups:
            p.drawLine(g.x0, int(top), g.x0, int(bottom))
            p.drawLine(g.x1, int(top), g.x1, int(bottom))

    def _note_rect(self, x: int, y: float) -> QRect:
        h = max(6, self.measure_px // 24)
        return QRect(x + 2, int(y - h / 2), L.LANE_W - 3, h)

    def _paint_notes(self, p: QPainter) -> None:
        p.setPen(Qt.NoPen)
        # BGM objects.
        bgm_col = self.columns[0]
        for n in self.project.bgm:
            y = self.y_for(n.absolute)
            p.fillRect(self._note_rect(bgm_col.x, y), C_NOTE_BGM)
        # Key notes.
        col_index = {}
        for col in self.columns:
            if col.kind == "key":
                col_index[(col.key_mode, col.lane)] = col.x
        for km, chart in self.project.charts.items():
            for n in chart:
                x = col_index.get((km, n.lane))
                if x is None:
                    continue
                y = self.y_for(n.absolute)
                colour = C_NOTE_BLUE if n.lane % 2 == 1 else C_NOTE_WHITE
                p.fillRect(self._note_rect(x, y), colour)

    # -- mouse -------------------------------------------------------------- #

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        col = L.column_at(self.columns, event.position().x())
        if col is None:
            return
        measure, pos = self.pos_at(event.position().y())
        if measure >= self.project.measures:
            return
        if event.button() == Qt.LeftButton:
            if col.kind == "bgm":
                self.project.toggle_bgm(measure, pos)
            else:
                self.project.toggle_note(col.key_mode, measure, pos, col.lane)
            self.changed.emit()
            self.update()
        elif event.button() == Qt.RightButton:
            # Right click always erases.
            from ..model import Note
            note = Note(measure, pos, col.lane if col.kind == "key" else 0)
            target = self.project.bgm if col.kind == "bgm" else self.project.charts[col.key_mode]
            if note in target:
                target.discard(note)
                self.changed.emit()
                self.update()


class LaneHeader(QWidget):
    """A thin fixed strip above the canvas showing group labels, kept aligned
    with the canvas as it scrolls horizontally."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.columns, self.groups, self._width = L.build_layout()
        self.x_offset = 0
        self.setFixedHeight(26)
        self.setMinimumWidth(self._width)

    def set_x_offset(self, value: int) -> None:
        self.x_offset = value
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), C_GROUP_BG_A)
        p.translate(-self.x_offset, 0)
        font = QFont()
        font.setBold(True)
        font.setPointSize(9)
        p.setFont(font)
        p.setPen(QPen(C_TEXT, 1))
        for g in self.groups:
            p.drawText(QRect(g.x0, 0, g.x1 - g.x0, self.height()),
                       Qt.AlignCenter, g.label)
        p.setPen(QPen(C_GROUP_SEP, 1))
        for g in self.groups:
            p.drawLine(g.x1 + L.GROUP_GAP // 2, 4, g.x1 + L.GROUP_GAP // 2, self.height() - 4)
        p.end()
