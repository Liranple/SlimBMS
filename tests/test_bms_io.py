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
    # 4K never uses channels 13 or 19 (those are 6K-only keys); make sure the
    # 6K notes make_project placed there didn't leak into the 4K export.
    assert not any(line.startswith("#00013:") for line in text4.splitlines())
    assert not any(line.startswith("#00019:") for line in text4.splitlines())


def test_bms_import_lands_in_hinted_key_mode():
    # Our exports carry a key-mode hint (#SLIMBMS_KEYMODE / #NK), so re-importing
    # routes notes back into that same key mode's lanes (not the import group),
    # lining them up with the playfield. Positions and BGM/metadata are kept.
    p = make_project()
    for km in (4, 6):
        text = bms_io.export_bms(p, km)
        back = bms_io.parse_bms(text)
        assert not back.charts[IMPORT_MODE], "hinted import should skip the import group"
        orig = {(n.measure, n.pos, n.lane) for n in p.charts[km]}
        got = {(n.measure, n.pos, n.lane) for n in back.charts[km]}
        assert got == orig, f"key mode {km} note/lane mismatch"
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
        assert set(back.charts[km]) == set(p.charts[km])


def test_import_without_hint_maps_to_import_lanes():
    # An external chart with no key-mode hint loads into the catch-all import
    # group by channel, so no notes are dropped. Channels 16/18/19 -> lanes 5,6,7.
    lines = ["#TITLE X", "#00016:01", "#00018:01", "#00019:01"]
    p = bms_io.parse_bms("\n".join(lines))
    lanes = {n.lane for n in p.charts[IMPORT_MODE]}
    assert lanes == {5, 6, 7}, f"expected import lanes 5,6,7, got {lanes}"
    assert not p.charts[6], "nothing should land in the 6K chart on import"


def test_bpm_change_roundtrip():
    # Mid-song tempo changes survive export -> import (channel 08 + #BPMxx).
    p = make_project()
    p.bpm_changes[Fraction(1)] = 140.0
    p.bpm_changes[Fraction(9, 4)] = 95.5       # non-integer position + float BPM
    back = bms_io.parse_bms(bms_io.export_bms(p, 6))
    assert back.bpm == p.bpm
    assert back.bpm_changes.get(Fraction(1)) == 140.0
    assert abs(back.bpm_changes.get(Fraction(9, 4), 0) - 95.5) < 0.01


def test_bpm_change_slbms_roundtrip():
    p = make_project()
    p.bpm_changes[Fraction(3)] = 180.0
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "p.slbms")
        bms_io.save_project(p, path)
        back = bms_io.load_project(path)
    assert back.bpm_changes == p.bpm_changes


def test_stop_roundtrip_bms():
    # STOP sequences survive export -> import (channel 09 + #STOPxx).
    p = make_project()
    p.stops[Fraction(1)] = Fraction(2)          # 2-beat freeze
    p.stops[Fraction(5, 2)] = Fraction(1, 2)    # half-beat freeze at a fractional pos
    back = bms_io.parse_bms(bms_io.export_bms(p, 6))
    assert back.stops == p.stops


def test_stop_dedup_shares_one_definition():
    # Two stops of the same length must share a single #STOPxx definition.
    p = make_project()
    p.stops[Fraction(1)] = Fraction(1)
    p.stops[Fraction(3)] = Fraction(1)          # same duration -> same index
    text = bms_io.export_bms(p, 4)
    stop_defs = [l for l in text.splitlines() if l.upper().startswith("#STOP")]
    assert len(stop_defs) == 1
    assert bms_io.parse_bms(text).stops == p.stops


def test_stop_slbms_roundtrip():
    p = make_project()
    p.stops[Fraction(7, 2)] = Fraction(3, 4)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "p.slbms")
        bms_io.save_project(p, path)
        back = bms_io.load_project(path)
    assert back.stops == p.stops


