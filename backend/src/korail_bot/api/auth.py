"""
Request authentication for internal HTTP endpoints.

Background reservation processes call /reservation-callback and
/check_payment over loopback and prove themselves with a token generated at
app start. These endpoints expose user state or send Telegram messages, so
they are never safe to leave open.
"""

import hmac

from flask import request

from korail_bot.config.settings import settings
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)

_LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "localhost"}


def _matches(expected: str | None, provided: str | None) -> bool:
    """
    Compare two secrets without leaking their contents through timing.

    Compared as bytes: compare_digest raises on str inputs holding non-ASCII
    characters, and the provided value comes from an untrusted request.
    """
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def verify_internal_request() -> bool:
    """
    Verify that a request came from one of our own background processes.

    Requires both a loopback source address and the per-run internal token,
    so neither a leaked token nor a spoofed source address is enough on
    its own.

    Returns:
        True if the request is authentic
    """
    if request.remote_addr not in _LOOPBACK_ADDRESSES:
        logger.warning(
            f"Rejected internal callback from non-loopback address {request.remote_addr}"
        )
        return False

    if not _matches(settings.INTERNAL_CALLBACK_TOKEN, request.args.get("token")):
        logger.warning(f"Rejected internal callback with invalid token from {request.remote_addr}")
        return False

    return True
