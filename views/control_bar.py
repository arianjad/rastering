"""Control bar: who moves the stage, where it is, and the run's vital signs, in one row.

Input is plain values through one method, set_status(); it never raises (a raise
in a Qt slot would abort the GUI) and never emits signals.
"""
from __future__ import annotations

from typing import Optional, Tuple

from PyQt5 import QtCore, QtWidgets

from .widgets import label, set_prop

# raster_state words are the BLACS menu's (bbx_zmq/menus/rastering.py): off, armed, local, running.
# Each: (word on the block, look, tooltip). Looks differ by shape, not only colour:
# off = thin outline, auto = solid block, held = thick double outline.
STATE_WORDS = {
    "off": ("OFF", "off", "No raster. The stage moves only when you move it."),
    "running": ("RUNNING", "auto", "A continuous raster started at this GUI moves the stage."),
    "armed": ("ARMED FOR BLACS", "auto",
              "BLACS moves the stage: one point per group of shots."),
    "local": ("HELD AT GUI", "held",
              "Armed, but held here: BLACS leaves it alone until you hand it back."),
}

CALIBRATION_STATES = ("none", "collecting", "loaded", "stale")

_KEEP = object()   # set_status: argument not given, keep what is shown


def state_words(state: str) -> Tuple[str, str, str]:
    """(word, look, tooltip) for a raster state; an unknown state shows as itself."""
    return STATE_WORDS.get(state, (str(state).upper(), "off", f"Raster state {state!r}"))


def point_words(index: Optional[int], total: Optional[int]) -> Tuple[str, str]:
    """('12 of 48', tooltip) from a 0-based point index (the shot file's point_index)."""
    if not total or total <= 0:
        return "—", "No path armed."
    if index is None or index < 0:
        return f"— of {total}", f"Path of {total} points armed; no point taken yet."
    return (f"{index + 1} of {total}",
            f"Point {index + 1} of {total} (point_index {index} in the shot file, which counts from 0).")


def calibration_words(state: str, clicks: Optional[Tuple[int, int]] = None) -> Tuple[str, str, str]:
    """(text, tone, tooltip) for the calibration state."""
    if state == "collecting":
        n, m = clicks if clicks else (0, 3)
        return (f"{n} of {m} clicks", "",
                "Calibrating: click the laser spot on the image at each stage position.")
    if state == "loaded":
        return "Ready", "", "Image px map to stage mm."
    if state == "stale":
        return ("Stale: camera changed", "fault",
                "The camera region or display changed since this calibration was made, so image "
                "clicks may land in the wrong place. Restore the camera settings or calibrate again.")
    return "None", "label", "No calibration: calibrate in Setup before running a pattern."


def fps_words(fps: Optional[float]) -> Tuple[str, str]:
    """(text, tone) for the camera frame rate; None means no frames are arriving."""
    if fps is None:
        return "No frames", "fault"
    return f"{fps:.1f} fps", ""


def mm_words(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.3f}"


