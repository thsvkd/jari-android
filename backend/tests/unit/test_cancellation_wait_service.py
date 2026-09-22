from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

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


def car(car_no: int = 3, remaining: int = 2) -> SimpleNamespace:
    return SimpleNamespace(car_no=car_no, remaining_seat_count=remaining)


def cars(*items: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(cars=items or (car(),))


def service(raw_plan: dict, seats: list[SimpleNamespace], trains=None):
    rail = Mock()
    inventory = SimpleNamespace(car_no=3, seats=seats)
    rail.search_selectable_trains.return_value = trains or [SimpleNamespace(train_no="015")]
    rail.seat_cars.return_value = cars()
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
    wait, rail = service(payload("consecutive"), [target("1A", 1), target("1B", 2, possible="N")])
    assert wait.poll_once() is None
    rail.reserve_designated.assert_not_called()

    rail.seat_inventory.return_value.seats[1].sale_possible = "Y"
    capture = wait.poll_once()

    assert [seat.label for seat in capture.targets] == ["1A", "1B"]
    assert rail.reserve_designated.call_args.kwargs == {
        "passenger_count": 2,
        "seat_class": "general",
    }


def seat(
    car_no: int,
    label: str,
    position: int,
    *,
    side: str = "left",
    row_position: int = 0,
) -> dict:
    return {
        "carNo": car_no,
        "seatNo": f"wire-{label}",
        "label": label,
        "row": 1,
        "column": label[-1],
        "adjacencyGroup": f"1:{side}",
        "position": position,
        "rowPosition": row_position,
    }


def entry(train_no: str, *seats: dict) -> dict:
    return {"trainNo": train_no, "seatClass": "general", "targets": list(seats)}


def one_seat_plan(*entries: dict, strategy: str = "independent") -> dict:
    return {"strategy": strategy, "passengerCount": 1, "trains": list(entries)}


def train_result(
    train_no: str, code: str | None = None, name: str | None = None
) -> SimpleNamespace:
    train = SimpleNamespace(train_no=train_no)
    if code is not None:
        train.general_reservation_code = code
    if name is not None:
        train.general_availability_name = name
    return train


def test_a_sold_out_class_is_skipped_without_a_single_request():
    # 30 trains of 14 cars used to cost ~450 seat reads a pass whether or not
    # any of them had a seat to sell.
    wait, rail = service(
        one_seat_plan(entry("015", seat(3, "1A", 1))),
        [target("1A", 1)],
        trains=[train_result("015", "00", "매진")],
    )

    assert wait.poll_once() is None
    rail.seat_cars.assert_not_called()
    rail.seat_inventory.assert_not_called()


def test_only_the_word_the_app_itself_blocks_on_skips_a_train():
    # Korail's app gates booking on the wording, not the code, so a code this
    # side does not recognise must not be read as sold out.
    for code, name in (("13", "좌석부족"), ("13", None), (None, "좌석부족")):
        wait, rail = service(
            one_seat_plan(entry("015", seat(3, "1A", 1))),
            [target("1A", 1)],
            trains=[train_result("015", code, name)],
        )

        assert wait.poll_once() is not None, (code, name)
        rail.seat_cars.assert_called_once()


def test_a_train_with_no_candidate_car_is_not_asked_about_its_cars():
    # A 30-train consecutive plan where only one train has a usable block
    # would otherwise pay 29 car lists a pass to learn nothing.
    wait, rail = service(
        {
            "strategy": "consecutive",
            "passengerCount": 2,
            "trains": [
                entry("015", seat(3, "1A", 1), seat(3, "1B", 2)),
                entry("017", seat(3, "1A", 1)),
            ],
        },
        [target("1A", 1, possible="N"), target("1B", 2, possible="N")],
        trains=[train_result("015", "11"), train_result("017", "11")],
    )

    assert wait.poll_once() is None
    # 017 has no pair of adjacent seats, so it has nothing to ask about.
    assert [call.args[0].train_no for call in rail.seat_cars.call_args_list] == ["015"]


def test_one_car_list_replaces_a_read_of_every_candidate_car():
    wait, rail = service(
        one_seat_plan(entry("015", seat(3, "1A", 1), seat(4, "1A", 1))),
        [target("1A", 1)],
        trains=[train_result("015", "11")],
    )
    rail.seat_cars.return_value = cars(car(3, 2), car(4, 0), car(9, 5))

    capture = wait.poll_once()

    assert [seat.label for seat in capture.targets] == ["1A"]
    assert rail.seat_cars.call_count == 1
    # Car 4 has nothing left and car 9 was never a candidate.
    assert [call.args[1] for call in rail.seat_inventory.call_args_list] == [3]


def test_a_train_that_does_not_say_is_read_anyway():
    # Older search results carry no reservation code. Guessing "sold out"
    # there would quietly stop watching the train.
    wait, rail = service(payload(), [target("1A", 1), target("1B", 2)])

    assert wait.poll_once() is not None
    rail.seat_cars.assert_called_once()


def test_one_failing_train_does_not_cost_the_pass():
    wait, rail = service(
        one_seat_plan(entry("015", seat(3, "1A", 1)), entry("017", seat(3, "1A", 1))),
        [target("1A", 1)],
        trains=[train_result("015", "11"), train_result("017", "11")],
    )
    rail.seat_cars.side_effect = [RuntimeError("코레일 오류"), cars()]

    capture = wait.poll_once()

    assert capture is not None
    assert capture.train.train_no == "017"


def test_a_pass_where_every_train_failed_is_a_failed_pass():
    wait, rail = service(
        one_seat_plan(entry("015", seat(3, "1A", 1)), entry("017", seat(3, "1A", 1))),
        [target("1A", 1)],
        trains=[train_result("015", "11"), train_result("017", "11")],
    )
    rail.seat_cars.side_effect = RuntimeError("코레일 오류")

    with pytest.raises(RuntimeError, match="코레일 오류"):
        wait.poll_once()


def test_consecutive_skips_a_sold_out_train_the_same_way():
    wait, rail = service(
        payload("consecutive"),
        [target("1A", 1), target("1B", 2)],
        trains=[train_result("015", "00", "매진")],
    )

    assert wait.poll_once() is None
    rail.seat_cars.assert_not_called()
    rail.seat_inventory.assert_not_called()


def test_a_block_is_booked_on_the_train_it_was_chosen_from():
    # Both trains offer the same physical seats, so a block cannot be traced
    # back to its train by seat: only the plan it came from says which is which.
    seats = (seat(3, "1A", 1), seat(3, "1B", 2))
    wait, rail = service(
        {
            "strategy": "consecutive",
            "passengerCount": 2,
            "trains": [entry("015", *seats), entry("017", *seats)],
        },
        [target("1A", 1), target("1B", 2)],
        trains=[train_result("015", "00", "매진"), train_result("017", "11")],
    )

    capture = wait.poll_once()

    assert capture.train.train_no == "017"
    assert rail.seat_cars.call_args.args[0].train_no == "017"


def test_three_passengers_are_booked_across_a_whole_row():
    # The row-wide block only earns its keep if it reaches the reservation.
    wait, rail = service(
        {
            "strategy": "consecutive",
            "passengerCount": 3,
            "trains": [
                entry(
                    "015",
                    seat(3, "5A", 1, row_position=1),
                    seat(3, "5B", 2, row_position=2),
                    seat(3, "5C", 1, side="right", row_position=3),
                )
            ],
        },
        [target("5A", 1), target("5B", 2), target("5C", 3)],
        trains=[train_result("015", "11")],
    )

    capture = wait.poll_once()

    assert [seat.label for seat in capture.targets] == ["5A", "5B", "5C"]
    booked = rail.reserve_designated.call_args
    assert [item.label for item in booked.args[2]] == ["5A", "5B", "5C"]
    assert booked.kwargs == {"passenger_count": 3, "seat_class": "general"}


def test_background_independent_wait_persists_and_notifies_each_capture(monkeypatch):
    plan = CancellationWaitPlan.from_payload(payload())
    captures = [
        DesignatedCapture(
            SimpleNamespace(pnr_no=f"R{index}"),
            SimpleNamespace(
                train_no="015",
                train_class_name="KTX",
                departure_station_name="서울",
                arrival_station_name="부산",
                departure_date="20260920",
                departure_time="090000",
            ),
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
    process.storage.get_user_timezone.return_value = "Asia/Seoul"
    process._send_callback = Mock()

    process._run_cancellation_wait()

    assert [call.kwargs["status"] for call in process._send_callback.call_args_list] == [2, 0]
    assert process.storage.save_multi_reservation_status.call_count == 2
    saved = str(process.storage.save_multi_reservation_status.call_args)
    assert "KTX 015 서울 → 부산 09:00" in saved
    assert "namespace(" not in saved
    process.storage.wait_for_payment.assert_not_called()
