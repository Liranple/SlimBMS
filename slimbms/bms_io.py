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

def _ln_channel(channel: str) -> str:
    """The long-note channel paired with a normal 1x object channel (11 -> 51)."""
    return "5" + channel[1:]


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
    # uBMSC key-mode extension: pins the play mode (e.g. #6K) so the game reads
    # the chart as N keys instead of guessing "7+1 keys" from the channels used.
    lines.append(f"#{key_mode}K")
    lines.append(f"#GENRE {project.genre}")
    lines.append(f"#TITLE {project.title}")
    lines.append(f"#ARTIST {project.artist}")
    lines.append(f"#BPM {_format_bpm(project.bpm)}")
    lines.append(f"#PLAYLEVEL {int(project.level)}")
    lines.append("#RANK 3")
    lines.append("#LNTYPE 1")  # long notes use paired head/tail on the 5x channel
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

        # Key objects grouped by lane -> channel. Tap notes go on the normal
        # channel (1x); long notes emit a head object at their start and a tail
        # object at their end, both on the paired LN channel (5x).
        chart = project.charts[key_mode]
        for lane, channel in enumerate(channels):
            lane_notes = [n for n in chart if n.lane == lane]
            taps = [n for n in lane_notes if not n.is_long and n.measure == measure]
            data = _measure_data(taps)
            if data:
                rows.append(f"#{measure:03d}{channel}:{data}")

            endpoints: List[Note] = []
            for n in lane_notes:
                if not n.is_long:
                    continue
                if n.measure == measure:
                    endpoints.append(Note(measure, n.pos, lane))
                end_abs = n.end_absolute
                end_measure = int(end_abs)
                if end_measure == measure:
                    endpoints.append(Note(measure, end_abs - end_measure, lane))
            data = _measure_data(endpoints)
            if data:
                rows.append(f"#{measure:03d}{_ln_channel(channel)}:{data}")

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

    When the header names a key mode (our ``#SLIMBMS_KEYMODE`` hint, or a uBMSC
    ``#4K`` / ``#6K`` command) the notes are loaded straight into that mode's
    lanes so they line up with the 4K / 6K playfield. Otherwise everything falls
    back to the dedicated import group (A1~A8) so nothing is lost regardless of
    how many channels the source uses. BGM and metadata are read normally.
    """
    project = Project()

    # measure -> channel -> list of positions
    body: Dict[int, Dict[str, List[Fraction]]] = {}
    max_measure = 0
    key_hint: Optional[int] = None

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
        elif upper.startswith("#SLIMBMS_KEYMODE"):
            try:
                key_hint = int(upper.split()[1])
            except (IndexError, ValueError):
                pass
        elif upper in ("#4K", "#5K", "#6K", "#7K", "#8K", "#9K"):
            if key_hint is None:                 # SLIMBMS_KEYMODE wins if present
                key_hint = int(upper[1:-1])
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

    # Load into the hinted key mode's lanes when known, else the catch-all
    # import group. Channel -> lane index for tap (1x) and LN (5x) channels.
    target = key_hint if key_hint in KEY_CHANNELS else IMPORT_MODE
    channels = KEY_CHANNELS[target]
    import_lane = {ch: lane for lane, ch in enumerate(channels)}
    ln_lane = {_ln_channel(ch): lane for lane, ch in enumerate(channels)}
    chart = project.charts[target]

    # LN endpoints, gathered across all measures per channel so head/tail pairs
    # can be matched in time order.
    ln_points: Dict[str, List[Fraction]] = {}
    for measure, chans in body.items():
        for channel, positions in chans.items():
            if channel == BGM_CHANNEL:
                for pos in positions:
                    project.bgm.add(Note(measure, pos, 0))
            elif channel in import_lane:
                lane = import_lane[channel]
                for pos in positions:
                    chart.add(Note(measure, pos, lane))
            elif channel in ln_lane:
                ln_points.setdefault(channel, []).extend(measure + pos for pos in positions)

    # Pair consecutive LN endpoints into hold notes (head, tail, head, tail…).
    for channel, points in ln_points.items():
        lane = ln_lane[channel]
        points.sort()
        for i in range(0, len(points) - 1, 2):
            start, end = points[i], points[i + 1]
            m = int(start)
            chart.add(Note(m, start - m, lane, end - start))
        if len(points) % 2:  # dangling head with no tail -> a plain tap
            start = points[-1]
            m = int(start)
            chart.add(Note(m, start - m, lane))
    return project


# --------------------------------------------------------------------------- #
# Native project format (.slbms JSON)
# --------------------------------------------------------------------------- #

def project_to_dict(project: Project) -> dict:
    def notes(objs) -> List[list]:
        # [measure, pos_num, pos_den, lane, len_num, len_den]. The two length
        # fields are omitted for taps to keep files compact and are treated as 0
        # when absent (so v1 files without them still load).
        rows = []
        for n in objs:
            row = [n.measure, n.pos.numerator, n.pos.denominator, n.lane]
            if n.is_long:
                row += [n.length.numerator, n.length.denominator]
            rows.append(row)
        return sorted(rows)

    return {
        "format": "slimbms",
        "version": 2,
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
    def to_note(row) -> Note:
        m, num, den, lane = row[0], row[1], row[2], row[3]
        length = Fraction(row[4], row[5]) if len(row) >= 6 else Fraction(0)
        return Note(m, Fraction(num, den), lane, length)

    for row in data.get("bgm", []):
        project.bgm.add(to_note(row))
    for km_str, objs in data.get("charts", {}).items():
        km = int(km_str)
        if km in ALL_MODES:
            for row in objs:
                project.charts[km].add(to_note(row))
    return project


def save_project(project: Project, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(project_to_dict(project), fh, ensure_ascii=False, indent=2)


def load_project(path: str) -> Project:
    with open(path, "r", encoding="utf-8") as fh:
        return project_from_dict(json.load(fh))
