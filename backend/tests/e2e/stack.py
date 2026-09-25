"""
e2e 스택: 가짜 Redis + 실제 모바일 서버(가짜 코레일) + 제어 서버.

    uv run --frozen python tests/e2e/stack.py [--api-port 18081] [--control-port 18090]
        [--redis-port 16379] [--origin http://127.0.0.1:4173 ...]

실제 코레일에는 한 번도 닿지 않아요. 서버와 예약 워커는 운영과 같은 코드로 돌고,
코레일 호출만 fake_korail/ 가 시나리오대로 답해요. 준비가 끝나면 제어 서버의
GET /health 가 200 을 돌려주고, 표준 출력에 E2E_READY {...} 한 줄을 적어요.

제어 서버 (127.0.0.1 전용)
    GET  /health             준비 여부
    GET  /config             API 주소, 관리자 계정
    POST /scenario           가짜 코레일 시나리오를 통째로 바꿈 (기본값 위에 덮어씀)
    POST /reset              시나리오를 기본값으로, 코레일 호출 기록과 요청 한도를 비움
    GET  /log                가짜 코레일이 받은 호출 목록
    POST /user               초대 코드로 새 사용자를 만들고 {username, password, token} 을 돌려줌
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil
import redis
from fakeredis import TcpFakeServer

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
FAKE_KORAIL = HERE / "fake_korail"
SCENARIO_KEY, LOG_KEY = "e2e:scenario", "e2e:log"

# 운영 기본값(초 단위 대기)을 줄여 한 흐름이 몇 초 안에 끝나게 해요. 로직은 그대로예요.
FAST_SETTINGS = {
    "SEARCH_INTERVAL": "0.05",
    "SEARCH_INTERVAL_JITTER": "0",
    "PROCESS_START_GRACE_SECONDS": "0.3",
    "PAYMENT_VERIFY_INTERVAL": "1",
    "PAYMENT_WATCH_LEASE": "4",
    "WATCHDOG_POLL_SECONDS": "1",
    "SCHEDULE_POLL_SECONDS": "1",
    "MAX_CONCURRENT_SEARCHES": "0",
}


def interpreter() -> tuple[str, dict]:
    """
    서버를 띄울 파이썬과 그에 필요한 환경.

    Windows 가상환경의 python.exe 는 실제 인터프리터를 자식으로 띄우는 런처라, 서버가
    Popen 으로 받은 pid 와 예약 워커의 os.getpid() 가 달라요. 워커는 그 둘이 같아야 일을
    시작하니(운영인 Linux 에서는 늘 같아요) 여기서는 실제 인터프리터로 띄우고 가상환경의
    site-packages 는 sitecustomize 가 붙여요.
    """
    if os.name != "nt" or sys.prefix == sys.base_prefix:
        return sys.executable, {}
    return sys._base_executable, {"JARI_E2E_VENV_SITE": sysconfig.get_paths()["purelib"]}


def request(method: str, url: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(
        urllib.request.Request(url, data, headers, method=method), timeout=30
    ) as reply:
        return json.loads(reply.read() or b"{}")


class Stack:
    def __init__(self, args):
        self.args = args
        self.data = Path(tempfile.mkdtemp(prefix="jari-e2e-"))
        self.api = f"http://127.0.0.1:{args.api_port}"
        self.redis_url = f"redis://127.0.0.1:{args.redis_port}/0"
        self.admin = {"username": "e2eadmin", "password": secrets.token_urlsafe(18)}
        self.server: subprocess.Popen | None = None
        self.tokens: list[str] = []
        self.ready = False
        pythonpath = os.pathsep.join(filter(None, [str(FAKE_KORAIL), os.environ.get("PYTHONPATH")]))
        self.env = {
            **os.environ,
            **FAST_SETTINGS,
            "PYTHONPATH": pythonpath,
            "PYTHONUTF8": "1",
            "JARI_E2E_FAKE_KORAIL": "1",
            "MOBILE_SECRET": secrets.token_urlsafe(40),
            "MOBILE_REDIS_URL": self.redis_url,
            "MOBILE_DATA_DIR": str(self.data),
            "MOBILE_ORIGINS": ",".join(["https://localhost", *args.origin]),
        }
        self.python, extra = interpreter()
        self.env.update(extra)
        self.env.pop("MOBILE_FCM_CREDENTIALS", None)
        self.env.pop("MOBILE_SECRET_FILE", None)
        self.redis = redis.Redis.from_url(self.redis_url, decode_responses=True)

    def start(self) -> None:
        fake = TcpFakeServer(("127.0.0.1", self.args.redis_port))
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        subprocess.run(
            [
                self.python,
                "-m",
                "korail_bot.mobile",
                "admin",
                "--username",
                self.admin["username"],
                # One token: token_urlsafe can start with "-", which argparse would take for an option.
                f"--password={self.admin['password']}",
            ],
            env=self.env,
            cwd=BACKEND,
            check=True,
            capture_output=True,
        )
        # 스택이 끝날 때까지 서버와 워커의 출력을 받아요. stop() 에서 닫아요.
        self.log = (self.data / "server.log").open("wb")
        self.server = subprocess.Popen(
            [
                self.python,
                "-m",
                "korail_bot.mobile",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.args.api_port),
            ],
            env=self.env,
            cwd=BACKEND,
            stdout=self.log,
            # Linux 에서 워커까지 한 그룹으로 정리하려고 새 세션으로 띄워요.
            start_new_session=os.name != "nt",
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise SystemExit(f"서버가 시작하지 못했어요. 로그: {self.data / 'server.log'}")
            try:
                request("GET", f"{self.api}/api/mobile/status")
                break
            except urllib.error.HTTPError as answered:
                answered.close()
                break  # 401 이라도 답했다면 떠 있는 거예요.
            except OSError:
                time.sleep(0.3)
        else:
            raise SystemExit("서버가 60초 안에 응답하지 않았어요.")
        # 서버 프로세스가 스스로 가짜 코레일을 끼웠다고 남긴 기록이 있어야 써요.
        installed = [json.loads(line) for line in self.redis.lrange(LOG_KEY, 0, -1)]
        if not any(e["event"] == "installed" and e["pid"] == self.server.pid for e in installed):
            raise SystemExit(
                "서버에 가짜 코레일이 끼워지지 않았어요. 실제 코레일로 나갈 수 있어 멈춰요."
            )
        self.reset()
        self.ready = True

    def reset(self) -> None:
        # 앞 테스트의 찾기가 다음 테스트의 시나리오로 좌석을 잡지 않게 멈춰요.
        for token in self.tokens:
            try:
                request("POST", f"{self.api}/api/mobile/search/cancel", {}, token)
            except urllib.error.HTTPError as refused:
                refused.close()
        # 예약을 잡은 워커는 찾기 기록을 지우고 결제 기한(가짜 코레일 10분)까지 결제를 지켜봐서,
        # 위의 취소로는 멈추지 않아요. 남겨 두면 테스트마다 하나씩 쌓여 메모리를 먹고, 다음 테스트에 알림을 보내요.
        for child in psutil.Process(self.server.pid).children(recursive=True):
            try:
                if "korail_bot.mobile.worker" in child.cmdline():
                    child.kill()
            except psutil.Error:
                pass
        self.redis.delete(SCENARIO_KEY, LOG_KEY)
        with sqlite3.connect(self.data / "identity.sqlite3") as db:
            db.execute("DELETE FROM rate_limits")

    def new_user(self) -> dict:
        admin = request(
            "POST", f"{self.api}/api/mobile/auth/login", {**self.admin, "role": "admin"}
        )
        invite = request("POST", f"{self.api}/api/mobile/invites", {"ttlHours": 1}, admin["token"])[
            "invite"
        ]
        user = {"username": f"u{secrets.token_hex(4)}", "password": secrets.token_urlsafe(16)}
        registered = request(
            "POST", f"{self.api}/api/mobile/auth/register", {**user, "invite": invite}
        )
        # 코레일 계정 연결도 실제 경로(/register → 가짜 코레일 로그인)로 해요.
        # 실존할 수 있는 전화번호 대신 쓰이지 않는 회원번호를 써요.
        request(
            "POST",
            f"{self.api}/api/mobile/register",
            {"username": "0000000000", "password": "korail-e2e"},
            registered["token"],
        )
        self.tokens.append(registered["token"])
        with sqlite3.connect(self.data / "identity.sqlite3") as db:
            db.execute("DELETE FROM rate_limits")
        return {**user, "token": registered["token"]}

    def stop(self) -> None:
        if self.server and self.server.poll() is None:
            if os.name == "nt":
                # 예약 워커는 서버의 자식이라 트리째 정리해요.
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(self.server.pid)], capture_output=True
                )
            else:
                os.killpg(os.getpgid(self.server.pid), signal.SIGTERM)
        if getattr(self, "log", None):
            self.log.close()


def control(stack: Stack):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status: int, body) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self.reply(200 if stack.ready else 503, {"ready": stack.ready})
            if self.path == "/config":
                return self.reply(
                    200, {"api": stack.api, "admin": stack.admin, "data": str(stack.data)}
                )
            if self.path == "/log":
                return self.reply(
                    200, [json.loads(line) for line in stack.redis.lrange(LOG_KEY, 0, -1)]
                )
            self.reply(404, {})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/scenario":
                stack.redis.set(SCENARIO_KEY, json.dumps(body))
                return self.reply(200, body)
            if self.path == "/reset":
                stack.reset()
                return self.reply(200, {"reset": True})
            if self.path == "/user":
                return self.reply(200, stack.new_user())
            self.reply(404, {})

    return ThreadingHTTPServer(("127.0.0.1", stack.args.control_port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-port", type=int, default=18081)
    parser.add_argument("--control-port", type=int, default=18090)
    parser.add_argument("--redis-port", type=int, default=16379)
    parser.add_argument("--origin", action="append", default=[])
    args = parser.parse_args()

    stack = Stack(args)
    # pytest·Playwright 는 끝낼 때 SIGTERM 을 보내요. finally 로 서버와 워커까지 정리해요.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = control(stack)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        stack.start()
        print(
            "E2E_READY " + json.dumps({"api": stack.api, "control": args.control_port}), flush=True
        )
        while stack.server.poll() is None:
            time.sleep(0.5)
        raise SystemExit(f"서버가 멈췄어요. 로그: {stack.data / 'server.log'}")
    except KeyboardInterrupt:
        pass
    finally:
        stack.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
