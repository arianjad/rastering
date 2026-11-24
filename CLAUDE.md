# Rastering GUI

Steers the ablation laser spot over a target: two Thorlabs Z912 stages (KCube controllers) move it, a Thorlabs Zelux camera shows it. Part of the BeamBox labscript DAQ (beambox-daq). R&D prototype: rewrite freely.

## How it is built

- **One controller, no waits on the GUI thread.** `raster_controller.SystemController` is the window's only door. Stage jobs go to one motor thread (`stage.py`) in a FIFO queue; their outcome comes back as Qt signals. Each window handler re-reads the controller, because queued signals can arrive out of order.
- **One check for every move.** `Stage._admit` runs on the motor thread just before each move, from the window or from BLACS. It refuses only what has no target: a px target with no calibration, a raster point queued before Stop or a re-arm. Everything else is done, with a warning when it costs something: outside the 0-12 mm travel (sent to the KCube, red), a jog past the edge (stops at the edge), a hand move while BLACS has the raster armed (red), during a continuous run (plain), a device home during any raster (red).
- **The raster is one state word.** `raster_session.py`: off, local (stepped at the GUI), armed (stepped by BLACS), running (continuous at the GUI). The path is in camera px and loops; ping-pong arms 2N points. `Refused` and the state words live there.
- **Calibration maps px to mm.** `calibration.py`: affine fit from 3 clicked spots, the calibration file (matrix, offset, user home, backlash, camera geometry), and the pointer to the file used last. A fit or a file above matrix condition 10 is used with a warning; a singular or non-finite matrix cannot be used.
- **The BLACS link speaks RaX's protocol.** `blacs_link.py`, envelope from RaX's `zmq_v2.py` (beambox-daq `bbx_zmq.zmq_v2`, the only import from beambox-daq). REQ/REP on port 55535: `PROGRAM_VALUE` `laser_raster_xy`, `laser_raster_x_coord` / `_y_coord`, `arm_raster`, `move_to_next`, `shots_per_step`, `disarm_raster`; `CHECK_VALUE` of the coordinates; `HELLO`, `PING`. PUB/SUB on port 55536, from its own thread: px position about 4 Hz; `heartbeat`, `raster_mode`, `raster_owner`, `calibration_status`, `raster_progress` about 1 Hz. Names, reply keys and codes follow upstream rastering `raster_controller.py` (`_RasteringV2Server`, `_zmq_loop`, `raster_point_meta`). Additions: `move_to_next` answers `wrapped: true` on the step back to point 0 and never `finished`, and `held: true` when it did not step (held at the GUI or a continuous run); a request the GUI does with a warning answers SUCCESS with a `warning` key. A move waits up to 10 s (`args.timeout_sec`); later answers TIMEOUT `command_timeout`, and the move still runs. Ports: `config.py` `NetworkConfig`.
- **BLACS side:** beambox-daq `src/bbx_devices/RasteringDevice/`. It sends `move_to_next` at the first shot of each group of N and writes the reply to `/data/<device>/raster`. Values arrive as floats.
- **The window decides enabled buttons in one place,** `window.gates()`. Views in `views/` take plain data and emit Qt signals.
- **Simulation is explicit.** `--simulate` builds `motors_sim.SimulatedMotor` and `camera_sim.SimCamera`; Kinesis and the camera SDK are never loaded. A KCube that fails to start is an error, never a silent switch.

## What can hurt

- **A real start moves real stages.** `py -3.13 main_rastering.py` without `--simulate` loads Kinesis and opens both KCubes; a move outside the travel goes to the KCube, whose own limits are the only stop.
- **The lab's GUI runs from `GUIs\rastering`** (the main worktree of this repo, branch `beambox`) and holds the BLACS ports. Work on a topic-branch worktree. A second copy next to it runs with `--simulate --no-blacs` (ports stay free); a simulated copy keeps its calibration, pointer and defaults in `sim/`, so it never writes the lab's files.
- **The lab's calibration file is written by the GUI**: on every fit (`calibration_data.json`), and on user home or backlash Set (the file in use). The next real start sends that file's backlash to the KCubes.
- **The theme preference is machine-wide.** Only View > Light theme writes it; tests and `scripts/` never do.

## How we build

- **The GUI does what it can.** A request the GUI can carry out is done. A warning stays only when it says something not visible on screen: hardware harm (red) or a shot landing on an unrecorded spot (red); otherwise one plain line, or nothing. A refusal stays only where no target exists (px with no calibration, step with nothing armed, a spiral that never ends, user home not set, a pattern over 2 million points).
- **Follow upstream rastering and RaX** for the link: same names, keys, codes; cite the file. A change to the wire goes in both repos together.
- **Physics values are settings** (pattern steps, delays, shots per point): placeholders are labelled as such.

## Working in the repo

- **Python:** the system `py -3.13` (PyQt5, pyqtgraph, pyzmq, pytest). `bbx_zmq` is an editable install of beambox-daq `src`; put another checkout's `src` first on `PYTHONPATH` to test against it.
- **Tests** (whole folder, about 12 s; no hardware, camera SDK or socket; safe while the lab's GUI runs):
  `QT_QPA_PLATFORM=offscreen QT_QPA_FONTDIR=C:/Windows/Fonts ZMQ_V2_REQUIRED=1 py -3.13 -m pytest -q -p no:cacheprovider tests`.
  They run the real controller with two in-memory motors (`tests/rig.py`), the real window offscreen with the fake camera, and BLACS's requests over `zmq_v2.InMemoryTransport` (`tests/test_blacs_link.py`). A test names, in its docstring, the harm it stops.
- **Screenshots:** `py -3.13 scripts/screenshot_window.py OUT_DIR` (simulated, offscreen, each state, both themes); `scripts/view_gallery.py` renders the view parts alone.

## Files

- `main_rastering.py`: start-up (theme, controller, camera thread, window, last calibration); `--simulate`, `--no-blacs`
- `raster_controller.py`: `SystemController`, `create_controller(cfg)`, `data_files`
- `blacs_link.py`: `RaxLink` (requests and live state), `start` (the two threads)
- `stage.py`: motors, motor thread, `_admit`, `reachable()`
- `raster_session.py`, `raster_paths.py` (pattern points; `MAX_PATH_POINTS`), `calibration.py`: pure, no Qt
- `config.py`: serials, travel, ports, camera defaults, paths; saved defaults file
- `hardware.py` (KCube via Kinesis/pythonnet), `motors_sim.py`, `camera_thorlabs.py`, `camera_sim.py`
- `window.py`, `views/`, `theme.py`
