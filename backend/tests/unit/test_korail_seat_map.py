from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from korail_mobile_api import (
    KorailReservationJobType,
    PhysicalSeat,
    SeatAttribute,
    SeatCar,
    SeatCarListResponse,
    SeatInventoryResponse,
)
from korail_mobile_api.errors import KorailAppError, KorailNoResultsError

from korail_bot.models import SeatTarget
from korail_bot.services.korail_service import KorailService
from korail_bot.services.seat_map_service import (
    SeatMapExpiredError,
    SeatMapNotFoundError,
    SeatMapService,
)
from korail_bot.utils.timezone import RAIL_TIMEZONE


def service_with_modern_client() -> KorailService:
    service = KorailService()
    service._logged_in = True
    service._modern_client = MagicMock()
    # 아래 열차들(9월 14일 등)이 아직 떠나지 않은 시각이에요.
    service.clock = lambda: datetime(2026, 9, 1, 9, 0, tzinfo=RAIL_TIMEZONE)
    return service


def physical_seat(
    *, seat_no: str = "000041", label: str = "5A", sale_possible: str = "Y"
) -> PhysicalSeat:
    return PhysicalSeat(
        seat_no=seat_no,
        sale_possible=sale_possible,
        direction_code="1",
        other_attribute_code="",
        requested_attribute_code="",
        floor="1",
        specification=label,
        sequence_no="1",
        message_code="",
        message="",
        visual_message_division_code="",
    )


def inventory(*seats: PhysicalSeat) -> SeatInventoryResponse:
    return SeatInventoryResponse(
        layout_type=4,
        arrangement_code="AB_CD",
        remaining_count=sum(seat.sale_possible == "Y" for seat in seats),
        total_count=len(seats),
        seats=seats,
        car_no=3,
    )


def test_korail_adapter_reads_cars_and_inventory_with_room_class_code():
    service = service_with_modern_client()
    train = SimpleNamespace(train_no="015")

    service.seat_cars(train, "special", 2)
    service._modern_client.get_seat_cars.assert_called_once_with(
        train, passenger_count=2, room_class_code="2"
    )

    service.seat_inventory(train, 3, "general", 1)
    assert service._modern_client.get_seat_cars.call_count == 2
    service._modern_client.get_seat_cars.assert_called_with(
        train, passenger_count=1, room_class_code="1"
    )
    service._modern_client.get_seat_inventory.assert_called_once_with(
        train, 3, passenger_count=1, room_class_code="1"
    )


def test_korail_adapter_reuses_matching_seat_context():
    service = service_with_modern_client()
    train = SimpleNamespace(train_no="015")

    service.seat_cars(train, "general", 1)
    service.seat_inventory(train, 3, "general", 1)

    service._modern_client.get_seat_cars.assert_called_once_with(
        train, passenger_count=1, room_class_code="1"
    )
    service._modern_client.get_seat_inventory.assert_called_once()


def test_sold_out_train_uses_nearby_matching_formation_for_seat_selection():
    service = service_with_modern_client()
    train = SimpleNamespace(
        train_no="009",
        train_class_code="00",
        departure_date="20260914",
        departure_time="063300",
        departure_station_name="서울",
        arrival_station_name="부산",
    )
    reference = SimpleNamespace(
        train_no="009",
        train_class_code="00",
        departure_date="20260915",
        departure_time="063300",
    )
    cars = SeatCarListResponse(
        cars=(SeatCar(3, "일반실", 1, ()),),
    )
    service._modern_client.get_seat_cars.side_effect = [
        KorailAppError("ERI411321", "잔여석이 없습니다."),
        cars,
    ]
    service._modern_client.search_trains.return_value = SimpleNamespace(trains=[reference])
    service._modern_client.get_seat_inventory.return_value = inventory(
        physical_seat(sale_possible="N")
    )

    assert service.seat_cars(train, "general", 1) is cars
    assert service.seat_layout_is_reference(train, "general", 1)
    assert service._modern_client.search_trains.call_args.args[0].departure_date == "20260921"
    assert service.seat_inventory(train, 3, "general", 1).car_no == 3
    service._modern_client.get_seat_inventory.assert_called_once_with(
        reference, 3, passenger_count=1, room_class_code="1"
    )


