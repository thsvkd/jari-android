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

from korail_bot.models import SeatTarget
from korail_bot.services.korail_service import KorailService
from korail_bot.services.seat_map_service import (
    SeatMapExpiredError,
    SeatMapNotFoundError,
    SeatMapService,
)


def service_with_modern_client() -> KorailService:
    service = KorailService()
    service._logged_in = True
    service._modern_client = MagicMock()
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
    service._modern_client.get_seat_inventory.assert_called_once_with(
        train, 3, passenger_count=1, room_class_code="1"
    )


def test_reserve_designated_uses_wire_seat_number_not_label():
    service = service_with_modern_client()
    train = SimpleNamespace(train_no="015")
    seat = physical_seat()
    current_inventory = inventory(seat)
    target = SeatTarget(car_no=3, seat_no="000041", label="5A", row=5, column="A")
    hold = SimpleNamespace(pnr_no="PRIVATE")
    service._modern_client.reserve.return_value = hold

    assert service.reserve_designated(
        train, current_inventory, [target], passenger_count=1, seat_class="general"
    ) is hold

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
        "familyLabel": "",
    }
    assert described["seats"][1]["adjacencyGroup"] == "5:left"
    assert all(not seat["familyLabel"] for seat in described["seats"])


def test_explicit_family_attribute_is_the_only_family_label_source():
    seat = physical_seat()
    seat = PhysicalSeat(
        **{**seat.__dict__, "other_attribute_code": "FAMILY", "message": "가족석"}
    )
    service = SeatMapService(family_attribute_codes={"FAMILY"})

    assert service.describe_inventory(inventory(seat))["seats"][0]["familyLabel"] == "가족석"
