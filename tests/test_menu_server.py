"""The rastering GUI as BLACS's menu server, end to end in memory.

BLACS's real client (bbx_zmq Client) talks over the in-memory link to the GUI's real menu server
(RasterMenuLink on a real SystemController, its motor thread and menu thread running). Only the
two Z912 KCubes are in-memory stand-ins (tests/rig.py): no Kinesis, no socket, no camera.

    QT_QPA_PLATFORM=offscreen ZMQ_V2_REQUIRED=1 py -3.13 -m pytest -q tests/test_menu_server.py
"""
from __future__ import annotations

import dataclasses
import threading
import time

import pytest
from bbx_zmq import Refused

import raster_controller as rc
from rig import MENU, PATH, Rig, wait_until

XY_WORDS = ("so a move to x, y would leave its path: stop the raster with the GUI's Stop button first, "
            "or take Raster_X / Raster_Y out of the sequence")


@pytest.fixture
def gui(tmp_path):
    g = Rig(tmp_path, blacs=True)
    g.plan()
    yield g
    g.close()


def test_a_move_outside_the_travel_is_refused_with_the_guis_words(gui):
    """Harm stopped: BLACS sends the stage past its 12 mm travel (the Z912 runs into its end stop,
    the spot leaves the target), or the refusal reaches BLACS as a bare "failed" and nobody can
    tell which point or why. A point inside the menu's range whose motor position is outside the
    travel is refused with the GUI's words (point, motor position, travel) and nothing moves; a
    point inside the travel moves both axes, X then Y."""
    with pytest.raises(Refused) as e:
        gui.client.set("xy", [1300.0, 200.0])                    # 13 mm: outside 0..12 mm
    assert e.value.code == "move_refused"
    assert e.value.words == ("Rejected: motor out of bounds -- target (1300.0, 200.0) -> "
                             "motor (13.0000, 2.0000) mm, travel is (0.0, 12.0, 0.0, 12.0)")
    assert gui.moves() == []
    assert gui.client.set("xy", [500.0, 600.0]) == [500.0, 600.0]
    assert gui.calls[-2:] == [("X", "move", 5.0), ("Y", "move", 6.0)]
    assert gui.client.snapshot()["xy"] == [500.0, 600.0]


def test_arm_takes_the_run_tabs_pattern_and_next_point_loops_with_finished(gui):
    """Harm stopped: BLACS arms something other than the pattern on the GUI's Run tab, runs past
    the path's end without anyone knowing the spots are hit a second time (lyse counts them as
    fresh), or the end of the path stops the queue. Arm answers the pattern's length; each next
    point answers the point it moved to; the step past the last point is point 0 again, at
    point 0's place, with finished = True."""
    assert gui.client.press("arm") == {"path_len": 3, "dropped": 0}
    answers = [gui.client.press("next_point") for _ in range(len(PATH) + 1)]
    assert [(a["point_index"], a["x"], a["y"], a["finished"]) for a in answers] == [
        (0, 100.0, 200.0, False), (1, 110.0, 200.0, False), (2, 120.0, 200.0, False),
        (0, 100.0, 200.0, True)]
    last = answers[-1]
    assert (last["path_len"], last["frame"]) == (3, "pixel")
    assert last["calibration_matrix"] == [[0.01, 0.0], [0.0, 0.01]]
    assert gui.x.pos == pytest.approx(1.0)                       # 100 px -> 1.0 mm
    snap = gui.client.snapshot()
    assert (snap["raster_state"], snap["point_index"], snap["path_len"]) == ("armed", 0, 3)
    assert snap["xy"] == [100.0, 200.0] and (snap["x"], snap["y"]) == (100.0, 200.0)


def test_with_ping_pong_the_path_runs_back_and_path_len_counts_the_way_back(tmp_path):
    """Harm stopped: with ping-pong on, BLACS records point indices or a path length that do not
    match the spots the stage visits (the shot file says spot 1 where spot 2 was ablated). The
    armed path is the pattern then the pattern backwards; path_len counts both halves."""
    g = Rig(tmp_path, blacs=True)
    try:
        g.plan(pingpong=True)
        assert g.client.press("arm") == {"path_len": 6, "dropped": 0}
        xs = [g.client.press("next_point")["x"] for _ in range(7)]
        assert xs == [100.0, 110.0, 120.0, 120.0, 110.0, 100.0, 100.0]
    finally:
        g.close()


