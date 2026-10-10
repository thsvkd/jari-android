"""Booking adapter: invited identities use their own encrypted railway login."""

import json
from datetime import datetime, timedelta
from functools import wraps
from time import monotonic

from korail2 import TrainType

from korail_bot.config.settings import settings
from korail_bot.handlers.conversation_handler import ConversationHandler
from korail_bot.mobile.config import MAX_REQUEST_BYTES
from korail_bot.models import OnboardedAccount, PaymentStatus, SeatPreference, SeatTarget
from korail_bot.models.station_snapshot import FALLBACK_STATIONS
from korail_bot.services.access_service import AccessDecision, AccessLevel, AccessService
from korail_bot.services.cancellation_wait_service import train_label
from korail_bot.services.korail_service import KorailService
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
        # chat_id -> (rail, expires_at, korail_id). Only touched under the
        # owner's operation lock, so a plain dict is enough.
        self._rails = {}

    @serialized_operation
    def register(self, chat_id, username, password):
        self._forget_rail(chat_id)
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
            logger.error(
                "Could not list current Korail trains for chat_id=%s (%s)",
                chat_id,
                type(exc).__name__,
            )
            raise MiniAppError(
                "열차 목록을 불러오지 못했어요. 잠시 후 다시 조회해 주세요.", 502
            ) from exc

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
    def listed_trains(self, chat_id, payload):
        """
        The train numbers list_trains last offered for this trip (date and route),
        or [] if the last list was for another trip or there is none. Agents check
        their picks against it; the app picks from the list itself.
        """
        submission = self._submission(payload)
        session = self.storage.get_user_session(chat_id)
        info = session.train_info if session else {}
        trip = (submission.dep_date, submission.src_station, submission.dst_station)
        if (info.get("depDate"), info.get("srcLocate"), info.get("dstLocate")) != trip:
            return []
        return [str(option.get("no")) for option in info.get("trainOptions") or []]

    @serialized_operation
    def seat_cars(self, chat_id, train_key, seat_class, passenger_count):
        train = self._seat_train(chat_id, train_key)
        count = self._passenger_count(passenger_count)
        rail = self._logged_in_rail(chat_id)
        try:
            response = rail.seat_cars(train, seat_class, count)
            return {
                "cars": self.seat_maps.describe_cars(response),
                "layoutReference": bool(rail.seat_layout_is_reference(train, seat_class, count)),
            }
        except ValueError as exc:
            raise MiniAppError(str(exc), 422) from exc
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
            return self._described_inventory(rail, train, car_no, seat_class, count)
        except ValueError as exc:
            raise MiniAppError(str(exc), 422) from exc
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
            cars = rail.seat_cars(train, seat_class, count)
        except ValueError as exc:
            raise MiniAppError(str(exc), 422) from exc
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
            self._forget_rail(chat_id)
            raise MiniAppError("코레일 계정을 연결해 주세요.", 428)
        cached = self._rails.get(chat_id)
        if cached and cached[1] > monotonic() and cached[2] == account.korail_id:
            self._rails[chat_id] = (cached[0], monotonic() + RAIL_SESSION_SECONDS, cached[2])
            return cached[0]
        self._forget_rail(chat_id)
        rail = self.conversation._rail_service(chat_id)
        if not rail.login(account.korail_id, account.korail_pw):
            raise MiniAppError(
                "코레일에 로그인하지 못했어요. 잠시 후 다시 시도하고, 계속되면 계정을 다시 연결해 주세요.",
                428,
            )
        self._rails[chat_id] = (rail, monotonic() + RAIL_SESSION_SECONDS, account.korail_id)
        return rail

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
            raise MiniAppError(
                "코레일에 로그인하지 못했어요. 잠시 후 다시 시도하고, 계속되면 계정을 다시 연결해 주세요.",
                428,
            )
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

        try:
            self.telegram.send_message(
                chat_id,
                f"{selected_trains[0]}편 코레일 예약 대기를 신청했어요. "
                "배정 결과는 코레일 앱이나 홈페이지에서도 확인해 주세요.",
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
    def delete_account(self, chat_id):
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

    def delete_account(self, chat_id):
        # Nothing railway-side was ever stored without a booking server.
        return {"deleted": True}

    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise MiniAppError(
                "예약 서버에 연결되지 않았어요. 지금은 앱 계정 기능만 이용할 수 있어요.", 503
            )

        return unavailable
