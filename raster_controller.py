"""The rastering GUI's controller: stage, raster, calibration and the BLACS link, without a window.

The window calls SystemController from the GUI thread. No call waits for a motor: stage jobs are
queued for the motor thread and their outcome comes back as Qt signals (emitted on the motor or
BLACS thread; Qt queues them to the window). A request refused at once returns a refused Result
and also says so on `message`. Parts:
    stage.py           the motors, the motor thread, the one admission check for every move
    raster_session.py  the raster's state, path and cursor
    calibration.py     the px -> mm fit and the calibration file
    raster_paths.py    the pattern points
BLACS talks to RasterMenuLink over the menu link (bbx_zmq, menu bbx_zmq/menus/rastering.py).
"""
from __future__ import annotations

import os
import threading
import traceback
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bbx_zmq import MenuServer, Refused, ZmqRepTransport
from bbx_zmq.menus.rastering import ARMED, LOCAL, MENU, OFF, RUNNING
from PyQt5.QtCore import QObject, pyqtSignal

from calibration import (LAST_CALIBRATION_POINTER, AffineCalibration, CalibrationFile, fit_affine,
                         last_calibration_path, read_calibration_file, remember_calibration_path,
                         write_calibration_file)
from config import APP_CONFIG, SETTINGS_DEFAULTS_FILE, SIM_FOLDER, AppConfig, HardwareConfig
from raster_paths import points_from_spec
from raster_session import BLACS, GUI, RUN, RasterPlan, RasterSession, RasterSnapshot
from stage import Command, Result, Stage, StagePosition

XY = Tuple[float, float]
HERE = os.path.dirname(os.path.abspath(__file__))
_MENU_POLL_MS = 50   # the BLACS link checks for a request this often, and for its stop
_SAVE_ERRORS = (OSError, TypeError, ValueError)   # a calibration file not written (the old one stays)


@dataclass
class _Clicks:
    """A calibration being collected: clicked spots [px] and the stage positions [mm]."""
    required: int
    camera_settings: Optional[Dict[str, Any]]
    px: List[XY] = field(default_factory=list)
    mm: List[XY] = field(default_factory=list)
    asked: int = 0


def longest_stage_job_s(hw: HardwareConfig) -> float:
    """The longest one stage job can take before its KCube gives up [s]: a device home, or a move
    of both axes one after the other; plus 2 s to stop and read back."""
    return max(hw.home_timeout_s, 2 * hw.move_timeout_s) + 2.0


def _axis(axis: str) -> str:
    a = str(axis).upper().strip()
    if a not in ("X", "Y"):
        raise ValueError(f"axis must be 'X' or 'Y', not {axis!r}")
    return a


