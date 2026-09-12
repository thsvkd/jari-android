"""Small formatters shared by the messages the bot sends."""


def format_duration(seconds: float) -> str:
    """
    A span of time, the way someone would say it out loud.

    Coarse on purpose. A search that has been running for three hours is
    reported as "3시간 12분", not to the second: the number is there to give a
    sense of how long the wait has been, and a ticking seconds field would
    only invite the reader to watch it.

    Args:
        seconds: How long, in seconds. Negative is treated as none at all,
            which is what a clock read out of order should look like.

    Returns:
        Something like "3시간 12분", "12분", or "1분 미만"
    """
    total_minutes = int(max(0.0, seconds) // 60)
    hours, minutes = divmod(total_minutes, 60)

    if hours and minutes:
        return f"{hours}시간 {minutes}분"
    if hours:
        return f"{hours}시간"
    if minutes:
        return f"{minutes}분"
    return "1분 미만"


def format_uptime(seconds: float) -> str:
    """
    How long the bot has been up, the way someone would say it out loud.

    Separate from format_duration because the two spans are of different
    sizes. A search runs for an afternoon; a deployment left alone runs for
    weeks, and "412시간" is a number the reader has to divide themselves.
    Under a day there is nothing to add, so it defers.

    Args:
        seconds: How long, in seconds

    Returns:
        Something like "17일 3시간", "3일", or whatever format_duration says
    """
    days, remainder = divmod(int(max(0.0, seconds)), 86400)

    if not days:
        return format_duration(seconds)

    hours = remainder // 3600
    return f"{days}일 {hours}시간" if hours else f"{days}일"
