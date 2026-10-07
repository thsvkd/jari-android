"""
에이전트가 부르는 MCP 엔드포인트(/api/mobile/mcp)를 명세 §5·§9 대로 고정한다.

도구는 앱이 부르는 것과 같은 gateway 메서드를 같은 입력 형태로 불러야 한다.
find_trains 의 조건은 앱 src/model.ts buildConditions 가 만드는 값과 같다.
조회 실패는 좌석 없음과 다르게 말하고, 권한 밖 도구는 gateway 에 닿지 않는다.
"""

import datetime
import json
import re
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from korail_bot.config.settings import settings
from korail_bot.mobile import api as api_module
from korail_bot.models.station_snapshot import FALLBACK_STATIONS
from korail_bot.services.mini_app_gateway import MiniAppError

from .agent_support import (
    BOOK_HINT,
    PRM_URL,
    app_headers,
    bearer,
    call_tool,
    cheap_setup,
    connect,
    make_env,
    rpc,
    signup,
    tool_text,
)

VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]
READ_TOOLS = {"find_trains", "get_seat_map", "get_status", "list_favourites"}
BOOK_TOOLS = {"start_watch", "stop_watch", "reserve_seat", "cancel_reservation"}
NOT_A_SOLD_OUT = "좌석이 없다는 뜻이 아니에요"
DATE = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
TRIP = {"date": DATE, "from": "서울", "to": "부산"}


@pytest.fixture(autouse=True)
def _cheap_setup(monkeypatch):
    cheap_setup(monkeypatch)


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


@pytest.fixture
def alice(env):
    return signup(env, "alice")


@pytest.fixture
def reader(env, alice):
    return connect(env, alice)["access_token"]


@pytest.fixture
def booker(env, alice):
    return connect(env, alice, allow_booking=True)["access_token"]


@pytest.fixture
def storage_id(env, alice):
    return env.identity.authenticate(alice["token"])["storage_id"]


def conditions_sent(mock):
    """gateway 가 받은 조건. 앱처럼 {"conditions": ...} 로 싸든 그대로 주든 같다."""
    payload = mock.call_args.args[1]
    return payload.get("conditions", payload)


# ----------------------------------------------------------------- 인증


def test_without_a_token_the_client_is_told_where_to_authorize(env):
    response = env.client.post(
        "/api/mobile/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
    )
    assert response.status_code == 401
    assert response.get_json() == {"error": "invalid_token"}
    challenge = response.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{PRM_URL}"' in challenge
    assert 'scope="jari.read jari.book"' in challenge


@pytest.mark.parametrize("token", ["", "x" * 43, "not even long"])
def test_an_invalid_token_gets_the_same_challenge(env, token):
    response = env.client.post(
        "/api/mobile/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers=bearer(token),
    )
    assert response.status_code == 401
    assert f'resource_metadata="{PRM_URL}"' in response.headers["WWW-Authenticate"]


# ----------------------------------------------------------------- 전송·수명주기


