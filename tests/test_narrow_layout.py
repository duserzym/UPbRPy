"""Responsive chrome keeps primary controls readable at narrow widths."""
from pathlib import Path
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets
from GUI.theme import active_theme, build_application
from GUI.widgets import WrappingButton


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def pump(app):
    for _ in range(15):
        app.processEvents()


@pytest.fixture(params=["light", "dark"])
def shell(app, request):
    from GUI.studio_window import StudioWindow
    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved = {key: settings.value(key) for key in settings.allKeys()}
    settings.clear()
    window = StudioWindow(theme_mode=request.param)
    window.resize(1440, 900)
    window.show()
    pump(app)
    try:
        yield window
    finally:
        for index in range(window.tabs.count()):
            window.tabs.widget(index).has_unsaved_changes = lambda: False
        window.close()
        pump(app)
        settings.clear()
        for key, value in saved.items():
            settings.setValue(key, value)


def assert_controls_fit(window):
    for scroll, bar in window._toolbar_strips:
        assert scroll.horizontalScrollBar().maximum() == 0
        assert bar.width() <= scroll.viewport().width()
        for button in bar.findChildren(QtWidgets.QToolButton):
            if button.isVisibleTo(bar):
                assert button.fontMetrics().horizontalAdvance(button.text()) <= button.width()-8
    for button in window.findChildren(QtWidgets.QPushButton):
        if not button.isVisibleTo(window):
            continue
        parent = button.parentWidget()
        assert parent.rect().contains(button.geometry()), button.text()
        option = QtWidgets.QStyleOptionButton()
        button.initStyleOption(option)
        rect = button.style().subElementRect(QtWidgets.QStyle.SubElement.SE_PushButtonContents, option, button)
        if isinstance(button, WrappingButton):
            bounds = button.fontMetrics().boundingRect(rect, QtCore.Qt.TextFlag.TextWordWrap, button.text())
            assert bounds.height() <= rect.height()
            assert bounds.width() <= rect.width()
        else:
            assert button.fontMetrics().horizontalAdvance(button.text()) <= rect.width(), button.text()


def test_all_requested_widths_and_loaded_states(app, shell):
    window = shell.active_reduction
    fixture = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"
    for loaded in (False, True):
        if loaded:
            window.load_experiment_directory(str(fixture), background=False)
        for width, height in ((760, 620), (900, 700), (1024, 768), (1000, 768), (1180, 760), (1280, 800), (1440, 900)):
            shell.resize(width, height)
            pump(app)
            assert shell.size() == QtCore.QSize(width, height)
            assert_controls_fit(window)
            title = window.header_title
            assert title.fontMetrics().horizontalAdvance(title.text()) <= title.width()
            legend = window.trace_plot.plot_item.legend
            if width < 1280:
                assert not legend.sceneBoundingRect().intersects(window.trace_plot.plot_item.vb.sceneBoundingRect())
                assert window.trace_plot.sceneRect().contains(legend.sceneBoundingRect())
                boundaries = window.trace_plot._signal_boundaries
                if "signal_start" in boundaries and "signal_end" in boundaries:
                    assert not boundaries["signal_start"].label.sceneBoundingRect().intersects(
                        boundaries["signal_end"].label.sceneBoundingRect())
            if width < 1000:
                assert window._width_mode == "compact"
                assert window.sidebar_panel.width() == 80
                assert window.inspector_panel.isHidden()
                assert window.trace_plot.width() >= 600
                assert window.step_primary_button.icon().isNull() is False
                assert window.step_primary_button.toolTip()
            elif width < 1280:
                assert window._width_mode == "narrow"
                rail = window.workflow_step_list
                for row in range(rail.count()):
                    item = rail.item(row)
                    option = QtWidgets.QStyleOptionViewItem()
                    rail.itemDelegate().initStyleOption(option, rail.indexFromItem(item))
                    option.rect = rail.visualItemRect(item)
                    rect = rail.style().subElementRect(QtWidgets.QStyle.SubElement.SE_ItemViewItemText, option, rail)
                    assert option.fontMetrics.horizontalAdvance(item.text()) <= rect.width()


