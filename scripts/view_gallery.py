"""Render every view part with fake data, in every state, to PNG files.

    py -3.13 scripts/view_gallery.py OUT_DIR [--scale 1.5] [--light-only | --dark-only]

Offscreen (no window opens, no camera, no motors, no network). Writes, per theme:
    window-<theme>-<state>-<width>.png   the parts assembled as in the design plan
    control-bar-<theme>-<width>.png      the control bar in each state
    camera-view-<theme>.png              overlays in several states
    parts-<theme>.png                    camera panel, jog pad, step rail, log line
    focus-<theme>.png                    keyboard focus on each kind of control
Widths are logical px (1280 = half the 4K screen at 150 %, 2560 = all of it).
--scale sets the device pixel ratio (1.5 = the lab monitor).
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out_dir")
    p.add_argument("--scale", type=float, default=1.0)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--light-only", action="store_true")
    g.add_argument("--dark-only", action="store_true")
    return p.parse_args(argv)


ARGS = parse_args()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
# offscreen reports 100 dpi, which high-DPI scaling turns into a 1.0417 ratio; undo that
os.environ["QT_SCALE_FACTOR"] = str(ARGS.scale * 96.0 / 100.0)

import numpy as np  # noqa: E402
from PyQt5 import QtCore, QtWidgets  # noqa: E402

import theme  # noqa: E402

theme.setup_high_dpi()
APP = QtWidgets.QApplication(["view_gallery"])

from camera_sim import SimCamera  # noqa: E402
from camera_thorlabs import DisplayGeometry, ThorCamConfig  # noqa: E402
from views import (CameraPanel, CameraView, ControlBar, JogPad, LogLine, Section, StepRail,  # noqa: E402
                   label, unreachable_mask)

# ---------------------------------------------------------------- fake data
GEOM = DisplayGeometry(rotation_k=-1, flip_x=False, flip_y=True)     # as camera_params.ini
_cam = SimCamera(ThorCamConfig(exposure_ms=56.3, gain=24))
_cam.open()
FRAME = _cam.grab()                                                   # 1080 x 1440 sensor
INFO = _cam.info()
SHOWN_W, SHOWN_H = FRAME.shape[0], FRAME.shape[1]                     # after a 90 deg rotation
CX, CY = 1079 - 0.48 * 1080, 0.52 * 1440                              # target centre, shown px

ang = np.deg2rad(np.arange(0, 360, 60) + 15)
CORNERS = [(CX + 300 * np.cos(a) * 1.05, CY + 300 * np.sin(a)) for a in ang]


def _inside(poly, x, y):
    n, ok = len(poly), np.ones_like(x, dtype=bool)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        ok &= (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1) >= 0
    return ok


gx, gy = np.meshgrid(np.arange(CX - 320, CX + 321, 40), np.arange(CY - 320, CY + 321, 40))
gx, gy = gx.ravel(), gy.ravel()
keep = _inside(CORNERS, gx, gy)
PATTERN = [(float(x), float(y)) for x, y in zip(gx[keep], gy[keep])]
EDITED = [(x + 20, y + 20) for x, y in PATTERN]
ARMED = PATTERN + PATTERN[::-1]                       # ping-pong path
M = np.array([[0.011, 0.0], [0.0, 0.011]])
B = np.array([-1.0, -2.0])
MASK = unreachable_mask(SHOWN_W, SHOWN_H, M, B, (0.0, 12.0, 0.0, 12.0))
SPOT = PATTERN[11]
TRAIL = PATTERN[:11]
BOUNDS = (CX - 340, CX + 340, CY - 340, CY + 340)
CAL_CLICKS = [(CX - 250, CY - 200), (CX + 230, CY - 150), (CX - 20, CY + 260)]

BAR_STATES = [
    ("first start: nothing known", dict(state="off", calibration="none", camera_fps=None)),
    ("off, calibrated", dict(state="off", x_mm=6.234, y_mm=3.118, calibration="loaded", camera_fps=16.9,
                             shots_per_point=3)),
    ("calibrating", dict(state="off", x_mm=6.234, y_mm=3.118, calibration="collecting", cal_clicks=(2, 3),
                         camera_fps=16.9)),
    ("running", dict(state="running", x_mm=7.020, y_mm=4.981, point_index=11, path_len=48,
                     calibration="loaded", shots_per_point=3, camera_fps=16.9)),
    ("armed for BLACS, edited", dict(state="armed", x_mm=10.475, y_mm=11.902, point_index=30, path_len=96,
                                     calibration="loaded", shots_per_point=3, pattern_edited=True,
                                     camera_fps=16.9)),
    ("held at GUI, stale", dict(state="local", x_mm=-0.012, y_mm=0.000, point_index=4, path_len=96,
                                calibration="stale", shots_per_point=1000, camera_fps=None)),
]


# ---------------------------------------------------------------- helpers
def pump(n=4):
    for _ in range(n):
        APP.processEvents()


def save(widget, path, size=None):
    if size is not None:
        widget.resize(*size)
    widget.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    widget.show()
    pump()
    for v in widget.findChildren(CameraView):
        v.paint_now()
        v.fit()
    pump()
    widget.grab().save(path)
    print("wrote", path)


def sheet(rows, width):
    """Captioned rows stacked on the ground colour."""
    w = QtWidgets.QWidget()
    w.setProperty("surface", "ground")
    w.setAttribute(QtCore.Qt.WA_StyledBackground, True)
    lay = QtWidgets.QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 16)
    lay.setSpacing(6)
    for caption, part in rows:
        cap = label(caption, role="caption")
        cap.setContentsMargins(14, 14, 0, 0)
        lay.addWidget(cap)
        lay.addWidget(part)
    w.setFixedWidth(width)
    return w


def camera_view(state: str) -> CameraView:
    v = CameraView()
    v.set_geometry(GEOM)
    v.show_frame(FRAME)
    v.paint_now()
    if state in ("pattern", "armed", "all"):
        v.set_pattern_corners(CORNERS)
        v.set_scan_bounds(BOUNDS)
    if state == "pattern":
        v.set_pending_pattern(PATTERN)
        v.set_direction_lines(True)
        v.set_manual_markers([(CX + 120, CY - 360)])
    if state in ("armed", "all"):
        v.set_armed_path(ARMED, pattern_len=len(PATTERN))
        v.set_pending_pattern(EDITED if state == "all" else [])
        v.set_selected_point(PATTERN[20])
        v.set_spot(SPOT)
        v.set_trail(TRAIL)
        v.set_unreachable(MASK)
    if state == "calibrate":
        v.set_calibration_clicks(CAL_CLICKS[:2])
        v.set_spot((CX + 230, CY - 150))
    return v


def camera_panel(locked=False) -> CameraPanel:
    p = CameraPanel()
    p.show_info(INFO)
    p.set_geometry(GEOM)
    p.set_settings_file(os.path.join(REPO, "camera_params.ini"))
    if locked:
        p.set_geometry_enabled(False, "A raster is active: stop it to change the region or display.")
    return p


def log_line(expanded=False) -> LogLine:
    log = LogLine(history_height=150)
    for msg, err in [("Camera open (SimCamera).", False),
                     ("Camera settings loaded from camera_params.ini", False),
                     ("Calibration loaded: 0.011 mm/px, from calibration_2026-11-20.json", False),
                     ("Pattern: 48 points (outline, 40 px steps)", False),
                     ("Move refused: x 12.40 mm is outside the stage travel 0 to 12 mm", True)]:
        log.add(msg, error=err)
    log.set_expanded(expanded)
    return log


def window(state: str, width: int, height: int) -> QtWidgets.QWidget:
    """The parts in the plan's layout (a mock; the window worker builds the real one)."""
    w = QtWidgets.QWidget()
    w.setProperty("surface", "ground")
    w.setAttribute(QtCore.Qt.WA_StyledBackground, True)
    v = QtWidgets.QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(0)
    bar = ControlBar()
    v.addWidget(bar)
    mid = QtWidgets.QHBoxLayout()
    mid.setContentsMargins(0, 0, 0, 0)
    mid.setSpacing(0)
    left = QtWidgets.QWidget()
    left.setFixedWidth(420)
    lv = QtWidgets.QVBoxLayout(left)
    lv.setContentsMargins(14, 10, 14, 12)
    lv.setSpacing(8)
    rail = StepRail()
    lv.addWidget(rail)
    content = QtWidgets.QScrollArea()
    content.setWidgetResizable(True)
    if state == "setup":
        rail.set_current(0)
        content.setWidget(camera_panel())
        bar.set_status(state="off", x_mm=6.234, y_mm=3.118, calibration="none", camera_fps=16.9)
        rail.show_progress(True, "none", 0, "off")
        view = camera_view("calibrate")
    else:
        rail.set_current(2)
        ph = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(ph)
        pl.setContentsMargins(0, 8, 0, 0)
        pl.addWidget(label("Run controls go here (built with the window).", role="small"))
        pl.addStretch(1)
        content.setWidget(ph)
        bar.set_status(state="armed", x_mm=SPOT[0] * 0.011 - 1, y_mm=SPOT[1] * 0.011 - 2,
                       point_index=11, path_len=len(ARMED), calibration="loaded", shots_per_point=3,
                       pattern_edited=True, camera_fps=16.9)
        rail.show_progress(True, "loaded", len(PATTERN), "armed")
        view = camera_view("all")
    lv.addWidget(content, 1)
    move = Section("Move stage")
    jog = JogPad()
    if state != "setup":
        jog.set_moves_enabled(False, "A raster is active: stop it before moving the stage by hand.")
    move.body.addWidget(jog, 0, 0)
    lv.addSpacing(6)
    lv.addWidget(move)
    mid.addWidget(left)
    rule = QtWidgets.QFrame()
    rule.setFixedWidth(1)
    rule.setProperty("surface", "panel")
    rule.setStyleSheet(f"background: {theme.tokens()['rule']};")
    mid.addWidget(rule)
    mid.addWidget(view, 1)
    v.addLayout(mid, 1)
    v.addWidget(log_line())
    w.resize(width, height)
    return w


