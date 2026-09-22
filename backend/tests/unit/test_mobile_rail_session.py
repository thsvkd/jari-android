"""One Korail login serves a user's seat-map browsing, not one per car."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis

from korail_bot.mobile import gateway as gateway_module
from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.runtime import MobileRuntime


def _runtime(tmp_path):
    return MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=fakeredis.FakeRedis(decode_responses=True),
    )


def test_seat_map_reads_share_one_korail_login(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    rails = []

    def rail_service(owner):
        rail = MagicMock()
        rail.login.return_value = True
        rails.append(rail)
        return rail

    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", rail_service)
    now = [1000.0]
    monkeypatch.setattr(gateway_module, "monotonic", lambda: now[0])
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
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="015"))
    cars = f"/api/mobile/trains/{key}/cars?seatClass=general&passengerCount=1"

    # Registration verified the password with a client of its own; the reads
    # that follow log in once and keep that session across calls.
    for _ in range(3):
        assert http.get(cars, headers=headers).status_code == 200
    assert len(rails) == 2
    assert rails[1].login.call_count == 1
    assert rails[1].seat_cars.call_count == 3

    # Korail refusing a read is what an expired session looks like from here,
    # so the session is dropped and the retry logs in afresh.
    rails[1].seat_cars.side_effect = RuntimeError("session gone")
    assert http.get(cars, headers=headers).status_code == 503
    rails[1].close.assert_called_once()
    assert http.get(cars, headers=headers).status_code == 200
    assert len(rails) == 3

    # A session older than the train keys it serves is not reused either.
    now[0] += gateway_module.RAIL_SESSION_SECONDS + 1
    assert http.get(cars, headers=headers).status_code == 200
    assert len(rails) == 4
    rails[2].close.assert_called_once()

    # Unlinking the account closes the session it was logged in with.
    assert http.post("/api/mobile/logout", headers=headers).status_code == 200
    rails[3].close.assert_called_once()
    assert runtime.gateway._rails == {}


def test_empty_firebase_placeholder_means_no_push(tmp_path, monkeypatch):
    placeholder = tmp_path / "firebase-admin.json"
    placeholder.write_text("")
    monkeypatch.setenv("MOBILE_SECRET", "a" * 40)
    monkeypatch.setenv("MOBILE_REDIS_URL", "redis://localhost:1/1")
    monkeypatch.setenv("MOBILE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MOBILE_FCM_CREDENTIALS", str(placeholder))
    assert MobileConfig.from_env().fcm_credentials is None

    placeholder.write_text("{}")
    assert MobileConfig.from_env().fcm_credentials == str(placeholder)
