"""Poll current Korail seat maps and reserve only user-approved seats."""

from __future__ import annotations

from dataclasses import dataclass

from korail_bot.models import CancellationWaitPlan, SeatTarget, TrainSeatTargets
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)

# What a current-API train answers for a class that can still be booked.
AVAILABLE_RESERVATION_CODE = "11"
# Korail's own app blocks booking on this word, not on the code (see the
# TrainSummary docstring in korail_mobile_api), so a train is only skipped
# when the code and the word agree. "좌석부족" still gets read.
SOLD_OUT_NAME = "매진"


def train_label(train) -> str:
    """A current-API train as people read it. Its repr is not display text."""
    name = getattr(train, "train_class_name", None) or "KTX"
    no = str(getattr(train, "train_no", "") or "")
    src = getattr(train, "departure_station_name", None) or "출발역"
    dst = getattr(train, "arrival_station_name", None) or "도착역"
    dep = str(getattr(train, "departure_time", "") or "")
    clock = f"{dep[:2]}:{dep[2:4]}" if len(dep) >= 4 else ""
    return f"{name} {no} {src} → {dst} {clock}".strip()


@dataclass(frozen=True)
class DesignatedCapture:
    """One newly secured Korail hold and the physical seats it contains."""

    hold: object
    train: object
    seat_class: str
    targets: tuple[SeatTarget, ...]


class CancellationWaitService:
    """Find cancellations without ever widening the approved seat range."""

    def __init__(self, rail, plan: CancellationWaitPlan, search_kwargs: dict) -> None:
        self.rail = rail
        self.plan = plan
        self.search_kwargs = search_kwargs

    def poll_once(
        self, excluded: set[tuple[str, str, int, str]] | None = None
    ) -> DesignatedCapture | None:
        """Run one read pass and reserve at most one matching hold."""
        passenger_count = 1 if self.plan.strategy == "independent" else self.plan.passenger_count
        trains = self.rail.search_selectable_trains(
            **self.search_kwargs, passenger_count=passenger_count
        )
        trains_by_number = {
            str(getattr(train, "train_no", "") or ""): train for train in trains
        }
        consecutive = self.plan.strategy == "consecutive"
        blocks_by_train = self._blocks_by_train() if consecutive else {}

        attempted = 0
        failed = 0
        failure: Exception | None = None
        for wanted in self.plan.trains:
            train = trains_by_number.get(wanted.train_no)
            if train is None:
                continue
            attempted += 1
            try:
                if consecutive:
                    capture = self._consecutive_capture(
                        train, wanted, blocks_by_train.get(wanted, ())
                    )
                else:
                    capture = self._independent_capture(train, wanted, excluded or set())
            except Exception as exc:
                # One train's outage is not the pass's outage: the next train
                # may well be the one holding the seat this user is waiting for.
                failed += 1
                failure = exc
                logger.warning(
                    "취소표 열차 조회 실패: train_no=%s (%s)", wanted.train_no, type(exc).__name__
                )
                continue
            if capture is not None:
                return capture
        if failure is not None and failed == attempted:
            # Nothing was read at all, so this is a failed pass and the caller's
            # backoff has to see it.
            raise failure
        return None

    def _open_cars(self, train, wanted, passenger_count: int, candidates: set[int]) -> set[int]:
        """
        Which candidate cars are worth a seat read this pass.

        A sold-out class costs nothing to skip, and one car list costs one
        request where reading every candidate car costs one each.
        """
        if not candidates:
            return set()
        code = getattr(train, f"{wanted.seat_class}_reservation_code", None)
        name = getattr(train, f"{wanted.seat_class}_availability_name", None)
        if code != AVAILABLE_RESERVATION_CODE and str(name or "").strip() == SOLD_OUT_NAME:
            return set()
        # allow_layout_reference=False: a formation from a nearby date is for
        # drawing a map, and this is booking. Here it means "skip this train".
        cars = self.rail.seat_cars(
            train, wanted.seat_class, passenger_count, allow_layout_reference=False
        )
        return {
            car.car_no for car in cars.cars if car.remaining_seat_count > 0
        } & candidates

    def _independent_capture(self, train, wanted, excluded) -> DesignatedCapture | None:
        by_car = self._by_car(wanted.targets)
        open_cars = self._open_cars(train, wanted, 1, set(by_car))
        for car_no, candidates in by_car.items():
            if car_no not in open_cars:
                continue
            inventory = self.rail.seat_inventory(train, car_no, wanted.seat_class, 1)
            sellable = self._sellable(inventory)
            target = next((item for item in candidates if (
                (item.seat_no, item.label) in sellable
                and (wanted.train_no, wanted.seat_class, item.car_no, item.seat_no)
                not in excluded
            )), None)
            if target is None:
                continue
            hold = self.rail.reserve_designated(
                train, inventory, [target], passenger_count=1, seat_class=wanted.seat_class
            )
            return DesignatedCapture(hold, train, wanted.seat_class, (target,))
        return None

    def _consecutive_capture(self, train, wanted, blocks) -> DesignatedCapture | None:
        open_cars = self._open_cars(
            train, wanted, self.plan.passenger_count, {block[0].car_no for block in blocks}
        )
        inventories: dict[int, object] = {}
        for block in blocks:
            car_no = block[0].car_no
            if car_no not in open_cars:
                continue
            inventory = inventories.get(car_no)
            if inventory is None:
                inventory = self.rail.seat_inventory(
                    train, car_no, wanted.seat_class, self.plan.passenger_count
                )
                inventories[car_no] = inventory
            sellable = self._sellable(inventory)
            if not all((item.seat_no, item.label) in sellable for item in block):
                continue
            hold = self.rail.reserve_designated(
                train,
                inventory,
                list(block),
                passenger_count=self.plan.passenger_count,
                seat_class=wanted.seat_class,
            )
            return DesignatedCapture(hold, train, wanted.seat_class, block)
        return None

    def _blocks_by_train(self) -> dict[TrainSeatTargets, list[tuple[SeatTarget, ...]]]:
        """Each consecutive block under the plan train it was chosen from."""
        grouped: dict[TrainSeatTargets, list[tuple[SeatTarget, ...]]] = {}
        for train_targets, block in self.plan.consecutive_groups():
            grouped.setdefault(train_targets, []).append(block)
        return grouped

    @staticmethod
    def _sellable(inventory) -> set[tuple[str, str]]:
        return {
            (seat.seat_no, seat.specification)
            for seat in inventory.seats
            if seat.sale_possible == "Y"
        }

    @staticmethod
    def _by_car(targets: tuple[SeatTarget, ...]) -> dict[int, list[SeatTarget]]:
        grouped: dict[int, list[SeatTarget]] = {}
        for target in targets:
            grouped.setdefault(target.car_no, []).append(target)
        return grouped
