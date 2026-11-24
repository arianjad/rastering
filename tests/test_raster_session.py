"""The raster session (raster_session.py): one state word, the armed path and its cursor; and the
continuous run on the motor thread, through the controller (tests/rig.py)."""
from __future__ import annotations

import threading
import time

import pytest
from raster_session import Refused

from rig import LINE, Rig, wait_until
from raster_session import BLACS, GUI, RUN, RasterPlan, RasterSession

PTS = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]


def _armed(state="local", pingpong=False):
    s = RasterSession()
    plan = RasterPlan(LINE, pingpong=pingpong)
    s.pending = plan
    s.arm(plan, PTS, dropped=0, state=state)
    return s


def test_the_cursor_walks_the_ping_pong_path_and_says_when_it_wraps():
    """Harm stopped: the shot file's point_index / path_len disagree with the spot the stage
    visits, or the step from the last point back to point 0 is not flagged (lyse counts the spots
    of the second pass as new). With ping-pong the path is the pattern then the pattern backwards;
    the step past its end is point 0 again with wrapped True, and only that step."""
    s = _armed(pingpong=True)
    snap = s.snapshot()
    assert (len(snap.path), snap.pattern_len, snap.point_index) == (6, 3, -1)
    walk = [s.next_point(GUI) for _ in range(7)]
    assert [(i, p[0], w) for i, p, w in walk] == [
        (0, 0.0, False), (1, 1.0, False), (2, 2.0, False), (3, 2.0, False), (4, 1.0, False),
        (5, 0.0, False), (0, 0.0, True)]


@pytest.mark.parametrize("state, by, code", [
    ("off", GUI, "not_armed"), ("armed", GUI, "blacs_steps"), ("running", GUI, "raster_running"),
    ("off", BLACS, "not_armed"), ("local", BLACS, "not_armed"), ("running", BLACS, "not_armed"),
    ("local", RUN, "not_running"),
])
def test_only_the_one_who_holds_the_raster_may_step_it(state, by, code):
    """Harm stopped: the GUI's Step advances a raster BLACS is counting shots on (BLACS's record
    is off by one from then on), BLACS steps a raster the operator holds, or a stale continuous
    run steps a raster that was handed over. A step by anyone but the holder is refused, with
    words, and the cursor does not move."""
    s = RasterSession() if state == "off" else _armed(state)
    with pytest.raises(Refused) as e:
        s.next_point(by)
    assert e.value.code == code and e.value.words
    assert s.snapshot().point_index == -1


def test_go_to_point_takes_the_raster_back_and_the_next_step_continues_after_it():
    """Harm stopped: after a go-to, the next step jumps back to where the run was (a spot is hit
    twice, another skipped), or BLACS keeps stepping a raster the operator has just moved by hand.
    Go to point k selects k, takes the raster back from BLACS, and the next step is k + 1."""
    s = _armed("armed")
    assert s.go_to(1) == (1, (1.0, 0.0), True)
    snap = s.snapshot()
    assert (snap.state, snap.point_index, snap.selected_index) == ("local", 1, 1)
    assert s.next_point(GUI)[0] == 2
    assert s.go_to(99)[0] == 2                      # clamped to the last point


def test_handing_a_continuous_run_to_blacs_keeps_its_place():
    """Harm stopped: handing a running raster to BLACS restarts it at point 0 (spots hit twice)
    or leaves the continuous run stepping too (two drivers). The run becomes BLACS's step mode at
    the same cursor; taking it back keeps the cursor as well."""
    s = _armed("running")
    s.next_point(RUN)
    s.next_point(RUN)
    assert s.hand_to_blacs() is True
    assert (s.snapshot().state, s.next_point(BLACS)[0]) == ("armed", 2)
    assert s.take_back(GUI) is True
    assert (s.snapshot().state, s.snapshot().point_index) == ("local", 2)


def test_path_edited_tells_the_run_tab_from_the_armed_raster():
    """Harm stopped: the operator edits the pattern while a raster is armed and believes the
    stage now runs the new one. Path edited is on exactly while armed with a different pending
    plan; Stop ends the raster and clears its path and owner."""
    s = _armed()
    assert not s.snapshot().path_edited
    s.pending = RasterPlan(LINE, pingpong=True)
    assert s.snapshot().path_edited
    run = s.run
    assert s.stop() is True
    snap = s.snapshot()
    assert (snap.state, snap.path, snap.path_edited, snap.armed_plan) == ("off", (), False, None)
    s.arm(s.pending, PTS, dropped=0, state="local")
    assert s.run == run + 1


@pytest.fixture
def gui(tmp_path):
    g = Rig(tmp_path)
    yield g
    g.close()


def test_a_continuous_run_loops_on_the_motor_thread_until_stop(gui):
    """Harm stopped: a continuous run stops at the end of the path (an unattended run idles), or
    keeps moving the stage after Stop. Started from the window it visits the path over and over,
    ping-pong included, and after Stop nothing more moves."""
    gui.plan(continuous=True, pingpong=True)
    assert gui.ctl.arm().ok
    wait_until(lambda: len(gui.moves("X")) >= 13)
    gui.ctl.stop_raster()
    gui.idle()
    xs = gui.moves("X")
    assert xs[:13] == [1.0, 1.1, 1.2, 1.2, 1.1, 1.0] * 2 + [1.0]
    time.sleep(0.05)
    assert gui.moves("X") == xs
    assert ("info", "Raster path done; started again at point 0.") in gui.messages


