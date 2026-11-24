"""Motor start-up and Kinesis task completion, without Kinesis: hardware.py loads pythonnet only
when a KCube is made, and no test makes one."""
from __future__ import annotations

import dataclasses
import os
import sys
import threading
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import hardware  # noqa: E402
import raster_controller as rc  # noqa: E402


def test_a_move_ends_on_its_own_completion_callback_and_not_on_a_stale_one():
    """Harm stopped: a late callback from an earlier, timed-out move counts as the end of the
    current one (the next shot fires while the stage still moves), or a callback that comes before
    the wait starts is lost (every such move stalls for the full timeout and reports a failure).
    The wait ends on its own task's callback, early or late; another task's does not end it."""
    w = hardware.TaskWaiter()
    w.done(7)                                        # came before the wait started
    assert w.wait(7, 0.5)
    threading.Timer(0.02, w.done, args=(3,)).start()   # stale: an older task
    threading.Timer(0.08, w.done, args=(8,)).start()
    assert w.wait(8, 2.0)
    assert not w.wait(9, 0.05)                       # never called back: timed out


def test_a_simulated_run_never_loads_pythonnet(tmp_path):
    """Harm stopped: --simulate loads pythonnet and Kinesis (it fails on a PC without them, or
    takes the KCubes the lab's live GUI is using). A simulated controller runs moves with no
    pythonnet in the process."""
    cfg = dataclasses.replace(
        config.APP_CONFIG, hardware=dataclasses.replace(config.APP_CONFIG.hardware, simulate=True),
        network=dataclasses.replace(config.APP_CONFIG.network, blacs_link=False),
        paths=dataclasses.replace(config.APP_CONFIG.paths, calibration_path=str(tmp_path / "c.json")))
    ctl = rc.create_controller(cfg)
    try:
        assert ctl.move_mm(0.05, 0.0).result(5).ok
    finally:
        ctl.close()
    assert "clr" not in sys.modules


def test_a_kcube_that_fails_to_start_is_an_error_not_a_simulated_motor(monkeypatch, tmp_path):
    """Harm stopped: a disconnected KCube silently becomes a simulated motor; every move then
    "succeeds" while the real stage never moves; or the error shown is the clean-up's (X failing
    to disconnect), not why Y did not start. Making the controller fails with Y's error, and the
    axis that did start is disconnected again."""
    started = []

    class KCubeStub:
        def __init__(self, serial_no, name, hw):
            if name == "Y":
                raise RuntimeError("KCube Y (27007520) not found")
            started.append(self)
            self.disconnected = False

        def disconnect(self):
            self.disconnected = True
            raise RuntimeError("KCube X: USB gone")

    monkeypatch.setattr(hardware, "KCube", KCubeStub)
    cfg = dataclasses.replace(config.APP_CONFIG, network=dataclasses.replace(config.APP_CONFIG.network, blacs_link=False))
    with pytest.raises(RuntimeError, match="KCube Y"):
        rc.create_controller(cfg)
    assert [s.disconnected for s in started] == [True]


class _Decimal(float):
    """System.Decimal as Kinesis uses it: made from a float, read back with ToSingle."""
    @staticmethod
    def ToSingle(v):
        return float(v)


class _KinesisDevice:
    """A Kinesis KCubeDCServo: a move ends `move_s` later with the completion callback for its
    task id, or never (`hang`). Records the calls."""

    def __init__(self, *, move_s=0.05, hang=False):
        self.Position = _Decimal(1.0)
        self.move_s, self.hang = move_s, hang
        self.calls = []

    def MoveTo(self, pos, done):
        self.calls.append(("MoveTo", float(pos)))
        task = len(self.calls)
        if not self.hang:
            def finish():
                self.Position = _Decimal(float(pos))
                done(task)
            threading.Timer(self.move_s, finish).start()
        return task

    def StopImmediate(self):
        self.calls.append(("StopImmediate",))


def _kcube(device, move_timeout_s=1.0):
    """A KCube around a fake Kinesis device, made without __init__: no pythonnet, no DLL, no USB."""
    k = object.__new__(hardware.KCube)
    k.name, k.serial_no = "X", "0"
    k._hw = dataclasses.replace(config.HardwareConfig(), move_timeout_s=move_timeout_s)
    k._Decimal = _Decimal
    k._waiter = hardware.TaskWaiter()
    k._on_done = k._waiter.done
    k._device = device
    return k


