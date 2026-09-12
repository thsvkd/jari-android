"""
Routing of a single Telegram update, independent of how it was received.

The polling adapter hands each Telegram update here so routing is independent
from transport and straightforward to exercise directly in tests.
"""

from korail_bot.handlers.command_handler import CommandHandler
from korail_bot.handlers.conversation_handler import ConversationHandler
from korail_bot.services import (
    MultiReservationReminderService,
    PaymentReminderService,
    ReservationService,
    TelegramService,
)
from korail_bot.storage.base import StorageInterface
from korail_bot.telegramBot import keyboards
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)


class TelegramUpdateProcessor:
    """Dispatches Telegram updates to the command and conversation handlers."""

    def __init__(
        self,
        storage: StorageInterface,
        telegram_service: TelegramService,
        reservation_service: ReservationService,
        payment_reminder_service: PaymentReminderService,
    ):
        """
        Initialize the update processor.

        Args:
            storage: Storage interface
            telegram_service: Telegram messaging service
            reservation_service: Reservation service
            payment_reminder_service: Payment reminder service
        """
        self.storage = storage
        self.telegram = telegram_service
        self.reservation = reservation_service
        self.payment_reminder = payment_reminder_service

        # Initialize multi-reservation reminder service (singleton for thread tracking)
        self.multi_reminder = MultiReservationReminderService(storage, telegram_service)

        # Initialize handlers. The conversation is built first: /start hands
        # over to it when the chat already registered a Korail account, so the
        # command handler needs it to exist.
        self.conversation_handler = ConversationHandler(
            storage, telegram_service, reservation_service
        )
        self.command_handler = CommandHandler(
            storage,
            telegram_service,
            reservation_service,
            payment_reminder_service,
            conversation_handler=self.conversation_handler,
        )

    def process(self, update: dict) -> None:
        """
        Handle one Telegram update.

        Never raises: an update that cannot be processed must not stop the
        the poller from reading the next one.

        Args:
            update: Telegram update object as delivered by the Bot API
        """
        try:
            # An edit is not a new answer, so it is ignored.
            if "edited_message" in update:
                return

            # Being blocked or having the chat deleted is the user withdrawing
            # from the bot, and the Korail login they registered has no reason
            # to outlive that. This is the only notice Telegram gives.
            if "my_chat_member" in update:
                self.process_membership_change(update["my_chat_member"])
                return

            # A button press arrives as its own update kind, carrying no
            # message from the user at all.
            if "callback_query" in update:
                self.process_callback_query(update["callback_query"])
                return

            # Extract message
            try:
                message = update["message"]
                chat_id = int(message["chat"]["id"])
            except (KeyError, ValueError) as e:
                logger.error(f"Invalid message format: {e}")
                return

            # A Keyboard Button Mini App returns a service message rather
            # than text. Its data is client-controlled and is therefore
            # parsed and validated at the conversation boundary before any
            # session or search is changed.
            web_app_data = message.get("web_app_data")
            if isinstance(web_app_data, dict):
                logger.info(f"Received Mini App submission from chat_id={chat_id}")
                self.conversation_handler.handle_mini_app_data(chat_id, web_app_data.get("data"))
                return

            try:
                text = message["text"].strip()
            except (KeyError, AttributeError):
                logger.error("Invalid message format: text is missing")
                return

            logger.info(f"Received message from chat_id={chat_id}: {text}")

            # The magic string works wherever it is typed, not only at the
            # welcome screen, so an operator can claim a chat without walking
            # back to the start of a conversation. Checked before anything
            # else for the same reason: whatever state the chat is in, this
            # answer means "this chat is mine", not an answer to the question
            # on screen.
            if self.command_handler.claim_developer_mode(chat_id, text):
                return

            if text == keyboards.MINI_APP_CHAT_FALLBACK:
                self.command_handler.handle_chat_start(chat_id)
                return

            # Get user session to check progress
            session = self.storage.get_user_session(chat_id)
            in_progress = session.in_progress if session else False
            progress_num = session.last_action if session else 0

            logger.debug(f"chat_id={chat_id}, in_progress={in_progress}, progress={progress_num}")

            # A message sent while a payment reminder is running is just a
            # message. It used to be read as "I have paid": the reminders
            # stopped and the booking was recorded as settled, so someone who
            # typed anything at all lost the seat quietly, and someone who
            # paid without saying so was nagged to the deadline.
            #
            # The payment is watched now, and the answer comes from the
            # railway. Turning the reminders off is /notify_off, which says
            # what it does and claims nothing.

            # Handle /cancel command first (works in any state)
            if text == "/cancel":
                self.command_handler.handle_cancel(chat_id)
                return

            # Route commands BEFORE checking random seating state
            # This allows users to use /help, /status even during payment waiting
            if self.command_handler.is_command(text):
                self.command_handler.route_command(chat_id, text)
                return

            # A random-seating run books one seat at a time and waits before
            # taking the next. The wait ends on its own - the payment is
            # verified against the railway now, and the run goes on the moment
            # it is - so this is the "don't wait for me, carry on" override
            # rather than the only way forward it used to be.
            #
            # It says nothing about the payment. The seat is recorded as paid
            # for when the railway says so, and not before.
            current_seat = self.storage.get_current_seat_index(chat_id)
            if current_seat is not None:
                logger.info(f"Told to carry on past seat {current_seat} for chat_id={chat_id}")
                self.storage.mark_payment_ready(chat_id, current_seat)

                # The reminders keep running: the seats already taken still
                # have to be paid for, and more are about to be added.
                self.telegram.send_message(
                    chat_id,
                    f"▶️ {current_seat + 1}번째 좌석은 넘어가고 다음 좌석 예약을 시작합니다.\n\n"
                    f"결제 여부는 계속 확인해서 알려드립니다.",
                )

                return

            # Check if waiting for admin password (takes priority over everything)
            if self.storage.is_waiting_for_admin_password(chat_id):
                # User is waiting to enter admin password
                # Both outcomes are terminal: the handler already told the user.
                self.command_handler.handle_admin_password(chat_id, text)
                return

            # Renaming a saved search. Held outside the session deliberately:
            # tidying up favourites is not a step of the booking flow, and
            # someone halfway through booking must not have that flow
            # disturbed by it. Checked here, after the commands, so /cancel
            # is still the way out.
            renaming = self.storage.get_pending_favourite_rename(chat_id)
            if renaming:
                self.command_handler.handle_favourite_rename(chat_id, renaming, text)
                return

            # A reporting interval typed into the reply box that "직접 입력"
            # opens. Same shape as the rename above, and outside the session
            # for the same reason: choosing how often to hear from a search is
            # not a step of the flow that books one.
            if self.storage.is_waiting_for_notify_input(chat_id):
                self.command_handler.handle_notify_input(chat_id, text)
                return

            # Handle conversation flow (non-command messages)
            if in_progress:
                # Handle conversation flow
                self.conversation_handler.handle_message(chat_id, text)
                return

            # Nothing was asked and nothing is running, so nothing is said.
            #
            # This used to answer every stray line with "진행중인 예약
            # 프로세스가 없습니다", which is a reply to a message that was
            # never addressed to the bot. In a group it made the bot talk over
            # people; in a direct chat it turned thinking out loud into a
            # notification. Every branch above still runs first, so the inputs
            # a conversation is actually waiting on - a date, a time, an
            # interval, a favourite's new name, the developer magic string -
            # are read exactly as before. What falls through to here is only
            # what nothing was waiting for.
            logger.debug(f"Nothing was waiting on this text from chat_id={chat_id} - ignoring")

        except Exception as e:
            logger.error(f"Error handling update: {e}", exc_info=True)

    def process_membership_change(self, membership: dict) -> None:
        """
        Handle the user blocking the bot or deleting the chat.

        Telegram reports this as a new_chat_member status of 'kicked' (blocked)
        or 'left'. Either way the person is gone, and the Korail password they
        registered should not sit in Redis waiting for them to come back.

        A running search is left alone deliberately: it holds its own copy of
        the login and may be minutes away from catching a seat the user asked
        for. Ending searches is what /cancel and the watchdog are for.
        """
        try:
            chat_id = int(membership["chat"]["id"])
            status = membership["new_chat_member"]["status"]
        except (KeyError, TypeError, ValueError):
            logger.warning("Ignoring a my_chat_member update in an unexpected shape")
            return

        if status not in ("kicked", "left"):
            # Anything else is the user starting or unblocking the bot, which
            # needs nothing done: the next /start goes through the normal flow.
            logger.info(f"Chat membership for chat_id={chat_id} changed to {status}")
            return

        logger.info(f"chat_id={chat_id} {status} the bot - dropping what it left behind")
        try:
            self.storage.delete_onboarded_account(chat_id)
        except Exception as e:
            logger.error(f"Could not drop the account for chat_id={chat_id}: {e}")

        # Saved searches go too. They hold nothing secret - two stations, a
        # time window - but they are personal, and someone who left has not
        # asked for a record of the journeys they take to be kept.
        try:
            self.storage.delete_all_favourites(chat_id)
        except Exception as e:
            logger.error(f"Could not drop the favourites for chat_id={chat_id}: {e}")

    def process_callback_query(self, query: dict) -> None:
        """
        Handle one inline keyboard button press.

        A press is turned into the answer the user would have typed and given
        to the same conversation handler, so buttons and typing cannot drift
        apart: there is one place that validates a date, and one place that
        decides what comes next.

        Args:
            query: The callback_query object from the Bot API
        """
        query_id = query.get("id")
        if not isinstance(query_id, str):
            # Nothing to acknowledge and nothing to reply to.
            logger.warning("Ignoring a callback_query with no id")
            return

        data = query.get("data")
        if not isinstance(data, str):
            self.telegram.answer_callback_query(query_id)
            return

        message = query.get("message")
        message = message if isinstance(message, dict) else {}
        try:
            chat_id = int(message["chat"]["id"])
        except (KeyError, TypeError, ValueError):
            # Telegram stops sending the originating message once it is too
            # old to edit, and without it there is no chat to answer in.
            self.telegram.answer_callback_query(
                query_id, "너무 오래된 메시지입니다.\n/start 로 다시 시작해주세요.", show_alert=True
            )
            return

        message_id = message.get("message_id")
        message_id = message_id if isinstance(message_id, int) else None

        step, _, value = data.partition(":")
        logger.info(f"Button press from chat_id={chat_id}: {data}")

        if step == keyboards.STEP_CANCEL:
            self.telegram.answer_callback_query(query_id)
            self._settle_keyboard(chat_id, message_id, message, data)
            self.command_handler.handle_cancel(chat_id)
            return

        # Answering for a search that died, which is not a step of the
        # conversation and so is handled before the progress check below. The
        # conversation has already been reset by the time this message is sent
        # - there is no progress for it to match - and unlike a question in
        # the flow, the answer is just as good an hour later: the search stays
        # dead until something is done about it.
        if step == keyboards.STEP_DEAD:
            self.telegram.answer_callback_query(query_id)
            self._settle_keyboard(chat_id, message_id, message, data)
            self._handle_dead_search(chat_id, value)
            return

        # What to do about a seat that is booked and unpaid. Not a step of the
        # conversation either - the booking that took the seat is over - and
        # handled before the progress check for the same reason the dead-search
        # buttons are: the reservation goes on sitting there unpaid however
        # long the message waits to be read.
        if step == keyboards.STEP_PAY:
            self.telegram.answer_callback_query(query_id)
            self._settle_keyboard(chat_id, message_id, message, data)
            self.command_handler.handle_payment_callback(chat_id, value)
            return

        # Asking to keep using the bot. Like the dead-search buttons, this is
        # not a step of the conversation - the session was reset when the
        # trial ran out - and the answer stays valid however long it sits.
        if step == keyboards.STEP_ACCESS:
            self.telegram.answer_callback_query(query_id)
            self._settle_keyboard(chat_id, message_id, message, data)
            if value == keyboards.ACCESS_ASK:
                self.conversation_handler.request_access(chat_id)
            return

        # Choosing how often a running search reports in. A setting, not a
        # step of the conversation - it is reached by /notify at any time, and
        # the answer is as good an hour later as it was at the time.
        if step == keyboards.STEP_NOTIFY:
            self.telegram.answer_callback_query(query_id)
            self._settle_keyboard(chat_id, message_id, message, data)
            self.command_handler.handle_notify_callback(chat_id, value)
            return

        # Saved searches. Its screens are walked in place - list, detail, back
        # to the list - so the keyboard is left for the handler to replace
        # rather than settled away here.
        if step == keyboards.STEP_FAV:
            self.telegram.answer_callback_query(query_id)
            self.command_handler.handle_favourite_callback(chat_id, message_id, value)
            return

        # The operator's own lists. Guarded by the same admin check the
        # commands that open them use, because a keyboard is only as private
        # as the chat it was sent to - and messages get forwarded.
        if step in (keyboards.STEP_APPROVE, keyboards.STEP_USERS):
            self.telegram.answer_callback_query(query_id)
            if not self.command_handler.may_administer(chat_id):
                self._remove_keyboard(chat_id, message_id)
                return
            self.command_handler.handle_access_callback(chat_id, message_id, step, value)
            return

        expected_progress = keyboards.STEP_PROGRESS.get(step)
        if expected_progress is None:
            logger.warning(f"Unknown callback step {step!r} from chat_id={chat_id}")
            self.telegram.answer_callback_query(
                query_id, "알 수 없는 버튼입니다.\n/start 로 다시 시작해주세요.", show_alert=True
            )
            return

        # The guard that makes buttons safe. Old messages keep their keyboards
        # forever, and every value here is a digit or a date that the current
        # step would accept without complaint - a tap on last week's "특실만"
        # would be recorded as four passengers.
        session = self.storage.get_user_session(chat_id)
        current_progress = session.last_action if session else None
        if current_progress != expected_progress:
            logger.info(
                f"Ignoring a stale button from chat_id={chat_id}: "
                f"{data} expects progress {expected_progress}, session is at {current_progress}"
            )
            self.telegram.answer_callback_query(
                query_id,
                "이미 지나간 단계의 버튼입니다.\n가장 최근 메시지에서 선택해주세요.",
                show_alert=True,
            )
            self._remove_keyboard(chat_id, message_id)
            return

        if value == keyboards.MANUAL:
            self.telegram.answer_callback_query(query_id, "원하는 값을 직접 입력해주세요.")
            self._remove_keyboard(chat_id, message_id)
            return

        self.telegram.answer_callback_query(query_id)

        # A repeatable step is still being answered - ticking one train off a
        # list does not close the list - so its keyboard is left alone and the
        # handler redraws it. Everything else is settled here, before
        # dispatching rather than after: the handler sends the next question,
        # and the answer to this one belongs above it.
        if step not in keyboards.REPEATABLE_STEPS and data not in keyboards.KEEPS_QUESTION_OPEN:
            self._settle_keyboard(chat_id, message_id, message, data)

        self.conversation_handler.handle_message(chat_id, value)

    def _handle_dead_search(self, chat_id: int, choice: str) -> None:
        """
        Act on what the user chose to do about a search that stopped.

        Both outcomes are terminal and both report themselves: resuming sends
        the search's own start notice, discarding confirms. Neither returns to
        the conversation, which was reset when the search died.
        """
        from korail_bot.telegramBot.messages import Messages

        if choice == keyboards.DEAD_RESUME:
            self.reservation.resume_dead_search(chat_id)
            return

        if choice == keyboards.DEAD_DISCARD:
            if self.reservation.discard_dead_search(chat_id):
                self.telegram.send_message(chat_id, Messages.SEARCH_DEAD_DISCARDED)
            else:
                self.telegram.send_message(chat_id, Messages.SEARCH_DEAD_GONE)
            return

        logger.warning(f"Unknown dead-search choice {choice!r} from chat_id={chat_id}")

    def _settle_keyboard(
        self, chat_id: int, message_id: int | None, message: dict, data: str
    ) -> None:
        """
        Record the choice on the message that offered it, and take its buttons away.

        Leaves the chat readable after the fact - a transcript of questions
        with no answers is not much of a transcript - and stops the same
        question being answered twice.

        Presentational, and contained accordingly. It runs before the answer
        is dispatched so the record lands above the next question, which puts
        it on the path of something that matters; nothing that happens here
        may cost the user the choice they just made.
        """
        if message_id is None:
            return

        try:
            text = message.get("text")
            if not isinstance(text, str):
                # A message without text cannot be edited into one that has it.
                self._remove_keyboard(chat_id, message_id)
                return

            label = keyboards.button_label(message.get("reply_markup"), data)
            self.telegram.edit_message_text(
                chat_id,
                message_id,
                f"{text}\n\n👉 선택: {label}" if label else text,
                reply_markup=keyboards.empty_keyboard(),
            )
        except Exception as e:
            logger.warning(f"Could not record the choice on message {message_id}: {e}")

    def _remove_keyboard(self, chat_id: int, message_id: int | None) -> None:
        """Take the buttons off a message without touching its text."""
        if message_id is None:
            return

        try:
            self.telegram.edit_message_reply_markup(chat_id, message_id, keyboards.empty_keyboard())
        except Exception as e:
            logger.warning(f"Could not remove the keyboard from message {message_id}: {e}")