@pytest.mark.parametrize(
    ("asked", "answered"),
    [
        *((version, version) for version in VERSIONS),
        ("2024-11-05", "2025-11-25"),
        ("2099-01-01", "2025-11-25"),
    ],
)
def test_initialize_negotiates_the_protocol_version(env, reader, asked, answered):
    response = rpc(
        env,
        reader,
        "initialize",
        {
            "protocolVersion": asked,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    assert response.status_code == 200
    assert response.json["jsonrpc"] == "2.0"
    assert response.json["id"] == 1
    assert response.json["result"]["protocolVersion"] == answered


def test_initialize_describes_the_server_without_a_session(env, reader):
    response = rpc(
        env,
        reader,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    )
    result = response.json["result"]
    assert response.mimetype == "application/json"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "jari"
    assert result["serverInfo"]["title"] == "자리났다"
    assert isinstance(result["serverInfo"]["version"], str) and result["serverInfo"]["version"]
    assert "결제" in result["instructions"]
    assert "Mcp-Session-Id" not in response.headers


def test_a_notification_is_accepted_without_a_body(env, reader):
    response = rpc(env, reader, "notifications/initialized", id=None)
    assert response.status_code == 202
    assert response.get_data() == b""


def test_ping_answers_empty(env, reader):
    assert rpc(env, reader, "ping").json["result"] == {}


def test_a_session_id_sent_by_the_client_is_ignored(env, reader):
    response = rpc(env, reader, "ping", headers={"Mcp-Session-Id": "whatever"})
    assert response.status_code == 200
    assert "Mcp-Session-Id" not in response.headers


@pytest.mark.parametrize("method", ["get", "delete"])
def test_there_is_no_stream_and_no_session_to_end(env, reader, method):
    response = getattr(env.client, method)("/api/mobile/mcp", headers=bearer(reader))
    assert response.status_code == 405


def test_a_batch_is_an_invalid_request(env, reader):
    response = env.client.post(
        "/api/mobile/mcp",
        json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
        headers=bearer(reader),
    )
    assert response.get_json()["error"]["code"] == -32600


def test_an_unknown_method_is_named_as_such(env, reader):
    response = rpc(env, reader, "resources/list")
    assert response.json["error"]["code"] == -32601
    assert response.json["id"] == 1


def test_an_unsupported_protocol_version_header_is_refused(env, reader):
    response = rpc(env, reader, "ping", headers={"MCP-Protocol-Version": "1999-01-01"})
    assert response.status_code == 400


@pytest.mark.parametrize("version", VERSIONS)
def test_every_supported_protocol_version_header_is_accepted(env, reader, version):
    assert rpc(env, reader, "ping", headers={"MCP-Protocol-Version": version}).status_code == 200


# ----------------------------------------------------------------- tools/list


@pytest.fixture
def tools(env, reader):
    response = rpc(env, reader, "tools/list", {})
    return {tool["name"]: tool for tool in response.json["result"]["tools"]}


def test_the_tool_list_is_exactly_the_specified_tools(tools):
    assert set(tools) == READ_TOOLS | BOOK_TOOLS


def test_every_tool_has_a_title_and_a_closed_input_schema(tools):
    for name, tool in tools.items():
        assert tool.get("title"), name
        assert tool["inputSchema"]["type"] == "object", name
        assert tool["inputSchema"]["additionalProperties"] is False, name
        assert "annotations" in tool, name


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
def test_reading_tools_are_marked_read_only(tools, name):
    assert tools[name]["annotations"]["readOnlyHint"] is True


@pytest.mark.parametrize(
    ("name", "hints"),
    [
        ("start_watch", {"destructiveHint": False, "idempotentHint": False}),
        ("stop_watch", {"destructiveHint": False, "idempotentHint": True}),
        ("reserve_seat", {"destructiveHint": True}),
        ("cancel_reservation", {"destructiveHint": True}),
    ],
)
def test_booking_tools_say_what_they_change(tools, name, hints):
    annotations = tools[name]["annotations"]
    assert annotations.get("readOnlyHint", False) is False
    for key, value in hints.items():
        assert annotations[key] is value


@pytest.mark.parametrize("name", ["find_trains", "get_seat_map", "start_watch", "reserve_seat"])
def test_tools_that_reach_korail_are_open_world(tools, name):
    assert tools[name]["annotations"]["openWorldHint"] is True


@pytest.mark.parametrize("name", ["start_watch", "reserve_seat", "cancel_reservation"])
def test_tools_that_spend_or_hold_seats_ask_for_the_user(tools, name):
    assert tools[name]["_meta"]["anthropic/requiresUserInteraction"] is True


def test_start_watch_says_it_books_on_its_own(tools):
    assert "결제 대기" in tools["start_watch"]["description"]


def test_find_trains_offers_only_known_stations(tools):
    properties = tools["find_trains"]["inputSchema"]["properties"]
    assert set(properties["from"]["enum"]) == FALLBACK_STATIONS
    assert set(properties["to"]["enum"]) == FALLBACK_STATIONS
    assert set(properties["seat"]["enum"]) == {"general", "special", "any"}
    assert set(tools["find_trains"]["inputSchema"]["required"]) >= {"date", "from", "to"}


def test_calling_an_unknown_tool_is_an_invalid_params_error(env, reader):
    response = rpc(env, reader, "tools/call", {"name": "delete_everything", "arguments": {}})
    assert response.json["error"]["code"] == -32602


# ----------------------------------------------------------------- 읽기 도구


@pytest.mark.parametrize(
    ("seat", "option", "classes"),
    [("general", "2", ["general"]), ("special", "4", ["special"]), ("any", "1", None)],
)
def test_find_trains_sends_what_the_app_would_send(env, reader, storage_id, seat, option, classes):
    call_tool(
        env,
        reader,
        "find_trains",
        {**TRIP, "earliest": "07:00", "latest": "12:00", "passengers": 2, "seat": seat},
    )
    env.gateway.list_trains.assert_called_once()
    assert env.gateway.list_trains.call_args.args[0] == storage_id
    sent = conditions_sent(env.gateway.list_trains)
    assert sent["v"] == 1
    assert sent["action"] == "prepare_search"
    assert sent["dep_date"] == DATE.replace("-", "")
    assert sent["src_station"] == "서울"
    assert sent["dst_station"] == "부산"
    assert sent["dep_time"] == "0700"
    assert sent["max_dep_time"] == "1200"
    assert sent["passenger_count"] == 2
    assert sent["seat_option"] == option
    assert sent.get("seat_classes") == classes
    assert sent.get("seat_preference", "") == ""
    assert sent.get("waitlist", False) is False


def test_find_trains_defaults_to_the_whole_day_one_person_any_seat(env, reader):
    call_tool(env, reader, "find_trains", TRIP)
    sent = conditions_sent(env.gateway.list_trains)
    assert sent["dep_time"] == "0000"
    assert sent["max_dep_time"] == "2400"  # §11 L1
    assert sent["passenger_count"] == 1
    assert sent["seat_option"] == "1"


def test_find_trains_returns_structured_trains_and_the_same_text(env, reader):
    result = call_tool(env, reader, "find_trains", TRIP)
    assert not result.get("isError")
    [train] = result["structuredContent"]["trains"]
    assert train["trainKey"] == "T1"
    assert train["trainNo"] == "00101"
    assert train["name"] == "KTX"
    assert train["generalAvailable"] is True
    assert train["specialAvailable"] is False
    assert train["soldOut"] is False
    assert train["waitlistEligible"] is False
    assert train["departs"] and train["arrives"]
    assert result["content"][0]["type"] == "text"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


@pytest.mark.parametrize(
    "arguments",
    [
        {**TRIP, "extra": True},
        {**TRIP, "date": "10/10/2026"},
        {**TRIP, "from": "어딘가"},
        {**TRIP, "passengers": 10},
        {**TRIP, "passengers": 0},
        {**TRIP, "earliest": "25:00"},
        {**TRIP, "seat": "first"},
        {"from": "서울", "to": "부산"},
    ],
)
def test_find_trains_checks_its_input_on_the_server(env, reader, arguments):
    result = call_tool(env, reader, "find_trains", arguments)
    assert result["isError"] is True
    env.gateway.list_trains.assert_not_called()


def test_get_seat_map_reads_the_whole_train_once(env, reader, storage_id):
    call_tool(
        env, reader, "get_seat_map", {"trainKey": "T1", "seatClass": "general", "passengers": 2}
    )
    env.gateway.seat_inventories.assert_called_once()
    args = env.gateway.seat_inventories.call_args.args
    assert args[0] == storage_id
    assert args[1] == "T1"
    assert args[2] == "general"
    assert str(args[3]) == "2"


def seat(car, label, free=True):
    return {"carNo": car, "seatNo": f"{car}-{label}", "label": label, "salePossible": free}


def inventory(car, seats):
    return {"carNo": car, "remainingCount": sum(s["salePossible"] for s in seats), "seats": seats}


def test_get_seat_map_lists_the_free_seats_per_car(env, reader):
    env.gateway.seat_inventories.return_value = {
        "inventories": [inventory(3, [seat(3, "7A"), seat(3, "7B", free=False)])],
        "failedCars": [],
        "layoutReference": False,
    }
    result = call_tool(
        env, reader, "get_seat_map", {"trainKey": "T1", "seatClass": "general", "passengers": 1}
    )
    assert not result.get("isError")
    assert "7A" in tool_text(result)


def test_an_empty_seat_map_says_nothing_can_be_booked(env, reader):
    env.gateway.seat_inventories.return_value = {
        "inventories": [],
        "failedCars": [],
        "layoutReference": False,
    }
    result = call_tool(
        env, reader, "get_seat_map", {"trainKey": "T1", "seatClass": "general", "passengers": 1}
    )
    assert "예약 가능한 좌석이 없어요" in tool_text(result)


def test_a_partly_read_seat_map_says_some_cars_were_not_read(env, reader):
    env.gateway.seat_inventories.return_value = {
        "inventories": [inventory(1, [seat(1, "1A")])],
        "failedCars": [2, 3],
        "layoutReference": False,
    }
    result = call_tool(
        env, reader, "get_seat_map", {"trainKey": "T1", "seatClass": "general", "passengers": 1}
    )
    assert "일부 호차는 조회하지 못했어요" in tool_text(result)
    assert "예약 가능한 좌석이 없어요" not in tool_text(result)


def test_a_huge_seat_map_is_cut_to_100kb(env, reader):
    long_label = "가" * 200
    env.gateway.seat_inventories.return_value = {
        "inventories": [
            inventory(car, [seat(car, f"{row}{long_label}") for row in range(200)])
            for car in range(1, 21)
        ],
        "failedCars": [],
        "layoutReference": False,
    }
    result = call_tool(
        env, reader, "get_seat_map", {"trainKey": "T1", "seatClass": "general", "passengers": 1}
    )
    assert len(tool_text(result).encode()) <= 100 * 1024
    if "structuredContent" in result:
        assert (
            len(json.dumps(result["structuredContent"], ensure_ascii=False).encode()) <= 100 * 1024
        )


def test_get_status_and_list_favourites_read_the_users_own_state(env, reader, storage_id):
    status = call_tool(env, reader, "get_status")
    favourites = call_tool(env, reader, "list_favourites")
    env.gateway.status.assert_called_once_with(storage_id)
    env.gateway.favourites.assert_called_once_with(storage_id)
    assert not status.get("isError") and not favourites.get("isError")


# ----------------------------------------------------------------- 조회 실패와 좌석 없음


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_failed_train_search_is_not_reported_as_sold_out(env, reader, status):
    env.gateway.list_trains.side_effect = MiniAppError("열차 목록을 불러오지 못했어요.", status)
    response = env.client.post(
        "/api/mobile/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "find_trains", "arguments": TRIP},
        },
        headers=bearer(reader),
    )
    assert response.status_code == 200
    result = response.json["result"]
    assert result["isError"] is True
    assert NOT_A_SOLD_OUT in tool_text(result)


def test_a_failed_seat_map_is_not_reported_as_sold_out(env, reader):
    env.gateway.seat_inventories.side_effect = MiniAppError("좌석표를 불러오지 못했어요.", 502)
    result = call_tool(
        env, reader, "get_seat_map", {"trainKey": "T1", "seatClass": "general", "passengers": 1}
    )
    assert result["isError"] is True
    assert NOT_A_SOLD_OUT in tool_text(result)
    assert "예약 가능한 좌석이 없어요" not in tool_text(result)


def test_a_railway_refusal_keeps_its_own_message(env, reader):
    env.gateway.list_trains.side_effect = MiniAppError(
        "코레일 계정을 다시 연결한 뒤 신청해 주세요.", 428
    )
    result = call_tool(env, reader, "find_trains", TRIP)
    assert result["isError"] is True
    assert "코레일 계정을 다시 연결한 뒤 신청해 주세요." in tool_text(result)


def test_an_internal_failure_does_not_leak_its_message(env, reader):
    env.gateway.list_trains.side_effect = RuntimeError("korail-password-hunter2")
    response = env.client.post(
        "/api/mobile/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "find_trains", "arguments": TRIP},
        },
        headers=bearer(reader),
    )
    assert "hunter2" not in response.get_data(as_text=True)
    assert "RuntimeError" not in response.get_data(as_text=True)


