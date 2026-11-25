"""The rastering GUI's main window: the control bar, the three steps (Setup, Pattern, Run), the jog
pad, the camera image and the log line, wired to the controller and the camera thread.

The window never waits for a motor: it calls SystemController (immediate answers, or stage jobs
whose outcome comes back as signals) and redraws from the controller's signals. Which buttons are
enabled is decided in one place, gates().
"""
from __future__ import annotations

import collections
import configparser
import os
import time
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

import theme
from calibration import camera_changed
from camera_thorlabs import CameraInfo, DisplayGeometry, ThorCamConfig, read_camera_ini, save_camera_ini
from config import SETTINGS_DEFAULTS_FILE, load_settings_defaults, save_settings_defaults
from raster_paths import nearest_index, points_from_spec
from raster_session import RasterPlan, RasterSnapshot
from stage import StagePosition
from views import CameraPanel, CameraView, ControlBar, JogPad, LogLine, Section, StepRail, label, sentence, set_prop
from views.camera_view import drawn_every, unreachable_mask
from views.pattern_step import USES_BOUNDS, PatternStep
from views.run_step import RunStep
from views.setup_step import SetupStep

HERE = os.path.dirname(os.path.abspath(__file__))
XY = Tuple[float, float]
SETUP, PATTERN, RUN = range(3)
LEFT_WIDTH = 420          # left column [logical px]
NO_FRAMES_S = 3.0         # no fps report for this long: the camera shows "No frames" [s]
TRAIL_KEEP = 10_000       # positions kept for the trail

ACTIVE = "A raster is active: stop it first."
CALIBRATING = "Finish or cancel the calibration first."
NEED_CAL = "Calibrate first (Setup)."
NEED_PATTERN = "No pattern: set one up in Pattern."
STEP_WHY = {"off": "Arm the pattern first (Arm here).", "armed": "BLACS steps this raster: Take back first.",
            "running": "A continuous run steps this raster: Stop it first."}
NO_USER_HOME = "No user home set: set one in Setup (Stage, User home)."
JOG_NOT_CALIBRATED ="Not calibrated: arrows follow the stage axes; check the direction on the image."


def gates(state: str, *, calibrated: bool, calibrating: bool, pattern_ok: bool,
          edited: bool, selected: bool, user_home: bool = True) -> Dict[str, Tuple[bool, str]]:
    """Every gated control: name -> (enabled, why not). The one place that decides it."""
    off = state == "off"

    def need(*conds) -> Tuple[bool, str]:
        for ok, why in conds:
            if not ok:
                return False, why
        return True, ""

    idle, quiet = (off, ACTIVE), (not calibrating, CALIBRATING)
    runnable = need(idle, (calibrated, NEED_CAL), (pattern_ok, NEED_PATTERN))
    return {
        "calibrate": need(),            # a raster armed: done, with a warning (raster_controller._apply)
        "cancel_calibration": need((calibrating, "")),
        "load_calibration": need(),
        "use_last": need(),
        "clear_calibration": need(),
        "save_calibration": need((calibrated, "No calibration to save.")),
        # Locked only while clicks are collected: clicks in two pixel frames make a fit that aims
        # nowhere. During a raster the stale-calibration mark shows a camera change.
        "restore_camera": need(quiet),
        "camera_geometry": need(quiet),
        "home": need(),                 # during a raster: done, red warning (stage.py)
        "backlash": need(),
        "moves": need(),                # during a raster: done, with the warning it earns (stage.py)
        "go_user_home": need((user_home, NO_USER_HOME)),
        "user_home_here": need(),       # copies the position shown into the boxes; Set is separate
        "start": runnable,
        "arm_blacs": runnable,
        "step": need((state == "local", STEP_WHY.get(state, ""))),
        "stop": need((not off, "No raster to stop.")),
        "hand": need((state in ("local", "running"), "BLACS has it already." if state == "armed"
                      else "Arm the pattern first.")),
        "take_back": need((state == "armed", "BLACS does not have the raster.")),
        "pause": need((state == "running", "No continuous run to pause.")),
        "continue": need((state == "local", "It is running already." if state == "running"
                          else STEP_WHY.get(state, ""))),
        "rearm": need((edited, ""), (calibrated, NEED_CAL), (pattern_ok, NEED_PATTERN)),
        "goto": need((state in ("local", "armed"), STEP_WHY["running"] if state == "running"
                      else "Arm the pattern first."),
                     (selected, "Pick a point first: click the image or type its number.")),
        "point": need((not off, "Arm the pattern first.")),
    }


# What File > Save settings as defaults keeps (each part's settings()).
SAVED_DEFAULTS = "pattern, run options, trail, jog steps and calibration spots"

# The highlighted Run button per state. None while BLACS steps the raster: the most visible button
# must not be one that takes the stage from a running sequence.
PRIMARY = {"off": "start", "local": "step", "running": "stop", "armed": None}


