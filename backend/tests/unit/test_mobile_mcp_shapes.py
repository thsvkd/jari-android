"""
도구 8개가 돌려주는 structuredContent 의 모양 전체를 고정한다(== 비교).

gateway 응답 하나를 주고 결과를 통째로 비교하므로, 키 이름이나 빠진 필드의 기본값이 바뀌면
여기서 드러난다. 결과 모양을 바꾸기로 하면 아래 SHAPES 표의 기대값만 고치면 된다.
"""

import datetime

import pytest

from korail_bot.config.settings import settings

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
            "running": {
                **RUNNING,
                "startedAt": "2026-10-07T09:00:00+09:00",
                "lastCheckedAt": "2026-10-07T09:01:00+09:00",
            },
            "scheduled": {"startAt": "2026-10-10T07:00:00+09:00"},
            "pending": [{**PENDING, "expiresAt": "2026-10-10T10:10:00+09:00", "paymentUrl": URL}],
            "notes": [
                WATCHING,
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
                "specialInfoShow": "MYSTERY",
                "passengerCount": 3,
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
        {"started": False, "waitlisted": True, "trainNo": "00101"},
        id="start_watch-waitlist",
    ),
    pytest.param(
        "stop_watch",
        {},
        "cancel_search",
        {"stopped": True, "unscheduled": True},
        {"stopped": True, "unscheduled": True},
        id="stop_watch",
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
        {"cancelled": True, "pending": [PENDING]},
        {"cancelled": True, "pending": [{**PENDING, "expiresAt": "2026-10-10T10:10:00+09:00"}]},
        id="cancel_reservation",
    ),
]


@pytest.fixture(autouse=True)
def _cheap_setup(monkeypatch):
    cheap_setup(monkeypatch)


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


@pytest.fixture
def booker(env):
    return connect(env, signup(env, "alice"), allow_booking=True)["access_token"]


@pytest.mark.parametrize(("tool", "arguments", "method", "answer", "expected"), SHAPES)
def test_tool_result_shape(env, booker, tool, arguments, method, answer, expected):
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
