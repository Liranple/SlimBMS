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
