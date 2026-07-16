"""Run a blocking function off the UI thread, delivering its result (or error)
back to the UI thread via queued signals."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal


class _Worker(QObject):
    """Runs a function on a daemon thread and delivers the result to the UI
    thread via a queued signal."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
