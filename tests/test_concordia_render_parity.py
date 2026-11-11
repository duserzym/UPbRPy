"""Parity with the per-row concordia renderer, frozen before batching."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from GUI.theme import THEMES, build_application
from GUI.widgets import (ConcordiaReviewWidget, ResultsTableWidget,
                         _DIAG_SPOT_COLORS, _REVIEW_SAMPLE_COLORS)
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def synthetic_frame(n=2000, *, kind="depth", branches=True):
    rng = np.random.default_rng(419)
    spots = np.arange(n) % 80
    frame = pd.DataFrame({"spot_name": [f"spot_{i}" for i in spots],
                          "sample": [f"sample_{i % 6}" for i in spots],
                          "signal_relative_time": np.arange(n) // 80 * .4255})
    prefix = "_fractionation_corrected" if kind == "depth" else ""
    ages = rng.uniform(100, 1200, n)
    wx, wy = wetherill_point(ages)
    tx, ty = tera_wasserburg_point(ages)
    for ratio, values in zip(("207Pb/235U", "206Pb/238U", "238U/206Pb", "207Pb/206Pb"),
                             (wx, wy, tx, ty)):
        column = ratio + prefix
        frame[column] = values * rng.uniform(.95, 1.05, n)
        errors = values * rng.uniform(.01, .08, n)
        errors[:6] = [np.nan, np.inf, 0, -.01, .01, .02]
        frame[column + "_2e"] = errors
        if branches:
            frame[column + "_2e_with_calibration"] = errors * 1.4
            frame[column + "_2e_no_calibration"] = errors * .8
        frame.loc[6, column] = np.nan
        frame.loc[7, column] = np.inf
    for stem in ("err_corr", "tw_rho"):
        rho = rng.uniform(-.95, .95, n)
        rho[8:17] = [np.nan, -1, 1, -5, 5, np.inf, -np.inf, 0, .999]
        frame[stem] = rho
        if branches:
            frame[stem + "_with_calibration"] = rho
            frame[stem + "_no_calibration"] = -rho
    # Duplicate labels deliberately: cache row identity must be positional.
    frame.index = np.arange(n) // 2
    return frame


def old_ellipse(x, y, x_2e, y_2e, rho, *, points=48):
    """The original scalar ellipse implementation, independent of the new API."""
    if not all(np.isfinite([x, y, x_2e, y_2e])):
        return np.array([]), np.array([])
    sx = float(x_2e) / 2.0
    sy = float(y_2e) / 2.0
    if sx <= 0 or sy <= 0:
        return np.array([]), np.array([])
    correlation = float(np.clip(0.0 if np.isnan(rho) else rho, -0.999, 0.999))
    covariance = np.array([[sx * sx, correlation * sx * sy],
                           [correlation * sx * sy, sy * sy]], dtype=float)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    eigenvalues = np.maximum(eigenvalues, 0.0)
    count = max(4, int(np.ceil((points - 1) / 4)) * 4)
    angle = np.linspace(0.0, 2.0 * np.pi, count + 1)
    circle = np.vstack((np.cos(angle), np.sin(angle)))
    offsets = eigenvectors @ np.diag(2.0 * np.sqrt(eigenvalues)) @ circle
    return x + offsets[0], y + offsets[1]


def old_depth_brushes(frame, theme, colormap="plasma"):
    """Original scalar colour loop from _depth_brushes, without the colour bar."""
    use_time = "signal_relative_time" in frame
    times = (pd.to_numeric(frame["signal_relative_time"], errors="coerce").to_numpy(dtype=float)
             if use_time else np.arange(len(frame), dtype=float))
    finite = times[np.isfinite(times)]
    low, high = (float(finite.min()), float(finite.max())) if finite.size else (0., 1.)
    cmap = ConcordiaReviewWidget.depth_color_map(colormap)
    normalized = (times - low) / max(high - low, np.finfo(float).eps)
    return [cmap.mapToQColor(float(np.clip(value, 0, 1))) if np.isfinite(value)
            else pg.mkColor(theme.muted) for value in normalized]


def old_rows(frame, diagram, kind, calibration, brushes):
    """Original _draw point and ellipse loops and branch resolution."""
    prefix = "" if kind == "spot" else "_fractionation_corrected"
    x_column, y_column = (("207Pb/235U", "206Pb/238U") if diagram == "wetherill"
                          else ("238U/206Pb", "207Pb/206Pb"))
    x_column, y_column = x_column + prefix, y_column + prefix
    branch = "with_calibration" if calibration else "no_calibration"

    def error_column(column):
        return next(c for c in (f"{column}_2e_{branch}", f"{column}_2e") if c in frame)

    x_values = pd.to_numeric(frame[x_column], errors="coerce").to_numpy()
    y_values = pd.to_numeric(frame[y_column], errors="coerce").to_numpy()
    x_errors = pd.to_numeric(frame[error_column(x_column)], errors="coerce").to_numpy()
    y_errors = pd.to_numeric(frame[error_column(y_column)], errors="coerce").to_numpy()
    stem = "err_corr" if diagram == "wetherill" else "tw_rho"
    column = next((c for c in (f"{stem}_{branch}", stem) if c in frame), None)
    rho_values = pd.to_numeric(frame[column], errors="coerce").to_numpy() if column else np.zeros(len(frame))
    finite = np.isfinite(x_values) & np.isfinite(y_values)
    point_indices = np.flatnonzero(finite)
    rows = frame.loc[finite].to_dict("records")
    points = [(str(row.get("spot_name", "")), float(x_values[i]), float(y_values[i]), brushes[i])
              for row, i in zip(rows, point_indices)]
    ellipses = []
    for (name, x, y, color), i in zip(points, point_indices):
        ex, ey = old_ellipse(x, y, x_errors[i], y_errors[i], float(rho_values[i]))
        if ex.size:
            ellipses.append((name, ex, ey, color))
    return points, ellipses


def dispose(widget, app):
    widget.close()
    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize("diagram", ["wetherill", "tera_wasserburg"])
@pytest.mark.parametrize("calibration", [True, False])
@pytest.mark.parametrize("branches", [True, False])
def test_depth_parity(app, diagram, calibration, branches):
    source = synthetic_frame(branches=branches)
    # Hide rows before presenting a subset; normalization must use that subset.
    positions = np.flatnonzero(~source.spot_name.isin({"spot_3", "spot_9"}).to_numpy())
    frame = source.iloc[positions].copy()
    frame.loc[frame.signal_relative_time.eq(0), "signal_relative_time"] = np.nan
    source = source.copy()
    source.iloc[positions] = frame
    canvas = ConcordiaReviewWidget()
    theme = THEMES["light"]
    try:
        canvas.show_data(frame, source_frame=source, row_positions=positions,
                         diagram=diagram, kind="depth", theme=theme,
                         include_calibration_uncertainty=calibration)
        points, ellipses = old_rows(frame, diagram, "depth", calibration, old_depth_brushes(frame, theme))
        np.testing.assert_array_equal(canvas._point_xy, np.array([(p[1], p[2]) for p in points]))
        assert [p[0] for p in canvas._points] == [p[0] for p in points]
        assert len(canvas._ellipses) == len(ellipses)
        for (name, handle, color), (expected_name, ex, ey, expected_color) in zip(canvas._ellipses, ellipses):
            assert name == expected_name
            assert handle.isVisible()
            for actual, expected in zip(handle.getData(), (ex, ey)):
                np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=0)
            assert color.name() == expected_color.name()
            assert color.alpha() == expected_color.alpha()
        for point, expected in zip(canvas._scatter.points(), points):
            assert point.brush().color().name() == expected[3].name()
            assert point.brush().color().alpha() == expected[3].alpha()
        assert not {"spot_3", "spot_9"} & {p[0] for p in canvas._points}
        pd.testing.assert_frame_equal(canvas._frame, frame)
        # Tooltip dictionaries remain unmaterialized until accessed.
        assert not canvas._point_rows.rows
        assert "Sweep time:" in canvas.tooltip_text(100)
        assert len(canvas._point_rows.rows) == 1
        assert len(canvas._ellipse_batch.paths) <= 65
    finally:
        dispose(canvas, app)


@pytest.mark.parametrize("mode", ["sample", "spot", "single"])
@pytest.mark.parametrize("diagram", ["wetherill", "tera_wasserburg"])
def test_spot_parity(app, mode, diagram):
    frame = synthetic_frame(80, kind="spot")
    canvas = ConcordiaReviewWidget()
    theme = THEMES["light"]
    canvas.set_spot_context(frame, frame, {})
    samples = sorted(frame["sample"].unique())
    sample_colors = {s: _REVIEW_SAMPLE_COLORS[i % len(_REVIEW_SAMPLE_COLORS)] for i, s in enumerate(samples)}
    spot_colors = {n: _DIAG_SPOT_COLORS[i % len(_DIAG_SPOT_COLORS)] for i, n in enumerate(frame.spot_name)}
    brushes = [pg.mkColor(sample_colors[str(row.get("sample", ""))] if mode == "sample"
                         else theme.accent if mode == "single" else spot_colors[str(row.get("spot_name", ""))])
               for row in frame.to_dict("records")]
    included = set(frame.spot_name) - {"spot_12"}
    try:
        canvas.show_data(frame, diagram=diagram, kind="spot", theme=theme, colour_mode=mode,
                         included_spots=included, include_calibration_uncertainty=True)
        points, ellipses = old_rows(frame, diagram, "spot", True, brushes)
        visible = [p for p in points if p[0] in included]
        np.testing.assert_array_equal(canvas._point_xy, np.array([(p[1], p[2]) for p in visible]))
        for (_, handle, color), (name, ex, ey, expected_color) in zip(canvas._ellipses, ellipses):
            assert handle.isVisible() == (name in included)
            for actual, expected in zip(handle.getData(), (ex, ey)):
                np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=0)
            assert color == expected_color
            assert handle.opts["pen"].color().name() == expected_color.name()
            assert handle.opts["pen"].color().alpha() == 155
            assert handle.opts["pen"].widthF() == 1
        for point, expected in zip(canvas._scatter.points(), visible):
            assert point.brush().color() == expected[3]
    finally:
        dispose(canvas, app)


@pytest.mark.parametrize("points", [1, 4, 17, 48, 101])
def test_batch_matches_scalar_grid_and_invalid_rows(points):
    values = np.array([1., np.nan, np.inf, 2., 3., 4., 5.])
    errors = np.array([.1, .1, .1, 0., -.1, .4, .5])
    rho = np.array([np.nan, 0, 0, 1, -1, 8, -8])
    ex, ey, valid = ConcordiaReviewWidget.ellipse_coordinates_batch(values, values, errors, errors, rho, points=points)
    assert valid.tolist() == [True, False, False, False, False, True, True]
    for i in np.flatnonzero(valid):
        reference = old_ellipse(values[i], values[i], errors[i], errors[i], rho[i], points=points)
        np.testing.assert_allclose(ex[i], reference[0], rtol=1e-12, atol=0)
        np.testing.assert_allclose(ey[i], reference[1], rtol=1e-12, atol=0)
    assert np.isnan(ex[~valid]).all() and np.isnan(ey[~valid]).all()
    assert ex.shape == (len(values), max(4, int(np.ceil((points - 1) / 4)) * 4) + 1)


def test_depth_reuses_geometry_and_scene_and_invalidates_new_results(app, monkeypatch):
    frame = synthetic_frame()
    spot = synthetic_frame(80, kind="spot")
    review = ResultsTableWidget()
    try:
        review.apply_theme(THEMES["light"])
        review.show_results(SimpleNamespace(parallel_results=spot, selected_results=spot))
        review.show_depth_profile(frame, "spot_0")
        review.set_all_spot_choices(True)
        canvas = review.depth_concordia
        items = canvas._plot_item, canvas._scatter, canvas._ellipse_batch, canvas._curve, canvas._hover_text
        geometry = canvas._geometry()
        original_draw = canvas._draw
        original_batch = canvas.ellipse_coordinates_batch
        monkeypatch.setattr(canvas, "_draw", lambda **kw: pytest.fail("rebuilt scene"))
        monkeypatch.setattr(canvas, "ellipse_coordinates_batch", lambda *a, **kw: pytest.fail("recomputed geometry"))
        for names in ({"spot_2"}, {"spot_2", "spot_8"}):
            review.set_spot_included(names, False)
            assert not names & set(canvas._frame.spot_name)
            assert canvas._geometry() is geometry
        for page in (review.spot_concordia_page, review.depth_concordia_page):
            review.tabs.setCurrentWidget(page)
            review.refresh_spot_concordia()
            review.refresh_depth_concordia(reset_view=False)
        review.show_samples(["sample_1"], mode="replace")
        assert set(canvas._frame["sample"]) == {"sample_1"}
        assert items == (canvas._plot_item, canvas._scatter, canvas._ellipse_batch, canvas._curve, canvas._hover_text)
        monkeypatch.setattr(canvas, "_draw", original_draw)
        monkeypatch.setattr(canvas, "ellipse_coordinates_batch", original_batch)
        replacement = frame.copy()
        replacement["207Pb/235U_fractionation_corrected"] *= 2
        review.show_depth_profile(replacement, "spot_1")
        assert canvas._geometry() is not geometry
        np.testing.assert_array_equal(canvas._geometry()[0], geometry[0] * 2)
    finally:
        dispose(review, app)


def test_sample_names_first_occurrence_and_order():
    first = pd.DataFrame({"spot_name": ["b", "a", "b", "c"], "sample": [None, "A", "B", "C"]})
    second = pd.DataFrame({"spot_name": ["a", "d"], "sample": ["changed", "D"]})
    assert list(ResultsTableWidget._sample_names(first, second).items()) == [
        ("b", ""), ("a", "A"), ("c", "C"), ("d", "D")]


@pytest.mark.parametrize("diagram", ["wetherill", "tera_wasserburg"])
def test_vectorized_visible_interval_matches_original(app, diagram):
    canvas = ConcordiaReviewWidget()
    point = wetherill_point if diagram == "wetherill" else tera_wasserburg_point
    canvas._diagram = diagram
    try:
        for low, high in ((.001, 1000), (563, 563.8), (1200, 4500), (0, 5000)):
            ratios = point(np.array([max(low, .0001), high]))
            ranges = [sorted(axis) for axis in ratios]
            lower, upper = .001, 4567.3
            for axis, limits in enumerate(ranges):
                ends = point(np.array([.001, 4567.3]))[axis]
                increasing = ends[1] > ends[0]
                ages = []
                for limit in sorted(limits):
                    lo, hi = .001, 4567.3
                    for _ in range(48):
                        mid = (lo + hi) / 2
                        if (point(mid)[axis] < limit) == increasing:
                            lo = mid
                        else:
                            hi = mid
                    ages.append((lo + hi) / 2)
                lower = max(lower, min(ages))
                upper = min(upper, max(ages))
            np.testing.assert_array_equal(canvas._visible_age_interval(ranges), (lower, upper))
    finally:
        dispose(canvas, app)


def test_empty_subset_can_be_restored_without_stale_message(app):
    canvas = ConcordiaReviewWidget()
    frame = synthetic_frame(80)
    kwargs = dict(source_frame=frame, diagram="wetherill", kind="depth", theme=THEMES["light"],
                  include_calibration_uncertainty=True)
    try:
        canvas.show_data(frame.iloc[:0], row_positions=np.array([], dtype=int), **kwargs)
        assert canvas._empty_message.isVisible()
        canvas.show_data(frame, **kwargs)
        assert not canvas._empty_message.isVisible()
        assert len(canvas._points) == len(frame) - 2
        geometry = canvas._geometry()
        canvas.show_data(frame.iloc[:0], row_positions=np.array([], dtype=int), **kwargs)
        assert canvas._empty_message.isVisible()
        assert not canvas._points and not canvas._ellipses
        canvas.show_data(frame, **kwargs)
        assert not canvas._empty_message.isVisible()
        assert canvas._geometry() is geometry
    finally:
        dispose(canvas, app)
