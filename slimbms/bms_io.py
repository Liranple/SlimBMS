"""Reading and writing files.

Two on-disk formats are supported:

* ``.bms`` — a standard BMS chart for a *single* key mode. This is the
  deliverable consumed by the game. Exporting asks which key mode to write.
* ``.slbms`` — a native JSON project holding *all three* key modes plus shared
  metadata, so an editing session round-trips without losing the other charts.
"""

from __future__ import annotations

import json
from fractions import Fraction
from math import gcd
from typing import Dict, List, Optional

from .model import (
    ALL_MODES,
    BGM_CHANNEL,
    IMPORT_MODE,
    KEY_CHANNELS,
    KEY_MODES,
    OBJ_VALUE,
    BGM_WAV_INDEX,
    Note,
    Project,
)

# --------------------------------------------------------------------------- #
# BMS export (one key mode -> .bms text)
# --------------------------------------------------------------------------- #

def _format_bpm(bpm: float) -> str:
    # Write an integer without a trailing ".0", otherwise keep the decimals.
    if float(bpm).is_integer():
        return str(int(bpm))
    return repr(float(bpm))


def _measure_data(objects: List[Note]) -> str:
    """Build the ``ZZ...`` data string for one channel within one measure.

    All objects use the same presence marker; the slot count is the least common
    multiple of the positions' denominators, then reduced to the shortest form.
    """
    if not objects:
        return ""
    length = 1
    for n in objects:
        length = length * n.pos.denominator // gcd(length, n.pos.denominator)
    slots = ["00"] * length
    for n in objects:
        idx = n.pos.numerator * (length // n.pos.denominator)
        slots[idx] = OBJ_VALUE
    # Reduce: if every filled index shares a factor with the length, shrink it.
    g = length
    for i, v in enumerate(slots):
        if v != "00":
            g = gcd(g, i)
    if g > 1:
        slots = slots[::g]
    return "".join(slots)


def export_bms(project: Project, key_mode: int) -> str:
    """Return the ``.bms`` text for ``key_mode`` (BGM + that chart's notes)."""
    if key_mode not in KEY_MODES:
        raise ValueError(f"unknown key mode: {key_mode}")

    lines: List[str] = []
    lines.append("*---------------------- HEADER FIELD")
    lines.append("#PLAYER 1")
    lines.append(f"#GENRE {project.genre}")
    lines.append(f"#TITLE {project.title}")
    lines.append(f"#ARTIST {project.artist}")
    lines.append(f"#BPM {_format_bpm(project.bpm)}")
    lines.append(f"#PLAYLEVEL {int(project.level)}")
    lines.append("#RANK 3")
    lines.append(f"#SLIMBMS_KEYMODE {key_mode}")  # hint for lossless re-import
    lines.append("")
    if project.bgm_file:
        lines.append(f"#WAV{BGM_WAV_INDEX} {project.bgm_file}")
    lines.append("")
    lines.append("*---------------------- MAIN DATA FIELD")

    channels = KEY_CHANNELS[key_mode]

    for measure in range(project.measures):
        rows: List[str] = []

        # BGM objects on channel 01.
        bgm_here = [n for n in project.bgm if n.measure == measure]
        data = _measure_data(bgm_here)
        if data:
            rows.append(f"#{measure:03d}{BGM_CHANNEL}:{data}")

        # Key objects grouped by lane -> channel.
        chart = project.charts[key_mode]
        for lane, channel in enumerate(channels):
            objs = [n for n in chart if n.measure == measure and n.lane == lane]
            data = _measure_data(objs)
            if data:
                rows.append(f"#{measure:03d}{channel}:{data}")

        lines.extend(rows)

    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# BMS import (.bms text -> Project with one key mode filled)
# --------------------------------------------------------------------------- #

def _parse_objects(data: str) -> List[Fraction]:
    """Return the positions (in [0,1)) of non-empty object slots in a data
    string of concatenated 2-char values."""
    pairs = [data[i:i + 2] for i in range(0, len(data) - 1, 2)]
    n = len(pairs)
    out: List[Fraction] = []
    for i, val in enumerate(pairs):
        if val != "00":
            out.append(Fraction(i, n))
    return out


def parse_bms(text: str) -> Project:
    """Parse a ``.bms`` chart into a :class:`Project`.

    All key objects are loaded into the dedicated import lane group (A1~A8) so
    nothing is lost regardless of how many channels the source uses; the BGM
    object and metadata are read normally.
    """
    project = Project()
    # Channel -> import lane index (A1~A8).
    import_lane = {ch: lane for lane, ch in enumerate(KEY_CHANNELS[IMPORT_MODE])}

    # measure -> channel -> list of positions
    body: Dict[int, Dict[str, List[Fraction]]] = {}
    max_measure = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("#TITLE "):
            project.title = line[7:].strip()
        elif upper.startswith("#ARTIST "):
            project.artist = line[8:].strip()
        elif upper.startswith("#GENRE "):
            project.genre = line[7:].strip()
        elif upper.startswith("#BPM "):
            try:
                project.bpm = float(line[5:].strip())
            except ValueError:
                pass
        elif upper.startswith(f"#WAV{BGM_WAV_INDEX}"):
            project.bgm_file = line[len("#WAV") + len(BGM_WAV_INDEX):].strip()
        elif len(line) >= 7 and line[6] == ":" and line[1:6].isalnum():
            # Body line: #XXXYY:data
            try:
                measure = int(line[1:4])
            except ValueError:
                continue
            channel = line[4:6]
            data = line[7:].strip()
            positions = _parse_objects(data)
            if not positions:
                continue
            body.setdefault(measure, {}).setdefault(channel, []).extend(positions)
            max_measure = max(max_measure, measure)

    project.measures = max(16, max_measure + 1)
    for measure, chans in body.items():
        for channel, positions in chans.items():
            if channel == BGM_CHANNEL:
                for pos in positions:
                    project.bgm.add(Note(measure, pos, 0))
            elif channel in import_lane:
                lane = import_lane[channel]
                for pos in positions:
                    project.charts[IMPORT_MODE].add(Note(measure, pos, lane))
    return project


# --------------------------------------------------------------------------- #
# Native project format (.slbms JSON)
# --------------------------------------------------------------------------- #

def project_to_dict(project: Project) -> dict:
    def notes(objs) -> List[list]:
        return sorted(
            [[n.measure, n.pos.numerator, n.pos.denominator, n.lane] for n in objs]
        )

    return {
        "format": "slimbms",
        "version": 1,
        "title": project.title,
        "artist": project.artist,
        "genre": project.genre,
        "bpm": project.bpm,
        "level": project.level,
        "bgm_file": project.bgm_file,
        "measures": project.measures,
        "bgm": notes(project.bgm),
        "charts": {str(km): notes(project.charts[km]) for km in ALL_MODES},
    }


def project_from_dict(data: dict) -> Project:
    project = Project(
        title=data.get("title", ""),
        artist=data.get("artist", ""),
        genre=data.get("genre", ""),
        bpm=float(data.get("bpm", 120.0)),
        level=int(data.get("level", 1)),
        bgm_file=data.get("bgm_file", ""),
        measures=int(data.get("measures", 16)),
    )
    for m, num, den, lane in data.get("bgm", []):
        project.bgm.add(Note(m, Fraction(num, den), lane))
    for km_str, objs in data.get("charts", {}).items():
        km = int(km_str)
        if km in ALL_MODES:
            for m, num, den, lane in objs:
                project.charts[km].add(Note(m, Fraction(num, den), lane))
    return project


def save_project(project: Project, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(project_to_dict(project), fh, ensure_ascii=False, indent=2)


def load_project(path: str) -> Project:
    with open(path, "r", encoding="utf-8") as fh:
        return project_from_dict(json.load(fh))
