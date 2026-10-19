"""
The MCP endpoint agents call: stateless Streamable HTTP, one JSON-RPC message per POST.

Each tool calls the same gateway method the app calls, with the conditions the
app would build (src/model.ts buildConditions), under the same user lock and
the same api:/rail: buckets. It authenticates with agent tokens only (oauth.py)
and answers every failure itself: a railway error becomes a tool result the
agent can read, never the app's HTTP remapping.
"""

import datetime
import json
import logging
import re
from zoneinfo import ZoneInfo

from flask import Response, jsonify, request

from korail_bot import __version__
from korail_bot.config.settings import settings
from korail_bot.mobile.identity import AuthError
from korail_bot.mobile.oauth import BOOK, MCP_PATH, PRM_PATH, SCOPES
from korail_bot.models.station_snapshot import FALLBACK_STATIONS
from korail_bot.services.mini_app_gateway import (
    SEARCH_RUNNING,
    SOLD_OUT_CLASS,
    KorailLoginUnavailable,
    MiniAppError,
)
from korail_bot.services.rail_service import duration_words

logger = logging.getLogger(__name__)

PATH = MCP_PATH
VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]
SEOUL = ZoneInfo("Asia/Seoul")
ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})")
MAX_RESULT_BYTES = 100 * 1024
# The app calls a watch quiet this long after its last check "확인 지연" (src/model.ts STALE_AFTER_MS).
STALE_AFTER = datetime.timedelta(seconds=120)
NOT_READ = "조회하지 못했어요(좌석이 없다는 뜻이 아니에요)."
TOO_MANY = "요청이 너무 많아요. 잠시 후 다시 시도해 주세요."
RAIL_TOO_MANY = "요청이 너무 많아요. 코레일에 닿는 도구는 1분에 10번까지 부를 수 있어요. 1분쯤 뒤에 다시 해 주세요."
BOOK_REFUSED = (
    "이 연결은 조회만 할 수 있어요. 예약을 맡기려면 사용자가 "
    "앱의 설정 → 에이전트 연결에서 '예약까지 맡기기'를 켜 주세요."
)
INSTRUCTIONS = (
    "자리났다는 코레일 열차를 찾고 취소표를 감시해 좌석을 잡아 주는 앱이에요. "
    "조회에 실패한 것과 좌석이 없는 것은 다른 결과예요: 실패하면 그대로 알리고 매진이라고 말하지 마세요. "
    "예약은 결제 대기까지만 진행돼요. 결제는 사용자가 결제 기한 안에 앱이나 코레일에서 직접 해야 해요. "
    "실패한 예약을 성공했다고 말하지 마세요. "
    "감시 시작·감시 멈추기·좌석 예약·예약 취소 전에는 사용자에게 확인을 받아 주세요. "
    "확인을 받을 때는 JSON 이 아니라 열차·날짜·구간·좌석·인원을 자연어로 풀어 보여 주세요. "
    "이 네 도구에는 summary 가 필요해요: 사용자가 승인 창에서 읽을 한두 문장이고, 실제 호출과 맞아야 해요. "
    "start_watch 의 trains 에는 find_trains 결과의 trainNo 를 그대로 넣어 주세요. "
    "trainKey 가 만료됐다는 오류가 나면 그때 find_trains 를 다시 불러 주세요."
)
SOLD_OUT_CLASS_NOTE = (
    "이 열차의 이 등급은 지금 매진이라 좌석을 잡을 수 없어요. 빈자리가 나기를 기다리려면 "
    "사용자에게 확인을 받고 start_watch 로 감시를 시작할 수 있어요(이미 감시 중이면 get_status 로 확인해요)."
)
# Korail sells about sixty days ahead (production answered 60 days out and failed from 62); a
# railway failure on a date past that may just be that.
SALE_HORIZON = datetime.timedelta(days=60)
NOT_ON_SALE_NOTE = (
    "이 날짜는 코레일이 아직 승차권을 팔지 않는 먼 날짜일 수 있어요(보통 두 달 앞까지만 열려요)."
)
HEALTH_NOTES = {
    "healthy": "감시 중이에요. 코레일을 계속 조회하고 있어요.",
    "running": "감시 중이에요. 코레일을 계속 조회하고 있어요.",
    "error": "최근 조회가 계속 실패하고 있어요. 좌석이 없다는 뜻이 아니에요.",
    "stale": "최근 조회가 계속 실패하고 있어요. 좌석이 없다는 뜻이 아니에요.",
    "unavailable": "감시가 멈춰 있어요. 사용자에게 앱에서 확인해 달라고 알려 주세요.",
    "unknown": "감시 상태를 아직 확인하지 못했어요. 잠시 뒤 다시 확인해 주세요.",
    "late": "감시는 돌고 있지만 한동안 코레일 조회 결과가 없어요. 좌석이 없다는 뜻이 아니에요. "
    "사용자에게 앱에서 확인해 달라고 알려 주세요.",
}
# How the last watch ended (status "ended", from search_ended), said only when nothing runs.
# A watch that gave up has not found that there are no seats, and must not be read as sold out.
ENDED_NOTES = {
    "blocked": "마지막 감시는 코레일이 접속을 막은 것 같아 멈췄어요. 좌석이 없다는 뜻이 아니에요. "
    "사용자에게 코레일 앱에서 로그인되는지 확인해 달라고 알려 주세요. 다시 감시를 시작하기 전에 꼭 확인해야 해요.",
    "unavailable": "마지막 감시는 코레일 응답이 {stop}째 없어 멈췄어요. 좌석이 없다는 뜻이 아니에요. "
    "코레일이 점검 중이거나 불안정할 수 있어요.",
    "expired": "마지막 감시는 {limit} 동안 좌석을 잡지 못해 시간 상한으로 멈췄어요. 더 기다리려면 "
    "사용자에게 확인을 받고 감시를 다시 시작해야 해요.",
    "error": "마지막 감시는 오류로 멈췄어요. 좌석이 없다는 뜻이 아니에요. 사용자에게 앱에서 확인해 달라고 알려 주세요.",
}


def _ended_note(reason):
    note = ENDED_NOTES.get(reason)
    return note and note.format(
        stop=duration_words(settings.KORAIL_FAILURE_STOP_SECONDS),
        limit=duration_words(settings.WATCH_MAX_SECONDS),
    )


WATCH_RUNNING_NOTE = (
    "이미 감시 중이에요. 다른 감시를 시작하려면 먼저 멈춰야 하고, "
    "stop_watch 는 사용자에게 확인을 받은 뒤에만 불러 주세요. 지금 감시는 get_status 로 볼 수 있어요."
)
# What the agent cannot tell when a call that changes things did not answer, by tool.
UNSURE_OUTCOME = {
    "start_watch": "감시가 시작됐을 수도 있으니 get_status 로 확인해 주세요.",
    "stop_watch": "감시가 멈췄는지 get_status 로 확인해 주세요.",
    "reserve_seat": "예약이 됐을 수도 있으니 get_status 로 확인해 주세요.",
    "cancel_reservation": "취소됐는지 get_status 로 확인해 주세요.",
}
SEARCH_RUNNING_NOTE = (
    "이미 감시 중이라 열차를 새로 찾을 수 없어요. 감시 중인 열차는 get_status 로 볼 수 있어요. "
    "새로 찾으려면 감시를 멈춰야 하고, stop_watch 는 사용자에게 확인을 받은 뒤에만 불러 주세요."
)
SEAT_CLASS_NOTES = {
    "GENERAL_FIRST": "일반실 우선(없으면 특실)",
    "GENERAL_ONLY": "일반실만",
    "SPECIAL_FIRST": "특실 우선(없으면 일반실)",
    "SPECIAL_ONLY": "특실만",
}
STRATEGY_NOTES = {"consecutive": "연속 좌석", "random": "따로 앉아도 됨"}

TIME = r"([01][0-9]|2[0-3]):[0-5][0-9]"
HHMM = "HH:MM 형식이에요(예: 07:30)"
PASSENGERS = {"type": "integer", "minimum": 1, "maximum": 9, "description": "승객 수, 기본 1"}
SEAT_CLASS = {"type": "string", "enum": ["general", "special"]}


def _summary(example, parts="무엇을 하는지"):
    return {
        "type": "string",
        "minLength": 5,
        "maxLength": 300,
        "description": (
            "사용자가 승인 창에서 읽을 한두 문장이에요. JSON 이 아니라 자연어로, 지금 하려는 일을 "
            f"사실대로 적어요. 꼭 넣을 것: {parts}(‘내일’ 같은 말 대신 날짜로). 예: {example}"
        ),
    }


