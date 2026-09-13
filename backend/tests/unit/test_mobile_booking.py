"""Use the real gateway and services with only railway/process boundaries faked."""

from datetime import timedelta
from unittest.mock import MagicMock

import fakeredis
import pytest

from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.runtime import MobileRuntime
from korail_bot.mobile.worker import apply_result
from korail_bot.models import (
    OnboardedAccount,
    PaymentStatus,
    ReservationOutcome,
    RunningReservation,
    TrainSearchParams,
    UserSession,
)
from korail_bot.utils.timezone import utc_now


def test_real_gateway_delegates_booking_schedule_and_favourites(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    rail = MagicMock()
    rail.login.return_value = True
    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: rail)
    start = MagicMock(return_value=True)
    monkeypatch.setattr(runtime.reservation, "start_reservation_process", start)
    alice = runtime.identity.register(
        "alice", "a long secure passphrase", runtime.identity.create_invite()
    )
    owner = runtime.identity.authenticate(alice["token"])["storage_id"]
    http = runtime.app.test_client()
    headers = {"Authorization": "Bearer " + alice["token"]}
    assert http.post(
        "/api/mobile/register",
        headers=headers,
        json={"username": "01012345678", "password": "rail password"},
    ).json == {"registered": True}
    conditions = {
        "v": 1,
        "action": "prepare_search",
        "dep_date": (utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
        "src_station": "서울",
        "dst_station": "부산",
        "dep_time": "0900",
        "max_dep_time": "1800",
        "train_type": "1",
        "seat_option": "2",
        "passenger_count": 2,
        "seat_strategy": "1",
        "seat_preference": "A,D:3-5",
    }
    response = http.post(
        "/api/mobile/search",
        headers=headers,
        json={"conditions": conditions, "trains": ["101", "105"]},
    )
    assert response.status_code == 200, response.json
    assert response.json["started"]
    params = start.call_args.kwargs["search_params"]
    assert params.train_numbers == ["101", "105"]
    assert params.passenger_count == 2
    assert params.seats_wanted.encode() == "A,D:3-5"
    assert start.call_args.kwargs["chat_id"] == owner
    assert start.call_args.kwargs["password"] == "rail password"
    runtime.storage.delete_user_session(owner)
    scheduled = http.post(
        "/api/mobile/schedule",
        headers=headers,
        json={
            "conditions": conditions,
            "start_at": (utc_now() + timedelta(minutes=10)).isoformat(),
        },
    )
    assert scheduled.status_code == 200, scheduled.json
    assert runtime.storage.get_scheduled_search(owner) is not None
    saved = http.post(
        "/api/mobile/favourites", headers=headers, json={"conditions": conditions, "name": "집으로"}
    )
    assert saved.status_code == 200
    fav_id = saved.json["favourites"][0]["id"]
    bob = runtime.identity.register(
        "bobby", "a long secure passphrase", runtime.identity.create_invite()
    )
    assert (
        http.delete(
            "/api/mobile/favourites/" + fav_id, headers={"Authorization": "Bearer " + bob["token"]}
        ).status_code
        == 404
    )
    assert http.delete("/api/mobile/favourites/" + fav_id, headers=headers).status_code == 200
    assert http.post("/api/mobile/logout", headers=headers, json={}).status_code == 200
    assert runtime.storage.get_scheduled_search(owner) is None
    assert runtime.storage.get_resume_credentials(owner) is None
    assert runtime.storage.get_onboarded_account(owner) is None
    runtime.storage.close()


def test_railway_registration_accepts_member_number_and_normalizes_phone(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    rail = MagicMock()
    rail.login.return_value = True
    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: rail)
    session = runtime.identity.register(
        "alice", "a long secure passphrase", runtime.identity.create_invite()
    )
    owner = runtime.identity.authenticate(session["token"])["storage_id"]
    http = runtime.app.test_client()
    headers = {"Authorization": "Bearer " + session["token"]}

    member = http.post(
        "/api/mobile/register",
        headers=headers,
        json={"username": "2456789012", "password": "rail password"},
    )
    assert member.status_code == 200
    rail.login.assert_called_with("2456789012", "rail password")
    assert runtime.storage.get_onboarded_account(owner).korail_id == "2456789012"

    phone = http.post(
        "/api/mobile/register",
        headers=headers,
        json={"username": "01012345678", "password": "rail password"},
    )
    assert phone.status_code == 200
    rail.login.assert_called_with("010-1234-5678", "rail password")
    assert runtime.storage.get_onboarded_account(owner).korail_id == "010-1234-5678"
    runtime.storage.close()


def test_auth_only_runtime_requires_no_bot_redis_or_rail(tmp_path, monkeypatch):
    monkeypatch.delenv("BOTTOKEN", raising=False)
    runtime = MobileRuntime(MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40))
    session = runtime.identity.register(
        "alice", "a long secure passphrase", runtime.identity.create_invite()
    )
    client = runtime.app.test_client()
    headers = {"Authorization": "Bearer " + session["token"]}
    response = client.post("/api/mobile/bootstrap", headers=headers, json={})
    assert response.status_code == 200
    assert response.json["capabilities"]["korail"] is False
    assert response.json["capabilities"]["scheduledSearch"] is False
    assert client.post("/api/mobile/search", headers=headers, json={}).status_code == 503


