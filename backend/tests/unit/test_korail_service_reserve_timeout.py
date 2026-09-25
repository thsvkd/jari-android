"""reserve_train must not report a plain sold-out for a request that never
answered (backend-core#2): a read timeout on the reserve POST is not the
same as no seats, and if Korail in fact created the pending reservation
before timing out, the caller needs to hear about that seat, not None.

A reservation already sitting there before the attempt (e.g. a duplicate
held while this search kept retrying, rail_service.py's DUPLICATE handling)
must never be handed back as this attempt's own success - that would attach
a fresh payment watchdog to a booking the caller did not just make and can
cause a seat swap or a double-counted seat upstream. reserve_train snapshots
the caller's own reservations() ids right before the POST and only adopts a
match whose id was not in that snapshot.

Built from korail2's real Train/Reservation classes (as
tests/e2e/fake_korail/jari_fake_korail.py does), not hand-made stand-ins:
Reservation overwrites dep_date from h_run_dt while Train's comes from
h_dpt_dt, so a real object is the only way a field-name mismatch between the
two would actually surface here.
"""

from types import SimpleNamespace

import pytest
import requests
from korail2.korail2 import Reservation, Train

from korail_bot.services import rail_service as rail_service_module
from korail_bot.services.korail_service import KorailService


def _train(train_no="101", dep_date="20260101", run_date=None):
    return Train(
        {
            "h_trn_clsf_cd": "100",
            "h_trn_clsf_nm": "KTX",
            "h_trn_gp_cd": "100",
            "h_trn_no": train_no,
            "h_dpt_rs_stn_nm": "서울",
            "h_dpt_rs_stn_cd": "0001",
            "h_dpt_dt": dep_date,
            "h_dpt_tm": "060000",
            "h_arv_rs_stn_nm": "부산",
            "h_arv_rs_stn_cd": "0020",
            "h_arv_dt": dep_date,
            "h_arv_tm": "090000",
            "h_run_dt": run_date or dep_date,
            "h_rsv_psb_flg": "Y",
            "h_rsv_psb_nm": "예약가능",
            "h_spe_rsv_cd": "11",
            "h_gen_rsv_cd": "11",
            "h_wait_rsv_flg": "-1",
        }
    )


def _reservation(train, rsv_id):
    return Reservation(
        {
            "h_trn_clsf_cd": train.train_type,
            "h_trn_clsf_nm": train.train_type_name,
            "h_trn_gp_cd": train.train_group,
            "h_trn_no": train.train_no,
            "h_dpt_rs_stn_nm": train.dep_name,
            "h_dpt_rs_stn_cd": train.dep_code,
            "h_dpt_tm": train.dep_time,
            "h_arv_rs_stn_nm": train.arr_name,
            "h_arv_rs_stn_cd": train.arr_code,
            "h_arv_tm": train.arr_time,
            "h_run_dt": train.run_date,
            "h_rsv_psb_flg": "Y",
            "h_spe_rsv_cd": train.special_seat,
            "h_gen_rsv_cd": train.general_seat,
            "h_wait_rsv_flg": "-1",
            "h_pnr_no": rsv_id,
            "h_tot_seat_cnt": "001",
            "h_ntisu_lmt_dt": "20260101",
            "h_ntisu_lmt_tm": "120000",
            "h_rsv_amt": "00059800",
        }
    )


class _LegacyClient:
    """Stands in for korail2's Korail client, with only what reserve_train touches.

    reservations() is called twice by the code under test: once as a
    pre-POST snapshot, once (only on a RequestException) to check what
    exists afterwards. `before`/`after` let a test tell the two calls apart,
    the same way a real reserve that times out could still leave a new PNR
    behind between them.
    """

    def __init__(self, *, reserve_error, before=(), after=None, reservations_error=None):
        self._session = SimpleNamespace(get=lambda *a, **k: None)
        self._reserve_error = reserve_error
        self._before = list(before)
        self._after = list(after) if after is not None else list(before)
        self._reservations_error = reservations_error
        self._calls = 0
        self.reserve_calls = 0

    def reserve(self, train, passengers=None, option=None):
        self.reserve_calls += 1
        raise self._reserve_error

    def reservations(self):
        self._calls += 1
        # Only the pre-POST snapshot fails; the check after the timeout answers.
        if self._reservations_error is not None and self._calls == 1:
            raise self._reservations_error
        return self._before if self._calls == 1 else self._after


def _service_with(monkeypatch, legacy_client):
    service = KorailService()
    service._logged_in = True
    service._korail_instance = legacy_client
    # note_search_failure()/wait_between_requests() must not sleep in a test.
    monkeypatch.setattr(rail_service_module.time, "sleep", lambda *_: None)
    return service


