"""
승인 창에 보이는 문장(summary)과, 그 문장이 실제 호출과 맞는지, 그리고 사용자가 읽는 오류·안내 문구.

승인 창은 인자를 그대로 찍는다. 사람이 읽을 수 있는 건 summary 한 줄뿐이라, 다른 열차나 좌석을
말하는 summary 는 엉뚱한 일을 승인하게 만든다. 서버가 코레일 예산을 쓰기 전에 거절한다.
"""

import datetime

import pytest

from korail_bot.mobile import mcp
from korail_bot.services.mini_app_gateway import SOLD_OUT_CLASS, MiniAppError

from .agent_support import call_tool, cheap_setup, connect, make_env, signup, tool_text

TODAY = datetime.date(2026, 10, 12)
DATE = (TODAY + datetime.timedelta(days=7)).isoformat()
TRIP = {"date": DATE, "from": "서울", "to": "부산"}
RESERVE = {
    "trainKey": "T1",
    "seatClass": "special",
    "passengers": 1,
    "carNo": 2,
    "seats": [{"carNo": 2, "seatNo": "2-8C", "label": "8C"}],
}


@pytest.fixture(autouse=True)
def _cheap_setup(monkeypatch):
    cheap_setup(monkeypatch)
    now = datetime.datetime(2026, 10, 12, 3, 0, tzinfo=datetime.UTC)
    monkeypatch.setattr(mcp, "_now", lambda: now)


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


@pytest.fixture
def booker(env):
    return connect(env, signup(env, "alice"), allow_booking=True)["access_token"]


@pytest.mark.parametrize(
    "tool", ["start_watch", "stop_watch", "reserve_seat", "cancel_reservation"]
)
def test_a_tool_that_changes_things_asks_for_a_sentence_the_user_can_read(tool):
    schema = mcp.TOOLS[tool][0]["inputSchema"]
    assert "summary" in schema["required"]
    assert next(iter(schema["properties"])) == "summary"
    assert "자연어" in schema["properties"]["summary"]["description"]


@pytest.mark.parametrize(
    "tool",
    sorted(set(mcp.TOOLS) - {"start_watch", "stop_watch", "reserve_seat", "cancel_reservation"}),
)
def test_a_read_tool_needs_no_summary(tool):
    assert "summary" not in mcp.TOOLS[tool][0]["inputSchema"]["properties"]


@pytest.mark.parametrize(
    ("tool", "arguments", "gateway_method"),
    [
        ("start_watch", {**TRIP, "trains": ["00101"]}, "start_search"),
        ("stop_watch", {}, "cancel_search"),
        ("reserve_seat", RESERVE, "reserve_designated"),
        ("cancel_reservation", {}, "cancel_pending"),
    ],
)
def test_a_call_without_a_sentence_is_refused_before_it_reaches_the_gateway(
    env, booker, tool, arguments, gateway_method
):
    result = call_tool(env, booker, tool, arguments, summary=False)
    assert result["isError"] is True
    assert "summary" in tool_text(result)
    assert not getattr(env.gateway, gateway_method).called


def test_a_reservation_sentence_must_name_the_car_and_seat(env, booker):
    said = "KTX 043 서울→부산 특실 좌석을 예약해요"
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said})
    assert result["isError"] is True
    text = tool_text(result)
    assert "2호차" in text
    assert "8C" in text
    env.gateway.reserve_designated.assert_not_called()


def test_a_reservation_sentence_that_names_another_seat_is_refused(env, booker):
    result = call_tool(
        env, booker, "reserve_seat", {**RESERVE, "summary": "KTX 043 특실 2호차 7A 1석을 예약해요"}
    )
    assert result["isError"] is True
    env.gateway.reserve_designated.assert_not_called()


def test_a_true_reservation_sentence_goes_through_however_it_is_spaced(env, booker):
    env.gateway.reserve_designated.return_value = {"reserved": True, "pending": []}
    said = "KTX 043 서울→부산 특실 2 호차 8c 1석을 예약해요"
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said})
    assert not result.get("isError"), tool_text(result)
    env.gateway.reserve_designated.assert_called_once()


@pytest.mark.parametrize(
    ("said", "missing"),
    [
        ("서울→부산 KTX 013 취소표를 감시해요", "10월 19일"),
        ("10월 19일 서울→대구 KTX 013 취소표를 감시해요", "부산"),
        ("10월 19일 서울→부산 취소표를 감시해요", "013"),
    ],
)
def test_a_watch_sentence_must_name_the_day_the_stretch_and_the_trains(env, booker, said, missing):
    arguments = {**TRIP, "date": "2026-10-19", "trains": ["013"], "summary": said}
    result = call_tool(env, booker, "start_watch", arguments)
    assert result["isError"] is True
    assert missing in tool_text(result)
    env.gateway.start_search.assert_not_called()


def test_a_watch_sentence_can_write_the_date_and_train_the_way_people_do(env, booker):
    env.gateway.listed_trains.return_value = ["013"]
    env.gateway.start_search.return_value = {"started": True}
    arguments = {
        **TRIP,
        "date": "2026-10-19",
        "trains": ["013"],
        "summary": "10/19 서울에서 부산 가는 KTX 13편 취소표를 감시해요",
    }
    result = call_tool(env, booker, "start_watch", arguments)
    assert not result.get("isError"), tool_text(result)


def test_a_waitlist_sentence_says_it_is_a_waitlist(env, booker):
    arguments = {
        **TRIP,
        "trains": ["013"],
        "waitlist": True,
        "summary": f"{int(DATE[5:7])}월 {int(DATE[8:])}일 서울→부산 013 열차 취소표를 감시해요",
    }
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert result["isError"] is True
    assert "예약 대기" in tool_text(result)


# ---------------------------------------------------------------- 입력 오류 문구


def test_a_search_that_ends_before_it_starts_names_both_fields_and_costs_no_budget(env, booker):
    for _ in range(11):
        result = call_tool(
            env, booker, "find_trains", {**TRIP, "earliest": "15:00", "latest": "13:00"}
        )
        assert tool_text(result) == "latest 값은 earliest 보다 늦은 시각이에요."
    env.gateway.list_trains.assert_not_called()


def test_a_past_date_names_its_field(env, booker):
    past = (TODAY - datetime.timedelta(days=1)).isoformat()
    result = call_tool(env, booker, "find_trains", {**TRIP, "date": past})
    assert tool_text(result) == "date 값은 오늘이거나 그 뒤 날짜예요."
    env.gateway.list_trains.assert_not_called()


def test_today_is_still_a_date_to_search(env, booker):
    env.gateway.list_trains.return_value = {"trains": [], "truncated": False}
    result = call_tool(env, booker, "find_trains", {**TRIP, "date": TODAY.isoformat()})
    assert not result.get("isError"), tool_text(result)


def test_a_malformed_train_number_says_what_a_train_number_is(env, booker):
    arguments = {**TRIP, "trains": ["abc"], "summary": "10월 19일 서울→부산 abc 감시를 시작해요"}
    text = tool_text(call_tool(env, booker, "start_watch", arguments))
    assert "숫자 1~5자리" in text


# ---------------------------------------------------------------- 실패 문구


