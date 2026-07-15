"""Offscreen smoke test: build the window, place notes via the canvas, export."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from slimbms.model import Project  # noqa: E402
from slimbms.ui.main_window import MainWindow  # noqa: E402
from slimbms.ui import layout as L  # noqa: E402


def click(view, x, y, button=Qt.LeftButton):
    pos = QPointF(x, y)
    ev = QMouseEvent(QMouseEvent.MouseButtonPress, pos, pos, button, button, Qt.NoModifier)
    view.mousePressEvent(ev)


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

    print("GUI smoke test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
