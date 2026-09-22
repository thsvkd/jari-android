"""Short-lived, owner-scoped handles and safe mobile seat-map payloads."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe

from korail_bot.models import parse_seat_label


class SeatMapNotFoundError(LookupError):
    """A train handle does not exist for this owner."""


class SeatMapExpiredError(LookupError):
    """A train handle existed but its short lease expired."""


@dataclass(frozen=True)
class _TrainEntry:
    owner_id: str
    train: object
    expires_at: datetime


class SeatMapService:
    """Keep server train objects out of the client and shape seat responses."""

    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._ttl = ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token_factory = token_factory or (lambda: token_urlsafe(24))
        self._entries: dict[str, _TrainEntry] = {}

    def remember_train(self, owner_id: str, train: object) -> str:
        """Return an opaque handle for one owner and one current train result."""
        self._purge_expired()
        key = self._token_factory()
        while key in self._entries:
            key = self._token_factory()
        self._entries[key] = _TrainEntry(
            owner_id=str(owner_id), train=train, expires_at=self._clock() + self._ttl
        )
        return key

    def get_train(self, owner_id: str, train_key: str) -> object:
        entry = self._entries.get(str(train_key))
        if entry is None or entry.owner_id != str(owner_id):
            raise SeatMapNotFoundError("열차 정보를 찾을 수 없습니다. 다시 조회해 주세요.")
        if entry.expires_at <= self._clock():
            self._entries.pop(str(train_key), None)
            raise SeatMapExpiredError("열차 정보가 만료되었습니다. 다시 조회해 주세요.")
        return entry.train

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)

    @staticmethod
    def describe_cars(response) -> list[dict]:
        return [
            {
                "carNo": car.car_no,
                "roomClassName": car.room_class_name,
                "remainingSeatCount": car.remaining_seat_count,
                "attributes": [
                    {"name": attribute.name, "code": attribute.code}
                    for attribute in car.attributes
                ],
            }
            for car in response.cars
        ]

    def describe_inventory(self, response) -> dict:
        car_no = response.car_no
        seats: list[dict] = []
        # Seats of one row, keyed by row, as (side rank, column, entry). The
        # row-wide position can only be numbered once the whole row is known.
        rows: dict[int, list[tuple[int, str, dict]]] = {}
        for seat in response.seats:
            parsed = parse_seat_label(seat.specification)
            row, column = parsed if parsed else self._numeric_grid(seat.specification)
            side, position = self._seat_side(column)
            family_label = ""
            message = seat.message.strip()
            # requested_attribute_code 015 is present on ordinary KTX seats
            # too. Korail's seat-specific message is the reliable marker.
            if "4인 동반석" in message:
                family_label = "4인 동반석"
            entry = {
                "carNo": car_no,
                "seatNo": seat.seat_no,
                "label": seat.specification,
                "salePossible": seat.sale_possible == "Y",
                "direction": seat.direction_code,
                "floor": seat.floor or "",
                "row": row,
                "column": column,
                "adjacencyGroup": f"{row}:{side}" if row is not None and side else "",
                "position": position,
                "rowPosition": 0,
                "familyLabel": family_label,
            }
            seats.append(entry)
            if row is not None and side:
                rows.setdefault(row, []).append((0 if side == "left" else 1, column, entry))
        for members in rows.values():
            # Left side first, then right, so a 2+2 row reads A,B,C,D.
            for index, (_, _, entry) in enumerate(sorted(members, key=lambda item: item[:2]), 1):
                entry["rowPosition"] = index
        return {
            "carNo": car_no,
            "layoutType": response.layout_type,
            "arrangementCode": response.arrangement_code,
            "remainingCount": response.remaining_count,
            "totalCount": response.total_count,
            "seats": seats,
            "windows": [
                {
                    "startLocationRatio": window.start_location_ratio,
                    "closeLocationRatio": window.close_location_ratio,
                }
                for window in response.windows
            ],
        }

    @staticmethod
    def _numeric_grid(label: str | None) -> tuple[int | None, str]:
        """
        Place a plainly numbered seat on the same row/column grid as a KTX one.

        무궁화호, ITX-새마을 and 누리로 label seats "1".."72" with no column
        letter, so the label parser cannot read them and the app would drop
        every one of those seats. They are numbered four abreast across the
        car, which is exactly what A·B·C·D already mean here: 1 and 4 at the
        windows, 2 and 3 on the aisle.

        ponytail: assumes 4-abreast numbering. A car of a different width would
        need arrangement_code/layout_type read instead of this.
        """
        text = str(label or "").strip()
        # ASCII digits only. "１２" and the Arabic-Indic digits satisfy isdigit()
        # and int() reads them as 12, which would quietly seat someone in a
        # place the label never named. A superscript "²" satisfies it too, and
        # int() then raises.
        if not (text.isascii() and text.isdigit()):
            return None, ""
        number = int(text)
        # The ceiling is what seat_plan.py will take back: a row past 999 fails
        # _bounded_int, and that rejects the whole plan rather than this one
        # seat. No formation numbers a car anywhere near this high.
        if not 1 <= number <= 999 * 4:
            return None, ""
        return (number - 1) // 4 + 1, "ABCD"[(number - 1) % 4]

    @staticmethod
    def _seat_side(column: str) -> tuple[str, int]:
        # Korail seat labels use A/B on one side and C/D on the other. Cars
        # with only three columns naturally produce A/B + C.
        if column in {"A", "B"}:
            return "left", 1 if column == "A" else 2
        if column in {"C", "D"}:
            return "right", 1 if column == "C" else 2
        return "", 0
