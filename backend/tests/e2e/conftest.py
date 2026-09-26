"""통합 테스트용 e2e 스택: 테스트 세션마다 한 번 띄우고, 테스트마다 새 사용자를 써요."""

from __future__ import annotations

import datetime
import json
import socket
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from stack import request


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Client:
    """한 사용자로 모바일 API 를 부르는 얇은 손잡이."""

    def __init__(self, api: str, control: str, user: dict):
        self.api, self.control, self.user = api, control, user

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        return request(method, f"{self.api}/api/mobile{path}", body, self.user["token"])

    def error(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        try:
            return 200, self.call(method, path, body)
        except urllib.error.HTTPError as failure:
            with failure:
                return failure.code, json.loads(failure.read() or b"{}")

    def scenario(self, **changes) -> None:
        request("POST", f"{self.control}/scenario", changes)

    def reset_log(self) -> None:
        request("POST", f"{self.control}/reset", {})

    def korail_log(self) -> list[dict]:
        return request("GET", f"{self.control}/log")

    def wait_for(self, check, *, timeout: float = 30, every: float = 0.3):
        """조건이 참이 될 때까지 /status 를 다시 읽어요. 마지막 상태를 실패 메시지에 담아요."""
        deadline = time.monotonic() + timeout
        status = None
        while time.monotonic() < deadline:
            status = self.call("GET", "/status")
            if check(status):
                return status
            time.sleep(every)
        raise AssertionError(f"{timeout}초 안에 조건을 만족하지 않았어요. 마지막 상태: {status}")


@pytest.fixture(scope="session")
def stack():
    api_port, control_port, redis_port = _free_port(), _free_port(), _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parent / "stack.py"),
            "--api-port",
            str(api_port),
            "--control-port",
            str(control_port),
            "--redis-port",
            str(redis_port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    control = f"http://127.0.0.1:{control_port}"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(process.stdout.read().decode(errors="replace"))
        try:
            request("GET", f"{control}/health")
            break
        except urllib.error.HTTPError as not_ready:
            not_ready.close()
            time.sleep(0.5)
        except OSError:
            time.sleep(0.5)
    else:
        process.kill()
        raise RuntimeError("e2e 스택이 90초 안에 준비되지 않았어요.")
    yield {
        "api": f"http://127.0.0.1:{api_port}",
        "control": control,
        "redis": f"redis://127.0.0.1:{redis_port}/0",
    }
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        process.terminate()
    # 출력을 비우면서 기다려요. 준비 뒤로 읽지 않은 파이프가 차 있으면 스택이 끝나며 출력을 쓰다 멈춰요(리눅스 CI).
    try:
        process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        raise RuntimeError(
            "e2e 스택이 종료 신호 뒤 30초 안에 끝나지 않았어요:\n"
            + output.decode(errors="replace")[-4000:]
        ) from None


@pytest.fixture
def client(stack):
    request("POST", f"{stack['control']}/reset", {})
    client = Client(stack["api"], stack["control"], request("POST", f"{stack['control']}/user", {}))
    yield client
    # 가짜 코레일은 루프백 밖 연결을 막고 기록해요. 하나라도 있으면 어딘가 실제 서버로 나가려 한 거예요.
    blocked = [call for call in client.korail_log() if call["event"] == "blocked"]
    assert not blocked, f"바깥으로 나가려던 연결이 있었어요: {blocked}"


@pytest.fixture
def conditions() -> dict:
    day = datetime.date.today() + datetime.timedelta(days=3)
    return {
        "v": 1,
        "action": "prepare_search",
        "dep_date": day.strftime("%Y%m%d"),
        "src_station": "서울",
        "dst_station": "부산",
        "dep_time": "0700",
        "max_dep_time": "2400",
        "train_type": "1",
        "seat_option": "2",
        "passenger_count": 1,
        "seat_strategy": "1",
        "seat_preference": "",
    }
