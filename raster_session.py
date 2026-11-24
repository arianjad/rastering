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
    """The raster as it is now; the window draws from this alone."""
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
    run: int = 0                       # counts arms; a queued raster point belongs to one run


class RasterSession:
    def __init__(self):
        self.pending: Optional[RasterPlan] = None
        self.shots_per_step = 1
        self.run = 0
        self._clear()

    def _clear(self) -> None:
        self.state, self.plan, self.path = OFF, None, ()
        self.pattern_len = self.dropped = 0
        self.cursor = self.selected = -1

    def arm(self, plan: RasterPlan, points: Sequence[XY], *, dropped: int, state: str) -> None:
        """Arm one pass of `points` [px] (already inside the travel) in `state` (LOCAL, ARMED or
        RUNNING); the cursor starts before point 0."""
        pts = tuple((float(x), float(y)) for x, y in points)
        self.state, self.plan = state, plan
        self.path = pts + pts[::-1] if plan.pingpong else pts
        self.pattern_len, self.dropped = len(pts), int(dropped)
        self.cursor = self.selected = -1
        self.run += 1

    def next_point(self, by: str) -> Tuple[int, XY, bool]:
        """Advance the cursor for `by` (GUI, BLACS or RUN): (index, point, wrapped), wrapped = this
        step went from the last point back to point 0."""
        if self.state != _STEPS_IN[by]:
            raise Refused(*_step_refusal(by, self.state))
        wrapped = self.cursor == len(self.path) - 1
        self.cursor = 0 if wrapped else self.cursor + 1
        return self.cursor, self.path[self.cursor], wrapped

    def go_to(self, index: int) -> Tuple[int, XY, bool]:
        """The GUI's go to point `index` (clamped): the next step continues after it. Takes the
        raster back from BLACS. Returns (index, point, taken back from BLACS)."""
        if self.state == OFF:
            raise Refused("not_armed", "no raster armed: arm the raster first")
        if self.state == RUNNING:
            raise Refused("raster_running", "a continuous run steps this raster: press Stop first")
        taken = self.state == ARMED
        self.state = LOCAL
        self.cursor = self.selected = max(0, min(int(index), len(self.path) - 1))
        return self.cursor, self.path[self.cursor], taken

    def select(self, index: int) -> int:
        """Pick point `index` (clamped) of the armed path; nothing moves."""
        if not self.path:
            raise Refused("not_armed", "no raster armed: there is no path to pick a point on")
        self.selected = max(0, min(int(index), len(self.path) - 1))
        return self.selected

    def hand_to_blacs(self) -> bool:
        """BLACS steps the raster from where it is. Returns True when this ended a continuous run."""
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

    def stop(self) -> bool:
        """End the raster. Returns True when one was armed or running."""
        was = self.state != OFF
        self._clear()
        return was

    def snapshot(self) -> RasterSnapshot:
        return RasterSnapshot(
            state=self.state, path=self.path, pattern_len=self.pattern_len, dropped=self.dropped,
            point_index=self.cursor, selected_index=self.selected, shots_per_step=self.shots_per_step,
            armed_plan=self.plan, pending_plan=self.pending,
            path_edited=self.state != OFF and self.pending is not None and self.pending != self.plan,
            run=self.run)


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
