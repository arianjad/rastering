"""Raster patterns (raster_paths.py): the points the stage visits, built once and checked up front."""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pytest
from scipy.spatial import Delaunay

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from raster_paths import MAX_PATH_POINTS, RasterSpec, nearest_index, points_from_spec  # noqa: E402


def _old_hull_loop(hull, xstep, ystep):
    """RaX's hull fill (one find_simplex call per grid point), copied from upstream
    raster_paths.py iter_convex_hull_fill with bounds=None, order "xy"."""
    def arange_inclusive(start, stop, step, include_stop=True, eps=1e-12):
        n = int(np.floor((stop - start) / step + eps)) + 1
        if n <= 0:
            return np.array([], dtype=float)
        arr = start + step * np.arange(n, dtype=float)
        if include_stop and len(arr) > 0 and abs(arr[-1] - stop) <= max(eps, abs(step) * 1e-9):
            arr[-1] = stop
        return arr

    tri = Delaunay(np.array(hull, dtype=float))
    xs_all, ys_all = [p[0] for p in hull], [p[1] for p in hull]
    xs = arange_inclusive(min(xs_all), max(xs_all), xstep, include_stop=False)
    ys = arange_inclusive(min(ys_all), max(ys_all), ystep, include_stop=False)
    out = []
    for x in xs:
        for y in ys:
            if tri.find_simplex(np.array([x, y], dtype=float)) >= 0:
                out.append((float(x), float(y)))
    return out


@pytest.mark.parametrize("hull, step", [
    ([(0, 0), (40, 0), (40, 40), (0, 40)], (10.0, 10.0)),               # grid points on every edge
    ([(100, 100), (300, 100), (200, 300)], (7.0, 5.0)),                  # triangle, uneven steps
    ([(30, 60), (450, 40), (420, 460), (60, 430)], (10.0, 10.0)),        # across a 500 px frame
    ([(0, 0), (90, 3), (95, 11), (4, 9), (50, 5)], (2.5, 0.5)),          # thin sliver, inner point
])
def test_hull_fill_visits_raxs_spots_snaking_like_the_squares(hull, step):
    """Harm stopped: the hull fill ablates a different set of spots than RaX's loop, jumps back
    after each column (one long stage move per column, which the squares never make), or runs
    its columns the other way round from the squares. Same spots as RaX's loop, column by
    column like the squares: from the top row (largest y) down the first, back up the next."""
    spec = RasterSpec(kind="hull", hull_points=hull, xstep=step[0], ystep=step[1])
    columns = {}
    for p in _old_hull_loop(hull, *step):          # x outer, y inner (smallest y first)
        columns.setdefault(p[0], []).append(p)
    snake = [p for i, col in enumerate(columns.values()) for p in (col[::-1] if i % 2 == 0 else col)]
    assert points_from_spec(spec) == snake
    square = points_from_spec(RasterSpec(kind="square_y", bounds=(0, 10, 0, 10), xstep=5, ystep=5))
    for path in (square, snake):                   # both run their first column down from the top
        first = [y for x, y in path if x == path[0][0]]
        assert first == sorted(first, reverse=True)


def test_square_patterns_snake_from_the_top_left_corner():
    """Harm stopped: the square patterns visit the grid in another order than drawn (an extra
    long move back at every row, and spots in the shot records that do not match the preview).
    X-priority runs row by row from (xmin, ymax), reversing each row; Y-priority column by column."""
    box = (0.0, 20.0, 0.0, 10.0)
    sx = points_from_spec(RasterSpec(kind="square_x", bounds=box, xstep=10.0, ystep=10.0))
    sy = points_from_spec(RasterSpec(kind="square_y", bounds=box, xstep=10.0, ystep=10.0))
    assert sx == [(0, 10), (10, 10), (20, 10), (20, 0), (10, 0), (0, 0)]
    assert sy == [(0, 10), (0, 0), (10, 0), (10, 10), (20, 10), (20, 0)]


def test_spiral_visits_each_spot_once_and_stays_in_the_scan_bounds():
    """Harm stopped: the spiral ablates the first spot of every ring twice in a row, or puts
    dozens of shots on the centre, or leaves the scan bounds the operator drew. Each spot comes
    once; the last ring (radius 0) is the centre once; nothing lies outside the bounds."""
    spec = RasterSpec(kind="spiral", origin=(100.0, 100.0), radius=20.0, step=10.0, angle_step=np.pi / 2,
                      bounds=(0.0, 200.0, 0.0, 200.0))
    pts = points_from_spec(spec)
    assert pts[0] == (120.0, 100.0)
    assert len(set(pts)) == len(pts) and pts[-1] == (100.0, 100.0)
    assert len(pts) == 4 + 4 + 1
    clipped = points_from_spec(RasterSpec(kind="spiral", origin=(100.0, 100.0), radius=20.0, step=10.0,
                                          angle_step=np.pi / 2, bounds=(100.0, 200.0, 0.0, 200.0)))
    assert clipped and all(x >= 100.0 for x, _ in clipped)


