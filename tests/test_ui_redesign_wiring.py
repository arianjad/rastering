"""Duck-typed wiring tests for the 2026-08-12 UI redesign glue in ui.py.

CAMERA CAVEAT (same as test_ui_slowdown_guards.py): importing ui.py pulls
in PyQt5 + pyueye. Never run while the rastering GUI is running.
Standalone-runnable:
    conda activate rastering && python -m pytest tests/test_ui_redesign_wiring.py
"""
from __future__ import annotations

import os
import sys
import time
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import ui  # noqa: E402
    _UI_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001
    ui = None
    _UI_IMPORT_ERROR = e


def _require_ui():
    if ui is None:
        import pytest
        pytest.skip(f"ui.py not importable here: {_UI_IMPORT_ERROR}")


def _spiral_self(text):
    return types.SimpleNamespace(
        alg_choice=types.SimpleNamespace(currentText=lambda: text),
        group_spiral=mock.Mock(name="group_spiral"),
    )


def test_spiral_group_shown_for_spiral():
    _require_ui()
    fake = _spiral_self("Spiral Raster")
    ui.RasterMainWindow._update_spiral_visibility(fake)
    fake.group_spiral.setVisible.assert_called_once_with(True)


def test_spiral_group_hidden_for_square():
    _require_ui()
    fake = _spiral_self("Square Raster X")
    ui.RasterMainWindow._update_spiral_visibility(fake)
    fake.group_spiral.setVisible.assert_called_once_with(False)


