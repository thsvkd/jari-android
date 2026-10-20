"""
Korail standby (코레일 예약 대기) after the one request: recorded, looked up no oftener than the
configured interval (one reservation-list read per owner), shown as "seems assigned" without
being handed to the payment watch, and withdrawable only from the account it was applied with.
"""

import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import DEFAULT, MagicMock

import fakeredis
import pytest
from korail2.korail2 import Ticket
from korail_mobile_api.errors import (
    KorailReservationRefusedError,
    KorailSessionExpiredError,
    KorailTransportError,
)
from korail_mobile_api.read_parsers import (
    parse_reservation_history_response,
    parse_ticket_reservation_detail_response,
)

from korail_bot.mobile import config as mobile_config
from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.gateway import WAITLIST_LIMIT, MobileGateway, WaitlistsActive
from korail_bot.mobile.runtime import MobileRuntime
from korail_bot.mobile.storage import MobileStorage
from korail_bot.services.korail_service import KorailService
from korail_bot.storage import redis as redis_storage
from korail_bot.utils.timezone import RAIL_TIMEZONE, utc_now

DEPARTS = (utc_now() + timedelta(days=3)).strftime("%Y%m%d")
TODAY = utc_now().astimezone(RAIL_TIMEZONE).strftime("%Y%m%d")
LATER = timedelta(minutes=11)


def registered(no):
    return {
        "train_no": no,
        "reservation_id": f"PNR{no}",
        "journey_count": "0001",
        "dep_date": DEPARTS,
        "dep_time": "072700",
        "train": SimpleNamespace(
            train_no=no,
            train_class_name="KTX",
            departure_station_name="서울",
            arrival_station_name="부산",
            departure_date=DEPARTS,
            departure_time="072700",
            arrival_time="101200",
        ),
    }


@pytest.fixture
def app(tmp_path, monkeypatch):
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=fakeredis.FakeRedis(decode_responses=True),
    )
    rail = MagicMock()
    rail.login.return_value = True
    rail.login_unavailable = False
    # Korail's member number for each login id: one member may log in by phone or by number.
    members = {}
    current = {"member": None}

    def login(username, password):
        digits = "".join(ch for ch in username if ch.isdigit())
        current["member"] = members.get(digits, f"M{digits}")
        return DEFAULT

    rail.login.side_effect = login
    rail.member_number.side_effect = lambda: current["member"]
    # Which member's reservation list each standby is in (Korail keeps one list per account).
    owned = {}

    def request(**kwargs):
        answer = registered(kwargs["train_no"])
        owned[answer["reservation_id"]] = current["member"]
        return answer

    rail.request_waitlist.side_effect = request
    states = {}

    def answers(standbys):
        reply = {}
        for standby in standbys:
            if standby.get("listed_only") and owned.get(standby["id"]) != current["member"]:
                reply[standby["id"]] = {"state": "unlisted"}
            else:
                reply[standby["id"]] = dict(states.get(standby["id"], {"state": "waiting"}))
        return reply

    rail.waitlist_states.side_effect = answers
    # The real verdict on what a blocked client gets (a reply without strResult).
    rail.looks_blocked.side_effect = KorailService().looks_blocked
    rail.login_blocked = False
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
        storage=runtime.storage,
        gateway=runtime.gateway,
        rail=rail,
        states=states,
        members=members,
        owned=owned,
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


def apply(app, train="015"):
    return app.http.post(
        "/api/mobile/search",
        headers=app.headers,
        json={"conditions": conditions(), "trains": [train]},
    )


def status(app):
    return app.http.get("/api/mobile/status", headers=app.headers).json


def cancel(app, standby, **extra):
    return app.http.post(
        "/api/mobile/waitlist/cancel", headers=app.headers, json={"id": standby, **extra}
    )


def looks(app):
    """How many times Korail's reservation list was read for standbys."""
    return app.rail.waitlist_states.call_count


# ---------------------------------------------------------------- applying


def test_a_standby_is_one_request_and_is_shown_as_waiting(app):
    answered = apply(app)

    assert answered.status_code == 200, answered.json
    assert answered.json == {"started": False, "waitlisted": True, "trainNo": "015"}
    app.rail.request_waitlist.assert_called_once()
    (entry,) = status(app)["waitlists"]
    assert entry["id"] == "PNR015"
    assert entry["state"] == "waiting"
    assert entry["otherAccount"] is False
    assert entry["trainInfo"].startswith("KTX 015 서울 → 부산")
    assert entry["checkedAt"] is None
    # The journey count and the account tag are for withdrawing and matching, not for the app.
    assert "journeyCount" not in entry and "account" not in entry
    stored = app.storage.get_waitlists(app.owner)[0]
    assert stored["account"] == app.storage.account_tag("member:M01012345678")
    assert "M01012345678" not in str(stored) and "12345678" not in str(stored)
    # Applying is not a seat: nothing waits for payment.
    assert status(app)["pending"] == []
    assert app.gateway.bootstrap(app.owner)["waitlistCheckMinutes"] == 10


def test_the_same_train_is_not_applied_for_twice(app):
    apply(app)

    again = apply(app)

    assert again.status_code == 409
    assert "이미" in again.json["error"]
    app.rail.request_waitlist.assert_called_once()


