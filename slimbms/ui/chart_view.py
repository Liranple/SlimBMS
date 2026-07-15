"""The scrollable chart canvas and its fixed lane-label header."""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..model import LANE_COLORS, Project
from . import layout as L

# Colours (dark theme).
C_BG = QColor("#1e1e24")
C_GROUP_BG_A = QColor("#23232b")
C_GROUP_BG_B = QColor("#1b1b21")
C_MEASURE = QColor("#8a8a99")
C_GRID_MAIN = QColor("#4a4a58")   # primary (snap) grid
C_GRID_SUB = QColor("#33333f")    # secondary reference grid
C_LANE_SEP = QColor("#33333d")
C_GROUP_SEP = QColor("#55556a")
C_TEXT = QColor("#c8c8d0")
C_NOTE_WHITE = QColor("#eef0f4")
C_NOTE_BLUE = QColor("#5aa0ff")
C_NOTE_GREY = QColor("#9aa0ac")
C_NOTE_BGM = QColor("#ffb347")
C_PLAYHEAD = QColor("#ff4d6d")

# Note colour by lane code.
NOTE_COLOR = {"W": C_NOTE_WHITE, "B": C_NOTE_BLUE, "G": C_NOTE_GREY}
# Pale lane background tint by lane code (kept faint so the grid stays visible).
LANE_TINT = {
    "W": QColor(255, 255, 255, 12),
    "B": QColor(90, 160, 255, 26),
    "G": QColor(150, 160, 175, 20),
}

FREE_DIV = 192  # placement resolution when snap is off / Shift held


