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
    # Per-measure *display* length: a measure -> visible fraction in (0, 1].
    # Purely an editor convenience (collapse the empty tail of a measure so it
    # shows fewer grid cells and takes less height). It does NOT change note
    # timing, audio or the exported .bms — a note stays at absolute measure+pos.
    measure_scales: Dict[int, Fraction] = field(default_factory=dict)
    # Editor/session settings (selected key mode, grid, zoom, speed, volume);
    # saved in .slbms so the workspace comes back as you left it.
    editor: Dict = field(default_factory=dict)

    # -- tempo -------------------------------------------------------------- #

    def bpm_at(self, pos) -> float:
        """The BPM in effect at absolute chart position ``pos``."""
        bpm = self.bpm
        best: Optional[Fraction] = None
        for p, val in self.bpm_changes.items():
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
            dict(self.measure_scales),
        )

    def restore(self, snap) -> None:
        charts, bgm, bpm_changes, measures, measure_scales = snap
        self.charts = {km: list(s) for km, s in charts.items()}
        self.bgm = set(bgm)
        self.bpm_changes = dict(bpm_changes)
        self.measures = measures
        self.measure_scales = dict(measure_scales)

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
