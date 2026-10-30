"""BeamBox: camera_thorlabs.py ini handling, offline.

No camera: ThorCamera.open() runs against a fake SDK and a fake Zelux
(1440 x 1080 sensor); TLCameraSDK is never created and no DLL is loaded.
"""
from __future__ import annotations

import os
import sys
from collections import namedtuple
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import camera_thorlabs as ct  # noqa: E402

_ROI = namedtuple("_ROI", "upper_left_x_pixels upper_left_y_pixels "
                          "lower_right_x_pixels lower_right_y_pixels")
_Range = namedtuple("_Range", "min max")


class _FakeZelux:
    sensor_width_pixels, sensor_height_pixels = 1440, 1080
    gain_range = _Range(0, 48)
    exposure_time_range_us = _Range(40, 10_000_000)

    def __init__(self):
        self.roi = _ROI(0, 0, 1439, 1079)
        self.gain = 0
        self.exposure_time_us = 30000

    def arm(self, n):
        pass

    def disarm(self):
        pass

    def issue_software_trigger(self):
        pass


class _FakeSDK:
    def discover_available_cameras(self):
        return ["FAKE"]

    def open_camera(self, serial):
        return _FakeZelux()


def _roi_start(cam):
    r = cam._cam.roi
    return r.upper_left_x_pixels, r.upper_left_y_pixels


def test_saved_subframe_roi_reopens_where_it_was_saved(tmp_path):
    """The ini Start X/Y must mean the same thing at open and on Load/Revert.
    Upstream read it as a centre offset at open, so a 640 px ROI saved at
    x = 100 relaunched at x = 300. Calibration is in camera pixels, so every
    click and raster after a relaunch would land about 200 px x 0.13 mm/px
    = 26 mm off target."""
    ini = str(tmp_path / "cam.ini")
    ct.save_thorcam_settings_to_ini(ini, {"aoi_width": 640, "aoi_height": 480,
                                          "aoi_x": 100, "aoi_y": 50})
    cfg = ct.load_thorcam_config_from_ini(ini)
    with mock.patch.object(ct, "TLCameraSDK", _FakeSDK):
        cam = ct.ThorCamera(cfg)
        cam.open()
    assert _roi_start(cam) == (100, 50)
    # Load / Revert path: ui -> request_aoi_change -> reinit_roi
    cam.reinit_roi(cfg.width, cfg.height, cfg.roi_offset_x, cfg.roi_offset_y)
    assert _roi_start(cam) == (100, 50)

