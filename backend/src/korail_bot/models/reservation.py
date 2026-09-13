"""Reservation data models."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any

# How a seat label reads once the railway has assigned one: a row number and a
# column letter, in that order. Matched case-insensitively and with the
# leading zeros a railway may or may not pad with, because "3A", "03A" and
# "3a" are the same seat and only one of them is worth depending on.
_SEAT_LABEL = re.compile(r"^0*(\d{1,3})\s*([A-Z])$")

#: The column letters a seat can carry. KTX general cars seat four
#: across as A B | C D; special cars seat three as A B | C. Naming all four
#: here and letting the user pick is deliberate - a car with no D simply never
#: produces a seat that matches one, which is the honest outcome.
SEAT_COLUMNS = ("A", "B", "C", "D")


@dataclass(frozen=True)
class SeatPreference:
    """
    Which seats the user will accept, as a set of candidates.

    Not a single seat. Cancellation tickets appear one at a time and are gone
    in seconds, so holding out for exactly 5호차 3A is holding out for
    something that will almost never come. A column set and a row range say
    "any of these will do", which is both what people actually want - a window
    seat, near the front - and what a search can realistically fill.

    Empty means no preference, which is what every search did before this
    existed and what every search still does unless the user says otherwise.
    """

    #: Column letters that will do. Empty means any column.
    columns: tuple[str, ...] = ()
    #: Lowest and highest acceptable row. None on either side leaves that end
    #: open, so a preference can say "row 5 or later" without inventing a last
    #: row that differs per train.
    row_min: int | None = None
    row_max: int | None = None

    def is_empty(self) -> bool:
        """Whether this asks for anything at all."""
        return not self.columns and self.row_min is None and self.row_max is None

    def matches(self, seat: str | None) -> bool:
        """
        Whether an assigned seat is one of the ones asked for.

        An empty preference matches everything. So does a seat label this
        cannot read: the caller acts on a False by giving the seat back, and
        giving back a seat because the label was in an unexpected shape would
        turn a parsing surprise into a search that cancels every seat it wins.
        Unreadable labels are reported by `parse_seat_label` returning None,
        and the caller is expected to say so rather than silently cancel.
        """
        if self.is_empty():
            return True

        parsed = parse_seat_label(seat)
        if parsed is None:
            return True

        row, column = parsed

        if self.columns and column not in self.columns:
            return False
        if self.row_min is not None and row < self.row_min:
            return False
        return not (self.row_max is not None and row > self.row_max)

    def describe(self) -> str:
        """How the preference reads in a summary, in Korean."""
        if self.is_empty():
            return "지정 없음"

        parts = []
        if self.columns:
            parts.append(f"{'·'.join(self.columns)}열")
        if self.row_min is not None and self.row_min == self.row_max:
            # One row, not a range. "맨 앞줄" is exactly this - and reading it
            # back as "1~1번" makes a deliberate choice look like a mistake.
            parts.append(f"{self.row_min}번")
        elif self.row_min is not None and self.row_max is not None:
            parts.append(f"{self.row_min}~{self.row_max}번")
        elif self.row_min is not None:
            parts.append(f"{self.row_min}번 이상")
        elif self.row_max is not None:
            parts.append(f"{self.row_max}번 이하")
        return " ".join(parts)

    def encode(self) -> str:
        """
        Flatten to the one string form argv and storage both carry.

        "A,D:1-15", "A,D:", ":1-15", and "" for no preference. One
        representation rather than three fields spread across a command line,
        a Redis hash and a JSON payload, so there is a single place where the
        shape can be got wrong.
        """
        if self.is_empty():
            return ""
        rows = ""
        if self.row_min is not None or self.row_max is not None:
            rows = f"{self.row_min or ''}-{self.row_max or ''}"
        return f"{','.join(self.columns)}:{rows}"

    @classmethod
    def decode(cls, text: str | None) -> "SeatPreference":
        """
        Read back what `encode` wrote.

        Anything unreadable becomes no preference. This is reached with argv
        from a build that predates the field and with records written before
        it existed, and both of those mean the same thing: nobody asked for a
        particular seat.
        """
        if not text or ":" not in text:
            return cls()

        column_part, _, row_part = text.partition(":")
        columns = tuple(
            letter
            for letter in (piece.strip().upper() for piece in column_part.split(","))
            if letter in SEAT_COLUMNS
        )

        row_min, row_max = None, None
        if "-" in row_part:
            low, _, high = row_part.partition("-")
            row_min = int(low) if low.strip().isdigit() else None
            row_max = int(high) if high.strip().isdigit() else None

        return cls(columns=columns, row_min=row_min, row_max=row_max)


def parse_seat_label(seat: str | None) -> tuple[int, str] | None:
    """
    Split a railway's seat label into its row and column.

    Returns None when the label is not in the shape this understands, which
    the caller must treat as "cannot tell" rather than "does not match" - see
    SeatPreference.matches.
    """
    if not seat:
        return None
    match = _SEAT_LABEL.match(str(seat).strip().upper())
    if not match:
        return None
    return int(match.group(1)), match.group(2)


@dataclass
class TrainSearchParams:
    """Parameters for searching trains."""

    dep_date: str  # Format: YYYYMMDD
    src_locate: str  # Station name (without '역')
    dst_locate: str  # Station name (without '역')
    dep_time: str  # Format: HHMMSS
    max_dep_time: str = "2400"  # Format: HHMM
    train_type: str = "TrainType.KTX"  # korail2.TrainType enum as string
    train_type_display: str = "KTX"
    special_option: str = "ReserveOption.GENERAL_FIRST"  # korail2.ReserveOption enum as string
    special_option_display: str = "GENERAL_FIRST"
    passenger_count: int = 1  # Number of adult passengers (1-9)
    seat_strategy: str = "consecutive"  # "consecutive" or "random"
    # Korail train numbers to watch. Empty means every train in the time
    # window, which is what the bot did before trains could be picked and is
    # still the better odds - a narrower watch is a deliberate choice to wait
    # for one particular train rather than take the first seat going.
    train_numbers: list[str] = field(default_factory=list)
    # Which seats will do, in the flattened form SeatPreference.encode writes.
    # Empty means any seat, which is what every search did before seats could
    # be asked for. Held encoded rather than as the object so that this stays
    # a plain dataclass of strings and numbers - the same thing argv carries
    # and the same thing Redis stores.
    seat_preference: str = ""
    # Structured physical-seat candidates for the mobile cancellation wait.
    # Empty keeps every search written before the seat-map flow compatible.
    seat_plan_json: str = ""

    def watches_specific_trains(self) -> bool:
        """Whether the search is narrowed to a chosen set of trains."""
        return bool(self.train_numbers)

    @property
    def seats_wanted(self) -> SeatPreference:
        """The seats this search will accept, however they were stored."""
        return SeatPreference.decode(self.seat_preference)

    def wants_specific_seats(self) -> bool:
        """Whether the search is narrowed to a chosen set of seats."""
        return not self.seats_wanted.is_empty()

    def validate(self) -> tuple[bool, str | None]:
        """
        Validate search parameters.

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Validate date format
        if not self.dep_date.isdigit() or len(self.dep_date) != 8:
            return False, "날짜 형식이 올바르지 않습니다 (YYYYMMDD)"

        # Validate date is not in the past
        from korail_bot.utils.timezone import RAIL_TIMEZONE

        today = datetime.now(RAIL_TIMEZONE).strftime("%Y%m%d")
        if self.dep_date < today:
            return False, "과거 날짜는 선택할 수 없습니다"

        # Validate time format
        if not self.dep_time[:4].isdigit() or len(self.dep_time) != 6:
            return False, "시간 형식이 올바르지 않습니다 (HHMMSS)"

        # The stations are not checked here. Korail's list is fetched and
        # cached rather than published with the client, and it lives behind
        # InputValidator - which is where a station is checked instead.

        return True, None


