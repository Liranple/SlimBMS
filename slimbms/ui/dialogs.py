"""Small application dialogs, kept out of the main-window module."""

from __future__ import annotations

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QKeySequenceEdit,
    QVBoxLayout,
)


class KeybindingsDialog(QDialog):
    """Lets the user reassign the app's configurable shortcuts."""

    def __init__(self, key_actions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("단축키 설정")
        self.setMinimumWidth(340)
        self._edits = {}
        self._defaults = {k: d for k, (_a, _l, d) in key_actions.items()}

        v = QVBoxLayout(self)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        for key, (act, label, _default) in key_actions.items():
            edit = QKeySequenceEdit(act.shortcut())
            self._edits[key] = edit
            form.addRow(label, edit)
        v.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
            | QDialogButtonBox.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(
            self._restore_defaults)
        v.addWidget(buttons)

    def _restore_defaults(self) -> None:
        for key, edit in self._edits.items():
            edit.setKeySequence(QKeySequence(self._defaults[key]))

    def result_shortcuts(self):
        return {key: edit.keySequence().toString()
                for key, edit in self._edits.items()}
