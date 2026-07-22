"""Offscreen rendering tests for the chart canvas.

Guards the P3 hot-path optimizations (grid-line culling to the visible strip
must draw exactly what a full-song scan would) and the chart-axis coordinate
system: positions live on the cumulative-measure-length axis, so resizing a
measure moves barlines — never notes.

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
        gm_lo = max(0, v._measure_key(abs_bot) - 1)
        gm_hi = min(measures, v._measure_key(abs_top) + 2)
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
    p.charts[4].append(Note(Fraction(13, 4), 0))
    p.charts[6].append(Note(Fraction(10), 2, Fraction(1, 2)))   # long note
    p.bgm.add(Note(Fraction(0), 0))
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
    """A per-measure length scales that measure's height and grid-cell count;
    y_for/absolute_at stay invertible and the later barlines are pulled up
    with no gap (the axis is contiguous)."""
    _app()
    p = Project(bpm=120, measures=8)
    v = ChartView(p)
    v.measure_px = 150
    v.set_grid_main(32)
    full_h = v.height()
    v._set_measure_cells(2, 16)                       # half of measure 2
    assert p.measure_scales[2] == Fraction(1, 2)
    assert v.height() == full_h - v.measure_px // 2   # lost half a measure
    for a in (0.0, 1.0, 2.0, 2.25, 2.5, 6.0):         # round-trips on the axis
        assert abs(v.absolute_at(v.y_for(a)) - a) < 1e-6
    # Measure 3 now starts right where measure 2's half ends — no gap.
    assert v._vprefix[3] == 2.5
    assert abs(v.y_for(v._vprefix[3]) - (v.y_for(2.0) - v.measure_px / 2)) < 1e-6
    assert len(list(v._grid_line_ys(v.grid_main, 2, 3))) == 15   # 16 cells
    assert len(list(v._grid_line_ys(v.grid_main, 1, 2))) == 31   # 32 cells


def test_measure_stretch_geometry_and_cap():
    """A measure can now stretch past full length (up to 2×): it gains height
    and grid cells, notes place into the extended region, and resizing still
    never touches note data. The drag cap clamps at double length."""
    _app()
    p = Project(bpm=120, measures=8)
    v = ChartView(p)
    v.measure_px = 150
    v.set_grid_main(32)
    full_h = v.height()
    v._set_measure_cells(2, 48)                       # 1.5× measure
    assert p.measure_scales[2] == Fraction(3, 2)
    assert v.height() == full_h + v.measure_px // 2   # gained half a measure
    assert len(list(v._grid_line_ys(v.grid_main, 2, 3))) == 47   # 48 cells
    # A note in the extended region (cell 40 of 48) is a normal axis position.
    n = Note(p.position(2, Fraction(40, 32)), 0)
    p.charts[4].append(n)
    v.refresh()
    assert p.locate(n.absolute) == (2, Fraction(40, 32))
    # Shrinking the measure back re-buckets it into measure 3 — data unchanged.
    v._set_measure_cells(2, 32)
    assert p.charts[4] == [n]
    assert p.locate(n.absolute) == (3, Fraction(8, 32))
    # The resize cap: a runaway drag clamps at double length, floor at 1 cell.
    v._set_measure_cells(2, 500)
    assert p.measure_scales[2] == Fraction(2)
    v._set_measure_cells(2, 0)
    assert p.measure_scales[2] == Fraction(1, 32)


def test_measure_scale_keeps_note_positions():
    """Resizing a measure moves only the barlines: every note keeps its
    chart-axis position and length — including its on-screen distance from the
    song start — while the derived (measure, cell) view re-buckets."""
    _app()
    p = Project(bpm=120, measures=8)
    taps = [Note(Fraction(3) + Fraction(c, 32), 0) for c in (0, 16, 22, 30)]
    ln = Note(Fraction(3) + Fraction(31, 32), 1, Fraction(2, 32))  # crosses 3|4
    p.charts[4].extend(taps + [ln])
    v = ChartView(p)
    v.set_grid_main(32)
    v.refresh()
    baseline = [(n.absolute, n.length) for n in p.charts[4]]
    dist0 = [v.y_for(0.0) - v.y_for(float(n.absolute)) for n in p.charts[4]]

    for cells in (16, 1, 24, 32):          # shrink, shrink hard, grow, restore
        v._set_measure_cells(3, cells)
        assert [(n.absolute, n.length) for n in p.charts[4]] == baseline
        assert [v.y_for(0.0) - v.y_for(float(n.absolute))
                for n in p.charts[4]] == dist0
    # Fully restored: the derived cell view is back where it started.
    assert p.locate(ln.absolute) == (3, Fraction(31, 32))
    # While shrunk to 16 cells the same note reads as measure-4 content.
    v._set_measure_cells(3, 16)
    assert p.locate(ln.absolute) == (4, Fraction(15, 32))
    assert p.locate(ln.end_absolute) == (4, Fraction(17, 32))


def test_boundary_long_note_survives_measure_shrink():
    """The reported bug: a 2-cell hold from the last cell of measure 80 into
    the first cell of measure 81 must keep its position and 2-cell length no
    matter how measure 80 is resized (16→15→…→1→16 cells)."""
    _app()
    p = Project(bpm=120, measures=100)
    ln = Note(Fraction(80) + Fraction(15, 16), 0, Fraction(2, 16))
    p.charts[4].append(ln)
    v = ChartView(p)
    v.set_grid_main(16)
    v.refresh()
    for cells in (15, 14, 8, 1, 16):
        v._set_measure_cells(80, cells)
        assert p.charts[4] == [ln], f"note rewritten at {cells} cells"
    assert p.locate(ln.absolute) == (80, Fraction(15, 16))   # fully restored


def test_scale_edit_never_touches_long_notes():
    """Long notes are never rewritten by measure resizes — including the old
    drift bug's setup (a tail on a barline after a run of 1-cell measures,
    then resizing a measure after the tail; the nominal-axis reflow used to
    grow the note a little on every pass)."""
    _app()
    p = Project(bpm=120, measures=140)
    for m in (70, 71, 85, 96, 97, 104, 112):        # visual-gimmick measures
        p.measure_scales[m] = Fraction(1, 32)
    ln = Note(Fraction(65), 0, p.position(113, Fraction(0)) - Fraction(65))
    p.charts[4].append(ln)
    v = ChartView(p)
    v.set_grid_main(32)
    v.refresh()
    for cells in (16, 32, 1, 32):        # resize a measure after the tail
        v._set_measure_cells(113, cells)
        assert p.charts[4] == [ln]
    for cells in (16, 32):               # and one the note's body spans
        v._set_measure_cells(85, cells)
        assert p.charts[4] == [ln]


def test_measure_shrink_survives_viewport_scroll_mid_drag():
    """The host may scroll the viewport mid-drag, which shifts widget-local y
    under a stationary mouse. The ruler drag must key off screen coordinates,
    or that shift reads as a huge upward drag and snaps the measure back to
    full length (bug: the shrink cancelled itself on the first try and only
    worked on a retry). The release must emit one settle `changed` so the
    timeline autofit can land."""
    from PySide6.QtCore import Qt, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent
    import slimbms.ui.layout as L

    _app()
    p = Project(bpm=120, measures=8)
    note = Note(Fraction(3) + Fraction(20, 32), 0)   # sits in the range we collapse
    p.charts[4].append(note)
    v = ChartView(p)
    v.set_grid_main(32)
    v.measure_px = 160
    v.refresh()
    v.resize(v._width, 2000)

    x = L.LEFT_MARGIN / 2
    y = v.y_for(3.0)
    cell_px = v.measure_px * float(v.grid_main)         # 5 px per cell

    def ev(kind, local_y, global_y, btn, btns):
        return QMouseEvent(kind, QPointF(x, local_y), QPointF(x, global_y),
                           btn, btns, Qt.NoModifier)

    v.mousePressEvent(ev(QEvent.MouseButtonPress, y, y, Qt.LeftButton, Qt.LeftButton))
    # Drag up 12 cells. Between the two moves the viewport scrolls by 300px, so
    # local y jumps while the physical (global) mouse position barely moves.
    v.mouseMoveEvent(ev(QEvent.MouseMove, y - 6 * cell_px, y - 6 * cell_px,
                        Qt.NoButton, Qt.LeftButton))
    v.mouseMoveEvent(ev(QEvent.MouseMove, y - 12 * cell_px + 300, y - 12 * cell_px,
                        Qt.NoButton, Qt.LeftButton))
    fired = []
    v.changed.connect(lambda: fired.append(v.is_scaling()))
    v.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, y - 12 * cell_px + 300,
                           y - 12 * cell_px, Qt.LeftButton, Qt.NoButton))

    assert v._current_cells(3) == 20        # stayed shrunk, not reset to 32
    n = p.charts[4][0]
    assert n.absolute == note.absolute      # the note itself never moved…
    assert p.locate(n.absolute) == (4, Fraction(0))   # …but now reads as measure 4
    assert fired and fired[-1] is False     # release emitted a settle signal


def test_move_long_note_keeps_length_across_shortened_measure():
    """Dragging a long note in edit mode translates it on the chart axis, so
    both its true and visible length are unchanged even inside a shortened
    measure. Edit mode must never change a long note's length."""
    from PySide6.QtCore import Qt, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent

    _app()
    p = Project(bpm=120, measures=10)
    p.measure_scales[3] = Fraction(16, 32)             # measure 3 halved
    ln = Note(Fraction(3) + Fraction(5, 32), 0, Fraction(7, 32))
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
    assert moved.length == ln.length                   # true length unchanged
    assert vis(moved) == before                        # visible length unchanged
    assert moved.absolute == ln.absolute + Fraction(6, 32)


