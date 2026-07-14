"""Dialog for editing shared song metadata."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
)

from ..model import Project


class MetadataDialog(QDialog):
    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.setWindowTitle("곡 정보")
        self.project = project

        self.title = QLineEdit(project.title)
        self.artist = QLineEdit(project.artist)
        self.genre = QLineEdit(project.genre)
        self.bpm = QDoubleSpinBox()
        self.bpm.setRange(1.0, 999.0)
        self.bpm.setDecimals(2)
        self.bpm.setValue(project.bpm)
        self.measures = QSpinBox()
        self.measures.setRange(1, 999)
        self.measures.setValue(project.measures)

        form = QFormLayout(self)
        form.addRow("제목", self.title)
        form.addRow("아티스트", self.artist)
        form.addRow("장르", self.genre)
        form.addRow("BPM", self.bpm)
        form.addRow("마디 수", self.measures)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def apply_to(self, project: Project) -> None:
        project.title = self.title.text()
        project.artist = self.artist.text()
        project.genre = self.genre.text()
        project.bpm = self.bpm.value()
        project.measures = self.measures.value()
