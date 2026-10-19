"""
도구 8개가 돌려주는 structuredContent 의 모양 전체를 고정한다(== 비교).

gateway 응답 하나를 주고 결과를 통째로 비교하므로, 키 이름이나 빠진 필드의 기본값이 바뀌면
여기서 드러난다. 결과 모양을 바꾸기로 하면 아래 SHAPES 표의 기대값만 고치면 된다.
"""

import datetime

import pytest

from korail_bot.config.settings import settings
from korail_bot.mobile import mcp

from .agent_support import (
    BOOK_HINT,
    call_tool,
    cheap_setup,
    connect,
    make_env,
    signup,
)

DATE = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
TRIP = {"date": DATE, "from": "서울", "to": "부산"}
SEAT_ARGS = {"trainKey": "T1", "seatClass": "general", "passengers": 1}
RESERVE_ARGS = {
    "trainKey": "T1",
    "seatClass": "general",
    "passengers": 1,
    "carNo": 3,
    "seats": [{"carNo": 3, "seatNo": "3-7A", "label": "7A"}],
}
URL = settings.KORAIL_PAYMENT_URL
PAY_NOTE = (
    "좌석을 잡았어요(결제 대기). 결제는 직접 해 주세요: 결제 기한 안에 앱이나 "
    f"코레일({URL})에서 결제하지 않으면 예약이 취소돼요."
)
WATCHING = "감시 중이에요. 코레일을 계속 조회하고 있어요."
UNKNOWN_HEALTH = "감시 상태를 아직 확인하지 못했어요. 잠시 뒤 다시 확인해 주세요."
NOTHING_GOING = "진행 중인 감시도, 결제를 기다리는 예약도 없어요."
PARTLY_READ = "일부 호차는 조회하지 못했어요. 그 호차에 좌석이 없다는 뜻은 아니에요."
NO_SEATS = "예약 가능한 좌석이 없어요."
SOLD_OUT_NOTE = (
    "soldOut 은 일반실·특실이 모두 매진일 때만 true 예요. "
    "등급별 남은 자리는 generalAvailable·specialAvailable 로 봐요."
)
TRUNCATED_NOTE = "열차가 많아 일부만 보여요. earliest·latest 로 시간대를 좁혀 다시 찾아 주세요."
WAITLISTED = (
    "코레일 예약 대기를 신청했어요. 좌석을 잡은 것이 아니고, 감시도 시작하지 않았어요. "
    "앱이 10분 이상 간격으로 배정 여부를 확인해 알리고, get_status 와 앱 홈, 코레일 앱에서 볼 수 있어요."
)
NOTHING_TO_STOP = "멈출 감시가 없어요. 진행 중이거나 시작 예정인 감시가 없었어요."
STOPPED = "진행 중이던 감시를 멈췄어요."
WATCH_TEXT = "감시 내용: 10월 10일 서울→부산 · 07:00 이후 출발 · 열차 00101"
CANCELLED = "결제 대기 예약을 취소하고 좌석을 돌려보냈어요."
PARTLY_CANCELLED = (
    "일부만 취소했어요. 결제를 기다리는 예약이 1건 남아 있어요. "
    "남은 예약은 결제 기한 안에 결제하지 않으면 취소돼요."
)


def seat(car, label, column=None, free=True, family=""):
    entry = {"carNo": car, "seatNo": f"{car}-{label}", "label": label, "salePossible": free}
    if column:
        entry["column"] = column
    if family:
        entry["familyLabel"] = family
    return entry


RUNNING = {
    "depDate": "20261010",
    "srcLocate": "서울",
    "dstLocate": "부산",
    "depTime": "0700",
    "maxDepTime": "2400",
    "specialInfoShow": "GENERAL_ONLY",
    "passengerCount": 2,
    "seatStrategy": "consecutive",
    "selectedTrains": ["00101"],
    "startedAt": "2026-10-07T00:00:00+00:00",
    "health": "healthy",
    "lastCheckedAt": "2026-10-07T00:01:00+00:00",
}
# get_status 는 감시를 start_watch 인자와 같은 말로 돌려준다(앱 필드 이름·코드가 아니라).
PLAIN_RUNNING = {
    "date": "2026-10-10",
    "from": "서울",
    "to": "부산",
    "earliest": "07:00",
    "latest": "24:00",
    "seat": "일반실만",
    "passengers": 2,
    "seatStrategy": "연속 좌석",
    "trains": ["00101"],
    "startedAt": "2026-10-07T09:00:00+09:00",
    "health": "healthy",
    "lastCheckedAt": "2026-10-07T09:01:00+09:00",
}
PENDING = {
    "reservationId": "R1",
    "trainInfo": "KTX 00101 서울 → 부산",
    "expiresAt": "2026-10-10T01:10:00+00:00",
    "seatLabels": ["3호차 7A"],
    "seatClass": "general",
}

