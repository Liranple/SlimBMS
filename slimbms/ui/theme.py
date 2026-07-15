"""A single cohesive dark theme for the whole app.

The chart canvas paints itself in dark tones; without a matching theme the
surrounding native widgets (toolbar, sidebar, menus) render light, which reads
as cluttered and clashes badly. Applying one Fusion-based palette + stylesheet
makes the window feel like one surface, and bumps contrast, focus rings and hit
targets for accessibility.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# Palette — kept in step with the canvas colours in ``chart_view``.
APP_BG = "#17171c"       # window backdrop
PANEL = "#212129"        # toolbar / sidebar surfaces
CANVAS = "#1e1e24"       # chart area (matches ChartView C_BG)
FIELD = "#2a2a33"        # text inputs
BORDER = "#33333d"       # hairlines
BORDER_STRONG = "#44444f"
TEXT = "#d6d6de"         # primary text
TEXT_DIM = "#9a9aa6"     # secondary / hints
ACCENT = "#6fd0ff"       # selection / focus (matches C_SELECT)
ACCENT_INK = "#0c1116"   # text on an accent fill
DANGER = "#ff6b81"

FONT_STACK = '"Segoe UI", "Malgun Gothic", "Noto Sans KR", sans-serif'


def _palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.Window, QColor(APP_BG))
    p.setColor(QPalette.WindowText, QColor(TEXT))
    p.setColor(QPalette.Base, QColor(FIELD))
    p.setColor(QPalette.AlternateBase, QColor(PANEL))
    p.setColor(QPalette.Text, QColor(TEXT))
    p.setColor(QPalette.Button, QColor(PANEL))
    p.setColor(QPalette.ButtonText, QColor(TEXT))
    p.setColor(QPalette.ToolTipBase, QColor(PANEL))
    p.setColor(QPalette.ToolTipText, QColor(TEXT))
    p.setColor(QPalette.Highlight, QColor(ACCENT))
    p.setColor(QPalette.HighlightedText, QColor(ACCENT_INK))
    p.setColor(QPalette.PlaceholderText, QColor(TEXT_DIM))
    disabled = QColor(TEXT_DIM)
    for grp in (QPalette.Disabled,):
        p.setColor(grp, QPalette.WindowText, disabled)
        p.setColor(grp, QPalette.Text, disabled)
        p.setColor(grp, QPalette.ButtonText, disabled)
    return p


STYLESHEET = f"""
* {{
    font-family: {FONT_STACK};
    font-size: 10pt;
}}
QMainWindow, QWidget {{
    background: {APP_BG};
    color: {TEXT};
}}

/* Toolbar ---------------------------------------------------------------- */
QToolBar {{
    background: {PANEL};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    spacing: 4px;
}}
QToolBar QLabel {{ color: {TEXT_DIM}; padding: 0 2px; }}
QToolBar::separator {{
    background: {BORDER};
    width: 1px;
    margin: 4px 8px;
}}
QToolButton {{
    background: transparent;
    color: {TEXT};
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 10px;
    min-height: 20px;
}}
QToolButton:hover {{ background: {FIELD}; }}
QToolButton:pressed {{ background: {BORDER}; }}
QToolButton:checked {{
    background: rgba(111, 208, 255, 0.16);
    border: 1px solid {ACCENT};
    color: {ACCENT};
}}
QToolButton:focus {{ border: 1px solid {ACCENT}; }}
QToolButton#Primary {{
    background: rgba(111, 208, 255, 0.16);
    border: 1px solid {ACCENT};
    color: {ACCENT};
    font-weight: 600;
}}
QToolButton#Primary:hover {{ background: rgba(111, 208, 255, 0.26); }}

/* Buttons ---------------------------------------------------------------- */
QPushButton {{
    background: {FIELD};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 7px 12px;
    min-height: 18px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton:checked {{
    background: rgba(111, 208, 255, 0.16);
    border-color: {ACCENT};
    color: {ACCENT};
}}

/* Inputs ----------------------------------------------------------------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {FIELD};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 5px 8px;
    min-height: 20px;
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_INK};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_INK};
    outline: none;
}}

/* Sidebar ---------------------------------------------------------------- */
QWidget#Sidebar {{
    background: {PANEL};
    border-left: 1px solid {BORDER};
}}
QLabel#Section {{
    color: {ACCENT};
    font-size: 8.5pt;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 2px 0;
}}
QLabel#Hint {{ color: {TEXT_DIM}; font-size: 9pt; }}
QFrame#HLine {{ background: {BORDER}; border: none; max-height: 1px; }}

/* Menus ------------------------------------------------------------------ */
QMenuBar {{ background: {PANEL}; color: {TEXT}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ background: transparent; padding: 6px 10px; border-radius: 4px; }}
QMenuBar::item:selected {{ background: {FIELD}; }}
QMenu {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; color: {ACCENT_INK}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}

/* Scroll area + bars ----------------------------------------------------- */
QScrollArea {{ border: 1px solid {BORDER}; background: {CANVAS}; }}
QScrollArea > QWidget > QWidget {{ background: {CANVAS}; }}
QScrollBar:vertical {{ background: {CANVAS}; width: 12px; margin: 0; }}
QScrollBar:horizontal {{ background: {CANVAS}; height: 12px; margin: 0; }}
QScrollBar::handle {{ background: {BORDER_STRONG}; border-radius: 6px; min-height: 28px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* Status bar + misc ------------------------------------------------------ */
QStatusBar {{ background: {PANEL}; color: {TEXT_DIM}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}
QToolTip {{
    background: {PANEL}; color: {TEXT};
    border: 1px solid {ACCENT}; border-radius: 4px; padding: 4px 6px;
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setPalette(_palette())
    font = QFont()
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(STYLESHEET)