def test_an_owner_has_at_most_a_few_standbys_at_once(app):
    for number in range(WAITLIST_LIMIT):
        assert apply(app, f"0{number}1").status_code == 200

    over = apply(app, "099")

    assert over.status_code == 409
    assert f"{WAITLIST_LIMIT}건까지" in over.json["error"]
    assert app.rail.request_waitlist.call_count == WAITLIST_LIMIT


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


def test_a_standby_without_a_number_is_reported_and_not_withdrawn_blindly(app):
    app.rail.request_waitlist.side_effect = lambda **kwargs: {
        **registered("015"),
        "reservation_id": "",
    }

    answered = apply(app)

    assert answered.status_code == 503
    assert "코레일 앱" in answered.json["error"]
    app.rail.cancel_waitlist.assert_not_called()
    assert status(app)["waitlists"] == []


def test_a_standby_that_cannot_be_saved_is_withdrawn(app):
    app.storage.save_waitlists = MagicMock(side_effect=RuntimeError("redis down"))

    answered = apply(app)

    assert answered.status_code == 503
    assert "취소했어요" in answered.json["error"]
    app.rail.cancel_waitlist.assert_called_once_with("PNR015", "0001")


def test_a_standby_that_can_neither_be_saved_nor_withdrawn_says_so(app):
    app.storage.save_waitlists = MagicMock(side_effect=RuntimeError("redis down"))
    app.rail.cancel_waitlist.side_effect = KorailTransportError("KORAIL HTTP 503")

    answered = apply(app)

    assert answered.status_code == 503
    assert "신청됐지만" in answered.json["error"]


# ---------------------------------------------------------------- looking


def test_one_list_read_per_owner_no_oftener_than_the_interval(app):
    apply(app, "015")
    apply(app, "017")
    created = utc_now()

    assert app.gateway.check_waitlists(now=created + timedelta(minutes=9)) == 0
    assert looks(app) == 0

    assert app.gateway.check_waitlists(now=created + LATER) == 2
    assert app.gateway.check_waitlists(now=created + LATER + timedelta(minutes=5)) == 0
    # Both standbys came from one read of the reservation list.
    assert looks(app) == 1
    asked = app.rail.waitlist_states.call_args.args[0]
    assert {standby["id"] for standby in asked} == {"PNR015", "PNR017"}
    assert asked[0]["applied_on"] == TODAY
    assert app.gateway.check_waitlists(now=created + LATER + timedelta(minutes=11)) == 2
    assert looks(app) == 2
    assert [e["state"] for e in status(app)["waitlists"]] == ["waiting", "waiting"]


def test_a_failed_look_is_not_retried_sooner_and_is_not_an_ending(app):
    apply(app)
    app.rail.waitlist_states.side_effect = KorailTransportError("KORAIL HTTP 503")
    first = utc_now() + LATER

    app.gateway.check_waitlists(now=first)
    app.gateway.check_waitlists(now=first + timedelta(minutes=5))

    assert looks(app) == 1
    (entry,) = status(app)["waitlists"]
    assert entry["state"] == "waiting"
    assert entry["checkFailed"] is True


def test_the_interval_holds_when_the_records_cannot_be_saved(app):
    apply(app)
    app.storage.save_waitlists = MagicMock(side_effect=RuntimeError("redis down"))
    first = utc_now() + LATER

    for minute in range(9):
        app.gateway.check_waitlists(now=first + timedelta(minutes=minute))

    assert looks(app) == 1
    app.gateway.check_waitlists(now=first + timedelta(minutes=10))
    assert looks(app) == 2


def test_a_refused_login_is_tried_once_and_pauses_every_standby_until_reconnected(app):
    for train in ("015", "017", "019"):
        apply(app, train)
    app.rail.login.return_value = False
    before = app.rail.login.call_count
    later = utc_now() + LATER

    app.gateway.check_waitlists(now=later)
    app.gateway.check_waitlists(now=later + timedelta(hours=1))

    assert app.rail.login.call_count == before + 1
    assert {e["checkError"] for e in status(app)["waitlists"]} == {"login"}
    assert looks(app) == 0

    app.rail.login.return_value = True
    app.http.post(
        "/api/mobile/register",
        headers=app.headers,
        json={"username": "01012345678", "password": "new password"},
    )
    assert all("checkError" not in e for e in app.storage.get_waitlists(app.owner))
    assert app.gateway.check_waitlists(now=later + timedelta(hours=2)) == 3


def test_an_expired_login_is_renewed_once_without_touching_the_seat_map_login(app):
    apply(app)
    seat_rail = MagicMock()
    app.gateway._rails[app.owner] = (seat_rail, 10**12, "01012345678")
    app.rail.waitlist_states.side_effect = [
        KorailSessionExpiredError("P058", "로그인이 필요합니다"),
        {"PNR015": {"state": "waiting"}},
    ]

    app.gateway.check_waitlists(now=utc_now() + LATER)

    assert status(app)["waitlists"][0]["checkFailed"] is False
    # A failed look never closes the session someone browses seat maps with.
    app.rail.waitlist_states.side_effect = KorailTransportError("KORAIL HTTP 503")
    app.gateway.check_waitlists(now=utc_now() + LATER * 3)
    seat_rail.close.assert_not_called()
    assert app.owner in app.gateway._rails