def test_arrow_move_long_note_across_one_cell_measures():
    """Arrow keys translate a long note on the chart axis — through a run of
    1-cell measures its length (true == visible) never changes."""
    _app()
    p = Project(bpm=120, measures=80)
    for m in range(66, 73):                            # 66..72 shrunk to 1 cell
        p.measure_scales[m] = Fraction(1, 32)
    ln = Note(Fraction(65), 0, Fraction(4, 32))        # 4-cell hold in measure 65
    p.charts[4].append(ln)
    v = ChartView(p)
    v.set_grid_main(32)
    v.set_mode("edit")
    v.refresh()

    cur = ln
    # 45 one-cell steps: clears measure 65 and the whole 1-cell run.
    for i in range(45):
        v.selection = {(4, cur)}
        v._move_selection(0, 1)
        cur = next(iter(v.selection))[1]
        assert cur.length == ln.length, f"length changed at step {i}"
        assert cur.absolute == ln.absolute + Fraction(i + 1, 32)
    assert p.locate(cur.absolute)[0] > 72              # actually got past the run


def test_paste_fills_free_space_in_clicked_measure():
    """A single-measure block pasted into a measure whose top is full drops into
    that same measure's free vertical space (top-down, first gap it fits) — here
    cells 0–16 are taken, so the copy lands in cells 16–32."""
    _app()
    p = Project(bpm=120, measures=16)
    step = Fraction(1, 32)
    for i in range(16):                       # measure 5, cells 0..15 full
        p.charts[4].append(Note(5 + i * step, 0))
    p.charts[4].append(Note(Fraction(6), 0))  # measure 6 has a note (like "46마디")
    v = ChartView(p)
    v.set_grid_main(32)
    v.refresh()
    v.selection = {(4, n) for n in list(p.charts[4]) if 5 <= n.absolute < 6}
    v._copy_selection(cut=False)

    v._paste_anchor = 5.0                     # click measure 5, paste
    v._paste()
    got = sorted(p.locate(n.absolute) for _m, n in v.selection)
    assert got == [(5, (16 + i) * step) for i in range(16)]


