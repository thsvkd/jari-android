"""Seat windows: how long a freed seat stays free, read off the watch's own answers.

Built from the libraries' real train classes (korail2 Train, korail_mobile_api
TrainSummary): the two name the same facts differently, and a field-name slip
would only show against the real objects.
"""

from types import SimpleNamespace

import fakeredis
import pytest
from korail2 import NoResultsError, SoldOutError
from korail2.korail2 import Train
from korail_mobile_api.models import TrainSummary

from korail_bot.mobile.storage import MobileStorage
from korail_bot.services import rail_service as rail_service_module
from korail_bot.services.korail_service import KorailService
from korail_bot.services.rail_service import SearchUnavailableError
from korail_bot.services.seat_windows import SeatWindows, summarize


def _train(train_no="101", general="13", special="13"):
    return Train(
        {
            "h_trn_clsf_cd": "100",
            "h_trn_clsf_nm": "KTX",
            "h_trn_gp_cd": "100",
            "h_trn_no": train_no,
            "h_dpt_rs_stn_nm": "서울",
            "h_dpt_rs_stn_cd": "0001",
            "h_dpt_dt": "20261101",
            "h_dpt_tm": "060000",
            "h_arv_rs_stn_nm": "부산",
            "h_arv_rs_stn_cd": "0020",
            "h_arv_dt": "20261101",
            "h_arv_tm": "090000",
            "h_run_dt": "20261101",
            "h_rsv_psb_flg": "Y",
            "h_rsv_psb_nm": "예약가능",
            "h_spe_rsv_cd": special,
            "h_gen_rsv_cd": general,
            "h_wait_rsv_flg": "-1",
        }
    )


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _windows():
    events, clock = [], _Clock()
    return SeatWindows(events.append, clock=clock), events, clock


def _feed(windows, clock, at, *trains):
    clock.now = at
    windows.observe(list(trains), passengers=1)


def test_a_seat_that_opens_and_closes_between_passes_becomes_one_window():
    windows, events, clock = _windows()
    _feed(windows, clock, 0, _train(general="13"))
    _feed(windows, clock, 2, _train(general="11"))
    _feed(windows, clock, 4, _train(general="11"))
    _feed(windows, clock, 6, _train(general="13"))

    (event,) = events
    assert event == {
        "date": "20261101",
        "train": "101",
        "dep": "0001",
        "arr": "0020",
        "passengers": 1,
        "seat_class": "general",
        "kind": "window",
        "source": event["source"],
        "closed_before": 0,
        "closed_after": 6,
        "min_s": 2,
        "max_s": 6,
        "passes": 2,
        "lost": False,
        "ours": False,
    }


def test_a_seat_free_before_the_watch_started_has_no_known_length():
    windows, events, clock = _windows()
    _feed(windows, clock, 0, _train(general="11"))
    _feed(windows, clock, 2, _train(general="13"))

    assert events == []


def test_every_train_in_the_answer_is_measured_not_only_the_watched_one():
    windows, events, clock = _windows()
    _feed(windows, clock, 0, _train("101"), _train("103"))
    _feed(windows, clock, 1, _train("101"), _train("103", special="11"))
    _feed(windows, clock, 2, _train("101"), _train("103"))

    assert [(e["train"], e["seat_class"]) for e in events] == [("103", "special")]


def test_a_window_closed_by_this_search_is_marked_as_its_own():
    windows, events, clock = _windows()
    _feed(windows, clock, 0, _train())
    _feed(windows, clock, 1, _train(general="11", special="11"))
    clock.now = 1.2
    windows.took(_train())
    # The general seat it took is gone; special stayed free past the booking.
    _feed(windows, clock, 2, _train(special="11"))
    _feed(windows, clock, 3, _train())

    assert [(e["seat_class"], e["ours"]) for e in events] == [
        ("general", True),
        ("special", False),
    ]


def test_a_refusal_repeated_every_pass_marks_the_window_once():
    windows, events, clock = _windows()
    _feed(windows, clock, 0, _train())
    for at in (1, 2, 3):
        _feed(windows, clock, at, _train(general="11"))
        windows.lost(_train())
    _feed(windows, clock, 4, _train())

    assert [(e["kind"], e["lost"]) for e in events] == [("window", True)]


