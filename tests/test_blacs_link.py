"""The rastering GUI's BLACS link (blacs_link.py, RaX's protocol), end to end in memory.

BLACS's side sends RaX's request envelopes (zmq_v2.encode_request) over InMemoryTransport to the
GUI's real link (RaxLink on a real SystemController, its motor, request and publisher threads
running). Only the two Z912 KCubes are in-memory stand-ins (tests/rig.py): no Kinesis, no socket,
no camera.

    QT_QPA_PLATFORM=offscreen ZMQ_V2_REQUIRED=1 py -3.13 -m pytest -q tests/test_blacs_link.py
"""
from __future__ import annotations

import threading
import time

import pytest

import blacs_link
import raster_controller as rc
from rig import PATH, Rig, wait_until


@pytest.fixture
def gui(tmp_path):
    g = Rig(tmp_path, blacs=True)
    g.plan()
    yield g
    g.close()


def _ok(reply):
    assert reply["status"] == "SUCCESS", reply
    return reply


def _err(reply):
    assert reply["status"] != "SUCCESS", reply
    return reply["status"], reply["error"]["code"], reply["error"]["message"]


def test_a_move_outside_the_travel_is_done_and_the_reply_carries_the_warning(gui):
    """Harm named: a sequence's x, y outside the 12 mm travel is the sequence's call (the KCube's
    limits are the only stop), but nobody may miss it: the Z912 may hit its end stop and the spot
    leaves the target. The move is done and answers SUCCESS with a warning field (point, motor
    position, travel), red on the GUI's log; a point inside moves both axes, X then Y, with no
    warning, and CHECK_VALUE reads it back."""
    reply = _ok(gui.blacs.program("laser_raster_xy", [1300.0, 200.0]))                  # 13 mm
    assert reply["warning"] == ("BLACS move to (1300.0, 200.0) px: outside the travel (0.0, 12.0, 0.0, 12.0) mm, "
                                "target (1300.0, 200.0) px -> motor (13.0000, 2.0000) mm: sent to the KCube; "
                                "its own limits are the only stop")
    assert gui.calls[-2:] == [("X", "move", 13.0), ("Y", "move", 2.0)]
    assert reply["warning"] in gui.warnings("red_warning")
    assert "warning" not in _ok(gui.blacs.program("laser_raster_xy", [500.0, 600.0]))
    assert gui.calls[-2:] == [("X", "move", 5.0), ("Y", "move", 6.0)]
    assert gui.blacs.check("laser_raster_x_coord_monitor")["value"] == 500.0
    assert gui.blacs.check("laser_raster_y_coord")["value"] == 600.0


def test_one_axis_moves_only_that_axis_from_where_the_stage_is(gui):
    """Harm stopped: a sequence that sets only Raster_X sends the stage to a stale y (or to 0) and
    ablates a spot nobody chose. laser_raster_x_coord keeps y where the stage is."""
    _ok(gui.blacs.program("laser_raster_xy", [500.0, 600.0]))
    _ok(gui.blacs.program("laser_raster_x_coord", 300.0))
    assert gui.calls[-2:] == [("X", "move", 3.0), ("Y", "move", 6.0)]


def test_arm_takes_the_run_tabs_pattern_and_move_to_next_loops_with_wrapped(gui):
    """Harm stopped: BLACS arms something other than the pattern on the GUI's Run tab, runs past
    the path's end without anyone knowing the spots are hit a second time (lyse counts them as
    fresh), or the end of the path stops the queue: RaX's BLACS takes finished: true as "path used
    up, arm again" and restarts. Arm answers the pattern's length; each move_to_next answers the
    point it moved to; the step past the last point is point 0 again with wrapped: true, and no
    reply ever says finished."""
    armed = _ok(gui.blacs.program("arm_raster", 0))
    assert (armed["mode"], armed["armed"], armed["dropped"]) == ("step", 3, 0)
    answers = [_ok(gui.blacs.program("move_to_next", 1)) for _ in range(len(PATH) + 1)]
    assert [(a["point_index"], a["target_xy"], a["wrapped"]) for a in answers] == [
        (0, [100.0, 200.0], False), (1, [110.0, 200.0], False), (2, [120.0, 200.0], False),
        (0, [100.0, 200.0], True)]
    assert not any("finished" in a for a in answers)
    last = answers[-1]
    assert (last["path_len"], last["frame"]) == (3, "pixel")
    assert last["calibration_matrix"] == [[0.01, 0.0], [0.0, 0.01]]
    assert last["calibration_offset"] == [0.0, 0.0]
    assert gui.x.pos == pytest.approx(1.0)                       # 100 px -> 1.0 mm


