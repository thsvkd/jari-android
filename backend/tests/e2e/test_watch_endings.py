"""
감시가 스스로 멈추는 세 가지(차단 의심·응답 없음 지속·시간 상한)를 실제 서버·워커로 확인해요.

코레일만 가짜(fake_korail)예요. 차단 의심은 실제 korail2 클라이언트가 strResult 없는 응답을 받는
데서부터 운영 코드가 그대로 돌아요. 멈춘 감시는 매진·성공·오류와 다른 종류의 알림과 종료 사유를 남겨요.
"""

from __future__ import annotations

import datetime
import time

from stack import request
from test_booking_flows import assert_app_wording, start_search


def notices(client, kind: str) -> list[str]:
    return [
        item["text"]
        for item in client.call("GET", "/notifications")["items"]
        if item["kind"] == kind
    ]


def ended(client, timeout: float = 30) -> dict:
    return client.wait_for(lambda status: status.get("ended"), timeout=timeout)


def test_the_first_reply_that_looks_like_a_block_ends_the_watch_at_once(client, conditions):
    client.scenario(search="blocked")
    start_search(client, conditions)

    status = ended(client)

    assert status["running"] is None and not status["pending"]
    assert status["ended"]["reason"] == "blocked"
    searches = [call for call in client.korail_log() if call["event"] == "search"]
    # 한 번 묻고 끝나요. 다시 묻거나 다시 로그인하지 않아요.
    assert [call["mode"] for call in searches] == ["blocked"]
    worker = searches[0]["pid"]
    logins = [c for c in client.korail_log() if c["event"] == "login" and c["pid"] == worker]
    assert [call["accepted"] for call in logins] == [True]
    (notice,) = notices(client, "blocked")
    assert notice.startswith("⛔ 코레일이 접속을 막은 것 같아 감시를 멈췄어요\n서울 → 부산\n")
    assert "코레일 앱에서 로그인되는지 확인해 주세요" in notice
    assert "좌석이 없다는 뜻이 아니에요" in notice
    assert_app_wording(notice)
    assert not notices(client, "error") and not notices(client, "reservation")


def test_failures_that_go_on_past_the_limit_end_the_watch_as_unavailable(client, conditions):
    client.scenario(search="unavailable", failure_stop_seconds=1)
    start_search(client, conditions)

    status = ended(client)

    assert status["ended"]["reason"] == "unavailable"
    searches = [call for call in client.korail_log() if call["event"] == "search"]
    assert len(searches) >= 2 and {call["mode"] for call in searches} == {"unavailable"}
    (notice,) = notices(client, "unavailable")
    assert notice.startswith("🌐 코레일 응답이 ")
    assert "감시를 멈췄어요" in notice.splitlines()[0]
    assert "좌석이 없다는 뜻이 아니에요" in notice
    assert_app_wording(notice)


def test_a_watch_that_finds_nothing_ends_at_its_time_limit(client, conditions):
    client.scenario(search="sold_out", watch_max_seconds=2)
    start_search(client, conditions)

    status = ended(client)

    assert status["ended"]["reason"] == "expired"
    assert not status["pending"]
    (notice,) = notices(client, "expired")
    assert "좌석을 잡지 못해 감시를 멈췄어요" in notice.splitlines()[0]
    assert_app_wording(notice)


def test_a_watch_brought_back_by_a_restart_keeps_counting_from_its_first_start(
    stack, client, conditions
):
    client.scenario(search="sold_out", watch_max_seconds=15)
    start_search(client, conditions)
    before = client.wait_for(
        lambda status: status["running"] and (status["running"].get("attemptCount") or 0) >= 1
    )["running"]
    started = before["startedAt"]

    request("POST", f"{stack['control']}/restart", {})
    restarted = time.time()
    after = client.wait_for(
        lambda status: (status["running"] or {}).get("health") == "healthy" or status.get("ended"),
        timeout=30,
    )

    # 다시 띄운 감시도 처음 시작한 시각을 그대로 들고 있어요(재시작 때 0 으로 돌리지 않아요).
    assert after["running"], f"재시작이 감시 시간({after})보다 오래 걸렸어요"
    assert after["running"]["startedAt"] == started
    after = ended(client, timeout=30)
    assert after["ended"]["reason"] == "expired"
    first = datetime.datetime.fromisoformat(started).timestamp()
    last = datetime.datetime.fromisoformat(after["ended"]["at"]).timestamp()
    # 처음 시작부터 15초에 끝나요. 재시작부터 다시 셌다면 돌아온 뒤로 15초를 더 돌아요.
    assert last < max(first + 15, restarted) + 4, (started, restarted, after["ended"])
