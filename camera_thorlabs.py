"""Thorlabs Zelux camera: settings, the camera settings file, the camera and its thread.

Units: exposure in ms everywhere outside the SDK call; ROI in sensor px with an
absolute upper-left start (the settings file's Start X / Start Y mean the same).

Display geometry (rotation and mirror of the shown image) is stored in the same
settings file, so read_camera_ini() returns both together.

The SDK is imported only inside ThorCamera.open(), so importing this module
never loads a camera DLL (camera_sim.SimCamera stands in for --simulate).
"""
from __future__ import annotations

import configparser
import os
import threading
import time
from dataclasses import dataclass, replace
from typing import Optional, Tuple

import numpy as np
from PyQt5 import QtCore

# --------------------------------------------------------------------------
# Plain data
# --------------------------------------------------------------------------

_ROT_CANON = {0: 0, 1: 1, 2: 2, 3: -1}   # np.rot90 k as stored in files: 90 deg clockwise is -1


@dataclass(frozen=True)
class DisplayGeometry:
    """How the image is shown: rotation (np.rot90 k: 0 none, -1 90 deg clockwise,
    2 180 deg, 1 90 deg anticlockwise) then mirror. Image px coordinates (pattern,
    clicks, calibration) are those of the rotated frame; mirroring is view only."""
    rotation_k: int = 0
    flip_x: bool = False
    flip_y: bool = False

    def __post_init__(self):
        object.__setattr__(self, "rotation_k", _ROT_CANON[int(self.rotation_k) % 4])
        object.__setattr__(self, "flip_x", bool(self.flip_x))
        object.__setattr__(self, "flip_y", bool(self.flip_y))


@dataclass(frozen=True)
class ThorCamConfig:
    """What the camera opens with. serial_number and max_fps come from config.py;
    the rest is also in the camera settings file."""
    exposure_ms: float = 30.0
    gain: int = 0
    gamma: float = 1.0            # software display gamma, applied to each frame
    width: int = 1440             # ROI size [px]
    height: int = 1080
    start_x: int = 0              # ROI upper-left, absolute sensor px
    start_y: int = 0
    serial_number: str = ""       # empty: the first camera found
    max_fps: float = 15.0         # soft limit of the grab loop [frames/s]

    @classmethod
    def from_app(cls, cam) -> "ThorCamConfig":
        """From config.CameraConfig (the values used when no settings file loads)."""
        return cls(exposure_ms=float(cam.exposure_ms), gain=int(cam.gain), gamma=float(cam.gamma),
                   width=int(cam.width), height=int(cam.height),
                   start_x=int(cam.start_x), start_y=int(cam.start_y),
                   serial_number=str(cam.serial_number), max_fps=float(cam.max_fps))


@dataclass(frozen=True)
class CameraInfo:
    """What the camera reports: ranges and the values it actually runs with."""
    sensor_width: int
    sensor_height: int
    exposure_ms: float
    exposure_min_ms: float
    exposure_max_ms: float
    gain: int
    gain_min: int
    gain_max: int
    gamma: float
    roi_x: int
    roi_y: int
    roi_width: int
    roi_height: int


def clamp_roi(width: int, height: int, start_x: int, start_y: int,
              sensor_w: int, sensor_h: int) -> Tuple[int, int, int, int]:
    """(x, y, w, h): size cut to the sensor, absolute start moved so the ROI stays
    on the sensor. Shared by ThorCamera and SimCamera so both place it the same."""
    w = max(1, min(int(width), int(sensor_w)))
    h = max(1, min(int(height), int(sensor_h)))
    x = max(0, min(int(start_x), int(sensor_w) - w))
    y = max(0, min(int(start_y), int(sensor_h) - h))
    return x, y, w, h


def gamma_lut(gamma: float) -> Optional[np.ndarray]:
    """8-bit lookup table for a display gamma, or None for gamma 1 (no change)."""
    gamma = float(gamma)
    if gamma <= 0 or gamma == 1.0:
        return None
    x = np.arange(256, dtype=np.float64) / 255.0
    return np.minimum(255, np.floor(x ** (1.0 / gamma) * 255.0)).astype(np.uint8)


# --------------------------------------------------------------------------
# Camera settings file (.ini, compatible with the old uEye Cockpit export)
# --------------------------------------------------------------------------

