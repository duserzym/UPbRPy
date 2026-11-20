"""Reducer legality, actionable error classification and exact compact UI text."""

from dataclasses import replace
from pathlib import Path
import ssl
import urllib.error

import pytest

from GUI.labupdate.install import NotConfiguredError
from GUI.labupdate.manifest import NoPublicRelease, Release
from GUI.labupdate.minisign import SignatureError
from GUI.labupdate.state import UpdateState, button_view, classify_error, transition

RELEASE = Release("v2.0.0", "notes", None, "https://example.org/app.zip", "sig")


def test_transitions():
    state = UpdateState(app_name="filament")
    assert transition(state, "download_started") is state
    state = transition(state, ("check_started", True))
    assert state.manual and state.status == "checking"
    assert transition(state, "check_started") is state
    state = transition(state, ("found", RELEASE))
    assert state.status == "available"
    state = transition(state, "download_started")
    state = transition(state, ("progress", 43, 100))
    assert (state.progress, state.total) == (43, 100)
    state = transition(state, ("downloaded", "/tmp/archive.zip"))
    assert state.status == "ready" and state.path == Path("/tmp/archive.zip")
    assert transition(state, "check_started") is state
    assert transition(state, ("check_started", True)).status == "checking"
    failed = transition(state, ("failed", SignatureError("bad signature")))
    assert failed.status == "error" and failed.kind == "signature" and failed.path is None
    assert failed.message == "bad signature"
    assert transition(UpdateState("checking"), "no_update").status == "none"
    assert transition(UpdateState(), ("unsupported", "read only", "not_writable")).status == "unsupported"


@pytest.mark.parametrize("error,kind,message", [
    (ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"), "certificate", "Could not verify the secure connection to updates.example.org."),
    (urllib.error.URLError("offline"), "offline", "No internet connection, or updates.example.org did not answer."),
    (TimeoutError("stalled"), "offline", "No internet connection, or updates.example.org did not answer."),
    (NoPublicRelease("404"), "no_release", "No public release of filament has been published."),
    (SignatureError("bad signature"), "signature", "The update's signature did not match. Nothing was installed."),
    (PermissionError("read only"), "not_writable", "filament cannot replace itself in this folder. Download the new version from GitHub."),
    (NotConfiguredError("no key"), "not_configured", "Updates are not configured for this build yet."),
    (ValueError("unexpected"), "other", "The update check failed: unexpected"),
])
def test_classify(error, kind, message):
    assert classify_error(error, app="filament", host="updates.example.org") == (kind, message)


@pytest.mark.parametrize("status,text", [("idle", "Check for a new version of filament."), ("checking", "Checking for updates…"),
    ("none", "Up to date (v1.0.0)"), ("available", "Update available: v2.0.0\nCurrent version: v1.0.0"),
    ("downloading", "Downloading v2.0.0… 43%"), ("ready", "Restart to update to v2.0.0")])
def test_button_text(status, text):
    state = UpdateState(status, info=RELEASE, app_name="filament", progress=43, total=100)
    view = button_view(state, "v1.0.0", False)
    assert view.text == text
    assert view.badge == (status in {"available", "ready"})
    assert view.busy == (status in {"checking", "downloading"})
    if status in {"available", "ready"}:
        assert view.action == ("download" if status == "available" else "restart")


def test_error_details_preserved():
    raw = "HTTP failure " * 30
    state = transition(UpdateState("checking"), ("failed", ValueError(raw)))
    assert state.message == raw
    assert len(button_view(state, "1.0.0").text) < 160
