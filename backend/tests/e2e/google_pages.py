"""Render Google confirmation pages through the real Flask routes for browser layout tests."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

from korail_bot.mobile import google_auth
from korail_bot.mobile.api import create_app
from korail_bot.mobile.identity import IdentityStore

PASSWORD = "eight888"
PUBLIC_URL = "https://jari.example"
CLIENT_ID = "client.apps.googleusercontent.com"
CLIENT_SECRET = "server-secret"


def confirmation_token(response) -> str:
    match = re.search(r'name="confirmation" value="([A-Za-z0-9_-]+)"', response.text)
    if not match:
        raise AssertionError("Google 확인 페이지에서 confirmation 값을 찾지 못했어요.")
    return match.group(1)


def start_link(client, headers, *, app_return=False) -> dict:
    response = client.post(
        "/api/mobile/account/google/link",
        json={"return": "app"} if app_return else {},
        headers=headers,
    )
    if response.status_code != 200:
        raise AssertionError(f"Google 연결 시작 실패: {response.status_code} {response.text}")
    return response.json


def callback(client, start: dict, subject: str):
    query = parse_qs(urlsplit(start["authorizationUrl"]).query)
    google_auth.exchange_code = lambda **_kwargs: {
        "sub": subject,
        "nonce": query["nonce"][0],
    }
    response = client.get(
        google_auth.CALLBACK_PATH,
        query_string={"state": query["state"][0], "code": "local-test-code"},
        base_url=PUBLIC_URL,
    )
    if response.status_code != 200:
        raise AssertionError(f"Google callback 실패: {response.status_code} {response.text}")
    return response


def render_pages() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="jari-google-pages-") as directory:
        identity = IdentityStore(Path(directory) / "identity.sqlite3")
        session = identity.register("alice", PASSWORD)
        identity.set_approval(session["user"]["id"], "approved")
        headers = {"Authorization": "Bearer " + session["token"]}
        app = create_app(
            identity,
            MagicMock(),
            public_url=PUBLIC_URL,
            google_client_id=CLIENT_ID,
            google_client_secret=CLIENT_SECRET,
        )
        app.testing = True
        client = app.test_client()

        first = start_link(client, headers)
        confirmation = callback(client, first, "google-layout-subject-1")
        token = confirmation_token(confirmation)
        wrong = client.post(
            google_auth.CONFIRM_PATH,
            data={
                "confirmation": token,
                "verificationCode": "WXYZ-2345",
                "decision": "allow",
            },
            base_url=PUBLIC_URL,
        )
        if wrong.status_code != 403:
            raise AssertionError(f"틀린 코드 응답 실패: {wrong.status_code} {wrong.text}")
        success = client.post(
            google_auth.CONFIRM_PATH,
            data={
                "confirmation": token,
                "verificationCode": first["verificationCode"],
                "decision": "allow",
            },
            base_url=PUBLIC_URL,
        )
        if success.status_code != 200:
            raise AssertionError(f"Google 확인 완료 실패: {success.status_code} {success.text}")

        second = start_link(client, headers)
        deny_confirmation = callback(client, second, "google-layout-subject-2")
        denied = client.post(
            google_auth.CONFIRM_PATH,
            data={
                "confirmation": confirmation_token(deny_confirmation),
                "decision": "deny",
            },
            base_url=PUBLIC_URL,
        )
        if denied.status_code != 200:
            raise AssertionError(f"Google 확인 거부 실패: {denied.status_code} {denied.text}")

        app_return = callback(
            client, start_link(client, headers, app_return=True), "google-layout-subject-3"
        )
        if 'href="intent://google-auth?handoff=' not in app_return.text:
            raise AssertionError("앱 복귀 페이지에 앱 열기 링크가 없어요.")
        app_missing = client.get(google_auth.APP_MISSING_PATH, base_url=PUBLIC_URL)
        if app_missing.status_code != 200:
            raise AssertionError(f"앱 없음 안내 실패: {app_missing.status_code} {app_missing.text}")

        return {
            "appReturn": app_return.text,
            "appMissing": app_missing.text,
            "confirmation": confirmation.text,
            "error": wrong.text,
            "success": success.text,
            "denied": denied.text,
        }


if __name__ == "__main__":
    print(json.dumps(render_pages(), ensure_ascii=False))