def jog_vector_mm(sx: int, sy: int, step_mm: float, geometry: DisplayGeometry, M=None) -> XY:
    """The stage move [mm] that moves the spot step_mm in screen direction (sx, sy), +y up.
    Calibrated: through the matrix M [mm/px]. Not calibrated: this rig's fixed camera-to-stage
    axes (stage x = -camera row, stage y = -camera column)."""
    dx = -sx if geometry.flip_x else sx          # screen -> image px of the shown (rotated) frame
    dy = -sy if geometry.flip_y else sy
    if M is not None:
        v = np.asarray(M, dtype=float) @ np.array([dx, dy], dtype=float)
        n = float(np.hypot(v[0], v[1]))
        if n > 0:
            return float(v[0]) * step_mm / n, float(v[1]) * step_mm / n
    k = geometry.rotation_k % 4                  # undo np.rot90(k) to get camera column, row
    col, row = {0: (dx, dy), 1: (-dy, dx), 2: (-dx, -dy), 3: (dy, -dx)}[k]
    return -row * step_mm, -col * step_mm


def _took(future) -> bool:
    """A stage job's Future ended with an ok Result (False: it failed or was cancelled)."""
    try:
        return bool(future.result().ok)
    except Exception:
        return False


def _gate(widget: QtWidgets.QWidget, on: bool, why: str) -> None:
    """Enable a control; while disabled its tooltip says why."""
    if widget.property("tip") is None:
        widget.setProperty("tip", widget.toolTip())
    widget.setEnabled(on)
    widget.setToolTip(widget.property("tip") if on or not why else why)


