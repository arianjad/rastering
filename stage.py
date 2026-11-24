"""The two Z912 stage axes behind one motor thread, one FIFO queue and one admission check.

Every move (raster point, go to point, jog, move in mm, one-axis move, device home, user home,
BLACS x, y) is queued here and checked once, on the motor thread, just before it runs:
  (a) a target in image pixels needs a calibration;
  (b) while a raster is armed or running, only that raster's own moves get through;
  (c) the target must lie inside the stage travel (a target within 10 um outside is moved onto
      the edge).
A refused move moves nothing; its words go back to the caller (BLACS or the window), which only
shows them. Only the motor thread calls the motors. When the queue is idle the thread reads the
position and reports it when it changed. The thread outlives any one job (an error is logged and
the next job runs); close() drops the queued jobs, lets the one in flight end, then disconnects.

Refusal codes BLACS can see are the menu's: raster_armed / raster_held / raster_running (a raster
holds the stage), not_calibrated, and move_refused for everything else (outside the travel, a
motor error, a raster point dropped by Stop or a new arm, the GUI closing); the words say which.
"""
from __future__ import annotations

import queue
import threading
import traceback
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Sequence, Tuple

import numpy as np
from bbx_zmq.menus.rastering import ARMED, LOCAL, OFF, RUNNING

from calibration import AffineCalibration

XY = Tuple[float, float]
Bounds = Tuple[float, float, float, float]   # xmin, xmax, ymin, ymax [mm]

MOTOR_EDGE_CLAMP_MM = 0.010   # a target this close outside the travel goes onto the edge [mm]
POLL_S = 0.2                  # idle this long, the motor thread reads the position [s]

# A move refused because a raster holds the stage: (code, who holds it) per raster state.
RASTER_HOLDERS = {
    ARMED: ("raster_armed", "the raster is armed for BLACS"),
    LOCAL: ("raster_held", "the raster is held at the GUI"),
    RUNNING: ("raster_running", "a continuous raster runs from the GUI"),
}


class Motor(Protocol):
    """One stage axis [mm]. Called only from the motor thread; each call returns when done."""
    def get_position(self) -> float: ...
    def move_to(self, pos: float) -> float: ...
    def home(self) -> float: ...
    def set_backlash(self, value: float) -> float: ...   # returns what the motor took
    def get_backlash(self) -> float: ...
    def disconnect(self) -> None: ...


@dataclass(frozen=True)
class Result:
    """How a stage job ended. A refusal has ok False, a code and the operator's words."""
    ok: bool
    words: str = ""
    code: str = ""
    mm: Optional[XY] = None        # where the stage is afterwards [mm]
    px: Optional[XY] = None        # a move in px: the point it was sent to [px]
    value: Optional[float] = None  # backlash [mm]


@dataclass(frozen=True)
class StagePosition:
    mm: XY                 # motor position [mm]
    px: Optional[XY]       # the same in image px; None without a calibration


@dataclass
class Command:
    """One job for the motor thread; `label` names it in the log ("Device home X").

    kind "move" takes one target: px (needs a calibration), mm, delta_mm (from where the stage
    is) or axis + value (only that axis moves); kind "home" homes `axis`; "set_backlash" /
    "get_backlash" act on `axis`; "read" reads the position.
    """
    label: str
    kind: str
    px: Optional[XY] = None
    mm: Optional[XY] = None
    delta_mm: Optional[XY] = None
    axis: Optional[str] = None
    value: Optional[float] = None
    raster_run: Optional[int] = None   # set on the raster's own moves: the run they belong to
    from_blacs: bool = False
    quiet: bool = False                # no start / done log lines (raster points, reads)
    then: Optional[Callable[[Result], None]] = None   # runs on the motor thread afterwards
    future: Future = field(default_factory=Future)


def clamp_to_bounds(xy: XY, bounds: Bounds, tol: float = MOTOR_EDGE_CLAMP_MM) -> XY:
    """Move a coordinate within `tol` outside `bounds` onto the bound; others pass unchanged."""
    xmin, xmax, ymin, ymax = bounds

    def snap(v, lo, hi):
        return lo if lo - tol <= v < lo else hi if hi < v <= hi + tol else v

    return snap(float(xy[0]), xmin, xmax), snap(float(xy[1]), ymin, ymax)


def within_travel(mm: np.ndarray, bounds: Bounds, tol: float = MOTOR_EDGE_CLAMP_MM) -> np.ndarray:
    """(N, 2) motor targets [mm] -> which ones the stage takes (after the edge snap)."""
    xmin, xmax, ymin, ymax = bounds
    x, y = mm[:, 0], mm[:, 1]
    return (x >= xmin - tol) & (x <= xmax + tol) & (y >= ymin - tol) & (y <= ymax + tol)


