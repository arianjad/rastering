# Window features: old window to new

Every control and behaviour of the old window (`ui.py` + `raster_gui.ui`, 2026-11), and where it is
now. Kept = same job, maybe a new name; Merged = folded into another control; Dropped = gone, with
the reason.

## Menu bar
| Old | Now |
|---|---|
| File menu | Kept |
| File > Save current as defaults (Ctrl+D) | Kept: File > Save settings as defaults (Ctrl+D): pattern, run options, trail, jog step. Backlash and user home now live in the calibration file. |
| View menu | Kept |
| View > Camera Settings (Ctrl+Shift+C) | Dropped: no dock; the camera settings are in the Setup step. |
| View > Light theme | Kept |
| No Exit | Added: File > Exit (Ctrl+Q); View > Fit image |

## Run tab → Run step
| Old | Now |
|---|---|
| Group "Raster" | Kept: section "Raster", with one sentence saying what the raster does now |
| Auto Raster | Merged: "Start" (Continuous on) or "Arm here" (step at the GUI) |
| Stop | Kept: "Stop (Esc)"; greyed when nothing is armed |
| Step | Kept; enabled only while held at the GUI |
| (none) | New: "Pause" holds a continuous run at the GUI at its point; "Continue" runs a held raster on by itself from the point the stage is at. Go to selected point N, then Continue: the run goes on from N |
| Continuous | Kept; editable while armed. Continuous and the delay reach the armed raster at once (the delay from its next point); a ping-pong change shows "Run options changed since arming" and waits for Re-arm |
| Delay (s) | Kept: "Delay per point" [s], enabled with Continuous |
| Ping-pong | Kept |
| Save position history | Kept: "Record to a file" (CSV: time_s, x_mm, y_mm, x_px, y_px, in `logs/`) |
| Group "Stepping / Remote control" | Merged into "Raster" and "Go to a point" |
| Point index box | Kept: "Point", counted from 0: the same number as point_index in the shot file (also in the control bar and the log) |
| Move to selected | Kept: "Go to selected point". Dropped: arming first when idle (arm, then pick). |
| Arm for remote / Return to local / Give to BLACS (one button, three faces) | Split: "Arm for BLACS", "Take back", "Hand to BLACS". After Go to selected point N, BLACS's next shot goes to N itself (the log says which point) |
| Re-arm from pending | Kept: "Re-arm with edited pattern", shown only when the pattern changed since arming; the raster stays as it was (held, running, or with BLACS) |

## Pattern tab → Pattern step
| Old | Now |
|---|---|
| Group "Raster Pattern" | Kept: Pattern step |
| Algorithm combo | Kept: "Kind": square rows along x, square columns along y, spiral, hull of clicked corners |
| Show rastering direction | Kept: "Show the order"; now drawn while armed too |
| Step x, y (px) | Kept; shown for square and hull |
| Spiral group | Kept: shown for spiral only |
| Spiral radius, radial step, angle step, angle change | Kept [px, px, rad, rad]; defaults 200 px, 20 px, 0.2 rad, 0 rad (placeholders: set and save your own). Refused at once, with words: an angle change that brings the angle step to 0, or more than 1 000 000 angle steps (the window would freeze) |
| Spiral centre x, y | Kept; also set by a click on the image; now saved with the defaults |
| Scan bounds group | Kept: shown for square and spiral (the hull never used them) |
| x/y lower/upper | Kept; saved bounds now survive the first camera frame; "Whole image" button added |
| Enforce scan bounds | Dropped (controller rebuild): every move is checked against the stage travel; the bounds only shape the pattern |
| Preview Path | Dropped: the preview updates on every edit |
| Clear All Raster Points | Merged: corners "Clear", plus "Remove last" |
| Save and Clear | Dropped: it was Stop + Clear and saved nothing |
| Live preview refresh | Kept, always on; every edit also sets the pattern BLACS arms |