def test_stop_cuts_a_continuous_runs_wait_short(gui):
    """Harm stopped: with a long delay between points, Stop leaves the stage busy for the rest of
    the delay (the operator's next move waits) or one more point moves after Stop. Stop ends the
    wait at once and no further point moves."""
    gui.plan(continuous=True, delay_s=5.0)
    gui.ctl.arm()
    wait_until(lambda: gui.moves("X") == [1.0])
    t0 = time.monotonic()
    gui.ctl.stop_raster()
    assert gui.ctl.move_mm(3.0, 3.0).result(2).ok
    assert time.monotonic() - t0 < 1.0
    assert gui.moves("X") == [1.0, 3.0]


def test_handing_a_running_raster_to_blacs_stops_the_run_from_stepping(gui):
    """Harm stopped: after the operator hands a running raster to BLACS, the continuous run keeps
    stepping it too (two drivers; BLACS's shot records no longer match the spots). The run stops
    by itself; BLACS's next point continues from where it stood."""
    gui.x.gate = threading.Event()
    gui.plan(continuous=True)
    gui.ctl.arm()
    assert gui.ctl.hand_to_blacs().ok
    gui.x.gate.set()
    gui.idle()
    assert gui.moves("X") == [1.0]
    i, _, _, _, future = gui.ctl.blacs_next_point()
    assert i == 1 and future.result(2).ok
    assert gui.moves("X") == [1.0, 1.1]


def test_a_motor_failure_halts_a_continuous_run_with_its_words(gui):
    """Harm stopped: a continuous run keeps hammering a stage that fails every move, and the
    operator never learns why it stopped. The first failure ends the run and says what failed."""
    gui.x.fail = "KCube X lost"
    gui.plan(continuous=True)
    gui.ctl.arm()
    wait_until(lambda: any(k == "error" and t.startswith("Raster halted: Raster point 0 of 3 failed: KCube X lost")
                           for k, t in gui.messages))
    assert gui.ctl.raster_snapshot().state == "off"
    assert gui.moves() == []


def test_re_arming_keeps_the_raster_held_or_running_as_it_was(gui):
    """Harm stopped: from HELD AT GUI with Continuous ticked, "Re-arm with edited pattern" started
    a continuous run at point 0, so the stage moved by itself (under the laser) when the operator
    only meant to swap the pattern. A re-arm keeps the state: held stays held, nothing moves; a
    run keeps running."""
    gui.plan()
    assert gui.ctl.arm().ok
    gui.plan(continuous=True, pingpong=True)                    # the edited pattern
    assert gui.ctl.arm().ok
    gui.idle()
    time.sleep(0.03)
    assert (gui.ctl.raster_snapshot().state, gui.moves()) == ("local", [])

    gui.ctl.stop_raster()
    gui.plan(continuous=True, delay_s=0.01)
    assert gui.ctl.arm().ok
    gui.plan(continuous=False, pingpong=True)
    assert gui.ctl.arm().ok
    assert gui.ctl.raster_snapshot().state == "running"


def test_pause_holds_a_run_and_continue_runs_on_from_the_point_picked(gui):
    """Harm stopped: a continuous run could not be paused (Stop loses the place; the detour through
    Hand to BLACS made the log blame BLACS) nor resumed from a chosen point (Re-arm starts again at
    point 0: spots ablated twice). Pause holds the run at its point; Go to selected point N, then
    Continue, runs on from N (moved to and waited at again), then N + 1."""
    gui.plan(continuous=True, delay_s=0.02)
    assert gui.ctl.arm().ok
    wait_until(lambda: len(gui.moves("X")) >= 2)
    assert gui.ctl.pause_raster().ok
    gui.idle()
    n = len(gui.moves("X"))
    time.sleep(0.06)
    assert gui.ctl.raster_snapshot().state == "local" and len(gui.moves("X")) == n
    assert gui.ctl.go_to_point(2).ok
    assert gui.ctl.continue_raster().ok
    wait_until(lambda: len(gui.moves("X")) >= n + 3)
    gui.ctl.stop_raster()
    assert gui.moves("X")[n:n + 3] == [1.2, 1.2, 1.0]            # go to 2; run on from 2; then 0


def test_handing_to_blacs_after_a_go_to_gives_blacs_that_point_first(gui):
    """Harm stopped: the operator took the raster back, went to point 2 to shoot it again and
    handed it to BLACS; BLACS's next shot went to point 3, so point 2 never got its shot. After a
    go to N, BLACS's next point is N (the stage is there already), and the log says so."""
    gui.plan()
    assert gui.ctl.arm(for_blacs=True).ok
    assert gui.ctl.blacs_next_point()[0] == 0
    gui.ctl.take_back()
    assert gui.ctl.go_to_point(2).ok
    assert gui.ctl.hand_to_blacs().ok
    assert ("info", "BLACS steps the raster: its next shot goes to point 2 of 3.") in gui.messages
    i, _, _, wrapped, future = gui.ctl.blacs_next_point()
    assert (i, wrapped, future.result(2).ok) == (2, False, True)
    assert gui.ctl.blacs_next_point()[:4:3] == (0, True)          # then on along the path


def test_a_continuous_runs_delay_does_not_hold_up_the_stage_queue(gui):
    """Harm stopped: in a continuous run's delay between points the motor thread slept, so every
    other request waited out the delay: the window's moves, and BLACS's x, y (with a delay of
    10 s or more BLACS heard "busy, still queued", and the move ran after Stop). In the delay a
    move is answered at once (refused: the run holds the stage); the next point still comes
    after the delay."""
    gui.plan(continuous=True, delay_s=0.4)
    assert gui.ctl.arm().ok
    wait_until(lambda: gui.moves("X") == [1.0])
    t0 = time.monotonic()
    res = gui.ctl.move_mm(5.0, 5.0).result(1)
    assert res.code == "raster_running" and time.monotonic() - t0 < 0.2
    wait_until(lambda: gui.moves("X") == [1.0, 1.1])
    assert time.monotonic() - t0 > 0.25
    gui.ctl.stop_raster()
