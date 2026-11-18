"""Collect Python cycles on the application thread, including Qt wrappers."""

from __future__ import annotations

import gc
import sys

from PySide6 import QtCore


# Python 3.14.5 restored the three-generation collector. In 3.14.0–4,
# collect(1) instead scans the young generation and an increment of the old.
_INCREMENTAL = (3, 14, 0) <= sys.version_info[:3] < (3, 14, 5)
_ATTRIBUTE = "_upbrpy_gc_policy"


class _Policy(QtCore.QObject):
    def __init__(self, app):
        super().__init__(app)
        self.was_enabled = gc.isenabled()
        gc.disable()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        # Keep automatic GC off through application teardown: workers may
        # still own Qt garbage. Only explicit uninstall restores the state.

    def restore(self):
        if self.was_enabled:
            gc.enable()
        else:
            gc.disable()

    @QtCore.Slot()
    def tick(self):
        counts = gc.get_count()
        thresholds = gc.get_threshold()
        if not thresholds[0] or counts[0] <= thresholds[0]:
            return
        if _INCREMENTAL:
            gc.collect(1)
        else:
            # One collection of the oldest due generation includes younger
            # generations, avoiding repeated scans during the same tick.
            generation = 0
            if counts[2] > thresholds[2]:
                generation = 2
            elif counts[1] > thresholds[1]:
                generation = 1
            gc.collect(generation)


def install(app) -> None:
    """Disable automatic GC and install one 500 ms GUI-thread timer per app."""
    if app is None or QtCore.QThread.currentThread() != app.thread():
        raise RuntimeError("Install GC policy on the QApplication thread")
    if getattr(app, _ATTRIBUTE, None) is None:
        setattr(app, _ATTRIBUTE, _Policy(app))


def uninstall(app) -> None:
    """Stop the timer and restore the enabled state recorded by install."""
    if QtCore.QThread.currentThread() != app.thread():
        raise RuntimeError("Uninstall GC policy on the QApplication thread")
    policy = getattr(app, _ATTRIBUTE, None)
    if policy is not None:
        policy.timer.stop()
        delattr(app, _ATTRIBUTE)
        policy.restore()
        policy.deleteLater()
