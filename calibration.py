"""Camera pixels to stage millimetres: the affine fit, its checks, and the calibration file.

    motor [mm] = M @ image [px] + b

No Qt. The file is JSON with the keys calibration_matrix, calibration_offset (required) and
user_home {x, y} [mm], backlash {x, y} [mm], camera_settings (the camera geometry the calibration
was made with; kept as read and written back, numpy numbers as plain ones), saved_at, notes. A
file with only the matrix and offset (the older format) reads fine. last_calibration_state.json
next to this file points at the calibration file used last. Both files are written whole or not
at all (a temporary file in the same folder, then renamed over the old one).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np

XY = Tuple[float, float]

HERE = os.path.dirname(os.path.abspath(__file__))
LAST_CALIBRATION_POINTER = os.path.join(HERE, "last_calibration_state.json")

# A fit is refused unless the stage moved at least this far in x and in y between the clicks
# [mm]: with less, the fit has no information on that axis (a stage moved in x only gave
# "scale 1.9e-09 mm/px").
MIN_STAGE_SPAN_MM = 0.010
# A fit or a calibration file above this condition number (largest / smallest singular value) is
# used with a warning. Camera pixels are square and the stage axes square to each other, so a real
# fit is near 1 (the lab's is 1.4; a camera viewing the target at 80 deg would give 6); far above
# means the stage positions lay (almost) on one line.
MAX_MATRIX_COND = 10.0
# Above this (or not finite) the matrix has no inverse worth the name: never usable.
SINGULAR_COND = 1e12


class UnusableMatrix(ValueError):
    """A px -> mm matrix that cannot be used. The message speaks of the clicks (a fit);
    file_words say the same of a matrix read from a file."""

    def __init__(self, words: str, file_words: str):
        super().__init__(words)
        self.file_words = file_words


class AffineCalibration:
    """motor [mm] = M @ image [px] + b. Read-only once made. Refused (UnusableMatrix) when M is not
    finite, singular, or its condition number `cond` is above max_cond (None: no such limit)."""

    def __init__(self, M, b, max_cond: Optional[float] = MAX_MATRIX_COND):
        self.M = np.array(M, dtype=float).reshape(2, 2)
        self.b = np.array(b, dtype=float).reshape(2)
        finite = bool(np.all(np.isfinite(self.M)) and np.all(np.isfinite(self.b)))
        with np.errstate(divide="ignore"):
            cond = float(np.linalg.cond(self.M)) if finite else float("inf")
        self.cond = cond
        limit = SINGULAR_COND if max_cond is None else min(float(max_cond), SINGULAR_COND)
        if not cond <= limit:
            raise UnusableMatrix(
                f"the stage positions at the clicks lie (almost) on one line (matrix condition {cond:.3g}, more "
                f"than {limit:g}): move the stage in both x and y between clicks, so the spots form a triangle",
                "it holds a value that is not a number" if not finite else
                "it maps the image onto a line (singular)" if not cond <= SINGULAR_COND else
                f"its condition number is {cond:.3g}, more than {limit:g}: it maps the image onto (almost) a line")
        self._Minv = np.linalg.inv(self.M)
        for a in (self.M, self.b, self._Minv):
            a.setflags(write=False)

    def px_to_mm_many(self, points) -> np.ndarray:
        """(N, 2) image points [px] -> (N, 2) motor [mm]. Element by element, so one point gives
        the same bits whether it comes alone or in a batch (the travel filter and the move check
        agree exactly)."""
        p = np.asarray(points, dtype=float).reshape(-1, 2)
        x, y = p[:, 0], p[:, 1]
        M, b = self.M, self.b
        return np.column_stack([M[0, 0] * x + M[0, 1] * y + b[0], M[1, 0] * x + M[1, 1] * y + b[1]])

    def px_to_mm(self, x: float, y: float) -> XY:
        mx, my = self.px_to_mm_many([(x, y)])[0]
        return float(mx), float(my)

    def mm_to_px(self, mx: float, my: float) -> XY:
        v = self._Minv @ (np.array([mx, my], dtype=float) - self.b)
        return float(v[0]), float(v[1])

    def to_json(self) -> Dict[str, Any]:
        return {"calibration_matrix": self.M.tolist(), "calibration_offset": self.b.tolist()}


def fit_affine(px_points: Sequence[XY], mm_points: Sequence[XY]) -> Tuple[AffineCalibration, str]:
    """Least-squares affine fit of clicked spots [px] to stage positions [mm]. Returns the
    calibration and one line for the log: with 4 or more spots it holds the fit error [um] (how far
    the fit puts each clicked spot from its stage position); 3 spots fit exactly, so a mis-click
    shows only from the 4th spot on. Raises ValueError (plain words) when it cannot fit."""
    n = len(px_points)
    if n < 3 or len(mm_points) != n:
        raise ValueError(f"need at least 3 spots with a stage position each (have {n})")
    span = np.ptp(np.asarray(mm_points, dtype=float), axis=0)
    if span.min() < MIN_STAGE_SPAN_MM:
        raise ValueError(f"the stage moved only {span[0] * 1000:.1f} um in x and {span[1] * 1000:.1f} um in y "
                         f"between the clicks: move the stage in both x and y between clicks (at least "
                         f"{MIN_STAGE_SPAN_MM * 1000:g} um each)")
    A = np.zeros((2 * n, 6))
    rhs = np.zeros(2 * n)
    for i, ((x, y), (mx, my)) in enumerate(zip(px_points, mm_points)):
        A[2 * i, 0:3] = (x, y, 1.0)
        A[2 * i + 1, 3:6] = (x, y, 1.0)
        rhs[2 * i], rhs[2 * i + 1] = mx, my
    params, _, rank, _sv = np.linalg.lstsq(A, rhs, rcond=None)
    if rank < 6:
        raise ValueError("the clicked spots lie on one line: click spots that form a triangle")
    cal = AffineCalibration([[params[0], params[1]], [params[3], params[4]]], [params[2], params[5]],
                            max_cond=None)      # above MAX_MATRIX_COND: used, the controller warns
    scale = float(np.sqrt(abs(np.linalg.det(cal.M))))
    words = f"{n} spots, scale {scale:.4g} mm/px"
    if n > 3:
        err_um = 1000.0 * np.hypot(*(cal.px_to_mm_many(px_points) - np.asarray(mm_points, dtype=float)).T)
        words += f", fit error {np.sqrt(np.mean(err_um ** 2)):.1f} um rms, {err_um.max():.1f} um worst spot"
    return cal, words


@dataclass(frozen=True)
class CalibrationFile:
    """A calibration with what is stored next to it. path: the file it was read from or saved to."""
    affine: AffineCalibration
    user_home_mm: Optional[XY] = None
    backlash_mm: Tuple[Optional[float], Optional[float]] = (None, None)
    camera_settings: Optional[Dict[str, Any]] = None
    notes: str = ""
    saved_at: str = ""
    path: Optional[str] = None


def read_calibration_file(path: str) -> CalibrationFile:
    """Read a calibration file (or follow last_calibration_state.json to the one it names).
    Raises OSError or ValueError with the file name in the words. A matrix above MAX_MATRIX_COND is
    read (affine.cond says so); a singular or non-finite one is not."""
    with open(path, "r") as f:
        data = json.load(f)
    target = data.get("last_calibration_path") if isinstance(data, dict) else None
    if isinstance(target, str) and "calibration_matrix" not in data:
        path = target
        with open(path, "r") as f:
            data = json.load(f)
    if not isinstance(data, dict) or "calibration_matrix" not in data or "calibration_offset" not in data:
        raise ValueError(f"{path} is not a calibration file (no calibration_matrix / calibration_offset)")
    uh = data.get("user_home")
    bl = data.get("backlash") if isinstance(data.get("backlash"), dict) else {}
    cs = data.get("camera_settings")
    try:      # a file is used even above MAX_MATRIX_COND (the controller warns); never when singular
        affine = AffineCalibration(data["calibration_matrix"], data["calibration_offset"], max_cond=None)
    except UnusableMatrix as e:
        raise ValueError(f"the calibration matrix in {path} cannot be used: {e.file_words}") from None
    return CalibrationFile(
        affine=affine,
        user_home_mm=(float(uh["x"]), float(uh["y"])) if isinstance(uh, dict) and "x" in uh and "y" in uh else None,
        backlash_mm=tuple(float(bl[k]) if bl.get(k) is not None else None for k in ("x", "y")),
        camera_settings=cs if isinstance(cs, dict) else None,
        notes=str(data.get("notes") or ""),
        saved_at=str(data.get("saved_at") or ""),
        path=os.path.abspath(path),
    )


def write_calibration_file(path: str, cal: CalibrationFile) -> CalibrationFile:
    """Write `cal` to `path` (stamped now); returns what was written, with its path. Raises
    OSError, or TypeError / ValueError for a value JSON cannot hold; the old file then stays."""
    saved_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    uh = cal.user_home_mm
    bx, by = (None if v is None else float(v) for v in cal.backlash_mm)
    camera = _plain(cal.camera_settings)
    data = {
        **cal.affine.to_json(),
        "user_home": {"x": float(uh[0]), "y": float(uh[1])} if uh is not None else None,
        "backlash": {"x": bx, "y": by},
        "camera_settings": camera,
        "saved_at": saved_at,
        "notes": cal.notes,
    }
    write_json_whole(path, data)
    return CalibrationFile(cal.affine, uh, (bx, by), camera, cal.notes, saved_at, os.path.abspath(path))


def _plain(value: Any) -> Any:
    """`value` with numpy numbers and arrays (at any depth in dicts, lists, tuples) as Python ones."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return _plain(value.tolist())
    if isinstance(value, np.generic):
        return value.item()
    return value