## Setup tab → Setup step
| Old | Now |
|---|---|
| Reset | Kept: "Clear"; during a raster it is done, with a line saying the armed path has no calibration now |
| Calibrate (Affine) | Kept: "Calibrate"; "Cancel calibration" (also Esc) while collecting; "spot 2 of 3" guidance |
| Revert to Last | Kept: "Use last" |
| Save Calibration As… | Kept: "Save as…". Dropped: the notes dialog (fits note their spots; the file name says the rest) |
| Load Calibration… | Kept: "Load…". A file whose matrix condition is above 10 loads with a warning (a fresh fit that far off is refused); a singular or non-finite matrix cannot be used |
| Apply camera settings from cal | Kept: "Use the calibration's camera settings", shown when the calibration is stale |
| Matrix spin boxes M11..M22, bX, bY | Changed: read-only M [mm/px], b [mm] and scale |
| Device Home X, Y, Both | Kept; during a raster it is done, with a red line (the stage sweeps to 0 mm under the laser) |
| User home readings, setpoints, Set X / Set Y | Kept: one Set for x and y; "Here" fills them from the stage (also while it moves); saved into the calibration file at once. Until one is set it reads "not set" and Go to user home is greyed (no (0, 0) default) |
| User home Go X / Go Y | Dropped: "Go to user home" (both axes) under Move stage, or Go to x, y |
| Backlash readings, setpoints, Set X / Set Y | Kept: one Set; saved into the calibration file once the motors answer (an axis that fails is not waited for; a calibration loaded meanwhile is not written); live during a raster |
| Display Options group | Merged |
| Show current position | Dropped: the stage crosshair is always shown |
| Display points + Last N | Merged: "Trail on image", last N positions (Run step) |
| Display raster points + Last N | Dropped: the whole armed path is drawn; above 50 000 points an even subset is drawn (the Pattern step says 1 point in k) and all are armed. A pattern over 2 million points cannot be built (the window would freeze) |

## Jog / Move panel → Move stage
| Old | Now |
|---|---|
| Step x, Step y (motor units) | Merged: one "Step" [mm] along the arrow's screen direction. A number typed in any box counts when a button reads it, Enter or not |
| Jog Up / Left / Right / Down | Kept: arrows; through the calibration matrix when calibrated; the rig's fixed axes otherwise, with a note under the pad to check the direction on the image. Live during a raster: red line while BLACS has it armed, a plain one during a continuous run, nothing while held at the GUI. A jog past the travel edge stops at the edge (plain line); a typed target outside the travel is sent to the KCube with a red line |
| Clear manual markers | Dropped: the go-to mark is the only manual mark; trail 0 hides the trail |
| x, y (motor units) | Kept: "x", "y" [mm, ±1000]; a click on the image on the Setup step fills them |
| Move to Position | Kept: "Go to x, y" |
| Preview Position | Merged: the go-to target is always marked on the image (when calibrated) |
| Go user home | Kept: "Go to user home" |

## Camera dock → Setup step, camera sections
| Old | Now |
|---|---|
| Pixel clock, Timing mode, FPS range, Target FPS, Gain boost | Dropped: no effect on the Zelux |
| Exposure slider + box, range label | Kept (logarithmic slider, ms) |
| Gain, Gamma | Kept |
| AOI width, height, start x, start y | Kept: "Region of interest" [px] |
| Apply AOI | Kept: "Apply region", enabled once edited; locked only while calibration clicks are collected (during a raster the stale-calibration mark shows a change) |
| Center | Kept: "Centre on sensor" |
| Sensor size label | Kept |
| Rotation, Flip X, Flip Y | Kept: Rotation, Mirror left–right, Mirror up–down; locked only while calibration clicks are collected |
| Loaded: label | Kept: file name under "Camera settings file" |
| Save Settings, Load Config, Revert | Kept: Save as…, Load…, Revert |

## Status strip → control bar
| Old | Now |
|---|---|
| Owner chip | Kept: state block OFF / RUNNING / ARMED FOR BLACS / HELD AT GUI |
| Motor chip | Kept: Stage position X, Y [mm] |
| Progress chip | Kept: Point "i of N": i as point_index in the shot file (first point 0), N the path length |
| Calibration chip | Kept: None / n of 3 clicks / Ready / Stale |
| Shots chip | Kept: Shots per point |
| FPS chip | Kept: Camera fps; "No frames" after 3 s |
| PATH EDITED — RE-ARM | Kept: edited notice; compares the plans, not only point counts |
| BOUNDS OFF | Dropped with Enforce scan bounds |
| ● REC | Merged: the file name shows under "Record to a file" |
| FPS text on the image | Dropped: fps is in the control bar |

