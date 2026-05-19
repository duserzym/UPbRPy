from __future__ import annotations


import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from GUI.theme import ThemeSpec


class MetricCard(QtWidgets.QFrame):
    def __init__(self, label: str, value: str, tone: str) -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(2)

        label_widget = QtWidgets.QLabel(label)
        label_widget.setObjectName("MetricLabel")
        self.value_widget = QtWidgets.QLabel(value)
        self.value_widget.setObjectName("MetricValue")
        swatch = QtWidgets.QLabel()
        swatch.setFixedHeight(5)
        swatch.setStyleSheet(f"background: {tone}; border-radius: 2px; margin-top: 6px;")

        layout.addWidget(label_widget)
        layout.addWidget(self.value_widget)
        layout.addWidget(swatch)

    def set_value(self, value: str) -> None:
        self.value_widget.setText(value)


class TracePlotWidget(pg.PlotWidget):
    regionChanged = QtCore.Signal(str, tuple)
    xWindowChanged = QtCore.Signal(tuple)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(200)
        self.setSizePolicy(
            QtWidgets.QSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
        )
        self.plot_item = self.getPlotItem()
        self._theme = ThemeSpec(
            name="dark",
            window="#07111c",
            window_alt="#091a2b",
            surface="#0d1c2d",
            surface_soft="#11263a",
            input_bg="#091523",
            input_border="#2a4159",
            text="#eaf2ff",
            muted="#90a6c1",
            accent="#69b4ff",
            accent_soft="rgba(105, 180, 255, 0.12)",
            accent_hover="rgba(105, 180, 255, 0.20)",
            accent_strong="#8bd7ff",
            success="#7ef7c9",
            success_soft="rgba(126, 247, 201, 0.12)",
            plot_background="#06111d",
            axis_text="#edf4ff",
            axis_line="#6d8298",
            grid_alpha=0.13,
        )
        self._region_overlays: dict[str, dict[str, object]] = {}
        self._hover_series: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._sigma_level = 2
        self._zoom_mode = "xy"
        self._interaction_mode = "zoom"
        self._suspend_x_window_signal = False
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        self._hover_proxy = pg.SignalProxy(self.scene().sigMouseMoved, rateLimit=60, slot=self._on_scene_mouse_moved)
        self.plot_item.vb.sigXRangeChanged.connect(self._on_x_range_changed)
        self._configure_plot()
        self.show_preview()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "plot_item"):
            return
        self.plot_item.layout.setRowFixedHeight(3, 62)
        self.plot_item.getAxis("bottom").setHeight(54)
        self.plot_item.showAxis("bottom")

    def apply_theme(self, theme: ThemeSpec) -> None:
        self._theme = theme
        self.setBackground(theme.plot_background)
        self._configure_plot()

    def _configure_plot(self) -> None:
        self.setBackground(self._theme.plot_background)
        self.showGrid(x=True, y=True, alpha=self._theme.grid_alpha)
        self.setMenuEnabled(False)
        self._apply_zoom_mode()
        self._apply_interaction_mode()
        self.setClipToView(True)
        self.plot_item.hideButtons()
        self.plot_item.setContentsMargins(20, 16, 20, 8)
        self.plot_item.layout.setRowFixedHeight(3, 62)
        label_style = {"color": self._theme.axis_text, "font-size": "15px", "font-weight": "600"}
        self.plot_item.setLabel("bottom", "Acquisition Time (s)", **label_style)
        self.plot_item.setLabel("left", "Counts / cps", **label_style)
        tick_font = QtGui.QFont()
        tick_font.setPointSizeF(11.5)
        left_axis = self.plot_item.getAxis("left")
        bottom_axis = self.plot_item.getAxis("bottom")
        left_axis.setTextPen(self._theme.axis_text)
        bottom_axis.setTextPen(self._theme.axis_text)
        left_axis.setPen(self._theme.axis_line)
        bottom_axis.setPen(self._theme.axis_line)
        left_axis.setStyle(tickFont=tick_font, tickTextOffset=10, autoExpandTextSpace=False)
        bottom_axis.setStyle(tickFont=tick_font, tickTextOffset=6, autoExpandTextSpace=True)
        left_axis.setWidth(88)
        bottom_axis.setHeight(54)
        self.plot_item.showAxis("bottom")

    def _reset_items(self) -> None:
        legend = self.plot_item.legend
        if legend is not None and legend.scene() is not None:
            legend.scene().removeItem(legend)
            self.plot_item.legend = None
        self.plot_item.clear()
        self._region_overlays = {}
        self._hover_series = {}
        self._configure_plot()
        self.plot_item.addLegend(offset=(16, 8), labelTextColor=self._theme.axis_text, labelTextSize="14pt", colCount=2)

    def show_preview(self, region_colors: dict[str, str] | None = None) -> None:
        self._reset_items()
        colors = _resolved_region_colors(region_colors)
        previews = [
            ("238U", "#69e2ff", 1.0, 0.0),
            ("206Pb", "#ff8a5b", 0.72, 0.8),
            ("207Pb", "#ffe56b", 0.52, 1.7),
            ("29Si", "#7ef7c9", 0.34, 2.4),
        ]
        n_spots = 4
        spot_duration = 80.0
        gap = 20.0
        period = spot_duration + gap
        total = n_spots * period
        t_global = np.linspace(0.0, total, n_spots * 400)
        t_local = t_global % period
        in_spot = (t_local < spot_duration).astype(float)
        rise = 1.0 / (1.0 + np.exp(-(t_local - 43.0) / 0.8))
        fall = 1.0 / (1.0 + np.exp((t_local - 70.0) / 2.0))
        plateau = 1450.0 * rise * fall * in_spot

        preview_stats: np.ndarray | None = None
        for name, color, scale, phase in previews:
            baseline = 480.0 + 18.0 * np.sin(t_global / 2.7 + phase) + 10.0 * np.cos(t_global / 6.5 + phase * 0.6)
            micro_ripple = 14.0 * np.sin(t_global / 1.9 + phase)
            y_values = (baseline + plateau + micro_ripple) * scale
            curve = self.plot_item.plot(t_global, y_values, pen=pg.mkPen(color, width=2.2), name=name)
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            self._hover_series[name] = (t_global, y_values)
            if name == "238U":
                preview_stats = y_values

        # Regions pinned to first spot only
        first_mask = t_global <= spot_duration
        t_first = t_global[first_mask]
        preview_frame = pd.DataFrame(
            {
                "Time": t_first,
                "238U": preview_stats[first_mask] if preview_stats is not None else np.zeros_like(t_first),
            }
        )
        self._add_region("preview_baseline", (18.0, 42.0), colors["baseline"], 44, movable=False, frame=preview_frame, stats_channel="238U")
        self._add_region("preview_plateau", (47.0, 62.0), colors["plateau"], 52, movable=False, frame=preview_frame, stats_channel="238U")
        self.set_x_window((10.0, 80.0))  # zoom to first spot; user can pan to others
        self.fit_y_to_visible_data()

    def show_spot(
        self,
        spot_data: pd.DataFrame,
        channels: list[str],
        channel_colors: dict[str, str],
        regions: dict[str, tuple[float, float] | None],
        *,
        editable_regions: bool = False,
        y_label: str = "Counts / cps",
        stats_channel: str | None = None,
        region_colors: dict[str, str] | None = None,
    ) -> None:
        if spot_data.empty:
            self.show_preview()
            return
        if not channels:
            self.show_empty("Check one or more channels to display traces.", y_label=y_label)
            return

        self._reset_items()
        label_style = {"color": self._theme.axis_text, "font-size": "15px", "font-weight": "600"}
        self.plot_item.setLabel("left", y_label, **label_style)
        x_values = spot_data["Time"].to_numpy(dtype=float)
        y_min = None
        y_max = None
        region_channel = stats_channel or channels[0]
        colors = _resolved_region_colors(region_colors)

        for channel in channels:
            if channel not in spot_data:
                continue
            y_values = spot_data[channel].to_numpy(dtype=float)
            color = channel_colors.get(channel, "#69e2ff")
            curve = self.plot_item.plot(x_values, y_values, pen=pg.mkPen(color, width=1.8), name=channel)
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            self._hover_series[channel] = (x_values, y_values)
            finite_values = y_values[np.isfinite(y_values)]
            if finite_values.size:
                current_min = float(np.min(finite_values))
                current_max = float(np.max(finite_values))
                y_min = current_min if y_min is None else min(y_min, current_min)
                y_max = current_max if y_max is None else max(y_max, current_max)

        if regions.get("baseline"):
            self._add_region("baseline", regions["baseline"], colors["baseline"], 44, movable=editable_regions, frame=spot_data, stats_channel=region_channel)
        if regions.get("plateau"):
            self._add_region("plateau", regions["plateau"], colors["plateau"], 52, movable=editable_regions, frame=spot_data, stats_channel=region_channel)

        self.setXRange(float(np.min(x_values)), float(np.max(x_values)), padding=0.01)
        if y_min is not None and y_max is not None:
            lower = y_min - max(1.0, abs(y_min) * 0.08)
            upper = y_max + max(1.0, abs(y_max) * 0.08)
            self.setYRange(lower, upper, padding=0.02)

    def show_series(
        self,
        frame: pd.DataFrame,
        series_names: list[str],
        series_colors: dict[str, str],
        *,
        y_label: str,
        line_style: str = "line",
    ) -> None:
        if frame.empty:
            self.show_preview()
            return
        if not series_names:
            self.show_empty("Check one or more channels to display traces.", y_label=y_label)
            return

        self._reset_items()
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
            curve = self.plot_item.plot(
                x_values,
                y_values,
                pen=pg.mkPen(color, width=2.0),
                name=name,
                symbol=symbol,
                symbolSize=5 if symbol else None,
                symbolBrush=color if symbol else None,
                symbolPen=pg.mkPen(color, width=1) if symbol else None,
            )
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            self._hover_series[name] = (x_values, y_values)
            current_min = float(np.min(finite_values))
            current_max = float(np.max(finite_values))
            y_min = current_min if y_min is None else min(y_min, current_min)
            y_max = current_max if y_max is None else max(y_max, current_max)

        self.setXRange(float(np.min(x_values)), float(np.max(x_values)), padding=0.02)
        if y_min is not None and y_max is not None:
            lower = y_min - abs(y_min) * 0.08 if y_min < 0 else y_min * 0.92
            upper = y_max * 1.08 if y_max > 0 else 1.0
            self.setYRange(lower, upper, padding=0.02)

    def current_regions(self) -> dict[str, tuple[float, float] | None]:
        return {
            name: tuple(sorted(float(value) for value in region["region"].getRegion()))
            for name, region in self._region_overlays.items()
        }

    def show_empty(self, message: str, *, y_label: str = "Counts / cps") -> None:
        self._reset_items()
        label_style = {"color": self._theme.axis_text, "font-size": "15px", "font-weight": "600"}
        self.plot_item.setLabel("left", y_label, **label_style)
        self.plot_item.setLabel("bottom", "Acquisition Time", units="s", **label_style)
        text_item = pg.TextItem(
            html=f"<div style='color:{self._theme.muted}; font-size:13pt; font-weight:600'>{message}</div>",
            anchor=(0.5, 0.5),
        )
        text_item.setPos(0.5, 0.5)
        self.plot_item.addItem(text_item)
        self.setXRange(0.0, 1.0, padding=0.0)
        self.setYRange(0.0, 1.0, padding=0.0)

    def visible_x_range(self) -> tuple[float, float]:
        x_range, _y_range = self.plot_item.viewRange()
        return float(x_range[0]), float(x_range[1])

    def visible_y_range(self) -> tuple[float, float]:
        _x_range, y_range = self.plot_item.viewRange()
        return float(y_range[0]), float(y_range[1])

    def set_x_window(self, bounds: tuple[float, float]) -> None:
        start_value, end_value = sorted(float(value) for value in bounds)
        self._suspend_x_window_signal = True
        try:
            self.setXRange(start_value, end_value, padding=0.0)
        finally:
            self._suspend_x_window_signal = False

    def set_y_window(self, bounds: tuple[float, float]) -> None:
        lower, upper = sorted(float(value) for value in bounds)
        self.setYRange(lower, upper, padding=0.0)

    def fit_y_to_visible_data(self) -> None:
        if not self._hover_series:
            return
        x_start, x_end = self.visible_x_range()
        y_min: float | None = None
        y_max: float | None = None

        for x_values, y_values in self._hover_series.values():
            mask = (x_values >= x_start) & (x_values <= x_end) & np.isfinite(y_values)
            if not np.any(mask):
                continue
            local_values = y_values[mask]
            if local_values.size >= 8:
                local_min = float(np.quantile(local_values, 0.01))
                local_max = float(np.quantile(local_values, 0.995))
            else:
                local_min = float(np.min(local_values))
                local_max = float(np.max(local_values))
            y_min = local_min if y_min is None else min(y_min, local_min)
            y_max = local_max if y_max is None else max(y_max, local_max)

        if y_min is None or y_max is None:
            return
        span = max(y_max - y_min, 1.0)
        lower = y_min - span * 0.03
        if y_min >= 0.0 and lower < span * 0.02:
            lower = 0.0
        upper = y_max + span * 0.10
        self.setYRange(lower, upper, padding=0.0)

    def set_zoom_mode(self, mode: str) -> None:
        self._zoom_mode = mode if mode in {"xy", "x", "y"} else "xy"
        self._apply_zoom_mode()

    def zoom_mode(self) -> str:
        return self._zoom_mode

    def set_interaction_mode(self, mode: str) -> None:
        self._interaction_mode = mode if mode in {"pan", "zoom"} else "zoom"
        self._apply_interaction_mode()

    def interaction_mode(self) -> str:
        return self._interaction_mode

    def _apply_interaction_mode(self) -> None:
        if self._interaction_mode == "pan":
            self.plot_item.vb.setMouseMode(pg.ViewBox.PanMode)
        else:
            self.plot_item.vb.setMouseMode(pg.ViewBox.RectMode)

    def _apply_zoom_mode(self) -> None:
        if self._zoom_mode == "x":
            self.setMouseEnabled(x=True, y=False)
        elif self._zoom_mode == "y":
            self.setMouseEnabled(x=False, y=True)
        else:
            self.setMouseEnabled(x=True, y=True)

    def set_sigma_level(self, sigma_level: int) -> None:
        self._sigma_level = 1 if sigma_level <= 1 else 2
        for region_name in list(self._region_overlays):
            self._update_region_overlay(region_name)

    def sigma_level(self) -> int:
        return self._sigma_level

    def _add_region(
        self,
        name: str,
        bounds: tuple[float, float],
        color: str,
        alpha: int,
        *,
        movable: bool,
        frame: pd.DataFrame,
        stats_channel: str | None,
    ) -> None:
        base_color = pg.mkColor(color)
        region = pg.LinearRegionItem(values=sorted(bounds), movable=movable, brush=pg.mkBrush(0, 0, 0, 0))
        transparent_pen = pg.mkPen(255, 255, 255, 0)
        for line in region.lines:
            line.setPen(transparent_pen)
            line.setHoverPen(transparent_pen)
        region.setZValue(6)
        mean_line = pg.PlotDataItem(pen=pg.mkPen(color, width=2.5))
        upper_line = pg.PlotDataItem(pen=pg.mkPen(base_color.red(), base_color.green(), base_color.blue(), max(120, alpha * 4), width=1.3))
        lower_line = pg.PlotDataItem(pen=pg.mkPen(base_color.red(), base_color.green(), base_color.blue(), max(120, alpha * 4), width=1.3))
        fill = pg.FillBetweenItem(upper_line, lower_line, brush=pg.mkBrush(base_color.red(), base_color.green(), base_color.blue(), alpha))
        fill.setZValue(3)
        mean_line.setZValue(4)
        upper_line.setZValue(3)
        lower_line.setZValue(3)

        self.plot_item.addItem(region)
        self.plot_item.addItem(fill)
        self.plot_item.addItem(upper_line)
        self.plot_item.addItem(lower_line)
        self.plot_item.addItem(mean_line)

        overlay_frame = frame[["Time", stats_channel]].dropna().copy() if stats_channel and stats_channel in frame else frame[["Time"]].copy()
        self._region_overlays[name] = {
            "region": region,
            "mean_line": mean_line,
            "upper_line": upper_line,
            "lower_line": lower_line,
            "frame": overlay_frame,
            "channel": stats_channel,
        }
        self._update_region_overlay(name)

        if movable:
            region.sigRegionChanged.connect(lambda _item, region_name=name: self._update_region_overlay(region_name))
            region.sigRegionChangeFinished.connect(lambda _item, region_name=name: self._emit_region_change(region_name))

    def _update_region_overlay(self, region_name: str) -> None:
        overlay = self._region_overlays.get(region_name)
        if overlay is None:
            return
        start_value, end_value = sorted(float(value) for value in overlay["region"].getRegion())
        frame = overlay["frame"]
        stats_channel = overlay["channel"]
        if frame.empty or stats_channel is None or stats_channel not in frame:
            return
        subset = frame[(frame["Time"] >= start_value) & (frame["Time"] <= end_value)]
        if subset.empty:
            return
        y_values = subset[stats_channel].to_numpy(dtype=float)
        finite_values = y_values[np.isfinite(y_values)]
        if finite_values.size == 0:
            return
        mean_value = float(np.mean(finite_values))
        std_value = float(np.std(finite_values, ddof=1)) if finite_values.size > 1 else 0.0
        spread = self._sigma_level * std_value
        x_bounds = np.array([start_value, end_value], dtype=float)

        overlay["mean_line"].setData(x_bounds, np.array([mean_value, mean_value], dtype=float))
        overlay["upper_line"].setData(x_bounds, np.array([mean_value + spread, mean_value + spread], dtype=float))
        overlay["lower_line"].setData(x_bounds, np.array([mean_value - spread, mean_value - spread], dtype=float))
        overlay["mean"] = mean_value
        overlay["std"] = std_value

    def _region_stats_text(self, x_value: float) -> list[str]:
        texts: list[str] = []
        for region_name, overlay in self._region_overlays.items():
            start_value, end_value = sorted(float(value) for value in overlay["region"].getRegion())
            if start_value <= x_value <= end_value and "mean" in overlay and "std" in overlay:
                mean_value = float(overlay["mean"])
                std_value = float(overlay["std"])
                sigma = self._sigma_level
                texts.append(f"{region_name}: mean {mean_value:,.2f}, {sigma}σ ±{sigma * std_value:,.2f}")
        return texts

    def _emit_region_change(self, region_name: str) -> None:
        overlay = self._region_overlays.get(region_name)
        if overlay is None:
            return
        bounds = tuple(sorted(float(value) for value in overlay["region"].getRegion()))
        self.regionChanged.emit(region_name, bounds)

    def _on_x_range_changed(self, _view_box: object, x_range: object) -> None:
        if self._suspend_x_window_signal:
            return
        self.xWindowChanged.emit(tuple(float(value) for value in x_range))

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        self.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)
        self.autoRange()
        event.accept()

    def _on_scene_mouse_moved(self, event: tuple[object]) -> None:
        if not event:
            return
        scene_position = event[0]
        if not self.sceneBoundingRect().contains(scene_position) or not self._hover_series:
            QtWidgets.QToolTip.hideText()
            return
        mouse_point = self.plot_item.vb.mapSceneToView(scene_position)
        x_value = float(mouse_point.x())
        tooltip_lines = [f"Time: {x_value:,.2f} s"]

        for name, (x_values, y_values) in list(self._hover_series.items())[:6]:
            if len(x_values) == 0:
                continue
            index = int(np.clip(np.searchsorted(x_values, x_value), 0, len(x_values) - 1))
            if index > 0 and abs(x_values[index - 1] - x_value) < abs(x_values[index] - x_value):
                index -= 1
            tooltip_lines.append(f"{name}: {float(y_values[index]):,.2f}")

        tooltip_lines.extend(self._region_stats_text(x_value))
        QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), "\n".join(tooltip_lines), self)


