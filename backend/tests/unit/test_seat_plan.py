import io
import json
import threading
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from korail_bot.models import (
    CancellationWaitPlan,
    SeatPlanError,
    TrainSearchParams,
    parse_seat_plan,
)
from korail_bot.services import reservation_service
from korail_bot.services.mini_app_service import MiniAppSubmission
from korail_bot.services.reservation_service import ReservationService
from korail_bot.storage.redis import RedisStorage
from korail_bot.telegramBot.telebotBackProcess import BackgroundReservationProcess
from korail_bot.utils.timezone import utc_now


def target(
    seat_no: str,
    label: str,
    *,
    car_no: int = 3,
    group: str = "5-left",
    position: int = 1,
    row: int = 5,
    row_position: int | None = None,
) -> dict:
    payload = {
        "carNo": car_no,
        "seatNo": seat_no,
        "label": label,
        "row": row,
        "column": label[-1],
        "direction": "forward",
        "adjacencyGroup": group,
        "position": position,
    }
    if row_position is not None:
        payload["rowPosition"] = row_position
    return payload


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


def test_a_train_taken_whole_needs_no_seats_and_satisfies_a_consecutive_plan():
    plan = CancellationWaitPlan.from_payload(
        {
            "strategy": "consecutive",
            "passengerCount": 2,
            "trains": [
                # One seat cannot hold two people, but the other train can seat them anywhere.
                {"trainNo": "015", "seatClass": "general", "targets": [target("S1", "5A")]},
                {"trainNo": "019", "seatClass": "any", "targets": []},
            ],
        }
    )

    assert [train.any_seat for train in plan.trains] == [False, True]
    assert parse_seat_plan(plan.to_json()) == plan

    with pytest.raises(SeatPlanError, match="좌석 등급이 있어야"):
        CancellationWaitPlan.from_payload(
            {
                "strategy": "independent",
                "passengerCount": 1,
                "trains": [{"trainNo": "015", "seatClass": "any", "targets": [target("S1", "5A")]}],
            }
        )


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

    labels = [[seat.label for seat in block] for _, block in plan.consecutive_groups()]
    assert labels == [["5A", "5B"], ["5C", "5D"]]


def three_seat_plan(*targets: dict) -> CancellationWaitPlan:
    return CancellationWaitPlan.from_payload(
        {
            "strategy": "consecutive",
            "passengerCount": 3,
            "trains": [{"trainNo": "015", "seatClass": "general", "targets": list(targets)}],
        }
    )


def test_three_passengers_may_take_a_whole_row_across_the_aisle():
    # A KTX row seats two and two, so three together always crosses the aisle.
    plan = three_seat_plan(
        target("S1", "5A", group="5-left", position=1, row_position=1),
        target("S2", "5B", group="5-left", position=2, row_position=2),
        target("S3", "5C", group="5-right", position=1, row_position=3),
    )

    labels = [[seat.label for seat in block] for _, block in plan.consecutive_groups()]
    assert labels == [["5A", "5B", "5C"]]


def test_the_same_seats_on_two_trains_stay_with_their_own_train():
    # The seat is described identically on both, so a block only knows which
    # train it belongs to because the plan says so.
    seats = [
        target("S1", "5A", group="5-left", position=1),
        target("S2", "5B", group="5-left", position=2),
    ]
    plan = CancellationWaitPlan.from_payload(
        {
            "strategy": "consecutive",
            "passengerCount": 2,
            "trains": [
                {"trainNo": "015", "seatClass": "general", "targets": seats},
                {"trainNo": "017", "seatClass": "general", "targets": seats},
            ],
        }
    )

    assert [
        (train.train_no, [seat.label for seat in block])
        for train, block in plan.consecutive_groups()
    ] == [("015", ["5A", "5B"]), ("017", ["5A", "5B"])]


def test_three_passengers_cannot_be_split_over_two_rows():
    with pytest.raises(SeatPlanError, match="같은 줄"):
        three_seat_plan(
            target("S1", "5A", group="5-left", position=1, row_position=1),
            target("S2", "5B", group="5-left", position=2, row_position=2),
            target("S3", "6A", group="6-left", position=1, row=6, row_position=1),
        )


def test_two_passengers_still_may_not_sit_across_the_aisle():
    with pytest.raises(SeatPlanError, match="붙어 있는"):
        CancellationWaitPlan.from_payload(
            {
                "strategy": "consecutive",
                "passengerCount": 2,
                "trains": [
                    {
                        "trainNo": "015",
                        "seatClass": "general",
                        "targets": [
                            target("S2", "5B", group="5-left", position=2, row_position=2),
                            target("S3", "5C", group="5-right", position=1, row_position=3),
                        ],
                    }
                ],
            }
        )


