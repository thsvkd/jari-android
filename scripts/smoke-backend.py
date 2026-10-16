"""Run the real auth-only CLI and exercise HTTP with temporary accounts/data."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from korail_bot.mobile.identity import IdentityStore


def main():
    with tempfile.TemporaryDirectory(prefix="jari-smoke-") as temporary:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = dict(os.environ)
        for name in list(env):
            if name.startswith("MOBILE_") or name in {"BOTTOKEN", "USERID", "USERPW"}:
                del env[name]
        env.update(MOBILE_SECRET=secrets.token_hex(32), MOBILE_DATA_DIR=temporary)
        process = subprocess.Popen(
            [sys.executable, "-m", "korail_bot.mobile", "serve", "--auth-only", "--host", "127.0.0.1", "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}/api/mobile"

        def post(route, payload, token=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = Request(base + route, data=json.dumps(payload).encode(), headers=headers)
            with urlopen(request, timeout=3) as response:
                return json.load(response)

        try:
            ready = False
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError("Auth-only CLI exited before readiness")
                try:
                    post("/bootstrap", {})
                except HTTPError as error:
                    assert error.code == 401
                    ready = True
                    break
                except URLError:
                    time.sleep(0.1)
            assert ready, "Auth-only CLI did not become ready"
            store = IdentityStore(str(Path(temporary) / "identity.sqlite3"))
            admin_password = secrets.token_hex(20)
            store.ensure_admin("smoke_admin", admin_password)
            admin = post("/auth/login", {"username": "smoke_admin", "password": admin_password, "role": "admin"})
            auth = post("/auth/register", {"username": "smoke_user", "password": secrets.token_hex(20)})
            state = post("/bootstrap", {}, auth["token"])
            assert state["user"]["username"] == "smoke_user"
            assert state["user"]["status"] == "pending"
            assert state["capabilities"]["korail"] is False
            try:
                post("/trains", {}, auth["token"])
                raise AssertionError("Unapproved member reached railway functions")
            except HTTPError as error:
                assert error.code == 403
            approved = post(f"/admin/users/{auth['user']['id']}/approval", {"status": "approved"}, admin["token"])
            assert approved["user"]["status"] == "approved"
            assert post("/bootstrap", {}, auth["token"])["user"]["status"] == "approved"
            assert post("/auth/logout", {}, auth["token"])["ok"] is True
            try:
                post("/bootstrap", {}, auth["token"])
                raise AssertionError("Revoked session still authorized")
            except HTTPError as error:
                assert error.code == 401
            print("PASS: real auth-only CLI, pending registration/rail denial/admin approval/bootstrap/logout/revocation; no railway or Redis calls")
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
