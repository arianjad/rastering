"""Start-up as main_rastering.main does it (build: controller, camera thread, window), simulated,
offscreen, with every file the GUI writes in a temporary folder. No port, no camera, no Kinesis;
the theme preference is never written."""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from rig import write_calibration  # noqa: E402

import config  # noqa: E402
import main_rastering  # noqa: E402


def _sim_cfg() -> config.AppConfig:
    c = config.APP_CONFIG
    return dataclasses.replace(c, hardware=dataclasses.replace(c.hardware, simulate=True),
                               network=dataclasses.replace(c.network, blacs_link=False))


def _files(folder) -> dict:
    return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


def test_a_simulated_copy_neither_loads_nor_writes_the_lab_guis_files(tmp_path):
    """Harm stopped: a --simulate copy run from the lab's folder loaded the lab's calibration, then
    wrote back into it (user home or backlash Set) the fake motors' backlash, and saved its
    fake-camera fits and its defaults over the lab's files; the next real start loaded them and
    sent that backlash to the KCubes. A simulated copy keeps its own files in sim/."""
    lab_cal = write_calibration(tmp_path / "calibration_data.json", backlash={"x": 0.02, "y": 0.02})
    (tmp_path / "last_calibration_state.json").write_text(json.dumps({"last_calibration_path": lab_cal}))
    (tmp_path / "settings_defaults.json").write_text(json.dumps({"jog": {"step_mm": 0.5}}))
    lab = _files(tmp_path)

    ctl, cam, win = main_rastering.build(_sim_cfg(), here=str(tmp_path))
    try:
        loaded = ctl.calibration_file
        win.setup.user_home_set_btn.click()          # saves the calibration file in use
        win._save_defaults()
        assert ctl.load_calibration(write_calibration(tmp_path / "elsewhere.json")).ok   # moves the pointer
        assert loaded is None
        assert os.path.dirname(ctl.calibration_path) == str(tmp_path / "sim")
    finally:
        win.close()
        ctl.close()
    os.remove(tmp_path / "elsewhere.json")
    assert _files(tmp_path) == lab
    assert sorted(_files(tmp_path / "sim")) == ["last_calibration_state.json", "settings_defaults.json"]


def test_an_error_in_a_button_handler_is_logged_and_the_gui_goes_on(tmp_path, monkeypatch):
    """Harm stopped: any error nobody caught in a Qt slot (one found: a calibration file whose
    camera rotation is null, then "Use the calibration's camera settings") made PyQt5 abort the
    whole GUI, mid-raster, BLACS link and all. main's hook logs one red line and the GUI goes on."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)       # put back after the test
    ctl, cam, win = main_rastering.build(_sim_cfg(), here=str(tmp_path))
    try:
        main_rastering.install_excepthook(win)

        def broken():
            raise TypeError("int() argument must be a string, not 'NoneType'")

        QtCore.QTimer.singleShot(0, broken)
        t = time.monotonic()
        while not win.log._lines[-1][1].startswith("Unexpected error") and time.monotonic() - t < 2:
            APP.processEvents()
        _stamp, text, error = win.log._lines[-1]
        assert error and "TypeError: int() argument must be a string" in text
    finally:
        win.close()
        ctl.close()


def test_a_failed_save_of_the_defaults_leaves_the_old_file_whole(tmp_path):
    """Harm stopped: Save settings as defaults wrote the file in place; a failure half way (a value
    JSON cannot hold, a full disk) left it cut short, so the next start lost every saved setting.
    The file is written whole or not at all: the old one stays."""
    path = str(tmp_path / "settings_defaults.json")
    config.save_settings_defaults({"jog": {"step_mm": 0.5}}, path)
    with open(path) as f:
        before = f.read()
    try:
        config.save_settings_defaults({"jog": {"step_mm": 0.25}, "bad": object()}, path)
    except (TypeError, ValueError, OSError):
        pass
    with open(path) as f:
        assert f.read() == before
