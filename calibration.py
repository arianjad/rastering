"""Camera pixels to stage millimetres: the affine fit, its checks, and the calibration file.

    motor [mm] = M @ image [px] + b

No Qt. The file is JSON with the keys calibration_matrix, calibration_offset (required) and
user_home {x, y} [mm], backlash {x, y} [mm], camera_settings (the camera geometry the calibration
was made with; kept as read and written back untouched), saved_at, notes. A file with only the
matrix and offset (the older format) reads fine. last_calibration_state.json next to this file
points at the calibration file used last.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np

XY = Tuple[float, float]

HERE = os.path.dirname(os.path.abspath(__file__))
LAST_CALIBRATION_POINTER = os.path.join(HERE, "last_calibration_state.json")


class AffineCalibration:
    """motor [mm] = M @ image [px] + b. Read-only once made."""

    def __init__(self, M, b):
        self.M = np.array(M, dtype=float).reshape(2, 2)
        self.b = np.array(b, dtype=float).reshape(2)
        try:
            self._Minv = np.linalg.inv(self.M)
        except np.linalg.LinAlgError:
            raise ValueError("the calibration matrix is singular (the motor positions lie on one line)") from None
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
    calibration and one line for the log. Raises ValueError (plain words) when it cannot fit."""
    n = len(px_points)
    if n < 3 or len(mm_points) != n:
        raise ValueError(f"need at least 3 spots with a stage position each (have {n})")
    A = np.zeros((2 * n, 6))
    rhs = np.zeros(2 * n)
    for i, ((x, y), (mx, my)) in enumerate(zip(px_points, mm_points)):
        A[2 * i, 0:3] = (x, y, 1.0)
        A[2 * i + 1, 3:6] = (x, y, 1.0)
        rhs[2 * i], rhs[2 * i + 1] = mx, my
    params, _, rank, sv = np.linalg.lstsq(A, rhs, rcond=None)
    if rank < 6:
        raise ValueError("the clicked spots lie on one line: click spots that form a triangle")
    cal = AffineCalibration([[params[0], params[1]], [params[3], params[4]]], [params[2], params[5]])
    scale = float(np.sqrt(abs(np.linalg.det(cal.M))))
    return cal, f"{n} spots, scale {scale:.4g} mm/px, cond(A) {sv.max() / sv.min():.3g}"


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
    Raises OSError or ValueError with the file name in the words."""
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
    return CalibrationFile(
        affine=AffineCalibration(data["calibration_matrix"], data["calibration_offset"]),
        user_home_mm=(float(uh["x"]), float(uh["y"])) if isinstance(uh, dict) and "x" in uh and "y" in uh else None,
        backlash_mm=tuple(float(bl[k]) if bl.get(k) is not None else None for k in ("x", "y")),
        camera_settings=cs if isinstance(cs, dict) else None,
        notes=str(data.get("notes") or ""),
        saved_at=str(data.get("saved_at") or ""),
        path=os.path.abspath(path),
    )


def write_calibration_file(path: str, cal: CalibrationFile) -> CalibrationFile:
    """Write `cal` to `path` (stamped now); returns what was written, with its path."""
    saved_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    uh = cal.user_home_mm
    bx, by = cal.backlash_mm
    data = {
        **cal.affine.to_json(),
        "user_home": {"x": float(uh[0]), "y": float(uh[1])} if uh is not None else None,
        "backlash": {"x": bx, "y": by},
        "camera_settings": cal.camera_settings,
        "saved_at": saved_at,
        "notes": cal.notes,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return CalibrationFile(cal.affine, uh, (bx, by), cal.camera_settings, cal.notes, saved_at,
                           os.path.abspath(path))


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


def last_calibration_path(pointer: str = LAST_CALIBRATION_POINTER) -> Optional[str]:
    """The calibration file used last, if the pointer names one that still exists."""
    try:
        with open(pointer, "r") as f:
            p = json.load(f).get("last_calibration_path")
    except (OSError, ValueError, AttributeError):
        return None
    return p if isinstance(p, str) and os.path.exists(p) else None


def remember_calibration_path(path: str, pointer: str = LAST_CALIBRATION_POINTER) -> None:
    """Point last_calibration_state.json at `path`. Raises OSError."""
    with open(pointer, "w") as f:
        json.dump({"last_calibration_path": os.path.abspath(path)}, f, indent=2)
