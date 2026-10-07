"""
에이전트 연결의 인가 서버(OAuth 2.1 + DCR + 앱 승인)를 명세대로 고정한다.

명세: .omc/plans/mcp-agent-spec.md §1~§4, §6~§7, §9. 이 테스트들이 지키는 것은
"토큰이 새도 앱 계정 전체가 열리지 않는다", "코드·refresh 가 새도 재사용하면 끊긴다",
"앱에서 사용자가 코드를 직접 넣어야만 연결된다"이다.
"""

import re
from datetime import datetime
from pathlib import Path

import pytest

from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.identity import digest

from .agent_support import (
    CLAUDE_CALLBACK,
    LOOPBACK_CALLBACK,
    PUBLIC,
    RESOURCE,
    USER_CODE,
    allow_booking_for,
    app_headers,
    approve,
    authorization_code,
    authorize,
    bearer,
    browser_cookie,
    call_tool,
    challenge_for,
    cheap_setup,
    connect,
    exchange,
    lookup,
    make_env,
    pkce,
    poll,
    redirect_params,
    refresh,
    register_client,
    rpc,
    signup,
    start,
    token,
    user_code,
)

DAY = 86400


@pytest.fixture(autouse=True)
def _cheap_setup(monkeypatch):
    cheap_setup(monkeypatch)


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


@pytest.fixture
def alice(env):
    return signup(env, "alice")


def oauth_error(response):
    return response.get_json(silent=True) or {}


def mcp_status(env, access_token):
    return rpc(env, access_token, "ping").status_code


# ----------------------------------------------------------------- §1 설정과 기능 스위치


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://jari.example/", "https://jari.example"),
        ("https://jari.example", "https://jari.example"),
        ("http://127.0.0.1:18281", "http://127.0.0.1:18281"),
        ("http://localhost:8080/", "http://localhost:8080"),
    ],
)
def test_public_url_keeps_https_or_local_http_without_trailing_slash(
    monkeypatch, tmp_path, raw, expected
):
    monkeypatch.setenv("MOBILE_SECRET", "s" * 40)
    monkeypatch.setenv("MOBILE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MOBILE_PUBLIC_URL", raw)
    assert MobileConfig.from_env(auth_only=True).public_url == expected


@pytest.mark.parametrize("raw", ["http://jari.example", "ftp://jari.example", "jari.example"])
def test_public_url_refuses_cleartext_remote_hosts(monkeypatch, tmp_path, raw):
    monkeypatch.setenv("MOBILE_SECRET", "s" * 40)
    monkeypatch.setenv("MOBILE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MOBILE_PUBLIC_URL", raw)
    with pytest.raises(ValueError):
        MobileConfig.from_env(auth_only=True)


def test_public_url_unset_means_the_feature_is_off(monkeypatch, tmp_path):
    monkeypatch.setenv("MOBILE_SECRET", "s" * 40)
    monkeypatch.setenv("MOBILE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MOBILE_PUBLIC_URL", raising=False)
    assert MobileConfig.from_env(auth_only=True).public_url is None


AGENT_PATHS = [
    ("get", "/.well-known/oauth-protected-resource/api/mobile/mcp"),
    ("get", "/.well-known/oauth-protected-resource"),
    ("get", "/.well-known/oauth-authorization-server"),
    ("post", "/oauth/register"),
    ("get", "/oauth/authorize"),
    ("get", "/oauth/authorize/poll"),
    ("post", "/oauth/token"),
    ("post", "/oauth/revoke"),
    ("post", "/api/mobile/mcp"),
    ("post", "/api/mobile/agents/requests/lookup"),
    ("post", "/api/mobile/agents/requests/approve"),
    ("post", "/api/mobile/agents/requests/deny"),
    ("get", "/api/mobile/agents"),
    ("post", "/api/mobile/agents/some-id"),
    ("delete", "/api/mobile/agents/some-id"),
]


@pytest.mark.parametrize(("method", "path"), AGENT_PATHS)
def test_every_agent_path_is_404_without_a_public_url(tmp_path, method, path):
    env = make_env(tmp_path, public_url=None)
    user = signup(env, "alice")
    response = getattr(env.client, method)(path, headers=app_headers(user), json={})
    assert response.status_code == 404


def test_bootstrap_says_whether_agents_can_connect(tmp_path):
    off = make_env(tmp_path / "off", public_url=None)
    on = make_env(tmp_path / "on")
    for env, expected in ((off, False), (on, True)):
        user = signup(env, "alice")
        response = env.client.post("/api/mobile/bootstrap", headers=app_headers(user))
        assert response.json["capabilities"]["agents"] is expected


def test_bootstrap_hands_the_app_the_mcp_url_only_when_on(tmp_path):
    # §11 M6: 앱은 baseUrl 로 URL 을 만들지 않고 서버가 아는 resource 를 그대로 보여 준다.
    on = make_env(tmp_path / "on", public_url="https://jari.thsvkd.dev")
    off = make_env(tmp_path / "off", public_url=None)
    user = signup(on, "alice")
    body = on.client.post("/api/mobile/bootstrap", headers=app_headers(user)).json
    assert body["agents"] == {"mcpUrl": "https://jari.thsvkd.dev/api/mobile/mcp"}
    user = signup(off, "alice")
    assert "agents" not in off.client.post("/api/mobile/bootstrap", headers=app_headers(user)).json


# ----------------------------------------------------------------- §2 메타데이터


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-protected-resource/api/mobile/mcp",
        "/.well-known/oauth-protected-resource",
    ],
)
def test_protected_resource_metadata_points_at_this_issuer(env, path):
    response = env.client.get(path)
    assert response.status_code == 200
    assert response.json["resource"] == RESOURCE
    assert response.json["authorization_servers"] == [PUBLIC]
    assert response.json["scopes_supported"] == ["jari.read", "jari.book"]
    assert response.json["bearer_methods_supported"] == ["header"]
    assert response.json["resource_name"] == "자리났다"


