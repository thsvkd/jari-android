"""Booking adapter: approved identities use their own encrypted railway login."""

import json
from datetime import UTC, datetime, timedelta
from functools import wraps
from time import monotonic

from korail2 import TrainType
from korail_mobile_api.errors import (
    KorailAppError,
    KorailAuthContinuationRequired,
    KorailAuthError,
    KorailReservationRefusedError,
    KorailSeatUnavailableError,
    KorailSoldOutError,
)

from korail_bot.config.settings import settings
from korail_bot.handlers.conversation_handler import ConversationHandler
from korail_bot.mobile.config import MAX_REQUEST_BYTES, WAITLIST_CHECK_MINUTES
from korail_bot.models import OnboardedAccount, PaymentStatus, SeatPreference, SeatTarget
from korail_bot.models.station_snapshot import FALLBACK_STATIONS
from korail_bot.services.access_service import AccessDecision, AccessLevel, AccessService
from korail_bot.services.cancellation_wait_service import train_label
from korail_bot.services.korail_service import KorailService
from korail_bot.services.mini_app_gateway import (
    SOLD_OUT_CLASS,
    MiniAppError,
    MiniAppGateway,
    login_failure,
)
from korail_bot.services.mini_app_service import MiniAppDataError, MiniAppSubmission
from korail_bot.services.seat_map_service import (
    SeatMapExpiredError,
    SeatMapNotFoundError,
    SeatMapService,
)
from korail_bot.utils.logger import get_logger
from korail_bot.utils.timezone import RAIL_TIMEZONE, as_utc, format_user_datetime, utc_now

logger = get_logger(__name__)

# How many cars in a row may fail before a whole-formation read gives up. An
# expired session or a Korail outage fails every car for the same reason, and
# finishing the formation anyway would hold this user's mutation lock - which
# is striped, so it is not only this user's - for one HTTP timeout per
# remaining car.
MAX_CONSECUTIVE_CAR_FAILURES = 3

# How long an idle Korail login is kept for a user's seat-map browsing.
# Logging in on every car tab risked locking the Korail account. Each use
# extends it, and it is no shorter than a train key lives (SeatMapService),
# so every live key is served by the login that issued it - and by the same
# reference formation a sold-out train was drawn with. A fresh login searches
# for that formation again and can land on another date whose car list
# differs, which is how a car could go missing from "모든 호차에 적용".
RAIL_SESSION_SECONDS = 1800
# Korail's refusals that say this account already holds the train (the Korail app sends the person
# to the reservation list for the last two): not "someone else took the seat".
ALREADY_BOOKED_CODES = {"WRR800029", "ERR911531", "ERR911051"}
UNSURE_BOOKING = "예약이 됐는지 확인하지 못했어요. 코레일 앱의 예약 목록을 먼저 확인해 주세요."
# Standbys an owner may have applied for and still be waiting on (or assigned) at once.
WAITLIST_LIMIT = 5
WAITLIST_ACTIVE = ("waiting", "assigned")
# Looks that wait for the person to connect the Korail account again (register): a refused
# login, and a reply like a block - as a blocked watch ends and only a new one starts again.
WAITLIST_PAUSED = ("login", "blocked")
# Passes in a row with the login gone again right after a fresh one, taken as a block.
WAITLIST_EXPIRY_LIMIT = 2
# What the app is shown of a standby record (not the journey count or the account tag).
WAITLIST_FIELDS = (
    "id",
    "trainNo",
    "trainInfo",
    "depDate",
    "depTime",
    "passengerCount",
    "state",
    "createdAt",
    "checkedAt",
    "checkFailed",
    "checkError",
    "expiresAt",
    "seatLabels",
    "finishedAt",
)


class _WaitlistLoginRefused(Exception):
    """Korail refused the linked account's login during a standby look."""


class _WaitlistBlocked(Exception):
    """Korail answered a standby look (or its login) the way it answers a blocked client."""


WAITLIST_BLOCKED = "코레일이 접속을 막은 것 같아요. 더 묻지 않고 멈췄어요. 코레일 앱에서 예약 대기를 확인해 주세요."


WAITLIST_CANCEL_BLOCKED = (
    "코레일이 접속을 막은 것 같아 예약 대기를 취소하지 못했어요. 코레일 앱에서 직접 취소해 주세요."
)


def _blocked(rail, exc):
    """Whether a failure is the reply a blocked client gets (KorailService.looks_blocked)."""
    if isinstance(exc, _WaitlistBlocked):
        return True
    judge = getattr(rail, "looks_blocked", None)
    return callable(judge) and judge(exc) is True


class WaitlistsActive(MiniAppError):
    """Deleting an account that still has standbys, without saying they are looked after."""

    code = "waitlists_active"


LOGIN_REFUSED = (
    "코레일에 로그인하지 못했어요. 잠시 후 다시 시도하고, 계속되면 계정을 다시 연결해 주세요."
)


# Korail's answer to a request on a login it has since dropped, when it is not P058: after ~30
# idle minutes a seat read came back FAIL with "[3]인증정보에 문제가 있습니다." as its code and
# message (production, 2026-10-14 05:31 KST), a plain KorailAppError.
_LOGIN_GONE_TEXT = "인증정보에 문제"


def _login_gone(exc):
    """Whether Korail turned a request away because the login behind it is gone."""
    said = f"{getattr(exc, 'code', '') or ''} {getattr(exc, 'message', '') or ''}"
    return _LOGIN_GONE_TEXT in said


def serialized_operation(method):
    """The scheduler and HTTP gateway must share one mutation boundary."""

    @wraps(method)
    def wrapped(self, chat_id, *args, **kwargs):
        with self.reservation.operation_lock(chat_id):
            try:
                return method(self, chat_id, *args, **kwargs)
            except MiniAppError as exc:
                # 502 is "Korail did not answer", and an expired session is
                # one way that happens. Drop it so the retry logs in afresh.
                if exc.status == 502:
                    self._forget_rail(chat_id)
                raise

    return wrapped


class MobileSubmission(MiniAppSubmission):
    # The app posts its conditions over HTTP, so what bounds them is the body
    # limit, not Telegram's 64KB: a whole-formation seat plan is larger.
    MAX_DATA_BYTES = MAX_REQUEST_BYTES

    @classmethod
    def _validated_station(cls, payload, key):
        value = cls._text(payload, key)
        if value not in FALLBACK_STATIONS:
            raise MiniAppDataError("목록에서 지원하는 역을 선택해 주세요.")
        return value


class InvitedAccess(AccessService):
    def evaluate(self, phone_number, is_developer=False):
        # Railway access is limited to app accounts approved by the operator.
        # It grants booking access, never developer or server-account rights.
        return AccessDecision(AccessLevel.APPROVED)


class MobileConversation(ConversationHandler):
    def uses_server_account(self, chat_id):
        return False

    def _remember_account(self, chat_id, username, password):
        # Registration may only report success once the durable write succeeds.
        self.storage.save_onboarded_account(
            OnboardedAccount(chat_id=chat_id, korail_id=username, korail_pw=password)
        )


