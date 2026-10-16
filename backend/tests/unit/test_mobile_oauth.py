"""
에이전트 연결의 인가 서버(OAuth 2.1 + DCR + 브라우저 로그인)를 명세대로 고정한다.

명세: .omc/plans/mcp-agent-spec.md §1~§4, §6~§7, §9. 이 테스트들이 지키는 것은
"토큰이 새도 앱 계정 전체가 열리지 않는다", "코드·refresh 가 새도 재사용하면 끊긴다",
"브라우저에서 자리났다 아이디·비밀번호로 로그인해 허용해야만 연결된다"이다.
"""

import re
from pathlib import Path

import pytest

from korail_bot.mobile import identity as identity_module
from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.identity import AuthError, IdentityStore
from korail_bot.mobile.notifications import Notifications
from korail_bot.mobile.oauth import LOCAL_PROGRAM

from .agent_support import (
    CLAUDE_CALLBACK,
    LOOPBACK_CALLBACK,
    PASSWORD,
    PUBLIC,
    RESOURCE,
    Clock,
    Pending,
    allow_booking_for,
    app_headers,
    authorization_code,
    authorize,
    bearer,
    call_tool,
    challenge_for,
    cheap_setup,
    code_from,
    connect,
    csrf_cookie,
    exchange,
    form_fields,
    make_env,
    pkce,
    redirect_params,
    refresh,
    register_client,
    rpc,
    signup,
    start,
    submit,
    token,
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
    ("get", "/.well-known/oauth-protected-resource/mcp"),
    ("get", "/.well-known/oauth-protected-resource"),
    ("get", "/.well-known/oauth-authorization-server"),
    ("post", "/oauth/register"),
    ("get", "/oauth/authorize"),
    ("post", "/oauth/authorize"),
    ("post", "/oauth/token"),
    ("post", "/oauth/revoke"),
    ("post", "/mcp"),
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
    assert body["agents"] == {"mcpUrl": "https://jari.thsvkd.dev/mcp"}
    user = signup(off, "alice")
    assert "agents" not in off.client.post("/api/mobile/bootstrap", headers=app_headers(user)).json


# ----------------------------------------------------------------- §2 메타데이터


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-protected-resource/mcp",
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
    assert env.client.post("/mcp", headers=foreign, json={}).status_code == 403


# ----------------------------------------------------------------- §4.2 DCR


@pytest.mark.parametrize(
    "uris",
    [
        [CLAUDE_CALLBACK],
        ["https://claude.com/api/mcp/auth_callback"],
        ["http://localhost:53682/callback"],
        ["http://127.0.0.1:33418/oauth/callback"],
        ["http://localhost/callback"],
        [
            CLAUDE_CALLBACK,
            "http://localhost:1/a",
            "http://localhost:2/b",
            "http://127.0.0.1/c",
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
        # CSP form-action 출처로 쓸 수 없어 받지 않는다.
        "http://[::1]:8080/cb",
        # urlsplit 은 탭·줄바꿈을 지우고 읽으니, 검사한 주소와 리다이렉트할 주소가 달라진다.
        "http://localhost/cb\r\nX-Injected: 1",
        "http://localhost/c\tb",
        "http://localhost/c b",
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
    code = code_from(submit(env, page, "alice"))
    pending = Pending(registered["client_id"], CLAUDE_CALLBACK, verifier, page)
    assert exchange(env, pending, code).status_code == 200
    [agent] = connections(env, alice)
    shown = agent["name"]
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
            {"resource": "https://other.example/mcp"},
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


def test_authorize_page_asks_for_the_app_account_and_says_who_asked(env):
    registered = register_client(env)
    response = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1])
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    html = response.get_data(as_text=True)
    assert "Claude — claude.ai" in html
    assert "조회만 허용합니다. 예약은 앱의 설정 → 에이전트 연결에서 켤 수 있어요." in html
    # 브라우저 비밀번호 관리자가 채울 수 있어야 한다.
    assert 'name="username" autocomplete="username"' in html
    assert 'type="password" name="password" autocomplete="current-password"' in html
    assert '<form method="post" action="/oauth/authorize">' in html
    assert ">로그인하고 허용</button>" in html
    # 거절에는 비밀번호가 필요 없다: 브라우저 검사를 건너뛴다.
    assert 'value="deny" formnovalidate>거절</button>' in html


def test_pending_member_cannot_authorize_an_agent(env):
    pending_user = env.client.post(
        "/api/mobile/auth/register",
        json={"username": "waiting", "password": PASSWORD},
    ).json
    pending = start(env)
    response = submit(env, pending.page, pending_user["user"]["username"])
    assert response.status_code == 403
    assert "승인" in response.get_data(as_text=True)


def test_lost_approval_blocks_existing_mcp_and_refresh_tokens(env, alice):
    tokens = connect(env, alice)
    with env.identity.connect() as db:
        db.execute("UPDATE users SET status='rejected' WHERE id=?", (alice["user"]["id"],))
    assert mcp_status(env, tokens["access_token"]) == 401
    response = refresh(env, tokens)
    assert response.status_code == 400
    assert oauth_error(response)["error"] == "invalid_grant"


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
        "base-uri 'none'",
        "form-action 'self' https://claude.ai",
        "frame-ancestors 'none'",
    ):
        assert directive in csp
    assert "script-src" not in csp
    nonce = re.search(r"style-src 'nonce-([^']+)'", csp)
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


def test_state_returns_to_the_client_exactly_as_it_came(env, alice):
    # state 는 hidden 필드로 폼을 지나 리다이렉트에 그대로 돌아간다(이스케이프는 페이지 안에서만).
    registered = register_client(env)
    hostile = "a\"b<c>&d ' e"
    page = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1], state=hostile)
    assert form_fields(page)["state"] == hostile
    assert redirect_params(submit(env, page, "alice").headers["Location"])["state"] == hostile


