"""Start the rastering GUI.

    py -3.13 main_rastering.py                        real camera, real motors, BLACS link on
    py -3.13 main_rastering.py --simulate --no-blacs   no hardware, no network port

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
import threading
import traceback
from typing import Tuple

from PyQt5 import QtGui, QtWidgets

import config

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rastering GUI")
    p.add_argument("--simulate", action="store_true", help="fake motors and fake camera (no hardware)")
    p.add_argument("--no-blacs", action="store_true", help="do not serve the BLACS menu link")
    return p.parse_args(argv)


def app_config(args: argparse.Namespace) -> config.AppConfig:
    cfg = config.APP_CONFIG
    simulate = (args.simulate or cfg.hardware.simulate
                or os.environ.get("RASTER_SIMULATE", "").lower() in ("1", "true", "yes"))
    return dataclasses.replace(
        cfg,
        hardware=dataclasses.replace(cfg.hardware, simulate=simulate),
        network=dataclasses.replace(cfg.network, blacs_link=cfg.network.blacs_link and not args.no_blacs),
    )


def camera_setup(cfg: config.AppConfig) -> Tuple[object, object, str, str]:
    """(ThorCamConfig, DisplayGeometry, settings file used or "", words for the log) from the
    camera settings file; config.py's values when it does not load."""
    from camera_thorlabs import DisplayGeometry, ThorCamConfig, read_camera_ini
    c = cfg.camera
    base = ThorCamConfig.from_app(c)
    geom = DisplayGeometry(rotation_k=c.rotation_k, flip_x=c.flip_x, flip_y=c.flip_y)
    path = c.camera_params_ini
    if not path:
        return base, geom, "", "No camera settings file configured: config.py values."
    path = path if os.path.isabs(path) else os.path.join(HERE, path)
    try:
        cam_cfg, geom = read_camera_ini(path, base, geom)
    except Exception as e:      # a bad file must not stop the GUI: config.py values instead
        return base, geom, "", f"Camera settings file not loaded ({e}): config.py values."
    return cam_cfg, geom, path, f"Camera settings loaded from {path}."


def build(cfg: config.AppConfig, *, here: str = HERE, remember_theme=None):
    """The controller, the camera thread (not started) and the window, with the last calibration
    loaded. here: the folder of the GUI's own files (tests pass a temporary folder)."""
    from calibration import remembered_calibration_path
    from camera_thorlabs import ThorCameraThread
    from raster_controller import create_controller, data_files
    from window import RasterWindow

    files = data_files(cfg, here)
    os.makedirs(os.path.dirname(files.pointer), exist_ok=True)
    controller = create_controller(cfg, files)
    cam_cfg, geom, cam_file, cam_words = camera_setup(cfg)
    camera_class = None
    if cfg.hardware.simulate:
        from camera_sim import SimCamera
        camera_class = SimCamera
    camera = ThorCameraThread(cam_cfg, camera_class=camera_class)

    win = RasterWindow(controller, camera, camera_config=cam_cfg, geometry=geom, camera_file=cam_file,
                       log_dir=os.path.join(HERE, cfg.paths.log_dir),
                       defaults_path=files.defaults, remember_theme=remember_theme)
    win.log.add(cam_words, error=not cam_file)
    if cfg.hardware.simulate:
        win.log.add(f"Simulated: calibration and defaults are kept in {os.path.dirname(files.pointer)}, "
                    "apart from the lab GUI's.")
    if remembered_calibration_path(controller.pointer_path):
        controller.load_calibration()        # a remembered file that is gone: the log names it
    else:
        win.log.add("No calibration used before: calibrate in Setup.")
    return controller, camera, win


def install_excepthook(win) -> None:
    """An error nobody caught in a Qt slot would make PyQt5 abort the whole GUI. Instead: the
    traceback goes to the console, one red line to the window's log, and the GUI goes on."""
    def hook(kind, value, tb):
        traceback.print_exception(kind, value, tb)
        if threading.current_thread() is threading.main_thread():   # the log is a widget
            try:
                win.log.add(f"Unexpected error ({kind.__name__}: {value}); the GUI goes on. "
                            "The details are in the console.", error=True)
            except Exception:
                pass
    sys.excepthook = hook


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = app_config(args)

    import theme
    theme.setup_high_dpi()
    app = QtWidgets.QApplication(sys.argv[:1])
    theme.apply_theme(app, light=theme.load_light_pref())
    app.setWindowIcon(QtGui.QIcon(os.path.join(HERE, "rastering.ico")))

    controller, camera, win = build(cfg, remember_theme=theme.save_light_pref)
    install_excepthook(win)
    win.resize(1280, 900)
    win.show()
    camera.start()
    try:
        return app.exec_()
    finally:
        controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
