"""TLS policy shared by SDK configuration and standalone HTTP queries."""

from __future__ import annotations

import os
import ssl


def verification_from_env() -> bool:
    """Read the process TLS policy, retaining the unverified deployment default."""

    value = os.environ.get("AKERNEL_VERIFY_TLS", "0").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError("AKERNEL_VERIFY_TLS must be 1/true/yes/on or 0/false/no/off")


def create_ssl_context() -> ssl.SSLContext:
    """Build an HTTP context; verified mode honors SSL_CERT_FILE/SSL_CERT_DIR."""

    if verification_from_env():
        context = ssl.create_default_context()
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context
