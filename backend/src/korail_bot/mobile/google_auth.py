"""Google OpenID Connect for accounts that were explicitly linked in app settings."""

import base64
import hashlib
import html
import ipaddress
import json
import math
import re
import secrets
import threading
import time
from urllib.parse import quote, urlencode

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from flask import Response, g, jsonify, request

from korail_bot.mobile.identity import AuthError
from korail_bot.mobile.pages import _STYLE

CALLBACK_PATH = "/api/mobile/auth/google/callback"
CONFIRM_PATH = "/api/mobile/auth/google/confirm"
APP_MISSING_PATH = "/api/mobile/auth/google/app-missing"
# The browser returns to the Android app through an intent addressed to this package only,
# so another app claiming the same scheme cannot receive the handoff.
ANDROID_PACKAGE = "dev.thsvkd.jari"
APP_RETURN_SCHEME = "dev.thsvkd.jari"
APP_RETURN_HOST = "google-auth"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

_GOOGLE_PAGE_STYLE = """
.google-card { padding: 20px; border: 1px solid var(--line); border-radius: 14px; background: var(--card); }
.google-card > :last-child { margin-bottom: 0; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 8px 12px; margin: 20px 0; }
dt { color: var(--muted); font-weight: 600; }
dd { margin: 0; }
form { display: grid; gap: 12px; margin-top: 20px; }
label { display: grid; gap: 8px; font-weight: 600; }
input, button { width: 100%; min-height: 48px; border-radius: 10px; font: inherit; }
input { padding: 10px 12px; border: 1px solid var(--line); background: var(--bg); color: var(--text); }
.actions { display: grid; gap: 12px; }
button { padding: 10px 14px; border: 1px solid var(--accent); background: var(--accent); color: white; font-weight: 700; cursor: pointer; }
button[value="deny"] { background: transparent; color: var(--text); border-color: var(--line); }
.app-return { display: flex; align-items: center; justify-content: center; min-height: 48px; margin-top: 20px; padding: 10px 14px; border-radius: 10px; background: var(--accent); color: white; font-weight: 700; text-decoration: none; }
@media (min-width: 520px) { .actions { grid-template-columns: 1fr 1fr; } }
"""


class GoogleTokenError(ValueError):
    pass


_jwks_lock = threading.Lock()
_jwks_cache = {"until": 0.0, "keys": {}}


def _decode(value):
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise GoogleTokenError("Invalid token encoding") from exc


def _json_part(value):
    try:
        result = json.loads(_decode(value))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GoogleTokenError("Invalid token JSON") from exc
    if not isinstance(result, dict):
        raise GoogleTokenError("Invalid token part")
    return result


def _google_keys(now):
    with _jwks_lock:
        if _jwks_cache["until"] > now:
            return _jwks_cache["keys"]
        try:
            response = requests.get(JWKS_URL, timeout=(3.05, 5))
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise GoogleTokenError("Could not load Google signing keys") from exc
        keys = {}
        for item in payload.get("keys", ()) if isinstance(payload, dict) else ():
            if (
                isinstance(item, dict)
                and item.get("kty") == "RSA"
                and item.get("alg") in {None, "RS256"}
                and isinstance(item.get("kid"), str)
            ):
                try:
                    modulus = int.from_bytes(_decode(item["n"]), "big")
                    exponent = int.from_bytes(_decode(item["e"]), "big")
                    keys[item["kid"]] = rsa.RSAPublicNumbers(exponent, modulus).public_key()
                except (KeyError, ValueError, TypeError):
                    continue
        if not keys:
            raise GoogleTokenError("Google returned no usable signing keys")
        headers = getattr(response, "headers", {})
        match = re.search(r"(?:^|,)\s*max-age=(\d+)", headers.get("Cache-Control", ""))
        ttl = min(int(match.group(1)), 3600) if match else 300
        _jwks_cache.update(until=now + max(ttl, 60), keys=keys)
        return keys


