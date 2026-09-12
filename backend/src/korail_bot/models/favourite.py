"""A search someone runs often enough to be worth saving."""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

#: How long a favourite's id is, in hex characters. It travels in
#: callback_data, which Telegram caps at 64 bytes after UTF-8 encoding, and it
#: only has to be unique within one chat's handful of favourites - so this is
#: chosen for brevity and has room to spare against collision either way.
FAVOURITE_ID_BYTES = 4

#: How long a name may be. Names are shown on buttons, and a button whose
#: label wraps to three lines is not a button anyone can read at a glance.
MAX_NAME_LENGTH = 40


def new_favourite_id() -> str:
    """A fresh id for a favourite."""
    return secrets.token_hex(FAVOURITE_ID_BYTES)


@dataclass
class FavouriteSearch:
    """
    Everything the booking flow asks for, except the date.

    The date is deliberately not part of it. A journey someone takes often is
    the same route, the same time of day and the same seat preferences; the
    day is the one answer that is different every time, and a favourite that
    remembered last month's date would be a trap rather than a shortcut.

    The trains to watch are left out for the same kind of reason: that list is
    fetched fresh for whichever date is chosen, and a saved selection would
    name trains that may not run that day.
    """

    chat_id: int
    fav_id: str
    name: str
    src_locate: str
    dst_locate: str
    dep_time: str
    max_dep_time: str
    train_type: str
    train_type_display: str
    special_option: str
    special_option_display: str
    passenger_count: int = 1
    seat_strategy: str = "consecutive"
    seat_strategy_display: str = ""
    # Which seats will do, as SeatPreference.encode wrote it. Defaulted so a
    # favourite saved before this existed reads back as asking for any seat.
    seat_preference: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_train_info(cls, chat_id: int, info: dict, name: str = "") -> "FavouriteSearch":
        """
        Build one out of the answers a session has collected.

        Args:
            chat_id: Telegram chat ID
            info: The session's train_info, as the summary screen sees it
            name: What to call it. Empty means one is derived from the route,
                  which is what saving with a single press does - being made
                  to type a name before a shortcut can be saved would cost
                  more than the shortcut saves.

        Returns:
            The favourite, with an id of its own
        """
        src = info.get("srcLocate", "")
        dst = info.get("dstLocate", "")
        return cls(
            chat_id=chat_id,
            fav_id=new_favourite_id(),
            name=(name.strip() or f"{src} → {dst}")[:MAX_NAME_LENGTH],
            src_locate=src,
            dst_locate=dst,
            dep_time=info.get("depTime", ""),
            max_dep_time=info.get("maxDepTime", ""),
            train_type=info.get("trainType", ""),
            train_type_display=info.get("trainTypeShow", ""),
            special_option=info.get("specialInfo", ""),
            special_option_display=info.get("specialInfoShow", ""),
            passenger_count=int(info.get("passengerCount", 1) or 1),
            seat_strategy=info.get("seatStrategy", "consecutive"),
            seat_strategy_display=info.get("seatStrategyShow", ""),
            seat_preference=info.get("seatPreference", ""),
        )

    def as_train_info(self) -> dict:
        """
        The same answers, in the shape the conversation carries them.

        Everything but the date, which is why a search started from a
        favourite still has one question to answer.
        """
        return {
            "srcLocate": self.src_locate,
            "dstLocate": self.dst_locate,
            "depTime": self.dep_time,
            "maxDepTime": self.max_dep_time,
            "trainType": self.train_type,
            "trainTypeShow": self.train_type_display,
            "specialInfo": self.special_option,
            "specialInfoShow": self.special_option_display,
            "passengerCount": self.passenger_count,
            "seatStrategy": self.seat_strategy,
            "seatStrategyShow": self.seat_strategy_display,
            "seatPreference": self.seat_preference,
        }

    @property
    def window(self) -> str:
        """The time window, as a clock face rather than as Korail sends it."""
        start = self.dep_time[:4] if self.dep_time else "0000"
        end = self.max_dep_time[:4] if self.max_dep_time else "2400"
        return f"{start[:2]}:{start[2:4]}~{end[:2]}:{end[2:4]}"

    @property
    def route(self) -> str:
        """The journey, for a label that has to fit on one line."""
        return f"{self.src_locate} → {self.dst_locate}"
