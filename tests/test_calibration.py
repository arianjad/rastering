"""Calibration (calibration.py) and its use by the controller: the file, the click-and-fit flow,
the user home and the backlash it carries (tests/rig.py: real controller, in-memory Z912 axes)."""
from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

from calibration import (CalibrationFile, AffineCalibration, fit_affine, read_calibration_file,
                         write_calibration_file)
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


def test_a_calibration_change_under_an_armed_raster_is_done_and_says_where_the_path_goes(tmp_path):
    """Harm named: the armed path is in pixels, so a new calibration moves every remaining point to
    other motor positions; that is the operator's call (a recalibration mid-run is normal) but
    must not pass unseen. Loading a file, a fresh fit and clearing are done while a raster is
    armed, each with one plain line; the raster stays armed and its next point uses the new
    calibration (cleared: its next point cannot be reached, and says so)."""
    g = Rig(tmp_path)
    try:
        g.plan()
        assert g.ctl.arm().ok
        other = write_calibration(tmp_path / "other.json", mm_per_px=0.02)
        assert g.ctl.load_calibration(other).ok
        assert g.warnings()[-1] == "A raster is armed: its path [px] now maps to the new calibration."
        assert g.ctl.raster_snapshot().state == "local"
        assert g.ctl.step().ok
        g.idle()
        assert g.moves("X")[-1] == pytest.approx(2.0)                 # point 0, 100 px x 0.02 mm/px
        _click_three_spots(g, [((1.0, 1.0), (100.0, 100.0)), ((3.0, 1.0), (300.0, 100.0)),
                               ((1.0, 2.0), (100.0, 200.0))])
        wait_until(lambda: any(t.startswith("Calibration fitted") for _, t in g.messages))
        assert g.warnings()[-1] == "A raster is armed: its path [px] now maps to the new calibration."
        assert g.ctl.clear_calibration().ok and g.ctl.calibration is None
        assert g.warnings()[-1] == ("A raster is armed and has no calibration now: its points cannot be moved to "
                                    "until one is loaded or fitted.")
        assert g.ctl.raster_snapshot().state == "local"
    finally:
        g.close()


def test_a_calibration_file_below_the_quality_limit_loads_with_a_warning(tmp_path):
    """Harm stopped: a calibration file whose matrix is far from square (condition above 10: maybe
    a camera at a steep angle, maybe a bad fit) was refused outright, so the operator could not
    use the only calibration they had. It loads, with one plain line saying what is odd about it;
    a matrix that holds no usable numbers (not finite, or singular) still cannot be used."""
    g = Rig(tmp_path, calibrated=False)
    try:
        steep = write_calibration(tmp_path / "steep.json", calibration_matrix=[[0.01, 0.0], [0.0, 0.0005]])
        assert g.ctl.load_calibration(steep).ok
        assert g.ctl.calibration.px_to_mm(100.0, 100.0) == pytest.approx((1.0, 0.05))
        assert len(g.warnings()) == 1 and "its matrix condition is 20, more than 10" in g.warnings()[0]
        for bad in ([[0.01, 0.0], [0.0, 0.0]], [[float("nan"), 0.0], [0.0, 0.01]]):
            path = write_calibration(tmp_path / "bad.json", calibration_matrix=bad)
            res = g.ctl.load_calibration(path)
            assert not res.ok and "cannot be used" in res.words
        assert g.ctl.calibration.px_to_mm(100.0, 100.0) == pytest.approx((1.0, 0.05))
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
        assert data["user_home"] is None                 # never set: not saved as (0, 0)
    finally:
        g.close()


# A calibration like the lab's (the camera is turned to the stage): image x moves motor y and
# image y moves motor x. The two scales and the small diagonal terms differ, so a transposed or
# swapped matrix gives other numbers.
TURNED_M = [[0.0002, -0.011], [-0.010, -0.0003]]   # [mm/px]
TURNED_B = [16.0, 13.0]                            # [mm]


