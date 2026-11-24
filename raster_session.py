"""What the raster is doing: one state word, the armed path and its cursor.

Pure: no Qt, no threads (the controller holds the lock around every call). The state words are
the rastering menu's (bbx_zmq/menus/rastering.py):
    OFF      no raster armed
    LOCAL    armed in step mode, stepped at the GUI
    ARMED    armed in step mode, stepped by BLACS (one point per group of shots)
    RUNNING  a continuous run at the GUI
The path always loops: the step after the last point is point 0 again (`wrapped`). A request
the state does not allow raises bbx_zmq.Refused(code, words).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from bbx_zmq import Refused
from bbx_zmq.menus.rastering import ARMED, LOCAL, OFF, RUNNING

from raster_paths import RasterSpec

XY = Tuple[float, float]

# Who asks for a next point, and the state in which they may.
GUI, BLACS, RUN = "gui", "blacs", "run"
_STEPS_IN = {GUI: LOCAL, BLACS: ARMED, RUN: RUNNING}


@dataclass(frozen=True)
class RasterPlan:
    """What the Run tab has set up: the pattern and how to run it."""
    spec: RasterSpec
    pingpong: bool = True      # forwards then backwards: the armed path is 2N points long
    continuous: bool = False   # a GUI arm starts a continuous run; else it is stepped
    delay_s: float = 0.0       # continuous run: wait after each point [s]


@dataclass(frozen=True)
class RasterSnapshot:
    """The raster at one moment. The controller sends one on raster_changed after it lets go of
    its lock, so two can arrive out of order: a slot draws from controller.raster_snapshot()
    (the window does), or ignores a snapshot whose seq is below the last one it drew."""
    state: str = OFF
    path: Tuple[XY, ...] = ()          # the armed path [px], the way back included with ping-pong
    pattern_len: int = 0               # points in one pass (before the ping-pong copy)
    dropped: int = 0                   # pattern points left out at arm: outside the stage travel
    point_index: int = -1              # the point last stepped to; -1: none yet
    selected_index: int = -1           # the point picked for "go to"; -1: none
    shots_per_step: int = 1            # set by BLACS: shots it takes per point
    armed_plan: Optional[RasterPlan] = None
    pending_plan: Optional[RasterPlan] = None
    path_edited: bool = False          # armed, and the Run tab's plan is not the armed one
    options_edited: bool = False       # path_edited, by a run option only (ping-pong), not the pattern
    run: int = 0                       # counts arms; a queued raster point belongs to one run
    seq: int = 0                       # counts snapshots: a higher seq was taken later


class RasterSession:
    def __init__(self):
        self.pending: Optional[RasterPlan] = None
        self.shots_per_step = 1
        self.run = 0
        self._seq = 0
        self._clear()

    def _clear(self) -> None:
        self.state, self.plan, self.path = OFF, None, ()
        self.pattern_len = self.dropped = 0
        self.cursor = self.selected = -1
        self.parked = False    # a go to put the stage on the cursor's point; nobody stepped since

    def arm(self, plan: RasterPlan, points: Sequence[XY], *, dropped: int, state: str) -> None:
        """Arm one pass of `points` [px] (already inside the travel) in `state` (LOCAL, ARMED or
        RUNNING); the cursor starts before point 0."""
        pts = tuple((float(x), float(y)) for x, y in points)
        self.state, self.plan = state, plan
        self.path = pts + pts[::-1] if plan.pingpong else pts
        self.pattern_len, self.dropped = len(pts), int(dropped)
        self.cursor = self.selected = -1
        self.parked = False
        self.run += 1

    def next_index(self, by: str) -> int:
        """The point `by`'s next step goes to. BLACS's first step after a go to N is N itself: the
        operator went there for BLACS to shoot it."""
        if by == BLACS and self.parked:
            return self.cursor
        return 0 if self.cursor == len(self.path) - 1 else self.cursor + 1

    def next_point(self, by: str) -> Tuple[int, XY, bool]:
        """Advance the cursor for `by` (GUI, BLACS or RUN): (index, point, wrapped), wrapped = this
        step went from the last point back to point 0."""
        if self.state != _STEPS_IN[by]:
            raise Refused(*_step_refusal(by, self.state))
        stays = by == BLACS and self.parked
        wrapped = not stays and self.cursor == len(self.path) - 1
        self.cursor, self.parked = self.next_index(by), False
        return self.cursor, self.path[self.cursor], wrapped

    def go_to(self, index: int) -> Tuple[int, XY, bool]:
        """The GUI's go to point `index` (clamped): the GUI's next step continues after it, BLACS's
        next step (once handed over) shoots it. Takes the raster back from BLACS. Returns (index,
        point, taken back from BLACS)."""
        if self.state == OFF:
            raise Refused("not_armed", "no raster armed: arm the raster first")
        if self.state == RUNNING:
            raise Refused("raster_running", "a continuous run steps this raster: press Stop first")
        taken = self.state == ARMED
        self.state = LOCAL
        self.cursor = self.selected = max(0, min(int(index), len(self.path) - 1))
        self.parked = True
        return self.cursor, self.path[self.cursor], taken

    def pause(self) -> int:
        """The GUI's Pause: a continuous run is held here, at its point (the cursor stays)."""
        if self.state != RUNNING:
            raise Refused("not_running", f"no continuous run to pause (raster state: {self.state})")
        self.state = LOCAL
        return self.cursor

    def resume(self) -> Tuple[int, XY]:
        """The GUI's Continue: a raster held here runs by itself again from the cursor's point (it
        is moved to and waited at again: its wait may have been cut short); none yet: point 0."""
        if self.state != LOCAL:
            raise Refused("not_held", f"Continue runs a raster held at the GUI (raster state: {self.state})")
        self.state, self.parked = RUNNING, False
        self.cursor = max(self.cursor, 0)
        return self.cursor, self.path[self.cursor]

    def select(self, index: int) -> int:
        """Pick point `index` (clamped) of the armed path; nothing moves."""
        if not self.path:
            raise Refused("not_armed", "no raster armed: there is no path to pick a point on")
        self.selected = max(0, min(int(index), len(self.path) - 1))
        return self.selected

    def hand_to_blacs(self) -> bool:
        """BLACS steps the raster from where it is (next_index(BLACS) is its next point). Returns
        True when this ended a continuous run."""
        if self.state == OFF:
            raise Refused("not_armed", "no raster armed to hand to BLACS")
        was_running = self.state == RUNNING
        self.state = ARMED
        return was_running

    def take_back(self, by: str) -> bool:
        """The GUI steps the raster from where it is. Returns True when BLACS had it."""
        if by == BLACS and self.state == RUNNING:
            raise Refused("raster_running", "a continuous raster runs from the GUI; stop it there")
        was_armed = self.state == ARMED
        if was_armed:
            self.state = LOCAL
        return was_armed

    def set_pending(self, plan: Optional[RasterPlan]) -> bool:
        """The Run tab's plan. When it differs from the armed one only in how the raster runs
        (delay, continuous: the path is the same), the armed raster takes it at once; returns True
        then. A new pattern or ping-pong (a new path) waits for a re-arm."""
        self.pending = plan
        armed = self.plan
        if self.state == OFF or plan is None or armed is None or plan == armed:
            return False
        if (plan.spec, plan.pingpong) != (armed.spec, armed.pingpong):
            return False
        self.plan = plan
        return True

    def stop(self) -> bool:
        """End the raster. Returns True when one was armed or running."""
        was = self.state != OFF
        self._clear()
        return was

    def snapshot(self) -> RasterSnapshot:
        self._seq += 1
        edited = self.state != OFF and self.pending is not None and self.pending != self.plan
        return RasterSnapshot(
            state=self.state, path=self.path, pattern_len=self.pattern_len, dropped=self.dropped,
            point_index=self.cursor, selected_index=self.selected, shots_per_step=self.shots_per_step,
            armed_plan=self.plan, pending_plan=self.pending, path_edited=edited,
            options_edited=edited and self.plan is not None and self.pending.spec == self.plan.spec,
            run=self.run, seq=self._seq)


def _step_refusal(by: str, state: str) -> Tuple[str, str]:
    if by == BLACS:
        return "not_armed", f"no raster armed for BLACS (raster state: {state})"
    if by == RUN:
        return "not_running", f"no continuous run (raster state: {state})"
    return {
        OFF: ("not_armed", "no raster armed: press Arm here first"),
        ARMED: ("blacs_steps", "BLACS steps this raster: press Take back first"),
        RUNNING: ("raster_running", "a continuous run steps this raster: press Stop first"),
    }[state]