TRIP = {
    "date": {
        "type": "string",
        "pattern": r"[0-9]{4}-[0-9]{2}-[0-9]{2}",
        "description": "YYYY-MM-DD 형식의 출발 날짜예요",
    },
    "from": {
        "type": "string",
        "enum": sorted(FALLBACK_STATIONS),
        "description": "출발역 이름이에요(예: 서울)",
    },
    "to": {
        "type": "string",
        "enum": sorted(FALLBACK_STATIONS),
        "description": "도착역 이름이에요(예: 부산)",
    },
    "earliest": {
        "type": "string",
        "pattern": TIME,
        "description": f"가장 이른 출발 시각이에요. {HHMM}. 기본 00:00",
    },
    "latest": {
        "type": "string",
        "pattern": f"{TIME}|24:00",
        "description": f"가장 늦은 출발 시각이에요. {HHMM}. 생략하거나 24:00 이면 제한 없음",
    },
    "passengers": PASSENGERS,
    "seat": {"type": "string", "enum": ["general", "special", "any"], "description": "기본 any"},
    "train_type": {"type": "string", "enum": ["ktx", "all"], "description": "기본 ktx(KTX 계열)"},
}


def _schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _tool(name, title, description, scope, schema, rail, confirm=False, **hints):
    tool = {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": schema,
        "annotations": {"title": title, "openWorldHint": rail, **hints},
    }
    if confirm:
        # A hint some clients honour; the scope check is what actually guards.
        tool["_meta"] = {"anthropic/requiresUserInteraction": True}
    return tool, scope, rail


_READ = {"readOnlyHint": True}
TOOLS = {
    spec[0]["name"]: spec
    for spec in [
        _tool(
            "find_trains",
            "열차 찾기",
            "날짜·구간·시간대로 코레일 열차와 남은 좌석 여부를 찾아요. "
            "trainKey 는 get_seat_map·reserve_seat 에, trainNo 는 start_watch 에 써요. "
            "start_watch 에는 같은 date·from·to·earliest·latest·train_type 을 그대로 넣어야 해요.",
            SCOPES[0],
            _schema(TRIP, ("date", "from", "to")),
            True,
            **_READ,
        ),
        _tool(
            "get_seat_map",
            "좌석표 보기",
            "find_trains 의 열차 하나에서 호차별로 예약할 수 있는 좌석을 보여 줘요.",
            SCOPES[0],
            _schema(
                {
                    "trainKey": {"type": "string", "minLength": 1, "maxLength": 200},
                    "seatClass": SEAT_CLASS,
                    "passengers": PASSENGERS,
                },
                ("trainKey", "seatClass"),
            ),
            True,
            **_READ,
        ),
        _tool(
            "get_status",
            "상태 보기",
            "감시 중인 찾기, 시작 예정인 찾기, 결제를 기다리는 예약(결제 기한·결제 주소)을 보여 줘요.",
            SCOPES[0],
            _schema({}),
            False,
            **_READ,
        ),
        _tool(
            "list_favourites",
            "즐겨찾기 보기",
            "사용자가 앱에 저장한 즐겨찾기 구간을 보여 줘요.",
            SCOPES[0],
            _schema({}),
            False,
            **_READ,
        ),
        _tool(
            "start_watch",
            "취소표 감시 시작",
            "고른 열차의 취소표를 감시해요. 좌석을 찾으면 자동으로 예약(결제 대기)까지 진행하고, "
            "결제는 사용자가 직접 해요. start_at 을 주면 그 시각에 감시를 시작해요. "
            "waitlist 는 열차 한 편에 코레일 예약 대기를 바로 신청해요.",
            BOOK,
            _schema(
                {
                    "summary": _summary(
                        "10월 19일 서울→부산 KTX 013·015 취소표를 감시하고, 좌석이 나면 자동으로 "
                        "잡아요(결제는 직접)",
                        "날짜(월·일), 출발역→도착역, 열차 번호, 특실이면 특실(seat 가 any 면 "
                        "'일반실 우선(없으면 특실)'), start_at 을 주면 시작 시각에 '부터'를 붙여"
                        "(예: 10월 17일 07:00부터)",
                    ),
                    **TRIP,
                    "trains": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 30,
                        "items": {
                            "type": "string",
                            "pattern": r"[0-9]{1,5}",
                            "description": "find_trains 결과의 trainNo(숫자 1~5자리)예요",
                        },
                        "description": "find_trains 결과의 trainNo 를 그대로 넣어요",
                    },
                    "waitlist": {
                        "type": "boolean",
                        "description": "기본 false. 예약 대기는 일반실로 신청돼요",
                    },
                    "start_at": {
                        "type": "string",
                        "maxLength": 64,
                        "description": "ISO 8601, 시간대 포함(예: 2026-12-01T07:00:00+09:00)",
                    },
                },
                ("summary", "date", "from", "to", "trains"),
            ),
            True,
            confirm=True,
            destructiveHint=False,
            idempotentHint=False,
        ),
        _tool(
            "stop_watch",
            "감시 멈추기",
            "진행 중이거나 시작 예정인 취소표 감시를 멈춰요.",
            BOOK,
            _schema(
                {
                    "summary": _summary(
                        "진행 중인 서울→부산 10월 19일 취소표 감시를 멈춰요",
                        "멈추는 감시의 날짜(월·일)와 출발역→도착역",
                    )
                },
                ("summary",),
            ),
            False,
            confirm=True,
            destructiveHint=False,
            idempotentHint=True,
        ),
        _tool(
            "reserve_seat",
            "좌석 예약",
            "get_seat_map 에서 고른 좌석을 바로 예약(결제 대기)해요. 승객 수만큼 좌석을 골라요. "
            "결제는 사용자가 결제 기한 안에 직접 해요.",
            BOOK,
            _schema(
                {
                    "summary": _summary(
                        "10월 19일 서울→부산 KTX 049 07:00, 특실 4호차 1C 창가 1석을 예약해요(결제 대기)",
                        "날짜(월·일), 출발역→도착역, 열차 번호, 특실이면 특실, 호차와 좌석 번호",
                    ),
                    "trainKey": {"type": "string", "minLength": 1, "maxLength": 200},
                    "seatClass": SEAT_CLASS,
                    "passengers": PASSENGERS,
                    "carNo": {"type": "integer", "minimum": 1, "maximum": 99},
                    "seats": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 9,
                        "items": _schema(
                            {
                                "carNo": {"type": "integer", "minimum": 1, "maximum": 99},
                                "seatNo": {"type": "string", "minLength": 1, "maxLength": 40},
                                "label": {"type": "string", "minLength": 1, "maxLength": 40},
                            },
                            ("carNo", "seatNo", "label"),
                        ),
                    },
                },
                ("summary", "trainKey", "seatClass", "carNo", "seats"),
            ),
            True,
            confirm=True,
            destructiveHint=True,
        ),
        _tool(
            "cancel_reservation",
            "결제 대기 예약 취소",
            "결제를 기다리는 예약을 취소해요. 잡은 좌석은 돌려보내요.",
            BOOK,
            _schema(
                {
                    "summary": _summary(
                        "결제를 기다리는 KTX 049 특실 4호차 1C 예약을 취소하고 좌석을 돌려보내요",
                        "취소하는 예약마다 열차 번호(결제를 기다리는 예약은 모두 함께 취소돼요)",
                    )
                },
                ("summary",),
            ),
            True,
            confirm=True,
            destructiveHint=True,
        ),
    ]
}


class Invalid(ValueError):
    pass


KINDS = {
    "object": "객체예요",
    "array": "목록이에요",
    "string": "문자열이에요",
    "integer": "정수예요",
    "boolean": "true 나 false 예요",
}


class Disconnected(Exception):
    """The connection ended while the call waited for the user's lock."""