# ---------------------------------------------------------------- render
def render(out: str, light: bool) -> None:
    theme.apply_theme(APP, light=light)       # applies only; never saves the machine-wide preference
    name = "light" if light else "dark"
    for width, height in ((1280, 900), (2560, 1400)):
        for state in ("setup", "armed"):
            save(window(state, width, height), os.path.join(out, f"window-{name}-{state}-{width}.png"))
        bars = []
        for caption, status in BAR_STATES:
            b = ControlBar()
            b.set_status(**status)
            bars.append((caption, b))
        save(sheet(bars, width), os.path.join(out, f"control-bar-{name}-{width}.png"))

    grid = QtWidgets.QWidget()
    grid.setProperty("surface", "ground")
    grid.setAttribute(QtCore.Qt.WA_StyledBackground, True)
    gl = QtWidgets.QGridLayout(grid)
    gl.setContentsMargins(12, 12, 12, 12)
    gl.setSpacing(12)
    for i, state in enumerate(("calibrate", "pattern", "armed", "all")):
        gl.addWidget(label({"calibrate": "Calibrating: 2 clicks, spot shown",
                            "pattern": "Pattern drawn (rings), direction lines, a manual mark",
                            "armed": "Armed (filled), spot, trail, selected point, unreachable shading",
                            "all": "Armed plus an edited pattern on top"}[state], role="caption"),
                     0 if i < 2 else 2, i % 2)
        gl.addWidget(camera_view(state), 1 if i < 2 else 3, i % 2)
    save(grid, os.path.join(out, f"camera-view-{name}.png"), (1400, 1500))

    parts = QtWidgets.QWidget()
    parts.setProperty("surface", "ground")
    parts.setAttribute(QtCore.Qt.WA_StyledBackground, True)
    pg_ = QtWidgets.QGridLayout(parts)
    pg_.setContentsMargins(16, 12, 16, 16)
    pg_.setHorizontalSpacing(32)
    pg_.setVerticalSpacing(8)
    col = QtWidgets.QVBoxLayout()
    col.addWidget(label("Camera panel", role="caption"))
    cp = camera_panel()
    cp.roi_w.setValue(640)                # an edited, not yet applied region
    col.addWidget(cp)
    col.addWidget(label("Camera panel, locked while a raster is active", role="caption"))
    col.addWidget(camera_panel(locked=True))
    wrap = QtWidgets.QWidget()
    wrap.setLayout(col)
    wrap.setFixedWidth(392)
    pg_.addWidget(wrap, 0, 0, 3, 1)

    col2 = QtWidgets.QVBoxLayout()
    col2.setSpacing(8)
    for caption, kind in (("Step rail: first start", 0), ("Step rail: ready, pattern drawn", 1),
                          ("Step rail: armed for BLACS, Run shown", 2)):
        col2.addWidget(label(caption, role="caption"))
        r = StepRail()
        if kind == 0:
            r.show_progress(False, "none", 0, "off")
        elif kind == 1:
            r.show_progress(True, "loaded", 48, "off")
            r.set_current(1)
        else:
            r.show_progress(True, "loaded", 48, "armed")
            r.set_current(2)
            r.buttons[1].setFocus()
        r.setFixedWidth(392)
        col2.addWidget(r)
    col2.addWidget(label("Jog pad", role="caption"))
    j = JogPad()
    j.setFixedWidth(392)
    col2.addWidget(j)
    col2.addWidget(label("Jog pad, locked", role="caption"))
    j2 = JogPad()
    j2.setFixedWidth(392)
    j2.set_moves_enabled(False, "A raster is active: stop it before moving the stage by hand.")
    col2.addWidget(j2)
    col2.addWidget(label("Log line, newest message is a refusal", role="caption"))
    col2.addWidget(log_line())
    col2.addWidget(label("Log line, expanded", role="caption"))
    col2.addWidget(log_line(expanded=True))
    col2.addStretch(1)
    wrap2 = QtWidgets.QWidget()
    wrap2.setLayout(col2)
    wrap2.setFixedWidth(760)
    pg_.addWidget(wrap2, 0, 1, 3, 1)
    save(parts, os.path.join(out, f"parts-{name}.png"), (1240, 1250))
    focus_sheet(os.path.join(out, f"focus-{name}.png"))