## Image
| Old | Now |
|---|---|
| Left click | Changed, by step: calibrating → calibration spot; Pattern → hull corner or spiral centre; Run → nearest armed point (nothing armed: Go to x, y target); Setup → Go to x, y target |
| Ctrl + left click | Kept: pick the nearest armed point, on any step |
| Right click, wheel, drag | Kept: wheel zooms, drag pans. Dropped: context menu and "A" button; "Fit" button and View > Fit image instead |
| Pointer crosshair | Kept; hides when the pointer leaves |
| Overlay colours | Changed: path colour = pattern (rings: not armed, dots: armed), spot colour = stage crosshair and trail, diamonds = hull corners, hatched = out of stage travel |
| Shortcuts | Kept Ctrl+D; added Esc (stop any raster, also one BLACS armed a moment ago; cancel a calibration), Ctrl+Q |
| Frame painting | Kept: newest frame only, at most 25 per second |

## Start and exit
| Old | Now |
|---|---|
| Command line | Kept: `--simulate` (or RASTER_SIMULATE=1), `--no-blacs` |
| Theme at start | Kept; high-DPI scaling added |
| Controller creation | Kept: `create_controller(cfg)` |
| Window build order | Replaced: `window.py` |
| Camera start | Kept: `camera_params.ini` read in `main_rastering.py`; config.py values if it does not load (said in the log) |
| Auto-load last calibration | Kept; Setup now shows its user home and backlash; first start says so in the log |
| 2 s strip timer | Merged: 1 s check for "No frames" |
| 40 ms frame timer | Kept (in the camera view) |
| BLACS arm hand-off through the window | Changed: BLACS arms the pattern the window has set, without a round trip through the window |
| Log pane | Merged: log line with "Show log" (newest 2000 lines) |
| Window close | Kept: camera stopped; the controller stops the raster and finishes queued stage jobs |

## Files
| Old | Now |
|---|---|
| `settings_defaults.json` | Kept; keys pattern, run, jog (older files: unknown keys ignored, re-save once) |
| `last_calibration_state.json` | Kept |
| `calibration_data.json` (fit auto-save) | Kept; now stores the camera settings of the fit, so "stale" works for a fresh fit |
| Named calibration files | Kept |
| `camera_params.ini` | Kept |
| Theme preference (QSettings) | Kept |
| Position history CSV | Kept, with px columns |
| Per-pass raster log JSON | Dropped (controller rebuild): BLACS writes each shot's point into the shot file |
| Not saved: hull corners, spiral centre | Now saved with the defaults. Window size: still not saved |

## "Odd or dead" list
1. Matrix spin boxes nobody read: fixed (read-only text).
2. uEye-only camera controls: removed.
3. Saved bounds overwritten by the first frame; spiral centre never saved: fixed.
4. Bounds tooltip claimed the hull used them; Enforce tooltip: fixed (bounds hidden for the hull; Enforce removed).
5. Units: one unit per quantity (stage mm, image px); jog step is mm along the screen direction.
6. "Reset" cleared the calibration under a running raster without a word: now "Clear"; a calibration change (Clear, Load, Use last, a fit) during a raster is done and says where the armed path goes now.
7. "Save and Clear": removed.
8. Stop enabled with nothing running: fixed. **Open:** Stop drops the queued points but lets a move already under way finish. Kinesis has `StopImmediate` (the KCube code calls it when a move times out), so Stop could also halt the move in flight.
9. Calibrate had no Cancel and fed later clicks to the calibration: fixed (Cancel, Esc; clicks go to the calibration only while collecting).
10. Re-arm never gated: shown only when the pattern was edited since arming.
11. Duplicate controls (Continuous vs three-face button vs Move to selected): one action per button; the state block shows who moves the stage.
12. **Open:** "Show the order" draws lines without arrowheads.
13. Startup auto-load did not show user home and backlash: fixed.
14. Fresh fit had no camera settings: fixed.
15. Spin boxes clamped to 0–99.99: fixed (ranges per quantity).
16. Crosshair stuck, no hull undo, no keyboard Stop: fixed.
17. uEye-era labels and dead ini keys: removed (the file is still called `camera_params.ini`).