class SystemController(QObject):
    """The GUI's one controller. Call it from the GUI thread; no method waits for a motor.

    Stage jobs (move_*, jog_mm, home, go_user_home, set_backlash) return a Future of their
    Result: do not call .result() on the GUI thread; the outcome also arrives as signals.
    Raster and calibration requests are decided at once and return a Result (refused: ok False,
    code, words; the words also go out on `message`). Signals come from the motor or BLACS
    thread; connect window slots with the default (queued) connection.
    """

    message = pyqtSignal(str, str)                # kind ("info", "refused", "error"), text
    position_changed = pyqtSignal(object)         # StagePosition
    busy_changed = pyqtSignal(bool)               # the motor thread has stage jobs
    raster_changed = pyqtSignal(object)           # RasterSnapshot (can arrive out of order: see its seq)
    calibration_changed = pyqtSignal(object)      # CalibrationFile, or None: no calibration
    calibration_progress = pyqtSignal(int, int)   # spots recorded, spots needed; (0, 0): not calibrating
    backlash_changed = pyqtSignal(str, float)     # axis, what the motor reports [mm]
    user_home_changed = pyqtSignal(float, float)  # [mm]

    def __init__(self, motor_x, motor_y, *, travel_mm, calibration_path: str,
                 pointer_path: str = LAST_CALIBRATION_POINTER,
                 close_wait_s: float = longest_stage_job_s(APP_CONFIG.hardware),
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.calibration_path = calibration_path   # every calibration fit is saved here
        self.close_wait_s = close_wait_s           # close() waits this long for the stage job in flight [s]
        self._pointer_path = pointer_path
        self._lock = threading.RLock()
        self._session = RasterSession()
        self._run_timer: Optional[threading.Timer] = None   # a continuous run's delay before its next point
        self._leg = 0    # counts arms, pauses and continues: a run's point from before one takes no next step
        self._cal_file: Optional[CalibrationFile] = None
        self._clicks: Optional[_Clicks] = None
        self._user_home: XY = (0.0, 0.0)
        self._menu_thread: Optional[threading.Thread] = None
        self._menu_stop = threading.Event()
        self._link_status: Tuple[str, str] = ("", "")
        self._stage = Stage(motor_x, motor_y, travel_mm, raster_state=self._raster_state,
                            on_log=self.message.emit, on_position=self.position_changed.emit,
                            on_backlash=self.backlash_changed.emit, on_busy=self.busy_changed.emit)
        self.read_backlash()

    # --- what the window reads ---

    @property
    def travel_mm(self) -> Tuple[float, float, float, float]:
        return self._stage.travel_mm

    @property
    def calibration(self) -> Optional[AffineCalibration]:
        return self._stage.calibration

    @property
    def pointer_path(self) -> str:
        """The file that names the calibration used last."""
        return self._pointer_path

    @property
    def calibration_file(self) -> Optional[CalibrationFile]:
        with self._lock:
            return self._cal_file

    @property
    def position(self) -> Optional[StagePosition]:
        return self._stage.position

    @property
    def backlash_mm(self) -> Tuple[Optional[float], Optional[float]]:
        return self._stage.backlash_mm

    @property
    def user_home_mm(self) -> XY:
        with self._lock:
            return self._user_home

    @property
    def calibration_clicks(self) -> Tuple[int, int]:
        """(spots recorded, spots needed) while calibrating; (0, 0) when not."""
        with self._lock:
            c = self._clicks
            return (len(c.px), c.required) if c is not None else (0, 0)

    @property
    def blacs_link_status(self) -> Tuple[str, str]:
        """(kind, words) of the BLACS link's latest news, also sent on `message`: kind "info"
        (serving on its port) or "error" (not started, or its socket keeps failing); ("", "")
        before it serves on a port. The link starts before the window exists, so the window shows
        this when it opens."""
        with self._lock:
            return self._link_status

    def reachable(self, points_px: Sequence[XY]) -> List[bool]:
        """For each image point [px]: can the stage go there (calibrated and inside the travel)?"""
        return self._stage.reachable(points_px)

    def raster_snapshot(self) -> RasterSnapshot:
        with self._lock:
            return self._session.snapshot()

    # --- stage jobs: each returns a Future of its Result; never wait on it in the GUI thread ---

    def move_mm(self, x: float, y: float) -> "Future[Result]":
        return self._stage.submit(Command(f"Move to ({x:.4f}, {y:.4f}) mm", "move", mm=(float(x), float(y))))

    def move_axis_mm(self, axis: str, value: float) -> "Future[Result]":
        axis = _axis(axis)
        return self._stage.submit(Command(f"Move {axis} to {value:.4f} mm", "move", axis=axis, value=float(value)))

    def jog_mm(self, dx: float, dy: float) -> "Future[Result]":
        return self._stage.submit(Command(f"Jog ({dx:+.4f}, {dy:+.4f}) mm", "move", delta_mm=(float(dx), float(dy))))

    def move_px(self, x: float, y: float, *, from_blacs: bool = False) -> "Future[Result]":
        who = "BLACS move" if from_blacs else "Move"
        return self._stage.submit(Command(f"{who} to ({x:.1f}, {y:.1f}) px", "move",
                                          px=(float(x), float(y)), from_blacs=from_blacs))

    def home(self, axis: str) -> "Future[Result]":
        """Device home (Kinesis Home) of one axis; home X then Y by calling twice."""
        axis = _axis(axis)
        return self._stage.submit(Command(f"Device home {axis}", "home", axis=axis))

    def go_user_home(self, axis: Optional[str] = None) -> "Future[Result]":
        """Move to the user home: both axes (axis None) or one."""
        ux, uy = self.user_home_mm
        if axis is None:
            return self._stage.submit(Command(f"User home ({ux:.4f}, {uy:.4f}) mm", "move", mm=(ux, uy)))
        axis = _axis(axis)
        v = ux if axis == "X" else uy
        return self._stage.submit(Command(f"User home {axis} ({v:.4f} mm)", "move", axis=axis, value=v))

    def set_backlash(self, axis: str, value_mm: float) -> "Future[Result]":
        axis = _axis(axis)
        return self._stage.submit(Command(f"Set backlash {axis} to {value_mm:.4f} mm", "set_backlash",
                                          axis=axis, value=float(value_mm)))

    def read_backlash(self) -> None:
        """Ask both motors for their backlash; answers come on backlash_changed."""
        for axis in "XY":
            self._stage.submit(Command(f"Read backlash {axis}", "get_backlash", axis=axis, quiet=True))

    def set_user_home(self, axis: str, value_mm: float) -> None:
        """Store one axis of the user home [mm] (kept in the calibration file when saved)."""
        axis = _axis(axis)
        with self._lock:
            x, y = self._user_home
            self._user_home = (float(value_mm), y) if axis == "X" else (x, float(value_mm))
            uh = self._user_home
        self.user_home_changed.emit(*uh)
        self.message.emit("info", f"User home {axis} set to {value_mm:.4f} mm.")

    # --- the raster ---

    def set_pending_plan(self, plan: Optional[RasterPlan]) -> None:
        """The pattern set up in the Run tab: what the next arm (the window's or BLACS's) arms."""
        with self._lock:
            old = self._session.plan
            taken = self._session.set_pending(plan)
            snap = self._session.snapshot()
        self.raster_changed.emit(snap)
        if taken and plan.delay_s != old.delay_s:
            self.message.emit("info", f"The armed raster now waits {plan.delay_s:g} s per point in a "
                                      "continuous run (from its next point).")

    def arm(self, *, for_blacs: bool = False) -> Result:
        """Arm the pending plan: a continuous run starts at once; otherwise it is stepped at the
        GUI (or by BLACS with for_blacs, or when BLACS has the raster now: a re-arm leaves it with
        BLACS)."""
        try:
            with self._lock:
                words = self._arm_locked(for_blacs=for_blacs)
                snap = self._session.snapshot()
        except Refused as r:
            return self._refused(r)
        self.raster_changed.emit(snap)
        self.message.emit("info", words)
        return Result(True, words)

    def step(self) -> Result:
        """The GUI's step to the next point (raster stepped at the GUI)."""
        try:
            with self._lock:
                i, pt, wrapped, _, snap = self._send_next_locked(GUI)
        except Refused as r:
            return self._refused(r)
        self._stepped(snap, wrapped, "")
        return Result(True, px=pt)

    def go_to_point(self, index: int) -> Result:
        """Move to point `index` of the armed path; the next step continues after it. Takes the
        raster back from BLACS."""
        try:
            with self._lock:
                s = self._session
                i, pt, taken = s.go_to(index)
                self._stage.submit(Command(f"Go to raster point {i} of {len(s.path)}", "move",
                                           px=pt, raster_run=s.run))
                snap = s.snapshot()
        except Refused as r:
            return self._refused(r)
        self.raster_changed.emit(snap)
        if taken:
            self.message.emit("info", "Go to point took the raster back from BLACS: it is stepped at the GUI now.")
        return Result(True, px=pt)

    def select_point(self, index: int) -> Result:
        """Pick a point of the armed path for "go to point"; nothing moves."""
        try:
            with self._lock:
                self._session.select(index)
                snap = self._session.snapshot()
        except Refused as r:
            return self._refused(r)
        self.raster_changed.emit(snap)
        return Result(True)

    def pause_raster(self) -> Result:
        """Hold a continuous run at the GUI, at the point it is at (a move under way finishes)."""
        try:
            with self._lock:
                i = self._session.pause()
                self._cancel_run_timer_locked()
                self._leg += 1
                snap = self._session.snapshot()
        except Refused as r:
            return self._refused(r)
        self.raster_changed.emit(snap)
        self.message.emit("info", f"Run paused at point {i} of {len(snap.path)}: Continue runs on from it; "
                                  "Go to selected point picks another.")
        return Result(True)

    def continue_raster(self) -> Result:
        """Run a raster held at the GUI by itself again, from the point it is at (moved to and waited
        at again), with the armed plan's delay."""
        try:
            with self._lock:
                i, pt = self._session.resume()
                self._cancel_run_timer_locked()
                self._leg += 1
                self._queue_run_move_locked(i, pt)
                snap = self._session.snapshot()
        except Refused as r:
            return self._refused(r)
        self.raster_changed.emit(snap)
        self.message.emit("info", f"Run continues from point {i} of {len(snap.path)}.")
        return Result(True)

    def hand_to_blacs(self) -> Result:
        """BLACS steps the armed raster from where it is (a continuous run stops)."""
        try:
            with self._lock:
                was_running = self._session.hand_to_blacs()
                self._cancel_run_timer_locked()
                nxt = self._session.next_index(BLACS)
                snap = self._session.snapshot()
        except Refused as r:
            return self._refused(r)
        self.raster_changed.emit(snap)
        self.message.emit("info", ("Continuous run stopped; " if was_running else "")
                          + f"BLACS steps the raster: its next shot goes to point {nxt} of {len(snap.path)}.")
        return Result(True)

    def take_back(self) -> Result:
        """The GUI steps the raster BLACS had, from where it is."""
        with self._lock:
            was_armed = self._session.take_back(GUI)
            snap = self._session.snapshot()
        if was_armed:
            self.raster_changed.emit(snap)
            self.message.emit("info", "Raster taken back from BLACS: step it at the GUI.")
        return Result(True)

    def stop_raster(self) -> None:
        """End the raster (any state); queued raster points are dropped."""
        with self._lock:
            was = self._session.stop()
            self._cancel_run_timer_locked()
            snap = self._session.snapshot()
        if was:
            self.raster_changed.emit(snap)
            self.message.emit("info", "Raster stopped.")

    # --- BLACS (called by RasterMenuLink on the menu-server thread; raise Refused) ---

    def blacs_arm(self) -> Dict[str, int]:
        with self._lock:
            s = self._session
            if s.state == RUNNING:
                raise Refused("raster_running", "a continuous raster runs from the GUI; stop it there")
            if s.state == LOCAL:
                s.hand_to_blacs()
                words = (f"BLACS took the armed raster: its next shot goes to point {s.next_index(BLACS)} "
                         f"of {len(s.path)}.")
            elif s.state == OFF:
                words = "BLACS: " + self._arm_locked(for_blacs=True)
            else:
                words = None
            snap = s.snapshot()
        self.raster_changed.emit(snap)
        if words:
            self.message.emit("info", words)
        return {"path_len": len(snap.path), "dropped": snap.dropped}

    def blacs_disarm(self) -> bool:
        with self._lock:
            was_armed = self._session.take_back(BLACS)
            snap = self._session.snapshot()
        if was_armed:
            self.raster_changed.emit(snap)
            self.message.emit("info", "BLACS released the raster; local control.")
        return was_armed

    def blacs_next_point(self) -> Tuple[int, XY, int, bool, "Future[Result]"]:
        """(index, point [px], path length, wrapped, the move's Future)."""
        with self._lock:
            i, pt, wrapped, future, snap = self._send_next_locked(BLACS)
        self._stepped(snap, wrapped, "BLACS: ")
        return i, pt, len(snap.path), wrapped, future

    def set_shots_per_step(self, n: int) -> int:
        with self._lock:
            self._session.shots_per_step = int(n)
            snap = self._session.snapshot()
        self.raster_changed.emit(snap)
        return int(n)

    # --- calibration ---

    def start_calibration(self, required_points: int = 3, *,
                          camera_settings: Optional[Dict[str, Any]] = None) -> Result:
        """Collect `required_points` clicks, each paired with the stage position read after the
        jobs queued before it. camera_settings: the camera geometry now, stored with the fit."""
        with self._lock:
            self._clicks = _Clicks(int(required_points), camera_settings)
        self.calibration_progress.emit(0, int(required_points))
        self.message.emit("info", f"Calibration: jog the beam to {required_points} distinct spots; "
                                  "click the laser spot on the image each time.")
        return Result(True)

    def cancel_calibration(self) -> None:
        """Stop collecting calibration clicks; the calibration in use stays."""
        with self._lock:
            was, self._clicks = self._clicks, None
        if was is not None:
            self.calibration_progress.emit(0, 0)
            self.message.emit("info", "Calibration cancelled; the calibration in use is unchanged.")

    def add_calibration_click(self, x: float, y: float) -> Result:
        with self._lock:
            clicks = self._clicks
            if clicks is not None:
                clicks.asked += 1
                k = clicks.asked
        if clicks is None:
            return self._refused(Refused("not_calibrating", "press Calibrate first"))
        px = (float(x), float(y))
        self._stage.submit(Command(f"Read the stage position for calibration spot {k}", "read", quiet=True,
                                   then=lambda res: self._pair_click(clicks, px, res)))
        return Result(True)

    def load_calibration(self, path: Optional[str] = None) -> Result:
        """Load a calibration file (None: the one used last) and use it: affine, user home,
        backlash."""
        path = path or last_calibration_path(self._pointer_path)
        if not path:
            return self._refused(Refused("no_calibration_file", "no calibration used before: load a calibration file"))
        try:
            cal = read_calibration_file(path)
        except (OSError, ValueError) as e:
            return self._refused(Refused("bad_calibration_file", f"could not load {path}: {e}"))
        res = self._apply(cal)
        if res.ok:
            extra = ", with camera settings" if cal.camera_settings is not None else ""
            self.message.emit("info", f"Loaded calibration from {cal.path}{extra}.")
        return res

    def save_calibration(self, path: str, *, camera_settings: Optional[Dict[str, Any]] = None,
                         notes: Optional[str] = None) -> Result:
        """Save the calibration with the user home and the backlash last read from the motors.
        camera_settings / notes: None keeps what the calibration has."""
        refusal = None
        with self._lock:
            cal = self._cal_file
            if cal is None:
                refusal = Refused("not_calibrated", "no calibration to save: calibrate first")
            else:
                cal = replace(cal, user_home_mm=self._user_home, backlash_mm=self._stage.backlash_mm,
                              camera_settings=cal.camera_settings if camera_settings is None else camera_settings,
                              notes=cal.notes if notes is None else str(notes))
                try:
                    cal = write_calibration_file(path, cal)
                    self._cal_file = cal
                except _SAVE_ERRORS as e:
                    refusal = Refused("save_failed", f"calibration not saved to {path} (the file there is "
                                                     f"unchanged): {e}")
        if refusal is not None:
            return self._refused(refusal)
        self._remember(cal.path)
        unread = [a for a, v in zip("XY", cal.backlash_mm) if v is None]
        self.message.emit("info", f"Calibration saved to {cal.path}" + (
            f" (backlash {' and '.join(unread)} not read from the motor yet: saved empty)." if unread else "."))
        self.calibration_changed.emit(cal)
        return Result(True)

    def clear_calibration(self) -> Result:
        res = self._apply(None)
        if res.ok:
            self.message.emit("info", "Calibration cleared.")
        return res

    # --- BLACS link ---

    def start_menu_server(self, transport=None) -> None:
        """Serve BLACS from a thread: on the menu's port, or on `transport` (tests:
        bbx_zmq.InMemoryTransport)."""
        if self._menu_thread is not None and self._menu_thread.is_alive():
            return
        self._menu_stop.clear()
        self._menu_thread = threading.Thread(target=self._serve_menu, args=(transport,),
                                             name="blacs-menu", daemon=True)
        self._menu_thread.start()

    def close(self) -> None:
        """Stop the raster and the BLACS link, drop the queued stage jobs, let the one in flight
        end (up to close_wait_s), disconnect the motors."""
        self._menu_stop.set()
        self.stop_raster()
        self._stage.close(self.close_wait_s)
        if self._menu_thread is not None:
            self._menu_thread.join(1.0)

    # --- inside ---

    def _raster_state(self) -> Tuple[str, int]:
        with self._lock:
            return self._session.state, self._session.run

    def _refused(self, r: Refused) -> Result:
        self.message.emit("refused", r.words)
        return Result(False, r.words, r.code)

    def _arm_locked(self, *, for_blacs: bool) -> str:
        """Arm the pending plan (caller holds the lock); returns the words for the log."""
        s = self._session
        plan = s.pending
        if plan is None:
            raise Refused("no_raster_configured", "no raster pattern is set up in the GUI")
        if self._stage.calibration is None:
            raise Refused("not_calibrated", "no calibration set; calibrate in the GUI first")
        try:
            pts = points_from_spec(plan.spec)
        except ValueError as e:
            raise Refused("no_raster_configured", f"raster could not be armed: {e}") from None
        kept = [p for p, ok in zip(pts, self._stage.reachable(pts)) if ok]
        travel = self._stage.travel_mm
        if not kept:
            raise Refused("no_raster_configured", f"every point of the pattern is outside the motor travel {travel} mm: "
                                                  "move the pattern toward the reachable part of the frame, or recalibrate")
        if for_blacs or s.state == ARMED:
            state = ARMED
        elif s.state in (LOCAL, RUNNING):
            state = s.state                       # a re-arm keeps it held or running: never starts a held raster
        else:
            state = RUNNING if plan.continuous else LOCAL
        s.arm(plan, kept, dropped=len(pts) - len(kept), state=state)
        self._cancel_run_timer_locked()           # a continuous run of the old arm takes no more steps
        self._leg += 1
        if state == RUNNING:
            self._queue_run_point_locked()
        how = {LOCAL: "step it at the GUI", ARMED: "BLACS steps it", RUNNING: "running"}[state]
        if s.dropped:
            return (f"Raster armed: {len(kept)} of {len(pts)} points; {s.dropped} dropped, outside motor "
                    f"travel {travel} mm; {how}.")
        return f"Raster armed: {len(kept)} points; {how}."

    def _send_next_locked(self, by: str):
        """Step the cursor for `by` and queue the move (caller holds the lock)."""
        s = self._session
        i, pt, wrapped = s.next_point(by)
        future = self._stage.submit(Command(f"Raster point {i} of {len(s.path)}", "move", px=pt,
                                            raster_run=s.run, quiet=by != GUI))
        return i, pt, wrapped, future, s.snapshot()

    def _stepped(self, snap: RasterSnapshot, wrapped: bool, who: str) -> None:
        self.raster_changed.emit(snap)
        if wrapped:
            self.message.emit("info", f"{who}Raster path done; started again at point 0.")

    def _queue_run_point_locked(self) -> bool:
        """Queue the continuous run's next point (caller holds the lock). Returns wrapped."""
        i, pt, wrapped = self._session.next_point(RUN)
        self._queue_run_move_locked(i, pt)
        return wrapped

    def _queue_run_move_locked(self, i: int, pt: XY) -> None:
        """Queue the move to point i of the continuous run; the run goes on from its follow-up."""
        s = self._session
        run, leg = s.run, self._leg
        self._stage.submit(Command(f"Raster point {i} of {len(s.path)}", "move", px=pt, raster_run=run,
                                   quiet=True, then=lambda res: self._after_run_point(res, leg)))

    def _after_run_point(self, res: Result, leg: int) -> None:
        """Motor thread, after a continuous run's point: halt on a failure; else queue the next
        point now or, after the plan's delay, from a timer (the motor thread never waits)."""
        with self._lock:
            s = self._session
            if s.state != RUNNING or self._leg != leg:
                return
            halted = not res.ok
            if halted:
                s.stop()
                snap = s.snapshot()
            elif s.plan.delay_s > 0:
                self._run_timer = threading.Timer(s.plan.delay_s, self._continue_run, args=(leg,))
                self._run_timer.daemon = True
                self._run_timer.start()
                return
        if halted:
            self.raster_changed.emit(snap)
            self.message.emit("error", f"Raster halted: {res.words}")
            return
        self._continue_run(leg)

    def _continue_run(self, leg: int) -> None:
        """Queue the continuous run's next point, unless the run was stopped, paused, handed to
        BLACS or armed again meanwhile."""
        with self._lock:
            if self._session.state != RUNNING or self._leg != leg:
                return
            self._run_timer = None
            wrapped = self._queue_run_point_locked()
            snap = self._session.snapshot()
        self._stepped(snap, wrapped, "")

    def _cancel_run_timer_locked(self) -> None:
        timer, self._run_timer = self._run_timer, None
        if timer is not None:
            timer.cancel()

    def _pair_click(self, clicks: _Clicks, px: XY, res: Result) -> None:
        """Motor thread: pair a calibration click with the stage position; fit when complete."""
        if not res.ok:
            self.message.emit("error", f"Calibration spot not recorded: {res.words}")
            return
        with self._lock:
            if self._clicks is not clicks:
                return
            clicks.px.append(px)
            clicks.mm.append(res.mm)
            n = len(clicks.px)
            if n >= clicks.required:
                self._clicks = None
            uh = self._user_home
        self.calibration_progress.emit(n, clicks.required)
        if n < clicks.required:
            return
        try:
            affine, summary = fit_affine(clicks.px, clicks.mm)
        except ValueError as e:
            self.calibration_progress.emit(0, 0)
            self.message.emit("refused", f"Calibration failed: {e}. Press Calibrate to start again.")
            return
        cal = CalibrationFile(affine, uh, self._stage.backlash_mm, clicks.camera_settings,
                              notes=f"fitted from {n} spots")
        res = self._apply(cal, save_to=self.calibration_path)
        self.calibration_progress.emit(0, 0)
        if res.ok:
            saved = self.calibration_file
            where = f"; saved to {saved.path}" if saved is not None and saved.path else ""
            self.message.emit("info", f"Calibration fitted: {summary}{where}.")

    def _apply(self, cal: Optional[CalibrationFile], *, save_to: Optional[str] = None) -> Result:
        """Use `cal` (None: no calibration): the stage's px -> mm, and the user home and backlash it
        carries. Refused while a raster is armed: its path is in px. Emits calibration_changed."""
        with self._lock:
            if self._session.state != OFF:
                refusal = Refused("raster_active", "calibration not changed: stop the raster first "
                                                   "(its path is in camera pixels)")
            else:
                refusal = None
                if save_to is not None and cal is not None:
                    try:
                        cal = write_calibration_file(save_to, cal)
                    except _SAVE_ERRORS as e:
                        self.message.emit("error", f"Calibration in use but not saved to {save_to} (the file "
                                                   f"there is unchanged): {e}. Save it with Save calibration.")
                self._cal_file = cal
                self._stage.set_calibration(cal.affine if cal is not None else None)
                if cal is not None and cal.user_home_mm is not None:
                    self._user_home = cal.user_home_mm
                uh = self._user_home
        if refusal is not None:
            return self._refused(refusal)
        if cal is not None:
            if cal.path:
                self._remember(cal.path)
            for axis, v, now in zip("XY", cal.backlash_mm, self._stage.backlash_mm):
                if v is not None and v != now:
                    self.set_backlash(axis, v)
            if cal.user_home_mm is not None:
                self.user_home_changed.emit(*uh)
        self.calibration_changed.emit(cal)
        return Result(True)

    def _remember(self, path: str) -> None:
        try:
            remember_calibration_path(path, self._pointer_path)
        except OSError as e:
            self.message.emit("error", f"Could not record {path} as the last calibration: {e}")

    def _serve_menu(self, transport) -> None:
        if transport is None:
            try:
                transport = ZmqRepTransport(f"tcp://*:{MENU.port}", recv_timeout_ms=_MENU_POLL_MS)
            except Exception as e:   # port taken, no pyzmq: the GUI runs without BLACS
                self._link_news("error", f"BLACS link not started, port {MENU.port}: {e}")
                return
            self._link_news("info", f"BLACS link: menu {MENU.name} v{MENU.version} on port {MENU.port}.")
        server = RasterMenuLink(self).menu_server(transport)
        failures = 0
        while not self._menu_stop.is_set():
            try:
                server.serve_once(timeout_ms=_MENU_POLL_MS)
                failures = 0
            except Exception:    # handler errors are answered; this is the socket
                traceback.print_exc()
                failures += 1
                if failures == 3:
                    self._link_news("error", "BLACS link: the socket keeps failing; see the console.")
                if failures >= 3:
                    self._menu_stop.wait(0.25)
        transport.close()

    def _link_news(self, kind: str, words: str) -> None:
        with self._lock:
            self._link_status = (kind, words)
        self.message.emit(kind, words)


class RasterMenuLink:
    """BLACS's requests on the rastering menu, answered on the menu-server thread.

    bbx_zmq.MenuServer has already refused names not on the menu and values out of range. Each
    handler asks the controller and answers what happened, or raises Refused(code, words) with the
    GUI's own words (the stage's check and the raster session decide; this only passes them on):
        read_all             xy, x, y [px] (where the stage is; None without a calibration),
                             shots_per_step, raster_state, point_index, path_len
        set xy               one move of both axes to [x, y] [px]; answers the point
        set shots_per_step   stored; the window shows it
        press arm            arm the Run tab's pattern in step mode for BLACS, or take over the
                             raster held at the GUI where it is; answers path_len, dropped
        press disarm         hand the raster to the GUI operator (path kept); answers was_armed
        press next_point     step to the next point; answers point_index, path_len, x, y, frame,
                             finished (True: this step started the path again at point 0),
                             calibration_matrix, calibration_offset
    A move the stage does not finish within the menu's time limit answers "gui_busy": the move
    stays queued and still runs.
    """

    def __init__(self, controller: SystemController):
        self.c = controller

    def menu_server(self, transport) -> MenuServer:
        return MenuServer(MENU, transport, read_all=self.read_all, set_value=self.set_value,
                          press_button=self.press_button)

    def read_all(self) -> Dict[str, Any]:
        snap = self.c.raster_snapshot()
        pos = self.c.position
        px = pos.px if pos is not None else None
        x, y = (float(px[0]), float(px[1])) if px is not None else (None, None)
        return {"xy": [x, y] if px is not None else None, "x": x, "y": y,
                "shots_per_step": snap.shots_per_step, "raster_state": snap.state,
                "point_index": snap.point_index, "path_len": len(snap.path)}

    def set_value(self, name: str, value):
        if name == "xy":
            limit = MENU.value("xy").set_s
            res = _wait(self.c.move_px(float(value[0]), float(value[1]), from_blacs=True), limit,
                        f"the stage did not finish the move to x, y within {limit:g} s; the move is "
                        "still queued and will run")
            return [float(v) for v in res.px]
        if name == "shots_per_step":
            return self.c.set_shots_per_step(int(value))
        raise Refused("no_handler", f"no handler for {name!r}")

    def press_button(self, name: str):
        if name == "arm":
            return self.c.blacs_arm()
        if name == "disarm":
            return {"was_armed": self.c.blacs_disarm()}
        if name == "next_point":
            i, (x, y), n, wrapped, future = self.c.blacs_next_point()
            limit = MENU.button("next_point").press_s
            _wait(future, limit, f"the stage did not reach the next point within {limit:g} s; the move is "
                                 "still queued and will run, and that point gets no shot (the next shot's "
                                 "next point steps past it)")
            cal = self.c.calibration
            return {"point_index": i, "path_len": n, "frame": "pixel", "x": x, "y": y, "finished": wrapped,
                    **(cal.to_json() if cal is not None else {})}
        raise Refused("no_handler", f"no handler for {name!r}")


def _wait(future: "Future[Result]", limit_s: float, busy_words: str) -> Result:
    """Wait (BLACS thread only) for a stage job; its refusal or a timeout becomes Refused."""
    try:
        res = future.result(timeout=limit_s)
    except FutureTimeout:
        raise Refused("gui_busy", busy_words) from None
    if not res.ok:
        raise Refused(res.code or "move_refused", res.words)
    return res


@dataclass(frozen=True)
class DataFiles:
    """The files the GUI writes by itself."""
    calibration: str   # every calibration fit is saved here
    pointer: str       # names the calibration file used last
    defaults: str      # File > Save settings as defaults


def data_files(cfg: AppConfig = APP_CONFIG, here: str = HERE) -> DataFiles:
    """Where the GUI keeps its own files: in `here` (this folder unless a test says otherwise).
    A simulated copy keeps them in here/sim/, so it never loads or overwrites the lab GUI's
    calibration (its backlash goes to the KCubes at the next real start) or defaults."""
    cal = cfg.paths.calibration_path
    if cfg.hardware.simulate:
        here = os.path.join(here, SIM_FOLDER)
        cal = os.path.basename(cal)
    return DataFiles(calibration=cal if os.path.isabs(cal) else os.path.join(here, cal),
                     pointer=os.path.join(here, os.path.basename(LAST_CALIBRATION_POINTER)),
                     defaults=os.path.join(here, os.path.basename(SETTINGS_DEFAULTS_FILE)))


def create_controller(cfg: AppConfig = APP_CONFIG, files: Optional[DataFiles] = None) -> SystemController:
    """The controller with the KCubes (or simulated motors when cfg.hardware.simulate), serving
    BLACS when cfg.network.blacs_link. files: default data_files(cfg)."""
    files = files or data_files(cfg)
    hw = cfg.hardware
    if hw.simulate:
        from motors_sim import SimulatedMotor
        motor_x, motor_y = SimulatedMotor("X"), SimulatedMotor("Y")
    else:
        from hardware import KCube
        motor_x = KCube(hw.serial_x, "X", hw)
        try:
            motor_y = KCube(hw.serial_y, "Y", hw)
        except Exception:
            try:
                motor_x.disconnect()
            except Exception:       # Y's error is the one to show; this one goes to the console
                traceback.print_exc()
            raise
    ctl = SystemController(motor_x, motor_y, travel_mm=hw.travel_mm, calibration_path=files.calibration,
                           pointer_path=files.pointer, close_wait_s=longest_stage_job_s(hw))
    if cfg.network.blacs_link:
        ctl.start_menu_server()
    return ctl
