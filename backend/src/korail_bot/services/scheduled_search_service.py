"""Starting searches at a time the user chose rather than straight away."""

import threading
from datetime import datetime, timedelta

from korail_bot.config.settings import settings
from korail_bot.models import ScheduledSearch, TrainSearchParams
from korail_bot.services.reservation_service import ReservationService
from korail_bot.services.telegram_service import TelegramService
from korail_bot.storage.base import StorageInterface
from korail_bot.telegramBot.messages import Messages
from korail_bot.utils.logger import get_logger
from korail_bot.utils.timezone import (
    DEFAULT_USER_TIMEZONE,
    as_utc,
    format_user_datetime,
    is_valid_timezone,
    railway_datetime,
    timezone_of,
    utc_now,
)

logger = get_logger(__name__)


class ScheduleError(Exception):
    """A start time that cannot be honoured, with a reason to show the user."""


class ScheduledSearchService:
    """
    Holds searches until their start time, then starts them.

    Tickets are not released evenly. Holiday booking opens at an announced
    minute, and cancellations bunch up in the hours before a train leaves. A
    search that begins at the right moment does better than one that has been
    running since yesterday - and it spends far fewer requests getting there.

    The waiting is done here rather than by the search process itself: a
    process asleep for two days is a process that dies with the next restart,
    while a record in Redis is picked up by whatever is running when the time
    comes.
    """

    def __init__(
        self,
        storage: StorageInterface,
        telegram_service: TelegramService,
        reservation_service: ReservationService,
    ):
        """
        Initialize the scheduler.

        Args:
            storage: Storage interface
            telegram_service: Used to tell the user their search has begun
            reservation_service: Starts the search when the time comes
        """
        self.storage = storage
        self.telegram = telegram_service
        self.reservation = reservation_service
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ==================== Booking ====================

    def validate_start_time(self, start_at: datetime, search_params: TrainSearchParams) -> None:
        """
        Check a start time is one the search can actually be run at.

        Raises rather than returns a flag: every one of these has something
        specific to tell the user, and a bare False at the call site would
        turn them all into the same shrug.

        Args:
            start_at: When the search should begin
            search_params: What it will search for, for the departure check

        Raises:
            ScheduleError: With a message meant for the user
        """
        now = utc_now()
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone_of(DEFAULT_USER_TIMEZONE))
        start_at = as_utc(start_at)

        if start_at <= now:
            raise ScheduleError(Messages.SCHEDULE_IN_THE_PAST)

        limit = now + timedelta(seconds=settings.SCHEDULE_MAX_AHEAD_SECONDS)
        if start_at > limit:
            # The login the search will need is stored under an expiry, and
            # this is that expiry. Booking past it would produce a schedule
            # that arrives with no way to log in.
            days = settings.SCHEDULE_MAX_AHEAD_SECONDS // 86400
            raise ScheduleError(Messages.SCHEDULE_TOO_FAR.format(days=days))

        departure = self._departure_of(search_params)
        if departure and start_at >= as_utc(departure):
            # Starting after the train has left is a search that can only ever
            # find nothing, run at full request rate, until someone cancels it.
            raise ScheduleError(
                Messages.SCHEDULE_AFTER_DEPARTURE.format(departure=f"{departure:%m/%d %H:%M} KST")
            )

    @staticmethod
    def _departure_of(search_params: TrainSearchParams) -> datetime | None:
        """When the earliest train in the search window leaves, if that is readable."""
        return railway_datetime(search_params.dep_date, search_params.dep_time[:4])

    def schedule(
        self,
        chat_id: int,
        username: str,
        password: str,
        search_params: TrainSearchParams,
        start_at: datetime,
        timezone_name: str | None = None,
    ) -> ScheduledSearch:
        """
        Book a search for later.

        Args:
            chat_id: Telegram chat ID
            username: Korail ID the search will log in with
            password: Its password, stored encrypted under the resume key
            search_params: What to search for
            start_at: When to begin

        Returns:
            The stored schedule

        Raises:
            ScheduleError: When the start time cannot be honoured
        """
        timezone_name = timezone_name or self.storage.get_user_timezone(chat_id)
        if not is_valid_timezone(timezone_name):
            timezone_name = DEFAULT_USER_TIMEZONE
        start_at = as_utc(start_at, naive_zone=timezone_of(timezone_name))
        self.validate_start_time(start_at, search_params)

        search = ScheduledSearch(
            chat_id=chat_id,
            korail_id=username,
            search_params=search_params,
            start_at=start_at,
            timezone_name=timezone_name,
        )
        # The password has to outlive the conversation for this to work at
        # all. It goes in the same encrypted slot a restart would use, under
        # the same expiry - which is what bounds how far ahead a search can
        # be booked.
        self.storage.save_resume_credentials(chat_id, username, password)
        self.storage.save_scheduled_search(search)

        logger.info(f"Scheduled a search for chat_id={chat_id} at {start_at:%Y-%m-%d %H:%M}")
        return search

    def cancel(self, chat_id: int) -> bool:
        """
        Drop a booked search.

        Returns:
            True when there was one to drop
        """
        if not self.storage.get_scheduled_search(chat_id):
            return False

        self.storage.delete_scheduled_search(chat_id)
        self.storage.delete_resume_credentials(chat_id)
        logger.info(f"Cancelled the scheduled search for chat_id={chat_id}")
        return True

    # ==================== Waiting ====================

    def start(self) -> None:
        """Start watching for schedules coming due, in a background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Scheduled search service is already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run, name="search-scheduler", daemon=True)
        self._thread.start()
        logger.info("Search scheduler started")

    def stop(self) -> None:
        """Ask the loop to finish; it wakes from its sleep to do so."""
        self._stop_event.set()

    def run(self) -> None:
        """
        Wait for schedules to come due, and start them.

        Never raises: this is the whole body of a thread, and an escaping
        exception would leave every booked search waiting forever with
        nothing to say so.
        """
        while not self._stop_event.is_set():
            try:
                delay = self.tick()
            except Exception as e:
                logger.error(f"Scheduler pass failed: {e}", exc_info=True)
                delay = settings.SCHEDULE_POLL_SECONDS

            self._stop_event.wait(delay)

        logger.info("Search scheduler stopped")

    def tick(self) -> float:
        """
        Start whatever is due, and say how long until the next thing is.

        Returns:
            Seconds to wait before looking again
        """
        searches = self.storage.get_all_scheduled_searches()
        if not searches:
            return settings.SCHEDULE_POLL_SECONDS

        now = utc_now()
        waiting = []
        for search in searches:
            if search.is_due(now):
                self._fire(search, now)
            else:
                waiting.append(search)

        if not waiting:
            return settings.SCHEDULE_POLL_SECONDS

        # Sleep exactly until the next one is due rather than polling at a
        # fixed rate. Someone waiting for booking to open at 07:00 means
        # 07:00, and a thirty-second poll would start them up to thirty
        # seconds late - by which time the tickets are gone.
        soonest = min(search.seconds_until_due(now) for search in waiting)
        return max(0.0, min(soonest, settings.SCHEDULE_POLL_SECONDS))

    def _fire(self, search: ScheduledSearch, now: datetime) -> None:
        """Start one search whose time has come, or explain why it cannot."""
        chat_id = search.chat_id
        # Removed first. Whatever happens next, this schedule has been dealt
        # with, and a failure that left the record in place would try again on
        # every pass of the loop.
        self.storage.delete_scheduled_search(chat_id)

        late_by = (as_utc(now) - as_utc(search.start_at)).total_seconds()
        if late_by > settings.SCHEDULE_GRACE_SECONDS:
            # The app was down when this came due. Starting it now would be a
            # search the user asked for hours ago appearing unannounced.
            logger.warning(
                f"Scheduled search for chat_id={chat_id} is {late_by / 60:.0f} minutes late - "
                f"past the grace period, not starting it"
            )
            self.telegram.send_message(
                chat_id,
                Messages.SCHEDULE_MISSED.format(
                    startAt=format_user_datetime(
                        search.start_at, search.timezone_name, "%m/%d %H:%M"
                    ),
                    srcLocate=search.search_params.src_locate,
                    dstLocate=search.search_params.dst_locate,
                ),
            )
            self.storage.delete_resume_credentials(chat_id)
            return

        credentials = self.storage.get_resume_credentials(chat_id)
        if not credentials:
            logger.error(f"Scheduled search for chat_id={chat_id} has no stored login")
            self.telegram.send_message(chat_id, Messages.SCHEDULE_NO_CREDENTIALS)
            return

        username, password = credentials
        logger.info(f"Starting the scheduled search for chat_id={chat_id}")

        self.telegram.send_message(
            chat_id,
            Messages.SCHEDULE_STARTING.format(
                srcLocate=search.search_params.src_locate,
                dstLocate=search.search_params.dst_locate,
                depDate=search.search_params.dep_date,
            ),
        )

        started = self.reservation.start_reservation_process(
            chat_id=chat_id,
            username=username,
            password=password,
            search_params=search.search_params,
        )
        if not started:
            # start_reservation_process refuses when a search is already
            # running for this chat, and has told the user so itself.
            logger.warning(f"Scheduled search for chat_id={chat_id} could not be started")