class ChartView(QWidget):
    """Paints the note grid for all key modes and edits notes on click."""

    changed = Signal()      # emitted whenever a note is added/removed
    zoom_step = Signal(int)  # Ctrl+wheel: +1 zoom in, -1 zoom out

    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.project = project
        self.measure_px = 150         # zoom: vertical pixels per measure
        self.grid_main = Fraction(1, 16)  # primary (snap) grid, of a measure
        self.grid_sub = Fraction(1, 12)   # secondary reference grid
        self.snap_on = True
        self.v_pad = 24
        self.playhead: Optional[float] = None  # absolute chart pos, or None
        self._hover = None            # (Column, measure, Fraction pos) or None
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

    def set_grid_main(self, num: int, den: int) -> None:
        if num >= 1 and den >= 1:
            self.grid_main = Fraction(num, den)
            self.update()

    def set_grid_sub(self, num: int, den: int) -> None:
        if num >= 1 and den >= 1:
            self.grid_sub = Fraction(num, den)
            self.update()

    def set_snap_on(self, on: bool) -> None:
        self.snap_on = on
        self.update()

    def refresh(self) -> None:
        self._apply_size()
        self.update()

    # -- coordinate transforms --------------------------------------------- #

    def y_for(self, absolute: float) -> float:
        """Pixel y for an absolute position in measures (0 = song start)."""
        return self.v_pad + (self.project.measures - absolute) * self.measure_px

    def absolute_at(self, y: float) -> float:
        return max(0.0, self.project.measures - (y - self.v_pad) / self.measure_px)

    def pos_at(self, y: float, snap: bool):
        """Return (measure, Fraction pos) for a pixel y. When ``snap`` the
        position is floored to the primary grid cell the cursor is in (so the
        note lands in exactly the cell under the mouse); otherwise it is floored
        to a fine resolution for near-free placement."""
        absolute = self.absolute_at(y)
        measure = int(absolute)
        frac = absolute - measure          # [0, 1)
        if snap:
            step = self.grid_main
            k = int(frac / float(step))    # floor -> cell under the cursor
            pos = step * k
        else:
            pos = Fraction(int(frac * FREE_DIV), FREE_DIV)
        if pos >= 1:
            pos = Fraction(0)
            measure += 1
        return measure, pos

    # -- painting ----------------------------------------------------------- #

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), C_BG)
        self._paint_lane_backgrounds(p)
        self._paint_horizontal_lines(p)
        self._paint_separators(p)
        self._paint_notes(p)
        self._paint_hover(p)
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
        top = int(self.v_pad)
        height = int(self.height() - 2 * self.v_pad)
        # Base group backgrounds (subtle alternating shade).
        for i, g in enumerate(self.groups):
            p.fillRect(QRect(g.x0, top, g.x1 - g.x0, height),
                       C_GROUP_BG_A if i % 2 == 0 else C_GROUP_BG_B)
        # Per-lane colour tint (pale, so grid lines remain visible).
        for col in self.columns:
            if col.kind != "key":
                continue
            code = LANE_COLORS.get(col.key_mode, "")
            if col.lane < len(code):
                p.fillRect(QRect(col.x, top, L.LANE_W, height),
                           LANE_TINT[code[col.lane]])

    def _grid_line_ys(self, step: Fraction):
        """Yield pixel y for every grid line at ``step`` spacing (skipping the
        measure line at k=0, drawn separately)."""
        measures = self.project.measures
        step_f = float(step)
        for m in range(measures):
            k = 1
            while k * step_f < 1.0:
                yield self.y_for(m + k * step_f)
                k += 1

    def _paint_horizontal_lines(self, p: QPainter) -> None:
        x0 = L.LEFT_MARGIN
        x1 = self.groups[-1].x1
        font = QFont()
        font.setPointSize(8)
        p.setFont(font)
        measures = self.project.measures

        # Secondary grid (faintest), then primary grid over it.
        p.setPen(QPen(C_GRID_SUB, 1))
        for y in self._grid_line_ys(self.grid_sub):
            p.drawLine(x0, int(y), x1, int(y))
        p.setPen(QPen(C_GRID_MAIN, 1))
        for y in self._grid_line_ys(self.grid_main):
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

    def _note_rect(self, x: int, absolute: float) -> QRect:
        """A note fills the grid cell above its timing line, sized to the
        primary grid so it fits the grid exactly at any zoom level."""
        cell_h = max(3, int(round(self.measure_px * float(self.grid_main))))
        y_line = int(round(self.y_for(absolute)))
        return QRect(x + 1, y_line - cell_h + 1, L.LANE_W - 2, cell_h - 1)

    def _note_color(self, key_mode: int, lane: int) -> QColor:
        code = LANE_COLORS.get(key_mode, "")
        if lane < len(code):
            return NOTE_COLOR[code[lane]]
        return C_NOTE_WHITE

    def _paint_notes(self, p: QPainter) -> None:
        p.setPen(Qt.NoPen)
        # BGM objects.
        bgm_col = self.columns[0]
        for n in self.project.bgm:
            p.fillRect(self._note_rect(bgm_col.x, n.absolute), C_NOTE_BGM)
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
                p.fillRect(self._note_rect(x, n.absolute), self._note_color(km, n.lane))

    def _hover_color(self, col) -> QColor:
        if col.kind == "bgm":
            base = C_NOTE_BGM
        else:
            base = self._note_color(col.key_mode, col.lane)
        ghost = QColor(base)
        ghost.setAlpha(90)
        return ghost

    def _paint_hover(self, p: QPainter) -> None:
        if self._hover is None:
            return
        col, measure, pos = self._hover
        rect = self._note_rect(col.x, measure + pos)
        p.setPen(QPen(QColor(255, 255, 255, 130), 1))
        p.setBrush(self._hover_color(col))
        p.drawRect(rect)
        p.setBrush(Qt.NoBrush)

    # -- mouse -------------------------------------------------------------- #

    def _snap_now(self, event: QMouseEvent) -> bool:
        # Snap unless disabled, or temporarily bypassed by holding Shift.
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        return self.snap_on and not shift

    def _resolve(self, event: QMouseEvent):
        col = L.column_at(self.columns, event.position().x())
        if col is None:
            return None
        measure, pos = self.pos_at(event.position().y(), self._snap_now(event))
        if measure < 0 or measure >= self.project.measures:
            return None
        return col, measure, pos

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        hover = self._resolve(event)
        if hover != self._hover:
            self._hover = hover
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover is not None:
            self._hover = None
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        resolved = self._resolve(event)
        if resolved is None:
            return
        col, measure, pos = resolved
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

    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & Qt.ControlModifier:
            self.zoom_step.emit(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
        else:
            event.ignore()  # let the scroll area scroll


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
