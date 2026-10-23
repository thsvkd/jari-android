"""
How the service reads what the real korail_mobile_api client raises on Korail's replies.

The client runs over a fake transport, so the exception types, codes and kept replies are the
library's own: a lookup that failed, a lookup that found nothing and a reply that looks like a block
are told apart by what the library gives, not by what a hand-built exception is made to say.
"""

import json

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
)

from korail_bot.services import korail_service
from korail_bot.services.korail_service import KorailService, _describe_reply, _redact_reply

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


def test_a_dynapath_refusal_is_its_own_verdict_not_a_block(monkeypatch):
    service = _service_answering(monkeypatch, {"code": "-2000"})

    error = _failure(service)

    assert isinstance(error, KorailDynaPathError)
    assert not service.looks_blocked(error)


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


def test_the_log_of_a_reply_without_str_result_keeps_what_helps_and_hides_what_identifies():
    body = {
        "h_msg_cd": "",
        "h_msg_txt": "회원 01012345678 은 접근할 수 없습니다",
        "rtnMsg": "비정상적인 접근입니다.",
        "strMbCrdNo": "member-card-value",
        "txtPwd": "password-value",
        "nested": {"pnrNo": "R123456", "note": "카드 4111 1111 1111 1111 입니다"},
    }

    shown = json.dumps(_redact_reply(body), ensure_ascii=False)

    assert "비정상적인 접근입니다." in shown
    for hidden in (
        "01012345678",
        "member-card-value",
        "password-value",
        "R123456",
        "4111 1111 1111 1111",
    ):
        assert hidden not in shown


def test_a_kept_reply_is_described_with_its_keys_and_a_redacted_excerpt():
    response = httpx.Response(200, json={**BLOCKED_REPLY, "txtPwd": "password-value"})

    said = _describe_reply(response, body=True)

    assert "keys=['h_msg_cd', 'h_msg_txt', 'rtnMsg', 'txtPwd']" in said
    assert "비정상적인 접근입니다." in said
    assert "password-value" not in said


def test_a_login_reply_is_described_by_its_keys_alone():
    response = httpx.Response(200, json={"strMbCrdNo": "member-card-value"})

    assert "member-card-value" not in _describe_reply(response, body=False)
