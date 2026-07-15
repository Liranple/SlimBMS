"""The scrollable chart canvas and its fixed lane-label header."""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen, QPolygon
from PySide6.QtWidgets import QWidget

from ..model import (
    DISPLAY_LABELS,
    KEY_MODES,
    LANE_COLORS,
    Note,
    Project,
    lanes_for,
)
from . import layout as L

# Colours (dark theme).
C_BG = QColor("#1e1e24")
C_GROUP_BG_A = QColor("#23232b")
C_GROUP_BG_B = QColor("#1b1b21")
C_MEASURE = QColor("#8a8a99")
C_GRID_FINE = QColor("#313139")   # fine snap grid (faint)
C_GRID_REF = QColor("#5a5a76")    # reference grid (brighter, drawn on top)
C_LANE_SEP = QColor("#33333d")
C_GROUP_SEP = QColor("#55556a")
C_TEXT = QColor("#c8c8d0")
C_NOTE_WHITE = QColor("#eef0f4")
C_NOTE_BLUE = QColor("#5aa0ff")
C_NOTE_GREY = QColor("#9aa0ac")
C_NOTE_BGM = QColor("#ffb347")
C_PLAYHEAD = QColor("#ff4d6d")
C_CONFLICT = QColor("#ff4d4d")   # notes that overlap another note in the same lane

# Note colour by lane code.
NOTE_COLOR = {"W": C_NOTE_WHITE, "B": C_NOTE_BLUE, "G": C_NOTE_GREY}
# Pale lane background tint by lane code (kept faint so the grid stays visible).
LANE_TINT = {
    "W": QColor(255, 255, 255, 12),
    "B": QColor(90, 160, 255, 26),
    "G": QColor(150, 160, 175, 20),
}

FREE_DIV = 192  # placement resolution when snap is off / Shift held

C_SELECT = QColor("#6fd0ff")            # accent for the selected key mode
C_SELECT_TINT = QColor(111, 208, 255, 20)  # faint fill over its lanes

# Live-recording keys, per key mode: {Qt key -> lane index}. Left hand Q/W/E,
# right hand numpad(or top-row) 7/8/9, mapped left-to-right across the lanes.
# Top-row and numpad digits both arrive as Key_7/8/9 (NumLock on), so both work.
RECORD_KEYS = {
    4: {Qt.Key_Q: 0, Qt.Key_W: 1, Qt.Key_8: 2, Qt.Key_9: 3},
    6: {Qt.Key_Q: 0, Qt.Key_W: 1, Qt.Key_E: 2, Qt.Key_7: 3, Qt.Key_8: 4, Qt.Key_9: 5},
}