def test_next_point_moves_nothing_unless_the_raster_is_armed_for_blacs(gui):
    """Harm stopped: a next point (a BLACS click, a shot) moves the stage under an operator who
    holds the raster at the GUI, or with no raster at all (wrong spot ablated, an alignment lost).
    With no raster, and after disarm, next point is refused with the raster state and nothing
    moves; arm again hands the raster back to BLACS at the same point."""
    with pytest.raises(Refused, match=r"\[not_armed\] no raster armed for BLACS \(raster state: off\)"):
        gui.client.press("next_point")
    gui.client.press("arm")
    gui.client.press("next_point")
    moves = gui.moves()
    assert gui.client.press("disarm") == {"was_armed": True}
    assert gui.client.snapshot()["raster_state"] == "local"
    with pytest.raises(Refused, match=r"raster state: local"):
        gui.client.press("next_point")
    assert gui.moves() == moves
    assert gui.client.press("arm") == {"path_len": 3, "dropped": 0}
    assert gui.client.press("next_point")["point_index"] == 1


def _make_raster(gui, state):
    """Put the raster in `state` the way people do: BLACS arms it ("armed"), BLACS arms then
    disarms it ("local"), the operator starts a continuous run ("running", slow stage)."""
    if state in ("armed", "local"):
        gui.client.press("arm")
    if state == "local":
        gui.client.press("disarm")
    if state == "running":
        gui.x.gate = threading.Event()          # the run's first point is still moving
        gui.plan(continuous=True)
        assert gui.ctl.arm().ok


@pytest.mark.parametrize("state, code, holder", [
    ("armed", "raster_armed", "the raster is armed for BLACS"),
    ("local", "raster_held", "the raster is held at the GUI"),
    ("running", "raster_running", "a continuous raster runs from the GUI"),
])
def test_a_move_while_any_raster_is_active_is_refused_and_says_what_to_do(gui, state, code, holder):
    """Harm stopped: a shot's x, y (Raster_X / Raster_Y in the sequence) or a tab move pulls the
    stage off a raster: the one BLACS steps, the one the operator holds, or a continuous run. The
    shot ablates a spot off the path and the shot file has no point for it. While any raster is
    active a move is refused, it does not move the stage, and the words say to stop the raster
    at the GUI or take the x, y out of the sequence; after the GUI's Stop the same move is done."""
    _make_raster(gui, state)
    assert gui.client.snapshot()["raster_state"] == state
    if state == "running":
        gui.x.gate.set()
    with pytest.raises(Refused) as e:
        gui.client.set("xy", [500.0, 600.0])
    assert (e.value.code, e.value.words) == (code, f"{holder}, {XY_WORDS}")
    assert 5.0 not in gui.moves("X") and 6.0 not in gui.moves("Y")
    gui.ctl.stop_raster()                                    # the GUI's Stop button
    assert gui.client.snapshot()["raster_state"] == "off"
    assert gui.client.set("xy", [500.0, 600.0]) == [500.0, 600.0]


def test_a_continuous_run_at_the_gui_is_not_taken_over_by_blacs(gui):
    """Harm stopped: BLACS's arm or disarm ends or converts a continuous raster the operator
    started at the GUI (the sweep stops half way, or two drivers move the stage). Both are refused
    with the GUI's words; the run stays running."""
    _make_raster(gui, "running")
    for button in ("arm", "disarm"):
        with pytest.raises(Refused, match=r"\[raster_running\] a continuous raster runs from the GUI"):
            gui.client.press(button)
    assert gui.client.snapshot()["raster_state"] == "running"


def test_arm_without_a_pattern_or_a_calibration_is_refused_and_arms_nothing(tmp_path):
    """Harm stopped: BLACS arms a raster the operator never set up, or one whose pixel points
    would be taken as motor millimetres (no calibration): the queue then steps the stage over
    spots nobody chose. Both arms are refused with words saying what is missing; the raster
    stays off."""
    g = Rig(tmp_path, calibrated=False, blacs=True)
    try:
        with pytest.raises(Refused, match=r"\[no_raster_configured\] no raster pattern is set up in the GUI"):
            g.client.press("arm")
        g.plan()
        with pytest.raises(Refused, match=r"\[not_calibrated\]"):
            g.client.press("arm")
        assert g.client.snapshot()["raster_state"] == "off"
    finally:
        g.close()


def test_a_move_without_a_calibration_or_out_of_the_menus_range_moves_nothing(tmp_path):
    """Harm stopped: BLACS's pixel coordinates are taken as motor millimetres by a GUI with no
    calibration (the spot lands elsewhere on the target, inside the travel, so nothing stops it),
    or a point off the camera frame reaches the motors. Without a calibration a move is refused;
    a point outside the menu's 0..4096 px is refused before the GUI's handler runs."""
    g = Rig(tmp_path, calibrated=False, blacs=True)
    try:
        with pytest.raises(Refused, match=r"\[not_calibrated\] no calibration set"):
            g.client.set("xy", [5.0, 5.0])
        with pytest.raises(Refused, match=r"\[out_of_range\] xy = \[5000.0, 5.0\] is outside"):
            g.client.set("xy", [5000.0, 5.0])
        assert g.moves() == []
        assert g.client.snapshot()["xy"] is None
    finally:
        g.close()


