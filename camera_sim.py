"""A fake camera with the same calls as ThorCamera, for running with no hardware.

Shows a still picture of a round target on a dark background with a little
moving noise, so the window, overlays and frame rate look alive. Used when the
GUI starts with --simulate.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np


class SimCamera:
    SENSOR_W, SENSOR_H = 1440, 1080

    def __init__(self, cfg):
        self.cfg = cfg
        self._gain = int(cfg.gain)
        self._gamma = float(cfg.gamma)
        self._exposure_ms = self._exposure0_ms = max(0.1, float(cfg.exposure_us) / 1000.0)
        self._roi = (0, 0, self.SENSOR_W, self.SENSOR_H)
        self._frames: list = []
        self._i = 0

    @property
    def live_width(self) -> int:
        return self._roi[2]

    @property
    def live_height(self) -> int:
        return self._roi[3]

    def open(self) -> None:
        self.reinit_roi(self.cfg.width, self.cfg.height,
                        self.cfg.roi_offset_x, self.cfg.roi_offset_y)

    def close(self) -> None:
        pass

    def reinit_roi(self, width: int, height: int, start_x: int, start_y: int) -> None:
        w = min(int(width), self.SENSOR_W)
        h = min(int(height), self.SENSOR_H)
        x = max(0, min(int(start_x), self.SENSOR_W - w))
        y = max(0, min(int(start_y), self.SENSOR_H - h))
        self._roi = (x, y, w, h)
        self._render()

    def set_exposure_ms(self, ms: float) -> None:
        self._exposure_ms = max(0.1, min(1000.0, float(ms)))
        self._render()

    def set_gain(self, gain: int) -> None:
        self._gain = max(0, min(100, int(gain)))
        self._render()

    def set_gamma(self, gamma: float) -> None:
        self._gamma = float(gamma)
        self._render()

    def get_camera_info(self) -> Dict[str, Any]:
        x, y, w, h = self._roi
        return {
            "sensor_width": self.SENSOR_W, "sensor_height": self.SENSOR_H,
            "exposure": self._exposure_ms, "exposure_min": 0.1,
            "exposure_max": 1000.0, "exposure_inc": 0.1,
            "gain": self._gain, "gain_min": 0, "gain_max": 100,
            "gamma": self._gamma, "gamma_min": 0.1, "gamma_max": 2.2,
            "aoi_x": x, "aoi_y": y, "aoi_width": w, "aoi_height": h,
            "pixel_clock": 0, "pixel_clocks": [0],
            "fps": round(1000.0 / self._exposure_ms, 2), "fps_min": 0.1, "fps_max": 100.0,
            "gain_boost": False,
        }

    def grab(self) -> np.ndarray:
        frame = self._frames[self._i % len(self._frames)]
        self._i += 1
        if self.cfg.emit_rgb:
            frame = np.dstack((frame, frame, frame))
        return frame

    def _render(self) -> None:
        """Draw the target once, plus four noise variants to cycle through."""
        x0, y0, w, h = self._roi
        yy, xx = np.mgrid[y0:y0 + h, x0:x0 + w].astype(np.float32)
        cx, cy = self.SENSOR_W * 0.52, self.SENSOR_H * 0.48
        r = np.hypot(xx - cx, yy - cy)
        target = np.where(r < 380, 0.40, 0.08)                  # the target disc
        target += 0.10 * np.sin(xx / 23.0) * np.sin(yy / 31.0) * (r < 380)  # surface texture
        target += np.where(np.abs(r - 230) < 4, 0.2, 0.0)       # an old ablation ring
        # Brightness scales with exposure and gain, relative to the values at open.
        brightness = (self._exposure_ms / self._exposure0_ms) * (1.0 + self._gain / 100.0)
        base = np.clip(target * brightness, 0.0, 1.0)
        if self._gamma != 1.0:
            base = base ** (1.0 / self._gamma)
        rng = np.random.default_rng(0)
        self._frames = [
            np.clip(base * 255 + rng.normal(0, 4, base.shape), 0, 255).astype(np.uint8)
            for _ in range(4)
        ]