def test_the_current_api_train_is_read_the_same_way():
    windows, events, clock = _windows()

    def summary(code):
        return TrainSummary(
            train_no="015",
            departure_station_code="0001",
            arrival_station_code="0020",
            departure_date="20261101",
            general_reservation_code=code,
            special_reservation_code="13",
        )

    _feed(windows, clock, 0, summary("13"))
    _feed(windows, clock, 3, summary("11"))
    _feed(windows, clock, 5, summary("13"))

    # "015" and korail2's "15" are the same train.
    assert [(e["train"], e["min_s"], e["max_s"]) for e in events] == [("15", 0, 5)]


def test_a_failing_store_never_stops_the_search():
    def broken(_event):
        raise ConnectionError("redis down")

    clock = _Clock()
    windows = SeatWindows(broken, clock=clock)
    _feed(windows, clock, 0, _train())
    _feed(windows, clock, 1, _train(general="11"))
    _feed(windows, clock, 2, _train())  # would raise if the error escaped


class _Legacy:
    def __init__(self, trains=(), reserve_error=None):
        self._session = SimpleNamespace(get=lambda *a, **k: None)
        self.reserve_calls = 0
        self._trains = list(trains)
        self._reserve_error = reserve_error
        self.include_no_seats = None

    def search_train(self, *args, include_no_seats=False, **kwargs):
        self.include_no_seats = include_no_seats
        return list(self._trains)

    def reservations(self):
        return []

    def reserve(self, train, passengers=None, option=None):
        self.reserve_calls += 1
        raise self._reserve_error


def _service(monkeypatch, legacy):
    service = KorailService()
    service._logged_in = True
    service._korail_instance = legacy
    monkeypatch.setattr(rail_service_module.time, "sleep", lambda *_: None)
    events = []
    service.seat_windows = SeatWindows(events.append)
    return service, events


def test_search_sees_sold_out_trains_but_still_returns_only_bookable_ones(monkeypatch):
    legacy = _Legacy([_train("101", general="13"), _train("103", general="11")])
    service, _ = _service(monkeypatch, legacy)
    observed = []
    service.seat_windows.observe = lambda trains, passengers: observed.extend(trains)

    trains = service.search_trains("20261101", "서울", "부산", verbose=False)

    assert legacy.include_no_seats is True
    assert [t.train_no for t in observed] == ["101", "103"]
    assert [t.train_no for t in trains] == ["103"]


def test_korail_answering_sold_out_at_reserve_time_is_a_lost_race(monkeypatch):
    service, events = _service(monkeypatch, _Legacy(reserve_error=SoldOutError("ERR211161")))

    clock = _Clock()
    service.seat_windows._clock = clock
    service.seat_windows.observe([_train()], 1)
    clock.now = 1
    service.seat_windows.observe([_train(general="11")], 1)

    assert service.reserve_train(_train(general="11")) is None
    clock.now = 2
    service.seat_windows.observe([_train()], 1)
    assert [e["lost"] for e in events] == [True]


def test_korail2_refusing_locally_is_not_a_race(monkeypatch):
    legacy = _Legacy(reserve_error=SoldOutError())
    service, events = _service(monkeypatch, legacy)

    assert service.reserve_train(_train(general="11")) is None
    assert legacy.reserve_calls == 1
    assert events == []


def _window(before, after, passes, train="101", source="a", ours=False, lost=False):
    cycle = (after - before) / (passes + 1)
    return {
        "kind": "window",
        "date": "20261101",
        "train": train,
        "dep": "0001",
        "arr": "0020",
        "passengers": 1,
        "seat_class": "general",
        "source": source,
        "closed_before": before,
        "closed_after": after,
        "min_s": (passes - 1) * cycle,
        "max_s": after - before,
        "passes": passes,
        "lost": lost,
        "ours": ours,
    }


def test_two_watches_seeing_one_opening_count_it_once():
    events = [_window(100, 106, 3, source="a"), _window(100.5, 106.5, 3, source="b")]

    assert summarize(events, interval=1.0)["windows"] == 1


