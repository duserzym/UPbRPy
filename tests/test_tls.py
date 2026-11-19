"""Offline certificate trust and headless TLS diagnostics."""

import io
import json
from pathlib import Path
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from UPbRPy.integration import tls


@pytest.fixture(autouse=True)
def isolated_tls_cache():
    tls.reset_cache()
    yield
    tls.reset_cache()


def test_bundled_roots_without_system_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "missing-certs"))
    context = tls.https_context()
    assert tls.ca_certificate_count() > 100
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert tls.https_context() is context
    tls.reset_cache()
    assert tls.https_context() is not context


def test_urlopen_uses_shared_https_context(monkeypatch):
    calls = []
    response = object()

    def open_request(opener, request, *, timeout):
        calls.append((opener, request, timeout))
        return response

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_request)
    request = urllib.request.Request("https://example.org")
    assert tls.urlopen(request, timeout=10) is response
    assert tls.urlopen("https://example.org/again", timeout=20) is response
    assert calls[0][0] is calls[1][0]
    assert calls[0][1:] == (request, 10)
    handler, = [h for h in calls[0][0].handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert handler._context is tls.https_context()
    tls.reset_cache()
    tls.urlopen(request, timeout=10)
    assert calls[2][0] is not calls[0][0]


@pytest.mark.parametrize("error,expected", [
    (ssl.SSLCertVerificationError("untrusted issuer"), True),
    (urllib.error.URLError(ssl.SSLCertVerificationError("untrusted issuer")), True),
    (urllib.error.URLError("timed out"), False),
    (RuntimeError("CERTIFICATE_VERIFY_FAILED"), True),
    (RuntimeError("certificate verify failed"), True),
])
def test_certificate_error(error, expected):
    assert tls.is_certificate_error(error) is expected


def test_requests_certificate_error():
    requests = pytest.importorskip("requests")
    assert tls.is_certificate_error(requests.exceptions.SSLError("untrusted issuer"))


def test_ca_bundle_missing_certifi(monkeypatch):
    monkeypatch.setitem(sys.modules, "certifi", None)
    assert tls.ca_bundle() is None


def test_ca_bundle_missing_file(monkeypatch, tmp_path):
    import certifi

    monkeypatch.setattr(certifi, "where", lambda: str(tmp_path / "missing.pem"))
    assert tls.ca_bundle() is None


def test_context_adds_local_roots_and_falls_back(monkeypatch):
    from unittest.mock import Mock

    context = Mock()
    create = Mock(return_value=context)
    monkeypatch.setattr(tls.ssl, "create_default_context", create)
    monkeypatch.setattr(tls, "ca_bundle", lambda: "/bundle.pem")
    assert tls.https_context() is context
    create.assert_called_once_with(cafile="/bundle.pem")
    context.load_default_certs.assert_called_once_with()
    tls.reset_cache()
    create.reset_mock()
    monkeypatch.setattr(tls, "ca_bundle", lambda: None)
    assert tls.https_context() is context
    create.assert_called_once_with()


@pytest.mark.parametrize("has_stdout", [True, False])
def test_check_tls_offline(monkeypatch, capsys, tmp_path, has_stdout):
    pytest.importorskip("PySide6")
    from GUI import main

    def forbidden(*args, **kwargs):
        pytest.fail("Offline TLS check created a QApplication or attempted a download")

    monkeypatch.setattr(main, "build_application", forbidden)
    monkeypatch.setattr(tls, "urlopen", forbidden)
    output = tmp_path / "check-tls.json"
    with monkeypatch.context() as patch:
        if not has_stdout:
            patch.setattr(sys, "stdout", None)
        assert main.main(["--check-tls", "--check-tls-output", str(output)]) == 0
    printed = capsys.readouterr().out
    report_json = output.read_text(encoding="utf-8")
    assert printed == (report_json if has_stdout else "")
    report = json.loads(report_json)
    assert report == {
        "ca_certificates": tls.ca_certificate_count(), "ca_bundle": tls.ca_bundle(),
        "url": None, "ok": None, "error": None, "detail": None,
    }
    assert report["ca_certificates"] > 100


@pytest.mark.parametrize("error,category,exit_code", [
    (None, None, 0),
    (urllib.error.URLError(ssl.SSLCertVerificationError("untrusted issuer")), "certificate", 1),
    (urllib.error.URLError("timed out"), "network", 0),
])
def test_check_tls_url(monkeypatch, capsys, tmp_path, error, category, exit_code):
    pytest.importorskip("PySide6")
    from GUI.main import main

    calls = []

    def open_request(url, *, timeout):
        calls.append((url, timeout))
        if error is not None:
            raise error
        return io.BytesIO()

    monkeypatch.setattr(tls, "urlopen", open_request)
    output = tmp_path / "check-tls.json"
    assert main(["--check-tls", "https://example.org", "--check-tls-output", str(output)]) == exit_code
    printed = capsys.readouterr().out
    assert output.read_text(encoding="utf-8") == printed
    report = json.loads(printed)
    assert calls == [("https://example.org", 10)]
    assert report["url"] == "https://example.org"
    assert report["ok"] is (error is None)
    assert report["error"] == category
    assert report["detail"] == (str(error) if error is not None else None)


def test_check_tls_rejects_too_few_roots(monkeypatch, capsys):
    pytest.importorskip("PySide6")
    from GUI.main import main

    monkeypatch.setattr(tls, "ca_certificate_count", lambda: 99)
    assert main(["--check-tls"]) == 1
    assert json.loads(capsys.readouterr().out)["ca_certificates"] == 99


@pytest.mark.parametrize("source,roots,error,returncode,stderr,expected", [
    ("file", 150, None, 0, "", None),
    ("stdout", 150, None, 0, "", None),
    ("invalid-file", 150, None, 0, "", "did not return JSON"),
    ("file", 99, None, 0, "", "failed"),
    ("file", 150, "certificate", 1, "", "failed"),
    ("file", 150, "network", 0, "", "Warning"),
    ("file", 150, None, 1, "", "failed"),
    ("file", 150, None, 0, "Traceback", "failed"),
])
def test_build_smoke_tls(monkeypatch, tmp_path, capsys, source, roots, error,
                         returncode, stderr, expected):
    from scripts import build_desktop as build

    monkeypatch.delenv("UPBRPY_SMOKE_SESSION", raising=False)
    monkeypatch.setattr(build, "_write_smoke_project", lambda work: work / "smoke.upbr")
    output = tmp_path / "check-tls.json"
    output.write_text('{"stale": true}', encoding="utf-8")
    report_json = json.dumps({"ca_certificates": roots, "error": error, "detail": "unavailable"})

    def run_frozen(application, arguments, work, **kwargs):
        if "--export" in arguments:
            Path(arguments[arguments.index("--export") + 1]).write_bytes(b"x" * 10_000)
        elif "--verify-reduction" in arguments:
            verification = Path(arguments[arguments.index("--verification-output") + 1])
            verification.mkdir()
            (verification / "verification.json").write_text(json.dumps({
                "frozen": True, "spots": 12, "stages": ["concentration"],
            }), encoding="utf-8")
        else:
            assert arguments == ["--check-tls", "https://api.github.com",
                                 "--check-tls-output", str(output)]
            assert not output.exists()
            assert kwargs["check"] is False
            if source != "stdout":
                output.write_text(report_json if source == "file" else "invalid", encoding="utf-8")
            # Invalid stdout must be ignored when the file exists. Valid
            # stdout must not hide a malformed file.
            stdout = "" if source == "file" else report_json
            return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(build, "_run_frozen", run_frozen)
    if expected and expected != "Warning":
        with pytest.raises(RuntimeError, match=expected):
            build.smoke_test_application(tmp_path / "app", tmp_path)
    else:
        assert build.smoke_test_application(tmp_path / "app", tmp_path).exists()
        assert ("Warning:" in capsys.readouterr().out) is (expected == "Warning")


@pytest.mark.parametrize("error,expected", [
    (None, True),
    (urllib.error.HTTPError("https://example.org", 503, "Unavailable", {}, None), True),
    (urllib.error.URLError(ssl.SSLCertVerificationError("untrusted issuer")), False),
])
def test_server_health_uses_tls(monkeypatch, error, expected):
    pytest.importorskip("PySide6")
    from GUI.database import _server_answers

    calls = []

    def open_request(url, *, timeout):
        calls.append((url, timeout))
        if error is not None:
            raise error
        return io.BytesIO()

    monkeypatch.setattr(tls, "urlopen", open_request)
    assert _server_answers("https://example.org/", timeout=2) is expected
    assert calls == [("https://example.org/v1/health", 2)]