def test_client_name_in_the_page_is_escaped(env):
    hostile = '<img src=x onerror="alert(1)">'
    registered = env.client.post(
        "/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK], "client_name": hostile}
    ).json
    html = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1]).get_data(
        as_text=True
    )
    assert "<img" not in html


def test_csrf_cookie_is_host_only_secure_and_unreadable_by_scripts(env):
    registered = register_client(env)
    name, value, attributes = csrf_cookie(
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
    name, _, attributes = csrf_cookie(
        authorize(
            env,
            registered["client_id"],
            CLAUDE_CALLBACK,
            pkce()[1],
            resource="http://127.0.0.1:18281/mcp",
        )
    )
    assert name == "jari_oauth"
    assert "secure" not in attributes
    assert "httponly" in attributes and "samesite=lax" in attributes


# ----------------------------------------------------------------- §4.4 로그인과 동의


def grant_count(env):
    with env.identity.connect() as db:
        return db.execute("SELECT count(*) FROM oauth_grants").fetchone()[0]


def session_count(env):
    with env.identity.connect() as db:
        return db.execute("SELECT count(*) FROM sessions").fetchone()[0]


@pytest.mark.parametrize(
    "cookie",
    [None, "x" * 43, "", "한글"],
    ids=["no-cookie", "other-value", "empty", "not-ascii"],
)
def test_a_form_without_its_own_csrf_cookie_is_refused(env, alice, cookie):
    pending = start(env)
    form = {**form_fields(pending.page), "username": "alice", "password": PASSWORD}
    form["decision"] = "allow"
    name = csrf_cookie(pending.page)[0]
    response = env.client.post(
        "/oauth/authorize",
        data=form,
        headers={"Cookie": f"{name}={cookie}"} if cookie is not None else {},
    )
    assert response.status_code == 400
    assert "Location" not in response.headers
    assert response.mimetype == "text/html"
    assert grant_count(env) == 0


def test_a_form_without_the_csrf_field_is_refused_even_with_the_cookie(env, alice):
    pending = start(env)
    response = submit(env, pending.page, "alice", csrf=None)
    assert response.status_code == 400
    assert "Location" not in response.headers


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_form_sent_from_another_site_is_refused_even_with_the_right_csrf(env, alice, site):
    pending = start(env)
    response = submit(env, pending.page, "alice", headers={"Sec-Fetch-Site": site})
    assert response.status_code == 400
    assert grant_count(env) == 0


def test_a_csrf_value_from_another_page_is_refused(env, alice):
    first, second = start(env), start(env)
    response = submit(env, first.page, "alice", csrf=form_fields(second.page)["csrf"])
    assert response.status_code == 400
    assert grant_count(env) == 0


def test_each_page_gets_a_fresh_csrf_value_in_cookie_and_form(env):
    pages = [start(env).page for _ in range(3)]
    values = {csrf_cookie(page)[1] for page in pages}
    assert len(values) == 3
    for page in pages:
        assert form_fields(page)["csrf"] == csrf_cookie(page)[1]


@pytest.mark.parametrize(
    ("redirect_uri", "used", "origin"),
    [
        (CLAUDE_CALLBACK, CLAUDE_CALLBACK, "https://claude.ai"),
        (LOOPBACK_CALLBACK, LOOPBACK_CALLBACK, "http://localhost:53682"),
        (LOOPBACK_CALLBACK, "http://localhost:61999/callback", "http://localhost:61999"),
        ("http://127.0.0.1/cb", "http://127.0.0.1:8123/cb", "http://127.0.0.1:8123"),
    ],
)
def test_csp_form_action_lets_the_redirect_back_to_the_client_through(
    env, redirect_uri, used, origin
):
    # Chrome 은 폼 제출 뒤 따라가는 리다이렉트에도 form-action 을 적용한다.
    registered = register_client(env, [redirect_uri])
    page = authorize(env, registered["client_id"], used, pkce()[1])
    assert page.status_code == 200
    assert f"form-action 'self' {origin};" in page.headers["Content-Security-Policy"]


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("alice", "a wrong long passphrase"),
        ("nobody", PASSWORD),
        ("Alice", "short"),
        ("a", PASSWORD),
        ("", ""),
    ],
)
def test_a_failed_sign_in_shows_the_same_form_and_says_nothing_more(env, alice, username, password):
    # 계정이 있는지 없는지는 드러내지 않는다: 같은 401, 같은 문구, 아이디는 채운 채로.
    pending = start(env)
    response = submit(env, pending.page, username, password)
    assert response.status_code == 401
    assert "Location" not in response.headers
    html = response.get_data(as_text=True)
    assert "아이디나 비밀번호를 다시 확인해 주세요." in html
    assert f'required value="{username}"' in html
    assert not password or password not in html
    # 다시 보낼 수 있는 새 폼이다.
    again = submit(env, response, "alice")
    assert again.status_code == 302
    assert grant_count(env) == 1


