"""Small reusable sidebar widgets."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
)
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

_UNLIMITED = 16_777_215


class NoWheelSpinBox(QSpinBox):
    """A spin box that ignores the mouse wheel (so scrolling the sidebar never
    nudges values) and shows no up/down buttons."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setButtonSymbols(QSpinBox.NoButtons)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):  # noqa: N802
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):  # noqa: N802
        event.ignore()


class CollapsibleSection(QWidget):
    """A titled section whose body expands/collapses with a smooth animation
    when its header is clicked. Add content with :meth:`add_widget` /
    :meth:`add_layout`."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._expanded = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.header = QToolButton()
        self.header.setObjectName("SectionHeader")
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(True)
        self.header.setArrowType(Qt.DownArrow)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.clicked.connect(self._toggle)
        outer.addWidget(self.header)

        self.content = QWidget()
        self.content.setObjectName("SectionContent")
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(12, 8, 8, 12)
        self.body.setSpacing(8)
        outer.addWidget(self.content)

        self._anim = QPropertyAnimation(self.content, b"maximumHeight", self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._anim.finished.connect(self._on_anim_done)

    # -- content ------------------------------------------------------------ #

    def add_widget(self, w) -> None:
        self.body.addWidget(w)

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)

    # -- expand / collapse -------------------------------------------------- #

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self.header.setArrowType(Qt.DownArrow if self._expanded else Qt.RightArrow)
        self._anim.stop()
        start = self.content.maximumHeight()
        if self._expanded:
            # Uncap first so sizeHint reflects the real content height.
            self.content.setMaximumHeight(_UNLIMITED)
            end = self.content.sizeHint().height()
            self.content.setMaximumHeight(start)
        else:
            end = 0
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start()

    def _on_anim_done(self) -> None:
        # Remove the cap while open so the section can grow with its content.
        if self._expanded:
            self.content.setMaximumHeight(_UNLIMITED)
