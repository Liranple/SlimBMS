"""Data model for SlimBMS.

A :class:`Project` holds one song's metadata plus independent charts
(one per key mode: 4K / 6K) that share the same BGM audio and timing.
Notes carry no keysounds — the sound of the song comes entirely from a single
BGM audio file placed on the BGM lane.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Set

# Sampling step used to turn a smooth BPM ramp into the discrete #BPM points that
# BMS (and our piecewise-constant TimeMap) actually understand: one point every
# 1/16 of a measure — a 16th note — which reads as a smooth accelerando/ritard.
RAMP_STEP = Fraction(1, 16)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

KEY_MODES = (4, 6)             # editable / exportable key modes
IMPORT_MODE = 8                # dedicated "import" lane group (A1~A8)
ALL_MODES = (4, 6, IMPORT_MODE)   # everything the project stores
DISPLAY_MODES = (4, 6, IMPORT_MODE)  # left-to-right order in the editor

# Lane index -> BMS object channel, per mode (1P visible channels).
# Channels are the standard BMS 1P keys: 11-15 = keys 1-5, 18/19 = keys 6/7,
# and 16 = scratch (never used here). The modes match uBMSC's playfields, as
# confirmed in-game (which channels each mode actually reads):
#   4K = keys 1,2,4,5      6K = keys 1,2,3,5,6,7
# The IMPORT_MODE group carries all eight 1P channels (A1~A8 in uBMSC) so any
# loaded .bms lands there without losing notes.
KEY_CHANNELS: Dict[int, List[str]] = {
    4: ["11", "12", "14", "15"],
    6: ["11", "12", "13", "15", "18", "19"],
    IMPORT_MODE: ["11", "12", "13", "14", "15", "16", "18", "19"],
}

# Per-lane colour code for each mode: 'W' white, 'B' blue, 'G' grey (import).
# Matches real key colours (white = odd key, blue = even key) for the channels
# above, so lanes look like the keyboard they map to.
LANE_COLORS: Dict[int, str] = {
    4: "WBBW",       # keys 1,2,4,5
    6: "WBWWBW",     # keys 1,2,3,5,6,7
    IMPORT_MODE: "GGGGGGGG",
}

DISPLAY_LABELS: Dict[int, str] = {
    4: "4K", 6: "6K", IMPORT_MODE: "LOAD",
}

# Channel used for the background-music object (whole-song audio start timing).
BGM_CHANNEL = "01"

# Keysound-less layout: the BGM object points at WAV01 (the imported song), so
# only the background music makes sound. Playable chart notes carry a separate
# marker (02) with NO #WAV defined for it, so hitting a note is silent — exactly
# uBMSC's "song on slot 1, notes on slot 2" workflow.
OBJ_VALUE = "01"          # BGM object -> WAV01 (the song)
BGM_WAV_INDEX = "01"
NOTE_VALUE = "02"         # chart notes -> silent slot (no #WAV02 emitted)


def lanes_for(key_mode: int) -> int:
    """Number of lanes for a key mode."""
    return len(KEY_CHANNELS[key_mode])


# --------------------------------------------------------------------------- #
# Note
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Note:
    """A single object at a snapped time position within a lane.

    ``pos`` is the fractional position inside the measure in ``[0, 1)`` — e.g.
    ``Fraction(1, 4)`` is the second beat of a 4/4 measure. Using an exact
    :class:`~fractions.Fraction` means positions round-trip through BMS output
    without floating-point drift.
    """

    measure: int
    pos: Fraction
    lane: int  # 0-based lane within its key mode; 0 for BGM objects
    length: Fraction = Fraction(0)  # hold duration in measures; 0 = tap note

    @property
    def absolute(self) -> Fraction:
        """Position measured in whole measures from the song start."""
        return self.measure + self.pos

    @property
    def is_long(self) -> bool:
        """True for a long (hold) note — one with a non-zero duration."""
        return self.length > 0

    @property
    def end_absolute(self) -> Fraction:
        """Absolute position of the note's end (== ``absolute`` for taps)."""
        return self.absolute + self.length


# --------------------------------------------------------------------------- #
# Tempo ramp
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BpmRamp:
    """A gradual tempo change: the BPM slides linearly from ``start_bpm`` at
    absolute position ``start`` to ``end_bpm`` at ``end`` (e.g. measure 78 →
    80 ramping 126 → 252). Stored as one editable object, but expanded into
    many discrete #BPM points at export/timing time (BMS has no native ramp)."""

    start: Fraction
    end: Fraction
    start_bpm: float
    end_bpm: float

    def value_at(self, pos) -> float:
        """Interpolated BPM at absolute position ``pos`` (clamped to the span)."""
        span = self.end - self.start
        if span <= 0:
            return self.end_bpm
        t = float((Fraction(pos) - self.start) / span)
        t = min(1.0, max(0.0, t))
        return self.start_bpm + (self.end_bpm - self.start_bpm) * t

    def points(self) -> Dict[Fraction, float]:
        """The discrete BPM breakpoints approximating this ramp: one every
        :data:`RAMP_STEP`, plus the exact endpoint carrying ``end_bpm``."""
        pts: Dict[Fraction, float] = {}
        if self.end <= self.start:
            pts[self.start] = self.end_bpm
            return pts
        pos = self.start
        while pos < self.end:
            pts[pos] = round(self.value_at(pos), 3)
            pos += RAMP_STEP
        pts[self.end] = round(self.end_bpm, 3)
        return pts


