"""Shared Korail search loop with direct mobile state and durable events."""

import os
import time
from contextlib import suppress

from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.identity import IdentityStore
from korail_bot.mobile.notifications import Notifications
from korail_bot.mobile.storage import MobileStorage
from korail_bot.models import PaymentStatus
from korail_bot.telegramBot.telebotBackProcess import (
    BackgroundReservationProcess,
    SearchStopped,
    install_shutdown_handlers,
)


def apply_result(storage, notifications, owner, message, status=0, seat_strategy="consecutive"):
    """No network callback: the child already has its own private storage scope."""
    # 앱은 종류로 알림 앞 표시를 정해요: 좌석을 잡음 / 문제 / 찾는 중 소식.
    kind = {0: "reservation", 1: "error"}.get(status, "search")
    notifications.publish(owner, message, kind=kind)
    if status == 0 and seat_strategy != "random":
        existing = storage.get_payment_status(owner)
        if not existing or existing.completed or existing.cancelled:
            storage.save_payment_status(
                PaymentStatus(chat_id=owner, completed=False, reminder_active=True)
            )
    if status in (0, 1):
        session = storage.get_user_session(owner)
        if session:
            session.reset()
            storage.save_user_session(session)
        storage.delete_running_reservation(owner)
        storage.delete_resume_credentials(owner)
        storage.delete_app_session_start(owner)


class MobileSearchProcess(BackgroundReservationProcess):
    def _runtime_services(self):
        config = MobileConfig.from_env()
        storage = MobileStorage(secret=config.secret, url=config.redis_url)
        notifications = Notifications(
            IdentityStore(config.database),
            secret=config.secret,
            storage=storage,
            push_enabled=bool(config.fcm_credentials),
        )
        return storage, notifications

    def _send_callback(
        self, message, status=0, is_multi=False, total_seats=1, seat_strategy="consecutive"
    ):
        apply_result(self.storage, self.telegram, self.chat_id, message, status, seat_strategy)

    def run(self):
        # The parent confirms the child then writes its running record. Wait
        # for that write so a very quick result cannot be overwritten by it.
        for _ in range(100):
            record = self.storage.get_running_reservation(self.chat_id)
            if record and record.process_id == os.getpid():
                break
            time.sleep(0.05)
        else:
            raise SystemExit("Parent did not register this search")
        try:
            super().run()
        finally:
            self.storage.close()


if __name__ == "__main__":
    install_shutdown_handlers()
    with suppress(SearchStopped, KeyboardInterrupt):
        MobileSearchProcess().run()
