"""The one admission check every stage move passes (stage.py), through the controller's public API
with its real motor thread and two in-memory Z912 axes (tests/rig.py)."""
from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest
from PyQt5.QtCore import Qt

import raster_controller as rc
from config import HardwareConfig
from rig import TRAVEL, Rig, wait_until
from raster_paths import RasterSpec, points_from_spec
from stage import out_of_bounds_message


@pytest.fixture
def gui(tmp_path):
    g = Rig(tmp_path)
    yield g
    g.close()


def _every_move(ctl):
    """Each kind of stage move the window can ask for, by name."""
    return {
        "move in mm": lambda: ctl.move_mm(13.0, 2.0),
        "move one axis": lambda: ctl.move_axis_mm("X", 12.5),
        "jog": lambda: ctl.jog_mm(12.0, 0.0),                    # from 1.0 mm to 13.0 mm
        "move in px": lambda: ctl.move_px(1300.0, 200.0),
        "user home": lambda: (ctl.set_user_home("X", 13.0), ctl.go_user_home())[1],
        "user home X": lambda: (ctl.set_user_home("X", 13.0), ctl.go_user_home("X"))[1],
    }


@pytest.mark.parametrize("kind", list(_every_move(None)))
def test_every_move_kind_outside_the_travel_is_refused_and_moves_nothing(gui, kind):
    """Harm stopped: one kind of move (a jog, a user home, a one-axis move...) skips the travel
    check and drives a Z912 into its end stop. Every kind is refused with the travel words, says
    so in the log, and no motor moves."""
    res = _every_move(gui.ctl)[kind]().result(2)
    assert (res.ok, res.code) == (False, "move_refused")
    assert res.words.startswith("Rejected: motor out of bounds -- ") and "travel is (0.0, 12.0, 0.0, 12.0)" in res.words
    assert gui.moves() == []
    assert any("Rejected: motor out of bounds" in t for t in gui.refusals())


def test_the_travel_words_name_the_point_and_where_it_maps(gui):
    """Harm stopped: the operator (or BLACS) cannot tell which point was refused or by how much.
    A px move names the image point, the motor position and the travel; an mm move the motor
    position and the travel."""
    assert gui.ctl.move_px(1300.0, 200.0).result(2).words == out_of_bounds_message(TRAVEL, (13.0, 2.0), (1300.0, 200.0))
    assert gui.ctl.move_mm(13.0, 2.0).result(2).words == (
        "Rejected: motor out of bounds -- motor (13.0000, 2.0000) mm, travel is (0.0, 12.0, 0.0, 12.0)")


def test_a_target_a_hair_outside_the_travel_moves_onto_the_edge(gui):
    """Harm stopped: a position measured at the travel edge (a few um past it once the other axis
    moved, through the calibration's cross terms) cannot be sent again, or a real overshoot gets
    through. Within 10 um outside, the stage goes onto the edge; further out is refused."""
    assert gui.ctl.move_mm(-0.004, 12.006).result(2).ok
    assert gui.calls[-2:] == [("X", "move", 0.0), ("Y", "move", 12.0)]
    assert not gui.ctl.move_mm(-0.02, 5.0).result(2).ok
    assert gui.moves() == [0.0, 12.0]


def test_a_move_in_pixels_needs_a_calibration(tmp_path):
    """Harm stopped: image pixels are taken as motor millimetres when no calibration is set (the
    spot lands somewhere else, inside the travel, so nothing else stops it). A px move is refused
    with words saying to calibrate; no point counts as reachable; moves in mm still work."""
    g = Rig(tmp_path, calibrated=False)
    try:
        res = g.ctl.move_px(5.0, 5.0).result(2)
        assert (res.ok, res.code) == (False, "not_calibrated")
        assert res.words == "no calibration set: calibrate in the GUI first (x, y are camera pixels)"
        assert g.ctl.reachable([(5.0, 5.0), (500.0, 500.0)]) == [False, False]
        assert g.moves() == []
        assert g.ctl.move_mm(5.0, 5.0).result(2).ok
    finally:
        g.close()


