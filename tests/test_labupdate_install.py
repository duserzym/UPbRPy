"""Signed archives, actual detached swaps, extraction safety and backup retention."""

from dataclasses import replace
import io
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import tarfile
import zipfile

import pytest

from GUI.labupdate.config import UpdateConfig
from GUI.labupdate import install
from GUI.labupdate.manifest import Release
from GUI.labupdate.minisign import SignatureError
from labupdate_signing import public_key, sign


def config(**kwargs):
    return UpdateConfig("Test App", "lab/test", public_key(), settings_org="Tests", settings_app="Install", **kwargs)


def make_archive(tmp_path, kind="tar", *, extra=None, windows=False):
    name = "Test App" if windows else "Test App.app"
    executable = name + ".exe" if windows else "Contents/MacOS/Test App"
    files = {f"{name}/{executable}": (b"new executable", 0o755), f"{name}/version": (b"new", 0o644)}
    if not windows:
        files[f"{name}/Contents/Info.plist"] = (plistlib.dumps({"CFBundleExecutable": "Test App"}), 0o644)
    path = tmp_path / ("update.zip" if kind == "zip" else "update.app.tar.gz")
    if kind == "zip":
        with zipfile.ZipFile(path, "w") as bundle:
            for filename, (data, mode) in files.items():
                info = zipfile.ZipInfo(filename)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | mode) << 16
                bundle.writestr(info, data)
            if extra:
                filename, data, symlink = extra
                info = zipfile.ZipInfo(filename)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777 if symlink else stat.S_IFREG | 0o644) << 16
                bundle.writestr(info, data)
    else:
        with tarfile.open(path, "w:gz") as bundle:
            for filename, (data, mode) in files.items():
                info = tarfile.TarInfo(filename)
                info.mode, info.size = mode, len(data)
                bundle.addfile(info, io.BytesIO(data))
            if extra:
                filename, data, symlink = extra
                info = tarfile.TarInfo(filename)
                if symlink:
                    info.type, info.linkname = tarfile.SYMTYPE, data.decode()
                    bundle.addfile(info)
                else:
                    info.size = len(data)
                    bundle.addfile(info, io.BytesIO(data))
    release = Release("2.0.0", "notes", None, path.as_uri(), sign(path.read_bytes()))
    return path, release


def old_app(tmp_path, windows=False):
    root = tmp_path / "Applications" / ("Test App" if windows else "Test App.app")
    root.mkdir(parents=True)
    (root / "version").write_text("old")
    return root


@pytest.mark.parametrize("kind", ["tar", "zip"])
def test_real_mac_swap_and_cleanup(tmp_path, monkeypatch, kind):
    archive, release = make_archive(tmp_path, kind, extra=("Test App.app/Contents/alias", b"MacOS/Test App", True))
    root = old_app(tmp_path)
    commands = tmp_path / "commands"
    commands.mkdir()
    # The helper sees an exited parent; only launching the app is stubbed.
    monkeypatch.setattr(install.os, "getpid", lambda: 99999999)
    opened = tmp_path / "opened"
    open_stub = commands / "open"
    open_stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$LABUPDATE_OPEN_LOG"\n')
    open_stub.chmod(0o755)
    monkeypatch.setenv("LABUPDATE_OPEN_LOG", str(opened))
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ["PATH"])
    script = install.prepare_install(archive, root, release, config(), runner=lambda cmd: subprocess.run(cmd, check=True))
    assert (root / "version").read_text() == "new"
    assert (install.previous_path(root) / "version").read_text() == "old"
    assert (root / "Contents/alias").is_symlink()
    assert (root / "Contents/alias").read_bytes() == b"new executable"
    assert (root / "Contents/MacOS/Test App").stat().st_mode & stat.S_IXUSR
    assert opened.read_text().splitlines() == ["-n", str(root)]
    assert not script.parent.exists()
    install.cleanup_previous(root)
    assert not install.previous_path(root).exists()
    assert (root / "version").read_text() == "new"


def test_windows_zip_stages_real_folder_and_waiting_helper(tmp_path):
    archive, release = make_archive(tmp_path, "zip", windows=True)
    root = old_app(tmp_path, windows=True)
    commands = []
    script = install.prepare_install(archive, root, release, config(), commands.append)
    try:
        content = script.read_text()
        assert "tasklist /FI" in content and ":wait" in content
        assert content.count('move "') == 3  # forward moves and rollback
        assert "Test App.exe" in content and 'start "" /D' in content
        assert commands == [["cmd.exe", "/d", "/c", str(script)]]
        assert (script.parent / "Test App/Test App.exe").read_bytes() == b"new executable"
        assert (root / "version").read_text() == "old"
    finally:
        shutil.rmtree(script.parent)


