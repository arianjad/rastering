"""views: jog pad, log line, step rail. Offscreen, no camera, no motors."""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from views.jog_pad import JogPad  # noqa: E402
from views.log_line import LogLine  # noqa: E402
from views.step_rail import progress_notes  # noqa: E402


def test_jog_arrows_send_the_screen_direction_and_lock_during_a_raster():
    """The up arrow must ask for 'up on the image' with the step shown (the window
    maps it to stage axes), and no arrow may move the stage while a raster runs."""
    pad = JogPad()
    pad.set_step_mm(0.25)
    got = []
    pad.jog_requested.connect(lambda dx, dy, s: got.append((dx, dy, s)))
    pad.arrow_buttons[(0, 1)].click()
    pad.arrow_buttons[(-1, 0)].click()
    assert got == [(0, 1, 0.25), (-1, 0, 0.25)]
    pad.set_moves_enabled(False, "A raster is active.")
    pad.arrow_buttons[(0, 1)].click()
    pad.home_btn.click()
    assert len(got) == 2 and not pad.reason.isHidden()


def test_log_keeps_a_bounded_history_and_marks_refusals():
    """The log runs for days (camera error bursts land here): the history must stay
    bounded, and a refusal must stand out from routine lines."""
    log = LogLine(max_lines=3)
    for i in range(5):
        log.add(f"line {i}")
    log.add("Move refused: outside the stage travel", error=True)
    assert [line.split("  ", 1)[1] for line in log.lines()] == ["line 3", "line 4",
                                                               "Move refused: outside the stage travel"]
    assert log.line.property("tone") == "fault"
    log.set_expanded(True)
    assert log.history.document().blockCount() == 3


def test_setup_is_done_only_with_camera_and_a_current_calibration():
    """A stale calibration sends clicks and rasters to the wrong place: Setup must
    not show as done with one, nor without a camera."""
    assert progress_notes(True, "loaded", 0, "off")[0] == (True, "Ready")
    assert progress_notes(True, "stale", 48, "off")[0][0] is False
    assert progress_notes(False, "loaded", 48, "off")[0][0] is False
    assert progress_notes(True, "loaded", 48, "armed")[1:] == [(True, "48 points"), (False, "Armed for BLACS")]