# ----------------------------------------------------------------- 예약 도구와 권한


BOOK_CALLS = [
    ("start_watch", {**TRIP, "trains": ["00101"]}, "start_search"),
    ("stop_watch", {}, "cancel_search"),
    (
        "reserve_seat",
        {
            "trainKey": "T1",
            "seatClass": "general",
            "passengers": 1,
            "carNo": 3,
            "seats": [{"carNo": 3, "seatNo": "3-7A", "label": "7A"}],
        },
        "reserve_designated",
    ),
    ("cancel_reservation", {}, "cancel_pending"),
]


@pytest.mark.parametrize(("name", "arguments", "method"), BOOK_CALLS)
def test_a_read_only_connection_cannot_book(env, reader, name, arguments, method):
    result = call_tool(env, reader, name, arguments)
    assert result["isError"] is True
    assert BOOK_HINT in tool_text(result)
    getattr(env.gateway, method).assert_not_called()
    env.gateway.schedule_search.assert_not_called()


def test_start_watch_starts_the_same_search_the_app_starts(env, booker, storage_id):
    result = call_tool(
        env, booker, "start_watch", {**TRIP, "earliest": "07:00", "trains": ["00101", "00103"]}
    )
    assert not result.get("isError")
    env.gateway.start_search.assert_called_once()
    args = env.gateway.start_search.call_args.args
    assert args[0] == storage_id
    assert args[1]["trains"] == ["00101", "00103"]
    sent = conditions_sent(env.gateway.start_search)
    assert sent["dep_date"] == DATE.replace("-", "")
    assert sent["dep_time"] == "0700"
    assert sent.get("waitlist", False) is False
    env.gateway.schedule_search.assert_not_called()


def test_start_watch_can_ask_for_a_waitlist(env, booker):
    call_tool(env, booker, "start_watch", {**TRIP, "trains": ["00101"], "waitlist": True})
    assert conditions_sent(env.gateway.start_search)["waitlist"] is True


def test_start_watch_with_a_start_time_schedules_instead(env, booker, storage_id):
    start_at = "2026-12-01T07:00:00+09:00"
    call_tool(env, booker, "start_watch", {**TRIP, "trains": ["00101"], "start_at": start_at})
    env.gateway.start_search.assert_not_called()
    env.gateway.schedule_search.assert_called_once()
    args = env.gateway.schedule_search.call_args.args
    assert args[0] == storage_id
    assert args[1]["start_at"] == start_at
    assert args[1]["trains"] == ["00101"]


@pytest.mark.parametrize(
    "arguments",
    [
        {**TRIP, "trains": []},
        {**TRIP},
        {**TRIP, "trains": ["00101"], "start_at": "2026-12-01T07:00:00"},
        {**TRIP, "trains": ["00101"], "unknown": 1},
    ],
)
def test_start_watch_checks_its_input(env, booker, arguments):
    result = call_tool(env, booker, "start_watch", arguments)
    assert result["isError"] is True
    env.gateway.start_search.assert_not_called()
    env.gateway.schedule_search.assert_not_called()