def test_an_unknown_account_spends_the_same_password_check(env, monkeypatch):
    # 없는 계정도 미리 만든 해시와 대조한다. 바로 거절하면 걸린 시간으로 계정 유무가 드러난다.
    checked = []
    original = identity_module.check_password_hash

    def spy(encoded, password):
        checked.append(encoded)
        return original(encoded, password)

    monkeypatch.setattr(identity_module, "check_password_hash", spy)
    with pytest.raises(AuthError):
        env.identity.verify("nobody", PASSWORD)
    assert checked == [identity_module._DUMMY_HASH]


def test_an_app_id_in_another_case_signs_in_like_the_app(env, alice):
    pending = start(env)
    assert submit(env, pending.page, "ALICE").status_code == 302


def test_signing_in_here_starts_no_app_session(env, alice):
    before = session_count(env)
    pending, code = authorization_code(env, alice)
    assert exchange(env, pending, code).status_code == 200
    assert session_count(env) == before


def login_from(env, ip, username, password="a wrong long passphrase"):
    return env.client.post(
        "/api/mobile/auth/login",
        headers={"CF-Connecting-IP": ip},
        json={"username": username, "password": password},
    ).status_code


def test_the_browser_shares_the_app_logins_budget_per_account(env, alice):
    # auth-user:alice 10회/5분을 앱 로그인과 같이 쓴다(가입이 1회 썼다).
    assert [login_from(env, "10.6.0.1", "alice") for _ in range(5)] == [401] * 5
    for n in range(4):
        page = start(env).page
        wrong = submit(env, page, "alice", "a wrong long passphrase", headers=ip(f"10.6.1.{n}"))
        assert wrong.status_code == 401
    pending = start(env)
    refused = submit(env, pending.page, "alice", headers=ip("10.6.2.1"))
    assert refused.status_code == 429
    assert "Location" not in refused.headers
    assert "요청이 너무 많아요. 잠시 후 다시 시도해 주세요." in refused.get_data(as_text=True)
    assert grant_count(env) == 0
    env.clock.advance(301)
    assert submit(env, refused, "alice", headers=ip("10.6.2.1")).status_code == 302


