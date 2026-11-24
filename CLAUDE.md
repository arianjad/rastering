# Rastering GUI

Laser ablation rastering control: Thorlabs Z912 motors, Thorlabs Zelux camera, pattern-based rastering.

## Python Environment

- On the BeamBox PC (MITBeamBox): the system Python, `py -3.13` (PyQt5, pyzmq, pytest). Its
  `bbx_zmq` is an editable install of beambox-daq `src`; put another checkout's `src` first on
  `PYTHONPATH` to test against it.
- On the RaX PC this GUI used conda env **`rastering`**, NOT `labscript`:
  `source ~/miniconda/etc/profile.d/conda.sh && conda activate rastering`
- **Run:** `py -3.13 main_rastering.py` (from this directory). Offline, next to the lab's GUI:
  `py -3.13 main_rastering.py --simulate --no-blacs` (fake motors, fake camera, no port).
- **Tests (the whole folder; no hardware, no camera SDK, no socket; about 6 s):**
  `QT_QPA_PLATFORM=offscreen QT_QPA_FONTDIR=C:/Windows/Fonts ZMQ_V2_REQUIRED=1 py -3.13 -m pytest -q -p no:cacheprovider tests`.
  They run the real `SystemController` with two in-memory motors (`tests/rig.py`), the real window
  offscreen with the fake camera (`tests/test_window.py`), and BLACS's client over
  `bbx_zmq.InMemoryTransport`. Safe to run while the lab's rastering GUI runs.
- **Screenshots:** `py -3.13 scripts/screenshot_window.py OUT_DIR` (the real window, simulated,
  offscreen, in each state, dark and light, 1280 and 2560 wide); `scripts/view_gallery.py` renders
  the view parts alone. Neither saves the theme preference.

## Simulation Mode (no motors)

- `HardwareConfig.simulate = True` (`main_rastering.py --simulate`): `raster_controller.create_controller` builds `motors_sim.SimulatedMotor` axes instead of KCubes; pythonnet and Kinesis are never loaded.
- Explicit only: a KCube that fails to start is an error, never a silent switch to simulated motors.
- A simulated copy keeps its calibration fit, last-calibration pointer and saved defaults in `sim/` (`raster_controller.data_files`): it never loads or overwrites the lab GUI's files.

## Worktrees

- This is the **live** rastering GUI — a git worktree of `RaXcollab/rastering` on `main`, which the operator runs between shots. Dev happens on **topic-branch worktrees** of the same repo (`git worktree add`); never park half-applied work on `main`.

## BLACS Integration

BeamBox (BBX-190): BLACS talks to this GUI through the **menu link** of beambox-daq `bbx_zmq`. One request port, the menu's (its number is written in `bbx_zmq/menus/rastering.py` only), no live stream (the old PUB socket is gone): BLACS asks for a snapshot about every 2 s.

- The menu, the same file BLACS loads: beambox-daq `src/bbx_zmq/menus/rastering.py` (values `xy`, `shots_per_step`, `x`, `y`, `raster_state`, `point_index`, `path_len`; buttons `arm`, `disarm`, `next_point`; time limits; port). Change names or ranges there, for both sides at once, and raise its version.
- `raster_controller.py`: `RasterMenuLink` holds the handlers; `bbx_zmq.MenuServer` checks every request against the menu first (not on the menu, out of range: refused before a handler runs). `SystemController.start_menu_server()` serves it from a thread (`config.py` `network.blacs_link`).
- The one guard for the stage is `stage.py` (`Stage._admit`, on the motor thread, for every move from BLACS or the window): a px target needs a calibration (`not_calibrated`); while any raster is active only its own moves pass (`raster_armed`, `raster_held`, `raster_running`); the target must be inside the travel (`move_refused`, `out_of_bounds_message`). The link only passes the words on.
- `arm` arms the pattern the window has set as the pending plan (`SystemController.set_pending_plan`), in step mode for BLACS; a raster held at the GUI is handed over where it is.
- `next_point` answers the point's details (index, path length, x, y, frame "pixel", calibration) and `finished = True` on the step that ran past the path end and started it again at point 0 (the path loops). It is refused unless `raster_state` is `armed`.
- BLACS side: beambox-daq `src/bbx_devices/RasteringDevice/` (presses next point at shot start while armed; writes the point into the shot file).

## Key Files

- `raster_controller.py` — `SystemController` (the window's one controller: signals out, no motor waits on the GUI thread), `RasterMenuLink` (BLACS), `create_controller(cfg)`
- `stage.py` — both motors, the FIFO motor thread, the one admission check for every move, `reachable()`
- `raster_session.py` — the raster's state (off/local/armed/running), armed path, cursor; `RasterPlan`, `RasterSnapshot` (pure)
- `calibration.py` — px -> mm affine fit and checks, the calibration file, the last-calibration pointer (pure)
- `motors_sim.py` — simulated stage axis (no pythonnet)
- `raster_paths.py` — pattern points: `points_from_spec(RasterSpec)` (square x/y, spiral, convex hull), `nearest_index` (pure)
- `config.py` — all configuration (hardware serials, travel, camera defaults, paths); saved defaults
- `hardware.py` — Thorlabs KCube (Kinesis via pythonnet, loaded only when a KCube is made)
- `main_rastering.py` — start-up: theme, controller, camera thread (real or `camera_sim.SimCamera`), window, last calibration
- `window.py` — the main window: layout, wiring to the controller and camera, `gates()` (the one place that decides which buttons are enabled), clicks on the image by step. Each signal handler re-reads the controller (queued signals can arrive out of order)
- `views/` — view parts, plain data in and Qt signals out: `control_bar`, `step_rail`, `setup_step`, `pattern_step`, `run_step`, `camera_view`, `camera_panel`, `jog_pad`, `log_line`, `widgets`
- `camera_thorlabs.py` — Zelux camera, its thread and the camera settings file (`camera_params.ini`); `camera_sim.py` — the fake camera
- `theme.py` — colour tokens, fonts, the stylesheet, high-DPI set-up; the machine-wide theme preference is written only by View > Light theme (main passes `theme.save_light_pref` to the window)
- `docs/window-features.md` — every control of the old window and where it is now
