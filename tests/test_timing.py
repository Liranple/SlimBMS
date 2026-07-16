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


def test_variable_bpm():
    # 120 bpm (0.5 m/s) for measures 0-2, then 240 bpm (1.0 m/s) after.
    p = Project(bpm=120.0)
    p.toggle_bgm(0, Fraction(0))
    p.bpm_changes[Fraction(2)] = 240.0
    tm = TimeMap(p)
    assert abs(tm.audio_seconds(2.0) - 4.0) < 1e-9     # 2 measures @120 = 4s
    assert abs(tm.audio_seconds(3.0) - 5.0) < 1e-9     # +1 measure @240 = 1s
    assert abs(tm.chart_pos(4.0) - 2.0) < 1e-9
    assert abs(tm.chart_pos(5.0) - 3.0) < 1e-9
    # Round-trips through the tempo change.
    for secs in (0.0, 2.0, 4.0, 6.5):
        assert abs(tm.audio_seconds(tm.chart_pos(secs)) - secs) < 1e-6


def test_variable_measure_length():
    # 120 bpm -> 0.5 m/s -> 2 s per full measure. Halving measure 1 makes it take
    # 1 s, so everything after it plays 1 s earlier and stays in sync.
    p = Project(bpm=120.0)
    p.toggle_bgm(0, Fraction(0))
    p.measure_scales[1] = Fraction(1, 2)
    tm = TimeMap(p)
    assert abs(tm.audio_seconds(1.0) - 2.0) < 1e-9      # measure 1 starts at 2 s
    assert abs(tm.audio_seconds(2.0) - 3.0) < 1e-9      # +1 s for the half measure
    assert abs(tm.audio_seconds(3.0) - 5.0) < 1e-9      # then full 2 s measures
    # A note at real offset 1/4 inside the half measure keeps that offset in time.
    assert abs(tm.audio_seconds(1.25) - 2.5) < 1e-9
    for secs in (0.0, 2.5, 3.0, 7.0):
        assert abs(tm.audio_seconds(tm.chart_pos(secs)) - secs) < 1e-6


def test_bpm_at():
    p = Project(bpm=100.0)
    p.bpm_changes[Fraction(4)] = 150.0
    assert p.bpm_at(Fraction(0)) == 100.0
    assert p.bpm_at(Fraction(3)) == 100.0
    assert p.bpm_at(Fraction(4)) == 150.0
    assert p.bpm_at(Fraction(10)) == 150.0


def test_snapshot_restore():
    from slimbms.model import Note
    p = Project(bpm=120.0)
    p.charts[4].append(Note(0, Fraction(0), 0))
    p.bpm_changes[Fraction(2)] = 140.0
    snap = p.snapshot()
    p.charts[4].append(Note(1, Fraction(0), 1))
    p.bpm_changes[Fraction(3)] = 90.0
    p.measures = 40
    p.restore(snap)
    assert len(p.charts[4]) == 1
    assert dict(p.bpm_changes) == {Fraction(2): 140.0}
    assert p.measures == 16


def test_snapshot_excludes_base_bpm():
    # Base BPM is metadata (edited via the sidebar like title/level) and is NOT
    # part of the undo history, so snapshot/restore must leave it untouched
    # rather than reverting a later base-BPM edit.
    p = Project(bpm=120.0)
    snap = p.snapshot()
    p.bpm = 180.0
    p.restore(snap)
    assert p.bpm == 180.0


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
