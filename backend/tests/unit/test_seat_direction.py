"""Seat direction is a booking request, never an inventory-code guess."""

import json
import sys
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis
import pytest

from korail_bot.handlers.conversation_handler import ConversationHandler
from korail_bot.mobile.storage import MobileStorage
from korail_bot.models import TrainSearchParams
from korail_bot.services import reservation_service
from korail_bot.services.korail_service import (
    KORAIL_TICKETRESERVATION,
    _ticket_reservation_seat_att,
    direction_seat_att_cd,
)
from korail_bot.services.mini_app_gateway import MiniAppGateway
from korail_bot.services.mini_app_service import MiniAppDataError, MiniAppSubmission
from korail_bot.services.reservation_service import ReservationService
from korail_bot.telegramBot.telebotBackProcess import BackgroundReservationProcess
from korail_bot.utils.timezone import utc_now


def _conditions(**changes):
    payload = {
        "v": 1,
        "action": "prepare_search",
        "dep_date": (utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
        "src_station": "서울",
        "dst_station": "부산",
        "dep_time": "0900",
        "max_dep_time": "1800",
        "train_type": "1",
        "seat_option": "2",
        "passenger_count": 1,
        "seat_strategy": "1",
        "seat_preference": "",
        "seat_direction": "forward",
    }
    payload.update(changes)
    return payload


def test_public_directions_map_only_to_korail_request_codes():
    assert MiniAppSubmission.parse(json.dumps(_conditions())).seat_direction == "009"
    assert (
        MiniAppSubmission.parse(json.dumps(_conditions(seat_direction="backward"))).seat_direction
        == "010"
    )
    assert direction_seat_att_cd("009") == "009"
    assert direction_seat_att_cd("010") == "010"
    assert direction_seat_att_cd("1") == "000"
    assert direction_seat_att_cd("2") == "000"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"waitlist": True}, "예약 대기"),
        ({"seat_classes": ["general"]}, "지정 좌석"),
        (
            {
                "seat_plan": {
                    "strategy": "independent",
                    "passengerCount": 1,
                    "trains": [{"trainNo": "015", "seatClass": "general", "targets": []}],
                }
            },
            "지정 좌석",
        ),
    ],
)
def test_direction_is_rejected_when_that_booking_path_cannot_honour_it(changes, message):
    with pytest.raises(MiniAppDataError, match=message):
        MiniAppSubmission.parse(json.dumps(_conditions(**changes)))


def test_direction_round_trips_storage_and_running_description():
    storage = MobileStorage(client=fakeredis.FakeRedis(decode_responses=True), secret="a" * 40)
    try:
        params = TrainSearchParams(
            dep_date="20261020",
            src_locate="서울",
            dst_locate="부산",
            dep_time="090000",
            seat_direction="010",
        )
        restored = storage._deserialize_search_params(storage._serialize_search_params(params))
        gateway = MiniAppGateway(storage, MagicMock(), MagicMock(), MagicMock())

        assert restored.seat_direction == "010"
        assert gateway._describe_params(restored)["seatDirection"] == "backward"
    finally:
        storage.close()


def test_conversation_maps_public_direction_to_the_worker_code():
    info = MiniAppSubmission.parse(json.dumps(_conditions())).as_train_info()
    params = ConversationHandler._build_search_params(
        object.__new__(ConversationHandler), SimpleNamespace(train_info=info)
    )

    assert info["seatDirection"] == "forward"
    assert params.seat_direction == "009"
    assert params.validate()[0]


def test_reservation_request_replaces_only_the_direction_attribute():
    calls = []
    session = SimpleNamespace()

    def get(url, *args, **kwargs):
        calls.append((url, kwargs["params"]))
        return "response"

    session.get = get
    original = session.get
    with _ticket_reservation_seat_att(session, "000", "009"):
        assert (
            session.get(
                KORAIL_TICKETRESERVATION,
                params={"txtSeatAttCd2": "000", "txtSeatAttCd3": "000", "keep": "yes"},
            )
            == "response"
        )

    assert calls == [
        (
            KORAIL_TICKETRESERVATION,
            {"txtSeatAttCd2": "009", "txtSeatAttCd3": "000", "keep": "yes"},
        )
    ]
    assert session.get is original


def test_search_loop_passes_direction_to_group_reservation(monkeypatch):
    from korail_bot.services import rail_service as rail_service_module
    from korail_bot.services.korail_service import KorailService

    monkeypatch.setattr(rail_service_module.time, "sleep", lambda *_: None)
    service = KorailService()
    service._logged_in = True
    train = SimpleNamespace(train_no="101")
    reservation = SimpleNamespace(rsv_id="R1")
    service.search_trains = MagicMock(return_value=[train])
    service.reserve_train = MagicMock(return_value=reservation)

    assert (
        service.search_and_reserve_loop(
            dep_date="20261020",
            src_locate="서울",
            dst_locate="부산",
            passenger_count=3,
            seat_strategy="consecutive",
            seat_direction="009",
        )
        is reservation
    )
    assert service.reserve_train.call_args.kwargs["seat_direction"] == "009"


def test_random_worker_passes_direction_to_each_single_seat():
    process = object.__new__(BackgroundReservationProcess)
    process.chat_id = -100
    process.dep_date = "20261020"
    process.src_locate = "서울"
    process.dst_locate = "부산"
    process.dep_time = "090000"
    process.max_dep_time = "1800"
    process.train_type = object()
    process.train_numbers = []
    process.reserve_option = object()
    process.seat_preference = None
    process.seat_direction = "010"
    process.telegram = MagicMock()
    process.rail = MagicMock()
    process.rail.search_trains.return_value = [SimpleNamespace(train_no="101")]
    process.rail.reserve_train.return_value = SimpleNamespace(rsv_id="R1")
    process.rail.keeps_seat.return_value = True

    assert process._reserve_single_seat_random(0).rsv_id == "R1"
    assert process.rail.reserve_train.call_args.kwargs["seat_direction"] == "010"


def test_worker_direction_slot_stays_after_an_empty_seat_plan(monkeypatch):
    service = ReservationService(MagicMock(), MagicMock())
    service._may_start = lambda chat_id: True
    service._under_concurrency_limit = lambda chat_id: True
    service._confirm_started = lambda *args: True
    proc = MagicMock(pid=4242)
    command = []

    def popen(cmd, **kwargs):
        command.extend(cmd)
        return proc

    monkeypatch.setattr(reservation_service.subprocess, "Popen", popen)
    assert service.start_reservation_process(
        chat_id=7,
        username="01012345678",
        password="rail password",
        search_params=TrainSearchParams(
            dep_date="20261020",
            src_locate="서울",
            dst_locate="부산",
            dep_time="090000",
            seat_plan_json="",
            seat_direction="009",
        ),
    )
    for thread in threading.enumerate():
        if thread.name.startswith(reservation_service._STDIN_THREAD_PREFIX):
            thread.join(timeout=5)

    argv = ["worker", *command[3:]]
    assert argv[13] == ""
    assert argv[14] == "009"
    monkeypatch.setattr(sys, "argv", argv)
    assert sys.argv[14] == "009"