def _scrolled(page: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    area = QtWidgets.QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    area.setFrameShape(QtWidgets.QFrame.NoFrame)
    area.setWidget(page)
    return area


class RasterWindow(QtWidgets.QMainWindow):
    """controller: raster_controller.SystemController. camera: a camera_thorlabs.ThorCameraThread
    (not started; main starts it). camera_config / geometry / camera_file: what the camera opens
    with, from the camera settings file. remember_theme(light): stores the theme choice (main
    passes theme.save_light_pref; tests and scripts pass nothing)."""

    _backlash_not_set = QtCore.pyqtSignal(str)   # axis whose backlash Set failed (sent from the motor thread)

    def __init__(self, controller, camera, *, camera_config: ThorCamConfig, geometry: DisplayGeometry,
                 camera_file: str = "", log_dir: str = os.path.join(HERE, "logs"),
                 defaults_path: str = SETTINGS_DEFAULTS_FILE,
                 remember_theme: Optional[Callable[[bool], None]] = None, parent=None):
        super().__init__(parent)
        self.ctl, self.camera = controller, camera
        self._cam_base, self._geometry, self._camera_file = camera_config, geometry, camera_file
        self._roi = (camera_config.width, camera_config.height, camera_config.start_x, camera_config.start_y)
        self._log_dir, self._defaults_path, self._remember_theme = log_dir, defaults_path, remember_theme
        self._snap: RasterSnapshot = controller.raster_snapshot()
        self._pos: Optional[StagePosition] = None
        self._busy = False
        self._last_jog = None                # Future of the last arrow jog
        self._cal_progress = (0, 0)
        self._cal_clicks: List[XY] = []
        self._points: List[XY] = []
        self._fps: Optional[float] = None
        self._fps_t = 0.0
        self._trail = collections.deque(maxlen=TRAIL_KEEP)
        self._history = None                 # the open position-history file
        self._fill_from_frame = True         # bounds and spiral centre from the first frame size
        self._backlash_to_save: set = set()   # axes of the last backlash Set not answered yet
        self._backlash_taken = False         # one of them took its value: save the file when all answered
        self._cal_seen = None                # (path, affine) of the calibration last shown
        self._goto_shown = False             # a Go to x, y target was clicked or typed
        self._spec = None                    # the pattern last built (raster_paths.RasterSpec)
        self._pattern_error = ""
        self._drawn: dict = {}               # what the image shows now (big point lists: by identity)
        self.setWindowTitle("Rastering")

        self._build()
        self._build_menus()
        self._connect()
        link_kind, link_words = controller.blacs_link_status   # news sent before this window existed
        if link_words:
            self.log.add(link_words, error=link_kind != "info")
        self.camera_panel.set_geometry(geometry)
        self.camera_panel.set_settings_file(camera_file)
        self.view.set_geometry(geometry)
        self._apply_saved_settings()
        self._on_calibration()
        self._on_user_home()
        self.setup.show_backlash(*controller.backlash_mm)
        self._on_position(None)
        QtCore.QTimer.singleShot(0, self._pick_first_step)

    # ================================================================ layout
    def _build(self) -> None:
        central = QtWidgets.QWidget()
        central.setProperty("surface", "ground")
        central.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        v = QtWidgets.QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.bar = ControlBar()
        v.addWidget(self.bar)

        mid = QtWidgets.QHBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)
        left = QtWidgets.QWidget()
        left.setFixedWidth(LEFT_WIDTH)
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(14, 10, 14, 12)
        lv.setSpacing(8)
        self.rail = StepRail()
        lv.addWidget(self.rail)

        self.camera_panel = CameraPanel()
        self.setup = SetupStep(self.camera_panel)
        self.pattern = PatternStep()
        self.run = RunStep()
        self.pages = QtWidgets.QStackedWidget()
        for page in (self.setup, self.pattern, self.run):
            self.pages.addWidget(_scrolled(page))
        lv.addWidget(self.pages, 1)

        move = Section("Move stage")
        self._moving = label("Stage moving…", role="small")
        self._moving.hide()
        move.head.addWidget(self._moving)
        self.jog = JogPad()
        move.body.addWidget(self.jog, 0, 0)
        lv.addSpacing(6)
        lv.addWidget(move)
        mid.addWidget(left)

        rule = QtWidgets.QFrame()
        rule.setFixedWidth(1)
        rule.setProperty("role", "rule")
        rule.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        mid.addWidget(rule)
        self.view = CameraView()
        self.view.set_frame_source(self.camera.take_frame)
        mid.addWidget(self.view, 1)
        v.addLayout(mid, 1)

        self.log = LogLine()
        v.addWidget(self.log)
        self.setCentralWidget(central)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        a = file_menu.addAction("Save settings as defaults", self._save_defaults)
        a.setShortcut(QtGui.QKeySequence("Ctrl+D"))
        a.setToolTip(f"{sentence(SAVED_DEFAULTS)}, loaded at the next start.")
        file_menu.addAction("Revert to saved defaults", self._revert_defaults)
        file_menu.addSeparator()
        a = file_menu.addAction("Exit", self.close)
        a.setShortcut(QtGui.QKeySequence("Ctrl+Q"))
        view_menu = self.menuBar().addMenu("&View")
        self.light_action = view_menu.addAction("Light theme")
        self.light_action.setCheckable(True)
        self.light_action.setChecked(theme.is_light())
        self.light_action.toggled.connect(self._set_light)
        view_menu.addAction("Fit image", self.view.fit)
        stop = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Escape), self)
        stop.activated.connect(self._escape)

    # ================================================================ wiring
    def _connect(self) -> None:
        c = self.ctl
        c.message.connect(self._on_message)
        c.position_changed.connect(self._on_position)
        c.busy_changed.connect(self._on_busy)
        c.raster_changed.connect(self._on_raster)
        c.calibration_changed.connect(self._on_calibration)
        c.calibration_progress.connect(self._on_cal_progress)
        c.backlash_changed.connect(self._on_backlash)
        self._backlash_not_set.connect(self._backlash_answered)
        c.user_home_changed.connect(self._on_user_home)

        cam = self.camera
        cam.info.connect(self._on_camera_info)
        cam.applied.connect(self.camera_panel.show_applied)
        cam.fps.connect(self._on_fps)
        cam.message.connect(self.log.add)
        cam.error.connect(lambda text: self.log.add(text, error=True))

        p = self.camera_panel
        p.exposure_changed.connect(cam.set_exposure_ms)
        p.gain_changed.connect(cam.set_gain)
        p.gamma_changed.connect(cam.set_gamma)
        p.roi_requested.connect(cam.set_roi)
        p.geometry_changed.connect(self._set_geometry)
        p.save_requested.connect(self._save_camera_file)
        p.load_requested.connect(lambda: self._load_camera_file(None))
        p.revert_requested.connect(lambda: self._load_camera_file(self._camera_file))

        s = self.setup
        s.calibrate_btn.clicked.connect(self._calibrate)
        s.cancel_btn.clicked.connect(c.cancel_calibration)
        s.load_btn.clicked.connect(self._load_calibration)
        s.save_btn.clicked.connect(self._save_calibration)
        s.use_last_btn.clicked.connect(lambda: c.load_calibration())
        s.clear_btn.clicked.connect(c.clear_calibration)
        s.restore_camera_btn.clicked.connect(self._restore_camera)
        s.home_x_btn.clicked.connect(lambda: c.home("X"))
        s.home_y_btn.clicked.connect(lambda: c.home("Y"))
        s.home_both_btn.clicked.connect(lambda: (c.home("X"), c.home("Y")))
        s.user_home_set_btn.clicked.connect(self._set_user_home)
        s.user_home_here_btn.clicked.connect(self._user_home_here)
        s.backlash_set_btn.clicked.connect(self._set_backlash)

        self.pattern.changed.connect(self._pattern_changed)
        self.pattern.whole_image_btn.clicked.connect(self._bounds_to_frame)

        r = self.run
        r.options_changed.connect(self._pattern_changed)
        r.start_btn.clicked.connect(lambda: c.arm())
        r.arm_blacs_btn.clicked.connect(lambda: c.arm(for_blacs=True))
        r.step_btn.clicked.connect(c.step)
        r.stop_btn.clicked.connect(c.stop_raster)
        r.hand_btn.clicked.connect(c.hand_to_blacs)
        r.take_back_btn.clicked.connect(c.take_back)
        r.pause_btn.clicked.connect(c.pause_raster)
        r.continue_btn.clicked.connect(c.continue_raster)
        r.rearm_btn.clicked.connect(lambda: c.arm())
        r.goto_btn.clicked.connect(self._go_to_point)
        r.point_box.valueChanged.connect(lambda n: c.select_point(n) if n >= 0 else None)
        r.point_box.editingFinished.connect(self._refresh)    # not redrawn while typed in: catch up on leaving
        r.point_box.lineEdit().textEdited.connect(lambda *_: self._apply_gates())   # a typed number counts
        r.record.toggled.connect(self._record_history)
        r.trail.valueChanged.connect(lambda *_: self._draw_trail())

        j = self.jog
        j.jog_requested.connect(self._jog)
        j.go_to_requested.connect(c.move_mm)
        j.home_requested.connect(lambda: c.go_user_home())
        c.hand_move_answer.connect(lambda kind, text: j.show_answer(text, red=kind not in ("", "warning")))
        j.x_box.valueChanged.connect(self._goto_typed)
        j.y_box.valueChanged.connect(self._goto_typed)

        self.rail.step_selected.connect(self._show_step)
        self.view.clicked.connect(self._on_click)

        self._watchdog = QtCore.QTimer(self)
        self._watchdog.setInterval(1000)       # coarse: no precise timers on the GUI thread
        self._watchdog.timeout.connect(self._check_frames)
        self._watchdog.start()

    # ================================================================ derived state
    def camera_settings(self) -> dict:
        """The camera geometry a calibration depends on, as stored in the calibration file."""
        w, h, x, y = self._roi
        g = self._geometry
        return {"aoi": {"width": int(w), "height": int(h), "start_x": int(x), "start_y": int(y)},
                "rotation_k": int(g.rotation_k), "flip_x": bool(g.flip_x), "flip_y": bool(g.flip_y)}

    def calibration_state(self) -> str:
        """none | collecting | loaded | stale (the control bar's words)."""
        if self._cal_progress != (0, 0):
            return "collecting"
        cf = self.ctl.calibration_file
        if cf is None:
            return "none"
        return "stale" if camera_changed(self.camera_settings(), cf.camera_settings) else "loaded"

    def _frame_wh(self) -> Tuple[int, int]:
        w, h = int(self._roi[0]), int(self._roi[1])
        return (h, w) if self._geometry.rotation_k % 2 else (w, h)

    def _refresh(self) -> None:
        """Redraw everything that follows from the raster, calibration and camera state."""
        s, cal_state = self._snap, self.calibration_state()
        self.bar.set_status(state=s.state, point_index=s.point_index, path_len=len(s.path),
                            calibration=cal_state, cal_clicks=self._cal_progress,
                            shots_per_point=s.shots_per_step, pattern_edited=s.path_edited,
                            options_edited=s.options_edited, camera_fps=self._fps)
        self.rail.show_progress(self._fps is not None, cal_state, len(self._points), s.state)
        cf = self.ctl.calibration_file
        self.setup.show_calibration(cal_state, path=(cf.path or "") if cf else "",
                                    saved_at=cf.saved_at if cf else "",
                                    M=cf.affine.M if cf else None, b=cf.affine.b if cf else None,
                                    clicks=self._cal_progress)
        set_prop(self.setup.calibrate_btn, "kind", "primary" if cal_state in ("none", "stale") else "")
        self.run.show_state(s.state, path_len=len(s.path), selected=s.selected_index,
                            shots=s.shots_per_step, dropped=s.dropped)
        self._apply_gates()

    def _apply_gates(self) -> None:
        s = self._snap
        g = gates(s.state, calibrated=self.ctl.calibration is not None,
                  calibrating=self._cal_progress != (0, 0), pattern_ok=bool(self._points),
                  edited=s.path_edited, selected=s.selected_index >= 0 or self.run.point_typed(),
                  user_home=None not in self.ctl.user_home_mm)
        st, r = self.setup, self.run
        for key, widgets in (("calibrate", (st.calibrate_btn,)), ("cancel_calibration", (st.cancel_btn,)),
                             ("load_calibration", (st.load_btn,)), ("use_last", (st.use_last_btn,)),
                             ("clear_calibration", (st.clear_btn,)), ("save_calibration", (st.save_btn,)),
                             ("restore_camera", (st.restore_camera_btn,)),
                             ("home", (st.home_x_btn, st.home_y_btn, st.home_both_btn)),
                             ("backlash", (st.backlash_set_btn,)),
                             ("user_home_here", (st.user_home_here_btn,)),
                             ("start", (r.start_btn,)), ("arm_blacs", (r.arm_blacs_btn,)),
                             ("step", (r.step_btn,)), ("stop", (r.stop_btn,)), ("hand", (r.hand_btn,)),
                             ("take_back", (r.take_back_btn,)), ("pause", (r.pause_btn,)),
                             ("continue", (r.continue_btn,)), ("rearm", (r.rearm_btn,)),
                             ("goto", (r.goto_btn,)), ("point", (r.point_box,))):
            for w in widgets:
                _gate(w, *g[key])
        r.rearm_btn.setVisible(s.path_edited)
        primary = PRIMARY.get(s.state, "start")
        r.set_primary(getattr(r, primary + "_btn") if primary else None)
        self.jog.set_moves_enabled(*g["moves"])
        _gate(self.jog.home_btn, *g["go_user_home"])
        self.jog.show_note("" if self.ctl.calibration is not None else JOG_NOT_CALIBRATED)
        self.camera_panel.set_geometry_enabled(*g["camera_geometry"])
        self._moving.setVisible(self._busy and s.state == "off")

    # ================================================================ controller -> window
    def _on_message(self, kind: str, text: str) -> None:
        """kind "info" and "warning" in the ink colour; "red_warning", "refused", "error" in red."""
        self.log.add(sentence(text), error=kind not in ("info", "warning"))

    # Signals from the motor thread arrive queued, those from the GUI thread at once, so an older
    # one can arrive after a newer one: each handler reads the controller's state now.

    def _on_position(self, pos: Optional[StagePosition]) -> None:
        pos = self.ctl.position or pos
        if pos is None or pos == self._pos:
            return
        self._pos = pos
        self.bar.set_status(x_mm=pos.mm[0], y_mm=pos.mm[1])
        self.view.set_spot(pos.px)
        if pos.moving:                       # on the way: shown, but trail and history keep stops only
            return
        if pos.px is not None:
            self._trail.append(pos.px)
            self._draw_trail()
        self._write_history(pos)

    def _write_history(self, pos: StagePosition) -> None:
        if self._history is None:
            return
        px = pos.px or (float("nan"), float("nan"))
        try:
            self._history.write(f"{time.time():.3f},{pos.mm[0]:.5f},{pos.mm[1]:.5f},{px[0]:.2f},{px[1]:.2f}\n")
            self._history.flush()
        except OSError as e:
            self.log.add(f"Position history not written, recording stopped: {e}", error=True)
            self.run.record.setChecked(False)

    def _on_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._apply_gates()

    def _on_raster(self, _snap: RasterSnapshot) -> None:
        self._snap = snap = self.ctl.raster_snapshot()
        if snap.path is not self._drawn.get("armed"):     # the path is one tuple per arm: redraw on a new arm only
            self._drawn["armed"] = snap.path
            self.view.set_armed_path(snap.path, snap.pattern_len)
        sel = snap.selected_index
        self.view.set_selected_point(snap.path[sel] if 0 <= sel < len(snap.path) else None)
        self._draw_pattern()
        self._refresh()

    def _on_calibration(self, *_) -> None:
        # Another calibration (loaded, fitted, cleared): a backlash answer still to come is not
        # saved into it; after a load the motors take that file's backlash next. The same one saved
        # again (user home Set, Save) keeps it: the answer is saved into that file when it comes.
        cf = self.ctl.calibration_file
        now = (cf.path, cf.affine) if cf is not None else None
        same = now is not None and self._cal_seen is not None and now[0] == self._cal_seen[0] \
            and now[1] is self._cal_seen[1]
        self._cal_seen = now
        if not same:
            self._backlash_to_save.clear()
            self._backlash_taken = False
        self._update_unreachable()
        self._show_goto()
        if self._points:
            self._show_pattern_result()
        self._refresh()

    def _on_cal_progress(self, *_) -> None:
        self._cal_progress = self.ctl.calibration_clicks
        if self._cal_progress == (0, 0):
            self._cal_clicks = []
        self.view.set_calibration_clicks(self._cal_clicks)
        self._refresh()

    def _on_backlash(self, axis: str, value: float) -> None:
        self.setup.show_backlash(*self.ctl.backlash_mm, fill=axis)
        if axis in self._backlash_to_save:
            self._backlash_taken = True
            self._backlash_answered(axis)

    def _backlash_answered(self, axis: str) -> None:
        """One axis of the backlash Set took its value or failed: once all have answered, save the
        calibration file if any took it."""
        if axis not in self._backlash_to_save:
            return
        self._backlash_to_save.discard(axis)
        if not self._backlash_to_save and self._backlash_taken:
            self._backlash_taken = False
            self._keep_in_calibration("The backlash")

    def _on_user_home(self, *_) -> None:
        self.setup.show_user_home(*self.ctl.user_home_mm)
        self._apply_gates()

    # ================================================================ camera
    def _on_camera_info(self, info: CameraInfo) -> None:
        self.camera_panel.show_info(info)
        self._roi = (int(info.roi_width), int(info.roi_height), int(info.roi_x), int(info.roi_y))
        self._frame_changed()

    def _on_fps(self, fps: float) -> None:
        was = self._fps is None
        self._fps, self._fps_t = float(fps), time.monotonic()
        self.bar.set_status(camera_fps=self._fps)
        if was:
            self._refresh()

    def _check_frames(self) -> None:
        if self._fps is not None and time.monotonic() - self._fps_t > NO_FRAMES_S:
            self._fps = None
            self._refresh()

    def _set_geometry(self, geometry: DisplayGeometry) -> None:
        self._geometry = geometry
        self.view.set_geometry(geometry)
        self._frame_changed()

    def _frame_changed(self) -> None:
        """The shown frame's size or geometry changed: fill defaults once, redraw what depends on it."""
        w, h = self._frame_wh()
        if self._fill_from_frame:
            self._fill_from_frame = False
            self.pattern.set_bounds((0, w, 0, h), emit=False)
            self.pattern.cx.set_quietly(w / 2)
            self.pattern.cy.set_quietly(h / 2)
            self._pattern_changed()
        self._update_unreachable()
        self._refresh()

    def _bounds_to_frame(self) -> None:
        w, h = self._frame_wh()
        self.pattern.set_bounds((0, w, 0, h))

    def _update_unreachable(self) -> None:
        cal = self.ctl.calibration
        if cal is None:
            self.view.set_unreachable(None)
            return
        w, h = self._frame_wh()
        self.view.set_unreachable(unreachable_mask(w, h, cal.M, cal.b, self.ctl.travel_mm))

    def _save_camera_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save camera settings",
                                                        self._camera_file or HERE, "Camera settings (*.ini)")
        if not path:
            return
        w, h, x, y = self._roi
        cfg = replace(self.camera_panel.camera_config(self._cam_base), width=w, height=h, start_x=x, start_y=y)
        try:
            save_camera_ini(path, cfg, self._geometry)
        except OSError as e:
            self.log.add(f"Camera settings not saved to {path}: {e}", error=True)
            return
        self._camera_file = path
        self.camera_panel.set_settings_file(path)
        self.log.add(f"Camera settings saved to {path}.")

    def _load_camera_file(self, path: Optional[str]) -> None:
        if path is None:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load camera settings",
                                                            self._camera_file or HERE, "Camera settings (*.ini)")
        if not path:
            return
        try:
            cfg, geom = read_camera_ini(path, self._cam_base, self._geometry)
        except (OSError, ValueError, configparser.Error) as e:
            self.log.add(f"Camera settings not loaded: {e}", error=True)
            return
        self.camera.apply_config(cfg)
        self.camera_panel.set_geometry(geom)
        self._set_geometry(geom)
        self._camera_file = path
        self.camera_panel.set_settings_file(path)
        self.log.add(f"Camera settings loaded from {path}.")

    def _restore_camera(self) -> None:
        cf = self.ctl.calibration_file
        cs = cf.camera_settings if cf else None
        if not cs:
            return
        aoi = cs.get("aoi") or {}
        if all(k in aoi for k in ("width", "height", "start_x", "start_y")):
            self.camera.set_roi(int(aoi["width"]), int(aoi["height"]), int(aoi["start_x"]), int(aoi["start_y"]))
        g = self._geometry
        geom = DisplayGeometry(rotation_k=cs.get("rotation_k", g.rotation_k), flip_x=cs.get("flip_x", g.flip_x),
                               flip_y=cs.get("flip_y", g.flip_y))
        self.camera_panel.set_geometry(geom)
        self._set_geometry(geom)
        self.log.add("Camera region, rotation and mirror set to the calibration's.")

    # ================================================================ calibration and stage
    def _calibrate(self) -> None:
        self._cal_clicks = []
        self.ctl.start_calibration(self.setup.spots.value(), camera_settings=self.camera_settings())

    def _load_calibration(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load calibration", HERE, "Calibration (*.json)")
        if path:
            self.ctl.load_calibration(path)

    def _save_calibration(self) -> None:
        cf = self.ctl.calibration_file
        if cf is None:
            return
        default = os.path.join(HERE, time.strftime("cal_%Y%m%d_%H%M.json"))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save calibration as", default, "Calibration (*.json)")
        if path:
            # a calibration from an older file has no camera settings: store today's with it
            self.ctl.save_calibration(path, camera_settings=None if cf.camera_settings else self.camera_settings())

    def _set_user_home(self) -> None:
        x, y = self.setup.user_home_x.value(), self.setup.user_home_y.value()   # before the echo refills them
        self.ctl.set_user_home("X", x)
        self.ctl.set_user_home("Y", y)
        self._keep_in_calibration("The user home")

    def _user_home_here(self) -> None:
        if self._pos is not None:
            self.setup.user_home_x.set_quietly(self._pos.mm[0])
            self.setup.user_home_y.set_quietly(self._pos.mm[1])

    def _set_backlash(self) -> None:
        now, sent = self.ctl.backlash_mm, False
        for axis, box, v in (("X", self.setup.backlash_x, now[0]), ("Y", self.setup.backlash_y, now[1])):
            value = box.value()
            if v is None or abs(value - v) > 1e-9:
                self._backlash_to_save.add(axis)
                self.ctl.set_backlash(axis, value).add_done_callback(
                    lambda f, axis=axis: None if _took(f) else self._backlash_not_set.emit(axis))
                sent = True
        if not sent:
            self.log.add("Backlash unchanged: the motors already have these values.")

    def _keep_in_calibration(self, what: str) -> None:
        """User home and backlash live in the calibration file: save it now, or say they are not kept."""
        cf = self.ctl.calibration_file
        if cf is not None and cf.path:
            self.log.add(f"{what} is kept in the calibration file: saving {os.path.basename(cf.path)}.")
            self.ctl.save_calibration(cf.path)
        else:
            self.log.add(f"{what} is kept only in a calibration file and none is in use: it is lost on "
                         "exit unless you save a calibration (Setup, Save as…).", error=True)

    def _jog(self, sx: int, sy: int, step_mm: float) -> None:
        if self.jog.held() and self._last_jog is not None and not self._last_jog.done():
            return                           # a held arrow repeats only once its last step is done
        cal = self.ctl.calibration
        dx, dy = jog_vector_mm(sx, sy, step_mm, self._geometry, cal.M if cal is not None else None)
        self._last_jog = self.ctl.jog_mm(dx, dy)

    def _goto_typed(self, *_) -> None:
        self._goto_shown = True
        self._show_goto()

    def _show_goto(self) -> None:
        """Mark on the image where Go to x, y would put the spot."""
        cal = self.ctl.calibration
        if not self._goto_shown or cal is None:
            self.view.set_manual_markers([])
            return
        self.view.set_manual_markers([cal.mm_to_px(self.jog.x_box.value(), self.jog.y_box.value())])

    # ================================================================ pattern and run
    def _pattern_changed(self) -> None:
        """A pattern or run option changed: rebuild the points if the pattern did (up to 2 million
        points take about a second), show them, send the pending plan."""
        spec = self.pattern.spec()
        if spec != self._spec:
            self._spec = spec
            try:
                self._points, self._pattern_error = points_from_spec(spec), ""
            except ValueError as e:
                self._points, self._pattern_error = [], str(e)
        self._show_pattern_result()
        o = self.run.settings()
        self.ctl.set_pending_plan(RasterPlan(spec, pingpong=o["pingpong"], continuous=o["continuous"],
                                             delay_s=o["delay_s"]) if self._points else None)
        self._draw_pattern()
        self._refresh()

    def _show_pattern_result(self) -> None:
        pts = self._points
        outside = 0
        if pts and self.ctl.calibration is not None:
            outside = len(pts) - sum(self.ctl.reachable(pts))
        self.pattern.show_result(len(pts) if pts else None, error=self._pattern_error, outside=outside,
                                 pingpong=self.run.pingpong.isChecked(), drawn_every=drawn_every(len(pts)))

    def _draw_pattern(self) -> None:
        s, kind = self._snap, self.pattern.kind_key()
        show = self.rail.current() != SETUP          # Setup: a clear image for the calibration clicks
        pending = self._points if show and (s.state == "off" or s.path_edited) else ()
        if pending is not self._drawn.get("pending"):    # up to 2 million points: not again on every step
            self._drawn["pending"] = pending
            self.view.set_pending_pattern(pending)
        self.view.set_pattern_corners(self.pattern.corners() if show and kind == "hull" else [])
        bounds = self._spec.bounds if self._spec is not None else None   # the drawn pattern's, not half-typed
        self.view.set_scan_bounds(bounds if show and kind in USES_BOUNDS else None)
        on = self.pattern.show_direction.isChecked()
        if on != self._drawn.get("order"):
            self._drawn["order"] = on
            self.view.set_direction_lines(on)

    def _go_to_point(self) -> None:
        n = self.run.point_box.value()       # a number typed but not entered is picked first
        if n >= 0:
            self.ctl.go_to_point(n)

    def _draw_trail(self) -> None:
        n = self.run.trail_len()             # runs on every position: never takes half-typed text
        self.view.set_trail(list(self._trail)[-n:] if n > 0 else [])

    def _record_history(self, on: bool) -> None:
        if not on:
            if self._history is not None:
                self._history.close()
                self._history = None
                self.log.add("Position history file closed.")
            self.run.show_record_file("")
            return
        path = os.path.join(self._log_dir, time.strftime("position_history_%Y%m%d_%H%M%S.csv"))
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            self._history = open(path, "w", newline="")
            self._history.write("time_s,x_mm,y_mm,x_px,y_px\n")
        except OSError as e:
            self.log.add(f"Position history not started: {e}", error=True)
            self.run.record.setChecked(False)
            return
        self.run.show_record_file(os.path.basename(path))
        self.log.add(f"Recording stage positions to {path}.")
        if self._pos is not None:
            self._write_history(self._pos)

    def _escape(self) -> None:
        """Esc: stop the GUI's own raster (held here or running) and stop collecting calibration
        spots. A raster BLACS holds stays armed: an Esc pressed by habit, to leave a box, would park
        the stage while BLACS goes on shooting one spot. The Stop button stops BLACS's raster. The
        controller decides under its lock: the raster may have changed hands a moment ago."""
        if self._cal_progress != (0, 0):
            self.ctl.cancel_calibration()
        if self.ctl.stop_raster(gui_only=True) == "armed":
            self.log.add("Esc leaves BLACS's raster armed. To stop it, press Stop.")

    # ================================================================ the image
    def _on_click(self, x: float, y: float, modifiers: int) -> None:
        """Calibrating: a calibration spot. Ctrl, or the Run step with a raster armed: pick the
        nearest path point. Pattern step: a hull corner or the spiral centre. Otherwise (Setup, or
        Run with nothing armed): the Go to x, y target."""
        if self._cal_progress != (0, 0):
            self._cal_clicks.append((x, y))
            self.view.set_calibration_clicks(self._cal_clicks)
            self.ctl.add_calibration_click(x, y)
            return
        step = self.rail.current()
        path = self.ctl.raster_snapshot().path
        if modifiers & int(QtCore.Qt.ControlModifier) or (step == RUN and path):
            self.ctl.select_point(nearest_index(path, x, y) if path else 0)
            return
        if step == PATTERN:
            kind = self.pattern.kind_key()
            if kind == "hull":
                self.pattern.add_corner(x, y)
            elif kind == "spiral":
                self.pattern.set_centre(x, y)
            else:
                self.log.add("A click on the image sets nothing for a square pattern: its box is the "
                             "numbers under Scan bounds.")
            return
        cal = self.ctl.calibration
        if cal is None:
            self.log.add("Not calibrated: a click on the image has no stage position yet.")
            return
        self.jog.set_go_to(*cal.px_to_mm(x, y))
        self._goto_shown = True
        self._show_goto()

    def _show_step(self, index: int) -> None:
        self.rail.set_current(index)
        self.pages.setCurrentIndex(index)
        self._draw_pattern()

    def _pick_first_step(self) -> None:
        calibrated = self.ctl.calibration is not None
        self._show_step(SETUP if not calibrated else RUN if self._points else PATTERN)

    # ================================================================ settings, theme, exit
    def _saved_parts(self):
        """(key in the defaults file, view part). Run before pattern: the pattern's change reads
        the run options."""
        return (("run", self.run), ("pattern", self.pattern), ("jog", self.jog), ("calibration", self.setup))

    def _apply_saved_settings(self) -> bool:
        """Put the saved defaults in the views. Returns whether a defaults file was read."""
        path = self._defaults_path
        try:
            d = load_settings_defaults(path)
        except (OSError, ValueError) as e:
            self.log.add(f"Saved defaults not read from {path} ({e}): the window's own values are shown.",
                         error=True)
            d = None
        found, d = d is not None, d or {}
        try:
            sub = {key: d[key] if isinstance(d.get(key), dict) else {} for key, _ in self._saved_parts()}
            if isinstance(sub["pattern"].get("bounds"), list):
                self._fill_from_frame = False          # saved bounds win over the first frame's size
            for key, part in self._saved_parts():     # the pattern's emits changed once: points and plan
                part.apply_settings(sub[key])
        except Exception as e:          # a hand-edited value of the wrong type: open all the same
            self.log.add(f"Saved defaults in {path} not all used ({type(e).__name__}: {e}): the values "
                         "after the bad one are the window's own.", error=True)
            self._pattern_changed()
        return found

    def settings(self) -> dict:
        """What File > Save settings as defaults writes."""
        return {key: part.settings() for key, part in self._saved_parts()}

    def _save_defaults(self) -> None:
        try:
            save_settings_defaults(self.settings(), self._defaults_path)
        except (OSError, TypeError, ValueError) as e:
            self.log.add(f"Defaults not saved: {e}", error=True)
            return
        self.log.add(f"{sentence(SAVED_DEFAULTS)} saved as defaults ({os.path.basename(self._defaults_path)}).")

    def _revert_defaults(self) -> None:
        """File > Revert to saved defaults."""
        if self._apply_saved_settings():
            self.log.add(f"{sentence(SAVED_DEFAULTS)} set back to the saved defaults "
                         f"({os.path.basename(self._defaults_path)}).")
        elif not os.path.exists(self._defaults_path):
            self.log.add("No saved defaults yet: File > Save settings as defaults saves the ones shown.")

    def _set_light(self, on: bool) -> None:
        theme.apply_theme(QtWidgets.QApplication.instance(), light=bool(on))
        if self._remember_theme is not None:
            self._remember_theme(bool(on))

    def closeEvent(self, event) -> None:
        self._watchdog.stop()
        self._record_history(False)
        self.camera.stop()
        self.camera.wait(2000)
        super().closeEvent(event)
