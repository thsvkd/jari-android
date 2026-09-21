"""Mobile storage and worker services cannot fall back to the bot."""

from unittest.mock import MagicMock

import fakeredis
import pytest

from korail_bot.mobile.storage import MobileStorage, NamespacedRedis
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
