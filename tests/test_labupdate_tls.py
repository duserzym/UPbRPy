"""Trust selection, ignored stale build paths and certificate classification."""

import ssl
import sys
import urllib.error

import pytest

from GUI.labupdate import tls


@pytest.fixture(autouse=True)
def reset():
    tls.reset_cache()
    yield
    tls.reset_cache()


def test_nonexistent_environment_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "missing"))
    context = tls.https_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert tls.describe()["environment"] == {}
    assert tls.https_context() is context


def test_certifi_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)
    assert tls.describe()["source"] == "certifi+system"
    assert tls.https_context().cert_store_stats()["x509_ca"] > 0


def test_system_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)
    monkeypatch.setitem(sys.modules, "certifi", None)
    assert tls.describe()["source"] == "system"
    assert tls.https_context().verify_mode == ssl.CERT_REQUIRED


def test_existing_environment_added(monkeypatch, tmp_path):
    import certifi
    monkeypatch.setitem(sys.modules, "truststore", None)
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    assert tls.describe()["environment"] == {"cafile": certifi.where(), "capath": str(tmp_path)}


@pytest.mark.parametrize("error,expected", [(ssl.SSLCertVerificationError("fail"), True),
    (urllib.error.URLError(ssl.SSLCertVerificationError("fail")), True),
    (ValueError("certificate verify failed"), True), (ValueError("CERTIFICATE_VERIFY_FAILED"), True),
    (urllib.error.URLError("offline"), False), (ValueError("other"), False)])
def test_certificate_errors(error, expected):
    assert tls.is_certificate_error(error) is expected
