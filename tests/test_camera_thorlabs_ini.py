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


def test_non_numeric_gamma_keeps_the_rest_of_the_ini(tmp_path):
    """A non-numeric Gamma used to raise ValueError and throw away the whole
    ini: startup then fell back to config.py's camera values, so the ROI and
    exposure silently changed. Gamma (display only) falls back to 1.0."""
    ini = str(tmp_path / "cam.ini")
    ct.save_thorcam_settings_to_ini(ini, {"aoi_width": 640, "aoi_height": 480,
                                          "aoi_x": 100, "aoi_y": 50,
                                          "exposure": 12.5, "gain": 7})
    with open(ini, encoding="utf-8") as f:
        txt = f.read().replace("gamma = 1.000000", "gamma = abc")
    with open(ini, "w", encoding="utf-8") as f:
        f.write(txt)
    cfg = ct.load_thorcam_config_from_ini(ini)
    assert cfg.gamma == 1.0
    assert (cfg.width, cfg.roi_offset_x, cfg.exposure_us, cfg.gain) == (640, 100, 12500.0, 7)