def test_an_assigned_seat_is_said_as_likely_and_kept_out_of_the_payment_watch(app):
    apply(app)
    app.states["PNR015"] = {
        "state": "assigned",
        "seats": ["3호차 7A"],
        "deadline": (DEPARTS, "060000"),
    }
    publish = MagicMock()
    app.gateway.telegram.publish = publish
    later = utc_now() + LATER

    app.gateway.check_waitlists(now=later)
    app.gateway.check_waitlists(now=later + timedelta(minutes=30))

    # Still looked up at the slow pace until Korail drops it; never by the payment watch.
    assert looks(app) == 2
    shown = status(app)
    assert shown["pending"] == []
    assert app.storage.get_payment_status(app.owner) is None
    (entry,) = shown["waitlists"]
    assert entry["state"] == "assigned"
    assert entry["seatLabels"] == ["3호차 7A"]
    assert entry["expiresAt"] is not None
    (call,) = publish.call_args_list
    assert "배정된 것 같아요" in call.args[1]
    assert "코레일 앱에서 확인" in call.args[1]
    assert call.kwargs["kind"] == "payment"

    # Paid in the Korail app: the slow look sees the standby gone and a ticket sold after it.
    app.states["PNR015"] = {"state": "paid"}
    app.gateway.check_waitlists(now=later + timedelta(minutes=45))
    assert status(app)["waitlists"][0]["state"] == "paid"


def test_an_assigned_standby_left_unpaid_ends_when_korail_drops_it(app):
    apply(app)
    app.states["PNR015"] = {"state": "assigned", "seats": ["3호차 7A"], "deadline": None}
    later = utc_now() + LATER
    app.gateway.check_waitlists(now=later)
    assert status(app)["waitlists"][0]["expiresAt"] is None

    app.states["PNR015"] = {"state": "ended"}
    app.gateway.check_waitlists(now=later + timedelta(minutes=11))

    assert status(app)["waitlists"][0]["state"] == "ended"


def test_a_standby_is_cleared_after_its_departure(app):
    apply(app)
    departed = utc_now() + timedelta(days=4)

    app.gateway.check_waitlists(now=departed)

    assert app.storage.get_waitlists(app.owner) == []


@pytest.mark.parametrize(
    ("state", "words"),
    [("paid", "발권"), ("ticketed", "승차권이 보여요"), ("ended", "보이지 않아요")],
)
def test_a_standby_that_left_the_list_is_finished_and_said(app, state, words):
    apply(app)
    app.states["PNR015"] = {"state": state}
    publish = MagicMock()
    app.gateway.telegram.publish = publish

    app.gateway.check_waitlists(now=utc_now() + LATER)

    (entry,) = status(app)["waitlists"]
    assert entry["state"] == state
    assert words in publish.call_args.args[1]


def test_a_finished_standby_leaves_the_screen_a_day_later(app):
    apply(app)
    app.states["PNR015"] = {"state": "ended"}
    later = utc_now() + LATER
    app.gateway.check_waitlists(now=later)

    app.gateway.check_waitlists(now=later + timedelta(hours=25))

    assert app.storage.get_waitlists(app.owner) == []


def test_a_pass_stops_when_the_runtime_is_stopping(app):
    apply(app)
    app.gateway.waitlist_should_stop = lambda: True

    assert app.gateway.check_waitlists(now=utc_now() + LATER) == 0
    assert looks(app) == 0


def test_a_pass_that_learns_of_the_stop_after_reading_does_not_write(app):
    apply(app)
    asked = iter([False, False, True])
    app.gateway.waitlist_should_stop = lambda: next(asked, True)

    app.gateway.check_waitlists(now=utc_now() + LATER)

    assert looks(app) == 1
    assert app.storage.get_waitlists(app.owner)[0]["checkedAt"] is None


# ---------------------------------------------------------------- accounts


def test_a_standby_of_another_korail_account_is_seen_missing_once_then_left_alone(app):
    apply(app)
    app.http.post(
        "/api/mobile/register",
        headers=app.headers,
        json={"username": "01099998888", "password": "other"},
    )
    later = utc_now() + LATER

    # Once, this account's own list is read: the standby is not in it, so it is another's.
    app.gateway.check_waitlists(now=later)
    app.gateway.check_waitlists(now=later + timedelta(minutes=11))
    app.gateway.check_waitlists(now=later + timedelta(minutes=22))

    assert looks(app) == 1
    assert app.rail.waitlist_states.call_args.args[0][0]["listed_only"] is True
    (entry,) = status(app)["waitlists"]
    assert entry["otherAccount"] is True
    assert entry["state"] == "waiting"
    refused = cancel(app, "PNR015")
    assert refused.status_code == 409
    assert "찾지 못한 대기" in refused.json["error"]
    app.rail.cancel_waitlist.assert_not_called()


def test_the_same_member_logging_in_by_another_id_keeps_its_standbys(app):
    apply(app)
    app.members["1234567890"] = "M01012345678"
    app.http.post(
        "/api/mobile/register",
        headers=app.headers,
        json={"username": "1234567890", "password": "rail password"},
    )

    app.gateway.check_waitlists(now=utc_now() + LATER)

    (entry,) = status(app)["waitlists"]
    assert entry["otherAccount"] is False
    assert entry["checkedAt"] is not None
    assert app.rail.waitlist_states.call_args.args[0][0]["listed_only"] is False
    assert cancel(app, "PNR015").status_code == 200