def test_a_kcube_move_returns_when_the_stage_is_there_and_is_stopped_on_timeout():
    """Harm stopped: a move returns as soon as Kinesis took it (the next shot fires while the stage
    still moves, and the position read back is the old one), or a move that never reports done
    is left running when the GUI gives up on it (the stage drifts on under the next shot).
    move_to returns after the completion callback, at the new position; a move with no callback
    within the time limit is stopped with StopImmediate and reported."""
    dev = _KinesisDevice(move_s=0.05)
    assert _kcube(dev).move_to(3.0) == 3.0
    assert dev.calls == [("MoveTo", 3.0)]
    stuck = _KinesisDevice(hang=True)
    with pytest.raises(TimeoutError, match=r"motor X did not finish the move to 3\.0000 mm within 0\.05 s"):
        _kcube(stuck, move_timeout_s=0.05).move_to(3.0)
    assert stuck.calls == [("MoveTo", 3.0), ("StopImmediate",)]


def test_a_kcube_move_reports_where_the_stage_is_while_it_moves(monkeypatch):
    """Harm stopped: during a move (up to 10 s) or a device home (up to 30 s) the window showed the
    start position until the end, so the operator took a moving stage for a parked one (or the
    GUI for hung). While Kinesis runs the move, its Position is read about every 0.2 s and handed
    to on_progress."""
    monkeypatch.setattr(hardware, "PROGRESS_S", 0.01)
    dev = _KinesisDevice(move_s=0.1)
    threading.Timer(0.04, lambda: setattr(dev, "Position", _Decimal(2.0))).start()   # half way
    seen = []
    assert _kcube(dev).move_to(3.0, on_progress=seen.append) == 3.0
    assert 2.0 in seen


def test_the_stage_shows_a_simulated_move_on_its_way(tmp_path):
    """Harm stopped: the stage position and the crosshair froze for the whole of a move or a home
    (the motor thread reads the position only when idle). A position on the way is reported
    (marked moving, so trail and history keep only where the stage stopped), then the end."""
    import motors_sim
    from stage import Stage
    seen = []
    mx = motors_sim.SimulatedMotor("X", speed_mm_s=10.0, progress_s=0.01)
    stage = Stage(mx, motors_sim.SimulatedMotor("Y"), (0.0, 12.0, 0.0, 12.0), on_position=seen.append)
    try:
        from stage import Command
        assert stage.submit(Command("test move", "move", mm=(1.0, 0.0))).result(5).ok
    finally:
        stage.close(2.0)
    on_the_way = [p for p in seen if p.moving]
    assert on_the_way and all(0.0 < p.mm[0] < 1.0 for p in on_the_way)
    assert (seen[-1].mm, seen[-1].moving) == ((1.0, 0.0), False)


def test_a_kcube_start_up_error_is_not_hidden_by_the_clean_up(monkeypatch):
    """Harm stopped: a KCube that cannot connect (held by another program) and then also fails to
    disconnect showed only the disconnect error, so the operator chased the wrong fault. The
    start-up error is raised, naming the KCube. Kinesis is faked and pythonnet blocked: no test
    can reach a real KCube."""
    class Device:
        def Connect(self, serial):
            raise RuntimeError("device in use by another program")

        def StopPolling(self):
            raise RuntimeError("stop polling failed")

        def Disconnect(self):
            raise RuntimeError("disconnect failed")

    monkeypatch.setitem(sys.modules, "clr", None)            # import clr fails: no pythonnet
    monkeypatch.setattr(hardware, "_load_kinesis", lambda _dir: None)
    system = types.ModuleType("System")
    system.Action, system.UInt64, system.Decimal = {int: lambda fn: fn}, int, _Decimal
    dm = types.ModuleType("Thorlabs.MotionControl.DeviceManagerCLI")
    dm.DeviceManagerCLI = types.SimpleNamespace(GetDeviceList=lambda: ["00000001"])
    dc = types.ModuleType("Thorlabs.MotionControl.KCube.DCServoCLI")
    dc.KCubeDCServo = types.SimpleNamespace(CreateKCubeDCServo=lambda serial: Device())
    for name, mod in {"System": system, "Thorlabs": types.ModuleType("Thorlabs"),
                      "Thorlabs.MotionControl": types.ModuleType("Thorlabs.MotionControl"),
                      "Thorlabs.MotionControl.DeviceManagerCLI": dm,
                      "Thorlabs.MotionControl.KCube": types.ModuleType("Thorlabs.MotionControl.KCube"),
                      "Thorlabs.MotionControl.KCube.DCServoCLI": dc}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    with pytest.raises(RuntimeError, match=r"KCube X \(00000001\) did not start: device in use by another program"):
        hardware.KCube("00000001", "X", config.HardwareConfig())
