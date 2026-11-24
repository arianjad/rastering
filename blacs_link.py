"""The BLACS link: RaX's protocol, REQ/REP for requests and PUB/SUB for the live state.

Names, reply keys and error codes are upstream rastering's (RaXcollab/rastering
raster_controller.py at 5fe760e: `_RasteringV2Server._handle_program` and `_handle_check`, the
PUB part of `SystemController._zmq_loop`, `raster_point_meta`). The envelope, HELLO and PING are
RaX's zmq_v2.py (beambox-daq `bbx_zmq.zmq_v2`, copied whole from RaX).

Requests (REP socket, config.py NetworkConfig.zmq_bind, port 55535). x, y are camera px.
    PROGRAM_VALUE laser_raster_xy [x, y]    one move of both axes (args.frame "motor": [mm])
    PROGRAM_VALUE laser_raster_x_coord v    one axis; the other stays where the stage is (also _y_)
    PROGRAM_VALUE arm_raster 0              arm the Run tab's pattern for BLACS, or take the raster
                                            held at the GUI where it is: {mode, armed, dropped}
    PROGRAM_VALUE move_to_next 1            armed for BLACS: step and wait for the move, answer the
                                            point (point_index, path_len, target_xy, frame,
                                            calibration_matrix, calibration_offset, wrapped).
                                            Held at the GUI or a continuous run: the point it is
                                            at, nothing moves. Nothing armed: {in_place: true,
                                            target_xy, frame}, nothing moves.
    PROGRAM_VALUE shots_per_step n          shown in the GUI: {shots_per_step}
    PROGRAM_VALUE disarm_raster 1           the GUI steps the raster BLACS had: {disarmed}
    CHECK_VALUE laser_raster_x_coord[_monitor]   where the stage is [px] (also _y_)
A move waits up to args.timeout_sec (default 10 s); a move not done by then answers TIMEOUT
command_timeout, and the move stays queued and still runs. A move done with a warning (outside the
travel; while the raster is armed for BLACS or a continuous run goes) answers SUCCESS with the
words in a `warning` key: the GUI does what the sequence asks and says what it cost.

Live state (PUB socket, NetworkConfig.pub_bind, port 55536), one text frame "<topic> <value>":
    every 0.25 s   laser_raster_x_coord_monitor, laser_raster_y_coord_monitor [px] (calibrated)
    every 1 s      heartbeat; raster_mode idle|manual|step|continuous; raster_owner
                   none|local|remote; calibration_status calibrated|uncalibrated;
                   raster_progress <point_index + 1>/<path_len>

Two additions to upstream, both additive (RaX's own BLACS still works with them):
- move_to_next answers wrapped: true on the step from the last point back to point 0. It never
  answers finished: true (in RaX that means "path used up, nothing moved, arm again"; this path
  loops).
- The PUB socket has its own thread, so the heartbeat keeps coming while a request waits up to
  10 s for a move (upstream publishes from the request thread and goes quiet meanwhile).
"""
from __future__ import annotations

import math
import threading
import traceback
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Dict, List, Optional

from bbx_zmq.zmq_v2 import RemoteControlServerBase, ZmqRepTransport, encode_reply, handler

from raster_session import ARMED, LOCAL, OFF, RUNNING, Refused

SERVE_POLL_MS = 250       # the request thread checks for its stop this often [ms]
PUB_PERIOD_S = 0.25       # positions are published this often [s]
SLOW_EVERY = 4            # heartbeat and raster words every 4th publish (about 1 s)
DEFAULT_TIMEOUT_S = 10.0  # a move waits this long unless args.timeout_sec says otherwise [s]

RASTER_MODE = {OFF: "idle", LOCAL: "manual", ARMED: "step", RUNNING: "continuous"}
RASTER_OWNER = {OFF: "none", LOCAL: "local", ARMED: "remote", RUNNING: "local"}
_COORDS = {"laser_raster_x_coord": 0, "laser_raster_y_coord": 1}
_MONITORS = {**_COORDS, "laser_raster_x_coord_monitor": 0, "laser_raster_y_coord_monitor": 1}
_RETRYABLE = {"motor_move_failed", "command_timeout", "position_not_initialized"}