def nearly_sold_out_train(day: str = "20260914"):
    return SimpleNamespace(
        train_no="009",
        train_class_code="00",
        departure_date=day,
        departure_time="063300",
        departure_station_name="서울",
        arrival_station_name="부산",
    )


def formation(*car_nos: int, remaining: int = 40, room: str = "일반실") -> SeatCarListResponse:
    return SeatCarListResponse(
        cars=tuple(SeatCar(car_no, room, remaining, ()) for car_no in car_nos)
    )


def wire_partial_train(service, train, *, real, nearby):
    """코레일이 `train` 에는 `real` 호차만, 다른 날짜(일 단위 간격 → 호차)에는 `nearby` 호차만 주게 한다."""
    references = {}
    for offset in nearby:
        day = (datetime.strptime(train.departure_date, "%Y%m%d") + timedelta(days=offset)).strftime(
            "%Y%m%d"
        )
        references[day] = SimpleNamespace(
            train_no=train.train_no,
            train_class_code=train.train_class_code,
            departure_date=day,
            departure_time=train.departure_time,
        )

    def search(query):
        reference = references.get(query.departure_date)
        return SimpleNamespace(trains=[reference] if reference else [])

    def cars(item, **_):
        if item is train:
            return formation(*real, remaining=2)
        offset = (
            datetime.strptime(item.departure_date, "%Y%m%d")
            - datetime.strptime(train.departure_date, "%Y%m%d")
        ).days
        return formation(*nearby[offset])

    service._modern_client.search_trains.side_effect = search
    service._modern_client.get_seat_cars.side_effect = cars
    return references


def test_a_car_list_of_one_car_is_completed_from_the_same_trains_nearby_formations():
    # 코레일은 잔여석이 있는 호차만 목록에 줘요. 거의 매진된 열차는 한 호차만 와서 앱은 그 목록을 편성 전체로
    # 그렸고, 다른 호차의 좌석표(취소표 대기 후보)를 고를 수 없었어요. 다른 날짜의 목록도 일부만 올 수 있어 합쳐요.
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    wire_partial_train(service, train, real=[5], nearby={7: [3, 5], 14: [5, 7, 8]})

    response = service.seat_cars(train, "general", 1)

    assert {car.car_no: car.remaining_seat_count for car in response.cars} == {
        3: 0,
        5: 2,
        7: 0,
        8: 0,
    }
    assert [car.car_no for car in response.cars] == [3, 5, 7, 8]
    # 편성은 다른 날짜 열차를 묻는 것이라 좌석표 전체가 참고용이 되지는 않아요.
    assert not service.seat_layout_is_reference(train, "general", 1)


def test_a_car_the_list_left_out_is_drawn_from_the_nearby_formation_with_no_seat_for_sale():
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    references = wire_partial_train(service, train, real=[5], nearby={7: [3, 5], 14: [5, 7]})
    service.seat_cars(train, "general", 1)
    # 다른 날짜의 좌석표라 그날 팔린 좌석은 이 열차와 상관없어요. 좌석 배치만 쓰고 모두 "팔 수 없음"으로 돌려줘요.
    service._modern_client.get_seat_inventory.return_value = inventory(
        physical_seat(seat_no="000041", label="5A", sale_possible="Y"),
        physical_seat(seat_no="000042", label="5B", sale_possible="Y"),
    )

    response = service.seat_inventory(train, 7, "general", 1, allow_layout_reference=True)

    assert [seat.specification for seat in response.seats] == ["5A", "5B"]
    assert all(seat.sale_possible == "N" for seat in response.seats)
    assert response.remaining_count == 0
    # 7호차를 담은 날짜(14일 뒤)의 열차에서 읽어요.
    (reference_day,) = [
        day for day, item in references.items() if item.departure_date == "20260928"
    ]
    service._modern_client.get_seat_inventory.assert_called_once_with(
        references[reference_day], 7, passenger_count=1, room_class_code="1"
    )


