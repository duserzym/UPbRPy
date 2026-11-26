"""Readability and action emphasis in Studio's two presentation palettes."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtWidgets

from GUI.theme import THEMES, build_application


def contrast(first, second):
    def luminance(value):
        color = QtGui.QColor(value)
        channels = [color.redF(), color.greenF(), color.blueF()]
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, (.2126, .7152, .0722)))
    low, high = sorted((luminance(first), luminance(second)))
    return (high + .05) / (low + .05)


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_action_and_secondary_text_contrast(mode):
    theme = THEMES[mode]
    assert contrast(theme.on_accent, theme.accent) >= 4.5
    for background in (theme.surface, theme.surface_soft):
        assert contrast(theme.accent, background) >= 4.5
        assert contrast(theme.muted, background) >= 4.5


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_export_emphasis_and_named_icon_actions(mode, monkeypatch):
    from GUI import main_window

    app = QtWidgets.QApplication.instance() or build_application([], mode)
    monkeypatch.setattr(main_window.Updater, "start_scheduler", lambda self: None)
    window = main_window.MainWindow(theme_mode=mode)
    try:
        window.resize(1280, 800)
        window.show()
        for _ in range(6):
            app.processEvents()
        # Isolate the presentation of a ready export from any reduction work.
        window.export_results_button.setEnabled(True)
        window.repick_all_spots_button.setEnabled(True)
        window.step_next_button.setEnabled(True)
        theme = THEMES[mode]

        def fill(button):
            image = button.grab().toImage()
            return image.pixelColor(image.width() // 2, 3).name()

        assert fill(window.export_results_button) == theme.accent
        assert fill(window.repick_all_spots_button) != theme.accent
        assert fill(window.step_next_button) != theme.accent
        folder = next(button for button in window.findChildren(QtWidgets.QToolButton)
                      if button.text() == "+ Folder")
        assert folder.toolButtonStyle() == QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly
        assert not folder.icon().isNull()
        for button in (folder, window.spot_tree_actions_button):
            interface = QtGui.QAccessible.queryAccessibleInterface(button)
            assert interface.text(QtGui.QAccessible.Text.Name) == button.toolTip()
    finally:
        window.has_unsaved_changes = lambda: False
        window.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