def test_authorization_server_metadata_offers_only_public_clients_with_s256(env):
    response = env.client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    meta = response.json
    assert meta["issuer"] == PUBLIC
    assert meta["authorization_endpoint"] == PUBLIC + "/oauth/authorize"
    assert meta["token_endpoint"] == PUBLIC + "/oauth/token"
    assert meta["registration_endpoint"] == PUBLIC + "/oauth/register"
    assert meta["revocation_endpoint"] == PUBLIC + "/oauth/revoke"
    assert meta["response_types_supported"] == ["code"]
    assert meta["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert meta["scopes_supported"] == ["jari.read", "jari.book"]
    assert meta["authorization_response_iss_parameter_supported"] is True
    # DCR 로 유도한다.
    assert "client_id_metadata_document_supported" not in meta


def test_browser_and_external_origins_reach_oauth_but_not_mcp(env):
    foreign = {"Origin": "https://evil.example"}
    assert (
        env.client.get("/.well-known/oauth-authorization-server", headers=foreign).status_code
        == 200
    )
    registered = env.client.post(
        "/oauth/register", headers=foreign, json={"redirect_uris": [CLAUDE_CALLBACK]}
    )
    assert registered.status_code == 201
    # MCP 는 기존 규칙 그대로: 허용 목록 밖 Origin 은 403.
    assert env.client.post("/api/mobile/mcp", headers=foreign, json={}).status_code == 403


# ----------------------------------------------------------------- §4.2 DCR


@pytest.mark.parametrize(
    "uris",
    [
        [CLAUDE_CALLBACK],
        ["https://claude.com/api/mcp/auth_callback"],
        ["http://localhost:53682/callback"],
        ["http://127.0.0.1:33418/oauth/callback"],
        ["http://[::1]:8080/cb"],
        ["http://localhost/callback"],
        [
            CLAUDE_CALLBACK,
            "http://localhost:1/a",
            "http://localhost:2/b",
            "http://127.0.0.1/c",
            "http://[::1]/d",
        ],
    ],
)
def test_registration_accepts_claude_callbacks_and_loopback(env, uris):
    response = env.client.post(
        "/oauth/register", json={"redirect_uris": uris, "client_name": "Claude"}
    )
    assert response.status_code == 201
    body = response.json
    assert len(body["client_id"]) >= 43  # 32바이트 이상 무작위
    assert isinstance(body["client_id_issued_at"], int)
    assert body["redirect_uris"] == uris
    assert "client_secret" not in body


def test_registered_client_ids_are_unique(env):
    ids = {register_client(env)["client_id"] for _ in range(5)}
    assert len(ids) == 5


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/callback",
        "https://claude.ai.evil.example/api/mcp/auth_callback",
        "https://claude.ai/api/mcp/auth_callback?next=https://evil.example",
        "https://claude.ai/api/mcp/auth_callback/extra",
        "https://claude.ai:8443/api/mcp/auth_callback",
        "http://claude.ai/api/mcp/auth_callback",
        "https://localhost:8080/callback",
        "http://localhost.evil.example/callback",
        "http://10.0.0.1/callback",
        "http://user@localhost/callback",
        "javascript:alert(1)",
        "",
    ],
)
def test_registration_refuses_redirects_outside_the_allow_list(env, uri):
    response = env.client.post("/oauth/register", json={"redirect_uris": [uri]})
    assert response.status_code == 400
    assert oauth_error(response).get("error") == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "uris", [[], [f"http://localhost/{n}" for n in range(6)], "http://localhost/cb"]
)
def test_registration_needs_one_to_five_redirects(env, uris):
    response = env.client.post("/oauth/register", json={"redirect_uris": uris})
    assert response.status_code == 400
    assert oauth_error(response).get("error") in {"invalid_redirect_uri", "invalid_client_metadata"}


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post", "private_key_jwt"])
def test_registration_refuses_confidential_clients(env, method):
    response = env.client.post(
        "/oauth/register",
        json={"redirect_uris": [CLAUDE_CALLBACK], "token_endpoint_auth_method": method},
    )
    assert response.status_code == 400
    assert oauth_error(response).get("error") == "invalid_client_metadata"


def test_registration_defaults_to_no_client_authentication(env):
    response = env.client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
    assert response.status_code == 201
    assert response.json.get("token_endpoint_auth_method", "none") == "none"


def test_client_name_shown_in_the_app_is_cleaned_and_short(env, alice):
    name = "Evil\x00\x1b[31m\nName" + "x" * 100
    registered = env.client.post(
        "/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK], "client_name": name}
    ).json
    verifier, challenge = pkce()
    page = authorize(env, registered["client_id"], CLAUDE_CALLBACK, challenge)
    shown = lookup(env, alice, user_code(page)).json["client"]["name"]
    assert len(shown) <= 64
    assert not any(ord(char) < 32 or ord(char) == 127 for char in shown)
    assert shown.startswith("Evil")


def register_from(env, ip):
    return env.client.post(
        "/oauth/register",
        headers={"CF-Connecting-IP": ip},
        json={"redirect_uris": [CLAUDE_CALLBACK]},
    ).status_code


def test_registration_allows_ten_an_hour_per_address(env):
    # §11 L12: IP별 10회/시간.
    statuses = [register_from(env, "203.0.113.9") for _ in range(11)]
    assert statuses == [201] * 10 + [429]
    assert register_from(env, "198.51.100.7") == 201
    env.clock.advance(3601)
    assert register_from(env, "203.0.113.9") == 201


def test_registration_allows_two_hundred_an_hour_overall(env):
    # §11 L12: 전역 200회/시간. 주소를 바꿔 가며 보내도 막힌다.
    statuses = [register_from(env, f"198.51.{n // 10}.{n % 10}") for n in range(200)]
    assert statuses == [201] * 200
    assert register_from(env, "192.0.2.1") == 429


@pytest.mark.parametrize(
    "extra",
    [
        {"grant_types": ["authorization_code", "refresh_token"]},
        {"grant_types": ["authorization_code"]},
        {"response_types": ["code"]},
    ],
)
def test_registration_accepts_the_supported_grant_and_response_types(env, extra):
    response = env.client.post(
        "/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK], **extra}
    )
    assert response.status_code == 201


@pytest.mark.parametrize(
    "extra",
    [
        {"grant_types": ["client_credentials"]},
        {"grant_types": ["authorization_code", "implicit"]},
        {"grant_types": ["urn:ietf:params:oauth:grant-type:device_code"]},
        {"response_types": ["token"]},
        {"response_types": ["code", "id_token"]},
    ],
)
def test_registration_refuses_other_grant_and_response_types(env, extra):
    # §11 L6.
    response = env.client.post(
        "/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK], **extra}
    )
    assert response.status_code == 400
    assert oauth_error(response).get("error") == "invalid_client_metadata"


def test_a_client_that_never_connected_is_forgotten_after_a_day(env):
    registered = register_client(env)
    env.clock.advance(DAY + 60)
    page = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1])
    assert page.status_code == 400
    assert "Location" not in page.headers


# ----------------------------------------------------------------- §4.3 authorize


def test_unknown_client_gets_an_error_page_and_no_redirect(env):
    response = authorize(env, "no-such-client", CLAUDE_CALLBACK, pkce()[1])
    assert response.status_code == 400
    assert "Location" not in response.headers
    assert response.mimetype == "text/html"


@pytest.mark.parametrize(
    "requested",
    [
        "https://claude.com/api/mcp/auth_callback",
        "https://evil.example/callback",
        "http://localhost:53682/other",
        "http://127.0.0.1:53682/callback",
    ],
)
def test_a_redirect_that_was_not_registered_is_never_followed(env, requested):
    registered = register_client(env, [CLAUDE_CALLBACK, LOOPBACK_CALLBACK])
    response = authorize(env, registered["client_id"], requested, pkce()[1])
    assert response.status_code == 400
    assert "Location" not in response.headers


def test_loopback_redirect_port_is_ignored(env):
    registered = register_client(env, [LOOPBACK_CALLBACK])
    response = authorize(env, registered["client_id"], "http://localhost:61999/callback", pkce()[1])
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("override", "codes"),
    [
        ({"code_challenge": None}, {"invalid_request"}),
        ({"code_challenge_method": "plain"}, {"invalid_request"}),
        ({"code_challenge_method": None}, {"invalid_request"}),
        (
            {"resource": "https://other.example/api/mobile/mcp"},
            {"invalid_request", "invalid_target"},
        ),
        ({"response_type": "token"}, {"invalid_request", "unsupported_response_type"}),
    ],
)
def test_other_authorize_errors_return_to_the_client_with_state_and_issuer(env, override, codes):
    registered = register_client(env)
    response = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1], **override)
    assert response.status_code in {302, 303}
    location = response.headers["Location"]
    assert location.startswith(CLAUDE_CALLBACK + "?")
    params = redirect_params(location)
    assert params["error"] in codes
    assert params["state"] == "state-123"
    assert params["iss"] == PUBLIC
    assert "code" not in params


def test_authorize_page_shows_the_code_where_to_enter_it_and_who_asked(env):
    registered = register_client(env)
    response = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1])
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    html = response.get_data(as_text=True)
    assert USER_CODE.search(html)
    assert "자리났다 앱 → 설정 → 에이전트 연결" in html
    assert "claude.ai" in html


def test_authorize_page_for_a_local_program_says_so(env):
    registered = register_client(env, [LOOPBACK_CALLBACK])
    response = authorize(env, registered["client_id"], LOOPBACK_CALLBACK, pkce()[1])
    html = response.get_data(as_text=True)
    # 인가 페이지(에이전트를 연 그 컴퓨터의 브라우저)는 "이 컴퓨터의 프로그램" 그대로다.
    # 앱 시트·목록의 문구(§11 H1)는 e2e/agents.spec.ts 가 고정한다.
    assert "이 컴퓨터의 프로그램" in html