def test_a_car_the_list_named_is_still_read_from_the_actual_train():
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    wire_partial_train(service, train, real=[5], nearby={7: [3, 5], 14: [5, 7]})
    real = inventory(physical_seat(label="5A", sale_possible="Y"))
    service._modern_client.get_seat_inventory.return_value = real
    service.seat_cars(train, "general", 1)

    response = service.seat_inventory(train, 5, "general", 1, allow_layout_reference=True)

    assert response is real
    assert response.seats[0].sale_possible == "Y"
    service._modern_client.get_seat_inventory.assert_called_once_with(
        train, 5, passenger_count=1, room_class_code="1"
    )
    # 참고 열차를 물으면서 코레일 쪽 열차 문맥이 바뀌었으니 실제 열차의 문맥을 한 번 되살려요.
    assert service._modern_client.get_seat_cars.call_args_list[-1].args == (train,)


def test_booking_reads_a_car_the_list_left_out_from_the_actual_train():
    # 예약 경로(allow_layout_reference=False)는 채운 호차를 절대 쓰지 않아요. 판매 가능 여부는 실제 열차만 알아요.
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    wire_partial_train(service, train, real=[5], nearby={7: [3, 5], 14: [5, 7]})
    real = inventory(physical_seat(label="3A", sale_possible="Y"))
    service._modern_client.get_seat_inventory.return_value = real
    service.seat_cars(train, "general", 1)

    assert service.seat_inventory(train, 3, "general", 1) is real
    service._modern_client.get_seat_inventory.assert_called_once_with(
        train, 3, passenger_count=1, room_class_code="1"
    )


def test_the_nearby_formations_are_looked_up_once_per_train_however_often_the_cars_are_listed():
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    wire_partial_train(service, train, real=[5], nearby={7: [3, 5], 14: [5, 7]})

    for _ in range(3):
        assert len(service.seat_cars(train, "general", 1).cars) == 3

    # 7일 뒤와 14일 뒤, 처음 한 번만
    assert service._modern_client.search_trains.call_count == 2


def test_a_full_formation_is_left_as_listed():
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    listed = formation(3, 5)
    wire_partial_train(service, train, real=[3, 5], nearby={7: [3, 5], 14: [5]})
    service._modern_client.get_seat_cars.side_effect = None
    service._modern_client.get_seat_cars.return_value = listed

    assert service.seat_cars(train, "general", 1) is listed


def test_a_nearby_formation_that_cannot_be_found_leaves_the_list_as_korail_gave_it():
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    listed = formation(5, remaining=2)
    service._modern_client.get_seat_cars.return_value = listed
    service._modern_client.search_trains.return_value = SimpleNamespace(trains=[])

    assert service.seat_cars(train, "general", 1) is listed
    # 못 찾았다는 사실도 기억해서, 목록을 볼 때마다 날짜를 다시 뒤지지 않아요.
    assert service.seat_cars(train, "general", 1) is listed
    assert service._modern_client.search_trains.call_count == 2


def test_a_date_korail_refuses_does_not_stop_the_next_date_from_being_read():
    # 예약 가능 기간(약 한 달)을 넘는 날은 검색이 "결과 없음"으로 실패해요. 앞 날짜가 그래도 다음 날짜를 봐요.
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    wire_partial_train(service, train, real=[5], nearby={7: [3, 5], 14: [5, 7]})
    search = service._modern_client.search_trains.side_effect

    def refuse_the_first_week(query):
        if query.departure_date == "20260921":
            raise KorailNoResultsError("ERI000", "결과가 없습니다.")
        return search(query)

    service._modern_client.search_trains.side_effect = refuse_the_first_week

    assert [car.car_no for car in service.seat_cars(train, "general", 1).cars] == [5, 7]


