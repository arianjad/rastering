"""A fake camera with the same calls as camera_thorlabs.ThorCamera, for running with no hardware.

Shows a round target on a dark background with a little noise, so the window,
overlays and frame rate look alive. Used when the GUI starts with --simulate.
"""
from __future__ import annotations

import numpy as np

from camera_thorlabs import CameraInfo, ThorCamConfig, clamp_roi, gamma_lut


class SimCamera:
    SENSOR_W, SENSOR_H = 1440, 1080
    GAIN_MAX = 48
    EXPOSURE_MIN_MS, EXPOSURE_MAX_MS = 0.04, 10_000.0

    def __init__(self, cfg: ThorCamConfig):
        self.cfg = cfg
        self._gain = int(cfg.gain)
        self._gamma = float(cfg.gamma)
        self._lut = gamma_lut(self._gamma)
        self._exposure_ms = self._exposure0_ms = max(self.EXPOSURE_MIN_MS, float(cfg.exposure_ms))
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
        self.set_roi(self.cfg.width, self.cfg.height, self.cfg.start_x, self.cfg.start_y)

    def close(self) -> None:
        pass

    def set_roi(self, width: int, height: int, start_x: int, start_y: int) -> None:
        self._roi = clamp_roi(width, height, start_x, start_y, self.SENSOR_W, self.SENSOR_H)
        self._render()

    def set_exposure_ms(self, ms: float) -> float:
        self._exposure_ms = max(self.EXPOSURE_MIN_MS, min(self.EXPOSURE_MAX_MS, float(ms)))
        self._render()
        return self._exposure_ms

    def set_gain(self, gain: int) -> int:
        self._gain = max(0, min(self.GAIN_MAX, int(gain)))
        self._render()
        return self._gain

    def set_gamma(self, gamma: float) -> float:
        self._gamma = float(gamma) if gamma > 0 else 1.0
        self._lut = gamma_lut(self._gamma)
        return self._gamma

    def info(self) -> CameraInfo:
        x, y, w, h = self._roi
        return CameraInfo(
            sensor_width=self.SENSOR_W, sensor_height=self.SENSOR_H,
            exposure_ms=self._exposure_ms, exposure_min_ms=self.EXPOSURE_MIN_MS,
            exposure_max_ms=self.EXPOSURE_MAX_MS,
            gain=self._gain, gain_min=0, gain_max=self.GAIN_MAX, gamma=self._gamma,
            roi_x=x, roi_y=y, roi_width=w, roi_height=h)

    def grab(self) -> np.ndarray:
        frame = self._frames[self._i % len(self._frames)]
        self._i += 1
        return frame if self._lut is None else self._lut[frame]

    def _render(self) -> None:
        """Draw the target once, plus four noise variants to cycle through."""
        x0, y0, w, h = self._roi
        yy, xx = np.mgrid[y0:y0 + h, x0:x0 + w].astype(np.float32)
        cx, cy = self.SENSOR_W * 0.52, self.SENSOR_H * 0.48
        r = np.hypot(xx - cx, yy - cy)
        target = np.where(r < 380, 0.40, 0.08)                               # the target disc
        target += 0.10 * np.sin(xx / 23.0) * np.sin(yy / 31.0) * (r < 380)   # surface texture
        target += np.where(np.abs(r - 230) < 4, 0.2, 0.0)                    # an old ablation ring
        # Brightness scales with exposure and gain, relative to the values at open.
        brightness = (self._exposure_ms / self._exposure0_ms) * (1.0 + self._gain / 48.0)
        base = np.clip(target * brightness, 0.0, 1.0)
        rng = np.random.default_rng(0)
        self._frames = [
            np.clip(base * 255 + rng.normal(0, 4, base.shape), 0, 255).astype(np.uint8)
            for _ in range(4)
        ]
