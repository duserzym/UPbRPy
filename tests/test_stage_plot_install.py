"""Frozen pre-optimization draws verify exact diagnostic pictures offscreen."""
import os
from pathlib import Path
from types import MethodType

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import numpy as np
import pandas as pd
import pytest
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets
from GUI import services, widgets
from GUI.theme import THEMES, build_application
from GUI.workflows import get_workflow_preset
from GUI.widgets import (AxisZoomViewBox, ConcordiaReviewWidget, _make_diag_axis,
                         _DIAG_SPOT_COLORS, _spot_time_spans)

def _old_downhole_draw(self) -> None:
    self.clear()
    self._spot_segments.clear()
    self._plot_item = None
    theme = self._theme
    data = self._data
    if theme is None:
        return
    if data is None:
        p = self.addPlot(0, 0, viewBox=AxisZoomViewBox())
        self._plot_item = p
        lbl = pg.TextItem("No downhole correction data available.\nRun the downhole correction step first.",
                          color=theme.muted, anchor=(0.5, 0.5))
        lbl.setFont(QtGui.QFont("", 11))
        p.addItem(lbl)
        lbl.setPos(0.5, 0.5)
        return

    ratio_name = data["ratio_name"]
    unified_time = data["unified_time"]
    mean_frac = data["mean_frac"]
    std_frac = float(data["std_frac"])
    primary_standard = data["primary_standard"]
    fit_label = str(data.get("fit_label", "Interpolated"))

    p = self.addPlot(row=0, col=0, viewBox=AxisZoomViewBox())
    self._plot_item = p
    _make_diag_axis(p, theme)
    p.setLabel("bottom", "Relative time (s)", color=theme.axis_text, size="9px")
    p.setLabel("left", "Fractionation factor", color=theme.axis_text, size="9px")
    p.setTitle(
        f"{ratio_name}  downhole fractionation — {primary_standard} ({fit_label})",
        color=theme.axis_text, size="10pt",
    )

    # Individual spot traces (thin, semi-transparent)
    spot_names = data.get("spot_names", [])
    for i, (rel_t, r) in enumerate(zip(data["spot_times"], data["spot_ratios"])):
        with np.errstate(divide="ignore", invalid="ignore"):
            frac = np.where(r > 0, data["known_ratio"] / r, np.nan)
        color = _DIAG_SPOT_COLORS[i % len(_DIAG_SPOT_COLORS)]
        c = pg.mkColor(color)
        c.setAlpha(120)
        p.plot(rel_t, frac, pen=pg.mkPen(c, width=1.2))
        if i < len(spot_names):
            points = np.column_stack((rel_t, frac))
            valid = np.all(np.isfinite(points), axis=1)
            segments = valid[:-1] & valid[1:]
            self._spot_segments.append((spot_names[i], points[:-1][segments], points[1:][segments]))

    # Residual scatter is the uncertainty model currently retained by the
    # reduction core.  The toolbar chooses whether one or two standard
    # deviations of that scatter are displayed around the fitted curve.
    if np.isfinite(std_frac) and std_frac >= 0:
        spread = self._sigma_level * std_frac
        upper = mean_frac + spread
        lower = mean_frac - spread
        fill_brush = pg.mkColor("#888888")
        fill_brush.setAlpha(30)
        upper_curve = p.plot(
            unified_time,
            upper,
            pen=pg.mkPen(
                "#888888",
                width=1,
                style=QtCore.Qt.PenStyle.DashLine,
            ),
        )
        lower_curve = p.plot(
            unified_time,
            lower,
            pen=pg.mkPen(
                "#888888",
                width=1,
                style=QtCore.Qt.PenStyle.DashLine,
            ),
        )
        fill = pg.FillBetweenItem(
            upper_curve,
            lower_curve,
            brush=fill_brush,
        )
        fill.uncertainty_band = True
        p.addItem(fill)

    # Mean fractionation (bold dashed white/dark)
    mean_color = theme.text
    p.plot(unified_time, mean_frac,
           pen=pg.mkPen(mean_color, width=2, style=QtCore.Qt.PenStyle.DashLine))

    # Reference line at 1.0
    p.addLine(y=1.0, pen=pg.mkPen(theme.muted, width=1,
                                   style=QtCore.Qt.PenStyle.DotLine))
    self._apply_zoom_mode()
    self._apply_interaction_mode()

