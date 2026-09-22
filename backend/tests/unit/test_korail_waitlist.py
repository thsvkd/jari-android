from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from korail2 import TrainType

from korail_bot.services.korail_service import KorailService


def service_with_modern_client():
    service = KorailService()
    service._logged_in = True
    service._modern_client = MagicMock()
    return service


def test_waitlist_listing_uses_the_same_modern_api_as_submission():
    service = service_with_modern_client()
    service._korail_instance = MagicMock()
    train = SimpleNamespace(
        train_no="005",
        train_group_name="KTX",
        train_class_name="KTX",
        departure_time="055800",
        arrival_time="084300",
        general_reservation_code="13",
        special_reservation_code="13",
        wait_reservation_flag=" 9",
    )
    service._modern_client.search_trains.return_value = SimpleNamespace(trains=(train,))

    trains = service.search_waitlist_trains(
        dep_date="20260919",
        src_locate="서울",
        dst_locate="부산",
        dep_time="055800",
        max_dep_time="0559",
        train_type=TrainType.KTX,
        passenger_count=1,
    )

    assert trains == [train]
    service._korail_instance.search_train.assert_not_called()
    assert service.describe_waitlist_train(train) == {
        "no": "005",
        "label": "05:58→08:43 KTX",
        "dep_time": "055800",
        "arr_time": "084300",
        "name": "KTX",
        "soldout": True,
        "waitlistEligible": True,
    }


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


def test_search_restores_leading_zeros_korail_drops_from_numeric_fields():
    from korail_mobile_api import TrainSummary
    from korail_mobile_api.errors import KorailProtocolError
    from korail_mobile_api.payloads import validate_seat_inventory_inputs

    service = service_with_modern_client()
    # 서울(0001)→부산(0020), KTX-산천(07): the codes came as JSON numbers, so
    # their leading zeros were gone before the row was parsed.
    raw = TrainSummary.from_raw(
        {
            "h_trn_no": 69,
            "h_trn_gp_cd": "100",
            "h_dpt_rs_stn_cd": 1,
            "h_arv_rs_stn_cd": 20,
            "h_dpt_dt": "20260922",
            "h_run_dt": "20260922",
            "h_dpt_tm": 63000,
            "h_arv_tm": "084300",
            "h_trn_clsf_cd": 7,
            "h_dpt_stn_run_ordr": 1,
            "h_arv_stn_run_ordr": 14,
        }
    )
    with pytest.raises(KorailProtocolError):
        validate_seat_inventory_inputs(raw, 1)
    service._modern_client.search_trains.return_value = SimpleNamespace(trains=(raw,))

    (train,) = service.search_waitlist_trains(
        dep_date="20260922",
        src_locate="0001",
        dst_locate="0020",
        dep_time="060000",
        max_dep_time="0700",
        train_type=TrainType.ALL,
        passenger_count=1,
    )

    validate_seat_inventory_inputs(train, 1)
    assert (train.train_no, train.departure_station_code, train.arrival_station_code) == (
        "069",
        "0001",
        "0020",
    )
    assert (train.departure_time, train.train_class_code) == ("063000", "07")
    assert (train.departure_run_order, train.arrival_run_order) == ("000001", "000014")
    assert train.arrival_time == "084300"
