"""Capture the status-bar update button and popup without network access.

Run from the worktree with QT_QPA_PLATFORM=offscreen when no display is
available. SHOT_DIR selects the output folder; defaults to docs/assets/screenshots.
"""

from pathlib import Path
import json
import os
import sys
import tempfile

# Executing this file directly must import this worktree's GUI package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtTest

from GUI.main_window import MainWindow
from GUI.studio_window import StudioWindow
from GUI.theme import build_application
from GUI.updater import Updater


def main():
    output = Path(os.environ.get("SHOT_DIR", ROOT / "docs/assets/screenshots"))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="upbrpy-update-shots-") as settings_dir:
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            # The offscreen plugin otherwise supplies an 800 x 800 screen,
            # forcing the popup away from a button in the 1512 x 982 window.
            screen_config = Path(settings_dir) / "screen.json"
            screen_config.write_text(json.dumps({"screens": [{"width": 1512, "height": 982}]}))
            os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={screen_config}"
        # Keep screenshot runs separate from the user's saved geometry/preferences.
        QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
        QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat,
                                 QtCore.QSettings.Scope.UserScope, settings_dir)
        original_settings = QtCore.QSettings

        class ScreenshotSettings(original_settings):
            def __init__(self, *args, **kwargs):
                if args and isinstance(args[0], str) and (len(args) < 2 or isinstance(args[1], str)):
                    super().__init__(self.Format.IniFormat, self.Scope.UserScope, *args, **kwargs)
                else:
                    super().__init__(*args, **kwargs)

        QtCore.QSettings = ScreenshotSettings
        app = build_application([], "light")
        app.setQuitOnLastWindowClosed(False)
        for scenario in ("idle", "available"):
            settings = QtCore.QSettings(str(Path(settings_dir) / f"{scenario}.ini"),
                                        QtCore.QSettings.Format.IniFormat)
            settings.setValue("updates/auto_download", False)
            updater = Updater(preview=scenario, version="0.1.0", settings=settings)
            shell = StudioWindow(reduction_factory=lambda: MainWindow(updater=updater))
            shell.resize(1512, 982)
            shell.show()
            QtTest.QTest.qWait(100)
            button = shell.active_reduction.check_updates_button
            if scenario == "available":
                button.click()
                QtTest.QTest.qWait(700)
            app.processEvents()
            capture = shell.grab()
            popup = button.popover
            if popup.isVisible():
                # Qt.Popup is a separate native window and is not in shell.grab().
                painter = QtGui.QPainter(capture)
                painter.drawPixmap(shell.mapFromGlobal(popup.pos()), popup.grab())
                painter.end()
            path = output / f"check-updates-{scenario}.png"
            if not capture.save(str(path)):
                raise RuntimeError(f"Could not save {path}")
            print(path)
            popup.hide()
            updater._shutdown()
            shell.close()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
