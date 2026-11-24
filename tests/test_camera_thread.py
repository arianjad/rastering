"""camera_thorlabs: newest-frame slot, settings sent back, gamma table. Offline
(SimCamera or a fake Zelux handle); no thread is started, no camera opened."""
from __future__ import annotations

import os
import sys
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import camera_thorlabs as ct  # noqa: E402
from camera_sim import SimCamera  # noqa: E402
from test_camera_thorlabs_ini import FakeZelux  # noqa: E402


def test_frame_slot_hands_out_only_the_newest_frame_once():
    """Queueing every frame to a busy GUI piled up 1.5 MB frames and froze the GUI
    (2026-08-11). The slot keeps one frame: older ones are dropped, not queued."""
    slot = ct.FrameSlot()
    a, b, c = (np.full((2, 2), v, np.uint8) for v in (1, 2, 3))
    for f in (a, b, c):
        slot.put(f)
    assert slot.take() is c
    assert slot.take() is None
    assert slot.skipped == 2


def _thread_with_sim():
    t = ct.ThorCameraThread(ct.ThorCamConfig(exposure_ms=50.0), camera_class=SimCamera)
    t._cam = SimCamera(t.cfg)
    t._cam.open()
    applied, infos = [], []
    t.applied.connect(lambda name, v: applied.append((name, v)))
    t.info.connect(infos.append)
    return t, applied, infos


def test_a_setting_change_sends_back_only_the_value_the_camera_took():
    """Dragging the exposure slider sends tens of changes a second; a full info
    refresh per change re-set every panel control mid-drag. Only the applied value
    comes back (clamped by the camera); full info only after a region change."""
    t, applied, infos = _thread_with_sim()
    t.set_exposure_ms(1e9)              # beyond the camera's range
    t.set_gain(7)
    t._apply_pending()
    assert applied == [("exposure_ms", SimCamera.EXPOSURE_MAX_MS), ("gain", 7.0)]
    assert infos == []
    t.set_roi(640, 480, 100, 50)
    t._apply_pending()
    assert len(infos) == 1 and (infos[0].roi_width, infos[0].roi_x) == (640, 100)


def test_gamma_table_is_built_once_per_change_not_per_frame():
    """The old grab built the 256-entry gamma table in Python for every frame,
    costing CPU at the frame rate. Build it on the change; frames only look it up."""
    cam = ct.ThorCamera(ct.ThorCamConfig())
    handle = FakeZelux()
    raw = (np.arange(16, dtype=np.uint16).reshape(4, 4) * 64)
    handle.get_pending_frame_or_null = lambda: type("F", (), {"image_buffer": raw})()
    handle.roi = handle.roi.__class__(0, 0, 3, 3)
    handle.sensor_width_pixels, handle.sensor_height_pixels = 4, 4
    cam.cfg = ct.ThorCamConfig(width=4, height=4)
    cam._configure(handle)
    with mock.patch.object(ct, "gamma_lut", wraps=ct.gamma_lut) as built:
        cam.set_gamma(2.0)
        frames = [cam.grab() for _ in range(5)]
    assert built.call_count == 1
    expected = ct.gamma_lut(2.0)[(raw >> 2).astype(np.uint8)]
    assert all(np.array_equal(f, expected) for f in frames)
