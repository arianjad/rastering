"""theme.py: palettes and high-DPI setup. Offscreen, no camera."""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import theme  # noqa: E402


def test_light_and_dark_have_the_same_tokens():
    """A token in one palette but not the other is a KeyError inside the live
    dark/light switch, a Qt slot: PyQt5 aborts the GUI on that."""
    assert set(theme.DARK) == set(theme.LIGHT)
    faces = {k: "X" for k in ("sans", "display", "display_bold", "mono")}
    for t in (theme.DARK, theme.LIGHT):
        assert theme.build_qss(t, faces)


def test_high_dpi_setup_refuses_once_the_app_exists():
    """Called after the QApplication exists, Qt ignores the scaling: on the 150 %
    4K monitor the window then comes up at 5.5 pt with unscaled pixel sizes.
    Refusing loudly makes the wrong call order visible at once."""
    from PyQt5 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    with pytest.raises(RuntimeError, match="before the QApplication"):
        theme.setup_high_dpi()
