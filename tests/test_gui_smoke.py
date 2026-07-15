"""Offscreen smoke test: build the window, place notes via the canvas, export."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fractions import Fraction  # noqa: E402

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from slimbms.model import Project  # noqa: E402
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

    # Click in the middle of the 5K group's first lane, somewhere in the timeline.
    fivek_cols = [c for c in view.columns if c.kind == "key" and c.key_mode == 5]
    col = fivek_cols[0]
    x = col.x + L.LANE_W / 2
    y = view.y_for(2.5)  # measure 2, half way
    click(view, x, y)
    assert view.project.note_count(5) == 1, "note should have been placed in 5K"

    # Place a BGM start object.
    bgm_col = view.columns[0]
    click(view, bgm_col.x + 5, view.y_for(0.0))
    assert view.project.bgm, "BGM object should have been placed"

    # Right-click the note erases it (recompute y from the actual note in case
    # the timeline auto-extended and shifted screen positions).
    note = next(iter(view.project.charts[5]))
    click(view, x, view.y_for(note.absolute), Qt.RightButton)
    assert view.project.note_count(5) == 0, "note should have been erased"

    # Export path exercises the whole pipeline.
    from slimbms import bms_io
    text = bms_io.export_bms(view.project, 5)
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

    # Holding a key while the playhead advances records a long note.
    view.set_playhead(4.0)
    press(view, Qt.Key_Q)                   # 4K lane 0 head at measure 4
    view.set_playhead(4.75)                 # playhead advances while held
    press(view, Qt.Key_Q, autorep=True)     # auto-repeat grows it
    view.set_playhead(5.0)
    release(view, Qt.Key_Q)                 # release fixes the tail at 5.0
    held = [n for n in view.project.charts[4] if n.is_long and n.measure == 4]
    assert len(held) == 1 and held[0].length == Fraction(1), \
        f"hold should record a 1-measure long note, got {held}"

    # 5K shares its middle lane between E and numpad-7.
    win.keymode_combo.setCurrentIndex(1)    # 5K
    assert view.selected_km == 5
    view.set_playhead(6.0)
    press(view, Qt.Key_E); release(view, Qt.Key_E)   # 5K lane 2
    view.set_playhead(7.0)
    press(view, Qt.Key_7); release(view, Qt.Key_7)   # 5K lane 2 (duplicate mapping)
    assert sorted(n.lane for n in view.project.charts[5]) == [2, 2], \
        "E and 7 both target the 5K middle lane"
    win.stop_play()

    print("GUI smoke test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