def test_a_failed_far_date_search_says_the_date_may_not_be_on_sale_yet(env, booker):
    env.gateway.list_trains.side_effect = MiniAppError("열차 목록을 불러오지 못했어요.", 502)
    far = (TODAY + datetime.timedelta(days=200)).isoformat()
    text = tool_text(call_tool(env, booker, "find_trains", {**TRIP, "date": far}))
    assert "좌석이 없다는 뜻이 아니에요" in text
    assert "아직 승차권을 팔지 않는 먼 날짜일 수 있어요" in text


def test_a_failed_near_date_search_does_not_blame_the_date(env, booker):
    env.gateway.list_trains.side_effect = MiniAppError("열차 목록을 불러오지 못했어요.", 502)
    text = tool_text(call_tool(env, booker, "find_trains", TRIP))
    assert "먼 날짜" not in text


def test_a_sold_out_class_sends_the_agent_to_a_watch_not_back_to_the_seat_map(env, booker):
    env.gateway.reserve_designated.side_effect = MiniAppError(SOLD_OUT_CLASS, 409)
    text = tool_text(
        call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": "특실 2호차 8C 예약"})
    )
    assert "매진" in text
    assert "start_watch" in text
    assert "좌석표를 새로 불러와" not in text


def test_the_seat_map_of_a_sold_out_class_asks_before_a_watch(env, booker):
    env.gateway.seat_inventories.return_value = {"layoutReference": True, "inventories": []}
    args = {"trainKey": "T1", "seatClass": "special"}
    text = tool_text(call_tool(env, booker, "get_seat_map", args))
    assert "사용자에게 확인을 받고 start_watch" in text


# ---------------------------------------------------------------- 결과 문구


def test_stopping_nothing_says_there_was_nothing_to_stop(env, booker):
    env.gateway.status.return_value = {"running": None, "scheduled": None, "pending": []}
    env.gateway.cancel_search.return_value = {"stopped": False, "unscheduled": False}
    notes = call_tool(env, booker, "stop_watch")["structuredContent"]["notes"]
    assert notes == ["멈출 감시가 없어요. 진행 중이거나 시작 예정인 감시가 없었어요."]


def test_a_watch_that_would_not_stop_is_not_called_absent(env, booker):
    env.gateway.status.return_value = {"running": {"depDate": "20261019"}, "scheduled": None}
    env.gateway.cancel_search.return_value = {"stopped": False, "unscheduled": False}
    notes = call_tool(env, booker, "stop_watch")["structuredContent"]["notes"]
    assert len(notes) == 1
    assert "멈추지 못했어요" in notes[0]
    assert "get_status" in notes[0]


def test_a_cancelled_reservation_says_which_one(env, booker):
    held = {
        "trainInfo": "KTX 049 서울 → 부산 · 10월 19일(월) 07:00→09:40",
        "seatLabels": ["4호차 1C"],
        "seatClass": "special",
    }
    env.gateway.status.return_value = {"running": None, "scheduled": None, "pending": [held]}
    env.gateway.cancel_pending.return_value = {"cancelled": True, "pending": []}
    notes = call_tool(env, booker, "cancel_reservation")["structuredContent"]["notes"]
    assert notes[0] == "취소한 예약: KTX 049 서울 → 부산 · 10월 19일(월) 07:00→09:40 특실 4호차 1C"


def test_a_booking_note_says_what_was_held(env, booker):
    held = {"trainInfo": "KTX 043 서울 → 부산", "seatLabels": ["2호차 8C"], "seatClass": "special"}
    env.gateway.reserve_designated.return_value = {"reserved": True, "pending": [held]}
    said = "KTX 043 특실 2호차 8C 1석을 예약해요"
    notes = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said})[
        "structuredContent"
    ]["notes"]
    assert notes[0] == "잡은 좌석: KTX 043 서울 → 부산 특실 2호차 8C"


def test_the_status_reads_the_watch_aloud(env, booker):
    env.gateway.status.return_value = {
        "running": {
            "depDate": "20261019",
            "depTime": "0700",
            "srcLocate": "서울",
            "dstLocate": "부산",
            "selectedTrains": ["013", "015"],
            "trainLabels": {"013": "KTX 013", "015": "KTX 015"},
            "health": "healthy",
            "lastCheckedAt": None,
        },
        "scheduled": {
            "startAt": "2026-10-13T07:00:00+09:00",
            "search": {"depDate": "20261020", "srcLocate": "서울", "dstLocate": "대전"},
        },
        "pending": [],
    }
    notes = call_tool(env, booker, "get_status")["structuredContent"]["notes"]
    assert "감시 내용: 10월 19일 서울→부산 · 07:00 이후 출발 · 열차 KTX 013, KTX 015" in notes
    assert "예약된 감시 내용: 10월 20일 서울→대전" in notes


def test_the_watch_note_names_the_train_the_apps_label_leaves_out(env, booker):
    # Production labels are the times and the name only ("07:27→10:03 KTX").
    env.gateway.status.return_value = {
        "running": {
            "depDate": "20261021",
            "srcLocate": "서울",
            "dstLocate": "부산",
            "selectedTrains": ["013", "30", "10"],
            "trainLabels": {
                "013": "07:27→10:03 KTX",
                "30": "09:30→12:30 KTX",
                "10": "07:27→10:03 KTX",
            },
            "health": "healthy",
            "lastCheckedAt": None,
        },
        "pending": [],
    }
    notes = call_tool(env, booker, "get_status")["structuredContent"]["notes"]
    assert (
        "감시 내용: 10월 21일 서울→부산 · "
        "열차 013 07:27→10:03 KTX, 30 09:30→12:30 KTX, 10 07:27→10:03 KTX"
    ) in notes


def test_a_started_watch_answers_in_start_watchs_words(env, booker):
    env.gateway.listed_trains.return_value = ["013"]
    env.gateway.start_search.return_value = {
        "started": True,
        "running": {
            "depDate": "20261101",
            "depTime": "0700",
            "maxDepTime": "0800",
            "srcLocate": "서울",
            "dstLocate": "부산",
            "selectedTrains": ["013"],
            "specialInfoShow": "GENERAL_ONLY",
            "seatStrategy": "consecutive",
            "passengerCount": 1,
        },
    }
    result = call_tool(
        env,
        booker,
        "start_watch",
        watch("11월 1일 서울→부산 KTX 013 취소표를 감시해요"),
        summary=False,
    )
    running = result["structuredContent"]["running"]
    assert running["date"] == "2026-11-01"
    assert (running["from"], running["to"], running["earliest"], running["latest"]) == (
        "서울",
        "부산",
        "07:00",
        "08:00",
    )
    assert running["seat"] == "일반실만"
    assert running["seatStrategy"] == "연속 좌석"
    assert not {"depDate", "srcLocate", "specialInfoShow"} & running.keys()
    assert result["structuredContent"]["notes"] == [
        "감시를 시작했어요: 11월 1일 서울→부산 · 07:00~08:00 출발 · 열차 013"
    ]


