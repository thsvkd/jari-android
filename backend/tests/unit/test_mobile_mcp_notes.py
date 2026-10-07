"""
도구 결과의 사람이 읽는 문장(notes)에 빠진 값이 "None"이나 빈 괄호로 새지 않는다.

gateway 가 값을 빼고 돌려줘도(오래된 실행 기록, 결제 주소가 없는 응답) 에이전트가
사용자에게 그대로 읽어 줄 문장이 깨지면 안 된다.
"""

import json

import pytest

from korail_bot.config.settings import settings

from .agent_support import call_tool, cheap_setup, connect, make_env, signup


@pytest.fixture(autouse=True)
def _cheap_setup(monkeypatch):
    cheap_setup(monkeypatch)


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


@pytest.fixture
def booker(env):
    return connect(env, signup(env, "alice"), allow_booking=True)["access_token"]


def notes_of(result):
    return json.dumps(result["structuredContent"]["notes"], ensure_ascii=False)


@pytest.mark.parametrize(
    ("running", "kept"),
    [
        ({"health": "healthy"}, None),
        ({"health": "healthy", "passengerCount": 2}, "좌석 조건: 2명"),
        (
            {"health": "healthy", "specialInfoShow": "GENERAL_ONLY", "seatStrategy": "random"},
            "좌석 조건: 일반실만, 따로 앉아도 됨",
        ),
    ],
)
def test_status_notes_leave_out_what_the_status_left_out(env, booker, running, kept):
    env.gateway.status.return_value = {"running": running, "scheduled": None, "pending": []}
    notes = notes_of(call_tool(env, booker, "get_status"))
    assert "None" not in notes
    if kept is None:
        assert "좌석 조건" not in notes
    else:
        assert kept in notes


def test_a_booking_without_a_payment_url_still_says_where_to_pay(env, booker):
    env.gateway.reserve_designated.return_value = {"reserved": True, "pending": []}
    result = call_tool(
        env,
        booker,
        "reserve_seat",
        {
            "trainKey": "T1",
            "seatClass": "general",
            "carNo": 3,
            "seats": [{"carNo": 3, "seatNo": "3-7A", "label": "7A"}],
        },
    )
    notes = notes_of(result)
    assert "()" not in notes
    assert f"코레일({settings.KORAIL_PAYMENT_URL})" in notes
