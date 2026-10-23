"""
A watch ends on purpose, and says why, instead of knocking on Korail forever.

pit5, 2026-10-17..19: one watch searched about once a second for 30 hours (101,000 searches,
65 logins). From 2026-10-19 02:57 UTC every reply came back without strResult - korail2 raised
KeyError: 'strResult' - and the watch kept asking, backed off to a minute, for four more hours.
The account's owner was then told by the Korail app that macro tools may get it restricted.

Three endings came out of that, each told apart from a booking, a sold-out train and a crash:
"blocked" on the first reply that looks like a block, "unavailable" once failures of any kind
have gone on for SEARCH_FAILURE_STOP_SECONDS, and "expired" once WATCH_MAX_SECONDS have passed
since the watch first started.
"""

import importlib.util
import json
import logging
import os
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import fakeredis
import httpx
import pytest
import requests
from korail2 import TrainType

from korail_bot.config.settings import settings
from korail_bot.mobile import mcp
from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.process import MobileReservationService
from korail_bot.mobile.storage import MobileStorage
from korail_bot.mobile.worker import apply_result
from korail_bot.models import RunningReservation, TrainSearchParams
from korail_bot.services import korail_service as korail_service_module
from korail_bot.services import rail_service as rail_service_module
from korail_bot.services.korail_service import KorailService
from korail_bot.services.rail_service import (
    SearchBlockedError,
    SearchGaveUp,
    SearchUnavailableError,
)
from korail_bot.utils.timezone import utc_now

from .korail_replies import blocked_reply

CHAT = -100
PASSWORD = "rail-password-secret"
# korail2's search_train reads datetime.utcnow() on every call, deprecated since 3.12.
KORAIL2_UTCNOW = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")


class _Replies(requests.adapters.BaseAdapter):
    """korail2's HTTP session answered from a list, without a socket. Counts what was sent."""

    def __init__(self, *bodies):
        super().__init__()
        self.bodies = list(bodies)
        self.sent = 0

    def send(self, request, **kwargs):
        self.sent += 1
        body = self.bodies[min(self.sent, len(self.bodies)) - 1]
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body, ensure_ascii=False).encode()
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


def _logged_in_service(*bodies) -> tuple[KorailService, _Replies]:
    """A KorailService with a real korail2 client whose replies are the given JSON bodies."""
    service = KorailService()
    client = service._build_client("01000000000", PASSWORD)
    replies = _Replies(*bodies)
    client._session.mount("https://", replies)
    client.logined = True
    service._korail_instance = client
    service._logged_in = True
    return service, replies


def _search(service):
    return service.search_trains("20261003", "서울", "부산", "070000", train_type=TrainType.KTX)


def _loop(service):
    return service.search_and_reserve_loop(
        dep_date="20261003", src_locate="서울", dst_locate="부산", passenger_count=1
    )


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(rail_service_module.time, "sleep", waits.append)
    return waits


# ==================== Defaults ====================