@pytest.mark.parametrize("spec, words", [
    (RasterSpec(kind="hull", hull_points=[(0, 0), (10, 0)], xstep=1, ystep=1),
     "a convex hull needs at least 3 points"),
    (RasterSpec(kind="hull", hull_points=[(0, 0), (1, 1), (2, 2)], xstep=1, ystep=1),
     "the corners lie on one line"),
    (RasterSpec(kind="hull", hull_points=[(10, 10), (490, 20), (250, 480)], xstep=0.01, ystep=0.01),
     "hull grid too fine"),
    (RasterSpec(kind="square_x", bounds=(0, 3000, 0, 3000), xstep=1, ystep=1),
     f"more than {MAX_PATH_POINTS:,} (the window would freeze): use larger steps"),
    (RasterSpec(kind="square_y", bounds=(0, 10, 0, 10), xstep=0, ystep=1), "step x and step y must be > 0"),
    (RasterSpec(kind="square_x", bounds=None), "needs scan bounds"),
    (RasterSpec(kind="spiral", origin=(900, 900), radius=5, step=1, angle_step=0.5, bounds=(0, 10, 0, 10)),
     "the spiral pattern has no points"),
    (RasterSpec(kind="zigzag"), "unknown pattern 'zigzag'"),
])
def test_a_pattern_that_cannot_be_built_says_why_before_anything_moves(spec, words):
    """Harm stopped: a bad pattern freezes the GUI for minutes (a sub-pixel hull grid), arms
    silently cut short at a cap, or fails with a scipy error nobody can act on. Every such
    pattern raises ValueError with words that say what to change."""
    with pytest.raises(ValueError, match=words.replace("(", r"\(").replace(")", r"\)")):
        points_from_spec(spec)


def test_a_pattern_of_more_than_50000_points_is_built_whole():
    """Harm stopped: a fine grid over a large target (5 x 5 px over the whole 1440 x 1080 frame:
    62,713 points) was refused at 50,000 points, though only drawing that many is slow; the
    operator could not run it at all. It is built whole, in well under a second; only a pattern
    that would freeze the window (over MAX_PATH_POINTS, 2 million) cannot be built."""
    t0 = time.perf_counter()
    pts = points_from_spec(RasterSpec(kind="square_x", bounds=(0, 1440, 0, 1080), xstep=5, ystep=5))
    assert len(pts) == 289 * 217 and time.perf_counter() - t0 < 1.0
    assert MAX_PATH_POINTS == 2_000_000


@pytest.mark.parametrize("change, angle_step, words", [
    (-0.05, 0.2, "the angle step falls to 0 or below on turn 5 of 10"),      # 0.2 - 4 * 0.05 = 0
    (0.0, 0.00005, "the spiral needs about 1,256,641 angle steps"),          # 10 turns of 125 664, + centre
])
def test_a_spiral_that_would_freeze_the_window_is_refused_at_once(change, angle_step, words):
    """Harm stopped: an angle change that drives the angle step to 0, or a tiny angle step, made
    the spiral loop run millions of steps on the GUI thread (bounds that clip the spiral keep it
    from stopping at the point cap): the window froze for seconds, and during a continuous run
    Esc and Stop did nothing meanwhile. Such a spiral is refused at once (well under 0.1 s), with
    words that say what to change."""
    spec = RasterSpec(kind="spiral", origin=(500.0, 500.0), radius=200.0, step=20.0, angle_step=angle_step,
                      angle_step_change=change, bounds=(0.0, 10.0, 0.0, 10.0))
    t0 = time.perf_counter()
    with pytest.raises(ValueError, match=words):
        points_from_spec(spec)
    assert time.perf_counter() - t0 < 0.1


def test_nearest_point_ties_go_to_the_lowest_index():
    """Harm stopped: with ping-pong every spot is in the path twice; a click picks the way-back
    copy and the go-to sends the raster's cursor to the wrong half of the path. The nearest point
    wins and a tie goes to the lowest index; no points gives -1."""
    path = [(0.0, 0.0), (10.0, 0.0), (10.0, 0.0), (0.0, 0.0)]
    assert nearest_index(path, 9.0, 1.0) == 1
    assert nearest_index(path, 1.0, 0.0) == 0
    assert nearest_index([], 1.0, 1.0) == -1


def test_a_pattern_that_cannot_be_built_says_why_in_the_operators_words():
    """Harm stopped: swapped scan bounds ("x from" larger than "x to") said "check the step sizes",
    so the operator changed the steps; and three corners on one line showed raw Qhull text
    ("QH6154 Qhull precision error ..."). Each says what is wrong, in plain words."""
    with pytest.raises(ValueError) as e:
        points_from_spec(RasterSpec(kind="square_x", bounds=(700.0, 300.0, 400.0, 900.0), xstep=40, ystep=40))
    assert "x from is larger than x to" in str(e.value)
    with pytest.raises(ValueError) as e:
        points_from_spec(RasterSpec(kind="hull", xstep=10, ystep=10,
                                    hull_points=((0.0, 0.0), (10.0, 10.0), (20.0, 20.0))))
    assert "one line" in str(e.value) and "Q" not in str(e.value)
