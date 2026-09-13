"""Boundary between the untrusted Telegram Mini App payload and a search."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from korail_bot.models import CancellationWaitPlan, SeatPlanError, SeatPreference
from korail_bot.utils.validators import InputValidator

MAX_WEB_APP_DATA_BYTES = 65536
SCHEMA_VERSION = 1
ACTION = "prepare_search"
START_PARAMETER_PREFIX = "ma1_"

# The static app offers these stations. Profile and menu-button Mini Apps do
# not have KeyboardButton's sendData transport, so their submission returns
# through Telegram's 64-character /start parameter. One base-36 character is
# enough to name every option while station names are recovered and validated
# here, on the trusted side of the boundary.
#
# Keyed by the letter the page puts in the token rather than by railway. "s"
# was SR's page, and SR is gone - but a page cached in someone's Telegram
# still sends "s", and its station indexes mean what that page offered. The
# table is what turns those indexes back into names, so it has to outlive the
# railway. Every name in both lists is a Korail station now.
START_PARAMETER_STATIONS = {
    "k": (
        "서울",
        "용산",
        "청량리",
        "광명",
        "천안아산",
        "오송",
        "대전",
        "동대구",
        "부산",
        "울산(통도사)",
        "포항",
        "익산",
        "전주",
        "광주송정",
        "목포",
        "여수EXPO",
        "강릉",
    ),
    "s": (
        "수서",
        "동탄",
        "평택지제",
        "천안아산",
        "오송",
        "대전",
        "동대구",
        "부산",
        "울산(통도사)",
        "포항",
        "광주송정",
        "목포",
        "익산",
        "전주",
        "여수EXPO",
        "창원중앙",
        "진주",
        "경주",
    ),
}
_START_PARAMETER = re.compile(
    r"^ma1_([ks])(\d{8})([0-9a-z])([0-9a-z])"
    r"(\d{4})(\d{4})([12])([1-4])([1-9])([12])$"
)


class MiniAppDataError(ValueError):
    """A Mini App submission that cannot safely become a reservation draft."""


@dataclass(frozen=True)
class MiniAppSubmission:
    """Validated travel preferences submitted by the static Mini App."""

    dep_date: str
    src_station: str
    dst_station: str
    dep_time: str
    max_dep_time: str
    train_type: str
    seat_option: str
    passenger_count: int
    seat_strategy: str
    # Which seats will do, flattened by SeatPreference.encode. Empty means any
    # seat. Defaulted so that a page from before this existed - a cached one,
    # or the fixed-width /start parameter - parses as asking for nothing in
    # particular rather than failing.
    seat_preference: str = ""
    waitlist: bool = False
    seat_classes: tuple[str, ...] = ()
    seat_plan_json: str = ""

    @classmethod
    def parse(cls, raw: object) -> "MiniAppSubmission":
        """Parse and validate client-controlled JSON from ``web_app_data``."""
        if not isinstance(raw, str) or not raw:
            raise MiniAppDataError("예약 정보가 비어 있습니다. 다시 열어 입력해주세요.")
        if len(raw.encode("utf-8")) > MAX_WEB_APP_DATA_BYTES:
            raise MiniAppDataError("예약 정보가 너무 큽니다. 다시 열어 입력해주세요.")

        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise MiniAppDataError("예약 정보를 읽을 수 없습니다. 다시 열어 입력해주세요.") from exc

        if not isinstance(payload, dict):
            raise MiniAppDataError("예약 정보 형식이 올바르지 않습니다.")
        if payload.get("v") != SCHEMA_VERSION or payload.get("action") != ACTION:
            raise MiniAppDataError("지원하지 않는 예약 화면입니다. /start 로 다시 열어주세요.")

        dep_date = cls._validated(payload, "dep_date", InputValidator.validate_date)
        src_station = cls._validated_station(payload, "src_station")
        dst_station = cls._validated_station(payload, "dst_station")
        if src_station == dst_station:
            raise MiniAppDataError("출발역과 도착역은 달라야 합니다.")

        dep_time = cls._validated(payload, "dep_time", InputValidator.validate_time)
        max_dep_time = cls._text(payload, "max_dep_time")
        if max_dep_time != "2400":
            error = InputValidator.validate_time(max_dep_time)
            if error:
                raise MiniAppDataError(error)
            if max_dep_time <= dep_time:
                raise MiniAppDataError("검색 종료 시각은 시작 시각보다 늦어야 합니다.")

        train_type = cls._text(payload, "train_type")
        # An SR page never asked which kind of train, because SR ran one, and
        # a cached one still submits nothing here. Its trains are KTX now, so
        # the empty answer means KTX rather than a dead end.
        if not train_type:
            train_type = "1"
        error = InputValidator.validate_train_type_choice(train_type)
        if error:
            raise MiniAppDataError(error)

        seat_option = cls._validated(
            payload, "seat_option", InputValidator.validate_special_option_choice
        )
        passenger = cls._validated(
            payload, "passenger_count", InputValidator.validate_passenger_count
        )
        seat_strategy = cls._text(payload, "seat_strategy")
        if int(passenger) == 1:
            seat_strategy = "1"
        else:
            error = InputValidator.validate_seat_strategy_choice(seat_strategy)
            if error:
                raise MiniAppDataError(error)

        seat_preference = SeatPreference.decode(cls._text(payload, "seat_preference")).encode()
        waitlist = payload.get("waitlist", False)
        if type(waitlist) is not bool:
            raise MiniAppDataError("예약 대기 신청 여부를 확인해 주세요.")

        raw_classes = payload.get("seat_classes", [])
        if not isinstance(raw_classes, list) or any(
            item not in {"general", "special"} for item in raw_classes
        ):
            raise MiniAppDataError("좌석 등급을 확인해 주세요.")
        seat_classes = tuple(dict.fromkeys(raw_classes))

        seat_plan_json = ""
        raw_plan = payload.get("seat_plan")
        if raw_plan is not None:
            try:
                plan = CancellationWaitPlan.from_payload(raw_plan)
            except SeatPlanError as exc:
                raise MiniAppDataError(str(exc)) from exc
            if plan.passenger_count != int(passenger):
                raise MiniAppDataError("좌석 계획의 승객 수가 검색 인원과 달라요.")
            if waitlist:
                raise MiniAppDataError("취소표 대기와 코레일 예약 대기를 함께 신청할 수 없어요.")
            seat_plan_json = plan.to_json()

        return cls(
            dep_date=dep_date,
            src_station=src_station,
            dst_station=dst_station,
            dep_time=dep_time,
            max_dep_time=max_dep_time,
            train_type=train_type,
            seat_option=seat_option,
            passenger_count=int(passenger),
            seat_strategy=seat_strategy,
            seat_preference=seat_preference,
            waitlist=waitlist,
            seat_classes=seat_classes,
            seat_plan_json=seat_plan_json,
        )

    @classmethod
    def parse_start_parameter(cls, token: object) -> "MiniAppSubmission":
        """Decode a profile/menu launch submission carried by ``/start``.

        Telegram limits start parameters to 64 URL-safe characters. The
        static page uses fixed-width fields and station indexes, then this
        method expands them into the ordinary JSON shape and runs the same
        validators as a KeyboardButton submission.
        """
        if not isinstance(token, str):
            raise MiniAppDataError("예약 화면에서 받은 시작 정보가 올바르지 않습니다.")

        match = _START_PARAMETER.fullmatch(token.strip())
        if not match:
            raise MiniAppDataError("예약 화면에서 받은 시작 정보가 올바르지 않습니다.")

        (
            operator_code,
            dep_date,
            src_code,
            dst_code,
            dep_time,
            max_dep_time,
            train_type,
            seat_option,
            passenger_count,
            seat_strategy,
        ) = match.groups()
        stations = START_PARAMETER_STATIONS[operator_code]
        try:
            src_station = stations[int(src_code, 36)]
            dst_station = stations[int(dst_code, 36)]
        except (IndexError, ValueError) as exc:
            raise MiniAppDataError("예약 화면에서 선택한 역을 읽을 수 없습니다.") from exc

        return cls.parse(
            json.dumps(
                {
                    "v": SCHEMA_VERSION,
                    "action": ACTION,
                    "dep_date": dep_date,
                    "src_station": src_station,
                    "dst_station": dst_station,
                    "dep_time": dep_time,
                    "max_dep_time": max_dep_time,
                    "train_type": train_type,
                    "seat_option": seat_option,
                    "passenger_count": passenger_count,
                    "seat_strategy": seat_strategy,
                },
                ensure_ascii=False,
            )
        )

    @staticmethod
    def _text(payload: dict, key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            return ""
        return str(value).strip()

    @classmethod
    def _validated(cls, payload: dict, key: str, validator: Callable[[str], str | None]) -> str:
        value = cls._text(payload, key)
        error = validator(value)
        if error:
            raise MiniAppDataError(error)
        return value

    @classmethod
    def _validated_station(cls, payload: dict, key: str) -> str:
        value = cls._text(payload, key)
        error = InputValidator.validate_station_name(value)
        if error:
            raise MiniAppDataError(error)
        return value

    def as_train_info(self) -> dict:
        """Convert the submission to the legacy conversation's stored shape."""
        if self.train_type == "1":
            train_type = "TrainType.KTX"
            train_type_display = "KTX 계열만"
        else:
            train_type = "TrainType.ALL"
            train_type_display = "모든 열차 (무궁화호 포함)"

        options = {
            "1": ("ReserveOption.GENERAL_FIRST", "GENERAL_FIRST"),
            "2": ("ReserveOption.GENERAL_ONLY", "GENERAL_ONLY"),
            "3": ("ReserveOption.SPECIAL_FIRST", "SPECIAL_FIRST"),
            "4": ("ReserveOption.SPECIAL_ONLY", "SPECIAL_ONLY"),
        }
        option, option_display = options[self.seat_option]
        strategy = "consecutive" if self.seat_strategy == "1" else "random"
        strategy_display = (
            "1명"
            if self.passenger_count == 1
            else ("연속 좌석" if strategy == "consecutive" else "랜덤 배치")
        )

        return {
            "depDate": self.dep_date,
            "srcLocate": self.src_station,
            "dstLocate": self.dst_station,
            "depTime": f"{self.dep_time}00",
            "maxDepTime": self.max_dep_time,
            "trainType": train_type,
            "trainTypeShow": train_type_display,
            "specialInfo": option,
            "specialInfoShow": option_display,
            "passengerCount": self.passenger_count,
            "seatStrategy": strategy,
            "seatStrategyShow": strategy_display,
            "seatPreference": self.seat_preference,
            "seatClasses": list(self.seat_classes),
            "seatPlan": self.seat_plan_json,
            "waitlist": self.waitlist,
            "selectedTrains": [],
        }