def test_scroll_speed_roundtrip_bms():
    # SCROLL (SC) and SPEED (SP), incl. fractional and negative multipliers.
    p = make_project()
    p.scrolls[Fraction(1)] = Fraction(2)
    p.scrolls[Fraction(5, 2)] = Fraction(1, 2)  # fractional position
    p.scrolls[Fraction(3)] = Fraction(-1)       # reverse scroll (all < 4 measures)
    p.speeds[Fraction(2)] = Fraction(3, 2)
    back = bms_io.parse_bms(bms_io.export_bms(p, 6))
    assert back.scrolls == p.scrolls
    assert back.speeds == p.speeds


def test_scroll_dedup_shares_one_definition():
    p = make_project()
    p.scrolls[Fraction(1)] = Fraction(2)
    p.scrolls[Fraction(3)] = Fraction(2)        # same multiplier -> one #SCROLLxx
    text = bms_io.export_bms(p, 4)
    defs = [l for l in text.splitlines() if l.upper().startswith("#SCROLL")]
    assert len(defs) == 1
    assert bms_io.parse_bms(text).scrolls == p.scrolls


def test_scroll_speed_slbms_roundtrip():
    p = make_project()
    p.scrolls[Fraction(5, 2)] = Fraction(-3, 4)
    p.speeds[Fraction(1)] = Fraction(2)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "p.slbms")
        bms_io.save_project(p, path)
        back = bms_io.load_project(path)
    assert back.scrolls == p.scrolls
    assert back.speeds == p.speeds


def test_measure_scales_slbms_roundtrip():
    p = make_project()
    p.measure_scales[1] = Fraction(1, 2)
    p.measure_scales[3] = Fraction(3, 4)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "p.slbms")
        bms_io.save_project(p, path)
        back = bms_io.load_project(path)
    assert back.measure_scales == p.measure_scales


def test_measure_length_channel02_export_and_roundtrip():
    # A shortened measure is real (BMS channel 02): it emits #mmm02 and the note
    # inside it keeps its real offset through a .bms export -> re-import.
    p = Project(bpm=120, measures=8)
    p.measure_scales[2] = Fraction(1, 2)          # measure 2 is half length
    p.toggle_note(4, 2, Fraction(1, 4), 0)        # note at offset 1/4 (< 1/2)
    p.toggle_bgm(0, Fraction(0))
    text = bms_io.export_bms(p, 4)
    assert "#00202:0.5" in text                   # measure-length line
    # The note's data fraction is pos/len = (1/4)/(1/2) = 1/2 of the short measure.
    back = bms_io.parse_bms(text)
    assert back.measure_scales.get(2) == Fraction(1, 2)
    got = [n for n in back.charts[4] if n.measure == 2]
    assert len(got) == 1 and got[0].pos == Fraction(1, 4)   # real offset preserved


def test_import_honors_key_mode_command():
    # A uBMSC #6K command (no SLIMBMS hint) routes notes into the 6K lanes.
    lines = ["#TITLE X", "#6K", "#00011:01", "#00019:01"]  # 6K channels 11, 19
    p = bms_io.parse_bms("\n".join(lines))
    assert not p.charts[IMPORT_MODE]
    lanes = {n.lane for n in p.charts[6]}
    assert lanes == {0, 5}, f"expected 6K lanes 0 and 5, got {lanes}"


def test_key_mode_channel_mapping():
    # Locks the in-game-confirmed layout; scratch (16) unused.
    # 4K = keys 1,2,4,5   6K = keys 1,2,3,5,6,7.
    assert KEY_CHANNELS[4] == ["11", "12", "14", "15"]
    assert KEY_CHANNELS[6] == ["11", "12", "13", "15", "18", "19"]
    assert "16" not in sum((KEY_CHANNELS[k] for k in (4, 6)), [])


