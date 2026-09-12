"""
Telling the people who registered an SR account that SR is gone.

SR sold the 수서 line as a separate company, with its own app, its own API and
its own membership. On 2026-09-01 that ended: the line was merged into
Korail's, its trains are KTX now, and both the SRT app and the API this bot
booked through were retired.

The registrations outlived the company. They sit in Redis, encrypted, under
keys of their own, belonging to an account that can no longer book anything.
Left alone they would fail silently - a search that finds nothing, or a
/tickets that comes back empty - and the user would have no way to tell that
from bad luck.

So each of them is told once, and the dead registration is dropped. What they
are told depends on what else they have: a chat that also registered with
Korail carries on with that account and needs only to know why the SRT option
disappeared, while a chat that had nothing but SR has to register again, and
may have to sign up as a 통합 회원 first - SR-only memberships did not carry
over on their own.

The key is the state. It is deleted only after the message has gone, so a
Telegram outage costs a retry on the next start rather than the notice.
"""

import threading

from korail_bot.services.telegram_service import TelegramService
from korail_bot.storage.base import StorageInterface
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)


class SrtMigrationNotifier:
    """Tells anyone still holding an SR registration that it is dead."""

    def __init__(self, storage: StorageInterface, telegram_service: TelegramService):
        """
        Initialize the notifier.

        Args:
            storage: Storage interface
            telegram_service: Telegram messaging service
        """
        self.storage = storage
        self.telegram = telegram_service

    def notify_in_background(self) -> threading.Thread | None:
        """
        Do the telling off the startup path.

        One HTTP call per affected chat, at the moment the bot is trying to
        come up. Nobody is waiting on this the way a running search is.

        Returns:
            The thread, or None when nobody is affected
        """
        if not self._pending():
            return None

        thread = threading.Thread(target=self.notify, name="srt-migration", daemon=True)
        thread.start()
        return thread

    def notify(self) -> int:
        """
        Tell everyone still holding an SR registration, once each.

        Returns:
            How many chats were told
        """
        chat_ids = self._pending()
        if not chat_ids:
            return 0

        told = 0
        for chat_id in chat_ids:
            if self._notify_one(chat_id):
                told += 1

        logger.info(f"Told {told}/{len(chat_ids)} chats that their SR registration is retired")
        return told

    def _notify_one(self, chat_id: int) -> bool:
        """
        Tell one chat, and drop its dead registration once told.

        Returns:
            True when the message went and the registration was dropped
        """
        from korail_bot.telegramBot.messages import Messages

        try:
            has_korail = bool(self.storage.get_onboarded_account(chat_id))
        except Exception as e:
            # Which of the two messages to send is unknown, and guessing would
            # tell someone to register an account they already have. Left for
            # the next start.
            logger.warning(f"Could not check the Korail account for chat_id={chat_id}: {e}")
            return False

        message = (
            Messages.SRT_RETIRED_KEEPS_KORAIL if has_korail else Messages.SRT_RETIRED_NO_ACCOUNT
        )

        if not self.telegram.send_message(chat_id, message):
            # Blocked the bot, or Telegram is down. The registration stays, so
            # this is tried again next start - and a chat that blocked the bot
            # is cleared out by the paths that already handle that.
            logger.info(f"Could not tell chat_id={chat_id} about the SR merger; will retry")
            return False

        try:
            self.storage.delete_retired_srt_account(chat_id)
        except Exception as e:
            # Told but not cleared. The cost is one repeated notice on the
            # next start, which is better than dropping credentials the user
            # has not been told about.
            logger.error(f"Could not drop the retired SR registration for chat_id={chat_id}: {e}")
            return True

        return True

    def _pending(self) -> list[int]:
        """Every chat still holding an SR registration."""
        try:
            return self.storage.get_retired_srt_chat_ids()
        except Exception as e:
            # Redis being unreadable at startup is somebody else's problem to
            # report; it is certainly not a reason to message anyone.
            logger.warning(f"Could not list retired SR registrations: {e}")
            return []
