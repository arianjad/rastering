"""Motor start-up and Kinesis task completion, without Kinesis: hardware.py loads pythonnet only
when a KCube is made, and no test makes one."""
from __future__ import annotations

import dataclasses
import os
import sys
import threading

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
    "succeeds" while the real stage never moves. Making the controller fails, and the axis that
    did start is disconnected again."""
    started = []

    class KCubeStub:
        def __init__(self, serial_no, name, hw):
            if name == "Y":
                raise RuntimeError("KCube Y (27007520) not found")
            started.append(self)
            self.disconnected = False

        def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(hardware, "KCube", KCubeStub)
    cfg = dataclasses.replace(config.APP_CONFIG, network=dataclasses.replace(config.APP_CONFIG.network, blacs_link=False))
    with pytest.raises(RuntimeError, match="KCube Y"):
        rc.create_controller(cfg)
    assert [s.disconnected for s in started] == [True]