def _strip_self(**over):
    base = dict(
        status_strip=mock.Mock(name="status_strip"),
        controller=types.SimpleNamespace(
            _raster_index=37, _raster_total_steps=150, calibration=object(),
            armed_path_points=lambda: [0] * 150,
            armed_pattern_len=lambda: 150),
        _raster_active_ui=True,
        _last_raster_source="remote",
        _raster_preview_pts=[],
        _cal_collecting=None,
        _cal_from_file=False,
        _cal_geometry_at_ready=None,
        _loaded_cal_bundle_camera_settings=None,
        _get_cal_bundled_camera_settings=lambda: None,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_strip_owner_armed_blacs():
    _require_ui()
    fake = _strip_self()
    ui.RasterMainWindow._update_strip_owner(fake)
    fake.status_strip.set_chip.assert_called_once_with("owner", "ARMED · BLACS", "cyan")


def test_strip_progress_active_vs_idle():
    _require_ui()
    fake = _strip_self()
    ui.RasterMainWindow._update_strip_progress(fake)
    fake.status_strip.set_chip.assert_called_once_with("progress", "pt 37 / 150")
    idle = _strip_self(_raster_active_ui=False)
    ui.RasterMainWindow._update_strip_progress(idle)
    idle.status_strip.set_chip.assert_called_once_with("progress", "pt — / —")


def test_strip_motor_chip():
    _require_ui()
    fake = _strip_self()
    ui.RasterMainWindow._update_strip_motor(fake, 4.2134, 1.0)
    fake.status_strip.set_chip.assert_called_once_with("motor", "X 4.213 · Y 1.000 mm")


def test_strip_pending_lights_only_on_count_mismatch():
    _require_ui()
    fake = _strip_self(_raster_preview_pts=[0] * 149)
    ui.RasterMainWindow._update_strip_pending(fake)
    fake.status_strip.set_warning.assert_called_once_with("pending", True)
    matched = _strip_self(_raster_preview_pts=[0] * 150)
    ui.RasterMainWindow._update_strip_pending(matched)
    matched.status_strip.set_warning.assert_called_once_with("pending", False)


def test_strip_cal_stale_when_geometry_diverged():
    _require_ui()
    fake = _strip_self(
        _cal_from_file=True,
        _loaded_cal_bundle_camera_settings={"rotation_k": -1},
        _get_cal_bundled_camera_settings=lambda: {"rotation_k": 2},
    )
    ui.RasterMainWindow._update_strip_cal(fake)
    fake.status_strip.set_chip.assert_called_once_with("cal", "cal stale", "warn")


def test_strip_slow_fps_stall_shows_dash():
    _require_ui()
    fake = types.SimpleNamespace(
        _last_frame_time=time.perf_counter() - 10,
        _fps_smoothed=13.2,
        status_strip=mock.Mock(name="status_strip"),
        _update_strip_cal=lambda: None,
    )
    ui.RasterMainWindow._update_strip_slow(fake)
    fake.status_strip.set_chip.assert_called_once_with("fps", "cam —", "warn")


def test_strip_cal_never_raises_when_camera_settings_lookup_errors():
    _require_ui()

    def boom():
        raise KeyError("aoi_width")

    fake = _strip_self(_get_cal_bundled_camera_settings=boom)
    ui.RasterMainWindow._update_strip_cal(fake)  # must not raise
    fake.status_strip.set_chip.assert_called_once_with("cal", "cal ✓", "good")


# ----------------------------------------------------------------------------
# BeamBox: Ping-pong checkbox (Run tab) -> armed path
# ----------------------------------------------------------------------------

class _Box:
    def __init__(self, checked):
        self._c = bool(checked)

    def isChecked(self):
        return self._c

    def setChecked(self, v):
        self._c = bool(v)


def _arm_with_pingpong_box(checked):
    """Run ui._start_raster on a stub; return the controller.start_raster kwargs."""
    ctl = types.SimpleNamespace(calibration=object(), _raster_active=False,
                                start_raster=mock.Mock(name="start_raster"))
    fake = types.SimpleNamespace(
        controller=ctl, _log=lambda m: None,
        _build_raster_spec=lambda: types.SimpleNamespace(kind="square_x", hull_points=None),
        raster_continuous_checkbox=_Box(False),
        raster_pingpong_checkbox=_Box(checked),
        _update_step_mode_ui=lambda: None,
        _clear_overlay_graphics=lambda: None,
        _refresh_raster_scatter=lambda: None,
    )
    with mock.patch.object(ui, "iter_path_from_spec", return_value=iter([(0.0, 0.0)])):
        ui.RasterMainWindow._start_raster(fake, source="remote")
    ctl.start_raster.assert_called_once()
    return ctl.start_raster.call_args.kwargs


def test_pingpong_checkbox_decides_the_armed_path():
    """The Ping-pong box must decide the armed path at every arm (BLACS arms
    included). If the box said "off" but the arm still ping-ponged, the shot
    h5 point_index/path_len would index a 2N cycle while the operator reads
    them as N sites, and shots get assigned to the wrong ablation site."""
    _require_ui()
    assert _arm_with_pingpong_box(False)["pingpong"] is False
    assert _arm_with_pingpong_box(True)["pingpong"] is True


def test_pingpong_choice_survives_save_and_relaunch():
    """"Save as defaults" must store the Ping-pong choice and the next launch
    must restore it. Otherwise a saved "off" silently comes back "on"
    (config.py default) and the h5 path_len doubles without anyone noticing."""
    _require_ui()
    ctl = types.SimpleNamespace(
        _read_motor_backlash_xy=lambda timeout_s: (None, None),
        get_user_home_xy=lambda: (0.0, 0.0))
    spin = types.SimpleNamespace(value=lambda: 0.1)
    chk = types.SimpleNamespace(isChecked=lambda: False)
    src = types.SimpleNamespace(
        controller=ctl, dx_button=spin, dy_button=spin,
        point_display_count=types.SimpleNamespace(value=lambda: 10),
        raster_point_display_count=types.SimpleNamespace(value=lambda: 10),
        show_all_points_checkbox=chk, show_all_raster_points_checkbox=chk,
        show_current_marker_checkbox=chk, show_direction_checkbox=chk,
        raster_pingpong_checkbox=_Box(False),
    )
    saved = ui.RasterMainWindow._gather_user_defaults(src)
    assert saved["pattern"]["back_and_forth"] is False

    dst = types.SimpleNamespace(raster_pingpong_checkbox=_Box(True),
                                controller=types.SimpleNamespace(set_user_home_xy=lambda x, y: None))
    with mock.patch.object(ui, "load_user_defaults", return_value=saved):
        ui.RasterMainWindow._apply_user_defaults(dst)
    assert dst.raster_pingpong_checkbox.isChecked() is False
