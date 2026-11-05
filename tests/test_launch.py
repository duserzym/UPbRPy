"""Launch paths: --open targets, upbr:// links, hand-off, URL registration."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from GUI.launch import (  # noqa: E402
    FileOpenFilter,
    LaunchError,
    SingleInstance,
    parse_open_target,
    parse_upbr_url,
    register_url_scheme,
    unregister_url_scheme,
    url_scheme_command,
)

from synthetic_bundles import write_session_bundle, zip_directory  # noqa: E402


REPO = Path(__file__).resolve().parents[1]
SESSION = "0192f0c4-0000-7000-8000-000000000001"


@pytest.fixture(scope="module")
def app():
    from GUI.theme import build_application

    return QtWidgets.QApplication.instance() or build_application([], theme_mode="light")


# --------------------------------------------------------------- parsing
def test_session_link():
    target = parse_open_target(
        f"upbr://session/{SESSION}?server=https%3A%2F%2Flab.example.org%2F&code=abc123"
    )
    assert target.kind == "session" and target.is_url
    assert target.identifier == SESSION
    assert target.server == "https://lab.example.org/"
    assert target.code == "abc123"
    plain = parse_upbr_url(f"upbr://session/{SESSION}?server=http://localhost:8010")
    assert plain.code == "" and plain.server == "http://localhost:8010"


def test_run_and_auth_links():
    run = parse_upbr_url("upbr://run/run-7?server=https://lab.example.org")
    assert (run.kind, run.identifier, run.server) == ("run", "run-7", "https://lab.example.org")
    assert parse_upbr_url("upbr://run/run-7").server == ""
    callback = parse_upbr_url("upbr://auth/callback?code=xyz")
    assert (callback.kind, callback.code) == ("auth_callback", "xyz")
    # laicpmsdb's app redirect style: parameters after a fragment route.
    fragment = parse_upbr_url("upbr://auth/callback#/auth/callback?code=frag")
    assert fragment.code == "frag"
    assert parse_upbr_url("UPBR://Session/abc?server=x").kind == "session"


@pytest.mark.parametrize(
    "value, message",
    [
        ("upbr://session/abc", "server"),
        ("upbr://session/?server=x", "upbr://session/<id>"),
        ("upbr://session/a/b?server=x", "upbr://session/<id>"),
        ("upbr://auth/callback", "no code"),
        ("upbr://auth/callback?error=access_denied", "access_denied"),
        ("upbr://mount/1", "does not know"),
        ("upbr://", "nothing"),
    ],
)
def test_bad_links(value, message):
    with pytest.raises(LaunchError, match=message):
        parse_open_target(value)


def test_paths(tmp_path):
    folder = tmp_path / "Apatite_20Mar25"
    folder.mkdir()
    assert parse_open_target(str(folder)).kind == "directory"
    project = tmp_path / "s.upbr"
    project.write_text("{}")
    assert parse_open_target(str(project)).kind == "project"
    assert parse_open_target(project.as_uri()).path == project.resolve()
    archive = tmp_path / "bundle.ZIP"
    archive.write_bytes(b"PK")
    assert parse_open_target(str(archive)).kind == "bundle_zip"
    with pytest.raises(LaunchError, match="does not exist"):
        parse_open_target(str(tmp_path / "missing.upbr"))
    other = tmp_path / "notes.txt"
    other.write_text("x")
    with pytest.raises(LaunchError, match="not a folder"):
        parse_open_target(str(other))


# ------------------------------------------------------ Windows registry
class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def CreateKey(self, root, path):  # noqa: N802 - winreg API
        self.values.setdefault(path, {})
        return path

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self.values[key][name] = value

    def CloseKey(self, key):  # noqa: N802
        pass

    def DeleteKey(self, root, path):  # noqa: N802
        if path not in self.values:
            raise OSError(path)
        del self.values[path]


def test_windows_registration_writes_hkcu_classes_upbr():
    fake = FakeWinreg()
    command = register_url_scheme(r"C:\Apps\UPbRPy Studio\UPbRPy Studio.exe", winreg_module=fake)
    assert command == r'"C:\Apps\UPbRPy Studio\UPbRPy Studio.exe" --open "%1"'
    root = r"Software\Classes\upbr"
    assert fake.values[root] == {"": "URL:UPbRPy Studio", "URL Protocol": ""}
    assert fake.values[root + r"\shell\open\command"][""] == command
    assert fake.values[root + r"\DefaultIcon"][""].endswith('UPbRPy Studio.exe",0')
    unregister_url_scheme(winreg_module=fake)
    assert root not in fake.values


def test_source_checkout_command_runs_the_gui_module():
    command = url_scheme_command()
    assert command.endswith('-m GUI.main --open "%1"')


def test_register_refuses_off_windows():
    if sys.platform == "win32":
        pytest.skip("Windows has a real registry")
    with pytest.raises(LaunchError, match="CFBundleURLTypes"):
        register_url_scheme()


# ------------------------------------------------------ single instance
def _wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
        if condition():
            return True
        time.sleep(0.01)
    return False


def _child_env(name: str) -> dict:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", UPBRPY_INSTANCE_NAME=name)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))
    return env


def test_second_launch_hands_link_to_running_instance(app):
    name = f"UPbRPy-test-{uuid.uuid4().hex[:8]}"
    instance = SingleInstance(name)
    received = []
    instance.received.connect(received.append)
    assert instance.listen()
    try:
        link = f"upbr://session/{SESSION}?server=http://127.0.0.1:9&code=c1"
        # The real entry point: a second `upbrpy-gui --open <link>`.
        child = subprocess.Popen(
            [sys.executable, "-m", "GUI.main", "--open", link],
            cwd=REPO, env=_child_env(name),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert _wait_for(lambda: received and child.poll() is not None, timeout=60)
        assert child.returncode == 0, child.stderr.read().decode()[-2000:]
        assert received == [link]
        # A second listener under the same name is refused while one runs.
        assert not SingleInstance(name).listen()
    finally:
        instance.close()


def test_send_without_running_instance_returns_false(app):
    assert not SingleInstance(f"UPbRPy-none-{uuid.uuid4().hex[:8]}").send("x", timeout_ms=200)


def test_macos_file_open_event_routes_links_and_files(app, tmp_path):
    seen = []
    event_filter = FileOpenFilter(seen.append)
    link = "upbr://run/run-1?server=https://lab.example.org"
    assert event_filter.eventFilter(app, QtGui.QFileOpenEvent(QtCore.QUrl(link)))
    project = tmp_path / "x.upbr"
    assert event_filter.eventFilter(app, QtGui.QFileOpenEvent(str(project)))
    assert seen == [link, str(project)]


# ------------------------------------------------------------- dispatch
@pytest.fixture()
def controller(app, monkeypatch):
    from GUI.icon import build_app_icon
    from GUI.main import StudioWindowController

    controller = StudioWindowController(theme_mode="light", app_icon=build_app_icon())
    opened = []
    monkeypatch.setattr(
        "GUI.main_window.MainWindow.load_project_file",
        lambda self, path, background=True: opened.append(("project", str(path))),
    )
    monkeypatch.setattr(
        "GUI.main_window.MainWindow.load_experiment_directory",
        lambda self, path, *a, **k: opened.append(("directory", str(path))),
    )
    controller.opened = opened
    yield controller
    for window in list(controller.windows.values()):
        window.close()


def test_open_target_dispatch(controller, tmp_path, monkeypatch):
    bundle = write_session_bundle(tmp_path / "src")
    plain = tmp_path / "plain"
    plain.mkdir()
    handled = []
    monkeypatch.setattr(type(controller.database), "handle", lambda self, t: handled.append(t))

    controller.open_target(str(plain))
    controller.open_target(str(bundle))  # a bundle root opens its project
    controller.open_target(str(bundle / "Synthetic_20Mar25.upbr"))
    controller.open_target(f"upbr://session/{SESSION}?server=http://x")
    assert controller.opened == [
        ("directory", str(plain.resolve())),
        ("project", str(bundle.resolve() / "Synthetic_20Mar25.upbr")),
        ("project", str(bundle.resolve() / "Synthetic_20Mar25.upbr")),
    ]
    assert [t.kind for t in handled] == ["session"]
    # The active empty tab was reused while it stayed empty (stubs load nothing).
    assert len(controller.windows) == 1
    assert controller.active_window().tabs.count() == 1


def test_open_bundle_zip_unpacks_into_cache(controller, tmp_path):
    bundle = write_session_bundle(tmp_path / "src")
    archive = tmp_path / "Synthetic_20Mar25.zip"
    archive.write_bytes(zip_directory(bundle))
    controller.database.cache_root = tmp_path / "cache"
    controller.open_target(str(archive))
    assert _wait_for(lambda: controller.opened)
    assert controller.opened == [
        ("project", str(tmp_path / "cache" / "Synthetic_20Mar25" / "Synthetic_20Mar25.upbr"))
    ]
