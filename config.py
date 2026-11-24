"""All settings of the rastering GUI, in one place (no environment-variable overrides).

Image coordinates are camera pixels [px]; stage coordinates are motor millimetres [mm].
settings_defaults.json next to this file holds the operator's saved pattern and display
settings (load_settings_defaults / save_settings_defaults).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

Bounds = Tuple[float, float, float, float]  # xmin, xmax, ymin, ymax

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_DEFAULTS_FILE = os.path.join(HERE, "settings_defaults.json")


@dataclass(frozen=True)
class HardwareConfig:
    # Motor serials (the BeamBox Z912 stages)
    serial_x: str = "27270103"
    serial_y: str = "27007520"

    # Simulated motors instead of the KCubes (main_rastering.py --simulate). Explicit only: a
    # KCube that fails to start is an error, never a silent switch to simulated motors.
    simulate: bool = False

    # Kinesis install folder (the .NET DLLs)
    kinesis_dir: str = r"C:\Program Files\Thorlabs\Kinesis"

    # Kinesis start-up and polling
    poll_ms: int = 100
    settings_wait_ms: int = 10_000
    device_settings_name: str = "Z912"

    # A move or a device home that takes longer is stopped and reported as failed [s]
    move_timeout_s: float = 10.0
    home_timeout_s: float = 30.0

    # Stage travel (xmin, xmax, ymin, ymax) [mm]: the Z912 travel is 0-12 mm. Every move is
    # checked against it (stage.py).
    travel_mm: Bounds = (0.0, 12.0, 0.0, 12.0)


@dataclass(frozen=True)
class NetworkConfig:
    # Serve BLACS the rastering menu (beambox-daq bbx_zmq/menus/rastering.py, its port).
    # False: the GUI runs without BLACS.
    blacs_link: bool = True


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
    # BeamBox (Zelux): AOI upper-left corner, ABSOLUTE sensor pixels, the
    # same meaning as the ini Start X/Y (clamped to the sensor). Used only
    # when no camera ini loads. Was -100 as a centre offset; with the full
    # 1440x1080 frame both meanings give start (0, 0).
    roi_offset_x: int = 0
    roi_offset_y: int = 0

    # Capture strategy + channel order
    use_freeze: bool = True   # use FreezeVideo loop
    emit_rgb: bool = False     # convert BGR->RGB for UI


@dataclass(frozen=True)
class RasterDefaults:
    # Default of the Run tab's ping-pong box: run the pattern forwards, then backwards, and so on,
    # so the stages retrace their path instead of jumping from the last point back to the first
    # (the stages have backlash). With ping-pong an N-point pattern is armed as a 2N-point path:
    # point_index runs 0..2N-1, path_len = 2N, spot k is visited at k and 2N-1-k, and the two
    # turnaround spots get two steps in a row.
    back_and_forth: bool = True


@dataclass(frozen=True)
class PathsConfig:
    # Written after every calibration fit [relative to this folder unless absolute]
    calibration_path: str = "calibration_data.json"
    # Where the position-history CSV goes
    log_dir: str = "logs"


@dataclass(frozen=True)
class AppConfig:
    hardware: HardwareConfig = HardwareConfig()
    network: NetworkConfig = NetworkConfig()
    camera: CameraConfig = CameraConfig()
    raster: RasterDefaults = RasterDefaults()
    paths: PathsConfig = PathsConfig()


APP_CONFIG = AppConfig()


def load_settings_defaults(path: str = SETTINGS_DEFAULTS_FILE) -> Optional[Dict[str, Any]]:
    """The saved pattern and display settings, or None when there are none (or unreadable)."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_settings_defaults(data: Dict[str, Any], path: str = SETTINGS_DEFAULTS_FILE) -> None:
    """Save the pattern and display settings. Raises OSError."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