def test_one_watch_seeing_a_seat_reopen_counts_two_openings():
    # The reviewer's case: closed, open, closed, open, closed on passes 0..5.9.
    events = [_window(0, 3.0, 1), _window(1.6, 5.9, 1)]

    assert summarize(events, interval=1.0)["windows"] == 2


def test_an_opening_any_watch_booked_is_left_out_for_all_of_them():
    events = [
        _window(100, 103, 1, source="a", ours=True),
        _window(100.2, 103.2, 1, source="b"),
        _window(200, 210, 4, lost=True),
    ]

    summary = summarize(events, interval=1.0)

    assert (summary["windows"], summary["ours"], summary["lost"]) == (1, 1, 1)


def test_summary_estimates_what_a_longer_interval_would_catch():
    # Passes 1.5s apart (1s interval + 0.5s request); half the windows seen
    # once, half seen four times; one pass stretched by a 60s backoff.
    events = [_window(i * 100, i * 100 + 3, 1, train=str(i)) for i in range(4)]
    events += [_window(i * 100, i * 100 + 7.5, 4, train=str(i)) for i in range(4, 8)]
    events.append(_window(900, 966, 4, train="slow"))

    summary = summarize(events, interval=1.0)

    assert summary["cycle_s"] == 1.5
    assert summary["seen_once"] == 0.444
    # Seen once (1.5s): 1.5/2.5 = 0.6 at 2s, 1.5/3.5 = 0.43 at 3s; the rest 1.
    assert summary["caught_at_interval"] == {"1s": 1.0, "2s": 0.822, "3s": 0.746}


def test_summary_of_nothing_says_so():
    assert summarize([], interval=1.0) == {"windows": 0, "ours": 0, "lost": 0}


def test_mobile_storage_keeps_windows_newest_first_and_capped(monkeypatch):
    storage = MobileStorage(client=fakeredis.FakeRedis(decode_responses=True), secret="a" * 40)
    monkeypatch.setattr(MobileStorage, "SEAT_WINDOWS_KEEP", 2)
    for at in (1, 2, 3):
        storage.record_seat_window(_window(at, at + 3, 1))

    assert [e["closed_before"] for e in storage.get_seat_windows()] == [3, 2]


class _Pager:
    """korail2 answering ten rows a page, starting from the time it is asked for."""

    def __init__(self, rows):
        self.rows = rows
        self.asked = []

    def search_train(self, dep, arr, date, start, **kwargs):
        self.asked.append(start)
        page = [t for t in self.rows if str(t.dep_time).zfill(6) >= start][:10]
        if not page:
            raise NoResultsError()
        return page


def _paged(count):
    return [
        SimpleNamespace(train_no=f"{n:03d}", dep_time=f"{5 + n:02d}0000", has_seat=lambda: True)
        for n in range(count)
    ]


def _search(service, wanted=frozenset(), until="2400"):
    return service._search_legacy_pages(
        "서울", "부산", "20261013", "000000", None, [], until, set(wanted)
    )


def test_the_watch_search_asks_for_the_next_page_of_ten(monkeypatch):
    pager = _Pager(_paged(14))
    service, _ = _service(monkeypatch, pager)

    rows = _search(service)

    assert [t.train_no for t in rows] == [f"{n:03d}" for n in range(14)]
    # The second page starts at the last row of the first; that row is not counted twice.
    assert pager.asked == ["000000", "140000"]


def test_the_watch_search_stops_once_every_wanted_train_is_found(monkeypatch):
    pager = _Pager(_paged(14))
    service, _ = _service(monkeypatch, pager)

    rows = _search(service, wanted={"002"})

    assert len(rows) == 10
    assert len(pager.asked) == 1


def test_a_short_page_is_the_end_of_the_day_and_costs_no_second_request(monkeypatch):
    pager = _Pager(_paged(6))
    service, _ = _service(monkeypatch, pager)

    assert len(_search(service)) == 6
    assert len(pager.asked) == 1


