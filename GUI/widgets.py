from __future__ import annotations

import os
import re
import weakref
from collections.abc import Iterable
from html import escape

import numpy as np
import pandas as pd

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

from PySide6 import QtCore, QtGui, QtWidgets

import pyqtgraph as pg
from pyqtgraph.graphicsItems.LegendItem import ItemSample

from GUI.services import CHANNEL_SECTIONS, _parse_channel_name, current_standard_archive
from GUI.state import ReductionSession
from GUI.theme import NeumorphicFrame, ThemeSpec, active_theme, theme_color
from UPbRPy.calibration import fit_cross_calibration
from UPbRPy.isochron import concordia_age_readout, tera_wasserburg_point, wetherill_point
from UPbRPy.isoplot import format_age_label, nice_step, visible_concordia_age_labels
from UPbRPy.results import format_mswd, format_value_with_uncertainty, weighted_mean_and_mswd
from UPbRPy.ratios import (
    excel_plateau_inlier_mask,
    normalize_plateau_filter_method,
)


_MASS_RE = re.compile(r'^(\d+)\s*([A-Za-z]+)$')


def _parse_channel(name: str) -> tuple[str, str] | None:
    """Return (Element_Capitalized, mass_str) for '238U' / 'U238', else None."""
    return _parse_channel_name(name)


class _RoundedCanvas:
    """Clip only the outer canvas corners; plot items retain normal rendering."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(self.rect()), 11, 11)
        self.setMask(QtGui.QRegion(path.toFillPolygon().toPolygon()))


class RoundedPlotWidget(_RoundedCanvas, pg.PlotWidget):
    """An opaque scientific plot with rounded outer corners."""


class RoundedGraphicsLayoutWidget(_RoundedCanvas, pg.GraphicsLayoutWidget):
    """A rounded canvas for multi-panel scientific plots."""


class RoundedLegendItem(pg.LegendItem):
    """Paint a compact rounded legend without a graphics effect."""

    def paint(self, painter, *args):
        theme = active_theme()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        fill = theme_color(theme.glass_strong)
        painter.setBrush(fill)
        painter.setPen(QtGui.QPen(theme_color(theme.input_border), 1))
        painter.drawRoundedRect(self.boundingRect().adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)


def _rounded_legend(plot, **options):
    legend = RoundedLegendItem(**options)
    legend.setParentItem(plot.vb)
    plot.legend = legend
    return legend


class _PillLineLabel(pg.InfLineLabel):
    def updatePosition(self):
        super().updatePosition()
        if callback := getattr(self, "position_callback", None):
            callback()

    def paint(self, painter, *args):
        # Keep pyqtgraph's transform and drag handling; paint only the background.
        super().paint(painter, *args)
        theme = active_theme()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(theme_color(theme.glass_strong))
        painter.setPen(QtGui.QPen(theme_color(theme.input_border), 1))
        rect = self.textItem.mapRectToParent(self.textItem.boundingRect())
        painter.drawRoundedRect(rect, rect.height()/2, rect.height()/2)


class WrappingButton(QtWidgets.QPushButton):
    """A native push button that paints its full label on wrapped lines."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.setToolTip(text)
        self.setAccessibleName(text)

    def _text_rect(self, width: int, height: int) -> QtCore.QRect:
        option = QtWidgets.QStyleOptionButton()
        self.initStyleOption(option)
        option.rect = QtCore.QRect(0, 0, width, height)
        return self.style().subElementRect(
            QtWidgets.QStyle.SubElement.SE_PushButtonContents, option, self,
        )

    def heightForWidth(self, width: int) -> int:
        if self.property("compactIcon"):
            return 28
        rect = self._text_rect(max(width, 40), 1000)
        text_height = self.fontMetrics().boundingRect(
            QtCore.QRect(0, 0, max(1, rect.width()), 1000),
            QtCore.Qt.TextFlag.TextWordWrap, self.text(),
        ).height()
        return text_height + 1000 - rect.height() + 2

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(160, self.heightForWidth(max(160, self.width())))

    def minimumSizeHint(self) -> QtCore.QSize:
        if self.property("compactIcon"):
            return QtCore.QSize(24, 28)
        return QtCore.QSize(80, self.heightForWidth(max(80, self.width())))

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self.setFixedHeight(self.heightForWidth(event.size().width()))

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        option = QtWidgets.QStyleOptionButton()
        self.initStyleOption(option)
        if self.property("compactIcon"):
            painter = QtWidgets.QStylePainter(self)
            painter.drawControl(QtWidgets.QStyle.ControlElement.CE_PushButton, option)
            return
        option.text = ""
        painter = QtWidgets.QStylePainter(self)
        painter.drawControl(QtWidgets.QStyle.ControlElement.CE_PushButton, option)
        painter.drawItemText(
            self._text_rect(self.width(), self.height()),
            QtCore.Qt.AlignmentFlag.AlignCenter | QtCore.Qt.TextFlag.TextWordWrap,
            option.palette, self.isEnabled(), self.text(),
            QtGui.QPalette.ColorRole.ButtonText,
        )


class SegmentButton(QtWidgets.QToolButton):
    """A segment whose full title stays in its tooltip when space is tight."""

    def minimumSizeHint(self):
        return QtCore.QSize(24, self.sizeHint().height())

    def paintEvent(self, event):
        option = QtWidgets.QStyleOptionToolButton()
        self.initStyleOption(option)
        option.text = self.fontMetrics().elidedText(self.text(), QtCore.Qt.TextElideMode.ElideRight, max(8, self.width()-8))
        painter = QtWidgets.QStylePainter(self)
        painter.drawComplexControl(QtWidgets.QStyle.ComplexControl.CC_ToolButton, option)


class _WorkflowRailDelegate(QtWidgets.QStyledItemDelegate):
    """Keep stage icons and number badges readable in the compact rail."""

    def paint(self, painter, option, index):
        if not self.parent().property("compactRail"):
            return super().paint(painter, option, index)
        theme = active_theme()
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(1, 1, -1, -1)
        selected = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        if selected or option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(theme_color(theme.accent_soft if selected else theme.row_hover))
            painter.drawRoundedRect(QtCore.QRectF(rect), 9, 9)
        icon = index.data(QtCore.Qt.ItemDataRole.DecorationRole)
        icon.paint(painter, QtCore.QRect(rect.left()+5, rect.center().y()-10, 20, 20))
        font = option.font
        font.setPixelSize(9)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(theme.accent if selected else theme.muted))
        painter.drawText(rect.adjusted(27, 0, -2, 0), QtCore.Qt.AlignmentFlag.AlignCenter, str(index.row()+1))
        painter.restore()


class SegmentedBar(QtWidgets.QFrame):
    """A stage bar with a short, painted selection transition."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pill = QtCore.QRectF()
        self._selected = None
        self._motion = QtCore.QVariantAnimation(self)
        self._motion.setDuration(180)
        self._motion.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self._motion.valueChanged.connect(self._move_pill)

    def add_segment(self, button):
        button.setProperty("segmentChild", True)
        button.toggled.connect(lambda checked: self._select(button, checked))

    def select_checked(self):
        for button in self.findChildren(SegmentButton):
            if button.isChecked():
                self._select(button, True)
                break

    def _move_pill(self, rect):
        self._pill = rect
        self.update()

    def _select(self, button, checked):
        if not checked:
            return
        self._selected = button
        target = QtCore.QRectF(button.geometry())
        self._motion.stop()
        if self._pill.isEmpty() or not self.isVisible():
            self._move_pill(target)
        else:
            self._motion.setStartValue(self._pill)
            self._motion.setEndValue(target)
            self._motion.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._motion.stop()
        QtCore.QTimer.singleShot(0, self, self._sync_pill)

    def _sync_pill(self):
        if self._selected is not None:
            self._move_pill(QtCore.QRectF(self._selected.geometry()))

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._pill.isEmpty():
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(theme_color(active_theme().accent_soft))
        painter.drawRoundedRect(self._pill, 9, 9)


# -- SpotTree --------------------------------------------------------------

class SpotTree(QtWidgets.QTreeWidget):
    """QTreeWidget with F2-rename and Delete-key support for spot management."""

    deleteRequested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAllColumnsShowFocus(False)
        self.setTextElideMode(QtCore.Qt.TextElideMode.ElideMiddle)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def drawBranches(self, painter, rect, index):
        # Draw only the disclosure; Fusion's branch selection fills the indent.
        if not self.model().hasChildren(index):
            return
        option = QtWidgets.QStyleOption()
        option.rect = QtCore.QRect(rect.right()-self.indentation()+4, rect.y(), self.indentation()-8, rect.height())
        option.state = QtWidgets.QStyle.StateFlag.State_Enabled
        primitive = (QtWidgets.QStyle.PrimitiveElement.PE_IndicatorArrowDown if self.isExpanded(index)
                     else QtWidgets.QStyle.PrimitiveElement.PE_IndicatorArrowRight)
        self.style().drawPrimitive(primitive, option, painter, self)

    def drawRow(self, painter, option, index):
        clean = QtWidgets.QStyleOptionViewItem(option)
        selected = bool(clean.state & QtWidgets.QStyle.StateFlag.State_Selected)
        hovered = bool(clean.state & QtWidgets.QStyle.StateFlag.State_MouseOver)
        clean.state &= ~(QtWidgets.QStyle.StateFlag.State_Selected | QtWidgets.QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            theme = active_theme()
            rect = QtCore.QRectF(self.visualRect(index)).adjusted(1, 1, -2, -1)
            painter.save()
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(theme_color(theme.accent_soft if selected else theme.row_hover))
            painter.drawRoundedRect(rect, 8, 8)
            painter.restore()
            if selected:
                clean.palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(theme.accent))
        painter.save()
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(self.visualRect(index)), 8, 8)
        painter.setClipPath(path, QtCore.Qt.ClipOperation.IntersectClip)
        super().drawRow(painter, clean, index)
        painter.restore()
        branches = QtCore.QRect(0, option.rect.top(), self.visualRect(index).left(), option.rect.height())
        self.drawBranches(painter, branches, index)

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
    def __init__(self, parent: QtWidgets.QWidget | None = None, *,
                 elide_mode=QtCore.Qt.TextElideMode.ElideRight) -> None:
        super().__init__(parent)
        self._elide_mode = elide_mode
        popup_view = ComboPopupListView(self)
        popup_view.setTextElideMode(elide_mode)
        popup_view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        popup_view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        popup_view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        popup_view.setSpacing(0)
        popup_view.setUniformItemSizes(True)
        popup_view.viewport().setAutoFillBackground(False)
        self.setView(popup_view)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QtCore.QSize(80, hint.height())

    def paintEvent(self, event):
        if self.isEditable():
            return super().paintEvent(event)
        option = QtWidgets.QStyleOptionComboBox()
        self.initStyleOption(option)
        field = self.style().subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_ComboBox, option,
            QtWidgets.QStyle.SubControl.SC_ComboBoxEditField, self,
        )
        option.currentText = self.fontMetrics().elidedText(
            self.currentText(), self._elide_mode, max(0, field.width()-2),
        )
        painter = QtWidgets.QStylePainter(self)
        painter.drawComplexControl(QtWidgets.QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QtWidgets.QStyle.ControlElement.CE_ComboBoxLabel, option)

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
        popup.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        popup.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        theme = active_theme()
        popup.setStyleSheet(
            "QFrame#ComboPopupContainer {"
            f"background: {theme.surface}; border: 1px solid {theme.input_border};"
            "border-radius: 12px; }"
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
            widget = item.widget()
            if widget is not None:
                # Removed rows must stop painting before deferred deletion runs.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
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
    """A pandas view with an optional, separate review include column."""

    includeChanged = QtCore.Signal(str, bool)

    def __init__(
        self,
        frame: pd.DataFrame | None = None,
        parent: QtCore.QObject | None = None,
        *,
        include_flags: dict[str, bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.include_flags = include_flags
        self._replace_frame(frame)

    def _replace_frame(self, frame: pd.DataFrame | None) -> None:
        self.frame = pd.DataFrame() if frame is None else frame.copy()
        # Qt can query thousands of cells during a header resize. Retain
        # column arrays so each lookup avoids pandas slicing and attrs copies.
        self._columns = tuple(column.array for _name, column in self.frame.items())
        self._spot_names = self.frame["spot_name"].array if "spot_name" in self.frame else None

    def spot_name(self, row: int) -> str:
        """Return the stable spot key for a source-model row."""
        return str(self._spot_names[row]) if self._spot_names is not None else ""

    def flags(self, index):
        """Allow checking only the optional include column."""
        flags = super().flags(index)
        if index.isValid() and self.include_flags is not None and index.column() == 0:
            flags |= QtCore.Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=QtCore.Qt.ItemDataRole.EditRole):
        """Request a review include change without editing the frame."""
        if (index.isValid() and self.include_flags is not None and index.column() == 0
                and role == QtCore.Qt.ItemDataRole.CheckStateRole):
            included = value == QtCore.Qt.CheckState.Checked or value == QtCore.Qt.CheckState.Checked.value
            self.include_flags[self.spot_name(index.row())] = included
            self.dataChanged.emit(index, index, [QtCore.Qt.ItemDataRole.CheckStateRole,
                                                QtCore.Qt.ItemDataRole.UserRole])
            self.includeChanged.emit(self.spot_name(index.row()), included)
            return True
        return False

    def set_frame(self, frame: pd.DataFrame | None) -> None:
        """Replace the displayed frame with one model reset notification."""
        self.beginResetModel()
        self._replace_frame(frame)
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
        if parent.isValid() or not len(self.frame.columns):
            return 0
        return len(self.frame.columns) + int(self.include_flags is not None)

    def data(
        self,
        index: QtCore.QModelIndex,
        role: int = int(QtCore.Qt.ItemDataRole.DisplayRole),
    ) -> object:
        if not index.isValid():
            return None
        offset = int(self.include_flags is not None)
        if offset and index.column() == 0:
            if role not in (QtCore.Qt.ItemDataRole.CheckStateRole, QtCore.Qt.ItemDataRole.UserRole):
                return None
            included = self.include_flags.get(self.spot_name(index.row()), True)
            if role == QtCore.Qt.ItemDataRole.CheckStateRole:
                return QtCore.Qt.CheckState.Checked if included else QtCore.Qt.CheckState.Unchecked
            return included
        if role not in (
            QtCore.Qt.ItemDataRole.DisplayRole, QtCore.Qt.ItemDataRole.TextAlignmentRole,
            QtCore.Qt.ItemDataRole.UserRole,
        ):
            return None
        value = self._columns[index.column() - offset][index.row()]
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
            if self.include_flags is not None and section == 0:
                return "Include"
            return str(self.frame.columns[section - int(self.include_flags is not None)])
        return str(section + 1)


_SPOT_NAMES_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 1
_SAMPLE_ROLE = _SPOT_NAMES_ROLE + 1


class _SpotReviewProxy(QtCore.QSortFilterProxyModel):
    """Filter spot names and samples, keeping tree groups in sample order."""

    def __init__(self, parent=None, *, grouped=False):
        super().__init__(parent)
        self._query = ""
        self._grouped = grouped
        self.setRecursiveFilteringEnabled(grouped)
        self.setSortRole(QtCore.Qt.ItemDataRole.UserRole)

    def set_spot_filter(self, text):
        """Apply a literal, case-insensitive spot or sample substring."""
        self.beginFilterChange()
        self._query = text.strip().casefold()
        self.endFilterChange()

    def filterAcceptsRow(self, row, parent):
        """Match spots directly and retain their sample ancestors recursively."""
        if not self._query:
            return super().filterAcceptsRow(row, parent)
        model = self.sourceModel()
        index = model.index(row, 0, parent)
        if self._grouped:
            if not parent.isValid():
                return False  # Recursive filtering retains matching children.
            names = index.data(_SPOT_NAMES_ROLE) or []
            sample = index.data(_SAMPLE_ROLE) or ""
        else:
            names = [model.spot_name(row)]
            sample = model.frame.iloc[row].get("sample", "")
        return any(self._query in str(value).casefold() for value in [*names, sample])

    def lessThan(self, left, right):
        """Sort samples ascending even when their spot sort is descending."""
        if self._grouped and not left.parent().isValid():
            model = self.sourceModel()
            a = model.index(left.row(), 0).data(_SAMPLE_ROLE)
            b = model.index(right.row(), 0).data(_SAMPLE_ROLE)
            return a > b if self.sortOrder() == QtCore.Qt.SortOrder.DescendingOrder else a < b
        return super().lessThan(left, right)


class _ConcordiaLegendSample(ItemSample):
    """A small colour swatch without the default 20-pixel row height."""

    def __init__(self, item):
        super().__init__(item)
        self.setFixedWidth(12)
        self.setFixedHeight(14)

    def boundingRect(self):
        return QtCore.QRectF(0, 0, 12, 14)

    def paint(self, painter, *args):
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(self.item.opts["brush"])
        painter.drawEllipse(QtCore.QPointF(6, 7), 3.25, 3.25)


class AxisZoomViewBox(pg.ViewBox):
    """Constrain zoom axes while allowing panning in both directions."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_zoom_axes("xy")

    def set_zoom_axes(self, mode: str) -> None:
        """Choose xy, x, or y for zooming without restricting panning."""
        self._zoom_axes = mode if mode in {"xy", "x", "y"} else "xy"
        self.setMouseEnabled(x=True, y=True)

    def zoom_axes(self) -> str:
        """Return the selected zoom axes."""
        return self._zoom_axes

    def _zoom_mask(self):
        return [self.state["mouseEnabled"][0] and self._zoom_axes != "y",
                self.state["mouseEnabled"][1] and self._zoom_axes != "x"]

    def wheelEvent(self, event, axis=None):
        """Zoom the chosen axes, or the axis item receiving the wheel."""
        mask = ([enabled and index == axis
                 for index, enabled in enumerate(self.state["mouseEnabled"])]
                if axis in (0, 1) else self._zoom_mask())
        if not any(mask):
            event.ignore()
            return
        scale = 1.02 ** (event.delta() * self.state["wheelScaleFactor"])
        factors = [scale if enabled else None for enabled in mask]
        transform = pg.functions.invertQTransform(self.childGroup.transform())
        center = pg.Point(transform.map(event.pos()))
        self._resetTarget()
        self.scaleBy(factors, center)
        event.accept()
        self.sigRangeChangedManually.emit(mask)

    def mouseDragEvent(self, event, axis=None):
        """Pan with left/middle drag and constrain right-drag zoom axes."""
        button = event.button()
        if (button == QtCore.Qt.MouseButton.LeftButton
                and self.state["mouseMode"] == self.RectMode and axis is None):
            # Keep pyqtgraph's rubber band and view history; showAxRect below
            # applies the zoom-axis choice when the rectangle is finished.
            super().mouseDragEvent(event, axis=axis)
            return

        event.accept()
        transform = pg.functions.invertQTransform(self.childGroup.transform())
        if button in (QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.MiddleButton):
            mask = [enabled and (axis is None or index == axis)
                    for index, enabled in enumerate(self.state["mouseEnabled"])]
            delta = (event.lastPos() - event.pos()) * np.array(mask, dtype=float)
            delta = transform.map(delta) - transform.map(pg.Point(0, 0))
            self._resetTarget()
            if any(mask):
                self.translateBy(x=delta.x() if mask[0] else None,
                                 y=delta.y() if mask[1] else None)
            self.sigRangeChangedManually.emit(mask)
        elif button == QtCore.Qt.MouseButton.RightButton:
            mask = [enabled and (axis is None or index == axis)
                    for index, enabled in enumerate(self._zoom_mask())]
            if self.state["aspectLocked"] is not False:
                mask[0] = False
            delta = event.screenPos() - event.lastScreenPos()
            factors = (1 + np.array(mask, dtype=float) * 0.02) ** np.array([-delta.x(), delta.y()])
            center = pg.Point(transform.map(event.buttonDownPos(button)))
            self._resetTarget()
            self.scaleBy(x=factors[0] if mask[0] else None,
                         y=factors[1] if mask[1] else None, center=center)
            self.sigRangeChangedManually.emit(mask)

    def showAxRect(self, rect, **kwargs):
        """Apply rectangle zoom only to the selected zoom axes."""
        rect = rect.normalized()
        mask = self._zoom_mask()
        limits = {}
        if mask[0]:
            limits["xRange"] = (rect.left(), rect.right())
        if mask[1]:
            limits["yRange"] = (rect.top(), rect.bottom())
        if self._zoom_axes != "xy":
            kwargs.setdefault("padding", 0)
        self.setRange(**limits, **kwargs)
        self.sigRangeChangedManually.emit(mask)