def test_the_defaults_search_every_three_seconds_and_bound_a_watch(monkeypatch):
    for name in (
        "SEARCH_INTERVAL",
        "SEARCH_INTERVAL_JITTER",
        "SEARCH_FAILURE_BACKOFF_CAP",
        "SEARCH_FAILURE_STOP_SECONDS",
        "WATCH_MAX_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    # A fresh copy of the module: the shared one was read with whatever the test run's env had.
    import korail_bot.config.settings as shared

    spec = importlib.util.spec_from_file_location("settings_defaults", shared.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)

    assert fresh.Settings.KORAIL_SEARCH_INTERVAL == 3.0
    assert fresh.Settings.KORAIL_SEARCH_INTERVAL_JITTER == 0.4
    # 20 x 3s: the longest gap between failed passes stays the minute it was at 60 x 1s.
    assert fresh.Settings.KORAIL_FAILURE_BACKOFF_CAP * fresh.Settings.KORAIL_SEARCH_INTERVAL == 60
    assert fresh.Settings.KORAIL_FAILURE_STOP_SECONDS == 600
    assert fresh.Settings.WATCH_MAX_SECONDS == 3600


def test_a_pass_waits_between_one_point_eight_and_four_point_two_seconds(monkeypatch):
    monkeypatch.setattr(settings, "KORAIL_SEARCH_INTERVAL", 3.0)
    monkeypatch.setattr(settings, "KORAIL_SEARCH_INTERVAL_JITTER", 0.4)
    service = KorailService()

    waits = [service.next_interval() for _ in range(500)]

    assert all(1.8 <= wait <= 4.2 for wait in waits)
    assert min(waits) < 2.2 and max(waits) > 3.8


# ==================== Blocked ====================


@KORAIL2_UTCNOW
def test_a_reply_without_str_result_is_a_block_not_a_failed_search(caplog):
    service, replies = _logged_in_service(
        {"h_msg_cd": "", "h_msg_txt": "", "rtnMsg": "비정상적인 접근입니다."}
    )

    with caplog.at_level(logging.ERROR, logger="korail_bot.services.korail_service"):
        with pytest.raises(SearchBlockedError) as raised:
            _search(service)

    assert isinstance(raised.value.__cause__, KeyError)
    assert replies.sent == 1
    said = caplog.text
    # The top-level keys and a short excerpt, for whoever reads the log next time.
    assert "HTTP 200 DynaPath-Result=None keys=['h_msg_cd', 'h_msg_txt', 'rtnMsg']" in said
    assert "비정상적인 접근입니다." in said
    assert PASSWORD not in said


@KORAIL2_UTCNOW
def test_korail_maintenance_is_a_failed_search_not_a_block():
    service, _ = _logged_in_service(
        {"strResult": "FAIL", "h_msg_cd": "S000", "h_msg_txt": "시스템 점검 중입니다."}
    )

    with pytest.raises(SearchUnavailableError) as raised:
        _search(service)

    assert not isinstance(raised.value, SearchBlockedError)
    assert "S000" in str(raised.value)
    assert not service.is_block_signal(raised.value)


@KORAIL2_UTCNOW
def test_the_first_blocked_reply_ends_the_watch_without_asking_again(no_sleep):
    service, replies = _logged_in_service({"h_msg_cd": "", "h_msg_txt": ""})
    service.reserve_train = Mock()

    with pytest.raises(SearchGaveUp) as raised:
        _loop(service)

    assert raised.value.reason == "blocked"
    # One request, no wait before it, nothing after it.
    assert replies.sent == 1
    assert no_sleep == []
    service.reserve_train.assert_not_called()


@KORAIL2_UTCNOW
def test_a_blocked_reply_on_a_later_page_ends_the_watch_too(no_sleep):
    first_page = {
        "strResult": "SUCC",
        "h_msg_cd": "",
        "h_msg_txt": "",
        "trn_infos": {
            "trn_info": [
                {
                    "h_trn_clsf_cd": "100",
                    "h_trn_clsf_nm": "KTX",
                    "h_trn_gp_cd": "100",
                    "h_trn_no": f"{101 + 2 * index:05d}",
                    "h_dpt_rs_stn_nm": "서울",
                    "h_dpt_rs_stn_cd": "0001",
                    "h_dpt_dt": "20261003",
                    "h_dpt_tm": f"{7 + index:02d}0000",
                    "h_arv_rs_stn_nm": "부산",
                    "h_arv_rs_stn_cd": "0020",
                    "h_arv_dt": "20261003",
                    "h_arv_tm": f"{10 + index:02d}0000",
                    "h_run_dt": "20261003",
                    "h_rsv_psb_flg": "N",
                    "h_rsv_psb_nm": "매진",
                    "h_spe_rsv_cd": "13",
                    "h_gen_rsv_cd": "13",
                    "h_wait_rsv_flg": "-1",
                }
                for index in range(10)
            ]
        },
    }
    service, replies = _logged_in_service(first_page, {"h_msg_cd": ""})

    with pytest.raises(SearchBlockedError):
        _search(service)

    assert replies.sent == 2


def _korail_answering(monkeypatch, body: dict) -> list:
    """The current Korail client, answering every request with `body`. Returns what was sent."""
    sent = []

    def answer(request):
        sent.append(request.url.path)
        return httpx.Response(200, json=body)

    real_client = korail_service_module.KorailClient
    monkeypatch.setattr(
        korail_service_module,
        "KorailClient",
        lambda config: real_client(config, transport=httpx.MockTransport(answer)),
    )
    return sent


def test_a_session_refresh_that_answers_like_a_block_sends_one_login_and_stops(
    monkeypatch, no_sleep
):
    # The real path: the half-hourly refresh falls due, its login is answered without strResult,
    # and nothing - not ensure_logged_in, not the search - asks Korail again.
    sent = _korail_answering(monkeypatch, {"h_msg_cd": ""})
    service = KorailService()
    service._logged_in = True
    service._username, service._password = "01000000000", PASSWORD
    service._relogin_due_at = time.time() - 1
    service.search_trains = Mock()

    with pytest.raises(SearchGaveUp) as raised:
        _loop(service)

    assert raised.value.reason == "blocked"
    assert len(sent) == 1
    assert service.login_blocked
    service.search_trains.assert_not_called()


def test_a_block_already_seen_stops_the_next_login_before_it_is_sent(monkeypatch, no_sleep):
    sent = _korail_answering(monkeypatch, {"h_msg_cd": ""})
    service = KorailService()
    service._username, service._password = "01000000000", PASSWORD
    service.login_blocked = True

    with pytest.raises(SearchBlockedError):
        service.ensure_logged_in()

    assert sent == []


def test_a_session_korail_drops_every_pass_stops_after_three_logins(monkeypatch, no_sleep):
    from korail2.korail2 import NeedToLoginError

    service = KorailService()
    service._logged_in = True
    service._korail_instance = object()
    service._username, service._password = "01000000000", PASSWORD
    service._search_legacy_pages = Mock(side_effect=NeedToLoginError("P058"))
    logins = []

    def login(username, password):
        logins.append(1)
        return True

    service._login_with_current_api = login

    with pytest.raises(SearchGaveUp) as raised:
        _loop(service)

    assert raised.value.reason == "blocked"
    assert len(logins) == service.RELOGIN_LIMIT == 3
    # A pass that only got a fresh login is a failure, not an empty answer.
    assert service._failure_streak == 3


def test_the_reply_hook_failing_does_not_stop_the_client_being_built(monkeypatch, caplog):
    class Moved:
        @property
        def _client(self):
            raise AttributeError("moved")

    real_client = korail_service_module.KorailClient

    def client(config):
        built = real_client(config)
        built.http = Moved()
        return built

    monkeypatch.setattr(korail_service_module, "KorailClient", client)

    with caplog.at_level(logging.DEBUG, logger="korail_bot.services.korail_service"):
        built = KorailService()._build_modern_client()

    assert isinstance(built.http, Moved)
    assert "reply hook not installed" in caplog.text


def test_a_login_reply_without_str_result_is_a_block_logged_without_its_body(monkeypatch, caplog):
    member = "member-detail-value"

    def answer(request):
        return httpx.Response(200, json={"h_msg_cd": "", "strMbCrdNo": member})

    real_client = korail_service_module.KorailClient
    monkeypatch.setattr(
        korail_service_module,
        "KorailClient",
        lambda config: real_client(config, transport=httpx.MockTransport(answer)),
    )
    service = KorailService()

    with caplog.at_level(logging.ERROR, logger="korail_bot.services.korail_service"):
        assert service.login("01000000000", PASSWORD) is False

    assert service.login_blocked and service.login_unavailable
    # The login reply is not kept for a later search's diagnosis.
    assert service._last_modern_reply is None
    said = caplog.text
    assert "keys=['h_msg_cd', 'strMbCrdNo']" in said
    assert member not in said and PASSWORD not in said


def test_a_seat_map_watch_reads_the_current_clients_way_of_saying_it():
    from korail_mobile_api.errors import KorailProtocolError

    service = KorailService()
    blocked = blocked_reply()
    not_json = KorailProtocolError("KORAIL response body was not valid JSON")

    assert service.is_block_signal(blocked)
    assert not service.is_block_signal(not_json)
    with pytest.raises(SearchGaveUp) as raised:
        service.note_search_failure(blocked)
    assert raised.value.reason == "blocked"


@KORAIL2_UTCNOW
def test_an_unpaid_list_without_str_result_stops_the_watch_instead_of_reading_as_unknown():
    service, replies = _logged_in_service({"h_msg_cd": ""})

    with pytest.raises(SearchBlockedError):
        service.unpaid_reservations()

    assert replies.sent == 1


def test_a_formation_lookup_that_answers_like_a_block_is_not_swallowed(monkeypatch):
    from korail_mobile_api.errors import KorailProtocolError

    service = KorailService()
    blocked = blocked_reply()
    monkeypatch.setattr(service, "_formation_lookup", Mock(side_effect=blocked))
    train = SimpleNamespace(
        train_no="00101", departure_date="20261003", departure_time="070000", train_class_code="00"
    )

    with pytest.raises(KorailProtocolError):
        service._with_unlisted_cars(train, "1", 1, ("k",), SimpleNamespace(cars=()))


def test_a_later_seat_map_search_page_that_answers_like_a_block_ends_the_pass(no_sleep):
    from korail_mobile_api import TrainSummary
    from korail_mobile_api.errors import KorailProtocolError

    rows = [
        TrainSummary(
            train_no=f"{101 + 2 * index:05d}",
            departure_date="20261003",
            departure_time=f"{7 + index:02d}0000",
            arrival_time=f"{10 + index:02d}0000",
            departure_station_name="서울",
            arrival_station_name="부산",
            run_date="20261003",
            train_class_name="KTX",
            train_group_name="KTX",
            train_class_code="00",
            general_reservation_code="13",
            special_reservation_code="13",
            general_availability_name="매진",
            special_availability_name="매진",
            wait_reservation_flag="-1",
        )
        for index in range(10)
    ]
    first = SimpleNamespace(
        trains=rows, next_page=lambda: None, metadata=SimpleNamespace(next_page_flag="Y")
    )
    service = KorailService()
    service._logged_in = True
    service._modern_client = Mock()
    service._modern_client.search_trains.side_effect = [
        first,
        blocked_reply(),
    ]

    with pytest.raises(KorailProtocolError):
        service.search_waitlist_trains(
            dep_date="20261003",
            src_locate="서울",
            dst_locate="부산",
            dep_time="070000",
            max_dep_time="2400",
            train_type=TrainType.KTX,
            passenger_count=1,
        )

    assert service._modern_client.search_trains.call_count == 2


def _korail2_train():
    from korail2.korail2 import Train

    return Train(
        {
            "h_trn_clsf_cd": "100",
            "h_trn_clsf_nm": "KTX",
            "h_trn_gp_cd": "100",
            "h_trn_no": "00101",
            "h_dpt_rs_stn_nm": "서울",
            "h_dpt_rs_stn_cd": "0001",
            "h_dpt_dt": "20261003",
            "h_dpt_tm": "070000",
            "h_arv_rs_stn_nm": "부산",
            "h_arv_rs_stn_cd": "0020",
            "h_arv_dt": "20261003",
            "h_arv_tm": "094100",
            "h_run_dt": "20261003",
            "h_rsv_psb_flg": "Y",
            "h_rsv_psb_nm": "예약가능",
            "h_spe_rsv_cd": "11",
            "h_gen_rsv_cd": "11",
            "h_wait_rsv_flg": "-1",
        }
    )


def test_a_reservation_list_without_str_result_ends_the_watch_before_reserving():
    service, replies = _logged_in_service({"h_msg_cd": ""})

    with pytest.raises(SearchGaveUp) as raised:
        service.reserve_train(_korail2_train())

    assert raised.value.reason == "blocked"
    assert replies.sent == 1


def test_a_reservation_answered_without_str_result_ends_the_watch_not_the_train():
    service, replies = _logged_in_service(
        {"strResult": "FAIL", "h_msg_cd": "P100", "h_msg_txt": "예약 내역이 없습니다."},
        {"h_msg_cd": ""},
    )

    with pytest.raises(SearchGaveUp) as raised:
        service.reserve_train(_korail2_train())

    assert raised.value.reason == "blocked"
    assert replies.sent == 2


# ==================== Unavailable for too long ====================


def _clock(monkeypatch, start=1_000_000.0):
    now = [start]
    monkeypatch.setattr(rail_service_module.time, "time", lambda: now[0])
    return now


def test_failures_of_any_kind_end_the_watch_once_they_last_the_limit(monkeypatch, no_sleep):
    monkeypatch.setattr(settings, "KORAIL_FAILURE_STOP_SECONDS", 600.0)
    now = _clock(monkeypatch)
    service = KorailService()
    maintenance = SearchUnavailableError("KorailError: 시스템 점검 중입니다. (S000)")

    service.note_search_failure(maintenance)
    now[0] += 599
    service.note_search_failure(SearchUnavailableError("ReadTimeout"))
    now[0] += 1
    with pytest.raises(SearchGaveUp) as raised:
        service.note_search_failure(maintenance)

    assert raised.value.reason == "unavailable"


def test_an_answer_in_between_starts_the_count_again(monkeypatch, no_sleep):
    monkeypatch.setattr(settings, "KORAIL_FAILURE_STOP_SECONDS", 600.0)
    now = _clock(monkeypatch)
    service = KorailService()

    service.note_search_failure(SearchUnavailableError("S000"))
    now[0] += 500
    service.note_search_success()
    now[0] += 200
    service.note_search_failure(SearchUnavailableError("S000"))
    now[0] += 500
    # 700s since the first failure, but only 500s of this run of them.
    service.note_search_failure(SearchUnavailableError("S000"))


def test_maintenance_keeps_the_watch_retrying_until_korail_answers(monkeypatch, no_sleep):
    monkeypatch.setattr(settings, "KORAIL_FAILURE_STOP_SECONDS", 600.0)
    # Without jitter: 3s +/- 40% reaches 4.2s and 6s +/- 40% starts at 3.6s, so
    # randomised waits are not always increasing even though the backoff is.
    monkeypatch.setattr(settings, "KORAIL_SEARCH_INTERVAL_JITTER", 0.0)
    service = KorailService()
    service._logged_in = True
    seat = SimpleNamespace(rsv_id="R1")
    maintenance = SearchUnavailableError("KorailError: 시스템 점검 중입니다. (S000)")
    service.search_trains = Mock(side_effect=[maintenance] * 4 + [[SimpleNamespace()]])
    service.reserve_train = Mock(return_value=seat)

    assert _loop(service) is seat
    assert service.search_trains.call_count == 5
    # Backed off in between: 3s, 6s, 12s, 24s at the default interval.
    assert no_sleep == [3.0, 6.0, 12.0, 24.0]


def test_the_stop_limit_at_zero_never_gives_up(monkeypatch, no_sleep):
    monkeypatch.setattr(settings, "KORAIL_FAILURE_STOP_SECONDS", 0.0)
    now = _clock(monkeypatch)
    service = KorailService()

    for _ in range(50):
        service.note_search_failure(SearchUnavailableError("S000"))
        now[0] += 3600


def test_the_failure_notice_says_the_watch_will_stop_not_that_it_goes_on(monkeypatch, no_sleep):
    monkeypatch.setattr(settings, "KORAIL_FAILURE_STOP_SECONDS", 600.0)
    monkeypatch.setattr(settings, "KORAIL_FAILURE_ALERT_THRESHOLD", 2)
    _clock(monkeypatch)
    said = []
    service = KorailService(on_status=said.append)

    service.note_search_failure(SearchUnavailableError("S000"))
    service.note_search_failure(SearchUnavailableError("S000"))

    (notice,) = said
    assert "10분 넘게 없으면 검색을 멈춥니다" in notice
    assert "계속" not in notice


# ==================== Expired ====================


def test_a_watch_past_its_time_limit_ends_before_asking_korail(monkeypatch, no_sleep):
    now = _clock(monkeypatch)
    service = KorailService()
    service._logged_in = True
    service.watch_deadline = now[0] - 1
    service.search_trains = Mock()

    with pytest.raises(SearchGaveUp) as raised:
        _loop(service)

    assert raised.value.reason == "expired"
    service.search_trains.assert_not_called()


def test_a_watch_runs_until_its_deadline_then_ends(monkeypatch, no_sleep):
    now = _clock(monkeypatch)
    service = KorailService()
    service._logged_in = True
    service.watch_deadline = now[0] + 10

    def sold_out(*args, **kwargs):
        now[0] += 4
        return []

    service.search_trains = Mock(side_effect=sold_out)

    with pytest.raises(SearchGaveUp) as raised:
        _loop(service)

    assert raised.value.reason == "expired"
    assert service.search_trains.call_count == 3


# ==================== The process says why ====================


def _process(rail, record=None):
    from korail_bot.telegramBot.telebotBackProcess import BackgroundReservationProcess

    process = BackgroundReservationProcess.__new__(BackgroundReservationProcess)
    process.rail = rail
    process.storage = MagicMock()
    process.storage.get_running_reservation.return_value = record
    process.telegram = MagicMock()
    process.chat_id = CHAT
    process.username, process.password = "01000000000", PASSWORD
    process.dep_date, process.src_locate, process.dst_locate = "20261003", "서울", "부산"
    process.dep_time, process.max_dep_time = "070000", "2400"
    process.train_type, process.reserve_option = TrainType.KTX, None
    process.passenger_count, process.seat_strategy = 1, "consecutive"
    process.train_numbers, process.seat_preference, process.seat_direction = [], None, ""
    process.seat_plan = None
    process._send_callback = MagicMock()
    return process


def _record(started_ago: timedelta):
    return RunningReservation(
        chat_id=CHAT,
        process_id=os.getpid(),
        korail_id="01000000000",
        search_params=TrainSearchParams(
            dep_date="20261003", src_locate="서울", dst_locate="부산", dep_time="070000"
        ),
        started_at=utc_now() - started_ago,
    )


@pytest.mark.parametrize(
    ("reason", "status", "words"),
    [
        ("blocked", 3, "코레일이 접속을 막은 것 같아 감시를 멈췄어요"),
        ("unavailable", 4, "코레일 응답이 10분째 없어 감시를 멈췄어요"),
        ("expired", 5, "1시간 동안 좌석을 잡지 못해 감시를 멈췄어요"),
    ],
)
def test_a_watch_that_gave_up_says_why_in_its_own_status(monkeypatch, reason, status, words):
    monkeypatch.setattr(settings, "KORAIL_FAILURE_STOP_SECONDS", 600.0)
    monkeypatch.setattr(settings, "WATCH_MAX_SECONDS", 3600.0)
    rail = KorailService()
    rail.login = Mock(return_value=True)
    rail.search_and_reserve_loop = Mock(side_effect=SearchGaveUp(reason))
    process = _process(rail)

    process.run()

    message = process._send_callback.call_args.args[0]
    assert process._send_callback.call_args.kwargs["status"] == status
    assert message.splitlines()[0].endswith(words)
    assert "서울 → 부산" in message
    # Neither a booking nor a sold-out train.
    assert "잡았어요" not in message and "매진" not in message


def test_a_watch_that_holds_some_seats_says_so_when_it_runs_out_of_time(monkeypatch):
    monkeypatch.setattr(settings, "WATCH_MAX_SECONDS", 3600.0)
    process = _process(KorailService())
    process._seats_held = 1

    process._give_up("expired")

    message = process._send_callback.call_args.args[0]
    assert message.splitlines()[0] == "⏱ 1석은 잡았고 나머지 감시는 시간 상한으로 멈췄어요"
    assert "잡은 좌석은 결제 기한 안에 결제해 주세요" in message
    assert "좌석을 잡지 못해" not in message


def test_a_login_that_answers_like_a_block_ends_the_watch_without_retrying(monkeypatch):
    from korail_bot.telegramBot import telebotBackProcess

    monkeypatch.setattr(settings, "LOGIN_RETRY_DELAYS_SECONDS", (5.0, 15.0))
    waits = []
    monkeypatch.setattr(telebotBackProcess.time, "sleep", waits.append)
    rail = KorailService()

    def login(username, password):
        rail.login_unavailable = rail.login_blocked = True
        return False

    rail.login = Mock(side_effect=login)
    process = _process(rail)

    process.run()

    assert rail.login.call_count == 1 and waits == []
    assert process._send_callback.call_args.kwargs["status"] == 3


def test_a_watch_resumed_past_its_limit_ends_without_logging_in(monkeypatch):
    monkeypatch.setattr(settings, "WATCH_MAX_SECONDS", 3600.0)
    rail = KorailService()
    rail.login = Mock()
    process = _process(rail, _record(timedelta(minutes=61)))

    process.run()

    rail.login.assert_not_called()
    assert process._send_callback.call_args.kwargs["status"] == 5


def test_the_time_limit_counts_from_the_first_start_not_the_resume(monkeypatch):
    monkeypatch.setattr(settings, "WATCH_MAX_SECONDS", 3600.0)
    record = _record(timedelta(minutes=50))
    process = _process(KorailService(), record)

    deadline = process._watch_deadline()

    assert deadline == pytest.approx(record.started_at.timestamp() + 3600)


def test_the_time_limit_at_zero_is_no_limit(monkeypatch):
    monkeypatch.setattr(settings, "WATCH_MAX_SECONDS", 0.0)
    process = _process(KorailService(), _record(timedelta(days=2)))

    assert process._watch_deadline() is None


def test_a_restart_brings_a_watch_back_with_its_first_start(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    storage = MobileStorage(client=client, secret="a" * 40)
    service = MobileReservationService(
        storage,
        MagicMock(),
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
    )
    left = _record(timedelta(minutes=30))
    left.run_id = "an-earlier-run"
    left.process_id = 4_000_000
    storage.save_running_reservation(left)
    storage.save_resume_credentials(CHAT, "01000000000", PASSWORD)
    started = []
    monkeypatch.setattr(
        service, "start_reservation_process", lambda **kwargs: started.append(kwargs) or True
    )

    assert service._resume(left)

    assert started[0]["started_at"] == left.started_at
    storage.close()


# ==================== The app and agents are told ====================


@pytest.mark.parametrize(
    ("status", "kind", "reason"),
    [(3, "blocked", "blocked"), (4, "unavailable", "unavailable"), (5, "expired", "expired")],
)
def test_each_ending_has_a_kind_of_its_own_for_the_app(tmp_path, status, kind, reason):
    client = fakeredis.FakeRedis(decode_responses=True)
    storage = MobileStorage(client=client, secret="a" * 40)
    storage.save_running_reservation(_record(timedelta(minutes=5)))
    notifications = MagicMock()

    apply_result(storage, notifications, CHAT, "끝", status=status)

    assert notifications.publish.call_args.kwargs["kind"] == kind
    assert storage.get_search_ended(CHAT)["reason"] == reason
    assert storage.get_running_reservation(CHAT) is None
    storage.close()


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        ("blocked", "코레일이 접속을 막은 것 같아"),
        ("unavailable", "코레일 응답이 10분째 없어"),
        ("expired", "1시간 동안 좌석을 잡지 못해"),
    ],
)
def test_get_status_says_how_the_last_watch_ended(monkeypatch, reason, words):
    monkeypatch.setattr(settings, "KORAIL_FAILURE_STOP_SECONDS", 600.0)
    monkeypatch.setattr(settings, "WATCH_MAX_SECONDS", 3600.0)

    said = mcp._status(
        {
            "running": None,
            "scheduled": None,
            "pending": [],
            "ended": {"reason": reason, "at": "2026-10-19T03:00:00+00:00"},
        }
    )

    (note,) = said["notes"]
    assert words in note
    assert "매진" not in note


def test_get_status_leaves_the_last_ending_out_while_a_watch_runs():
    said = mcp._status(
        {
            "running": {"health": "healthy", "lastCheckedAt": utc_now().isoformat()},
            "scheduled": None,
            "pending": [],
            "ended": {"reason": "blocked", "at": "2026-10-19T03:00:00+00:00"},
        }
    )

    assert not any("막은" in note for note in said["notes"])
