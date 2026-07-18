"""Offscreen rendering tests for the chart canvas.

Guards the P3 hot-path optimizations: grid-line culling to the visible strip
must draw exactly what a full-song scan would, and the whole paint path (grid,
notes, long notes, playhead) must render without error after layout changes.

Run: QT_QPA_PLATFORM=offscreen python tests/test_render.py
"""

import os
import sys
from fractions import Fraction

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from slimbms.model import Note, Project  # noqa: E402
from slimbms.ui.chart_view import ChartView  # noqa: E402


def _app():
    return QApplication.instance() or QApplication([])


def test_grid_culling_matches_full_scan():
    """Visible-range grid generation must yield the same lines (after the strip
    cull) as scanning every measure — i.e. culling drops nothing visible."""
    _app()
    v = ChartView(Project(bpm=120, measures=200))
    v.measure_px = 150
    measures = v.project.measures
    # Try several scroll strips, including the very top and bottom.
    for lo, hi in ((5000, 5800), (0, 800), (28000, 29000), (100, 260)):
        v._vis_lo, v._vis_hi = lo, hi
        abs_top = v.absolute_at(lo)
        abs_bot = v.absolute_at(hi)
        gm_lo = max(0, int(abs_bot) - 1)
        gm_hi = min(measures, int(abs_top) + 2)
        for step in (v.grid_main, v.grid_sub):
            full = {int(y) for y in v._grid_line_ys(step, 0, measures)
                    if lo <= int(y) <= hi}
            culled = {int(y) for y in v._grid_line_ys(step, gm_lo, gm_hi)
                      if lo <= int(y) <= hi}
            assert full == culled, f"grid mismatch at strip {(lo, hi)}, step {step}"


def test_paint_path_renders():
    """The full paint path renders without error, including long notes, a
    playhead, and after a lane-width change (which rebuilds the col_x cache)."""
    _app()
    p = Project(bpm=150, measures=64)
    p.charts[4].append(Note(3, Fraction(1, 4), 0))
    p.charts[6].append(Note(10, Fraction(0), 2, Fraction(1, 2)))   # long note
    p.bgm.add(Note(0, Fraction(0), 0))
    v = ChartView(p)
    v.resize(v._width, 800)
    v.set_playhead(5.0)
    pm = QPixmap(v._width, 800)
    v.render(pm)                       # exercises paintEvent end-to-end
    v.set_lane_width(40)               # rebuilds layout + col_x cache
    v.render(pm)
    v.set_bgm_width(120)
    v.render(pm)


def test_measure_scale_geometry():
    """A per-measure display scale collapses that measure's tail: it takes less
    height and shows fewer grid cells, while y_for/absolute_at stay invertible
    and the measures below it are pulled up with no gap."""
    _app()
    p = Project(bpm=120, measures=8)
    v = ChartView(p)
    v.measure_px = 150
    v.set_grid_main(32)
    full_h = v.height()
    v._set_measure_cells(2, 16)                       # half of measure 2
    assert p.measure_scales[2] == Fraction(1, 2)
    assert v.height() == full_h - v.measure_px // 2   # lost half a measure
    for a in (0.0, 1.0, 2.0, 2.25, 3.0, 7.0):         # round-trips through it
        assert abs(v.absolute_at(v.y_for(a)) - a) < 1e-6
    assert abs(v.y_for(3.0) - v.y_for(2.5)) < 1e-6    # no gap after the tail
    assert len(list(v._grid_line_ys(v.grid_main, 2, 3))) == 15   # 16 cells
    assert len(list(v._grid_line_ys(v.grid_main, 1, 2))) == 31   # 32 cells