class _ConcordiaViewBox(AxisZoomViewBox):
    """Route spot clicks and selection rectangles to the review widget."""

    def __init__(self, review):
        super().__init__()
        self.review = weakref.proxy(review)

    def mouseClickEvent(self, event):
        if event.double() and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.review._reset_from_double_click(event.scenePos())
            event.accept()
        elif self.review.supports_select_mode() and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.review._select_at(event.scenePos(), event.modifiers())
            event.accept()
        else:
            super().mouseClickEvent(event)

    def mouseDragEvent(self, event, axis=None):
        if (self.review.interaction_mode() == "select" and axis is None
                and event.button() == QtCore.Qt.MouseButton.LeftButton):
            event.accept()
            if event.isFinish():
                self.rbScaleBox.hide()
                rect = QtCore.QRectF(self.mapToView(event.buttonDownPos()), self.mapToView(event.pos()))
                extend = bool(event.modifiers() & (QtCore.Qt.KeyboardModifier.ShiftModifier
                                                   | QtCore.Qt.KeyboardModifier.ControlModifier))
                self.review.select_rectangle(rect, extend=extend)
            else:
                self.updateScaleBox(event.buttonDownPos(), event.pos())
        else:
            super().mouseDragEvent(event, axis=axis)


class _ConcordiaRows:
    """Materialize only tooltip rows, preserving edits to already read rows."""

    def __init__(self, frame, positions):
        self.frame = frame
        self.positions = np.asarray(positions, dtype=int)
        self.rows = {}

    def __len__(self):
        return len(self.positions)

    def __getitem__(self, index):
        if index not in self.rows:
            self.rows[index] = self.frame.iloc[self.positions[index]].to_dict()
        return self.rows[index]


class _ConcordiaEllipse:
    """A per-row ellipse handle; the canvas owns the single graphics item."""

    def __init__(self, owner, x, y, pen, outline_color=None):
        self.owner = weakref.ref(owner)
        self.x, self.y = x, y
        self.opts = {"pen": pen}
        self.outline_color = outline_color
        self.visible = True

    def getData(self):
        return self.x, self.y

    def setPen(self, pen):
        if pen != self.opts["pen"]:
            self.opts["pen"] = pen
            self._mark_dirty()

    def setVisible(self, visible):
        if self.visible != visible:
            self.visible = visible
            self._mark_dirty()

    def _mark_dirty(self):
        owner = self.owner()
        if owner is not None:
            owner.dirty = True

    def isVisible(self):
        return self.visible


class _ConcordiaEllipseBatch(pg.GraphicsObject):
    """Cache ellipse paths by pen, with no per-ellipse scene objects."""

    def __init__(self):
        super().__init__()
        self.handles = []
        self.paths = []
        self.dirty = True
        self._bounds = QtCore.QRectF()
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def rebuild(self):
        if not self.dirty:
            return
        groups = {}
        for handle in self.handles:
            if not handle.visible:
                continue
            pen = handle.opts["pen"]
            color = pen.color()
            if handle.outline_color is not None:
                alpha = color.alpha()
                color = QtGui.QColor(handle.outline_color)
                color.setAlpha(alpha)
            key = color.rgba(), pen.widthF()
            groups.setdefault(key, []).append(handle)
        paths = []
        bounds = QtCore.QRectF()
        for (rgba, width), handles in groups.items():
            # Finite connections close each ellipse and skip its NaN separator.
            x = np.full((len(handles), len(handles[0].x) + 1), np.nan)
            y = np.full_like(x, np.nan)
            x[:, :-1] = [handle.x for handle in handles]
            y[:, :-1] = [handle.y for handle in handles]
            path = pg.arrayToQPath(x.ravel(), y.ravel(), connect="finite")
            pen = pg.mkPen(QtGui.QColor.fromRgba(rgba), width=width)
            paths.append((pen, path))
            bounds = bounds.united(path.boundingRect())
        self.prepareGeometryChange()
        self._bounds = bounds
        self.paths = paths
        self.dirty = False
        self.update()

    def boundingRect(self):
        return self._bounds

    def paint(self, painter, *_args):
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        for pen, path in self.paths:
            painter.setPen(pen)
            painter.drawPath(path)


