from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np
from PyQt5 import QtCore

from thorlabs_tsi_sdk.tl_camera import TLCameraSDK, TLCamera
from thorlabs_tsi_sdk.tl_camera_enums import OPERATION_MODE


@dataclass
class ThorCamConfig:
    serial_number: str = ""        # Leave empty to use first available camera
    width: int = 1440              # Zelux full width
    height: int = 1080             # Zelux full height
    exposure_us: float = 30000.0   # Exposure in MICROSECONDS (not ms like uEye)
    gain: int = 0                  # 0 = minimum gain
    max_fps: float = 15.0          # Soft throttle for the grab loop
    emit_rgb: bool = False         # Emit grayscale or RGB
    roi_offset_x: int = 0
    roi_offset_y: int = 0
    gamma: float = 1.0             # Software display gamma (applied in grab())

class ThorCamera:
    """
    Thin ThorLabs TSI SDK wrapper mimicking the UEyeCamera interface.
    Drop-in replacement for UEyeCamera.
    """
    def __init__(self, cfg: ThorCamConfig):
        self.cfg = cfg
        self._sdk: Optional[TLCameraSDK] = None
        self._cam: Optional[TLCamera] = None
        self._opened = False

        # Live dimensions (updated on ROI change)
        self._live_width = int(cfg.width)
        self._live_height = int(cfg.height)

        # Sensor max dimensions (populated in open())
        self._sensor_width = 0
        self._sensor_height = 0

        # Software gamma (ThorLabs has no hardware gamma)
        self._gamma: float = float(cfg.gamma)

    @property
    def live_width(self) -> int:
        return self._live_width

    @property
    def live_height(self) -> int:
        return self._live_height

    def open(self) -> None:
        self._sdk = TLCameraSDK()
        cameras = self._sdk.discover_available_cameras()
        if not cameras:
            raise RuntimeError("No ThorLabs cameras found")

        serial = self.cfg.serial_number if self.cfg.serial_number else cameras[0]
        self._cam = self._sdk.open_camera(serial)

        # Sensor dimensions
        self._sensor_width = self._cam.sensor_width_pixels
        self._sensor_height = self._cam.sensor_height_pixels

        # Continuous acquisition mode
        self._cam.operation_mode = OPERATION_MODE.SOFTWARE_TRIGGERED

        # ROI
        self._setup_roi(self.cfg.width, self.cfg.height,
                        self.cfg.roi_offset_x, self.cfg.roi_offset_y)

          # Gain
        self.set_gain(self.cfg.gain)

        # Arm FIRST, then set exposure
        self._cam.frames_per_trigger_zero_for_unlimited = 0
        self._cam.arm(2)

        # Exposure AFTER arming
        self.set_exposure_us(self.cfg.exposure_us)

        self._cam.issue_software_trigger()

        self._opened = True

    # ------------------------------------------------------------------
    # ROI helpers
    # ------------------------------------------------------------------

    def _setup_roi(self, width: int, height: int,
                   offset_x: int = 0, offset_y: int = 0) -> None:
        req_w = min(int(width), self._sensor_width)
        req_h = min(int(height), self._sensor_height)

        # Center + offset
        off_x = (self._sensor_width - req_w) // 2 - int(offset_x)
        off_y = (self._sensor_height - req_h) // 2 - int(offset_y)

        # Clamp
        off_x = max(0, min(off_x, self._sensor_width - req_w))
        off_y = max(0, min(off_y, self._sensor_height - req_h))

        self._cam.roi = self._cam.roi.__class__(off_x, off_y,
                                                off_x + req_w,
                                                off_y + req_h)
        actual = self._cam.roi
        self._live_width = actual.lower_right_x_pixels - actual.upper_left_x_pixels + 1
        self._live_height = actual.lower_right_y_pixels - actual.upper_left_y_pixels + 1

    def reinit_roi(self, width: int, height: int,
                   start_x: int, start_y: int) -> None:
        """Change the ROI at runtime. start_x/start_y are absolute (as in
        upstream UEyeCamera.reinit_aoi). BeamBox: clamped to the sensor like
        _setup_roi, and the camera is re-armed even if the ROI set fails --
        an unclamped .ini start (352 + 1440 > sensor width) used to raise
        after disarm() and leave the camera disarmed."""
        req_w = min(int(width), self._sensor_width)
        req_h = min(int(height), self._sensor_height)
        off_x = max(0, min(int(start_x), self._sensor_width - req_w))
        off_y = max(0, min(int(start_y), self._sensor_height - req_h))
        self._cam.disarm()
        try:
            self._cam.roi = self._cam.roi.__class__(off_x, off_y,
                                                    off_x + req_w,
                                                    off_y + req_h)
            actual = self._cam.roi
            self._live_width = actual.lower_right_x_pixels - actual.upper_left_x_pixels + 1
            self._live_height = actual.lower_right_y_pixels - actual.upper_left_y_pixels + 1
        finally:
            self._cam.arm(2)
            self._cam.issue_software_trigger()

    # ------------------------------------------------------------------
    # Runtime parameter setters
    # ------------------------------------------------------------------

    def set_exposure_us(self, exposure_us: float) -> None:
        """Exposure in microseconds."""
        exp = max(self._cam.exposure_time_range_us.min,
                  min(self._cam.exposure_time_range_us.max,
                      float(exposure_us)))
        self._cam.exposure_time_us = int(exp)

    def set_exposure_ms(self, exposure_ms: float) -> None:
        """Convenience wrapper — converts ms to us."""
        self.set_exposure_us(exposure_ms * 1000.0)

    def set_gain(self, gain: int) -> None:
        gain = max(self._cam.gain_range.min,
                   min(self._cam.gain_range.max, int(gain)))
        self._cam.gain = gain

    def set_gamma(self, gamma: float) -> None:
        """Software gamma — applied in grab() via numpy."""
        self._gamma = float(gamma)

    # ------------------------------------------------------------------
    # Camera info (mirrors UEyeCamera.get_camera_info)
    # ------------------------------------------------------------------

    def get_camera_info(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {}
        info["sensor_width"] = self._sensor_width
        info["sensor_height"] = self._sensor_height

        info["exposure"] = self._cam.exposure_time_us / 1000.0  # return in ms for UI
        info["exposure_min"] = self._cam.exposure_time_range_us.min / 1000.0
        info["exposure_max"] = self._cam.exposure_time_range_us.max / 1000.0
        info["exposure_inc"] = 0.1

        info["gain"] = self._cam.gain
        info["gain_min"] = self._cam.gain_range.min
        info["gain_max"] = self._cam.gain_range.max

        info["gamma"] = self._gamma
        info["gamma_min"] = 0.1
        info["gamma_max"] = 2.2

        info["aoi_x"] = self._cam.roi.upper_left_x_pixels
        info["aoi_y"] = self._cam.roi.upper_left_y_pixels
        info["aoi_width"] = self._live_width
        info["aoi_height"] = self._live_height

        # ThorLabs manages pixel clock internally — report 0
        info["pixel_clock"] = 0
        info["pixel_clocks"] = [0]

        # FPS estimate from exposure
        exp_s = self._cam.exposure_time_us / 1e6
        info["fps"] = round(1.0 / exp_s, 2) if exp_s > 0 else 0.0
        info["fps_min"] = 0.1
        info["fps_max"] = 100.0

        info["gain_boost"] = False  # Not available on Zelux

        return info

    # ------------------------------------------------------------------
    # Frame grab
    # ------------------------------------------------------------------

    def grab(self) -> np.ndarray:
        if not self._opened:
            raise RuntimeError("Camera not opened")

        frame_data = self._cam.get_pending_frame_or_null()

        if frame_data is None:
            self._cam.issue_software_trigger()
            timeout = time.time() + 2.0
            while time.time() < timeout:
                frame_data = self._cam.get_pending_frame_or_null()
                if frame_data is not None:
                    break
                time.sleep(0.005)

        if frame_data is None:
            raise RuntimeError("Frame grab timed out")

        frame = np.copy(frame_data.image_buffer).reshape(
            self._live_height, self._live_width
        )

        # Normalize 12-bit to 8-bit (Zelux is 10-bit)
        frame = (frame >> 2).astype(np.uint8)

        # Software gamma
        if self._gamma != 1.0:
            lut = np.array([
                min(255, int(((i / 255.0) ** (1.0 / self._gamma)) * 255))
                for i in range(256)
            ], dtype=np.uint8)
            frame = lut[frame]

        if self.cfg.emit_rgb:
            frame = np.dstack((frame, frame, frame))

        return frame

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        if not self._opened:
            return
        try:
            self._cam.disarm()
        except Exception:
            pass
        try:
            self._cam.dispose()
        except Exception:
            pass
        try:
            self._sdk.dispose()
        except Exception:
            pass
        self._opened = False
# =====================================================================
# Camera thread with unified pending-parameter pattern
# =====================================================================

class ThorCameraThread(QtCore.QThread):
    new_frame = QtCore.pyqtSignal(np.ndarray)
    status = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(str)
    camera_info_signal = QtCore.pyqtSignal(dict)

    def __init__(self, cfg: Optional[ThorCamConfig] = None, parent=None):
        super().__init__(parent)
        self.cfg = cfg or ThorCamConfig()
        self._cam: Optional[ThorCamera] = None
        self._running = False

        self._params_lock = QtCore.QMutex()
        self._pending: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Thread-safe parameter slots (called from UI thread)
    # ------------------------------------------------------------------

    @QtCore.pyqtSlot(float)
    def set_exposure_ms(self, v: float) -> None:
        with QtCore.QMutexLocker(self._params_lock):
            self._pending["exposure"] = float(v)

    @QtCore.pyqtSlot(int)
    def set_master_gain(self, v: int) -> None:
        with QtCore.QMutexLocker(self._params_lock):
            self._pending["gain"] = int(v)

    @QtCore.pyqtSlot(float)
    def set_gamma(self, v: float) -> None:
        with QtCore.QMutexLocker(self._params_lock):
            self._pending["gamma"] = float(v)

    @QtCore.pyqtSlot(bool)
    def set_gain_boost(self, v: bool) -> None:
        # Zelux has no gain boost — silently ignore
        pass

    @QtCore.pyqtSlot(int)
    def set_pixel_clock(self, v: int) -> None:
        # ThorLabs manages pixel clock internally — silently ignore
        pass

    @QtCore.pyqtSlot(float)
    def set_target_fps(self, v: float) -> None:
        # FPS is controlled via exposure on ThorLabs — silently ignore
        pass

    @QtCore.pyqtSlot(bool)
    def set_prioritize_exposure(self, v: bool) -> None:
        # Not needed on ThorLabs — silently ignore
        pass

    def request_ini_extras(self, ini_path: str) -> None:
        # uEye-only .ini extras (hot-pixel correction, hardware gamma). ui.py
        # calls this 2 s after start; the Zelux has none of them, and AOI,
        # gain and exposure already come from the .ini at open — silently ignore
        pass

    def request_aoi_change(self, width: int, height: int,
                           start_x: int, start_y: int) -> None:
        with QtCore.QMutexLocker(self._params_lock):
            self._pending["aoi"] = (int(width), int(height),
                                    int(start_x), int(start_y))

    def request_info_refresh(self) -> None:
        with QtCore.QMutexLocker(self._params_lock):
            self._pending["refresh_info"] = True

    def stop(self) -> None:
        self._running = False
        self.requestInterruption()

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._running = True
        self._cam = ThorCamera(self.cfg)
        try:
            self._cam.open()
            self.status.emit("ThorLabs camera opened.")
        except Exception as e:
            self.error.emit(f"Camera open failed: {e}")
            return

        try:
            self.camera_info_signal.emit(self._cam.get_camera_info())
        except Exception:
            pass

        dt_min = 1.0 / max(float(self.cfg.max_fps), 0.1)
        t_last = 0.0

        try:
            while self._running and not self.isInterruptionRequested():
                # ---- Apply all pending parameter changes ----
                with QtCore.QMutexLocker(self._params_lock):
                    pending = dict(self._pending)
                    self._pending.clear()

                need_info_update = False

                if "aoi" in pending:
                    w, h, sx, sy = pending["aoi"]
                    try:
                        self._cam.reinit_roi(w, h, sx, sy)
                        need_info_update = True
                        self.status.emit(
                            f"ROI: {self._cam.live_width}x{self._cam.live_height} at ({sx},{sy})")
                    except Exception as e:
                        self.error.emit(f"ROI change failed: {e}")

                if "gain" in pending:
                    try:
                        self._cam.set_gain(pending["gain"])
                        need_info_update = True
                    except Exception as e:
                        self.error.emit(f"Gain set failed: {e}")

                if "gamma" in pending:
                    try:
                        self._cam.set_gamma(pending["gamma"])
                    except Exception as e:
                        self.error.emit(f"Gamma set failed: {e}")

                if "exposure" in pending:
                    try:
                        self._cam.set_exposure_ms(pending["exposure"])
                        need_info_update = True
                    except Exception as e:
                        self.error.emit(f"Exposure set failed: {e}")

                if "refresh_info" in pending:
                    need_info_update = True

                if need_info_update:
                    try:
                        self.camera_info_signal.emit(self._cam.get_camera_info())
                    except Exception:
                        pass

                # ---- Grab frame ----
                try:
                    frame = self._cam.grab()
                except Exception as e:
                    self.error.emit(f"Frame grab failed: {e}")
                    # BeamBox: a grab that fails at once (camera disarmed)
                    # skipped the throttle below and flooded the log.
                    time.sleep(0.5)
                    continue

                self.new_frame.emit(frame)

                # ---- Soft throttle ----
                now = time.time()
                if t_last != 0.0:
                    dt = now - t_last
                    if dt < dt_min:
                        time.sleep(dt_min - dt)
                t_last = time.time()

        finally:
            try:
                self._cam.close()
            except Exception:
                pass
            self.status.emit("ThorLabs camera closed.")
    # =====================================================================
# .ini loader / saver  (ThorLabs edition)
# =====================================================================

def load_thorcam_config_from_ini(ini_path: str, **overrides) -> ThorCamConfig:
    """
    Parse a saved .ini file and return a ThorCamConfig.
    Compatible with the same .ini files used by the old uEye code,
    but ignores uEye-specific fields (pixel clock, gain boost, etc.).
    """
    import configparser

    cp = configparser.ConfigParser()
    cp.read(ini_path, encoding="utf-8-sig")

    def _get(section, key, fallback=None):
        try:
            return cp.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return fallback

    def _getint(section, key, fallback=0):
        v = _get(section, key)
        return int(v) if v is not None else fallback

    def _getfloat(section, key, fallback=0.0):
        v = _get(section, key)
        return float(v) if v is not None else fallback

    kwargs: dict = {}

    kwargs["width"] = _getint("Image size", "Width", 1440)
    kwargs["height"] = _getint("Image size", "Height", 1080)
    kwargs["roi_offset_x"] = _getint("Image size", "Start X", 0)
    kwargs["roi_offset_y"] = _getint("Image size", "Start Y", 0)

    # Exposure: old .ini stores ms, ThorCamConfig wants us
    exposure_ms = _getfloat("Timing", "Exposure", 30.0)
    kwargs["exposure_us"] = exposure_ms * 1000.0

    kwargs["gain"] = _getint("Gain", "Master", 0)

    # Software gamma, written by save_thorcam_settings_to_ini (read the same
    # way as upstream's load_ueye_config_from_ini).
    gamma = _getfloat("Parameters", "Gamma", 1.0)
    if gamma > 0:
        kwargs["gamma"] = gamma

    kwargs.update(overrides)
    return ThorCamConfig(**kwargs)


def save_thorcam_settings_to_ini(ini_path: str, settings: dict) -> None:
    """
    Write camera + display settings to an .ini file.

    `settings` keys (all optional, fall back to defaults):
        exposure_ms, gain, gamma,
        aoi_width, aoi_height, aoi_x, aoi_y,
        rotation_k, flip_x, flip_y
    """
    import configparser

    cp = configparser.ConfigParser()
    try:
        cp.read(ini_path, encoding="utf-8-sig")
    except Exception:
        pass

    for section in ("Image size", "Timing", "Gain", "Parameters", "Display"):
        if not cp.has_section(section):
            cp.add_section(section)

    # Image size
    cp.set("Image size", "Width",   str(settings.get("aoi_width", 1440)))
    cp.set("Image size", "Height",  str(settings.get("aoi_height", 1080)))
    cp.set("Image size", "Start X", str(settings.get("aoi_x", 0)))
    cp.set("Image size", "Start Y", str(settings.get("aoi_y", 0)))

    # Timing — store exposure in ms for human readability
    # The camera dock's get_current_settings() names it "exposure" (ms).
    exposure_ms = settings.get("exposure_ms", settings.get("exposure", 30.0))
    cp.set("Timing", "Exposure", f"{exposure_ms:.6f}")

    # Gain
    cp.set("Gain", "Master", str(settings.get("gain", 0)))

    # Parameters
    cp.set("Parameters", "Gamma", f"{settings.get('gamma', 1.0):.6f}")

    # Display
    cp.set("Display", "rotation_k", str(settings.get("rotation_k", 0)))
    cp.set("Display", "flip_x",     str(int(settings.get("flip_x", False))))
    cp.set("Display", "flip_y",     str(int(settings.get("flip_y", False))))

    with open(ini_path, "w", encoding="utf-8") as f:
        cp.write(f)


def _load_display_settings_from_ini(ini_path: str) -> dict:
    """
    Read the [Display] section (rotation_k, flip_x, flip_y).
    Returns empty dict if section is missing.
    """
    import configparser
    cp = configparser.ConfigParser()
    try:
        cp.read(ini_path, encoding="utf-8-sig")
    except Exception:
        return {}

    out = {}
    try:
        out["rotation_k"] = int(cp.get("Display", "rotation_k"))
    except Exception:
        pass
    try:
        out["flip_x"] = bool(int(cp.get("Display", "flip_x")))
    except Exception:
        pass
    try:
        out["flip_y"] = bool(int(cp.get("Display", "flip_y")))
    except Exception:
        pass
    return out