def test_a_plan_stored_before_row_positions_existed_still_parses():
    plan = CancellationWaitPlan.from_payload(
        {
            "strategy": "independent",
            "passengerCount": 1,
            "trains": [{"trainNo": "015", "seatClass": "general", "targets": [target("S1", "5A")]}],
        }
    )

    assert plan.trains[0].targets[0].row_position == 0
    assert parse_seat_plan(plan.to_json()) == plan


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

    # A whole KTX formation is close to a thousand seats, and "apply to every
    # car" is a thing the app offers. That has to go through.
    whole_train = CancellationWaitPlan.from_payload(
        {
            "strategy": "independent",
            "passengerCount": 1,
            "trains": [
                {
                    "trainNo": "015",
                    "seatClass": "general",
                    "targets": [target(f"S{i}", f"{i + 1}A") for i in range(1000)],
                }
            ],
        }
    )
    assert len(whole_train.trains[0].targets) == 1000

    with pytest.raises(SeatPlanError, match="2000"):
        CancellationWaitPlan.from_payload(
            {
                "strategy": "independent",
                "passengerCount": 1,
                "trains": [
                    {
                        "trainNo": "015",
                        "seatClass": "general",
                        "targets": [target(f"S{i}", f"{i + 1}A") for i in range(2001)],
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


PLAN_JSON = CancellationWaitPlan.from_payload(
    {
        "strategy": "independent",
        "passengerCount": 1,
        "trains": [{"trainNo": "015", "seatClass": "general", "targets": [target("S1", "5A")]}],
    }
).to_json()


def started(service, monkeypatch, seat_plan_json):
    """Run one start against a fake child and return its argv and stdin line."""
    service._may_start = lambda chat_id: True
    service._under_concurrency_limit = lambda chat_id: True
    service._confirm_started = lambda *args: True
    proc = MagicMock(pid=4242)
    written: list[bytes] = []
    proc.stdin.write.side_effect = written.append
    command: list[str] = []

    def popen(cmd, **kwargs):
        command.extend(cmd)
        return proc

    monkeypatch.setattr(reservation_service.subprocess, "Popen", popen)

    assert service.start_reservation_process(
        chat_id=7,
        username="01012345678",
        password="rail password",
        search_params=TrainSearchParams(
            dep_date="20260920",
            src_locate="서울",
            dst_locate="부산",
            dep_time="090000",
            seat_plan_json=seat_plan_json,
        ),
    )

    # The handover now runs on its own thread so a full pipe cannot wedge the
    # server; wait for it rather than racing it.
    for thread in threading.enumerate():
        if thread.name.startswith(reservation_service._STDIN_THREAD_PREFIX):
            thread.join(timeout=5)
            assert not thread.is_alive()
    # How the child sees it: `python -m <module> *arguments` puts arguments[0]
    # at sys.argv[1].
    return ["worker", *command[3:]], written


def test_seat_plan_reaches_the_worker_on_stdin_without_shifting_argv(monkeypatch):
    # A plan covering whole formations is past MAX_ARG_STRLEN, so it rides the
    # line that already carries the credentials. The slot must keep its
    # position, or every argument before it changes meaning on resume.
    argv, written = started(ReservationService(MagicMock(), MagicMock()), monkeypatch, PLAN_JSON)

    assert argv[13] == "stdin"
    assert PLAN_JSON not in "".join(argv)
    handover = json.loads(b"".join(written).decode("utf-8"))
    assert handover["seat_plan"] == PLAN_JSON
    assert handover["username"] == "01012345678"


def test_a_search_without_a_plan_leaves_the_slot_empty(monkeypatch):
    argv, written = started(ReservationService(MagicMock(), MagicMock()), monkeypatch, "")

    assert argv[13] == ""
    assert json.loads(b"".join(written).decode("utf-8"))["seat_plan"] == ""


def test_mobile_runtime_tag_does_not_move_the_plan_slot(tmp_path, monkeypatch):
    from korail_bot.mobile.config import MobileConfig
    from korail_bot.mobile.process import MobileReservationService

    config = MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1")
    service = MobileReservationService(MagicMock(), MagicMock(), config)
    argv, _ = started(service, monkeypatch, PLAN_JSON)

    # The runtime tag goes on after the arguments, so the worker still reads
    # the plan slot at sys.argv[13].
    assert argv[13] == "stdin"
    assert argv[14] == service.tag


def test_worker_reads_the_credentials_line_with_or_without_a_plan(monkeypatch):
    read = BackgroundReservationProcess._read_credentials

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"username": "u", "password": "p", "seat_plan": PLAN_JSON}) + "\n"),
    )
    assert read() == ("u", "p", PLAN_JSON)

    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"username": "u", "password": "p"}) + "\n")
    )
    assert read() == ("u", "p", "")


@pytest.mark.parametrize(
    ("slot", "from_stdin", "expected"),
    [
        ("stdin", PLAN_JSON, PLAN_JSON),
        ("", "", None),
        (PLAN_JSON, "", PLAN_JSON),
    ],
)
def test_worker_takes_the_plan_from_where_the_slot_says_it_is(
    monkeypatch, slot, from_stdin, expected
):
    monkeypatch.setattr("sys.argv", ["worker", *[""] * 12, slot])

    plan = BackgroundReservationProcess._seat_plan_from(from_stdin)

    assert (plan.to_json() if plan else None) == expected


def test_worker_refuses_to_start_when_the_promised_plan_never_arrived(monkeypatch):
    # Fail closed. A search that lost its plan and carried on would book
    # whatever seat came free - a seat the user never picked.
    monkeypatch.setattr("sys.argv", ["worker", *[""] * 12, "stdin"])

    with pytest.raises(SeatPlanError):
        BackgroundReservationProcess._seat_plan_from("")


def test_an_older_worker_refuses_the_marker_rather_than_widening_the_search():
    # A worker built before the plan moved to stdin reads the marker as a plan.
    # It has to fail, not fall back to "any seat".
    with pytest.raises(SeatPlanError):
        parse_seat_plan("stdin")


def test_mobile_submission_preserves_validated_cancellation_wait_plan():
    conditions = {
        "v": 1,
        "action": "prepare_search",
        "dep_date": (utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
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
