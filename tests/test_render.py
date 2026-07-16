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
