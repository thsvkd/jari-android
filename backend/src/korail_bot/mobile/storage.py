"""All shared railway records use a mobile-only key namespace and key."""

import hashlib
import hmac
import json
import secrets
import time

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from korail_bot.storage.redis import RedisStorage
from korail_bot.utils.crypto import SecretBox

# Redis runs next to the app and answers a lease call in milliseconds, so this
# much silence is a stall. Failing then costs nothing: the next renewal tries again.
LEASE_SOCKET_TIMEOUT = 2
LEASE_CONNECT_TIMEOUT = 2


def lease_client_for(
    url, *, socket_timeout=LEASE_SOCKET_TIMEOUT, socket_connect_timeout=LEASE_CONNECT_TIMEOUT
):
    """
    A client for the runtime lease alone: every call is one attempt.

    A retrying client stretches one call against a stalled Redis over many
    timeouts - redis.Redis() defaults to 10 retries, about a minute at 5 s
    each - and until the call returns the runtime cannot tell whether its
    lease still holds. Failing fast leaves time for another renewal before
    the runtime stops waiting at lease_until (see LEASE_MARGIN in
    runtime.py). redis-py 8 does not retry for a client from from_url, but
    that is a default, and it differs between constructors; here it is set.
    """
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
        retry=Retry(NoBackoff(), 0),
    )


class NamespacedRedis:
    PREFIX = "jari:mobile:v1:"
    # The namespace this data lived under before the app was renamed.
    LEGACY_PREFIX = "teum:mobile:v1:"
    SINGLE_KEY = frozenset(
        {
            "get",
            "set",
            "expire",
            "incr",
            "ttl",
            "sadd",
            "sismember",
            "smembers",
            "srem",
            "lpush",
            "ltrim",
            "lrange",
        }
    )

    def __init__(self, client, lease_client=None):
        self.client = client
        # Every lease call goes through this one (see lease_client_for). A
        # caller that hands in a single client, as the tests do, gets it for both.
        self.lease_client = client if lease_client is None else lease_client

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
        try:
            self.client.close()
        finally:
            if self.lease_client is not self.client:
                self.lease_client.close()

    LEASE_KEY = "runtime_owner"
    LEASE_ATTEMPTS = 3

    def acquire_lease(self, owner, ttl):
        """True if this SET NX took the lease; None if the key was already there."""
        return self.lease_client.set(self.PREFIX + self.LEASE_KEY, owner, nx=True, ex=ttl)

    def lease_ttl(self):
        return self.lease_client.ttl(self.PREFIX + self.LEASE_KEY)

    def _lease(self, owner, ttl=None):
        """
        True if `owner` held the lease and it was renewed (ttl) or released.

        False only once Redis has answered that another owner, or none, holds
        it. A WatchError is not that answer: redis-py also raises it when the
        connection fails while watching - a TimeoutError during a host stall
        becomes one - so the check is made again. One that keeps coming back
        is raised, for the caller to treat as a failed pass.
        """
        key = self.PREFIX + self.LEASE_KEY
        for attempt in range(1, self.LEASE_ATTEMPTS + 1):
            with self.lease_client.pipeline() as transaction:
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
                    if attempt == self.LEASE_ATTEMPTS:
                        raise
        raise AssertionError("unreachable")

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
        self._account_tag_key = None
        if client is not None:
            self.redis = NamespacedRedis(client)
        else:
            self.redis = NamespacedRedis(
                redis.Redis.from_url(
                    url,
                    decode_responses=True,
                    socket_connect_timeout=3,
                    socket_timeout=5,
                ),
                # Connects on first use; only the runtime ever takes a lease.
                lease_client_for(url),
            )
        self.redis.ping()

    def _secret_box(self):
        return self.box

    # Kept when an account is deleted: no personal data (why and when a search
    # ended, and the worker's PID), it expires within a week, and a deploy
    # check under way reads it to tell a search that ended from one it lost.
    KEPT_ON_DELETE = ("search_ended",)

    def delete_user_data(self, chat_id):
        """
        Every record this storage keeps for one owner. Each key names its owner
        as a whole segment (`name:<id>` or `name:<id>:<more>`), so a scan for
        that segment finds them all, including kinds added after this was written.
        """
        keys = {
            key
            for pattern in (f"*:{chat_id}", f"*:{chat_id}:*")
            for key in self.redis.scan_iter(match=pattern, count=100)
        }
        keys -= {f"{kind}:{chat_id}" for kind in self.KEPT_ON_DELETE}
        self.redis.delete(*keys)
        return len(keys)

    # Korail sells 60 days ahead; a standby record is kept a little past that at most. Finished
    # ones are pruned well before (MobileGateway.check_waitlists).
    WAITLIST_TTL = 70 * 24 * 3600

    def get_waitlists(self, chat_id):
        """The Korail standbys this owner applied for through the app, oldest first."""
        raw = self.redis.get(f"waitlist:{chat_id}")
        try:
            entries = json.loads(raw) if raw else []
        except ValueError:
            return []
        return (
            [entry for entry in entries if isinstance(entry, dict)]
            if isinstance(entries, list)
            else []
        )

    def save_waitlists(self, chat_id, entries):
        key = f"waitlist:{chat_id}"
        if entries:
            self.redis.set(key, json.dumps(entries, ensure_ascii=False), ex=self.WAITLIST_TTL)
        else:
            self.redis.delete(key)

    TAG_KEY = "waitlist_tag_key"

    def account_tag(self, value):
        """
        Which Korail account a standby was applied for with, without keeping its number: a keyed
        hash, so a member number cannot be read back by trying them all. The key is random and
        kept beside the standbys in Redis, not derived from MOBILE_SECRET, so rotating the secret
        does not make every standby look like another account's.
        """
        if self._account_tag_key is None:
            stored = self.redis.get(self.TAG_KEY)
            if not stored:
                self.redis.set(self.TAG_KEY, secrets.token_hex(32), nx=True)
                stored = self.redis.get(self.TAG_KEY)
            self._account_tag_key = bytes.fromhex(stored)
        return hmac.new(self._account_tag_key, value.encode(), hashlib.sha256).hexdigest()[:32]

    def get_waitlist_link(self, chat_id):
        """Which member tag the linked login was last seen to belong to ({login, member})."""
        raw = self.redis.get(f"waitlist_link:{chat_id}")
        try:
            link = json.loads(raw) if raw else None
        except ValueError:
            return None
        return link if isinstance(link, dict) else None

    def save_waitlist_link(self, chat_id, link):
        self.redis.set(f"waitlist_link:{chat_id}", json.dumps(link), ex=self.WAITLIST_TTL)

    def mark_waitlist_connected(self, chat_id):
        """When the Korail account was last connected (register): after a block, a fresh start."""
        self.redis.set(f"waitlist_connected:{chat_id}", str(time.time()), ex=self.WAITLIST_TTL)

    def get_waitlist_connected(self, chat_id):
        raw = self.redis.get(f"waitlist_connected:{chat_id}")
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    def waitlist_owners(self):
        owners = []
        for key in self.redis.scan_iter(match="waitlist:*", count=100):
            owner = key.split(":", 1)[1]
            if owner.lstrip("-").isdigit():
                owners.append(int(owner))
        return owners

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
