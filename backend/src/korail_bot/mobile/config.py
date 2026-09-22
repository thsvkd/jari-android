"""Explicit, isolated standalone configuration; never loads the bot's .env."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# One size for the whole app request path: the HTTP body limit and the
# submission boundary that re-reads the conditions out of that body.
MAX_REQUEST_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class MobileConfig:
    database: str
    secret: str = field(repr=False)
    redis_url: str | None = field(default=None, repr=False)
    fcm_credentials: str | None = None
    origins: tuple[str, ...] = ("https://localhost", "capacitor://localhost")

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
        return cls(
            str(data / "identity.sqlite3"),
            secret,
            None if auth_only else url,
            fcm,
            origins,
        )
