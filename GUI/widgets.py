from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from GUI.services import CHANNEL_SECTIONS, _parse_channel_name
from GUI.theme import ThemeSpec


_MASS_RE = re.compile(r'^(\d+)\s*([A-Za-z]+)$')


def _parse_channel(name: str) -> tuple[str, str] | None:
    """Return (Element_Capitalized, mass_str) for '238U' / 'U238', else None."""
    return _parse_channel_name(name)


# -- SpotTree --------------------------------------------------------------

class SpotTree(QtWidgets.QTreeWidget):
    """QTreeWidget with F2-rename and Delete-key support for spot management."""

    deleteRequested = QtCore.Signal()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # type: ignore[override]
        key = event.key()
        if key == QtCore.Qt.Key.Key_F2:
            item = self.currentItem()
            if item and (item.flags() & QtCore.Qt.ItemFlag.ItemIsEditable):
                self.editItem(item, 0)
                return
        elif key in (QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace):
            self.deleteRequested.emit()
            return
        super().keyPressEvent(event)


class ComboPopupListView(QtWidgets.QListView):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectItems)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and index != self.currentIndex():
            self.setCurrentIndex(index)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            self.setCurrentIndex(index)
        super().mousePressEvent(event)


class StyledComboBox(QtWidgets.QComboBox):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        popup_view = ComboPopupListView(self)
        popup_view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        popup_view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        popup_view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        popup_view.setSpacing(0)
        popup_view.setUniformItemSizes(True)
        popup_view.viewport().setAutoFillBackground(False)
        self.setView(popup_view)

    def showPopup(self) -> None:
        super().showPopup()
        self._sync_popup_current_index()
        self._style_popup_container()

    def _sync_popup_current_index(self) -> None:
        view = self.view()
        model = self.model()
        current_row = self.currentIndex()
        if view is None or model is None or current_row < 0:
            return
        index = model.index(current_row, self.modelColumn(), self.rootModelIndex())
        if index.isValid():
            view.setCurrentIndex(index)
            view.scrollTo(index, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)

    def _style_popup_container(self) -> None:
        view = self.view()
        if view is None:
            return
        popup = view.window()
        if popup is None:
            return

        popup.setObjectName("ComboPopupContainer")
        popup.setContentsMargins(0, 0, 0, 0)
        popup.setAutoFillBackground(False)
        popup.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, False)
        popup.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)

        base_color = view.palette().base().color().name()
        border_color = view.palette().mid().color().name()
        popup.setStyleSheet(
            "QFrame#ComboPopupContainer {"
            f"background: {base_color};"
            f"border: 1px solid {border_color};"
            "border-radius: 12px;"
            "}"
        )

        rect = popup.rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(rect.adjusted(0, 0, -1, -1)), 12, 12)
        popup.setMask(QtGui.QRegion(path.toFillPolygon().toPolygon()))

class ChipButton(QtWidgets.QPushButton):
    """QPushButton that also emits doubleClicked via mouseDoubleClickEvent."""

    doubleClicked = QtCore.Signal()

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        self.doubleClicked.emit()
        event.accept()