def _hold_raster(g, state):
    """Arm the 3-point line so the raster is in `state`: "local" (stepped at the GUI), "armed"
    (BLACS steps it) or "running" (a continuous run, its first point still moving)."""
    if state == "running":
        g.x.gate = threading.Event()
        g.plan(continuous=True)
    else:
        g.plan()
    assert g.ctl.arm(for_blacs=state == "armed").ok
    assert g.ctl.raster_snapshot().state == state


@pytest.mark.parametrize("state, code", [("local", "raster_held"), ("armed", "raster_armed"),
                                         ("running", "raster_running")])
def test_while_a_raster_is_active_only_its_own_moves_reach_the_stage(gui, state, code):
    """Harm stopped: a jog, a move, a device home or a user home pulls the stage off an armed or
    running raster: the next shot ablates a spot off the path and the shot file records the
    path's point for it. Every other move kind is refused, naming who holds the raster; the
    raster's own step still moves."""
    _hold_raster(gui, state)
    futures = [f() for f in (lambda: gui.ctl.move_mm(5.0, 5.0), lambda: gui.ctl.move_axis_mm("Y", 5.0),
                             lambda: gui.ctl.jog_mm(0.1, 0.0), lambda: gui.ctl.move_px(500.0, 500.0),
                             lambda: gui.ctl.home("X"), lambda: gui.ctl.go_user_home())]
    if state == "running":
        gui.x.gate.set()
    results = [f.result(2) for f in futures]
    assert [r.code for r in results] == [code] * len(futures)
    assert all("stop the raster first" in r.words for r in results)
    assert ("X", "home") not in gui.calls and 5.0 not in gui.moves()
    if state == "local":
        assert gui.ctl.step().ok
        wait_until(lambda: gui.moves() == [1.0, 2.0])


def test_a_raster_point_queued_before_stop_does_not_move_the_stage(gui):
    """Harm stopped: after the operator presses Stop, raster points still queued behind a slow
    move run anyway (the stage keeps moving after Stop). Points queued before Stop are dropped."""
    gui.plan()
    gui.ctl.arm()
    gui.x.gate = threading.Event()
    gui.ctl.step()
    assert gui.x.moving.wait(2)                  # the first point is moving, slowly
    gui.ctl.step()
    gui.ctl.stop_raster()
    gui.x.gate.set()
    gui.idle()
    assert gui.moves("X") == [1.0]
    assert any("dropped: the raster was stopped" in t for t in gui.refusals())


def test_the_arm_time_travel_filter_and_the_move_check_agree_point_by_point(tmp_path):
    """Harm stopped: the arm keeps a point the move check then refuses (a raster fails one shot
    per such point, mid queue), or drops one it would take. With the lab's real calibration and a
    hull drawn across the 500 px frame (its first column maps below the 0 mm travel floor), every
    armed point is taken by the move check and every dropped point is refused; the window's
    reachable() shading says the same."""
    g = Rig(tmp_path, calibrated=False)
    try:
        cal = str(tmp_path / "live.json")
        with open(cal, "w") as f:   # the lab's calibration_data.json, fitted 2026-07-20
            json.dump({"calibration_matrix": [[0.002787742864362515, -2.5420147699350567e-05],
                                              [0.00014685850917358595, 0.0037957276217188197]],
                       "calibration_offset": [-0.30720362288827385, -0.07179191809704172]}, f)
        assert g.ctl.load_calibration(cal).ok
        spec = RasterSpec(kind="hull", hull_points=[(30, 60), (450, 40), (420, 460), (60, 430)], xstep=10, ystep=10)
        g.plan(spec)
        assert g.ctl.arm().ok
        snap = g.ctl.raster_snapshot()
        assert snap.dropped > 0 and snap.pattern_len == len(snap.path)
        g.ctl.stop_raster()
        everything = points_from_spec(spec)
        armed = set(snap.path)
        dropped = [p for p in everything if p not in armed]
        assert len(dropped) == snap.dropped
        assert g.ctl.reachable(everything) == [p in armed for p in everything]
        asked = [(p, True) for p in snap.path] + [(p, False) for p in dropped]
        futures = [g.ctl.move_px(*p) for p, _ in asked]
        assert [f.result(5).ok for f in futures] == [want for _, want in asked]
    finally:
        g.close()


