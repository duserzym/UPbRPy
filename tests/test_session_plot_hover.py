"""Session diagnostic identity, hover content, and indexed hit testing."""

import time

import numpy as np
import pandas as pd
import pytest

from GUI import services
from GUI.state import ReductionSession
from GUI.workflows import get_workflow_preset


def plot_session(monkeypatch, group_names=("RM_b", "RM_a"), group_times=(30., 10.),
                 observation_times=(10., 30.)):
    monkeypatch.setattr(services, "current_standard_archive", lambda: pd.DataFrame(
        {"206Pb/238U*": [0.18]}, index=["RM"]))
    frame = pd.DataFrame({
        "spot_name": np.repeat(group_names, 2),
        "sample": "RM",
        "Time": np.repeat(group_times, 2) + np.tile([-1., 1.], len(group_times)),
    })
    n = len(observation_times)
    observations = {"time": np.array(observation_times), "fractionation": np.ones(n),
                    "std": np.full(n, .002), "weights": np.arange(1., n + 1)}
    result = {"fit_observations": observations, "fit_residuals": np.arange(n) * .001,
              "predictor": lambda x: np.ones_like(x), "fit_prediction_x": np.array([0., 40.]),
              "model": "linear", "std": .003}
    session = ReductionSession(primary_68_standard="RM")
    # The experiment is name-sorted, as on import. Start times give acquisition order.
    session.experiment = pd.DataFrame({"spot_names": ["RM_a", "RM_b", "unknown"],
                                       "spot_starting_times": [8., 28., 0.]})
    session.experiment.attrs["calculation_excluded_spots"] = ["RM_excluded"]
    session.reduction_cache = {"downhole_results": {"plateau_corrected": frame},
                               "session_correction_results": {"correction_results": {"206Pb/238U": result}}}
    return session, observations, result


@pytest.mark.parametrize(("names", "times", "observed", "expected"), [
    (("RM_b", "RM_a"), (30., 10.), (10. + 1e-7, 30.), ["RM_a", "RM_b"]),
    (("extra", "RM_b", "RM_a"), (20., 30., 10.), (10., 30.), ["RM_a", "RM_b"]),
    (("RM_b",), (30.,), (10., 30.), ["standard 1", "RM_b"]),
    (("RM_a", "RM_b"), (110., 130.), (10., 30.), ["standard 1", "standard 2"]),
    (("RM_a", "RM_b"), (10., 10.), (10., 30.), ["standard 1", "standard 2"]),
])
def test_service_matches_times_and_preserves_cache(monkeypatch, names, times, observed, expected):
    session, observations, result = plot_session(monkeypatch, names, times, observed)
    cache = session.reduction_cache
    before = dict(cache)
    frame = cache["downhole_results"]["plateau_corrected"]
    unchanged = frame.copy(deep=True)
    data = services.get_session_plot_data(session, get_workflow_preset("zircon"))
    assert data["spot_names"] == expected
    for key in ("spot_samples", "spot_order", "spot_weights", "spot_residuals", "spot_start_times"):
        assert len(data[key]) == len(observed)
    assert data["spot_mean_times"] is observations["time"]
    assert data["spot_mean_fracs"] is observations["fractionation"]
    assert data["spot_frac_errs"] is observations["std"]
    assert data["spot_weights"] is observations["weights"]
    assert data["spot_residuals"] is result["fit_residuals"]
    assert session.reduction_cache is cache
    assert cache.keys() == before.keys()
    assert all(cache[key] is value for key, value in before.items())
    pd.testing.assert_frame_equal(frame, unchanged)
    assert services.get_session_plot_data(session, get_workflow_preset("zircon")) is data
    if expected == ["RM_a", "RM_b"]:
        np.testing.assert_array_equal(data["spot_order"], [2, 3])
        np.testing.assert_array_equal(data["spot_start_times"], [8., 28.])
        assert data["total_spots"] == 3
        assert data["spot_samples"] == ["RM", "RM"]


