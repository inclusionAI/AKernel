"""Bounded diagnostic messages that do not echo configured credentials."""

import os

_SECRET_NAMES = (
    "AKERNEL_TOKEN",
    "AKERNEL_SECOND_TENANT_TOKEN",
    "AKERNEL_REDIS_PASSWORD",
    "AWS_SECRET_ACCESS_KEY",
    "AKERNEL_S3_SECRET_ACCESS_KEY",
)


def safe_error(error: BaseException, *, limit: int = 300) -> str:
    message = repr(error)
    for name in _SECRET_NAMES:
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, "<redacted>")
    return message[:limit]
