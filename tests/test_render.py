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


def test_measure_scale_blocks_shrink_past_notes():
    """A measure can't collapse below a cell that holds a note."""
    _app()
    p = Project(bpm=120, measures=8)
    p.charts[4].append(Note(3, Fraction(1, 2), 0))    # note at the measure midpoint
    v = ChartView(p)
    v.set_grid_main(32)
    v._set_measure_cells(3, 4)                         # try to over-shrink
    assert v._current_cells(3) == 17                   # clamped to keep the note


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