def test_the_browser_shares_the_app_logins_budget_per_address(env, alice):
    assert [login_from(env, "10.7.0.1", f"user{n}") for n in range(10)] == [401] * 10
    pending = start(env)
    assert submit(env, pending.page, "alice", headers=ip("10.7.0.1")).status_code == 429
    assert login_from(env, "10.7.0.1", "alice", PASSWORD) == 429


def test_the_browser_shares_the_app_logins_overall_budget(env, alice):
    # auth-any 60회/5분. 가입이 1회 썼다.
    statuses = [login_from(env, f"10.8.{n // 50}.{n % 50}", f"user{n}") for n in range(59)]
    assert statuses == [401] * 59
    pending = start(env)
    assert submit(env, pending.page, "alice", headers=ip("10.8.9.9")).status_code == 429


def ip(address):
    return {"CF-Connecting-IP": address}


def test_deny_returns_access_denied_without_a_password(env):
    pending = start(env)
    response = submit(env, pending.page, None, None, decision="deny")
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith(CLAUDE_CALLBACK + "?")
    params = redirect_params(location)
    assert params["error"] == "access_denied"
    assert params["state"] == "state-123"
    assert params["iss"] == PUBLIC
    assert "code" not in params
    assert grant_count(env) == 0


def test_allow_returns_a_code_with_state_and_issuer(env, alice):
    pending = start(env)
    response = submit(env, pending.page, "alice")
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith(CLAUDE_CALLBACK + "?")
    params = redirect_params(location)
    assert set(params) == {"code", "state", "iss"}
    assert params["state"] == "state-123"
    assert params["iss"] == PUBLIC
    assert len(params["code"]) >= 43


def test_a_loopback_client_gets_its_code_on_the_port_it_opened(env, alice):
    registered = register_client(env, [LOOPBACK_CALLBACK])
    page = authorize(env, registered["client_id"], "http://localhost:61999/callback", pkce()[1])
    location = submit(env, page, "alice").headers["Location"]
    assert location.startswith("http://localhost:61999/callback?code=")


def test_a_request_without_state_gets_none_back(env, alice):
    registered = register_client(env)
    page = authorize(env, registered["client_id"], CLAUDE_CALLBACK, pkce()[1], state=None)
    assert "state" not in form_fields(page)
    assert "state" not in redirect_params(submit(env, page, "alice").headers["Location"])


@pytest.mark.parametrize(
    "field",
    [
        {"redirect_uri": "https://evil.example/callback"},
        {"client_id": "no-such-client"},
        {"redirect_uri": None},
    ],
)
def test_a_tampered_form_that_cannot_be_vouched_for_is_an_error_page(env, alice, field):
    # POST 도 GET 과 같은 검사를 다시 한다. 리다이렉트할 곳을 믿을 수 없으면 오류 페이지.
    pending = start(env)
    response = submit(env, pending.page, "alice", **field)
    assert response.status_code == 400
    assert "Location" not in response.headers
    assert grant_count(env) == 0


@pytest.mark.parametrize(
    ("field", "error"),
    [
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": "short"}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"resource": "https://other.example/mcp"}, "invalid_target"),
        ({"scope": "bad\\scope"}, "invalid_scope"),
    ],
)
def test_a_tampered_form_returns_the_error_to_the_client(env, alice, field, error):
    pending = start(env)
    response = submit(env, pending.page, "alice", **field)
    assert response.status_code == 302
    params = redirect_params(response.headers["Location"])
    assert params["error"] == error
    assert "code" not in params
    assert grant_count(env) == 0


def test_the_form_post_spends_the_authorize_budget_too(env, alice):
    pending = start(env)  # 1회
    for _ in range(59):
        authorize(env, pending.client_id, CLAUDE_CALLBACK, pkce()[1])
    response = submit(env, pending.page, "alice")
    assert response.status_code == 429
    assert "Location" not in response.headers


def test_a_new_connection_only_reads_whatever_the_client_asked_for(env, alice):
    # 클라이언트는 authorize 에서 "jari.read jari.book" 을 요청한다(agent_support.authorize_query).
    # §12 S-H1: 새 연결은 늘 jari.read 로 시작한다.
    tokens = connect(env, alice)
    assert tokens["scope"].split() == ["jari.read"]
    assert connections(env, alice)[0]["allowBooking"] is False