def check(schema, value, name="arguments"):
    """The subset of JSON Schema the tool schemas use, checked on the server."""
    kind = schema["type"]
    if not {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }[kind]:
        raise Invalid(f"{name} 값은 {KINDS[kind]}.")
    # "{field} 값은 …" every time: a Korean particle after an English field name is a guess.
    described = f"{name} 값은 {schema.get('description', '')}".rstrip()
    if "enum" in schema and value not in schema["enum"]:
        choices = schema["enum"]
        if len(choices) <= 10:
            raise Invalid(f"{name} 값은 {', '.join(choices)} 중 하나예요.")
        raise Invalid(described)
    if kind == "integer" and not schema["minimum"] <= value <= schema["maximum"]:
        raise Invalid(f"{name} 값은 {schema['minimum']}~{schema['maximum']} 사이예요.")
    if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        raise Invalid(described if "description" in schema else f"{name} 값을 확인해 주세요.")
    if kind == "string" and not (
        schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 200)
    ):
        raise Invalid(
            f"{name} 값은 {schema.get('minLength', 0)}~{schema.get('maxLength', 200)}자예요."
        )
    if kind == "array":
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise Invalid(f"{name} 값은 {schema['minItems']}~{schema['maxItems']}개예요.")
        for item in value:
            check(schema["items"], item, name)
    if kind == "object":
        unknown = set(value) - set(schema["properties"])
        missing = set(schema["required"]) - set(value)
        if "summary" in missing and not unknown:
            raise Invalid(
                f"summary 값이 필요해요. {schema['properties']['summary']['description']}"
            )
        if unknown or missing:
            raise Invalid(f"{name}: 모르는 값 {sorted(unknown)}, 빠진 값 {sorted(missing)}")
        for key, item in value.items():
            check(schema["properties"][key], item, key)


def conditions(arguments):
    """What the app's buildConditions sends for the same trip."""
    try:
        date = datetime.date.fromisoformat(arguments["date"])
    except ValueError as exc:
        raise Invalid("date 값은 실제 있는 날짜(YYYY-MM-DD)예요.") from exc
    if date < _today():
        raise Invalid("date 값은 오늘이거나 그 뒤 날짜예요.")
    latest = arguments.get("latest", "24:00")
    if latest != "24:00" and latest <= arguments.get("earliest", "00:00"):
        raise Invalid("latest 값은 earliest 보다 늦은 시각이에요.")
    seat = arguments.get("seat", "any")
    result = {
        "v": 1,
        "action": "prepare_search",
        "dep_date": date.strftime("%Y%m%d"),
        "src_station": arguments["from"],
        "dst_station": arguments["to"],
        "dep_time": arguments.get("earliest", "00:00").replace(":", ""),
        "max_dep_time": arguments["latest"].replace(":", "") if "latest" in arguments else "2400",
        "train_type": "2" if arguments.get("train_type") == "all" else "1",
        "seat_option": {"general": "2", "special": "4", "any": "1"}[seat],
        "passenger_count": arguments.get("passengers", 1),
        "seat_strategy": "1",
        "seat_preference": "",
        "waitlist": arguments.get("waitlist", False),
    }
    if seat != "any":
        result["seat_classes"] = [seat]
    return result


def _clock(raw):
    raw = str(raw or "")
    return f"{raw[:2]}:{raw[2:4]}" if len(raw) >= 4 else raw


def _trains(result):
    notes = [
        "soldOut 은 일반실·특실이 모두 매진일 때만 true 예요. "
        "등급별 남은 자리는 generalAvailable·specialAvailable 로 봐요."
    ]
    if result.get("truncated"):
        notes.append(
            "열차가 많아 일부만 보여요. earliest·latest 로 시간대를 좁혀 다시 찾아 주세요."
        )
    if not result.get("trains"):
        notes.append(
            "이 시간대에 열차가 없어요. 아직 승차권을 팔지 않는 먼 날짜도 이렇게 보일 수 있어요."
        )
    return {
        "notes": notes,
        "trains": [
            {
                "trainKey": train.get("trainKey"),
                "trainNo": train.get("no"),
                "name": train.get("name"),
                "departs": _clock(train.get("dep_time")),
                "arrives": _clock(train.get("arr_time")),
                "generalAvailable": train.get("generalAvailable", False),
                "specialAvailable": train.get("specialAvailable", False),
                "soldOut": train.get("soldout", False),
                "waitlistEligible": train.get("waitlistEligible", False),
            }
            for train in result.get("trains", [])
        ],
        "truncated": result.get("truncated", False),
    }


def _free_seats(inventory):
    """Free seats, each with what an agent asks about: window or aisle, a 4-seat family set."""
    seats = inventory.get("seats", [])
    # The outermost columns of the car are the windows: A·D in a 2+2 car, A·C in a 1+2 one.
    # ponytail: numeric-label cars get letters synthesized by the seat map; a layout-aware
    # answer would read rows the way src/seat-map.ts seatColumnSets does. Direction codes
    # are left out until their meaning (forward/backward) is known for certain.
    columns = sorted({seat.get("column") for seat in seats if seat.get("column")})
    windows = {columns[0], columns[-1]} if len(columns) > 1 else set()
    free = []
    for seat in seats:
        if not seat.get("salePossible"):
            continue
        entry = {"seatNo": seat["seatNo"], "label": seat["label"]}
        if seat.get("column") in columns and len(columns) > 1:
            entry["position"] = "창가" if seat["column"] in windows else "통로"
        if seat.get("familyLabel"):
            entry["note"] = seat["familyLabel"]
        free.append(entry)
    return free


def _seat_map(result, arguments):
    cars = [
        {"carNo": inventory["carNo"], "freeSeats": _free_seats(inventory)}
        for inventory in result.get("inventories", [])
    ]
    cars = [car for car in cars if car["freeSeats"]]
    failed = result.get("failedCars", [])
    notes = []
    if result.get("layoutReference") or any(
        inventory.get("layoutReference") for inventory in result.get("inventories", [])
    ):
        # Korail listed no car in this class, so the server drew the same train number on a
        # nearby date. Its free seats are that day's: offering them as free here sent an agent
        # to reserve seats that were not on sale.
        cars = []
        notes.append(
            "이 열차의 이 등급은 지금 매진이에요. 좌석표는 가까운 날짜의 같은 열차 편성을 "
            "참고로 읽은 것이라 예약할 수 있는 좌석이 없어요. 빈자리가 나기를 기다리려면 "
            "사용자에게 확인을 받고 start_watch 로 감시를 시작할 수 있어요(이미 감시 중이면 "
            "get_status 로 확인해요)."
        )
    elif failed:
        notes.append("일부 호차는 조회하지 못했어요. 그 호차에 좌석이 없다는 뜻은 아니에요.")
    elif not cars:
        notes.append("예약 가능한 좌석이 없어요.")
    summary = {
        "trainKey": arguments["trainKey"],
        "seatClass": arguments["seatClass"],
        "cars": cars,
        "failedCars": failed,
        "notes": notes,
    }
    # The whole formation can run to megabytes; a few seats per car is enough to choose from.
    for keep in (None, 40, 10, 3):
        if keep is not None:
            summary["cars"] = [{**car, "freeSeats": car["freeSeats"][:keep]} for car in cars]
            summary["truncated"] = True
            summary["notes"] = [*notes, f"좌석이 많아 호차마다 {keep}석까지만 보여요."]
        if len(_text(summary).encode()) <= MAX_RESULT_BYTES:
            break
    return summary


def _day(raw):
    """20261019 as 10월 19일; what is not a date stays as it came."""
    raw = str(raw or "")
    if len(raw) == 8 and raw.isdigit():
        return f"{int(raw[4:6])}월 {int(raw[6:8])}일"
    return raw


def _held(item):
    """One pending reservation as a sentence: the train, the class and the seats."""
    klass = {"general": "일반실", "special": "특실"}.get(item.get("seatClass"), "")
    seats = ", ".join(item.get("seatLabels") or [])
    return " ".join(part for part in (item.get("trainInfo"), klass, seats) if part)


def _named(no, labels):
    """
    A watched train as the note says it. The app's label is the times and the name
    ("07:27→10:03 KTX"), so the number goes in front unless the label already has it.
    """
    no = str(no)
    label = str(labels.get(no) or "")
    if re.search(rf"(?<![\d:]){re.escape(no)}(?![\d:])", label):
        return label
    return f"{no} {label}".strip()


def _watching(search):
    """A running or scheduled search as a sentence: the day, the stretch and the trains."""
    labels = search.get("trainLabels") or {}
    trains = [_named(no, labels) for no in search.get("selectedTrains") or []]
    shown = ", ".join(trains[:5]) + (f" 외 {len(trains) - 5}편" if len(trains) > 5 else "")
    begins, ends = str(search.get("depTime") or "")[:4], str(search.get("maxDepTime") or "")[:4]
    start = _clock(begins) if begins and begins != "0000" else ""
    end = _clock(ends) if ends and ends != "2400" else ""
    window = (
        f"{start}~{end} 출발"
        if start and end
        else f"{start} 이후 출발"
        if start
        else f"{end} 이전 출발"
        if end
        else ""
    )
    src, dst = search.get("srcLocate"), search.get("dstLocate")
    where = f"{src}→{dst}" if src and dst else ""
    parts = (
        f"{_day(search.get('depDate'))} {where}".strip(),
        window,
        f"열차 {shown}" if shown else "",
    )
    return " · ".join(part for part in parts if part)