def _old_session_draw(self) -> None:
    self.clear()
    self._plot_item = None
    theme = self._theme
    data = self._data
    if theme is None:
        return
    if data is None:
        p = self.addPlot(0, 0, viewBox=AxisZoomViewBox())
        self._plot_item = p
        lbl = pg.TextItem("No session fractionation data available.\nRun the session correction step first.",
                          color=theme.muted, anchor=(0.5, 0.5))
        lbl.setFont(QtGui.QFont("", 11))
        p.addItem(lbl)
        lbl.setPos(0.5, 0.5)
        return

    ratio_name = data["ratio_name"]
    primary_standard = data["primary_standard"]
    t_arr = data["spot_mean_times"]
    f_arr = data["spot_mean_fracs"]
    e_arr = data["spot_frac_errs"]
    fit_times = data["fit_times"]
    fit_vals = data["fit_vals"]
    fit_std = float(data["fit_std"])
    show_uncertainty = bool(data.get("show_uncertainty", False))
    fit_label = str(data.get("fit_label", "Polynomial"))

    p = self.addPlot(row=0, col=0, viewBox=AxisZoomViewBox())
    self._plot_item = p
    _make_diag_axis(p, theme)
    p.setLabel("bottom", "Session time (s)", color=theme.axis_text, size="9px")
    p.setLabel("left", "Fractionation factor", color=theme.axis_text, size="9px")
    p.setTitle(
        f"{ratio_name}  session fractionation — {primary_standard} ({fit_label})",
        color=theme.axis_text, size="10pt",
    )

    if show_uncertainty:
        spread = self._sigma_level * fit_std
        fill_brush = pg.mkColor("#888888")
        fill_brush.setAlpha(30)
        upper_curve = p.plot(
            fit_times,
            fit_vals + spread,
            pen=pg.mkPen("#888888", width=1, style=QtCore.Qt.PenStyle.DashLine),
        )
        lower_curve = p.plot(
            fit_times,
            fit_vals - spread,
            pen=pg.mkPen("#888888", width=1, style=QtCore.Qt.PenStyle.DashLine),
        )
        fill = pg.FillBetweenItem(upper_curve, lower_curve, brush=fill_brush)
        fill.uncertainty_band = True
        p.addItem(fill)

    # Polynomial fit follows the same weighted notebook fit stored by the core.
    p.plot(fit_times, fit_vals, pen=pg.mkPen("#e05050", width=2))

    point_color = theme.text
    if show_uncertainty:
        err = pg.ErrorBarItem(
            x=t_arr,
            y=f_arr,
            height=2 * self._sigma_level * e_arr,
            beam=0,
            pen=pg.mkPen(point_color, width=1.2),
        )
        p.addItem(err)
    scatter = pg.ScatterPlotItem(
        x=t_arr,
        y=f_arr,
        symbol="o",
        size=8,
        pen=pg.mkPen(point_color, width=1.2),
        brush=pg.mkBrush(point_color),
        hoverable=True,
        hoverSize=12,
        data=data.get("spot_names", [primary_standard] * len(t_arr)),
        tip=lambda x, y, name: f"{name}\nSession time: {x:,.2f} s\nFactor: {y:.5g}",
    )
    p.addItem(scatter)

    # Reference line at 1.0
    p.addLine(y=1.0, pen=pg.mkPen(theme.muted, width=1,
                                   style=QtCore.Qt.PenStyle.DotLine))
    self._apply_zoom_mode()
    self._apply_interaction_mode()

