"""Compatibility exports for the shared TLS helper in :mod:`UPbRPy.tls`."""

from UPbRPy.tls import (
    ca_bundle,
    ca_certificate_count,
    is_certificate_error,
    reset_cache,
    ssl_context as https_context,
    urlopen,
)

__all__ = [
    "urlopen", "is_certificate_error", "ca_bundle", "https_context",
    "reset_cache", "ca_certificate_count",
]
