"""Verified HTTPS using bundled certificates and any local trust store."""

from __future__ import annotations

from functools import cache
from pathlib import Path
import ssl
import urllib.error
import urllib.request


def ca_bundle() -> str | None:
    """Return certifi's CA bundle when it is installed and present."""
    try:
        import certifi
    except ImportError:
        return None
    bundle = certifi.where()
    return bundle if Path(bundle).is_file() else None


@cache
def https_context() -> ssl.SSLContext:
    """Return a shared verifying context with bundled and local CA roots."""
    bundle = ca_bundle()
    if bundle is None:
        return ssl.create_default_context()
    context = ssl.create_default_context(cafile=bundle)
    context.load_default_certs()
    return context


@cache
def _https_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=https_context())
    )


def reset_cache() -> None:
    """Discard the context and opener, for example after changing trust stores."""
    _https_opener.cache_clear()
    https_context.cache_clear()


def urlopen(request_or_url, *, timeout):
    """Open a request or URL using the shared verifying HTTPS context."""
    return _https_opener().open(request_or_url, timeout=timeout)


def ca_certificate_count() -> int:
    """Return the number of trusted CA certificates in the shared context."""
    return https_context().cert_store_stats()["x509_ca"]


def is_certificate_error(exc) -> bool:
    """Recognize certificate failures from ssl, urllib and optional requests."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    if isinstance(exc, urllib.error.URLError) and isinstance(
        exc.reason, ssl.SSLCertVerificationError
    ):
        return True
    try:
        from requests.exceptions import SSLError
    except ImportError:
        pass
    else:
        if isinstance(exc, SSLError):
            return True
    message = str(exc).lower()
    return any(text in message for text in (
        "certificate_verify_failed", "certificate verify failed",
    ))