def test_keysoundless_bgm_and_notes_use_separate_slots():
    # Keysound-less: BGM object -> WAV01 (the song); chart notes -> silent 02
    # with no #WAV02 defined, so hitting a note makes no sound.
    p = Project(title="KS", bgm_file="song.ogg", measures=2)
    p.bgm.add(Note(0, Fraction(0), 0))
    p.charts[4].append(Note(0, Fraction(0), 0))   # tap on channel 11
    text = bms_io.export_bms(p, 4)
    assert "#WAV01 song.ogg" in text
    assert "#WAV02" not in text, "the note slot stays undefined (silent)"
    bgm_row = [l for l in text.splitlines() if l.startswith("#00001:")][0]
    assert bgm_row.split(":", 1)[1] == "01", "BGM object references the song (01)"
    note_row = [l for l in text.splitlines() if l.startswith("#00011:")][0]
    assert note_row.split(":", 1)[1] == "02", "chart note references the silent slot (02)"


def test_long_note_exports_on_ln_channel():
    # A 4K long note in lane 0 (channel 11 -> LN channel 51) spanning half a
    # measure emits a head at its start and a tail at its end on channel 51.
    p = Project(title="LN", bpm=120, measures=4)
    p.charts[4].append(Note(0, Fraction(1, 4), 0, Fraction(1, 2)))  # head 1/4, tail 3/4
    text = bms_io.export_bms(p, 4)
    assert "#LNTYPE 1" in text
    ln = [line for line in text.splitlines() if line.startswith("#00051:")]
    assert ln, "long note should use LN channel 51"
    # Head at 1/4 and tail at 3/4 -> slots 1 and 3 of a length-4 data string.
    # Chart notes carry the silent marker 02 (keysound-less); BGM uses 01.
    data = ln[0].split(":", 1)[1]
    assert data == "00020002", f"unexpected LN data {data!r}"


def test_long_note_spanning_measures_pairs_back():
    # A long note crossing a measure boundary round-trips through export+import
    # (head/tail paired by time order) with its duration intact.
    p = Project(title="LN2", bpm=120, measures=8)
    p.charts[6].append(Note(1, Fraction(1, 2), 2, Fraction(3, 4)))  # ends in measure 2
    text = bms_io.export_bms(p, 6)
    back = bms_io.parse_bms(text)
    longs = [n for n in back.charts[6] if n.is_long]
    assert len(longs) == 1, "exactly one long note should be reconstructed"
    n = longs[0]
    assert n.absolute == Fraction(3, 2) and n.length == Fraction(3, 4)


def test_slbms_roundtrip_preserves_length():
    p = Project(title="Hold", bpm=130, measures=4)
    p.charts[6].append(Note(0, Fraction(0), 1, Fraction(1, 3)))  # long
    p.charts[6].append(Note(1, Fraction(1, 4), 2))              # tap
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "p.slbms")
        bms_io.save_project(p, path)
        back = bms_io.load_project(path)
    assert set(back.charts[6]) == set(p.charts[6]), "length must survive the JSON round-trip"


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


def test_image_headers_roundtrip():
    # STAGEFILE / BANNER / BACKBMP survive both the .bms and .slbms round-trips,
    # and are omitted from .bms output when empty.
    p = Project(title="T", stagefile="cover.png", banner="ban.jpg",
                backbmp="bg.png")
    txt = bms_io.export_bms(p, 4)
    assert "#STAGEFILE cover.png" in txt
    assert "#BANNER ban.jpg" in txt
    assert "#BACKBMP bg.png" in txt
    back = bms_io.parse_bms(txt)
    assert (back.stagefile, back.banner, back.backbmp) == (
        "cover.png", "ban.jpg", "bg.png")

    d = bms_io.project_to_dict(p)
    back2 = bms_io.project_from_dict(d)
    assert (back2.stagefile, back2.banner, back2.backbmp) == (
        "cover.png", "ban.jpg", "bg.png")

    empty = bms_io.export_bms(Project(), 4)
    assert "STAGEFILE" not in empty
    assert "BANNER" not in empty
    assert "BACKBMP" not in empty


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
