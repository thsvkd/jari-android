"""Long-polling receiver for Telegram updates."""

import json
import threading
from typing import Any

import requests

from korail_bot.config.settings import settings
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)


class TelegramPoller:
    """
    Pulls updates from the Telegram Bot API with getUpdates.

    Every connection is outbound, so this works on a Raspberry Pi behind a
    router without a public address. Updates are handed to the transport-free
    processor used by the conversation and command tests.
    """

    # A failing Telegram API is retried forever, but slowly: the bot is
    # useless while it is down and hammering it helps nobody.
    INITIAL_BACKOFF_SECONDS: float = 1.0
    MAX_BACKOFF_SECONDS: float = 60.0

    def __init__(
        self,
        bot_token: str,
        update_processor: Any,
        poll_timeout: int | None = None,
        request_timeout: int | None = None,
    ):
        """
        Initialize the poller.

        Args:
            bot_token: Telegram bot token
            update_processor: Object with a process(update: dict) method
            poll_timeout: Long-poll timeout sent to Telegram, in seconds
            request_timeout: HTTP timeout, must exceed poll_timeout
        """
        self.base_url = settings.TELEGRAM_API_BASE_URL.format(token=bot_token)
        self.processor = update_processor
        self.poll_timeout = poll_timeout or settings.TELEGRAM_POLL_TIMEOUT
        self.request_timeout = request_timeout or settings.TELEGRAM_POLL_REQUEST_TIMEOUT
        self.session = requests.Session()

        # None means "whatever Telegram still has pending", which is what we
        # want on the first call after a restart. Afterwards the offset
        # acknowledges everything already processed.
        self._offset: int | None = None
        self._backoff: float = self.INITIAL_BACKOFF_SECONDS
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start polling in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Telegram poller is already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run, name="telegram-poller", daemon=True)
        self._thread.start()
        logger.info("Telegram poller started")

    def stop(self) -> None:
        """
        Ask the polling loop to finish.

        Returns immediately; the loop exits once the in-flight long poll
        comes back.
        """
        self._stop_event.set()

    def run(self) -> None:
        """
        Poll until stop() is called.

        Never raises: this runs as the whole body of a thread, and an
        escaping exception would silently leave the bot deaf.
        """
        self._delete_webhook()

        while not self._stop_event.is_set():
            received, updates = self._get_updates()

            if not received:
                self._wait_before_retry()
                continue

            offset_before = self._offset

            for update in updates:
                self._process_update(update)

            # Telegram always sends update_id, but an update arriving without
            # one cannot be acknowledged - and Telegram would hand back the
            # same batch immediately, spinning this loop as fast as the
            # network allows. Treat it like a failed poll.
            if updates and self._offset == offset_before:
                logger.warning(
                    "No update in the batch could be acknowledged - backing off "
                    "to avoid hammering the API"
                )
                self._wait_before_retry()
                continue

            self._backoff = self.INITIAL_BACKOFF_SECONDS

        logger.info("Telegram poller stopped")

    def _delete_webhook(self) -> None:
        """
        Drop any registered webhook before polling.

        Telegram answers getUpdates with 409 Conflict while a webhook is
        registered. Pending updates are kept: they are real user messages
        that arrived while the bot was down.
        """
        try:
            response = self.session.get(
                f"{self.base_url}/deleteWebhook",
                params={"drop_pending_updates": "false"},
                timeout=self.request_timeout,
            )
            if response.status_code == 200:
                logger.info("Webhook deleted - switching to polling")
            else:
                logger.warning(
                    f"deleteWebhook returned HTTP {response.status_code} - "
                    f"getUpdates may be refused with 409 Conflict"
                )
        except requests.RequestException as e:
            logger.warning(
                f"deleteWebhook failed: {e} - getUpdates may be refused with "
                f"409 Conflict if a webhook is still registered"
            )

    def _get_updates(self) -> tuple[bool, list]:
        """
        Fetch the next batch of updates.

        Returns:
            (True, updates) on a successful poll, (False, []) on any failure
        """
        params: dict[str, str | int] = {
            "timeout": self.poll_timeout,
            # Telegram expects a JSON array here, not repeated parameters.
            # callback_query is what a button press arrives as; leave it out
            # and every inline keyboard in the bot silently does nothing.
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if self._offset is not None:
            params["offset"] = self._offset

        try:
            response = self.session.get(
                f"{self.base_url}/getUpdates", params=params, timeout=self.request_timeout
            )
        except requests.RequestException as e:
            logger.warning(f"getUpdates request failed: {e}")
            return False, []

        if response.status_code == 409:
            logger.error(
                "getUpdates was refused with 409 Conflict: another poller or "
                "a webhook is already consuming this bot's updates. Telegram "
                "allows exactly one consumer per bot token - stop the other "
                "instance. Startup removes a registered webhook automatically."
            )
            return False, []

        if response.status_code != 200:
            logger.warning(f"getUpdates returned HTTP {response.status_code}")
            return False, []

        try:
            payload = response.json()
        except ValueError as e:
            logger.warning(f"getUpdates returned a non-JSON body: {e}")
            return False, []

        if not isinstance(payload, dict) or not payload.get("ok"):
            logger.warning(f"getUpdates reported an error: {payload}")
            return False, []

        result = payload.get("result")
        if not isinstance(result, list):
            logger.warning(f"getUpdates returned an unexpected result: {result}")
            return False, []

        return True, result

    def _process_update(self, update: Any) -> None:
        """
        Hand one update to the processor.

        A single bad update must never abort the batch or the loop, so
        everything is contained here.
        """
        try:
            if isinstance(update, dict) and isinstance(update.get("update_id"), int):
                # Acknowledge before processing: an update that makes the
                # processor blow up would otherwise be redelivered forever.
                self._offset = update["update_id"] + 1
            else:
                logger.warning(f"Skipping update without a usable update_id: {update}")
                return

            self.processor.process(update)
        except Exception as e:
            logger.error(f"Error processing update: {e}", exc_info=True)

    def _wait_before_retry(self) -> None:
        """Sleep out the current backoff, waking up early on stop()."""
        logger.warning(f"Retrying getUpdates in {self._backoff:.0f}s")
        self._stop_event.wait(self._backoff)
        self._backoff = min(self._backoff * 2, self.MAX_BACKOFF_SECONDS)
