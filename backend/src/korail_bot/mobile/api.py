"""Session-authenticated mobile API. No bot token and no callback endpoint."""

import threading
from functools import wraps

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

from korail_bot.mobile.identity import AuthError, timestamp
from korail_bot.services.mini_app_gateway import MiniAppError

PREFIX = "/api/mobile"
IDENTITY_FIELDS = {"chatId", "chat_id", "userId", "user_id", "storage_id", "identity"}


def create_app(identity, gateway, notifications=None, *, origins=(), booking_available=True):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    # The runtime is single-process; serialize each user's service mutations
    # so two simultaneous taps cannot launch competing search subprocesses.
    locks = [threading.RLock() for _ in range(128)]

    @app.errorhandler(AuthError)
    @app.errorhandler(MiniAppError)
    def domain_error(exc):
        # 401 always means the app Bearer session expired. Railway credentials
        # belong to a separate account and must not log the app user out.
        status = 428 if isinstance(exc, MiniAppError) and exc.status == 401 else exc.status
        return jsonify(error=str(exc)), status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error="요청을 확인해주세요."), exc.code

    @app.errorhandler(Exception)
    def internal_error(exc):
        # Upstream exception strings can contain railway credentials.
        app.logger.error("Mobile request failed (%s)", type(exc).__name__)
        return jsonify(error="처리 중 문제가 생겼습니다. 잠시 후 다시 시도해주세요."), 500

    @app.before_request
    def validate_request():
        origin = request.headers.get("Origin")
        if origin and origin not in origins:
            raise AuthError("허용되지 않은 앱 출처입니다.", 403)

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        origin = request.headers.get("Origin")
        if origin and origin in origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, X-User-Timezone"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        if response.status_code == 429:
            response.headers["Retry-After"] = "300"
        return response

    def body():
        if not request.get_data():
            return {}
        try:
            payload = request.get_json(silent=True)
        except RecursionError as exc:
            raise AuthError("JSON 중첩이 너무 깊습니다.", 400) from exc
        if not isinstance(payload, dict):
            raise AuthError("JSON 객체를 보내주세요.", 400)

        pending = [(payload, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 16:
                raise AuthError("JSON 중첩이 너무 깊습니다.", 400)
            if isinstance(value, dict):
                if IDENTITY_FIELDS.intersection(value):
                    raise AuthError("사용자 식별자는 서버에서 결정합니다.", 400)
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
        return payload

    def limited(key, limit, window):
        if not identity.allow(key, limit, window):
            raise AuthError("요청이 너무 잦습니다. 잠시 후 다시 시도해주세요.", 429)

    def authenticated(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            scheme, _, token = request.headers.get("Authorization", "").partition(" ")
            if scheme.lower() != "bearer":
                raise AuthError()
            g.user = identity.authenticate(token)
            g.token = token
            limited("api:" + g.user["id"], 120, 60)
            if request.method in {"POST", "DELETE"}:
                g.payload = body()
            if request.path.rsplit(PREFIX, 1)[-1] in {
                "/register",
                "/trains",
                "/search",
                "/schedule",
                "/reservations/cancel",
            }:
                limited("rail:" + g.user["id"], 10, 60)
            with locks[abs(g.user["storage_id"]) % len(locks)]:
                # A queued mutation may have waited behind logout. Recheck the
                # server session once it owns the user's mutation lock.
                g.user = identity.authenticate(token)
                timezone_name = request.headers.get("X-User-Timezone", "")
                if timezone_name:
                    gateway.sync_timezone(g.user["storage_id"], timezone_name)
                return view(*args, **kwargs)

        return wrapped

    @app.post(PREFIX + "/auth/register")
    @app.post(PREFIX + "/auth/login")
    def auth():
        limited("auth-ip:" + (request.remote_addr or "unknown"), 10, 300)
        payload = body()
        username = payload.get("username")
        if isinstance(username, str):
            limited("auth-user:" + username.lower(), 10, 300)
        if request.path.endswith("/register"):
            return jsonify(
                identity.register(username, payload.get("password"), payload.get("invite"))
            )
        role = payload.get("role")
        if role not in {None, "admin", "member"}:
            raise AuthError("로그인 경로를 확인해주세요.", 400)
        return jsonify(identity.login(username, payload.get("password"), expected_role=role))

    @app.post(PREFIX + "/auth/logout")
    @authenticated
    def logout_auth():
        identity.revoke(g.token)
        return jsonify(ok=True)

    @app.post(PREFIX + "/invites")
    @authenticated
    def create_invite():
        if g.user.get("role") != "admin":
            raise AuthError("관리자만 초대 코드를 만들 수 있습니다.", 403)
        hours = g.payload.get("ttlHours", 24)
        if hours is None:
            hours = 24
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 168:
            raise AuthError("초대 유효 시간은 1시간에서 7일 사이로 정해주세요.", 400)
        token = identity.create_invite(ttl=hours * 3600)
        return jsonify(
            invite=token,
            ttlHours=hours,
            expiresAt=timestamp(identity.clock() + hours * 3600),
        )

    @app.post(PREFIX + "/bootstrap")
    @authenticated
    def bootstrap():
        result = gateway.bootstrap(g.user["storage_id"])
        result["user"] = {key: g.user[key] for key in ("id", "username", "role")}
        result["capabilities"] = {
            "korail": booking_available,
            "srt": False,
            "waitlist": False,
            "scheduledSearch": booking_available,
            "durableNotifications": notifications is not None,
            "favourites": booking_available,
            "notificationSettings": booking_available,
            "push": bool(notifications and notifications.push_available),
            "lastChecked": False,
        }
        result["notifications"] = {
            "pushAvailable": bool(notifications and notifications.push_available)
        }
        return jsonify(result)

    def call_gateway(method, payload=False):
        result = getattr(gateway, method)(g.user["storage_id"], *([g.payload] if payload else []))
        return jsonify(result)

    for path, method, payload in [
        ("trains", "list_trains", True),
        ("search", "start_search", True),
        ("schedule", "schedule_search", True),
        ("search/cancel", "cancel_search", False),
        ("reservations/cancel", "cancel_pending", False),
        ("access-request", "request_access", False),
        ("favourites", "save_favourite", True),
        ("logout", "logout", False),
    ]:

        def action(method=method, payload=payload):
            return call_gateway(method, payload)

        app.add_url_rule(PREFIX + "/" + path, method, authenticated(action), methods=["POST"])

    @app.post(PREFIX + "/register")
    @authenticated
    def register_rail():
        for key in ("username", "password"):
            if not isinstance(g.payload.get(key), str) or not 1 <= len(g.payload[key]) <= 128:
                raise AuthError("철도 로그인 정보를 확인해주세요.", 400)
        return jsonify(
            gateway.register(g.user["storage_id"], g.payload["username"], g.payload["password"])
        )

    @app.get(PREFIX + "/status")
    @authenticated
    def status():
        return call_gateway("status")

    @app.get(PREFIX + "/favourites")
    @authenticated
    def favourites():
        return jsonify(favourites=gateway.favourites(g.user["storage_id"]))

    @app.delete(PREFIX + "/favourites/<fav_id>")
    @authenticated
    def delete_favourite(fav_id):
        return jsonify(gateway.delete_favourite(g.user["storage_id"], fav_id))

    @app.post(PREFIX + "/notify")
    @authenticated
    def notify():
        return jsonify(gateway.set_notify_minutes(g.user["storage_id"], g.payload.get("minutes")))

    @app.get(PREFIX + "/notifications")
    @authenticated
    def events():
        return jsonify(
            items=notifications.items(g.user["storage_id"]) if notifications else [],
            pushAvailable=bool(notifications and notifications.push_available),
        )

    @app.route(PREFIX + "/devices", methods=["POST", "DELETE"])
    @authenticated
    def devices():
        if not notifications or not notifications.push_available:
            raise AuthError(
                "서버에 푸시 알림이 설정되지 않았습니다. 앱 알림함을 이용해주세요.", 503
            )
        token = g.payload.get("token")
        if not isinstance(token, str) or not 20 <= len(token) <= 4096:
            raise AuthError("기기 토큰을 확인해주세요.", 400)
        if request.method == "POST":
            if g.payload.get("platform") != "android":
                raise AuthError("지원하지 않는 기기입니다.", 400)
            notifications.register_device(g.user["storage_id"], token)
        else:
            notifications.remove_device(g.user["storage_id"], token)
        return jsonify(ok=True, pushAvailable=True)

    return app
