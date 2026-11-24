"""Open the real window offscreen with simulated hardware, drive it through its states, save PNGs.

    py -3.13 scripts/screenshot_window.py OUT_DIR [--states fresh,calibrated,...] [--dark-only]

No camera, no motors, no network port, nothing written outside a temporary folder (calibration
files, defaults) and OUT_DIR. The theme is applied, never saved. Writes
    <state>-<dark|light>-<width>.png   at 1280 x 900 and 2560 x 1440 logical px
States, in order (each builds on the one before): fresh, calibrating, calibrated, hull, running,
armed (for BLACS, with an edited pattern and a refusal in the log).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
STATES = ("fresh", "calibrating", "calibrated", "hull", "running", "armed")
SIZES = ((1280, 900), (2560, 1440))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out_dir")
    p.add_argument("--states", default=",".join(STATES))
    p.add_argument("--dark-only", action="store_true")
    p.add_argument("--sizes", default="1280,2560")
    return p.parse_args(argv)


ARGS = parse_args()
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
os.environ["QT_SCALE_FACTOR"] = str(96.0 / 100.0)     # offscreen reports 100 dpi; make it 1.0

from PyQt5 import QtCore, QtWidgets  # noqa: E402

import theme  # noqa: E402

theme.setup_high_dpi()
APP = QtWidgets.QApplication(["screenshot_window"])
theme.apply_theme(APP, light=False)

from camera_sim import SimCamera  # noqa: E402
from camera_thorlabs import DisplayGeometry, ThorCamConfig, ThorCameraThread  # noqa: E402
from motors_sim import SimulatedMotor  # noqa: E402
from raster_controller import SystemController  # noqa: E402
from window import PATTERN, RUN, SETUP, RasterWindow  # noqa: E402

GEOM = DisplayGeometry(rotation_k=-1, flip_x=False, flip_y=True)       # as camera_params.ini
CAM = ThorCamConfig(exposure_ms=56.3, gain=24, max_fps=15.0)
M, B = [[0.0, -0.011], [-0.011, 0.0]], [16.0, 13.0]                    # px -> mm, stage axes swapped


def pump(seconds: float) -> None:
    t = time.monotonic()
    while time.monotonic() - t < seconds:
        APP.processEvents()
        time.sleep(0.01)


def until(cond, timeout: float = 5.0) -> None:
    t = time.monotonic()
    while not cond():
        if time.monotonic() - t > timeout:
            raise RuntimeError("timed out")
        APP.processEvents()
        time.sleep(0.01)


def main() -> int:
    out = ARGS.out_dir
    os.makedirs(out, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="raster-shots-")
    ctl = SystemController(SimulatedMotor("X", position_mm=6.2, speed_mm_s=20),
                           SimulatedMotor("Y", position_mm=3.1, speed_mm_s=20), travel_mm=(0, 12, 0, 12),
                           calibration_path=os.path.join(tmp, "calibration_data.json"),
                           pointer_path=os.path.join(tmp, "last_calibration_state.json"))
    camera = ThorCameraThread(CAM, camera_class=SimCamera)
    win = RasterWindow(ctl, camera, camera_config=CAM, geometry=GEOM,
                       camera_file=os.path.join(REPO, "camera_params.ini"),
                       log_dir=os.path.join(tmp, "logs"), defaults_path=os.path.join(tmp, "defaults.json"))
    win.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    win.resize(1280, 900)
    win.show()
    camera.start()
    until(lambda: win.bar.status()["camera_fps"] is not None)
    widths = {int(w) for w in ARGS.sizes.split(",")}
    wanted = set(ARGS.states.split(","))

    def shoot(state: str) -> None:
        if state not in wanted:
            return
        for light in ([False] if ARGS.dark_only else [False, True]):
            theme.apply_theme(APP, light=light)                 # applied only, never saved
            for w, h in SIZES:
                if w not in widths:
                    continue
                win.resize(w, h)
                pump(0.25)
                win.view.paint_now()
                path = os.path.join(out, f"{state}-{'light' if light else 'dark'}-{w}.png")
                win.grab().save(path)
                print("wrote", path)
        theme.apply_theme(APP, light=False)
        win.resize(1280, 900)
        pump(0.1)

    # fresh start: no calibration, Setup shown
    pump(0.3)
    shoot("fresh")

    # calibrating: one spot clicked, the next asked for
    win.setup.calibrate_btn.click()
    win._on_click(560.0, 460.0, 0)
    until(lambda: win.calibration_state() == "collecting" and win._cal_progress[0] == 1)
    ctl.jog_mm(1.0, 0.0)
    shoot("calibrating")
    win.setup.cancel_btn.click()
    pump(0.1)

    # calibrated: a file with the camera settings in use
    cal = os.path.join(tmp, "cal_2026-11-20.json")
    with open(cal, "w") as f:
        json.dump({"calibration_matrix": M, "calibration_offset": B, "user_home": {"x": 6.0, "y": 6.0},
                   "backlash": {"x": 0.01, "y": 0.02}, "camera_settings": win.camera_settings(),
                   "saved_at": "2026-11-20T14:03:11", "notes": ""}, f)
    ctl.load_calibration(cal)
    until(lambda: win.calibration_state() == "loaded")
    win._on_click(520.0, 700.0, 0)        # a Go to x, y target
    pump(0.3)
    shoot("calibrated")

    # hull pattern drawn on the Pattern step
    win._show_step(PATTERN)
    win.pattern.kind.setCurrentIndex(win.pattern.kind.findData("hull"))
    win.pattern.xstep.setValue(40.0)
    win.pattern.ystep.setValue(40.0)
    for x, y in ((330, 520), (560, 380), (800, 500), (840, 820), (600, 1000), (360, 880)):
        win._on_click(float(x), float(y), 0)
    win.pattern.show_direction.setChecked(True)
    pump(0.2)
    shoot("hull")

    # running: a continuous run from the Run step
    win._show_step(RUN)
    win.run.continuous.setChecked(True)
    win.run.delay.setValue(0.2)
    win.run.start_btn.click()
    pump(2.0)
    shoot("running")

    # armed for BLACS, then the pattern edited; a refused request in the log
    win.run.stop_btn.click()
    win.run.continuous.setChecked(False)
    win.run.arm_blacs_btn.click()
    pump(0.2)
    win._on_click(560.0, 600.0, int(QtCore.Qt.ControlModifier))
    win._show_step(PATTERN)
    win._on_click(330.0, 700.0, 0)        # one more corner: the pattern now differs from the armed one
    win._show_step(RUN)
    ctl.step()                            # refused: BLACS steps this raster
    pump(0.3)
    shoot("armed")

    win.close()
    ctl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
