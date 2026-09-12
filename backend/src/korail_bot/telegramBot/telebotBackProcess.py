"""
Background process for train reservation.

This module is executed as a subprocess to continuously search for
and attempt to reserve trains.

Search parameters arrive on argv; Korail credentials arrive as a single
JSON line on stdin, because argv is readable by every process on the host.
"""

import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta

import requests
from korail2 import ReserveOption, TrainType

from korail_bot.config.settings import settings
from korail_bot.models import (
    MultiReservationStatus,
    PaymentStatus,
    ReservationOutcome,
    ReservationPaymentStatus,
    SeatPreference,
    SingleReservationInfo,
)
from korail_bot.services import (
    KorailService,
    MultiReservationReminderService,
    PaymentReminderService,
    TelegramService,
)
from korail_bot.services.rail_service import (
    DuplicateReservationError,
    SearchUnavailableError,
)
from korail_bot.storage.redis import RedisStorage
from korail_bot.telegramBot.messages import Messages
from korail_bot.utils.formatting import format_duration
from korail_bot.utils.logger import LoggerFactory, get_logger
from korail_bot.utils.privacy import mask_phone
from korail_bot.utils.timezone import (
    RAIL_TIMEZONE,
    as_utc,
    format_railway_journey,
    utc_now,
)

logger = get_logger(__name__)

# Set recursion limit
sys.setrecursionlimit(settings.RECURSION_LIMIT)


class SearchStopped(SystemExit):
    """
    Raised inside the search process when it is asked to stop.

    Deriving from SystemExit keeps it a BaseException, so it travels through
    the `except Exception` blocks of the search loop untouched and never
    turns into an error report to the user: whoever sent the signal already
    knows the search is over. The exit status stays 0 - being told to stop is
    not a failure.
    """

    def __init__(self, signum: int):
        super().__init__(0)
        self.signal_name = signal.Signals(signum).name


def install_shutdown_handlers() -> None:
    """
    Turn a stop signal into an orderly exit.

    A search spends nearly all of its life asleep between requests, so a stop
    signal almost always lands somewhere in the middle of the loop. Without a
    handler that is either an instant death with nothing in the log (SIGTERM,
    which is how /cancel and the app shutting down stop a search) or a
    traceback on the terminal that reads like a crash (SIGINT).

    The handler does no work of its own beyond raising; the signal it fired on
    travels with the exception and is logged once the stack has unwound to
    somewhere the process is not halfway through something else.
    """

    def stop(signum, _frame):
        raise SearchStopped(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)


