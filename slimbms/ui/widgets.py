"""Small reusable sidebar widgets."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .palette import ACCENT

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


class NoWheelComboBox(QComboBox):
    """A combo box that ignores the mouse wheel when closed, so scrolling the
    sidebar never changes the selection (the wheel scrolls the panel instead).
    The dropdown popup still scrolls normally once opened."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
        # Let the body shrink all the way to 0 during the collapse animation so
        # it slides shut smoothly instead of snapping when a child's minimum
        # height would otherwise resist the last few pixels.
        self.content.setMinimumHeight(0)
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(12, 8, 8, 12)
        self.body.setSpacing(8)
        outer.addWidget(self.content)

        self._anim = QPropertyAnimation(self.content, b"maximumHeight", self)
        self._anim.setDuration(210)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
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
        # Animate over the *actual* content height in both directions (when open
        # the max height is uncapped, so read sizeHint, not maximumHeight).
        full = self.content.sizeHint().height()
        if self._expanded:
            start, end = 0, full
        else:
            start, end = full, 0
        self.content.setMaximumHeight(start)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start()

    def _on_anim_done(self) -> None:
        # Uncap while open so the section can still grow with its content.
        if self._expanded:
            self.content.setMaximumHeight(_UNLIMITED)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        """Set the open/closed state instantly (no animation) — used when
        restoring a saved layout."""
        if bool(expanded) == self._expanded:
            return
        self._expanded = bool(expanded)
        self.header.setChecked(self._expanded)
        self.header.setArrowType(Qt.DownArrow if self._expanded else Qt.RightArrow)
        self._anim.stop()
        self.content.setMaximumHeight(_UNLIMITED if self._expanded else 0)


class DragValue(QWidget):
    """A compact 'drag to adjust' control, like a volume knob: press and slide
    the mouse left or right to change a numeric value in fixed steps. An icon
    sits on the left and the current value is shown on the right. Vertical
    movement is ignored, so the mouse only needs to travel sideways."""

    changed = Signal(float)
    GROOVE_L = 28   # groove left edge (matches paintEvent)
    GROOVE_R_PAD = 52  # space reserved on the right for the value text

    def __init__(self, icon: str, minimum: float, maximum: float,
                 step: float, value: float, parent=None):
        super().__init__(parent)
        self._icon = icon
        self._min = float(minimum)
        self._max = float(maximum)
        self._step = float(step)
        self._value = self._quant(value)
        self._dragging = False
        self.setCursor(Qt.PointingHandCursor)
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

    def _value_at(self, x: float) -> float:
        """The value the groove maps the widget-x position to (absolute, so the
        handle follows the mouse)."""
        gx0 = self.GROOVE_L
        gx1 = self.width() - self.GROOVE_R_PAD
        if gx1 <= gx0:
            return self._value
        frac = max(0.0, min(1.0, (x - gx0) / (gx1 - gx0)))
        return self._min + frac * (self._max - self._min)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self.set_value(self._value_at(event.position().x()))
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self.set_value(self._value_at(event.position().x()))
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._dragging = False

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
        gx0, gx1 = self.GROOVE_L, w - self.GROOVE_R_PAD
        if gx1 <= gx0:
            p.end()
            return
        span = self._max - self._min
        frac = (self._value - self._min) / span if span > 0 else 0.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#31313b"))
        p.drawRoundedRect(QRect(gx0, int(cy) - 3, gx1 - gx0, 6), 3, 3)
        fill_w = int((gx1 - gx0) * frac)
        p.setBrush(QColor(ACCENT))
        p.drawRoundedRect(QRect(gx0, int(cy) - 3, fill_w, 6), 3, 3)
        p.setBrush(QColor("#dbe7f2"))
        p.drawEllipse(QPoint(gx0 + fill_w, int(cy)), 6, 6)
        p.end()
