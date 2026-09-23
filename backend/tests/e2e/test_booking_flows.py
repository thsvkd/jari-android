"""
예약이 잡힌 뒤 사용자가 보는 것까지, 실제 서버·워커·저장소를 거쳐 확인해요.

코레일만 가짜(fake_korail)이고 나머지는 운영 코드 그대로예요. 실제 예약은 하지 않아요.
"""

from __future__ import annotations

import datetime

# 텔레그램 봇 시절 문구. 앱 알림에 남으면 안 되는 것들이에요.
CHAT_VOCABULARY = ["/notify_off", "/tickets", "/cancel", "/start", "봇", "===", "━━", "답장"]


def label(conditions: dict, dep: str, arr: str, no: str = "00101", name: str = "KTX") -> str:
    day = datetime.datetime.strptime(conditions["dep_date"], "%Y%m%d")
    weekday = "월화수목금토일"[day.weekday()]
    return f"{name} {no} 서울 → 부산 · {day.month}월 {day.day}일({weekday}) {dep}→{arr}"


def booking_notices(client) -> list[str]:
    items = client.call("GET", "/notifications")["items"]
    booked = [item for item in items if "잡았어요" in item["text"] or "예약했어요" in item["text"]]
    # 앱은 종류로 알림 앞 표시("예약"·"결제")를 정해요. 예약 소식이 "찾기"로 보이면 안 돼요.
    assert all(item["kind"] in ("reservation", "payment") for item in booked), booked
    return [item["text"] for item in booked]


def assert_app_wording(text: str) -> None:
    leftovers = [word for word in CHAT_VOCABULARY if word in text]
    assert not leftovers, f"앱 알림에 채팅 문구가 남았어요 {leftovers}: {text!r}"
    assert "출발역" not in text and "도착역" not in text, text


def start_search(client, conditions: dict, trains: list[str] | None = None) -> dict:
    return client.call("POST", "/search", {"conditions": conditions, "trains": trains or ["00101"]})


def test_a_seat_chosen_on_the_map_is_booked_and_shown_as_one_clean_line(client, conditions):
    trains = client.call("POST", "/trains", {"conditions": conditions})["trains"]
    first = trains[0]
    seats = client.call(
        "GET", f"/trains/{first['trainKey']}/cars/1/seats?seatClass=general&passengerCount=1"
    )["seats"]
    seat = next(seat for seat in seats if seat["salePossible"])

    booked = client.call(
        "POST",
        "/reservations/designated",
        {
            "trainKey": first["trainKey"],
            "seatClass": "general",
            "passengerCount": 1,
            "carNo": 1,
            "seats": [seat],
        },
    )

    assert booked["reserved"] is True
    pending = client.call("GET", "/status")["pending"]
    assert [item["trainInfo"] for item in pending] == [label(conditions, "07:00", "09:41")]
    assert pending[0]["seatLabels"] == [f"1호차 {seat['label']}"]
    expires = datetime.datetime.fromisoformat(pending[0]["expiresAt"])
    left = expires - datetime.datetime.now(datetime.UTC)
    assert datetime.timedelta(minutes=9) < left <= datetime.timedelta(minutes=10)
    notices = booking_notices(client)
    assert notices and label(conditions, "07:00", "09:41") in notices[0]
    assert_app_wording(notices[0])
    assert any(call["event"] == "reserve_designated" for call in client.korail_log())


def test_the_search_worker_books_a_freed_seat_and_the_app_sees_it(client, conditions):
    assert start_search(client, conditions)["started"] is True

    status = client.wait_for(lambda status: status["pending"])

    assert [item["trainInfo"] for item in status["pending"]] == [
        label(conditions, "07:00", "09:41")
    ]
    assert status["pending"][0]["expiresAt"]
    log = client.korail_log()
    polls = [call for call in log if call["event"] == "search"]
    assert len(polls) >= 3, "빈자리가 나기 전에 헛돈 조회가 있어야 해요"
    assert [call["event"] for call in log].count("reserve_train") == 1
    notice = client.wait_for(lambda _: booking_notices(client)) and booking_notices(client)[0]
    assert notice.startswith("🎉 좌석을 잡았어요\n" + label(conditions, "07:00", "09:41"))
    assert "결제 기한 안에 코레일에서 결제해 주세요." in notice
    assert_app_wording(notice)


def test_a_payment_made_on_korail_clears_the_pending_card(client, conditions):
    start_search(client, conditions)
    client.wait_for(lambda status: status["pending"])

    client.scenario(outcome="PAID")

    client.wait_for(lambda status: not status["pending"], timeout=20)


def test_cancelling_a_pending_booking_gives_the_seat_back_to_korail(client, conditions):
    start_search(client, conditions)
    pending = client.wait_for(lambda status: status["pending"] and not status["running"])["pending"]

    result = client.call("POST", "/reservations/cancel", {})

    assert result["cancelled"] is True
    assert result["pending"] == []
    cancelled = [call["rsv_id"] for call in client.korail_log() if call["event"] == "cancel"]
    assert cancelled == [pending[0]["reservationId"]]


def test_an_unreachable_korail_is_not_reported_as_no_seats(client, conditions):
    client.scenario(search="unavailable")
    start_search(client, conditions)
    # "error" 여야 해요. 멈춤(stale)이나 미확인(unavailable)은 실패를 알린 게 아니에요.
    failing = client.wait_for(
        lambda status: status["running"] and status["running"].get("health") == "error",
        timeout=30,
    )
    failed_searches = [call for call in client.korail_log() if call["event"] == "search"]
    assert failed_searches and {call["mode"] for call in failed_searches} == {"unavailable"}

    client.call("POST", "/search/cancel", {})
    client.scenario(search="sold_out")
    start_search(client, conditions)
    quiet = client.wait_for(
        lambda status: status["running"] and (status["running"].get("attemptCount") or 0) >= 3,
        timeout=20,
    )

    assert failing["running"]["health"] != quiet["running"]["health"]
    assert quiet["running"]["health"] == "healthy"
    assert not quiet["pending"]


def _python_with_fake_korail(stack, code: str, redis_url: str | None = None):
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parent / "fake_korail"),
        "PYTHONUTF8": "1",
        "JARI_E2E_FAKE_KORAIL": "1",
        "MOBILE_REDIS_URL": redis_url or stack["redis"],
    }
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def test_a_process_with_the_fake_cannot_reach_anything_outside_this_machine(stack, client):
    result = _python_with_fake_korail(
        stack,
        """
import socket
try:
    socket.create_connection(("1.1.1.1", 80), timeout=3)
except OSError as blocked:
    print("BLOCKED", blocked)
""",
    )

    assert "BLOCKED e2e: 루프백 밖 연결을 막았어요 (1.1.1.1)" in result.stdout, result.stderr
    blocked = [call for call in client.korail_log() if call["event"] == "blocked"]
    assert [call["host"] for call in blocked] == ["1.1.1.1"]
    # 이 테스트가 일부러 남긴 기록이라, 다른 테스트처럼 끝에서 실패로 세지 않게 비워요.
    client.reset_log()


def test_the_fake_refuses_to_start_anywhere_but_a_local_redis(stack):
    result = _python_with_fake_korail(
        stack, "print('STARTED')", redis_url="redis://redis.example.com:6379/0"
    )

    assert result.returncode == 97
    assert "STARTED" not in result.stdout
    assert "가짜 코레일을 끼우지 못해 멈춰요" in result.stderr
