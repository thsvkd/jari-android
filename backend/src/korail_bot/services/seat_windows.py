"""
How long a freed seat stays free, measured from what a watch already reads.

Every pass of a watch asks Korail for the trains around one departure time,
and the answer carries the seat state of every train in it - not only the ones
the user picked. Remembering that state between passes turns the answer the
search already pays for into a record of when each train's seats opened and
closed, without a single extra request. That record is what decides how long
the search can afford to wait between passes: a seat that stays free for ten
seconds is caught as well at three as at one.

A window is only measured between two passes, so it comes as a range:
``min_s`` from the first pass that saw the seat free to the last, ``max_s``
from the last pass that saw it taken to the first that saw it taken again, and
``passes`` the number of passes that saw it free. A seat free when the watch
started, or still free when it ended, has no known length and is not recorded,
which leans the lengths short - the safe side for choosing an interval.

The state is per train and class ("11" = some seat can be had), not per seat:
a designated-seat watch waits for one seat, but what is measured here is the
class as a whole.

Nothing in here may stop a search: every entry point swallows its own errors.
"""

import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from statistics import median

#: Seat state code both Korail APIs use for "can be reserved".
AVAILABLE = "11"

_FIELDS = {
    "date": ("dep_date", "departure_date"),
    "train": ("train_no",),
    "dep": ("dep_code", "departure_station_code"),
    "arr": ("arr_code", "arrival_station_code"),
}
_CLASSES = {
    "general": ("general_seat", "general_reservation_code"),
    "special": ("special_seat", "special_reservation_code"),
}
_KEY = ("date", "train", "dep", "arr", "passengers", "seat_class")


def _first(train, names) -> str | None:
    for name in names:
        value = getattr(train, name, None)
        if value is not None:
            return str(value).strip()
    return None


def _identity(train) -> tuple | None:
    """(date, train, dep, arr) for either API's train object, or None."""
    values = [_first(train, names) for names in _FIELDS.values()]
    if not all(values):
        return None
    # The current API pads train numbers ("015"); korail2 may not.
    values[1] = values[1].lstrip("0") or "0"
    return tuple(values)


class SeatWindows:
    """Turns successive search answers into open-seat windows."""

    def __init__(self, record: Callable[[dict], None], clock: Callable[[], float] = time.time):
        self._record = record
        self._clock = clock
        #: Tells this watch's windows from another's watching the same train,
        #: so that two sightings of one opening count once and two openings
        #: seen by one watch never merge. Random; nothing about the user.
        self._source = uuid.uuid4().hex[:8]
        #: key -> when the pass that last saw that seat taken ran.
        self._closed_at: dict[tuple, float] = {}
        #: key -> {"before": last taken, "first"/"last": seen free, "passes", "lost"}.
        self._open: dict[tuple, dict] = {}
        #: Train -> when this search last reserved on it.
        self._took_at: dict[tuple, float] = {}

    def observe(self, trains, passengers: int) -> None:
        """Feed one search answer, every train in it, before any filtering."""
        with suppress(Exception):
            now = self._clock()
            for train in trains or ():
                identity = _identity(train)
                if identity is None:
                    continue
                for seat_class, names in _CLASSES.items():
                    state = _first(train, names)
                    if state is None:
                        continue
                    key = (*identity, passengers, seat_class)
                    if state == AVAILABLE:
                        self._saw_open(key, now)
                    else:
                        self._saw_closed(key, identity, now)

    def took(self, train) -> None:
        """This search reserved a seat on `train`."""
        with suppress(Exception):
            identity = _identity(train)
            if identity is not None:
                self._took_at[identity] = self._clock()

    def lost(self, train) -> None:
        """
        A seat the search saw free was gone by the time it asked for it.

        Kept on the open window rather than as an event of its own: a watch
        that is refused pass after pass (a window seat wanted, only aisles
        left) would otherwise log one every pass and push the windows out.
        """
        with suppress(Exception):
            identity = _identity(train)
            for key, window in self._open.items():
                if key[:4] == identity:
                    window["lost"] = True

    def _saw_open(self, key: tuple, now: float) -> None:
        window = self._open.get(key)
        if window is None:
            # No "before" when free since the watch began: never recorded.
            window = self._open[key] = {
                "before": self._closed_at.pop(key, None),
                "first": now,
                "passes": 0,
                "lost": False,
            }
        window["last"] = now
        window["passes"] += 1

    def _saw_closed(self, key: tuple, identity: tuple, now: float) -> None:
        window = self._open.pop(key, None)
        self._closed_at[key] = now
        if window is None or window["before"] is None:
            return
        event = dict(zip(_KEY, key, strict=True))
        event.update(
            kind="window",
            source=self._source,
            closed_before=round(window["before"], 3),
            closed_after=round(now, 3),
            min_s=round(window["last"] - window["first"], 3),
            max_s=round(now - window["before"], 3),
            passes=window["passes"],
            lost=window["lost"],
            # Booked after the last pass that saw it free: this search took
            # the seat that closed it. A class that stayed free past the
            # booking (other class, more seats) saw a later pass and is not.
            ours=self._took_at.get(identity, -1.0) >= window["last"],
        )
        with suppress(Exception):
            self._record(event)


