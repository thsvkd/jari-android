"""What every rail operator's search loop has in common.

Korail and SR are two companies with two APIs, but the thing this bot does to
them is one thing: ask for seats on a train that is sold out, over and over,
slowly enough not to look like a machine, and reserve the moment one appears.
That loop - how long to wait, when to re-authenticate, how to tell "sold out"
from "not answering", what to tell the user while it waits - has nothing to do
with which company is on the other end.

This module holds that part. What is left for a subclass is small and entirely
operator-specific: how to log in, how to ask for trains, how to reserve one,
and how to check whether a reservation has been paid for.
"""

import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from korail_bot.config.settings import settings
from korail_bot.models import ReservationOutcome, SeatPreference, parse_seat_label
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SearchProgress:
    """
    What a running search can say about itself.

    Facts only. Whether any of it is worth telling the user, and how it should
    read when it is, belongs to whoever owns the conversation - this class has
    never known there is one.
    """

    #: How many times the loop has asked the operator for trains.
    attempts: int
    #: Seconds since the search loop began.
    elapsed_seconds: float
    #: Failed requests in a row right now. 0 means the operator is answering.
    failure_streak: int

    @property
    def healthy(self) -> bool:
        """Whether the operator is answering at the moment."""
        return self.failure_streak == 0


class DuplicateReservationError(Exception):
    """Raised when attempting to reserve a train that's already reserved."""

    pass


class SearchUnavailableError(Exception):
    """
    Raised when a search could not be carried out at all.

    Distinct from finding no trains, which is an ordinary answer and comes
    back as an empty list. This one means the operator refused, timed out, or
    replied with something the client could not read - a state the search
    cannot fix by asking again immediately, and that the user should hear
    about if it persists.
    """

    pass


