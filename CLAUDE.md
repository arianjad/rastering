# Rastering GUI

Laser ablation rastering control: Thorlabs Z912 motors, IDS uEye camera, pattern-based rastering.

## Python Environment

- On the BeamBox PC (MITBeamBox): the system Python, `py -3.13` (PyQt5, pyzmq, pytest). Its
  `bbx_zmq` is an editable install of beambox-daq `src`; put another checkout's `src` first on
  `PYTHONPATH` to test against it.
- On the RaX PC this GUI used conda env **`rastering`**, NOT `labscript`:
  `source ~/miniconda/etc/profile.d/conda.sh && conda activate rastering`
- **Run:** `python main_rastering.py` (from this directory)
- **Tests (controller, no window, no hardware, no socket):** `QT_QPA_PLATFORM=offscreen ZMQ_V2_REQUIRED=1 py -3.13 -m pytest -q -p no:cacheprovider tests/test_raster_paths.py tests/test_raster_session.py tests/test_stage_gate.py tests/test_menu_server.py tests/test_calibration.py tests/test_hardware.py` (about 3 s). They run the real `SystemController` with its threads and two in-memory motors (`tests/rig.py`); BLACS's client talks over `bbx_zmq.InMemoryTransport`. Tests that import `ui.py` start the camera: never run them (or the whole `tests/` dir) while the lab's rastering GUI runs.

## Simulation Mode (no motors)

- `HardwareConfig.simulate = True` (`main_rastering.py --simulate`): `raster_controller.create_controller` builds `motors_sim.SimulatedMotor` axes instead of KCubes; pythonnet and Kinesis are never loaded.
- Explicit only: a KCube that fails to start is an error, never a silent switch to simulated motors.

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
- `ui.py` — PyQt5 GUI (no ZMQ, no hardware direct calls)
- `config.py` — All configuration (ports, hardware serials, camera params)
- `hardware.py` — Thorlabs KCube (Kinesis via pythonnet, loaded only when a KCube is made)
- `camera.py` — IDS uEye camera interface
- `raster_paths.py` — pattern points: `points_from_spec(RasterSpec)` (square x/y, spiral, convex hull), `nearest_index` (pure)
- `status_strip.py` — annunciator chips (pure state functions + StatusStrip; camera-safe tests); rendered in a strip at the TOP of the image pane (bottom QStatusBar is hidden)
- `theme.py` — dark + light QSS/palettes and theme persistence (QSettings, machine-wide); applied at startup in main_rastering.py, toggled live via View → Light theme (ui.py)
