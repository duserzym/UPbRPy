"""Verified feed creation with disposable signatures and mocked cargo signing."""

import json
from pathlib import Path

import pytest

from GUI.labupdate import release
from labupdate_signing import public_key, sign


@pytest.fixture
def inputs(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("- Print sheets fit A4 and Letter.\n")
    key = tmp_path / "public.key"
    key.write_text(public_key())
    archive = tmp_path / "app.tar.gz"
    archive.write_bytes(b"signed archive")
    Path(str(archive) + ".sig").write_text(sign(archive.read_bytes()))
    return dict(repo="duserzym/Test", version="1.2.3", notes_file=notes,
                archives={"darwin-aarch64": archive}, pubkey_file=key, out=tmp_path / "latest.json")


def test_feed_matches_tauri_and_checks_every_platform(inputs, tmp_path):
    windows = tmp_path / "app.zip"
    windows.write_bytes(b"windows archive")
    signature = Path(str(windows) + ".sig")
    signature.write_text(sign(windows.read_bytes()))
    inputs["archives"]["windows-x86_64"] = windows
    feed = release.build_feed(**inputs)
    assert json.loads(inputs["out"].read_text()) == feed
    assert feed["notes"] == inputs["notes_file"].read_text()
    assert feed["platforms"]["darwin-aarch64"]["url"] == "https://github.com/duserzym/Test/releases/download/v1.2.3/app.tar.gz"
    windows.write_bytes(b"tampered")
    inputs["out"].unlink()
    with pytest.raises(ValueError):
        release.build_feed(**inputs)
    assert not inputs["out"].exists()


@pytest.mark.parametrize("notes", [None, "", " \n", "x" * 601])
def test_notes_required(inputs, notes):
    if notes is None:
        inputs["notes_file"].unlink()
    else:
        inputs["notes_file"].write_text(notes)
    with pytest.raises((OSError, ValueError)):
        release.build_feed(**inputs)
    assert not inputs["out"].exists()


def test_sign_missing_only_and_verify_result(inputs, monkeypatch):
    archive = inputs["archives"]["darwin-aarch64"]
    Path(str(archive) + ".sig").unlink()
    calls = []

    def run(command, *, env, check):
        assert command == ["cargo", "tauri", "signer", "sign", "-f", "private.key", "-p", "", str(archive)]
        assert check and env["TAURI_SIGNING_PRIVATE_KEY_PASSWORD"] == ""
        calls.append(command)
        Path(str(archive) + ".sig").write_text(sign(archive.read_bytes()))

    monkeypatch.setattr(release.subprocess, "run", run)
    release.build_feed(**inputs, sign_with="private.key")
    release.build_feed(**inputs, sign_with="private.key")
    assert len(calls) == 1
    Path(str(archive) + ".sig").write_text("invalid")
    inputs["out"].unlink()
    with pytest.raises(ValueError):
        release.build_feed(**inputs, sign_with="private.key")
    assert not inputs["out"].exists() and len(calls) == 1


def test_cli(inputs):
    args = ["--repo", inputs["repo"], "--version", inputs["version"],
            "--notes-file", str(inputs["notes_file"]), "--darwin-aarch64", str(inputs["archives"]["darwin-aarch64"]),
            "--pubkey-file", str(inputs["pubkey_file"]), "--out", str(inputs["out"])]
    assert release.main(args) == 0
    assert inputs["out"].is_file()