class SessionOverviewWidget(pg.PlotWidget):
    windowChanged = QtCore.Signal(tuple)

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(140)
        self.plot_item = self.getPlotItem()
        self._theme = ThemeSpec(
            name="dark",
            window="#07111c",
            window_alt="#091a2b",
            surface="#0d1c2d",
            surface_soft="#11263a",
            input_bg="#091523",
            input_border="#2a4159",
            text="#eaf2ff",
            muted="#90a6c1",
            accent="#69b4ff",
            accent_soft="rgba(105, 180, 255, 0.12)",
            accent_hover="rgba(105, 180, 255, 0.20)",
            accent_strong="#8bd7ff",
            success="#7ef7c9",
            success_soft="rgba(126, 247, 201, 0.12)",
            plot_background="#06111d",
            axis_text="#edf4ff",
            axis_line="#6d8298",
            grid_alpha=0.13,
        )
        self._window_region: pg.LinearRegionItem | None = None
        self._sync_guard = False
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        self._configure_plot()
        self.show_preview()

    def apply_theme(self, theme: ThemeSpec) -> None:
        self._theme = theme
        self._configure_plot()
        if self._window_region is not None:
            self._window_region.setBrush(pg.mkBrush(31, 99, 207, 32 if theme.name == "light" else 56))
            for line in self._window_region.lines:
                line.setPen(pg.mkPen(theme.accent, width=1.2))

    def _configure_plot(self) -> None:
        self.setBackground(self._theme.plot_background)
        self.showGrid(x=True, y=False, alpha=self._theme.grid_alpha * 0.9)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=False, y=False)
        self.plot_item.hideButtons()
        self.plot_item.setContentsMargins(16, 2, 12, 10)
        label_style = {"color": self._theme.axis_text, "font-size": "12px", "font-weight": "600"}
        self.plot_item.setLabel("bottom", "Session time", units="s", **label_style)
        self.plot_item.setLabel("left", "Counts", **label_style)
        tick_font = QtGui.QFont()
        tick_font.setPointSizeF(9.5)
        left_axis = self.plot_item.getAxis("left")
        bottom_axis = self.plot_item.getAxis("bottom")
        left_axis.setTextPen(self._theme.axis_text)
        bottom_axis.setTextPen(self._theme.axis_text)
        left_axis.setPen(self._theme.axis_line)
        bottom_axis.setPen(self._theme.axis_line)
        left_axis.setStyle(tickFont=tick_font, tickTextOffset=8, autoExpandTextSpace=False)
        bottom_axis.setStyle(tickFont=tick_font, tickTextOffset=4, autoExpandTextSpace=True)
        left_axis.setWidth(68)
        bottom_axis.setHeight(44)

    def _reset_items(self) -> None:
        self.plot_item.clear()
        self._window_region = None
        self._configure_plot()

    def show_preview(self) -> None:
        self._reset_items()
        previews = [
            ("238U", "#69e2ff", 1.0, 0.0),
            ("206Pb", "#ff8a5b", 0.72, 0.8),
            ("207Pb", "#ffe56b", 0.52, 1.7),
            ("29Si", "#7ef7c9", 0.34, 2.4),
        ]
        n_spots = 4
        spot_duration = 80.0
        gap = 20.0
        period = spot_duration + gap  # 100 s per spot
        total = n_spots * period
        t_global = np.linspace(0.0, total, n_spots * 200)
        t_local = t_global % period
        in_spot = (t_local < spot_duration).astype(float)
        rise = 1.0 / (1.0 + np.exp(-(t_local - 43.0) / 0.8))
        fall = 1.0 / (1.0 + np.exp((t_local - 70.0) / 2.0))
        plateau = 1450.0 * rise * fall * in_spot
        for _name, color, scale, phase in previews:
            baseline = 480.0 + 12.0 * np.sin(t_global / 12.0 + phase)
            y = (baseline + plateau) * scale
            curve = self.plot_item.plot(t_global, y, pen=pg.mkPen(color, width=1.0))
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
        self._ensure_window_region((10.0, 80.0))
        self.setXRange(0.0, total, padding=0.01)

    def show_series(self, frame: pd.DataFrame, channel: str, color: str, current_window: tuple[float, float]) -> None:
        self._reset_items()
        if frame.empty or channel not in frame or "Time" not in frame:
            self.show_preview()
            return
        x_values = frame["Time"].to_numpy(dtype=float)
        y_values = frame[channel].to_numpy(dtype=float)
        finite_mask = np.isfinite(x_values) & np.isfinite(y_values)
        if not finite_mask.any():
            self.show_preview()
            return
        x_values = x_values[finite_mask]
        y_values = y_values[finite_mask]
        curve = self.plot_item.plot(x_values, y_values, pen=pg.mkPen(color, width=1.4))
        curve.setDownsampling(auto=True, method="peak")
        curve.setClipToView(True)
        self._ensure_window_region(current_window)
        self.setXRange(float(np.min(x_values)), float(np.max(x_values)), padding=0.01)

    def set_window(self, bounds: tuple[float, float]) -> None:
        self._ensure_window_region(bounds)
        if self._window_region is None:
            return
        self._sync_guard = True
        try:
            self._window_region.setRegion(sorted(float(value) for value in bounds))
        finally:
            self._sync_guard = False

    def _ensure_window_region(self, bounds: tuple[float, float]) -> None:
        if self._window_region is None:
            self._window_region = pg.LinearRegionItem(
                values=sorted(float(value) for value in bounds),
                movable=True,
                brush=pg.mkBrush(31, 99, 207, 32 if self._theme.name == "light" else 56),
            )
            for line in self._window_region.lines:
                line.setPen(pg.mkPen(self._theme.accent, width=1.2))
            self._window_region.setZValue(5)
            self.plot_item.addItem(self._window_region)
            self._window_region.sigRegionChanged.connect(self._on_region_changed)
        else:
            self._window_region.setRegion(sorted(float(value) for value in bounds))

    def _on_region_changed(self) -> None:
        if self._sync_guard or self._window_region is None:
            return
        self.windowChanged.emit(tuple(float(value) for value in self._window_region.getRegion()))


def make_list_item(text: str, checked: bool = False) -> QtWidgets.QListWidgetItem:
    item = QtWidgets.QListWidgetItem(text)
    item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
    item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
    return item


def _resolved_region_colors(region_colors: dict[str, str] | None) -> dict[str, str]:
    colors = {
        "baseline": "#8a4c34",
        "plateau": "#2c5e87",
    }
    if region_colors:
        colors.update({key: value for key, value in region_colors.items() if key in colors and value})
    return colors
