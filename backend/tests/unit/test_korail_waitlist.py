from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from korail2 import TrainType
from korail_mobile_api.errors import KorailNoResultsError

from korail_bot.services.korail_service import KorailService


def one_page(trains):
    """A search answer with no page behind it."""
    return SimpleNamespace(trains=trains, next_page=lambda: None)


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
    service._modern_client.search_trains.return_value = one_page((train,))

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
    service._modern_client.search_trains.return_value = one_page((train,))
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
    service._modern_client.search_trains.return_value = one_page((train,))

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
    service._modern_client.search_trains.return_value = one_page((train,))
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
            "h_seat_att_cd": 15,
        }
    )
    with pytest.raises(KorailProtocolError):
        validate_seat_inventory_inputs(raw, 1)
    service._modern_client.search_trains.return_value = one_page((raw,))

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
    assert (train.arrival_time, train.seat_attribute_code) == ("084300", "015")


def test_a_coupled_units_alphanumeric_class_code_passes_both_form_checks():
    from korail_mobile_api import TrainSummary
    from korail_mobile_api.mutation_payloads import _journey_fields
    from korail_mobile_api.payloads import validate_seat_inventory_inputs

    # What pit5 logged for 서울→부산 19:35 on 2026-09-22: the second unit of
    # the coupled KTX-산천 (9069 beside 069) carries h_trn_clsf_cd "0A".
    train = TrainSummary.from_raw(
        {
            "h_trn_no": "9069",
            "h_trn_gp_cd": "100",
            "h_trn_clsf_cd": "0A",
            "h_dpt_rs_stn_cd": "0001",
            "h_arv_rs_stn_cd": "0020",
            "h_dpt_dt": "20260922",
            "h_run_dt": "20260922",
            "h_dpt_tm": "193500",
            "h_arv_tm": "221000",
            "h_dpt_stn_run_ordr": "000001",
            "h_arv_stn_run_ordr": "000007",
            "h_seat_att_cd": "015",
            "h_dpt_stn_cons_ordr": "000001",
            "h_arv_stn_cons_ordr": "000007",
        }
    )

    validate_seat_inventory_inputs(train, 1)
    assert _journey_fields(train)["train_class_code"] == "0A"
    with pytest.raises(Exception, match="train_class_code"):
        validate_seat_inventory_inputs(replace(train, train_class_code="0a"), 1)


def row(number, clock):
    return SimpleNamespace(
        train_no=number,
        train_group_name="KTX",
        train_class_name="KTX",
        departure_time=clock,
        arrival_time="235900",
        general_reservation_code="11",
        special_reservation_code="13",
        wait_reservation_flag="-1",
    )


def pages(*answers):
    """Search answers in order; each is its rows plus whether Korail has another page."""
    sent = []
    for index, rows in enumerate(answers):
        more = index + 1 < len(answers)
        sent.append(
            SimpleNamespace(
                trains=rows, next_page=(lambda i=index: f"cursor{i}") if more else (lambda: None)
            )
        )
    return sent


def listing(service, **extra):
    return service.search_waitlist_trains(
        dep_date="20261013",
        src_locate="서울",
        dst_locate="부산",
        dep_time="000000",
        max_dep_time="2400",
        train_type=TrainType.KTX,
        passenger_count=1,
        **extra,
    )


def test_a_search_follows_korails_pages_through_the_window():
    service = service_with_modern_client()
    first = [row(f"{n:03d}", f"{6 + n:02d}0000") for n in range(10)]
    second = [row(f"{n:03d}", f"{16 + n:02d}0000") for n in range(10, 15)]
    service._modern_client.search_trains.side_effect = pages(first, second)

    trains = listing(service)

    assert [train.train_no for train in trains] == [t.train_no for t in first + second]
    calls = service._modern_client.search_trains.call_args_list
    assert [call.kwargs["continuation"] for call in calls] == [None, "cursor0"]
    assert service.search_cut_short is False


def test_a_search_stops_once_the_wanted_trains_are_in_hand():
    service = service_with_modern_client()
    first = [row("001", "060000"), row("003", "070000")]
    service._modern_client.search_trains.side_effect = pages(first, [row("005", "080000")])

    trains = listing(service, wanted=["003"])

    assert [train.train_no for train in trains] == ["001", "003"]
    assert service._modern_client.search_trains.call_count == 1


def test_a_search_stops_when_a_page_reaches_the_end_of_the_window():
    service = service_with_modern_client()
    service._modern_client.search_trains.side_effect = pages(
        [row("001", "060000"), row("003", "150000")], [row("005", "160000")]
    )

    trains = service.search_waitlist_trains(
        dep_date="20261013",
        src_locate="서울",
        dst_locate="부산",
        dep_time="000000",
        max_dep_time="1500",
        train_type=TrainType.KTX,
        passenger_count=1,
    )

    assert [train.train_no for train in trains] == ["001"]
    assert service._modern_client.search_trains.call_count == 1


