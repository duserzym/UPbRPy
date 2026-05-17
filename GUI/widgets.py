from __future__ import annotations

import math

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
        layout.setSpacing(6)

        label_widget = QtWidgets.QLabel(label)
        label_widget.setObjectName("MetricLabel")
        self.value_widget = QtWidgets.QLabel(value)
        self.value_widget.setObjectName("MetricValue")
        swatch = QtWidgets.QLabel()
        swatch.setFixedHeight(4)
        swatch.setStyleSheet(f"background: {tone}; border-radius: 2px;")

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
        self.setMinimumHeight(420)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
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
        self._suspend_x_window_signal = False
        self._hover_proxy = pg.SignalProxy(self.scene().sigMouseMoved, rateLimit=60, slot=self._on_scene_mouse_moved)
        self.plot_item.vb.sigXRangeChanged.connect(self._on_x_range_changed)
        self._configure_plot()
        self.show_preview()

    def apply_theme(self, theme: ThemeSpec) -> None:
        self._theme = theme
        self.setBackground(theme.plot_background)
        self._configure_plot()

    def _configure_plot(self) -> None:
        self.setBackground(self._theme.plot_background)
        self.showGrid(x=True, y=True, alpha=self._theme.grid_alpha)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=True, y=True)
        self.setClipToView(True)
        self.plot_item.hideButtons()
        self.plot_item.vb.setMouseMode(pg.ViewBox.RectMode)
        self.plot_item.setContentsMargins(20, 16, 20, 18)
        self.plot_item.layout.setRowFixedHeight(3, 68)
        label_style = {"color": self._theme.axis_text, "font-size": "15px", "font-weight": "600"}
        self.plot_item.setLabel("bottom", "Acquisition Time", units="s", **label_style)
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
        bottom_axis.setHeight(62)

    def _reset_items(self) -> None:
        legend = self.plot_item.legend
        if legend is not None and legend.scene() is not None:
            legend.scene().removeItem(legend)
            self.plot_item.legend = None
        self.plot_item.clear()
        self._region_overlays = {}
        self._hover_series = {}
        self._configure_plot()
        self.plot_item.addLegend(offset=(16, 8), labelTextColor=self._theme.axis_text, labelTextSize="14pt")

    def show_preview(self) -> None:
        self._reset_items()
        time_axis = np.array([index * 0.4 for index in range(300)], dtype=float)
        previews = [
            ("238U", "#69e2ff", 1.0, 0.0),
            ("206Pb", "#ff8a5b", 0.72, 0.8),
            ("207Pb", "#ffe56b", 0.52, 1.7),
            ("29Si", "#7ef7c9", 0.34, 2.4),
        ]
        preview_stats: np.ndarray | None = None
        for name, color, scale, phase in previews:
            values = []
            for index, _time_value in enumerate(time_axis):
                baseline = 500 + 120 * math.sin(index / 18.0 + phase)
                peak = 1400 * math.exp(-((index - 145) ** 2) / 950.0)
                ripple = 60 * math.sin(index / 4.0 + phase)
                values.append((baseline + peak + ripple) * scale)
            y_values = np.array(values, dtype=float)
            curve = self.plot_item.plot(time_axis, y_values, pen=pg.mkPen(color, width=2.2), name=name)
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            self._hover_series[name] = (time_axis, y_values)
            if name == "238U":
                preview_stats = y_values

        preview_frame = pd.DataFrame(
            {
                "Time": time_axis,
                "238U": preview_stats if preview_stats is not None else np.zeros_like(time_axis),
            }
        )
        self._add_region("preview_baseline", (42.0, 54.0), "#ff8a5b", 18, movable=False, frame=preview_frame, stats_channel="238U")
        self._add_region("preview_plateau", (54.0, 76.0), "#69e2ff", 20, movable=False, frame=preview_frame, stats_channel="238U")
        self.setXRange(0, 120, padding=0)
        self.setYRange(0, 2400, padding=0.02)

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
    ) -> None:
        if spot_data.empty or not channels:
            self.show_preview()
            return

        self._reset_items()
        label_style = {"color": self._theme.axis_text, "font-size": "15px", "font-weight": "600"}
        self.plot_item.setLabel("left", y_label, **label_style)
        x_values = spot_data["Time"].to_numpy(dtype=float)
        y_min = None
        y_max = None
        region_channel = stats_channel or channels[0]

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
            self._add_region("baseline", regions["baseline"], "#ff8a5b", 18, movable=editable_regions, frame=spot_data, stats_channel=region_channel)
        if regions.get("plateau"):
            self._add_region("plateau", regions["plateau"], "#69e2ff", 20, movable=editable_regions, frame=spot_data, stats_channel=region_channel)

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
        if frame.empty or not series_names:
            self.show_preview()
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

    def visible_x_range(self) -> tuple[float, float]:
        x_range, _y_range = self.plot_item.viewRange()
        return float(x_range[0]), float(x_range[1])

    def set_x_window(self, bounds: tuple[float, float]) -> None:
        start_value, end_value = sorted(float(value) for value in bounds)
        self._suspend_x_window_signal = True
        try:
            self.setXRange(start_value, end_value, padding=0.0)
        finally:
            self._suspend_x_window_signal = False

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
        mean_line = pg.PlotDataItem(pen=pg.mkPen(color, width=2.2))
        upper_line = pg.PlotDataItem(pen=pg.mkPen(base_color.red(), base_color.green(), base_color.blue(), max(70, alpha * 4), width=0.8))
        lower_line = pg.PlotDataItem(pen=pg.mkPen(base_color.red(), base_color.green(), base_color.blue(), max(70, alpha * 4), width=0.8))
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
        self.plot_item.setContentsMargins(16, 4, 12, 10)
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
        x_values = np.linspace(0.0, 120.0, 500)
        y_values = 400.0 + 110.0 * np.sin(x_values / 9.0) + 60.0 * np.cos(x_values / 3.7)
        self.plot_item.plot(x_values, y_values, pen=pg.mkPen("#6177a2", width=1.4))
        self._ensure_window_region((20.0, 70.0))
        self.setXRange(0.0, 120.0, padding=0.0)

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
