"""Storage implementations for application state."""

from korail_bot.storage.base import StorageInterface
from korail_bot.storage.redis import RedisStorage

__all__ = [
    "RedisStorage",
    "StorageInterface",
]
