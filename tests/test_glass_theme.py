"""Glass chrome fits laptop viewports in both palettes."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets

from GUI.theme import THEMES, active_theme, build_application, theme_color


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or build_application([], "light")


def pump(app):
    for _ in range(12):
        app.processEvents()


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_glass_tokens_are_valid(mode):
    theme = THEMES[mode]
    for value in (theme.glass, theme.glass_strong, theme.glass_border,
                  theme.glass_edge, theme.input_border, theme.accent_soft):
        color = theme_color(value)
        assert color.isValid() and 0 < color.alphaF() < 1
    assert theme_color(theme.plot_background).alpha() == 255
    assert all(theme_color(color).isValid() for color in theme.wallpaper)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_chrome_and_inspector_stay_inside_their_viewports(app, mode):
    from GUI.studio_window import StudioWindow

    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved = {key: settings.value(key) for key in settings.allKeys()}
    settings.clear()
    shell = StudioWindow(theme_mode=mode)
    shell.show()
    reduction = shell.active_reduction
    try:
        assert active_theme().name == mode
        for width, height in ((1440, 900), (1280, 800)):
            shell.resize(width, height)
            reduction.apply_default_layout()
            pump(app)
            for scroll, bar in reduction._toolbar_strips:
                assert scroll.horizontalScrollBar().maximum() == 0
                for button in bar.findChildren(QtWidgets.QAbstractButton):
                    if button.isVisibleTo(bar):
                        assert button.x() >= 0
                        assert button.geometry().right() < bar.width()
            rail = reduction.workflow_step_list
            last = rail.visualItemRect(rail.item(rail.count()-1))
            assert last.bottom() <= rail.viewport().height()
            assert reduction.project_name_label.fontMetrics().horizontalAdvance("No experiment loaded") <= reduction.project_name_label.width()
            # Scrolling vertically is fine; hiding the horizontal scrollbar must
            # never leave fields and their arrow buttons outside the viewport.
            for index in (1, 2):
                reduction.inspector_tabs.setCurrentIndex(index)
                pump(app)
                for scroll in reduction.inspector_tabs.currentWidget().findChildren(QtWidgets.QScrollArea):
                    if scroll.isVisibleTo(shell):
                        assert scroll.widget().width() <= scroll.viewport().width()
            reduction.inspector_tabs.setCurrentIndex(0)
            reduction.autopick_settings_toggle.setChecked(True)
            pump(app)
            for button in (reduction.save_project_button, reduction.export_results_button,
                           reduction.import_button, reduction.repick_all_spots_button):
                assert button.geometry().bottom() <= reduction.sidebar_panel.height()
            reduction.autopick_settings_toggle.setChecked(False)
    finally:
        reduction.has_unsaved_changes = lambda: False
        shell.close()
        pump(app)
        settings.clear()
        for key, value in saved.items():
            settings.setValue(key, value)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_step_card_spacing_and_full_stage_labels_on_resize(app, mode):
    from pathlib import Path
    from GUI.studio_window import StudioWindow

    settings = QtCore.QSettings("UPbRPy", "Studio")
    saved = {key: settings.value(key) for key in settings.allKeys()}
    settings.clear()
    shell = StudioWindow(theme_mode=mode)
    shell.resize(1440, 900)
    shell.show()
    reduction = shell.active_reduction
    source = Path(__file__).parent / "fixtures/qtegra/Zircon_04Nov26_slice/Zircon_04Nov26_slice_Rich.csv"
    try:
        for loaded in (False, True):
            if loaded:
                reduction.load_experiment_directory(str(source), background=False)
                assert reduction.session.loaded
            shell.resize(1440, 900)
            reduction.apply_default_layout()
            pump(app)
            for width, height in ((1440, 900), (1280, 800)):
                # Exercise the resize path without resetting the layout at 1280.
                shell.resize(width, height)
                pump(app)
                card = reduction.workflow_context_bar
                assert not card.findChildren(QtWidgets.QPushButton)
                title = reduction.workflow_hint_title
                status = reduction.workflow_status_label
                icon = reduction.workflow_context_icon
                margins = card.layout().contentsMargins()
                assert margins.top() == margins.bottom()
                assert card.height() <= max(20, title.sizeHint().height()) + 42
                previous_right = None
                for widget in (icon, title, status):
                    origin = widget.mapTo(card, QtCore.QPoint())
                    assert origin.x() >= 19
                    assert origin.y() >= 14
                    assert origin.y() + widget.height() <= card.height() - 14
                    assert origin.x() + widget.width() <= card.width() - 19
                    if previous_right is not None:
                        assert origin.x() - previous_right >= 8
                    previous_right = origin.x() + widget.width()
                assert status.alignment() & QtCore.Qt.AlignmentFlag.AlignRight
                assert reduction.workflow_hint_body.parentWidget() is reduction.step_actions_block
                assert reduction.workflow_hint_body.toolTip() == card.toolTip()
                assert reduction.workflow_hint_body.text()
                for button in reduction.plot_mode_buttons.values():
                    assert button.fontMetrics().horizontalAdvance(button.text()) <= button.width() - 8
                if width == 1280:
                    assert reduction.workspace_splitter.sizes()[0] <= 195
                    assert reduction.workspace_splitter.sizes()[2] <= 335
            status.setText("Selection status " * 100)
            pump(app)
            assert QtWidgets.QLabel.text(status).endswith("…")
            assert status.text() == "Selection status " * 100
    finally:
        reduction.has_unsaved_changes = lambda: False
        shell.close()
        pump(app)
        settings.clear()
        for key, value in saved.items():
            settings.setValue(key, value)