def test_a_search_says_when_the_page_limit_cut_it_short():
    service = service_with_modern_client()
    service._modern_client.search_trains.side_effect = pages(
        *([[row(f"{n:03d}", f"{n:02d}0000")] for n in range(8)])
    )

    listing(service)

    assert service._modern_client.search_trains.call_count == 5
    assert service.search_cut_short is True


def test_a_window_with_no_train_is_an_empty_list_not_a_failure():
    service = service_with_modern_client()
    service._modern_client.search_trains.side_effect = KorailNoResultsError("ERI000", "없음")

    assert listing(service) == []
    assert service.search_cut_short is False


def numeric_row(number, minute_of_day):
    """A row as Korail sends it when the numbers arrive as JSON numbers: no leading zeros."""
    from korail_mobile_api import TrainSummary

    hour, minute = divmod(minute_of_day, 60)
    return TrainSummary.from_raw(
        {
            "h_trn_no": number,
            "h_trn_gp_cd": "100",
            "h_dpt_rs_stn_cd": 1,
            "h_arv_rs_stn_cd": 20,
            "h_dpt_dt": "20261013",
            "h_run_dt": "20261013",
            "h_dpt_tm": hour * 10000 + minute * 100,
            "h_arv_tm": "235900",
            "h_trn_clsf_cd": 7,
        }
    )


def test_pages_are_read_after_the_leading_zeros_are_restored():
    # 07:35 arrives as 73500. Read raw, that is "73:50", past a 12:00 window, and the day stopped at page one.
    service = service_with_modern_client()
    first = [numeric_row(n, 6 * 60 + 5 * n) for n in range(1, 11)]
    second = [numeric_row(n, 9 * 60 + 5 * n) for n in range(11, 14)]
    service._modern_client.search_trains.side_effect = pages(first, second)

    trains = service.search_waitlist_trains(
        dep_date="20261013",
        src_locate="서울",
        dst_locate="부산",
        dep_time="060000",
        max_dep_time="1200",
        train_type=TrainType.ALL,
        passenger_count=1,
    )

    assert len(trains) == 13
    assert service._modern_client.search_trains.call_count == 2


def test_a_watch_starts_at_the_page_its_trains_are_on_once_it_has_seen_them(monkeypatch):
    service = service_with_modern_client()
    monkeypatch.setattr(service, "wait_between_requests", lambda *_: 0)
    first = [row(f"{n:03d}", f"{6 + n:02d}0000") for n in range(10)]
    second = [row("015", "170000"), row("017", "180000")]
    service._modern_client.search_trains.side_effect = pages(first, second)

    listing(service, wanted=["015"])
    assert service._modern_client.search_trains.call_count == 2

    service._modern_client.search_trains.reset_mock()
    service._modern_client.search_trains.side_effect = pages(second)
    listing(service, wanted=["015"])

    (call,) = service._modern_client.search_trains.call_args_list
    assert call.args[0].departure_time == "170000"


def test_a_vanished_watched_train_does_not_walk_the_day_every_pass(monkeypatch):
    service = service_with_modern_client()
    monkeypatch.setattr(service, "wait_between_requests", lambda *_: 0)
    service._wanted_departures[("20261013", "서울", "부산")] = {"015": "170000"}
    gone = [row(f"{n:03d}", f"{17 + n // 10:02d}{n % 10 * 5:02d}00") for n in range(10)]
    service._modern_client.search_trains.side_effect = pages(gone, [row("099", "230000")])

    trains = listing(service, wanted=["015"])

    assert len(trains) == 10
    assert service._modern_client.search_trains.call_count == 1


def test_a_waitlist_request_finds_a_train_on_a_later_page(monkeypatch):
    service = service_with_modern_client()
    monkeypatch.setattr(service, "wait_between_requests", lambda *_: 0)
    first = [row(f"{n:03d}", f"{6 + n:02d}0000") for n in range(10)]
    target = row("015", "170000")
    target.wait_reservation_flag = " 9"
    service._modern_client.search_trains.side_effect = pages(first, [target])
    service._modern_client.reserve.return_value = SimpleNamespace(pnr_no="PRIVATE")

    result = service.request_waitlist(
        dep_date="20261013",
        src_locate="서울",
        dst_locate="부산",
        dep_time="000000",
        train_no="015",
    )

    assert result["train_no"] == "015"
    assert service._modern_client.search_trains.call_count == 2
