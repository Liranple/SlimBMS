"""Small application dialogs, kept out of the main-window module."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QKeySequence, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QKeySequenceEdit,
    QTextBrowser,
    QVBoxLayout,
)

from .palette import ACCENT, BORDER, CANVAS, PANEL, TEXT, TEXT_DIM
from .toolbar_icons import make_icon


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


# --------------------------------------------------------------------------- #
# Help / usage guide
# --------------------------------------------------------------------------- #

_HELP_ICONS = ("open", "save", "import", "export",
               "first", "back", "play", "forward", "stop")


def _ico(name: str) -> str:
    return (f'<img src="icon:{name}" width="17" height="17" '
            f'style="vertical-align:middle;">')


def _section(title: str, body: str) -> str:
    """An accent header bar followed by a short description block."""
    return (
        f'<table width="100%" cellspacing="0" cellpadding="7" '
        f'style="margin-top:16px;"><tr>'
        f'<td bgcolor="{PANEL}" style="color:{ACCENT};"><b>{title}</b></td>'
        f'</tr></table>'
        f'<table width="100%" cellspacing="0" cellpadding="11"><tr>'
        f'<td style="color:{TEXT};">{body}</td></tr></table>'
    )


def _help_html(version: str) -> str:
    def name(text):   # a highlighted UI-element name (no box)
        return f'<b style="color:{ACCENT};">{text}</b>'

    dim = f'color:{TEXT_DIM};'
    sections = [
        ("상단 패널 · 파일",
         f'{_ico("open")} {name("열기")} &nbsp; {_ico("save")} {name("저장")} &nbsp; '
         f'{_ico("import")} {name("가져오기")} &nbsp; {_ico("export")} {name("내보내기")}'
         f'<div style="{dim} margin-top:6px;">'
         f'편집 세션(.slbms)을 열고 저장하며, 기존 .bms를 가져오거나 '
         f'<b>선택한 키 모드를 .bms로 내보냅니다.</b></div>'),
        ("재생 패널",
         f'{_ico("first")} {_ico("back")} {_ico("play")} {_ico("forward")} {_ico("stop")}'
         f'<div style="{dim} margin-top:6px;">'
         f'곡 미리보기 조작 — 처음으로 · 1초 뒤/앞 · 재생/일시정지(Space) · 정지.</div>'),
        ("편집 / 추가",
         f'{name("추가")}(F3) 모드에서 클릭으로 노트를 찍고 위·아래로 드래그해 롱노트를 만듭니다. '
         f'{name("편집")}(F2) 모드에서 노트를 선택해 방향키·드래그로 옮깁니다.'),
        ("4K / 6K",
         f'.bms로 <b>내보낼 키 모드</b>를 선택합니다. 두 채보는 같은 곡을 공유합니다.'),
        ("사이드바",
         f'곡 정보 · 격자 · 확대/축소 · BPM 변화 · 음원 · 녹음 설정이 '
         f'접이식 섹션으로 모여 있습니다.'),
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
