"""Google login links an approved account explicitly and never exposes an app token in URLs."""

import base64
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from korail_bot.mobile.api import create_app
from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.google_auth import GoogleTokenError, verify_id_token
from korail_bot.mobile.identity import AuthError, IdentityStore, digest

PASSWORD = "eight888"
CLIENT_ID = "client.apps.googleusercontent.com"
CLIENT_SECRET = "server-secret"
PUBLIC_URL = "https://jari.example"


def b64(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def signed_token(private_key, **overrides):
    now = int(time.time())
    header = {"alg": "RS256", "kid": "test-key", "typ": "JWT"}
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "nonce": "expected-nonce",
        "sub": "google-subject-1",
        "email": "must-not-be-used@example.com",
        **overrides,
    }
    signing_input = f"{b64(header)}.{b64(claims)}".encode()
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{b64(signature)}"


@pytest.fixture
def google_key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "kid": "test-key",
                "n": b64(public.n.to_bytes((public.n.bit_length() + 7) // 8, "big")),
                "e": b64(public.e.to_bytes((public.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }
    return private, jwks


def make_env(tmp_path, *, enabled=True, clock=time.time):
    identity = IdentityStore(tmp_path / "identity.sqlite3", clock=clock)
    app = create_app(
        identity,
        MagicMock(),
        public_url=PUBLIC_URL,
        google_client_id=CLIENT_ID if enabled else None,
        google_client_secret=CLIENT_SECRET if enabled else None,
    )
    app.testing = True
    return identity, app.test_client()


def register(identity, client, username="alice"):
    session = identity.register(username, PASSWORD)
    identity.set_approval(session["user"]["id"], "approved")
    return session, {"Authorization": "Bearer " + session["token"]}


def callback(client, start, monkeypatch, *, subject="google-subject-1"):
    query = parse_qs(urlsplit(start["authorizationUrl"]).query)
    monkeypatch.setattr(
        "korail_bot.mobile.google_auth.exchange_code",
        lambda **kwargs: {"sub": subject, "nonce": query["nonce"][0]},
    )
    return client.get(
        "/api/mobile/auth/google/callback",
        query_string={"state": query["state"][0], "code": "provider-code"},
        base_url=PUBLIC_URL,
    )


def confirmation_token(response):
    match = re.search(r'name="confirmation" value="([A-Za-z0-9_-]+)"', response.text)
    assert match
    return match.group(1)


def complete_callback(client, start, monkeypatch, *, subject="google-subject-1"):
    response = callback(client, start, monkeypatch, subject=subject)
    assert response.status_code == 200
    return client.post(
        "/api/mobile/auth/google/confirm",
        data={
            "confirmation": confirmation_token(response),
            "verificationCode": start["verificationCode"],
            "decision": "allow",
        },
        base_url=PUBLIC_URL,
    )


def test_google_config_is_disabled_or_rejects_partial_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MOBILE_SECRET", "s" * 40)
    monkeypatch.setenv("MOBILE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MOBILE_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("MOBILE_GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("MOBILE_PUBLIC_URL", raising=False)
    assert MobileConfig.from_env(auth_only=True).google_enabled is False

    monkeypatch.setenv("MOBILE_GOOGLE_CLIENT_ID", CLIENT_ID)
    with pytest.raises(ValueError, match="Google login requires"):
        MobileConfig.from_env(auth_only=True)


def test_google_routes_report_disabled_and_refuse_start(tmp_path):
    _, client = make_env(tmp_path, enabled=False)
    assert client.get("/api/mobile/auth/google").json == {"enabled": False}
    assert client.post("/api/mobile/auth/google/start").status_code == 503


def test_google_login_never_auto_links_email_or_creates_an_account(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    register(identity, client)
    start = client.post("/api/mobile/auth/google/start").json
    completed = callback(client, start, monkeypatch)
    assert completed.status_code == 403
    poll = client.post("/api/mobile/auth/google/poll", json={"ticket": start["ticket"]})
    assert poll.status_code == 403
    assert identity.google_linked("google-subject-1") is None
    assert identity.find_user("must-not-be-used") is None


def test_live_approved_session_links_without_password(tmp_path):
    identity, client = make_env(tmp_path)
    _, headers = register(identity, client)
    assert client.get("/api/mobile/account/google", headers=headers).json == {
        "enabled": True,
        "linked": False,
        "reauthenticationRequired": False,
        "signedInWithGoogle": False,
    }
    linked = client.post("/api/mobile/account/google/link", json={}, headers=headers)
    assert linked.status_code == 200
    assert linked.json["expiresIn"] == 300
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", linked.json["verificationCode"])
    query = parse_qs(urlsplit(linked.json["authorizationUrl"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    assert "code_verifier" not in query


@pytest.mark.parametrize("authenticated_at", [None, 399.999, 1_601.0])
def test_live_approved_session_links_without_password_regardless_of_authentication_age(
    tmp_path, authenticated_at
):
    now = [1_000.0]
    identity, client = make_env(tmp_path, clock=lambda: now[0])
    _, headers = register(identity, client)
    with identity.connect() as db:
        db.execute(
            "UPDATE sessions SET authenticated_at=? WHERE hash=?",
            (authenticated_at, digest(headers["Authorization"].removeprefix("Bearer "))),
        )
    linked = client.post("/api/mobile/account/google/link", json={}, headers=headers)
    assert linked.status_code == 200


def test_legacy_live_approved_session_links_without_password(tmp_path):
    path = tmp_path / "identity.sqlite3"
    token = "legacy-session-" + "x" * 32
    db = sqlite3.connect(path)
    try:
        db.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,"
            " password_hash TEXT NOT NULL, storage_id INTEGER UNIQUE NOT NULL,"
            " role TEXT NOT NULL DEFAULT 'member', status TEXT NOT NULL DEFAULT 'approved',"
            " application_note TEXT, created REAL)"
        )
        db.execute(
            "INSERT INTO users VALUES ('old', 'alice', 'unused', -1, 'member', 'approved', NULL, 1)"
        )
        db.execute(
            "CREATE TABLE sessions (hash TEXT PRIMARY KEY, user_id TEXT NOT NULL"
            " REFERENCES users(id), expires REAL NOT NULL)"
        )
        db.execute("INSERT INTO sessions VALUES (?, 'old', 2000)", (digest(token),))
        db.commit()
    finally:
        db.close()

    identity = IdentityStore(path, clock=lambda: 1_000.0)
    app = create_app(
        identity,
        MagicMock(),
        public_url=PUBLIC_URL,
        google_client_id=CLIENT_ID,
        google_client_secret=CLIENT_SECRET,
    )
    app.testing = True
    response = app.test_client().post(
        "/api/mobile/account/google/link",
        json={},
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200
    assert response.json["expiresIn"] == 300
    with identity.connect() as db:
        row = db.execute("SELECT authenticated_at, via_google FROM sessions").fetchone()
    assert row["authenticated_at"] is None
    # Without Google linked, an older session is a password sign-in; unlinking from it keeps it.
    assert row["via_google"] == 0


def test_older_sessions_of_a_google_linked_account_count_as_google_sessions(tmp_path):
    # Nothing recorded how they began, so the owner's unlink must not leave one of them signed in.
    identity, client = make_env(tmp_path)
    linked, _ = register(identity, client)
    plain, _ = register(identity, client, username="bobby")
    identity.google_link(linked["user"]["id"], "google-subject")
    with identity.connect() as db:
        db.execute("ALTER TABLE sessions DROP COLUMN via_google")
    reopened = IdentityStore(identity.path)
    assert reopened.session_via_google(linked["token"])
    assert not reopened.session_via_google(plain["token"])


def test_expired_revoked_pending_or_rejected_session_cannot_start_account_link(tmp_path):
    identity, client = make_env(tmp_path)
    expired, expired_headers = register(identity, client, "expired")
    revoked, revoked_headers = register(identity, client, "revoked")
    pending = identity.register("pending", PASSWORD)
    pending_headers = {"Authorization": "Bearer " + pending["token"]}
    rejected = identity.register("rejected", PASSWORD)
    identity.set_approval(rejected["user"]["id"], "rejected")
    rejected_headers = {"Authorization": "Bearer " + rejected["token"]}
    with identity.connect() as db:
        db.execute(
            "UPDATE sessions SET expires=? WHERE hash=?",
            (identity.clock() - 1, digest(expired["token"])),
        )
    identity.revoke(revoked["token"])

    assert (
        client.post("/api/mobile/account/google/link", json={}, headers=expired_headers).status_code
        == 401
    )
    assert (
        client.post("/api/mobile/account/google/link", json={}, headers=revoked_headers).status_code
        == 401
    )
    for headers in (pending_headers, rejected_headers):
        response = client.post("/api/mobile/account/google/link", json={}, headers=headers)
        assert response.status_code == 403
        assert response.json["code"] == "approval_required"


def test_link_then_login_creates_session_only_when_ticket_is_polled(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    session, headers = register(identity, client)
    start = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    assert complete_callback(client, start, monkeypatch).status_code == 200
    with identity.connect() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM google_identities").fetchone()[0] == 0
    assert client.post("/api/mobile/auth/google/poll", json={"ticket": start["ticket"]}).json == {
        "status": "complete",
        "linked": True,
    }
    assert client.get("/api/mobile/account/google", headers=headers).json == {
        "enabled": True,
        "linked": True,
        "reauthenticationRequired": False,
        "signedInWithGoogle": False,
    }
    identity.revoke(session["token"])

    login = client.post("/api/mobile/auth/google/start").json
    assert complete_callback(client, login, monkeypatch).status_code == 200
    with identity.connect() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    result = client.post("/api/mobile/auth/google/poll", json={"ticket": login["ticket"]})
    assert result.status_code == 200
    assert result.json["status"] == "complete"
    assert result.json["auth"]["user"]["id"] == session["user"]["id"]
    google_headers = {"Authorization": "Bearer " + result.json["auth"]["token"]}
    assert client.get("/api/mobile/account/google", headers=google_headers).json == {
        "enabled": True,
        "linked": True,
        "reauthenticationRequired": False,
        "signedInWithGoogle": True,
    }


def test_recent_authorization_remains_valid_for_the_five_minute_link_flow(tmp_path, monkeypatch):
    now = [1_000.0]
    identity, client = make_env(tmp_path, clock=lambda: now[0])
    _, headers = register(identity, client)
    now[0] = 1_599.0
    start = client.post("/api/mobile/account/google/link", json={}, headers=headers).json

    now[0] = 1_600.0
    assert complete_callback(client, start, monkeypatch).status_code == 200
    result = client.post("/api/mobile/auth/google/poll", json={"ticket": start["ticket"]})
    assert result.json == {"status": "complete", "linked": True}


def test_expired_link_session_cannot_link_at_callback_or_poll(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    session, headers = register(identity, client)
    start = client.post("/api/mobile/account/google/link", json={}, headers=headers).json
    identity.revoke(session["token"])
    assert callback(client, start, monkeypatch).status_code == 403
    assert (
        client.post("/api/mobile/auth/google/poll", json={"ticket": start["ticket"]}).status_code
        == 403
    )
    assert identity.google_linked("google-subject-1") is None


def test_link_session_revoked_during_provider_exchange_is_rechecked_at_poll(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    session, headers = register(identity, client)
    start = client.post("/api/mobile/account/google/link", json={}, headers=headers).json
    query = parse_qs(urlsplit(start["authorizationUrl"]).query)

    def exchange(**kwargs):
        identity.revoke(session["token"])
        return {"sub": "google-subject-1", "nonce": query["nonce"][0]}

    monkeypatch.setattr("korail_bot.mobile.google_auth.exchange_code", exchange)
    callback_response = client.get(
        "/api/mobile/auth/google/callback",
        query_string={"state": query["state"][0], "code": "provider-code"},
        base_url=PUBLIC_URL,
    )
    assert callback_response.status_code == 403
    assert (
        client.post("/api/mobile/auth/google/poll", json={"ticket": start["ticket"]}).status_code
        == 403
    )
    assert identity.google_linked("google-subject-1") is None


def test_state_and_ticket_are_flow_bound_one_time_and_poll_consumes_once(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    _, headers = register(identity, client)
    first = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    second = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    first_query = parse_qs(urlsplit(first["authorizationUrl"]).query)
    monkeypatch.setattr(
        "korail_bot.mobile.google_auth.exchange_code",
        MagicMock(side_effect=GoogleTokenError("invalid provider response")),
    )
    assert (
        client.get(
            "/api/mobile/auth/google/callback",
            query_string={"state": first_query["state"][0], "code": "one"},
        ).status_code
        >= 400
    )
    assert (
        client.get(
            "/api/mobile/auth/google/callback",
            query_string={"state": first_query["state"][0], "code": "two"},
        ).status_code
        >= 400
    )
    swapped = first["ticket"].split(".")[0] + "." + second["ticket"].split(".")[1]
    assert client.post("/api/mobile/auth/google/poll", json={"ticket": swapped}).status_code >= 400


def test_only_one_concurrent_poll_can_consume_a_completed_login(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    _, headers = register(identity, client)
    link = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    assert complete_callback(client, link, monkeypatch).status_code == 200
    assert (
        client.post("/api/mobile/auth/google/poll", json={"ticket": link["ticket"]}).status_code
        == 200
    )
    login = client.post("/api/mobile/auth/google/start").json
    assert complete_callback(client, login, monkeypatch).status_code == 200

    def poll():
        with client.application.test_client() as thread_client:
            return thread_client.post(
                "/api/mobile/auth/google/poll", json={"ticket": login["ticket"]}
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _: poll(), range(2)))
    assert statuses == [200, 400]


def test_unlink_needs_only_the_session_and_account_deletion_cascades_google_identity(
    tmp_path, monkeypatch
):
    identity, client = make_env(tmp_path)
    session, headers = register(identity, client)
    link = client.post("/api/mobile/account/google/link", json={}, headers=headers).json
    complete_callback(client, link, monkeypatch)
    client.post("/api/mobile/auth/google/poll", json={"ticket": link["ticket"]})
    assert client.post("/api/mobile/account/google/unlink", json={}).status_code == 401
    assert identity.google_status(session["user"]["id"])
    unfinished = client.post("/api/mobile/account/google/link", json={}, headers=headers).json
    assert client.post("/api/mobile/account/google/unlink", json={}, headers=headers).json == {
        "linked": False,
        "signedOut": False,
    }
    unfinished_query = parse_qs(urlsplit(unfinished["authorizationUrl"]).query)
    assert (
        client.get(
            "/api/mobile/auth/google/callback",
            query_string={"state": unfinished_query["state"][0], "code": "late-code"},
        ).status_code
        == 400
    )
    identity.google_link(session["user"]["id"], "replacement-subject")
    identity.delete_user(session["user"]["id"], "alice")
    assert identity.google_linked("replacement-subject") is None


def test_real_rs256_verification_rejects_tampering_and_claim_mismatch(google_key, monkeypatch):
    private, jwks = google_key

    class Reply:
        def raise_for_status(self):
            return None

        def json(self):
            return jwks

    monkeypatch.setattr("korail_bot.mobile.google_auth.requests.get", lambda *a, **k: Reply())
    assert (
        verify_id_token(signed_token(private), CLIENT_ID, "expected-nonce", now=time.time())["sub"]
        == "google-subject-1"
    )
    for changes in (
        {"aud": "another-client"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 1},
        {"exp": True},
        {"exp": float("nan")},
        {"iat": int(time.time()) + 120},
        {"azp": "another-client"},
        {"nonce": "another-nonce"},
    ):
        with pytest.raises(GoogleTokenError):
            verify_id_token(signed_token(private, **changes), CLIENT_ID, "expected-nonce")
    token = signed_token(private)
    parts = token.split(".")
    signature = bytearray(base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4)))
    signature[0] ^= 1
    tampered = ".".join((parts[0], parts[1], b64(bytes(signature))))
    with pytest.raises(GoogleTokenError):
        verify_id_token(tampered, CLIENT_ID, "expected-nonce")


def test_callback_page_contains_no_app_bearer_or_ticket(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    _, headers = register(identity, client)
    start = client.post(
        "/api/mobile/account/google/link",
        json={"password": PASSWORD},
        headers={
            **headers,
            "User-Agent": "Jari Android/1.0",
            "CF-Connecting-IP": "203.0.113.9",
        },
    ).json
    response = callback(client, start, monkeypatch)
    html = response.get_data(as_text=True)
    assert start["ticket"] not in html
    assert start["verificationCode"] not in html
    assert "Bearer" not in html
    assert "alice 앱 계정" in html
    assert "앱으로 돌아가" in html
    assert 'type="password"' in html
    assert "Android 앱" in html
    assert "203.0.113.9" in html
    assert "계정과 예약 정보" in html
    assert "이 앱의 로그인 허용" in html
    assert "거부" in html
    assert '<meta name="viewport"' in html
    assert "<main>" in html
    assert "min-height: 48px" in html
    assert "style-src 'nonce-" in response.headers["Content-Security-Policy"]
    assert re.search(r'<style nonce="[A-Za-z0-9_-]+">', html)
    database = Path(identity.path).read_bytes()
    assert start["verificationCode"].replace("-", "").encode() not in database
    assert confirmation_token(response).encode() not in database
    cookie_value = re.search(r"=[^;]+", response.headers["Set-Cookie"]).group()[1:]
    assert cookie_value.encode() not in database


def test_one_google_subject_cannot_be_linked_to_two_app_accounts(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    _, alice_headers = register(identity, client)
    alice = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=alice_headers
    ).json
    complete_callback(client, alice, monkeypatch, subject="shared-subject")
    assert (
        client.post("/api/mobile/auth/google/poll", json={"ticket": alice["ticket"]}).status_code
        == 200
    )

    _, bob_headers = register(identity, client, "bobby")
    bob = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=bob_headers
    ).json
    complete_callback(client, bob, monkeypatch, subject="shared-subject")
    assert (
        client.post("/api/mobile/auth/google/poll", json={"ticket": bob["ticket"]}).status_code
        == 409
    )


def test_browser_and_initiating_app_must_both_confirm_before_poll_can_finish(tmp_path, monkeypatch):
    identity, attacker = make_env(tmp_path)
    _, headers = register(identity, attacker)
    linked = attacker.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    complete_callback(attacker, linked, monkeypatch)
    attacker.post("/api/mobile/auth/google/poll", json={"ticket": linked["ticket"]})
    identity.revoke_sessions(identity.find_user("alice")["id"])

    start = attacker.post("/api/mobile/auth/google/start").json
    with attacker.application.test_client() as victim:
        callback_response = callback(victim, start, monkeypatch)
        hidden = confirmation_token(callback_response)

        assert (
            attacker.post(
                "/api/mobile/auth/google/confirm",
                data={"confirmation": hidden, "verificationCode": start["verificationCode"]},
                base_url=PUBLIC_URL,
            ).status_code
            == 400
        )
        assert (
            attacker.post(
                "/api/mobile/auth/google/confirm",
                data={
                    "confirmation": start["ticket"],
                    "verificationCode": start["verificationCode"],
                },
                base_url=PUBLIC_URL,
            ).status_code
            == 400
        )
        assert attacker.post(
            "/api/mobile/auth/google/poll", json={"ticket": start["ticket"]}
        ).json == {"status": "pending"}
        with identity.connect() as db:
            assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0

        wrong = victim.post(
            "/api/mobile/auth/google/confirm",
            data={
                "confirmation": hidden,
                "verificationCode": "AAAA-AAAA",
                "decision": "allow",
            },
            base_url=PUBLIC_URL,
        )
        assert wrong.status_code == 403
        assert attacker.post(
            "/api/mobile/auth/google/poll", json={"ticket": start["ticket"]}
        ).json == {"status": "pending"}
        with identity.connect() as db:
            assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0

        finished = victim.post(
            "/api/mobile/auth/google/confirm",
            data={
                "confirmation": hidden,
                "verificationCode": start["verificationCode"],
                "decision": "allow",
            },
            base_url=PUBLIC_URL,
        )
        assert finished.status_code == 200
        assert (
            victim.post(
                "/api/mobile/auth/google/confirm",
                data={
                    "confirmation": hidden,
                    "verificationCode": start["verificationCode"],
                    "decision": "allow",
                },
                base_url=PUBLIC_URL,
            ).status_code
            == 400
        )

    completed = attacker.post("/api/mobile/auth/google/poll", json={"ticket": start["ticket"]})
    assert completed.status_code == 200
    assert completed.json["status"] == "complete"


def test_browser_can_explicitly_deny_without_creating_a_session(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    local, headers = register(identity, client)
    link = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    complete_callback(client, link, monkeypatch)
    client.post("/api/mobile/auth/google/poll", json={"ticket": link["ticket"]})
    with identity.connect() as db:
        before = db.execute("SELECT count(*) FROM sessions").fetchone()[0]

    start = client.post("/api/mobile/auth/google/start").json
    response = callback(client, start, monkeypatch)
    denied = client.post(
        "/api/mobile/auth/google/confirm",
        data={"confirmation": confirmation_token(response), "decision": "deny"},
        base_url=PUBLIC_URL,
    )
    assert denied.status_code == 200
    assert "거부" in denied.text
    with identity.connect() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == before
    assert (
        client.post("/api/mobile/auth/google/poll", json={"ticket": start["ticket"]}).status_code
        == 400
    )
    assert identity.authenticate(local["token"])["id"] == local["user"]["id"]


def test_cancel_requires_ticket_proof_and_invalidates_browser_confirmation(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    _, headers = register(identity, client)
    start = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    callback_response = callback(client, start, monkeypatch)
    hidden = confirmation_token(callback_response)
    selector, proof = start["ticket"].split(".")
    wrong = selector + "." + ("A" if proof[0] != "A" else "B") + proof[1:]
    assert client.post("/api/mobile/auth/google/cancel", json={"ticket": wrong}).status_code == 400
    assert client.post("/api/mobile/auth/google/cancel", json={"ticket": start["ticket"]}).json == {
        "cancelled": True
    }
    assert (
        client.post(
            "/api/mobile/auth/google/confirm",
            data={"confirmation": hidden, "verificationCode": start["verificationCode"]},
            base_url=PUBLIC_URL,
        ).status_code
        == 400
    )


def test_cancel_rolls_back_a_delivered_session_and_link(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    local, headers = register(identity, client)
    link = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    complete_callback(client, link, monkeypatch)
    assert (
        client.post("/api/mobile/auth/google/poll", json={"ticket": link["ticket"]}).status_code
        == 200
    )
    assert identity.google_status(local["user"]["id"])
    assert client.post("/api/mobile/auth/google/cancel", json={"ticket": link["ticket"]}).json == {
        "cancelled": True
    }
    assert not identity.google_status(local["user"]["id"])

    identity.google_link(local["user"]["id"], "google-subject-1")
    login = client.post("/api/mobile/auth/google/start").json
    complete_callback(client, login, monkeypatch)
    delivered = client.post("/api/mobile/auth/google/poll", json={"ticket": login["ticket"]}).json
    google_token = delivered["auth"]["token"]
    assert identity.authenticate(google_token)["id"] == local["user"]["id"]
    assert (
        client.post("/api/mobile/auth/google/cancel", json={"ticket": login["ticket"]}).status_code
        == 200
    )
    with pytest.raises(AuthError):
        identity.authenticate(google_token)


def test_cancel_is_idempotent_after_a_lost_response(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    local, headers = register(identity, client)
    link = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    complete_callback(client, link, monkeypatch)
    client.post("/api/mobile/auth/google/poll", json={"ticket": link["ticket"]})

    assert (
        client.post("/api/mobile/auth/google/cancel", json={"ticket": link["ticket"]}).status_code
        == 200
    )
    identity.google_link(local["user"]["id"], "google-subject-1")
    assert client.post("/api/mobile/auth/google/cancel", json={"ticket": link["ticket"]}).json == {
        "cancelled": True
    }
    assert identity.google_status(local["user"]["id"])
    assert (
        client.post("/api/mobile/auth/google/poll", json={"ticket": link["ticket"]}).status_code
        == 403
    )


def test_acknowledge_clears_delivered_receipt_without_rolling_back(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    local, headers = register(identity, client)
    link = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    complete_callback(client, link, monkeypatch)
    assert (
        client.post("/api/mobile/auth/google/poll", json={"ticket": link["ticket"]}).status_code
        == 200
    )
    assert client.post("/api/mobile/auth/google/ack", json={"ticket": link["ticket"]}).json == {
        "acknowledged": True
    }
    assert (
        client.post("/api/mobile/auth/google/cancel", json={"ticket": link["ticket"]}).status_code
        == 400
    )
    assert identity.google_status(local["user"]["id"])


def test_expired_delivered_link_receipt_cannot_remove_a_later_link(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    local, headers = register(identity, client)
    link = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    complete_callback(client, link, monkeypatch)
    assert (
        client.post("/api/mobile/auth/google/poll", json={"ticket": link["ticket"]}).status_code
        == 200
    )
    with identity.connect() as db:
        db.execute("DELETE FROM google_identities WHERE user_id=?", (local["user"]["id"],))
    identity.google_link(local["user"]["id"], "google-subject-1")
    identity.clock = lambda: time.time() + 301

    assert client.post("/api/mobile/auth/google/cancel", json={"ticket": link["ticket"]}).json == {
        "cancelled": True
    }
    assert identity.google_status(local["user"]["id"])


def test_unlink_revokes_other_sessions_but_keeps_the_caller(tmp_path, monkeypatch):
    now = [1_000.0]
    identity, client = make_env(tmp_path, clock=lambda: now[0])
    local, headers = register(identity, client)
    link = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    complete_callback(client, link, monkeypatch)
    client.post("/api/mobile/auth/google/poll", json={"ticket": link["ticket"]})
    login = client.post("/api/mobile/auth/google/start").json
    complete_callback(client, login, monkeypatch)
    google = client.post("/api/mobile/auth/google/poll", json={"ticket": login["ticket"]}).json[
        "auth"
    ]

    now[0] = 1_601.0
    # A 4.20 or older app still sends the app password, even a wrong one; it is not checked any more.
    assert client.post(
        "/api/mobile/account/google/unlink", json={"password": "wrong-pass"}, headers=headers
    ).json == {"linked": False, "signedOut": False}
    assert identity.authenticate(local["token"])["id"] == local["user"]["id"]
    with pytest.raises(AuthError):
        identity.authenticate(google["token"])


def test_unlink_from_a_google_session_signs_that_session_out_too(tmp_path, monkeypatch):
    # Whoever holds the Google account must not unlink first and keep their own way in
    # while every other phone of the owner is signed out.
    identity, client = make_env(tmp_path)
    local, headers = register(identity, client)
    link = client.post("/api/mobile/account/google/link", json={}, headers=headers).json
    complete_callback(client, link, monkeypatch)
    client.post("/api/mobile/auth/google/poll", json={"ticket": link["ticket"]})
    login = client.post("/api/mobile/auth/google/start").json
    complete_callback(client, login, monkeypatch)
    google = client.post("/api/mobile/auth/google/poll", json={"ticket": login["ticket"]}).json[
        "auth"
    ]
    google_headers = {"Authorization": "Bearer " + google["token"]}
    assert client.get("/api/mobile/account/google", headers=google_headers).json[
        "signedInWithGoogle"
    ]
    assert not client.get("/api/mobile/account/google", headers=headers).json["signedInWithGoogle"]

    assert client.post(
        "/api/mobile/account/google/unlink", json={}, headers=google_headers
    ).json == {"linked": False, "signedOut": True}
    assert not identity.google_status(local["user"]["id"])
    for token in (google["token"], local["token"]):
        with pytest.raises(AuthError):
            identity.authenticate(token)


def test_unlink_with_nothing_linked_signs_no_one_out(tmp_path):
    identity, client = make_env(tmp_path)
    local, headers = register(identity, client)
    other = identity.login(local["user"]["username"], PASSWORD)
    assert client.post("/api/mobile/account/google/unlink", json={}, headers=headers).json == {
        "linked": False,
        "signedOut": False,
    }
    assert identity.authenticate(other["token"])["id"] == local["user"]["id"]
    assert identity.authenticate(local["token"])["id"] == local["user"]["id"]


def test_browser_confirmation_allows_at_most_five_code_guesses(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    _, headers = register(identity, client)
    start = client.post(
        "/api/mobile/account/google/link", json={"password": PASSWORD}, headers=headers
    ).json
    response = callback(client, start, monkeypatch)
    hidden = confirmation_token(response)
    statuses = [
        client.post(
            "/api/mobile/auth/google/confirm",
            data={
                "confirmation": hidden,
                "verificationCode": f"AAAA-AAA{letter}",
                "decision": "allow",
            },
            base_url=PUBLIC_URL,
        ).status_code
        for letter in "ABCDE"
    ]
    assert statuses == [403, 403, 403, 403, 429]
    assert (
        client.post(
            "/api/mobile/auth/google/confirm",
            data={
                "confirmation": hidden,
                "verificationCode": start["verificationCode"],
                "decision": "allow",
            },
            base_url=PUBLIC_URL,
        ).status_code
        == 400
    )


def handoff_from(response):
    match = re.search(
        r'href="intent://google-auth\?handoff=([A-Za-z0-9_-]+)#Intent;([^"]+)"', response.text
    )
    assert match
    return match.group(1), match.group(2)


def test_app_return_flow_has_no_code_and_finishes_only_with_the_browser_handoff(
    tmp_path, monkeypatch
):
    identity, client = make_env(tmp_path)
    session, headers = register(identity, client)
    identity.google_link(session["user"]["id"], "google-subject-1")

    start = client.post("/api/mobile/auth/google/start", json={"return": "app"}).json
    assert "verificationCode" not in start
    page = callback(client, start, monkeypatch)
    assert page.status_code == 200
    assert "확인 코드" not in page.text and "<form" not in page.text
    assert "alice 계정으로 앱 로그인을 마무리해요" in page.text
    assert page.headers["Cache-Control"] == "no-store"
    handoff, intent = handoff_from(page)
    # Only this package may receive the handoff; a phone without the app lands on a plain page.
    assert "scheme=dev.thsvkd.jari;package=dev.thsvkd.jari;" in intent
    assert (
        "S.browser_fallback_url=https%3A%2F%2Fjari.example%2Fapi%2Fmobile%2Fauth%2Fgoogle%2Fapp-missing"
        in intent
    )
    assert (
        f'http-equiv="refresh" content="0;url=intent://google-auth?handoff={handoff}#Intent;'
        in page.text
    )
    assert session["token"] not in page.text and start["ticket"] not in page.text

    # The app that started the flow, polling with its ticket alone, keeps waiting.
    assert client.post("/api/mobile/auth/google/poll", json={"ticket": start["ticket"]}).json == {
        "status": "pending"
    }
    done = client.post(
        "/api/mobile/auth/google/poll", json={"ticket": start["ticket"], "handoff": handoff}
    )
    assert done.status_code == 200
    assert done.json["status"] == "complete"
    assert identity.authenticate(done.json["auth"]["token"])["id"] == session["user"]["id"]
    again = client.post(
        "/api/mobile/auth/google/poll", json={"ticket": start["ticket"], "handoff": handoff}
    )
    assert again.status_code == 400


def test_sent_sign_in_link_cannot_deliver_the_victims_session_to_the_sender(tmp_path, monkeypatch):
    """The sender of a sign-in link holds the ticket; only the victim's phone sees the handoff."""
    identity, client = make_env(tmp_path)
    victim, _ = register(identity, client, "victim")
    identity.google_link(victim["user"]["id"], "victim-subject")

    attacker = client.post("/api/mobile/auth/google/start", json={"return": "app"}).json
    handoff, _ = handoff_from(callback(client, attacker, monkeypatch, subject="victim-subject"))

    for guess in (None, "x" * 43):
        body = {"ticket": attacker["ticket"]} | ({"handoff": guess} if guess else {})
        response = client.post("/api/mobile/auth/google/poll", json=body)
        assert "auth" not in (response.json or {})

    # The victim's own app receives the handoff, but it belongs to another flow: nothing is issued
    # and the victim's waiting flow is dropped so the person starts again knowingly.
    own = client.post("/api/mobile/auth/google/start", json={"return": "app"}).json
    stolen = client.post(
        "/api/mobile/auth/google/poll", json={"ticket": own["ticket"], "handoff": handoff}
    )
    assert stolen.status_code == 403
    assert "이 앱에서 시작한 구글 로그인이 아니에요" in stolen.json["error"]
    assert (
        client.post("/api/mobile/auth/google/poll", json={"ticket": own["ticket"]}).status_code
        == 400
    )
    db = sqlite3.connect(tmp_path / "identity.sqlite3")
    try:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        db.close()


def test_app_return_link_needs_the_handoff_and_cancel_clears_it(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    session, headers = register(identity, client)

    start = client.post(
        "/api/mobile/account/google/link", json={"return": "app"}, headers=headers
    ).json
    assert "verificationCode" not in start
    page = callback(client, start, monkeypatch)
    assert "alice 앱 계정에 Google 연결을 마무리해요" in page.text
    handoff, _ = handoff_from(page)
    assert client.post("/api/mobile/auth/google/poll", json={"ticket": start["ticket"]}).json == {
        "status": "pending"
    }
    assert identity.google_linked("google-subject-1") is None
    linked = client.post(
        "/api/mobile/auth/google/poll", json={"ticket": start["ticket"], "handoff": handoff}
    ).json
    assert linked == {"status": "complete", "linked": True}
    assert identity.google_linked("google-subject-1") == session["user"]["id"]

    assert (
        client.post("/api/mobile/auth/google/cancel", json={"ticket": start["ticket"]}).status_code
        == 200
    )
    assert identity.google_linked("google-subject-1") is None

    other = client.post(
        "/api/mobile/account/google/link", json={"return": "app"}, headers=headers
    ).json
    other_handoff, _ = handoff_from(
        callback(client, other, monkeypatch, subject="google-subject-2")
    )
    client.post("/api/mobile/auth/google/cancel", json={"ticket": other["ticket"]})
    cancelled = client.post(
        "/api/mobile/auth/google/poll", json={"ticket": other["ticket"], "handoff": other_handoff}
    )
    assert cancelled.status_code == 403
    assert identity.google_linked("google-subject-2") is None


def test_app_return_flow_reports_an_unlinked_account_and_refuses_the_code_page(
    tmp_path, monkeypatch
):
    identity, client = make_env(tmp_path)
    start = client.post("/api/mobile/auth/google/start", json={"return": "app"}).json
    page = callback(client, start, monkeypatch, subject="nobody")
    assert page.status_code == 403
    assert "intent://" not in page.text
    failed = client.post("/api/mobile/auth/google/poll", json={"ticket": start["ticket"]})
    assert failed.status_code == 403

    # An app-return flow can never become a code confirmation, and a code flow never a handoff.
    session, _ = register(identity, client)
    identity.google_link(session["user"]["id"], "linked")
    flow = identity.google_start("login", app_return=True)
    row = identity.google_take_callback(flow["state"])
    assert identity.google_prepare_confirmation(row["id"], "linked") is False
    assert identity.google_prepare_handoff(row["id"], "linked")["username"] == "alice"
    code_flow = identity.google_start("login")
    code_row = identity.google_take_callback(code_flow["state"])
    assert identity.google_prepare_handoff(code_row["id"], "linked") is False
    assert identity.google_prepare_confirmation(code_row["id"], "linked")["username"] == "alice"


def test_app_missing_page_explains_where_to_finish(tmp_path):
    _, client = make_env(tmp_path)
    page = client.get("/api/mobile/auth/google/app-missing")
    assert page.status_code == 200
    assert "구글 로그인을 시작한 휴대폰에서 다시 시도해 주세요." in page.text


def test_existing_identity_database_gains_app_return_columns(tmp_path):
    path = tmp_path / "identity.sqlite3"
    IdentityStore(path)
    db = sqlite3.connect(path)
    with db:
        db.execute("DROP TABLE google_flows")
        db.execute(
            "CREATE TABLE google_flows (id TEXT PRIMARY KEY, proof_hash TEXT NOT NULL,"
            " state_hash TEXT UNIQUE NOT NULL, nonce TEXT NOT NULL, code_verifier TEXT NOT NULL,"
            " verification_hash TEXT NOT NULL, initiator_label TEXT NOT NULL,"
            " initiator_ip TEXT NOT NULL, mode TEXT NOT NULL, user_id TEXT, session_hash TEXT,"
            " expires REAL NOT NULL, callback_used INTEGER NOT NULL DEFAULT 0,"
            " status TEXT NOT NULL DEFAULT 'pending', subject TEXT, error TEXT,"
            " confirmation_hash TEXT, confirmation_cookie_hash TEXT,"
            " confirmation_cookie_name TEXT, confirmation_attempts INTEGER NOT NULL DEFAULT 0,"
            " delivered_session_hash TEXT, delivered_link INTEGER NOT NULL DEFAULT 0)"
        )
    db.close()
    identity = IdentityStore(path)
    flow = identity.google_start("login", app_return=True)
    assert "verificationCode" not in flow


def test_handoff_poll_edge_cases_never_issue_a_session(tmp_path, monkeypatch):
    identity, client = make_env(tmp_path)
    session, _ = register(identity, client)
    identity.google_link(session["user"]["id"], "google-subject-1")
    poll = "/api/mobile/auth/google/poll"

    # A handoff against a code flow: the code flow is dropped, nothing is issued.
    code = client.post("/api/mobile/auth/google/start", json={}).json
    assert callback(client, code, monkeypatch).status_code == 200
    refused = client.post(poll, json={"ticket": code["ticket"], "handoff": "x" * 43})
    assert refused.status_code == 403
    assert client.post(poll, json={"ticket": code["ticket"]}).status_code == 400

    # Malformed handoffs are rejected before touching the flow.
    flow = client.post("/api/mobile/auth/google/start", json={"return": "app"}).json
    for bad in (123, "short", "y" * 129):
        assert client.post(poll, json={"ticket": flow["ticket"], "handoff": bad}).status_code == 400
    assert client.post(poll, json={"ticket": flow["ticket"]}).json == {"status": "pending"}

    # Two finished app-return flows: each handoff opens only its own.
    first = client.post("/api/mobile/auth/google/start", json={"return": "app"}).json
    second = client.post("/api/mobile/auth/google/start", json={"return": "app"}).json
    first_handoff, _ = handoff_from(callback(client, first, monkeypatch))
    second_handoff, _ = handoff_from(callback(client, second, monkeypatch))
    crossed = client.post(poll, json={"ticket": second["ticket"], "handoff": first_handoff})
    assert crossed.status_code == 403
    assert (
        client.post(poll, json={"ticket": second["ticket"], "handoff": second_handoff}).status_code
        == 400
    )
    assert (
        "auth" in client.post(poll, json={"ticket": first["ticket"], "handoff": first_handoff}).json
    )


def test_expired_app_return_flow_cannot_be_finished_with_its_handoff(tmp_path, monkeypatch):
    now = [1_000_000.0]
    identity, client = make_env(tmp_path, clock=lambda: now[0])
    session, _ = register(identity, client)
    identity.google_link(session["user"]["id"], "google-subject-1")
    start = client.post("/api/mobile/auth/google/start", json={"return": "app"}).json
    handoff, _ = handoff_from(callback(client, start, monkeypatch))
    now[0] += 301
    expired = client.post(
        "/api/mobile/auth/google/poll", json={"ticket": start["ticket"], "handoff": handoff}
    )
    assert expired.status_code == 400
    assert "만료" in expired.json["error"]
