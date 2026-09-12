"""
Encryption helpers for secrets that have to be persisted.

Korail credentials are supplied by users over Telegram and must survive a
few conversation steps in Redis. They are stored encrypted so that read
access to Redis (a dump, a snapshot, an exposed port) does not hand over
plaintext passwords.

The `cryptography` package is a declared dependency in pyproject.toml. It
used to arrive only as a transitive dependency of pyopenssl, which nothing
in this codebase imports.
"""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from korail_bot.config.settings import settings
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)

# Marks values produced by this module. Anything without the prefix is
# treated as unreadable rather than as a plaintext fallback.
_PREFIX = "v1:"

# Fixed salt: the key must be reproducible across restarts, and SESSION_SECRET
# is expected to be high-entropy key material rather than a human password.
_KDF_SALT = b"korailbot.session.v1"
_KDF_ITERATIONS = 200_000


class SecretBox:
    """Symmetric encryption for short secrets."""

    def __init__(self, secret: str | None = None):
        """
        Build a secret box.

        Args:
            secret: Key material. Defaults to settings.SESSION_SECRET.
                    When absent, an ephemeral key is generated, which means
                    stored secrets cannot be read back after a restart.
        """
        secret = secret if secret is not None else settings.SESSION_SECRET

        if secret:
            self._ephemeral = False
            key = hashlib.pbkdf2_hmac(
                "sha256", secret.encode("utf-8"), _KDF_SALT, _KDF_ITERATIONS, dklen=32
            )
            self._fernet = Fernet(base64.urlsafe_b64encode(key))
        else:
            self._ephemeral = True
            self._fernet = Fernet(Fernet.generate_key())
            logger.warning(
                "SESSION_SECRET is not set - using an ephemeral encryption key. "
                "Stored credentials will not be readable after a restart."
            )

    @property
    def is_ephemeral(self) -> bool:
        """True when the key is not derived from configured key material."""
        return self._ephemeral

    def encrypt(self, plaintext: str | None) -> str | None:
        """
        Encrypt a secret for storage.

        Args:
            plaintext: Value to protect. Empty values are passed through.

        Returns:
            Prefixed ciphertext, or the original value when there is nothing
            to encrypt.
        """
        if not plaintext:
            return plaintext

        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return f"{_PREFIX}{token}"

    def decrypt(self, ciphertext: str | None) -> str | None:
        """
        Decrypt a stored secret.

        Args:
            ciphertext: Value produced by encrypt().

        Returns:
            The plaintext, or None when the value cannot be read (wrong key,
            tampered payload, or a legacy plaintext record). Callers treat
            None as "no credentials" and ask the user to enter them again.
        """
        if not ciphertext:
            return None

        if not ciphertext.startswith(_PREFIX):
            logger.warning(
                "Encountered a stored secret without an encryption marker - "
                "discarding it and requiring re-entry."
            )
            return None

        try:
            return self._fernet.decrypt(ciphertext[len(_PREFIX) :].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            logger.warning(
                "Stored secret could not be decrypted (key rotated or data "
                "tampered with) - requiring re-entry."
            )
            return None


_secret_box: SecretBox | None = None


def get_secret_box() -> SecretBox:
    """Get the process-wide secret box."""
    global _secret_box
    if _secret_box is None:
        _secret_box = SecretBox()
    return _secret_box


def identity_hash(value: str) -> str:
    """
    A stable, non-reversible identifier for a phone number.

    Trial counts and approvals hang off the Korail phone number rather than
    the chat, because a new Telegram account is free and a new Korail account
    is not. That means keeping the number - and a Redis dump full of readable
    phone numbers is exactly the thing not to leave lying around.

    Only equality is ever needed, so the number is never stored: this keys on
    an HMAC of it. Encryption would be the wrong tool, since it would mean the
    plaintext is recoverable by design.

    Keyed with SESSION_SECRET, so the hashes are useless to anyone who reads
    the database without also having the key - unkeyed digests of a
    ten-digit number would fall to a lookup table in seconds.

    Rotating SESSION_SECRET orphans every existing hash. That means trial
    counts reset and approvals have to be granted again, which is the same
    consequence rotation already has for stored credentials.
    """
    key = (settings.SESSION_SECRET or "").encode("utf-8")
    digits = "".join(character for character in value if character.isdigit())
    return hmac.new(key, digits.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
