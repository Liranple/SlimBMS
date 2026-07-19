"""Offscreen smoke test: build the window, place notes via the canvas, export."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fractions import Fraction  # noqa: E402

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from slimbms.model import Note, Project  # noqa: E402
from slimbms.ui.main_window import MainWindow  # noqa: E402
from slimbms.ui import layout as L  # noqa: E402


def click(view, x, y, button=Qt.LeftButton):
    pos = QPointF(x, y)
    down = QMouseEvent(QMouseEvent.MouseButtonPress, pos, pos, button, button, Qt.NoModifier)
    view.mousePressEvent(down)
    up = QMouseEvent(QMouseEvent.MouseButtonRelease, pos, pos, button, Qt.NoButton, Qt.NoModifier)
    view.mouseReleaseEvent(up)


def press(view, key, autorep=False):
    ev = QKeyEvent(QKeyEvent.KeyPress, key, Qt.NoModifier, "", autorep)
    view.keyPressEvent(ev)


def release(view, key, autorep=False):
    ev = QKeyEvent(QKeyEvent.KeyRelease, key, Qt.NoModifier, "", autorep)
    view.keyReleaseEvent(ev)


def drag(view, x, y0, y1):
    """Left-press at y0, move to y1, release — the add-mode long-note gesture."""
    from PySide6.QtGui import QMouseEvent
    for etype, y in ((QMouseEvent.MouseButtonPress, y0),
                     (QMouseEvent.MouseMove, y1),
                     (QMouseEvent.MouseButtonRelease, y1)):
        pos = QPointF(x, y)
        btn = Qt.LeftButton if etype != QMouseEvent.MouseMove else Qt.NoButton
        ev = QMouseEvent(etype, pos, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        {QMouseEvent.MouseButtonPress: view.mousePressEvent,
         QMouseEvent.MouseMove: view.mouseMoveEvent,
         QMouseEvent.MouseButtonRelease: view.mouseReleaseEvent}[etype](ev)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    win = MainWindow(Project(title="Smoke", bpm=150, measures=16))
    view = win.view

    # Click in the middle of the 6K group's first lane, somewhere in the timeline.
    sixk_cols = [c for c in view.columns if c.kind == "key" and c.key_mode == 6]
    col = sixk_cols[0]
    x = col.x + L.LANE_W / 2
    y = view.y_for(2.5)  # measure 2, half way
    click(view, x, y)
    assert view.project.note_count(6) == 1, "note should have been placed in 6K"

    # Place a BGM start object.
    bgm_col = view.columns[0]
    click(view, bgm_col.x + 5, view.y_for(0.0))
    assert view.project.bgm, "BGM object should have been placed"

    # Right-click the note erases it (recompute y from the actual note in case
    # the timeline auto-extended and shifted screen positions).
    note = next(iter(view.project.charts[6]))
    click(view, x, view.y_for(note.absolute), Qt.RightButton)
    assert view.project.note_count(6) == 0, "note should have been erased"

    # Export path exercises the whole pipeline.
    from slimbms import bms_io
    text = bms_io.export_bms(view.project, 6)
    assert "#TITLE Smoke" in text

    # Preview playback advances the playhead even without an audio device
    # (the clock runs off a monotonic timer; audio degrades gracefully).
    import time
    win._start_play()
    time.sleep(0.05)
    win._on_play_tick()
    assert view.playhead is not None and view.playhead >= 0, "playhead should be set"
    win.stop_play()
    assert view.playhead is None, "stop should clear the playhead"

    # Long note by mouse: left-press then drag up creates a hold in add mode.
    view.set_grid_main(16)
    fourk0 = [c for c in view.columns if c.kind == "key" and c.key_mode == 4][0]
    xcol = fourk0.x + L.LANE_W / 2
    drag(view, xcol, view.y_for(2.0), view.y_for(2.5))
    longs = [n for n in view.project.charts[4] if n.is_long]
    assert len(longs) == 1, "drag should produce one long note"
    assert longs[0].absolute == Fraction(2) and longs[0].length == Fraction(1, 2), \
        f"long note span wrong: {longs[0].absolute}+{longs[0].length}"
    # Right-click anywhere on its body erases the whole long note.
    click(view, xcol, view.y_for(2.25), Qt.RightButton)
    assert not [n for n in view.project.charts[4] if n.is_long], "LN body erase failed"

    # Live keyboard recording: while playing, mapped keys drop a note at the
    # playhead time in the selected key mode's lane (the cursor is not involved).
    win._start_play()
    assert view.live_playing, "view should be in live mode while playing"
    assert view.selected_km == 4, "default selected key mode is 4K"
    view.set_playhead(3.20)                 # snaps to nearest 1/16 -> 51/16
    press(view, Qt.Key_Q); release(view, Qt.Key_Q)   # quick tap -> lane 0
    press(view, Qt.Key_9); release(view, Qt.Key_9)   # quick tap -> lane 3
    assert view.project.note_count(4) == 2, "two taps should record two notes"
    assert sorted(n.lane for n in view.project.charts[4]) == [0, 3]
    assert all(n.absolute == Fraction(51, 16) for n in view.project.charts[4]), \
        "recorded notes should snap to the nearest 1/16 grid line at the playhead"
    assert not any(n.is_long for n in view.project.charts[4]), "quick taps aren't long"

    # Holding a key records only a single tap — auto-repeat never grows a long
    # note (removed in v0.38.1 so a brief hold on a fine grid can't become one).
    view.set_playhead(4.0)
    press(view, Qt.Key_Q)                   # 4K lane 0 tap at measure 4
    view.set_playhead(4.75)                 # playhead advances while held
    press(view, Qt.Key_Q, autorep=True)     # auto-repeat is ignored
    view.set_playhead(5.0)
    release(view, Qt.Key_Q)
    held = [n for n in view.project.charts[4] if n.measure == 4]
    assert len(held) == 1 and not held[0].is_long, \
        f"a held key should record one plain tap, got {held}"

    # 6K maps E and numpad-7 to distinct lanes (2 and 3).
    win._km_actions[6].trigger()            # switch to 6K
    assert view.selected_km == 6
    view.set_playhead(6.0)
    press(view, Qt.Key_E); release(view, Qt.Key_E)   # 6K lane 2
    view.set_playhead(7.0)
    press(view, Qt.Key_7); release(view, Qt.Key_7)   # 6K lane 3
    assert sorted(n.lane for n in view.project.charts[6]) == [2, 3], \
        "E and 7 map to distinct 6K lanes"
    win.stop_play()

    # Arrow-key moves must never absorb another note: moving onto an occupied
    # cell keeps both (overlap, flagged) and fires a one-shot overlap warning.
    view.overlap_warning.disconnect(win._warn_overlap)   # avoid the modal in tests
    warned = []
    view.overlap_warning.connect(lambda: warned.append(1))
    view.set_mode("edit")
    view.grid_main = Fraction(1)
    for km in view.project.charts.values():
        km.clear()
    a = Note(1, Fraction(0), 0)
    b = Note(2, Fraction(0), 0)
    view.project.charts[4].append(a)
    view.project.charts[4].append(b)
    view.selection = {(4, a)}
    view._move_selection(0, 1)              # a: measure 1 -> 2, right onto b
    assert len(view.project.charts[4]) == 2, "a move must not absorb another note"
    assert warned, "overlapping another note should warn"
    view._move_selection(0, 1)              # a passes through to measure 3, b stays
    assert sorted(int(n.measure) for n in view.project.charts[4]) == [2, 3], \
        "the passed-over note must stay put"

    # -- 노트 속도: the end row doubles as the "ramp or not" switch ---------- #
    p = win.project
    p.scrolls.clear()
    p.speeds.clear()

    def set_scroll(m1, c1, v1, m2, c2, v2):
        win.scroll_measure.setValue(m1); win.scroll_cell.setValue(c1)
        win.scroll_value.setValue(v1)
        win.scroll_measure2.setValue(m2); win.scroll_cell2.setValue(c2)
        win.scroll_value2.setValue(v2)

    set_scroll(4, 0, 2.0, 8, 0, 1.0)          # a real range -> linear ramp
    assert all(not wdg.property("inactive") for wdg in win._scroll_end_row), \
        "a usable end point must light the end row up"
    win._add_scroll()
    assert not p.scrolls and len(p.speeds) == 2, "end after start must stay a ramp"

    set_scroll(12, 0, 3.0, 0, 0, 0.0)         # end below start -> instant step
    assert all(wdg.property("inactive") for wdg in win._scroll_end_row), \
        "an end point below the start must grey the end row out"
    win._add_scroll()
    assert p.scrolls == {Fraction(12): Fraction(3)}, "end below start must be 순간"
    assert len(p.speeds) == 2, "the instant step must not touch the ramp"

    win._load_scroll(("scroll", Fraction(12)))   # double-clicking an instant row
    assert (win.scroll_measure.value(), win.scroll_value.value()) == (12, 3.0)
    assert (win.scroll_measure2.value(), win.scroll_cell2.value(),
            win.scroll_value2.value()) == (0, 0, 0.0), "the end row must zero out"
    assert all(wdg.property("inactive") for wdg in win._scroll_end_row), \
        "loading a 순간 marker must leave the end row greyed"
    win._commit_scroll(("scroll", Fraction(12)))
    assert p.scrolls == {Fraction(12): Fraction(3)}, "re-committing must stay 순간"

    win._load_scroll(("speed", Fraction(4), Fraction(8)))   # ramps load unchanged
    assert (win.scroll_measure.value(), win.scroll_value.value()) == (4, 2.0)
    assert (win.scroll_measure2.value(), win.scroll_value2.value()) == (8, 1.0)

    print("GUI smoke test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
