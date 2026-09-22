"""A running record says a search was started; these say whether it is running."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis
from freezegun import freeze_time

from korail_bot.config.settings import settings
from korail_bot.mobile.storage import MobileStorage
from korail_bot.models import RunningReservation, TrainSearchParams
from korail_bot.services.mini_app_gateway import MiniAppGateway
from korail_bot.telegramBot.telebotBackProcess import BackgroundReservationProcess
from korail_bot.utils.timezone import utc_now


def _gateway(client, alive=True):
    """The real gateway and storage, with only the process check faked."""
    storage = MobileStorage(client=client, secret="a" * 40)
    reservation = MagicMock()
    reservation.is_search_alive.return_value = alive
    return storage, MiniAppGateway(storage, MagicMock(), reservation, MagicMock())


def _start_running(storage, minutes_ago=2):
    storage.save_running_reservation(
        RunningReservation(
            chat_id=-100,
            process_id=123,
            korail_id="phone",
            search_params=TrainSearchParams(
                dep_date="20260930",
                src_locate="서울",
                dst_locate="부산",
                dep_time="070000",
            ),
            run_id=settings.RUN_ID,
            started_at=utc_now() - timedelta(minutes=minutes_ago),
        )
    )


def test_a_worker_that_is_asking_reports_when_it_last_did_and_how_often():
    client = fakeredis.FakeRedis(decode_responses=True)
    storage, gateway = _gateway(client)
    _start_running(storage)
    storage.save_search_heartbeat(-100, attempts=137, failure_streak=0)

    running = gateway._running(-100)

    assert running["health"] == "healthy"
    assert running["attemptCount"] == 137
    assert running["lastCheckedAt"] == storage.get_search_heartbeat(-100)["checkedAt"]
    # Since the search started, not since this pass of its loop.
    assert 120 <= running["elapsedSeconds"] < 180
    storage.close()


def test_a_worker_that_has_not_finished_its_first_pass_is_alive_without_a_time():
    client = fakeredis.FakeRedis(decode_responses=True)
    storage, gateway = _gateway(client)
    _start_running(storage, minutes_ago=0)

    running = gateway._running(-100)

    # Running, and honest that there is nothing to report yet: an invented
    # check time would be the same lie as no time at all, only harder to spot.
    assert running["health"] == "healthy"
    assert running["lastCheckedAt"] is None
    assert running["attemptCount"] is None
    storage.close()


def test_a_record_whose_worker_is_gone_is_unavailable():
    client = fakeredis.FakeRedis(decode_responses=True)
    storage, gateway = _gateway(client, alive=False)
    _start_running(storage)
    # Even with a stamp on file: it is what the search managed before dying.
    storage.save_search_heartbeat(-100, attempts=137, failure_streak=0)

    running = gateway._running(-100)

    assert running["health"] == "unavailable"
    assert running["lastCheckedAt"] is None
    assert running["attemptCount"] is None
    storage.close()


def test_the_stamp_of_an_earlier_search_is_not_read_as_this_one_s():
    client = fakeredis.FakeRedis(decode_responses=True)
    storage, gateway = _gateway(client)
    # The stamp outlives the search that wrote it - nothing clears it - so a
    # search started since must not inherit the last check of its predecessor.
    with freeze_time(utc_now() - timedelta(minutes=10)):
        storage.save_search_heartbeat(-100, attempts=900, failure_streak=0)
    _start_running(storage)

    running = gateway._running(-100)

    assert running["health"] == "healthy"
    assert running["lastCheckedAt"] is None
    assert running["attemptCount"] is None
    storage.close()


def test_a_run_of_failures_is_a_search_in_trouble_not_a_search_finding_nothing():
    client = fakeredis.FakeRedis(decode_responses=True)
    storage, gateway = _gateway(client)
    _start_running(storage)
    storage.save_search_heartbeat(
        -100, attempts=137, failure_streak=settings.KORAIL_FAILURE_ALERT_THRESHOLD
    )

    running = gateway._running(-100)

    # The process is fine and the last check is real; what it got back is not.
    assert running["health"] == "error"
    assert running["attemptCount"] == 137
    assert running["lastCheckedAt"] is not None
    storage.close()


def test_one_failed_request_is_not_yet_trouble():
    client = fakeredis.FakeRedis(decode_responses=True)
    storage, gateway = _gateway(client)
    _start_running(storage)
    storage.save_search_heartbeat(-100, attempts=137, failure_streak=1)

    assert gateway._running(-100)["health"] == "healthy"
    storage.close()


def _search_process(storage):
    """
    A search process with only the state the reporting path reads.

    Built without __init__, which parses argv and reads credentials off
    stdin: this is about one callback of a loop, not about starting a search.
    """
    search = object.__new__(BackgroundReservationProcess)
    search.storage = storage
    search.chat_id = -100
    search.telegram = MagicMock()
    search._report_minutes = 0
    search._report_minutes_read_at = None
    search._reported_at = 0.0
    return search


def test_the_worker_stamps_every_pass_even_when_progress_reports_are_off():
    client = fakeredis.FakeRedis(decode_responses=True)
    storage = MobileStorage(client=client, secret="a" * 40)
    # Reports off, which is the default: the stamp is not a message to the
    # user, so /notify must not be what decides whether the app can see a
    # living search.
    search = _search_process(storage)

    search._report_search_progress(
        SimpleNamespace(attempts=7, failure_streak=0, elapsed_seconds=12.0)
    )

    assert storage.get_search_heartbeat(-100)["attempts"] == 7
    search.telegram.send_message.assert_not_called()
    storage.close()


def test_a_redis_that_will_not_take_the_stamp_does_not_stop_the_search():
    storage = MagicMock()
    storage.save_search_heartbeat.side_effect = ConnectionError("redis is away")
    storage.get_progress_report_minutes.return_value = 0

    # Nothing raised: a search is not worth ending over a field on a screen.
    _search_process(storage)._report_search_progress(
        SimpleNamespace(attempts=7, failure_streak=0, elapsed_seconds=12.0)
    )


def test_an_unreadable_stamp_is_no_stamp():
    client = fakeredis.FakeRedis(decode_responses=True)
    storage, gateway = _gateway(client)
    _start_running(storage)
    storage.redis.set("search_heartbeat:-100", "not json")

    running = gateway._running(-100)

    assert running["health"] == "healthy"
    assert running["lastCheckedAt"] is None
    storage.close()


def test_a_record_left_by_an_earlier_run_counts_as_gone():
    """The process that run started is not ours, whatever its PID now holds."""
    from korail_bot.services.reservation_service import ReservationService

    service = ReservationService(MagicMock(), MagicMock())
    service._owns_process = MagicMock(return_value=True)
    params = TrainSearchParams(
        dep_date="20260930", src_locate="서울", dst_locate="부산", dep_time="070000"
    )

    mine = RunningReservation(
        chat_id=-100, process_id=123, korail_id="p", search_params=params, run_id=settings.RUN_ID
    )
    theirs = RunningReservation(
        chat_id=-100, process_id=123, korail_id="p", search_params=params, run_id="an earlier run"
    )

    assert service.is_search_alive(mine)
    assert not service.is_search_alive(theirs)
    # The PID was never even looked at for the stale one.
    assert service._owns_process.call_count == 1