class ConcordiaReviewWidget(RoundedGraphicsLayoutWidget):
    """Lightweight native concordia review with two-sigma ratio ellipses.

    The widget consumes already reduced ratio columns.  It deliberately does
    not calculate ages or corrections, keeping the GUI as a view over the
    shared :mod:`UPbRPy` pipeline rather than creating a second reduction
    implementation.
    """

    selectionChanged = QtCore.Signal(object)
    spotHovered = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._theme: ThemeSpec | None = None
        self._frame = pd.DataFrame()
        self._source_frame = self._frame
        self._source_positions = np.array([], dtype=int)
        self._geometry_cache = {}
        self._row_draw_cache = {}
        self._scatter_brush_cache = {}
        self._render_key = None
        self._ellipse_batch = None
        self._empty_message = None
        self._diagram = "wetherill"
        self._kind = "spot"
        self._include_calibration_uncertainty = True
        self._plot_item: pg.PlotItem | None = None
        self._zoom_mode = "xy"
        self._interaction_mode = "zoom"
        self.selected_spots: set[str] = set()
        self._scatter = None
        self._ellipses = []
        self._points = []
        self._point_xy = np.empty((0, 2))
        self._point_rows = []
        self._all_points = []
        self._all_point_rows = []
        self._point_errors = np.empty((0, 2))
        self._included_spots: set[str] | None = None
        self._legend_samples: list[str] = []
        self._metadata_by_spot = {}
        self._include_flags = {}
        self._spot_colors = {}
        self._sample_colors = {}
        self._colour_mode = "sample"
        self._depth_colormap = "plasma"
        self._colorbar = None
        self._concordia_age_ma = None
        self._age_marker = None
        self._default_range = None
        self._age_items = []
        self._label_ages = np.array([])
        self._label_step = 100.0
        self._curve = None
        self._legend = None
        self._hover_index = None
        self._hover_ring = None
        self._hover_text = None
        self._click_timer = QtCore.QElapsedTimer()
        self._click_selection = set()
        self._click_scene_pos = None
        self._label_timer = QtCore.QTimer(self)
        self._label_timer.setSingleShot(True)
        self._label_timer.timeout.connect(self._update_concordia_labels)
        self.scene().sigMouseMoved.connect(self._on_mouse_moved)

    def set_spot_context(self, frame: pd.DataFrame, selected_frame: pd.DataFrame,
                         include_flags: dict[str, bool]) -> None:
        """Retain whole-result metadata and stable palettes for spot review."""
        self._include_flags = include_flags
        self._metadata_by_spot = {}
        for source in (frame, selected_frame):
            for row in source.to_dict("records"):
                name = str(row.get("spot_name", ""))
                self._metadata_by_spot.setdefault(name, {}).update(
                    {key: value for key, value in row.items() if self._has_value(value)})
        names = list(self._metadata_by_spot)
        self._spot_colors = {name: _DIAG_SPOT_COLORS[i % len(_DIAG_SPOT_COLORS)]
                             for i, name in enumerate(names)}
        samples = sorted({str(row["sample"]) for row in self._metadata_by_spot.values()
                          if self._has_value(row.get("sample"))})
        self._sample_colors = {name: _REVIEW_SAMPLE_COLORS[i % len(_REVIEW_SAMPLE_COLORS)]
                               for i, name in enumerate(samples)}

    def supports_select_mode(self) -> bool:
        """Return whether this canvas shows selectable spot means."""
        return self._kind == "spot"

    def select_spots(self, spot_names, *, extend: bool = False, toggle: bool = False) -> None:
        """Select spot keys, optionally extending or toggling the selection."""
        names = set(spot_names)
        selected = (self.selected_spots ^ names if toggle else
                    self.selected_spots | names if extend else names)
        if selected != self.selected_spots:
            self.selected_spots = selected
            self._update_selection_style()
            self.selectionChanged.emit(set(selected))

    def select_rectangle(self, rect: QtCore.QRectF, *, extend: bool = False) -> None:
        """Select plotted spot centres inside a rectangle in data coordinates."""
        rect = rect.normalized()
        self.select_spots({name for name, x, y, _color in self._points
                           if rect.contains(QtCore.QPointF(x, y))}, extend=extend)

    def _select_at(self, scene_pos, modifiers):
        self._click_selection = set(self.selected_spots)
        self._click_scene_pos = QtCore.QPointF(scene_pos)
        self._click_timer.start()
        index = self.nearest_point(scene_pos, radius=10)
        names = {self._points[index][0]} if index is not None else set()
        control = bool(modifiers & QtCore.Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
        if names or not (control or shift):
            self.select_spots(names, extend=shift, toggle=control)

    def _scatter_brush(self, color):
        # ScatterPlotItem keys its symbol atlas by brush object identity.
        key = color.getRgbF()
        if key not in self._scatter_brush_cache:
            self._scatter_brush_cache[key] = pg.mkBrush(color)
        return self._scatter_brush_cache[key]

    def _update_selection_style(self, *, positions_changed=False):
        if self._scatter is None:
            return
        if self._kind == "depth":
            if positions_changed:
                self._scatter.setData(x=self._point_xy[:, 0], y=self._point_xy[:, 1],
                                      brush=[self._scatter_brush(p[3]) for p in self._points])
            return
        theme = self._theme
        brushes, pens, sizes = [], [], []
        for name, _x, _y, color in self._points:
            selected = name in self.selected_spots
            fill = QtGui.QColor(color)
            fill.setAlpha(100 if self.selected_spots and not selected else 255)
            brushes.append(pg.mkBrush(fill))
            pens.append(pg.mkPen(theme.accent if selected else theme.axis_text, width=2 if selected else 0.7))
            sizes.append(11 if selected else 8)
        if positions_changed:
            self._scatter.setData(
                x=self._point_xy[:, 0], y=self._point_xy[:, 1],
                brush=brushes, pen=pens, size=sizes,
            )
            return
        self._scatter.setBrush(brushes)
        self._scatter.setPen(pens)
        self._scatter.setSize(sizes)
        for name, ellipse, color in self._ellipses:
            selected = name in self.selected_spots
            line = QtGui.QColor(color)
            line.setAlpha(70 if self.selected_spots and not selected else 255 if selected else 155)
            ellipse.setPen(pg.mkPen(line, width=1.8 if selected else 1.0))
        if self._ellipse_batch is not None:
            self._ellipse_batch.rebuild()

    def apply_theme(self, theme: ThemeSpec) -> None:
        """Apply a theme and redraw the retained concordia data."""
        self._theme = theme
        self._render_key = None
        self._row_draw_cache.clear()
        self.setBackground(theme.plot_background)
        self._draw(preserve_view=True)

    def show_data(
        self,
        frame: pd.DataFrame,
        *,
        diagram: str,
        kind: str,
        include_calibration_uncertainty: bool,
        theme: ThemeSpec,
        reset_view: bool = True,
        colour_mode: str = "sample",
        depth_colormap: str = "plasma",
        included_spots: set[str] | None = None,
        source_frame: pd.DataFrame | None = None,
        row_positions: np.ndarray | None = None,
    ) -> None:
        """Display reduced rows, optionally retaining geometry from a source frame.

        A supplied source is immutable until replaced by new results. Positions
        identify the displayed rows within that source, including duplicate index
        labels. Without a source, each call reads the input anew.
        """
        source = frame if source_frame is None else source_frame
        positions = np.arange(len(source)) if row_positions is None else np.asarray(row_positions, dtype=int)
        if positions.ndim != 1 or len(positions) != len(frame):
            raise ValueError("row_positions must identify each displayed row within source_frame")
        if np.any(positions < 0) or np.any(positions >= len(source)):
            raise ValueError("row_positions must be within source_frame")
        if source_frame is None or source is not self._source_frame:
            self._geometry_cache.clear()
            self._row_draw_cache.clear()
            self._scatter_brush_cache.clear()
            self._render_key = None
        self._source_frame = source
        render_key = (id(source), diagram, kind, bool(include_calibration_uncertainty),
                      id(theme), colour_mode, depth_colormap,
                      tuple(self._sample_colors.items()), tuple(self._spot_colors.items()))
        same_render = self._render_key == render_key and self._plot_item is not None
        same_rows = np.array_equal(positions, self._source_positions)
        previous_included = self._included_spots
        self._source_positions = positions
        preserve_view = (not reset_view and self._diagram == diagram and self._kind == kind)
        self._frame = frame.copy()
        self._included_spots = included_spots
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
        self._colour_mode = colour_mode
        self._depth_colormap = depth_colormap
        if self._kind == "spot" and "sample" in frame:
            # Standalone canvases can also colour their complete input frame.
            samples = set(frame["sample"].dropna().astype(str))
            if not samples.issubset(self._sample_colors):
                samples = sorted(samples | self._sample_colors.keys())
                self._sample_colors = {name: _REVIEW_SAMPLE_COLORS[i % len(_REVIEW_SAMPLE_COLORS)]
                                       for i, name in enumerate(samples)}
        self.setBackground(theme.plot_background)
        self._render_key = render_key
        if same_render:
            if same_rows:
                self._included_spots = previous_included
                self.set_included_spots(included_spots)
            else:
                hovered = self._points[self._hover_index][0] if self._hover_index is not None else None
                self._set_hover(None)
                self._populate_data()
                self._default_range = self._initial_range(*self._concordia_coordinates)
                self._update_sample_legend()
                if hovered is not None:
                    self._set_hover(next((i for i, p in enumerate(self._points) if p[0] == hovered), None))
            if reset_view:
                self.restore_default_view()
            return
        self._row_draw_cache.clear()
        self._draw(preserve_view=preserve_view)

    def set_included_spots(self, names: set[str] | None) -> None:
        """Update spot visibility while retaining the plotted geometry and view."""
        previous = self._included_spots
        if previous == names:
            return
        self._included_spots = names
        if self._scatter is None:
            return
        hovered = self._points[self._hover_index][0] if self._hover_index is not None else None
        if hovered is not None and names is not None and hovered not in names:
            self._set_hover(None)
        self._update_visible_points()
        changed = previous ^ names if previous is not None and names is not None else None
        for name, ellipse, _color in self._ellipses:
            if changed is None or name in changed:
                ellipse.setVisible(names is None or name in names)
        self._ellipse_batch.rebuild()
        self._update_selection_style(positions_changed=True)
        old_rect = self._legend.sceneBoundingRect() if self._legend is not None else None
        self._update_sample_legend()
        self._position_sample_legend()
        new_rect = self._legend.sceneBoundingRect() if self._legend is not None else None
        if old_rect != new_rect:
            self._schedule_labels()
        self._default_range = self._initial_range(*self._concordia_coordinates)
        if hovered is not None and (names is None or hovered in names):
            self._hover_index = next(i for i, point in enumerate(self._points) if point[0] == hovered)
            self._position_hover()

    def _update_visible_points(self) -> None:
        included = self._included_spots
        indices = [
            index for index, point in enumerate(self._all_points)
            if included is None or point[0] in included
        ]
        self._points = [self._all_points[index] for index in indices]
        if isinstance(self._all_point_rows, _ConcordiaRows):
            self._point_rows = _ConcordiaRows(self._all_point_rows.frame, self._all_point_rows.positions[indices])
        else:
            self._point_rows = []
        self._point_xy = np.asarray(
            [(x, y) for _name, x, y, _color in self._points], dtype=float
        ).reshape(-1, 2)
        self._visible_errors = self._point_errors[indices]

    def set_concordia_age(self, age_ma: float | None) -> None:
        """Mark the fitted age on the curve without fitting or changing the view."""
        self._concordia_age_ma = age_ma
        if self._plot_item is None or self._theme is None:
            return
        if self._age_marker is None:
            self._age_marker = pg.ScatterPlotItem(symbol="d", size=10)
            self._age_marker.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            self._age_marker.setZValue(20)
            self._plot_item.addItem(self._age_marker, ignoreBounds=True)
        if age_ma is None or self._kind != "spot":
            self._age_marker.setData([], [])
            return
        point = wetherill_point if self._diagram == "wetherill" else tera_wasserburg_point
        x, y = point(np.array([age_ma]))
        self._age_marker.setData(x, y, brush=pg.mkBrush(self._theme.accent),
                                 pen=pg.mkPen(self._theme.axis_text, width=.7))

    @staticmethod
    def depth_color_map(name: str) -> pg.ColorMap:
        """Return a built-in map oriented with its lighter endpoint at time zero."""
        source = pg.colormap.get(name)
        colors = source.color.copy()
        qcolors = source.mapToQColor(source.pos)

        def luminance(rgb):
            rgb = rgb[:3]
            linear = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
            return float(linear @ np.array([.2126, .7152, .0722]))

        if luminance(colors[0]) < luminance(colors[-1]):
            return pg.ColorMap(1 - source.pos[::-1], qcolors[::-1], name=name)
        return pg.ColorMap(source.pos.copy(), qcolors, name=name)

    def _depth_brushes(self, frame, theme):
        use_time = "signal_relative_time" in frame
        times = (pd.to_numeric(frame["signal_relative_time"], errors="coerce").to_numpy(dtype=float)
                 if use_time else np.arange(len(frame), dtype=float))
        finite = times[np.isfinite(times)]
        low, high = (float(finite.min()), float(finite.max())) if finite.size else (0., 1.)
        cmap = self.depth_color_map(self._depth_colormap)
        normalized = (times - low) / max(high - low, np.finfo(float).eps)
        brushes = cmap.mapToQColor(np.clip(np.nan_to_num(normalized), 0, 1))
        for index in np.flatnonzero(~np.isfinite(normalized)):
            brushes[index] = pg.mkColor(theme.muted)
        if self._colorbar is None:
            self._colorbar = pg.ColorBarItem(width=10, interactive=False, orientation="horizontal",
                                             colorMapMenu=False)
            self.addItem(self._colorbar, 1, 0)
            self.ci.layout.setRowStretchFactor(0, 1)
            self.ci.layout.setRowStretchFactor(1, 0)
            self._colorbar.setMaximumHeight(65)
        self._colorbar.show()
        self._colorbar.setColorMap(cmap)
        self._colorbar.setLevels((low, high if high > low else low + 1))
        for name in ("left", "right", "top", "bottom"):
            axis = self._colorbar.getAxis(name)
            axis.setPen(pg.mkPen(theme.axis_line))
            axis.setTextPen(pg.mkPen(theme.axis_text))
        label = "Time in ablation (s)" if use_time else "Sweep"
        self._colorbar.axis.setLabel(label, color=theme.axis_text)

        def endpoint(value, depth):
            text = f"{value:.2g} s" if use_time else f"sweep {int(value) + 1}"
            return value, f"{text} ({depth})"

        ticks = [endpoint(low, "shallow")]
        if high > low:
            ticks.append(endpoint(high, "deep"))
        # Tick text is centred at each endpoint. Reserve its overhang inside
        # the canvas and allow the axis to draw the full labels.
        metrics = QtGui.QFontMetricsF(self._colorbar.axis.style["tickFont"] or self.font())
        margin = int(max(metrics.horizontalAdvance(text) for _, text in ticks) / 2) + 10
        self._colorbar.layout.setContentsMargins(margin, 0, margin, 0)
        self._colorbar.axis.setStyle(hideOverlappingLabels=margin)
        self._colorbar.axis.setTicks([ticks])
        return brushes

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
        if not all(np.isfinite([x, y, x_2e, y_2e])):
            return np.array([]), np.array([])
        sx = float(x_2e) / 2.0
        sy = float(y_2e) / 2.0
        if sx <= 0 or sy <= 0:
            return np.array([]), np.array([])
        correlation = float(np.clip(0.0 if np.isnan(rho) else rho, -0.999, 0.999))
        covariance = np.array(
            [[sx * sx, correlation * sx * sy],
             [correlation * sx * sy, sy * sy]],
            dtype=float,
        )
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        eigenvalues = np.maximum(eigenvalues, 0.0)
        # Include all four cardinal angles, including the closing point.
        count = max(4, int(np.ceil((points - 1) / 4)) * 4)
        angle = np.linspace(0.0, 2.0 * np.pi, count + 1)
        circle = np.vstack((np.cos(angle), np.sin(angle)))
        # The supplied columns are absolute 2SE values, so multiplying the
        # one-SE covariance axes by two returns the requested 2SE ellipse.
        offsets = eigenvectors @ np.diag(2.0 * np.sqrt(eigenvalues)) @ circle
        return x + offsets[0], y + offsets[1]

    @staticmethod
    def ellipse_coordinates_batch(x, y, x_2e, y_2e, rho, *, points=48) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return stacked two-SE ellipses and a mask of valid input rows.

        Invalid rows have NaN vertices. The angle grid and covariance
        eigendecomposition match :meth:`ellipse_coordinates`.
        """
        x, y, x_2e, y_2e, rho = np.broadcast_arrays(
            *[np.asarray(value, dtype=float) for value in (x, y, x_2e, y_2e, rho)])
        x, y, x_2e, y_2e, rho = [a.reshape(-1) for a in (x, y, x_2e, y_2e, rho)]
        valid = (np.isfinite(x) & np.isfinite(y) & np.isfinite(x_2e) & np.isfinite(y_2e)
                 & (x_2e > 0) & (y_2e > 0))
        count = max(4, int(np.ceil((points - 1) / 4)) * 4)
        ex = np.full((len(x), count + 1), np.nan)
        ey = np.full_like(ex, np.nan)
        sx, sy = x_2e[valid] / 2, y_2e[valid] / 2
        correlation = np.clip(np.where(np.isnan(rho[valid]), 0., rho[valid]), -.999, .999)
        covariance = np.empty((len(sx), 2, 2))
        covariance[:, 0, 0], covariance[:, 1, 1] = sx * sx, sy * sy
        covariance[:, 0, 1] = covariance[:, 1, 0] = correlation * sx * sy
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        axes = np.zeros_like(covariance)
        axes[:, 0, 0], axes[:, 1, 1] = (2 * np.sqrt(np.maximum(eigenvalues, 0.))).T
        angle = np.linspace(0., 2. * np.pi, count + 1)
        circle = np.vstack((np.cos(angle), np.sin(angle)))
        offsets = eigenvectors @ axes @ circle
        ex[valid], ey[valid] = x[valid, None] + offsets[:, 0], y[valid, None] + offsets[:, 1]
        return ex, ey, valid

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

    def _draw(self, *, preserve_view=False) -> None:
        retained_range = self._plot_item.vb.viewRange() if preserve_view and self._plot_item else None
        hovered = (self._points[self._hover_index][0] if self._hover_index is not None else None)
        self._label_timer.stop()
        if self._hover_index is not None:
            self._set_hover(None)
        # GraphicsLayout.clear() only detaches PlotItems; their parentless
        # menus and scroll areas depend on Python wrapper lifetime. Retain
        # one plot and give its QWidget controls the canvas as Qt owner.
        if self._plot_item is not None:
            self._plot_item.clear()
            for overlay in (self._hover_text, self._legend):
                if overlay is not None:
                    self.scene().removeItem(overlay)
                    overlay.deleteLater()
        self._scatter = None
        self._ellipse_batch = None
        self._empty_message = None
        self._age_marker = None
        self._ellipses = []
        self._points = []
        self._point_xy = np.empty((0, 2))
        self._point_rows = []
        self._all_points = []
        self._all_point_rows = []
        self._hover_index = None
        self._age_items = []
        self._label_ages = np.array([])
        self._default_range = None
        self._legend = None
        self._legend_samples = []
        if self._colorbar is not None:
            self._colorbar.hide()
        theme = self._theme
        if theme is None:
            return
        if self._plot_item is None:
            self._plot_item = self.addPlot(0, 0, viewBox=_ConcordiaViewBox(self))
            for menu in (self._plot_item.ctrlMenu, self._plot_item.vb.menu):
                menu.setParent(self, menu.windowFlags())
            self._plot_item.vb.sigRangeChanged.connect(self._schedule_labels)
            self._plot_item.vb.sigResized.connect(self._schedule_labels)
        plot = self._plot_item
        _make_diag_axis(plot, theme)
        plot.setAspectLocked(False)
        tick_font = QtGui.QFont()
        tick_font.setPointSizeF(12.0)
        for axis_name in ("left", "bottom"):
            plot.getAxis(axis_name).enableAutoSIPrefix(False)
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
        self._concordia_coordinates = concordia_x, concordia_y
        self._curve = plot.plot(
            concordia_x,
            concordia_y,
            pen=pg.mkPen(theme.muted, width=1.4),
        )
        self._hover_ring = pg.ScatterPlotItem(size=16, pen=pg.mkPen(theme.accent, width=2),
                                             brush=pg.mkBrush(None))
        self._hover_ring.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        plot.addItem(self._hover_ring, ignoreBounds=True)
        hover_background = theme_color(theme.surface)
        hover_background.setAlpha(245)
        self._hover_text = pg.TextItem(color=theme.axis_text, fill=pg.mkBrush(hover_background),
                                      border=pg.mkPen(theme_color(theme.input_border)), anchor=(0, 0))
        self._hover_text.setZValue(30)
        self._hover_ring.setZValue(29)
        self._hover_text.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        # A viewport child stays above the legend and does not enter auto-range.
        self._hover_text.setParentItem(plot.vb)
        self._hover_text.hide()

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
            self._empty_message = message
            message.setPos(0.5, 0.5)
            self._default_range = [[0, 1], [0, 1]]
            plot.setRange(xRange=(0, 1), yRange=(0, 1), padding=0)
            if retained_range is not None:
                plot.setRange(xRange=retained_range[0], yRange=retained_range[1], padding=0)
            self._apply_zoom_mode()
            self._apply_interaction_mode()
            self._schedule_labels()
            return

        self._populate_data()

        self._default_range = self._initial_range(concordia_x, concordia_y)
        view = retained_range if retained_range is not None else self._default_range
        plot.setRange(xRange=view[0], yRange=view[1], padding=0)
        self._update_sample_legend()
        self.set_concordia_age(self._concordia_age_ma)
        if hovered is not None:
            index = next((i for i, point in enumerate(self._points) if point[0] == hovered), None)
            self._set_hover(index)
        self._schedule_labels()
        self._apply_zoom_mode()
        self._apply_interaction_mode()

    def _geometry(self):
        """Cache full-source ratio arrays and vertices for each uncertainty branch."""
        key = self._diagram, self._kind, self._include_calibration_uncertainty
        if key in self._geometry_cache:
            return self._geometry_cache[key]
        frame = self._source_frame
        x_column, y_column, sx_column, sy_column = self._ratio_columns()

        def values(column):
            return (pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
                    if column is not None and column in frame else np.full(len(frame), np.nan))

        x, y = values(x_column), values(y_column)
        if sx_column and sy_column:
            sx, sy = values(sx_column), values(sy_column)
        else:
            sx, sy = np.full(len(frame), np.nan), np.full(len(frame), np.nan)
        branch = "with_calibration" if self._include_calibration_uncertainty else "no_calibration"
        stem = "err_corr" if self._diagram == "wetherill" else "tw_rho"
        column = next((c for c in (f"{stem}_{branch}", stem) if c in frame), None)
        rho = values(column) if column is not None else np.zeros(len(frame))
        ex, ey, valid = self.ellipse_coordinates_batch(x, y, sx, sy, rho)
        result = x, y, sx, sy, ex, ey, valid, rho
        self._geometry_cache[key] = result
        return result

    def _populate_data(self):
        """Replace visible rows without replacing the scene or recomputing vertices."""
        frame, theme = self._frame, self._theme
        x, y, sx, sy, ex, ey, valid, _rho = self._geometry()
        if self._empty_message is not None:
            self._empty_message.setVisible(frame.empty)
        elif frame.empty:
            self._empty_message = pg.TextItem("No compatible corrected ratios are available for this view.",
                                              color=theme.muted, anchor=(.5, .5))
            self._plot_item.addItem(self._empty_message)
            self._empty_message.setPos(.5, .5)
        positions = self._source_positions
        if self._ellipse_batch is None:
            self._ellipse_batch = _ConcordiaEllipseBatch()
            self._plot_item.addItem(self._ellipse_batch, ignoreBounds=True)
        cache_key = self._render_key, positions.tobytes()
        cached = self._row_draw_cache.get(cache_key)
        if cached is None:
            if self._kind == "depth":
                brushes = self._depth_brushes(frame, theme)
                # Only ellipse outlines are quantised; points keep full map precision.
                cmap = self.depth_color_map(self._depth_colormap)
                palette = cmap.mapToQColor(np.linspace(0, 1, 64))
                times = (pd.to_numeric(frame["signal_relative_time"], errors="coerce").to_numpy(dtype=float)
                         if "signal_relative_time" in frame else np.arange(len(frame), dtype=float))
                finite_times = times[np.isfinite(times)]
                low, high = ((finite_times.min(), finite_times.max()) if finite_times.size else (0., 1.))
                normalized = (times - low) / max(high - low, np.finfo(float).eps)
                levels = np.rint(np.clip(np.nan_to_num(normalized), 0, 1) * 63).astype(int)
                outlines = [palette[level] if np.isfinite(time) else pg.mkColor(theme.muted)
                            for level, time in zip(levels, times)]
            else:
                samples = frame["sample"].astype(str) if "sample" in frame else [""] * len(frame)
                names = frame["spot_name"].astype(str) if "spot_name" in frame else [""] * len(frame)
                brushes = [
                    pg.mkColor(self._sample_colors.get(sample, theme.muted)
                               if self._colour_mode == "sample" else theme.accent
                               if self._colour_mode == "single" else
                               self._spot_colors.get(name, _DIAG_SPOT_COLORS[i % len(_DIAG_SPOT_COLORS)]))
                    for i, (sample, name) in enumerate(zip(samples, names))
                ]
                outlines = [None] * len(frame)
            names = frame["spot_name"].astype(str).to_numpy() if "spot_name" in frame else [""] * len(frame)
            finite = np.isfinite(x[positions]) & np.isfinite(y[positions])
            indices = np.flatnonzero(finite)
            point_positions = positions[finite]
            points = [(names[i], float(x[p]), float(y[p]), brushes[i])
                      for i, p in zip(indices, point_positions)]
            rows = _ConcordiaRows(self._source_frame, point_positions)
            ellipses = []
            pens = {}
            for i, p in zip(indices, point_positions):
                if valid[p]:
                    color = brushes[i]
                    rgba = color.rgba()
                    if rgba not in pens:
                        line = QtGui.QColor(color)
                        line.setAlpha(155)
                        pens[rgba] = pg.mkPen(line, width=1.0)
                    handle = _ConcordiaEllipse(self._ellipse_batch, ex[p], ey[p], pens[rgba], outlines[i])
                    ellipses.append((names[i], handle, color))
            errors = np.column_stack((sx[point_positions], sy[point_positions]))
            cached = points, rows, ellipses, errors
            # Bound retained subset colours and handles during repeated filtering.
            if len(self._row_draw_cache) >= 4:
                self._row_draw_cache.pop(next(iter(self._row_draw_cache)))
            self._row_draw_cache[cache_key] = cached
        elif self._kind == "depth":
            # Update levels and labels as well when a previous subset is restored.
            self._depth_brushes(frame, theme)
        self._all_points, self._all_point_rows, self._ellipses, self._point_errors = cached
        self._update_visible_points()
        self._ellipse_batch.handles = [handle for _, handle, _ in self._ellipses]
        self._ellipse_batch.dirty = True
        for name, handle, _ in self._ellipses:
            handle.setVisible(self._included_spots is None or name in self._included_spots)
        if self._scatter is None:
            self._scatter = pg.ScatterPlotItem(symbol="o", size=6 if self._kind == "depth" else 8,
                                             pen=pg.mkPen(theme.axis_text, width=.7))
            self._plot_item.addItem(self._scatter)
            self._scatter.sigClicked.connect(self._spot_clicked)
        self._update_selection_style(positions_changed=True)
        if self._kind == "spot":
            self._update_selection_style()
        self._ellipse_batch.rebuild()

    def set_zoom_mode(self, mode: str) -> None:
        """Restrict mouse zooming to both axes or one selected axis."""
        self._zoom_mode = mode if mode in {"xy", "x", "y"} else "xy"
        self._apply_zoom_mode()

    def zoom_mode(self) -> str:
        """Return the current concordia zoom-axis mode."""
        return self._zoom_mode

    def set_interaction_mode(self, mode: str) -> None:
        """Choose box zoom, pan, or spot rectangle selection."""
        allowed = {"pan", "zoom", "select"} if self.supports_select_mode() else {"pan", "zoom"}
        self._interaction_mode = mode if mode in allowed else "zoom"
        self._apply_interaction_mode()

    def _spot_clicked(self, _scatter, _points, event):
        if event.double():
            self._reset_from_double_click(event.scenePos())
        elif self.supports_select_mode():
            self._select_at(event.scenePos(), event.modifiers())

    def interaction_mode(self) -> str:
        """Return the current concordia mouse interaction."""
        return self._interaction_mode

    def _apply_zoom_mode(self) -> None:
        if self._plot_item is None:
            return
        self._plot_item.vb.set_zoom_axes(self._zoom_mode)

    def _apply_interaction_mode(self) -> None:
        if self._plot_item is None:
            return
        self._plot_item.vb.setMouseMode(
            pg.ViewBox.PanMode
            if self._interaction_mode == "pan"
            else pg.ViewBox.RectMode
        )

    def restore_default_view(self) -> None:
        """Reset the data-plus-concordia range without rebuilding plot items."""
        if self._plot_item is not None and self._default_range is not None:
            self._plot_item.setRange(xRange=self._default_range[0],
                                     yRange=self._default_range[1], padding=0)

    def mouseDoubleClickEvent(self, event):
        """Reset on a native double-click anywhere inside the plot area."""
        scene_pos = self.mapToScene(event.position().toPoint())
        if (event.button() == QtCore.Qt.MouseButton.LeftButton and self._plot_item is not None
                and self._plot_item.vb.sceneBoundingRect().contains(scene_pos)):
            self._reset_from_double_click(scene_pos)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def _reset_from_double_click(self, scene_pos):
        # Qt delivers the first click before it recognizes a double-click.
        # Undo that click's selection edit while keeping ordinary clicks immediate.
        if (self.supports_select_mode() and self._click_timer.isValid()
                and self._click_timer.elapsed() <= QtWidgets.QApplication.doubleClickInterval()
                and (scene_pos - self._click_scene_pos).manhattanLength()
                <= QtWidgets.QApplication.startDragDistance()):
            self.select_spots(self._click_selection)
        self.restore_default_view()

    def _schedule_labels(self, *_args):
        if not self.closed and self.isVisible() and not self._label_timer.isActive():
            self._label_timer.start(0)

    def _visible_age_interval(self, ranges):
        """Invert each monotonic ratio to find the visible age interval."""
        point = wetherill_point if self._diagram == "wetherill" else tera_wasserburg_point
        limits = np.sort(np.asarray(ranges, dtype=float), axis=1)
        ends = np.asarray(point(np.array([.001, 4567.3])))
        increasing = ends[:, 1] > ends[:, 0]
        lo, hi = np.full((2, 2), .001), np.full((2, 2), 4567.3)
        # Both limits of both axes are inverted in the same 48 evaluations.
        for _ in range(48):
            mid = (lo + hi) / 2
            px, py = point(mid)
            values = np.stack((px[0], py[1]))
            advance = (values < limits) == increasing[:, None]
            lo = np.where(advance, mid, lo)
            hi = np.where(advance, hi, mid)
        ages = (lo + hi) / 2
        return max(.001, ages.min(axis=1).max()), min(4567.3, ages.max(axis=1).min())

    @QtCore.Slot()
    def _update_concordia_labels(self):
        if self._plot_item is None or self._curve is None:
            return
        plot, theme = self._plot_item, self._theme
        self._position_sample_legend()
        legend_rect = self._legend.sceneBoundingRect() if self._legend is not None else QtCore.QRectF()
        for item in self._age_items:
            plot.removeItem(item)
        self._age_items = []
        self._label_ages = np.array([])
        ranges = plot.vb.viewRange()
        lo, hi = self._visible_age_interval(ranges)
        point = wetherill_point if self._diagram == "wetherill" else tera_wasserburg_point
        if hi <= lo:
            self._curve.setData([], [])
            return
        margin = max((hi - lo) * 0.15, 0.001)
        ages = np.linspace(max(.001, lo - margin), min(4567.3, hi + margin), 1200)
        x, y = point(ages)
        self._curve.setData(x, y)
        candidates = visible_concordia_age_labels(
            "Wetherill" if self._diagram == "wetherill" else "TW",
            ages, x, y, ranges[0], ranges[1], maximum=12,
        )
        step = (nice_step(round(float(np.ptp(candidates)) / (len(candidates) - 1), 10))
                if len(candidates) > 1 else nice_step(max(hi - lo, 1) / 10))
        self._label_step = step
        previous = []
        accepted = []
        for age in candidates:
            px, py = point(age)
            scene_pos = plot.vb.mapViewToScene(QtCore.QPointF(float(px), float(py)))
            label = pg.TextItem(format_age_label(age, step), color=theme.muted, anchor=(0, 0))
            font = QtGui.QFont()
            font.setPointSizeF(10.5)
            label.setFont(font)
            plot.addItem(label, ignoreBounds=True)
            self._place_text(label, scene_pos + QtCore.QPointF(5, -8))
            rect = label.sceneBoundingRect().adjusted(-4, -3, 4, 3)
            if (not plot.vb.sceneBoundingRect().contains(label.sceneBoundingRect())
                    or rect.intersects(legend_rect)
                    or any(rect.intersects(other) for other in previous)):
                plot.removeItem(label)
                continue
            marker = pg.ScatterPlotItem(x=[px], y=[py], size=4,
                                        pen=pg.mkPen(theme.muted), brush=pg.mkBrush(theme.muted))
            marker.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            label.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            plot.addItem(marker, ignoreBounds=True)
            previous.append(rect)
            accepted.append(age)
            self._age_items.extend((label, marker))
        self._label_ages = np.asarray(accepted)
        if self._hover_index is not None:
            self._position_hover()

    def _place_text(self, item, scene_pos):
        item.updateTransform()
        bounds = self._plot_item.vb.sceneBoundingRect().adjusted(3, 3, -3, -3)
        rect = item.sceneBoundingRect()
        x = max(bounds.left(), min(scene_pos.x(), bounds.right() - rect.width()))
        y = max(bounds.top(), min(scene_pos.y(), bounds.bottom() - rect.height()))
        item.setPos(item.parentItem().mapFromScene(QtCore.QPointF(x, y)))

    def _update_sample_legend(self):
        if self._kind != "spot" or self._colour_mode != "sample":
            return
        samples = sorted({
            str(value) for value in (
                self._point_rows.frame["sample"].iloc[self._point_rows.positions]
                if isinstance(self._point_rows, _ConcordiaRows) and "sample" in self._point_rows.frame else []
            ) if self._has_value(value)
        })
        if samples == self._legend_samples:
            return
        self._legend_samples = samples
        if self._legend is not None:
            self.scene().removeItem(self._legend)
            self._legend.deleteLater()
            self._legend = None
        if len(samples) < 2:
            return
        self._legend = pg.LegendItem(labelTextColor=self._theme.axis_text,
                                     labelTextSize="8.5pt", verSpacing=-1, horSpacing=3,
                                     sampleType=_ConcordiaLegendSample,
                                     brush=pg.mkBrush(theme_color(self._theme.glass_strong)),
                                     pen=pg.mkPen(theme_color(self._theme.input_border)))
        self._legend.setParentItem(self._plot_item.vb)
        self._legend.setZValue(20)
        self._legend.layout.setContentsMargins(5, 4, 5, 4)
        self._legend.setColumnCount(2 if len(samples) > 10 else 1)
        for sample in samples:
            swatch = pg.ScatterPlotItem(size=7, pen=pg.mkPen(None),
                                        brush=pg.mkBrush(self._sample_colors[sample]))
            self._legend.addItem(swatch, escape(sample))
            label = self._legend.items[-1][1]
            label.item.document().setDocumentMargin(0)
            label.updateMin()
        self._legend.layout.activate()
        self._legend.updateSize()

    def _position_sample_legend(self):
        """Choose the corner covering the fewest drawn centres in this view."""
        if self._legend is None:
            return
        vb = self._plot_item.vb
        bounds = vb.boundingRect().adjusted(8, 8, -8, -8)
        size = self._legend.boundingRect().size()
        corners = [QtCore.QPointF(x, y)
                   for y in (bounds.top(), bounds.bottom() - size.height())
                   for x in (bounds.right() - size.width(), bounds.left())]
        boxes = [QtCore.QRectF(corner, size) for corner in corners]
        # Map four boxes, then count all centres together with numpy.
        limits = [vb.childGroup.mapRectFromParent(box) for box in boxes]
        low = np.array([[box.left(), box.top()] for box in limits])
        high = np.array([[box.right(), box.bottom()] for box in limits])
        counts = np.all((self._point_xy[:, None, :] >= low)
                        & (self._point_xy[:, None, :] <= high), axis=2).sum(axis=0)
        self._legend.setPos(corners[int(np.argmin(counts))])

    def nearest_point(self, scene_pos, *, radius: float = 6) -> int | None:
        """Return the nearest drawn centre within a screen-space pixel radius."""
        if self._plot_item is None or not len(self._point_xy):
            return None
        vb = self._plot_item.vb
        if not vb.sceneBoundingRect().contains(scene_pos):
            return None
        pos = vb.mapSceneToView(scene_pos)
        scale = np.abs(vb.viewPixelSize())
        if not np.all(np.isfinite(scale)) or np.any(scale <= 0):
            return None
        delta = (self._point_xy - [pos.x(), pos.y()]) / scale
        distances = np.einsum("ij,ij->i", delta, delta)
        index = int(np.argmin(distances))
        return index if distances[index] <= radius * radius else None

    @staticmethod
    def _has_value(value):
        if isinstance(value, (int, float, np.number)):
            return bool(np.isfinite(value))
        return value is not None and str(value).strip() not in {"", "nan", "None", "<NA>"}

    def tooltip_text(self, index: int) -> str:
        """Build a plain-text tooltip for one drawn spot or corrected sweep."""
        row = self._point_rows[index].copy()
        name = self._points[index][0]
        if self._kind == "spot":
            row.update(self._metadata_by_spot.get(name, {}))
        lines = [name] if name else []

        def add(label, value, suffix=""):
            if self._has_value(value):
                lines.append(f"{label}: {value}{suffix}")

        def measured(label, column, suffix):
            error_column = self.uncertainty_column(pd.DataFrame(columns=list(row)), column,
                                                  self._include_calibration_uncertainty)
            text = format_value_with_uncertainty(row.get(column), row.get(error_column))
            add(label, text, suffix)

        if self._kind == "depth":
            time = row.get("signal_relative_time")
            if self._has_value(time):
                add("Sweep time", f"{float(time):.3g}", " s")
            for ratio in ("206Pb/238U", "207Pb/235U", "238U/206Pb", "207Pb/206Pb", "208Pb/232Th"):
                measured(ratio, f"{ratio}_fractionation_corrected", "")
            return "\n".join(lines)
        add("Sample", row.get("sample"))
        grain = row.get("grain")
        if self._has_value(grain):
            add("Grain", grain)
        else:
            number = row.get("spot_number")
            match = re.search(r"(\d+)$", name)
            add("Spot number", number if self._has_value(number) else match.group(1) if match else None)
        preferred = format_value_with_uncertainty(
            row.get("preferred_date"), row.get(self.uncertainty_column(
                pd.DataFrame(columns=list(row)), "preferred_date", self._include_calibration_uncertainty)))
        details = []
        method = row.get("preferred_date_method")
        if self._has_value(method):
            details.append(str(method))
        passed = row.get("preferred_date_pass")
        if self._has_value(passed):
            details.append("pass" if passed is True or str(passed).lower() == "true" else "fail")
        add("Preferred date", preferred, " Ma" + (f" ({', '.join(details)})" if details else ""))
        for ratio in ("206Pb/238U", "207Pb/235U", "207Pb/206Pb"):
            measured(f"{ratio} date", f"{ratio}_age", " Ma")
        for key, label, spec, suffix in (("disc%", "Discordance", ".1f", "%"),
                                         ("U", "U", ".3g", " ug/g"),
                                         ("Th", "Th", ".3g", " ug/g"),
                                         ("Th/U", "Th/U", ".3g", "")):
            value = row.get(key)
            if self._has_value(value):
                add(label, format(float(value), spec), suffix)
        add("Automatic flags", row.get("automatic_flags"))
        add("Review flag", row.get("review_flag"))
        add("Included in review", "yes" if self._include_flags.get(name, True) else "no")
        return "\n".join(lines)

    @QtCore.Slot(object)
    def _on_mouse_moved(self, scene_pos):
        self._set_hover(self.nearest_point(scene_pos))

    def _set_hover(self, index):
        if index == self._hover_index:
            return
        self._hover_index = index
        if index is None:
            self._hover_ring.setData([], [])
            self._hover_text.hide()
            if self._kind == "spot":
                self.spotHovered.emit("")
            return
        name, x, y, _color = self._points[index]
        self._hover_ring.setData([x], [y])
        self._hover_text.setHtml('<div style="color:' + self._theme.axis_text +
                                 '; padding:3px; font-size:11px">' +
                                 escape(self.tooltip_text(index)).replace("\n", "<br>") + '</div>')
        self._hover_text.show()
        self._position_hover()
        if self._kind == "spot":
            self.spotHovered.emit(name)

    def _position_hover(self):
        _name, x, y, _color = self._points[self._hover_index]
        scene_pos = self._plot_item.vb.mapViewToScene(QtCore.QPointF(x, y))
        self._place_text(self._hover_text, scene_pos + QtCore.QPointF(12, 12))

    def leaveEvent(self, event):
        """Clear the hover highlight when the pointer leaves the canvas."""
        if self._hover_ring is not None:
            self._set_hover(None)
        super().leaveEvent(event)

    def hideEvent(self, event):
        """Stop deferred scene edits and clear hover while the canvas is hidden."""
        self._label_timer.stop()
        if not self.closed and self._hover_index is not None:
            self._set_hover(None)
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_labels()

    def _initial_range(self, concordia_x: np.ndarray, concordia_y: np.ndarray):
        """Frame observations plus their nearest concordia neighborhood."""
        if not len(self._point_xy):
            return [[0, 1], [0, 1]]
        x_values, y_values = self._point_xy.T
        x_errors, y_errors = self._visible_errors.T
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
        return [
            [x_low - x_padding, x_high + x_padding],
            [max(0.0, y_low - y_padding), y_high + y_padding],
        ]


class ElidedLabel(QtWidgets.QLabel):
    """A one-line label that shortens its text with "…" instead of forcing
    its parent wider; the full text is the tooltip while it is shortened.

    Small screens (a 14-inch laptop) cannot give every header label its
    natural width, and a plain ``QLabel`` would either widen the window or
    be cut off mid-character.
    """

    def __init__(self, text: str = "", parent: QtWidgets.QWidget | None = None, *,
                 elide_mode=QtCore.Qt.TextElideMode.ElideRight) -> None:
        super().__init__(parent)
        self._elide_mode = elide_mode
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
            self._full_text, self._elide_mode, width
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


class DetectorCalibrationPanel(NeumorphicFrame):
    """Preview overlap fits and request re-import with explicit detector settings."""

    apply_requested = QtCore.Signal(dict)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DetectorCalibrationPanel")
        self._session = None
        self._preview_key = None
        self._constant = None
        self._drift = None
        self._options = {}
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        title = ElidedLabel("Detector cross-calibration")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)
        self.reason_label = ElidedLabel()
        layout.addWidget(self.reason_label)

        form = QtWidgets.QFormLayout()
        form.setSpacing(4)
        form.setFormAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.mode_combo = StyledComboBox()
        for label, value in (("Qtegra factor", "qtegra"),
                             ("Session constant", "constant"), ("Time drift", "drift")):
            self.mode_combo.addItem(label, value)
        self.basis_combo = StyledComboBox()
        self.basis_combo.addItem("All acquisitions", "all")
        self.basis_combo.addItem("Primary standard", "primary_standard")
        self.channel_combo = StyledComboBox()
        self.channel_combo.setToolTip("Channel shown in the plot; Apply calibrates all fitted channels.")
        for combo in (self.mode_combo, self.basis_combo, self.channel_combo):
            combo.setMinimumContentsLength(6)
            combo.setStyleSheet(
                "QComboBox { min-height: 18px; padding: 4px 26px 4px 7px; border-width: 1px; }"
            )
            combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
        form.addRow("Mode", self.mode_combo)
        form.addRow("Basis", self.basis_combo)
        form.addRow("Plot channel", self.channel_combo)
        layout.addLayout(form)
        self.apply_button = QtWidgets.QPushButton("Apply")
        self.apply_button.setToolTip("Re-import detector signals and keep reviewed selection windows.")
        self.apply_button.clicked.connect(lambda: self.apply_requested.emit(self.settings()))
        layout.addWidget(self.apply_button)
        self.plot = RoundedPlotWidget()
        self.plot.setMinimumSize(0, 220)
        self.plot.setMaximumHeight(220)
        self.plot.setLabel("bottom", "Session time", units="min")
        self.plot.setLabel("left", "Factor")
        self.plot.setToolTip("Points: acquisition slopes; curve: smoothed drift; solid: session constant; dashed: Qtegra.")
        layout.addWidget(self.plot)
        self.plot_caption = QtWidgets.QLabel(
            "points: per-acquisition slope · line: smoothed drift · "
            "solid: session constant · dashed: Qtegra"
        )
        self.plot_caption.setObjectName("SecondaryText")
        self.plot_caption.setStyleSheet("font-size: 10px;")
        self.plot_caption.setWordWrap(True)
        layout.addWidget(self.plot_caption)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Channel", "Qtegra", "Fitted ± SE", "Δ %"])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setStyleSheet(
            "QTableWidget { font-size: 11px; }"
            "QHeaderView::section { font-size: 10px; padding: 3px; }"
        )
        header_font = QtGui.QFont(self.table.font())
        header_font.setPixelSize(10)
        self.table.horizontalHeader().setFont(header_font)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.horizontalHeader().setMinimumHeight(1)
        self.table.setMinimumSize(0, 92)
        self.table.setMaximumHeight(112)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        layout.addStretch(1)
        self.basis_combo.currentIndexChanged.connect(self._refresh_preview)
        self.channel_combo.currentIndexChanged.connect(self._draw)
        self.mode_combo.currentIndexChanged.connect(self._update_apply_button)
        self.mode_combo.currentIndexChanged.connect(self._draw)
        self.set_session(None)

    def set_session(self, session: ReductionSession | None) -> None:
        """Show the retained raw session and its saved detector choices."""
        self._session = session
        self._preview_key = None
        options = {} if session is None else session.import_settings.get("detector_calibration", {})
        self._options = dict(options) if isinstance(options, dict) else {"mode": options}
        qtegra = None if session is None else session.qtegra
        capable = qtegra is not None and qtegra.has("analog_counter")
        self.setEnabled(capable)
        for combo in (self.mode_combo, self.basis_combo):
            combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(self._options.get("mode", "qtegra"))))
        self.basis_combo.setCurrentIndex(max(0, self.basis_combo.findData(self._options.get("on", "all"))))
        primary = "" if session is None else session.primary_68_standard
        self.basis_combo.setItemText(1, f"Primary standard ({primary})" if primary else "Primary standard")
        for combo in (self.mode_combo, self.basis_combo):
            combo.blockSignals(False)
        if capable:
            self._refresh_preview()
        else:
            self._constant = self._drift = None
            self.channel_combo.clear()
            self.table.setRowCount(0)
            self.plot.clear()
            self.reason_label.setText("Needs a Rich export: analog and counter readings")
            self.reason_label.show()
            self._update_apply_button()

    def settings(self) -> dict:
        """Return the reader's detector-calibration mapping for the pending choice."""
        options = dict(self._options)
        options.update(mode=self.mode_combo.currentData(), on=self.basis_combo.currentData())
        if options["on"] == "primary_standard":
            options["primary_standard"] = self._session.primary_68_standard
        else:
            options.pop("primary_standard", None)
        return options

    def _refresh_preview(self, *_args) -> None:
        if self._session is None or self._session.qtegra is None or not self.isEnabled():
            return
        options = self.settings()
        sep = self._session.import_settings.get("sep")
        if sep is None:
            from UPbRPy.io import resolve_session_manifest
            manifest = resolve_session_manifest(
                self._session.qtegra, self._session.import_settings.get("session_manifest"),
            )
            sep = manifest.sep if manifest is not None and manifest.sep else "_"
        fit_options = dict(
            on=options["on"], primary_standard=options.get("primary_standard"),
            window=options.get("window", (1e5, 2.4e6)),
            smoothing_s=options.get("smoothing_s", 1800.0), sep=sep,
        )
        key = (id(self._session.qtegra), repr(fit_options))
        if key != self._preview_key:
            try:
                self._constant = fit_cross_calibration(self._session.qtegra, **fit_options, mode="constant")
                self._drift = fit_cross_calibration(self._session.qtegra, **fit_options, mode="drift")
            except ValueError as exc:
                self._constant = self._drift = None
                self.channel_combo.clear()
                self.table.setRowCount(0)
                self.plot.clear()
                self.reason_label.setText(str(exc))
                self.reason_label.show()
                self._update_apply_button()
                return
            self._preview_key = key
        previous = self.channel_combo.currentText()
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        self.channel_combo.addItems(list(self._constant.channels))
        target = previous if previous in self._constant.channels else "238U"
        self.channel_combo.setCurrentIndex(max(0, self.channel_combo.findText(target)))
        self.channel_combo.blockSignals(False)
        self.reason_label.setText("" if self._constant.channels else "No overlap points for this basis.")
        self.reason_label.setVisible(not self._constant.channels)
        self._update_apply_button()
        self._draw()

    def _update_apply_button(self, *_args) -> None:
        self.apply_button.setEnabled(
            self.isEnabled() and (self.mode_combo.currentData() == "qtegra"
                                  or bool(self._constant and self._constant.channels))
        )

    def apply_theme(self, theme: ThemeSpec) -> None:
        """Use the Studio plot palette for all detector diagnostics."""
        self.plot.setBackground(theme.plot_background)
        for name in ("left", "bottom"):
            axis = self.plot.getAxis(name)
            axis.setPen(pg.mkPen(theme.axis_line))
            axis.setTextPen(pg.mkPen(theme.axis_text))
        self._draw()

    def _draw(self, *_args) -> None:
        self.plot.clear()
        if self._constant is None:
            return
        theme = active_theme()
        table_fits = self._drift if self.mode_combo.currentData() == "drift" else self._constant
        self.table.setRowCount(len(table_fits.channels))
        for row, (name, fit) in enumerate(table_fits.channels.items()):
            difference = "—" if not fit.qtegra_factor else f"{100 * (fit.factor / fit.qtegra_factor - 1):+.1f}"
            factor = f"{fit.factor:,.0f}" if abs(fit.factor) >= 1000 else f"{fit.factor:.4g}"
            error = f"{fit.factor_se:,.0f}" if abs(fit.factor) >= 1000 else f"{fit.factor_se:.2g}"
            qtegra = ("—" if fit.qtegra_factor is None else
                      f"{fit.qtegra_factor:,.0f}" if abs(fit.qtegra_factor) >= 1000 else
                      f"{fit.qtegra_factor:.4g}")
            values = (name, qtegra, f"{factor} ± {error}", difference)
            for column, value in enumerate(values):
                self.table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
            if name == self.channel_combo.currentText():
                self.table.selectRow(row)
        name = self.channel_combo.currentText()
        if name not in self._constant.channels:
            return
        constant = self._constant.channels[name]
        drift = self._drift.channels[name]
        self.plot.addItem(pg.InfiniteLine(pos=constant.factor, angle=0,
                                         pen=pg.mkPen(theme.muted, width=1)))
        if constant.qtegra_factor is not None:
            self.plot.addItem(pg.InfiniteLine(pos=constant.qtegra_factor, angle=0,
                pen=pg.mkPen(theme.muted, width=1, style=QtCore.Qt.PenStyle.DashLine)))
        if drift.times_s is not None:
            point_color = QtGui.QColor(theme.accent)
            point_color.setAlpha(90)
            scatter = pg.ScatterPlotItem(
                x=drift.times_s / 60, y=drift.raw_factors,
                size=6, brush=point_color, pen=None, hoverable=True, hoverSize=10,
                data=drift.acquisition_labels,
                tip=lambda x, y, spot: f"{spot}\nSession time: {x:.2f} min\nFactor: {y:.5g}",
            )
            scatter.setZValue(3)
            self.plot.addItem(scatter)
            self.plot.plot(drift.times_s / 60, drift.factors,
                           pen=pg.mkPen(theme.accent, width=3)).setZValue(2)


class _ReviewTabs(QtWidgets.QTabWidget):
    """Accept the original table handles when selecting their containing tab."""

    def setCurrentWidget(self, widget):
        """Select the tab containing a review view or page."""
        while widget is not None and self.indexOf(widget) < 0:
            widget = widget.parentWidget()
        if widget is not None:
            super().setCurrentWidget(widget)


class FlowLayout(QtWidgets.QLayout):
    """Lay out review chips in rows, constraining each item to the row width."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(4)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QtCore.QRect(0, 0, width, 0), measure=True)

    def minimumSize(self):
        return QtCore.QSize(0, 0)

    def sizeHint(self):
        return QtCore.QSize(200, self.heightForWidth(200))

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect)

    def _arrange(self, rect, *, measure=False):
        x, y, row_height = rect.x(), rect.y(), 0
        width = max(1, rect.width())
        for item in self._items:
            chip = item.widget()
            if chip.isHidden():
                continue
            item_width = min(width, item.sizeHint().width())
            item_height = chip.height_for_width(item_width)
            if row_height and x + item_width > rect.x() + width:
                x, y, row_height = rect.x(), y + row_height + self.spacing(), 0
            if not measure:
                item.setGeometry(QtCore.QRect(x, y, item_width, item_height))
            x += item_width + self.spacing()
            row_height = max(row_height, item_height)
        return y - rect.y() + row_height


class _SampleChip(QtWidgets.QAbstractButton):
    """A sample swatch and count with a full, partial or empty inclusion border."""

    def __init__(self, sample, count, color, parent=None):
        super().__init__(parent)
        self.sample_color = QtGui.QColor(color)
        self.setText(f"{sample} {count}")
        self.setAccessibleName(self.text())
        self.setToolTip(f"{sample}: {count} spots\nClick: show only · Cmd/Ctrl-click: toggle · Shift-click: add range")
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setProperty("inclusionState", "inactive")
        font = self.font()
        font.setPixelSize(11)
        self.setFont(font)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Preferred)

    def sizeHint(self):
        # Leave one pixel for Qt's fractional glyph-advance rounding.
        return QtCore.QSize(self.fontMetrics().horizontalAdvance(self.text()) + 25,
                            self.height_for_width(10000))

    def minimumSizeHint(self):
        return QtCore.QSize(0, self.sizeHint().height())

    def height_for_width(self, width):
        bounds = self.fontMetrics().boundingRect(
            QtCore.QRect(0, 0, max(1, width - 24), 10000),
            QtCore.Qt.TextFlag.TextWrapAnywhere, self.text())
        return max(22, bounds.height() + 6)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        state = self.property("inclusionState")
        color = self.sample_color
        background = self.palette().color(QtGui.QPalette.ColorRole.Button)
        painter.setBrush(background)
        border = color if state != "inactive" else self.palette().color(QtGui.QPalette.ColorRole.Mid)
        pen = QtGui.QPen(border, 2 if state == "active" or self.hasFocus() else 1)
        if state == "partial":
            pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        painter.setPen(pen)
        rect = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.drawRoundedRect(rect, 7, 7)
        if state != "inactive" or self.isDown():
            tint = QtGui.QColor(color)
            tint.setAlpha(28 if state == "active" else 12)
            painter.setBrush(tint)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 7, 7)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QtCore.QRectF(6, (self.height() - 8) / 2, 8, 8))
        painter.setPen(self.palette().color(QtGui.QPalette.ColorRole.ButtonText))
        painter.drawText(self.rect().adjusted(18, 3, -6, -3),
                         QtCore.Qt.TextFlag.TextWrapAnywhere | QtCore.Qt.AlignmentFlag.AlignVCenter,
                         self.text())


class _SampleChipArea(QtWidgets.QScrollArea):
    """Wrap chips, scrolling vertically after about three rows."""

    def __init__(self):
        super().__init__()
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        content = QtWidgets.QWidget()
        self.flow = FlowLayout(content)
        self.setWidget(content)
        self.chips = {}
        self.viewport().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.Resize:
            self.reflow()
        return super().eventFilter(watched, event)

    def reflow(self):
        height = self.flow.heightForWidth(self.viewport().width())
        self.widget().setMinimumHeight(height)
        row_height = max((chip.sizeHint().height() for chip in self.chips.values()
                          if not chip.isHidden()), default=22)
        self.setFixedHeight(min(height, 3 * row_height + 2 * self.flow.spacing()))
        self.flow.invalidate()


class ResultsTableWidget(QtWidgets.QWidget):
    """Sortable tables plus quick concordia reviews of final products."""

    selectedSpotsChanged = QtCore.Signal(object)
    showGlassesChanged = QtCore.Signal(bool)
    appearance_changed = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None, *,
                 settings: QtCore.QSettings | None = None) -> None:
        """Create review views, optionally remembering display preferences."""
        super().__init__(parent)
        self._settings = settings
        self.include_flags: dict[str, bool] = {}
        self.selected_spots: set[str] = set()
        self._spot_samples: dict[str, str] = {}
        self._spot_sample_order: list[str] = []
        self._depth_sample_order: list[str] = []
        self._run_order: list[str] = []
        archive = current_standard_archive()
        self._glass_materials = (set(archive.index[archive["material"].eq("glass")].astype(str))
                                 if "material" in archive else set())
        self._show_glasses = False
        self._glass_buttons = []
        self._chip_areas = []
        self._last_sample_chip = None
        self._depth_sample_choices = False
        self._blank_empty_readout = False
        self._syncing_selection = False
        self._readout_timer = QtCore.QTimer(self)
        self._readout_timer.setSingleShot(True)
        self._readout_timer.timeout.connect(self._update_age_readout)
        self.concordia_readout = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)
        self.summary_label = QtWidgets.QLabel(
            "Calculate final results to review preferred ages and flags."
        )
        self.summary_label.setObjectName("SecondaryText")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.tabs = _ReviewTabs()
        self.results_table, self.results_model = self.create_table(include_flags=self.include_flags)
        self.results_model.includeChanged.connect(self._table_include_changed)
        self.results_table.selectionModel().selectionChanged.connect(self._table_selection_changed)
        self.secondary_table, self.secondary_model = self.create_table()
        self.depth_table, self.depth_model = self.create_table()
        self.results_tree, self.results_group_model = self._create_grouped_view()
        self.secondary_tree, self.secondary_group_model = self._create_grouped_view()
        (self.results_page, self.results_group_toggle, self.results_filter,
         self._results_stack) = self._create_group_page(self.results_table, self.results_tree, "final")
        (self.secondary_page, self.secondary_group_toggle, self.secondary_filter,
         self._secondary_stack) = self._create_group_page(self.secondary_table, self.secondary_tree, "secondary")
        self.tabs.addTab(self.results_page, "Final results")
        self.tabs.addTab(self.secondary_page, "Secondary standards")
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
        for view in self._review_views():
            view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
            view.installEventFilter(self)
            view.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(self._show_spot_menu)
        self.spot_choice_list.itemSelectionChanged.connect(self._chooser_selection_changed)
        for view in (self.results_tree, self.secondary_tree, self.spot_choice_tree):
            view.selectionModel().selectionChanged.connect(
                lambda *args, view=view: self._group_selection_changed(view))
        self.spot_concordia.selectionChanged.connect(
            lambda names: self.set_selected_spots(names, origin=self.spot_concordia)
        )
        self.spot_concordia.spotHovered.connect(self._scroll_hovered_spot)
        self.tabs.currentChanged.connect(self._results_tab_changed)
        self._hovered_choice_name = ""

    _sample_names_cache = {}

    @staticmethod
    def _sample_names(*frames):
        """Return the first sample per spot in frame order, caching installed frames."""
        samples = {}
        cache = ResultsTableWidget._sample_names_cache
        for frame in frames:
            key = id(frame)
            cached = cache.get(key)
            if cached is None or cached[0]() is not frame:
                mapping = {}
                if "spot_name" in frame:
                    names = frame["spot_name"].astype(str)
                    first = ~names.duplicated()
                    values = (frame["sample"].where(frame["sample"].notna(), "").astype(str)
                              if "sample" in frame else pd.Series("", index=frame.index))
                    mapping = dict(zip(names[first], values[first]))
                cache[key] = weakref.ref(frame, lambda _ref, key=key: cache.pop(key, None)), mapping
            for name, sample in cache[key][1].items():
                samples.setdefault(name, sample)
        return samples

    def _glass_spot_names(self):
        samples = self._sample_names(self._depth_frame)
        samples.update(self._spot_samples)
        return {name for name, sample in samples.items() if sample in self._glass_materials}

    def set_glass_materials(self, names: Iterable[str]) -> None:
        """Replace glass sample names; apply defaults to reclassified spots."""
        previous = self._glass_spot_names()
        self._glass_materials = set(map(str, names))
        current = self._glass_spot_names()
        self._set_spot_inclusions({**dict.fromkeys(previous - current, True),
                                   **dict.fromkeys(current - previous, self._show_glasses)})
        self._update_glass_buttons()

    def show_glasses(self) -> bool:
        """Return the bulk glass inclusion preference."""
        return self._show_glasses

    def set_show_glasses(self, enabled: bool) -> None:
        """Set all glass spots' inclusion without emitting showGlassesChanged."""
        self._show_glasses = bool(enabled)
        for button in self._glass_buttons:
            blocker = QtCore.QSignalBlocker(button)
            button.setChecked(self._show_glasses)
            del blocker
        self.set_spot_included(self._glass_spot_names(), self._show_glasses)

    def _glass_toggled(self, enabled):
        self.set_show_glasses(enabled)
        self.showGlassesChanged.emit(self._show_glasses)
        self.appearance_changed.emit()

    def _create_glass_button(self):
        button = QtWidgets.QToolButton()
        button.setText("Glasses")
        button.setCheckable(True)
        button.setProperty("modeToggle", True)
        button.setToolTip("Include or exclude all glass reference spots")
        button.setChecked(self._show_glasses)
        button.setVisible(False)
        button.toggled.connect(self._glass_toggled)
        self._glass_buttons.append(button)
        return button

    def _update_glass_buttons(self):
        visible = bool(self._glass_spot_names())
        for button in self._glass_buttons:
            button.setVisible(visible)
        self._update_sample_chips()

    def _sample_spots(self):
        samples = dict(self._spot_samples)
        for name, sample in self._sample_names(self._depth_frame).items():
            samples.setdefault(name, sample)
        return samples

    def sample_chip_names(self) -> list[str]:
        """Return samples in run order, then depth-only samples."""
        return self._in_run_order(self._spot_sample_order + self._depth_sample_order)

    def set_sample_run_order(self, names: Iterable[str]) -> None:
        """Order the sample chips by acquisition (first spot of each sample).

        The reduction frames are grouped by sample, so the caller supplies the
        order taken from the session's acquisition start times. Samples not
        named keep their frame order after the named ones.
        """
        self._run_order = list(dict.fromkeys(map(str, names)))
        if self._chip_areas:
            self._rebuild_sample_chips()

    def _in_run_order(self, names):
        names = list(dict.fromkeys(names))
        rank = {name: index for index, name in enumerate(self._run_order)}
        return sorted(names, key=lambda name: (rank.get(name, len(rank)), names.index(name)))

    @staticmethod
    def _sample_order(*frames):
        return list(dict.fromkeys(sample for sample in
                                  ResultsTableWidget._sample_names(*frames).values() if sample))

    def _create_sample_chips(self, kind):
        area = _SampleChipArea()
        area.kind = kind
        self._chip_areas.append(area)
        return area

    def _rebuild_sample_chips(self):
        samples = self._sample_spots()
        names = self.sample_chip_names()
        fallback = {sample: _REVIEW_SAMPLE_COLORS[i % len(_REVIEW_SAMPLE_COLORS)]
                    for i, sample in enumerate(sorted(names))}
        colors = {**fallback, **self.spot_concordia._sample_colors}
        self.depth_concordia._sample_colors = colors.copy()
        for area in self._chip_areas:
            while area.flow.count():
                chip = area.flow.takeAt(0).widget()
                chip.hide()
                chip.deleteLater()
            area.chips.clear()
            page_spots = (self._spot_samples if area.kind == "spot"
                          else self._sample_names(self._depth_frame))
            page_order = self._in_run_order(
                self._spot_sample_order if area.kind == "spot" else self._depth_sample_order
            )
            for sample in page_order:
                count = sum(samples.get(name) == sample for name in page_spots)
                if not count:
                    continue
                chip = _SampleChip(sample, count, colors[sample])
                chip.clicked.connect(lambda checked=False, sample=sample, area=area:
                                     self._sample_chip_clicked(sample, area))
                area.chips[sample] = chip
                area.flow.addWidget(chip)
        if self._last_sample_chip not in names:
            self._last_sample_chip = None
        self._update_sample_chips()

    def _update_sample_chips(self):
        samples = self._sample_spots()
        for area in self._chip_areas:
            page_spots = (self._spot_samples if area.kind == "spot"
                          else self._sample_names(self._depth_frame))
            for sample, chip in area.chips.items():
                flags = [self.include_flags.get(name, False) for name in page_spots
                         if samples.get(name) == sample]
                state = "active" if all(flags) else "partial" if any(flags) else "inactive"
                chip.setProperty("inclusionState", state)
                chip.setVisible(self._show_glasses or sample not in self._glass_materials)
                chip.update()
            area.setVisible(any(not chip.isHidden() for chip in area.chips.values()))
            area.reflow()

    def _sample_chip_clicked(self, sample, area):
        modifiers = QtWidgets.QApplication.keyboardModifiers()
        names = [name for name, chip in area.chips.items() if not chip.isHidden()]
        if (modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier
                and self._last_sample_chip in names):
            first, last = sorted((names.index(self._last_sample_chip), names.index(sample)))
            self.show_samples(names[first:last + 1], mode="add")
        else:
            mode = "toggle" if modifiers & QtCore.Qt.KeyboardModifier.ControlModifier else "replace"
            self.show_samples([sample], mode=mode)
        self._last_sample_chip = sample

    def show_samples(self, names: Iterable[str], *, mode: str = "replace") -> None:
        """Include whole samples by replacement, per-sample toggle or addition.

        A partially included sample toggles to fully included. Glass samples
        can be selected through chips only while the Glasses toggle is on.
        """
        if mode not in {"replace", "toggle", "add"}:
            raise ValueError("mode must be 'replace', 'toggle' or 'add'")
        names = {names} if isinstance(names, str) else set(names)
        samples = self._sample_spots()
        groups = {sample: [name for name, value in samples.items() if value == sample]
                  for sample in names if self._show_glasses or sample not in self._glass_materials}
        changes = dict.fromkeys(self.include_flags, False) if mode == "replace" else {}
        for spots in groups.values():
            included = mode != "toggle" or not all(self.include_flags.get(name, False) for name in spots)
            changes.update(dict.fromkeys(spots, included))
        self._depth_sample_choices = True
        self.set_selected_spots(set())
        self._set_spot_inclusions(changes)

    def clear_spot_choices(self) -> None:
        """Exclude every spot, clear selection and blank the live age readout."""
        self.set_selected_spots(set())
        self._set_spot_inclusions(dict.fromkeys(self.include_flags, False))
        self._blank_empty_readout = True

    def _create_spot_bulk_buttons(self):
        row = QtWidgets.QHBoxLayout()
        for text, callback in (("All", lambda: self.set_all_spot_choices(True)),
                               ("Clear", self.clear_spot_choices)):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
        return row

    def _review_views(self):
        return (self.results_table, self.results_tree, self.secondary_tree,
                self.spot_choice_list, self.spot_choice_tree)

    def _create_grouped_view(self):
        view = QtWidgets.QTreeView()
        view.setObjectName("ReviewGroupedTree")
        view.setStyleSheet("QTreeView#ReviewGroupedTree::item { border-radius: 0px; }")
        model = QtGui.QStandardItemModel(view)
        proxy = _SpotReviewProxy(view, grouped=True)
        proxy.setSourceModel(model)
        view.setModel(proxy)
        view.setSortingEnabled(True)
        view.setAlternatingRowColors(True)
        view.setUniformRowHeights(True)
        view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        view.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        view.header().setStretchLastSection(False)
        view.header().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        model.itemChanged.connect(lambda item: self._group_include_changed(item))
        return view, model

    def _group_button(self, key):
        button = QtWidgets.QToolButton()
        button.setText("Group by sample")
        button.setCheckable(True)
        button.setProperty("modeToggle", True)
        if self._settings is not None:
            button.setChecked(self._settings.value(f"review_group_{key}_v1", False, type=bool))
            button.toggled.connect(lambda checked: self._settings.setValue(f"review_group_{key}_v1", checked))
        return button

    def _create_group_page(self, flat, tree, key):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        toggle = self._group_button(key)
        search = QtWidgets.QLineEdit()
        search.setMinimumWidth(0)
        search.setPlaceholderText("Filter spots…")
        search.setClearButtonEnabled(True)
        row.addWidget(toggle)
        row.addWidget(search, 1)
        layout.addLayout(row)
        stack = QtWidgets.QStackedWidget()
        stack.addWidget(flat)
        stack.addWidget(tree)
        stack.setCurrentIndex(int(toggle.isChecked()))
        toggle.toggled.connect(lambda checked: self._switch_group_view(stack, checked))
        search.textChanged.connect(lambda text: self._filter_group_page(flat, tree, text))
        layout.addWidget(stack, 1)
        return page, toggle, search, stack

    def _switch_group_view(self, stack, checked):
        stack.setCurrentIndex(int(checked))
        self.set_selected_spots(self.selected_spots)

    def _filter_group_page(self, flat, tree, text):
        self._syncing_selection = True
        try:
            if flat is self.results_table:
                flat.model().set_spot_filter(text)
            else:
                flat.model().setFilterKeyColumn(-1)
                flat.model().setFilterFixedString(text)
                flat.model().setFilterCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseInsensitive)
            tree.model().set_spot_filter(text)
        finally:
            self._syncing_selection = False
        self.set_selected_spots(self.selected_spots)

    @staticmethod
    def _tree_rows(view):
        model = view.model()
        for row in range(model.rowCount()):
            parent = model.index(row, 0)
            yield parent
            for child in range(model.rowCount(parent)):
                yield model.index(child, 0, parent)

    def _group_selection_changed(self, view):
        if self._syncing_selection:
            return
        names = {name for index in view.selectionModel().selectedRows()
                 for name in index.data(_SPOT_NAMES_ROLE) or []}
        visible = self._visible_spot_names(view)
        self.set_selected_spots(names | (self.selected_spots - visible), origin=view)

    def _group_include_changed(self, item):
        if not getattr(self, "_updating_groups", False) and item.column() == 0 and item.isCheckable():
            self.set_spot_included(item.data(_SPOT_NAMES_ROLE) or [],
                                   item.checkState() != QtCore.Qt.CheckState.Unchecked)

    def _sample_summary(self, names):
        frame = self.results_model.frame
        if not {"spot_name", "preferred_date", "preferred_date_2e"}.issubset(frame.columns):
            return ""
        included = [name for name in names if self.include_flags.get(name, True)]
        spots = frame.loc[frame.spot_name.astype(str).isin(included)]
        result = weighted_mean_and_mswd(pd.to_numeric(spots.preferred_date, errors="coerce"),
                                        pd.to_numeric(spots.preferred_date_2e, errors="coerce"))
        if result["n"] < 2:
            return ""
        value = format_value_with_uncertainty(result["weighted_mean"], result["weighted_mean_2e"])
        return f"{value} Ma (2SE); MSWD {format_mswd(result['mswd'])}; n={result['n']}"

    @staticmethod
    def _secondary_summary(row):
        """Describe the engine's secondary summary without recalculating it."""
        if "6/38 WM ±2s" in row:
            return " · ".join([str(row["sample"]), f"n={row['n']}", *(
                f"{system} {row[system + ' WM ±2s']} Ma; accepted "
                f"{row[system + ' accepted']} Ma; offset {row[system + ' offset %']}%"
                for system in ("6/38", "7/6"))])
        def number(column):
            try:
                return float(row.get(column, np.nan))
            except (TypeError, ValueError):
                return np.nan

        parts = [str(row["sample"])]
        total, flagged = number("n_total"), number("n_flagged")
        counts = f"{total:g} spots" if np.isfinite(total) else ""
        if np.isfinite(flagged):
            counts += f" ({flagged:g} flagged)" if counts else f"{flagged:g} flagged"
        if counts:
            parts.append(counts)
        mean = format_value_with_uncertainty(number("weighted_mean"), number("weighted_mean_2e"))
        if mean:
            parts.append(f"weighted mean {mean} Ma (2SE)")
        mswd = format_mswd(number("mswd"))
        if mswd:
            parts.append(f"MSWD {mswd}")
        accepted, error = number("accepted_age"), number("accepted_age_2e")
        if np.isfinite(accepted):
            uncertainty = f" ± {error:g}" if np.isfinite(error) else ""
            parts.append(f"accepted {accepted:g}{uncertainty} Ma")
        offset = number("offset_percent")
        if np.isfinite(offset):
            parts.append(f"offset {offset:+.1f}%")
        return " · ".join(parts)

    @staticmethod
    def _group_row(values, columns, names, sample):
        items = [QtGui.QStandardItem() for _ in range(len(columns) + 1)]
        for item in items:
            item.setEditable(False)
        items[0].setCheckable(True)
        items[0].setData(list(names), _SPOT_NAMES_ROLE)
        items[0].setData(sample, _SAMPLE_ROLE)
        for item, column in zip(items[1:], columns):
            value = values.get(column, "")
            if pd.isna(value):
                value = ""
            if isinstance(value, np.generic):
                value = value.item()
            item.setText(f"{value:.7g}" if isinstance(value, float) else str(value))
            item.setData(value, QtCore.Qt.ItemDataRole.UserRole)
        return items

    def _build_grouped_views(self):
        self._updating_groups = True
        try:
            final = self.results_model.frame
            secondary = self.secondary_model.frame
            child_columns = [name for name in ("spot_name", "preferred_date", "preferred_date_2e",
                             "preferred_date_method", "preferred_date_pass", "disc%", "automatic_flags")
                             if name in self._spot_frame]
            for model, columns in ((self.results_group_model, list(final.columns)),
                                   (self.secondary_group_model, child_columns)):
                model.clear()
                model.setHorizontalHeaderLabels(["Include", *map(str, columns)])
            self.spot_choice_group_model.clear()
            self.spot_choice_group_model.setHorizontalHeaderLabels(["Spots"])
            self._spot_samples = self._sample_names(final, self._spot_frame)
            if "spot_name" in final:
                for sample in sorted({self._spot_samples[str(name)] for name in final.spot_name}):
                    spots = final.loc[final.spot_name.astype(str).map(self._spot_samples).eq(sample)]
                    names = list(spots.spot_name.astype(str))
                    parent = self._group_row({}, list(final.columns), names, sample)
                    self.results_group_model.appendRow(parent)
                    for _, row in spots.iterrows():
                        parent[0].appendRow(self._group_row(row, list(final.columns), [str(row.spot_name)], sample))
            if "sample" in secondary:
                for _, row in secondary.sort_values("sample").iterrows():
                    sample = str(row["sample"])
                    spots = self._spot_frame
                    if {"sample", "spot_name"}.issubset(spots.columns):
                        spots = spots.loc[spots["sample"].astype(str).eq(sample)]
                    else:
                        spots = pd.DataFrame()
                    names = list(spots.spot_name.astype(str)) if "spot_name" in spots else []
                    parent = self._group_row({}, child_columns, names, sample)
                    parent[0].setText(self._secondary_summary(row))
                    self.secondary_group_model.appendRow(parent)
                    for _, spot in spots.iterrows():
                        # Summary values remain the engine's; children show only spot fields.
                        values = {column: spot.get(column, "") for column in child_columns}
                        parent[0].appendRow(self._group_row(values, child_columns, [str(spot.spot_name)], sample))
            for sample in sorted(set(self._spot_samples.values())):
                names = [name for name in self.include_flags if self._spot_samples.get(name) == sample]
                parent = self._group_row({}, [], names, sample)[0]
                self.spot_choice_group_model.appendRow(parent)
                for name in names:
                    child = self._group_row({}, [], [name], sample)[0]
                    child.setText(name)
                    child.setToolTip(name)
                    parent.appendRow(child)
        finally:
            self._updating_groups = False
        self._update_group_inclusion()
        for view in (self.results_tree, self.secondary_tree, self.spot_choice_tree):
            model = view.model()
            columns = [model.headerData(column, QtCore.Qt.Orientation.Horizontal)
                       for column in range(model.columnCount())]
            view.sortByColumn(columns.index("spot_name") if "spot_name" in columns else 0,
                              QtCore.Qt.SortOrder.AscendingOrder)
            view.expandAll()
        for view in (self.results_tree, self.secondary_tree):
            for row in range(view.model().rowCount()):
                view.setFirstColumnSpanned(row, QtCore.QModelIndex(), True)

    def _update_group_inclusion(self):
        self._updating_groups = True
        try:
            for model in (self.results_group_model, self.secondary_group_model, self.spot_choice_group_model):
                for row in range(model.rowCount()):
                    parent = model.item(row)
                    names = parent.data(_SPOT_NAMES_ROLE) or []
                    count = sum(self.include_flags.get(name, True) for name in names)
                    state = (QtCore.Qt.CheckState.PartiallyChecked if 0 < count < len(names)
                             else QtCore.Qt.CheckState.Checked if count else QtCore.Qt.CheckState.Unchecked)
                    parent.setCheckState(state)
                    sample = parent.data(_SAMPLE_ROLE) or "(no sample)"
                    if model is self.results_group_model:
                        summary = self._sample_summary(names)
                        parent.setText(f"{sample} · {len(names)} spots ({count} included)" + (f" · {summary}" if summary else ""))
                    elif model is self.spot_choice_group_model:
                        parent.setText(f"{sample} · {count}/{len(names)}")
                    parent.setToolTip(f"{sample} ({count} included / {len(names)})"
                                      if model is self.spot_choice_group_model else parent.text())
                    for child_row in range(parent.rowCount()):
                        child = parent.child(child_row)
                        included = self.include_flags.get(child.data(_SPOT_NAMES_ROLE)[0], True)
                        child.setCheckState(QtCore.Qt.CheckState.Checked if included else QtCore.Qt.CheckState.Unchecked)
                        child.setData(included, QtCore.Qt.ItemDataRole.UserRole)
        finally:
            self._updating_groups = False

    def _scroll_hovered_spot(self, name):
        chooser = self.spot_choice_tree if self.spot_group_toggle.isChecked() else self.spot_choice_list
        if chooser is self.spot_choice_tree:
            model = self.spot_choice_group_model
            rows = []
            self._updating_groups = True
            try:
                for index in self._tree_rows(chooser):
                    if not index.parent().isValid():
                        continue
                    names = index.data(_SPOT_NAMES_ROLE)
                    item = model.itemFromIndex(chooser.model().mapToSource(index))
                    item.setData(QtGui.QBrush(theme_color(self._theme.accent_soft)) if names == [name] else None,
                                 QtCore.Qt.ItemDataRole.BackgroundRole)
                    if names == [name]:
                        chooser.expand(index.parent())
                        rows.append(index)
            finally:
                self._updating_groups = False
            self._scroll_selection_into_view(chooser, rows)
            self._hovered_choice_name = name
            return
        blocker = QtCore.QSignalBlocker(chooser)
        for row in range(chooser.count()):
            item = chooser.item(row)
            if item.text() == self._hovered_choice_name:
                item.setData(QtCore.Qt.ItemDataRole.BackgroundRole, None)
            if name and item.text() == name and not item.isHidden():
                color = theme_color(self._theme.accent_soft)
                item.setBackground(QtGui.QBrush(color))
                self._scroll_selection_into_view(chooser, [chooser.indexFromItem(item)])
        self._hovered_choice_name = name
        del blocker

    def _results_tab_changed(self, _index):
        if self.tabs.currentWidget() is self.results_page:
            view = self.results_tree if self.results_group_toggle.isChecked() else self.results_table
            rows = view.selectionModel().selectedRows()
            if view is self.results_tree:
                for index in rows:
                    view.expand(index.parent())
                self._scroll_selection_into_view(view, rows)
                return
            rows = sorted(rows, key=lambda index: index.row())
            self._scroll_selection_into_view(
                self.results_table, [index for index in rows if not self.results_table.isRowHidden(index.row())])

    def _table_include_changed(self, name, included):
        self.set_spot_included({name}, included)

    def _chooser_include_changed(self, item):
        self.set_spot_included({item.text()}, item.checkState() == QtCore.Qt.CheckState.Checked)

    def set_spot_included(self, spot_names, included: bool) -> None:
        """Set review inclusion for spot keys and update both views once."""
        self._set_spot_inclusions(dict.fromkeys(spot_names, bool(included)))

    def _set_spot_inclusions(self, changes):
        """Apply mixed inclusion changes before notifying views or fitting."""
        self._blank_empty_readout = False
        self.include_flags.update(changes)
        blocker = QtCore.QSignalBlocker(self.spot_choice_list)
        for index in range(self.spot_choice_list.count()):
            item = self.spot_choice_list.item(index)
            item.setCheckState(QtCore.Qt.CheckState.Checked if self.include_flags.get(item.text(), True)
                               else QtCore.Qt.CheckState.Unchecked)
        del blocker
        if self.results_model.rowCount():
            self.results_model.dataChanged.emit(self.results_model.index(0, 0),
                                               self.results_model.index(self.results_model.rowCount()-1, 0),
                                               [QtCore.Qt.ItemDataRole.CheckStateRole, QtCore.Qt.ItemDataRole.UserRole])
        self._update_group_inclusion()
        self._update_sample_chips()
        self.refresh_spot_concordia(inclusions_only=True)
        self.refresh_depth_concordia(reset_view=False, inclusions_only=True)

    def set_selected_spots(self, spot_names, *, origin=None) -> None:
        """Share spot selection, keeping the origin view's scroll position."""
        if self._syncing_selection:
            return
        self.selected_spots = set(spot_names) & self.include_flags.keys()
        self._syncing_selection = True
        try:
            selection = QtCore.QItemSelection()
            proxy = self.results_table.model()
            table_rows = []
            for row in range(proxy.rowCount()):
                source = proxy.mapToSource(proxy.index(row, 0))
                if self.results_model.spot_name(source.row()) in self.selected_spots:
                    selection.select(proxy.index(row, 0), proxy.index(row, proxy.columnCount()-1))
                    if not self.results_table.isRowHidden(row):
                        table_rows.append(proxy.index(row, 0))
            self.results_table.selectionModel().select(selection, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
            chooser_rows = []
            for index in range(self.spot_choice_list.count()):
                item = self.spot_choice_list.item(index)
                item.setSelected(item.text() in self.selected_spots)
                if item.isSelected() and not item.isHidden():
                    chooser_rows.append(self.spot_choice_list.indexFromItem(item))
            for view, rows in ((self.results_table, table_rows),
                               (self.spot_choice_list, chooser_rows)):
                if view is not origin:
                    self._scroll_selection_into_view(view, rows)
            for view in (self.results_tree, self.secondary_tree, self.spot_choice_tree):
                selection = QtCore.QItemSelection()
                rows = []
                for index in self._tree_rows(view):
                    if not index.parent().isValid():
                        continue  # A header is a selection action for its spots.
                    names = set(index.data(_SPOT_NAMES_ROLE) or [])
                    if names and names <= self.selected_spots:
                        selection.select(index, index.siblingAtColumn(view.model().columnCount()-1))
                        if index.parent().isValid():
                            rows.append(index)
                view.selectionModel().select(selection, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
                if view is not origin and view.isVisible():
                    for index in rows:
                        view.expand(index.parent())
                    self._scroll_selection_into_view(view, rows)
            self.spot_concordia.select_spots(self.selected_spots)
        finally:
            self._syncing_selection = False
        self.selectedSpotsChanged.emit(set(self.selected_spots))
        self._readout_timer.start(0)

    @staticmethod
    def _scroll_selection_into_view(view, rows) -> None:
        """Reveal the first selected row only when all are outside the viewport."""
        if not view.isVisible():
            return
        for index in rows:
            rect = view.visualRect(index)
            if rect.height() > 0 and rect.bottom() >= 0 and rect.top() < view.viewport().height():
                return
        if rows:
            view.scrollTo(rows[0])

    def _table_selection_changed(self, *_args):
        if not self._syncing_selection:
            proxy = self.results_table.model()
            self.set_selected_spots({self.results_model.spot_name(proxy.mapToSource(index).row())
                                     for index in self.results_table.selectionModel().selectedRows()}
                                    | (self.selected_spots - self._visible_spot_names(self.results_table)),
                                    origin=self.results_table)

    def _chooser_selection_changed(self):
        if not self._syncing_selection:
            hidden = {self.spot_choice_list.item(i).text() for i in range(self.spot_choice_list.count())
                      if self.spot_choice_list.item(i).isHidden()}
            self.set_selected_spots({item.text() for item in self.spot_choice_list.selectedItems()}
                                    | (self.selected_spots & hidden), origin=self.spot_choice_list)

    def _visible_spot_names(self, view):
        if view in (self.results_tree, self.secondary_tree, self.spot_choice_tree):
            return {name for index in self._tree_rows(view) if index.parent().isValid()
                    for name in index.data(_SPOT_NAMES_ROLE) or []}
        if view is self.spot_choice_list:
            return {view.item(i).text() for i in range(view.count()) if not view.item(i).isHidden()}
        proxy = self.results_table.model()
        return {self.results_model.spot_name(proxy.mapToSource(proxy.index(row, 0)).row())
                for row in range(proxy.rowCount()) if not self.results_table.isRowHidden(row)}

    def toggle_selected_inclusion(self) -> None:
        """Exclude an entirely included selection; otherwise include it."""
        if self.selected_spots:
            self.set_spot_included(self.selected_spots, not all(self.include_flags.get(name, True)
                                                             for name in self.selected_spots))

    def include_selected(self) -> None:
        """Include all selected spots in the review plot."""
        self.set_spot_included(self.selected_spots, True)

    def exclude_selected(self) -> None:
        """Exclude all selected spots from the review plot."""
        self.set_spot_included(self.selected_spots, False)

    def invert_selection(self, view=None) -> None:
        """Invert selection within the invoking view's visible spots."""
        if view is None:
            view = self.results_tree if self.results_group_toggle.isChecked() else self.results_table
        visible = self._visible_spot_names(view)
        self.set_selected_spots(self.selected_spots ^ visible, origin=view)

    def select_same_sample(self) -> None:
        """Select all spots belonging to any currently selected sample."""
        samples = {self._spot_samples[name] for name in self.selected_spots if name in self._spot_samples}
        self.set_selected_spots({name for name, sample in self._spot_samples.items() if sample in samples})

    def _show_spot_menu(self, pos):
        view = self.sender()
        menu = QtWidgets.QMenu(view)
        for text, callback in (("Include selected", self.include_selected),
                               ("Exclude selected", self.exclude_selected),
                               ("Invert selection", lambda: self.invert_selection(view)),
                               ("Select same sample", self.select_same_sample)):
            menu.addAction(text, callback)
        if isinstance(view, QtWidgets.QTreeView):
            menu.addSeparator()
            menu.addAction("Expand all", view.expandAll)
            menu.addAction("Collapse all", view.collapseAll)
        menu.exec(view.viewport().mapToGlobal(pos))

    def eventFilter(self, watched, event):
        """Handle review inclusion and native Select All keyboard commands."""
        if event.type() == QtCore.QEvent.Type.KeyPress and watched in self._review_views():
            if event.key() == QtCore.Qt.Key.Key_Space:
                self.toggle_selected_inclusion()
                return True
            if event.matches(QtGui.QKeySequence.StandardKey.SelectAll):
                self.set_selected_spots(self._visible_spot_names(watched), origin=watched)
                return True
        return super().eventFilter(watched, event)

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
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.spot_sample_chips = self._create_sample_chips("spot")
        layout.addWidget(self.spot_sample_chips)
        review_row = QtWidgets.QHBoxLayout()
        review_row.setSpacing(8)
        layout.addLayout(review_row, 1)
        controls = QtWidgets.QWidget()
        controls.setMaximumWidth(230)
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(QtWidgets.QLabel("Spot means to display"))
        self.spot_filter = QtWidgets.QLineEdit()
        self.spot_filter.setMinimumWidth(0)
        self.spot_filter.setPlaceholderText("Filter spots…")
        self.spot_filter.textChanged.connect(self.filter_spot_choices)
        controls_layout.addWidget(self.spot_filter)
        self.spot_group_toggle = self._group_button("chooser")
        filter_row = QtWidgets.QHBoxLayout()
        filter_row.setSpacing(4)
        filter_row.addWidget(self.spot_group_toggle)
        self.spot_glasses_toggle = self._create_glass_button()
        filter_row.addWidget(self.spot_glasses_toggle)
        filter_row.addStretch(1)
        controls_layout.addLayout(filter_row)
        self.spot_choice_list = QtWidgets.QListWidget()
        self.spot_choice_list.setTextElideMode(QtCore.Qt.TextElideMode.ElideMiddle)
        self.spot_choice_list.itemChanged.connect(self._chooser_include_changed)
        self.spot_choice_tree, self.spot_choice_group_model = self._create_grouped_view()
        self.spot_choice_tree.model().setSortRole(QtCore.Qt.ItemDataRole.DisplayRole)
        self.spot_choice_tree.setHeaderHidden(True)
        self.spot_choice_tree.setTextElideMode(QtCore.Qt.TextElideMode.ElideMiddle)
        self.spot_choice_tree.header().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._chooser_stack = QtWidgets.QStackedWidget()
        # A wrapping readout makes Qt ask this page for height-for-width.
        # Let the chooser and canvas shrink instead of inheriting their
        # preferred heights and scrolling the bottom controls off a laptop.
        self._chooser_stack.setMinimumHeight(80)
        self._chooser_stack.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                           QtWidgets.QSizePolicy.Policy.Ignored)
        self._chooser_stack.addWidget(self.spot_choice_list)
        self._chooser_stack.addWidget(self.spot_choice_tree)
        self._chooser_stack.setCurrentIndex(int(self.spot_group_toggle.isChecked()))
        self.spot_group_toggle.toggled.connect(lambda checked: self._switch_group_view(self._chooser_stack, checked))
        controls_layout.addWidget(self._chooser_stack, 1)
        controls_layout.addLayout(self._create_spot_bulk_buttons())
        diagram_row, self.spot_diagram_buttons = self.create_diagram_buttons(
            self.refresh_spot_concordia
        )
        controls_layout.addWidget(diagram_row)
        colour_row = QtWidgets.QHBoxLayout()
        colour_row.setSpacing(0)
        colour_row.addWidget(QtWidgets.QLabel("Colour: "))
        colour_group = QtWidgets.QButtonGroup(page)
        self.spot_colour_buttons = {}
        saved_colour = (self._settings.value("review_spot_colour_v1", "sample", type=str)
                        if self._settings is not None else "sample")
        if saved_colour not in {"sample", "spot", "single"}:
            saved_colour = "sample"
        for key, label in (("sample", "Sample"), ("spot", "Spot"), ("single", "Single")):
            button = QtWidgets.QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setProperty("modeToggle", True)
            button.setChecked(key == saved_colour)
            button.clicked.connect(lambda checked=False, key=key: self.set_spot_colour_mode(key))
            colour_group.addButton(button)
            colour_row.addWidget(button)
            self.spot_colour_buttons[key] = button
        colour_row.addStretch(1)
        controls_layout.addLayout(colour_row)
        self.concordia_age_label = QtWidgets.QLabel("Included: No concordia age for this set")
        self.concordia_age_label.setObjectName("SecondaryText")
        self.concordia_age_label.setWordWrap(True)
        self.concordia_age_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
                                                       | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.concordia_age_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                              QtWidgets.QSizePolicy.Policy.Preferred)
        self.concordia_age_label.setMaximumHeight(self.concordia_age_label.fontMetrics().lineSpacing() * 3 + 6)
        controls_layout.addWidget(self.concordia_age_label)
        self.spot_concordia = ConcordiaReviewWidget()
        self.spot_concordia.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                          QtWidgets.QSizePolicy.Policy.Ignored)
        review_row.addWidget(controls)
        review_row.addWidget(self.spot_concordia, 1)
        return page

    def create_depth_concordia_page(self) -> QtWidgets.QWidget:
        """Build the one-spot sweep-wise concordia review page."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.depth_sample_chips = self._create_sample_chips("depth")
        layout.addWidget(self.depth_sample_chips)
        review_row = QtWidgets.QHBoxLayout()
        review_row.setSpacing(8)
        layout.addLayout(review_row, 1)
        chooser = QtWidgets.QWidget()
        chooser.setMaximumWidth(230)
        controls = QtWidgets.QVBoxLayout(chooser)
        controls.setContentsMargins(0, 0, 0, 0)
        review_row.addWidget(chooser)
        controls.addWidget(QtWidgets.QLabel("Depth-profile spot"))
        self.depth_spot_combo = StyledComboBox(elide_mode=QtCore.Qt.TextElideMode.ElideMiddle)
        self.depth_spot_combo.currentTextChanged.connect(self.depth_spot_combo.setToolTip)
        self.depth_spot_combo.setMinimumContentsLength(8)
        self.depth_spot_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.depth_spot_combo.currentIndexChanged.connect(
            self._depth_spot_changed
        )
        controls.addWidget(self.depth_spot_combo)
        self.depth_glasses_toggle = self._create_glass_button()
        controls.addWidget(self.depth_glasses_toggle, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        controls.addLayout(self._create_spot_bulk_buttons())
        diagram_row, self.depth_diagram_buttons = self.create_diagram_buttons(
            self.refresh_depth_concordia
        )
        controls.addWidget(diagram_row)
        cmap_row = QtWidgets.QHBoxLayout()
        cmap_row.setSpacing(6)
        cmap_row.addWidget(QtWidgets.QLabel("Colour map"))
        self.depth_colormap_combo = StyledComboBox()
        self.depth_colormap_combo.setMinimumContentsLength(7)
        self.depth_colormap_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        available = set(pg.colormap.listMaps())
        self.depth_colormap_combo.addItems([name for name in (
            "plasma", "viridis", "inferno", "magma", "cividis", "turbo", "CET-L1", "CET-L2")
            if name in available])
        saved_map = (self._settings.value("review_depth_colormap_v1", "plasma", type=str)
                     if self._settings is not None else "plasma")
        index = self.depth_colormap_combo.findText(saved_map)
        self.depth_colormap_combo.setCurrentIndex(max(0, index))
        self.depth_colormap_combo.currentTextChanged.connect(self.set_depth_colormap)
        cmap_row.addWidget(self.depth_colormap_combo)
        cmap_row.addStretch(1)
        controls.addLayout(cmap_row)
        controls.addStretch(1)
        self.depth_concordia = ConcordiaReviewWidget()
        self.depth_concordia.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                           QtWidgets.QSizePolicy.Policy.Ignored)
        review_row.addWidget(self.depth_concordia, 1)
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

    def supports_select_mode(self) -> bool:
        """Return whether the active result canvas supports spot selection."""
        target = self.active_concordia()
        return target is not None and target.supports_select_mode()

    def restore_default_view(self) -> None:
        """Restore the current concordia view without changing table state."""
        target = self.active_concordia()
        if target is not None:
            target.restore_default_view()

    def filter_spot_choices(self, text: str) -> None:
        """Hide non-matching spot selectors without changing selections."""
        query = text.strip().casefold()
        blocker = QtCore.QSignalBlocker(self.spot_choice_list)
        for index in range(self.spot_choice_list.count()):
            item = self.spot_choice_list.item(index)
            sample = getattr(self, "_spot_samples", {}).get(item.text(), "")
            item.setHidden(not any(query in value.casefold() for value in (item.text(), sample)))
        del blocker
        syncing = self._syncing_selection
        self._syncing_selection = True
        try:
            self.spot_choice_tree.model().set_spot_filter(text)
        finally:
            self._syncing_selection = syncing
        if not syncing:
            self.set_selected_spots(self.selected_spots, origin=self.spot_choice_list)

    def set_all_spot_choices(self, checked: bool) -> None:
        """Include all non-glass spots and glasses per the toggle.

        False retains the legacy action of excluding filtered chooser spots;
        clear_spot_choices() excludes every spot regardless of the filter.
        """
        if checked:
            glasses = self._glass_spot_names()
            self._depth_sample_choices = True
            self.set_selected_spots(set())
            self._set_spot_inclusions({name: self._show_glasses or name not in glasses
                                       for name in self.include_flags})
        else:
            view = self.spot_choice_tree if self.spot_group_toggle.isChecked() else self.spot_choice_list
            self.set_spot_included(self._visible_spot_names(view), False)

    def refresh_spot_concordia(
        self, *, reset_view: bool = False, inclusions_only: bool = False,
    ) -> None:
        """Refresh the checked spot means, reusing geometry for inclusion changes."""
        if self._theme is None:
            return
        selected_names = {name for name, included in self.include_flags.items() if included}
        if inclusions_only:
            self.spot_concordia.set_included_spots(selected_names)
        else:
            self.spot_concordia.show_data(
                self._spot_frame,
                source_frame=self._spot_frame,
                diagram=self.selected_diagram(self.spot_diagram_buttons),
                kind="spot",
                include_calibration_uncertainty=self._include_calibration_uncertainty,
                theme=self._theme,
                reset_view=reset_view,
                colour_mode=self.spot_colour_mode(),
                included_spots=selected_names,
            )
        self.spot_concordia.set_concordia_age(None)
        self._readout_timer.start(0)

    def spot_colour_mode(self) -> str:
        """Return the spot ellipse and point colour mode."""
        return next(key for key, button in self.spot_colour_buttons.items() if button.isChecked())

    def set_spot_colour_mode(self, mode: str) -> None:
        """Choose sample, spot or single colour and remember the preference."""
        if mode not in self.spot_colour_buttons:
            return
        self.spot_colour_buttons[mode].setChecked(True)
        if self._settings is not None:
            self._settings.setValue("review_spot_colour_v1", mode)
        self.refresh_spot_concordia()
        self.appearance_changed.emit()

    def set_depth_colormap(self, name: str) -> None:
        """Choose a built-in depth map, recolouring without changing the view."""
        index = self.depth_colormap_combo.findText(name)
        if index < 0:
            return
        blocker = QtCore.QSignalBlocker(self.depth_colormap_combo)
        self.depth_colormap_combo.setCurrentIndex(index)
        del blocker
        if self._settings is not None:
            self._settings.setValue("review_depth_colormap_v1", name)
        self.refresh_depth_concordia(reset_view=False)
        self.appearance_changed.emit()

    def _update_age_readout(self):
        frame = self._spot_frame
        selected = bool(self.selected_spots)
        names = {name for name, included in self.include_flags.items() if included
                 and (not selected or name in self.selected_spots)}
        frame = (frame.loc[frame.spot_name.astype(str).isin(names)] if "spot_name" in frame
                 else frame.iloc[0:0])
        self.concordia_readout = concordia_age_readout(
            frame, space=self.selected_diagram(self.spot_diagram_buttons),
            include_calibration_uncertainty=self._include_calibration_uncertainty)
        prefix = "Selected" if selected else "Included"
        result = self.concordia_readout
        if result is None:
            text = "" if self._blank_empty_readout and not names else f"{prefix}: No concordia age for this set"
            tooltip = text
        else:
            age = format_value_with_uncertainty(result.age_ma, result.age_2se)
            text = (f"{prefix}: Concordia age\n{age} Ma (2σ)\n"
                    f"MSWD {format_mswd(result.mswd)} · n = {result.n}")
            tooltip = f"{prefix}: {result.text}"
            if selected and len(names) < len(self.selected_spots):
                tooltip += f"\n{len(names)} of {len(self.selected_spots)} selected are included"
        self.concordia_age_label.setText(text)
        self.concordia_age_label.setToolTip(tooltip)
        self.spot_concordia.set_concordia_age(result.age_ma if result else None)

    def hideEvent(self, event):
        """Stop pending fits before closing or hiding the review views."""
        self._readout_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        """Refresh the readout when returning to the review views."""
        super().showEvent(event)
        self._readout_timer.start(0)

    def _depth_spot_changed(self, _index):
        self._depth_sample_choices = False
        self.refresh_depth_concordia()

    def refresh_depth_concordia(
        self, *, reset_view: bool = True, inclusions_only: bool = False,
    ) -> None:
        """Redraw corrected sweeps for the chosen depth-profile spot."""
        if self._theme is None:
            return
        spot_name = self.depth_spot_combo.currentText()
        source = self._depth_frame
        mask = np.ones(len(source), dtype=bool)
        if self._depth_sample_choices and "spot_name" in source:
            names = {name for name, included in self.include_flags.items() if included}
            mask = source["spot_name"].astype(str).isin(names).to_numpy()
        elif spot_name and "spot_name" in source:
            mask = source["spot_name"].astype(str).eq(spot_name).to_numpy()
        if not self._depth_sample_choices and not self.include_flags.get(spot_name, self._show_glasses
                                      if spot_name in self._glass_spot_names() else True):
            mask = np.zeros(len(source), dtype=bool)
        positions = np.flatnonzero(mask)
        selected = source.iloc[positions]
        self.depth_concordia.show_data(
            selected,
            source_frame=source,
            row_positions=positions,
            diagram=self.selected_diagram(self.depth_diagram_buttons),
            kind="depth",
            include_calibration_uncertainty=(
                self._include_calibration_uncertainty
            ),
            theme=self._theme,
            reset_view=reset_view,
            depth_colormap=self.depth_colormap_combo.currentText(),
        )

    def create_table(
        self,
        *,
        include_flags: dict[str, bool] | None = None,
    ) -> tuple[QtWidgets.QTableView, DataFrameTableModel]:
        """Create one sortable table backed by a Qt proxy."""
        view = QtWidgets.QTableView()
        model = DataFrameTableModel(parent=view, include_flags=include_flags)
        proxy = _SpotReviewProxy(view) if include_flags is not None else QtCore.QSortFilterProxyModel(view)
        proxy.setSourceModel(model)
        proxy.setSortRole(QtCore.Qt.ItemDataRole.UserRole)
        view.setModel(proxy)
        view.setSortingEnabled(True)
        if include_flags is not None:
            view.sortByColumn(-1, QtCore.Qt.SortOrder.AscendingOrder)
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
        if include_flags is None:
            view.horizontalHeader().setSectionResizeMode(
                QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            )
        return view, model

    def show_results(
        self, finalized: object, *, review_flags: dict[str, str] | None = None
    ) -> None:
        """Display calculated results with optional project review annotations."""
        final_frame = getattr(finalized, "selected_results", pd.DataFrame())
        parallel_frame = getattr(finalized, "parallel_results", pd.DataFrame())
        if review_flags:
            def annotated(frame: pd.DataFrame) -> pd.DataFrame:
                if "spot_name" not in frame:
                    return frame
                return frame.assign(review_flag=frame["spot_name"].map(review_flags).fillna(""))

            final_frame = annotated(final_frame)
            parallel_frame = annotated(parallel_frame)
        # Capture acquisition order before any table or grouped-view sorting.
        self._spot_sample_order = self._sample_order(parallel_frame, final_frame)
        secondary = getattr(
            finalized,
            "secondary_standard_summary",
            pd.DataFrame(),
        )
        by_system = getattr(finalized, "reference_material_summary_by_system", pd.DataFrame())
        if not by_system.empty:
            from UPbRPy.export_table import secondary_summary_wide

            wide = secondary_summary_wide(by_system)
            secondary = wide[["sample", "n"]].copy()
            for system, prefix in (("6/38", "68"), ("7/6", "76")):
                secondary[system + " WM ±2s"] = [
                    f"{mean:.1f} ± {error:.1f}" if np.isfinite(mean) and np.isfinite(error) else ""
                    for mean, error in zip(wide[prefix + "_weighted_mean"], wide[prefix + "_weighted_mean_2e"])
                ]
                secondary[system + " accepted"] = wide[prefix + "_accepted_date"].map(
                    lambda value: f"{value:.1f}" if np.isfinite(value) else "")
                secondary[system + " offset %"] = wide[prefix + "_offset_percent"].map(
                    lambda value: f"{value:+.2f}" if np.isfinite(value) else "")
        retained_selection = set(self.selected_spots)
        self._syncing_selection = True
        self.results_model.set_frame(final_frame)
        sort_column = next((final_frame.columns.get_loc(name) + 1
                            for name in ("sample", "spot_name") if name in final_frame), -1)
        self.results_table.sortByColumn(sort_column, QtCore.Qt.SortOrder.AscendingOrder)
        # Data replacement can change column widths. Review checkboxes cannot.
        self.results_table.resizeColumnsToContents()
        self.secondary_model.set_frame(secondary)
        self._spot_frame = parallel_frame.copy()
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
        spot_names = list(dict.fromkeys(name for frame in (final_frame, self._spot_frame)
                                      if "spot_name" in frame
                                      for name in frame["spot_name"].dropna().astype(str)))
        self._spot_samples = self._sample_names(final_frame, self._spot_frame)
        glass_spots = self._glass_spot_names()
        retained_flags = {name: self.include_flags.get(
            name, self._show_glasses if name in glass_spots else True) for name in spot_names}
        self.include_flags.clear()
        self.include_flags.update(retained_flags)
        self._update_glass_buttons()
        self.spot_concordia.set_spot_context(self._spot_frame, final_frame, self.include_flags)
        self.spot_choice_list.blockSignals(True)
        self.spot_choice_list.clear()
        for spot_name in dict.fromkeys(spot_names):
            item = QtWidgets.QListWidgetItem(spot_name)
            item.setToolTip(spot_name)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                QtCore.Qt.CheckState.Checked if self.include_flags[spot_name] else QtCore.Qt.CheckState.Unchecked
            )
            self.spot_choice_list.addItem(item)
        self.spot_choice_list.blockSignals(False)
        self._build_grouped_views()
        self._rebuild_sample_chips()
        self._syncing_selection = False
        self.filter_spot_choices(self.spot_filter.text())
        self.set_selected_spots(retained_selection)
        self.refresh_spot_concordia(reset_view=True)
        self.refresh_depth_concordia()
        if self.tabs.currentWidget() not in (
            self.results_page,
            self.secondary_page,
            self.spot_concordia_page,
        ):
            self.tabs.setCurrentWidget(self.results_page)

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

_REVIEW_SAMPLE_COLORS = [
    "#4477aa", "#ee7733", "#228833", "#cc6677", "#aa4499", "#66aabb",
    "#aa8833", "#44aa99", "#dd6699", "#8888cc", "#999933", "#bb5566",
    "#3366cc", "#cc4422", "#7f55aa", "#669944", "#008899", "#b07850",
    "#6f7d8c", "#bb55bb",
]


def _make_diag_axis(plot_item: pg.PlotItem, theme: ThemeSpec) -> None:
    """Apply consistent axis styling to a diagnostic sub-plot."""
    tick_font = QtGui.QFont()
    tick_font.setPointSizeF(9.0)
    for axis_name in ("left", "bottom"):
        ax = plot_item.getAxis(axis_name)
        ax.setPen(theme.axis_line)
        ax.setTextPen(theme.axis_text)
        ax.setStyle(tickFont=tick_font, tickTextOffset=4)
    plot_item.showGrid(x=True, y=True, alpha=theme.grid_alpha)
    plot_item.setContentsMargins(4, 4, 4, 4)


class DownholeFractionationWidget(RoundedGraphicsLayoutWidget):
    """Primary-RM downhole factors with an adjustable uncertainty envelope.

    Data is supplied via :meth:`show_data`.  Accepts the dict returned by
    ``services.get_downhole_plot_data``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._theme: ThemeSpec | None = None
        self._data: dict | None = None
        self._plot_item: pg.PlotItem | None = None
        self._spot_segments: list[tuple[str, np.ndarray, np.ndarray]] = []
        self._hover_proxy = pg.SignalProxy(
            self.scene().sigMouseMoved, rateLimit=30, slot=self._on_mouse_moved
        )
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

    def _on_mouse_moved(self, event: tuple) -> None:
        if self._plot_item is None or not event:
            return
        vb = self._plot_item.vb
        scene_pos = event[0]
        if vb.sceneBoundingRect().contains(scene_pos):
            pos = vb.mapSceneToView(scene_pos)
            scale = np.abs(vb.viewPixelSize())
            if np.all(np.isfinite(scale)) and np.all(scale > 0):
                name = self._nearest_trace(np.array([pos.x(), pos.y()]), scale)
                if name:
                    QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), name, self)
                    return
        QtWidgets.QToolTip.hideText()

    def _nearest_trace(self, pos: np.ndarray, scale: np.ndarray) -> str:
        name = ""
        closest = 6.0 ** 2
        for spot, start, end in self._spot_segments:
            delta = (end - start) / scale
            offset = (pos - start) / scale
            length_sq = np.sum(delta * delta, axis=1)
            projection = np.divide(
                np.sum(offset * delta, axis=1), length_sq,
                out=np.zeros_like(length_sq), where=length_sq > 0,
            )
            residual = offset - np.clip(projection, 0, 1)[:, None] * delta
            distance_sq = np.min(np.sum(residual * residual, axis=1), initial=np.inf)
            if distance_sq < closest:
                name, closest = spot, distance_sq
        return name

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
        self._plot_item.vb.set_zoom_axes(self._zoom_mode)

    def _apply_interaction_mode(self) -> None:
        if self._plot_item is None:
            return
        if self._interaction_mode == "pan":
            self._plot_item.vb.setMouseMode(pg.ViewBox.PanMode)
        else:
            self._plot_item.vb.setMouseMode(pg.ViewBox.RectMode)


