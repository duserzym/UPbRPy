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
    monkeypatch.setattr(build_desktop, "archive_application", lambda *args, **kw: archived.append((args, kw)))

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


@pytest.mark.parametrize("platform,sandbox,check_tls", [
    ("darwin", "/usr/bin/sandbox-exec", True),
    ("darwin", "/usr/bin/sandbox-exec", "update"),
    ("darwin", None, True),
    ("linux", None, True),
    ("win32", None, True),
    ("darwin", "/usr/bin/sandbox-exec", False),
])
def test_frozen_tls_hides_homebrew_only_when_supported(tmp_path, monkeypatch, platform, sandbox, check_tls):
    import subprocess

    monkeypatch.setattr(build_desktop.sys, "platform", platform)
    monkeypatch.setattr(build_desktop.shutil, "which", lambda name: sandbox)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(build_desktop.subprocess, "run", run)
    arguments = (["--update-self-test", "file:///feed.json"] if check_tls == "update" else
                 ["--check-tls", "https://github.com/"] if check_tls else ["--version-output", "version.txt"])
    overrides = {"SSL_CERT_FILE": str(tmp_path / "missing.pem"),
                 "SSL_CERT_DIR": str(tmp_path / "missing-certs")}
    build_desktop._run_frozen(tmp_path / "app", arguments, tmp_path, environment_overrides=overrides)
    command, options = calls[0]
    if platform == "darwin" and sandbox and check_tls:
        profile = tmp_path / "no-homebrew-ca.sb"
        assert command[:3] == [sandbox, "-f", str(profile)]
        assert profile.read_text() == (
            '(version 1)\n(allow default)\n'
            '(deny file-read* (subpath "/opt/homebrew/etc/openssl@3") '
            '(subpath "/usr/local/etc/openssl@3"))\n')
    else:
        assert "-f" not in command
        assert not (tmp_path / "no-homebrew-ca.sb").exists()
    assert command[-len(arguments):] == arguments
    assert all(options["env"][name] == value for name, value in overrides.items())


def test_build_requires_both_trust_dependencies(monkeypatch):
    monkeypatch.setattr(build_desktop.importlib.util, "find_spec",
                        lambda name: None if name in {"truststore", "certifi"} else object())
    with pytest.raises(RuntimeError, match="certifi, truststore"):
        build_desktop.require_build_dependencies()


@pytest.mark.parametrize("signed", [False, True])
def test_archive_keeps_legacy_zip_and_adds_tar_checksums(tmp_path, monkeypatch, signed):
    import hashlib
    import json
    import tarfile
    import zipfile
    from GUI import update_config
    from GUI.labupdate import release
    from labupdate_signing import public_key, sign

    monkeypatch.setattr(build_desktop.sys, "platform", "darwin")
    monkeypatch.setattr(build_desktop, "platform_label", lambda: "macOS-arm64")
    monkeypatch.setattr(build_desktop, "project_version", lambda: "0.1.5")
    monkeypatch.delenv("UPBRPY_UPDATER_KEY", raising=False)
    monkeypatch.setattr(update_config, "PUBLIC_KEY", public_key())
    app = tmp_path / "UPbRPy Studio.app"
    contents = app / "Contents"
    contents.mkdir(parents=True)
    (contents / "payload").write_bytes(b"signed application")
    (contents / "link").symlink_to("payload")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[0] == "ditto":
            assert command[1:5] == ["-c", "-k", "--sequesterRsrc", "--keepParent"]
            with zipfile.ZipFile(command[-1], "w") as archive:
                archive.write(contents / "payload", arcname="UPbRPy Studio.app/Contents/payload")
        elif command[:4] == ["cargo", "tauri", "signer", "sign"]:
            path = Path(command[-1])
            Path(str(path) + ".sig").write_text(sign(path.read_bytes()))
        else:
            pytest.fail(f"Unexpected command: {command}")

    monkeypatch.setattr(build_desktop.subprocess, "run", run)
    result = build_desktop.archive_application(app, tmp_path, sign_with=Path("test.key") if signed else None)
    assert result.name == "UPbRPy-Studio-0.1.5-macOS-arm64.zip"
    tarball = tmp_path / "UPbRPy-Studio-0.1.5-macOS-arm64.app.tar.gz"
    with tarfile.open(tarball) as bundle:
        link = bundle.getmember("UPbRPy Studio.app/Contents/link")
        assert link.issym() and link.linkname == "payload"
    expected = {result.name, tarball.name}
    if signed:
        expected |= {tarball.name + ".sig", "latest.json"}
        feed = json.loads((tmp_path / "latest.json").read_text())
        assert feed["notes"] == (ROOT / "release-notes/0.1.5.md").read_text()
        assert feed["platforms"]["darwin-aarch64"]["url"].endswith(tarball.name)
        assert sum(command[0] == "cargo" for command in commands) == 1
    lines = (tmp_path / "SHA256SUMS.txt").read_text().splitlines()
    assert {line.split("  ", 1)[1] for line in lines} == expected
    for line in lines:
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
    assert not (tmp_path / "updater.pub").exists()