def test_authorize_page_cannot_be_framed_cached_or_leak_its_url(env):
    registered = register_client(env)
    response = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1])
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    csp = response.headers["Content-Security-Policy"]
    for directive in (
        "default-src 'none'",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ):
        assert directive in csp
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp)
    assert nonce, csp
    assert f'nonce="{nonce.group(1)}"' in response.get_data(as_text=True)


def test_each_authorize_page_gets_its_own_nonce(env):
    registered = register_client(env)
    nonces = {
        re.search(
            r"'nonce-([^']+)'",
            authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1]).headers[
                "Content-Security-Policy"
            ],
        ).group(1)
        for _ in range(3)
    }
    assert len(nonces) == 3


def test_state_chosen_by_the_client_is_not_written_into_the_page(env):
    registered = register_client(env)
    hostile = "</script><script>alert(1)</script>"
    response = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1], state=hostile)
    assert response.status_code == 200
    assert "<script>alert(1)" not in response.get_data(as_text=True)


def test_state_is_never_part_of_the_page(env):
    # §11 M10: state 는 리다이렉트 URL 에만 쓴다. 이스케이프한 꼴로도 페이지에 없다.
    registered = register_client(env)
    marker = "s7a7e" + "q" * 20
    response = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1], state=marker)
    assert marker not in response.get_data(as_text=True)


def test_client_name_in_the_page_is_escaped(env):
    hostile = '<img src=x onerror="alert(1)">'
    registered = env.client.post(
        "/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK], "client_name": hostile}
    ).json
    html = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1]).get_data(
        as_text=True
    )
    assert "<img" not in html


def test_browser_cookie_is_host_only_secure_and_unreadable_by_scripts(env):
    registered = register_client(env)
    name, value, attributes = browser_cookie(
        authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1])
    )
    assert name == "__Host-jari_oauth"
    assert len(value) >= 32
    for attribute in ("secure", "httponly", "samesite=lax", "path=/"):
        assert attribute in attributes
    assert "domain=" not in attributes


def test_local_http_server_drops_the_host_prefix_and_secure(tmp_path):
    env = make_env(tmp_path, public_url="http://127.0.0.1:18281")
    registered = register_client(env)
    name, _, attributes = browser_cookie(
        authorize(
            env,
            registered["client_id"],
            CLAUDE_CALLBACK,
            pkce()[1],
            resource="http://127.0.0.1:18281/api/mobile/mcp",
        )
    )
    assert name == "jari_oauth"
    assert "secure" not in attributes
    assert "httponly" in attributes and "samesite=lax" in attributes


# ----------------------------------------------------------------- §4.4 poll


def test_poll_is_pending_until_the_app_approves(env, alice):
    pending = start(env, alice)
    response = poll(env, pending.request_id, pending.cookie)
    assert response.status_code == 200
    assert response.json["status"] == "pending"
    assert not response.json.get("redirect")


def test_poll_needs_the_browser_that_started_the_request(env, alice):
    first = start(env, alice)
    second = start(env, alice)
    assert poll(env, first.request_id).status_code == 404
    assert poll(env, first.request_id, (first.cookie[0], "x" * 43)).status_code == 404
    # 다른 요청을 시작한 브라우저의 쿠키로는 이 요청을 볼 수 없다.
    assert poll(env, first.request_id, second.cookie).status_code == 404


def test_approved_poll_hands_the_code_to_that_browser_once(env, alice):
    pending = start(env, alice)
    assert approve(env, alice, pending.request_id).status_code == 200

    first = poll(env, pending.request_id, pending.cookie)
    assert first.json["status"] == "approved"
    redirect = first.json["redirect"]
    assert redirect.startswith(CLAUDE_CALLBACK + "?")
    params = redirect_params(redirect)
    assert params["state"] == "state-123"
    assert params["iss"] == PUBLIC
    assert len(params["code"]) >= 43

    second = poll(env, pending.request_id, pending.cookie)
    assert second.json["status"] == "expired"
    assert not second.json.get("redirect")


def test_denied_poll_returns_access_denied_to_the_client(env, alice):
    pending = start(env, alice)
    response = env.client.post(
        "/api/mobile/agents/requests/deny",
        headers=app_headers(alice),
        json={"requestId": pending.request_id},
    )
    assert response.status_code == 200
    polled = poll(env, pending.request_id, pending.cookie).json
    assert polled["status"] == "denied"
    params = redirect_params(polled["redirect"])
    assert params["error"] == "access_denied"
    assert params["state"] == "state-123"
    assert params["iss"] == PUBLIC
    assert "code" not in params


def test_a_request_expires_after_ten_minutes(env, alice):
    pending = start(env, alice)
    env.clock.advance(601)
    assert poll(env, pending.request_id, pending.cookie).json["status"] == "expired"
    assert lookup(env, alice, pending.code).status_code == 404
    assert approve(env, alice, pending.request_id).status_code == 404


# ----------------------------------------------------------------- §4.5 앱 승인


def test_lookup_names_the_client_and_where_it_will_return(env, alice):
    registered = register_client(env)
    page = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1])
    found = lookup(env, alice, user_code(page))
    assert found.status_code == 200
    body = found.json
    assert isinstance(body["requestId"], str) and body["requestId"]
    assert body["client"] == {"name": "Claude", "host": "claude.ai", "local": False}
    # §11 M5·H1: 요청 시각도 준다(시트의 "N분 전 요청").
    requested = datetime.fromisoformat(body["requestedAt"])
    expires = datetime.fromisoformat(body["expiresAt"])
    assert requested.tzinfo is not None
    assert abs(requested.timestamp() - env.clock.now) < 5
    assert 590 <= (expires - requested).total_seconds() <= 600


def test_lookup_marks_a_loopback_client_as_local(env, alice):
    registered = register_client(env, [LOOPBACK_CALLBACK])
    page = authorize(env, registered["client_id"], LOOPBACK_CALLBACK, pkce()[1])
    assert lookup(env, alice, user_code(page)).json["client"]["local"] is True


@pytest.mark.parametrize(
    "variant",
    [
        lambda code: code,
        lambda code: code.lower(),
        lambda code: code.replace("-", ""),
        lambda code: " " + code[:4] + " " + code[5:] + " ",
        lambda code: code[:2] + "-" + code[2:].replace("-", "").lower(),
    ],
)
def test_lookup_ignores_case_hyphens_and_spaces(env, alice, variant):
    registered = register_client(env)
    page = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1])
    code = user_code(page)
    canonical = lookup(env, alice, code).json["requestId"]
    assert lookup(env, alice, variant(code)).json["requestId"] == canonical


def test_wrong_and_expired_codes_get_the_same_answer(env, alice):
    pending = start(env, alice)
    wrong = lookup(env, alice, "ABCD-EFGH" if pending.code != "ABCD-EFGH" else "HGFE-DCBA")
    env.clock.advance(601)
    expired = lookup(env, alice, pending.code)
    for response in (wrong, expired):
        assert response.status_code == 404
        assert response.json["error"] == "코드를 다시 확인해 주세요."


@pytest.mark.parametrize("path", ["lookup", "approve", "deny"])
def test_approval_routes_need_the_app_session(env, path):
    response = env.client.post(f"/api/mobile/agents/requests/{path}", json={"code": "ABCD-EFGH"})
    assert response.status_code == 401


def test_code_guessing_is_capped_per_user_and_failures_count(env, alice):
    pending = start(env, alice)  # 1회
    statuses = [lookup(env, alice, "ZZZZ-ZZZZ").status_code for _ in range(9)]  # 2~10회
    assert statuses == [404] * 9
    assert lookup(env, alice, pending.code).status_code == 429  # 11회
    env.clock.advance(301)
    assert lookup(env, alice, pending.code).status_code == 200