class IsotopeGridWidget(QtWidgets.QWidget):
    """Compact element-grouped channel toggle grid.

    Channels are displayed in geochemical sections (Geochronology, Internal
    Standards, REE+Y, HFSE, ...).  Each element occupies one row with
    mass-number chips.

    Interaction
    -----------
    * Single click  → selects the chip as the active stats channel
                      (emits ``channelSelected``).
    * Double click  → toggles the channel on/off in the trace plot
                      (emits ``channelToggled``).
    * Right click   → opens a colour picker
                      (emits ``colorChangeRequested``).
    """

    channelToggled = QtCore.Signal(str, bool)   # (channel_name, active)
    channelSelected = QtCore.Signal(str)         # channel_name (stats channel)
    colorChangeRequested = QtCore.Signal(str)    # channel_name

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._channels: list[str] = []
        self._checked: dict[str, bool] = {}
        self._colors: dict[str, str] = {}
        self._buttons: dict[str, ChipButton] = {}
        self._selected_channel: str | None = None
        self._theme_name: str = "dark"
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(2)

    # -- Public API --------------------------------------------------------

    def set_channels(
        self,
        channels: list[str],
        checked: set[str],
        colors: dict[str, str],
        theme_name: str = "dark",
    ) -> None:
        self._channels = list(channels)
        self._checked = {ch: ch in checked for ch in channels}
        self._colors = dict(colors)
        self._theme_name = theme_name
        # Preserve selected channel if it is still present
        if self._selected_channel not in channels:
            self._selected_channel = None
        self._rebuild()

    def set_selected_channel(self, channel: str | None) -> None:
        """Highlight *channel* as the active stats channel."""
        old = self._selected_channel
        self._selected_channel = channel
        for ch in {old, channel}:
            if ch and ch in self._buttons:
                idx = self._buttons[ch].property("isotope_index") or 0
                self._apply_chip_style(self._buttons[ch], ch, isotope_index=idx)

    def set_color(self, channel: str, color: str) -> None:
        self._colors[channel] = color
        if channel in self._buttons:
            idx = self._buttons[channel].property("isotope_index") or 0
            self._apply_chip_style(self._buttons[channel], channel, isotope_index=idx)

    def set_theme(self, theme_name: str) -> None:
        """Re-apply chip styles after a theme switch."""
        self._theme_name = theme_name
        for ch, btn in self._buttons.items():
            idx = btn.property("isotope_index") or 0
            self._apply_chip_style(btn, ch, isotope_index=idx)

    def checked_channels(self) -> list[str]:
        return [ch for ch, state in self._checked.items() if state]

    # -- Internal ----------------------------------------------------------

    def _rebuild(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._buttons.clear()

        element_groups: dict[str, list[str]] = {}
        other: list[str] = []
        for ch in self._channels:
            parsed = _parse_channel(ch)
            if parsed:
                elem, _ = parsed
                element_groups.setdefault(elem, []).append(ch)
            else:
                other.append(ch)

        placed: set[str] = set()

        for section_label, section_elems in CHANNEL_SECTIONS:
            if section_elems is None:
                present = [e for e in sorted(element_groups) if e not in placed]
            else:
                present = [e for e in section_elems if e in element_groups and e not in placed]

            has_other = section_elems is None and bool(other)
            if not present and not has_other:
                continue

            self._layout.addWidget(self._make_section_header(section_label))

            for elem in present:
                self._layout.addWidget(self._make_element_row(elem, element_groups[elem]))
                placed.add(elem)

            if has_other:
                self._layout.addWidget(self._make_other_row(other))

        self._layout.addStretch(1)

    def _make_section_header(self, title: str) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(w)
        layout.setContentsMargins(0, 6, 0, 1)
        layout.setSpacing(6)
        lbl = QtWidgets.QLabel(title)
        lbl.setObjectName("IsotopeSectionLabel")
        line = QtWidgets.QWidget()
        line.setFixedHeight(1)
        line.setObjectName("IsotopeSectionLine")
        line.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        layout.addWidget(lbl)
        layout.addWidget(line, 1)
        return w

    def _make_element_row(self, elem: str, channels: list[str]) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        row_layout = QtWidgets.QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)
        elem_lbl = QtWidgets.QLabel(elem)
        elem_lbl.setObjectName("IsotopeElemLabel")
        elem_lbl.setFixedWidth(26)
        row_layout.addWidget(elem_lbl)
        sorted_channels = sorted(channels, key=lambda c: int(_parse_channel(c)[1]) if _parse_channel(c) else 0)
        for idx, ch in enumerate(sorted_channels):
            row_layout.addWidget(self._make_chip(ch, isotope_index=idx))
        row_layout.addStretch(1)
        return row

    def _make_other_row(self, channels: list[str]) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        row_layout = QtWidgets.QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)
        lbl = QtWidgets.QLabel("...")
        lbl.setObjectName("IsotopeElemLabel")
        lbl.setFixedWidth(26)
        row_layout.addWidget(lbl)
        for ch in channels:
            row_layout.addWidget(self._make_chip(ch))
        row_layout.addStretch(1)
        return row

    def _make_chip(self, channel: str, isotope_index: int = 0) -> ChipButton:
        parsed = _parse_channel(channel)
        label = f"{parsed[1]}\n{parsed[0]}" if parsed else channel
        btn = ChipButton(label)
        btn.setObjectName("IsotopeChip")
        btn.setFixedSize(44, 40)
        btn.setToolTip(channel)
        btn.setProperty("isotope_index", isotope_index)
        self._apply_chip_style(btn, channel, isotope_index=isotope_index)
        btn.clicked.connect(lambda _checked=False, ch=channel: self._on_chip_clicked(ch))
        btn.doubleClicked.connect(lambda ch=channel: self._on_chip_double_clicked(ch))
        btn.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(
            lambda _pos, ch=channel: self.colorChangeRequested.emit(ch)
        )
        self._buttons[channel] = btn
        return btn

    # -- Chip styling ------------------------------------------------------
    # Border styles cycle per isotope index so isotopes of the same element
    # are visually distinguishable even though they share the same hue.
    _BORDER_STYLES = ["solid", "dashed", "dotted", "double"]

    def _apply_chip_style(
        self,
        btn: ChipButton,
        channel: str,
        *,
        isotope_index: int = 0,
    ) -> None:
        color = self._colors.get(channel, "#6c7a8a")
        border_style = self._BORDER_STYLES[isotope_index % len(self._BORDER_STYLES)]
        is_active = self._checked.get(channel, True)
        is_selected = (channel == self._selected_channel)
        is_dark = (self._theme_name == "dark")

        if is_active:
            if is_selected:
                bg       = f"{color}55"
                border_w = "2.5px"
                border_c = color
                text_c   = "#ffffff" if is_dark else "#111111"
                fw       = "800"
            else:
                bg       = f"{color}28"
                border_w = "1.5px"
                border_c = f"{color}99"
                text_c   = "#dddddd" if is_dark else "#111111"
                fw       = "700"
        else:
            # Inactive (hidden from trace)
            bg       = "transparent"
            border_w = "1px"
            border_c = f"{color}44"
            text_c   = "#666666" if is_dark else "#aaaaaa"
            fw       = "600"

        btn.setStyleSheet(f"""
            QPushButton#IsotopeChip {{
                background: {bg};
                border: {border_w} {border_style} {border_c};
                border-radius: 8px;
                color: {text_c};
                font-size: 9px;
                font-weight: {fw};
                padding: 0px;
            }}
            QPushButton#IsotopeChip:hover {{
                background: {color}44;
                border: 2px {border_style} {color};
            }}
        """)

    # -- Interaction handlers ----------------------------------------------

    def _on_chip_clicked(self, channel: str) -> None:
        """Single click: select chip as the active stats channel."""
        old = self._selected_channel
        self._selected_channel = channel
        # Refresh styles for old and new selection
        for ch in {old, channel}:
            if ch and ch in self._buttons:
                idx = self._buttons[ch].property("isotope_index") or 0
                self._apply_chip_style(self._buttons[ch], ch, isotope_index=idx)
        self.channelSelected.emit(channel)

    def _on_chip_double_clicked(self, channel: str) -> None:
        """Double click: toggle channel on/off in the trace plot."""
        new_state = not self._checked.get(channel, True)
        self._checked[channel] = new_state
        if channel in self._buttons:
            idx = self._buttons[channel].property("isotope_index") or 0
            self._apply_chip_style(self._buttons[channel], channel, isotope_index=idx)
        self.channelToggled.emit(channel, new_state)


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


