"""The scrollable chart canvas and its fixed lane-label header."""

from __future__ import annotations

import bisect
import time
from fractions import Fraction
from typing import Optional

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen, QPolygon
from PySide6.QtWidgets import QWidget

from ..model import (
    DISPLAY_LABELS,
    DISPLAY_MODES,
    KEY_MODES,
    LANE_COLORS,
    Note,
    Project,
    lanes_for,
)
from ..timing import TimeMap
from . import layout as L
from . import palette

# Colours (dark theme). Colours shared with the Qt theme come from ``palette``
# so the canvas and the surrounding chrome can never drift apart; the rest are
# canvas-only and owned here.
C_BG = QColor(palette.CANVAS)
C_GROUP_BG_A = QColor("#23232b")
C_GROUP_BG_B = QColor("#1b1b21")
C_MEASURE = QColor("#8a8a99")
C_GRID_FINE = QColor("#313139")   # fine snap grid (faint)
C_GRID_REF = QColor("#5a5a76")    # reference grid (brighter, drawn on top)
C_LANE_SEP = QColor(palette.BORDER)
C_GROUP_SEP = QColor("#55556a")
C_TEXT = QColor("#c8c8d0")
C_NOTE_WHITE = QColor("#eef0f4")
C_NOTE_BLUE = QColor("#5aa0ff")
C_NOTE_GREY = QColor("#9aa0ac")
C_NOTE_BGM = QColor("#ffb347")
C_PLAYHEAD = QColor("#ff4d6d")
C_CONFLICT = QColor("#ff4d4d")   # notes that overlap another note in the same lane
C_BPM = QColor("#c792ea")        # mid-song tempo-change markers
C_BPM_FAST = QColor(255, 70, 70, 26)   # faint tint where a segment is faster than base
C_BPM_SLOW = QColor(70, 130, 255, 26)  # faint tint where a segment is slower than base
C_WAVE = QColor(120, 180, 255, 70)  # BGM waveform in the BGM lane

# Note colour by lane code.
NOTE_COLOR = {"W": C_NOTE_WHITE, "B": C_NOTE_BLUE, "G": C_NOTE_GREY}
# Pale lane background tint by lane code (kept faint so the grid stays visible).
LANE_TINT = {
    "W": QColor(255, 255, 255, 12),
    "B": QColor(90, 160, 255, 26),
    "G": QColor(150, 160, 175, 20),
}

FREE_DIV = 192  # placement resolution when snap is off / Shift held

# Global left-to-right lane order across the key modes (4K, 6K, LOAD), so
# arrow-key moves can carry a note across mode boundaries as one continuous
# space. Index 0 = 4K lane 0 … last = LOAD lane 7.
_GLOBAL_LANES = [(km, lane) for km in DISPLAY_MODES for lane in range(lanes_for(km))]
_GLOBAL_INDEX = {ml: i for i, ml in enumerate(_GLOBAL_LANES)}

HIT_FLASH_SEC = 0.18   # how long a note's hit flash lasts
HIT_MAX_STEP = 0.15    # ignore playhead jumps bigger than this (seeks, not playback)

