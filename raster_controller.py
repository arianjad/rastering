"""
controller.py

Core coordinator for the kinematic mirror / beam steering app.

Design goals:
- UI thread only renders and emits user intent.
- Motor DLL access is SINGLE-OWNED by a dedicated motor I/O thread.
- The BLACS link (menu server, RasterMenuLink) is "stupid": menu check -> controller.request_* -> reply.
- Calibration is explicit: affine transform between target-space (plot coords) and motor-space.
- Rastering is a state machine in the controller, driven by UI or ZMQ commands.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QTimer

from raster_paths import collect_points

# The menu link to BLACS comes from the beambox-daq package bbx_zmq (BBX-190).
# Install once into this GUI's Python:  python -m pip install -e <beambox-daq> --no-deps
# (or put <beambox-daq>\src first on PYTHONPATH). The rastering menu, the same file BLACS loads:
# bbx_zmq/menus/rastering.py (names, ranges, buttons, time limits, port 55535).
from bbx_zmq import MenuServer, Refused, ZmqRepTransport
from bbx_zmq.menus.rastering import MENU


# -----------------------------
# Types / dataclasses
# -----------------------------

TargetXY = Tuple[float, float]   # "target space": the coordinates the user clicks in the plot (same units as plot axes)
MotorXY = Tuple[float, float]    # motor device units (whatever Motor.get_position / move_to uses)


# -----------------------------
# Last-used calibration path persistence (small state file)
# -----------------------------
# Module-level helpers (rather than class-bound) because they're stateless
# filesystem operations that have nothing to do with SystemController instance
# state. Anchored to this module's directory so the breadcrumb survives a
# launch from a different CWD (e.g. a desktop shortcut).

LAST_CAL_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "last_calibration_state.json"
)


def load_last_calibration_path() -> Optional[str]:
    """Return the absolute path of the calibration file most recently saved
    or loaded, if it still exists on disk. Returns None on first launch (no
    state file) or if the recorded file was deleted."""
    if not os.path.exists(LAST_CAL_STATE_FILE):
        return None
    try:
        with open(LAST_CAL_STATE_FILE, "r") as f:
            data = json.load(f)
        p = data.get("last_calibration_path")
        if isinstance(p, str) and os.path.exists(p):
            return p
    except Exception:
        return None
    return None


def save_last_calibration_path(path: str) -> None:
    """Persist `path` as the last-used calibration. Non-fatal on I/O error
    (the application keeps working without the breadcrumb, but the failure
    is logged to stderr so the cause can be diagnosed)."""
    try:
        with open(LAST_CAL_STATE_FILE, "w") as f:
            json.dump({"last_calibration_path": os.path.abspath(path)}, f, indent=2)
    except Exception as e:
        import sys
        print(
            f"[raster_controller] Could not write {LAST_CAL_STATE_FILE}: {e}",
            file=sys.stderr,
        )


USER_DEFAULTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings_defaults.json"
)


def load_user_defaults() -> Optional[Dict[str, Any]]:
    """Return the persisted UI defaults dict, or None if absent/unreadable."""
    if not os.path.exists(USER_DEFAULTS_FILE):
        return None
    try:
        with open(USER_DEFAULTS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


def save_user_defaults(data: Dict[str, Any]) -> None:
    """Persist the UI defaults dict (non-fatal on I/O error -- logged to stderr)."""
    try:
        with open(USER_DEFAULTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        import sys
        print(f"[raster_controller] Could not write {USER_DEFAULTS_FILE}: {e}", file=sys.stderr)


class CommandType(Enum):
    MOVE_TARGET = auto()     # payload: {"target_xy": (x, y)}   target-space; mapped via cal if set
    MOVE_X_ONLY = auto()     # payload: {"x": float}   (y taken from cached target pos)
    MOVE_Y_ONLY = auto()     # payload: {"y": float}   (x taken from cached target pos)
    JOG_TARGET = auto()      # payload: {"delta_xy": (dx, dy)} (adds to cached target pos)

    MOVE_MOTOR = auto()      # payload: {"motor_xy": (mx, my)} motor-space; bypasses calibration
    JOG_MOTOR = auto()       # payload: {"delta_motor": (dmx, dmy)} (adds to cached motor pos)
    MOVE_MOTOR_X_ONLY = auto()  # payload: {"x": float} motor-space; live Y read inside worker
    MOVE_MOTOR_Y_ONLY = auto()  # payload: {"y": float} motor-space; live X read inside worker

    READ_POS = auto()        # no payload
    STOP = auto()            # payload: {"reason": str}

    HOME_SOFT_X = auto()
    HOME_SOFT_Y = auto()
    HOME_HARD_X = auto()
    HOME_HARD_Y = auto()

    SET_BACKLASH_X = auto()  # payload: {"value": float}
    SET_BACKLASH_Y = auto()  # payload: {"value": float}

    GET_BACKLASH_X = auto()  # no payload; returns value via MotorResult.value
    GET_BACKLASH_Y = auto()  # no payload; returns value via MotorResult.value

    NOOP = auto()


@dataclass
class MotorResult:
    ok: bool
    message: str = ""
    cmd_id: str = ""
    source: str = "ui"
    tag: str = ""

    target_xy: Optional[TargetXY] = None
    motor_xy: Optional[MotorXY] = None
    # Generic scalar payload (e.g. backlash readback). None for commands that
    # don't return a value.
    value: Optional[float] = None
    ts: float = field(default_factory=time.time)


@dataclass
class MotorCommand:
    cmd_type: CommandType
    payload: Dict[str, Any] = field(default_factory=dict)

    # metadata
    cmd_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_ts: float = field(default_factory=time.time)
    source: str = "ui"       # "ui" | "zmq" | "raster" | "internal"
    tag: str = ""            # e.g., "raster_step" for chaining logic
    priority: int = 100      # lower = higher priority (STOP should be 0)

    # synchronous reply channel (optional)
    reply_q: Optional["queue.Queue[MotorResult]"] = None


# Forgiveness at the motor travel limits (motor-space mm): a coordinate
# MEASURED at a bound maps a few um past it once the partner axis has moved
# (affine cross-terms; <=~5 um over the full field with the 2026-07-20 fit).
# Snap those onto the bound so re-asserting a measured edge position succeeds
# (2026-08-04 incident); anything further out is a real violation and still
# rejects via the motor_bounds check.
MOTOR_EDGE_CLAMP_MM = 0.010


def clamp_to_bounds(xy, bounds, tol=MOTOR_EDGE_CLAMP_MM):
    """Snap coords within ``tol`` OUTSIDE ``bounds`` onto the nearest bound.
    In-bounds and far-out values pass through unchanged; bounds=None is a
    no-op. Units: whatever ``bounds`` is in (motor mm at the call site)."""
    if bounds is None:
        return xy
    xmin, xmax, ymin, ymax = bounds

    def snap(v, lo, hi):
        if lo - tol <= v < lo:
            return lo
        if hi < v <= hi + tol:
            return hi
        return v

    return (snap(float(xy[0]), xmin, xmax), snap(float(xy[1]), ymin, ymax))


def out_of_bounds_message(bounds, motor_xy, target_xy=None) -> str:
    """Rejection text naming the coordinates that failed the travel check.

    A bare "motor out of bounds" is unactionable: the operator cannot tell
    which point of a raster path was refused, nor by how much, and the same
    string is what BLACS surfaces as the shot error.
    """
    where = f"motor ({motor_xy[0]:.4f}, {motor_xy[1]:.4f}) mm"
    if target_xy is not None:
        where = f"target ({target_xy[0]:.1f}, {target_xy[1]:.1f}) -> " + where
    return f"Rejected: motor out of bounds -- {where}, travel is {bounds}"


@dataclass
class AffineCalibration:
    """
    motor_xy = M @ target_xy + b
    target_xy = inv(M) @ (motor_xy - b)
    """
    M: np.ndarray  # shape (2,2)
    b: np.ndarray  # shape (2,)

    def __post_init__(self) -> None:
        self.M = np.array(self.M, dtype=float).reshape(2, 2)
        self.b = np.array(self.b, dtype=float).reshape(2,)
        # cache inverse (may raise if singular)
        self._Minv = np.linalg.inv(self.M)

    def target_to_motor(self, x: float, y: float) -> MotorXY:
        v = self.M @ np.array([x, y], dtype=float) + self.b
        return float(v[0]), float(v[1])

    def motor_to_target(self, mx: float, my: float) -> TargetXY:
        v = self._Minv @ (np.array([mx, my], dtype=float) - self.b)
        return float(v[0]), float(v[1])

    def to_json(self) -> Dict[str, Any]:
        return {"calibration_matrix": self.M.tolist(), "calibration_offset": self.b.tolist()}

    @staticmethod
    def from_json(data: Dict[str, Any]) -> "AffineCalibration":
        if "calibration_matrix" not in data or "calibration_offset" not in data:
            raise ValueError(
                "JSON is not a calibration bundle (missing 'calibration_matrix'/'calibration_offset'). "
                "If you selected 'last_calibration_state.json', that is a pointer file -- "
                "use the 'Use Last Value' button instead, or pick the actual calibration .json."
            )
        return AffineCalibration(M=np.array(data["calibration_matrix"]), b=np.array(data["calibration_offset"]))


@dataclass
class CalibrationSession:
    required_points: int = 3
    target_points: list[TargetXY] = field(default_factory=list)
    motor_points: list[MotorXY] = field(default_factory=list)

    def add_pair(self, target_xy: TargetXY, motor_xy: MotorXY) -> None:
        self.target_points.append((float(target_xy[0]), float(target_xy[1])))
        self.motor_points.append((float(motor_xy[0]), float(motor_xy[1])))

    @property
    def n(self) -> int:
        return len(self.target_points)

    @property
    def is_ready(self) -> bool:
        return self.n >= self.required_points

    def fit_affine(self) -> Tuple[AffineCalibration, Dict[str, Any]]:
        """
        Fits affine mapping:
            [mx]   [a b][x] + [tx]
            [my] = [c d][y]   [ty]
        using least squares with N>=3.

        Returns (AffineCalibration, diagnostics)
        """
        if self.n < 3:
            raise ValueError("Need at least 3 calibration pairs")

        # Build A matrix as in your original code:
        # for each point (x,y) -> rows [x y 1 0 0 0] and [0 0 0 x y 1]
        A = np.zeros((2 * self.n, 6), dtype=float)
        b = np.zeros((2 * self.n,), dtype=float)

        for i, ((x, y), (mx, my)) in enumerate(zip(self.target_points, self.motor_points)):
            A[2 * i, 0:3] = [x, y, 1.0]
            A[2 * i + 1, 3:6] = [x, y, 1.0]
            b[2 * i] = mx
            b[2 * i + 1] = my

        params, residuals, rank, svals = np.linalg.lstsq(A, b, rcond=None)

        # Extract affine components
        M = np.array([[params[0], params[1]],
                      [params[3], params[4]]], dtype=float)
        off = np.array([params[2], params[5]], dtype=float)

        # Diagnostics
        diag: Dict[str, Any] = {}
        diag["rank"] = int(rank)
        diag["singular_values"] = [float(v) for v in svals]
        diag["residuals"] = [float(r) for r in (residuals if np.ndim(residuals) else [])]

        # Condition number-ish for A (helps warn about degeneracy)
        if len(svals) > 0 and np.min(svals) > 0:
            diag["cond_A"] = float(np.max(svals) / np.min(svals))
        else:
            diag["cond_A"] = float("inf")

        # Additional geometric degeneracy check: area of triangle for first 3 points
        # (useful when required_points==3)
        if self.n >= 3:
            (x1, y1), (x2, y2), (x3, y3) = self.target_points[0], self.target_points[1], self.target_points[2]
            area2 = abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1))
            diag["triangle_area2_first3"] = float(area2)

        cal = AffineCalibration(M=M, b=off)  # may raise if singular
        return cal, diag


# -----------------------------
# BLACS link: the rastering menu's handlers (BBX-190)
# -----------------------------


class RasterMenuLink:
    """The handlers of this GUI's menu server (bbx_zmq MenuServer, menu bbx_zmq/menus/rastering.py).

    The menu server refuses, before these run, a name that is not on the menu and a value out of
    the menu's range. These handlers run on the server thread and only call the controller's
    thread-safe API (request_*, raster_step, the state lock); a motor move runs on the motor
    thread and is waited for up to the menu's time limit. Each answers what really happened, or
    raises Refused(code, words) with this GUI's own words:
        read_all()       every menu value: xy, x, y [px], shots_per_step, raster_state,
                         point_index, path_len
        set xy           one move of both axes to [x, y] (target frame, px). Refused without a
                         calibration (pixels would be taken as motor mm) and, with this GUI's
                         travel check, outside the stage travel ("move_refused").
        set shots_per_step   stored and shown on the GUI's status strip
        press arm        arm the path set up in the Run tab, step mode, for BLACS (through the
                         panel: remote_arm_provider); a raster armed here is handed to BLACS where
                         it is. A continuous run is refused: stop it at the GUI.
        press disarm     hand the raster to the GUI operator (path kept)
        press next_point step to the next path point; answers its details (raster_point_meta)
                         and finished = True when the path had run to its end and this step
                         started it again at point 0 (the path loops). Refused unless the raster
                         is armed for BLACS ("not_armed").
    """

    def __init__(self, controller):
        self.c = controller

    def menu_server(self, transport):
        return MenuServer(MENU, transport, read_all=self.read_all, set_value=self.set_value,
                          press_button=self.press_button)

    # --- read ---
    def raster_state(self) -> str:
        c = self.c
        with c._state_lock:
            if not c._raster_active:
                return "off"
            if c._raster_continuous:
                return "running"
            return "armed" if c._raster_source == "remote" else "local"

    def read_all(self) -> Dict[str, Any]:
        c = self.c
        state = self.raster_state()
        with c._state_lock:
            txy = c._last_target_xy
            n = len(c._raster_path_pts)
            index = c._raster_index - 1 if n else -1
            per_step = c._shots_per_step
        x, y = (float(txy[0]), float(txy[1])) if txy is not None else (None, None)
        return {"xy": [x, y] if txy is not None else None, "x": x, "y": y,
                "shots_per_step": int(per_step), "raster_state": state,
                "point_index": int(index), "path_len": int(n)}

    # --- set ---
    def set_value(self, name, value):
        c = self.c
        if name == "xy":
            if c.calibration is None:
                raise Refused("not_calibrated", "no calibration set: calibrate in the GUI first "
                                                "(x, y are camera pixels)")
            res = c.request_move_target(float(value[0]), float(value[1]), source="zmq", wait=True,
                                        timeout_s=MENU.value("xy").set_s)
            if res is None or not res.ok:
                raise Refused("move_refused", res.message if res is not None else "motor move failed")
            return [float(v) for v in res.target_xy]
        if name == "shots_per_step":
            n = int(value)
            with c._state_lock:
                c._shots_per_step = n
            c.raster_shots_per_step_signal.emit(n)
            return n
        raise Refused("no_handler", f"no handler for {name!r}")

    # --- press ---
    def press_button(self, name):
        if name == "arm":
            return self._arm()
        if name == "disarm":
            if self.raster_state() == "running":
                raise Refused("raster_running", "a continuous raster runs from the GUI; stop it there")
            was = self.raster_state() == "armed"
            if self.c.take_local_control() and was:
                self.c.status_signal.emit("BLACS released the raster; local control.")
            return {"was_armed": was}
        if name == "next_point":
            return self._next_point()
        raise Refused("no_handler", f"no handler for {name!r}")

    def _arm(self):
        c = self.c
        state = self.raster_state()
        if state == "running":
            raise Refused("raster_running", "a continuous raster runs from the GUI; stop it there")
        if state == "local":
            c.give_remote_control()
        elif state == "off":
            provider = getattr(c, "remote_arm_provider", None)
            if provider is None:
                raise Refused("no_raster_configured", "no GUI panel attached to arm a raster from")
            outcome = queue.Queue(maxsize=1)

            def reply(ok, code=None, message=None):
                try:
                    outcome.put_nowait((bool(ok), code, message))
                except queue.Full:
                    pass
            provider(False, reply)
            try:
                ok, code, message = outcome.get(timeout=MENU.button("arm").press_s)
            except queue.Empty:
                raise Refused("gui_busy", "the GUI panel did not arm the raster in time") from None
            if not ok:
                raise Refused(code or "arm_failed", message or "raster could not be armed")
        with c._state_lock:
            n, dropped = len(c._raster_path_pts), c._raster_dropped_count
        c.status_signal.emit(f"BLACS: raster armed, {n} points.")
        return {"path_len": n, "dropped": dropped}

    def _next_point(self):
        c = self.c
        state = self.raster_state()
        if state != "armed":
            raise Refused("not_armed", f"no raster armed for BLACS (raster state: {state})")
        with c._state_lock:
            wraps = c._raster_index >= len(c._raster_path_pts)
        res = c.raster_step(source="zmq", wait=True, timeout_s=MENU.button("next_point").press_s)
        if res is None:
            raise Refused("not_armed", f"next point not taken (raster state: {self.raster_state()})")
        if not res.ok:
            raise Refused("move_refused", res.message)
        meta = c.raster_point_meta(res)
        x, y = meta.pop("target_xy", (None, None))
        if wraps:
            c.status_signal.emit("BLACS: raster path finished; started again at point 1.")
        return {**meta, "x": x, "y": y, "finished": wraps}


# -----------------------------
# Controller
# -----------------------------

class SystemController(QObject):
    """
    Coordinates motors, calibration, raster sessions, and ZMQ requests.

    UI and ZMQ should only call request_* methods. They are safe from any thread.
    """

    # --- signals to UI ---
    status_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    # Positions (in target space and motor space)
    target_position_signal = pyqtSignal(float, float)
    motor_position_signal = pyqtSignal(float, float)

    # Async motor-backlash readback (axis "X"/"Y", value). Lets the UI refresh
    # the Backlash Reading label WITHOUT a blocking GUI-thread wait on the
    # motor FIFO -- mirrors motor_position_signal. Emitted by _deliver_result
    # for GET_BACKLASH_* results.
    backlash_reading_signal = pyqtSignal(str, float)

    # Command completion (useful for UI + raster chaining)
    command_done_signal = pyqtSignal(str, bool, str, str)  # cmd_id, ok, message, tag

    # Calibration lifecycle
    calibration_prompt_signal = pyqtSignal(str)
    calibration_progress_signal = pyqtSignal(int, int)  # collected, required
    calibration_ready_signal = pyqtSignal(object)       # AffineCalibration
    calibration_failed_signal = pyqtSignal(str)

    # Raster lifecycle
    raster_state_signal = pyqtSignal(bool)              # active?
    raster_finished_signal = pyqtSignal()
    raster_log_path_signal = pyqtSignal(str)
    # Who is driving the raster: None (idle) / "local" (GUI) / "remote" (ZMQ,
    # i.e. BLACS). Display-only for the UI -- it never disables local controls.
    raster_source_signal = pyqtSignal(object)
    # Shots-per-step BLACS last programmed (int), or None when unknown/disarmed.
    # Display-only: the GUI never acts on it, it just shows the operator how many
    # shots BLACS fires before asking for the next point.
    raster_shots_per_step_signal = pyqtSignal(object)

    # Path-point selection (F2: "go to an arbitrary site"). (index, x, y);
    # index == -1 means the selection was cleared. Emitting it never moves motors.
    selection_changed_signal = pyqtSignal(int, float, float)

    def __init__(
        self,
        motor_x: Any,
        motor_y: Any,
        *,
        calibration_path: str = "calibration_data.json",
        target_bounds: Optional[Tuple[float, float, float, float]] = None,
        motor_bounds: Optional[Tuple[float, float, float, float]] = None,
        motor_bounds_units: str = "motor",
        telemetry_period_s: float = 0.2,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)

        self.motor_x = motor_x
        self.motor_y = motor_y

        self._state_lock = threading.RLock()

        # Remote arm bridge: ui.py registers a callable
        # (want_continuous: bool, reply: callable(ok, code=None, message=None))
        # that the ZMQ server invokes to arm a raster from the GUI's configured
        # path. Called from the ZMQ thread -- the provider must not touch
        # widgets directly; it marshals to the Qt main thread itself and calls
        # `reply` exactly once. None => headless (no UI attached).
        self.remote_arm_provider = None

        # Calibration
        self.calibration_path = calibration_path
        self.calibration: Optional[AffineCalibration] = None
        self._cal_session: Optional[CalibrationSession] = None

        # User Home: stored (X, Y) motor target position the user can move to
        # via "Go to User Home". NOT a coordinate-frame origin -- reported motor
        # positions and the calibration matrix are unaffected.
        self._user_home_x: float = 0.0
        self._user_home_y: float = 0.0

        # Bounds
        if motor_bounds is not None and motor_bounds_units != "motor":
            raise ValueError(
                "motor_bounds is motor-space mm (units='motor'); got "
                f"units={motor_bounds_units!r} -- pixel/target-space limits "
                "belong in target_bounds"
            )
        self.target_bounds = target_bounds   # xmin, xmax, ymin, ymax in target space
        self.motor_bounds = motor_bounds     # xmin, xmax, ymin, ymax in motor units

        # Cached positions
        self._last_motor_xy: Optional[MotorXY] = None
        self._last_target_xy: Optional[TargetXY] = None

        # Command queue + worker thread
        # Heap key is (priority, seq, cmd). `seq` is a unique monotonic
        # tiebreaker so the tuple comparison never reaches the MotorCommand
        # (which has no __lt__): two same-priority commands enqueued within
        # one time.time() tick -- e.g. Device Home X then Y -- would otherwise
        # crash with "TypeError: '<' not supported between MotorCommand". This
        # is the documented heapq/PriorityQueue pattern. next() on an
        # itertools.count() is atomic under the GIL, so it is safe for the
        # multiple producer threads (UI, telemetry timer, ZMQ server).
        self._q: "queue.PriorityQueue[Tuple[int, int, MotorCommand]]" = queue.PriorityQueue()
        self._q_seq = itertools.count()
        self._stop_evt = threading.Event()
        self._motor_thread = threading.Thread(target=self._motor_worker_loop, name="motor-io", daemon=True)
        self._motor_thread.start()

        # Raster state. The path is materialized into an indexed list so the
        # cursor is seekable (enables go-to-arbitrary-site); _raster_index is
        # the index of the NEXT point to emit (exhausted iff index >= len).
        self._raster_path_pts: List[TargetXY] = []
        self._raster_index: int = 0
        self._raster_selected_index: int = -1
        self._raster_active: bool = False
        self._raster_continuous: bool = False
        # BeamBox: continuous run wraps instead of finishing (start_raster repeat=True).
        self._raster_repeat: bool = False
        # BeamBox: points in the pattern as drawn, i.e. BEFORE the
        # motor-travel filter and before repeat / ping-pong expansion. The UI
        # compares the on-screen (unfiltered) pattern with THIS, not with the
        # armed path, which the filter shortens and ping-pong doubles.
        self._raster_pattern_len: int = 0
        # None while no raster is active; "local" / "remote" otherwise.
        self._raster_source: Optional[str] = None
        # Shots BLACS takes per raster point (menu value shots_per_step, BBX-190). BLACS
        # counts the shots and presses next_point; the GUI holds the number and shows it.
        self._shots_per_step: int = 1
        self._raster_delay_s = 0.0
        self._raster_log: list[Dict[str, Any]] = []
        self._raster_log_path: Optional[str] = None
        self._raster_total_steps: int = 0
        # Points the last arm dropped as unreachable; reported on the arm reply.
        self._raster_dropped_count: int = 0

        # Telemetry polling (via READ_POS commands, so it never touches DLL outside motor thread)
        self._telemetry_period_s = float(telemetry_period_s)
        self._telemetry_enabled = self._telemetry_period_s > 0
        self._telemetry_thread = threading.Thread(target=self._telemetry_loop, name="telemetry", daemon=True)
        if self._telemetry_enabled:
            self._telemetry_thread.start()

        # BLACS link (menu server thread, BBX-190)
        self._menu_thread: Optional[threading.Thread] = None
        self._menu_stop_evt = threading.Event()

        # Raster chaining: advance on completion of raster-tagged move
        self.command_done_signal.connect(self._on_command_done)

    # -------------------------
    # Public API (UI / ZMQ)
    # -------------------------

    def request_move_target(self, x: float, y: float, *, source: str = "ui", wait: bool = False, timeout_s: float = 10.0) -> Optional[MotorResult]:
        reply_q = queue.Queue(maxsize=1) if wait else None
        cmd = MotorCommand(
            cmd_type=CommandType.MOVE_TARGET,
            payload={"target_xy": (float(x), float(y))},
            source=source,
            tag="move_target",
            reply_q=reply_q,
        )
        self._enqueue(cmd)
        if wait:
            return self._wait_reply(reply_q, cmd.cmd_id, timeout_s)
        return None

    def request_move_x(self, x: float, *, source: str = "ui", wait: bool = False, timeout_s: float = 10.0) -> Optional[MotorResult]:
        reply_q = queue.Queue(maxsize=1) if wait else None
        cmd = MotorCommand(
            cmd_type=CommandType.MOVE_X_ONLY,
            payload={"x": float(x)},
            source=source,
            tag="move_x",
            reply_q=reply_q,
        )
        self._enqueue(cmd)
        if wait:
            return self._wait_reply(reply_q, cmd.cmd_id, timeout_s)
        return None

    def request_move_y(self, y: float, *, source: str = "ui", wait: bool = False, timeout_s: float = 10.0) -> Optional[MotorResult]:
        reply_q = queue.Queue(maxsize=1) if wait else None
        cmd = MotorCommand(
            cmd_type=CommandType.MOVE_Y_ONLY,
            payload={"y": float(y)},
            source=source,
            tag="move_y",
            reply_q=reply_q,
        )
        self._enqueue(cmd)
        if wait:
            return self._wait_reply(reply_q, cmd.cmd_id, timeout_s)
        return None

    def request_move_motor(self, mx: float, my: float, *, source: str = "ui", wait: bool = False, timeout_s: float = 10.0) -> Optional[MotorResult]:
        """
        Move directly in motor coordinates. Bypasses calibration entirely; the (mx, my)
        argument is used as the motor target. Used by the manual-controls "Move to Position"
        flow where the spinboxes display motor units regardless of calibration state.
        """
        reply_q = queue.Queue(maxsize=1) if wait else None
        cmd = MotorCommand(
            cmd_type=CommandType.MOVE_MOTOR,
            payload={"motor_xy": (float(mx), float(my))},
            source=source,
            tag="move_motor",
            reply_q=reply_q,
        )
        self._enqueue(cmd)
        if wait:
            return self._wait_reply(reply_q, cmd.cmd_id, timeout_s)
        return None

    def request_move_motor_axis(self, axis: str, value: float, *, source: str = "ui", wait: bool = False, timeout_s: float = 10.0) -> Optional[MotorResult]:
        """
        Move a single motor axis to `value`. The other axis stays where it is
        -- its position is read live inside the motor worker thread just before
        the move, so the result is correct even if other commands are queued
        ahead of this one. Use this in preference to MOVE_MOTOR with a cached
        snapshot when only one axis should move.
        """
        axis = axis.upper().strip()
        if axis not in ("X", "Y"):
            self.error_signal.emit("Single-axis move rejected: axis must be 'X' or 'Y'")
            return None
        cmd_type = CommandType.MOVE_MOTOR_X_ONLY if axis == "X" else CommandType.MOVE_MOTOR_Y_ONLY
        payload_key = "x" if axis == "X" else "y"
        reply_q = queue.Queue(maxsize=1) if wait else None
        cmd = MotorCommand(
            cmd_type=cmd_type,
            payload={payload_key: float(value)},
            source=source,
            tag=f"move_motor_{axis.lower()}_only",
            reply_q=reply_q,
        )
        self._enqueue(cmd)
        if wait:
            return self._wait_reply(reply_q, cmd.cmd_id, timeout_s)
        return None

    def request_jog_motor(self, dmx: float, dmy: float, *, source: str = "ui", wait: bool = False, timeout_s: float = 10.0) -> Optional[MotorResult]:
        """
        Jog by a delta in motor coordinates (adds to cached motor position).
        Used by manual-controls jog buttons; step sizes are interpreted as motor units.
        """
        reply_q = queue.Queue(maxsize=1) if wait else None
        cmd = MotorCommand(
            cmd_type=CommandType.JOG_MOTOR,
            payload={"delta_motor": (float(dmx), float(dmy))},
            source=source,
            tag="jog_motor",
            reply_q=reply_q,
        )
        self._enqueue(cmd)
        if wait:
            return self._wait_reply(reply_q, cmd.cmd_id, timeout_s)
        return None

    def request_stop(self, *, reason: str = "user") -> None:
        cmd = MotorCommand(cmd_type=CommandType.STOP, payload={"reason": reason}, source="ui", tag="stop", priority=0)
        self._enqueue(cmd)

    # ------------------------------------------------------------------
    # User Home (stored motor target position; not a coordinate-frame origin)
    # ------------------------------------------------------------------

    def get_user_home(self, axis: str) -> float:
        axis = axis.upper().strip()
        with self._state_lock:
            return self._user_home_x if axis == "X" else self._user_home_y

    def set_user_home(self, axis: str, value: float) -> float:
        axis = axis.upper().strip()
        v = float(value)
        with self._state_lock:
            if axis == "X":
                self._user_home_x = v
            else:
                self._user_home_y = v
        return v

    def set_user_home_xy(self, x: float, y: float) -> Tuple[float, float]:
        x, y = float(x), float(y)
        with self._state_lock:
            self._user_home_x = x
            self._user_home_y = y
        return x, y

    def get_user_home_xy(self) -> Tuple[float, float]:
        with self._state_lock:
            return self._user_home_x, self._user_home_y

    def request_go_user_home(self, axis: Optional[str] = None, *, source: str = "ui") -> None:
        """
        Move the motor to the stored User Home value. axis="X"/"Y" sends a
        single-axis MOVE_MOTOR_*_ONLY (the worker reads the other-axis live
        position just before moving, so a click immediately after a prior
        Home / Move does not race a stale snapshot). axis=None sends a full-XY
        MOVE_MOTOR.
        """
        ux, uy = self.get_user_home_xy()
        if axis is None:
            self.request_move_motor(ux, uy, source=source)
            return
        axis = axis.upper().strip()
        if axis == "X":
            self.request_move_motor_axis("X", ux, source=source)
        else:
            self.request_move_motor_axis("Y", uy, source=source)

    def request_home(self, axis: str, *, hard: bool = False, source: str = "ui", wait: bool = False, timeout_s: float = 30.0) -> Optional[MotorResult]:
        axis = axis.upper().strip()
        if axis not in ("X", "Y"):
            self.error_signal.emit("Home request rejected: axis must be 'X' or 'Y'")
            return None
        cmd_type = {
            ("X", False): CommandType.HOME_SOFT_X,
            ("Y", False): CommandType.HOME_SOFT_Y,
            ("X", True): CommandType.HOME_HARD_X,
            ("Y", True): CommandType.HOME_HARD_Y,
        }[(axis, hard)]
        reply_q = queue.Queue(maxsize=1) if wait else None
        cmd = MotorCommand(cmd_type=cmd_type, payload={}, source=source, tag=f"home_{axis}_{'hard' if hard else 'soft'}", reply_q=reply_q)
        self._enqueue(cmd)
        if wait:
            return self._wait_reply(reply_q, cmd.cmd_id, timeout_s)
        return None

    def request_set_backlash(self, axis: str, value: float, *, source: str = "ui", wait: bool = False, timeout_s: float = 5.0) -> Optional[MotorResult]:
        axis = axis.upper().strip()
        if axis not in ("X", "Y"):
            self.error_signal.emit("Backlash request rejected: axis must be 'X' or 'Y'")
            return None
        cmd_type = CommandType.SET_BACKLASH_X if axis == "X" else CommandType.SET_BACKLASH_Y
        reply_q = queue.Queue(maxsize=1) if wait else None
        cmd = MotorCommand(cmd_type=cmd_type, payload={"value": float(value)}, source=source, tag=f"backlash_{axis}", reply_q=reply_q)
        self._enqueue(cmd)
        if wait:
            return self._wait_reply(reply_q, cmd.cmd_id, timeout_s)
        return None

    def request_get_backlash(self, axis: str, *, source: str = "ui", wait: bool = True, timeout_s: float = 2.0) -> Optional[MotorResult]:
        """
        Read the current motor backlash for one axis. Routed through the motor
        thread so the DLL is touched only from its single owning thread.

        Default wait=True because the typical caller (UI startup populate) needs
        the value synchronously to set its spinbox. The result's `value` field
        carries the readback.
        """
        axis = axis.upper().strip()
        if axis not in ("X", "Y"):
            self.error_signal.emit("Get-backlash request rejected: axis must be 'X' or 'Y'")
            return None
        cmd_type = CommandType.GET_BACKLASH_X if axis == "X" else CommandType.GET_BACKLASH_Y
        reply_q = queue.Queue(maxsize=1) if wait else None
        cmd = MotorCommand(
            cmd_type=cmd_type,
            payload={},
            source=source,
            tag=f"get_backlash_{axis}",
            priority=50,   # higher than telemetry, below STOP — same as request_pos
            reply_q=reply_q,
        )
        self._enqueue(cmd)
        if wait:
            return self._wait_reply(reply_q, cmd.cmd_id, timeout_s)
        return None
    
    def request_pos(self, *, source: str = "ui", wait: bool = False, timeout_s: float = 2.0) -> Optional[MotorResult]:
        """
        Request a fresh motor position read.

        If wait=True, blocks until the motor thread returns the readback (safe for calibration).
        """
        reply_q = queue.Queue(maxsize=1) if wait else None
        cmd = MotorCommand(
            cmd_type=CommandType.READ_POS,
            payload={},
            source=source,
            tag="read_pos",
            priority=50,   # higher priority than telemetry (but below STOP)
            reply_q=reply_q,
        )
        self._enqueue(cmd)
        if wait:
            return self._wait_reply(reply_q, cmd.cmd_id, timeout_s)
        return None


    # --- calibration control ---

    def start_calibration(self, required_points: int = 3) -> None:
        with self._state_lock:
            self._cal_session = CalibrationSession(required_points=int(required_points))
        self.calibration_prompt_signal.emit(
            f"Calibration: jog the beam to {required_points} distinct spots; click the laser spot each time."
        )
        self.calibration_progress_signal.emit(0, int(required_points))

    def cancel_calibration(self) -> None:
        with self._state_lock:
            self._cal_session = None
        self.calibration_prompt_signal.emit("Calibration cancelled.")

    def add_calibration_click(self, x: float, y: float) -> None:
        """
        Called by UI when the user clicks the laser spot during calibration.

        We MUST pair this click with a fresh motor readback to avoid telemetry race conditions.
        """
        with self._state_lock:
            sess = self._cal_session

        if sess is None:
            return

        # Optional guard: if your motor class exposes is_moving(), reject mid-motion clicks
        try:
            if hasattr(self.motor_x, "is_moving") and self.motor_x.is_moving():
                self.calibration_failed_signal.emit("Wait for motion to stop, then click the laser spot.")
                return
            if hasattr(self.motor_y, "is_moving") and self.motor_y.is_moving():
                self.calibration_failed_signal.emit("Wait for motion to stop, then click the laser spot.")
                return
        except Exception:
            pass

        # Force a fresh read from motor thread
        res = self.request_pos(source="internal", wait=True, timeout_s=2.0)
        if not res or (not res.ok) or (res.motor_xy is None):
            self.calibration_failed_signal.emit("Could not read motor position for calibration point.")
            return

        motor_xy = res.motor_xy
        sess.add_pair((float(x), float(y)), motor_xy)

        self.calibration_progress_signal.emit(sess.n, sess.required_points)

        if sess.is_ready:
            self._finish_calibration()


    def _finish_calibration(self) -> None:
        with self._state_lock:
            sess = self._cal_session
        if sess is None:
            return

        try:
            cal, diag = sess.fit_affine()
        except Exception as e:
            self.calibration_failed_signal.emit(f"Calibration failed: {e}")
            return

        # Basic degeneracy guard (first 3 points collinear -> tiny area)
        area2 = diag.get("triangle_area2_first3", None)
        if area2 is not None and area2 < 1e-6:
            self.calibration_failed_signal.emit("Calibration failed: first 3 points nearly collinear. Choose a triangle.")
            return

        self.set_calibration(cal)

        # Auto-save the freshly-fit affine to the default calibration path
        # using the bundled schema. camera_settings stays None here -- those
        # live in the UI dock and would need a callback; the manual
        # "Save Calibration As..." flow is the right place to bundle them in.
        # is_raster_running is implicitly False (calibration and raster are
        # mutually exclusive UI modes), so the guard is a no-op here.
        try:
            self.save_calibration_to_path(
                self.calibration_path,
                camera_settings=None,
                notes="auto-saved after fit",
            )
        except Exception as e:
            self.error_signal.emit(f"Auto-save after calibration failed: {e}")

        with self._state_lock:
            self._cal_session = None

        self.calibration_ready_signal.emit(cal)

        # Geometric mean of x/y scales: sqrt(|det(M)|) gives motor-units per target-unit.
        try:
            scale = float(np.sqrt(abs(np.linalg.det(cal.M))))
            scale_str = f"{scale:.4g}"
        except Exception:
            scale_str = "n/a"
        cond_A = diag.get("cond_A", float("nan"))
        try:
            cond_str = f"{float(cond_A):.3g}"
        except Exception:
            cond_str = str(cond_A)
        self.status_signal.emit(
            f"Calibration complete: scale~{scale_str} motor/target, cond(A)~{cond_str}"
        )

    def set_calibration(self, cal: AffineCalibration) -> None:
        with self._state_lock:
            self.calibration = cal
        self.status_signal.emit("Calibration set.")

        # Update cached target position if possible
        with self._state_lock:
            lm = self._last_motor_xy
            c = self.calibration
        if lm is not None and c is not None:
            tx, ty = c.motor_to_target(lm[0], lm[1])
            with self._state_lock:
                self._last_target_xy = (tx, ty)
            self.target_position_signal.emit(tx, ty)

    def clear_calibration(self) -> None:
        with self._state_lock:
            self.calibration = None
        self.status_signal.emit("Calibration cleared.")

    # --- Extended calibration save/load (named-file with bundled state) ---

    @property
    def is_raster_running(self) -> bool:
        with self._state_lock:
            return self._raster_active

    def _read_motor_backlash_xy(
        self, *, timeout_s: float = 60.0
    ) -> Tuple[Optional[float], Optional[float]]:
        """Synchronous reads of the live motor backlash for both axes. Returns
        (x, y); either may be None if the motor doesn't support it or the
        read fails outright. Used by save_calibration_to_path to bundle the
        value.

        timeout_s is generous (default 60s) so the request can wait behind a
        long-running command on the motor FIFO -- e.g. a Device Home that
        takes up to 45s. The motor command queue serializes naturally: this
        request runs as soon as the currently-executing command finishes.
        """
        out: Dict[str, Optional[float]] = {"X": None, "Y": None}
        for axis in ("X", "Y"):
            try:
                res = self.request_get_backlash(axis, wait=True, timeout_s=timeout_s)
                if res is not None and res.ok and res.value is not None:
                    out[axis] = float(res.value)
            except Exception:
                pass
        return out["X"], out["Y"]

    def save_calibration_to_path(
        self,
        path: str,
        *,
        camera_settings: Optional[Dict[str, Any]] = None,
        notes: str = "",
    ) -> None:
        """
        Snapshot the current calibration + user home + motor backlash +
        (caller-supplied) camera_settings into a bundled JSON file at `path`.
        camera_settings is opaque to the controller -- the UI passes whatever
        AOI / rotation / flip dict the camera dock exposes.

        Raises on I/O failure.
        """
        if self.is_raster_running:
            raise RuntimeError("Refusing to save calibration while raster is running.")
        with self._state_lock:
            cal = self.calibration
            uhx, uhy = self._user_home_x, self._user_home_y
        if cal is None:
            raise RuntimeError("No calibration to save.")
        bx, by = self._read_motor_backlash_xy()
        # If the motor was busy (or never connected), backlash reads return
        # None. Save the bundle anyway -- partial bundle is still useful --
        # but emit a visible advisory so the user knows the JSON is lossy.
        if bx is None or by is None:
            self.status_signal.emit(
                "Warning: motor backlash not read (motor busy or disconnected). "
                "Saved bundle has null backlash for one or both axes."
            )
        bundle: Dict[str, Any] = {
            **cal.to_json(),
            "user_home": {"x": float(uhx), "y": float(uhy)},
            "backlash": {
                "x": float(bx) if bx is not None else None,
                "y": float(by) if by is not None else None,
            },
            "camera_settings": camera_settings,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "notes": str(notes or ""),
        }
        with open(path, "w") as f:
            json.dump(bundle, f, indent=2)
        save_last_calibration_path(path)
        self.status_signal.emit(f"Calibration saved to {path}")

    def load_calibration_from_path(self, path: str, *, _depth: int = 0) -> Dict[str, Any]:
        """
        Read a bundled calibration JSON from `path`. Immediately applies:
          - affine matrix (set_calibration)
          - user home values (set_user_home_xy)
          - motor backlash (request_set_backlash per axis)

        Returns the bundled `camera_settings` dict (or None) for the caller
        to apply on demand. Caller is responsible for any UI updates after
        the apply (e.g. refreshing reading labels).

        Backward-compatible: files containing only calibration_matrix +
        calibration_offset (the legacy schema) load cleanly with all
        new bundled fields treated as unset.

        Raises on parse / fit failure.
        """
        if self.is_raster_running:
            raise RuntimeError("Refusing to load calibration while raster is running.")
        if not os.path.exists(path):
            raise FileNotFoundError(f"No calibration file found: {path}")
        with open(path, "r") as f:
            data = json.load(f)

        # If the user browsed to the breadcrumb pointer file
        # (last_calibration_state.json: {"last_calibration_path": "..."}),
        # follow it to the real bundle instead of failing on the missing matrix.
        if "calibration_matrix" not in data and isinstance(data.get("last_calibration_path"), str):
            if _depth >= 4:
                raise ValueError("calibration pointer chain too deep (possible cycle); pick the actual calibration .json")
            target = data["last_calibration_path"]
            if os.path.exists(target) and os.path.abspath(target) != os.path.abspath(path):
                return self.load_calibration_from_path(target, _depth=_depth + 1)

        # Affine matrix is required
        cal = AffineCalibration.from_json(data)
        self.set_calibration(cal)

        # User home is optional
        uh = data.get("user_home")
        if isinstance(uh, dict) and "x" in uh and "y" in uh:
            self.set_user_home_xy(float(uh["x"]), float(uh["y"]))

        # Backlash is optional. Applied via request_set_backlash so the motor
        # accepts via the same path the manual Set uses (handles Decimal
        # conversion in the worker).
        bl = data.get("backlash")
        if isinstance(bl, dict):
            for axis_key, axis in (("x", "X"), ("y", "Y")):
                v = bl.get(axis_key)
                if v is not None:
                    try:
                        self.request_set_backlash(axis, float(v))
                    except Exception as e:
                        self.error_signal.emit(f"Failed to apply backlash {axis} from cal: {e}")

        save_last_calibration_path(path)
        # Signal "ready" only after the FULL bundle applied cleanly (matrix +
        # user_home + backlash). If any step above raised, we never reach here,
        # so the UI won't show a half-applied calibration as "ready".
        self.calibration_ready_signal.emit(cal)
        self.status_signal.emit(f"Loaded calibration from {path}")
        return data

    # --- Last-used calibration path persistence: see module-level
    #     load_last_calibration_path / save_last_calibration_path above.

    # --- raster control ---

    def start_raster(self, path_iter, *, continuous: bool = True, log_dir: str | None = None, delay_s: float = 0.0, source: str = "local",
                     repeat: int | bool = 1, pingpong: bool = True) -> None:
        """
        Start rastering along a target-space path (iterable of (x,y) points).
        If continuous=False, raster is "armed" and only advances when raster_step() is called.

        `source` records who armed it ("local" = GUI panel, "remote" = armed on
        a ZMQ client's behalf); it only drives the UI's control indicator.

        `repeat` (BeamBox) says what a CONTINUOUS run does when the path runs
        out (step mode always wraps, see raster_step):
            1     - stop, the upstream behaviour
            n > 1 - run the path n times
            True  - keep going until stop_raster() is called
        With `pingpong` (only used when repeating) the path reverses every
        other lap, so it retraces its own steps instead of jumping from the
        last point back to the first. That saves one long move per lap, and
        stops the stages reversing direction at an arbitrary point -- which
        matters, they have backlash. The turnaround points are visited twice
        in a row (last point of one lap = first point of the next).
        """
        with self._state_lock:
            cal = self.calibration
        if cal is None:
            self.status_signal.emit("Cannot start raster: no calibration set. Calibrate first.")
            return

        # Materialize the path once into an indexed list (capped, same as the
        # preview). This makes the cursor seekable (go-to-arbitrary-site) and
        # gives a real total for the progress display. Refuse an empty path
        # rather than arming and immediately finishing.
        try:
            pts = collect_points(path_iter, max_points=50000)
        except Exception as e:
            # Degenerate hull / too-fine grid / step=0 raise on the first next()
            # inside collect_points (the path generator body is deferred).
            self.status_signal.emit(f"Cannot start raster: {e}")
            return
        if not pts:
            self.status_signal.emit("Cannot start raster: path has no points.")
            return
        if len(pts) >= 50000:
            self.status_signal.emit(
                "Warning: raster path hit the 50000-point cap and was truncated; "
                "the progress total reflects only the first 50000 points."
            )

        # Drop points the motor can never reach. Part of the camera frame maps
        # outside travel under any calibration, and nothing on screen marks it,
        # so a pattern drawn over that region arms happily and then fails one
        # point at a time at STEP time -- which mid-queue is one failed shot per
        # point. A convex hull is the worst case: it emits its grid x-ascending
        # from the bounding-box corner, so the unreachable column comes FIRST
        # and every retry lands on another one.
        dropped = 0
        if self.motor_bounds is not None:
            reachable = [p for p in pts
                         if self._within_bounds(
                             self._target_to_motor_clamped(cal, p), self.motor_bounds)]
            dropped = len(pts) - len(reachable)
            pts = reachable
            if not pts:
                self.status_signal.emit(
                    "Cannot start raster: every point of the pattern is outside "
                    f"the motor travel {self.motor_bounds} mm. Move the pattern "
                    "toward the reachable part of the frame, or recalibrate."
                )
                return
            if dropped:
                self.status_signal.emit(
                    f"Raster armed: {len(pts)} of {len(pts) + dropped} points; "
                    f"{dropped} dropped, outside motor travel "
                    f"{self.motor_bounds} mm."
                )

        # BeamBox: repeat / ping-pong. Test `is True` explicitly, never
        # `== 1`: in Python True == 1, so a `repeat != 1` guard silently skips
        # looping when repeat is True.
        forever = repeat is True
        # Count as drawn: the travel filter's drops included (BBX-173 R2-3).
        # The on-screen preview is unfiltered, so the post-filter count made
        # the UI say "pending -- press Re-arm" after every arm that dropped
        # a point, and each Re-arm restarts a BLACS raster at point 0.
        pattern_len = len(pts) + dropped
        try:
            laps = 1 if forever else max(1, int(repeat))
        except (TypeError, ValueError):
            laps = 1
        if forever:
            # One cycle; the cursor wraps it (continuous via _raster_repeat,
            # step mode always). With ping-pong a cycle is out-and-back.
            if pingpong:
                pts = pts + pts[::-1]
        elif laps > 1:
            pts = [p for lap in range(laps)
                   for p in (pts[::-1] if (pingpong and lap % 2) else pts)]

        with self._state_lock:
            self._raster_path_pts = pts
            self._raster_repeat = forever
            self._raster_pattern_len = pattern_len
            self._raster_index = 0
            self._raster_selected_index = -1
            self._raster_active = True
            self._raster_continuous = bool(continuous)
            self._raster_source = source
            self._raster_delay_s = float(delay_s) if continuous else 0.0
            self._raster_log = []
            self._raster_log_path = None
            self._raster_total_steps = len(pts)
            # Written with the path, not beside it: the arm reply reads both
            # under this same lock, so the count can never describe a different
            # arm than the one it is reported with.
            self._raster_dropped_count = dropped

        # log_dir=None -> per-pass JSON logging off (the default; enable
        # via config.paths.raster_log_enabled).
        if log_dir is not None:
            os.makedirs(log_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            self._raster_log_path = os.path.join(log_dir, f"raster_log_{ts}.json")
            self.raster_log_path_signal.emit(self._raster_log_path)

        self.raster_state_signal.emit(True)
        self.raster_source_signal.emit(source)
        started = "Raster started." if continuous else "Raster armed (step mode)."
        if forever:
            started += " Looping until stopped."
        elif laps > 1:
            started += f" {laps} laps."
        self.status_signal.emit(started)

        if continuous:
            self._enqueue_next_raster_point()


    def _next_raster_point_locked(self, *, wrap: bool = False) -> Optional[TargetXY]:
        """Return the next path point and advance the cursor, or None if the
        path is exhausted (the StopIteration equivalent). With wrap=True a
        non-empty exhausted path rewinds to point 0 instead: the armed
        pattern repeats and the raster never finishes. CALLER MUST HOLD
        self._state_lock -- this is the single place the cursor advances."""
        n = len(self._raster_path_pts)
        if self._raster_index >= n:
            if not (wrap and n):
                return None
            # Rewind BEFORE consuming: raster_point_meta reads
            # _raster_index - 1, so rewinding after would stamp
            # point_index -1 into the shot h5.
            self._raster_index = 0
        pt = self._raster_path_pts[self._raster_index]
        self._raster_index += 1
        return pt

    def raster_step(self, *, source: str = "ui", wait: bool = False, timeout_s: float = 10.0) -> Optional[MotorResult]:
        """
        Advance exactly one raster step.

        - In step mode (continuous=False): this is the only way the raster advances.
        - In continuous mode: we *reject* explicit stepping to avoid double-driving.
        - While BLACS owns the raster: we *reject* non-zmq stepping. Advancing the
          cursor behind BLACS's back desyncs its shot bookkeeping; the operator
          hands the raster back (take_local_control) or stops it first.
        """
        with self._state_lock:
            active = self._raster_active
            continuous = self._raster_continuous
            # Decided under the lock, before the cursor can move.
            refused_remote = (active and source != "zmq"
                              and self._raster_source == "remote")
            # Mirror image: BLACS must not advance a raster the operator holds.
            # This is NOT an error -- the shot still fires, at whatever point
            # the operator last stepped to. The handler turns it into a SUCCESS
            # reply carrying the current point's meta.
            held_by_operator = (active and source == "zmq"
                                and self._raster_source == "local")
            stepping = (active and not continuous
                        and not refused_remote and not held_by_operator)
            # Step-mode stepping wraps for every source: the armed pattern
            # repeats until Stop / disarm_raster, the only teardown paths.
            # An operator Step past the last point must not tear down the
            # raster BLACS is stepping (2026-08-04; ends the zmq-vs-ui
            # split). Continuous runs keep single-pass semantics via
            # _enqueue_next_raster_point.
            pt = (self._next_raster_point_locked(wrap=True)
                  if stepping else None)
        # Ownership is deliberately NOT touched here. Last-stepper-wins made
        # _raster_source an artifact of whoever called last, so every BLACS
        # step silently seized the raster and "Return to local control" could
        # never hold (2026-08-07 incident). Ownership now changes only when a
        # human changes it: start_raster, arm_raster, disarm_raster, or
        # take_local_control.

        if not active:
            self.error_signal.emit("Raster step requested but raster is not active.")
            return None

        if continuous:
            self.error_signal.emit("Raster is running in continuous mode; step is disabled.")
            return None

        if refused_remote:
            self.error_signal.emit(
                "BLACS owns the raster; press 'Return to local control' before stepping locally.")
            return None

        if held_by_operator:
            # Silent by design: the queue is running and this is the normal
            # state while the operator hand-drives. The handler replies SUCCESS.
            return None

        if pt is None:
            # path exhausted
            self._finish_raster()
            return None

        x, y = pt
        reply_q = queue.Queue(maxsize=1) if wait else None

        cmd = MotorCommand(
            cmd_type=CommandType.MOVE_TARGET,
            payload={"target_xy": (float(x), float(y))},
            source=source,            # keep initiator ("ui" or "zmq")
            tag="raster_step",        # critical
            priority=100,
            reply_q=reply_q,
        )
        self._enqueue(cmd)

        if wait:
            return self._wait_reply(reply_q, cmd.cmd_id, timeout_s)
        return None

    def raster_point_meta(self, res: Optional[MotorResult] = None) -> Dict[str, Any]:
        """Provenance for the point just stepped to: which point on the path,
        the target-space coords it was commanded to, and the calibration needed
        to interpret those coords. Rides on the move_to_next reply; BLACS writes
        it into the shot h5, so keep it small (6 calibration floats + 3 scalars).

        `res` is the step's MotorResult: its target_xy is the exact commanded
        point, immune to the cursor moving between the step and this read.
        """
        with self._state_lock:
            i = self._raster_index - 1
            total = len(self._raster_path_pts)
            pt = self._raster_path_pts[i] if 0 <= i < total else None
            cal = self.calibration
            txy = self._last_target_xy
        # Explicit frame for target_xy: calibrated target space is pixels;
        # UNCALIBRATED is a straight passthrough where target coords ARE motor
        # mm (_execute: `if cal is None: motor_xy = target_xy`). Without this
        # key the two are indistinguishable in the shot h5.
        meta: Dict[str, Any] = {
            "point_index": i,
            "path_len": total,
            "frame": "pixel" if cal is not None else "motor",
        }
        if i < 0:
            # Armed but never stepped: point_index -1 is the honest value
            # ("not on a path point yet"). The coordinate reported is the
            # laser's ACTUAL cached position -- NOT path point 0, which
            # nothing has moved to. Point-0 coords here would be plausible
            # and wrong in the shot h5, the worst kind of record.
            pt = txy
        xy = (res.target_xy if res is not None and res.target_xy else pt)
        if xy is not None:
            meta["target_xy"] = [float(xy[0]), float(xy[1])]
        if cal is not None:
            meta.update(cal.to_json())
        return meta

    def armed_path_points(self) -> List[TargetXY]:
        """A copy of the armed path, for the UI to draw. The UI must render
        THIS, never its own preview cache: the cache freezes while armed and
        was what made the display disagree with the running path (2026-08-07)."""
        with self._state_lock:
            return list(self._raster_path_pts)

    def armed_pattern_len(self) -> int:
        """BeamBox: points in the armed pattern as drawn, before the
        motor-travel filter and before ping-pong / repeat expansion (0 when
        nothing is armed). Compare the on-screen pattern with this;
        armed_path_points() is shorter by the dropped points and 2x long with
        ping-pong."""
        with self._state_lock:
            return self._raster_pattern_len

    def take_local_control(self) -> bool:
        """Operator takes the raster back from BLACS ("Return to local control").

        Binding: ownership is a persistent flag no step ever rewrites (see
        raster_step), so this holds until a human changes it again. Returns
        False and touches nothing when no raster is active.
        """
        with self._state_lock:
            if not self._raster_active:
                return False
            self._raster_source = "local"
        self.raster_source_signal.emit("local")
        return True

    def give_remote_control(self) -> bool:
        """Operator hands an armed raster to BLACS from the GUI side -- the
        mirror of take_local_control, so the Control axis is settable from
        either screen. Returns False and touches nothing when no raster is
        active (there is nothing to hand over).

        Handing over mid-continuous CONVERTS the run to step mode: BLACS drives
        point-by-point, so the free-running chain must stop or the two would
        both advance the cursor. Clearing _raster_continuous under the same lock
        that flips ownership stops the chain after the in-flight point
        (_on_command_done re-reads the flag before re-enqueueing) -- the raster
        stays active and the cursor stays mid-path, so BLACS resumes where the
        operator left off rather than at point 0. The remote side may never do
        this conversion itself; only a human at the GUI can (arm_raster refuses
        it with raster_in_continuous_mode).
        """
        with self._state_lock:
            if not self._raster_active:
                return False
            self._raster_source = "remote"
            converted = self._raster_continuous
            self._raster_continuous = False
            n = len(self._raster_path_pts)
            # Cursor = index of the next point; raster_step wraps it, so mirror
            # that here instead of reporting an off-the-end "n+1/n".
            nxt = (self._raster_index % n) + 1 if n else 0
        self.raster_source_signal.emit("remote")
        if converted:
            self.status_signal.emit(
                f"Continuous run stopped - BLACS drives step-by-step "
                f"from point {nxt}/{n}."
            )
        return True

    def stop_raster(self) -> None:
        with self._state_lock:
            was_active = self._raster_active
            # _raster_source is NOT cleared: it is the ownership flag, not path
            # state. It must outlive the path so a move_to_next arriving with
            # nothing armed can still tell that the operator holds control and
            # fire in place rather than failing the shot.
            self._raster_active = False
            self._raster_path_pts = []
            self._raster_repeat = False
            self._raster_pattern_len = 0
            self._raster_index = 0
            self._raster_selected_index = -1
            self._raster_continuous = False

        if was_active:
            self.status_signal.emit("Raster stopped.")
            self.raster_state_signal.emit(False)
            self.selection_changed_signal.emit(-1, 0.0, 0.0)
            self._flush_raster_log()

    # ------------------------------------------------------------------
    # Path-point selection + go-to-arbitrary-site (F2)
    #
    # Selection NEVER moves motors -- it only highlights a point. Motion
    # happens solely through request_go_to_path_index / goto_selected_point
    # (the UI's explicit "Move to selected" button). All operate on the armed
    # path (_raster_path_pts, populated by start_raster).
    # ------------------------------------------------------------------

    @property
    def is_continuous(self) -> bool:
        """True only while a continuous raster run is in progress. Read this
        (not the UI checkbox) when gating goto -- the controller is the source
        of truth for the run state."""
        with self._state_lock:
            return self._raster_active and self._raster_continuous

    def select_path_index(self, n: int) -> Optional[TargetXY]:
        """Select path point n (NO motion). Clamps to range; emits
        selection_changed_signal. Returns the selected (x, y), or None if no
        path is loaded."""
        with self._state_lock:
            pts = self._raster_path_pts
            if not pts:
                return None
            i = max(0, min(int(n), len(pts) - 1))
            self._raster_selected_index = i
            x, y = pts[i]
        self.selection_changed_signal.emit(i, float(x), float(y))
        return (float(x), float(y))

    def select_nearest_path_point(self, x: float, y: float) -> Optional[TargetXY]:
        """Select the path point nearest to (x, y) (NO motion). Ties resolve to
        the lowest index. Emits selection_changed_signal. Returns the selected
        (x, y), or None if no path is loaded."""
        with self._state_lock:
            pts = self._raster_path_pts
            if not pts:
                return None
            best_i = 0
            best_d = None
            for i, (px, py) in enumerate(pts):
                d = (px - x) ** 2 + (py - y) ** 2
                if best_d is None or d < best_d:
                    best_d = d
                    best_i = i
            self._raster_selected_index = best_i
            sx, sy = pts[best_i]
        self.selection_changed_signal.emit(best_i, float(sx), float(sy))
        return (float(sx), float(sy))

    def request_go_to_path_index(self, n: int, *, source: str = "ui") -> bool:
        """Move to path point n -- the ONLY selection method that moves motors.
        Sets the cursor to n+1 so a subsequent Step/Continuous resumes AFTER the
        visited site. Rejected if no path is loaded, or while a continuous run
        is in progress (the run-loop owns the cursor). Returns True if a move
        was enqueued.

        A non-zmq goto on a BLACS-owned raster TAKES local control (unlike
        raster_step, which refuses): a targeted operator move to a named site is
        unambiguous intent, not an accidental override. BLACS reclaims ownership
        on its next move_to_next, which resumes from the cursor set here.
        """
        with self._state_lock:
            pts = self._raster_path_pts
            if not pts:
                return False
            if self._raster_active and self._raster_continuous:
                return False
            i = max(0, min(int(n), len(pts) - 1))
            x, y = pts[i]
            self._raster_selected_index = i
            self._raster_index = i + 1
            took_over = (self._raster_active and source != "zmq"
                         and self._raster_source == "remote")
            if took_over:
                self._raster_source = "local"
        # motion + emits happen AFTER releasing the lock (move uses the existing
        # request_move_target -> MOVE_TARGET path, which inherits bounds checks)
        if took_over:
            self.raster_source_signal.emit("local")
        self.request_move_target(float(x), float(y), source=source)
        self.selection_changed_signal.emit(i, float(x), float(y))
        return True

    def goto_selected_point(self, *, source: str = "ui") -> bool:
        """Move to the currently-selected path point (the UI 'Move to selected'
        button). Returns False if nothing is selected."""
        with self._state_lock:
            sel = self._raster_selected_index
        if sel < 0:
            self.error_signal.emit("No path point selected.")
            return False
        return self.request_go_to_path_index(sel, source=source)

    # ------------------------------------------------------------------
    # Target-space bounds (the Automatic-Controls "Display Bounds" box).
    # _execute already enforces self.target_bounds on the calibrated
    # MOVE_TARGET path; the UI just never set it. Wiring _display_bounds to
    # set_target_bounds() is what makes the drawn box actually clamp motion --
    # raster steps, manual moves, and go-to-site all inherit the check.
    # ------------------------------------------------------------------

    def set_target_bounds(self, bounds: Optional[Tuple[float, float, float, float]]) -> None:
        """Set/replace the target-space bounds (xmin, xmax, ymin, ymax) enforced
        on calibrated MOVE_TARGET commands. Pass None to clear."""
        with self._state_lock:
            self.target_bounds = tuple(float(b) for b in bounds) if bounds is not None else None

    def clear_target_bounds(self) -> None:
        """Remove target-space bounds enforcement."""
        with self._state_lock:
            self.target_bounds = None

    # --- BLACS link: the menu server (BBX-190) ---

    def start_menu_server(self) -> None:
        """Serve BLACS on the rastering menu's port (REP, tcp://*:55535; bbx_zmq/menus/rastering.py)
        from a daemon thread. The handlers: RasterMenuLink."""
        if self._menu_thread and self._menu_thread.is_alive():
            self.status_signal.emit("BLACS link already running.")
            return
        self._menu_stop_evt.clear()
        self._menu_thread = threading.Thread(target=self._menu_loop, name="blacs-menu-server", daemon=True)
        self._menu_thread.start()

    def stop_menu_server(self) -> None:
        self._menu_stop_evt.set()

    def _menu_loop(self) -> None:
        try:
            transport = ZmqRepTransport(f"tcp://*:{MENU.port}", recv_timeout_ms=250)
        except Exception as e:   # port taken, no pyzmq: the GUI runs without BLACS
            self.error_signal.emit(f"BLACS link not started, port {MENU.port}: {e}")
            return
        server = RasterMenuLink(self).menu_server(transport)
        print(f"[BLACS link] menu {MENU.name} v{MENU.version} on port {MENU.port}.")
        self.status_signal.emit(f"BLACS link: menu {MENU.name} v{MENU.version} on port {MENU.port}.")
        failures = 0
        while not self._menu_stop_evt.is_set():
            try:
                server.serve_once(timeout_ms=250)
                failures = 0
            except Exception:    # handler errors are answered; this is the socket
                import traceback
                traceback.print_exc()
                failures += 1
                if failures == 3:
                    self.error_signal.emit("BLACS link: the socket keeps failing; see the console.")
                if failures >= 3:
                    self._menu_stop_evt.wait(0.25)
        try:
            transport.close()
        except Exception:
            pass

    # -------------------------
    # Internal helpers
    # -------------------------

    def _wait_reply(self, reply_q: "queue.Queue[MotorResult]", cmd_id: str, timeout_s: float) -> MotorResult:
        """
        Block until reply_q produces a MotorResult or `timeout_s` fires.

        When called from the Qt GUI thread, drives QApplication.processEvents()
        while waiting so paint / status / timer events keep firing -- otherwise
        a long backlash read (e.g. waiting behind a Device Home for ~45s)
        freezes the UI completely and risks a Windows "Not Responding" prompt.

        User input events (mouse / keyboard) are deliberately excluded during
        the wait so the user can't re-fire a button handler mid-wait
        (re-entrancy: a second click during processEvents would push a new
        invocation onto the call stack while the first is still mid-Save).
        Input events queue and dispatch once the wait completes.

        Non-GUI callers (motor worker thread, ZMQ daemon thread, or anything
        running before QApplication.exec_()) take the plain blocking-get
        branch -- they don't have a Qt event loop to drive and processEvents
        would error.
        """
        from PyQt5.QtCore import QThread, QEventLoop
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance()
        is_gui_thread = app is not None and QThread.currentThread() is app.thread()

        if not is_gui_thread:
            try:
                return reply_q.get(timeout=float(timeout_s))
            except queue.Empty:
                return MotorResult(ok=False, message="timeout waiting for result", cmd_id=cmd_id)

        # GUI thread: poll the queue in short slices, processing non-input
        # events between polls. 20 ms poll + 30 ms processEvents budget gives
        # ~33 FPS repaint opportunity while burning minimal CPU.
        deadline = time.monotonic() + float(timeout_s)
        poll_s = 0.02
        process_budget_ms = 30
        while True:
            try:
                return reply_q.get(timeout=poll_s)
            except queue.Empty:
                app.processEvents(QEventLoop.ExcludeUserInputEvents, process_budget_ms)
                if time.monotonic() >= deadline:
                    return MotorResult(ok=False, message="timeout waiting for result", cmd_id=cmd_id)

    def _enqueue(self, cmd: MotorCommand) -> None:
        # next(self._q_seq) -- not created_ts -- is the tiebreaker: created_ts
        # (time.time()) is not unique across rapid successive enqueues, so it
        # could let the heap fall through to comparing MotorCommand objects.
        self._q.put((cmd.priority, next(self._q_seq), cmd))

    def _within_bounds(self, xy: Tuple[float, float], bounds: Optional[Tuple[float, float, float, float]]) -> bool:
        if bounds is None:
            return True
        x, y = xy
        xmin, xmax, ymin, ymax = bounds
        return (xmin <= x <= xmax) and (ymin <= y <= ymax)

    def _target_to_motor_clamped(self, cal, xy: Tuple[float, float]) -> Tuple[float, float]:
        """Map a target-space point to edge-clamped motor coords.

        Single source of truth for target->motor: the arm-time reachability
        filter and the per-move bounds gate must agree exactly, or the filter
        admits points the gate then refuses.
        """
        mx, my = cal.target_to_motor(float(xy[0]), float(xy[1]))
        return clamp_to_bounds((float(mx), float(my)), self.motor_bounds)

    def _telemetry_loop(self) -> None:
        """
        Periodically request READ_POS. This keeps UI labels current without
        any thread other than motor-io calling into the DLL.
        """
        while not self._stop_evt.is_set():
            time.sleep(self._telemetry_period_s)
            cmd = MotorCommand(cmd_type=CommandType.READ_POS, source="internal", tag="telemetry", priority=200)
            self._enqueue(cmd)

    def _motor_worker_loop(self) -> None:
        """
        Single-owner motor I/O loop. This is the ONLY place motor DLL calls occur.
        """
        while not self._stop_evt.is_set():
            try:
                _, _, cmd = self._q.get(timeout=0.25)
            except queue.Empty:
                continue

            if cmd.cmd_type == CommandType.NOOP:
                continue

            if cmd.cmd_type == CommandType.STOP:
                res = self._execute_stop(cmd)
                self._deliver_result(cmd, res)
                continue

            # Announce start for slow blocking commands so the user gets progress
            # feedback (motor home and motor move can take seconds).
            if cmd.tag in self._LOGGABLE_START_TAGS:
                self.status_signal.emit(self._format_start_message(cmd))

            try:
                res = self._execute(cmd)
            except Exception as e:
                res = MotorResult(ok=False, message=f"Unhandled motor error: {e}", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag)

            self._deliver_result(cmd, res)

    def _execute_stop(self, cmd: MotorCommand) -> MotorResult:
        reason = str(cmd.payload.get("reason", ""))
        # Drain queue quickly so STOP takes effect
        self._drain_queue()
        # Attempt to stop motors if supported
        try:
            if hasattr(self.motor_x, "stop"):
                self.motor_x.stop()
            if hasattr(self.motor_y, "stop"):
                self.motor_y.stop()
            return MotorResult(ok=True, message=f"Stop executed ({reason})", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag)
        except Exception as e:
            return MotorResult(ok=False, message=f"Stop error: {e}", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag)

    def _drain_queue(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            return

    def _execute(self, cmd: MotorCommand) -> MotorResult:
        # Helper: read motor positions (motor space)
        def read_motor_xy() -> MotorXY:
            mx = float(self.motor_x.get_position())
            my = float(self.motor_y.get_position())
            return mx, my

        def _read_and_cache_position() -> Tuple[MotorXY, TargetXY]:
            """Read motor positions and update both motor and target caches."""
            mx, my = read_motor_xy()
            with self._state_lock:
                self._last_motor_xy = (mx, my)
                cal = self.calibration
            if cal is not None:
                tx, ty = cal.motor_to_target(mx, my)
            else:
                tx, ty = mx, my
            with self._state_lock:
                self._last_target_xy = (tx, ty)
            return (mx, my), (tx, ty)

        if cmd.cmd_type == CommandType.READ_POS:
            motor_xy, target_xy = _read_and_cache_position()
            return MotorResult(
                ok=True,
                message="read_pos",
                cmd_id=cmd.cmd_id,
                source=cmd.source,
                tag=cmd.tag,
                motor_xy=motor_xy,
                target_xy=target_xy,
            )

        # Home / backlash do not require calibration
        if cmd.cmd_type in (CommandType.HOME_SOFT_X, CommandType.HOME_HARD_X):
            fn = getattr(self.motor_x, "hard_home" if cmd.cmd_type == CommandType.HOME_HARD_X else "soft_home", None)
            if fn is None:
                return MotorResult(ok=False, message="Motor X does not support home", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag)
            fn()
            motor_xy, target_xy = _read_and_cache_position()
            return MotorResult(ok=True, message="home_x complete", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag, motor_xy=motor_xy, target_xy=target_xy)

        if cmd.cmd_type in (CommandType.HOME_SOFT_Y, CommandType.HOME_HARD_Y):
            fn = getattr(self.motor_y, "hard_home" if cmd.cmd_type == CommandType.HOME_HARD_Y else "soft_home", None)
            if fn is None:
                return MotorResult(ok=False, message="Motor Y does not support home", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag)
            fn()
            motor_xy, target_xy = _read_and_cache_position()
            return MotorResult(ok=True, message="home_y complete", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag, motor_xy=motor_xy, target_xy=target_xy)

        if cmd.cmd_type in (CommandType.SET_BACKLASH_X, CommandType.SET_BACKLASH_Y):
            axis = "X" if cmd.cmd_type == CommandType.SET_BACKLASH_X else "Y"
            value = float(cmd.payload.get("value", 0.0))
            motor = self.motor_x if axis == "X" else self.motor_y
            fn = getattr(motor, "set_backlash", None)
            if fn is None:
                return MotorResult(ok=False, message=f"Motor {axis} does not support backlash", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag)
            rb = fn(value)
            # set_backlash returns the motor's post-set read-back (KCube does
            # SetBacklash then GetBacklash; base/sim echo the value). Report
            # THAT -- not the requested value -- so the ack and the Reading
            # label show what the motor actually accepted (clipping included),
            # carried in MotorResult.value for backlash_reading_signal.
            confirmed = float(rb) if rb is not None else float(value)
            return MotorResult(ok=True, message=f"backlash {axis} set to {confirmed}", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag, value=confirmed)

        if cmd.cmd_type in (CommandType.GET_BACKLASH_X, CommandType.GET_BACKLASH_Y):
            axis = "X" if cmd.cmd_type == CommandType.GET_BACKLASH_X else "Y"
            motor = self.motor_x if axis == "X" else self.motor_y
            fn = getattr(motor, "get_backlash", None)
            if fn is None:
                return MotorResult(ok=False, message=f"Motor {axis} does not support get_backlash", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag)
            try:
                v = float(fn())
            except Exception as e:
                return MotorResult(ok=False, message=f"get_backlash {axis} failed: {e}", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag)
            return MotorResult(ok=True, message=f"backlash {axis} = {v}", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag, value=v)

        # Single-axis motor-space move (live read of the un-moved axis).
        if cmd.cmd_type in (CommandType.MOVE_MOTOR_X_ONLY, CommandType.MOVE_MOTOR_Y_ONLY):
            live_x, live_y = read_motor_xy()
            if cmd.cmd_type == CommandType.MOVE_MOTOR_X_ONLY:
                motor_xy = (float(cmd.payload["x"]), float(live_y))
            else:
                motor_xy = (float(live_x), float(cmd.payload["y"]))

            if not self._within_bounds(motor_xy, self.motor_bounds):
                return MotorResult(
                    ok=False,
                    message=out_of_bounds_message(self.motor_bounds, motor_xy),
                    cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag,
                    motor_xy=motor_xy,
                )

            # Only command the axis that's actually changing -- the other axis
            # was just read live, so passing it back through move_to would be
            # a redundant no-op-ish call to the Kinesis SDK.
            if cmd.cmd_type == CommandType.MOVE_MOTOR_X_ONLY:
                self.motor_x.move_to(motor_xy[0])
            else:
                self.motor_y.move_to(motor_xy[1])

            mx2, my2 = read_motor_xy()
            with self._state_lock:
                self._last_motor_xy = (mx2, my2)
                cal = self.calibration
            target_xy = cal.motor_to_target(mx2, my2) if cal is not None else (mx2, my2)
            with self._state_lock:
                self._last_target_xy = target_xy

            return MotorResult(
                ok=True, message="Move complete",
                cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag,
                target_xy=target_xy, motor_xy=(mx2, my2),
            )

        # Motor-space motion commands (bypass calibration; manual UI uses these)
        if cmd.cmd_type in (CommandType.MOVE_MOTOR, CommandType.JOG_MOTOR):
            if cmd.cmd_type == CommandType.MOVE_MOTOR:
                mx_t, my_t = cmd.payload["motor_xy"]
            else:
                with self._state_lock:
                    last_motor = self._last_motor_xy
                if last_motor is None:
                    return MotorResult(ok=False, message="No cached motor position for JOG_MOTOR", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag)
                dmx, dmy = cmd.payload["delta_motor"]
                mx_t, my_t = float(last_motor[0]) + float(dmx), float(last_motor[1]) + float(dmy)

            motor_xy = (float(mx_t), float(my_t))
            if not self._within_bounds(motor_xy, self.motor_bounds):
                return MotorResult(
                    ok=False,
                    message=out_of_bounds_message(self.motor_bounds, motor_xy),
                    cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag,
                    motor_xy=motor_xy,
                )

            self.motor_x.move_to(motor_xy[0])
            self.motor_y.move_to(motor_xy[1])

            mx2, my2 = read_motor_xy()
            with self._state_lock:
                self._last_motor_xy = (mx2, my2)
                cal = self.calibration
            target_xy = cal.motor_to_target(mx2, my2) if cal is not None else (mx2, my2)
            with self._state_lock:
                self._last_target_xy = target_xy

            return MotorResult(
                ok=True, message="Move complete",
                cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag,
                target_xy=target_xy, motor_xy=(mx2, my2),
            )

        # Motion commands (calibrated OR uncalibrated passthrough)
        with self._state_lock:
            cal = self.calibration

        # Determine target point
        if cmd.cmd_type == CommandType.MOVE_TARGET:
            tx, ty = cmd.payload["target_xy"]

        elif cmd.cmd_type in (CommandType.MOVE_X_ONLY, CommandType.MOVE_Y_ONLY,
                              CommandType.JOG_TARGET):
            # Base coordinate comes from a FRESH motor read, never the
            # last-commanded cache: target->motor is cross-coupled, so a
            # stale partner recomputes BOTH motor coords and physically
            # drags the other axis on a "single-axis" move (2026-08-04
            # first-move bug). Also replaces the old telemetry fallback
            # that used motor-space coords as target-space ones.
            _, here = _read_and_cache_position()
            if cmd.cmd_type == CommandType.MOVE_X_ONLY:
                tx, ty = float(cmd.payload["x"]), float(here[1])
            elif cmd.cmd_type == CommandType.MOVE_Y_ONLY:
                tx, ty = float(here[0]), float(cmd.payload["y"])
            else:
                dx, dy = cmd.payload["delta_xy"]
                tx, ty = float(here[0]) + float(dx), float(here[1]) + float(dy)

        else:
            return MotorResult(ok=False, message=f"Unsupported command {cmd.cmd_type}", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag)

        target_xy = (float(tx), float(ty))

        if cal is None:
            # Uncalibrated passthrough: interpret target coords as motor coords
            motor_xy = target_xy

            # Do NOT apply target_bounds here (they're image/target-space bounds)
            if not self._within_bounds(motor_xy, self.motor_bounds):
                return MotorResult(
                    ok=False,
                    message=out_of_bounds_message(
                        self.motor_bounds, motor_xy, target_xy),
                    cmd_id=cmd.cmd_id,
                    source=cmd.source,
                    tag=cmd.tag,
                    target_xy=target_xy,
                    motor_xy=motor_xy,
                )
        else:
            # Calibrated: enforce target bounds, map, then enforce motor bounds
            if not self._within_bounds(target_xy, self.target_bounds):
                return MotorResult(
                    ok=False,
                    message="Rejected: target out of bounds",
                    cmd_id=cmd.cmd_id,
                    source=cmd.source,
                    tag=cmd.tag,
                    target_xy=target_xy,
                )

            motor_xy = self._target_to_motor_clamped(cal, target_xy)

            if not self._within_bounds(motor_xy, self.motor_bounds):
                return MotorResult(
                    ok=False,
                    message=out_of_bounds_message(
                        self.motor_bounds, motor_xy, target_xy),
                    cmd_id=cmd.cmd_id,
                    source=cmd.source,
                    tag=cmd.tag,
                    target_xy=target_xy,
                    motor_xy=motor_xy,
                )

        # Execute moves (sequential for now). Driver should include its own timeout.
        # NOTE: Your original RasterManager moves A then B. We keep that behavior for now.
        self.motor_x.move_to(motor_xy[0])
        self.motor_y.move_to(motor_xy[1])

        # Read back
        mx2, my2 = read_motor_xy()
        with self._state_lock:
            self._last_motor_xy = (mx2, my2)
            self._last_target_xy = target_xy

        # Raster logging (only for raster steps, and only when a per-pass log
        # file is armed: with wrap the pattern repeats indefinitely, so an
        # ungated append would grow this list for the process lifetime)
        if cmd.tag == "raster_step" and self._raster_log_path is not None:
            self._raster_log.append({
                "timestamp": time.time(),
                "x": target_xy[0],
                "y": target_xy[1],
                "mx": mx2,
                "my": my2,
                "initiator": cmd.source,   # "ui" | "zmq" | "raster" | etc.
            })

        return MotorResult(ok=True, message="Move complete", cmd_id=cmd.cmd_id, source=cmd.source, tag=cmd.tag, target_xy=target_xy, motor_xy=(mx2, my2))

    # Tags whose successful completion should be logged. Excludes telemetry/read_pos
    # (too chatty) and raster_step (one per raster point).
    _LOGGABLE_SUCCESS_TAGS = {
        "home_X_soft", "home_X_hard", "home_Y_soft", "home_Y_hard",
        "move_target", "move_motor", "move_x", "move_y",
        # Single-axis motor moves (User Home Go X/Y). Blocking like
        # move_motor -> same start+success acks; absent here a successful
        # Go was completely silent and looked broken.
        "move_motor_x_only", "move_motor_y_only",
        # User-initiated STOP: instantaneous (no start line needed) but
        # its success must still be confirmed in the log.
        "stop",
        "jog", "jog_motor",
        # Set-backlash is async (motor thread); without these tags the
        # success reply is silently dropped and the user gets no ack in the
        # log. get_backlash_* deliberately stays OUT -- every Set re-reads
        # and startup populates both axes, so logging gets would multi-log.
        "backlash_X", "backlash_Y",
    }

    # Tags whose START should also be logged. Home and move are blocking on the
    # motor thread (can take seconds); a "starting" line gives progress feedback.
    # Jog is included with a terse axis-only label so rapid clicks stay readable.
    _LOGGABLE_START_TAGS = {
        "home_X_soft", "home_X_hard", "home_Y_soft", "home_Y_hard",
        "move_target", "move_motor", "move_x", "move_y",
        "move_motor_x_only", "move_motor_y_only",
        "jog", "jog_motor",
    }

    def _deliver_result(self, cmd: MotorCommand, res: MotorResult) -> None:
        # Sync reply (ZMQ wait mode)
        if cmd.reply_q is not None:
            try:
                cmd.reply_q.put_nowait(res)
            except Exception:
                pass

        # Emit signals to UI (Qt will queue cross-thread automatically)
        self.command_done_signal.emit(res.cmd_id, res.ok, res.message, res.tag)

        if res.motor_xy is not None:
            self.motor_position_signal.emit(res.motor_xy[0], res.motor_xy[1])
        if res.target_xy is not None:
            self.target_position_signal.emit(res.target_xy[0], res.target_xy[1])

        # Async backlash read-back -> Reading label, with NO GUI-thread wait
        # on the motor FIFO. The SET reply carries the motor's post-set value
        # (Option C); a standalone GET reply carries a fresh read. Both tags
        # end in the axis letter, so split("_")[-1] yields "X"/"Y". Keyed on
        # tag (not source) by design: the label reflects true motor state
        # regardless of initiator (ui / zmq / cal-load).
        if res.ok and res.value is not None and res.tag in ("backlash_X", "backlash_Y", "get_backlash_X", "get_backlash_Y"):
            self.backlash_reading_signal.emit(res.tag.split("_")[-1], float(res.value))

        if not res.ok and res.message:
            self.status_signal.emit(f"[{res.source}] {res.message}")
        elif res.ok and res.tag in self._LOGGABLE_SUCCESS_TAGS:
            self.status_signal.emit(self._format_success_message(res))

    @staticmethod
    def _format_start_message(cmd: MotorCommand) -> str:
        tag = cmd.tag
        payload = cmd.payload
        if tag.startswith("home_"):
            parts = tag.split("_")
            axis = parts[1] if len(parts) > 1 else "?"
            kind = parts[2] if len(parts) > 2 else "?"
            return f"{kind.capitalize()} home: {axis} starting..."
        if tag in ("move_target", "move_motor"):
            mxy = payload.get("motor_xy")
            if mxy is not None:
                return f"Move starting: target motor=({float(mxy[0]):.5f}, {float(mxy[1]):.5f})"
            txy = payload.get("target_xy")
            if txy is not None:
                return f"Move starting: target=({float(txy[0]):.5f}, {float(txy[1]):.5f})"
            return "Move starting..."
        if tag in ("move_x", "move_motor_x_only"):
            v = payload.get("x")
            return f"Move starting (x={float(v):.5f})..." if v is not None else "Move starting (x)..."
        if tag in ("move_y", "move_motor_y_only"):
            v = payload.get("y")
            return f"Move starting (y={float(v):.5f})..." if v is not None else "Move starting (y)..."
        if tag in ("jog", "jog_motor"):
            delta = payload.get("delta_motor") or payload.get("delta_xy")
            if delta is not None:
                dx, dy = float(delta[0]), float(delta[1])
                if dx != 0 and dy == 0:
                    return f"Jogging X{'+' if dx > 0 else '-'} {abs(dx):.5f}..."
                if dy != 0 and dx == 0:
                    return f"Jogging Y{'+' if dy > 0 else '-'} {abs(dy):.5f}..."
            return "Jogging..."
        return f"{tag} starting..."

    @staticmethod
    def _format_success_message(res: MotorResult) -> str:
        tag = res.tag
        mxy = res.motor_xy
        if tag.startswith("home_"):
            # tag format: home_X_soft / home_Y_hard
            parts = tag.split("_")
            axis = parts[1] if len(parts) > 1 else "?"
            kind = parts[2] if len(parts) > 2 else "?"
            if mxy is not None:
                return f"{kind.capitalize()} home complete: {axis} (motor=({mxy[0]:.5f}, {mxy[1]:.5f}))"
            return f"{kind.capitalize()} home complete: {axis}"
        if tag in ("move_target", "move_motor"):
            if mxy is not None:
                return f"Move complete: motor=({mxy[0]:.5f}, {mxy[1]:.5f})"
            return "Move complete."
        if tag in ("move_x", "move_y", "move_motor_x_only", "move_motor_y_only"):
            axis = "x" if tag in ("move_x", "move_motor_x_only") else "y"
            if mxy is not None:
                return f"Move complete ({axis}): motor=({mxy[0]:.5f}, {mxy[1]:.5f})"
            return f"Move complete ({axis})."
        if tag == "stop":
            return res.message or "Stopped."
        if tag in ("jog", "jog_motor"):
            if mxy is not None:
                return f"Jog complete: motor=({mxy[0]:.5f}, {mxy[1]:.5f})"
            return "Jog complete."
        if tag in ("backlash_X", "backlash_Y"):
            # Worker message is the hardware-confirmed value
            # ("backlash X set to <v>"); surface it verbatim.
            return res.message or f"Backlash {tag.split('_')[-1]} set."
        return f"{tag} complete."

    # Raster chaining runs in Qt thread
    @pyqtSlot(str, bool, str, str)
    def _on_command_done(self, cmd_id: str, ok: bool, message: str, tag: str) -> None:
        # Only chain raster in continuous mode, and only after a raster step finishes.
        if tag != "raster_step":
            return

        with self._state_lock:
            active = self._raster_active
            continuous = self._raster_continuous
            delay_s = float(getattr(self, "_raster_delay_s", 0.0))

        if not active or not continuous:
            return

        if not ok:
            self.status_signal.emit("Raster halted due to an error.")
            self.stop_raster()
            return

        if delay_s > 0:
            QTimer.singleShot(int(delay_s * 1000), self._enqueue_next_raster_point)
        else:
            self._enqueue_next_raster_point()



    def _enqueue_next_raster_point(self) -> None:
        # Hold lock through active check AND iterator consumption to prevent
        # race where stop_raster() runs between the check and next(it),
        # especially when called from a QTimer.singleShot() delay.
        #
        # The continuous re-check is what makes that delayed call safe against
        # a hand-over: give_remote_control converts a running sweep to step
        # mode, leaving a state this path never had to consider before (active
        # but no longer continuous). An in-flight delay timer would otherwise
        # steal one more point past the one the status line promised -- and if
        # it fired in the gap after the pass's last point, the no-wrap read
        # below returns None and _finish_raster() tears the raster down, so
        # BLACS's next move_to_next would hit raster_not_active under remote
        # ownership and sticky-pause the queue. Every caller is continuous-only
        # with the flag already True, so this only ever fires on the stale
        # timer (and on the zero-delay path, where _on_command_done reads the
        # flag under lock but calls out here without it).
        with self._state_lock:
            active = self._raster_active
            if not active or not self._raster_continuous:
                return
            pt = self._next_raster_point_locked(
                wrap=getattr(self, "_raster_repeat", False))
            if pt is not None:
                x, y = pt
                cmd = MotorCommand(
                    cmd_type=CommandType.MOVE_TARGET,
                    payload={"target_xy": (float(x), float(y))},
                    source="raster",
                    tag="raster_step",
                    priority=100,
                )
                self._enqueue(cmd)
                return
        # exhausted — finish outside the lock (emits signals)
        self._finish_raster()

    def _finish_raster(self) -> None:
        with self._state_lock:
            # _raster_source is NOT cleared: path exhaustion is a machine
            # event, and ownership changes only when a human changes it --
            # same contract as stop_raster. Clearing here silently dropped
            # the operator's hold the moment their pattern ran out.
            self._raster_active = False
            self._raster_continuous = False
            self._raster_path_pts = []
            self._raster_repeat = False
            self._raster_pattern_len = 0
            self._raster_index = 0
            self._raster_selected_index = -1
        self.raster_state_signal.emit(False)
        self.raster_finished_signal.emit()
        self.status_signal.emit("Raster finished.")
        self.selection_changed_signal.emit(-1, 0.0, 0.0)
        self._flush_raster_log()

    def _flush_raster_log(self) -> None:
        path = self._raster_log_path
        if not path:
            return
        try:
            with open(path, "w") as f:
                json.dump(self._raster_log, f, indent=2)
            self.status_signal.emit(f"Raster log saved: {path}")
        except Exception as e:
            self.error_signal.emit(f"Could not write raster log: {e}")
        finally:
            self._raster_log = []
            self._raster_log_path = None

    # -------------------------
    # Shutdown
    # -------------------------

    def shutdown(self) -> None:
        """
        Stop all threads and flush logs. Call on app exit.
        """
        self.stop_menu_server()
        self._stop_evt.set()
        self._enqueue(MotorCommand(cmd_type=CommandType.NOOP, source="internal", priority=0))
        try:
            self._motor_thread.join(timeout=2.0)
        except Exception:
            pass
        if self._menu_thread is not None:
            try:
                self._menu_thread.join(timeout=0.5)
            except Exception:
                pass
        self._flush_raster_log()


# -------------------------
# Optional factory helpers
# -------------------------

def create_controller_from_config(config_obj=None) -> "SystemController":
    """
    Convenience constructor that instantiates motors from hardware.py using config.py defaults.

    This keeps SystemController itself decoupled (it still accepts motor objects),
    but gives you a one-liner in main.py.

    Usage:
        import config
        from controller import create_controller_from_config
        ctl = create_controller_from_config(config.APP_CONFIG)
    """
    # Local imports to avoid hard dependencies when unit-testing controller logic
    from hardware import KCube, KinesisOptions, SimulatedMotor

    # Late import of config to avoid circular imports
    if config_obj is None:
        try:
            import config as _cfg
            config_obj = getattr(_cfg, "APP_CONFIG", None) or _cfg
        except Exception as e:
            raise RuntimeError("No config provided and could not import config.py") from e

    # Support either dataclass-style config (APP_CONFIG.hardware.serial_x) or module constants (SERIAL_X)
    def _get(path, default=None):
        cur = config_obj
        for part in path.split("."):
            if hasattr(cur, part):
                cur = getattr(cur, part)
            elif isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    serial_x = _get("hardware.serial_x", _get("SERIAL_X"))
    serial_y = _get("hardware.serial_y", _get("SERIAL_Y"))
    if serial_x is None or serial_y is None:
        raise RuntimeError("Missing motor serials (expected hardware.serial_x/serial_y or SERIAL_X/SERIAL_Y)")

    # Simulate: config flag OR env override (RASTER_SIMULATE=1 for a no-rig
    # test launch without editing config). Explicit only -- we never auto-
    # fall back to sim on a KCube init failure, or a genuinely disconnected
    # motor on the real rig would silently no-op every move.
    simulate = bool(_get("hardware.simulate", False)) or os.environ.get(
        "RASTER_SIMULATE", "").lower() in ("1", "true", "yes")

    if simulate:
        motor_x = SimulatedMotor(serial_x, "X")
        motor_y = SimulatedMotor(serial_y, "Y")
    else:
        opts = KinesisOptions(
            kinesis_dir=_get("hardware.kinesis_dir", None),
            poll_ms=int(_get("hardware.poll_ms", 100)),
            settings_wait_ms=int(_get("hardware.settings_wait_ms", 10_000)),
            device_settings_name=str(_get("hardware.device_settings_name", "Z912")),
            verbose=bool(_get("hardware.verbose", False)),
        )
        motor_x = KCube(serial_x, "X", options=opts)
        motor_y = KCube(serial_y, "Y", options=opts)

    ctl = SystemController(
        motor_x,
        motor_y,
        calibration_path=str(_get("paths.calibration_path", _get("CALIBRATION_PATH", "calibration_data.json"))),
        target_bounds=_get("raster.target_bounds", _get("DEFAULT_TARGET_BOUNDS", None)),
        motor_bounds=_get("hardware.motor_bounds", _get("DEFAULT_MOTOR_BOUNDS", None)),
        telemetry_period_s=float(_get("telemetry.period_s", _get("TELEMETRY_PERIOD_S", 0.2))),
    )

    # BLACS link: the menu server on the rastering menu's port (config network.blacs_link)
    if _get("network.blacs_link", True):
        ctl.start_menu_server()

    return ctl