def write_json_whole(path: str, data: Any) -> None:
    """Write `data` as JSON to `path` whole or not at all: the text is made first, written to a
    temporary file in the same folder, then renamed over `path`."""
    text = json.dumps(data, indent=2)
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def camera_changed(now: Optional[Dict[str, Any]], saved: Optional[Dict[str, Any]]) -> bool:
    """True when a camera setting stored with the calibration (region, rotation, mirror) differs
    from the one in use now. A key missing on either side never counts (older files have none)."""
    if not now or not saved:
        return False
    for key, v in saved.items():
        if key not in now:
            continue
        if isinstance(v, dict) and isinstance(now[key], dict):
            if camera_changed(now[key], v):
                return True
        elif now[key] != v:
            return True
    return False


def remembered_calibration_path(pointer: str = LAST_CALIBRATION_POINTER) -> Optional[str]:
    """The calibration file the pointer names (it may since have been deleted or moved)."""
    try:
        with open(pointer, "r") as f:
            p = json.load(f).get("last_calibration_path")
    except (OSError, ValueError, AttributeError):
        return None
    return p if isinstance(p, str) else None


def remember_calibration_path(path: str, pointer: str = LAST_CALIBRATION_POINTER) -> None:
    """Point last_calibration_state.json at `path`. Raises OSError."""
    write_json_whole(pointer, {"last_calibration_path": os.path.abspath(path)})