def test_with_ping_pong_the_path_runs_back_and_path_len_counts_the_way_back(tmp_path):
    """Harm stopped: with ping-pong on, BLACS records point indices or a path length that do not
    match the spots the stage visits (the shot file says spot 1 where spot 2 was ablated). The
    armed path is the pattern then the pattern backwards; path_len counts both halves."""
    g = Rig(tmp_path, blacs=True)
    try:
        g.plan(pingpong=True)
        assert _ok(g.blacs.program("arm_raster", 0))["armed"] == 6
        xs = [_ok(g.blacs.program("move_to_next", 1))["target_xy"][0] for _ in range(7)]
        assert xs == [100.0, 110.0, 120.0, 120.0, 110.0, 100.0, 100.0]
    finally:
        g.close()


def test_move_to_next_moves_nothing_unless_the_raster_is_armed_for_blacs(gui):
    """Harm stopped: a shot's move_to_next moves the stage under an operator who holds the raster at
    the GUI, or with no raster at all (wrong spot ablated, an alignment lost); or it fails the
    shot, so a Stop at the GUI halts BLACS's queue. With nothing armed it answers in_place with
    where the stage is; held at the GUI (after disarm) it answers the point the raster is at;
    nothing moves in either case. Arm again gives the raster back to BLACS at the same point."""
    _ok(gui.blacs.program("laser_raster_xy", [500.0, 600.0]))
    moves = gui.moves()
    off = _ok(gui.blacs.program("move_to_next", 1))
    assert (off["in_place"], off["target_xy"], off["frame"]) == (True, [500.0, 600.0], "pixel")
    _ok(gui.blacs.program("arm_raster", 0))
    _ok(gui.blacs.program("move_to_next", 1))
    moves = gui.moves()
    assert _ok(gui.blacs.program("disarm_raster", 1))["disarmed"] is True
    assert gui.ctl.raster_snapshot().state == "local"
    held = _ok(gui.blacs.program("move_to_next", 1))
    assert (held["point_index"], held["target_xy"], held["path_len"]) == (0, [100.0, 200.0], 3)
    assert "in_place" not in held and "wrapped" not in held
    assert gui.moves() == moves
    _ok(gui.blacs.program("arm_raster", 0))
    assert _ok(gui.blacs.program("move_to_next", 1))["point_index"] == 1


def _make_raster(gui, state):
    """Put the raster in `state` the way people do: BLACS arms it ("armed"), BLACS arms then
    disarms it ("local"), the operator starts a continuous run ("running", slow stage)."""
    if state in ("armed", "local"):
        _ok(gui.blacs.program("arm_raster", 0))
    if state == "local":
        _ok(gui.blacs.program("disarm_raster", 1))
    if state == "running":
        gui.x.gate = threading.Event()          # the run's first point is still moving
        gui.plan(continuous=True)
        assert gui.ctl.arm().ok


@pytest.mark.parametrize("state, kind", [("armed", "red_warning"), ("local", None), ("running", "warning")])
def test_a_move_while_a_raster_is_active_is_done_and_says_what_it_costs(gui, state, kind):
    """Harm named: a shot's x, y (Raster_X / Raster_Y in the sequence) while BLACS has the raster
    armed puts the shot off the recorded raster point; that is the sequence's call, but the
    operator must see it (red) and BLACS's record must say so (the reply's warning). During a
    continuous run the run's next point moves the stage back (plain warning); held at the GUI
    nothing steps the stage (no warning). The move is done in every state."""
    _make_raster(gui, state)
    assert gui.ctl.raster_snapshot().state == state
    if state == "running":
        gui.x.gate.set()
    reply = _ok(gui.blacs.program("laser_raster_xy", [500.0, 600.0]))
    assert 5.0 in gui.moves("X") and 6.0 in gui.moves("Y")
    if kind is None:
        assert "warning" not in reply
    else:
        assert reply["warning"].startswith("BLACS move to (500.0, 600.0) px: done ")
        assert reply["warning"] in gui.warnings(kind)


def test_a_continuous_run_at_the_gui_is_not_taken_over_by_blacs(gui):
    """Harm stopped: BLACS's arm or disarm ends or converts a continuous raster the operator
    started at the GUI (the sweep stops half way, or two drivers move the stage). Both answer
    raster_in_continuous_mode (RaX's code) with the GUI's words; the run stays running."""
    _make_raster(gui, "running")
    for name, value in (("arm_raster", 0), ("disarm_raster", 1)):
        status, code, words = _err(gui.blacs.program(name, value))
        assert (code, words) == ("raster_in_continuous_mode", "a continuous raster runs from the GUI; stop it there")
    assert gui.ctl.raster_snapshot().state == "running"


