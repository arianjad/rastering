# Rastering GUI

Laser ablation rastering control: Thorlabs Z912 motors, IDS uEye camera, pattern-based rastering.

## Python Environment

- On the BeamBox PC (MITBeamBox): the system Python, `py -3.13` (PyQt5, pyzmq, pytest). Its
  `bbx_zmq` is an editable install of beambox-daq `src`; put another checkout's `src` first on
  `PYTHONPATH` to test against it.
- On the RaX PC this GUI used conda env **`rastering`**, NOT `labscript`:
  `source ~/miniconda/etc/profile.d/conda.sh && conda activate rastering`
- **Run:** `python main_rastering.py` (from this directory)
- **Tests:** `pytest tests/test_raster_pathmodel.py tests/test_menu_server.py tests/test_raster_gui_ui_structure.py tests/test_status_strip.py` are ALL camera-safe (pure controller/menu-server/UI-structure logic, no `ui.py` import; run ALL before any commit). With `QT_QPA_PLATFORM=offscreen`. `test_command_queue.py` and `test_raster_goto_handlers.py` import `ui.py` → open the uEye camera → **HANG when the GUI/camera is busy** — never run them (or the whole `tests/` dir) while the rastering GUI runs. Use `python -m py_compile` for syntax. Tests are standalone-runnable.

## Simulation Mode (no motors)

- Launch with `RASTER_SIMULATE=1` (env var) or set `HardwareConfig.simulate = True` in `config.py` — the controller factory builds `SimulatedMotor` instances instead of real KCubes (`raster_controller.py:create_controller_from_config`).
- Explicit opt-in only: there is **no automatic fallback** to sim when a KCube fails to init — a disconnected motor on the real rig must error loudly, not silently no-op moves.
- Use for UI/raster-logic testing with no hardware attached; verified headless (window + event loop + clean shutdown) 2026-07-02.

## Worktrees

- This is the **live** rastering GUI — a git worktree of `RaXcollab/rastering` on `main`, which the operator runs between shots. Dev happens on **topic-branch worktrees** of the same repo (`git worktree add`); never park half-applied work on `main`.

## BLACS Integration

BeamBox (BBX-190): BLACS talks to this GUI through the **menu link** of beambox-daq `bbx_zmq`. One request port, the menu's (its number is written in `bbx_zmq/menus/rastering.py` only), no live stream (the old PUB socket is gone): BLACS asks for a snapshot about every 2 s.

- The menu, the same file BLACS loads: beambox-daq `src/bbx_zmq/menus/rastering.py` (values `xy`, `shots_per_step`, `x`, `y`, `raster_state`, `point_index`, `path_len`; buttons `arm`, `disarm`, `next_point`; time limits; port). Change names or ranges there, for both sides at once, and raise its version.
- `raster_controller.py`: `RasterMenuLink` holds the handlers; `bbx_zmq.MenuServer` checks every request against the menu first (not on the menu, out of range: refused before a handler runs). `SystemController.start_menu_server()` serves it from a thread (`config.py` `network.blacs_link`).
- This GUI is the one guard for the stage: a move outside the motor travel is refused with its own words (`out_of_bounds_message`); a move without a calibration is refused; a move (`xy`, from a sequence's Raster_X / Raster_Y or the BLACS tab) is refused while any raster is active: armed for BLACS (`raster_armed`), held at the GUI (`raster_held`) or a continuous run (`raster_running`); only raster state `off` (after the GUI's Stop) takes a move.
- `next_point` answers the point's details (index, path length, x, y, frame, calibration) and `finished = True` on the step that ran past the path end and started it again at point 0 (the path loops). It is refused unless `raster_state` is `armed` (armed for BLACS).
- An arm from scratch goes through the Run tab (`ui.py` `_on_remote_arm_requested`, via `remote_arm_provider`).
- BLACS side: beambox-daq `src/bbx_devices/RasteringDevice/` (presses next point at shot start while armed; writes the point into the shot file).

## Key Files

- `raster_controller.py` — Central controller: motor commands, raster state machine, BLACS menu server
- `ui.py` — PyQt5 GUI (no ZMQ, no hardware direct calls)
- `config.py` — All configuration (ports, hardware serials, camera params)
- `hardware.py` — Thorlabs Kinesis motor interface
- `camera.py` — IDS uEye camera interface
- `raster_paths.py` — Path generation algorithms (grid, spiral, convex hull)
- `status_strip.py` — annunciator chips (pure state functions + StatusStrip; camera-safe tests); rendered in a strip at the TOP of the image pane (bottom QStatusBar is hidden)
- `theme.py` — dark + light QSS/palettes and theme persistence (QSettings, machine-wide); applied at startup in main_rastering.py, toggled live via View → Light theme (ui.py)
