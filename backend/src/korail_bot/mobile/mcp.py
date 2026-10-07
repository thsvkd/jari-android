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
from korail_bot.mobile.oauth import BOOK, PRM_PATH, SCOPES
from korail_bot.models.station_snapshot import FALLBACK_STATIONS
from korail_bot.services.mini_app_gateway import MiniAppError

logger = logging.getLogger(__name__)

PATH = "/api/mobile/mcp"
VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]
SEOUL = ZoneInfo("Asia/Seoul")
ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})")
MAX_RESULT_BYTES = 100 * 1024
NOT_READ = "조회하지 못했어요(좌석이 없다는 뜻이 아니에요)."
TOO_MANY = "요청이 너무 많아요. 잠시 후 다시 시도해 주세요."
RAIL_TOO_MANY = "요청이 너무 많아요. 코레일 조회는 1분에 10번까지예요. 1분쯤 뒤에 다시 해 주세요."
BOOK_REFUSED = (
    "이 연결은 조회만 할 수 있어요. 예약을 맡기려면 사용자가 "
    "앱의 설정 → 에이전트 연결에서 '예약까지 맡기기'를 켜 주세요."
)
INSTRUCTIONS = (
    "자리났다는 코레일 열차를 찾고 취소표를 감시해 좌석을 잡아 주는 앱이에요. "
    "조회에 실패한 것과 좌석이 없는 것은 다른 결과예요: 실패하면 그대로 알리고 매진이라고 말하지 마세요. "
    "예약은 결제 대기까지만 진행돼요. 결제는 사용자가 결제 기한 안에 앱이나 코레일에서 직접 해야 해요. "
    "실패한 예약을 성공했다고 말하지 마세요. "
    "감시 시작·좌석 예약·예약 취소 전에는 사용자에게 확인을 받아 주세요. "
    "start_watch 의 trains 에는 find_trains 결과의 trainNo 를 그대로 넣어 주세요. "
    "trainKey 가 만료됐다는 오류가 나면 그때 find_trains 를 다시 불러 주세요."
)
HEALTH_NOTES = {
    "healthy": "감시 중이에요. 코레일을 계속 조회하고 있어요.",
    "running": "감시 중이에요. 코레일을 계속 조회하고 있어요.",
    "error": "최근 조회가 계속 실패하고 있어요. 좌석이 없다는 뜻이 아니에요.",
    "stale": "최근 조회가 계속 실패하고 있어요. 좌석이 없다는 뜻이 아니에요.",
    "unavailable": "감시가 멈춰 있어요. 사용자에게 앱에서 확인해 달라고 알려 주세요.",
    "unknown": "감시 상태를 아직 확인하지 못했어요. 잠시 뒤 다시 확인해 주세요.",
}
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
            "trainKey 는 get_seat_map·reserve_seat 에, trainNo 는 start_watch 에 써요.",
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
                    **TRIP,
                    "trains": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 30,
                        "items": {"type": "string", "pattern": r"[0-9]{1,5}"},
                        "description": "find_trains 결과의 trainNo 를 그대로 넣어요",
                    },
                    "waitlist": {"type": "boolean", "description": "기본 false"},
                    "start_at": {
                        "type": "string",
                        "maxLength": 64,
                        "description": "ISO 8601, 시간대 포함(예: 2026-12-01T07:00:00+09:00)",
                    },
                },
                ("date", "from", "to", "trains"),
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
            _schema({}),
            False,
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
                ("trainKey", "seatClass", "carNo", "seats"),
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
            _schema({}),
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
    return {
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
    if failed:
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


def _reserved(result):
    return {
        **result,
        "notes": [
            "좌석을 잡았어요(결제 대기). 결제는 직접 해 주세요: 결제 기한 안에 앱이나 "
            f"코레일({result.get('paymentUrl') or settings.KORAIL_PAYMENT_URL})에서 결제하지 않으면 "
            "예약이 취소돼요."
        ],
    }


def _status(result):
    """What the app's status says, with where to pay and a Korean reading of the codes."""
    pending = [
        {**item, "paymentUrl": settings.KORAIL_PAYMENT_URL} for item in result.get("pending") or []
    ]
    notes = []
    running = result.get("running")
    if running:
        notes.append(HEALTH_NOTES.get(running.get("health"), HEALTH_NOTES["unknown"]))
        seat_class, count = running.get("specialInfoShow"), running.get("passengerCount")
        strategy = running.get("seatStrategy")
        # A value the status left out is left out of the sentence too, never read as "None".
        parts = [
            SEAT_CLASS_NOTES.get(seat_class, seat_class) if seat_class else None,
            f"{count}명" if count else None,
            STRATEGY_NOTES.get(strategy, strategy) if strategy else None,
        ]
        if any(parts):
            notes.append("좌석 조건: " + ", ".join(part for part in parts if part))
    if result.get("scheduled"):
        notes.append("정한 시각에 감시를 시작해요.")
    if pending:
        notes.append(
            f"결제를 기다리는 예약이 {len(pending)}건 있어요. "
            "결제 기한 안에 사용자가 직접 결제해야 해요."
        )
    return {
        **result,
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
        return datetime.datetime.fromisoformat(value).astimezone(SEOUL).isoformat()
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
            return lambda gateway, user: gateway.start_search(user, payload)
        try:
            start_at = datetime.datetime.fromisoformat(arguments["start_at"])
        except ValueError as exc:
            raise Invalid("start_at 값을 확인해 주세요. ISO 8601 형식이에요.") from exc
        if start_at.tzinfo is None:
            raise Invalid("start_at 에 시간대를 넣어 주세요(예: +09:00).")
        payload["start_at"] = arguments["start_at"]
        return lambda gateway, user: gateway.schedule_search(user, payload)
    if name == "stop_watch":
        return lambda gateway, user: gateway.cancel_search(user)
    if name == "reserve_seat":
        payload = {
            "trainKey": arguments["trainKey"],
            "seatClass": arguments["seatClass"],
            "passengerCount": arguments.get("passengers", 1),
            "carNo": arguments["carNo"],
            "seats": arguments["seats"],
        }
        return lambda gateway, user: _reserved(gateway.reserve_designated(user, payload))
    return lambda gateway, user: gateway.cancel_pending(user)


def unlisted(gateway, storage_id, arguments):
    """Why start_watch's train numbers are refused, or None: they must come from find_trains."""
    listed = gateway.listed_trains(storage_id, {"conditions": conditions(arguments)})
    if all(number in listed for number in arguments["trains"]):
        return None
    numbers = ", ".join(listed) or "없음(먼저 같은 날짜·구간으로 find_trains 를 불러 주세요)"
    return f"find_trains 결과의 trainNo 를 그대로 넣어 주세요. 가능한 번호: {numbers}"


def _failure(name, exc):
    message = str(exc)
    if exc.status in (404, 410):
        # The trainKey is gone (the list was replaced or it expired) or never existed.
        return "열차 정보가 없거나 만료됐어요. find_trains 를 다시 불러 주세요."
    if exc.status in (401, 502, 503, 504):
        # A login that failed is a read that failed too: never a sold-out train.
        if name in ("find_trains", "get_seat_map"):
            return f"{NOT_READ} {message}"
        if exc.status != 401:
            return f"{message} 코레일에 닿지 못했어요. 결과는 get_status 로 확인해 주세요."
    return message


def _result(value=None, error=None):
    if error is not None:
        return {"content": [{"type": "text", "text": error}], "isError": True}
    value = _korean_times(value)
    text = _text(value)
    if len(text.encode()) > MAX_RESULT_BYTES:
        return _result(error="결과가 너무 커서 보내지 못했어요. 조건을 좁혀 다시 시도해 주세요.")
    return {"content": [{"type": "text", "text": text}], "structuredContent": value}


def install(app, identity, gateway, store, locks):
    """POST /api/mobile/mcp. GET and DELETE answer 405: no stream, no session."""
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
                # ponytail: the app and its agents share one train list per user, so
                # a search on either side replaces the other's trainKeys (410 says so).
                return _result(run(gateway, user))
        except Disconnected:
            raise
        except Invalid as exc:
            return _result(error=str(exc))
        except (AuthError, MiniAppError) as exc:
            return _result(error=_failure(name, exc))
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
