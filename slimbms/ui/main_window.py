"""Main application window: menus, toolbar and the scrolling chart canvas."""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QTimer
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
from ..audio import AudioPlayer
from ..model import KEY_MODES, Project
from ..timing import TimeMap
from .chart_view import ChartView, LaneHeader
from .metadata_dialog import MetadataDialog

PREVIEW_FPS = 60
# Where the playhead sits within the viewport (fraction from the top). Notes
# scroll upward, so keeping it low leaves upcoming notes visible above it.
PLAYHEAD_VIEWPORT_FRACTION = 0.72

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

        # Playback / preview.
        self.audio = AudioPlayer()
        self._bgm_path: Optional[str] = None   # full path for playback
        self._timemap: Optional[TimeMap] = None
        self._preview_active = False
        self._loop_a: Optional[float] = None
        self._loop_b: Optional[float] = None
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(int(1000 / PREVIEW_FPS))
        self._play_timer.timeout.connect(self._on_play_tick)

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

        start_action = QAction("⏮", self)
        start_action.setToolTip("처음으로 (Home)")
        start_action.setShortcut(Qt.Key_Home)
        start_action.triggered.connect(self.go_to_start)
        tb.addAction(start_action)

        back_action = QAction("◀ 1마디", self)
        back_action.setToolTip("한 마디 뒤로 (←)")
        back_action.setShortcut(Qt.Key_Left)
        back_action.triggered.connect(lambda: self.seek_relative(-1))
        tb.addAction(back_action)

        self.play_action = QAction("▶ 재생", self)
        self.play_action.setShortcut(Qt.Key_Space)
        self.play_action.triggered.connect(self.toggle_play)
        tb.addAction(self.play_action)

        fwd_action = QAction("1마디 ▶", self)
        fwd_action.setToolTip("한 마디 앞으로 (→)")
        fwd_action.setShortcut(Qt.Key_Right)
        fwd_action.triggered.connect(lambda: self.seek_relative(1))
        tb.addAction(fwd_action)

        stop_action = QAction("■ 정지", self)
        stop_action.triggered.connect(self.stop_play)
        tb.addAction(stop_action)
        tb.addSeparator()

        # A-B loop (repeat a section).
        set_a = QAction("A", self)
        set_a.setToolTip("현재 위치를 반복 시작점으로 ([)")
        set_a.setShortcut(Qt.Key_BracketLeft)
        set_a.triggered.connect(self.set_loop_a)
        tb.addAction(set_a)
        set_b = QAction("B", self)
        set_b.setToolTip("현재 위치를 반복 끝점으로 (])")
        set_b.setShortcut(Qt.Key_BracketRight)
        set_b.triggered.connect(self.set_loop_b)
        tb.addAction(set_b)
        self.loop_action = QAction("🔁 반복", self)
        self.loop_action.setCheckable(True)
        self.loop_action.setToolTip("A-B 구간 반복 켜기/끄기 (\\)")
        self.loop_action.setShortcut(Qt.Key_Backslash)
        tb.addAction(self.loop_action)
        clear_loop = QAction("구간 해제", self)
        clear_loop.triggered.connect(self.clear_loop)
        tb.addAction(clear_loop)
        tb.addSeparator()

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
        self.stop_play()
        self.view.project = self.project
        self.view.refresh()
        self._update_title()

    # -- playback / preview ------------------------------------------------- #

    def _ensure_timemap(self) -> TimeMap:
        # Rebuilt on demand so BPM / BGM-offset edits always take effect.
        self._timemap = TimeMap(self.project)
        return self._timemap

    def _current_chart_pos(self) -> float:
        return self._ensure_timemap().chart_pos(self.audio.position())

    def toggle_play(self) -> None:
        if self.audio.playing:
            self._pause_play()
        else:
            self._start_play()

    def _start_play(self) -> None:
        self._ensure_timemap()
        self._preview_active = True
        # Resume from the current position (0 when stopped -> top of the song).
        self.audio.play(self.audio.position())
        self.play_action.setText("⏸ 일시정지")
        self._play_timer.start()

    def _pause_play(self) -> None:
        self.audio.pause()
        self._play_timer.stop()
        self.play_action.setText("▶ 재생")
        # Keep the playhead visible where we paused so seeking has a reference.
        self.view.set_playhead(self._current_chart_pos())

    def stop_play(self) -> None:
        self.audio.stop()
        self._play_timer.stop()
        self.play_action.setText("▶ 재생")
        self._preview_active = False
        self.view.set_playhead(None)

    def go_to_start(self) -> None:
        self._seek_to(0.0)

    def seek_relative(self, d_measures: float) -> None:
        cur = self._current_chart_pos()
        self._seek_to(cur + d_measures)

    def _seek_to(self, chart_pos: float) -> None:
        chart_pos = max(0.0, min(float(self.project.measures), chart_pos))
        secs = self._ensure_timemap().audio_seconds(chart_pos)
        self.audio.seek(secs)
        self._preview_active = True
        self.view.set_playhead(chart_pos)
        self._follow_playhead(chart_pos)

    def _on_play_tick(self) -> None:
        if self._timemap is None:
            return
        pos = self.audio.position()
        chart_pos = self._timemap.chart_pos(pos)
        # A-B loop: jump back to A when we reach B.
        if (self.loop_action.isChecked() and self._loop_a is not None
                and self._loop_b is not None and chart_pos >= self._loop_b):
            self._seek_to(self._loop_a)
            return
        # Stop at the end of the timeline (or audio).
        if chart_pos >= self.project.measures or (
            self.audio.duration and pos >= self.audio.duration
        ):
            self.stop_play()
            return
        self.view.set_playhead(chart_pos)
        self._follow_playhead(chart_pos)

    # -- A-B loop ----------------------------------------------------------- #

    def set_loop_a(self) -> None:
        self._loop_a = self._current_chart_pos()
        if self._loop_b is not None and self._loop_b <= self._loop_a:
            self._loop_b = None
        self.view.set_loop(self._loop_a, self._loop_b)

    def set_loop_b(self) -> None:
        b = self._current_chart_pos()
        if self._loop_a is not None and b <= self._loop_a:
            QMessageBox.information(self, "구간 반복",
                                    "반복 끝점(B)은 시작점(A)보다 뒤여야 합니다.")
            return
        self._loop_b = b
        self.view.set_loop(self._loop_a, self._loop_b)

    def clear_loop(self) -> None:
        self._loop_a = None
        self._loop_b = None
        self.loop_action.setChecked(False)
        self.view.set_loop(None, None)

    def _viewport_chart_pos(self) -> float:
        """Chart position currently at the playhead line in the viewport."""
        vbar = self.scroll.verticalScrollBar()
        vp_h = self.scroll.viewport().height()
        y_in_view = vbar.value() + vp_h * PLAYHEAD_VIEWPORT_FRACTION
        absolute = self.project.measures - (y_in_view - self.view.v_pad) / self.view.measure_px
        return max(0.0, absolute)

    def _follow_playhead(self, chart_pos: float) -> None:
        vbar = self.scroll.verticalScrollBar()
        vp_h = self.scroll.viewport().height()
        y = self.view.y_for(chart_pos)
        target = int(y - vp_h * PLAYHEAD_VIEWPORT_FRACTION)
        target = max(vbar.minimum(), min(vbar.maximum(), target))
        vbar.setValue(target)

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
        self._bgm_path = path
        loaded = self.audio.load(path)
        self._on_changed()
        note = "" if loaded else "\n\n(이 환경에서는 오디오 장치가 없어 재생 미리보기는 실제 PC에서만 됩니다.)"
        QMessageBox.information(
            self, "BGM 설정됨",
            f"BGM 파일명: {self.project.bgm_file}\n"
            "이 파일을 .bms와 같은 폴더에 두어야 게임에서 재생됩니다." + note)

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
