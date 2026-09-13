import json

import pytest

from korail_bot.models import (
    CancellationWaitPlan,
    SeatPlanError,
    TrainSearchParams,
    parse_seat_plan,
)
from korail_bot.services.mini_app_service import MiniAppSubmission
from korail_bot.storage.redis import RedisStorage


def target(
    seat_no: str,
    label: str,
    *,
    car_no: int = 3,
    group: str = "5-left",
    position: int = 1,
) -> dict:
    return {
        "carNo": car_no,
        "seatNo": seat_no,
        "label": label,
        "row": 5,
        "column": label[-1],
        "direction": "forward",
        "adjacencyGroup": group,
        "position": position,
    }


def test_independent_plan_accepts_more_candidates_than_passengers():
    plan = CancellationWaitPlan.from_payload(
        {
            "strategy": "independent",
            "passengerCount": 2,
            "trains": [
                {
                    "trainNo": "015",
                    "seatClass": "general",
                    "targets": [
                        target("S1", "5A", position=1),
                        target("S2", "5B", position=2),
                        target("S3", "6A", group="6-left", position=1),
                    ],
                }
            ],
        }
    )

    assert plan.passenger_count == 2
    assert len(plan.trains[0].targets) == 3
    assert parse_seat_plan(plan.to_json()) == plan


def test_consecutive_groups_do_not_cross_an_aisle_car_or_class():
    plan = CancellationWaitPlan.from_payload(
        {
            "strategy": "consecutive",
            "passengerCount": 2,
            "trains": [
                {
                    "trainNo": "015",
                    "seatClass": "general",
                    "targets": [
                        target("A", "5A", group="5-left", position=1),
                        target("B", "5B", group="5-left", position=2),
                        target("C", "5C", group="5-right", position=1),
                        target("D", "5D", group="5-right", position=2),
                        target("E", "5E", car_no=4, group="5-left", position=1),
                    ],
                },
                {
                    "trainNo": "015",
                    "seatClass": "special",
                    "targets": [target("F", "5A", group="5-left", position=1)],
                },
            ],
        }
    )

    labels = [[seat.label for seat in group] for group in plan.consecutive_groups()]
    assert labels == [["5A", "5B"], ["5C", "5D"]]


@pytest.mark.parametrize(
    "change,message",
    [
        ({"strategy": "unknown"}, "좌석 배치"),
        ({"passengerCount": 0}, "승객"),
        ({"trains": []}, "열차"),
    ],
)
def test_invalid_plan_is_rejected(change, message):
    payload = {
        "strategy": "independent",
        "passengerCount": 1,
        "trains": [
            {
                "trainNo": "015",
                "seatClass": "general",
                "targets": [target("S1", "5A")],
            }
        ],
        **change,
    }

    with pytest.raises(SeatPlanError, match=message):
        CancellationWaitPlan.from_payload(payload)


def test_duplicate_and_oversized_targets_are_rejected():
    duplicate = target("S1", "5A")
    with pytest.raises(SeatPlanError, match="중복"):
        CancellationWaitPlan.from_payload(
            {
                "strategy": "independent",
                "passengerCount": 1,
                "trains": [
                    {
                        "trainNo": "015",
                        "seatClass": "general",
                        "targets": [duplicate, duplicate],
                    }
                ],
            }
        )

    with pytest.raises(SeatPlanError, match="200"):
        CancellationWaitPlan.from_payload(
            {
                "strategy": "independent",
                "passengerCount": 1,
                "trains": [
                    {
                        "trainNo": "015",
                        "seatClass": "general",
                        "targets": [target(f"S{i}", f"{i + 1}A") for i in range(201)],
                    }
                ],
            }
        )


def test_empty_or_legacy_seat_plan_is_backward_compatible():
    assert parse_seat_plan("") is None
    assert parse_seat_plan(None) is None
    with pytest.raises(SeatPlanError, match="좌석 계획"):
        parse_seat_plan(json.dumps({"strategy": "independent"}))


def test_search_params_keep_seat_plan_and_read_legacy_records():
    storage = object.__new__(RedisStorage)
    params = TrainSearchParams(
        dep_date="20260920",
        src_locate="서울",
        dst_locate="부산",
        dep_time="090000",
        seat_plan_json='{"strategy":"independent"}',
    )

    stored = storage._serialize_search_params(params)
    assert storage._deserialize_search_params(stored).seat_plan_json == params.seat_plan_json
    stored.pop("seat_plan_json")
    assert storage._deserialize_search_params(stored).seat_plan_json == ""


def test_mobile_submission_preserves_validated_cancellation_wait_plan():
    conditions = {
        "v": 1,
        "action": "prepare_search",
        "dep_date": "20260920",
        "src_station": "서울",
        "dst_station": "부산",
        "dep_time": "0900",
        "max_dep_time": "1800",
        "train_type": "1",
        "seat_option": "2",
        "passenger_count": 2,
        "seat_strategy": "2",
        "seat_classes": ["general"],
        "seat_plan": {
            "strategy": "independent",
            "passengerCount": 2,
            "trains": [
                {
                    "trainNo": "015",
                    "seatClass": "general",
                    "targets": [target("wire-1A", "1A", group="1:left")],
                }
            ],
        },
    }

    submission = MiniAppSubmission.parse(json.dumps(conditions, ensure_ascii=False))

    assert submission.seat_classes == ("general",)
    assert json.loads(submission.seat_plan_json)["trains"][0]["targets"][0]["label"] == "1A"
    assert submission.as_train_info()["seatPlan"] == submission.seat_plan_json
