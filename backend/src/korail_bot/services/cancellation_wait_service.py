"""Poll current Korail seat maps and reserve only user-approved seats."""

from __future__ import annotations

from dataclasses import dataclass

from korail_bot.models import CancellationWaitPlan, SeatTarget


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

        if self.plan.strategy == "consecutive":
            return self._poll_consecutive(trains_by_number)
        return self._poll_independent(trains_by_number, excluded or set())

    def _poll_independent(
        self,
        trains_by_number: dict[str, object],
        excluded: set[tuple[str, str, int, str]],
    ) -> DesignatedCapture | None:
        for wanted in self.plan.trains:
            train = trains_by_number.get(wanted.train_no)
            if train is None:
                continue
            for car_no, candidates in self._by_car(wanted.targets).items():
                inventory = self.rail.seat_inventory(train, car_no, wanted.seat_class, 1)
                sellable = {
                    (seat.seat_no, seat.specification)
                    for seat in inventory.seats
                    if seat.sale_possible == "Y"
                }
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

    def _poll_consecutive(self, trains_by_number: dict[str, object]) -> DesignatedCapture | None:
        inventories: dict[tuple[str, str, int], object] = {}
        classes = {(item.train_no, item.seat_class) for item in self.plan.trains}
        for block in self.plan.consecutive_groups():
            target = block[0]
            wanted = next(
                item
                for item in self.plan.trains
                if target in item.targets and (item.train_no, item.seat_class) in classes
            )
            train = trains_by_number.get(wanted.train_no)
            if train is None:
                continue
            key = (wanted.train_no, wanted.seat_class, target.car_no)
            inventory = inventories.get(key)
            if inventory is None:
                inventory = self.rail.seat_inventory(
                    train, target.car_no, wanted.seat_class, self.plan.passenger_count
                )
                inventories[key] = inventory
            sellable = {
                (seat.seat_no, seat.specification)
                for seat in inventory.seats
                if seat.sale_possible == "Y"
            }
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

    @staticmethod
    def _by_car(targets: tuple[SeatTarget, ...]) -> dict[int, list[SeatTarget]]:
        grouped: dict[int, list[SeatTarget]] = {}
        for target in targets:
            grouped.setdefault(target.car_no, []).append(target)
        return grouped