def test_stage_jobs_run_one_at_a_time_in_the_order_asked_with_their_names_in_the_log(gui):
    """Harm stopped: Device Home X then Y run out of order or overlap, or a job runs with no line
    in the log, so the operator cannot tell what the stage is doing. Jobs run first in, first
    out, one at a time; each logs its name when it starts and when it is done."""
    gui.ctl.home("X")
    gui.ctl.home("Y")
    gui.ctl.move_mm(3.0, 4.0).result(2)
    assert [c for c in gui.calls if c[1] in ("home", "move")] == [
        ("X", "home"), ("Y", "home"), ("X", "move", 3.0), ("Y", "move", 4.0)]
    info = [t for k, t in gui.messages if k == "info"]
    assert "Device home X ..." in info and "Device home Y: done, motor (0.0000, 0.0000) mm" in info
    assert info[-1] == "Move to (3.0000, 4.0000) mm: done, motor (3.0000, 4.0000) mm"


def test_the_window_learns_where_the_stage_is_without_asking(gui):
    """Harm stopped: the position on screen goes stale (it only changed on a move the window made)
    or floods the window with identical updates. The motor thread reports the position in mm and
    px when it changed, also when the stage moved by itself, and only then."""
    gui.ctl.move_mm(2.0, 3.0).result(2)
    assert gui.positions[-1].mm == (2.0, 3.0) and gui.positions[-1].px == pytest.approx((200.0, 300.0))
    n = len(gui.positions)
    gui.x.pos = 2.5                                   # moved by hand, at the KCube
    wait_until(lambda: gui.positions[-1].mm == (2.5, 3.0))
    time.sleep(0.25)                                  # another idle read, no change
    assert len(gui.positions) == n + 1
    assert np.allclose(gui.ctl.position.px, (250.0, 300.0))


def test_go_to_point_moves_the_stage_to_that_point_of_the_armed_path(gui):
    """Harm stopped: Go to point is refused by the stage's own check (taken for a foreign move
    while the raster is held at the GUI) or goes to another point, so the operator cannot re-aim
    at a missed spot. It moves the stage to that point; the next step continues after it."""
    gui.plan()
    assert gui.ctl.arm().ok
    assert gui.ctl.go_to_point(1).ok
    gui.idle()
    assert gui.calls[-2:] == [("X", "move", pytest.approx(1.1)), ("Y", "move", 2.0)]
    assert gui.refusals() == []
    assert gui.ctl.step().ok
    gui.idle()
    assert gui.moves("X")[-1] == pytest.approx(1.2)


def test_a_one_axis_move_and_user_home_x_leave_the_other_axis_alone(gui):
    """Harm stopped: Move X (or User home X) also sends Y somewhere (its stale reading, or the user
    home's Y), moving the spot along a direction the operator did not ask for. Only the named
    axis gets a command."""
    gui.ctl.set_user_home("X", 4.0)
    gui.ctl.set_user_home("Y", 7.0)
    assert gui.ctl.move_axis_mm("X", 5.0).result(2).ok
    assert gui.ctl.go_user_home("X").result(2).ok
    assert gui.ctl.move_axis_mm("Y", 6.0).result(2).ok
    assert [c for c in gui.calls if c[1] == "move"] == [("X", "move", 5.0), ("X", "move", 4.0), ("Y", "move", 6.0)]
    assert (gui.x.pos, gui.y.pos) == (4.0, 6.0)


