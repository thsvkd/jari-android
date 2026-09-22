"""All shared railway records use a mobile-only key namespace and key."""

import redis

from korail_bot.storage.redis import RedisStorage
from korail_bot.utils.crypto import SecretBox


class NamespacedRedis:
    PREFIX = "jari:mobile:v1:"
    # The namespace this data lived under before the app was renamed.
    LEGACY_PREFIX = "teum:mobile:v1:"
    SINGLE_KEY = frozenset(
        {"get", "set", "expire", "incr", "ttl", "sadd", "sismember", "smembers", "srem"}
    )

    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        if name not in self.SINGLE_KEY:
            raise AttributeError(name)

        def call(key, *args, **kwargs):
            return getattr(self.client, name)(self.PREFIX + key, *args, **kwargs)

        return call

    def adopt_legacy_keys(self):
        """Move keys left under the old namespace into this one. Keeps TTLs; never overwrites."""
        moved = 0
        for key in self.client.scan_iter(match=self.LEGACY_PREFIX + "*", count=100):
            if self.client.renamenx(key, self.PREFIX + key.removeprefix(self.LEGACY_PREFIX)):
                moved += 1
        return moved

    def scan_iter(self, match="*", count=100):
        for key in self.client.scan_iter(match=self.PREFIX + match, count=count):
            yield key.removeprefix(self.PREFIX)

    def delete(self, *keys):
        return self.client.delete(*(self.PREFIX + key for key in keys)) if keys else 0

    def exists(self, *keys):
        return self.client.exists(*(self.PREFIX + key for key in keys))

    def ping(self):
        return self.client.ping()

    def dbsize(self):
        return sum(1 for _ in self.scan_iter())

    def flushdb(self):
        return self.delete(*list(self.scan_iter()))

    def close(self):
        self.client.close()

    def _lease(self, owner, ttl=None):
        key = self.PREFIX + "runtime_owner"
        with self.client.pipeline() as transaction:
            try:
                transaction.watch(key)
                if transaction.get(key) != owner:
                    return False
                transaction.multi()
                if ttl is None:
                    transaction.delete(key)
                else:
                    transaction.expire(key, ttl)
                transaction.execute()
                return True
            except redis.WatchError:
                return False

    def renew_lease(self, owner, ttl):
        return self._lease(owner, ttl)

    def release_lease(self, owner):
        return self._lease(owner)


class MobileStorage(RedisStorage):
    def __init__(self, *, secret, url=None, client=None):
        if len(secret) < 32:
            raise ValueError("MOBILE_SECRET must contain at least 32 characters")
        if client is None and not url:
            raise ValueError("MOBILE_REDIS_URL must be explicitly configured")
        self.box = SecretBox(secret)
        self.redis = NamespacedRedis(
            client
            if client is not None
            else redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=5,
            )
        )
        self.redis.ping()

    def _secret_box(self):
        return self.box

    def is_developer(self, chat_id):
        return False

    def get_all_developers(self):
        return []

    def _serialize_user_session(self, session):
        data = super()._serialize_user_session(session)
        if data.get("credentials"):
            data["credentials"]["korail_id"] = self.box.encrypt(data["credentials"]["korail_id"])
        return data

    def _deserialize_user_session(self, data):
        if data.get("credentials"):
            data["credentials"]["korail_id"] = (
                self.box.decrypt(data["credentials"]["korail_id"]) or ""
            )
        return super()._deserialize_user_session(data)

    def _serialize_running_reservation(self, reservation):
        data = super()._serialize_running_reservation(reservation)
        data["korail_id"] = self.box.encrypt(data["korail_id"])
        return data

    def _deserialize_running_reservation(self, data):
        data["korail_id"] = self.box.decrypt(data["korail_id"]) or ""
        return super()._deserialize_running_reservation(data)

    def _serialize_scheduled_search(self, search):
        data = super()._serialize_scheduled_search(search)
        data["korail_id"] = self.box.encrypt(data["korail_id"])
        return data

    def _deserialize_scheduled_search(self, data):
        data["korail_id"] = self.box.decrypt(data["korail_id"]) or ""
        return super()._deserialize_scheduled_search(data)

    def _serialize_dead_search(self, search):
        data = super()._serialize_dead_search(search)
        data["korail_id"] = self.box.encrypt(data["korail_id"])
        return data

    def _deserialize_dead_search(self, data):
        data["korail_id"] = self.box.decrypt(data["korail_id"]) or ""
        return super()._deserialize_dead_search(data)