def test_a_missing_start_time_names_what_to_write(env, booker):
    env.gateway.listed_trains.return_value = ["013"]
    said = "11월 1일 서울→부산 KTX 013 취소표를 10월 30일 07:00에 감시해요"
    arguments = watch(said, start_at="2026-10-30T07:00:00+09:00")
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert result["isError"] is True
    assert "감시를 시작할 시각과 날짜·구간·열차 번호(예:" in tool_text(result)
    assert ")를 사실대로" in tool_text(result)


# ---------------------------------------------------------------- summary 가 실제 호출과 맞는지(경계)


def watch(said, **more):
    arguments = {**TRIP, "date": "2026-11-01", "trains": ["013"], "summary": said, **more}
    return arguments


@pytest.mark.parametrize(
    ("said", "why"),
    [
        (
            "11월 1일 서울→부산 KTX 015(13:00 출발) 취소표를 감시해요",
            "열차 번호는 시각 속 13 이 아니에요",
        ),
        ("11월 1일 서울→부산 KTX 0130 취소표를 감시해요", "013 은 0130 이 아니에요"),
        ("11/15 서울→부산 KTX 013 취소표를 감시해요", "11/1 은 11/15 가 아니에요"),
        ("11월 15일 서울→부산 KTX 013 취소표를 감시해요", "1일은 15일이 아니에요"),
        ("11월 1일 서울→서대전 KTX 013 취소표를 감시해요", "부산이 아니라 서대전"),
        ("11월 1일 부산→서울 KTX 013 취소표를 감시해요", "방향이 반대"),
    ],
)
def test_a_watch_sentence_that_only_looks_like_the_call_is_refused(env, booker, said, why):
    result = call_tool(env, booker, "start_watch", watch(said), summary=False)
    assert result["isError"] is True, why
    env.gateway.start_search.assert_not_called()


def test_a_numeric_train_number_is_not_found_in_the_date(env, booker):
    arguments = watch("10월 19일 서울→부산 KTX 015 취소표를 감시해요", date="2026-10-19")
    arguments["trains"] = ["019"]
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert result["isError"] is True


@pytest.mark.parametrize(
    "said",
    [
        "11월 1일 서울→부산 KTX 013 취소표를 감시해요",
        "2026-11-01 서울에서 부산 KTX 13 취소표를 감시해요",
        "11/01 서울→부산 KTX 013 취소표를 감시해요",
        "11월 01일 서울 → 부산 KTX 0013 취소표를 감시해요",
    ],
)
def test_a_watch_sentence_may_be_written_several_ways(env, booker, said):
    env.gateway.listed_trains.return_value = ["013"]
    env.gateway.start_search.return_value = {"started": True}
    result = call_tool(env, booker, "start_watch", watch(said), summary=False)
    assert not result.get("isError"), tool_text(result)


@pytest.mark.parametrize(
    ("to", "said"),
    [("울산(통도사)", "서울→울산"), ("여수EXPO", "서울→여수엑스포")],
)
def test_a_station_may_be_said_without_its_parenthesis_or_by_its_common_name(env, booker, to, said):
    env.gateway.listed_trains.return_value = ["013"]
    env.gateway.start_search.return_value = {"started": True}
    arguments = watch(f"11월 1일 {said} KTX 013 취소표를 감시해요", to=to)
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert not result.get("isError"), tool_text(result)


def test_a_watch_sentence_cannot_borrow_payment_waiting_for_a_waitlist(env, booker):
    said = "11월 1일 서울→부산 KTX 013 취소표를 감시하고 잡으면 결제 대기로 둬요"
    result = call_tool(env, booker, "start_watch", watch(said, waitlist=True), summary=False)
    assert result["isError"] is True
    assert "예약 대기" in tool_text(result)


def test_a_watch_sentence_that_says_waitlist_when_it_is_not_one_is_refused(env, booker):
    said = "11월 1일 서울→부산 KTX 013 예약 대기를 신청해요"
    result = call_tool(env, booker, "start_watch", watch(said), summary=False)
    assert result["isError"] is True
    assert "waitlist" in tool_text(result)


@pytest.mark.parametrize(
    "said",
    [
        "KTX 043 특실 12호차 18C 1석을 예약해요",
        "KTX 043 특실 2호차 18C 1석을 예약해요",
        "KTX 043 특실 8C 2호차 1석을 예약해요",
    ],
)
def test_a_seat_must_follow_its_own_car_as_a_whole_label(env, booker, said):
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said}, summary=False)
    assert result["isError"] is True
    env.gateway.reserve_designated.assert_not_called()


def test_two_cars_with_swapped_seats_are_refused(env, booker):
    arguments = {
        **RESERVE,
        "passengers": 2,
        "seats": [
            {"carNo": 2, "seatNo": "2-9D", "label": "9D"},
            {"carNo": 3, "seatNo": "3-8C", "label": "8C"},
        ],
        "summary": "KTX 043 특실 2호차 8C, 3호차 9D 2석을 예약해요",
    }
    result = call_tool(env, booker, "reserve_seat", arguments, summary=False)
    assert result["isError"] is True


def test_a_numbered_seat_needs_to_be_said_as_a_number_of_seat(env, booker):
    one = {"carNo": 1, "seatNo": "1-7", "label": "7"}
    arguments = {**RESERVE, "seatClass": "general", "carNo": 1, "seats": [one]}
    wrong = call_tool(
        env, booker, "reserve_seat", {**arguments, "summary": "1호차 17번"}, summary=False
    )
    assert wrong["isError"] is True
    env.gateway.reserve_designated.return_value = {"reserved": True, "pending": []}
    right = call_tool(
        env, booker, "reserve_seat", {**arguments, "summary": "무궁화호 1호차 7번"}, summary=False
    )
    assert not right.get("isError"), tool_text(right)


def test_a_reservation_sentence_for_another_train_than_the_key_is_refused(env, booker):
    env.gateway.reserved_train.return_value = {"trainNo": "043", "depDate": "20261013"}
    said = "KTX 999 10월 20일 서울→부산 특실 2호차 8C 1석을 예약해요"
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said}, summary=False)
    assert result["isError"] is True
    assert "043" in tool_text(result)
    env.gateway.reserve_designated.assert_not_called()


def test_a_reservation_sentence_for_the_keys_train_goes_through(env, booker):
    env.gateway.reserved_train.return_value = {"trainNo": "043", "depDate": "20261013"}
    env.gateway.reserve_designated.return_value = {"reserved": True, "pending": []}
    said = "KTX 043 10월 13일 서울→부산 특실 2호차 8C 1석을 예약해요"
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said}, summary=False)
    assert not result.get("isError"), tool_text(result)


# ---------------------------------------------------------------- 멈추기·취소 흐름


def test_stopping_goes_ahead_when_the_status_cannot_be_read(env, booker):
    env.gateway.status.side_effect = RuntimeError("redis down")
    env.gateway.cancel_search.return_value = {"stopped": True, "unscheduled": False}
    result = call_tool(env, booker, "stop_watch")
    assert not result.get("isError"), tool_text(result)
    env.gateway.cancel_search.assert_called_once()


def test_a_scheduled_watch_that_was_dropped_is_said_to_be_dropped(env, booker):
    env.gateway.cancel_search.return_value = {"stopped": False, "unscheduled": True}
    notes = call_tool(env, booker, "stop_watch")["structuredContent"]["notes"]
    assert notes == ["시작 예정이던 감시를 취소했어요."]


