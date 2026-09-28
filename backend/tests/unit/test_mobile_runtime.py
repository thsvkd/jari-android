"""Mobile storage and worker services cannot fall back to the bot."""

import contextlib
import itertools
import logging
import secrets
import socket
import threading
import time
from unittest.mock import MagicMock

import fakeredis
import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.exceptions import WatchError

from korail_bot.mobile.storage import (
    LEASE_SOCKET_TIMEOUT,
    MobileStorage,
    NamespacedRedis,
    lease_client_for,
)
from korail_bot.models import OnboardedAccount, UserSession


def test_mobile_namespace_and_encryption_survive_reopen():
    client = fakeredis.FakeRedis(decode_responses=True)
    client.set("user_session:123", "telegram record")
    storage = MobileStorage(client=client, secret="a" * 40)
    storage.save_user_session(UserSession(chat_id=-123))
    storage.save_onboarded_account(
        OnboardedAccount(chat_id=-123, korail_id="010-1234-5678", korail_pw="rail secret")
    )
    assert client.get("user_session:123") == "telegram record"
    assert len(storage.get_all_user_sessions()) == 1
    assert storage.get_user_session(123) is None
    assert storage.get_onboarded_account(-123).korail_pw == "rail secret"
    assert "rail secret" not in str([client.get(k) for k in client.scan_iter()])
    reopened = MobileStorage(client=client, secret="a" * 40)
    assert reopened.get_onboarded_account(-123).korail_pw == "rail secret"
    storage.redis.flushdb()
    assert client.get("user_session:123") == "telegram record"
    client.close()


def test_namespace_wrapper_cannot_forward_unknown_redis_commands():
    client = fakeredis.FakeRedis(decode_responses=True)
    with pytest.raises(AttributeError):
        NamespacedRedis(client).execute_command("FLUSHALL")
    client.close()


def test_lease_cannot_be_extended_or_deleted_by_another_owner():
    client = fakeredis.FakeRedis(decode_responses=True)
    redis = NamespacedRedis(client)
    redis.set("runtime_owner", "alice", ex=120)
    assert not redis.renew_lease("bob", 120)
    assert not redis.release_lease("bob")
    assert redis.get("runtime_owner") == "alice"
    assert redis.renew_lease("alice", 120)
    assert redis.release_lease("alice")
    client.close()


LEASE_KEY = NamespacedRedis.PREFIX + "runtime_owner"


def _drop_watched_reads(client, failures):
    """The next `failures` WATCHed reads fail as redis-py reports a dropped connection."""
    real = client.pipeline

    def pipeline(*args, **kwargs):
        transaction = real(*args, **kwargs)
        get = transaction.get

        def dropped(key):
            if failures:
                failures.pop()
                raise WatchError("A TimeoutError occurred while watching one or more keys")
            return get(key)

        transaction.get = dropped
        return transaction

    client.pipeline = pipeline


def _runtime(tmp_path, client):
    from korail_bot.mobile.config import MobileConfig
    from korail_bot.mobile.runtime import MobileRuntime

    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    runtime.services = []
    return runtime


def test_connection_failure_while_watching_is_not_a_lost_lease():
    client = fakeredis.FakeRedis(decode_responses=True)
    redis = NamespacedRedis(client)
    redis.set("runtime_owner", "alice", ex=120)
    _drop_watched_reads(client, [1])
    assert redis.renew_lease("alice", 120)
    _drop_watched_reads(client, [1] * NamespacedRedis.LEASE_ATTEMPTS)
    with pytest.raises(WatchError):
        redis.renew_lease("alice", 120)
    assert redis.get("runtime_owner") == "alice"
    assert not redis.renew_lease("bob", 120)
    client.close()


