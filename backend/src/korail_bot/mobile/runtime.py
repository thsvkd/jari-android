"""One independent process owns mobile search, scheduling and payment watching."""

import secrets
import threading
import time

from redis.exceptions import RedisError

from korail_bot.mobile.api import create_app
from korail_bot.mobile.gateway import (
    InvitedAccess,
    MobileConversation,
    MobileGateway,
    UnavailableGateway,
)
from korail_bot.mobile.identity import IdentityStore
from korail_bot.mobile.notifications import Notifications, configured_fcm
from korail_bot.mobile.process import MobileReservationService
from korail_bot.mobile.scheduler import MobileScheduledSearchService
from korail_bot.mobile.storage import MobileStorage
from korail_bot.services.payment_watchdog_service import PaymentWatchdogService
from korail_bot.services.search_watchdog_service import SearchWatchdogService
from korail_bot.services.seat_map_service import SeatMapService
from korail_bot.utils.logger import get_logger
from korail_bot.utils.timezone import as_utc, utc_now

logger = get_logger(__name__)

# The owner renews its lease every PASS_WAIT on a thread that does nothing else.
# One it stopped renewing - killed, or cut off from Redis - expires after this.
LEASE_TTL = 120
# A renewal sent at s keeps the key until at least s + LEASE_TTL. The runtime
# counts on its lease only until lease_until = s + LEASE_TTL - LEASE_MARGIN, and
# has to have stopped its searches before Redis could expire the key and let
# another runtime take it. It gives the lease up at lease_until, late only by
# its own thread's scheduling:
# - the lease thread does nothing but renew, and starts the moment start() has
#   the lease, so no other work can delay a check. Resuming an earlier run's
#   searches takes a 1 s start grace, about nine main-client round trips and a
#   SQLite write for each - usually no more than five, though resuming has no
#   cap - and can run past lease_until on a saturated SD card (simulated: 103 s
#   at 1.5 s per Redis reply and 4 s per SQLite write). A background pass can
#   take minutes: up to five pushes, and
#   firebase-admin retries a push's connect and read once each and a 500/503
#   four times, with httpTimeout for every connect and every read;
# - a renewal still out at lease_until is not waited for any longer. How long
#   one takes is up to redis-py, not a sum of socket timeouts: every reconnect
#   is a four-round-trip handshake, and a failed WATCH reconnects just to
#   UNWATCH. Against a Redis answering just within the timeout, one failing
#   renewal took 26 socket timeouts - 52 s at 2 s;
# - after a failed renewal the thread tries again by lease_until.
# The whole margin is left to stop the searches, one worker after another: 3 s
# for one that ignores SIGTERM, 6 s if even the kill hangs, for up to
# MAX_CONCURRENT_SEARCHES (5) of them. Five hung kills take all 30 s, but a
# worker does nothing more once SIGKILL is sent - the last at 4 x 6 + 3 = 27 s.
# This holds while Redis keeps the key for its TTL (AOF, no wall-clock step)
# and a freeze of this process plus the time to stop the searches stays under
# LEASE_TTL - PASS_WAIT - no freeze past about 80 s with five hung kills;
# there is no fencing of the search workers beyond that.
LEASE_MARGIN = 30


class _NoAnswer(Exception):
    """A lease call had not answered by its deadline."""


def _answer_by(deadline, call, *args, name, unless=None):
    """
    call(*args) on a thread of its own: what it returned or raised, if it did
    so by `deadline` (monotonic) and before `unless` was set; else _NoAnswer.

    A call not waited for any longer is left to run out. Closing the lease
    client ends it at its next read, or redis-py's own timeouts do: it may
    reconnect and go on for its remaining tries. Nothing reads its answer.
    """
    answer = {}
    answered = threading.Event()

    def run():
        try:
            answer["value"] = call(*args)
        except Exception as exc:
            answer["error"] = exc
        finally:
            answered.set()

    threading.Thread(target=run, daemon=True, name=name).start()
    # In slices, to notice `unless`; the last one ends at the deadline.
    while not answered.wait(max(0.0, min(deadline - time.monotonic(), 0.1))):
        if time.monotonic() >= deadline or (unless is not None and unless.is_set()):
            raise _NoAnswer
    if "error" in answer:
        raise answer["error"]
    return answer["value"]