def test_cancelling_goes_ahead_when_the_status_cannot_be_read(env, booker):
    env.gateway.status.side_effect = RuntimeError("redis down")
    env.gateway.cancel_pending.return_value = {"cancelled": True, "pending": []}
    result = call_tool(env, booker, "cancel_reservation")
    assert not result.get("isError"), tool_text(result)
    assert result["structuredContent"]["notes"] == [
        "결제 대기 예약을 취소하고 좌석을 돌려보냈어요."
    ]


def test_a_partial_cancel_names_only_what_was_given_back(env, booker):
    first = {"reservationId": "A", "trainInfo": "KTX 049", "seatLabels": ["4호차 1C"]}
    second = {"reservationId": "B", "trainInfo": "KTX 051", "seatLabels": ["5호차 2A"]}
    env.gateway.status.return_value = {
        "running": None,
        "scheduled": None,
        "pending": [first, second],
    }
    env.gateway.cancel_pending.return_value = {"cancelled": True, "pending": [second]}
    notes = call_tool(env, booker, "cancel_reservation")["structuredContent"]["notes"]
    assert notes[0] == "취소한 예약: KTX 049 4호차 1C"
    assert "KTX 051" not in notes[0]
    assert "1건 남아 있어요" in notes[1]


def test_a_watch_with_missing_parts_is_read_without_none_or_dangling_arrows(env, booker):
    env.gateway.status.return_value = {
        "running": {
            "depDate": "20261019",
            "depTime": "0000",
            "srcLocate": None,
            "dstLocate": "부산",
        },
        "scheduled": None,
        "pending": [],
    }
    notes = call_tool(env, booker, "get_status")["structuredContent"]["notes"]
    assert "감시 내용: 10월 19일" in notes


# ---------------------------------------------------------------- 문장 속 숫자가 열차 번호를 가리지 않는다


@pytest.mark.parametrize(
    "said",
    [
        "11월 1일 서울→부산 KTX 013 일반실 우선 취소표를 감시해요",
        "11월 1일 서울→부산 KTX 013 일반석 우선 감시해요",
        "11월 1일 서울→부산 KTX 013 월요일 출발 감시해요",
        "11월 1일 서울→부산 KTX 013/015 취소표를 감시해요",
    ],
)
def test_words_after_the_train_number_do_not_eat_it(env, booker, said):
    env.gateway.listed_trains.return_value = ["013", "015"]
    env.gateway.start_search.return_value = {"started": True}
    arguments = watch(said)
    arguments["trains"] = ["013", "015"] if "015" in said else ["013"]
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert not result.get("isError"), tool_text(result)


@pytest.mark.parametrize(
    ("trains", "said"),
    [
        (["001"], "11월 1일 서울→부산 KTX 003 1석 감시해요"),
        (["002"], "11월 1일 서울→부산 KTX 005 2명 감시해요"),
        (["002"], "11월 1일 서울→부산 KTX 015 2편 감시해요"),
        (["007"], "11월 1일 서울→부산 KTX 013 7시 출발 감시해요"),
    ],
)
def test_counts_and_clock_times_are_not_train_numbers(env, booker, trains, said):
    arguments = {**watch(said), "trains": trains}
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert result["isError"] is True
    env.gateway.start_search.assert_not_called()


def test_a_station_name_with_a_digit_is_not_a_train_number(env, booker):
    arguments = {**watch("11월 1일 서울→인천공항T1 KTX 003 감시해요"), "to": "인천공항T1"}
    arguments["trains"] = ["001"]
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert result["isError"] is True


def test_a_reservation_sentence_needs_the_train_beyond_the_seat_and_head_count(env, booker):
    env.gateway.reserved_train.return_value = {"trainNo": "009", "depDate": "20261101"}
    said = "11월 1일 KTX 043 일반실 2호차 8C 1석을 예약해요"
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said}, summary=False)
    assert result["isError"] is True


def test_a_reservation_sentence_with_a_class_word_still_finds_the_train(env, booker):
    env.gateway.reserved_train.return_value = {"trainNo": "043", "depDate": "20261101"}
    env.gateway.reserve_designated.return_value = {"reserved": True, "pending": []}
    said = "11월 1일 KTX 043 일반실 2호차 8C 1석을 예약해요"
    arguments = {**RESERVE, "seatClass": "general", "summary": said}
    result = call_tool(env, booker, "reserve_seat", arguments, summary=False)
    assert not result.get("isError"), tool_text(result)


def test_a_seat_number_is_not_a_count_of_seats(env, booker):
    one = {"carNo": 1, "seatNo": "1-1", "label": "1"}
    arguments = {
        **RESERVE,
        "carNo": 1,
        "seats": [one],
        "summary": "무궁화호 1호차 3번 좌석 1석을 예약해요",
    }
    assert call_tool(env, booker, "reserve_seat", arguments, summary=False)["isError"] is True


def test_a_bare_name_that_fits_two_stations_names_neither(env, booker):
    arguments = {**watch("11월 1일 서울→판교 KTX 013 감시해요"), "to": "판교(충남)"}
    assert call_tool(env, booker, "start_watch", arguments, summary=False)["isError"] is True
    env.gateway.listed_trains.return_value = ["013"]
    env.gateway.start_search.return_value = {"started": True}
    named = {**watch("11월 1일 서울→판교(충남) KTX 013 감시해요"), "to": "판교(충남)"}
    assert not call_tool(env, booker, "start_watch", named, summary=False).get("isError")


def test_a_stop_that_ran_is_not_an_error_when_the_status_cannot_tell_if_there_was_a_watch(
    env, booker
):
    env.gateway.cancel_search.return_value = {"stopped": False, "unscheduled": False}
    env.gateway.status.side_effect = RuntimeError("redis down")
    result = call_tool(env, booker, "stop_watch")
    assert not result.get("isError"), tool_text(result)
    assert "멈추지 못했어요" in result["structuredContent"]["notes"][0]


def test_a_reservation_without_an_id_is_not_called_cancelled_while_some_are_left(env, booker):
    nameless = {"trainInfo": "KTX 049", "seatLabels": ["4호차 1C"]}
    kept = {"reservationId": "B", "trainInfo": "KTX 051", "seatLabels": ["5호차 2A"]}
    env.gateway.status.return_value = {
        "running": None,
        "scheduled": None,
        "pending": [nameless, kept],
    }
    env.gateway.cancel_pending.return_value = {"cancelled": True, "pending": [kept]}
    notes = call_tool(env, booker, "cancel_reservation")["structuredContent"]["notes"]
    assert notes[0].startswith("일부만 취소했어요")


def test_the_sentence_asks_for_the_parts_it_is_checked_on():
    reserve = mcp.TOOLS["reserve_seat"][0]["inputSchema"]["properties"]["summary"]["description"]
    assert "열차 번호" in reserve
    assert "호차" in reserve
    watch_text = mcp.TOOLS["start_watch"][0]["inputSchema"]["properties"]["summary"]["description"]
    assert "출발역→도착역" in watch_text


