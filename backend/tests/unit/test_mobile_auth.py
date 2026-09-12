"""Real persistent auth, exercised without Redis or railway calls."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from korail_bot.mobile.identity import AuthError, IdentityStore


def test_invitation_is_single_use_and_session_survives_restart(tmp_path):
    path = tmp_path / "identity.sqlite3"
    store = IdentityStore(path)
    invite = store.create_invite()
    session = store.register("alice", "a long secure passphrase", invite)
    reopened = IdentityStore(path)
    assert reopened.authenticate(session["token"])["username"] == "alice"
    with pytest.raises(AuthError):
        reopened.register("bob", "another secure passphrase", invite)
    assert session["user"]["id"].startswith("mobile_")


def test_invite_race_has_exactly_one_winner(tmp_path):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    invite = store.create_invite()

    def redeem(name):
        try:
            return store.register(name, "a long secure passphrase", invite)
        except AuthError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(result is not None for result in pool.map(redeem, ["alice", "bob"])) == 1


def test_invalid_expired_invites_password_and_revocation(tmp_path):
    clock = [1000.0]
    store = IdentityStore(tmp_path / "identity.sqlite3", clock=lambda: clock[0])
    invite = store.create_invite(ttl=1)
    clock[0] += 2
    for rejected in [invite, "wrong"]:
        with pytest.raises(AuthError):
            store.register("alice", "a long secure passphrase", rejected)
    session = store.register("alice", "a long secure passphrase", store.create_invite())
    with pytest.raises(AuthError):
        store.login("alice", "wrong password")
    store.revoke(session["token"])
    with pytest.raises(AuthError):
        store.authenticate(session["token"])
    next_session = store.login("alice", "a long secure passphrase")
    clock[0] += 31 * 86400
    with pytest.raises(AuthError):
        store.authenticate(next_session["token"])


def test_plaintext_secrets_are_not_persisted_and_failed_signup_keeps_invite(tmp_path):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    invite = store.create_invite()
    password = "a unique and secure password"
    alice = store.register("alice", password, invite)
    second = store.create_invite()
    with pytest.raises(AuthError):
        store.register("ALICE", password, second)
    assert store.register("bobby", password, second)["user"]["username"] == "bobby"
    data = Path(store.path).read_bytes()
    for secret in (invite, second, password, alice["token"]):
        assert secret.encode() not in data
    assert b"scrypt:32768:8:3" in data


def test_limits_survive_restart_and_reset_only_after_window(tmp_path):
    now = [1000.0]
    store = IdentityStore(tmp_path / "identity.sqlite3", clock=lambda: now[0])
    assert store.allow("one", 1, 60)
    assert not IdentityStore(store.path, clock=lambda: now[0]).allow("one", 1, 60)
    now[0] += 61
    assert store.allow("one", 1, 60)


def test_admin_login_is_separate_from_invited_members(tmp_path):
    store = IdentityStore(tmp_path / "identity.sqlite3")
    admin = store.ensure_admin("thsvkd", "a long admin passphrase")
    assert admin["role"] == "admin"
    invited = store.register("alice", "a long secure passphrase", store.create_invite())
    assert invited["user"]["role"] == "member"
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
