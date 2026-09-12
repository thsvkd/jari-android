"""Loopback callback used by background reservation processes."""

from flask import make_response, request
from flask_restful import Resource

from korail_bot.api.auth import verify_internal_request
from korail_bot.services import (
    MultiReservationReminderService,
    PaymentReminderService,
    TelegramService,
)
from korail_bot.storage.base import StorageInterface
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)


class ReservationCallbackAPI(Resource):
    """Accept reservation results from this app's background processes."""

    def __init__(
        self,
        storage: StorageInterface,
        telegram_service: TelegramService,
        payment_reminder_service: PaymentReminderService,
        **kwargs,
    ):
        """
        Initialize the internal callback handler.

        Args:
            storage: Storage interface
            telegram_service: Telegram messaging service
            payment_reminder_service: Payment reminder service
        """
        super().__init__(**kwargs)
        self.storage = storage
        self.telegram = telegram_service
        self.payment_reminder = payment_reminder_service
        self.multi_reminder = MultiReservationReminderService(storage, telegram_service)

    def get(self):
        """
        Handle GET request for callbacks from background processes.

        This is used by the background reservation process to notify
        the bot about reservation results. It can send arbitrary text to
        arbitrary chats, so it is restricted to our own processes.
        """
        if not verify_internal_request():
            return make_response("Forbidden", 403)

        try:
            # Extract parameters. Everything off the wire is str or missing;
            # the converted values get names of their own rather than being
            # assigned back over the raw ones, so each name means one thing.
            chat_id_arg = request.args.get("chatId")
            msg = request.args.get("msg")
            status = request.args.get("status")
            is_multi_arg = request.args.get("isMulti", "0")
            total_seats_arg = request.args.get("totalSeats", "1")
            seat_strategy = request.args.get("seatStrategy", "consecutive")

            # Checked one at a time rather than with all([...]): that form
            # rejects the same inputs but tells neither a type checker nor a
            # reader which of the three was missing.
            if not chat_id_arg or not msg or not status:
                logger.warning("Incomplete callback parameters")
                return make_response("OK")

            chat_id = int(chat_id_arg)
            is_multi = is_multi_arg == "1"
            total_seats = int(total_seats_arg)

            logger.info(
                f"Callback from background process: chat_id={chat_id}, status={status}, "
                f"is_multi={is_multi}, total_seats={total_seats}, seat_strategy={seat_strategy}"
            )

            # Send message to user
            self.telegram.send_message(chat_id, msg)

            # Handle different status codes
            # status=0: Complete success (all reservations done)
            # status=1: Error/failure
            # status=2: Partial success (random seating intermediate notification)

            # status=1 means the search process gave up and exited. Its record
            # has to go with it, or /status keeps reporting a search that is
            # not running and a restart would try to resume it.
            if str(status) == "1":
                logger.info(f"Reservation process failed for chat_id={chat_id}")
                self.storage.delete_running_reservation(chat_id)
                self.storage.delete_resume_credentials(chat_id)
                self.storage.delete_app_session_start(chat_id)

                session = self.storage.get_user_session(chat_id)
                if session:
                    session.reset()
                    self.storage.save_user_session(session)

                return make_response("OK")

            if str(status) == "2":
                # Partial reservation notification (random seating)
                logger.info(f"Partial reservation notification for chat_id={chat_id}")

                # Check if multi-reservation status exists and start reminders if needed
                multi_status = self.storage.get_multi_reservation_status(chat_id)
                if multi_status:
                    # Start multi-reservation reminders (checks for duplicates internally)
                    self.multi_reminder.start_reminders(chat_id)

                # Message already sent above, no further action needed
                # The poller handles the user's payment confirmation.
                return make_response("OK")

            # If reservation successful (status == 0)
            if str(status) == "0":
                logger.info(f"Reservation successful for chat_id={chat_id}")

                # Reset user session
                session = self.storage.get_user_session(chat_id)
                if session:
                    session.reset()
                    self.storage.save_user_session(session)

                # Start appropriate payment reminders
                # For random seating, multi-reminder is already running (started on first seat)
                # Don't start duplicate reminder service
                if seat_strategy == "random":
                    logger.info(
                        f"Random seating complete - multi-reminder already running for chat_id={chat_id}"
                    )
                    # Multi-reservation reminder was started on first partial callback (status=2)
                    # It will continue until all seats are paid or expired
                elif is_multi:
                    logger.info(f"Starting multi-reservation reminders for chat_id={chat_id}")
                    # Consecutive seating with multiple passengers
                    # TODO: This path may need multi-reminder support in the future
                    self.payment_reminder.start_reminders(chat_id)
                else:
                    logger.info(f"Starting single payment reminders for chat_id={chat_id}")
                    self.payment_reminder.start_reminders(chat_id)

                # Clean up running reservation. The search is over, so the
                # credentials kept for a restart have no reason to exist.
                self.storage.delete_running_reservation(chat_id)
                self.storage.delete_resume_credentials(chat_id)
                self.storage.delete_app_session_start(chat_id)

            return make_response("OK")

        except Exception as e:
            logger.error(f"Error handling callback: {e}", exc_info=True)
            return make_response("OK")