def test_a_failing_formation_lookup_does_not_fail_the_car_list_and_is_tried_again():
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    listed = formation(5, remaining=2)
    service._modern_client.get_seat_cars.return_value = listed
    service._modern_client.search_trains.side_effect = ConnectionError("timeout")

    assert service.seat_cars(train, "general", 1) is listed
    assert service.seat_cars(train, "general", 1) is listed
    assert service._modern_client.search_trains.call_count == 2


def test_a_reference_that_stops_answering_is_searched_again_by_the_next_car_list_not_by_each_car():
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    wire_partial_train(service, train, real=[5], nearby={7: [3, 5], 14: [5, 7]})
    service.seat_cars(train, "general", 1)
    searches = service._modern_client.search_trains.call_count
    service._modern_client.get_seat_inventory.side_effect = KorailAppError(
        "ERI411321", "잔여석이 없습니다."
    )

    for car_no in (3, 7):
        with pytest.raises(KorailAppError):
            service.seat_inventory(train, car_no, "general", 1, allow_layout_reference=True)
    # 실패한 호차마다 날짜를 다시 뒤지지 않아요(한 번에 읽는 도중 요청이 늘어나 실제 호차까지 밀려요).
    assert service._modern_client.search_trains.call_count == searches

    service._modern_client.get_seat_inventory.side_effect = None
    service.seat_cars(train, "general", 1)
    assert service._modern_client.search_trains.call_count == searches + 2


def test_a_transient_korail_error_is_not_remembered_as_no_formation():
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    listed = formation(5, remaining=2)
    service._modern_client.get_seat_cars.return_value = listed
    service._modern_client.search_trains.side_effect = KorailAppError("ERI999", "점검 중입니다.")

    assert service.seat_cars(train, "general", 1) is listed
    assert service.seat_cars(train, "general", 1) is listed
    assert service._modern_client.search_trains.call_count == 2


def test_a_new_session_works_out_the_missing_cars_before_reading_a_car_the_app_already_lists():
    # 502 뒤에 세션이 새로 만들어지면 인스턴스는 호차 목록을 읽은 적이 없지만, 앱은 채운 호차를 탭으로 가지고 있어요.
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    references = wire_partial_train(service, train, real=[5], nearby={7: [3, 5], 14: [5, 7]})
    service._modern_client.get_seat_inventory.return_value = inventory(physical_seat())

    response = service.seat_inventory(train, 3, "general", 1, allow_layout_reference=True)

    assert all(seat.sale_possible == "N" for seat in response.seats)
    assert service._modern_client.get_seat_inventory.call_args.args[0] is references["20260921"]


def test_a_partial_list_that_later_sells_out_draws_the_whole_train_from_one_reference():
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    wire_partial_train(service, train, real=[5], nearby={7: [3, 5], 14: [5, 7]})
    service.seat_cars(train, "general", 1)
    cars = service._modern_client.get_seat_cars.side_effect

    def sold_out_now(item, **kwargs):
        if item is train:
            raise KorailAppError("ERI411321", "잔여석이 없습니다.")
        return cars(item, **kwargs)

    service._modern_client.get_seat_cars.side_effect = sold_out_now
    service.seat_cars(train, "general", 1)

    assert service.seat_layout_is_reference(train, "general", 1)
    assert not service._formation_gaps.get((service._seat_key(train), "1", 1))


def test_booking_reads_no_nearby_formation_for_a_partial_car_list():
    # 대기 워커는 좌석표를 그리지 않고 예약해요. 다른 날짜 편성을 찾는 검색은 한 바퀴마다 늘어나는 비용이에요.
    service = service_with_modern_client()
    train = nearly_sold_out_train()
    listed = formation(5, remaining=2)
    service._modern_client.get_seat_cars.return_value = listed

    assert service.seat_cars(train, "general", 1, allow_layout_reference=False) is listed
    service._modern_client.search_trains.assert_not_called()


def test_a_departed_train_says_so_without_asking_korail():
    service = service_with_modern_client()
    service.clock = lambda: datetime(2026, 9, 26, 17, 15, tzinfo=RAIL_TIMEZONE)
    train = SimpleNamespace(train_no="107", departure_date="20260926", departure_time="171300")

    with pytest.raises(ValueError, match="이미 출발한 열차"):
        service.seat_cars(train, "special", 1)
    service._modern_client.get_seat_cars.assert_not_called()


