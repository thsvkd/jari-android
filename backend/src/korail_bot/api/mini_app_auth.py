"""
Who is on the other end of a Mini App request.

The chat flow never had to ask. Updates arrive over long polling from
Telegram's own servers, so the chat ID on an update is a fact - nothing a user
can write changes it. The Mini App inverts that: the page runs on the user's
phone and calls this app directly, so every request arrives with a chat ID the
sender chose. Believing it would let anyone book with anyone else's registered
account by typing a different number.

Telegram closes that hole with ``initData``: a query string it hands to the
page at launch, signed with a key derived from the bot token. Only Telegram
and this app know the token, so a valid signature is proof the payload came
from Telegram and names the user Telegram says it names. This module is the
only place that decides a request's chat ID, and it decides it from that
signature alone.
"""

import hashlib
import hmac
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qsl

from korail_bot.config.settings import settings
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)

# Telegram's fixed salt for deriving the signing key from the bot token.
_SECRET_SALT = b"WebAppData"

# Everything but `hash` goes into the string that gets signed, `hash` being
# the signature itself. `signature` stays in: it belongs to Telegram's
# separate Ed25519 scheme for third parties who do not hold the bot token,
# and it is part of the payload this signature covers.
_SIGNATURE_FIELD = "hash"

# Telegram's Ed25519 field, named here only so a rejected payload can say
# whether leaving it out would have made the signature check pass.
_THIRD_PARTY_FIELD = "signature"


class MiniAppAuthError(Exception):
    """A Mini App request whose sender could not be established."""


@dataclass(frozen=True)
class MiniAppIdentity:
    """The user Telegram says launched the Mini App."""

    chat_id: int
    username: str | None
    language_code: str | None


def _signing_key(bot_token: str) -> bytes:
    """Derive Telegram's Mini App signing key from the bot token."""
    return hmac.new(_SECRET_SALT, bot_token.encode("utf-8"), hashlib.sha256).digest()


def _data_check_string(fields: list[tuple[str, str]]) -> str:
    """Rebuild the exact string Telegram signed."""
    return "\n".join(f"{key}={value}" for key, value in sorted(fields) if key != _SIGNATURE_FIELD)


def verify_init_data(
    raw: str, bot_token: str | None = None, now: float | None = None
) -> MiniAppIdentity:
    """
    Establish who sent a Mini App request, or refuse to answer it.

    Args:
        raw: The verbatim ``Telegram.WebApp.initData`` query string
        bot_token: The signing token. Defaults to this bot's.
        now: Current epoch seconds, for tests.

    Returns:
        The identity Telegram vouched for

    Raises:
        MiniAppAuthError: The payload was absent, malformed, unsigned, signed
            with the wrong key, too old, or named no user
    """
    token = bot_token if bot_token is not None else settings.TELEGRAM_BOT_TOKEN
    if not token:
        # Refusing is the only safe answer: with no token there is no key, and
        # accepting unverified payloads would hand every account to whoever
        # asks first.
        raise MiniAppAuthError("no bot token to verify Mini App requests with")

    if not raw or not isinstance(raw, str):
        raise MiniAppAuthError("no initData")
    if len(raw) > settings.MINI_APP_INIT_DATA_MAX_BYTES:
        raise MiniAppAuthError("initData is too large")

    # keep_blank_values, because a field Telegram sent empty was signed empty
    # and dropping it changes the string being checked.
    fields = parse_qsl(raw, keep_blank_values=True, strict_parsing=False)
    provided = next((value for key, value in fields if key == _SIGNATURE_FIELD), None)
    if not provided:
        raise MiniAppAuthError("initData carries no hash")

    expected = hmac.new(
        _signing_key(token), _data_check_string(fields).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, provided.lower()):
        raise MiniAppAuthError(
            f"initData signature does not match ({_mismatch_detail(fields, token, provided)})"
        )

    # A signature stays valid forever, so one captured off a device - a shared
    # screen, a backup, a browser history - would be a permanent key to that
    # account. The age check turns it into a temporary one.
    auth_date = next((value for key, value in fields if key == "auth_date"), None)
    try:
        issued_at = int(auth_date or "")
    except ValueError as exc:
        raise MiniAppAuthError("initData has no usable auth_date") from exc

    age = (time.time() if now is None else now) - issued_at
    if age > settings.MINI_APP_INIT_DATA_TTL_SECONDS:
        raise MiniAppAuthError("initData has expired")
    if age < -settings.MINI_APP_CLOCK_SKEW_SECONDS:
        # Dated in the future beyond ordinary clock drift. Either a clock is
        # wrong or the value was tampered with; neither is worth acting on.
        raise MiniAppAuthError("initData is dated in the future")

    return _identity(fields)