def test_a_stale_account_tag_is_mended_by_finding_the_standby_in_this_accounts_list(app):
    apply(app)
    entries = app.storage.get_waitlists(app.owner)
    entries[0]["account"] = "a-tag-made-with-an-old-key"
    app.storage.save_waitlists(app.owner, entries)

    app.gateway.check_waitlists(now=utc_now() + LATER)

    stored = app.storage.get_waitlists(app.owner)[0]
    assert stored["account"] == app.storage.account_tag("member:M01012345678")
    assert status(app)["waitlists"][0]["otherAccount"] is False


def test_the_account_tag_does_not_follow_the_server_secret(app):
    other = MobileStorage(secret="b" * 40, client=app.storage.redis.client)

    assert other.account_tag("member:M1") == app.storage.account_tag("member:M1")


def test_another_accounts_standbys_do_not_count_against_the_limit(app):
    for number in range(WAITLIST_LIMIT):
        apply(app, f"0{number}1")
    app.http.post(
        "/api/mobile/register",
        headers=app.headers,
        json={"username": "01099998888", "password": "other"},
    )

    assert apply(app, "099").status_code == 200


def test_with_no_korail_account_linked_a_standby_says_so_and_korail_is_not_asked(app):
    apply(app)
    app.storage.delete_onboarded_account(app.owner)

    app.gateway.check_waitlists(now=utc_now() + LATER)

    (entry,) = status(app)["waitlists"]
    assert entry["checkError"] == "unlinked"
    assert looks(app) == 0


def delete(app, **extra):
    return app.http.post(
        "/api/mobile/account/delete",
        headers=app.headers,
        json={"password": "a long secure passphrase", **extra},
    )


def test_an_account_with_a_live_standby_is_deleted_only_once_it_is_looked_after(app):
    apply(app)

    refused = delete(app)

    assert refused.status_code == 409
    assert refused.json["code"] == "waitlists_active"
    assert "코레일 앱에서 직접 관리" in refused.json["error"]
    assert app.storage.get_waitlists(app.owner)
    assert delete(app, acknowledgeWaitlists=True).status_code == 200
    assert app.storage.get_waitlists(app.owner) == []


def test_logging_out_of_korail_does_not_let_a_standby_be_erased_unasked(app):
    apply(app)
    app.http.post("/api/mobile/logout", headers=app.headers, json={})

    with pytest.raises(WaitlistsActive):
        app.gateway.delete_account(app.owner)

    assert app.storage.get_waitlists(app.owner)
    assert app.gateway.delete_account(app.owner, acknowledge_waitlists=True) == {"deleted": True}


def test_a_withdrawn_standby_does_not_hold_the_account_back(app):
    apply(app)
    cancel(app, "PNR015")

    assert app.gateway.delete_account(app.owner) == {"deleted": True}


def test_nothing_is_kept_for_an_owner_whose_standbys_are_gone(app):
    apply(app)
    app.gateway.check_waitlists(now=utc_now() + LATER)
    assert app.owner in app.gateway._waitlist_rails
    app.gateway.delete_account(app.owner, acknowledge_waitlists=True)

    app.gateway.check_waitlists(now=utc_now() + LATER * 3)

    assert app.owner not in app.gateway._waitlist_rails
    assert app.owner not in app.gateway._waitlist_attempts
    app.rail.close.assert_called()


# ---------------------------------------------------------------- withdrawing


def test_withdrawing_a_standby_uses_its_own_record(app):
    apply(app)

    unknown = cancel(app, "SOMEONE-ELSE")
    withdrawn = cancel(app, "PNR015")

    assert unknown.status_code == 404
    assert withdrawn.status_code == 200, withdrawn.json
    app.rail.cancel_waitlist.assert_called_once_with("PNR015", "0001")
    assert [entry["state"] for entry in withdrawn.json["waitlists"]] == ["cancelled"]
    app.gateway.check_waitlists(now=utc_now() + timedelta(hours=1))
    assert looks(app) == 1  # only the read before withdrawing


def test_a_standby_assigned_meanwhile_is_withdrawn_only_after_agreeing_to_give_the_seat_back(
    app,
):
    apply(app)
    app.states["PNR015"] = {"state": "assigned", "seats": ["3호차 7A"], "deadline": None}

    first = cancel(app, "PNR015")

    assert first.status_code == 409
    assert "배정된 좌석도" in first.json["error"]
    app.rail.cancel_waitlist.assert_not_called()
    assert status(app)["waitlists"][0]["state"] == "assigned"

    agreed = cancel(app, "PNR015", releaseAssigned=True)
    assert agreed.status_code == 200
    app.rail.cancel_waitlist.assert_called_once()


def test_a_standby_korail_already_ended_is_not_withdrawn(app):
    apply(app)
    app.states["PNR015"] = {"state": "ended"}

    answered = cancel(app, "PNR015")

    assert answered.status_code == 409
    app.rail.cancel_waitlist.assert_not_called()
    assert status(app)["waitlists"][0]["state"] == "ended"


