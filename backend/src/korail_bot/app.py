"""
Flask application entry point for Korail KTX Telegram Bot.

This is the refactored version using the new service-oriented architecture.
"""

import atexit
import signal
import sys
import threading

from flask import Flask
from flask_restful import Api
from werkzeug.serving import is_running_from_reloader

from korail_bot import __version__
from korail_bot.api import PaymentCheckAPI, ReservationCallbackAPI
from korail_bot.config.settings import settings
from korail_bot.handlers import TelegramUpdateProcessor
from korail_bot.public_app import serve_public_app
from korail_bot.runtime import mark_start
from korail_bot.services import (
    PaymentReminderService,
    PaymentWatchdogService,
    ReleaseAnnouncer,
    ReservationService,
    ScheduledSearchService,
    SearchWatchdogService,
    SrtMigrationNotifier,
    TelegramPoller,
    TelegramService,
)
from korail_bot.services.mini_app_gateway import MiniAppGateway
from korail_bot.startup import publish_command_menus
from korail_bot.storage.redis import RedisStorage
from korail_bot.utils.logger import LoggerFactory, get_logger

# Configure logging
logger = get_logger(__name__)

# Stamped here rather than where it is read: /version reports how long the bot
# has been up, and the clock has to start before Redis, the services and the
# poller do. Measured from the end of startup it would be short by however long
# starting took, and measured on first use it would say "1분 미만" to whoever
# asked first, however long the bot had really been running.
mark_start()

# Validate settings
try:
    settings.validate()
except ValueError as e:
    logger.error(f"Configuration error: {e}")
    sys.exit(1)

# Surface non-fatal misconfiguration (weak secrets, disabled protections)
for warning in settings.warnings():
    logger.warning(f"⚠️  {warning}")

# Set recursion limit
sys.setrecursionlimit(settings.RECURSION_LIMIT)

# Create Flask application.
#
# This one is the internal listener: the background search processes report
# here over loopback, and nothing else may. It used to carry a blanket
# CORS(application), which allowed any origin to have a browser call these
# endpoints. Nothing needed it - a subprocess making an HTTP request is not
# subject to the same-origin policy at all - and the Mini App does not use
# this app. The Mini App's own listener is built in korail_bot.public_app and
# is same-origin, so it needs no CORS either.
application = Flask(__name__)
api = Api(application)

# Initialize storage (Redis)
try:
    storage = RedisStorage()
    logger.info("✅ Redis storage initialized successfully")
    # Restore debug mode from Redis
    if storage.is_debug_mode():
        LoggerFactory.set_log_level("DEBUG")
        logger.info("Debug mode restored from Redis - log level set to DEBUG")
except Exception as e:
    logger.error(f"❌ Failed to initialize Redis storage: {e}")
    logger.error("Please ensure Redis is running and accessible")
    sys.exit(1)

# Initialize services
telegram_service = TelegramService(settings.TELEGRAM_BOT_TOKEN)
reservation_service = ReservationService(storage, telegram_service)
payment_reminder_service = PaymentReminderService(storage, telegram_service)
scheduled_search_service = ScheduledSearchService(storage, telegram_service, reservation_service)
search_watchdog_service = SearchWatchdogService(reservation_service)
payment_watchdog_service = PaymentWatchdogService(storage, telegram_service)

# Configure API resources with dependency injection
api.add_resource(
    ReservationCallbackAPI,
    "/reservation-callback",
    resource_class_kwargs={
        "storage": storage,
        "telegram_service": telegram_service,
        "payment_reminder_service": payment_reminder_service,
    },
)

api.add_resource(PaymentCheckAPI, "/check_payment", resource_class_kwargs={"storage": storage})

logger.info("=" * 60)
logger.info(f"Korail KTX Telegram Bot v{__version__} - Redis Version")
logger.info("=" * 60)
logger.info("Telegram updates: long polling")
logger.info(f"Flask host: {settings.FLASK_HOST}")
logger.info(f"Flask port: {settings.FLASK_PORT}")
logger.info(f"Debug mode: {settings.FLASK_DEBUG}")
logger.info(f"Log level: {settings.LOG_LEVEL}")
logger.info(f"Redis: {settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}")
if settings.KORAIL_SEARCH_INTERVAL_JITTER > 0:
    _jitter_spread = settings.KORAIL_SEARCH_INTERVAL * settings.KORAIL_SEARCH_INTERVAL_JITTER
    logger.info(
        f"Search interval: {settings.KORAIL_SEARCH_INTERVAL}s "
        f"randomised over {settings.KORAIL_SEARCH_INTERVAL - _jitter_spread:.2f}"
        f"-{settings.KORAIL_SEARCH_INTERVAL + _jitter_spread:.2f}s "
        f"(jitter {settings.KORAIL_SEARCH_INTERVAL_JITTER:.0%})"
    )
else:
    logger.info(f"Search interval: {settings.KORAIL_SEARCH_INTERVAL}s (fixed)")
logger.info(f"Payment timeout: {settings.PAYMENT_TIMEOUT_MINUTES}min")
logger.info(f"Reminder interval: {settings.PAYMENT_REMINDER_INTERVAL_SECONDS}s")
logger.info(f"Payment verify interval: {settings.PAYMENT_VERIFY_INTERVAL_SECONDS}s")
logger.info("Public Telegram endpoint: disabled")
logger.info(f"Telegram Mini App: {'enabled' if settings.mini_app_enabled() else 'disabled'}")
logger.info(
    "Mini App API: "
    + (
        f"port {settings.MINI_APP_API_PORT}, published"
        if settings.MINI_APP_API_ENABLED
        else "disabled"
    )
)
logger.info(f"Admin commands: {'enabled' if settings.ADMIN_PASSWORD else 'disabled'}")
logger.info(f"Developer mode: {'enabled' if settings.ADMIN_MAGIC_STRING else 'disabled'}")
logger.info(
    "Korail login: "
    + (
        "registered per user; developer chats use USERID/USERPW"
        if settings.has_preconfigured_korail_credentials()
        else "registered per user"
    )
)
logger.info(f"Resume on restart: {'enabled' if settings.RESUME_ON_RESTART else 'disabled'}")
logger.info("=" * 60)