def _old_show_series(
    self,
    frame: pd.DataFrame,
    series_names: list[str],
    series_colors: dict[str, str],
    *,
    y_label: str,
    line_style: str = "line",
) -> None:
    if frame.empty:
        self.show_empty("")
        return
    if not series_names:
        self.show_empty("Check one or more channels to display traces.", y_label=y_label)
        return

    self._reset_items()
    self._hover_spans = _spot_time_spans(frame)
    label_style = {"color": self._theme.axis_text, "font-size": "15px", "font-weight": "600"}
    self.plot_item.setLabel("left", y_label, **label_style)
    x_values = frame["Time"].to_numpy(dtype=float)
    y_min = None
    y_max = None

    for name in series_names:
        if name not in frame:
            continue
        y_values = frame[name].to_numpy(dtype=float)
        finite_values = y_values[np.isfinite(y_values)]
        if not finite_values.size:
            continue
        color = series_colors.get(name, "#1f63cf")
        symbol = None if line_style == "line" else "o"
        # Configure clipping before the first data assignment. Creating a
        # populated item first builds the full-session symbol atlas even
        # though the attached view will immediately downsample it.
        curve = pg.PlotDataItem(
            pen=pg.mkPen(color, width=2.0),
            name=name,
            symbol=symbol,
            symbolSize=5 if symbol else None,
            symbolBrush=color if symbol else None,
            symbolPen=pg.mkPen(color, width=1) if symbol else None,
        )
        curve.setDownsampling(auto=True, method="peak")
        curve.setClipToView(True)
        self.plot_item.addItem(curve)
        curve.setData(x_values, y_values)
        self._hover_series[name] = (x_values, y_values)
        current_min = float(np.min(finite_values))
        current_max = float(np.max(finite_values))
        y_min = current_min if y_min is None else min(y_min, current_min)
        y_max = current_max if y_max is None else max(y_max, current_max)

    ranges = dict(xRange=(float(np.min(x_values)), float(np.max(x_values))))
    if y_min is not None and y_max is not None:
        lower = y_min - abs(y_min) * 0.08 if y_min < 0 else y_min * 0.92
        upper = y_max * 1.08 if y_max > 0 else 1.0
        ranges["yRange"] = (lower, upper)
    self.plot_item.vb.setRange(**ranges, padding=0.02)

def _old_show_depth_profile(
    self,
    depth_profile: pd.DataFrame,
    spot_name: str | None,
    *,
    include_calibration_uncertainty: bool = True,
) -> None:
    """Review the useful sweep-wise columns for one selected spot."""
    self._include_calibration_uncertainty = bool(
        include_calibration_uncertainty
    )
    self._depth_sample_order = self._sample_order(depth_profile)
    selected = depth_profile
    if spot_name and "spot_name" in selected:
        selected = selected[selected["spot_name"].eq(spot_name)]
    review_columns = [
        column
        for column in ("sample", "spot_name", "signal_relative_time")
        if column in selected.columns
    ]
    for ratio in ("206Pb/238U", "207Pb/206Pb", "208Pb/232Th"):
        ratio_column = f"{ratio}_fractionation_corrected"
        if ratio_column in selected.columns:
            review_columns.append(ratio_column)
            uncertainty_column = ConcordiaReviewWidget.uncertainty_column(
                selected,
                ratio_column,
                self._include_calibration_uncertainty,
            )
            if uncertainty_column:
                review_columns.append(uncertainty_column)
    for ratio in ("206Pb/238U", "207Pb/206Pb", "208Pb/232Th"):
        age_column = f"{ratio}_age"
        if age_column in selected.columns:
            review_columns.append(age_column)
            uncertainty_column = ConcordiaReviewWidget.uncertainty_column(
                selected,
                age_column,
                self._include_calibration_uncertainty,
            )
            if uncertainty_column:
                review_columns.append(uncertainty_column)
    review_frame = selected.loc[:, review_columns].copy()
    review_frame = review_frame.rename(
        columns={
            column: str(column).replace("_age", "_date")
            for column in review_frame.columns
            if "_age" in str(column)
        }
    )
    self.depth_model.set_frame(review_frame)
    self._depth_frame = depth_profile.copy()
    self._depth_sample_choices = False
    glasses = self._glass_spot_names()
    for name in self._sample_names(self._depth_frame):
        self.include_flags.setdefault(name, self._show_glasses if name in glasses else True)
    self._build_depth_choices()
    self._rebuild_sample_chips()
    self._update_glass_buttons()
    existing_spot = self.depth_spot_combo.currentText()
    spot_names = (
        depth_profile["spot_name"].dropna().astype(str).drop_duplicates().tolist()
        if "spot_name" in depth_profile.columns
        else []
    )
    self.depth_spot_combo.blockSignals(True)
    self.depth_spot_combo.clear()
    self.depth_spot_combo.addItems(spot_names)
    for index, name in enumerate(spot_names):
        self.depth_spot_combo.setItemData(index, name, QtCore.Qt.ItemDataRole.ToolTipRole)
    preferred = spot_name or existing_spot
    preferred_index = self.depth_spot_combo.findText(preferred)
    if preferred_index >= 0:
        self.depth_spot_combo.setCurrentIndex(preferred_index)
    self.depth_spot_combo.blockSignals(False)
    self.depth_spot_combo.setToolTip(self.depth_spot_combo.currentText())
    self.refresh_depth_concordia()
    target = spot_name or "all spots"
    self.summary_label.setText(
        f"{len(selected):,} corrected signal sweeps · {target}"
    )
    if self.tabs.currentWidget() not in (
        self.depth_table,
        self.depth_concordia_page,
    ):
        self.tabs.setCurrentWidget(self.depth_table)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