def test_stop_watch_and_cancel_reservation_reach_the_users_own_state(env, booker, storage_id):
    call_tool(env, booker, "stop_watch")
    call_tool(env, booker, "cancel_reservation")
    env.gateway.cancel_search.assert_called_once_with(storage_id)
    env.gateway.cancel_pending.assert_called_once_with(storage_id)


def test_reserve_seat_books_the_chosen_seat_and_leaves_payment_to_the_user(env, booker, storage_id):
    arguments = BOOK_CALLS[2][1]
    result = call_tool(env, booker, "reserve_seat", arguments)
    assert not result.get("isError")
    env.gateway.reserve_designated.assert_called_once()
    args = env.gateway.reserve_designated.call_args.args
    assert args[0] == storage_id
    assert args[1]["trainKey"] == "T1"
    assert args[1]["seatClass"] == "general"
    assert args[1]["passengerCount"] == 1
    assert args[1]["carNo"] == 3
    assert [(s["carNo"], s["seatNo"], s["label"]) for s in args[1]["seats"]] == [(3, "3-7A", "7A")]
    text = tool_text(result)
    assert "결제는 직접 해 주세요" in text
    assert "https://www.korail.com/ticket/myticket/list" in text


# ----------------------------------------------------------------- 코레일 한도 공유


def rate_limited(response):
    # §11 M3: rail 초과는 HTTP 200 + 도구 결과 isError 다. HTTP 429 는 api 버킷 초과에만 쓴다.
    assert response.status_code == 200, response.get_data(as_text=True)
    result = response.json["result"]
    return result.get("isError") is True and "요청이 너무 많아요" in tool_text(result)


def test_agent_calls_count_against_the_apps_railway_budget(env, alice, reader):
    env.gateway.seat_cars.return_value = {"cars": []}
    for _ in range(10):
        assert (
            env.client.get("/api/mobile/trains/T1/cars", headers=app_headers(alice)).status_code
            == 200
        )
    response = rpc(env, reader, "tools/call", {"name": "find_trains", "arguments": TRIP})
    assert rate_limited(response), response.get_data(as_text=True)
    env.gateway.list_trains.assert_not_called()


def test_app_calls_after_ten_agent_searches_are_limited(env, alice, reader):
    for _ in range(10):
        assert not call_tool(env, reader, "find_trains", TRIP).get("isError")
    response = env.client.post(
        "/api/mobile/trains", headers=app_headers(alice), json={"conditions": {}}
    )
    assert response.status_code == 429


@pytest.mark.parametrize("name", ["get_status", "list_favourites", "stop_watch"])
def test_tools_that_skip_korail_do_not_spend_the_railway_budget(env, alice, booker, name):
    # §11 M2: get_status·list_favourites·stop_watch 는 rail 버킷을 세지 않는다(앱과 같은 기준).
    for _ in range(11):
        assert not call_tool(env, booker, name).get("isError")
    env.gateway.seat_cars.return_value = {"cars": []}
    assert (
        env.client.get("/api/mobile/trains/T1/cars", headers=app_headers(alice)).status_code == 200
    )


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_seat_map", {"trainKey": "T1", "seatClass": "general", "passengers": 1}),
        ("start_watch", {**TRIP, "trains": ["00101"]}),
        ("reserve_seat", BOOK_CALLS[2][1]),
        ("cancel_reservation", {}),
    ],
)
def test_every_tool_that_reaches_korail_spends_the_railway_budget(
    env, alice, booker, name, arguments
):
    env.gateway.seat_cars.return_value = {"cars": []}
    for _ in range(10):
        call_tool(env, booker, name, arguments)
    assert (
        env.client.get("/api/mobile/trains/T1/cars", headers=app_headers(alice)).status_code == 429
    )


def test_a_tool_over_the_railway_budget_never_reaches_the_gateway(env, booker):
    for _ in range(10):
        call_tool(env, booker, "cancel_reservation")
    response = rpc(env, booker, "tools/call", {"name": "cancel_reservation", "arguments": {}})
    assert rate_limited(response)
    assert env.gateway.cancel_pending.call_count == 10


def test_more_than_120_requests_a_minute_is_an_http_429(env, reader):
    # §11 M3: api:<user> 120/60s 는 앱과 같은 버킷이고, 넘으면 HTTP 429.
    env.clock.advance(61)  # 연결하며 쓴 앱 요청(lookup·approve)을 창 밖으로 보낸다.
    statuses = [rpc(env, reader, "ping").status_code for _ in range(121)]
    assert statuses == [200] * 120 + [429]


def test_app_requests_and_agent_requests_share_the_api_budget(env, alice, reader):
    env.clock.advance(61)
    for _ in range(60):
        assert env.client.get("/api/mobile/status", headers=app_headers(alice)).status_code == 200
    statuses = [rpc(env, reader, "ping").status_code for _ in range(61)]
    assert statuses == [200] * 60 + [429]


# ----------------------------------------------------------------- §11 M1·L2


@pytest.mark.parametrize("token_kind", ["expired", "revoked", "app_session"])
def test_every_401_carries_the_challenge(env, alice, token_kind):
    tokens = connect(env, alice)
    if token_kind == "expired":
        env.clock.advance(3601)
        token = tokens["access_token"]
    elif token_kind == "revoked":
        env.client.post(
            "/oauth/revoke",
            data={"token": tokens["access_token"], "client_id": tokens["client_id"]},
        )
        token = tokens["access_token"]
    else:
        token = alice["token"]
    response = rpc(env, token, "ping")
    assert response.status_code == 401
    assert f'resource_metadata="{PRM_URL}"' in response.headers["WWW-Authenticate"]


def test_a_railway_login_failure_stays_a_tool_error(env, reader):
    # 앱 경로는 MiniAppError 401 을 428 로 바꾼다. MCP 에서는 그 재매핑이 일어나면 안 된다.
    env.gateway.list_trains.side_effect = MiniAppError("코레일 비밀번호를 확인해 주세요.", 401)
    response = rpc(env, reader, "tools/call", {"name": "find_trains", "arguments": TRIP})
    assert response.status_code == 200
    assert response.json["result"]["isError"] is True
    assert "코레일 비밀번호를 확인해 주세요." in tool_text(response.json["result"])


@pytest.mark.parametrize(
    ("given", "sent"), [({"train_type": "ktx"}, "1"), ({"train_type": "all"}, "2"), ({}, "1")]
)
def test_find_trains_maps_train_type_like_the_app(env, reader, given, sent):
    call_tool(env, reader, "find_trains", {**TRIP, **given})
    assert conditions_sent(env.gateway.list_trains)["train_type"] == sent