def test_an_empty_car_list_is_read_as_sold_out_and_shows_a_nearby_formation():
    # KTX 107 특실(2026-09-26): 코레일이 오류 없이 호차 0개를 주자 앱에 "특실 좌석표가 아직 제공되지 않아요"만 떴어요.
    service = service_with_modern_client()
    train = SimpleNamespace(
        train_no="107",
        train_class_code="00",
        departure_date="20260926",
        departure_time="171300",
        departure_station_name="서울",
        arrival_station_name="부산",
    )
    reference = SimpleNamespace(
        train_no="107", train_class_code="00", departure_date="20261003", departure_time="171300"
    )
    cars = SeatCarListResponse(cars=(SeatCar(4, "특실", 1, ()),))
    service._modern_client.get_seat_cars.side_effect = [SeatCarListResponse(train_no="107"), cars]
    service._modern_client.search_trains.return_value = SimpleNamespace(trains=[reference])

    assert service.seat_cars(train, "special", 1) is cars
    assert service.seat_layout_is_reference(train, "special", 1)


def test_booking_takes_an_empty_car_list_as_no_seat_without_a_reference_search():
    service = service_with_modern_client()
    train = SimpleNamespace(train_no="107", departure_date="20260926", departure_time="171300")
    service._modern_client.get_seat_cars.return_value = SeatCarListResponse(train_no="107")

    assert not service.seat_cars(train, "special", 1, allow_layout_reference=False).cars
    service._modern_client.search_trains.assert_not_called()


def test_booking_reads_no_nearby_formation_for_a_sold_out_train():
    # The cancellation-wait worker books, it does not draw a map: another
    # date's formation would cost up to seven searches and seat nobody.
    service = service_with_modern_client()
    train = SimpleNamespace(
        train_no="009",
        train_class_code="00",
        departure_date="20260914",
        departure_time="063300",
        departure_station_name="서울",
        arrival_station_name="부산",
    )
    service._modern_client.get_seat_cars.side_effect = KorailAppError(
        "ERI411321", "잔여석이 없습니다."
    )

    response = service.seat_cars(train, "general", 1, allow_layout_reference=False)

    assert response.cars == ()
    service._modern_client.search_trains.assert_not_called()
    assert not service.seat_layout_is_reference(train, "general", 1)


def test_nearby_layout_uses_one_passenger_for_a_larger_party():
    service = service_with_modern_client()
    train = SimpleNamespace(
        train_no="009",
        train_class_code="00",
        departure_date="20260914",
        departure_time="063300",
        departure_station_name="서울",
        arrival_station_name="부산",
    )
    reference = SimpleNamespace(
        train_no="009",
        train_class_code="00",
        departure_date="20260921",
        departure_time="063300",
    )
    cars = SeatCarListResponse(cars=(SeatCar(3, "특실", 1, ()),))
    service._modern_client.get_seat_cars.side_effect = [
        KorailAppError("ERI411321", "잔여석이 없습니다."),
        cars,
    ]
    service._modern_client.search_trains.return_value = SimpleNamespace(trains=[reference])
    service._modern_client.get_seat_inventory.return_value = inventory(
        physical_seat(sale_possible="N")
    )

    assert service.seat_cars(train, "special", 2) is cars
    query = service._modern_client.search_trains.call_args.args[0]
    assert query.passengers == 1
    assert service.seat_inventory(train, 3, "special", 2).car_no == 3
    service._modern_client.get_seat_inventory.assert_called_once_with(
        reference, 3, passenger_count=1, room_class_code="2"
    )


