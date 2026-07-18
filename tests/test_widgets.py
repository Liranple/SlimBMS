"""Tests for the small UI classes extracted out of main_window.py in P5:
the DragValue gauge (widgets), the background Worker (worker) and the
KeybindingsDialog (dialogs). These had no direct coverage before.

Run: QT_QPA_PLATFORM=offscreen python tests/test_widgets.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QAction, QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from slimbms.ui.dialogs import KeybindingsDialog  # noqa: E402
from slimbms.ui.widgets import DragValue  # noqa: E402
from slimbms.ui.worker import _Worker  # noqa: E402


def _app():
    return QApplication.instance() or QApplication([])


def test_dragvalue_maps_and_clamps():
    _app()
    d = DragValue("×", 0.0, 1.0, 0.1, 0.5)
    d.resize(200, 30)
    assert d.value() == 0.5
    # groove maps [GROOVE_L, width-GROOVE_R_PAD] -> [min, max]
    assert abs(d._value_at(d.GROOVE_L) - 0.0) < 1e-9
    assert abs(d._value_at(d.width() - d.GROOVE_R_PAD) - 1.0) < 1e-9
    d.step_by(2)
    assert abs(d.value() - 0.7) < 1e-9
    d.set_value(5.0)
    assert d.value() == 1.0       # clamped to max
    d.set_value(-5.0)
    assert d.value() == 0.0       # clamped to min


def test_dragvalue_emits_on_change():
    _app()
    d = DragValue("♪", 0.0, 1.0, 0.05, 0.0)
    seen = []
    d.changed.connect(seen.append)
    d.set_value(0.5)
    assert seen == [0.5]
    d.set_value(0.5)              # no change -> no emit
    assert seen == [0.5]


def test_worker_delivers_result_and_error():
    _app()
    # Same-thread _run() gives a direct signal delivery (no event loop needed).
    w = _Worker(lambda: 42)
    out = []
    w.done.connect(out.append)
    w._run()
    assert out == [42]

    w2 = _Worker(lambda: 1 / 0)
    errs = []
    w2.failed.connect(errs.append)
    w2._run()
    assert errs and "division" in errs[0].lower()


def test_keybindings_dialog_roundtrip():
    from PySide6.QtCore import Qt

    _app()
    act = QAction("Play")
    act.setShortcut(QKeySequence("Space"))
    key_actions = {"play": (act, "재생", "Space")}
    # 4K and 6K recording keys are edited on separate tabs.
    record_lists = {4: [int(Qt.Key_Q), int(Qt.Key_W)],
                    6: [int(Qt.Key_A), int(Qt.Key_S), int(Qt.Key_D)]}
    record_defaults = {k: list(v) for k, v in record_lists.items()}
    dlg = KeybindingsDialog(key_actions, record_lists, record_defaults)
    assert dlg.result_shortcuts()["play"] == "Space"
    assert dlg.result_record_keys()[4] == [int(Qt.Key_Q), int(Qt.Key_W)]

    # Editing a shortcut and a 4K recording key, then reading them back.
    dlg._edits["play"].setKeySequence(QKeySequence("Ctrl+P"))
    dlg._rec_edits[4][1].setKeySequence(QKeySequence(Qt.Key_K))
    assert dlg.result_shortcuts()["play"] == "Ctrl+P"
    assert dlg.result_record_keys()[4] == [int(Qt.Key_Q), int(Qt.Key_K)]
    assert dlg.result_record_keys()[6] == [int(Qt.Key_A), int(Qt.Key_S), int(Qt.Key_D)]

    # Restoring defaults reverts both shortcuts and recording keys.
    dlg._restore_defaults()
    assert dlg.result_shortcuts()["play"] == "Space"
    assert dlg.result_record_keys()[4] == [int(Qt.Key_Q), int(Qt.Key_W)]


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