class MobileRuntime:
    # How long start() waits out a lease left behind by a runtime that is gone.
    # A live owner keeps renewing, so it outlasts this and is still refused.
    LEASE_WAIT = LEASE_TTL + 15
    LEASE_POLL = 5
    # Between background passes, and between lease renewals.
    PASS_WAIT = 10
    # How long stop() waits for each thread, and for Redis to take the lease
    # back. Against compose.yaml's stop_grace_period of 60 s: five joins and
    # the release take up to 35 s, and the searches up to 30 s (LEASE_MARGIN).
    # Only with all of them at their worst - searches whose kills hang, stuck
    # threads, a Redis that does not answer - does stop() run to about 65 s.
    # A SIGKILL then comes before the release, and the lease left behind only
    # delays the next start, by up to LEASE_TTL, which it waits out.
    JOIN_WAIT = 5
    RELEASE_WAIT = 10

    def __init__(self, config, *, redis_client=None, push=None, on_lease_lost=None):
        self.config = config
        self.on_lease_lost = on_lease_lost
        self.identity = IdentityStore(config.database)
        self.storage = (
            MobileStorage(secret=config.secret, url=config.redis_url, client=redis_client)
            if config.redis_url or redis_client is not None
            else None
        )
        self.notifications = Notifications(
            self.identity,
            secret=config.secret,
            storage=self.storage,
            push=push or configured_fcm(config.fcm_credentials),
        )
        self.stop_event = threading.Event()
        self.stop_lock = threading.Lock()
        self.thread = None
        self.lease_thread = None
        self.services = []
        self.owner = secrets.token_hex(24)
        self.lease_until = 0.0
        self.lease_lost = False
        self.started = False
        self.reservation = None
        self.seat_maps = SeatMapService()
        if self.storage is not None:
            self.reservation = MobileReservationService(self.storage, self.notifications, config)
            conversation = MobileConversation(
                self.storage,
                self.notifications,
                self.reservation,
                access_service=InvitedAccess(self.storage),
            )
            scheduler = MobileScheduledSearchService(
                self.storage, self.notifications, self.reservation
            )
            self.gateway = MobileGateway(
                self.storage,
                self.notifications,
                self.reservation,
                conversation_handler=conversation,
                scheduled_search_service=scheduler,
                seat_map_service=self.seat_maps,
            )
            self.services = [
                scheduler,
                SearchWatchdogService(self.reservation),
                PaymentWatchdogService(self.storage, self.notifications),
            ]
        else:
            self.gateway = UnavailableGateway()
        self.app = create_app(
            self.identity,
            self.gateway,
            self.notifications,
            origins=config.origins,
            booking_available=self.storage is not None,
            health=self.health,
            privacy_contact=config.privacy_contact,
        )

    def health(self):
        """What /health reports: Redis answers and this runtime still holds its lease."""
        if self.storage is None:
            return {"ok": True, "booking": False}
        try:
            redis = bool(self.storage.redis.ping())
        except RedisError:
            redis = False
        lease = not self.lease_lost and time.monotonic() < self.lease_until
        return {"ok": redis and lease, "booking": True, "redis": redis, "lease": lease}

    def start(self):
        if self.started:
            return
        steps = []
        if self.storage:
            moved = self.storage.redis.adopt_legacy_keys()
            if moved:
                logger.info("Adopted %d keys from the legacy Redis namespace", moved)
            self._acquire_lease(self.storage.redis)
            # Renewed from here on: resuming searches can outlast the time the
            # lease is counted on (see LEASE_MARGIN).
            self.lease_thread = threading.Thread(
                target=self._keep_renewing, daemon=True, name="mobile-lease"
            )
            self.lease_thread.start()
            steps = [self.reservation.reconcile_after_restart, *(s.start for s in self.services)]
        self.thread = threading.Thread(target=self._run, daemon=True, name="mobile-notifications")
        steps.append(self.thread.start)
        # The lease thread may find the lease lost while these run. Each runs
        # whole before stop() or not at all, so none starts a search, service
        # or thread after stop() tore them down; start() ends instead. On a
        # lost lease the SIGTERM also ends the step the main thread is in, once
        # it is back in Python: a SQLite write on a busy database holds it up
        # to 15 s.
        for step in steps:
            with self.stop_lock:
                if self.lease_lost or self.stop_event.is_set():
                    raise RuntimeError("Mobile runtime stopped while starting")
                step()
        self.started = True

    def _acquire_lease(self, redis):
        # A lease left by a runtime that never released it (killed, or cut off
        # mid-teardown) expires within LEASE_TTL. Exiting at once would only
        # have the container restarted over and over until it does. Redis not
        # answering - a host stall - is waited out within the same window, and
        # raised once it has not answered by the end of it.
        deadline = time.monotonic() + self.LEASE_WAIT
        held = unanswered = False
        while True:
            sent = time.monotonic()
            try:
                # Only a SET NX takes the lease. When Redis ran one but its
                # reply was lost, the next try here finds the key holding this
                # runtime's token - 48 random hex digits nothing else writes -
                # and renewing it is that SET NX going through, confirmed. The
                # lease client never resends a command itself; this loop does.
                if redis.acquire_lease(self.owner, LEASE_TTL) or (
                    redis.renew_lease(self.owner, LEASE_TTL)
                ):
                    self.lease_until = sent + LEASE_TTL - LEASE_MARGIN
                    if held or unanswered:
                        logger.info("Mobile runtime lease acquired")
                    return
                if not held:
                    logger.warning(
                        "Mobile runtime lease is held (expires in %ss); waiting up to %ss",
                        redis.lease_ttl(),
                        self.LEASE_WAIT,
                    )
                    held = True
                failure = None
            except RedisError as exc:
                failure = exc
                if not unanswered:
                    unanswered = True
                    logger.warning(
                        "Redis did not answer the mobile runtime lease (%s); retrying for up to %ss",
                        type(exc).__name__,
                        self.LEASE_WAIT,
                    )
            if time.monotonic() >= deadline or self.stop_event.is_set():
                if failure is not None:
                    raise failure
                raise RuntimeError("Another mobile runtime owns this Redis namespace")
            self.stop_event.wait(self.LEASE_POLL)

    def _renew_lease(self, redis):
        # True once renewed, False once lost; raises what the renewal raised,
        # and None once stop() has begun. Waited for only until lease_until:
        # one still out then is lost - the runtime ends. A late answer moves no
        # lease_until; in Redis it can only extend a key still holding this
        # runtime's token, which stop() then releases - or, landing after,
        # WATCH aborts it.
        sent = time.monotonic()
        try:
            renewed = _answer_by(
                self.lease_until,
                redis.renew_lease,
                self.owner,
                LEASE_TTL,
                name="mobile-lease-renewal",
                unless=self.stop_event,
            )
        except _NoAnswer:
            if self.stop_event.is_set():
                return None  # stop() releases it; not waited for
            logger.error("Mobile runtime lease could not be renewed in time")
            return False
        if renewed:
            self.lease_until = sent + LEASE_TTL - LEASE_MARGIN
        return renewed

    def _keep_lease(self):
        """True once renewed, False once lost, None when this try could not tell."""
        if time.monotonic() >= self.lease_until:
            # From here Redis may expire it within LEASE_MARGIN: the time left
            # to stop the searches, not to wait on a renewal (see LEASE_MARGIN).
            logger.error("Mobile runtime lease was not renewed in time")
            return False
        try:
            return self._renew_lease(self.storage.redis)
        except RedisError as exc:
            # Not an answer: the lease may still be ours until lease_until.
            logger.warning(
                "Mobile runtime lease renewal failed (%s); trying again", type(exc).__name__
            )
        except Exception:
            # Not Redis failing but this code. Tried again all the same: it
            # moves no lease_until, so it cannot keep the lease past it.
            logger.exception("Mobile runtime lease renewal failed; trying again")
        return None

    def _keep_renewing(self):
        while not self.stop_event.is_set():
            if self._keep_lease() is False:
                if self.stop_event.is_set():
                    return  # stop() released it while this renewal ran
                logger.error("Mobile runtime lease lost; stopping searches")
                # start() then starts nothing more, and stop() need not wait
                # for this thread: it only waits for stop_lock from here.
                self.lease_lost = True
                # Another runtime may own the namespace now, and the HTTP
                # server would go on writing to it. End the process before
                # tearing down, so requests are refused rather than failed.
                if self.on_lease_lost:
                    self.on_lease_lost()
                self.stop()
                return
            # No later than lease_until, where a lease no renewal extended is given up.
            self.stop_event.wait(min(self.PASS_WAIT, max(0.0, self.lease_until - time.monotonic())))

    def _run(self):
        steps = [self.notifications.deliver]
        if self.storage:
            # Searches an earlier run left that start() could not bring back
            # are tried again here, as each falls due (reconcile_after_restart
            # keeps the count and the backoff). And every search's stored
            # login is kept from expiring while its record says it runs.
            steps = [
                self.reservation.reconcile_after_restart,
                self.reservation.keep_resumable,
                self.remind_pending,
                *steps,
            ]
        while not self.stop_event.is_set():
            # Each on its own: a push that fails must not cost a search its
            # retry, or a login its expiry.
            for step in steps:
                if self.stop_event.is_set():
                    return
                try:
                    step()
                except Exception as exc:
                    logger.error("Mobile background pass failed (%s)", type(exc).__name__)
            self.stop_event.wait(self.PASS_WAIT)

    def remind_pending(self):
        if self.storage is None:
            return
        # Re-read durable payment records every pass. No memory-only reminder
        # registry is required to reconstruct reminders after a restart.
        now = utc_now()
        for owner in {s.chat_id for s in self.storage.get_all_payment_statuses()} | {
            s.chat_id for s in self.storage.get_all_multi_reservation_statuses()
        }:
            for item in self.gateway.pending_payments.pending(owner):
                if not item.expires_at:
                    continue
                remaining = int((as_utc(item.expires_at) - now).total_seconds())
                if remaining <= 0:
                    continue
                self.notifications.publish(
                    owner,
                    f"예약한 좌석의 결제 시간이 약 {max(1, remaining // 60)}분 남았어요. 코레일 앱에서 결제해 주세요.",
                    kind="payment",
                    dedupe=f"payment:{owner}:{item.reservation_id}:{int(now.timestamp()) // 60}",
                )

    def stop(self):
        # A second caller waits for the first to finish. On a lost lease the
        # lease thread tears down while the main thread, woken by the
        # SIGTERM, calls this too; returning at once would let the interpreter
        # exit and kill that daemon thread before it stopped the searches and
        # released the lease - which the next start then finds still held.
        #
        # Before anything, and before waiting for that lock: from here every
        # search that stops is being stopped on purpose, its record left for
        # the next run. A dead-search pass already under way must not report
        # one as a death - that moves the record out of the next run's reach -
        # and no retry may start a search the teardown would then miss.
        if self.reservation:
            self.reservation._shutting_down = True
        with self.stop_lock:
            if self.stop_event.is_set():
                return
            self.stop_event.set()
            # Each step is tried whatever became of the ones before: searches
            # left running, or a lease left held, outlast this process.
            steps = [service.stop for service in self.services]
            if self.reservation:
                steps.append(self.reservation.shutdown)
            for step in steps:
                try:
                    step()
                except Exception:
                    logger.exception("Mobile runtime teardown step failed")
            threads = [getattr(service, "_thread", None) for service in self.services]
            threads += [self.thread, None if self.lease_lost else self.lease_thread]
            for thread in threads:
                # join() refuses a thread never started, as after a failed start().
                if thread and thread.is_alive() and thread is not threading.current_thread():
                    thread.join(timeout=self.JOIN_WAIT)
            if self.storage:
                try:
                    _answer_by(
                        time.monotonic() + self.RELEASE_WAIT,
                        self.storage.redis.release_lease,
                        self.owner,
                        name="mobile-lease-release",
                    )
                except _NoAnswer:
                    logger.error("Mobile runtime lease not released in time")
                except Exception:
                    # The next start waits it out; it expires within LEASE_TTL.
                    logger.exception("Mobile runtime lease not released")
                # Closes the lease client too, under a release not waited for.
                self.storage.close()