# The bot pulls updates from Telegram. Flask remains for background reservation
# processes to report results over loopback to /reservation-callback and
# /check_payment.
#
# The Flask reloader executes this module in two processes, which would give
# the bot token two competing consumers and earn a 409 from Telegram.
# A search lives in a child process, so anything recorded by a previous run of
# this app is now unattended. Resume it or tell the user, before new updates
# start arriving and /status has a chance to lie about it.
if not is_running_from_reloader():
    reservation_service.reconcile_after_restart()

    # Publish the command menu, for everyone and for the operators. Best
    # effort: Telegram being unreachable at startup means the menus keep what
    # they had, which is not a reason to refuse to start a bot whose searches
    # run over a different connection.
    publish_command_menus(storage, telegram_service)

    # Whoever runs the server updates it; the people using it find out by
    # noticing that something moved. Told once, on the start after a version
    # change, and off the startup path: a message per user is not a thing to
    # keep the searches waiting on.
    ReleaseAnnouncer(storage, telegram_service).announce_in_background()
    # Separate from the release notice on purpose: this one is owed to a
    # handful of chats rather than to everyone, and it stops being owed as
    # soon as each of them has been told.
    SrtMigrationNotifier(storage, telegram_service).notify_in_background()

# Searches booked for a later time are held in Redis and started by this
# thread. It lives in the app rather than in a process of its own because a
# process asleep until tomorrow morning dies with the next restart, while a
# record in Redis is picked up by whatever is running when the time comes.
if not is_running_from_reloader():
    scheduled_search_service.start()

# A search that dies takes its record with it into a lie: the record says a
# search is running, and nothing else is watching to contradict it. This
# thread does, and tells the user, because the alternative is them waiting out
# the afternoon on a process that stopped at lunchtime.
if not is_running_from_reloader():
    search_watchdog_service.start()

# A payment is watched by the search process that took the seat, which is
# already logged in - but that process does not exist for a random-seating run
# and does not survive a restart. This thread watches whatever is left, so a
# payment is confirmed in the chat within seconds either way.
if not is_running_from_reloader():
    payment_watchdog_service.start()

poller = None
update_processor = None
if not is_running_from_reloader():
    update_processor = TelegramUpdateProcessor(
        storage, telegram_service, reservation_service, payment_reminder_service
    )
    poller = TelegramPoller(settings.TELEGRAM_BOT_TOKEN, update_processor)
    poller.start()

# The Mini App's listener, on a port of its own. It shares this process - and
# so the same conversation handler, the same registry of running searches and
# the same Redis connection - with the chat, which is the whole reason a
# reservation started in the app behaves identically to one started in chat.
#
# What it does not share is the socket: the internal callbacks above are not
# registered on it. See korail_bot.public_app for why that separation is
# structural rather than a check.
if not is_running_from_reloader() and update_processor is not None:
    serve_public_app(
        MiniAppGateway(
            storage,
            telegram_service,
            reservation_service,
            conversation_handler=update_processor.conversation_handler,
            scheduled_search_service=scheduled_search_service,
        )
    )


_shutdown_lock = threading.Lock()
_shutdown_done = False


def shutdown() -> None:
    """
    Stop polling and take the running searches down with the app.

    Runs at most once, from whichever of the signal handler, the exit hook or
    the main loop gets here first.
    """
    global _shutdown_done
    with _shutdown_lock:
        if _shutdown_done:
            return
        _shutdown_done = True

    logger.info("Shutting down...")

    if poller:
        poller.stop()

    scheduled_search_service.stop()
    search_watchdog_service.stop()
    payment_watchdog_service.stop()

    try:
        reservation_service.shutdown()
    except Exception as e:
        logger.error(f"Error while stopping search processes: {e}", exc_info=True)

    logger.info("Shutdown complete")


def _handle_stop_signal(signum, _frame):
    """
    Leave the main loop so the exit path runs.

    Without a handler here SIGTERM is fatal on the spot, and the searches this
    process started are left behind with nowhere to report to. In a container
    it is worse than that: the app is PID 1, and PID 1 ignores every signal it
    has not explicitly asked for - 'docker stop' would wait out its whole
    timeout and then SIGKILL the app.
    """
    logger.info(f"Received {signal.Signals(signum).name}")
    raise SystemExit(0)


# The reloader runs this module in two processes; only the one that owns the
# poller and the search processes should be tearing anything down.
if not is_running_from_reloader():
    atexit.register(shutdown)
    try:
        signal.signal(signal.SIGTERM, _handle_stop_signal)
        signal.signal(signal.SIGINT, _handle_stop_signal)
    except ValueError:
        # Only the main thread may install handlers. A WSGI server that
        # imports this module from a worker thread does its own signal
        # handling; the exit hook above still runs when it tears the worker
        # down.
        logger.debug("Not the main thread - leaving signal handling alone")

if __name__ == "__main__":
    logger.info("Starting Flask application...")
    try:
        application.run(
            debug=settings.FLASK_DEBUG,
            host=settings.FLASK_HOST,
            port=settings.FLASK_PORT,
            threaded=True,
        )
    finally:
        shutdown()
