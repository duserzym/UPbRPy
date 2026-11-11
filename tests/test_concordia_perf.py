"""Generous timing and scene-size guards for the batched concordia canvas."""
import time

import numpy as np
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtWidgets

from GUI.theme import THEMES, build_application
from GUI.widgets import ConcordiaReviewWidget
from test_concordia_render_parity import dispose, synthetic_frame


@pytest.mark.parametrize("diagram", ["wetherill", "tera_wasserburg"])
def test_batched_concordia_smoke(diagram):
    app = QtWidgets.QApplication.instance() or build_application([], "light")
    canvas = ConcordiaReviewWidget()
    canvas.resize(1000, 650)
    canvas.show()
    source = synthetic_frame(3000)
    kwargs = dict(diagram=diagram, kind="depth", theme=THEMES["light"],
                  include_calibration_uncertainty=True, source_frame=source)
    try:
        start = time.perf_counter()
        canvas.show_data(source, **kwargs)
        app.processEvents()
        canvas.grab()
        assert time.perf_counter() - start < 3.
        count = len(canvas._plot_item.items)
        assert len(canvas._ellipses) > 2900
        assert len(canvas._ellipse_batch.paths) <= 64
        assert count < 40
        indices = np.flatnonzero(source.spot_name.ne("spot_25").to_numpy())
        start = time.perf_counter()
        canvas.show_data(source.iloc[indices], row_positions=indices, reset_view=False, **kwargs)
        app.processEvents()
        canvas.grab()
        assert time.perf_counter() - start < .5
        # Labels have their own bounded item pool; ellipse count never affects it.
        canvas._label_timer.stop()
        for item in canvas._age_items:
            canvas._plot_item.removeItem(item)
        canvas._age_items = []
        large_count = len(canvas._plot_item.items)
        canvas.show_data(source.iloc[:20], row_positions=np.arange(20), reset_view=False, **kwargs)
        assert len(canvas._plot_item.items) == large_count
        assert len(canvas._ellipses) < 20
    finally:
        dispose(canvas, app)
