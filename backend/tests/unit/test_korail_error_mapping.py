"""
How the service reads what the real korail_mobile_api client raises on Korail's replies.

The client runs over a fake transport, so the exception types, codes and kept replies are the
library's own: a lookup that failed, a lookup that found nothing and a reply that looks like a block
are told apart by what the library gives, not by what a hand-built exception is made to say.
"""

import re

import httpx
import pytest
from korail2 import TrainType
from korail_mobile_api import KorailClient, KorailSession
from korail_mobile_api.errors import (
    KorailAppUpdateRequiredError,
    KorailDynaPathError,
    KorailServiceUnavailableError,
    KorailSoldOutError,
    KorailTransportError,
    classify_app_error,
)

from korail_bot.services import korail_service
from korail_bot.services.korail_service import (
    KorailService,
    _describe_reply,
    _reply_excerpt,
    korail_reply_code,
)

from .korail_replies import BLOCKED_REPLY


def _service_answering(monkeypatch, body, status=200) -> KorailService:
    """A logged-in KorailService whose current client gets `body` for every request."""

    def reply(request):
        return httpx.Response(status, json=body)

    real = KorailClient
    monkeypatch.setattr(
        korail_service,
        "KorailClient",
        lambda config: real(config, transport=httpx.MockTransport(reply)),
    )
    service = KorailService()
    client = service._build_modern_client()
    client.session.current = KorailSession(
        jsessionid="synthetic-session", member_no="1234567890", raw={}
    )
    service._modern_client = client
    service._logged_in = True
    return service


def _list(service):
    return service.search_waitlist_trains(
        dep_date="20261101",
        src_locate="서울",
        dst_locate="부산",
        dep_time="060000",
        max_dep_time="2400",
        train_type=TrainType.KTX,
        passenger_count=1,
    )


def _failure(service) -> Exception:
    with pytest.raises(Exception) as raised:
        _list(service)
    return raised.value


def fail(code, text="synthetic"):
    return {"strResult": "FAIL", "h_msg_cd": code, "h_msg_txt": text}


def test_nothing_running_in_the_window_is_an_empty_list_not_a_failure(monkeypatch):
    assert _list(_service_answering(monkeypatch, fail("WRG000000"))) == []


def test_a_client_korail_turns_away_is_a_failed_lookup_with_its_code(monkeypatch):
    # 2026-10-21: the stale DynaPath token got -8204 "update the app" on every train search.
    service = _service_answering(monkeypatch, fail("-8204", "최신 버전으로 업데이트"))

    error = _failure(service)

    assert isinstance(error, KorailAppUpdateRequiredError)
    assert error.code == "-8204"
    assert not service.looks_blocked(error)


def test_a_sold_out_answer_is_not_mistaken_for_an_empty_window_or_a_block(monkeypatch):
    service = _service_answering(monkeypatch, fail("ERR211161", "매진"))

    error = _failure(service)

    assert isinstance(error, KorailSoldOutError)
    assert not service.looks_blocked(error)


def test_korail_down_is_a_failed_lookup_not_a_block(monkeypatch):
    service = _service_answering(monkeypatch, fail("S000", "시스템 점검"))

    error = _failure(service)

    assert isinstance(error, KorailServiceUnavailableError)
    assert not service.looks_blocked(error)


def test_an_http_error_whose_body_has_no_str_result_is_not_a_block(monkeypatch):
    service = _service_answering(monkeypatch, {"error": "bad gateway"}, status=502)

    error = _failure(service)

    assert isinstance(error, KorailTransportError)
    assert not service.looks_blocked(error)


@pytest.mark.parametrize(
    ("refusal", "code"),
    [
        ({"code": "-2000"}, "-2000"),
        ({"strResult": "FAIL", "h_msg_cd": "", "h_msg_txt": "", "dynaCd": -8202}, "-8202"),
    ],
)
def test_a_dynapath_refusal_is_a_failed_lookup_that_stops_the_watch_and_keeps_its_code(
    monkeypatch, refusal, code
):
    service = _service_answering(monkeypatch, refusal)

    error = _failure(service)

    assert isinstance(error, KorailDynaPathError)
    assert service.looks_blocked(error)
    assert korail_reply_code(error) == code


def test_the_code_of_an_error_is_korails_result_code_first():
    assert korail_reply_code(classify_app_error("-8204", "x")) == "-8204"
    assert korail_reply_code(KorailTransportError("no reply")) is None


def test_a_search_reply_without_str_result_is_a_block(monkeypatch):
    service = _service_answering(monkeypatch, BLOCKED_REPLY)

    error = _failure(service)

    assert service.looks_blocked(error)


def test_a_seat_map_read_answered_without_str_result_is_a_block(monkeypatch):
    service = _service_answering(monkeypatch, BLOCKED_REPLY)
    train = _train_row()

    with pytest.raises(Exception) as raised:
        service._modern_client.get_seat_cars(train)

    assert service.looks_blocked(raised.value)


