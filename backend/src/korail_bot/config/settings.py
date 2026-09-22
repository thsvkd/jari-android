"""
Application configuration management.

This module centralizes all configuration variables from environment
and provides type-safe access to settings throughout the application.
"""

import os
import secrets
from urllib.parse import urlsplit


def _digits(value: str) -> str:
    """Keep only the digits of a value, so phone formats stop mattering."""
    return "".join(character for character in value if character.isdigit())


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _env_ratio(name: str, default: float) -> float:
    """Read a ratio between 0 and 1, falling back on anything unusable."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(max(value, 0.0), 1.0)


def _env_int_at_least(name: str, default: int, minimum: int) -> int:
    """Read a whole number, refusing to go below a floor.

    A too-small interval is not a preference to be honoured: it is a phone
    buzzing so often that the message it carries stops being read. Anything
    unusable - blank, not a number - falls back on the default rather than
    stopping the bot from starting over a typo in a tuning knob.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, minimum)


def _internal_callback_token() -> str:
    """
    Token shared between the Flask app and its background reservation processes.

    Generated once per app start when not supplied. It is written back into
    os.environ so that subprocesses spawned by the app inherit it automatically.
    """
    token = os.environ.get("INTERNAL_CALLBACK_TOKEN")
    if not token:
        token = secrets.token_urlsafe(32)
        os.environ["INTERNAL_CALLBACK_TOKEN"] = token
    return token


