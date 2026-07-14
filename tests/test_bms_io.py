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
    # 5K chart
    p.toggle_note(5, 0, Fraction(1, 2), 4)
    p.toggle_note(5, 2, Fraction(1, 3), 2)
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
    text = bms_io.export_bms(p, 5)
    assert "#TITLE Test Song" in text
    assert "#BPM 145" in text
    assert "#WAV01 song.ogg" in text
    assert "#SLIMBMS_KEYMODE 5" in text
    # BGM object on channel 01, measure 000
    assert "#00001:" in text
    # A 5K note used channel 15 (lane 4)
    assert KEY_CHANNELS[5][4] == "15"
    assert any(line.startswith("#00015:") for line in text.splitlines())


def test_export_only_selected_key_mode():
    p = make_project()
    text4 = bms_io.export_bms(p, 4)
    # 4K has no lane 4 (would be channel 15); make sure 5K/6K notes didn't leak.
    # channel 15 only exists for 5K/6K, so it must be absent from the 4K export.
    assert not any(line.startswith("#00015:") for line in text4.splitlines())


def test_bms_import_lands_in_import_lane():
    # Exporting a key mode then importing routes every note into the dedicated
    # import lane group. Timing positions and BGM/metadata must be preserved.
    p = make_project()
    for km in (4, 5, 6):
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
    for km in (4, 5, 6):
        assert back.charts[km] == p.charts[km]


def test_import_channels_map_to_import_lanes():
    # Channels 16/18/19 (which don't fit the 6-lane 6K group) must still load
    # into the import lane instead of being dropped.
    lines = ["#TITLE X", "#00016:01", "#00018:01", "#00019:01"]
    p = bms_io.parse_bms("\n".join(lines))
    lanes = {n.lane for n in p.charts[IMPORT_MODE]}
    assert lanes == {5, 6, 7}, f"expected import lanes 5,6,7, got {lanes}"
    assert not p.charts[6], "nothing should land in the 6K chart on import"


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
