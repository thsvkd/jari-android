"""Real persistent auth, exercised without Redis or railway calls."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from korail_bot.mobile.identity import AuthError, IdentityStore, digest


@pytest.mark.parametrize("password", ["x" * 7, "x" * 129])
def test_password_length_rejects_values_outside_eight_to_128(tmp_path, password):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    with pytest.raises(AuthError, match="8~128"):
        store.credentials("alice", password)


@pytest.mark.parametrize("password", ["x" * 8, "x" * 128])
def test_password_length_accepts_eight_and_128_for_member_and_admin(tmp_path, password):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    member = store.register("alice", password)
    assert store.login("alice", password)["user"] == member["user"]
    store.ensure_admin("operator", password)
    assert store.login("operator", password)["user"]["role"] == "admin"


def test_registration_is_pending_and_session_survives_restart(tmp_path):
    path = tmp_path / "identity.sqlite3"
    store = IdentityStore(path)
    session = store.register("alice", "a long secure passphrase", "같이 출근하는 가족")
    reopened = IdentityStore(path)
    assert reopened.authenticate(session["token"])["status"] == "pending"
    assert session["user"]["applicationNote"] == "같이 출근하는 가족"
    assert session["user"]["createdAt"] is not None
    assert session["user"]["id"].startswith("mobile_")


def test_registration_note_is_optional_and_limited_to_200_characters(tmp_path):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    assert store.register("alice", "a long secure passphrase")["user"]["applicationNote"] is None
    assert (
        store.register("bobby", "a long secure passphrase", "x" * 200)["user"]["applicationNote"]
        == "x" * 200
    )
    with pytest.raises(AuthError, match="200") as error:
        store.register("carol", "a long secure passphrase", "x" * 201)
    assert error.value.status == 400
    with pytest.raises(AuthError):
        store.register("david", "a long secure passphrase", 123)


def test_password_login_and_revocation(tmp_path):
    clock = [1000.0]
    store = IdentityStore(tmp_path / "identity.sqlite3", clock=lambda: clock[0])
    session = store.register("alice", "a long secure passphrase")
    with pytest.raises(AuthError):
        store.login("alice", "wrong password")
    store.revoke(session["token"])
    with pytest.raises(AuthError):
        store.authenticate(session["token"])
    next_session = store.login("alice", "a long secure passphrase")
    clock[0] += 31 * 86400
    with pytest.raises(AuthError):
        store.authenticate(next_session["token"])


def test_plaintext_secrets_are_not_persisted(tmp_path):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    password = "a unique and secure password"
    note = "my private application note"
    alice = store.register("alice", password, note)
    with pytest.raises(AuthError):
        store.register("ALICE", password)
    data = Path(store.path).read_bytes()
    for secret in (password, alice["token"]):
        assert secret.encode() not in data
    assert note.encode() in data
    assert b"scrypt:32768:8:3" in data


def test_limits_survive_restart_and_reset_only_after_window(tmp_path):
    now = [1000.0]
    store = IdentityStore(tmp_path / "identity.sqlite3", clock=lambda: now[0])
    assert store.allow("one", 1, 60)
    assert not IdentityStore(store.path, clock=lambda: now[0]).allow("one", 1, 60)
    now[0] += 61
    assert store.allow("one", 1, 60)


def test_admin_login_is_separate_from_members(tmp_path):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    admin = store.ensure_admin("thsvkd", "a long admin passphrase")
    assert admin["role"] == "admin"
    member = store.register("alice", "a long secure passphrase")
    assert member["user"]["role"] == "member"
    session = store.login("thsvkd", "a long admin passphrase", expected_role="admin")
    assert session["user"]["role"] == "admin"
    with pytest.raises(AuthError) as member_door:
        store.login("thsvkd", "a long admin passphrase", expected_role="member")
    assert member_door.value.status == 403
    with pytest.raises(AuthError) as admin_door:
        store.login("alice", "a long secure passphrase", expected_role="admin")
    assert admin_door.value.status == 403
    store.ensure_admin("thsvkd", "a replaced admin passphrase")
    store.login("thsvkd", "a replaced admin passphrase", expected_role="admin")
    with pytest.raises(AuthError):
        store.login("thsvkd", "a long admin passphrase", expected_role="admin")


def test_existing_users_are_migrated_to_approved_without_inventing_created_at(tmp_path):
    path = tmp_path / "identity.sqlite3"
    token = "old-session-" + "x" * 32
    db = sqlite3.connect(path)
    try:
        db.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,"
            " password_hash TEXT NOT NULL, storage_id INTEGER UNIQUE NOT NULL,"
            " role TEXT NOT NULL DEFAULT 'member')"
        )
        db.execute("INSERT INTO users VALUES ('old', 'alice', 'hash', -1, 'member')")
        db.execute(
            "CREATE TABLE sessions (hash TEXT PRIMARY KEY, user_id TEXT NOT NULL"
            " REFERENCES users(id), expires REAL NOT NULL)"
        )
        db.execute("INSERT INTO sessions VALUES (?, 'old', 2000)", (digest(token),))
        db.commit()
    finally:
        db.close()
    store = IdentityStore(path, clock=lambda: 1000)
    user = store.find_user("alice")
    assert user["status"] == "approved"
    assert user["applicationNote"] is None
    assert user["createdAt"] is None
    assert store.authenticate(token) == {**user, "storage_id": -1}
    newcomer = store.register("bobby", "a long secure passphrase")
    assert newcomer["user"]["status"] == "pending"
    reopened = IdentityStore(path, clock=lambda: 1000)
    assert reopened.authenticate(token)["status"] == "approved"
    assert reopened.authenticate(newcomer["token"])["status"] == "pending"


def test_approval_decisions_are_idempotent_and_never_demote_approved_or_admin(tmp_path):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    alice = store.register("alice", "a long secure passphrase")["user"]
    admin = store.ensure_admin("operator", "a long admin passphrase")
    assert store.set_approval(alice["id"], "rejected")["status"] == "rejected"
    assert store.set_approval(alice["id"], "rejected")["status"] == "rejected"
    assert store.set_approval(alice["id"], "approved")["status"] == "approved"
    assert store.set_approval(alice["id"], "approved")["status"] == "approved"
    with pytest.raises(AuthError) as demote:
        store.set_approval(alice["id"], "rejected")
    assert demote.value.status == 409
    with pytest.raises(AuthError) as change_admin:
        store.set_approval(admin["id"], "rejected")
    assert change_admin.value.status == 403


def test_member_list_puts_pending_then_rejected_before_approved_accounts(tmp_path):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    approved = store.register("approved", "a long secure passphrase")["user"]
    rejected = store.register("rejected", "a long secure passphrase")["user"]
    store.register("pending", "a long secure passphrase")
    store.set_approval(approved["id"], "approved")
    store.set_approval(rejected["id"], "rejected")
    store.ensure_admin("operator", "a long admin passphrase")
    assert [user["username"] for user in store.list_users()] == [
        "pending",
        "rejected",
        "approved",
        "operator",
    ]


def test_concurrent_approve_and_reject_can_never_leave_an_approved_member_rejected(tmp_path):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    member = store.register("alice", "a long secure passphrase")["user"]

    def decide(status):
        try:
            return store.set_approval(member["id"], status)["status"]
        except AuthError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ["approved", "rejected"]))

    assert "approved" in results
    assert results[1] in {"rejected", 409}
    assert store.find_user("alice")["status"] == "approved"
