"""Main application window: menus, toolbar and the scrolling chart canvas."""

from __future__ import annotations

import os
from typing import Dict, Optional

from PySide6.QtCore import (
    QEvent,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
)
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, bms_io, updater
from ..audio import AudioPlayer
from ..model import IMPORT_MODE, KEY_MODES, Project
from ..timing import TimeMap
from .appicon import build_icon
from .chart_view import RECORD_KEYS, ChartView, LaneHeader
from .dialogs import HelpDialog, KeybindingsDialog
from .palette import DANGER
from .playback import PlaybackController
from .toolbar_icons import make_icon
from .widgets import (
    MARKER_RIGHT_ROLE,
    CollapsibleSection,
    DragValue,
    MarkerListDelegate,
    NoWheelDoubleSpinBox,
    NoWheelSpinBox,
)
from .worker import _Worker

# Org/app pair for QSettings, in one place so the string literals aren't
# duplicated across every persisted-setting call site.
_SETTINGS_ORG = "SlimBMS"
_SETTINGS_APP = "SlimBMS"

# Playback speeds pre-built in the background when a BGM loads, so selecting one
# later is instant. The common practice range is slow (0.5–0.9); 1.0x needs no
# build. Faster-than-1 speeds are rarely used and stay build-on-demand.
PRECOMPUTE_SPEEDS = (0.5, 0.6, 0.7, 0.8, 0.9)


def _settings() -> QSettings:
    """The app's persistent settings store (window geometry, last folders, …)."""
    return QSettings(_SETTINGS_ORG, _SETTINGS_APP)


class _MarkerEdit:
    """Add / edit workflow shared by the BPM, 정지 and 노트 속도 marker lists.

    Normal mode: [추가] adds from the inputs (overwriting a marker at the same
    position). [수정] enters edit mode for the selected list row — the buttons
    become [완료]/[취소] (colour-coded) — and [완료] replaces that row (delete +
    add) with the current inputs, so changing a marker's position moves it
    instead of leaving a duplicate."""

    def __init__(self, win, add_btn, edit_btn, del_btn, list_w,
                 add_fn, load_fn, commit_fn):
        self.win = win
        self.add_btn, self.edit_btn, self.del_btn = add_btn, edit_btn, del_btn
        self.list = list_w
        self.add_fn, self.load_fn, self.commit_fn = add_fn, load_fn, commit_fn
        self.target = None
        add_btn.clicked.connect(self._primary)
        edit_btn.clicked.connect(self._secondary)
        list_w.itemDoubleClicked.connect(
            lambda it: self.load_fn(it.data(Qt.UserRole)))

    @staticmethod
    def _restyle(btn, name):
        btn.setObjectName(name)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _primary(self) -> None:        # 추가 / 완료
        if self.target is None:
            self.add_fn()
        elif self.commit_fn(self.target):
            self._exit()

    def _secondary(self) -> None:      # 수정 / 취소
        if self.target is not None:    # 취소
            self._exit()
            return
        item = self.list.currentItem()
        if item is None:
            self.win.statusBar().showMessage("먼저 리스트에서 항목을 선택하세요.", 2500)
            return
        self.target = item.data(Qt.UserRole)
        self.load_fn(self.target)
        self.add_btn.setText("완료");  self._restyle(self.add_btn, "Confirm")
        self.edit_btn.setText("취소"); self._restyle(self.edit_btn, "Cancel")
        self.del_btn.setEnabled(False)

    def _exit(self) -> None:
        self.target = None
        self.add_btn.setText("추가");  self._restyle(self.add_btn, "")
        self.edit_btn.setText("수정"); self._restyle(self.edit_btn, "")
        self.del_btn.setEnabled(True)