class Settings:
    """Application settings loaded from environment variables."""

    # Telegram Bot Configuration
    TELEGRAM_BOT_TOKEN: str = os.environ.get("BOTTOKEN", "")
    TELEGRAM_API_BASE_URL: str = "https://api.telegram.org/bot{token}"
    # Long-poll parameters. The HTTP read timeout must exceed the long-poll
    # timeout, otherwise every idle poll would look like a network failure.
    TELEGRAM_POLL_TIMEOUT: int = 30
    TELEGRAM_POLL_REQUEST_TIMEOUT: int = 35
    # Optional Telegram Mini App that carries the whole reservation, from the
    # travel conditions through the live train list to starting the search.
    # The page is a thin client: it holds no Korail logic and reaches this app
    # over the API below, which is the only thing published to the internet.
    MINI_APP_URL: str | None = (os.environ.get("MINI_APP_URL") or "").strip() or None

    # The public listener. Deliberately a second WSGI server on a port of its
    # own rather than more routes on the existing one. /reservation-callback
    # can send arbitrary text to arbitrary chats and defends itself by
    # requiring a loopback source address - and behind a reverse proxy every
    # request has one, including requests from the internet. Serving it on a
    # listener that is never exposed removes the question instead of answering
    # it: the route does not exist on the socket the world can reach.
    MINI_APP_API_ENABLED: bool = _env_bool("MINI_APP_API_ENABLED", False)
    # Binds to every interface by default: this listener exists to be reached
    # from outside the container, and the container publishes nothing to the
    # host that compose has not been told to publish.
    MINI_APP_API_HOST: str = os.environ.get("MINI_APP_API_HOST", "0.0.0.0")
    MINI_APP_API_PORT: int = int(os.environ.get("MINI_APP_API_PORT", "8081"))
    MINI_APP_API_THREADS: int = int(os.environ.get("MINI_APP_API_THREADS", "8"))
    # Where the Mini App's own files are. Empty means the webapp/ directory of
    # this checkout, which is right for both the container and a dev run.
    MINI_APP_WEBAPP_DIR: str | None = (os.environ.get("MINI_APP_WEBAPP_DIR") or "").strip() or None

    # How long a Telegram initData payload stays usable. Telegram signs it
    # once at launch and the page reuses it for the whole session, so this is
    # also how long a session may last before the app has to be reopened.
    MINI_APP_INIT_DATA_TTL_SECONDS: int = int(
        os.environ.get("MINI_APP_INIT_DATA_TTL_SECONDS", "86400")
    )
    # Tolerance for a phone whose clock runs ahead of this host's.
    MINI_APP_CLOCK_SKEW_SECONDS: int = int(os.environ.get("MINI_APP_CLOCK_SKEW_SECONDS", "300"))
    MINI_APP_INIT_DATA_MAX_BYTES: int = int(os.environ.get("MINI_APP_INIT_DATA_MAX_BYTES", "4096"))

    # Per-caller caps. The lower one guards the endpoints that reach Korail or
    # SR over the network, where an unbounded caller costs the railway too.
    MINI_APP_RATE_LIMIT: int = int(os.environ.get("MINI_APP_RATE_LIMIT", "60"))
    MINI_APP_RATE_WINDOW_SECONDS: int = int(os.environ.get("MINI_APP_RATE_WINDOW_SECONDS", "60"))
    MINI_APP_RAIL_RATE_LIMIT: int = int(os.environ.get("MINI_APP_RAIL_RATE_LIMIT", "10"))
    MINI_APP_RAIL_RATE_WINDOW_SECONDS: int = int(
        os.environ.get("MINI_APP_RAIL_RATE_WINDOW_SECONDS", "60")
    )

    # Korail Configuration
    # Credentials kept on the server. When both are set the bot logs in with
    # them instead of asking each user for a phone number and a password -
    # see has_preconfigured_korail_credentials().
    KORAIL_ADMIN_USER_ID: str | None = (os.environ.get("USERID") or "").strip() or None
    # Not stripped: a password may legitimately begin or end with a space.
    KORAIL_ADMIN_PASSWORD: str | None = os.environ.get("USERPW") or None
    KORAIL_SEARCH_INTERVAL: float = float(os.environ.get("SEARCH_INTERVAL", "1"))  # seconds
    # Every wait between Korail requests is drawn from
    # interval * (1 +/- jitter) instead of being a fixed number of seconds, so
    # the search does not knock on the door on a metronome. 0 disables it and
    # restores the old fixed interval.
    KORAIL_SEARCH_INTERVAL_JITTER: float = _env_ratio("SEARCH_INTERVAL_JITTER", 0.4)
    # Korail closes a login server-side after a while, and a search that runs
    # for hours has to renew it. Drawn afresh after every login rather than
    # kept as a fixed period, for the same reason the search interval is.
    # 0 stops renewing ahead of time and waits for Korail to reject the
    # session instead, which is when the client re-authenticates anyway.
    KORAIL_RELOGIN_INTERVAL: float = float(os.environ.get("RELOGIN_INTERVAL", "1800"))
    KORAIL_RELOGIN_INTERVAL_JITTER: float = _env_ratio("RELOGIN_INTERVAL_JITTER", 0.4)
    # A search that finds nothing and a search that could not ask both end up
    # with no trains, so a run of failures is the only thing that tells them
    # apart. How long a run has to get before the user hears about it:
    KORAIL_FAILURE_ALERT_THRESHOLD: int = int(
        os.environ.get("SEARCH_FAILURE_ALERT_THRESHOLD", "10")
    )
    # How long to wait before saying it again, while it is still failing.
    KORAIL_FAILURE_REALERT_SECONDS: float = float(
        os.environ.get("SEARCH_FAILURE_REALERT_SECONDS", "1800")
    )
    # Failures back the search off instead of keeping it at full rate: if
    # Korail is refusing us, asking every second is what makes it worse. The
    # wait doubles per failure, up to this multiple of the search interval.
    KORAIL_FAILURE_BACKOFF_CAP: float = float(os.environ.get("SEARCH_FAILURE_BACKOFF_CAP", "60"))
    # The mobile app build the client reports itself as. korail2 carries the
    # build that was current when it was packaged; this is the escape hatch
    # for when Korail stops serving that build and the library has not caught
    # up yet. Unset means whatever the library ships.
    KORAIL_APP_VERSION: str | None = os.environ.get("KORAIL_APP_VERSION") or None
    # Where the user is sent to pay for a seat the bot just reserved.
    #
    # Korail moved letskorail.com to korail.com and rebuilt the site as a
    # single-page app, which took the old .do pages with it: the address this
    # used to carry now redirects to a 404. Overridable, because that has
    # happened once and the link is only useful while it points somewhere.
    #
    # /ticket/reservation/list is the reservation list - the site's own
    # RESERVATION_LIST route - and it is where an unpaid reservation is paid
    # for. Signed-out visitors are asked to log in and returned here
    # afterwards, so the link works whatever state the browser is in.
    KORAIL_PAYMENT_URL: str = os.environ.get(
        "KORAIL_PAYMENT_URL", "https://www.korail.com/ticket/reservation/list"
    )
    # The station guide, offered when a station name has to be typed.
    KORAIL_STATION_LIST_URL: str = os.environ.get(
        "KORAIL_STATION_LIST_URL", "https://www.korail.com/ticket/train/stationGuide/station"
    )
    # Payment Reminder Configuration
    PAYMENT_TIMEOUT_MINUTES: int = int(os.environ.get("PAYMENT_TIMEOUT_MINUTES", "10"))
    # How often the "you have not paid yet" message repeats.
    #
    # Half a minute: twenty messages across a ten-minute window. A minute was
    # quiet enough to miss when the deadline is what matters, and the older
    # ten seconds meant sixty buzzes at someone typing a card number into
    # another app.
    #
    # The floor is what keeps a well-meant "make it more urgent" from turning
    # the reminder into the thing being ignored. Below it the value is raised
    # rather than refused: a badly tuned knob should not stop the bot.
    PAYMENT_REMINDER_MIN_INTERVAL_SECONDS: int = 10
    PAYMENT_REMINDER_INTERVAL_SECONDS: int = _env_int_at_least(
        "PAYMENT_REMINDER_INTERVAL", 30, PAYMENT_REMINDER_MIN_INTERVAL_SECONDS
    )
    # How often a watcher asks the railway whether the reservation is still
    # unpaid. One listing request each time - the same cadence a search runs
    # at, and for the same reason: the answer is only useful while it is
    # fresh. Someone who has just paid should be told within seconds, not
    # left wondering whether the bot noticed.
    PAYMENT_VERIFY_INTERVAL_SECONDS: int = int(os.environ.get("PAYMENT_VERIFY_INTERVAL", "3"))
    # How long a watcher's claim on one chat's payment stays good without
    # being renewed.
    #
    # Two things can watch a payment: the search process that took the seat,
    # which is already logged in, and the app, which picks up what nobody is
    # watching. The claim is what keeps them from both polling the same
    # reservation and both announcing it. Several intervals long, so an
    # ordinary slow request does not hand the watch over, and short enough
    # that a killed process is replaced in seconds.
    PAYMENT_WATCH_LEASE_SECONDS: int = int(
        os.environ.get("PAYMENT_WATCH_LEASE", str(max(10, PAYMENT_VERIFY_INTERVAL_SECONDS * 4)))
    )

    # Progress reports from a running search
    #
    # A search can run for hours without a word, and silence is
    # indistinguishable from a bot that died. /notify turns on a periodic
    # "still going" message and picks how often it comes. Off unless asked
    # for: an unwanted message every five minutes is worse than the silence.
    #
    # The bounds are what a chat may ask for. A report costs nothing but
    # attention, and one every ten seconds would cost a great deal of it.
    PROGRESS_REPORT_MIN_MINUTES: int = int(os.environ.get("PROGRESS_REPORT_MIN_MINUTES", "1"))
    PROGRESS_REPORT_MAX_MINUTES: int = int(os.environ.get("PROGRESS_REPORT_MAX_MINUTES", "180"))
    # What "/notify on" means, and what the keyboard offers first.
    PROGRESS_REPORT_DEFAULT_MINUTES: int = int(
        os.environ.get("PROGRESS_REPORT_DEFAULT_MINUTES", "5")
    )
    # How long the search process may reuse the preference it last read out of
    # Redis. Bounds how quickly a /notify takes effect on a search already
    # running, against reading a key on every pass of a loop that runs about
    # once a second.
    PROGRESS_PREFERENCE_TTL_SECONDS: float = float(
        os.environ.get("PROGRESS_PREFERENCE_TTL_SECONDS", "30")
    )
    # How long the stamp a search leaves on every pass of its loop outlives
    # that pass. Nothing clears the stamp when a search process is killed, so
    # it has to expire on its own; generous enough that a search backing off
    # from a run of failures still counts as having reported in.
    SEARCH_HEARTBEAT_TTL_SECONDS: int = int(os.environ.get("SEARCH_HEARTBEAT_TTL_SECONDS", "600"))
    # Legacy korail2 requests carried no timeout at all; these bound one poll.
    KORAIL_CONNECT_TIMEOUT: float = float(os.environ.get("KORAIL_CONNECT_TIMEOUT", "10"))
    KORAIL_READ_TIMEOUT: float = float(os.environ.get("KORAIL_READ_TIMEOUT", "30"))
    # A live worker that has not stamped a pass for this long is stuck, not searching.
    SEARCH_SILENT_AFTER_SECONDS: int = int(os.environ.get("SEARCH_SILENT_AFTER_SECONDS", "180"))

    # Favourite searches
    #
    # A cap rather than a quota: the list is a keyboard, and a keyboard with
    # forty rows on it is not a list anyone reads. Ten is more journeys than
    # anyone takes regularly, and hitting it is a prompt to tidy up.
    MAX_FAVOURITES: int = int(os.environ.get("MAX_FAVOURITES", "10"))

    # Waiting for something typed
    #
    # A few screens end with "the next thing you send is the answer" - a new
    # name for a favourite, a reporting interval that is not on the keyboard.
    # How long that stays true. One abandoned mid-thought must not swallow
    # whatever gets typed an hour later.
    PENDING_INPUT_TTL_SECONDS: int = int(os.environ.get("PENDING_INPUT_TTL_SECONDS", "300"))

    # Flask Configuration
    # Only background reservation processes call the HTTP endpoints, so the
    # default stays on loopback. An explicit override still wins.
    FLASK_HOST: str = os.environ.get("FLASK_HOST", "127.0.0.1")
    FLASK_PORT: int = int(os.environ.get("FLASK_PORT", "8080"))
    # Defaults to False: the Werkzeug debugger is a remote code execution
    # surface and must never be enabled on a reachable deployment.
    FLASK_DEBUG: bool = _env_bool("FLASK_DEBUG", False)

    # Application Callback URLs (internal, loopback only)
    CALLBACK_BASE_URL: str = f"http://127.0.0.1:{FLASK_PORT}"
    INTERNAL_CALLBACK_TOKEN: str = _internal_callback_token()

    # Logging Configuration
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Magic Admin Login String
    # Opt-in only: when unset, the shortcut that logs in with the operator's
    # own Korail account is disabled.
    ADMIN_MAGIC_STRING: str | None = os.environ.get("ADMIN_MAGIC_STRING") or None

    # Admin Command Authentication
    # Distinct from USERPW so that guessing it never yields a Korail password.
    # When unset, every admin command is refused.
    ADMIN_PASSWORD: str | None = os.environ.get("ADMIN_PASSWORD") or None
    ADMIN_MAX_AUTH_FAILURES: int = int(os.environ.get("ADMIN_MAX_AUTH_FAILURES", "5"))
    ADMIN_LOCKOUT_SECONDS: int = int(os.environ.get("ADMIN_LOCKOUT_SECONDS", "900"))
    ADMIN_SESSION_TTL_SECONDS: int = int(os.environ.get("ADMIN_SESSION_TTL_SECONDS", "3600"))

    # Credential Protection
    # Key material used to encrypt Korail credentials before they touch Redis.
    SESSION_SECRET: str | None = os.environ.get("SESSION_SECRET") or None
    # Sessions expire so that credentials never linger indefinitely.
    SESSION_TTL_SECONDS: int = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))

    # Restart Recovery
    #
    # A search runs in a child process, so a restart of the app leaves the
    # stored reservation without anything actually searching. Resuming means
    # logging in to Korail again, which means keeping the password for as long
    # as the search is alive - encrypted, in a key of its own, deleted the
    # moment the search ends. Turn this off to be told about the interruption
    # instead of recovering from it.
    RESUME_ON_RESTART: bool = _env_bool("RESUME_ON_RESTART", True)
    # Backstop only: the credentials are deleted when the search finishes.
    RESUME_TTL_SECONDS: int = int(os.environ.get("RESUME_TTL_SECONDS", "259200"))

    # ==================== Onboarding ====================

    # How long a registered Korail account is kept before the user has to
    # register again. Default 90 days.
    #
    # This is the one credential in the system that outlives the booking it
    # was entered for: registering once and reusing it is the whole point.
    # That convenience is paid for by the password sitting in Redis between
    # bookings rather than only during one, so it is encrypted, expires on
    # its own, and is deleted the moment the user logs out or blocks the bot.
    CREDENTIAL_TTL_SECONDS: int = int(os.environ.get("CREDENTIAL_TTL_SECONDS", "7776000"))

    # ==================== Who may use this bot ====================

    # Numbers that never need approving. The old name was ALLOW_LIST, back
    # when it was the only gate; it is still read so that existing .env files
    # keep working.
    #
    # This is one of several ways to be allowed, not the list of everyone who
    # is: people can also be approved from the chat, and everyone gets a few
    # searches before either applies.
    PREAPPROVED_USERS: list[str] = (
        (os.environ.get("PREAPPROVED_USERS") or os.environ.get("ALLOW_LIST") or "").split(",")
        if (os.environ.get("PREAPPROVED_USERS") or os.environ.get("ALLOW_LIST"))
        else []
    )
    # How many searches someone may run before they need approving.
    #
    # The point is that a stranger who finds the bot can try it, while the
    # server does not end up hammering Korail on behalf of everyone who ever
    # typed /start. 0 means approval is required from the first search;
    # a negative number means never require approval.
    TRIAL_SEARCH_LIMIT: int = int(os.environ.get("TRIAL_SEARCH_LIMIT", "3"))
    # How long an unanswered access request is kept. Default 30 days.
    REQUEST_TTL_SECONDS: int = int(os.environ.get("REQUEST_TTL_SECONDS", "2592000"))
    # Ceiling on searches running at once, across every user, per railway.
    #
    # Each search asks its railway for seats every few seconds. Korail and SR
    # have separate endpoints and rate limits, so one railway being full does
    # not consume the other's allowance. 0 disables the ceiling.
    MAX_CONCURRENT_SEARCHES: int = int(os.environ.get("MAX_CONCURRENT_SEARCHES", "5"))

    # ==================== Scheduled searches ====================

    # How far ahead a search may be booked. Bounded by RESUME_TTL_SECONDS
    # because the login it will need is stored under that expiry: schedule
    # past it and the moment arrives with no way to log in.
    SCHEDULE_MAX_AHEAD_SECONDS: int = int(
        os.environ.get("SCHEDULE_MAX_AHEAD_SECONDS", str(RESUME_TTL_SECONDS))
    )
    # How long past its start time a missed schedule is still worth running.
    # Covers the app being restarted across the moment; beyond it the user
    # would be getting a search they asked for hours ago without warning.
    SCHEDULE_GRACE_SECONDS: int = int(os.environ.get("SCHEDULE_GRACE_SECONDS", "600"))
    # Longest the scheduler sleeps between checks. It normally sleeps exactly
    # until the next start time, so this only bounds how quickly a newly
    # booked search is noticed.
    SCHEDULE_POLL_SECONDS: float = float(os.environ.get("SCHEDULE_POLL_SECONDS", "30"))

    # ==================== Watching the search processes ====================

    # How long a freshly spawned search is given to prove it is alive.
    #
    # A search that cannot start at all - a broken interpreter, a missing
    # dependency - dies within milliseconds of exec. Waiting this long before
    # reporting success costs nothing on the path that works and is the only
    # thing standing between the user and a "search started" message about a
    # process that is already gone.
    PROCESS_START_GRACE_SECONDS: float = float(os.environ.get("PROCESS_START_GRACE_SECONDS", "1.0"))
    # How often the running searches are checked for still existing. A search
    # that dies is not noticed until the next pass, so this is roughly how
    # long the user waits to be told.
    WATCHDOG_POLL_SECONDS: float = float(os.environ.get("WATCHDOG_POLL_SECONDS", "30"))
    # How long a dead search's details are kept so the user can resume it.
    # Bounded by RESUME_TTL_SECONDS: past that the stored login is gone and
    # resuming would have nothing to log in with.
    DEAD_SEARCH_TTL_SECONDS: int = int(
        os.environ.get("DEAD_SEARCH_TTL_SECONDS", str(RESUME_TTL_SECONDS))
    )

    # Identifies this run of the application. Reservations carry it, so
    # anything left over from an earlier run is recognisable as abandoned.
    RUN_ID: str = secrets.token_hex(8)

    # Process Management
    RECURSION_LIMIT: int = 10**7

    # Redis Configuration
    REDIS_HOST: str = os.environ.get("REDIS_HOST", "localhost")
    REDIS_PORT: int = int(os.environ.get("REDIS_PORT", "6379"))
    REDIS_DB: int = int(os.environ.get("REDIS_DB", "0"))
    REDIS_PASSWORD: str | None = os.environ.get("REDIS_PASSWORD")
    REDIS_DECODE_RESPONSES: bool = True
    REDIS_SOCKET_TIMEOUT: int = 5
    REDIS_SOCKET_CONNECT_TIMEOUT: int = 5
    REDIS_MAX_CONNECTIONS: int = 50

    @classmethod
    def validate(cls) -> None:
        """Validate required settings."""
        if not cls.TELEGRAM_BOT_TOKEN:
            raise ValueError("BOTTOKEN environment variable is required")

    @classmethod
    def warnings(cls) -> list[str]:
        """Return non-fatal configuration warnings to surface at startup."""
        messages = []

        if not cls.SESSION_SECRET:
            messages.append(
                "SESSION_SECRET is not set - credentials are encrypted with an "
                "ephemeral key, so stored sessions become unreadable after a "
                "restart. Generate one with 'scripts/setup.sh secrets'."
            )
            messages.append(
                "SESSION_SECRET is not set - phone numbers are keyed into trial "
                "counts and access requests with an unkeyed digest, which a "
                "lookup table reverses. Anyone who reads the database recovers "
                "the numbers it was meant to keep."
            )
            if cls.RESUME_ON_RESTART:
                messages.append(
                    "RESUME_ON_RESTART is enabled but SESSION_SECRET is not "
                    "set, so an interrupted search can never be resumed - the "
                    "stored credentials are unreadable after the restart that "
                    "would need them."
                )

        if not cls.ADMIN_PASSWORD:
            messages.append("ADMIN_PASSWORD is not set - all admin commands are disabled.")

        # Typed anywhere in any chat, and a correct guess is a standing grant
        # of the admin surface. Wrong guesses cannot be counted - every
        # ordinary message would look like one - so length is the defence.
        if cls.ADMIN_MAGIC_STRING and len(cls.ADMIN_MAGIC_STRING) < 16:
            messages.append(
                f"ADMIN_MAGIC_STRING is only {len(cls.ADMIN_MAGIC_STRING)} characters. "
                "It turns any chat that types it into a developer chat, and failed "
                "guesses cannot be rate limited, so use at least 16 characters of "
                "something nobody would send by accident."
            )

        if cls.has_preconfigured_korail_credentials():
            if cls.ADMIN_MAGIC_STRING:
                messages.append(
                    "USERID/USERPW are set - developer chats book with that Korail "
                    "account instead of registering one. Everyone else is unaffected."
                )
            else:
                messages.append(
                    "USERID/USERPW are set but ADMIN_MAGIC_STRING is not, so no chat "
                    "can become a developer chat and the account is never used. Set "
                    "ADMIN_MAGIC_STRING to use it, or unset USERID/USERPW."
                )

        if not cls.REDIS_PASSWORD:
            messages.append(
                "REDIS_PASSWORD is not set - make sure Redis is not reachable "
                "from outside the container network."
            )

        if cls.FLASK_DEBUG:
            messages.append(
                "FLASK_DEBUG is enabled - the Werkzeug debugger allows remote "
                "code execution. Never enable this on a reachable host."
            )

        if cls.MINI_APP_URL and not cls.mini_app_enabled():
            messages.append(
                "MINI_APP_URL is ignored because Telegram Mini Apps require an "
                "absolute HTTPS URL. The chat reservation flow remains available."
            )

        if cls.MINI_APP_API_ENABLED and not cls.TELEGRAM_BOT_TOKEN:
            messages.append(
                "MINI_APP_API_ENABLED is set with no BOTTOKEN. Mini App requests "
                "are signed with a key derived from the token, so every request "
                "will be refused until one is configured."
            )

        if cls.MINI_APP_API_ENABLED and cls.MINI_APP_API_PORT == cls.FLASK_PORT:
            messages.append(
                "MINI_APP_API_PORT equals FLASK_PORT. The public listener exists "
                "to keep the internal callbacks off the exposed socket; sharing a "
                "port defeats it."
            )

        if cls.mini_app_enabled() and not cls.MINI_APP_API_ENABLED:
            messages.append(
                "MINI_APP_URL is set but MINI_APP_API_ENABLED is not. The screen "
                "will open and every action in it will fail to reach this bot."
            )

        return messages

    @classmethod
    def mini_app_enabled(cls) -> bool:
        """Whether a usable public URL was configured for the Mini App."""
        if not cls.MINI_APP_URL:
            return False
        parsed = urlsplit(cls.MINI_APP_URL)
        return parsed.scheme == "https" and bool(parsed.netloc)

    @classmethod
    def has_preconfigured_korail_credentials(cls) -> bool:
        """
        Whether the server can log in without asking the user anything.

        USERID/USERPW are the operator's own Korail account. When both are
        present the phone number and password prompts have a known answer,
        so the conversation skips straight past them.
        """
        return bool(cls.KORAIL_ADMIN_USER_ID and cls.KORAIL_ADMIN_PASSWORD)

    @classmethod
    def preconfigured_credentials(cls) -> tuple[str | None, str | None]:
        """Return the fixed Korail credentials, if any were configured."""
        return cls.KORAIL_ADMIN_USER_ID, cls.KORAIL_ADMIN_PASSWORD

    @classmethod
    def has_preconfigured_credentials(cls) -> bool:
        """Whether both fixed-login fields are present."""
        username, password = cls.preconfigured_credentials()
        return bool(username and password)

    @classmethod
    def has_any_preconfigured_credentials(cls) -> bool:
        """Whether a server-side login is configured at all."""
        return cls.has_preconfigured_korail_credentials()

    @classmethod
    def is_preapproved(cls, phone_number: str) -> bool:
        """
        Whether this number was approved in advance, in the environment.

        One of several ways to be allowed, not the whole gate - people can
        also be approved from the chat, and everyone gets TRIAL_SEARCH_LIMIT
        searches before either matters. So an empty list means "nobody is
        pre-approved", not "nobody may use the bot".

        Compared digit by digit so that '010-1234-5678' and '01012345678'
        are the same number, whichever form the list happens to use.
        """
        allowed = {_digits(entry) for entry in cls.PREAPPROVED_USERS if _digits(entry)}
        return _digits(phone_number) in allowed


# Singleton instance
settings = Settings()
