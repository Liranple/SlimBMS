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


def test_stop_freezes_playhead():
    # 120 bpm -> 2 s per measure. A 2-beat STOP at measure 1 (=2 s) freezes the
    # scroll for 1 s while the audio keeps rolling.
    p = Project(bpm=120.0)
    p.toggle_bgm(0, Fraction(0))
    p.stops[Fraction(1)] = Fraction(2)         # 2 beats at 120bpm = 1.0 s
    tm = TimeMap(p)
    # Reaches measure 1 at t=2.0, holds there through t=3.0, then resumes.
    assert abs(tm.chart_pos(2.0) - 1.0) < 1e-9
    for t in (2.0, 2.5, 3.0):
        assert abs(tm.chart_pos(t) - 1.0) < 1e-6   # frozen during the stop
    assert abs(tm.chart_pos(4.0) - 1.5) < 1e-9     # 1 s past the freeze = 0.5 measure
    # The playhead never runs backwards across the stop.
    vals = [tm.chart_pos(t / 20) for t in range(0, 120)]
    assert all(b >= a - 1e-9 for a, b in zip(vals, vals[1:]))
    # A position after the stop includes the paused second in its audio time.
    assert abs(tm.audio_seconds(2.0) - 5.0) < 1e-9  # 4 s travel + 1 s stop


def test_stop_with_no_stops_is_identity():
    # With no stops the timing must be exactly the plain-BPM result (no drift).
    a = Project(bpm=137.0)
    a.toggle_bgm(0, Fraction(0))
    a.bpm_changes[Fraction(3)] = 90.0
    tm = TimeMap(a)
    for secs in (0.0, 1.3, 4.0, 9.9):
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
    p.stops[Fraction(1)] = Fraction(2)
    p.scrolls[Fraction(1)] = Fraction(2)
    p.speeds[Fraction(2)] = Fraction(3, 2)
    snap = p.snapshot()
    p.charts[4].append(Note(1, Fraction(0), 1))
    p.bpm_changes[Fraction(3)] = 90.0
    p.stops[Fraction(5)] = Fraction(1)
    p.scrolls[Fraction(6)] = Fraction(-1)
    p.speeds.clear()
    p.measures = 40
    p.restore(snap)
    assert len(p.charts[4]) == 1
    assert dict(p.bpm_changes) == {Fraction(2): 140.0}
    assert dict(p.stops) == {Fraction(1): Fraction(2)}
    assert dict(p.scrolls) == {Fraction(1): Fraction(2)}
    assert dict(p.speeds) == {Fraction(2): Fraction(3, 2)}
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
