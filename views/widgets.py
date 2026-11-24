"""Small widgets the view parts share; the window may use them too."""
from __future__ import annotations

from PyQt5 import QtCore, QtWidgets


def set_prop(widget: QtWidgets.QWidget, name: str, value) -> None:
    """Set a dynamic property the stylesheet selects on, and restyle if it changed."""
    if widget.property(name) != value:
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()


def label(text: str = "", role: str | None = None, tone: str | None = None) -> QtWidgets.QLabel:
    """QLabel with a font role (theme.ROLES) and a colour tone (label, fault, spot, path)."""
    w = QtWidgets.QLabel(text)
    if role:
        w.setProperty("role", role)
    if tone:
        w.setProperty("tone", tone)
    return w


class Section(QtWidgets.QWidget):
    """A heading (Bahnschrift) with a hairline to its right, then the content
    in `body` (a QGridLayout: labels in column 0, values to the right)."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(10)
        self.title = label(title, role="heading")
        head.addWidget(self.title)
        line = QtWidgets.QFrame()
        line.setProperty("role", "rule")
        line.setFixedHeight(1)
        line.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        head.addWidget(line, 1, QtCore.Qt.AlignVCenter)
        v.addLayout(head)
        self.body = QtWidgets.QGridLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setHorizontalSpacing(10)
        self.body.setVerticalSpacing(6)
        v.addLayout(self.body)


class _NoWheelUnlessFocused(QtCore.QObject):
    """Scrolling a panel must not change a value the pointer passes over."""

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Wheel and not obj.hasFocus():
            event.ignore()
            return True
        return False


_wheel_guard = None


def _guard_wheel(w: QtWidgets.QWidget) -> None:
    global _wheel_guard
    if _wheel_guard is None:
        _wheel_guard = _NoWheelUnlessFocused()
    w.setFocusPolicy(QtCore.Qt.StrongFocus)
    w.installEventFilter(_wheel_guard)


class NumberBox(QtWidgets.QDoubleSpinBox):
    """Number entry: right-aligned, unit suffix, no arrow buttons (type, arrow keys
    or the wheel once focused), and valueChanged only on Enter or leaving the box."""

    def __init__(self, decimals: int = 3, minimum: float = 0.0, maximum: float = 1e6,
                 unit: str = "", step: float = 1.0, parent=None):
        super().__init__(parent)
        self.setDecimals(decimals)
        self.setRange(minimum, maximum)
        self.setSingleStep(step)
        if unit:
            self.setSuffix(f" {unit}")
        self.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.setKeyboardTracking(False)
        self.setAccelerated(True)
        _guard_wheel(self)

    def set_quietly(self, value: float) -> None:
        """Show a value without emitting valueChanged (for values the program sets)."""
        self.blockSignals(True)
        self.setValue(value)
        self.blockSignals(False)


class IntBox(QtWidgets.QSpinBox):
    """Integer twin of NumberBox."""

    def __init__(self, minimum: int = 0, maximum: int = 1_000_000, unit: str = "",
                 step: int = 1, parent=None):
        super().__init__(parent)
        self.setRange(minimum, maximum)
        self.setSingleStep(step)
        if unit:
            self.setSuffix(f" {unit}")
        self.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.setKeyboardTracking(False)
        _guard_wheel(self)

    def set_quietly(self, value: int) -> None:
        self.blockSignals(True)
        self.setValue(int(value))
        self.blockSignals(False)


def quiet_slider(orientation=QtCore.Qt.Horizontal) -> QtWidgets.QSlider:
    """Slider that ignores the wheel unless focused (same reason as NumberBox)."""
    s = QtWidgets.QSlider(orientation)
    _guard_wheel(s)
    return s
