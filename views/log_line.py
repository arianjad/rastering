"""Log line: the newest message in one line at the bottom, expandable to the history."""
from __future__ import annotations

import collections
import html
import time

from PyQt5 import QtCore, QtWidgets

import theme
from .widgets import label, set_prop


class LogLine(QtWidgets.QWidget):
    """add(text, error=False) logs a message with the time. Errors and refusals
    show in the fault colour. The history keeps the newest `max_lines`."""

    expanded_changed = QtCore.pyqtSignal(bool)

    def __init__(self, parent=None, max_lines: int = 2000, history_height: int = 180):
        super().__init__(parent)
        self.setObjectName("logLine")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self._lines = collections.deque(maxlen=int(max_lines))   # (stamp, text, error)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.history = QtWidgets.QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setProperty("role", "log")
        self.history.setMaximumBlockCount(int(max_lines))
        self.history.setFixedHeight(history_height)
        self.history.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.history.hide()
        lay.addWidget(self.history)

        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(12, 4, 8, 4)
        row.setSpacing(10)
        self.stamp = label("", role="log", tone="label")
        row.addWidget(self.stamp)
        self.line = label("", role="log")
        self.line.setMinimumWidth(10)
        self.line.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        row.addWidget(self.line, 1)
        self.toggle = QtWidgets.QPushButton("Show log")
        self.toggle.setCheckable(True)
        self.toggle.setToolTip("Show or hide the earlier messages")
        self.toggle.toggled.connect(self.set_expanded)
        row.addWidget(self.toggle)
        lay.addLayout(row)
        theme.changed().connect(self._rebuild)

    def add(self, text: str, *, error: bool = False) -> None:
        stamp = time.strftime("%H:%M:%S")
        entry = (stamp, str(text), bool(error))
        self._lines.append(entry)
        self._show_newest()
        if self.is_expanded():
            self.history.appendHtml(self._html(entry))

    def lines(self) -> list:
        """The history as plain text lines, oldest first."""
        return [f"{s}  {t}" for s, t, _ in self._lines]

    def is_expanded(self) -> bool:
        return not self.history.isHidden()

    def set_expanded(self, on: bool) -> None:
        on = bool(on)
        if on == self.is_expanded():
            return
        if on:
            self._rebuild()
        self.history.setVisible(on)
        self.toggle.blockSignals(True)
        self.toggle.setChecked(on)
        self.toggle.blockSignals(False)
        self.toggle.setText("Hide log" if on else "Show log")
        self.expanded_changed.emit(on)

    def _html(self, entry) -> str:
        stamp, text, error = entry
        t = theme.tokens()
        colour = t["fault"] if error else t["ink"]
        return (f'<span style="color:{t["label"]}">{stamp}</span>&nbsp;&nbsp;'
                f'<span style="color:{colour}">{html.escape(text)}</span>')

    def _rebuild(self) -> None:
        self.history.clear()
        for e in self._lines:                 # one block per line, so the block limit counts lines
            self.history.appendHtml(self._html(e))
        self.history.verticalScrollBar().setValue(self.history.verticalScrollBar().maximum())
        self._show_newest()

    def _show_newest(self) -> None:
        if not self._lines:
            self.stamp.setText("")
            self.line.setText("")
            return
        stamp, text, error = self._lines[-1]
        self.stamp.setText(stamp)
        fm = self.line.fontMetrics()
        self.line.setText(fm.elidedText(text, QtCore.Qt.ElideRight, max(20, self.line.width())))
        self.line.setToolTip(text if fm.horizontalAdvance(text) > self.line.width() else "")
        set_prop(self.line, "tone", "fault" if error else "")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._show_newest()
