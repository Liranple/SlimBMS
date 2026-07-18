"""Small application dialogs, kept out of the main-window module."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QKeySequence, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QKeySequenceEdit,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .palette import ACCENT, BORDER, CANVAS, PANEL, TEXT, TEXT_DIM
from .toolbar_icons import make_icon


def _seq_to_key(seq: QKeySequence):
    """The base Qt key of a single-key sequence (ignoring modifiers), or None."""
    if seq.count() == 0:
        return None
    return int(seq[0].key())


class KeybindingsDialog(QDialog):
    """Reassign the app's shortcuts. A tab holds the general/transport keys, and
    one tab per key mode holds that mode's live-recording (채보) keys."""

    def __init__(self, key_actions, record_lists, record_defaults, parent=None):
        super().__init__(parent)
        self.setWindowTitle("키 설정")
        self.setMinimumWidth(380)
        self._edits = {}
        self._defaults = {k: d for k, (_a, _l, d) in key_actions.items()}
        self._rec_edits = {}                 # {km: [QKeySequenceEdit per lane]}
        self._rec_defaults = record_defaults

        v = QVBoxLayout(self)
        tabs = QTabWidget()

        # General / transport shortcuts.
        general = QWidget()
        gform = QFormLayout(general)
        gform.setHorizontalSpacing(14)
        gform.setVerticalSpacing(8)
        for key, (act, label, _default) in key_actions.items():
            edit = QKeySequenceEdit(act.shortcut())
            edit.setMaximumSequenceLength(1)
            self._edits[key] = edit
            gform.addRow(label, edit)
        tabs.addTab(general, "일반 · 재생")

        # One tab per key mode for its recording keys (one row per lane).
        for km, keys in record_lists.items():
            page = QWidget()
            form = QFormLayout(page)
            form.setHorizontalSpacing(14)
            form.setVerticalSpacing(8)
            edits = []
            for lane, k in enumerate(keys):
                edit = QKeySequenceEdit(QKeySequence(k))
                edit.setMaximumSequenceLength(1)
                edits.append(edit)
                form.addRow(f"{lane + 1}번 레인", edit)
            self._rec_edits[km] = edits
            tabs.addTab(page, f"{km}K 채보")

        v.addWidget(tabs)

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
        for km, edits in self._rec_edits.items():
            for lane, edit in enumerate(edits):
                edit.setKeySequence(QKeySequence(self._rec_defaults[km][lane]))

    def result_shortcuts(self):
        return {key: edit.keySequence().toString()
                for key, edit in self._edits.items()}

    def result_record_keys(self):
        """{km: [qt_key per lane]}; a cleared field keeps its default."""
        out = {}
        for km, edits in self._rec_edits.items():
            lst = []
            for lane, edit in enumerate(edits):
                k = _seq_to_key(edit.keySequence())
                lst.append(k if k is not None else self._rec_defaults[km][lane])
            out[km] = lst
        return out


# --------------------------------------------------------------------------- #
# Help / usage guide
# --------------------------------------------------------------------------- #

_HELP_ICONS = ("open", "save", "import", "export",
               "first", "back", "play", "forward", "stop")


def _ico(name: str) -> str:
    return (f'<img src="icon:{name}" width="17" height="17" '
            f'style="vertical-align:middle;">')


def _section(title: str, body: str) -> str:
    """An accent header bar followed by its description block."""
    return (
        f'<table width="100%" cellspacing="0" cellpadding="7" '
        f'style="margin-top:16px;"><tr>'
        f'<td bgcolor="{PANEL}" style="color:{ACCENT};"><b>{title}</b></td>'
        f'</tr></table>' + body
    )


def _rows(items) -> str:
    """A borderless label : description table (no boxes around the labels)."""
    out = ['<table width="100%" cellspacing="0" cellpadding="7" '
           'style="margin-top:2px;">']
    for label, desc in items:
        out.append(
            f'<tr><td width="33%" valign="top" style="color:{ACCENT};">'
            f'<b>{label}</b></td>'
            f'<td valign="top" style="color:{TEXT};">{desc}</td></tr>')
    out.append('</table>')
    return "".join(out)