def test_approve_and_deny_share_the_guessing_budget(env, alice):
    pending = start(env, alice)  # 1회
    for _ in range(9):
        env.client.post(
            "/api/mobile/agents/requests/deny",
            headers=app_headers(alice),
            json={"requestId": "no-such-request"},
        )
    assert approve(env, alice, pending.request_id).status_code == 429


def test_another_user_cannot_touch_a_request_already_decided(env, alice):
    bob = signup(env, "bob")
    pending = start(env, alice)
    assert approve(env, alice, pending.request_id).status_code == 200
    assert lookup(env, bob, pending.code).status_code == 404
    assert approve(env, bob, pending.request_id).status_code == 404
    denied = env.client.post(
        "/api/mobile/agents/requests/deny",
        headers=app_headers(bob),
        json={"requestId": pending.request_id},
    )
    assert denied.status_code == 404


def deny(env, user, request_id):
    return env.client.post(
        "/api/mobile/agents/requests/deny",
        headers=app_headers(user),
        json={"requestId": request_id},
    )


def test_the_first_user_to_look_a_code_up_claims_the_request(env, alice):
    # §11 M7: lookup 한 사용자가 요청을 확보한다. 다른 사용자는 코드를 알아도, requestId 를 알아도 못 쓴다.
    bob = signup(env, "bob")
    pending = start(env, alice)
    assert lookup(env, bob, pending.code).status_code == 404
    assert approve(env, bob, pending.request_id).status_code == 404
    assert deny(env, bob, pending.request_id).status_code == 404
    assert poll(env, pending.request_id, pending.cookie).json["status"] == "pending"
    assert approve(env, alice, pending.request_id).status_code == 200


def test_a_claimed_request_still_expires_ten_minutes_after_it_was_made(env, alice):
    registered = register_client(env)
    page = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1])
    env.clock.advance(540)
    request_id = lookup(env, alice, user_code(page)).json["requestId"]
    env.clock.advance(61)
    assert approve(env, alice, request_id).status_code == 404


def test_a_denied_request_can_not_be_approved_afterwards(env, alice):
    pending = start(env, alice)
    env.client.post(
        "/api/mobile/agents/requests/deny",
        headers=app_headers(alice),
        json={"requestId": pending.request_id},
    )
    assert approve(env, alice, pending.request_id).status_code == 404


def test_a_new_connection_only_reads_whatever_the_client_asked_for(env, alice):
    # 클라이언트는 authorize 에서 "jari.read jari.book" 을 요청한다(agent_support.authorize_query).
    # §12 S-H1: 새 연결은 늘 jari.read 로 시작한다.
    tokens = connect(env, alice)
    assert tokens["scope"].split() == ["jari.read"]
    assert connections(env, alice)[0]["allowBooking"] is False


@pytest.mark.parametrize("value", [True, False, "yes", None])
def test_approve_refuses_an_allow_booking_field(env, alice, value):
    # §12 S-H1: 예약 허용은 연결한 뒤 목록에서만 켠다. 오래된 앱이 켠 채 보내면 무시하지 않고 막는다.
    pending = start(env, alice)
    response = env.client.post(
        "/api/mobile/agents/requests/approve",
        headers=app_headers(alice),
        json={"requestId": pending.request_id, "allowBooking": value},
    )
    assert response.status_code == 400
    assert poll(env, pending.request_id, pending.cookie).json["status"] == "pending"
    assert connections(env, alice) == []


# ----------------------------------------------------------------- §12 S-H1 위치 신호


def country_lookup(env, user, *, asked=None, entered=None):
    registered = register_client(env)
    page = authorize(
        env,
        registered["client_id"],
        CLAUDE_CALLBACK,
        pkce()[1],
        headers={"CF-IPCountry": asked} if asked is not None else None,
    )
    found = lookup(
        env, user, user_code(page), headers={"CF-IPCountry": entered} if entered else None
    )
    assert found.status_code == 200
    return found.json


def test_lookup_tells_where_the_requesting_browser_was(env, alice):
    body = country_lookup(env, alice, asked="JP", entered="JP")
    assert body["requestCountry"] == "JP"
    assert body["countryMismatch"] is False


def test_lookup_flags_a_browser_in_another_country(env, alice):
    body = country_lookup(env, alice, asked="JP", entered="KR")
    assert body["requestCountry"] == "JP"
    assert body["countryMismatch"] is True


def test_no_country_header_means_no_country_and_no_warning(env, alice):
    body = country_lookup(env, alice)
    assert body.get("requestCountry") is None
    assert body.get("countryMismatch", False) is False


@pytest.mark.parametrize("raw", ["jp", "JPN", "J", "X1", "", "<b>", "T1"])
def test_only_two_capital_letters_are_kept_as_a_country(env, alice, raw):
    body = country_lookup(env, alice, asked=raw, entered="KR")
    assert body.get("requestCountry") is None
    assert body.get("countryMismatch", False) is False


# ----------------------------------------------------------------- §4.6 token


def test_token_response_is_a_bearer_pair_that_is_not_cached(env, alice):
    pending, code = authorization_code(env, alice)
    response = exchange(env, pending, code)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    body = response.json
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["access_token"] != body["refresh_token"]
    assert len(body["access_token"]) >= 32 and len(body["refresh_token"]) >= 32
    assert mcp_status(env, body["access_token"]) == 200


def test_tokens_are_stored_only_as_hashes(env, alice):
    tokens = connect(env, alice)
    # WAL 모드라 최근 쓰기는 -wal 파일에 있다.
    raw = b"".join(
        Path(env.identity.path + suffix).read_bytes()
        for suffix in ("", "-wal")
        if Path(env.identity.path + suffix).exists()
    )
    for value in (tokens["access_token"], tokens["refresh_token"]):
        assert value.encode() not in raw


@pytest.mark.parametrize(
    "override",
    [
        {"code_verifier": "w" * 64},
        {"code_verifier": None},
        {"redirect_uri": "https://claude.com/api/mcp/auth_callback"},
        {"redirect_uri": None},
        {"code": "not-a-code-" + "x" * 40},
    ],
)
def test_code_exchange_refuses_anything_that_does_not_match(env, alice, override):
    pending, code = authorization_code(env, alice)
    # "code" 는 exchange 의 위치 인자라 키워드로 덮어쓸 수 없다. 틀린 코드는 그 자리에 넣는다.
    override = dict(override)
    response = exchange(env, pending, override.pop("code", code), **override)
    assert response.status_code == 400
    assert oauth_error(response).get("error") in {"invalid_grant", "invalid_request"}


def test_code_exchange_refuses_another_clients_id(env, alice):
    pending, code = authorization_code(env, alice)
    other = register_client(env)["client_id"]
    response = exchange(env, pending, code, client_id=other)
    assert response.status_code == 400
    assert oauth_error(response).get("error") in {"invalid_grant", "invalid_client"}


def test_code_exchange_refuses_another_resource(env, alice):
    pending, code = authorization_code(env, alice)
    response = exchange(env, pending, code, resource="https://other.example/api/mobile/mcp")
    assert response.status_code == 400
    assert oauth_error(response).get("error") in {"invalid_grant", "invalid_target"}


@pytest.mark.parametrize(
    ("length", "accepted"), [(42, False), (43, True), (128, True), (129, False)]
)
def test_code_verifier_must_be_43_to_128_characters(env, alice, length, accepted):
    verifier = ("abcdefghij" * 13)[:length]
    pending, code = authorization_code(env, alice, verifier=verifier)
    response = exchange(env, pending, code)
    assert (response.status_code == 200) is accepted, response.get_data(as_text=True)


@pytest.mark.parametrize(("seconds", "accepted"), [(59, True), (61, False)])
def test_a_code_lives_sixty_seconds(env, alice, seconds, accepted):
    pending, code = authorization_code(env, alice)
    env.clock.advance(seconds)
    response = exchange(env, pending, code)
    assert (response.status_code == 200) is accepted
    if not accepted:
        assert oauth_error(response).get("error") == "invalid_grant"


