"""BBX-190: the rastering GUI as BLACS's menu server, end to end in memory.

The real BLACS client (bbx_zmq Client) talks over the in-memory link to the real menu server of
raster_controller.py (RasterMenuLink on a real SystemController, its motor thread running). Only
the stages are fake: FakeStage objects stand in for the two Z912 KCubes (no Kinesis, no motor,
no socket, no camera; ui.py is not imported). Each test docstring names the harm it stops.

The GUI panel's part of an arm (building the path from the Run tab) lives in ui.py, which opens
the camera; here a stand-in for ui._on_remote_arm_requested arms a fixed test path the way the
panel does (step mode, owner "remote", looping the path).

Run (camera-safe), with the beambox-daq src first on the path (its bbx_zmq has the menus):
    PYTHONPATH=<beambox-daq>\\src QT_QPA_PLATFORM=offscreen py -3.13 -m pytest -q tests/test_menu_server.py
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
menus = pytest.importorskip("bbx_zmq.menus.rastering", reason="needs beambox-daq src with the menus")
MENU = menus.MENU

TRAVEL = (0.0, 12.0, 0.0, 12.0)          # [mm] Z912 travel, config.py HardwareConfig.motor_bounds
MM_PER_PX = 0.01                         # test calibration: motor [mm] = 0.01 * target [px]
PATH = [(100.0, 200.0), (110.0, 200.0), (120.0, 200.0)]      # [px] test path


class FakeStage:
    """One Z912 axis: remembers where it was sent [mm]; records every move."""

    def __init__(self, pos=1.0):
        self.pos, self.moves = float(pos), []

    def get_position(self):
        return self.pos

    def move_to(self, pos, *, timeout_s=10.0):
        self.moves.append(float(pos))
        self.pos = float(pos)
        return self.pos


@pytest.fixture(scope="module")
def app():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class Gui:
    """A SystemController with fake stages, its menu link served on a thread over the in-memory
    link, and a real BLACS client on it."""

    def __init__(self, calibrated=True):
        import numpy as np

        import raster_controller as rc
        from bbx_zmq import Client, InMemoryTransport
        self.x, self.y = FakeStage(), FakeStage()
        self.ctl = rc.SystemController(self.x, self.y, motor_bounds=TRAVEL, telemetry_period_s=0)
        if calibrated:
            self.ctl.calibration = rc.AffineCalibration(M=np.eye(2) * MM_PER_PX, b=np.zeros(2))
        self.ctl.remote_arm_provider = self._panel_arm
        to_gui, from_gui = InMemoryTransport.pair()
        self.server = rc.RasterMenuLink(self.ctl).menu_server(from_gui)
        self.client = Client(MENU, lambda: to_gui, margin_s=0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _panel_arm(self, want_continuous, reply):
        """Stands in for ui._on_remote_arm_requested: arms the panel's path in step mode."""
        self.ctl.start_raster(iter(PATH), continuous=False, source="remote", repeat=True, pingpong=False)
        reply(True)

    def _serve(self):
        while not self._stop.is_set():
            self.server.serve_once(timeout_ms=20)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.ctl.shutdown()


@pytest.fixture
def gui(app):
    g = Gui()
    yield g
    g.close()


def test_a_move_outside_the_travel_is_refused_with_the_guis_words(gui):
    """Harm stopped: BLACS sends the stage past its 12 mm travel (the Z912 runs into its end
    stop, the mirror mount is strained, the spot leaves the target), or the refusal reaches BLACS
    as a bare "failed" and nobody can tell which point or why. A point inside the menu's range
    whose motor position is outside the travel is refused with the GUI's own words (the point,
    the motor position, the travel), and no stage moves; a point inside the travel moves both
    axes in one move."""
    from bbx_zmq import Refused
    gui.client.hello()
    with pytest.raises(Refused) as e:
        gui.client.set("xy", [1300.0, 200.0])                # 13 mm: outside 0..12 mm
    assert e.value.code == "move_refused"
    assert e.value.words == ("Rejected: motor out of bounds -- target (1300.0, 200.0) -> "
                             "motor (13.0000, 2.0000) mm, travel is (0.0, 12.0, 0.0, 12.0)")
    assert (gui.x.moves, gui.y.moves) == ([], [])
    assert gui.client.set("xy", [500.0, 600.0]) == [500.0, 600.0]
    assert (gui.x.moves, gui.y.moves) == ([5.0], [6.0])