# ── Diagnostic plot palette ────────────────────────────────────────────────

_DIAG_SPOT_COLORS = [
    "#5590f5", "#f5c040", "#f07840", "#b864f0", "#40c870",
    "#d04848", "#40c8d8", "#f060a8", "#b8d840", "#f0a040",
    "#7090d8", "#d090f0",
]


def _make_diag_axis(plot_item: pg.PlotItem, theme: ThemeSpec) -> None:
    """Apply consistent axis styling to a diagnostic sub-plot."""
    tick_font = QtGui.QFont()
    tick_font.setPointSizeF(9.0)
    for axis_name in ("left", "bottom"):
        ax = plot_item.getAxis(axis_name)
        ax.setTextPen(theme.axis_text)
        ax.setPen(theme.axis_line)
        ax.setStyle(tickFont=tick_font, tickTextOffset=4)
    plot_item.showGrid(x=True, y=True, alpha=theme.grid_alpha)
    plot_item.setContentsMargins(4, 4, 4, 4)


class DownholeFractionationWidget(pg.GraphicsLayoutWidget):
    """Stacked primary-RM downhole fractionation plot with mean + ±1σ envelope.

    Data is supplied via :meth:`show_data`.  Accepts the dict returned by
    ``services.get_downhole_plot_data``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._theme: ThemeSpec | None = None
        self._data: dict | None = None
        self._plot_item: pg.PlotItem | None = None
        self._zoom_mode = "xy"
        self._interaction_mode = "zoom"
        self.setSizePolicy(
            QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                  QtWidgets.QSizePolicy.Policy.Expanding)
        )

    def apply_theme(self, theme: ThemeSpec) -> None:
        self._theme = theme
        self.setBackground(theme.plot_background)
        if self._data is not None:
            self._draw()

    def show_data(self, data: dict | None, theme: ThemeSpec) -> None:
        self._theme = theme
        self._data = data
        self.setBackground(theme.plot_background)
        self._draw()

    def _draw(self) -> None:
        self.clear()
        self._plot_item = None
        theme = self._theme
        data = self._data
        if theme is None:
            return
        if data is None:
            p = self.addPlot(0, 0)
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

        p = self.addPlot(row=0, col=0)
        self._plot_item = p
        _make_diag_axis(p, theme)
        p.setLabel("bottom", "Relative time (s)", color=theme.axis_text, size="9px")
        p.setLabel("left", "Fractionation factor", color=theme.axis_text, size="9px")
        p.setTitle(
            f"{ratio_name}  downhole fractionation — {primary_standard}",
            color=theme.axis_text, size="10pt",
        )

        # Individual spot traces (thin, semi-transparent)
        for i, (rel_t, r) in enumerate(zip(data["spot_times"], data["spot_ratios"])):
            with np.errstate(divide="ignore", invalid="ignore"):
                frac = np.where(r > 0, data["known_ratio"] / r, np.nan)
            color = _DIAG_SPOT_COLORS[i % len(_DIAG_SPOT_COLORS)]
            c = pg.mkColor(color)
            c.setAlpha(120)
            p.plot(rel_t, frac, pen=pg.mkPen(c, width=1.2))

        # ±1σ envelope (fill between dashed boundary curves)
        upper = mean_frac + std_frac
        lower = mean_frac - std_frac
        fill_brush = pg.mkColor("#888888")
        fill_brush.setAlpha(30)
        upper_curve = p.plot(unified_time, upper,
                             pen=pg.mkPen("#888888", width=1, style=QtCore.Qt.PenStyle.DashLine))
        lower_curve = p.plot(unified_time, lower,
                             pen=pg.mkPen("#888888", width=1, style=QtCore.Qt.PenStyle.DashLine))
        fill = pg.FillBetweenItem(upper_curve, lower_curve, brush=fill_brush)
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

    def _apply_zoom_mode(self) -> None:
        if self._plot_item is None:
            return
        if self._zoom_mode == "x":
            self._plot_item.vb.setMouseEnabled(x=True, y=False)
        elif self._zoom_mode == "y":
            self._plot_item.vb.setMouseEnabled(x=False, y=True)
        else:
            self._plot_item.vb.setMouseEnabled(x=True, y=True)

    def _apply_interaction_mode(self) -> None:
        if self._plot_item is None:
            return
        if self._interaction_mode == "pan":
            self._plot_item.vb.setMouseMode(pg.ViewBox.PanMode)
        else:
            self._plot_item.vb.setMouseMode(pg.ViewBox.RectMode)


class SessionFractionationWidget(pg.GraphicsLayoutWidget):
    """Session-wide fractionation: spot-mean factors, polynomial fit + ±1σ envelope.

    Data is supplied via :meth:`show_data`.  Accepts the dict returned by
    ``services.get_session_plot_data``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._theme: ThemeSpec | None = None
        self._data: dict | None = None
        self._plot_item: pg.PlotItem | None = None
        self._zoom_mode = "xy"
        self._interaction_mode = "zoom"
        self.setSizePolicy(
            QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                  QtWidgets.QSizePolicy.Policy.Expanding)
        )

    def apply_theme(self, theme: ThemeSpec) -> None:
        self._theme = theme
        self.setBackground(theme.plot_background)
        if self._data is not None:
            self._draw()

    def show_data(self, data: dict | None, theme: ThemeSpec) -> None:
        self._theme = theme
        self._data = data
        self.setBackground(theme.plot_background)
        self._draw()

    def _draw(self) -> None:
        self.clear()
        self._plot_item = None
        theme = self._theme
        data = self._data
        if theme is None:
            return
        if data is None:
            p = self.addPlot(0, 0)
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

        p = self.addPlot(row=0, col=0)
        self._plot_item = p
        _make_diag_axis(p, theme)
        p.setLabel("bottom", "Session time (s)", color=theme.axis_text, size="9px")
        p.setLabel("left", "Fractionation factor", color=theme.axis_text, size="9px")
        p.setTitle(
            f"{ratio_name}  session fractionation — {primary_standard}",
            color=theme.axis_text, size="10pt",
        )

        # ±1σ polynomial fit envelope
        fill_brush = pg.mkColor("#888888")
        fill_brush.setAlpha(30)
        upper_curve = p.plot(fit_times, fit_vals + fit_std,
                             pen=pg.mkPen("#888888", width=1, style=QtCore.Qt.PenStyle.DashLine))
        lower_curve = p.plot(fit_times, fit_vals - fit_std,
                             pen=pg.mkPen("#888888", width=1, style=QtCore.Qt.PenStyle.DashLine))
        fill = pg.FillBetweenItem(upper_curve, lower_curve, brush=fill_brush)
        p.addItem(fill)

        # Polynomial fit (red)
        p.plot(fit_times, fit_vals, pen=pg.mkPen("#e05050", width=2))

        # Spot-mean scatter + error bars
        accent = theme.accent
        err = pg.ErrorBarItem(x=t_arr, y=f_arr, height=2 * e_arr,
                              beam=0, pen=pg.mkPen(accent, width=1.5))
        p.addItem(err)
        scatter = pg.ScatterPlotItem(x=t_arr, y=f_arr,
                                     symbol="o", size=8,
                                     pen=pg.mkPen(accent, width=1.5),
                                     brush=pg.mkBrush(accent))
        p.addItem(scatter)

        # Reference line at 1.0
        p.addLine(y=1.0, pen=pg.mkPen(theme.muted, width=1,
                                       style=QtCore.Qt.PenStyle.DotLine))
        self._apply_zoom_mode()
        self._apply_interaction_mode()

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

    def _apply_zoom_mode(self) -> None:
        if self._plot_item is None:
            return
        if self._zoom_mode == "x":
            self._plot_item.vb.setMouseEnabled(x=True, y=False)
        elif self._zoom_mode == "y":
            self._plot_item.vb.setMouseEnabled(x=False, y=True)
        else:
            self._plot_item.vb.setMouseEnabled(x=True, y=True)

    def _apply_interaction_mode(self) -> None:
        if self._plot_item is None:
            return
        if self._interaction_mode == "pan":
            self._plot_item.vb.setMouseMode(pg.ViewBox.PanMode)
        else:
            self._plot_item.vb.setMouseMode(pg.ViewBox.RectMode)


