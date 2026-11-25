"""Raster patterns: the points of a square, spiral or convex-hull pattern, in image pixels [px].

Pure: no Qt, no motors. A RasterSpec describes a pattern; points_from_spec builds it once, checked
up front, and raises ValueError with words for the operator when it cannot.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np
from scipy.spatial import Delaunay

XY = Tuple[float, float]
Bounds = Tuple[float, float, float, float]  # xmin, xmax, ymin, ymax [px]

KINDS = ("square_x", "square_y", "spiral", "hull")
# The most points a pattern may have: building and arming take about a second per million on the
# GUI thread, and a typo'd 0.01 px step would ask for billions (the window would freeze). The
# image draws at most views/camera_view.MAX_DRAWN of them; all are armed.
MAX_PATH_POINTS = 2_000_000
_MAX_HULL_CELLS = 5_000_000     # hull grid cells tested at most (memory and time)
# Spiral angle steps at most, clipped ones included: the spiral is built point by point on the
# GUI thread (about 0.3 s per million steps), so more would freeze the window.
_MAX_SPIRAL_STEPS = 1_000_000


@dataclass(frozen=True)
class RasterSpec:
    """One pattern [px].

    kind         "square_x" (rows along x), "square_y" (columns along y), "spiral" (inward), "hull"
    bounds       square: the box scanned; spiral: points outside it are left out (None: none);
                 hull: not used (the hull fills its own bounding box)
    xstep, ystep square and hull grid steps
    origin, radius, step, angle_step, angle_step_change
                 spiral: centre, start radius, radius decrease per turn, angle per point [rad],
                 angle increase per turn [rad]
    hull_points  hull: the clicked corners
    """
    kind: str
    bounds: Optional[Bounds] = None
    xstep: float = 0.01
    ystep: float = 0.01
    origin: Optional[XY] = None
    radius: float = 1.0
    step: float = 0.1
    angle_step: float = 0.2
    angle_step_change: float = 0.0
    hull_points: Tuple[XY, ...] = ()

    def __post_init__(self) -> None:
        # Tuples, so two specs compare equal by value and the spec can be hashed.
        if self.bounds is not None:
            object.__setattr__(self, "bounds", tuple(float(v) for v in self.bounds))
        if self.origin is not None:
            object.__setattr__(self, "origin", (float(self.origin[0]), float(self.origin[1])))
        object.__setattr__(self, "hull_points",
                           tuple((float(x), float(y)) for x, y in self.hull_points))


def points_from_spec(spec: RasterSpec) -> List[XY]:
    """The pattern's points in visiting order [px]. Raises ValueError (plain words) when the
    pattern cannot be built, has no points, or has more than MAX_PATH_POINTS."""
    if spec.bounds is not None:
        xmin, xmax, ymin, ymax = spec.bounds
        for axis, lo, hi in (("x", xmin, xmax), ("y", ymin, ymax)):
            if lo > hi:
                raise ValueError(f"{axis} from is larger than {axis} to in the scan bounds: swap them")
    if spec.kind in ("square_x", "square_y"):
        pts = _square(spec)
        hint = "check the step sizes against the scan bounds"
    elif spec.kind == "spiral":
        pts = _spiral(spec)
        hint = "the spiral's centre, radius and step may not reach into the scan bounds"
    elif spec.kind == "hull":
        pts = _hull(spec.hull_points, spec.xstep, spec.ystep)
        hint = "the steps may be larger than the hull"
    else:
        raise ValueError(f"unknown pattern {spec.kind!r}: use one of {', '.join(KINDS)}")
    if not pts:
        raise ValueError(f"the {spec.kind} pattern has no points: {hint}")
    return pts


def nearest_index(points: Sequence[XY], x: float, y: float) -> int:
    """Index of the point nearest (x, y); the lowest index wins a tie; -1 for no points."""
    if len(points) == 0:
        return -1
    p = np.asarray(points, dtype=float)
    return int(np.argmin((p[:, 0] - x) ** 2 + (p[:, 1] - y) ** 2))


def _too_many(n: int) -> ValueError:
    return ValueError(f"the pattern has {n:,} points, more than {MAX_PATH_POINTS:,} (the window would freeze): "
                      "use larger steps")


def _grid(start: float, stop: float, step: float, *, snap_stop: bool) -> np.ndarray:
    """start, start + step, ... not past stop; with snap_stop the last value is set to stop
    when it lands on stop within rounding."""
    n = int(np.floor((stop - start) / step + 1e-12)) + 1
    if n <= 0:
        return np.array([], dtype=float)
    arr = start + step * np.arange(n, dtype=float)
    if snap_stop and abs(arr[-1] - stop) <= max(1e-12, abs(step) * 1e-9):
        arr[-1] = stop
    return arr


def _square(spec: RasterSpec) -> List[XY]:
    """Serpentine from (xmin, ymax): along x row by row (square_x) or along y column by column."""
    if spec.bounds is None:
        raise ValueError(f"the {spec.kind} pattern needs scan bounds")
    if spec.xstep <= 0 or spec.ystep <= 0:
        raise ValueError("step x and step y must be > 0")
    xmin, xmax, ymin, ymax = spec.bounds
    xs = _grid(xmin, xmax, spec.xstep, snap_stop=True)
    ys = _grid(ymax, ymin, -spec.ystep, snap_stop=True)      # top row first
    if xs.size * ys.size > MAX_PATH_POINTS:
        raise _too_many(xs.size * ys.size)
    if spec.kind == "square_x":
        return [(float(x), float(y)) for i, y in enumerate(ys) for x in (xs if i % 2 == 0 else xs[::-1])]
    return _snake_columns(xs, ys)


def _snake_columns(xs: np.ndarray, ys: np.ndarray, keep: Optional[np.ndarray] = None) -> List[XY]:
    """Column by column along xs; the first column runs along ys, the next back, and so on.
    keep [column, row]: the grid points to visit (None: all); a column with none is skipped."""
    cols = []
    for i, x in enumerate(xs):
        y = ys if keep is None else ys[keep[i]]
        if y.size:
            cols.append(np.column_stack([np.full(y.size, x), y if len(cols) % 2 == 0 else y[::-1]]))
    return list(map(tuple, np.concatenate(cols).tolist())) if cols else []


def _spiral(spec: RasterSpec) -> List[XY]:
    """Inward spiral: a point every angle_step around a circle from angle 0, the radius shrinking
    by step each turn; a ring of radius 0 is the centre once; points outside the bounds are left
    out."""
    if spec.origin is None:
        raise ValueError("the spiral pattern needs a centre")
    if spec.radius < 0 or spec.step <= 0:
        raise ValueError("spiral radius must be >= 0 and spiral step > 0")
    if spec.angle_step <= 0:
        raise ValueError("spiral angle step must be > 0")
    _check_spiral_size(spec)
    ox, oy = spec.origin
    r, alpha, d_alpha = float(spec.radius), 0.0, float(spec.angle_step)
    xmin, xmax, ymin, ymax = spec.bounds if spec.bounds is not None else (-np.inf, np.inf, -np.inf, np.inf)
    pts: List[XY] = []
    for _ in range(_MAX_SPIRAL_STEPS):
        if alpha >= 2 * np.pi:          # one turn done: next ring, starting again at angle 0
            alpha = 0.0
            d_alpha += float(spec.angle_step_change)
            r -= spec.step
            if r < -1e-9 * spec.step:
                return pts
        centre = r <= 1e-9 * spec.step
        x, y = (ox, oy) if centre else (ox + r * math.cos(alpha), oy + r * math.sin(alpha))
        alpha += d_alpha
        if xmin <= x <= xmax and ymin <= y <= ymax:
            pts.append((float(x), float(y)))
            if len(pts) > MAX_PATH_POINTS:
                raise _too_many(len(pts))
        if centre:
            return pts
    raise ValueError("the spiral needs too many steps: use a larger angle step or spiral step")


def _check_spiral_size(spec: RasterSpec) -> None:
    """Refuse, before the loop, a spiral whose angle step reaches 0 (it would never finish a turn)
    or that needs more than _MAX_SPIRAL_STEPS steps: turns x steps per turn, plus the centre."""
    turns = max(0, math.ceil(spec.radius / spec.step - 1e-9))     # rings of radius > 0
    if turns > _MAX_SPIRAL_STEPS:
        raise ValueError(f"the spiral has {turns:,} turns, more than {_MAX_SPIRAL_STEPS:,} steps allow: "
                         "use a larger spiral step or a smaller radius")
    if turns == 0:
        return
    d = spec.angle_step + np.arange(turns) * float(spec.angle_step_change)   # angle step of each turn
    bad = np.flatnonzero(d <= 0)
    if bad.size:
        raise ValueError(f"the angle step falls to 0 or below on turn {bad[0] + 1} of {turns} (it changes by "
                         "the angle change each turn): make the angle change less negative, or the spiral "
                         "step larger")
    steps = int(np.ceil(2 * np.pi / d).sum()) + 1
    if steps > _MAX_SPIRAL_STEPS:
        raise ValueError(f"the spiral needs about {steps:,} angle steps, more than {_MAX_SPIRAL_STEPS:,}: "
                         "use a larger angle step or spiral step")


def _hull(hull: Sequence[XY], xstep: float, ystep: float) -> List[XY]:
    """The grid points inside the convex hull of the clicked corners, over the hull's bounding box
    (RaX's spots), column by column like the squares: from the top row (ymax) down the first
    column, back up the next."""
    if len(hull) < 3:
        raise ValueError("a convex hull needs at least 3 points: click them on the image")
    if xstep <= 0 or ystep <= 0:
        raise ValueError("step x and step y must be > 0")
    corners = np.asarray(hull, dtype=float)
    try:
        tri = Delaunay(corners)
    except Exception:   # scipy QhullError (its text is for programmers): the corners span no area
        raise ValueError("the corners lie on one line: click at least 3 corners that enclose an area") from None
    xs = _grid(corners[:, 0].min(), corners[:, 0].max(), xstep, snap_stop=False)
    ys = _grid(corners[:, 1].min(), corners[:, 1].max(), ystep, snap_stop=False)
    if xs.size * ys.size > _MAX_HULL_CELLS:
        raise ValueError(f"hull grid too fine ({xs.size}x{ys.size} cells > {_MAX_HULL_CELLS}): "
                         "increase step x / y for this hull")
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    grid = np.column_stack([gx.ravel(), gy.ravel()])
    inside = (tri.find_simplex(grid) >= 0).reshape(xs.size, ys.size)     # [column, row]
    n = int(inside.sum())
    if n > MAX_PATH_POINTS:
        raise _too_many(n)
    return _snake_columns(xs, ys[::-1], inside[:, ::-1])                  # top row first, as the squares
