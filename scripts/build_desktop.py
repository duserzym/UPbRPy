"""Build, smoke-test, and optionally archive UPbRPy Studio.

Run this script with the Python interpreter from the environment whose
scientific and Qt dependencies should be bundled. PyInstaller creates an
on-directory application so Qt plugins and scientific shared libraries remain
inspectable and application startup does not require temporary extraction.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_FILE = PROJECT_ROOT / "packaging" / "UPbRPyStudio.spec"
DEFAULT_DIST_DIRECTORY = PROJECT_ROOT / "dist"
DEFAULT_WORK_DIRECTORY = PROJECT_ROOT / "build" / "pyinstaller"
APPLICATION_NAME = "UPbRPy Studio"


def project_version() -> str:
    """Return the package version used in archive and bundle metadata."""
    metadata = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return str(metadata["project"]["version"])


def platform_label() -> str:
    """Return a stable, readable platform label for distributable archives."""
    system = {
        "Darwin": "macOS",
        "Windows": "Windows",
        "Linux": "Linux",
    }.get(platform.system(), platform.system())
    machine = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "AMD64": "x86_64",
        "x86_64": "x86_64",
    }.get(platform.machine(), platform.machine())
    return f"{system}-{machine}"


def application_path(dist_directory: Path) -> Path:
    """Return the expected bundled application for the current platform."""
    if sys.platform == "darwin":
        return dist_directory / f"{APPLICATION_NAME}.app"
    if sys.platform == "win32":
        return (
            dist_directory
            / APPLICATION_NAME
            / f"{APPLICATION_NAME}.exe"
        )
    return dist_directory / APPLICATION_NAME / APPLICATION_NAME


def application_executable(application: Path) -> Path:
    """Return the executable nested inside an app, folder, or direct path."""
    if sys.platform == "darwin":
        return (
            application
            / "Contents"
            / "MacOS"
            / APPLICATION_NAME
        )
    return application


def require_build_dependencies() -> None:
    """Raise a focused error when the selected interpreter cannot build."""
    required = ("PyInstaller", "PySide6", "pyqtgraph", "xlsxwriter")
    missing = [
        name
        for name in required
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        extras = ", ".join(missing)
        raise RuntimeError(
            f"Missing desktop build dependencies: {extras}. "
            'Install them with `python -m pip install -e ".[gui,build]"` '
            "and rerun this script with the same Python interpreter."
        )


def run_pyinstaller(
    dist_directory: Path,
    work_directory: Path,
    *,
    clean: bool,
) -> Path:
    """Execute the checked-in PyInstaller specification."""
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(dist_directory),
        "--workpath",
        str(work_directory),
    ]
    if clean:
        command.append("--clean")
    command.append(str(SPEC_FILE))
    environment = os.environ.copy()
    matplotlib_cache = work_directory / "matplotlib"
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    environment["MPLCONFIGDIR"] = str(matplotlib_cache)
    # Keep PyInstaller's binary/signing cache inside the disposable work tree.
    # This avoids stale caches from another Python architecture and makes CI
    # and sandboxed local builds independent of the user's Library/AppData.
    pyinstaller_cache = work_directory / "config"
    pyinstaller_cache.mkdir(parents=True, exist_ok=True)
    environment["PYINSTALLER_CONFIG_DIR"] = str(pyinstaller_cache)
    subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
    )
    application = application_path(dist_directory)
    executable = application_executable(application)
    if not application.exists() or not executable.exists():
        raise RuntimeError(
            "PyInstaller completed without producing the expected "
            f"application: {application}"
        )
    return application


def smoke_test_application(
    application: Path,
    work_directory: Path,
) -> Path:
    """Launch the frozen GUI offscreen and verify it renders a real window."""
    screenshot = work_directory / "UPbRPy-Studio-smoke.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    if screenshot.exists():
        screenshot.unlink()
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["MPLCONFIGDIR"] = str(work_directory / "matplotlib")
    subprocess.run(
        [
            str(application_executable(application)),
            "--theme",
            "light",
            "--export",
            str(screenshot),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        timeout=180,
    )
    if not screenshot.exists() or screenshot.stat().st_size < 10_000:
        raise RuntimeError(
            "The frozen GUI exited without producing a valid smoke-test "
            f"screenshot: {screenshot}"
        )
    return screenshot


def archive_application(
    application: Path,
    dist_directory: Path,
) -> Path:
    """Create a transport-safe archive suitable for CI or manual testing."""
    archive_stem = (
        f"UPbRPy-Studio-{project_version()}-{platform_label()}"
    )
    archive = dist_directory / f"{archive_stem}.zip"
    if archive.exists():
        archive.unlink()
    if sys.platform == "darwin":
        subprocess.run(
            [
                "ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(application),
                str(archive),
            ],
            check=True,
        )
    else:
        bundle_directory = (
            application.parent
            if application.is_file()
            else application
        )
        generated = shutil.make_archive(
            str(archive.with_suffix("")),
            "zip",
            root_dir=bundle_directory.parent,
            base_dir=bundle_directory.name,
        )
        archive = Path(generated)
    return archive


def parse_arguments(
    argv: list[str] | None = None,
) -> argparse.Namespace:
    """Parse build locations and optional validation controls."""
    parser = argparse.ArgumentParser(
        description="Build the native UPbRPy Studio desktop application."
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=DEFAULT_DIST_DIRECTORY,
        help="Output directory for the application and archive.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK_DIRECTORY,
        help="Disposable PyInstaller analysis/build directory.",
    )
    parser.add_argument(
        "--clean",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Clear PyInstaller caches before analysis (default: true).",
    )
    parser.add_argument(
        "--smoke-test",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Render an offscreen screenshot from the frozen app.",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Create a platform-labelled ZIP after validation.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build the app and report paths intended for manual testing."""
    arguments = parse_arguments(argv)
    require_build_dependencies()
    dist_directory = arguments.dist_dir.expanduser().resolve()
    work_directory = arguments.work_dir.expanduser().resolve()
    dist_directory.mkdir(parents=True, exist_ok=True)
    work_directory.mkdir(parents=True, exist_ok=True)

    application = run_pyinstaller(
        dist_directory,
        work_directory,
        clean=arguments.clean,
    )
    print(f"Application: {application}")
    if arguments.smoke_test:
        screenshot = smoke_test_application(
            application,
            work_directory,
        )
        print(f"Smoke-test screenshot: {screenshot}")
    if arguments.archive:
        archive = archive_application(application, dist_directory)
        print(f"Archive: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
