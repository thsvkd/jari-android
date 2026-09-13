from types import SimpleNamespace

import requests

from korail_bot.services.korail_service import KorailService


class _ModernClient:
    def __init__(self, *, fail: bool = False):
        self.config = SimpleNamespace(version="250601003", key="current-app-key")
        self.http = SimpleNamespace(
            _dynapath_headers=lambda method, path: {
                "x-test-method": method,
                "x-test-path": path,
            }
        )
        self.fail = fail
        self.closed = False
        self.login_args = None

    def login(self, username: str, password: str, **kwargs):
        self.login_args = (username, password, kwargs)
        if self.fail:
            raise RuntimeError("synthetic login failure")
        return SimpleNamespace(
            jsessionid="synthetic-session",
            member_card_no="1234567890",
            raw={"Key": "session-app-key"},
        )

    def close(self):
        self.closed = True


class _LegacyClient:
    def __init__(self):
        self._session = requests.Session()
        self._version = "old"
        self._key = "old"
        self.membership_number = None
        self.logined = False


def test_login_uses_current_api_and_bridges_authenticated_session(monkeypatch):
    service = KorailService()
    modern = _ModernClient()
    legacy = _LegacyClient()
    monkeypatch.setattr(service, "_build_modern_client", lambda: modern)
    monkeypatch.setattr(service, "_build_client", lambda *_: legacy)

    assert service.login("1234567890", "synthetic-password") is True

    assert modern.login_args == (
        "1234567890",
        "synthetic-password",
        {"input_flag": "2"},
    )
    assert legacy._session.cookies.get("JSESSIONID") == "synthetic-session"
    assert legacy._version == "250601003"
    assert legacy._key == "session-app-key"
    assert legacy.membership_number == "1234567890"
    assert legacy.logined is True
    assert service._korail_instance is legacy
    assert service._modern_client is modern

    headers, sid = legacy._get_auth_headers_and_sid(
        "https://smart.letskorail.com/classes/com.korail.mobile.seatMovie.ScheduleView"
    )
    assert headers == {
        "x-test-method": "POST",
        "x-test-path": "/classes/com.korail.mobile.seatMovie.ScheduleView",
    }
    assert sid is None


def test_login_failure_closes_candidate_and_clears_session(monkeypatch):
    service = KorailService()
    modern = _ModernClient(fail=True)
    monkeypatch.setattr(service, "_build_modern_client", lambda: modern)

    assert service.login("1234567890", "synthetic-password") is False

    assert modern.closed is True
    assert service._logged_in is False
    assert service._korail_instance is None
    assert service._modern_client is None


def test_hyphenated_phone_login_is_explicitly_classified_as_phone(monkeypatch):
    service = KorailService()
    modern = _ModernClient()
    monkeypatch.setattr(service, "_build_modern_client", lambda: modern)
    monkeypatch.setattr(service, "_build_client", lambda *_: _LegacyClient())

    assert service.login("010-1234-5678", "synthetic-password") is True

    assert modern.login_args == (
        "010-1234-5678",
        "synthetic-password",
        {"input_flag": "4"},
    )


def test_successful_relogin_replaces_and_closes_previous_client(monkeypatch):
    service = KorailService()
    first = _ModernClient()
    second = _ModernClient()
    legacy_clients = [_LegacyClient(), _LegacyClient()]
    modern_clients = iter([first, second])
    monkeypatch.setattr(service, "_build_modern_client", lambda: next(modern_clients))
    monkeypatch.setattr(service, "_build_client", lambda *_: legacy_clients.pop(0))

    assert service.login("1234567890", "synthetic-password") is True
    assert service._relogin() is True

    assert first.closed is True
    assert second.closed is False
    assert service._modern_client is second
    assert service._relogin_count == 1