def _train_row():
    from korail_mobile_api import TrainSummary

    return TrainSummary.from_raw(
        {
            "h_trn_no": "00101",
            "h_trn_gp_cd": "100",
            "h_trn_clsf_cd": "00",
            "h_dpt_rs_stn_cd": "0001",
            "h_arv_rs_stn_cd": "0020",
            "h_dpt_dt": "20261101",
            "h_run_dt": "20261101",
            "h_dpt_tm": "070000",
            "h_arv_tm": "094100",
            "h_dpt_stn_run_ordr": "000001",
            "h_arv_stn_run_ordr": "000007",
            "h_seat_att_cd": "015",
        }
    )


def test_the_excerpt_of_a_reply_keeps_what_tells_a_block_from_an_outage():
    body = {**BLOCKED_REPLY, "strResult": "FAIL", "h_msg_cd": "S000", "code": -2000}

    assert _reply_excerpt(body) == {
        "strResult": "FAIL",
        "h_msg_cd": "S000",
        "code": "-2000",
        "rtnMsg": "비정상적인 접근입니다.",
    }


@pytest.mark.parametrize(
    ("body", "excerpt"),
    [
        # The diagnostics production relies on stay readable from codes and known notices.
        (
            {"strResult": "FAIL", "h_msg_cd": "-8204", "h_msg_txt": "최신 버전으로 업데이트"},
            {"strResult": "FAIL", "h_msg_cd": "-8204", "h_msg_txt": "최신 버전으로 업데이트"},
        ),
        (
            {"strResult": "FAIL", "h_msg_cd": "S000", "h_msg_txt": "시스템 점검 중입니다."},
            {"strResult": "FAIL", "h_msg_cd": "S000"},
        ),
        (BLOCKED_REPLY, {"rtnMsg": "비정상적인 접근입니다."}),
        ({"code": "-2000"}, {"code": "-2000"}),
        ({"dynaCd": -8202}, {}),
    ],
)
def test_the_excerpt_tells_update_required_maintenance_and_a_restricted_account_apart(
    body, excerpt
):
    assert _reply_excerpt(body) == excerpt


# Strings a reply could quote the request in. Each carries a marker that must never be logged.
LEAKING_TEXTS = [
    '{"sid":"SYNTHETIC_SESSION"}',
    "https://h/p?txt%50wd=SYNTHETIC_PASSWORD",
    "https://h/p?next=txtPwd%3DSYNTHETIC_PASSWORD",
    "txtPwd=SYNTHETIC_HEAD&SYNTHETIC_TAIL",
    # The round before: a name=value pair, a cookie, a bearer token, a query, a card number.
    "txtPwd=SYNTHETIC_PASSWORD JSESSIONID=SYNTHETIC_SESSION",
    "https://smart.letskorail.com/x?txtPwd=SYNTHETIC_PASSWORD&sid=SYNTHETIC_SESSION&ok=1",
    '{"txtPwd": "SYNTHETIC_PASSWORD", "strMbCrdNo":"SYNTHETIC_MEMBER"}',
    "Cookie: JSESSIONID=SYNTHETIC_SESSION; other=1",
    "Set-Cookie: JSESSIONID=SYNTHETIC_SESSION; Path=/",
    "Authorization: Bearer SYNTHETIC_TOKEN",
    "비정상적인 접근입니다. SYNTHETIC_TAIL",
    "카드 4111 1111 1111 1111 SYNTHETIC_CARD",
]
MARKER = re.compile(r"SYNTHETIC_[A-Z]+|4111")


@pytest.mark.parametrize("text", LEAKING_TEXTS)
@pytest.mark.parametrize("field", ["rtnMsg", "h_msg_txt", "message", "msg"])
def test_no_free_text_of_a_logged_reply_reaches_the_log(text, field):
    response = httpx.Response(
        200, json={"h_msg_cd": "", field: text, "txtPwd": "SYNTHETIC_PASSWORD"}
    )

    said = _describe_reply(response, body=True)

    assert not MARKER.search(said), said


@pytest.mark.parametrize("text", LEAKING_TEXTS)
def test_no_nested_or_code_field_text_of_a_logged_reply_reaches_the_log(text):
    response = httpx.Response(
        200,
        json={
            "strResult": text,
            "h_msg_cd": text,
            "h_rslt_cd": text,
            "code": text,
            "nested": {"rtnMsg": text},
            "rows": [text],
        },
    )

    said = _describe_reply(response, body=True)

    assert not MARKER.search(said), said


@pytest.mark.parametrize(
    "code", ["-8204", "S000", "WRG000000", "ERI411321", "WRR800029", "P100", "-2000", "SEMGTK"]
)
def test_the_codes_korail_uses_are_logged_as_they_are(code):
    assert _reply_excerpt({"h_msg_cd": code}) == {"h_msg_cd": code}


