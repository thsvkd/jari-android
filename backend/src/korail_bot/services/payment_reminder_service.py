"""Payment reminder service."""

import threading
import time
from datetime import UTC, datetime

import requests

from korail_bot.config.settings import settings
from korail_bot.models import PaymentStatus
from korail_bot.services.telegram_service import MessageTemplates, TelegramService
from korail_bot.storage.base import StorageInterface
from korail_bot.telegramBot.messages import Messages
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)


class PaymentReminderService:
    """
    Service for sending payment reminder notifications.

    Sends periodic reminders to users to complete payment within the time limit.
    """

    def __init__(self, storage: StorageInterface, telegram_service: TelegramService):
        """
        Initialize payment reminder service.

        Args:
            storage: Storage interface for tracking payment status
            telegram_service: Telegram service for sending reminders
        """
        self.storage = storage
        self.telegram = telegram_service
        self.timeout_minutes = settings.PAYMENT_TIMEOUT_MINUTES
        self.interval_seconds = settings.PAYMENT_REMINDER_INTERVAL_SECONDS

    def start_reminders(self, chat_id: int) -> None:
        """
        Start sending payment reminders to a user (in background thread).

        Sends reminders at configured intervals until:
        - User confirms payment
        - Timeout expires

        Args:
            chat_id: Telegram chat ID to send reminders to
        """
        # Check if there's already an active reminder
        existing_status = self.storage.get_payment_status(chat_id)
        if existing_status and existing_status.reminder_active:
            logger.warning(f"Reminder already active for chat_id={chat_id}, skipping duplicate")
            return

        # Initialize payment status
        payment_status = PaymentStatus(
            chat_id=chat_id, completed=False, created_at=datetime.now(UTC), reminder_active=True
        )
        self.storage.save_payment_status(payment_status)

        logger.info(
            f"Starting payment reminders for chat_id={chat_id} in background thread, "
            f"timeout={self.timeout_minutes}min, interval={self.interval_seconds}sec"
        )

        # Start reminder loop in background thread (non-blocking)
        thread = threading.Thread(target=self._reminder_loop, args=(chat_id,), daemon=True)
        thread.start()

    def _reminder_loop(self, chat_id: int) -> None:
        """
        Reminder loop that runs in background thread.

        Args:
            chat_id: Telegram chat ID
        """
        try:
            total_seconds = self.timeout_minutes * 60

            # Stops at the deadline rather than one interval past it. The
            # overshoot cost ten seconds when reminders were ten seconds
            # apart; at a minute apart it would hold the timeout message back
            # for a whole minute after the seat was already gone.
            for elapsed in range(self.interval_seconds, total_seconds + 1, self.interval_seconds):
                time.sleep(self.interval_seconds)

                # Payment settled, or the window was closed by /cancel. Both
                # paths have already told the user; stop quietly rather than
                # sending a second message about it.
                if self.check_payment_completed(chat_id):
                    self.deactivate_reminders(chat_id)
                    return

                # Or the user simply asked for quiet. Read out of Redis rather
                # than held here, because /notify_off is served by the update
                # handler and this loop is a thread it cannot reach into.
                if self.is_silenced(chat_id):
                    logger.info(f"Reminders were turned off for chat_id={chat_id}")
                    return

                # Calculate remaining time
                remaining_seconds = total_seconds - elapsed

                # Send reminder if time remaining
                if remaining_seconds > 0:
                    remaining_minutes = remaining_seconds // 60
                    remaining_secs = remaining_seconds % 60
                    self._send_reminder(chat_id, remaining_minutes, remaining_secs)

            # Final check after timeout
            if not self.check_payment_completed(chat_id):
                self._send_timeout_message(chat_id)

            # Deactivate reminder after completion or timeout
            self.deactivate_reminders(chat_id)

        except Exception as e:
            logger.error(f"Error in reminder loop for chat_id={chat_id}: {e}", exc_info=True)

    def check_payment_completed(self, chat_id: int) -> bool:
        """
        Check if payment has been completed for a chat ID.

        Args:
            chat_id: Telegram chat ID

        Returns:
            True if payment completed, False otherwise
        """
        try:
            # Try internal storage first
            payment_status = self.storage.get_payment_status(chat_id)
            if payment_status and payment_status.completed:
                return True

            # Also check via API (for compatibility)
            callback_url = f"{settings.CALLBACK_BASE_URL}/check_payment"
            params: dict[str, str | int] = {
                "chatId": chat_id,
                "token": settings.INTERNAL_CALLBACK_TOKEN,
            }
            response = requests.get(callback_url, params=params, timeout=5)
            return response.json().get("completed", False)

        except Exception as e:
            logger.error(f"Error checking payment status for chat_id={chat_id}: {e}")
            return False

    def is_silenced(self, chat_id: int) -> bool:
        """Whether the user has asked for the reminders to stop."""
        payment_status = self.storage.get_payment_status(chat_id)
        return bool(payment_status and not payment_status.reminder_active)

    def silence(self, chat_id: int) -> bool:
        """
        Stop reminding, without claiming anything about the payment.

        This is the whole difference from what used to happen here. Any
        message at all was read as "I have paid": the reminders stopped and
        the record was marked settled, so someone who typed "잠깐만" lost the
        seat quietly and someone who paid without saying so was nagged to the
        deadline. Turning the reminders off is now just that - the payment is
        still watched, and whatever it turns out to be is still reported.

        Args:
            chat_id: Telegram chat ID

        Returns:
            True when there were reminders to stop
        """
        payment_status = self.storage.get_payment_status(chat_id)
        if not payment_status or not payment_status.reminder_active:
            return False

        payment_status.reminder_active = False
        self.storage.save_payment_status(payment_status)
        logger.info(f"Payment reminders silenced for chat_id={chat_id}")
        return True

    def _send_reminder(self, chat_id: int, minutes: int, seconds: int) -> None:
        """Send a payment reminder message."""
        message = MessageTemplates.payment_reminder(minutes, seconds)
        self.telegram.send_message(chat_id, message)
        logger.debug(f"Sent payment reminder to chat_id={chat_id}, remaining={minutes}m {seconds}s")

    def _send_timeout_message(self, chat_id: int) -> None:
        """Send reminder timeout message (10 minutes elapsed)."""
        self.telegram.send_message(chat_id, Messages.PAYMENT_REMINDER_TIMEOUT)
        logger.warning(f"Payment reminder timeout for chat_id={chat_id}")

    def deactivate_reminders(self, chat_id: int, completed: bool = False) -> None:
        """
        Stop reminders for a chat ID.

        Public because callers outside the reminder loop need it: /cancel ends
        the payment window along with everything else, and used to write
        `completed` and `reminder_active` by hand instead, leaving two places
        that had to agree on what stopping a reminder means.

        Args:
            chat_id: Telegram chat ID
            completed: Also mark the payment settled, so the loop does not
                       treat the silence as a timeout and warn the user about
                       a reservation they have already dealt with.
        """
        payment_status = self.storage.get_payment_status(chat_id)
        if not payment_status:
            return

        payment_status.reminder_active = False
        if completed:
            payment_status.completed = True
        self.storage.save_payment_status(payment_status)
        logger.info(f"Deactivated reminder for chat_id={chat_id}")
