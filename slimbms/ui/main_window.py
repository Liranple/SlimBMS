"""Main application window: menus, toolbar and the scrolling chart canvas."""

from __future__ import annotations

import os
from typing import Optional

import threading

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, bms_io, updater
from ..audio import AudioPlayer
from ..model import IMPORT_MODE, KEY_MODES, Project
from ..timing import TimeMap
from .appicon import build_icon
from .chart_view import ChartView, LaneHeader
from .toolbar_icons import make_icon
from .widgets import CollapsibleSection, NoWheelDoubleSpinBox, NoWheelSpinBox


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

class DragValue(QWidget):
    """A compact 'drag to adjust' control, like a volume knob: press and slide
    the mouse left or right to change a numeric value in fixed steps. An icon
    sits on the left and the current value is shown on the right. Vertical
    movement is ignored, so the mouse only needs to travel sideways."""

    changed = Signal(float)
    PX_PER_STEP = 14   # horizontal pixels the mouse travels for one step

    def __init__(self, icon: str, minimum: float, maximum: float,
                 step: float, value: float, parent=None):
        super().__init__(parent)
        self._icon = icon
        self._min = float(minimum)
        self._max = float(maximum)
        self._step = float(step)
        self._value = self._quant(value)
        self._drag_x = None
        self._drag_val = self._value
        self.setCursor(Qt.SizeHorCursor)
        self.setFixedHeight(30)
        self.setMinimumWidth(160)

    def _quant(self, v: float) -> float:
        v = round(v / self._step) * self._step
        return max(self._min, min(self._max, v))

    def value(self) -> float:
        return self._value

    def set_value(self, v: float, notify: bool = True) -> None:
        v = self._quant(v)
        if abs(v - self._value) < 1e-9:
            return
        self._value = v
        self.update()
        if notify:
            self.changed.emit(v)

    def step_by(self, n: int) -> None:
        self.set_value(self._value + n * self._step)

    # -- interaction -------------------------------------------------------- #

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_x = event.position().x()
            self._drag_val = self._value
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_x is None:
            return
        dx = event.position().x() - self._drag_x   # sideways travel only
        steps = round(dx / self.PX_PER_STEP)
        self.set_value(self._drag_val + steps * self._step)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_x = None

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.ignore()   # no wheel adjustment; let the sidebar scroll instead

    # -- painting ----------------------------------------------------------- #

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        cy = h / 2

        # Icon on the left.
        icon_font = QFont()
        icon_font.setPointSize(14)
        p.setFont(icon_font)
        p.setPen(QColor("#9fb4c8"))
        p.drawText(QRect(0, 0, 22, h), Qt.AlignCenter, self._icon)

        # Current value on the right.
        val_font = QFont()
        val_font.setPointSize(9)
        val_font.setBold(True)
        p.setFont(val_font)
        p.setPen(QColor("#e6ecf2"))
        p.drawText(QRect(w - 46, 0, 46, h),
                   Qt.AlignRight | Qt.AlignVCenter, f"{self._value:.2f}")

        # Groove between icon and value, with a filled portion and a round handle.
        gx0, gx1 = 28, w - 52
        if gx1 <= gx0:
            p.end()
            return
        span = self._max - self._min
        frac = (self._value - self._min) / span if span > 0 else 0.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#31313b"))
        p.drawRoundedRect(QRect(gx0, int(cy) - 3, gx1 - gx0, 6), 3, 3)
        fill_w = int((gx1 - gx0) * frac)
        p.setBrush(QColor("#6fd0ff"))
        p.drawRoundedRect(QRect(gx0, int(cy) - 3, fill_w, 6), 3, 3)
        p.setBrush(QColor("#dbe7f2"))
        p.drawEllipse(QPoint(gx0 + fill_w, int(cy)), 6, 6)
        p.end()


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


PREVIEW_FPS = 60
# Where the playhead sits within the viewport (fraction from the top). Notes
# scroll upward, so keeping it low leaves upcoming notes visible above it.
PLAYHEAD_VIEWPORT_FRACTION = 0.72


