"""
Everything the Mini App can ask this bot to do.

The screen holds no Korail logic. It asks for stations and gets the list the
bot already fetches; it asks for trains and gets what ``rail_service`` found;
it presses start and the same ``start_booking`` the chat's confirmation button
presses runs. Nothing here reimplements a railway - each method is a
translation between JSON and a service that existed before this file did.

Two rules shape it. Credentials never travel outward: a registered password
goes into a login and never into a response. And the Mini App never gets a
route around a rule the chat obeys - the access gate, the trial allowance and
the duplicate-search guard are enforced by the shared code both call.
"""

from dataclasses import dataclass
from datetime import datetime

from korail_bot import __version__
from korail_bot.config.settings import settings
from korail_bot.handlers.conversation_handler import ConversationHandler
from korail_bot.models import (
    MAJOR_STATIONS,
    FavouriteSearch,
    SeatPlanError,
    TrainSearchParams,
    UserProgress,
    UserSession,
    parse_seat_plan,
)
from korail_bot.services.mini_app_service import (
    ACTION,
    SCHEMA_VERSION,
    MiniAppDataError,
    MiniAppSubmission,
)
from korail_bot.services.pending_payment_service import PendingPaymentService
from korail_bot.services.reservation_service import ReservationService
from korail_bot.services.scheduled_search_service import ScheduledSearchService, ScheduleError
from korail_bot.services.telegram_service import TelegramService
from korail_bot.storage.base import StorageInterface
from korail_bot.utils.logger import get_logger
from korail_bot.utils.timezone import as_utc, is_valid_timezone

logger = get_logger(__name__)

#: A search may be narrowed to at most this many trains, matching the chat's
#: own list. More than this on screen is not a choice, it is a wall of text.
MAX_SELECTED_TRAINS = 30


class MiniAppError(Exception):
    """
    Something the person holding the phone has to be told.

    Carries the sentence to show them and the status to answer with, so that
    the resource layer never has to invent either.
    """

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class RailState:
    """What the app needs to know about Korail before offering it."""

    registered: bool
    stations: list[str]
    major_stations: list[str]