def test_service_missing_metadata_and_changed_results(monkeypatch):
    session, observations, result = plot_session(monkeypatch)
    observations["weights"] = np.array([1.])
    result["fit_residuals"] = np.ones((2, 1))
    data = services.get_session_plot_data(session, get_workflow_preset("zircon"))
    assert np.isnan(data["spot_weights"]).all()
    assert np.isnan(data["spot_residuals"]).all()
    session.reduction_cache["downhole_results"] = {"plateau_corrected": pd.DataFrame({
        "sample": ["RM"], "spot_name": ["new"], "Time": [10.]})}
    updated = services.get_session_plot_data(session, get_workflow_preset("zircon"))
    assert updated is not data
    assert updated["spot_names"] == ["new", "standard 2"]


def hover_data():
    return {"ratio_name": "206Pb/238U", "known_ratio": .18, "primary_standard": "RM",
            "spot_names": ["RM_37", "RM_80"], "spot_samples": ["Reference material", "RM"],
            "spot_mean_times": np.array([1234.5, 2500.]),
            "spot_mean_fracs": np.array([1.0234, 1.025]), "spot_frac_errs": np.array([.002, .003]),
            "spot_order": [37, 80], "total_spots": 212, "spot_weights": [8.3, 10.],
            "spot_residuals": [-.0012, .0005], "fit_times": np.array([0., 3000.]),
            "fit_vals": np.array([1.01, 1.01]), "fit_std": .00175,
            "fit_label": "linear", "show_uncertainty": True}


@pytest.fixture
def canvas(monkeypatch):
    from PySide6 import QtWidgets
    from GUI.theme import build_application
    from GUI.widgets import SessionFractionationWidget

    app = QtWidgets.QApplication.instance() or build_application([], "light")
    widget = SessionFractionationWidget()
    widget.resize(900, 550)
    widget.show()
    shown, hidden = [], []
    monkeypatch.setattr(QtWidgets.QToolTip, "showText", lambda _pos, text, _widget: shown.append(text))
    monkeypatch.setattr(QtWidgets.QToolTip, "hideText", lambda: hidden.append(True))
    yield app, widget, shown, hidden
    widget.close()
    app.processEvents()


def move(widget, x, y):
    from PySide6.QtCore import QPointF
    widget._on_mouse_moved((widget._plot_item.vb.mapViewToScene(QPointF(x, y)),))


@pytest.mark.parametrize("theme_name", ["light", "dark"])
@pytest.mark.parametrize("sigma", [1, 2])
def test_point_curve_and_leave_hover(canvas, theme_name, sigma):
    import pyqtgraph as pg
    from PySide6 import QtCore, QtGui
    from GUI.theme import THEMES

    app, widget, shown, hidden = canvas
    data = hover_data()
    widget.show_data(data, THEMES[theme_name])
    widget.set_sigma_level(sigma)
    app.processEvents()
    widget._plot_item.vb.setRange(xRange=(0, 3000), yRange=(.99, 1.04), padding=0)
    statuses = []
    widget.set_spot_status_provider(lambda name: (statuses.append(name) or True, "Check signal"))
    move(widget, 1234.5, 1.0234)
    text = shown[-1]
    for part in ("RM_37", "Reference material", "#37 of 212", "1,234.5 s (0:20:34)",
                 f"± {sigma * .002:.4f} ({sigma}σ)", "Used in session fit", "weight 0.83 (relative)",
                 "residual −0.0012", "Excluded in review", "Flagged: Check signal", "206Pb/238U 0.17588"):
        assert part in text
    assert statuses == ["RM_37"]
    ring = widget._hover_ring
    assert ring.isVisible()
    assert ring.pen().color() == QtGui.QColor(THEMES[theme_name].accent)
    assert ring not in widget._plot_item.vb.addedItems
    scatters = [item for item in widget._plot_item.items if isinstance(item, pg.ScatterPlotItem)]
    assert len(scatters) == 1 and not scatters[0].opts["hoverable"]
    # The fit has only two samples: this exercises the intervening segment.
    move(widget, 1800., 1.01)
    assert f"Session fit (linear) · t 1,800.0 s · factor 1.0100 ± {sigma * .00175:.4f} ({sigma}σ)" in shown[-1]
    assert not ring.isVisible()
    assert statuses == ["RM_37"]
    move(widget, 1800., 1.035)
    assert hidden
    assert not ring.isVisible()
    move(widget, 1234.5, 1.0234)
    widget.leaveEvent(QtCore.QEvent(QtCore.QEvent.Type.Leave))
    assert not ring.isVisible()
    move(widget, 1234.5, 1.0234)
    widget.apply_theme(THEMES["dark" if theme_name == "light" else "light"])
    assert not widget._hover_ring.isVisible()
    assert widget._hover_ring is not ring
    assert widget._hover_ring.pen().color() == QtGui.QColor(widget._theme.accent)
    widget.show_data(None, THEMES[theme_name])
    assert widget._point_index is None
    assert widget._hover_ring is None
    widget.show_data(data, THEMES[theme_name])
    widget.clear()
    assert widget._point_index is None
    widget.show_data(data, THEMES[theme_name])
    assert widget._plot_item.scene() is widget.scene()