# (도구, 입력, gateway 메서드, gateway 응답, 기대 structuredContent)
SHAPES = [
    pytest.param(
        "find_trains",
        TRIP,
        "list_trains",
        {
            "trains": [
                {
                    "no": "00101",
                    "label": "07:00→09:41 KTX",
                    "dep_time": "070000",
                    "arr_time": "0941",
                    "name": "KTX",
                    "soldout": True,
                    "waitlistEligible": True,
                    "trainKey": "T1",
                    "generalAvailable": True,
                    "specialAvailable": True,
                }
            ],
            "truncated": True,
            "passengerCount": 1,
        },
        {
            "trains": [
                {
                    "trainKey": "T1",
                    "trainNo": "00101",
                    "name": "KTX",
                    "departs": "07:00",
                    "arrives": "09:41",
                    "generalAvailable": True,
                    "specialAvailable": True,
                    "soldOut": True,
                    "waitlistEligible": True,
                }
            ],
            "truncated": True,
            "notes": [SOLD_OUT_NOTE, TRUNCATED_NOTE],
        },
        id="find_trains-full",
    ),
    pytest.param(
        "find_trains",
        TRIP,
        "list_trains",
        {"trains": [{"no": "00103", "trainKey": "T3", "dep_time": "123"}]},
        {
            "trains": [
                {
                    "trainKey": "T3",
                    "trainNo": "00103",
                    "name": None,
                    "departs": "123",
                    "arrives": "",
                    "generalAvailable": False,
                    "specialAvailable": False,
                    "soldOut": False,
                    "waitlistEligible": False,
                }
            ],
            "truncated": False,
            "notes": [SOLD_OUT_NOTE],
        },
        id="find_trains-missing-fields",
    ),
    pytest.param(
        "get_seat_map",
        SEAT_ARGS,
        "seat_inventories",
        {
            "inventories": [
                {
                    "carNo": 3,
                    "seats": [
                        seat(3, "7A", "A", family="4인 동반석"),
                        seat(3, "7B", "B"),
                        seat(3, "7C", "C", free=False),
                        seat(3, "7D", "D"),
                    ],
                },
                {"carNo": 4, "seats": [seat(4, "1A", "A", free=False)]},
            ],
            "failedCars": [],
            "layoutReference": False,
        },
        {
            "trainKey": "T1",
            "seatClass": "general",
            "cars": [
                {
                    "carNo": 3,
                    "freeSeats": [
                        {"seatNo": "3-7A", "label": "7A", "position": "창가", "note": "4인 동반석"},
                        {"seatNo": "3-7B", "label": "7B", "position": "통로"},
                        {"seatNo": "3-7D", "label": "7D", "position": "창가"},
                    ],
                }
            ],
            "failedCars": [],
            "notes": [],
        },
        id="get_seat_map-full",
    ),
    pytest.param(
        "get_seat_map",
        SEAT_ARGS,
        "seat_inventories",
        {
            "inventories": [
                {"carNo": 5, "seats": [seat(5, "1")]},
                {"carNo": 6},
                {"carNo": 7, "seats": [seat(7, "1A", "A")]},
            ]
        },
        {
            "trainKey": "T1",
            "seatClass": "general",
            "cars": [
                {"carNo": 5, "freeSeats": [{"seatNo": "5-1", "label": "1"}]},
                {"carNo": 7, "freeSeats": [{"seatNo": "7-1A", "label": "1A"}]},
            ],
            "failedCars": [],
            "notes": [],
        },
        id="get_seat_map-missing-fields",
    ),
    pytest.param(
        "get_seat_map",
        SEAT_ARGS,
        "seat_inventories",
        {"inventories": [], "failedCars": []},
        {
            "trainKey": "T1",
            "seatClass": "general",
            "cars": [],
            "failedCars": [],
            "notes": [NO_SEATS],
        },
        id="get_seat_map-empty",
    ),
    pytest.param(
        "get_seat_map",
        SEAT_ARGS,
        "seat_inventories",
        {
            "inventories": [{"carNo": 1, "seats": [seat(1, "1A", "A", free=False)]}],
            "failedCars": [2],
        },
        {
            "trainKey": "T1",
            "seatClass": "general",
            "cars": [],
            "failedCars": [2],
            "notes": [PARTLY_READ],
        },
        id="get_seat_map-partly-read",
    ),
    pytest.param(
        "get_status",
        {},
        "status",
        {
            "running": RUNNING,
            "scheduled": {"startAt": "2026-10-09T22:00:00+00:00"},
            "pending": [PENDING],
        },
        {
            "running": PLAIN_RUNNING,
            "scheduled": {"startAt": "2026-10-10T07:00:00+09:00"},
            "pending": [{**PENDING, "expiresAt": "2026-10-10T10:10:00+09:00", "paymentUrl": URL}],
            "notes": [
                WATCHING,
                WATCH_TEXT,
                "좌석 조건: 일반실만, 2명, 연속 좌석",
                "정한 시각에 감시를 시작해요.",
                "결제를 기다리는 예약이 1건 있어요. 결제 기한 안에 사용자가 직접 결제해야 해요.",
            ],
        },
        id="get_status-full",
    ),
    pytest.param(
        "get_status",
        {},
        "status",
        {
            "running": {**RUNNING, "seatsHeld": 1, "seatsWanted": 2},
            "scheduled": None,
            "pending": [PENDING],
        },
        {
            "running": {**PLAIN_RUNNING, "seatsHeld": 1, "seatsWanted": 2},
            "scheduled": None,
            "pending": [{**PENDING, "expiresAt": "2026-10-10T10:10:00+09:00", "paymentUrl": URL}],
            "notes": [
                WATCHING,
                WATCH_TEXT,
                "좌석 조건: 일반실만, 2명, 연속 좌석",
                "결제를 기다리는 예약이 1건 있어요. 결제 기한 안에 사용자가 직접 결제해야 해요.",
                "2석 중 1석을 잡았고 나머지는 감시가 찾고 있어요.",
            ],
        },
        id="get_status-party-partly-held",
    ),
    pytest.param(
        "get_status",
        {},
        "status",
        {
            "running": {
                "health": "zzz",
                "specialInfoShow": "MYSTERY",
                "passengerCount": 3,
                "seatStrategy": "zigzag",
            },
            "scheduled": None,
            "pending": None,
        },
        {
            "running": {
                "health": "zzz",
                "seat": "MYSTERY",
                "passengers": 3,
                "seatStrategy": "zigzag",
            },
            "scheduled": None,
            "pending": [],
            "notes": [UNKNOWN_HEALTH, "좌석 조건: MYSTERY, 3명, zigzag"],
        },
        id="get_status-unknown-codes",
    ),
    pytest.param(
        "get_status",
        {},
        "status",
        {"running": None, "scheduled": None, "pending": []},
        {"running": None, "scheduled": None, "pending": [], "notes": [NOTHING_GOING]},
        id="get_status-nothing",
    ),
    pytest.param(
        "list_favourites",
        {},
        "favourites",
        [{"id": "f1", "name": "주말 부산", "route": "서울 → 부산"}],
        {"favourites": [{"id": "f1", "name": "주말 부산", "route": "서울 → 부산"}]},
        id="list_favourites",
    ),
    pytest.param(
        "list_favourites",
        {},
        "favourites",
        [],
        {"favourites": []},
        id="list_favourites-empty",
    ),
    pytest.param(
        "start_watch",
        {**TRIP, "trains": ["00101"]},
        "start_search",
        {"started": True, "startedAt": "2026-10-07T00:00:00+00:00"},
        {"started": True, "startedAt": "2026-10-07T09:00:00+09:00"},
        id="start_watch",
    ),
    pytest.param(
        "start_watch",
        {**TRIP, "trains": ["00101"], "waitlist": True},
        "start_search",
        {"started": False, "waitlisted": True, "trainNo": "00101"},
        {"started": False, "waitlisted": True, "trainNo": "00101", "notes": [WAITLISTED]},
        id="start_watch-waitlist",
    ),
    pytest.param(
        "stop_watch",
        {},
        "cancel_search",
        {"stopped": True, "unscheduled": True},
        {
            "stopped": True,
            "unscheduled": True,
            "notes": [STOPPED, "시작 예정이던 감시를 취소했어요."],
        },
        id="stop_watch",
    ),
    pytest.param(
        "stop_watch",
        {},
        "cancel_search",
        {"stopped": False, "unscheduled": False},
        {"stopped": False, "unscheduled": False, "notes": [NOTHING_TO_STOP]},
        id="stop_watch-nothing-stopped",
    ),
    pytest.param(
        "reserve_seat",
        RESERVE_ARGS,
        "reserve_designated",
        {
            "reserved": True,
            "reservationId": "R1",
            "expiresAt": "2026-10-10T01:10:00+00:00",
            "paymentUrl": URL,
        },
        {
            "reserved": True,
            "reservationId": "R1",
            "expiresAt": "2026-10-10T10:10:00+09:00",
            "paymentUrl": URL,
            "notes": [PAY_NOTE],
        },
        id="reserve_seat",
    ),
    pytest.param(
        "cancel_reservation",
        {},
        "cancel_pending",
        {"cancelled": True, "pending": []},
        {"cancelled": True, "pending": [], "notes": [CANCELLED]},
        id="cancel_reservation",
    ),
    pytest.param(
        "cancel_reservation",
        {},
        "cancel_pending",
        {"cancelled": True, "pending": [PENDING]},
        {
            "cancelled": True,
            "pending": [{**PENDING, "expiresAt": "2026-10-10T10:10:00+09:00", "paymentUrl": URL}],
            "notes": [PARTLY_CANCELLED],
        },
        id="cancel_reservation-partly",
    ),
]