@pytest.fixture(scope="module")
def reduced():
    preset = get_workflow_preset("zircon")
    path = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
    session = services.load_session(str(path), preset=preset)
    session.total_data = session.total_data.assign(**{"202Hg": 1.})
    session.channels = [*session.channels, "202Hg"]
    session.pipeline = None
    session.session_fit_method = "mean"
    session.primary_68_standard = session.primary_76_standard = session.primary_82_standard = "S513"
    services.ensure_depth_profile(session, preset)
    return session, preset


def _grab(widget, app):
    widget.resize(900, 500)
    widget.show()
    for _ in range(3):
        app.processEvents()
        widget.grab()
    return widget.grab().toImage()


@pytest.mark.parametrize("kind", ["trace", "downhole", "session", "depth"])
@pytest.mark.parametrize("sigma", [1, 2])
def test_frozen_draw_pixel_identity(app, reduced, kind, sigma):
    session, preset = reduced
    theme = THEMES["light"]
    if kind == "trace":
        old, new = widgets.TracePlotWidget(), widgets.TracePlotWidget()
        old.show_series = MethodType(_old_show_series, old)
        frame = services.ensure_normalized_plateau(session, preset)
        channels = session.channels[:3]
        for view in (old, new):
            view.show_series(frame, channels, {c: "#1f63cf" for c in channels},
                             y_label="Normalized plateau cps", line_style="scatter")
    elif kind == "depth":
        old, new = widgets.ResultsTableWidget(), widgets.ResultsTableWidget()
        old.show_depth_profile = MethodType(_old_show_depth_profile, old)
        frame = session.reduction_cache["depth_profile_results"]
        for view in (old, new):
            view.show_depth_profile(frame, str(frame.spot_name.iloc[0]),
                                    include_calibration_uncertainty=sigma == 2)
    else:
        cls, draw, get_data = ((widgets.DownholeFractionationWidget, _old_downhole_draw,
                               services.get_downhole_plot_data) if kind == "downhole" else
                              (widgets.SessionFractionationWidget, _old_session_draw,
                               services.get_session_plot_data))
        old, new = cls(), cls()
        old._draw = MethodType(draw, old)
        data = get_data(session, preset)
        assert data is not None
        for view in (old, new):
            view.set_sigma_level(sigma)
            view.show_data(data, theme)
    try:
        assert _grab(old, app) == _grab(new, app)
        # Exercise the retained items too, with changed options and data.
        if kind in ("downhole", "session"):
            for view in (old, new):
                view.set_sigma_level(3 - sigma)
            assert _grab(old, app) == _grab(new, app)
            for view in (old, new):
                view.show_data(None, theme)
            assert _grab(old, app) == _grab(new, app)
            for view in (old, new):
                view.show_data(data, theme)
            assert _grab(old, app) == _grab(new, app)
        elif kind == "trace":
            for view in (old, new):
                view.show_series(frame, channels[:2], {c: "#e05050" for c in channels},
                                 y_label="Changed channel", line_style="line")
            assert _grab(old, app) == _grab(new, app)
            for view in (old, new):
                view.show_series(frame, channels[::-1], {c: "#e05050" for c in channels},
                                 y_label="Reordered", line_style="scatter")
            assert _grab(old, app) == _grab(new, app)
        elif kind == "depth":
            changed = frame.copy()
            changed["206Pb/238U_fractionation_corrected"] *= 1.01
            spot = str(changed.spot_name.iloc[-1])
            for view in (old, new):
                view.show_depth_profile(changed, spot,
                                        include_calibration_uncertainty=sigma != 2)
            assert _grab(old, app) == _grab(new, app)
            pd.testing.assert_frame_equal(old.depth_model.frame, new.depth_model.frame,
                                          check_exact=True, check_flags=False)
    finally:
        for view in (old, new):
            view.hide()
            view.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def test_downhole_data_cache_tracks_sources_ratio_and_options(reduced):
    session, preset = reduced
    first = services.get_downhole_plot_data(session, preset)
    assert services.get_downhole_plot_data(session, preset) is first
    other = services.get_downhole_plot_data(session, preset, "207Pb/206Pb")
    assert other is not first
    session.downhole_fit_method, original = "linear", session.downhole_fit_method
    try:
        assert services.get_downhole_plot_data(session, preset) is not first
    finally:
        session.downhole_fit_method = original
    services.ensure_downhole_results(session, preset)
    first = services.get_downhole_plot_data(session, preset)
    # Replace the result without changing any values: identity alone invalidates.
    session.reduction_cache["downhole_results"] = dict(session.reduction_cache["downhole_results"])
    assert services.get_downhole_plot_data(session, preset) is not first


