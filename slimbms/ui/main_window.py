"""Main application window: menus, toolbar and the scrolling chart canvas."""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import bms_io
from ..model import KEY_MODES, Project
from .chart_view import ChartView, LaneHeader
from .metadata_dialog import MetadataDialog

# Snap options shown in the toolbar: label -> denominator (fraction of a measure)
SNAP_OPTIONS = [
    ("1/4", 4),
    ("1/8", 8),
    ("1/16", 16),
    ("1/32", 32),
    ("1/6", 6),
    ("1/12", 12),
    ("1/24", 24),
]


class MainWindow(QMainWindow):
    def __init__(self, project: Optional[Project] = None):
        super().__init__()
        self.project = project or Project()
        self.project_path: Optional[str] = None
        self._dirty = False

        self.setWindowTitle("SlimBMS")
        self.resize(720, 900)

        self._build_canvas()
        self._build_toolbar()
        self._build_menu()
        self._update_title()

    # -- construction ------------------------------------------------------- #

    def _build_canvas(self) -> None:
        central = QWidget()
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        self.header = LaneHeader()
        vbox.addWidget(self.header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.view = ChartView(self.project)
        self.view.changed.connect(self._on_changed)
        self.scroll.setWidget(self.view)
        self.scroll.horizontalScrollBar().valueChanged.connect(self.header.set_x_offset)
        vbox.addWidget(self.scroll)

        self.setCentralWidget(central)
        # Start scrolled to the bottom (song start).
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())

    def _build_toolbar(self) -> None:
        tb = QToolBar("도구")
        tb.setMovable(False)
        self.addToolBar(tb)

        tb.addWidget(QLabel(" 스냅 "))
        self.snap_combo = QComboBox()
        for label, _div in SNAP_OPTIONS:
            self.snap_combo.addItem(label)
        self.snap_combo.setCurrentIndex(1)  # 1/8
        self.snap_combo.currentIndexChanged.connect(self._on_snap_changed)
        tb.addWidget(self.snap_combo)

        tb.addSeparator()
        tb.addWidget(QLabel(" 줌 "))
        zoom_out = QAction("−", self)
        zoom_out.triggered.connect(lambda: self.view.set_zoom(self.view.measure_px - 30))
        tb.addAction(zoom_out)
        zoom_in = QAction("＋", self)
        zoom_in.triggered.connect(lambda: self.view.set_zoom(self.view.measure_px + 30))
        tb.addAction(zoom_in)

        tb.addSeparator()
        tb.addWidget(QLabel(" 저장할 키 "))
        self.keymode_combo = QComboBox()
        for km in KEY_MODES:
            self.keymode_combo.addItem(f"{km}K")
        tb.addWidget(self.keymode_combo)
        export_action = QAction("이 키로 .bms 저장", self)
        export_action.triggered.connect(self.export_bms)
        tb.addAction(export_action)

    def _build_menu(self) -> None:
        m = self.menuBar()

        file_menu = m.addMenu("파일")
        self._add(file_menu, "새로 만들기", self.new_project, QKeySequence.New)
        self._add(file_menu, "프로젝트 열기…", self.open_project, QKeySequence.Open)
        self._add(file_menu, "프로젝트 저장", self.save_project, QKeySequence.Save)
        self._add(file_menu, "프로젝트 다른 이름으로 저장…", self.save_project_as,
                  QKeySequence("Ctrl+Shift+S"))
        file_menu.addSeparator()
        self._add(file_menu, "BMS 가져오기…", self.import_bms)
        self._add(file_menu, "선택한 키로 .bms 내보내기…", self.export_bms,
                  QKeySequence("Ctrl+E"))
        file_menu.addSeparator()
        self._add(file_menu, "종료", self.close)

        song_menu = m.addMenu("곡")
        self._add(song_menu, "곡 정보 편집…", self.edit_metadata)
        self._add(song_menu, "BGM 오디오 선택…", self.choose_bgm)

    def _add(self, menu, text, slot, shortcut=None) -> None:
        act = QAction(text, self)
        act.triggered.connect(slot)
        if shortcut is not None:
            act.setShortcut(shortcut)
        menu.addAction(act)

    # -- state -------------------------------------------------------------- #

    def _on_changed(self) -> None:
        self._dirty = True
        self._update_title()

    def _on_snap_changed(self, index: int) -> None:
        self.view.set_snap(SNAP_OPTIONS[index][1])

    def _update_title(self) -> None:
        name = os.path.basename(self.project_path) if self.project_path else "제목 없음"
        star = "*" if self._dirty else ""
        song = self.project.title or "(무제)"
        self.setWindowTitle(f"SlimBMS — {song} — {name}{star}")

    def _reload_view(self) -> None:
        self.view.project = self.project
        self.view.refresh()
        self._update_title()

    # -- file actions ------------------------------------------------------- #

    def new_project(self) -> None:
        if not self._confirm_discard():
            return
        self.project = Project()
        self.project_path = None
        self._dirty = False
        self._reload_view()

    def open_project(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "프로젝트 열기", "", "SlimBMS 프로젝트 (*.slbms);;모든 파일 (*)")
        if not path:
            return
        try:
            self.project = bms_io.load_project(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "열기 실패", str(exc))
            return
        self.project_path = path
        self._dirty = False
        self._reload_view()

    def save_project(self) -> None:
        if not self.project_path:
            self.save_project_as()
            return
        try:
            bms_io.save_project(self.project, self.project_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "저장 실패", str(exc))
            return
        self._dirty = False
        self._update_title()

    def save_project_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "프로젝트 저장", self._suggest_name(".slbms"),
            "SlimBMS 프로젝트 (*.slbms)")
        if not path:
            return
        if not path.lower().endswith(".slbms"):
            path += ".slbms"
        self.project_path = path
        self.save_project()

    def import_bms(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "BMS 가져오기", "", "BMS 채보 (*.bms *.bme *.bml);;모든 파일 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                self.project = bms_io.parse_bms(fh.read())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "가져오기 실패", str(exc))
            return
        self.project_path = None
        self._dirty = True
        self._reload_view()

    def export_bms(self) -> None:
        km = KEY_MODES[self.keymode_combo.currentIndex()]
        if not self.project.bgm:
            QMessageBox.warning(
                self, "BGM 없음",
                "BGM 출력 시작 타이밍이 없습니다. BGM 레인에 시작 지점을 먼저 찍어주세요.")
        default = self._suggest_name(f"_{km}k.bms")
        path, _ = QFileDialog.getSaveFileName(
            self, f"{km}K .bms 내보내기", default, "BMS 채보 (*.bms)")
        if not path:
            return
        if not path.lower().endswith(".bms"):
            path += ".bms"
        try:
            text = bms_io.export_bms(self.project, km)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "내보내기 실패", str(exc))
            return
        QMessageBox.information(
            self, "내보내기 완료",
            f"{km}K 채보를 저장했습니다:\n{path}\n\n노트 수: {self.project.note_count(km)}")

    # -- song actions ------------------------------------------------------- #

    def edit_metadata(self) -> None:
        dlg = MetadataDialog(self.project, self)
        if dlg.exec():
            dlg.apply_to(self.project)
            self._on_changed()
            self.view.refresh()

    def choose_bgm(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "BGM 오디오 선택", "",
            "오디오 (*.wav *.ogg *.mp3 *.flac);;모든 파일 (*)")
        if not path:
            return
        self.project.bgm_file = os.path.basename(path)
        self._on_changed()
        QMessageBox.information(
            self, "BGM 설정됨",
            f"BGM 파일명: {self.project.bgm_file}\n"
            "이 파일을 .bms와 같은 폴더에 두어야 게임에서 재생됩니다.")

    # -- helpers ------------------------------------------------------------ #

    def _suggest_name(self, suffix: str) -> str:
        base = self.project.title.strip() or "untitled"
        base = "".join(c for c in base if c.isalnum() or c in " _-").strip() or "untitled"
        return base + suffix

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        res = QMessageBox.question(
            self, "저장하지 않은 변경",
            "저장하지 않은 변경사항이 있습니다. 계속할까요?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return res == QMessageBox.Yes

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()
