"""Conversion between audio time (seconds) and chart position (measures).

Supports mid-song tempo changes: the BPM is piecewise-constant over absolute
chart position (4/4 time). Audio ``t = 0`` corresponds to the first BGM object's
chart position, so moving the BGM marker shifts where the song lines up.
"""

from __future__ import annotations

from fractions import Fraction

from .model import Project

BEATS_PER_MEASURE = 4


def _mps(bpm: float) -> float:
    """Measures per second for a BPM in 4/4 time."""
    return max(1.0, bpm) / 60.0 / BEATS_PER_MEASURE


class TimeMap:
    def __init__(self, project: Project) -> None:
        self.t0 = float(min((n.absolute for n in project.bgm), default=Fraction(0)))

        # Breakpoints (chart position, bpm) from the song start (t0) onward.
        changes = sorted((float(p), max(1.0, b)) for p, b in project.bpm_changes.items())
        start_bpm = max(1.0, project.bpm)
        for p, b in changes:
            if p <= self.t0:
                start_bpm = b
        self._pos = [self.t0]
        self._bpm = [start_bpm]
        for p, b in changes:
            if p > self.t0:
                self._pos.append(p)
                self._bpm.append(b)
        # Cumulative seconds at each breakpoint.
        self._sec = [0.0]
        for i in range(1, len(self._pos)):
            dpos = self._pos[i] - self._pos[i - 1]
            self._sec.append(self._sec[-1] + dpos / _mps(self._bpm[i - 1]))

    @property
    def measures_per_second(self) -> float:
        """Tempo at the song start — an estimate used only for auto-sizing."""
        return _mps(self._bpm[0])

    def _seg_by_seconds(self, s: float) -> int:
        i = 0
        for k in range(len(self._sec)):
            if self._sec[k] <= s:
                i = k
            else:
                break
        return i

    def _seg_by_pos(self, p: float) -> int:
        i = 0
        for k in range(len(self._pos)):
            if self._pos[k] <= p:
                i = k
            else:
                break
        return i

    def chart_pos(self, audio_seconds: float) -> float:
        """Absolute chart position (measures) for a playback time."""
        s = max(0.0, audio_seconds)
        i = self._seg_by_seconds(s)
        return self._pos[i] + (s - self._sec[i]) * _mps(self._bpm[i])

    def audio_seconds(self, chart_pos: float) -> float:
        """Playback time for an absolute chart position (clamped at 0)."""
        p = float(chart_pos)
        if p <= self._pos[0]:
            return 0.0
        i = self._seg_by_pos(p)
        return self._sec[i] + (p - self._pos[i]) / _mps(self._bpm[i])
