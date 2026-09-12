"""
When this process started, for the one command that reports it.

/version answers "how long has the bot been up", and nothing else in the bot
knew. The clock has to be read on the way up rather than when the question is
first asked, or the first person to ask would be told the bot started just now
- which is the one answer that looks right and never is.

So app.py stamps it, and this module holds the stamp. A process that never
stamped one - a test, a search subprocess, anything importing the handler
without starting the app - has no uptime to report rather than a made-up one,
which is why the reader hands back None instead of zero.
"""

from datetime import datetime

#: Set once by mark_start(). None until then, and it stays None in any process
#: that is not the app.
_started_at: datetime | None = None


def mark_start(now: datetime | None = None) -> datetime:
    """
    Record that the process starts now.

    Called from app.py, before the services are built: the number people read
    is how long the bot has been up, not how long it has been finished
    starting.

    Args:
        now: The moment to record. Injectable so a test does not have to wait.

    Returns:
        The moment recorded
    """
    global _started_at
    _started_at = now or datetime.now()
    return _started_at


def started_at() -> datetime | None:
    """When this process started, or None if nothing stamped it."""
    return _started_at


def uptime_seconds(now: datetime | None = None) -> float | None:
    """
    How long this process has been running, in seconds.

    Args:
        now: The moment to measure against. Injectable for tests.

    Returns:
        The span in seconds, or None when the start was never recorded. Never
        negative: a clock that moved backwards should read as "just started",
        not as a bot that will start later today.
    """
    if _started_at is None:
        return None
    return max(0.0, ((now or datetime.now()) - _started_at).total_seconds())
