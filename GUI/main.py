from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
from pathlib import Path


if __name__ == "__main__":
    multiprocessing.freeze_support()


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
from GUI.wheel_guard import install_wheel_guard
from GUI.updater import Updater


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
        self._checking_quit = False
        self._database = None
        self.updater = Updater(self)
        self.updater.start_scheduler()

    def _create_reduction(self) -> MainWindow:
        reduction = MainWindow(theme_mode=self.theme_mode, app_icon=self.app_icon, updater=self.updater)
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

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if (
            event.type() != QtCore.QEvent.Type.Quit
            or watched is not QtWidgets.QApplication.instance()
        ):
            return False
        if self._checking_quit:
            event.ignore()
            return True
        self._checking_quit = True
        try:
            # A Cancel in any window must leave every window and tab open.
            windows = tuple(self.windows.values())
            for window in windows:
                if not window.confirm_close():
                    event.ignore()
                    return True
            for window in windows:
                window.close_confirmed()
        finally:
            self._checking_quit = False
        return False

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
                activate_project=self.activate_open_project,
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
        existing = self.activate_open_project(project_file=project_file)
        if existing is not None:
            return existing
        window = self.window_for_new_content()
        window.load_project_file(project_file)
        return window

    def activate_open_project(
        self,
        *,
        server: str = "",
        session_id: str = "",
        run_id: str = "",
        project_file: str = "",
        fresh: bool = False,
    ) -> MainWindow | None:
        """Select and raise an open project by path or database origin."""
        path = Path(project_file).resolve() if project_file else None
        for shell in self.windows.values():
            for index in range(shell.tabs.count()):
                reduction = shell.tabs.widget(index)
                origin = reduction.session.origin or {}
                if fresh and (
                    reduction.project_path is None
                    or Path(reduction.project_path).name != f"{origin.get('session_key', '')}.upbr"
                ):
                    continue
                same_path = (path is not None and reduction.project_path is not None
                             and Path(reduction.project_path).resolve() == path)
                same_server = (not server
                               or str(origin.get("server", "")).rstrip("/") == server.rstrip("/"))
                same_origin = same_server and (
                    (session_id and origin.get("session_id") == session_id)
                    or (run_id and origin.get("run_id") == run_id)
                )
                if same_path or same_origin:
                    shell.select_tab(index)
                    if shell.isMinimized():
                        shell.showNormal()
                    self._show_window(shell)
                    self._remember_active(shell)
                    return reduction
        return None

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
        return window

    def open_target(self, value: str | LaunchTarget) -> None:
        """Open an ``--open`` value: folder, .upbr, bundle zip or upbr:// link."""
        try:
            target = value if isinstance(value, LaunchTarget) else parse_open_target(value)
        except LaunchError as exc:
            shell = self.active_window() or self.create_window()
            from UPbRPy.notices import ReductionError
            reduction = shell.active_reduction or shell.add_tab()
            reduction._handle_action_error("Cannot open", ReductionError(
                str(exc), "import", "Open an existing Qtegra export, .upbr project, or session bundle.",
            ))
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
            self.database.open_bundle,
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
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="UPbRPy PySide6 + pyqtgraph GUI")
    parser.add_argument("--export", metavar="PNG", help="Export the current window to a PNG and exit")
    parser.add_argument("--version-output", metavar="FILE", help="Write Studio's version to a file and exit")
    parser.add_argument(
        "--check-tls", nargs="?", const="", metavar="URL",
        help="Report trusted CA certificates, optionally check an HTTPS URL, and exit",
    )
    parser.add_argument(
        "--check-tls-output", metavar="PATH",
        help="Also write the --check-tls JSON report to a UTF-8 file",
    )
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
        "--verify-through",
        metavar="STAGE",
        default="export",
        help=(
            "Last pipeline stage for --verify-reduction (for example concentration); "
            "the default, export, runs every stage and writes the export bundle"
        ),
    )
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
    if args.version_output:
        from GUI.updates import installed_version

        # Windowed Windows executables have no stdout; use a file so the build
        # can verify the updater's actual version on every platform.
        Path(args.version_output).write_text(installed_version() + "\n", encoding="utf-8")
        return 0
    if args.check_tls is not None:
        from UPbRPy.integration import tls

        report = {
            "ca_certificates": 0, "ca_bundle": tls.ca_bundle(),
            "url": args.check_tls or None, "ok": None, "error": None, "detail": None,
        }
        try:
            report["ca_certificates"] = tls.ca_certificate_count()
            if args.check_tls:
                with tls.urlopen(args.check_tls, timeout=10):
                    report["ok"] = True
        except Exception as exc:
            if args.check_tls:
                report["ok"] = False
            report["error"] = "certificate" if tls.is_certificate_error(exc) else "network"
            report["detail"] = str(exc)
        report_json = json.dumps(report) + "\n"
        if args.check_tls_output:
            Path(args.check_tls_output).write_text(report_json, encoding="utf-8")
        if sys.stdout is not None:
            print(report_json, end="")
        return int(report["ca_certificates"] < 100 or report["error"] == "certificate")

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
    from GUI.gc_policy import install as install_gc_policy
    install_gc_policy(app)
    install_wheel_guard(app)

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
        from UPbRPy.pipeline import PipelineStage
        preset = infer_workflow_preset(args.verify_reduction)
        session = load_session(args.verify_reduction, preset=preset)
        pipeline = ensure_shared_pipeline(session, preset)
        through = PipelineStage(args.verify_through)
        if through != PipelineStage.EXPORT:
            pipeline.run_to(through)
            report = {
                "frozen": bool(getattr(sys, "frozen", False)),
                "workflow": preset.key,
                "through": through.value,
                "spots": int(session.experiment["spot_names"].nunique()),
                "stages": [stage.value for stage in pipeline.cache.stages()],
            }
            Path(args.verification_output).mkdir(parents=True, exist_ok=True)
            Path(args.verification_output, "verification.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))
            return 0
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
        # Scripted export: report load errors on stderr and fail instead of
        # opening a modal dialog that nobody can dismiss offscreen.
        errors: list[str] = []

        def record_error(_parent, title, text, *_args, **_kwargs):
            errors.append(f"{title}: {text}")
            print(f"{title}: {text}", file=sys.stderr)
            return QtWidgets.QMessageBox.StandardButton.Ok

        QtWidgets.QMessageBox.critical = staticmethod(record_error)
        QtWidgets.QMessageBox.warning = staticmethod(record_error)
        reduction = window.active_reduction
        if target is not None and target.kind == "directory":
            reduction.load_experiment_directory(args.open, background=False, offer_restore=False)
        elif target is not None and target.kind == "project":
            reduction.load_project_file(target.path, background=False)
        export_window(window, args.export)
        if errors or (target is not None and not reduction.session.loaded):
            if not errors:
                print(f"Could not open {args.open}", file=sys.stderr)
            return 1
        return 0

    if instance is not None:
        instance.received.connect(controller.open_target)
        instance.listen()
        app.installEventFilter(controller)
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