@pytest.mark.parametrize("failure", [None, "signature", "manifest", "tls", "public_key", "verification_ran", "crash", "missing"])
def test_frozen_update_self_test_mandatory_checks(tmp_path, monkeypatch, failure):
    import json
    from GUI.labupdate import minisign, selftest
    from GUI.labupdate.config import platform_key

    def frozen(app, arguments, work_directory, **kwargs):
        assert arguments[0] == "--update-self-test"
        manifest = json.loads((tmp_path / "update-self-test.json").read_text())
        selected = manifest["platforms"][platform_key()]
        from urllib.parse import urlsplit, unquote
        archive = Path(unquote(urlsplit(selected["url"]).path))
        minisign.verify_file(archive, selected["signature"], selftest.SELF_TEST_PUBKEY)
        assert kwargs["environment_overrides"] == {
            "SSL_CERT_FILE": str(tmp_path / "nonexistent-cert.pem"),
            "SSL_CERT_DIR": str(tmp_path / "nonexistent-certs"),
        }
        report = {"ok": True, "verification_ran": True, "tls": {"source": "truststore"},
                  "https": {"status": "network", "message": "offline"},
                  "signature": {"status": "ok", "tampered_rejected": True},
                  "manifest": {"status": "ok", "archive_verified": True},
                  "public_key": {"status": "ok"}}
        if failure in {"tls", "manifest", "signature", "public_key"}:
            report[failure]["status"] = "error"
        if failure == "tls":
            report["tls"] = {}
        if failure == "verification_ran":
            report["verification_ran"] = False
        if failure != "missing":
            Path(arguments[-1]).write_text(json.dumps(report))
        return SimpleNamespace(returncode=int(failure == "crash"), stderr="")

    monkeypatch.setattr(build_desktop, "_run_frozen", frozen)
    if failure:
        with pytest.raises(RuntimeError, match="self-test"):
            build_desktop.verify_update_self_test(tmp_path / "fake.app", tmp_path)
    else:
        build_desktop.verify_update_self_test(tmp_path / "fake.app", tmp_path)


def test_self_test_cli_exits_before_gui(tmp_path, monkeypatch):
    from GUI import main
    from GUI.labupdate import selftest

    calls = []
    monkeypatch.setattr(selftest, "run_self_test", lambda cfg, manifest_url: calls.append((cfg, manifest_url)) or {"ok": True})
    monkeypatch.setattr(main, "build_application", lambda *a, **kw: pytest.fail("Started GUI"))
    output = tmp_path / "report.json"
    assert main.main(["--update-self-test", "file:///feed.json", "--update-self-test-output", str(output)]) == 0
    assert calls[0][1] == "file:///feed.json"
    assert output.read_text().strip() == '{\n  "ok": true\n}'


def test_package_data_and_frozen_lookup_include_root_notes(tmp_path, monkeypatch):
    import tomllib
    from setuptools import Distribution
    from setuptools.command.build_py import build_py
    from GUI import update_config

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dist = Distribution({"packages": ["GUI"], "package_dir": {"": str(ROOT)},
                         "package_data": {"GUI": metadata["tool"]["setuptools"]["package-data"]["GUI"]}})
    dist.script_name = str(ROOT / "setup.py")
    command = build_py(dist)
    command.build_lib = str(tmp_path / "bundle")
    command.ensure_finalized()
    command.run()
    bundled = tmp_path / "bundle/release-notes/0.1.5.md"
    assert bundled.read_bytes() == (ROOT / "release-notes/0.1.5.md").read_bytes()
    monkeypatch.setattr(update_config, "__file__", str(tmp_path / "bundle/GUI/update_config.py"))
    assert update_config.release_notes("0.1.5") == bundled.read_text().strip()