C_SELECT = QColor(palette.ACCENT)       # accent for the selected key mode
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
    scroll_h = Signal(int)      # Shift+wheel: horizontal scroll by this angle delta
    seek_requested = Signal(float)  # set playback to this absolute chart position
    overlap_warning = Signal()      # a move left the selection overlapping another note

    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.project = project
        self.measure_px = 150         # zoom: vertical pixels per measure
        self.lane_w = L.LANE_W        # zoom: horizontal pixels per key lane
        self.bgm_w = 64               # BGM lane width (draggable, wider for the waveform)
        self.grid_main = Fraction(1, 16)  # primary (snap) grid, of a measure
        self.grid_sub = Fraction(1, 12)   # secondary reference grid
        self.snap_on = True
        self.v_pad = 24
        self.playhead: Optional[float] = None  # absolute chart pos, or None
        self._hits = {}               # (mode, Note) -> monotonic time it crossed the playhead
        self.record_offset_measures = 0.0  # recording latency comp, in measures
        self._wave = None             # normalised peak envelope (numpy array)
        self._wave_bps = 200          # buckets per second in _wave
        self._wave_tm = None          # cached TimeMap for chart<->audio mapping
        self.show_waveform = True
        self.live_playing = False     # True while the preview is actively playing
        self.selected_km = KEY_MODES[0]  # key mode being recorded / highlighted
        self._hover = None            # (Column, measure, Fraction pos) or None
        self.mode = "add"             # "add" (F3) or "edit" (F2)
        self.selection = set()        # {(mode, Note)} ; mode is int or "bgm"
        self._overlap_warned = False  # debounce: warn once per continuous overlap
        self._clipboard = None        # [(mode, Fraction d_abs, lane)]
        self._drag_start = None       # (x, y) rubber-band anchor (empty-area drag)
        self._drag_cur = None
        self._drag_shift = False
        self._paste_anchor = 0.0
        self._move_drag = None        # {origs, px, py, moved} while dragging notes to move them
        self._add_drag = None         # (mode_key, Note, start_abs, Column) while dragging a long note
        self._rec_pending = {}        # key -> (km, Note, start_abs) for keys held during recording
        self._len_drag = None         # {mode, note, end:'head'|'tail'} while dragging a long-note endpoint
        self._scale_drag = None       # {measure, y0, start_cells, min_cells, active} while resizing a measure
        self._vprefix = [0.0]         # cumulative display heights per measure (built in _apply_size)
        self._vtotal = 0.0            # total display height in measure units
        # Undo/redo: snapshots of the project's editable state, coalesced by a
        # settle timer so bursts (drags, recording) collapse to one step.
        self._undo_stack = []
        self._redo_stack = []
        self._committed = None
        self._restoring = False
        self._undo_limit = 300
        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.timeout.connect(self._flush_history)
        self._colx = {}
        self._build_layout()
        # Paint caches, rebuilt on edit (not per paint): overlap flags plus a
        # by-measure index so painting only touches notes near the viewport.
        self._conflicts = set()          # {(km, Note)} overlapping notes
        self._taps_by_measure = {}       # km -> {measure -> [tap Note]}
        self._longs = {}                 # km -> [long Note]
        self._bgm_by_measure = {}        # measure -> [BGM Note]
        self.changed.connect(self._rebuild_caches)
        self.changed.connect(self._schedule_history)
        self._committed = self.project.snapshot()
        self._rebuild_caches()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._apply_size()

    # -- geometry ----------------------------------------------------------- #

    def _build_layout(self) -> None:
        """(Re)build the column/group layout and cache the key-lane x lookup so
        paints and hit-tests don't rebuild that dict on every call."""
        self.columns, self.groups, self._width = L.build_layout(self.lane_w, self.bgm_w)
        self._colx = {(c.key_mode, c.lane): c.x
                      for c in self.columns if c.kind == "key"}

    def _rebuild_scale_prefix(self) -> None:
        """Cache the cumulative display height (in measure units) up to the start
        of each measure, honouring per-measure display scales. A measure with
        scale s occupies s * measure_px pixels; the collapsed tail (1 - s) takes
        no space. Built here so y_for/absolute_at stay O(1) via prefix sums."""
        measures = self.project.measures
        scales = self.project.measure_scales
        prefix = [0.0] * (measures + 1)
        for m in range(measures):
            s = scales.get(m)
            prefix[m + 1] = prefix[m] + (float(s) if s is not None else 1.0)
        self._vprefix = prefix
        self._vtotal = prefix[measures]

    def _scale_of(self, m: int) -> float:
        if 0 <= m < self.project.measures:
            s = self.project.measure_scales.get(m)
            return float(s) if s is not None else 1.0
        return 1.0

    def _apply_size(self) -> None:
        self._rebuild_scale_prefix()
        height = self._vtotal * self.measure_px + 2 * self.v_pad
        self.setFixedSize(self._width, int(height))
        self.updateGeometry()

    def set_zoom(self, measure_px: int) -> None:
        self.measure_px = max(40, min(600, measure_px))
        self._apply_size()
        self.update()

    def set_lane_width(self, lane_w: int) -> None:
        self.lane_w = max(14, min(80, lane_w))
        self._build_layout()
        self._apply_size()
        self.update()

    def set_bgm_width(self, bgm_w: int) -> None:
        self.bgm_w = max(20, min(400, int(bgm_w)))
        self._build_layout()
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

    def set_waveform(self, peaks, buckets_per_sec: int) -> None:
        self._wave = peaks
        self._wave_bps = buckets_per_sec
        self._wave_tm = TimeMap(self.project)
        self.update()

    def set_show_waveform(self, on: bool) -> None:
        self.show_waveform = on
        self.update()

    def set_live(self, on: bool) -> None:
        """Toggle live tap-along: while on, a left click in add mode drops a
        note at the current playhead time (in the clicked lane) instead of at
        the cursor's vertical position."""
        self.live_playing = on
        if not on:
            self._rec_pending.clear()  # stop growing any held-key long notes
            self._hits.clear()
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

    def _display_pos(self, absolute: float) -> float:
        """Absolute position -> display position (measure units) after collapsing
        the hidden tail of any scaled measure. Positions inside a collapsed tail
        map to the measure's top edge."""
        a = float(absolute)
        m = int(a)
        if m < 0:
            return a
        if m >= self.project.measures:
            return self._vtotal + (a - self.project.measures)
        return self._vprefix[m] + min(a - m, self._scale_of(m))

    def y_for(self, absolute: float) -> float:
        """Pixel y for an absolute position in measures (0 = song start)."""
        return self.v_pad + (self._vtotal - self._display_pos(absolute)) * self.measure_px

    def absolute_at(self, y: float) -> float:
        v = self._vtotal - (y - self.v_pad) / self.measure_px
        if v <= 0.0:
            return 0.0
        if v >= self._vtotal:
            return float(self.project.measures) + (v - self._vtotal)
        m = bisect.bisect_right(self._vprefix, v) - 1
        m = max(0, min(self.project.measures - 1, m))
        return max(0.0, m + (v - self._vprefix[m]))

    # -- per-measure display length ---------------------------------------- #

    def _base_cells(self) -> int:
        """Grid cells in a full (unscaled) measure."""
        return max(1, int(round(1.0 / float(self.grid_main))))

    def _current_cells(self, m: int) -> int:
        """Visible grid cells in measure ``m`` at the current scale."""
        return max(1, int(round(self._scale_of(m) * self._base_cells())))

    def _measure_min_cells(self, m: int) -> int:
        """Fewest cells measure ``m`` can shrink to while still showing every
        note/BGM it holds (can't collapse a cell that has an object in it)."""
        step = self.grid_main
        max_ext = Fraction(0)
        def consider(n):
            nonlocal max_ext
            ext = min(Fraction(1), n.end_absolute - m) if n.is_long else n.pos + step
            if ext > max_ext:
                max_ext = ext
        for chart in self.project.charts.values():
            for n in chart:
                if n.measure == m:
                    consider(n)
        for n in self.project.bgm:
            if n.measure == m:
                consider(n)
        if max_ext <= 0:
            return 1
        import math
        cells = math.ceil(float(max_ext) / float(step) - 1e-9)
        return max(1, min(self._base_cells(), cells))

    def _apply_scale_drag(self, event: QMouseEvent) -> None:
        d = self._scale_drag
        dy = d["y0"] - event.position().y()      # drag up = positive = grow taller
        if not d["active"] and abs(dy) < 4:
            return                               # still might be a plain click
        d["active"] = True
        cell_px = max(1.0, self.measure_px * float(self.grid_main))
        cells = d["start_cells"] + int(round(dy / cell_px))
        cells = max(d["min_cells"], min(self._base_cells(), cells))
        if cells != self._current_cells(d["measure"]):
            self._set_measure_cells(d["measure"], cells)
        self.cursor_info.emit(f"마디 {d['measure']} · {cells}칸")

    def _set_measure_cells(self, m: int, cells: int) -> None:
        base = self._base_cells()
        # Never collapse below the notes already in the measure, or above full.
        cells = max(self._measure_min_cells(m), min(base, cells))
        if cells >= base:
            self.project.measure_scales.pop(m, None)
        else:
            self.project.measure_scales[m] = Fraction(cells) * self.grid_main
        self._apply_size()
        self.changed.emit()          # dirty + coalesced undo entry
        self.update()

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
        """Return (measure, Fraction pos) for the current playhead position,
        shifted back by the recording offset (to compensate for reaction/audio
        latency). When ``snap`` the time is quantised to the *nearest* primary
        grid line; otherwise it keeps a fine free resolution."""
        ph = max(0.0, (self.playhead or 0.0) - self.record_offset_measures)
        frac = Fraction(ph).limit_denominator(FREE_DIV)
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
        self._paint_bpm_regions(p)
        self._paint_waveform(p)
        self._paint_horizontal_lines(p)
        self._paint_separators(p)
        self._paint_selected_group(p)
        self._paint_notes(p)
        self._paint_hits(p)
        self._paint_selection(p)
        self._paint_hover(p)
        self._paint_drag(p)
        self._paint_bpm_changes(p)
        self._paint_playhead(p)
        p.end()

    def _paint_hits(self, p: QPainter) -> None:
        """Flash notes that just crossed the playhead — a bright pop that fades.
        A long note keeps its flash refreshed for as long as the playhead sits
        inside its body, so the effect lasts the whole hold and only fades once
        it's released."""
        now = time.monotonic()
        # Keep held long notes lit: refresh their flash every frame while the
        # playhead is between the head and the tail.
        ph = self.playhead
        if self.live_playing and ph is not None:
            for km, longs in self._longs.items():
                for n in longs:
                    if n.absolute <= ph <= n.end_absolute:
                        self._hits[(km, n)] = now
        if not self._hits:
            return
        bgmx = self.columns[0].x
        colx = self._col_x()
        p.setBrush(Qt.NoBrush)
        expired = []
        for (mode, n), t in self._hits.items():
            inten = 1.0 - (now - t) / HIT_FLASH_SEC
            if inten <= 0:
                expired.append((mode, n))
                continue
            if mode == "bgm":
                x, w = bgmx, self.bgm_w
            else:
                x, w = colx.get((mode, n.lane)), self.lane_w
            if x is None:
                continue
            is_long = mode != "bgm" and n.is_long
            # Long notes glow over their whole body; taps pop from the head cell.
            rect = self._sel_rect(x, n, w) if is_long else self._note_rect(x, n.absolute, w)
            grow = int(6 * inten)          # the pop expands outward as it fades
            r = rect.adjusted(-grow, -grow, grow, grow)
            fill_a = int((70 if is_long else 150) * inten)
            p.fillRect(r, QColor(255, 255, 255, fill_a))
            p.setPen(QPen(QColor(255, 255, 255, int(230 * inten)), 2))
            p.drawRect(r)
        for k in expired:
            del self._hits[k]

    def _paint_waveform(self, p: QPainter) -> None:
        """Draw the BGM's amplitude envelope down the BGM lane, mapped through
        the time map so it lines up with the notes even across tempo changes."""
        if (self._wave is None or not self.show_waveform
                or self._wave_tm is None or len(self._wave) == 0):
            return
        peaks = self._wave
        bps = self._wave_bps
        tm = self._wave_tm
        n = len(peaks)
        cx = self.columns[0].x + self.bgm_w / 2.0
        halfw = self.bgm_w * 0.46
        lo = max(int(self.v_pad), int(self._vis_lo))
        hi = min(int(self.height() - self.v_pad), int(self._vis_hi))
        p.setPen(QPen(C_WAVE, 1))
        for y in range(lo, hi, 2):
            absolute = self.absolute_at(y)
            idx = int(tm.audio_seconds(absolute) * bps)
            if 0 <= idx < n:
                a = float(peaks[idx]) * halfw
                if a > 0.5:
                    p.drawLine(int(cx - a), y, int(cx + a), y)

    def _paint_bpm_regions(self, p: QPainter) -> None:
        """Faintly tint each tempo segment across the note lanes — red where it's
        faster than the base BPM, blue where slower. Painted under the grid,
        waveform and notes (with very low alpha) so nothing gets obscured."""
        changes = self.project.bpm_changes
        if not changes:
            return
        base = self.project.bpm
        x0 = self.groups[0].x0
        x1 = self.groups[-1].x1
        end = float(self.project.measures)
        items = sorted(changes.items())
        for i, (pos, bpm) in enumerate(items):
            if bpm > base:
                col = C_BPM_FAST
            elif bpm < base:
                col = C_BPM_SLOW
            else:
                continue                          # same as base -> no tint
            lo = float(pos)
            hi = float(items[i + 1][0]) if i + 1 < len(items) else end
            if hi <= lo:
                continue
            y_top = int(self.y_for(hi))           # later position = smaller y
            y_bot = int(self.y_for(lo))
            if y_bot < self._vis_lo or y_top > self._vis_hi:
                continue                          # outside the exposed strip
            p.fillRect(QRect(x0, y_top, x1 - x0, y_bot - y_top), col)

    def _paint_bpm_changes(self, p: QPainter) -> None:
        if not self.project.bpm_changes:
            return
        x0 = L.LEFT_MARGIN
        x1 = self.groups[-1].x1
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        for pos, bpm in self.project.bpm_changes.items():
            y = int(self.y_for(float(pos)))
            if y < self._vis_lo - 20 or y > self._vis_hi + 20:
                continue
            # A solid line across the lanes marks exactly where the tempo changes.
            p.setPen(QPen(C_BPM, 2))
            p.drawLine(x0, y, x1, y)
            # A clear pill just above the line naming the new tempo.
            label = f"♩ {bpm:g} BPM"
            p.setFont(font)
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(label) + 12
            tag = QRect(x0 + 2, y - 19, tw, 17)
            p.setPen(Qt.NoPen)
            p.setBrush(C_BPM)
            p.drawRoundedRect(tag, 4, 4)
            p.setPen(QPen(C_GROUP_BG_B, 1))     # dark ink on the accent pill
            p.drawText(tag, Qt.AlignCenter, label)

    def set_playhead(self, absolute: Optional[float]) -> None:
        prev = self.playhead
        self.playhead = absolute
        # Flash notes the playhead just crossed (only during smooth playback —
        # skip big jumps from seeking).
        if (self.live_playing and absolute is not None and prev is not None
                and 0 < absolute - prev <= HIT_MAX_STEP):
            self._register_hits(prev, absolute)
        self.update()

    def _register_hits(self, prev: float, cur: float) -> None:
        now = time.monotonic()
        for m in range(int(prev), int(cur) + 1):
            for km, chart in self.project.charts.items():
                for n in self._taps_by_measure.get(km, {}).get(m, ()):
                    if prev < n.absolute <= cur:
                        self._hits[(km, n)] = now
            for n in self._bgm_by_measure.get(m, ()):
                if prev < n.absolute <= cur:
                    self._hits[("bgm", n)] = now
        for km, longs in self._longs.items():
            for n in longs:
                if prev < n.absolute <= cur:
                    self._hits[(km, n)] = now

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

    def _grid_line_ys(self, step: Fraction, m_lo: int, m_hi: int):
        """Yield pixel y for every grid line at ``step`` spacing within measures
        ``[m_lo, m_hi)`` (skipping the measure line at k=0, drawn separately).
        Cells stay the same size; a scaled measure just shows fewer of them, so
        we stop at that measure's visible fraction."""
        step_f = float(step)
        for m in range(m_lo, m_hi):
            limit = self._scale_of(m)
            k = 1
            while k * step_f < limit - 1e-9:
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

        # Only measures overlapping the exposed strip can contribute visible
        # lines, so iterate just those (O(viewport)) instead of the whole song.
        # draw_row still culls exactly; this range is a strict superset of what
        # it accepts, so the drawn output is unchanged. (+/-1 measure of margin.)
        abs_top = self.absolute_at(lo)          # smaller y = larger position
        abs_bot = self.absolute_at(hi)
        gm_lo = max(0, int(abs_bot) - 1)
        gm_hi = min(measures, int(abs_top) + 2)   # exclusive bound for cell grids

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
        for y in self._grid_line_ys(self.grid_main, gm_lo, gm_hi):
            draw_row(y)
        p.setPen(QPen(C_GRID_REF, 1))
        for y in self._grid_line_ys(self.grid_sub, gm_lo, gm_hi):
            draw_row(y)

        # Measure lines + numbers (measure `measures` is the final top boundary).
        for m in range(gm_lo, min(measures, gm_hi) + 1):
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

    def _note_rect(self, x: int, absolute: float, w: Optional[int] = None) -> QRect:
        """A note fills the grid cell above its timing line, sized to the
        primary grid so it fits the grid exactly at any zoom level."""
        if w is None:
            w = self.lane_w
        cell_h = max(3, int(round(self.measure_px * float(self.grid_main))))
        y_line = int(round(self.y_for(absolute)))
        return QRect(x + 1, y_line - cell_h + 1, w - 2, cell_h - 1)

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
        if self._wave is not None:
            self._wave_tm = TimeMap(self.project)   # BPM/BGM may have changed

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
                p.fillRect(self._note_rect(bgm_x, n.absolute, self.bgm_w), C_NOTE_BGM)
        col_index = self._colx
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
        rect = self._note_rect(col.x, measure + pos, col.w)
        p.setPen(QPen(QColor(255, 255, 255, 130), 1))
        p.setBrush(self._hover_color(col))
        p.drawRect(rect)
        p.setBrush(Qt.NoBrush)

    def _col_x(self):
        return self._colx   # cached; rebuilt in _build_layout on any layout change

    def _sel_rect(self, x: int, note: Note, w: Optional[int] = None) -> QRect:
        """Bounding outline for a note — the head cell for a tap, or the whole
        span from tail cap to head line for a long note."""
        head = self._note_rect(x, note.absolute, w)
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
            w = self.bgm_w if mode == "bgm" else self.lane_w
            p.drawRect(self._sel_rect(x, n, w))

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
        col = L.column_at(self.columns, event.position().x())
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
            self.project.add_object("bgm", note)
        else:
            note = Note(measure, pos, col.lane)
            self.project.add_object(col.key_mode, note)
        self.changed.emit()
        self.update()
        return note

    def _relen(self, mode, note: Note, lo: Fraction, length: Fraction) -> Note:
        """Replace ``note`` in lane ``mode`` with one starting at absolute ``lo``
        with the given ``length`` (0 collapses it back to a tap). Returns the
        new note."""
        self.project.remove_object(mode, note)
        measure = int(lo)
        pos = lo - measure
        new = Note(measure, pos, note.lane, length) if length > 0 \
            else Note(measure, pos, note.lane)
        self.project.add_object(mode, new)
        return new

    def _grow_add_drag(self, event: QMouseEvent) -> None:
        mode_key, note, start_abs, col = self._add_drag
        if col.kind == "bgm":
            return  # BGM markers stay taps
        measure, pos = self.pos_at(event.position().y(), self._snap_now(event))
        end_abs = measure + pos
        lo = min(start_abs, end_abs)
        hi = min(max(start_abs, end_abs), Fraction(self.project.measures))
        new = self._relen(mode_key, note, lo, hi - lo)
        self._add_drag = (mode_key, new, start_abs, col)
        self.changed.emit()
        self.update()

    def _apply_move_drag(self, event: QMouseEvent) -> None:
        """Reposition the dragged selection by the grid-snapped delta from the
        press point. Always recomputed from the captured originals, so dragging
        back and forth doesn't accumulate error."""
        md = self._move_drag
        dx = event.position().x() - md["px"]
        step = self.grid_main
        d_lane = int(round(dx / self.lane_w))
        # Track the cursor's actual chart position (via absolute_at, which honours
        # per-measure display scales) so dragging stays 1:1 with the mouse even
        # through collapsed measures; snap that delta to the grid.
        d_abs_raw = self.absolute_at(event.position().y()) - self.absolute_at(md["py"])
        d_cells = int(round(d_abs_raw / float(step)))
        if not md["moved"] and d_lane == 0 and d_cells == 0:
            return
        d_abs = d_cells * step
        limit = Fraction(self.project.measures) - step
        # Remove the currently-placed (possibly already-moved) notes first.
        for mode, n in self.selection:
            self.project.remove_object(mode, n)
        new_sel = set()
        for mode, orig in md["origs"]:
            new_abs = max(Fraction(0), min(limit, orig.absolute + d_abs))
            measure = int(new_abs)
            pos = new_abs - measure
            if mode == "bgm":
                lane = 0
            else:
                lane = min(max(orig.lane + d_lane, 0), lanes_for(mode) - 1)
            moved = Note(measure, pos, lane, orig.length)
            self.project.add_object(mode, moved)
            new_sel.add((mode, moved))
        self.selection = new_sel
        md["moved"] = True
        self.changed.emit()
        self.update()

    def _endpoint_near(self, note: Note, y: float):
        """Return 'head' or 'tail' if ``y`` is near that end of a long note, else
        None — used to grab an endpoint for length editing."""
        cell_h = max(3.0, self.measure_px * float(self.grid_main))
        thresh = max(8.0, cell_h * 0.6)
        head_y = self.y_for(note.absolute)          # bottom (start)
        tail_y = self.y_for(note.end_absolute)      # top (end)
        dh, dt = abs(y - head_y), abs(y - tail_y)
        if min(dh, dt) > thresh:
            return None
        return "tail" if dt <= dh else "head"

    def _apply_len_drag(self, event: QMouseEvent) -> None:
        """Resize a long note by dragging its head or tail to the cursor."""
        ld = self._len_drag
        mode, note = ld["mode"], ld["note"]
        measure, pos = self.pos_at(event.position().y(), self._snap_now(event))
        cur = Fraction(measure) + pos
        step = self.grid_main
        head, tail = note.absolute, note.end_absolute
        if ld["end"] == "tail":
            new_end = max(head + step, min(cur, Fraction(self.project.measures)))
            new = self._relen(mode, note, head, new_end - head)
        else:
            new_head = max(Fraction(0), min(cur, tail - step))
            new = self._relen(mode, note, new_head, tail - new_head)
        ld["note"] = new
        self.selection = {(mode, new)}
        self.changed.emit()
        self.update()

    def _erase_at(self, col, y: float) -> None:
        hit = self._note_at_point(col, y)
        if hit is None:
            return
        mode, n = hit
        self.project.remove_object(mode, n)
        self.selection.discard(hit)
        self.changed.emit()
        self.update()

    def _cursor_text(self, event: QMouseEvent) -> str:
        """A readout of the cursor position on the current (snap) grid, prefixed
        by the lane group under it, e.g. ``4K · 마디 3 · 5/16 · 2번``."""
        col = L.column_at(self.columns, event.position().x())
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
        if self._scale_drag is not None:
            self._apply_scale_drag(event)
            return
        # Hovering the left ruler hints that measures can be resized there.
        if event.position().x() < L.LEFT_MARGIN:
            self.setCursor(Qt.SplitVCursor)
        else:
            self.unsetCursor()
        self.cursor_info.emit(self._cursor_text(event))
        if self.mode == "edit" and self._len_drag is not None:
            self._apply_len_drag(event)
            return
        if self.mode == "edit" and self._move_drag is not None:
            self._apply_move_drag(event)
            return
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
        self.unsetCursor()
        if self._hover is not None:
            self._hover = None
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.setFocus()
        x, y = event.position().x(), event.position().y()

        # Middle-click anywhere sets the playback position without touching notes.
        if event.button() == Qt.MiddleButton:
            self.seek_requested.emit(self.absolute_at(y))
            return

        # Left button on the measure ruler: a plain click seeks (as before), but
        # dragging up/down resizes that measure's display length (collapsing its
        # empty tail so it shows fewer grid cells). We defer the decision to the
        # release / first drag movement.
        if event.button() == Qt.LeftButton and x < L.LEFT_MARGIN:
            m = int(self.absolute_at(y))
            if 0 <= m < self.project.measures:
                self._scale_drag = {"measure": m, "y0": y, "active": False,
                                    "start_cells": self._current_cells(m),
                                    "min_cells": self._measure_min_cells(m)}
            else:
                self.seek_requested.emit(self.absolute_at(y))
            return

        col = L.column_at(self.columns, x)

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
        self._overlap_warned = False   # a fresh interaction may warn again
        self._drag_shift = bool(event.modifiers() & Qt.ShiftModifier)
        hit = self._note_at_point(col, y) if col is not None else None

        if hit is not None and not self._drag_shift:
            mode, note = hit
            # Near a long note's end → resize it; elsewhere on a note → move.
            if mode != "bgm" and note.is_long:
                end = self._endpoint_near(note, y)
                if end is not None:
                    self.selection = {hit}
                    self._len_drag = {"mode": mode, "note": note, "end": end}
                    self.update()
                    return
            # Press on a note (no Shift) → drag to move it. Grab the whole
            # selection if the note is part of it, otherwise just this note.
            if hit not in self.selection:
                self.selection = {hit}
            self._move_drag = {"origs": list(self.selection),
                               "px": x, "py": y, "moved": False}
            self.update()
            return
        if hit is not None:   # Shift+click a note → toggle it in the selection
            self.selection ^= {hit}
            self.update()
            return
        # Empty area → rubber-band selection.
        self._drag_start = (x, y)
        self._drag_cur = (x, y)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._scale_drag is not None:   # measure-ruler press: drag = resize
            d = self._scale_drag
            self._scale_drag = None
            if not d["active"]:            # no drag -> it was a plain seek click
                self.seek_requested.emit(self.absolute_at(d["y0"]))
            self.update()
            return
        if self._add_drag is not None:      # finished dragging out a long note
            self._add_drag = None
            self.update()
            return
        if self._len_drag is not None:      # finished resizing a long note
            self._len_drag = None
            self.update()
            return
        if self._move_drag is not None:     # finished dragging notes to move them
            self._move_drag = None
            self._flag_overlap(self._selection_overlaps())
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
            col = L.column_at(self.columns, x0)
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

        def inside(cx, w, absolute):
            cxc = cx + w / 2
            cyc = self.y_for(absolute)
            return rx0 <= cxc <= rx1 and ry0 <= cyc <= ry1

        for mode, chart in self.project.charts.items():
            for n in chart:
                cx = colx.get((mode, n.lane))
                if cx is not None and inside(cx, self.lane_w, n.absolute):
                    found.add((mode, n))
        for n in self.project.bgm:
            if inside(bgmx, self.bgm_w, n.absolute):
                found.add(("bgm", n))
        return found

    def wheelEvent(self, event) -> None:  # noqa: N802
        # Holding Alt makes many platforms report the wheel on the X axis, so
        # fall back to it — otherwise angleDelta().y() is 0 and the step would be
        # stuck at -1 (zoom out only).
        d = event.angleDelta()
        delta = d.y() if d.y() != 0 else d.x()
        if delta == 0:
            event.ignore()
            return
        mods = event.modifiers()
        step = 1 if delta > 0 else -1
        if mods & Qt.ControlModifier:
            self.zoom_step.emit(step)          # vertical zoom
            event.accept()
        elif mods & Qt.AltModifier:
            self.lane_zoom_step.emit(step)     # horizontal (lane width) zoom
            event.accept()
        elif mods & Qt.ShiftModifier:
            self.scroll_h.emit(delta)          # horizontal scroll
            event.accept()
        else:
            event.ignore()  # let the scroll area scroll (vertical)

    # -- edit-mode keyboard operations ------------------------------------- #

    def _record_press(self, key: int, lane: int) -> None:
        """Drop a single tap note at the playhead in the selected key mode's
        lane. Holds never become long notes — each press is one tap."""
        if self.playhead is None:
            return
        measure, pos = self.playhead_cell(self.snap_on)
        if measure < 0 or measure >= self.project.measures:
            return
        note = Note(measure, pos, lane)
        self.project.add_object(self.selected_km, note)
        self._rec_pending[key] = note   # held; only used to ignore auto-repeat
        self.changed.emit()
        self.update()

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if not event.isAutoRepeat() and event.key() in self._rec_pending:
            self._rec_pending.pop(event.key(), None)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Live recording: while playing, a mapped key drops one tap note at the
        # playhead in the selected key mode's lane. Auto-repeat (holding) is
        # ignored, so a hold is still just a single tap.
        if self.live_playing:
            lane = RECORD_KEYS.get(self.selected_km, {}).get(event.key())
            if lane is not None:
                if not event.isAutoRepeat():
                    self._record_press(event.key(), lane)
                event.accept()
                return
        if self.mode != "edit":
            super().keyPressEvent(event)
            return
        key, mod = event.key(), event.modifiers()
        ctrl = bool(mod & Qt.ControlModifier)
        if ctrl and key == Qt.Key_A:
            self.select_all()
        elif key == Qt.Key_Delete:
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
        else:
            super().keyPressEvent(event)  # ` (flip) is a configurable window action
            return
        event.accept()

    def _target_for(self, mode):
        return self.project.bgm if mode == "bgm" else self.project.charts[mode]

    # -- undo / redo -------------------------------------------------------- #
    #
    # Edits are coalesced automatically: every ``changed`` (re)starts a short
    # timer, and when it settles the pre-burst state is pushed to the undo
    # stack. So a whole drag / recording burst becomes one undo step without any
    # per-operation begin/commit calls.

    def _schedule_history(self) -> None:
        if self._restoring:
            return
        self._history_timer.start(400)

    def _flush_history(self) -> None:
        """Commit any pending change as one undo entry."""
        if self._committed is None:
            self._committed = self.project.snapshot()
            return
        current = self.project.snapshot()
        if current != self._committed:
            self._undo_stack.append(self._committed)
            if len(self._undo_stack) > self._undo_limit:
                self._undo_stack.pop(0)
            self._redo_stack.clear()
            self._committed = current

    def clear_history(self) -> None:
        self._history_timer.stop()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._committed = self.project.snapshot()

    def _restore(self, snap) -> None:
        self._restoring = True
        self.project.restore(snap)
        self._committed = self.project.snapshot()
        self.selection = set()
        self.changed.emit()
        self.update()
        self._restoring = False

    def undo(self) -> None:
        self._flush_history()   # commit an in-flight edit first
        if not self._undo_stack:
            return
        self._redo_stack.append(self.project.snapshot())
        self._restore(self._undo_stack.pop())

    def redo(self) -> None:
        if not self._redo_stack:
            return
        self._undo_stack.append(self.project.snapshot())
        self._restore(self._redo_stack.pop())

    def _delete_selection(self) -> None:
        if not self.selection:
            return
        for mode, n in self.selection:
            self.project.remove_object(mode, n)
        self.selection = set()
        self.changed.emit()
        self.update()

    def _move_selection(self, d_lane: int, d_cells: int) -> None:
        if not self.selection:
            return
        step = self.grid_main
        hi = Fraction(self.project.measures) - step
        # Compute every note's target first; if ANY would cross a wall (the left
        # edge of 4K, the right edge of LOAD, or the top/bottom), abort the whole
        # move so the selection never gets squished against an edge. Key notes
        # move through 4K→6K→LOAD as one continuous lane space.
        proposed = []
        for mode, n in self.selection:
            new_abs = n.absolute + d_cells * step
            if new_abs < 0 or new_abs > hi:
                return
            if mode == "bgm":
                newmode, newlane = "bgm", 0
            else:
                g = _GLOBAL_INDEX[(mode, n.lane)] + d_lane
                if g < 0 or g >= len(_GLOBAL_LANES):
                    return
                newmode, newlane = _GLOBAL_LANES[g]
            proposed.append((mode, n, newmode, newlane, new_abs))
        # Lift the selected notes out first; the remaining ("stationary") notes
        # are the ones a move must never delete. Notes overlap freely now (charts
        # are lists), so a move can pass through others instead of absorbing them.
        for mode, n, *_ in proposed:
            self.project.remove_object(mode, n)
        stationary = {m: list(c) for m, c in self.project.charts.items()}
        new_sel = set()
        overlap = False
        for mode, n, newmode, newlane, new_abs in proposed:
            measure = int(new_abs)
            pos = new_abs - measure
            moved = Note(measure, pos, newlane, n.length)
            if newmode != "bgm":
                for other in stationary.get(newmode, ()):
                    if other.lane == newlane and self._overlaps(moved, other):
                        overlap = True
                        break
            self.project.add_object(newmode, moved)
            new_sel.add((newmode, moved))
        self.selection = new_sel
        self._flag_overlap(overlap)
        self.changed.emit()
        self.update()

    def _flag_overlap(self, overlap: bool) -> None:
        """Emit a one-shot warning when a move first lands the selection on top
        of another note; stays quiet until the selection leaves the overlap."""
        if overlap:
            if not self._overlap_warned:
                self._overlap_warned = True
                self.overlap_warning.emit()
        else:
            self._overlap_warned = False

    def _selection_overlaps(self) -> bool:
        """True if any selected note overlaps a note (same lane) that isn't part
        of the selection — used to warn after a drag-move."""
        for mode, moved in self.selection:
            if mode == "bgm":
                continue
            for other in self.project.charts.get(mode, ()):
                if other.lane != moved.lane or (mode, other) in self.selection:
                    continue
                if self._overlaps(moved, other):
                    return True
        return False

    def select_all(self) -> None:
        """Select every note (all key modes + BGM); switches to edit mode so the
        selection is visible."""
        self.set_mode("edit")
        sel = set()
        for km, chart in self.project.charts.items():
            for n in chart:
                sel.add((km, n))
        for n in self.project.bgm:
            sel.add(("bgm", n))
        self.selection = sel
        self.update()

    def flip_selection(self) -> None:
        """Public entry: mirror the selected notes left↔right within each mode's
        lanes (lane 1↔N, 2↔N-1, …)."""
        self._flip_selection()

    def _flip_selection(self) -> None:
        if not self.selection:
            return
        new_sel = set()
        for mode, n in self.selection:
            self.project.remove_object(mode, n)
        for mode, n in self.selection:
            if mode == "bgm":
                flipped = n
            else:
                lane = lanes_for(mode) - 1 - n.lane
                flipped = Note(n.measure, n.pos, lane, n.length)
            self.project.add_object(mode, flipped)
            new_sel.add((mode, flipped))
        self.selection = new_sel
        self.changed.emit()
        self.update()

    def _copy_selection(self, cut: bool) -> None:
        if not self.selection:
            return
        base = min(n.measure for _mode, n in self.selection)
        span = max(n.measure for _mode, n in self.selection) - base + 1
        # Measure-aligned: keep each note's measure offset from the block start.
        self._clipboard = (span, [(mode, n.measure - base, n.pos, n.lane, n.length)
                                  for mode, n in self.selection])
        if cut:
            self._delete_selection()

    def _measures_occupied(self, start: int, span: int, modes) -> bool:
        """Whether any note (in the clipboard's modes) sits in measures
        ``[start, start+span)``."""
        for mode in modes:
            for n in self._target_for(mode):
                if start <= n.measure < start + span:
                    return True
        return False

    def _paste(self) -> None:
        if not self._clipboard:
            return
        span, items = self._clipboard
        modes = {mode for mode, *_ in items}
        max_start = self.project.measures - span
        if max_start < 0:
            return
        # Start at the paste anchor's measure, then walk forward to the first run
        # of ``span`` empty measures (fills empty measures; drops it straight in
        # when the target is already clear).
        start = min(max_start, int(max(0.0, self._paste_anchor)))
        while start < max_start and self._measures_occupied(start, span, modes):
            start += 1
        if self._measures_occupied(start, span, modes):
            return   # no empty room left
        new_sel = set()
        for mode, m_off, pos, lane, length in items:
            note = Note(start + m_off, pos, lane, length)
            self.project.add_object(mode, note)
            new_sel.add((mode, note))
        if new_sel:
            self.selection = new_sel
            self.changed.emit()
            self.update()


