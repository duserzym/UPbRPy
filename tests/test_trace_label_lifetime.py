"""Removed trace labels must stop receiving their former scene's paint signal."""

import os

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI.theme import build_application
from GUI.widgets import TracePlotWidget


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def paint_receivers(plot):
    # TextItem connects its updateTransform slot only when first painted.
    plot.grab()
    return plot.scene().receivers("2sigPrepareForPaint()")


def show_boundaries(plot):
    frame = pd.DataFrame({"Time": np.arange(100.), "238U": np.arange(100.)})
    plot.show_spot(
        frame, ["238U"], {"238U": "#2468d4"},
        {"baseline": (0, 20), "plateau": (40, 70), "signal": (30, 80)},
        editable_regions=True,
    )
    return frame


def test_trace_redraw_disconnects_removed_labels_before_collection(app):
    plot = TracePlotWidget()
    plot.resize(800, 500)
    baseline = paint_receivers(plot)
    retired = []
    try:
        for _ in range(4):
            frame = show_boundaries(plot)
            assert paint_receivers(plot) == baseline + 2
            # Keep wrappers alive: cleanup must happen on removal, without
            # relying on a later cyclic collection during signal dispatch.
            retired.extend(line.label for line in plot._signal_boundaries.values())
            plot.show_series(frame, ["238U"], {"238U": "#2468d4"}, y_label="cps")
            assert paint_receivers(plot) == baseline
        plot.scene().sigPrepareForPaint.emit()
    finally:
        plot.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_trace_label_reconnects_when_moved_to_another_scene(app):
    first, second = TracePlotWidget(), TracePlotWidget()
    for plot in (first, second):
        plot.resize(800, 500)
    try:
        baseline = paint_receivers(first)
        second.plot_item.setRange(xRange=(0, 100), yRange=(0, 100))
        other_baseline = paint_receivers(second)
        show_boundaries(first)
        assert paint_receivers(first) == baseline + 2
        line = first._signal_boundaries["signal_start"]
        for _ in range(3):
            first.plot_item.removeItem(line)
            assert paint_receivers(first) == baseline + 1
            second.plot_item.addItem(line)
            assert paint_receivers(second) == other_baseline + 1
            second.plot_item.removeItem(line)
            assert paint_receivers(second) == other_baseline
            first.plot_item.addItem(line)
            assert paint_receivers(first) == baseline + 2
    finally:
        first.deleteLater()
        second.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
