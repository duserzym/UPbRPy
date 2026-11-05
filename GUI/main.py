from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtWidgets

from GUI.icon import build_app_icon
from GUI.launch import (
    FileOpenFilter,
    LaunchError,
    LaunchTarget,
    SingleInstance,
    parse_open_target,
    register_url_scheme,
    unregister_url_scheme,
)
from GUI.main_window import MainWindow
from GUI.studio_window import StudioWindow
from GUI.theme import build_application


class StudioWindowController(QtCore.QObject):
    """Own Studio windows and route new content into their document tabs."""

    def __init__(
        self,
        *,
        theme_mode: str,
        app_icon,
    ) -> None:
        super().__init__()
        self.theme_mode = theme_mode
        self.app_icon = app_icon
        self.windows: dict[int, StudioWindow] = {}
        self._last_active: StudioWindow | None = None
        self._database = None

    def _create_reduction(self) -> MainWindow:
        reduction = MainWindow(theme_mode=self.theme_mode, app_icon=self.app_icon)
        reduction.database_links = self.database
        return reduction

    def create_window(self, *, show: bool = True, create_empty: bool = True) -> StudioWindow:
        """Create, register, and optionally show a Studio window."""
        window = StudioWindow(
            theme_mode=self.theme_mode,
            app_icon=self.app_icon,
            reduction_factory=self._create_reduction,
            create_empty=create_empty,
        )
        identity = id(window)
        self.windows[identity] = window
        self._last_active = window
        window.new_window_requested.connect(self.create_window)
        window.move_tab_requested.connect(lambda reduction: self.move_tab_to_new_window(window, reduction))
        window.activated.connect(lambda: self._remember_active(window))
        window.destroyed.connect(lambda _object=None: self.release_window(identity))
        if show:
            self._show_window(window)
        return window

    def _remember_active(self, window: StudioWindow) -> None:
        self._last_active = window

    @staticmethod
    def _show_window(window: StudioWindow) -> None:
        window.show()
        window.raise_()
        window.activateWindow()

    def move_tab_to_new_window(self, source: StudioWindow, reduction: MainWindow) -> StudioWindow:
        """Move the existing reduction and its running work into another shell."""
        if source.tabs.count() < 2:
            return source
        moved = source.take_tab(reduction)
        target = self.create_window(show=False, create_empty=False)
        target.add_tab(moved)
        self._show_window(target)
        return target

    def open_project_window(self, project_file: str) -> MainWindow:
        """Compatibility entry point: open a project in a document tab."""
        return self.open_project_in_window(project_file)

    def open_experiment_window(self, directory: str) -> MainWindow:
        """Compatibility entry point: open raw data in a document tab."""
        return self.open_directory_in_window(directory)

    # ------------------------------------------------------------ --open
    @property
    def database(self):
        """Database-mode flows (links, sign-in), created on first use."""
        if self._database is None:
            from GUI.database import DatabaseLinks

            self._database = DatabaseLinks(
                open_bundle=self.open_session_bundle,
                open_project=self.open_project_in_window,
                parent_widget=self.active_window,
            )
        return self._database

    def active_window(self) -> StudioWindow | None:
        """The most recently active Studio shell; dialogs attach here."""
        active = QtWidgets.QApplication.activeWindow()
        if isinstance(active, StudioWindow) and id(active) in self.windows:
            self._last_active = active
        if self._last_active is not None and id(self._last_active) in self.windows:
            return self._last_active
        return next(reversed(self.windows.values()), None)

    def window_for_new_content(self) -> MainWindow:
        """Get an empty tab in the most recently active Studio window."""
        shell = self.active_window() or self.create_window(show=False)
        reduction = shell.reduction_for_new_content()
        self._show_window(shell)
        return reduction

    def open_project_in_window(self, project_file: str) -> MainWindow:
        window = self.window_for_new_content()
        window.load_project_file(project_file)
        return window

    def open_directory_in_window(self, directory: str) -> MainWindow:
        window = self.window_for_new_content()
        window.load_experiment_directory(directory)
        return window

    def open_session_bundle(self, bundle) -> MainWindow:
        """Open an unpacked session bundle: its project, else its raw/ folder."""
        if bundle.project_file is not None:
            window = self.open_project_in_window(str(bundle.project_file))
        else:
            window = self.open_directory_in_window(str(bundle.raw_directory))
        note = "cached copy is current" if bundle.reused else "fetched"
        window.append_log(
            f"Session bundle {bundle.session_key} ({note}): {bundle.path}"
        )
        if bundle.superseded is not None:
            window.append_log(
                f"The previous cached copy was kept as {bundle.superseded}."
            )
        return window

    def open_target(self, value: str | LaunchTarget) -> None:
        """Open an ``--open`` value: folder, .upbr, bundle zip or upbr:// link."""
        try:
            target = value if isinstance(value, LaunchTarget) else parse_open_target(value)
        except LaunchError as exc:
            QtWidgets.QMessageBox.warning(self.active_window(), "Cannot open", str(exc))
            return
        if target.is_url:
            self.database.handle(target)
            return
        if target.kind == "project":
            self.open_project_in_window(str(target.path))
        elif target.kind == "bundle_zip":
            self._open_bundle_zip(target.path)
        elif target.kind == "directory":
            from UPbRPy.session_bundle import find_bundle_project
            from UPbRPy.session_manifest import SESSION_MANIFEST_FILENAME

            folder = target.path
            if (folder / SESSION_MANIFEST_FILENAME).is_file() and (folder / "raw").is_dir():
                project = find_bundle_project(folder, folder.name)
                if project is not None:
                    self.open_project_in_window(str(project))
                else:
                    self.open_directory_in_window(str(folder / "raw"))
            else:
                self.open_directory_in_window(str(folder))

    def _open_bundle_zip(self, path: Path) -> None:
        from UPbRPy.session_bundle import unpack_session_bundle

        self.database.run(
            lambda: unpack_session_bundle(path, self.database.cache_root),
            self.open_session_bundle,
            title="Open session bundle",
        )

    def activate_next_window(self, source: StudioWindow | MainWindow) -> None:
        """Compatibility entry point: cycle tabs in the containing shell."""
        shell = source if isinstance(source, StudioWindow) else source.window()
        if isinstance(shell, StudioWindow):
            shell.next_tab()

    def release_window(self, identity: int) -> None:
        """Forget a destroyed window so its reduction data can be reclaimed."""
        window = self.windows.pop(identity, None)
        if self._last_active is window:
            self._last_active = None


