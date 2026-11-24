"""A stand-in for one Z912 stage axis (main_rastering.py --simulate): no hardware, no pythonnet."""
from __future__ import annotations

import time


class SimulatedMotor:
    """Moves at `speed_mm_s` (so a simulated run paces like the real stage); home is 0 mm. During a
    move its position changes along the way and is reported every `progress_s` [s], as a KCube's."""

    def __init__(self, name: str, *, position_mm: float = 0.0, speed_mm_s: float = 2.0,
                 progress_s: float = 0.2):
        self.name = name
        self._pos = float(position_mm)
        self._speed = float(speed_mm_s)
        self._progress_s = float(progress_s)
        self._backlash = 0.0

    def get_position(self) -> float:
        return self._pos

    def move_to(self, pos: float, on_progress=None) -> float:
        start, end = self._pos, float(pos)
        total = 0.02 + abs(end - start) / self._speed     # [s]
        t0 = time.monotonic()
        while True:
            left = total - (time.monotonic() - t0)
            if left <= 0:
                break
            time.sleep(min(left, self._progress_s))
            frac = min(1.0, (time.monotonic() - t0) / total)
            if frac < 1.0:
                self._pos = start + (end - start) * frac
                if on_progress is not None:
                    on_progress(self._pos)
        self._pos = end
        return self._pos

    def home(self, on_progress=None) -> float:
        return self.move_to(0.0, on_progress)

    def set_backlash(self, value: float) -> float:
        self._backlash = float(value)
        return self._backlash

    def get_backlash(self) -> float:
        return self._backlash

    def disconnect(self) -> None:
        pass
