"""Booking adapter: invited identities use their own encrypted railway login."""

import json
from datetime import datetime, timedelta
from functools import wraps

from korail2 import TrainType

from korail_bot.config.settings import settings
from korail_bot.handlers.conversation_handler import ConversationHandler
from korail_bot.models import OnboardedAccount, PaymentStatus, SeatPreference, SeatTarget
from korail_bot.models.station_snapshot import FALLBACK_STATIONS
from korail_bot.services.access_service import AccessDecision, AccessLevel, AccessService
from korail_bot.services.mini_app_gateway import MiniAppError, MiniAppGateway
from korail_bot.services.mini_app_service import MiniAppDataError, MiniAppSubmission
from korail_bot.services.seat_map_service import (
    SeatMapExpiredError,
    SeatMapNotFoundError,
    SeatMapService,
)
from korail_bot.utils.logger import get_logger
from korail_bot.utils.timezone import RAIL_TIMEZONE, as_utc, utc_now

logger = get_logger(__name__)


def serialized_operation(method):
    """The scheduler and HTTP gateway must share one mutation boundary."""

    @wraps(method)
    def wrapped(self, chat_id, *args, **kwargs):
        with self.reservation.operation_lock(chat_id):
            return method(self, chat_id, *args, **kwargs)

    return wrapped


class MobileSubmission(MiniAppSubmission):
    @classmethod
    def _validated_station(cls, payload, key):
        value = cls._text(payload, key)
        if value not in FALLBACK_STATIONS:
            raise MiniAppDataError("목록에서 지원하는 역을 선택해 주세요.")
        return value


