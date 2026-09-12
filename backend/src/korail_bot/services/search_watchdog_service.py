"""Noticing searches that stopped without saying so."""

import threading

from korail_bot.config.settings import settings
from korail_bot.services.reservation_service import ReservationService
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)


class SearchWatchdogService:
    """
    Checks that the searches on record are still being performed.

    A search runs in a process of its own, and every way it is meant to end
    goes through a callback that clears its record away. Death is the
    exception: a process killed for memory, or crashing somewhere the error
    path does not reach, leaves the record behind and tells nobody. From the
    user's side that looks exactly like a search finding nothing - the bot is
    up, the last message says the search began, and the silence means the
    tickets have not come up yet.

    It can go on for hours, and the whole point of the bot is to be watching
    during those hours. So a loop checks the records against the processes
    that should be behind them, and anything missing is reported to the user
    with the choice of starting it again.
    """

    def __init__(self, reservation_service: ReservationService):
        """
        Initialize the watchdog.

        Args:
            reservation_service: Owns the search processes, and does the
                                 reporting when one turns out to be gone
        """
        self.reservation = reservation_service
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start watching, in a background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Search watchdog is already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run, name="search-watchdog", daemon=True)
        self._thread.start()
        logger.info("Search watchdog started")

    def stop(self) -> None:
        """Ask the loop to finish; it wakes from its sleep to do so."""
        self._stop_event.set()

    def run(self) -> None:
        """
        Check the searches, over and over, until asked to stop.

        Never raises: this is the whole body of a thread, and the failure it
        exists to catch is precisely the one nobody else notices. A watchdog
        that dies quietly is worse than none, because it is trusted.
        """
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                logger.error(f"Watchdog pass failed: {e}", exc_info=True)

            self._stop_event.wait(settings.WATCHDOG_POLL_SECONDS)

        logger.info("Search watchdog stopped")

    def tick(self) -> int:
        """
        One pass over the running searches.

        Returns:
            How many dead searches were found and reported
        """
        found = self.reservation.detect_dead_searches()
        if found:
            logger.warning(f"Watchdog found {found} search(es) that had stopped without notice")
        return found
