from types import SimpleNamespace

import httpx
import pytest
import requests
from korail_mobile_api import KorailClient, KorailConfig
from korail_mobile_api.errors import (
    KorailAuthError,
    KorailServiceUnavailableError,
    KorailTransportError,
)

from korail_bot.services import korail_service
from korail_bot.services.korail_service import KORAIL_DYNAPATH_TOKEN, KorailService


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


def test_a_login_in_place_closes_the_korail2_session_it_replaces(monkeypatch):
    # A cached mobile rail logs in again in place when Korail drops its login.
    service = KorailService()
    modern_clients = iter([_ModernClient(), _ModernClient()])
    legacy = [_LegacyClient(), _LegacyClient()]
    built = list(legacy)
    monkeypatch.setattr(service, "_build_modern_client", lambda: next(modern_clients))
    monkeypatch.setattr(service, "_build_client", lambda *_: built.pop(0))
    closed = []
    for client in legacy:
        client._session.close = lambda client=client: closed.append(client)

    assert service.login("1234567890", "synthetic-password") is True
    assert service.login("1234567890", "synthetic-password") is True

    assert closed == [legacy[0]]
    assert service._korail_instance is legacy[1]


class _UnreachableClient(_ModernClient):
    def __init__(self, error: Exception):
        super().__init__()
        self.error = error

    def login(self, username: str, password: str, **kwargs):
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        KorailTransportError("KORAIL HTTP 503 for POST /classes/login"),
        KorailServiceUnavailableError("SEMGTK", "점검 중"),
        TimeoutError("timed out"),
    ],
)
def test_a_login_korail_never_answered_is_told_apart_from_a_refusal(monkeypatch, error):
    service = KorailService()
    modern = _UnreachableClient(error)
    monkeypatch.setattr(service, "_build_modern_client", lambda: modern)

    assert service.login("1234567890", "synthetic-password") is False

    assert service.login_unavailable is True
    assert modern.closed is True
    assert service._modern_client is None


def test_a_refused_login_is_not_mistaken_for_korail_being_down(monkeypatch):
    service = KorailService()
    service.login_unavailable = True  # left by an earlier try
    modern = _UnreachableClient(KorailAuthError("비밀번호 오류"))
    monkeypatch.setattr(service, "_build_modern_client", lambda: modern)

    assert service.login("1234567890", "synthetic-password") is False

    assert service.login_unavailable is False


def test_a_relogin_korail_never_answered_fails_quietly(monkeypatch):
    service = KorailService()
    service._username, service._password = "1234567890", "synthetic-password"
    monkeypatch.setattr(
        service, "_build_modern_client", lambda: _UnreachableClient(KorailTransportError("x"))
    )

    assert service._relogin() is False
    assert service._logged_in is False


def _korail_answering_login(login_reply):
    """
    The real korail_mobile_api client over a fake transport: the service check
    and the crypto parameters succeed, and the login POST gets `login_reply`.
    The whole login path of the library runs, wrapping included.
    """

    def reply(request):
        if request.url.path.endswith("MobileService.cache"):
            return httpx.Response(200, json={"strResult": "SUCC", "h_msg_cd": "", "h_msg_txt": ""})
        if request.url.path.endswith("common.code.do"):
            return httpx.Response(
                200,
                json={"strResult": "SUCC", "h_msg_cd": "", "h_msg_txt": "", "pwdAESCphd": "N"},
            )
        return login_reply

    return KorailClient(KorailConfig(enable_dynapath=True), transport=httpx.MockTransport(reply))


@pytest.mark.parametrize(
    "login_reply",
    [
        # Korail down for maintenance at the login POST itself, which the
        # library re-raises as KorailAuthError.
        httpx.Response(200, json={"strResult": "FAIL", "h_msg_cd": "SEMGTK", "h_msg_txt": "점검"}),
        # A maintenance page instead of a Korail answer.
        httpx.Response(200, text="<html>서비스 점검 중</html>"),
        httpx.Response(503, text="Service Unavailable"),
    ],
)
def test_korail_down_at_the_login_itself_is_not_a_refusal(monkeypatch, login_reply):
    service = KorailService()
    monkeypatch.setattr(
        service, "_build_modern_client", lambda: _korail_answering_login(login_reply)
    )

    assert service.login("1234567890", "synthetic-password") is False

    assert service.login_unavailable is True


def test_a_login_korail_turns_down_is_a_refusal(monkeypatch):
    service = KorailService()
    refused = httpx.Response(
        200, json={"strResult": "FAIL", "h_msg_cd": "WRC000392", "h_msg_txt": "비밀번호 오류"}
    )
    monkeypatch.setattr(service, "_build_modern_client", lambda: _korail_answering_login(refused))

    assert service.login("1234567890", "synthetic-password") is False

    assert service.login_unavailable is False


MAINTENANCE = "시스템 점검 작업으로‘26.10.14. 01시 30분 ~ 04시 30분까지 승차권 예매 서비스를 일시 중지 하오니 이점 양해 부탁드립니다."


