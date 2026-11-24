"""Calibration (calibration.py) and its use by the controller: the file, the click-and-fit flow,
the user home and the backlash it carries (tests/rig.py: real controller, in-memory Z912 axes)."""
from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

from calibration import CalibrationFile, AffineCalibration, read_calibration_file, write_calibration_file
from rig import Rig, wait_until, write_calibration


def test_an_old_calibration_file_with_only_the_matrix_still_loads(tmp_path):
    """Harm stopped: a calibration saved by the older GUI (only calibration_matrix and
    calibration_offset) no longer loads, and the operator has to calibrate again before the next
    run. It loads; no user home or backlash is invented for it."""
    path = write_calibration(tmp_path / "old.json", mm_per_px=0.13)
    cal = read_calibration_file(path)
    assert cal.affine.px_to_mm(10.0, 20.0) == pytest.approx((1.3, 2.6))
    assert (cal.user_home_mm, cal.backlash_mm, cal.camera_settings) == (None, (None, None), None)


def test_a_saved_calibration_reads_back_whole_and_keeps_the_old_keys(tmp_path):
    """Harm stopped: a save loses the user home, the backlash or the camera geometry the
    calibration was made with, or writes keys the lab's other GUI copy cannot read. Everything
    reads back as written; the camera settings come back untouched; the keys are the old ones."""
    camera = {"aoi": {"width": 1440, "height": 1080, "start_x": 0, "start_y": 0}, "rotation_k": -1,
              "flip_x": False, "flip_y": True, "extra": [1, {"k": None}]}
    cal = CalibrationFile(AffineCalibration([[0.0028, -2.5e-5], [1.5e-4, 0.0038]], [-0.31, -0.07]),
                          user_home_mm=(6.0, 5.5), backlash_mm=(0.01, None), camera_settings=camera, notes="n")
    write_calibration_file(str(tmp_path / "c.json"), cal)
    with open(tmp_path / "c.json") as f:
        assert set(json.load(f)) == {"calibration_matrix", "calibration_offset", "user_home", "backlash",
                                     "camera_settings", "saved_at", "notes"}
    back = read_calibration_file(str(tmp_path / "c.json"))
    assert np.array_equal(back.affine.M, cal.affine.M) and np.array_equal(back.affine.b, cal.affine.b)
    assert (back.user_home_mm, back.backlash_mm, back.camera_settings, back.notes) == ((6.0, 5.5), (0.01, None), camera, "n")


def test_picking_the_pointer_file_loads_the_calibration_it_names(tmp_path):
    """Harm stopped: the operator browses to last_calibration_state.json (the pointer) and gets an
    error, or a non-calibration JSON loads as garbage. The pointer is followed; another JSON is
    refused naming the file."""
    path = write_calibration(tmp_path / "real.json")
    with open(tmp_path / "pointer.json", "w") as f:
        json.dump({"last_calibration_path": path}, f)
    assert read_calibration_file(str(tmp_path / "pointer.json")).path == path
    with open(tmp_path / "other.json", "w") as f:
        json.dump({"exposure": 3}, f)
    with pytest.raises(ValueError, match="is not a calibration file"):
        read_calibration_file(str(tmp_path / "other.json"))


def _click_three_spots(g, spots):
    """Jog the stage to each (mm, px) spot and click it, the way the operator calibrates."""
    g.ctl.start_calibration(3, camera_settings={"rotation_k": -1})
    for mm, px in spots:
        g.ctl.move_mm(*mm)
        g.ctl.add_calibration_click(*px)


def test_three_clicks_fit_use_and_save_the_calibration(tmp_path):
    """Harm stopped: a click is paired with a stale stage position (the move before it had not
    finished), so the fit maps pixels to the wrong millimetres; or the fit is used but not saved
    and the next start has none. Each click pairs with the position after the moves queued
    before it; the fit is used at once (px moves land right), saved, and remembered for the next
    start with the camera geometry it was made with."""
    g = Rig(tmp_path, calibrated=False)
    try:
        g.x.gate = threading.Event()             # the stage is slow: clicks come before moves end
        _click_three_spots(g, [((1.0, 1.0), (100.0, 100.0)), ((3.0, 1.0), (300.0, 100.0)),
                               ((1.0, 2.0), (100.0, 200.0))])
        g.x.gate.set()
        wait_until(lambda: g.cals and g.cals[-1] is not None)
        cal = g.cals[-1]
        assert cal.affine.px_to_mm(200.0, 150.0) == pytest.approx((2.0, 1.5))
        assert cal.camera_settings == {"rotation_k": -1} and cal.path.endswith("calibration_data.json")
        assert read_calibration_file(str(tmp_path / "last_calibration_state.json")).path == cal.path
        assert g.ctl.move_px(500.0, 600.0).result(2).ok and (g.x.pos, g.y.pos) == pytest.approx((5.0, 6.0))
    finally:
        g.close()


