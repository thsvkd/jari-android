"""
에이전트 연결(OAuth + MCP) 테스트가 같이 쓰는 손잡이.

명세: .omc/plans/mcp-agent-spec.md. 앱 API 테스트(test_mobile_api.py)처럼 실제
IdentityStore 와 MagicMock 게이트웨이로 create_app 을 만들고, 시계는 IdentityStore 에
주입해 움직인다. 서버는 그 시계(identity.clock)로 만료를 판단해야 한다.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlencode, urlsplit

from korail_bot.mobile import identity as identity_module
from korail_bot.mobile.api import create_app
from korail_bot.mobile.identity import IdentityStore

PUBLIC = "https://jari.example"
RESOURCE = PUBLIC + "/api/mobile/mcp"
PRM_URL = PUBLIC + "/.well-known/oauth-protected-resource/api/mobile/mcp"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK_CALLBACK = "http://localhost:53682/callback"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
USER_CODE = re.compile(rf"\b([{CODE_ALPHABET}]{{4}})-([{CODE_ALPHABET}]{{4}})\b")
PROTOCOL = "2025-06-18"
BOOK_HINT = "예약까지 맡기기"


class Clock:
    """IdentityStore(clock=...) 에 넣는 손으로 움직이는 시계."""

    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@dataclass
class Env:
    client: object
    identity: IdentityStore
    gateway: MagicMock
    clock: Clock
    app: object


def cheap_setup(monkeypatch):
    """
    테스트 준비만 싸게 한다. 검사하는 동작과 경계값은 그대로다.

    - 가입 한 번의 scrypt(32768:8:3) 가 이 PC 에서 약 1초였다. 이 테스트들은 비밀번호 저장
      방식을 다루지 않으니 싼 해시로 바꾼다. 저장 방식 자체는 test_mobile_auth.py 가 지킨다.
    - IdentityStore 는 쓰기마다 연결을 새로 열고 커밋한다. WAL 커밋은 이 PC 에서 동기화를 꺼도
      한 번에 약 4ms, 켜면 약 14ms 라, 레이트리밋 경계를 재는 테스트(요청 수백 번)가 수십 초
      걸렸다. 버리는 임시 DB 이고 테스트가 한 스레드에서 차례로 부르니 저널을 메모리에 두고
      동기화를 끈다(약 1ms). 트랜잭션·제약 조건의 의미는 같고, 바뀌는 것은 정전 내구성과
      여러 연결이 동시에 읽고 쓸 때의 동작뿐인데 이 테스트들은 그것을 다루지 않는다.
    """
    monkeypatch.setattr(identity_module, "PASSWORD_METHOD", "pbkdf2:sha256:1")
    connect = sqlite3.connect

    def scratch(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.execute("PRAGMA journal_mode=MEMORY")
        db.execute("PRAGMA synchronous=OFF")
        return db

    monkeypatch.setattr(sqlite3, "connect", scratch)


def gateway_mock():
    gateway = MagicMock()
    gateway.bootstrap.return_value = {"running": None}
    gateway.status.return_value = {"running": None, "scheduled": None, "pending": []}
    gateway.start_search.return_value = {"started": True}
    # §13 U1: 같은 날짜·구간으로 받은 마지막 열차 목록의 번호. start_watch 테스트가 쓰는 번호를 담아 둔다.
    gateway.listed_trains.return_value = ["00101", "00103"]
    gateway.schedule_search.return_value = {
        "scheduled": True,
        "startAt": "2026-10-10T07:00:00+09:00",
    }
    gateway.cancel_search.return_value = {"stopped": True, "unscheduled": False}
    gateway.cancel_pending.return_value = {"cancelled": True, "pending": []}
    gateway.favourites.return_value = []
    gateway.list_trains.return_value = {
        "trains": [
            {
                "no": "00101",
                "label": "07:00→09:41 KTX",
                "dep_time": "070000",
                "arr_time": "094100",
                "name": "KTX",
                "soldout": False,
                "waitlistEligible": False,
                "trainKey": "T1",
                "generalAvailable": True,
                "specialAvailable": False,
            }
        ],
        "truncated": False,
        "passengerCount": 1,
    }
    gateway.seat_inventories.return_value = {
        "inventories": [],
        "failedCars": [],
        "layoutReference": False,
    }
    gateway.reserve_designated.return_value = {
        "reserved": True,
        "expiresAt": "2026-10-10T07:10:00+00:00",
        "paymentUrl": "https://www.korail.com/ticket/myticket/list",
    }
    return gateway


def make_env(tmp_path, *, public_url=PUBLIC, clock=None, identity=None, gateway=None, **kwargs):
    clock = clock or Clock()
    identity = identity or IdentityStore(tmp_path / "app.sqlite3", clock=clock)
    gateway = gateway or gateway_mock()
    # 명세 §1: 공개 주소는 create_app 의 public_url 키워드로 넘긴다. 없으면 기능 전체가 꺼진다.
    if public_url is None:
        app = create_app(identity, gateway, **kwargs)
    else:
        app = create_app(identity, gateway, public_url=public_url, **kwargs)
    app.testing = True
    # 쿠키는 테스트가 직접 보낸다. 브라우저 쿠키 없이 poll 하는 경우를 따로 검사해야 해서다.
    return Env(app.test_client(use_cookies=False), identity, gateway, clock, app)


def signup(env, username):
    response = env.client.post(
        "/api/mobile/auth/register",
        json={
            "username": username,
            "password": "a long secure passphrase",
            "invite": env.identity.create_invite(),
        },
    )
    assert response.status_code == 200, response.json
    return response.json


def app_headers(user):
    return {"Authorization": "Bearer " + user["token"]}


def bearer(token):
    return {"Authorization": "Bearer " + token}


def pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, challenge_for(verifier)


def register_client(env, redirect_uris=(CLAUDE_CALLBACK,), **extra):
    response = env.client.post(
        "/oauth/register",
        json={"redirect_uris": list(redirect_uris), "client_name": "Claude", **extra},
    )
    assert response.status_code == 201, (response.status_code, response.get_data(as_text=True))
    return response.json


def authorize_query(client_id, redirect_uri, challenge, **overrides):
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-123",
        "resource": RESOURCE,
        "scope": "jari.read jari.book",
    }
    query.update(overrides)
    return {key: value for key, value in query.items() if value is not None}


def authorize(env, client_id, redirect_uri, challenge, *, headers=None, **overrides):
    query = authorize_query(client_id, redirect_uri, challenge, **overrides)
    return env.client.get("/oauth/authorize?" + urlencode(query), headers=headers or {})


def user_code(response):
    match = USER_CODE.search(response.get_data(as_text=True))
    assert match, "인가 페이지에 ABCD-EFGH 형식의 코드가 없어요"
    return f"{match.group(1)}-{match.group(2)}"


def browser_cookie(response):
    """인가 페이지가 준 브라우저 비밀 쿠키 (이름, 값, 속성 문자열 소문자)."""
    for header in response.headers.getlist("Set-Cookie"):
        name, _, rest = header.partition("=")
        if name in {"__Host-jari_oauth", "jari_oauth"}:
            value, _, attributes = rest.partition(";")
            return name, value, attributes.lower()
    raise AssertionError("인가 페이지가 브라우저 쿠키를 주지 않았어요")


def poll(env, request_id, cookie=None):
    headers = {"Cookie": f"{cookie[0]}={cookie[1]}"} if cookie else {}
    return env.client.get(
        "/oauth/authorize/poll?" + urlencode({"request": request_id}), headers=headers
    )


def lookup(env, user, code, headers=None):
    return env.client.post(
        "/api/mobile/agents/requests/lookup",
        headers={**app_headers(user), **(headers or {})},
        json={"code": code},
    )


def approve(env, user, request_id):
    # §12 S-H1: 승인은 예약 허용을 받지 않는다. 새 연결은 늘 jari.read 다.
    return env.client.post(
        "/api/mobile/agents/requests/approve",
        headers=app_headers(user),
        json={"requestId": request_id},
    )


def allow_booking_for(env, user, on=True):
    """연결 목록에서 예약 허용을 바꾼다. 연결은 하나라고 가정한다."""
    [agent] = env.client.get("/api/mobile/agents", headers=app_headers(user)).json["agents"]
    response = env.client.post(
        f"/api/mobile/agents/{agent['id']}", headers=app_headers(user), json={"allowBooking": on}
    )
    assert response.status_code == 200, response.get_data(as_text=True)


def token(env, form):
    return env.client.post("/oauth/token", data=form)


def redirect_params(url):
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


@dataclass
class Pending:
    client_id: str
    redirect_uri: str
    verifier: str
    code: str
    cookie: tuple
    request_id: str


def challenge_for(verifier):
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def start(env, user, redirect_uri=CLAUDE_CALLBACK, verifier=None):
    """DCR → authorize → 앱 lookup 까지. 승인 직전 상태를 돌려준다."""
    registered = register_client(env, [redirect_uri])
    verifier = verifier or pkce()[0]
    challenge = challenge_for(verifier)
    page = authorize(env, registered["client_id"], redirect_uri, challenge)
    assert page.status_code == 200, page.get_data(as_text=True)
    code = user_code(page)
    found = lookup(env, user, code)
    assert found.status_code == 200, found.json
    return Pending(
        registered["client_id"],
        redirect_uri,
        verifier,
        code,
        browser_cookie(page)[:2],
        found.json["requestId"],
    )


def authorization_code(env, user, *, redirect_uri=CLAUDE_CALLBACK, verifier=None):
    """승인하고 브라우저 poll 로 권한 코드를 받는다."""
    pending = start(env, user, redirect_uri, verifier)
    assert approve(env, user, pending.request_id).status_code == 200
    polled = poll(env, pending.request_id, pending.cookie)
    assert polled.status_code == 200 and polled.json["status"] == "approved", polled.json
    params = redirect_params(polled.json["redirect"])
    return pending, params["code"]


def exchange(env, pending, code, **overrides):
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "client_id": pending.client_id,
        "code_verifier": pending.verifier,
        "resource": RESOURCE,
    }
    form.update(overrides)
    return token(env, {key: value for key, value in form.items() if value is not None})


def connect(env, user, *, allow_booking=False):
    """
    전체 흐름. 토큰 응답 JSON 에 client_id 를 더해 돌려준다.
    allow_booking 이면 연결한 뒤 목록에서 예약 허용을 켠다(§12 S-H1, 시트에는 토글이 없다).
    """
    pending, code = authorization_code(env, user)
    issued = exchange(env, pending, code)
    assert issued.status_code == 200, issued.get_data(as_text=True)
    if allow_booking:
        allow_booking_for(env, user)
    return {**issued.json, "client_id": pending.client_id}


def refresh(env, tokens, refresh_token=None):
    return token(
        env,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token or tokens["refresh_token"],
            "client_id": tokens["client_id"],
        },
    )


def rpc(env, access_token, method, params=None, *, id=1, headers=None):
    message = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        message["id"] = id
    if params is not None:
        message["params"] = params
    return env.client.post(
        "/api/mobile/mcp",
        json=message,
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
            **(bearer(access_token) if access_token else {}),
            **(headers or {}),
        },
    )


def call_tool(env, access_token, name, arguments=None):
    response = rpc(env, access_token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200, (response.status_code, response.get_data(as_text=True))
    assert "error" not in response.json, response.json
    return response.json["result"]


def tool_text(result):
    return "\n".join(item.get("text", "") for item in result.get("content", []))