def test_second_stop_waits_until_the_first_has_released_the_lease(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    runtime.start()
    assert client.get(LEASE_KEY) == runtime.owner
    tearing_down = threading.Event()

    def slow_shutdown():
        tearing_down.set()
        time.sleep(0.5)

    monkeypatch.setattr(runtime.reservation, "shutdown", slow_shutdown)
    # The notification thread on a lost lease, then the main thread after the SIGTERM.
    first = threading.Thread(target=runtime.stop)
    first.start()
    assert tearing_down.wait(2)
    runtime.stop()
    assert client.get(LEASE_KEY) is None
    first.join()
    client.close()


def test_start_waits_out_a_lease_left_by_a_runtime_that_is_gone(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    client.set(LEASE_KEY, "killed", px=300)
    runtime = _runtime(tmp_path, client)
    monkeypatch.setattr(runtime, "LEASE_POLL", 0.05)
    runtime.start()
    assert client.get(LEASE_KEY) == runtime.owner
    runtime.stop()
    client.close()


@pytest.mark.parametrize(
    "holder",
    [
        lambda own: "alive",
        lambda own: secrets.token_hex(24),
        lambda own: own[:-1] + ("1" if own[-1] == "0" else "0"),
    ],
    ids=["another-owner", "another-runtime-token", "one-digit-off-its-own-token"],
)
def test_start_still_refuses_a_live_owner(tmp_path, monkeypatch, holder):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    assert len(runtime.owner) == 48
    live = holder(runtime.owner)
    client.set(LEASE_KEY, live, ex=120)
    monkeypatch.setattr(runtime, "LEASE_WAIT", 0.2)
    monkeypatch.setattr(runtime, "LEASE_POLL", 0.05)
    with pytest.raises(RuntimeError, match="Another mobile runtime"):
        runtime.start()
    runtime.stop()
    assert client.get(LEASE_KEY) == live
    client.close()


def _unanswered_lease_sets(client, failures, *, ran=False):
    """The next `failures` SETs of the lease time out; with `ran`, after Redis has run them."""
    real = client.set

    def unanswered(name, *args, **kwargs):
        if name == LEASE_KEY and failures:
            failures.pop()
            if ran:
                real(name, *args, **kwargs)
            raise RedisTimeoutError("Timeout reading from socket")
        return real(name, *args, **kwargs)

    client.set = unanswered


def test_start_retries_a_lease_redis_did_not_answer(tmp_path, monkeypatch, caplog):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    monkeypatch.setattr(runtime, "LEASE_POLL", 0.05)
    failures = [1, 1]
    _unanswered_lease_sets(client, failures)
    with caplog.at_level(logging.WARNING, logger="korail_bot.mobile.runtime"):
        runtime.start()
    assert failures == []
    assert client.get(LEASE_KEY) == runtime.owner
    assert sum("did not answer" in record.getMessage() for record in caplog.records) == 1
    runtime.stop()
    client.close()


def test_start_takes_a_lease_only_its_own_unanswered_set_wrote(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    # Waiting out its own lease would take LEASE_TTL; this start has 1 s.
    monkeypatch.setattr(runtime, "LEASE_WAIT", 1)
    monkeypatch.setattr(runtime, "LEASE_POLL", 0.05)
    _unanswered_lease_sets(client, [1], ran=True)
    runtime.start()
    assert client.get(LEASE_KEY) == runtime.owner
    runtime.stop()
    client.close()


def test_start_raises_without_the_lease_when_redis_never_answers(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    monkeypatch.setattr(runtime, "LEASE_WAIT", 0.2)
    monkeypatch.setattr(runtime, "LEASE_POLL", 0.05)
    failures = [1] * 1000
    _unanswered_lease_sets(client, failures)
    began = time.monotonic()
    with pytest.raises(RedisTimeoutError):
        runtime.start()
    assert time.monotonic() - began >= 0.2
    assert len(failures) < 999  # tried again, not raised at the first timeout
    assert not runtime.started and runtime.lease_until == 0
    runtime.stop()
    assert client.get(LEASE_KEY) is None
    client.close()


def _renew_for(runtime, seconds=2):
    """Run the lease thread's loop until it returns, or for `seconds`, then stop it."""
    renewing = threading.Thread(target=runtime._keep_renewing)
    renewing.start()
    renewing.join(seconds)
    runtime.stop_event.set()
    renewing.join(2)
    assert not renewing.is_alive()


def _renewals_left_running():
    return [t for t in threading.enumerate() if t.name == "mobile-lease-renewal"]


# How late past lease_until the loss may be noticed: thread scheduling on a busy runner.
EPSILON = 0.25


def test_lease_until_counts_from_when_the_lease_call_was_sent(tmp_path, monkeypatch):
    from korail_bot.mobile.runtime import LEASE_MARGIN, LEASE_TTL

    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    redis = runtime.storage.redis
    late = 0.2

    def answered_late(call):
        def answer(*args, **kwargs):
            time.sleep(late)
            return call(*args, **kwargs)

        return answer

    # Redis keeps the key LEASE_TTL from when it ran the call, some time after
    # it was sent; counting from the send is what keeps the margin whole.
    monkeypatch.setattr(redis, "acquire_lease", answered_late(redis.acquire_lease))
    monkeypatch.setattr(redis, "renew_lease", answered_late(redis.renew_lease))
    sent = time.monotonic()
    runtime._acquire_lease(redis)
    assert (
        sent + LEASE_TTL - LEASE_MARGIN
        <= runtime.lease_until
        < sent + LEASE_TTL - LEASE_MARGIN + late / 2
    )
    assert LEASE_TTL - 1 <= client.ttl(LEASE_KEY) <= LEASE_TTL
    client.expire(LEASE_KEY, 5)
    sent = time.monotonic()
    assert runtime._renew_lease(redis) is True
    assert (
        sent + LEASE_TTL - LEASE_MARGIN
        <= runtime.lease_until
        < sent + LEASE_TTL - LEASE_MARGIN + late / 2
    )
    assert LEASE_TTL - 1 <= client.ttl(LEASE_KEY) <= LEASE_TTL
    runtime.storage.close()


def test_redis_keeps_a_given_up_lease_while_every_search_is_stopped(tmp_path):
    import subprocess
    import sys

    from korail_bot.config.settings import settings

    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    runtime._acquire_lease(runtime.storage.redis)
    # How long Redis goes on holding the key after the runtime gives it up.
    held_after = client.pttl(LEASE_KEY) / 1000 - (runtime.lease_until - time.monotonic())
    # A search worker that ignores SIGTERM: stop() waits out the terminate, then kills it.
    stubborn = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            " print('ready', flush=True); time.sleep(60)",
            "korail_bot.mobile.worker",
            runtime.reservation.tag,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert stubborn.stdout.readline() == "ready\n"
        runtime.reservation._children[stubborn.pid] = stubborn
        began = time.monotonic()
        runtime.stop()
        stopping = time.monotonic() - began
        assert stubborn.poll() is not None
    finally:
        stubborn.kill()
        stubborn.wait()
        stubborn.stdout.close()
    # Workers are stopped one after another, up to the concurrent-search ceiling.
    assert settings.MAX_CONCURRENT_SEARCHES * stopping < held_after
    client.close()


def _shut(connection):
    with contextlib.suppress(OSError):
        connection.shutdown(socket.SHUT_RDWR)  # wakes a thread blocked reading it
    connection.close()


@contextlib.contextmanager
def _slow_redis(under, over):
    """
    A Redis that answers every command `under` late, and a GET `over` late.

    Slow, not silent: just under a socket timeout for every reply, and just
    over it for the watched read of a renewal. Yields its URL and the URL of
    the Redis behind it.
    """
    from fakeredis import TcpFakeServer

    server = TcpFakeServer(("127.0.0.1", 0))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    upstream = server.server_address
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.05)
    connections = []
    threads = []
    done = threading.Event()

    def relay(client):
        backend = socket.create_connection(upstream)
        connections.extend([client, backend])
        slow = threading.Event()

        def requests():
            with contextlib.suppress(OSError):
                while data := client.recv(65536):
                    if b"\r\nGET\r\n" in data:
                        slow.set()
                    backend.sendall(data)

        def replies():
            with contextlib.suppress(OSError):
                while data := backend.recv(65536):
                    time.sleep(over if slow.is_set() else under)
                    slow.clear()
                    client.sendall(data)

        for pump in (requests, replies):
            thread = threading.Thread(target=pump)
            thread.start()
            threads.append(thread)

    def accept():
        while not done.is_set():
            with contextlib.suppress(TimeoutError):
                relay(listener.accept()[0])

    accepting = threading.Thread(target=accept)
    accepting.start()
    try:
        yield (
            f"redis://127.0.0.1:{listener.getsockname()[1]}/0",
            f"redis://127.0.0.1:{upstream[1]}/0",
        )
    finally:
        done.set()
        accepting.join(2)
        listener.close()
        for connection in connections:
            _shut(connection)
        for thread in threads:
            thread.join(2)
        server.shutdown()
        server.server_close()


def test_a_renewal_redis_answers_too_slowly_is_given_up_at_lease_until(tmp_path):
    timeout = 0.5
    with _slow_redis(under=0.7 * timeout, over=1.4 * timeout) as (url, upstream):
        runtime = _runtime(tmp_path, lease_client_for(upstream))
        redis = runtime.storage.redis
        redis.lease_client = lease_client_for(
            url, socket_timeout=timeout, socket_connect_timeout=timeout
        )
        redis.set("runtime_owner", runtime.owner, ex=120)
        runtime.lease_until = time.monotonic() + 1
        # Every reply comes in time, and still the renewal would go on for
        # about 20 timeouts, 10 s (see LEASE_MARGIN). It is not waited for.
        kept = runtime._keep_lease()
        noticed = time.monotonic()
        still_out = [t.is_alive() for t in _renewals_left_running()]
    runtime.storage.close()
    for thread in _renewals_left_running():
        thread.join(5)  # ends once the Redis it waited on is gone
    assert kept is False
    assert runtime.lease_until <= noticed < runtime.lease_until + EPSILON
    assert still_out == [True]
    assert _renewals_left_running() == []


@contextlib.contextmanager
def _silent_redis():
    """A Redis that takes connections and never answers, like one on a stalled host."""
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(0.05)
    accepted = []
    done = threading.Event()

    def accept():
        while not done.is_set():
            with contextlib.suppress(TimeoutError):
                accepted.append(server.accept()[0])

    thread = threading.Thread(target=accept)
    thread.start()
    try:
        yield f"redis://127.0.0.1:{server.getsockname()[1]}/0"
    finally:
        done.set()
        thread.join(2)
        server.close()
        for connection in accepted:
            connection.close()


def test_a_lease_call_against_a_stalled_redis_fails_after_one_timeout():
    timeout = 0.25
    with _silent_redis() as url:
        lease = lease_client_for(url, socket_timeout=timeout, socket_connect_timeout=timeout)
        assert lease.connection_pool.connection_kwargs["retry"].get_retries() == 0
        redis = NamespacedRedis(fakeredis.FakeRedis(decode_responses=True), lease)
        try:
            for call in (
                lambda: redis.acquire_lease("me", 120),
                lambda: redis.lease_ttl(),
                lambda: redis.renew_lease("me", 120),
                lambda: redis.release_lease("me"),
            ):
                began = time.monotonic()
                with pytest.raises(RedisTimeoutError):
                    call()
                # One unanswered read. redis.Redis()'s default retry waits for 11.
                assert 0.8 * timeout <= time.monotonic() - began < 3 * timeout
        finally:
            redis.close()


def _open(client):
    pool = client.connection_pool
    return [c for c in [*pool._available_connections, *pool._in_use_connections] if c._sock]


@pytest.mark.parametrize("release_fails", [False, True], ids=["released", "release-failed"])
def test_lease_calls_have_their_own_client_which_stop_closes(tmp_path, monkeypatch, release_fails):
    from fakeredis import TcpFakeServer

    from korail_bot.mobile.config import MobileConfig
    from korail_bot.mobile.runtime import MobileRuntime

    server = TcpFakeServer(("127.0.0.1", 0))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"redis://127.0.0.1:{server.server_address[1]}/0"
        runtime = MobileRuntime(MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, url))
        runtime.services = []
        redis = runtime.storage.redis
        pool = redis.lease_client.connection_pool
        assert redis.lease_client is not redis.client
        assert pool.connection_kwargs["retry"].get_retries() == 0
        assert pool.connection_kwargs["socket_timeout"] == LEASE_SOCKET_TIMEOUT
        # The rest of the app keeps its client as it was.
        assert redis.client.connection_pool.connection_kwargs["socket_timeout"] == 5
        runtime.start()
        assert redis.get("runtime_owner") == runtime.owner
        assert _open(redis.lease_client) and _open(redis.client)
        if release_fails:
            monkeypatch.setattr(
                redis, "release_lease", MagicMock(side_effect=RedisTimeoutError("stall"))
            )
        runtime.stop()
        assert _open(redis.lease_client) == _open(redis.client) == []
    finally:
        server.shutdown()
        server.server_close()


def test_past_lease_until_the_lease_is_given_up_without_asking_redis(tmp_path, monkeypatch):
    lost = []
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    runtime.on_lease_lost = lambda: lost.append(True)
    monkeypatch.setattr(runtime, "PASS_WAIT", 0.05)
    runtime._acquire_lease(runtime.storage.redis)
    # Redis still holds the key and would renew it, but the margin has begun.
    runtime.lease_until = time.monotonic() - 0.01
    renew = MagicMock(wraps=runtime.storage.redis.renew_lease)
    monkeypatch.setattr(runtime.storage.redis, "renew_lease", renew)
    _renew_for(runtime)
    renew.assert_not_called()
    assert lost == [True]
    client.close()


def test_stop_while_a_renewal_is_out_is_not_taken_for_a_lost_lease(tmp_path, monkeypatch):
    lost = []
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    runtime.on_lease_lost = lambda: lost.append(True)
    runtime._acquire_lease(runtime.storage.redis)
    redis = runtime.storage.redis
    release = redis.release_lease
    released = threading.Event()
    seen = []

    def release_and_tell(owner):
        try:
            return release(owner)
        finally:
            released.set()

    # docker stop: the main thread tears down and releases the lease while
    # this renewal is out, and the renewal then finds the lease gone.
    stopping = threading.Thread(target=runtime.stop)

    def renew_during_stop(owner, ttl):
        stopping.start()
        seen.append(released.wait(2))
        return False

    monkeypatch.setattr(redis, "release_lease", release_and_tell)
    monkeypatch.setattr(redis, "renew_lease", renew_during_stop)
    _renew_for(runtime)
    stopping.join(2)
    assert seen == [True]
    assert lost == []
    assert client.get(LEASE_KEY) is None
    client.close()


def test_stop_waits_for_the_lease_thread(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    redis = runtime.storage.redis
    renew = redis.renew_lease
    out = threading.Event()

    def slow_renewal(owner, ttl):
        out.set()
        time.sleep(0.3)
        return renew(owner, ttl)

    monkeypatch.setattr(redis, "renew_lease", slow_renewal)
    runtime.start()
    assert out.wait(2)
    runtime.stop()
    assert not runtime.lease_thread.is_alive()
    assert client.get(LEASE_KEY) is None
    client.close()


FAILED_RENEWALS = pytest.mark.parametrize(
    "error", [RedisTimeoutError("stall"), AttributeError("bug")], ids=["redis-error", "bug"]
)


@FAILED_RENEWALS
def test_a_failed_renewal_is_tried_again_while_the_lease_holds(
    tmp_path, monkeypatch, caplog, error
):
    lost = []
    runtime = _runtime(tmp_path, fakeredis.FakeRedis(decode_responses=True))
    runtime.on_lease_lost = lambda: lost.append(True)
    monkeypatch.setattr(runtime, "PASS_WAIT", 0.05)
    runtime.lease_until = time.monotonic() + 60
    renew = MagicMock(side_effect=error)
    monkeypatch.setattr(runtime.storage.redis, "renew_lease", renew)
    with caplog.at_level(logging.WARNING, logger="korail_bot.mobile.runtime"):
        _renew_for(runtime, 0.5)
    assert renew.call_count >= 3
    assert lost == []
    # Once for each failure; a bug with its traceback.
    logged = [r for r in caplog.records if r.name == "korail_bot.mobile.runtime"]
    assert len(logged) == renew.call_count
    assert all("renewal failed" in r.getMessage() for r in logged)
    if isinstance(error, AttributeError):
        assert all(r.exc_info and r.exc_info[0] is AttributeError for r in logged)
    runtime.storage.close()


@FAILED_RENEWALS
def test_a_renewal_failing_until_lease_until_gives_the_lease_up_then(tmp_path, monkeypatch, error):
    lost = []
    runtime = _runtime(tmp_path, fakeredis.FakeRedis(decode_responses=True))
    runtime.on_lease_lost = lambda: lost.append(time.monotonic())
    # PASS_WAIT is 10 s: after a failure the next try comes at lease_until.
    runtime.lease_until = time.monotonic() + 0.3
    monkeypatch.setattr(runtime.storage.redis, "renew_lease", MagicMock(side_effect=error))
    _renew_for(runtime)
    assert len(lost) == 1
    assert runtime.lease_until <= lost[0] < runtime.lease_until + EPSILON


def test_slow_push_delivery_does_not_hold_up_the_lease(tmp_path, monkeypatch):
    import korail_bot.mobile.runtime as runtime_module

    # Counted on for 1 s after each renewal, kept by Redis for 2 s.
    monkeypatch.setattr(runtime_module, "LEASE_TTL", 2)
    monkeypatch.setattr(runtime_module, "LEASE_MARGIN", 1)
    lost = []
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    runtime.on_lease_lost = lambda: lost.append(True)
    monkeypatch.setattr(runtime, "PASS_WAIT", 0.1)
    redis = runtime.storage.redis
    renew = redis.renew_lease
    renewals = []

    def counted(owner, ttl):
        renewals.append(time.monotonic())
        return renew(owner, ttl)

    delivered = threading.Event()
    held = []

    def slow_delivery():
        # Pushes into a network that drops packets, for longer than the lease
        # is counted on and than Redis keeps it.
        if not delivered.is_set():
            time.sleep(2.5)
            held.append((client.get(LEASE_KEY), client.pttl(LEASE_KEY)))
            delivered.set()
        return 0

    monkeypatch.setattr(redis, "renew_lease", counted)
    monkeypatch.setattr(runtime.notifications, "deliver", slow_delivery)
    runtime.start()
    began = time.monotonic()
    assert delivered.wait(5)
    runtime.stop()
    assert lost == []
    assert held[0][0] == runtime.owner
    assert held[0][1] > 1000  # ms: renewed within the last second
    assert len([t for t in renewals if began <= t <= began + 2.5]) >= 10
    assert max(b - a for a, b in itertools.pairwise(renewals)) < 1
    assert not runtime.lease_thread.is_alive()
    client.close()


def test_a_renewal_answered_after_lease_until_changes_nothing(tmp_path, monkeypatch):
    import korail_bot.mobile.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "LEASE_TTL", 2)
    monkeypatch.setattr(runtime_module, "LEASE_MARGIN", 1)
    lost = []
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = _runtime(tmp_path, client)
    runtime.on_lease_lost = lambda: lost.append(time.monotonic())
    redis = runtime.storage.redis
    renew, release = redis.renew_lease, redis.release_lease
    answer = threading.Event()
    ran, released = [], []

    def answered_late(owner, ttl):
        answer.wait(5)
        ran.append(renew(owner, ttl))  # Redis runs it only now,
        return True  # and the reply says it renewed

    def recorded(owner):
        released.append(release(owner))
        return released[-1]

    monkeypatch.setattr(redis, "renew_lease", answered_late)
    monkeypatch.setattr(redis, "release_lease", recorded)
    runtime.start()
    deadline = runtime.lease_until
    # Given up at lease_until and torn down, by the lease thread itself.
    runtime.lease_thread.join(3)
    assert not runtime.lease_thread.is_alive()
    answer.set()
    for thread in _renewals_left_running():
        thread.join(2)
    assert len(lost) == 1
    assert deadline <= lost[0] < deadline + EPSILON
    assert released == [True]
    assert ran == [False]  # the key it would have extended was released
    assert runtime.lease_until == deadline
    assert client.get(LEASE_KEY) is None
    client.close()


def _serve(monkeypatch, tmp_path, serve, seen):
    """
    `python -m korail_bot.mobile serve`, with `serve` for the server. Appends
    to `seen` what SIGTERM is set to when runtime.stop() runs.
    """
    import signal
    import sys

    import waitress

    import korail_bot.mobile.runtime as runtime_module
    from korail_bot.mobile import __main__ as cli

    class Runtime:
        def __init__(self, config, **kwargs):
            self.app = MagicMock(config={"MAX_CONTENT_LENGTH": 1})

        def start(self):
            pass

        def stop(self):
            seen.append(signal.getsignal(signal.SIGTERM))

    monkeypatch.setattr(runtime_module, "MobileRuntime", Runtime)
    monkeypatch.setattr(waitress, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["korail_bot.mobile", "serve"])
    monkeypatch.setenv("MOBILE_SECRET", "a" * 40)
    monkeypatch.setenv("MOBILE_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("MOBILE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MOBILE_SECRET_FILE", raising=False)
    handlers = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
    try:
        cli.main()
    finally:
        signal.signal(signal.SIGINT, handlers[0])
        signal.signal(signal.SIGTERM, handlers[1])


def test_teardown_ignores_sigterm_however_the_server_ended(tmp_path, monkeypatch):
    import signal

    def serve(*args, **kwargs):
        raise OSError("address already in use")

    seen = []
    with pytest.raises(OSError, match="address already in use"):
        _serve(monkeypatch, tmp_path, serve, seen)
    assert seen == [signal.SIG_IGN]


def test_a_sigterm_leaves_sigterm_ignored_from_its_handler_on(tmp_path, monkeypatch):
    import signal

    seen = []

    def serve(*args, **kwargs):
        try:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # docker stop
        finally:
            seen.append(signal.getsignal(signal.SIGTERM))

    _serve(monkeypatch, tmp_path, serve, seen)
    assert seen == [signal.SIG_IGN, signal.SIG_IGN]


def test_failed_process_stop_does_not_claim_search_stopped(tmp_path, monkeypatch):
    from korail_bot.mobile.config import MobileConfig
    from korail_bot.mobile.process import MobileReservationService
    from korail_bot.models import RunningReservation, TrainSearchParams

    client = fakeredis.FakeRedis(decode_responses=True)
    storage = MobileStorage(client=client, secret="a" * 40)
    service = MobileReservationService(
        storage,
        MagicMock(),
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
    )
    storage.save_running_reservation(
        RunningReservation(
            chat_id=-100,
            process_id=123,
            korail_id="phone",
            search_params=TrainSearchParams(
                dep_date="20260914", src_locate="서울", dst_locate="부산", dep_time="090000"
            ),
        )
    )
    monkeypatch.setattr(service, "_terminate_search_process", lambda pid: False)
    monkeypatch.setattr(service, "_is_running", lambda pid: True)
    assert service.cancel_reservation(-100) is False
    assert storage.get_running_reservation(-100) is not None
    client.close()


def test_mobile_worker_construction_and_callback_never_construct_bot(tmp_path, monkeypatch):
    import sys

    from korail_bot.mobile.config import MobileConfig
    from korail_bot.mobile.runtime import MobileRuntime
    from korail_bot.mobile.worker import MobileSearchProcess
    from korail_bot.telegramBot import telebotBackProcess

    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    forbidden = MagicMock(side_effect=AssertionError("Bot transport is forbidden"))
    monkeypatch.setattr(telebotBackProcess, "TelegramService", forbidden)
    monkeypatch.setattr(telebotBackProcess.requests, "session", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "20260914",
            "서울",
            "부산",
            "090000",
            "TrainType.KTX",
            "ReserveOption.GENERAL_ONLY",
            "-100",
            "2400",
        ],
    )
    monkeypatch.setattr(
        MobileSearchProcess,
        "_read_credentials",
        staticmethod(lambda: ("01012345678", "rail password", "")),
    )
    monkeypatch.setattr(
        MobileSearchProcess,
        "_runtime_services",
        lambda self: (runtime.storage, runtime.notifications),
    )
    monkeypatch.setattr(MobileSearchProcess, "_build_rail_service", lambda self: MagicMock())
    worker = MobileSearchProcess()
    worker._send_callback("좌석을 예약했습니다", status=0)
    assert runtime.notifications.items(-100)
    forbidden.assert_not_called()
    runtime.storage.close()


def test_worker_environment_and_command_are_isolated(tmp_path, monkeypatch):
    from korail_bot.mobile.config import MobileConfig
    from korail_bot.mobile.process import MobileReservationService

    monkeypatch.setenv("BOTTOKEN", "bot-secret")
    monkeypatch.setenv("USERPW", "operator-password")
    config = MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:16389/1")
    service = MobileReservationService(MagicMock(), MagicMock(), config)
    command = service._process_command(["date", "from", "to"])
    assert command[2] == "korail_bot.mobile.worker"
    assert "bot-secret" not in str(command)
    env = service._process_options()["env"]
    assert "BOTTOKEN" not in env and "USERPW" not in env
    assert env["MOBILE_REDIS_URL"] == config.redis_url
    assert env["MOBILE_DATA_DIR"] == str(tmp_path)


def test_auth_only_import_and_start_have_no_network_side_effects(tmp_path):
    import os
    import subprocess
    import sys

    code = """
import socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('No socket connection allowed')
socket.socket.connect = forbidden
from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.runtime import MobileRuntime
runtime = MobileRuntime(MobileConfig(sys.argv[1], 'test-only-secret-material-with-32-characters'))
runtime.start()
runtime.stop()
assert not runtime.app.test_client().get('/api/mobile/status').status_code == 200
print('auth-only ready')
"""
    env = os.environ.copy()
    env.pop("BOTTOKEN", None)
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "identity.sqlite3")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "auth-only ready" in result.stdout


def test_a_lost_lease_ends_the_process_instead_of_serving_on(tmp_path):
    from korail_bot.mobile.config import MobileConfig
    from korail_bot.mobile.runtime import MobileRuntime

    lost = []
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=fakeredis.FakeRedis(decode_responses=True),
        on_lease_lost=lambda: lost.append(True),
    )
    runtime.lease_until = time.monotonic() + 60
    # Redis answers that another runtime holds it.
    runtime.storage.redis.set("runtime_owner", "another-runtime")

    runtime._keep_renewing()

    assert lost == [True]
    assert runtime.stop_event.is_set()


