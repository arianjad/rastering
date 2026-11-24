"""The main window as the operator sees it: a real RasterWindow, offscreen, on the real controller
with two in-memory motors (tests/rig.py) and the fake camera (camera_sim.SimCamera) in its real
thread. No Kinesis, no camera SDK, no socket; BLACS talks over bbx_zmq.InMemoryTransport.

    QT_QPA_PLATFORM=offscreen ZMQ_V2_REQUIRED=1 py -3.13 -m pytest -q tests/test_window.py
"""
from __future__ import annotations

import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from rig import Rig, write_calibration  # noqa: E402

from camera_sim import SimCamera  # noqa: E402
from camera_thorlabs import DisplayGeometry, ThorCamConfig, ThorCameraThread  # noqa: E402
from window import PATTERN, RUN, RasterWindow  # noqa: E402

CAM = ThorCamConfig(exposure_ms=50.0, gain=10, width=1440, height=1080, max_fps=30.0)
GEOM = DisplayGeometry(rotation_k=-1, flip_x=False, flip_y=True)    # as the lab's camera_params.ini
LINE = {"kind": "square_x", "bounds": [100, 120, 200, 200], "xstep": 10, "ystep": 10}   # 3 points [px]


def pump_until(cond, timeout_s: float = 3.0) -> None:
    t = time.monotonic()
    while not cond():
        if time.monotonic() - t > timeout_s:
            raise AssertionError("timed out waiting")
        APP.processEvents()
        time.sleep(0.003)


def pump(seconds: float = 0.05) -> None:
    t = time.monotonic()
    while time.monotonic() - t < seconds:
        APP.processEvents()
        time.sleep(0.003)


@pytest.fixture
def make(tmp_path):
    made = []

    def build(*, calibrated=True, blacs=False, pattern=LINE, run=None):
        rig = Rig(tmp_path, calibrated=calibrated, blacs=blacs)
        defaults = tmp_path / "settings_defaults.json"
        saved = {}
        if pattern is not None:
            saved["pattern"] = pattern
        saved["run"] = {"pingpong": False, "continuous": False, **(run or {})}
        defaults.write_text(json.dumps(saved))
        cam = ThorCameraThread(CAM, camera_class=SimCamera)
        win = RasterWindow(rig.ctl, cam, camera_config=CAM, geometry=GEOM, log_dir=str(tmp_path / "logs"),
                           defaults_path=str(defaults))
        made.append((rig, win))
        win.resize(1280, 900)
        win.show()
        cam.start()
        pump()
        return rig, win

    yield build
    for rig, win in made:
        win.close()
        rig.close()


def run_buttons(win) -> set:
    r = win.run
    return {name for name in ("start", "arm_blacs", "step", "stop", "hand", "take_back", "goto")
            if getattr(r, name + "_btn").isEnabled()}


def test_buttons_offer_only_what_the_raster_state_allows(make):
    """Harm stopped: a button the state does not allow is live (Step or a hand jog while BLACS
    steps the stage, Start on top of a run), so the stage leaves the armed path between shots;
    or the one action the operator needs is greyed with no reason given. Each state offers its
    own actions, and a greyed button says why."""
    rig, win = make()
    assert run_buttons(win) == {"start", "arm_blacs"}
    assert win.jog.arrow_buttons[(0, 1)].isEnabled()

    win.run.start_btn.click()                                   # step mode: held at the GUI
    pump_until(lambda: win.bar.status()["state"] == "local")
    assert run_buttons(win) == {"step", "stop", "hand"}
    assert not win.jog.arrow_buttons[(0, 1)].isEnabled()
    assert "stop it before moving the stage" in win.jog.reason.text()

    win.run.hand_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "armed")
    assert run_buttons(win) == {"stop", "take_back"}
    assert win.run.step_btn.toolTip() == "BLACS steps this raster: Take back first."
    assert win.bar.state_block.text() == "ARMED FOR BLACS"

    win.run.stop_btn.click()
    win.setup.clear_btn.click()
    pump_until(lambda: win.bar.status()["calibration"] == "none")
    assert run_buttons(win) == set()
    assert win.run.start_btn.toolTip() == "Calibrate first (Setup)."


def test_escape_stops_a_continuous_run(make):
    """Harm stopped: a continuous run keeps moving the stage (and the laser keeps ablating) while
    the operator looks for the Stop button. Esc anywhere in the window stops it; no further
    point is sent."""
    rig, win = make(run={"continuous": True, "delay_s": 0.02})
    win.run.start_btn.click()
    pump_until(lambda: len(rig.moves()) >= 4)
    QtWidgets.QApplication.setActiveWindow(win)
    QTest.keyClick(win.pattern.xstep, QtCore.Qt.Key_Escape)    # focus in a number box
    pump_until(lambda: win.bar.status()["state"] == "off")
    rig.idle()
    n = len(rig.moves())
    pump(0.15)
    assert len(rig.moves()) == n
    assert win.run.start_btn.isEnabled() and not win.run.stop_btn.isEnabled()


