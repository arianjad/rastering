"""
config.py

Central configuration for the beam steering / raster application.

Philosophy:
- Keep all settings here (single source of truth).
- No environment-variable overrides.

NOTE: Target-space units here should match your plot/click coordinates (pixels or mm depending on UI scaling).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

Bounds = Tuple[float, float, float, float]  # xmin, xmax, ymin, ymax


# -------------------------
# Dataclass-style config
# -------------------------

@dataclass(frozen=True)
class HardwareConfig:
    # Motor serials
    serial_x: str = "27270103"  # BeamBox
    serial_y: str = "27007520"  # BeamBox

    # Run with SimulatedMotor instead of real KCubes (UI/raster testing
    # with no motor connected). Env var RASTER_SIMULATE=1 overrides this at
    # launch without editing config. Leave False for the real rig.
    simulate: bool = False


    # Kinesis install directory.
    # - Set explicitly if needed, or leave None to let hardware.py search common locations.
    kinesis_dir: Optional[str] = None

    # Kinesis init/polling
    poll_ms: int = 100
    settings_wait_ms: int = 10_000
    device_settings_name: str = "Z912"
    verbose: bool = False

    # HARD safety bounds (xmin, xmax, ymin, ymax) -- Z912 travel is 0-12 mm.
    # Set to None to disable hard bounding in the controller.
    motor_bounds: Optional[Bounds] = (0.0, 12.0, 0.0, 12.0)
    # Units guard: motor_bounds is MOTOR-space mm, never pixel/target units
    # (pixel-space limits belong in raster.target_bounds). The controller
    # refuses to construct if this is not "motor".
    motor_bounds_units: str = "motor"


@dataclass(frozen=True)
class TelemetryConfig:
    # Position polling period (seconds); controller enqueues READ_POS at this rate
    period_s: float = 0.2


@dataclass(frozen=True)
class NetworkConfig:
    # ZMQ bind endpoint (set to "" or None to disable auto-start)
    zmq_bind: str = "tcp://*:55535"
    # PUB-SUB bind endpoint for broadcasting status to BLACS (set to "" to disable)
    pub_bind: str = "tcp://*:55536"


@dataclass(frozen=True)
class CameraConfig:
    # Defaults for camera operation (uEye). ui.py reads these for UEyeConfig construction.

    # Which uEye camera to open (0 is typical for single-camera setups)
    camera_id: int = 0

    # Path to a uEye Cockpit-exported .ini file.
    # If set to a valid file, its settings OVERRIDE the fields below.
    # Set to "" or None to use the manual fields instead.
    camera_params_ini: Optional[str] = 'camera_params.ini'  # You can export this from the uEye Cockpit software after configuring your camera, and it will override the manual settings below.

    # Acquisition pixel clock (MHz): controls FPS + bandwidth
    pixel_clock_mhz: int = 10

    # Default exposure (ms)
    exposure_ms_default: float = 75

    # Target FPS must be set to allow the exposure time (1/0.050s = 20 FPS)
    target_fps: float = 1/exposure_ms_default * 1000

    #  Analog Controls for the new camera.py logic
    master_gain: int = 13           # 0-100
    gamma: float = 1.6             # Will be converted to 160
    enable_gain_boost: bool = False # False reduces noise

    # Image orientation options (applied in UI via a display transform)
    flip_x: bool = False
    flip_y: bool = True

    # Requested AOI / output size (camera.py can honor these depending on your setup)
    width: int = 1440
    height: int = 1080
    # Add offsets to shift the AOI from the center (pixels)
    # Swapped x,y or inverted +/- if you rotate or flip the image in the UI
    roi_offset_x: int = 0
    roi_offset_y: int = -100

    # Capture strategy + channel order
    use_freeze: bool = True   # use FreezeVideo loop
    emit_rgb: bool = False     # convert BGR->RGB for UI


@dataclass(frozen=True)
class RasterDefaults:
    # SOFT bounds in target-space units (what user sees/raster uses).
    # Keep None if you want UI to define these from hull/bounds widgets.
    target_bounds: Optional[Bounds] = None

    x_step: float = 0.2
    y_step: float = 0.2

    # Spiral defaults
    spiral_radius: float = 1.0
    spiral_step: float = 0.1
    spiral_angle_step: float = 0.2
    spiral_angle_step_change: float = 0.0

    # BeamBox: run the pattern forwards, then backwards, then forwards ...
    # (ping-pong), so the stages retrace their path instead of jumping from
    # the last point back to the first (one long move less per pass, and no
    # direction reversal at an arbitrary point: the stages have backlash).
    # True = the old lab copy's behaviour. False = upstream: every pass
    # restarts at point 0.
    # This is only the DEFAULT of the "Ping-pong" checkbox in the Run tab's
    # Raster group. The value that counts is the checkbox at arm time (every
    # arm, local or BLACS, goes through ui._start_raster). "Save as defaults"
    # stores it as pattern.back_and_forth in settings_defaults.json, which
    # overrides this value at the next launch.
    # Consequence of ping-pong on: one armed cycle is 2N points for an N-point
    # pattern. The shot h5 point_index runs 0..2N-1 and path_len = 2N
    # (site k is visited at index k and at 2N-1-k; target_xy stays exact),
    # so "one pass = path_len x shots_per_step shots" means 2 visits per
    # site, and the two turnaround points get 2 x shots_per_step shots in a
    # row. Ping-pong off: one cycle is N points, point_index 0..N-1,
    # path_len = N, one visit per site per pass.
    back_and_forth: bool = True


@dataclass(frozen=True)
class PathsConfig:
    calibration_path: str = "calibration_data.json"
    raster_log_dir: str = "logs"
    # Per-pass raster_log_<ts>.json files. Off by default: with BLACS
    # wrap-on-finished (2026-08-03) a long queue writes one file per pass.
    raster_log_enabled: bool = False


@dataclass(frozen=True)
class AppConfig:
    hardware: HardwareConfig = HardwareConfig()
    telemetry: TelemetryConfig = TelemetryConfig()
    network: NetworkConfig = NetworkConfig()
    camera: CameraConfig = CameraConfig()
    raster: RasterDefaults = RasterDefaults()
    paths: PathsConfig = PathsConfig()


APP_CONFIG = AppConfig()

# -------------------------
# Backwards-compatible constants (optional)
# -------------------------

SERIAL_X = APP_CONFIG.hardware.serial_x
SERIAL_Y = APP_CONFIG.hardware.serial_y

CALIBRATION_PATH = APP_CONFIG.paths.calibration_path
ZMQ_BIND = APP_CONFIG.network.zmq_bind
TELEMETRY_PERIOD_S = APP_CONFIG.telemetry.period_s

DEFAULT_TARGET_BOUNDS = APP_CONFIG.raster.target_bounds
DEFAULT_MOTOR_BOUNDS = APP_CONFIG.hardware.motor_bounds
