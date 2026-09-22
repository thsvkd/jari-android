"""One independent process owns mobile search, scheduling and payment watching."""

import secrets
import threading

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


class MobileRuntime:
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
        self.thread = None
        self.services = []
        self.owner = secrets.token_hex(24)
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
            if not self.storage.redis.set("runtime_owner", self.owner, nx=True, ex=120):
                raise RuntimeError("Another mobile runtime owns this Redis namespace")
            self.reservation.reconcile_after_restart()
            for service in self.services:
                service.start()
        self.started = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="mobile-notifications")
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                if self.storage:
                    if not self.storage.redis.renew_lease(self.owner, 120):
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
            self.stop_event.wait(10)

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
            self.storage.redis.release_lease(self.owner)
            self.storage.close()
