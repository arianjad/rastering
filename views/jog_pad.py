"""Jog pad: move the stage by a step in an on-screen direction, go to x, y, or go to user home.

Emits signals only. Directions are as seen on the image (+x right, +y up); the
window turns them into stage axes using the display geometry and calibration.
Two rings of arrows, as in Micro-Manager's stage control: the inner single arrows
move by the fine step, the outer double arrows by the coarse step. Holding an
arrow repeats it; the window skips a repeat while the last step is still moving.
"""
from __future__ import annotations

from typing import Any, Dict

from PyQt5 import QtCore, QtGui, QtWidgets

import theme
from .widgets import NumberBox, label, sentence, set_prop

_ARROWS = {(0, 1): ("up", 90), (-1, 0): ("left", 180), (1, 0): ("right", 0), (0, -1): ("down", 270)}
BUTTON_PX = 30
REPEAT_DELAY_MS = 400      # hold this long before the first repeat
REPEAT_EVERY_MS = 150


def _arrow_icon(angle_deg: float, double: bool = False, size: int = 20) -> QtGui.QIcon:
    """Filled triangle (two for double) pointing at angle_deg (0 = right, 90 = up), ink colour;
    dim when disabled."""
    icon = QtGui.QIcon()
    for mode, key in ((QtGui.QIcon.Normal, "ink"), (QtGui.QIcon.Disabled, "dim")):
        pm = QtGui.QPixmap(size * 2, size * 2)        # drawn at 2x so it stays sharp at 150 %
        pm.setDevicePixelRatio(2.0)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.translate(size / 2, size / 2)
        p.rotate(-angle_deg)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(theme.tokens()[key]))
        s = size * (0.26 if double else 0.34)
        for shift in ((-s * 0.55, s * 0.75) if double else (0.0,)):
            p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(shift + s, 0), QtCore.QPointF(shift - s * 0.65, -s),
                                           QtCore.QPointF(shift - s * 0.65, s)]))
        p.end()
        icon.addPixmap(pm, mode)
    return icon


def _line(v: QtWidgets.QVBoxLayout) -> QtWidgets.QLabel:
    """A hidden, wrapping small line added to `v`."""
    w = label("", role="small")
    w.setWordWrap(True)
    w.hide()
    v.addWidget(w)
    return w


