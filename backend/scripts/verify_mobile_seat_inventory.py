"""Read a small live Korail seat sample and emit only non-sensitive evidence."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from datetime import timedelta
from pathlib import Path

from korail2 import TrainType

from korail_bot.models import parse_seat_label
from korail_bot.services.korail_service import KorailService
from korail_bot.utils.timezone import RAIL_TIMEZONE, utc_now

_FAMILY_TERMS = ("가족", "유아", "동반")


def build_inventory_evidence(seat_class: str, car, inventory) -> dict:
    """Summarize layout capability without train, car, account, or seat IDs."""
    labels = [parse_seat_label(seat.specification) for seat in inventory.seats]
    parsed = [label for label in labels if label is not None]
    marker_candidates = [attribute.name for attribute in car.attributes]
    marker_candidates.extend(seat.message for seat in inventory.seats if seat.message)
    family_markers = sorted(
        {
            marker.strip()
            for marker in marker_candidates
            if marker.strip() and any(term in marker for term in _FAMILY_TERMS)
        }
    )
    family_seats = [
        seat
        for seat in inventory.seats
        if seat.message and any(term in seat.message for term in _FAMILY_TERMS)
    ]
    return {
        "seatClass": seat_class,
        "roomClassName": car.room_class_name,
        "layoutType": inventory.layout_type,
        "arrangementCode": inventory.arrangement_code,
        "seatCount": len(inventory.seats),
        "sellableCount": sum(seat.sale_possible == "Y" for seat in inventory.seats),
        "rowCount": len({row for row, _column in parsed}),
        "columnCount": len({column for _row, column in parsed}),
        "hasDirection": any(bool(seat.direction_code) for seat in inventory.seats),
        "hasFloor": any(bool(seat.floor) for seat in inventory.seats),
        "hasSeatAttributes": any(
            bool(seat.other_attribute_code or seat.requested_attribute_code)
            for seat in inventory.seats
        ),
        "carAttributes": sorted({attribute.name for attribute in car.attributes}),
        "familyMarkers": family_markers,
        "familyOtherAttributeCodes": sorted(
            {seat.other_attribute_code for seat in family_seats if seat.other_attribute_code}
        ),
        "familyRequestedAttributeCodes": sorted(
            {
                seat.requested_attribute_code
                for seat in family_seats
                if seat.requested_attribute_code
            }
        ),
    }


def collect_live_evidence(username: str, password: str) -> dict:
    service = KorailService()
    if not service.login(username, password):
        raise RuntimeError("코레일 로그인에 실패했습니다.")

    local_date = (utc_now().astimezone(RAIL_TIMEZONE) + timedelta(days=7)).strftime("%Y%m%d")
    trains = service.search_selectable_trains(
        dep_date=local_date,
        src_locate="서울",
        dst_locate="부산",
        dep_time="060000",
        max_dep_time="2200",
        train_type=TrainType.KTX,
        passenger_count=1,
    )
    samples: list[dict] = []
    for seat_class in ("general", "special"):
        for train in trains:
            try:
                cars = service.seat_cars(train, seat_class, 1)
            except Exception:
                continue
            for car in cars.cars:
                try:
                    inventory = service.seat_inventory(
                        train, car.car_no, seat_class, passenger_count=1
                    )
                except Exception:
                    continue
                samples.append(build_inventory_evidence(seat_class, car, inventory))
                break
            if any(sample["seatClass"] == seat_class for sample in samples):
                break
    return {
        "service": "Korail",
        "readOnly": True,
        "sampleCount": len(samples),
        "classesFound": sorted({sample["seatClass"] for sample in samples}),
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    username = os.getenv("KORAIL_VERIFY_USERNAME") or getpass.getpass(
        "코레일 회원번호 또는 휴대전화: "
    )
    password = os.getenv("KORAIL_VERIFY_PASSWORD") or getpass.getpass("코레일 비밀번호: ")
    evidence = collect_live_evidence(username, password)
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
