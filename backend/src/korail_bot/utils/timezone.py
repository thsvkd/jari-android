"""One unambiguous clock for storage, railways, and each user."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from typing import TypeGuard
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RAIL_TIMEZONE_NAME = "Asia/Seoul"
RAIL_TIMEZONE = ZoneInfo(RAIL_TIMEZONE_NAME)
DEFAULT_USER_TIMEZONE = RAIL_TIMEZONE_NAME


def utc_now() -> datetime:
    """The current instant, aware and suitable for storage/comparison."""
    return datetime.now(UTC)


def timezone_of(name: str | None) -> ZoneInfo:
    """Resolve an IANA zone, falling back to the railway's zone."""
    if not isinstance(name, str) or not name or len(name) > 64:
        return RAIL_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return RAIL_TIMEZONE


def is_valid_timezone(name: object) -> TypeGuard[str]:
    """
    Whether a client supplied a usable IANA time-zone name.

    A TypeGuard rather than a plain bool: the first thing this rejects is
    anything that is not a str, so a True answer really does establish the
    type. The zone read back out of Redis is typed `bytes | str | None` and
    is guarded with this before being returned as the `str` it now is - which
    a bool leaves the caller unable to prove.
    """
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def as_utc(moment: datetime, *, naive_zone: tzinfo = UTC) -> datetime:
    """Normalize a moment to UTC; legacy naive values need an explicit origin."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=naive_zone)
    return moment.astimezone(UTC)


def in_user_timezone(moment: datetime, timezone_name: str | None) -> datetime:
    """Render an instant on the user's clock."""
    return as_utc(moment).astimezone(timezone_of(timezone_name))


def zone_suffix(moment: datetime, timezone_name: str | None) -> str:
    """A visible abbreviation and offset, so a clock reading is never bare."""
    local = in_user_timezone(moment, timezone_name)
    name = local.tzname() or timezone_name or DEFAULT_USER_TIMEZONE
    offset = local.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
    return f"{name} (UTC{offset})"


def format_user_datetime(
    moment: datetime,
    timezone_name: str | None,
    pattern: str = "%m월 %d일 %H:%M",
) -> str:
    """Format an instant on the user's clock with an explicit zone."""
    local = in_user_timezone(moment, timezone_name)
    return f"{local:{pattern}} {zone_suffix(moment, timezone_name)}"


def railway_datetime(dep_date: str, clock: str) -> datetime | None:
    """Read a Korail/SR date and clock as an aware Korean railway moment."""
    digits = str(clock or "").replace(":", "")[:6]
    if len(digits) == 4:
        digits += "00"
    try:
        local = datetime.strptime(f"{dep_date}{digits}", "%Y%m%d%H%M%S")
    except (TypeError, ValueError):
        return None
    return local.replace(tzinfo=RAIL_TIMEZONE)


def railway_journey(
    dep_date: str,
    dep_clock: str,
    arr_clock: str,
) -> tuple[datetime, datetime] | None:
    """Departure/arrival instants, including an arrival after midnight."""
    departure = railway_datetime(dep_date, dep_clock)
    arrival = railway_datetime(dep_date, arr_clock)
    if departure is None or arrival is None:
        return None
    if arrival < departure:
        arrival += timedelta(days=1)
    return departure, arrival


def user_local_to_utc(moment: datetime, timezone_name: str | None) -> datetime:
    """Interpret a wall-clock selection in the user's zone and store its instant."""
    if moment.tzinfo is not None:
        return moment.astimezone(UTC)

    zone = timezone_of(timezone_name)
    candidates = [moment.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
    valid = [
        candidate
        for candidate in candidates
        if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == moment
    ]
    if not valid:
        raise ValueError(f"Local time does not exist in {timezone_name}: {moment.isoformat()}")
    if len(valid) == 2 and valid[0].utcoffset() != valid[1].utcoffset():
        raise ValueError(f"Local time is ambiguous in {timezone_name}: {moment.isoformat()}")
    return valid[0].astimezone(UTC)


def format_railway_window(
    dep_date: str,
    dep_clock: str,
    max_clock: str,
    timezone_name: str | None,
) -> str:
    """A search window with railway and user clocks both made explicit."""
    start = railway_datetime(dep_date, dep_clock)
    if start is None:
        return f"{dep_date} {dep_clock[:4]}~{max_clock} (대한민국 철도 시각)"

    rail_start = start.strftime("%m/%d %H:%M")
    local_start = in_user_timezone(start, timezone_name)
    if max_clock == "2400":
        rail = f"{rail_start} 이후 전체 KST"
        local = f"{local_start:%m/%d %H:%M} 이후에 출발하는 한국 열차"
    else:
        end = railway_datetime(dep_date, max_clock)
        if end is None:
            return f"{rail_start}~{max_clock} KST"
        if end <= start:
            end += timedelta(days=1)
        local_end = in_user_timezone(end, timezone_name)
        rail = f"{rail_start}~{end:%H:%M} KST"
        local = f"{local_start:%m/%d %H:%M}~{local_end:%m/%d %H:%M}"

    return f"내 시간: {local} {zone_suffix(start, timezone_name)}\n한국 철도 시각: {rail}"


def format_railway_journey(
    dep_date: str,
    dep_clock: str,
    arr_clock: str,
    timezone_name: str | None,
) -> str | None:
    """One train's local journey, followed by the authoritative KST clocks."""
    journey = railway_journey(dep_date, dep_clock, arr_clock)
    if journey is None:
        return None
    departure, arrival = journey
    local_departure = in_user_timezone(departure, timezone_name)
    local_arrival = in_user_timezone(arrival, timezone_name)
    return (
        f"{local_departure:%m/%d %H:%M}→{local_arrival:%m/%d %H:%M} "
        f"{zone_suffix(departure, timezone_name)} · "
        f"한국 {departure:%m/%d %H:%M}→{arrival:%m/%d %H:%M} KST"
    )