class MobileGateway(MiniAppGateway):
    def __init__(
        self,
        *args,
        seat_map_service=None,
        waitlist_check_minutes=WAITLIST_CHECK_MINUTES,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.seat_maps = seat_map_service or SeatMapService()
        self.waitlist_check_minutes = waitlist_check_minutes
        # Touched only by the standby look's own thread (WaitlistWatch).
        self._waitlist_rails = {}
        self._waitlist_attempts = {}
        # Passes in a row whose fresh login was "gone" again at once (_waitlist_call).
        self._waitlist_expiries = {}
        # chat_id -> (rail, expires_at, korail_id). Only touched under the
        # owner's operation lock, so a plain dict is enough.
        self._rails = {}

    @serialized_operation
    def register(self, chat_id, username, password):
        self._forget_rail(chat_id)
        result = super().register(chat_id, username, password)
        # A standby check paused on a refused login may log in again with the new account.
        entries = self._stored_waitlists(chat_id)
        paused = [entry for entry in entries if entry.pop("checkError", None)]
        if paused:
            self.storage.save_waitlists(chat_id, entries)
        connected = getattr(self.storage, "mark_waitlist_connected", None)
        if connected:
            # Connecting the account again is the person saying to try again after a block.
            connected(chat_id)
        return result

    @serialized_operation
    def list_trains(self, chat_id, payload):
        session, submission = self._prepared_session(chat_id, payload)
        rail = self._logged_in_rail(chat_id)
        try:
            trains = self._again_if_expired(
                chat_id,
                rail,
                lambda: rail.search_selectable_trains(
                    dep_date=submission.dep_date,
                    src_locate=submission.src_station,
                    dst_locate=submission.dst_station,
                    dep_time=f"{submission.dep_time}00",
                    max_dep_time=submission.max_dep_time,
                    train_type=TrainType.KTX if submission.train_type == "1" else TrainType.ALL,
                    passenger_count=submission.passenger_count,
                ),
            )
        except MiniAppError:
            raise
        except Exception as exc:
            logger.error(
                "Could not list current Korail trains for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )
            raise MiniAppError(
                "열차 목록을 불러오지 못했어요. 잠시 후 다시 조회해 주세요.", 502
            ) from exc

        truncated = (
            len(trains) > self.conversation.MAX_TRAIN_OPTIONS or rail.search_cut_short is True
        )
        trains = trains[: self.conversation.MAX_TRAIN_OPTIONS]
        options = []
        for train in trains:
            option = rail.describe_waitlist_train(train)
            option.update(
                {
                    "trainKey": self.seat_maps.remember_train(chat_id, train),
                    "generalAvailable": getattr(train, "general_reservation_code", None) == "11",
                    "specialAvailable": getattr(train, "special_reservation_code", None) == "11",
                    "trainGroup": getattr(train, "train_group_name", None),
                }
            )
            options.append(option)
        session.train_info["trainOptions"] = options
        self.storage.save_user_session(session)
        return {
            "trains": options,
            "truncated": truncated,
            "passengerCount": submission.passenger_count,
        }

    @serialized_operation
    def listed_trains(self, chat_id, payload):
        """
        The train numbers list_trains last offered for this trip (date and route),
        or [] if the last list was for another trip or there is none. Agents check
        their picks against it; the app picks from the list itself.

        Only the ones a watch with these conditions can see: the worker searches from this
        submission's earliest time to its latest, and a KTX watch never meets an ITX.
        """
        submission = self._submission(payload)
        session = self.storage.get_user_session(chat_id)
        info = session.train_info if session else {}
        trip = (submission.dep_date, submission.src_station, submission.dst_station)
        if (info.get("depDate"), info.get("srcLocate"), info.get("dstLocate")) != trip:
            return []
        ktx_only = submission.train_type == "1"
        return [
            str(option.get("no"))
            for option in info.get("trainOptions") or []
            if self._watchable(option, submission, ktx_only)
        ]

    @staticmethod
    def _watchable(option, submission, ktx_only):
        # The seat-plan worker filters on the train group; the older worker asks Korail for KTX
        # (korail2 TrainType.KTX, group 100). Both APIs put KTX-이음 in group 100 (production,
        # 2026-10-13), so the group is the test; names are the fallback.
        group = option.get("trainGroup")
        if ktx_only and not (
            group == "KTX" if group else str(option.get("name") or "").startswith("KTX")
        ):
            return False
        clock = str(option.get("dep_time") or "")[:4]
        # A departure time that cannot be read is left to the worker's own filter.
        return not clock.isdigit() or (
            int(submission.dep_time) <= int(clock) < int(submission.max_dep_time)
        )

    @serialized_operation
    def seat_cars(self, chat_id, train_key, seat_class, passenger_count):
        train = self._seat_train(chat_id, train_key)
        count = self._passenger_count(passenger_count)
        rail = self._logged_in_rail(chat_id)
        try:
            response = self._again_if_expired(
                chat_id, rail, lambda: rail.seat_cars(train, seat_class, count)
            )
            return {
                "cars": self.seat_maps.describe_cars(response),
                "layoutReference": bool(rail.seat_layout_is_reference(train, seat_class, count)),
            }
        except ValueError as exc:
            raise MiniAppError(str(exc), 422) from exc
        except MiniAppError:
            raise
        except Exception as exc:
            # The message is safe to log here: no request on this path carries
            # the password, and a protocol error's text names the failing field.
            logger.error(
                "Could not read Korail cars for chat_id=%s (%s: %s) %s",
                chat_id,
                type(exc).__name__,
                exc,
                KorailService.describe_train_row(train),
            )
            raise MiniAppError(
                "호차 정보를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.", 502
            ) from exc

    def _described_inventory(self, rail, train, car_no, seat_class, count):
        """One car's seats, shaped the way the app caches them per car."""
        response = rail.seat_inventory(
            train,
            car_no,
            seat_class,
            count,
            allow_layout_reference=True,
        )
        result = self.seat_maps.describe_inventory(response)
        result["layoutReference"] = bool(rail.seat_layout_is_reference(train, seat_class, count))
        return result

    @serialized_operation
    def seat_inventory(self, chat_id, train_key, car_no, seat_class, passenger_count):
        train = self._seat_train(chat_id, train_key)
        count = self._passenger_count(passenger_count)
        rail = self._logged_in_rail(chat_id)
        try:
            return self._again_if_expired(
                chat_id,
                rail,
                lambda: self._described_inventory(rail, train, car_no, seat_class, count),
            )
        except ValueError as exc:
            raise MiniAppError(str(exc), 422) from exc
        except MiniAppError:
            raise
        except Exception as exc:
            logger.error(
                "Could not read Korail seats for chat_id=%s (%s: %s) %s",
                chat_id,
                type(exc).__name__,
                exc,
                KorailService.describe_train_row(train),
            )
            raise MiniAppError(
                "좌석표를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.", 502
            ) from exc

    @serialized_operation
    def seat_inventories(self, chat_id, train_key, seat_class, passenger_count):
        """
        Every car of one train in a single request.

        Read per car, this costs a Korail login each time and trips the rail
        rate limit at around ten cars, so "apply to every car" could not finish
        on an eighteen-car KTX. One login and one car list here, then the
        per-car reads reuse the ScheduleView context korail_service already
        caches on the rail instance.
        """
        train = self._seat_train(chat_id, train_key)
        count = self._passenger_count(passenger_count)
        rail = self._logged_in_rail(chat_id)
        try:
            cars = self._again_if_expired(
                chat_id, rail, lambda: rail.seat_cars(train, seat_class, count)
            )
        except ValueError as exc:
            raise MiniAppError(str(exc), 422) from exc
        except MiniAppError:
            raise
        except Exception as exc:
            logger.error(
                "Could not read Korail cars for chat_id=%s (%s: %s) %s",
                chat_id,
                type(exc).__name__,
                exc,
                KorailService.describe_train_row(train),
            )
            raise MiniAppError(
                "호차 정보를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.", 502
            ) from exc

        inventories = []
        failed = []
        reasons = []
        consecutive = 0
        # 잔여석이 있는 호차를 먼저 읽어요. 목록에서 빠져 참고 편성으로 채운 호차(잔여석 0)는 그 뒤에 몰아 읽어야
        # 코레일 쪽 열차 문맥을 오가는 일이 적고, 참고 편성을 읽지 못해도 실제 호차가 그 실패에 묻히지 않아요.
        car_numbers = [
            car.car_no for car in sorted(cars.cars, key=lambda car: car.remaining_seat_count <= 0)
        ]
        for index, car_no in enumerate(car_numbers):
            try:
                inventories.append(
                    self._described_inventory(rail, train, car_no, seat_class, count)
                )
                consecutive = 0
            except Exception as exc:
                # A car that could not be read is not a car with no free seats.
                # Naming it lets the app say so instead of drawing it empty.
                failed.append(car_no)
                reasons.append(type(exc).__name__)
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_CAR_FAILURES:
                    # Whatever is wrong is not specific to these cars. Give the
                    # app what was read and stop paying a timeout per car.
                    failed.extend(car_numbers[index + 1 :])
                    break
        if failed:
            logger.error(
                "Could not read Korail seats for chat_id=%s cars=%s (%s)",
                chat_id,
                failed,
                ",".join(sorted(set(reasons))),
            )
        # Every car failing is a failed read, and answering 200 with nothing in
        # it would look exactly like a sold-out train. A train that simply has
        # no bookable car answers with two empty lists, which does not.
        if failed and not inventories:
            raise MiniAppError("좌석표를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.", 502)
        return {
            # 읽은 순서(실제 호차 먼저)가 아니라 호차 순서로 돌려줘요. 앱의 선택 요약과 대기 대상이 이 순서를 따라요.
            "inventories": sorted(inventories, key=lambda inventory: inventory["carNo"]),
            "failedCars": failed,
            "layoutReference": bool(rail.seat_layout_is_reference(train, seat_class, count)),
        }

    @serialized_operation
    def reserve_designated(self, chat_id, payload):
        if self.pending_payments.pending(chat_id):
            raise MiniAppError(
                "결제를 기다리는 예약이 있어요. 먼저 결제하거나 예약을 취소해 주세요.", 409
            )
        train_key = payload.get("trainKey")
        seat_class = payload.get("seatClass")
        car_no = payload.get("carNo")
        if not isinstance(train_key, str) or not isinstance(seat_class, str):
            raise MiniAppError("열차와 좌석 등급을 다시 선택해 주세요.", 422)
        if isinstance(car_no, bool) or not isinstance(car_no, int) or not 1 <= car_no <= 99:
            raise MiniAppError("호차를 다시 선택해 주세요.", 422)
        count = self._passenger_count(payload.get("passengerCount"))
        raw_targets = payload.get("seats")
        if not isinstance(raw_targets, list) or len(raw_targets) != count:
            raise MiniAppError("승객 수만큼 좌석을 선택해 주세요.", 422)
        try:
            targets = [SeatTarget.from_payload(target) for target in raw_targets]
        except ValueError as exc:
            raise MiniAppError(str(exc), 422) from exc
        train = self._seat_train(chat_id, train_key)
        rail = self._logged_in_rail(chat_id)
        try:
            drawn_from_another_date = rail.seat_layout_is_reference(train, seat_class, count)
        except ValueError as exc:
            raise MiniAppError(str(exc), 409) from exc
        if drawn_from_another_date is True:
            # The seat map was drawn from another date because this class is sold out. Its
            # seats are not on sale on this train, and reading "the current car" would read
            # that date's.
            raise MiniAppError(SOLD_OUT_CLASS, 409)

        def book():
            current = rail.seat_inventory(train, car_no, seat_class, count)
            return rail.reserve_designated(
                train,
                current,
                targets,
                passenger_count=count,
                seat_class=seat_class,
            )

        try:
            # An expired login is Korail turning the request away before it books; it is asked
            # again on a fresh login unless the answer names a reservation (_again_if_expired).
            hold = self._again_if_expired(chat_id, rail, book, booking=True)
        except ValueError as exc:
            raise MiniAppError(str(exc), 409) from exc
        except MiniAppError:
            raise
        except (
            KorailSoldOutError,
            KorailSeatUnavailableError,
            KorailReservationRefusedError,
        ) as exc:
            # Korail answered: it will not take this booking. Not an outage.
            logger.warning(
                "Korail refused designated seats for chat_id=%s (%s %s)",
                chat_id,
                type(exc).__name__,
                getattr(exc, "code", None),
            )
            message, status = self._refusal(exc)
            raise MiniAppError(message, status) from exc
        except Exception as exc:
            logger.error(
                "Could not reserve designated seats for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )
            raise MiniAppError(
                "선택한 좌석을 예약하지 못했어요. 좌석표를 새로 불러와 주세요.", 502
            ) from exc

        reservation_id = rail.reservation_id(hold)
        if not reservation_id:
            self._release_failed_hold(rail, hold, chat_id)
            raise MiniAppError(
                "예약 번호를 확인하지 못했어요. 코레일 예약 목록을 확인해 주세요.", 502
            )
        expires_at = self._payment_deadline(rail, hold)
        train_info = train_label(train)
        # 좌석표 예약과 취소표 대기가 같은 모양으로 적어요. 호차가 없으면 어느 칸인지 알 수 없어요.
        labels = [f"{target.car_no}호차 {target.label}" for target in targets]
        status = PaymentStatus(
            chat_id=chat_id,
            completed=False,
            reminder_active=True,
            reservation_id=reservation_id,
            train_info=train_info,
            expires_at=expires_at,
            train_no=str(getattr(train, "train_no", "") or ""),
            dep_date=str(getattr(train, "departure_date", "") or ""),
            dep_time=str(getattr(train, "departure_time", "") or ""),
            seat_labels=labels,
            seat_class=seat_class,
        )
        try:
            self.storage.save_payment_status(status)
        except Exception as exc:
            self._release_failed_hold(rail, hold, chat_id)
            raise MiniAppError(
                "예약 정보를 안전하게 저장하지 못해 좌석을 다시 돌려보냈어요.", 503
            ) from exc
        try:
            self.telegram.publish(
                chat_id,
                f"{train_info} {', '.join(labels)} 좌석을 예약했어요. 결제 기한 안에 코레일에서 결제해 주세요.",
                kind="payment",
                dedupe=f"designated:{chat_id}:{reservation_id}",
            )
        except Exception as exc:
            logger.error(
                "Could not publish designated-seat notification for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )
        session = self.storage.get_user_session(chat_id)
        if session:
            session.reset()
            self.storage.save_user_session(session)
        return {
            "reserved": True,
            "pending": self._pending(chat_id),
            "paymentUrl": settings.KORAIL_PAYMENT_URL,
        }

    @staticmethod
    def _refusal(exc) -> tuple[str, int]:
        """
        What Korail's refusal means to the person, and the status to say it with.

        409 is "this seat was just sold": the app reloads the seat map on it. A refusal that is not
        about the seat (the account already holds the train) is a 422, so that nothing retries it
        with another seat and the app shows the sentence as it is.
        """
        if isinstance(exc, KorailReservationRefusedError):
            if getattr(exc, "code", None) in ALREADY_BOOKED_CODES:
                return (
                    "이 계정에 이미 이 열차의 예약이 있어요. 코레일 예약 목록을 확인해 주세요.",
                    422,
                )
            return (
                "코레일이 예약을 받아 주지 않았어요. 코레일 앱에서 예약 목록을 확인해 주세요.",
                422,
            )
        return (
            "코레일이 이 좌석을 받아 주지 않았어요. 이미 팔렸을 수 있으니 좌석표를 새로 불러와 주세요.",
            409,
        )

    @staticmethod
    def _release_failed_hold(rail, hold, chat_id):
        try:
            rail.release_unpaid_hold(hold)
        except Exception as exc:
            logger.critical(
                "Could not release untracked Korail hold for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )

    def reserved_train(self, chat_id, train_key):
        """Which train a trainKey is, for checking what an agent says it books; None if unknown."""
        try:
            train = self.seat_maps.get_train(chat_id, train_key)
        except (SeatMapExpiredError, SeatMapNotFoundError):
            return None
        return {
            "trainNo": str(getattr(train, "train_no", "") or ""),
            "depDate": str(getattr(train, "departure_date", "") or ""),
            "from": str(getattr(train, "departure_station_name", "") or ""),
            "to": str(getattr(train, "arrival_station_name", "") or ""),
        }

    def _seat_train(self, chat_id, train_key):
        try:
            return self.seat_maps.get_train(chat_id, train_key)
        except SeatMapExpiredError as exc:
            raise MiniAppError(str(exc), 410) from exc
        except SeatMapNotFoundError as exc:
            raise MiniAppError(str(exc), 404) from exc

    @staticmethod
    def _passenger_count(value):
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise MiniAppError("승객 수를 확인해 주세요.", 422) from exc
        if isinstance(value, bool) or not 1 <= count <= 9:
            raise MiniAppError("승객 수는 1명에서 9명 사이여야 해요.", 422)
        return count

    def _logged_in_rail(self, chat_id):
        account = self.storage.get_onboarded_account(chat_id)
        if not account:
            self._forget_rail(chat_id)
            raise MiniAppError("코레일 계정을 연결해 주세요.", 428)
        cached = self._rails.get(chat_id)
        if cached and cached[1] > monotonic() and cached[2] == account.korail_id:
            self._rails[chat_id] = (cached[0], monotonic() + RAIL_SESSION_SECONDS, cached[2])
            return cached[0]
        self._forget_rail(chat_id)
        rail = self.conversation._rail_service(chat_id)
        if not rail.login(account.korail_id, account.korail_pw):
            raise login_failure(rail, LOGIN_REFUSED, 428)
        self._rails[chat_id] = (rail, monotonic() + RAIL_SESSION_SECONDS, account.korail_id)
        return rail

    def _again_if_expired(self, chat_id, rail, call, *, booking=False):
        """
        call(), and once more on a fresh login if Korail says this one is gone.

        Korail drops an idle login sooner than RAIL_SESSION_SECONDS keeps it (production: gone
        after 28 idle minutes, KorailSessionExpiredError P058), so a booking made a while after
        the seat map failed once. After a longer idle Korail answered a seat read FAIL with
        "인증정보에 문제" instead (a plain KorailAppError, _login_gone). The client also forgets a
        login a swallowed P058 ended, and then refuses before sending anything
        (KorailAuthError). The same rail logs in again rather than a new one: it keeps the
        reference formation its train keys were drawn with, and only the server-side seat
        context is set up afresh.

        A booking is asked again only when the expiry carries no reservation number: Korail
        checks the session first, but an answer that names a reservation means something may
        be held, and booking again would leave that one untracked.
        """
        try:
            return call()
        except KorailAuthContinuationRequired:
            raise
        except (KorailAuthError, KorailAppError) as exc:
            if isinstance(exc, KorailAppError) and not _login_gone(exc):
                raise
            raw = getattr(exc, "raw", None)
            if booking and isinstance(raw, dict) and raw.get("h_pnr_no"):
                logger.critical(
                    "Korail answered a booking for chat_id=%s as expired with a reservation number",
                    chat_id,
                )
                raise MiniAppError(UNSURE_BOOKING, 502) from exc
            logger.info(
                "Korail login gone for chat_id=%s (%s); logging in again",
                chat_id,
                type(exc).__name__,
            )
        account = self.storage.get_onboarded_account(chat_id)
        if not account:
            self._forget_rail(chat_id)
            raise MiniAppError("코레일 계정을 연결해 주세요.", 428)
        if not rail.login(account.korail_id, account.korail_pw):
            self._forget_rail(chat_id)
            raise login_failure(rail, LOGIN_REFUSED, 428)
        return call()

    def _forget_rail(self, chat_id):
        cached = self._rails.pop(chat_id, None)
        if cached:
            cached[0].close()

    @staticmethod
    def _payment_deadline(rail, hold):
        raw_date, raw_time = rail.payment_due(hold)
        if isinstance(raw_date, str) and isinstance(raw_time, str):
            try:
                local = datetime.strptime(f"{raw_date}{raw_time[:6]}", "%Y%m%d%H%M%S")
                return as_utc(local, naive_zone=RAIL_TIMEZONE)
            except ValueError:
                pass
        return utc_now() + timedelta(minutes=settings.PAYMENT_TIMEOUT_MINUTES)

    @serialized_operation
    def start_search(self, chat_id, payload):
        submission = self._submission(payload)
        if not submission.waitlist:
            return super().start_search(chat_id, payload)

        full = MiniAppError(
            f"코레일 예약 대기는 한 번에 {WAITLIST_LIMIT}건까지 신청할 수 있어요. "
            "홈에서 필요 없는 대기를 취소한 뒤 신청해 주세요.",
            409,
        )
        active = [
            entry
            for entry in self._stored_waitlists(chat_id)
            if entry.get("state") in WAITLIST_ACTIVE
        ]
        # Only the ones this account can withdraw count: another account's are out of its reach.
        # Counted from the records first when the account is known, so a full list costs no login.
        linked = self.storage.get_onboarded_account(chat_id)
        known = self._known_member_tag(chat_id, linked)
        if known and sum(self._same_account(e, known) for e in active) >= WAITLIST_LIMIT:
            raise full

        session, submission = self._prepared_session(chat_id, payload)
        selected_trains = self._selected_trains(payload)
        session.train_info["selectedTrains"] = selected_trains
        self.storage.save_user_session(session)

        if len(selected_trains) != 1:
            raise MiniAppError("예약 대기를 신청할 열차 한 편을 골라 주세요.", 422)
        credentials = session.credentials
        if credentials is None:
            raise MiniAppError("코레일 계정을 다시 연결한 뒤 신청해 주세요.", 428)

        train_no = selected_trains[0]
        if any(
            entry.get("trainNo") == train_no and entry.get("depDate") == submission.dep_date
            for entry in active
        ):
            raise MiniAppError(
                "이미 이 열차에 코레일 예약 대기를 신청했어요. 홈에서 확인해 주세요.", 409
            )
        rail = self.conversation._rail_service(chat_id)
        if not rail.login(credentials.korail_id, credentials.korail_pw):
            if getattr(rail, "login_blocked", False) is True:
                self._pause_blocked(chat_id, utc_now())
                raise MiniAppError(WAITLIST_BLOCKED, 502)
            raise login_failure(rail, LOGIN_REFUSED, 428)
        # Remembered against the linked login, the one the screen and the slow look compare with.
        member = self._member_tag(
            chat_id, linked.korail_id if linked else credentials.korail_id, rail
        )
        if member is None:
            # Without the member a standby could not be told from another account's later.
            raise MiniAppError(
                "코레일 회원 정보를 확인하지 못했어요. 잠시 뒤 다시 신청해 주세요.", 502
            )
        if sum(self._same_account(entry, member) for entry in active) >= WAITLIST_LIMIT:
            raise full
        try:
            registered = rail.request_waitlist(
                dep_date=submission.dep_date,
                src_locate=submission.src_station,
                dst_locate=submission.dst_station,
                dep_time=f"{submission.dep_time}00",
                train_no=train_no,
                passenger_count=submission.passenger_count,
            )
        except ValueError as exc:
            raise MiniAppError(str(exc), 409) from exc
        except (KorailSoldOutError, KorailReservationRefusedError) as exc:
            # Korail answered and would not take it (already applied, no standby left): not an outage.
            logger.warning(
                "Korail refused a standby for chat_id=%s (%s %s)",
                chat_id,
                type(exc).__name__,
                getattr(exc, "code", None),
            )
            raise MiniAppError(
                "코레일이 예약 대기 신청을 받아 주지 않았어요. 코레일 앱에서 예약 내역을 확인해 주세요.",
                422,
            ) from exc
        except Exception as exc:
            logger.error(
                "Could not register Korail standby for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )
            if _blocked(rail, exc):
                # The account's other standbys stop as well: a block is the account's, not a train's.
                self._pause_blocked(chat_id, utc_now())
                raise MiniAppError(WAITLIST_BLOCKED, 502) from exc
            raise MiniAppError(
                "코레일 예약 대기를 신청하지 못했어요. 잠시 후 목록을 새로 조회해 주세요.",
                502,
            ) from exc

        reservation_id = registered.get("reservation_id") or ""
        if not reservation_id:
            # Korail took it but named no number: nothing to follow or withdraw it by.
            logger.critical("Korail standby for chat_id=%s came back without a number", chat_id)
            raise MiniAppError(
                "예약 대기가 신청됐을 수 있지만 예약번호를 받지 못했어요. 코레일 앱에서 예약 내역을 확인해 주세요.",
                503,
            )
        train = registered.get("train")
        entry = {
            "id": reservation_id,
            "journeyCount": registered.get("journey_count") or "",
            # The Korail member this was applied with, the one later looks and withdrawals compare
            # against (a keyed hash, not the number).
            "account": member,
            "trainNo": registered["train_no"],
            "trainInfo": train_label(train) if train is not None else f"열차 {train_no}",
            "depDate": registered.get("dep_date") or submission.dep_date,
            "depTime": registered.get("dep_time") or "",
            "passengerCount": submission.passenger_count,
            "state": "waiting",
            "createdAt": utc_now().isoformat(),
            "checkedAt": None,
            "checkFailed": False,
        }
        try:
            self.storage.save_waitlists(chat_id, [*self._stored_waitlists(chat_id), entry])
        except Exception as exc:
            # A standby the app cannot follow would be assigned without anyone hearing of it.
            logger.error(
                "Could not record a Korail standby for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )
            try:
                rail.cancel_waitlist(entry["id"], entry["journeyCount"])
            except Exception as cancel_error:
                logger.critical(
                    "Could not withdraw an unrecorded Korail standby for chat_id=%s (%s)",
                    chat_id,
                    type(cancel_error).__name__,
                )
                raise MiniAppError(
                    "예약 대기는 신청됐지만 앱에 저장하지 못했어요. 코레일 앱에서 예약 내역을 확인해 주세요.",
                    503,
                ) from exc
            raise MiniAppError(
                "예약 대기 정보를 안전하게 저장하지 못해 신청을 취소했어요. 잠시 후 다시 신청해 주세요.",
                503,
            ) from exc

        try:
            self.telegram.send_message(
                chat_id,
                f"{entry['trainInfo']} 코레일 예약 대기를 신청했어요. 좌석이 배정된 것 같으면 알려드려요. "
                f"배정 여부는 {self._check_minutes_text()}마다 한 번만 확인하니, 코레일 앱 알림도 함께 확인해 주세요.",
            )
            session.reset()
            self.storage.save_user_session(session)
        except Exception as exc:
            # Korail already holds the standby; failing here would invite a second one.
            logger.error(
                "Standby registered but the follow-up failed for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )
        return {"started": False, "waitlisted": True, "trainNo": registered["train_no"]}

    # ==================== Korail standby (예약 대기) after the request ====================
    #
    # A standby is one request; Korail assigns a freed seat by itself. What is left for the app
    # is to notice an assignment, which it does by reading the person's own reservation list:
    # once per owner per waitlist_check_minutes (never under ten), one list read shared by all of
    # that owner's standbys, plus one read per standby still listed. Following five standbys costs
    # a handful of reads an hour, unlike a watch that searches every few seconds.
    #
    # Telling "assigned" from "waiting" is this app's reading of Korail's answer (seats on the
    # reservation), not yet confirmed against Korail, and a standby's payment window can be hours
    # long. So an assigned standby is not handed to the payment watch (which asks every few
    # seconds): it stays here, is asked about at the same slow pace until Korail drops it, and is
    # shown as "seems assigned - check in the Korail app".

    # A finished standby (paid, ended, withdrawn) stays on the home screen this long.
    WAITLIST_FINISHED_KEPT = timedelta(hours=24)

    # Set by the runtime once it holds its lease: whether a pass should stop where it is.
    waitlist_should_stop = staticmethod(lambda: False)

    def _stored_waitlists(self, chat_id):
        reader = getattr(self.storage, "get_waitlists", None)
        return reader(chat_id) if reader else []

    def _account_tag(self, value):
        tag = getattr(self.storage, "account_tag", None)
        return tag(value) if tag and value else None

    def _member_tag(self, chat_id, korail_id, rail):
        """
        The tag of the Korail member a logged-in rail belongs to, remembered against the login
        it was seen with so the screen can tell another account's standby without a login. The
        login id alone cannot: one member logs in by phone, membership number or e-mail.
        """
        reader = getattr(rail, "member_number", None)
        number = reader() if callable(reader) else None
        if not isinstance(number, str) or not number.strip():
            return None
        tag = self._account_tag(f"member:{number.strip()}")
        saver = getattr(self.storage, "save_waitlist_link", None)
        if saver and tag:
            try:
                saver(chat_id, {"login": self._account_tag(f"login:{korail_id}"), "member": tag})
            except Exception as exc:
                logger.warning("Could not remember a standby account (%s)", type(exc).__name__)
        return tag

    def _known_member_tag(self, chat_id, account):
        """The linked account's member tag if it was seen for this very login, else None."""
        reader = getattr(self.storage, "get_waitlist_link", None)
        link = reader(chat_id) if reader and account else None
        if link and link.get("login") == self._account_tag(f"login:{account.korail_id}"):
            return link.get("member")
        return None

    def _check_minutes_text(self):
        return f"{max(1, round(self.waitlist_check_minutes))}분"

    def _waitlists(self, chat_id, now=None, entries=None):
        """
        The standbys to show, without their private fields. One applied for with a Korail
        account other than the one linked now is marked otherAccount (it is neither looked up
        nor withdrawn); with none linked, the active ones say so (checkError "unlinked").
        """
        now = now or utc_now()
        account = self.storage.get_onboarded_account(chat_id)
        tag = self._known_member_tag(chat_id, account)
        shown = []
        for entry in self._stored_waitlists(chat_id) if entries is None else entries:
            if not self._waitlist_kept(entry, now):
                continue
            view = {key: entry.get(key) for key in WAITLIST_FIELDS}
            active = entry.get("state") in WAITLIST_ACTIVE
            view["otherAccount"] = bool(active and tag and not self._same_account(entry, tag))
            if active and account is None:
                view["checkError"] = "unlinked"
            shown.append(view)
        return shown

    @staticmethod
    def _same_account(entry, tag):
        """
        Applied with this member. One with no tag (applied before the member was known) is not
        taken for this account's: it is judged by this account's list alone (listed_only).
        """
        return bool(tag) and entry.get("account") == tag

    def bootstrap(self, chat_id):
        return {
            **super().bootstrap(chat_id),
            "waitlists": self._waitlists(chat_id),
            "waitlistCheckMinutes": self.waitlist_check_minutes,
        }

    @staticmethod
    def _stamp(value):
        try:
            return as_utc(datetime.fromisoformat(value)) if isinstance(value, str) else None
        except ValueError:
            return None

    def _waitlist_kept(self, entry, now):
        """Whether a standby still belongs on the screen (and in storage)."""
        departs = self._departure(entry)
        if departs is not None and now > departs + timedelta(hours=1):
            return False
        if entry.get("state") in WAITLIST_ACTIVE:
            # Assigned or not, Korail keeps it no later than the departure, and the slow look
            # turns it into paid or ended once Korail drops it.
            return True
        finished = self._stamp(entry.get("finishedAt"))
        return finished is not None and now < finished + self.WAITLIST_FINISHED_KEPT

    @staticmethod
    def _departure(entry):
        moment = f"{entry.get('depDate') or ''}{(entry.get('depTime') or '')[:4]}"
        try:
            return as_utc(datetime.strptime(moment, "%Y%m%d%H%M"), naive_zone=RAIL_TIMEZONE)
        except ValueError:
            return None

    # A standby this account's list was seen without is looked for again after this long.
    WAITLIST_RECHECK_UNSEEN = timedelta(days=1)

    def _seen_missing(self, entry, tag, now):
        """Whether this account's list was seen without this standby lately."""
        seen = entry.get("mismatchSeen")
        if not tag or not isinstance(seen, dict) or seen.get("member") != tag:
            return False
        at = self._stamp(seen.get("at"))
        return at is not None and now - at < self.WAITLIST_RECHECK_UNSEEN

    def _checkable(self, entries, tag, now):
        """
        Standbys a look may ask about. Another account's (or an untagged one) are included until
        this account's list has been seen without them, and again a day after that: a tag that
        went stale is mended by finding the reservation in this account's list.
        """
        return [
            entry
            for entry in entries
            if entry.get("state") in WAITLIST_ACTIVE
            and entry.get("checkError") not in WAITLIST_PAUSED
            and entry.get("id")
            and not self._seen_missing(entry, tag, now)
        ]

    def _owner_due(self, chat_id, checkable, now, interval):
        """Whether this owner's standbys may be asked about now: the last look is `interval` old."""
        stamps = [self._stamp(entry.get("checkedAt")) for entry in checkable]
        stamps = [stamp for stamp in stamps if stamp]
        if not stamps:
            created = [self._stamp(entry.get("createdAt")) for entry in checkable]
            stamps = [min(stamp for stamp in created if stamp)] if any(created) else []
        # Kept in memory as well: a look whose record could not be saved still counts.
        if self._waitlist_attempts.get(chat_id):
            stamps.append(self._waitlist_attempts[chat_id])
        return not stamps or now - max(stamps) >= interval

    def check_waitlists(self, interval=None, now=None):
        """
        One pass over every owner's standbys; each owner is asked about at most once per
        `interval` (waitlist_check_minutes). Returns how many standbys were looked up.
        """
        interval = interval or timedelta(minutes=self.waitlist_check_minutes)
        lister = getattr(self.storage, "waitlist_owners", None)
        owners = lister() if lister else []
        checked = 0
        for chat_id in owners:
            if self.waitlist_should_stop():
                break
            try:
                checked += self._check_owner_waitlists(chat_id, interval, now or utc_now())
            except Exception as exc:
                logger.error(
                    "Standby check failed for chat_id=%s (%s)", chat_id, type(exc).__name__
                )
        # An owner whose standbys are all gone (finished, withdrawn, account deleted) keeps
        # nothing here: not the floor, and not a login holding their password.
        alive = set(owners)
        for chat_id in [owner for owner in self._waitlist_rails if owner not in alive]:
            self._drop_waitlist_rail(chat_id)
        for chat_id in [owner for owner in self._waitlist_attempts if owner not in alive]:
            self._waitlist_attempts.pop(chat_id, None)
        for chat_id in [owner for owner in self._waitlist_expiries if owner not in alive]:
            self._waitlist_expiries.pop(chat_id, None)
        return checked

    def _check_owner_waitlists(self, chat_id, interval, now):
        """
        Korail is asked without holding the owner's lock (it is striped, so it is other people's
        too); the answers are merged under it afterwards, onto whatever the records are by then.
        """
        entries = self._stored_waitlists(chat_id)
        account = self.storage.get_onboarded_account(chat_id)
        if account is None:
            self._drop_waitlist_rail(chat_id)
        known = self._known_member_tag(chat_id, account)
        checkable = self._checkable(entries, known, now) if account else []
        if not checkable or not self._owner_due(chat_id, checkable, now, interval):
            if any(not self._waitlist_kept(entry, now) for entry in entries):
                self._merge_waitlists(chat_id, now, {}, ids=(), background=True)
            return 0
        if self._watch_blocked_since_connected(chat_id):
            # The account's watch ended on a reply like a block after the account was last
            # connected: the standby look does not knock on the same door. Nothing is asked.
            self._merge_waitlists(
                chat_id,
                now,
                {},
                failure="blocked",
                ids={entry["id"] for entry in checkable},
                background=True,
            )
            return 0
        self._waitlist_attempts[chat_id] = now
        answers, failure, member, others = {}, None, known, set()
        try:
            rail = self._waitlist_rail(chat_id, account)
            member = self._member_tag(chat_id, account.korail_id, rail)
            if member is None:
                # Without the member nothing can be told apart: the look counts as failed, and
                # no standby is marked as missing from a list that was never matched to it.
                raise RuntimeError("Korail named no member for the login")
            others = {e["id"] for e in checkable if not self._same_account(e, member)}
            checkable = [
                e
                for e in checkable
                if e["id"] not in others or not self._seen_missing(e, member, now)
            ]
            others &= {e["id"] for e in checkable}
            if checkable:
                answers = self._waitlist_call(
                    rail,
                    account,
                    lambda: rail.waitlist_states(
                        [self._standby(e, listed_only=e["id"] in others) for e in checkable]
                    ),
                    owner=chat_id,
                )
            self._waitlist_expiries.pop(chat_id, None)
        except _WaitlistLoginRefused:
            # One refused login per pass at most: every standby of this account waits for the
            # account to be connected again (register), so the next pass does not try again.
            failure = "login"
            self._drop_waitlist_rail(chat_id)
            logger.warning("Standby check for chat_id=%s: Korail refused the login", chat_id)
        except Exception as exc:
            rail = self._waitlist_rails.get(chat_id, (None,))[0]
            failure = "blocked" if _blocked(rail, exc) else "failed"
            self._drop_waitlist_rail(chat_id)
            if failure == "blocked":
                # Nothing more is asked for this account - no login, no next standby - until the
                # person connects the account again, as a watch stops at the first such reply.
                logger.error("Standby check for chat_id=%s: Korail answered like a block", chat_id)
            else:
                logger.warning(
                    "Standby check for chat_id=%s failed (%s)", chat_id, type(exc).__name__
                )
        if self.waitlist_should_stop():
            return 0
        self._merge_waitlists(
            chat_id,
            now,
            answers,
            failure=failure,
            ids={entry["id"] for entry in checkable},
            member=member,
            others=others,
            background=True,
        )
        return len(checkable)

    @staticmethod
    def _standby(entry, *, listed_only=False):
        created = MobileGateway._stamp(entry.get("createdAt"))
        return {
            "id": entry["id"],
            "train_no": entry.get("trainNo") or "",
            "dep_date": entry.get("depDate") or "",
            "dep_time": entry.get("depTime") or "",
            "applied_on": created.astimezone(RAIL_TIMEZONE).strftime("%Y%m%d") if created else "",
            "listed_only": listed_only,
        }

    def _waitlist_rail(self, chat_id, account):
        """
        A login of the standby look's own, apart from the one the person's seat maps use: a
        failed look must not close the session someone is browsing seats with.
        """
        cached = self._waitlist_rails.get(chat_id)
        if cached and cached[1] == account.korail_id and cached[2] > monotonic():
            self._waitlist_rails[chat_id] = (
                cached[0],
                cached[1],
                monotonic() + RAIL_SESSION_SECONDS,
            )
            return cached[0]
        self._drop_waitlist_rail(chat_id)
        rail = self.conversation._rail_service(chat_id)
        self._log_in_for_waitlist(rail, account)
        self._waitlist_rails[chat_id] = (
            rail,
            account.korail_id,
            monotonic() + RAIL_SESSION_SECONDS,
        )
        return rail

    @staticmethod
    def _log_in_for_waitlist(rail, account):
        if rail.login(account.korail_id, account.korail_pw):
            return
        if getattr(rail, "login_blocked", False) is True:
            raise _WaitlistBlocked()
        if getattr(rail, "login_unavailable", False) is True:
            raise RuntimeError("Korail did not take the login")
        raise _WaitlistLoginRefused()

    def _waitlist_call(self, rail, account, call, *, owner=None):
        """
        call(), and once more on a fresh login if Korail says the login is gone. A login that is
        "gone" again right after a fresh one, in two passes running, is how a block can look
        too: it stops like one rather than logging in every ten minutes forever.
        """
        try:
            return call()
        except KorailAuthContinuationRequired:
            raise
        except (KorailAuthError, KorailAppError) as exc:
            if _blocked(rail, exc):
                raise
            if isinstance(exc, KorailAppError) and not _login_gone(exc):
                raise
        self._log_in_for_waitlist(rail, account)
        try:
            return call()
        except (KorailAuthError, KorailAppError) as exc:
            if _blocked(rail, exc) or (isinstance(exc, KorailAppError) and not _login_gone(exc)):
                raise
            if owner is not None:
                self._waitlist_expiries[owner] = self._waitlist_expiries.get(owner, 0) + 1
                if self._waitlist_expiries[owner] >= WAITLIST_EXPIRY_LIMIT:
                    raise _WaitlistBlocked() from exc
            raise

    def _drop_waitlist_rail(self, chat_id):
        cached = self._waitlist_rails.pop(chat_id, None)
        if cached:
            try:
                cached[0].close()
            except Exception as exc:
                logger.debug("Closing a standby login failed (%s)", type(exc).__name__)

    @serialized_operation
    def _merge_waitlists(
        self,
        chat_id,
        now,
        answers,
        *,
        failure=None,
        ids=(),
        member=None,
        others=(),
        background=False,
    ):
        entries = [e for e in self._stored_waitlists(chat_id) if self._waitlist_kept(e, now)]
        notices = []
        for entry in entries:
            if entry.get("id") not in ids or entry.get("state") not in WAITLIST_ACTIVE:
                continue
            if entry["id"] in others and failure is None:
                found = (answers.get(entry["id"]) or {}).get("state")
                if found not in ("waiting", "assigned", "unknown"):
                    # Not in this account's list: another account's, as its tag said. Not
                    # asked about again until the linked account changes.
                    entry["mismatchSeen"] = {"member": member, "at": now.isoformat()}
                    continue
                # Listed under this account: the tag was stale. It is this account's after all.
                entry["account"] = member
                entry.pop("mismatchSeen", None)
            entry["checkedAt"] = now.isoformat()
            if failure in WAITLIST_PAUSED:
                entry["checkFailed"] = True
                entry["checkError"] = failure
                continue
            answer = answers.get(entry["id"]) if failure is None else None
            state = (answer or {}).get("state")
            if state in (None, "unknown"):
                entry["checkFailed"] = True
                continue
            entry["checkFailed"] = False
            entry.pop("checkError", None)
            if state == "waiting":
                entry["state"] = "waiting"
                entry.pop("expiresAt", None)
                entry.pop("seatLabels", None)
            elif state == "assigned":
                newly = entry.get("state") != "assigned"
                entry["state"] = "assigned"
                entry["expiresAt"] = self._deadline(answer.get("deadline"))
                entry["seatLabels"] = list(answer.get("seats") or [])
                if newly:
                    notices.append((entry, self._assigned_text(chat_id, entry), "payment"))
            else:
                entry["state"] = state
                entry["finishedAt"] = now.isoformat()
                notices.append((entry, self._finished_text(entry), "reservation"))
        if background and self.waitlist_should_stop():
            # Stopping: the next run looks again rather than this one writing past its lease.
            return entries
        try:
            self.storage.save_waitlists(chat_id, entries)
        except Exception as exc:
            # The look itself still counts (_waitlist_attempts), so a store that keeps failing
            # does not make Korail be asked any oftener.
            logger.error(
                "Could not save standby records for chat_id=%s (%s)", chat_id, type(exc).__name__
            )
        for entry, text, kind in notices:
            self._publish_waitlist(chat_id, entry, text, kind=kind)
        return entries

    @staticmethod
    def _deadline(deadline):
        if not deadline:
            return None
        try:
            local = datetime.strptime(f"{deadline[0]}{deadline[1][:6]}", "%Y%m%d%H%M%S")
        except (TypeError, ValueError):
            return None
        return as_utc(local, naive_zone=RAIL_TIMEZONE).isoformat()

    def _assigned_text(self, chat_id, entry):
        seats = f" {', '.join(entry['seatLabels'])}" if entry.get("seatLabels") else ""
        expires = self._stamp(entry.get("expiresAt"))
        until = (
            f" 결제 기한은 {format_user_datetime(expires, self.storage.get_user_timezone(chat_id))}로 보여요."
            if expires
            else ""
        )
        return (
            f"{entry['trainInfo']}{seats} 코레일 예약 대기에 좌석이 배정된 것 같아요. "
            f"코레일 앱에서 확인하고 결제해 주세요.{until}"
        )

    @staticmethod
    def _finished_text(entry):
        train = entry["trainInfo"]
        if entry["state"] == "paid":
            return f"{train} 코레일 예약 대기로 배정된 좌석의 승차권이 발권된 것을 확인했어요."
        if entry["state"] == "ticketed":
            return (
                f"{train} 코레일 예약 대기가 목록에서 사라졌고 같은 열차의 승차권이 보여요. "
                "코레일 앱에서 확인해 주세요."
            )
        return (
            f"{train} 코레일 예약 대기가 더 이상 보이지 않아요. "
            "취소됐거나 기한이 지났을 수 있으니 코레일 앱에서 확인해 주세요."
        )

    def _publish_waitlist(self, chat_id, entry, text, *, kind="reservation"):
        try:
            self.telegram.publish(
                chat_id,
                text,
                kind=kind,
                dedupe=f"waitlist:{chat_id}:{entry['id']}:{entry['state']}",
            )
        except Exception as exc:
            logger.error(
                "Could not publish a standby notice for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )

    def _watch_blocked_since_connected(self, chat_id):
        """Whether the account's watch last ended on a block, after it was last connected."""
        reader = getattr(self.storage, "get_search_ended", None)
        ended = reader(chat_id) if reader else None
        if not isinstance(ended, dict) or ended.get("reason") != "blocked":
            return False
        connected = getattr(self.storage, "get_waitlist_connected", None)
        since = connected(chat_id) if connected else None
        # search_ended keeps whole seconds; a block in the same second as the connection
        # counts as after it, since stopping is the safe side of not knowing.
        return since is None or float(ended.get("at") or 0) >= int(since)

    def _pause_blocked(self, chat_id, now):
        """Stop looking at every standby of this owner until the account is connected again."""
        ids = {
            entry["id"]
            for entry in self._stored_waitlists(chat_id)
            if entry.get("state") in WAITLIST_ACTIVE and entry.get("id")
        }
        self._merge_waitlists(chat_id, now, {}, failure="blocked", ids=ids)

    @serialized_operation
    def cancel_waitlist(self, chat_id, payload):
        """
        Withdraw one standby this owner applied for. The reservation number comes from the
        owner's own records; the request only says which of them. Korail is asked where the
        standby stands first: one that was assigned meanwhile gives a seat back, which the
        person must have agreed to (releaseAssigned).
        """
        payload = payload if isinstance(payload, dict) else {}
        wanted = payload.get("id")
        entries = self._stored_waitlists(chat_id)
        entry = next(
            (
                e
                for e in entries
                if isinstance(wanted, str)
                and e.get("id") == wanted
                and e.get("state") in WAITLIST_ACTIVE
            ),
            None,
        )
        if entry is None:
            raise MiniAppError("취소할 예약 대기를 찾지 못했어요. 홈을 새로 불러와 주세요.", 404)
        account = self.storage.get_onboarded_account(chat_id)
        if account is None:
            raise MiniAppError("코레일 계정을 연결한 뒤 예약 대기를 취소해 주세요.", 428)
        other_account = MiniAppError(
            "지금 연결한 코레일 계정의 예약 목록에서 찾지 못한 대기예요. "
            "신청한 계정의 코레일 앱에서 취소해 주세요.",
            409,
        )
        now = utc_now()
        known = self._known_member_tag(chat_id, account)
        if self._seen_missing(entry, known, now):
            raise other_account
        unread = MiniAppError(
            "코레일에서 예약 대기 상태를 확인하지 못해 취소하지 않았어요. 잠시 후 다시 시도해 주세요.",
            502,
        )
        # A login of its own, like the slow look's: a login answered like a block is seen as one
        # (rather than "check your password"), and the seat-map session is left alone.
        rail = self.conversation._rail_service(chat_id)
        try:
            return self._withdraw(chat_id, entry, entries, payload, account, rail, now, unread)
        except _WaitlistBlocked as exc:
            self._pause_blocked(chat_id, now)
            raise MiniAppError(WAITLIST_CANCEL_BLOCKED, 502) from exc
        except _WaitlistLoginRefused as exc:
            raise login_failure(rail, LOGIN_REFUSED, 428) from exc
        finally:
            try:
                rail.close()
            except Exception as exc:
                logger.debug("Closing a withdrawal login failed (%s)", type(exc).__name__)

    def _withdraw(self, chat_id, entry, entries, payload, account, rail, now, unread):
        other_account = MiniAppError(
            "지금 연결한 코레일 계정의 예약 목록에서 찾지 못한 대기예요. "
            "신청한 계정의 코레일 앱에서 취소해 주세요.",
            409,
        )
        try:
            self._log_in_for_waitlist(rail, account)
        except RuntimeError as exc:
            raise MiniAppError(LOGIN_REFUSED, 502) from exc
        member = self._member_tag(chat_id, account.korail_id, rail)
        if member is None:
            raise MiniAppError(
                "코레일 회원 정보를 확인하지 못해 취소하지 않았어요. 잠시 뒤 다시 시도해 주세요.",
                502,
            )
        # A stale or missing tag is given the benefit of the doubt only as far as this account's
        # own list: listed there, it is this account's; not listed, it is left alone.
        foreign = not self._same_account(entry, member)
        try:
            answers = self._waitlist_call(
                rail,
                account,
                lambda: rail.waitlist_states([self._standby(entry, listed_only=foreign)]),
            )
        except (_WaitlistBlocked, _WaitlistLoginRefused):
            raise
        except Exception as exc:
            logger.error(
                "Could not read a standby before withdrawing it for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )
            if _blocked(rail, exc):
                raise _WaitlistBlocked() from exc
            raise unread from exc
        answer = answers.get(entry["id"]) or {}
        state = answer.get("state")
        if foreign:
            self._merge_waitlists(
                chat_id, now, answers, ids={entry["id"]}, member=member, others={entry["id"]}
            )
            if state not in ("waiting", "assigned", "unknown"):
                raise other_account
        if state in (None, "unknown"):
            raise unread
        if state in ("paid", "ticketed", "ended"):
            self._merge_waitlists(chat_id, now, answers, ids={entry["id"]})
            raise MiniAppError(
                "이 예약 대기는 코레일에서 이미 끝났어요. 홈에서 상태를 확인해 주세요.", 409
            )
        if state == "assigned" and payload.get("releaseAssigned") is not True:
            self._merge_waitlists(chat_id, now, answers, ids={entry["id"]})
            raise MiniAppError(
                "그사이 좌석이 배정된 것 같아요. 취소하면 배정된 좌석도 코레일에 돌려줘요. "
                "홈에서 다시 확인해 주세요.",
                409,
            )
        try:
            self._waitlist_call(
                rail,
                account,
                lambda: rail.cancel_waitlist(entry["id"], entry.get("journeyCount")),
            )
        except (_WaitlistBlocked, _WaitlistLoginRefused):
            raise
        except Exception as exc:
            logger.error(
                "Could not withdraw a Korail standby for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )
            if _blocked(rail, exc):
                raise _WaitlistBlocked() from exc
            raise MiniAppError(
                "예약 대기를 취소하지 못했어요. 코레일 앱에서 예약 내역을 확인해 주세요.", 502
            ) from exc
        entries = self._stored_waitlists(chat_id) or entries
        for current in entries:
            if current.get("id") == entry["id"]:
                current["state"] = "cancelled"
                current["finishedAt"] = now.isoformat()
        try:
            self.storage.save_waitlists(chat_id, entries)
        except Exception as exc:
            # Korail took the withdrawal; the next look finds it gone and records it as ended.
            logger.error(
                "Withdrew a standby but could not record it for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )
        return {"cancelled": True, "waitlists": self._waitlists(chat_id, now, entries)}

    @serialized_operation
    def schedule_search(self, chat_id, payload):
        if self._submission(payload).waitlist:
            raise MiniAppError("예약 대기는 선택한 열차에 바로 신청해 주세요.", 422)
        return super().schedule_search(chat_id, payload)

    @staticmethod
    def _stations():
        # Opening an unlinked app performs no railway request. The same
        # snapshot is used by the mobile booking boundary.
        return FALLBACK_STATIONS

    def status(self, chat_id):
        result = super().status(chat_id)
        # With nothing running, how the last watch ended (search_ended, kept a week and cleared
        # when a new one starts): an agent asking must hear "blocked" or "expired", not silence.
        note = None if result["running"] else self.storage.get_search_ended(chat_id)
        if note:
            result["ended"] = {
                "reason": note["reason"],
                "at": datetime.fromtimestamp(note["at"], tz=UTC).isoformat(),
            }
        # Korail standbys are not watches: shown whatever is running.
        result["waitlists"] = self._waitlists(chat_id)
        return result

    def _running(self, chat_id):
        self.reservation.detect_dead_searches()
        record = self.storage.get_running_reservation(chat_id)
        if record and record.is_stale(settings.RUN_ID):
            # Left by the run before a restart, and still recorded: the
            # runtime is bringing it back, retrying for up to about five
            # minutes before it gives up and says so. Hiding it meanwhile
            # showed no search at all, while starting one was refused as
            # already running. "unknown" is the app's own word for a search
            # that is registered but whose state it cannot vouch for.
            running = super()._running(chat_id)
            return running and {
                **running,
                "health": "unknown",
                "lastCheckedAt": None,
                "attemptCount": None,
            }
        if record and not self.reservation._is_running(record.process_id):
            return None
        return super()._running(chat_id)

    @staticmethod
    def _submission(payload):
        conditions = payload.get("conditions", payload)
        if isinstance(conditions, dict):
            operator = conditions.get("operator", payload.get("operator", "korail"))
            if not isinstance(operator, str) or operator not in {"korail", "KORAIL", "KTX"}:
                raise MiniAppError("현재는 코레일만 이용할 수 있어요.", 422)
            preference = conditions.get("seat_preference", "")
            if not isinstance(preference, str):
                raise MiniAppError("좌석 조건을 확인해 주세요.")
            decoded = SeatPreference.decode(preference)
            if decoded.encode() != preference:
                raise MiniAppError("좌석 조건은 A,D:3-5 형식으로 입력해 주세요.")
            if any(
                row is not None and not 1 <= row <= 99 for row in (decoded.row_min, decoded.row_max)
            ) or (decoded.row_min and decoded.row_max and decoded.row_min > decoded.row_max):
                raise MiniAppError("좌석 번호는 1~99 사이에서 작은 번호부터 입력해 주세요.")
            if conditions.get("waitlist"):
                if conditions.get("seat_option") not in {"1", "2"}:
                    raise MiniAppError("코레일 예약 대기는 일반실만 신청할 수 있어요.", 422)
                if preference:
                    raise MiniAppError("코레일 예약 대기에서는 좌석 위치를 지정할 수 없어요.", 422)
        try:
            # Compact separators: the body limit was met on the wire, and the
            # default spacing would push a plan just under it over the line.
            return MobileSubmission.parse(
                json.dumps(conditions, ensure_ascii=False, separators=(",", ":"))
            )
        except (MiniAppDataError, TypeError, ValueError) as exc:
            raise MiniAppError(str(exc)) from exc

    @serialized_operation
    def logout(self, chat_id):
        # Erasing the linked login while a schedule still holds its copy would
        # allow future booking after the user deliberately disconnected it.
        result = self.cancel_search(chat_id)
        if not result["stopped"] and self.storage.get_running_reservation(chat_id):
            raise MiniAppError(
                "검색을 중지하지 못했어요. 검색을 다시 중지한 뒤 계정 연결을 해제해 주세요.", 409
            )
        self.storage.delete_resume_credentials(chat_id)
        self.storage.delete_user_session(chat_id)
        self.storage.delete_app_session_start(chat_id)
        self.storage.delete_onboarded_account(chat_id)
        self._forget_rail(chat_id)
        return {"registered": False}

    @serialized_operation
    def delete_account(self, chat_id, acknowledge_waitlists=False):
        """
        Stop and erase everything this owner has on the railway side: the search
        (its worker killed, as the user's own cancel does), a search booked for
        later, the linked Korail login, favourites, stopped-search records and
        every other record under this owner's id.

        A seat held for payment is refused rather than cancelled here: giving it
        back is a Korail request the user should see answered, and the payment
        watch would otherwise go on polling with a login this erases.
        """
        pending = MiniAppError(
            "결제를 기다리는 예약이 있어요. 결제하거나 예약을 취소한 뒤 탈퇴해 주세요.", 409
        )
        not_stopped = MiniAppError("자리 찾기를 멈추지 못했어요. 잠시 후 다시 시도해 주세요.", 409)
        if self.pending_payments.pending(chat_id):
            raise pending
        active = [
            entry
            for entry in self._stored_waitlists(chat_id)
            if entry.get("state") in WAITLIST_ACTIVE
        ]
        if active and not acknowledge_waitlists:
            # Erasing the record would leave Korail assigning a seat nobody here hears of. Some
            # cannot be withdrawn from the app (another account, no account, a refused login),
            # so the person may instead say they will look after them in the Korail app.
            raise WaitlistsActive(
                f"코레일 예약 대기가 {len(active)}건 남아 있어요. 앱에서 취소할 수 있는 대기는 먼저 "
                "취소하고, 그대로 탈퇴하면 남은 대기는 코레일 앱에서 직접 관리해 주세요.",
                409,
            )
        # The raw key, not get_running_reservation alone: a record this build
        # cannot read comes back as None, the cancel would leave it and its
        # worker alone, and erasing it would leave that worker searching - and
        # booking - for an account that no longer exists. Refused before the
        # cancel, which would otherwise drop a search booked for later.
        record = self.storage.get_running_reservation(chat_id)
        if record is None and self.storage.redis.exists(f"running_reservation:{chat_id}"):
            raise not_stopped
        self.cancel_search(chat_id)
        if self.storage.redis.exists(f"running_reservation:{chat_id}"):
            raise not_stopped
        # The worker is a process of its own and takes no lock: between the
        # check above and its kill it may have had Korail hold a seat. Its
        # payment record, or the mark it leaves the moment the hold comes back,
        # says so; erasing now would leave a hold the user never hears about.
        if self.pending_payments.pending(chat_id):
            raise pending
        if (
            record is not None
            and self.storage.search_mark_pid(chat_id, "held_seat") == record.process_id
        ):
            # Held, but stopped before it could record the booking: only Korail knows.
            raise MiniAppError(
                "코레일에서 잡힌 좌석이 있을 수 있어요. 코레일 앱에서 예약 내역을 확인한 뒤 다시 시도해 주세요.",
                409,
            )
        self._forget_rail(chat_id)
        self.storage.delete_user_data(chat_id)
        return {"deleted": True}

    @serialized_operation
    def cancel_search(self, chat_id):
        result = super().cancel_search(chat_id)
        # A stopped sequential run leaves its seat counter for two hours, and that counter alone
        # makes the unpaid seat it took impossible to give back.
        self.storage.set_current_seat_index(chat_id, None)
        if result["unscheduled"]:
            self.storage.delete_resume_credentials(chat_id)
            self.storage.delete_app_session_start(chat_id)
        return result

    def request_access(self, chat_id):
        return {"requested": False, "approved": True}


class UnavailableGateway:
    """An explicit auth-only runtime for local integration without a railway."""

    def bootstrap(self, chat_id):
        return {
            "version": "auth-only",
            "timeZone": "Asia/Seoul",
            "developer": False,
            "rail": {
                "registered": False,
                "stations": sorted(FALLBACK_STATIONS),
                "majorStations": [],
                "displayName": "코레일",
            },
            "running": None,
            "scheduled": None,
            "pending": [],
            "favourites": [],
            "notifyMinutes": 0,
            "draft": None,
            "paymentUrl": "https://www.letskorail.com/",
        }

    def sync_timezone(self, chat_id, zone):
        pass

    def delete_account(self, chat_id, acknowledge_waitlists=False):
        # Nothing railway-side was ever stored without a booking server.
        return {"deleted": True}

    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise MiniAppError(
                "예약 서버에 연결되지 않았어요. 지금은 앱 계정 기능만 이용할 수 있어요.", 503
            )

        return unavailable
