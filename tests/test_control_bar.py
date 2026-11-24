"""views.control_bar: the words the operator reads. Offscreen, no camera."""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from views.control_bar import ControlBar  # noqa: E402


def _shown(bar):
    return {"state": bar.state_block.text(), "look": bar.state_block.property("look"),
            "point": bar.point.value.text(), "cal": bar.calibration.value.text(),
            "cal_tone": bar.calibration.value.property("tone"), "camera": bar.camera.value.text(),
            "x": bar.x_value.text(), "edited": not bar.edited_notice.isHidden()}


def test_state_block_says_who_moves_the_stage_by_word_and_shape():
    """The operator must see at a glance whether BLACS or a GUI raster can move the
    stage, also through laser goggles: each state has its own words, and the two
    'moves without you' states share the solid block while off and held do not."""
    bar = ControlBar()
    seen = {}
    for state in ("off", "running", "armed", "local"):
        bar.set_status(state=state)
        seen[state] = (_shown(bar)["state"], _shown(bar)["look"])
    assert seen == {"off": ("OFF", "off"), "running": ("RUNNING", "auto"),
                    "armed": ("ARMED FOR BLACS", "auto"), "local": ("HELD AT GUI", "held")}


def test_point_is_the_shot_files_point_index():
    """Harm stopped: the bar counted points from 1 and the shot file from 0, so a shot was
    matched to the spot next to the one it hit. 'Point 11 of 48' on screen is point_index 11 in
    the shot file, and the tooltip says so."""
    bar = ControlBar()
    bar.set_status(point_index=11, path_len=48)
    assert bar.point.value.text() == "11 of 48"
    assert "same number as point_index in the shot file" in bar.point.toolTip()
    bar.set_status(point_index=-1, path_len=0)
    assert bar.point.value.text() == "—"


def test_a_partial_update_keeps_the_rest():
    """Position telemetry arrives 4 times a second with x and y only; it must not
    reset the state, the calibration warning or the edited-pattern notice."""
    bar = ControlBar()
    bar.set_status(state="armed", calibration="stale", pattern_edited=True, camera_fps=None)
    bar.set_status(x_mm=6.2341, y_mm=3.1)
    s = _shown(bar)
    assert (s["state"], s["cal"], s["cal_tone"], s["edited"]) == (
        "ARMED FOR BLACS", "Stale: camera changed", "fault", True)
    assert (s["x"], s["camera"]) == ("6.234", "No frames")
    bar.set_status(pattern_edited=False)
    assert not _shown(bar)["edited"]
