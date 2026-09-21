"""Authentication gates must apply before any booking service runs."""

import json
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
        "reservations/designated",
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
    # The body limit is what bounds a seat plan, so the refusal has to say
    # which choice was too big rather than "요청 내용을 확인해 주세요".
    oversized = client.post(
        "/api/mobile/search", headers=headers, json={"x": "x" * (2 * 1024 * 1024)}
    )
    assert oversized.status_code == 413
    assert "좌석" in oversized.json["error"]
    assert client.get("/reservation-callback").status_code == 404
    assert client.get("/check_payment").status_code == 404
    for _ in range(10):
        response = client.post(
            "/api/mobile/auth/login",
            json={"username": "alice", "password": "wrong long passphrase"},
        )
    assert response.status_code == 429


def test_tunnel_clients_and_seat_maps_have_their_own_limits(api):
    client, _, gateway = api

    def login(ip, username):
        return client.post(
            "/api/mobile/auth/login",
            headers={"CF-Connecting-IP": ip},
            json={"username": username, "password": "wrong long passphrase"},
        ).status_code

    for index in range(10):
        login("203.0.113.1", f"user{index}")
    assert login("203.0.113.1", "user10") == 429
    assert login("203.0.113.2", "someone") != 429

    gateway.seat_cars.return_value = {"cars": []}
    alice = signup(api, "alice")
    headers = {"Authorization": "Bearer " + alice["token"]}
    statuses = [
        client.get("/api/mobile/trains/T1/cars", headers=headers).status_code for _ in range(11)
    ]
    assert statuses == [200] * 10 + [429]


def test_reading_a_whole_formation_costs_one_rail_slot(api):
    # The per-car route spends a slot per car and runs out partway through a
    # long train. This one reads every car for the price of a single call.
    client, identity, gateway = api
    gateway.seat_inventories.return_value = {
        "inventories": [],
        "failedCars": [],
        "layoutReference": False,
    }
    alice = signup(api, "alice")
    headers = {"Authorization": "Bearer " + alice["token"]}

    statuses = [
        client.get(
            "/api/mobile/trains/T1/seats?seatClass=general&passengerCount=2", headers=headers
        ).status_code
        for _ in range(11)
    ]

    assert statuses == [200] * 10 + [429]
    gateway.seat_inventories.assert_called_with(
        identity.authenticate(alice["token"])["storage_id"], "T1", "general", "2"
    )


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


def test_upstream_failure_keeps_its_message_and_cors_headers(tmp_path):
    from korail_bot.services.mini_app_gateway import MiniAppError

    identity = IdentityStore(tmp_path / "app.sqlite3")
    gateway = MagicMock()
    message = "호차 정보를 불러오지 못했어요. 잠시 후 다시 시도해 주세요."
    gateway.seat_cars.side_effect = MiniAppError(message, 502)
    app = create_app(identity, gateway, origins=("https://localhost",))
    app.testing = True
    client = app.test_client()
    alice = signup((client, identity, gateway), "alice")
    response = client.get(
        "/api/mobile/trains/T1/cars",
        headers={"Authorization": "Bearer " + alice["token"], "Origin": "https://localhost"},
    )
    # Cloudflare replaces an origin 502 with its own body and no CORS headers.
    assert response.status_code == 503
    assert response.json == {"error": message}
    assert response.headers["Access-Control-Allow-Origin"] == "https://localhost"


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


def test_a_whole_car_seat_plan_fits_inside_the_body_limit(api):
    # The body limit is the only thing bounding a seat plan, so a plan the app
    # can really produce - "apply to every car" over a long formation - has to
    # arrive intact rather than as a 413.
    client, identity, gateway = api
    alice = signup(api, "alice")
    headers = {"Authorization": "Bearer " + alice["token"]}
    targets = [
        {
            "carNo": 1 + index // 60,
            "seatNo": f"{index:06d}",
            "label": f"{index % 60 + 1}",
            "row": index % 60 // 4 + 1,
            "column": "ABCD"[index % 4],
            "direction": "forward",
            "floor": "1",
            "adjacencyGroup": f"{index % 60 // 4 + 1}:left",
            "position": index % 4 + 1,
        }
        for index in range(900)
    ]
    payload = {
        "conditions": {
            "seat_plan": {
                "strategy": "independent",
                "passengerCount": 1,
                "trains": [{"trainNo": "015", "seatClass": "general", "targets": targets}],
            }
        }
    }

    response = client.post("/api/mobile/search", headers=headers, json=payload)

    # Well past the 16KiB the limit used to be, so this fails if it goes back.
    assert len(json.dumps(payload)) > 100_000
    assert response.status_code == 200
    gateway.start_search.assert_called_once_with(
        identity.authenticate(alice["token"])["storage_id"], payload
    )


def test_overly_nested_payload_is_a_client_error(api):
    client, _, gateway = api
    alice = signup(api, "alice")
    headers = {"Authorization": "Bearer " + alice["token"]}
    payload = {}
    for _ in range(40):
        payload = {"nested": payload}
    assert client.post("/api/mobile/search", headers=headers, json=payload).status_code == 400
    gateway.start_search.assert_not_called()


def test_seat_routes_use_authenticated_owner_and_gateway(api):
    client, identity, gateway = api
    alice = signup(api, "alice")
    owner = identity.authenticate(alice["token"])["storage_id"]
    headers = {"Authorization": "Bearer " + alice["token"]}
    gateway.seat_cars.return_value = {"cars": []}
    gateway.seat_inventory.return_value = {"seats": []}
    gateway.reserve_designated.return_value = {"reserved": True}

    assert client.get(
        "/api/mobile/trains/key-1/cars?seatClass=general&passengerCount=2",
        headers=headers,
    ).json == {"cars": []}
    gateway.seat_cars.assert_called_once_with(owner, "key-1", "general", "2")

    assert client.get(
        "/api/mobile/trains/key-1/cars/3/seats?seatClass=special&passengerCount=1",
        headers=headers,
    ).json == {"seats": []}
    gateway.seat_inventory.assert_called_once_with(owner, "key-1", 3, "special", "1")

    payload = {"trainKey": "key-1", "seatClass": "general", "passengerCount": 1}
    assert client.post(
        "/api/mobile/reservations/designated", headers=headers, json=payload
    ).json == {"reserved": True}
    gateway.reserve_designated.assert_called_once_with(owner, payload)