def test_find_trains_offers_only_ktx_or_all(tools):
    assert set(tools["find_trains"]["inputSchema"]["properties"]["train_type"]["enum"]) == {
        "ktx",
        "all",
    }


def test_find_trains_refuses_another_train_type(env, reader):
    result = call_tool(env, reader, "find_trains", {**TRIP, "train_type": "1"})
    assert result["isError"] is True
    env.gateway.list_trains.assert_not_called()


# ----------------------------------------------------------------- §12 S-L1·S-L2·S-L4


@pytest.mark.parametrize("name", [["find_trains"], 1, None, {"a": 1}, True])
def test_a_tool_name_that_is_not_a_string_is_invalid_params(env, reader, name):
    response = rpc(env, reader, "tools/call", {"name": name, "arguments": {}})
    assert response.status_code == 200
    assert response.json["error"]["code"] == -32602


def test_an_oversized_mcp_body_is_a_json_rpc_error(env, reader):
    response = env.client.post(
        "/api/mobile/mcp",
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": "x" * (3 * 1024 * 1024)}
        ),
        content_type="application/json",
        headers=bearer(reader),
    )
    assert response.status_code == 413
    error = response.get_json()["error"]
    assert isinstance(error, dict), error
    assert error["code"] == -32600


def test_an_oversized_app_body_keeps_the_app_wording(env, alice):
    response = env.client.post(
        "/api/mobile/search", headers=app_headers(alice), json={"x": "x" * (2 * 1024 * 1024)}
    )
    assert response.status_code == 413
    assert "좌석" in response.json["error"]


class WatchedLock:
    """
    create_app 이 만드는 사용자 락(threading.RLock) 대신 쓰는 같은 동작의 락.
    다른 스레드가 쥐고 있어 기다려야 하는 순간 contended 를 켠다. 테스트는 그 신호를 보고 나서야
    상태를 바꾸므로, 두 번째 호출이 락 앞에 도착했는지를 시간에 기대지 않고 안다.
    """

    contended = threading.Event()
    _rlock = staticmethod(threading.RLock)

    def __init__(self):
        self._lock = self._rlock()

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            WatchedLock.contended.set()
            self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()


@pytest.fixture
def watched(tmp_path):
    """사용자 락을 WatchedLock 으로 바꾼 앱과 그 사용자. 락은 create_app 이 만들 때만 바꾼다."""
    WatchedLock.contended.clear()
    # api.py 가 보는 threading 만 바꾼다. 다른 모듈의 RLock 은 그대로다.
    with patch.object(api_module, "threading", SimpleNamespace(RLock=WatchedLock)):
        env = make_env(tmp_path)
    return env, signup(env, "alice")


class Held:
    """
    find_trains 가 사용자 락을 잡은 채 멈춰 있게 한다. 그동안 같은 사용자의 다음 도구 호출은 락을 기다린다.
    """

    def __init__(self, env, token):
        self.entered, self.release = threading.Event(), threading.Event()
        result = env.gateway.list_trains.return_value

        def slow(*args):
            self.entered.set()
            assert self.release.wait(10)
            return result

        env.gateway.list_trains.side_effect = slow
        client = env.app.test_client(use_cookies=False)
        self.first = threading.Thread(
            target=lambda: client.post(
                "/api/mobile/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "find_trains", "arguments": TRIP},
                },
                headers=bearer(token),
            )
        )
        self.first.start()
        assert self.entered.wait(10)

    def queue(self, env, token, name):
        """락 뒤에 줄을 세운 도구 호출. 끝나면 replies[0] 에 응답이 들어 있다."""
        client = env.app.test_client(use_cookies=False)
        self.replies = []
        self.second = threading.Thread(
            target=lambda: self.replies.append(
                client.post(
                    "/api/mobile/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": {}},
                    },
                    headers=bearer(token),
                )
            )
        )
        self.second.start()
        # 두 번째 호출이 인증·권한 검사를 지나 사용자 락에서 기다리기 시작할 때까지 기다린다.
        assert WatchedLock.contended.wait(10), "두 번째 호출이 락 앞에 오지 않았어요"

    def finish(self):
        self.release.set()
        self.first.join(10)
        self.second.join(10)
        return self.replies[0]


def test_a_call_waiting_for_the_lock_rechecks_the_token(watched):
    # §12 S-L4: 락을 기다리는 동안 연결이 끊기면 401.
    env, alice = watched
    tokens = connect(env, alice, allow_booking=True)
    held = Held(env, tokens["access_token"])
    held.queue(env, tokens["access_token"], "cancel_reservation")
    revoked = env.client.post(
        "/oauth/revoke", data={"token": tokens["access_token"], "client_id": tokens["client_id"]}
    )
    assert revoked.status_code == 200
    reply = held.finish()
    assert reply.status_code == 401
    env.gateway.cancel_pending.assert_not_called()


def test_a_call_waiting_for_the_lock_rereads_the_booking_permission(watched):
    # §12 S-L4: 락을 기다리는 동안 예약 허용이 꺼지면 isError. 앱의 토글 경로도 같은 락을 잡으므로
    # 여기서는 저장소를 직접 바꿔 "기다리는 사이에 꺼졌다"를 만든다(§6 의 oauth_grants.scope).
    env, alice = watched
    tokens = connect(env, alice, allow_booking=True)
    held = Held(env, tokens["access_token"])
    held.queue(env, tokens["access_token"], "cancel_reservation")
    with env.identity.connect() as db:
        db.execute("UPDATE oauth_grants SET scope='jari.read'")
    reply = held.finish()
    assert reply.status_code == 200
    assert reply.json["result"].get("isError") is True
    env.gateway.cancel_pending.assert_not_called()


# ----------------------------------------------------------------- §13 사용자 에이전트 반영

ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})")


def times_in(value):
    return ISO.findall(json.dumps(value, ensure_ascii=False))


def test_input_errors_do_not_spend_the_railway_budget(env, alice, reader):
    # §13 U3: scope 검사 → 입력 검증 → rail 버킷. 잘못 넣은 요청은 코레일을 부르지 않으니 세지 않는다.
    for _ in range(10):
        assert (
            call_tool(env, reader, "find_trains", {**TRIP, "date": "10/10/2026"})["isError"] is True
        )
    result = call_tool(
        env, reader, "get_seat_map", {"trainKey": "T1", "seatClass": "general", "passengers": 1}
    )
    assert not result.get("isError"), tool_text(result)
    env.gateway.seat_inventories.assert_called_once()


