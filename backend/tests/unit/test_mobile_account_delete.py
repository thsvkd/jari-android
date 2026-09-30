"""회원 탈퇴: 찾기를 멈추고 그 계정의 기록을 모두 지우며, 다른 계정은 건드리지 않아요. 공개 안내 페이지도 여기서 봐요."""

from datetime import timedelta
from unittest.mock import MagicMock

import fakeredis
import pytest

from korail_bot.mobile.__main__ import delete_account as delete_account_cli
from korail_bot.mobile.api import create_app
from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.identity import IdentityStore
from korail_bot.mobile.runtime import MobileRuntime
from korail_bot.models import (
    DeadSearch,
    DeathCause,
    PaymentStatus,
    RunningReservation,
    TrainSearchParams,
)
from korail_bot.utils.timezone import utc_now

PASSWORD = "a long secure passphrase"


def conditions():
    return {
        "v": 1,
        "action": "prepare_search",
        "dep_date": (utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
        "src_station": "서울",
        "dst_station": "부산",
        "dep_time": "0900",
        "max_dep_time": "1800",
        "train_type": "1",
        "seat_option": "2",
        "passenger_count": 1,
        "seat_strategy": "1",
        "seat_preference": "",
    }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    sent = []
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
        push=lambda token, event_id: sent.append(token),
    )
    rail = MagicMock()
    rail.login.return_value = True
    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: rail)
    monkeypatch.setattr(
        runtime.reservation, "start_reservation_process", MagicMock(return_value=True)
    )
    yield runtime
    runtime.storage.close()


def member(runtime, username):
    """A member with a linked Korail login, a favourite, an inbox item and a phone."""
    signed = runtime.identity.register(username, PASSWORD, runtime.identity.create_invite())
    http = runtime.app.test_client()
    headers = {"Authorization": "Bearer " + signed["token"]}
    assert http.post(
        "/api/mobile/register",
        headers=headers,
        json={"username": "0000000000", "password": "rail password"},
    ).json == {"registered": True}
    assert (
        http.post(
            "/api/mobile/favourites", headers=headers, json={"conditions": conditions()}
        ).status_code
        == 200
    )
    assert http.post("/api/mobile/notify", headers=headers, json={"minutes": 30}).status_code == 200
    assert (
        http.post(
            "/api/mobile/devices",
            headers=headers,
            json={"token": f"fcm-token-of-{username}-0123456789", "platform": "android"},
        ).status_code
        == 200
    )
    owner = runtime.identity.authenticate(signed["token"])["storage_id"]
    runtime.notifications.publish(owner, "자리를 찾고 있어요.")
    return http, headers, owner, signed


def owner_keys(runtime, owner):
    return sorted(
        key
        for pattern in (f"*:{owner}", f"*:{owner}:*")
        for key in runtime.storage.redis.scan_iter(pattern)
    )


def running(owner, pid=4242):
    return RunningReservation(
        chat_id=owner,
        process_id=pid,
        korail_id="0000000000",
        search_params=TrainSearchParams(
            dep_date="20261003", src_locate="서울", dst_locate="부산", dep_time="070000"
        ),
    )


