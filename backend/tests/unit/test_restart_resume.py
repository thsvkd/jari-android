"""A search running when the API restarts comes back in the new process."""

import json
import sys
import threading
import time
from datetime import timedelta
from unittest.mock import MagicMock

import fakeredis
import pytest

from korail_bot.config.settings import settings
from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.process import MobileReservationService
from korail_bot.mobile.storage import MobileStorage
from korail_bot.models import (
    DeathCause,
    MultiReservationStatus,
    PaymentStatus,
    RunningReservation,
    TrainSearchParams,
)
from korail_bot.utils.timezone import utc_now

CHAT = -100
# A PID no process here has: the record's process is gone, as after a restart.
GONE_PID = 4_000_000


def _service(tmp_path):
    client = fakeredis.FakeRedis(decode_responses=True)
    storage = MobileStorage(client=client, secret="a" * 40)
    service = MobileReservationService(
        storage,
        MagicMock(),
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
    )
    return storage, service


def _left_by_an_earlier_run(storage, *, chat_id=CHAT, pid=GONE_PID, started_minutes_ago=30):
    record = RunningReservation(
        chat_id=chat_id,
        process_id=pid,
        korail_id="0000000000",
        search_params=TrainSearchParams(
            dep_date="20261003", src_locate="서울", dst_locate="부산", dep_time="070000"
        ),
        run_id="an-earlier-run",
        started_at=utc_now() - timedelta(minutes=started_minutes_ago),
    )
    storage.save_running_reservation(record)
    storage.save_resume_credentials(chat_id, "0000000000", "rail password")
    return record


def _worker_that(service, code):
    """
    Spawn `code` in place of the search worker, with the worker's command line.

    _owns_process recognises a worker by its module name and the runtime's tag
    on the command line; this child carries both, so everything that stops or
    looks for a search treats it as one.
    """
    service._process_command = lambda arguments: [
        sys.executable,
        "-c",
        code,
        "korail_bot.mobile.worker",
        service.tag,
    ]


def _stop_children(service):
    for child in list(service._children.values()):
        child.kill()
        child.wait()


# ==================== 1. The login outlives RESUME_TTL_SECONDS ====================


def test_a_running_search_keeps_its_login_past_the_resume_ttl(tmp_path):
    storage, service = _service(tmp_path)
    _left_by_an_earlier_run(storage)
    storage.get_or_create_app_session_start(CHAT)
    # What is left of it four days into a search for a sold-out train.
    storage.redis.expire(f"resume_credentials:{CHAT}", 5)
    storage.redis.expire(f"app_session_start:{CHAT}", 5)

    service.keep_resumable()

    assert storage.resume_credentials_ttl(CHAT) > settings.RESUME_TTL_SECONDS - 60
    assert storage.redis.ttl(f"app_session_start:{CHAT}") > settings.RESUME_TTL_SECONDS - 60
    storage.close()


