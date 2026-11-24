"""Look of the rastering GUI: colour tokens, fonts by role, one stylesheet.

Call order at startup (see main_rastering.py):
    theme.setup_high_dpi()             # before QApplication exists
    app = QtWidgets.QApplication(...)
    theme.apply_theme(app, light=theme.load_light_pref())

Widgets pick a font role with the dynamic property "role" (QSS selects on it):
    label.setProperty("role", "heading")
Custom-painted parts and pyqtgraph read tokens() and font(role) directly and
re-read them when `changed` fires (a live dark/light switch).
"""
from __future__ import annotations

# Colour tokens. Same roles in both palettes; light uses darker hues so text
# stays readable on light panels.
#   ground  window ground          panel  raised panels       well  inputs, camera surround
#   ink     text                   label  secondary text      dim   disabled text
#   rule    1 px separators        hover  pointer-over fill
#   spot    where the stage is now (image crosshair AND position readout)
#   path    the pattern (dots on the image AND the Pattern step)
#   auto    the stage moves without you (laser-sign yellow); on_auto = text on it
#   fault   refusals and errors
DARK = {
    "ground": "#16181B",
    "panel": "#212429",
    "well": "#0D0E10",
    "ink": "#ECE8E1",
    "label": "#9C978F",
    "dim": "#5F5C57",
    "rule": "#33373D",
    "hover": "#2A2E34",
    "spot": "#FF4FA3",
    "path": "#4CC3FF",
    "auto": "#F5C400",
    "auto_edge": "#F5C400",
    "on_auto": "#0D0E10",
    "fault": "#FF5A4E",
}

LIGHT = {
    "ground": "#E9E7E3",
    "panel": "#F6F5F2",
    "well": "#FFFFFF",
    "ink": "#1B1C1F",
    "label": "#5B5750",
    "dim": "#A29E97",
    "rule": "#C8C4BD",
    "hover": "#ECEAE5",
    "spot": "#C0186E",
    "path": "#0B6E9E",
    "auto": "#F5C400",
    "auto_edge": "#8A6D00",   # yellow block's edge and the held outline, visible on a light ground
    "on_auto": "#0D0E10",
    "fault": "#C42B21",
}

# Overlays sit on the camera image, which does not change with the theme, so
# they always use the bright (dark-theme) meaning colours.
OVERLAY = {k: DARK[k] for k in ("spot", "path", "fault", "ink", "well", "auto")}

# Font roles: (face, size in pt, weight). Sizes are explicit pt, never derived
# from the system font. Faces: display = Bahnschrift (headings, step names, the
# state word), sans = Segoe UI (everything else; its digits are equal width),
# mono = Cascadia Mono (the log only).
ROLES = {
    "body": ("sans", 10.0, 400),
    "small": ("sans", 9.0, 400),
    "caption": ("sans", 9.0, 400),
    "value": ("sans", 11.0, 400),
    "position": ("sans", 17.0, 600),
    "heading": ("display", 11.5, 400),
    "step": ("display", 12.5, 400),
    "state": ("display_bold", 15.0, 600),
    "log": ("mono", 9.5, 400),
}

_FACE_CHOICES = {
    "sans": ("Segoe UI",),
    "display": ("Bahnschrift", "Segoe UI"),
    "display_bold": ("Bahnschrift SemiBold", "Bahnschrift", "Segoe UI"),
    "mono": ("Cascadia Mono", "Cascadia Code", "Consolas", "Courier New"),
}

_faces: dict | None = None     # face key -> installed family (resolved once)
_APPLIED_LIGHT = False
_notifier = None

# Theme is an operator preference, deliberately machine-wide (one registry key
# shared by all copies of this GUI). Sole reader/writer of this key.
_SETTINGS_KEY = ("RaX", "rastering-gui")


def setup_high_dpi() -> None:
    """Scale the GUI on the 150 % 4K monitor (device pixel ratio 1.5, logical
    2560 x 1440). Must run before the QApplication exists; Qt ignores it after."""
    from PyQt5 import QtCore, QtGui
    if QtCore.QCoreApplication.instance() is not None:
        raise RuntimeError("theme.setup_high_dpi() must be called before the QApplication is created")
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def is_light() -> bool:
    """The theme actually applied (not the stored preference)."""
    return _APPLIED_LIGHT