def test_reserve_train_read_timeout_backs_off_and_returns_none_when_nothing_pending(
    monkeypatch,
):
    train = _train()
    legacy = _LegacyClient(reserve_error=requests.exceptions.ReadTimeout("no answer"))
    service = _service_with(monkeypatch, legacy)

    result = service.reserve_train(train)

    # Contract with callers (rail_service.py, telebotBackProcess.py) is
    # preserved: None means "nothing to show for this attempt", never an
    # exception.
    assert result is None
    # A stalled request must count as a failure so the loop backs off,
    # unlike an ordinary sold-out answer.
    assert service._failure_streak == 1


def test_reserve_train_read_timeout_returns_the_seat_korail_created_anyway(monkeypatch):
    train = _train()
    new_reservation = _reservation(train, rsv_id="E2E_NEW")
    legacy = _LegacyClient(
        reserve_error=requests.exceptions.ReadTimeout("no answer"),
        before=(),
        after=[new_reservation],
    )
    service = _service_with(monkeypatch, legacy)

    result = service.reserve_train(train)

    assert result is new_reservation


def test_reserve_train_read_timeout_adopts_a_train_past_midnight(monkeypatch):
    # The reservation carries the run date (h_run_dt), the searched train the
    # departure date at this station (h_dpt_dt); here they differ.
    train = _train(dep_date="20260102", run_date="20260101")
    new_reservation = _reservation(train, rsv_id="E2E_NIGHT")
    legacy = _LegacyClient(
        reserve_error=requests.exceptions.ReadTimeout("no answer"),
        after=[new_reservation],
    )
    service = _service_with(monkeypatch, legacy)

    assert service.reserve_train(train) is new_reservation


def test_reserve_train_read_timeout_ignores_a_pending_seat_on_another_train(monkeypatch):
    train = _train(train_no="101")
    other_train = _train(train_no="999")
    new_reservation_on_other_train = _reservation(other_train, rsv_id="E2E_OTHER")
    legacy = _LegacyClient(
        reserve_error=requests.exceptions.ReadTimeout("no answer"),
        before=(),
        after=[new_reservation_on_other_train],
    )
    service = _service_with(monkeypatch, legacy)

    assert service.reserve_train(train) is None


def test_reserve_train_read_timeout_does_not_adopt_a_reservation_that_predates_the_attempt(
    monkeypatch,
):
    """
    Regression: holding an unpaid reservation on the same train while a
    search keeps retrying is a supported state (rail_service.py's DUPLICATE
    handling). A ReadTimeout on this attempt's own POST must not make
    reserve_train hand that pre-existing reservation back as if it were this
    attempt's success - the id was already in the pre-POST snapshot.
    """
    train = _train()
    pre_existing = _reservation(train, rsv_id="E2E_PREEXISTING")
    legacy = _LegacyClient(
        reserve_error=requests.exceptions.ReadTimeout("no answer"),
        before=[pre_existing],
        after=[pre_existing],  # unchanged - no new PNR actually appeared
    )
    service = _service_with(monkeypatch, legacy)

    assert service.reserve_train(train) is None


def test_reserve_train_read_timeout_does_not_adopt_anything_when_the_snapshot_fails(monkeypatch):
    """If the pre-POST snapshot itself cannot be taken, there is nothing to
    compare against, so nothing is adopted - just backoff and None."""
    train = _train()
    legacy = _LegacyClient(
        reserve_error=requests.exceptions.ReadTimeout("no answer"),
        reservations_error=requests.exceptions.ConnectionError("refused"),
        # A same-train hold that may well have existed before: with no
        # snapshot there is no telling, so it must not be adopted.
        after=[_reservation(train, rsv_id="E2E_UNKNOWN_AGE")],
    )
    service = _service_with(monkeypatch, legacy)

    assert service.reserve_train(train) is None
    assert service._failure_streak == 1


def test_reserve_train_read_timeout_never_raises_search_unavailable(monkeypatch):
    """
    A prior fix for this raised SearchUnavailableError, which callers do not
    catch around reserve_train (rail_service.py, telebotBackProcess.py) - it
    would end the search reporting an unexpected error instead of just
    backing off. reserve_train's None-on-nothing-to-report contract must hold.
    """
    train = _train()
    legacy = _LegacyClient(reserve_error=requests.exceptions.ConnectionError("refused"))
    service = _service_with(monkeypatch, legacy)

    try:
        result = service.reserve_train(train)
    except Exception as exc:
        pytest.fail(f"reserve_train must swallow request failures, raised {exc!r}")
    assert result is None