def test_the_background_pass_keeps_logins_and_retries_restart_recovery(tmp_path, monkeypatch):
    from korail_bot.mobile.runtime import MobileRuntime

    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    runtime.PASS_WAIT = 0.05
    _left_by_an_earlier_run(runtime.storage)
    runtime.storage.redis.expire(f"resume_credentials:{CHAT}", 5)
    reconciled = []
    monkeypatch.setattr(
        runtime.reservation, "reconcile_after_restart", lambda: reconciled.append(1)
    )

    thread = threading.Thread(target=runtime._run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (
        len(reconciled) >= 2 and runtime.storage.resume_credentials_ttl(CHAT) > 60
    ):
        time.sleep(0.02)
    runtime.stop_event.set()
    thread.join(5)

    assert len(reconciled) >= 2
    assert runtime.storage.resume_credentials_ttl(CHAT) > settings.RESUME_TTL_SECONDS - 60
    runtime.storage.close()


# ==================== 2. A worker rides out Korail not answering its login ====================


class _Rail:
    """Korail as the worker sees it: login answers in the given order, then a search."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.logins = 0
        self.searched = False
        self.login_unavailable = False

    def login(self, username, password):
        self.logins += 1
        logged_in, self.login_unavailable = self.answers.pop(0)
        return logged_in

    def search_and_reserve_loop(self, **kwargs):
        from korail_bot.telegramBot.telebotBackProcess import SearchStopped

        self.searched = True
        # How a search that is under way ends when asked to stop.
        raise SearchStopped(15)


def _worker(tmp_path, monkeypatch, rail):
    from korail_bot.mobile.worker import MobileSearchProcess
    from korail_bot.telegramBot import telebotBackProcess

    storage, _ = _service(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "20261003",
            "서울",
            "부산",
            "070000",
            "TrainType.KTX",
            "GENERAL_FIRST",
            "-100",
            "2400",
        ],
    )
    monkeypatch.setattr(
        MobileSearchProcess,
        "_read_credentials",
        staticmethod(lambda: ("0000000000", "rail password", "")),
    )
    monkeypatch.setattr(
        MobileSearchProcess, "_runtime_services", lambda self: (storage, MagicMock())
    )
    monkeypatch.setattr(MobileSearchProcess, "_build_rail_service", lambda self: rail)
    waits = []
    monkeypatch.setattr(telebotBackProcess.time, "sleep", waits.append)
    worker = MobileSearchProcess()
    worker._send_callback = MagicMock()
    return worker, waits


def test_a_login_korail_did_not_answer_twice_is_tried_again_and_the_search_runs(
    tmp_path, monkeypatch
):
    from korail_bot.telegramBot.telebotBackProcess import (
        BackgroundReservationProcess,
        SearchStopped,
    )

    monkeypatch.setattr(settings, "LOGIN_RETRY_DELAYS_SECONDS", (5.0, 15.0, 30.0))
    rail = _Rail([(False, True), (False, True), (True, False)])
    worker, waits = _worker(tmp_path, monkeypatch, rail)

    with pytest.raises(SearchStopped):
        BackgroundReservationProcess.run(worker)

    assert rail.logins == 3
    assert waits == [5.0, 15.0]
    assert rail.searched
    worker._send_callback.assert_not_called()


def test_a_refused_login_ends_the_search_at_once(tmp_path, monkeypatch):
    from korail_bot.telegramBot.telebotBackProcess import BackgroundReservationProcess

    rail = _Rail([(False, False)])
    worker, waits = _worker(tmp_path, monkeypatch, rail)

    BackgroundReservationProcess.run(worker)

    assert (rail.logins, waits, rail.searched) == (1, [], False)
    message = worker._send_callback.call_args.args[0]
    assert worker._send_callback.call_args.kwargs["status"] == 1
    assert "로그인 실패" in message


def test_korail_unreachable_past_every_retry_says_so_rather_than_blame_the_password(
    tmp_path, monkeypatch
):
    from korail_bot.telegramBot.telebotBackProcess import BackgroundReservationProcess

    monkeypatch.setattr(settings, "LOGIN_RETRY_DELAYS_SECONDS", (1.0, 2.0))
    rail = _Rail([(False, True)] * 3)
    worker, waits = _worker(tmp_path, monkeypatch, rail)

    BackgroundReservationProcess.run(worker)

    assert (rail.logins, waits, rail.searched) == (3, [1.0, 2.0], False)
    message = worker._send_callback.call_args.args[0]
    assert worker._send_callback.call_args.kwargs["status"] == 1
    assert "연결하지 못해" in message
    assert "비밀번호" not in message


# ==================== 3. A failed resume is tried again ====================


def test_a_resume_that_dies_on_the_way_up_keeps_the_search_and_tries_again(tmp_path, monkeypatch):
    storage, service = _service(tmp_path)
    monkeypatch.setattr(settings, "PROCESS_START_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(service, "_RESUME_RETRY_SECONDS", 0.0)
    left = _left_by_an_earlier_run(storage)

    _worker_that(service, "import sys; sys.exit(3)")
    assert service.reconcile_after_restart()["retrying"] == 1

    # Nothing destroyed and nothing said: the record, the login to resume
    # with, and no death notice contradicting the resume that follows.
    record = storage.get_running_reservation(CHAT)
    assert (record.run_id, record.process_id) == (left.run_id, left.process_id)
    assert storage.get_resume_credentials(CHAT) == ("0000000000", "rail password")
    assert storage.get_dead_search(CHAT) is None
    service.telegram.send_message.assert_not_called()

    _worker_that(service, "import sys, time; sys.stdin.read(); time.sleep(30)")
    try:
        assert service.reconcile_after_restart()["resumed"] == 1
        record = storage.get_running_reservation(CHAT)
        assert record.run_id == settings.RUN_ID
        assert record.process_id in service._children
        assert storage.get_dead_search(CHAT) is None
        (notice,) = [call.args[1] for call in service.telegram.send_message.call_args_list]
        assert "다시 시작" in notice
    finally:
        _stop_children(service)
        storage.close()


# ==================== 4. Retried with a cap, then given up once ====================


def test_restart_recovery_that_keeps_failing_gives_up_once_and_keeps_the_login(
    tmp_path, monkeypatch
):
    storage, service = _service(tmp_path)
    monkeypatch.setattr(service, "_RESUME_ATTEMPTS", 3)
    monkeypatch.setattr(service, "_RESUME_RETRY_SECONDS", 0.0)
    _left_by_an_earlier_run(storage)

    def broken(reservation):
        raise RuntimeError("Redis dropped the call")

    monkeypatch.setattr(service, "_resume", broken)

    assert service.reconcile_after_restart()["retrying"] == 1
    assert service.reconcile_after_restart()["retrying"] == 1
    # Still recorded between tries, and nobody told anything yet.
    assert storage.get_running_reservation(CHAT) is not None
    service.telegram.send_message.assert_not_called()

    assert service.reconcile_after_restart()["failed"] == 1
    assert storage.get_running_reservation(CHAT) is None
    dead = storage.get_dead_search(CHAT)
    assert dead.cause is DeathCause.RESUME_FAILED
    # Starting it again by hand still has a login to start it with.
    assert dead.resumable is True
    assert storage.get_resume_credentials(CHAT) is not None
    assert service.telegram.send_message.call_count == 1
    assert "재시작" in service.telegram.send_message.call_args.args[1]

    # Exactly one notice, however many passes follow.
    service.reconcile_after_restart()
    assert service.telegram.send_message.call_count == 1
    storage.close()


def test_a_retry_not_yet_due_is_left_alone(tmp_path, monkeypatch):
    storage, service = _service(tmp_path)
    tries = []
    monkeypatch.setattr(service, "_resume", lambda reservation: tries.append(1) or False)
    _left_by_an_earlier_run(storage)

    service.reconcile_after_restart()
    service.reconcile_after_restart()

    assert tries == [1]
    storage.close()


def test_a_search_stopped_while_its_retry_waited_is_not_brought_back(tmp_path, monkeypatch):
    storage, service = _service(tmp_path)
    monkeypatch.setattr(service, "_RESUME_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(service, "_resume", lambda reservation: False)
    _left_by_an_earlier_run(storage)
    service.reconcile_after_restart()

    assert service.cancel_reservation(CHAT) is True
    started = MagicMock(return_value=True)
    monkeypatch.setattr(service, "_resume", started)
    service.reconcile_after_restart()

    started.assert_not_called()
    assert storage.get_running_reservation(CHAT) is None
    storage.close()


# ==================== 5. A seat already held is not searched for again ====================


def test_a_search_that_had_just_taken_a_seat_is_not_resumed_beside_it(tmp_path, monkeypatch):
    storage, service = _service(tmp_path)
    start = MagicMock(side_effect=AssertionError("resumed beside a held seat"))
    monkeypatch.setattr(service, "start_reservation_process", start)
    _left_by_an_earlier_run(storage, started_minutes_ago=30)
    # Korail held the seat and the worker wrote it down; the SIGTERM came
    # before the callback cleared the running record.
    storage.save_payment_status(
        PaymentStatus(
            chat_id=CHAT,
            completed=False,
            reminder_active=True,
            reservation_id="PNR1",
            expires_at=utc_now() + timedelta(minutes=10),
        )
    )

    assert service.reconcile_after_restart()["interrupted"] == 1

    assert storage.get_running_reservation(CHAT) is None
    assert storage.get_resume_credentials(CHAT) is None
    assert "두 번 잡지" in service.telegram.send_message.call_args.args[1]
    storage.close()


def test_a_seat_held_from_before_the_search_began_does_not_stop_it_resuming(tmp_path, monkeypatch):
    storage, service = _service(tmp_path)
    monkeypatch.setattr(service, "start_reservation_process", MagicMock(return_value=True))
    _left_by_an_earlier_run(storage, started_minutes_ago=5)
    storage.save_payment_status(
        PaymentStatus(
            chat_id=CHAT,
            completed=False,
            reservation_id="PNR0",
            created_at=utc_now() - timedelta(minutes=8),
        )
    )

    assert service.reconcile_after_restart()["resumed"] == 1
    storage.close()


def test_seats_held_one_by_one_since_the_search_began_stop_it_resuming(tmp_path):
    from korail_bot.models import ReservationPaymentStatus, SingleReservationInfo

    storage, service = _service(tmp_path)
    record = _left_by_an_earlier_run(storage)
    storage.save_multi_reservation_status(
        MultiReservationStatus(
            chat_id=CHAT,
            reservations=[
                SingleReservationInfo(
                    reservation_id="PNR1",
                    reservation_obj=None,
                    reserved_at=utc_now(),
                    expires_at=utc_now() + timedelta(minutes=10),
                    status=ReservationPaymentStatus.PENDING,
                    seat_number=1,
                    train_info="KTX 00101",
                )
            ],
            total_seats=2,
            seat_strategy="random",
            created_at=utc_now(),
        )
    )

    assert service.resume_blocker(record) == "seat_held"
    storage.close()


# ==================== 6. Shutdown is never mistaken for deaths ====================


def test_stop_marks_the_shutdown_before_it_stops_anything(tmp_path):
    from korail_bot.mobile.runtime import MobileRuntime

    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    seen = []
    first = MagicMock()
    first.stop.side_effect = lambda: seen.append(runtime.reservation._shutting_down)
    first._thread = None
    runtime.services = [first]

    runtime.stop()

    assert seen == [True]


def test_a_dead_search_pass_that_meets_the_shutdown_reports_nothing(tmp_path, monkeypatch):
    storage, service = _service(tmp_path)
    record = _left_by_an_earlier_run(storage)
    record.run_id = settings.RUN_ID
    storage.save_running_reservation(record)
    real_read = storage.get_running_reservation

    def read_as_shutdown_begins(chat_id):
        # The teardown starts between the pass's check and its verdict.
        service._shutting_down = True
        return real_read(chat_id)

    monkeypatch.setattr(storage, "get_running_reservation", read_as_shutdown_begins)

    assert service.detect_dead_searches() == 0
    assert real_read(CHAT) is not None
    assert storage.get_dead_search(CHAT) is None
    storage.close()


def test_nothing_starts_once_shutdown_has_begun(tmp_path, monkeypatch):
    storage, service = _service(tmp_path)
    spawn = MagicMock(side_effect=AssertionError("spawned during shutdown"))
    monkeypatch.setattr("subprocess.Popen", spawn)
    service._shutting_down = True

    assert not service.start_reservation_process(
        chat_id=CHAT,
        username="0000000000",
        password="rail password",
        search_params=TrainSearchParams(
            dep_date="20261003", src_locate="서울", dst_locate="부산", dep_time="070000"
        ),
        resumed=True,
        restoring=True,
    )
    storage.close()


# ==================== 7. Never kill a search this run just resumed ====================


def test_a_stale_pid_that_is_now_one_of_our_own_searches_is_left_alone(tmp_path, monkeypatch):
    storage, service = _service(tmp_path)
    _worker_that(service, "import sys, time; sys.stdin.read(); time.sleep(30)")
    monkeypatch.setattr(settings, "PROCESS_START_GRACE_SECONDS", 0.2)
    try:
        _left_by_an_earlier_run(storage, chat_id=-1)
        assert service.reconcile_after_restart()["resumed"] == 1
        resumed_pid = storage.get_running_reservation(-1).process_id
        # The second record's old PID is the one the first resume was just given.
        _left_by_an_earlier_run(storage, chat_id=-2, pid=resumed_pid)

        assert service.reconcile_after_restart()["resumed"] == 1

        assert service._children[resumed_pid].poll() is None
        assert storage.get_running_reservation(-1).process_id == resumed_pid
    finally:
        _stop_children(service)
        storage.close()


# ==================== What the deploy check reads ====================


def test_the_running_listing_says_what_a_restart_would_do_without_secrets(tmp_path, monkeypatch):
    from korail_bot.mobile import __main__ as cli

    storage, service = _service(tmp_path)
    _left_by_an_earlier_run(storage, chat_id=-1)
    _left_by_an_earlier_run(storage, chat_id=-2)
    storage.save_partial_reservation(-2, 0, {"rsv_id": "PNR1"})
    monkeypatch.setattr("korail_bot.mobile.storage.MobileStorage", lambda **kwargs: storage)
    printed = []
    monkeypatch.setattr("builtins.print", lambda text: printed.append(text))

    cli.print_running(service.config, as_json=True)

    (listing,) = printed
    assert "0000000000" not in listing and "rail password" not in listing
    searches = json.loads(listing)["searches"]
    assert [row["id"] for row in searches] == [-2, -1]
    assert searches[0]["resumable"] is False
    assert searches[0]["reason"] == "seats_reserved"
    assert searches[1]["resumable"] is True
    assert searches[1]["reason"] is None
    assert searches[1]["runId"] == "an-earlier-run"
    assert searches[1]["workerAlive"] is False
    assert searches[1]["credentialTtlSeconds"] > 0


@pytest.mark.parametrize("resume", [True, False])
def test_the_running_listing_follows_the_resume_setting(tmp_path, monkeypatch, resume):
    storage, service = _service(tmp_path)
    monkeypatch.setattr(settings, "RESUME_ON_RESTART", resume)
    _left_by_an_earlier_run(storage)

    (row,) = service.describe_running()

    assert row["resumable"] is resume
    assert row["reason"] == (None if resume else "resume_disabled")
    storage.close()
