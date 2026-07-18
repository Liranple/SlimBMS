"""Small application dialogs, kept out of the main-window module."""

from __future__ import annotations

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QKeySequenceEdit,
    QTextBrowser,
    QVBoxLayout,
)

from .palette import ACCENT, BORDER, CANVAS, FIELD, PANEL, TEXT, TEXT_DIM


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

def _kbd(*keys: str) -> str:
    """Render one or more keys as small chips, joined by a thin ``+``."""
    chip = (f'<span style="background-color:{FIELD}; color:{ACCENT}; '
            f'font-family:Consolas,monospace; font-size:9pt;">'
            '&nbsp;{k}&nbsp;</span>')
    return f'<span style="color:{TEXT_DIM};"> + </span>'.join(
        chip.format(k=k) for k in keys)


def _section(title: str, rows) -> str:
    """A titled block: an accent header bar then zebra-striped key/description
    rows. ``rows`` is a list of ``(left_html, right_html)`` pairs."""
    head = (
        f'<table width="100%" cellspacing="0" cellpadding="7" '
        f'style="margin-top:18px;"><tr>'
        f'<td bgcolor="{PANEL}" style="color:{ACCENT};">'
        f'<b>{title}</b></td></tr></table>'
    )
    body = [f'<table width="100%" cellspacing="0" cellpadding="8">']
    for i, (left, right) in enumerate(rows):
        bg = CANVAS if i % 2 == 0 else PANEL
        body.append(
            f'<tr bgcolor="{bg}">'
            f'<td width="38%" valign="top">{left}</td>'
            f'<td valign="top" style="color:{TEXT};">{right}</td></tr>'
        )
    body.append('</table>')
    return head + "".join(body)