def tokens() -> dict:
    """Colour tokens of the theme now applied."""
    return LIGHT if _APPLIED_LIGHT else DARK


def load_light_pref() -> bool:
    from PyQt5.QtCore import QSettings
    return QSettings(*_SETTINGS_KEY).value("light_theme", False, type=bool)


def save_light_pref(on: bool) -> None:
    from PyQt5.QtCore import QSettings
    QSettings(*_SETTINGS_KEY).setValue("light_theme", bool(on))


def changed():
    """Signal (no arguments) emitted after apply_theme; connect restyle slots to it."""
    global _notifier
    if _notifier is None:
        from PyQt5 import QtCore

        class _Notifier(QtCore.QObject):
            changed = QtCore.pyqtSignal()

        _notifier = _Notifier()
    return _notifier.changed


def _resolve_faces() -> dict:
    global _faces
    if _faces is None:
        from PyQt5.QtGui import QFontDatabase
        families = set(QFontDatabase().families())
        _faces = {key: next((f for f in choices if f in families), choices[-1])
                  for key, choices in _FACE_CHOICES.items()}
    return _faces


def font(role: str = "body"):
    """QFont for a role (see ROLES). Needs a QApplication."""
    from PyQt5 import QtGui
    face, pt, weight = ROLES[role]
    f = QtGui.QFont(_resolve_faces()[face])
    f.setPointSizeF(pt)
    f.setWeight(QtGui.QFont.DemiBold if weight >= 600 else QtGui.QFont.Normal)
    if face == "sans":
        f.setStyleStrategy(QtGui.QFont.PreferAntialias)
    return f


