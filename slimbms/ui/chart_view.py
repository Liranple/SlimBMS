"""The scrollable chart canvas and its fixed lane-label header."""

from __future__ import annotations

import bisect
import math
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
C_STOP = QColor("#ffb454")       # STOP (freeze) markers — a warm amber, distinct from BPM
C_SCROLL = QColor("#4ec9b0")     # 순간 변속 (SCROLL) markers — teal
C_SPEED = QColor("#dcdcaa")      # 선형 변속 (SPEED) markers — khaki
C_SPEED_REGION = QColor(220, 220, 170, 30)  # faint tint over a 선형 변속 ramp span
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

# Feedback when a move is rejected (hits a wall): the selection shakes with a
# fading red outline.
REJECT_SEC = 0.34      # animation length
REJECT_AMP = 4.0       # horizontal shake amplitude (px)
REJECT_FREQ = 58.0     # shake angular speed (rad/s) — a rapid tremble
C_REJECT = QColor("#ff4d4d")

C_SELECT = QColor(palette.ACCENT)       # accent for the selected key mode
C_SELECT_TINT = QColor(111, 208, 255, 20)  # faint fill over its lanes

# Default live-recording keys, per key mode: {Qt key -> lane index}. Left hand
# Q/W/E, right hand numpad(or top-row) 7/8/9, mapped left-to-right across the
# lanes. Top-row and numpad digits both arrive as Key_7/8/9 (NumLock on), so both
# work. Users can reassign these (편집 → 키 설정); the live map lives on the view.
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
        # Rejected-move feedback: the notes to shake and when the shake started.
        self._reject_notes = []       # [(mode, Note)] flashing red while rejected
        self._reject_start = 0.0
        self._reject_timer = QTimer(self)
        self._reject_timer.setInterval(16)
        self._reject_timer.timeout.connect(self._on_reject_tick)
        self.record_offset_measures = 0.0  # recording latency comp, in measures
        self._wave = None             # normalised peak envelope (numpy array)
        self._wave_bps = 200          # buckets per second in _wave
        self._wave_tm = None          # cached TimeMap for chart<->audio mapping
        self.show_waveform = True
        self.live_playing = False     # True while the preview is actively playing
        self.selected_km = KEY_MODES[0]  # key mode being recorded / highlighted
        # Live-recording key map ({km: {qt_key: lane}}); reassignable via settings.
        self.record_keys = {km: dict(m) for km, m in RECORD_KEYS.items()}
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

    def set_record_keys(self, mapping) -> None:
        """Replace the live-recording key map ({km: {qt_key: lane}})."""
        self.record_keys = {km: dict(m) for km, m in mapping.items()}

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

    def _apply_scale_drag(self, event: QMouseEvent) -> None:
        d = self._scale_drag
        # Measures stack upward from their number, so grabbing the number and
        # pushing it up should collapse the measure: drag up = fewer cells.
        dy = event.position().y() - d["y0"]      # drag up = negative = shrink
        if not d["active"] and abs(dy) < 4:
            return                               # still might be a plain click
        d["active"] = True
        cell_px = max(1.0, self.measure_px * float(self.grid_main))
        cells = d["start_cells"] + int(round(dy / cell_px))
        cells = max(d["min_cells"], min(self._base_cells(), cells))
        if cells != self._current_cells(d["measure"]):
            self._set_measure_cells(d["measure"], cells)
            # Reflow live from the pristine originals so the trailing notes visibly
            # spread into the next measure as you drag (and slide back if you grow
            # it again), instead of piling on the boundary until release.
            self._reflow_from(d["origin"])
        self.cursor_info.emit(f"마디 {d['measure']} · {cells}칸")

    def _set_measure_cells(self, m: int, cells: int) -> None:
        base = self._base_cells()
        # A measure can shrink all the way to a single cell; notes left in the
        # collapsed tail aren't lost — they flow into the next measure once the
        # drag is released (see :meth:`_reflow_collapsed`). Clamp only to
        # [1, base] here so the live drag can preview the full range.
        cells = max(1, min(base, cells))
        if cells >= base:
            self.project.measure_scales.pop(m, None)
        else:
            self.project.measure_scales[m] = Fraction(cells) * self.grid_main
        self._apply_size()
        self.changed.emit()          # dirty + coalesced undo entry
        self.update()

    def _reflow_one(self, measure: int, pos: Fraction):
        """Push a position forward out of any collapsed measure tail: while it
        sits at/after its measure's (shortened) length, drop it into the next
        measure. Returns ``(measure, pos, moved)``. Full measures have length 1,
        so a note with ``pos < 1`` never moves — only newly-collapsed ones do."""
        moved = False
        while pos >= self.project.measure_length(measure):
            pos -= self.project.measure_length(measure)
            measure += 1
            moved = True
        return measure, pos, moved

    def _reflow_abs(self, absolute: Fraction):
        """Reflow a whole absolute position (a note *start*) out of collapsed
        tails — right-continuous. Returns ``(new_absolute, moved)``."""
        m = int(absolute)
        m2, p2, moved = self._reflow_one(m, absolute - m)
        return m2 + p2, moved

    def _reflow_end(self, absolute: Fraction):
        """Reflow a note *end* position — left-continuous, so a tail landing
        exactly on a measure boundary counts as the end of the preceding
        measure's content (and a note wholly inside a collapsed tail keeps its
        length instead of shrinking to a point). Returns ``(new_absolute, moved)``."""
        if absolute <= 0:
            return absolute, False
        m = int(absolute)
        pos = absolute - m
        if pos == 0:                       # on a boundary → end of measure m-1
            m -= 1
            pos = Fraction(1)
        while pos > self.project.measure_length(m):   # strict: boundary stays put
            pos -= self.project.measure_length(m)
            m += 1
        result = m + pos
        return result, result != absolute

    def _capture_reflow_origin(self):
        """A snapshot of note/BGM/BPM positions to reflow *from*, so a live
        measure-resize can recompute placements from pristine originals every
        drag step (dragging back out un-collapses them instead of losing them)."""
        return (
            {km: list(c) for km, c in self.project.charts.items()},
            set(self.project.bgm),
            dict(self.project.bpm_changes),
            dict(self.project.stops),
            dict(self.project.scrolls),
            dict(self.project.speeds),
        )

    def _reflow_from(self, origin) -> bool:
        """Rebuild every note / BGM / BPM object from ``origin`` (a snapshot),
        pushing anything now stranded in a collapsed measure tail forward into
        the following measure(s) — carrying its offset, so shrinking a measure
        spreads its trailing notes across the next measure (cells 22,24,26… →
        0,2,4…), not piling them all on the first cell. Returns True if anything
        moved."""
        orig_charts, orig_bgm, orig_bpm, orig_stops, orig_scrolls, orig_speeds = origin
        changed = False
        highest = self.project.measures - 1

        for km in self.project.charts:
            out = []
            for n in orig_charts.get(km, ()):
                head, hmoved = self._reflow_abs(n.absolute)
                if n.length > 0:
                    # Reflow the tail too, so a long note whose end lands in a
                    # collapsed tail carries its end into the next measure instead
                    # of stopping at the boundary; recompute the length.
                    tail, tmoved = self._reflow_end(n.end_absolute)
                    if hmoved or tmoved:
                        changed = True
                        n = Note(int(head), head - int(head), n.lane, tail - head)
                elif hmoved:
                    changed = True
                    n = Note(int(head), head - int(head), n.lane, n.length)
                out.append(n)
                highest = max(highest, int(n.end_absolute))
            self.project.charts[km][:] = out

        out_bgm = set()
        for n in orig_bgm:
            m2, p2, moved = self._reflow_one(n.measure, n.pos)
            if moved:
                changed = True
                n = Note(m2, p2, n.lane, n.length)
            out_bgm.add(n)
            highest = max(highest, n.measure)
        self.project.bgm = out_bgm

        out_bpm = {}
        for abspos, val in orig_bpm.items():
            m = int(abspos)
            m2, p2, moved = self._reflow_one(m, abspos - m)
            if moved:
                changed = True
            out_bpm[m2 + p2] = val
            highest = max(highest, m2)
        self.project.bpm_changes = out_bpm

        out_stops = {}
        for abspos, beats in orig_stops.items():
            m = int(abspos)
            m2, p2, moved = self._reflow_one(m, abspos - m)
            if moved:
                changed = True
            out_stops[m2 + p2] = beats
            highest = max(highest, m2)
        self.project.stops = out_stops

        for orig_map, attr in ((orig_scrolls, "scrolls"), (orig_speeds, "speeds")):
            out = {}
            for abspos, val in orig_map.items():
                m = int(abspos)
                m2, p2, moved = self._reflow_one(m, abspos - m)
                if moved:
                    changed = True
                out[m2 + p2] = val
                highest = max(highest, m2)
            setattr(self.project, attr, out)

        if highest + 1 > self.project.measures:
            self.project.measures = highest + 1
        self._rebuild_caches()
        self._apply_size()
        self.changed.emit()
        self.update()
        return changed

    def _reflow_collapsed(self) -> bool:
        """Reflow the current notes in place (relocate any stranded in a collapsed
        measure tail into the following measures). Convenience wrapper over
        :meth:`_reflow_from` using the current state as the origin."""
        return self._reflow_from(self._capture_reflow_origin())

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
        self._paint_speed_regions(p)
        self._paint_waveform(p)
        self._paint_horizontal_lines(p)
        self._paint_separators(p)
        self._paint_selected_group(p)
        self._paint_notes(p)
        self._paint_hits(p)
        self._paint_selection(p)
        self._paint_reject(p)
        self._paint_hover(p)
        self._paint_drag(p)
        self._paint_bpm_changes(p)
        self._paint_stops(p)
        self._paint_scrolls(p)
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

    def _label_x(self) -> int:
        """Left edge of the empty label strip to the right of the last lane
        group, where all gimmick labels sit clear of the notes."""
        return self.groups[-1].x1 + L.RIGHT_PAD

    def _marker_label(self, p: QPainter, y: int, text: str, col) -> None:
        """Draw a gimmick pill in the right-hand label strip, vertically centred
        on the marker's line so it never covers a note lane."""
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        p.setFont(font)
        tw = p.fontMetrics().horizontalAdvance(text) + 12
        tag = QRect(self._label_x(), y - 8, tw, 16)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(tag, 4, 4)
        p.setPen(QPen(C_GROUP_BG_B, 1))     # dark ink on the accent pill
        p.drawText(tag, Qt.AlignCenter, text)

    def _speed_ramps(self):
        """선형 변속 ramps for display/editing (see Project.speed_ramps)."""
        return self.project.speed_ramps()

    def _paint_speed_regions(self, p: QPainter) -> None:
        """Faintly shade each 선형 변속 (SPEED) ramp span across the note lanes so
        it's clear where the gradual change runs from and to."""
        if not self.project.speeds:
            return
        x0 = self.groups[0].x0
        x1 = self.groups[-1].x1
        for sp, ep, _sv, _ev in self._speed_ramps():
            if ep <= sp:
                continue
            y_top = int(self.y_for(float(ep)))   # later position = smaller y
            y_bot = int(self.y_for(float(sp)))
            if y_bot < self._vis_lo or y_top > self._vis_hi:
                continue
            p.fillRect(QRect(x0, y_top, x1 - x0, y_bot - y_top), C_SPEED_REGION)

    def _paint_bpm_changes(self, p: QPainter) -> None:
        if not self.project.bpm_changes:
            return
        x0 = L.LEFT_MARGIN
        x1 = self.groups[-1].x1
        for pos, bpm in self.project.bpm_changes.items():
            y = int(self.y_for(float(pos)))
            if y < self._vis_lo - 12 or y > self._vis_hi + 12:
                continue
            # A solid line across the lanes marks exactly where the tempo changes;
            # the pill naming it sits out in the right-hand label strip.
            p.setPen(QPen(C_BPM, 2))
            p.drawLine(x0, y, x1, y)
            self._marker_label(p, y, f"♩ {bpm:g} BPM", C_BPM)

    def _paint_stops(self, p: QPainter) -> None:
        """Mark STOP (freeze) positions with an amber dashed line + a label in the
        right-hand strip. Only the exposed strip is drawn."""
        if not self.project.stops:
            return
        x0 = L.LEFT_MARGIN
        x1 = self.groups[-1].x1
        for pos, beats in self.project.stops.items():
            y = int(self.y_for(float(pos)))
            if y < self._vis_lo - 12 or y > self._vis_hi + 12:
                continue
            p.setPen(QPen(C_STOP, 2, Qt.DashLine))
            p.drawLine(x0, y, x1, y)
            self._marker_label(p, y, f"■ {float(beats):g}박 정지", C_STOP)

    def _paint_scrolls(self, p: QPainter) -> None:
        """Mark 순간 변속 (SCROLL, step) and 선형 변속 (SPEED, interpolated)
        note-speed changes with a dotted line + a label in the right-hand strip.
        Editing only marks WHERE they are — the game (Qwilight) renders the
        actual scroll effect. Only the exposed strip is drawn."""
        if not self.project.scrolls and not self.project.speeds:
            return
        x0 = L.LEFT_MARGIN
        x1 = self.groups[-1].x1
        for source, col, tag_txt in ((self.project.scrolls, C_SCROLL, "순간"),
                                     (self.project.speeds, C_SPEED, "선형")):
            for pos, mult in source.items():
                y = int(self.y_for(float(pos)))
                if y < self._vis_lo - 12 or y > self._vis_hi + 12:
                    continue
                p.setPen(QPen(col, 2, Qt.DotLine))
                p.drawLine(x0, y, x1, y)
                self._marker_label(p, y, f"{tag_txt} ×{float(mult):g}", col)

    def set_playhead(self, absolute: Optional[float]) -> None:
        prev = self.playhead
        self.playhead = absolute
        # Flash notes the playhead just crossed (only during smooth playback —
        # skip big jumps from seeking). Compare in *display* space (collapsed
        # measure tails removed) so a shortened measure — where the absolute
        # position jumps at the boundary but real playback time is continuous —
        # neither looks like a seek nor skips the notes right after it.
        if self.live_playing and absolute is not None and prev is not None:
            d_prev = self._display_pos(prev)
            d_cur = self._display_pos(absolute)
            if 0 < d_cur - d_prev <= HIT_MAX_STEP:
                self._register_hits(prev, absolute, d_prev, d_cur)
        self.update()

    def _register_hits(self, prev: float, cur: float,
                       d_prev: float, d_cur: float) -> None:
        now = time.monotonic()

        def crossed(n) -> bool:
            # Judge the crossing on display positions: a note in (or right after)
            # a shortened measure lines up with real playback time this way.
            return d_prev < self._display_pos(float(n.absolute)) <= d_cur

        for m in range(int(prev), int(cur) + 1):
            for km, chart in self.project.charts.items():
                for n in self._taps_by_measure.get(km, {}).get(m, ()):
                    if crossed(n):
                        self._hits[(km, n)] = now
            for n in self._bgm_by_measure.get(m, ()):
                if crossed(n):
                    self._hits[("bgm", n)] = now
        for km, longs in self._longs.items():
            for n in longs:
                if crossed(n):
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

    def _reject_feedback(self) -> None:
        """Kick off the 'move rejected' shake: the selection trembles with a
        fading red outline for a moment (nothing actually moves)."""
        if not self.selection:
            return
        self._reject_notes = list(self.selection)
        self._reject_start = time.monotonic()
        self._reject_timer.start()
        self.update()

    def _on_reject_tick(self) -> None:
        if time.monotonic() - self._reject_start >= REJECT_SEC:
            self._reject_timer.stop()
            self._reject_notes = []
        self.update()

    def _paint_reject(self, p: QPainter) -> None:
        if not self._reject_notes:
            return
        now = time.monotonic()
        t = now - self._reject_start
        if t >= REJECT_SEC:
            return
        # Intensity fades from the *last* trigger, but the tremble phase runs off
        # the absolute clock so mashing a blocked key keeps it bright and shaking
        # smoothly (a phase reset per press would otherwise freeze the wobble).
        decay = 1.0 - t / REJECT_SEC
        dx = int(round(REJECT_AMP * decay * math.sin(now * REJECT_FREQ)))
        colx = self._col_x()
        bgmx = self.columns[0].x
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(C_REJECT.red(), C_REJECT.green(), C_REJECT.blue(),
                             int(230 * decay)), 2))
        for mode, n in self._reject_notes:
            x = bgmx if mode == "bgm" else colx.get((mode, n.lane))
            if x is None:
                continue
            w = self.bgm_w if mode == "bgm" else self.lane_w
            rect = self._sel_rect(x, n, w)
            p.drawRect(rect.adjusted(dx - 1, -1, dx + 1, 1))

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

    def _global_lane_at(self, x: float) -> int:
        """Global lane index (0-based across the 4K→6K→LOAD continuum) of the key
        column nearest to ``x``, ignoring BGM and the gaps between groups. Used to
        translate a horizontal drag into a lane delta that can cross modes."""
        best_i, best_d = 0, None
        for col in self.columns:
            if col.kind != "key":
                continue
            center = col.x + col.w / 2
            d = abs(x - center)
            if best_d is None or d < best_d:
                best_i = _GLOBAL_INDEX[(col.key_mode, col.lane)]
                best_d = d
        return best_i

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
        back and forth doesn't accumulate error.

        Movement is in *display* space (collapsed measure tails removed) so a
        note tracks the cursor 1:1 through shortened measures and both ends of a
        long note shift together — its visible length never changes and its tail
        can't slip into a hidden region (which would make it look resized)."""
        md = self._move_drag
        step = self.grid_main
        # Horizontal movement walks the whole 4K→6K→LOAD lane space as one
        # continuum (like the arrow keys), so a note can cross mode boundaries.
        # The delta is taken from the actual columns under the press/cursor (not
        # dx/lane_w) so the group gaps between modes don't throw off the count.
        d_lane = self._global_lane_at(event.position().x()) - \
            self._global_lane_at(md["px"])
        # Clamp the delta *collectively* (not per note): shift only as far as the
        # whole selection can go while staying in bounds. Clamping each note
        # independently is what used to squish them all against a mode's edge.
        gs = [_GLOBAL_INDEX[(mode, orig.lane)]
              for mode, orig in md["origs"] if mode != "bgm"]
        if gs:
            d_lane = max(-min(gs),
                         min((len(_GLOBAL_LANES) - 1) - max(gs), d_lane))
        else:
            d_lane = 0
        # Vertical delta in display units (pixels back to display position). A
        # normal drag snaps to the primary grid; a Shift drag is free placement,
        # quantised only to whole pixels.
        def disp_at(y):
            return self._vtotal - (y - self.v_pad) / self.measure_px
        raw = disp_at(event.position().y()) - disp_at(md["py"])
        if md.get("free"):
            d_disp = Fraction(int(round(raw * self.measure_px)), self.measure_px)
        else:
            d_disp = int(round(raw / float(step))) * step
        if not md["moved"] and d_lane == 0 and d_disp == 0:
            return
        cum = self.project.cumulative_lengths()
        total = cum[self.project.measures]
        # Remove the notes placed by the previous drag step (the originals on the
        # first step) — tracked independently of the selection so a Shift-drag of
        # an unselected note can't leave a duplicate behind.
        for mode, n in md["placed"]:
            self.project.remove_object(mode, n)
        new_sel = set()
        for mode, orig in md["origs"]:
            head_d = self._display_pos_frac(orig.absolute, cum)
            len_d = self._display_pos_frac(orig.end_absolute, cum) - head_d
            # Keep the whole (rigid) note within the timeline.
            new_head_d = max(Fraction(0), min(total - step - len_d, head_d + d_disp))
            new_abs = self._absolute_from_display_frac(new_head_d, cum)
            measure = int(new_abs)
            pos = new_abs - measure
            if mode == "bgm":
                newmode, lane, length = "bgm", 0, Fraction(0)
            else:
                # Walk the global lane space so the note can move into an
                # adjacent key mode (4K↔6K↔LOAD); the delta is pre-clamped so
                # this index is always valid.
                newmode, lane = _GLOBAL_LANES[_GLOBAL_INDEX[(mode, orig.lane)] + d_lane]
                # Tail follows the head by the same display span, then back to an
                # absolute length — both ends stay on visible cells.
                tail_abs = self._absolute_from_display_frac(new_head_d + len_d, cum)
                length = tail_abs - new_abs
            moved = Note(measure, pos, lane, length)
            self.project.add_object(newmode, moved)
            new_sel.add((newmode, moved))
        self.selection = new_sel
        md["placed"] = list(new_sel)
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

    def _long_endpoint_at(self, col, y: float):
        """If ``y`` is near the head/tail of a long note in this column's lane,
        return ``(mode, note, 'head'|'tail')``; else None. Used in add mode so
        that clicking a long note's endpoint resizes it instead of dropping a
        new tap there."""
        if col.kind == "bgm":
            return None
        best = None
        best_d = None
        for n in self.project.charts[col.key_mode]:
            if n.lane != col.lane or not n.is_long:
                continue
            end = self._endpoint_near(n, y)
            if end is None:
                continue
            ref = n.absolute if end == "head" else n.end_absolute
            d = abs(y - self.y_for(ref))
            if best_d is None or d < best_d:
                best, best_d = (col.key_mode, n, end), d
        return best

    def _apply_len_drag(self, event: QMouseEvent) -> None:
        """Resize a long note by dragging its head or tail to the cursor.
        Shrinking down to a single cell (head meets tail) collapses it back
        into a plain tap; dragging out again regrows a long note."""
        ld = self._len_drag
        mode, note = ld["mode"], ld["note"]
        measure, pos = self.pos_at(event.position().y(), self._snap_now(event))
        cur = Fraction(measure) + pos
        head, tail = note.absolute, note.end_absolute
        if ld["end"] == "tail":
            new_end = max(head, min(cur, Fraction(self.project.measures)))
            new = self._relen(mode, note, head, new_end - head)
        else:
            new_head = max(Fraction(0), min(cur, tail))
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
        if self.mode == "edit" and self._move_drag is not None:
            self._apply_move_drag(event)
            return
        if self.mode == "edit" and self._drag_start is not None:
            self._drag_cur = (event.position().x(), event.position().y())
            self.update()
            return
        if self.mode == "add":
            if self._len_drag is not None:
                self._apply_len_drag(event)  # resizing a long note endpoint
                return
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
                                    "min_cells": 1,
                                    "origin": self._capture_reflow_origin()}
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
                # Pressing on an existing long note's head/tail grabs it for
                # resizing instead of dropping a new tap there.
                grab = self._long_endpoint_at(c, y)
                if grab is not None:
                    mode, note, end = grab
                    self.selection = {(mode, note)}
                    self._len_drag = {"mode": mode, "note": note, "end": end}
                    self.update()
                    return
                # Pressing on an existing note: a plain click does nothing (no
                # duplicate), but dragging grows an existing tap into a long
                # note from where it sits.
                hit = self._note_at_point(c, y)
                if hit is not None:
                    mode, note = hit
                    if mode != "bgm" and not note.is_long:
                        self._add_drag = (mode, note, note.absolute, c)
                        self._hover = None
                    return
                # Empty cell: drop a note; dragging up/down before release turns
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

        if hit is not None:
            # Press on a note → drag to move it. Without Shift the move snaps to
            # the grid; WITH Shift it's free placement (1px). A Shift press that
            # never drags falls back to the classic select-toggle (on release):
            # adding an unselected note now, removing a selected one on release.
            toggle = None
            if self._drag_shift:
                if hit not in self.selection:
                    self.selection = self.selection | {hit}   # shift-click adds
                else:
                    toggle = hit                              # remove if no drag
            elif hit not in self.selection:
                self.selection = {hit}
            self._move_drag = {"origs": list(self.selection),
                               "placed": list(self.selection),
                               "px": x, "py": y, "moved": False,
                               "free": self._drag_shift, "toggle": toggle}
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
            else:
                # Final commit from the pristine originals at the settled scale
                # (notes already reflowed live during the drag).
                self._reflow_from(d["origin"])
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
            md = self._move_drag
            self._move_drag = None
            if not md["moved"] and md.get("toggle") is not None:
                # A Shift press that never dragged → classic select-toggle.
                self.selection = self.selection ^ {md["toggle"]}
            else:
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
            lane = self.record_keys.get(self.selected_km, {}).get(event.key())
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
        shift = bool(mod & Qt.ShiftModifier)
        # Vertical step for Up/Down: Ctrl snaps to the secondary grid, Shift
        # nudges one pixel (free placement), plain steps one primary cell.
        # Left/Right always walk the 4K→6K→LOAD lanes regardless of modifier.
        vmode = "sub" if ctrl else "px" if shift else "main"
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
            # Ctrl hops to the adjacent key mode (same lane index); otherwise one
            # lane left through the continuous 4K→6K→LOAD space.
            self._move_selection(0, 0, mode_jump=-1) if ctrl \
                else self._move_selection(-1, 0)
        elif key == Qt.Key_Right:
            self._move_selection(0, 0, mode_jump=1) if ctrl \
                else self._move_selection(1, 0)
        elif key == Qt.Key_Up:
            self._move_selection(0, 1, vmode)
        elif key == Qt.Key_Down:
            self._move_selection(0, -1, vmode)
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

    def _display_pos_frac(self, absolute: Fraction, cum) -> Fraction:
        """Exact (Fraction) display position of an absolute chart position, with
        collapsed measure tails removed — the Fraction twin of :meth:`_display_pos`.
        ``cum`` is ``project.cumulative_lengths()`` (passed in to avoid rebuilding
        it per note)."""
        measures = self.project.measures
        m = int(absolute)
        if m < 0:
            return absolute
        if m >= measures:
            return cum[measures] + (absolute - measures)
        return cum[m] + min(absolute - m, self.project.measure_length(m))

    def _absolute_from_display_frac(self, disp: Fraction, cum) -> Fraction:
        """Inverse of :meth:`_display_pos_frac`: a display position back to an
        absolute chart position."""
        measures = self.project.measures
        total = cum[measures]
        if disp <= 0:
            return Fraction(0)
        if disp >= total:
            return Fraction(measures) + (disp - total)
        m = bisect.bisect_right(cum, disp) - 1
        m = max(0, min(measures - 1, m))
        return m + (disp - cum[m])

    def _sub_step_cells(self) -> int:
        """How many primary cells a Ctrl move covers: floor(main / secondary),
        at least 1. E.g. a 32-cell grid with a /8 secondary steps 4 cells, a /4
        secondary steps 5 (21-cell grid → 21 // 4)."""
        main_cells = int(1 / self.grid_main)
        sub_cells = int(1 / self.grid_sub)
        return max(1, main_cells // sub_cells)

    def _vertical_delta(self, vdir: int, vmode: str) -> Fraction:
        """A single display-space delta applied to the WHOLE selection, so the
        relative spacing is preserved and notes can never collapse onto each
        other. 'main' steps one primary cell, 'px' one pixel; 'sub' (Ctrl) steps
        several primary cells at once — floor(main / secondary)."""
        if vmode == "px":
            return vdir * Fraction(1, self.measure_px)
        if vmode == "sub":
            return vdir * self._sub_step_cells() * self.grid_main
        return vdir * self.grid_main   # 'main'

    def _move_selection(self, d_lane: int, vdir: int, vmode: str = "main",
                        mode_jump: int = 0) -> None:
        if not self.selection:
            return
        # Vertical moves are computed in *display* space (collapsed measure tails
        # removed) so a shortened measure's last visible cell moves straight to
        # the next measure's first cell — not through its hidden cells.
        cum = self.project.cumulative_lengths()
        total = cum[self.project.measures]
        # Compute every note's target first; if ANY would cross a wall (the left
        # edge of 4K, the right edge of LOAD, or the top/bottom), abort the whole
        # move so the selection never gets squished against an edge. Plain lane
        # moves walk 4K→6K→LOAD as one continuous space; a mode_jump hops to the
        # adjacent key mode keeping the same lane index (aborting if the index
        # doesn't exist there, or there's no mode on that side).
        vdelta = self._vertical_delta(vdir, vmode) if vdir else Fraction(0)
        proposed = []
        for mode, n in self.selection:
            if vdir:
                disp = self._display_pos_frac(n.absolute, cum) + vdelta
                if disp < 0 or disp >= total:
                    return self._reject_feedback()
                new_abs = self._absolute_from_display_frac(disp, cum)
            else:
                new_abs = n.absolute
            if mode == "bgm":
                newmode, newlane = "bgm", 0   # BGM has no lane space to move in
            elif mode_jump:
                mi = DISPLAY_MODES.index(mode) + mode_jump
                if mi < 0 or mi >= len(DISPLAY_MODES):
                    return self._reject_feedback()
                newmode = DISPLAY_MODES[mi]
                if n.lane >= lanes_for(newmode):
                    return self._reject_feedback()
                newlane = n.lane
            else:
                g = _GLOBAL_INDEX[(mode, n.lane)] + d_lane
                if g < 0 or g >= len(_GLOBAL_LANES):
                    return self._reject_feedback()
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
        if self._reject_notes:                 # a prior shake is now moot
            self._reject_timer.stop()
            self._reject_notes = []
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
        # Also capture each measure's length (scale) across the block, so pasting
        # reproduces shortened measures (e.g. a copied 24-cell measure stays 24
        # cells wherever it lands). Keyed by offset from the block start.
        scales = {m - base: self.project.measure_scales[m]
                  for m in range(base, base + span)
                  if m in self.project.measure_scales}
        # Measure-aligned: keep each note's measure offset from the block start.
        self._clipboard = (span, [(mode, n.measure - base, n.pos, n.lane, n.length)
                                  for mode, n in self.selection], scales)
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
        span, items, scales = self._clipboard
        modes = {mode for mode, *_ in items}
        # Start at the paste anchor's measure, then walk forward to the first run
        # of ``span`` empty measures. Measures past the timeline's end are empty,
        # so this always finds room; the timeline then grows to fit (autofit on
        # the change signal) — pasting near the end no longer fails.
        start = max(0, int(max(0.0, self._paste_anchor)))
        while self._measures_occupied(start, span, modes):
            start += 1
        new_sel = set()
        for mode, m_off, pos, lane, length in items:
            note = Note(start + m_off, pos, lane, length)
            self.project.add_object(mode, note)
            new_sel.add((mode, note))
        # Reproduce the copied block's measure lengths onto the pasted measures.
        for m_off in range(span):
            target = start + m_off
            if m_off in scales:
                self.project.measure_scales[target] = scales[m_off]
            else:
                self.project.measure_scales.pop(target, None)
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
