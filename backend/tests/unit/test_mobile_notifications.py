"""A restart or failed push must not lose a user's notification."""

from pathlib import Path

import pytest

from korail_bot.mobile.identity import AuthError, IdentityStore, digest
from korail_bot.mobile.notifications import Notifications


def test_notifications_are_durable_and_scoped(tmp_path):
    store = IdentityStore(tmp_path / "app.sqlite3")
    events = Notifications(store, secret="a" * 40)
    events.send_message(-100, "예약되었습니다")
    reopened = Notifications(IdentityStore(store.path), secret="a" * 40)
    assert reopened.items(-100)[0]["text"] == "예약되었습니다"
    assert reopened.items(-200) == []
    assert not reopened.push_available


def test_push_failure_retries_without_losing_event_or_crossing_user(tmp_path):
    calls = []

    def push(token, event):
        calls.append(token)
        if len(calls) == 1:
            raise OSError("offline")

    clock = [1000.0]
    store = IdentityStore(tmp_path / "app.sqlite3", clock=lambda: clock[0])
    events = Notifications(store, secret="a" * 40, push=push)
    token = "private-device-token-for-alice"
    events.register_device(-100, token)
    events.register_device(-100, token)
    events.remove_device(-200, token)  # another owner may not remove A's device
    with events.identity.connect() as db:
        assert (
            db.execute("SELECT owner FROM devices WHERE hash=?", (digest(token),)).fetchone()[
                "owner"
            ]
            == -100
        )
    events.remove_device(-100, token)
    events.register_device(-100, token)
    events.send_message(-100, "좌석 예약 완료")
    assert events.deliver() == 0
    assert len(events.items(-100)) == 1
    clock[0] += 120
    assert events.deliver() == 1
    assert calls == [token, token]
    assert token.encode() not in Path(store.path).read_bytes()


def test_register_device_reassigns_from_previous_owner_instead_of_409(tmp_path):
    """
    A logged-out phone still holds its FCM token. Rejecting the next
    account's registration with 409 locked that phone out of push forever,
    with no UI to clear the old binding (backend-mobile#0). Only the app
    install that holds the token can present it, so the current login
    should take it over.
    """
    store = IdentityStore(tmp_path / "app.sqlite3")
    events = Notifications(store, secret="a" * 40, push_enabled=True)
    token = "shared-phone-fcm-token"
    events.register_device(-100, token)
    events.send_message(-100, "A의 예약 알림")
    assert len(events.items(-100)) == 1

    # No AuthError: registering under a new owner reassigns the device.
    events.register_device(-200, token)

    with events.identity.connect() as db:
        row = db.execute("SELECT owner FROM devices WHERE hash=?", (digest(token),)).fetchone()
        assert row["owner"] == -200
        # A's queued push for this device must not reach B's phone.
        outbox = db.execute("SELECT * FROM outbox WHERE device_hash=?", (digest(token),)).fetchall()
        assert outbox == []

    # The device-limit guard still applies only to genuinely new devices.
    events.register_device(-200, "another-token-for-b")


def test_register_device_takeover_is_still_capped_by_the_new_owners_device_limit(tmp_path):
    """A takeover is a genuinely new device for the new owner, so it must not
    let them slip past the 10-device cap that a fresh registration enforces.
    """
    store = IdentityStore(tmp_path / "app.sqlite3")
    events = Notifications(store, secret="a" * 40, push_enabled=True)
    token = "a-shared-fcm-token-already-owned-by-a"
    events.register_device(-100, token)
    for i in range(10):
        events.register_device(-200, f"b-device-{i}")

    with pytest.raises(AuthError):
        events.register_device(-200, token)

    with events.identity.connect() as db:
        row = db.execute("SELECT owner FROM devices WHERE hash=?", (digest(token),)).fetchone()
        assert row["owner"] == -100