def test_a_turned_calibration_maps_pixels_to_the_right_motor_axis_and_back():
    """Harm stopped: px -> mm uses the matrix transposed, or mm -> px leaves out the offset: with
    the lab's turned camera every spot (and the marker of where the stage is) lands on the wrong
    axis or millimetres away. The numbers are worked by hand."""
    cal = AffineCalibration(TURNED_M, TURNED_B)
    assert cal.px_to_mm(100.0, 300.0) == pytest.approx((12.72, 11.91))
    assert cal.px_to_mm(500.0, 200.0) == pytest.approx((13.9, 7.94))
    assert np.allclose(cal.px_to_mm_many([(100.0, 300.0), (500.0, 200.0)]), [(12.72, 11.91), (13.9, 7.94)])
    assert cal.mm_to_px(12.72, 11.91) == pytest.approx((100.0, 300.0))


def test_three_clicks_with_the_turned_camera_fit_a_calibration_that_lands_on_the_spot(tmp_path):
    """Harm stopped: the fit puts its x and y rows the wrong way round, which a camera lined up
    with the stage cannot show: with the lab's turned camera every px move then lands
    millimetres off. Clicks made with M = [[0, -0.011], [-0.010, 0]] mm/px, b = (16, 13) mm give
    that calibration back, and a px move lands where it says (worked by hand)."""
    g = Rig(tmp_path, calibrated=False)
    try:
        _click_three_spots(g, [((10.5, 10.0), (300.0, 500.0)), ((10.5, 7.0), (600.0, 500.0)),
                               ((6.1, 10.0), (300.0, 900.0))])
        wait_until(lambda: g.cals and g.cals[-1] is not None)
        cal = g.cals[-1].affine
        assert np.allclose(cal.M, [[0.0, -0.011], [-0.010, 0.0]]) and np.allclose(cal.b, [16.0, 13.0])
        assert g.ctl.move_px(450.0, 700.0).result(2).ok
        assert (g.x.pos, g.y.pos) == pytest.approx((8.3, 8.5))
    finally:
        g.close()


@pytest.mark.parametrize("stage_mm", [
    [(1.0, 5.0), (3.0, 5.0), (5.0, 5.0)],      # the stage moved in x only
    [(5.0, 5.0), (5.0, 5.0), (5.0, 5.0)],      # the stage did not move
    [(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)],      # the stage moved along one line
], ids=["x only", "not moved", "one line"])
def test_clicks_without_a_stage_move_in_both_x_and_y_are_refused_and_save_nothing(tmp_path, stage_mm):
    """Harm stopped: three clicks with the stage moved along one axis only (or not at all) gave
    "scale 1.9e-09 mm/px", were used at once and overwrote calibration_data.json and the
    last-calibration pointer: the next px move went anywhere and the good calibration was gone.
    The fit is refused with words saying to move the stage in both x and y between clicks; the
    calibration in use, the file and the pointer stay as they were."""
    g = Rig(tmp_path)
    try:
        saved = tmp_path / "calibration_data.json"
        pointer = tmp_path / "last_calibration_state.json"
        write_calibration(saved)
        files = saved.read_text(), pointer.read_text()
        before = g.ctl.calibration
        _click_three_spots(g, list(zip(stage_mm, [(100.0, 100.0), (300.0, 100.0), (100.0, 300.0)])))
        wait_until(lambda: any("Calibration failed" in t for t in g.refusals()))
        assert "move the stage in both x and y between clicks" in g.refusals()[-1]
        assert g.ctl.calibration is before
        assert (saved.read_text(), pointer.read_text()) == files
    finally:
        g.close()


def test_a_save_that_cannot_be_written_leaves_the_good_file_whole(tmp_path):
    """Harm stopped: a camera setting JSON cannot hold made Save calibration fail half way through
    writing: the good file was left truncated (the next start loads no calibration) and the
    error escaped to the window. The save is refused with words; the file on disk is the old
    one, whole, and no temporary file is left next to it."""
    g = Rig(tmp_path)
    try:
        path = tmp_path / "saved.json"
        assert g.ctl.save_calibration(str(path), notes="good").ok
        good = path.read_text()
        res = g.ctl.save_calibration(str(path), camera_settings={"sensor": object()})
        assert (res.ok, res.code) == (False, "save_failed") and "not saved" in res.words
        assert path.read_text() == good
        assert sorted(p.name for p in tmp_path.iterdir()) == ["cal.json", "last_calibration_state.json", "saved.json"]
    finally:
        g.close()


