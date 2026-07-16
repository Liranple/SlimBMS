"""Conversion between audio time (seconds) and chart position (measures).

Supports mid-song tempo changes AND per-measure lengths (BMS channel 02). The
BPM is piecewise-constant over *real* position — the running sum of measure
lengths — so a shortened measure genuinely takes less time and everything after
it plays earlier. Audio ``t = 0`` corresponds to the first BGM object's chart
position, so moving the BGM marker shifts where the song lines up.
"""

from __future__ import annotations

import bisect
from fractions import Fraction

from .model import Project

BEATS_PER_MEASURE = 4


def _mps(bpm: float) -> float:
    """Measures per second for a BPM in 4/4 time."""
    return max(1.0, bpm) / 60.0 / BEATS_PER_MEASURE


class TimeMap:
    def __init__(self, project: Project) -> None:
        self._measures = project.measures
        # Cumulative real length up to the start of each measure (floats).
        self._lp = [float(x) for x in project.cumulative_lengths()]
        self._len = [float(project.measure_length(m)) for m in range(self._measures)]

        self.t0 = self._real(float(min((n.absolute for n in project.bgm),
                                       default=Fraction(0))))

        # Tempo breakpoints in REAL position, from the song start (t0) onward.
        changes = sorted((self._real(float(p)), max(1.0, b))
                         for p, b in project.bpm_changes.items())
        start_bpm = max(1.0, project.bpm)
        for r, b in changes:
            if r <= self.t0:
                start_bpm = b
        self._rpos = [self.t0]
        self._bpm = [start_bpm]
        for r, b in changes:
            if r > self.t0:
                self._rpos.append(r)
                self._bpm.append(b)
        # Cumulative seconds at each breakpoint.
        self._sec = [0.0]
        for i in range(1, len(self._rpos)):
            dpos = self._rpos[i] - self._rpos[i - 1]
            self._sec.append(self._sec[-1] + dpos / _mps(self._bpm[i - 1]))

    # -- absolute <-> real (measure-length aware) --------------------------- #

    def _real(self, absolute: float) -> float:
        """Absolute chart position -> real position (running length sum)."""
        m = int(absolute)
        if m < 0:
            return absolute
        if m >= self._measures:
            return self._lp[self._measures] + (absolute - self._measures)
        frac = absolute - m
        return self._lp[m] + min(frac, self._len[m])

    def _absolute(self, real: float) -> float:
        """Real position -> absolute chart position (inverse of :meth:`_real`)."""
        if real <= 0.0:
            return 0.0
        total = self._lp[self._measures]
        if real >= total:
            return float(self._measures) + (real - total)
        m = bisect.bisect_right(self._lp, real) - 1
        m = max(0, min(self._measures - 1, m))
        return m + (real - self._lp[m])

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

    def _seg_by_real(self, r: float) -> int:
        i = 0
        for k in range(len(self._rpos)):
            if self._rpos[k] <= r:
                i = k
            else:
                break
        return i

    def chart_pos(self, audio_seconds: float) -> float:
        """Absolute chart position (measures) for a playback time."""
        s = max(0.0, audio_seconds)
        i = self._seg_by_seconds(s)
        real = self._rpos[i] + (s - self._sec[i]) * _mps(self._bpm[i])
        return self._absolute(real)

    def audio_seconds(self, chart_pos: float) -> float:
        """Playback time for an absolute chart position (clamped at 0)."""
        r = self._real(float(chart_pos))
        if r <= self._rpos[0]:
            return 0.0
        i = self._seg_by_real(r)
        return self._sec[i] + (r - self._rpos[i]) / _mps(self._bpm[i])
