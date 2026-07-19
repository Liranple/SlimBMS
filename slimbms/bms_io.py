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
    NOTE_VALUE,
    OBJ_VALUE,
    BGM_WAV_INDEX,
    Note,
    Project,
)

# --------------------------------------------------------------------------- #
# BMS export (one key mode -> .bms text)
# --------------------------------------------------------------------------- #

BPM_CH_INT = "03"    # inline integer BPM (2-hex-digit value)
BPM_CH_EXT = "08"    # extended BPM: value indexes a #BPMxx definition
MEASURE_LEN_CH = "02"   # measure length (a decimal multiplier of a full measure)
STOP_CH = "09"       # STOP sequence: value indexes a #STOPxx definition
# #STOPxx values are in 1/192 of a 4/4 measure; one beat (1/4 measure) = 48 of
# those units. Stops are stored in beats, so beats <-> value is a factor of 48.
STOP_UNITS_PER_BEAT = 48
SCROLL_CH = "SC"     # scroll-velocity multiplier (step), indexes #SCROLLxx
SPEED_CH = "SP"      # scroll-velocity multiplier (interpolated), indexes #SPEEDxx

_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _format_length(length: Fraction) -> str:
    """A short decimal string for a measure-length multiplier (channel 02)."""
    s = ("%.10f" % float(length)).rstrip("0").rstrip(".")
    return s or "0"