class SessionFractionationWidget(RoundedGraphicsLayoutWidget):
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
        self._plot_item.vb.set_zoom_axes(self._zoom_mode)

    def _apply_interaction_mode(self) -> None:
        if self._plot_item is None:
            return
        if self._interaction_mode == "pan":
            self._plot_item.vb.setMouseMode(pg.ViewBox.PanMode)
        else:
            self._plot_item.vb.setMouseMode(pg.ViewBox.RectMode)


class CalibrationSlopesWidget(RoundedGraphicsLayoutWidget):
    """Measured glass points and origin-anchored fits (cps vs. known ppm).

    Data is supplied via :meth:`show_data`.  Accepts the dict returned by
    ``services.get_calibration_slope_data``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._theme: ThemeSpec | None = None
        self._data: dict | None = None
        self._channel: str | None = None
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
        plot = getattr(self, "_plot_item", None)
        if plot is not None and plot.legend is not None:
            columns = self._legend_column_count(len(self._scatter_for_line))
            if plot.legend.columnCount != columns:
                plot.legend.setColumnCount(columns)

    def apply_theme(self, theme: ThemeSpec) -> None:
        self._theme = theme
        self.setBackground(theme.plot_background)
        if self._data is not None:
            self._draw()

    def show_data(
        self, data: dict | None, theme: ThemeSpec, *, channel: str | None = None,
    ) -> None:
        """Display the requested channel or all calibrated channels."""
        self._theme = theme
        self._data = data
        self._channel = channel
        self.setBackground(theme.plot_background)
        self._draw()

    def set_channel(self, channel: str | None) -> None:
        """Switch the plotted channel. A value of None displays all channels."""
        if channel != self._channel:
            self._channel = channel
            self._draw()

    def restore_default_view(self) -> None:
        if self._plot_item is not None:
            self._plot_item.enableAutoRange()

    def _draw(self) -> None:
        self.clear()
        self._scatter_for_line = {}
        self._plot_item = None
        theme = self._theme
        data = self._data
        if theme is None:
            return
        if data is None or not data.get("channels"):
            p = self.addPlot(0, 0, viewBox=AxisZoomViewBox())
            self._plot_item = p
            lbl = pg.TextItem("No calibration data available.\nRun the concentration calibration step first.",
                              color=theme.muted, anchor=(0.5, 0.5))
            lbl.setFont(QtGui.QFont("", 11))
            p.addItem(lbl)
            lbl.setPos(0.5, 0.5)
            return

        all_channels = data["channels"]
        channels = [self._channel] if self._channel in all_channels else all_channels
        p = self.addPlot(row=0, col=0, viewBox=AxisZoomViewBox())
        self._plot_item = p
        _make_diag_axis(p, theme)
        title = f"{channels[0]} calibration · measured glass spots" if len(channels) == 1 else "Calibration slopes by element"
        p.setTitle(title, color=theme.axis_text, size="10pt")
        p.setLabel("left", "cps", color=theme.axis_text, size="9px")
        p.setLabel("bottom", "ppm", color=theme.axis_text, size="9px")
        legend = _rounded_legend(p, offset=(12, 12), labelTextColor=theme.axis_text, labelTextSize="9pt")
        if legend is not None:
            legend.setColumnCount(self._legend_column_count(len(channels)))

        for channel in channels:
            ch_data = data["channel_data"][channel]
            ppm = np.asarray(ch_data["ppm"], dtype=float)
            cps = np.asarray(ch_data["cps"], dtype=float)
            slope = float(ch_data["slope"])
            color = _DIAG_SPOT_COLORS[all_channels.index(channel) % len(_DIAG_SPOT_COLORS)]
            labels = ch_data.get("labels", [channel] * len(ppm))
            spot_names = ch_data.get("spot_names", labels)
            sample_symbols = dict(zip(dict.fromkeys(labels), ("o", "s", "t", "d")))
            scatter = pg.ScatterPlotItem(
                x=ppm,
                y=cps,
                symbol=[sample_symbols.get(label, "o") for label in labels],
                size=9,
                pen=pg.mkPen(theme.axis_text, width=0.9),
                brush=pg.mkBrush(color),
                hoverable=True,
                hoverSize=13,
                data=list(zip(spot_names, labels, [channel] * len(ppm))),
                tip=lambda x, y, spot: (
                    f"{spot[0]} · {spot[1]} · {spot[2]}\n"
                    f"Reference: {x:,.4g} ppm\nMeasured: {y:,.5g} cps"
                ),
            )
            scatter.setZValue(5)
            p.addItem(scatter)

            # Dashed origin-anchored slope line. Keep this as the legend item;
            # clicking the legend sample will toggle both this line and its scatter.
            # Notebook calibration slope is ppm/cps. In this plot x=ppm, y=cps,
            # so the physically consistent origin-anchored line is y = x / slope.
            if np.isfinite(slope) and slope > 0:
                line_xmax = float(np.nanmax(ppm)) * 1.05
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
        self._plot_item.vb.set_zoom_axes(self._zoom_mode)

    def _apply_interaction_mode(self) -> None:
        if self._plot_item is None:
            return
        if self._interaction_mode == "pan":
            self._plot_item.vb.setMouseMode(pg.ViewBox.PanMode)
        else:
            self._plot_item.vb.setMouseMode(pg.ViewBox.RectMode)


def _spot_time_spans(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Index acquisition intervals for spot tooltips on session-time axes."""
    if frame.empty or "spot_name" not in frame or "Time" not in frame:
        return np.array([]), np.array([]), []
    bounds = frame.groupby("spot_name", sort=False)["Time"].agg(["min", "max"])
    bounds = bounds.dropna().sort_values("min")
    return bounds["min"].to_numpy(), bounds["max"].to_numpy(), bounds.index.astype(str).tolist()


