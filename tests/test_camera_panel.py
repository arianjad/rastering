"""views.camera_panel: what reaches the camera. Offscreen, no camera."""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from camera_sim import SimCamera  # noqa: E402
from camera_thorlabs import DisplayGeometry, ThorCamConfig  # noqa: E402
from views.camera_panel import CameraPanel  # noqa: E402


def _panel():
    cam = SimCamera(ThorCamConfig(exposure_ms=56.3, gain=24))
    cam.open()
    p = CameraPanel()
    sent = []
    for name in ("exposure_changed", "gain_changed", "gamma_changed", "roi_requested", "geometry_changed"):
        getattr(p, name).connect(lambda *a, name=name: sent.append((name, a)))
    p.show_info(cam.info())
    p.set_geometry(DisplayGeometry(rotation_k=-1, flip_y=True))
    return p, sent


def test_each_gesture_reaches_the_camera_once_and_shown_values_never_do():
    """A slider once moved its number box but never reached the camera (commit
    56d1733); an echo between slider and box would send every change twice; and a
    value the program shows (camera info, a loaded file) must not be sent back."""
    p, sent = _panel()
    assert sent == []
    p.exposure_slider.setValue(p.exposure_slider.value() + 100)
    from_slider = p.exposure_box.value()
    p.exposure_box.setValue(20.0)
    p.gain_slider.setValue(30)
    p.mirror_x.setChecked(True)
    assert sent == [("exposure_changed", (from_slider,)), ("exposure_changed", (20.0,)),
                    ("gain_changed", (30,)),
                    ("geometry_changed", (DisplayGeometry(rotation_k=-1, flip_x=True, flip_y=True),))]
    assert from_slider > 56.3
    p.show_applied("exposure_ms", 19.98)
    assert p.exposure_box.value() == 19.98 and len(sent) == 4


def test_region_is_sent_only_on_apply_with_absolute_sensor_start():
    """Changing the region makes the calibration stale, so typing a value must not
    send it; Apply sends width, height and the absolute start the Centre button
    computed from the sensor size."""
    p, sent = _panel()
    p.roi_w.setValue(640)
    p.roi_h.setValue(480)
    p.centre_btn.click()
    assert sent == []
    p.apply_roi_btn.click()
    assert sent == [("roi_requested", (640, 480, 400, 300))]
    assert p.camera_config().width == 640 and p.camera_config().start_x == 400


def test_camera_load_and_revert_keep_their_tooltips_after_a_raster():
    """Harm stopped: after the first raster the camera Load and Revert buttons had blank tooltips
    for the rest of the session, so the operator could not tell what Revert would go back to.
    Locked, they say why; unlocked again, they say what they do."""
    p, _ = _panel()
    p.set_settings_file("camera_params.ini")
    tips = (p.load_btn.toolTip(), p.revert_btn.toolTip())
    p.set_geometry_enabled(False, "A raster is active.")
    assert p.load_btn.toolTip() == "A raster is active."
    p.set_geometry_enabled(True)
    assert (p.load_btn.toolTip(), p.revert_btn.toolTip()) == tips and all(tips)
