"""Tests for audio-time <-> chart-position conversion."""

import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slimbms.model import Project  # noqa: E402
from slimbms.timing import TimeMap  # noqa: E402


def test_measures_per_second():
    p = Project(bpm=120.0)               # 120 bpm, 4/4 -> 0.5 measures/sec
    tm = TimeMap(p)
    assert abs(tm.measures_per_second - 0.5) < 1e-9


def test_roundtrip_conversion():
    p = Project(bpm=150.0)
    tm = TimeMap(p)
    for secs in (0.0, 1.0, 3.7, 42.0):
        pos = tm.chart_pos(secs)
        assert abs(tm.audio_seconds(pos) - secs) < 1e-9


def test_bgm_offset_sets_t0():
    p = Project(bpm=120.0)
    p.toggle_bgm(2, Fraction(0))         # audio starts at chart measure 2
    tm = TimeMap(p)
    assert tm.t0 == 2.0
    # At t=0 the playhead sits at measure 2; 2 seconds later at measure 3.
    assert abs(tm.chart_pos(0.0) - 2.0) < 1e-9
    assert abs(tm.chart_pos(2.0) - 3.0) < 1e-9


def test_audio_seconds_clamped():
    p = Project(bpm=120.0)
    p.toggle_bgm(4, Fraction(0))
    tm = TimeMap(p)
    # Positions before the BGM start clamp to 0 (can't play negative time).
    assert tm.audio_seconds(0.0) == 0.0


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