def _seat_terms(search):
    """The class, head count and seating of a watch, as get_status and start_watch say them."""
    seat_class, count = search.get("specialInfoShow"), search.get("passengerCount")
    strategy = search.get("seatStrategy")
    # A value the status left out is left out of the sentence too, never read as "None".
    parts = [
        SEAT_CLASS_NOTES.get(seat_class, seat_class) if seat_class else None,
        f"{count}명" if count else None,
        STRATEGY_NOTES.get(strategy, strategy) if strategy else None,
    ]
    return ", ".join(part for part in parts if part)


def _reserved(result):
    held = [text for item in result.get("pending") or [] if (text := _held(item))]
    return {
        **result,
        "notes": [
            *([f"잡은 좌석: {'; '.join(held)}"] if held else []),
            "좌석을 잡았어요(결제 대기). 결제는 직접 해 주세요: 결제 기한 안에 앱이나 "
            f"코레일({result.get('paymentUrl') or settings.KORAIL_PAYMENT_URL})에서 결제하지 않으면 "
            "예약이 취소돼요.",
        ],
    }


def _now():
    return datetime.datetime.now(datetime.UTC)


def _today():
    """Today on the railway's clock: the date a ticket can no longer be bought for is before it."""
    return _now().astimezone(SEOUL).date()


def _age(raw):
    """How old an ISO time is, or None when it is missing or unreadable."""
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.UTC)
    return _now() - stamp


def _quiet(running, health):
    """
    Whether the app would draw this search as "확인 지연" (src/model.ts deriveRadarView).

    Any state but a failing one with a last check older than STALE_AFTER; with no check at all, a
    healthy search whose first pass has been under way that long.
    """
    if health in ("error", "unavailable", "stale"):
        return False
    checked = _age(running.get("lastCheckedAt"))
    if checked is not None:
        return checked > STALE_AFTER
    started = _age(running.get("startedAt"))
    return health in ("healthy", "running") and started is not None and started > STALE_AFTER


def _started(result):
    if result.get("waitlisted"):
        return {
            **result,
            "notes": [
                "코레일 예약 대기를 신청했어요. 감시는 시작하지 않았고 get_status 에도 나오지 않아요. "
                "배정 결과는 사용자가 앱이나 코레일에서 확인해야 해요."
            ],
        }
    running = result.get("running")
    if isinstance(running, dict):
        # The same words get_status uses, not the app's fields: what is watched, then for whom.
        said = _watching(running)
        notes = []
        if said and result.get("started"):
            notes.append(f"감시를 시작했어요: {said}")
            if terms := _seat_terms(running):
                notes.append(f"좌석 조건: {terms}")
            if settings.WATCH_MAX_SECONDS > 0:
                notes.append(
                    f"감시는 시작하고 {duration_words(settings.WATCH_MAX_SECONDS)}이 지나도 "
                    "좌석을 잡지 못하면 멈춰요."
                )
        return {
            **result,
            "running": _plain_watch(running),
            **({"notes": notes} if notes else {}),
        }
    return result


def _stopped(result, was_watching):
    """Whether it stopped, and, when not, whether there was anything to stop."""
    notes = []
    if result.get("stopped"):
        notes.append("진행 중이던 감시를 멈췄어요.")
    if result.get("unscheduled"):
        notes.append("시작 예정이던 감시를 취소했어요.")
    if not notes and was_watching:
        notes.append("감시를 멈추지 못했어요. get_status 로 아직 돌고 있는지 확인해 주세요.")
    if not notes:
        notes.append("멈출 감시가 없어요. 진행 중이거나 시작 예정인 감시가 없었어요.")
    return {**result, "notes": notes}


def _cancelled(result, before=()):
    left = result.get("pending") or []
    kept = {item.get("reservationId") for item in left if item.get("reservationId")}
    # A held reservation with no id cannot be told from a kept one: with some left, leave it out.
    gone = [
        _held(item)
        for item in before
        if (item.get("reservationId") not in kept) and (item.get("reservationId") or not left)
    ]
    gone = [text for text in gone if text]
    said = [f"취소한 예약: {'; '.join(gone)}"] if gone else []
    if not left:
        return {**result, "notes": [*said, "결제 대기 예약을 취소하고 좌석을 돌려보냈어요."]}
    return {
        **result,
        "pending": [{**item, "paymentUrl": settings.KORAIL_PAYMENT_URL} for item in left],
        "notes": [
            *said,
            f"일부만 취소했어요. 결제를 기다리는 예약이 {len(left)}건 남아 있어요. "
            "남은 예약은 결제 기한 안에 결제하지 않으면 취소돼요.",
        ],
    }


def _plain_watch(search):
    """
    A watch in start_watch's own words (date·from·to·earliest·latest·trains·passengers) instead
    of the app's field names and codes ("20261019", "0700", "GENERAL_FIRST").
    """
    if not isinstance(search, dict):
        return search
    day = _trip_day(search.get("depDate"))
    begins, ends = str(search.get("depTime") or "")[:4], str(search.get("maxDepTime") or "")[:4]
    seat_class, strategy = search.get("specialInfoShow"), search.get("seatStrategy")
    plain = {
        "date": day.isoformat() if day else search.get("depDate"),
        "from": search.get("srcLocate"),
        "to": search.get("dstLocate"),
        "earliest": _clock(begins) if begins.isdigit() else None,
        "latest": _clock(ends) if ends.isdigit() else None,
        "trainType": search.get("trainTypeShow"),
        "trains": search.get("selectedTrains"),
        "trainLabels": search.get("trainLabels"),
        "seat": SEAT_CLASS_NOTES.get(seat_class, seat_class),
        "passengers": search.get("passengerCount"),
        "seatStrategy": STRATEGY_NOTES.get(strategy, strategy),
        "seatPreference": search.get("seatPreference"),
    }
    rest = {key: value for key, value in search.items() if key not in _APP_WATCH_FIELDS}
    return {**{key: value for key, value in plain.items() if value is not None}, **rest}


_APP_WATCH_FIELDS = {
    "depDate",
    "srcLocate",
    "dstLocate",
    "depTime",
    "maxDepTime",
    "trainTypeShow",
    "selectedTrains",
    "trainLabels",
    "specialInfoShow",
    "passengerCount",
    "seatStrategy",
    "seatPreference",
}


def _status(result):
    """What the app's status says, with where to pay and a Korean reading of the codes."""
    pending = [
        {**item, "paymentUrl": settings.KORAIL_PAYMENT_URL} for item in result.get("pending") or []
    ]
    notes = []
    running = result.get("running")
    if running:
        health = running.get("health")
        if _quiet(running, health):
            # The server calls a search healthy while its heartbeat exists; the app, which draws
            # what the user reads, calls it "확인 지연" once the last check is this old.
            health = "late"
        notes.append(HEALTH_NOTES.get(health, HEALTH_NOTES["unknown"]))
        if running_text := _watching(running):
            notes.append(f"감시 내용: {running_text}")
        if terms := _seat_terms(running):
            notes.append(f"좌석 조건: {terms}")
    if result.get("scheduled"):
        notes.append("정한 시각에 감시를 시작해요.")
        later = result["scheduled"].get("search") if isinstance(result["scheduled"], dict) else None
        if scheduled_text := _watching(later or {}):
            notes.append(f"예약된 감시 내용: {scheduled_text}")
    if pending:
        notes.append(
            f"결제를 기다리는 예약이 {len(pending)}건 있어요. "
            "결제 기한 안에 사용자가 직접 결제해야 해요."
        )
        held, wanted = (running or {}).get("seatsHeld"), (running or {}).get("seatsWanted")
        if isinstance(held, int) and isinstance(wanted, int) and wanted > held > 0:
            notes.append(f"{wanted}석 중 {held}석을 잡았고 나머지는 감시가 찾고 있어요.")
    ended = result.get("ended")
    if not running and not result.get("scheduled") and isinstance(ended, dict):
        if said := _ended_note(ended.get("reason")):
            notes.append(said)
    scheduled = result.get("scheduled")
    if isinstance(scheduled, dict) and "search" in scheduled:
        scheduled = {**scheduled, "search": _plain_watch(scheduled["search"])}
    return {
        **result,
        "running": _plain_watch(running),
        "scheduled": scheduled,
        "pending": pending,
        "notes": notes or ["진행 중인 감시도, 결제를 기다리는 예약도 없어요."],
    }


