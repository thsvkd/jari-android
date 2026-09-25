"""A second DUPLICATE in the same consecutive search must not end it
(backend-core#1).

reserve_train answering "DUPLICATE" used to raise DuplicateReservationError
once, which telebotBackProcess.run() caught, notified the user about, and
handled by calling search_and_reserve_loop a second time - resetting the
local duplicate_notified flag. The existing reservation and the seat were
both still there, so the very next DUPLICATE raised the same exception
uncaught, and the search ended as "알수 없는 오류로 예매에 실패했습니다"
seconds after promising to keep looking.

_search_and_reserve_random had the identical raise-then-swallow trap: nothing
in the current call graph catches DuplicateReservationError any more (the
handler that used to was removed with the consecutive-path fix above), so a
second DUPLICATE there would now propagate as an unhandled exception instead
of just ending the search cleanly with None.
"""

from types import SimpleNamespace
from unittest.mock import Mock

from korail_bot.services import rail_service as rail_service_module
from korail_bot.services.korail_service import KorailService


def test_a_second_duplicate_in_one_search_keeps_searching_instead_of_ending_it(
    monkeypatch,
):
    # note_search_failure()/wait_between_requests() must not sleep in a test.
    monkeypatch.setattr(rail_service_module.time, "sleep", lambda *_: None)
    announced: list[str] = []
    service = KorailService(on_status=announced.append)
    service._logged_in = True
    train = SimpleNamespace(train_no="101")
    reservation = SimpleNamespace(rsv_id="R1")
    service.search_trains = Mock(return_value=[train])
    service.reserve_train = Mock(side_effect=["DUPLICATE", "DUPLICATE", reservation])

    result = service.search_and_reserve_loop(
        dep_date="20260101",
        src_locate="서울",
        dst_locate="부산",
        passenger_count=1,
        seat_strategy="consecutive",
    )

    # Search survived both duplicates and returned the seat found afterwards,
    # in the one call - no exception, no second call from the caller.
    assert result is reservation
    assert service.reserve_train.call_count == 3
    # The user hears about the duplicate once, not once per attempt.
    assert len(announced) == 1
    assert "기존 예약" in announced[0]


def test_a_second_duplicate_in_one_random_search_keeps_searching_instead_of_raising(
    monkeypatch,
):
    monkeypatch.setattr(rail_service_module.time, "sleep", lambda *_: None)
    announced: list[str] = []
    service = KorailService(on_status=announced.append)
    service._logged_in = True
    train = SimpleNamespace(train_no="101")
    reservation = SimpleNamespace(rsv_id="R1")
    service.search_trains = Mock(return_value=[train])
    service.reserve_train = Mock(side_effect=["DUPLICATE", "DUPLICATE", reservation])

    result = service.search_and_reserve_loop(
        dep_date="20260101",
        src_locate="서울",
        dst_locate="부산",
        passenger_count=1,
        seat_strategy="random",
    )

    # No exception out of the loop, and the seat found after both duplicates
    # is returned in the one call.
    assert result is reservation
    assert service.reserve_train.call_count == 3
    assert len(announced) == 1
    assert "기존 예약" in announced[0]