def test_deleting_stops_the_search_and_erases_only_that_account(runtime, monkeypatch):
    http, headers, owner, alice = member(runtime, "alice")
    _, bob_headers, bob, _ = member(runtime, "bobby")
    runtime.storage.save_running_reservation(running(owner))
    runtime.storage.save_resume_credentials(owner, "0000000000", "rail password")
    runtime.storage.save_dead_search(
        DeadSearch(
            chat_id=owner,
            korail_id="0000000000",
            search_params=running(owner).search_params,
            cause=DeathCause.CRASHED,
        )
    )
    runtime.storage.mark_search_logged_in(owner, 4242)
    terminated = []

    def terminate(pid):
        terminated.append(pid)
        return True

    # The user's own cancel path: the worker is killed by its PID, not left to the watchdog.
    monkeypatch.setattr(runtime.reservation, "_terminate_search_process", terminate)
    assert owner_keys(runtime, owner)
    bob_before = owner_keys(runtime, bob)

    response = http.post("/api/mobile/account/delete", headers=headers, json={"password": PASSWORD})

    assert response.status_code == 200, response.json
    assert response.json == {"deleted": True}
    assert terminated == [4242]
    # Only the non-personal end mark is left, for a deploy check that may be waiting on it.
    assert owner_keys(runtime, owner) == [f"search_ended:{owner}"]
    assert runtime.storage.get_search_endings()[owner]["reason"] == "cancelled"
    assert runtime.notifications.items(owner) == []
    with runtime.identity.connect() as db:
        assert db.execute("SELECT count(*) FROM devices WHERE owner=?", (owner,)).fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT count(*) FROM outbox o JOIN events e ON e.id=o.event_id WHERE e.owner=?",
                (owner,),
            ).fetchone()[0]
            == 0
        )
        assert db.execute("SELECT count(*) FROM users WHERE username='alice'").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT count(*) FROM sessions WHERE user_id=?", (alice["user"]["id"],)
            ).fetchone()[0]
            == 0
        )
    # The session is gone with the account, and the name can no longer log in.
    assert http.get("/api/mobile/status", headers=headers).status_code == 401
    assert (
        http.post(
            "/api/mobile/auth/login", json={"username": "alice", "password": PASSWORD}
        ).status_code
        == 401
    )
    # Bob keeps everything.
    assert owner_keys(runtime, bob) == bob_before
    assert runtime.notifications.items(bob)
    assert http.get("/api/mobile/favourites", headers=bob_headers).json["favourites"]


def test_a_wrong_password_deletes_nothing_and_is_not_a_logout(runtime):
    http, headers, owner, _ = member(runtime, "alice")
    before = owner_keys(runtime, owner)
    for password in ("wrong long passphrase", None, ""):
        response = http.post(
            "/api/mobile/account/delete", headers=headers, json={"password": password}
        )
        # 403: the app reads 401 as an expired session and would log the user out.
        assert response.status_code == 403
        assert response.json["error"] == "앱 비밀번호가 맞지 않아요."
    assert owner_keys(runtime, owner) == before
    assert http.get("/api/mobile/status", headers=headers).status_code == 200
    # Guessing is bounded by its own bucket.
    for _ in range(2):
        http.post("/api/mobile/account/delete", headers=headers, json={"password": "x" * 12})
    assert (
        http.post(
            "/api/mobile/account/delete", headers=headers, json={"password": PASSWORD}
        ).status_code
        == 429
    )


def test_a_seat_waiting_for_payment_is_refused_before_anything_changes(runtime):
    http, headers, owner, _ = member(runtime, "alice")
    runtime.storage.save_payment_status(
        PaymentStatus(
            chat_id=owner,
            completed=False,
            reminder_active=True,
            reservation_id="R1",
            expires_at=utc_now() + timedelta(minutes=5),
        )
    )
    before = owner_keys(runtime, owner)
    response = http.post("/api/mobile/account/delete", headers=headers, json={"password": PASSWORD})
    assert response.status_code == 409
    assert "결제를 기다리는 예약" in response.json["error"]
    assert owner_keys(runtime, owner) == before
    assert http.get("/api/mobile/status", headers=headers).status_code == 200


def test_the_last_admin_cannot_leave_but_one_of_two_can(runtime):
    runtime.identity.ensure_admin("root", PASSWORD)
    http = runtime.app.test_client()
    token = runtime.identity.login("root", PASSWORD)["token"]
    headers = {"Authorization": "Bearer " + token}
    refused = http.post("/api/mobile/account/delete", headers=headers, json={"password": PASSWORD})
    assert refused.status_code == 409
    assert "마지막 관리자" in refused.json["error"]
    assert http.get("/api/mobile/status", headers=headers).status_code == 200

    runtime.identity.ensure_admin("deputy", PASSWORD)
    deleted = http.post("/api/mobile/account/delete", headers=headers, json={"password": PASSWORD})
    assert deleted.status_code == 200
    assert runtime.identity.find_user("root") is None
    assert runtime.identity.find_user("deputy")["role"] == "admin"