def test_reusing_a_code_revokes_what_it_already_issued(env, alice):
    pending, code = authorization_code(env, alice)
    first = exchange(env, pending, code).json
    second = exchange(env, pending, code)
    assert second.status_code == 400
    assert oauth_error(second).get("error") == "invalid_grant"
    assert mcp_status(env, first["access_token"]) == 401
    assert refresh(env, {**first, "client_id": pending.client_id}).status_code == 400
    # §11 L3: grant 자체를 폐기한다. 연결 목록에서도 사라진다.
    assert connections(env, alice) == []


def test_an_approved_connection_appears_only_after_the_token_exchange(env, alice):
    # §11 L4: 승인은 grant 를 "대기"로 만들고, 토큰 교환이 성공해야 목록에 나온다.
    pending, code = authorization_code(env, alice)
    assert connections(env, alice) == []
    assert exchange(env, pending, code).status_code == 200
    assert [agent["name"] for agent in connections(env, alice)] == ["Claude"]


def test_a_code_never_exchanged_leaves_no_connection(env, alice):
    pending, code = authorization_code(env, alice)
    env.clock.advance(61)
    assert exchange(env, pending, code).status_code == 400
    assert connections(env, alice) == []


def test_token_endpoint_wants_a_form_body(env, alice):
    pending, code = authorization_code(env, alice)
    response = env.client.post(
        "/oauth/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "client_id": pending.client_id,
            "code_verifier": pending.verifier,
        },
    )
    assert response.status_code == 400
    assert oauth_error(response).get("error") == "invalid_request"


def test_unknown_grant_type_is_named_as_such(env):
    response = token(env, {"grant_type": "password", "username": "a", "password": "b"})
    assert response.status_code == 400
    assert oauth_error(response).get("error") == "unsupported_grant_type"


def test_token_errors_say_nothing_internal(env):
    response = token(env, {"grant_type": "authorization_code", "code": "x" * 43})
    assert response.status_code == 400
    assert set(oauth_error(response)) <= {"error", "error_description"}


GLOBAL_PHRASES = (
    "요청 내용을 확인해 주세요.",
    "JSON 객체 형식으로 보내 주세요.",
    "처리 중 문제가 생겼어요",
)


@pytest.mark.parametrize(
    ("path", "kwargs", "code"),
    [
        (
            "/oauth/token",
            {"data": "%%%not-a-form", "content_type": "text/plain"},
            "invalid_request",
        ),
        ("/oauth/token", {"data": {}}, "invalid_request"),
        (
            "/oauth/register",
            {"data": "[1, 2", "content_type": "application/json"},
            "invalid_client_metadata",
        ),
        ("/oauth/register", {"json": ["not", "an", "object"]}, "invalid_client_metadata"),
    ],
)
def test_oauth_errors_keep_their_own_shape(env, path, kwargs, code):
    # §11 M1: 전역 오류 처리기의 한국어 문구가 OAuth 오류 본문을 바꾸면 안 된다.
    response = env.client.post(path, **kwargs)
    assert response.status_code == 400
    body = oauth_error(response)
    assert body.get("error") == code
    assert set(body) <= {"error", "error_description"}
    assert not any(phrase in response.get_data(as_text=True) for phrase in GLOBAL_PHRASES)


def test_oauth_rate_limit_answers_in_oauth_shape(env):
    for _ in range(10):
        register_from(env, "203.0.113.9")
    response = env.client.post(
        "/oauth/register",
        headers={"CF-Connecting-IP": "203.0.113.9"},
        json={"redirect_uris": [CLAUDE_CALLBACK]},
    )
    assert response.status_code == 429
    body = oauth_error(response)
    assert set(body) <= {"error", "error_description"}
    assert re.fullmatch(r"[a-z_]+", body.get("error", ""))


def token_from(env, ip, form=None):
    return env.client.post(
        "/oauth/token",
        headers={"CF-Connecting-IP": ip},
        data=form or {"grant_type": "refresh_token", "refresh_token": "x" * 43, "client_id": "c"},
    ).status_code


def test_token_endpoint_allows_sixty_a_minute_per_address(env):
    # §11 L5: IP별 60회/분. 실패한 요청도 센다.
    statuses = [token_from(env, "203.0.113.9") for _ in range(61)]
    assert statuses == [400] * 60 + [429]
    assert token_from(env, "198.51.100.7") == 400
    env.clock.advance(61)
    assert token_from(env, "203.0.113.9") == 400


def test_token_endpoint_allows_sixty_a_minute_per_client(env):
    # §12 S-L3: 전역 600/분 대신 client_id 별 60/분. 주소를 바꿔도 같은 클라이언트면 막힌다.
    client_id = register_client(env)["client_id"]
    form = {"grant_type": "refresh_token", "refresh_token": "x" * 43, "client_id": client_id}
    statuses = [token_from(env, f"10.0.{n // 50}.{n % 50}", form) for n in range(61)]
    assert statuses == [400] * 60 + [429]
    other = register_client(env)["client_id"]
    assert token_from(env, "192.0.2.1", {**form, "client_id": other}) == 400


def test_unknown_clients_only_meet_the_address_budget(env):
    # 모르는 client_id 로는 버킷을 만들지 않는다. 그 대신 전역 버킷도 없다.
    statuses = [token_from(env, f"10.1.{n // 50}.{n % 50}") for n in range(700)]
    assert 429 not in statuses


def test_revoke_shares_the_clients_token_budget(env):
    client_id = register_client(env)["client_id"]
    form = {"grant_type": "refresh_token", "refresh_token": "x" * 43, "client_id": client_id}
    for n in range(60):
        token_from(env, f"10.2.0.{n}", form)
    response = env.client.post(
        "/oauth/revoke",
        headers={"CF-Connecting-IP": "192.0.2.9"},
        data={"token": "x" * 43, "client_id": client_id},
    )
    assert response.status_code == 429


# ----------------------------------------------------------------- §12 S-M1 대기 요청 한도


def authorize_from(env, client_id, ip):
    return authorize(env, client_id, CLAUDE_CALLBACK, pkce()[1], headers={"CF-Connecting-IP": ip})


def test_authorize_allows_three_hundred_a_minute_overall(env):
    client_id = register_client(env)["client_id"]
    statuses = [
        authorize_from(env, client_id, f"10.3.{n // 100}.{n % 100}").status_code for n in range(300)
    ]
    assert statuses == [200] * 300
    refused = authorize_from(env, client_id, "192.0.2.1")
    assert refused.status_code == 429
    assert "Location" not in refused.headers


def test_poll_allows_three_thousand_a_minute_overall(env, alice):
    pending = start(env, alice)
    cookie = f"{pending.cookie[0]}={pending.cookie[1]}"

    def poll_from(ip):
        return env.client.get(
            "/oauth/authorize/poll?request=" + pending.request_id,
            headers={"CF-Connecting-IP": ip, "Cookie": cookie},
        ).status_code

    statuses = [poll_from(f"10.4.{n // 100}.{n % 100}") for n in range(3000)]
    assert 429 not in statuses
    assert poll_from("192.0.2.1") == 429


def test_authorize_stops_making_requests_at_two_thousand_waiting(env):
    # 살아 있는 대기 요청이 2000개면 새 요청을 만들지 않고 503 오류 페이지(리다이렉트 없음).
    client_id = register_client(env)["client_id"]
    made = 0
    for minute in range(7):
        for n in range(min(300, 2000 - made)):
            assert authorize_from(env, client_id, f"10.5.{minute}.{n}").status_code == 200
            made += 1
        env.clock.advance(61)
    assert made == 2000
    full = authorize_from(env, client_id, "192.0.2.1")
    assert full.status_code == 503
    assert "Location" not in full.headers
    html = full.get_data(as_text=True)
    assert "잠시 후 다시 시도해 주세요" in html
    assert not USER_CODE.search(html)
    # 대기 요청이 만료되면 다시 만든다.
    env.clock.advance(601)
    assert authorize_from(env, client_id, "192.0.2.2").status_code == 200