def test_a_withdrawal_whose_state_could_not_be_read_is_not_sent(app):
    apply(app)
    app.rail.waitlist_states.side_effect = KorailTransportError("KORAIL HTTP 503")

    answered = cancel(app, "PNR015")

    assert answered.status_code == 503
    app.rail.cancel_waitlist.assert_not_called()


def test_a_refused_withdrawal_keeps_the_standby_waiting(app):
    apply(app)
    app.rail.cancel_waitlist.side_effect = KorailReservationRefusedError("ERR", "불가")

    refused = cancel(app, "PNR015")

    assert refused.status_code == 503
    assert "코레일 앱" in refused.json["error"]
    assert status(app)["waitlists"][0]["state"] == "waiting"


def test_a_withdrawal_korail_took_is_answered_even_if_it_cannot_be_saved(app):
    apply(app)
    app.storage.save_waitlists = MagicMock(side_effect=RuntimeError("redis down"))

    answered = cancel(app, "PNR015")

    assert answered.status_code == 200
    assert answered.json["waitlists"][0]["state"] == "cancelled"


# ---------------------------------------------------------------- KorailService, on Korail's JSON

ENVELOPE = {"strResult": "SUCC", "h_msg_cd": "IRZ000001", "h_msg_txt": "정상 처리되었습니다."}


def history(*rows):
    """ReservationView as Korail sends it, through the library's own parser."""
    return parse_reservation_history_response(
        {**ENVELOPE, "jrny_infos": {"jrny_info": [{"train_infos": {"train_info": list(rows)}}]}}
    )


def history_row(pnr, *, deadline=True):
    return {
        "h_pnr_no": pnr,
        "h_trn_no": "015",
        "h_run_dt": DEPARTS,
        "h_dpt_tm": "072700",
        "h_arv_tm": "101200",
        "h_dpt_rs_stn_nm": "서울",
        "h_arv_rs_stn_nm": "부산",
        "h_tot_seat_cnt": "001",
        **({"h_ntisu_lmt_dt": DEPARTS, "h_ntisu_lmt_tm": "060000"} if deadline else {}),
    }


def detail(*seats):
    """certification.ReservationList as Korail sends it, through the library's own parser."""
    return parse_ticket_reservation_detail_response(
        {
            **ENVELOPE,
            "h_pnr_no": "PNR015",
            "jrny_infos": {
                "jrny_info": [
                    {
                        "h_trn_no": "015",
                        "seat_infos": {
                            "seat_info": [
                                {"h_srcar_no": car, "h_seat_no": seat, "h_psrm_cl_cd": "1"}
                                for car, seat in seats
                            ]
                        },
                    }
                ]
            },
        }
    )


def ticket(sold):
    """A korail2 Ticket from MyTicketList's JSON."""
    info = {
        "h_trn_clsf_cd": "100",
        "h_trn_clsf_nm": "KTX",
        "h_trn_gp_cd": "100",
        "h_trn_no": "015",
        "h_dpt_rs_stn_nm": "서울",
        "h_dpt_rs_stn_cd": "0001",
        "h_dpt_dt": DEPARTS,
        "h_dpt_tm": "072700",
        "h_arv_rs_stn_nm": "부산",
        "h_arv_rs_stn_cd": "0020",
        "h_arv_dt": DEPARTS,
        "h_arv_tm": "101200",
        "h_run_dt": DEPARTS,
        "h_seat_cnt": "001",
        "h_rcvd_amt": "00059800",
        "h_srcar_no": "3",
        "h_seat_no": "7A",
        **({"h_orgtk_sale_dt": sold} if sold else {}),
    }
    return Ticket({"ticket_list": [{"train_info": [info]}]})


def korail():
    service = KorailService()
    service._logged_in = True
    service._modern_client = MagicMock()
    service._korail_instance = MagicMock()
    return service


STANDBY = {
    "id": "PNR015",
    "train_no": "015",
    "dep_date": DEPARTS,
    "dep_time": "072700",
    "applied_on": TODAY,
}


def state(service, *standbys):
    return service.waitlist_states(list(standbys or [STANDBY]))


def test_a_listed_standby_without_seats_is_waiting():
    service = korail()
    service._modern_client.get_reservation_history.return_value = history(history_row("PNR015"))
    service._modern_client.get_ticket_reservation_detail.return_value = detail()

    assert state(service) == {"PNR015": {"state": "waiting"}}


def test_a_waiting_standby_as_korail_actually_sent_it_is_waiting_with_no_deadline():
    # Recorded from a real standby on 2026-10-20: the list row carries a zero payment date and
    # the detail one seat row with car 0 and no seat number.
    service = korail()
    row = {**history_row("PNR015"), "h_ntisu_lmt_dt": "00000000", "h_ntisu_lmt_tm": "235959"}
    service._modern_client.get_reservation_history.return_value = history(
        {**row, "h_rsv_tp_cd": "08", "h_jrny_sqno": "0001", "h_rsv_chg_no": "000"}
    )
    service._modern_client.get_ticket_reservation_detail.return_value = detail((0, ""))

    assert state(service) == {"PNR015": {"state": "waiting"}}
    assert MobileGateway._deadline(("00000000", "235959")) is None