def test_paste_overlaps_in_place_when_no_room():
    """When the block is taller than the free space left in the clicked measure,
    it's duplicated in place (overlapping) at that measure — never pushed to an
    empty measure elsewhere."""
    _app()
    p = Project(bpm=120, measures=16)
    step = Fraction(1, 32)
    for i in range(20):                       # cells 0..19 full → only 12 free < 20
        p.charts[4].append(Note(5 + i * step, 0))
    v = ChartView(p)
    v.set_grid_main(32)
    v.refresh()
    v.selection = {(4, n) for n in list(p.charts[4])}
    v._copy_selection(cut=False)
    before = len(p.charts[4])

    v._paste_anchor = 5.0
    v._paste()
    assert len(p.charts[4]) == before + 20                  # 20 duplicates added
    got = sorted(p.locate(n.absolute) for _m, n in v.selection)
    assert got == [(5, i * step) for i in range(20)]


def test_paste_multi_measure_block_lands_at_clicked_measure():
    """A block spanning several measures pastes at the clicked measure keeping
    each note's measure offset — without carrying copied measure lengths or
    hunting for empty measures."""
    _app()
    p = Project(bpm=120, measures=16)
    for m in (3, 4, 5):
        p.measure_scales[m] = Fraction(24, 32)     # copied from shortened measures
    for m in (3, 4, 5):
        p.charts[4].append(Note(p.position(m, Fraction(0)), 0))
    v = ChartView(p)
    v.set_grid_main(32)
    v.refresh()
    v.selection = {(4, n) for n in list(p.charts[4])}
    v._copy_selection(cut=False)

    v._paste_anchor = float(p.position(8, Fraction(0)))
    v._paste()
    pasted = sorted(p.locate(n.absolute) for _m, n in v.selection)
    assert pasted == [(8, Fraction(0)), (9, Fraction(0)), (10, Fraction(0))]
    # Target measures keep their own (full) length — copied scales aren't applied.
    assert all(p.measure_length(m) == Fraction(1) for m in (8, 9, 10))