@pytest.mark.parametrize(
    "said",
    [
        "11월 1일 서울→부산 KTX 013 매진이라 취소표를 감시해요",
        "11월 1일 서울→부산 KTX 013 시간대를 넓혀 감시해요",
        "11월 1일 서울→부산 KTX 013 석이 나면 감시해요",
        "11월 1일 서울→부산 KTX 013 인원 1명 감시해요",
    ],
)
def test_a_word_that_starts_like_a_unit_does_not_eat_the_train_number(env, booker, said):
    env.gateway.listed_trains.return_value = ["013"]
    env.gateway.start_search.return_value = {"started": True}
    result = call_tool(env, booker, "start_watch", watch(said), summary=False)
    assert not result.get("isError"), tool_text(result)


def test_a_key_with_no_date_still_finds_the_train_past_the_sentence_date(env, booker):
    env.gateway.reserved_train.return_value = {"trainNo": "001", "depDate": ""}
    said = "11월 1일 KTX 043 2호차 8C 1석을 예약해요"
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said}, summary=False)
    assert result["isError"] is True


def test_a_reversed_stretch_says_it_is_the_order(env, booker):
    arguments = watch("11월 1일 부산→서울 KTX 013 감시해요")
    text = tool_text(call_tool(env, booker, "start_watch", arguments, summary=False))
    assert "서울→부산 순서" in text


@pytest.mark.parametrize(("days", "hinted"), [(60, False), (61, True)])
def test_the_not_on_sale_hint_starts_past_the_days_korail_sells(env, booker, days, hinted):
    env.gateway.list_trains.side_effect = MiniAppError("열차 목록을 불러오지 못했어요.", 502)
    day = (TODAY + datetime.timedelta(days=days)).isoformat()
    text = tool_text(call_tool(env, booker, "find_trains", {**TRIP, "date": day}))
    assert ("두 달 앞까지만" in text) is hinted


@pytest.mark.parametrize(
    ("window", "said"),
    [
        ({"depTime": "0700", "maxDepTime": "0900"}, "07:00~09:00 출발"),
        ({"depTime": "0700", "maxDepTime": "2400"}, "07:00 이후 출발"),
        ({"depTime": "0000", "maxDepTime": "0900"}, "09:00 이전 출발"),
    ],
)
def test_the_status_says_the_whole_search_window(env, booker, window, said):
    env.gateway.status.return_value = {
        "running": {"depDate": "20261019", "srcLocate": "서울", "dstLocate": "부산", **window},
        "scheduled": None,
        "pending": [],
    }
    notes = call_tool(env, booker, "get_status")["structuredContent"]["notes"]
    assert f"감시 내용: 10월 19일 서울→부산 · {said}" in notes


# ---------------------------------------------------------------- 멈추기·취소의 summary 도 실제와 맞는지

WATCHING_BUSAN = {
    "running": {"depDate": "20261019", "srcLocate": "서울", "dstLocate": "부산"},
    "scheduled": None,
    "pending": [],
}


@pytest.mark.parametrize(
    ("said", "missing"),
    [
        ("진행 중인 감시를 멈춰요", "10월 19일"),
        ("10월 19일 서울→대구 감시를 멈춰요", "부산"),
        ("10월 20일 서울→부산 감시를 멈춰요", "10월 19일"),
    ],
)
def test_a_stop_sentence_must_name_the_watch_it_stops(env, booker, said, missing):
    env.gateway.status.return_value = WATCHING_BUSAN
    result = call_tool(env, booker, "stop_watch", {"summary": said}, summary=False)
    assert result["isError"] is True
    assert missing in tool_text(result)
    env.gateway.cancel_search.assert_not_called()


def test_a_stop_sentence_names_the_scheduled_watch_too(env, booker):
    env.gateway.status.return_value = {
        **WATCHING_BUSAN,
        "scheduled": {"search": {"depDate": "20261020", "srcLocate": "서울", "dstLocate": "대전"}},
    }
    said = "10월 19일 서울→부산 감시를 멈춰요"
    result = call_tool(env, booker, "stop_watch", {"summary": said}, summary=False)
    assert "10월 20일" in tool_text(result)
    said = "10월 19일 서울→부산 감시와 10월 20일 서울→대전 예약된 감시를 멈춰요"
    result = call_tool(env, booker, "stop_watch", {"summary": said}, summary=False)
    assert not result.get("isError"), tool_text(result)


def test_a_true_stop_sentence_goes_through(env, booker):
    env.gateway.status.return_value = WATCHING_BUSAN
    said = "진행 중인 10/19 서울에서 부산 가는 취소표 감시를 멈춰요"
    result = call_tool(env, booker, "stop_watch", {"summary": said}, summary=False)
    assert not result.get("isError"), tool_text(result)
    env.gateway.cancel_search.assert_called_once()


def test_with_nothing_to_stop_any_sentence_goes_through(env, booker):
    env.gateway.cancel_search.return_value = {"stopped": False, "unscheduled": False}
    said = "진행 중인 감시를 멈춰요"
    result = call_tool(env, booker, "stop_watch", {"summary": said}, summary=False)
    assert not result.get("isError"), tool_text(result)


TWO_HELD = {
    "running": None,
    "scheduled": None,
    "pending": [
        {"reservationId": "A", "trainInfo": "KTX 049 서울 → 부산 · 10월 19일(월) 07:00→09:40"},
        {"reservationId": "B", "trainInfo": "KTX 051 서울 → 부산 · 10월 19일(월) 08:00→10:40"},
    ],
}


def test_a_cancel_sentence_must_name_every_reservation_it_gives_back(env, booker):
    env.gateway.status.return_value = TWO_HELD
    said = "결제를 기다리는 10월 19일 KTX 049 예약을 취소해요"
    result = call_tool(env, booker, "cancel_reservation", {"summary": said}, summary=False)
    assert result["isError"] is True
    text = tool_text(result)
    assert "051" in text
    assert "모두 함께 취소" in text
    env.gateway.cancel_pending.assert_not_called()


def test_a_cancel_sentence_with_every_train_goes_through(env, booker):
    env.gateway.status.return_value = TWO_HELD
    said = "결제를 기다리는 10월 19일 KTX 049(07:00), KTX 51 예약 2건을 취소해요"
    result = call_tool(env, booker, "cancel_reservation", {"summary": said}, summary=False)
    assert not result.get("isError"), tool_text(result)
    env.gateway.cancel_pending.assert_called_once()


def test_a_cancel_sentence_does_not_find_the_train_in_the_clock(env, booker):
    env.gateway.status.return_value = {
        **TWO_HELD,
        "pending": [{"reservationId": "A", "trainInfo": "KTX 007 서울 → 부산"}],
    }
    said = "07:00 출발 열차 예약을 취소해요"
    result = call_tool(env, booker, "cancel_reservation", {"summary": said}, summary=False)
    assert result["isError"] is True


# ---------------------------------------------------------------- 등급·인원·구간·시작 시각


def test_a_special_seat_sentence_must_say_special(env, booker):
    said = "KTX 043 2호차 8C 1석을 예약해요"
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said}, summary=False)
    assert result["isError"] is True
    assert "특실" in tool_text(result)
    env.gateway.reserve_designated.assert_not_called()