def _b36(n: int) -> str:
    """A 2-character base-36 index (01..ZZ) for BMS object values."""
    return _B36[(n // 36) % 36] + _B36[n % 36]


def _from_b36(s: str) -> int:
    return int(s, 36)


def _ln_channel(channel: str) -> str:
    """The long-note channel paired with a normal 1x object channel (11 -> 51)."""
    return "5" + channel[1:]


def _format_bpm(bpm: float) -> str:
    # Write an integer without a trailing ".0", otherwise keep the decimals.
    if float(bpm).is_integer():
        return str(int(bpm))
    return repr(float(bpm))


# How finely a 선형 변속 ramp is chopped into SCROLL steps on export: one step
# per 1/16 of a measure. Fine enough to read as a smooth ramp, moderate enough
# not to stress the player with an extreme number of scroll changes.
_RAMP_STEP = Fraction(1, 16)


def _ramp_to_scroll_steps(ramps):
    """Turn 선형 변속 ramps into a ``{position: multiplier}`` staircase of SCROLL
    steps that approximates each smooth ramp (the game reads #SCROLL, not
    #SPEED). Each step holds its value until the next, so the last step lands the
    end value exactly, matching the ramp's end."""
    out = {}
    for sp, ep, sv, ev in ramps:
        span = ep - sp
        if span <= 0:
            out[sp] = sv
            continue
        p = sp
        while p < ep:
            out[p] = sv + (ev - sv) * ((p - sp) / span)
            p += _RAMP_STEP
        out[ep] = ev                    # exact end value, held afterwards
    return out


def _format_decimal(x) -> str:
    """A tidy decimal string for a SCROLL/SPEED multiplier (may be negative or
    fractional): integers stay bare, otherwise a short float."""
    f = float(x)
    if f.is_integer():
        return str(int(f))
    return repr(round(f, 6))


def _measure_data(objects: List[Note], value: str = OBJ_VALUE) -> str:
    """Build the ``ZZ...`` data string for one channel within one measure.

    Every object uses ``value`` as its marker (BGM points at the song, chart
    notes at a silent slot); the slot count is the least common multiple of the
    positions' denominators, then reduced to the shortest form.
    """
    if not objects:
        return ""
    length = 1
    for n in objects:
        length = length * n.pos.denominator // gcd(length, n.pos.denominator)
    slots = ["00"] * length
    for n in objects:
        idx = n.pos.numerator * (length // n.pos.denominator)
        # An object at pos >= the measure's length falls past a shortened
        # measure's end (e.g. a long-note tail or BPM change the UI let slip
        # through). Clamp it to the last slot rather than crash the whole export.
        idx = min(idx, length - 1)
        slots[idx] = value
    # Reduce: if every filled index shares a factor with the length, shrink it.
    g = length
    for i, v in enumerate(slots):
        if v != "00":
            g = gcd(g, i)
    if g > 1:
        slots = slots[::g]
    return "".join(slots)


def _measure_data_valued(items) -> str:
    """Like :func:`_measure_data` but each object carries its own 2-char value.

    ``items`` is a list of ``(Fraction pos, str value)``."""
    if not items:
        return ""
    length = 1
    for pos, _ in items:
        length = length * pos.denominator // gcd(length, pos.denominator)
    slots = ["00"] * length
    for pos, val in items:
        idx = min(pos.numerator * (length // pos.denominator), length - 1)
        slots[idx] = val
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
    if project.stagefile:
        lines.append(f"#STAGEFILE {project.stagefile}")
    if project.banner:
        lines.append(f"#BANNER {project.banner}")
    if project.backbmp:
        lines.append(f"#BACKBMP {project.backbmp}")
    lines.append("#RANK 3")
    lines.append("#LNTYPE 1")  # long notes use paired head/tail on the 5x channel
    lines.append(f"#SLIMBMS_KEYMODE {key_mode}")  # hint for lossless re-import

    # Mid-song tempo changes: define #BPMxx values and place them on channel 08.
    bpm_changes = sorted(project.bpm_changes.items())
    bpm_index = {}   # absolute position -> 2-char index
    for i, (pos, bpm) in enumerate(bpm_changes, start=1):
        idx = _b36(i)
        bpm_index[pos] = idx
        lines.append(f"#BPM{idx} {_format_bpm(bpm)}")

    # STOP sequences: define #STOPxx values (deduplicated by duration) and place
    # them on channel 09. A stop's beats become 1/192-measure units (beats * 48).
    stop_val_index = {}   # duration (beats, Fraction) -> 2-char index
    stop_index = {}       # absolute position -> 2-char index
    for pos, beats in sorted(project.stops.items()):
        if beats <= 0:
            continue
        if beats not in stop_val_index:
            idx = _b36(len(stop_val_index) + 1)
            stop_val_index[beats] = idx
            lines.append(f"#STOP{idx} {int(round(float(beats) * STOP_UNITS_PER_BEAT))}")
        stop_index[pos] = stop_val_index[beats]

    # SCROLL / SPEED velocity multipliers (beatoraja/Qwilight): #SCROLLxx / #SPEEDxx
    # definitions (deduplicated by value) placed on channels SC / SP.
    def _valued_defs(source, keyword):
        val_index = {}   # multiplier -> index
        pos_index = {}   # absolute position -> index
        for pos, val in sorted(source.items()):
            if val not in val_index:
                idx = _b36(len(val_index) + 1)
                val_index[val] = idx
                lines.append(f"#{keyword}{idx} {_format_decimal(val)}")
            pos_index[pos] = val_index[val]
        return pos_index

    # 선형 변속(SPEED) is exported as a staircase of fine SCROLL steps rather
    # than #SPEED markers: the target engine (Qwilight) reads #SCROLL but not
    # #SPEED, so approximating each smooth ramp with many small SCROLL steps is
    # what actually produces the gradual scroll change in-game. Explicit 순간
    # 변속 markers win at any shared position.
    combined_scrolls = dict(project.scrolls)
    for pos, val in _ramp_to_scroll_steps(project.speed_ramps()).items():
        combined_scrolls.setdefault(pos, val)
    scroll_index = _valued_defs(combined_scrolls, "SCROLL")
    lines.append("")
    if project.bgm_file:
        lines.append(f"#WAV{BGM_WAV_INDEX} {project.bgm_file}")
    lines.append("")
    lines.append("*---------------------- MAIN DATA FIELD")

    channels = KEY_CHANNELS[key_mode]

    for measure in range(project.measures):
        rows: List[str] = []

        # Measure length (channel 02) when this measure is shortened. Object
        # positions below are given as a fraction of the shortened measure, so a
        # note at offset ``pos`` becomes ``pos / mlen``.
        mlen = project.measure_length(measure)
        if mlen != 1:
            rows.append(f"#{measure:03d}{MEASURE_LEN_CH}:{_format_length(mlen)}")

        def frac(pos: Fraction) -> Fraction:
            return pos / mlen if mlen != 1 else pos

        # Tempo changes on channel 08 (extended BPM).
        bpm_here = [(frac(pos - measure), idx) for pos, idx in bpm_index.items()
                    if int(pos) == measure]
        data = _measure_data_valued(bpm_here)
        if data:
            rows.append(f"#{measure:03d}{BPM_CH_EXT}:{data}")

        # STOP sequences on channel 09.
        stop_here = [(frac(pos - measure), idx) for pos, idx in stop_index.items()
                     if int(pos) == measure]
        data = _measure_data_valued(stop_here)
        if data:
            rows.append(f"#{measure:03d}{STOP_CH}:{data}")

        # Note-speed multipliers on channel SC (선형 ramps are folded into these
        # as fine steps above; #SPEED is not emitted — the game ignores it).
        scroll_here = [(frac(pos - measure), idx) for pos, idx in scroll_index.items()
                       if int(pos) == measure]
        data = _measure_data_valued(scroll_here)
        if data:
            rows.append(f"#{measure:03d}{SCROLL_CH}:{data}")

        # BGM objects on channel 01.
        bgm_here = [Note(measure, frac(n.pos), 0)
                    for n in project.bgm if n.measure == measure]
        data = _measure_data(bgm_here)
        if data:
            rows.append(f"#{measure:03d}{BGM_CHANNEL}:{data}")

        # Key objects grouped by lane -> channel. Tap notes go on the normal
        # channel (1x); long notes emit a head object at their start and a tail
        # object at their end, both on the paired LN channel (5x).
        chart = project.charts[key_mode]
        for lane, channel in enumerate(channels):
            lane_notes = [n for n in chart if n.lane == lane]
            taps = [Note(measure, frac(n.pos), lane)
                    for n in lane_notes if not n.is_long and n.measure == measure]
            data = _measure_data(taps, NOTE_VALUE)
            if data:
                rows.append(f"#{measure:03d}{channel}:{data}")

            endpoints: List[Note] = []
            for n in lane_notes:
                if not n.is_long:
                    continue
                if n.measure == measure:
                    endpoints.append(Note(measure, frac(n.pos), lane))
                end_abs = n.end_absolute
                end_measure = int(end_abs)
                if end_measure == measure:
                    endpoints.append(Note(measure, frac(end_abs - end_measure), lane))
            data = _measure_data(endpoints, NOTE_VALUE)
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


def _parse_length(data: str):
    """Parse a channel-02 measure-length value (a decimal like ``0.5``) into a
    tidy Fraction, or ``None`` if it isn't a positive number."""
    try:
        length = Fraction(data.strip()).limit_denominator(4096)
    except (ValueError, ZeroDivisionError):
        return None
    return length if length > 0 else None


def _parse_valued(data: str):
    """Like :func:`_parse_objects` but keeps each slot's 2-char value:
    a list of ``(Fraction pos, str value)``."""
    pairs = [data[i:i + 2] for i in range(0, len(data) - 1, 2)]
    n = len(pairs)
    return [(Fraction(i, n), v) for i, v in enumerate(pairs) if v != "00"]


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
    bpm_defs: Dict[str, float] = {}   # #BPMxx index -> value
    tempo_objs: List = []             # (measure, Fraction pos, channel, value)
    measure_lengths: Dict[int, Fraction] = {}   # measure -> length multiplier
    stop_defs: Dict[str, Fraction] = {}   # #STOPxx index -> duration in beats
    stop_objs: List = []                  # (measure, Fraction pos, value)
    scroll_defs: Dict[str, Fraction] = {}   # #SCROLLxx index -> multiplier
    speed_defs: Dict[str, Fraction] = {}    # #SPEEDxx index -> multiplier
    scroll_objs: List = []                  # (measure, Fraction pos, value)
    speed_objs: List = []                   # (measure, Fraction pos, value)

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
        elif upper.startswith("#BPM") and len(line) > 4 and line[4] != " ":
            # Indexed tempo definition: #BPMxx value
            idx = upper[4:6]
            try:
                bpm_defs[idx] = float(line.split(None, 1)[1])
            except (IndexError, ValueError):
                pass
        elif upper.startswith("#STOP") and len(line) > 5 and line[5] != " ":
            # Indexed stop definition: #STOPxx units (1/192 of a 4/4 measure).
            idx = upper[5:7]
            try:
                units = int(line.split(None, 1)[1])
                stop_defs[idx] = Fraction(units, STOP_UNITS_PER_BEAT)
            except (IndexError, ValueError):
                pass
        elif upper.startswith("#SCROLL") and len(line) > 7 and line[7] != " ":
            idx = upper[7:9]
            try:
                scroll_defs[idx] = Fraction(line.split(None, 1)[1]).limit_denominator(100000)
            except (IndexError, ValueError, ZeroDivisionError):
                pass
        elif upper.startswith("#SPEED") and len(line) > 6 and line[6] != " ":
            idx = upper[6:8]
            try:
                speed_defs[idx] = Fraction(line.split(None, 1)[1]).limit_denominator(100000)
            except (IndexError, ValueError, ZeroDivisionError):
                pass
        elif upper.startswith("#STAGEFILE "):
            project.stagefile = line[len("#STAGEFILE "):].strip()
        elif upper.startswith("#BANNER "):
            project.banner = line[len("#BANNER "):].strip()
        elif upper.startswith("#BACKBMP "):
            project.backbmp = line[len("#BACKBMP "):].strip()
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
            if channel == MEASURE_LEN_CH:
                length = _parse_length(data)
                if length is not None and length != 1:
                    measure_lengths[measure] = length
                max_measure = max(max_measure, measure)
                continue
            if channel in (BPM_CH_INT, BPM_CH_EXT):
                for pos, val in _parse_valued(data):
                    tempo_objs.append((measure, pos, channel, val))
                max_measure = max(max_measure, measure)
                continue
            if channel == STOP_CH:
                for pos, val in _parse_valued(data):
                    stop_objs.append((measure, pos, val))
                max_measure = max(max_measure, measure)
                continue
            if channel.upper() == SCROLL_CH:
                for pos, val in _parse_valued(data):
                    scroll_objs.append((measure, pos, val))
                max_measure = max(max_measure, measure)
                continue
            if channel.upper() == SPEED_CH:
                for pos, val in _parse_valued(data):
                    speed_objs.append((measure, pos, val))
                max_measure = max(max_measure, measure)
                continue
            positions = _parse_objects(data)
            if not positions:
                continue
            body.setdefault(measure, {}).setdefault(channel, []).extend(positions)
            max_measure = max(max_measure, measure)

    def mlen(m: int) -> Fraction:
        return measure_lengths.get(m, Fraction(1))

    # Resolve tempo changes; a change at the very start becomes the base BPM.
    # A within-measure fraction maps to the shortened measure: pos -> pos * len.
    for measure, pos, channel, val in tempo_objs:
        if channel == BPM_CH_INT:
            try:
                bpm = float(int(val, 16))
            except ValueError:
                continue
        else:
            bpm = bpm_defs.get(val.upper())
            if bpm is None:
                continue
        abs_pos = measure + pos * mlen(measure)
        if abs_pos <= 0:
            project.bpm = bpm
        else:
            project.bpm_changes[abs_pos] = bpm

    # Resolve STOP objects against their #STOPxx definitions.
    for measure, pos, val in stop_objs:
        beats = stop_defs.get(val.upper())
        if beats is None or beats <= 0:
            continue
        abs_pos = measure + pos * mlen(measure)
        if abs_pos > 0:
            project.stops[abs_pos] = beats

    # Resolve SCROLL / SPEED objects against their definitions.
    for objs, defs, target in ((scroll_objs, scroll_defs, project.scrolls),
                               (speed_objs, speed_defs, project.speeds)):
        for measure, pos, val in objs:
            mult = defs.get(val.upper())
            if mult is None:
                continue
            abs_pos = measure + pos * mlen(measure)
            if abs_pos >= 0:
                target[abs_pos] = mult

    project.measure_scales = dict(measure_lengths)
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
        L = mlen(measure)
        for channel, positions in chans.items():
            if channel == BGM_CHANNEL:
                for pos in positions:
                    project.bgm.add(Note(measure, pos * L, 0))
            elif channel in import_lane:
                lane = import_lane[channel]
                for pos in positions:
                    chart.append(Note(measure, pos * L, lane))
            elif channel in ln_lane:
                ln_points.setdefault(channel, []).extend(
                    measure + pos * L for pos in positions)

    # Pair consecutive LN endpoints into hold notes (head, tail, head, tail…).
    for channel, points in ln_points.items():
        lane = ln_lane[channel]
        points.sort()
        for i in range(0, len(points) - 1, 2):
            start, end = points[i], points[i + 1]
            m = int(start)
            chart.append(Note(m, start - m, lane, end - start))
        if len(points) % 2:  # dangling head with no tail -> a plain tap
            start = points[-1]
            m = int(start)
            chart.append(Note(m, start - m, lane))
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
        "version": 3,
        "title": project.title,
        "artist": project.artist,
        "genre": project.genre,
        "bpm": project.bpm,
        "level": project.level,
        "stagefile": project.stagefile,
        "banner": project.banner,
        "backbmp": project.backbmp,
        "bgm_file": project.bgm_file,
        "bgm_path": project.bgm_path,
        "editor": project.editor,
        "measures": project.measures,
        "bpm_changes": sorted([p.numerator, p.denominator, b]
                              for p, b in project.bpm_changes.items()),
        "measure_scales": sorted([m, s.numerator, s.denominator]
                                 for m, s in project.measure_scales.items()),
        "stops": sorted([p.numerator, p.denominator, b.numerator, b.denominator]
                        for p, b in project.stops.items()),
        "scrolls": sorted([p.numerator, p.denominator, v.numerator, v.denominator]
                          for p, v in project.scrolls.items()),
        "speeds": sorted([p.numerator, p.denominator, v.numerator, v.denominator]
                         for p, v in project.speeds.items()),
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
        stagefile=data.get("stagefile", ""),
        banner=data.get("banner", ""),
        backbmp=data.get("backbmp", ""),
        bgm_file=data.get("bgm_file", ""),
        bgm_path=data.get("bgm_path", ""),
        editor=data.get("editor", {}) or {},
        measures=int(data.get("measures", 16)),
    )
    def to_note(row) -> Note:
        m, num, den, lane = row[0], row[1], row[2], row[3]
        length = Fraction(row[4], row[5]) if len(row) >= 6 else Fraction(0)
        return Note(m, Fraction(num, den), lane, length)

    for row in data.get("bpm_changes", []):
        project.bpm_changes[Fraction(int(row[0]), int(row[1]))] = float(row[2])
    for row in data.get("measure_scales", []):
        project.measure_scales[int(row[0])] = Fraction(int(row[1]), int(row[2]))
    for row in data.get("stops", []):
        project.stops[Fraction(int(row[0]), int(row[1]))] = Fraction(int(row[2]), int(row[3]))
    for key, target in (("scrolls", project.scrolls), ("speeds", project.speeds)):
        for row in data.get(key, []):
            target[Fraction(int(row[0]), int(row[1]))] = Fraction(int(row[2]), int(row[3]))
    for row in data.get("bgm", []):
        project.bgm.add(to_note(row))
    for km_str, objs in data.get("charts", {}).items():
        km = int(km_str)
        if km in ALL_MODES:
            for row in objs:
                project.charts[km].append(to_note(row))
    return project


def save_project(project: Project, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(project_to_dict(project), fh, ensure_ascii=False, indent=2)


def load_project(path: str) -> Project:
    with open(path, "r", encoding="utf-8") as fh:
        return project_from_dict(json.load(fh))