def test_measure_scale_reflows_notes_past_shrink():
    """Shrinking a measure past a note carries the note into the next measure
    (grid 32, note at cell 16 of measure 3, halve the measure -> measure 4 cell
    0). The note keeps its offset from the collapsed boundary."""
    _app()
    p = Project(bpm=120, measures=8)
    p.charts[4].append(Note(3, Fraction(1, 2), 0))    # cell 16 of a 32-cell grid
    v = ChartView(p)
    v.set_grid_main(32)
    v._set_measure_cells(3, 16)                        # halve the measure
    assert v._current_cells(3) == 16                   # no clamp — it really shrank
    v._reflow_collapsed()                              # commit (as on drag release)
    notes = p.charts[4]
    assert len(notes) == 1
    n = notes[0]
    assert n.measure == 4 and n.pos == Fraction(0)     # moved to measure 4 cell 0


def test_measure_scale_reflow_offsets_and_cascades():
    """A note deeper in the collapsed tail keeps its cell offset past the seam,
    and reflow cascades through consecutive shortened measures."""
    _app()
    p = Project(bpm=120, measures=8)
    p.charts[4].append(Note(3, Fraction(20, 32), 0))   # cell 20
    v = ChartView(p)
    v.set_grid_main(32)
    v._set_measure_cells(3, 16)                         # keep cells 0..15
    v._reflow_collapsed()
    n = p.charts[4][0]
    assert n.measure == 4 and n.pos == Fraction(4, 32)  # 20 - 16 = cell 4


def test_measure_shrink_spreads_notes_and_restores_on_grow():
    """Shrinking a measure spreads every trailing note across the next measure
    keeping their spacing (not piling them on cell 0), and reflowing from the
    pristine originals means growing it again slides them back."""
    _app()
    p = Project(bpm=120, measures=8)
    for c in (22, 24, 26, 28, 30):
        p.charts[4].append(Note(3, Fraction(c, 32), 0))
    p.charts[4].append(Note(3, Fraction(20, 32), 0, Fraction(12, 32)))  # long note
    v = ChartView(p)
    v.set_grid_main(32)
    v.refresh()
    origin = v._capture_reflow_origin()

    # Shrink measure 3 to 20 cells: cells 20..31 collapse.
    v._set_measure_cells(3, 20)
    v._reflow_from(origin)
    cells = sorted((n.measure, int(n.pos * 32), int(n.length * 32)) for n in p.charts[4])
    assert cells == [(4, 0, 12), (4, 2, 0), (4, 4, 0), (4, 6, 0), (4, 8, 0), (4, 10, 0)]

    # Grow back to full: every note returns to its original spot (long note too).
    v._set_measure_cells(3, 32)
    v._reflow_from(origin)
    back = sorted((n.measure, int(n.pos * 32), int(n.length * 32)) for n in p.charts[4])
    assert back == [(3, 20, 12), (3, 22, 0), (3, 24, 0), (3, 26, 0), (3, 28, 0), (3, 30, 0)]


def test_move_long_note_keeps_visible_length_across_shortened_measure():
    """Dragging a long note in edit mode moves it rigidly in display space, so
    its visible length is unchanged even when the move carries it across a
    shortened measure — its tail can't slip into the hidden region and look
    resized. Edit mode must never change a long note's length."""
    from PySide6.QtCore import Qt, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent

    _app()
    p = Project(bpm=120, measures=10)
    p.measure_scales[3] = Fraction(16, 32)             # measure 3 halved
    ln = Note(3, Fraction(5, 32), 0, Fraction(7, 32))  # head cell 5, tail cell 12
    p.charts[4].append(ln)
    v = ChartView(p)
    v.set_grid_main(32)
    v.set_mode("edit")
    v.refresh()
    v.resize(1400, 4000)

    def vis(n):
        return round(v.y_for(n.absolute) - v.y_for(n.end_absolute), 2)

    before = vis(ln)
    x = v._col_x()[(4, 0)] + v.lane_w / 2
    cell = v.measure_px * float(v.grid_main)
    head_y = v.y_for(ln.absolute)
    def ev(kind, y, btn, btns):
        return QMouseEvent(kind, QPointF(x, y), btn, btns, Qt.NoModifier)
    v.mousePressEvent(ev(QEvent.MouseButtonPress, head_y - 1, Qt.LeftButton, Qt.LeftButton))
    v.mouseMoveEvent(ev(QEvent.MouseMove, head_y - 1 - 6 * cell, Qt.NoButton, Qt.LeftButton))
    v.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, head_y - 1 - 6 * cell, Qt.LeftButton, Qt.NoButton))

    moved = next(iter(v.selection))[1]
    assert vis(moved) == before                        # visible length unchanged
    # Tail sits on a visible cell (frac below the measure's scale), never hidden.
    tm = int(moved.end_absolute)
    tail_frac = moved.end_absolute - tm
    assert tail_frac < p.measure_length(tm) or tail_frac == 0