@pytest.mark.parametrize(
    "value",
    [
        "4111111111111111",
        "4111 1111 1111 1111",
        4111111111111111,
        "txtPwd=SYNTHETIC_PASSWORD",
        "a=b",
        "A" * 17 + "1",
        "s000",
        "S000 ",
        True,
    ],
)
def test_a_code_field_that_does_not_look_like_a_code_is_never_logged(value):
    excerpt = _reply_excerpt({"h_msg_cd": value})

    if value is True:
        assert excerpt == {}
    else:
        assert excerpt == {"h_msg_cd": "<redacted>"}


def test_a_known_notice_is_logged_only_when_it_is_exactly_that_text():
    assert _reply_excerpt({"rtnMsg": " 비정상적인 접근입니다. "}) == {
        "rtnMsg": "비정상적인 접근입니다."
    }
    assert _reply_excerpt({"rtnMsg": "비정상적인 접근입니다. 계정 SYNTHETIC_TAIL"}) == {}


def test_a_kept_reply_is_described_with_its_keys_and_its_codes():
    response = httpx.Response(200, json={**BLOCKED_REPLY, "txtPwd": "SYNTHETIC_PASSWORD"})

    said = _describe_reply(response, body=True)

    assert "keys=['h_msg_cd', 'h_msg_txt', 'rtnMsg', 'txtPwd']" in said
    assert "비정상적인 접근입니다." in said
    assert not MARKER.search(said)


def test_a_login_reply_is_described_by_its_keys_alone():
    response = httpx.Response(200, json={"strMbCrdNo": "member-card-value"})

    assert "member-card-value" not in _describe_reply(response, body=False)


# ---------------------------------------------- the standby request, over the real client

TICKET_RESERVATION = "/classes/com.korail.mobile.certification.TicketReservation"
RESERVATION_WAIT = "/classes/com.korail.mobile.reservationWait.ReservationWait"
CANCEL_CHECK = "/classes/com.korail.mobile.reservationCancel.ReservationCancelChk"
CANCEL_FIRST = "/classes/com.korail.mobile.reservationCancel.ReservationCancel"

SUCCESS = {"strResult": "SUCC", "h_msg_cd": "IRZ000001", "h_msg_txt": ""}
STANDBY_HOLD = {
    **SUCCESS,
    "h_pnr_no": "SYNTHETIC-PNR",
    "h_jrny_cnt": "0001",
    "h_wct_no": "SYN",
    "h_tmp_job_sqno1": "000001",
    "h_tmp_job_sqno2": "000002",
}


def _standby_train():
    from dataclasses import replace

    return replace(
        _train_row(),
        wait_reservation_flag=" 9",
        general_reservation_code="13",
        special_reservation_code="13",
        departure_construction_order="000001",
        arrival_construction_order="000007",
    )


def _service_answering_by_path(monkeypatch, answers) -> tuple[KorailService, list[str]]:
    """A logged-in KorailService whose client answers by request path; returns the paths asked."""
    asked: list[str] = []

    def reply(request):
        asked.append(request.url.path)
        return httpx.Response(200, json=answers[request.url.path])

    real = KorailClient
    monkeypatch.setattr(
        korail_service,
        "KorailClient",
        lambda config: real(config, transport=httpx.MockTransport(reply)),
    )
    service = KorailService()
    client = service._build_modern_client()
    client.session.current = KorailSession(
        jsessionid="synthetic-session", member_no="1234567890", raw={}
    )
    service._modern_client = client
    service._logged_in = True
    return service, asked


def test_a_standby_is_a_hold_then_its_options_and_nothing_is_cancelled(monkeypatch):
    service, asked = _service_answering_by_path(
        monkeypatch, {TICKET_RESERVATION: STANDBY_HOLD, RESERVATION_WAIT: SUCCESS}
    )

    hold = service._register_standby(_standby_train(), 1)

    assert hold.pnr_no == "SYNTHETIC-PNR"
    assert asked == [TICKET_RESERVATION, RESERVATION_WAIT]


def test_a_standby_whose_options_fail_is_released_with_the_one_request_1_1_1_sent(monkeypatch):
    service, asked = _service_answering_by_path(
        monkeypatch,
        {
            TICKET_RESERVATION: STANDBY_HOLD,
            RESERVATION_WAIT: {"strResult": "FAIL", "h_msg_cd": "WRR800029", "h_msg_txt": "x"},
            CANCEL_CHECK: SUCCESS,
        },
    )

    with pytest.raises(Exception) as raised:
        service._register_standby(_standby_train(), 1)

    assert raised.value.code == "WRR800029"
    # ReservationCancelChk alone: no ReservationCancel check before it (check_first=False).
    assert asked == [TICKET_RESERVATION, RESERVATION_WAIT, CANCEL_CHECK]
    assert CANCEL_FIRST not in asked


def test_withdrawing_a_standby_sends_the_one_cancel_request(monkeypatch):
    service, asked = _service_answering_by_path(monkeypatch, {CANCEL_CHECK: SUCCESS})

    assert service.cancel_waitlist("SYNTHETIC-PNR", "0001") is True

    assert asked == [CANCEL_CHECK]