class BackgroundReservationProcess:
    """Background process for train reservation."""

    def __init__(self):
        """Initialize from command line arguments and credentials on stdin."""
        if len(sys.argv) < 9:
            logger.error("Insufficient arguments")
            sys.exit(1)

        self.username, self.password = self._read_credentials()
        self.dep_date = sys.argv[1]
        self.src_locate = sys.argv[2]
        self.dst_locate = sys.argv[3]
        self.dep_time = sys.argv[4]
        self.train_type_str = sys.argv[5]
        self.special_info_str = sys.argv[6]
        # int, not the raw argv string: every storage call and the Telegram
        # client want a number, and the spawner passes str(chat_id) of one.
        # Two places already converted at the point of use; the rest were
        # handing a string to parameters typed int and getting away with it
        # because it only ever ended up interpolated into a key or a URL.
        self.chat_id = int(sys.argv[7])
        self.max_dep_time = sys.argv[8]

        # Optional parameters with defaults
        self.passenger_count = int(sys.argv[9]) if len(sys.argv) > 9 else 1
        self.seat_strategy = sys.argv[10] if len(sys.argv) > 10 else "consecutive"
        # Which trains to watch, comma-joined. Empty - and absent, which is
        # how a search started by an older build arrives - means the whole
        # time window, the behaviour that predates picking trains at all.
        self.train_numbers = [
            number for number in (sys.argv[11] if len(sys.argv) > 11 else "").split(",") if number
        ]
        # Which seats will do, flattened by SeatPreference.encode. Empty - and
        # absent, which is how a search started before seats could be asked
        # for arrives - means any seat, the behaviour that predates this.
        self.seat_preference = SeatPreference.decode(sys.argv[12] if len(sys.argv) > 12 else None)

        # Parse train type
        self.train_type = self._parse_train_type(self.train_type_str)
        self.reserve_option = self._parse_reserve_option(self.special_info_str)

        # Initialize services
        self.storage, self.telegram = self._runtime_services()
        self.payment_reminder = PaymentReminderService(self.storage, self.telegram)
        self.multi_reminder = MultiReservationReminderService(self.storage, self.telegram)
        self.rail = self._build_rail_service()

        # Who this process is when it claims the watch on a payment. Per
        # process, so the claim cannot outlive it by being renewed by whatever
        # takes its place.
        self._watch_owner = f"search:{os.getpid()}"

        # Progress reporting state. The first report is due one interval from
        # now rather than immediately: the user has just been told the search
        # started, and repeating that back a second later says nothing.
        self._reported_at: float = time.monotonic()
        self._report_minutes: int = 0
        self._report_minutes_read_at: float | None = None

        logger.info(f"Redis storage connected: {settings.REDIS_HOST}:{settings.REDIS_PORT}")

        # Restore debug mode from Redis
        if self.storage.is_debug_mode():
            LoggerFactory.set_log_level("DEBUG")
            logger.info("Debug mode restored from Redis - log level set to DEBUG")

        logger.info("========================================")
        logger.info("Background Process Initialized")
        logger.info("========================================")
        logger.info(f"  chat_id: {self.chat_id}")
        logger.info(f"  username: {mask_phone(self.username)}")
        logger.info(f"  dep_date: '{self.dep_date}'")
        logger.info(f"  src_locate: '{self.src_locate}'")
        logger.info(f"  dst_locate: '{self.dst_locate}'")
        logger.info(f"  dep_time: '{self.dep_time}'")
        logger.info(f"  max_dep_time: '{self.max_dep_time}'")
        logger.info(f"  train_type_str: '{self.train_type_str}' -> {self.train_type}")
        logger.info(f"  special_info_str: '{self.special_info_str}' -> {self.reserve_option}")
        logger.info(f"  passenger_count: {self.passenger_count}")
        logger.info(f"  seat_strategy: '{self.seat_strategy}'")
        logger.info(
            f"  watching: {', '.join(self.train_numbers) if self.train_numbers else 'every train in the window'}"
        )
        logger.info("========================================")

    def _runtime_services(self):
        """Default bot transport; independent runtimes inject their own services."""
        return RedisStorage(), TelegramService(settings.TELEGRAM_BOT_TOKEN)

    @staticmethod
    def _read_credentials() -> tuple:
        """
        Read Korail credentials from the first line of stdin.

        The parent writes a single JSON object and closes the pipe. Reading
        them here keeps the password out of argv, where any local process
        could see it.

        Returns:
            Tuple of (username, password)
        """
        try:
            raw = sys.stdin.readline()
        except Exception as e:
            logger.error(f"Failed to read credentials from stdin: {e}")
            sys.exit(1)

        if not raw or not raw.strip():
            logger.error(
                "No credentials received on stdin. This process is started by "
                "the bot and cannot be launched manually without them."
            )
            sys.exit(1)

        try:
            payload = json.loads(raw)
            username = payload["username"]
            password = payload["password"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error(f"Malformed credentials payload on stdin: {type(e).__name__}")
            sys.exit(1)

        if not username or not password:
            logger.error("Empty Korail credentials received")
            sys.exit(1)

        return username, password

    def _reservation_time_context(self, reservation) -> str:
        """Show a reserved train on both the user's clock and the railway clock."""
        journey = format_railway_journey(
            str(getattr(reservation, "dep_date", None) or self.dep_date),
            str(getattr(reservation, "dep_time", None) or self.dep_time),
            str(getattr(reservation, "arr_time", None) or ""),
            self.storage.get_user_timezone(self.chat_id),
        )
        return f"🕒 {journey}" if journey else "🌐 열차 시각: KST (대한민국 철도 시각)"

    def _train_info_for_user(self, reservation) -> str:
        """Reservation text plus an unambiguous local/Korean time translation."""
        return f"{reservation}\n{self._reservation_time_context(reservation)}"

    def _check_this_is_right(self, reservation) -> str:
        """
        The date and train spelled out, and the way out if they are wrong.

        Placed after the seat is taken rather than before it. Asking first
        was the obvious design and it does not work: a cancelled seat is gone
        in seconds, and a confirmation prompt spends them. So the seat is
        caught, and then the user is shown what was caught while there is
        still time to hand it back for nothing.

        The date leads because the date is what goes wrong. The train text
        already carries the times and the stations, but a booking on the
        right train on the wrong day reads as correct until the platform.

        Best effort: this is decoration on a message that must go out
        regardless, so anything unreadable is left out rather than raised.
        """
        hints = self._train_hints(reservation)

        lines = []
        date = hints["dep_date"]
        if len(date) == 8 and date.isdigit():
            try:
                day = datetime.strptime(date, "%Y%m%d")
                weekday = "월화수목금토일"[day.weekday()]
                lines.append(f"   📅 {day.year}년 {day.month}월 {day.day}일 ({weekday})")
            except ValueError:
                lines.append(f"   📅 {date}")

        if hints["train_no"]:
            lines.append(f"   🚆 열차번호 {hints['train_no']}")

        if self.passenger_count:
            lines.append(f"   👤 {self.passenger_count}명")

        if not lines:
            return ""

        joined = "\n".join(lines)
        return f"""
🔍 이 표가 맞는지 확인해주세요
{joined}

❗ 날짜나 열차가 다르다면 지금 바로 /tickets 에서 놓아주세요.
   결제 전이라 수수료 없이 되돌릴 수 있습니다. 결제한 뒤에는
   환불 수수료가 붙고, 그건 코레일톡에서만 됩니다."""

    @property
    def payment_url(self) -> str:
        """Where the user goes to pay for a seat this search just took."""
        return settings.KORAIL_PAYMENT_URL

    def _build_rail_service(self):
        """
        Build the Korail client this search runs against.

        A resumed search is the same app session the user started, not a
        freshly launched one, so it picks up the timestamp it began with.
        """
        return KorailService(
            app_session_start=self.storage.get_or_create_app_session_start(self.chat_id),
            on_status=self._announce_search_status,
            on_progress=self._report_search_progress,
        )

    def _parse_train_type(self, train_type_str: str):
        """Parse train type from string."""
        # Check for exact string representation of enum
        if "TrainType.KTX" in train_type_str:
            return TrainType.KTX
        elif "TrainType.ALL" in train_type_str:
            return TrainType.ALL
        # Check for numeric values (backward compatibility)
        elif train_type_str == "100":  # KTX value
            return TrainType.KTX
        elif train_type_str == "0":  # ALL value
            return TrainType.ALL
        # Fallback to checking for keywords
        elif "KTX" in train_type_str.upper() and "ALL" not in train_type_str.upper():
            return TrainType.KTX
        else:
            return TrainType.ALL

    #: The four seat preferences, in the order that lets a substring search
    #: find the right one: no name here is contained in another.
    SEAT_OPTION_NAMES = ("GENERAL_FIRST", "GENERAL_ONLY", "SPECIAL_FIRST", "SPECIAL_ONLY")

    def _parse_reserve_option(self, option_str: str):
        """
        Parse the seat preference from what was stored.

        korail2 and SR spell the four preferences identically -
        GENERAL_FIRST, GENERAL_ONLY, SPECIAL_FIRST, SPECIAL_ONLY - in two
        enums that know nothing of each other. So the name is read once and
        looked up in whichever enum this search's railway takes, and a
        favourite saved against one railway still means something against the
        other.

        Args:
            option_str: What was stored, e.g. "ReserveOption.GENERAL_FIRST"

        Returns:
            A ReserveOption for Korail, a SeatType for SR

        korail2's ReserveOption is a plain class holding four strings rather
        than an Enum, so the name is looked up with getattr rather than by
        subscripting.
        """
        wanted = option_str.upper()
        name = next(
            (option for option in self.SEAT_OPTION_NAMES if option in wanted),
            "GENERAL_FIRST",
        )

        return getattr(ReserveOption, name)

    def run(self):
        """Run the reservation process."""
        try:
            logger.info(f"Logging in as {mask_phone(self.username)}...")

            # Login
            if not self.rail.login(self.username, self.password):
                logger.error("Login failed")
                message = f"""
❌ 코레일 로그인 실패

아이디/비밀번호가 올바르지 않거나 코레일 서버에 문제가 있습니다.

💡 조치 방법:
1. 코레일 회원번호를 확인하세요
2. 비밀번호가 올바른지 확인하세요
3. 코레일 사이트에서 직접 로그인을 시도해보세요
4. 계정이 잠기지 않았는지 확인하세요

🔗 코레일 로그인: {self.payment_url}

정보 수정이 필요하면 /cancel 후 다시 시작하세요.
"""
                self._send_callback(message, status=1)
                return

            logger.info("Login successful, starting reservation loop...")

            # Check seat strategy
            if self.seat_strategy == "random":
                # Random seating: reserve one seat at a time with payment confirmation
                self._run_random_reservation()
                return

            # Consecutive seating: original logic
            # Search and reserve
            reservation = None
            try:
                reservation = self.rail.search_and_reserve_loop(
                    dep_date=self.dep_date,
                    src_locate=self.src_locate,
                    dst_locate=self.dst_locate,
                    dep_time=self.dep_time,
                    max_dep_time=self.max_dep_time,
                    train_type=self.train_type,
                    reserve_option=self.reserve_option,
                    passenger_count=self.passenger_count,
                    seat_strategy=self.seat_strategy,
                    train_numbers=self.train_numbers,
                    seat_preference=self.seat_preference,
                )
            except DuplicateReservationError as e:
                # First duplicate detection - notify user but continue searching
                logger.warning(f"Duplicate reservation detected (first time): {e}")
                message = f"""
⚠️ 기존 예약 감지

이미 동일한 열차에 대한 예약이 존재합니다.

🔄 기존 예약이 취소될 때까지 대기하면서 계속 검색합니다...

🔗 기존 예약 확인: {self.payment_url}

💡 검색을 중단하려면 /cancel 명령어를 사용하세요.
💡 기존 예약을 취소하면 자동으로 새 예약을 시도합니다.
"""
                # Send notification but DON'T stop the process
                self._send_callback(message, status=2)  # status=2 for warning/info

                # Continue the reservation loop (retry)
                logger.info("Continuing search after duplicate detection...")
                try:
                    reservation = self.rail.search_and_reserve_loop(
                        dep_date=self.dep_date,
                        src_locate=self.src_locate,
                        dst_locate=self.dst_locate,
                        dep_time=self.dep_time,
                        max_dep_time=self.max_dep_time,
                        train_type=self.train_type,
                        reserve_option=self.reserve_option,
                        passenger_count=self.passenger_count,
                        seat_strategy=self.seat_strategy,
                        train_numbers=self.train_numbers,
                        seat_preference=self.seat_preference,
                    )
                except DuplicateReservationError:
                    # Should not happen as we already notified, but handle gracefully
                    logger.error("Duplicate error raised again - this shouldn't happen")
                    pass
            except requests.exceptions.RequestException as e:
                logger.error(f"Network error during reservation: {e}")
                message = f"""
🌐 네트워크 오류

코레일 서버와 통신 중 오류가 발생했습니다.

오류 내용: {e!s}

💡 조치 방법:
1. 인터넷 연결을 확인하세요
2. 잠시 후 다시 시도하세요 (/cancel 후 /start)
3. 코레일 서버가 점검 중일 수 있습니다

🔗 코레일 사이트 상태 확인: {self.payment_url}
"""
                self._send_callback(message, status=1)
                return
            except ValueError as e:
                logger.error(f"Invalid data during reservation: {e}")
                message = f"""
⚠️ 입력 데이터 오류

입력하신 정보에 문제가 있습니다.

오류 내용: {e!s}

💡 조치 방법:
1. 역 이름을 확인하세요 (예: 서울, 부산)
2. 날짜 형식을 확인하세요 (YYYYMMDD)
3. 시간 형식을 확인하세요 (HHMMSS)
4. /cancel 후 정확한 정보로 다시 시도하세요
"""
                self._send_callback(message, status=1)
                return
            except Exception as e:
                # Catch any other unexpected errors from the loop
                logger.error(f"Unexpected error in reservation loop: {e}", exc_info=True)
                message = f"""
❌ 예약 검색 중 예상치 못한 오류

오류 유형: {type(e).__name__}
오류 내용: {e!s}

💡 조치 방법:
1. /cancel 후 다시 시도하세요
2. 문제가 계속되면 관리자에게 문의하세요

로그에 자세한 정보가 기록되었습니다.
"""
                self._send_callback(message, status=1)
                return

            if reservation:
                logger.info(f"Reservation successful: {reservation}")

                # Check if this is a random allocation with multiple reservations
                is_random = (
                    hasattr(reservation, "_is_random_allocation")
                    and reservation._is_random_allocation
                )
                total_seats = getattr(reservation, "_total_seats", self.passenger_count)

                # Build success message
                if is_random and total_seats > 1:
                    all_reservations = getattr(reservation, "_all_reservations", [reservation])
                    reservation_details = "\n".join(
                        [
                            f"좌석 {i + 1}: {self._train_info_for_user(res)}"
                            for i, res in enumerate(all_reservations)
                        ]
                    )
                    message = f"""
🎉 열차 예약에 성공했습니다!!

총 {total_seats}명의 좌석이 개별적으로 예약되었습니다.
(랜덤 배치 옵션: 좌석이 떨어져 있을 수 있습니다)

예약에 성공한 열차 정보는 다음과 같습니다.
===================
{reservation_details}
===================

⚠️ 중요: {settings.PAYMENT_TIMEOUT_MINUTES}분내에 사이트에서 결제를 완료하지 않으면 예약이 취소됩니다!

💡 결제하시면 봇이 직접 확인해서 알려드립니다. 따로 답장하실 필요 없습니다.
🔕 재촉 알림만 끄시려면 /notify_off
🔗 결제 링크: {self.payment_url}
"""

                    # Create MultiReservationStatus for smart reminders
                    try:
                        self._create_multi_reservation_status(all_reservations, total_seats)
                    except Exception as e:
                        logger.error(
                            f"Failed to create multi-reservation status: {e}", exc_info=True
                        )
                        # Non-critical error - reservation succeeded, just reminder setup failed
                        # Continue with callback

                else:
                    seats_text = f"{self.passenger_count}명" if self.passenger_count > 1 else ""
                    consecutive_text = " (연속된 좌석)" if self.passenger_count > 1 else ""
                    message = f"""
🎉 열차 예약에 성공했습니다!!

{seats_text}{consecutive_text}

예약에 성공한 열차 정보는 다음과 같습니다.
===================
{self._train_info_for_user(reservation)}
==================={self._check_this_is_right(reservation)}

⚠️ 중요: {settings.PAYMENT_TIMEOUT_MINUTES}분내에 사이트에서 결제를 완료하지 않으면 예약이 취소됩니다!

💡 결제하시면 봇이 직접 확인해서 알려드립니다. 따로 답장하실 필요 없습니다.
🔕 재촉 알림만 끄시려면 /notify_off
🔗 결제 링크: {self.payment_url}
"""

                # Send callback with reservation metadata
                self._send_callback(
                    message,
                    status=0,
                    is_multi=is_random and total_seats > 1,
                    total_seats=total_seats,
                    seat_strategy=self.seat_strategy,
                )

                # Note: Payment reminders will be started by main app after receiving callback
                # (subprocess and main app don't share memory, so reminders must start in main app)

                # Then stay and watch. This process holds the only logged-in
                # Korail session there is - the main app deletes the stored
                # credentials the moment this callback lands - so it is the
                # only thing that can find out whether the payment actually
                # happened.
                if not (is_random and total_seats > 1):
                    self._watch_payment(reservation)

            else:
                logger.warning("Reservation failed - no result")
                message = """
알수 없는 오류로 예매에 실패했습니다. 처음부터 다시 시도해주세요.

[문제가 없는데 계속 반복되는 경우, 이미 해당 열차가 예매가 되었을 수 있습니다. 사이트를 확인해주세요.]
"""
                self._send_callback(message, status=0)

        except Exception as e:
            logger.error(f"Error in reservation process: {e}", exc_info=True)

            # Build detailed error message
            error_type = type(e).__name__
            error_msg = str(e)

            message = f"""
❌ 예약 프로세스 오류 발생

오류 유형: {error_type}
오류 내용: {error_msg}

📋 상황:
- 출발일: {self.dep_date}
- 출발역: {self.src_locate}
- 도착역: {self.dst_locate}
- 출발시각: {self.dep_time}

💡 조치 방법:
1. 인터넷 연결 상태를 확인하세요
2. 코레일 계정 정보가 올바른지 확인하세요
3. 코레일 사이트가 정상 작동하는지 확인하세요
4. /cancel 후 다시 시도하세요

🔗 코레일 사이트 확인: {self.payment_url}
"""
            self._send_callback(message, status=1)

        logger.info(f"Reservation process ended for {mask_phone(self.username)}")

    def _payment_deadline(self, reservation) -> datetime:
        """
        When the railway stops holding this seat.

        Both railways state the deadline on the reservation itself, so that is
        what is used rather than PAYMENT_TIMEOUT_MINUTES - the configured
        value is this bot's idea of the window and can only ever be an
        approximation of theirs. Falls back to it when the reservation does
        not carry a readable one.
        """
        raw_date, raw_time = self.rail.payment_due(reservation)

        if isinstance(raw_date, str) and isinstance(raw_time, str):
            try:
                local = datetime.strptime(f"{raw_date}{raw_time[:6]}", "%Y%m%d%H%M%S")
                return as_utc(local, naive_zone=RAIL_TIMEZONE)
            except ValueError:
                pass

        logger.warning(
            f"Reservation carried no readable payment deadline "
            f"({raw_date!r} {raw_time!r}); falling back to the configured window"
        )
        return utc_now() + timedelta(minutes=settings.PAYMENT_TIMEOUT_MINUTES)

    def _watch_payment(self, reservation) -> None:
        """
        Watch the reservation until it is paid for or lost.

        Korail lists a reservation only while it is waiting to be paid for,
        so it dropping off the list before the deadline means the payment
        went through. Nothing here pays for anything: it is one read-only
        listing call, repeated slowly.

        This replaces a guess. "Payment complete" used to mean the user had
        sent the bot any message at all, so someone who answered the reminder
        without paying was told the matter was settled and quietly lost the
        seat, while someone who paid and said nothing was nagged until the
        window closed.

        The app watches too, for the payments this cannot - a random-seating
        run, or one whose process was killed by a restart. The claim renewed
        on each pass is what keeps the two of them from both asking and both
        announcing; it lapses seconds after this process dies, which is
        exactly when the app should be taking over.
        """
        rsv_id = self.rail.reservation_id(reservation)
        if not rsv_id:
            logger.warning("Reservation has no number - cannot verify payment")
            return

        deadline = self._payment_deadline(reservation)
        interval = settings.PAYMENT_VERIFY_INTERVAL_SECONDS
        # Which train this is on. Korail's issued tickets carry no reservation
        # number, so this is the only thing a paid-for booking can be matched
        # by; SR ignores it. Taken from the object in hand rather than read
        # back out of Redis, which is the same value by a longer route.
        hints = self._train_hints(reservation)
        logger.info(f"Watching reservation {rsv_id} until {deadline:%H:%M:%S}")

        # Claimed before the details are written down, so the app never sees a
        # reservation it could watch without also seeing that this is on it.
        self._claim_the_watch()
        self._record_pending_payment(reservation, rsv_id, deadline)

        def now() -> datetime:
            # Legacy/test deadlines are naive and must stay on the wall clock
            # that created them. New railway deadlines are aware UTC.
            return utc_now() if deadline.tzinfo else datetime.now()

        while now() < deadline:
            time.sleep(min(interval, max(1.0, (deadline - now()).total_seconds())))
            self._claim_the_watch()

            outcome = self.rail.reservation_outcome(rsv_id, **hints)

            if outcome in (ReservationOutcome.UNKNOWN, ReservationOutcome.OUTSTANDING):
                # UNKNOWN: the railway could not be asked, or could not say
                # which of paid and cancelled a disappearance was. Neither is
                # an answer, and saying the payment went through here would be
                # the same guess this exists to remove.
                continue

            if outcome is ReservationOutcome.PAID:
                logger.info(f"Reservation {rsv_id} was issued as a ticket - payment settled")
            else:
                logger.info(f"Reservation {rsv_id} went back unpaid")
            self._settle_payment(outcome)
            return

        # The deadline passed with the reservation still on the list, so it
        # was never paid for. Worth saying plainly even when the user has
        # already told us otherwise: they are the one who will find out at
        # the station.
        logger.warning(f"Reservation {rsv_id} expired unpaid")
        self._settle_payment(ReservationOutcome.OUTSTANDING)

    def _claim_the_watch(self) -> None:
        """
        Tell the app that this process is watching, and keep telling it.

        Best effort in both directions. If the claim cannot be made, the worst
        case is that the app watches too and one of them announces the payment
        first; if it is never renewed - because this process died - the app
        picks the payment up, which is the point.
        """
        try:
            self.storage.claim_payment_watch(
                self.chat_id, self._watch_owner, settings.PAYMENT_WATCH_LEASE_SECONDS
            )
        except Exception as e:
            logger.warning(f"Could not claim the payment watch for chat_id={self.chat_id}: {e}")

    def _record_pending_payment(self, reservation, rsv_id: str, deadline: datetime) -> None:
        """
        Write down what the user is being asked to pay for.

        The payment record used to say only that a window was open, which was
        all the reminder loop needed. /status has to name the booking, and
        giving it back needs its number - and this process is the only place
        that has either: the main app deletes the credentials the moment the
        reservation lands, and never sees the reservation itself.

        Written after the callback, which is what creates the record. Best
        effort: the seat is already booked and the user already told, so
        nothing here is worth failing the watch over.
        """
        try:
            status = self.storage.get_payment_status(self.chat_id)
            if not status:
                # The callback did not get as far as creating one. A record
                # with the reservation on it beats no record at all.
                status = PaymentStatus(chat_id=self.chat_id, completed=False, reminder_active=False)

            status.reservation_id = rsv_id
            status.train_info = self._train_info_for_user(reservation)
            status.expires_at = deadline
            hints = self._train_hints(reservation)
            status.train_no = hints["train_no"]
            status.dep_date = hints["dep_date"]
            status.dep_time = hints["dep_time"]
            self.storage.save_payment_status(status)
            logger.info(f"Recorded reservation {rsv_id} as awaiting payment until {deadline:%H:%M}")
        except Exception as e:
            logger.error(f"Could not record what {rsv_id} is waiting on: {e}", exc_info=True)

    def _train_hints(self, reservation) -> dict[str, str]:
        """
        Which train a booking is on, in the three fields the check matches on.

        Korail files an issued ticket under no reservation number at all, so
        the only way to tell a booking that was paid for from one the user
        cancelled is to look for a ticket on the same train, on the same day,
        at the same time. describe_train already knows which field each
        operator keeps the train number in.

        Best effort, and empty on failure by design: nothing here is worth
        ending a watch over, and a check with no hints answers "cannot tell",
        which is the safe end of the trade.
        """
        try:
            train = self.rail.describe_train(reservation)
            return {
                "train_no": str(train.get("no") or ""),
                "dep_date": str(getattr(reservation, "dep_date", "") or ""),
                "dep_time": str(train.get("dep_time") or ""),
            }
        except Exception as e:
            logger.warning(f"Could not read which train this booking is on: {e}")
            return {"train_no": "", "dep_date": "", "dep_time": ""}

    def _settle_payment(self, outcome: ReservationOutcome) -> None:
        """
        Record what the watch found, and tell the user.

        Three endings, not two. A reservation that left the unpaid list
        without a ticket behind it was not paid for - it was cancelled, or
        the deadline caught it - and calling that a payment is exactly the
        thing that told a user their cancelled booking had gone through.

        OUTSTANDING arrives here only from the deadline: the loop ran out of
        time with the seat still listed.
        """
        status = self.storage.get_payment_status(self.chat_id)
        already_confirmed = bool(status and status.completed)

        # Either way the reminder loop in the main app has nothing left to
        # remind anyone about, and it reads this flag out of Redis.
        if status:
            status.completed = True
            status.reminder_active = False
            if outcome is ReservationOutcome.RELEASED:
                # `completed` only means "stop reminding". Which of paid and
                # given back it was has to be on the record too.
                status.cancelled = True
            self.storage.save_payment_status(status)

        if outcome is ReservationOutcome.PAID:
            # The user having said so already makes this a confirmation of
            # something they know. No second message for that.
            if not already_confirmed:
                self.telegram.send_message(self.chat_id, Messages.PAYMENT_VERIFIED)
        elif outcome is ReservationOutcome.RELEASED:
            # Said even when the user claimed to have paid: a cancellation is
            # the one they are least likely to know about, and the news they
            # would otherwise get at the platform.
            self.telegram.send_message(self.chat_id, Messages.PAYMENT_RELEASED)
        else:
            self.telegram.send_message(self.chat_id, Messages.PAYMENT_EXPIRED_VERIFIED)

    def _update_multi_reservation_status(
        self, seat_index: int, reservation, total_seats: int
    ) -> None:
        """
        Create or update MultiReservationStatus for tracking individual seat payment.

        Called after each seat is reserved in random allocation mode.

        Args:
            seat_index: Index of the seat just reserved (0-based)
            reservation: Reservation object from korail2
            total_seats: Total number of seats being reserved
        """
        try:
            now = utc_now()
            expires_at = now + timedelta(minutes=settings.PAYMENT_TIMEOUT_MINUTES)

            # Get existing status or create new
            multi_status = self.storage.get_multi_reservation_status(self.chat_id)

            if not multi_status or seat_index == 0:
                # First seat - delete any old status and create fresh one
                if seat_index == 0 and multi_status:
                    logger.info(f"Deleting old MultiReservationStatus for chat_id={self.chat_id}")
                    self.storage.delete_multi_reservation_status(self.chat_id)

                logger.info(f"Creating new MultiReservationStatus for chat_id={self.chat_id}")
                multi_status = MultiReservationStatus(
                    chat_id=self.chat_id,
                    reservations=[],
                    total_seats=total_seats,
                    seat_strategy=self.seat_strategy,
                    created_at=now,
                    manually_stopped=False,
                )

            # Add this reservation
            rsv_id = self.rail.reservation_id(reservation) or f"seat_{seat_index + 1}"
            hints = self._train_hints(reservation)
            info = SingleReservationInfo(
                reservation_id=rsv_id,
                reservation_obj=reservation,
                reserved_at=now,
                expires_at=expires_at,
                status=ReservationPaymentStatus.PENDING,
                seat_number=seat_index + 1,
                train_info=self._train_info_for_user(reservation),
                # What the watchdog matches an issued Korail ticket against.
                # Nothing else records these, so a seat booked by a build
                # without them stays unmatched - and unmatched means silence.
                train_no=hints["train_no"],
                dep_date=hints["dep_date"],
                dep_time=hints["dep_time"],
            )
            multi_status.reservations.append(info)

            # Save to storage
            self.storage.save_multi_reservation_status(multi_status)
            logger.info(
                f"Updated MultiReservationStatus: {len(multi_status.reservations)}/{total_seats} seats"
            )

        except Exception as e:
            logger.error(f"Failed to update MultiReservationStatus: {e}", exc_info=True)

    def _create_multi_reservation_status(self, all_reservations: list, total_seats: int) -> None:
        """
        Create MultiReservationStatus for tracking individual seat payment.
        (Legacy method - kept for compatibility)

        Args:
            all_reservations: List of reservation objects from korail2
            total_seats: Total number of seats reserved
        """
        try:
            now = utc_now()
            expires_at = now + timedelta(minutes=settings.PAYMENT_TIMEOUT_MINUTES)

            # Create SingleReservationInfo for each reservation
            reservation_infos = []
            for i, res in enumerate(all_reservations):
                rsv_id = self.rail.reservation_id(res) or f"unknown_{i + 1}"

                info = SingleReservationInfo(
                    reservation_id=rsv_id,
                    reservation_obj=res,
                    reserved_at=now,
                    expires_at=expires_at,
                    status=ReservationPaymentStatus.PENDING,
                    seat_number=i + 1,
                    train_info=self._train_info_for_user(res),
                )
                reservation_infos.append(info)

            # Create MultiReservationStatus
            multi_status = MultiReservationStatus(
                chat_id=self.chat_id,
                reservations=reservation_infos,
                total_seats=total_seats,
                seat_strategy=self.seat_strategy,
                created_at=now,
                manually_stopped=False,
            )

            # Save to storage
            self.storage.save_multi_reservation_status(multi_status)
            logger.info(
                f"Created MultiReservationStatus for chat_id={self.chat_id} "
                f"with {len(reservation_infos)} reservations"
            )

        except Exception as e:
            logger.error(f"Failed to create MultiReservationStatus: {e}", exc_info=True)

    def _announce_search_status(self, message: str) -> None:
        """
        Tell the user how the search itself is doing.

        Straight to Telegram rather than through _send_callback: this is news
        about the search, not a result for it, and every callback status means
        the search has ended one way or another.
        """
        self.telegram.send_message(self.chat_id, message)

    # ==================== Reporting in ====================
    #
    # A search can run for hours without saying anything, and silence is
    # indistinguishable from a process that died. /notify turns on a periodic
    # "still going" message; off unless asked for, because an unwanted message
    # every five minutes is worse than the silence it replaces.
    #
    # Two clocks, with a reason each. The interval is what the user asked for.
    # The preference is re-read from Redis at most every so often, so that
    # /notify reaches a search already running without the loop reading a key
    # on every one of its roughly-one-a-second passes.

    def _report_interval_minutes(self) -> int:
        """
        How often this chat wants to hear from the search. 0 is off.

        Cached, and deliberately fail-quiet: Redis being briefly unreachable
        should make the reports pause, not end the search.
        """
        now = time.monotonic()
        if (
            self._report_minutes_read_at is None
            or now - self._report_minutes_read_at >= settings.PROGRESS_PREFERENCE_TTL_SECONDS
        ):
            self._report_minutes_read_at = now
            try:
                self._report_minutes = self.storage.get_progress_report_minutes(self.chat_id)
            except Exception as e:
                logger.warning(f"Could not read the progress report preference: {e}")
                self._report_minutes = 0

        return self._report_minutes

    def _report_search_progress(self, progress) -> None:
        """
        Say that the search is still going, no more often than asked.

        Called on every pass of the search loop, which is why nearly every
        call returns here without doing anything.
        """
        minutes = self._report_interval_minutes()
        if minutes <= 0:
            return

        now = time.monotonic()
        if now - self._reported_at < minutes * 60:
            return

        self._reported_at = now
        self.telegram.send_message(self.chat_id, self._progress_message(progress))

    def _progress_message(self, progress) -> str:
        """What a progress report reads like."""
        watch = (
            f"지정 열차 {len(self.train_numbers)}개 ({', '.join(self.train_numbers)}번)"
            if self.train_numbers
            else "시간대 전체"
        )
        health = (
            "코레일 응답 정상"
            if progress.healthy
            else f"코레일 응답 없음 (연속 {progress.failure_streak}회 실패, 간격을 늘려 재시도 중)"
        )

        return Messages.SEARCH_PROGRESS.format(
            elapsed=format_duration(progress.elapsed_seconds),
            srcLocate=self.src_locate,
            dstLocate=self.dst_locate,
            depDate=self.dep_date,
            depTime=self.dep_time[:4] if self.dep_time else "N/A",
            maxDepTime=self.max_dep_time,
            watch=watch,
            attempts=f"{progress.attempts:,}",
            health=health,
        )

    def _send_callback(
        self,
        message: str,
        status: int = 0,
        is_multi: bool = False,
        total_seats: int = 1,
        seat_strategy: str = "consecutive",
    ):
        """
        Send callback to main app.

        Args:
            message: Message to send to user
            status: 0 for success/completion, 1 for error
            is_multi: True if multi-reservation (random allocation with multiple seats)
            total_seats: Total number of seats reserved
            seat_strategy: Seat allocation strategy used
        """
        try:
            callback_url = f"{settings.CALLBACK_BASE_URL}/reservation-callback"
            params: dict[str, str | int] = {
                "chatId": self.chat_id,
                "msg": message,
                "status": status,
                "isMulti": "1" if is_multi else "0",
                "totalSeats": str(total_seats),
                "seatStrategy": seat_strategy,
                # Inherited from the parent process via the environment.
                "token": settings.INTERNAL_CALLBACK_TOKEN,
            }

            session = requests.session()
            response = session.get(callback_url, params=params, timeout=10)

            if response.status_code == 200:
                logger.debug(f"Callback sent successfully: status={status}, is_multi={is_multi}")
            else:
                logger.warning(
                    f"Callback returned non-200 status: {response.status_code}, "
                    f"response={response.text[:200]}"
                )

        except requests.exceptions.Timeout:
            logger.error("Callback timeout - main app may be down or slow")
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Failed to connect to main app for callback: {e}")
        except Exception as e:
            logger.error(f"Unexpected error sending callback: {e}", exc_info=True)

    def _run_random_reservation(self):
        """
        Run random seating reservation: one seat at a time with payment confirmation.

        Flow for each seat:
        1. Search and reserve one seat
        2. Send notification to user
        3. Wait for payment confirmation (up to 10 minutes)
        4. Proceed to next seat
        """
        total_seats = self.passenger_count
        logger.info(f"=== RANDOM SEATING MODE: {total_seats} seats ===")

        for seat_index in range(total_seats):
            logger.info(f"━━━ Seat {seat_index + 1}/{total_seats} ━━━")

            # Reserve one seat (don't set current_seat_index yet!)
            try:
                reservation = self._reserve_single_seat_random(seat_index)
            except Exception as e:
                logger.error(f"Failed to reserve seat {seat_index + 1}: {e}", exc_info=True)
                error_msg = f"""
❌ {seat_index + 1}번째 좌석 예약 실패

오류: {e!s}

💡 /cancel 후 다시 시도하세요.
"""
                self._send_callback(error_msg, status=1)
                return

            if not reservation:
                logger.error(f"No reservation returned for seat {seat_index + 1}")
                error_msg = f"❌ {seat_index + 1}번째 좌석 예약 실패 (결과 없음)"
                self._send_callback(error_msg, status=1)
                return

            # Save partial reservation
            reservation_data = {
                "seat_index": seat_index,
                "train_info": self._train_info_for_user(reservation),
                "reserved_at": utc_now().isoformat(),
            }
            self.storage.save_partial_reservation(self.chat_id, seat_index, reservation_data)
            logger.info(f"✅ Seat {seat_index + 1} reserved and saved to Redis")

            # Create or update MultiReservationStatus for reminder service
            self._update_multi_reservation_status(seat_index, reservation, total_seats)

            # NOTE: Don't start reminder service here (subprocess doesn't share memory with main app)
            # Reminder will be started by main app when it receives the callback (status=2)

            # NOW set current seat index for payment waiting
            # This prevents "결제 대기중" message before reservation succeeds
            self.storage.set_current_seat_index(self.chat_id, seat_index)

            # Send notification to user
            message = self._build_partial_reservation_message(seat_index, total_seats, reservation)
            self._send_callback(
                message, status=2, seat_strategy=self.seat_strategy
            )  # status=2: partial success

            # Wait for payment confirmation (or timeout)
            if seat_index < total_seats - 1:  # Not the last seat
                logger.info(f"⏳ Waiting for payment confirmation for seat {seat_index + 1}...")
                payment_confirmed = self.storage.wait_for_payment(
                    self.chat_id,
                    seat_index,
                    timeout=600,  # 10 minutes
                )

                if payment_confirmed:
                    logger.info(f"✅ Payment confirmed for seat {seat_index + 1}")
                    confirm_msg = f"""
✅ {seat_index + 1}번째 좌석 결제 확인!

다음 좌석 예약을 시작합니다...
"""
                    self._send_callback(confirm_msg, status=2, seat_strategy=self.seat_strategy)
                else:
                    logger.warning(f"⏱ Payment timeout for seat {seat_index + 1}")
                    timeout_msg = f"""
⏱ {seat_index + 1}번째 좌석 결제 시간 초과

10분이 지났습니다. 다음 좌석 예약을 진행합니다.

⚠️ 미결제 좌석은 자동 취소될 수 있으니 빠르게 결제해주세요!
"""
                    self._send_callback(timeout_msg, status=2, seat_strategy=self.seat_strategy)

                # Brief pause before next reservation
                logger.info("Waiting 3 seconds before next reservation...")
                time.sleep(3)

        # All seats reserved!
        self.storage.set_current_seat_index(self.chat_id, None)  # Clear index
        all_reservations = self.storage.get_partial_reservations(self.chat_id)

        final_message = self._build_final_random_message(all_reservations, total_seats)
        self._send_callback(
            final_message, status=0, seat_strategy=self.seat_strategy
        )  # status=0: complete success

        logger.info(f"🎉 All {total_seats} seats reserved successfully!")

    def _reserve_single_seat_random(self, seat_index: int):
        """
        Reserve a single seat for random allocation.

        Args:
            seat_index: Index of the seat being reserved (0-based)

        Returns:
            Reservation object if successful

        Raises:
            Exception: If reservation fails with non-duplicate error
        """
        logger.info(f"🔍 Starting search for seat {seat_index + 1}...")

        attempts = 0
        max_attempts = None  # Infinite
        duplicate_notified = False  # Track if we already notified about duplicate

        while True:
            attempts += 1
            if max_attempts and attempts > max_attempts:
                logger.error(f"Max attempts reached for seat {seat_index + 1}")
                return None

            is_summary = attempts % 60 == 0

            if is_summary:
                logger.debug(f"🔄 Search attempt #{attempts} for seat {seat_index + 1}")

            self.rail.report_progress(attempts)

            # Search for trains (single passenger)
            try:
                self.rail.ensure_logged_in()
                trains = self.rail.search_trains(
                    dep_date=self.dep_date,
                    src_locate=self.src_locate,
                    dst_locate=self.dst_locate,
                    dep_time=self.dep_time,
                    max_dep_time=self.max_dep_time,
                    train_type=self.train_type,
                    passenger_count=1,  # Single seat
                    verbose=is_summary,
                    train_numbers=self.train_numbers,
                )
            except SearchUnavailableError as e:
                # Backs off and tells the user once the run of failures is
                # long enough to mean something. Used to be a bare
                # `except Exception` that logged and retried at full rate,
                # so a Korail that had stopped answering was invisible.
                self.rail.wait_between_requests(self.rail.note_search_failure(e))
                continue

            self.rail.note_search_success()

            if trains:
                logger.debug(f"✅ Search completed: found {len(trains)} trains")
            elif is_summary:
                logger.debug(f"📊 Attempt #{attempts}: no trains found, retrying...")

            if not trains:
                self.rail.wait_between_requests()
                continue

            # Try to reserve (trains found = rare, always log)
            duplicate_found = False
            for idx, train in enumerate(trains, 1):
                logger.info(f"🚂 Trying train {idx}/{len(trains)}: {train}")

                reservation = self.rail.reserve_train(
                    train,
                    option=self.reserve_option,
                    passenger_count=1,
                    seat_preference=self.seat_preference,
                )

                if reservation == "DUPLICATE":
                    # Duplicate reservation exists - notify user once and keep retrying
                    logger.warning(f"⚠️ Duplicate reservation detected for seat {seat_index + 1}")
                    duplicate_found = True

                    if not duplicate_notified:
                        # Send notification only once
                        self.telegram.send_message(
                            self.chat_id,
                            f"⚠️ {seat_index + 1}번째 좌석 예약 시도 중 기존 예약 감지\n\n"
                            f"이미 해당 시간에 예약된 좌석이 있습니다.\n"
                            f"기존 예약이 취소될 때까지 10초마다 재시도합니다.\n\n"
                            f"🔗 기존 예약 확인: {self.payment_url}\n\n"
                            f"💡 검색을 중단하려면 /cancel 명령어를 사용하세요.\n"
                            f"💡 기존 예약을 취소하면 자동으로 새 예약을 시도합니다.",
                        )
                        duplicate_notified = True
                        logger.info(f"📢 Duplicate notification sent for seat {seat_index + 1}")

                    # Continue to next train
                    continue

                elif reservation:
                    # Not a win until it is a seat the user asked for. A
                    # rejected seat has already been given back by the time
                    # this returns False, so the loop just keeps looking.
                    if not self.rail.keeps_seat(reservation, self.seat_preference):
                        continue

                    logger.info(
                        f"✅ Seat {seat_index + 1} reserved after {attempts} search attempts!"
                    )
                    logger.info(f"🎉 Successfully reserved: {reservation}")
                    return reservation
                else:
                    logger.info(f"  ❌ Train {idx} failed (sold out or unavailable)")

            # All trains in this search failed
            if duplicate_found:
                logger.info("⚠️ Duplicate reservation detected, waiting ~10s before retry...")
                self.rail.wait_seconds(10)  # Around 10 seconds when duplicate found
            else:
                logger.debug(f"All {len(trains)} trains sold out in attempt #{attempts}")
                self.rail.wait_between_requests()

    def _build_partial_reservation_message(
        self, seat_index: int, total_seats: int, reservation
    ) -> str:
        """Build message for partial reservation success."""
        return f"""
🎉 {seat_index + 1}/{total_seats}번째 좌석 예약 성공!

━━━━━━━━━━━━━━━━━━━━
{self._train_info_for_user(reservation)}
━━━━━━━━━━━━━━━━━━━━{self._check_this_is_right(reservation)}

⏰ 예약 후 {settings.PAYMENT_TIMEOUT_MINUTES}분 이내 결제하세요!
🔗 결제: {self.payment_url}

💡 결제가 확인되면 다음 좌석 예약이 자동으로 시작됩니다.
🔕 재촉 알림만 끄시려면 /notify_off

⚠️ 결제가 확인되지 않아도 10분 뒤에는 다음 좌석 예약을 진행합니다.
   지금 바로 넘어가려면 아무 메시지나 보내주세요.
"""

    def _build_final_random_message(self, all_reservations: list, total_seats: int) -> str:
        """Build final message for all random reservations complete."""
        reservation_details = "\n".join(
            [f"좌석 {i + 1}: {r.get('train_info', 'N/A')}" for i, r in enumerate(all_reservations)]
        )

        return f"""
🎉🎉 모든 좌석 예약 완료! 🎉🎉

총 {total_seats}명의 좌석이 개별적으로 예약되었습니다.
(랜덤 배치: 좌석이 떨어져 있을 수 있습니다)

━━━━━━━━━━━━━━━━━━━━
{reservation_details}
━━━━━━━━━━━━━━━━━━━━

⚠️ 중요 안내:
• 모든 좌석을 {settings.PAYMENT_TIMEOUT_MINUTES}분 내 결제해야 합니다!
• 미결제 시 자동 취소됩니다!

🔗 결제 링크: {self.payment_url}

✅ 축하합니다! 🎊
"""


if __name__ == "__main__":
    install_shutdown_handlers()
    try:
        process = BackgroundReservationProcess()
        process.run()
    except SearchStopped as stopped:
        # The record in Redis is deliberately left alone: either the bot
        # removed it before signalling (/cancel), or it is shutting down and
        # will resume the search from that record on its next start.
        logger.info(f"Search stopped by {stopped.signal_name} - exiting")
    except KeyboardInterrupt:
        # Only reachable if the interrupt arrives before the handlers are in
        # place. Same outcome, without the traceback.
        logger.info("Search interrupted - exiting")
