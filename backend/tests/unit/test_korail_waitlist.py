from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from korail_bot.services.korail_service import KorailService


def service_with_modern_client():
    service = KorailService()
    service._logged_in = True
    service._modern_client = MagicMock()
    return service


def test_waitlist_reserves_and_confirms_one_eligible_train():
    service = service_with_modern_client()
    train = SimpleNamespace(train_no="015", wait_reservation_flag=" 9")
    service._modern_client.search_trains.return_value = SimpleNamespace(trains=(train,))
    hold = SimpleNamespace(pnr_no="PRIVATE")
    service._modern_client.reserve.return_value = hold

    result = service.request_waitlist(
        dep_date="20260920",
        src_locate="서울",
        dst_locate="부산",
        dep_time="090000",
        train_no="015",
        passenger_count=2,
    )

    assert result["train_no"] == "015"
    reserve_kwargs = service._modern_client.reserve.call_args.kwargs
    assert reserve_kwargs["passengers"].adult == 2
    assert reserve_kwargs["job_type"].value == "1102"
    confirm_kwargs = service._modern_client.confirm_standby_hold.call_args.kwargs
    assert confirm_kwargs["sms_notify"] is False
    assert confirm_kwargs["phone_no"] is None


def test_waitlist_refuses_a_train_without_the_official_flag():
    service = service_with_modern_client()
    train = SimpleNamespace(train_no="019", wait_reservation_flag="0")
    service._modern_client.search_trains.return_value = SimpleNamespace(trains=(train,))

    with pytest.raises(ValueError, match="예약 대기 대상"):
        service.request_waitlist(
            dep_date="20260920",
            src_locate="서울",
            dst_locate="부산",
            dep_time="090000",
            train_no="019",
        )

    service._modern_client.reserve.assert_not_called()


def test_waitlist_cancels_the_hold_when_follow_up_fails():
    service = service_with_modern_client()
    train = SimpleNamespace(train_no="015", wait_reservation_flag=" 9")
    hold = SimpleNamespace(pnr_no="PRIVATE")
    service._modern_client.search_trains.return_value = SimpleNamespace(trains=(train,))
    service._modern_client.reserve.return_value = hold
    service._modern_client.confirm_standby_hold.side_effect = RuntimeError("follow-up failed")

    with pytest.raises(RuntimeError, match="follow-up failed"):
        service.request_waitlist(
            dep_date="20260920",
            src_locate="서울",
            dst_locate="부산",
            dep_time="090000",
            train_no="015",
        )

    service._modern_client.cancel_unpaid_hold.assert_called_once()