# ----------------------------------------------------------------- §12 S-L2 너무 큰 본문


@pytest.mark.parametrize(
    ("path", "kwargs"),
    [
        ("/oauth/register", {"data": "x" * (3 * 1024 * 1024), "content_type": "application/json"}),
        ("/oauth/token", {"data": {"code": "x" * (3 * 1024 * 1024)}}),
        ("/oauth/revoke", {"data": {"token": "x" * (3 * 1024 * 1024)}}),
    ],
)
def test_an_oversized_oauth_body_gets_an_oauth_error(env, path, kwargs):
    response = env.client.post(path, **kwargs)
    assert response.status_code == 413
    assert response.get_json() == {"error": "invalid_request"}


def test_revoke_shares_the_token_budget(env):
    for _ in range(60):
        token_from(env, "203.0.113.9")
    response = env.client.post(
        "/oauth/revoke",
        headers={"CF-Connecting-IP": "203.0.113.9"},
        data={"token": "x" * 43, "client_id": "c"},
    )
    assert response.status_code == 429


def test_poll_allows_a_hundred_twenty_a_minute_per_address(env, alice):
    pending = start(env, alice)

    def poll_from(ip):
        return env.client.get(
            "/oauth/authorize/poll?request=" + pending.request_id,
            headers={
                "CF-Connecting-IP": ip,
                "Cookie": f"{pending.cookie[0]}={pending.cookie[1]}",
            },
        ).status_code

    statuses = [poll_from("203.0.113.9") for _ in range(121)]
    assert statuses == [200] * 120 + [429]
    assert poll_from("198.51.100.7") == 200


# ----------------------------------------------------------------- refresh·만료·revoke


def test_refresh_rotates_and_the_old_refresh_dies(env, alice):
    tokens = connect(env, alice)
    rotated = refresh(env, tokens)
    assert rotated.status_code == 200
    assert rotated.headers["Cache-Control"] == "no-store"
    new = rotated.json
    assert new["refresh_token"] != tokens["refresh_token"]
    assert new["access_token"] != tokens["access_token"]
    assert mcp_status(env, new["access_token"]) == 200


def test_refreshing_ends_the_previous_access_token(env, alice):
    # §12 S-M2: 회전하면 같은 grant 의 이전 access 는 지워진다. 새 access 만 남는다.
    tokens = connect(env, alice)
    new = refresh(env, tokens).json
    assert mcp_status(env, tokens["access_token"]) == 401
    assert mcp_status(env, new["access_token"]) == 200


def test_an_older_refresh_than_the_last_one_is_simply_refused(env, alice):
    # §12 S-M2: 바로 앞 세대만 재사용 탐지용으로 남는다. 그보다 오래된 것은 행이 없어 그냥 invalid_grant.
    first = connect(env, alice)
    second = {**refresh(env, first).json, "client_id": first["client_id"]}
    third = {**refresh(env, second).json, "client_id": first["client_id"]}
    replay = refresh(env, first)
    assert replay.status_code == 400
    assert oauth_error(replay).get("error") == "invalid_grant"
    # 연결은 살아 있다.
    assert mcp_status(env, third["access_token"]) == 200
    assert refresh(env, third).status_code == 200


def test_replaying_the_previous_generation_still_ends_the_connection(env, alice):
    first = connect(env, alice)
    second = {**refresh(env, first).json, "client_id": first["client_id"]}
    third = {**refresh(env, second).json, "client_id": first["client_id"]}
    replay = refresh(env, second)
    assert replay.status_code == 400
    assert mcp_status(env, third["access_token"]) == 401
    assert refresh(env, third).status_code == 400
    assert connections(env, alice) == []


def test_token_rows_stay_bounded_however_often_a_client_refreshes(env, alice):
    tokens = connect(env, alice)
    for _ in range(12):
        tokens = {**refresh(env, tokens).json, "client_id": tokens["client_id"]}
    with env.identity.connect() as db:
        count = db.execute("SELECT count(*) FROM oauth_tokens").fetchone()[0]
    # 지금 access 하나, 지금 refresh 하나, 바로 앞 refresh 하나.
    assert count <= 3


def test_replaying_a_rotated_refresh_ends_the_whole_connection(env, alice):
    tokens = connect(env, alice)
    new = {**refresh(env, tokens).json, "client_id": tokens["client_id"]}
    replay = refresh(env, tokens)
    assert replay.status_code == 400
    assert oauth_error(replay).get("error") == "invalid_grant"
    # 정당한 쪽이 가진 최신 토큰도 함께 끊긴다.
    assert mcp_status(env, new["access_token"]) == 401
    assert refresh(env, new).status_code == 400


def test_refresh_needs_the_same_client(env, alice):
    tokens = connect(env, alice)
    other = register_client(env)["client_id"]
    response = refresh(env, {**tokens, "client_id": other})
    assert response.status_code == 400
    assert oauth_error(response).get("error") == "invalid_grant"


def test_access_token_lasts_an_hour_and_refresh_still_works(env, alice):
    tokens = connect(env, alice)
    env.clock.advance(3601)
    assert mcp_status(env, tokens["access_token"]) == 401
    assert refresh(env, tokens).status_code == 200


def test_an_unused_refresh_token_lasts_thirty_days(env, alice):
    tokens = connect(env, alice)
    env.clock.advance(30 * DAY + 60)
    assert refresh(env, tokens).status_code == 400


def test_revoking_a_refresh_token_stops_refreshing(env, alice):
    tokens = connect(env, alice)
    response = env.client.post(
        "/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": tokens["client_id"]}
    )
    assert response.status_code == 200
    assert refresh(env, tokens).status_code == 400


def test_revoking_an_access_token_stops_it_at_once(env, alice):
    tokens = connect(env, alice)
    env.client.post(
        "/oauth/revoke", data={"token": tokens["access_token"], "client_id": tokens["client_id"]}
    )
    assert mcp_status(env, tokens["access_token"]) == 401


def test_revoking_an_unknown_token_is_not_an_error(env):
    response = env.client.post("/oauth/revoke", data={"token": "x" * 43, "client_id": "nobody"})
    assert response.status_code == 200


# ----------------------------------------------------------------- 토큰 교차 사용·audience


def test_app_session_cannot_call_mcp(env, alice):
    response = rpc(env, alice["token"], "ping")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer ")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/mobile/status"),
        ("post", "/api/mobile/bootstrap"),
        ("get", "/api/mobile/agents"),
    ],
)
def test_agent_token_cannot_use_the_app_api(env, alice, method, path):
    tokens = connect(env, alice, allow_booking=True)
    response = getattr(env.client, method)(path, headers=bearer(tokens["access_token"]), json={})
    assert response.status_code == 401


def test_a_refresh_token_is_not_an_access_token(env, alice):
    tokens = connect(env, alice)
    assert mcp_status(env, tokens["refresh_token"]) == 401


def test_a_token_issued_for_another_resource_is_refused(tmp_path):
    first = make_env(tmp_path)
    user = signup(first, "alice")
    tokens = connect(first, user)
    # 같은 저장소, 다른 공개 주소(= 다른 resource)로 다시 띄운 서버.
    moved = make_env(
        tmp_path, public_url="https://other.example", identity=first.identity, clock=first.clock
    )
    response = rpc(moved, tokens["access_token"], "ping")
    assert response.status_code == 401


# ----------------------------------------------------------------- §7 연결 목록·토글·해제


def connections(env, user):
    response = env.client.get("/api/mobile/agents", headers=app_headers(user))
    assert response.status_code == 200
    return response.json["agents"]


def test_connection_list_shows_what_the_user_approved(env, alice):
    tokens = connect(env, alice)
    call_tool(env, tokens["access_token"], "get_status")
    [agent] = connections(env, alice)
    assert agent["name"] == "Claude"
    assert agent["host"] == "claude.ai"
    assert agent["local"] is False
    assert agent["allowBooking"] is False
    assert agent["createdAt"]
    assert agent["lastUsedAt"]