def test_copy_paste_carries_measure_scales():
    """Copying notes from shortened measures and pasting reproduces the measure
    lengths on the pasted block (24-cell measures stay 24-cell), and pasting past
    the current end still places the notes."""
    _app()
    p = Project(bpm=120, measures=16)
    for m in (3, 4, 5):
        p.measure_scales[m] = Fraction(24, 32)         # shrink to 24 cells
        p.charts[4].append(Note(m, Fraction(0), 0))
    v = ChartView(p)
    v.set_grid_main(32)
    v.refresh()
    v.selection = {(4, n) for n in list(p.charts[4])}
    v._copy_selection(cut=False)

    v._paste_anchor = 6.0
    v._paste()
    pasted = sorted((n.measure, n.pos) for n in p.charts[4] if n.measure >= 6)
    assert pasted == [(6, Fraction(0)), (7, Fraction(0)), (8, Fraction(0))]
    # Pasted measures adopt the copied 24-cell length; the next measure stays full.
    assert all(p.measure_length(m) == Fraction(24, 32) for m in (6, 7, 8))
    assert p.measure_length(9) == Fraction(1)


def test_paste_past_end_places_notes():
    """Pasting when the timeline is too short still places the notes (the
    timeline grows to fit) instead of silently failing."""
    _app()
    p = Project(bpm=120, measures=16)
    p.charts[4] += [Note(14, Fraction(0), 0), Note(15, Fraction(0), 1)]
    v = ChartView(p)
    v.set_grid_main(16)
    v.refresh()
    v.selection = {(4, n) for n in list(p.charts[4])}
    v._copy_selection(cut=False)
    v._paste_anchor = 15.0        # not enough room before the end
    v._paste()
    assert len(v.selection) == 2  # notes were placed (into measures beyond 15)
    assert max(n.measure for _m, n in v.selection) >= 16


def test_reflow_moves_long_note_tail():
    """Shrinking a measure carries a long note's tail into the next measure too
    (not just the head): a tail in the collapsed region relocates, and a note
    wholly inside the collapsed region keeps its length."""
    _app()
    # head visible, tail in the collapsed region -> tail moves, length grows to
    # span the (removed) gap.
    p = Project(bpm=120, measures=8)
    p.charts[4].append(Note(3, Fraction(10, 32), 0, Fraction(18, 32)))
    v = ChartView(p)
    v.set_grid_main(32)
    origin = v._capture_reflow_origin()
    p.measure_scales[3] = Fraction(24, 32)
    v.refresh()
    v._reflow_from(origin)
    n = p.charts[4][0]
    assert (n.measure, n.pos) == (3, Fraction(10, 32))
    assert n.end_absolute == 4 + Fraction(4, 32)          # tail now in measure 4

    # whole note inside the collapsed region, tail on the boundary -> both ends
    # relocate and the length is preserved (no shrink to a point).
    p2 = Project(bpm=120, measures=8)
    p2.charts[4].append(Note(3, Fraction(20, 32), 0, Fraction(12, 32)))
    v2 = ChartView(p2)
    v2.set_grid_main(32)
    origin2 = v2._capture_reflow_origin()
    p2.measure_scales[3] = Fraction(20, 32)
    v2.refresh()
    v2._reflow_from(origin2)
    m = p2.charts[4][0]
    assert (m.measure, m.pos, m.length) == (4, Fraction(0), Fraction(12, 32))