def test_paste_into_shortened_measure_never_hides_notes():
    """Pasting a block copied from a full measure into a shorter one keeps
    every note visible: an offset past the target measure's end simply lands
    in the next measure — the axis has no hidden region to fall into."""
    _app()
    p = Project(bpm=120, measures=16)
    step = Fraction(1, 32)
    a, b = Note(Fraction(3), 0), Note(3 + 28 * step, 0)
    p.charts[4].extend([a, b])
    p.measure_scales[8] = Fraction(16, 32)      # target is half length
    v = ChartView(p)
    v.set_grid_main(32)
    v.refresh()
    v.selection = {(4, a), (4, b)}
    v._copy_selection(cut=False)
    v._paste_anchor = float(p.position(8, Fraction(0)))
    v._paste()
    got = sorted(p.locate(n.absolute) for _m, n in v.selection)
    # Cell 0 fits in measure 8; cell 28 overflows the 16-cell measure into 9.
    assert got == [(8, Fraction(0)), (9, Fraction(12, 32))]


def test_paste_requests_focus_on_pasted_block():
    """Pasting asks the window to scroll to the pasted block, so resizing the
    canvas can't leave the view somewhere else."""
    _app()
    p = Project(bpm=120, measures=16)
    for m in (3, 4):
        p.charts[4].append(Note(Fraction(m), 0))
    v = ChartView(p)
    v.set_grid_main(32)
    v.refresh()
    v.selection = {(4, n) for n in list(p.charts[4])}
    v._copy_selection(cut=False)

    seen = []
    v.focus_requested.connect(lambda lo, hi: seen.append((lo, hi)))
    v._paste_anchor = 8.0
    v._paste()
    assert len(seen) == 1
    lo, hi = seen[0]
    # Block spans measures 8–9; focus covers through the end of measure 9.
    assert lo == 8.0
    assert abs(hi - 10.0) < 1e-9


