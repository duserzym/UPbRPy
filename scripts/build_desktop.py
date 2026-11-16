"""Build, smoke-test, and optionally archive UPbRPy Studio.

Run this script with the Python interpreter from the environment whose
scientific and Qt dependencies should be bundled. PyInstaller creates an
on-directory application so Qt plugins and scientific shared libraries remain
inspectable and application startup does not require temporary extraction.
"""

from __future__ import annotations

import argparse
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_FILE = PROJECT_ROOT / "packaging" / "UPbRPyStudio.spec"
CACHE_BASE = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) if sys.platform == "win32" else (Path.home() / "Library/Caches" if sys.platform == "darwin" else Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))))
CHECKOUT_KEY = hashlib.sha256(str(PROJECT_ROOT).encode()).hexdigest()[:12]
BUILD_ROOT = Path(os.environ.get("UPBRPY_BUILD_ROOT", str(CACHE_BASE / "UPbRPy" / CHECKOUT_KEY)))
DEFAULT_DIST_DIRECTORY = BUILD_ROOT / "dist"
DEFAULT_WORK_DIRECTORY = BUILD_ROOT / "work"
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
    """Raise a focused error when the selected interpreter cannot build.

    requests and keyring (the [database] extra) are required too: the spec
    bundles database mode only when both are importable, and a build without
    them opens no upbr:// links and cannot submit to upbdb.
    """
    required = (
        "PyInstaller", "PySide6", "pyqtgraph", "xlsxwriter", "requests", "keyring",
    )
    missing = [
        name
        for name in required
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        extras = ", ".join(missing)
        raise RuntimeError(
            f"Missing desktop build dependencies: {extras}. "
            'Install them with `python -m pip install -e ".[gui,build,database]"` '
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


SMOKE_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "qtegra" / "Zircon_demo"
SMOKE_CSV = SMOKE_FIXTURE / "Zircon_demo_Rich.csv"


def _run_frozen(application: Path, arguments: list[str], work_directory: Path) -> subprocess.CompletedProcess:
    """Run the frozen app offscreen with isolated settings and autosaves."""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["MPLCONFIGDIR"] = str(work_directory / "matplotlib")
    environment["UPBRPY_AUTOSAVE_DIR"] = str(work_directory / "autosave")
    result = subprocess.run(
        [str(application_executable(application)), "--theme", "light", *arguments],
        cwd=work_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0 or "Traceback" in result.stderr:
        raise RuntimeError(
            f"The frozen app failed on {' '.join(arguments)} "
            f"(exit {result.returncode}):\n{result.stderr[-4000:]}"
        )
    return result


def _write_smoke_project(work_directory: Path) -> Path:
    """Save a project for the fixture with the build environment's sources."""
    sys.path.insert(0, str(PROJECT_ROOT))
    from GUI.services import ensure_shared_pipeline, load_session
    from GUI.workflows import infer_workflow_preset
    from UPbRPy.pipeline import PipelineStage
    from UPbRPy.project import write_reduction_project

    preset = infer_workflow_preset(str(SMOKE_CSV))
    session = load_session(str(SMOKE_CSV), preset=preset)
    pipeline = ensure_shared_pipeline(session, preset)
    return write_reduction_project(
        pipeline,
        work_directory / "smoke-project.upbr",
        resume_stage=PipelineStage.SELECTION,
    )


def smoke_test_application(
    application: Path,
    work_directory: Path,
) -> Path:
    """Exercise the frozen app on real data, not only a launch.

    1. Render the empty window.
    2. Open a Qtegra Rich CSV fixture and render it.
    3. Open a saved project for that fixture and render it.
    4. Run ``--verify-reduction`` on the fixture through concentrations, and
       through the export bundle on ``$UPBRPY_SMOKE_SESSION`` when it is set.
    Load errors make the frozen app exit non-zero (see ``--export`` in
    GUI/main.py), so a module missing from the bundle fails the build.
    """
    work_directory.mkdir(parents=True, exist_ok=True)
    screenshots = {
        "empty": (work_directory / "UPbRPy-Studio-smoke.png", []),
        "csv": (work_directory / "UPbRPy-Studio-smoke-csv.png", ["--open", str(SMOKE_CSV)]),
    }
    project = _write_smoke_project(work_directory)
    screenshots["project"] = (work_directory / "UPbRPy-Studio-smoke-project.png", ["--open", str(project)])
    for name, (screenshot, arguments) in screenshots.items():
        if screenshot.exists():
            screenshot.unlink()
        _run_frozen(application, [*arguments, "--export", str(screenshot)], work_directory)
        if not screenshot.exists() or screenshot.stat().st_size < 10_000:
            raise RuntimeError(
                f"The frozen GUI did not produce a valid {name} screenshot: {screenshot}"
            )
    # The repository fixture is a slice without 202Hg, so its reduction can
    # run through concentrations; a complete session given in
    # UPBRPY_SMOKE_SESSION (kept outside the repository) runs every stage
    # and writes the export bundle.
    checks = [(SMOKE_CSV, "concentration")]
    full_session = os.environ.get("UPBRPY_SMOKE_SESSION")
    if full_session:
        checks.append((Path(full_session).expanduser(), "export"))
    for index, (source, through) in enumerate(checks):
        verification = work_directory / f"verify-reduction-{index}"
        if verification.exists():
            shutil.rmtree(verification)
        _run_frozen(
            application,
            ["--verify-reduction", str(source), "--verification-output", str(verification),
             "--verify-through", through],
            work_directory,
        )
        report = json.loads((verification / "verification.json").read_text())
        if not report.get("frozen") or report.get("spots", 0) <= 0:
            raise RuntimeError(f"The frozen reduction check returned an incomplete report: {report}")
        if through != "export" and through not in report.get("stages", []):
            raise RuntimeError(f"The frozen reduction check did not reach {through}: {report}")
        print(f"Frozen reduction check ({source.name}, through {through}): {report['spots']} spots")
    return screenshots["empty"][0]


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


def sign_application(application: Path) -> None:
    """Sign the macOS app with ``APPLE_SIGNING_IDENTITY`` when it is set.

    A stable identity (the lab's certificate) keeps macOS keychain permissions
    across builds and updates; without it PyInstaller's ad-hoc signature stays.
    """
    identity = os.environ.get("APPLE_SIGNING_IDENTITY", "").strip()
    if sys.platform != "darwin" or not identity or identity == "-":
        return
    subprocess.run(
        ["codesign", "--force", "--deep", "--timestamp=none", "--sign", identity, str(application)],
        check=True,
    )
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(application)], check=True)
    print(f"Signed with: {identity}")


def main(argv: list[str] | None = None) -> int:
    """Build the app and report paths intended for manual testing."""
    arguments = parse_arguments(argv)
    require_build_dependencies()
    dist_directory = arguments.dist_dir.expanduser().resolve()
    work_directory = arguments.work_dir.expanduser().resolve()
    for output in (dist_directory, work_directory):
        if output == PROJECT_ROOT or PROJECT_ROOT in output.parents:
            raise ValueError("Build outputs must be outside the source repository")
    dist_directory.mkdir(parents=True, exist_ok=True)
    work_directory.mkdir(parents=True, exist_ok=True)

    application = run_pyinstaller(
        dist_directory,
        work_directory,
        clean=arguments.clean,
    )
    sign_application(application)
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