class InvitedAccess(AccessService):
    def evaluate(self, phone_number, is_developer=False):
        # App registration itself required the operator's one-time invitation.
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
    def __init__(self, *args, seat_map_service=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.seat_maps = seat_map_service or SeatMapService()

    @serialized_operation
    def register(self, chat_id, username, password):
        return super().register(chat_id, username, password)

    @serialized_operation
    def list_trains(self, chat_id, payload):
        session, submission = self._prepared_session(chat_id, payload)
        rail = self._logged_in_rail(chat_id)
        try:
            trains = rail.search_selectable_trains(
                dep_date=submission.dep_date,
                src_locate=submission.src_station,
                dst_locate=submission.dst_station,
                dep_time=f"{submission.dep_time}00",
                max_dep_time=submission.max_dep_time,
                train_type=TrainType.KTX if submission.train_type == "1" else TrainType.ALL,
                passenger_count=submission.passenger_count,
            )
        except Exception as exc:
            logger.error("Could not list current Korail trains for chat_id=%s (%s)", chat_id, type(exc).__name__)
            raise MiniAppError("열차 목록을 불러오지 못했어요. 잠시 후 다시 조회해 주세요.", 502) from exc

        truncated = len(trains) > self.conversation.MAX_TRAIN_OPTIONS
        trains = trains[: self.conversation.MAX_TRAIN_OPTIONS]
        options = []
        for train in trains:
            option = rail.describe_waitlist_train(train)
            option.update(
                {
                    "trainKey": self.seat_maps.remember_train(chat_id, train),
                    "generalAvailable": getattr(train, "general_reservation_code", None) == "11",
                    "specialAvailable": getattr(train, "special_reservation_code", None) == "11",
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
    def seat_cars(self, chat_id, train_key, seat_class, passenger_count):
        train = self._seat_train(chat_id, train_key)
        count = self._passenger_count(passenger_count)
        rail = self._logged_in_rail(chat_id)
        try:
            return {"cars": self.seat_maps.describe_cars(rail.seat_cars(train, seat_class, count))}
        except ValueError as exc:
            raise MiniAppError(str(exc), 422) from exc
        except Exception as exc:
            logger.error("Could not read Korail cars for chat_id=%s (%s)", chat_id, type(exc).__name__)
            raise MiniAppError("호차 정보를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.", 502) from exc

    @serialized_operation
    def seat_inventory(self, chat_id, train_key, car_no, seat_class, passenger_count):
        train = self._seat_train(chat_id, train_key)
        count = self._passenger_count(passenger_count)
        rail = self._logged_in_rail(chat_id)
        try:
            response = rail.seat_inventory(train, car_no, seat_class, count)
            return self.seat_maps.describe_inventory(response)
        except ValueError as exc:
            raise MiniAppError(str(exc), 422) from exc
        except Exception as exc:
            logger.error("Could not read Korail seats for chat_id=%s (%s)", chat_id, type(exc).__name__)
            raise MiniAppError("좌석표를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.", 502) from exc

    @serialized_operation
    def reserve_designated(self, chat_id, payload):
        if self.pending_payments.pending(chat_id):
            raise MiniAppError("결제를 기다리는 예약이 있어요. 먼저 결제하거나 예약을 취소해 주세요.", 409)
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
            current = rail.seat_inventory(train, car_no, seat_class, count)
            hold = rail.reserve_designated(
                train,
                current,
                targets,
                passenger_count=count,
                seat_class=seat_class,
            )
        except ValueError as exc:
            raise MiniAppError(str(exc), 409) from exc
        except Exception as exc:
            logger.error("Could not reserve designated seats for chat_id=%s (%s)", chat_id, type(exc).__name__)
            raise MiniAppError("선택한 좌석을 예약하지 못했어요. 좌석표를 새로 불러와 주세요.", 502) from exc

        reservation_id = rail.reservation_id(hold)
        if not reservation_id:
            self._release_failed_hold(rail, hold, chat_id)
            raise MiniAppError("예약 번호를 확인하지 못했어요. 코레일 예약 목록을 확인해 주세요.", 502)
        expires_at = self._payment_deadline(rail, hold)
        train_info = self._train_info(train)
        labels = [target.label for target in targets]
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
            raise MiniAppError("예약 정보를 안전하게 저장하지 못해 좌석을 다시 돌려보냈어요.", 503) from exc
        try:
            self.telegram.publish(
                chat_id,
                f"{train_info} {', '.join(labels)} 좌석을 예약했어요. 결제 기한 안에 코레일에서 결제해 주세요.",
                kind="payment",
                dedupe=f"designated:{chat_id}:{reservation_id}",
            )
        except Exception as exc:
            logger.error("Could not publish designated-seat notification for chat_id=%s (%s)", chat_id, type(exc).__name__)
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
    def _release_failed_hold(rail, hold, chat_id):
        try:
            rail.release_unpaid_hold(hold)
        except Exception as exc:
            logger.critical(
                "Could not release untracked Korail hold for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )

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
            raise MiniAppError("코레일 계정을 연결해 주세요.", 428)
        rail = self.conversation._rail_service(chat_id)
        if not rail.login(account.korail_id, account.korail_pw):
            raise MiniAppError("코레일에 로그인하지 못했어요. 계정을 다시 연결해 주세요.", 428)
        return rail

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

    @staticmethod
    def _train_info(train):
        name = getattr(train, "train_class_name", None) or "KTX"
        no = str(getattr(train, "train_no", "") or "")
        src = getattr(train, "departure_station_name", None) or "출발역"
        dst = getattr(train, "arrival_station_name", None) or "도착역"
        dep = str(getattr(train, "departure_time", "") or "")
        clock = f"{dep[:2]}:{dep[2:4]}" if len(dep) >= 4 else ""
        return f"{name} {no} {src} → {dst} {clock}".strip()

    @serialized_operation
    def start_search(self, chat_id, payload):
        submission = self._submission(payload)
        if not submission.waitlist:
            return super().start_search(chat_id, payload)

        session, submission = self._prepared_session(chat_id, payload)
        selected_trains = self._selected_trains(payload)
        session.train_info["selectedTrains"] = selected_trains
        self.storage.save_user_session(session)

        if len(selected_trains) != 1:
            raise MiniAppError("예약 대기를 신청할 열차 한 편을 골라 주세요.", 422)
        credentials = session.credentials
        if credentials is None:
            raise MiniAppError("코레일 계정을 다시 연결한 뒤 신청해 주세요.", 428)

        rail = self.conversation._rail_service(chat_id)
        if not rail.login(credentials.korail_id, credentials.korail_pw):
            raise MiniAppError("코레일에 로그인하지 못했어요. 계정을 다시 연결해 주세요.", 428)
        try:
            registered = rail.request_waitlist(
                dep_date=submission.dep_date,
                src_locate=submission.src_station,
                dst_locate=submission.dst_station,
                dep_time=f"{submission.dep_time}00",
                train_no=selected_trains[0],
                passenger_count=submission.passenger_count,
            )
        except ValueError as exc:
            raise MiniAppError(str(exc), 409) from exc
        except Exception as exc:
            logger.error(
                "Could not register Korail standby for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )
            raise MiniAppError(
                "코레일 예약 대기를 신청하지 못했어요. 잠시 후 목록을 새로 조회해 주세요.",
                502,
            ) from exc

        self.telegram.send_message(
            chat_id,
            f"{selected_trains[0]}편 코레일 예약 대기를 신청했어요. "
            "배정 결과는 코레일 앱이나 홈페이지에서도 확인해 주세요.",
        )
        session.reset()
        self.storage.save_user_session(session)
        return {"started": False, "waitlisted": True, "trainNo": registered["train_no"]}

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

    def _running(self, chat_id):
        self.reservation.detect_dead_searches()
        record = self.storage.get_running_reservation(chat_id)
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
            return MobileSubmission.parse(json.dumps(conditions, ensure_ascii=False))
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
        return {"registered": False}

    @serialized_operation
    def cancel_search(self, chat_id):
        result = super().cancel_search(chat_id)
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

    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise MiniAppError(
                "예약 서버에 연결되지 않았어요. 지금은 앱 계정 기능만 이용할 수 있어요.", 503
            )

        return unavailable