def test_a_pattern_edit_after_arming_shows_the_edited_notice_until_re_armed(make):
    """Harm stopped: BLACS keeps stepping the old armed path while the image shows the edited
    pattern, and the operator believes the new pattern is being shot. An edit after arming shows
    the notice and the Re-arm button; re-arming arms the edited pattern and BLACS keeps it."""
    rig, win = make()
    win.run.arm_blacs_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "armed")
    assert not win.bar.edited_notice.isVisibleTo(win) and not win.run.rearm_btn.isVisibleTo(win)

    win.pattern.xstep.setValue(5.0)                             # 5 points instead of 3
    pump_until(lambda: win.bar.status()["pattern_edited"])
    assert win.bar.edited_notice.isVisibleTo(win)
    assert win.run.rearm_btn.isVisibleTo(win) and win.run.rearm_btn.isEnabled()
    assert len(rig.ctl.raster_snapshot().path) == 3

    win.run.rearm_btn.click()
    pump_until(lambda: not win.bar.status()["pattern_edited"])
    snap = rig.ctl.raster_snapshot()
    assert (snap.state, len(snap.path)) == ("armed", 5)
    assert not win.bar.edited_notice.isVisibleTo(win)


def test_blacs_arms_the_pattern_drawn_in_the_window(make):
    """Harm stopped: BLACS arms a pattern other than the one on the operator's screen (an old one,
    or none) and ablates spots nobody chose. The corners clicked on the image are the pattern
    BLACS arms and steps."""
    rig, win = make(blacs=True, pattern={"kind": "hull", "xstep": 10, "ystep": 10})
    win._show_step(PATTERN)
    for x, y in ((100, 100), (130, 100), (130, 120), (100, 120)):
        win._on_click(float(x), float(y), 0)
    pump()
    shown = win._points
    assert len(shown) == 12 and win.pattern.count.text() == "12 points"

    assert rig.client.press("arm") == {"path_len": 12, "dropped": 0}
    first = rig.client.press("next_point")
    assert (first["x"], first["y"]) == shown[0]
    pump_until(lambda: win.bar.status()["state"] == "armed" and win.bar.status()["point_index"] == 0)
    assert win.bar.point.value.text() == "1 of 12"


def test_startup_with_a_saved_calibration_shows_it_in_setup(make, tmp_path):
    """Harm stopped: after a restart the window says "no calibration", or shows another user home
    or backlash, while the controller uses the file; the operator recalibrates, or saves the
    wrong values over a good file. Loaded at startup (as main_rastering does), the file's name,
    fit, user home and backlash all show in Setup."""
    rig, win = make(calibrated=False)
    path = write_calibration(tmp_path / "cal_lab.json", user_home={"x": 2.5, "y": 3.5},
                             backlash={"x": 0.012, "y": 0.034}, saved_at="2026-11-20T14:03:11")
    assert rig.ctl.load_calibration(path).ok
    pump_until(lambda: win.setup.backlash_y.value() == 0.034)
    assert win.setup.cal_state.text() == "Calibrated: cal_lab.json"
    assert win.setup.matrix.isVisibleTo(win)
    assert win.setup.user_home_reading.text() == "x 2.500 mm,  y 3.500 mm"
    assert win.setup.backlash_x.value() == 0.012
    assert win.bar.status()["calibration"] == "loaded"
    assert win.run.start_btn.isEnabled()


def test_saved_scan_bounds_survive_the_first_camera_frame(make):
    """Harm stopped: the bounds the operator saved as defaults are replaced by the whole frame at
    every start (the old window did this), so the next pattern covers the whole image and the
    stage runs over spots outside the target. Saved bounds stay; with none saved, the first
    frame fills them."""
    rig, win = make(pattern={**LINE, "bounds": [10, 300, 20, 400]})
    pump_until(lambda: win.view.frames_painted > 0 and win._roi[0] == 1440)
    pump(0.1)
    assert win.pattern.bounds() == (10, 300, 20, 400)

    rig2, win2 = make(pattern=None)
    pump_until(lambda: win2.view.frames_painted > 0)
    pump_until(lambda: win2.pattern.bounds() == (0, 1080, 0, 1440))


def test_setting_the_user_home_saves_it_in_the_calibration_file(make):
    """Harm stopped: the user home lives only in the calibration file; set in the window but not
    saved, it is gone at the next start and "Go to user home" drives the stage to the old place.
    Set writes it into the calibration file in use at once."""
    rig, win = make()
    win.setup.user_home_x.setValue(4.25)
    win.setup.user_home_y.setValue(5.5)
    win.setup.user_home_set_btn.click()
    pump()
    with open(rig.ctl.calibration_file.path) as f:
        assert json.load(f)["user_home"] == {"x": 4.25, "y": 5.5}


def test_jog_up_moves_the_spot_up_on_the_screen(make):
    """Harm stopped: an arrow moves the spot the other way on the image (the display is rotated and
    mirrored), so the operator jogs the beam off the target. With a calibration, Up moves the
    stage so the spot goes up the screen by the step: here the image is mirrored up-down, so
    up the screen is smaller image y, and stage y = 0.01 mm/px * image y."""
    rig, win = make()
    win.jog.step_box.setValue(0.05)
    before = rig.ctl.position.mm
    win.jog.arrow_buttons[(0, 1)].click()
    rig.idle()
    assert rig.moves() == [pytest.approx(before[0]), pytest.approx(before[1] - 0.05)]
