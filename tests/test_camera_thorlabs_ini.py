"""camera_thorlabs: the camera settings file and the ROI, offline.

No camera and no SDK: ThorCamera is configured on a fake Zelux handle
(1440 x 1080 sensor); ThorCamera.open() is never called.
"""
from __future__ import annotations

import os
import sys
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import camera_thorlabs as ct  # noqa: E402
from camera_sim import SimCamera  # noqa: E402

_ROI = namedtuple("_ROI", "upper_left_x_pixels upper_left_y_pixels lower_right_x_pixels lower_right_y_pixels")
_Range = namedtuple("_Range", "min max")


class FakeZelux:
    sensor_width_pixels, sensor_height_pixels = 1440, 1080
    gain_range = _Range(0, 48)
    exposure_time_range_us = _Range(40, 10_000_000)

    def __init__(self):
        self.roi = _ROI(0, 0, 1439, 1079)
        self.gain = 0
        self.exposure_time_us = 30000
        self.frames_per_trigger_zero_for_unlimited = 1

    def arm(self, n):
        pass

    def disarm(self):
        pass

    def issue_software_trigger(self):
        pass


def thor(cfg):
    cam = ct.ThorCamera(cfg)
    cam._configure(FakeZelux())
    return cam


def test_saved_subframe_roi_reopens_where_it_was_saved(tmp_path):
    """Start X / Start Y must mean the same at open and on Load / Revert. Read as a
    centre offset at open, a 640 px ROI saved at x = 100 reopened at x = 300; the
    calibration is in camera px, so every click and raster would land ~26 mm off."""
    ini = str(tmp_path / "cam.ini")
    ct.save_camera_ini(ini, ct.ThorCamConfig(width=640, height=480, start_x=100, start_y=50),
                       ct.DisplayGeometry())
    cfg, _ = ct.read_camera_ini(ini)
    cam = thor(cfg)
    assert cam.info().roi_x == 100 and cam.info().roi_y == 50
    assert (cam.live_width, cam.live_height) == (640, 480)
    cam.set_roi(cfg.width, cfg.height, cfg.start_x, cfg.start_y)       # Load / Revert path
    assert (cam.info().roi_x, cam.info().roi_y) == (100, 50)


def test_non_numeric_gamma_keeps_the_rest_of_the_file(tmp_path):
    """A bad Gamma used to throw away the whole file, so startup silently fell back to
    config.py's ROI and exposure. Gamma is display only: it falls back to 1.0."""
    ini = str(tmp_path / "cam.ini")
    ct.save_camera_ini(ini, ct.ThorCamConfig(exposure_ms=12.5, gain=7, width=640, start_x=100),
                       ct.DisplayGeometry(rotation_k=-1, flip_y=True))
    with open(ini, encoding="utf-8") as f:
        txt = f.read().replace("gamma = 1.000000", "gamma = abc")
    with open(ini, "w", encoding="utf-8") as f:
        f.write(txt)
    cfg, geom = ct.read_camera_ini(ini)
    assert cfg.gamma == 1.0
    assert (cfg.width, cfg.start_x, cfg.exposure_ms, cfg.gain) == (640, 100, 12.5, 7)
    assert geom == ct.DisplayGeometry(rotation_k=-1, flip_x=False, flip_y=True)


def test_display_geometry_round_trips_and_missing_keys_keep_the_given_values(tmp_path):
    """The image orientation is part of the calibration: a file that reloads it
    rotated or mirrored makes the calibration stale. A file without a Display
    section (an old uEye export) must leave the current orientation alone."""
    ini = str(tmp_path / "cam.ini")
    ct.save_camera_ini(ini, ct.ThorCamConfig(), ct.DisplayGeometry(rotation_k=3, flip_x=True))
    _, geom = ct.read_camera_ini(ini)
    assert geom == ct.DisplayGeometry(rotation_k=-1, flip_x=True, flip_y=False)   # 3 is stored as -1
    bare = tmp_path / "old.ini"
    bare.write_text("[Image size]\nwidth = 1440\n", encoding="utf-8")
    current = ct.DisplayGeometry(rotation_k=2, flip_y=True)
    assert ct.read_camera_ini(str(bare), geometry=current)[1] == current


def test_fake_and_real_camera_place_an_off_sensor_roi_the_same():
    """The lab file has Start X = 352 with a full 1440 px width. Both cameras must
    clamp it to the same place, or a pattern checked offline lands elsewhere live."""
    cfg = ct.ThorCamConfig(width=1440, height=1080, start_x=352, start_y=352)
    real, fake = thor(cfg), SimCamera(cfg)
    fake.open()
    ri, fi = real.info(), fake.info()
    assert (ri.roi_x, ri.roi_y, ri.roi_width, ri.roi_height) == (fi.roi_x, fi.roi_y, fi.roi_width,
                                                                   fi.roi_height) == (0, 0, 1440, 1080)