def test_connection_list_is_per_user(env, alice):
    bob = signup(env, "bob")
    connect(env, alice)
    [agent] = connections(env, alice)
    assert connections(env, bob) == []
    assert (
        env.client.post(
            f"/api/mobile/agents/{agent['id']}",
            headers=app_headers(bob),
            json={"allowBooking": True},
        ).status_code
        == 404
    )
    assert (
        env.client.delete(f"/api/mobile/agents/{agent['id']}", headers=app_headers(bob)).status_code
        == 404
    )
    assert connections(env, alice)[0]["allowBooking"] is False


def test_turning_booking_on_applies_without_reconnecting(env, alice):
    tokens = connect(env, alice)
    [agent] = connections(env, alice)
    refused = call_tool(env, tokens["access_token"], "stop_watch")
    assert refused["isError"] is True
    response = env.client.post(
        f"/api/mobile/agents/{agent['id']}", headers=app_headers(alice), json={"allowBooking": True}
    )
    assert response.status_code == 200
    assert response.json["allowBooking"] is True
    allowed = call_tool(env, tokens["access_token"], "stop_watch")
    assert not allowed.get("isError")
    env.gateway.cancel_search.assert_called_once()


def test_turning_booking_off_applies_at_once(env, alice):
    tokens = connect(env, alice, allow_booking=True)
    [agent] = connections(env, alice)
    env.client.post(
        f"/api/mobile/agents/{agent['id']}",
        headers=app_headers(alice),
        json={"allowBooking": False},
    )
    result = call_tool(env, tokens["access_token"], "stop_watch")
    assert result["isError"] is True
    env.gateway.cancel_search.assert_not_called()


def test_toggle_needs_a_boolean(env, alice):
    connect(env, alice)
    [agent] = connections(env, alice)
    response = env.client.post(
        f"/api/mobile/agents/{agent['id']}",
        headers=app_headers(alice),
        json={"allowBooking": "yes"},
    )
    assert response.status_code == 400


def test_disconnecting_ends_every_token_at_once(env, alice):
    tokens = connect(env, alice)
    [agent] = connections(env, alice)
    response = env.client.delete(f"/api/mobile/agents/{agent['id']}", headers=app_headers(alice))
    assert response.status_code == 200
    assert mcp_status(env, tokens["access_token"]) == 401
    assert refresh(env, tokens).status_code == 400
    assert connections(env, alice) == []


def test_app_logout_keeps_agent_connections(env, alice):
    tokens = connect(env, alice)
    assert env.client.post("/api/mobile/auth/logout", headers=app_headers(alice)).status_code == 200
    assert mcp_status(env, tokens["access_token"]) == 200


def test_deleting_the_account_ends_agent_connections(env, alice):
    tokens = connect(env, alice)
    pending = start(env, alice)
    deleted = env.client.post(
        "/api/mobile/account/delete",
        headers=app_headers(alice),
        json={"password": "a long secure passphrase"},
    )
    assert deleted.status_code == 200
    assert mcp_status(env, tokens["access_token"]) == 401
    assert refresh(env, tokens).status_code == 400
    # 그 사용자가 보던 대기 요청도 지워져, 브라우저는 더 기다리지 않는다.
    polled = poll(env, pending.request_id, pending.cookie)
    assert (polled.get_json(silent=True) or {}).get("status") != "approved"


def test_agent_routes_need_the_app_session(env):
    assert env.client.get("/api/mobile/agents").status_code == 401
    assert env.client.delete("/api/mobile/agents/x").status_code == 401
    assert env.client.post("/api/mobile/agents/x", json={"allowBooking": True}).status_code == 401


def test_metadata_urls_follow_the_configured_public_url(tmp_path):
    env = make_env(tmp_path, public_url="https://jari.thsvkd.dev")
    prm = env.client.get("/.well-known/oauth-protected-resource/api/mobile/mcp").json
    assert prm["resource"] == "https://jari.thsvkd.dev/api/mobile/mcp"
    unauthenticated = env.client.post(
        "/api/mobile/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
    )
    assert (
        'resource_metadata="https://jari.thsvkd.dev/.well-known/oauth-protected-resource/api/mobile/mcp"'
        in unauthenticated.headers["WWW-Authenticate"]
    )


# ----------------------------------------------------------------- §13 U2·U-Low


def connect_again(env, user, client_id, verifier):
    """같은 client_id 로 인가를 처음부터 다시 한다(에이전트가 다시 로그인한 경우)."""
    page = authorize(env, client_id, CLAUDE_CALLBACK, challenge_for(verifier))
    found = lookup(env, user, user_code(page)).json
    cookie = browser_cookie(page)[:2]
    assert approve(env, user, found["requestId"]).status_code == 200
    code = redirect_params(poll(env, found["requestId"], cookie).json["redirect"])["code"]
    issued = token(
        env,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
    )
    assert issued.status_code == 200, issued.get_data(as_text=True)
    return issued.json


def test_reconnecting_the_same_client_replaces_its_connection(env, alice):
    # §13 U2: 같은 (사용자, client_id) 의 grant 는 하나만 남는다. 이전 토큰은 끊긴다.
    client_id = register_client(env)["client_id"]
    verifier = pkce()[0]
    first = connect_again(env, alice, client_id, verifier)
    second = connect_again(env, alice, client_id, verifier)
    third = connect_again(env, alice, client_id, verifier)
    assert len(connections(env, alice)) == 1
    assert mcp_status(env, first["access_token"]) == 401
    assert mcp_status(env, second["access_token"]) == 401
    assert mcp_status(env, third["access_token"]) == 200
    refreshed = refresh(env, {**first, "client_id": client_id})
    assert refreshed.status_code == 400


def test_different_clients_stay_separate_even_with_the_same_name(env, alice):
    connect(env, alice)
    connect(env, alice)
    assert [agent["name"] for agent in connections(env, alice)] == ["Claude", "Claude"]


def test_reconnecting_does_not_touch_another_users_connection(env, alice):
    bob = signup(env, "bob")
    client_id = register_client(env)["client_id"]
    verifier = pkce()[0]
    bobs = connect_again(env, bob, client_id, verifier)
    connect_again(env, alice, client_id, verifier)
    assert mcp_status(env, bobs["access_token"]) == 200
    assert len(connections(env, bob)) == 1


def test_authorize_page_names_a_local_program_without_nested_brackets(env):
    registered = env.client.post(
        "/oauth/register",
        json={"redirect_uris": [LOOPBACK_CALLBACK], "client_name": "Claude Code"},
    ).json
    html = authorize(env, registered["client_id"], LOOPBACK_CALLBACK, pkce()[1]).get_data(
        as_text=True
    )
    assert "Claude Code — 이 컴퓨터의 프로그램" in html
    assert "((" not in html and "))" not in html


# ----------------------------------------------------------------- 뮤테이션 생존 변이를 잡는 검사
# .omc/plans/mutation-report.md 의 "실제 공백"과 손 변이 생존. 동작으로 쓴다(줄 번호에 기대지 않는다).


def test_starting_a_request_leaves_other_live_requests_alone(env, alice):
    # 만료 요청 정리 기준(now - 10분)이 now + 10분으로 바뀌면 살아 있는 요청까지 지운다.
    registered = register_client(env)
    first = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1])
    env.clock.advance(300)
    authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1])
    found = lookup(env, alice, user_code(first))
    assert found.status_code == 200
    assert poll(env, found.json["requestId"], browser_cookie(first)[:2]).json["status"] == "pending"


def test_a_late_but_valid_exchange_survives_cleanup_in_between(env, alice):
    # 승인했지만 아직 교환하지 않은 grant 는 "요청 만료 + 코드 수명"까지 지우면 안 된다.
    # 그 사이에 다른 등록(정리를 부르는 쓰기)이 끼어도 교환이 된다.
    pending = start(env, alice)
    assert approve(env, alice, pending.request_id).status_code == 200
    env.clock.advance(598)
    code = redirect_params(poll(env, pending.request_id, pending.cookie).json["redirect"])["code"]
    env.clock.advance(59)
    register_client(env)
    assert exchange(env, pending, code).status_code == 200