@pytest.mark.parametrize("what", ["move", "next_point"])
def test_a_move_that_takes_too_long_says_it_is_still_queued_and_will_run(tmp_path, monkeypatch, what):
    """Harm stopped: a move or next point the stage does not finish within the menu's time limit
    reaches BLACS as "refused", so people think the stage stayed where it was, but the queued move
    runs a moment later (the spot moves under the next shot); after a late next point nobody knows
    that point got no shot. The answer has its own code (gui_busy), says the move is still queued
    and will run (for next point: that point gets no shot), and the stage does move once free."""
    short = dataclasses.replace(
        MENU, values=tuple(dataclasses.replace(v, set_s=0.2) if v.name == "xy" else v for v in MENU.values),
        buttons=tuple(dataclasses.replace(b, press_s=0.2) if b.name == "next_point" else b
                      for b in MENU.buttons))
    import raster_controller as rc
    monkeypatch.setattr(rc, "MENU", short)                # 0.2 s limits, so the test is quick
    g = Rig(tmp_path, blacs=True, menu=short)
    try:
        g.plan()
        g.x.gate = threading.Event()                       # the stage is busy
        if what == "move":
            with pytest.raises(Refused) as e:
                g.client.set("xy", [500.0, 600.0])
            words = "the stage did not finish the move to x, y within 0.2 s; the move is still queued and will run"
            want = 5.0
        else:
            g.client.press("arm")
            with pytest.raises(Refused) as e:
                g.client.press("next_point")
            words = ("the stage did not reach the next point within 0.2 s; the move is still queued and "
                     "will run, and that point gets no shot (the next shot's next point steps past it)")
            want = 1.0                                     # PATH[0]: 100 px -> 1.0 mm
        assert (e.value.code, e.value.words) == ("gui_busy", words)
        g.x.gate.set()                                     # the stage is free again
        wait_until(lambda: g.moves("X") == [want])
    finally:
        g.close()


def test_shots_per_step_is_held_by_the_gui_and_reported(gui):
    """Harm stopped: shots per step set in the BLACS tab is not what the GUI shows (the operator
    reads one number, BLACS steps by another), or a non-whole or zero count reaches the GUI.
    A set is stored, comes back in the snapshot and reaches the window; 0 and 2.5 are refused."""
    assert gui.client.set("shots_per_step", 3) == 3
    assert gui.client.snapshot()["shots_per_step"] == 3
    assert gui.snaps[-1].shots_per_step == 3
    for bad in (0, 2.5):
        with pytest.raises(Refused, match=r"\[out_of_range\]"):
            gui.client.set("shots_per_step", bad)


def test_blacs_xy_in_a_continuous_runs_delay_is_refused_at_once_and_never_runs(gui):
    """Harm stopped: BLACS's x, y during a continuous run with a long delay between points waited
    out the delay (10 s or more: BLACS heard "busy, still queued and will run", and the move did
    run once the operator pressed Stop: the spot jumped off the path). It is refused at once
    with raster_running, and nothing moves after Stop."""
    gui.plan(continuous=True, delay_s=5.0)
    assert gui.ctl.arm().ok
    wait_until(lambda: gui.moves("X") == [1.0])
    t0 = time.monotonic()
    with pytest.raises(Refused) as e:
        gui.client.set("xy", [500.0, 600.0])
    assert e.value.code == "raster_running" and time.monotonic() - t0 < 1.0
    gui.ctl.stop_raster()
    gui.idle()
    assert gui.moves() == [1.0, 2.0]


def test_blacs_hears_move_refused_for_a_motor_failure_and_for_a_dropped_point(gui):
    """Harm stopped: BLACS got codes outside the ones its menu lists for a move that did not
    happen (motor_error for a failing KCube, raster_stopped for a point dropped by the GUI's
    Stop), so its tab cannot tell them from a fault in the link. Both answer move_refused, with
    the GUI's words saying what happened, and nothing moves."""
    gui.x.fail = "KCube X lost"
    with pytest.raises(Refused) as e:
        gui.client.set("xy", [500.0, 600.0])
    assert e.value.code == "move_refused" and "failed: KCube X lost" in e.value.words
    gui.x.fail = None
    gui.client.press("arm")
    hold = threading.Event()                       # the motor thread is busy with another job
    gui.ctl._stage.submit(rc.Command("test: hold", "read", quiet=True, then=lambda _: hold.wait(5)))
    answer = {}

    def press():
        try:
            answer["ok"] = gui.client.press("next_point")
        except Refused as r:
            answer["refused"] = r

    t = threading.Thread(target=press)
    t.start()
    wait_until(lambda: gui.ctl.raster_snapshot().point_index == 0)   # BLACS's point is queued
    gui.ctl.stop_raster()                                            # the GUI's Stop
    hold.set()
    t.join(5)
    assert answer["refused"].code == "move_refused"
    assert "dropped: the raster was stopped" in answer["refused"].words
    assert gui.moves() == []