def build_qss(t: dict, faces: dict) -> str:
    """The one stylesheet, built from colour tokens and resolved font faces."""
    role_rules = "\n".join(
        f'*[role="{name}"] {{ font-family: "{faces[face]}"; font-size: {pt}pt; '
        f'font-weight: {weight}; }}'
        for name, (face, pt, weight) in ROLES.items())
    return f"""
QWidget {{ color: {t['ink']}; }}
QMainWindow, QDialog {{ background: {t['ground']}; }}
*[surface="ground"] {{ background: {t['ground']}; }}
*[surface="panel"] {{ background: {t['panel']}; }}
*[surface="well"] {{ background: {t['well']}; }}
QToolTip {{ background: {t['panel']}; color: {t['ink']}; border: 1px solid {t['rule']}; padding: 4px 6px; }}

{role_rules}
*[role="caption"], *[role="small"] {{ color: {t['label']}; }}
QLabel[tone="label"] {{ color: {t['label']}; }}
QLabel[tone="fault"] {{ color: {t['fault']}; }}
QLabel[tone="spot"] {{ color: {t['spot']}; }}
QLabel[tone="path"] {{ color: {t['path']}; }}

QGroupBox {{
    border: none; border-top: 1px solid {t['rule']};
    margin-top: 26px; padding-top: 6px;
    font-family: "{faces['display']}"; font-size: {ROLES['heading'][1]}pt;
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 0px; top: 2px; padding: 0px; color: {t['ink']};
}}

QPushButton, QToolButton {{
    background: {t['panel']}; color: {t['ink']};
    border: 1px solid {t['rule']}; border-radius: 3px;
    padding: 5px 12px; min-height: 18px;
}}
QPushButton:hover, QToolButton:hover {{ background: {t['hover']}; border-color: {t['label']}; }}
QPushButton:pressed, QToolButton:pressed, QPushButton:checked, QToolButton:checked {{
    background: {t['well']}; border-color: {t['ink']};
}}
QPushButton:focus, QToolButton:focus {{ border: 2px solid {t['ink']}; padding: 4px 11px; }}
QPushButton:disabled, QToolButton:disabled {{
    color: {t['dim']}; background: {t['ground']}; border-color: {t['rule']};
}}
QPushButton[kind="primary"] {{ background: {t['ink']}; color: {t['ground']}; border-color: {t['ink']}; font-weight: 600; }}
QPushButton[kind="primary"]:hover {{ background: {t['label']}; border-color: {t['label']}; }}
QPushButton[kind="primary"]:focus {{ border: 2px solid {t['path']}; }}
QPushButton[kind="edited"] {{ color: {t['path']}; border-color: {t['path']}; }}
QPushButton[kind="primary"]:disabled {{ background: {t['ground']}; color: {t['dim']}; border-color: {t['rule']}; }}

QAbstractSpinBox, QLineEdit, QComboBox {{
    background: {t['well']}; color: {t['ink']};
    border: 1px solid {t['rule']}; border-radius: 3px;
    padding: 4px 6px; min-height: 18px;
    selection-background-color: {t['path']}; selection-color: {t['well']};
}}
QAbstractSpinBox:hover, QLineEdit:hover, QComboBox:hover {{ border-color: {t['label']}; }}
QAbstractSpinBox:focus, QLineEdit:focus, QComboBox:focus {{ border: 2px solid {t['ink']}; padding: 3px 5px; }}
QAbstractSpinBox:disabled, QLineEdit:disabled, QComboBox:disabled {{ color: {t['dim']}; background: {t['ground']}; }}
QComboBox QAbstractItemView {{
    background: {t['panel']}; border: 1px solid {t['rule']};
    selection-background-color: {t['hover']}; selection-color: {t['ink']}; outline: none;
}}

QCheckBox {{ spacing: 8px; }}
QCheckBox:disabled {{ color: {t['dim']}; }}
*[role="rule"] {{ background: {t['rule']}; border: none; }}

QSlider {{ min-height: 22px; }}
QSlider::groove:horizontal {{ height: 4px; background: {t['well']}; border: 1px solid {t['rule']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {t['label']}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {t['label']}; width: 14px; height: 14px; margin: -6px 0px; border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: {t['ink']}; }}
QSlider::handle:horizontal:focus {{ background: {t['ink']}; border: 2px solid {t['path']}; width: 12px; height: 12px; }}
QSlider::handle:horizontal:disabled {{ background: {t['dim']}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0px; }}
QScrollBar::handle:vertical {{ background: {t['rule']}; min-height: 28px; border-radius: 4px; margin: 1px; }}
QScrollBar::handle:vertical:hover {{ background: {t['label']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0px; }}
QScrollBar::handle:horizontal {{ background: {t['rule']}; min-width: 28px; border-radius: 4px; margin: 1px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QPlainTextEdit, QTextEdit {{ background: {t['well']}; border: 1px solid {t['rule']}; border-radius: 3px; }}
QMenuBar {{ background: {t['ground']}; }}
QMenuBar::item:selected {{ background: {t['panel']}; }}
QMenu {{ background: {t['panel']}; border: 1px solid {t['rule']}; }}
QMenu::item {{ padding: 5px 18px; }}
QMenu::item:selected {{ background: {t['hover']}; }}
QSplitter::handle {{ background: {t['rule']}; }}

/* Control bar */
#controlBar {{ background: {t['panel']}; border-bottom: 1px solid {t['rule']}; }}
#stateBlock {{ border-radius: 3px; padding: 0px 14px; }}
#stateBlock[look="off"] {{ background: transparent; color: {t['label']}; border: 1px solid {t['rule']}; }}
#stateBlock[look="auto"] {{ background: {t['auto']}; color: {t['on_auto']}; border: 1px solid {t['auto_edge']}; }}
#stateBlock[look="held"] {{ background: transparent; color: {t['ink']}; border: 3px double {t['auto_edge']}; }}
#editedNotice {{ color: {t['path']}; border: 1px solid {t['path']}; border-radius: 3px; padding: 2px 10px; }}

/* Log line */
#logLine {{ background: {t['panel']}; border-top: 1px solid {t['rule']}; }}
#logLine QPlainTextEdit {{ border: none; border-bottom: 1px solid {t['rule']}; border-radius: 0px; }}
"""