def _korean_times(value):
    """Every timestamp a tool returns, in Asia/Seoul (+09:00): what the user reads off a clock."""
    if isinstance(value, dict):
        return {key: _korean_times(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_korean_times(item) for item in value]
    if isinstance(value, str) and ISO_TIME.fullmatch(value):
        try:
            return datetime.datetime.fromisoformat(value).astimezone(SEOUL).isoformat()
        except (ValueError, OverflowError):
            # A user's own text (a favourite's name) can look like a time without being one.
            return value
    return value


def _text(value):
    return json.dumps(value, ensure_ascii=False)


def prepare(name, arguments):
    """
    Check the input and build the gateway call, without calling it: input errors must not
    spend the railway budget. The returned call takes (gateway, storage_id) and shapes the
    result. Raises Invalid.
    """
    check(TOOLS[name][0]["inputSchema"], arguments)
    if "summary" in arguments:
        _confirmed(name, arguments)
    if name == "find_trains":
        payload = {"conditions": conditions(arguments)}
        return lambda gateway, user: _trains(gateway.list_trains(user, payload))
    if name == "get_seat_map":
        query = (arguments["trainKey"], arguments["seatClass"], str(arguments.get("passengers", 1)))
        return lambda gateway, user: _seat_map(gateway.seat_inventories(user, *query), arguments)
    if name == "get_status":
        return lambda gateway, user: _status(gateway.status(user))
    if name == "list_favourites":
        return lambda gateway, user: {"favourites": gateway.favourites(user)}
    if name == "start_watch":
        payload = {"conditions": conditions(arguments), "trains": arguments["trains"]}
        if "start_at" not in arguments:
            return lambda gateway, user: _started(gateway.start_search(user, payload))
        try:
            start_at = datetime.datetime.fromisoformat(arguments["start_at"])
        except ValueError as exc:
            raise Invalid("start_at 값을 확인해 주세요. ISO 8601 형식이에요.") from exc
        if start_at.tzinfo is None:
            raise Invalid("start_at 에 시간대를 넣어 주세요(예: +09:00).")
        payload["start_at"] = arguments["start_at"]
        return lambda gateway, user: gateway.schedule_search(user, payload)
    if name == "stop_watch":

        def stop(gateway, user):
            # Stop first: the stop never waits on a read. Only a stop that found nothing asks
            # afterwards whether there had been something.
            result = gateway.cancel_search(user)
            watching = False
            if not (result.get("stopped") or result.get("unscheduled")):
                try:
                    after = gateway.status(user)
                    watching = bool(after.get("running") or after.get("scheduled"))
                except Exception as exc:
                    # The stop ran; only the telling-apart failed. Assume there was a watch.
                    logger.warning("stop_watch could not read the status (%s)", type(exc).__name__)
                    watching = True
            return _stopped(result, watching)

        return stop
    if name == "reserve_seat":
        payload = {
            "trainKey": arguments["trainKey"],
            "seatClass": arguments["seatClass"],
            "passengerCount": arguments.get("passengers", 1),
            "carNo": arguments["carNo"],
            "seats": arguments["seats"],
        }
        return lambda gateway, user: _reserved(gateway.reserve_designated(user, payload))

    def cancel(gateway, user):
        try:
            before = gateway.status(user).get("pending") or []
        except Exception as exc:
            # Naming what was cancelled is a nicety; the cancel must not wait on it.
            logger.warning("cancel_reservation could not read the status (%s)", type(exc).__name__)
            before = []
        return _cancelled(gateway.cancel_pending(user), before)

    return cancel


# What a sentence says that is not a train number: a clock time, a car, a head count (a lone
# digit may stand apart from its unit: "2 명"; "013 석이" stays a train), a seat label ("8 C").
# Also a bare day ("19일", not "13 일반실"), a count of bookings ("2건") and a numbered seat;
# "1인" is a head count but "049인 열차" is the train.
_NOT_A_TRAIN = re.compile(
    r"\d{1,2}:\d{2}|\d+\s*호차|\d+(?:명|석|매(?!진)|시|분|건|개)|(?<!\d)[1-9]\s*인(?![가-힣])"
    r"|(?<!\d)[1-9]\s+(?:명|석|건)|(?<!\d)\d{1,2}\s*일(?!반)|\d+\s*번\s*(?:좌석|자리)"
    r"|\d+\s*번째|(?<!\d)\d{1,2}(?:\s*장(?![가-힣])|자리|좌석)|(?<!\d)\d{1,2}\s+(?:시|분|개)(?![가-힣])"
    r"|\d{4}-\d{2}-\d{2}|(?<!\d)\d{1,2}\s?[A-Z](?![A-Z])"
)
# A head count the sentence states: "1석", "2 명", "1인", "한 명".
_COUNT = re.compile(
    r"(?<!\d)(\d{1,2})(?:\s*(?:명|석|장)|자리|좌석|\s*인(?![가-힣])|\s*매(?!진))"
    r"|(?:어른|성인|대인)\s*(\d{1,2})"
)
# Word numerals take 분 too ("두 분"); a digit before 분 is minutes.
_WORD_COUNT = re.compile(
    r"(?<![가-힣])(한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:명|사람|좌석|자리|분|장)|(혼자)"
)
_WORDS = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9}
_WORDS["열"] = 10
_WORDS["혼자"] = 1
_CAR_MENTION = re.compile(r"(?<!\d)(\d{1,2})\s*호차")
# A station written the way people say it: without the parenthesis, or by its common name.
_STATION_ALIASES = {"여수EXPO": ["여수엑스포"]}


def _said(text):
    return re.sub(r"\s+", " ", text).strip().upper()


def _part(number):
    """A month or a day as written: 3 or 03, but 10 only as 10 ("010/013" is two trains)."""
    return f"0?{number}" if number < 10 else str(number)


def _date_forms(date):
    month, day = _part(date.month), _part(date.day)
    return (
        date.isoformat(),
        rf"(?<!\d){month}\s*월\s*{day}\s*일",
        rf"(?<!\d){month}\s*[/.\-]\s*{day}(?!\d)",
    )


def _date_in(text, date):
    return any(re.search(form, text) for form in _date_forms(date))


def _number_in(text, number, *dates):
    """
    Whether the train number (or the number without its zeros) stands alone in the text.

    The stations, this call's own dates, clock times, cars, head counts and seat labels go
    first: their digits are not the train's ("서울→인천공항T1" is not train 1).
    """
    text = _STATION_NAMES.sub(" ", text)
    dates = [date for date in dates if date]
    # With no date to go by (the key's had none), any "M월 D일" or "M/D" goes.
    for form in (
        [form for date in dates for form in _date_forms(date)]
        if dates
        else [r"\d{1,2}\s*월\s*\d{1,2}\s*일", r"(?<!\d)\d{1,2}\s*[/.]\s*\d{1,2}(?!\d)"]
    ):
        text = re.sub(form, " ", text)
    text = _NOT_A_TRAIN.sub(" ", text)
    return re.search(rf"(?<!\d)0*{int(number)}(?!\d)", text) is not None


def _seat_pattern(label):
    """The seat label standing alone: 8C or "8 C", but not 18C; a bare number needs 번/석."""
    spelled = r"\s*".join(re.escape(part) for part in re.findall(r"\d+|[^\d\s]+", label.upper()))
    tail = r"\s*번" if label.strip().isdigit() else r"(?![0-9A-Z])"
    return rf"(?<![0-9A-Z]){spelled}{tail}"


# "8C·D" is 8C and 8D: the row once, then each letter.
_SHARED_ROW = re.compile(
    r"(?<![0-9A-Z])(\d{1,2})([A-Z])((?:\s*[·,/]\s*[A-Z](?![0-9A-Z]))+)(\s*\(\s*\d+\s*호차\s*\))?"
)
_BRACKETED_SEAT = re.compile(r"(?<![0-9A-Z])\d{1,3}(?:\s?[A-Z]|\s*번)?\s*\(\s*\d+\s*호차\s*\)")


def _one_label_each(text):
    """ "8C·D" as "8C 8D", and "8C·D(2호차)" as "8C(2호차) 8D(2호차)"."""
    return _SHARED_ROW.sub(
        lambda hit: " ".join(
            hit.group(1) + letter + (hit.group(4) or "")
            for letter in [hit.group(2), *re.findall(r"[A-Z]", hit.group(3))]
        ),
        text,
    )


