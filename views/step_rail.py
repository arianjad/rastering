"""Step rail: Setup, Pattern, Run in work order, each marked done or not.

Done is a filled badge with a tick, not done a ring with the step number, so it
reads without colour. The Pattern step carries the path colour, like the
pattern on the image.
"""
from __future__ import annotations

from typing import Sequence

from PyQt5 import QtCore, QtGui, QtWidgets

import theme

STEPS = ("Setup", "Pattern", "Run")
RUN_NOTES = {"off": "Off", "running": "Running", "armed": "Armed for BLACS", "local": "Held at GUI"}


def progress_notes(camera_ok: bool, calibration: str, pattern_points: int, raster_state: str):
    """[(done, note)] for Setup, Pattern, Run from plain status values.
    calibration: "none" | "collecting" | "loaded" | "stale" (as the control bar)."""
    if not camera_ok:
        setup = (False, "Camera off")
    elif calibration == "loaded":
        setup = (True, "Ready")
    elif calibration == "stale":
        setup = (False, "Calibration stale")
    elif calibration == "collecting":
        setup = (False, "Calibrating")
    else:
        setup = (False, "Needs calibration")
    n = int(pattern_points or 0)
    pattern = (n > 0, f"{n} point{'s' if n != 1 else ''}" if n > 0 else "No pattern yet")
    run = (False, RUN_NOTES.get(raster_state, str(raster_state)))
    return [setup, pattern, run]


class StepButton(QtWidgets.QAbstractButton):
    def __init__(self, number: int, name: str, accent: str = "ink", parent=None):
        super().__init__(parent)
        self.number, self.name, self.accent = number, name, accent
        self.note = ""
        self.done = False
        self.setCheckable(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setAttribute(QtCore.Qt.WA_Hover, True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.setCursor(QtCore.Qt.PointingHandCursor)

    def set_state(self, done: bool, note: str) -> None:
        if (done, note) != (self.done, self.note):
            self.done, self.note = bool(done), str(note)
            self.setAccessibleDescription(("done: " if done else "not done: ") + note)
            self.updateGeometry()
            self.update()

    _PAD, _BADGE, _GAP = 10, 22, 8

    def sizeHint(self):
        name_fm = QtGui.QFontMetrics(theme.font("step"))
        note_fm = QtGui.QFontMetrics(theme.font("small"))
        w = 2 * self._PAD + max(self._BADGE + self._GAP + name_fm.horizontalAdvance(self.name),
                                note_fm.horizontalAdvance(self.note or "x"))
        h = 8 + max(self._BADGE, name_fm.height()) + 2 + note_fm.height() + 8 + 3
        return QtCore.QSize(w, h)

    def minimumSizeHint(self):
        return QtCore.QSize(100, self.sizeHint().height())

    def paintEvent(self, event):
        t = theme.tokens()
        accent = QtGui.QColor(t[self.accent])
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self.rect()
        if self.isChecked():
            p.fillRect(r, QtGui.QColor(t["panel"]))
        elif self.underMouse():
            p.fillRect(r, QtGui.QColor(t["hover"]))

        # badge, then the name beside it, the note under both
        name_f, note_f = theme.font("step"), theme.font("small")
        nfm, sfm = QtGui.QFontMetrics(name_f), QtGui.QFontMetrics(note_f)
        d = self._BADGE
        line_h = max(d, nfm.height())
        bx, by = self._PAD, 8 + (line_h - d) // 2
        badge = QtCore.QRectF(bx, by, d, d)
        if self.done:
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(accent)
            p.drawEllipse(badge)
            tick = QtGui.QPainterPath()
            tick.moveTo(bx + d * 0.27, by + d * 0.52)
            tick.lineTo(bx + d * 0.44, by + d * 0.69)
            tick.lineTo(bx + d * 0.75, by + d * 0.33)
            pen = QtGui.QPen(QtGui.QColor(t["ground"]), 2.4)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawPath(tick)
        else:
            ring = QtGui.QColor(t["ink"] if self.isChecked() else t["label"])
            p.setPen(QtGui.QPen(ring, 1.5))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawEllipse(badge.adjusted(0.75, 0.75, -0.75, -0.75))
            f = theme.font("small")
            f.setWeight(QtGui.QFont.DemiBold)
            p.setFont(f)
            p.drawText(badge, QtCore.Qt.AlignCenter, str(self.number))

        tx = bx + d + self._GAP
        p.setFont(name_f)
        p.setPen(QtGui.QColor(t["ink"] if self.isChecked() or self.underMouse() else t["label"]))
        p.drawText(QtCore.QRect(tx, 8, r.width() - tx - self._PAD, line_h),
                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, self.name)
        p.setFont(note_f)
        p.setPen(QtGui.QColor(t["label"]))
        note_w = r.width() - 2 * self._PAD
        note = sfm.elidedText(self.note, QtCore.Qt.ElideRight, note_w)
        p.drawText(QtCore.QRect(bx, 8 + line_h + 2, note_w, sfm.height()),
                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, note)

        # current step: a bar along the bottom in the step's accent
        if self.isChecked():
            p.fillRect(QtCore.QRect(0, r.height() - 3, r.width(), 3), accent)
        # keyboard focus
        if self.hasFocus():
            pen = QtGui.QPen(QtGui.QColor(t["ink"]), 1.5, QtCore.Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawRoundedRect(QtCore.QRectF(r).adjusted(2, 2, -2, -5), 3, 3)
        p.end()


class StepRail(QtWidgets.QWidget):
    """Three step buttons. step_selected(index) when the operator picks one."""

    step_selected = QtCore.pyqtSignal(int)

    def __init__(self, steps: Sequence[str] = STEPS, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)
        self.buttons = []
        for i, name in enumerate(steps):
            b = StepButton(i + 1, name, accent="path" if name == "Pattern" else "ink")
            self._group.addButton(b, i)
            lay.addWidget(b, 1)
            self.buttons.append(b)
        self.buttons[0].setChecked(True)
        self._group.idClicked.connect(self.step_selected.emit)
        theme.changed().connect(self.update)

    def current(self) -> int:
        return self._group.checkedId()

    def set_current(self, index: int) -> None:
        """Mark a step as the one shown (no signal)."""
        self.buttons[int(index)].setChecked(True)

    def set_step(self, index: int, done: bool, note: str = "") -> None:
        self.buttons[int(index)].set_state(done, note)

    def show_progress(self, camera_ok: bool, calibration: str, pattern_points: int,
                      raster_state: str) -> None:
        """Done marks and notes for all steps from plain status values."""
        for i, (done, note) in enumerate(progress_notes(camera_ok, calibration, pattern_points,
                                                        raster_state)):
            self.set_step(i, done, note)
