"""Setup step: calibration (image px to stage mm), the stage's homes and backlash, the camera.

A plain widget: buttons and boxes are attributes the window connects; show_* methods take plain
values and never emit.
"""
from __future__ import annotations

import os
from typing import Optional, Sequence, Tuple

from PyQt5 import QtCore, QtWidgets

from .widgets import NumberBox, Section, label, set_prop


def _row(*buttons) -> QtWidgets.QHBoxLayout:
    row = QtWidgets.QHBoxLayout()
    row.setSpacing(8)
    for b in buttons:
        row.addWidget(b)
    row.addStretch(1)
    return row


def _mm(v: Optional[float], decimals: int = 3) -> str:
    return "—" if v is None else f"{v:.{decimals}f}"


class SetupStep(QtWidgets.QWidget):
    def __init__(self, camera_panel: QtWidgets.QWidget, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(18)

        # --- calibration ---
        cal = Section("Calibration")
        g = cal.body
        self.cal_state = label("No calibration.")
        self.cal_state.setWordWrap(True)
        self.cal_detail = label("", role="small")
        self.cal_detail.setWordWrap(True)
        g.addWidget(self.cal_state, 0, 0, 1, 2)
        g.addWidget(self.cal_detail, 1, 0, 1, 2)

        self.calibrate_btn = QtWidgets.QPushButton("Calibrate")
        self.calibrate_btn.setToolTip("Fit image px to stage mm from 3 spots: jog the beam to a spot, "
                                      "click the laser spot on the image, three times.")
        self.cancel_btn = QtWidgets.QPushButton("Cancel calibration")
        self.cancel_btn.setToolTip("Stop collecting spots; the calibration in use stays.")
        self.restore_camera_btn = QtWidgets.QPushButton("Use the calibration's camera settings")
        self.restore_camera_btn.setToolTip("Set the camera region, rotation and mirror back to the ones "
                                           "this calibration was made with.")
        g.addLayout(_row(self.calibrate_btn, self.cancel_btn), 2, 0, 1, 2)
        g.addLayout(_row(self.restore_camera_btn), 3, 0, 1, 2)

        self.load_btn = QtWidgets.QPushButton("Load…")
        self.load_btn.setToolTip("Use a calibration file (with its user home and backlash).")
        self.save_btn = QtWidgets.QPushButton("Save as…")
        self.save_btn.setToolTip("Save the calibration with the user home, backlash and camera settings.")
        self.use_last_btn = QtWidgets.QPushButton("Use last")
        self.use_last_btn.setToolTip("Load the calibration file used last.")
        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.clear_btn.setToolTip("Forget the calibration in use (files stay). Patterns cannot run without one.")
        g.addLayout(_row(self.load_btn, self.save_btn, self.use_last_btn, self.clear_btn), 4, 0, 1, 2)

        # The fit, read-only: stage [mm] = M [mm/px] · image [px] + b [mm].
        self.matrix = QtWidgets.QWidget()
        self.matrix.setToolTip("stage [mm] = M · image [px] + b")
        mg = QtWidgets.QGridLayout(self.matrix)
        mg.setContentsMargins(0, 2, 0, 0)
        mg.setHorizontalSpacing(14)
        mg.setVerticalSpacing(1)
        self._m = []
        for r, name in enumerate(("M (mm/px)", "", "b (mm)", "Scale")):
            mg.addWidget(label(name, role="small"), r, 0)
            cells = []
            for c in (1, 2):
                v = label("", role="small")
                v.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                v.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
                mg.addWidget(v, r, c)
                cells.append(v)
            self._m.append(cells)
        mg.setColumnStretch(3, 1)
        g.addWidget(self.matrix, 5, 0, 1, 2)
        lay.addWidget(cal)

        # --- stage ---
        st = Section("Stage")
        g = st.body
        g.setColumnStretch(0, 1)
        g.setColumnStretch(1, 1)
        self.home_x_btn = QtWidgets.QPushButton("X")
        self.home_y_btn = QtWidgets.QPushButton("Y")
        self.home_both_btn = QtWidgets.QPushButton("Both")
        for b in (self.home_x_btn, self.home_y_btn, self.home_both_btn):
            b.setToolTip("Device home: the motor runs to its mechanical zero (slow, full travel).")
        g.addWidget(label("Device home"), 0, 0)
        g.addLayout(_row(self.home_x_btn, self.home_y_btn, self.home_both_btn), 0, 1, 1, 2)

        self.user_home_reading = label("", tone="label")
        self.user_home_x = NumberBox(decimals=3, minimum=-100, maximum=100, unit="mm", step=0.1)
        self.user_home_y = NumberBox(decimals=3, minimum=-100, maximum=100, unit="mm", step=0.1)
        self.user_home_set_btn = QtWidgets.QPushButton("Set")
        self.user_home_set_btn.setToolTip("Store x, y as the user home (nothing moves). "
                                          "Kept in the calibration file.")
        self.user_home_here_btn = QtWidgets.QPushButton("Here")
        self.user_home_here_btn.setToolTip("Fill x, y with where the stage is now.")
        g.addWidget(label("User home"), 1, 0)
        g.addWidget(self.user_home_reading, 1, 1, 1, 2)
        g.addWidget(self.user_home_x, 2, 0)
        g.addWidget(self.user_home_y, 2, 1)
        g.addLayout(_row(self.user_home_set_btn, self.user_home_here_btn), 2, 2)

        self.backlash_reading = label("", tone="label")
        self.backlash_x = NumberBox(decimals=4, minimum=0, maximum=1, unit="mm", step=0.001)
        self.backlash_y = NumberBox(decimals=4, minimum=0, maximum=1, unit="mm", step=0.001)
        self.backlash_set_btn = QtWidgets.QPushButton("Set")
        self.backlash_set_btn.setToolTip("Send x, y to the motors (they report what they took). "
                                         "Kept in the calibration file.")
        g.addWidget(label("Backlash"), 3, 0)
        g.addWidget(self.backlash_reading, 3, 1, 1, 2)
        g.addWidget(self.backlash_x, 4, 0)
        g.addWidget(self.backlash_y, 4, 1)
        g.addLayout(_row(self.backlash_set_btn), 4, 2)
        for box, axis in ((self.user_home_x, "x"), (self.user_home_y, "y"),
                          (self.backlash_x, "x"), (self.backlash_y, "y")):
            box.setPrefix(f"{axis}  ")
        lay.addWidget(st)

        # --- camera ---
        lay.addWidget(camera_panel)
        self.show_user_home(None, None)
        self.show_backlash(None, None)

    # ---------------- program -> view ----------------
    def show_calibration(self, state: str, *, path: str = "", saved_at: str = "", M=None, b=None,
                         clicks: Tuple[int, int] = (0, 3)) -> None:
        """state: "none" | "collecting" | "loaded" | "stale" (as the control bar)."""
        calibrating = state == "collecting"
        name = os.path.basename(path) if path else "not saved to a file"
        saved = f"Saved {saved_at.replace('T', ' ')}." if saved_at else "Not saved to a file."
        if calibrating:
            done, needed = clicks
            text = f"Calibrating: spot {min(done + 1, needed)} of {needed}."
            detail = ("Jog the beam to a new spot (Move stage, below), then click the laser spot on "
                      "the image. The spots must not lie on one line.")
            tone = "path"
        elif state == "none":
            text, detail, tone = "No calibration.", "Calibrate, load a file, or use the last one.", "fault"
        elif state == "stale":
            text = f"Stale: {name}"
            detail = (f"{saved} The camera region, rotation or mirror is not the one this calibration "
                      "was made with, so image clicks land in the wrong place.")
            tone = "fault"
        else:
            text, detail, tone = f"Calibrated: {name}", saved, ""
        self.cal_state.setText(text)
        self.cal_state.setToolTip(path)
        set_prop(self.cal_state, "tone", tone)
        self.cal_detail.setText(detail)
        self.cal_detail.setVisible(bool(detail))
        self.cancel_btn.setVisible(calibrating)
        self.calibrate_btn.setVisible(not calibrating)
        self.restore_camera_btn.setVisible(state == "stale")
        show = M is not None and not calibrating
        self.matrix.setVisible(show)
        if show:
            scale = abs(float(M[0][0] * M[1][1] - M[0][1] * M[1][0])) ** 0.5
            rows = ((f"{M[0][0]:.6f}", f"{M[0][1]:.6f}"), (f"{M[1][0]:.6f}", f"{M[1][1]:.6f}"),
                    (f"{b[0]:.4f}", f"{b[1]:.4f}"), (f"{scale:.6f} mm/px", ""))
            for cells, texts in zip(self._m, rows):
                for cell, t in zip(cells, texts):
                    cell.setText(t.replace("-", "−"))

    def show_user_home(self, x: Optional[float], y: Optional[float]) -> None:
        """The user home [mm]; None: not set."""
        self.user_home_reading.setText("not set" if x is None and y is None else f"x {_mm(x)} mm,  y {_mm(y)} mm")
        if x is not None and y is not None:
            self.user_home_x.set_quietly(x)
            self.user_home_y.set_quietly(y)

    def show_backlash(self, x: Optional[float], y: Optional[float], *, fill: Sequence[str] = "XY") -> None:
        """The motors' backlash readings; the boxes of the axes in `fill` take them too."""
        self.backlash_reading.setText(f"x {_mm(x, 4)} mm,  y {_mm(y, 4)} mm")
        self.backlash_reading.setToolTip("What the motors report.")
        for axis, v, box in (("X", x, self.backlash_x), ("Y", y, self.backlash_y)):
            if v is not None and axis in fill:
                box.set_quietly(v)
