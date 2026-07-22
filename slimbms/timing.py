"""Conversion between audio time (seconds) and chart position.

Positions everywhere are on the chart axis — the running sum of measure
lengths, so a shortened measure genuinely takes less time. Supports mid-song
tempo changes (BPM is piecewise-constant over the axis) AND STOP sequences
(channel 09): a STOP inserts a flat segment where the chart position (and thus
the playhead) holds still while seconds keep advancing — the audio keeps
playing but the scroll freezes. Audio ``t = 0`` corresponds to the first BGM
object's chart position, so moving the BGM marker shifts where the song lines
up.

All of this is precomputed once at construction into piecewise breakpoint
arrays, so per-frame :meth:`chart_pos` during playback is just a small bisect
lookup — no gimmick is re-evaluated every tick.
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
        self.total = float(project.total_length())
        self.t0 = float(min((n.absolute for n in project.bgm),
                            default=Fraction(0)))

        # Tempo breakpoints on the chart axis, from the song start (t0) onward.
        changes = sorted((float(p), max(1.0, b))
                         for p, b in project.bpm_changes.items())
        start_bpm = max(1.0, project.bpm)
        for r, b in changes:
            if r <= self.t0:
                start_bpm = b

        # Merge BPM changes and STOPs into one event stream ordered by chart
        # position, then walk it once building the piecewise breakpoint arrays.
        # ``kind`` 0 = BPM change, 1 = STOP; at the same position the BPM change
        # is applied first so the stop's duration uses the new tempo.
        events = [(r, 0, b) for r, b in changes if r > self.t0]
        for p, beats in project.stops.items():
            r = float(p)
            if r > self.t0 and beats > 0:
                events.append((r, 1, float(beats)))
        events.sort(key=lambda e: (e[0], e[1]))

        self._rpos = [self.t0]
        self._sec = [0.0]
        self._bpm = [start_bpm]
        for real, kind, val in events:
            last_r, last_sec, last_bpm = self._rpos[-1], self._sec[-1], self._bpm[-1]
            if real > last_r:
                # Normal segment advancing at the running tempo up to this event.
                self._rpos.append(real)
                self._sec.append(last_sec + (real - last_r) / _mps(last_bpm))
                self._bpm.append(last_bpm)
            if kind == 0:                                   # BPM change
                if self._rpos[-1] == real:
                    self._bpm[-1] = val
                else:
                    self._rpos.append(real)
                    self._sec.append(self._sec[-1])
                    self._bpm.append(val)
            else:                                           # STOP: flat segment
                # `val` beats hold at the current tempo: seconds advance, the
                # chart position (and the playhead) stay put.
                pause = val * 60.0 / max(1.0, self._bpm[-1])
                self._rpos.append(real)
                self._sec.append(self._sec[-1] + pause)
                self._bpm.append(self._bpm[-1])

    @property
    def measures_per_second(self) -> float:
        """Tempo at the song start — an estimate used only for auto-sizing."""
        return _mps(self._bpm[0])

    def _seg_by_seconds(self, s: float) -> int:
        # Last breakpoint with _sec <= s (arrays are non-decreasing).
        return max(0, bisect.bisect_right(self._sec, s) - 1)

    def _seg_by_real(self, r: float) -> int:
        return max(0, bisect.bisect_right(self._rpos, r) - 1)

    def chart_pos(self, audio_seconds: float) -> float:
        """Chart-axis position for a playback time."""
        s = max(0.0, audio_seconds)
        i = self._seg_by_seconds(s)
        real = self._rpos[i] + (s - self._sec[i]) * _mps(self._bpm[i])
        # Never run past the segment's end. For a STOP segment the next
        # breakpoint shares this position, so the playhead holds still for
        # the freeze; for a normal segment this is a no-op bound.
        if i + 1 < len(self._rpos):
            real = min(real, self._rpos[i + 1])
        return real

    def audio_seconds(self, chart_pos: float) -> float:
        """Playback time for a chart-axis position (clamped at 0)."""
        r = float(chart_pos)
        if r <= self._rpos[0]:
            return 0.0
        i = self._seg_by_real(r)
        return self._sec[i] + (r - self._rpos[i]) / _mps(self._bpm[i])
