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
            assert reduction.project_summary_label.fontMetrics().horizontalAdvance("No experiment loaded") <= reduction.project_summary_label.width()
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
    source = Path(__file__).parent / "fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
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
                card = reduction.step_actions_block
                caption = reduction.step_actions_caption
                assert not caption.findChildren(QtWidgets.QPushButton)
                title = reduction.step_actions_title
                status = reduction.workflow_status_label
                assert caption.height() <= 28
                previous_bottom = 0
                for widget in (title, status):
                    if not widget.isVisible():
                        continue
                    origin = widget.mapTo(caption, QtCore.QPoint())
                    assert origin.x() >= 0
                    assert origin.y() >= previous_bottom
                    assert origin.y() + widget.height() <= caption.height()
                    assert origin.x() + widget.width() <= caption.width()
                    shown = QtWidgets.QLabel.text(widget)
                    assert widget.fontMetrics().horizontalAdvance(shown) <= widget.width()
                    assert widget.fontMetrics().height() <= widget.height()
                    assert widget.toolTip() == card.toolTip()
                    previous_bottom = origin.y() + widget.height()
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
