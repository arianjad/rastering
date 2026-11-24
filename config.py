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
# --simulate keeps its calibration, last-calibration pointer and defaults in this subfolder,
# apart from the lab GUI's (raster_controller.data_files).
SIM_FOLDER = "sim"


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
    # Thorlabs Zelux. The camera settings file wins: when it loads, its values
    # replace the ones below (camera_thorlabs.read_camera_ini).
    camera_params_ini: Optional[str] = "camera_params.ini"
    serial_number: str = ""        # empty: the first camera found
    max_fps: float = 15.0          # soft limit of the grab loop [frames/s]

    # Used only when no settings file loads.
    exposure_ms: float = 75.0
    gain: int = 13
    gamma: float = 1.6             # software display gamma
    width: int = 1440              # ROI [px]
    height: int = 1080
    start_x: int = 0               # ROI upper-left, absolute sensor px
    start_y: int = 0
    # Display: np.rot90 k (0 none, -1 90 deg clockwise, 2 180 deg, 1 90 deg
    # anticlockwise), then mirror.
    rotation_k: int = -1
    flip_x: bool = False
    flip_y: bool = True


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
