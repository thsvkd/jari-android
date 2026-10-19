"""
Korail standby (코레일 예약 대기) after the one request: recorded, looked up no oftener than the
configured interval, handed to the payment flow once a seat is assigned, and withdrawable.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis
import pytest
from korail_mobile_api import (
    ReservationDetailJourney,
    ReservationHistoryResponse,
    ReservationHistoryTrain,
    ReservationSeatDetail,
    TicketReservationDetailResponse,
)
from korail_mobile_api.errors import KorailReservationRefusedError, KorailTransportError

from korail_bot.mobile import config as mobile_config
from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.runtime import MobileRuntime
from korail_bot.services.korail_service import KorailService
from korail_bot.utils.timezone import utc_now

DEPARTS = (utc_now() + timedelta(days=3)).strftime("%Y%m%d")


@pytest.fixture
def app(tmp_path, monkeypatch):
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=fakeredis.FakeRedis(decode_responses=True),
    )
    rail = MagicMock()
    rail.login.return_value = True
    rail.request_waitlist.return_value = {
        "train_no": "015",
        "reservation_id": "PNR015",
        "journey_count": "0001",
        "dep_date": DEPARTS,
        "dep_time": "072700",
        "train": SimpleNamespace(
            train_no="015",
            train_class_name="KTX",
            departure_station_name="서울",
            arrival_station_name="부산",
            departure_date=DEPARTS,
            departure_time="072700",
            arrival_time="101200",
        ),
    }
    rail.waitlist_state.return_value = {"state": "waiting"}
    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: rail)
    user = runtime.identity.register("alice", "a long secure passphrase")
    runtime.identity.set_approval(user["user"]["id"], "approved")
    owner = runtime.identity.authenticate(user["token"])["storage_id"]
    http = runtime.app.test_client()
    headers = {"Authorization": "Bearer " + user["token"]}
    assert http.post(
        "/api/mobile/register",
        headers=headers,
        json={"username": "01012345678", "password": "rail password"},
    ).json == {"registered": True}
    return SimpleNamespace(
        runtime=runtime,
        gateway=runtime.gateway,
        rail=rail,
        owner=owner,
        http=http,
        headers=headers,
    )


def conditions(**changes):
    return {
        "v": 1,
        "action": "prepare_search",
        "dep_date": DEPARTS,
        "src_station": "서울",
        "dst_station": "부산",
        "dep_time": "0700",
        "max_dep_time": "1200",
        "train_type": "1",
        "seat_option": "2",
        "passenger_count": 1,
        "seat_strategy": "1",
        "seat_preference": "",
        "waitlist": True,
        **changes,
    }


def apply(app, **changes):
    return app.http.post(
        "/api/mobile/search",
        headers=app.headers,
        json={"conditions": conditions(**changes), "trains": ["015"]},
    )


def status(app):
    return app.http.get("/api/mobile/status", headers=app.headers).json


def test_a_standby_is_one_request_and_is_shown_as_waiting(app):
    answered = apply(app)

    assert answered.status_code == 200, answered.json
    assert answered.json == {"started": False, "waitlisted": True, "trainNo": "015"}
    app.rail.request_waitlist.assert_called_once()
    (entry,) = status(app)["waitlists"]
    assert entry["id"] == "PNR015"
    assert entry["state"] == "waiting"
    assert entry["trainInfo"].startswith("KTX 015 서울 → 부산")
    assert entry["checkedAt"] is None
    # The journey count is what a withdrawal needs, not something the app is shown.
    assert "journeyCount" not in entry
    # Applying is not a seat: nothing waits for payment.
    assert status(app)["pending"] == []
    assert app.gateway.bootstrap(app.owner)["waitlistCheckMinutes"] == 10


def test_the_same_train_is_not_applied_for_twice(app):
    apply(app)

    again = apply(app)

    assert again.status_code == 409
    assert "이미" in again.json["error"]
    app.rail.request_waitlist.assert_called_once()


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (ValueError("선택한 열차는 현재 코레일 예약 대기 대상이 아니에요."), 409),
        (KorailReservationRefusedError("WRR800029", "동일한 예약 내역이 있습니다."), 422),
        # Korail not answering leaves as 503 (Cloudflare replaces 502 bodies).
        (KorailTransportError("KORAIL HTTP 503"), 503),
    ],
)
def test_a_standby_korail_did_not_take_records_nothing(app, failure, code):
    app.rail.request_waitlist.side_effect = failure

    answered = apply(app)

    assert answered.status_code == code
    assert status(app)["waitlists"] == []


def test_a_standby_is_looked_up_only_once_the_interval_has_passed(app):
    apply(app)
    created = utc_now()

    assert app.gateway.check_waitlists(now=created + timedelta(minutes=9)) == 0
    app.rail.waitlist_state.assert_not_called()

    assert app.gateway.check_waitlists(now=created + timedelta(minutes=10, seconds=1)) == 1
    assert app.gateway.check_waitlists(now=created + timedelta(minutes=15)) == 0
    assert app.rail.waitlist_state.call_count == 1
    assert app.rail.waitlist_state.call_args.args == ("PNR015",)
    (entry,) = status(app)["waitlists"]
    assert entry["state"] == "waiting"
    assert entry["checkedAt"] is not None
    assert entry["checkFailed"] is False


def test_a_failed_look_is_not_retried_sooner_and_is_not_an_ending(app):
    apply(app)
    app.rail.waitlist_state.side_effect = KorailTransportError("KORAIL HTTP 503")
    first = utc_now() + timedelta(minutes=11)

    app.gateway.check_waitlists(now=first)
    app.gateway.check_waitlists(now=first + timedelta(minutes=5))

    assert app.rail.waitlist_state.call_count == 1
    (entry,) = status(app)["waitlists"]
    assert entry["state"] == "waiting"
    assert entry["checkFailed"] is True


def test_a_refused_login_pauses_the_look_until_the_account_is_connected_again(app):
    apply(app)
    app.rail.login.return_value = False
    before = app.rail.login.call_count
    later = utc_now() + timedelta(minutes=11)

    app.gateway.check_waitlists(now=later)
    app.gateway.check_waitlists(now=later + timedelta(hours=1))

    assert app.rail.login.call_count == before + 1
    (entry,) = status(app)["waitlists"]
    assert entry["checkError"] == "login"

    app.rail.login.return_value = True
    app.http.post(
        "/api/mobile/register",
        headers=app.headers,
        json={"username": "01012345678", "password": "new password"},
    )
    assert "checkError" not in app.runtime.storage.get_waitlists(app.owner)[0]
    assert app.gateway.check_waitlists(now=later + timedelta(hours=2)) == 1


def test_an_assigned_seat_becomes_the_pending_payment_and_is_said_once(app):
    apply(app)
    app.rail.waitlist_state.return_value = {
        "state": "assigned",
        "seats": ["3호차 7A"],
        "deadline": (DEPARTS, "060000"),
    }
    publish = MagicMock()
    app.gateway.telegram.publish = publish
    later = utc_now() + timedelta(minutes=11)

    app.gateway.check_waitlists(now=later)
    app.gateway.check_waitlists(now=later + timedelta(minutes=30))

    assert app.rail.waitlist_state.call_count == 1
    shown = status(app)
    (pending,) = shown["pending"]
    assert pending["reservationId"] == "PNR015"
    assert pending["seatLabels"] == ["3호차 7A"]
    assert pending["seatClass"] == "general"
    assert pending["expiresAt"] is not None
    (entry,) = shown["waitlists"]
    assert entry["state"] == "assigned"
    assert entry["seatLabels"] == ["3호차 7A"]
    (call,) = publish.call_args_list
    assert "좌석이 배정됐어요" in call.args[1]
    assert call.kwargs["kind"] == "payment"


@pytest.mark.parametrize(("state", "words"), [("paid", "결제를 확인"), ("ended", "보이지 않아요")])
def test_a_standby_that_left_the_list_is_finished_and_said(app, state, words):
    apply(app)
    app.rail.waitlist_state.return_value = {"state": state}
    publish = MagicMock()
    app.gateway.telegram.publish = publish

    app.gateway.check_waitlists(now=utc_now() + timedelta(minutes=11))

    (entry,) = status(app)["waitlists"]
    assert entry["state"] == state
    assert status(app)["pending"] == []
    assert words in publish.call_args.args[1]


def test_a_finished_standby_leaves_the_screen_a_day_later(app):
    apply(app)
    app.rail.waitlist_state.return_value = {"state": "ended"}
    later = utc_now() + timedelta(minutes=11)
    app.gateway.check_waitlists(now=later)

    app.gateway.check_waitlists(now=later + timedelta(hours=25))

    assert app.runtime.storage.get_waitlists(app.owner) == []


def test_withdrawing_a_standby_uses_its_own_record(app):
    apply(app)

    unknown = app.http.post(
        "/api/mobile/waitlist/cancel", headers=app.headers, json={"id": "SOMEONE-ELSE"}
    )
    withdrawn = app.http.post(
        "/api/mobile/waitlist/cancel", headers=app.headers, json={"id": "PNR015"}
    )

    assert unknown.status_code == 404
    assert withdrawn.status_code == 200, withdrawn.json
    app.rail.cancel_waitlist.assert_called_once_with("PNR015", "0001")
    assert [entry["state"] for entry in withdrawn.json["waitlists"]] == ["cancelled"]
    # Withdrawn is not looked up again.
    app.gateway.check_waitlists(now=utc_now() + timedelta(hours=1))
    app.rail.waitlist_state.assert_not_called()


def test_a_refused_withdrawal_keeps_the_standby_waiting(app):
    apply(app)
    app.rail.cancel_waitlist.side_effect = KorailReservationRefusedError("ERR", "불가")

    refused = app.http.post(
        "/api/mobile/waitlist/cancel", headers=app.headers, json={"id": "PNR015"}
    )

    assert refused.status_code == 503
    assert "코레일 앱" in refused.json["error"]
    assert status(app)["waitlists"][0]["state"] == "waiting"


# ---------------------------------------------------------------- KorailService


def korail():
    service = KorailService()
    service._logged_in = True
    service._modern_client = MagicMock()
    service._korail_instance = MagicMock()
    return service


def listed(*pnrs, deadline=True):
    raw = {"h_ntisu_lmt_dt": DEPARTS, "h_ntisu_lmt_tm": "060000"} if deadline else {}
    return ReservationHistoryResponse(
        str_result="SUCC",
        items=tuple(ReservationHistoryTrain(pnr_no=pnr, train_no="015", raw=raw) for pnr in pnrs),
    )


def detail(*seats):
    return TicketReservationDetailResponse(
        str_result="SUCC",
        journeys=(
            ReservationDetailJourney(
                seats=tuple(ReservationSeatDetail(car_no=car, seat_no=seat) for car, seat in seats)
            ),
        ),
    )


def state(service):
    return service.waitlist_state("PNR015", train_no="015", dep_date=DEPARTS, dep_time="072700")


def test_a_listed_standby_without_seats_is_waiting():
    service = korail()
    service._modern_client.get_reservation_history.return_value = listed("PNR015")
    service._modern_client.get_ticket_reservation_detail.return_value = detail()

    assert state(service) == {"state": "waiting"}


def test_a_listed_standby_with_seats_is_assigned_with_its_deadline():
    service = korail()
    service._modern_client.get_reservation_history.return_value = listed("PNR015")
    service._modern_client.get_ticket_reservation_detail.return_value = detail(("03", "7A"))

    assert state(service) == {
        "state": "assigned",
        "seats": ["3호차 7A"],
        "deadline": (DEPARTS, "060000"),
    }


def test_a_standby_gone_from_the_list_is_paid_only_with_a_ticket_on_that_train():
    service = korail()
    service._modern_client.get_reservation_history.return_value = listed("OTHER")
    ticket = SimpleNamespace(train_no="015", dep_date=DEPARTS, dep_time="072700")
    service._korail_instance.tickets.return_value = [ticket]
    assert state(service) == {"state": "paid"}

    service._korail_instance.tickets.return_value = [
        SimpleNamespace(train_no="017", dep_date=DEPARTS, dep_time="080000")
    ]
    assert state(service) == {"state": "ended"}


def test_a_list_that_could_not_be_read_is_a_failure_not_an_ending():
    service = korail()
    service._modern_client.get_reservation_history.side_effect = KorailTransportError("503")

    with pytest.raises(KorailTransportError):
        state(service)


def test_withdrawing_sends_the_standby_number_and_journey_count():
    service = korail()

    assert service.cancel_waitlist("PNR015", "0001") is True

    hold = service._modern_client.cancel_unpaid_hold.call_args.args[0]
    assert (hold.pnr_no, hold.journey_count, hold.str_result) == ("PNR015", "0001", "SUCC")
    assert service._modern_client.cancel_unpaid_hold.call_args.kwargs["consent"].allow_cancel


def test_a_train_korail_sent_no_standby_flag_for_is_unknown_not_ineligible():
    train = SimpleNamespace(train_no="015", departure_time="072700", arrival_time="101200")

    assert KorailService.describe_waitlist_train(train)["waitlistEligible"] is None


# ---------------------------------------------------------------- config


def test_the_check_interval_cannot_be_set_below_ten_minutes(monkeypatch, tmp_path):
    monkeypatch.setenv("MOBILE_SECRET", "s" * 40)
    monkeypatch.setenv("MOBILE_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MOBILE_DATA_DIR", str(tmp_path))
    assert MobileConfig.from_env().waitlist_check_minutes == 10
    monkeypatch.setenv("MOBILE_WAITLIST_CHECK_MINUTES", "30")
    assert MobileConfig.from_env().waitlist_check_minutes == 30
    for too_often in ("5", "0", "soon"):
        monkeypatch.setenv("MOBILE_WAITLIST_CHECK_MINUTES", too_often)
        with pytest.raises(ValueError, match="at least 10"):
            MobileConfig.from_env()
    assert mobile_config.WAITLIST_CHECK_MIN_MINUTES == 10