def test_trace_reuses_items_and_updates_result_channel_and_style(app):
    view = widgets.TracePlotWidget()
    frame = pd.DataFrame({"Time": [0., 1.], "A": [2., 3.], "B": [4., 5.]})
    try:
        view.show_series(frame, ["A"], {"A": "red"}, y_label="A", line_style="scatter")
        curve = view._series_items["A"]
        changed = frame.assign(A=[6., 7.])
        view.show_series(changed, ["A", "B"], {"A": "blue"}, y_label="B")
        assert view._series_items["A"] is curve
        np.testing.assert_array_equal(curve.getOriginalDataset()[1], [6., 7.])
        assert curve.opts["pen"].color() == QtGui.QColor("blue")
        assert curve.opts["symbol"] is None
        view.show_series(changed, ["B"], {}, y_label="B")
        assert set(view._series_items) == {"B"}
    finally:
        view.hide()
        view.deleteLater()


def test_downhole_cache_does_not_retain_closed_session(reduced):
    import copy
    import gc
    import weakref
    source, preset = reduced
    session = copy.copy(source)
    data = services.get_downhole_plot_data(session, preset)
    assert data is not None
    identity = id(session)
    reference = weakref.ref(session)
    assert identity in services._downhole_plot_cache
    del session
    gc.collect()
    assert reference() is None
    assert identity not in services._downhole_plot_cache


def test_uniform_scatter_falls_back_for_per_point_style(app):
    scatter = widgets._UniformScatterItem()
    reference = pg.ScatterPlotItem()
    for item in (scatter, reference):
        item.setData([0., 1.], [2., 3.], size=[4., 8.],
                     pen=[pg.mkPen("red"), pg.mkPen("blue")])
    np.testing.assert_array_equal(scatter.data["sourceRect"], reference.data["sourceRect"])


def test_downhole_arrays_match_original_spot_slicing(reduced):
    session, preset = reduced
    services.ensure_downhole_results(session, preset)
    data = services.get_downhole_plot_data(session, preset)
    raw = session.reduction_cache["signal_data_with_raw_ratios"]
    primary = raw[raw["sample"] == data["primary_standard"]]
    expected_names = []
    for name in primary["spot_name"].unique():
        spot = primary[primary["spot_name"] == name]
        time = spot["Time"].values.astype(float)
        ratio = spot[data["ratio_name"]].values.astype(float)
        valid = np.isfinite(ratio) & (ratio > 0)
        if valid.sum() < 2:
            continue
        index = len(expected_names)
        np.testing.assert_array_equal(data["spot_times"][index], (time - time[0])[valid])
        np.testing.assert_array_equal(data["spot_ratios"][index], ratio[valid])
        expected_names.append(str(name))
    assert data["spot_names"] == expected_names