def focus_sheet(path: str) -> None:
    """Each kind of control with keyboard focus, side by side (focus needs an active window)."""
    from PyQt5 import QtGui
    shots = []

    def grab_focused(host, target, caption, size):
        host.resize(*size)
        host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
        host.show()
        QtWidgets.QApplication.setActiveWindow(host)
        target.setFocus(QtCore.Qt.TabFocusReason)
        pump()
        shots.append((caption, host.grab()))

    for attr, caption in (("exposure_box", "number box"), ("exposure_slider", "slider"),
                          ("mirror_x", "check box"), ("rotation", "drop-down"), ("save_btn", "button")):
        p = camera_panel()
        grab_focused(p, getattr(p, attr), f"Focus: {caption}", (392, 560))
    r = StepRail()
    grab_focused(r, r.buttons[1], "Focus: step", (392, 64))
    j = JogPad()
    grab_focused(j, j.arrow_buttons[(0, 1)], "Focus: jog arrow", (392, 190))
    cols = 4
    cell_w = 392 + 24
    rows = (len(shots) + cols - 1) // cols
    cell_h = 600
    img = QtGui.QImage(cols * cell_w, rows * cell_h, QtGui.QImage.Format_ARGB32)
    img.fill(QtGui.QColor(theme.tokens()["ground"]))
    painter = QtGui.QPainter(img)
    painter.setPen(QtGui.QColor(theme.tokens()["label"]))
    painter.setFont(theme.font("caption"))
    for i, (caption, pm) in enumerate(shots):
        x, y = (i % cols) * cell_w + 12, (i // cols) * cell_h + 8
        painter.drawText(x, y + 14, caption)
        pm.setDevicePixelRatio(1.0)
        painter.drawPixmap(x, y + 22, pm.scaled(392, cell_h - 30, QtCore.Qt.KeepAspectRatio,
                                                QtCore.Qt.SmoothTransformation))
    painter.end()
    img.save(path)
    print("wrote", path)


def main() -> int:
    os.makedirs(ARGS.out_dir, exist_ok=True)
    themes = [False, True]
    if ARGS.light_only:
        themes = [True]
    elif ARGS.dark_only:
        themes = [False]
    for light in themes:
        render(ARGS.out_dir, light)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
