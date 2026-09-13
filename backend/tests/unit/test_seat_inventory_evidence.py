import json

from korail_mobile_api import PhysicalSeat, SeatAttribute, SeatCar, SeatInventoryResponse
from scripts.verify_mobile_seat_inventory import build_inventory_evidence


def test_evidence_contains_layout_capabilities_without_sensitive_identifiers():
    car = SeatCar(
        car_no=8,
        room_class_name="일반실",
        remaining_seat_count=2,
        attributes=(SeatAttribute(name="유아동반", code="BABY"),),
        total_seat_count=48,
    )
    inventory = SeatInventoryResponse(
        layout_type=4,
        arrangement_code="AB_CD",
        remaining_count=2,
        total_count=48,
        car_no=8,
        seats=(
            PhysicalSeat(
                seat_no="SECRET-WIRE-ID",
                sale_possible="Y",
                direction_code="1",
                other_attribute_code="BABY",
                requested_attribute_code="",
                floor="1",
                specification="5A",
                sequence_no="10",
                message_code="",
                message="유아동반석",
                visual_message_division_code="",
            ),
        ),
    )

    evidence = build_inventory_evidence("general", car, inventory)
    rendered = json.dumps(evidence, ensure_ascii=False)

    assert evidence["seatClass"] == "general"
    assert evidence["layoutType"] == 4
    assert evidence["columnCount"] == 1
    assert evidence["hasDirection"] is True
    assert evidence["hasFloor"] is True
    assert evidence["familyMarkers"] == ["유아동반", "유아동반석"]
    assert evidence["familyOtherAttributeCodes"] == ["BABY"]
    assert evidence["familyRequestedAttributeCodes"] == []
    assert "SECRET-WIRE-ID" not in rendered
    assert "5A" not in rendered
    assert '"carNo"' not in rendered