class CalibrationSlopesWidget(pg.GraphicsLayoutWidget):
    """Grid of origin-anchored calibration slope plots (cps vs. known ppm).

    Data is supplied via :meth:`show_data`.  Accepts the dict returned by
    ``services.get_calibration_slope_data``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._theme: ThemeSpec | None = None
        self._data: dict | None = None
        self._scatter_for_line: dict[pg.PlotDataItem, pg.ScatterPlotItem] = {}
        self._plot_item: pg.PlotItem | None = None
        self._zoom_mode = "xy"
        self._interaction_mode = "zoom"
        self.setSizePolicy(
            QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                  QtWidgets.QSizePolicy.Policy.Expanding)
        )

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        # Rebuild legend column layout when width changes.
        if hasattr(self, "_theme") and hasattr(self, "_data") and self._theme is not None and self._data is not None:
            self._draw()

    def apply_theme(self, theme: ThemeSpec) -> None:
        self._theme = theme
        self.setBackground(theme.plot_background)
        if self._data is not None:
            self._draw()

    def show_data(self, data: dict | None, theme: ThemeSpec) -> None:
        self._theme = theme
        self._data = data
        self.setBackground(theme.plot_background)
        self._draw()

    def _draw(self) -> None:
        self.clear()
        self._scatter_for_line = {}
        self._plot_item = None
        theme = self._theme
        data = self._data
        if theme is None:
            return
        if data is None or not data.get("channels"):
            p = self.addPlot(0, 0)
            self._plot_item = p
            lbl = pg.TextItem("No calibration data available.\nRun the concentration calibration step first.",
                              color=theme.muted, anchor=(0.5, 0.5))
            lbl.setFont(QtGui.QFont("", 11))
            p.addItem(lbl)
            lbl.setPos(0.5, 0.5)
            return

        channels = data["channels"]
        p = self.addPlot(row=0, col=0)
        self._plot_item = p
        _make_diag_axis(p, theme)
        p.setTitle("Calibration slopes by element", color=theme.axis_text, size="10pt")
        p.setLabel("left", "cps", color=theme.axis_text, size="9px")
        p.setLabel("bottom", "ppm", color=theme.axis_text, size="9px")
        legend = p.addLegend(offset=(12, 12), labelTextColor=theme.axis_text, labelTextSize="9pt")
        if legend is not None:
            legend.setColumnCount(self._legend_column_count(len(channels)))

        global_xmax = 0.0
        for channel in channels:
            ch_data = data["channel_data"][channel]
            ppm = np.asarray(ch_data["ppm"], dtype=float)
            if ppm.size:
                global_xmax = max(global_xmax, float(np.nanmax(ppm)))
        if global_xmax <= 0:
            global_xmax = 1.0
        line_xmax = global_xmax * 1.05

        for idx, channel in enumerate(channels):
            ch_data = data["channel_data"][channel]
            ppm = np.asarray(ch_data["ppm"], dtype=float)
            cps = np.asarray(ch_data["cps"], dtype=float)
            slope = float(ch_data["slope"])
            color = _DIAG_SPOT_COLORS[idx % len(_DIAG_SPOT_COLORS)]

            # Scatter points for calibration standards
            scatter = pg.ScatterPlotItem(
                x=ppm,
                y=cps,
                symbol="o",
                size=7,
                pen=pg.mkPen(color, width=1.2),
                brush=pg.mkBrush(color),
            )
            p.addItem(scatter)

            # Dashed origin-anchored slope line. Keep this as the legend item;
            # clicking the legend sample will toggle both this line and its scatter.
            # Notebook calibration slope is ppm/cps. In this plot x=ppm, y=cps,
            # so the physically consistent origin-anchored line is y = x / slope.
            if np.isfinite(slope) and slope > 0:
                line_item = p.plot(
                    [0.0, line_xmax],
                    [0.0, line_xmax / slope],
                    pen=pg.mkPen(color, width=2.0, style=QtCore.Qt.PenStyle.DashLine),
                    name=channel,
                )
            else:
                line_item = p.plot(
                    [0.0],
                    [0.0],
                    pen=pg.mkPen(color, width=2.0, style=QtCore.Qt.PenStyle.DashLine),
                    name=channel,
                )
            self._scatter_for_line[line_item] = scatter

        if legend is not None:
            legend.sigSampleClicked.connect(self._on_legend_sample_clicked)
        self._apply_zoom_mode()
        self._apply_interaction_mode()

    def _on_legend_sample_clicked(self, line_item: object) -> None:
        if not isinstance(line_item, pg.PlotDataItem):
            return
        scatter = self._scatter_for_line.get(line_item)
        if scatter is not None:
            scatter.setVisible(line_item.isVisible())

    def _legend_column_count(self, n_channels: int) -> int:
        if n_channels <= 0:
            return 1
        # Width-based estimate (roughly one legend column per ~220 px).
        width_cap = max(1, int(self.width() / 220))
        # Row-based estimate to avoid very tall legends with many channels.
        # Keep around <=12 rows when possible.
        row_based = max(1, (n_channels + 11) // 12)
        return min(n_channels, max(width_cap, row_based))

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

    def _apply_zoom_mode(self) -> None:
        if self._plot_item is None:
            return
        if self._zoom_mode == "x":
            self._plot_item.vb.setMouseEnabled(x=True, y=False)
        elif self._zoom_mode == "y":
            self._plot_item.vb.setMouseEnabled(x=False, y=True)
        else:
            self._plot_item.vb.setMouseEnabled(x=True, y=True)

    def _apply_interaction_mode(self) -> None:
        if self._plot_item is None:
            return
        if self._interaction_mode == "pan":
            self._plot_item.vb.setMouseMode(pg.ViewBox.PanMode)
        else:
            self._plot_item.vb.setMouseMode(pg.ViewBox.RectMode)


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

        # Floating spot-name label in the top-right corner
        self._spot_name_label = QtWidgets.QLabel("", self)
        self._spot_name_label.setObjectName("SpotOverlayLabel")
        self._spot_name_label.setStyleSheet(
            "color: rgba(180,200,220,0.55); font-size: 13px; font-weight: 600;"
            " background: transparent; padding: 2px 6px;"
        )
        self._spot_name_label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self._spot_name_label.hide()

        self.show_preview()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "plot_item"):
            return
        self.plot_item.layout.setRowFixedHeight(3, 62)
        self.plot_item.getAxis("bottom").setHeight(54)
        self.plot_item.showAxis("bottom")
        self._reposition_spot_label()

    def _update_spot_label(self, spot_name: str | None) -> None:
        if not hasattr(self, "_spot_name_label"):
            return
        if spot_name:
            self._spot_name_label.setText(spot_name)
            self._spot_name_label.adjustSize()
            self._reposition_spot_label()
            self._spot_name_label.show()
        else:
            self._spot_name_label.hide()

    def _reposition_spot_label(self) -> None:
        if not hasattr(self, "_spot_name_label"):
            return
        lbl = self._spot_name_label
        lbl.adjustSize()
        margin = 14
        x = self.width() - lbl.width() - margin
        y = margin
        lbl.move(max(0, x), max(0, y))

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
        spot_name: str | None = None,
    ) -> None:
        if spot_data.empty:
            self.show_preview()
            self._update_spot_label(None)
            return
        if not channels:
            self.show_empty("Check one or more channels to display traces.", y_label=y_label)
            self._update_spot_label(None)
            return

        self._reset_items()
        self._update_spot_label(spot_name)
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