def test_ctrl_mode_jump():
    """Ctrl+Left/Right hops to the adjacent key mode keeping the lane index,
    aborting the whole move if any note's lane doesn't exist there."""
    from slimbms.model import IMPORT_MODE

    _app()
    def setup(mode, lanes):
        p = Project(bpm=120, measures=8)
        notes = [Note(2, Fraction(0), L) for L in lanes]
        p.charts[mode] += notes
        v = ChartView(p)
        v.set_mode("edit")
        v.refresh()
        v.selection = {(mode, n) for n in notes}
        return v
    def state(v):
        return sorted((mode, n.lane) for mode, n in v.selection)

    # 6K lane 0 -> Ctrl+Right -> LOAD lane 0 (index preserved).
    v = setup(6, [0, 1])
    v._move_selection(0, 0, mode_jump=1)
    assert state(v) == [(IMPORT_MODE, 0), (IMPORT_MODE, 1)]

    # 6K lanes 0,4 -> Ctrl+Left: lane 4 has no 4K equivalent -> whole move aborts.
    v = setup(6, [0, 4])
    v._move_selection(0, 0, mode_jump=-1)
    assert state(v) == [(6, 0), (6, 4)]

    # LOAD lanes 0,2,3,5 -> Ctrl+Left twice: first lands in 6K, second aborts
    # (lane 5 has no 4K equivalent) so it stays in 6K.
    v = setup(IMPORT_MODE, [0, 2, 3, 5])
    v._move_selection(0, 0, mode_jump=-1)
    assert state(v) == [(6, 0), (6, 2), (6, 3), (6, 5)]
    v._move_selection(0, 0, mode_jump=-1)
    assert state(v) == [(6, 0), (6, 2), (6, 3), (6, 5)]

    # 4K lane 0 -> Ctrl+Left: no mode to the left -> blocked.
    v = setup(4, [0])
    v._move_selection(0, 0, mode_jump=-1)
    assert state(v) == [(4, 0)]


def test_ctrl_sub_move_preserves_spacing_no_overlap():
    """Ctrl+Up/Down (secondary-grid move) shifts the whole selection by one
    uniform delta, so notes never collapse onto each other — even when several
    fall in the same secondary-grid interval (previously they'd overlap)."""
    _app()
    p = Project(bpm=120, measures=210)
    cells = [1, 3, 5, 9, 11, 13]                      # grid 32, sub 12
    for c in cells:
        p.charts[6].append(Note(202, Fraction(c, 32), 0))
    v = ChartView(p)
    v.set_grid_main(32)
    v.set_grid_sub(12)
    v.set_mode("edit")
    v.refresh()
    v.selection = {(6, n) for n in list(p.charts[6])}

    def positions():
        return sorted(n.pos for n in p.charts[6])
    before = positions()
    gaps_before = [before[i + 1] - before[i] for i in range(len(before) - 1)]

    v._move_selection(0, 1, "sub")                    # Ctrl+Up
    after = positions()
    assert len(set(after)) == len(cells)              # all distinct — no overlap
    gaps_after = [after[i + 1] - after[i] for i in range(len(after) - 1)]
    assert gaps_after == gaps_before                  # spacing preserved
    assert all(a > b for a, b in zip(after, before))  # everything moved up


