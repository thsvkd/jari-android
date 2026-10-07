"""
에이전트 연결 한 흐름을 실제 서버·워커·저장소와 가짜 코레일로 끝까지 따라가요.

MCP 클라이언트가 하는 순서 그대로예요: 토큰 없이 부름 → 401 의 메타데이터 주소 →
DCR → 브라우저 인가 페이지(코드·쿠키) → 사용자가 앱에서 코드 입력·승인 → 브라우저 poll →
토큰 교환 → MCP initialize → tools/list → find_trains → start_watch → refresh 회전.
명세: .omc/plans/mcp-agent-spec.md. 스택은 MOBILE_PUBLIC_URL 을 API 주소로 띄워요.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit

CALLBACK = "http://127.0.0.1:53682/callback"
CODE = re.compile(
    r"\b([ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4})-([ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4})\b"
)


class Reply:
    def __init__(self, status: int, headers, body: bytes):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body or b"{}")


def send(method: str, url: str, *, data: bytes | None = None, headers: dict | None = None) -> Reply:
    request = urllib.request.Request(url, data, headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:
            return Reply(reply.status, reply.headers, reply.read())
    except urllib.error.HTTPError as failure:
        with failure:
            return Reply(failure.code, failure.headers, failure.read())


def post_json(url: str, body, headers: dict | None = None) -> Reply:
    return send(
        "POST",
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def post_form(url: str, form: dict) -> Reply:
    return send(
        "POST",
        url,
        data=urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


class Agent:
    """MCP 클라이언트 흉내. 브라우저 쿠키도 이 손잡이가 들고 있어요."""

    def __init__(self, api: str):
        self.api = api
        self.resource = api + "/api/mobile/mcp"
        self.verifier = secrets.token_urlsafe(48)
        self.cookie = ""
        self.ids = iter(range(1, 1000))

    def challenge(self) -> str:
        digest = hashlib.sha256(self.verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def register(self) -> None:
        reply = post_json(
            self.api + "/oauth/register",
            {"redirect_uris": [CALLBACK], "client_name": "e2e 에이전트"},
        )
        assert reply.status == 201, reply.body
        self.client_id = reply.json()["client_id"]

    def open_browser(self) -> str:
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": self.challenge(),
            "code_challenge_method": "S256",
            "state": "e2e-state",
            "resource": self.resource,
            "scope": "jari.read jari.book",
        }
        page = send("GET", self.api + "/oauth/authorize?" + urlencode(query))
        assert page.status == 200, page.body
        for header in page.headers.get_all("Set-Cookie") or []:
            if header.startswith("jari_oauth="):
                self.cookie = header.split(";", 1)[0]
        assert self.cookie, "인가 페이지가 브라우저 쿠키를 주지 않았어요"
        match = CODE.search(page.body.decode())
        assert match, "인가 페이지에 코드가 없어요"
        return f"{match.group(1)}-{match.group(2)}"

    def poll(self, request_id: str) -> dict:
        reply = send(
            "GET",
            self.api + "/oauth/authorize/poll?" + urlencode({"request": request_id}),
            headers={"Cookie": self.cookie},
        )
        assert reply.status == 200, reply.body
        return reply.json()

    def exchange(self, code: str) -> dict:
        reply = post_form(
            self.api + "/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CALLBACK,
                "client_id": self.client_id,
                "code_verifier": self.verifier,
                "resource": self.resource,
            },
        )
        assert reply.status == 200, reply.body
        self.tokens = reply.json()
        return self.tokens

    def refresh(self, refresh_token: str) -> Reply:
        return post_form(
            self.api + "/oauth/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
            },
        )

    def rpc(self, method: str, params: dict | None = None, *, notify: bool = False) -> Reply:
        message = {"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}
        if not notify:
            message["id"] = next(self.ids)
        return post_json(
            self.resource,
            message,
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
                "Authorization": "Bearer " + self.tokens["access_token"],
            },
        )

    def tool(self, name: str, arguments: dict | None = None) -> dict:
        reply = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        assert reply.status == 200, reply.body
        return reply.json()["result"]


def connect(stack, client, *, allow_booking: bool) -> Agent:
    agent = Agent(stack["api"])
    agent.register()
    code = agent.open_browser()
    found = client.call("POST", "/agents/requests/lookup", {"code": code.lower()})
    assert found["client"]["local"] is True
    assert agent.poll(found["requestId"])["status"] == "pending"
    # §12 S-H1: 승인은 늘 읽기 전용이다. 예약 허용은 연결한 뒤 목록에서 켠다.
    client.call("POST", "/agents/requests/approve", {"requestId": found["requestId"]})
    approved = agent.poll(found["requestId"])
    assert approved["status"] == "approved"
    params = parse_qs(urlsplit(approved["redirect"]).query)
    assert params["state"] == ["e2e-state"]
    assert params["iss"] == [stack["api"]]
    agent.exchange(params["code"][0])
    assert agent.tokens["scope"].split() == ["jari.read"]
    if allow_booking:
        [connection] = client.call("GET", "/agents")["agents"]
        client.call("POST", f"/agents/{connection['id']}", {"allowBooking": True})
    return agent


def trip() -> dict:
    day = datetime.date.today() + datetime.timedelta(days=3)
    return {"date": day.isoformat(), "from": "서울", "to": "부산", "earliest": "07:00"}


def test_an_mcp_client_without_a_token_finds_where_to_authorize(stack):
    reply = post_json(
        stack["api"] + "/api/mobile/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    )
    assert reply.status == 401
    challenge = reply.headers["WWW-Authenticate"]
    prm_url = re.search(r'resource_metadata="([^"]+)"', challenge).group(1)
    prm = send("GET", prm_url).json()
    assert prm["resource"] == stack["api"] + "/api/mobile/mcp"
    issuer = prm["authorization_servers"][0]
    meta = send("GET", issuer + "/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"] == issuer + "/oauth/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]


def test_an_agent_connects_through_the_app_and_starts_a_watch(stack, client):
    agent = connect(stack, client, allow_booking=True)

    hello = agent.rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "e2e", "version": "0"},
        },
    ).json()["result"]
    assert hello["serverInfo"]["name"] == "jari"
    assert agent.rpc("notifications/initialized", notify=True).status == 202
    names = {tool["name"] for tool in agent.rpc("tools/list", {}).json()["result"]["tools"]}
    assert {"find_trains", "start_watch", "reserve_seat"} <= names

    found = agent.tool("find_trains", trip())
    assert not found.get("isError"), found
    numbers = [train["trainNo"] for train in found["structuredContent"]["trains"]]
    assert "00101" in numbers
    # 가짜 코레일이 실제로 그 구간·날짜로 열차 목록을 받았어요(find_trains → gateway.list_trains).
    day = trip()["date"].replace("-", "")
    listed = [call for call in client.korail_log() if call["event"] == "list_trains"]
    assert (day, "서울", "부산") in [
        (call["dep_date"], call["src"], call["dst"]) for call in listed
    ]

    watching = agent.tool("start_watch", {**trip(), "trains": ["00101"]})
    assert not watching.get("isError"), watching
    # 앱이 보는 상태에도 같은 찾기가 보여요. 가짜 코레일은 몇 번 뒤에 좌석을 내줘 결제 대기로 넘어가요.
    client.wait_for(lambda status: status["running"] or status["pending"])
    # 감시는 워커가 코레일을 다시 조회하며 돌아요.
    client.wait_for(lambda _: any(call["event"] == "search" for call in client.korail_log()))

    [connection] = client.call("GET", "/agents")["agents"]
    assert connection["allowBooking"] is True
    assert connection["lastUsedAt"]


def test_a_read_only_agent_can_look_but_not_book(stack, client):
    agent = connect(stack, client, allow_booking=False)
    assert not agent.tool("find_trains", trip()).get("isError")

    refused = agent.tool("start_watch", {**trip(), "trains": ["00101"]})

    assert refused["isError"] is True
    status = client.call("GET", "/status")
    assert not status["running"] and not status.get("scheduled")


def test_refresh_rotates_and_a_replay_cuts_the_agent_off(stack, client):
    agent = connect(stack, client, allow_booking=False)
    first = agent.tokens["refresh_token"]

    rotated = agent.refresh(first)
    assert rotated.status == 200, rotated.body
    agent.tokens = rotated.json()
    assert agent.rpc("ping").status == 200

    replay = agent.refresh(first)
    assert replay.status == 400
    assert replay.json()["error"] == "invalid_grant"
    assert agent.rpc("ping").status == 401


def test_disconnecting_in_the_app_cuts_the_agent_off_at_once(stack, client):
    agent = connect(stack, client, allow_booking=False)
    [connection] = client.call("GET", "/agents")["agents"]

    client.call("DELETE", f"/agents/{connection['id']}")

    assert agent.rpc("ping").status == 401
    assert client.call("GET", "/agents")["agents"] == []


# ----------------------------------------------------------------- §13 실사용에서 드러난 것

NOT_READ = "조회하지 못했어요(좌석이 없다는 뜻이 아니에요)"


def text_of(result: dict) -> str:
    return "\n".join(item.get("text", "") for item in result.get("content", []))


def test_start_watch_takes_only_train_numbers_find_trains_returned(stack, client):
    # §13 U1: 사용자 에이전트가 ["103", "99999"] 를 넣었어요. 추측해 고치지 않고 가능한 번호를 알려요.
    agent = connect(stack, client, allow_booking=True)
    listed = agent.tool("find_trains", trip())
    numbers = [train["trainNo"] for train in listed["structuredContent"]["trains"]]
    for wrong in (["103", "99999"], ["00101", "99999"]):
        refused = agent.tool("start_watch", {**trip(), "trains": wrong})
        assert refused.get("isError") is True, refused
        assert re.search(r"find_trains 결과의 trainNo ?를 그대로 넣어 주세요", text_of(refused))
        assert numbers[0] in text_of(refused)
    status = client.call("GET", "/status")
    assert not status["running"] and not status.get("scheduled")
    assert not agent.tool("start_watch", {**trip(), "trains": [numbers[0]]}).get("isError")


def test_a_failed_train_list_and_a_sold_out_one_read_differently(stack, client):
    # §13 T2: 코레일이 답하지 않은 것과 모든 열차가 매진인 것은 도구 결과에서 달라야 해요.
    agent = connect(stack, client, allow_booking=False)
    client.scenario(list="unavailable")
    failed = agent.tool("find_trains", trip())
    assert failed["isError"] is True
    assert NOT_READ in text_of(failed)

    client.scenario(list="sold_out")
    sold_out = agent.tool("find_trains", trip())
    assert not sold_out.get("isError"), sold_out
    trains = sold_out["structuredContent"]["trains"]
    assert trains and all(train["soldOut"] for train in trains)
    assert "조회하지 못했어요" not in text_of(sold_out)


def test_a_seat_must_be_free_and_named_as_on_the_map(stack, client):
    # §13 T1: 가짜 코레일도 실제처럼 판매 여부와 표시를 검사해요. 좌석을 잡으면 결제 안내가 따라와요.
    agent = connect(stack, client, allow_booking=True)
    train = agent.tool("find_trains", trip())["structuredContent"]["trains"][0]
    seats = agent.tool(
        "get_seat_map", {"trainKey": train["trainKey"], "seatClass": "general", "passengers": 1}
    )
    car = seats["structuredContent"]["cars"][0]
    free = car["freeSeats"][0]

    def reserve(seat: dict) -> dict:
        return agent.tool(
            "reserve_seat",
            {
                "trainKey": train["trainKey"],
                "seatClass": "general",
                "passengers": 1,
                "carNo": car["carNo"],
                "seats": [
                    {"carNo": car["carNo"], "seatNo": seat["seatNo"], "label": seat["label"]}
                ],
            },
        )

    # 가짜 좌석표는 짝수 줄 D 석을 팔린 것으로 둬요.
    sold = {"seatNo": f"{car['carNo']:02d}0204", "label": "2D"}
    refused = reserve(sold)
    assert refused["isError"] is True
    assert "판매 가능한 좌석이 없습니다" in text_of(refused)
    renamed = reserve({**free, "label": "99Z"})
    assert renamed["isError"] is True
    assert "좌석 정보가 바뀌었습니다" in text_of(renamed)
    assert not any(call["event"] == "reserve_designated" for call in client.korail_log())

    booked = reserve(free)
    assert not booked.get("isError"), booked
    # §13 U5·U6: 상태에도 결제 주소와 기한이 있고, 시각은 한국 시간이에요.
    [pending] = agent.tool("get_status")["structuredContent"]["pending"]
    assert pending["paymentUrl"].startswith("https://")
    assert pending["expiresAt"].endswith("+09:00")
    assert not agent.tool("cancel_reservation").get("isError")
