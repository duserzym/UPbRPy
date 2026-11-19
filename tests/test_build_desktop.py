"""Build-checkout version metadata and mandatory frozen version validation."""

from importlib import metadata, util
from pathlib import Path
import runpy
import shutil
import sys
from types import ModuleType, SimpleNamespace

import pytest

from GUI import updates
from scripts import build_desktop


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("stale_format", ["egg-info", "dist-info"])
def test_spec_stamps_checkout_version_despite_stale_metadata(tmp_path, monkeypatch, stale_format):
    stale = tmp_path / "stale"
    info = stale / f"UPbRPy.{stale_format}"
    info.mkdir(parents=True)
    (info / ("PKG-INFO" if stale_format == "egg-info" else "METADATA")).write_text(
        "Metadata-Version: 2.1\nName: UPbRPy\nVersion: 0.0.1\n\n"
    )
    monkeypatch.syspath_prepend(str(stale))
    assert metadata.version("UPbRPy") == "0.0.1"

    # Execute the real spec with PyInstaller stubbed: no dependency or build.
    # SPECPATH selects the checkout, not installed code.
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_submodules = lambda name: []
    hooks.collect_data_files = lambda name: []

    def copy_metadata(name):
        assert name != "UPbRPy", "Must not look up the venv's UPbRPy metadata"
        return []

    hooks.copy_metadata = copy_metadata
    for name in ("PyInstaller", "PyInstaller.utils"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setitem(sys.modules, hooks.__name__, hooks)
    captured = SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[
        # Analysis can automatically copy metadata for version() calls.
        ("UPbRPy.egg-info/PKG-INFO", "stale-egg-info", "DATA"),
        ("upbrpy-0.0.1.dist-info/METADATA", "stale-dist-info", "DATA"),
        ("other.dist-info/METADATA", "other-metadata", "DATA"),
    ])

    def analysis(*args, **kwargs):
        return captured

    with monkeypatch.context() as spec_patches:
        spec_patches.setattr(util, "find_spec", lambda name: None)
        runpy.run_path(str(ROOT / "packaging/UPbRPyStudio.spec"), init_globals={
            "SPECPATH": str(ROOT / "packaging"), "workpath": str(tmp_path / "work"),
            "Analysis": analysis,
            **dict.fromkeys(("PYZ", "EXE", "COLLECT", "BUNDLE"), lambda *a, **kw: None),
        })

    assert ("other.dist-info/METADATA", "other-metadata", "DATA") in captured.datas
    assert len(captured.datas) == 2
    destination_file, source, kind = captured.datas[-1]
    assert (destination_file, kind) == ("UPbRPy.egg-info/PKG-INFO", "DATA")
    destination = str(Path(destination_file).parent)
    bundle = tmp_path / "bundle"
    (bundle / destination).mkdir(parents=True)
    shutil.copy2(source, bundle / destination / "PKG-INFO")
    expected = build_desktop.project_version()
    assert metadata.PathDistribution(bundle / destination).version == expected

    monkeypatch.setattr(updates, "__file__", str(bundle / "GUI/updates.py"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert updates.installed_version() == expected
    # The stamp wins even though normal metadata lookup still finds 0.0.1.
    assert metadata.version("UPbRPy") == "0.0.1"
    monkeypatch.setattr(sys, "path", [str(bundle)])
    assert metadata.version("UPbRPy") == expected


def test_frozen_missing_stamp_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "__file__", str(tmp_path / "GUI/updates.py"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(metadata, "version", lambda name: "0.0.1")
    with pytest.raises(RuntimeError, match="Missing frozen UPbRPy version"):
        updates.installed_version()


def test_version_output_uses_updater_version_without_starting_gui(tmp_path, monkeypatch):
    from GUI import main

    monkeypatch.setattr(updates, "installed_version", lambda: "1.2.3")

    def unexpected(*args, **kwargs):
        pytest.fail("Version output must exit before starting the GUI")

    monkeypatch.setattr(main, "build_application", unexpected)
    monkeypatch.setattr(main, "StudioWindowController", unexpected)
    output = tmp_path / "version.txt"
    assert main.main(["--theme", "light", "--version-output", str(output)]) == 0
    assert output.read_text() == "1.2.3\n"


@pytest.mark.parametrize("reported", ["current", "0.0.1", "", None])
def test_verify_application_version(tmp_path, monkeypatch, reported):
    application = tmp_path / "fake.app"
    output = tmp_path / "frozen-version.txt"
    expected = build_desktop.project_version()
    output.write_text(expected)  # A previous successful run must not mask failure.

    def run_frozen(app, arguments, work_directory):
        assert (app, arguments, work_directory) == (
            application, ["--version-output", str(output)], tmp_path,
        )
        assert not output.exists()
        if reported is not None:
            output.write_text((expected if reported == "current" else reported) + "\n")

    monkeypatch.setattr(build_desktop, "_run_frozen", run_frozen)
    if reported == "current":
        build_desktop.verify_application_version(application, tmp_path)
    else:
        with pytest.raises(RuntimeError, match="Frozen app version mismatch"):
            build_desktop.verify_application_version(application, tmp_path)


@pytest.mark.parametrize("reported", ["current", "0.0.1"])
def test_build_checks_version_even_without_smoke_test(tmp_path, monkeypatch, reported):
    monkeypatch.setattr(build_desktop, "require_build_dependencies", lambda: None)
    monkeypatch.setattr(build_desktop, "run_pyinstaller", lambda *a, **kw: tmp_path / "fake.app")
    monkeypatch.setattr(build_desktop, "sign_application", lambda app: None)
    archived = []
    monkeypatch.setattr(build_desktop, "archive_application", lambda *args: archived.append(args))

    def run_frozen(app, arguments, work_directory):
        Path(arguments[1]).write_text(
            build_desktop.project_version() if reported == "current" else reported
        )

    monkeypatch.setattr(build_desktop, "_run_frozen", run_frozen)
    arguments = ["--dist-dir", str(tmp_path / "dist"), "--work-dir", str(tmp_path / "work"),
                 "--no-smoke-test", "--archive"]
    if reported == "current":
        assert build_desktop.main(arguments) == 0
        assert len(archived) == 1
    else:
        with pytest.raises(RuntimeError, match="Frozen app version mismatch"):
            build_desktop.main(arguments)
        assert not archived
