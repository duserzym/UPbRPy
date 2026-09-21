from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtWidgets

from GUI.icon import build_app_icon
from GUI.main_window import MainWindow
from GUI.theme import build_application


class StudioWindowController(QtCore.QObject):
    """Own independent reduction windows for one application process.

    Scientific data, selections, and pipeline caches live on each
    :class:`MainWindow`. Shared Python and Qt libraries remain in one process,
    making multiple sessions much lighter than launching several complete app
    processes.
    """

    def __init__(
        self,
        *,
        theme_mode: str,
        app_icon,
    ) -> None:
        super().__init__()
        self.theme_mode = theme_mode
        self.app_icon = app_icon
        self.windows: dict[int, MainWindow] = {}

    def create_window(
        self,
        *,
        show: bool = True,
    ) -> MainWindow:
        """Create, register, and optionally show an empty reduction window."""
        window = MainWindow(
            theme_mode=self.theme_mode,
            app_icon=self.app_icon,
        )
        identity = id(window)
        self.windows[identity] = window
        window.new_window_requested.connect(self.create_window)
        window.next_window_requested.connect(
            lambda source=window: self.activate_next_window(source)
        )
        window.open_project_in_new_window_requested.connect(
            self.open_project_window
        )
        window.open_experiment_in_new_window_requested.connect(
            self.open_experiment_window
        )
        window.destroyed.connect(
            lambda _object=None, key=identity: self.release_window(key)
        )
        if show:
            window.show()
            window.raise_()
            window.activateWindow()
        return window

    def open_project_window(self, project_file: str) -> MainWindow:
        """Open and recalculate a lightweight project in a new window."""
        window = self.create_window()
        window.load_project_file(project_file)
        return window

    def open_experiment_window(self, directory: str) -> MainWindow:
        """Import one experiment directory in a new window."""
        window = self.create_window()
        window.load_experiment_directory(directory)
        return window

    def activate_next_window(self, source: MainWindow) -> None:
        """Cycle keyboard focus through the currently open reductions."""
        ordered = [
            window
            for window in self.windows.values()
            if window.isVisible()
        ]
        if len(ordered) < 2:
            return
        try:
            index = ordered.index(source)
        except ValueError:
            index = -1
        target = ordered[(index + 1) % len(ordered)]
        target.showNormal()
        target.raise_()
        target.activateWindow()

    def release_window(self, identity: int) -> None:
        """Forget a destroyed window so its reduction data can be reclaimed."""
        self.windows.pop(identity, None)


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
    parser.add_argument("--verify-reduction", metavar="DIR", help="Run a full reduction/export smoke check on an experiment and exit")
    parser.add_argument("--verification-output", metavar="DIR", help="Output directory required by --verify-reduction")
    args = parser.parse_args(argv)
    if args.verify_reduction and not args.verification_output:
        parser.error("--verify-reduction requires --verification-output")

    settings = QtCore.QSettings("UPbRPy", "Studio")
    theme_mode = args.theme or str(settings.value("theme_mode", "light", type=str) or "light")
    app = build_application(argv, theme_mode=theme_mode)
    icon = build_app_icon()
    app.setWindowIcon(icon)
    controller = StudioWindowController(
        theme_mode=theme_mode,
        app_icon=icon,
    )
    window = controller.create_window(show=False)

    if args.verify_reduction:
        from GUI.services import load_session, ensure_shared_pipeline
        from GUI.workflows import infer_workflow_preset
        from UPbRPy.reproducibility import export_reduction_bundle
        preset = infer_workflow_preset(args.verify_reduction)
        session = load_session(args.verify_reduction, preset=preset)
        pipeline = ensure_shared_pipeline(session, preset)
        bundle = export_reduction_bundle(pipeline, args.verification_output, hash_inputs=True)
        if bundle.warnings:
            raise RuntimeError("; ".join(bundle.warnings))
        report = {
            "frozen": bool(getattr(sys, "frozen", False)),
            "workflow": preset.key,
            "spots": len(pipeline.run_spot_means()),
            "sweeps": len(pipeline.run_depth_profile()),
            "files": [str(path) for path in bundle.files],
        }
        Path(args.verification_output, "verification.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        return 0

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
