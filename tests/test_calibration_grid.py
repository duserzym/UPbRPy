"""The concentration-calibration view overlays every calibrated element by default."""

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI import main_window
from GUI.main_window import MainWindow
from GUI.theme import active_theme, build_application
from GUI.widgets import CALIBRATION_GRID, CalibrationSlopesWidget


RICH = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"
CHANNELS = ["31P", "49Ti", "89Y", "91Zr", "139La", "232Th", "238U"]


def fake_data(channels=CHANNELS):
    return {
        "channels": list(channels),
        "channel_data": {
            channel: {
                "cps": np.array([1.0e3, 4.0e5]) * (index + 1),
                "ppm": np.array([40.0, 450.0]),
                "slope": 450.0 / (4.0e5 * (index + 1)),
                "labels": ["NIST614", "NIST610"],
                "spot_names": ["NIST614 1", "NIST610 1"],
            }
            for index, channel in enumerate(channels)
        },
    }


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


def test_widget_draws_one_small_plot_per_element(app):
    widget = CalibrationSlopesWidget()
    widget.resize(900, 600)
    data = fake_data()
    widget.show_data(data, active_theme(app), channel=CALIBRATION_GRID)
    assert widget.grid_channels() == CHANNELS
    first = widget._grid_items["31P"]
    widget.show_data(data, active_theme(app), channel=CALIBRATION_GRID)
    assert widget._grid_items["31P"] is first  # unchanged data is not redrawn
    widget.set_channel("238U")
    assert widget.grid_channels() == []
    assert widget.channel() == "238U"
    widget.set_channel(None)  # every element on one plot, as before
    assert widget.grid_channels() == [] and len(widget._scatter_for_line) == len(CHANNELS)
    widget.set_interaction_mode("pan")
    widget.set_channel(CALIBRATION_GRID)
    assert all(item.vb.state["mouseMode"] == item.vb.PanMode for item in widget._grid_items.values())


def test_studio_opens_the_calibration_step_on_one_overlay_plot(app, monkeypatch):
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    data = fake_data()
    monkeypatch.setattr(main_window, "get_calibration_slope_data", lambda *args: data)
    window = MainWindow(theme_mode="light")
    try:
        window.resize(1280, 800)
        window.show()
        window.load_experiment_directory(str(RICH), background=False)
        window.set_plot_mode("calibration_slopes", refresh=True)
        app.processEvents()
        combo = window.calibration_channel_combo
        # Yiming asked (2026-11-12) for one overlay plot by default.
        assert combo.currentIndex() == 0
        assert combo.currentText() == "All elements"
        assert combo.currentData() is None
        assert window.calibration_slopes_plot.grid_channels() == []
        combo.setCurrentIndex(combo.findData(CALIBRATION_GRID))
        assert combo.currentText() == "Separate plots"
        assert window.calibration_slopes_plot.grid_channels() == CHANNELS
        # Double-clicking a small plot focuses that element; the choice stays.
        window.calibration_slopes_plot.channelRequested.emit("89Y")
        assert combo.currentData() == "89Y"
        assert window.calibration_slopes_plot.grid_channels() == []
        window.refresh_view()
        assert combo.currentData() == "89Y"
        combo.setCurrentIndex(combo.findData(CALIBRATION_GRID))
        assert window.calibration_slopes_plot.grid_channels() == CHANNELS
    finally:
        window.has_unsaved_changes = lambda: False
        window.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