def test_next_point_at_the_end_of_the_path_is_reported_as_finished(gui):
    """Harm stopped: BLACS runs past the end of the raster path without anyone knowing that the
    target spots are now hit a second time (lyse counts them as fresh spots), or the end of the
    path stops the queue. Each next point answers the point it moved to; the step past the last
    point says finished = True and is point 0 again (the GUI loops the path), at point 0's place."""
    gui.client.hello()
    gui.client.press("arm")
    answers = [gui.client.press("next_point") for _ in range(len(PATH) + 1)]
    assert [(a["point_index"], a["x"], a["y"], a["finished"]) for a in answers] == [
        (0, 100.0, 200.0, False), (1, 110.0, 200.0, False), (2, 120.0, 200.0, False),
        (0, 100.0, 200.0, True)]
    assert answers[-1]["path_len"] == len(PATH) and answers[-1]["frame"] == "pixel"
    assert gui.x.moves[-1] == pytest.approx(1.0)                 # 100 px -> 1.0 mm
    snap = gui.client.snapshot()
    assert (snap["raster_state"], snap["point_index"], snap["path_len"]) == ("armed", 0, 3)
    assert snap["xy"] == [100.0, 200.0] and (snap["x"], snap["y"]) == (100.0, 200.0)


def test_next_point_moves_nothing_unless_the_raster_is_armed_for_blacs(gui):
    """Harm stopped: a next point (a BLACS click, a shot) moves the stage under an operator who
    holds the raster at the GUI, or with no raster at all (wrong spot ablated, an alignment lost).
    With no raster, and after disarm (the raster handed back to the GUI), next point is refused
    with the raster state and no stage moves; arm again hands it back to BLACS at the same point."""
    from bbx_zmq import Refused
    gui.client.hello()
    with pytest.raises(Refused, match=r"\[not_armed\] no raster armed for BLACS \(raster state: off\)"):
        gui.client.press("next_point")
    gui.client.press("arm")
    gui.client.press("next_point")
    moves = list(gui.x.moves)
    assert gui.client.press("disarm") == {"was_armed": True}
    assert gui.client.snapshot()["raster_state"] == "local"
    with pytest.raises(Refused, match=r"raster state: local"):
        gui.client.press("next_point")
    assert gui.x.moves == moves
    gui.client.press("arm")
    assert gui.client.press("next_point")["point_index"] == 1


def test_a_continuous_run_at_the_gui_is_not_taken_over_by_blacs(gui):
    """Harm stopped: BLACS's arm or disarm ends or converts a continuous raster the operator
    started at the GUI (the sweep stops half way, or two drivers move the stage). Both are
    refused with the GUI's words; the run stays continuous."""
    from bbx_zmq import Refused
    gui.ctl.start_raster(iter(PATH), continuous=False, source="local", repeat=True, pingpong=False)
    gui.ctl._raster_continuous = True            # running (not enqueued: the test drives no sweep)
    gui.client.hello()
    for button in ("arm", "disarm"):
        with pytest.raises(Refused, match=r"\[raster_running\] a continuous raster runs from the GUI"):
            gui.client.press(button)
    assert gui.client.snapshot()["raster_state"] == "running"


def test_a_move_without_a_calibration_or_out_of_the_menus_range_moves_nothing(app):
    """Harm stopped: BLACS's pixel coordinates are taken as motor millimetres by a GUI with no
    calibration (the spot lands somewhere else on the target, inside the travel, so nothing
    stops it), or a point off the camera frame reaches the motors. Without a calibration a move
    is refused; a point outside the menu's 0..4096 px is refused before the GUI's handler runs."""
    from bbx_zmq import Refused
    g = Gui(calibrated=False)
    try:
        g.client.hello()
        with pytest.raises(Refused, match=r"\[not_calibrated\]"):
            g.client.set("xy", [5.0, 5.0])
        with pytest.raises(Refused, match=r"\[out_of_range\] xy = \[5000.0, 5.0\] is outside"):
            g.client.set("xy", [5000.0, 5.0])
        assert (g.x.moves, g.y.moves) == ([], [])
    finally:
        g.close()


def test_shots_per_step_is_held_by_the_gui_and_reported(gui):
    """Harm stopped: shots per step set in the BLACS tab is not what the GUI shows (the operator
    reads one number, BLACS steps by another), or a non-whole or zero count reaches the GUI.
    A set is stored and comes back in the snapshot; 0 and 2.5 are refused by the menu."""
    from bbx_zmq import Refused
    gui.client.hello()
    assert gui.client.set("shots_per_step", 3) == 3
    assert gui.client.snapshot()["shots_per_step"] == 3
    for bad in (0, 2.5):
        with pytest.raises(Refused, match=r"\[out_of_range\]"):
            gui.client.set("shots_per_step", bad)
