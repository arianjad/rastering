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


def test_live_box_and_button_borders_stand_out_in_both_themes():
    """Harm stopped: box and button borders at 1.7:1 in the light theme (1.5:1 in dark), so an
    editable box looked like plain text. Every live border is at least 3:1 against the box fill,
    the panel and the ground, in both themes (WCAG 2.2, 1.4.11 non-text contrast)."""
    import theme

    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    for tokens in (theme.DARK, theme.LIGHT):
        for fill in ("well", "panel", "ground"):
            hi, lo = sorted((lum(tokens["edge"]), lum(tokens[fill])), reverse=True)
            assert (hi + 0.05) / (lo + 0.05) >= 3.0, (tokens["edge"], tokens[fill])