class LaneHeader(QWidget):
    """A thin fixed strip above the canvas showing group labels, kept aligned
    with the canvas as it scrolls horizontally. The BGM|4K divider is a draggable
    two-line grip that resizes the BGM lane."""

    bgm_width_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lane_w = L.LANE_W
        self.bgm_w = 64
        self.columns, self.groups, self._width = L.build_layout(self.lane_w, self.bgm_w)
        self.x_offset = 0
        self.selected_km = KEY_MODES[0]
        self._drag = None            # (press_widget_x, start_bgm_w) while dragging
        self.setFixedHeight(26)
        self.setMouseTracking(True)
        self.setMinimumWidth(0)

    def set_lane_width(self, lane_w: int) -> None:
        self.lane_w = lane_w
        self.columns, self.groups, self._width = L.build_layout(self.lane_w, self.bgm_w)
        self.update()

    def set_bgm_width(self, bgm_w: int) -> None:
        self.bgm_w = max(20, min(400, int(bgm_w)))
        self.columns, self.groups, self._width = L.build_layout(self.lane_w, self.bgm_w)
        self.update()

    def set_x_offset(self, value: int) -> None:
        self.x_offset = value
        self.update()

    def set_selected_km(self, key_mode: int) -> None:
        self.selected_km = key_mode
        self.update()

    # -- draggable BGM|4K divider ------------------------------------------- #

    def _grip_x(self) -> int:
        """Widget-space x of the BGM/4K divider grip."""
        return self.groups[0].x1 + L.GROUP_GAP // 2 - self.x_offset

    def _on_grip(self, x: float) -> bool:
        return abs(x - self._grip_x()) <= 6

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._on_grip(event.position().x()):
            self._drag = (event.position().x(), self.bgm_w)
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None:
            px, start_w = self._drag
            self.bgm_width_changed.emit(int(start_w + (event.position().x() - px)))
            return
        self.setCursor(Qt.SplitHCursor if self._on_grip(event.position().x())
                       else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = None

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
        for g in self.groups[2:]:   # 4K|6K, 6K|LOAD (BGM|4K is the grip below)
            p.drawLine(g.x0 - L.GROUP_GAP // 2, 4, g.x0 - L.GROUP_GAP // 2, self.height() - 4)
        # BGM|4K divider grip: two accent lines to signal it's draggable.
        gx = self.groups[0].x1 + L.GROUP_GAP // 2
        p.setPen(QPen(C_SELECT, 2))
        p.drawLine(gx - 2, 3, gx - 2, self.height() - 3)
        p.drawLine(gx + 2, 3, gx + 2, self.height() - 3)
        p.end()