def _lab_style():
    """Fusion, with check boxes drawn from the tokens: a clear box, a tick, and a
    ring when focused (Fusion's own are near invisible on the dark panels)."""
    from PyQt5 import QtCore, QtGui, QtWidgets
    S = QtWidgets.QStyle

    class LabStyle(QtWidgets.QProxyStyle):
        def pixelMetric(self, metric, option=None, widget=None):
            if metric in (S.PM_IndicatorWidth, S.PM_IndicatorHeight):
                return 16
            return super().pixelMetric(metric, option, widget)

        def drawPrimitive(self, element, option, painter, widget=None):
            if element != S.PE_IndicatorCheckBox:
                return super().drawPrimitive(element, option, painter, widget)
            t = tokens()
            st = option.state
            enabled = bool(st & S.State_Enabled)
            focus = bool(st & S.State_HasFocus) and enabled
            hot = bool(st & S.State_MouseOver) and enabled
            r = QtCore.QRectF(option.rect).adjusted(1.5, 1.5, -1.5, -1.5)
            painter.save()
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            edge = t["ink"] if (focus or hot) else (t["label"] if enabled else t["dim"])
            painter.setPen(QtGui.QPen(QtGui.QColor(edge), 2.0 if focus else 1.2))
            painter.setBrush(QtGui.QColor(t["well"] if enabled else t["ground"]))
            painter.drawRoundedRect(r, 2.5, 2.5)
            mark = QtGui.QColor(t["ink"] if enabled else t["dim"])
            pen = QtGui.QPen(mark, 2.0)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
            painter.setPen(pen)
            x, y, w, h = r.x(), r.y(), r.width(), r.height()
            if st & S.State_On:
                path = QtGui.QPainterPath()
                path.moveTo(x + w * 0.22, y + h * 0.52)
                path.lineTo(x + w * 0.42, y + h * 0.72)
                path.lineTo(x + w * 0.78, y + h * 0.30)
                painter.drawPath(path)
            elif st & S.State_NoChange:
                painter.drawLine(QtCore.QPointF(x + w * 0.25, y + h / 2), QtCore.QPointF(x + w * 0.75, y + h / 2))
            painter.restore()

    return LabStyle(QtWidgets.QStyleFactory.create("Fusion"))


def apply_theme(app, light: bool = False) -> None:
    """Apply the dark (default) or light theme. Safe to re-call at runtime."""
    from PyQt5 import QtGui

    global _APPLIED_LIGHT
    first = _faces is None
    if first:
        app.setStyle(_lab_style())    # one style on every PC; the QSS assumes it
    faces = _resolve_faces()
    _APPLIED_LIGHT = bool(light)
    t = tokens()

    c = QtGui.QColor
    pal = QtGui.QPalette()
    for role, key in ((QtGui.QPalette.Window, "ground"), (QtGui.QPalette.WindowText, "ink"),
                      (QtGui.QPalette.Base, "well"), (QtGui.QPalette.AlternateBase, "panel"),
                      (QtGui.QPalette.Text, "ink"), (QtGui.QPalette.Button, "panel"),
                      (QtGui.QPalette.ButtonText, "ink"), (QtGui.QPalette.ToolTipBase, "panel"),
                      (QtGui.QPalette.ToolTipText, "ink"), (QtGui.QPalette.Highlight, "path"),
                      (QtGui.QPalette.HighlightedText, "well"), (QtGui.QPalette.Light, "rule"),
                      (QtGui.QPalette.Mid, "rule"), (QtGui.QPalette.Dark, "well"),
                      (QtGui.QPalette.PlaceholderText, "label")):
        pal.setColor(role, c(t[key]))
    for role in (QtGui.QPalette.Text, QtGui.QPalette.ButtonText, QtGui.QPalette.WindowText):
        pal.setColor(QtGui.QPalette.Disabled, role, c(t["dim"]))
    app.setPalette(pal)
    app.setFont(font("body"))
    app.setStyleSheet(build_qss(t, faces))
    changed().emit()