class RaxLink(RemoteControlServerBase):
    """BLACS's requests, answered on the request thread by calling the controller
    (raster_controller.SystemController), and the live state for publish(text)."""

    CAPABILITIES = frozenset({"monitors", "heartbeat"})

    def __init__(self, controller, transport, publish: Callable[[str], None] = lambda text: None):
        super().__init__("RasteringGUI", transport)
        self.c = controller
        self._publish = publish
        self._ticks = 0

    # --- requests ---

    @handler("PROGRAM_VALUE")
    def _handle_program(self, connection, value, args, request_id) -> bytes:
        limit = _timeout_s(args)
        try:
            if connection == "laser_raster_xy":
                extra = self._move_xy(value, args.get("frame", "pixel"), limit)
            elif connection in _COORDS:
                extra = self._move_axis(_COORDS[connection], value, limit)
            elif connection == "arm_raster":
                armed = self.c.blacs_arm()
                extra = {"mode": "step", "armed": armed["path_len"], "dropped": armed["dropped"]}
            elif connection == "move_to_next":
                extra = self._move_to_next(limit)
            elif connection == "shots_per_step":
                extra = {"shots_per_step": self.c.set_shots_per_step(_shots(value))}
            elif connection == "disarm_raster":
                extra = {"disarmed": self.c.blacs_disarm()}
            else:
                return _unknown(request_id, connection)
        except Refused as r:
            return _error(request_id, r.code, r.words)
        return encode_reply(status="SUCCESS", request_id=request_id, extra=extra)

    @handler("CHECK_VALUE")
    def _handle_check(self, connection, value, args, request_id) -> bytes:
        if connection not in _MONITORS:
            return _unknown(request_id, connection)
        pos = self.c.position
        if pos is None or pos.px is None:
            return _error(request_id, "position_not_initialized",
                          f"{connection}: no position in camera px yet (no calibration, or no position "
                          "read since the GUI started)", status="UNKNOWN_CONNECTION")
        return encode_reply(status="SUCCESS", request_id=request_id, value=float(pos.px[_MONITORS[connection]]))

    def _move_xy(self, value, frame: str, limit: float) -> Dict[str, Any]:
        x, y = _pair(value)
        if frame == "pixel":
            future, unit = self.c.move_px(x, y, from_blacs=True), "px"
        elif frame == "motor":
            future, unit = self.c.move_mm(x, y, from_blacs=True), "mm"
        else:
            raise Refused("invalid_frame", f"frame must be 'pixel' or 'motor'; got {frame!r}")
        res = _wait(future, limit, f"the stage did not finish the move to ({x:g}, {y:g}) {unit} within "
                                   f"{limit:g} s; the move is still queued and will run")
        return {"warning": res.warning} if res.warning else {}

    def _move_axis(self, k: int, value, limit: float) -> Dict[str, Any]:
        v = _number(value)
        pos = self.c.position
        if pos is None or pos.px is None:
            raise Refused("not_calibrated", "no calibration set: calibrate in the GUI first (x, y are camera pixels)")
        xy = [float(pos.px[0]), float(pos.px[1])]
        xy[k] = v
        return self._move_xy(xy, "pixel", limit)

    def _move_to_next(self, limit: float) -> Dict[str, Any]:
        try:
            i, pt, n, wrapped, future = self.c.blacs_next_point()
        except Refused:              # not armed for BLACS: answer where the stage is, move nothing
            return self._not_stepped()
        try:
            res = _wait(future, limit, f"the stage did not reach the next point within {limit:g} s; the move "
                                       "is still queued and will run, and that point gets no shot (the next "
                                       "shot's next point steps past it)")
        except Refused as r:
            raise Refused(r.code if r.code == "command_timeout" else "raster_step_failed", r.words) from None
        return {**self._point_meta(i, n, pt), "wrapped": wrapped, **({"warning": res.warning} if res.warning else {})}

    def _not_stepped(self) -> Dict[str, Any]:
        """move_to_next when BLACS does not step the raster. Nothing armed: fire in place, with
        where the stage is (upstream: "fire in place"). Held at the GUI or a continuous run: the
        point the raster is at (upstream: "held by operator")."""
        snap, pos, cal = self.c.raster_snapshot(), self.c.position, self.c.calibration
        if snap.state == OFF:
            extra: Dict[str, Any] = {"in_place": True, "frame": "pixel" if cal is not None else "motor"}
            xy = (pos.px if cal is not None else pos.mm) if pos is not None else None
            if xy is not None:
                extra["target_xy"] = [float(xy[0]), float(xy[1])]
            return extra
        i, n = snap.point_index, len(snap.path)
        pt = snap.path[i] if 0 <= i < n else (pos.px if pos is not None else None)
        return self._point_meta(i, n, pt)

    def _point_meta(self, i: int, n: int, pt) -> Dict[str, Any]:
        """upstream raster_point_meta: which point, where it was sent [px], and the calibration."""
        meta: Dict[str, Any] = {"point_index": int(i), "path_len": int(n), "frame": "pixel"}
        if pt is not None:
            meta["target_xy"] = [float(pt[0]), float(pt[1])]
        cal = self.c.calibration
        if cal is not None:
            meta.update(cal.to_json())
        return meta

    # --- live state ---

    def publish_once(self) -> None:
        """Positions [px] on every call; heartbeat and the raster words on every 4th, first call
        included."""
        slow = self._ticks % SLOW_EVERY == 0
        self._ticks += 1
        pos = self.c.position
        if pos is not None and pos.px is not None:
            self._publish(f"laser_raster_x_coord_monitor {float(pos.px[0])}")
            self._publish(f"laser_raster_y_coord_monitor {float(pos.px[1])}")
        if not slow:
            return
        snap = self.c.raster_snapshot()
        self._publish("heartbeat")
        self._publish(f"raster_mode {RASTER_MODE[snap.state]}")
        self._publish(f"raster_owner {RASTER_OWNER[snap.state]}")
        self._publish("calibration_status " + ("calibrated" if self.c.calibration is not None else "uncalibrated"))
        self._publish(f"raster_progress {snap.point_index + 1}/{len(snap.path)}")

    # --- the two threads ---

    def serve_requests(self, stop: threading.Event, news: Callable[[str, str], None]) -> None:
        failures = 0
        while not stop.is_set():
            try:
                self.serve_once(timeout_ms=SERVE_POLL_MS)
                failures = 0
            except Exception:       # a handler's error is answered; this is the socket
                traceback.print_exc()
                failures += 1
                if failures == 3:
                    news("error", "BLACS link: the request socket keeps failing; see the console.")
                if failures >= 3:
                    stop.wait(0.25)

    def publish_forever(self, stop: threading.Event, news: Callable[[str, str], None]) -> None:
        failed = False
        while not stop.is_set():
            try:
                self.publish_once()
                failed = False
            except Exception as e:
                if not failed:      # once per outage
                    traceback.print_exc()
                    news("error", f"BLACS live state not published: {e}")
                failed = True
            stop.wait(PUB_PERIOD_S)