def test_alt_drag_copies_selection():
    """Alt + mouse drag on a selected note leaves the original in place and drops
    a moved duplicate (a plain drag would move it; Alt copies)."""
    from PySide6.QtCore import Qt, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent

    _app()
    p = Project(bpm=120, measures=8)
    v = ChartView(p)
    v.set_grid_main(16)
    v.set_mode("edit")
    v.refresh()
    v.resize(1400, 3000)
    v.measure_px = 160
    n = Note(Fraction(2), 0)
    p.charts[4].append(n)
    x = v._col_x()[(4, 0)] + v.lane_w / 2
    y = v.y_for(n.absolute)
    # Alt-drag up one cell (10 px at measure_px 160 snaps to the 1/16 grid).
    v.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(x, y),
                                  Qt.LeftButton, Qt.LeftButton, Qt.AltModifier))
    v.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x, y - 10),
                                 Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    v.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(x, y - 10),
                                    Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    positions = sorted(nn.absolute for nn in p.charts[4])
    assert len(p.charts[4]) == 2                        # original + copy
    assert positions == [Fraction(2), 2 + Fraction(1, 16)]  # original stayed, copy moved
    # The copy (not the original) ends up selected for further nudging.
    assert len(v.selection) == 1
    assert next(iter(v.selection))[1].absolute == 2 + Fraction(1, 16)


def test_alt_click_without_drag_makes_no_copy():
    """A bare Alt-click (no movement) on a note must not create a duplicate."""
    from PySide6.QtCore import Qt, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent

    _app()
    p = Project(bpm=120, measures=8)
    v = ChartView(p)
    v.set_grid_main(16)
    v.set_mode("edit")
    v.refresh()
    v.resize(1400, 3000)
    v.measure_px = 160
    n = Note(Fraction(2), 0)
    p.charts[4].append(n)
    x = v._col_x()[(4, 0)] + v.lane_w / 2
    y = v.y_for(n.absolute)
    v.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(x, y),
                                  Qt.LeftButton, Qt.LeftButton, Qt.AltModifier))
    v.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(x, y),
                                    Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    assert len(p.charts[4]) == 1                        # no duplicate


def test_playhead_cell_snaps_measure_locally():
    """Recording snap quantises within the measure under the playhead; near a
    shortened measure's end it rounds onto the next measure's first cell —
    never into cells the measure doesn't have."""
    _app()
    p = Project(bpm=120, measures=8)
    p.measure_scales[3] = Fraction(8, 32)       # 8-cell measure: axis [3, 3.25)
    v = ChartView(p)
    v.set_grid_main(32)
    v.refresh()
    v.playhead = 3.0 + 7.4 / 32                 # inside cell 7
    assert v.playhead_cell(True) == (3, Fraction(7, 32))
    v.playhead = 3.0 + 7.9 / 32                 # rounds up past the measure's end
    assert v.playhead_cell(True) == (4, Fraction(0))


