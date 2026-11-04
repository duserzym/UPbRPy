from __future__ import annotations

import os
import re

import numpy as np
import pandas as pd

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

from PySide6 import QtCore, QtGui, QtWidgets

import pyqtgraph as pg

from GUI.services import CHANNEL_SECTIONS, _parse_channel_name
from GUI.theme import NeumorphicFrame, ThemeSpec, active_theme
from UPbRPy.isochron import tera_wasserburg_point, wetherill_point
from UPbRPy.ratios import (
    excel_plateau_inlier_mask,
    normalize_plateau_filter_method,
)


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
        self._expand_popup_to_contents()
        self._style_popup_container()

    def _expand_popup_to_contents(self) -> None:
        """Keep long scientific method names readable in narrow inspectors."""
        view = self.view()
        if view is None or self.count() == 0:
            return
        widest_text = max(
            self.fontMetrics().horizontalAdvance(self.itemText(index))
            for index in range(self.count())
        )
        popup_width = max(self.width(), widest_text + 54)
        view.setMinimumWidth(popup_width)
        popup = view.window()
        if popup is not None:
            popup.setMinimumWidth(popup_width)

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
    """Accessible periodic-table isotope tile with an explicit active marker."""

    doubleClicked = QtCore.Signal()

    def __init__(
        self,
        element: str,
        mass: str,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        """Create a tile whose mass and element use separate typography."""
        super().__init__(parent)
        self.element = element
        self.mass = mass
        self.tile_color = QtGui.QColor("#6177a2")
        self.tile_active = True
        self.tile_selected = False
        self.dark_theme = True
        self.hovered = False
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"{mass}{element} isotope channel")

    def set_visual_state(
        self,
        color: str,
        *,
        active: bool,
        selected: bool,
        dark_theme: bool,
    ) -> None:
        """Update the tile without relying on hue as the only state cue."""
        self.tile_color = QtGui.QColor(color)
        self.tile_active = active
        self.tile_selected = selected
        self.dark_theme = dark_theme
        self.update()

    def enterEvent(self, event: QtCore.QEvent) -> None:
        self.hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self.hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        """Paint a high-contrast miniature periodic-table cell."""
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)

        if self.tile_active:
            background = QtGui.QColor(self.tile_color)
            background = (
                background.lighter(112)
                if self.hovered
                else background
            )
        else:
            background = QtGui.QColor(
                "#242a34" if self.dark_theme else "#edf0f4"
            )

        border = QtGui.QColor(
            "#ffffff" if self.dark_theme else "#111827"
        )
        if not self.tile_selected:
            border = (
                self.tile_color.darker(145)
                if self.tile_active
                else QtGui.QColor(
                    "#667085" if self.dark_theme else "#98a2b3"
                )
            )
        pen = QtGui.QPen(border, 2.6 if self.tile_selected else 1.2)
        if not self.tile_active:
            pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 8, 8)

        luminance = (
            0.2126 * background.redF()
            + 0.7152 * background.greenF()
            + 0.0722 * background.blueF()
        )
        foreground = QtGui.QColor(
            "#111318" if luminance > 0.58 else "#ffffff"
        )
        if not self.tile_active:
            foreground = QtGui.QColor(
                "#98a2b3" if self.dark_theme else "#667085"
            )
        painter.setPen(foreground)

        mass_font = QtGui.QFont(self.font())
        mass_font.setPointSizeF(max(7.0, self.height() * 0.17))
        mass_font.setWeight(QtGui.QFont.Weight.DemiBold)
        painter.setFont(mass_font)
        painter.drawText(
            QtCore.QRectF(rect.left() + 5, rect.top() + 2, 22, 12),
            QtCore.Qt.AlignmentFlag.AlignLeft,
            self.mass,
        )

        element_font = QtGui.QFont(self.font())
        element_font.setPointSizeF(max(10.0, self.height() * 0.28))
        element_font.setWeight(QtGui.QFont.Weight.Bold)
        painter.setFont(element_font)
        painter.drawText(
            rect.adjusted(0, 8, 0, 0),
            QtCore.Qt.AlignmentFlag.AlignCenter,
            self.element,
        )

        marker_center = QtCore.QPointF(rect.right() - 6, rect.bottom() - 6)
        marker_pen = QtGui.QPen(foreground, 1.4)
        painter.setPen(marker_pen)
        painter.setBrush(
            foreground if self.tile_active else QtCore.Qt.BrushStyle.NoBrush
        )
        painter.drawEllipse(marker_center, 2.6, 2.6)
        painter.end()

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
        element, mass = parsed if parsed else (channel, "")
        btn = ChipButton(element, mass)
        btn.setObjectName("IsotopeChip")
        # A slightly denser tile preserves the periodic-table visual language
        # while exposing substantially more elements in the inspector.
        btn.setFixedSize(44, 42)
        btn.setToolTip(
            f"{channel}\nClick: use for selection statistics\n"
            "Double-click: show or hide trace\nRight-click: choose color"
        )
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

    def _apply_chip_style(
        self,
        btn: ChipButton,
        channel: str,
        *,
        isotope_index: int = 0,
    ) -> None:
        color = self._colors.get(channel, "#6c7a8a")
        is_active = self._checked.get(channel, True)
        is_selected = (channel == self._selected_channel)
        is_dark = (self._theme_name == "dark")
        btn.set_visual_state(
            color,
            active=is_active,
            selected=is_selected,
            dark_theme=is_dark,
        )

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


