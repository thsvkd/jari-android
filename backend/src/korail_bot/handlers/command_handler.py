"""Command handler for Telegram bot commands."""

import hmac

from korail_bot import __version__
from korail_bot.config.settings import settings
from korail_bot.models import UserProgress, UserSession
from korail_bot.models.favourite import MAX_NAME_LENGTH
from korail_bot.release_notes import notes_for
from korail_bot.runtime import uptime_seconds
from korail_bot.services import (
    AccessService,
    MessageTemplates,
    MultiReservationReminderService,
    PaymentReminderService,
    PendingPaymentService,
    ReservationService,
    TelegramService,
)
from korail_bot.storage.base import StorageInterface
from korail_bot.telegramBot import keyboards
from korail_bot.utils.formatting import format_uptime
from korail_bot.utils.logger import LoggerFactory, get_logger
from korail_bot.utils.privacy import mask_phone
from korail_bot.utils.timezone import format_user_datetime

logger = get_logger(__name__)


class CommandHandler:
    """Handles Telegram bot commands like /start, /cancel, etc."""

    def __init__(
        self,
        storage: StorageInterface,
        telegram_service: TelegramService,
        reservation_service: ReservationService,
        payment_reminder_service: PaymentReminderService,
        conversation_handler=None,
    ):
        """
        Initialize command handler.

        Args:
            storage: Storage interface
            telegram_service: Telegram messaging service
            reservation_service: Reservation service
            payment_reminder_service: Payment reminder service
            conversation_handler: Used by /start to log in with an account the
                chat registered earlier, which is the conversation's job and
                is done there. Optional so that tests exercising the plain
                commands do not have to build one.
        """
        self.storage = storage
        self.telegram = telegram_service
        self.reservation = reservation_service
        self.payment_reminder = payment_reminder_service
        self.conversation = conversation_handler
        # Shares the conversation's view of who is allowed, so an approval
        # granted here is visible to the gate immediately.
        self.access = (
            conversation_handler.access if conversation_handler else AccessService(storage)
        )
        # Everything /status has to say about a seat that is already booked,
        # and the only way to give one back short of the railway's own site.
        self.pending_payments = PendingPaymentService(storage, telegram_service)
        # The other half of the payment reminders: the multi-seat ones are run
        # by their own service, and /notify_off has to reach both.
        self.multi_reminder = MultiReservationReminderService(storage, telegram_service)

    def handle_start(self, chat_id: int, start_parameter: str = "") -> None:
        """
        Handle /start command.

        Args:
            chat_id: Telegram chat ID
            start_parameter: Optional payload from a Telegram deep link
        """
        logger.info(f"Handling /start for chat_id={chat_id}")

        # Profile and menu-button Mini Apps cannot use KeyboardButton's
        # sendData service message. Their compact, untrusted conditions arrive
        # as a deep-link parameter and go through the same validation before
        # an ordinary /start is allowed to reset or resume anything.
        if (
            start_parameter
            and self.conversation
            and self.conversation.handle_mini_app_start_parameter(chat_id, start_parameter)
        ):
            return

        # Get or create user session
        session = self.storage.get_user_session(chat_id)
        if not session:
            session = UserSession(chat_id=chat_id, in_progress=False, last_action=UserProgress.INIT)
            self.storage.save_user_session(session)

        if self.conversation:
            # A booking left half-finished is offered back before anything is
            # written over it. Starting over stays one press away; losing a
            # dozen answers without being asked does not.
            if self.conversation.offer_draft(chat_id, session):
                return

            if self.conversation.offer_mini_app(chat_id, session):
                return

            self.conversation.begin_flow(chat_id, session)
            return

        # No conversation handler wired up - nothing can log in, so the
        # welcome is all there is to send.
        session.in_progress = True
        session.last_action = UserProgress.STARTED
        self.storage.save_user_session(session)
        self.telegram.send_message(
            chat_id,
            MessageTemplates.welcome_message(skip_login_prompts=True),
            reply_markup=keyboards.start_confirm_keyboard(),
        )

    def handle_chat_start(self, chat_id: int) -> None:
        """Start the original chat flow from the Mini App fallback button."""
        if not self.conversation:
            self.handle_start(chat_id)
            return
        session = self.storage.get_user_session(chat_id) or UserSession(chat_id=chat_id)
        if self.conversation.offer_draft(chat_id, session):
            return
        self.conversation.begin_flow(chat_id, session)

    def handle_onboarding(self, chat_id: int) -> None:
        """
        Handle /onboarding (/init): register a Korail account.

        Separate from /start because re-registering is a thing people need to
        do on purpose - a changed password, a different account - and /start
        deliberately skips straight past the login once one is stored.
        """
        logger.info(f"Handling /onboarding for chat_id={chat_id}")

        session = self.storage.get_user_session(chat_id)
        if not session:
            session = UserSession(chat_id=chat_id, in_progress=False, last_action=UserProgress.INIT)

        session.in_progress = True
        session.last_action = UserProgress.STARTED
        # Tells the login step that this is a registration rather than a
        # booking: the difference is that an account already on file is
        # something to offer to replace, not something to log straight in with.
        session.train_info = {"onboarding": True}
        self.storage.save_user_session(session)
        self.telegram.send_message(
            chat_id,
            MessageTemplates.ONBOARDING_INTRO,
            reply_markup=keyboards.onboarding_start_keyboard(),
        )

    def handle_logout(self, chat_id: int) -> None:
        """
        Handle /logout: forget the registered account.

        Only the registration is dropped. A search that is already running
        keeps its own copy of the login and is left alone - stopping someone's
        search is what /cancel is for, and doing it here would be a surprise.
        """
        logger.info(f"Handling /logout for chat_id={chat_id}")

        if not self.storage.get_onboarded_account(chat_id):
            self.telegram.send_message(chat_id, MessageTemplates.LOGOUT_NOTHING)
            return

        self.storage.delete_onboarded_account(chat_id)

        session = self.storage.get_user_session(chat_id)
        if session:
            session.credentials = None
            session.reset()
            self.storage.save_user_session(session)

        self.telegram.send_message(chat_id, MessageTemplates.LOGOUT_DONE)

    # ==================== Developer mode ====================

    def claim_developer_mode(self, chat_id: int, text: str) -> bool:
        """
        Turn this chat into a developer chat, if the text is the magic string.

        Disabled unless ADMIN_MAGIC_STRING is set, so a repository with no
        secret in it has no such door. Compared with compare_digest, because
        a plain == on a secret leaks its length through timing.

        Failed guesses are not counted, and cannot be: every ordinary message
        that is not the magic string would look like one. What guards this is
        the length of the secret and the fact that a successful claim is
        announced - see below.

        Returns:
            True when the message was the magic string and has been dealt
            with, so the caller stops routing it
        """
        secret = settings.ADMIN_MAGIC_STRING
        if not secret or not text:
            return False
        # Compared as bytes because compare_digest rejects str inputs
        # containing non-ASCII characters. Every message typed into the bot
        # reaches this, station names included, so a str comparison here
        # raises TypeError on the first Korean word anyone types and takes the
        # whole update with it - the message simply goes unanswered.
        if not hmac.compare_digest(text.strip().encode("utf-8"), secret.encode("utf-8")):
            return False

        if self.storage.is_developer(chat_id):
            self.telegram.send_message(chat_id, MessageTemplates.DEVELOPER_ALREADY)
            return True

        # Told to the operators who were already here, before this chat is
        # added to them. Failed guesses cannot be counted, so the defence is
        # that a successful one is impossible to do quietly: whoever holds the
        # bot finds out the moment it happens.
        existing = self.storage.get_all_developers()

        self.storage.set_developer(chat_id, True)
        logger.warning(f"chat_id={chat_id} entered developer mode with the magic string")

        # The tools arrive in the menu at the same moment they start working.
        self.publish_command_menu(chat_id)

        self.telegram.send_message(chat_id, MessageTemplates.DEVELOPER_ON)
        if existing:
            self.telegram.send_to_multiple(existing, MessageTemplates.DEVELOPER_NEW_NOTICE)
        return True

    def handle_devoff(self, chat_id: int) -> None:
        """Handle /devoff: give up developer mode in this chat."""
        logger.info(f"Handling /devoff for chat_id={chat_id}")

        if not self.storage.is_developer(chat_id):
            self.telegram.send_message(chat_id, MessageTemplates.DEVELOPER_NOT_ON)
            return

        self.storage.set_developer(chat_id, False)
        logger.warning(f"chat_id={chat_id} left developer mode")

        # And leave with it, rather than sitting in the menu offering
        # /flushredis to a chat the bot no longer treats as an operator.
        self.publish_command_menu(chat_id)

        self.telegram.send_message(chat_id, MessageTemplates.DEVELOPER_OFF)

    # ==================== Approving people ====================

    def publish_command_menu(self, chat_id: int) -> None:
        """
        Put the right command menu on one chat.

        Developer chats get a list of their own, carrying the operator's tools
        as well as everyone else's commands - Telegram shows the narrowest
        matching list rather than merging them. Every other chat has no list
        of its own and falls back to the default one.

        Keyed on developer mode rather than may_administer: a password session
        expires on its own, and a menu that quietly went stale an hour later
        would offer /flushredis to a chat that would then be asked to
        authenticate for it. Developer mode is the standing grant, and it
        changes only when someone changes it - which is when this runs.

        Best effort. Telegram being unreachable must not be the reason
        /devoff appears to have failed.
        """
        try:
            if self.storage.is_developer(chat_id):
                self.telegram.set_my_commands(MessageTemplates.DEVELOPER_COMMANDS, chat_id=chat_id)
            else:
                self.telegram.delete_my_commands(chat_id)
        except Exception as e:
            logger.warning(f"Could not update the command menu for chat_id={chat_id}: {e}")

    def may_administer(self, chat_id: int) -> bool:
        """
        Whether this chat may use the operator's tools right now.

        Two ways in: developer mode, which is deliberate and lasts, and the
        password check, which is per-session. Callbacks are re-checked through
        this rather than trusting that a keyboard was only ever sent to an
        operator - a message can be forwarded, and the button would come back
        carrying whatever chat pressed it.
        """
        return self.storage.is_developer(chat_id) or self.storage.is_admin_authenticated(chat_id)

    def handle_approve(self, chat_id: int) -> None:
        """Handle /approve: show pending access requests as buttons."""
        logger.info(f"Handling /approve for chat_id={chat_id}")

        requests = self.access.pending_requests()
        if not requests:
            self.telegram.send_message(chat_id, MessageTemplates.APPROVE_EMPTY)
            return

        timezone_name = self.storage.get_user_timezone(chat_id)
        self.telegram.send_message(
            chat_id,
            MessageTemplates.APPROVE_LIST.format(count=len(requests)),
            reply_markup=keyboards.approve_list_keyboard(requests, timezone_name),
        )

    def handle_users(self, chat_id: int) -> None:
        """Handle /users: show approved users as buttons."""
        logger.info(f"Handling /users for chat_id={chat_id}")

        users = self.access.approved_users()
        if not users:
            self.telegram.send_message(chat_id, MessageTemplates.USERS_EMPTY)
            return

        timezone_name = self.storage.get_user_timezone(chat_id)
        self.telegram.send_message(
            chat_id,
            MessageTemplates.USERS_LIST.format(count=len(users)),
            reply_markup=keyboards.users_list_keyboard(users, timezone_name),
        )

    def handle_access_callback(
        self, chat_id: int, message_id: int | None, step: str, value: str
    ) -> None:
        """
        Act on a press in the approve or users list.

        Args:
            chat_id: The operator's chat
            message_id: The message the keyboard is on, so it can be updated
                        in place rather than pushing a new one per press
            step: Which list it came from
            value: The prefixed action, carrying a phone hash where needed
        """
        if step == keyboards.STEP_APPROVE:
            self._handle_approve_callback(chat_id, message_id, value)
        else:
            self._handle_users_callback(chat_id, message_id, value)

    def _handle_approve_callback(self, chat_id: int, message_id: int | None, value: str) -> None:
        """Act on a press in the pending-requests list."""
        if value == keyboards.APPROVE_CLOSE:
            self._close_list(chat_id, message_id, "닫았습니다.")
            return

        if value == keyboards.APPROVE_BACK:
            requests = self.access.pending_requests()
            if not requests:
                self._close_list(chat_id, message_id, MessageTemplates.APPROVE_EMPTY)
                return
            timezone_name = self.storage.get_user_timezone(chat_id)
            self._edit_list(
                chat_id,
                message_id,
                MessageTemplates.APPROVE_LIST.format(count=len(requests)),
                keyboards.approve_list_keyboard(requests, timezone_name),
            )
            return

        if value.startswith(keyboards.APPROVE_PICK):
            phone_hash = value[len(keyboards.APPROVE_PICK) :]
            request = self.storage.get_access_request(phone_hash)
            if not request:
                self._close_list(chat_id, message_id, MessageTemplates.APPROVE_GONE)
                return
            timezone_name = self.storage.get_user_timezone(chat_id)
            self._edit_list(
                chat_id,
                message_id,
                MessageTemplates.APPROVE_CONFIRM.format(
                    maskedPhone=request.masked_phone,
                    requestedAt=format_user_datetime(request.requested_at, timezone_name),
                ),
                keyboards.approve_decision_keyboard(phone_hash),
            )
            return

        if value.startswith(keyboards.APPROVE_YES):
            phone_hash = value[len(keyboards.APPROVE_YES) :]
            request = self.access.approve(phone_hash, approved_by=chat_id)
            if not request:
                self._close_list(chat_id, message_id, MessageTemplates.APPROVE_GONE)
                return
            self._close_list(
                chat_id,
                message_id,
                MessageTemplates.APPROVE_DONE.format(maskedPhone=request.masked_phone),
            )
            # The whole point of approving is that the person finds out.
            self.telegram.send_message(request.chat_id, MessageTemplates.ACCESS_APPROVED)
            return

        if value.startswith(keyboards.APPROVE_NO):
            phone_hash = value[len(keyboards.APPROVE_NO) :]
            request = self.access.reject(phone_hash)
            if not request:
                self._close_list(chat_id, message_id, MessageTemplates.APPROVE_GONE)
                return
            self._close_list(
                chat_id,
                message_id,
                MessageTemplates.APPROVE_REJECTED.format(maskedPhone=request.masked_phone),
            )
            self.telegram.send_message(request.chat_id, MessageTemplates.ACCESS_REJECTED)
            return

        logger.warning(f"Unknown approve action {value!r} from chat_id={chat_id}")

    def _handle_users_callback(self, chat_id: int, message_id: int | None, value: str) -> None:
        """Act on a press in the approved-users list."""
        if value == keyboards.USERS_CLOSE:
            self._close_list(chat_id, message_id, "닫았습니다.")
            return

        if value == keyboards.USERS_BACK:
            users = self.access.approved_users()
            if not users:
                self._close_list(chat_id, message_id, MessageTemplates.USERS_EMPTY)
                return
            timezone_name = self.storage.get_user_timezone(chat_id)
            self._edit_list(
                chat_id,
                message_id,
                MessageTemplates.USERS_LIST.format(count=len(users)),
                keyboards.users_list_keyboard(users, timezone_name),
            )
            return

        if value.startswith(keyboards.USERS_REVOKE):
            phone_hash = value[len(keyboards.USERS_REVOKE) :]
            user = self.access.revoke(phone_hash)
            if not user:
                self._close_list(chat_id, message_id, MessageTemplates.USERS_REVOKE_GONE)
                return
            self._close_list(
                chat_id,
                message_id,
                MessageTemplates.USERS_REVOKED.format(maskedPhone=user.masked_phone),
            )
            return

        if value.startswith(keyboards.USERS_PICK):
            phone_hash = value[len(keyboards.USERS_PICK) :]
            user = next(
                (u for u in self.access.approved_users() if u.phone_hash == phone_hash), None
            )
            if not user:
                self._close_list(chat_id, message_id, MessageTemplates.USERS_REVOKE_GONE)
                return
            self._edit_list(
                chat_id,
                message_id,
                MessageTemplates.USERS_REVOKE_CONFIRM.format(maskedPhone=user.masked_phone),
                keyboards.users_revoke_keyboard(phone_hash),
            )
            return

        logger.warning(f"Unknown users action {value!r} from chat_id={chat_id}")

    def _edit_list(self, chat_id: int, message_id: int | None, text: str, markup: dict) -> None:
        """Replace a list in place, so pressing through it does not spam."""
        if message_id is None:
            self.telegram.send_message(chat_id, text, reply_markup=markup)
            return
        try:
            self.telegram.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        except Exception as e:
            logger.warning(f"Could not update the list on message {message_id}: {e}")
            self.telegram.send_message(chat_id, text, reply_markup=markup)

    def _close_list(self, chat_id: int, message_id: int | None, text: str) -> None:
        """Finish with a list: leave the outcome, take the buttons away."""
        if message_id is None:
            self.telegram.send_message(chat_id, text)
            return
        try:
            self.telegram.edit_message_text(
                chat_id, message_id, text, reply_markup=keyboards.empty_keyboard()
            )
        except Exception as e:
            logger.warning(f"Could not close the list on message {message_id}: {e}")
            self.telegram.send_message(chat_id, text)

    # ==================== Favourite searches ====================
    #
    # Someone who takes the same journey often answers the same nine questions
    # every time. A favourite is all of those answers except the date - the
    # one that is different every trip, and the one a saved search must never
    # pretend to know.
    #
    # Saved from the summary screen, where every answer is already on screen
    # and it costs one press. Managed from /fav.

    def handle_favourites(self, chat_id: int) -> None:
        """
        Handle /fav - open the list of saved searches.

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /fav for chat_id={chat_id}")
        self._show_favourites(chat_id, message_id=None)

    def handle_favourite_callback(self, chat_id: int, message_id: int | None, value: str) -> None:
        """
        Act on a press in the saved-search screens.

        Args:
            chat_id: Telegram chat ID
            message_id: The message the keyboard is on, so the list can be
                        walked in place rather than pushing a message per press
            value: The prefixed action, carrying a favourite id where needed
        """
        if value == keyboards.FAV_CLOSE:
            self._close_list(chat_id, message_id, "즐겨찾기를 닫았습니다.")
            return

        if value == keyboards.FAV_BACK:
            self._show_favourites(chat_id, message_id)
            return

        # Every remaining action is "<prefix>:<favourite id>", and all of them
        # need the favourite, so it is fetched once before the branching.
        fav_id = value.partition(":")[2]
        favourite = self.storage.get_favourite(chat_id, fav_id)
        if not favourite:
            # Deleted from another device, or a press on a list left open
            # from before it was deleted here.
            self._close_list(chat_id, message_id, MessageTemplates.FAV_GONE)
            return

        if value.startswith(keyboards.FAV_PICK):
            self._show_favourite(chat_id, message_id, favourite)
        elif value.startswith(keyboards.FAV_START):
            self._start_from_favourite(chat_id, message_id, favourite)
        elif value.startswith(keyboards.FAV_RENAME):
            self._ask_for_new_name(chat_id, message_id, favourite)
        elif value.startswith(keyboards.FAV_DELETE):
            self._edit_list(
                chat_id,
                message_id,
                MessageTemplates.FAV_DELETE_CONFIRM.format(name=favourite.name),
                keyboards.favourite_delete_keyboard(fav_id),
            )
        elif value.startswith(keyboards.FAV_CONFIRM_DELETE):
            self.storage.delete_favourite(chat_id, fav_id)
            logger.info(f"Deleted favourite {fav_id} for chat_id={chat_id}")
            self._close_list(
                chat_id, message_id, MessageTemplates.FAV_DELETED.format(name=favourite.name)
            )
        else:
            logger.warning(f"Unknown favourite action {value!r} from chat_id={chat_id}")

    def handle_favourite_rename(self, chat_id: int, fav_id: str, name: str) -> None:
        """
        Take the new name someone typed for a saved search.

        Args:
            chat_id: Telegram chat ID
            fav_id: Which favourite is being renamed
            name: What they typed
        """
        name = name.strip()
        if not name:
            self.telegram.send_message(chat_id, MessageTemplates.FAV_NAME_EMPTY)
            return

        # Whatever happens next, this chat is no longer renaming anything -
        # including when the favourite turned out to be gone.
        self.storage.set_pending_favourite_rename(chat_id, None)

        favourite = self.storage.get_favourite(chat_id, fav_id)
        if not favourite:
            self.telegram.send_message(chat_id, MessageTemplates.FAV_GONE)
            return

        favourite.name = name[:MAX_NAME_LENGTH]
        self.storage.save_favourite(favourite)
        self.telegram.send_message(
            chat_id, MessageTemplates.FAV_RENAMED.format(name=favourite.name)
        )

    def _show_favourites(self, chat_id: int, message_id: int | None) -> None:
        """The list, or an explanation of how to fill it."""
        favourites = self.storage.get_favourites(chat_id)
        if not favourites:
            self._close_list(chat_id, message_id, MessageTemplates.FAV_EMPTY)
            return

        self._edit_list(
            chat_id,
            message_id,
            MessageTemplates.FAV_LIST.format(count=len(favourites)),
            keyboards.favourites_keyboard(favourites),
        )

    def _show_favourite(self, chat_id: int, message_id: int | None, favourite) -> None:
        """One saved search, and what can be done with it."""
        strategy = (
            f" · {favourite.seat_strategy_display}"
            if favourite.passenger_count > 1 and favourite.seat_strategy_display
            else ""
        )
        self._edit_list(
            chat_id,
            message_id,
            MessageTemplates.FAV_DETAIL.format(
                name=favourite.name,
                route=favourite.route,
                window=favourite.window,
                trainType=favourite.train_type_display or "N/A",
                seatOption=favourite.special_option_display or "N/A",
                passengerCount=favourite.passenger_count,
                seatStrategy=strategy,
                createdAt=format_user_datetime(
                    favourite.created_at,
                    self.storage.get_user_timezone(chat_id),
                    "%Y-%m-%d",
                ),
            ),
            keyboards.favourite_detail_keyboard(favourite.fav_id),
        )

    def _ask_for_new_name(self, chat_id: int, message_id: int | None, favourite) -> None:
        """Wait for the next message to be a name."""
        self.storage.set_pending_favourite_rename(chat_id, favourite.fav_id)
        self._close_list(
            chat_id,
            message_id,
            MessageTemplates.FAV_RENAME_PROMPT.format(name=favourite.name, max=MAX_NAME_LENGTH),
        )

    def _start_from_favourite(self, chat_id: int, message_id: int | None, favourite) -> None:
        """
        Begin a booking with everything but the date already answered.

        Deliberately routed through the conversation rather than starting a
        search here. A favourite is a shortcut past the questions, not past
        the login, the trial gate, or the summary the user confirms.
        """
        if not self.conversation:
            logger.error("Cannot start from a favourite without the conversation handler")
            return

        session = self.storage.get_user_session(chat_id)
        if session and session.last_action == UserProgress.FINDING_TICKET:
            self._close_list(chat_id, message_id, MessageTemplates.FAV_BUSY)
            return

        self._close_list(chat_id, message_id, f"⭐ {favourite.name}")
        self.conversation.start_from_favourite(chat_id, favourite)

    # ==================== Progress reports ====================
    #
    # A search runs for hours without a word, and the silence is
    # indistinguishable from a process that died. This is how the user asks it
    # to check in - and, by default, does not: an unwanted message every five
    # minutes is worse than the silence it replaces.

    def handle_notify(self, chat_id: int, args: str = "") -> None:
        """
        Handle /notify - set or clear the progress report interval.

        Args:
            chat_id: Telegram chat ID
            args: What followed the command. Empty opens the settings screen;
                  "off" clears it; a number of minutes sets it.
        """
        logger.info(f"Handling /notify for chat_id={chat_id}: {args!r}")

        args = args.strip().lower()
        if not args:
            self._show_notify_settings(chat_id)
            return

        if args in ("off", "0", "끄기", "해제"):
            self.set_progress_reports(chat_id, 0)
            return

        if args in ("on", "켜기"):
            self.set_progress_reports(chat_id, settings.PROGRESS_REPORT_DEFAULT_MINUTES)
            return

        # "10", "10m", "10분" all mean the same thing, and a person who has
        # been reading "10분마다" on a button will type the 분.
        digits = args.removesuffix("m").removesuffix("min").removesuffix("분").strip()
        if not digits.isdigit():
            self.telegram.send_message(
                chat_id, MessageTemplates.NOTIFY_UNPARSEABLE.format(value=args)
            )
            return

        self.set_progress_reports(chat_id, int(digits))

    def set_progress_reports(self, chat_id: int, minutes: int) -> None:
        """
        Record how often this chat wants to hear from a running search.

        Args:
            chat_id: Telegram chat ID
            minutes: The interval, or 0 to turn reports off. Out-of-range
                     values are refused rather than clamped: silently turning
                     a request for one minute into fifteen would leave the
                     user believing something else was set.
        """
        if minutes <= 0:
            was_on = self.storage.get_progress_report_minutes(chat_id) > 0
            self.storage.set_progress_report_minutes(chat_id, 0)
            self.telegram.send_message(
                chat_id,
                MessageTemplates.NOTIFY_OFF if was_on else MessageTemplates.NOTIFY_ALREADY_OFF,
            )
            return

        if not (
            settings.PROGRESS_REPORT_MIN_MINUTES <= minutes <= settings.PROGRESS_REPORT_MAX_MINUTES
        ):
            self.telegram.send_message(
                chat_id,
                MessageTemplates.NOTIFY_OUT_OF_RANGE.format(
                    value=minutes,
                    min=settings.PROGRESS_REPORT_MIN_MINUTES,
                    max=settings.PROGRESS_REPORT_MAX_MINUTES,
                ),
            )
            return

        self.storage.set_progress_report_minutes(chat_id, minutes)
        self.telegram.send_message(chat_id, MessageTemplates.NOTIFY_ON.format(minutes=minutes))

    def _show_notify_settings(self, chat_id: int) -> None:
        """
        Both alarms on one screen, each labelled with what it acts on.

        /notify set the search reporting interval and /notify_off stopped the
        payment nagging - a pair by name and nothing alike in effect. Showing
        them together is the fix: the reader sees two alarms, two states, and
        which command belongs to which.
        """
        current = self.storage.get_progress_report_minutes(chat_id)
        reminder_active = self._payment_reminder_active(chat_id)
        self.telegram.send_message(
            chat_id,
            MessageTemplates.NOTIFY_SETTINGS.format(
                current=self._describe_notify_setting(current),
                payment=self._describe_payment_reminder(chat_id),
                min=settings.PROGRESS_REPORT_MIN_MINUTES,
                max=settings.PROGRESS_REPORT_MAX_MINUTES,
            ),
            reply_markup=keyboards.notify_keyboard(current, reminder_active),
        )

    def _payment_reminder_active(self, chat_id: int) -> bool:
        """Whether anything is currently nagging this chat about a payment."""
        status = self.storage.get_payment_status(chat_id)
        if status and status.reminder_active and not status.completed:
            return True

        multi = self.storage.get_multi_reservation_status(chat_id)
        return bool(multi and multi.should_show_reminder())

    def _describe_payment_reminder(self, chat_id: int) -> str:
        """
        The payment alarm's state, with "nothing to nag about" kept separate.

        A chat with no unpaid seat is not the same as one that switched the
        reminders off, and reading either as the other is how the two alarms
        got confused for each other in the first place.
        """
        if self._payment_reminder_active(chat_id):
            return MessageTemplates.NOTIFY_PAYMENT_ACTIVE

        status = self.storage.get_payment_status(chat_id)
        multi = self.storage.get_multi_reservation_status(chat_id)
        awaiting = bool(status and status.is_awaiting_payment()) or bool(
            multi and multi.get_pending_count() > 0
        )
        if awaiting:
            return MessageTemplates.NOTIFY_PAYMENT_INACTIVE

        return MessageTemplates.NOTIFY_PAYMENT_NONE

    @staticmethod
    def _describe_notify_setting(minutes: int) -> str:
        """How the settings screen names the interval in force."""
        if minutes <= 0:
            return MessageTemplates.NOTIFY_CURRENT_OFF
        return MessageTemplates.NOTIFY_CURRENT_ON.format(minutes=minutes)

    def handle_notify_callback(self, chat_id: int, value: str) -> None:
        """
        Act on a press in the /notify settings screen.

        Args:
            chat_id: Telegram chat ID
            value: The interval in minutes, or the "off" sentinel
        """
        if value == keyboards.NOTIFY_OFF:
            self.storage.set_waiting_for_notify_input(chat_id, False)
            self.set_progress_reports(chat_id, 0)
            return

        # The other alarm on the same screen. Routed to the same handler
        # /notify_off uses, so the two ways of asking cannot drift apart.
        if value == keyboards.NOTIFY_PAYMENT_OFF:
            self.storage.set_waiting_for_notify_input(chat_id, False)
            self.handle_notify_off(chat_id)
            return

        if value == keyboards.MANUAL:
            self._ask_for_notify_interval(chat_id)
            return

        if not value.isdigit():
            logger.warning(f"Unknown notify choice {value!r} from chat_id={chat_id}")
            return

        self.storage.set_waiting_for_notify_input(chat_id, False)
        self.set_progress_reports(chat_id, int(value))

    def _ask_for_notify_interval(self, chat_id: int) -> None:
        """
        Wait for the next message to be a number of minutes.

        Sent with force_reply so Telegram opens a reply box rather than
        leaving the user to work out that a bare number is now expected. The
        keyboard offers round numbers; this is how someone asks for seven.
        """
        self.storage.set_waiting_for_notify_input(chat_id)
        self.telegram.send_message(
            chat_id,
            MessageTemplates.NOTIFY_ASK_MINUTES.format(
                min=settings.PROGRESS_REPORT_MIN_MINUTES,
                max=settings.PROGRESS_REPORT_MAX_MINUTES,
            ),
            reply_markup=keyboards.force_reply("분 단위 숫자"),
        )

    def handle_notify_input(self, chat_id: int, text: str) -> None:
        """
        Take the interval someone typed into the reply box.

        Args:
            chat_id: Telegram chat ID
            text: What they typed
        """
        # Read as an answer once. A value the parser refuses gets its own
        # message from handle_notify, and the reply box is gone by then, so
        # leaving the flag set would silently claim the next thing typed.
        self.storage.set_waiting_for_notify_input(chat_id, False)
        self.handle_notify(chat_id, text)

    def handle_cancel(self, chat_id: int) -> None:
        """
        Handle /cancel command.

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /cancel for chat_id={chat_id}")

        # A search booked for later has no process to stop, only a record -
        # and it has to go before anything else, or the schedule fires later
        # and starts the very search the user just cancelled.
        scheduled_cancelled = self._cancel_scheduled_search(chat_id)

        # A search that died is waiting on the user to resume or drop it.
        # /cancel is them dropping it, and leaving the record would have
        # /status go on reporting a stopped search after they cancelled it.
        # Taken before cancel_reservation, which reports "nothing running"
        # when it finds nothing - true, and not what happened here.
        discarded = self.reservation.discard_dead_search(chat_id)

        # Cancel any running reservation. Returns False when there was none,
        # having told the user so itself - unless a dead search was just
        # dropped, which is the answer to /cancel and gets its own reply.
        cancelled = False if discarded else self.reservation.cancel_reservation(chat_id)

        # The rest runs either way: /cancel is also how a user gets out of a
        # half-finished conversation, and that state outlives the search.

        # Reset user session
        session = self.storage.get_user_session(chat_id)
        if session:
            session.reset()
            self.storage.save_user_session(session)

        # Waiting on something typed - a new name for a saved search, a
        # reporting interval - is one of the states /cancel exists to get out
        # of, and the only way out of it: anything else typed would be taken
        # as the answer.
        self.storage.set_pending_favourite_rename(chat_id, None)
        self.storage.set_waiting_for_notify_input(chat_id, False)

        # Clear multi-reservation status (for random seating)
        self.storage.delete_multi_reservation_status(chat_id)
        logger.debug(f"Cleared multi-reservation status for chat_id={chat_id}")

        # Stop payment reminders through the service that owns them, rather
        # than writing the same two fields by hand from here.
        self.payment_reminder.deactivate_reminders(chat_id, completed=True)

        # Clear admin password waiting state if any
        self.storage.set_waiting_for_admin_password(chat_id, False)

        if cancelled:
            self.telegram.send_message(chat_id, "✅ 예약이 취소되었습니다.")
        elif discarded:
            self.telegram.send_message(chat_id, "✅ 멈춰 있던 검색을 정리했습니다.")
        elif scheduled_cancelled:
            # cancel_reservation stays quiet when there was no running search,
            # which for a booked-but-not-started one would be no reply at all.
            self.telegram.send_message(chat_id, "✅ 예약해둔 검색을 취소했습니다.")

    def _cancel_scheduled_search(self, chat_id: int) -> bool:
        """
        Drop a search that was waiting for its start time.

        Never raises: /cancel is how a user gets out of anything, and it has
        several other things to clean up after this one.
        """
        try:
            from korail_bot.services import ScheduledSearchService

            scheduler = ScheduledSearchService(self.storage, self.telegram, self.reservation)
            return scheduler.cancel(chat_id)
        except Exception as e:
            logger.error(f"Failed to cancel the scheduled search for chat_id={chat_id}: {e}")
            return False

    def handle_status(self, chat_id: int) -> None:
        """
        Handle /status command.

        A seat already booked is reported alongside the search, not instead of
        it: someone can be waiting on a second seat while the first one sits
        unpaid, and either half alone would read as the other having been lost.

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /status for chat_id={chat_id}")

        status_message = self.reservation.get_status(chat_id)
        pending = self.pending_payments.describe(chat_id)

        if not pending:
            self.telegram.send_message(chat_id, status_message)
            return

        self.telegram.send_message(
            chat_id,
            f"{status_message}\n\n{pending}",
            reply_markup=keyboards.payment_pending_keyboard(),
        )

    def handle_tickets(self, chat_id: int) -> None:
        """
        Handle /tickets: what this chat holds, and which half the bot can undo.

        /status answers "is anything happening"; this answers "what do I
        have". They are different questions, and the second one is the one a
        user asks after being told a payment went through - which is how the
        railway's own app became the only place to check.

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /tickets for chat_id={chat_id}")
        self.pending_payments.show_tickets(chat_id)

    def handle_notify_off(self, chat_id: int) -> None:
        """
        Handle /notify_off: stop the payment reminders, and only that.

        Offered in the message that announces a booking, where it is the
        answer to "stop buzzing me". It replaces reading any message at all as
        "I have paid", which stopped the reminders by recording a payment
        nobody had checked - so the user who typed something lost the seat
        quietly and the user who paid in silence was nagged to the deadline.

        Nothing here claims the payment happened or did not. The railway is
        still asked, and whatever it answers is still reported.

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /notify_off for chat_id={chat_id}")
        from korail_bot.telegramBot.messages import Messages

        silenced = self.payment_reminder.silence(chat_id)

        multi = self.storage.get_multi_reservation_status(chat_id)
        if multi and multi.should_show_reminder():
            self.multi_reminder.stop_reminders(chat_id, manual=True)
            silenced = True

        if silenced:
            self.telegram.send_message(chat_id, Messages.NOTIFY_OFF_DONE)
            return

        # Nothing was buzzing. Said plainly, with the other thing that could
        # have been meant - /notify controls the reports a running search
        # sends, which is a different noise from a different part of the bot.
        self.telegram.send_message(chat_id, Messages.NOTIFY_OFF_NOTHING)

    def handle_timezone(self, chat_id: int, args: str = "") -> None:
        """Show or change the IANA zone used for this chat's clock."""
        from korail_bot.telegramBot.messages import Messages
        from korail_bot.utils.timezone import (
            format_user_datetime,
            is_valid_timezone,
            utc_now,
        )

        requested = args.strip()
        if not requested:
            current = self.storage.get_user_timezone(chat_id)
            self.telegram.send_message(
                chat_id,
                Messages.TIMEZONE_STATUS.format(
                    timeZone=current,
                    now=format_user_datetime(utc_now(), current, "%Y년 %m월 %d일 %H:%M"),
                ),
            )
            return

        if not is_valid_timezone(requested):
            self.telegram.send_message(chat_id, Messages.TIMEZONE_INVALID.format(value=requested))
            return

        self.storage.set_user_timezone(chat_id, requested)
        self.telegram.send_message(
            chat_id,
            Messages.TIMEZONE_CHANGED.format(
                timeZone=requested,
                now=format_user_datetime(utc_now(), requested, "%Y년 %m월 %d일 %H:%M"),
            ),
        )

    def handle_payment_callback(self, chat_id: int, value: str) -> None:
        """
        Act on what the user chose to do about a reservation awaiting payment.

        Args:
            chat_id: Telegram chat ID
            value: The answer the button carried
        """
        from korail_bot.telegramBot.messages import Messages

        if value == keyboards.PAY_CANCEL:
            self.pending_payments.confirm_cancellation(chat_id)
            return

        if value == keyboards.PAY_CONFIRM_CANCEL:
            self.pending_payments.cancel(chat_id)
            return

        if value == keyboards.PAY_KEEP:
            self.telegram.send_message(chat_id, Messages.PAYMENT_CANCEL_KEPT)
            return

        logger.warning(f"Unknown payment choice {value!r} from chat_id={chat_id}")

    def handle_debug_on(self, chat_id: int) -> None:
        """
        Handle /debug_on command.

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /debug_on for chat_id={chat_id}")
        self.storage.set_debug_mode(True)
        LoggerFactory.set_log_level("DEBUG")
        self.telegram.send_message(
            chat_id,
            "🐛 디버그 로그가 활성화되었습니다.\n\n"
            "서버 로그 레벨이 DEBUG로 전환되었습니다.\n"
            "새로 시작하는 예약 검색부터 상세 로그가 출력됩니다.\n"
            "/debug_off로 비활성화할 수 있습니다.",
        )

    def handle_debug_off(self, chat_id: int) -> None:
        """
        Handle /debug_off command.

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /debug_off for chat_id={chat_id}")
        self.storage.set_debug_mode(False)
        LoggerFactory.set_log_level("INFO")
        self.telegram.send_message(
            chat_id,
            "✅ 디버그 로그가 비활성화되었습니다.\n\n서버 로그 레벨이 INFO로 복원되었습니다.",
        )

    def handle_cancel_all(self, chat_id: int) -> None:
        """
        Handle /cancelall command (admin only).

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /cancelall for chat_id={chat_id}")

        # This is an admin command - in production, you'd want to check permissions
        count = self.reservation.cancel_all_reservations(chat_id)
        logger.info(f"Cancelled {count} reservations by admin chat_id={chat_id}")

    def handle_all_users(self, chat_id: int) -> None:
        """
        Handle /allusers command (admin only).

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /allusers for chat_id={chat_id}")

        sessions = self.storage.get_all_user_sessions()
        user_ids = []

        for session in sessions:
            if session.credentials:
                user_ids.append(mask_phone(session.credentials.korail_id))
            else:
                user_ids.append(f"chat_{session.chat_id}")

        message = f"총 {len(user_ids)}명의 유저가 있습니다 : {user_ids}"
        self.telegram.send_message(chat_id, message)

    def handle_broadcast(self, chat_id: int, message: str) -> None:
        """
        Handle /broadcast command (admin only).

        Args:
            chat_id: Admin chat ID
            message: Message to broadcast
        """
        logger.info(f"Handling /broadcast for chat_id={chat_id}")

        # Get all user chat IDs
        sessions = self.storage.get_all_user_sessions()
        all_chat_ids = [s.chat_id for s in sessions]

        if message:
            sent_count = self.telegram.send_to_multiple(all_chat_ids, message)
            logger.info(f"Broadcast sent to {sent_count}/{len(all_chat_ids)} users")
        else:
            # Default fun message if no message provided
            self.telegram.send_to_multiple(all_chat_ids, "앙 기모띠")

    def handle_flush_redis(self, chat_id: int) -> None:
        """
        Handle /flushredis command (admin only).

        WARNING: This will delete ALL Redis data including all user sessions,
        running reservations, and payment statuses.

        Args:
            chat_id: Admin chat ID
        """
        logger.warning(f"Handling /flushredis for chat_id={chat_id}")

        try:
            # Check if storage is Redis-based
            if not hasattr(self.storage, "flush_all"):
                self.telegram.send_message(chat_id, "❌ 현재 스토리지는 Redis가 아닙니다.")
                return

            # Flush all data
            deleted_count = self.storage.flush_all()

            message = f"✅ Redis 메모리가 초기화되었습니다.\n삭제된 키: {deleted_count}개"
            self.telegram.send_message(chat_id, message)
            logger.warning(
                f"Redis flushed by admin chat_id={chat_id}, deleted {deleted_count} keys"
            )

        except Exception as e:
            logger.error(f"Failed to flush Redis: {e}")
            self.telegram.send_message(chat_id, f"❌ Redis 초기화 실패: {e!s}")

    def handle_help(self, chat_id: int) -> None:
        """
        Handle /help command.

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /help for chat_id={chat_id}")
        self.telegram.send_message(
            chat_id, MessageTemplates.help_message(is_admin=self.may_administer(chat_id))
        )

    def handle_version(self, chat_id: int) -> None:
        """
        Handle /version: what is running here, and how it is doing.

        Open to everyone, because the question "did the update land" is asked
        by the people using the bot as often as by whoever deployed it - the
        update announcement arrives once and scrolls away, and until now there
        was nothing to ask afterwards.

        What an operator gets on top of that is counts, not names: how many
        searches are running, how many bookings are waiting to be paid for.
        That is the block that answers "can I restart", and it is added only
        for a chat that may act on it.

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /version for chat_id={chat_id}")

        elapsed = uptime_seconds()
        note = notes_for(__version__)

        self.telegram.send_message(
            chat_id,
            MessageTemplates.version_message(
                version=__version__,
                uptime=format_uptime(elapsed) if elapsed is not None else "",
                release_headline=note.headline if note else "",
                operator_status=self._operator_status() if self.may_administer(chat_id) else "",
            ),
        )

    def handle_about(self, chat_id: int) -> None:
        """
        Handle /about: what this bot is and what it does not do.

        Kept apart from /version so each answers one question. Someone
        checking which version is deployed should not have to read the
        introduction, and someone meeting the bot for the first time should
        not have to work out what it is from a version number.

        Args:
            chat_id: Telegram chat ID
        """
        logger.info(f"Handling /about for chat_id={chat_id}")
        self.telegram.send_message(chat_id, MessageTemplates.about_message(__version__))

    def _operator_status(self) -> str:
        """
        The operational counts, for the operator half of /version.

        Every number comes out of Redis, so a storage that is not answering
        cannot be reported as zeroes - that reads as a quiet afternoon rather
        than as a bot that has lost its records. One failure takes the whole
        block, and says so.

        Returns:
            The block to append, or the message saying it could not be read
        """
        try:
            running = len(self.storage.get_all_running_reservations())
            scheduled = len(self.storage.get_all_scheduled_searches())
            awaiting = sum(
                1
                for status in self.storage.get_all_payment_statuses()
                if status.is_awaiting_payment()
            )
            registered = len(self.storage.get_all_onboarded_chat_ids())
            debug_logging = self.storage.is_debug_mode()
        except Exception as e:
            logger.error(f"Could not read the operational status: {e}")
            return MessageTemplates.VERSION_OPERATOR_UNAVAILABLE

        return MessageTemplates.operator_status(
            running=running,
            scheduled=scheduled,
            awaiting_payment=awaiting,
            registered_chats=registered,
            debug_logging=debug_logging,
        )

    def handle_unknown_command(self, chat_id: int, command: str) -> None:
        """
        Answer a slash command the bot does not have.

        Kept, where a stray line of prose is now ignored, because the two are
        not the same act. Prose was never addressed to the bot; a slash is
        someone reaching for a command and missing, usually by a letter, and
        silence there reads as the bot being down. It is one line pointing at
        /help - the five-command summary it used to print went stale the
        moment the commands were renamed, and /help is the copy that cannot.

        Args:
            chat_id: Telegram chat ID
            command: Unknown command text
        """
        from korail_bot.telegramBot.messages import Messages

        logger.warning(f"Unknown command '{command}' from chat_id={chat_id}")
        self.telegram.send_message(chat_id, Messages.ERROR_INVALID_COMMAND)

    def is_command(self, text: str) -> bool:
        """
        Check if text is a command.

        Args:
            text: Message text

        Returns:
            True if text starts with '/'
        """
        return bool(text) and text.startswith("/")

    def route_command(self, chat_id: int, text: str) -> bool:
        """
        Route command to appropriate handler.

        Args:
            chat_id: Telegram chat ID
            text: Command text

        Returns:
            True if command was handled, False otherwise
        """
        if not self.is_command(text):
            return False

        # Parse command and arguments
        parts = text.split(" ", 1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Public commands
        if command == "/start":
            self.handle_start(chat_id, args)
        elif command in ("/onboarding", "/init"):
            self.handle_onboarding(chat_id)
        elif command == "/logout":
            self.handle_logout(chat_id)
        elif command == "/approve":
            self._handle_admin_command(chat_id, self.handle_approve, "/approve")
        elif command == "/users":
            self._handle_admin_command(chat_id, self.handle_users, "/users")
        elif command == "/devoff":
            self.handle_devoff(chat_id)
        # /stop is the name in the menu; /cancel keeps working because it is
        # printed all over messages already sent and sitting in users' chat
        # history, and dropping the word would turn every one of them into a
        # lie. Both stop the search and neither touches a booked seat - that
        # is /tickets.
        elif command in ("/stop", "/cancel"):
            self.handle_cancel(chat_id)
        elif command == "/status":
            self.handle_status(chat_id)
        elif command == "/tickets":
            self.handle_tickets(chat_id)
        elif command == "/notify":
            self.handle_notify(chat_id, args)
        elif command == "/notify_off":
            self.handle_notify_off(chat_id)
        elif command == "/timezone":
            self.handle_timezone(chat_id, args)
        elif command in ("/fav", "/favorites", "/favourites"):
            self.handle_favourites(chat_id)
        elif command == "/help":
            self.handle_help(chat_id)
        elif command == "/version":
            self.handle_version(chat_id)
        elif command == "/about":
            self.handle_about(chat_id)
        # Admin commands - require authentication
        elif command == "/cancelall":
            self._handle_admin_command(chat_id, self.handle_cancel_all, "/cancelall")
        elif command == "/allusers":
            self._handle_admin_command(chat_id, self.handle_all_users, "/allusers")
        elif command == "/broadcast":
            self._handle_admin_command(
                chat_id, lambda cid: self.handle_broadcast(cid, args), f"/broadcast {args}"
            )
        elif command == "/flushredis":
            self._handle_admin_command(chat_id, self.handle_flush_redis, "/flushredis")
        # Debug commands - admin only
        elif command == "/debug_on":
            self._handle_admin_command(chat_id, self.handle_debug_on, "/debug_on")
        elif command == "/debug_off":
            self._handle_admin_command(chat_id, self.handle_debug_off, "/debug_off")
        else:
            self.handle_unknown_command(chat_id, command)

        return True

    def _is_locked_out(self, chat_id: int) -> bool:
        """Check whether admin authentication is currently blocked."""
        return self.storage.get_admin_auth_failures(chat_id) >= settings.ADMIN_MAX_AUTH_FAILURES

    def _send_lockout_message(self, chat_id: int) -> None:
        """Tell the user how long the lockout lasts."""
        from korail_bot.telegramBot.messages import Messages

        remaining_seconds = self.storage.get_admin_lockout_remaining(chat_id)
        remaining_minutes = max(1, -(-remaining_seconds // 60))  # round up
        self.telegram.send_message(
            chat_id, Messages.ADMIN_AUTH_LOCKED.format(remaining_minutes=remaining_minutes)
        )

    def _handle_admin_command(self, chat_id: int, handler_func, command_name: str = "") -> None:
        """
        Handle admin command with authentication check.

        Args:
            chat_id: Telegram chat ID
            handler_func: Function to call if authenticated
            command_name: Name of the command for tracking
        """
        from korail_bot.telegramBot.messages import Messages

        # No admin password configured means no admin surface at all.
        if not settings.ADMIN_PASSWORD:
            self.telegram.send_message(chat_id, Messages.ADMIN_DISABLED)
            logger.warning(
                f"Admin command {command_name} refused for chat_id={chat_id}: "
                f"ADMIN_PASSWORD is not configured"
            )
            return

        # Developer mode is a standing grant, so it answers before the
        # password does - an operator should not be asked to authenticate in
        # the chat they deliberately marked as theirs.
        if self.storage.is_developer(chat_id) or self.storage.is_admin_authenticated(chat_id):
            handler_func(chat_id)
            return

        if self._is_locked_out(chat_id):
            self._send_lockout_message(chat_id)
            logger.warning(
                f"Admin command {command_name} refused for chat_id={chat_id}: "
                f"locked out after repeated failures"
            )
            return

        # Request password and mark as waiting
        self.storage.set_waiting_for_admin_password(chat_id, True)
        self.storage.set_pending_admin_command(chat_id, command_name)
        self.telegram.send_message(chat_id, Messages.ADMIN_AUTH_REQUIRED)
        logger.info(f"Admin authentication required for chat_id={chat_id}, command={command_name}")

    def handle_admin_password(self, chat_id: int, password: str) -> bool:
        """
        Handle admin password input.

        Attempts are rate limited: after ADMIN_MAX_AUTH_FAILURES failures the
        chat is locked out for ADMIN_LOCKOUT_SECONDS, which turns an unlimited
        online guessing channel into a bounded one.

        Args:
            chat_id: Telegram chat ID
            password: Password attempt

        Returns:
            True if authenticated successfully
        """
        from korail_bot.telegramBot.messages import Messages

        # Get pending command before clearing state
        pending_command = self.storage.get_pending_admin_command(chat_id)

        # Clear waiting state
        self.storage.set_waiting_for_admin_password(chat_id, False)
        self.storage.set_pending_admin_command(chat_id, None)

        if not settings.ADMIN_PASSWORD:
            self.telegram.send_message(chat_id, Messages.ADMIN_DISABLED)
            return False

        if self._is_locked_out(chat_id):
            self._send_lockout_message(chat_id)
            logger.warning(f"Admin authentication blocked (locked out): chat_id={chat_id}")
            return False

        # Constant-time comparison so the password cannot be recovered by
        # timing individual characters. Compared as bytes because
        # compare_digest rejects str inputs containing non-ASCII characters,
        # and users do type Korean into this prompt.
        if hmac.compare_digest(password.encode("utf-8"), settings.ADMIN_PASSWORD.encode("utf-8")):
            self.storage.clear_admin_auth_failures(chat_id)
            self.storage.set_admin_authenticated(chat_id, True)
            self.telegram.send_message(chat_id, Messages.ADMIN_AUTH_SUCCESS)
            logger.info(f"Admin authenticated: chat_id={chat_id}")

            # Execute pending command if exists
            if pending_command:
                logger.info(f"Executing pending admin command: {pending_command}")
                self.route_command(chat_id, pending_command)

            return True

        failures = self.storage.register_admin_auth_failure(chat_id)
        remaining = settings.ADMIN_MAX_AUTH_FAILURES - failures
        logger.warning(
            f"Admin authentication failed: chat_id={chat_id}, "
            f"failures={failures}/{settings.ADMIN_MAX_AUTH_FAILURES}"
        )

        if remaining <= 0:
            self._send_lockout_message(chat_id)
        else:
            self.telegram.send_message(
                chat_id,
                Messages.ADMIN_AUTH_FAILED_REMAINING.format(
                    remaining=remaining,
                    lockout_minutes=max(1, settings.ADMIN_LOCKOUT_SECONDS // 60),
                ),
            )

        return False
