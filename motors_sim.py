"""A stand-in for one Z912 stage axis (main_rastering.py --simulate): no hardware, no pythonnet."""
from __future__ import annotations

import time


class SimulatedMotor:
    """Moves at `speed_mm_s` (so a simulated run paces like the real stage); home is 0 mm."""

    def __init__(self, name: str, *, position_mm: float = 0.0, speed_mm_s: float = 2.0):
        self.name = name
        self._pos = float(position_mm)
        self._speed = float(speed_mm_s)
        self._backlash = 0.0

    def get_position(self) -> float:
        return self._pos

    def move_to(self, pos: float) -> float:
        time.sleep(0.02 + abs(float(pos) - self._pos) / self._speed)
        self._pos = float(pos)
        return self._pos

    def home(self) -> float:
        return self.move_to(0.0)

    def set_backlash(self, value: float) -> float:
        self._backlash = float(value)
        return self._backlash

    def get_backlash(self) -> float:
        return self._backlash

    def disconnect(self) -> None:
        pass