def export_window(window: QtWidgets.QMainWindow, output_path: str) -> None:
    window.show()
    for _ in range(4):
        QtWidgets.QApplication.processEvents()
    window.grab().save(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UPbRPy PySide6 + pyqtgraph GUI")
    parser.add_argument("--export", metavar="PNG", help="Export the current window to a PNG and exit")
    parser.add_argument(
        "--open",
        metavar="TARGET",
        help=(
            "Open on startup: an experiment directory, a Qtegra CSV, a .upbr project, a "
            "session-bundle .zip, or a upbr://session/<id>?server=…&code=…, "
            "upbr://run/<id>?server=… or upbr://auth/callback?code=… link"
        ),
    )
    parser.add_argument("--theme", choices=["system", "light", "dark"], help="Override the saved GUI theme mode")
    parser.add_argument("--verify-reduction", metavar="DIR", help="Run a full reduction/export smoke check on an experiment and exit")
    parser.add_argument("--verification-output", metavar="DIR", help="Output directory required by --verify-reduction")
    parser.add_argument(
        "--register-url-scheme",
        action="store_true",
        help="Windows: register upbr:// links for this user (HKCU\\Software\\Classes\\upbr) and exit",
    )
    parser.add_argument(
        "--unregister-url-scheme",
        action="store_true",
        help="Windows: remove the upbr:// registration and exit",
    )
    args = parser.parse_args(argv)
    if args.verify_reduction and not args.verification_output:
        parser.error("--verify-reduction requires --verification-output")

    if args.register_url_scheme or args.unregister_url_scheme:
        try:
            if args.unregister_url_scheme:
                unregister_url_scheme()
                print("Removed the upbr:// registration.")
            else:
                print(f"Registered upbr:// links: {register_url_scheme()}")
        except LaunchError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    target = None
    if args.open:
        try:
            target = parse_open_target(args.open)
        except LaunchError as exc:
            parser.error(str(exc))

    settings = QtCore.QSettings("UPbRPy", "Studio")
    theme_mode = args.theme or str(settings.value("theme_mode", "light", type=str) or "light")
    app = build_application(argv, theme_mode=theme_mode)

    interactive = not (args.export or args.verify_reduction)
    instance = SingleInstance() if interactive else None
    if instance is not None and target is not None:
        # A file or link while Studio runs: hand it over and leave.
        if instance.send(args.open):
            return 0

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

    if args.export:
        if target is not None and target.kind == "directory":
            window.active_reduction.load_experiment_directory(args.open, background=False)
        elif target is not None and target.kind == "project":
            window.active_reduction.load_project_file(target.path, background=False)
        export_window(window, args.export)
        return 0

    if instance is not None:
        instance.received.connect(controller.open_target)
        instance.listen()
    # macOS delivers upbr:// links and Finder opens as QFileOpenEvent.
    file_open_filter = FileOpenFilter(controller.open_target, app)
    app.installEventFilter(file_open_filter)

    window.show()
    if target is not None:
        controller.open_target(target)
    try:
        return app.exec()
    finally:
        if instance is not None:
            instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