def _seats_missing(text, seats):
    """
    The seats the sentence does not put in their own car: "N호차 label" in that order, or the
    car in brackets right after the seat, "label(N호차)". "label N호차" stays refused: in
    "2호차 8C 3호차 9D" it would read 8C as in car 3.
    """
    text = _one_label_each(text)
    # A bracketed seat belongs to its bracket only, never to the car named before it.
    plain = _BRACKETED_SEAT.sub(" ", text)
    cars = list(_CAR_MENTION.finditer(plain))
    segments = [
        (int(car.group(1)), plain[car.end() : cars[n + 1].start() if n + 1 < len(cars) else None])
        for n, car in enumerate(cars)
    ]
    missing = []
    for seat in seats:
        pattern = _seat_pattern(seat["label"])
        bracketed = rf"{pattern}\s*\(\s*0*{seat['carNo']}\s*호차\s*\)"
        if not re.search(bracketed, text) and not any(
            number == seat["carNo"] and re.search(pattern, segment) for number, segment in segments
        ):
            missing.append(f"{seat['carNo']}호차 {seat['label']}")
    return missing


def _station_forms(name):
    bare = re.sub(r"\(.*?\)", "", name)
    return {
        name.upper(),
        bare.upper(),
        *(alias.upper() for alias in _STATION_ALIASES.get(name, [])),
    }


_STATION_FORMS: dict[str, set[str]] = {}
for _station in FALLBACK_STATIONS:
    for _form in _station_forms(_station):
        if _form:
            _STATION_FORMS.setdefault(_form, set()).add(_station)
# 판교 is two stations, 판교(경기) and 판교(충남): a bare name that fits two names neither.
_STATION_FORMS = {
    form: stations
    for form, stations in _STATION_FORMS.items()
    if len(stations) == 1 or form in {station.upper() for station in stations}
}
# Longest first: 서대전 is a station of its own, not 대전 with a letter in front.
_STATION_NAMES = re.compile(
    "|".join(re.escape(form) for form in sorted(_STATION_FORMS, key=len, reverse=True))
)


def _stations_missing(text, source, target):
    """The stretch named in order: the source before the target, whole names only."""
    named = [_STATION_FORMS[hit.group()] for hit in _STATION_NAMES.finditer(text)]
    for n, stations in enumerate(named):
        if source in stations and any(target in later for later in named[n + 1 :]):
            return []
    absent = [place for place in (source, target) if not any(place in found for found in named)]
    # Both named, but the wrong way round: say the order, not a name that was written.
    return absent or [f"{source}→{target} 순서"]


def _confirmed(name, arguments):
    """
    The sentence the user is asked to approve must name what the call does.

    The approval window prints the arguments; the summary is the one line in it a person can
    read, so a summary that names another seat or train would approve the wrong thing. Raises
    Invalid, before anything is spent. Numbers are matched whole (8C is not in 18C, train 13 is
    not in 13:00), and a seat is read in the car it follows.
    """
    text = _said(arguments["summary"])
    missing, how = [], ""
    if name in ("reserve_seat", "start_watch"):
        _same_party(text, arguments.get("passengers", 1))
        seat_class = arguments.get("seatClass") or arguments.get("seat", "any")
        if arguments.get("waitlist"):
            # Korail's standby is applied for in 일반실 whatever seat says (request_waitlist).
            if seat_class == "special":
                raise Invalid(
                    "예약 대기는 일반실로만 신청돼요. seat 를 general 로 넣거나 빼 주세요."
                )
            if re.search(_CLASSES["special"], text):
                # Not even as a fallback ("일반실 우선, 없으면 특실"): there is none.
                raise Invalid("예약 대기는 일반실로만 신청돼요. summary 에서 특실을 빼 주세요.")
            seat_class = "general"
        _same_class(text, seat_class)
    if name == "reserve_seat":
        how = "호차와 좌석 번호(예: 4호차 1C)"
        missing = _seats_missing(text, arguments["seats"])
    elif name == "start_watch":
        how = "날짜·구간·열차 번호(예: 10월 19일 서울→부산 KTX 013)"
        date = (
            datetime.date.fromisoformat(arguments["date"]) if _is_date(arguments["date"]) else None
        )
        if date and not _date_in(text, date):
            missing.append(f"{date.month}월 {date.day}일")
        missing += _stations_missing(text, arguments["from"], arguments["to"])
        begins = _starts(arguments)
        if begins:
            how = (
                "감시를 시작할 시각과 날짜·구간·열차 번호"
                "(예: 10월 17일 07:00부터, 10월 19일 서울→부산 KTX 013)"
            )
            missing += _start_missing(text, begins, date)
        if len(arguments["trains"]) <= 5:
            start_day = begins.date() if begins else None
            missing += [
                number
                for number in arguments["trains"]
                if not _number_in(text, number, date, start_day)
            ]
        waits = "예약 대기" in text or "예약대기" in text.replace(" ", "")
        if arguments.get("waitlist") and not waits:
            missing.append("예약 대기")
        if waits and not arguments.get("waitlist"):
            raise Invalid(
                "summary 가 예약 대기 신청이라고 하는데 waitlist 는 false 예요. "
                "취소표 감시라면 '예약 대기'라는 말을 빼 주세요."
            )
    if missing:
        raise Invalid(
            f"summary 에 {how}를 사실대로 적어 주세요. 빠진 것: {', '.join(dict.fromkeys(missing))}"
        )


def _same_party(text, passengers):
    """A head count the sentence states must be the call's: "2명" approves two seats, not one."""
    said = {int(a or b) for a, b in _COUNT.findall(text)}
    said |= {_WORDS[a or b] for a, b in _WORD_COUNT.findall(text)}
    said -= {passengers}
    if said:
        raise Invalid(
            f"summary 에는 {min(said)}명(석)이라고 했는데 passengers 는 {passengers}예요. "
            "인원을 사실대로 적어 주세요."
        )


_CLASSES = {"general": "(?:일반실|일반석)", "special": "(?:특실|특석)"}
_SAID_CLASS = {"general": "일반실", "special": "특실"}
_RULED_OUT = (
    r"\s*(?:은|는|이|을|를|도)?\s*(?:말고|아닌|아니라|아님|대신|빼고|빼|제외|안\s*(?:돼|되))"
)


def _first(text, klass):
    """Whether the sentence puts this class first: "일반실 우선", "없으면 특실" puts the other."""
    other = "special" if klass == "general" else "general"
    return re.search(rf"{_CLASSES[klass]}\s*(?:을|를|이)?\s*우선|없으면\s*{_CLASSES[other]}", text)


def _same_class(text, seat_class):
    """특실 costs more: a sentence approving 특실 or 일반실 must say the class the call asks for."""
    for klass, spelled in _CLASSES.items():
        if re.search(spelled + _RULED_OUT, text):
            raise Invalid(
                f"summary 에서 {_SAID_CLASS[klass]}을 빼는 말은 읽는 사람이 헷갈려요. "
                "고르는 등급만 적어 주세요."
            )
    named = {klass for klass, spelled in _CLASSES.items() if re.search(spelled, text)}
    # 특실 only: a sentence that lets 일반실 do too ("일반실 또는 특실") approves something cheaper.
    if seat_class == "special" and named != {"special"}:
        raise Invalid("summary 에 특실이라고 적어 주세요. 이 호출은 특실 좌석만 잡아요.")
    if seat_class == "general" and (named == {"special"} or _first(text, "special")):
        raise Invalid(
            "summary 는 특실이라고 하는데 이 호출은 일반실만 잡아요. 등급을 사실대로 적어 주세요."
        )
    # "any" is 일반실 first and 특실 when there is none: one class named must be put that way.
    only = any(re.search(rf"{spelled}\s*만", text) for spelled in _CLASSES.values())
    general_first = _first(text, "general")
    special_first = _first(text, "special")
    if (
        seat_class == "any"
        and named
        and (only or special_first or not (len(named) == 2 or general_first))
    ):
        raise Invalid(
            "summary 는 한 등급만 말하는데 seat 는 any(일반실 우선, 없으면 특실)예요. "
            "'일반실 우선(없으면 특실)'처럼 적거나, 한 등급만 원하면 seat 를 general 이나 "
            "special 로 넣어 주세요."
        )