def test_a_fit_with_numpy_numbers_in_the_camera_settings_is_saved(tmp_path):
    """Harm stopped: camera geometry that arrives as numpy numbers made the save after a fit fail
    (TypeError): the fit was never written and calibration_data.json was left truncated. The
    fit is saved and reads back with plain numbers."""
    g = Rig(tmp_path, calibrated=False)
    try:
        g.ctl.start_calibration(3, camera_settings={"aoi": {"width": np.int64(1440)}, "rotation_k": np.int32(-1),
                                                    "gamma": np.float32(1.5), "roi": np.array([0, 0, 1440, 1080])})
        for mm, px in [((1.0, 1.0), (100.0, 100.0)), ((3.0, 1.0), (300.0, 100.0)), ((1.0, 2.0), (100.0, 200.0))]:
            g.ctl.move_mm(*mm)
            g.ctl.add_calibration_click(*px)
        wait_until(lambda: any(t.startswith("Calibration fitted") for _, t in g.messages))
        back = read_calibration_file(str(tmp_path / "calibration_data.json"))
        assert back.camera_settings == {"aoi": {"width": 1440}, "rotation_k": -1, "gamma": 1.5, "roi": [0, 0, 1440, 1080]}
        assert back.affine.px_to_mm(200.0, 150.0) == pytest.approx((2.0, 1.5))
    finally:
        g.close()


def test_load_without_a_path_loads_the_calibration_used_last(tmp_path):
    """Harm stopped: at start-up (main calls load_calibration() with no path) the GUI loads no
    calibration, or an older one, instead of the one used last: the first px moves of the day go
    to the wrong spots. After loading A then B, a load with no path gives B."""
    g = Rig(tmp_path, calibrated=False)
    try:
        assert g.ctl.load_calibration(write_calibration(tmp_path / "a.json", mm_per_px=0.01)).ok
        assert g.ctl.load_calibration(write_calibration(tmp_path / "b.json", mm_per_px=0.02)).ok
        assert g.ctl.clear_calibration().ok and g.ctl.calibration is None
        assert g.ctl.load_calibration().ok
        assert g.ctl.calibration.px_to_mm(100.0, 100.0) == pytest.approx((2.0, 2.0))
    finally:
        g.close()


@pytest.mark.parametrize("matrix", [[[float("nan"), 0.0], [0.0, 0.01]], [[0.01, 0.01], [0.01, 0.01]]])
def test_an_unusable_calibration_file_is_refused_with_words_about_the_file(tmp_path, matrix):
    """Harm stopped: loading a calibration file with a NaN or a singular matrix said "the stage
    positions at the clicks lie on one line ... move the stage between clicks", so the operator
    went to recalibrate a file they had only opened. The refusal names the file and its matrix."""
    path = str(tmp_path / "broken.json")
    with open(path, "w") as f:
        json.dump({"calibration_matrix": matrix, "calibration_offset": [0.0, 0.0]}, f)
    with pytest.raises(ValueError) as e:
        read_calibration_file(path)
    assert "broken.json" in str(e.value) and "calibration matrix" in str(e.value)
    assert "click" not in str(e.value)


def test_use_last_names_the_remembered_file_when_it_is_gone(tmp_path):
    """Harm stopped: Use last, with the calibration file used last deleted or moved, said "no
    calibration used before", so the operator recalibrated instead of finding the file. The
    refusal names the missing file."""
    g = Rig(tmp_path, calibrated=False)
    try:
        path = write_calibration(tmp_path / "cal_lab.json")
        assert g.ctl.load_calibration(path).ok
        g.ctl.clear_calibration()
        import os
        os.remove(path)
        res = g.ctl.load_calibration()
        assert not res.ok and "cal_lab.json" in res.words and "no calibration used before" not in res.words
    finally:
        g.close()


def test_the_fit_line_has_no_number_that_reads_like_a_failed_limit():
    """Harm stopped: the fit's log line showed "cond(A) 5.81e+04", a number of another matrix,
    next to the matrix limit of 10, so a good calibration read as a failed one. The line gives
    the spots and the scale only."""
    px = [(0.0, 0.0), (100.0, 0.0), (0.0, 100.0)]
    mm = [(1.0, 1.0), (2.1, 1.0), (1.0, 2.1)]
    _cal, words = fit_affine(px, mm)
    assert words == "3 spots, scale 0.011 mm/px"