@pytest.fixture(autouse=True)
def _cheap_setup(monkeypatch):
    cheap_setup(monkeypatch)
    # RUNNING's last check is 2026-10-07T09:01+09:00: thirty seconds earlier than "now".
    now = datetime.datetime(2026, 10, 7, 0, 1, 30, tzinfo=datetime.UTC)
    monkeypatch.setattr(mcp, "_now", lambda: now)


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


@pytest.fixture
def booker(env):
    return connect(env, signup(env, "alice"), allow_booking=True)["access_token"]


@pytest.mark.parametrize(("tool", "arguments", "method", "answer", "expected"), SHAPES)
def test_tool_result_shape(env, booker, tool, arguments, method, answer, expected):
    env.gateway.status.return_value = {"running": None, "scheduled": None, "pending": []}
    getattr(env.gateway, method).return_value = answer
    result = call_tool(env, booker, tool, arguments)
    assert not result.get("isError"), result
    assert result["structuredContent"] == expected


def test_every_tool_has_a_shape_case():
    from korail_bot.mobile.mcp import TOOLS

    assert {case.values[0] for case in SHAPES} == set(TOOLS)


@pytest.mark.parametrize(
    ("cars", "per_car", "width", "keep"),
    [
        # 좌석 하나가 약 2 x width 바이트(seatNo 에도 표시가 들어감). 100KB 에 맞는 첫 단계에서 멈춘다.
        (1, 100, 1100, 40),
        (1, 100, 2000, 10),
        (8, 12, 1500, 3),
    ],
)
def test_a_huge_seat_map_is_cut_per_car_and_says_how_far(env, booker, cars, per_car, width, keep):
    label = "x" * width
    env.gateway.seat_inventories.return_value = {
        "inventories": [
            {"carNo": car, "seats": [seat(car, f"{n:03d}{label}") for n in range(per_car)]}
            for car in range(1, cars + 1)
        ],
        "failedCars": [],
    }
    result = call_tool(env, booker, "get_seat_map", SEAT_ARGS)["structuredContent"]
    assert [len(car["freeSeats"]) for car in result["cars"]] == [keep] * cars
    assert result["cars"][0]["freeSeats"][0] == {"seatNo": f"1-000{label}", "label": f"000{label}"}
    assert result["truncated"] is True
    assert result["notes"] == [f"좌석이 많아 호차마다 {keep}석까지만 보여요."]


def test_a_refused_tool_has_no_structured_content(env):
    reader = connect(env, signup(env, "bob"))["access_token"]
    result = call_tool(env, reader, "stop_watch")
    assert result == {
        "content": [{"type": "text", "text": result["content"][0]["text"]}],
        "isError": True,
    }
    assert BOOK_HINT in result["content"][0]["text"]