def test_rejected_move_triggers_shake_feedback():
    """A blocked move (here a Ctrl mode-jump with no target lane) arms the
    red-shake feedback; a valid move does not."""
    from slimbms.model import IMPORT_MODE

    _app()
    p = Project(bpm=120, measures=8)
    n = Note(2, Fraction(0), 5)                     # 6K lane 5 (no 4K equivalent)
    p.charts[6].append(n)
    v = ChartView(p)
    v.set_mode("edit")
    v.refresh()
    v.selection = {(6, n)}

    v._move_selection(0, 0, mode_jump=-1)           # rejected
    assert len(v._reject_notes) == 1
    assert v._reject_timer.isActive()

    # A valid move (6K lane 5 -> LOAD lane 5) clears any pending shake.
    v._move_selection(0, 0, mode_jump=1)
    assert next(iter(v.selection))[0] == IMPORT_MODE
    assert v._reject_notes == []
    assert not v._reject_timer.isActive()


def test_edit_move_modifiers():
    """Edit-mode note movement: Ctrl+Up/Down snaps to the secondary grid,
    Shift+Up/Down nudges one pixel (free placement), plain Up/Down steps one
    primary cell, and Left/Right walk the lanes with 4K-left / LOAD-right walls."""
    from slimbms.model import IMPORT_MODE

    def fresh():
        p = Project(bpm=120, measures=8)
        v = ChartView(p)
        v.set_grid_main(16)
        v.set_grid_sub(12)
        v.set_mode("edit")
        v.refresh()
        v.measure_px = 160
        return p, v

    def sole(v):
        return next(iter(v.selection))[1]

    _app()
    # Ctrl steps floor(main / secondary) primary cells (32-grid, /8 secondary ->
    # 4 cells): 0 -> 4/32 -> 8/32, and back.
    p, v = fresh()
    v.set_grid_main(32)
    v.set_grid_sub(8)
    n = Note(2, Fraction(0), 0)
    p.charts[4].append(n)
    v.selection = {(4, n)}
    v._move_selection(0, 1, "sub")
    assert sole(v).pos == Fraction(4, 32)
    v._move_selection(0, 1, "sub")
    assert sole(v).pos == Fraction(8, 32)
    v._move_selection(0, -1, "sub")
    assert sole(v).pos == Fraction(4, 32)
    # A non-dividing ratio floors: 21-cell grid, /4 secondary -> 21 // 4 = 5 cells.
    v.set_grid_main(21)
    v.set_grid_sub(4)
    assert v._sub_step_cells() == 5

    # Shift nudges one pixel (free placement); plain steps one primary cell.
    p, v = fresh()
    n = Note(2, Fraction(0), 0)
    p.charts[4].append(n)
    v.selection = {(4, n)}
    v._move_selection(0, 1, "px")
    assert sole(v).pos == Fraction(1, 160)          # 1 px at measure_px 160
    p, v = fresh()
    n = Note(2, Fraction(0), 0)
    p.charts[4].append(n)
    v.selection = {(4, n)}
    v._move_selection(0, 1, "main")
    assert sole(v).pos == Fraction(1, 16)

    # Lane walls: 4K lane 0 can't go left; a right move crosses 4K -> 6K.
    p, v = fresh()
    n = Note(2, Fraction(0), 0)
    p.charts[4].append(n)
    v.selection = {(4, n)}
    v._move_selection(-1, 0)
    assert next(iter(v.selection))[0] == 4 and sole(v).lane == 0   # blocked, unchanged
    n2 = Note(2, Fraction(0), 3)                                   # rightmost 4K lane
    p.charts[4] = [n2]
    v.selection = {(4, n2)}
    v._move_selection(1, 0)
    assert next(iter(v.selection))[0] == 6 and sole(v).lane == 0   # 4K -> 6K
    # LOAD rightmost lane can't go further right.
    n3 = Note(2, Fraction(0), 7)
    p.charts[IMPORT_MODE] = [n3]
    v.selection = {(IMPORT_MODE, n3)}
    v._move_selection(1, 0)
    assert next(iter(v.selection))[0] == IMPORT_MODE               # blocked, still LOAD