class JogPad(QtWidgets.QWidget):
    jog_requested = QtCore.pyqtSignal(int, int, float)    # screen dx, dy in {-1, 0, 1} (+y up), step [mm]
    go_to_requested = QtCore.pyqtSignal(float, float)     # stage x, y [mm]
    home_requested = QtCore.pyqtSignal()                  # go to the stored user home

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(14)
        v.addLayout(top)

        self.step_box = NumberBox(decimals=3, minimum=0.001, maximum=12.0, unit="mm", step=0.01)
        self.step_box.setValue(0.010)
        self.step_box.setToolTip("How far one press of an inner (single) arrow moves the stage [mm].")
        self.coarse_box = NumberBox(decimals=3, minimum=0.001, maximum=12.0, unit="mm", step=0.1)
        self.coarse_box.setValue(0.100)
        self.coarse_box.setToolTip("How far one press of an outer (double) arrow moves the stage [mm].")

        pad = QtWidgets.QGridLayout()
        pad.setSpacing(2)
        self.arrow_buttons = {}        # fine step, inner ring
        self.coarse_buttons = {}       # coarse step, outer ring
        self._rings = ((self.arrow_buttons, self.step_box, False), (self.coarse_buttons, self.coarse_box, True))
        self._repeating = None         # the arrow whose hold has repeated (its release adds no step)
        for ring, (buttons, box, coarse) in enumerate(self._rings, start=1):
            box.valueChanged.connect(self._retip)
            for (dx, dy), (name, _angle) in _ARROWS.items():
                b = QtWidgets.QToolButton()
                b.setProperty("kind", "icon")
                b.setIconSize(QtCore.QSize(20, 20))
                b.setFixedSize(BUTTON_PX, BUTTON_PX)
                b.setAccessibleName(f"Jog {name}{' coarse' if coarse else ''}")
                b.setAutoRepeat(True)
                b.setAutoRepeatDelay(REPEAT_DELAY_MS)
                b.setAutoRepeatInterval(REPEAT_EVERY_MS)
                b.clicked.connect(lambda _=False, b=b, dx=dx, dy=dy, box=box: self._clicked(b, dx, dy, box))
                buttons[(dx, dy)] = b
                pad.addWidget(b, 2 - ring * dy, 2 + ring * dx)
        for i in range(5):
            pad.setColumnMinimumWidth(i, BUTTON_PX)
            pad.setRowMinimumHeight(i, BUTTON_PX)
        top.addLayout(pad)

        form = QtWidgets.QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        self.x_box = NumberBox(decimals=3, minimum=-1000, maximum=1000, unit="mm", step=0.1)
        self.y_box = NumberBox(decimals=3, minimum=-1000, maximum=1000, unit="mm", step=0.1)
        self.x_box.setToolTip("Stage x to go to [mm].")
        self.y_box.setToolTip("Stage y to go to [mm].")
        rows = (("Fine", self.step_box), ("Coarse", self.coarse_box), ("x", self.x_box), ("y", self.y_box))
        for r, (name, box) in enumerate(rows):
            form.addWidget(label(name), r, 0)
            form.addWidget(box, r, 1)
        form.setColumnStretch(1, 1)
        top.addLayout(form, 1)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.go_btn = QtWidgets.QPushButton("Go to x, y")
        self.go_btn.setToolTip("Move the stage to x, y above [mm].")
        self.go_btn.clicked.connect(lambda: self.go_to_requested.emit(self.x_box.value(), self.y_box.value()))
        self.home_btn = QtWidgets.QPushButton("Go to user home")
        self.home_btn.setToolTip("Move the stage to the stored user home position.")
        self.home_btn.clicked.connect(self.home_requested.emit)
        row.addWidget(self.go_btn)
        row.addWidget(self.home_btn)
        row.addStretch(1)
        v.addLayout(row)

        self.answer = _line(v)         # the last hand move's warning, next to the buttons
        self.reason = _line(v)
        self.note = _line(v)
        self._restyle()
        self._retip()
        theme.changed().connect(self._restyle)

    def _arrows(self):
        """(button, direction, step box, coarse) for every arrow."""
        return [(b, d, box, coarse) for buttons, box, coarse in self._rings for d, b in buttons.items()]

    def _restyle(self) -> None:
        for b, (dx, dy), _box, coarse in self._arrows():
            b.setIcon(_arrow_icon(_ARROWS[(dx, dy)][1], double=coarse))

    def _retip(self, *_):
        for b, (dx, dy), box, _coarse in self._arrows():
            b.setToolTip(f"Move the spot {_ARROWS[(dx, dy)][0]} on the image by {float(box.value()):.3f} mm. "
                         "Hold to repeat.")

    def _clicked(self, b, dx: int, dy: int, box: NumberBox) -> None:
        """A click, or a repeat while held. Qt also clicks on the release after repeats; that
        one adds no step, so a hold moves exactly by its repeats."""
        if b.isDown():
            self._repeating = b
        elif self._repeating is b:
            self._repeating = None
            return
        self.jog_requested.emit(dx, dy, float(box.value()))

    def step_mm(self) -> float:
        """The fine step [mm]."""
        return float(self.step_box.value())

    def coarse_mm(self) -> float:
        return float(self.coarse_box.value())

    def set_step_mm(self, mm: float) -> None:
        self.step_box.setValue(float(mm))

    def set_coarse_mm(self, mm: float) -> None:
        self.coarse_box.setValue(float(mm))

    def held(self) -> bool:
        """An arrow is being held down (its clicks now are repeats)."""
        return any(b.isDown() for b, *_ in self._arrows())

    def settings(self) -> Dict[str, Any]:
        return {"step_mm": self.step_mm(), "coarse_mm": self.coarse_mm()}

    def apply_settings(self, d: Dict[str, Any]) -> None:
        for key, box in (("step_mm", self.step_box), ("coarse_mm", self.coarse_box)):
            if d.get(key) is not None:
                box.setValue(float(d[key]))

    def set_go_to(self, x_mm: float, y_mm: float) -> None:
        """Fill the x, y boxes (e.g. from a click on the image) without moving."""
        self.x_box.set_quietly(float(x_mm))
        self.y_box.set_quietly(float(y_mm))

    def set_moves_enabled(self, on: bool, reason: str = "") -> None:
        """Lock the move buttons and say why."""
        for b in [b for b, *_ in self._arrows()] + [self.go_btn, self.home_btn]:
            b.setEnabled(bool(on))
        self.reason.setText(reason)
        self.reason.setVisible(not on and bool(reason))

    def show_answer(self, text: str, red: bool = False) -> None:
        """What the last hand move cost or why it did not run, under the buttons ("" hides it)."""
        self.answer.setText(sentence(text))
        set_prop(self.answer, "tone", "fault" if red else "")
        self.answer.setVisible(bool(text))

    def show_note(self, text: str) -> None:
        """One quiet line under the pad ("" hides it)."""
        self.note.setText(text)
        self.note.setVisible(bool(text))