def test_refused_book_tools_do_not_spend_the_railway_budget(env, alice, reader):
    for _ in range(10):
        assert call_tool(env, reader, "cancel_reservation")["isError"] is True
    env.gateway.seat_cars.return_value = {"cars": []}
    assert (
        env.client.get("/api/mobile/trains/T1/cars", headers=app_headers(alice)).status_code == 200
    )


def test_the_railway_limit_says_when_to_try_again(env, booker):
    for _ in range(10):
        call_tool(env, booker, "cancel_reservation")
    result = call_tool(env, booker, "cancel_reservation")
    assert result["isError"] is True
    assert "1분쯤 뒤에 다시 해 주세요" in tool_text(result)


def test_instructions_ask_for_a_new_search_only_when_a_train_key_expired(env, reader):
    response = rpc(
        env,
        reader,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    )
    instructions = response.json["result"]["instructions"]
    assert "trainKey" in instructions and "만료" in instructions
    assert "예약 전에는 find_trains 를 다시" not in instructions


@pytest.mark.parametrize(
    ("message", "status"),
    [
        (
            "코레일에 로그인하지 못했어요. 잠시 후 다시 시도하고, 계속되면 계정을 다시 연결해 주세요.",
            401,
        ),
        ("열차 목록을 불러오지 못했어요. 잠시 후 다시 조회해 주세요.", 502),
    ],
)
def test_a_failed_read_says_so_once_with_the_gateway_message_once(env, reader, message, status):
    # §13 U4: 401 도 조회 실패다. "조회하지 못했어요(좌석이 없다는 뜻이 아니에요)" + gateway 문구 한 번.
    env.gateway.list_trains.side_effect = MiniAppError(message, status)
    result = call_tool(env, reader, "find_trains", TRIP)
    text = tool_text(result)
    assert result["isError"] is True
    assert text.count("조회하지 못했어요(좌석이 없다는 뜻이 아니에요)") == 1
    assert text.count(message) == 1
    assert text.count("다시 연결") <= 1


PENDING = {
    "reservationId": "R1",
    "trainInfo": "KTX 00101 서울 → 부산",
    "expiresAt": "2026-10-10T01:10:00+00:00",
    "seatNumber": "1호차 7A",
    "seatLabels": ["1호차 7A"],
    "seatClass": "general",
}


def test_status_tells_where_and_until_when_to_pay(env, reader):
    # §13 U5: 결제 대기 항목마다 paymentUrl 과 결제 기한.
    env.gateway.status.return_value = {"running": None, "scheduled": None, "pending": [PENDING]}
    result = call_tool(env, reader, "get_status")
    [pending] = result["structuredContent"]["pending"]
    assert pending["paymentUrl"] == settings.KORAIL_PAYMENT_URL
    assert pending["expiresAt"] == "2026-10-10T10:10:00+09:00"


RUNNING = {
    "depDate": "20261010",
    "srcLocate": "서울",
    "dstLocate": "부산",
    "depTime": "0700",
    "maxDepTime": "2400",
    "trainTypeShow": "KTX 계열만",
    "specialInfoShow": "GENERAL_ONLY",
    "passengerCount": 2,
    "seatStrategy": "consecutive",
    "seatPreference": "",
    "selectedTrains": ["00101"],
    "startedAt": "2026-10-07T00:00:00+00:00",
    "elapsedSeconds": 60.0,
    "health": "healthy",
    "lastCheckedAt": "2026-10-07T00:01:00+00:00",
    "attemptCount": 3,
}


def test_every_time_a_tool_returns_is_in_korean_time(env, booker):
    # §13 U6: 도구 결과의 시각은 모두 Asia/Seoul(+09:00).
    env.gateway.status.return_value = {
        "running": RUNNING,
        "scheduled": {"startAt": "2026-10-09T22:00:00+00:00"},
        "pending": [PENDING],
    }
    env.gateway.reserve_designated.return_value = {
        "reserved": True,
        "expiresAt": "2026-10-10T01:10:00+00:00",
        "paymentUrl": settings.KORAIL_PAYMENT_URL,
    }
    env.gateway.schedule_search.return_value = {
        "scheduled": True,
        "startAt": "2026-10-09T22:00:00+00:00",
    }
    outputs = [
        call_tool(env, booker, "get_status")["structuredContent"],
        call_tool(env, booker, "reserve_seat", BOOK_CALLS[2][1])["structuredContent"],
        call_tool(
            env,
            booker,
            "start_watch",
            {**TRIP, "trains": ["00101"], "start_at": "2026-10-10T07:00:00+09:00"},
        )["structuredContent"],
    ]
    for output in outputs:
        found = times_in(output)
        assert found, output
        assert all(moment.endswith("+09:00") for moment in found), found


@pytest.mark.parametrize("health", ["error", "stale"])
def test_status_notes_say_a_failing_search_is_not_a_sold_out(env, reader, health):
    # §13 U7: notes 가 내부 값을 한국어로 풀어 준다.
    env.gateway.status.return_value = {
        "running": {**RUNNING, "health": health},
        "scheduled": None,
        "pending": [],
    }
    result = call_tool(env, reader, "get_status")
    notes = json.dumps(result["structuredContent"]["notes"], ensure_ascii=False)
    assert "최근 조회가 계속 실패하고 있어요" in notes
    assert "좌석이 없다는 뜻이 아니에요" in notes


def test_status_notes_explain_seat_strategy_and_class_in_korean(env, reader):
    env.gateway.status.return_value = {"running": RUNNING, "scheduled": None, "pending": []}
    result = call_tool(env, reader, "get_status")
    notes = json.dumps(result["structuredContent"]["notes"], ensure_ascii=False)
    assert "연속" in notes
    assert "일반실" in notes
    # 내부 값은 그대로 둔다.
    assert result["structuredContent"]["running"]["seatStrategy"] == "consecutive"


def test_seat_map_says_what_each_free_seat_is_like(env, reader):
    # §13 U8: 좌석별로 창가·통로, 특수석 메시지(4인 동반석)를 짧게.
    def detailed(label, column, family=""):
        return {**seat(3, label), "column": column, "familyLabel": family, "direction": "1"}

    env.gateway.seat_inventories.return_value = {
        "inventories": [
            inventory(
                3, [detailed("7A", "A", "4인 동반석"), detailed("7B", "B"), detailed("8D", "D")]
            )
        ],
        "failedCars": [],
        "layoutReference": False,
    }
    result = call_tool(
        env, reader, "get_seat_map", {"trainKey": "T1", "seatClass": "general", "passengers": 1}
    )
    [car] = result["structuredContent"]["cars"]
    by_label = {entry["label"]: json.dumps(entry, ensure_ascii=False) for entry in car["freeSeats"]}
    assert "4인 동반석" in by_label["7A"]
    assert "창가" in by_label["7A"] and "창가" in by_label["8D"]
    assert "통로" in by_label["7B"]