def test_a_general_seat_sentence_cannot_say_special(env, booker):
    said = "KTX 043 특실 2호차 8C 1석을 예약해요"
    arguments = {**RESERVE, "seatClass": "general", "summary": said}
    result = call_tool(env, booker, "reserve_seat", arguments, summary=False)
    assert result["isError"] is True
    assert "일반실" in tool_text(result)


def test_a_sentence_with_another_head_count_is_refused(env, booker):
    said = "KTX 043 특실 2호차 8C 2석을 예약해요"
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said}, summary=False)
    assert result["isError"] is True
    assert "passengers" in tool_text(result)


def test_a_reservation_sentence_names_the_keys_stretch(env, booker):
    env.gateway.reserved_train.return_value = {
        "trainNo": "043",
        "depDate": "20261101",
        "from": "서울",
        "to": "부산",
    }
    said = "11월 1일 KTX 043 서울→대전 특실 2호차 8C 1석을 예약해요"
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said}, summary=False)
    assert result["isError"] is True
    assert "부산" in tool_text(result)
    env.gateway.reserve_designated.return_value = {"reserved": True, "pending": []}
    said = "11월 1일 KTX 043 서울→부산 특실 2호차 8C 1석을 예약해요"
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said}, summary=False)
    assert not result.get("isError"), tool_text(result)


@pytest.mark.parametrize(
    ("seat", "said", "refused"),
    [
        ("special", "11월 1일 서울→부산 KTX 013 취소표를 감시해요", True),
        ("special", "11월 1일 서울→부산 KTX 013 특실 취소표를 감시해요", False),
        ("general", "11월 1일 서울→부산 KTX 013 특실 취소표를 감시해요", True),
        ("any", "11월 1일 서울→부산 KTX 013 일반실만 감시해요", True),
        ("any", "11월 1일 서울→부산 KTX 013 일반실이든 특실이든 감시해요", False),
    ],
)
def test_a_watch_sentence_says_the_class_it_watches(env, booker, seat, said, refused):
    env.gateway.listed_trains.return_value = ["013"]
    result = call_tool(env, booker, "start_watch", watch(said, seat=seat), summary=False)
    assert bool(result.get("isError")) is refused, tool_text(result)


@pytest.mark.parametrize(
    ("start_at", "said", "refused"),
    [
        ("2026-10-30T07:00:00+09:00", "11월 1일 서울→부산 KTX 013 감시를 시작해요", True),
        ("2026-10-30T07:00:00+09:00", "11월 1일 서울→부산 KTX 013 07:00부터 감시해요", True),
        ("2026-10-30T07:00:00+09:00", "10월 30일 07:00부터 11월 1일 서울→부산 KTX 013 감시", False),
        (
            "2026-10-30T07:00:00+09:00",
            "10월 30일 오전 7시에 시작해 11월 1일 서울→부산 KTX 013 감시",
            False,
        ),
        ("2026-10-30T07:00:00+09:00", "10월 30일 오후 7시에 11월 1일 서울→부산 KTX 013 감시", True),
        (
            "2026-10-30T19:30:00+09:00",
            "10월 30일 오후 7시 반부터 11월 1일 서울→부산 KTX 013 감시",
            False,
        ),
        ("2026-10-30T19:30:00+09:00", "10월 30일 7시 반에 11월 1일 서울→부산 KTX 013 감시", True),
        ("2026-10-31T22:00:00Z", "11월 1일 07:00부터 서울→부산 KTX 013 감시해요", False),
    ],
)
def test_a_scheduled_watch_sentence_says_when_it_begins(env, booker, start_at, said, refused):
    env.gateway.listed_trains.return_value = ["013"]
    arguments = watch(said, start_at=start_at)
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert bool(result.get("isError")) is refused, tool_text(result)
    assert env.gateway.schedule_search.called is not refused


def test_the_start_date_is_not_taken_for_a_train_number(env, booker):
    arguments = watch(
        "10월 13일 07:00에 11월 1일 서울→부산 KTX 015 감시해요",
        start_at="2026-10-13T07:00:00+09:00",
    )
    arguments["trains"] = ["013"]
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert result["isError"] is True
    assert "013" in tool_text(result)


# ---------------------------------------------------------------- 사람들이 쓰는 좌석·인원 표기


@pytest.mark.parametrize(
    ("seats", "said"),
    [
        ([{"carNo": 2, "seatNo": "2-8C", "label": "8C"}], "KTX 043 특실 8C(2호차) 1석을 예약해요"),
        ([{"carNo": 2, "seatNo": "2-8C", "label": "8C"}], "KTX 043 특실 2호차 8 C 1석을 예약해요"),
        (
            [
                {"carNo": 2, "seatNo": "2-8C", "label": "8C"},
                {"carNo": 2, "seatNo": "2-8D", "label": "8D"},
            ],
            "KTX 043 특실 2호차 8C·D 2석을 예약해요",
        ),
        (
            [
                {"carNo": 2, "seatNo": "2-8C", "label": "8C"},
                {"carNo": 2, "seatNo": "2-8D", "label": "8D"},
            ],
            "KTX 043 특실 2호차 8C, D 2 명 예약해요",
        ),
    ],
)
def test_seats_may_be_written_the_way_people_do(env, booker, seats, said):
    env.gateway.reserve_designated.return_value = {"reserved": True, "pending": []}
    arguments = {**RESERVE, "passengers": len(seats), "seats": seats, "summary": said}
    result = call_tool(env, booker, "reserve_seat", arguments, summary=False)
    assert not result.get("isError"), tool_text(result)


def test_a_bracketed_seat_is_not_read_in_the_car_before_it(env, booker):
    arguments = {
        **RESERVE,
        "passengers": 2,
        "seats": [
            {"carNo": 2, "seatNo": "2-1A", "label": "1A"},
            {"carNo": 2, "seatNo": "2-8C", "label": "8C"},
        ],
        "summary": "KTX 043 특실 2호차 1A, 8C(3호차) 2석을 예약해요",
    }
    result = call_tool(env, booker, "reserve_seat", arguments, summary=False)
    assert result["isError"] is True
    assert "2호차 8C" in tool_text(result)


def test_a_spaced_head_count_does_not_hide_the_train_number(env, booker):
    arguments = {**watch("11월 1일 서울→부산 KTX 013 2 명 감시해요"), "passengers": 2}
    arguments["trains"] = ["002"]
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert result["isError"] is True


def test_train_numbers_written_like_a_date_are_not_the_date(env, booker):
    env.gateway.listed_trains.return_value = ["010", "013"]
    arguments = {
        **watch("10월 13일 서울→부산 KTX 010/013 취소표를 감시해요"),
        "date": "2026-10-13",
        "trains": ["010", "013"],
    }
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert not result.get("isError"), tool_text(result)


# ---------------------------------------------------------------- 검토에서 찾은 거짓 통과