def test_keys_under_the_old_namespace_are_adopted_once_without_overwriting():
    client = fakeredis.FakeRedis(decode_responses=True)
    client.set("teum:mobile:v1:favourite:1:a", "old", ex=600)
    client.set("teum:mobile:v1:runtime_owner", "stale")
    client.set("jari:mobile:v1:runtime_owner", "current")
    redis = NamespacedRedis(client)

    assert redis.adopt_legacy_keys() == 1
    assert redis.get("favourite:1:a") == "old"
    assert 0 < redis.ttl("favourite:1:a") <= 600
    assert redis.get("runtime_owner") == "current"
    assert client.get("teum:mobile:v1:favourite:1:a") is None
    assert redis.adopt_legacy_keys() == 0


def test_worker_writes_the_booked_train_down_before_the_app_is_told(tmp_path, monkeypatch):
    # The app reads its status the moment the booking notice lands. A payment
    # record written after that notice showed as an empty card in between.
    import sys

    from korail2.korail2 import Reservation

    from korail_bot.mobile.config import MobileConfig
    from korail_bot.mobile.runtime import MobileRuntime
    from korail_bot.mobile.worker import MobileSearchProcess
    from korail_bot.models import PaymentStatus
    from korail_bot.telegramBot.telebotBackProcess import BackgroundReservationProcess

    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    # An earlier booking the user already paid for must not swallow this one.
    runtime.storage.save_payment_status(
        PaymentStatus(chat_id=-100, completed=True, reminder_active=False, train_info="지난 표")
    )
    booked = Reservation(
        {
            "h_trn_clsf_nm": "KTX",
            "h_trn_no": "00101",
            "h_dpt_rs_stn_nm": "서울",
            "h_arv_rs_stn_nm": "부산",
            "h_run_dt": "20260924",
            "h_dpt_tm": "131800",
            "h_arv_tm": "155900",
            "h_pnr_no": "R1",
            "h_tot_seat_cnt": "001",
            "h_ntisu_lmt_dt": "20260923",
            "h_ntisu_lmt_tm": "033200",
            "h_rsv_amt": "00078200",
        }
    )
    rail = MagicMock()
    rail.login.return_value = True
    rail.search_and_reserve_loop.return_value = booked
    rail.reservation_id.return_value = "R1"
    rail.payment_due.return_value = ("20260923", "033200")
    rail.describe_train.return_value = {"no": "00101", "dep_time": "131800"}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "20260924",
            "서울",
            "부산",
            "130000",
            "TrainType.KTX",
            "ReserveOption.GENERAL_ONLY",
            "-100",
            "2400",
        ],
    )
    monkeypatch.setattr(
        MobileSearchProcess, "_read_credentials", staticmethod(lambda: ("01012345678", "pw", ""))
    )
    monkeypatch.setattr(
        MobileSearchProcess,
        "_runtime_services",
        lambda self: (runtime.storage, runtime.notifications),
    )
    monkeypatch.setattr(MobileSearchProcess, "_build_rail_service", lambda self: rail)
    worker = MobileSearchProcess()
    seen = []
    tell = worker._send_callback

    def at_callback(message, **kwargs):
        seen.append(runtime.storage.get_payment_status(-100))
        tell(message, **kwargs)

    worker._send_callback = at_callback
    worker._watch_payment = lambda reservation: None

    BackgroundReservationProcess.run(worker)

    assert seen[0].train_info == "KTX 00101 서울 → 부산 · 9월 24일(목) 13:18→15:59"
    assert seen[0].reservation_id == "R1" and not seen[0].completed
    after = runtime.storage.get_payment_status(-100)
    assert after.train_info == seen[0].train_info and after.reminder_active
    runtime.storage.close()