@dataclass
class ScheduledSearch:
    """
    A search that has been set up but is not to begin yet.

    Everything a search needs, held until its start time comes round. The
    reason to want one is that tickets are not released evenly: holiday
    booking opens at an announced minute, and cancellations cluster at the
    hours around a departure. A search that starts at the right moment beats
    one that has been grinding away since yesterday.
    """

    chat_id: int
    korail_id: str
    search_params: "TrainSearchParams"
    start_at: datetime
    timezone_name: str = "Asia/Seoul"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Turn a user's wall-clock selection into one UTC instant."""
        from korail_bot.utils.timezone import user_local_to_utc

        self.start_at = user_local_to_utc(self.start_at, self.timezone_name)

    def is_due(self, now: datetime | None = None) -> bool:
        """Whether the moment has arrived."""
        from korail_bot.utils.timezone import as_utc, utc_now

        return as_utc(now or utc_now()) >= as_utc(self.start_at)

    def seconds_until_due(self, now: datetime | None = None) -> float:
        """How long until it starts; zero once it is due."""
        from korail_bot.utils.timezone import as_utc, utc_now

        return max(0.0, (as_utc(self.start_at) - as_utc(now or utc_now())).total_seconds())


@dataclass
class RunningReservation:
    """Information about a running reservation process."""

    chat_id: int
    process_id: int
    korail_id: str
    search_params: TrainSearchParams
    # Identifies the application run that spawned the search process. A record
    # carrying anything else was left behind by a run that is already gone,
    # which means nothing is searching for it any more.
    run_id: str = ""
    # default_factory, not datetime.now(): a plain default is evaluated once,
    # when the class is defined, so every record would carry the time the
    # process started rather than the time the search did.
    #
    # Wrapped in a lambda rather than passed as datetime.now, so that the name
    # is resolved when a record is built. Handing over the bound method here
    # would capture the real one at import, before anything that patches the
    # clock - freezegun in the tests - has replaced it.
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def is_stale(self, current_run_id: str) -> bool:
        """Check whether this record outlived the process that owned it."""
        return self.run_id != current_run_id