@pytest.mark.parametrize(
    "said",
    [
        # 열차가 07:00 에 떠나지만 감시는 21:00 부터라고 말한다.
        "11월 1일 서울→부산 KTX 013(07:00 출발) 취소표를 10월 30일 21:00부터 감시",
        "11월 1일 07:00 이후 서울→부산 KTX 013 을 10월 30일 오후 9시부터 감시",
        # 여행 날짜와 시작 날짜를 바꿔 쓴다.
        "10월 30일 서울→부산 KTX 013 취소표를 11월 1일 7시부터 감시",
        # 오후·밤·PM 의 7시는 07:00 이 아니다.
        "11월 1일 서울→부산 KTX 013 을 10월 30일 오후에 7시부터 감시",
        "11월 1일 서울→부산 KTX 013 을 10월 30일 7:00 PM부터 감시",
        "11월 1일 서울→부산 KTX 013 을 10월 30일 7시 반부터 감시",
    ],
)
def test_a_start_time_from_elsewhere_in_the_sentence_is_not_the_start(env, booker, said):
    env.gateway.listed_trains.return_value = ["013"]
    arguments = watch(said, start_at="2026-10-30T07:00:00+09:00")
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert result["isError"] is True, said
    env.gateway.schedule_search.assert_not_called()


@pytest.mark.parametrize(
    ("start_at", "said", "refused"),
    [
        ("2026-11-01T07:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 07:00부터 감시", False),
        ("2026-11-01T07:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 10월 31일 07:00부터", True),
        (
            "2026-10-30T07:00:00+09:00",
            "11월 1일 서울→부산 KTX 013, 10월 30일(목) 7시 00분부터",
            False,
        ),
        ("2026-10-30T12:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 10월 30일 밤 12시부터", True),
        ("2026-10-30T00:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 10월 30일 밤 12시부터", False),
    ],
)
def test_the_start_day_and_clock_edges(env, booker, start_at, said, refused):
    env.gateway.listed_trains.return_value = ["013"]
    result = call_tool(env, booker, "start_watch", watch(said, start_at=start_at), summary=False)
    assert bool(result.get("isError")) is refused, tool_text(result)


@pytest.mark.parametrize(
    "said",
    [
        "19일 KTX 049 예약을 취소해요",
        "10/19 KTX 049 예약을 취소해요",
        "2026-10-19 KTX 049 예약을 취소해요",
        "KTX 049 2호차 19번 좌석 취소",
    ],
)
def test_a_day_or_seat_number_does_not_stand_for_another_reservations_train(env, booker, said):
    env.gateway.status.return_value = {
        "running": None,
        "scheduled": None,
        "pending": [
            {"reservationId": "A", "trainInfo": "KTX 049 서울 → 부산 · 10월 19일(월) 07:00→09:40"},
            {"reservationId": "B", "trainInfo": "KTX 019 서울 → 부산 · 10월 19일(월) 06:00→08:40"},
        ],
    }
    result = call_tool(env, booker, "cancel_reservation", {"summary": said}, summary=False)
    assert result["isError"] is True
    assert "019" in tool_text(result)
    env.gateway.cancel_pending.assert_not_called()


def test_a_count_of_bookings_is_not_a_train(env, booker):
    env.gateway.status.return_value = {
        "running": None,
        "scheduled": None,
        "pending": [{"reservationId": "A", "trainInfo": "KTX 001 서울 → 부산"}],
    }
    said = "결제 대기 예약 1건을 취소해요"
    result = call_tool(env, booker, "cancel_reservation", {"summary": said}, summary=False)
    assert result["isError"] is True


def test_a_station_digit_is_not_asked_for_as_a_train(env, booker):
    env.gateway.status.return_value = {
        "running": None,
        "scheduled": None,
        "pending": [{"reservationId": "A", "trainInfo": "KTX 서울 → 인천공항T1"}],
    }
    said = "결제 대기 예약을 취소해요"
    result = call_tool(env, booker, "cancel_reservation", {"summary": said}, summary=False)
    assert not result.get("isError"), tool_text(result)


@pytest.mark.parametrize(
    "said",
    [
        "11월 1일 서울→부산 KTX 013 일반실 취소표 감시",
        "11월 1일 서울→부산 KTX 013 특실 아닌 일반실 감시",
    ],
)
def test_any_class_is_not_approved_as_one_class(env, booker, said):
    env.gateway.listed_trains.return_value = ["013"]
    result = call_tool(env, booker, "start_watch", watch(said), summary=False)
    assert result["isError"] is True


@pytest.mark.parametrize(
    ("seat_class", "said"),
    [
        ("special", "KTX 043 특실 아닌 일반실 2호차 8C 1석을 예약해요"),
        ("general", "KTX 043 일반실 말고 특실 2호차 8C 1석을 예약해요"),
    ],
)
def test_a_class_said_only_to_be_ruled_out_is_refused(env, booker, seat_class, said):
    arguments = {**RESERVE, "seatClass": seat_class, "summary": said}
    assert call_tool(env, booker, "reserve_seat", arguments, summary=False)["isError"] is True


@pytest.mark.parametrize(
    "said",
    [
        "11월 1일 서울→부산 KTX 013 1인 취소표 감시",
        "11월 1일 서울→부산 KTX 013 한 명 감시",
        "11월 1일 서울→부산 KTX 013 10명 감시",
    ],
)
def test_head_counts_in_other_words_must_match_too(env, booker, said):
    env.gateway.listed_trains.return_value = ["013"]
    arguments = {**watch(said), "passengers": 4}
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert result["isError"] is True
    assert "passengers" in tool_text(result)


def test_an_out_of_range_start_is_refused_not_an_error(env, booker):
    said = "11월 1일 서울→부산 KTX 013 감시"
    arguments = watch(said, start_at="9999-12-31T23:59:00-14:00")
    text = tool_text(call_tool(env, booker, "start_watch", arguments, summary=False))
    assert "처리 중 문제" not in text


def test_a_shared_row_in_brackets_puts_both_seats_in_the_car(env, booker):
    env.gateway.reserve_designated.return_value = {"reserved": True, "pending": []}
    arguments = {
        **RESERVE,
        "passengers": 2,
        "seats": [
            {"carNo": 2, "seatNo": "2-8C", "label": "8C"},
            {"carNo": 2, "seatNo": "2-8D", "label": "8D"},
        ],
        "summary": "KTX 043 특실 8C·D(2호차) 2석을 예약해요",
    }
    result = call_tool(env, booker, "reserve_seat", arguments, summary=False)
    assert not result.get("isError"), tool_text(result)


def test_a_train_number_before_a_copula_is_still_the_train(env, booker):
    env.gateway.reserved_train.return_value = {"trainNo": "049", "depDate": "20261101"}
    env.gateway.reserve_designated.return_value = {"reserved": True, "pending": []}
    said = "11월 1일 KTX 049인 열차 특실 2호차 8C 1석을 예약해요"
    result = call_tool(env, booker, "reserve_seat", {**RESERVE, "summary": said}, summary=False)
    assert not result.get("isError"), tool_text(result)


# ---------------------------------------------------------------- 2차 검토에서 찾은 거짓 통과