def read_camera_ini(path: str, defaults: ThorCamConfig = ThorCamConfig(),
                    geometry: DisplayGeometry = DisplayGeometry()
                    ) -> Tuple[ThorCamConfig, DisplayGeometry]:
    """Camera settings and display geometry from a settings file. A key the file
    lacks keeps the value in `defaults` / `geometry`. Rules kept from the lab copy:
    Start X / Start Y are absolute sensor px; a non-numeric Gamma falls back to 1.0
    (it is display only, and must not throw away the ROI and exposure)."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"camera settings file not found: {path}")
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8-sig")
    d = defaults
    try:
        gamma = cp.getfloat("Parameters", "Gamma", fallback=d.gamma)
    except ValueError:
        gamma = 1.0
    cfg = replace(
        d,
        width=cp.getint("Image size", "Width", fallback=d.width),
        height=cp.getint("Image size", "Height", fallback=d.height),
        start_x=cp.getint("Image size", "Start X", fallback=d.start_x),
        start_y=cp.getint("Image size", "Start Y", fallback=d.start_y),
        exposure_ms=cp.getfloat("Timing", "Exposure", fallback=d.exposure_ms),
        gain=cp.getint("Gain", "Master", fallback=d.gain),
        gamma=gamma if gamma > 0 else 1.0,
    )
    geom = DisplayGeometry(
        rotation_k=cp.getint("Display", "rotation_k", fallback=geometry.rotation_k),
        flip_x=cp.getboolean("Display", "flip_x", fallback=geometry.flip_x),
        flip_y=cp.getboolean("Display", "flip_y", fallback=geometry.flip_y),
    )
    return cfg, geom


def save_camera_ini(path: str, cfg: ThorCamConfig, geometry: DisplayGeometry) -> None:
    """Write camera settings and display geometry; other sections of an existing
    file (the uEye export's extras) are kept."""
    cp = configparser.ConfigParser(interpolation=None)
    if os.path.isfile(path):
        cp.read(path, encoding="utf-8-sig")
    for section in ("Image size", "Timing", "Gain", "Parameters", "Display"):
        if not cp.has_section(section):
            cp.add_section(section)
    cp.set("Image size", "Width", str(int(cfg.width)))
    cp.set("Image size", "Height", str(int(cfg.height)))
    cp.set("Image size", "Start X", str(int(cfg.start_x)))
    cp.set("Image size", "Start Y", str(int(cfg.start_y)))
    cp.set("Timing", "Exposure", f"{float(cfg.exposure_ms):.6f}")
    cp.set("Gain", "Master", str(int(cfg.gain)))
    cp.set("Parameters", "Gamma", f"{float(cfg.gamma):.6f}")
    cp.set("Display", "rotation_k", str(int(geometry.rotation_k)))
    cp.set("Display", "flip_x", str(int(geometry.flip_x)))
    cp.set("Display", "flip_y", str(int(geometry.flip_y)))
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)


# --------------------------------------------------------------------------
# The camera
# --------------------------------------------------------------------------

