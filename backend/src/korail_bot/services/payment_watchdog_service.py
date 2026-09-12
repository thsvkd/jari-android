"""
Noticing that a payment happened, from the app rather than from a search.

The search process that takes a seat stays behind and watches it: it holds the
only logged-in session there is at that moment, so it can ask the railway
whether the reservation is still unpaid and tell the user the moment it is not.
That covers the ordinary booking and covers it well.

It leaves two holes, and both of them are silence exactly where the user is
waiting to hear something.

- **A random-seating run has no watcher at all.** It books a seat at a time and
  never gets to the watching part, so "payment complete" there still means the
  user said so.
- **A restart takes the watcher with it.** The search process is killed, its
  record is already gone (the callback cleared it when the seat was booked), so
  nothing resumes it. The reminders keep coming from Redis while the one thing
  that could stop them is dead.

This fills both from the app, which survives its own restarts and can log in
with the account the chat registered. It is deliberately the second choice: a
watcher already logged in beats one that has to log in again, so this only
takes what nobody else has claimed.
"""

import os
import threading
from datetime import datetime

from korail_bot.config.settings import settings
from korail_bot.models import (
    MultiReservationStatus,
    PaymentStatus,
    ReservationOutcome,
    ReservationPaymentStatus,
)
from korail_bot.services.telegram_service import TelegramService
from korail_bot.storage.base import StorageInterface
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)