def test_optional_metadata_uncertainty_and_outside_view(canvas):
    from PySide6.QtCore import QPointF
    from GUI.theme import THEMES

    app, widget, shown, hidden = canvas
    data = hover_data()
    data.update(spot_weights=[np.nan, np.nan], spot_residuals=[np.nan, np.nan], show_uncertainty=False)
    widget.show_data(data, THEMES["light"])
    app.processEvents()
    move(widget, 1234.5, 1.0234)
    assert "weight" not in shown[-1] and "residual" not in shown[-1]
    assert "Used in session fit" in shown[-1]
    move(widget, 1800., 1.01)
    assert "Session fit" in shown[-1] and "±" not in shown[-1]
    count = len(shown)
    widget._on_mouse_moved((QPointF(-100., -100.),))
    assert len(shown) == count and hidden
    assert not widget._hover_ring.isVisible()


def test_5000_points_use_local_candidates(canvas, monkeypatch):
    from GUI.theme import THEMES

    app, widget, shown, _hidden = canvas
    n = 5000
    data = hover_data()
    data.update(spot_mean_times=np.arange(n, dtype=float), spot_mean_fracs=np.full(n, 1.0234),
                spot_frac_errs=np.full(n, .002), spot_names=[f"RM_{i}" for i in range(n)],
                spot_samples=["RM"] * n, spot_order=np.arange(1, n + 1), total_spots=n,
                spot_weights=np.ones(n), spot_residuals=np.zeros(n),
                fit_times=np.arange(n, dtype=float), fit_vals=np.full(n, 1.01))
    # Reverse the arrays to verify that the index retains source identities.
    for key in ("spot_mean_times", "spot_mean_fracs", "spot_frac_errs", "spot_names",
                "spot_samples", "spot_order", "spot_weights", "spot_residuals", "fit_times", "fit_vals"):
        data[key] = data[key][::-1]
    widget.show_data(data, THEMES["light"])
    app.processEvents()
    widget._plot_item.vb.setRange(xRange=(2000, 2100), yRange=(1., 1.04), padding=0)
    counts = []
    candidates = widget._hover_candidates
    def counted(*args, **kwargs):
        result = candidates(*args, **kwargs)
        counts.append(len(result))
        return result
    monkeypatch.setattr(widget, "_hover_candidates", counted)
    started = time.perf_counter()
    for i in range(200):
        move(widget, 2020. + i % 60, 1.0234 if i % 2 else 1.01)
    elapsed = time.perf_counter() - started
    assert elapsed < .8, elapsed
    assert max(counts) < 10
    assert "RM_2039" in shown[-1]
    assert "#2040 of 5000" in shown[-1]