def _spot_at_time(spans: tuple[np.ndarray, np.ndarray, list[str]], time: float) -> str:
    starts, ends, names = spans
    index = int(np.searchsorted(starts, time, side="right")) - 1
    return names[index] if index >= 0 and time <= ends[index] else ""


class TracePlotWidget(RoundedPlotWidget):
    regionChanged = QtCore.Signal(str, tuple)
    xWindowChanged = QtCore.Signal(tuple)

    def __init__(self) -> None:
        super().__init__(viewBox=AxisZoomViewBox())
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
        self.plot_item.vb.sigResized.connect(self._position_signal_labels)
        self._configure_plot()

        # Floating spot-name label in the top-right corner
        self._spot_name_label = ElidedLabel("", self, elide_mode=QtCore.Qt.TextElideMode.ElideMiddle)
        self._spot_name_label.setObjectName("SpotOverlayLabel")
        self._spot_name_label.setStyleSheet(
            f"color: {self._theme.muted}; font-size: 11px; font-weight: 600;"
            " background: transparent; padding: 2px 6px;"
        )
        self._spot_name_label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self._spot_name_label.hide()

        self.show_preview()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "plot_item"):
            return
        narrow = self.window().width() < 1280
        self.plot_item.layout.setRowFixedHeight(3, 50 if narrow else 62)
        self.plot_item.getAxis("bottom").setHeight(44 if narrow else 54)
        self.plot_item.showAxis("bottom")
        self._reposition_spot_label()
        self._layout_trace_legend()

    def _layout_trace_legend(self) -> None:
        """Reserve a row outside the data view for narrow-window legends."""
        legend = self.plot_item.legend
        if legend is None:
            return
        outside = self.window().width() < 1280
        if outside != bool(getattr(legend, "_outside_trace", False)):
            # Anchored legends reposition themselves on every geometry change;
            # a fresh unanchored item lets the graphics layout own its position.
            replacement = RoundedLegendItem(
                offset=None if outside else (12, 8),
                labelTextColor=self._theme.axis_text, labelTextSize="9pt" if outside else "10pt",
            )
            for sample, label in legend.items:
                replacement.addItem(sample.item, label.text)
            if getattr(legend, "_outside_trace", False):
                self.plot_item.layout.removeItem(legend)
            if legend.scene() is not None:
                legend.scene().removeItem(legend)
            legend = replacement
            self.plot_item.legend = legend
            if outside:
                legend.setParentItem(self.plot_item)
                self.plot_item.layout.addItem(legend, 4, 1)
                legend.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
                legend.layout.setContentsMargins(4, 0, 4, 0)
            else:
                legend.setParentItem(self.plot_item.vb)
            legend._outside_trace = outside
        legend.setColumnCount(max(1, min(6, (self.width()-115)//100)) if outside else 2)

    def _update_spot_label(self, spot_name: str | None) -> None:
        if not hasattr(self, "_spot_name_label"):
            return
        if spot_name:
            self._spot_name_label.setText(spot_name)
            self._spot_name_label.setToolTip(spot_name)
            self._spot_name_label.adjustSize()
            self._reposition_spot_label()
            self._spot_name_label.show()
        else:
            self._spot_name_label.hide()

    def _reposition_spot_label(self) -> None:
        if not hasattr(self, "_spot_name_label"):
            return
        lbl = self._spot_name_label
        lbl.setMaximumWidth(max(1, self.width() - 28))
        lbl.adjustSize()
        margin = 14
        x = self.width() - lbl.width() - margin
        y = 0 if self.window().width() < 1280 else margin
        lbl.move(max(0, x), max(0, y))

    def _position_signal_labels(self) -> None:
        """Keep edge pills outside selection boxes and inside the view."""
        if getattr(self, "_positioning_signal_labels", False):
            return
        self._positioning_signal_labels = True
        try:
            view = self.plot_item.vb
            area = view.sceneBoundingRect().adjusted(5, 5, -5, -5)
            if area.width() <= 0 or area.height() <= 0:
                return
            obstacles = []
            for overlay in self._region_overlays.values():
                if "mean" not in overlay:
                    continue
                start, end = overlay["region"].getRegion()
                mean, spread = overlay["mean"], overlay["spread"]
                rect = QtCore.QRectF(
                    view.mapViewToScene(QtCore.QPointF(start, mean + spread)),
                    view.mapViewToScene(QtCore.QPointF(end, mean - spread)),
                ).normalized().adjusted(-4, -4, 4, 4)
                obstacles.append(rect)
            for name in ("signal_start", "signal_end"):
                line = self._signal_boundaries.get(name)
                if line is None:
                    continue
                label = line.label
                x = view.mapViewToScene(QtCore.QPointF(line.value(), 0)).x()
                label.setVisible(view.sceneBoundingRect().left() <= x <= view.sceneBoundingRect().right())
                if not label.isVisible():
                    continue
                size = label.sceneBoundingRect().size()
                width, height = size.width(), size.height()
                if width <= 0 or height <= 0:
                    continue
                left = x - width - 5 if name == "signal_start" else x + 5
                left = max(area.left(), min(left, area.right() - width))
                # Try the top first, then lanes above/below each obstacle.
                candidates = [area.top(), area.bottom() - height]
                for obstacle in obstacles:
                    candidates.extend((obstacle.top() - height - 5, obstacle.bottom() + 5))
                # A very short plot may lack two vertical lanes. In that case
                # slide along the nearest box edge rather than cover a pill.
                left_candidates = [left]
                for obstacle in obstacles:
                    left_candidates.extend((obstacle.left() - width - 5, obstacle.right() + 5))
                left_candidates = sorted(set(left_candidates), key=lambda x: abs(x - left))
                placement = next((
                    (x, y) for x in left_candidates for y in sorted(candidates)
                    if area.left() <= x <= area.right() - width
                    and area.top() <= y <= area.bottom() - height
                    and not any(QtCore.QRectF(x, y, width, height).intersects(rect)
                                for rect in obstacles)
                ), (left, area.top()))
                left, top = placement
                anchor = ((x - left) / width, 0.5)
                label.anchors = [anchor, anchor]
                position = 1 - (top + height / 2 - view.sceneBoundingRect().top()) / max(1, view.height())
                label.setPosition(position)
                obstacles.append(QtCore.QRectF(left, top, width, height).adjusted(-5, -5, 5, 5))
        finally:
            self._positioning_signal_labels = False

    def apply_theme(self, theme: ThemeSpec) -> None:
        self._theme = theme
        self.setBackground(theme.plot_background)
        self._configure_plot()
        if hasattr(self, "_spot_name_label"):
            self._spot_name_label.setStyleSheet(f"color: {theme.muted}; background: transparent; padding: 2px 6px; font-size: 11px;")

    def _configure_plot(self) -> None:
        self.setBackground(self._theme.plot_background)
        self.showGrid(x=True, y=True, alpha=self._theme.grid_alpha)
        self.setMenuEnabled(False)
        self._apply_zoom_mode()
        self._apply_interaction_mode()
        self.setClipToView(True)
        self.plot_item.hideButtons()
        self.plot_item.setContentsMargins(20, 16, 20, 8)
        narrow = self.window().width() < 1280
        self.plot_item.layout.setRowFixedHeight(3, 50 if narrow else 62)
        label_style = {"color": self._theme.axis_text, "font-size": "15px", "font-weight": "600"}
        self.plot_item.setLabel("bottom", "Acquisition Time (s)", **label_style)
        self.plot_item.setLabel("left", "Counts / cps", **label_style)
        tick_font = QtGui.QFont()
        tick_font.setPointSizeF(11.5)
        left_axis = self.plot_item.getAxis("left")
        bottom_axis = self.plot_item.getAxis("bottom")
        left_axis.setPen(self._theme.plot_grid)
        bottom_axis.setPen(self._theme.plot_grid)
        left_axis.setTextPen(self._theme.axis_text)
        bottom_axis.setTextPen(self._theme.axis_text)
        left_axis.setStyle(tickFont=tick_font, tickTextOffset=10, autoExpandTextSpace=False)
        bottom_axis.setStyle(tickFont=tick_font, tickTextOffset=6, autoExpandTextSpace=True)
        left_axis.setWidth(88)
        bottom_axis.setHeight(44 if narrow else 54)
        self.plot_item.showAxis("bottom")

    def _reset_items(self) -> None:
        legend = self.plot_item.legend
        if legend is not None and legend.scene() is not None:
            if getattr(legend, "_outside_trace", False):
                self.plot_item.layout.removeItem(legend)
            legend.scene().removeItem(legend)
            self.plot_item.legend = None
        self.plot_item.clear()
        self._region_overlays = {}
        self._signal_boundaries = {}
        self._signal_markers = {}
        self._hover_series = {}
        self._hover_spans = _spot_time_spans(pd.DataFrame())
        self._hover_spot_name = None
        self._measurement_layers = {}
        self._selection_times = np.array([], dtype=float)
        self._selection_channel = None
        self._configure_plot()
        _rounded_legend(self.plot_item, offset=(12, 8), labelTextColor=self._theme.axis_text, labelTextSize="10pt", colCount=2)
        self._layout_trace_legend()

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
        self._hover_spans = _spot_time_spans(spot_data)
        self._hover_spot_name = spot_name if "spot_name" not in spot_data else None
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
        self.plot_item.vb.set_zoom_axes(self._zoom_mode)

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
        )
        line.label = _PillLineLabel(line, text=label, position=0.93, color=self._theme.axis_text, movable=movable)
        line.label.textItem.document().setDocumentMargin(6)
        line.label.position_callback = self._position_signal_labels
        line.setBounds(tuple(sorted(float(item) for item in bounds)))
        line.setZValue(12)
        self.plot_item.addItem(line)
        self._signal_boundaries[name] = line
        self._position_signal_labels()
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
        self._position_signal_labels()

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
        self._position_signal_labels()
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
        self._position_signal_labels()

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
        if not self.plot_item.vb.sceneBoundingRect().contains(scene_position) or not self._hover_series:
            QtWidgets.QToolTip.hideText()
            return
        mouse_point = self.plot_item.vb.mapSceneToView(scene_position)
        x_value = float(mouse_point.x())
        spot = _spot_at_time(self._hover_spans, x_value) or self._hover_spot_name
        tooltip_lines = [f"Spot: {spot}"] if spot else []
        tooltip_lines.append(f"Time: {x_value:,.2f} s")

        for name, (x_values, y_values) in list(self._hover_series.items())[:6]:
            if len(x_values) == 0:
                continue
            index = int(np.clip(np.searchsorted(x_values, x_value), 0, len(x_values) - 1))
            if index > 0 and abs(x_values[index - 1] - x_value) < abs(x_values[index] - x_value):
                index -= 1
            tooltip_lines.append(f"{name}: {float(y_values[index]):,.2f}")

        tooltip_lines.extend(self._region_stats_text(x_value))
        QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), "\n".join(tooltip_lines), self)