def out_of_bounds_message(bounds: Bounds, motor_xy: XY, target_xy: Optional[XY] = None) -> str:
    """The refusal of a target outside the travel: which point, where it maps, the travel."""
    where = f"motor ({motor_xy[0]:.4f}, {motor_xy[1]:.4f}) mm"
    if target_xy is not None:
        where = f"target ({target_xy[0]:.1f}, {target_xy[1]:.1f}) -> " + where
    return f"Rejected: motor out of bounds -- {where}, travel is {bounds}"


def _quiet(*_args) -> None:
    pass


class Stage:
    """Both axes, the travel, the calibration and the motor thread.

    raster_state() -> (state word, run) is asked on the motor thread for check (b). The on_*
    callbacks are called on the motor thread: on_log(kind, text) with kind "info", "refused" or
    "error"; on_position(StagePosition); on_backlash(axis, mm); on_busy(bool).
    """

    def __init__(self, motor_x: Motor, motor_y: Motor, travel_mm: Bounds, *,
                 raster_state: Callable[[], Tuple[str, int]] = lambda: (OFF, 0),
                 on_log=_quiet, on_position=_quiet, on_backlash=_quiet, on_busy=_quiet):
        self._motors = {"X": motor_x, "Y": motor_y}
        self.travel_mm: Bounds = tuple(float(v) for v in travel_mm)
        self._raster_state = raster_state
        self._log, self._on_position, self._on_backlash, self._on_busy = on_log, on_position, on_backlash, on_busy
        self._lock = threading.Lock()
        self._calibration: Optional[AffineCalibration] = None
        self._position: Optional[StagePosition] = None
        self._backlash = {"X": None, "Y": None}
        self._poll_failed = False
        self._closing = False
        self._queue: "queue.Queue[Optional[Command]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="motor", daemon=True)
        self._thread.start()

    # --- any thread ---

    def submit(self, cmd: Command) -> "Future[Result]":
        """Queue `cmd` for the motor thread. After close() it is refused at once."""
        with self._lock:
            if not self._closing:
                self._queue.put(cmd)
                return cmd.future
        self._settle(cmd)
        return cmd.future

    @property
    def calibration(self) -> Optional[AffineCalibration]:
        with self._lock:
            return self._calibration

    def set_calibration(self, cal: Optional[AffineCalibration]) -> None:
        with self._lock:
            self._calibration = cal
            pos = self._position
            if pos is not None:
                self._position = StagePosition(pos.mm, cal.mm_to_px(*pos.mm) if cal else None)
                pos = self._position
        if pos is not None:
            self._on_position(pos)

    @property
    def position(self) -> Optional[StagePosition]:
        with self._lock:
            return self._position

    @property
    def backlash_mm(self) -> Tuple[Optional[float], Optional[float]]:
        """The last backlash each motor reported [mm] (None: not read yet)."""
        with self._lock:
            return self._backlash["X"], self._backlash["Y"]

    def reachable(self, points_px: Sequence[XY]) -> List[bool]:
        """For each image point [px]: would check (a) and (c) let a move there through?"""
        cal = self.calibration
        if cal is None or len(points_px) == 0:
            return [False] * len(points_px)
        return within_travel(cal.px_to_mm_many(points_px), self.travel_mm).tolist()

    def close(self, timeout_s: float) -> bool:
        """Drop the queued jobs (each refused: the GUI is closing), let the job in flight end, then
        disconnect the motors and end the thread. Waits up to `timeout_s` (the longest one job may
        take); False, and an error line, when the job in flight had not ended by then."""
        with self._lock:
            self._closing = True
            self._queue.put(None)
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            self._log("error", f"The stage job in flight had not ended after {timeout_s:g} s: closing "
                               "without disconnecting the motors.")
            return False
        return True

    # --- the motor thread ---

    def _run(self) -> None:
        busy = False
        self._poll()
        while True:
            try:
                cmd = self._queue.get(timeout=POLL_S)
            except queue.Empty:
                self._poll()
                continue
            if cmd is None:
                break
            try:
                if not busy:
                    busy = True
                    self._on_busy(True)
                self._settle(cmd)
            except Exception as e:           # never end the thread: later jobs, close, disconnect
                self._thread_error(cmd, e)
            try:
                if busy and self._queue.empty():
                    busy = False
                    self._on_busy(False)
            except Exception as e:
                self._thread_error(cmd, e)
        for axis, motor in self._motors.items():
            try:
                motor.disconnect()
            except Exception as e:
                self._log("error", f"Disconnecting motor {axis} failed: {e}")

    def _settle(self, cmd: Command) -> None:
        """Run `cmd` (closing: refuse it instead), set its Future, run its follow-up. A Future
        cancelled while queued is skipped."""
        if not cmd.future.set_running_or_notify_cancel():
            if not cmd.quiet:
                self._log("info", f"{cmd.label}: cancelled before it ran.")
            return
        if self._closing:
            result = Result(False, f"{cmd.label}: not run, the GUI is closing", "move_refused")
        else:
            result = self._execute(cmd)
        cmd.future.set_result(result)
        if cmd.then is not None:
            try:
                cmd.then(result)
            except Exception as e:
                self._log("error", f"{cmd.label}: follow-up failed: {e}")

    def _thread_error(self, cmd: Command, e: Exception) -> None:
        traceback.print_exc()
        try:
            self._log("error", f"{cmd.label}: unexpected error on the motor thread ({type(e).__name__}: {e}); "
                               "the next stage jobs still run.")
        except Exception:
            pass

    def _poll(self) -> None:
        """Read the position; a failing read is logged once until a read works again."""
        try:
            self._set_position(self._read_mm())
            self._poll_failed = False
        except Exception as e:
            if not self._poll_failed:
                self._log("error", f"Reading the stage position failed: {e}")
            self._poll_failed = True

    def _execute(self, cmd: Command) -> Result:
        try:
            if cmd.kind in ("move", "home"):
                return self._move(cmd)
            motor = self._motors[cmd.axis] if cmd.axis else None
            if cmd.kind == "read":
                mm = self._read_mm()
                self._set_position(mm)
                return Result(True, mm=mm)
            if cmd.kind == "set_backlash":
                value = float(motor.set_backlash(float(cmd.value)))
            elif cmd.kind == "get_backlash":
                value = float(motor.get_backlash())
            else:
                raise ValueError(f"unknown stage job {cmd.kind!r}")
            with self._lock:
                self._backlash[cmd.axis] = value
            self._on_backlash(cmd.axis, value)
            if not cmd.quiet:
                self._log("info", f"{cmd.label}: motor {cmd.axis} backlash is {value:.4f} mm")
            return Result(True, value=value)
        except Exception as e:
            self._log("error", f"{cmd.label} failed: {e}")
            return Result(False, words=f"{cmd.label} failed: {e}", code="move_refused")

    def _move(self, cmd: Command) -> Result:
        target = self._admit(cmd)
        if isinstance(target, Result):
            self._log("refused", f"{cmd.label}: {target.words}")
            return target
        if not cmd.quiet:
            self._log("info", f"{cmd.label} ...")
        if cmd.kind == "home":
            self._motors[cmd.axis].home()
        else:                                   # X then Y, never both at once
            if cmd.axis in (None, "X"):
                self._motors["X"].move_to(target[0])
            if cmd.axis in (None, "Y"):
                self._motors["Y"].move_to(target[1])
        mm = self._read_mm()
        self._set_position(mm, cmd.px)
        if not cmd.quiet:
            self._log("info", f"{cmd.label}: done, motor ({mm[0]:.4f}, {mm[1]:.4f}) mm")
        return Result(True, mm=mm, px=cmd.px)

    def _admit(self, cmd: Command):
        """The one admission check: the target [mm] to move to (None for a home), or a refusal."""
        state, run = self._raster_state()
        if cmd.raster_run is not None:
            if state == OFF or run != cmd.raster_run:
                return Result(False, "dropped: the raster was stopped or armed again", "move_refused")
        elif state != OFF:
            code, holder = RASTER_HOLDERS[state]
            if cmd.from_blacs:
                words = (f"{holder}, so a move to x, y would leave its path: stop the raster with the "
                         "GUI's Stop button first, or take Raster_X / Raster_Y out of the sequence")
            else:
                words = f"refused, {holder}: stop the raster first"
            return Result(False, words, code)
        if cmd.kind == "home":
            return None
        if cmd.px is not None:
            cal = self.calibration
            if cal is None:
                return Result(False, "no calibration set: calibrate in the GUI first (x, y are camera pixels)",
                              "not_calibrated")
            mm = cal.px_to_mm(*cmd.px)
        elif cmd.delta_mm is not None:
            here = self._read_mm()
            mm = (here[0] + cmd.delta_mm[0], here[1] + cmd.delta_mm[1])
        elif cmd.axis is not None:
            here = self._read_mm()
            mm = (float(cmd.value), here[1]) if cmd.axis == "X" else (here[0], float(cmd.value))
        else:
            mm = (float(cmd.mm[0]), float(cmd.mm[1]))
        if not within_travel(np.array([mm]), self.travel_mm)[0]:
            return Result(False, out_of_bounds_message(self.travel_mm, mm, cmd.px), "move_refused")
        return clamp_to_bounds(mm, self.travel_mm)

    def _read_mm(self) -> XY:
        return float(self._motors["X"].get_position()), float(self._motors["Y"].get_position())

    def _set_position(self, mm: XY, px: Optional[XY] = None) -> None:
        """Record where the stage is; a move in px keeps the point it was sent to as its px
        position until the motors report a different mm."""
        with self._lock:
            old = self._position
            if px is None and old is not None and old.mm == mm:
                return
            cal = self._calibration
            if px is None and cal is not None:
                px = cal.mm_to_px(*mm)
            self._position = new = StagePosition(mm, px if cal is not None else None)
        if new != old:
            self._on_position(new)