def _korail_in_maintenance(notice=MAINTENANCE):
    """Korail's service check answering the way it did live during a maintenance window."""

    def reply(request):
        if request.url.path.endswith("MobileService.cache"):
            return httpx.Response(
                200, json={"strResult": "FAIL", "h_msg_cd": "S100", "h_msg_txt": notice}
            )
        raise AssertionError(f"no login is sent in maintenance: {request.url.path}")

    return KorailClient(KorailConfig(enable_dynapath=True), transport=httpx.MockTransport(reply))


def test_a_maintenance_window_is_korail_down_with_its_own_notice_not_a_refusal(monkeypatch):
    service = KorailService()
    monkeypatch.setattr(service, "_build_modern_client", _korail_in_maintenance)

    assert service.login("1234567890", "synthetic-password") is False

    assert service.login_unavailable is True
    assert service.login_unavailable_notice == MAINTENANCE


def test_a_refusal_after_maintenance_drops_the_old_notice(monkeypatch):
    service = KorailService()
    monkeypatch.setattr(service, "_build_modern_client", _korail_in_maintenance)
    service.login("1234567890", "synthetic-password")
    refused = httpx.Response(
        200, json={"strResult": "FAIL", "h_msg_cd": "WRC000392", "h_msg_txt": "비밀번호 오류"}
    )
    monkeypatch.setattr(service, "_build_modern_client", lambda: _korail_answering_login(refused))

    assert service.login("1234567890", "synthetic-password") is False

    assert service.login_unavailable is False
    assert service.login_unavailable_notice is None


def test_a_maintenance_window_without_a_sentence_is_still_korail_down(monkeypatch):
    service = KorailService()
    monkeypatch.setattr(service, "_build_modern_client", lambda: _korail_in_maintenance(""))

    assert service.login("1234567890", "synthetic-password") is False

    assert service.login_unavailable is True
    assert service.login_unavailable_notice is None


def test_a_long_maintenance_sentence_comes_back_as_one_short_line(monkeypatch):
    long_notice = "점검\n" + "안내 " * 200
    service = KorailService()
    monkeypatch.setattr(
        service, "_build_modern_client", lambda: _korail_in_maintenance(long_notice)
    )

    service.login("1234567890", "synthetic-password")

    assert "\n" not in service.login_unavailable_notice
    assert len(service.login_unavailable_notice) <= 200


def test_s100_from_the_login_itself_stays_a_refusal(monkeypatch):
    # Only the service check, before the password is sent, is read as maintenance.
    service = KorailService()
    refused = httpx.Response(
        200, json={"strResult": "FAIL", "h_msg_cd": "S100", "h_msg_txt": "확인이 필요해요"}
    )
    monkeypatch.setattr(service, "_build_modern_client", lambda: _korail_answering_login(refused))

    assert service.login("1234567890", "synthetic-password") is False

    assert service.login_unavailable is False


SCHEDULE_VIEW = "/classes/com.korail.mobile.seatMovie.ScheduleView"


def _client_asking_through(monkeypatch, sent):
    def reply(request):
        sent.append(request)
        return httpx.Response(200, json={"strResult": "SUCC", "h_msg_cd": "", "h_msg_txt": ""})

    real = KorailClient
    monkeypatch.setattr(
        korail_service,
        "KorailClient",
        lambda config: real(config, transport=httpx.MockTransport(reply)),
    )
    return KorailService()


def test_the_current_client_sends_the_accepted_dynapath_token_on_a_train_search(monkeypatch):
    sent = []
    client = _client_asking_through(monkeypatch, sent)._build_modern_client()

    client.http.post_form(SCHEDULE_VIEW, {})

    (request,) = sent
    assert request.headers[client.config.dynapath.header_name] == KORAIL_DYNAPATH_TOKEN
    assert KORAIL_DYNAPATH_TOKEN.startswith("2!")


def test_the_legacy_adapter_gets_the_same_dynapath_token(monkeypatch):
    service = KorailService()
    modern = service._build_modern_client()
    monkeypatch.setattr(service, "_build_client", lambda *_: _LegacyClient())
    session = SimpleNamespace(jsessionid="synthetic-session", member_card_no="1", raw={})

    service._activate_legacy_session(modern, session, "1234567890", "synthetic-password")

    headers, _ = service._korail_instance._get_auth_headers_and_sid(
        f"https://smart.letskorail.com{SCHEDULE_VIEW}"
    )
    assert headers == {modern.config.dynapath.header_name: KORAIL_DYNAPATH_TOKEN}
    modern.close()


def test_the_current_client_keeps_the_library_dynapath_allowlist():
    from korail_mobile_api.config import enabled_dynapath_config

    client = KorailService()._build_modern_client()
    library = enabled_dynapath_config()
    assert client.config.dynapath.enabled is True
    assert client.config.dynapath.token_settings is None
    assert client.config.dynapath.allowlist_paths == library.allowlist_paths
    assert client.config.dynapath.header_name == library.header_name
    client.close()