def test_a_raster_point_queued_before_a_re_arm_does_not_move_the_stage(gui):
    """Harm stopped: after the operator arms again (a new pattern), a point of the old raster still
    queued behind a slow move runs anyway: the stage visits a spot of a path that is no longer
    armed. Points of the old arm are dropped; the new arm's first step moves."""
    gui.plan()
    gui.ctl.arm()
    gui.x.gate = threading.Event()
    gui.ctl.step()
    assert gui.x.moving.wait(2)                  # the old arm's first point is moving, slowly
    gui.ctl.step()                               # its second point waits in the queue
    gui.plan(RasterSpec(kind="square_x", bounds=(300.0, 320.0, 400.0, 400.0), xstep=10.0, ystep=10.0))
    assert gui.ctl.arm().ok
    assert gui.ctl.step().ok                     # the new arm's first point: 300 px -> 3.0 mm
    gui.x.gate.set()
    gui.idle()
    assert gui.moves("X") == [1.0, 3.0]
    assert any("dropped: the raster was stopped or armed again" in t for t in gui.refusals())


def test_a_job_cancelled_while_queued_does_not_stop_the_motor_thread(gui):
    """Harm stopped: a stage job whose Future was cancelled while it waited in the queue killed the
    motor thread: every later move (window and BLACS) waited forever, the window showed the stage
    busy for good and the KCubes were never disconnected. The cancelled job is skipped; later
    jobs run; the stage reports idle again."""
    busy = []
    gui.ctl.busy_changed.connect(busy.append, Qt.DirectConnection)
    gui.x.gate = threading.Event()
    first = gui.ctl.move_mm(2.0, 2.0)
    assert gui.x.moving.wait(2)
    assert gui.ctl.move_mm(4.0, 4.0).cancel()
    gui.x.gate.set()
    assert first.result(2).ok
    assert gui.ctl.move_mm(3.0, 3.0).result(2).ok
    assert gui.moves() == [2.0, 2.0, 3.0, 3.0]
    wait_until(lambda: busy[-1] is False)


def test_close_drops_queued_jobs_lets_the_move_in_flight_end_then_disconnects(tmp_path):
    """Harm stopped: closing the GUI ran every queued move first (the stage kept moving after the
    window was gone, a 30 s device home included), or left the KCubes connected (the next start
    cannot open them). Queued jobs are refused ("the GUI is closing") and never run; the move in
    flight finishes; then both motors are disconnected."""
    g = Rig(tmp_path)
    try:
        g.x.gate = threading.Event()
        first = g.ctl.move_mm(3.0, 3.0)
        assert g.x.moving.wait(2)
        queued = [g.ctl.move_mm(5.0, 5.0), g.ctl.home("Y")]
        closing = threading.Thread(target=g.ctl.close)
        closing.start()
        time.sleep(0.05)
        assert closing.is_alive()                      # it waits for the move in flight
        g.x.gate.set()
        closing.join(2)
        assert first.result(0).ok
        for f in queued:
            res = f.result(0)
            assert not res.ok and "the GUI is closing" in res.words
        assert g.moves() == [3.0, 3.0] and ("Y", "home") not in g.calls
        assert g.calls[-2:] == [("X", "disconnect"), ("Y", "disconnect")]
    finally:
        g.close()


def test_close_waits_for_the_job_in_flight_as_long_as_it_may_take_then_says_so(tmp_path):
    """Harm stopped: closing during a device home (up to 30 s) gave up after a fixed 2 s and the
    process exited with the KCubes still connected and nothing said. The close waits as long as
    one stage job may take by the KCube's own time limits (a home, or a move of both axes), and
    when even that runs out it says the motors were not disconnected."""
    g = Rig(tmp_path)
    try:
        g.ctl.close_wait_s = 0.2                       # the test's stand-in for the home's 30 s
        g.x.gate = threading.Event()
        g.ctl.move_mm(3.0, 3.0)
        assert g.x.moving.wait(2)
        t0 = time.monotonic()
        g.ctl.close()
        assert 0.15 < time.monotonic() - t0 < 1.0
        assert any(k == "error" and "without disconnecting the motors" in t for k, t in g.messages)
        assert ("X", "disconnect") not in g.calls
        g.x.gate.set()
        wait_until(lambda: ("Y", "disconnect") in g.calls)   # once the move ends, it still disconnects
    finally:
        g.close()
    assert rc.longest_stage_job_s(HardwareConfig(move_timeout_s=10.0, home_timeout_s=30.0)) >= 30.0