def test_a_listed_standby_with_seats_is_assigned_with_its_deadline():
    service = korail()
    service._modern_client.get_reservation_history.return_value = history(history_row("PNR015"))
    service._modern_client.get_ticket_reservation_detail.return_value = detail(("03", "7A"))

    assert state(service) == {
        "PNR015": {"state": "assigned", "seats": ["3호차 7A"], "deadline": (DEPARTS, "060000")}
    }


def test_one_list_read_answers_every_standby():
    service = korail()
    service._modern_client.get_reservation_history.return_value = history(
        history_row("PNR015"), history_row("PNR017", deadline=False)
    )
    service._modern_client.get_ticket_reservation_detail.return_value = detail()

    answers = state(service, STANDBY, {**STANDBY, "id": "PNR017"})

    assert answers == {"PNR015": {"state": "waiting"}, "PNR017": {"state": "waiting"}}
    service._modern_client.get_reservation_history.assert_called_once()
    service._korail_instance.tickets.assert_not_called()


def test_an_empty_reservation_list_is_an_answer():
    service = korail()
    empty = {"strResult": "FAIL", "h_msg_cd": "P100", "h_msg_txt": "조회 결과가 없습니다."}
    service._modern_client.get_reservation_history.return_value = (
        parse_reservation_history_response(empty)
    )
    service._korail_instance.tickets.return_value = []

    assert state(service) == {"PNR015": {"state": "ended"}}


@pytest.mark.parametrize(
    ("sold", "applied", "expected"),
    [
        (TODAY, "20200101", "paid"),
        # Same day: the list has no time of sale, so a ticket bought earlier cannot be ruled out.
        (TODAY, TODAY, "ticketed"),
        ("20200101", TODAY, "ended"),
        (None, TODAY, "ticketed"),
    ],
)
def test_a_standby_gone_from_the_list_is_paid_only_by_a_ticket_sold_after_the_day_applied(
    sold, applied, expected
):
    service = korail()
    service._modern_client.get_reservation_history.return_value = history(history_row("OTHER"))
    service._korail_instance.tickets.return_value = [ticket(sold)]

    assert state(service, {**STANDBY, "applied_on": applied}) == {"PNR015": {"state": expected}}


def test_a_standby_this_account_may_not_own_is_only_looked_for_in_its_list():
    service = korail()
    service._modern_client.get_reservation_history.return_value = history(history_row("OTHER"))

    assert state(service, {**STANDBY, "listed_only": True}) == {"PNR015": {"state": "unlisted"}}
    service._korail_instance.tickets.assert_not_called()


def test_the_member_number_comes_from_the_login_session():
    service = korail()
    service._modern_client.session.current.member_card_no = "1234567890"

    assert service.member_number() == "1234567890"


def test_a_list_that_could_not_be_read_is_a_failure_not_an_ending():
    service = korail()
    service._modern_client.get_reservation_history.side_effect = KorailTransportError("503")

    with pytest.raises(KorailTransportError):
        state(service)


def test_a_seat_read_that_failed_is_unknown_for_that_standby_only():
    service = korail()
    service._modern_client.get_reservation_history.return_value = history(history_row("PNR015"))
    service._modern_client.get_ticket_reservation_detail.side_effect = KorailTransportError("503")

    assert state(service) == {"PNR015": {"state": "unknown"}}


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


def test_the_check_interval_stays_between_ten_minutes_and_a_day(monkeypatch, tmp_path):
    monkeypatch.setenv("MOBILE_SECRET", "s" * 40)
    monkeypatch.setenv("MOBILE_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MOBILE_DATA_DIR", str(tmp_path))
    assert MobileConfig.from_env().waitlist_check_minutes == 10
    monkeypatch.setenv("MOBILE_WAITLIST_CHECK_MINUTES", "30")
    assert MobileConfig.from_env().waitlist_check_minutes == 30
    for wrong in ("5", "0", "soon", "nan", "inf", "100000"):
        monkeypatch.setenv("MOBILE_WAITLIST_CHECK_MINUTES", wrong)
        with pytest.raises(ValueError, match="at least 10"):
            MobileConfig.from_env()
    assert mobile_config.WAITLIST_CHECK_MIN_MINUTES == 10


# ---------------------------------------------------------------- MCP and the watch thread


def test_get_status_tells_an_agent_about_standbys_as_they_are():
    from korail_bot.mobile.mcp import _status

    result = _status(
        {
            "running": None,
            "scheduled": None,
            "pending": [],
            "waitlists": [
                {"trainInfo": "KTX 015", "state": "waiting", "checkFailed": True},
                {"trainInfo": "KTX 017", "state": "assigned"},
                {"trainInfo": "KTX 019", "state": "waiting", "checkError": "login"},
                {"trainInfo": "KTX 021", "state": "waiting", "otherAccount": True},
            ],
        }
    )

    notes = " ".join(result["notes"])
    assert "아무" not in notes and "없어요." not in result["notes"][0]
    assert "10분 이상 간격" in notes and "끝났다는 뜻은 아니에요" in notes
    assert "배정된 것 같아요(확정 아님)" in notes
    assert "다시 연결" in notes and "예약 목록에서 찾지 못해" in notes


def test_the_watch_thread_starts_once_and_stops_on_request():
    from korail_bot.mobile.runtime import WaitlistWatch

    gateway = SimpleNamespace(waitlist_check_minutes=10, check_waitlists=MagicMock())
    watch = WaitlistWatch(gateway)
    watch.start()
    first = watch._thread
    watch.start()

    assert watch._thread is first
    watch.stop()
    first.join(timeout=2)
    assert not first.is_alive() and watch.stopping()