def test_compact_actions_settings_and_remembered_inspector(app, shell):
    window = shell.active_reduction
    fixture = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"
    window.load_experiment_directory(str(fixture), background=False)
    shell.resize(760, 620)
    pump(app)
    for row in range(window.workflow_step_list.count()):
        window.workflow_step_list.setCurrentRow(row)
        pump(app)
        assert_controls_fit(window)
        rail = window.workflow_step_list
        assert rail.verticalScrollBar().maximum() == 0
        assert rail.viewport().rect().contains(rail.visualItemRect(rail.item(9)))
        for widget in window.sidebar_panel.findChildren(QtWidgets.QWidget):
            if widget.isVisibleTo(window.sidebar_panel) and widget.width() and widget.height():
                rect = QtCore.QRect(widget.mapTo(window.sidebar_panel, QtCore.QPoint()), widget.size())
                assert window.sidebar_panel.rect().contains(rect), widget.objectName()
    window.autopick_settings_toggle.click()
    pump(app)
    assert window.compact_settings_panel.isVisible()
    assert window.workflow_combo.isVisible()
    assert window.selection_threshold_percent_spin.isVisible()
    assert window.centralWidget().rect().contains(window.compact_settings_panel.geometry())
    window.autopick_settings_toggle.click()
    window.inspector_toggle.click()
    pump(app)
    assert window.inspector_panel.isVisible()
    assert window.workspace_splitter.count() == 2
    assert window.centralWidget().rect().contains(window.inspector_panel.geometry())
    shell.resize(1005, 700)
    pump(app)
    assert window._width_mode == "compact"
    shell.resize(1024, 768)
    pump(app)
    assert window._width_mode == "narrow"
    assert window.workspace_splitter.count() == 3
    shell.resize(900, 700)
    pump(app)
    assert window.inspector_toggle.isChecked()
    assert window.inspector_panel.isVisible()
    window.inspector_toggle.click()
    assert str(window.settings.value("compact_inspector_visible_v1")).lower() == "false"


def test_compact_action_icons_remain_visible_when_disabled(app, shell):
    window = shell.active_reduction
    shell.resize(796, 720)
    pump(app)
    assert not window.repick_all_spots_button.isEnabled()
    assert not window.export_results_button.isEnabled()
    fixture = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"

    for loaded in (False, True):
        if loaded:
            window.load_experiment_directory(str(fixture), background=False)
        # Stage actions are rebuilt when the selected workflow row changes.
        for row in range(window.workflow_step_list.count()):
            window.workflow_step_list.setCurrentRow(row)
            pump(app)
            buttons = [
                window.save_project_button, window.export_results_button,
                window.import_button, window.repick_all_spots_button,
                window.reference_library_button, window.autopick_settings_toggle,
                window.step_primary_button, *window.step_secondary_buttons,
            ]
            for button in buttons:
                assert not button.text()
                assert not button.icon().isNull(), button.toolTip()
                was_enabled = button.isEnabled()
                try:
                    for enabled in (True, False):
                        button.setEnabled(enabled)
                        mode = QtGui.QIcon.Mode.Normal if enabled else QtGui.QIcon.Mode.Disabled
                        for state in (QtGui.QIcon.State.Off, QtGui.QIcon.State.On):
                            pixmap = button.icon().pixmap(button.iconSize(), mode, state)
                            assert not pixmap.isNull(), button.toolTip()
                            image = pixmap.toImage()
                            color = max(
                                (image.pixelColor(x, y) for x in range(image.width())
                                 for y in range(image.height())), key=lambda c: c.alpha(),
                            )
                            assert color.alpha() > 0, button.toolTip()
                            if not enabled:
                                assert 0.35 <= color.alphaF() <= 0.45
                                # Disabled accent buttons have a neutral background.
                                expected = QtGui.QColor(active_theme().text)
                                assert all(abs(a - b) <= 3 for a, b in zip(
                                    color.getRgb()[:3], expected.getRgb()[:3],
                                ))
                finally:
                    button.setEnabled(was_enabled)