# --------------------------------------------------------------------------- #
# Project
# --------------------------------------------------------------------------- #

@dataclass
class Project:
    """One song: shared metadata + three key-mode charts + BGM objects."""

    title: str = ""
    artist: str = ""
    genre: str = ""
    bpm: float = 120.0           # base BPM, in effect from the song start
    level: int = 1               # play level / difficulty
    stagefile: str = ""          # #STAGEFILE image filename (splash / cover)
    banner: str = ""             # #BANNER image filename
    backbmp: str = ""            # #BACKBMP background image filename
    bgm_file: str = ""            # audio filename, e.g. "song.ogg" (portable)
    bgm_path: str = ""            # full audio path, to auto-reconnect on open
    measures: int = 16           # number of measures in the timeline
    # Key-mode charts are LISTS, not sets: two notes can legitimately overlap
    # (they're flagged as a conflict, never silently merged), so an edit must
    # never make one absorb another. BGM stays a set (a repeated trigger at the
    # same time is genuinely the same object).
    charts: Dict[int, List[Note]] = field(
        default_factory=lambda: {k: [] for k in ALL_MODES}
    )
    bgm: Set[Note] = field(default_factory=set)  # BGM objects (lane 0)
    # Mid-song tempo changes: absolute chart position (measures) -> BPM. The
    # base ``bpm`` applies before the first change.
    bpm_changes: Dict[Fraction, float] = field(default_factory=dict)
    # Gradual tempo ramps (e.g. an accelerando from measure 78 to 80). Kept as
    # editable objects; expanded into discrete points for timing/export.
    bpm_ramps: List[BpmRamp] = field(default_factory=list)
    # Editor/session settings (selected key mode, grid, zoom, speed, volume);
    # saved in .slbms so the workspace comes back as you left it.
    editor: Dict = field(default_factory=dict)

    # -- tempo -------------------------------------------------------------- #

    def effective_bpm_changes(self) -> Dict[Fraction, float]:
        """All tempo breakpoints the timing/exporter should see: the ramps
        expanded into discrete points, then explicit ``bpm_changes`` layered on
        top (an explicit point at the same position wins)."""
        merged: Dict[Fraction, float] = {}
        for ramp in self.bpm_ramps:
            merged.update(ramp.points())
        merged.update(self.bpm_changes)
        return merged

    def bpm_at(self, pos) -> float:
        """The BPM in effect at absolute chart position ``pos``."""
        bpm = self.bpm
        best: Optional[Fraction] = None
        for p, val in self.effective_bpm_changes().items():
            if p <= pos and (best is None or p > best):
                best, bpm = p, val
        return max(1.0, bpm)

    # -- undo/redo snapshots ------------------------------------------------ #

    def snapshot(self):
        """A cheap copy of all editable note/tempo/length state (Notes are
        immutable, so copying the lists/set just copies references)."""
        return (
            {km: list(s) for km, s in self.charts.items()},
            set(self.bgm),
            dict(self.bpm_changes),
            self.measures,
            list(self.bpm_ramps),
        )

    def restore(self, snap) -> None:
        charts, bgm, bpm_changes, measures, bpm_ramps = snap
        self.charts = {km: list(s) for km, s in charts.items()}
        self.bgm = set(bgm)
        self.bpm_changes = dict(bpm_changes)
        self.measures = measures
        self.bpm_ramps = list(bpm_ramps)

    # -- note editing ------------------------------------------------------- #

    def toggle_note(self, key_mode: int, measure: int, pos: Fraction, lane: int) -> bool:
        """Add the note if absent, remove it if present. Returns the new state
        (``True`` = note now exists)."""
        note = Note(measure, pos, lane)
        chart = self.charts[key_mode]
        if note in chart:
            chart.remove(note)
            return False
        chart.append(note)
        return True

    def add_object(self, mode, note: Note) -> None:
        """Place a note on a key-mode chart (``mode`` is the int key mode) or the
        BGM lane (``mode == 'bgm'``). Charts allow overlaps; BGM dedups."""
        if mode == "bgm":
            self.bgm.add(note)
        else:
            self.charts[mode].append(note)

    def remove_object(self, mode, note: Note) -> None:
        """Remove one instance of ``note`` from the given lane, if present."""
        if mode == "bgm":
            self.bgm.discard(note)
        else:
            try:
                self.charts[mode].remove(note)
            except ValueError:
                pass

    def toggle_bgm(self, measure: int, pos: Fraction) -> bool:
        note = Note(measure, pos, 0)
        if note in self.bgm:
            self.bgm.discard(note)
            return False
        self.bgm.add(note)
        return True

    def clear_key_mode(self, key_mode: int) -> None:
        self.charts[key_mode].clear()

    # -- convenience -------------------------------------------------------- #

    def note_count(self, key_mode: int) -> int:
        return len(self.charts[key_mode])