class ThorCamera:
    """Thorlabs TSI SDK camera. Same calls as camera_sim.SimCamera:
    open, close, grab, set_exposure_ms, set_gain, set_gamma, set_roi, info."""

    def __init__(self, cfg: ThorCamConfig):
        self.cfg = cfg
        self._sdk = None
        self._cam = None
        self._opened = False
        self._sensor_w = 0
        self._sensor_h = 0
        self._live_w = int(cfg.width)
        self._live_h = int(cfg.height)
        self._gamma = float(cfg.gamma)
        self._lut = gamma_lut(self._gamma)

    @property
    def live_width(self) -> int:
        return self._live_w

    @property
    def live_height(self) -> int:
        return self._live_h

    def open(self) -> None:
        from thorlabs_tsi_sdk.tl_camera import TLCameraSDK      # here: --simulate never loads the SDK
        from thorlabs_tsi_sdk.tl_camera_enums import OPERATION_MODE
        self._sdk = TLCameraSDK()
        cameras = self._sdk.discover_available_cameras()
        if not cameras:
            raise RuntimeError("no Thorlabs camera found")
        handle = self._sdk.open_camera(self.cfg.serial_number or cameras[0])
        handle.operation_mode = OPERATION_MODE.SOFTWARE_TRIGGERED
        self._configure(handle)

    def _configure(self, handle) -> None:
        """ROI, gain, arm, then exposure (the SDK takes exposure after arming)."""
        self._cam = handle
        self._sensor_w = handle.sensor_width_pixels
        self._sensor_h = handle.sensor_height_pixels
        self._write_roi(self.cfg.width, self.cfg.height, self.cfg.start_x, self.cfg.start_y)
        self.set_gain(self.cfg.gain)
        handle.frames_per_trigger_zero_for_unlimited = 0
        handle.arm(2)
        self.set_exposure_ms(self.cfg.exposure_ms)
        handle.issue_software_trigger()
        self._opened = True

    def _write_roi(self, width, height, start_x, start_y) -> None:
        x, y, w, h = clamp_roi(width, height, start_x, start_y, self._sensor_w, self._sensor_h)
        # SDK ROI corners are inclusive pixels
        self._cam.roi = self._cam.roi.__class__(x, y, x + w - 1, y + h - 1)
        r = self._cam.roi
        self._live_w = r.lower_right_x_pixels - r.upper_left_x_pixels + 1
        self._live_h = r.lower_right_y_pixels - r.upper_left_y_pixels + 1

    def set_roi(self, width: int, height: int, start_x: int, start_y: int) -> None:
        """New ROI (absolute start, clamped to the sensor). The camera is re-armed
        even if the ROI set fails, so a bad ROI never leaves it disarmed."""
        self._cam.disarm()
        try:
            self._write_roi(width, height, start_x, start_y)
        finally:
            self._cam.arm(2)
            self._cam.issue_software_trigger()

    def set_exposure_ms(self, ms: float) -> float:
        """Set exposure, clamped to the camera range; returns what the camera took [ms]."""
        rng = self._cam.exposure_time_range_us
        us = max(rng.min, min(rng.max, float(ms) * 1000.0))
        self._cam.exposure_time_us = int(us)
        return self._cam.exposure_time_us / 1000.0

    def set_gain(self, gain: int) -> int:
        rng = self._cam.gain_range
        self._cam.gain = max(rng.min, min(rng.max, int(gain)))
        return int(self._cam.gain)

    def set_gamma(self, gamma: float) -> float:
        """Software gamma: the lookup table is built here, once per change."""
        self._gamma = float(gamma) if gamma > 0 else 1.0
        self._lut = gamma_lut(self._gamma)
        return self._gamma

    def info(self) -> CameraInfo:
        c = self._cam
        r = c.roi
        exp_rng, gain_rng = c.exposure_time_range_us, c.gain_range
        return CameraInfo(
            sensor_width=int(self._sensor_w), sensor_height=int(self._sensor_h),
            exposure_ms=c.exposure_time_us / 1000.0,
            exposure_min_ms=exp_rng.min / 1000.0, exposure_max_ms=exp_rng.max / 1000.0,
            gain=int(c.gain), gain_min=int(gain_rng.min), gain_max=int(gain_rng.max),
            gamma=self._gamma,
            roi_x=int(r.upper_left_x_pixels), roi_y=int(r.upper_left_y_pixels),
            roi_width=int(self._live_w), roi_height=int(self._live_h))

    def grab(self) -> np.ndarray:
        if not self._opened:
            raise RuntimeError("camera not open")
        frame_data = self._cam.get_pending_frame_or_null()
        if frame_data is None:
            self._cam.issue_software_trigger()
            deadline = time.time() + 2.0
            while frame_data is None and time.time() < deadline:
                time.sleep(0.005)
                frame_data = self._cam.get_pending_frame_or_null()
        if frame_data is None:
            raise RuntimeError("no frame within 2 s")
        raw = np.asarray(frame_data.image_buffer).reshape(self._live_h, self._live_w)
        frame = (raw >> 2).astype(np.uint8)          # 10-bit Zelux pixels to 8 bit
        return frame if self._lut is None else self._lut[frame]

    def close(self) -> None:
        if not self._opened:
            return
        for step in (lambda: self._cam.disarm(), lambda: self._cam.dispose(),
                     lambda: self._sdk.dispose()):
            try:
                step()
            except Exception:
                pass
        self._opened = False


# --------------------------------------------------------------------------
# Newest-frame slot and the camera thread
# --------------------------------------------------------------------------