@pytest.mark.parametrize(
    ("arguments", "hint"),
    [
        ({**TRIP, "date": "10/10/2026"}, "YYYY-MM-DD"),
        ({**TRIP, "from": "어딘가"}, "역 이름"),
        ({**TRIP, "earliest": "7시"}, "HH:MM"),
    ],
)
def test_input_errors_name_the_expected_form(env, reader, arguments, hint):
    # §13 U-Low.
    result = call_tool(env, reader, "find_trains", arguments)
    assert result["isError"] is True
    assert hint in tool_text(result)


def test_latest_24_00_means_no_limit(env, reader):
    result = call_tool(env, reader, "find_trains", {**TRIP, "latest": "24:00"})
    assert not result.get("isError"), tool_text(result)
    assert conditions_sent(env.gateway.list_trains)["max_dep_time"] == "2400"


# ----------------------------------------------------------------- 뮤테이션 생존 변이를 잡는 검사


def test_a_boolean_is_not_a_passenger_count(env, reader):
    assert call_tool(env, reader, "find_trains", {**TRIP, "passengers": True})["isError"] is True
    env.gateway.list_trains.assert_not_called()


def test_nine_passengers_is_the_largest_party(env, reader):
    assert not call_tool(env, reader, "find_trains", {**TRIP, "passengers": 9}).get("isError")


@pytest.mark.parametrize(("key", "accepted"), [("", False), ("k" * 200, True), ("k" * 201, False)])
def test_train_key_length_is_checked_on_the_server(env, reader, key, accepted):
    result = call_tool(env, reader, "get_seat_map", {"trainKey": key, "seatClass": "general"})
    assert bool(result.get("isError")) is not accepted
    assert env.gateway.seat_inventories.called is accepted


@pytest.mark.parametrize(("count", "accepted"), [(9, True), (10, False)])
def test_at_most_nine_seats_per_reservation(env, booker, count, accepted):
    seats = [{"carNo": 3, "seatNo": f"3-{n}A", "label": f"{n}A"} for n in range(1, count + 1)]
    arguments = {
        "trainKey": "T1",
        "seatClass": "general",
        "passengers": min(count, 9),
        "carNo": 3,
        "seats": seats,
    }
    result = call_tool(env, booker, "reserve_seat", arguments)
    assert bool(result.get("isError")) is not accepted
    assert env.gateway.reserve_designated.called is accepted


def test_a_bad_list_item_names_its_field(env, booker):
    result = call_tool(env, booker, "reserve_seat", {**BOOK_CALLS[2][1], "seats": ["7A"]})
    assert result["isError"] is True
    assert "seats" in tool_text(result)


@pytest.mark.parametrize(
    ("arguments", "named"),
    [({**TRIP, "extra": True}, "extra"), ({"from": "서울", "to": "부산"}, "date")],
)
def test_an_unknown_or_missing_field_is_named(env, reader, arguments, named):
    # 이 검사가 빠지면 뒤의 KeyError 가 "처리 중 문제"로 바뀌어 무엇이 틀렸는지 알 수 없다.
    result = call_tool(env, reader, "find_trains", arguments)
    assert result["isError"] is True
    assert named in tool_text(result)
    assert "처리 중 문제" not in tool_text(result)


def test_find_trains_sends_seat_strategy_and_an_empty_seat_preference(env, reader):
    call_tool(env, reader, "find_trains", TRIP)
    sent = conditions_sent(env.gateway.list_trains)
    assert sent["seat_strategy"] == "1"
    assert sent["seat_preference"] == ""


def test_passengers_default_to_one_for_seat_maps_and_reservations(env, booker):
    call_tool(env, booker, "get_seat_map", {"trainKey": "T1", "seatClass": "general"})
    assert env.gateway.seat_inventories.call_args.args[3] == "1"
    arguments = {key: value for key, value in BOOK_CALLS[2][1].items() if key != "passengers"}
    call_tool(env, booker, "reserve_seat", arguments)
    assert env.gateway.reserve_designated.call_args.args[1]["passengerCount"] == 1


def test_list_favourites_returns_them_under_favourites(env, reader):
    env.gateway.favourites.return_value = [{"id": "f1", "name": "주말 부산"}]
    result = call_tool(env, reader, "list_favourites")
    assert result["structuredContent"] == {"favourites": [{"id": "f1", "name": "주말 부산"}]}


@pytest.mark.parametrize(
    ("start_at", "hint"),
    [("next tuesday", "start_at"), ("2026-12-01T07:00:00", "+09:00")],
)
def test_a_bad_start_time_says_what_is_wrong(env, booker, start_at, hint):
    result = call_tool(
        env, booker, "start_watch", {**TRIP, "trains": ["00101"], "start_at": start_at}
    )
    assert result["isError"] is True
    assert hint in tool_text(result)


@pytest.mark.parametrize("status", [404, 410])
def test_an_expired_train_key_points_back_to_find_trains(env, reader, status):
    # §14 N5: 열차 정보가 없거나(404) 만료된(410) trainKey 는 같은 안내로 find_trains 로 돌려보낸다.
    env.gateway.seat_inventories.side_effect = MiniAppError("열차 정보를 찾을 수 없어요.", status)
    result = call_tool(env, reader, "get_seat_map", {"trainKey": "T1", "seatClass": "general"})
    assert result["isError"] is True
    assert tool_text(result) == "열차 정보가 없거나 만료됐어요. find_trains 를 다시 불러 주세요."


def test_a_failed_booking_call_sends_the_agent_to_get_status(env, booker):
    # 조회가 아닌 도구가 코레일에 닿지 못하면 결과를 get_status 로 확인하라고 한다(이미 됐을 수 있다).
    message = "예약을 취소하지 못했어요."
    env.gateway.cancel_pending.side_effect = MiniAppError(message, 502)
    text = tool_text(call_tool(env, booker, "cancel_reservation"))
    assert message in text
    assert "get_status" in text