def _starts(arguments):
    """When a scheduled watch begins, on the railway's clock; None when it begins now."""
    try:
        begins = datetime.datetime.fromisoformat(arguments.get("start_at") or "")
    except ValueError:
        return None  # prepare says what is wrong with it
    try:
        return begins.astimezone(SEOUL) if begins.tzinfo else None
    except OverflowError as exc:
        raise Invalid("start_at 값을 확인해 주세요. 열차 날짜보다 앞선 시각이에요.") from exc


_AM, _PM = r"(?:오전|새벽|아침|A\.?M\.?)", r"(?:오후|저녁|밤|낮|P\.?M\.?)"


def _halves(hour):
    """The words for this hour's half of the day, and for the other half. 밤 12시 is midnight."""
    if hour == 0:
        return r"(?:밤|자정|오전|새벽|A\.?M\.?)", r"(?:오후|저녁|낮|P\.?M\.?)"
    if hour == 12:
        return r"(?:오후|낮|정오|P\.?M\.?)", r"(?:오전|새벽|아침|밤|A\.?M\.?)"
    return (_AM, _PM) if hour < 12 else (_PM, _AM)


def _clock_pattern(at):
    """
    The clock time said as 07:00, 7시, 7시 00분, 오전 7시 or 오후 7시 반, as one pattern, with the
    text it is to be found in: "오후 7시", "오후에 7시" and "7:00 PM" hold a 7시 or 7:00 that is
    not 07:00, so the other half of the day goes first.
    """
    hour, minute = at.hour, at.minute
    own, other = _halves(hour)
    if minute == 0:
        minutes = r"(?:\s*0{1,2}\s*분)?(?!\s*(?:\d+\s*분|반))"
    elif minute == 30:
        minutes = r"\s*(?:반|30\s*분)"
    else:
        minutes = rf"\s*{_part(minute)}\s*분"
    twelve = _part(hour % 12 or 12)
    forms = [
        rf"(?<!\d){_part(hour)}:{minute:02d}(?!\d)",
        rf"(?<!\d){_part(hour)}\s*시{minutes}",
        rf"{own}\s*{twelve}(?:\s*시{minutes}|:{minute:02d}(?!\d))",
        *([{0: "자정", 12: "정오"}[hour]] if minute == 0 and hour in (0, 12) else []),
    ]
    strip = [
        rf"{other}[가-힣]{{0,2}}\s*\d{{1,2}}",
        rf"\d{{1,2}}(?::\d{{2}})?\s*{other}(?![가-힣])",
    ]
    return "|".join(forms), strip


# The words that make a time the moment the watch begins, not a train's departure.
# Not "07:00부터 09:00까지 출발하는": that is the trains' window, not the watch's start.
_FROM = r"\s*(?:부터|에\s*(?:감시를?\s*)?시작)(?!\s*(?:\d{1,2}(?::\d{2}|\s*시)\S*\s*)?까지|\s*출발)"
# Any clock time with 부터: the watch's start is the last one ("07:00부터 다니는 … 05:00부터 감시").
_ANY_CLOCK_FROM = re.compile(
    r"(?:\d{1,2}:\d{2}|\d{1,2}\s*시(?:\s*\d{1,2}\s*분|\s*반)?|정오|자정)" + _FROM
)
_NOW = re.compile(r"지금부터|지금\s*바로|바로\s*(?:감시|시작)|즉시")
_ANY_DAY = (
    r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}\s*월\s*\d{1,2}\s*일|(?<!\d)\d{1,2}\s*[/.\-]\s*\d{1,2}(?!\d)"
    r"|(?<!\d)\d{1,2}\s*일)"
)


def _start_missing(text, begins, day):
    """
    A scheduled watch's sentence says when it begins: the time followed by 부터 (or "에 시작"),
    with the start's own day in front of it when that is not the trip's. A time anywhere else (a
    train leaving at 07:00) and the trip's day in front of the time are not the start.
    """
    if day and begins.date() > day:
        raise Invalid("start_at 이 열차 날짜보다 뒤예요. 감시는 열차가 떠나기 전에 시작해야 해요.")
    if _NOW.search(text):
        raise Invalid(
            "summary 는 지금 시작한다고 하는데 start_at 은 나중 시각이에요. "
            "지금 시작하려면 start_at 을 빼 주세요."
        )
    clock, strip = _clock_pattern(begins)
    for pattern in strip:
        text = re.sub(pattern, " ", text)
    other_day = begins.date() != day
    # Whatever day is written right before the time must be the start's own.
    pattern = rf"({_ANY_DAY}\s*(?:\([가-힣]\))?\s*(?:에|,|\.)?\s*)?(?:{clock}){_FROM}"
    last = max((hit.start() for hit in _ANY_CLOCK_FROM.finditer(text)), default=-1)
    for hit in re.finditer(pattern, text):
        if hit.end() <= last:
            continue
        written = hit.group(1)
        if _day_written(written, begins.date(), day) if written else not other_day:
            return []
    when = f"{begins.hour:02d}:{begins.minute:02d}"
    if other_day:
        when = f"{begins.month}월 {begins.day}일 {when}"
    return [f"감시 시작 시각 '{when}부터'"]


def _day_written(written, date, trip):
    """
    Whether the day written before a time is this date: "10월 17일", "10/17", or "17일" when the
    trip is in the same month (a reader takes a bare day in the trip's month).
    """
    bare = re.fullmatch(r"\s*(\d{1,2})\s*일\D*", written)
    same_month = trip is None or trip.month == date.month
    return _date_in(written, date) or bool(bare and same_month and int(bare.group(1)) == date.day)


def _is_date(raw):
    try:
        datetime.date.fromisoformat(raw)
    except ValueError:
        return False
    return True


def _trip_day(raw):
    """20261019 as a date, or None."""
    raw = str(raw or "")
    iso = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return datetime.date.fromisoformat(iso) if len(raw) == 8 and _is_date(iso) else None


def _trip_missing(text, day, source, target):
    """The day and the stretch of a watch or a train, as far as they are known."""
    missing = []
    if day and not _date_in(text, day):
        missing.append(f"{day.month}월 {day.day}일")
    if source in FALLBACK_STATIONS and target in FALLBACK_STATIONS:
        missing += _stations_missing(text, source, target)
    return missing


def _read_status(gateway, storage_id, name):
    """The status, for checking a sentence; None when it cannot be read (the call goes ahead)."""
    try:
        return gateway.status(storage_id)
    except Exception as exc:
        logger.warning("%s could not read the status (%s)", name, type(exc).__name__)
        return None


def unconfirmed_stop(gateway, storage_id, arguments):
    """Why stop_watch's sentence does not name the watch it stops, or None."""
    status = _read_status(gateway, storage_id, "stop_watch")
    if not status:
        return None
    scheduled = status.get("scheduled")
    watches = [
        status.get("running"),
        scheduled.get("search") if isinstance(scheduled, dict) else None,
    ]
    text = _said(arguments["summary"])
    missing = []
    for watch in watches:
        if isinstance(watch, dict):
            day = _trip_day(watch.get("depDate"))
            missing += _trip_missing(text, day, watch.get("srcLocate"), watch.get("dstLocate"))
    if not missing:
        return None
    return (
        "summary 에 멈추는 감시의 날짜와 구간을 사실대로 적어 주세요(예: 10월 19일 서울→부산). "
        f"빠진 것: {', '.join(dict.fromkeys(missing))}"
    )


# The train number in a pending reservation's trainInfo ("KTX 049 서울 → 부산 · 10월 19일(월) …").
# A pending reservation's trainInfo: "KTX 049 서울 → 부산 · 10월 19일(월) 07:00→09:40". The number
# follows the train's name; a station's digits (인천공항T1) are not it.
_HELD_TRAIN = re.compile(r"^\S+\s+(\d{1,5})(?!\d)")
_HELD_DAY = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")


def _held_day(info):
    """The day in a trainInfo, in the coming twelve months, or None."""
    hit = _HELD_DAY.search(info)
    if not hit:
        return None
    today = _today()
    for year in (today.year, today.year + 1):
        try:
            day = datetime.date(year, int(hit.group(1)), int(hit.group(2)))
        except ValueError:
            continue
        if day >= today - datetime.timedelta(days=1):
            return day
    return None