def test_clicks_on_one_line_are_refused_and_change_nothing(tmp_path):
    """Harm stopped: three spots on one line give a calibration that sends the stage anywhere
    along the other direction. The fit is refused with words saying to click a triangle; the
    calibration in use stays."""
    g = Rig(tmp_path)
    try:
        before = g.ctl.calibration
        _click_three_spots(g, [((1.0, 1.0), (100.0, 100.0)), ((2.0, 2.0), (200.0, 200.0)),
                               ((3.0, 3.0), (300.0, 300.0))])
        wait_until(lambda: any("Calibration failed" in t for t in g.refusals()))
        assert "click spots that form a triangle" in g.refusals()[-1]
        assert g.ctl.calibration is before
    finally:
        g.close()


def test_the_calibration_cannot_change_under_an_armed_raster(tmp_path):
    """Harm stopped: loading or clearing a calibration while a raster is armed moves every
    remaining path point (the path is in pixels) to other spots on the target. Both are refused
    while armed; the calibration in use stays."""
    g = Rig(tmp_path)
    try:
        g.plan()
        g.ctl.arm()
        before = g.ctl.calibration
        other = write_calibration(tmp_path / "other.json", mm_per_px=0.02)
        assert g.ctl.load_calibration(other).code == "raster_active"
        assert g.ctl.clear_calibration().code == "raster_active"
        assert g.ctl.calibration is before
    finally:
        g.close()


def test_loading_a_file_sets_its_backlash_and_user_home(tmp_path):
    """Harm stopped: a calibration file's backlash or user home is not applied, so the stage
    approaches spots with the wrong backlash and User Home goes to the wrong place. Loading sets
    the backlash on both motors and the user home; Go user home moves there."""
    g = Rig(tmp_path, calibrated=False)
    try:
        path = write_calibration(tmp_path / "b.json", user_home={"x": 6.0, "y": 5.5}, backlash={"x": 0.02, "y": 0.03})
        assert g.ctl.load_calibration(path).ok
        g.idle()
        assert ("X", "set_backlash", 0.02) in g.calls and ("Y", "set_backlash", 0.03) in g.calls
        assert g.ctl.backlash_mm == (0.02, 0.03)
        assert g.ctl.go_user_home().result(2).ok and (g.x.pos, g.y.pos) == (6.0, 5.5)
    finally:
        g.close()


def test_backlash_shows_what_the_motor_took_and_saving_never_waits_for_the_stage(tmp_path):
    """Harm stopped: the backlash shown (and saved) is what the operator typed, not what the
    KCube took (it clips); or Save calibration freezes the GUI behind a long move or a device home
    to read the backlash. Set reports and caches the motor's value; Save writes the cached values
    at once, even while the stage is stuck in a move."""
    g = Rig(tmp_path, clip=0.05)
    try:
        assert g.ctl.set_backlash("X", 0.2).result(2).value == 0.05
        assert ("X", 0.05) in g.backlash and g.ctl.backlash_mm == (0.05, 0.0)
        g.x.gate = threading.Event()
        g.ctl.move_mm(3.0, 3.0)                    # the stage is busy for a while
        t0 = time.monotonic()
        assert g.ctl.save_calibration(str(tmp_path / "saved.json"), notes="run 7").ok
        assert time.monotonic() - t0 < 0.5
        with open(tmp_path / "saved.json") as f:
            data = json.load(f)
        assert (data["backlash"], data["notes"]) == ({"x": 0.05, "y": 0.0}, "run 7")
        assert data["user_home"] == {"x": 0.0, "y": 0.0}
    finally:
        g.close()