class MainWindow(QMainWindow):
    def __init__(self, project: Optional[Project] = None):
        super().__init__()
        self.project = project or Project()
        self.project_path: Optional[str] = None
        self._dirty = False
        self._last_dirs = {}  # per-operation folder used since this project opened

        # Playback / preview (transport handled by PlaybackController).
        self.audio = AudioPlayer()
        self._bgm_path: Optional[str] = None   # full path for playback
        self.playback = PlaybackController(self)

        self.setWindowTitle("SlimBMS")
        self.setWindowIcon(build_icon())
        self.resize(1360, 880)

        self._update_manual = False
        self._build_canvas()
        self._build_toolbar()
        self._build_menu()
        self._register_shortcuts()
        self._load_shortcuts()
        self._load_record_keys()
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
        self.view.seek_requested.connect(self._seek_to_chart)
        self.view.overlap_warning.connect(self._warn_overlap)
        self.view.markers_changed.connect(self._refresh_marker_lists)
        self.view.focus_requested.connect(self._focus_chart_range)
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
        # Apply the sidebar's default zoom / volume to the view & audio (the
        # widgets don't emit on construction, so push their defaults through).
        self._apply_zoom_v(self.zoom_v.value())
        self._apply_zoom_h(self.zoom_h.value())
        self.audio.set_volume(self.volume.value())
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

        # -- Images (STAGEFILE / BANNER / BACKBMP) -------------------------- #
        images = CollapsibleSection("이미지")
        self._image_buttons: Dict[str, QPushButton] = {}
        self._image_names: Dict[str, QLabel] = {}
        for field, caption in (
            ("stagefile", "대표 이미지"),
            ("banner", "배너 이미지"),
            ("backbmp", "배경 이미지"),
        ):
            images.add_widget(self._image_row(field, caption))
        outer.addWidget(images)

        # -- Grid ----------------------------------------------------------- #
        # Two plain number boxes: the LEFT is the snap basis (cells per measure
        # that notes land on); the RIGHT is a lighter reference grid.
        grid = CollapsibleSection("격자")
        self.sb_g1 = self._grid_box(32, self._apply_grids)   # snap basis
        self.sb_g2 = self._grid_box(8, self._apply_grids)    # reference
        self.sb_snap = QPushButton("격자 스냅 : 켜짐")
        self.sb_snap.setCheckable(True)
        self.sb_snap.setChecked(True)
        self.sb_snap.toggled.connect(self._toggle_snap)
        grow = QHBoxLayout()
        grow.setSpacing(12)
        grow.addWidget(self._labeled("스냅 격자", self.sb_g1))
        grow.addWidget(self._labeled("보조 격자", self.sb_g2))
        # The snap toggle sits in the empty space to the right of the two boxes.
        grow.addWidget(self._labeled(" ", self.sb_snap), 1)
        grid.add_layout(grow)
        grid.add_widget(self._hint("Shift : 자유배치"))
        outer.addWidget(grid)

        # -- Zoom ----------------------------------------------------------- #
        zoom = CollapsibleSection("확대/축소")
        self.zoom_v = DragValue("↕", 0.25, 4.0, 0.25, 2.0)
        self.zoom_v.changed.connect(self._apply_zoom_v)
        zoom.add_widget(self.zoom_v)
        self.zoom_h = DragValue("↔", 0.5, 2.75, 0.25, 1.25)
        self.zoom_h.changed.connect(self._apply_zoom_h)
        zoom.add_widget(self.zoom_h)
        zoom.add_widget(self._hint("Ctrl+Wheel : ↕    Alt+Wheel : ↔"))
        outer.addWidget(zoom)

        # -- Tempo changes -------------------------------------------------- #
        tempo = CollapsibleSection("BPM 변화")
        # Start point (also used on its own for a single instant tempo change).
        self.bpm_measure = NoWheelSpinBox()
        self.bpm_measure.setRange(0, 9999)
        self.bpm_cell = NoWheelSpinBox()
        # Position within the measure, counted in snap-grid cells: one measure is
        # split into `스냅 격자` cells, so the max follows the snap-grid setting.
        self.bpm_cell.setRange(0, self.sb_g1.value())
        self.bpm_value = NoWheelDoubleSpinBox()
        self.bpm_value.setRange(1.0, 999.0)
        self.bpm_value.setDecimals(2)
        self.bpm_value.setValue(120.0)
        brow = QHBoxLayout()
        brow.setSpacing(8)
        brow.addWidget(self._labeled("마디", self.bpm_measure))
        brow.addWidget(self._labeled("칸", self.bpm_cell))
        brow.addWidget(self._labeled("BPM", self.bpm_value))
        tempo.add_layout(brow)
        self.bpm_list = QListWidget()
        self.bpm_list.setItemDelegate(MarkerListDelegate(self.bpm_list))
        self.bpm_list.setMaximumHeight(84)
        self._bpm_edit = self._marker_controls(
            tempo, self.bpm_list, self._add_bpm_change, self._load_bpm,
            self._commit_bpm, self._remove_bpm_change)
        outer.addWidget(tempo)

        # -- STOP (freeze) gimmick ------------------------------------------ #
        stopsec = CollapsibleSection("정지")
        self.stop_measure = NoWheelSpinBox()
        self.stop_measure.setRange(0, 9999)
        self.stop_cell = NoWheelSpinBox()
        self.stop_cell.setRange(0, self.sb_g1.value())
        # Freeze length in beats (1 beat = a quarter note); the scroll holds this
        # long while the audio keeps playing.
        self.stop_beats = NoWheelDoubleSpinBox()
        self.stop_beats.setRange(0.05, 64.0)
        self.stop_beats.setDecimals(2)
        self.stop_beats.setSingleStep(0.25)
        self.stop_beats.setValue(1.0)
        srow = QHBoxLayout()
        srow.setSpacing(8)
        srow.addWidget(self._labeled("마디", self.stop_measure))
        srow.addWidget(self._labeled("칸", self.stop_cell))
        srow.addWidget(self._labeled("박자", self.stop_beats))
        stopsec.add_layout(srow)
        self.stop_list = QListWidget()
        self.stop_list.setItemDelegate(MarkerListDelegate(self.stop_list))
        self.stop_list.setMaximumHeight(84)
        self._stop_edit = self._marker_controls(
            stopsec, self.stop_list, self._add_stop, self._load_stop,
            self._commit_stop, self._remove_stop)
        outer.addWidget(stopsec)

        # -- 노트 속도 (scroll velocity) ------------------------------------ #
        # One tool: a start point steps the speed (순간). Tick "끝점 지정" to add
        # an end point and it becomes a smooth ramp (선형). Both compile to the
        # same #SCROLL steps on export — the checkbox is just "one point vs two".
        scrollsec = CollapsibleSection("노트 속도")

        def _mk_value(default):
            v = NoWheelDoubleSpinBox()
            v.setRange(-64.0, 64.0)
            v.setDecimals(2)
            v.setSingleStep(0.25)
            v.setValue(default)
            return v

        self.scroll_measure = NoWheelSpinBox(); self.scroll_measure.setRange(0, 9999)
        self.scroll_cell = NoWheelSpinBox(); self.scroll_cell.setRange(0, self.sb_g1.value())
        self.scroll_value = _mk_value(2.0)
        self.scroll_measure2 = NoWheelSpinBox(); self.scroll_measure2.setRange(0, 9999)
        self.scroll_cell2 = NoWheelSpinBox(); self.scroll_cell2.setRange(0, self.sb_g1.value())
        self.scroll_value2 = _mk_value(1.0)

        def _cap(text):
            lab = self._hint(text); lab.setAlignment(Qt.AlignLeft | Qt.AlignVCenter); return lab
        sgrid = QGridLayout()
        sgrid.setContentsMargins(0, 0, 0, 0)
        sgrid.setHorizontalSpacing(8); sgrid.setVerticalSpacing(4)
        sgrid.addWidget(_cap("마디"), 0, 1)
        sgrid.addWidget(_cap("칸"), 0, 2)
        sgrid.addWidget(_cap("배속"), 0, 3)
        # Always a start → end range (two points), so both rows show at once.
        start_tag = self._hint("시작"); start_tag.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        sgrid.addWidget(start_tag, 1, 0)
        sgrid.addWidget(self.scroll_measure, 1, 1)
        sgrid.addWidget(self.scroll_cell, 1, 2)
        sgrid.addWidget(self.scroll_value, 1, 3)
        end_tag = self._hint("끝"); end_tag.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        sgrid.addWidget(end_tag, 2, 0)
        sgrid.addWidget(self.scroll_measure2, 2, 1)
        sgrid.addWidget(self.scroll_cell2, 2, 2)
        sgrid.addWidget(self.scroll_value2, 2, 3)
        for col, stretch in ((0, 0), (1, 1), (2, 1), (3, 1)):
            sgrid.setColumnStretch(col, stretch)
        scrollsec.add_layout(sgrid)
        # The end row is what decides 순간 vs 선형, so let it show that: it greys
        # out while it holds no end point past the start (= a single step) and
        # lights up the moment one is typed (= a ramp). Still editable either way
        # — greying it out is the invitation to fill it in, not a lockout.
        self._scroll_end_row = (end_tag, self.scroll_measure2,
                                self.scroll_cell2, self.scroll_value2)
        for box in (self.scroll_measure, self.scroll_cell,
                    self.scroll_measure2, self.scroll_cell2):
            box.valueChanged.connect(self._sync_scroll_end_row)
        self.sb_g1.valueChanged.connect(self._sync_scroll_end_row)
        self._sync_scroll_end_row()
        self.scroll_list = QListWidget()
        self.scroll_list.setItemDelegate(MarkerListDelegate(self.scroll_list))
        self.scroll_list.setMaximumHeight(96)
        self._scroll_edit = self._marker_controls(
            scrollsec, self.scroll_list, self._add_scroll, self._load_scroll,
            self._commit_scroll, self._remove_scroll)
        outer.addWidget(scrollsec)

        # -- Audio ---------------------------------------------------------- #
        audio = CollapsibleSection("음원")
        audio.add_widget(self._hint("재생 속도"))
        # 0.1 steps so every reachable speed lands on the pre-built cache grid
        # (see PRECOMPUTE_SPEEDS); speeds above 1.0 aren't used, so cap at 1.0.
        self.speed = DragValue("×", 0.5, 1.0, 0.1, 1.0)
        self.speed.changed.connect(self._set_speed)
        audio.add_widget(self.speed)
        audio.add_widget(self._hint("음량"))
        self.volume = DragValue("♪", 0.0, 1.0, 0.05, 0.3)
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
        rec = CollapsibleSection("실시간 채보")
        self.rec_offset = NoWheelSpinBox()
        self.rec_offset.setRange(-300, 300)    # latency comp, either direction
        self.rec_offset.setSingleStep(5)
        self.rec_offset.setSuffix(" ms")
        self.rec_offset.valueChanged.connect(self._update_record_offset)
        rec.add_widget(self._labeled("입력 지연 보정", self.rec_offset))
        rec.add_widget(self._hint("보정값을 늘리면 노트가 더 빠르게 기록됨"))
        self.rec_countin = QPushButton("카운트인 : 꺼짐")
        self.rec_countin.setCheckable(True)
        self.rec_countin.toggled.connect(
            lambda on: self.rec_countin.setText("카운트인 : 켜짐" if on else "카운트인 : 꺼짐"))
        self.rec_metronome = QPushButton("메트로놈 : 꺼짐")
        self.rec_metronome.setCheckable(True)
        self.rec_metronome.toggled.connect(
            lambda on: self.rec_metronome.setText("메트로놈 : 켜짐" if on else "메트로놈 : 꺼짐"))
        rrow = QHBoxLayout()
        rrow.setSpacing(6)
        rrow.addWidget(self.rec_countin)
        rrow.addWidget(self.rec_metronome)
        rec.add_layout(rrow)
        outer.addWidget(rec)

        # Keyed by title so the collapsed/expanded state can be saved & restored.
        self._sections = {
            s.header.text(): s
            for s in (info, images, grid, zoom, tempo, stopsec, scrollsec, audio, rec)
        }

        outer.addStretch(1)
        # Scrollable so the (tall) sidebar never clips its lower sections.
        scroll = QScrollArea()
        scroll.setObjectName("SidebarScroll")
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(316)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Keep the vertical scrollbar's gutter reserved at all times. Otherwise
        # a collapse animation crosses the "fits / doesn't fit" threshold and the
        # scrollbar flickers in and out, jittering the panel width every frame.
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
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
        # A plain wrapper QWidget otherwise inherits the app-wide dark QWidget
        # background (a near-black box behind the caption). Scope it transparent
        # so the caption sits flat on the panel, just like a standalone hint.
        box.setObjectName("FlatRow")
        box.setStyleSheet("QWidget#FlatRow { background: transparent; }")
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        cap = QLabel(caption)
        cap.setObjectName("Hint")
        col.addWidget(cap)
        col.addWidget(widget)
        return box

    def _marker_controls(self, section, list_w, add_fn, load_fn, commit_fn,
                         remove_fn):
        """Build a marker section's [추가][수정][삭제] button row (delete on the
        right, red) above a roomy list, and wire an :class:`_MarkerEdit`
        controller (returned — keep a reference)."""
        add_btn = QPushButton("추가")
        edit_btn = QPushButton("수정")
        del_btn = QPushButton("삭제")
        del_btn.setObjectName("Danger")
        del_btn.clicked.connect(remove_fn)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(add_btn)
        row.addWidget(edit_btn)
        row.addWidget(del_btn)
        section.add_layout(row)
        list_w.setMaximumHeight(200)                # roomier — many markers fit
        section.add_widget(list_w)
        return _MarkerEdit(self, add_btn, edit_btn, del_btn, list_w,
                           add_fn, load_fn, commit_fn)

    def _image_row(self, field: str, caption: str) -> QWidget:
        """One horizontal row for a BMS image header (STAGEFILE / BANNER /
        BACKBMP): the caption, the chosen filename (clipped to whatever room is
        left — only the front is needed), and a button that picks a file or, once
        one is set, turns into a red '취소' that clears it."""
        box = QWidget()
        box.setObjectName("FlatRow")
        box.setStyleSheet("QWidget#FlatRow { background: transparent; }")
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        cap = QLabel(caption)
        cap.setObjectName("Hint")
        row.addWidget(cap)
        name = QLabel()
        name.setObjectName("Hint")
        name.setWordWrap(False)
        # Ignore the label's own size hint so a long filename can't push the
        # button — it just fills the middle and clips (front-aligned).
        name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(name, 1)
        self._image_names[field] = name
        btn = QPushButton()
        btn.setFixedWidth(76)
        btn.clicked.connect(lambda: self._toggle_image(field, caption))
        row.addWidget(btn)
        self._image_buttons[field] = btn
        self._refresh_image_button(field)
        return box

    # Same 1px border width as a normal button so swapping in/out doesn't
    # nudge the button's size — only the colour changes.
    _IMAGE_CANCEL_QSS = (
        "QPushButton { background: %s; color: #2a0d12;"
        " border: 1px solid %s; border-radius: 6px; padding: 7px 12px; }"
        "QPushButton:hover { background: #ff8494; border-color: #ff8494; }"
        % (DANGER, DANGER)
    )

    def _refresh_image_button(self, field: str) -> None:
        """Sync a row to the project: show the filename (clipped) and set the
        button to '파일 선택' when empty or a red '취소' once a file is chosen."""
        name = getattr(self.project, field, "")
        lbl = self._image_names[field]
        lbl.setText(name)
        lbl.setToolTip(name)
        btn = self._image_buttons[field]
        if name:
            btn.setText("취소")
            btn.setStyleSheet(self._IMAGE_CANCEL_QSS)
        else:
            btn.setText("파일 선택")
            btn.setStyleSheet("")

    def _toggle_image(self, field: str, caption: str) -> None:
        # A file is already set → this click clears it.
        if getattr(self.project, field, ""):
            setattr(self.project, field, "")
            self._refresh_image_button(field)
            self._on_changed()
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f"{caption} 선택", self._dir_for("image"),
            "이미지 (*.png *.jpg *.jpeg *.bmp *.gif);;모든 파일 (*)")
        if not path:
            return
        self._remember_dir("image", path)
        setattr(self.project, field, os.path.basename(path))
        self._refresh_image_button(field)
        self._on_changed()

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
        self.edit_mode_action = QAction("편집[F2]", self)
        self.edit_mode_action.setCheckable(True)
        self.edit_mode_action.triggered.connect(lambda: self._set_mode("edit"))
        tb.addAction(self.edit_mode_action)
        self.add_mode_action = QAction("추가[F3]", self)
        self.add_mode_action.setCheckable(True)
        self.add_mode_action.setChecked(True)
        self.add_mode_action.triggered.connect(lambda: self._set_mode("add"))
        tb.addAction(self.add_mode_action)

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

        # Push the "collapse / expand every sidebar section" controls to the far
        # right of the toolbar so they sit above the sidebar and never scroll
        # away with it.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.expand_all_action = QAction(make_icon("expand_all"), "모두 펴기", self)
        self.expand_all_action.triggered.connect(lambda: self._set_all_sections(True))
        tb.addAction(self.expand_all_action)
        self.collapse_all_action = QAction(make_icon("collapse_all"), "모두 접기", self)
        self.collapse_all_action.triggered.connect(lambda: self._set_all_sections(False))
        tb.addAction(self.collapse_all_action)

        # Mode / key-mode buttons keep their text labels (no icon).
        for act in (self.add_mode_action, self.edit_mode_action,
                    *self._km_actions.values()):
            btn = tb.widgetForAction(act)
            if btn is not None:
                btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        # The collapse / expand-all pair reads as part of the icon toolbar:
        # same monochrome glyphs and hover treatment as the transport buttons.
        # Their meaning comes from a real tooltip (below).
        for act in (self.expand_all_action, self.collapse_all_action):
            btn = tb.widgetForAction(act)
            if btn is not None:
                btn.setObjectName("SectionToggle")
                btn.setToolButtonStyle(Qt.ToolButtonIconOnly)

        # No hover tooltips on any toolbar button: an empty tip just falls back
        # to the button text, so swallow the ToolTip event on each button. The
        # two icon-only section buttons are the exception — they need the tip to
        # be readable at all.
        keep_tips = {self.expand_all_action, self.collapse_all_action}
        for act in tb.actions():
            btn = tb.widgetForAction(act)
            if btn is not None and act not in keep_tips:
                btn.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.ToolTip:
            return True   # suppress toolbar hover tooltips
        return super().eventFilter(obj, event)

    def _build_menu(self) -> None:
        m = self.menuBar()

        file_menu = m.addMenu("파일")
        self._add(file_menu, "새로 만들기", self.new_project, QKeySequence.New)
        self._add(file_menu, "열기", self.open_project, QKeySequence.Open)
        self._add(file_menu, "저장", self.save_project, QKeySequence.Save)
        self._add(file_menu, "다른 이름으로 저장", self.save_project_as,
                  QKeySequence("Ctrl+Shift+S"))
        file_menu.addSeparator()
        self._add(file_menu, "bms 가져오기", self.import_bms)
        self._add(file_menu, "bms 내보내기", self.export_bms,
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
        self._add(edit_menu, "키 설정", self.open_keybindings)

        song_menu = m.addMenu("곡")
        self._add(song_menu, "음원 파일 등록", self.choose_bgm)
        song_menu.addSeparator()
        self._add(song_menu, "검증 / 통계", self.show_stats)

        help_menu = m.addMenu("도움말")
        self._add(help_menu, "사용법", self.show_help, QKeySequence.HelpContents)
        help_menu.addSeparator()
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

    # Seeking also accepts the numpad +/- keys. The OS reports these as separate
    # keys carrying Qt.KeypadModifier, so the main-row binding never matches them
    # — we register both the plain and the keypad-modified form explicitly.
    _KEYPAD_EXTRAS = {
        "forward": (Qt.Key_Plus, Qt.KeypadModifier | Qt.Key_Plus),
        "back": (Qt.Key_Minus, Qt.KeypadModifier | Qt.Key_Minus),
    }

    def _apply_shortcut(self, key: str, seq: str) -> None:
        """Set an action's shortcut, keeping the numpad seek keys alongside the
        (configurable) primary one so they always work."""
        act = self._key_actions[key][0]
        seqs = [QKeySequence(seq)]
        seqs += [QKeySequence(extra) for extra in self._KEYPAD_EXTRAS.get(key, ())]
        act.setShortcuts(seqs)

    def _load_shortcuts(self) -> None:
        s = _settings()
        for key, (act, _label, default) in self._key_actions.items():
            seq = s.value(f"shortcuts/{key}", default)
            self._apply_shortcut(key, seq)

    # -- live-recording keys (configurable, per key mode) ------------------- #

    def _record_key_defaults(self):
        """{km: [qt_key per lane]} from the built-in RECORD_KEYS map."""
        out = {}
        for km, mapping in RECORD_KEYS.items():
            by_lane = {lane: key for key, lane in mapping.items()}
            out[km] = [int(by_lane[lane]) for lane in range(len(mapping))]
        return out

    def _load_record_keys(self) -> None:
        s = _settings()
        defaults = self._record_key_defaults()
        self._record_cfg = {}
        for km, dkeys in defaults.items():
            lane_keys = []
            for lane, dk in enumerate(dkeys):
                raw = s.value(f"record/{km}/{lane}", None)
                lane_keys.append(int(raw) if raw is not None else dk)
            self._record_cfg[km] = lane_keys
        self._apply_record_keys()

    def _apply_record_keys(self) -> None:
        mapping = {km: {key: lane for lane, key in enumerate(keys)}
                   for km, keys in self._record_cfg.items()}
        self.view.set_record_keys(mapping)

    def open_keybindings(self) -> None:
        dlg = KeybindingsDialog(self._key_actions, self._record_cfg,
                                self._record_key_defaults(), self)
        if dlg.exec():
            s = _settings()
            for key, seq in dlg.result_shortcuts().items():
                self._apply_shortcut(key, seq)
                s.setValue(f"shortcuts/{key}", seq)
            self._record_cfg = dlg.result_record_keys()
            for km, keys in self._record_cfg.items():
                for lane, k in enumerate(keys):
                    s.setValue(f"record/{km}/{lane}", int(k))
            self._apply_record_keys()

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

    def _refresh_marker_lists(self) -> None:
        """Re-sync the BPM / 정지 / 노트 속도 lists after the data changed behind
        their backs (undo / redo, measure reflow). Any in-progress edit is
        dropped since its target row may no longer exist."""
        if not hasattr(self, "_bpm_edit"):
            return
        for ctl in (self._bpm_edit, self._stop_edit, self._scroll_edit):
            if ctl.target is not None:
                ctl._exit()
        self._refresh_bpm_list()
        self._refresh_stop_list()
        self._refresh_scroll_list()

    def _set_all_sections(self, expanded: bool) -> None:
        """Collapse or expand every sidebar section at once."""
        for sec in self._sections.values():
            sec.set_expanded(expanded)

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
        for field in self._image_buttons:
            self._refresh_image_button(field)
        self._refresh_bpm_list()
        self._refresh_stop_list()
        self._refresh_scroll_list()

    def _set_meta(self, field: str, value) -> None:
        setattr(self.project, field, value)
        self._on_changed()

    # -- editor/session settings (persisted in .slbms) ---------------------- #

    def _capture_editor_settings(self) -> None:
        self.project.editor = {
            "selected_km": self.view.selected_km,
            "grid_snap": self.sb_g1.value(),
            "grid_sub": self.sb_g2.value(),
            "snap_on": self.sb_snap.isChecked(),
            "zoom_v": self.zoom_v.value(),
            "zoom_h": self.zoom_h.value(),
            "speed": self.speed.value(),
            "volume": self.volume.value(),
            "sections": {t: s.is_expanded() for t, s in self._sections.items()},
        }

    def _apply_editor_settings(self) -> None:
        e = self.project.editor or {}
        if e.get("selected_km") in KEY_MODES:
            self._set_keymode(e["selected_km"])
        if "grid_snap" in e:
            self.sb_g1.setValue(int(e["grid_snap"]))
        if "grid_sub" in e:
            self.sb_g2.setValue(int(e["grid_sub"]))
        if "snap_on" in e:
            self.sb_snap.setChecked(bool(e["snap_on"]))
        if "zoom_v" in e:
            self.zoom_v.set_value(float(e["zoom_v"]))
        if "zoom_h" in e:
            self.zoom_h.set_value(float(e["zoom_h"]))
        if "speed" in e:
            self.speed.set_value(float(e["speed"]))
        if "volume" in e:
            self.volume.set_value(float(e["volume"]))
        for title, expanded in (e.get("sections") or {}).items():
            sec = self._sections.get(title)
            if sec is not None:
                sec.set_expanded(bool(expanded))

    def _autofit_measures(self) -> None:
        """Size the timeline so it spans every note and the whole song.

        The song's length is measured in *real* units (the running sum of measure
        lengths), not in measure counts: a shortened measure covers less music, so
        the count has to grow to keep reaching the end of the audio — and shrink
        back when the measures are stretched out again. Growing is immediate;
        trimming waits until a measure-length drag has settled so the canvas never
        resizes under the cursor mid-drag."""
        if self.view.is_editing_notes():
            # A note drag rewrites notes on every mouse step, and dragging the
            # BGM marker moves the song start with them. Refitting here would
            # rebuild the canvas and the scroll bar under the cursor each frame;
            # the release re-emits `changed`, so the fit happens once, after.
            return
        highest = 0
        for chart in self.project.charts.values():
            for n in chart:
                highest = max(highest, int(n.end_absolute))
        for n in self.project.bgm:
            highest = max(highest, n.measure)
        need = highest + 4  # keep a few empty measures above for placing notes
        if self.audio.duration > 0:
            tm = TimeMap(self.project)
            # Real span the audio occupies, starting where the BGM marker sits.
            target = tm.t0 + self.audio.duration * tm.measures_per_second
            need = max(need, self._measures_spanning(target) + 2)
        need = max(16, need)
        if need > self.project.measures:
            self._resize_measures(need)
        elif need < self.project.measures and not self.view.is_scaling():
            self._resize_measures(need)

    def _measures_spanning(self, target: float) -> int:
        """How many measures it takes to cover ``target`` real units. Measures
        past the current end of the timeline count as full length."""
        total = 0.0
        m = 0
        while total < target and m < 100000:
            total += float(self.project.measure_length(m))
            m += 1
        return m

    def _center_on_last_note(self) -> None:
        """Scroll so the last-placed note sits at the vertical centre of the
        view. Falls back to the song start (bottom) when there are no notes.
        BGM objects don't count — this centres on the actual chart."""
        vbar = self.scroll.verticalScrollBar()
        vp_h = self.scroll.viewport().height()
        last = None
        for chart in self.project.charts.values():
            for n in chart:
                end = float(n.end_absolute)
                if last is None or end > last:
                    last = end
        if last is None:
            vbar.setValue(vbar.maximum())          # empty chart -> song start
        else:
            vbar.setValue(int(self.view.y_for(last) - vp_h / 2))

    def _focus_chart_range(self, lo: float, hi: float) -> None:
        """Scroll the absolute range ``[lo, hi]`` into view (used after a paste).
        A range that's already comfortably visible is left alone, so pasting
        inside the current view doesn't jerk the chart around."""
        # Run after the pending relayout: the paste resizes the canvas, and the
        # scroll bar's range only catches up once the scroll area has resized.
        QTimer.singleShot(0, lambda: self._do_focus_chart_range(lo, hi))

    def _do_focus_chart_range(self, lo: float, hi: float) -> None:
        vbar = self.scroll.verticalScrollBar()
        vp_h = self.scroll.viewport().height()
        # The chart runs bottom-up, so the later position is the higher one.
        y_top = self.view.y_for(hi)
        y_bot = self.view.y_for(lo)
        margin = min(80, vp_h // 6)
        top, bottom = vbar.value(), vbar.value() + vp_h
        if (y_bot - y_top) + 2 * margin >= vp_h:
            target = (y_top + y_bot) / 2 - vp_h / 2      # taller than the view: centre it
        elif y_top - margin < top:
            target = y_top - margin
        elif y_bot + margin > bottom:
            target = y_bot + margin - vp_h
        else:
            return                                        # already visible
        vbar.setValue(int(round(target)))

    def _resize_measures(self, need: int) -> None:
        # The chart is drawn bottom-up, so measures are added (or dropped) at the
        # top: shifting the scroll value by exactly the height change leaves every
        # pixel of chart where it was. The scroll area only recomputes the bar's
        # range on its next layout pass, so set the new range here too — otherwise
        # the value would clamp to the stale maximum and the view would visibly
        # jolt before snapping back.
        vbar = self.scroll.verticalScrollBar()
        vp_h = self.scroll.viewport().height()
        before = self.view.content_height()
        self.project.measures = need
        self.view.refresh()
        delta = self.view.content_height() - before
        if not delta:
            return
        vbar.setRange(0, max(0, self.view.content_height() - vp_h))
        vbar.setValue(vbar.value() + delta)

    def _apply_grids(self) -> None:
        # Left box drives the snap grid; right box is the reference grid.
        self.view.set_grid_main(self.sb_g1.value())
        self.view.set_grid_sub(self.sb_g2.value())
        # One measure holds exactly `스냅 격자` cells, so the BPM-change cell box
        # caps at the snap-grid value (a bigger cell is clamped down to it).
        if hasattr(self, "bpm_cell"):
            self.bpm_cell.setMaximum(self.sb_g1.value())
        if hasattr(self, "stop_cell"):
            self.stop_cell.setMaximum(self.sb_g1.value())
        if hasattr(self, "scroll_cell"):
            self.scroll_cell.setMaximum(self.sb_g1.value())
            self.scroll_cell2.setMaximum(self.sb_g1.value())

    def _toggle_snap(self, on: bool) -> None:
        self.view.set_snap_on(on)
        self.sb_snap.setText("격자 스냅 : 켜짐" if on else "격자 스냅 : 꺼짐")

    def _toggle_waveform(self, on: bool) -> None:
        self.view.set_show_waveform(on)
        self.sb_wave.setText("파형 표시 : 켜짐" if on else "파형 표시 : 꺼짐")

    def _set_bgm_width(self, w: int) -> None:
        self.view.set_bgm_width(w)
        self.header.set_bgm_width(self.view.bgm_w)   # keep header in sync (clamped)
        _settings().setValue("sidebar/bgm_width", self.view.bgm_w)

    def _load_layout_prefs(self) -> None:
        raw = _settings().value("sidebar/bgm_width", 64)
        try:
            w = int(raw)
        except (TypeError, ValueError):
            w = 64
        self.view.set_bgm_width(w)
        self.header.set_bgm_width(self.view.bgm_w)

    def _restore_geometry(self) -> None:
        geo = _settings().value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)   # size + position + maximized state

    def _save_geometry(self) -> None:
        _settings().setValue("window/geometry", self.saveGeometry())

    # -- tempo changes ------------------------------------------------------ #

    def _add_bpm_change(self) -> None:
        from fractions import Fraction
        grid = max(1, self.sb_g1.value())
        cell = min(self.bpm_cell.value(), grid)   # never past one measure of cells
        pos = Fraction(self.bpm_measure.value()) + Fraction(cell, grid)
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
        for pos, bpm in sorted(self.project.bpm_changes.items(), reverse=True):
            self._add_marker_row(self.bpm_list, self._loc(pos), f"♩ {bpm:g}", pos)

    def _load_bpm(self, pos) -> None:
        """Load a BPM marker's (마디/칸/BPM) into the inputs and centre the
        canvas on it (used by double-click and by entering edit mode)."""
        if pos not in self.project.bpm_changes:
            return
        self._set_pos_inputs(self.bpm_measure, self.bpm_cell, pos)
        self.bpm_value.setValue(self.project.bpm_changes[pos])
        self._center_canvas(pos)

    def _commit_bpm(self, target) -> bool:
        """Replace the edited BPM marker: remove the old one, add the new one
        from the inputs (so changing its position moves it)."""
        from fractions import Fraction
        grid = max(1, self.sb_g1.value())
        cell = min(self.bpm_cell.value(), grid)
        pos = Fraction(self.bpm_measure.value()) + Fraction(cell, grid)
        self.project.bpm_changes.pop(target, None)
        self.project.bpm_changes[pos] = float(self.bpm_value.value())
        self.view.changed.emit()
        self.view.update()
        self._refresh_bpm_list()
        return True

    def _add_stop(self) -> None:
        from fractions import Fraction
        grid = max(1, self.sb_g1.value())
        cell = min(self.stop_cell.value(), grid)   # never past one measure of cells
        pos = Fraction(self.stop_measure.value()) + Fraction(cell, grid)
        beats = Fraction(self.stop_beats.value()).limit_denominator(192)
        if beats <= 0:
            return
        self.project.stops[pos] = beats
        self.view.changed.emit()   # marks dirty + schedules an undo entry
        self.view.update()
        self._refresh_stop_list()

    def _remove_stop(self) -> None:
        item = self.stop_list.currentItem()
        if item is None:
            return
        pos = item.data(Qt.UserRole)
        if pos in self.project.stops:
            del self.project.stops[pos]
            self.view.changed.emit()
            self.view.update()
            self._refresh_stop_list()

    def _refresh_stop_list(self) -> None:
        self.stop_list.clear()
        for pos, beats in sorted(self.project.stops.items(), reverse=True):
            self._add_marker_row(self.stop_list, self._loc(pos),
                                 f"■ {float(beats):g}박", pos)

    def _load_stop(self, pos) -> None:
        if pos not in self.project.stops:
            return
        self._set_pos_inputs(self.stop_measure, self.stop_cell, pos)
        self.stop_beats.setValue(float(self.project.stops[pos]))
        self._center_canvas(pos)

    def _commit_stop(self, target) -> bool:
        from fractions import Fraction
        pos = self._marker_pos(self.stop_measure, self.stop_cell)
        beats = Fraction(self.stop_beats.value()).limit_denominator(192)
        if beats <= 0:
            return False
        self.project.stops.pop(target, None)
        self.project.stops[pos] = beats
        self.view.changed.emit()
        self.view.update()
        self._refresh_stop_list()
        return True

    # -- 노트 속도 (시작 → 끝 구간 변속) ---------------------------------- #

    def _marker_pos(self, measure_box, cell_box):
        from fractions import Fraction
        grid = max(1, self.sb_g1.value())
        cell = min(cell_box.value(), grid)
        return Fraction(measure_box.value()) + Fraction(cell, grid)

    def _set_pos_inputs(self, measure_box, cell_box, pos) -> None:
        grid = max(1, self.sb_g1.value())
        m = int(pos)
        measure_box.setValue(m)
        cell_box.setValue(int(round(float(pos - m) * grid)))

    def _center_canvas(self, pos) -> None:
        vbar = self.scroll.verticalScrollBar()
        vp_h = self.scroll.viewport().height()
        vbar.setValue(int(self.view.y_for(float(pos)) - vp_h / 2))

    def _loc(self, pos) -> str:
        """A position label shared by every marker list: '마디 18 · 칸 12'
        (or just '마디 18' at a measure start), with no grid denominator."""
        grid = max(1, self.sb_g1.value())
        m = int(pos)
        cell = int(round(float(pos - m) * grid))
        return f"마디 {m}" if cell == 0 else f"마디 {m} · 칸 {cell}"

    def _add_marker_row(self, listw, left: str, right: str, userdata) -> None:
        """Add a two-column row (position | value) rendered by MarkerListDelegate
        so the right column lines up neatly down the list."""
        listw.addItem(left)
        item = listw.item(listw.count() - 1)
        item.setData(MARKER_RIGHT_ROLE, right)
        item.setData(Qt.UserRole, userdata)

    def _scroll_from_inputs(self):
        """(start_pos, end_pos, start_val, end_val) from the inputs. ``end_pos``
        is None when the end point isn't after the start (e.g. left at 0): that
        means a 순간 변속 — a single step at the start point — instead of a ramp,
        so the end row doubles as the "no ramp" switch."""
        from fractions import Fraction
        sp = self._marker_pos(self.scroll_measure, self.scroll_cell)
        ep = self._marker_pos(self.scroll_measure2, self.scroll_cell2)
        return (sp, ep if ep > sp else None,
                Fraction(self.scroll_value.value()).limit_denominator(1000),
                Fraction(self.scroll_value2.value()).limit_denominator(1000))

    def _sync_scroll_end_row(self) -> None:
        """Grey the 노트 속도 end row while it holds no usable end point, so the
        순간(단일 지점) / 선형(구간) state is visible without a caption."""
        active = self._marker_pos(self.scroll_measure2, self.scroll_cell2) > \
            self._marker_pos(self.scroll_measure, self.scroll_cell)
        for w in self._scroll_end_row:
            if w.property("inactive") == (not active):
                continue                       # already in this state
            w.setProperty("inactive", not active)
            w.style().unpolish(w)
            w.style().polish(w)

    def _write_scroll(self, sp, ep, sv, ev) -> None:
        """Store either a ramp (two SPEED markers) or a single 순간 SCROLL step."""
        if ep is None:
            self.project.scrolls[sp] = sv
        else:
            self.project.speeds[sp] = sv
            self.project.speeds[ep] = ev

    def _add_scroll(self) -> None:
        sp, ep, sv, ev = self._scroll_from_inputs()
        self._write_scroll(sp, ep, sv, ev)
        self.view.changed.emit()
        self.view.update()
        self._refresh_scroll_list()

    def _remove_scroll_data(self, data) -> None:
        if data[0] == "speed":                  # ("speed", start_pos, end_pos)
            for pos in data[1:]:
                self.project.speeds.pop(pos, None)
        else:                                    # ("scroll", pos)
            self.project.scrolls.pop(data[1], None)

    def _commit_scroll(self, target) -> bool:
        sp, ep, sv, ev = self._scroll_from_inputs()
        self._remove_scroll_data(target)         # drop the edited entry first
        self._write_scroll(sp, ep, sv, ev)
        self.view.changed.emit()
        self.view.update()
        self._refresh_scroll_list()
        return True

    def _remove_scroll(self) -> None:
        item = self.scroll_list.currentItem()
        if item is None:
            return
        self._remove_scroll_data(item.data(Qt.UserRole))
        self.view.changed.emit()
        self.view.update()
        self._refresh_scroll_list()

    def _refresh_scroll_list(self) -> None:
        self.scroll_list.clear()
        rows = []   # (sort_key, left, right, userdata)
        for pos, val in self.project.scrolls.items():
            rows.append((float(pos), self._loc(pos), f"×{float(val):g}",
                         ("scroll", pos)))
        for sp, ep, sv, ev in self.view._speed_ramps():
            rows.append((float(sp), f"{self._loc(sp)} ~ {self._loc(ep)}",
                         f"×{float(sv):g} ~ ×{float(ev):g}", ("speed", sp, ep)))
        for _key, left, right, data in sorted(rows, key=lambda r: r[0], reverse=True):
            self._add_marker_row(self.scroll_list, left, right, data)

    def _load_scroll(self, data) -> None:
        def set_point(measure_box, cell_box, value_box, pos, val):
            self._set_pos_inputs(measure_box, cell_box, pos)
            value_box.setValue(float(val))

        if data[0] == "speed":
            _, sp, ep = data
            if sp not in self.project.speeds:
                return
            set_point(self.scroll_measure, self.scroll_cell, self.scroll_value,
                      sp, self.project.speeds[sp])
            set_point(self.scroll_measure2, self.scroll_cell2, self.scroll_value2,
                      ep, self.project.speeds.get(ep, self.project.speeds[sp]))
            focus = sp
        else:                                    # a single (순간) marker
            _, pos = data
            if pos not in self.project.scrolls:
                return
            set_point(self.scroll_measure, self.scroll_cell, self.scroll_value,
                      pos, self.project.scrolls[pos])
            # Zero the whole end row: that's what marks it as 순간 (no ramp), so
            # committing it back keeps it a single step instead of growing a ramp.
            self.scroll_measure2.setValue(0)
            self.scroll_cell2.setValue(0)
            self.scroll_value2.setValue(0.0)
            focus = pos
        self._center_canvas(focus)

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

    def _warn_overlap(self) -> None:
        # Non-modal: a brief status-bar note (the overlap also shows as a red
        # outline on the notes). No blocking dialog.
        self.statusBar().showMessage(
            "⚠ 노트가 다른 노트와 겹쳤습니다 (빨간 테두리로 표시됨)", 4000)

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

    # -- playback / preview (transport lives in PlaybackController) --------- #

    def _set_speed(self, factor: float) -> None:
        self.playback._set_speed(factor)

    def _commit_speed(self) -> None:
        self.playback._commit_speed()

    def toggle_play(self) -> None:
        self.playback.toggle_play()

    def _start_play(self) -> None:
        self.playback._start_play()

    def _do_start_play(self) -> None:
        self.playback._do_start_play()

    def _countin_tick(self) -> None:
        self.playback._countin_tick()

    def _pause_play(self) -> None:
        self.playback._pause_play()

    def stop_play(self) -> None:
        self.playback.stop_play()

    def go_to_start(self) -> None:
        self.playback.go_to_start()

    def seek_seconds(self, d_seconds: float) -> None:
        self.playback.seek_seconds(d_seconds)

    def _seek_audio(self, seconds: float) -> None:
        self.playback._seek_audio(seconds)

    def _seek_to_chart(self, absolute: float) -> None:
        self.playback._seek_to_chart(absolute)

    def _on_play_tick(self) -> None:
        self.playback._on_play_tick()

    def _update_record_offset(self) -> None:
        self.playback._update_record_offset()

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
            # The .slbms stores the full audio path + editor settings, so the
            # recovery copy comes back exactly as it was.
            self._capture_editor_settings()
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
            self._set_project_path(None)
            self._dirty = True
            self._reload_view()
            self._auto_load_bgm()          # the recovery .slbms keeps the full path
            self._apply_editor_settings()
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
            f"정지(STOP) : {len(p.stops)}개",
            f"노트 속도(순간/선형) : {len(p.scrolls)} / {len(p.speeds) // 2}개",
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
        self._set_project_path(None)
        self._dirty = False
        self._clear_recovery()
        self._reset_editor_settings()
        self._reload_view()

    def _reset_editor_settings(self) -> None:
        """Put every editor/session control back to its default and drop the
        loaded audio, so 'New' starts from a clean slate."""
        self.audio.unload()
        self._bgm_path = None
        self.view.set_waveform(None, 200)
        for w, val in (
            (self.sb_g1, 32), (self.sb_g2, 8),
        ):
            w.setValue(val)
        self.sb_snap.setChecked(True)
        self.sb_wave.setChecked(True)
        self.zoom_v.set_value(2.0)
        self.zoom_h.set_value(1.25)
        self.speed.set_value(1.0)
        self.volume.set_value(0.3)
        self._set_keymode(KEY_MODES[0])
        for sec in self._sections.values():
            sec.set_expanded(True)

    def open_project(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "프로젝트 열기", self._dir_for("open"),
            "SlimBMS 프로젝트 (*.slbms);;모든 파일 (*)")
        if not path:
            return
        self.load_project_path(path)

    def load_project_path(self, path: str) -> bool:
        """Open a .slbms file by path (used by the menu and by double-click /
        file-association launches). Returns True on success."""
        try:
            self.project = bms_io.load_project(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "열기 실패", str(exc))
            return False
        self._remember_dir("open", path)
        self._set_project_path(path)
        self._dirty = False
        self._reload_view()
        self._auto_load_bgm()          # reconnect the audio automatically
        self._apply_editor_settings()  # key mode, grid, zoom, speed, volume
        # Defer centring until the layout settles so the scrollbar range is
        # final (zoom/grid changes above resize the view).
        QTimer.singleShot(0, self._center_on_last_note)
        return True

    def save_project(self) -> None:
        if not self.project_path:
            self.save_project_as()
            return
        try:
            self._capture_editor_settings()
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
        self._set_project_path(path)
        self.save_project()

    def import_bms(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "bms 가져오기", self._dir_for("import"),
            "bms 채보 (*.bms *.bme *.bml);;모든 파일 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                self.project = bms_io.parse_bms(fh.read())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "가져오기 실패", str(exc))
            return
        self._remember_dir("import", path)
        self._set_project_path(None)
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
            self, f"{km}K bms 내보내기", default, "bms 채보 (*.bms)")
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
            f"{km}K 채보를 저장했습니다.\n\n노트 수: {self.project.note_count(km)}")

    # -- song actions ------------------------------------------------------- #

    def choose_bgm(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "음원 파일 선택", self._dir_for("bgm"),
            "오디오 (*.wav *.ogg *.mp3 *.flac);;모든 파일 (*)")
        if not path:
            return
        self._remember_dir("bgm", path)
        loaded = self._apply_bgm(path)
        self._on_changed()
        note = "" if loaded else "\n\n(이 환경에서는 오디오 장치가 없어 재생 미리보기는 실제 PC에서만 됩니다.)"
        QMessageBox.information(
            self, "BGM 설정됨",
            f"BGM 파일명: {self.project.bgm_file}\n"
            "이 파일을 .bms와 같은 폴더에 두어야 게임에서 재생됩니다." + note)

    def _auto_load_bgm(self) -> None:
        """Reconnect the project's BGM audio after open/recovery: try the saved
        full path, then the audio filename next to the project, then the last
        BGM folder. Silent no-op if none exist (won't nag)."""
        name = self.project.bgm_file
        if not name:
            return
        candidates = [self.project.bgm_path]
        if self.project_path:
            candidates.append(os.path.join(os.path.dirname(self.project_path), name))
        d = self._dir_for("bgm")
        if d:
            candidates.append(os.path.join(d, name))
        for c in candidates:
            if c and os.path.exists(c):
                self._apply_bgm(c)
                return

    def _apply_bgm(self, path: str) -> bool:
        """Load an audio file as the BGM: register it, decode, build the
        waveform and pre-stretch. Used by the file dialog and by recovery."""
        self.project.bgm_file = os.path.basename(path)
        self.project.bgm_path = path
        self._bgm_path = path
        loaded = self.audio.load(path)
        self.sb_bgm_label.setText(self.project.bgm_file)
        peaks, bps = self.audio.waveform_peaks()
        self.view.set_waveform(peaks, bps)
        if loaded:
            # Grow the timeline up front to span the whole song, so there's room
            # to place / paste notes across its full length from the start.
            self._autofit_measures()
        if loaded and not self.audio.stretch_ready():
            worker = self._bgm_speed_worker = _Worker(self.audio.build_stretch)
            worker.start()
        if loaded:
            # Pre-build the common slow speeds in the background so picking one on
            # the gauge applies instantly (and caps build memory to a small
            # streamed block instead of the whole song at once).
            speeds = list(PRECOMPUTE_SPEEDS)
            pre = self._bgm_precompute_worker = _Worker(
                lambda: self.audio.precompute_speeds(speeds))
            pre.start()
        return loaded

    # -- updates ------------------------------------------------------------ #

    def show_help(self) -> None:
        HelpDialog(__version__, self).exec()

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

        # A newer version exists: ask the user before applying it.
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("업데이트")
        box.setText("새로운 업데이트가 있습니다.\n지금 업데이트할까요?")
        box.setInformativeText(
            f"현재 버전 : v{__version__}\n새 버전 : {info.tag}")
        ok = box.addButton("확인", QMessageBox.AcceptRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(ok)
        box.exec()
        if box.clickedButton() is not ok:
            return
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

    def _set_project_path(self, path: Optional[str]) -> None:
        """Point the window at a .slbms file (or none). Drops the per-operation
        folder overrides so every dialog starts at the new project's folder."""
        self.project_path = path
        self._last_dirs.clear()

    def _project_dir(self) -> str:
        """The folder the current .slbms lives in ("" when never saved)."""
        return os.path.dirname(self.project_path) if self.project_path else ""

    def _dir_for(self, key: str) -> str:
        """Where a file dialog for the ``key`` operation (open/save/import/export/
        bgm/image) should start: the project's own folder, since a song's audio,
        images and exports normally sit next to the .slbms. Once the user picks a
        different folder for an operation it's reused for the rest of the session
        (reset when another project is opened); with no project open it falls back
        to that operation's last folder from the previous session."""
        d = self._last_dirs.get(key)
        if d:
            return d
        d = self._project_dir()
        if d:
            return d
        return _settings().value(f"paths/{key}", "") or ""

    def _remember_dir(self, key: str, path: str) -> None:
        d = os.path.dirname(path)
        # Only override the project folder when the user actually went elsewhere.
        if d and d != self._project_dir():
            self._last_dirs[key] = d
        else:
            self._last_dirs.pop(key, None)
        _settings().setValue(f"paths/{key}", d)

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