class RailService(ABC):
    """The search-and-reserve loop, minus the operator."""

    #: What this operator is called when talking to the user.
    operator_name = "철도"

    #: How many unwanted seats a search will give back before it stops being
    #: fussy and keeps the next one it wins.
    #:
    #: Every rejection hands a seat to whoever is searching next, and the seat
    #: that replaces it may never come. Somewhere past here the preference has
    #: stopped being a preference and started being the reason the user is
    #: travelling tomorrow instead of today, so the search takes what it has
    #: and says so - a seat in the wrong row beats no seat, and being told
    #: which seat you got beats finding out at the platform.
    MAX_SEAT_REJECTIONS = 20

    def __init__(
        self,
        app_session_start: str | None = None,
        on_status: Callable[[str], None] | None = None,
        on_progress: Callable[["SearchProgress"], None] | None = None,
    ):
        """
        Initialize the service.

        Args:
            app_session_start: When this user's app session began, in epoch
                               milliseconds. A search that a restart
                               interrupted passes back the value it started
                               with so it stays one session; None lets the
                               client stamp the moment it was built. Only
                               operators that carry such a stamp use it.
            on_status: Called with a message for the user when the search
                       stops being able to reach the operator, and again when
                       it recovers. A callback rather than a TelegramService
                       so that this class keeps knowing nothing about
                       Telegram; None means nobody is told, which is right for
                       the short-lived clients that only log in and stop.
            on_progress: Called once per pass of the search loop with what the
                       search knows about itself. Deliberately unthrottled:
                       deciding how often the user hears anything is the
                       caller's business, and it is the only party that knows
                       what the user asked for.
        """
        self._logged_in = False
        self._search_interval = settings.KORAIL_SEARCH_INTERVAL
        self._search_jitter = settings.KORAIL_SEARCH_INTERVAL_JITTER
        self._app_session_start = app_session_start
        self._username: str | None = None
        self._password: str | None = None
        self._relogin_interval = settings.KORAIL_RELOGIN_INTERVAL
        self._relogin_jitter = settings.KORAIL_RELOGIN_INTERVAL_JITTER
        self._relogin_due_at: float = 0.0
        self._relogin_count: int = 0
        self._on_status = on_status
        self._on_progress = on_progress
        self._search_started_at: float = 0.0
        self._failure_streak: int = 0
        self._failure_since: float = 0.0
        self._failure_alerted_at: float = 0.0
        self._failure_threshold = settings.KORAIL_FAILURE_ALERT_THRESHOLD
        self._failure_realert = settings.KORAIL_FAILURE_REALERT_SECONDS
        self._failure_backoff_cap = settings.KORAIL_FAILURE_BACKOFF_CAP
        #: Seats given back for not being the ones asked for. Counted across
        #: the whole search rather than per pass, because the cap it is
        #: measured against is about the search as a whole.
        self._seat_rejections: int = 0
        #: Whether the user has already been told that seats cannot be checked
        #: on this railway. Said once; repeating it every pass would be noise
        #: on a loop that runs for hours.
        self._seat_check_warned: bool = False

    # ==================== What each operator must supply ====================

    @property
    @abstractmethod
    def default_train_type(self) -> Any:
        """The train type a search means when it does not say."""

    @property
    @abstractmethod
    def default_reserve_option(self) -> Any:
        """The seat preference a reservation means when it does not say."""

    @abstractmethod
    def login(self, username: str, password: str) -> bool:
        """Authenticate, returning whether it worked."""

    @abstractmethod
    def _relogin(self) -> bool:
        """Authenticate again with the stored credentials."""

    @abstractmethod
    def search_trains(
        self,
        dep_date: str,
        src_locate: str,
        dst_locate: str,
        dep_time: str = "000000",
        max_dep_time: str = "2400",
        train_type: Any = None,
        passenger_count: int = 1,
        verbose: bool = True,
        include_no_seats: bool = False,
        train_numbers: list[str] | None = None,
    ) -> list:
        """
        Ask the operator for trains.

        Returns an empty list when there are none to be had - an ordinary
        answer. Raises SearchUnavailableError when the question could not be
        put at all, which the loop must be able to tell apart.
        """

    @abstractmethod
    def reserve_train(
        self,
        train,
        option: Any = None,
        passenger_count: int = 1,
        seat_preference: SeatPreference | None = None,
    ) -> Any:
        """
        Try to reserve one train.

        Returns the reservation on success, None when the seat went before the
        request landed, and the string "DUPLICATE" when the operator refuses
        because this account already holds an equivalent booking.

        seat_preference is the column/row set the user will accept. Operators
        that can ask up front (Korail window/aisle codes) do so here; the
        rest ignore it and honour the same object after the fact in
        keeps_seat.
        """

    @abstractmethod
    def is_reservation_outstanding(self, rsv_id: str) -> bool | None:
        """
        Whether a reservation is still sitting unpaid.

        True while it is still unpaid, False once it is gone, and None when
        the operator could not be asked - which is not the same as gone, and
        must not be read as one.
        """

    def reservation_outcome(
        self,
        rsv_id: str,
        *,
        train_no: str = "",
        dep_date: str = "",
        dep_time: str = "",
    ) -> ReservationOutcome:
        """
        What became of a reservation: paid for, given up, or still waiting.

        Not abstract. The base answer is built out of
        `is_reservation_outstanding`, and it deliberately refuses to turn that
        method's False into PAID. False there means only "no longer on the
        unpaid list", and a reservation the user cancelled in the app leaves
        that list in exactly the same way a paid-for one does. Announcing a
        payment on the strength of a disappearance is the mistake this whole
        type exists to undo, so a disappearance the operator cannot explain is
        UNKNOWN, and UNKNOWN means the user is told nothing.

        An operator whose API can tell the two apart overrides this and says
        which it was.

        Args:
            rsv_id: The reservation number to ask about
            train_no: Which train the seat is on, where the operator needs it
                      to match a ticket against the reservation
            dep_date: That train's running date (YYYYMMDD), same purpose
            dep_time: That train's departure time (HHMMSS), same purpose

        Returns:
            OUTSTANDING while it is still unpaid, and UNKNOWN for everything
            else this base implementation cannot distinguish.
        """
        outstanding = self.is_reservation_outstanding(rsv_id)
        if outstanding is True:
            return ReservationOutcome.OUTSTANDING
        return ReservationOutcome.UNKNOWN

    def cancel_reservation(self, rsv_id: str) -> bool:
        """
        Give one unpaid reservation back.

        Not abstract: the base answer is "this operator's client cannot", and
        an operator that can says so by overriding. Reservations left alone
        are not lost either - unpaid, the railway reclaims the seat when the
        deadline passes - so the difference is how soon the seat goes back and
        whether the user is left holding a booking they never wanted.

        Args:
            rsv_id: The reservation number to give back

        Returns:
            True when the railway confirmed the cancellation. False for every
            other outcome, including "could not ask" - a caller must never
            read this as the seat having been given back.
        """
        logger.warning(f"{self.operator_name} cannot cancel {rsv_id} outright")
        return False

    def issued_tickets(self) -> list[dict] | None:
        """
        The tickets this account has already paid for and been issued.

        Read-only on purpose. Refunding an issued ticket costs a fee and
        cannot be undone, so the bot shows the ticket and leaves the button to
        the railway's own app - see PendingPaymentService.show_tickets.

        Not abstract: the base answer is "this operator's client cannot ask",
        and an operator that can says so by overriding.

        Returns:
            One dict per ticket, carrying only strings and booleans so the
            result can go straight into a message or into Redis. Keys:
            reservation_id (empty when the railway files no number against a
            ticket), train, dep_date, dep_time, seats, operator.

            None means the question could not be asked - not logged in, or the
            railway refused. That is a different answer from ``[]``, which
            means it was asked and there are no issued tickets, and the two
            must never be collapsed: telling someone they have no tickets when
            nobody looked is how a ticket gets missed.
        """
        logger.warning(f"{self.operator_name} cannot list issued tickets")
        return None

    # ==================== Reading a reservation ====================
    #
    # The two clients hand back objects that carry the same facts under
    # different names - korail2's rsv_id is SR's reservation_number, its
    # buy_limit_date/-time are SR's payment_date/-time. Everything downstream
    # of a successful reservation needs those two facts and nothing else about
    # the object, so the translation happens here rather than at each of the
    # half-dozen places that ask.

    @staticmethod
    @abstractmethod
    def reservation_id(reservation) -> str | None:
        """
        The number this operator files the reservation under.

        None when the object does not carry one, which leaves the caller
        unable to check on it later - worth saying so rather than inventing a
        value that will never match anything.
        """

    @staticmethod
    @abstractmethod
    def payment_due(reservation) -> tuple[str | None, str | None]:
        """
        When the operator stops holding this seat.

        Returns:
            (YYYYMMDD, HHMMSS), either of which is None when the reservation
            did not state it.
        """

    @staticmethod
    @abstractmethod
    def describe_train(train) -> dict:
        """
        Reduce a train to what the keyboard and the summary need.

        Only strings and booleans: this goes into the session, which is
        serialised to Redis, and a client's own object would not survive the
        trip.

        Returns:
            {"no": train number, "label": what to put on the button,
             "soldout": whether it has nothing left to book}
        """

    @staticmethod
    def _clock(value: str | None) -> str:
        """HHMMSS as a clock face. The seconds are always zero here."""
        return f"{value[:2]}:{value[2:4]}" if value and len(value) >= 4 else "??:??"

    # ==================== Pacing ====================

    def _schedule_next_relogin(self) -> None:
        """
        Decide when the session should be refreshed next.

        Drawn again after every login rather than kept as a fixed period: a
        search running for half a day would otherwise re-authenticate exactly
        on the half hour, every half hour. A base interval of 0 turns the
        refresh off, leaving the session to be renewed when the operator
        actually rejects it.
        """
        if self._relogin_interval <= 0:
            self._relogin_due_at = 0.0
            return

        delay = self._spread(self._relogin_interval, self._relogin_jitter)
        self._relogin_due_at = time.time() + delay
        logger.debug(f"🔄 Next session refresh in {delay:.0f}s")

    def _check_session_refresh(self):
        """Refresh the session before the operator gets around to expiring it."""
        if self._relogin_due_at and time.time() >= self._relogin_due_at:
            logger.debug("🔄 Session due for a refresh, re-logging in")
            self._relogin()

    def ensure_logged_in(self) -> None:
        """
        Get the session back before asking for trains again.

        A refresh that failed leaves the service logged out and otherwise
        unchanged, and nothing about that state stops the loop from going
        round again. What used to happen next was that search_trains found
        no session and raised ValueError - which the loop does not catch,
        because it means "this was called wrong", not "this attempt failed".
        It travelled all the way out to the process, where the handler for
        bad input told the user to check their station names. A session the
        operator dropped is not a typo in a station name, and the search
        ended on that advice.

        So the recovery happens here instead, in front of every search, and a
        failed one is reported as what it is: an attempt that did not come
        off. The loop then backs off and tries again - which is right,
        because a login that fails now is usually a login that works in a
        minute - and if it stays broken, the run of failures reaches the
        threshold and the user hears that the operator is not answering.

        Raises:
            SearchUnavailableError: When the session could not be recovered
        """
        if self._logged_in:
            return

        logger.warning(f"{self.operator_name} session is gone - logging in again before searching")
        if not self._relogin():
            raise SearchUnavailableError(
                f"{self.operator_name} session expired and could not be re-established"
            )

        logger.info(f"✅ {self.operator_name} session recovered, resuming the search")

    @staticmethod
    def _spread(seconds: float, ratio: float) -> float:
        """
        Draw a value from seconds * (1 +/- ratio).

        A fixed interval makes the search a metronome: every request lands the
        same number of seconds after the previous one, a pattern no person
        browsing the site would ever produce. Drawing each wait uniformly from
        a band around the configured value keeps the average rate but removes
        that signature. With ratio at 0 the value is returned unchanged.
        """
        if ratio <= 0:
            return seconds

        spread = seconds * ratio
        return max(0.0, random.uniform(seconds - spread, seconds + spread))

    def jittered(self, seconds: float) -> float:
        """Spread a wait between requests over the configured search jitter."""
        return self._spread(seconds, self._search_jitter)

    def next_interval(self, multiplier: float = 1.0) -> float:
        """
        Draw the next wait between requests.

        Args:
            multiplier: Scales the base interval (e.g. 1.5 for a longer wait)

        Returns:
            The number of seconds to wait
        """
        return self.jittered(self._search_interval * multiplier)

    def wait_between_requests(self, multiplier: float = 1.0) -> float:
        """Sleep for a randomised interval and return how long it waited."""
        delay = self.next_interval(multiplier)
        time.sleep(delay)
        return delay

    def wait_seconds(self, seconds: float) -> float:
        """Sleep for a randomised version of a fixed wait, in seconds."""
        delay = self.jittered(seconds)
        time.sleep(delay)
        return delay

    # ==================== Reachability tracking ====================
    #
    # The operator answering "no trains" and the operator not answering at all
    # both leave the loop with nothing to reserve. Told apart, the first is the
    # whole point of the search and the second is worth waking the user for;
    # run together, as they were, a blocked search looks exactly like a
    # sold-out one and reports itself as healthy forever.

    def _announce(self, message: str) -> None:
        """Pass a message to the user, if anyone is listening."""
        if not self._on_status:
            return
        try:
            self._on_status(message)
        except Exception as e:
            # Telling the user failed. That must not end the search.
            logger.error(f"Failed to deliver search status message: {e}", exc_info=True)

    def begin_search(self) -> None:
        """
        Mark the moment the search loop started.

        Read off a monotonic clock, so a report of "3시간째" cannot be turned
        into nonsense by the host's wall clock moving under it.
        """
        self._search_started_at = time.monotonic()

    def report_progress(self, attempts: int) -> None:
        """
        Hand the caller what the search knows about itself.

        Called once per pass of the loop and never throttled here. Reporting
        is not this class's decision: it does not know whether the user asked
        for reports, how often they want them, or whether anyone is reading.

        Args:
            attempts: How many times the loop has asked for trains
        """
        if not self._on_progress:
            return

        started = self._search_started_at or time.monotonic()
        try:
            self._on_progress(
                SearchProgress(
                    attempts=attempts,
                    elapsed_seconds=time.monotonic() - started,
                    failure_streak=self._failure_streak,
                )
            )
        except Exception as e:
            # Telling the user how it is going must never be the reason the
            # search stops going.
            logger.error(f"Failed to deliver search progress: {e}", exc_info=True)

    def note_search_failure(self, error: Exception) -> float:
        """
        Record a failed request and say how much to slow down.

        Args:
            error: What went wrong, for the log and the user-facing message

        Returns:
            A multiplier for the next wait between requests
        """
        self._failure_streak += 1
        now = time.time()
        if self._failure_streak == 1:
            self._failure_since = now

        logger.error(
            f"❌ {self.operator_name} request failed ({self._failure_streak} in a row): "
            f"{type(error).__name__}: {error}",
            exc_info=self._failure_streak == 1,
        )

        due = self._failure_streak >= self._failure_threshold and (
            self._failure_alerted_at == 0.0
            or now - self._failure_alerted_at >= self._failure_realert
        )
        if due:
            self._failure_alerted_at = now
            minutes = int((now - self._failure_since) // 60)
            self._announce(
                f"⚠️ {self.operator_name} 응답을 받지 못하고 있습니다\n\n"
                f"연속 {self._failure_streak}회 실패"
                f"{f' (약 {minutes}분째)' if minutes else ''}.\n"
                f"마지막 오류: {type(error).__name__}\n\n"
                f"검색은 계속하되 요청 간격을 늘려 재시도합니다.\n"
                f"{self.operator_name} 점검 중이거나 접속이 차단되었을 수 있습니다.\n\n"
                f"💡 중단하려면 /cancel 을 사용하세요."
            )

        # Doubling per failure, capped: at the default 1s interval this walks
        # 1s, 2s, 4s ... up to a minute between attempts. Shifting instead of
        # ** so a long outage cannot produce a float overflow.
        multiplier = float(1 << min(self._failure_streak - 1, 20))
        return min(multiplier, self._failure_backoff_cap)

    def note_search_success(self) -> None:
        """Record that the operator answered, and say so if it had stopped."""
        if self._failure_streak == 0:
            return

        recovered_from = self._failure_streak
        alerted = self._failure_alerted_at != 0.0
        self._failure_streak = 0
        self._failure_since = 0.0
        self._failure_alerted_at = 0.0

        logger.info(
            f"✅ {self.operator_name} is answering again after {recovered_from} failed request(s)"
        )
        # Only worth a message if the silence was reported in the first place.
        if alerted:
            self._announce(
                f"✅ {self.operator_name} 응답이 정상으로 돌아왔습니다\n\n"
                f"원래 간격으로 검색을 계속합니다."
            )

    # ==================== The loop ====================

    def search_and_reserve_loop(
        self,
        dep_date: str,
        src_locate: str,
        dst_locate: str,
        dep_time: str = "000000",
        max_dep_time: str = "2400",
        train_type: Any = None,
        reserve_option: Any = None,
        passenger_count: int = 1,
        seat_strategy: str = "consecutive",
        max_attempts: int | None = None,
        train_numbers: list[str] | None = None,
        seat_preference: SeatPreference | None = None,
    ):
        """
        Continuously search for trains and attempt reservation until successful.

        Args:
            dep_date: Departure date (YYYYMMDD)
            src_locate: Source station
            dst_locate: Destination station
            dep_time: Departure time (HHMMSS)
            max_dep_time: Maximum departure time (HHMM)
            train_type: Train type filter, in whatever the operator understands
            reserve_option: Seat preference, in whatever the operator understands
            passenger_count: Number of adult passengers
            seat_strategy: "consecutive" for seats together, "random" for separate seats
            max_attempts: Maximum attempts (None for infinite)
            train_numbers: Watch only these train numbers; empty or None
                           watches every train in the time window
            seat_preference: Which seats will do; None or empty takes any.
                           Honoured only where the railway reports the seat it
                           assigned before payment - see keeps_seat

        Returns:
            Reservation object(s) when successful, None if max_attempts reached
        """
        if not self._logged_in:
            raise ValueError("Must login before searching")

        if train_type is None:
            train_type = self.default_train_type
        if reserve_option is None:
            reserve_option = self.default_reserve_option

        logger.info(
            f"Starting reservation loop: {src_locate} -> {dst_locate} "
            f"on {dep_date} at {dep_time} for {passenger_count} passengers ({seat_strategy} seating)"
        )
        self.begin_search()

        if seat_strategy == "consecutive":
            return self._search_and_reserve_consecutive(
                dep_date,
                src_locate,
                dst_locate,
                dep_time,
                max_dep_time,
                train_type,
                reserve_option,
                passenger_count,
                max_attempts,
                train_numbers,
                seat_preference,
            )
        else:  # random
            return self._search_and_reserve_random(
                dep_date,
                src_locate,
                dst_locate,
                dep_time,
                max_dep_time,
                train_type,
                reserve_option,
                passenger_count,
                max_attempts,
                train_numbers,
                seat_preference,
            )

    def _search_and_reserve_consecutive(
        self,
        dep_date: str,
        src_locate: str,
        dst_locate: str,
        dep_time: str,
        max_dep_time: str,
        train_type: Any,
        reserve_option: Any,
        passenger_count: int,
        max_attempts: int | None,
        train_numbers: list[str] | None = None,
        seat_preference: SeatPreference | None = None,
    ):
        """Reserve seats consecutively (together)."""
        attempts = 0
        duplicate_notified = False

        logger.info(f"🔄 Starting consecutive seat search loop (passengers={passenger_count})")

        while True:
            attempts += 1
            if max_attempts and attempts > max_attempts:
                logger.warning(f"❌ Reached max attempts ({max_attempts}), stopping")
                return None

            if attempts % 1000 == 0:
                logger.info(
                    f"📊 Search attempt #{attempts} (still searching..., re-logins: {self._relogin_count})"
                )

            is_summary = attempts % 60 == 0

            if is_summary:
                logger.debug(f"━━━ Search attempt #{attempts} ━━━")

            self.report_progress(attempts)
            self._check_session_refresh()

            # Search for trains
            try:
                self.ensure_logged_in()
                trains = self.search_trains(
                    dep_date,
                    src_locate,
                    dst_locate,
                    dep_time,
                    max_dep_time,
                    train_type,
                    passenger_count,
                    verbose=is_summary,
                    train_numbers=train_numbers,
                )
            except SearchUnavailableError as e:
                self.wait_between_requests(self.note_search_failure(e))
                continue

            self.note_search_success()

            if not trains:
                if is_summary:
                    logger.debug(f"📊 Attempt #{attempts}: no trains found, retrying...")
                self.wait_between_requests()
                continue

            # Try to reserve each train found (trains found = rare, always log)
            for idx, train in enumerate(trains, 1):
                logger.debug(f"🚂 Trying train {idx}/{len(trains)}")
                reservation = self.reserve_train(
                    train,
                    option=reserve_option,
                    passenger_count=passenger_count,
                    seat_preference=seat_preference,
                )

                if reservation == "DUPLICATE":
                    # Duplicate reservation detected
                    if not duplicate_notified:
                        # First time - raise exception to notify user once
                        duplicate_notified = True
                        logger.warning("⚠️ First duplicate detection - notifying user")
                        raise DuplicateReservationError("동일한 예약 내역이 존재합니다")
                    else:
                        # Already notified - just log and continue
                        logger.debug("Duplicate reservation still exists, continuing search...")
                elif reservation:
                    # A seat is not a result until it is one of the seats the
                    # user asked for. keeps_seat gives back the ones that are
                    # not, so a False here leaves nothing held and the loop
                    # carries on as if the train had been sold out.
                    if not self.keeps_seat(reservation, seat_preference):
                        continue
                    logger.info(f"🎉 CONSECUTIVE RESERVATION SUCCESS after {attempts} attempts!")
                    return reservation
                else:
                    logger.debug(f"Train {idx} failed (sold out or unavailable)")

            logger.debug(f"All {len(trains)} trains sold out in attempt #{attempts}")

            # Wait before next search
            self.wait_between_requests()

    def _search_and_reserve_random(
        self,
        dep_date: str,
        src_locate: str,
        dst_locate: str,
        dep_time: str,
        max_dep_time: str,
        train_type: Any,
        reserve_option: Any,
        passenger_count: int,
        max_attempts: int | None,
        train_numbers: list[str] | None = None,
        seat_preference: SeatPreference | None = None,
    ):
        """Reserve seats randomly (one at a time until target count reached)."""
        attempts = 0
        # Whatever the operator's client hands back for a booking; the two
        # have no type in common, and nothing here reads one except through
        # reservation_id and payment_due.
        reservations: list[Any] = []
        target_count = passenger_count
        duplicate_notified = False

        logger.info(f"Random seating: will reserve {target_count} individual tickets")

        while len(reservations) < target_count:
            attempts += 1
            if max_attempts and attempts > max_attempts:
                logger.warning(f"Reached max attempts ({max_attempts}), stopping")
                # Cancel any partial reservations
                self._cancel_reservations(reservations)
                return None

            is_summary = attempts % 60 == 0

            self.report_progress(attempts)
            self._check_session_refresh()

            # Search for trains (search for single passenger each time)
            try:
                self.ensure_logged_in()
                trains = self.search_trains(
                    dep_date,
                    src_locate,
                    dst_locate,
                    dep_time,
                    max_dep_time,
                    train_type,
                    passenger_count=1,
                    verbose=is_summary,
                    train_numbers=train_numbers,
                )
            except SearchUnavailableError as e:
                self.wait_between_requests(self.note_search_failure(e))
                continue

            self.note_search_success()

            if not trains:
                if is_summary:
                    logger.debug(f"📊 Attempt #{attempts}: no trains found, retrying...")
                self.wait_between_requests()
                continue

            # Try to reserve each train found (trains found = rare, always log)
            for train in trains:
                logger.debug(
                    f"Found train: {train}, attempting reservation "
                    f"({len(reservations) + 1}/{target_count})..."
                )

                # Reserve one seat at a time
                reservation = self.reserve_train(
                    train,
                    option=reserve_option,
                    passenger_count=1,
                    seat_preference=seat_preference,
                )

                if reservation == "DUPLICATE":
                    # Duplicate reservation detected
                    if not duplicate_notified:
                        # First time - raise exception to notify user once
                        duplicate_notified = True
                        logger.warning("First duplicate detection - notifying user")
                        raise DuplicateReservationError("동일한 예약 내역이 존재합니다")
                    else:
                        # Already notified - just log and continue
                        logger.debug("Duplicate reservation still exists, continuing search...")
                elif reservation:
                    # Each ticket here is booked on its own, so an unwanted
                    # seat can be given back without touching the ones already
                    # secured - unlike the consecutive case, where the whole
                    # booking stands or falls together.
                    if not self.keeps_seat(reservation, seat_preference):
                        continue

                    reservations.append(reservation)
                    current_count = len(reservations)
                    logger.info(
                        f"Reserved seat {current_count}/{target_count} (attempt #{attempts})"
                    )
                    logger.debug(f"Reservation details: {reservation}")

                    # Check if we've reached target
                    if current_count >= target_count:
                        logger.info(
                            f"All {target_count} seats reserved successfully! "
                            f"Total attempts: {attempts}"
                        )
                        # Return the first reservation as primary (for compatibility)
                        # Store all reservations in a custom attribute for later access
                        first_reservation = reservations[0]
                        first_reservation._all_reservations = reservations
                        first_reservation._is_random_allocation = True
                        first_reservation._total_seats = target_count
                        return first_reservation

                    # Add delay between individual reservations to avoid rate limit
                    # Use longer interval for safety
                    self.wait_between_requests(1.5)
                    break  # Found a train and reserved, restart search loop

                else:
                    logger.debug("Reservation failed, continuing search...")

            # Wait before next search attempt
            self.wait_between_requests()

        return reservations[0] if reservations else None

    # ==================== Keeping only the seats that were asked for ========
    #
    # A search can be told which seats will do - a set of column letters, a
    # range of rows. Neither railway lets a booking request a particular seat,
    # so the only way to honour that is after the fact: take whatever seat
    # comes, look at it, and give it back if it is not one of them.
    #
    # That only works where the railway says which seat it gave before the
    # ticket is paid for. SR fills the label on the reservation. Korail's
    # unpaid listing does not, so KorailService asks a second, PNR-keyed
    # endpoint and hangs the labels on the object before keeps_seat runs.

    @staticmethod
    def assigned_seats(reservation) -> list[str]:
        """
        The seats a booking was given, labelled as the railway labels them.

        Empty means the railway did not say - which is not the same as a
        booking with no seats, and callers must not read it as one. The
        operator that can read unpaid seats overrides this; the base answer
        is "cannot tell".
        """
        return []

    def keeps_seat(self, reservation, preference: SeatPreference | None) -> bool:
        """
        Decide whether a booking just made is one to hold on to.

        Returns True when the booking is kept, which covers every case where
        there is nothing to object to: no preference was set, the railway does
        not report seats, the seats match, or the search has been fussy for
        long enough. Returns False only after the booking has already been
        given back, so a caller that gets False has nothing to clean up and
        can simply carry on searching.

        Args:
            reservation: The booking that was just made
            preference: Which seats the user will accept; None or empty means
                        any of them

        Returns:
            True to keep the booking, False once it has been released
        """
        if preference is None or preference.is_empty():
            return True

        seats = self.assigned_seats(reservation)
        if not seats:
            self._warn_seats_unchecked(
                f"⚠️ {self.operator_name}은(는) 결제 전에 좌석 번호를 알려주지 않아 "
                "좌석 조건을 확인할 수 없습니다. 조건은 무시하고 검색을 계속합니다."
            )
            return True

        if any(parse_seat_label(seat) is None for seat in seats):
            self._warn_seats_unchecked(
                f"⚠️ 좌석 번호를 읽지 못했습니다 ({', '.join(seats)}). "
                "좌석 조건은 무시하고 검색을 계속합니다."
            )
            return True

        # Every seat has to do. On a multi-seat booking the ones that do not
        # match are seats somebody in the party has to sit in, and there is no
        # way to give back only those.
        if all(preference.matches(seat) for seat in seats):
            return True

        self._seat_rejections += 1
        got = ", ".join(seats)

        if self._seat_rejections > self.MAX_SEAT_REJECTIONS:
            self._announce(
                f"🪑 원하시는 좌석({preference.describe()})을 "
                f"{self.MAX_SEAT_REJECTIONS}번 놓쳐서 이번 좌석({got})으로 확정했습니다."
            )
            logger.info(f"Seat preference given up after {self._seat_rejections} rejections")
            return True

        rsv_id = self.reservation_id(reservation)
        if not rsv_id or not self.cancel_reservation(rsv_id):
            # The seat could not be handed back. Keeping it is the lesser
            # wrong: the alternative is walking away from a booking that
            # exists, which the user would never hear about and would still be
            # on the hook for.
            logger.warning(f"Could not release unwanted seat(s) {got}; keeping the booking")
            self._announce(
                f"🪑 조건에 맞지 않는 좌석({got})이지만 반납에 실패해 그대로 두었습니다."
            )
            return True

        logger.info(
            f"Released seat(s) {got} - not in {preference.describe()} "
            f"({self._seat_rejections}/{self.MAX_SEAT_REJECTIONS})"
        )
        return False

    def _warn_seats_unchecked(self, message: str) -> None:
        """Say once that the seat condition cannot be honoured."""
        if self._seat_check_warned:
            return
        self._seat_check_warned = True
        logger.warning(message)
        self._announce(message)

    def _cancel_reservations(self, reservations: list) -> None:
        """
        Release the seats a random-seating run took but could not finish with.

        The default gives them up in the only way that always works: it stops
        paying for them, and the operator reclaims the seats when the deadline
        passes. An operator whose client can cancel outright should override
        this and do so - the seats go back sooner, and the user is not left
        with bookings they never completed.
        """
        if not reservations:
            return

        logger.warning(f"Leaving {len(reservations)} partial reservation(s) to expire unpaid")
        for reservation in reservations:
            # Reached while giving up on a search that has already gone wrong.
            # Describing a reservation must not be the thing that raises.
            try:
                logger.warning(f"  giving up: {reservation}")
            except Exception as e:
                logger.error(f"Failed to describe an abandoned reservation: {e}")

    @property
    def is_logged_in(self) -> bool:
        """Check if currently logged in."""
        return self._logged_in