def _help_html(version: str) -> str:
    sections = [
        ("시작하기", [
            (_kbd("음원"), "사이드바 <b>음원</b> 섹션에서 <b>음원 파일 등록</b>으로 곡(WAV/OGG/MP3)을 불러옵니다. "
                          "파형이 왼쪽 BGM 레인에 표시됩니다."),
            (_kbd("4K") + " " + _kbd("6K"), "상단 툴바에서 편집할 키 모드를 고릅니다. 두 채보는 같은 곡을 공유합니다."),
            (_kbd("내보내기"), "완성되면 선택한 키 모드를 <b>.bms</b>로 내보냅니다. 작업 상태는 "
                              "<b>.slbms</b>로 저장·복원됩니다(자동 저장/복구 지원)."),
        ]),
        ("노트 찍기 · 추가 모드", [
            (_kbd("추가", "F3"), "추가 모드로 전환합니다."),
            ("마우스 <b>클릭</b>", "레인의 격자 칸에 노트를 찍습니다. 다시 우클릭하면 지워집니다."),
            (_kbd("Shift") + " + 클릭", "격자를 무시하고 <b>자유 배치</b>합니다."),
            ("<b>격자</b> 사이드바", "왼쪽 숫자 = 스냅(노트가 놓이는) 격자, 오른쪽 = 참고용 보조 격자."),
        ]),
        ("롱노트", [
            ("빈 칸에서 <b>위·아래 드래그</b>", "드래그한 만큼 길이를 갖는 롱노트를 만듭니다. (추가 모드)"),
            ("끝점 <b>드래그</b>", "롱노트의 머리/꼬리를 잡아 길이를 조절합니다. (추가 모드 전용)"),
            (_kbd("추가", "F3"), "길이 조절은 추가 모드에서만 됩니다. 편집 모드에선 통째로 이동만 됩니다."),
        ]),
        ("선택 · 편집 모드", [
            (_kbd("편집", "F2"), "편집 모드로 전환합니다."),
            ("<b>클릭</b> / <b>드래그</b>", "노트를 선택하거나, 빈 영역을 드래그해 여러 개를 상자 선택합니다."),
            (_kbd("Shift") + " + 클릭", "선택에 노트를 하나씩 추가·제거합니다."),
            (_kbd("Ctrl", "A"), "전체 선택. &nbsp; " + _kbd("Delete") + " 선택 삭제. &nbsp; "
             + _kbd("Ctrl", "C") + " / " + _kbd("Ctrl", "V") + " 복사·붙여넣기."),
        ]),
        ("노트 이동 · 편집 모드", [
            (_kbd("↑") + " " + _kbd("↓"), "격자 한 칸씩 위·아래로 이동합니다."),
            (_kbd("←") + " " + _kbd("→"), "레인을 이동합니다 <b>(4K ↔ 6K ↔ LOAD)</b>. 4K 왼쪽·LOAD 오른쪽은 막힙니다."),
            (_kbd("Ctrl") + " + " + _kbd("↑") + _kbd("↓"), "<b>보조 격자</b> 라인으로 스냅해 이동합니다."),
            (_kbd("Shift") + " + " + _kbd("↑") + _kbd("↓"), "<b>1px씩</b> 미세하게(자유 배치) 이동합니다."),
            (_kbd("Shift") + " + 드래그", "마우스로 <b>자유 배치</b> 이동합니다. (일반 드래그는 격자 스냅)"),
        ]),
        ("재생 · 미리보기", [
            (_kbd("Space"), "재생 / 일시정지. &nbsp;" + _kbd("Home") + " 처음으로. &nbsp;"
             + _kbd("-") + " / " + _kbd("=") + " 1초 뒤·앞으로."),
            ("왼쪽 눈금 <b>클릭</b>", "그 위치로 재생 지점을 옮깁니다(가운데 클릭은 어디서나)."),
            ("<b>재생 속도</b> 게이지", "음정을 유지한 채 배속을 바꿉니다(처리에 잠깐 걸릴 수 있음)."),
            ("<b>녹음</b> 섹션", "재생 중 " + _kbd("Q") + _kbd("W") + _kbd("E") + " … 키로 실시간 입력. "
                                "카운트인·메트로놈·입력 보정 지원."),
        ]),
        ("마디 길이 · 변박", [
            ("왼쪽 눈금 <b>위·아래 드래그</b>", "그 마디 하나의 길이를 격자 칸 단위로 줄이거나 늘립니다(BMS 채널 02)."),
            ("마디 축소 시", "칸을 넘어선 노트는 <b>다음 마디로 자동 이월</b>됩니다(드래그 중 실시간, 되돌리면 복원)."),
        ]),
        ("그 외", [
            (_kbd("Ctrl") + " + 휠 / " + _kbd("Alt") + " + 휠", "세로 / 가로 확대·축소. &nbsp;"
             + _kbd("Shift") + " + 휠 = 좌우 스크롤."),
            (_kbd("Ctrl", "Z") + " / " + _kbd("Ctrl", "Y"), "되돌리기 / 다시하기."),
            ("<b>편집 → 설정</b>", "단축키를 원하는 대로 바꿀 수 있습니다."),
        ]),
    ]
    body = "".join(_section(t, r) for t, r in sections)
    return f"""
    <div style="font-size:20pt; color:{ACCENT};"><b>SlimBMS 사용법</b></div>
    <div style="color:{TEXT_DIM}; font-size:10pt; margin-top:2px;">
        무키음 4K / 6K BMS 채보 에디터 &nbsp;·&nbsp; v{version}
    </div>
    <div style="color:{TEXT_DIM}; font-size:10pt; margin-top:10px;">
        곡을 불러와 격자에 노트를 찍고, 키 모드별로 <b>.bms</b>로 내보내는
        간결한 에디터입니다. 아래는 자주 쓰는 조작입니다.
    </div>
    {body}
    """


class HelpDialog(QDialog):
    """A clean, scrollable usage guide shown from the Help menu."""

    def __init__(self, version: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("사용법")
        self.resize(660, 740)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(12)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.document().setDocumentMargin(20)
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