def test_reading_a_whole_formation_restores_the_schedule_context_once():
    # What the batch route is for: eighteen cars must not mean eighteen
    # ScheduleView calls. get_seat_cars is what restores that server-side
    # context, and the cached seat context is what keeps it at one.
    service = service_with_modern_client()
    train = SimpleNamespace(train_no="015")
    service._modern_client.get_seat_cars.return_value = SeatCarListResponse(
        cars=tuple(SeatCar(car_no, "일반실", 1, ()) for car_no in range(1, 19))
    )
    service._modern_client.get_seat_inventory.return_value = inventory(physical_seat())

    service.seat_cars(train, "general", 1)
    for car_no in range(1, 19):
        service.seat_inventory(train, car_no, "general", 1, allow_layout_reference=True)

    assert service._modern_client.get_seat_cars.call_count == 1
    assert service._modern_client.search_trains.call_count == 0
    assert service._modern_client.get_seat_inventory.call_count == 18


def test_a_sold_out_formation_looks_up_its_reference_date_only_once():
    # The expensive path: a sold-out train searches up to seven days for a
    # matching formation. Doing that per car would be eighteen searches.
    service = service_with_modern_client()
    train = SimpleNamespace(
        train_no="009",
        train_class_code="00",
        departure_date="20260914",
        departure_time="063300",
        departure_station_name="서울",
        arrival_station_name="부산",
    )
    reference = SimpleNamespace(
        train_no="009",
        train_class_code="00",
        departure_date="20260921",
        departure_time="063300",
    )
    cars = SeatCarListResponse(
        cars=tuple(SeatCar(car_no, "일반실", 1, ()) for car_no in range(1, 19))
    )
    service._modern_client.get_seat_cars.side_effect = [
        KorailAppError("ERI411321", "잔여석이 없습니다."),
        cars,
    ]
    service._modern_client.search_trains.return_value = SimpleNamespace(trains=[reference])
    service._modern_client.get_seat_inventory.return_value = inventory(
        physical_seat(sale_possible="N")
    )

    service.seat_cars(train, "general", 1)
    for car_no in range(1, 19):
        service.seat_inventory(train, car_no, "general", 1, allow_layout_reference=True)

    # One failed attempt plus one against the reference, and one date search.
    assert service._modern_client.get_seat_cars.call_count == 2
    assert service._modern_client.search_trains.call_count == 1
    assert service._modern_client.get_seat_inventory.call_count == 18
    assert service.seat_layout_is_reference(train, "general", 1)


def test_sold_out_inventory_is_empty_until_actual_train_has_a_seat():
    service = service_with_modern_client()
    train = SimpleNamespace(train_no="009")
    service._modern_client.get_seat_cars.side_effect = KorailAppError(
        "ERI411321", "잔여석이 없습니다."
    )

    response = service.seat_inventory(train, 3, "general", 1)

    assert response.car_no == 3
    assert response.seats == ()
    service._modern_client.get_seat_inventory.assert_not_called()


def test_selection_inventory_can_use_nearby_matching_formation():
    service = service_with_modern_client()
    train = SimpleNamespace(
        train_no="009",
        train_class_code="00",
        departure_date="20260914",
        departure_time="063300",
        departure_station_name="서울",
        arrival_station_name="부산",
    )
    reference = SimpleNamespace(
        train_no="009",
        train_class_code="00",
        departure_date="20260915",
        departure_time="063300",
    )
    cars = SeatCarListResponse(cars=(SeatCar(3, "일반실", 1, ()),))
    service._modern_client.get_seat_cars.side_effect = [
        KorailAppError("ERI411321", "잔여석이 없습니다."),
        cars,
    ]
    service._modern_client.search_trains.return_value = SimpleNamespace(trains=[reference])
    service._modern_client.get_seat_inventory.return_value = inventory(
        physical_seat(sale_possible="N")
    )

    response = service.seat_inventory(train, 3, "general", 1, allow_layout_reference=True)

    assert response.car_no == 3
    assert service.seat_layout_is_reference(train, "general", 1)
    service._modern_client.get_seat_inventory.assert_called_once_with(
        reference, 3, passenger_count=1, room_class_code="1"
    )


