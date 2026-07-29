from __future__ import annotations

import argparse
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtWidgets

from GUI.icon import build_app_icon
from GUI.main_window import MainWindow
from GUI.theme import build_application


def export_window(window: QtWidgets.QMainWindow, output_path: str) -> None:
    window.show()
    for _ in range(4):
        QtWidgets.QApplication.processEvents()
    window.grab().save(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UPbRPy PySide6 + pyqtgraph GUI")
    parser.add_argument("--export", metavar="PNG", help="Export the current window to a PNG and exit")
    parser.add_argument("--open", metavar="DIR", help="Open an experiment directory on startup")
    parser.add_argument("--theme", choices=["system", "light", "dark"], help="Override the saved GUI theme mode")
    args = parser.parse_args(argv)

    settings = QtCore.QSettings("UPbRPy", "Studio")
    theme_mode = args.theme or str(settings.value("theme_mode", "light", type=str) or "light")
    app = build_application(argv, theme_mode=theme_mode)
    icon = build_app_icon()
    app.setWindowIcon(icon)
    window = MainWindow(theme_mode=theme_mode, app_icon=icon)

    if args.open:
        window.load_experiment_directory(
            args.open,
            background=not bool(args.export),
        )

    if args.export:
        export_window(window, args.export)
        return 0

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