def _mismatch_detail(fields: list[tuple[str, str]], token: str, provided: str) -> str:
    """
    Say enough about a rejected payload to tell two unrelated causes apart.

    A mismatch means the payload was signed with a different key, or over a
    different set of fields than the one rebuilt here. Those need opposite
    fixes - one is a misconfigured bot, the other is a bug in this module -
    and the log said only that they had failed, which is the same sentence
    for both.

    Field names are safe to log and their values are not: the values are the
    user's id and name, and this bot's logs are read in a public repository's
    issues. So only the names go out, plus the one derived fact that
    separates the causes.
    """
    names = ",".join(sorted(key for key, _ in fields)) or "none"
    if not any(key == _THIRD_PARTY_FIELD for key, _ in fields):
        # The observed cause, every time so far: a bot whose Mini App URL in
        # BotFather names another bot's deployment. Telegram signs with the
        # launching bot's token, this app checks with its own, and the two
        # bots are not the same one.
        return (
            f"fields={names}; signed with another bot's token - check the Mini App URL "
            f"registered in BotFather for the bot this launch came from"
        )

    # Telegram's own documentation is read both ways on whether its Ed25519
    # field belongs in the HMAC's input. If leaving it out would have
    # verified, that is the answer, and it is this module that is wrong.
    without_third_party = hmac.new(
        _signing_key(token),
        "\n".join(
            f"{key}={value}"
            for key, value in sorted(fields)
            if key not in (_SIGNATURE_FIELD, _THIRD_PARTY_FIELD)
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if hmac.compare_digest(without_third_party, provided.lower()):
        return f"fields={names}; WOULD MATCH if `signature` were excluded"
    return f"fields={names}; excluding `signature` does not help either"


def _identity(fields: list[tuple[str, str]]) -> MiniAppIdentity:
    """Read the user out of a payload whose signature already checked out."""
    raw_user = next((value for key, value in fields if key == "user"), None)
    if not raw_user:
        # Telegram omits `user` when the app is opened from an inline context
        # it cannot attribute. There is nobody to act as, so there is nothing
        # this app can do for the request.
        raise MiniAppAuthError("initData names no user")

    try:
        user = json.loads(raw_user)
    except (TypeError, ValueError) as exc:
        raise MiniAppAuthError("initData user is not readable") from exc

    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise MiniAppAuthError("initData user has no id")

    return MiniAppIdentity(
        chat_id=user["id"],
        username=user.get("username") if isinstance(user.get("username"), str) else None,
        language_code=(
            user.get("language_code") if isinstance(user.get("language_code"), str) else None
        ),
    )


class RateLimiter:
    """
    A cap on how often one caller may ask.

    Signature checking establishes who is asking; it does not stop someone
    with a valid signature from asking ten times a second. The endpoints
    behind this reach Korail and SR over the network, so an unbounded caller
    costs the railway as well as this host.

    A sliding window rather than a fixed one: a fixed window lets a caller
    spend a full quota at 11:59:59 and another at 12:00:00.
    """

    def __init__(self, limit: int, window_seconds: float):
        """
        Args:
            limit: How many requests one key may make per window
            window_seconds: How long the window is
        """
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """
        Record a request and say whether it is within the cap.

        Args:
            key: What to count against, usually the chat ID
            now: Current epoch seconds, for tests

        Returns:
            True when the request may proceed
        """
        moment = time.monotonic() if now is None else now
        with self._lock:
            cutoff = moment - self.window

            # Dropped once empty, so a caller seen once does not occupy memory
            # for the lifetime of the process.
            #
            # Swept before this caller's own deque is installed, and never
            # after. The sweep drops every key whose window is empty, and a
            # caller arriving fresh - or returning after a quiet spell - is
            # exactly such a key: sweeping once its deque was in hand deleted
            # the entry that was about to be written to, so `hits.append`
            # below landed on a deque no longer reachable from `_hits`. The
            # next request installed another empty one and lost its hit the
            # same way, and the cap stopped applying to that caller entirely.
            if len(self._hits) > self.limit:
                self._forget_idle(cutoff)

            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self.limit:
                return False

            hits.append(moment)
            return True

    def _forget_idle(self, cutoff: float) -> None:
        """Drop callers with nothing left in the window. Caller holds the lock."""
        for key in [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]
