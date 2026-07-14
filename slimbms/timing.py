"""Conversion between audio time (seconds) and chart position (measures).

Assumes a constant BPM and 4/4 time for now (per-measure BPM/meter changes can
be layered on later). Audio ``t = 0`` corresponds to the first BGM object's
chart position, so moving the BGM marker shifts where the song lines up.
"""

from __future__ import annotations

from .model import Project

BEATS_PER_MEASURE = 4


class TimeMap:
    def __init__(self, project: Project) -> None:
        self.bpm = max(1.0, project.bpm)
        # Chart position (in measures) where audio playback starts.
        self.t0 = min((n.absolute for n in project.bgm), default=0.0)

    @property
    def measures_per_second(self) -> float:
        beats_per_second = self.bpm / 60.0
        return beats_per_second / BEATS_PER_MEASURE

    def chart_pos(self, audio_seconds: float) -> float:
        """Absolute chart position (measures) for a playback time."""
        return self.t0 + audio_seconds * self.measures_per_second

    def audio_seconds(self, chart_pos: float) -> float:
        """Playback time for an absolute chart position (clamped at 0)."""
        secs = (chart_pos - self.t0) / self.measures_per_second
        return max(0.0, secs)
