"""The main window as the operator sees it: a real RasterWindow, offscreen, on the real controller
with two in-memory motors (tests/rig.py) and the fake camera (camera_sim.SimCamera) in its real
thread. No Kinesis, no camera SDK, no socket; BLACS talks over bbx_zmq.InMemoryTransport.

    QT_QPA_PLATFORM=offscreen ZMQ_V2_REQUIRED=1 py -3.13 -m pytest -q tests/test_window.py
"""
from __future__ import annotations

import json
import math
import os
import threading
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
# px -> mm like the lab's fit (axes swapped, ~0.011 mm/px), made uneven and not symmetric so that
# M, its transpose and its inverse all point an arrow somewhere different.
SKEW_M, SKEW_B = [[0.002, -0.011], [-0.009, 0.001]], [16.0, 13.0]
ARROWS = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}


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

    def build(*, calibrated=True, blacs=False, pattern=LINE, run=None, start_camera=True):
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
        if start_camera:
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


def hand_moves(win) -> set:
    """The enabled controls that move the stage, or change how it moves, by hand."""
    j, s = win.jog, win.setup
    named = {f"jog {name}": j.arrow_buttons[d] for name, d in ARROWS.items()}
    named.update({"go to x, y": j.go_btn, "go to user home": j.home_btn, "device home": s.home_both_btn,
                  "backlash set": s.backlash_set_btn})
    return {name for name, w in named.items() if w.isEnabled()}


def saved(path) -> dict:
    with open(path) as f:
        return json.load(f)


def type_into(box, text: str) -> None:
    """Type over a number box's value as the operator does, without Enter."""
    QtWidgets.QApplication.setActiveWindow(box.window())
    box.setFocus()
    box.selectAll()
    QTest.keyClicks(box, text)


def test_buttons_offer_only_what_the_raster_state_allows(make):
    """Harm stopped: a button the state does not allow is live (Step, a hand jog or a backlash
    change while BLACS steps the stage, Start on top of a run), so the stage leaves the armed path
    between shots; or the one action the operator needs is greyed with no reason given. Each state
    offers its own actions, and a greyed button says why."""
    rig, win = make()
    assert run_buttons(win) == {"start", "arm_blacs"}
    assert len(hand_moves(win)) == 8

    win.run.start_btn.click()                                   # step mode: held at the GUI
    pump_until(lambda: win.bar.status()["state"] == "local")
    assert run_buttons(win) == {"step", "stop", "hand"}
    assert hand_moves(win) == set()
    assert "stop it before moving the stage" in win.jog.reason.text()

    win.run.hand_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "armed")
    assert run_buttons(win) == {"stop", "take_back"}
    assert win.run.step_btn.toolTip() == "BLACS steps this raster: Take back first."
    assert win.bar.state_block.text() == "ARMED FOR BLACS"
    assert hand_moves(win) == set()
    assert win.setup.backlash_set_btn.toolTip() == "A raster is active: stop it first."

    win.run.stop_btn.click()
    win.run.continuous.setChecked(True)
    win.run.delay.setValue(0.02)
    win.run.start_btn.click()                                   # a continuous run
    pump_until(lambda: win.bar.status()["state"] == "running")
    assert run_buttons(win) == {"stop", "hand"}
    assert hand_moves(win) == set()

    win.run.stop_btn.click()
    win.setup.clear_btn.click()
    pump_until(lambda: win.bar.status()["calibration"] == "none")
    assert run_buttons(win) == set()
    assert win.run.start_btn.toolTip() == "Calibrate first (Setup)."


def test_pause_and_continue_buttons_hold_and_resume_a_run(make):
    """Harm stopped: the window had no way to pause a continuous run and pick it up again; the
    operator had to Stop (losing the place) or misuse Hand to BLACS. Pause is offered while it
    runs and holds it at the GUI; Continue is offered while held and runs it again."""
    rig, win = make(run={"continuous": True, "delay_s": 0.02})
    win.run.start_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "running")
    assert win.run.pause_btn.isEnabled() and not win.run.continue_btn.isEnabled()
    win.run.pause_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "local")
    assert win.run.continue_btn.isEnabled() and not win.run.pause_btn.isEnabled()
    win.run.continue_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "running")
    win.run.stop_btn.click()


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


def test_escape_stops_a_raster_blacs_armed_a_moment_ago(make):
    """Harm stopped: BLACS arms the raster and the operator presses Esc before the window has
    drawn the news; the window still believed nothing was armed, so Esc did nothing and BLACS
    went on stepping the stage. Esc stops whatever raster the controller has, at once."""
    rig, win = make(blacs=True)
    QtWidgets.QApplication.setActiveWindow(win)
    assert rig.client.press("arm")["path_len"] == 3          # BLACS's thread; no Qt events run since
    QTest.keyClick(win, QtCore.Qt.Key_Escape)
    assert rig.ctl.raster_snapshot().state == "off"


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
    assert first["point_index"] == 0 and win.bar.point.value.text() == "0 of 12"   # the shot file's number