def unconfirmed_cancel(gateway, storage_id, arguments):
    """
    Why cancel_reservation's sentence does not name every reservation it gives back, or None.

    The cancel takes every pending reservation of the account, so the sentence names each train.
    """
    status = _read_status(gateway, storage_id, "cancel_reservation")
    held = [item for item in (status or {}).get("pending") or [] if isinstance(item, dict)]
    if len(held) > 5:
        return None
    infos = [str(item.get("trainInfo") or "") for item in held]
    days = [_held_day(info) for info in infos]
    numbers = [hit.group(1) for info in infos if (hit := _HELD_TRAIN.search(info))]
    text = _said(arguments["summary"])
    missing = [
        f"열차 {number}" for number in dict.fromkeys(numbers) if not _number_in(text, number, *days)
    ]
    if not missing:
        return None
    return (
        "summary 에 취소하는 예약의 열차 번호를 사실대로 적어 주세요. 결제를 기다리는 예약은 "
        f"모두 함께 취소돼요. 빠진 것: {', '.join(missing)}"
    )


def unconfirmed_train(gateway, storage_id, arguments):
    """Why reserve_seat's sentence names another train than the key's, or None."""
    train = gateway.reserved_train(storage_id, arguments["trainKey"])
    if not isinstance(train, dict):
        return None  # expired or unknown: the booking itself says so
    text = _said(arguments["summary"])
    missing = []
    day = _trip_day(train.get("depDate"))
    number = str(train.get("trainNo") or "")
    if number.isdigit() and not _number_in(text, number, day):
        missing.append(f"열차 번호 {number}")
    missing += _trip_missing(text, day, train.get("from"), train.get("to"))
    if not missing:
        return None
    return (
        "summary 에 예약하는 열차의 날짜·구간·번호를 사실대로 적어 주세요. "
        f"빠진 것: {', '.join(missing)}"
    )


def unlisted(gateway, storage_id, arguments):
    """Why start_watch's train numbers are refused, or None: they must come from find_trains."""
    listed = gateway.listed_trains(storage_id, {"conditions": conditions(arguments)})
    if all(number in listed for number in arguments["trains"]):
        return None
    numbers = ", ".join(listed) or "없음(먼저 같은 날짜·구간으로 find_trains 를 불러 주세요)"
    return (
        "find_trains 결과의 trainNo 를 그대로 넣어 주세요. 같은 date·from·to·earliest·latest·"
        f"train_type 으로 찾은 열차만 감시할 수 있어요. 가능한 번호: {numbers}"
    )


def _failure(name, exc, arguments=None):
    message = str(exc)
    if exc.status == 409 and message == SOLD_OUT_CLASS:
        return SOLD_OUT_CLASS_NOTE
    if message == SEARCH_RUNNING:
        return SEARCH_RUNNING_NOTE if name == "find_trains" else WATCH_RUNNING_NOTE
    if exc.status in (404, 410) and name in ("get_seat_map", "reserve_seat"):
        # The trainKey expired (30 minutes) or never existed.
        return "열차 정보가 없거나 만료됐어요. find_trains 를 다시 불러 주세요."
    if exc.status in (401, 502, 503, 504):
        # A login that failed is a read that failed too: never a sold-out train.
        if name in ("find_trains", "get_seat_map"):
            # Korail said why (down, or a maintenance window): not a date it does not sell yet.
            hint = "" if isinstance(exc, KorailLoginUnavailable) else _far_date(name, arguments)
            return f"{NOT_READ} {message}{hint}"
        if isinstance(exc, KorailLoginUnavailable):
            # Korail never took the login, so nothing was booked, started or given back.
            return message
        if exc.status != 401:
            # Not "unreachable": a refusal and a failed save come through here too. What the
            # agent needs to know is that the booking may or may not have happened.
            return f"{message} {UNSURE_OUTCOME.get(name, '')}".rstrip()
    return message


def _far_date(name, arguments):
    """A hint for a failed search of a date Korail may not have opened yet."""
    try:
        far = name == "find_trains" and (
            datetime.date.fromisoformat(arguments["date"]) > _today() + SALE_HORIZON
        )
    except (KeyError, TypeError, ValueError):
        return ""
    return f" {NOT_ON_SALE_NOTE}" if far else ""


def _result(value=None, error=None):
    if error is not None:
        return {"content": [{"type": "text", "text": error}], "isError": True}
    value = _korean_times(value)
    text = _text(value)
    if len(text.encode()) > MAX_RESULT_BYTES:
        return _result(error="결과가 너무 커서 보내지 못했어요. 조건을 좁혀 다시 시도해 주세요.")
    return {"content": [{"type": "text", "text": text}], "structuredContent": value}


def install(app, identity, gateway, store, locks):
    """POST /mcp. GET and DELETE answer 405: no stream, no session."""
    challenge = f'Bearer resource_metadata="{store.issuer}{PRM_PATH}", scope="{" ".join(SCOPES)}"'

    def reply(message_id, result=None, error=None, status=200):
        body = {"jsonrpc": "2.0", "id": message_id}
        body.update({"error": error} if error else {"result": result})
        return jsonify(body), status

    def call(token, agent, params):
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or name not in TOOLS or not isinstance(arguments, dict):
            return None
        _, scope, rail = TOOLS[name]
        if scope == BOOK and agent["scope"] != BOOK:
            return _result(error=BOOK_REFUSED)
        user = agent["storage_id"]
        try:
            run = prepare(name, arguments)
            if name == "start_watch" and (refused := unlisted(gateway, user, arguments)):
                return _result(error=refused)
            if name == "reserve_seat" and (refused := unconfirmed_train(gateway, user, arguments)):
                return _result(error=refused)
            # Checked before the budget, so a wrong sentence costs no call, and again under the
            # lock below, so it is checked against what then gets stopped or given back.
            confirm = {"stop_watch": unconfirmed_stop, "cancel_reservation": unconfirmed_cancel}
            if name in confirm and (refused := confirm[name](gateway, user, arguments)):
                return _result(error=refused)
            # The app counts the same calls (api.py authenticated): status reads are free.
            if rail and not identity.allow("rail:" + agent["id"], 10, 60):
                return _result(error=RAIL_TOO_MANY)
            with locks[abs(user) % len(locks)]:
                # A call queued behind another may outlive its connection or its permission.
                agent = store.agent(token)
                if agent is None:
                    raise Disconnected
                if scope == BOOK and agent["scope"] != BOOK:
                    return _result(error=BOOK_REFUSED)
                if name in confirm and (refused := confirm[name](gateway, user, arguments)):
                    return _result(error=refused)
                # ponytail: the app and its agents share one train list per user, so a search on
                # either side replaces the other's trainOptions and draft. trainKeys stay valid
                # for their 30 minutes, but start_watch refuses numbers the last list lacks.
                return _result(run(gateway, user))
        except Disconnected:
            raise
        except Invalid as exc:
            return _result(error=str(exc))
        except (AuthError, MiniAppError) as exc:
            return _result(error=_failure(name, exc, arguments))
        except Exception as exc:
            # Upstream exception strings can contain railway credentials.
            logger.error("Agent tool %s failed (%s)", name, type(exc).__name__)
            return _result(error="처리 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.")

    def unauthorized():
        response = jsonify(error="invalid_token")
        response.status_code = 401
        response.headers["WWW-Authenticate"] = challenge
        return response

    @app.post(PATH)
    def mcp():
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        agent = store.agent(token) if scheme.lower() == "bearer" else None
        if agent is None:
            return unauthorized()
        if not identity.allow("api:" + agent["id"], 120, 60):
            return reply(None, error={"code": -32000, "message": TOO_MANY}, status=429)
        if request.headers.get("MCP-Protocol-Version", VERSIONS[0]) not in VERSIONS:
            return reply(
                None, error={"code": -32600, "message": "Unsupported protocol version"}, status=400
            )
        try:
            message = json.loads(request.get_data())
        except (ValueError, RecursionError):
            return reply(None, error={"code": -32700, "message": "Parse error"})
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            return reply(None, error={"code": -32600, "message": "Invalid Request"})
        if "id" not in message:
            return Response(status=202)
        message_id, method = message["id"], message["method"]
        params = message.get("params", {})
        if not isinstance(params, dict):
            return reply(message_id, error={"code": -32602, "message": "Invalid params"})
        if method == "initialize":
            asked = params.get("protocolVersion")
            return reply(
                message_id,
                {
                    "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "jari", "title": "자리났다", "version": __version__},
                    "instructions": INSTRUCTIONS,
                },
            )
        if method == "ping":
            return reply(message_id, {})
        if method == "tools/list":
            return reply(message_id, {"tools": [spec[0] for spec in TOOLS.values()]})
        if method == "tools/call":
            try:
                result = call(token, agent, params)
            except Disconnected:
                return unauthorized()
            if result is None:
                return reply(message_id, error={"code": -32602, "message": "Unknown tool"})
            return reply(message_id, result)
        return reply(message_id, error={"code": -32601, "message": "Method not found"})
