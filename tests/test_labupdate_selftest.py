"""Mandatory vector rejection, file smoke feeds and windowed JSON diagnostics."""

import argparse
from dataclasses import replace
import io
import json
import ssl
import sys
import urllib.error
import urllib.request

import pytest

from GUI.labupdate import selftest
from GUI.labupdate.config import UpdateConfig, platform_key
from labupdate_signing import public_key, sign


def config(pubkey=""):
    return UpdateConfig("Test", "lab/test", pubkey, settings_org="Tests", settings_app="Selftest")


@pytest.fixture(autouse=True)
def offline_https(monkeypatch):
    real_open = urllib.request.urlopen
    def opener(request, *, timeout):
        if request.full_url == "https://github.com/":
            raise urllib.error.URLError("offline smoke test")
        return real_open(request, timeout=timeout)
    monkeypatch.setattr(selftest.tls, "urlopen", opener)


def test_embedded_vector_generated_by_helper():
    assert selftest.SELF_TEST_PUBKEY == public_key()
    assert selftest.SELF_TEST_SIGNATURE == sign(selftest.SELF_TEST_DATA)


def test_embedded_vector_passes():
    result = selftest.run_self_test(config())
    assert result["ok"] and result["verification_ran"]
    assert result["signature"]["tampered_rejected"]
    assert result["public_key"]["status"] == "not configured"
    assert result["https"]["status"] == "network"


def test_silently_disabled_verifier_fails(monkeypatch):
    monkeypatch.setattr(selftest.minisign, "verify_bytes", lambda *args: None)
    result = selftest.run_self_test(config())
    assert not result["ok"] and not result["verification_ran"]
    assert "accepted" in result["signature"]["message"]


def test_certificate_error_fails(monkeypatch):
    def bad_open(*args, **kwargs):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))
    monkeypatch.setattr(selftest.tls, "urlopen", bad_open)
    result = selftest.run_self_test(config())
    assert not result["ok"] and result["https"]["status"] == "certificate"
    assert result["verification_ran"]


def test_any_http_status_is_ok(monkeypatch):
    def http_error(*args, **kwargs):
        raise urllib.error.HTTPError("https://github.com/", 503, "Unavailable", {}, io.BytesIO())
    monkeypatch.setattr(selftest.tls, "urlopen", http_error)
    assert selftest.run_self_test(config())["https"] == {"status": "ok", "http_status": 503}


@pytest.mark.parametrize("archive_dir", [False, True])
def test_file_manifest_uses_only_embedded_key(tmp_path, archive_dir):
    archive = tmp_path / "app.zip"
    archive.write_bytes(b"fake signed archive; the smoke test does not extract it")
    manifest = tmp_path / "latest.json"
    manifest.write_text(json.dumps({"version": "2.0.0", "platforms": {platform_key(): {
        "url": archive.as_uri(), "signature": sign(archive.read_bytes())}}}))
    # A different valid application key must never replace the embedded test key.
    cfg = config(public_key(seed=b"x" * 32))
    result = selftest.run_self_test(cfg, manifest.as_uri(), tmp_path if archive_dir else None)
    assert result["ok"] and result["manifest"]["archive_verified"]
    manifest.write_text(manifest.read_text().replace(sign(archive.read_bytes()), sign(archive.read_bytes(), seed=b"x" * 32)))
    assert not selftest.run_self_test(cfg, manifest.as_uri())["ok"]


def test_bad_configured_key_fails():
    assert not selftest.run_self_test(config("garbage"))["ok"]


def test_output_file_and_no_stdout(tmp_path, monkeypatch):
    parser = argparse.ArgumentParser()
    selftest.add_self_test_argument(parser)
    assert selftest.handle_self_test(parser.parse_args([]), config()) is None
    output = tmp_path / "selftest.json"
    args = parser.parse_args(["--update-self-test", "--update-self-test-output", str(output)])
    monkeypatch.setattr(sys, "stdout", None)
    assert selftest.handle_self_test(args, config()) == 0
    assert json.loads(output.read_text())["verification_ran"]
    monkeypatch.setattr(selftest.minisign, "verify_bytes", lambda *a: None)
    assert selftest.handle_self_test(args, config()) == 1
    assert not json.loads(output.read_text())["ok"]


def test_unexpected_https_failure_is_not_ignored(monkeypatch):
    def broken_open(*args, **kwargs):
        raise RuntimeError("broken transport")
    monkeypatch.setattr(selftest.tls, "urlopen", broken_open)
    result = selftest.run_self_test(config())
    assert not result["ok"] and result["https"]["status"] == "error"