def test_the_watch_search_reads_departures_korail_sends_as_numbers(monkeypatch):
    # 07:35:00 arrives as 73500. Read raw it is past 23:59 and the day would stop at page one.
    rows = [
        SimpleNamespace(train_no=f"{n:03d}", dep_time=73500 + n * 100, has_seat=lambda: True)
        for n in range(14)
    ]
    pager = _Pager(rows)
    service, _ = _service(monkeypatch, pager)

    assert len(_search(service, until="1200")) == 14
    assert len(pager.asked) == 2  # read raw, "73500" is past noon and the day ended at page one


def test_a_page_that_fails_after_the_first_keeps_the_first(monkeypatch):
    rows = _paged(10)

    class Flaky(_Pager):
        def search_train(self, dep, arr, date, start, **kwargs):
            if self.asked:
                raise ConnectionError("timeout")
            return super().search_train(dep, arr, date, start, **kwargs)

    service, _ = _service(monkeypatch, Flaky(rows))

    assert len(_search(service)) == 10
    assert service.search_cut_short is True


def test_a_failed_page_is_not_a_missing_watched_train(monkeypatch):
    class Flaky(_Pager):
        def search_train(self, dep, arr, date, start, **kwargs):
            if self.asked:
                raise ConnectionError("timeout")
            return super().search_train(dep, arr, date, start, **kwargs)

    service, _ = _service(monkeypatch, Flaky(_paged(14)))

    with pytest.raises(ConnectionError):
        _search(service, wanted={"012"})


def test_a_failed_page_after_a_sold_out_first_page_is_a_failed_search_not_no_seat(monkeypatch):
    class Flaky(_Pager):
        def search_train(self, dep, arr, date, start, **kwargs):
            if self.asked:
                raise ConnectionError("timeout")
            return super().search_train(dep, arr, date, start, **kwargs)

    sold_out = [
        SimpleNamespace(train_no=f"{n:03d}", dep_time=f"{5 + n:02d}0000", has_seat=lambda: False)
        for n in range(10)
    ]
    service, _ = _service(monkeypatch, Flaky(sold_out))

    with pytest.raises(SearchUnavailableError):
        service.search_trains("20261013", "서울", "부산", "000000", "2400", include_no_seats=False)


def test_a_watched_train_in_the_minute_a_legacy_page_ends_is_still_found(monkeypatch):
    def at(number, clock):
        return SimpleNamespace(train_no=number, dep_time=clock, has_seat=lambda: True)

    fill = [at(f"{n:03d}", f"{8 + n // 4:02d}{n % 4 * 15 + 5:02d}00") for n in range(1, 9)]
    pager = _Pager([at("100", "080000"), *fill, at("200", "123000"), at("300", "123000")])
    service, _ = _service(monkeypatch, pager)

    first = _search(service, wanted={"100", "300"})
    assert {t.train_no for t in first} >= {"100", "300"}

    again = _search(service, wanted={"100", "300"})

    assert "300" in {t.train_no for t in again}


class _Described:
    """A train that only has the line korail2 prints for it."""

    def __init__(self, line, dep_time=None):
        self.line = line
        if dep_time is not None:
            self.dep_time = dep_time

    def __str__(self):
        return self.line


def test_a_sold_out_train_taking_a_waiting_list_still_has_its_departure_read():
    # "...(07:27~10:03) 예약 대기(일반) ...": the last bracket of the line is the seat class, not the time.
    service = KorailService()
    waiting = _Described("[KTX] 10월 14일, 서울~부산(07:27~10:03) 예약 대기(일반) 53,900원 5%적립")
    assert service._extract_departure_time(waiting) == 727


def test_station_names_with_brackets_do_not_hide_the_departure():
    service = KorailService()
    train = _Described("[KTX] 10월 14일, 서울~울산(통도사)(09:44~12:50) 일반실 40,000원")
    assert service._extract_departure_time(train) == 944


def test_the_departure_field_is_read_first_even_when_korail_sends_a_number():
    service = KorailService()
    assert service._extract_departure_time(_Described("no times here", dep_time=73500)) == 735
    assert service._extract_departure_time(_Described("no times here", dep_time="072700")) == 727


def test_an_unreadable_line_is_zero_not_a_crash():
    assert KorailService()._extract_departure_time(_Described("???")) == 0