def test_a_point_index_from_the_shot_file_goes_to_that_point(make):
    """Harm stopped: the operator reads point_index 2 in a shot file, types 2 into Point and
    presses Go to; the old box counted from 1, so the stage went to point_index 1 and the next
    shots hit the wrong spot. Every point number in the window is the shot file's (first = 0)."""
    rig, win = make()
    win.run.start_btn.click()                                   # arm here: step at the GUI
    pump_until(lambda: win.bar.status()["state"] == "local")
    type_into(win.run.point_box, "2")
    QTest.keyClick(win.run.point_box, QtCore.Qt.Key_Return)
    win.run.goto_btn.click()
    rig.idle()
    assert rig.moves()[-2:] == [pytest.approx(1.2), pytest.approx(2.0)]   # point 2 = (120, 200) px
    pump_until(lambda: win.bar.status()["point_index"] == 2)
    assert win.bar.point.value.text() == "2 of 3"
    assert "point_index" in win.run.point_box.toolTip()


def test_a_click_on_the_run_step_with_nothing_armed_sets_the_go_to_target(make):
    """Harm stopped: on the Run step with nothing armed, a click on the image (meant as "go
    there", as on Setup) gave a red refusal; red lines that are not faults teach the operator to
    ignore the real ones. With nothing armed the click sets the Go to x, y target, quietly."""
    rig, win = make()
    win._show_step(RUN)
    win._on_click(300.0, 400.0, 0)
    pump()
    assert (win.jog.x_box.value(), win.jog.y_box.value()) == (pytest.approx(3.0), pytest.approx(4.0))
    assert rig.refusals() == []


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
    rig, win = make(pattern={**LINE, "bounds": [10, 300, 20, 400]}, start_camera=False)
    infos = []
    win.camera.info.connect(infos.append)        # after the window's own slot: it has run first
    win.camera.start()
    pump_until(lambda: infos and win.view.frames_painted > 0)
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


def test_a_failed_backlash_set_still_saves_the_axis_that_took_it(make):
    """Harm stopped: Set sends x and y; y's motor fails, x takes its new backlash, but the file
    was saved only once both had answered, so it never was: the next start loads the old x into
    the motor. And the failed axis stayed pending, so a later answer (after loading another file)
    rewrote that other file. Each axis that took its value is saved; a failed one is let go."""
    rig, win = make()

    def lost(_value):
        raise RuntimeError("KCube Y lost")

    rig.y.set_backlash = lost
    win.setup.backlash_x.setValue(0.05)
    win.setup.backlash_y.setValue(0.02)
    win.setup.backlash_set_btn.click()
    rig.idle()
    pump_until(lambda: saved(rig.ctl.calibration_file.path).get("backlash") == {"x": 0.05, "y": 0.0})


def test_a_backlash_answer_after_loading_another_file_does_not_rewrite_it(make, tmp_path):
    """Harm stopped: the operator sets the backlash while the stage is busy, then loads another
    calibration file before the motor answers; the answer saved the operator's value into the
    newly loaded file, whose own backlash the motor then took: file and motor disagree, and the
    file was changed without anyone saving it. Loading a file lets go of the pending Set."""
    rig, win = make()
    other = write_calibration(tmp_path / "cal_other.json", backlash={"x": 0.03, "y": 0.0})
    with open(other) as f:
        before = f.read()
    rig.x.gate = threading.Event()                  # a slow move: the Set waits behind it
    rig.ctl.jog_mm(0.1, 0.0)
    assert rig.x.moving.wait(2)
    win.setup.backlash_x.setValue(0.05)
    win.setup.backlash_set_btn.click()
    assert rig.ctl.load_calibration(other).ok
    rig.x.gate.set()
    rig.idle()
    pump_until(lambda: win.setup.backlash_reading.text().startswith("x 0.0300"))
    pump(0.05)
    with open(other) as f:
        assert f.read() == before


def test_an_arrow_moves_the_step_typed_even_before_enter(make):
    """Harm stopped: with Step at 1 mm the operator types 0.05 and clicks an arrow without Enter
    (the arrows take no focus, so the box had not taken the number); the stage moved 1 mm, 20
    times further than shown, off the target. An arrow moves the step the box shows."""
    rig, win = make()
    win.jog.step_box.setValue(1.0)
    type_into(win.jog.step_box, "0.05")
    before = rig.ctl.position.mm
    QTest.mouseClick(win.jog.arrow_buttons[(0, 1)], QtCore.Qt.LeftButton)
    rig.idle()
    x, y = rig.moves()
    assert math.hypot(x - before[0], y - before[1]) == pytest.approx(0.05)