def test_reserve_designated_uses_wire_seat_number_not_label():
    service = service_with_modern_client()
    train = SimpleNamespace(train_no="015")
    seat = physical_seat()
    current_inventory = inventory(seat)
    target = SeatTarget(car_no=3, seat_no="000041", label="5A", row=5, column="A")
    hold = SimpleNamespace(pnr_no="PRIVATE")
    service._modern_client.reserve.return_value = hold

    assert (
        service.reserve_designated(
            train, current_inventory, [target], passenger_count=1, seat_class="general"
        )
        is hold
    )

    kwargs = service._modern_client.reserve.call_args.kwargs
    assert kwargs["job_type"] is KorailReservationJobType.SEAT_DESIGNATED
    assert kwargs["seats"][0].seat_no == "000041"
    assert kwargs["passengers"].adult == 1
    assert kwargs["seat_class"].value == "1"


def test_reserve_designated_rejects_stale_or_mismatched_selection():
    service = service_with_modern_client()
    train = SimpleNamespace(train_no="015")
    sold = physical_seat(sale_possible="N")
    current_inventory = inventory(sold)

    with pytest.raises(ValueError, match="판매 가능한 좌석"):
        service.reserve_designated(
            train,
            current_inventory,
            [SeatTarget(car_no=3, seat_no="000041", label="5A")],
            passenger_count=1,
            seat_class="general",
        )

    with pytest.raises(ValueError, match="승객 수"):
        service.reserve_designated(
            train,
            inventory(physical_seat()),
            [SeatTarget(car_no=3, seat_no="000041", label="5A")],
            passenger_count=2,
            seat_class="general",
        )
    service._modern_client.reserve.assert_not_called()


def test_seat_map_keys_are_owner_scoped_and_expire():
    now = datetime(2026, 9, 14, 12, tzinfo=UTC)
    service = SeatMapService(clock=lambda: now, token_factory=lambda: "opaque")
    train = SimpleNamespace(train_no="015")
    key = service.remember_train("owner-a", train)

    assert key == "opaque"
    assert service.get_train("owner-a", key) is train
    with pytest.raises(SeatMapNotFoundError):
        service.get_train("owner-b", key)

    service = SeatMapService(
        clock=lambda: now + timedelta(minutes=11), token_factory=lambda: "unused"
    )
    service._entries[key] = SimpleNamespace(
        owner_id="owner-a", train=train, expires_at=now + timedelta(minutes=10)
    )
    with pytest.raises(SeatMapExpiredError):
        service.get_train("owner-a", key)


def test_seat_map_serializes_real_layout_without_inventing_family_seats():
    service = SeatMapService()
    cars = SeatCarListResponse(
        cars=(
            SeatCar(
                car_no=3,
                room_class_name="일반실",
                remaining_seat_count=1,
                attributes=(SeatAttribute(name="유아동반", code="BABY"),),
            ),
        )
    )
    seats = inventory(
        physical_seat(),
        physical_seat(seat_no="000042", label="5B", sale_possible="N"),
    )

    assert service.describe_cars(cars) == [
        {
            "carNo": 3,
            "roomClassName": "일반실",
            "remainingSeatCount": 1,
            "attributes": [{"name": "유아동반", "code": "BABY"}],
        }
    ]
    described = service.describe_inventory(seats)
    assert described["layoutType"] == 4
    assert described["arrangementCode"] == "AB_CD"
    assert described["seats"][0] == {
        "carNo": 3,
        "seatNo": "000041",
        "label": "5A",
        "salePossible": True,
        "direction": "1",
        "floor": "1",
        "row": 5,
        "column": "A",
        "adjacencyGroup": "5:left",
        "position": 1,
        "rowPosition": 1,
        "familyLabel": "",
    }
    assert described["seats"][1]["adjacencyGroup"] == "5:left"
    assert all(not seat["familyLabel"] for seat in described["seats"])