def test_spot_names_keep_suffixes_and_full_tooltips(app, shell):
    from types import SimpleNamespace
    import pandas as pd

    window = shell.active_reduction
    fixture = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"
    window.load_experiment_directory(str(fixture), background=False)
    shell.resize(1024, 768)
    pump(app)
    tree = window.spot_list
    assert tree.textElideMode() == QtCore.Qt.TextElideMode.ElideMiddle
    iterator = QtWidgets.QTreeWidgetItemIterator(tree)
    spot_names = []
    while item := iterator.value():
        if item.childCount() == 0:
            assert item.toolTip(0) == item.text(0)
            spot_names.append(item.text(0))
        iterator += 1
    assert spot_names
    for suffix in ("323", "324"):
        name = f"SAMPLE-A_S__{suffix}"
        shown = tree.fontMetrics().elidedText(name, tree.textElideMode(), 110)
        assert "…" in shown and shown.endswith(suffix)
    header = window.project_name_label
    header.setFixedWidth(120)
    pump(app)
    shown = QtWidgets.QLabel.text(header)
    assert "…" in shown and shown.endswith(".csv")
    assert header.toolTip() == fixture.name

    results = window.results_table
    frame = pd.DataFrame({"spot_name": spot_names})
    results.show_results(SimpleNamespace(parallel_results=frame))
    assert results.spot_choice_list.textElideMode() == QtCore.Qt.TextElideMode.ElideMiddle
    for index, name in enumerate(spot_names):
        assert results.spot_choice_list.item(index).toolTip() == name
    results.show_depth_profile(frame, spot_names[0])
    combo = results.depth_spot_combo
    assert combo.view().textElideMode() == QtCore.Qt.TextElideMode.ElideMiddle
    assert combo._elide_mode == QtCore.Qt.TextElideMode.ElideMiddle
    for index, name in enumerate(spot_names):
        assert combo.itemData(index, QtCore.Qt.ItemDataRole.ToolTipRole) == name
    combo.setCurrentIndex(1)
    assert combo.toolTip() == spot_names[1]


def test_signal_pills_stay_by_edges_and_avoid_boxes(app, shell):
    window = shell.active_reduction
    fixture = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"
    window.load_experiment_directory(str(fixture), background=False)
    plot = window.trace_plot
    start = plot._signal_boundaries["signal_start"]
    end = plot._signal_boundaries["signal_end"]
    for width, height in ((1024, 768), (1180, 760), (760, 620), (1440, 900)):
        shell.resize(width, height)
        pump(app)
        vb = plot.plot_item.vb
        start_x, end_x = start.value(), end.value()
        # Also force the edges close together and up against either view edge.
        for bounds in (plot.visible_x_range(), (start_x - 0.1, end_x + 0.1),
                       (start_x - 1, end_x + 500)):
            plot.set_x_window(bounds)
            pump(app)
            rects = [line.label.sceneBoundingRect() for line in (start, end)]
            assert not rects[0].intersects(rects[1])
            for rect in rects:
                assert vb.sceneBoundingRect().contains(rect)
                for overlay in plot._region_overlays.values():
                    if "mean" not in overlay:
                        continue
                    left, right = overlay["region"].getRegion()
                    mean, spread = overlay["mean"], overlay["spread"]
                    box = QtCore.QRectF(
                        vb.mapViewToScene(QtCore.QPointF(left, mean + spread)),
                        vb.mapViewToScene(QtCore.QPointF(right, mean - spread)),
                    ).normalized()
                    assert not rect.intersects(box)
