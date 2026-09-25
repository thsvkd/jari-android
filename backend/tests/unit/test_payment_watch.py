"""_watch_payment must not report a payment as settled on a guess
(backend-core#0).

The deadline used to end with an unconditional _settle_payment(OUTSTANDING)
regardless of what the loop had actually seen. A run of nothing but UNKNOWN
answers - the railway could not be asked, for the whole payment window - was
treated exactly like a confirmed "still unpaid", so a user who had in fact
paid was told "코레일에 확인한 결과 결제가 완료되지 않았습니다 / 좌석은
다시 풀렸습니다", and the record was marked completed so the app's own
payment watchdog never looked again.

The first fix tracked only whether OUTSTANDING was *ever* seen during the
window, which still failed the realistic sequence: an OUTSTANDING poll taken
seconds after booking (before the user could possibly have paid), then
UNKNOWN for the rest of the window because the user in fact paid and the
railway could not be re-asked. `_watch_payment` now re-reads
reservation_outcome() once more right at the deadline and decides from that
read alone.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import fakeredis
import pytest

from korail_bot.models import ReservationOutcome
from korail_bot.storage.redis import RedisStorage
from korail_bot.telegramBot import telebotBackProcess
from korail_bot.telegramBot.messages import Messages
from korail_bot.telegramBot.telebotBackProcess import BackgroundReservationProcess
from korail_bot.utils.timezone import utc_now


def _storage_on_fake_redis() -> RedisStorage:
    # object.__new__ skips RedisStorage.__init__, which opens a real socket
    # and pings it; fakeredis stands in for the client instead, so
    # get_payment_status/save_payment_status/claim_payment_watch run their
    # real code against something that behaves like Redis, with no server.
    storage = object.__new__(RedisStorage)
    storage.redis = fakeredis.FakeRedis(decode_responses=True)
    return storage


def _process(outcomes) -> BackgroundReservationProcess:
    process = BackgroundReservationProcess.__new__(BackgroundReservationProcess)
    process.rail = Mock()
    process.rail.reservation_id.return_value = "R1"
    if isinstance(outcomes, list):
        process.rail.reservation_outcome.side_effect = outcomes
    else:
        process.rail.reservation_outcome.return_value = outcomes
    process.storage = _storage_on_fake_redis()
    process.telegram = Mock()
    process.chat_id = -100
    process._watch_owner = "test:1"
    process._train_info_for_user = Mock(return_value="KTX 015 서울 → 부산")
    process._train_hints = Mock(
        return_value={"train_no": "015", "dep_date": "20260920", "dep_time": "090000"}
    )
    return process


def _run_with_ticking_clock(process, outcome_count, monkeypatch):
    base = utc_now()
    # A fake clock advanced only by the loop's own sleep, so each pass
    # through the loop advances one tick - no real delay, and the deadline
    # is reached after exactly `outcome_count` ticks.
    ticks = {"n": 0}
    monkeypatch.setattr(
        telebotBackProcess.time, "sleep", lambda *_: ticks.__setitem__("n", ticks["n"] + 1)
    )
    monkeypatch.setattr(telebotBackProcess, "utc_now", lambda: base + timedelta(seconds=ticks["n"]))
    process._payment_deadline = Mock(return_value=base + timedelta(seconds=outcome_count - 0.5))
    process._watch_payment(SimpleNamespace())


def test_a_deadline_of_nothing_but_unknown_leaves_the_payment_unsettled(monkeypatch):
    process = _process(ReservationOutcome.UNKNOWN)
    _run_with_ticking_clock(process, 1, monkeypatch)

    assert process.rail.reservation_outcome.called
    # Never settled: completed must stay False so the app watchdog still
    # checks this reservation itself.
    status = process.storage.get_payment_status(process.chat_id)
    assert status is not None
    assert status.completed is False
    sent = [call.args[1] for call in process.telegram.send_message.call_args_list]
    assert sent == [Messages.PAYMENT_UNVERIFIED]
    assert Messages.PAYMENT_EXPIRED_VERIFIED not in sent


def test_a_deadline_confirmed_outstanding_still_settles_as_expired(monkeypatch):
    # Sanity check the other side of the same branch: an actually confirmed
    # OUTSTANDING must still end the watch and settle the seat as expired.
    process = _process(ReservationOutcome.OUTSTANDING)
    _run_with_ticking_clock(process, 1, monkeypatch)

    status = process.storage.get_payment_status(process.chat_id)
    assert status.completed is True
    sent = [call.args[1] for call in process.telegram.send_message.call_args_list]
    assert sent == [Messages.PAYMENT_EXPIRED_VERIFIED]


def test_an_early_outstanding_then_unknown_for_the_rest_of_the_window_does_not_expire_a_paid_seat(
    monkeypatch,
):
    # The realistic sequence the bug missed: one OUTSTANDING poll right after
    # booking (before the user could have paid), then the user pays and the
    # railway cannot be re-asked (UNKNOWN) for the rest of the window - one
    # UNKNOWN per remaining loop pass, plus the final re-read right at the
    # deadline, which must also be UNKNOWN for this scenario.
    loop_outcomes = [ReservationOutcome.OUTSTANDING] + [ReservationOutcome.UNKNOWN] * 10
    final_outcome = ReservationOutcome.UNKNOWN
    process = _process([*loop_outcomes, final_outcome])
    _run_with_ticking_clock(process, len(loop_outcomes), monkeypatch)

    status = process.storage.get_payment_status(process.chat_id)
    assert status is not None
    assert status.completed is False
    sent = [call.args[1] for call in process.telegram.send_message.call_args_list]
    assert sent == [Messages.PAYMENT_UNVERIFIED]
    assert Messages.PAYMENT_EXPIRED_VERIFIED not in sent


@pytest.mark.parametrize(
    ("final_outcome", "message"),
    [
        (ReservationOutcome.PAID, Messages.PAYMENT_VERIFIED),
        (ReservationOutcome.RELEASED, Messages.PAYMENT_RELEASED),
    ],
    ids=["paid", "released"],
)
def test_a_real_answer_at_the_deadline_is_reported_as_itself(final_outcome, message, monkeypatch):
    # The loop never got an answer, but the re-read at the deadline did. That
    # answer is what the user hears - not "could not reach the railway".
    process = _process([ReservationOutcome.UNKNOWN, final_outcome])
    _run_with_ticking_clock(process, 1, monkeypatch)

    assert process.storage.get_payment_status(process.chat_id).completed is True
    sent = [call.args[1] for call in process.telegram.send_message.call_args_list]
    assert sent == [message]
