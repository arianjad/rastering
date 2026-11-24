"""Jog pad: move the stage by a step in an on-screen direction, go to x, y, or go to user home.

Emits signals only. Directions are as seen on the image (+x right, +y up); the
window turns them into stage axes using the display geometry and calibration.
"""
from __future__ import annotations

from PyQt5 import QtCore, QtGui, QtWidgets

import theme
from .widgets import NumberBox, label

_ARROWS = {(0, 1): ("up", 90), (-1, 0): ("left", 180), (1, 0): ("right", 0), (0, -1): ("down", 270)}
BUTTON_PX = 42


def _arrow_icon(angle_deg: float, size: int = 24) -> QtGui.QIcon:
    """Filled triangle pointing at angle_deg (0 = right, 90 = up), ink colour; dim when disabled."""
    icon = QtGui.QIcon()
    for mode, key in ((QtGui.QIcon.Normal, "ink"), (QtGui.QIcon.Disabled, "dim")):
        pm = QtGui.QPixmap(size * 2, size * 2)        # drawn at 2x so it stays sharp at 150 %
        pm.setDevicePixelRatio(2.0)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.translate(size / 2, size / 2)
        p.rotate(-angle_deg)
        tri = QtGui.QPolygonF([QtCore.QPointF(size * 0.34, 0), QtCore.QPointF(-size * 0.22, -size * 0.34),
                               QtCore.QPointF(-size * 0.22, size * 0.34)])
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(theme.tokens()[key]))
        p.drawPolygon(tri)
        p.end()
        icon.addPixmap(pm, mode)
    return icon


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
        top.setSpacing(18)
        v.addLayout(top)

        pad = QtWidgets.QGridLayout()
        pad.setSpacing(3)
        self.arrow_buttons = {}
        for (dx, dy), (name, _angle) in _ARROWS.items():
            b = QtWidgets.QToolButton()
            b.setIconSize(QtCore.QSize(24, 24))
            b.setFixedSize(BUTTON_PX, BUTTON_PX)
            b.setAccessibleName(f"Jog {name}")
            b.clicked.connect(lambda _=False, dx=dx, dy=dy: self.jog_requested.emit(dx, dy, self.step_mm()))
            self.arrow_buttons[(dx, dy)] = b
            pad.addWidget(b, 1 - dy, 1 + dx)
        pad.setColumnMinimumWidth(1, BUTTON_PX)
        pad.setRowMinimumHeight(1, BUTTON_PX)
        top.addLayout(pad)

        form = QtWidgets.QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        self.step_box = NumberBox(decimals=3, minimum=0.001, maximum=12.0, unit="mm", step=0.01)
        self.step_box.setValue(0.010)
        self.step_box.setToolTip("How far one arrow press moves the stage [mm].")
        self.step_box.valueChanged.connect(self._retip)
        self.x_box = NumberBox(decimals=3, minimum=-1000, maximum=1000, unit="mm", step=0.1)
        self.y_box = NumberBox(decimals=3, minimum=-1000, maximum=1000, unit="mm", step=0.1)
        self.x_box.setToolTip("Stage x to go to [mm].")
        self.y_box.setToolTip("Stage y to go to [mm].")
        for r, (name, box) in enumerate((("Step", self.step_box), ("x", self.x_box), ("y", self.y_box))):
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

        self.reason = label("", role="small")
        self.reason.setWordWrap(True)
        self.reason.hide()
        v.addWidget(self.reason)
        self._restyle()
        self._retip()
        theme.changed().connect(self._restyle)

    def _restyle(self) -> None:
        for (dx, dy), b in self.arrow_buttons.items():
            b.setIcon(_arrow_icon(_ARROWS[(dx, dy)][1]))

    def _retip(self, *_):
        for (dx, dy), b in self.arrow_buttons.items():
            b.setToolTip(f"Move the spot {_ARROWS[(dx, dy)][0]} on the image by {self.step_mm():.3f} mm")

    def step_mm(self) -> float:
        return float(self.step_box.value())

    def set_step_mm(self, mm: float) -> None:
        self.step_box.set_quietly(float(mm))
        self._retip()

    def set_go_to(self, x_mm: float, y_mm: float) -> None:
        """Fill the x, y boxes (e.g. from a click on the image) without moving."""
        self.x_box.set_quietly(float(x_mm))
        self.y_box.set_quietly(float(y_mm))

    def set_moves_enabled(self, on: bool, reason: str = "") -> None:
        """Lock the move buttons (e.g. while a raster is active) and say why."""
        for b in list(self.arrow_buttons.values()) + [self.go_btn, self.home_btn]:
            b.setEnabled(bool(on))
        self.reason.setText(reason)
        self.reason.setVisible(not on and bool(reason))
