"""Run step: run the pattern at the GUI or arm it for BLACS, step, stop, go to a point, and the
position history. A plain widget: the window connects the buttons and sets which are enabled.
"""
from __future__ import annotations

from typing import Any, Dict

from PyQt5 import QtCore, QtWidgets

from .widgets import IntBox, NumberBox, Section, label, set_prop

# What the raster does now, in one sentence per state (the menu's raster_state words).
STATE_SENTENCES = {
    "off": "Nothing moves the stage on its own.",
    "local": "Armed and held here: Step moves to the next point.",
    "armed": "BLACS steps the stage: one point per group of shots.",
    "running": "Running: the stage goes through the points by itself.",
}


def _button(text: str, tip: str) -> QtWidgets.QPushButton:
    b = QtWidgets.QPushButton(text)
    b.setToolTip(tip)
    b.setProperty("tip", tip)       # the tooltip to restore when the button is enabled again
    return b


class RunStep(QtWidgets.QWidget):
    options_changed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(18)

        # --- the raster ---
        r = Section("Raster")
        g = r.body
        self.sentence = label(STATE_SENTENCES["off"])
        self.sentence.setWordWrap(True)
        g.addWidget(self.sentence, 0, 0, 1, 2)
        self.start_btn = _button("Start", "")
        self.arm_blacs_btn = _button("Arm for BLACS", "Arm the pattern; BLACS steps it, one point per "
                                                      "group of shots. Nothing moves until the first shot.")
        self.step_btn = _button("Step", "Move to the next point of the armed path.")
        self.stop_btn = _button("Stop (Esc)", "End the raster; queued points are dropped. "
                                               "A move already under way finishes.")
        self.hand_btn = _button("Hand to BLACS", "BLACS steps the armed path from the point it is at.")
        self.take_back_btn = _button("Take back", "Step the armed path here, from the point it is at.")
        self.rearm_btn = _button("Re-arm with edited pattern",
                                 "Replace the armed path with the pattern on the image, from its first "
                                 "point. BLACS keeps the raster if it has it.")
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for i, b in enumerate((self.start_btn, self.arm_blacs_btn, self.step_btn, self.stop_btn,
                               self.hand_btn, self.take_back_btn)):
            grid.addWidget(b, i // 2, i % 2)
        grid.addWidget(self.rearm_btn, 3, 0, 1, 2)
        g.addLayout(grid, 1, 0, 1, 2)
        lay.addWidget(r)

        # --- how to run ---
        h = Section("How to run")
        g = h.body
        self.continuous = QtWidgets.QCheckBox("Continuous")
        self.continuous.setToolTip("On: Start runs through the points by itself. Off: Arm, then Step "
                                   "at the GUI. BLACS always steps one point per shot.")
        self.delay = NumberBox(decimals=3, minimum=0.0, maximum=60.0, unit="s", step=0.05)
        self.delay.setToolTip("Continuous run: wait this long at each point [s].")
        self.delay_label = label("Delay per point")
        self.pingpong = QtWidgets.QCheckBox("Ping-pong: forwards, then back")
        self.pingpong.setToolTip("Run the pattern forwards, then backwards, so the stage retraces its "
                                 "path instead of jumping back to the start (the stages have backlash). "
                                 "An N-point pattern becomes a 2N-step path.")
        g.addWidget(self.continuous, 0, 0)
        g.addWidget(self.delay_label, 0, 1, QtCore.Qt.AlignRight)
        g.addWidget(self.delay, 0, 2)
        g.addWidget(self.pingpong, 1, 0, 1, 3)
        self.shots = label("—")
        g.addWidget(label("Shots per point"), 2, 0)
        g.addWidget(self.shots, 2, 1, 1, 2)
        g.setColumnStretch(2, 1)
        lay.addWidget(h)

        # --- go to a point ---
        p = Section("Go to a point")
        g = p.body
        self.point_box = IntBox(0, 0)
        self.point_box.setSpecialValueText("—")
        self.point_box.setToolTip("Pick a point of the armed path by number (nothing moves). "
                                  "point_index in the shot file is this number minus 1.")
        self.point_of = label("of —", tone="label")
        self.goto_btn = _button("Go to selected point", "Move to the picked point; the next step "
                                "continues after it. Takes the raster back from BLACS.")
        g.addWidget(label("Point"), 0, 0)
        g.addWidget(self.point_box, 0, 1)
        g.addWidget(self.point_of, 0, 2)
        g.addWidget(self.goto_btn, 0, 3)
        g.setColumnStretch(1, 1)
        g.addWidget(label("Click the image to pick the nearest point.", role="small"), 1, 0, 1, 4)
        lay.addWidget(p)

        # --- position history ---
        ph = Section("Position history")
        g = ph.body
        self.record = QtWidgets.QCheckBox("Record to a file")
        self.record.setToolTip("Write each new stage position (mm and px) to a CSV file while on.")
        self.record_file = label("", role="small")
        self.trail = IntBox(0, 10000, "positions", 10)
        self.trail.setToolTip("Show where the spot has been: the last N positions as dots (0: none).")
        g.addWidget(self.record, 0, 0, 1, 2)
        g.addWidget(self.record_file, 1, 0, 1, 2)
        g.addWidget(label("Trail on image"), 2, 0)
        g.addWidget(self.trail, 2, 1)
        g.setColumnStretch(2, 1)
        lay.addWidget(ph)
        self.show_record_file("")
        lay.addStretch(1)

        self.pingpong.setChecked(True)
        self.trail.setValue(20)
        self.continuous.toggled.connect(self._show_mode)
        for sig in (self.continuous.toggled, self.pingpong.toggled, self.delay.valueChanged):
            sig.connect(lambda *_: self.options_changed.emit())
        self._show_mode()

    def _show_mode(self, *_):
        on = self.continuous.isChecked()
        self.delay.setEnabled(on)
        self.delay_label.setEnabled(on)
        tip = ("Start a continuous run: the stage goes through the points by itself." if on else
               "Arm the pattern to step it here with Step. Nothing moves until you step.")
        self.start_btn.setText("Start" if on else "Arm here")
        self.start_btn.setProperty("tip", tip)
        self.start_btn.setToolTip(tip)

    # ---------------- program -> view ----------------
    def show_state(self, state: str, *, path_len: int, selected: int, shots: int, dropped: int) -> None:
        sentence = STATE_SENTENCES.get(state, state)
        if dropped:
            sentence += f" {dropped} pattern points were outside the stage travel and left out."
        self.sentence.setText(sentence)
        self.point_box.blockSignals(True)          # a shorter path must not pick a point by clamping
        self.point_box.setMaximum(max(0, path_len))
        self.point_box.setValue(selected + 1 if selected >= 0 else 0)
        self.point_box.blockSignals(False)
        self.point_of.setText(f"of {path_len}" if path_len else "of —")
        self.shots.setText(f"{shots}  (set by BLACS)")

    def show_record_file(self, path: str) -> None:
        self.record_file.setText(path)
        self.record_file.setVisible(bool(path))

    def set_primary(self, button: QtWidgets.QPushButton) -> None:
        """Style one button as the main action now."""
        for b in (self.start_btn, self.arm_blacs_btn, self.step_btn, self.stop_btn, self.hand_btn,
                  self.take_back_btn, self.rearm_btn):
            set_prop(b, "kind", "primary" if b is button else "edited" if b is self.rearm_btn else "")

    # ---------------- saved settings ----------------
    def settings(self) -> Dict[str, Any]:
        return {"continuous": self.continuous.isChecked(), "pingpong": self.pingpong.isChecked(),
                "delay_s": self.delay.value(), "trail": self.trail.value()}

    def apply_settings(self, d: Dict[str, Any]) -> None:
        for key, box in (("continuous", self.continuous), ("pingpong", self.pingpong)):
            if d.get(key) is not None:
                box.blockSignals(True)
                box.setChecked(bool(d[key]))
                box.blockSignals(False)
        if d.get("delay_s") is not None:
            self.delay.set_quietly(float(d["delay_s"]))
        if d.get("trail") is not None:
            self.trail.setValue(int(d["trail"]))
        self._show_mode()