def test_an_admin_account_can_connect_too(env):
    env.identity.ensure_admin("root", PASSWORD)
    pending = start(env)
    assert submit(env, pending.page, "root").status_code == 302


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


def test_tokens_and_codes_are_stored_only_as_hashes(env, alice):
    pending, code = authorization_code(env, alice)
    tokens = exchange(env, pending, code).json
    # WAL 모드라 최근 쓰기는 -wal 파일에 있다.
    raw = b"".join(
        Path(env.identity.path + suffix).read_bytes()
        for suffix in ("", "-wal")
        if Path(env.identity.path + suffix).exists()
    )
    for value in (code, tokens["access_token"], tokens["refresh_token"]):
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
    response = exchange(env, pending, code, resource="https://other.example/mcp")
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
    pending, code = authorization_code(env, alice)
    deleted = env.client.post(
        "/api/mobile/account/delete",
        headers=app_headers(alice),
        json={"password": PASSWORD},
    )
    assert deleted.status_code == 200
    assert mcp_status(env, tokens["access_token"]) == 401
    assert refresh(env, tokens).status_code == 400
    # 탈퇴 전에 받아 둔 권한 코드도 함께 지워져 교환되지 않는다.
    assert exchange(env, pending, code).status_code == 400
    with env.identity.connect() as db:
        assert db.execute("SELECT count(*) FROM oauth_codes").fetchone()[0] == 0


def test_agent_routes_need_the_app_session(env):
    assert env.client.get("/api/mobile/agents").status_code == 401
    assert env.client.delete("/api/mobile/agents/x").status_code == 401
    assert env.client.post("/api/mobile/agents/x", json={"allowBooking": True}).status_code == 401


def test_metadata_urls_follow_the_configured_public_url(tmp_path):
    env = make_env(tmp_path, public_url="https://jari.thsvkd.dev")
    prm = env.client.get("/.well-known/oauth-protected-resource/mcp").json
    assert prm["resource"] == "https://jari.thsvkd.dev/mcp"
    unauthenticated = env.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert (
        'resource_metadata="https://jari.thsvkd.dev/.well-known/oauth-protected-resource/mcp"'
        in unauthenticated.headers["WWW-Authenticate"]
    )


# ----------------------------------------------------------------- §13 U2·U-Low


def connect_again(env, user, client_id, verifier):
    """같은 client_id 로 인가를 처음부터 다시 한다(에이전트가 다시 로그인한 경우)."""
    page = authorize(env, client_id, CLAUDE_CALLBACK, challenge_for(verifier))
    code = code_from(submit(env, page, user["user"]["username"]))
    issued = exchange(env, Pending(client_id, CLAUDE_CALLBACK, verifier, page), code)
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


def test_a_late_but_valid_exchange_survives_cleanup_in_between(env, alice):
    # 허용했지만 아직 교환하지 않은 grant 는 코드 수명(60초)까지 지우면 안 된다.
    # 그 사이에 다른 등록(정리를 부르는 쓰기)이 끼어도 교환이 된다.
    pending, code = authorization_code(env, alice)
    env.clock.advance(59)
    register_client(env)
    assert exchange(env, pending, code).status_code == 200


def test_a_connection_never_exchanged_is_cleaned_up_after_its_code(env, alice):
    authorization_code(env, alice)
    env.clock.advance(61)
    register_client(env)
    assert grant_count(env) == 0


def test_a_code_replayed_half_an_hour_later_still_ends_the_connection(env, alice):
    # 쓴 코드는 만료 뒤 한 시간 남겨 둔다. 그 사이의 정리가 지우면 재사용을 알아채지 못한다.
    pending, code = authorization_code(env, alice)
    first = exchange(env, pending, code).json
    env.clock.advance(1800)
    register_client(env)
    assert exchange(env, pending, code).status_code == 400
    assert mcp_status(env, first["access_token"]) == 401
    assert connections(env, alice) == []


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
        f"style-src 'nonce-{nonce}'",
        "base-uri 'none'",
        "form-action 'self' https://claude.ai",
        "frame-ancestors 'none'",
    }