def test_a_request_expires_exactly_at_ten_minutes(env, alice):
    pending = start(env, alice)
    env.clock.advance(600)
    assert poll(env, pending.request_id, pending.cookie).json["status"] == "expired"


def test_a_code_expires_exactly_at_sixty_seconds(env, alice):
    pending, code = authorization_code(env, alice)
    env.clock.advance(60)
    assert exchange(env, pending, code).status_code == 400


def test_a_refresh_token_expires_exactly_at_thirty_days(env, alice):
    tokens = connect(env, alice)
    env.clock.advance(30 * DAY)
    assert refresh(env, tokens).status_code == 400


def test_revoking_an_access_token_keeps_the_connection(env, alice):
    # RFC 7009: access 를 폐기하면 그 토큰만 끝난다. refresh 는 계속 된다.
    tokens = connect(env, alice)
    env.client.post(
        "/oauth/revoke", data={"token": tokens["access_token"], "client_id": tokens["client_id"]}
    )
    assert refresh(env, tokens).status_code == 200
    assert len(connections(env, alice)) == 1


def test_revoking_a_refresh_token_ends_the_whole_connection(env, alice):
    tokens = connect(env, alice)
    env.client.post(
        "/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": tokens["client_id"]}
    )
    assert mcp_status(env, tokens["access_token"]) == 401
    assert connections(env, alice) == []


def test_authorize_page_csp_is_exactly_the_specified_directives(env):
    registered = register_client(env)
    response = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1])
    directives = {part.strip() for part in response.headers["Content-Security-Policy"].split(";")}
    nonce = re.search(r"'nonce-([^']+)'", response.headers["Content-Security-Policy"]).group(1)
    assert directives - {""} == {
        "default-src 'none'",
        f"script-src 'nonce-{nonce}'",
        f"style-src 'nonce-{nonce}'",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    }


def test_refresh_answers_with_the_connections_current_scope(env, alice):
    tokens = connect(env, alice)
    first = refresh(env, tokens).json
    assert first["scope"] == "jari.read"
    allow_booking_for(env, alice)
    second = refresh(env, {**first, "client_id": tokens["client_id"]}).json
    assert second["scope"] == "jari.book"


@pytest.mark.parametrize(
    ("field", "value"),
    # 빠진 값과 빈 값 둘 다. 빠진 값만 보면 form[...] 의 KeyError 가 같은 400 을 내 검사가 없어도 통과한다.
    [("refresh_token", None), ("client_id", None), ("refresh_token", ""), ("client_id", "")],
)
def test_refresh_without_a_required_field_is_an_invalid_request(env, alice, field, value):
    tokens = connect(env, alice)
    form = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": tokens["client_id"],
    }
    if value is None:
        del form[field]
    else:
        form[field] = value
    response = token(env, form)
    assert response.status_code == 400
    assert oauth_error(response)["error"] == "invalid_request"


@pytest.mark.parametrize("code", [12345678, None, ["ABCD-EFGH"], {"code": 1}])
def test_lookup_with_a_code_that_is_not_text_is_just_not_found(env, alice, code):
    assert lookup(env, alice, code).status_code == 404


@pytest.mark.parametrize("request_id", [123, None, ["x"], {"id": "x"}])
def test_approve_with_a_request_id_that_is_not_text_is_just_not_found(env, alice, request_id):
    response = env.client.post(
        "/api/mobile/agents/requests/approve",
        headers=app_headers(alice),
        json={"requestId": request_id},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "uri", ["http://localhost/cb?next=1", "http://localhost/cb#frag", "http://127.0.0.1:9/cb?x"]
)
def test_loopback_redirects_with_a_query_or_fragment_are_refused(env, uri):
    response = env.client.post("/oauth/register", json={"redirect_uris": [uri]})
    assert response.status_code == 400
    assert oauth_error(response)["error"] == "invalid_redirect_uri"


def test_without_a_cloudflare_header_each_address_has_its_own_budget(env):
    def register_at(address):
        return env.client.post(
            "/oauth/register",
            json={"redirect_uris": [CLAUDE_CALLBACK]},
            environ_base={"REMOTE_ADDR": address},
        ).status_code

    assert [register_at("10.9.0.1") for _ in range(11)] == [201] * 10 + [429]
    assert register_at("10.9.0.2") == 201


def test_the_token_budget_window_is_sixty_seconds(env):
    for _ in range(60):
        token_from(env, "203.0.113.50")
    assert token_from(env, "203.0.113.50") == 429
    env.clock.advance(60)
    assert token_from(env, "203.0.113.50") == 400


def test_deleting_the_account_removes_the_requests_it_held(env, alice):
    # 손 변이: oauth_requests.user_id 의 FK·CASCADE 를 빼면 탈퇴 뒤에도 요청이 남아 poll 이 만료까지 pending 이다.
    pending = start(env, alice)
    env.client.post(
        "/api/mobile/account/delete",
        headers=app_headers(alice),
        json={"password": "a long secure passphrase"},
    )
    assert poll(env, pending.request_id, pending.cookie).status_code == 404


def test_deleting_the_account_clears_its_code_guessing_budget(env, alice):
    # 손 변이: erase_account 가 agent-code 버킷을 지우지 않으면 그 행이 남는다.
    for _ in range(3):
        lookup(env, alice, "ZZZZ-ZZZZ")
    key = digest("agent-code:" + alice["user"]["id"])
    with env.identity.connect() as db:
        assert db.execute("SELECT count(*) FROM rate_limits WHERE key=?", (key,)).fetchone()[0] == 1
    env.client.post(
        "/api/mobile/account/delete",
        headers=app_headers(alice),
        json={"password": "a long secure passphrase"},
    )
    with env.identity.connect() as db:
        assert db.execute("SELECT count(*) FROM rate_limits WHERE key=?", (key,)).fetchone()[0] == 0


def test_a_wrong_method_under_oauth_answers_in_oauth_shape(env):
    # 손 변이: http_error 의 OAuth 분기를 빼면 앱 문구가 나간다.
    response = env.client.get("/oauth/token")
    assert response.status_code == 405
    assert response.get_json() == {"error": "invalid_request"}


def test_an_internal_failure_under_oauth_answers_in_oauth_shape(env, monkeypatch):
    # 손 변이: internal_error 의 OAuth 분기를 빼면 앱 문구가 나간다.
    from korail_bot.mobile import oauth as oauth_module

    def broken(*args, **kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(oauth_module.AgentStore, "register", broken)
    response = env.client.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
    assert response.status_code == 500
    assert response.get_json() == {"error": "server_error"}


def test_the_per_client_token_budget_window_is_sixty_seconds(env):
    client_id = register_client(env)["client_id"]
    form = {"grant_type": "refresh_token", "refresh_token": "x" * 43, "client_id": client_id}
    for n in range(60):
        token_from(env, f"10.8.0.{n}", form)
    assert token_from(env, "10.8.1.0", form) == 429
    env.clock.advance(60)
    assert token_from(env, "10.8.1.1", form) == 400


def test_reconnecting_starts_read_only_again_even_after_booking_was_allowed(env, alice):
    # §15 N8: client_id 는 비밀이 아니다. 다시 인가한 연결이 예약 허용을 이어받으면 피싱 한 번으로
    # 예약 권한까지 넘어간다. 새 연결은 늘 조회만 한다.
    client_id = register_client(env)["client_id"]
    verifier = pkce()[0]
    connect_again(env, alice, client_id, verifier)
    allow_booking_for(env, alice)
    again = connect_again(env, alice, client_id, verifier)
    assert again["scope"] == "jari.read"
    [connection] = connections(env, alice)
    assert connection["allowBooking"] is False