@pytest.mark.parametrize("damage", ["bad", "missing", "no_key"])
def test_no_install_without_valid_signature(tmp_path, damage, monkeypatch):
    archive, release = make_archive(tmp_path)
    root, cfg = old_app(tmp_path), config()
    if damage == "bad":
        archive.write_bytes(archive.read_bytes() + b"changed")
    elif damage == "missing":
        release = replace(release, signature="")
    else:
        cfg = replace(cfg, pubkey="")
    monkeypatch.setattr(install, "_extract", lambda *a, **kw: pytest.fail("Must verify before extracting"))
    with pytest.raises((SignatureError, install.NotConfiguredError)):
        install.prepare_install(archive, root, release, cfg, lambda cmd: pytest.fail("helper launched"))
    assert (root / "version").read_text() == "old"
    assert list(root.parent.iterdir()) == [root]


@pytest.mark.parametrize("kind", ["tar", "zip"])
@pytest.mark.parametrize("extra", [("../outside", b"bad", False), ("/absolute", b"bad", False),
    ("C:/outside", b"bad", False), ("Test App.app/Contents/escape", b"../../../outside", True),
    ("Test App.app/Contents/escape", b"/tmp/outside", True)])
def test_reject_unsafe_archives(tmp_path, kind, extra):
    archive, release = make_archive(tmp_path, kind, extra=extra)
    root = old_app(tmp_path)
    with pytest.raises(ValueError):
        install.prepare_install(archive, root, release, config(), lambda cmd: pytest.fail("unsafe archive launched"))
    assert list(root.parent.iterdir()) == [root]
    assert not (tmp_path / "outside").exists()


def test_link_traversal_rejected(tmp_path):
    archive, release = make_archive(tmp_path, extra=("Test App.app/Contents/MacOS", b"elsewhere", True))
    with pytest.raises(ValueError, match="symbolic link"):
        install.prepare_install(archive, old_app(tmp_path), release, config())


def test_existing_previous_is_retained(tmp_path):
    archive, release = make_archive(tmp_path)
    root = old_app(tmp_path)
    previous = install.previous_path(root)
    previous.mkdir()
    (previous / "version").write_text("previous")
    with pytest.raises(ValueError, match="Previous"):
        install.prepare_install(archive, root, release, config())
    assert (previous / "version").read_text() == "previous"


def test_backup_symlink_never_removed(tmp_path):
    root = old_app(tmp_path)
    keep = tmp_path / "keep"
    keep.mkdir()
    install.previous_path(root).symlink_to(keep, target_is_directory=True)
    install.cleanup_previous(root)
    assert keep.exists() and install.previous_path(root).is_symlink()


def test_file_download_and_progress(tmp_path):
    archive, release = make_archive(tmp_path)
    progress = []
    path = install.download_archive(release, config(), progress=lambda *args: progress.append(args))
    try:
        assert path != archive and path.read_bytes() == archive.read_bytes()
        assert progress[-1][0] == archive.stat().st_size
    finally:
        shutil.rmtree(path.parent)


@pytest.mark.parametrize("known_size", [True, False])
def test_archive_cap(tmp_path, known_size):
    class Response(io.BytesIO):
        headers = {"Content-Length": "100"} if known_size else {}
    release = Release("2.0.0", "", None, "https://example.org/app.zip", sign(b"x" * 100))
    with pytest.raises(ValueError, match="size limit"):
        install.download_archive(release, config(max_archive_bytes=10), lambda request, **kw: Response(b"x" * 100))


def test_reverified_after_download(tmp_path):
    archive, release = make_archive(tmp_path)
    path = install.download_archive(release, config())
    try:
        path.write_bytes(b"modified after download")
        with pytest.raises(SignatureError):
            install.prepare_install(path, old_app(tmp_path), release, config())
    finally:
        shutil.rmtree(path.parent)


def test_install_paths_are_generic(tmp_path, monkeypatch):
    root = old_app(tmp_path)
    assert install.previous_path(root).name == "Test App.previous.app"
    assert install.previous_path(tmp_path / "filament").name == "filament.previous"
    monkeypatch.setattr(install.sys, "frozen", True, raising=False)
    monkeypatch.setattr(install.sys, "platform", "darwin")
    monkeypatch.setattr(install.sys, "executable", str(root / "Contents/MacOS/Test App"))
    assert install.is_frozen() and install.install_root() == root
    monkeypatch.setattr(install.sys, "platform", "win32")
    monkeypatch.setattr(install.sys, "executable", str(root / "Test.exe"))
    assert install.install_root() == root
    assert install.can_replace(root) and not install.can_replace(None)


def test_download_http_error_includes_reply():
    import urllib.error
    def opener(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", {}, io.BytesIO(b"service unavailable"))
    release = Release("2.0.0", "", None, "https://example.org/app.zip", "sig")
    with pytest.raises(ValueError, match="HTTP 503: service unavailable"):
        install.download_archive(release, config(), opener)
