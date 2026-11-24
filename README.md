# Laser ablation rastering GUI

Steers the ablation laser spot over a target: a Thorlabs Zelux camera shows the target, two
Thorlabs Z912 stages (KCube controllers) move it. You calibrate image px to stage mm, draw a
pattern on the image, then run it from this window or arm it so BLACS (labscript) steps one point
per shot.

## Run

```
py -3.13 main_rastering.py                         real camera, real stages, BLACS link on
py -3.13 main_rastering.py --simulate --no-blacs   fake camera and stages, no network port
```

`--simulate` (or `RASTER_SIMULATE=1`) never loads the camera SDK or Kinesis. `--no-blacs` leaves
the BLACS port free, so a simulated copy can run next to the lab's GUI.

## The window

- **Control bar** (top): who moves the stage (OFF, RUNNING, ARMED FOR BLACS, HELD AT GUI), the
  stage position [mm], the point, the calibration, shots per point, camera frames per second.
- **Setup**: calibration (Calibrate: jog the beam to 3 spots and click the laser spot each time;
  Load…, Save as…, Use last, Clear), device home, user home and backlash (kept in the calibration
  file), camera exposure, gain, gamma, region, rotation and mirror, and the camera settings file.
- **Pattern**: square (rows along x or columns along y), inward spiral, or a hull of corners you
  click on the image; steps and bounds in image px. The points show on the image as you edit.
- **Run**: Start (continuous) or Arm here (step with Step), Arm for BLACS, Hand to BLACS / Take
  back, Stop (also Esc), Re-arm with edited pattern, go to a point, position history.
- **Move stage** (always visible): arrows move the spot on the image by the step [mm]; Go to x, y;
  Go to user home. Greyed while a raster is active.
- File > Save settings as defaults keeps the pattern, run options and jog step for the next start.

`docs/window-features.md` lists every control of the previous window and where it went.

## BLACS link

A menu server on the rastering menu's port (beambox-daq `src/bbx_zmq/menus/rastering.py`, the
same file BLACS loads; handlers in `raster_controller.py` `RasterMenuLink`). BLACS reads all values
in one snapshot, moves the spot to x, y [px], sets shots per point, and presses arm, disarm and
next point. Arm takes the pattern set up in the window. `stage.py` is the one check for every
move: a point outside the stage travel, a px point without a calibration, or a move while a raster
is armed is refused, with words that say why.

## Requirements

- Python 3.13 with PyQt5, pyqtgraph, numpy, scipy, pyzmq; beambox-daq `bbx_zmq` on the path.
- Thorlabs Kinesis (for the stages) and the Thorlabs camera DLLs (in `dlls/`, SDK in `thorlabs_tsi_sdk/`).
- Motor serials, Kinesis folder and stage travel: `config.py` (`HardwareConfig`).

## Tests

```
QT_QPA_PLATFORM=offscreen QT_QPA_FONTDIR=C:/Windows/Fonts ZMQ_V2_REQUIRED=1 py -3.13 -m pytest -q -p no:cacheprovider tests
```

No hardware, no camera, no network port: safe to run while the lab's GUI runs (about 6 s).