def test_callback_persists_event_and_clears_only_own_search(tmp_path):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    params = TrainSearchParams(
        dep_date="20260913", src_locate="서울", dst_locate="부산", dep_time="080000"
    )
    for owner in (-100, -200):
        runtime.storage.save_user_session(UserSession(chat_id=owner, in_progress=True))
        runtime.storage.save_running_reservation(
            RunningReservation(
                chat_id=owner, process_id=123, korail_id="phone", search_params=params
            )
        )
    apply_result(runtime.storage, runtime.notifications, -100, "예약 완료", 0)
    assert runtime.storage.get_running_reservation(-100) is None
    assert runtime.storage.get_running_reservation(-200) is not None
    assert runtime.storage.get_payment_status(-100).reminder_active
    assert runtime.notifications.items(-100)[0]["text"] == "예약 완료"
    assert runtime.notifications.items(-200) == []
    runtime.storage.close()


def test_payment_reminders_survive_runtime_recreation(tmp_path):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    runtime.storage.save_payment_status(
        PaymentStatus(
            chat_id=-100,
            completed=False,
            reminder_active=True,
            reservation_id="R1",
            expires_at=utc_now() + timedelta(minutes=5),
        )
    )
    runtime.remind_pending()
    runtime.remind_pending()
    assert len(runtime.notifications.items(-100)) == 1
    assert runtime.notifications.items(-100)[0]["kind"] == "payment"
    runtime.storage.close()