def test_edit_shift_drag_is_free_placement():
    """A Shift mouse-drag moves a note with 1px (free) placement, a plain drag
    snaps to the grid, and neither leaves a duplicate behind."""
    from PySide6.QtCore import Qt, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent

    _app()
    def setup():
        p = Project(bpm=120, measures=8)
        v = ChartView(p)
        v.set_grid_main(16)
        v.set_mode("edit")
        v.refresh()
        v.resize(1400, 3000)
        v.measure_px = 160
        return p, v

    def drag(v, x, y, dy, mods):
        v.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(x, y),
                                      Qt.LeftButton, Qt.LeftButton, mods))
        v.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x, y + dy),
                                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        v.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(x, y + dy),
                                        Qt.LeftButton, Qt.NoButton, Qt.NoModifier))

    # Shift-drag an unselected note up 16px -> free placement, no duplicate.
    p, v = setup()
    n = Note(2, Fraction(0), 0)
    p.charts[4].append(n)
    x = v._col_x()[(4, 0)] + v.lane_w / 2
    drag(v, x, v.y_for(n.absolute), -16, Qt.ShiftModifier)
    assert len(p.charts[4]) == 1
    assert p.charts[4][0].pos == Fraction(16, 160)   # 16 px, off the 1/16 grid

    # A plain drag of 7px snaps to the nearest grid cell.
    p, v = setup()
    n = Note(2, Fraction(0), 0)
    p.charts[4].append(n)
    x = v._col_x()[(4, 0)] + v.lane_w / 2
    drag(v, x, v.y_for(n.absolute), -7, Qt.NoModifier)
    assert len(p.charts[4]) == 1
    assert p.charts[4][0].pos == Fraction(1, 16)


def test_arrow_move_skips_collapsed_cells():
    """Arrow-key vertical moves step through display space: from a shortened
    measure's last visible cell the note jumps to the next measure's first cell,
    not through the measure's hidden (collapsed) cells."""
    _app()
    p = Project(bpm=120, measures=8)
    p.measure_scales[3] = Fraction(24, 32)             # measure 3 shrunk to 24 cells
    v = ChartView(p)
    v.set_grid_main(32)
    v.set_mode("edit")
    v.refresh()

    n = Note(3, Fraction(23, 32), 0)                   # last visible cell
    p.charts[4].append(n)
    v.selection = {(4, n)}
    v._move_selection(0, 1)                            # up one cell
    moved = next(iter(v.selection))[1]
    assert moved.measure == 4 and moved.pos == Fraction(0)

    v._move_selection(0, -1)                           # back down one cell
    moved = next(iter(v.selection))[1]
    assert moved.measure == 3 and moved.pos == Fraction(23, 32)


def test_hit_flash_across_shortened_measure():
    """A note in the first cell right after a shortened measure must still flash
    when the playhead crosses it. The absolute position jumps at the boundary of
    a collapsed measure, so hit detection judges the crossing in display space —
    otherwise the jump looks like a seek and the flash is skipped (issue: a note
    at measure 54 cell 0 after halving measure 53 never lit)."""
    _app()
    p = Project(bpm=120, measures=8)
    p.charts[4].append(Note(4, Fraction(0), 0))    # first cell of measure 4
    p.measure_scales[3] = Fraction(1, 2)           # halve the measure before it
    v = ChartView(p)
    v.refresh()
    v.set_live(True)
    v.set_playhead(3.49)                           # inside collapsed measure 3
    v.set_playhead(4.02)                           # into measure 4, past the note
    flashed = [n for (_mode, n) in v._hits]
    assert any(n.measure == 4 and n.pos == Fraction(0) for n in flashed)


def test_colx_cache_tracks_layout():
    """The cached col_x lookup must stay consistent with the live columns after
    every layout change."""
    _app()
    v = ChartView(Project(bpm=120, measures=16))
    for change in (lambda: v.set_lane_width(50), lambda: v.set_bgm_width(90),
                   lambda: v.set_lane_width(20)):
        change()
        expected = {(c.key_mode, c.lane): c.x
                    for c in v.columns if c.kind == "key"}
        assert v._col_x() == expected


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
