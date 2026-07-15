"""Unit tests for the data model and BMS/project I/O (no GUI needed)."""

import os
import sys
import tempfile
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slimbms.model import IMPORT_MODE, KEY_CHANNELS, Note, Project  # noqa: E402
from slimbms import bms_io  # noqa: E402


def make_project() -> Project:
    p = Project(title="Test Song", artist="Me", genre="Techno", bpm=145.0, measures=4)
    p.bgm_file = "song.ogg"
    p.toggle_bgm(0, Fraction(0, 1))               # BGM starts at measure 0 beat 0
    # 4K chart
    p.toggle_note(4, 0, Fraction(0, 1), 0)
    p.toggle_note(4, 0, Fraction(1, 4), 1)
    p.toggle_note(4, 1, Fraction(3, 8), 3)
    # 6K chart
    p.toggle_note(6, 0, Fraction(0, 1), 5)
    p.toggle_note(6, 3, Fraction(5, 16), 0)
    return p


def test_toggle_is_idempotent_pair():
    p = Project()
    assert p.toggle_note(4, 0, Fraction(0), 0) is True
    assert p.note_count(4) == 1
    assert p.toggle_note(4, 0, Fraction(0), 0) is False
    assert p.note_count(4) == 0


def test_measure_data_reduction():
    # A single note at beat 0 -> length 1
    notes = [Note(0, Fraction(0, 1), 0)]
    assert bms_io._measure_data(notes) == "01"
    # Note at 1/2 -> "0001"
    notes = [Note(0, Fraction(1, 2), 0)]
    assert bms_io._measure_data(notes) == "0001"
    # Two notes at 0 and 1/2 -> both slots filled -> "0101"
    notes = [Note(0, Fraction(0), 0), Note(0, Fraction(1, 2), 0)]
    assert bms_io._measure_data(notes) == "0101"


def test_bms_export_contains_header_and_body():
    p = make_project()
    text = bms_io.export_bms(p, 6)
    assert "#TITLE Test Song" in text
    assert "#BPM 145" in text
    assert "#WAV01 song.ogg" in text
    assert "#SLIMBMS_KEYMODE 6" in text
    # uBMSC key-mode command so the game reads it as 6 keys, not "7+1".
    assert any(line.strip() == "#6K" for line in text.splitlines())
    assert "#4K" not in text
    # BGM object on channel 01, measure 000
    assert "#00001:" in text
    # 6K lane 5 maps to key 7 (channel 19); make_project put a note there.
    assert KEY_CHANNELS[6][5] == "19"
    assert any(line.startswith("#00019:") for line in text.splitlines())


def test_export_only_selected_key_mode():
    p = make_project()
    text4 = bms_io.export_bms(p, 4)
    # 4K never uses channels 11 or 19 (those are 6K's outer keys); make sure the
    # 6K notes make_project placed there didn't leak into the 4K export.
    assert not any(line.startswith("#00011:") for line in text4.splitlines())
    assert not any(line.startswith("#00019:") for line in text4.splitlines())


def test_bms_import_lands_in_import_lane():
    # Exporting a key mode then importing routes every note into the dedicated
    # import lane group. Timing positions and BGM/metadata must be preserved.
    p = make_project()
    for km in (4, 6):
        text = bms_io.export_bms(p, km)
        back = bms_io.parse_bms(text)
        assert not back.charts[km], "notes should not land back in the key-mode chart"
        orig_positions = {(n.measure, n.pos) for n in p.charts[km]}
        import_positions = {(n.measure, n.pos) for n in back.charts[IMPORT_MODE]}
        assert import_positions == orig_positions, f"key mode {km} timing mismatch"
        assert back.bgm == p.bgm
        assert back.title == p.title
        assert back.bpm == p.bpm
        assert back.bgm_file == p.bgm_file


def test_project_json_roundtrip():
    p = make_project()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "proj.slbms")
        bms_io.save_project(p, path)
        back = bms_io.load_project(path)
    assert back.title == p.title
    assert back.bpm == p.bpm
    assert back.measures == p.measures
    assert back.bgm == p.bgm
    for km in (4, 6):
        assert back.charts[km] == p.charts[km]


def test_import_channels_map_to_import_lanes():
    # Channels 16/18/19 (which don't fit the 6-lane 6K group) must still load
    # into the import lane instead of being dropped.
    lines = ["#TITLE X", "#00016:01", "#00018:01", "#00019:01"]
    p = bms_io.parse_bms("\n".join(lines))
    lanes = {n.lane for n in p.charts[IMPORT_MODE]}
    assert lanes == {5, 6, 7}, f"expected import lanes 5,6,7, got {lanes}"
    assert not p.charts[6], "nothing should land in the 6K chart on import"


def test_key_mode_channel_mapping():
    # Locks the uBMSC-matching layout: both hands split around the centre key,
    # scratch (16) unused. 4K=keys2,3,5,6  6K=keys1,2,3,5,6,7.
    assert KEY_CHANNELS[4] == ["12", "13", "15", "18"]
    assert KEY_CHANNELS[6] == ["11", "12", "13", "15", "18", "19"]
    assert "16" not in sum((KEY_CHANNELS[k] for k in (4, 6)), [])


def test_long_note_exports_on_ln_channel():
    # A 4K long note in lane 0 (channel 12 -> LN channel 52) spanning half a
    # measure emits a head at its start and a tail at its end on channel 52.
    p = Project(title="LN", bpm=120, measures=4)
    p.charts[4].add(Note(0, Fraction(1, 4), 0, Fraction(1, 2)))  # head 1/4, tail 3/4
    text = bms_io.export_bms(p, 4)
    assert "#LNTYPE 1" in text
    ln = [line for line in text.splitlines() if line.startswith("#00052:")]
    assert ln, "long note should use LN channel 52"
    # Head at 1/4 and tail at 3/4 -> slots 1 and 3 of a length-4 data string.
    data = ln[0].split(":", 1)[1]
    assert data == "00010001", f"unexpected LN data {data!r}"


def test_long_note_spanning_measures_pairs_back():
    # A long note crossing a measure boundary round-trips through export+import
    # (head/tail paired by time order) with its duration intact.
    p = Project(title="LN2", bpm=120, measures=8)
    p.charts[6].add(Note(1, Fraction(1, 2), 2, Fraction(3, 4)))  # ends in measure 2
    text = bms_io.export_bms(p, 6)
    back = bms_io.parse_bms(text)
    longs = [n for n in back.charts[IMPORT_MODE] if n.is_long]
    assert len(longs) == 1, "exactly one long note should be reconstructed"
    n = longs[0]
    assert n.absolute == Fraction(3, 2) and n.length == Fraction(3, 4)


def test_slbms_roundtrip_preserves_length():
    p = Project(title="Hold", bpm=130, measures=4)
    p.charts[6].add(Note(0, Fraction(0), 1, Fraction(1, 3)))  # long
    p.charts[6].add(Note(1, Fraction(1, 4), 2))              # tap
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "p.slbms")
        bms_io.save_project(p, path)
        back = bms_io.load_project(path)
    assert back.charts[6] == p.charts[6], "length must survive the JSON round-trip"


def test_slbms_v1_without_length_still_loads():
    # Old files store 4-field note rows (no length) — they must load as taps.
    data = {
        "version": 1, "title": "Old", "bpm": 120, "measures": 4,
        "bgm": [[0, 0, 1, 0]],
        "charts": {"4": [[0, 1, 4, 2]]},
    }
    p = bms_io.project_from_dict(data)
    (n,) = p.charts[4]
    assert n.length == Fraction(0) and n.lane == 2


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