class DeathCause(StrEnum):
    """Why a search stopped without saying so."""

    # Never got going: the process was spawned and was gone moments later.
    START_FAILED = "start_failed"
    # Ran for a while and then vanished, without the callback that a search
    # ending normally always sends.
    CRASHED = "crashed"


@dataclass
class DeadSearch:
    """
    A search that stopped without finishing, kept so it can be picked back up.

    A search ending normally - a seat booked, or the attempt given up on -
    calls back to the app, which is what clears its record away. Nothing calls
    back when the process simply dies, and the difference matters to the user:
    they are waiting on a search that no longer exists, and the tickets they
    were waiting for are still out there. So the details are moved here, where
    they are no longer mistaken for a running search but are still everything
    needed to start the same search again.
    """

    chat_id: int
    korail_id: str
    search_params: TrainSearchParams
    cause: DeathCause
    # Whether the login kept for restarts was still there when the death was
    # noticed. Without it a search cannot be resumed, and the user is better
    # told that up front than offered a button that fails.
    resumable: bool = True
    died_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class PaymentStatus:
    """Payment completion status for a reservation."""

    chat_id: int
    completed: bool = False
    # When the payment window opened. Named to match
    # MultiReservationStatus.created_at, which holds the same thing for the
    # multi-seat case; it was 'reservation_time' here alone.
    #
    # Stamped on creation so the field is never meaningless. Still optional,
    # because a record written before the field existed deserializes without
    # one and there is no way to invent the time it should have had.
    created_at: datetime | None = field(default_factory=lambda: datetime.now(UTC))
    reminder_active: bool = False

    # What is actually waiting to be paid for.
    #
    # The record used to say only that a window was open, which was all the
    # reminder loop needed. /status has to name the booking, and cancelling it
    # needs its number - so the search process, which is the only thing
    # holding the reservation, writes them here once the seat is secured.
    # Absent on a record written before this existed, and on one whose
    # reservation carried no number.
    reservation_id: str | None = None
    train_info: str = ""
    expires_at: datetime | None = None
    # Given back on purpose, as opposed to paid for or left to expire. Kept
    # apart from `completed`, which the reminder loop reads as "stop asking":
    # both are true here, and only one of them is what happened.
    cancelled: bool = False

    # Which train this seat is on, for matching against the issued-ticket
    # list. Korail's tickets carry no reservation number, so the only way to
    # tell a paid-for reservation from a cancelled one is to look for a ticket
    # on the same train, on the same day, at the same time.
    #
    # Empty by default on purpose: a record written before these existed has
    # nothing to match on, and the Korail check answers UNKNOWN for it - which
    # means the user hears nothing rather than hearing a guess.
    train_no: str = ""
    dep_date: str = ""
    dep_time: str = ""
    seat_labels: list[str] = field(default_factory=list)
    seat_class: str = ""

    def is_awaiting_payment(self) -> bool:
        """Whether there is still a seat here for the user to pay for."""
        from korail_bot.utils.timezone import utc_now

        if self.completed or self.cancelled:
            return False
        if not self.expires_at:
            return True
        now = utc_now() if self.expires_at.tzinfo else datetime.now()
        return now < self.expires_at


class ReservationPaymentStatus(Enum):
    """Payment status for individual reservations."""

    PENDING = "pending"  # Reservation made, awaiting payment
    PAID = "paid"  # Payment completed by user
    EXPIRED = "expired"  # Reservation expired due to timeout
    CANCELLED = "cancelled"  # Manually cancelled by user


