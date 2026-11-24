"""Test rig: a real SystemController (stage thread, raster session, calibration, the BLACS link's
request and publisher threads) with two in-memory motors in place of the Z912 KCubes. No Kinesis,
no camera, no socket: `Blacs` sends RaX's request envelopes over zmq_v2.InMemoryTransport."""
from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbx_zmq.zmq_v2 import InMemoryTransport, encode_request, parse_envelope  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402

import blacs_link  # noqa: E402
import raster_controller as rc  # noqa: E402
from raster_paths import RasterSpec  # noqa: E402
from raster_session import RasterPlan  # noqa: E402

TRAVEL = (0.0, 12.0, 0.0, 12.0)    # [mm] the Z912 travel (config.py HardwareConfig.travel_mm)
MM_PER_PX = 0.01                   # test calibration: motor [mm] = 0.01 * image [px]
PATH = [(100.0, 200.0), (110.0, 200.0), (120.0, 200.0)]     # [px] a 3-point test pattern
blacs_link.SERVE_POLL_MS = 20       # [ms] the request thread sees its stop at once: quick tests
LINE = RasterSpec(kind="square_x", bounds=(100.0, 120.0, 200.0, 200.0), xstep=10.0, ystep=10.0)  # = PATH


class FakeMotor:
    """One Z912 axis in memory [mm]: records every call in `calls` (shared by both axes when given).
    `gate`: a threading.Event a move waits for (a slow stage); `moving` is set once a move started.
    `clip`: the largest backlash it takes. `fail`: words of an error every move raises."""

    def __init__(self, name, calls, pos=1.0, clip=None):
        self.name, self.calls, self.pos, self.clip = name, calls, float(pos), clip
        self.backlash = 0.0
        self.gate = None
        self.moving = threading.Event()
        self.fail = None

    def get_position(self):
        return self.pos

    def move_to(self, pos, on_progress=None):
        self.moving.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError(self.fail)
        self.calls.append((self.name, "move", float(pos)))
        self.pos = float(pos)
        return self.pos

    def home(self, on_progress=None):
        self.calls.append((self.name, "home"))
        self.pos = 0.0
        return 0.0

    def set_backlash(self, value):
        self.calls.append((self.name, "set_backlash", float(value)))
        self.backlash = min(float(value), self.clip) if self.clip is not None else float(value)
        return self.backlash

    def get_backlash(self):
        self.calls.append((self.name, "get_backlash"))
        return self.backlash

    def disconnect(self):
        self.calls.append((self.name, "disconnect"))


def write_calibration(path, mm_per_px=MM_PER_PX, **extra):
    """A calibration file: motor [mm] = mm_per_px * image [px]."""
    with open(path, "w") as f:
        json.dump({"calibration_matrix": [[mm_per_px, 0.0], [0.0, mm_per_px]],
                   "calibration_offset": [0.0, 0.0], **extra}, f)
    return str(path)


def wait_until(predicate, timeout_s=2.0):
    t0 = time.monotonic()
    while not predicate():
        if time.monotonic() - t0 > timeout_s:
            raise AssertionError("timed out waiting")
        time.sleep(0.002)


class Blacs:
    """BLACS's REQ end of the link: one request, one reply, RaX's envelope (zmq_v2)."""

    def __init__(self, transport):
        self.t = transport
        self.n = 0

    def ask(self, action, connection="", value=None, *, wait_s=5.0, **args) -> dict:
        self.n += 1
        self.t.send(encode_request(action, request_id=self.n, connection=connection, value=value,
                                   args=args or None))
        reply = parse_envelope(self.t.recv(timeout_ms=int(wait_s * 1000)))
        assert reply.get("id") == self.n
        return reply

    def program(self, name, value, **args) -> dict:
        return self.ask("PROGRAM_VALUE", name, value, **args)

    def check(self, name) -> dict:
        return self.ask("CHECK_VALUE", name)


class Rig:
    """The GUI without its window. `messages`: (kind, text) log lines; `snaps`: raster snapshots;
    `cals`: calibration_changed payloads; `calls`: every motor call, both axes, in order.
    blacs=True: the BLACS link runs too; `blacs` asks it, `published` gets its PUB frames."""

    def __init__(self, tmp_path, *, calibrated=True, blacs=False, pos=1.0, clip=None):
        self.calls = []
        self.x = FakeMotor("X", self.calls, pos, clip)
        self.y = FakeMotor("Y", self.calls, pos, clip)
        self.messages, self.snaps, self.cals, self.positions, self.backlash = [], [], [], [], []
        self.ctl = rc.SystemController(self.x, self.y, travel_mm=TRAVEL,
                                       calibration_path=str(tmp_path / "calibration_data.json"),
                                       pointer_path=str(tmp_path / "last_calibration_state.json"))
        direct = Qt.DirectConnection
        self.ctl.message.connect(lambda k, t: self.messages.append((k, t)), direct)
        self.ctl.raster_changed.connect(self.snaps.append, direct)
        self.ctl.calibration_changed.connect(self.cals.append, direct)
        self.ctl.position_changed.connect(self.positions.append, direct)
        self.ctl.backlash_changed.connect(lambda a, v: self.backlash.append((a, v)), direct)
        if calibrated:
            assert self.ctl.load_calibration(write_calibration(tmp_path / "cal.json")).ok
        if blacs:
            to_gui, from_gui = InMemoryTransport.pair()
            self.published = []
            self.ctl.start_blacs_link(transport=from_gui, publish=self.published.append)
            self.blacs = Blacs(to_gui)
            assert self.blacs.ask("HELLO")["status"] == "SUCCESS"
        self.idle()

    def plan(self, spec=LINE, **kw):
        self.ctl.set_pending_plan(RasterPlan(spec, **{"pingpong": False, **kw}))

    def moves(self, axis=None):
        return [c[2] for c in self.calls if c[1] == "move" and (axis is None or c[0] == axis)]

    def refusals(self):
        return [t for k, t in self.messages if k == "refused"]

    def warnings(self, kind="warning"):
        """Log lines of `kind`: "warning" (plain) or "red_warning"."""
        return [t for k, t in self.messages if k == kind]

    def idle(self):
        """Wait until every stage job queued so far has run."""
        done = threading.Event()
        self.ctl._stage.submit(rc.Command("test: idle", "read", quiet=True, then=lambda _: done.set()))
        assert done.wait(2)

    def close(self):
        for m in (self.x, self.y):
            if m.gate is not None:
                m.gate.set()
        self.ctl.close()
