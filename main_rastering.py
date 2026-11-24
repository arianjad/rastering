"""Start the rastering GUI.

    py -3.13 main_rastering.py                       real camera, real motors, BLACS link on
    py -3.13 main_rastering.py --simulate --no-blacs  no hardware, no network port

--simulate: fake motors and a fake camera (camera_sim.py); the camera SDK and
Kinesis are never loaded. Same as RASTER_SIMULATE=1.
--no-blacs: do not serve the BLACS menu link (its port stays free, so this copy
can run next to the lab's real GUI).
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys

from PyQt5 import QtGui, QtWidgets

import config

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rastering GUI")
    p.add_argument("--simulate", action="store_true",
                   help="fake motors and fake camera (no hardware)")
    p.add_argument("--no-blacs", action="store_true",
                   help="do not serve the BLACS menu link")
    return p.parse_args(argv)


def app_config(args: argparse.Namespace) -> config.AppConfig:
    cfg = config.APP_CONFIG
    simulate = (args.simulate or cfg.hardware.simulate
                or os.environ.get("RASTER_SIMULATE", "").lower() in ("1", "true", "yes"))
    return dataclasses.replace(
        cfg,
        hardware=dataclasses.replace(cfg.hardware, simulate=simulate),
        network=dataclasses.replace(cfg.network,
                                    blacs_link=cfg.network.blacs_link and not args.no_blacs),
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = app_config(args)

    app = QtWidgets.QApplication(sys.argv[:1])
    from theme import apply_theme, load_light_pref
    apply_theme(app, light=load_light_pref())
    app.setWindowIcon(QtGui.QIcon(os.path.join(HERE, "rastering.ico")))

    from raster_controller import create_controller_from_config, load_last_calibration_path
    controller = create_controller_from_config(cfg)

    from ui import RasterMainWindow
    win = RasterMainWindow(controller, ui_path=os.path.join(HERE, "raster_gui.ui"),
                           simulate_camera=cfg.hardware.simulate)

    # Load the last-used calibration, if any (first launch: none; calibrate first).
    try:
        last_path = load_last_calibration_path()
        if last_path:
            data = controller.load_calibration_from_path(last_path)
            win.note_loaded_cal_bundle(data, source_path=last_path)
    except Exception as e:
        print(f"[startup] Could not auto-load last calibration: {e}")

    win.show()
    rc = app.exec_()
    controller.shutdown()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