class PaymentWatchdogService:
    """Watches the payments nothing else is watching, and reports what it finds."""

    def __init__(self, storage: StorageInterface, telegram_service: TelegramService):
        """
        Initialize the watchdog.

        Args:
            storage: Where the payment records and the watch claims live
            telegram_service: How the user is told
        """
        self.storage = storage
        self.telegram = telegram_service
        # Who this watcher is, for the claim. Per process, so a restart is a
        # different owner and cannot renew the claim its predecessor left.
        self.owner = f"app:{os.getpid()}"
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Whether the last pass found anything at all to watch. Nothing
        # pending means nothing worth scanning Redis for every few seconds,
        # and a payment window is minutes long against hours of quiet.
        self._busy = False
        # One logged-in client per chat, kept between passes. Logging in on
        # every pass would be a login every few seconds per pending payment,
        # which is far more than the listing it exists to perform.
        self._clients: dict[int, object] = {}

    # ==================== The thread ====================

    def start(self) -> None:
        """Start watching, in a background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Payment watchdog is already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run, name="payment-watchdog", daemon=True)
        self._thread.start()
        logger.info("Payment watchdog started")

    def stop(self) -> None:
        """Ask the loop to finish; it wakes from its sleep to do so."""
        self._stop_event.set()

    def run(self) -> None:
        """
        Check the payments, over and over, until asked to stop.

        Never raises, for the reason the search watchdog does not: this is the
        whole body of a thread, and a watchdog that dies quietly is worse than
        none because it is trusted.
        """
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                logger.error(f"Payment watchdog pass failed: {e}", exc_info=True)

            self._stop_event.wait(self._next_pass_in())

        logger.info("Payment watchdog stopped")

    def _next_pass_in(self) -> float:
        """
        How long to wait before looking again.

        Quick while there is a payment outstanding - somebody is standing
        there having just paid, and the point of this is telling them within
        seconds. Slow when there is not: a payment window is minutes long and
        the bot spends hours between them, and scanning Redis every few
        seconds for nothing is a cost with no reader.

        Whether the payments found are this watcher's to check does not
        change the answer. The one being watched by a search process is the
        one this has to take over within seconds when that process is killed.
        """
        if self._busy:
            return settings.PAYMENT_VERIFY_INTERVAL_SECONDS
        return max(settings.WATCHDOG_POLL_SECONDS, settings.PAYMENT_VERIFY_INTERVAL_SECONDS)

    # ==================== One pass ====================

    def tick(self) -> int:
        """
        One pass over the payments nobody else is watching.

        Returns:
            How many reservations were settled this pass
        """
        chats = self._chats_with_unsettled_payments()
        self._busy = bool(chats)

        settled = 0
        for chat_id in chats:
            try:
                settled += self._check(chat_id)
            except Exception as e:
                # One chat's railway being unreachable is not a reason to
                # leave the others unwatched.
                logger.error(f"Could not check the payment for chat_id={chat_id}: {e}")

        # A payment settled by the other watcher simply stops appearing here,
        # so this is the only place its login is let go of. Without it the
        # clients accumulate for as long as the app runs.
        for chat_id in [held for held in self._clients if held not in chats]:
            self._let_go(chat_id)

        return settled

    def _chats_with_unsettled_payments(self) -> list[int]:
        """
        Every chat holding a reservation whose fate is not yet known.

        Read from the records rather than from the user-facing view, which
        hides a reservation once its deadline passes: a seat lost is as much
        news as a seat paid for, and the record is where that is decided.
        """
        chats = []

        for status in self.storage.get_all_payment_statuses():
            if self._unsettled_single(status):
                chats.append(status.chat_id)

        for multi in self.storage.get_all_multi_reservation_statuses():
            if self._unsettled_seats(multi) and multi.chat_id not in chats:
                chats.append(multi.chat_id)

        return chats

    @staticmethod
    def _unsettled_single(status: PaymentStatus) -> bool:
        """Whether one booking is still waiting on an answer from the railway."""
        # No number means nothing to ask about: a record written before the
        # seat details landed, or by a build that did not write them.
        return bool(status.reservation_id) and not status.completed and not status.cancelled

    @staticmethod
    def _unsettled_seats(multi: MultiReservationStatus) -> bool:
        """Whether a random-seating run still has a seat waiting on an answer."""
        return any(r.status == ReservationPaymentStatus.PENDING for r in multi.reservations)

    def _check(self, chat_id: int) -> int:
        """
        Ask the railway about one chat's reservations, and act on the answer.

        Returns:
            How many were settled
        """
        if not self.storage.claim_payment_watch(
            chat_id, self.owner, settings.PAYMENT_WATCH_LEASE_SECONDS
        ):
            # The search process that took the seat is on it, and it is
            # already logged in. Nothing to add.
            return 0

        rail = self._client(chat_id)
        if rail is None:
            return 0

        settled = self._settle_single(chat_id, rail) + self._settle_seats(chat_id, rail)

        if not self._still_watching(chat_id):
            self._let_go(chat_id)

        return settled

    # ==================== Acting on the answer ====================

    def _settle_single(self, chat_id: int, rail) -> int:
        """Report a single booking as paid for, as given up, or as lost."""
        from korail_bot.telegramBot.messages import Messages

        status = self.storage.get_payment_status(chat_id)
        if not status or not self._unsettled_single(status):
            return 0

        outcome = rail.reservation_outcome(
            status.reservation_id,
            train_no=status.train_no,
            dep_date=status.dep_date,
            dep_time=status.dep_time,
        )

        if outcome is ReservationOutcome.UNKNOWN:
            # The railway could not be asked, or could not say which of paid
            # and cancelled this was. Neither is an answer, and announcing
            # either is the guess this exists to remove.
            return 0

        if outcome is ReservationOutcome.OUTSTANDING:
            if not self._past_deadline(status.expires_at):
                return 0
            message = Messages.PAYMENT_EXPIRED_VERIFIED
            logger.warning(f"Reservation {status.reservation_id} expired unpaid")
        elif outcome is ReservationOutcome.PAID:
            message = Messages.PAYMENT_VERIFIED
            logger.info(f"Reservation {status.reservation_id} is settled - payment went through")
        else:
            message = Messages.PAYMENT_RELEASED
            # The record has to say it was given back rather than paid for.
            # `completed` alone only means "stop reminding", and reading it
            # later as a payment is how this went wrong in the first place.
            status.cancelled = True
            logger.info(f"Reservation {status.reservation_id} went back unpaid")

        # Written before the message is sent, and read back first: the reminder
        # loop and the other watcher both go by this flag, and a message sent
        # against a record that still says "pending" can be sent twice.
        status.completed = True
        status.reminder_active = False
        self.storage.save_payment_status(status)

        self.telegram.send_message(chat_id, message)
        return 1

    def _settle_seats(self, chat_id: int, rail) -> int:
        """Report the seats of a random-seating run, one at a time."""
        multi = self.storage.get_multi_reservation_status(chat_id)
        if not multi:
            return 0

        settled = []
        for reservation in sorted(multi.reservations, key=lambda r: r.seat_number):
            if reservation.status != ReservationPaymentStatus.PENDING:
                continue

            outcome = rail.reservation_outcome(
                reservation.reservation_id,
                train_no=reservation.train_no,
                dep_date=reservation.dep_date,
                dep_time=reservation.dep_time,
            )

            if outcome is ReservationOutcome.UNKNOWN:
                continue

            if outcome is ReservationOutcome.OUTSTANDING:
                if not self._past_deadline(reservation.expires_at):
                    continue
                reservation.status = ReservationPaymentStatus.EXPIRED
            elif outcome is ReservationOutcome.PAID:
                reservation.status = ReservationPaymentStatus.PAID
            else:
                reservation.status = ReservationPaymentStatus.CANCELLED

            settled.append(reservation)

        if not settled:
            return 0

        self.storage.save_multi_reservation_status(multi)

        for reservation in settled:
            paid = reservation.status == ReservationPaymentStatus.PAID
            if paid:
                # Only a payment releases the next seat. A seat given back was
                # never paid for, and moving the run on from it would be the
                # thing _release_the_next_seat exists to prevent.
                self._release_the_next_seat(chat_id, reservation.seat_number)
            self.telegram.send_message(
                chat_id,
                self._seat_message(reservation.status).format(
                    seat=reservation.seat_number,
                    train=reservation.train_info or "예약 정보 없음",
                    remaining=multi.get_pending_count(),
                ),
            )

        return len(settled)

    @staticmethod
    def _seat_message(status: ReservationPaymentStatus) -> str:
        """What to tell the user about one seat, given what became of it."""
        from korail_bot.telegramBot.messages import Messages

        if status == ReservationPaymentStatus.PAID:
            return Messages.PAYMENT_VERIFIED_SEAT
        if status == ReservationPaymentStatus.CANCELLED:
            return Messages.PAYMENT_RELEASED_SEAT
        return Messages.PAYMENT_EXPIRED_SEAT

    def _release_the_next_seat(self, chat_id: int, seat_number: int) -> None:
        """
        Let a random-seating run carry on to the seat after this one.

        That run books one seat, waits to be told it was paid for, and only
        then takes the next - and being told meant the user sending a message.
        Now that the payment is a fact rather than a claim, the fact is what
        releases it, and the user has nothing to do but pay.
        """
        current = self.storage.get_current_seat_index(chat_id)
        if current is None or current + 1 != seat_number:
            # Not the seat the search is waiting on. Confirming a different
            # one would let it move on from a seat nobody has paid for.
            return

        self.storage.mark_payment_ready(chat_id, current)
        logger.info(f"Seat {seat_number} paid for - releasing the next seat for chat_id={chat_id}")

    @staticmethod
    def _past_deadline(deadline: datetime | None) -> bool:
        """
        Whether the railway has stopped holding this seat.

        A record with no deadline is never called lost. It was written before
        the details were, and inventing one would announce the loss of a seat
        that may still be there to pay for.
        """
        from korail_bot.utils.timezone import utc_now

        if not deadline:
            return False
        now = utc_now() if deadline.tzinfo else datetime.now()
        return now >= deadline

    # ==================== Logging in ====================

    def _client(self, chat_id: int):
        """
        A logged-in client for the railway holding this chat's reservations.

        Kept between passes. At this cadence a login per pass would be several
        logins a minute for a payment that needs one listing call each time.
        """
        client = self._clients.get(chat_id)
        if client is not None:
            return client

        username, password = self._credentials(chat_id)
        if not username or not password:
            # Nothing to log in with. Not an error to repeat every pass: the
            # payment is still watched by whoever took the seat, if anyone,
            # and the user is still being reminded.
            logger.debug(f"No account to verify the payment with for chat_id={chat_id}")
            return None

        client = self._rail_service()
        if not client.login(username, password):
            logger.warning(f"Could not log in to verify a payment for {chat_id}")
            return None

        self._clients[chat_id] = client
        return client

    def _credentials(self, chat_id: int) -> tuple[str | None, str | None]:
        """The login to check with: the registered account, or the fixed one."""
        account = self.storage.get_onboarded_account(chat_id)
        if account:
            return account.korail_id, account.korail_pw

        if self.storage.is_developer(chat_id) and settings.has_preconfigured_credentials():
            return settings.preconfigured_credentials()

        return None, None

    @staticmethod
    def _rail_service():
        """A Korail client."""
        from korail_bot.services.korail_service import KorailService

        return KorailService()

    # ==================== Letting go ====================

    def _still_watching(self, chat_id: int) -> bool:
        """Whether this chat has anything left to watch."""
        status = self.storage.get_payment_status(chat_id)
        if status and self._unsettled_single(status):
            return True

        multi = self.storage.get_multi_reservation_status(chat_id)
        return bool(multi and self._unsettled_seats(multi))

    def _let_go(self, chat_id: int) -> None:
        """Drop the claim and the login once there is nothing left to check."""
        self._clients.pop(chat_id, None)
        self.storage.release_payment_watch(chat_id, self.owner)
