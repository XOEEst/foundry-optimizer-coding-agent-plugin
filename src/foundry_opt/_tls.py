"""TLS contexts that trust the host's verified system certificate store."""

from __future__ import annotations

import ssl


def system_ssl_context() -> ssl.SSLContext:
    """Return a hostname-checking client context using system trust anchors."""

    context = ssl.create_default_context()
    if context.minimum_version < ssl.TLSVersion.TLSv1_2:
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


__all__ = ["system_ssl_context"]