def test_an_error_page_allows_no_form_at_all(env):
    response = authorize(env, "no-such-client", CLAUDE_CALLBACK, pkce()[1])
    assert "form-action 'none'" in response.headers["Content-Security-Policy"]


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


def test_restarting_on_a_database_from_the_device_code_flow_keeps_connections(tmp_path):
    # 운영 DB 에는 장치 코드 흐름의 oauth_requests 표가 남아 있다. 시작할 때 그 표만 지우고
    # 클라이언트·연결·토큰은 그대로 둔다.
    first = make_env(tmp_path)
    user = signup(first, "alice")
    tokens = connect(first, user)
    with first.identity.connect() as db:
        db.execute(
            "CREATE TABLE oauth_requests (id TEXT PRIMARY KEY, user_code_hash TEXT UNIQUE NOT NULL,"
            " user_id TEXT REFERENCES users(id) ON DELETE CASCADE, status TEXT)"
        )
        db.execute(
            "INSERT INTO oauth_requests VALUES ('r1', 'h', ?, 'pending')", (user["user"]["id"],)
        )
    again = make_env(tmp_path, clock=first.clock)
    with again.identity.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "oauth_requests" not in tables
    assert {"oauth_clients", "oauth_grants", "oauth_tokens", "oauth_codes"} <= tables
    assert mcp_status(again, tokens["access_token"]) == 200
    assert refresh(again, tokens).status_code == 200
    assert len(connections(again, user)) == 1


# ----------------------------------------------------------------- 새 연결 알림


def alerted_env(tmp_path, **kwargs):
    identity = IdentityStore(tmp_path / "app.sqlite3", clock=Clock())
    inbox = Notifications(identity, secret="a" * 40, **kwargs)
    return make_env(tmp_path, identity=identity, clock=identity.clock, notifications=inbox), inbox


def agent_alerts(env, inbox, user):
    owner = env.identity.find_user(user["user"]["username"])["storage_id"]
    return [item["text"] for item in inbox.items(owner) if item["kind"] == "agent"]


@pytest.mark.parametrize(
    ("redirect_uri", "where"), [(CLAUDE_CALLBACK, "claude.ai"), (LOOPBACK_CALLBACK, LOCAL_PROGRAM)]
)
def test_a_new_connection_tells_only_its_owner(tmp_path, redirect_uri, where):
    # 피싱 대응: 남의 클라이언트로 시작한 링크에 로그인했어도 앱 알림으로 바로 안다.
    env, inbox = alerted_env(tmp_path)
    alice, bob = signup(env, "alice"), signup(env, "bob")
    pending, code = authorization_code(env, alice, redirect_uri=redirect_uri)
    assert exchange(env, pending, code).status_code == 200
    [text] = agent_alerts(env, inbox, alice)
    assert f"Claude ({where})" in text
    assert "설정 → 에이전트 연결" in text
    assert agent_alerts(env, inbox, bob) == []


def test_refreshing_or_failing_an_exchange_tells_nobody(tmp_path):
    env, inbox = alerted_env(tmp_path)
    alice = signup(env, "alice")
    pending, code = authorization_code(env, alice)
    assert exchange(env, pending, code, code_verifier="w" * 64).status_code == 400
    assert agent_alerts(env, inbox, alice) == []
    tokens = {**exchange(env, pending, code).json, "client_id": pending.client_id}
    assert refresh(env, tokens).status_code == 200
    # 코드를 다시 쓰면 연결이 끊길 뿐 새 연결 알림은 없다.
    assert exchange(env, pending, code).status_code == 400
    assert len(agent_alerts(env, inbox, alice)) == 1


def test_a_failing_alert_still_issues_the_tokens(tmp_path, monkeypatch):
    env, inbox = alerted_env(tmp_path)
    alice = signup(env, "alice")

    def broken(*args, **kwargs):
        raise RuntimeError("inbox down")

    monkeypatch.setattr(inbox, "publish", broken)
    tokens = connect(env, alice)
    assert mcp_status(env, tokens["access_token"]) == 200


def test_the_alert_is_queued_for_push(tmp_path):
    env, inbox = alerted_env(tmp_path, push_enabled=True)
    alice = signup(env, "alice")
    owner = env.identity.find_user("alice")["storage_id"]
    inbox.register_device(owner, "alice-phone")
    connect(env, alice)
    with env.identity.connect() as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