class SessionOverviewWidget(RoundedPlotWidget):
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
        self._hover_proxy = pg.SignalProxy(
            self.scene().sigMouseMoved, rateLimit=30, slot=self._on_mouse_moved
        )
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
        left_axis.setPen(self._theme.plot_grid)
        bottom_axis.setPen(self._theme.plot_grid)
        left_axis.setTextPen(self._theme.axis_text)
        bottom_axis.setTextPen(self._theme.axis_text)
        left_axis.setStyle(tickFont=tick_font, tickTextOffset=8, autoExpandTextSpace=False)
        bottom_axis.setStyle(tickFont=tick_font, tickTextOffset=4, autoExpandTextSpace=True)
        left_axis.setWidth(68)
        bottom_axis.setHeight(44)

    def _reset_items(self) -> None:
        self.plot_item.clear()
        self._window_region = None
        self._hover_spans = _spot_time_spans(pd.DataFrame())
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
        self._hover_spans = _spot_time_spans(frame)
        x_values = x_values[finite_mask]
        y_values = y_values[finite_mask]
        curve = self.plot_item.plot(x_values, y_values, pen=pg.mkPen(color, width=1.4))
        curve.setDownsampling(auto=True, method="peak")
        curve.setClipToView(True)
        self._ensure_window_region(current_window)
        self.setXRange(float(np.min(x_values)), float(np.max(x_values)), padding=0.01)

    def _on_mouse_moved(self, event: tuple) -> None:
        if not event:
            return
        vb = self.plot_item.vb
        scene_pos = event[0]
        if vb.sceneBoundingRect().contains(scene_pos):
            time = vb.mapSceneToView(scene_pos).x()
            name = _spot_at_time(self._hover_spans, time)
            if name:
                QtWidgets.QToolTip.showText(
                    QtGui.QCursor.pos(), f"Spot: {name}\nSession time: {time:,.2f} s", self
                )
                return
        QtWidgets.QToolTip.hideText()

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