def _help_html(version: str) -> str:
    sections = [
        ("파일 관리", _rows([
            (f'{_ico("open")} 열기', "slbms 파일을 불러옵니다"),
            (f'{_ico("save")} 저장', "slbms 파일로 저장합니다"),
            (f'{_ico("import")} 가져오기', "bms 파일을 가져옵니다"),
            (f'{_ico("export")} 내보내기', "선택한 키로 bms 파일을 내보냅니다"),
        ])),
        ("재생 패널", _rows([
            (f'{_ico("first")} 처음으로', "재생 위치를 곡의 맨 앞으로 옮깁니다"),
            (f'{_ico("back")} 1초 뒤로', "재생 위치를 1초 뒤로 옮깁니다"),
            (f'{_ico("play")} 재생 / 일시정지', "미리보기를 재생하거나 멈춥니다"),
            (f'{_ico("forward")} 1초 앞으로', "재생 위치를 1초 앞으로 옮깁니다"),
            (f'{_ico("stop")} 정지', "재생을 멈춥니다"),
        ])),
        ("편집 / 추가", _rows([
            ("편집", "노트의 위치를 방향키 · 마우스로 수정할 수 있습니다"),
            ("추가", "마우스 좌클릭(드래그)으로 노트(롱노트)를 추가 · 변경하고, "
                    "우클릭으로 노트를 삭제할 수 있습니다"),
        ])),
        ("키 선택", _rows([
            ("4K / 6K", "bms로 내보낼 키 모드를 선택하며, 선택한 키의 레인이 강조됩니다"),
        ])),
        ("사이드 패널", _rows([
            ("곡 정보", "제목 · 아티스트 · 장르 · BPM · 난이도 등 곡 정보를 입력합니다"),
            ("이미지", "대표 · 배너 · 배경 이미지를 지정합니다"),
            ("격자", "노트가 놓이는 스냅 격자와 참고용 보조 격자를 설정합니다"),
            ("확대/축소", "채보의 세로 · 가로 배율을 조절합니다"),
            ("BPM 변화", "곡 중간의 BPM 변화를 추가 · 삭제합니다"),
            ("음원", "곡 음원을 등록하고 재생 속도 · 음량 · 파형 표시를 조절합니다"),
            ("실시간 채보", "재생 중 키 입력으로 노트를 녹음하며, "
                          "카운트인 · 메트로놈 · 입력 보정을 지원합니다"),
        ])),
    ]
    body = "".join(_section(t, b) for t, b in sections)
    return f"""
    <div style="font-size:20pt; color:{ACCENT};"><b>SlimBMS 사용법</b></div>
    <div style="color:{TEXT_DIM}; font-size:10pt; margin-top:2px;">
        무키음 4K / 6K BMS 채보 에디터 &nbsp;·&nbsp; v{version}
    </div>
    {body}
    """


class HelpDialog(QDialog):
    """A clean, scrollable usage guide shown from the Help menu."""

    def __init__(self, version: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("사용법")
        self.resize(620, 560)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(12)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.document().setDocumentMargin(20)
        # Embed the real toolbar icons so the file / playback rows match the app.
        doc = browser.document()
        for icon_name in _HELP_ICONS:
            doc.addResource(QTextDocument.ImageResource, QUrl(f"icon:{icon_name}"),
                            make_icon(icon_name).pixmap(17, 17))
        browser.setStyleSheet(
            f"QTextBrowser {{ background-color:{CANVAS}; color:{TEXT};"
            f" border:1px solid {BORDER}; border-radius:8px; }}")
        browser.setHtml(_help_html(version))
        v.addWidget(browser)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.accept)
        close = buttons.button(QDialogButtonBox.Close)
        close.setText("닫기")
        close.clicked.connect(self.accept)
        v.addWidget(buttons)
