"""
The one listener that faces the internet, and what is deliberately not on it.

The bot already had an HTTP server, and the obvious way to serve a Mini App
API was to add routes to it. That would have been a mistake worth naming.

``/reservation-callback`` can send arbitrary text to an arbitrary chat. It
defends itself by requiring a loopback source address, which is sound while
nothing forwards to it - and stops being sound the moment a reverse proxy
does, because a proxy connects from localhost and so every request through it
has a loopback source address, including requests from the internet. The check
would pass for the whole world.

Tightening the check is one way. Serving that route on a socket the world
cannot reach is a better one: it removes the question instead of answering it,
and it stays removed when somebody later adds a route and forgets which app
they added it to. So this module builds a second Flask app carrying only the
Mini App API and the page itself, and only this one is published.
"""

import mimetypes
import threading
from pathlib import Path

from flask import Flask, Response, send_from_directory

from korail_bot import __version__
from korail_bot.api.mini_app import build_blueprint
from korail_bot.config.settings import settings
from korail_bot.services.mini_app_gateway import MiniAppGateway
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)

#: The page's own files, which are stamped with the version as it is served.
#:
#: Telegram keeps a Mini App running when it is minimised rather than closed,
#: so a deploy does not reach someone who never shut the page: they go on
#: running the JavaScript they opened with, against a bot that has moved on.
#: This is not theoretical - a seat-selection screen shipped and was reported
#: missing, and what was in the phone was the build before it.
#:
#: Cache headers do not settle it. These files already go out no-cache and the
#: WebView had them anyway. A URL that has changed is the one thing no cache
#: can answer from what it already holds.
STAMPED_ASSETS = ("app.js", "app.css")

#: Where the Mini App's own files live, relative to the repository root. The
#: page is served from here rather than from a static host so that it shares
#: an origin with the API - no CORS to widen, and no third party who can
#: replace the login screen of a bot that handles railway accounts.
WEBAPP_DIRECTORY = Path(__file__).resolve().parent.parent.parent / "webapp"

API_PREFIX = "/api"


def _webapp_root() -> Path:
    """Where to serve the page from, wherever this is installed."""
    configured = (settings.MINI_APP_WEBAPP_DIR or "").strip()
    return Path(configured).resolve() if configured else WEBAPP_DIRECTORY


def stamped_index(root: Path, version: str = __version__) -> str:
    """
    The page, with its own assets pointed at a versioned URL.

    Done here rather than in the file so that ``webapp/index.html`` stays a
    page that opens and works on its own - a placeholder baked into the source
    would be a literal in every context that is not this function.

    Only the app's own two files are stamped. Telegram's SDK is somebody
    else's URL and not ours to decorate.

    Args:
        root: Where the page's files live
        version: What to stamp them with; the running version by default

    Returns:
        The page's HTML, ready to send
    """
    html = (root / "index.html").read_text(encoding="utf-8")
    for asset in STAMPED_ASSETS:
        html = html.replace(f'"{asset}"', f'"{asset}?v={version}"')
    return html


def create_public_app(gateway: MiniAppGateway) -> Flask:
    """
    Build the internet-facing app.

    Args:
        gateway: What the API routes call into

    Returns:
        A Flask app carrying the Mini App API and the Mini App page, and
        nothing else
    """
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(build_blueprint(gateway), url_prefix=API_PREFIX)

    root = _webapp_root()

    # Telegram renders the page inside a WebView it controls, and the API is
    # same-origin, so nothing needs to frame this or fetch it from elsewhere.
    # Saying so costs one header each and closes clickjacking and sniffing.
    @app.after_request
    def _harden(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            # 'unsafe-inline' for style only: Telegram's own SDK is the one
            # script allowed from elsewhere, and the page's styles are in a
            # file of their own but its theme variables are set inline.
            "default-src 'self'; "
            "script-src 'self' https://telegram.org; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors https://telegram.org https://*.telegram.org; "
            "base-uri 'none'; "
            "form-action 'none'",
        )
        # A signed initData is a credential; a cached response keyed only by
        # URL is how one user's status ends up on another user's screen.
        if response.headers.get("Content-Type", "").startswith("application/json"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/health")
    def health() -> Response:
        """Answers the container healthcheck. Names nothing and needs no auth."""
        return Response("ok", mimetype="text/plain")

    @app.get("/")
    def index():
        # Read per request rather than once at startup: the file is small,
        # and a page held in memory is a page that goes on being served after
        # it has been edited - which is the class of problem this is fixing.
        response = Response(stamped_index(root), mimetype="text/html")
        # The stamp only works while this page itself is fresh. A cached copy
        # names the previous version's assets, and the versioning buys
        # nothing. no-store rather than no-cache because the thing being
        # guarded against is a client that revalidates when it feels like it.
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/<path:filename>")
    def asset(filename: str):
        """
        Serve one of the app's own files.

        ``send_from_directory`` is what refuses a traversal: it resolves the
        name under the root and raises rather than answering when the result
        escapes. The extension check on top of it means a file that lands in
        this directory by accident is not automatically published.
        """
        if Path(filename).suffix.lower() not in {".html", ".css", ".js", ".svg", ".png", ".ico"}:
            return Response("Not Found", status=404)
        return send_from_directory(root, filename)

    return app


def serve_public_app(gateway: MiniAppGateway) -> threading.Thread | None:
    """
    Start the public listener in a thread of this process.

    A thread rather than a second container: the gateway reaches the same
    ConversationHandler, the same registry of running searches and the same
    Redis connection the bot is already using, and a second process would
    have its own of each.

    Args:
        gateway: What the API routes call into

    Returns:
        The thread serving it, or None when the Mini App API is switched off
    """
    if not settings.MINI_APP_API_ENABLED:
        logger.info("Mini App API: disabled")
        return None

    from waitress import serve

    app = create_public_app(gateway)

    # Explicit, because a static page served with the wrong type is a page
    # the WebView refuses to run rather than one that renders oddly.
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")

    def run() -> None:
        try:
            serve(
                app,
                host=settings.MINI_APP_API_HOST,
                port=settings.MINI_APP_API_PORT,
                threads=settings.MINI_APP_API_THREADS,
                # This listener sits behind a reverse proxy that terminates
                # TLS. Without this, waitress logs and any generated URL claim
                # http:// on the port it happens to be bound to.
                url_scheme="https",
                ident="korail-bot",
            )
        except Exception:
            # The bot's real work is long polling, which does not go through
            # here. A listener that could not bind must be loud, and must not
            # take the searches down with it.
            logger.error("Mini App API listener stopped", exc_info=True)

    thread = threading.Thread(target=run, name="mini-app-api", daemon=True)
    thread.start()
    logger.info(
        f"Mini App API: listening on {settings.MINI_APP_API_HOST}:{settings.MINI_APP_API_PORT} "
        f"(internal callbacks are not served here)"
    )
    return thread