class ReservationOutcome(Enum):
    """
    What took the reservation off the unpaid list.

    The old check answered one question - is it still listed? - and read a
    "no" as a payment. It is not: a reservation the user cancelled by hand
    disappears exactly the same way, and someone who cancelled was told the
    payment had gone through. These four keep the two apart, and keep
    "could not ask" apart from both.
    """

    OUTSTANDING = "outstanding"  # Still sitting there, waiting to be paid for
    PAID = "paid"  # A ticket for it was found on the issued list
    RELEASED = "released"  # Gone without a ticket - cancelled, or left to expire
    UNKNOWN = "unknown"  # Could not be asked. Say nothing.


@dataclass
class SingleReservationInfo:
    """Information about a single train reservation."""

    reservation_id: str  # Unique ID from korail2
    reservation_obj: Any  # Original reservation object from korail2
    reserved_at: datetime  # When reservation was created
    expires_at: datetime  # When reservation will expire
    status: ReservationPaymentStatus  # Current payment status
    seat_number: int  # Seat number in the group (1, 2, 3...)
    train_info: str  # Human-readable train info for display
    # Which train this seat is on, for matching against Korail's issued-ticket
    # list - the same fields, and the same reason, as PaymentStatus carries.
    # Empty on records written before they existed, which reads as UNKNOWN.
    train_no: str = ""
    dep_date: str = ""
    dep_time: str = ""
    seat_labels: list[str] = field(default_factory=list)
    seat_class: str = ""
    seat_keys: list[tuple[int, str]] = field(default_factory=list)

    def get_remaining_seconds(self) -> int:
        """Get remaining seconds until expiration."""
        if self.status != ReservationPaymentStatus.PENDING:
            return 0

        from korail_bot.utils.timezone import utc_now

        expires_at = self.expires_at
        now = utc_now() if expires_at.tzinfo else datetime.now()
        if now >= expires_at:
            return 0

        return int((expires_at - now).total_seconds())

    def get_remaining_minutes_display(self) -> str:
        """Get human-readable remaining time (e.g., '8분 30초')."""
        remaining = self.get_remaining_seconds()
        if remaining <= 0:
            return "만료됨"

        minutes = remaining // 60
        seconds = remaining % 60
        return f"{minutes}분 {seconds}초"

    def is_expired(self) -> bool:
        """Check if reservation has expired."""
        from korail_bot.utils.timezone import utc_now

        now = utc_now() if self.expires_at.tzinfo else datetime.now()
        return now >= self.expires_at


@dataclass
class MultiReservationStatus:
    """Status tracking for multiple reservations in random seat allocation."""

    chat_id: int
    reservations: list[SingleReservationInfo]
    total_seats: int
    seat_strategy: str  # "random" or "consecutive"
    created_at: datetime
    manually_stopped: bool = False  # True if user manually stopped reminders

    def get_pending_count(self) -> int:
        """Count how many reservations are still pending payment."""
        return sum(
            1
            for r in self.reservations
            if r.status == ReservationPaymentStatus.PENDING and not r.is_expired()
        )

    def get_paid_count(self) -> int:
        """Count how many reservations have been paid."""
        return sum(1 for r in self.reservations if r.status == ReservationPaymentStatus.PAID)

    def get_expired_count(self) -> int:
        """Count how many reservations have expired."""
        return sum(
            1
            for r in self.reservations
            if r.status == ReservationPaymentStatus.EXPIRED or r.is_expired()
        )

    def should_show_reminder(self) -> bool:
        """Determine if reminder should be shown."""
        # Don't show if manually stopped
        if self.manually_stopped:
            return False

        # Show only if there are pending reservations that haven't expired
        return self.get_pending_count() > 0

    def get_most_urgent_reservation(self) -> SingleReservationInfo | None:
        """Get the reservation with least time remaining (most urgent)."""
        pending = [
            r
            for r in self.reservations
            if r.status == ReservationPaymentStatus.PENDING and not r.is_expired()
        ]

        if not pending:
            return None

        # Return the one with earliest expiration time
        return min(pending, key=lambda r: r.expires_at)

    def mark_all_expired(self) -> None:
        """Mark all pending reservations as expired."""
        for reservation in self.reservations:
            if reservation.status == ReservationPaymentStatus.PENDING:
                reservation.status = ReservationPaymentStatus.EXPIRED

    def mark_reservation_paid(self, seat_number: int) -> bool:
        """Mark a specific reservation as paid."""
        for reservation in self.reservations:
            if reservation.seat_number == seat_number:
                reservation.status = ReservationPaymentStatus.PAID
                return True
        return False
