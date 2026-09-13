from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from korail_bot.models import CancellationWaitPlan, ReservationOutcome, SeatTarget
from korail_bot.services.cancellation_wait_service import (
    CancellationWaitService,
    DesignatedCapture,
)
from korail_bot.telegramBot.telebotBackProcess import BackgroundReservationProcess
from korail_bot.utils.timezone import utc_now


def target(number: str, position: int, *, possible: str = "Y") -> SimpleNamespace:
    return SimpleNamespace(
        seat_no=f"wire-{number}", specification=number, sale_possible=possible, position=position
    )


def payload(strategy: str = "independent", passenger_count: int = 2) -> dict:
    return {
        "strategy": strategy,
        "passengerCount": passenger_count,
        "trains": [
            {
                "trainNo": "015",
                "seatClass": "general",
                "targets": [
                    {
                        "carNo": 3,
                        "seatNo": f"wire-{number}",
                        "label": number,
                        "row": 1,
                        "column": number[-1],
                        "adjacencyGroup": "1:left",
                        "position": position,
                    }
                    for number, position in (("1A", 1), ("1B", 2))
                ],
            }
        ],
    }


def service(raw_plan: dict, seats: list[SimpleNamespace]):
    rail = Mock()
    train = SimpleNamespace(train_no="015")
    inventory = SimpleNamespace(car_no=3, seats=seats)
    rail.search_selectable_trains.return_value = [train]
    rail.seat_inventory.return_value = inventory
    rail.reserve_designated.return_value = SimpleNamespace(pnr_no="hold")
    wait = CancellationWaitService(
        rail,
        CancellationWaitPlan.from_payload(raw_plan),
        {"dep_date": "20260920", "src_locate": "서울", "dst_locate": "부산"},
    )
    return wait, rail


def test_independent_capture_reserves_one_matching_seat_and_honours_exclusion():
    wait, rail = service(payload(), [target("1A", 1), target("1B", 2)])

    capture = wait.poll_once({("015", "general", 3, "wire-1A")})

    assert [seat.label for seat in capture.targets] == ["1B"]
    assert rail.reserve_designated.call_args.kwargs == {
        "passenger_count": 1,
        "seat_class": "general",
    }


def test_consecutive_capture_requires_the_whole_block_to_be_sellable():
    wait, rail = service(
        payload("consecutive"), [target("1A", 1), target("1B", 2, possible="N")]
    )
    assert wait.poll_once() is None
    rail.reserve_designated.assert_not_called()

    rail.seat_inventory.return_value.seats[1].sale_possible = "Y"
    capture = wait.poll_once()

    assert [seat.label for seat in capture.targets] == ["1A", "1B"]
    assert rail.reserve_designated.call_args.kwargs == {
        "passenger_count": 2,
        "seat_class": "general",
    }


def test_background_independent_wait_persists_and_notifies_each_capture(monkeypatch):
    plan = CancellationWaitPlan.from_payload(payload())
    captures = [
        DesignatedCapture(
            SimpleNamespace(pnr_no=f"R{index}"),
            SimpleNamespace(train_no="015"),
            "general",
            (SeatTarget.from_payload(raw),),
        )
        for index, raw in enumerate(payload()["trains"][0]["targets"], 1)
    ]
    poller = Mock()
    poller.poll_once.side_effect = captures
    monkeypatch.setattr(
        "korail_bot.telegramBot.telebotBackProcess.CancellationWaitService",
        Mock(return_value=poller),
    )
    process = BackgroundReservationProcess.__new__(BackgroundReservationProcess)
    process.seat_plan = plan
    process.rail = Mock()
    process.rail.reservation_id.side_effect = ["R1", "R2"]
    process.rail.reservation_outcome.return_value = ReservationOutcome.OUTSTANDING
    process.storage = Mock()
    process.storage.get_multi_reservation_status.return_value = None
    process.chat_id = -100
    process.dep_date = "20260920"
    process.src_locate = "서울"
    process.dst_locate = "부산"
    process.dep_time = "090000"
    process.max_dep_time = "1800"
    process.train_type = object()
    process._payment_deadline = Mock(return_value=utc_now() + timedelta(minutes=10))
    process._train_hints = Mock(
        return_value={"train_no": "015", "dep_date": "20260920", "dep_time": "090000"}
    )
    process._train_info_for_user = Mock(return_value="KTX 015 서울 → 부산")
    process._send_callback = Mock()

    process._run_cancellation_wait()

    assert [call.kwargs["status"] for call in process._send_callback.call_args_list] == [2, 0]
    assert process.storage.save_multi_reservation_status.call_count == 2
    process.storage.wait_for_payment.assert_not_called()
