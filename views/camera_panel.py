"""Camera panel: the Zelux controls that matter (exposure, gain, gamma, region,
display rotation and mirror) and the camera settings file. A plain widget for
the Setup step.

Values the program shows (show_info, show_applied, set_geometry) never emit
signals; each operator gesture emits its signal once.
"""
from __future__ import annotations

import math
import os
from dataclasses import replace
from typing import Optional

from PyQt5 import QtCore, QtWidgets

from camera_thorlabs import CameraInfo, DisplayGeometry, ThorCamConfig
from .widgets import IntBox, NumberBox, Section, label, quiet_slider, set_prop

ROTATIONS = (("No rotation", 0), ("90° clockwise", -1), ("180°", 2), ("90° anticlockwise", 1))
GAMMA_RANGE = (0.1, 4.0)       # software gamma offered here (display only)
_SLIDER_STEPS = 1000


class _LinkedValue:
    """A slider and a number box showing one value. Either one moved by the
    operator updates the other silently and calls `commit` once."""

    def __init__(self, slider, box, to_slider, from_slider, commit):
        self.slider, self.box = slider, box
        self._to, self._from, self._commit = to_slider, from_slider, commit
        slider.valueChanged.connect(self._on_slider)
        box.valueChanged.connect(self._on_box)

    def _on_slider(self, s):
        v = self._from(s)
        self.box.blockSignals(True)
        self.box.setValue(v)
        self.box.blockSignals(False)
        self._commit(self.box.value())

    def _on_box(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(self._to(v))
        self.slider.blockSignals(False)
        self._commit(v)

    def show(self, v):
        """Show a value without committing it."""
        for w in (self.slider, self.box):
            w.blockSignals(True)
        self.box.setValue(v)
        self.slider.setValue(self._to(self.box.value()))
        for w in (self.slider, self.box):
            w.blockSignals(False)


class CameraPanel(QtWidgets.QWidget):
    exposure_changed = QtCore.pyqtSignal(float)            # ms
    gain_changed = QtCore.pyqtSignal(int)                  # camera gain index
    gamma_changed = QtCore.pyqtSignal(float)
    roi_requested = QtCore.pyqtSignal(int, int, int, int)  # width, height, start x, start y [px]
    geometry_changed = QtCore.pyqtSignal(object)           # DisplayGeometry
    save_requested = QtCore.pyqtSignal()
    load_requested = QtCore.pyqtSignal()
    revert_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._exp_min, self._exp_max = 0.04, 10_000.0
        self._sensor = (1440, 1080)
        self._camera_roi: Optional[tuple] = None
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(18)

        # --- image ---
        img = Section("Image")
        g = img.body
        g.setColumnStretch(1, 1)
        self.exposure_box = NumberBox(decimals=3, minimum=self._exp_min, maximum=self._exp_max,
                                      unit="ms", step=1.0)
        self.gain_box = IntBox(0, 48)
        self.gamma_box = NumberBox(decimals=2, minimum=GAMMA_RANGE[0], maximum=GAMMA_RANGE[1], step=0.05)
        self.exposure_slider = quiet_slider()
        self.gain_slider = quiet_slider()
        self.gamma_slider = quiet_slider()
        self.exposure_slider.setRange(0, _SLIDER_STEPS)
        self.gamma_slider.setRange(0, _SLIDER_STEPS)
        self.gain_slider.setRange(0, 48)
        rows = (("Exposure", self.exposure_slider, self.exposure_box,
                 "Exposure time [ms]. The slider is logarithmic."),
                ("Gain", self.gain_slider, self.gain_box, "Camera gain, in the camera's own steps."),
                ("Gamma", self.gamma_slider, self.gamma_box,
                 "Display gamma (applied to the shown image only; 1 = unchanged)."))
        for r, (name, slider, box, tip) in enumerate(rows):
            lab = label(name)
            for w in (lab, slider, box):
                w.setToolTip(tip)
            box.setMinimumWidth(self._box_width())
            g.addWidget(lab, r, 0)
            g.addWidget(slider, r, 1)
            g.addWidget(box, r, 2)
        self._exposure = _LinkedValue(
            self.exposure_slider, self.exposure_box, self._exp_to_slider, self._slider_to_exp,
            lambda v: self.exposure_changed.emit(float(v)))
        self._gain = _LinkedValue(self.gain_slider, self.gain_box, int, int,
                                  lambda v: self.gain_changed.emit(int(v)))
        g0, g1 = GAMMA_RANGE
        self._gamma = _LinkedValue(
            self.gamma_slider, self.gamma_box,
            lambda v: int(round((v - g0) / (g1 - g0) * _SLIDER_STEPS)),
            lambda s: g0 + (g1 - g0) * s / _SLIDER_STEPS,
            lambda v: self.gamma_changed.emit(float(v)))
        lay.addWidget(img)

        # --- region of interest ---
        roi = Section("Region of interest")
        self._roi_group = roi
        g = roi.body
        self.roi_w = IntBox(1, 4096, "px", 4)
        self.roi_h = IntBox(1, 4096, "px", 4)
        self.roi_x = IntBox(0, 4096, "px", 4)
        self.roi_y = IntBox(0, 4096, "px", 4)
        self.roi_x.setToolTip("Upper-left corner on the sensor [px], counted from the sensor corner.")
        self.roi_y.setToolTip(self.roi_x.toolTip())
        for r, c, name, box in ((0, 0, "Width", self.roi_w), (0, 2, "Height", self.roi_h),
                                (1, 0, "Start x", self.roi_x), (1, 2, "Start y", self.roi_y)):
            g.addWidget(label(name), r, c)
            g.addWidget(box, r, c + 1)
            box.valueChanged.connect(self._show_roi_hint)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(8)
        self.apply_roi_btn = QtWidgets.QPushButton("Apply region")
        self.apply_roi_btn.setToolTip("Send this region to the camera. A calibration made with "
                                      "another region no longer fits.")
        self.centre_btn = QtWidgets.QPushButton("Centre on sensor")
        self.centre_btn.setToolTip("Fill Start x and Start y so the region sits in the sensor's middle.")
        btns.addWidget(self.apply_roi_btn)
        btns.addWidget(self.centre_btn)
        btns.addStretch(1)
        g.addLayout(btns, 2, 0, 1, 4)
        self.roi_hint = label("", role="small")
        g.addWidget(self.roi_hint, 3, 0, 1, 4)
        self.apply_roi_btn.clicked.connect(self._request_roi)
        self.centre_btn.clicked.connect(self._centre)
        lay.addWidget(roi)

        # --- display ---
        disp = Section("Display")
        self._disp_group = disp
        g = disp.body
        self.rotation = QtWidgets.QComboBox()
        for text, k in ROTATIONS:
            self.rotation.addItem(text, k)
        self.mirror_x = QtWidgets.QCheckBox("Mirror left–right")
        self.mirror_y = QtWidgets.QCheckBox("Mirror up–down")
        self.mirror_x.setToolTip("flip_x in the settings file")
        self.mirror_y.setToolTip("flip_y in the settings file")
        g.addWidget(label("Rotation"), 0, 0)
        g.addWidget(self.rotation, 0, 1)
        g.addWidget(self.mirror_x, 1, 1)
        g.addWidget(self.mirror_y, 2, 1)
        g.setColumnStretch(1, 1)
        self.rotation.currentIndexChanged.connect(self._emit_geometry)
        self.mirror_x.toggled.connect(self._emit_geometry)
        self.mirror_y.toggled.connect(self._emit_geometry)
        lay.addWidget(disp)

        # --- settings file ---
        f = Section("Camera settings file")
        self.file_label = label("No file loaded", tone="label")
        self.file_label.setMinimumWidth(10)
        f.body.addWidget(self.file_label, 0, 0)
        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(8)
        self.save_btn = QtWidgets.QPushButton("Save as…")
        self.load_btn = QtWidgets.QPushButton("Load…")
        self.revert_btn = QtWidgets.QPushButton("Revert")
        self.save_btn.setToolTip("Save these camera and display settings to a file.")
        self.load_btn.setToolTip("Load camera and display settings from a file.")
        self.revert_btn.setToolTip("Go back to the settings in the file named above.")
        for b, sig in ((self.save_btn, self.save_requested), (self.load_btn, self.load_requested),
                       (self.revert_btn, self.revert_requested)):
            btns.addWidget(b)
            b.clicked.connect(sig.emit)
        btns.addStretch(1)
        f.body.addLayout(btns, 1, 0)
        lay.addWidget(f)
        lay.addStretch(1)
        self._file_path = ""
        self._show_roi_hint()

    # ---------------- mapping ----------------
    def _box_width(self) -> int:
        self.ensurePolished()
        return self.fontMetrics().horizontalAdvance("10000.000 ms") + 18

    def _exp_to_slider(self, ms: float) -> int:
        lo, hi = math.log(self._exp_min), math.log(self._exp_max)
        return int(round((math.log(max(ms, self._exp_min)) - lo) / (hi - lo) * _SLIDER_STEPS))

    def _slider_to_exp(self, s: int) -> float:
        lo, hi = math.log(self._exp_min), math.log(self._exp_max)
        return math.exp(lo + (hi - lo) * s / _SLIDER_STEPS)

    # ---------------- program -> panel (no signals) ----------------
    def show_info(self, info: CameraInfo) -> None:
        """Ranges and current values the camera reports (after open or a ROI change)."""
        self._sensor = (int(info.sensor_width), int(info.sensor_height))
        self._exp_min = max(1e-3, float(info.exposure_min_ms))
        self._exp_max = max(self._exp_min * 1.001, float(info.exposure_max_ms))
        self.exposure_box.setRange(self._exp_min, self._exp_max)
        self.gain_box.setRange(int(info.gain_min), int(info.gain_max))
        self.gain_slider.blockSignals(True)
        self.gain_slider.setRange(int(info.gain_min), int(info.gain_max))
        self.gain_slider.blockSignals(False)
        self._exposure.show(float(info.exposure_ms))
        self._gain.show(int(info.gain))
        self._gamma.show(float(info.gamma))
        sw, sh = self._sensor
        for box, mx in ((self.roi_w, sw), (self.roi_h, sh), (self.roi_x, sw - 1), (self.roi_y, sh - 1)):
            box.setMaximum(max(1, mx))
        self._camera_roi = (int(info.roi_width), int(info.roi_height), int(info.roi_x), int(info.roi_y))
        for box, val in zip((self.roi_w, self.roi_h, self.roi_x, self.roi_y), self._camera_roi):
            box.set_quietly(val)
        self._show_roi_hint()

    def show_applied(self, name: str, value: float) -> None:
        """The value the camera took after a change: "exposure_ms" | "gain" | "gamma"."""
        link = {"exposure_ms": self._exposure, "gain": self._gain, "gamma": self._gamma}.get(name)
        if link is not None:
            link.show(int(value) if name == "gain" else float(value))

    def set_geometry(self, geometry: DisplayGeometry) -> None:
        """Show a display geometry (e.g. from a loaded file) without emitting."""
        widgets = (self.rotation, self.mirror_x, self.mirror_y)
        for w in widgets:
            w.blockSignals(True)
        idx = self.rotation.findData(int(geometry.rotation_k))
        self.rotation.setCurrentIndex(max(0, idx))
        self.mirror_x.setChecked(bool(geometry.flip_x))
        self.mirror_y.setChecked(bool(geometry.flip_y))
        for w in widgets:
            w.blockSignals(False)

    def set_settings_file(self, path: str) -> None:
        """Name the camera settings file in use ("" = none)."""
        self._file_path = path or ""
        self.revert_btn.setEnabled(bool(path))
        self.file_label.setToolTip(path or "")
        self._elide_file()

    def set_geometry_enabled(self, on: bool, reason: str = "") -> None:
        """Lock the region, display and Load/Revert (a calibration depends on them),
        e.g. while a raster is active; reason shows as the tooltip."""
        for w in (self._roi_group, self._disp_group, self.load_btn, self.revert_btn):
            if w.property("tip") is None:          # the tooltip to give back when unlocked
                w.setProperty("tip", w.toolTip())
            w.setEnabled(bool(on) and (w is not self.revert_btn or bool(self._file_path)))
            w.setToolTip(w.property("tip") if on else reason)

    # ---------------- panel -> program ----------------
    def display_geometry(self) -> DisplayGeometry:
        return DisplayGeometry(rotation_k=int(self.rotation.currentData()),
                               flip_x=self.mirror_x.isChecked(), flip_y=self.mirror_y.isChecked())

    def camera_config(self, base: ThorCamConfig = ThorCamConfig()) -> ThorCamConfig:
        """The panel's values on top of `base` (serial number and fps limit come from base)."""
        return replace(base, exposure_ms=float(self.exposure_box.value()), gain=int(self.gain_box.value()),
                       gamma=float(self.gamma_box.value()), width=self.roi_w.value(),
                       height=self.roi_h.value(), start_x=self.roi_x.value(), start_y=self.roi_y.value())

    def _emit_geometry(self, *_):
        self.geometry_changed.emit(self.display_geometry())

    def _request_roi(self):
        self.roi_requested.emit(self.roi_w.value(), self.roi_h.value(), self.roi_x.value(), self.roi_y.value())

    def _centre(self):
        sw, sh = self._sensor
        self.roi_x.setValue(max(0, (sw - self.roi_w.value()) // 2))
        self.roi_y.setValue(max(0, (sh - self.roi_h.value()) // 2))

    def _show_roi_hint(self, *_):
        boxes = (self.roi_w.value(), self.roi_h.value(), self.roi_x.value(), self.roi_y.value())
        edited = self._camera_roi is not None and boxes != self._camera_roi
        sw, sh = self._sensor
        if edited:
            self.roi_hint.setText("Edited: press Apply region to send it to the camera.")
        else:
            self.roi_hint.setText(f"Sensor {sw} × {sh} px")
        set_prop(self.roi_hint, "tone", "path" if edited else "")
        self.apply_roi_btn.setEnabled(edited or self._camera_roi is None)

    def _elide_file(self):
        name = os.path.basename(self._file_path) if self._file_path else "No file loaded"
        fm = self.file_label.fontMetrics()
        width = max(40, self.file_label.width())
        self.file_label.setText(fm.elidedText(name, QtCore.Qt.ElideMiddle, width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide_file()