def test_ctrl_mode_jump():
    """Ctrl+Left/Right hops to the adjacent key mode keeping the lane index,
    aborting the whole move if any note's lane doesn't exist there."""
    from slimbms.model import IMPORT_MODE

    _app()
    def setup(mode, lanes):
        p = Project(bpm=120, measures=8)
        notes = [Note(Fraction(2), L) for L in lanes]
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
        p.charts[6].append(Note(202 + Fraction(c, 32), 0))
    v = ChartView(p)
    v.set_grid_main(32)
    v.set_grid_sub(12)
    v.set_mode("edit")
    v.refresh()
    v.selection = {(6, n) for n in list(p.charts[6])}

    def positions():
        return sorted(n.absolute for n in p.charts[6])
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
    n = Note(Fraction(2), 5)                        # 6K lane 5 (no 4K equivalent)
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
    n = Note(Fraction(2), 0)
    p.charts[4].append(n)
    v.selection = {(4, n)}
    v._move_selection(0, 1, "sub")
    assert sole(v).absolute == 2 + Fraction(4, 32)
    v._move_selection(0, 1, "sub")
    assert sole(v).absolute == 2 + Fraction(8, 32)
    v._move_selection(0, -1, "sub")
    assert sole(v).absolute == 2 + Fraction(4, 32)
    # A non-dividing ratio floors: 21-cell grid, /4 secondary -> 21 // 4 = 5 cells.
    v.set_grid_main(21)
    v.set_grid_sub(4)
    assert v._sub_step_cells() == 5

    # Shift nudges one pixel (free placement); plain steps one primary cell.
    p, v = fresh()
    n = Note(Fraction(2), 0)
    p.charts[4].append(n)
    v.selection = {(4, n)}
    v._move_selection(0, 1, "px")
    assert sole(v).absolute == 2 + Fraction(1, 160)  # 1 px at measure_px 160
    p, v = fresh()
    n = Note(Fraction(2), 0)
    p.charts[4].append(n)
    v.selection = {(4, n)}
    v._move_selection(0, 1, "main")
    assert sole(v).absolute == 2 + Fraction(1, 16)

    # Lane walls: 4K lane 0 can't go left; a right move crosses 4K -> 6K.
    p, v = fresh()
    n = Note(Fraction(2), 0)
    p.charts[4].append(n)
    v.selection = {(4, n)}
    v._move_selection(-1, 0)
    assert next(iter(v.selection))[0] == 4 and sole(v).lane == 0   # blocked, unchanged
    n2 = Note(Fraction(2), 3)                                      # rightmost 4K lane
    p.charts[4] = [n2]
    v.selection = {(4, n2)}
    v._move_selection(1, 0)
    assert next(iter(v.selection))[0] == 6 and sole(v).lane == 0   # 4K -> 6K
    # LOAD rightmost lane can't go further right.
    n3 = Note(Fraction(2), 7)
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
    n = Note(Fraction(2), 0)
    p.charts[4].append(n)
    x = v._col_x()[(4, 0)] + v.lane_w / 2
    drag(v, x, v.y_for(n.absolute), -16, Qt.ShiftModifier)
    assert len(p.charts[4]) == 1
    assert p.charts[4][0].absolute == 2 + Fraction(16, 160)  # 16 px, off the grid
    # A plain drag of 7px snaps to the nearest grid cell.
    p, v = setup()
    n = Note(Fraction(2), 0)
    p.charts[4].append(n)
    x = v._col_x()[(4, 0)] + v.lane_w / 2
    drag(v, x, v.y_for(n.absolute), -7, Qt.NoModifier)
    assert len(p.charts[4]) == 1
    assert p.charts[4][0].absolute == 2 + Fraction(1, 16)