def test_jog_arrows_move_the_spot_that_way_on_the_screen(make, tmp_path):
    """Harm stopped: an arrow moves the spot another way on the image (the display is rotated and
    mirrored, the stage axes are swapped against the camera's), so the operator jogs the beam off
    the target. With a calibration each arrow moves the stage by the step, so the spot goes that
    way on the screen: the image is mirrored up-down here, so up the screen is smaller image y."""
    rig, win = make()
    assert rig.ctl.load_calibration(write_calibration(tmp_path / "cal_lab.json", calibration_matrix=SKEW_M,
                                                      calibration_offset=SKEW_B)).ok
    pump()
    win.jog.set_step_mm(0.05)
    cal = rig.ctl.calibration
    for name, (sx, sy) in ARROWS.items():
        before = rig.ctl.position.mm
        win.jog.arrow_buttons[(sx, sy)].click()
        rig.idle()
        after = rig.ctl.position.mm
        assert math.hypot(after[0] - before[0], after[1] - before[1]) == pytest.approx(0.05), name
        (u0, v0), (u1, v1) = cal.mm_to_px(*before), cal.mm_to_px(*after)
        shown = (u1 - u0, -(v1 - v0))                # image px -> screen (+y up; mirrored up-down)
        n = math.hypot(*shown)
        assert (shown[0] / n, shown[1] / n) == (pytest.approx(sx, abs=1e-9), pytest.approx(sy, abs=1e-9)), name


def test_uncalibrated_arrows_move_the_stage_axes_and_say_so(make, tmp_path):
    """Harm stopped: without a calibration the arrows can only follow the stage axes, which may
    not match the rotated, mirrored image; the operator, not told, jogs the beam the wrong way off
    the target. The pad says to check the direction on the image, and the arrows keep today's
    fixed mapping for the lab's display (rotated 90 deg clockwise, mirrored up-down)."""
    rig, win = make(calibrated=False)
    assert win.jog.note.isVisibleTo(win)
    assert win.jog.note.text() == "Not calibrated: arrows follow the stage axes; check the direction on the image."
    win.jog.set_step_mm(0.05)
    seen = {}
    for name, d in ARROWS.items():
        before = rig.ctl.position.mm
        win.jog.arrow_buttons[d].click()
        rig.idle()
        after = rig.ctl.position.mm
        seen[name] = (round(after[0] - before[0], 6), round(after[1] - before[1], 6))
    assert seen == {"up": (0.0, 0.05), "down": (0.0, -0.05), "left": (-0.05, 0.0), "right": (0.05, 0.0)}
    assert rig.ctl.load_calibration(write_calibration(tmp_path / "cal_new.json")).ok
    pump()
    assert not win.jog.note.isVisibleTo(win)


@pytest.mark.parametrize("text", ['{"pattern": {"xstep": 20}, "jog": {"step_mm": "fast"}}', "{not json"])
def test_a_bad_defaults_file_still_opens_the_window_and_says_so(tmp_path, text):
    """Harm stopped: one hand-edited value of the wrong type in settings_defaults.json stopped the
    window opening at all (no GUI for the next shots), and a file that is not JSON was ignored
    without a word, so the operator's saved pattern silently became the placeholders. The window
    opens, with what could be used, and says in red that the defaults file was not (all) used."""
    rig = Rig(tmp_path)
    (tmp_path / "settings_defaults.json").write_text(text)
    cam = ThorCameraThread(CAM, camera_class=SimCamera)
    win = RasterWindow(rig.ctl, cam, camera_config=CAM, geometry=GEOM, log_dir=str(tmp_path / "logs"),
                       defaults_path=str(tmp_path / "settings_defaults.json"))
    try:
        errors = [t for _s, t, error in win.log._lines if error]
        assert any("settings_defaults.json" in t for t in errors)
        assert win._points                            # a pattern is built all the same
    finally:
        win.close()
        rig.close()


def test_a_blacs_link_that_did_not_start_is_shown_when_the_window_opens(tmp_path, monkeypatch):
    """Harm stopped: the BLACS link fails to start (its port taken, e.g. by another copy of this
    GUI) before the window exists, so the one message saying so went nowhere and the operator
    learnt it only when BLACS's shots failed. The window shows the link's words when it opens.
    The port is never bound: the socket maker is replaced by one that fails."""
    import raster_controller as rc

    def port_taken(*_args, **_kwargs):
        raise OSError("Address in use")

    monkeypatch.setattr(rc, "ZmqRepTransport", port_taken)
    rig = Rig(tmp_path)
    rig.ctl.start_menu_server()
    pump_until(lambda: rig.ctl.blacs_link_status[0] == "error")
    cam = ThorCameraThread(CAM, camera_class=SimCamera)
    win = RasterWindow(rig.ctl, cam, camera_config=CAM, geometry=GEOM, log_dir=str(tmp_path / "logs"),
                       defaults_path=str(tmp_path / "settings_defaults.json"))
    try:
        assert any("BLACS link not started, port 55535: Address in use" in line for line in win.log.lines())
    finally:
        win.close()
        rig.close()