def summarize(events: list[dict], interval: float, intervals=(1.0, 2.0, 3.0)) -> dict:
    """
    What the kept windows say about the wait between passes.

    Sightings of one opening by several watches (same train and class, other
    sources, overlapping ranges) are one window, and a window any of them
    booked is left out: it says how fast this service books, not how long a
    seat lasts.

    Lengths are read in passes, not seconds: a window seen free k times lasted
    about k cycles, and the cycle is the median one, so a pass stretched by a
    backoff or a reservation attempt does not inflate either. What the cycle
    adds over the configured interval is the request's own time, kept when
    asking what another interval would catch: min(1, k * cycle / (every +
    overhead)). Windows seen only once are the ones a longer wait loses first.
    """
    groups: list[list[dict]] = []
    for event in sorted(
        (e for e in events if e.get("kind") == "window"), key=lambda e: e["closed_before"]
    ):
        key = tuple(event[k] for k in _KEY)
        # ponytail: looks back 50 openings, ample for watches of one train a
        # few seconds apart; widen it if many watches share one route.
        for group in reversed(groups[-50:]):
            head = group[0]
            if (
                tuple(head[k] for k in _KEY) == key
                and all(e["source"] != event["source"] for e in group)
                and event["closed_before"] < max(e["closed_after"] for e in group)
            ):
                group.append(event)
                break
        else:
            groups.append([event])

    ours = sum(1 for group in groups if any(e["ours"] for e in group))
    kept = [
        min(group, key=lambda e: e["max_s"] - e["min_s"])
        for group in groups
        if not any(e["ours"] for e in group)
    ]
    lost = sum(1 for group in groups if any(e["lost"] for e in group))
    if not kept:
        return {"windows": 0, "ours": ours, "lost": lost}

    # max_s - min_s spans the pass before the window and the pass after it.
    cycle = median((w["max_s"] - w["min_s"]) / 2 for w in kept)
    overhead = max(0.0, cycle - interval)
    lengths = sorted(w["passes"] * cycle for w in kept)

    def share(test) -> float:
        return round(sum(1 for w in kept if test(w)) / len(kept), 3)

    return {
        "windows": len(kept),
        "ours": ours,
        "lost": lost,
        "cycle_s": round(cycle, 2),
        "seen_once": share(lambda w: w["passes"] == 1),
        "length_s": {
            name: round(lengths[min(len(lengths) - 1, int(q * len(lengths)))], 1)
            for name, q in (("p10", 0.1), ("p50", 0.5), ("p90", 0.9))
        },
        "caught_at_interval": {
            f"{every:g}s": round(
                sum(min(1.0, length / (every + overhead)) for length in lengths) / len(lengths), 3
            )
            for every in intervals
        },
    }