def test_mouse_drag_crosses_key_modes():
    """A mouse drag must walk the 4K→6K→LOAD lane continuum so a note can move
    between modes (regression: it used to clamp inside the note's own mode, so
    dragging 6K→4K/LOAD stuck at the mode edge and piled notes to one side)."""
    from PySide6.QtCore import Qt, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent
    from slimbms.model import IMPORT_MODE

    _app()

    def setup():
        p = Project(bpm=120, measures=8)
        v = ChartView(p)
        v.set_grid_main(16)
        v.set_mode("edit")
        v.refresh()
        v.resize(1600, 3000)
        v.measure_px = 160
        return p, v

    def drag(v, x0, x1, y):
        v.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(x0, y),
                                      Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        v.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x1, y),
                                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        v.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(x1, y),
                                        Qt.LeftButton, Qt.NoButton, Qt.NoModifier))

    def center(v, mode, lane):
        return v._col_x()[(mode, lane)] + v.lane_w / 2

    # 6K lane 0 -> drag to the 4K group (lane 1).
    p, v = setup()
    n = Note(Fraction(2), 0)
    p.charts[6].append(n)
    drag(v, center(v, 6, 0), center(v, 4, 1), v.y_for(n.absolute))
    assert len(p.charts[6]) == 0 and len(p.charts[4]) == 1
    assert p.charts[4][0].lane == 1

    # 4K lane 2 -> drag all the way into LOAD.
    p, v = setup()
    n = Note(Fraction(2), 2)
    p.charts[4].append(n)
    drag(v, center(v, 4, 2), center(v, IMPORT_MODE, 5), v.y_for(n.absolute))
    assert len(p.charts[4]) == 0 and len(p.charts[IMPORT_MODE]) == 1
    assert p.charts[IMPORT_MODE][0].lane == 5

    # Two notes dragged past the LOAD right edge shift together (collective
    # clamp) instead of both squishing onto the last lane.
    p, v = setup()
    a = Note(Fraction(2), 0)                   # LOAD lane 0
    b = Note(Fraction(2), 1)                   # LOAD lane 1
    p.charts[IMPORT_MODE].extend([a, b])
    v.selection = {(IMPORT_MODE, a), (IMPORT_MODE, b)}
    v._move_drag = {"origs": [(IMPORT_MODE, a), (IMPORT_MODE, b)],
                    "placed": [(IMPORT_MODE, a), (IMPORT_MODE, b)],
                    "px": center(v, IMPORT_MODE, 0), "py": v.y_for(a.absolute),
                    "moved": False, "free": False, "toggle": None}
    # Aim far right (well past the last LOAD lane).
    far_right = center(v, IMPORT_MODE, 7) + 500
    v.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(far_right, v.y_for(a.absolute)),
                                 Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    lanes = sorted(nn.lane for nn in p.charts[IMPORT_MODE])
    assert lanes == [6, 7]   # kept one lane apart, pinned to the right edge


def test_arrow_move_steps_through_shortened_measure():
    """Arrow-key vertical moves step one grid cell on the chart axis: from a
    shortened measure's last cell the note lands on the next measure's first
    cell (the axis has no hidden cells to cross)."""
    _app()
    p = Project(bpm=120, measures=8)
    p.measure_scales[3] = Fraction(24, 32)             # measure 3 shrunk to 24 cells
    v = ChartView(p)
    v.set_grid_main(32)
    v.set_mode("edit")
    v.refresh()

    n = Note(p.position(3, Fraction(23, 32)), 0)       # last cell of measure 3
    p.charts[4].append(n)
    v.selection = {(4, n)}
    v._move_selection(0, 1)                            # up one cell
    moved = next(iter(v.selection))[1]
    assert p.locate(moved.absolute) == (4, Fraction(0))

    v._move_selection(0, -1)                           # back down one cell
    moved = next(iter(v.selection))[1]
    assert p.locate(moved.absolute) == (3, Fraction(23, 32))


def test_hit_flash_across_shortened_measure():
    """A note in the first cell right after a shortened measure still flashes
    when the playhead crosses it: the chart axis is continuous across the
    boundary, so smooth playback never looks like a seek there."""
    _app()
    p = Project(bpm=120, measures=8)
    p.measure_scales[3] = Fraction(1, 2)           # halve measure 3: axis [3, 3.5)
    note = Note(p.position(4, Fraction(0)), 0)     # first cell of measure 4 = 3.5
    p.charts[4].append(note)
    v = ChartView(p)
    v.refresh()
    v.set_live(True)
    v.set_playhead(3.45)                           # inside shortened measure 3
    v.set_playhead(3.52)                           # into measure 4, past the note
    flashed = [n for (_mode, n) in v._hits]
    assert any(n.absolute == note.absolute for n in flashed)


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