class DataFrameTableModel(QtCore.QAbstractTableModel):
    """Memory-efficient, read-only Qt view of a pandas DataFrame."""

    def __init__(
        self,
        frame: pd.DataFrame | None = None,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.frame = pd.DataFrame() if frame is None else frame.copy()

    def set_frame(self, frame: pd.DataFrame | None) -> None:
        """Replace the displayed frame with one model reset notification."""
        self.beginResetModel()
        self.frame = pd.DataFrame() if frame is None else frame.copy()
        self.endResetModel()

    def rowCount(
        self,
        parent: QtCore.QModelIndex = QtCore.QModelIndex(),
    ) -> int:
        return 0 if parent.isValid() else len(self.frame)

    def columnCount(
        self,
        parent: QtCore.QModelIndex = QtCore.QModelIndex(),
    ) -> int:
        return 0 if parent.isValid() else len(self.frame.columns)

    def data(
        self,
        index: QtCore.QModelIndex,
        role: int = int(QtCore.Qt.ItemDataRole.DisplayRole),
    ) -> object:
        if not index.isValid():
            return None
        value = self.frame.iat[index.row(), index.column()]
        if role == int(QtCore.Qt.ItemDataRole.DisplayRole):
            if pd.isna(value):
                return ""
            if isinstance(value, (float, np.floating)):
                return f"{float(value):.7g}"
            return str(value)
        if role == int(QtCore.Qt.ItemDataRole.TextAlignmentRole):
            if isinstance(value, (int, float, np.integer, np.floating)):
                return int(
                    QtCore.Qt.AlignmentFlag.AlignRight
                    | QtCore.Qt.AlignmentFlag.AlignVCenter
                )
        if role == int(QtCore.Qt.ItemDataRole.UserRole):
            if pd.isna(value):
                return ""
            return value.item() if isinstance(value, np.generic) else value
        return None

    def headerData(
        self,
        section: int,
        orientation: QtCore.Qt.Orientation,
        role: int = int(QtCore.Qt.ItemDataRole.DisplayRole),
    ) -> object:
        if role != int(QtCore.Qt.ItemDataRole.DisplayRole):
            return None
        if orientation == QtCore.Qt.Orientation.Horizontal:
            return str(self.frame.columns[section])
        return str(section + 1)


class ConcordiaReviewWidget(pg.GraphicsLayoutWidget):
    """Lightweight native concordia review with two-sigma ratio ellipses.

    The widget consumes already reduced ratio columns.  It deliberately does
    not calculate ages or corrections, keeping the GUI as a view over the
    shared :mod:`UPbRPy` pipeline rather than creating a second reduction
    implementation.
    """

    def __init__(self) -> None:
        super().__init__()
        self._theme: ThemeSpec | None = None
        self._frame = pd.DataFrame()
        self._diagram = "wetherill"
        self._kind = "spot"
        self._include_calibration_uncertainty = True
        self._plot_item: pg.PlotItem | None = None
        self._zoom_mode = "xy"
        self._interaction_mode = "zoom"

    def apply_theme(self, theme: ThemeSpec) -> None:
        """Apply a theme and redraw the retained concordia data."""
        self._theme = theme
        self.setBackground(theme.plot_background)
        self._draw()

    def show_data(
        self,
        frame: pd.DataFrame,
        *,
        diagram: str,
        kind: str,
        include_calibration_uncertainty: bool,
        theme: ThemeSpec,
    ) -> None:
        """Display selected spot means or one spot's corrected sweeps."""
        self._frame = frame.copy()
        self._diagram = (
            "tera_wasserburg"
            if diagram == "tera_wasserburg"
            else "wetherill"
        )
        self._kind = "depth" if kind == "depth" else "spot"
        self._include_calibration_uncertainty = bool(
            include_calibration_uncertainty
        )
        self._theme = theme
        self.setBackground(theme.plot_background)
        self._draw()

    @staticmethod
    def uncertainty_column(
        frame: pd.DataFrame,
        value_column: str,
        include_calibration_uncertainty: bool,
    ) -> str | None:
        """Resolve a named two-SE branch, falling back to the legacy column."""
        branch = (
            "with_calibration"
            if include_calibration_uncertainty
            else "no_calibration"
        )
        for candidate in (
            f"{value_column}_2e_{branch}",
            f"{value_column}_2e",
        ):
            if candidate in frame.columns:
                return candidate
        return None

    @staticmethod
    def ellipse_coordinates(
        x: float,
        y: float,
        x_2e: float,
        y_2e: float,
        rho: float,
        *,
        points: int = 48,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return a two-standard-error covariance ellipse in ratio space."""
        if not all(np.isfinite([x, y, x_2e, y_2e, rho])):
            return np.array([]), np.array([])
        sx = abs(float(x_2e)) / 2.0
        sy = abs(float(y_2e)) / 2.0
        if sx == 0 or sy == 0:
            return np.array([]), np.array([])
        bounded_rho = float(np.clip(rho, -0.999, 0.999))
        covariance = np.array(
            [[sx * sx, bounded_rho * sx * sy],
             [bounded_rho * sx * sy, sy * sy]],
            dtype=float,
        )
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        eigenvalues = np.maximum(eigenvalues, 0.0)
        angle = np.linspace(0.0, 2.0 * np.pi, points)
        circle = np.vstack((np.cos(angle), np.sin(angle)))
        # The supplied columns are absolute 2SE values, so multiplying the
        # one-SE covariance axes by two returns the requested 2SE ellipse.
        offsets = eigenvectors @ np.diag(2.0 * np.sqrt(eigenvalues)) @ circle
        return x + offsets[0], y + offsets[1]

    def _ratio_columns(self) -> tuple[str, str, str | None, str | None]:
        prefix = "" if self._kind == "spot" else "_fractionation_corrected"
        if self._diagram == "wetherill":
            x_column = f"207Pb/235U{prefix}"
            y_column = f"206Pb/238U{prefix}"
        else:
            x_column = f"238U/206Pb{prefix}"
            y_column = f"207Pb/206Pb{prefix}"
        return (
            x_column,
            y_column,
            self.uncertainty_column(
                self._frame,
                x_column,
                self._include_calibration_uncertainty,
            ),
            self.uncertainty_column(
                self._frame,
                y_column,
                self._include_calibration_uncertainty,
            ),
        )

    def _draw(self) -> None:
        self.clear()
        self._plot_item = None
        theme = self._theme
        if theme is None:
            return
        plot = self.addPlot(0, 0)
        self._plot_item = plot
        _make_diag_axis(plot, theme)
        plot.setAspectLocked(False)
        tick_font = QtGui.QFont()
        tick_font.setPointSizeF(12.0)
        for axis_name in ("left", "bottom"):
            plot.getAxis(axis_name).setStyle(
                tickFont=tick_font,
                tickTextOffset=8,
            )

        if self._diagram == "wetherill":
            plot.setLabel(
                "bottom", "207Pb/235U", color=theme.axis_text, size="15px"
            )
            plot.setLabel(
                "left", "206Pb/238U", color=theme.axis_text, size="15px"
            )
            concordia_x, concordia_y = wetherill_point(
                np.linspace(1.0, 4500.0, 900)
            )
        else:
            plot.setLabel(
                "bottom", "238U/206Pb", color=theme.axis_text, size="15px"
            )
            plot.setLabel(
                "left", "207Pb/206Pb", color=theme.axis_text, size="15px"
            )
            concordia_x, concordia_y = tera_wasserburg_point(
                np.linspace(1.0, 4500.0, 900)
            )
        plot.setTitle(
            "Spot-mean concordia review"
            if self._kind == "spot"
            else "Sweep-wise depth-profile concordia review",
            color=theme.axis_text,
            size="13pt",
        )
        plot.plot(
            concordia_x,
            concordia_y,
            pen=pg.mkPen(theme.muted, width=1.4),
        )

        frame = self._frame
        x_column, y_column, sx_column, sy_column = self._ratio_columns()
        required = [x_column, y_column]
        if frame.empty or any(column not in frame.columns for column in required):
            message = pg.TextItem(
                "No compatible corrected ratios are available for this view.",
                color=theme.muted,
                anchor=(0.5, 0.5),
            )
            plot.addItem(message)
            message.setPos(0.5, 0.5)
            return

        x_values = pd.to_numeric(frame[x_column], errors="coerce").to_numpy()
        y_values = pd.to_numeric(frame[y_column], errors="coerce").to_numpy()
        if sx_column and sy_column:
            x_errors = pd.to_numeric(frame[sx_column], errors="coerce").to_numpy()
            y_errors = pd.to_numeric(frame[sy_column], errors="coerce").to_numpy()
        else:
            x_errors = np.full(len(frame), np.nan)
            y_errors = np.full(len(frame), np.nan)
        uncertainty_branch = (
            "with_calibration"
            if self._include_calibration_uncertainty
            else "no_calibration"
        )
        correlation_stem = (
            "err_corr" if self._diagram == "wetherill" else "tw_rho"
        )
        correlation_column = next(
            (
                candidate
                for candidate in (
                    f"{correlation_stem}_{uncertainty_branch}",
                    correlation_stem,
                )
                if candidate in frame.columns
            ),
            None,
        )
        rho_values = (
            pd.to_numeric(frame[correlation_column], errors="coerce")
            .fillna(0.0)
            .clip(-1.0, 1.0)
            .to_numpy()
            if correlation_column is not None
            else np.zeros(len(frame), dtype=float)
        )
        finite = np.isfinite(x_values) & np.isfinite(y_values)

        if self._kind == "depth" and "signal_relative_time" in frame.columns:
            times = pd.to_numeric(
                frame["signal_relative_time"], errors="coerce"
            ).to_numpy()
            finite_times = times[np.isfinite(times)]
            time_min = float(np.nanmin(finite_times)) if finite_times.size else 0.0
            time_span = (
                float(np.nanmax(finite_times) - time_min)
                if finite_times.size
                else 1.0
            )
            time_span = max(time_span, np.finfo(float).eps)
            normalized = np.clip((times - time_min) / time_span, 0.0, 1.0)
            brushes = [
                pg.intColor(int(value * 255), hues=256)
                if np.isfinite(value)
                else pg.mkColor(theme.muted)
                for value in normalized
            ]
        else:
            brushes = [
                pg.mkColor(_DIAG_SPOT_COLORS[index % len(_DIAG_SPOT_COLORS)])
                for index in range(len(frame))
            ]

        for index in np.flatnonzero(finite):
            ellipse_x, ellipse_y = self.ellipse_coordinates(
                float(x_values[index]),
                float(y_values[index]),
                float(x_errors[index]),
                float(y_errors[index]),
                float(rho_values[index]),
            )
            if ellipse_x.size:
                ellipse_color = QtGui.QColor(brushes[index])
                ellipse_color.setAlpha(155)
                plot.plot(
                    ellipse_x,
                    ellipse_y,
                    pen=pg.mkPen(ellipse_color, width=1.0),
                )
        plot.addItem(
            pg.ScatterPlotItem(
                x=x_values[finite],
                y=y_values[finite],
                symbol="o",
                size=6 if self._kind == "depth" else 8,
                pen=pg.mkPen(theme.axis_text, width=0.7),
                brush=[brushes[index] for index in np.flatnonzero(finite)],
            )
        )

        # Age labels remain sparse enough for a quick-look panel, while the
        # full 0--4.5 Ga curve remains available when users pan or zoom.
        for age in (500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500):
            if self._diagram == "wetherill":
                age_x, age_y = wetherill_point(age)
            else:
                age_x, age_y = tera_wasserburg_point(age)
            marker = pg.ScatterPlotItem(
                x=[float(age_x)], y=[float(age_y)], size=4,
                pen=pg.mkPen(theme.muted), brush=pg.mkBrush(theme.muted),
            )
            plot.addItem(marker)
            label = pg.TextItem(str(age), color=theme.muted, anchor=(0, 0.5))
            age_font = QtGui.QFont()
            age_font.setPointSizeF(10.5)
            label.setFont(age_font)
            plot.addItem(label)
            label.setPos(float(age_x), float(age_y))

        self._set_initial_range(
            plot,
            x_values[finite],
            y_values[finite],
            x_errors[finite],
            y_errors[finite],
            concordia_x,
            concordia_y,
        )
        self._apply_zoom_mode()
        self._apply_interaction_mode()

    def set_zoom_mode(self, mode: str) -> None:
        """Restrict mouse zooming to both axes or one selected axis."""
        self._zoom_mode = mode if mode in {"xy", "x", "y"} else "xy"
        self._apply_zoom_mode()

    def zoom_mode(self) -> str:
        """Return the current concordia zoom-axis mode."""
        return self._zoom_mode

    def set_interaction_mode(self, mode: str) -> None:
        """Choose rectangle zoom or panning for the concordia view."""
        self._interaction_mode = mode if mode in {"pan", "zoom"} else "zoom"
        self._apply_interaction_mode()

    def interaction_mode(self) -> str:
        """Return the current concordia mouse interaction."""
        return self._interaction_mode

    def _apply_zoom_mode(self) -> None:
        if self._plot_item is None:
            return
        self._plot_item.vb.setMouseEnabled(
            x=self._zoom_mode in {"xy", "x"},
            y=self._zoom_mode in {"xy", "y"},
        )

    def _apply_interaction_mode(self) -> None:
        if self._plot_item is None:
            return
        self._plot_item.vb.setMouseMode(
            pg.ViewBox.PanMode
            if self._interaction_mode == "pan"
            else pg.ViewBox.RectMode
        )

    def restore_default_view(self) -> None:
        """Recompute the useful data-plus-concordia initial range."""
        self._draw()

    @staticmethod
    def _set_initial_range(
        plot: pg.PlotItem,
        x_values: np.ndarray,
        y_values: np.ndarray,
        x_errors: np.ndarray,
        y_errors: np.ndarray,
        concordia_x: np.ndarray,
        concordia_y: np.ndarray,
    ) -> None:
        """Frame observations plus their nearest concordia neighborhood."""
        if not x_values.size:
            plot.autoRange()
            return
        finite_xe = np.where(np.isfinite(x_errors), np.abs(x_errors), 0.0)
        finite_ye = np.where(np.isfinite(y_errors), np.abs(y_errors), 0.0)
        x_low = float(np.min(x_values - finite_xe))
        x_high = float(np.max(x_values + finite_xe))
        y_low = float(np.min(y_values - finite_ye))
        y_high = float(np.max(y_values + finite_ye))

        x_scale = max(x_high - x_low, abs(np.nanmean(x_values)) * 0.15, 1e-6)
        y_scale = max(y_high - y_low, abs(np.nanmean(y_values)) * 0.15, 1e-6)
        center_x = float(np.nanmean(x_values))
        center_y = float(np.nanmean(y_values))
        distances = (
            ((concordia_x - center_x) / x_scale) ** 2
            + ((concordia_y - center_y) / y_scale) ** 2
        )
        nearest = int(np.nanargmin(distances))
        x_low = min(x_low, float(concordia_x[nearest]))
        x_high = max(x_high, float(concordia_x[nearest]))
        y_low = min(y_low, float(concordia_y[nearest]))
        y_high = max(y_high, float(concordia_y[nearest]))
        x_padding = max((x_high - x_low) * 0.12, 1e-6)
        y_padding = max((y_high - y_low) * 0.12, 1e-6)
        plot.setRange(
            xRange=(x_low - x_padding, x_high + x_padding),
            yRange=(max(0.0, y_low - y_padding), y_high + y_padding),
            padding=0.0,
        )


class ElidedLabel(QtWidgets.QLabel):
    """A one-line label that shortens its text with "…" instead of forcing
    its parent wider; the full text is the tooltip while it is shortened.

    Small screens (a 14-inch laptop) cannot give every header label its
    natural width, and a plain ``QLabel`` would either widen the window or
    be cut off mid-character.
    """

    def __init__(self, text: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._own_tooltip = ""
        self.setWordWrap(False)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        self._full_text = str(text)
        self._elide()

    def text(self) -> str:  # noqa: D401 - Qt API
        return self._full_text

    def setToolTip(self, tooltip: str) -> None:  # noqa: N802 - Qt API
        self._own_tooltip = tooltip
        super().setToolTip(tooltip)

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt API
        metrics = self.fontMetrics()
        margins = self.contentsMargins()
        return QtCore.QSize(
            metrics.horizontalAdvance(self._full_text) + margins.left() + margins.right() + 2,
            super().sizeHint().height(),
        )

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt API
        metrics = self.fontMetrics()
        return QtCore.QSize(
            metrics.horizontalAdvance("…") + 4,
            super().minimumSizeHint().height(),
        )

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange):
            self._elide()

    def _elide(self) -> None:
        margins = self.contentsMargins()
        width = max(0, self.width() - margins.left() - margins.right())
        shown = self.fontMetrics().elidedText(
            self._full_text, QtCore.Qt.TextElideMode.ElideRight, width
        )
        super().setText(shown)
        elided = shown != self._full_text
        super().setToolTip(self._full_text if elided and not self._own_tooltip else self._own_tooltip)


def scrolling_page(page: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    """Put a tab page in a frameless scroll area: it fills the tab when
    there is room and scrolls instead of imposing its minimum size."""
    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    scroll.setWidget(page)
    return scroll


def elide_tab_titles(tabs: QtWidgets.QTabWidget) -> None:
    """Shorten tab titles with "…" on narrow panels instead of showing the
    tab bar's scroll arrows; each title stays readable as its tooltip."""
    tabs.setElideMode(QtCore.Qt.TextElideMode.ElideRight)
    tabs.setUsesScrollButtons(False)
    for index in range(tabs.count()):
        if not tabs.tabToolTip(index):
            tabs.setTabToolTip(index, tabs.tabText(index))


class ResultsTableWidget(QtWidgets.QWidget):
    """Sortable tables plus quick concordia reviews of final products."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)
        self.summary_label = QtWidgets.QLabel(
            "Calculate final results to review preferred ages and flags."
        )
        self.summary_label.setObjectName("SecondaryText")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.tabs = QtWidgets.QTabWidget()
        self.results_table, self.results_model = self.create_table()
        self.secondary_table, self.secondary_model = self.create_table()
        self.depth_table, self.depth_model = self.create_table()
        self.tabs.addTab(self.results_table, "Final results")
        self.tabs.addTab(self.secondary_table, "Secondary standards")
        self.tabs.addTab(self.depth_table, "Depth profile")
        # The concordia pages scroll when the plot column is short, so a
        # laptop screen does not force every results page to their height.
        self.spot_concordia_page = scrolling_page(self.create_spot_concordia_page())
        self.depth_concordia_page = scrolling_page(self.create_depth_concordia_page())
        self.tabs.addTab(self.spot_concordia_page, "Spot concordia")
        self.tabs.addTab(self.depth_concordia_page, "Depth concordia")
        elide_tab_titles(self.tabs)
        layout.addWidget(self.tabs, 1)

        self._theme: ThemeSpec | None = None
        self._spot_frame = pd.DataFrame()
        self._depth_frame = pd.DataFrame()
        self._include_calibration_uncertainty = True

    def create_diagram_buttons(
        self,
        callback,
    ) -> tuple[QtWidgets.QWidget, dict[str, QtWidgets.QToolButton]]:
        """Create an exclusive Wetherill/Tera--Wasserburg toggle row."""
        container = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        group = QtWidgets.QButtonGroup(container)
        group.setExclusive(True)
        buttons: dict[str, QtWidgets.QToolButton] = {}
        for key, label in (
            ("wetherill", "Wetherill"),
            ("tera_wasserburg", "Tera–Wasserburg"),
        ):
            button = QtWidgets.QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setProperty("modeToggle", True)
            button.setChecked(key == "wetherill")
            button.clicked.connect(callback)
            group.addButton(button)
            row.addWidget(button)
            buttons[key] = button
        row.addStretch(1)
        return container, buttons

    def create_spot_concordia_page(self) -> QtWidgets.QWidget:
        """Build the selectable multi-spot concordia review page."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        controls = QtWidgets.QWidget()
        controls.setMaximumWidth(230)
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(QtWidgets.QLabel("Spot means to display"))
        self.spot_filter = QtWidgets.QLineEdit()
        self.spot_filter.setPlaceholderText("Filter spots…")
        self.spot_filter.textChanged.connect(self.filter_spot_choices)
        controls_layout.addWidget(self.spot_filter)
        self.spot_choice_list = QtWidgets.QListWidget()
        self.spot_choice_list.itemChanged.connect(
            lambda _item: self.refresh_spot_concordia()
        )
        controls_layout.addWidget(self.spot_choice_list, 1)
        select_row = QtWidgets.QHBoxLayout()
        select_all = QtWidgets.QPushButton("All")
        select_none = QtWidgets.QPushButton("None")
        select_all.clicked.connect(lambda: self.set_all_spot_choices(True))
        select_none.clicked.connect(lambda: self.set_all_spot_choices(False))
        select_row.addWidget(select_all)
        select_row.addWidget(select_none)
        controls_layout.addLayout(select_row)
        diagram_row, self.spot_diagram_buttons = self.create_diagram_buttons(
            self.refresh_spot_concordia
        )
        controls_layout.addWidget(diagram_row)
        self.spot_concordia = ConcordiaReviewWidget()
        layout.addWidget(controls)
        layout.addWidget(self.spot_concordia, 1)
        return page

    def create_depth_concordia_page(self) -> QtWidgets.QWidget:
        """Build the one-spot sweep-wise concordia review page."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        # Two short rows rather than one long one, so the page (and with it
        # the whole results area) fits a laptop-width plot column.
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Depth-profile spot"))
        self.depth_spot_combo = StyledComboBox()
        self.depth_spot_combo.setMinimumContentsLength(8)
        self.depth_spot_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.depth_spot_combo.currentIndexChanged.connect(
            self.refresh_depth_concordia
        )
        controls.addWidget(self.depth_spot_combo, 1)
        layout.addLayout(controls)
        diagram_row, self.depth_diagram_buttons = self.create_diagram_buttons(
            self.refresh_depth_concordia
        )
        layout.addWidget(diagram_row)
        self.depth_concordia = ConcordiaReviewWidget()
        layout.addWidget(self.depth_concordia, 1)
        return page

    @staticmethod
    def selected_diagram(
        buttons: dict[str, QtWidgets.QToolButton],
    ) -> str:
        """Return the checked concordia coordinate system."""
        for key, button in buttons.items():
            if button.isChecked():
                return key
        return "wetherill"

    def apply_theme(self, theme: ThemeSpec) -> None:
        """Theme and redraw both embedded concordia canvases."""
        self._theme = theme
        self.refresh_spot_concordia()
        self.refresh_depth_concordia()

    def active_concordia(self) -> ConcordiaReviewWidget | None:
        """Return the concordia canvas represented by the current result tab."""
        current = self.tabs.currentWidget()
        if current is self.spot_concordia_page:
            return self.spot_concordia
        if current is self.depth_concordia_page:
            return self.depth_concordia
        # When the plot mode is switched directly to Final or Depth, use the
        # corresponding review canvas even before its tab is opened.
        return None

    def set_zoom_mode(self, mode: str) -> None:
        """Forward zoom-axis changes to the visible concordia canvas."""
        target = self.active_concordia()
        if target is not None:
            target.set_zoom_mode(mode)

    def zoom_mode(self) -> str:
        """Return the visible concordia zoom mode, or the default."""
        target = self.active_concordia()
        return target.zoom_mode() if target is not None else "xy"

    def set_interaction_mode(self, mode: str) -> None:
        """Forward pan/box selection to the visible concordia canvas."""
        target = self.active_concordia()
        if target is not None:
            target.set_interaction_mode(mode)

    def interaction_mode(self) -> str:
        """Return the visible concordia interaction mode, or the default."""
        target = self.active_concordia()
        return target.interaction_mode() if target is not None else "zoom"

    def restore_default_view(self) -> None:
        """Restore the current concordia view without changing table state."""
        target = self.active_concordia()
        if target is not None:
            target.restore_default_view()

    def filter_spot_choices(self, text: str) -> None:
        """Hide non-matching spot selectors without changing selections."""
        query = text.strip().casefold()
        for index in range(self.spot_choice_list.count()):
            item = self.spot_choice_list.item(index)
            item.setHidden(query not in item.text().casefold())

    def set_all_spot_choices(self, checked: bool) -> None:
        """Check or clear every currently visible spot in one redraw."""
        self.spot_choice_list.blockSignals(True)
        for index in range(self.spot_choice_list.count()):
            item = self.spot_choice_list.item(index)
            if not item.isHidden():
                item.setCheckState(
                    QtCore.Qt.CheckState.Checked
                    if checked
                    else QtCore.Qt.CheckState.Unchecked
                )
        self.spot_choice_list.blockSignals(False)
        self.refresh_spot_concordia()

    def refresh_spot_concordia(self) -> None:
        """Redraw only the checked final spot means."""
        if self._theme is None:
            return
        selected_names = {
            self.spot_choice_list.item(index).text()
            for index in range(self.spot_choice_list.count())
            if self.spot_choice_list.item(index).checkState()
            == QtCore.Qt.CheckState.Checked
        }
        selected = self._spot_frame
        if selected_names and "spot_name" in selected.columns:
            selected = selected[selected["spot_name"].astype(str).isin(selected_names)]
        elif not selected_names:
            selected = selected.iloc[0:0]
        self.spot_concordia.show_data(
            selected,
            diagram=self.selected_diagram(self.spot_diagram_buttons),
            kind="spot",
            include_calibration_uncertainty=(
                self._include_calibration_uncertainty
            ),
            theme=self._theme,
        )

    def refresh_depth_concordia(self) -> None:
        """Redraw corrected sweeps for the chosen depth-profile spot."""
        if self._theme is None:
            return
        spot_name = self.depth_spot_combo.currentText()
        selected = self._depth_frame
        if spot_name and "spot_name" in selected.columns:
            selected = selected[selected["spot_name"].astype(str).eq(spot_name)]
        self.depth_concordia.show_data(
            selected,
            diagram=self.selected_diagram(self.depth_diagram_buttons),
            kind="depth",
            include_calibration_uncertainty=(
                self._include_calibration_uncertainty
            ),
            theme=self._theme,
        )

    def create_table(
        self,
    ) -> tuple[QtWidgets.QTableView, DataFrameTableModel]:
        """Create one sortable table backed by a Qt proxy."""
        view = QtWidgets.QTableView()
        model = DataFrameTableModel(parent=view)
        proxy = QtCore.QSortFilterProxyModel(view)
        proxy.setSourceModel(model)
        proxy.setSortRole(QtCore.Qt.ItemDataRole.UserRole)
        view.setModel(proxy)
        view.setSortingEnabled(True)
        view.setAlternatingRowColors(True)
        view.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        view.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        view.setWordWrap(False)
        view.verticalHeader().setVisible(False)
        view.horizontalHeader().setStretchLastSection(False)
        view.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        return view, model

    def show_results(self, finalized: object) -> None:
        """Display a FinalizedReduction without duplicating reduction logic."""
        final_frame = getattr(finalized, "selected_results", pd.DataFrame())
        secondary = getattr(
            finalized,
            "secondary_standard_summary",
            pd.DataFrame(),
        )
        self.results_model.set_frame(final_frame)
        self.secondary_model.set_frame(secondary)
        self._spot_frame = getattr(
            finalized, "parallel_results", pd.DataFrame()
        ).copy()
        decision = getattr(finalized, "decision", None)
        method = getattr(decision, "common_pb_method", "none")
        uncertainty = getattr(
            decision,
            "include_calibration_uncertainty",
            True,
        )
        self._include_calibration_uncertainty = bool(uncertainty)
        self.summary_label.setText(
            f"{len(final_frame):,} spots · common-Pb method {method} · "
            f"calibration uncertainty {'included' if uncertainty else 'excluded'}"
        )
        existing_names = {
            self.spot_choice_list.item(index).text(): (
                self.spot_choice_list.item(index).checkState()
            )
            for index in range(self.spot_choice_list.count())
        }
        spot_names = (
            self._spot_frame["spot_name"].dropna().astype(str).tolist()
            if "spot_name" in self._spot_frame.columns
            else []
        )
        self.spot_choice_list.blockSignals(True)
        self.spot_choice_list.clear()
        for spot_name in dict.fromkeys(spot_names):
            item = QtWidgets.QListWidgetItem(spot_name)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                existing_names.get(spot_name, QtCore.Qt.CheckState.Checked)
            )
            self.spot_choice_list.addItem(item)
        self.spot_choice_list.blockSignals(False)
        self.refresh_spot_concordia()
        if self.tabs.currentWidget() not in (
            self.results_table,
            self.secondary_table,
            self.spot_concordia_page,
        ):
            self.tabs.setCurrentWidget(self.results_table)

    def show_depth_profile(
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
        existing_spot = self.depth_spot_combo.currentText()
        spot_names = (
            depth_profile["spot_name"].dropna().astype(str).drop_duplicates().tolist()
            if "spot_name" in depth_profile.columns
            else []
        )
        self.depth_spot_combo.blockSignals(True)
        self.depth_spot_combo.clear()
        self.depth_spot_combo.addItems(spot_names)
        preferred = spot_name or existing_spot
        preferred_index = self.depth_spot_combo.findText(preferred)
        if preferred_index >= 0:
            self.depth_spot_combo.setCurrentIndex(preferred_index)
        self.depth_spot_combo.blockSignals(False)
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


class MetricCard(NeumorphicFrame):
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
    """Primary-RM downhole factors with an adjustable uncertainty envelope.

    Data is supplied via :meth:`show_data`.  Accepts the dict returned by
    ``services.get_downhole_plot_data``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._theme: ThemeSpec | None = None
        self._data: dict | None = None
        self._plot_item: pg.PlotItem | None = None
        self._sigma_level = 2
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
        fit_label = str(data.get("fit_label", "Interpolated"))

        p = self.addPlot(row=0, col=0)
        self._plot_item = p
        _make_diag_axis(p, theme)
        p.setLabel("bottom", "Relative time (s)", color=theme.axis_text, size="9px")
        p.setLabel("left", "Fractionation factor", color=theme.axis_text, size="9px")
        p.setTitle(
            f"{ratio_name}  downhole fractionation — {primary_standard} ({fit_label})",
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

    def set_sigma_level(self, sigma_level: int) -> None:
        """Redraw the diagnostic with a one- or two-sigma envelope."""
        self._sigma_level = 1 if sigma_level <= 1 else 2
        if self._data is not None:
            self._draw()

    def sigma_level(self) -> int:
        """Return the displayed uncertainty multiplier."""
        return self._sigma_level

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
    """Session factors and fitted correction with adjustable uncertainty.

    Data is supplied via :meth:`show_data`.  Accepts the dict returned by
    ``services.get_session_plot_data``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._theme: ThemeSpec | None = None
        self._data: dict | None = None
        self._plot_item: pg.PlotItem | None = None
        self._sigma_level = 2
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
        show_uncertainty = bool(data.get("show_uncertainty", False))
        fit_label = str(data.get("fit_label", "Polynomial"))

        p = self.addPlot(row=0, col=0)
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
        )
        p.addItem(scatter)

        # Reference line at 1.0
        p.addLine(y=1.0, pen=pg.mkPen(theme.muted, width=1,
                                       style=QtCore.Qt.PenStyle.DotLine))
        self._apply_zoom_mode()
        self._apply_interaction_mode()

    def set_sigma_level(self, sigma_level: int) -> None:
        """Redraw fit bands and standard-spot error bars at 1σ or 2σ."""
        self._sigma_level = 1 if sigma_level <= 1 else 2
        if self._data is not None:
            self._draw()

    def sigma_level(self) -> int:
        """Return the displayed uncertainty multiplier."""
        return self._sigma_level

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
        # 160 px keeps the trace editable when a laptop screen squeezes the
        # column; it normally gets 250 px or more (tests/test_small_screens.py).
        self.setMinimumHeight(160)
        self.setSizePolicy(
            QtWidgets.QSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
        )
        self.plot_item = self.getPlotItem()
        self._theme = active_theme()
        self._region_overlays: dict[str, dict[str, object]] = {}
        self._signal_boundaries: dict[str, pg.InfiniteLine] = {}
        self._signal_markers: dict[str, pg.ScatterPlotItem] = {}
        self._hover_series: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._measurement_layers: dict[str, pg.PlotDataItem] = {}
        self._measurement_point_limit = 3000
        self._selection_times = np.array([], dtype=float)
        self._selection_channel: str | None = None
        self._sigma_level = 2
        self._zoom_mode = "xy"
        self._interaction_mode = "zoom"
        self._suspend_x_window_signal = False
        self._snap_guard = False
        self._measurement_refresh_timer = QtCore.QTimer(self)
        self._measurement_refresh_timer.setSingleShot(True)
        self._measurement_refresh_timer.setInterval(35)
        self._measurement_refresh_timer.timeout.connect(
            self._update_visible_measurement_points
        )
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        self._hover_proxy = pg.SignalProxy(self.scene().sigMouseMoved, rateLimit=60, slot=self._on_scene_mouse_moved)
        self.plot_item.vb.sigXRangeChanged.connect(self._on_x_range_changed)
        self.plot_item.vb.sigYRangeChanged.connect(self._on_y_range_changed)
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
        self._signal_boundaries = {}
        self._signal_markers = {}
        self._hover_series = {}
        self._measurement_layers = {}
        self._selection_times = np.array([], dtype=float)
        self._selection_channel = None
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
        self._add_region("preview_baseline", (18.0, 42.0), colors["baseline"], 44, movable=False, frame=preview_frame, stats_channel="238U", plateau_filter_method="none")
        self._add_region("preview_plateau", (47.0, 62.0), colors["plateau"], 52, movable=False, frame=preview_frame, stats_channel="238U", plateau_filter_method="none")
        self._add_signal_boundary(
            "signal_start",
            43.0,
            "#00e676",
            "signal start",
            movable=False,
            bounds=(0.0, 47.0),
        )
        self._add_signal_boundary(
            "signal_end",
            70.0,
            "#ff8c1a",
            "signal end",
            movable=False,
            bounds=(62.0, 80.0),
        )
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
        plateau_filter_method: str = "none",
        selection_frame: pd.DataFrame | None = None,
    ) -> None:
        """Show session traces with selections evaluated for one active spot.

        ``spot_data`` may contain the complete session so the user can widen
        the overview navigator beyond the active ablation. Selection points,
        statistics, and point-snapping use ``selection_frame`` when supplied,
        keeping the reviewed spot isolated from neighboring acquisitions.
        """
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
        active_selection_frame = (
            selection_frame
            if selection_frame is not None and not selection_frame.empty
            else spot_data
        )
        self._selection_times = active_selection_frame["Time"].to_numpy(
            dtype=float
        )
        self._selection_channel = region_channel
        colors = _resolved_region_colors(region_colors)
        point_size = 5.5 if len(channels) <= 4 else 4.0

        for channel in channels:
            if channel not in spot_data:
                continue
            y_values = spot_data[channel].to_numpy(dtype=float)
            color = channel_colors.get(channel, "#69e2ff")
            curve = self.plot_item.plot(
                x_values,
                y_values,
                pen=pg.mkPen(color, width=1.8),
                name=channel,
            )
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            curve.setZValue(2)

            # Draw sampled sweeps as a separate layer.  Keeping the points
            # separate from the connecting curve lets selection uncertainty
            # envelopes remain behind the observations while the mean bars
            # remain readable.  Automatic downsampling and clipping keep a
            # full-session trace responsive even when many channels are on.
            measurements = self.plot_item.plot(
                [],
                [],
                pen=None,
                symbol="o",
                symbolSize=point_size,
                symbolBrush=pg.mkBrush(color),
                symbolPen=pg.mkPen(self._theme.plot_background, width=0.7),
            )
            measurements.setDownsampling(auto=True, method="subsample")
            measurements.setClipToView(True)
            measurements.setZValue(5)
            measurements.setProperty("measurement_points", True)
            self._measurement_layers[channel] = measurements
            self._hover_series[channel] = (x_values, y_values)
            finite_values = y_values[np.isfinite(y_values)]
            if finite_values.size:
                current_min = float(np.min(finite_values))
                current_max = float(np.max(finite_values))
                y_min = current_min if y_min is None else min(y_min, current_min)
                y_max = current_max if y_max is None else max(y_max, current_max)

        if regions.get("baseline"):
            self._add_region("baseline", regions["baseline"], colors["baseline"], 44, movable=editable_regions, frame=active_selection_frame, stats_channel=region_channel, plateau_filter_method="none")
        if regions.get("plateau"):
            self._add_region("plateau", regions["plateau"], colors["plateau"], 52, movable=editable_regions, frame=active_selection_frame, stats_channel=region_channel, plateau_filter_method=plateau_filter_method)
        signal_region = regions.get("signal")
        if signal_region:
            plateau_region = regions.get("plateau")
            minimum_time = float(np.nanmin(x_values))
            maximum_time = float(np.nanmax(x_values))
            start_maximum = (
                float(plateau_region[0])
                if plateau_region
                else float(signal_region[1])
            )
            end_minimum = (
                float(plateau_region[1])
                if plateau_region
                else float(signal_region[0])
            )
            self._add_signal_boundary(
                "signal_start",
                float(signal_region[0]),
                "#00e676",
                "signal start",
                movable=editable_regions,
                bounds=(minimum_time, start_maximum),
            )
            self._add_signal_boundary(
                "signal_end",
                float(signal_region[1]),
                "#ff8c1a",
                "signal end",
                movable=editable_regions,
                bounds=(end_minimum, maximum_time),
            )

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
        regions = {
            name: tuple(sorted(float(value) for value in region["region"].getRegion()))
            for name, region in self._region_overlays.items()
        }
        signal_start = self._signal_boundaries.get("signal_start")
        signal_end = self._signal_boundaries.get("signal_end")
        regions["signal"] = (
            tuple(
                sorted(
                    (
                        float(signal_start.value()),
                        float(signal_end.value()),
                    )
                )
            )
            if signal_start is not None and signal_end is not None
            else None
        )
        return regions

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
        plateau_filter_method: str,
    ) -> None:
        base_color = pg.mkColor(color)
        region = pg.LinearRegionItem(
            values=sorted(bounds),
            movable=movable,
            # The statistical FillBetweenItem below is the visible selection
            # box.  Keeping the region brush transparent avoids painting a
            # full-height vertical strip through unrelated channels.
            brush=pg.mkBrush(0, 0, 0, 0),
            hoverBrush=pg.mkBrush(
                base_color.red(),
                base_color.green(),
                base_color.blue(),
                14,
            ),
        )
        edge_pen = pg.mkPen(
            base_color.red(),
            base_color.green(),
            base_color.blue(),
            145 if movable else 70,
            width=1.3,
        )
        hover_pen = pg.mkPen(color, width=3.0)
        for line in region.lines:
            line.setPen(edge_pen)
            line.setHoverPen(hover_pen)
            line.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        region.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        region.setZValue(6)
        mean_line = pg.PlotDataItem(pen=pg.mkPen(color, width=2.5))
        upper_line = pg.PlotDataItem(pen=pg.mkPen(base_color.red(), base_color.green(), base_color.blue(), max(120, alpha * 4), width=1.3))
        lower_line = pg.PlotDataItem(pen=pg.mkPen(base_color.red(), base_color.green(), base_color.blue(), max(120, alpha * 4), width=1.3))
        fill = pg.FillBetweenItem(upper_line, lower_line, brush=pg.mkBrush(base_color.red(), base_color.green(), base_color.blue(), alpha))
        fill.setZValue(3)
        mean_line.setZValue(4)
        upper_line.setZValue(3)
        lower_line.setZValue(3)
        selected_points = pg.ScatterPlotItem(
            symbol="o",
            size=8.0,
            pen=pg.mkPen(self._theme.plot_background, width=1.0),
            brush=pg.mkBrush(color),
        )
        selected_points.setZValue(9)
        excluded_points = pg.ScatterPlotItem(
            symbol="o",
            size=9.0,
            pen=pg.mkPen(color, width=2.0),
            brush=pg.mkBrush(self._theme.plot_background),
        )
        excluded_points.setZValue(10)
        excluded_segments = pg.PlotDataItem(
            pen=pg.mkPen(
                color,
                width=1.8,
                style=QtCore.Qt.PenStyle.DashLine,
            )
        )
        excluded_segments.setZValue(8)
        excluded_segment_mask = pg.PlotDataItem(
            pen=pg.mkPen(self._theme.plot_background, width=4.2)
        )
        excluded_segment_mask.setZValue(7)

        self.plot_item.addItem(region)
        self.plot_item.addItem(fill)
        self.plot_item.addItem(upper_line)
        self.plot_item.addItem(lower_line)
        self.plot_item.addItem(mean_line)
        self.plot_item.addItem(excluded_segment_mask)
        self.plot_item.addItem(excluded_segments)
        self.plot_item.addItem(selected_points)
        self.plot_item.addItem(excluded_points)

        overlay_frame = frame[["Time", stats_channel]].dropna().copy() if stats_channel and stats_channel in frame else frame[["Time"]].copy()
        self._region_overlays[name] = {
            "region": region,
            "mean_line": mean_line,
            "upper_line": upper_line,
            "lower_line": lower_line,
            "frame": overlay_frame,
            "channel": stats_channel,
            "selected_points": selected_points,
            "excluded_points": excluded_points,
            "excluded_segments": excluded_segments,
            "excluded_segment_mask": excluded_segment_mask,
            "plateau_filter_method": normalize_plateau_filter_method(
                plateau_filter_method
            ),
        }
        self._update_region_overlay(name)

        if movable:
            region.sigRegionChanged.connect(lambda _item, region_name=name: self._update_region_overlay(region_name, emit_live=True))
            region.sigRegionChangeFinished.connect(lambda _item, region_name=name: self._emit_region_change(region_name))

    def _add_signal_boundary(
        self,
        name: str,
        value: float,
        color: str,
        label: str,
        *,
        movable: bool,
        bounds: tuple[float, float],
    ) -> None:
        """Add a draggable signal edge constrained around the plateau."""
        line = pg.InfiniteLine(
            pos=value,
            angle=90,
            movable=movable,
            pen=pg.mkPen(color, width=2.6),
            hoverPen=pg.mkPen(color, width=4.0),
            label=label,
            labelOpts={
                "position": 0.93,
                "color": color,
                "fill": pg.mkBrush(0, 0, 0, 125),
                "movable": movable,
            },
        )
        line.setBounds(tuple(sorted(float(item) for item in bounds)))
        line.setZValue(12)
        self.plot_item.addItem(line)
        self._signal_boundaries[name] = line
        marker = pg.ScatterPlotItem(
            symbol="d" if name == "signal_start" else "s",
            size=15 if name == "signal_start" else 13,
            pen=pg.mkPen(self._theme.plot_background, width=1.4),
            brush=pg.mkBrush(color),
        )
        marker.setZValue(14)
        self.plot_item.addItem(marker)
        self._signal_markers[name] = marker
        self._update_signal_marker(name)
        if movable:
            line.sigPositionChanged.connect(
                lambda _item, boundary_name=name: self._on_signal_position_changed(
                    boundary_name
                )
            )
            line.sigPositionChangeFinished.connect(
                lambda _item, boundary_name=name: self._emit_signal_change(
                    boundary_name
                )
            )

    def _nearest_measurement_time(self, value: float) -> float:
        """Return the measured sweep time nearest a dragged boundary."""
        finite_times = self._selection_times[
            np.isfinite(self._selection_times)
        ]
        if not finite_times.size:
            return float(value)
        return float(finite_times[np.argmin(np.abs(finite_times - value))])

    def _on_signal_position_changed(self, boundary_name: str) -> None:
        """Snap a moving signal edge to a sweep and update its point marker."""
        if self._snap_guard:
            return
        line = self._signal_boundaries.get(boundary_name)
        if line is None:
            return
        snapped = self._nearest_measurement_time(float(line.value()))
        if not np.isclose(snapped, float(line.value())):
            self._snap_guard = True
            try:
                line.setValue(snapped)
            finally:
                self._snap_guard = False
        self._update_signal_marker(boundary_name)
        # The final snapped value is emitted by sigPositionChangeFinished.
        # Keeping mouse-move updates local avoids repeatedly rebuilding the
        # workflow status while the handle is still moving.

    def _update_signal_marker(self, boundary_name: str) -> None:
        """Place a signal-start/end glyph on the selected display channel."""
        marker = self._signal_markers.get(boundary_name)
        line = self._signal_boundaries.get(boundary_name)
        series = self._hover_series.get(self._selection_channel or "")
        if marker is None or line is None or series is None:
            return
        x_values, y_values = series
        if not len(x_values):
            return
        index = int(np.argmin(np.abs(x_values - float(line.value()))))
        marker.setData(
            x=[float(x_values[index])],
            y=[float(y_values[index])],
        )

    def _emit_signal_change(self, boundary_name: str) -> None:
        """Report the combined signal interval after either edge moves."""
        start = self._signal_boundaries.get("signal_start")
        end = self._signal_boundaries.get("signal_end")
        if start is None or end is None:
            return
        bounds = tuple(sorted((float(start.value()), float(end.value()))))
        self.regionChanged.emit(boundary_name, bounds)

    def _update_region_overlay(
        self,
        region_name: str,
        *,
        emit_live: bool = False,
    ) -> None:
        overlay = self._region_overlays.get(region_name)
        if overlay is None:
            return
        start_value, end_value = sorted(float(value) for value in overlay["region"].getRegion())
        snapped_bounds = (
            self._nearest_measurement_time(start_value),
            self._nearest_measurement_time(end_value),
        )
        snapped_bounds = tuple(sorted(snapped_bounds))
        if emit_live and not np.allclose(
            snapped_bounds,
            (start_value, end_value),
        ):
            self._snap_guard = True
            try:
                overlay["region"].setRegion(snapped_bounds)
            finally:
                self._snap_guard = False
            start_value, end_value = snapped_bounds
        frame = overlay["frame"]
        stats_channel = overlay["channel"]
        if frame.empty or stats_channel is None or stats_channel not in frame:
            return
        subset = frame[
            (frame["Time"] >= start_value) & (frame["Time"] < end_value)
        ]
        if subset.empty:
            overlay["selected_points"].setData([], [])
            overlay["excluded_points"].setData([], [])
            overlay["excluded_segments"].setData([], [])
            overlay["excluded_segment_mask"].setData([], [])
            return
        selected_times = subset["Time"].to_numpy(dtype=float)
        y_values = subset[stats_channel].to_numpy(dtype=float)
        finite_mask = np.isfinite(selected_times) & np.isfinite(y_values)
        inlier_mask = finite_mask.copy()
        if (
            region_name in {"plateau", "preview_plateau"}
            and overlay["plateau_filter_method"] == "excel_2sigma"
            and finite_mask.sum() >= 3
        ):
            local_mask = excel_plateau_inlier_mask(
                selected_times[finite_mask],
                y_values[finite_mask],
            )
            inlier_mask[:] = False
            inlier_mask[np.flatnonzero(finite_mask)] = local_mask
        excluded_mask = finite_mask & ~inlier_mask
        overlay["selected_points"].setData(
            x=selected_times[inlier_mask],
            y=y_values[inlier_mask],
        )
        overlay["excluded_points"].setData(
            x=selected_times[excluded_mask],
            y=y_values[excluded_mask],
        )
        dashed_x: list[float] = []
        dashed_y: list[float] = []
        for index in range(max(0, len(selected_times) - 1)):
            if not (finite_mask[index] and finite_mask[index + 1]):
                continue
            if excluded_mask[index] or excluded_mask[index + 1]:
                dashed_x.extend(
                    [selected_times[index], selected_times[index + 1], np.nan]
                )
                dashed_y.extend([y_values[index], y_values[index + 1], np.nan])
        overlay["excluded_segments"].setData(dashed_x, dashed_y)
        overlay["excluded_segment_mask"].setData(dashed_x, dashed_y)

        finite_values = y_values[inlier_mask]
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
        overlay["spread"] = spread
        overlay["included_count"] = int(inlier_mask.sum())
        overlay["excluded_count"] = int(excluded_mask.sum())
        self._update_region_span(overlay)

        if region_name == "plateau":
            self._constrain_signal_edges_to_plateau(start_value, end_value)
        # Selection points, filtered sweeps, and statistics have already been
        # redrawn locally. sigRegionChangeFinished persists the final bounds;
        # emitting every mouse move made large-session dragging noticeably
        # laggy without adding useful state changes.

    def _constrain_signal_edges_to_plateau(
        self,
        plateau_start: float,
        plateau_end: float,
    ) -> None:
        """Keep signal edges valid while a plateau window is dragged."""
        signal_start = self._signal_boundaries.get("signal_start")
        signal_end = self._signal_boundaries.get("signal_end")
        if signal_start is None or signal_end is None:
            return
        finite_times = self._selection_times[np.isfinite(self._selection_times)]
        if not finite_times.size:
            return
        signal_start.setBounds((float(finite_times.min()), plateau_start))
        signal_end.setBounds((plateau_end, float(finite_times.max())))
        if float(signal_start.value()) > plateau_start:
            signal_start.setValue(plateau_start)
        if float(signal_end.value()) < plateau_end:
            signal_end.setValue(plateau_end)
        self._update_signal_marker("signal_start")
        self._update_signal_marker("signal_end")

    def _region_stats_text(self, x_value: float) -> list[str]:
        texts: list[str] = []
        for region_name, overlay in self._region_overlays.items():
            start_value, end_value = sorted(float(value) for value in overlay["region"].getRegion())
            if start_value <= x_value <= end_value and "mean" in overlay and "std" in overlay:
                mean_value = float(overlay["mean"])
                std_value = float(overlay["std"])
                sigma = self._sigma_level
                included = int(overlay.get("included_count", 0))
                excluded = int(overlay.get("excluded_count", 0))
                exclusion_note = (
                    f", {excluded} excluded" if excluded else ""
                )
                texts.append(
                    f"{region_name}: n={included}{exclusion_note}, "
                    f"mean {mean_value:,.2f}, {sigma}σ ±{sigma * std_value:,.2f}"
                )
        return texts

    def _emit_region_change(self, region_name: str) -> None:
        if self._snap_guard:
            return
        overlay = self._region_overlays.get(region_name)
        if overlay is None:
            return
        bounds = tuple(sorted(float(value) for value in overlay["region"].getRegion()))
        self.regionChanged.emit(region_name, bounds)

    def _on_x_range_changed(self, _view_box: object, x_range: object) -> None:
        if self._suspend_x_window_signal:
            self._measurement_refresh_timer.start()
            return
        self._measurement_refresh_timer.start()
        self.xWindowChanged.emit(tuple(float(value) for value in x_range))

    def _update_visible_measurement_points(self) -> None:
        """Draw real sweeps in view without carrying invisible scatter glyphs.

        The connecting curves always retain every sweep. When zoomed into one
        or several spots, every measured point is drawn. At session scale an
        evenly spaced subset is sufficient because multiple points occupy the
        same screen pixel; limiting glyphs keeps pan and navigator drags fluid.
        """
        if not self._measurement_layers:
            return
        x_start, x_end = self.visible_x_range()
        for channel, layer in self._measurement_layers.items():
            series = self._hover_series.get(channel)
            if series is None:
                continue
            x_values, y_values = series
            if not len(x_values):
                layer.setData([], [])
                continue
            if np.all(np.diff(x_values) >= 0.0):
                start_index = int(np.searchsorted(x_values, x_start, side="left"))
                end_index = int(np.searchsorted(x_values, x_end, side="right"))
                visible_indices = np.arange(start_index, end_index, dtype=int)
            else:
                visible_indices = np.flatnonzero(
                    (x_values >= x_start) & (x_values <= x_end)
                )
            if visible_indices.size > self._measurement_point_limit:
                sample_positions = np.linspace(
                    0,
                    visible_indices.size - 1,
                    self._measurement_point_limit,
                    dtype=int,
                )
                visible_indices = visible_indices[sample_positions]
            layer.setData(
                x=x_values[visible_indices],
                y=y_values[visible_indices],
            )

    def _on_y_range_changed(self, _view_box: object, _y_range: object) -> None:
        """Keep draggable selection edges confined to the uncertainty box."""
        for overlay in self._region_overlays.values():
            self._update_region_span(overlay)

    def _update_region_span(self, overlay: dict[str, object]) -> None:
        """Map mean ± displayed sigma into the view's normalized height.

        ``LinearRegionItem`` uses a normalized vertical span for its fill and
        draggable edges.  Updating that span when the y-axis changes makes the
        selection look like a compact statistical box rather than a vertical
        band, without sacrificing its point-snapped horizontal handles.
        """
        if "mean" not in overlay or "spread" not in overlay:
            return
        y_min, y_max = self.visible_y_range()
        y_span = y_max - y_min
        if not np.isfinite(y_span) or y_span <= 0.0:
            return
        mean_value = float(overlay["mean"])
        spread = float(overlay["spread"])
        lower = (mean_value - spread - y_min) / y_span
        upper = (mean_value + spread - y_min) / y_span
        lower, upper = sorted((lower, upper))
        # A nearly constant selection still needs a usable visual handle.
        minimum_span = 0.012
        if upper - lower < minimum_span:
            center = 0.5 * (lower + upper)
            lower = center - minimum_span / 2.0
            upper = center + minimum_span / 2.0
        region = overlay["region"]
        region.setSpan(
            max(0.0, min(1.0, lower)),
            max(0.0, min(1.0, upper)),
        )

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
        # The overview is a navigation strip; keeping it shallow reserves the
        # larger share of the plot card for sweep-level selection editing.
        self.setFixedHeight(115)
        self.plot_item = self.getPlotItem()
        self._theme = active_theme()
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
