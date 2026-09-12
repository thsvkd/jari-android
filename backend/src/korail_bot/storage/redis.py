"""Redis storage implementation."""

import json
import time
from datetime import datetime

import redis

from korail_bot.config.settings import settings
from korail_bot.models import (
    AccessRequest,
    ApprovedUser,
    DeadSearch,
    DeathCause,
    FavouriteSearch,
    MultiReservationStatus,
    OnboardedAccount,
    PaymentStatus,
    RunningReservation,
    ScheduledSearch,
    TrainSearchParams,
    UserCredentials,
    UserSession,
)
from korail_bot.storage.base import StorageInterface
from korail_bot.utils.crypto import get_secret_box
from korail_bot.utils.logger import get_logger
from korail_bot.utils.timezone import as_utc

logger = get_logger(__name__)


class RedisStorage(StorageInterface):
    """
    Redis-based storage implementation.

    Provides persistent, process-shared state management using Redis.
    All data survives application restarts and is accessible across processes.
    """

    def _secret_box(self):
        """Encryption scope; independent runtimes may supply their own key."""
        return get_secret_box()

    def __init__(self):
        """Initialize Redis connection pool."""
        try:
            self.redis = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD,
                decode_responses=settings.REDIS_DECODE_RESPONSES,
                socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
                socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT,
                # No retry_on_timeout: redis-py deprecated it because the
                # default Retry already lists TimeoutError among the errors it
                # retries, so passing it changed nothing but the warning count.
                max_connections=settings.REDIS_MAX_CONNECTIONS,
            )
            # Test connection
            self.redis.ping()
            logger.info(f"Redis connected: {settings.REDIS_HOST}:{settings.REDIS_PORT}")
        except redis.RedisError as e:
            logger.error(f"Redis connection failed: {e}")
            raise

    def close(self) -> None:
        """
        Hand back the connection pool's sockets.

        The bot builds one storage per process and keeps it for as long as the
        process lives, so nothing in production has to call this. Tests build
        one per test, and a client nobody closes keeps its socket until the
        garbage collector reaches it - at a moment nobody chose, in whatever
        test happens to be running then.
        """
        self.redis.close()

    @staticmethod
    def _text(value) -> str | None:
        """
        A stored value as text, or None when there was none.

        REDIS_DECODE_RESPONSES is pinned True, so every read is already a
        string - but redis-py is typed for both, and the type checker has no
        way to know which one this client was built with.
        """
        if value is None:
            return None
        return value if isinstance(value, str) else value.decode()

    def _scan_keys(self, pattern: str) -> list[str]:
        """
        Every key matching a pattern, without blocking the server.

        KEYS walks the whole keyspace in one shot and Redis is single
        threaded, so for the duration nothing else - including the search
        process asking whether it should still be running - is served. SCAN
        does the same walk in small batches and lets other commands through
        in between.

        The cursor may hand back a key more than once, which KEYS never did,
        so the results are deduplicated. Order is not guaranteed by either.
        """
        return list(dict.fromkeys(self.redis.scan_iter(match=pattern, count=100)))

    # ==================== User Session Management ====================

    def get_user_session(self, chat_id: int) -> UserSession | None:
        """Get user session by chat ID."""
        key = f"user_session:{chat_id}"
        data = self.redis.get(key)
        if not data:
            return None

        try:
            session_dict = json.loads(data)
            return self._deserialize_user_session(session_dict)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to deserialize user session: {e}")
            return None

    def save_user_session(self, session: UserSession) -> None:
        """
        Save or update user session.

        Sessions carry credentials, so they expire instead of living forever.
        The TTL is refreshed on every save, i.e. it counts from last activity.
        """
        key = f"user_session:{session.chat_id}"
        data = json.dumps(self._serialize_user_session(session))
        self.redis.set(key, data, ex=settings.SESSION_TTL_SECONDS)
        logger.debug(f"Saved user session for chat_id={session.chat_id}")

    def delete_user_session(self, chat_id: int) -> None:
        """Delete user session."""
        key = f"user_session:{chat_id}"
        self.redis.delete(key)

    def get_all_user_sessions(self) -> list[UserSession]:
        """Get all user sessions."""
        keys = self._scan_keys("user_session:*")
        sessions = []
        for key in keys:
            data = self.redis.get(key)
            if data:
                try:
                    session_dict = json.loads(data)
                    sessions.append(self._deserialize_user_session(session_dict))
                except (json.JSONDecodeError, KeyError):
                    continue
        return sessions

    # ==================== Running Reservation Management ====================

    def get_running_reservation(self, chat_id: int) -> RunningReservation | None:
        """Get running reservation by chat ID."""
        key = f"running_reservation:{chat_id}"
        data = self.redis.get(key)
        if not data:
            return None

        try:
            res_dict = json.loads(data)
            return self._deserialize_running_reservation(res_dict)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to deserialize running reservation: {e}")
            return None

    def save_running_reservation(self, reservation: RunningReservation) -> None:
        """Save running reservation."""
        key = f"running_reservation:{reservation.chat_id}"
        data = json.dumps(self._serialize_running_reservation(reservation))
        self.redis.set(key, data)

    # ==================== Scheduled searches ====================

    def get_scheduled_search(self, chat_id: int) -> ScheduledSearch | None:
        """Get the search waiting to start for a chat ID."""
        data = self.redis.get(f"scheduled_search:{chat_id}")
        if not data:
            return None
        return self._deserialize_scheduled_search(json.loads(data))

    def save_scheduled_search(self, search: ScheduledSearch) -> None:
        """
        Save a search to be started later.

        Given a lifetime that outlasts its start time but not by much. The
        record is deleted when the search actually starts; the expiry is for
        the one that never does, because the app was down at the moment it
        came due and nobody is going to want it three days later.
        """
        key = f"scheduled_search:{search.chat_id}"
        data = json.dumps(self._serialize_scheduled_search(search))
        ttl = int(search.seconds_until_due()) + settings.SCHEDULE_GRACE_SECONDS
        self.redis.set(key, data, ex=max(ttl, settings.SCHEDULE_GRACE_SECONDS))

    def delete_scheduled_search(self, chat_id: int) -> None:
        """Forget a search that was waiting to start."""
        self.redis.delete(f"scheduled_search:{chat_id}")

    def get_all_scheduled_searches(self) -> list[ScheduledSearch]:
        """Every search waiting to start."""
        searches = []
        for key in self._scan_keys("scheduled_search:*"):
            data = self.redis.get(key)
            if data:
                searches.append(self._deserialize_scheduled_search(json.loads(data)))
        return searches

    def _serialize_scheduled_search(self, search: ScheduledSearch) -> dict:
        """Serialize ScheduledSearch to dict."""
        from korail_bot.utils.timezone import as_utc

        return {
            "schema": 2,
            "chat_id": search.chat_id,
            "korail_id": search.korail_id,
            "start_at": as_utc(search.start_at).isoformat(),
            "timezone_name": search.timezone_name,
            "created_at": as_utc(search.created_at).isoformat(),
            "search_params": self._serialize_search_params(search.search_params),
        }

    def _deserialize_scheduled_search(self, data: dict) -> ScheduledSearch:
        """Deserialize dict to ScheduledSearch."""
        from korail_bot.utils.timezone import RAIL_TIMEZONE, as_utc, timezone_of

        timezone_name = data.get("timezone_name") or "Asia/Seoul"
        start_at = datetime.fromisoformat(data["start_at"])
        created_at = datetime.fromisoformat(data["created_at"])
        # v1 wrote the user's wall-clock selection without a zone. Existing
        # records came from Korean railway users, so treating those values as
        # Korea time repairs the nine-hour Docker shift instead of preserving
        # it forever. v2 is always aware UTC.
        legacy_zone = RAIL_TIMEZONE if not data.get("schema") else timezone_of(timezone_name)
        return ScheduledSearch(
            chat_id=data["chat_id"],
            korail_id=data["korail_id"],
            start_at=as_utc(start_at, naive_zone=legacy_zone),
            timezone_name=timezone_name,
            # The old app container itself ran in UTC. Only start_at was a
            # user-entered Korean wall clock; created_at came from the server.
            created_at=as_utc(created_at),
            search_params=self._deserialize_search_params(data["search_params"]),
        )

    # ==================== Searches that died ====================

    def get_dead_search(self, chat_id: int) -> DeadSearch | None:
        """Get the stopped search a chat has yet to deal with."""
        data = self.redis.get(f"dead_search:{chat_id}")
        if not data:
            return None
        return self._deserialize_dead_search(json.loads(data))

    def save_dead_search(self, search: DeadSearch) -> None:
        """
        Keep a stopped search so the user can resume or discard it.

        Expires with the login that resuming needs: past that the buttons
        offered with it would have nothing to act on.
        """
        key = f"dead_search:{search.chat_id}"
        data = json.dumps(self._serialize_dead_search(search))
        self.redis.set(key, data, ex=settings.DEAD_SEARCH_TTL_SECONDS)

    def delete_dead_search(self, chat_id: int) -> None:
        """Forget a stopped search."""
        self.redis.delete(f"dead_search:{chat_id}")

    def _serialize_dead_search(self, search: DeadSearch) -> dict:
        """Serialize DeadSearch to dict."""
        from korail_bot.utils.timezone import as_utc

        return {
            "chat_id": search.chat_id,
            "korail_id": search.korail_id,
            "cause": search.cause.value,
            "resumable": search.resumable,
            "died_at": as_utc(search.died_at).isoformat(),
            "search_params": self._serialize_search_params(search.search_params),
        }

    def _deserialize_dead_search(self, data: dict) -> DeadSearch:
        """Deserialize dict to DeadSearch."""
        from korail_bot.utils.timezone import as_utc

        return DeadSearch(
            chat_id=data["chat_id"],
            korail_id=data["korail_id"],
            cause=DeathCause(data["cause"]),
            resumable=data.get("resumable", True),
            died_at=as_utc(datetime.fromisoformat(data["died_at"])),
            search_params=self._deserialize_search_params(data["search_params"]),
        )

    def delete_running_reservation(self, chat_id: int) -> None:
        """Delete running reservation."""
        key = f"running_reservation:{chat_id}"
        self.redis.delete(key)

    def get_all_running_reservations(self) -> list[RunningReservation]:
        """Get all running reservations."""
        keys = self._scan_keys("running_reservation:*")
        reservations = []
        for key in keys:
            data = self.redis.get(key)
            if data:
                try:
                    res_dict = json.loads(data)
                    reservations.append(self._deserialize_running_reservation(res_dict))
                except (json.JSONDecodeError, KeyError):
                    continue
        return reservations

    # ==================== Onboarded accounts ====================

    #: Where SR registrations were kept while it was a separate railway. Read
    #: only to find their owners and tell them the account is dead - see
    #: get_retired_srt_chat_ids.
    RETIRED_SRT_KEY_PREFIX = "user_credentials:srt:"

    @staticmethod
    def _credentials_key(chat_id: int) -> str:
        """Where a chat's registration lives."""
        return f"user_credentials:{chat_id}"

    def save_onboarded_account(self, account: OnboardedAccount) -> None:
        """
        Store the railway account a chat registered, encrypted.

        Unlike the resume credentials, this outlives the booking it was
        entered for - that is what registering once buys. It is kept in a key
        of its own rather than on the session, because the session is reset at
        the end of every flow and would take the registration with it.

        """
        key = self._credentials_key(account.chat_id)
        box = self._secret_box()
        data = json.dumps(
            {
                "korail_id": box.encrypt(account.korail_id),
                "korail_pw": box.encrypt(account.korail_pw),
                "onboarded_at": account.onboarded_at.isoformat(),
            }
        )
        self.redis.set(key, data, ex=settings.CREDENTIAL_TTL_SECONDS)
        logger.debug(f"Saved account for chat_id={account.chat_id}")

    def get_onboarded_account(self, chat_id: int) -> OnboardedAccount | None:
        """
        Get the account a chat registered.

        Args:
            chat_id: Telegram chat ID

        Returns:
            The account, or None when absent or undecryptable. The latter
            happens when SESSION_SECRET changed, and the caller then treats
            the chat as not registered - which is the truth, since nothing
            can log in with what cannot be read.
        """
        data = self.redis.get(self._credentials_key(chat_id))
        if not data:
            return None

        try:
            stored = json.loads(data)
            box = self._secret_box()
            korail_id = box.decrypt(stored["korail_id"])
            korail_pw = box.decrypt(stored["korail_pw"])
            if korail_id is None or korail_pw is None:
                # What the docstring above promises. decrypt() has already
                # logged why it could not read the value; handing back a
                # record with a None inside it would give every caller an
                # account that cannot log in and does not admit it.
                logger.error(f"Could not decrypt the onboarded account for chat_id={chat_id}")
                return None
            return OnboardedAccount(
                chat_id=chat_id,
                korail_id=korail_id,
                korail_pw=korail_pw,
                onboarded_at=as_utc(datetime.fromisoformat(stored["onboarded_at"])),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Could not read the onboarded account for chat_id={chat_id}: {e}")
            return None

    def delete_onboarded_account(self, chat_id: int) -> None:
        """
        Forget a registered account.

        The dead SR registration goes with it. Logging out and being blocked
        both mean "let go of my credentials", and leaving the retired ones
        behind would be keeping some of them.

        Args:
            chat_id: Telegram chat ID
        """
        self.redis.delete(self._credentials_key(chat_id), f"{self.RETIRED_SRT_KEY_PREFIX}{chat_id}")
        logger.debug(f"Deleted onboarded account for chat_id={chat_id}")

    def get_retired_srt_chat_ids(self) -> list[int]:
        """
        Every chat still holding an SR registration.

        SR sold the 수서 line as a separate company until 2026-09-01, and a
        chat could register with it. Those keys outlived the company. They are
        never logged in with - nothing can - and this exists only so their
        owners can be told why the bot has stopped booking for them.
        """
        chat_ids = []
        for key in self._scan_keys(f"{self.RETIRED_SRT_KEY_PREFIX}*"):
            try:
                chat_ids.append(int(key.rsplit(":", 1)[-1]))
            except ValueError:
                logger.warning(f"Skipping a retired SR key that carries no chat id: {key}")
        return chat_ids

    def delete_retired_srt_account(self, chat_id: int) -> None:
        """Drop one chat's dead SR registration."""
        self.redis.delete(f"{self.RETIRED_SRT_KEY_PREFIX}{chat_id}")
        logger.info(f"Dropped the retired SR registration for chat_id={chat_id}")

    def get_all_onboarded_chat_ids(self) -> list[int]:
        """
        Every chat that has registered with either railway.

        Read off the keys rather than by decrypting each record: this exists
        to address people, and a registration whose SESSION_SECRET has moved
        on is still a person who uses the bot. Deduplicated, because someone
        registered with both railways is still one person to write to.
        """
        chat_ids = []
        for key in self._scan_keys("user_credentials:*"):
            try:
                chat_id = int(key.rpartition(":")[2])
            except ValueError:
                logger.warning(f"Skipping a credentials key with no chat id in it: {key!r}")
                continue
            if chat_id not in chat_ids:
                chat_ids.append(chat_id)
        return chat_ids

    # ==================== Favourite searches ====================
    #
    # Kept per chat under a key each, so listing is a scan over one prefix and
    # deleting one costs nothing. No expiry: a shortcut that quietly forgot
    # itself would be worse than never having been offered, and unlike the
    # registered account there is nothing secret in here to age out - a
    # favourite is two station names, a time window and a seat preference.

    def save_favourite(self, favourite: FavouriteSearch) -> None:
        """Store a favourite, replacing one with the same id."""
        key = f"favourite:{favourite.chat_id}:{favourite.fav_id}"
        self.redis.set(
            key,
            json.dumps(
                {
                    "name": favourite.name,
                    "src_locate": favourite.src_locate,
                    "dst_locate": favourite.dst_locate,
                    "dep_time": favourite.dep_time,
                    "max_dep_time": favourite.max_dep_time,
                    "train_type": favourite.train_type,
                    "train_type_display": favourite.train_type_display,
                    "special_option": favourite.special_option,
                    "special_option_display": favourite.special_option_display,
                    "passenger_count": favourite.passenger_count,
                    "seat_strategy": favourite.seat_strategy,
                    "seat_strategy_display": favourite.seat_strategy_display,
                    "seat_preference": favourite.seat_preference,
                    "created_at": favourite.created_at.isoformat(),
                }
            ),
        )
        logger.debug(f"Saved favourite {favourite.fav_id} for chat_id={favourite.chat_id}")

    def get_favourite(self, chat_id: int, fav_id: str) -> FavouriteSearch | None:
        """One favourite, or None when it is not there any more."""
        data = self.redis.get(f"favourite:{chat_id}:{fav_id}")
        if not data:
            return None
        return self._deserialize_favourite(chat_id, fav_id, data)

    def get_favourites(self, chat_id: int) -> list[FavouriteSearch]:
        """
        Every favourite this chat has saved, oldest first.

        The order is the order they were saved in, which is the only order the
        user has any memory of. A scan hands keys back in no order at all, so
        it is imposed here.
        """
        favourites = []
        for key in self._scan_keys(f"favourite:{chat_id}:*"):
            data = self.redis.get(key)
            if not data:
                continue
            favourite = self._deserialize_favourite(chat_id, key.rpartition(":")[2], data)
            if favourite:
                favourites.append(favourite)

        return sorted(favourites, key=lambda favourite: favourite.created_at)

    def delete_favourite(self, chat_id: int, fav_id: str) -> bool:
        """
        Forget a favourite.

        Returns:
            True when there was one to forget, so the caller can tell a
            deletion from a second press on a button that already worked
        """
        return bool(self.redis.delete(f"favourite:{chat_id}:{fav_id}"))

    def delete_all_favourites(self, chat_id: int) -> int:
        """Forget all of a chat's favourites. Returns how many there were."""
        keys = self._scan_keys(f"favourite:{chat_id}:*")
        return int(self.redis.delete(*keys)) if keys else 0

    def set_pending_favourite_rename(self, chat_id: int, fav_id: str | None) -> None:
        """
        Note that the next message typed here is a new name for a favourite.

        Kept apart from the session rather than on it: renaming is not a step
        of the booking flow, and someone who is halfway through booking a
        ticket must not have that flow disturbed by tidying up their saved
        searches. Given a short life of its own, so a rename abandoned
        mid-thought does not swallow the next thing typed an hour later.
        """
        key = f"favourite_rename:{chat_id}"
        if fav_id is None:
            self.redis.delete(key)
        else:
            self.redis.set(key, fav_id, ex=settings.PENDING_INPUT_TTL_SECONDS)

    def get_pending_favourite_rename(self, chat_id: int) -> str | None:
        """Which favourite this chat is in the middle of renaming, if any."""
        return self._text(self.redis.get(f"favourite_rename:{chat_id}")) or None

    @staticmethod
    def _deserialize_favourite(chat_id: int, fav_id: str, data) -> FavouriteSearch | None:
        """Read one back, or None when the record cannot be understood."""
        try:
            stored = json.loads(data)
            return FavouriteSearch(
                chat_id=chat_id,
                fav_id=fav_id,
                name=stored["name"],
                src_locate=stored["src_locate"],
                dst_locate=stored["dst_locate"],
                dep_time=stored.get("dep_time", ""),
                max_dep_time=stored.get("max_dep_time", ""),
                train_type=stored.get("train_type", ""),
                train_type_display=stored.get("train_type_display", ""),
                special_option=stored.get("special_option", ""),
                special_option_display=stored.get("special_option_display", ""),
                passenger_count=int(stored.get("passenger_count", 1)),
                seat_strategy=stored.get("seat_strategy", "consecutive"),
                seat_preference=stored.get("seat_preference", ""),
                seat_strategy_display=stored.get("seat_strategy_display", ""),
                created_at=as_utc(datetime.fromisoformat(stored["created_at"])),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error(f"Could not read favourite {fav_id} for chat_id={chat_id}: {e}")
            return None

    # ==================== Trials, requests and approvals ====================

    def get_trial_count(self, phone_hash: str) -> int:
        """How many trial searches this number has used."""
        value = self.redis.get(f"trial:{phone_hash}")
        try:
            return int(value) if value else 0
        except (TypeError, ValueError):
            return 0

    def increment_trial_count(self, phone_hash: str) -> int:
        """
        Record one used trial search.

        Returns:
            The new total
        """
        return int(self.redis.incr(f"trial:{phone_hash}"))

    def save_access_request(self, request: AccessRequest) -> None:
        """Record someone asking to keep using the bot."""
        key = f"access_request:{request.phone_hash}"
        data = json.dumps(
            {
                "phone_hash": request.phone_hash,
                "chat_id": request.chat_id,
                "masked_phone": request.masked_phone,
                "requested_at": request.requested_at.isoformat(),
            }
        )
        self.redis.set(key, data, ex=settings.REQUEST_TTL_SECONDS)

    def get_access_request(self, phone_hash: str) -> AccessRequest | None:
        """Get one pending request."""
        data = self.redis.get(f"access_request:{phone_hash}")
        if not data:
            return None
        return self._deserialize_access_request(json.loads(data))

    def delete_access_request(self, phone_hash: str) -> None:
        """Forget a request, answered or withdrawn."""
        self.redis.delete(f"access_request:{phone_hash}")

    def get_all_access_requests(self) -> list[AccessRequest]:
        """Every request still waiting on an answer, oldest first."""
        requests = []
        for key in self._scan_keys("access_request:*"):
            data = self.redis.get(key)
            if not data:
                continue
            try:
                requests.append(self._deserialize_access_request(json.loads(data)))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return sorted(requests, key=lambda r: r.requested_at)

    def _deserialize_access_request(self, data: dict) -> AccessRequest:
        """Deserialize dict to AccessRequest."""
        return AccessRequest(
            phone_hash=data["phone_hash"],
            chat_id=data["chat_id"],
            masked_phone=data["masked_phone"],
            requested_at=as_utc(datetime.fromisoformat(data["requested_at"])),
        )

    def save_approved_user(self, user: ApprovedUser) -> None:
        """Record an approval. No expiry: an approval is not a lease."""
        key = f"approved:{user.phone_hash}"
        data = json.dumps(
            {
                "phone_hash": user.phone_hash,
                "masked_phone": user.masked_phone,
                "approved_at": user.approved_at.isoformat(),
                "approved_by": user.approved_by,
            }
        )
        self.redis.set(key, data)

    def is_approved(self, phone_hash: str) -> bool:
        """Whether this number has been approved."""
        return bool(self.redis.exists(f"approved:{phone_hash}"))

    def delete_approved_user(self, phone_hash: str) -> None:
        """Withdraw an approval."""
        self.redis.delete(f"approved:{phone_hash}")

    def get_all_approved_users(self) -> list[ApprovedUser]:
        """Everyone approved from the chat, most recent first."""
        users = []
        for key in self._scan_keys("approved:*"):
            data = self.redis.get(key)
            if not data:
                continue
            try:
                stored = json.loads(data)
                users.append(
                    ApprovedUser(
                        phone_hash=stored["phone_hash"],
                        masked_phone=stored["masked_phone"],
                        approved_at=as_utc(datetime.fromisoformat(stored["approved_at"])),
                        approved_by=stored.get("approved_by", 0),
                    )
                )
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return sorted(users, key=lambda u: u.approved_at, reverse=True)

    # ==================== Developer chats ====================

    def is_developer(self, chat_id: int) -> bool:
        """Whether this chat is in developer mode."""
        return bool(self.redis.sismember("developers", str(chat_id)))

    def set_developer(self, chat_id: int, enabled: bool = True) -> None:
        """
        Turn developer mode on or off for a chat.

        Kept in a set rather than on the session, because it has to outlive
        every reset the booking flow performs - an operator does not expect
        to lose their tools by finishing a booking.
        """
        if enabled:
            self.redis.sadd("developers", str(chat_id))
        else:
            self.redis.srem("developers", str(chat_id))

    def get_all_developers(self) -> list[int]:
        """Every chat in developer mode."""
        return [int(value) for value in self.redis.smembers("developers")]

    # ==================== Progress report preference ====================
    #
    # A preference, not session state: it is set once and expected to hold for
    # every search after that, so it lives in a key of its own rather than on
    # the session the booking flow resets when it ends. No expiry for the same
    # reason - a preference that quietly forgot itself would be worse than one
    # that was never offered.

    def get_progress_report_minutes(self, chat_id: int) -> int:
        """
        How often this chat wants progress reports, in minutes.

        Returns:
            The interval, or 0 when reports are off - which is the default,
            and what an unreadable value is treated as. A malformed key must
            not turn into a search that messages the user every second.
        """
        raw = self.redis.get(f"progress_report:{chat_id}")
        if raw is None:
            return 0
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            logger.warning(f"Unreadable progress report interval for chat_id={chat_id}: {raw!r}")
            return 0

    def set_progress_report_minutes(self, chat_id: int, minutes: int) -> None:
        """Set the reporting interval, or 0 to stop reporting."""
        key = f"progress_report:{chat_id}"
        if minutes <= 0:
            self.redis.delete(key)
        else:
            self.redis.set(key, str(int(minutes)))

    # ==================== User time zone ====================

    def get_user_timezone(self, chat_id: int) -> str:
        """The user's IANA zone, with Korea as the explicit legacy default."""
        from korail_bot.utils.timezone import DEFAULT_USER_TIMEZONE, is_valid_timezone

        raw = self.redis.get(f"user_timezone:{chat_id}")
        return raw if is_valid_timezone(raw) else DEFAULT_USER_TIMEZONE

    def set_user_timezone(self, chat_id: int, timezone_name: str) -> None:
        """Remember one validated IANA zone without an expiry."""
        from korail_bot.utils.timezone import is_valid_timezone

        if not is_valid_timezone(timezone_name):
            raise ValueError(f"Unknown IANA time zone: {timezone_name!r}")
        self.redis.set(f"user_timezone:{chat_id}", timezone_name)

    def set_waiting_for_notify_input(self, chat_id: int, waiting: bool = True) -> None:
        """
        Note that the next message typed here is a reporting interval.

        The keyboard offers round numbers; this is how someone asks for seven
        minutes. Short-lived for the same reason the rename flag is: a screen
        walked away from must not claim the next thing typed an hour later.
        """
        key = f"notify_input:{chat_id}"
        if waiting:
            self.redis.set(key, "1", ex=settings.PENDING_INPUT_TTL_SECONDS)
        else:
            self.redis.delete(key)

    def is_waiting_for_notify_input(self, chat_id: int) -> bool:
        """Whether this chat is in the middle of typing a reporting interval."""
        return self.redis.exists(f"notify_input:{chat_id}") > 0

    # ==================== Resume Credentials Management ====================

    def save_resume_credentials(self, chat_id: int, username: str, password: str) -> None:
        """
        Store the credentials needed to restart an interrupted search.

        Kept apart from the user session and encrypted: this is the one place
        a Korail password outlives the request that carried it, so it exists
        only while a search is actually running.
        """
        key = f"resume_credentials:{chat_id}"
        box = self._secret_box()
        data = json.dumps({"username": box.encrypt(username), "password": box.encrypt(password)})
        self.redis.set(key, data, ex=settings.RESUME_TTL_SECONDS)
        logger.debug(f"Saved resume credentials for chat_id={chat_id}")

    def get_resume_credentials(self, chat_id: int) -> tuple | None:
        """
        Get the credentials of an interrupted search.

        Returns:
            (username, password), or None when absent or undecryptable - the
            latter happens when SESSION_SECRET changed, and the caller then
            treats the search as unrecoverable.
        """
        data = self.redis.get(f"resume_credentials:{chat_id}")
        if not data:
            return None

        try:
            stored = json.loads(data)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to deserialize resume credentials: {e}")
            return None

        box = self._secret_box()
        username = box.decrypt(stored.get("username"))
        password = box.decrypt(stored.get("password"))

        if not username or not password:
            logger.warning(
                f"Resume credentials for chat_id={chat_id} could not be read - "
                f"the encryption key changed"
            )
            return None

        return username, password

    def delete_resume_credentials(self, chat_id: int) -> None:
        """Forget the credentials of a search that is over."""
        self.redis.delete(f"resume_credentials:{chat_id}")

    # ==================== Korail Client Identity ====================

    def get_or_create_app_session_start(self, chat_id: int) -> str:
        """
        When this user's Korail app session began, in epoch milliseconds.

        The Korail client stamps every request with the moment its app was
        started. A search lives in a child process, so without somewhere to
        keep that moment every restart would announce a freshly launched app
        for a search the user has had running since yesterday. It is created
        once and handed back unchanged for as long as the search can be
        resumed.

        Returns:
            The timestamp as a decimal string
        """
        key = f"app_session_start:{chat_id}"
        now = str(int(time.time() * 1000))

        # Created and returned in one step: two searches starting together
        # must not each believe they made it.
        if self.redis.set(key, now, nx=True, ex=settings.RESUME_TTL_SECONDS):
            return now

        stored = self._text(self.redis.get(key))
        if not stored:
            # Expired between the two calls. Rare, and a fresh one is right.
            self.redis.set(key, now, ex=settings.RESUME_TTL_SECONDS)
            return now

        # Keep it for as long as the search that owns it can still be resumed.
        self.redis.expire(key, settings.RESUME_TTL_SECONDS)
        return stored

    def delete_app_session_start(self, chat_id: int) -> None:
        """Forget an app session, so the next search starts a new one."""
        self.redis.delete(f"app_session_start:{chat_id}")

    # ==================== Payment Status Management ====================

    def get_payment_status(self, chat_id: int) -> PaymentStatus | None:
        """Get payment status by chat ID."""
        key = f"payment_status:{chat_id}"
        data = self.redis.get(key)
        if not data:
            return None

        try:
            status_dict = json.loads(data)
            return self._deserialize_payment_status(status_dict)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to deserialize payment status: {e}")
            return None

    def save_payment_status(self, status: PaymentStatus) -> None:
        """
        Save payment status.

        Kept until a little past the moment the seat is lost. The configured
        window is only this bot's idea of how long a railway holds a seat, so
        the deadline stated on the reservation wins when there is one - a
        record that expired while its reservation was still live would have
        /status say there is nothing to pay for.
        """
        key = f"payment_status:{status.chat_id}"
        data = json.dumps(self._serialize_payment_status(status))
        grace = 5 * 60
        ttl = settings.PAYMENT_TIMEOUT_MINUTES * 60 + grace
        if status.expires_at:
            from korail_bot.utils.timezone import utc_now

            now = utc_now() if status.expires_at.tzinfo else datetime.now()
            ttl = max(ttl, int((status.expires_at - now).total_seconds()) + grace)
        self.redis.set(key, data, ex=ttl)

    def delete_payment_status(self, chat_id: int) -> None:
        """Delete payment status."""
        key = f"payment_status:{chat_id}"
        self.redis.delete(key)

    def claim_payment_watch(self, chat_id: int, owner: str, ttl: int) -> bool:
        """
        Take or renew the watch on one chat's payment.

        SET NX is what makes this a claim rather than a note: whichever
        watcher asks first gets it, and the other finds out by being told no
        instead of by both of them announcing the same payment.

        The expiry is what hands it over when a watcher dies - which is the
        case this exists for, since the search process holding it is killed by
        every restart and the app has to take over without being told.

        Args:
            chat_id: The chat whose payment is being watched
            owner: Who is asking, stable for as long as they watch
            ttl: How long the claim is good for without renewal, in seconds

        Returns:
            True when the caller may watch
        """
        key = f"payment_watch:{chat_id}"
        if self.redis.set(key, owner, nx=True, ex=ttl):
            return True

        # Already claimed. Ours to renew, or somebody else's to leave alone.
        if self._text(self.redis.get(key)) != owner:
            return False

        self.redis.expire(key, ttl)
        return True

    def release_payment_watch(self, chat_id: int, owner: str) -> None:
        """
        Give up the watch, if it is still ours to give up.

        Checked rather than deleted outright: a claim that expired and was
        taken by somebody else belongs to them now, and dropping it would put
        two watchers back on the same payment.
        """
        key = f"payment_watch:{chat_id}"
        if self._text(self.redis.get(key)) == owner:
            self.redis.delete(key)

    def get_all_payment_statuses(self) -> list[PaymentStatus]:
        """Get all payment statuses."""
        keys = self._scan_keys("payment_status:*")
        statuses = []
        for key in keys:
            data = self.redis.get(key)
            if data:
                try:
                    status_dict = json.loads(data)
                    statuses.append(self._deserialize_payment_status(status_dict))
                except (json.JSONDecodeError, KeyError):
                    continue
        return statuses

    # ==================== Subscriber Management ====================

    # ==================== Admin Management ====================

    def is_admin_authenticated(self, chat_id: int) -> bool:
        """Check if user is authenticated as admin."""
        key = f"admin_authenticated:{chat_id}"
        return bool(self.redis.get(key))

    def set_admin_authenticated(self, chat_id: int, authenticated: bool = True) -> None:
        """Set admin authentication status for chat ID."""
        key = f"admin_authenticated:{chat_id}"
        if authenticated:
            self.redis.set(key, "1", ex=settings.ADMIN_SESSION_TTL_SECONDS)
        else:
            self.redis.delete(key)

    def register_admin_auth_failure(self, chat_id: int) -> int:
        """
        Record a failed admin password attempt.

        The counter expires after the lockout window, so attempts spread out
        over time do not accumulate into a permanent lockout.

        Args:
            chat_id: Telegram chat ID

        Returns:
            Number of failures recorded within the current window
        """
        key = f"admin_auth_failures:{chat_id}"
        failures = self.redis.incr(key)
        if failures == 1:
            self.redis.expire(key, settings.ADMIN_LOCKOUT_SECONDS)
        return int(failures)

    def get_admin_auth_failures(self, chat_id: int) -> int:
        """Get the number of recent failed admin password attempts."""
        value = self.redis.get(f"admin_auth_failures:{chat_id}")
        return int(value) if value else 0

    def get_admin_lockout_remaining(self, chat_id: int) -> int:
        """Get seconds remaining before failed attempts are forgotten."""
        ttl = self.redis.ttl(f"admin_auth_failures:{chat_id}")
        return ttl if ttl and ttl > 0 else 0

    def clear_admin_auth_failures(self, chat_id: int) -> None:
        """Reset the failed admin password attempt counter."""
        self.redis.delete(f"admin_auth_failures:{chat_id}")

    def is_waiting_for_admin_password(self, chat_id: int) -> bool:
        """Check if user is waiting to enter admin password."""
        key = f"admin_password_pending:{chat_id}"
        return bool(self.redis.get(key))

    def set_waiting_for_admin_password(self, chat_id: int, waiting: bool = True) -> None:
        """Set whether user is waiting to enter admin password."""
        key = f"admin_password_pending:{chat_id}"
        if waiting:
            self.redis.set(key, "1", ex=300)  # 5 min TTL
        else:
            self.redis.delete(key)

    def get_pending_admin_command(self, chat_id: int) -> str | None:
        """Get pending admin command waiting for authentication."""
        key = f"pending_admin_command:{chat_id}"
        return self._text(self.redis.get(key))

    def set_pending_admin_command(self, chat_id: int, command: str | None) -> None:
        """Set pending admin command waiting for authentication."""
        key = f"pending_admin_command:{chat_id}"
        if command:
            self.redis.set(key, command, ex=300)  # 5 min TTL
        else:
            self.redis.delete(key)

    # ==================== Multi-Reservation Status Management ====================

    def get_multi_reservation_status(self, chat_id: int) -> MultiReservationStatus | None:
        """Get multi-reservation status by chat ID."""
        key = f"multi_reservation_status:{chat_id}"
        data = self.redis.get(key)
        if not data:
            return None

        try:
            status_dict = json.loads(data)
            return self._deserialize_multi_reservation_status(status_dict)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to deserialize multi reservation status: {e}")
            return None

    def save_multi_reservation_status(self, status: MultiReservationStatus) -> None:
        """Save multi-reservation status."""
        key = f"multi_reservation_status:{status.chat_id}"
        data = json.dumps(self._serialize_multi_reservation_status(status))
        # Set with TTL
        ttl = (settings.PAYMENT_TIMEOUT_MINUTES + 5) * 60
        self.redis.set(key, data, ex=ttl)

    def delete_multi_reservation_status(self, chat_id: int) -> None:
        """Delete multi-reservation status and related keys."""
        # Delete main status
        key = f"multi_reservation_status:{chat_id}"
        self.redis.delete(key)

        # Delete current seat index
        seat_key = f"current_seat_index:{chat_id}"
        self.redis.delete(seat_key)

        # Delete all payment ready flags for this user
        payment_keys = self._scan_keys(f"payment_ready:{chat_id}:*")
        if payment_keys:
            self.redis.delete(*payment_keys)

    def get_all_multi_reservation_statuses(self) -> list[MultiReservationStatus]:
        """Get all multi-reservation statuses."""
        keys = self._scan_keys("multi_reservation_status:*")
        statuses = []
        for key in keys:
            data = self.redis.get(key)
            if data:
                try:
                    status_dict = json.loads(data)
                    statuses.append(self._deserialize_multi_reservation_status(status_dict))
                except (json.JSONDecodeError, KeyError):
                    continue
        return statuses

    # ==================== Partial Reservation Management (Random Seating) ====================

    def save_partial_reservation(
        self, chat_id: int, seat_index: int, reservation_data: dict
    ) -> None:
        """
        Save a partial reservation for random seating.

        Args:
            chat_id: User chat ID
            seat_index: Index of the seat (0-based)
            reservation_data: Serialized reservation information
        """
        key = f"partial_reservations:{chat_id}"
        # Store as JSON array
        existing = self.redis.get(key)
        reservations = json.loads(existing) if existing else []

        # Add or update
        while len(reservations) <= seat_index:
            reservations.append(None)
        reservations[seat_index] = reservation_data

        data = json.dumps(reservations)
        # TTL: 2 hours (enough for multiple reservations)
        self.redis.set(key, data, ex=7200)
        logger.info(f"Saved partial reservation {seat_index} for chat_id={chat_id}")

    def get_partial_reservations(self, chat_id: int) -> list[dict]:
        """Get all partial reservations for a chat_id."""
        key = f"partial_reservations:{chat_id}"
        data = self.redis.get(key)
        if not data:
            return []

        try:
            reservations = json.loads(data)
            # Filter out None values
            return [r for r in reservations if r is not None]
        except json.JSONDecodeError:
            return []

    def delete_partial_reservations(self, chat_id: int) -> None:
        """Delete all partial reservations for a chat_id."""
        key = f"partial_reservations:{chat_id}"
        self.redis.delete(key)

    def get_current_seat_index(self, chat_id: int) -> int | None:
        """Get the current seat index being reserved (for random seating)."""
        key = f"current_seat_index:{chat_id}"
        value = self.redis.get(key)
        return int(value) if value is not None else None

    def set_current_seat_index(self, chat_id: int, index: int | None) -> None:
        """Set the current seat index being reserved."""
        key = f"current_seat_index:{chat_id}"
        if index is not None:
            self.redis.set(key, str(index), ex=7200)  # 2 hour TTL
        else:
            self.redis.delete(key)

    def is_payment_ready(self, chat_id: int, seat_index: int) -> bool:
        """Check if payment is ready for a specific seat."""
        key = f"payment_ready:{chat_id}:{seat_index}"
        return bool(self.redis.get(key))

    def mark_payment_ready(self, chat_id: int, seat_index: int) -> None:
        """Mark payment as ready for a specific seat."""
        key = f"payment_ready:{chat_id}:{seat_index}"
        self.redis.set(key, "1", ex=60)  # 60s TTL
        logger.info(f"Marked payment ready for seat {seat_index}, chat_id={chat_id}")

    def wait_for_payment(self, chat_id: int, seat_index: int, timeout: int = 600) -> bool:
        """
        Wait for payment confirmation with polling.

        Args:
            chat_id: User chat ID
            seat_index: Seat index (0-based)
            timeout: Maximum wait time in seconds (default 10 minutes)

        Returns:
            True if payment confirmed within timeout, False otherwise
        """
        key = f"payment_ready:{chat_id}:{seat_index}"
        start_time = time.time()

        logger.info(f"Waiting for payment confirmation (seat {seat_index}, timeout={timeout}s)...")

        while time.time() - start_time < timeout:
            # Check if payment flag is set
            if self.redis.get(key):
                # Delete the flag
                self.redis.delete(key)
                elapsed = int(time.time() - start_time)
                logger.info(f"Payment confirmed after {elapsed}s")
                return True

            # Sleep 1 second
            time.sleep(1)

            # Log every 30 seconds
            elapsed = int(time.time() - start_time)
            if elapsed % 30 == 0 and elapsed > 0:
                remaining = timeout - elapsed
                logger.debug(f"Still waiting for payment... {remaining}s remaining")

        logger.warning(f"Payment timeout after {timeout}s")
        return False

    # ==================== Serialization Helpers ====================

    def _serialize_user_session(self, session: UserSession) -> dict:
        """Serialize UserSession to dict."""
        return {
            "chat_id": session.chat_id,
            "in_progress": session.in_progress,
            "last_action": session.last_action,
            "process_id": session.process_id,
            "train_info": session.train_info,
            "credentials": {
                "korail_id": session.credentials.korail_id,
                # Encrypted at rest: Redis must never hold a usable password.
                "korail_pw": self._secret_box().encrypt(session.credentials.korail_pw),
            }
            if session.credentials
            else None,
            "search_params": self._serialize_search_params(session.search_params)
            if session.search_params
            else None,
        }

    def _deserialize_user_session(self, data: dict) -> UserSession:
        """Deserialize dict to UserSession."""
        credentials = None
        if data.get("credentials"):
            c = data["credentials"]
            # An unreadable password (rotated key, tampered value) decrypts to
            # None; the user is then asked to enter it again.
            credentials = UserCredentials(
                korail_id=c["korail_id"],
                korail_pw=self._secret_box().decrypt(c.get("korail_pw")) or "",
            )

        search_params = None
        if data.get("search_params"):
            search_params = self._deserialize_search_params(data["search_params"])

        return UserSession(
            chat_id=data["chat_id"],
            in_progress=data["in_progress"],
            last_action=data["last_action"],
            process_id=data.get("process_id", 9999999),
            train_info=data.get("train_info", {}),
            credentials=credentials,
            search_params=search_params,
        )

    def _serialize_search_params(self, params: TrainSearchParams) -> dict:
        """
        Turn search parameters into something JSON can hold.

        One place rather than three. The user session, the running
        reservation and the scheduled search all carry the same object, and
        each used to spell out its own field list - so a new field meant
        finding every one of them, and the running reservation had already
        quietly fallen behind by two.
        """
        return {
            "dep_date": params.dep_date,
            "src_locate": params.src_locate,
            "dst_locate": params.dst_locate,
            "dep_time": params.dep_time,
            "max_dep_time": params.max_dep_time,
            "train_type": params.train_type,
            "train_type_display": params.train_type_display,
            "special_option": params.special_option,
            "special_option_display": params.special_option_display,
            "passenger_count": params.passenger_count,
            "seat_strategy": params.seat_strategy,
            "train_numbers": params.train_numbers,
            "seat_preference": params.seat_preference,
        }

    def _deserialize_search_params(self, data: dict) -> TrainSearchParams:
        """
        Rebuild search parameters from storage.

        Every field is read with a default. Records outlive the code that
        wrote them, and a stored search that a deploy makes unreadable is a
        search the user is still waiting on.
        """
        defaults = TrainSearchParams(dep_date="", src_locate="", dst_locate="", dep_time="")
        return TrainSearchParams(
            dep_date=data["dep_date"],
            src_locate=data["src_locate"],
            dst_locate=data["dst_locate"],
            dep_time=data["dep_time"],
            max_dep_time=data.get("max_dep_time", defaults.max_dep_time),
            train_type=data.get("train_type", defaults.train_type),
            train_type_display=data.get("train_type_display", defaults.train_type_display),
            special_option=data.get("special_option", defaults.special_option),
            special_option_display=data.get(
                "special_option_display", defaults.special_option_display
            ),
            passenger_count=data.get("passenger_count", defaults.passenger_count),
            seat_strategy=data.get("seat_strategy", defaults.seat_strategy),
            train_numbers=data.get("train_numbers") or [],
            seat_preference=data.get("seat_preference", defaults.seat_preference),
        )

    def _serialize_running_reservation(self, reservation: RunningReservation) -> dict:
        """Serialize RunningReservation to dict."""
        return {
            "chat_id": reservation.chat_id,
            "process_id": reservation.process_id,
            "korail_id": reservation.korail_id,
            "run_id": reservation.run_id,
            "search_params": self._serialize_search_params(reservation.search_params),
        }

    def _deserialize_running_reservation(self, data: dict) -> RunningReservation:
        """Deserialize dict to RunningReservation."""
        search_params = self._deserialize_search_params(data["search_params"])

        return RunningReservation(
            chat_id=data["chat_id"],
            process_id=data["process_id"],
            korail_id=data["korail_id"],
            search_params=search_params,
            # Records written before restart recovery existed have no run id,
            # which correctly marks them as belonging to an earlier run.
            run_id=data.get("run_id", ""),
        )

    def _serialize_payment_status(self, status: PaymentStatus) -> dict:
        """Serialize PaymentStatus to dict."""
        from korail_bot.utils.timezone import as_utc

        return {
            "schema": 2,
            "chat_id": status.chat_id,
            "reminder_active": status.reminder_active,
            "completed": status.completed,
            "created_at": as_utc(status.created_at).isoformat() if status.created_at else None,
            "reservation_id": status.reservation_id,
            "train_info": status.train_info,
            "expires_at": as_utc(status.expires_at).isoformat() if status.expires_at else None,
            "cancelled": status.cancelled,
            "train_no": status.train_no,
            "dep_date": status.dep_date,
            "dep_time": status.dep_time,
        }

    def _deserialize_payment_status(self, data: dict) -> PaymentStatus:
        """Deserialize dict to PaymentStatus."""
        from korail_bot.utils.timezone import RAIL_TIMEZONE, as_utc

        # Legacy records mixed two origins: created_at came from the UTC app
        # container, while expires_at was copied from Korail/SR's Korean
        # payment deadline. Schema 2 writes both as aware UTC.
        legacy = not data.get("schema")
        created_at_raw = data.get("created_at") or data.get("reservation_time")
        created_at = as_utc(datetime.fromisoformat(created_at_raw)) if created_at_raw else None
        expires_raw = data.get("expires_at")
        expires_at = datetime.fromisoformat(expires_raw) if expires_raw else None
        if expires_at is not None:
            expires_at = (
                as_utc(expires_at, naive_zone=RAIL_TIMEZONE) if legacy else as_utc(expires_at)
            )

        return PaymentStatus(
            chat_id=data["chat_id"],
            reminder_active=data["reminder_active"],
            completed=data["completed"],
            # 'reservation_time' is the old name for this field. These records
            # live for PAYMENT_TIMEOUT_MINUTES + 5, so reading both only
            # matters across a deploy, but that is exactly when a user is
            # part-way through paying.
            created_at=created_at,
            # Everything below is written after the seat is secured, so a
            # record read between the window opening and that write has none
            # of it - as has one written before these fields existed.
            reservation_id=data.get("reservation_id"),
            train_info=data.get("train_info") or "",
            expires_at=expires_at,
            cancelled=data.get("cancelled", False),
            # Read with a default, never indexed: a record written before
            # these fields existed must come back as a record with no train
            # hints - which reads as "cannot tell" - rather than as a
            # KeyError that takes the whole watchdog pass down with it.
            train_no=data.get("train_no", "") or "",
            dep_date=data.get("dep_date", "") or "",
            dep_time=data.get("dep_time", "") or "",
        )

    def _serialize_multi_reservation_status(self, status: MultiReservationStatus) -> dict:
        """Serialize MultiReservationStatus to dict."""
        from korail_bot.utils.timezone import as_utc

        return {
            "chat_id": status.chat_id,
            "reservations": [
                {
                    "reservation_id": r.reservation_id,
                    "reserved_at": as_utc(r.reserved_at).isoformat(),
                    "expires_at": as_utc(r.expires_at).isoformat(),
                    "status": r.status.value if hasattr(r.status, "value") else r.status,
                    "seat_number": r.seat_number,
                    "train_info": r.train_info,
                    "train_no": r.train_no,
                    "dep_date": r.dep_date,
                    "dep_time": r.dep_time,
                }
                for r in status.reservations
            ],
            "total_seats": status.total_seats,
            "seat_strategy": status.seat_strategy,
            "created_at": as_utc(status.created_at).isoformat(),
            "manually_stopped": status.manually_stopped,
        }

    def _deserialize_multi_reservation_status(self, data: dict) -> MultiReservationStatus:
        """Deserialize dict to MultiReservationStatus."""
        from korail_bot.models import ReservationPaymentStatus, SingleReservationInfo
        from korail_bot.utils.timezone import as_utc

        reservations = [
            SingleReservationInfo(
                reservation_id=r["reservation_id"],
                reservation_obj=None,  # Can't serialize actual reservation object
                reserved_at=as_utc(datetime.fromisoformat(r["reserved_at"])),
                expires_at=as_utc(datetime.fromisoformat(r["expires_at"])),
                status=ReservationPaymentStatus(r["status"])
                if isinstance(r["status"], str)
                else r["status"],
                seat_number=r["seat_number"],
                train_info=r["train_info"],
                # With a default, for the reason the single-booking record
                # reads them that way: a seat written before these fields
                # existed has no hints, not a KeyError.
                train_no=r.get("train_no", "") or "",
                dep_date=r.get("dep_date", "") or "",
                dep_time=r.get("dep_time", "") or "",
            )
            for r in data["reservations"]
        ]

        return MultiReservationStatus(
            chat_id=data["chat_id"],
            reservations=reservations,
            total_seats=data["total_seats"],
            seat_strategy=data["seat_strategy"],
            created_at=as_utc(datetime.fromisoformat(data["created_at"])),
            manually_stopped=data["manually_stopped"],
        )

    # ==================== Admin Operations ====================

    def flush_all(self) -> int:
        """
        Flush all Redis data (admin operation).

        WARNING: This will delete ALL data from the Redis database.
        Use with extreme caution.

        Returns:
            Number of keys deleted
        """
        try:
            # Get count before flushing
            key_count = self.redis.dbsize()

            # Flush all data in current database
            self.redis.flushdb()

            logger.warning(f"Redis database flushed. {key_count} keys deleted.")
            return key_count
        except redis.RedisError as e:
            logger.error(f"Failed to flush Redis: {e}")
            raise

    # ==================== Debug Mode Management ====================

    def is_debug_mode(self) -> bool:
        """Check if global debug mode is enabled."""
        return self.redis.get("debug_mode:global") == "1"

    # ==================== Release announcements ====================

    def get_announced_version(self) -> str | None:
        """
        The version this deployment last told its users about.

        Returns:
            The version string, or None when nothing has been announced yet
        """
        return self._text(self.redis.get("announced_version")) or None

    def set_announced_version(self, version: str) -> None:
        """Record that this version's announcement has been dealt with."""
        self.redis.set("announced_version", version)

    def set_debug_mode(self, enabled: bool) -> None:
        """Enable or disable global debug mode."""
        if enabled:
            self.redis.set("debug_mode:global", "1")
            logger.info("Global debug mode enabled")
        else:
            self.redis.delete("debug_mode:global")
            logger.info("Global debug mode disabled")