class StandardBiasWidget(RoundedGraphicsLayoutWidget):
    """Selectable reference-material date offsets supplied by UPbRPy.bias."""

    selectionChanged = QtCore.Signal(object)
    spotHovered = QtCore.Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent=parent)
        self._theme = active_theme()
        self._table = pd.DataFrame()
        self._summary = {}
        self._log_x = False
        self._per_sample_trends = False
        self._x_label = "U, ug/g"
        self._y_label = "Offset from accepted 206Pb/238U date (%)"
        self._zoom_mode = "xy"
        self._interaction_mode = "zoom"
        self.selected_spots = set()
        self._scatters = {}
        self._point_rows = []
        self._point_xy = np.empty((0, 2))
        self._hover_index = None
        self._legend = self._hover_text = self._trend_text = None
        self._plot_item = self.addPlot(0, 0, viewBox=_ConcordiaViewBox(self))
        for menu in (self._plot_item.ctrlMenu, self._plot_item.vb.menu):
            menu.setParent(self, menu.windowFlags())
        self._plot_item.vb.sigRangeChanged.connect(self._position_labels)
        self._plot_item.vb.sigResized.connect(self._position_labels)
        self.scene().sigMouseMoved.connect(self._on_mouse_moved)

    def show_bias(self, table, summary, *, theme, log_x=False, x_label, y_label) -> None:
        """Draw a bias table and matching summary; labels describe original units."""
        self._table = table.copy()
        self._summary = summary
        self._theme = theme
        self._log_x = bool(log_x)
        self._x_label, self._y_label = x_label, y_label
        self._draw()

    def apply_theme(self, theme: ThemeSpec) -> None:
        """Redraw using the supplied Studio palette."""
        self._theme = theme
        self._draw()

    def set_per_sample_trends(self, enabled: bool) -> None:
        """Show or hide the optional individual reference-material fits."""
        self._per_sample_trends = bool(enabled)
        self._draw()

    def _draw(self):
        plot, theme = self._plot_item, self._theme
        self._set_hover(None)
        plot.clear()
        for item in (self._legend, self._hover_text, self._trend_text):
            if item is not None and item.scene() is not None:
                item.scene().removeItem(item)
        self._legend = self._hover_text = self._trend_text = None
        self._scatters = {}
        self._point_rows = []
        self._point_xy = np.empty((0, 2))
        self._error_bars = []
        self._mean_lines = []
        self._trend_lines = []
        self.setBackground(theme.plot_background)
        _make_diag_axis(plot, theme)
        plot.setLogMode(x=self._log_x, y=False)
        plot.setLabel("bottom", self._x_label, color=theme.axis_text)
        plot.setLabel("left", self._y_label, color=theme.axis_text)
        for name in ("bottom", "left"):
            plot.getAxis(name).enableAutoSIPrefix(False)
        plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(theme.muted, width=1)))
        table = self._table
        if not table.empty:
            finite = table.plottable & np.isfinite(table.x) & np.isfinite(table.offset_percent)
            if self._log_x:
                finite &= table.x > 0
            visible = table.loc[finite]
            samples = sorted(table["sample"].astype(str).unique())
            colors = {name: _REVIEW_SAMPLE_COLORS[i % len(_REVIEW_SAMPLE_COLORS)]
                      for i, name in enumerate(samples)}
            self._legend = _rounded_legend(plot, offset=(-10, 32), labelTextColor=theme.axis_text,
                                           labelTextSize="9pt", verSpacing=0)
            self._legend.anchor((1, 0), (1, 0), offset=(-10, 32))
            for sample, group in visible.groupby("sample", sort=True):
                color = colors[str(sample)]
                x = group.x.to_numpy(dtype=float)
                y = group.offset_percent.to_numpy(dtype=float)
                display_x = np.log10(x) if self._log_x else x
                scatter = pg.ScatterPlotItem(x=display_x, y=y, size=8, brush=color,
                                             pen=pg.mkPen(color, width=0.8))
                # ScatterPlotItem and ErrorBarItem have no pyqtgraph log transform;
                # only their coordinates are mapped, while the axis uses log mode.
                scatter.sigClicked.connect(self._spot_clicked)
                scatter._bias_rows = group.to_dict("records")
                scatter._bias_color = color
                self._scatters[sample] = scatter
                plot.addItem(scatter)
                self._point_rows.extend(scatter._bias_rows)
                errors = group.offset_2s_percent.to_numpy(dtype=float)
                valid_error = np.isfinite(errors) & (errors >= 0)
                bars = pg.ErrorBarItem(x=display_x[valid_error], y=y[valid_error],
                                      top=errors[valid_error], bottom=errors[valid_error],
                                      pen=pg.mkPen(color, width=1))
                plot.addItem(bars)
                self._error_bars.append(bars)
                stats = self._summary.get("samples", {}).get(sample, {})
                mean, error = stats.get("weighted_mean", np.nan), stats.get("weighted_mean_2e", np.nan)
                self._legend.addItem(scatter, f"{sample}  {mean:+.1f} ± {error:.1f} %  n {stats.get('n', 0)}")
                if np.isfinite(mean):
                    self._mean_lines.append(plot.plot([x.min(), x.max()], [mean, mean],
                        pen=pg.mkPen(color, width=1.5, style=QtCore.Qt.PenStyle.DashLine)))
                if self._per_sample_trends:
                    self._draw_trend(self._summary.get("trend_by_sample", {}).get(sample), color)
            self._point_xy = np.array([
                [np.log10(row['x']) if self._log_x else row['x'], row['offset_percent']]
                for row in self._point_rows], dtype=float).reshape(-1, 2)
        self._draw_trend(self._summary.get("trend"), theme.text)
        background = theme_color(theme.surface)
        background.setAlpha(245)
        self._hover_text = pg.TextItem(color=theme.axis_text, fill=pg.mkBrush(background),
                                      border=pg.mkPen(theme_color(theme.input_border)))
        self._hover_text.setParentItem(plot.vb)
        self._hover_text.setZValue(30)
        self._hover_text.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self._hover_text.hide()
        fit = self._summary.get("trend")
        self._trend_text = pg.TextItem(color=theme.text)
        self._trend_text.setParentItem(plot.vb)
        self._trend_text.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self._trend_text.setZValue(20)
        self._trend_text.setText(self._slope_text(fit) if fit else "Trend unavailable (requires 3 points with errors and x spread)")
        self._update_selection_style()
        plot.autoRange()
        self._apply_zoom_mode()
        self._apply_interaction_mode()
        self._position_labels()

    @staticmethod
    def _slope_text(fit):
        units = fit['units'].replace('percent', '%')
        return (f"slope {fit['slope']:+.3g} ± {fit['slope_2s']:.3g} {units} (2σ), "
                f"p {fit['p_value']:.2g}, n {fit['n']}")

    def _draw_trend(self, fit, color):
        if not fit:
            return
        bounds = fit["x_range"]
        x = np.geomspace(*bounds, 100) if self._log_x else np.linspace(*bounds, 100)
        y = fit["intercept"] + fit["slope"] * (np.log10(x) if self._log_x else x)
        self._trend_lines.append(self._plot_item.plot(x, y, pen=pg.mkPen(color, width=1.8)))

    def set_selected_spots(self, names) -> None:
        """Highlight spot names without emitting selectionChanged."""
        self.selected_spots = set(names)
        self._update_selection_style()

    def select_spots(self, names, *, extend=False, toggle=False) -> None:
        """Update the selection and emit only when its set changes."""
        names = set(names)
        selected = (self.selected_spots ^ names if toggle else
                    self.selected_spots | names if extend else names)
        if selected != self.selected_spots:
            self.set_selected_spots(selected)
            self.selectionChanged.emit(set(selected))

    def _update_selection_style(self):
        for scatter in self._scatters.values():
            chosen = [str(row["spot_name"]) in self.selected_spots for row in scatter._bias_rows]
            scatter.setPen([pg.mkPen(scatter._bias_color, width=2.5 if selected else 0.8)
                            for selected in chosen])
            scatter.setSize([11 if selected else 8 for selected in chosen])

    def supports_select_mode(self) -> bool:
        """Return True because all displayed points represent individual spots."""
        return True

    def _select_at(self, scene_pos, modifiers):
        index = self.nearest_point(scene_pos, radius=10)
        names = {str(self._point_rows[index]["spot_name"])} if index is not None else set()
        toggle = bool(modifiers & (QtCore.Qt.KeyboardModifier.ControlModifier
                                   | QtCore.Qt.KeyboardModifier.MetaModifier))
        extend = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
        if names or not (toggle or extend):
            self.select_spots(names, extend=extend, toggle=toggle)

    def _spot_clicked(self, scatter, points, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            if event.double():
                self._reset_from_double_click(event.scenePos())
            else:
                self._select_at(event.scenePos(), event.modifiers())

    def select_rectangle(self, rect, *, extend=False) -> None:
        """Select centres in view coordinates, including log coordinates."""
        rect = rect.normalized()
        self.select_spots({str(row["spot_name"]) for row, xy in zip(self._point_rows, self._point_xy)
                           if rect.contains(QtCore.QPointF(*xy))}, extend=extend)

    def nearest_point(self, scene_pos, *, radius=6):
        """Return the nearest visible point within a radius in screen pixels."""
        return ConcordiaReviewWidget.nearest_point(self, scene_pos, radius=radius)

    def _reset_from_double_click(self, scene_pos):
        self._plot_item.autoRange()

    def tooltip_text(self, index: int) -> str:
        """Return the spot's date, reference value and offset with absolute 2s."""
        row = self._point_rows[index]
        return (f"{row['spot_name']}\n{row['sample']}\n{self._x_label}: {row['x']:.5g}\n"
                f"Date: {row['date']:.5g} ± {row['date_2s']:.4g} Ma (2σ)\n"
                f"Accepted {row['date_system']}: {row['accepted']:.5g} Ma\n"
                f"Offset: {row['offset_percent']:+.4g} ± {row['offset_2s_percent']:.4g} % (2σ)")

    def _on_mouse_moved(self, scene_pos):
        self._set_hover(self.nearest_point(scene_pos))

    def _set_hover(self, index):
        if index == self._hover_index:
            return
        self._hover_index = index
        if index is None:
            if self._hover_text is not None:
                self._hover_text.hide()
            self.spotHovered.emit("")
            return
        self._hover_text.setHtml('<div style="color:' + self._theme.axis_text +
            '; padding:3px; font-size:11px">' + escape(self.tooltip_text(index)).replace('\n', '<br>') + '</div>')
        self._hover_text.show()
        self._position_labels()
        self.spotHovered.emit(str(self._point_rows[index]["spot_name"]))

    def _position_labels(self, *args):
        vb = self._plot_item.vb
        if self._trend_text is not None:
            self._trend_text.setPos(8, 8)
        if self._hover_index is not None and self._hover_text is not None:
            point = vb.mapFromView(QtCore.QPointF(*self._point_xy[self._hover_index]))
            bounds = self._hover_text.boundingRect()
            self._hover_text.setPos(max(4, min(point.x() + 12, vb.width() - bounds.width() - 4)),
                                    max(4, min(point.y() + 12, vb.height() - bounds.height() - 4)))

    def leaveEvent(self, event):
        self._set_hover(None)
        super().leaveEvent(event)

    def set_zoom_mode(self, mode: str) -> None:
        """Constrain mouse zoom to xy, x, or y."""
        self._zoom_mode = mode if mode in {"xy", "x", "y"} else "xy"
        self._apply_zoom_mode()

    def zoom_mode(self) -> str:
        """Return the current zoom-axis constraint."""
        return self._zoom_mode

    def set_interaction_mode(self, mode: str) -> None:
        """Set pan, rectangle zoom, or rectangle spot selection."""
        self._interaction_mode = mode if mode in {"pan", "zoom", "select"} else "zoom"
        self._apply_interaction_mode()

    def interaction_mode(self) -> str:
        """Return the current mouse interaction mode."""
        return self._interaction_mode

    def _apply_zoom_mode(self):
        self._plot_item.vb.set_zoom_axes(self._zoom_mode)

    def _apply_interaction_mode(self):
        self._plot_item.vb.setMouseMode(pg.ViewBox.PanMode if self._interaction_mode == "pan"
                                       else pg.ViewBox.RectMode)


class StandardBiasControls(QtWidgets.QWidget):
    """Compact date and x selectors with an externally supplied bias summary."""

    changed = QtCore.Signal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from UPbRPy.bias import BIAS_DATE_SYSTEMS, BIAS_QUANTITIES

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        form = QtWidgets.QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.date_combo = StyledComboBox()
        self.quantity_combo = StyledComboBox()
        for system in BIAS_DATE_SYSTEMS:
            self.date_combo.addItem("Preferred" if system == "preferred" else system, system)
        for key, label in BIAS_QUANTITIES.items():
            self.quantity_combo.addItem(label, key)
        for combo in (self.date_combo, self.quantity_combo):
            combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(5)
            combo.setMinimumWidth(0)
        form.addRow("Date", self.date_combo)
        form.addRow("X", self.quantity_combo)
        layout.addLayout(form)
        self.log_x_check = QtWidgets.QCheckBox("Log x")
        self.per_sample_trends_check = QtWidgets.QCheckBox("Per-standard trends")
        layout.addWidget(self.log_x_check)
        layout.addWidget(self.per_sample_trends_check)
        self.summary_label = QtWidgets.QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.summary_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)
        self.date_combo.currentIndexChanged.connect(self._emit_changed)
        self.quantity_combo.currentIndexChanged.connect(self._emit_changed)
        self.log_x_check.toggled.connect(self._emit_changed)
        self.per_sample_trends_check.toggled.connect(self._emit_changed)

    def state(self) -> dict:
        """Return date, quantity, log_x and per_sample_trends settings."""
        return {"date": self.date_combo.currentData(), "quantity": self.quantity_combo.currentData(),
                "log_x": self.log_x_check.isChecked(),
                "per_sample_trends": self.per_sample_trends_check.isChecked()}

    def set_state(self, state: dict) -> None:
        """Apply known settings without emitting changed; ignore unknown keys."""
        widgets = (self.date_combo, self.quantity_combo, self.log_x_check, self.per_sample_trends_check)
        blockers = [QtCore.QSignalBlocker(widget) for widget in widgets]
        for key, combo in (("date", self.date_combo), ("quantity", self.quantity_combo)):
            if key in state:
                index = combo.findData(state[key])
                if index >= 0:
                    combo.setCurrentIndex(index)
        for key, check in (("log_x", self.log_x_check), ("per_sample_trends", self.per_sample_trends_check)):
            if key in state:
                check.setChecked(bool(state[key]))
        del blockers

    def set_summary(self, summary: dict) -> None:
        """Show the pooled slope and sample weighted means in percent (2s)."""
        fit = summary.get("trend")
        lines = [StandardBiasWidget._slope_text(fit) if fit else "Trend unavailable"]
        for sample, stats in summary.get("samples", {}).items():
            lines.append(f"{sample}: {stats['weighted_mean']:+.1f} ± {stats['weighted_mean_2e']:.1f} % (2σ), n {stats['n']}")
        self.summary_label.setText("\n".join(lines))

    def _emit_changed(self, *args):
        self.changed.emit(self.state())