class FrameSlot:
    """One-frame mailbox from the camera thread to the GUI: put() replaces the
    frame, take() hands out the newest one once. A busy GUI skips frames instead
    of queueing them (each frame is ~1.5 MB)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self.skipped = 0              # frames replaced before the GUI took them

    def put(self, frame: np.ndarray) -> None:
        with self._lock:
            if self._frame is not None:
                self.skipped += 1
            self._frame = frame

    def take(self) -> Optional[np.ndarray]:
        with self._lock:
            frame, self._frame = self._frame, None
        return frame


class ThorCameraThread(QtCore.QThread):
    """Runs one camera (ThorCamera, or camera_sim.SimCamera offline) in its own
    thread. The GUI pulls frames with take_frame(); settings go in through the
    set_* calls and come back as `applied` (the value the camera took)."""

    info = QtCore.pyqtSignal(object)          # CameraInfo: at open and after a ROI change
    applied = QtCore.pyqtSignal(str, float)   # "exposure_ms" | "gain" | "gamma", value taken
    fps = QtCore.pyqtSignal(float)            # frames grabbed per second, about once a second
    message = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(str)

    def __init__(self, cfg: Optional[ThorCamConfig] = None, camera_class=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg or ThorCamConfig()
        self._camera_class = camera_class or ThorCamera
        self._cam = None
        self._slot = FrameSlot()
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._running = False

    # --- called from the GUI thread ---
    def take_frame(self) -> Optional[np.ndarray]:
        """Newest frame not yet taken, or None."""
        return self._slot.take()

    def set_exposure_ms(self, ms: float) -> None:
        self._queue("exposure_ms", float(ms))

    def set_gain(self, gain: int) -> None:
        self._queue("gain", int(gain))

    def set_gamma(self, gamma: float) -> None:
        self._queue("gamma", float(gamma))

    def set_roi(self, width: int, height: int, start_x: int, start_y: int) -> None:
        self._queue("roi", (int(width), int(height), int(start_x), int(start_y)))

    def apply_config(self, cfg: ThorCamConfig) -> None:
        """Exposure, gain, gamma and ROI of a loaded settings file, in one go."""
        with self._lock:
            self._pending.update(exposure_ms=float(cfg.exposure_ms), gain=int(cfg.gain),
                                 gamma=float(cfg.gamma),
                                 roi=(int(cfg.width), int(cfg.height), int(cfg.start_x), int(cfg.start_y)))

    def request_info(self) -> None:
        self._queue("info", True)

    def stop(self) -> None:
        self._running = False
        self.requestInterruption()

    def _queue(self, key, value) -> None:
        with self._lock:
            self._pending[key] = value

    # --- camera thread ---
    def _apply_pending(self) -> None:
        """Apply queued settings; send back only what changed (full info on ROI)."""
        with self._lock:
            pending, self._pending = self._pending, {}
        cam = self._cam
        send_info = bool(pending.get("info"))
        if "roi" in pending:
            w, h, x, y = pending["roi"]
            try:
                cam.set_roi(w, h, x, y)
                send_info = True
                self.message.emit(f"Camera region: {cam.live_width} x {cam.live_height} px at ({x}, {y}) px")
            except Exception as e:
                self.error.emit(f"Camera region not set: {e}")
        for key, setter in (("exposure_ms", cam.set_exposure_ms), ("gain", cam.set_gain),
                            ("gamma", cam.set_gamma)):
            if key in pending:
                try:
                    self.applied.emit(key, float(setter(pending[key])))
                except Exception as e:
                    self.error.emit(f"Camera {key.replace('_ms', '')} not set: {e}")
        if send_info:
            try:
                self.info.emit(cam.info())
            except Exception as e:
                self.error.emit(f"Camera info not read: {e}")

    def run(self) -> None:
        self._running = True
        self._cam = self._camera_class(self.cfg)
        try:
            self._cam.open()
        except Exception as e:
            self.error.emit(f"Camera did not open: {e}")
            return
        self.message.emit(f"Camera open ({type(self._cam).__name__}).")
        try:
            self.info.emit(self._cam.info())
        except Exception as e:
            self.error.emit(f"Camera info not read: {e}")

        dt_min = 1.0 / max(float(self.cfg.max_fps), 0.1)    # soft frame-rate limit
        next_grab = 0.0
        n, t_window = 0, None            # frame intervals counted since the window's first frame
        try:
            while self._running and not self.isInterruptionRequested():
                self._apply_pending()
                wait = next_grab - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
                next_grab = time.perf_counter() + dt_min
                try:
                    frame = self._cam.grab()
                except Exception as e:
                    self.error.emit(f"Camera frame not grabbed: {e}")
                    time.sleep(0.5)      # a failing grab returns at once; do not flood the log
                    continue
                self._slot.put(frame)

                now = time.perf_counter()
                if t_window is None:
                    t_window = now
                else:
                    n += 1
                    if now - t_window >= 1.0:
                        self.fps.emit(n / (now - t_window))
                        n, t_window = 0, now
        finally:
            try:
                self._cam.close()
            except Exception:
                pass
            self.message.emit("Camera closed.")
