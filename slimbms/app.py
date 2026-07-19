"""Application entry point."""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication

from .ui.appicon import build_icon
from .ui.main_window import MainWindow
from .ui.theme import apply_theme


def _set_windows_app_id() -> None:
    """Give Windows an explicit AppUserModelID so the taskbar uses our window
    icon instead of grouping under the default Python/host icon."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SlimBMS.Editor")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("SlimBMS")
    app.setWindowIcon(build_icon())
    apply_theme(app)
    window = MainWindow()
    window.show()

    # When launched via double-click / file association, Windows passes the
    # file path as an argument. Open it instead of showing an empty window.
    path = _project_path_from_args(app.arguments()[1:])
    if path:
        window.load_project_path(path)

    return app.exec()


def _project_path_from_args(args: list[str]) -> str | None:
    """Return the first existing .slbms path among the launch arguments."""
    for arg in args:
        if arg.lower().endswith(".slbms") and os.path.isfile(arg):
            return arg
    return None


if __name__ == "__main__":
    sys.exit(main())
