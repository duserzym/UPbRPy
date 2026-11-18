"""Read session bundles written before the UPbDB rename without rewriting them."""

from __future__ import annotations

import hashlib
import os
import tomllib

import pytest

from UPbRPy.session_bundle import unpack_session_bundle
from UPbRPy.session_manifest import (
    SessionManifestError,
    parse_session_manifest,
    read_session_manifest,
)

from synthetic_bundles import LABELS, write_session_bundle, zip_directory


@pytest.fixture
def legacy_bundle(tmp_path):
    bundle = write_session_bundle(tmp_path / "source")
    manifest = bundle / "session_manifest.toml"
    manifest.write_bytes(
        b'format = "laicpmsdb-session-bundle"\n' + manifest.read_bytes()
    )
    return bundle


def test_legacy_manifest_parses_identically_to_current_format(legacy_bundle):
    path = legacy_bundle / "session_manifest.toml"
    original = path.read_bytes()
    legacy = tomllib.loads(original.decode("utf-8"))
    current = {**legacy, "format": "upbdb-session-bundle"}

    expected = parse_session_manifest(current, path)
    assert parse_session_manifest(legacy, path) == expected
    assert read_session_manifest(path) == expected
    assert path.read_bytes() == original


def test_unknown_manifest_format_gives_next_step(tmp_path):
    with pytest.raises(SessionManifestError) as error:
        parse_session_manifest(
            {"format": "unknown-session-bundle"}, tmp_path / "session_manifest.toml"
        )
    assert str(error.value) == (
        "Unsupported session bundle format unknown-session-bundle. "
        "Download the session again from UPbDB, or open its Qtegra export "
        "folder directly."
    )


def test_unpack_legacy_bundle_preserves_manifest_and_hash(legacy_bundle, tmp_path):
    path = legacy_bundle / "session_manifest.toml"
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()

    unpacked = unpack_session_bundle(
        zip_directory(legacy_bundle), tmp_path / "cache", expected_export_sha256=digest
    )

    assert unpacked.export_sha256 == digest
    assert (unpacked.path / path.name).read_bytes() == original
    assert path.read_bytes() == original


def test_studio_open_legacy_bundle_without_message_box(legacy_bundle, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    from GUI.icon import build_app_icon
    from GUI.main import StudioWindowController
    from GUI.main_window import MainWindow
    from GUI.theme import build_application

    monkeypatch.setenv("UPBRPY_UPDATE_PREVIEW", "none")
    app = QtWidgets.QApplication.instance() or build_application([], theme_mode="light")
    messages = []

    def unexpected_message(*args, **kwargs):
        messages.append((args, kwargs))
        pytest.fail("Opening a legacy session bundle created a QMessageBox")

    monkeypatch.setattr(QtWidgets.QMessageBox, "__init__", unexpected_message)
    for name in ("critical", "warning", "information", "question"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, unexpected_message)

    # Use the real project restore, synchronously so assertions see its result.
    load_project_file = MainWindow.load_project_file
    monkeypatch.setattr(
        MainWindow, "load_project_file",
        lambda self, path: load_project_file(self, path, background=False),
    )
    controller = StudioWindowController(theme_mode="light", app_icon=build_app_icon())
    path = legacy_bundle / "session_manifest.toml"
    original = path.read_bytes()
    try:
        # Studio's --open dispatch selects the bundled .upbr and imports raw/.
        controller.open_target(str(legacy_bundle))
        app.processEvents()
        window = controller.active_window().active_reduction
        assert window.session.loaded
        assert set(window.session.experiment["spot_names"]) == set(LABELS)
        assert window.project_path == legacy_bundle / f"{legacy_bundle.name}.upbr"
        assert window.session.origin["session_key"] == legacy_bundle.name
        assert messages == []
        assert not any(isinstance(widget, QtWidgets.QMessageBox) for widget in app.allWidgets())
        assert path.read_bytes() == original
    finally:
        for shell in list(controller.windows.values()):
            for index in range(shell.tabs.count()):
                shell.tabs.widget(index).has_unsaved_changes = lambda: False
            shell.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
