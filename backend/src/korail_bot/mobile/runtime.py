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

# The owner renews its lease on every background pass. One it stopped renewing
# - killed, or cut off from Redis - expires after this.
LEASE_TTL = 120
# A renewal sent at s keeps the key until at least s + LEASE_TTL. The runtime
# counts on its lease only until lease_until = s + LEASE_TTL - LEASE_MARGIN, and
# has to have stopped its searches before Redis could expire the key and let
# another runtime take it. Past lease_until the loss is noticed within 12 s:
# - each pass checks lease_until before it renews, and a pass whose renewal
#   failed ends there, so a check follows a failed renewal within PASS_WAIT;
# - a renewal still waiting on Redis at lease_until counts as lost when it
#   returns. Lease calls never retry (storage.lease_client_for), so against a
#   stalled Redis each of the LEASE_ATTEMPTS tries ends at its first unanswered
#   read: a renewal takes at most 3 x (2 s connect + 2 s read) = 12 s.
# That leaves 18 s of the margin to stop the searches. A pass that renewed goes
# on to reminders and push delivery, on the main client and FCM: those have
# LEASE_TTL - LEASE_MARGIN - PASS_WAIT = 80 s before they could hold up a check,
# against 8 s for a stalled main-client call (3 s connect + 5 s read; redis-py 8
# does not retry for a from_url client) and 50 s for five pushes at 10 s each.
LEASE_MARGIN = 30


class MobileRuntime:
    # How long start() waits out a lease left behind by a runtime that is gone.
    # A live owner keeps renewing, so it outlasts this and is still refused.
    LEASE_WAIT = LEASE_TTL + 15
    LEASE_POLL = 5
    # Between background passes.
    PASS_WAIT = 10

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
        self.services = []
        self.owner = secrets.token_hex(24)
        self.lease_until = 0.0
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
        )

    def start(self):
        if self.started:
            return
        if self.storage:
            moved = self.storage.redis.adopt_legacy_keys()
            if moved:
                logger.info("Adopted %d keys from the legacy Redis namespace", moved)
            self._acquire_lease(self.storage.redis)
            self.reservation.reconcile_after_restart()
            for service in self.services:
                service.start()
        self.started = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="mobile-notifications")
        self.thread.start()

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
        # False once the lease is gone. A Redis error is not that - the lease
        # may still be ours - so it is raised and the next pass tries again,
        # until the lease could have expired: then it counts as lost anyway.
        # Any other exception is a bug, not a lost lease: raised for _keep_lease.
        sent = time.monotonic()
        try:
            renewed = redis.renew_lease(self.owner, LEASE_TTL)
        except RedisError:
            if time.monotonic() < self.lease_until:
                raise
            logger.error("Mobile runtime lease could not be renewed in time")
            return False
        if renewed:
            self.lease_until = sent + LEASE_TTL - LEASE_MARGIN
        return renewed

    def _keep_lease(self):
        """True once renewed, False once lost; raises when this pass could not tell."""
        if time.monotonic() >= self.lease_until:
            # From here Redis may expire it within LEASE_MARGIN: the time left
            # to stop the searches, not to wait on a renewal (see LEASE_MARGIN).
            logger.error("Mobile runtime lease was not renewed in time")
            return False
        try:
            return self._renew_lease(self.storage.redis)
        except RedisError:
            raise  # a failed pass: _renew_lease found the lease may still hold
        except Exception:
            # Not Redis failing but this code. Try again next pass while the
            # lease still holds; once it may not, one runtime per namespace
            # matters more than this one staying up.
            if time.monotonic() < self.lease_until:
                logger.exception("Mobile runtime lease renewal failed; trying again next pass")
                raise
            logger.exception("Mobile runtime lease renewal failed and the lease may have expired")
            return False

    def _run(self):
        while not self.stop_event.is_set():
            try:
                if self.storage:
                    if not self._keep_lease():
                        if self.stop_event.is_set():
                            return  # stop() released it while this pass ran
                        logger.error("Mobile runtime lease lost; stopping searches")
                        # Another runtime may own the namespace now, and the HTTP
                        # server would go on writing to it. End the process before
                        # tearing down, so requests are refused rather than failed.
                        if self.on_lease_lost:
                            self.on_lease_lost()
                        self.stop()
                        return
                    self.remind_pending()
                self.notifications.deliver()
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
        # notification thread tears down while the main thread, woken by the
        # SIGTERM, calls this too; returning at once would let the interpreter
        # exit and kill that daemon thread before it stopped the searches and
        # released the lease - which the next start then finds still held.
        with self.stop_lock:
            if self.stop_event.is_set():
                return
            self.stop_event.set()
            for service in self.services:
                service.stop()
            if self.reservation:
                self.reservation.shutdown()
            for service in self.services:
                thread = getattr(service, "_thread", None)
                if thread and thread is not threading.current_thread():
                    thread.join(timeout=5)
            if self.thread and self.thread is not threading.current_thread():
                self.thread.join(timeout=5)
            if self.storage:
                try:
                    self.storage.redis.release_lease(self.owner)
                except Exception:
                    # The next start waits it out; it expires within LEASE_TTL.
                    logger.exception("Mobile runtime lease not released")
                # Closes the lease client too.
                self.storage.close()