def test_the_operator_deletes_an_account_for_an_emailed_request(runtime, monkeypatch):
    _, headers, owner, _ = member(runtime, "alice")
    runtime.storage.save_running_reservation(running(owner))
    killed = []
    monkeypatch.setattr(
        "korail_bot.mobile.process.MobileReservationService._terminate_search_process",
        lambda self, pid: killed.append(pid) or True,
    )
    monkeypatch.setattr("korail_bot.mobile.runtime.MobileRuntime", lambda config, push: runtime)
    # The CLI closes the storage it opened; this one is still the test's.
    monkeypatch.setattr(runtime.storage, "close", lambda: None)

    assert delete_account_cli(runtime.config, "ALICE").startswith("Deleted alice")

    assert killed == [4242]
    assert owner_keys(runtime, owner) == [f"search_ended:{owner}"]
    assert runtime.identity.find_user("alice") is None
    with pytest.raises(ValueError, match="No app account"):
        delete_account_cli(runtime.config, "alice")


@pytest.fixture
def pages(tmp_path):
    def build(contact):
        app = create_app(
            IdentityStore(tmp_path / f"{contact is None}.sqlite3"),
            MagicMock(),
            origins=("https://localhost",),
            privacy_contact=contact,
        )
        app.testing = True
        return app.test_client()

    return build


@pytest.mark.parametrize("path", ["/privacy", "/delete-account"])
def test_public_pages_are_cacheable_html_for_any_browser(pages, path):
    client = pages("privacy@example.com")
    # A link opened from another site carries its Origin; the API's own check must not refuse it.
    for headers in ({}, {"Origin": "https://play.google.com"}):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.mimetype == "text/html"
        assert response.headers["Content-Type"] == "text/html; charset=utf-8"
        assert response.headers["Cache-Control"] == "public, max-age=3600"
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert "Access-Control-Allow-Origin" not in response.headers
        body = response.get_data(as_text=True)
        assert 'href="mailto:privacy@example.com"' in body
        assert "설정</b>을 누릅니다" in body
        assert "웹으로 탈퇴 요청" in body
    assert client.head(path).status_code == 200
    # The API keeps its own rules.
    api = client.get("/api/mobile/health", headers={"Origin": "https://play.google.com"})
    assert api.status_code == 403
    assert api.headers["Cache-Control"] == "no-store"


def test_privacy_page_names_what_is_kept_and_who_receives_it(pages):
    body = pages("privacy@example.com").get("/privacy").get_data(as_text=True)
    for fact in (
        "scrypt",
        "코레일 아이디",
        "Firebase Cloud Messaging",
        "Cloudflare",
        "10MB씩 3개",
        "최근 50건",
        "광고",
    ):
        assert fact in body


def test_without_a_contact_the_pages_point_to_the_app(pages):
    client = pages(None)
    for path in ("/privacy", "/delete-account"):
        body = client.get(path).get_data(as_text=True)
        assert "mailto:" not in body
        assert "설정 → 회원 탈퇴" in body


def test_a_contact_is_escaped(pages):
    body = pages('a"><script>@example.com').get("/privacy").get_data(as_text=True)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_contact_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MOBILE_SECRET", "a" * 40)
    monkeypatch.setenv("MOBILE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MOBILE_PRIVACY_CONTACT", " privacy@example.com ")
    assert MobileConfig.from_env(auth_only=True).privacy_contact == "privacy@example.com"
    monkeypatch.setenv("MOBILE_PRIVACY_CONTACT", "")
    assert MobileConfig.from_env(auth_only=True).privacy_contact is None
