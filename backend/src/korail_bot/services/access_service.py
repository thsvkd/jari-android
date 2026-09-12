"""Deciding who may run a search, and handling requests to be allowed to."""

from dataclasses import dataclass
from enum import StrEnum

from korail_bot.config.settings import settings
from korail_bot.models import AccessRequest, ApprovedUser
from korail_bot.storage.base import StorageInterface
from korail_bot.utils.crypto import identity_hash
from korail_bot.utils.logger import get_logger
from korail_bot.utils.privacy import mask_phone

logger = get_logger(__name__)


class AccessLevel(StrEnum):
    """Why someone may (or may not) run a search."""

    # The operator's own chat. No limits, no counting.
    DEVELOPER = "developer"
    # Listed in PREAPPROVED_USERS, or approved from the chat.
    APPROVED = "approved"
    # Neither, but has searches left from the trial allowance.
    TRIAL = "trial"
    # Out of trial searches and not approved.
    EXHAUSTED = "exhausted"


@dataclass
class AccessDecision:
    """The answer, with enough context to explain it to the user."""

    level: AccessLevel
    used: int = 0
    limit: int = 0

    @property
    def allowed(self) -> bool:
        """Whether the search may go ahead."""
        return self.level != AccessLevel.EXHAUSTED

    @property
    def counts_against_trial(self) -> bool:
        """Whether starting this search should use up an allowance."""
        return self.level == AccessLevel.TRIAL

    @property
    def remaining(self) -> int:
        """Trial searches left, for the message that mentions them."""
        return max(0, self.limit - self.used)


class AccessService:
    """
    Who is allowed to make this bot talk to Korail.

    A Telegram bot is findable by name and anyone who opens it can talk to it,
    so without a gate the operator's server ends up running searches for
    strangers - and it is the operator's IP that Korail sees hammering it.

    The gate is deliberately not a wall. Someone who finds the bot gets a few
    searches, which is enough to see whether it works for them, and after that
    they can ask. That way sharing with a friend costs the operator one button
    press rather than an .env edit and a restart.

    Everything here hangs off the Korail phone number rather than the chat: a
    new Telegram account is free, so per-chat counting is a limit anyone can
    reset at will. The number itself is never stored - only a keyed hash of it
    (see utils.crypto.identity_hash) plus a masked form for display.
    """

    def __init__(self, storage: StorageInterface):
        """
        Initialize the access service.

        Args:
            storage: Where trials, requests and approvals live
        """
        self.storage = storage

    def evaluate(self, phone_number: str, is_developer: bool = False) -> AccessDecision:
        """
        Decide whether this number may run a search.

        Nothing is written here - asking must not cost an allowance, or a user
        who changed their mind at the summary screen would be charged for it.
        Use consume() when a search actually starts.

        Args:
            phone_number: The Korail login, in any format
            is_developer: Whether the chat is in developer mode

        Returns:
            The decision, and what it was based on
        """
        limit = settings.TRIAL_SEARCH_LIMIT

        if is_developer:
            return AccessDecision(level=AccessLevel.DEVELOPER, limit=limit)

        if settings.is_preapproved(phone_number):
            return AccessDecision(level=AccessLevel.APPROVED, limit=limit)

        phone_hash = identity_hash(phone_number)
        if self.storage.is_approved(phone_hash):
            return AccessDecision(level=AccessLevel.APPROVED, limit=limit)

        # A negative limit means the trial never runs out, which is how an
        # operator turns the gate off entirely.
        used = self.storage.get_trial_count(phone_hash)
        if limit < 0:
            return AccessDecision(level=AccessLevel.TRIAL, used=used, limit=limit)

        if used < limit:
            return AccessDecision(level=AccessLevel.TRIAL, used=used, limit=limit)

        return AccessDecision(level=AccessLevel.EXHAUSTED, used=used, limit=limit)

    def consume(self, phone_number: str, decision: AccessDecision) -> None:
        """
        Charge a search against the trial allowance, when it is one.

        Called at the moment a search actually starts. Not at onboarding, and
        not when the summary is shown: what costs the operator is a process
        asking Korail for seats every few seconds, and that is the thing worth
        counting.
        """
        if not decision.counts_against_trial or decision.limit < 0:
            return

        total = self.storage.increment_trial_count(identity_hash(phone_number))
        logger.info(f"Trial search {total}/{decision.limit} used by {mask_phone(phone_number)}")

    # ==================== Asking to be allowed ====================

    def request_access(self, chat_id: int, phone_number: str) -> AccessRequest | None:
        """
        Record a request to keep using the bot.

        Returns:
            The request, or None when there is already one pending for this
            number - pressing the button twice should not queue twice.
        """
        phone_hash = identity_hash(phone_number)

        if self.storage.get_access_request(phone_hash):
            logger.info(f"Access request from {mask_phone(phone_number)} is already pending")
            return None

        request = AccessRequest(
            phone_hash=phone_hash,
            chat_id=chat_id,
            masked_phone=mask_phone(phone_number),
        )
        self.storage.save_access_request(request)
        logger.info(f"Access requested by {request.masked_phone} (chat_id={chat_id})")
        return request

    def pending_requests(self) -> list[AccessRequest]:
        """Every request waiting on an answer, oldest first."""
        return self.storage.get_all_access_requests()

    def approve(self, phone_hash: str, approved_by: int) -> AccessRequest | None:
        """
        Grant a pending request.

        Returns:
            The request that was granted, so the caller can tell the person
            who made it. None when there is no such request - it expired, or
            another operator got there first.
        """
        request = self.storage.get_access_request(phone_hash)
        if not request:
            return None

        self.storage.save_approved_user(
            ApprovedUser(
                phone_hash=phone_hash,
                masked_phone=request.masked_phone,
                approved_by=approved_by,
            )
        )
        self.storage.delete_access_request(phone_hash)
        logger.info(f"Approved {request.masked_phone} (by chat_id={approved_by})")
        return request

    def reject(self, phone_hash: str) -> AccessRequest | None:
        """
        Turn down a pending request.

        The request is dropped rather than remembered as refused. Someone who
        was turned down once may be worth allowing later - a friend of a
        friend, a second ask with an explanation - and a permanent no would
        need a way to undo it that nobody would ever find.

        Returns:
            The request that was turned down, or None if it was already gone
        """
        request = self.storage.get_access_request(phone_hash)
        if not request:
            return None

        self.storage.delete_access_request(phone_hash)
        logger.info(f"Rejected the access request from {request.masked_phone}")
        return request

    # ==================== Managing who is approved ====================

    def approved_users(self) -> list[ApprovedUser]:
        """Everyone approved from the chat, most recent first."""
        return self.storage.get_all_approved_users()

    def revoke(self, phone_hash: str) -> ApprovedUser | None:
        """
        Withdraw an approval.

        The trial count is left where it is. Someone who was approved and then
        revoked has already used the bot; handing them a fresh allowance would
        make revoking mean less than it says.

        Returns:
            The approval that was withdrawn, or None when there was none
        """
        user = next((u for u in self.approved_users() if u.phone_hash == phone_hash), None)
        if not user:
            return None

        self.storage.delete_approved_user(phone_hash)
        logger.info(f"Revoked approval for {user.masked_phone}")
        return user