class _Field(QtWidgets.QWidget):
    """Caption above a value."""

    def __init__(self, caption: str, value_role: str = "value", parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.caption = label(caption, role="caption")
        self.value = label("—", role=value_role)
        lay.addWidget(self.caption)
        lay.addStretch(1)                 # captions share the top line, values the bottom
        lay.addWidget(self.value)

    def show_value(self, text: str, tone: str = "", tip: str = "") -> None:
        if self.value.text() != text:
            self.value.setText(text)
        set_prop(self.value, "tone", tone)
        self.setToolTip(tip)


class ControlBar(QtWidgets.QWidget):
    """One row across the top of the window.

    set_status(state=, x_mm=, y_mm=, point_index=, path_len=, calibration=,
               cal_clicks=, shots_per_point=, pattern_edited=, camera_fps=)
    Every argument is optional; one left out keeps what is shown."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("controlBar")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 9)
        lay.setSpacing(26)

        # The state block: the one bold element of the window.
        self._state_field = _Field("Raster")
        lay.addWidget(self._state_field)
        old = self._state_field.value
        self.state_block = label("OFF", role="state")
        self.state_block.setObjectName("stateBlock")
        self.state_block.setAlignment(QtCore.Qt.AlignCenter)
        self._state_field.layout().replaceWidget(old, self.state_block)
        self._state_field.layout().setSpacing(4)
        old.deleteLater()
        self._state_field.value = self.state_block

        # Stage position, in the spot colour like the crosshair on the image.
        pos = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(pos)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(2)
        pl.addWidget(label("Stage position", role="caption"))
        pl.addStretch(1)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.x_value = label("—", role="position", tone="spot")
        self.y_value = label("—", role="position", tone="spot")
        for name, val in (("X", self.x_value), ("Y", self.y_value)):
            val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignBaseline)
            row.addWidget(label(name, role="value", tone="label"), 0, QtCore.Qt.AlignBaseline)
            row.addWidget(val, 0, QtCore.Qt.AlignBaseline)
            row.addWidget(label("mm", role="value", tone="label"), 0, QtCore.Qt.AlignBaseline)
            row.addSpacing(12)
        pl.addLayout(row)
        lay.addWidget(pos)

        self.point = _Field("Point")
        lay.addWidget(self.point)
        self.calibration = _Field("Calibration")
        lay.addWidget(self.calibration)
        self.shots = _Field("Shots per point")
        lay.addWidget(self.shots)

        self.edited_notice = label("Pattern edited since arming.\nRe-arm to run it.", role="small")
        self.edited_notice.setObjectName("editedNotice")
        self.edited_notice.setToolTip("The pattern on the image is not the one armed: "
                                      "the armed path still runs until you re-arm.")
        self.edited_notice.hide()
        lay.addWidget(self.edited_notice)

        lay.addStretch(1)
        self.camera = _Field("Camera")
        self.camera.value.setAlignment(QtCore.Qt.AlignRight)
        self.camera.caption.setAlignment(QtCore.Qt.AlignRight)
        lay.addWidget(self.camera)

        self._status = dict(state="off", x_mm=None, y_mm=None, point_index=None, path_len=0,
                            calibration="none", cal_clicks=None, shots_per_point=None,
                            pattern_edited=False, camera_fps=None)
        self._fix_widths()
        self._show()

    def status(self) -> dict:
        """The values shown now (a copy)."""
        return dict(self._status)

    def set_status(self, *, state=_KEEP, x_mm=_KEEP, y_mm=_KEEP, point_index=_KEEP,
                   path_len=_KEEP, calibration=_KEEP, cal_clicks=_KEEP, shots_per_point=_KEEP,
                   pattern_edited=_KEEP, camera_fps=_KEEP) -> None:
        """state: "off" | "running" | "armed" | "local" (the menu's raster_state).
        x_mm, y_mm: stage position [mm], None = unknown.
        point_index: 0-based point the stage was last stepped to (-1/None = none yet);
        path_len: points in the armed path (0 = none; ping-pong counts the way back).
        calibration: "none" | "collecting" | "loaded" | "stale"; cal_clicks: (done, needed).
        shots_per_point: int or None. pattern_edited: the pattern on screen differs from
        the armed one. camera_fps: frames/s, None = no frames arriving."""
        new = {k: v for k, v in dict(
            state=state, x_mm=x_mm, y_mm=y_mm, point_index=point_index, path_len=path_len,
            calibration=calibration, cal_clicks=cal_clicks, shots_per_point=shots_per_point,
            pattern_edited=pattern_edited, camera_fps=camera_fps).items() if v is not _KEEP}
        if all(self._status.get(k) == v for k, v in new.items()):
            return                       # telemetry repeats the same values; skip repaint
        self._status.update(new)
        try:
            self._show()
        except Exception as e:          # display only: never take the GUI down
            self.setToolTip(f"Control bar could not show a value: {e}")

    def _fix_widths(self) -> None:
        """Fixed widths so live values never shift their neighbours."""
        for w in (self.state_block, self.x_value, self.y_value, self.point.value, self.camera.value,
                  self.calibration.value, self.shots.value):
            w.ensurePolished()            # role fonts come from the stylesheet
        fm = self.state_block.fontMetrics()
        words = [w for w, _, _ in STATE_WORDS.values()]
        self.state_block.setFixedWidth(max(fm.horizontalAdvance(w) for w in words) + 2 * 14 + 8)
        self.state_block.setFixedHeight(fm.height() + 12)
        pm = self.x_value.fontMetrics()
        for v in (self.x_value, self.y_value):
            v.setMinimumWidth(pm.horizontalAdvance("-00.000") + 2)
        vm = self.point.value.fontMetrics()
        self.point.value.setMinimumWidth(vm.horizontalAdvance("8888 of 8888"))
        self.calibration.value.setMinimumWidth(vm.horizontalAdvance(calibration_words("stale")[0]))
        self.shots.caption.ensurePolished()
        self.shots.value.setMinimumWidth(self.shots.caption.fontMetrics().horizontalAdvance("Shots per point"))
        self.camera.value.setMinimumWidth(vm.horizontalAdvance("No frames"))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.StyleChange, QtCore.QEvent.FontChange):
            self._fix_widths()

    def _show(self) -> None:
        s = self._status
        word, look, tip = state_words(s["state"])
        if self.state_block.text() != word:
            self.state_block.setText(word)
        set_prop(self.state_block, "look", look)
        self._state_field.setToolTip(tip)

        self.x_value.setText(mm_words(s["x_mm"]))
        self.y_value.setText(mm_words(s["y_mm"]))

        text, ptip = point_words(s["point_index"], s["path_len"])
        self.point.show_value(text, "" if s["path_len"] else "label", ptip)

        text, tone, ctip = calibration_words(s["calibration"], s["cal_clicks"])
        self.calibration.show_value(text, tone, ctip)

        n = s["shots_per_point"]
        self.shots.show_value("—" if n is None else str(int(n)), "" if n else "label",
                              "Shots per raster point (BLACS sets it from the sequence).")

        text, tone = fps_words(s["camera_fps"])
        self.camera.show_value(text, tone, "Frames per second arriving from the camera.")

        self.edited_notice.setVisible(bool(s["pattern_edited"]))