def test_arm_without_a_pattern_or_a_calibration_is_refused_and_arms_nothing(tmp_path):
    """Harm stopped: BLACS arms a raster the operator never set up, or one whose pixel points
    would be taken as motor millimetres (no calibration): the queue then steps the stage over
    spots nobody chose. Both arms answer ERROR with words saying what is missing; the raster
    stays off."""
    g = Rig(tmp_path, calibrated=False, blacs=True)
    try:
        assert _err(g.blacs.program("arm_raster", 0))[1:] == (
            "no_raster_configured", "no raster pattern is set up in the GUI")
        g.plan()
        assert _err(g.blacs.program("arm_raster", 0))[1] == "not_calibrated"
        assert g.ctl.raster_snapshot().state == "off"
    finally:
        g.close()


def test_a_move_without_a_calibration_or_with_no_numbers_moves_nothing(tmp_path):
    """Harm stopped: BLACS's pixel coordinates are taken as motor millimetres by a GUI with no
    calibration (the spot lands elsewhere on the target, inside the travel, so nothing stops it),
    or a value that is not a pair of numbers reaches the motors. Without a calibration a move
    answers not_calibrated; a bad value answers invalid_value; CHECK_VALUE has no px position."""
    g = Rig(tmp_path, calibrated=False, blacs=True)
    try:
        assert _err(g.blacs.program("laser_raster_xy", [5.0, 5.0]))[1] == "not_calibrated"
        assert _err(g.blacs.program("laser_raster_x_coord", 5.0))[1] == "not_calibrated"
        for bad in ([1.0], "1, 2", [1.0, float("nan")]):
            assert _err(g.blacs.program("laser_raster_xy", bad))[1] == "invalid_value"
        assert g.moves() == []
        assert _err(g.blacs.check("laser_raster_x_coord_monitor"))[:2] == (
            "UNKNOWN_CONNECTION", "position_not_initialized")
        assert _err(g.blacs.program("laser_power", 1))[:2] == ("UNKNOWN_CONNECTION", "unknown_connection")
    finally:
        g.close()


@pytest.mark.parametrize("what", ["move", "move_to_next"])
def test_a_move_that_takes_too_long_says_it_is_still_queued_and_will_run(tmp_path, what):
    """Harm stopped: a move or move_to_next the stage does not finish within the time limit
    reaches BLACS as "failed", so people think the stage stayed where it was, but the queued move
    runs a moment later (the spot moves under the next shot); after a late step nobody knows that
    point got no shot. The answer is TIMEOUT command_timeout, says the move is still queued and
    will run (for a step: that point gets no shot), and the stage does move once free."""
    g = Rig(tmp_path, blacs=True)
    try:
        g.plan()
        g.x.gate = threading.Event()                       # the stage is busy
        if what == "move":
            reply = g.blacs.program("laser_raster_xy", [500.0, 600.0], timeout_sec=0.2)
            words = ("the stage did not finish the move to (500, 600) px within 0.2 s; the move is still "
                     "queued and will run")
            want = 5.0
        else:
            _ok(g.blacs.program("arm_raster", 0))
            reply = g.blacs.program("move_to_next", 1, timeout_sec=0.2)
            words = ("the stage did not reach the next point within 0.2 s; the move is still queued and "
                     "will run, and that point gets no shot (the next shot's next point steps past it)")
            want = 1.0                                     # PATH[0]: 100 px -> 1.0 mm
        assert _err(reply) == ("TIMEOUT", "command_timeout", words)
        g.x.gate.set()                                     # the stage is free again
        wait_until(lambda: g.moves("X") == [want])
    finally:
        g.close()


def test_shots_per_step_is_held_by_the_gui_and_reported(gui):
    """Harm stopped: shots per step set in the BLACS tab is not what the GUI shows (the operator
    reads one number, BLACS steps by another), or a zero count reaches the GUI. A set is stored,
    answered and reaches the window; 0 answers invalid_value."""
    assert _ok(gui.blacs.program("shots_per_step", 3))["shots_per_step"] == 3
    assert gui.snaps[-1].shots_per_step == 3
    assert _err(gui.blacs.program("shots_per_step", 0))[1] == "invalid_value"
    assert gui.ctl.raster_snapshot().shots_per_step == 3


def test_blacs_xy_in_a_continuous_runs_delay_is_done_at_once(gui):
    """Harm stopped: BLACS's x, y during a continuous run with a long delay between points waited
    out the delay (10 s or more: BLACS heard "still queued and will run", and the move ran after
    the operator pressed Stop: the spot jumped off the path). It is done at once, between the
    run's points, with the warning in the reply; nothing more moves after Stop."""
    gui.plan(continuous=True, delay_s=5.0)
    assert gui.ctl.arm().ok
    wait_until(lambda: gui.moves("X") == [1.0])
    t0 = time.monotonic()
    assert "continuous run" in _ok(gui.blacs.program("laser_raster_xy", [500.0, 600.0]))["warning"]
    assert time.monotonic() - t0 < 1.0
    gui.ctl.stop_raster()
    gui.idle()
    assert gui.moves() == [1.0, 2.0, 5.0, 6.0]