def test_payment_watchdog_uses_mobile_account_and_durable_sink(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    runtime.storage.save_onboarded_account(
        OnboardedAccount(chat_id=-100, korail_id="01012345678", korail_pw="rail-password")
    )
    runtime.storage.save_payment_status(
        PaymentStatus(
            chat_id=-100,
            completed=False,
            reminder_active=True,
            reservation_id="R1",
            expires_at=utc_now() + timedelta(minutes=5),
        )
    )
    rail = MagicMock()
    rail.login.return_value = True
    rail.reservation_outcome.return_value = ReservationOutcome.PAID
    watchdog = runtime.services[2]
    monkeypatch.setattr(watchdog, "_rail_service", lambda: rail)
    assert watchdog.tick() == 1
    assert runtime.storage.get_payment_status(-100).completed
    assert runtime.notifications.items(-100)
    assert runtime.notifications.items(-200) == []
    assert "rail-password" not in str(runtime.notifications.items(-100))
    runtime.storage.close()


@pytest.mark.parametrize(
    "extra",
    [
        {"operator": "srt"},
        {"seat_preference": "AD:3-5"},
        {"seat_preference": "A:9-3"},
    ],
)
def test_unsupported_conditions_are_rejected_not_ignored(extra):
    from korail_bot.mobile.gateway import MobileGateway
    from korail_bot.services.mini_app_gateway import MiniAppError

    with pytest.raises(MiniAppError):
        MobileGateway._submission(extra)


def test_waitlist_condition_is_validated_and_preserved():
    from korail_bot.mobile.gateway import MobileGateway

    submission = MobileGateway._submission(
        {
            "v": 1,
            "action": "prepare_search",
            "dep_date": "20260920",
            "src_station": "서울",
            "dst_station": "부산",
            "dep_time": "0900",
            "max_dep_time": "1200",
            "train_type": "1",
            "seat_option": "2",
            "passenger_count": 1,
            "seat_strategy": "1",
            "seat_preference": "",
            "waitlist": True,
        }
    )

    assert submission.waitlist is True
    assert submission.as_train_info()["waitlist"] is True


def test_scheduler_executes_persisted_conditions_without_chat(tmp_path, monkeypatch):
    from freezegun import freeze_time

    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    scheduler = runtime.gateway.scheduler
    params = TrainSearchParams(
        dep_date=(utc_now() + timedelta(days=2)).strftime("%Y%m%d"),
        src_locate="서울",
        dst_locate="부산",
        dep_time="090000",
    )
    start_at = utc_now() + timedelta(minutes=1)
    scheduler.schedule(-100, "01012345678", "rail-password", params, start_at)
    start = MagicMock(return_value=True)
    monkeypatch.setattr(runtime.reservation, "start_reservation_process", start)
    with freeze_time(start_at + timedelta(seconds=1)):
        scheduler.tick()
    assert start.call_args.kwargs["chat_id"] == -100
    assert start.call_args.kwargs["password"] == "rail-password"
    assert runtime.storage.get_scheduled_search(-100) is None
    assert runtime.notifications.items(-100)
    runtime.storage.close()


def test_unlink_cannot_finish_while_scheduled_start_holds_credentials(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    runtime.storage.save_onboarded_account(
        OnboardedAccount(chat_id=-100, korail_id="01012345678", korail_pw="rail-password")
    )
    scheduler = runtime.gateway.scheduler
    params = TrainSearchParams(
        dep_date=(utc_now() + timedelta(days=2)).strftime("%Y%m%d"),
        src_locate="서울",
        dst_locate="부산",
        dep_time="090000",
    )
    scheduled = scheduler.schedule(
        -100, "01012345678", "rail-password", params, utc_now() + timedelta(minutes=1)
    )
    paused, release, logout_entered, logout_done = (threading.Event() for _ in range(4))
    order = []

    def notify(*args, **kwargs):
        paused.set()
        assert release.wait(5)

    def start(*args, **kwargs):
        order.append("start")
        return True

    def logout():
        logout_entered.set()
        result = runtime.gateway.logout(-100)
        order.append("logout")
        logout_done.set()
        return result

    monkeypatch.setattr(runtime.notifications, "send_message", notify)
    monkeypatch.setattr(runtime.reservation, "start_reservation_process", start)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            firing = pool.submit(
                scheduler._fire, scheduled, scheduled.start_at + timedelta(seconds=1)
            )
            assert paused.wait(5)
            unlink = pool.submit(logout)
            assert logout_entered.wait(5)
            finished_early = logout_done.wait(0.15)
            release.set()
            firing.result(timeout=5)
            assert unlink.result(timeout=5) == {"registered": False}
        assert not finished_early
        assert order == ["start", "logout"]
        scheduler._fire(scheduled, scheduled.start_at + timedelta(seconds=2))
        assert order == ["start", "logout"]
        assert runtime.storage.get_onboarded_account(-100) is None
    finally:
        release.set()
        runtime.storage.close()
