"""
코레일 예약 대기: 신청 한 번, 느린 확인, 배정되면 결제 대기로, 취소까지 실제 서버·저장소로 확인해요.

코레일만 가짜(fake_korail)예요. 스택은 확인 간격을 약 1초로 줄여요(운영은 10분 이상).
"""

from __future__ import annotations

from test_booking_flows import assert_app_wording

WAITLIST_TRAIN = "00103"


def apply(client, conditions: dict, train: str = WAITLIST_TRAIN) -> tuple[int, dict]:
    return client.error(
        "POST",
        "/search",
        {"conditions": {**conditions, "waitlist": True}, "trains": [train]},
    )


def test_a_standby_is_one_request_and_shown_as_waiting_not_as_a_seat(client, conditions):
    code, result = apply(client, conditions)

    assert code == 200, result
    assert result == {"started": False, "waitlisted": True, "trainNo": WAITLIST_TRAIN}
    status = client.call("GET", "/status")
    (entry,) = status["waitlists"]
    assert entry["state"] == "waiting"
    assert entry["trainNo"] == WAITLIST_TRAIN
    assert status["pending"] == [] and status["running"] is None
    calls = client.korail_log()
    assert [call["train_no"] for call in calls if call["event"] == "waitlist"] == [WAITLIST_TRAIN]
    assert not any(call["event"].startswith("reserve") for call in calls)
    # 확인은 신청 뒤 간격이 지나야 해요. 그 사이 목록을 바로 읽지 않아요.
    client.wait_for(lambda s: s["waitlists"][0]["checkedAt"], timeout=20)


def test_a_train_korail_does_not_take_a_standby_for_is_refused_before_any_request(
    client, conditions
):
    code, result = apply(client, conditions, train="00101")

    assert code == 409
    assert "예약 대기 대상" in result["error"]
    assert not any(call["event"] == "waitlist" for call in client.korail_log())
    assert client.call("GET", "/status")["waitlists"] == []


def test_a_refused_or_unreachable_request_records_nothing(client, conditions):
    client.scenario(waitlist_request="refused")
    refused, _ = apply(client, conditions)
    client.scenario(waitlist_request="unavailable")
    unreachable, _ = apply(client, conditions)

    assert (refused, unreachable) == (422, 503)
    assert client.call("GET", "/status")["waitlists"] == []


def test_a_seat_that_seems_assigned_is_said_so_without_becoming_a_pending_payment(
    client, conditions
):
    apply(client, conditions)
    client.scenario(waitlist="assigned")

    status = client.wait_for(lambda s: s["waitlists"][0]["state"] == "assigned", timeout=30)

    (entry,) = status["waitlists"]
    assert entry["seatLabels"] == ["3호차 7A"]
    assert entry["expiresAt"]
    # The payment watch (every few seconds) is not given an unconfirmed assignment.
    assert status["pending"] == []
    items = client.call("GET", "/notifications")["items"]
    assigned = [item for item in items if "배정된 것 같아요" in item["text"]]
    assert len(assigned) == 1 and assigned[0]["kind"] == "payment"
    assert_app_wording(assigned[0]["text"])

    # Paid in the Korail app the same day: the ticket list has no time of sale, so the app only
    # says a ticket on that train is there.
    client.scenario(waitlist="paid")
    client.wait_for(lambda s: s["waitlists"][0]["state"] == "ticketed", timeout=30)


def test_a_standby_of_another_korail_account_is_left_alone(client, conditions):
    apply(client, conditions)
    client.call("POST", "/register", {"username": "0000000001", "password": "korail-e2e"})

    # One look reads the new account's own list; the standby is not in it.
    status = client.wait_for(lambda s: s["waitlists"][0]["otherAccount"], timeout=30)

    assert status["waitlists"][0]["state"] == "waiting"
    code, _ = client.error("POST", "/waitlist/cancel", {"id": status["waitlists"][0]["id"]})
    assert code == 409
    assert not any(call["event"] == "waitlist_cancel" for call in client.korail_log())


def test_a_failed_look_is_not_an_ending(client, conditions):
    apply(client, conditions)
    client.scenario(waitlist="unavailable")

    status = client.wait_for(lambda s: s["waitlists"][0]["checkFailed"], timeout=30)

    assert status["waitlists"][0]["state"] == "waiting"
    assert not any(
        "보이지 않아요" in i["text"] for i in client.call("GET", "/notifications")["items"]
    )


def test_a_standby_gone_from_korail_is_ended(client, conditions):
    apply(client, conditions)
    client.scenario(waitlist="ended")

    status = client.wait_for(lambda s: s["waitlists"][0]["state"] != "waiting", timeout=30)

    assert status["waitlists"][0]["state"] == "ended"


def test_withdrawing_a_standby_sends_its_number_to_korail(client, conditions):
    apply(client, conditions)
    (entry,) = client.call("GET", "/status")["waitlists"]

    result = client.call("POST", "/waitlist/cancel", {"id": entry["id"]})
    # Korail is asked where it stands first, so a seat assigned meanwhile is not given back unasked.
    assert any(call["event"] == "waitlist_list" for call in client.korail_log())

    assert [e["state"] for e in result["waitlists"]] == ["cancelled"]
    withdrawn = [
        call["rsv_id"] for call in client.korail_log() if call["event"] == "waitlist_cancel"
    ]
    assert withdrawn == [entry["id"]]
    code, _ = client.error("POST", "/waitlist/cancel", {"id": entry["id"]})
    assert code == 404