class MiniAppGateway:
    """Serves the Mini App by driving the services the chat flow drives."""

    def __init__(
        self,
        storage: StorageInterface,
        telegram_service: TelegramService,
        reservation_service: ReservationService,
        conversation_handler: ConversationHandler | None = None,
        pending_payment_service: PendingPaymentService | None = None,
        scheduled_search_service: ScheduledSearchService | None = None,
    ):
        """
        Args:
            storage: Where sessions, registrations and favourites live
            telegram_service: Used to confirm actions in the chat as well as
                in the app, so the two never disagree about what happened
            reservation_service: Starts and stops search processes
            conversation_handler: The shared reservation logic. Built here
                when not supplied.
            pending_payment_service: Reads and cancels unpaid reservations
            scheduled_search_service: Books searches for a later time
        """
        self.storage = storage
        self.telegram = telegram_service
        self.reservation = reservation_service
        self.conversation = conversation_handler or ConversationHandler(
            storage, telegram_service, reservation_service
        )
        self.pending_payments = pending_payment_service or PendingPaymentService(
            storage, telegram_service
        )
        self.scheduler = scheduled_search_service or ScheduledSearchService(
            storage, telegram_service, reservation_service
        )

    def sync_timezone(self, chat_id: int, timezone_name: str) -> None:
        """Remember the browser's IANA zone; ignore forged/unknown names."""
        if is_valid_timezone(timezone_name):
            self.storage.set_user_timezone(chat_id, timezone_name)

    # ==================== Opening the app ====================

    def bootstrap(self, chat_id: int) -> dict:
        """
        Everything the app needs to draw its first screen in one round trip.

        One request rather than eight, because the app opens inside Telegram
        over whatever connection the phone has, and eight sequential requests
        is the difference between a screen and a spinner.

        Args:
            chat_id: The chat Telegram vouched for

        Returns:
            The app's whole starting state
        """
        session = self.storage.get_user_session(chat_id)
        return {
            "version": __version__,
            "timeZone": self.storage.get_user_timezone(chat_id),
            "developer": self.storage.is_developer(chat_id),
            "rail": self._rail_state(chat_id),
            "running": self._running(chat_id),
            "scheduled": self._scheduled(chat_id),
            "pending": self._pending(chat_id),
            "favourites": self.favourites(chat_id),
            "notifyMinutes": self.storage.get_progress_report_minutes(chat_id),
            "draft": self._draft(session),
            "paymentUrl": settings.KORAIL_PAYMENT_URL,
        }

    def _rail_state(self, chat_id: int) -> dict:
        """
        Whether the chat can book, and where the trains stop.

        The station list is served rather than shipped with the page. It used
        to be a constant in the JavaScript and another in the Python, which
        meant Korail's several hundred stations were represented by the
        seventeen someone had typed out, and the two copies could disagree.
        """
        registered = bool(
            self.storage.get_onboarded_account(chat_id)
        ) or self.conversation.uses_server_account(chat_id)

        state = RailState(
            registered=registered,
            stations=sorted(self._stations()),
            major_stations=list(MAJOR_STATIONS),
        )
        return {
            "registered": state.registered,
            "stations": state.stations,
            "majorStations": state.major_stations,
            "displayName": "코레일",
        }

    @staticmethod
    def _stations() -> set[str]:
        """The stations Korail serves, from wherever that list lives."""
        from korail_bot.utils.station_codes import get_valid_stations

        try:
            return get_valid_stations()
        except Exception as exc:
            # The list is a convenience for the picker; the name is validated
            # again on submission either way. An empty picker is better than
            # a screen that will not open.
            logger.warning(f"Could not list Korail stations for the Mini App: {exc}")
            return set(MAJOR_STATIONS)

    def _draft(self, session: UserSession | None) -> dict | None:
        """The answers of a booking left half-finished, for pre-filling."""
        if not session or not session.in_progress:
            return None
        if session.last_action in (UserProgress.INIT, UserProgress.FINDING_TICKET):
            return None
        info = session.train_info or {}
        if not info.get("depDate"):
            return None
        return self._conditions_of(info)

    @staticmethod
    def _conditions_of(info: dict) -> dict:
        """Turn a stored booking back into the app's own field names."""
        conditions = {
            # The app hands this dict straight back as its conditions, and
            # MiniAppSubmission.parse refuses one without the schema stamp.
            "v": SCHEMA_VERSION,
            "action": ACTION,
            "dep_date": info.get("depDate", ""),
            "src_station": info.get("srcLocate", ""),
            "dst_station": info.get("dstLocate", ""),
            "dep_time": (info.get("depTime") or "")[:4],
            "max_dep_time": info.get("maxDepTime", "2400"),
            "train_type": "1" if "KTX" in (info.get("trainType") or "").upper() else "2",
            "seat_option": {
                "GENERAL_FIRST": "1",
                "GENERAL_ONLY": "2",
                "SPECIAL_FIRST": "3",
                "SPECIAL_ONLY": "4",
            }.get(str(info.get("specialInfo", "")).rsplit(".", 1)[-1], "1"),
            "passenger_count": info.get("passengerCount", 1),
            "seat_strategy": "1" if info.get("seatStrategy") == "consecutive" else "2",
            "seat_preference": info.get("seatPreference", ""),
            "trains": list(info.get("selectedTrains") or []),
        }
        try:
            plan = parse_seat_plan(info.get("seatPlan"))
        except SeatPlanError:
            # A draft that cannot carry its seat plan is still worth
            # pre-filling; losing the whole screen over it is not.
            plan = None
        if plan is not None:
            conditions["seat_plan"] = plan.as_payload()
        return conditions

    # ==================== Registering an account ====================

    def register(self, chat_id: int, username: str, password: str) -> dict:
        """
        Verify a Korail login and remember it for this chat.

        The password is checked against Korail before anything is stored: a
        registration that does not log in is worse than none, since it is
        only discovered by a search that fails hours later.

        Args:
            chat_id: The chat Telegram vouched for
            username: Membership number or phone number
            password: The Korail password

        Returns:
            That the chat is now registered

        Raises:
            MiniAppError: The input was malformed, or Korail refused the login
        """
        from korail_bot.utils.validators import InputValidator

        error = InputValidator.validate_korail_login(username)
        if error:
            raise MiniAppError(error)
        error = InputValidator.validate_password(password)
        if error:
            raise MiniAppError(error)

        normalized = InputValidator.normalize_korail_login(username)
        if not normalized:
            raise MiniAppError("휴대전화 번호 또는 회원번호를 다시 확인해 주세요.")

        rail = self.conversation._rail_service(chat_id)
        if not rail.login(normalized, password):
            raise MiniAppError(
                "코레일에 로그인하지 못했어요. 회원번호와 비밀번호를 다시 확인해 주세요.",
                status=401,
            )

        self.conversation._remember_account(chat_id, normalized, password)
        return {"registered": True}

    def logout(self, chat_id: int) -> dict:
        """Forget the registration."""
        self.storage.delete_onboarded_account(chat_id)
        return {"registered": False}

    # ==================== Choosing a train ====================

    def list_trains(self, chat_id: int, payload: dict) -> dict:
        """
        The trains running in the chosen window, with their seat counts.

        Sold-out trains are included, as they are in the chat: a train with
        seats left needs no watching, so the ones worth picking are exactly
        the ones an ordinary search would leave out.

        Args:
            chat_id: The chat Telegram vouched for
            payload: The app's conditions, in the shape MiniAppSubmission
                already validates

        Returns:
            The trains, and the conditions they were found for

        Raises:
            MiniAppError: The conditions were rejected, no account is
                registered for that railway, or the railway could not be asked
        """
        session, submission = self._prepared_session(chat_id, payload)

        options = self.conversation.fetch_train_options(chat_id, session)
        if options is None:
            raise MiniAppError(
                "지금은 열차 목록을 불러올 수 없어요. "
                "열차를 고르지 않고 시간대 전체 검색으로 계속할 수 있어요.",
                status=503,
            )

        truncated = len(options) > self.conversation.MAX_TRAIN_OPTIONS
        if truncated:
            options = options[: self.conversation.MAX_TRAIN_OPTIONS]

        # Stored so that starting the search does not have to ask the railway
        # a second time, and so a selection survives the app being reopened.
        session.train_info["trainOptions"] = options
        self.storage.save_user_session(session)

        return {
            "trains": options,
            "truncated": truncated,
            "passengerCount": submission.passenger_count,
        }

    # ==================== Starting the search ====================

    def start_search(self, chat_id: int, payload: dict) -> dict:
        """
        Start watching for cancellations, now.

        Runs the same ``start_booking`` the chat's confirmation button runs,
        so the access gate and the trial allowance apply identically here.

        Args:
            chat_id: The chat Telegram vouched for
            payload: Conditions plus the trains to narrow to

        Returns:
            Whether a search is now running, and what to say about it

        Raises:
            MiniAppError: The conditions were rejected or nothing could start
        """
        session, _ = self._prepared_session(chat_id, payload)
        session.train_info["selectedTrains"] = self._selected_trains(payload)
        self.storage.save_user_session(session)

        outcome = self.conversation.start_booking(chat_id, session)

        if outcome.needs_access_request:
            return {
                "started": False,
                "needsAccessRequest": True,
                "accessRequestPending": outcome.access_request_pending,
                "trialUsed": outcome.trial_used,
                "trialLimit": outcome.trial_limit,
            }

        if not outcome.started:
            raise MiniAppError(
                outcome.error or "빈자리 검색을 시작하지 못했어요. 잠시 후 다시 시도해 주세요.",
                status=409,
            )

        return {
            "started": True,
            "trialUsed": outcome.trial_used,
            "trialLimit": outcome.trial_limit,
            "running": self._running(chat_id),
        }

    def schedule_search(self, chat_id: int, payload: dict) -> dict:
        """
        Book the search to start at a stated time instead of now.

        Args:
            chat_id: The chat Telegram vouched for
            payload: Conditions, trains, and ``start_at`` as an ISO timestamp

        Returns:
            When the search will start

        Raises:
            MiniAppError: The time was unusable or the booking could not be
                stored
        """
        session, _ = self._prepared_session(chat_id, payload)
        session.train_info["selectedTrains"] = self._selected_trains(payload)
        self.storage.save_user_session(session)

        start_at = self._start_time(payload)
        params = self.conversation._build_search_params(session)
        credentials = session.credentials
        if credentials is None or not credentials.korail_pw:
            raise MiniAppError(
                "예약된 검색을 시작하려면 코레일에 다시 로그인해야 해요. "
                "코레일 계정을 다시 연결한 뒤 시도해 주세요.",
                status=409,
            )

        try:
            self.scheduler.validate_start_time(start_at, params)
            self.scheduler.schedule(
                chat_id=chat_id,
                username=credentials.korail_id,
                password=credentials.korail_pw,
                search_params=params,
                start_at=start_at,
            )
        except ScheduleError as exc:
            raise MiniAppError(str(exc)) from exc
        except Exception as exc:
            logger.error(f"Could not schedule a Mini App search for chat_id={chat_id}: {exc}")
            from korail_bot.telegramBot.messages import Messages

            raise MiniAppError(Messages.ERROR_RESERVATION_START_FAILED, status=500) from exc

        session.reset()
        self.storage.save_user_session(session)
        return {"scheduled": True, "startAt": as_utc(start_at).isoformat()}

    def cancel_search(self, chat_id: int) -> dict:
        """
        Stop a running search, or drop one booked for later.

        Delegates to the same method /cancel uses, which stops the process,
        clears the record and the stored credentials, and says so in the
        chat - so a search stopped from the app looks the same afterwards as
        one stopped from the chat.
        """
        stopped = self.reservation.cancel_reservation(chat_id)

        scheduled = bool(self.storage.get_scheduled_search(chat_id))
        if scheduled:
            self.storage.delete_scheduled_search(chat_id)

        return {"stopped": stopped, "unscheduled": scheduled}

    # ==================== Seats already taken ====================

    def status(self, chat_id: int) -> dict:
        """What is running, what is booked for later, what awaits payment."""
        return {
            "running": self._running(chat_id),
            "scheduled": self._scheduled(chat_id),
            "pending": self._pending(chat_id),
        }

    def cancel_pending(self, chat_id: int) -> dict:
        """
        Give the unpaid seats this chat holds back to the railway.

        Whole-chat rather than one reservation at a time, because that is what
        the service does and what the chat's own button does: the seats of one
        booking are held together and giving back half of a random-seating run
        leaves the user with a partial trip nobody asked for.

        The reservation numbers are never taken from the request. They come
        from this chat's own pending list, so a number typed into the app
        cannot reach a stranger's booking.

        Args:
            chat_id: The chat Telegram vouched for

        Returns:
            Whether the railway confirmed, and what is left pending

        Raises:
            MiniAppError: The railway did not confirm the cancellation
        """
        if not self.pending_payments.pending(chat_id):
            raise MiniAppError("결제를 기다리는 예약이 없어요.", status=404)

        if not self.pending_payments.cancel(chat_id):
            # The service has already said in the chat which of the several
            # reasons it was - mid-booking, login refused, railway refused.
            raise MiniAppError("예약을 취소하지 못했어요. 앱 알림을 확인해 주세요.", status=502)

        return {"cancelled": True, "pending": self._pending(chat_id)}

    def request_access(self, chat_id: int) -> dict:
        """Ask the operator to be let in past the trial limit."""
        self.conversation.request_access(chat_id)
        account = self.storage.get_onboarded_account(chat_id)
        return {"requested": bool(account)}

    # ==================== Favourites and notifications ====================

    def favourites(self, chat_id: int) -> list[dict]:
        """
        Every saved search this chat can start again in one press.

        The conditions come back with the date left out, exactly as the chat's
        favourites do - a route saved in March is not a trip in March.
        """
        return [
            {
                "id": favourite.fav_id,
                "name": favourite.name,
                "route": favourite.route,
                "window": favourite.window,
                "conditions": self._conditions_of(favourite.as_train_info()),
            }
            for favourite in self.storage.get_favourites(chat_id)
        ]

    def save_favourite(self, chat_id: int, payload: dict) -> dict:
        """Store the conditions on screen under a name."""
        submission = self._submission(payload)
        name = payload.get("name")
        favourite = FavouriteSearch.from_train_info(
            chat_id,
            submission.as_train_info(),
            name.strip() if isinstance(name, str) else "",
        )
        self.storage.save_favourite(favourite)
        return {"saved": True, "favourites": self.favourites(chat_id)}

    def delete_favourite(self, chat_id: int, fav_id: str) -> dict:
        """Forget one saved search."""
        if not self.storage.delete_favourite(chat_id, fav_id):
            raise MiniAppError("저장한 조건을 찾을 수 없어요.", status=404)
        return {"deleted": True, "favourites": self.favourites(chat_id)}

    def set_notify_minutes(self, chat_id: int, minutes: object) -> dict:
        """
        How often a running search reports in, 0 meaning not at all.

        The same bounds /notify enforces, read from the same settings, so the
        two surfaces cannot come to disagree about what an allowed interval is.
        """
        if isinstance(minutes, bool) or not isinstance(minutes, (int, str)):
            raise MiniAppError("알림 간격을 숫자로 입력해 주세요.")
        try:
            value = int(minutes)
        except ValueError as exc:
            raise MiniAppError("알림 간격을 숫자로 입력해 주세요.") from exc

        low = settings.PROGRESS_REPORT_MIN_MINUTES
        high = settings.PROGRESS_REPORT_MAX_MINUTES
        if value != 0 and not low <= value <= high:
            raise MiniAppError(
                f"알림 간격은 {low}분에서 {high}분 사이여야 해요. 0을 입력하면 꺼져요."
            )

        self.storage.set_progress_report_minutes(chat_id, value)
        return {"notifyMinutes": value}

    # ==================== Shared plumbing ====================

    def _prepared_session(
        self, chat_id: int, payload: dict
    ) -> tuple[UserSession, MiniAppSubmission]:
        """
        A session holding these conditions, logged in and ready to book.

        Refuses rather than overwrites when a search is already running: the
        chat flow makes the same refusal, and silently replacing a search
        someone is relying on is not a thing a screen should do by accident.
        """
        submission = self._submission(payload)

        session = self.storage.get_user_session(chat_id) or UserSession(chat_id=chat_id)
        if session.last_action == UserProgress.FINDING_TICKET:
            raise MiniAppError(
                "이미 검색 중이에요. 먼저 검색을 중지한 뒤 새로 시작해 주세요.", status=409
            )

        selected = list(session.train_info.get("selectedTrains") or [])
        session.reset()
        session.in_progress = True
        session.train_info = submission.as_train_info()
        session.train_info["selectedTrains"] = selected
        session.last_action = UserProgress.SEAT_STRATEGY_INPUT_SUCCESS

        self._log_in(chat_id, session)
        self.storage.save_user_session(session)
        return session, submission

    def _log_in(self, chat_id: int, session: UserSession) -> None:
        """
        Put a verified login on the session, or say what is missing.

        The registered password is verified against Korail rather than
        trusted, exactly as the chat flow verifies it: people change their
        password without telling the bot.
        """
        if self.conversation.uses_server_account(chat_id):
            if self.conversation._login_with_environment_credentials(chat_id, session):
                return
            raise MiniAppError("코레일에 로그인하지 못했어요.", status=502)

        account = self.storage.get_onboarded_account(chat_id)
        if not account:
            raise MiniAppError(
                "코레일 계정이 연결되지 않았어요. 먼저 계정을 연결해 주세요.",
                status=428,
            )

        rail = self.conversation._rail_service(chat_id)
        if not rail.login(account.korail_id, account.korail_pw):
            # login() answers False for a timeout as well as for a refused
            # password, so a failure here is no proof the account is stale.
            # Deleting it would cost the user their link over a Korail hiccup.
            raise MiniAppError(
                "코레일에 로그인하지 못했어요. 잠시 후 다시 시도하고, "
                "계속되면 계정을 다시 연결해 주세요.",
                status=401,
            )

        session.credentials = account.as_credentials()

    @staticmethod
    def _submission(payload: dict) -> MiniAppSubmission:
        """Validate the app's conditions with the boundary that already exists."""
        import json

        if not isinstance(payload, dict):
            raise MiniAppError("예약 조건을 확인할 수 없어요.")

        conditions = payload.get("conditions", payload)
        try:
            return MiniAppSubmission.parse(json.dumps(conditions, ensure_ascii=False))
        except MiniAppDataError as exc:
            raise MiniAppError(str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise MiniAppError("예약 조건을 확인할 수 없어요.") from exc

    @staticmethod
    def _selected_trains(payload: dict) -> list[str]:
        """
        The train numbers to narrow to, or none to watch the whole window.

        Validated rather than trusted: these go into the arguments of a search
        process, and 'whatever the client sent' is not a shape to pass there.
        """
        raw = payload.get("trains") or []
        if not isinstance(raw, list):
            raise MiniAppError("선택한 열차 목록을 확인할 수 없어요.")
        if len(raw) > MAX_SELECTED_TRAINS:
            raise MiniAppError(f"열차는 최대 {MAX_SELECTED_TRAINS}개까지 고를 수 있어요.")

        numbers = []
        for item in raw:
            text = str(item).strip()
            if not text.isdigit() or len(text) > 5:
                raise MiniAppError("열차 번호가 올바르지 않아요.")
            numbers.append(text)
        return numbers

    @staticmethod
    def _start_time(payload: dict) -> datetime:
        """Read the scheduled start out of the request."""
        raw = payload.get("start_at")
        if not isinstance(raw, str) or not raw:
            raise MiniAppError("검색을 시작할 시각을 골라 주세요.")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MiniAppError("검색 시작 시각을 확인할 수 없어요.") from exc
        if parsed.tzinfo is None:
            # A bare wall clock is exactly what caused the nine-hour Docker
            # shift: there is no honest way for a UTC server to guess which
            # user's clock it came from.
            raise MiniAppError("검색 시작 시각의 시간대 정보가 없어요. 화면을 새로 열어 주세요.")
        return as_utc(parsed)

    def _running(self, chat_id: int) -> dict | None:
        """The search this chat has going, if any."""
        running = self.storage.get_running_reservation(chat_id)
        if not running:
            return None
        return {
            **self._describe_params(running.search_params),
            "startedAt": as_utc(running.started_at).isoformat() if running.started_at else None,
        }

    def _scheduled(self, chat_id: int) -> dict | None:
        """The search booked to start later, if any."""
        scheduled = self.storage.get_scheduled_search(chat_id)
        if not scheduled:
            return None
        return {
            "startAt": as_utc(scheduled.start_at).isoformat() if scheduled.start_at else None,
            "timeZone": scheduled.timezone_name,
            "search": self._describe_params(scheduled.search_params),
        }

    @staticmethod
    def _describe_params(params: TrainSearchParams) -> dict:
        """What a search is looking for, in the app's own field names."""
        return {
            "depDate": params.dep_date,
            "srcLocate": params.src_locate,
            "dstLocate": params.dst_locate,
            "depTime": params.dep_time[:4],
            "maxDepTime": params.max_dep_time,
            "trainTypeShow": params.train_type_display,
            "specialInfoShow": params.special_option_display,
            "passengerCount": params.passenger_count,
            "seatStrategy": params.seat_strategy,
            "seatPreference": params.seats_wanted.describe(),
            "selectedTrains": list(params.train_numbers),
        }

    def _pending(self, chat_id: int) -> list[dict]:
        """Seats held and waiting to be paid for."""
        return [
            {
                "reservationId": item.reservation_id,
                "trainInfo": item.train_info,
                "expiresAt": as_utc(item.expires_at).isoformat() if item.expires_at else None,
                "seatNumber": item.seat_number,
                "seatLabels": list(item.seat_labels or []),
                "seatClass": item.seat_class,
            }
            for item in self.pending_payments.pending(chat_id)
        ]
