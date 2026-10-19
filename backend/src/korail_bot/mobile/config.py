"""Explicit, isolated standalone configuration; never loads the bot's .env."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# One size for the whole app request path: the HTTP body limit and the
# submission boundary that re-reads the conditions out of that body.
MAX_REQUEST_BYTES = 2 * 1024 * 1024

# How often a registered Korail standby (예약 대기) is looked up, and the least that may be set.
# Each look is a login and two or three reads on the person's own Korail account; asking often is
# what got accounts blocked as macros, so this stays slow on purpose.
WAITLIST_CHECK_MINUTES = 10
WAITLIST_CHECK_MIN_MINUTES = 10
# A day: past that a standby would be assigned and lost unseen between two looks.
WAITLIST_CHECK_MAX_MINUTES = 24 * 60


@dataclass(frozen=True)
class MobileConfig:
    database: str
    secret: str = field(repr=False)
    redis_url: str | None = field(default=None, repr=False)
    fcm_credentials: str | None = None
    origins: tuple[str, ...] = ("https://localhost", "capacitor://localhost")
    # Shown on the public privacy and account-deletion pages. Unset, they point
    # to in-app deletion and the operator instead.
    privacy_contact: str | None = None
    # Where agents reach this API from outside (MOBILE_PUBLIC_URL). Unset,
    # agent connections (OAuth and MCP) are switched off entirely.
    public_url: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = field(default=None, repr=False)
    waitlist_check_minutes: float = WAITLIST_CHECK_MINUTES

    @property
    def google_enabled(self):
        return bool(self.public_url and self.google_client_id and self.google_client_secret)

    @classmethod
    def from_env(cls, *, auth_only=False):
        secret_file = os.environ.get("MOBILE_SECRET_FILE")
        secret = (
            Path(secret_file).read_text(encoding="utf-8").strip()
            if secret_file
            else os.environ.get("MOBILE_SECRET", "")
        )
        if len(secret) < 32:
            raise ValueError(
                "Set MOBILE_SECRET_FILE or MOBILE_SECRET with at least 32 random characters"
            )
        url = os.environ.get("MOBILE_REDIS_URL")
        if not auth_only and (not url or urlsplit(url).scheme not in {"redis", "rediss"}):
            raise ValueError(
                "Set MOBILE_REDIS_URL to the dedicated Redis database (redis:// or rediss://)"
            )
        data = Path(os.environ.get("MOBILE_DATA_DIR", ".data/jari")).resolve()
        legacy = data.with_name("teum")
        if "MOBILE_DATA_DIR" not in os.environ and not data.exists() and legacy.is_dir():
            legacy.rename(data)  # the directory this data lived in before the app was renamed
        origins = tuple(
            filter(
                None,
                os.environ.get("MOBILE_ORIGINS", "https://localhost,capacitor://localhost").split(
                    ","
                ),
            )
        )
        if "*" in origins:
            raise ValueError("MOBILE_ORIGINS must list explicit origins")
        fcm = os.environ.get("MOBILE_FCM_CREDENTIALS") or None
        # compose mounts the Firebase key as a secret and refuses to start
        # without the file, so init-backend.ps1 leaves an empty one until a
        # real key exists. Empty means "no push", not a key to load.
        if fcm and Path(fcm).is_file() and Path(fcm).stat().st_size == 0:
            fcm = None
        google_client_id = os.environ.get("MOBILE_GOOGLE_CLIENT_ID", "").strip() or None
        google_client_secret = os.environ.get("MOBILE_GOOGLE_CLIENT_SECRET", "").strip() or None
        external_url = public_url(os.environ.get("MOBILE_PUBLIC_URL", ""))
        google_parts = (external_url, google_client_id, google_client_secret)
        if (google_client_id or google_client_secret) and not all(google_parts):
            raise ValueError(
                "Google login requires MOBILE_PUBLIC_URL, MOBILE_GOOGLE_CLIENT_ID, and "
                "MOBILE_GOOGLE_CLIENT_SECRET together"
            )
        raw_minutes = os.environ.get("MOBILE_WAITLIST_CHECK_MINUTES", "").strip()
        try:
            waitlist_minutes = float(raw_minutes) if raw_minutes else WAITLIST_CHECK_MINUTES
        except ValueError:
            waitlist_minutes = -1.0
        if not (
            math.isfinite(waitlist_minutes)
            and WAITLIST_CHECK_MIN_MINUTES <= waitlist_minutes <= WAITLIST_CHECK_MAX_MINUTES
        ):
            raise ValueError(
                f"MOBILE_WAITLIST_CHECK_MINUTES must be at least {WAITLIST_CHECK_MIN_MINUTES} "
                f"and at most {WAITLIST_CHECK_MAX_MINUTES}"
            )
        return cls(
            str(data / "identity.sqlite3"),
            secret,
            None if auth_only else url,
            fcm,
            origins,
            os.environ.get("MOBILE_PRIVACY_CONTACT", "").strip() or None,
            external_url,
            google_client_id,
            google_client_secret,
            waitlist_minutes,
        )


def public_url(raw):
    """The issuer agents see: https, or plain http to this machine for local tests."""
    raw = raw.strip().rstrip("/")
    if not raw:
        return None
    parts = urlsplit(raw)
    local = parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}
    # The OAuth metadata lives at the root, so a path would put it out of reach.
    if (
        (parts.scheme != "https" and not local)
        or not parts.hostname
        or any((parts.path, parts.query, parts.fragment, parts.username))
    ):
        raise ValueError("MOBILE_PUBLIC_URL must be an https origin such as https://jari.example")
    return raw
