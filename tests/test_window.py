"""The main window as the operator sees it: a real RasterWindow, offscreen, on the real controller
with two in-memory motors (tests/rig.py) and the fake camera (camera_sim.SimCamera) in its real
thread. No Kinesis, no camera SDK, no socket; BLACS talks over zmq_v2.InMemoryTransport.

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
    """Harm stopped: a raster button the state does not allow is live (Step while BLACS steps the
    stage, Start on top of a run), so two drivers step one path; or a hand move the GUI can do is
    greyed during a raster, so the operator cannot nudge the beam or home a stuck stage without
    stopping the raster first. Each state offers its own raster actions, a greyed one says why,
    and the hand moves stay live in every state (the stage check warns about what they cost)."""
    rig, win = make()
    rig.ctl.set_user_home("X", 2.0)                             # Go to user home needs one
    rig.ctl.set_user_home("Y", 2.0)
    pump()
    assert run_buttons(win) == {"start", "arm_blacs"}
    every_move = hand_moves(win)
    assert len(every_move) == 8

    win.run.start_btn.click()                                   # step mode: held at the GUI
    pump_until(lambda: win.bar.status()["state"] == "local")
    assert run_buttons(win) == {"step", "stop", "hand"}
    assert hand_moves(win) == every_move
    assert not win.jog.reason.isVisibleTo(win)

    win.run.hand_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "armed")
    assert run_buttons(win) == {"stop", "take_back"}
    assert win.run.step_btn.toolTip() == "BLACS steps this raster: Take back first."
    assert win.bar.state_block.text() == "ARMED FOR BLACS"
    assert hand_moves(win) == every_move

    win.run.stop_btn.click()
    win.run.continuous.setChecked(True)
    win.run.delay.setValue(0.02)
    win.run.start_btn.click()                                   # a continuous run
    pump_until(lambda: win.bar.status()["state"] == "running")
    assert run_buttons(win) == {"stop", "hand"}
    assert hand_moves(win) == every_move

    win.run.stop_btn.click()
    win.setup.clear_btn.click()
    pump_until(lambda: win.bar.status()["calibration"] == "none")
    assert run_buttons(win) == set()
    assert win.run.start_btn.toolTip() == "Calibrate first (Setup)."


def test_calibration_buttons_stay_live_during_a_raster_and_arm_while_collecting_clicks(make):
    """Harm stopped: Calibrate, Load, Use last and Clear were greyed while a raster was armed, and
    Start / Arm for BLACS while calibration clicks were being collected, though the GUI can do
    both (a calibration change says where the armed path goes now); the operator had to stop a
    raster to recalibrate. They stay live. The camera region and display stay locked while
    clicks are collected: clicks in two pixel frames would make a fit that aims nowhere."""
    rig, win = make()
    win.run.start_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "local")
    st = win.setup
    assert all(b.isEnabled() for b in (st.calibrate_btn, st.load_btn, st.use_last_btn, st.clear_btn))
    win.run.stop_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "off")
    st.calibrate_btn.click()
    pump_until(lambda: win.bar.status()["calibration"] == "collecting")
    assert win.run.start_btn.isEnabled() and win.run.arm_blacs_btn.isEnabled()
    assert all(b.isEnabled() for b in (st.load_btn, st.use_last_btn, st.clear_btn))
    assert not win.camera_panel.load_btn.isEnabled()
    assert win.camera_panel.load_btn.toolTip() == "Finish or cancel the calibration first."


def test_camera_backlash_and_here_stay_live_during_a_raster_or_a_move(make):
    """Harm stopped: the camera region, rotation, mirror, settings Load / Revert, "Use the
    calibration's camera settings" and backlash Set were locked during a raster, and user home
    "Here" while the stage moved, though none of them moves the stage off the path: px -> mm comes
    from the calibration, and the stale-calibration mark shows a camera change. The operator had
    to stop a raster to fix the image or the backlash. All stay live, with no message."""
    rig, win = make()
    win.run.arm_blacs_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "armed")
    cp, st = win.camera_panel, win.setup
    assert all(w.isEnabled() for w in (cp._roi_group, cp._disp_group, cp.load_btn, st.restore_camera_btn,
                                       st.backlash_set_btn))
    rig.x.gate = threading.Event()                    # the stage is moving
    rig.ctl.jog_mm(0.1, 0.0)
    assert rig.x.moving.wait(2)
    pump_until(lambda: win._busy)
    assert st.user_home_here_btn.isEnabled()
    rig.x.gate.set()


def test_a_big_pattern_is_armed_whole_and_drawn_as_a_subset(make):
    """Harm stopped: a pattern over 50,000 points was refused, so a fine grid could not be run;
    drawn whole, a million points take seconds per redraw and the window stalls on every edit.
    A 100,000-point pattern arms all its points; the image draws one point in 2 (at most 50,000)
    and the Pattern step says so."""
    rig, win = make(pattern={"kind": "square_x", "bounds": [0, 999, 0, 99], "xstep": 1, "ystep": 1})
    pump_until(lambda: len(win._points) == 100_000)
    assert win.pattern.count.text() == "100000 points"
    assert "1 point in 2 drawn" in win.pattern.count_detail.text()
    assert len(win.view._pending_item.getData()[0]) == 50_000
    win.run.arm_blacs_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "armed")
    assert len(rig.ctl.raster_snapshot().path) == 100_000
    assert len(win.view._armed_item.getData()[0]) == 50_000
    rig.ctl.stop_raster()


def test_a_plain_warning_is_not_red_and_a_red_warning_is(make):
    """Harm stopped: every warning shown in the fault colour (red lines that are not faults teach
    the operator to ignore the real ones), or a warning that does need attention (a move outside
    the travel, a hand move under BLACS's armed raster) shown like an info line. A plain warning
    shows in the ink colour; a red warning, a refusal and an error in red."""
    rig, win = make()
    for kind, red in (("info", False), ("warning", False), ("red_warning", True), ("refused", True), ("error", True)):
        rig.ctl.message.emit(kind, f"a {kind} line")
        pump_until(lambda: win.log._lines[-1][1] == f"A {kind} line")
        assert win.log._lines[-1][2] is red, kind


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


def test_escape_leaves_a_raster_blacs_armed_a_moment_ago(make):
    """Harm stopped: an Esc pressed by habit (to leave a number box) while BLACS runs a sequence
    stopped BLACS's raster; the stage parked and BLACS went on shooting one spot. Esc leaves a
    BLACS-armed raster armed, also one armed before the window drew it, and says so; the Stop
    button still stops it."""
    rig, win = make(blacs=True)
    QtWidgets.QApplication.setActiveWindow(win)
    assert rig.blacs.program("arm_raster", 0)["armed"] == 3   # BLACS's thread; no Qt events run since
    QTest.keyClick(win.pattern.xstep, QtCore.Qt.Key_Escape)
    assert rig.ctl.raster_snapshot().state == "armed"
    assert "Esc leaves BLACS's raster armed" in win.log.lines()[-1]
    pump_until(lambda: win.run.stop_btn.isEnabled())
    win.run.stop_btn.click()
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


def test_a_new_delay_reaches_the_armed_raster_at_once_and_is_not_called_a_pattern_edit(make):
    """Harm stopped: changing only the delay per point of a running raster said "Pattern edited
    since arming. Re-arm to run it."; the run kept the old delay, and Re-arm restarted it at point 0,
    so spots were ablated twice. The delay (or Continuous) is taken by the armed raster at once;
    a ping-pong change (a new path) asks for a re-arm as "Run options changed"."""
    rig, win = make(run={"continuous": True, "delay_s": 0.05})
    win.run.start_btn.click()
    pump_until(lambda: win.bar.status()["state"] == "running")
    win.run.delay.setValue(0.02)
    pump()
    snap = rig.ctl.raster_snapshot()
    assert (snap.path_edited, snap.armed_plan.delay_s) == (False, 0.02)
    assert not win.bar.edited_notice.isVisibleTo(win)
    win.run.pingpong.setChecked(True)
    pump_until(lambda: win.bar.edited_notice.isVisibleTo(win))
    assert win.bar.edited_notice.text().startswith("Run options changed since arming.")
    win.run.stop_btn.click()


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

    armed = rig.blacs.program("arm_raster", 0)
    assert (armed["armed"], armed["dropped"]) == (12, 0)
    first = rig.blacs.program("move_to_next", 1)
    assert tuple(first["target_xy"]) == shown[0]
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


def test_a_point_number_being_typed_survives_a_blacs_shot(make):
    """Harm stopped: while BLACS stepped the raster, every shot redrew the Point box and wiped the
    number the operator was typing ("7" became "—"), so Go to selected point went nowhere or to
    another point. What is typed stays until the operator enters it or leaves the box."""
    rig, win = make(blacs=True)
    win._show_step(RUN)
    assert rig.blacs.program("arm_raster", 0)["armed"] == 3
    pump_until(lambda: win.bar.status()["state"] == "armed")
    type_into(win.run.point_box, "2")
    assert rig.blacs.program("move_to_next", 1)["point_index"] == 0
    pump_until(lambda: win.bar.status()["point_index"] == 0)
    assert win.run.point_box.text() == "2"


def test_go_to_selected_point_takes_a_number_typed_without_enter(make):
    """Harm stopped: the operator typed 2 into Point and reached for Go to selected point, which
    stayed greyed until Enter (everywhere else in the window a typed number counts), so they
    pressed something else or thought going to a point was not allowed now. The typed number
    enables the button, and the click goes there."""
    rig, win = make()
    win.run.start_btn.click()                                   # arm here
    pump_until(lambda: win.bar.status()["state"] == "local")
    type_into(win.run.point_box, "2")
    assert win.run.goto_btn.isEnabled()
    win.run.goto_btn.click()
    rig.idle()
    assert rig.moves()[-2:] == [pytest.approx(1.2), pytest.approx(2.0)]


def test_a_click_on_the_pattern_step_that_sets_nothing_says_why(make):
    """Harm stopped: with a square pattern, a click on the image in the Pattern step did nothing
    and said nothing; the operator clicked again and again, or concluded the window had hung. The
    log line says the square pattern takes its box from the numbers."""
    rig, win = make()
    win._show_step(PATTERN)
    n = len(win.log.lines())
    win._on_click(300.0, 400.0, 0)
    assert len(win.log.lines()) == n + 1 and "Scan bounds" in win.log.lines()[-1]


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


def test_go_to_user_home_is_greyed_until_one_is_set(make):
    """Harm stopped: with no user home ever set, Setup showed "x 0.000 mm, y 0.000 mm" as if one
    were set, and Go to user home drove the stage to its corner (0, 0) mm, off the target. Until
    one is set, Setup says "not set" and Go to user home is greyed with the reason."""
    rig, win = make()
    assert win.setup.user_home_reading.text() == "not set"
    assert not win.jog.home_btn.isEnabled()
    assert win.jog.home_btn.toolTip() == "No user home set: set one in Setup (Stage, User home)."
    assert not rig.ctl.go_user_home().result(2).ok
    rig.idle()
    assert rig.moves() == []
    win.setup.user_home_x.setValue(4.0)
    win.setup.user_home_y.setValue(5.0)
    win.setup.user_home_set_btn.click()
    pump()
    assert win.jog.home_btn.isEnabled() and win.setup.user_home_reading.text() == "x 4.000 mm,  y 5.000 mm"


def test_a_user_home_set_does_not_lose_a_backlash_set_still_waiting(make):
    """Harm stopped: backlash Set while the stage was busy, then user home Set: saving the user home
    let go of the backlash waiting for the motor, so the motor took 0.05 mm but the file kept the
    old value, and the next start sent the old value to the motor. Saving the same file keeps the
    waiting backlash; it is saved when the motor answers."""
    rig, win = make()
    path = rig.ctl.calibration_file.path
    rig.x.gate = threading.Event()                  # a slow move: the backlash Set waits behind it
    rig.ctl.jog_mm(0.1, 0.0)
    assert rig.x.moving.wait(2)
    win.setup.backlash_x.setValue(0.05)
    win.setup.backlash_set_btn.click()
    win.setup.user_home_x.setValue(4.0)
    win.setup.user_home_set_btn.click()
    pump()
    rig.x.gate.set()
    rig.idle()
    pump_until(lambda: saved(path)["backlash"]["x"] == 0.05)
    assert saved(path)["user_home"]["x"] == 4.0


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
    import blacs_link

    def port_taken(*_args, **_kwargs):
        raise OSError("Address in use")

    monkeypatch.setattr(blacs_link, "ZmqRepTransport", port_taken)
    rig = Rig(tmp_path)
    rig.ctl.start_blacs_link("tcp://*:55535", "")
    pump_until(lambda: rig.ctl.blacs_link_status[0] == "error")
    cam = ThorCameraThread(CAM, camera_class=SimCamera)
    win = RasterWindow(rig.ctl, cam, camera_config=CAM, geometry=GEOM, log_dir=str(tmp_path / "logs"),
                       defaults_path=str(tmp_path / "settings_defaults.json"))
    try:
        assert any("BLACS link not started on tcp://*:55535: Address in use" in line for line in win.log.lines())
    finally:
        win.close()
        rig.close()


def test_the_spots_box_sets_how_many_clicks_a_calibration_takes(make):
    """Harm stopped: the operator asks for 5 spots to see the fit error, and the window fits after
    3, so the spots meant to catch a mis-click are never taken. Calibrate waits for as many clicks
    as the Spots box says."""
    rig, win = make()
    win.setup.spots.setValue(5)
    win.setup.calibrate_btn.click()
    pump_until(lambda: win.bar.status()["calibration"] == "collecting")
    assert rig.ctl.calibration_clicks == (0, 5)


def test_a_hand_move_warning_shows_under_the_arrows_and_a_clean_move_clears_it(make):
    """Harm stopped: a Go to x, y outside the 0-12 mm travel (sent on to the KCube) was told only
    in the bottom log line, far from the button, and the operator missed it. The warning shows
    under the jog pad, in red; the next move with nothing to say clears it."""
    rig, win = make()
    win.jog.set_go_to(13.0, 5.0)
    win.jog.go_btn.click()
    pump_until(lambda: win.jog.answer.isVisibleTo(win))
    assert "outside the travel" in win.jog.answer.text()
    assert win.jog.answer.property("tone") == "fault"
    win.jog.set_go_to(5.0, 5.0)
    win.jog.go_btn.click()
    pump_until(lambda: not win.jog.answer.isVisibleTo(win))


def test_the_outer_arrows_move_by_the_coarse_step(make):
    """Harm stopped: crossing the stage at the 0.01 mm fine step took hundreds of clicks, or the
    step had to be retyped between fine and coarse moves. The outer (double) arrows move by the
    coarse step, the inner ones by the fine step."""
    rig, win = make(calibrated=False)
    win.jog.set_step_mm(0.01)
    win.jog.set_coarse_mm(0.5)
    x0, y0 = rig.x.pos, rig.y.pos
    win.jog.coarse_buttons[(1, 0)].click()
    rig.idle()
    win.jog.arrow_buttons[(1, 0)].click()
    rig.idle()
    assert abs(rig.x.pos - x0) + abs(rig.y.pos - y0) == pytest.approx(0.51)


def test_a_held_arrow_repeats_but_never_runs_ahead_of_the_stage(make):
    """Harm stopped: holding an arrow queued a jog every 150 ms whatever the stage did, so on a
    slow stage the jogs piled up and the stage kept moving after the button was let go. A held
    arrow repeats only once the last step is done, and letting go adds no step."""
    rig, win = make(calibrated=False)
    gate = threading.Event()                    # the stage stalls on its first move
    rig.x.gate = rig.y.gate = gate
    b = win.jog.arrow_buttons[(1, 0)]
    QTest.mousePress(b, QtCore.Qt.LeftButton)
    pump(1.2)                                   # about 5 repeats: 400 ms, then every 150 ms
    QTest.mouseRelease(b, QtCore.Qt.LeftButton)
    gate.set()
    rig.idle()
    pump()
    jogs = [t for k, t in rig.messages if t.startswith("Jog (") and t.endswith(" ...")]
    assert len(jogs) == 1


def test_rarely_used_camera_sections_start_folded_and_open_with_one_click(make):
    """Harm stopped: the Setup step needed about 500 px of scrolling at 1280 x 900, with the stage
    controls below camera region, display and file settings that are set once. Those three start
    folded; one click on the arrow opens one."""
    rig, win = make()
    cp = win.camera_panel
    sections = (cp._roi_group, cp._disp_group, cp._file_group)
    assert all(s.is_folded() for s in sections)
    cp._roi_group.fold_btn.click()
    assert not cp._roi_group.is_folded()


def test_revert_to_saved_defaults_puts_the_saved_pattern_back(make):
    """Harm stopped: after trying other values, the only way back to the saved pattern was a
    restart, which closes the camera and drops the BLACS link. File > Revert to saved defaults
    puts the saved pattern back."""
    rig, win = make()                            # saved: square, x step 10 px
    win.pattern.xstep.setValue(37)
    file_menu = win.menuBar().actions()[0].menu()
    [a for a in file_menu.actions() if a.text() == "Revert to saved defaults"][0].trigger()
    assert win.pattern.xstep.value() == pytest.approx(10)


def test_no_run_button_stands_out_while_blacs_steps_the_raster(make):
    """Harm stopped: while BLACS stepped the raster, "Take back" was the black highlighted button,
    the most inviting click on the screen; one click parks a running sequence's stage. No Run
    button stands out while BLACS holds the raster."""
    rig, win = make(blacs=True)
    assert rig.blacs.program("arm_raster", 0)["armed"] == 3
    pump_until(lambda: win.bar.status()["state"] == "armed")
    r = win.run
    buttons = (r.start_btn, r.arm_blacs_btn, r.step_btn, r.stop_btn, r.hand_btn, r.take_back_btn,
               r.pause_btn, r.continue_btn)
    assert [b.text() for b in buttons if b.property("kind") == "primary"] == []