def verify_id_token(token, client_id, nonce, *, now=None):
    """Verify Google's RS256 ID token locally and return only authenticated claims."""
    if not isinstance(token, str) or token.count(".") != 2:
        raise GoogleTokenError("Invalid ID token")
    encoded_header, encoded_claims, encoded_signature = token.split(".")
    header = _json_part(encoded_header)
    claims = _json_part(encoded_claims)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise GoogleTokenError("Unexpected token algorithm")
    current = time.time() if now is None else now
    key = _google_keys(current).get(header["kid"])
    if key is None:
        # A rotated key can arrive while an older set is cached. Refresh once.
        with _jwks_lock:
            _jwks_cache["until"] = 0
        key = _google_keys(current).get(header["kid"])
    if key is None:
        raise GoogleTokenError("Unknown signing key")
    try:
        key.verify(
            _decode(encoded_signature),
            f"{encoded_header}.{encoded_claims}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise GoogleTokenError("Invalid token signature") from exc
    if claims.get("iss") not in ISSUERS or claims.get("aud") != client_id:
        raise GoogleTokenError("Token issuer or audience mismatch")
    expires = claims.get("exp")
    issued = claims.get("iat")
    if (
        not isinstance(expires, int | float)
        or isinstance(expires, bool)
        or not math.isfinite(expires)
        or expires <= current
    ):
        raise GoogleTokenError("Expired token")
    if (
        not isinstance(issued, int | float)
        or isinstance(issued, bool)
        or not math.isfinite(issued)
        or issued > current + 60
    ):
        raise GoogleTokenError("Invalid token issue time")
    if "azp" in claims and claims["azp"] != client_id:
        raise GoogleTokenError("Token authorized party mismatch")
    if claims.get("nonce") != nonce:
        raise GoogleTokenError("Token nonce mismatch")
    if not isinstance(claims.get("sub"), str) or not 1 <= len(claims["sub"]) <= 255:
        raise GoogleTokenError("Missing token subject")
    return claims


def exchange_code(*, code, client_id, client_secret, redirect_uri, nonce, code_verifier):
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
            timeout=(3.05, 8),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise GoogleTokenError("Google token exchange failed") from exc
    if not isinstance(payload, dict):
        raise GoogleTokenError("Invalid Google token response")
    return verify_id_token(payload.get("id_token"), client_id, nonce)


def install(
    app,
    identity,
    *,
    public_url,
    client_id,
    client_secret,
    body,
    limited,
    authenticated,
):
    configured = tuple(bool(value) for value in (public_url, client_id, client_secret))
    if (client_id or client_secret) and not all(configured):
        raise ValueError(
            "Google login requires MOBILE_PUBLIC_URL, MOBILE_GOOGLE_CLIENT_ID, and "
            "MOBILE_GOOGLE_CLIENT_SECRET together"
        )
    enabled = all(configured)
    redirect_uri = public_url + CALLBACK_PATH if enabled else None

    def app_return_url(handoff):
        fallback = quote(public_url + APP_MISSING_PATH, safe="")
        return (
            f"intent://{APP_RETURN_HOST}?handoff={handoff}#Intent;scheme={APP_RETURN_SCHEME};"
            f"package={ANDROID_PACKAGE};S.browser_fallback_url={fallback};end"
        )

    def start(mode, *, user_id=None, session_token=None, app_return=False):
        if not enabled:
            raise AuthError("Google 로그인이 서버에 설정되지 않았어요.", 503)
        client = request.headers.get("CF-Connecting-IP") or request.remote_addr or "unknown"
        try:
            initiator_ip = str(ipaddress.ip_address(client))
        except ValueError:
            initiator_ip = "알 수 없음"
        user_agent = request.headers.get("User-Agent", "").lower()
        initiator_label = (
            "Android 앱" if "android" in user_agent else "브라우저 또는 기타 클라이언트"
        )
        limited("google-start-any", 60, 300)
        limited("google-start-ip:" + client, 10, 300)
        flow = identity.google_start(
            mode,
            user_id=user_id,
            session_token=session_token,
            initiator_label=initiator_label,
            initiator_ip=initiator_ip,
            ttl=300,
            app_return=app_return,
        )
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(flow["codeVerifier"].encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid",
                "state": flow["state"],
                "nonce": flow["nonce"],
                "prompt": "select_account",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        result = {
            "ticket": flow["ticket"],
            "authorizationUrl": AUTHORIZE_URL + "?" + query,
            "expiresIn": flow["expiresIn"],
        }
        # Earlier apps and the browser build still show the code; app-return flows have none.
        if "verificationCode" in flow:
            result["verificationCode"] = flow["verificationCode"]
        return result

    def page(
        message,
        status=200,
        confirmation=None,
        *,
        title="Google 로그인",
        initiator_label=None,
        initiator_ip=None,
        app_url=None,
    ):
        nonce = secrets.token_urlsafe(16)
        form = ""
        refresh = ""
        if app_url:
            escaped = html.escape(app_url, quote=True)
            # Try once on arrival; the button below is the tap browsers may require to leave.
            refresh = f'<meta http-equiv="refresh" content="0;url={escaped}">'
            form = f'<a class="app-return" href="{escaped}">자리났다 앱 열기</a>'

        if confirmation:
            details = (
                "<dl><dt>요청 앱</dt><dd>"
                + html.escape(initiator_label or "알 수 없는 앱")
                + "</dd><dt>요청 IP</dt><dd>"
                + html.escape(initiator_ip or "알 수 없음")
                + "</dd></dl>"
                "<p>허용하면 요청한 앱이 이 자리났다 계정과 예약 정보에 접근할 수 있어요. "
                "본인이 자리났다 앱에서 직접 시작했고, 그 앱에 표시된 코드인지 확인해 주세요. "
                "다른 사람이 알려 준 코드는 입력하지 마세요.</p>"
            )
            form = details + (
                f'<form method="post" action="{CONFIRM_PATH}">'
                f'<input type="hidden" name="confirmation" value="{html.escape(confirmation, quote=True)}">'
                "<label>앱에 표시된 확인 코드 "
                '<input type="password" name="verificationCode" autocomplete="one-time-code" '
                'inputmode="text" minlength="8" maxlength="9" required></label>'
                '<div class="actions">'
                '<button type="submit" name="decision" value="allow">'
                "이 앱의 로그인 허용</button>"
                '<button type="submit" name="decision" value="deny" formnovalidate>'
                "거부</button></div></form>"
            )
        response = Response(
            '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            + refresh
            + "<title>"
            + html.escape(title)
            + " · 자리났다</title>"
            + f'<style nonce="{nonce}">{_STYLE}{_GOOGLE_PAGE_STYLE}</style>'
            + '</head><body><main><section class="google-card"><h1>'
            + html.escape(title)
            + "</h1><p>"
            + html.escape(message)
            + "</p>"
            + form
            + "</section></main></body></html>",
            status=status,
            mimetype="text/html",
        )
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/mobile/auth/google")
    def google_config():
        return jsonify(enabled=enabled)

    @app.post("/api/mobile/auth/google/start")
    def google_login_start():
        return jsonify(start("login", app_return=body().get("return") == "app"))

    @app.post("/api/mobile/auth/google/poll")
    def google_poll():
        if not enabled:
            raise AuthError("Google 로그인이 서버에 설정되지 않았어요.", 503)
        client = request.headers.get("CF-Connecting-IP") or request.remote_addr or "unknown"
        limited("google-poll-ip:" + client, 600, 300)
        payload = body()
        result = identity.google_poll(payload.get("ticket"), payload.get("handoff"))
        if "error" in result:
            raise AuthError(result["error"], result["statusCode"])
        return jsonify(result)

    @app.post("/api/mobile/auth/google/cancel")
    def google_cancel():
        if not enabled:
            raise AuthError("Google 로그인이 서버에 설정되지 않았어요.", 503)
        client = request.headers.get("CF-Connecting-IP") or request.remote_addr or "unknown"
        limited("google-cancel-ip:" + client, 60, 300)
        identity.google_cancel(body().get("ticket"))
        return jsonify(cancelled=True)

    @app.post("/api/mobile/auth/google/ack")
    def google_acknowledge():
        if not enabled:
            raise AuthError("Google 로그인이 서버에 설정되지 않았어요.", 503)
        client = request.headers.get("CF-Connecting-IP") or request.remote_addr or "unknown"
        limited("google-ack-ip:" + client, 60, 300)
        identity.google_acknowledge(body().get("ticket"))
        return jsonify(acknowledged=True)

    @app.get(CALLBACK_PATH)
    def google_callback():
        if not enabled:
            return page("Google 로그인이 서버에 설정되지 않았어요.", 503)
        try:
            flow = identity.google_take_callback(request.args.get("state"))
        except AuthError as exc:
            return page(
                "요청이 만료됐거나 이미 사용됐어요. 앱으로 돌아가 다시 시작해 주세요.",
                exc.status,
            )
        code = request.args.get("code")
        if request.args.get("error") or not isinstance(code, str) or not code:
            identity.google_fail_callback(flow["id"])
            return page("Google 로그인이 완료되지 않았어요. 앱으로 돌아가 다시 시도해 주세요.", 400)
        try:
            claims = exchange_code(
                code=code,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                nonce=flow["nonce"],
                code_verifier=flow["code_verifier"],
            )
        except GoogleTokenError as exc:
            identity.google_fail_callback(flow["id"])
            app.logger.info("Google callback verification failed (%s)", type(exc).__name__)
            return page("Google 계정을 확인할 수 없어요. 앱으로 돌아가 다시 시도해 주세요.", 400)
        if flow["app_return"]:
            confirmation = identity.google_prepare_handoff(flow["id"], claims["sub"])
        else:
            confirmation = identity.google_prepare_confirmation(
                flow["id"], claims["sub"], secure_cookie=public_url.startswith("https://")
            )
        if not confirmation:
            return page(
                "앱 로그인이 만료되었거나 연결 요청이 취소됐어요. 앱으로 돌아가 다시 시작해 주세요.",
                403,
            )
        if confirmation.get("error") == "unlinked":
            return page(
                "먼저 자리났다 앱 설정에서 기존 계정에 이 Google 계정을 연결해 주세요.",
                403,
            )
        if flow["app_return"]:
            title = (
                f"{confirmation['username']} 앱 계정에 Google 연결을 마무리해요"
                if flow["mode"] == "link"
                else f"{confirmation['username']} 계정으로 앱 로그인을 마무리해요"
            )
            return page(
                "자리났다 앱이 열리면 자동으로 끝나요. 앱이 열리지 않으면 아래 버튼을 눌러 주세요. "
                "구글 로그인을 시작한 이 휴대폰의 앱에서만 완료돼요.",
                title=title,
                app_url=app_return_url(confirmation["handoff"]),
            )
        if flow["mode"] == "link":
            response = page(
                "연결을 시작한 앱에 표시된 코드를 입력한 뒤 앱으로 돌아가 주세요.",
                confirmation=confirmation["confirmation"],
                title=f"{confirmation['username']} 앱 계정에 Google 연결을 허용할까요?",
                initiator_label=confirmation["initiatorLabel"],
                initiator_ip=confirmation["initiatorIp"],
            )
        else:
            response = page(
                "로그인을 시작한 앱에 표시된 코드를 입력한 뒤 앱으로 돌아가 주세요.",
                confirmation=confirmation["confirmation"],
                title=f"{confirmation['username']} 계정의 앱 로그인을 허용할까요?",
                initiator_label=confirmation["initiatorLabel"],
                initiator_ip=confirmation["initiatorIp"],
            )
        response.set_cookie(
            confirmation["cookieName"],
            confirmation["cookie"],
            max_age=300,
            secure=public_url.startswith("https://"),
            httponly=True,
            samesite="Strict",
            path="/",
        )
        return response

    @app.get(APP_MISSING_PATH)
    def google_app_missing():
        return page(
            "이 브라우저에서는 자리났다 앱을 열 수 없어요. "
            "구글 로그인을 시작한 휴대폰에서 다시 시도해 주세요.",
            title="자리났다 앱을 열 수 없어요",
        )

    @app.post(CONFIRM_PATH)
    def google_confirm():
        if not enabled:
            return page("Google 로그인이 서버에 설정되지 않았어요.", 503)
        client = request.headers.get("CF-Connecting-IP") or request.remote_addr or "unknown"
        limited("google-confirm-ip:" + client, 20, 300)
        confirmation = request.form.get("confirmation")
        decision = request.form.get("decision")
        if decision == "deny":
            cookie_name = identity.google_deny(confirmation, request.cookies)
            if not cookie_name:
                return page("Google 확인 요청을 다시 시작해 주세요.", 400)
            response = page("이 앱의 로그인 요청을 거부했어요. 창을 닫아도 돼요.")
            response.delete_cookie(cookie_name, path="/")
            return response
        if decision != "allow":
            return page("허용하거나 거부해 주세요.", 400)
        result = identity.google_confirm(
            confirmation,
            request.cookies,
            request.form.get("verificationCode"),
        )
        if "error" in result:
            retry = confirmation if result["statusCode"] == 403 else None
            return page(result["error"], result["statusCode"], confirmation=retry)
        response = page("확인이 끝났어요. 앱으로 돌아가 완료해 주세요.")
        response.delete_cookie(result["cookieName"], path="/")
        return response

    @app.get("/api/mobile/account/google")
    @authenticated
    def google_account():
        return jsonify(
            enabled=enabled,
            linked=enabled and identity.google_status(g.user["id"]),
            # Kept for older clients; unlink now converts this phone to a local session.
            signedInWithGoogle=identity.session_via_google(g.token),
            # 4.18 clients treat a missing field as requiring the app password.
            reauthenticationRequired=False,
        )

    @app.post("/api/mobile/account/google/link")
    @authenticated
    def google_link():
        limited("google-link:" + g.user["id"], 5, 300)
        return jsonify(
            start(
                "link",
                user_id=g.user["id"],
                session_token=g.token,
                app_return=g.payload.get("return") == "app",
            )
        )

    @app.post("/api/mobile/account/google/unlink")
    @authenticated
    def google_unlink():
        limited("google-unlink:" + g.user["id"], 5, 300)
        # Like linking, the live session is enough; 4.20 and older apps still send a password, now ignored.
        return jsonify(identity.google_unlink(g.user["id"], g.token))

    return enabled
