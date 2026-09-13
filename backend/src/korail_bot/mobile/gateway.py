"""Booking adapter: invited identities use their own encrypted railway login."""

import json
from functools import wraps

from korail_bot.handlers.conversation_handler import ConversationHandler
from korail_bot.models import OnboardedAccount, SeatPreference
from korail_bot.models.station_snapshot import FALLBACK_STATIONS
from korail_bot.services.access_service import AccessDecision, AccessLevel, AccessService
from korail_bot.services.mini_app_gateway import MiniAppError, MiniAppGateway
from korail_bot.services.mini_app_service import MiniAppDataError, MiniAppSubmission


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
    @serialized_operation
    def register(self, chat_id, username, password):
        return super().register(chat_id, username, password)

    @serialized_operation
    def list_trains(self, chat_id, payload):
        return super().list_trains(chat_id, payload)

    @serialized_operation
    def start_search(self, chat_id, payload):
        return super().start_search(chat_id, payload)

    @serialized_operation
    def schedule_search(self, chat_id, payload):
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
            if conditions.get("waitlist") or payload.get("waitlist"):
                raise MiniAppError("예약 대기는 아직 지원하지 않아요.", 422)
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
