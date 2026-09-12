"""
Telling the users when the bot they use has been updated.

A self-hosted bot is updated by whoever runs the server, and the people using
it find out by noticing that something moved. Buttons appear, a message reads
differently, a command they were told about is not there yet. This closes that
gap: when the running version differs from the one Redis says was last
announced, everyone who uses the bot is told once.

Once, and only on a change. A notice that arrives on every restart is not an
announcement, it is a symptom.
"""

import html
import threading

from korail_bot import __version__
from korail_bot.release_notes import notes_for
from korail_bot.services.telegram_service import TelegramService
from korail_bot.storage.base import StorageInterface
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)


def _escape(text: str) -> str:
    """
    Make a note safe to put inside the announcement's markup.

    quote=False because these land in text, not in an attribute: escaping the
    quotes there would turn every "직접 입력" in a release note into &quot;,
    which is correct HTML and needless noise in the payload.
    """
    return html.escape(text, quote=False)


class ReleaseAnnouncer:
    """Announces a version change to everyone who uses the bot."""

    #: How the announcement is sent. The only message this bot marks up, and
    #: only because a fold has no other spelling in the Bot API.
    PARSE_MODE = "HTML"

    def __init__(
        self,
        storage: StorageInterface,
        telegram_service: TelegramService,
        version: str = __version__,
    ):
        """
        Initialize the announcer.

        Args:
            storage: Storage interface
            telegram_service: Telegram messaging service
            version: The version now running. Injectable so a test does not
                have to reinstall the package to have an opinion about it.
        """
        self.storage = storage
        self.telegram = telegram_service
        self.version = version

    def announce_in_background(self) -> threading.Thread | None:
        """
        Do the announcing off the startup path.

        One HTTP call per user, at the moment the bot is trying to come up.
        The searches this app manages are more urgent than the news that it
        was updated, so the news waits on its own thread.

        Returns:
            The thread, or None when there is nothing to announce
        """
        if not self._is_due():
            return None

        thread = threading.Thread(target=self.announce, name="release-announcer", daemon=True)
        thread.start()
        return thread

    def announce(self) -> int:
        """
        Tell everyone, once.

        Returns:
            How many chats were told
        """
        if not self._is_due():
            return 0

        previous = self._last_announced()

        # Written before the sending rather than after. A crash halfway
        # through costs the rest of the audience one notice; the other order
        # would re-announce the same version to everyone on every restart
        # until a full pass succeeded, which is the worse failure by far - a
        # missed update notice is forgettable, a repeating one is not.
        try:
            self.storage.set_announced_version(self.version)
        except Exception as e:
            logger.error(f"Could not record the announced version: {e}")
            return 0

        audience = self.audience()
        if not audience:
            logger.info(f"Updated to v{self.version}; nobody to tell yet")
            return 0

        message = self.message()
        sent = self.telegram.send_to_multiple(audience, message, parse_mode=self.PARSE_MODE)
        logger.info(
            f"Announced v{self.version} (from {previous or 'an unknown version'}) "
            f"to {sent}/{len(audience)} chats"
        )
        return sent

    def audience(self) -> list[int]:
        """
        Everyone who uses this bot.

        Three overlapping sources, because none alone is the answer.
        Registered accounts are the users proper, and outlive their sessions
        by months. Sessions catch anyone mid-conversation who has not
        registered yet. Developer chats are usually both, and are the one
        audience that must never be missed - the operator wants to know their
        own deploy landed.

        Order is preserved and duplicates dropped, so the operator hears
        first and nobody hears twice.
        """
        found: list[int] = []
        for source in (self._developers, self._registered, self._in_conversation):
            try:
                found.extend(source())
            except Exception as e:
                # One unreadable source must not cost the whole announcement.
                logger.warning(f"Could not read an audience for the release notice: {e}")

        return list(dict.fromkeys(found))

    def message(self) -> str:
        """
        The announcement, with release notes when the release has any.

        Everything interpolated is escaped: these lines are written by hand
        in release_notes.py, and one stray < in a hurried entry would take
        the whole announcement down rather than showing up wrong.
        """
        from korail_bot.telegramBot.messages import Messages

        notes = notes_for(self.version)
        version = _escape(self.version)

        if not notes:
            return Messages.UPDATED.format(version=version)

        headline = _escape(notes.headline)
        if not notes.detail.strip():
            # No fold rather than an empty one: a box that opens onto nothing
            # is worse than no box.
            return Messages.UPDATED_HEADLINE_ONLY.format(version=version, headline=headline)

        return Messages.UPDATED_WITH_NOTES.format(
            version=version, headline=headline, detail=_escape(notes.detail)
        )

    def _is_due(self) -> bool:
        """
        Whether this version still owes its users an announcement.

        A deployment with nothing recorded is treated as an update rather than
        as a fresh install. It is the honest reading for the common case - the
        feature shipped into a bot that already had users - and the case it
        gets wrong, a Redis wiped by /flushredis, has no audience left to
        bother: the wipe took the sessions and the registrations with it.
        """
        try:
            return self._last_announced() != self.version
        except Exception as e:
            # Redis being unreadable at startup is somebody else's problem to
            # report; it is certainly not a reason to message everyone.
            logger.warning(f"Could not read the announced version: {e}")
            return False

    def _last_announced(self) -> str | None:
        return self.storage.get_announced_version()

    def _developers(self) -> list[int]:
        return self.storage.get_all_developers()

    def _registered(self) -> list[int]:
        return self.storage.get_all_onboarded_chat_ids()

    def _in_conversation(self) -> list[int]:
        return [session.chat_id for session in self.storage.get_all_user_sessions()]
