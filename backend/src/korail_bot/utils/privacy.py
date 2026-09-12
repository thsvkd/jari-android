"""Helpers for keeping personal data out of logs and messages."""

import re
from collections.abc import Iterable

_PHONE_PATTERN = re.compile(r"^(01[0-9])-(\d{3,4})-(\d{4})$")


def mask_phone(phone: str | None) -> str:
    """
    Mask the middle block of a Korean mobile number.

    Korail IDs are phone numbers, so they show up in status messages,
    subscriber notifications and logs. Only the tail is kept, which is
    enough for an operator to tell two users apart.

    Args:
        phone: Phone number such as '010-1234-5678'

    Returns:
        Masked number such as '010-****-5678', or a generic placeholder
        when the input is empty or not a phone number.
    """
    if not phone:
        return "(알 수 없음)"

    match = _PHONE_PATTERN.match(phone.strip())
    if not match:
        # Not a phone number - keep only the first two characters.
        return phone[:2] + "***"

    prefix, middle, suffix = match.groups()
    return f"{prefix}-{'*' * len(middle)}-{suffix}"


def mask_phones(phones: Iterable[str | None]) -> list[str]:
    """Mask every number in an iterable."""
    return [mask_phone(phone) for phone in phones]