def test_a_failed_search_does_not_send_the_agent_to_get_status(env, reader):
    env.gateway.list_trains.side_effect = MiniAppError("열차 목록을 불러오지 못했어요.", 502)
    text = tool_text(call_tool(env, reader, "find_trains", TRIP))
    assert "열차 목록을 불러오지 못했어요." in text
    assert "get_status" not in text


def test_an_error_result_is_text_content(env, reader):
    result = call_tool(env, reader, "find_trains", {**TRIP, "extra": 1})
    assert result["content"][0]["type"] == "text"


def test_tools_call_without_arguments_is_an_empty_object(env, reader):
    response = rpc(env, reader, "tools/call", {"name": "get_status"})
    assert "error" not in response.json
    assert not response.json["result"].get("isError")


def test_a_result_too_big_to_send_says_so(env, reader):
    env.gateway.favourites.return_value = [{"id": str(n), "name": "가" * 200} for n in range(400)]
    result = call_tool(env, reader, "list_favourites")
    assert result["isError"] is True
    assert "너무 커서" in tool_text(result)


def test_an_internal_failure_says_something_went_wrong(env, reader):
    env.gateway.status.side_effect = RuntimeError("boom")
    result = call_tool(env, reader, "get_status")
    assert result["isError"] is True
    assert "처리 중 문제가 생겼어요" in tool_text(result)


def test_the_reservation_note_carries_the_payment_address(env, booker):
    result = call_tool(env, booker, "reserve_seat", BOOK_CALLS[2][1])
    url = env.gateway.reserve_designated.return_value["paymentUrl"]
    assert any(url in note for note in result["structuredContent"]["notes"])


def test_the_railway_budget_window_is_sixty_seconds(env, booker):
    for _ in range(10):
        call_tool(env, booker, "cancel_reservation")
    assert call_tool(env, booker, "cancel_reservation")["isError"] is True
    env.clock.advance(60)
    assert not call_tool(env, booker, "cancel_reservation").get("isError")


# ----------------------------------------------------------------- §13 U1 (gateway.listed_trains)

LISTED_HINT = re.compile(r"find_trains 결과의 trainNo ?를 그대로 넣어 주세요")


@pytest.mark.parametrize(
    ("listed", "trains"),
    [([], ["00101"]), (["00105"], ["00101", "00105"]), (["00105", "00107"], ["00101"])],
)
def test_start_watch_refuses_numbers_find_trains_did_not_return(env, alice, booker, listed, trains):
    env.gateway.listed_trains.return_value = listed
    result = call_tool(env, booker, "start_watch", {**TRIP, "trains": trains})
    assert result.get("isError") is True, result
    text = tool_text(result)
    assert LISTED_HINT.search(text), text
    for number in listed:
        assert number in text
    env.gateway.start_search.assert_not_called()
    env.gateway.schedule_search.assert_not_called()
    # 앞 검사에서 거절한 호출은 rail 버킷을 쓰지 않는다.
    env.gateway.seat_cars.return_value = {"cars": []}
    statuses = [
        env.client.get("/api/mobile/trains/T1/cars", headers=app_headers(alice)).status_code
        for _ in range(10)
    ]
    assert statuses == [200] * 10


def test_start_watch_asks_for_the_list_of_the_same_trip(env, booker, storage_id):
    call_tool(env, booker, "start_watch", {**TRIP, "earliest": "07:00", "trains": ["00101"]})
    env.gateway.listed_trains.assert_called_once()
    args = env.gateway.listed_trains.call_args.args
    assert args[0] == storage_id
    sent = args[1].get("conditions", args[1])
    assert sent["dep_date"] == DATE.replace("-", "")
    assert sent["src_station"] == "서울"
    assert sent["dst_station"] == "부산"
    env.gateway.start_search.assert_called_once()


def test_the_refusal_lists_every_number_find_trains_returned(env, booker):
    env.gateway.listed_trains.return_value = ["00105", "00107"]
    text = tool_text(call_tool(env, booker, "start_watch", {**TRIP, "trains": ["00101"]}))
    assert "가능한 번호: 00105, 00107" in text


def test_with_no_list_yet_the_refusal_says_to_call_find_trains_first(env, booker):
    env.gateway.listed_trains.return_value = []
    text = tool_text(call_tool(env, booker, "start_watch", {**TRIP, "trains": ["00101"]}))
    assert "없음(먼저 같은 날짜·구간으로 find_trains 를 불러 주세요)" in text


def test_a_railway_login_failure_on_a_booking_tool_is_passed_on_as_is(env, booker):
    # 조회가 아닌 도구의 401 은 "닿지 못했어요/get_status" 를 붙이지 않는다(코레일에는 닿았다).
    message = "코레일에 로그인하지 못했어요. 계정을 다시 연결해 주세요."
    env.gateway.cancel_pending.side_effect = MiniAppError(message, 401)
    text = tool_text(call_tool(env, booker, "cancel_reservation"))
    assert text == message


def test_reserve_seat_passes_the_party_size(env, booker):
    seats = [{"carNo": 3, "seatNo": f"3-{n}A", "label": f"{n}A"} for n in (1, 2)]
    call_tool(env, booker, "reserve_seat", {**BOOK_CALLS[2][1], "passengers": 2, "seats": seats})
    assert env.gateway.reserve_designated.call_args.args[1]["passengerCount"] == 2


# ----------------------------------------------------------------- §15 N6 고를 수 있는 값을 알려 주는 입력 오류


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("find_trains", {**TRIP, "seat": "first"}, "seat 값은 general, special, any 중 하나예요"),
        ("find_trains", {**TRIP, "train_type": "1"}, "train_type 값은 ktx, all 중 하나예요"),
        ("find_trains", {**TRIP, "passengers": 10}, "passengers 값은 1~9 사이예요"),
        ("find_trains", {**TRIP, "passengers": 0}, "passengers 값은 1~9 사이예요"),
        ("find_trains", {**TRIP, "date": "2026-02-30"}, "date 값은 실제 있는 날짜(YYYY-MM-DD)예요"),
        (
            "get_seat_map",
            {"trainKey": "T1", "seatClass": "first"},
            "seatClass 값은 general, special 중 하나예요",
        ),
        (
            "reserve_seat",
            {**BOOK_CALLS[2][1], "carNo": 100},
            "carNo 값은 1~99 사이예요",
        ),
    ],
)
def test_choice_and_range_errors_say_what_is_allowed(env, booker, tool, arguments, message):
    result = call_tool(env, booker, tool, arguments)
    assert result["isError"] is True
    assert message in tool_text(result)
