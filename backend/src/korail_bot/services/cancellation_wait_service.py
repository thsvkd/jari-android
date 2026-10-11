"""Poll current Korail seat maps and reserve only user-approved seats."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from korail_mobile_api.errors import KorailAppError, KorailProtocolError, KorailTransportError

from korail_bot.models import CancellationWaitPlan, SeatTarget, TrainSeatTargets
from korail_bot.services.korail_service import DUPLICATE_RESERVATION_CODE
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)

# What a current-API train answers for a class that can still be booked.
AVAILABLE_RESERVATION_CODE = "11"
# Korail's own app blocks booking on this word, not on the code (see the
# TrainSummary docstring in korail_mobile_api), so a train is only skipped
# when the code and the word agree. "좌석부족" still gets read.
SOLD_OUT_NAME = "매진"
# How long a lost answer is looked for on the unpaid list. Korail makes the
# hold while it handles the POST, before the answer that went missing, so a
# hold that exists is listed within moments; minutes cover a slow list
# without reading it for a whole payment window.
LOST_ANSWER_WINDOW_SECONDS = 180


def train_label(train) -> str:
    """
    A current-API train or a korail2 reservation as people read it, on one
    line: ``KTX 015 서울 → 부산 · 9월 24일(목) 13:18→15:59``.

    Neither repr is display text, and a reservation's str also carries the
    price and payment deadline, which the app already shows on their own.
    """

    def pick(*names: str) -> str:
        return next((str(v).strip() for n in names if (v := getattr(train, n, None))), "")

    def clock(value: str) -> str:
        return f"{value[:2]}:{value[2:4]}" if len(value) >= 4 else ""

    name = pick("train_class_name", "train_type_name") or "KTX"
    # korail2 keeps the station names in dep_name/arr_name; the current API spells them out.
    src = pick("departure_station_name", "dep_name") or "출발역"
    dst = pick("arrival_station_name", "arr_name") or "도착역"
    label = f"{name} {pick('train_no')} {src} → {dst}".replace("  ", " ")

    date = pick("departure_date", "dep_date")
    try:
        day = datetime.strptime(date, "%Y%m%d")
        when = f"{day.month}월 {day.day}일({'월화수목금토일'[day.weekday()]})"
    except ValueError:
        when = ""
    dep = clock(pick("departure_time", "dep_time"))
    arr = clock(pick("arrival_time", "arr_time"))
    detail = " ".join(filter(None, [when, f"{dep}→{arr}" if dep and arr else dep]))
    return f"{label} · {detail}" if detail else label


@dataclass(frozen=True)
class DesignatedCapture:
    """One newly secured Korail hold and the physical seats it contains."""

    hold: object
    train: object
    seat_class: str
    targets: tuple[SeatTarget, ...]


@dataclass(frozen=True)
class LostAnswer:
    """A reserve request of this watch whose answer never arrived."""

    train: object
    seat_class: str
    passenger_count: int
    targets: tuple[SeatTarget, ...]
    until: float

    def same_request(self, other: LostAnswer) -> bool:
        return same_service(self.train, other.train) and (
            self.seat_class,
            self.passenger_count,
            self.targets,
        ) == (other.seat_class, other.passenger_count, other.targets)


def same_service(a, b) -> bool:
    """
    Same train number on the same running date.

    A current-API train keeps the running date in run_date; a korail2
    reservation in dep_date (its h_run_dt).
    """

    def key(train) -> tuple[str, str]:
        run = getattr(train, "dep_date", None) if hasattr(train, "rsv_id") else None
        return (
            str(getattr(train, "train_no", "") or ""),
            str(run or getattr(train, "run_date", "") or ""),
        )

    return key(a) == key(b)


class CancellationWaitService:
    """Find cancellations without ever widening the approved seat range."""

    def __init__(
        self, rail, plan: CancellationWaitPlan, search_kwargs: dict, on_duplicate=None
    ) -> None:
        self.rail = rail
        self.plan = plan
        self.search_kwargs = search_kwargs
        # Called with a train Korail refused because this account already
        # holds it, when the hold cannot be one this watch made.
        self.on_duplicate = on_duplicate or (lambda train: None)
        # Reservation numbers that are never this watch's: what the account
        # held when the watch began, then every hold the watch handed back.
        # None until Korail has listed them once.
        self._known: set[str] | None = None
        # Requests whose answer was lost, one per request however often it
        # was lost. Korail may have made the hold anyway, so the unpaid list
        # is read each pass until it shows up or the window is over.
        self._lost: list[LostAnswer] = []
        # Seats of unpaid reservations already read: an unpaid hold's seats do
        # not change, so each is read once per watch.
        self._seats: dict[str, frozenset] = {}
        self._mismatch_logged: set[str] = set()

    def poll_once(
        self, excluded: set[tuple[str, str, int, str]] | None = None
    ) -> DesignatedCapture | None:
        """Run one read pass and reserve at most one matching hold."""
        if self._known is None:
            listed = self.rail.unpaid_reservations()
            if listed is not None:
                # A hold from a lost answer can be on a list first read only
                # now: it stays claimable rather than being filed as old.
                self._known = {
                    rsv_id
                    for item in listed
                    if (rsv_id := self.rail.reservation_id(item))
                    and not any(same_service(item, lost.train) for lost in self._lost)
                }
        if self._lost and (capture := self._recover_lost()) is not None:
            return self._taken(capture)
        passenger_count = 1 if self.plan.strategy == "independent" else self.plan.passenger_count
        trains = self.rail.search_selectable_trains(
            **self.search_kwargs,
            passenger_count=passenger_count,
            wanted=[item.train_no for item in self.plan.trains],
        )
        trains_by_number = {str(getattr(train, "train_no", "") or ""): train for train in trains}
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
                if wanted.any_seat:
                    capture = self._any_seat_capture(train, wanted, passenger_count)
                elif consecutive:
                    capture = self._consecutive_capture(
                        train, wanted, blocks_by_train.get(wanted, ())
                    )
                else:
                    capture = self._independent_capture(train, wanted, excluded or set())
            except Exception as exc:
                if (
                    isinstance(exc, (KorailTransportError, KorailProtocolError))
                    and (capture := self._recover_lost()) is not None
                ):
                    return self._taken(capture)
                # One train's outage is not the pass's outage: the next train
                # may well be the one holding the seat this user is waiting for.
                failed += 1
                failure = exc
                logger.warning(
                    "취소표 열차 조회 실패: train_no=%s (%s)", wanted.train_no, type(exc).__name__
                )
                continue
            if capture is not None:
                return self._taken(capture)
        if failure is not None and failed == attempted:
            # Nothing was read at all, so this is a failed pass and the caller's
            # backoff has to see it.
            raise failure
        return None

    def _taken(self, capture: DesignatedCapture) -> DesignatedCapture:
        if self._known is not None:
            self._known.add(self.rail.reservation_id(capture.hold))
        return capture

    def _reserve(self, reserve, train, *args, targets, passenger_count: int, seat_class: str):
        """
        One reserve call. Remembers a lost answer, and tells on_duplicate
        about a refusal that a lost answer does not explain.
        """
        try:
            hold = reserve(train, *args, passenger_count=passenger_count, seat_class=seat_class)
        except (KorailTransportError, KorailProtocolError):
            answer = LostAnswer(
                train,
                seat_class,
                passenger_count,
                tuple(targets),
                time.monotonic() + LOST_ANSWER_WINDOW_SECONDS,
            )
            # The same request lost again only moves its window on.
            self._lost = [lost for lost in self._lost if not lost.same_request(answer)]
            self._lost.append(answer)
            raise
        except KorailAppError as exc:
            if exc.code != DUPLICATE_RESERVATION_CODE:
                raise
            hold = "DUPLICATE"
        if hold == "DUPLICATE":
            # The account already holds this train. After a lost answer on it
            # that is most likely this watch's own hold, which the unpaid list
            # will show; "cancel your booking" would be the wrong advice.
            if not any(same_service(lost.train, train) for lost in self._lost):
                self.on_duplicate(train)
            return None
        return hold or None

    def _recover_lost(self) -> DesignatedCapture | None:
        """
        A hold a lost answer made, read back from the unpaid list.

        Taken only when it is provably this watch's: a reservation number the
        watch did not start with, on the same train and running date, for as
        many seats as were asked for, and in exactly the seats asked for or,
        for a whole-train request, in the class asked for. Anything less could
        be the user's own booking from the Korail app, the seat map or another
        search, and is left alone.
        """
        now = time.monotonic()
        self._lost = [lost for lost in self._lost if lost.until > now]
        if not self._lost or self._known is None:
            return None
        for item in self.rail.unpaid_reservations() or []:
            rsv_id = self.rail.reservation_id(item)
            if not rsv_id or rsv_id in self._known:
                continue
            for lost in self._lost:
                if not same_service(item, lost.train) or (
                    getattr(item, "seat_no_count", None) != lost.passenger_count
                ):
                    continue
                seats = self._seats.get(rsv_id)
                if seats is None:
                    seats = self.rail.reserved_seats(item)
                    if seats is None:
                        continue
                    self._seats[rsv_id] = seats
                if lost.targets:
                    matches = {(car, label) for car, label, _ in seats} == {
                        (target.car_no, target.label) for target in lost.targets
                    }
                else:
                    matches = {seat_class for _, _, seat_class in seats} == {lost.seat_class}
                if not matches:
                    if rsv_id not in self._mismatch_logged:
                        # Korail's own seat wording, to check the format against.
                        self._mismatch_logged.add(rsv_id)
                        logger.warning(
                            "응답을 잃은 열차의 새 예약이 요청한 좌석과 달라 받지 않습니다: "
                            "train_no=%s seats=%s",
                            lost.train.train_no,
                            sorted(seats),
                        )
                    continue
                self._lost = [other for other in self._lost if not other.same_request(lost)]
                logger.info(
                    "응답을 잃은 예약을 미결제 목록에서 찾았습니다: train_no=%s",
                    lost.train.train_no,
                )
                return DesignatedCapture(item, lost.train, lost.seat_class, lost.targets)
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
        return {car.car_no for car in cars.cars if car.remaining_seat_count > 0} & candidates

    def _any_seat_capture(self, train, wanted, passenger_count: int) -> DesignatedCapture | None:
        """
        A train chosen without seats: let the operator seat the party, the way
        the plain search does, but only in the class the card was chosen for.
        """
        classes = ("general", "special") if wanted.seat_class == "any" else (wanted.seat_class,)
        # "any" books general first, as the plain search does, from the codes
        # this search already read: no request for a class that is not open.
        seat_class = next(
            (
                name
                for name in classes
                if getattr(train, f"{name}_reservation_code", None) == AVAILABLE_RESERVATION_CODE
            ),
            None,
        )
        if seat_class is None:
            return None
        hold = self._reserve(
            self.rail.reserve_any_seat,
            train,
            targets=(),
            passenger_count=passenger_count,
            seat_class=seat_class,
        )
        if hold is None:
            return None
        return DesignatedCapture(hold, train, seat_class, ())

    def _independent_capture(self, train, wanted, excluded) -> DesignatedCapture | None:
        by_car = self._by_car(wanted.targets)
        open_cars = self._open_cars(train, wanted, 1, set(by_car))
        for car_no, candidates in by_car.items():
            if car_no not in open_cars:
                continue
            inventory = self.rail.seat_inventory(train, car_no, wanted.seat_class, 1)
            sellable = self._sellable(inventory)
            target = next(
                (
                    item
                    for item in candidates
                    if (
                        (item.seat_no, item.label) in sellable
                        and (wanted.train_no, wanted.seat_class, item.car_no, item.seat_no)
                        not in excluded
                    )
                ),
                None,
            )
            if target is None:
                continue
            hold = self._reserve(
                self.rail.reserve_designated,
                train,
                inventory,
                [target],
                targets=(target,),
                passenger_count=1,
                seat_class=wanted.seat_class,
            )
            if hold is None:
                return None
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
            hold = self._reserve(
                self.rail.reserve_designated,
                train,
                inventory,
                list(block),
                targets=block,
                passenger_count=self.plan.passenger_count,
                seat_class=wanted.seat_class,
            )
            if hold is None:
                return None
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