class ChartView(QWidget):
    """Paints the note grid for all key modes and edits notes on click."""

    changed = Signal()        # emitted whenever a note is added/removed
    zoom_step = Signal(int)   # Ctrl+wheel: +1 vertical zoom in, -1 out
    lane_zoom_step = Signal(int)  # Alt+wheel: +1 horizontal (lane width) zoom in, -1 out
    mode_changed = Signal(str)  # "add" or "edit"
    cursor_info = Signal(str)   # live "group · grid coords" text for the status bar

    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.project = project
        self.measure_px = 150         # zoom: vertical pixels per measure
        self.lane_w = L.LANE_W        # zoom: horizontal pixels per lane
        self.grid_main = Fraction(1, 16)  # primary (snap) grid, of a measure
        self.grid_sub = Fraction(1, 12)   # secondary reference grid
        self.snap_on = True
        self.v_pad = 24
        self.playhead: Optional[float] = None  # absolute chart pos, or None
        self.live_playing = False     # True while the preview is actively playing
        self.selected_km = KEY_MODES[0]  # key mode being recorded / highlighted
        self._hover = None            # (Column, measure, Fraction pos) or None
        self.mode = "add"             # "add" (F3) or "edit" (F2)
        self.selection = set()        # {(mode, Note)} ; mode is int or "bgm"
        self._clipboard = None        # [(mode, Fraction d_abs, lane)]
        self._drag_start = None       # (x, y) rubber-band anchor
        self._drag_cur = None
        self._drag_shift = False
        self._paste_anchor = 0.0
        self._add_drag = None         # (mode_key, Note, start_abs, Column) while dragging a long note
        self._rec_pending = {}        # key -> (km, Note, start_abs) for keys held during recording
        self.columns, self.groups, self._width = L.build_layout(self.lane_w)
        # Paint caches, rebuilt on edit (not per paint): overlap flags plus a
        # by-measure index so painting only touches notes near the viewport.
        self._conflicts = set()          # {(km, Note)} overlapping notes
        self._taps_by_measure = {}       # km -> {measure -> [tap Note]}
        self._longs = {}                 # km -> [long Note]
        self._bgm_by_measure = {}        # measure -> [BGM Note]
        self.changed.connect(self._rebuild_caches)
        self._rebuild_caches()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
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

    def set_lane_width(self, lane_w: int) -> None:
        self.lane_w = max(14, min(80, lane_w))
        self.columns, self.groups, self._width = L.build_layout(self.lane_w)
        self._apply_size()
        self.update()

    def set_grid_main(self, cells_per_measure: int) -> None:
        if cells_per_measure >= 1:
            self.grid_main = Fraction(1, cells_per_measure)
            self.update()

    def set_grid_sub(self, cells_per_measure: int) -> None:
        if cells_per_measure >= 1:
            self.grid_sub = Fraction(1, cells_per_measure)
            self.update()

    def set_snap_on(self, on: bool) -> None:
        self.snap_on = on
        self.update()

    def set_live(self, on: bool) -> None:
        """Toggle live tap-along: while on, a left click in add mode drops a
        note at the current playhead time (in the clicked lane) instead of at
        the cursor's vertical position."""
        self.live_playing = on
        if not on:
            self._rec_pending.clear()  # stop growing any held-key long notes
        self.update()

    def set_selected_km(self, key_mode: int) -> None:
        """The key mode notes are recorded into and that is highlighted."""
        self.selected_km = key_mode
        self.update()

    def set_mode(self, mode: str) -> None:
        if mode not in ("add", "edit"):
            return
        self.mode = mode
        if mode == "add":
            self.selection = set()
        self._hover = None
        self.mode_changed.emit(mode)
        self.update()

    def refresh(self) -> None:
        self._rebuild_caches()   # project may have been replaced/resized
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

    def playhead_cell(self, snap: bool):
        """Return (measure, Fraction pos) for the current playhead position.
        When ``snap`` the time is quantised to the *nearest* primary grid line
        (nearest, not floored, so a tap that lands slightly off-beat still snaps
        to the intended cell); otherwise it keeps a fine free resolution."""
        frac = Fraction(max(0.0, self.playhead or 0.0)).limit_denominator(FREE_DIV)
        if snap:
            step = self.grid_main
            frac = step * round(frac / step)
        measure = int(frac)
        return measure, frac - measure

    # -- painting ----------------------------------------------------------- #

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        # Only the exposed strip needs repainting; cull grid lines and notes
        # outside it so scrolling a big chart stays smooth.
        clip = event.rect()
        self._vis_lo = clip.top() - 2
        self._vis_hi = clip.bottom() + 2
        p.fillRect(clip, C_BG)
        self._paint_lane_backgrounds(p)
        self._paint_horizontal_lines(p)
        self._paint_separators(p)
        self._paint_selected_group(p)
        self._paint_notes(p)
        self._paint_selection(p)
        self._paint_hover(p)
        self._paint_drag(p)
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
                p.fillRect(QRect(col.x, top, self.lane_w, height),
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
        font = QFont()
        font.setPointSize(8)
        p.setFont(font)
        measures = self.project.measures
        # Draw grid/measure lines only inside each lane group; the gaps between
        # groups (BGM · 4K · 6K · LOAD) stay empty background.
        spans = [(g.x0, g.x1) for g in self.groups]

        lo, hi = self._vis_lo, self._vis_hi

        def draw_row(y: float) -> None:
            yi = int(y)
            if yi < lo or yi > hi:
                return
            for gx0, gx1 in spans:
                p.drawLine(gx0, yi, gx1, yi)

        # Fine snap grid first (faint), then the reference grid on top (brighter)
        # so its guide lines stay visible even where they coincide with the
        # denser snap lines.
        p.setPen(QPen(C_GRID_FINE, 1))
        for y in self._grid_line_ys(self.grid_main):
            draw_row(y)
        p.setPen(QPen(C_GRID_REF, 1))
        for y in self._grid_line_ys(self.grid_sub):
            draw_row(y)

        # Measure lines + numbers.
        for m in range(measures + 1):
            y = self.y_for(m)
            p.setPen(QPen(C_MEASURE, 1))
            draw_row(y)
            if m < measures and lo <= int(y) <= hi + 16:
                p.setPen(QPen(C_TEXT, 1))
                p.drawText(QRect(2, int(y) - 16, L.LEFT_MARGIN - 6, 14),
                           Qt.AlignRight | Qt.AlignVCenter, str(m))

    def _selected_group_span(self):
        xs = [c.x for c in self.columns
              if c.kind == "key" and c.key_mode == self.selected_km]
        if not xs:
            return None
        return min(xs), max(xs) + self.lane_w

    def _paint_selected_group(self, p: QPainter) -> None:
        """Gently highlight the selected key mode's lanes: a faint accent tint
        plus thin accent edges, kept subtle so it doesn't fight the notes."""
        span = self._selected_group_span()
        if span is None:
            return
        x0, x1 = span
        top = int(self.v_pad)
        bottom = int(self.height() - self.v_pad)
        p.fillRect(QRect(x0, top, x1 - x0, bottom - top), C_SELECT_TINT)
        p.setPen(QPen(C_SELECT, 2))
        p.setBrush(Qt.NoBrush)
        p.drawLine(x0, top, x0, bottom)
        p.drawLine(x1, top, x1, bottom)

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
        return QRect(x + 1, y_line - cell_h + 1, self.lane_w - 2, cell_h - 1)

    def _paint_note(self, p: QPainter, x: int, note: Note, color: QColor) -> None:
        head = self._note_rect(x, note.absolute)
        if note.is_long:
            # One continuous full-width body from tail to head, plus a bright
            # outline, so even a short hold reads as a single connected note and
            # can't be mistaken for two separate taps.
            tail = self._note_rect(x, note.end_absolute)
            span = QRect(head.x(), tail.top(), head.width(), head.bottom() - tail.top())
            body = QColor(color)
            body.setAlpha(64)
            p.fillRect(span, body)
            p.setPen(QPen(color, 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(span.adjusted(1, 1, -1, -1))
            p.setPen(Qt.NoPen)
            p.fillRect(tail, color)             # solid tail cap
        p.fillRect(head, color)                 # solid head cap

    def _note_color(self, key_mode: int, lane: int) -> QColor:
        code = LANE_COLORS.get(key_mode, "")
        if lane < len(code):
            return NOTE_COLOR[code[lane]]
        return C_NOTE_WHITE

    @staticmethod
    def _overlaps(a: Note, b: Note) -> bool:
        """True when two notes in the same lane collide: their bodies overlap,
        or they start on the same line (a tap sitting on a long note's head, or
        two notes stacked at the same time). Back-to-back long notes that merely
        touch at one endpoint are *not* flagged."""
        return (a.absolute < b.end_absolute and b.absolute < a.end_absolute) \
            or a.absolute == b.absolute

    def _rebuild_caches(self) -> None:
        """Rebuild paint caches after an edit (or project reload) — NOT per paint.
        Builds overlap flags and a by-measure tap index (plus a small long-note
        list) so a large imported chart paints only what's near the viewport and
        never re-scans every note each frame."""
        conflicts = set()
        taps_by_measure = {}
        longs = {}
        for km, chart in self.project.charts.items():
            tm = {}
            lg = []
            by_lane = {}
            for n in chart:
                by_lane.setdefault(n.lane, []).append(n)
                if n.is_long:
                    lg.append(n)
                else:
                    tm.setdefault(n.measure, []).append(n)
            taps_by_measure[km] = tm
            longs[km] = lg
            # Overlap flags: sort each lane so the inner scan can break early.
            for notes in by_lane.values():
                notes.sort(key=lambda n: n.absolute)
                for i, a in enumerate(notes):
                    a_end = a.end_absolute
                    for b in notes[i + 1:]:
                        if b.absolute >= a_end and b.absolute != a.absolute:
                            break   # sorted: nothing further can overlap a
                        if self._overlaps(a, b):
                            conflicts.add((km, a))
                            conflicts.add((km, b))
        bgm_by_measure = {}
        for n in self.project.bgm:
            bgm_by_measure.setdefault(n.measure, []).append(n)
        self._conflicts = conflicts
        self._taps_by_measure = taps_by_measure
        self._longs = longs
        self._bgm_by_measure = bgm_by_measure

    def _visible(self, note: Note) -> bool:
        """True when any part of the note falls inside the exposed paint strip."""
        top = self.y_for(note.end_absolute)     # higher position = smaller y
        bottom = self.y_for(note.absolute)
        cell_h = self.measure_px * float(self.grid_main)
        return bottom >= self._vis_lo and (top - cell_h) <= self._vis_hi

    def _visible_measure_range(self):
        """Inclusive measure range whose notes can fall in the exposed strip."""
        measures = self.project.measures
        abs_top = self.absolute_at(self._vis_lo)    # top of strip = higher pos
        abs_bot = self.absolute_at(self._vis_hi)
        m_lo = max(0, int(abs_bot) - 1)
        m_hi = min(measures - 1, int(abs_top) + 1)
        return m_lo, m_hi

    def _paint_notes(self, p: QPainter) -> None:
        p.setPen(Qt.NoPen)
        m_lo, m_hi = self._visible_measure_range()
        # BGM objects (always taps) in the visible measures.
        bgm_x = self.columns[0].x
        for m in range(m_lo, m_hi + 1):
            for n in self._bgm_by_measure.get(m, ()):
                p.fillRect(self._note_rect(bgm_x, n.absolute), C_NOTE_BGM)
        col_index = {}
        for col in self.columns:
            if col.kind == "key":
                col_index[(col.key_mode, col.lane)] = col.x
        conflicts = self._conflicts
        for km in self.project.charts:
            tm = self._taps_by_measure.get(km, {})
            for m in range(m_lo, m_hi + 1):
                for n in tm.get(m, ()):
                    x = col_index.get((km, n.lane))
                    if x is None:
                        continue
                    self._paint_note(p, x, n, self._note_color(km, n.lane))
                    if (km, n) in conflicts:
                        self._paint_conflict(p, x, n)
            # Long notes are few; check each against the strip precisely so a
            # hold starting below the viewport still draws where it reaches in.
            for n in self._longs.get(km, ()):
                x = col_index.get((km, n.lane))
                if x is None or not self._visible(n):
                    continue
                self._paint_note(p, x, n, self._note_color(km, n.lane))
                if (km, n) in conflicts:
                    self._paint_conflict(p, x, n)

    def _paint_conflict(self, p: QPainter, x: int, note: Note) -> None:
        """Flag a note that overlaps another in its lane: a red outline around
        its whole span plus a small warning triangle at the top corner."""
        rect = self._sel_rect(x, note)
        p.setPen(QPen(C_CONFLICT, 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(rect.adjusted(1, 1, -1, -1))

        # Warning badge (a red triangle with a white "!") pinned to the top-right
        # corner of the span, clamped so it fits inside a narrow lane.
        bs = max(8, min(12, self.lane_w - 2))
        bx = rect.right() - bs
        by = rect.top()
        tri = QPolygon([
            QPoint(bx, by + bs),
            QPoint(bx + bs, by + bs),
            QPoint(bx + bs // 2, by),
        ])
        p.setPen(Qt.NoPen)
        p.setBrush(C_CONFLICT)
        p.drawPolygon(tri)
        font = QFont()
        font.setBold(True)
        font.setPointSize(max(6, bs - 5))
        p.setFont(font)
        p.setPen(QPen(QColor("#ffffff"), 1))
        p.drawText(QRect(bx, by, bs, bs), Qt.AlignHCenter | Qt.AlignBottom, "!")
        p.setBrush(Qt.NoBrush)

    def _hover_color(self, col) -> QColor:
        if col.kind == "bgm":
            base = C_NOTE_BGM
        else:
            base = self._note_color(col.key_mode, col.lane)
        ghost = QColor(base)
        ghost.setAlpha(90)
        return ghost

    def _paint_hover(self, p: QPainter) -> None:
        if self._hover is None or self.mode != "add":
            return
        col, measure, pos = self._hover
        rect = self._note_rect(col.x, measure + pos)
        p.setPen(QPen(QColor(255, 255, 255, 130), 1))
        p.setBrush(self._hover_color(col))
        p.drawRect(rect)
        p.setBrush(Qt.NoBrush)

    def _col_x(self):
        return {(c.key_mode, c.lane): c.x for c in self.columns if c.kind == "key"}

    def _sel_rect(self, x: int, note: Note) -> QRect:
        """Bounding outline for a note — the head cell for a tap, or the whole
        span from tail cap to head line for a long note."""
        head = self._note_rect(x, note.absolute)
        if not note.is_long:
            return head
        y_top = int(round(self.y_for(note.end_absolute))) - head.height()
        return QRect(head.x(), y_top, head.width(), head.bottom() - y_top)

    def _paint_selection(self, p: QPainter) -> None:
        if not self.selection:
            return
        colx = self._col_x()
        bgmx = self.columns[0].x
        p.setPen(QPen(QColor("#ffe06a"), 2))
        p.setBrush(Qt.NoBrush)
        for mode, n in self.selection:
            x = bgmx if mode == "bgm" else colx.get((mode, n.lane))
            if x is None:
                continue
            p.drawRect(self._sel_rect(x, n))

    def _paint_drag(self, p: QPainter) -> None:
        if self._drag_start is None or self._drag_cur is None:
            return
        x0, y0 = self._drag_start
        x1, y1 = self._drag_cur
        rect = QRect(int(min(x0, x1)), int(min(y0, y1)),
                     int(abs(x1 - x0)), int(abs(y1 - y0)))
        p.setPen(QPen(QColor("#ffe06a"), 1, Qt.DashLine))
        p.setBrush(QColor(255, 224, 106, 30))
        p.drawRect(rect)
        p.setBrush(Qt.NoBrush)

    # -- mouse -------------------------------------------------------------- #

    def _snap_now(self, event: QMouseEvent) -> bool:
        # Snap unless disabled, or temporarily bypassed by holding Shift.
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        return self.snap_on and not shift

    def _resolve(self, event: QMouseEvent):
        col = L.column_at(self.columns, event.position().x(), self.lane_w)
        if col is None:
            return None
        measure, pos = self.pos_at(event.position().y(), self._snap_now(event))
        if measure < 0 or measure >= self.project.measures:
            return None
        return col, measure, pos

    def _note_at_point(self, col, y: float):
        """Closest note in the column's lane whose cell contains y, or None."""
        if col.kind == "bgm":
            pool = [("bgm", n) for n in self.project.bgm]
        else:
            pool = [(col.key_mode, n) for n in self.project.charts[col.key_mode]
                    if n.lane == col.lane]
        cell_h = max(3.0, self.measure_px * float(self.grid_main))
        best, best_d = None, None
        for mode, n in pool:
            y_head = self.y_for(n.absolute)
            # A long note is grabbable anywhere along its body; a tap only in
            # its single cell.
            top = self.y_for(n.end_absolute) - cell_h if n.is_long else y_head - cell_h
            if top - 4 <= y <= y_head + 4:
                d = abs((top + y_head) / 2 - y)
                if best_d is None or d < best_d:
                    best, best_d = (mode, n), d
        return best

    def _add_note(self, col, measure, pos) -> Note:
        if col.kind == "bgm":
            note = Note(measure, pos, 0)
            self.project.bgm.add(note)
        else:
            note = Note(measure, pos, col.lane)
            self.project.charts[col.key_mode].add(note)
        self.changed.emit()
        self.update()
        return note

    def _relen(self, target, note: Note, lo: Fraction, length: Fraction) -> Note:
        """Replace ``note`` in ``target`` with one starting at absolute ``lo``
        with the given ``length`` (0 collapses it back to a tap). Returns the
        new note."""
        target.discard(note)
        measure = int(lo)
        pos = lo - measure
        new = Note(measure, pos, note.lane, length) if length > 0 \
            else Note(measure, pos, note.lane)
        target.add(new)
        return new

    def _grow_add_drag(self, event: QMouseEvent) -> None:
        mode_key, note, start_abs, col = self._add_drag
        if col.kind == "bgm":
            return  # BGM markers stay taps
        measure, pos = self.pos_at(event.position().y(), self._snap_now(event))
        end_abs = measure + pos
        lo = min(start_abs, end_abs)
        hi = min(max(start_abs, end_abs), Fraction(self.project.measures))
        new = self._relen(self.project.charts[mode_key], note, lo, hi - lo)
        self._add_drag = (mode_key, new, start_abs, col)
        self.changed.emit()
        self.update()

    def _erase_at(self, col, y: float) -> None:
        hit = self._note_at_point(col, y)
        if hit is None:
            return
        mode, n = hit
        target = self.project.bgm if mode == "bgm" else self.project.charts[mode]
        target.discard(n)
        self.selection.discard(hit)
        self.changed.emit()
        self.update()

    def _cursor_text(self, event: QMouseEvent) -> str:
        """A readout of the cursor position on the current (snap) grid, prefixed
        by the lane group under it, e.g. ``4K · 마디 3 · 5/16 · 2번``."""
        col = L.column_at(self.columns, event.position().x(), self.lane_w)
        if col is None:
            return ""
        measure, pos = self.pos_at(event.position().y(), True)  # always grid-based
        if measure < 0 or measure >= self.project.measures:
            return ""
        div = self.grid_main.denominator          # cells per measure
        cell = int(pos / self.grid_main)          # cell index within the measure
        if col.kind == "bgm":
            return f"BGM · 마디 {measure} · {cell}/{div}"
        label = DISPLAY_LABELS.get(col.key_mode, "")
        return f"{label} · 마디 {measure} · {cell}/{div} · {col.lane + 1}번"

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.cursor_info.emit(self._cursor_text(event))
        if self.mode == "edit" and self._drag_start is not None:
            self._drag_cur = (event.position().x(), event.position().y())
            self.update()
            return
        if self.mode == "add":
            if self._add_drag is not None:
                self._grow_add_drag(event)   # dragging out a long note
                return
            hover = self._resolve(event)
            if hover != self._hover:
                self._hover = hover
                self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.cursor_info.emit("")
        if self._hover is not None:
            self._hover = None
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.setFocus()
        x, y = event.position().x(), event.position().y()
        col = L.column_at(self.columns, x, self.lane_w)

        if self.mode == "add":
            resolved = self._resolve(event)
            if resolved is None:
                return
            c, measure, pos = resolved
            if event.button() == Qt.LeftButton:
                # Left click drops a note; dragging up/down before release turns
                # it into a long note spanning the dragged range.
                note = self._add_note(c, measure, pos)
                mode_key = "bgm" if c.kind == "bgm" else c.key_mode
                self._add_drag = (mode_key, note, measure + pos, c)
                self._hover = None
            elif event.button() == Qt.RightButton:
                self._erase_at(c, y)                      # right = erase (whole note)
            return

        # edit mode
        if event.button() == Qt.RightButton:
            if col is not None:
                self._erase_at(col, y)
            return
        if event.button() != Qt.LeftButton:
            return
        self._paste_anchor = self.absolute_at(y)
        self._drag_start = (x, y)
        self._drag_cur = (x, y)
        self._drag_shift = bool(event.modifiers() & Qt.ShiftModifier)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._add_drag is not None:      # finished dragging out a long note
            self._add_drag = None
            self.update()
            return
        if self.mode != "edit" or self._drag_start is None:
            self._drag_start = self._drag_cur = None
            return
        x0, y0 = self._drag_start
        x1, y1 = self._drag_cur
        moved = abs(x1 - x0) > 4 or abs(y1 - y0) > 4
        if moved:
            found = self._notes_in_rect(x0, y0, x1, y1)
            self.selection = (self.selection | found) if self._drag_shift else found
        else:
            col = L.column_at(self.columns, x0, self.lane_w)
            hit = self._note_at_point(col, y0) if col else None
            if hit is not None:
                if self._drag_shift:
                    self.selection ^= {hit}
                else:
                    self.selection = {hit}
            elif not self._drag_shift:
                self.selection = set()
        self._drag_start = self._drag_cur = None
        self.update()

    def _notes_in_rect(self, x0, y0, x1, y1):
        rx0, rx1 = sorted((x0, x1))
        ry0, ry1 = sorted((y0, y1))
        colx = self._col_x()
        bgmx = self.columns[0].x
        found = set()

        def inside(cx, absolute):
            cxc = cx + self.lane_w / 2
            cyc = self.y_for(absolute)
            return rx0 <= cxc <= rx1 and ry0 <= cyc <= ry1

        for mode, chart in self.project.charts.items():
            for n in chart:
                cx = colx.get((mode, n.lane))
                if cx is not None and inside(cx, n.absolute):
                    found.add((mode, n))
        for n in self.project.bgm:
            if inside(bgmx, n.absolute):
                found.add(("bgm", n))
        return found

    def wheelEvent(self, event) -> None:  # noqa: N802
        step = 1 if event.angleDelta().y() > 0 else -1
        if event.modifiers() & Qt.ControlModifier:
            self.zoom_step.emit(step)          # vertical zoom
            event.accept()
        elif event.modifiers() & Qt.AltModifier:
            self.lane_zoom_step.emit(step)     # horizontal (lane width) zoom
            event.accept()
        else:
            event.ignore()  # let the scroll area scroll

    # -- edit-mode keyboard operations ------------------------------------- #

    def _record_press(self, key: int, lane: int) -> None:
        """Start recording a note at the playhead in the selected key mode's
        lane. Held long enough (a key hold), it grows into a long note."""
        if self.playhead is None:
            return
        measure, pos = self.playhead_cell(self.snap_on)
        if measure < 0 or measure >= self.project.measures:
            return
        note = Note(measure, pos, lane)
        self.project.charts[self.selected_km].add(note)
        self._rec_pending[key] = (self.selected_km, note, measure + pos)
        self.changed.emit()
        self.update()

    def _grow_record(self, key: int) -> None:
        """Extend a held note to the current playhead (called on auto-repeat and
        on release)."""
        entry = self._rec_pending.get(key)
        if entry is None or self.playhead is None:
            return
        km, note, start_abs = entry
        measure, pos = self.playhead_cell(self.snap_on)
        end_abs = measure + pos
        if end_abs <= start_abs:
            return
        new = self._relen(self.project.charts[km], note, start_abs, end_abs - start_abs)
        self._rec_pending[key] = (km, new, start_abs)
        self.changed.emit()
        self.update()

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        # Finish a held recording note: its final length is the release time.
        if not event.isAutoRepeat() and event.key() in self._rec_pending:
            self._grow_record(event.key())
            self._rec_pending.pop(event.key(), None)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Live recording: while playing, mapped keys drop a note at the playhead
        # in the selected key mode's lane (any edit/add mode). Holding a key grows
        # it into a long note; auto-repeat presses extend it rather than spamming.
        if self.live_playing:
            lane = RECORD_KEYS.get(self.selected_km, {}).get(event.key())
            if lane is not None:
                if event.isAutoRepeat():
                    self._grow_record(event.key())
                else:
                    self._record_press(event.key(), lane)
                event.accept()
                return
        if self.mode != "edit":
            super().keyPressEvent(event)
            return
        key, mod = event.key(), event.modifiers()
        ctrl = bool(mod & Qt.ControlModifier)
        if key == Qt.Key_Delete:
            self._delete_selection()
        elif ctrl and key == Qt.Key_C:
            self._copy_selection(cut=False)
        elif ctrl and key == Qt.Key_X:
            self._copy_selection(cut=True)
        elif ctrl and key == Qt.Key_V:
            self._paste()
        elif key == Qt.Key_Left:
            self._move_selection(-1, 0)
        elif key == Qt.Key_Right:
            self._move_selection(1, 0)
        elif key == Qt.Key_Up:
            self._move_selection(0, 1)
        elif key == Qt.Key_Down:
            self._move_selection(0, -1)
        elif key in (Qt.Key_QuoteLeft, Qt.Key_AsciiTilde):
            self._flip_selection()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _target_for(self, mode):
        return self.project.bgm if mode == "bgm" else self.project.charts[mode]

    def _delete_selection(self) -> None:
        if not self.selection:
            return
        for mode, n in self.selection:
            self._target_for(mode).discard(n)
        self.selection = set()
        self.changed.emit()
        self.update()

    def _move_selection(self, d_lane: int, d_cells: int) -> None:
        if not self.selection:
            return
        step = self.grid_main
        new_sel = set()
        for mode, n in self.selection:
            self._target_for(mode).discard(n)
        for mode, n in self.selection:
            new_abs = n.absolute + d_cells * step
            new_abs = max(Fraction(0), min(Fraction(self.project.measures) - step, new_abs))
            measure = int(new_abs)
            pos = new_abs - measure
            if mode == "bgm":
                lane = 0
            else:
                lane = min(max(n.lane + d_lane, 0), lanes_for(mode) - 1)
            moved = Note(measure, pos, lane, n.length)
            self._target_for(mode).add(moved)
            new_sel.add((mode, moved))
        self.selection = new_sel
        self.changed.emit()
        self.update()

    def _flip_selection(self) -> None:
        if not self.selection:
            return
        new_sel = set()
        for mode, n in self.selection:
            self._target_for(mode).discard(n)
        for mode, n in self.selection:
            if mode == "bgm":
                flipped = n
            else:
                lane = lanes_for(mode) - 1 - n.lane
                flipped = Note(n.measure, n.pos, lane, n.length)
            self._target_for(mode).add(flipped)
            new_sel.add((mode, flipped))
        self.selection = new_sel
        self.changed.emit()
        self.update()

    def _copy_selection(self, cut: bool) -> None:
        if not self.selection:
            return
        anchor = min(n.absolute for _mode, n in self.selection)
        self._clipboard = [(mode, n.absolute - anchor, n.lane, n.length)
                           for mode, n in self.selection]
        if cut:
            self._delete_selection()

    def _paste(self) -> None:
        if not self._clipboard:
            return
        anchor = Fraction(self._paste_anchor).limit_denominator(192)
        new_sel = set()
        for mode, d_abs, lane, length in self._clipboard:
            new_abs = anchor + d_abs
            if new_abs < 0 or new_abs >= self.project.measures:
                continue
            measure = int(new_abs)
            pos = new_abs - measure
            note = Note(measure, pos, lane, length)
            self._target_for(mode).add(note)
            new_sel.add((mode, note))
        if new_sel:
            self.selection = new_sel
            self.changed.emit()
            self.update()


class LaneHeader(QWidget):
    """A thin fixed strip above the canvas showing group labels, kept aligned
    with the canvas as it scrolls horizontally."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lane_w = L.LANE_W
        self.columns, self.groups, self._width = L.build_layout(self.lane_w)
        self.x_offset = 0
        self.selected_km = KEY_MODES[0]
        self.setFixedHeight(26)
        # Don't force the full content width onto the layout — the header scrolls
        # with the canvas (via x_offset) and clips, so a small minimum keeps the
        # sidebar from being pushed off-screen.
        self.setMinimumWidth(0)

    def set_lane_width(self, lane_w: int) -> None:
        self.lane_w = lane_w
        self.columns, self.groups, self._width = L.build_layout(self.lane_w)
        self.update()

    def set_x_offset(self, value: int) -> None:
        self.x_offset = value
        self.update()

    def set_selected_km(self, key_mode: int) -> None:
        self.selected_km = key_mode
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), C_GROUP_BG_A)
        p.translate(-self.x_offset, 0)
        font = QFont()
        font.setBold(True)
        font.setPointSize(9)
        p.setFont(font)
        sel_label = DISPLAY_LABELS.get(self.selected_km)
        for g in self.groups:
            selected = g.label == sel_label
            if selected:
                p.fillRect(QRect(g.x0, 0, g.x1 - g.x0, self.height()), C_SELECT_TINT)
                p.fillRect(QRect(g.x0, self.height() - 2, g.x1 - g.x0, 2), C_SELECT)
            p.setPen(QPen(C_SELECT if selected else C_TEXT, 1))
            p.drawText(QRect(g.x0, 0, g.x1 - g.x0, self.height()),
                       Qt.AlignCenter, g.label)
        p.setPen(QPen(C_GROUP_SEP, 1))
        for g in self.groups:
            p.drawLine(g.x1 + L.GROUP_GAP // 2, 4, g.x1 + L.GROUP_GAP // 2, self.height() - 4)
        p.end()
