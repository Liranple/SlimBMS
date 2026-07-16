"""Guards the P4 colour/settings consolidation: the shared design tokens must
keep their exact values, and the theme + canvas must derive from them (so they
can't drift), and the QSettings helper must use the app's org/app pair.

Run: QT_QPA_PLATFORM=offscreen python tests/test_palette.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from slimbms.ui import palette  # noqa: E402


# The exact, known-good values before the consolidation — locked so a future
# edit to palette.py can't silently recolour the app.
EXPECTED = {
    "APP_BG": "#17171c", "PANEL": "#212129", "CANVAS": "#1e1e24",
    "FIELD": "#2a2a33", "BORDER": "#33333d", "BORDER_STRONG": "#44444f",
    "TEXT": "#d6d6de", "TEXT_DIM": "#9a9aa6", "ACCENT": "#6fd0ff",
    "ACCENT_INK": "#0c1116", "DANGER": "#ff6b81",
}


def test_palette_values_unchanged():
    for name, value in EXPECTED.items():
        assert getattr(palette, name) == value, f"{name} changed to {getattr(palette, name)}"


def test_theme_derives_from_palette():
    from slimbms.ui import theme
    for name in ("APP_BG", "PANEL", "CANVAS", "FIELD", "BORDER",
                 "BORDER_STRONG", "TEXT", "TEXT_DIM", "ACCENT",
                 "ACCENT_INK", "DANGER"):
        assert getattr(theme, name) == getattr(palette, name), \
            f"theme.{name} drifted from palette"


def test_canvas_shared_colours_match_palette():
    QApplication.instance() or QApplication([])
    from slimbms.ui import chart_view as cv
    # The three colours that were duplicated across files must now equal the
    # single-source tokens exactly.
    assert cv.C_BG.name() == palette.CANVAS
    assert cv.C_SELECT.name() == palette.ACCENT
    assert cv.C_LANE_SEP.name() == palette.BORDER
    # And the intra-file dedup kept its value.
    assert cv.C_GROUP_BG_B.name() == "#1b1b21"


def test_settings_helper_uses_app_pair():
    QApplication.instance() or QApplication([])
    from slimbms.ui.main_window import _settings, _SETTINGS_ORG, _SETTINGS_APP
    s = _settings()
    assert (_SETTINGS_ORG, _SETTINGS_APP) == ("SlimBMS", "SlimBMS")
    assert s.organizationName() == "SlimBMS"
    assert s.applicationName() == "SlimBMS"


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
