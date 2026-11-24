"""Camera view: the live image with the pattern, the stage spot and the other marks on it.

Coordinates in and out are image px of the shown frame AFTER rotation (the frame
calibration, pattern and clicks live in); mirroring is view only. Frames are
painted on a ~40 ms timer, newest only.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

import theme
from camera_thorlabs import DisplayGeometry

XY = Tuple[float, float]
_CACHE = QtWidgets.QGraphicsItem.DeviceCoordinateCache
_HATCH_UP = 3          # mask cells drawn 3 x 3 so the hatch has room


def unreachable_mask(frame_w: int, frame_h: int, M, b, motor_bounds, cell_px: int = 8) -> np.ndarray:
    """Coarse bool mask (rows = image y, cols = image x, one cell per cell_px px),
    True where the affine map motor = M @ (x, y) + b lands outside
    motor_bounds = (xmin, xmax, ymin, ymax) [mm]. For CameraView.set_unreachable."""
    nx = max(1, int(np.ceil(frame_w / cell_px)))
    ny = max(1, int(np.ceil(frame_h / cell_px)))
    xs = (np.arange(nx) + 0.5) * (frame_w / nx)
    ys = (np.arange(ny) + 0.5) * (frame_h / ny)
    X, Y = np.meshgrid(xs, ys)
    M = np.asarray(M, dtype=float)
    mx = M[0, 0] * X + M[0, 1] * Y + float(b[0])
    my = M[1, 0] * X + M[1, 1] * Y + float(b[1])
    xmin, xmax, ymin, ymax = motor_bounds
    return ~((mx >= xmin) & (mx <= xmax) & (my >= ymin) & (my <= ymax))


def _reticle() -> QtGui.QPainterPath:
    """Crosshair with an open centre, so the spot itself stays visible."""
    p = QtGui.QPainterPath()
    for x0, y0, x1, y1 in ((-0.5, 0, -0.16, 0), (0.16, 0, 0.5, 0), (0, -0.5, 0, -0.16), (0, 0.16, 0, 0.5)):
        p.moveTo(x0, y0)
        p.lineTo(x1, y1)
    return p


def _xs_ys(points: Optional[Sequence[XY]]):
    if points is None or len(points) == 0:
        return np.empty(0), np.empty(0)
    a = np.asarray(points, dtype=float).reshape(-1, 2)
    return a[:, 0], a[:, 1]


class CameraView(QtWidgets.QWidget):
    """Live camera image and overlays.

    Signals: clicked(x, y, modifiers) on a left click (image px; modifiers is the
    int of Qt.KeyboardModifiers); hovered(x, y) as the pointer moves over the image."""

    clicked = QtCore.pyqtSignal(float, float, int)
    hovered = QtCore.pyqtSignal(float, float)

    def __init__(self, parent=None, frame_interval_ms: int = 40):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._plot = pg.PlotWidget()
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        lay.addWidget(self._plot)
        self._pi = self._plot.getPlotItem()
        self._vb = self._pi.getViewBox()
        self._vb.setAspectLocked(True)
        self._vb.setDefaultPadding(0.0)
        self._vb.setMenuEnabled(False)

        self._geom = DisplayGeometry()
        self._frame_wh: Optional[Tuple[int, int]] = None
        self._last_raw: Optional[np.ndarray] = None
        self._newest: Optional[np.ndarray] = None
        self._source: Optional[Callable[[], Optional[np.ndarray]]] = None
        self.frames_painted = 0
        self._mask: Optional[np.ndarray] = None
        self._pending: list = []
        self._armed: list = []
        self._directions = False
        self._build_items()

        self._fit_btn = QtWidgets.QToolButton(self._plot)
        self._fit_btn.setText("Fit")
        self._fit_btn.setToolTip("Show the whole image again (wheel zooms, drag pans)")
        self._fit_btn.setCursor(QtCore.Qt.ArrowCursor)
        self._fit_btn.clicked.connect(self.fit)

        scene = self._plot.scene()
        scene.sigMouseClicked.connect(self._on_click)
        scene.sigMouseMoved.connect(self._on_move)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(int(frame_interval_ms))   # coarse timer: never PreciseTimer on the GUI thread
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        theme.changed().connect(self._restyle)
        self._restyle()

    # ---------------- items ----------------
    def _build_items(self) -> None:
        o = theme.OVERLAY
        screen = QtGui.QGuiApplication.primaryScreen()
        k = max(1.0, screen.devicePixelRatio()) if screen else 1.0

        def pen(color, width, **kw):
            # pyqtgraph pens are cosmetic (device px): scale so lines keep their weight at 150 %
            return pg.mkPen(color, width=width * k, **kw)

        halo = pen(QtGui.QColor(0, 0, 0, 170), 4)
        well = pen(o["well"], 1)

        def add(item, z, cache=True):
            item.setZValue(z)
            if cache:
                item.setCacheMode(_CACHE)
            self._pi.addItem(item, ignoreBounds=True)
            return item

        self._image = pg.ImageItem(axisOrder="row-major")
        self._image.setZValue(-10)
        self._pi.addItem(self._image)
        self._unreach = add(pg.ImageItem(axisOrder="row-major"), -5)

        fault = QtGui.QColor(o["fault"])
        self._fault_rgb = (fault.red(), fault.green(), fault.blue())

        self._bounds = QtWidgets.QGraphicsRectItem()
        self._bounds.setPen(pen(o["path"], 1.5, style=QtCore.Qt.DashLine))
        self._bounds.setBrush(QtGui.QBrush(QtCore.Qt.NoBrush))
        self._bounds.setVisible(False)
        add(self._bounds, 1)

        path_c = QtGui.QColor(o["path"])
        line_c = QtGui.QColor(path_c)
        line_c.setAlpha(150)
        self._dir_line = add(pg.PlotDataItem(pen=pen(line_c, 1)), 2)
        self._corner_line = add(pg.PlotDataItem(pen=pen(line_c, 1.5, style=QtCore.Qt.DashLine)), 2)

        self._pending_item = add(pg.ScatterPlotItem(size=8, symbol="o", pen=pen(o["path"], 1.6),
                                                    brush=None), 3)
        self._armed_item = add(pg.ScatterPlotItem(size=7, symbol="o", pen=well, brush=pg.mkBrush(o["path"])), 4)
        self._corner_item = add(pg.ScatterPlotItem(size=14, symbol="d", pen=pen(o["well"], 1.5),
                                                   brush=pg.mkBrush(o["path"])), 5)

        trail_c = QtGui.QColor(o["spot"])
        trail_c.setAlpha(130)
        self._trail_item = add(pg.ScatterPlotItem(size=5, pen=None, brush=pg.mkBrush(trail_c)), 6)
        self._manual_halo = add(pg.ScatterPlotItem(size=13, symbol="x", pen=halo, brush=None), 7)
        self._manual_item = add(pg.ScatterPlotItem(size=13, symbol="x", pen=pen(o["ink"], 2),
                                                   brush=None), 7.1)
        self._cal_item = add(pg.ScatterPlotItem(size=18, symbol="o", pen=pen(o["ink"], 2),
                                                brush=None), 8)
        self._cal_labels: list = []

        self._sel_halo = add(pg.ScatterPlotItem(size=22, symbol="o", pen=halo, brush=None), 9)
        self._sel_item = add(pg.ScatterPlotItem(size=22, symbol="o", pen=pen(o["ink"], 2),
                                                brush=None), 9.1)
        reticle = _reticle()
        self._spot_halo = add(pg.ScatterPlotItem(size=40, symbol=reticle, pen=pen(QtGui.QColor(0, 0, 0, 190), 5),
                                                 brush=None), 10)
        self._spot_item = add(pg.ScatterPlotItem(size=40, symbol=reticle, pen=pen(o["spot"], 2.2),
                                                 brush=None), 10.1)

        hover_c = QtGui.QColor(o["ink"])
        hover_c.setAlpha(70)
        hp = pen(hover_c, 1, style=QtCore.Qt.DashLine)
        self._hover_v = pg.InfiniteLine(angle=90, movable=False, pen=hp)
        self._hover_h = pg.InfiniteLine(angle=0, movable=False, pen=hp)
        for ln in (self._hover_v, self._hover_h):
            ln.setZValue(11)
            ln.setVisible(False)
            self._pi.addItem(ln, ignoreBounds=True)

    def _restyle(self) -> None:
        t = theme.DARK            # dark surround in both themes: a light one glares around the image
        self._plot.setBackground(t["well"])
        tick_font = theme.font("small")
        for name in ("left", "bottom"):
            ax = self._pi.getAxis(name)
            ax.setPen(pg.mkPen(t["rule"]))
            ax.setTextPen(pg.mkPen(t["label"]))
            ax.setStyle(tickFont=tick_font, tickLength=-5, tickTextOffset=4)
        self._pi.setLabel("bottom", "image x (px)", color=t["label"], **{"font-size": "9pt"})
        self._pi.setLabel("left", "image y (px)", color=t["label"], **{"font-size": "9pt"})
        self._place_fit_button()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_fit_button()

    def _place_fit_button(self) -> None:
        self._fit_btn.adjustSize()
        self._fit_btn.move(self._plot.width() - self._fit_btn.width() - 8, 8)

    # ---------------- frames ----------------
    def set_frame_source(self, take: Optional[Callable[[], Optional[np.ndarray]]]) -> None:
        """take() returns the newest frame not yet shown, or None
        (e.g. camera_thorlabs.ThorCameraThread.take_frame). Polled every tick."""
        self._source = take

    def show_frame(self, frame: np.ndarray) -> None:
        """Offer a frame (2D uint8, camera orientation). Only the newest offered
        before the next tick is painted."""
        self._newest = frame

    def paint_now(self) -> None:
        """Paint the newest frame now instead of on the next tick."""
        self._tick()

    def frame_size(self) -> Optional[Tuple[int, int]]:
        """(width, height) [px] of the shown, rotated frame; None before the first frame."""
        return self._frame_wh

    def _tick(self) -> None:
        frame = None
        if self._source is not None:
            try:
                frame = self._source()
            except Exception:
                frame = None
        if frame is None:
            frame, self._newest = self._newest, None
        if frame is not None:
            self._last_raw = frame
            self._paint(frame)

    def _paint(self, raw: np.ndarray) -> None:
        k = self._geom.rotation_k
        frame = np.rot90(raw, k) if k else raw
        self._image.setImage(frame, autoLevels=False, levels=(0, 255))
        self.frames_painted += 1
        wh = (int(frame.shape[1]), int(frame.shape[0]))
        if wh != self._frame_wh:
            self._frame_wh = wh
            self._image.setRect(QtCore.QRectF(0, 0, wh[0], wh[1]))
            self._stretch_mask()
            self.fit()

    # ---------------- geometry ----------------
    def set_geometry(self, geometry: DisplayGeometry) -> None:
        """Rotation and mirror of the shown image, as one value."""
        if geometry == self._geom:
            return
        rotated = geometry.rotation_k != self._geom.rotation_k
        self._geom = geometry
        self._vb.invertX(geometry.flip_x)
        self._vb.invertY(geometry.flip_y)
        if rotated and self._last_raw is not None:
            self._paint(self._last_raw)

    def display_geometry(self) -> DisplayGeometry:
        return self._geom

    def fit(self) -> None:
        """Show the whole frame."""
        if self._frame_wh:
            w, h = self._frame_wh
            self._vb.setRange(QtCore.QRectF(0, 0, w, h), padding=0.0)

    # ---------------- overlays (image px) ----------------
    def set_pending_pattern(self, points: Sequence[XY]) -> None:
        """The pattern drawn but not armed: open rings in the path colour."""
        self._pending = list(points or [])
        self._pending_item.setData(*_xs_ys(self._pending))
        self._draw_directions()

    def set_armed_path(self, points: Sequence[XY], pattern_len: Optional[int] = None) -> None:
        """The armed path: filled dots in the path colour. With ping-pong the path is
        the pattern then itself reversed; pass pattern_len to draw only the pattern."""
        pts = list(points or [])
        if pattern_len is not None and pattern_len >= 0:
            pts = pts[:int(pattern_len)]
        self._armed = pts
        self._armed_item.setData(*_xs_ys(pts))
        self._draw_directions()

    def set_pattern_corners(self, points: Sequence[XY]) -> None:
        """Corners the operator clicked for an outline pattern, joined into a closed outline."""
        xs, ys = _xs_ys(points)
        self._corner_item.setData(xs, ys)
        if len(xs) >= 2:
            self._corner_line.setData(np.r_[xs, xs[:1]], np.r_[ys, ys[:1]])
        else:
            self._corner_line.setData([], [])

    def set_direction_lines(self, on: bool) -> None:
        """Join the points in path order (the armed path if there is one, else the pending one)."""
        self._directions = bool(on)
        self._draw_directions()

    def _draw_directions(self) -> None:
        pts = self._armed or self._pending
        if self._directions and len(pts) >= 2:
            self._dir_line.setData(*_xs_ys(pts))
        else:
            self._dir_line.setData([], [])

    def set_selected_point(self, xy: Optional[XY]) -> None:
        """Ring around the path point picked for 'go to point'; None hides it."""
        xs, ys = _xs_ys([xy] if xy is not None else None)
        self._sel_halo.setData(xs, ys)
        self._sel_item.setData(xs, ys)

    def set_spot(self, xy: Optional[XY]) -> None:
        """Where the stage puts the laser spot now (spot-colour crosshair); None hides it."""
        xs, ys = _xs_ys([xy] if xy is not None else None)
        self._spot_halo.setData(xs, ys)
        self._spot_item.setData(xs, ys)

    def set_trail(self, points: Sequence[XY]) -> None:
        """Where the spot has been (small faint spot-colour dots)."""
        self._trail_item.setData(*_xs_ys(points))

    def set_manual_markers(self, points: Sequence[XY]) -> None:
        """Crosses at points the operator marked (e.g. 'go to' previews)."""
        xs, ys = _xs_ys(points)
        self._manual_halo.setData(xs, ys)
        self._manual_item.setData(xs, ys)

    def set_calibration_clicks(self, points: Sequence[XY]) -> None:
        """Numbered rings at the calibration clicks so far."""
        xs, ys = _xs_ys(points)
        self._cal_item.setData(xs, ys)
        for item in self._cal_labels:
            self._pi.removeItem(item)
        self._cal_labels = []
        f = theme.font("body")
        f.setWeight(QtGui.QFont.DemiBold)
        for i, (x, y) in enumerate(zip(xs, ys), start=1):
            t = pg.TextItem(str(i), color=theme.OVERLAY["ink"], anchor=(-0.35, 1.25),
                            fill=pg.mkBrush(0, 0, 0, 160))
            t.setFont(f)
            t.setPos(float(x), float(y))
            t.setZValue(8.5)
            t.setCacheMode(_CACHE)
            self._pi.addItem(t, ignoreBounds=True)
            self._cal_labels.append(t)

    def set_scan_bounds(self, bounds: Optional[Tuple[float, float, float, float]]) -> None:
        """(xmin, xmax, ymin, ymax) [image px] as a dashed outline; None hides it."""
        if bounds is None:
            self._bounds.setVisible(False)
            return
        xmin, xmax, ymin, ymax = (float(v) for v in bounds)
        self._bounds.setRect(QtCore.QRectF(xmin, ymin, xmax - xmin, ymax - ymin))
        self._bounds.setVisible(True)

    def set_unreachable(self, mask: Optional[np.ndarray]) -> None:
        """Shade where the stage cannot reach. mask: 2D bool, rows = image y,
        cols = image x, covering the whole frame at any coarse resolution
        (see unreachable_mask). Drawn as one small image stretched over the frame."""
        self._mask = None if mask is None else np.asarray(mask, dtype=bool)
        if self._mask is None or not self._mask.any():
            self._unreach.setVisible(False)
            return
        # Hatched, so it reads as 'not here' without colour; still one small image.
        up = np.kron(self._mask, np.ones((_HATCH_UP, _HATCH_UP), dtype=bool))
        ii, jj = np.indices(up.shape)
        stripe = (ii + jj) % 6 < 2
        rgba = np.zeros(up.shape + (4,), dtype=np.uint8)
        rgba[..., :3] = self._fault_rgb
        rgba[..., 3] = np.where(up, np.where(stripe, 105, 35), 0)
        self._unreach.setImage(rgba, autoLevels=False, levels=(0, 255))
        self._unreach.setVisible(True)
        self._stretch_mask()

    def _stretch_mask(self) -> None:
        if self._mask is not None and self._frame_wh:
            w, h = self._frame_wh
            self._unreach.setRect(QtCore.QRectF(0, 0, w, h))

    def clear_overlays(self) -> None:
        """Remove every mark (not the image)."""
        self.set_pending_pattern([])
        self.set_armed_path([])
        self.set_pattern_corners([])
        self.set_selected_point(None)
        self.set_spot(None)
        self.set_trail([])
        self.set_manual_markers([])
        self.set_calibration_clicks([])
        self.set_scan_bounds(None)
        self.set_unreachable(None)

    # ---------------- pointer ----------------
    def _on_click(self, ev) -> None:
        if ev.button() != QtCore.Qt.LeftButton or ev.double():
            return
        pos = ev.scenePos()
        if not self._vb.sceneBoundingRect().contains(pos):
            return
        p = self._vb.mapSceneToView(pos)
        self.clicked.emit(float(p.x()), float(p.y()), int(ev.modifiers()))

    def _on_move(self, pos) -> None:
        inside = self._vb.sceneBoundingRect().contains(pos)
        self._hover_v.setVisible(inside)
        self._hover_h.setVisible(inside)
        if inside:
            p = self._vb.mapSceneToView(pos)
            self._hover_v.setPos(p.x())
            self._hover_h.setPos(p.y())
            self.hovered.emit(float(p.x()), float(p.y()))

    def leaveEvent(self, event):
        self._hover_v.setVisible(False)
        self._hover_h.setVisible(False)
        super().leaveEvent(event)
