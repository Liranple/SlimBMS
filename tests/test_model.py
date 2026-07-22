"""Tests for the chart-axis helpers (locate/position/total_length).

Run: python tests/test_model.py
"""

import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slimbms.model import Project  # noqa: E402


def _scaled_project():
    """4 measures with measure 1 shortened to 1/2 and measure 2 to 3/4."""
    p = Project(measures=4)
    p.measure_scales[1] = Fraction(1, 2)
    p.measure_scales[2] = Fraction(3, 4)
    return p


def test_total_length():
    p = _scaled_project()
    assert p.total_length() == Fraction(1) + Fraction(1, 2) + Fraction(3, 4) + 1


def test_locate_position_roundtrip():
    p = _scaled_project()
    cum = p.cumulative_lengths()
    for m in range(p.measures):
        for num in range(0, 8):
            off = p.measure_length(m) * Fraction(num, 8)
            a = p.position(m, off, cum)
            assert p.locate(a, cum) == (m, off)


def test_locate_measure_boundaries():
    p = _scaled_project()
    # Measure starts land exactly on their own measure at offset 0.
    cum = p.cumulative_lengths()
    for m in range(p.measures):
        assert p.locate(cum[m]) == (m, Fraction(0))
    # A position inside shortened measure 1 stays there.
    assert p.locate(Fraction(5, 4)) == (1, Fraction(1, 4))
    # The old "collapsed tail" region now simply belongs to the next measure.
    assert p.locate(Fraction(7, 4)) == (2, Fraction(1, 4))


def test_locate_extrapolates_past_end():
    p = _scaled_project()
    total = p.total_length()
    assert p.locate(total) == (p.measures, Fraction(0))
    assert p.locate(total + Fraction(5, 2)) == (p.measures + 2, Fraction(1, 2))
    assert p.position(p.measures + 2, Fraction(1, 2)) == total + Fraction(5, 2)


def test_locate_negative_passes_through():
    p = _scaled_project()
    assert p.locate(Fraction(-3, 8)) == (0, Fraction(-3, 8))
    assert p.position(0, Fraction(-3, 8)) == Fraction(-3, 8)


def test_uniform_project_matches_int_split():
    p = Project(measures=8)                 # no scales: axis == measure + pos
    cum = p.cumulative_lengths()
    for a in (Fraction(0), Fraction(3, 2), Fraction(25, 4)):
        m, off = p.locate(a, cum)
        assert m == int(a) and off == a - int(a)


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