# ---------------------------------------------------------------- a reply like a block

BLOCKED_REPLY = {"h_msg_cd": "", "h_msg_txt": "", "rtnMsg": "비정상적인 접근입니다."}


def blocked_reply():
    """What the library raises on Korail's reply without strResult."""
    try:
        parse_reservation_history_response(BLOCKED_REPLY)
    except Exception as exc:
        return exc
    raise AssertionError("the parser took a reply without strResult")


def test_a_look_answered_like_a_block_stops_every_standby_with_nothing_more_asked(app):
    for train in ("015", "017"):
        apply(app, train)
    app.rail.waitlist_states.side_effect = blocked_reply()
    logins = app.rail.login.call_count
    later = utc_now() + LATER

    app.gateway.check_waitlists(now=later)
    app.gateway.check_waitlists(now=later + timedelta(minutes=11))
    app.gateway.check_waitlists(now=later + timedelta(hours=3))

    assert looks(app) == 1
    # One login for the look itself; none again to "recover" from the block.
    assert app.rail.login.call_count == logins + 1
    assert {e["checkError"] for e in status(app)["waitlists"]} == {"blocked"}

    # Connecting the account again is the person saying to try again, as starting a new watch is.
    app.rail.waitlist_states.side_effect = None
    app.rail.waitlist_states.return_value = {
        "PNR015": {"state": "waiting"},
        "PNR017": {"state": "waiting"},
    }
    app.http.post(
        "/api/mobile/register",
        headers=app.headers,
        json={"username": "01012345678", "password": "rail password"},
    )
    assert app.gateway.check_waitlists(now=later + timedelta(hours=4)) == 2


def test_a_login_answered_like_a_block_stops_the_look(app):
    apply(app)
    app.rail.login.return_value = False
    app.rail.login_blocked = True

    app.gateway.check_waitlists(now=utc_now() + LATER)
    app.gateway.check_waitlists(now=utc_now() + LATER * 3)

    assert status(app)["waitlists"][0]["checkError"] == "blocked"
    assert looks(app) == 0


def test_an_application_answered_like_a_block_records_nothing_and_says_so(app):
    app.rail.request_waitlist.side_effect = blocked_reply()

    answered = apply(app)

    assert answered.status_code == 503
    assert "접속을 막은 것 같아요" in answered.json["error"]
    assert status(app)["waitlists"] == []


def test_a_withdrawal_answered_like_a_block_is_not_sent_and_pauses_the_looks(app):
    apply(app)
    app.rail.waitlist_states.side_effect = blocked_reply()

    answered = cancel(app, "PNR015")

    assert answered.status_code == 503
    assert "코레일 앱에서 직접 취소" in answered.json["error"]
    app.rail.cancel_waitlist.assert_not_called()
    assert status(app)["waitlists"][0]["checkError"] == "blocked"


def test_a_seat_read_answered_like_a_block_stops_before_the_next_standby():
    from korail_bot.services.rail_service import SearchBlockedError

    service = korail()
    service._modern_client.get_reservation_history.return_value = history(
        history_row("PNR015"), history_row("PNR017")
    )
    service._modern_client.get_ticket_reservation_detail.side_effect = blocked_reply()

    with pytest.raises(SearchBlockedError):
        state(service, STANDBY, {**STANDBY, "id": "PNR017"})

    assert service._modern_client.get_ticket_reservation_detail.call_count == 1


def test_get_status_says_a_standby_look_was_stopped_by_a_block():
    from korail_bot.mobile.mcp import _status

    result = _status(
        {
            "running": None,
            "scheduled": None,
            "pending": [],
            "waitlists": [{"trainInfo": "KTX 015", "state": "waiting", "checkError": "blocked"}],
        }
    )

    assert "접속을 막은 것 같아" in " ".join(result["notes"])


# ---------------------------------------------------------------- one account, one verdict


def test_a_watch_that_ended_on_a_block_stops_the_standby_look_without_asking(app):
    apply(app)
    # Connected a while ago; the watch was blocked since.
    app.storage.redis.set(f"waitlist_connected:{app.owner}", "1")
    app.storage.mark_search_ended(app.owner, "blocked", 4242)
    logins = app.rail.login.call_count

    app.gateway.check_waitlists(now=utc_now() + LATER)

    assert looks(app) == 0
    assert app.rail.login.call_count == logins
    assert status(app)["waitlists"][0]["checkError"] == "blocked"


def test_a_block_in_the_same_second_as_the_connection_still_stops_the_look(app):
    apply(app)
    # The fixture connected the account just now; search_ended keeps whole seconds.
    app.storage.mark_search_ended(app.owner, "blocked", 4242)

    app.gateway.check_waitlists(now=utc_now() + LATER)

    assert looks(app) == 0
    assert status(app)["waitlists"][0]["checkError"] == "blocked"


