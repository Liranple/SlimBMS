"""Data model for SlimBMS.

A :class:`Project` holds one song's metadata plus independent charts
(one per key mode: 4K / 6K) that share the same BGM audio and timing.
Notes carry no keysounds — the sound of the song comes entirely from a single
BGM audio file placed on the BGM lane.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Set

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

KEY_MODES = (4, 6)             # editable / exportable key modes
IMPORT_MODE = 8                # dedicated "import" lane group (A1~A8)
ALL_MODES = (4, 6, IMPORT_MODE)   # everything the project stores
DISPLAY_MODES = (4, 6, IMPORT_MODE)  # left-to-right order in the editor

# Lane index -> BMS object channel, per mode (1P visible channels).
# Channels are the standard BMS 1P keys: 11-15 = keys 1-5, 18/19 = keys 6/7,
# and 16 = scratch (never used here). The modes match uBMSC's playfields, with
# both hands split around the centre key so the game reads them correctly:
#   4K = keys 2,3,5,6      6K = keys 1,2,3,5,6,7
# The IMPORT_MODE group carries all eight 1P channels (A1~A8 in uBMSC) so any
# loaded .bms lands there without losing notes.
KEY_CHANNELS: Dict[int, List[str]] = {
    4: ["12", "13", "15", "18"],
    6: ["11", "12", "13", "15", "18", "19"],
    IMPORT_MODE: ["11", "12", "13", "14", "15", "16", "18", "19"],
}

# Per-lane colour code for each mode: 'W' white, 'B' blue, 'G' grey (import).
# Matches real key colours (white = odd key, blue = even key) for the channels
# above, so lanes look like the keyboard they map to.
LANE_COLORS: Dict[int, str] = {
    4: "BWWB",       # keys 2,3,5,6
    6: "WBWWBW",     # keys 1,2,3,5,6,7
    IMPORT_MODE: "GGGGGGGG",
}

DISPLAY_LABELS: Dict[int, str] = {
    4: "4K", 6: "6K", IMPORT_MODE: "불러오기",
}

# Channel used for the background-music object (whole-song audio start timing).
BGM_CHANNEL = "01"

# All notes reference this single WAV index. For a keysound-less chart every
# object simply points at the one BGM audio; the value is a presence marker.
OBJ_VALUE = "01"
BGM_WAV_INDEX = "01"


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
    bpm: float = 120.0
    level: int = 1               # play level / difficulty
    bgm_file: str = ""            # audio filename, e.g. "song.ogg"
    measures: int = 16           # number of measures in the timeline
    charts: Dict[int, Set[Note]] = field(
        default_factory=lambda: {k: set() for k in ALL_MODES}
    )
    bgm: Set[Note] = field(default_factory=set)  # BGM objects (lane 0)

    # -- note editing ------------------------------------------------------- #

    def toggle_note(self, key_mode: int, measure: int, pos: Fraction, lane: int) -> bool:
        """Add the note if absent, remove it if present. Returns the new state
        (``True`` = note now exists)."""
        note = Note(measure, pos, lane)
        chart = self.charts[key_mode]
        if note in chart:
            chart.discard(note)
            return False
        chart.add(note)
        return True

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