@pytest.mark.parametrize(
    ("seat", "said"),
    [
        ("any", "11월 1일 서울→부산 KTX 013 일반석 감시"),
        ("any", "11월 1일 서울→부산 KTX 013 모든 일반실 취소표 감시"),
        ("any", "11월 1일 서울→부산 KTX 013 일반실 취소표 감시, 좌석이 나면 언제든 잡아요"),
        ("special", "11월 1일 서울→부산 KTX 013 일반실 우선(없으면 특실) 감시"),
        ("special", "11월 1일 서울→부산 KTX 013 일반실 감시, 특실 아님"),
        ("special", "11월 1일 서울→부산 KTX 013 특실은 안 되고 일반실 감시"),
        ("general", "11월 1일 서울→부산 KTX 013 특실 우선(없으면 일반실) 감시"),
        ("general", "11월 1일 서울→부산 KTX 013 특석 감시"),
    ],
)
def test_a_class_spelled_or_qualified_otherwise_must_still_match(env, booker, seat, said):
    env.gateway.listed_trains.return_value = ["013"]
    result = call_tool(env, booker, "start_watch", watch(said, seat=seat), summary=False)
    assert result["isError"] is True, said


@pytest.mark.parametrize(
    "said",
    [
        "11월 1일 서울→부산 KTX 013 일반실 우선(없으면 특실) 감시",
        "11월 1일 서울→부산 KTX 013 일반실이든 특실이든 감시",
        "11월 1일 서울→부산 KTX 013 등급 상관없이 감시",
    ],
)
def test_any_class_said_as_either_goes_through(env, booker, said):
    env.gateway.listed_trains.return_value = ["013"]
    result = call_tool(env, booker, "start_watch", watch(said), summary=False)
    assert not result.get("isError"), tool_text(result)


@pytest.mark.parametrize(
    ("start_at", "said", "refused"),
    [
        ("2026-10-30T00:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 10월 30일 정오부터", True),
        ("2026-10-30T12:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 10월 30일 자정부터", True),
        ("2026-10-30T12:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 10월 30일 정오부터", False),
        (
            "2026-11-01T07:00:00+09:00",
            "11월 1일 07:00부터 출발하는 서울→부산 KTX 013 취소표를 10월 31일 21:00부터 감시",
            True,
        ),
        (
            "2026-11-01T07:00:00+09:00",
            "11월 1일 서울→부산 KTX 013 07:00부터 09:00까지 출발하는 열차를 지금부터 감시",
            True,
        ),
        ("2027-10-30T07:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 10월 30일 07:00부터", True),
        ("2026-10-30T07:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 10월 30일에 7시부터", False),
        # 여행이 11월이면 맨 "30일" 은 11월 30일로 읽힌다.
        ("2026-10-30T07:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 30일 7시부터 감시", True),
        ("2026-11-01T05:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 1일 5시부터 감시", False),
        ("2026-10-30T07:00:00+09:00", "11월 1일 서울→부산 KTX 013 을 10.30. 7시부터 감시", False),
        ("0001-01-01T00:00:00+14:00", "11월 1일 서울→부산 KTX 013 을 감시", True),
    ],
)
def test_the_start_is_the_watchs_own(env, booker, start_at, said, refused):
    env.gateway.listed_trains.return_value = ["013"]
    result = call_tool(env, booker, "start_watch", watch(said, start_at=start_at), summary=False)
    assert bool(result.get("isError")) is refused, tool_text(result)
    assert "처리 중 문제" not in tool_text(result)


@pytest.mark.parametrize(
    "said",
    [
        "11월 1일 서울→부산 KTX 013 표 1장 감시",
        "11월 1일 서울→부산 KTX 013 1자리 감시",
        "11월 1일 서울→부산 KTX 013 혼자 탈 좌석 감시",
        "11월 1일 서울→부산 KTX 013 어른 1 감시",
        "11월 1일 서울→부산 KTX 013 두 분 감시",
    ],
)
def test_head_counts_in_any_unit_must_match(env, booker, said):
    env.gateway.listed_trains.return_value = ["013"]
    result = call_tool(env, booker, "start_watch", {**watch(said), "passengers": 4}, summary=False)
    assert result["isError"] is True, said


@pytest.mark.parametrize(
    ("train", "said"),
    [
        ("002", "11월 1일 서울→부산 2번째 열차 감시"),
        ("005", "11월 1일 서울→부산 KTX 5 분 간격으로 감시"),
        ("010", "11월 1일 서울→부산 10 시 출발 감시"),
        ("007", "11월 1일 서울→부산 오전 7 시 출발 감시"),
    ],
)
def test_small_numbers_in_counts_and_times_are_not_trains(env, booker, train, said):
    arguments = {**watch(said), "trains": [train]}
    assert call_tool(env, booker, "start_watch", arguments, summary=False)["isError"] is True


def test_a_wrong_cancel_sentence_costs_no_railway_call(env, booker):
    env.gateway.status.return_value = {
        "running": None,
        "scheduled": None,
        "pending": [{"reservationId": "A", "trainInfo": "KTX 049 서울 → 부산"}],
    }
    for _ in range(11):
        result = call_tool(
            env, booker, "cancel_reservation", {"summary": "예약을 취소해요"}, summary=False
        )
        assert "049" in tool_text(result)
    assert "1분" not in tool_text(result)


# ---------------------------------------------------------------- 3차 검토에서 찾은 것


@pytest.mark.parametrize(
    ("seat", "said"),
    [
        ("any", "11월 1일 서울→부산 KTX 013 일반실 취소표 감시, 좌석이 없으면 계속 감시"),
        ("any", "11월 1일 서울→부산 KTX 013 일반실 취소표를 우선 감시"),
        ("any", "11월 1일 서울→부산 KTX 013 특실 우선(없으면 일반실) 감시"),
        ("special", "11월 1일 서울→부산 KTX 013 일반실 또는 특실 감시"),
        ("special", "11월 1일 서울→부산 KTX 013 특실, 일반실도 괜찮아요"),
    ],
)
def test_a_class_word_elsewhere_does_not_qualify_the_class(env, booker, seat, said):
    env.gateway.listed_trains.return_value = ["013"]
    result = call_tool(env, booker, "start_watch", watch(said, seat=seat), summary=False)
    assert result["isError"] is True, said


@pytest.mark.parametrize(
    "said",
    [
        "11월 1일 07:00부터의 서울→부산 KTX 013 취소표를 10월 31일 21:00부터 감시",
        "11월 1일 07:00부터 다니는 서울→부산 KTX 013 을 05:00부터 감시",
    ],
)
def test_the_last_time_with_from_is_the_start(env, booker, said):
    env.gateway.listed_trains.return_value = ["013"]
    arguments = watch(said, start_at="2026-11-01T07:00:00+09:00")
    assert call_tool(env, booker, "start_watch", arguments, summary=False)["isError"] is True


@pytest.mark.parametrize(
    ("start_at", "said"),
    [
        (None, "11월 1일 서울→부산 KTX 013 좌석 감시"),
        (
            "2026-10-30T07:00:00+09:00",
            "11월 1일 서울→부산 KTX 013 을 10월 30일 07:00부터 31일까지 감시",
        ),
    ],
)
def test_round_three_false_rejects_go_through(env, booker, start_at, said):
    env.gateway.listed_trains.return_value = ["013"]
    arguments = watch(said, **({"start_at": start_at} if start_at else {}))
    result = call_tool(env, booker, "start_watch", arguments, summary=False)
    assert not result.get("isError"), tool_text(result)