def test_a_block_older_than_the_last_connection_does_not_stop_the_look(app, monkeypatch):
    apply(app)
    # The block is noted a few seconds before the person connects again, as it would be after
    # they read the notice: a block in the very same second counts as after the connection.
    noted_at = time.time() - 5
    with monkeypatch.context() as patch:
        patch.setattr(redis_storage, "time", SimpleNamespace(time=lambda: noted_at))
        app.storage.mark_search_ended(app.owner, "blocked", 4242)
    later = utc_now() + LATER
    app.storage.redis.set(f"waitlist_connected:{app.owner}", "1")
    app.gateway.check_waitlists(now=later)
    assert status(app)["waitlists"][0]["checkError"] == "blocked"
    # Connecting the account again is the person's fresh start.
    app.http.post(
        "/api/mobile/register",
        headers=app.headers,
        json={"username": "01012345678", "password": "rail password"},
    )

    assert app.gateway.check_waitlists(now=later + timedelta(hours=1)) == 1


def test_an_application_answered_like_a_block_stops_the_other_standbys_too(app):
    apply(app, "015")
    app.rail.request_waitlist.side_effect = blocked_reply()

    assert apply(app, "017").status_code == 503

    assert status(app)["waitlists"][0]["checkError"] == "blocked"


def test_a_withdrawal_whose_login_is_answered_like_a_block_is_a_block(app):
    apply(app)
    app.rail.login.return_value = False
    app.rail.login_blocked = True

    answered = cancel(app, "PNR015")

    assert answered.status_code == 503
    assert "코레일 앱에서 직접 취소" in answered.json["error"]
    assert status(app)["waitlists"][0]["checkError"] == "blocked"
    app.rail.cancel_waitlist.assert_not_called()


def test_a_login_gone_again_right_after_a_fresh_one_twice_in_a_row_stops_like_a_block(app):
    apply(app)
    app.rail.waitlist_states.side_effect = KorailSessionExpiredError("P058", "로그인이 필요합니다")
    later = utc_now() + LATER

    for passes in range(6):
        app.gateway.check_waitlists(now=later + timedelta(minutes=11 * passes))

    # Two passes (each: ask, log in again, ask), then nothing more.
    assert looks(app) == 4
    assert status(app)["waitlists"][0]["checkError"] == "blocked"


def test_a_standby_is_not_applied_for_without_the_korail_member(app):
    app.rail.member_number.side_effect = lambda: None

    answered = apply(app)

    assert answered.status_code == 503
    assert "회원 정보" in answered.json["error"]
    app.rail.request_waitlist.assert_not_called()


def test_an_untagged_standby_is_judged_only_by_this_accounts_list(app):
    apply(app)
    entries = app.storage.get_waitlists(app.owner)
    entries[0].pop("account")
    app.storage.save_waitlists(app.owner, entries)
    app.owned["PNR015"] = "MSOMEONE"

    app.gateway.check_waitlists(now=utc_now() + LATER)

    asked = app.rail.waitlist_states.call_args.args[0][0]
    assert asked["listed_only"] is True
    (entry,) = status(app)["waitlists"]
    # Not in this account's list: neither "ended" nor this account's to withdraw.
    assert entry["state"] == "waiting" and entry["otherAccount"] is True
    assert cancel(app, "PNR015").status_code == 409


def test_a_look_without_the_member_is_a_failed_look_and_marks_nothing_missing(app):
    apply(app)
    app.rail.member_number.side_effect = lambda: None

    app.gateway.check_waitlists(now=utc_now() + LATER)

    stored = app.storage.get_waitlists(app.owner)[0]
    assert stored["checkFailed"] is True and "mismatchSeen" not in stored
    assert looks(app) == 0


def test_a_standby_missing_from_this_accounts_list_is_looked_for_again_a_day_later(app):
    apply(app)
    app.http.post(
        "/api/mobile/register",
        headers=app.headers,
        json={"username": "01099998888", "password": "other"},
    )
    later = utc_now() + LATER

    app.gateway.check_waitlists(now=later)
    app.gateway.check_waitlists(now=later + timedelta(hours=12))
    assert looks(app) == 1

    app.gateway.check_waitlists(now=later + timedelta(hours=25))
    assert looks(app) == 2
    assert app.rail.waitlist_states.call_args.args[0][0]["listed_only"] is True


def test_a_full_list_of_this_accounts_standbys_is_refused_before_logging_in(app):
    for number in range(WAITLIST_LIMIT):
        apply(app, f"0{number}1")
    logins = app.rail.login.call_count

    assert apply(app, "099").status_code == 409
    assert app.rail.login.call_count == logins


def test_the_operator_deletes_an_account_with_standbys_only_when_told_the_user_was_informed(
    app, monkeypatch
):
    from korail_bot.mobile.__main__ import delete_account as delete_account_cli

    apply(app)
    monkeypatch.setattr("korail_bot.mobile.runtime.MobileRuntime", lambda config, push: app.runtime)
    monkeypatch.setattr(app.storage, "close", lambda: None)

    with pytest.raises(ValueError, match="--acknowledge-waitlists") as refused:
        delete_account_cli(app.runtime.config, "alice")
    assert "코레일 앱에서 예약 대기를 관리하라고 안내했는지" in str(refused.value)
    assert app.runtime.identity.find_user("alice") is not None

    assert delete_account_cli(app.runtime.config, "alice", acknowledge_waitlists=True).startswith(
        "Deleted alice"
    )
    assert app.runtime.identity.find_user("alice") is None