def test_numeric_seat_labels_get_a_grid_so_the_app_can_show_them():
    # 무궁화호·ITX-새마을·누리로 label seats "1".."72" with no column letter.
    # Without a row the app drops every seat and the car looks empty.
    service = SeatMapService()
    described = service.describe_inventory(
        inventory(
            physical_seat(seat_no="000001", label="1"),
            physical_seat(seat_no="000002", label="2"),
            physical_seat(seat_no="000004", label="4"),
            physical_seat(seat_no="000005", label="5"),
        )
    )
    seats = {seat["label"]: seat for seat in described["seats"]}

    assert [(seats[n]["row"], seats[n]["column"]) for n in ("1", "2", "4", "5")] == [
        (1, "A"),
        (1, "B"),
        (1, "D"),
        (2, "A"),
    ]
    # 1·2 sit together, 3·4 sit together, and 5 starts the next row.
    assert seats["1"]["adjacencyGroup"] == seats["2"]["adjacencyGroup"] == "1:left"
    assert seats["4"]["adjacencyGroup"] == "1:right"
    assert seats["5"]["adjacencyGroup"] == "2:left"
    assert (seats["1"]["position"], seats["2"]["position"]) == (1, 2)
    # Numbered seats are counted across the row exactly as A·B·C·D are, over
    # the seats the car actually reports: "3" is missing here, so "4" is third.
    assert [seats[n]["rowPosition"] for n in ("1", "2", "4", "5")] == [1, 2, 3, 1]


def test_row_position_numbers_a_row_across_the_aisle():
    # Three people can only sit together by crossing the aisle, so the app
    # needs each seat's place in the whole row, not just in its pair.
    service = SeatMapService()
    described = service.describe_inventory(
        inventory(
            physical_seat(seat_no="000044", label="5D"),
            physical_seat(seat_no="000041", label="5A"),
            physical_seat(seat_no="000043", label="5C"),
            physical_seat(seat_no="000042", label="5B"),
            physical_seat(seat_no="000051", label="6A"),
        )
    )
    seats = {seat["label"]: seat for seat in described["seats"]}

    assert [seats[label]["rowPosition"] for label in ("5A", "5B", "5C", "5D")] == [1, 2, 3, 4]
    assert seats["6A"]["rowPosition"] == 1


def test_unreadable_seat_label_still_has_no_row():
    service = SeatMapService()
    described = service.describe_inventory(inventory(physical_seat(label="창측")))

    assert described["seats"][0]["row"] is None
    assert described["seats"][0]["column"] == ""
    assert described["seats"][0]["adjacencyGroup"] == ""
    assert described["seats"][0]["rowPosition"] == 0
    assert described["seats"][0]["label"] == "창측"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("01", (1, "A")),
        ("72", (18, "D")),
        # Nothing to place, and nothing that may raise either: int() chokes on
        # "²", while "１２" and the Arabic-Indic digits would be read as 12 and
        # seat someone where the label never said.
        ("0", (None, "")),
        ("²", (None, "")),
        ("１２", (None, "")),
        ("٤", (None, "")),
        # Past this the derived row fails seat_plan.py's 1..999 bound, and that
        # rejects the whole plan rather than this one seat.
        ("4000", (None, "")),
    ],
)
def test_numeric_grid_places_only_plain_ascii_seat_numbers(label, expected):
    described = SeatMapService().describe_inventory(inventory(physical_seat(label=label)))
    seat = described["seats"][0]

    assert (seat["row"], seat["column"]) == expected
    assert seat["label"] == label


def test_attribute_code_alone_does_not_invent_family_seat():
    seat = physical_seat()
    seat = PhysicalSeat(**{**seat.__dict__, "requested_attribute_code": "015", "message": ""})
    service = SeatMapService()

    assert service.describe_inventory(inventory(seat))["seats"][0]["familyLabel"] == ""


def test_korail_companion_message_marks_family_seat():
    seat = physical_seat()
    seat = PhysicalSeat(
        **{
            **seat.__dict__,
            "requested_attribute_code": "052",
            "message": "4인 동반석 역방향 좌석으로 5% 할인 적용",
        }
    )
    service = SeatMapService()

    assert service.describe_inventory(inventory(seat))["seats"][0]["familyLabel"] == "4인 동반석"