class MainWindow(QMainWindow):
    def __init__(self, project: Optional[Project] = None):
        super().__init__()
        self.project = project or Project()
        self.project_path: Optional[str] = None
        self._dirty = False
        self._last_dirs = {}  # per-operation last folder (open/save/import/export/bgm)

        # Playback / preview.
        self.audio = AudioPlayer()
        self._bgm_path: Optional[str] = None   # full path for playback
        self._timemap: Optional[TimeMap] = None
        self._preview_active = False
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(int(1000 / PREVIEW_FPS))
        self._play_timer.timeout.connect(self._on_play_tick)
        # Recording aids: count-in beats + metronome beat tracking.
        self._counting_in = False
        self._countin_left = 0
        self._last_beat = -1
        self._countin_timer = QTimer(self)
        self._countin_timer.timeout.connect(self._countin_tick)
        # Debounce speed-gauge drags: rebuild the stretch once the drag settles.
        self._pending_speed = 1.0
        self._speed_timer = QTimer(self)
        self._speed_timer.setSingleShot(True)
        self._speed_timer.timeout.connect(self._commit_speed)

        self.setWindowTitle("SlimBMS")
        self.setWindowIcon(build_icon())
        self.resize(1360, 880)

        self._update_manual = False
        self._build_canvas()
        self._build_toolbar()
        self._build_menu()
        self._register_shortcuts()
        self._load_shortcuts()
        self._load_layout_prefs()
        self._restore_geometry()   # reuse the last window size/position
        self._update_title()
        self._on_mode_changed("add")
        self._set_keymode(KEY_MODES[0])

        # Autosave a recovery copy while there are unsaved changes.
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(45000)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start()

        # Offer to recover an unsaved session, then check for updates.
        QTimer.singleShot(300, self._check_recovery)
        QTimer.singleShot(1500, lambda: self.check_for_updates(manual=False))

    # -- construction ------------------------------------------------------- #

    def _build_canvas(self) -> None:
        central = QWidget()
        hbox = QHBoxLayout(central)
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.setSpacing(0)

        # Left: header + scrolling chart.
        left = QWidget()
        vbox = QVBoxLayout(left)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        self.header = LaneHeader()
        vbox.addWidget(self.header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.view = ChartView(self.project)
        self.view.changed.connect(self._on_changed)
        self.view.zoom_step.connect(self._zoom_step)
        self.view.lane_zoom_step.connect(self._lane_zoom_step)
        self.view.mode_changed.connect(self._on_mode_changed)
        self.view.cursor_info.connect(self._show_cursor)
        self.view.scroll_h.connect(self._scroll_horizontal)
        self.scroll.setWidget(self.view)
        self.scroll.horizontalScrollBar().valueChanged.connect(self.header.set_x_offset)
        self.header.bgm_width_changed.connect(self._set_bgm_width)
        vbox.addWidget(self.scroll)
        hbox.addWidget(left, 1)

        # Right: song / grid sidebar.
        hbox.addWidget(self._build_sidebar())

        self.setCentralWidget(central)
        self._sync_sidebar()
        self._apply_grids()   # sync the view's grids to the sidebar defaults
        # Start scrolled to the bottom (song start).
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())

    def _build_sidebar(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("Sidebar")
        panel.setFixedWidth(300)
        outer = QVBoxLayout(panel)
        self._sidebar_panel = panel
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- Song info ------------------------------------------------------ #
        info = CollapsibleSection("곡 정보")
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.setContentsMargins(0, 0, 0, 0)

        self.sb_title = QLineEdit()
        self.sb_title.setPlaceholderText("곡 제목")
        self.sb_title.textEdited.connect(lambda t: self._set_meta("title", t))
        form.addRow("제목", self.sb_title)

        self.sb_artist = QLineEdit()
        self.sb_artist.setPlaceholderText("아티스트")
        self.sb_artist.textEdited.connect(lambda t: self._set_meta("artist", t))
        form.addRow("아티스트", self.sb_artist)

        self.sb_genre = QLineEdit()
        self.sb_genre.textEdited.connect(lambda t: self._set_meta("genre", t))
        form.addRow("장르", self.sb_genre)

        self.sb_bpm = NoWheelDoubleSpinBox()
        self.sb_bpm.setRange(1.0, 999.0)
        self.sb_bpm.setDecimals(2)
        self.sb_bpm.valueChanged.connect(lambda v: self._set_meta("bpm", v))
        form.addRow("BPM", self.sb_bpm)

        self.sb_level = NoWheelSpinBox()
        self.sb_level.setRange(0, 99)
        self.sb_level.valueChanged.connect(lambda v: self._set_meta("level", v))
        form.addRow("레벨", self.sb_level)
        info.add_layout(form)
        outer.addWidget(info)

        # -- Grid ----------------------------------------------------------- #
        # Two plain number boxes: the LEFT is the snap basis (cells per measure
        # that notes land on); the RIGHT is a lighter reference grid.
        grid = CollapsibleSection("격자")
        self.sb_g1 = self._grid_box(16, self._apply_grids)   # snap basis
        self.sb_g2 = self._grid_box(4, self._apply_grids)    # reference
        grow = QHBoxLayout()
        grow.setSpacing(12)
        grow.addWidget(self._labeled("스냅 격자", self.sb_g1))
        grow.addWidget(self._labeled("보조 격자", self.sb_g2))
        grow.addStretch(1)
        grid.add_layout(grow)
        self.sb_snap = QPushButton("격자 스냅 : 켜짐")
        self.sb_snap.setCheckable(True)
        self.sb_snap.setChecked(True)
        self.sb_snap.toggled.connect(self._toggle_snap)
        grid.add_widget(self.sb_snap)
        grid.add_widget(self._hint("Shift : 자유배치"))
        outer.addWidget(grid)

        # -- Zoom ----------------------------------------------------------- #
        zoom = CollapsibleSection("확대/축소")
        self.zoom_v = DragValue("↕", 0.25, 4.0, 0.25, 1.0)
        self.zoom_v.changed.connect(self._apply_zoom_v)
        zoom.add_widget(self.zoom_v)
        self.zoom_h = DragValue("↔", 0.5, 2.75, 0.25, 1.0)
        self.zoom_h.changed.connect(self._apply_zoom_h)
        zoom.add_widget(self.zoom_h)
        zoom.add_widget(self._hint("Ctrl+Wheel : ↕    Alt+Wheel : ↔"))
        outer.addWidget(zoom)

        # -- Tempo changes -------------------------------------------------- #
        tempo = CollapsibleSection("BPM 변화")
        self.bpm_measure = NoWheelSpinBox()
        self.bpm_measure.setRange(0, 9999)
        self.bpm_cell = NoWheelSpinBox()
        self.bpm_cell.setRange(0, 15)          # position within the measure, in 1/16
        self.bpm_value = NoWheelDoubleSpinBox()
        self.bpm_value.setRange(1.0, 999.0)
        self.bpm_value.setDecimals(2)
        self.bpm_value.setValue(120.0)
        brow = QHBoxLayout()
        brow.setSpacing(8)
        brow.addWidget(self._labeled("마디", self.bpm_measure))
        brow.addWidget(self._labeled("칸(1/16)", self.bpm_cell))
        brow.addWidget(self._labeled("BPM", self.bpm_value))
        tempo.add_layout(brow)
        add_bpm = QPushButton("추가 / 변경")
        add_bpm.clicked.connect(self._add_bpm_change)
        tempo.add_widget(add_bpm)
        self.bpm_list = QListWidget()
        self.bpm_list.setMaximumHeight(84)
        tempo.add_widget(self.bpm_list)
        del_bpm = QPushButton("선택 삭제")
        del_bpm.clicked.connect(self._remove_bpm_change)
        tempo.add_widget(del_bpm)
        tempo.add_widget(self._hint("곡 시작 템포는 위 '곡 정보'의 BPM"))
        outer.addWidget(tempo)

        # -- Audio ---------------------------------------------------------- #
        audio = CollapsibleSection("음원")
        audio.add_widget(self._hint("재생 속도"))
        self.speed = DragValue("×", 0.25, 2.0, 0.05, 1.0)
        self.speed.changed.connect(self._set_speed)
        audio.add_widget(self.speed)
        audio.add_widget(self._hint("음량"))
        self.volume = DragValue("♪", 0.0, 1.0, 0.05, 1.0)
        self.volume.changed.connect(self.audio.set_volume)
        audio.add_widget(self.volume)
        self.sb_bgm_btn = QPushButton("음원 파일 등록")
        self.sb_bgm_btn.clicked.connect(self.choose_bgm)
        audio.add_widget(self.sb_bgm_btn)
        self.sb_bgm_label = QLabel("(없음)")
        self.sb_bgm_label.setObjectName("Hint")
        self.sb_bgm_label.setWordWrap(True)
        audio.add_widget(self.sb_bgm_label)
        self.sb_wave = QPushButton("파형 표시 : 켜짐")
        self.sb_wave.setCheckable(True)
        self.sb_wave.setChecked(True)
        self.sb_wave.toggled.connect(self._toggle_waveform)
        audio.add_widget(self.sb_wave)
        outer.addWidget(audio)

        # -- Recording ------------------------------------------------------ #
        rec = CollapsibleSection("녹음")
        self.rec_offset = NoWheelSpinBox()
        self.rec_offset.setRange(-300, 300)
        self.rec_offset.setSingleStep(5)
        self.rec_offset.setSuffix(" ms")
        self.rec_offset.valueChanged.connect(self._update_record_offset)
        rec.add_widget(self._labeled("녹음 오프셋 (입력 지연 보정)", self.rec_offset))
        self.rec_countin = QPushButton("카운트인 : 꺼짐")
        self.rec_countin.setCheckable(True)
        self.rec_countin.toggled.connect(
            lambda on: self.rec_countin.setText("카운트인 : 켜짐" if on else "카운트인 : 꺼짐"))
        rec.add_widget(self.rec_countin)
        self.rec_metronome = QPushButton("메트로놈 : 꺼짐")
        self.rec_metronome.setCheckable(True)
        self.rec_metronome.toggled.connect(
            lambda on: self.rec_metronome.setText("메트로놈 : 켜짐" if on else "메트로놈 : 꺼짐"))
        rec.add_widget(self.rec_metronome)
        rec.add_widget(self._hint("오프셋을 늘리면 노트가 더 이르게 기록됨"))
        outer.addWidget(rec)

        outer.addStretch(1)
        # Scrollable so the (tall) sidebar never clips its lower sections.
        scroll = QScrollArea()
        scroll.setObjectName("SidebarScroll")
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(316)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return scroll

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Section")
        return label

    def _hint(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Hint")
        label.setWordWrap(True)
        return label

    def _labeled(self, caption: str, widget: QWidget) -> QWidget:
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        cap = QLabel(caption)
        cap.setObjectName("Hint")
        col.addWidget(cap)
        col.addWidget(widget)
        return box

    def _hline(self) -> QFrame:
        line = QFrame()
        line.setObjectName("HLine")
        line.setFrameShape(QFrame.HLine)
        line.setFixedHeight(1)
        return line

    def _grid_box(self, cells, cb):
        n = NoWheelSpinBox()
        n.setRange(1, 192)
        n.setValue(cells)
        n.setAlignment(Qt.AlignCenter)
        n.setFixedWidth(56)
        n.valueChanged.connect(cb)
        return n

    def _build_toolbar(self) -> None:
        tb = QToolBar("도구")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)   # drawn icons (uniform set)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)

        # File actions up front for quick access: open / save / import / export.
        for name, label, slot in (
            ("open", "열기", self.open_project),
            ("save", "저장", self.save_project),
            ("import", "가져오기", self.import_bms),
            ("export", "내보내기", self.export_bms),
        ):
            act = QAction(make_icon(name), label, self)
            act.triggered.connect(lambda checked=False, s=slot: s())
            tb.addAction(act)
        tb.addSeparator()

        # Transport buttons — one consistent drawn icon set.
        # Shortcuts here are the defaults; the user can remap them (편집 → 설정).
        self.start_action = QAction(make_icon("first"), "처음", self)
        self.start_action.triggered.connect(self.go_to_start)
        tb.addAction(self.start_action)

        self.back_action = QAction(make_icon("back"), "1초 뒤로", self)
        self.back_action.triggered.connect(lambda: self.seek_seconds(-1.0))
        tb.addAction(self.back_action)

        self._icon_play = make_icon("play")
        self._icon_pause = make_icon("pause")
        self.play_action = QAction(self._icon_play, "재생", self)
        self.play_action.triggered.connect(self.toggle_play)
        tb.addAction(self.play_action)
        play_btn = tb.widgetForAction(self.play_action)
        if play_btn is not None:
            play_btn.setObjectName("Primary")

        self.fwd_action = QAction(make_icon("forward"), "1초 앞으로", self)
        self.fwd_action.triggered.connect(lambda: self.seek_seconds(1.0))
        tb.addAction(self.fwd_action)

        stop_action = QAction(make_icon("stop"), "정지", self)
        stop_action.triggered.connect(self.stop_play)
        tb.addAction(stop_action)

        # [ / ] nudge the playback-speed gauge down / up (0.05 steps).
        self.speed_down_action = QAction(self)
        self.speed_down_action.triggered.connect(lambda: self.speed.step_by(-1))
        self.addAction(self.speed_down_action)
        self.speed_up_action = QAction(self)
        self.speed_up_action.triggered.connect(lambda: self.speed.step_by(1))
        self.addAction(self.speed_up_action)

        tb.addSeparator()
        self.add_mode_action = QAction("추가", self)
        self.add_mode_action.setCheckable(True)
        self.add_mode_action.setChecked(True)
        self.add_mode_action.triggered.connect(lambda: self._set_mode("add"))
        tb.addAction(self.add_mode_action)
        self.edit_mode_action = QAction("편집", self)
        self.edit_mode_action.setCheckable(True)
        self.edit_mode_action.triggered.connect(lambda: self._set_mode("edit"))
        tb.addAction(self.edit_mode_action)

        tb.addSeparator()
        # Two-option segmented toggle (4K / 6K); the checked one is highlighted.
        self._km_actions = {}
        km_group = QActionGroup(self)
        km_group.setExclusive(True)
        for km in KEY_MODES:
            act = QAction(f"{km}K", self)
            act.setCheckable(True)
            act.triggered.connect(lambda checked, m=km: self._set_keymode(m))
            km_group.addAction(act)
            tb.addAction(act)
            self._km_actions[km] = act

        # Mode / key-mode buttons keep their text labels (no icon).
        for act in (self.add_mode_action, self.edit_mode_action, *self._km_actions.values()):
            btn = tb.widgetForAction(act)
            if btn is not None:
                btn.setToolButtonStyle(Qt.ToolButtonTextOnly)

        # No hover tooltips on any toolbar button: an empty tip just falls back
        # to the button text, so swallow the ToolTip event on each button.
        for act in tb.actions():
            btn = tb.widgetForAction(act)
            if btn is not None:
                btn.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.ToolTip:
            return True   # suppress toolbar hover tooltips
        return super().eventFilter(obj, event)

    def _build_menu(self) -> None:
        m = self.menuBar()

        file_menu = m.addMenu("파일")
        self._add(file_menu, "새로 만들기", self.new_project, QKeySequence.New)
        self._add(file_menu, "프로젝트 열기", self.open_project, QKeySequence.Open)
        self._add(file_menu, "프로젝트 저장", self.save_project, QKeySequence.Save)
        self._add(file_menu, "프로젝트 다른 이름으로 저장", self.save_project_as,
                  QKeySequence("Ctrl+Shift+S"))
        file_menu.addSeparator()
        self._add(file_menu, "BMS 가져오기", self.import_bms)
        self._add(file_menu, "선택한 키로 .bms 내보내기", self.export_bms,
                  QKeySequence("Ctrl+E"))
        file_menu.addSeparator()
        self._add(file_menu, "종료", self.close)

        edit_menu = m.addMenu("편집")
        self.undo_action = self._add(edit_menu, "되돌리기", self.view.undo)
        self.redo_action = self._add(edit_menu, "다시하기", self.view.redo)
        edit_menu.addSeparator()
        self._add(edit_menu, "전체 선택\tCtrl+A", self.view.select_all)
        self.flip_action = self._add(edit_menu, "좌우 반전", self.view.flip_selection)
        edit_menu.addSeparator()
        self._add(edit_menu, "설정…", self.open_keybindings)

        song_menu = m.addMenu("곡")
        self._add(song_menu, "음원 파일 등록", self.choose_bgm)
        song_menu.addSeparator()
        self._add(song_menu, "검증 / 통계", self.show_stats)

        help_menu = m.addMenu("도움말")
        self._add(help_menu, "업데이트 확인", lambda: self.check_for_updates(manual=True))
        self._add(help_menu, "정보", self.show_about)

    def _add(self, menu, text, slot, shortcut=None) -> QAction:
        act = QAction(text, self)
        act.triggered.connect(slot)
        if shortcut is not None:
            act.setShortcut(shortcut)
        menu.addAction(act)
        return act

    # -- configurable shortcuts --------------------------------------------- #

    def _register_shortcuts(self) -> None:
        # key -> (action, label, default sequence). Order = dialog display order.
        self._key_actions = {
            "play": (self.play_action, "재생 / 일시정지", "Space"),
            "start": (self.start_action, "처음으로", "Home"),
            "back": (self.back_action, "1초 뒤로", "-"),
            "forward": (self.fwd_action, "1초 앞으로", "="),
            "add_mode": (self.add_mode_action, "추가 모드", "F3"),
            "edit_mode": (self.edit_mode_action, "편집 모드", "F2"),
            "speed_down": (self.speed_down_action, "재생 속도 감소", "["),
            "speed_up": (self.speed_up_action, "재생 속도 증가", "]"),
            "undo": (self.undo_action, "되돌리기", "Ctrl+Z"),
            "redo": (self.redo_action, "다시하기", "Ctrl+Y"),
            "flip": (self.flip_action, "좌우 반전", "`"),
        }

    def _load_shortcuts(self) -> None:
        s = QSettings("SlimBMS", "SlimBMS")
        for key, (act, _label, default) in self._key_actions.items():
            seq = s.value(f"shortcuts/{key}", default)
            act.setShortcut(QKeySequence(seq))

    def open_keybindings(self) -> None:
        dlg = KeybindingsDialog(self._key_actions, self)
        if dlg.exec():
            s = QSettings("SlimBMS", "SlimBMS")
            for key, seq in dlg.result_shortcuts().items():
                self._key_actions[key][0].setShortcut(QKeySequence(seq))
                s.setValue(f"shortcuts/{key}", seq)

    # -- state -------------------------------------------------------------- #

    def _on_changed(self) -> None:
        self._dirty = True
        self._autofit_measures()
        self._update_title()

    def _update_title(self) -> None:
        name = os.path.basename(self.project_path) if self.project_path else "제목 없음"
        star = "*" if self._dirty else ""
        song = self.project.title or "(무제)"
        self.setWindowTitle(f"SlimBMS — {song} — {name}{star}")

    def _reload_view(self) -> None:
        self.stop_play()
        self.view.project = self.project
        self.view.clear_history()   # a fresh project starts a fresh undo history
        self.view.refresh()
        self._sync_sidebar()
        self._update_title()

    # -- sidebar ------------------------------------------------------------ #

    def _sync_sidebar(self) -> None:
        """Populate the sidebar from the current project without re-triggering
        the change signals."""
        p = self.project
        for w, val in (
            (self.sb_title, p.title), (self.sb_artist, p.artist),
            (self.sb_genre, p.genre),
        ):
            w.blockSignals(True); w.setText(val); w.blockSignals(False)
        for w, val in ((self.sb_bpm, p.bpm), (self.sb_level, p.level)):
            w.blockSignals(True); w.setValue(val); w.blockSignals(False)
        self.sb_bgm_label.setText(p.bgm_file or "(없음)")
        self._refresh_bpm_list()

    def _set_meta(self, field: str, value) -> None:
        setattr(self.project, field, value)
        self._on_changed()

    def _autofit_measures(self) -> None:
        """Grow the timeline to cover all notes and the BGM length (never
        shrinks, so the view doesn't jump while editing)."""
        highest = 0
        for chart in self.project.charts.values():
            for n in chart:
                highest = max(highest, int(n.end_absolute))
        for n in self.project.bgm:
            highest = max(highest, n.measure)
        need = highest + 4  # keep a few empty measures above for placing notes
        if self.audio.duration > 0:
            mps = TimeMap(self.project).measures_per_second
            need = max(need, int(self.audio.duration * mps) + 2)
        need = max(16, need)
        if need > self.project.measures:
            self._resize_measures(need)

    def _resize_measures(self, need: int) -> None:
        # Preserve the chart position at the viewport centre across the resize.
        vbar = self.scroll.verticalScrollBar()
        vp_h = self.scroll.viewport().height()
        center_abs = self.view.absolute_at(vbar.value() + vp_h / 2)
        self.project.measures = need
        self.view.refresh()
        vbar.setValue(int(self.view.y_for(center_abs) - vp_h / 2))

    def _apply_grids(self) -> None:
        # Left box drives the snap grid; right box is the reference grid.
        self.view.set_grid_main(self.sb_g1.value())
        self.view.set_grid_sub(self.sb_g2.value())

    def _toggle_snap(self, on: bool) -> None:
        self.view.set_snap_on(on)
        self.sb_snap.setText("격자 스냅 : 켜짐" if on else "격자 스냅 : 꺼짐")

    def _toggle_waveform(self, on: bool) -> None:
        self.view.set_show_waveform(on)
        self.sb_wave.setText("파형 표시 : 켜짐" if on else "파형 표시 : 꺼짐")

    def _set_bgm_width(self, w: int) -> None:
        self.view.set_bgm_width(w)
        self.header.set_bgm_width(self.view.bgm_w)   # keep header in sync (clamped)
        QSettings("SlimBMS", "SlimBMS").setValue("sidebar/bgm_width", self.view.bgm_w)

    def _load_layout_prefs(self) -> None:
        raw = QSettings("SlimBMS", "SlimBMS").value("sidebar/bgm_width", 64)
        try:
            w = int(raw)
        except (TypeError, ValueError):
            w = 64
        self.view.set_bgm_width(w)
        self.header.set_bgm_width(self.view.bgm_w)

    def _restore_geometry(self) -> None:
        geo = QSettings("SlimBMS", "SlimBMS").value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)   # size + position + maximized state

    def _save_geometry(self) -> None:
        QSettings("SlimBMS", "SlimBMS").setValue("window/geometry", self.saveGeometry())

    # -- tempo changes ------------------------------------------------------ #

    def _add_bpm_change(self) -> None:
        from fractions import Fraction
        pos = Fraction(self.bpm_measure.value()) + Fraction(self.bpm_cell.value(), 16)
        self.project.bpm_changes[pos] = float(self.bpm_value.value())
        self.view.changed.emit()   # marks dirty + schedules an undo entry
        self.view.update()
        self._refresh_bpm_list()

    def _remove_bpm_change(self) -> None:
        item = self.bpm_list.currentItem()
        if item is None:
            return
        pos = item.data(Qt.UserRole)
        if pos in self.project.bpm_changes:
            del self.project.bpm_changes[pos]
            self.view.changed.emit()
            self.view.update()
            self._refresh_bpm_list()

    def _refresh_bpm_list(self) -> None:
        self.bpm_list.clear()
        for pos, bpm in sorted(self.project.bpm_changes.items()):
            measure = int(pos)
            frac = pos - measure
            cell = int(frac * 16)
            text = f"마디 {measure} · {cell}/16 → BPM {bpm:g}"
            self.bpm_list.addItem(text)
            self.bpm_list.item(self.bpm_list.count() - 1).setData(Qt.UserRole, pos)

    def _set_mode(self, mode: str) -> None:
        self.view.set_mode(mode)

    def _set_keymode(self, km: int) -> None:
        self.view.set_selected_km(km)
        self.header.set_selected_km(km)
        act = self._km_actions.get(km)
        if act is not None and not act.isChecked():
            act.setChecked(True)   # keep the toggle in sync when set in code

    def _on_mode_changed(self, mode: str) -> None:
        self.add_mode_action.setChecked(mode == "add")
        self.edit_mode_action.setChecked(mode == "edit")

    def _show_cursor(self, text: str) -> None:
        # The status bar shows only the live cursor coordinate now.
        if text:
            self.statusBar().showMessage(text)
        else:
            self.statusBar().clearMessage()

    # Pixel size at zoom factor 1.00 (matches the view's defaults).
    V_ZOOM_BASE = 150   # vertical: pixels per measure
    H_ZOOM_BASE = 30    # horizontal: pixels per lane (== layout.LANE_W)

    def _zoom_step(self, direction: int) -> None:
        # Ctrl+wheel nudges the vertical drag control, which applies the zoom.
        self.zoom_v.step_by(direction)

    def _lane_zoom_step(self, direction: int) -> None:
        # Alt+wheel nudges the horizontal drag control.
        self.zoom_h.step_by(direction)

    def _scroll_horizontal(self, delta: int) -> None:
        # Shift+wheel scrolls the chart left/right.
        hbar = self.scroll.horizontalScrollBar()
        hbar.setValue(hbar.value() - delta)

    def _apply_zoom_v(self, factor: float) -> None:
        # Vertical zoom while keeping the chart position at the viewport centre.
        vbar = self.scroll.verticalScrollBar()
        vp_h = self.scroll.viewport().height()
        center_abs = self.view.absolute_at(vbar.value() + vp_h / 2)
        self.view.set_zoom(int(round(self.V_ZOOM_BASE * factor)))
        vbar.setValue(int(self.view.y_for(center_abs) - vp_h / 2))

    def _apply_zoom_h(self, factor: float) -> None:
        # Horizontal (lane width) zoom, keeping the same relative scroll centre.
        hbar = self.scroll.horizontalScrollBar()
        vp_w = self.scroll.viewport().width()
        old_w = max(1, self.view._width)
        center_frac = (hbar.value() + vp_w / 2) / old_w
        self.view.set_lane_width(int(round(self.H_ZOOM_BASE * factor)))
        self.header.set_lane_width(self.view.lane_w)
        hbar.setValue(int(self.view._width * center_frac - vp_w / 2))

    # -- playback / preview ------------------------------------------------- #

    def _set_speed(self, factor: float) -> None:
        # Debounce: the gauge fires on every 0.05 step while dragging, but the
        # pitch-preserving stretch is heavy, so only rebuild once it settles.
        self._pending_speed = factor
        self._speed_timer.start(280)

    def _commit_speed(self) -> None:
        factor = self._pending_speed
        if abs(factor - self.audio.speed) < 1e-9:
            return
        was_playing = self.audio.playing
        pos = self.audio.position()
        # Stop audio + clock while we rebuild the stretch (pitch preserved).
        self._play_timer.stop()
        self.audio.stop()
        self.view.set_live(False)
        self.audio.set_speed(factor)
        if not self.audio.loaded or self.audio.stretch_ready():
            self._speed_ready(pos, was_playing)   # 1.0x / no audio: nothing to build
            return
        self.statusBar().showMessage(f"재생 속도 {factor:.2f}× 처리 중…")
        worker = self._speed_worker = _Worker(self.audio.build_stretch)
        worker.done.connect(lambda _=None: self._speed_ready(pos, was_playing))
        worker.failed.connect(lambda _msg: self._speed_ready(pos, was_playing))
        worker.start()

    def _speed_ready(self, pos: float, was_playing: bool) -> None:
        self.statusBar().clearMessage()
        self.audio.seek(pos)
        if was_playing:
            self._start_play()
        elif self._preview_active:
            self.view.set_playhead(self._ensure_timemap().chart_pos(pos))

    def _ensure_timemap(self) -> TimeMap:
        # Rebuilt on demand so BPM / BGM-offset edits always take effect.
        self._timemap = TimeMap(self.project)
        return self._timemap

    def _current_chart_pos(self) -> float:
        return self._ensure_timemap().chart_pos(self.audio.position())

    def _update_record_offset(self) -> None:
        # Convert the ms offset to measures at the current tempo for recording.
        mps = TimeMap(self.project).measures_per_second
        self.view.record_offset_measures = (self.rec_offset.value() / 1000.0) * mps

    def toggle_play(self) -> None:
        if self._counting_in:
            self._cancel_countin()
            self.stop_play()
        elif self.audio.playing:
            self._pause_play()
        else:
            self._start_play()

    def _start_play(self) -> None:
        # A count-in plays four beats before a fresh start (not when resuming).
        if self.rec_countin.isChecked() and not self.audio.paused and not self._counting_in:
            self._begin_countin()
            return
        self._do_start_play()

    def _do_start_play(self) -> None:
        self._ensure_timemap()
        self._preview_active = True
        self._update_record_offset()
        self._last_beat = -1
        # Un-pause in place when we were paused (no re-seek, so the audio stays
        # in sync); otherwise start/seek to the current position.
        if self.audio.paused:
            self.audio.resume()
        else:
            self.audio.play(self.audio.position())
        self.play_action.setIcon(self._icon_pause)
        self._play_timer.start()
        self.view.set_live(True)
        self.view.setFocus()   # so recording keys reach the canvas

    def _begin_countin(self) -> None:
        self._counting_in = True
        self._countin_left = 3          # first beat plays now, three more follow
        self.play_action.setIcon(self._icon_pause)
        self.statusBar().showMessage("카운트인…")
        self.audio.play_click(accent=True)
        interval = int(60000.0 / max(1.0, self.project.bpm))
        self._countin_timer.setInterval(interval)
        self._countin_timer.start()

    def _countin_tick(self) -> None:
        if not self._counting_in:
            self._countin_timer.stop()
            return
        if self._countin_left <= 0:
            self._countin_timer.stop()
            self._counting_in = False
            self.statusBar().clearMessage()
            self._do_start_play()
            return
        self.audio.play_click(accent=False)
        self._countin_left -= 1

    def _cancel_countin(self) -> None:
        self._countin_timer.stop()
        self._counting_in = False
        self.statusBar().clearMessage()

    def _pause_play(self) -> None:
        self.audio.pause()
        self._play_timer.stop()
        self.play_action.setIcon(self._icon_play)
        self.view.set_live(False)
        self._on_mode_changed(self.view.mode)
        # Keep the playhead visible where we paused so seeking has a reference.
        self.view.set_playhead(self._current_chart_pos())

    def stop_play(self) -> None:
        self._cancel_countin()
        self.audio.stop()
        self._play_timer.stop()
        self.play_action.setIcon(self._icon_play)
        self._preview_active = False
        self.view.set_live(False)
        self._on_mode_changed(self.view.mode)
        self.view.set_playhead(None)

    def go_to_start(self) -> None:
        self._seek_audio(0.0)

    def seek_seconds(self, d_seconds: float) -> None:
        self._seek_audio(self.audio.position() + d_seconds)

    def _seek_audio(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        self.audio.seek(seconds)
        self._preview_active = True
        chart_pos = self._ensure_timemap().chart_pos(seconds)
        self.view.set_playhead(chart_pos)
        self._follow_playhead(chart_pos)

    def _on_play_tick(self) -> None:
        if self._timemap is None:
            return
        pos = self.audio.position()
        chart_pos = self._timemap.chart_pos(pos)
        # Stop at the end of the timeline (or audio).
        if chart_pos >= self.project.measures or (
            self.audio.duration and pos >= self.audio.duration
        ):
            self.stop_play()
            return
        if self.rec_metronome.isChecked():
            beat = int(chart_pos * 4)   # 4 beats per measure (4/4)
            if beat > self._last_beat:
                self.audio.play_click(accent=(beat % 4 == 0))
                self._last_beat = beat
        self.view.set_playhead(chart_pos)
        self._follow_playhead(chart_pos)

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

    # -- autosave / recovery ------------------------------------------------ #

    def _recovery_path(self) -> str:
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        if not base:
            import tempfile
            base = os.path.join(tempfile.gettempdir(), "SlimBMS")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "recovery.slbms")

    def _autosave(self) -> None:
        if not self._dirty:
            return
        try:
            bms_io.save_project(self.project, self._recovery_path())
        except Exception:  # noqa: BLE001 — best effort, never interrupt editing
            pass

    def _clear_recovery(self) -> None:
        try:
            os.remove(self._recovery_path())
        except OSError:
            pass

    def _check_recovery(self) -> None:
        path = self._recovery_path()
        if not os.path.exists(path):
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("작업 복구")
        box.setText("저장하지 않고 종료된 작업이 있습니다.\n복구할까요?")
        restore = box.addButton("복구", QMessageBox.AcceptRole)
        box.addButton("무시", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is restore:
            try:
                self.project = bms_io.load_project(path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "복구 실패", str(exc))
                return
            self.project_path = None
            self._dirty = True
            self._reload_view()
        else:
            self._clear_recovery()

    # -- validation / stats ------------------------------------------------- #

    def _validate(self):
        p = self.project
        warns = []
        if not p.bgm:
            warns.append("BGM 시작 마커가 없음 (내보내기 타이밍 기준 없음)")
        if self.view._conflicts:
            warns.append(f"겹치는 노트 {len(self.view._conflicts)}개 (빨간 표시)")
        if p.bgm:
            t0 = min(n.absolute for n in p.bgm)
            early = sum(1 for km in KEY_MODES for n in p.charts[km] if n.absolute < t0)
            if early:
                warns.append(f"BGM 시작보다 앞선 노트 {early}개")
        for km in KEY_MODES:
            if not p.charts[km]:
                warns.append(f"{km}K 채보가 비어 있음")
        return warns

    def show_stats(self) -> None:
        p = self.project
        lines = [
            f"마디 수 : {p.measures}",
            f"기본 BPM : {p.bpm:g}    BPM 변화 : {len(p.bpm_changes)}개",
            f"BGM 마커 : {len(p.bgm)}개",
            "",
        ]
        for km in KEY_MODES:
            chart = p.charts[km]
            longs = sum(1 for n in chart if n.is_long)
            lines.append(f"{km}K — 노트 {len(chart)}개 (롱 {longs})")
        if p.charts[IMPORT_MODE]:
            lines.append(f"LOAD — 노트 {len(p.charts[IMPORT_MODE])}개")
        lines.append("")
        warns = self._validate()
        if warns:
            lines.append("⚠ 경고")
            lines += [f"  · {w}" for w in warns]
        else:
            lines.append("문제 없음 ✓")
        QMessageBox.information(self, "검증 / 통계", "\n".join(lines))

    # -- file actions ------------------------------------------------------- #

    def new_project(self) -> None:
        if not self._confirm_discard():
            return
        self.project = Project()
        self.project_path = None
        self._dirty = False
        self._clear_recovery()
        self._reload_view()

    def open_project(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "프로젝트 열기", self._dir_for("open"),
            "SlimBMS 프로젝트 (*.slbms);;모든 파일 (*)")
        if not path:
            return
        try:
            self.project = bms_io.load_project(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "열기 실패", str(exc))
            return
        self._remember_dir("open", path)
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
        self._clear_recovery()   # a real save supersedes the recovery copy
        self._update_title()

    def save_project_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "프로젝트 저장", self._dialog_path("save", self._suggest_name(".slbms")),
            "SlimBMS 프로젝트 (*.slbms)")
        if not path:
            return
        if not path.lower().endswith(".slbms"):
            path += ".slbms"
        self._remember_dir("save", path)
        self.project_path = path
        self.save_project()

    def import_bms(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "BMS 가져오기", self._dir_for("import"),
            "BMS 채보 (*.bms *.bme *.bml);;모든 파일 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                self.project = bms_io.parse_bms(fh.read())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "가져오기 실패", str(exc))
            return
        self._remember_dir("import", path)
        self.project_path = None
        self._dirty = True
        self._reload_view()
        # Highlight the key mode the notes landed in, so it matches the view.
        for km in KEY_MODES:
            if self.project.charts[km]:
                self._set_keymode(km)
                break

    def export_bms(self) -> None:
        km = self.view.selected_km
        if not self.project.bgm:
            QMessageBox.warning(
                self, "BGM 없음",
                "BGM 출력 시작 타이밍이 없습니다. BGM 레인에 시작 지점을 먼저 찍어주세요.")
        default = self._dialog_path("export", self._suggest_name(f"_{km}k.bms"))
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
        self._remember_dir("export", path)
        QMessageBox.information(
            self, "내보내기 완료",
            f"{km}K 채보를 저장했습니다:\n{path}\n\n노트 수: {self.project.note_count(km)}")

    # -- song actions ------------------------------------------------------- #

    def choose_bgm(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "음원 파일 선택", self._dir_for("bgm"),
            "오디오 (*.wav *.ogg *.mp3 *.flac);;모든 파일 (*)")
        if not path:
            return
        self._remember_dir("bgm", path)
        self.project.bgm_file = os.path.basename(path)
        self._bgm_path = path
        loaded = self.audio.load(path)
        self.sb_bgm_label.setText(self.project.bgm_file)
        # Waveform for the editor background.
        peaks, bps = self.audio.waveform_peaks()
        self.view.set_waveform(peaks, bps)
        # Pre-build the stretch for the current speed so the first play at a
        # non-1.0x speed doesn't stall.
        if loaded and not self.audio.stretch_ready():
            worker = self._bgm_speed_worker = _Worker(self.audio.build_stretch)
            worker.start()
        self._on_changed()
        note = "" if loaded else "\n\n(이 환경에서는 오디오 장치가 없어 재생 미리보기는 실제 PC에서만 됩니다.)"
        QMessageBox.information(
            self, "BGM 설정됨",
            f"BGM 파일명: {self.project.bgm_file}\n"
            "이 파일을 .bms와 같은 폴더에 두어야 게임에서 재생됩니다." + note)

    # -- updates ------------------------------------------------------------ #

    def show_about(self) -> None:
        QMessageBox.information(
            self, "SlimBMS 정보",
            f"SlimBMS v{__version__}\n무키음 4K/6K BMS 채보 에디터")

    def check_for_updates(self, manual: bool) -> None:
        self._update_manual = manual
        worker = self._update_checker = _Worker(updater.check_latest)
        worker.done.connect(self._on_update_checked)
        worker.failed.connect(lambda msg: self._on_update_checked(None))
        worker.start()

    def _on_update_checked(self, info) -> None:
        manual = self._update_manual
        self._update_manual = False
        if info is None:
            if manual:
                QMessageBox.warning(self, "업데이트 확인",
                                    "업데이트 정보를 가져오지 못했습니다. 인터넷 연결을 확인하세요.")
            return
        if not updater.is_newer(info.tag):
            if manual:
                QMessageBox.information(self, "업데이트 확인",
                                        f"이미 최신 버전입니다 (v{__version__}).")
            return

        # A newer version exists: apply it right away, no confirmation prompt or
        # changelog — just download and restart.
        self._begin_update(info)

    def _begin_update(self, info) -> None:
        if not info.download_url:
            QMessageBox.warning(self, "업데이트", "릴리스에서 설치 파일(.zip)을 찾지 못했습니다.")
            return
        if not updater.is_frozen():
            QMessageBox.information(
                self, "업데이트",
                f"새 버전 {info.tag} 이 있습니다.\n"
                "지금은 소스 코드로 실행 중이라 자동 적용은 설치 버전에서만 됩니다.\n"
                "GitHub 릴리스 페이지에서 받을 수 있습니다.")
            return

        self._progress = QProgressDialog("업데이트를 내려받는 중…", "취소", 0, 0, self)
        self._progress.setWindowTitle("업데이트")
        self._progress.setWindowModality(Qt.ApplicationModal)
        self._progress.setMinimumDuration(0)
        self._progress.setCancelButton(None)  # download can't be cancelled midway
        self._progress.show()

        worker = self._download_worker = _Worker(
            lambda: updater.download_update(info.download_url))
        worker.done.connect(self._on_update_downloaded)
        worker.failed.connect(self._on_update_failed)
        worker.start()

    def _on_update_downloaded(self, result) -> None:
        self._progress.close()
        new_app_dir, tmp_root = result
        # Restart straight into the new version without a further prompt.
        self.stop_play()
        updater.swap_and_restart(new_app_dir, tmp_root)  # exits the process

    def _on_update_failed(self, msg: str) -> None:
        self._progress.close()
        QMessageBox.critical(self, "업데이트 실패", f"업데이트 중 오류가 발생했습니다:\n{msg}")

    # -- helpers ------------------------------------------------------------ #

    def _dir_for(self, key: str) -> str:
        """The folder last used for the ``key`` operation (open/save/import/
        export/bgm), remembered separately and persisted across sessions."""
        d = self._last_dirs.get(key)
        if d is None:
            d = QSettings("SlimBMS", "SlimBMS").value(f"paths/{key}", "") or ""
            self._last_dirs[key] = d
        return d

    def _remember_dir(self, key: str, path: str) -> None:
        d = os.path.dirname(path)
        self._last_dirs[key] = d
        QSettings("SlimBMS", "SlimBMS").setValue(f"paths/{key}", d)

    def _dialog_path(self, key: str, name: str) -> str:
        """Default path for a save dialog: the ``key`` operation's last folder +
        ``name`` so it opens where that operation last wrote."""
        d = self._dir_for(key)
        return os.path.join(d, name) if d else name

    def _suggest_name(self, suffix: str) -> str:
        base = self.project.title.strip() or "untitled"
        base = "".join(c for c in base if c.isalnum() or c in " _-").strip() or "untitled"
        return base + suffix

    def _confirm_discard(self) -> bool:
        """When there are unsaved changes, ask whether to save, discard, or
        cancel. Returns True to proceed (discarded, or saved successfully),
        False to abort the current action."""
        if not self._dirty:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("저장하지 않은 변경")
        box.setText("저장하지 않은 변경사항이 있습니다.\n저장할까요?")
        save_btn = box.addButton("저장", QMessageBox.AcceptRole)
        discard_btn = box.addButton("저장 안 함", QMessageBox.DestructiveRole)
        cancel_btn = box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(save_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            return False
        if clicked is discard_btn:
            return True
        # Save: proceed only if it actually completed (a cancelled Save As, or a
        # save error, leaves the project dirty).
        self.save_project()
        return not self._dirty

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._confirm_discard():
            self._save_geometry()    # remember the window size/position
            self._clear_recovery()   # clean exit — no crash to recover from
            event.accept()
        else:
            event.ignore()
