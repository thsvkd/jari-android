"""Structured seat candidates for a cancellation-ticket wait."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_PLAN_TRAINS = 30
# A trust boundary, not a budget for the user: the longest real formation is
# under 1000 seats, so "every seat in the train" has to fit comfortably. What
# actually bounds a plan's size is MAX_REQUEST_BYTES in mobile/config.py,
# read both by the HTTP body limit and by MobileSubmission.MAX_DATA_BYTES.
MAX_TARGETS_PER_TRAIN = 2000
MAX_TEXT_LENGTH = 64
SEAT_CLASSES = ("general", "special")
SEAT_STRATEGIES = ("independent", "consecutive")


class SeatPlanError(ValueError):
    """A seat plan cannot be trusted or cannot produce the requested booking."""


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SeatPlanError(f"{label} 정보를 확인할 수 없어요.")
    return value


def _small_text(value: object, label: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise SeatPlanError(f"{label} 정보를 확인할 수 없어요.")
    text = value.strip()
    if (required and not text) or len(text) > MAX_TEXT_LENGTH:
        raise SeatPlanError(f"{label} 정보를 확인할 수 없어요.")
    return text


def _bounded_int(value: object, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SeatPlanError(f"{label} 정보를 확인할 수 없어요.")
    return value


def _contiguous_blocks(
    targets: list[SeatTarget],
    position_of: Callable[[SeatTarget], int],
    wanted: int,
) -> list[tuple[SeatTarget, ...]]:
    """Every run of ``wanted`` seats whose positions follow one another."""

    ordered = sorted(targets, key=position_of)
    blocks: list[tuple[SeatTarget, ...]] = []
    for index in range(len(ordered) - wanted + 1):
        block = ordered[index : index + wanted]
        positions = [position_of(target) for target in block]
        if positions == list(range(positions[0], positions[0] + wanted)):
            blocks.append(tuple(block))
    return blocks


@dataclass(frozen=True)
class SeatTarget:
    """One physical seat the user is willing to take."""

    car_no: int
    seat_no: str
    label: str
    row: int | None = None
    column: str = ""
    direction: str = ""
    floor: str = ""
    adjacency_group: str = ""
    position: int = 0
    row_position: int = 0

    @classmethod
    def from_payload(cls, raw: object) -> SeatTarget:
        item = _mapping(raw, "좌석")
        row_raw = item.get("row")
        row = None if row_raw is None else _bounded_int(row_raw, "좌석 행", 1, 999)
        return cls(
            car_no=_bounded_int(item.get("carNo"), "호차", 1, 99),
            seat_no=_small_text(item.get("seatNo"), "좌석 식별자"),
            label=_small_text(item.get("label"), "좌석 번호"),
            row=row,
            column=_small_text(item.get("column", ""), "좌석 열", required=False).upper(),
            direction=_small_text(item.get("direction", ""), "좌석 방향", required=False),
            floor=_small_text(item.get("floor", ""), "좌석 층", required=False),
            adjacency_group=_small_text(
                item.get("adjacencyGroup", ""), "좌석 인접 그룹", required=False
            ),
            position=_bounded_int(item.get("position", 0), "좌석 위치", 0, 99),
            row_position=_bounded_int(item.get("rowPosition", 0), "좌석 줄 위치", 0, 99),
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "carNo": self.car_no,
            "seatNo": self.seat_no,
            "label": self.label,
            "row": self.row,
            "column": self.column,
            "direction": self.direction,
            "floor": self.floor,
            "adjacencyGroup": self.adjacency_group,
            "position": self.position,
            "rowPosition": self.row_position,
        }


@dataclass(frozen=True)
class TrainSeatTargets:
    """Acceptable seats in one room class of one train."""

    train_no: str
    seat_class: str
    targets: tuple[SeatTarget, ...]

    @classmethod
    def from_payload(cls, raw: object) -> TrainSeatTargets:
        item = _mapping(raw, "열차 좌석")
        train_no = _small_text(item.get("trainNo"), "열차 번호")
        if not train_no.isdigit() or len(train_no) > 5:
            raise SeatPlanError("열차 번호를 확인할 수 없어요.")
        seat_class = _small_text(item.get("seatClass"), "좌석 등급")
        if seat_class not in SEAT_CLASSES:
            raise SeatPlanError("좌석 등급을 확인할 수 없어요.")
        raw_targets = item.get("targets")
        if not isinstance(raw_targets, list) or not raw_targets:
            raise SeatPlanError("예약할 좌석 후보를 한 자리 이상 골라 주세요.")
        if len(raw_targets) > MAX_TARGETS_PER_TRAIN:
            raise SeatPlanError(f"한 열차의 좌석 후보는 {MAX_TARGETS_PER_TRAIN}개까지 고를 수 있어요.")
        targets = tuple(SeatTarget.from_payload(target) for target in raw_targets)
        keys = [(target.car_no, target.seat_no) for target in targets]
        if len(set(keys)) != len(keys):
            raise SeatPlanError("중복된 좌석 후보가 있어요.")
        return cls(train_no=train_no, seat_class=seat_class, targets=targets)

    def as_payload(self) -> dict[str, object]:
        return {
            "trainNo": self.train_no,
            "seatClass": self.seat_class,
            "targets": [target.as_payload() for target in self.targets],
        }


@dataclass(frozen=True)
class CancellationWaitPlan:
    """Every seat a cancellation-ticket search is allowed to reserve."""

    strategy: str
    passenger_count: int
    trains: tuple[TrainSeatTargets, ...]

    @classmethod
    def from_payload(cls, raw: object) -> CancellationWaitPlan:
        payload = _mapping(raw, "좌석 계획")
        strategy = _small_text(payload.get("strategy"), "좌석 배치")
        if strategy not in SEAT_STRATEGIES:
            raise SeatPlanError("좌석 배치 방식을 확인할 수 없어요.")
        passenger_count = _bounded_int(payload.get("passengerCount"), "승객 수", 1, 9)
        raw_trains = payload.get("trains")
        if not isinstance(raw_trains, list) or not raw_trains:
            raise SeatPlanError("취소표 대기 열차를 한 편 이상 골라 주세요.")
        if len(raw_trains) > MAX_PLAN_TRAINS:
            raise SeatPlanError(f"열차는 {MAX_PLAN_TRAINS}편까지 고를 수 있어요.")
        trains = tuple(TrainSeatTargets.from_payload(train) for train in raw_trains)
        keys = [(train.train_no, train.seat_class) for train in trains]
        if len(set(keys)) != len(keys):
            raise SeatPlanError("같은 열차와 좌석 등급이 중복됐어요.")
        plan = cls(strategy=strategy, passenger_count=passenger_count, trains=trains)
        if strategy == "consecutive" and not plan.consecutive_groups():
            if passenger_count >= 3:
                raise SeatPlanError("선택한 좌석 안에 인원수만큼 같은 줄에 나란히 붙은 좌석이 없어요.")
            raise SeatPlanError("선택한 좌석 안에 인원수만큼 붙어 있는 좌석이 없어요.")
        return plan

    def as_payload(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "passengerCount": self.passenger_count,
            "trains": [train.as_payload() for train in self.trains],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_payload(), ensure_ascii=False, separators=(",", ":"))

    def consecutive_groups(
        self,
    ) -> tuple[tuple[TrainSeatTargets, tuple[SeatTarget, ...]], ...]:
        """
        Candidate blocks of seats sitting next to each other, each with the
        train it was chosen from.

        The train comes back with the block because a seat cannot identify it:
        the same physical seat of the same car may be chosen on two different
        trains, and matching by value alone would book the wrong one.
        """

        groups: list[tuple[TrainSeatTargets, tuple[SeatTarget, ...]]] = []
        wanted = self.passenger_count
        for train in self.trains:
            by_group: dict[tuple[int, str], list[SeatTarget]] = {}
            for target in train.targets:
                if not target.adjacency_group:
                    continue
                by_group.setdefault((target.car_no, target.adjacency_group), []).append(target)
            for targets in by_group.values():
                for block in _contiguous_blocks(targets, lambda t: t.position, wanted):
                    groups.append((train, block))
            if wanted < 3:
                continue
            # A KTX row seats two and two, so three people can only sit
            # together by taking the aisle: the whole row, not one side.
            by_row: dict[tuple[int, int], list[SeatTarget]] = {}
            for target in train.targets:
                if target.row is None or target.row_position <= 0:
                    continue
                by_row.setdefault((target.car_no, target.row), []).append(target)
            for targets in by_row.values():
                for block in _contiguous_blocks(targets, lambda t: t.row_position, wanted):
                    groups.append((train, block))
        return tuple(groups)


def parse_seat_plan(text: str | None) -> CancellationWaitPlan | None:
    """Read a stored plan while keeping records from older versions valid."""

    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SeatPlanError("좌석 계획을 읽을 수 없어요.") from exc
    try:
        return CancellationWaitPlan.from_payload(payload)
    except SeatPlanError as exc:
        raise SeatPlanError(f"좌석 계획을 읽을 수 없어요. {exc}") from exc
