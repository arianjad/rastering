"""Pattern step: what pattern to raster, in image px. Every edit emits `changed` once; the window
rebuilds the points and shows them (no Preview button).

spec() builds a raster_paths.RasterSpec from the boxes; show_result() shows the point count or the
words of why there is no pattern.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PyQt5 import QtCore, QtWidgets

from raster_paths import RasterSpec
from .widgets import NumberBox, Section, label, sentence, set_prop

XY = Tuple[float, float]
KINDS = (("square_x", "Square, rows along x"), ("square_y", "Square, columns along y"),
         ("spiral", "Spiral, inward"), ("hull", "Hull of clicked corners"))
USES_BOUNDS = ("square_x", "square_y", "spiral")
USES_STEPS = ("square_x", "square_y", "hull")


def _box(decimals: int, unit: str, lo: float, hi: float, step: float, tip: str) -> NumberBox:
    b = NumberBox(decimals=decimals, minimum=lo, maximum=hi, unit=unit, step=step)
    b.setToolTip(tip)
    return b


class PatternStep(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._corners: List[XY] = []
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(18)

        # --- kind and steps ---
        pat = Section("Pattern")
        g = pat.body
        self.kind = QtWidgets.QComboBox()
        for key, text in KINDS:
            self.kind.addItem(text, key)
        self.kind.setToolTip("Square: a back-and-forth grid inside the scan bounds. Spiral: rings "
                             "inward from a centre, clipped to the scan bounds. Hull: a grid inside "
                             "the corners you click on the image.")
        g.addWidget(label("Kind"), 0, 0)
        g.addWidget(self.kind, 0, 1, 1, 3)
        self.xstep = _box(2, "px", 0.01, 10000, 1, "Grid step along image x [px].")
        self.ystep = _box(2, "px", 0.01, 10000, 1, "Grid step along image y [px].")
        self._step_row = (label("Step x"), self.xstep, label("y"), self.ystep)
        for c, w in enumerate(self._step_row):
            g.addWidget(w, 1, c)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        self.show_direction = QtWidgets.QCheckBox("Show the order (lines from point to point)")
        g.addWidget(self.show_direction, 2, 0, 1, 4)
        lay.addWidget(pat)

        # --- scan bounds (square, spiral) ---
        self.bounds_section = Section("Scan bounds")
        g = self.bounds_section.body
        tip = "Square: the box the grid fills. Spiral: points outside it are left out. [image px]"
        self.xlow = _box(1, "px", -10000, 100000, 10, tip)
        self.xhigh = _box(1, "px", -10000, 100000, 10, tip)
        self.ylow = _box(1, "px", -10000, 100000, 10, tip)
        self.yhigh = _box(1, "px", -10000, 100000, 10, tip)
        for r, (name, lo, hi) in enumerate((("x from", self.xlow, self.xhigh), ("y from", self.ylow, self.yhigh))):
            g.addWidget(label(name), r, 0)
            g.addWidget(lo, r, 1)
            g.addWidget(label("to"), r, 2)
            g.addWidget(hi, r, 3)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        self.whole_image_btn = QtWidgets.QPushButton("Whole image")
        self.whole_image_btn.setToolTip("Set the bounds to the whole camera image.")
        g.addWidget(self.whole_image_btn, 2, 1, 1, 3, QtCore.Qt.AlignLeft)
        lay.addWidget(self.bounds_section)

        # --- spiral ---
        self.spiral_section = Section("Spiral")
        g = self.spiral_section.body
        self.cx = _box(1, "px", -10000, 100000, 10, "Spiral centre, image x [px]. Click the image to set it.")
        self.cy = _box(1, "px", -10000, 100000, 10, "Spiral centre, image y [px]. Click the image to set it.")
        self.radius = _box(2, "px", 0, 100000, 10, "Radius of the first ring [px].")
        self.rstep = _box(2, "px", 0.01, 100000, 1, "The radius shrinks by this much each turn [px].")
        self.astep = _box(4, "rad", 0.0001, 6.2832, 0.01, "Angle between neighbouring points [rad].")
        self.achange = _box(4, "rad", -6.2832, 6.2832, 0.01, "The angle step grows by this much each turn [rad].")
        for r, (n1, b1, n2, b2) in enumerate((("Centre x", self.cx, "y", self.cy),
                                              ("Radius", self.radius, "step", self.rstep),
                                              ("Angle step", self.astep, "change", self.achange))):
            g.addWidget(label(n1), r, 0)
            g.addWidget(b1, r, 1)
            g.addWidget(label(n2), r, 2)
            g.addWidget(b2, r, 3)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        g.addWidget(label("Click the image to put the centre there.", role="small"), 3, 0, 1, 4)
        lay.addWidget(self.spiral_section)

        # --- hull corners ---
        self.hull_section = Section("Corners")
        g = self.hull_section.body
        self.corner_count = label("")
        self.remove_corner_btn = QtWidgets.QPushButton("Remove last")
        self.clear_corners_btn = QtWidgets.QPushButton("Clear")
        g.addWidget(self.corner_count, 0, 0, 1, 3)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.remove_corner_btn)
        row.addWidget(self.clear_corners_btn)
        row.addStretch(1)
        g.addLayout(row, 1, 0, 1, 3)
        lay.addWidget(self.hull_section)

        # --- result ---
        res = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(res)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(2)
        self.count = label("", role="heading", tone="path")
        self.count_detail = label("", role="small")
        self.count_detail.setWordWrap(True)
        rl.addWidget(self.count)
        rl.addWidget(self.count_detail)
        lay.addWidget(res)
        lay.addStretch(1)

        # defaults (labelled: the operator sets the real values and saves them)
        self.xstep.setValue(40.0)
        self.ystep.setValue(40.0)
        self.xhigh.setValue(1000.0)
        self.yhigh.setValue(1000.0)
        self.radius.setValue(200.0)
        self.rstep.setValue(20.0)
        self.astep.setValue(0.2)
        self.kind.setCurrentIndex(0)

        for b in (self.xstep, self.ystep, self.xlow, self.xhigh, self.ylow, self.yhigh, self.cx, self.cy,
                  self.radius, self.rstep, self.astep, self.achange):
            b.valueChanged.connect(self._emit)
        self.kind.currentIndexChanged.connect(self._on_kind)
        self.show_direction.toggled.connect(self._emit)
        self.remove_corner_btn.clicked.connect(self.remove_last_corner)
        self.clear_corners_btn.clicked.connect(self.clear_corners)
        self._on_kind(emit=False)

    # ---------------- reads ----------------
    def kind_key(self) -> str:
        return str(self.kind.currentData())

    def corners(self) -> List[XY]:
        return list(self._corners)

    def bounds(self) -> Tuple[float, float, float, float]:
        return (self.xlow.value(), self.xhigh.value(), self.ylow.value(), self.yhigh.value())

    def spec(self) -> RasterSpec:
        k = self.kind_key()
        return RasterSpec(kind=k, bounds=self.bounds() if k in USES_BOUNDS else None,
                          xstep=self.xstep.value(), ystep=self.ystep.value(),
                          origin=(self.cx.value(), self.cy.value()), radius=self.radius.value(),
                          step=self.rstep.value(), angle_step=self.astep.value(),
                          angle_step_change=self.achange.value(), hull_points=tuple(self._corners))

    # ---------------- edits from the image ----------------
    def add_corner(self, x: float, y: float) -> None:
        self._corners.append((float(x), float(y)))
        self._show_corners()
        self._emit()

    def remove_last_corner(self) -> None:
        if self._corners:
            self._corners.pop()
            self._show_corners()
            self._emit()

    def clear_corners(self) -> None:
        if self._corners:
            self._corners.clear()
            self._show_corners()
            self._emit()

    def set_centre(self, x: float, y: float) -> None:
        self.cx.set_quietly(x)
        self.cy.set_quietly(y)
        self._emit()

    def set_bounds(self, b, *, emit: bool = True) -> None:
        for box, v in zip((self.xlow, self.xhigh, self.ylow, self.yhigh), b):
            box.set_quietly(float(v))
        if emit:
            self._emit()

    # ---------------- program -> view ----------------
    def show_result(self, points: Optional[int], *, error: str = "", outside: int = 0,
                    pingpong: bool = False, drawn_every: int = 1) -> None:
        """points: the pattern's point count (None: no pattern, `error` says why). drawn_every:
        the image draws 1 point in this many."""
        if points is None:
            self.count.setText("No pattern")
            set_prop(self.count, "tone", "fault")
            self.count_detail.setText(sentence(error) + ("." if error and error[-1] != "." else ""))
            set_prop(self.count_detail, "tone", "fault")
            return
        self.count.setText(f"{points} point{'s' if points != 1 else ''}")
        set_prop(self.count, "tone", "path")
        notes = []
        if pingpong:
            notes.append(f"Ping-pong: {2 * points} steps per round trip.")
        if outside:
            notes.append(f"{outside} outside the stage travel: left out when armed.")
        if drawn_every > 1:
            notes.append(f"1 point in {drawn_every} drawn; all are armed.")
        self.count_detail.setText(" ".join(notes))
        set_prop(self.count_detail, "tone", "fault" if outside == points else "")

    # ---------------- saved settings ----------------
    def settings(self) -> Dict[str, Any]:
        return {"kind": self.kind_key(), "xstep": self.xstep.value(), "ystep": self.ystep.value(),
                "bounds": list(self.bounds()),
                "spiral": {"centre": [self.cx.value(), self.cy.value()], "radius": self.radius.value(),
                           "step": self.rstep.value(), "angle_step": self.astep.value(),
                           "angle_step_change": self.achange.value()},
                "hull_corners": [list(p) for p in self._corners],
                "show_direction": self.show_direction.isChecked()}

    def apply_settings(self, d: Dict[str, Any]) -> None:
        """Show saved settings (keys missing keep their value); emits changed once."""
        self.blockSignals(True)
        try:
            i = self.kind.findData(d.get("kind"))
            if i >= 0:
                self.kind.setCurrentIndex(i)
            for key, box in (("xstep", self.xstep), ("ystep", self.ystep)):
                if d.get(key) is not None:
                    box.set_quietly(float(d[key]))
            if isinstance(d.get("bounds"), list) and len(d["bounds"]) == 4:
                self.set_bounds(d["bounds"], emit=False)
            sp = d.get("spiral") or {}
            if isinstance(sp.get("centre"), list) and len(sp["centre"]) == 2:
                self.cx.set_quietly(float(sp["centre"][0]))
                self.cy.set_quietly(float(sp["centre"][1]))
            for key, box in (("radius", self.radius), ("step", self.rstep), ("angle_step", self.astep),
                             ("angle_step_change", self.achange)):
                if sp.get(key) is not None:
                    box.set_quietly(float(sp[key]))
            corners = d.get("hull_corners")
            if isinstance(corners, list):
                self._corners = [(float(p[0]), float(p[1])) for p in corners if len(p) == 2]
            if d.get("show_direction") is not None:
                self.show_direction.setChecked(bool(d["show_direction"]))
        finally:
            self.blockSignals(False)
        self._on_kind(emit=False)
        self._emit()

    # ---------------- inside ----------------
    def _on_kind(self, *_, emit: bool = True) -> None:
        k = self.kind_key()
        for w in self._step_row:
            w.setVisible(k in USES_STEPS)
        self.bounds_section.setVisible(k in USES_BOUNDS)
        self.spiral_section.setVisible(k == "spiral")
        self.hull_section.setVisible(k == "hull")
        self._show_corners()
        if emit:
            self._emit()

    def _show_corners(self) -> None:
        n = len(self._corners)
        need = "" if n >= 3 else f" At least 3 needed."
        self.corner_count.setText(f"{n} corner{'s' if n != 1 else ''}.{need} Click the image to add one.")
        self.remove_corner_btn.setEnabled(n > 0)
        self.clear_corners_btn.setEnabled(n > 0)

    def _emit(self, *_):
        self.changed.emit()
