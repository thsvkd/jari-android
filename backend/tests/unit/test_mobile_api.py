"""Authentication gates must apply before any booking service runs."""

from unittest.mock import MagicMock

import pytest

from korail_bot.mobile.api import create_app
from korail_bot.mobile.identity import IdentityStore


@pytest.fixture
def api(tmp_path):
    identity = IdentityStore(tmp_path / "app.sqlite3")
    gateway = MagicMock()
    gateway.bootstrap.return_value = {"running": None}
    gateway.status.return_value = {"running": None, "scheduled": None, "pending": []}
    gateway.start_search.return_value = {"started": True}
    app = create_app(identity, gateway)
    app.testing = True
    return app.test_client(), identity, gateway


def signup(api, username):
    client, identity, _ = api
    response = client.post(
        "/api/mobile/auth/register",
        json={
            "username": username,
            "password": "a long secure passphrase",
            "invite": identity.create_invite(),
        },
    )
    assert response.status_code == 200
    return response.json


def test_server_identity_is_only_identity_and_logout_revokes(api, monkeypatch):
    monkeypatch.delenv("BOTTOKEN", raising=False)
    alice = signup(api, "alice")
    bob = signup(api, "bob")
    client, identity, gateway = api
    headers = {"Authorization": "Bearer " + alice["token"]}
    response = client.post(
        "/api/mobile/search", headers=headers, json={"chatId": bob["user"]["id"]}
    )
    assert response.status_code == 400
    gateway.start_search.assert_not_called()
    response = client.post("/api/mobile/search", headers=headers, json={"conditions": {}})
    assert response.status_code == 200
    gateway.start_search.assert_called_once_with(
        identity.authenticate(alice["token"])["storage_id"], {"conditions": {}}
    )
    assert client.post("/api/mobile/auth/logout", headers=headers, json={}).status_code == 200
    assert client.get("/api/mobile/status", headers=headers).status_code == 401
    assert (
        client.get(
            "/api/mobile/status", headers={"Authorization": "Bearer " + bob["token"]}
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "path",
    [
        "bootstrap",
        "register",
        "logout",
        "trains",
        "search",
        "schedule",
        "search/cancel",
        "reservations/cancel",
        "access-request",
        "favourites",
        "notify",
        "devices",
        "invites",
    ],
)
def test_every_mutation_needs_session(api, path):
    assert api[0].post("/api/mobile/" + path, json={}).status_code == 401


def test_malformed_oversized_rate_limited_and_no_callback(api):
    client, _, _ = api
    alice = signup(api, "alice")
    headers = {"Authorization": "Bearer " + alice["token"]}
    assert client.post("/api/mobile/search", headers=headers, json=[]).status_code == 400
    assert (
        client.post("/api/mobile/search", headers=headers, json={"x": "x" * 17000}).status_code
        == 413
    )
    assert client.get("/reservation-callback").status_code == 404
    assert client.get("/check_payment").status_code == 404
    for _ in range(10):
        response = client.post(
            "/api/mobile/auth/login",
            json={"username": "alice", "password": "wrong long passphrase"},
        )
    assert response.status_code == 429


def test_railway_auth_failure_does_not_revoke_app_session(api):
    from korail_bot.services.mini_app_gateway import MiniAppError

    client, _, gateway = api
    alice = signup(api, "alice")
    headers = {"Authorization": "Bearer " + alice["token"]}
    gateway.register.side_effect = MiniAppError("철도 비밀번호를 확인해주세요.", 401)
    response = client.post(
        "/api/mobile/register",
        headers=headers,
        json={"username": "01012345678", "password": "rail password"},
    )
    assert response.status_code == 428
    assert client.get("/api/mobile/status", headers=headers).status_code == 200


def test_only_admin_can_create_invites(api):
    client, identity, _ = api
    alice = signup(api, "alice")
    forbidden = client.post(
        "/api/mobile/invites",
        headers={"Authorization": "Bearer " + alice["token"]},
        json={"ttlHours": 24},
    )
    assert forbidden.status_code == 403
    identity.ensure_admin("operator", "a long admin passphrase")
    admin = client.post(
        "/api/mobile/auth/login",
        json={
            "username": "operator",
            "password": "a long admin passphrase",
            "role": "admin",
        },
    )
    assert admin.status_code == 200
    created = client.post(
        "/api/mobile/invites",
        headers={"Authorization": "Bearer " + admin.json["token"]},
        json={"ttlHours": 24},
    )
    assert created.status_code == 200
    assert len(created.json["invite"]) >= 16
    guest = client.post(
        "/api/mobile/auth/register",
        json={
            "username": "chosen",
            "password": "a long secure passphrase",
            "invite": created.json["invite"],
        },
    )
    assert guest.status_code == 200
    assert guest.json["user"]["role"] == "member"


def test_overly_nested_payload_is_a_client_error(api):
    client, _, gateway = api
    alice = signup(api, "alice")
    headers = {"Authorization": "Bearer " + alice["token"]}
    payload = {}
    for _ in range(40):
        payload = {"nested": payload}
    assert client.post("/api/mobile/search", headers=headers, json=payload).status_code == 400
    gateway.start_search.assert_not_called()
