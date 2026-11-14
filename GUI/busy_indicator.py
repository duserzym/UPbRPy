"""A delayed, fixed-slot status indicator with reduced-motion support."""

from __future__ import annotations

from functools import lru_cache
import os
import subprocess
import sys

from PySide6 import QtCore, QtGui, QtWidgets

from GUI.theme import active_theme


@lru_cache(maxsize=1)
def _system_reduced_motion() -> bool:
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "com.apple.universalaccess", "reduceMotion"],
                capture_output=True, text=True, timeout=1, check=False,
            )
            return result.returncode == 0 and result.stdout.strip().lower() in {"1", "true"}
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False


def reduced_motion() -> bool:
    """Read the environment override or the cached macOS motion preference."""
    override = os.environ.get("UPBRPY_REDUCE_MOTION")
    return override == "1" if override in {"0", "1"} else _system_reduced_motion()


class BusyIndicator(QtWidgets.QWidget):
    """Reserve a status-bar slot, painting task text only after 150 ms."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(260, 22)
        self._opacity = 0.0
        self._text = ""
        self._label = ""
        self._running = False
        self._reduced_motion = reduced_motion()
        self._clock = QtCore.QElapsedTimer()
        self.delay_timer = QtCore.QTimer(self)
        self.delay_timer.setSingleShot(True)
        self.delay_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self.delay_timer.setInterval(150)
        self.delay_timer.timeout.connect(self._reveal)
        self.motion_timer = QtCore.QTimer(self)
        self.motion_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self.motion_timer.setInterval(16)
        self.motion_timer.timeout.connect(self.update)
        self.fade = QtCore.QPropertyAnimation(self, b"opacity", self)
        self.fade.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self.fade.finished.connect(self._fade_finished)

    def _get_opacity(self):
        return self._opacity

    def _set_opacity(self, value):
        self._opacity = float(value)
        self.update()

    opacity = QtCore.Property(float, _get_opacity, _set_opacity)

    def start(self, label: str) -> None:
        self.fade.stop()
        self.motion_timer.stop()
        self.opacity = 0.0
        self._running = True
        self._label = label
        self.set_progress("")
        self.delay_timer.start()

    @QtCore.Slot(str)
    def set_progress(self, stage: str) -> None:
        if self._running:
            self._text = f"{self._label} · {stage}" if stage else self._label
            self.setToolTip(self._text)
            self.setAccessibleName(self._text)
            self.update()

    def stop(self) -> None:
        self._running = False
        self.delay_timer.stop()
        self.fade.stop()
        if self._reduced_motion:
            self.opacity = 0.0
            self.motion_timer.stop()
        elif self.opacity > 0:
            self._fade_to(0.0, 150)

    def _reveal(self):
        if not self._running:
            return
        if self._reduced_motion:
            self.opacity = 1.0
        else:
            self._clock.start()
            self.motion_timer.start()
            self._fade_to(1.0, 180)

    def _fade_to(self, opacity, duration):
        self.fade.stop()
        self.fade.setDuration(duration)
        self.fade.setStartValue(self.opacity)
        self.fade.setEndValue(opacity)
        self.fade.start()

    def _fade_finished(self):
        if not self._running:
            self.motion_timer.stop()

    def paintEvent(self, event):
        if self.opacity <= 0:
            return
        painter = QtGui.QPainter(self)
        painter.setOpacity(self.opacity)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        accent = QtGui.QColor(active_theme().accent)
        if self._reduced_motion:
            painter.setBrush(accent)
            painter.drawEllipse(QtCore.QPointF(8, self.height() / 2), 3, 3)
        else:
            painter.setPen(QtGui.QPen(accent, 2, QtCore.Qt.PenStyle.SolidLine,
                                     QtCore.Qt.PenCapStyle.RoundCap))
            angle = (self._clock.elapsed() % 1400) / 1400 * 360
            painter.drawArc(QtCore.QRectF(2, (self.height() - 12) / 2, 12, 12),
                            int(-angle * 16), 100 * 16)
        painter.setPen(QtGui.QColor(active_theme().muted))
        font = self.font()
        font.setPixelSize(11)
        painter.setFont(font)
        rectangle = self.rect().adjusted(23, 0, -4, 0)
        text = painter.fontMetrics().elidedText(
            self._text, QtCore.Qt.TextElideMode.ElideRight, rectangle.width())
        painter.drawText(rectangle, QtCore.Qt.AlignmentFlag.AlignVCenter, text)