def start(controller, stop: threading.Event, *, bind: str, pub_bind: str, transport=None,
          publish: Optional[Callable[[str], None]] = None,
          news: Callable[[str, str], None]) -> List[threading.Thread]:
    """Serve BLACS: make the sockets (bind / pub_bind; "" = none) unless `transport` / `publish`
    are given (tests: one end of zmq_v2.InMemoryTransport.pair(), list.append), start the
    request and publisher threads, return them. news(kind, words): what the operator sees."""
    made: List[str] = []
    close_rep = close_pub = None
    if transport is None:
        if not bind:
            return []
        try:
            transport = ZmqRepTransport(bind, recv_timeout_ms=SERVE_POLL_MS)
        except Exception as e:      # port taken, no pyzmq: the GUI runs without BLACS
            news("error", f"BLACS link not started on {bind}: {e}")
            return []
        close_rep = transport.close
        made.append(f"requests on {bind}")
    if publish is None and pub_bind:
        try:
            sock = _pub_socket(pub_bind)
        except Exception as e:
            news("error", f"BLACS live state not published on {pub_bind}: {e}; requests are served.")
        else:
            publish, close_pub = sock.send_string, (lambda: sock.close(0))
            made.append(f"live state on {pub_bind}")
    if made:
        news("info", "BLACS link: " + ", ".join(made) + ".")
    link = RaxLink(controller, transport, publish or (lambda text: None))

    def run(job, close):
        try:
            job(stop, news)
        finally:
            if close is not None:
                try:
                    close()
                except Exception:
                    traceback.print_exc()

    threads = [threading.Thread(target=run, args=(link.serve_requests, close_rep), name="blacs-req", daemon=True)]
    if publish is not None:
        threads.append(threading.Thread(target=run, args=(link.publish_forever, close_pub),
                                        name="blacs-pub", daemon=True))
    for t in threads:
        t.start()
    return threads


def _pub_socket(address: str):
    import zmq
    sock = zmq.Context.instance().socket(zmq.PUB)
    sock.setsockopt(zmq.LINGER, 0)
    sock.bind(address)
    return sock


def _wait(future, limit_s: float, late_words: str):
    """Wait (request thread only) for a stage job: its refusal or a late finish becomes Refused."""
    try:
        res = future.result(timeout=limit_s)
    except FutureTimeout:
        raise Refused("command_timeout", late_words) from None
    if not res.ok:
        raise Refused(res.code or "motor_move_failed", res.words)
    return res


def _error(request_id, code: str, words: str, *, status: Optional[str] = None) -> bytes:
    status = status or ("TIMEOUT" if code == "command_timeout" else "ERROR")
    return encode_reply(status=status, request_id=request_id,
                        error={"code": code, "message": words, "retryable": code in _RETRYABLE})


def _unknown(request_id, connection) -> bytes:
    return _error(request_id, "unknown_connection", f"unknown_connection: {connection}",
                  status="UNKNOWN_CONNECTION")


def _timeout_s(args) -> float:
    try:
        return float(args.get("timeout_sec", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def _number(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = math.nan
    if not math.isfinite(v):
        raise Refused("invalid_value", f"value must be a number; got {value!r}")
    return v


def _pair(value):
    try:
        if isinstance(value, (str, bytes, dict)) or len(value) != 2:
            raise TypeError
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        x = y = math.nan
    if not (math.isfinite(x) and math.isfinite(y)):
        raise Refused("invalid_value", f"value must be a pair of finite numbers [x, y]; got {value!r}")
    return x, y


def _shots(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        raise Refused("invalid_value", f"shots_per_step must be an integer >= 1; got {value!r}")
    return n
