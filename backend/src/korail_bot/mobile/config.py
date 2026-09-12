"""Explicit, isolated standalone configuration; never loads the bot's .env."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


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
        data = Path(os.environ.get("MOBILE_DATA_DIR", ".data/teum")).resolve()
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
        return cls(
            str(data / "identity.sqlite3"),
            secret,
            None if auth_only else url,
            os.environ.get("MOBILE_FCM_CREDENTIALS"),
            origins,
        )
