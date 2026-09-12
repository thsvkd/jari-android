"""A restart or failed push must not lose a user's notification."""

from pathlib import Path

import pytest

from korail_bot.mobile.identity import AuthError, IdentityStore
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
    with pytest.raises(AuthError):
        events.register_device(-200, token)
    events.remove_device(-200, token)
    events.send_message(-100, "좌석 예약 완료")
    assert events.deliver() == 0
    assert len(events.items(-100)) == 1
    clock[0] += 120
    assert events.deliver() == 1
    assert calls == [token, token]
    assert token.encode() not in Path(store.path).read_bytes()
