"""
The HTTP surface the Mini App talks to, and the only one exposed.

Every route here is behind ``verify_init_data``: the chat ID is taken from
Telegram's signature and never from the request body, so a caller cannot act
as somebody else by naming them. The gateway does the work; this file is the
translation between HTTP and it, and the place where a refusal becomes a
status code.

Nothing here is registered on the app that serves /reservation-callback. That
separation is the point - see ``korail_bot.public_app``.
"""

from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import Blueprint, Response, jsonify, request

from korail_bot.api.mini_app_auth import MiniAppAuthError, RateLimiter, verify_init_data
from korail_bot.config.settings import settings
from korail_bot.services.mini_app_gateway import MiniAppError, MiniAppGateway
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)

#: Sent as a header rather than a query parameter. Query strings end up in
#: proxy access logs and browser history; this value is a bearer credential
#: for the whole of a Mini App session.
INIT_DATA_HEADER = "X-Telegram-Init-Data"
TIMEZONE_HEADER = "X-User-Timezone"


def _error(message: str, status: int) -> tuple[Response, int]:
    """One shape for every refusal, so the app has one thing to render."""
    return jsonify({"error": message}), status


def build_blueprint(gateway: MiniAppGateway) -> Blueprint:
    """
    Wire the Mini App routes onto a blueprint.

    Built as a function rather than a module-level blueprint so the gateway
    is injected: tests mount it on a throwaway Flask app with a stand-in, and
    nothing has to import the real Redis to exercise a route.

    Args:
        gateway: What actually does the work

    Returns:
        The blueprint, ready to register under a URL prefix
    """
    blueprint = Blueprint("mini_app", __name__)

    general = RateLimiter(settings.MINI_APP_RATE_LIMIT, settings.MINI_APP_RATE_WINDOW_SECONDS)
    # A tighter budget for the routes that make this host talk to a railway.
    # Those cost somebody else's server, not only ours.
    rail = RateLimiter(
        settings.MINI_APP_RAIL_RATE_LIMIT, settings.MINI_APP_RAIL_RATE_WINDOW_SECONDS
    )

    def authenticated(limiter: RateLimiter = general) -> Callable:
        """Establish the caller, or answer without running the route."""

        def decorate(view: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(view)
            def guarded(*args: Any, **kwargs: Any):
                raw = request.headers.get(INIT_DATA_HEADER, "")
                try:
                    identity = verify_init_data(raw)
                except MiniAppAuthError as exc:
                    # Logged without the payload: it is a valid credential for
                    # whoever holds it, and this log is not the place for one.
                    logger.warning(f"Refused a Mini App request: {exc}")
                    return _error("인증에 실패했습니다. 예약 화면을 닫았다가 다시 열어주세요.", 401)

                if not limiter.allow(str(identity.chat_id)):
                    logger.warning(f"Rate limited chat_id={identity.chat_id}")
                    return _error("요청이 너무 잦습니다. 잠시 후 다시 시도해주세요.", 429)

                # Telegram's Bot API does not carry a user's time zone. The
                # browser does, so every authenticated Mini App request keeps
                # the chat preference current before any clock is rendered.
                timezone_name = request.headers.get(TIMEZONE_HEADER, "")
                if timezone_name:
                    gateway.sync_timezone(identity.chat_id, timezone_name)

                try:
                    return view(identity.chat_id, *args, **kwargs)
                except MiniAppError as exc:
                    return _error(exc.message, exc.status)
                except Exception:
                    # The traceback goes to the log, never to the page: it
                    # names internals and this endpoint faces the internet.
                    logger.error(
                        f"Unhandled error serving {request.path} for chat_id={identity.chat_id}",
                        exc_info=True,
                    )
                    return _error("처리 중 문제가 생겼습니다. 잠시 후 다시 시도해주세요.", 500)

            return guarded

        return decorate

    def body() -> dict:
        """The JSON body, or an empty one - never an exception."""
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    # ==================== Opening ====================

    @blueprint.post("/bootstrap")
    @authenticated()
    def bootstrap(chat_id: int):
        return jsonify(gateway.bootstrap(chat_id))

    # ==================== Account ====================

    @blueprint.post("/register")
    @authenticated(rail)
    def register(chat_id: int):
        payload = body()
        return jsonify(
            gateway.register(
                chat_id,
                str(payload.get("username", "")),
                str(payload.get("password", "")),
            )
        )

    @blueprint.post("/logout")
    @authenticated()
    def logout(chat_id: int):
        return jsonify(gateway.logout(chat_id))

    # ==================== Choosing and starting ====================

    @blueprint.post("/trains")
    @authenticated(rail)
    def trains(chat_id: int):
        return jsonify(gateway.list_trains(chat_id, body()))

    @blueprint.post("/search")
    @authenticated(rail)
    def search(chat_id: int):
        return jsonify(gateway.start_search(chat_id, body()))

    @blueprint.post("/schedule")
    @authenticated(rail)
    def schedule(chat_id: int):
        return jsonify(gateway.schedule_search(chat_id, body()))

    @blueprint.post("/search/cancel")
    @authenticated()
    def cancel_search(chat_id: int):
        return jsonify(gateway.cancel_search(chat_id))

    # ==================== Seats already taken ====================

    @blueprint.get("/status")
    @authenticated()
    def status(chat_id: int):
        return jsonify(gateway.status(chat_id))

    @blueprint.post("/reservations/cancel")
    @authenticated(rail)
    def cancel_reservations(chat_id: int):
        return jsonify(gateway.cancel_pending(chat_id))

    @blueprint.post("/access-request")
    @authenticated()
    def access_request(chat_id: int):
        return jsonify(gateway.request_access(chat_id))

    # ==================== Favourites and notifications ====================

    @blueprint.get("/favourites")
    @authenticated()
    def list_favourites(chat_id: int):
        return jsonify({"favourites": gateway.favourites(chat_id)})

    @blueprint.post("/favourites")
    @authenticated()
    def add_favourite(chat_id: int):
        return jsonify(gateway.save_favourite(chat_id, body()))

    @blueprint.delete("/favourites/<fav_id>")
    @authenticated()
    def remove_favourite(chat_id: int, fav_id: str):
        return jsonify(gateway.delete_favourite(chat_id, fav_id))

    @blueprint.post("/notify")
    @authenticated()
    def notify(chat_id: int):
        return jsonify(gateway.set_notify_minutes(chat_id, body().get("minutes")))

    return blueprint
