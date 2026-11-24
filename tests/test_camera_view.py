"""views.camera_view: frames, geometry, overlays, clicks. Offscreen, no camera."""
from __future__ import annotations

import os
import sys
import types

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

import pyqtgraph as pg  # noqa: E402

from camera_thorlabs import DisplayGeometry  # noqa: E402
from views.camera_view import CameraView, unreachable_mask  # noqa: E402

FRAME = np.zeros((1080, 1440), np.uint8)       # camera orientation: rows = 1080


def test_geometry_setter_sets_the_view_not_toggles_it():
    """Setting the same mirror twice must leave the view mirrored once; a toggling
    setter shows the image mirrored after every second reload, and the operator's
    clicks then land on the opposite side of the target."""
    v = CameraView()
    g = DisplayGeometry(rotation_k=-1, flip_x=False, flip_y=True)
    for _ in range(2):
        v.set_geometry(g)
    vb = v._vb
    assert vb.yInverted() and not vb.xInverted()
    v.show_frame(FRAME)
    v.paint_now()
    assert v.frame_size() == (1080, 1440)        # rotated 90 degrees: image px of the shown frame


def test_only_the_newest_frame_is_painted():
    """Frames offered faster than the paint tick must not each be painted: the GUI
    paints the newest one and skips the rest."""
    v = CameraView()
    v._timer.stop()
    a, b = np.zeros((4, 6), np.uint8), np.full((4, 6), 200, np.uint8)
    v.show_frame(a)
    v.show_frame(b)
    v.paint_now()
    v.paint_now()                                 # nothing new: no repaint
    assert v.frames_painted == 1
    assert v._image.image[0, 0] == 200
    queue = [None, a]
    v.set_frame_source(lambda: queue.pop() if queue else None)
    v.paint_now()
    assert v.frames_painted == 2 and v._image.image[0, 0] == 0


def test_ping_pong_path_draws_the_pattern_once_and_shading_is_one_image():
    """With ping-pong the armed path is the pattern then itself reversed: drawing it
    all doubles every dot. The unreachable shading is one stretched image; one
    square item per cell (thousands) made the old view slow to pan."""
    v = CameraView()
    v.show_frame(FRAME)
    v.paint_now()
    pattern = [(10.0, 20.0), (30.0, 20.0), (50.0, 20.0)]
    v.set_armed_path(pattern + pattern[::-1], pattern_len=len(pattern))
    assert len(v._armed_item.data) == 3
    mask = unreachable_mask(1440, 1080, np.eye(2) * 0.01, (0.0, 0.0), (0.0, 12.0, 0.0, 12.0))
    v.set_unreachable(mask)
    images = [i for i in v._pi.items if isinstance(i, pg.ImageItem) and i.isVisible()]
    assert len(images) == 2                      # the camera frame and the shading
    r = v._unreach.mapRectToParent(v._unreach.boundingRect())
    assert (r.width(), r.height()) == (1440, 1080)


def test_unreachable_mask_marks_where_the_affine_map_leaves_the_travel():
    """The shading must match the stage travel: at 0.01 mm/px and 0..12 mm travel,
    image x beyond 1200 px is out of reach and everything left of it is not."""
    m = unreachable_mask(1440, 1080, [[0.01, 0], [0, 0.01]], (0.0, 0.0), (0.0, 12.0, 0.0, 12.0), cell_px=8)
    assert m.shape == (135, 180)
    x_centres = (np.arange(180) + 0.5) * 8
    assert np.array_equal(m[0], x_centres > 1200)


def test_click_reports_image_px_of_the_point_under_the_pointer():
    """Clicks feed calibration and moves: the reported x, y must be the image px
    under the pointer, also with the view mirrored."""
    v = CameraView()
    v.set_geometry(DisplayGeometry(rotation_k=0, flip_x=True, flip_y=True))
    v.resize(800, 600)
    v.show()
    v.show_frame(FRAME)
    v.paint_now()
    APP.processEvents()
    got = []
    v.clicked.connect(lambda x, y, m: got.append((round(x, 3), round(y, 3), m)))
    scene_pos = v._vb.mapViewToScene(QtCore.QPointF(300.0, 200.0))
    ev = types.SimpleNamespace(button=lambda: QtCore.Qt.LeftButton, double=lambda: False,
                               scenePos=lambda: scene_pos, modifiers=lambda: QtCore.Qt.ControlModifier)
    v._on_click(ev)
    v.close()
    assert got == [(300.0, 200.0, int(QtCore.Qt.ControlModifier))]