def test_blacs_hears_why_for_a_motor_failure_and_for_a_dropped_point(gui):
    """Harm stopped: a move that did not happen reaches BLACS without the reason (a failing KCube,
    a point dropped by the GUI's Stop), so its tab cannot tell them from a fault in the link. A
    move answers motor_move_failed and a step raster_step_failed (RaX's codes), each with the
    GUI's words saying what happened, and nothing moves."""
    gui.x.fail = "KCube X lost"
    status, code, words = _err(gui.blacs.program("laser_raster_xy", [500.0, 600.0]))
    assert code == "motor_move_failed" and "failed: KCube X lost" in words
    gui.x.fail = None
    _ok(gui.blacs.program("arm_raster", 0))
    hold = threading.Event()                       # the motor thread is busy with another job
    gui.ctl._stage.submit(rc.Command("test: hold", "read", quiet=True, then=lambda _: hold.wait(5)))
    answer = {}
    t = threading.Thread(target=lambda: answer.update(gui.blacs.program("move_to_next", 1)))
    t.start()
    wait_until(lambda: gui.ctl.raster_snapshot().point_index == 0)   # BLACS's point is queued
    gui.ctl.stop_raster()                                            # the GUI's Stop
    hold.set()
    t.join(5)
    status, code, words = _err(answer)
    assert code == "raster_step_failed" and "dropped: the raster was stopped" in words
    assert gui.moves() == []


def test_the_live_state_has_rax_topics_and_words(tmp_path):
    """Harm stopped: BLACS's SUB socket gets topics or words its tab and shot file do not know, so
    the monitor values in the shot file are empty and the tab cannot show who drives the raster.
    Every call publishes the two positions [px]; every 4th call (the first included) also
    heartbeat, raster_mode, raster_owner, calibration_status and raster_progress, with the state
    words mapped as upstream rastering does."""
    g = Rig(tmp_path)
    try:
        g.plan()
        frames = []
        link = blacs_link.RaxLink(g.ctl, transport=None, publish=frames.append)
        _ok_move = g.ctl.move_px(500.0, 600.0).result(2)
        assert _ok_move.ok
        link.publish_once()
        assert frames == ["laser_raster_x_coord_monitor 500.0", "laser_raster_y_coord_monitor 600.0",
                          "heartbeat", "raster_mode idle", "raster_owner none", "calibration_status calibrated",
                          "raster_progress 0/0"]
        frames.clear()
        for _ in range(3):
            link.publish_once()
        assert frames == ["laser_raster_x_coord_monitor 500.0", "laser_raster_y_coord_monitor 600.0"] * 3
        expected = {"local": ("manual", "local"), "armed": ("step", "remote"), "running": ("continuous", "local")}
        for state, (mode, owner) in expected.items():
            g.ctl.stop_raster()
            g.plan(continuous=state == "running", delay_s=5.0)
            assert g.ctl.arm(for_blacs=state == "armed").ok
            if state == "armed":
                g.ctl.blacs_next_point()[-1].result(2)
            frames.clear()
            for _ in range(4):
                link.publish_once()
            slow = [f for f in frames if not f.startswith("laser_raster")]
            progress = "1/3" if state in ("armed", "running") else "0/3"
            assert slow == ["heartbeat", f"raster_mode {mode}", f"raster_owner {owner}",
                            "calibration_status calibrated", f"raster_progress {progress}"], state
    finally:
        g.close()


def test_the_heartbeat_keeps_coming_while_a_move_is_waited_for(tmp_path, monkeypatch):
    """Harm stopped: while BLACS's request waits up to 10 s for a move, the GUI stops publishing
    (upstream publishes from the request thread), so BLACS's tab says "no heartbeat" and the shot's
    monitor values are stale. The publisher has its own thread: heartbeats arrive during the wait."""
    monkeypatch.setattr(blacs_link, "PUB_PERIOD_S", 0.01)
    g = Rig(tmp_path, blacs=True)
    try:
        g.plan()
        g.x.gate = threading.Event()                 # the move hangs until released
        answer = {}
        t = threading.Thread(target=lambda: answer.update(g.blacs.program("laser_raster_xy", [500.0, 600.0])))
        t.start()
        assert g.x.moving.wait(2)
        n = g.published.count("heartbeat")
        wait_until(lambda: g.published.count("heartbeat") >= n + 3)
        assert t.is_alive()                          # still waiting for the move
        g.x.gate.set()
        t.join(5)
        assert answer["status"] == "SUCCESS"
    finally:
        g.close()
