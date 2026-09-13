"""Korail API service wrapper.

Only the Korail-specific half lives here - logging in, asking korail2 for
trains, and reserving one. The search loop that drives those calls is in
:mod:`korail_bot.services.rail_service`, shared with SR.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests
from korail2 import AdultPassenger, NoResultsError, ReserveOption, SoldOutError, TrainType
from korail2 import Korail as K2MKorail

# Not re-exported by the package, and there is no other way to reach the
# cancellation endpoint: the client's own cancel() cannot be called (see
# cancel_reservation below). TicketReservation is wrapped the same way, to
# slip a seat-location code into a payload korail2 otherwise hard-codes.
from korail2.korail2 import KORAIL_CANCEL, KORAIL_MOBILE, KORAIL_TICKETRESERVATION
from korail_mobile_api import (
    KorailClient,
    KorailConfig,
    KorailPassengerCounts,
    KorailReservationJobType,
    KorailSeatAssignment,
    KorailSeatClass,
    MutationConsent,
    SeatInventoryResponse,
    TrainSearchQuery,
)
from korail_mobile_api.constants import KORAIL_STANDBY_WAIT_FLAG
from korail_mobile_api.errors import KorailAppError

from korail_bot.config.settings import settings
from korail_bot.models import ReservationOutcome, SeatPreference, SeatTarget
from korail_bot.services.rail_service import (
    DuplicateReservationError,
    RailService,
    SearchProgress,
    SearchUnavailableError,
)
from korail_bot.utils.logger import get_logger
from korail_bot.utils.privacy import mask_phone

logger = get_logger(__name__)

# PNR-keyed unpaid seat detail. korail2 never calls this; ReservationView
# (which it does call) lists bookings without the seats. letskorail and srtgo
# both follow the listing with this one, and that is where h_seat_no lives
# before anything is paid for.
KORAIL_RESERVATION_DETAIL = f"{KORAIL_MOBILE}.certification.ReservationList"

# KTX 일반실 is 2+2: A and D at the windows, B and C on the aisle. A mixed
# set cannot be expressed in Korail's one location slot, so it stays "any"
# and the post-hoc matcher is what honours it.
_WINDOW_COLUMNS = frozenset({"A", "D"})
_AISLE_COLUMNS = frozenset({"B", "C"})
_NO_REMAINING_SEATS_CODE = "ERI411321"

# Re-exported: these used to be defined here, and half the codebase imports
# them from this module. Moving them to rail_service.py is not a reason to
# make every caller say so.
__all__ = [
    "DuplicateReservationError",
    "KorailService",
    "SearchProgress",
    "SearchUnavailableError",
    "location_seat_att_cd",
]


def location_seat_att_cd(preference: SeatPreference | None) -> str:
    """
    Korail's txtSeatAttCd3 value for a column set.

    012 창측, 013 내측, 000 무관. Row ranges have no attribute and are
    honoured after the fact, the way SR honours the whole condition.
    """
    if preference is None or not preference.columns:
        return "000"
    columns = set(preference.columns)
    if columns <= _WINDOW_COLUMNS:
        return "012"
    if columns <= _AISLE_COLUMNS:
        return "013"
    return "000"


@contextmanager
def _ticket_reservation_seat_att(session, att_cd: str):
    """
    Slip txtSeatAttCd3 onto korail2's TicketReservation GET without copying
    the rest of the payload. korail2 hard-codes 000; wrapping the session
    for the one call is the same seam cancel_reservation already uses.
    """
    if att_cd == "000":
        yield
        return

    original_get = session.get

    def get(url, *args, **kwargs):
        params = kwargs.get("params")
        if url == KORAIL_TICKETRESERVATION and isinstance(params, dict):
            kwargs = {**kwargs, "params": {**params, "txtSeatAttCd3": att_cd}}
        return original_get(url, *args, **kwargs)

    session.get = get
    try:
        yield
    finally:
        session.get = original_get


class KorailService(RailService):
    """Service for interacting with Korail API."""

    operator_name = "코레일"

    def __init__(self, *args, **kwargs):
        """
        Initialize Korail service.

        Takes the same arguments as :class:`RailService`.
        """
        super().__init__(*args, **kwargs)
        self._korail_instance: K2MKorail | None = None
        self._modern_client: KorailClient | None = None
        self._modern_seat_context: tuple[int, str, int] | None = None
        self._seat_layout_references: dict[tuple[int, str, int], object] = {}

        # Log class methods to verify correct version is loaded
        logger.debug(
            f"KorailService initialized with methods: {[m for m in dir(self) if not m.startswith('_')]}"
        )

    @property
    def default_train_type(self) -> TrainType:
        return TrainType.KTX

    @property
    def default_reserve_option(self) -> ReserveOption:
        return ReserveOption.GENERAL_FIRST

    def _build_client(self, username: str, password: str) -> K2MKorail:
        """
        Build a Korail client that belongs to this service alone.

        korail2 keeps its requests.Session on the class, so out of the box
        every client in a process shares one cookie jar - two users answering
        the password prompt at the same time would be handing each other their
        Korail session. Each client gets a session of its own here, carrying
        over the User-Agent the library set.

        Args:
            username: Korail username
            password: Korail password

        Returns:
            A client that has not logged in yet
        """
        client = K2MKorail(username, password, auto_login=False)

        user_agent = client._session.headers.get("User-Agent")
        client._session = requests.Session()
        if user_agent:
            client._session.headers.update({"User-Agent": user_agent})

        if settings.KORAIL_APP_VERSION:
            client._version = settings.KORAIL_APP_VERSION

        if self._app_session_start:
            # Every request carries when the app was started. A search the
            # bot restarted is the same session continuing, so it keeps the
            # timestamp it began with rather than announcing a fresh launch.
            client._engine.app_start_ts = self._app_session_start

        return client

    def _build_modern_client(self) -> KorailClient:
        """Build the current Korail mobile client with DynaPath enabled."""
        config_options = {"enable_dynapath": True}
        if settings.KORAIL_APP_VERSION:
            config_options["version"] = settings.KORAIL_APP_VERSION
        return KorailClient(KorailConfig(**config_options))

    def _activate_legacy_session(self, modern: KorailClient, session, username: str, password: str) -> None:
        """Give the existing search/reservation adapter a freshly authenticated session."""
        legacy = self._build_client(username, password)
        legacy._session.cookies.set("JSESSIONID", session.jsessionid)
        legacy._version = modern.config.version
        legacy._key = session.raw.get("Key") or modern.config.key
        legacy.membership_number = session.member_card_no
        legacy.logined = True

        def current_auth(url: str):
            path = urlsplit(url).path
            method = "GET" if url == KORAIL_TICKETRESERVATION else "POST"
            return modern.http._dynapath_headers(method, path), None

        legacy._get_auth_headers_and_sid = current_auth
        self._korail_instance = legacy

    def _login_with_current_api(self, username: str, password: str) -> bool:
        candidate = self._build_modern_client()
        try:
            digits = "".join(character for character in username if character.isdigit())
            if "@" in username:
                input_flag = "5"
            elif digits.startswith("01") and len(digits) in {10, 11}:
                input_flag = "4"
            else:
                input_flag = "2"
            session = candidate.login(username, password, input_flag=input_flag)
            self._activate_legacy_session(candidate, session, username, password)
        except Exception as exc:
            candidate.close()
            self._korail_instance = None
            self._logged_in = False
            logger.warning(
                "Korail login failed for user %s (%s: %s)",
                mask_phone(username),
                type(exc).__name__,
                exc,
            )
            return False

        previous = self._modern_client
        self._modern_client = candidate
        self._modern_seat_context = None
        if previous is not None:
            previous.close()
        self._logged_in = True
        return True

    def login(self, username: str, password: str) -> bool:
        """
        Login to Korail with credentials.

        Args:
            username: Korail username (phone number in format 010-xxxx-xxxx)
            password: Korail password

        Returns:
            True if login successful, False otherwise
        """
        self._logged_in = self._login_with_current_api(username, password)
        if self._logged_in:
            self._username = username
            self._password = password
            self._schedule_next_relogin()
            logger.info("Korail login successful for user: %s", mask_phone(username))
        return self._logged_in

    def _relogin(self) -> bool:
        """Attempt to re-login with stored credentials after session expiry."""
        if not self._username or not self._password:
            logger.error("🔒 Cannot re-login: no stored credentials")
            return False

        logger.debug("🔄 Session expired, attempting re-login...")
        try:
            self._logged_in = self._login_with_current_api(self._username, self._password)
            if self._logged_in:
                self._relogin_count += 1
                logger.debug(f"✅ Re-login successful (total: {self._relogin_count})")
            else:
                logger.error("❌ Re-login failed")
            return self._logged_in
        except Exception as e:
            logger.error(f"❌ Re-login error: {e}")
            self._logged_in = False
            return False
        finally:
            # Whether or not it worked. A failed refresh that left the
            # deadline in the past would try again on every pass of the search
            # loop, which is a login attempt every couple of seconds; the
            # session is renewed on demand anyway when Korail rejects it.
            self._schedule_next_relogin()

    def search_waitlist_trains(
        self,
        *,
        dep_date: str,
        src_locate: str,
        dst_locate: str,
        dep_time: str,
        max_dep_time: str,
        train_type: TrainType,
        passenger_count: int,
    ) -> list:
        """List trains with the same current API used to submit standby."""
        client = self._modern_client
        if not self._logged_in or client is None:
            raise ValueError("Must login before searching trains")

        result = client.search_trains(
            TrainSearchQuery(
                departure_station_code=src_locate,
                arrival_station_code=dst_locate,
                departure_date=dep_date,
                departure_time=dep_time,
                passengers=passenger_count,
            )
        )
        trains = list(result.trains)
        if train_type == TrainType.KTX:
            trains = [train for train in trains if train.train_group_name == "KTX"]
        if max_dep_time != "2400":
            trains = [
                train
                for train in trains
                if str(train.departure_time or "")[:4].isdigit()
                and int(str(train.departure_time)[:4]) < int(max_dep_time)
            ]
        return trains

    def search_selectable_trains(self, **kwargs) -> list:
        """Search using the same current API whose train objects seat reads require."""
        return self.search_waitlist_trains(**kwargs)

    @staticmethod
    def _seat_class(seat_class: str) -> KorailSeatClass:
        try:
            return {
                "general": KorailSeatClass.GENERAL,
                "special": KorailSeatClass.SPECIAL,
            }[seat_class]
        except KeyError as exc:
            raise ValueError("좌석 등급은 일반실 또는 특실이어야 합니다.") from exc

    def seat_cars(self, train, seat_class: str, passenger_count: int = 1):
        """Read cars for a current train result and cabin class."""
        client = self._modern_client
        if not self._logged_in or client is None:
            raise ValueError("좌석을 조회하려면 먼저 로그인해 주세요.")
        cabin = self._seat_class(seat_class)
        key = (id(train), cabin.value, passenger_count)
        try:
            response = client.get_seat_cars(
                train,
                passenger_count=passenger_count,
                room_class_code=cabin.value,
            )
            layout_train = train
            self._seat_layout_references.pop(key, None)
        except KorailAppError as exc:
            if not self._is_no_remaining_seats(exc):
                raise
            layout_train, response = self._nearby_layout_reference(
                train, cabin.value, passenger_count
            )
            self._seat_layout_references[key] = layout_train
        self._modern_seat_context = (id(layout_train), cabin.value, passenger_count)
        return response

    def seat_layout_is_reference(
        self, train, seat_class: str, passenger_count: int = 1
    ) -> bool:
        """Whether selection uses the same train number on a nearby date."""
        cabin = self._seat_class(seat_class)
        return (id(train), cabin.value, passenger_count) in self._seat_layout_references

    def seat_inventory(
        self,
        train,
        car_no: int,
        seat_class: str,
        passenger_count: int = 1,
        *,
        allow_layout_reference: bool = False,
    ):
        """Read the current sellability and layout for one car."""
        client = self._modern_client
        if not self._logged_in or client is None:
            raise ValueError("좌석을 조회하려면 먼저 로그인해 주세요.")
        cabin = self._seat_class(seat_class)
        key = (id(train), cabin.value, passenger_count)
        layout_train = self._seat_layout_references.get(key, train)
        context = (id(layout_train), cabin.value, passenger_count)
        # TResidualSeatsResearch depends on server-side context created by
        # ScheduleView. Mobile HTTP requests log in again independently, so a
        # seat-detail call must restore that context after every fresh login.
        if self._modern_seat_context != context:
            try:
                client.get_seat_cars(
                    layout_train,
                    passenger_count=passenger_count,
                    room_class_code=cabin.value,
                )
            except KorailAppError as exc:
                if layout_train is train and self._is_no_remaining_seats(exc):
                    if not allow_layout_reference:
                        return SeatInventoryResponse(car_no=car_no)
                    layout_train, _ = self._nearby_layout_reference(
                        train, cabin.value, passenger_count
                    )
                    self._seat_layout_references[key] = layout_train
                    context = (id(layout_train), cabin.value, passenger_count)
                else:
                    raise
            self._modern_seat_context = context
        return client.get_seat_inventory(
            layout_train,
            car_no,
            passenger_count=passenger_count,
            room_class_code=cabin.value,
        )

    @staticmethod
    def _is_no_remaining_seats(exc: KorailAppError) -> bool:
        return (
            getattr(exc, "code", None) == _NO_REMAINING_SEATS_CODE
            or "잔여석이 없습니다" in str(getattr(exc, "message", "") or "")
        )

    def _nearby_layout_reference(
        self, train, room_class_code: str, passenger_count: int
    ):
        """Read the same scheduled train's formation from a nearby service date."""
        client = self._modern_client
        if client is None:
            raise ValueError("좌석을 조회하려면 먼저 로그인해 주세요.")
        try:
            base_date = datetime.strptime(str(train.departure_date), "%Y%m%d")
        except (TypeError, ValueError) as exc:
            raise ValueError("열차 운행일을 확인할 수 없습니다.") from exc
        # The nearest day can itself be almost sold out and expose only one
        # bookable car. Start at the far edge of this short window so the UI
        # can offer a useful formation while still matching the train number.
        for offset in range(7, 0, -1):
            result = client.search_trains(
                TrainSearchQuery(
                    departure_station_code=str(train.departure_station_name or ""),
                    arrival_station_code=str(train.arrival_station_name or ""),
                    departure_date=(base_date + timedelta(days=offset)).strftime("%Y%m%d"),
                    departure_time=str(train.departure_time or "000000"),
                    passengers=passenger_count,
                )
            )
            reference = next(
                (
                    item
                    for item in result.trains
                    if item.train_no == train.train_no
                    and (
                        not train.train_class_code
                        or not item.train_class_code
                        or item.train_class_code == train.train_class_code
                    )
                ),
                None,
            )
            if reference is None:
                continue
            try:
                response = client.get_seat_cars(
                    reference,
                    passenger_count=passenger_count,
                    room_class_code=room_class_code,
                )
            except KorailAppError as exc:
                if self._is_no_remaining_seats(exc):
                    continue
                raise
            if response.cars:
                return reference, response
        raise ValueError("가까운 운행일에서도 이 열차의 좌석 편성을 확인할 수 없습니다.")

    def reserve_designated(
        self,
        train,
        inventory,
        targets: list[SeatTarget],
        *,
        passenger_count: int,
        seat_class: str,
    ):
        """Reserve only sellable wire seats from the freshly read inventory."""
        client = self._modern_client
        if not self._logged_in or client is None:
            raise ValueError("좌석을 예약하려면 먼저 로그인해 주세요.")
        if passenger_count < 1 or len(targets) != passenger_count:
            raise ValueError("선택한 좌석 수와 승객 수가 같아야 합니다.")
        if inventory.car_no is None or any(target.car_no != inventory.car_no for target in targets):
            raise ValueError("한 번의 지정 예약에서는 같은 호차의 좌석만 선택할 수 있습니다.")

        physical_by_number = {seat.seat_no: seat for seat in inventory.seats}
        assignments = []
        for target in targets:
            physical = physical_by_number.get(target.seat_no)
            if physical is None or physical.sale_possible != "Y":
                raise ValueError("선택한 좌석 중 현재 판매 가능한 좌석이 없습니다.")
            if physical.specification != target.label:
                raise ValueError("좌석 정보가 바뀌었습니다. 좌석표를 다시 불러와 주세요.")
            assignments.append(KorailSeatAssignment.from_inventory(inventory, physical))

        return client.reserve(
            train,
            consent=MutationConsent(allow_reserve=True, dry_run=False),
            passengers=KorailPassengerCounts(adult=passenger_count),
            seat_class=self._seat_class(seat_class),
            job_type=KorailReservationJobType.SEAT_DESIGNATED,
            seats=assignments,
        )

    def release_unpaid_hold(self, hold):
        """Release a modern unpaid hold when the app cannot safely persist it."""
        client = self._modern_client
        if not self._logged_in or client is None:
            raise ValueError("예약을 취소하려면 먼저 로그인해 주세요.")
        return client.cancel_unpaid_hold(
            hold,
            consent=MutationConsent(allow_cancel=True, dry_run=False),
        )

    @staticmethod
    def describe_waitlist_train(train) -> dict:
        """Reduce a current-API train to the mobile train-list contract."""
        dep_time = str(getattr(train, "departure_time", "") or "")
        arr_time = str(getattr(train, "arrival_time", "") or "")
        name = (
            getattr(train, "train_class_name", None)
            or getattr(train, "train_group_name", None)
            or "열차"
        )
        return {
            "no": str(getattr(train, "train_no", "") or ""),
            "label": f"{RailService._clock(dep_time)}→{RailService._clock(arr_time)} {name}",
            "dep_time": dep_time,
            "arr_time": arr_time,
            "name": name,
            "soldout": not any(
                getattr(train, field, None) == "11"
                for field in ("general_reservation_code", "special_reservation_code")
            ),
            "waitlistEligible": getattr(train, "wait_reservation_flag", None)
            == KORAIL_STANDBY_WAIT_FLAG,
        }

    def search_trains(
        self,
        dep_date: str,
        src_locate: str,
        dst_locate: str,
        dep_time: str = "000000",
        max_dep_time: str = "2400",
        train_type: TrainType = TrainType.KTX,
        passenger_count: int = 1,
        verbose: bool = True,
        include_no_seats: bool = False,
        train_numbers: list[str] | None = None,
    ) -> list:
        """
        Search for available trains.

        Args:
            dep_date: Departure date (YYYYMMDD)
            src_locate: Source station name (without '역')
            dst_locate: Destination station name (without '역')
            dep_time: Departure time (HHMMSS)
            max_dep_time: Maximum departure time threshold (HHMM)
            train_type: Type of train to search for
            passenger_count: Number of adult passengers
            include_no_seats: Return sold-out trains too. Off for the search
                              loop, which only wants what it can reserve; on
                              for showing the user what runs in the window,
                              where the sold-out ones are the whole point.
            train_numbers: Keep only these Korail train numbers. None or empty
                           means every train in the window.

        Returns:
            List of available trains

        Raises:
            ValueError: If not logged in
        """
        if not self._logged_in or not self._korail_instance:
            raise ValueError("Must login before searching trains")

        if train_type is None:
            train_type = self.default_train_type

        try:
            # Create passenger list
            passengers = [AdultPassenger(passenger_count)]

            if verbose:
                logger.debug("🔍 Searching trains with parameters:")
                logger.debug(f"  dep_date: {dep_date} (type: {type(dep_date).__name__})")
                logger.debug(f"  src_locate: '{src_locate}' (type: {type(src_locate).__name__})")
                logger.debug(f"  dst_locate: '{dst_locate}' (type: {type(dst_locate).__name__})")
                logger.debug(f"  dep_time: {dep_time} (type: {type(dep_time).__name__})")
                logger.debug(f"  train_type: {train_type}")
                logger.debug(f"  passengers: {passengers} (count: {passenger_count})")
                logger.debug(f"  max_dep_time: {max_dep_time}")

            trains = self._korail_instance.search_train(
                src_locate,
                dst_locate,
                dep_date,
                dep_time,
                train_type=train_type,
                passengers=passengers,
                include_no_seats=include_no_seats,
            )

            if verbose:
                logger.debug(f"📋 Korail API returned {len(trains) if trains else 0} trains")

                # Log each train found with seat availability
                if trains:
                    for i, train in enumerate(trains, 1):
                        train_str = str(train)
                        logger.debug(f"  Train #{i}: {train_str}")

                        if hasattr(train, "seat_available"):
                            logger.debug(f"    Seats available: {train.seat_available}")
                        if hasattr(train, "general_seat"):
                            logger.debug(f"    General seats: {train.general_seat}")
                        if hasattr(train, "special_seat"):
                            logger.debug(f"    Special seats: {train.special_seat}")

            # Filter by max departure time
            if trains and max_dep_time != "2400":
                filtered_trains = []
                max_time = int(max_dep_time)

                if verbose:
                    logger.debug(f"🔧 Applying max_dep_time filter: {max_dep_time}")

                for train in trains:
                    dep_time_int = self._extract_departure_time(train)
                    if dep_time_int > 0 and dep_time_int < max_time:
                        filtered_trains.append(train)
                        if verbose:
                            logger.debug(f"  ✅ Kept: {dep_time_int} < {max_time}")
                    else:
                        if verbose:
                            logger.debug(f"  ❌ Filtered out: {dep_time_int} >= {max_time}")

                trains = filtered_trains
                if verbose:
                    logger.debug(f"📊 After filtering: {len(trains)} trains remain")

            # Narrow to the trains the user picked, if they picked any.
            #
            # Applied here rather than in the loops so both seat strategies get
            # it from one place, and so a train that stops running mid-search
            # simply stops appearing rather than needing to be noticed.
            if trains and train_numbers:
                wanted = set(train_numbers)
                trains = [train for train in trains if train.train_no in wanted]
                if verbose:
                    logger.debug(
                        f"🎯 Watching {len(wanted)} chosen train(s): {len(trains)} of them "
                        f"are in this result"
                    )

            if verbose:
                logger.debug(
                    f"✅ Search complete: {len(trains)} trains available "
                    f"({src_locate}→{dst_locate} on {dep_date})"
                )
            return trains

        except NoResultsError:
            if verbose:
                logger.debug("No trains found for search criteria (NoResultsError)")
            return []
        except Exception as e:
            if type(e).__name__ == "NeedToLoginError":
                logger.debug(f"🔒 Session expired during search, re-logging in: {e}")
                if self._relogin():
                    return []  # Will retry on next loop iteration
                else:
                    raise
            # Anything else means the request did not get an answer we
            # understand. Raised rather than returned as an empty list: the
            # caller cannot act on what it cannot distinguish from a sold-out
            # train, and this used to be swallowed here, leaving a search that
            # had stopped working reporting itself as still looking.
            raise SearchUnavailableError(f"{type(e).__name__}: {e}") from e

    def reserve_train(
        self,
        train,
        option: ReserveOption = ReserveOption.GENERAL_FIRST,
        passenger_count: int = 1,
        seat_preference: SeatPreference | None = None,
    ):
        """
        Attempt to reserve a specific train.

        Args:
            train: Train object from search_trains()
            option: Reservation option (special seat preference)
            passenger_count: Number of adult passengers
            seat_preference: Column/row set to ask for. Window/aisle columns
                become txtSeatAttCd3; the rest is checked after the booking
                exists, see assigned_seats.

        Returns:
            Reservation object if successful, None otherwise
            Returns "DUPLICATE" string if duplicate reservation detected
        """
        if not self._logged_in or not self._korail_instance:
            raise ValueError("Must login before reserving")

        if option is None:
            option = self.default_reserve_option

        try:
            # Create passenger list
            passengers = [AdultPassenger(passenger_count)]
            att_cd = location_seat_att_cd(seat_preference)

            logger.debug("🎫 Attempting reservation:")
            logger.debug(f"  Train: {train}")
            logger.debug(f"  Option: {option}")
            logger.debug(f"  Passengers: {passenger_count}")
            logger.debug(f"  Seat att cd3: {att_cd}")

            with _ticket_reservation_seat_att(self._korail_instance._session, att_cd):
                reservation = self._korail_instance.reserve(
                    train, passengers=passengers, option=option
                )

            if reservation:
                logger.info("🎉 RESERVATION SUCCESS!")
                logger.info(f"  Reservation details: {reservation}")
                if hasattr(reservation, "rsv_id"):
                    logger.info(f"  Reservation ID: {reservation.rsv_id}")
                self._attach_assigned_seats(reservation)
                return reservation
            else:
                logger.debug("Reservation returned None (no seats available)")
                return None

        except SoldOutError:
            logger.debug(f"Train sold out during reservation attempt: {train}")
            return None
        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__

            # Check for duplicate reservation error
            if "동일한 예약 내역" in error_msg or "WRR800029" in error_msg:
                # Return special value instead of raising exception
                logger.warning("⚠️ Duplicate reservation detected - will continue searching")
                logger.warning(f"  Error: {error_msg}")
                return "DUPLICATE"

            if error_type == "NeedToLoginError":
                logger.debug(f"🔒 Session expired during reservation, re-logging in: {error_msg}")
                if self._relogin():
                    return None  # Will retry on next loop iteration
                else:
                    raise

            logger.error(f"❌ Reservation error ({error_type}): {error_msg}")
            logger.error(f"  Train: {train}")
            logger.error(f"  Option: {option}")
            logger.error("  Full traceback:", exc_info=True)
            return None

    def request_waitlist(
        self,
        *,
        dep_date: str,
        src_locate: str,
        dst_locate: str,
        dep_time: str,
        train_no: str,
        passenger_count: int = 1,
    ) -> dict[str, str]:
        """Apply for Korail's official standby list for one eligible train."""
        client = self._modern_client
        if not self._logged_in or client is None:
            raise ValueError("코레일에 로그인한 뒤 예약 대기를 신청해 주세요.")

        result = client.search_trains(
            TrainSearchQuery(
                departure_station_code=src_locate,
                arrival_station_code=dst_locate,
                departure_date=dep_date,
                departure_time=dep_time,
                passengers=passenger_count,
            )
        )
        train = next((item for item in result.trains if str(item.train_no) == str(train_no)), None)
        if train is None:
            raise ValueError("선택한 열차를 다시 찾지 못했어요. 목록을 새로 조회해 주세요.")
        if str(getattr(train, "wait_reservation_flag", "") or "") != " 9":
            raise ValueError("선택한 열차는 현재 코레일 예약 대기 대상이 아니에요.")

        reserve_consent = MutationConsent(allow_reserve=True, dry_run=False)
        hold = client.reserve(
            train,
            consent=reserve_consent,
            passengers=KorailPassengerCounts(adult=passenger_count),
            seat_class=KorailSeatClass.GENERAL,
            job_type=KorailReservationJobType.STANDBY,
        )
        try:
            client.confirm_standby_hold(
                hold,
                consent=reserve_consent,
                allow_seat_class_change=False,
                sms_notify=False,
                phone_no=None,
            )
        except Exception:
            try:
                client.cancel_unpaid_hold(
                    hold,
                    consent=MutationConsent(allow_cancel=True, dry_run=False),
                )
            except Exception as cancel_error:
                logger.error(
                    "Could not cancel incomplete Korail standby hold (%s)",
                    type(cancel_error).__name__,
                )
            raise

        logger.info("Korail standby registration completed for train %s", train_no)
        return {"train_no": str(train_no)}

    # ==================== Payment, observed rather than performed ====================
    #
    # The bot reserves; the user pays. Nothing here pays for anything, and
    # korail2 could not if it wanted to - it has no payment call at all. What
    # it does have is the list of reservations still waiting to be paid for,
    # which is enough to tell whether a payment happened without ever taking
    # part in one.

    def is_reservation_outstanding(self, rsv_id: str) -> bool | None:
        """
        Whether a reservation is still sitting unpaid.

        Korail lists a reservation until it is paid for, cancelled, or left
        to expire, at which point it drops off. Read-only, and the only way
        the bot can know a payment happened: until now "payment complete"
        meant the user had sent any message at all, which is a claim rather
        than a fact.

        Args:
            rsv_id: The reservation number to look for

        Returns:
            True while it is still unpaid, False once it is gone, and None
            when Korail could not be asked - which is not the same as gone,
            and must not be read as one.
        """
        if not self._logged_in or not self._korail_instance:
            return None

        try:
            reservations = self._korail_instance.reservations()
        except NoResultsError:
            # No reservations at all. An ordinary answer, not a failure.
            return False
        except Exception as e:
            logger.warning(f"Could not check whether {rsv_id} is still unpaid: {e}")
            return None

        return any(str(getattr(r, "rsv_id", "")) == str(rsv_id) for r in reservations)

    def reservation_outcome(
        self,
        rsv_id: str,
        *,
        train_no: str = "",
        dep_date: str = "",
        dep_time: str = "",
    ) -> ReservationOutcome:
        """
        Tell a Korail reservation that was paid for from one that was let go.

        Both leave the unpaid list, and for a while the bot read either as a
        payment - so a user who cancelled in the Korail app was congratulated
        on a booking they no longer had. The second list is what separates
        them: `tickets()` holds what was actually issued, so a reservation
        that vanished with a ticket behind it was paid for and one that
        vanished with nothing was not.

        The awkward part is that a Korail ticket carries no reservation
        number. There is nothing to match on but the journey, so the match is
        train number + running date, narrowed by departure time when the
        record has one.

        **That leaves a real ambiguity, and it is worth stating plainly.** A
        user who already held a ticket on the same train on the same day -
        bought separately, or booked for somebody else - and then cancelled
        this reservation will be told the payment went through, because the
        ticket that matches is the older one. The trade is deliberate: the
        alternative is never saying PAID at all, and the ordinary case is
        someone paying for the seat this bot just took for them.

        Args:
            rsv_id: The reservation number to ask about
            train_no: The train the seat is on; required for the ticket match
            dep_date: That train's running date (YYYYMMDD); also required
            dep_time: That train's departure time (HHMMSS); narrows the match
                      when present, ignored when not

        Returns:
            OUTSTANDING while it is still on the unpaid list, PAID when a
            matching ticket was issued, RELEASED when it is gone and no
            ticket matches, and UNKNOWN whenever Korail could not be asked or
            there is nothing to match on.
        """
        if not self._logged_in or not self._korail_instance:
            return ReservationOutcome.UNKNOWN

        try:
            reservations = self._korail_instance.reservations()
        except NoResultsError:
            # No unpaid reservations at all. An ordinary answer: this one is
            # off the list, and the ticket list decides what that meant.
            reservations = []
        except Exception as e:
            logger.warning(f"Could not check whether {rsv_id} is still unpaid: {e}")
            return ReservationOutcome.UNKNOWN

        if any(str(getattr(r, "rsv_id", "")) == str(rsv_id) for r in reservations):
            return ReservationOutcome.OUTSTANDING

        # Gone from the unpaid list. Without a train to look for there is no
        # way to ask the second question, and a guess here is the thing being
        # removed - so nothing is said.
        if not train_no or not dep_date:
            logger.info(
                f"Reservation {rsv_id} is off the unpaid list, but the record carries no "
                f"train to match a ticket against - saying nothing"
            )
            return ReservationOutcome.UNKNOWN

        try:
            tickets = self._korail_instance.tickets()
        except NoResultsError:
            # No tickets at all, so certainly none for this journey.
            return ReservationOutcome.RELEASED
        except Exception as e:
            logger.warning(f"Could not check whether {rsv_id} was issued as a ticket: {e}")
            return ReservationOutcome.UNKNOWN

        for ticket in tickets:
            if str(getattr(ticket, "train_no", "")) != str(train_no):
                continue
            if str(getattr(ticket, "dep_date", "")) != str(dep_date):
                continue
            if dep_time and str(getattr(ticket, "dep_time", ""))[:4] != str(dep_time)[:4]:
                continue
            logger.info(f"Reservation {rsv_id} was issued as a ticket on train {train_no}")
            return ReservationOutcome.PAID

        logger.info(f"Reservation {rsv_id} went without a ticket - cancelled or expired")
        return ReservationOutcome.RELEASED

    def cancel_reservation(self, rsv_id: str) -> bool:
        """
        Give one unpaid reservation back to Korail.

        korail2's own cancel() is not used, and cannot be: it sends the GET
        with its parameters in the body, which Korail ignores, so the call
        raises JSONDecodeError without having cancelled anything. The same
        values sent as query parameters are accepted - that is what this does,
        and it is the whole of the difference. See docs/payment-automation-poc.md.

        The reservation is looked up rather than taken on trust, because the
        cancellation needs the journey numbers that only the listing carries -
        and because a number that is not on the list is not outstanding, which
        is worth finding out before telling anyone it was cancelled.

        Args:
            rsv_id: The reservation number to give back

        Returns:
            True when Korail confirmed it. False for every other outcome,
            including a reservation that was not there to cancel.
        """
        if not self._logged_in or not self._korail_instance:
            logger.warning(f"Not logged in - cannot cancel {rsv_id}")
            return False

        korail = self._korail_instance
        try:
            reservations = korail.reservations()
        except NoResultsError:
            reservations = []
        except Exception as e:
            logger.error(f"Could not list reservations to cancel {rsv_id}: {e}")
            return False

        target = next(
            (r for r in reservations if str(getattr(r, "rsv_id", "")) == str(rsv_id)), None
        )
        if target is None:
            logger.warning(f"Reservation {rsv_id} is not outstanding - nothing to cancel")
            return False

        try:
            response = korail._session.get(
                KORAIL_CANCEL,
                params={
                    "Device": korail._device,
                    "Version": korail._version,
                    "Key": korail._key,
                    "txtPnrNo": target.rsv_id,
                    "txtJrnySqno": target.journey_no,
                    "txtJrnyCnt": target.journey_cnt,
                    "hidRsvChgNo": target.rsv_chg_no,
                },
            )
            cancelled = bool(korail._result_check(response.json()))
        except Exception as e:
            # Includes KorailError, which is how the client reports a refusal.
            logger.error(f"Korail refused to cancel {rsv_id}: {type(e).__name__}: {e}")
            return False

        logger.info(f"Cancelled reservation {rsv_id}")
        return cancelled

    def issued_tickets(self) -> list[dict] | None:
        """
        The tickets Korail has already issued to this account.

        Read-only, and deliberately so. cancel_reservation above only ever
        finds its target in the *unpaid* reservation list, so it could not
        refund one of these even if asked - and refunding is a fee-bearing,
        irreversible money operation that this bot does not perform. The
        ticket is shown; the button stays in 코레일톡.

        Returns:
            One dict per ticket, or None when the question could not be asked.
            An empty list means Korail was asked and answered "none", which is
            not the same thing.
        """
        if not self._logged_in or not self._korail_instance:
            logger.warning("Not logged in - cannot list issued tickets")
            return None

        try:
            tickets = self._korail_instance.tickets()
        except NoResultsError:
            # Korail says "no tickets" by raising. That is an answer.
            return []
        except Exception as e:
            logger.error(f"Could not list issued Korail tickets: {type(e).__name__}: {e}")
            return None

        return [self._describe_ticket(ticket) for ticket in tickets or []]

    @staticmethod
    def _describe_ticket(ticket) -> dict:
        """One issued Korail ticket, flattened to strings for a message."""
        train_no = str(getattr(ticket, "train_no", "") or "")
        name = getattr(ticket, "train_type_name", None) or "열차"
        dep = getattr(ticket, "dep_name", None) or ""
        arr = getattr(ticket, "arr_name", None) or ""
        dep_time = str(getattr(ticket, "dep_time", "") or "")
        arr_time = str(getattr(ticket, "arr_time", "") or "")

        car_no = str(getattr(ticket, "car_no", "") or "")
        seat_no = str(getattr(ticket, "seat_no", "") or "")
        seat_count = getattr(ticket, "seat_no_count", None)
        seats = " ".join(part for part in (f"{car_no}호차" if car_no else "", seat_no) if part)
        if seats and seat_count and int(seat_count) > 1:
            seats = f"{seats} 외 {int(seat_count) - 1}석"

        return {
            # Korail files no reservation number against an issued ticket -
            # rsv_id belongs to the booking, which is gone once it is paid
            # for. The ticket number is what 코레일톡 asks for instead.
            "reservation_id": KorailService._ticket_number(ticket),
            "train": f"[{name} {train_no}] {dep}({RailService._clock(dep_time)})"
            f"→{arr}({RailService._clock(arr_time)})",
            "dep_date": str(getattr(ticket, "dep_date", "") or ""),
            "dep_time": dep_time,
            "seats": seats,
            "operator": KorailService.operator_name,
        }

    @staticmethod
    def _ticket_number(ticket) -> str:
        """
        The 승차권 번호 korail2 assembles from the four purchase fields.

        Empty when any of them is missing: get_ticket_no() joins them
        unconditionally, so a half-filled ticket yields a string of "None"s
        that would be worse than saying nothing.
        """
        parts = [
            getattr(ticket, name, None)
            for name in ("sale_info1", "sale_info2", "sale_info3", "sale_info4")
        ]
        if not all(parts):
            return ""
        try:
            return str(ticket.get_ticket_no())
        except Exception as e:
            logger.debug(f"Could not read a Korail ticket number: {type(e).__name__}: {e}")
            return ""

    # Partial reservations from a random-seating run are still not cancelled
    # here. Doing so is a decision about a search that is already going wrong -
    # which of the seats already taken to give back, and whether giving them
    # back beats leaving the user something to pay for - and it is not this
    # method's to make. The inherited no-op logs what it would have released.

    @staticmethod
    def reservation_id(reservation) -> str | None:
        """The reservation number, as korail2 names it."""
        rsv_id = getattr(reservation, "rsv_id", None) or getattr(reservation, "pnr_no", None)
        return str(rsv_id) if rsv_id else None

    @staticmethod
    def payment_due(reservation) -> tuple[str | None, str | None]:
        """When Korail stops holding this seat, as korail2 names it."""
        return (
            getattr(reservation, "buy_limit_date", None)
            or getattr(reservation, "payment_deadline_date", None),
            getattr(reservation, "buy_limit_time", None)
            or getattr(reservation, "payment_deadline_time", None),
        )

    @staticmethod
    def describe_train(train) -> dict:
        """Reduce a korail2 train to what the keyboard and the summary need."""
        dep_time = getattr(train, "dep_time", None)
        arr_time = getattr(train, "arr_time", None)
        name = getattr(train, "train_type_name", None) or "열차"
        return {
            "no": str(getattr(train, "train_no", "") or ""),
            "label": (f"{RailService._clock(dep_time)}→{RailService._clock(arr_time)} {name}"),
            "dep_time": str(dep_time or ""),
            "arr_time": str(arr_time or ""),
            "name": name,
            "soldout": not (hasattr(train, "has_seat") and train.has_seat()),
            "waitlistEligible": bool(
                hasattr(train, "has_general_waiting_list") and train.has_general_waiting_list()
            ),
        }

    @staticmethod
    def assigned_seats(reservation) -> list[str]:
        """
        The seats Korail gave this unpaid booking, labelled as "3A".

        korail2's Reservation object does not parse them. The listing it
        already calls (ReservationView) does not carry them either. A second
        call, certification.ReservationList keyed by PNR, does - the same
        route letskorail and srtgo use, and the one this attaches at reserve
        time. Empty means that call did not say, which keeps_seat treats as
        "cannot tell" rather than cancelling.
        """
        seats = getattr(reservation, "assigned_seats", None)
        if not isinstance(seats, (list, tuple)):
            return []
        return [str(seat) for seat in seats if seat]

    def _attach_assigned_seats(self, reservation) -> None:
        """Fill reservation.assigned_seats from the unpaid detail call."""
        rsv_id = self.reservation_id(reservation)
        if not rsv_id:
            return
        try:
            reservation.assigned_seats = self._unpaid_seat_labels(rsv_id)
        except Exception as e:
            logger.warning(f"Could not attach unpaid seats for {rsv_id}: {type(e).__name__}: {e}")

    def _unpaid_seat_labels(self, rsv_id: str) -> list[str]:
        """
        Ask Korail which seats an unpaid PNR was given.

        Read-only. Failures come back as an empty list so a booking is never
        thrown away because the detail call hiccupped.
        """
        korail = self._korail_instance
        if korail is None:
            return []

        try:
            response = korail._session.get(
                KORAIL_RESERVATION_DETAIL,
                params={
                    "Device": korail._device,
                    "Version": korail._version,
                    "Key": korail._key,
                    "hidPnrNo": rsv_id,
                },
            )
            payload = response.json()
        except Exception as e:
            logger.warning(f"Could not fetch unpaid seats for {rsv_id}: {type(e).__name__}: {e}")
            return []

        if not isinstance(payload, dict):
            return []

        try:
            if not korail._result_check(payload):
                return []
        except NoResultsError:
            return []
        except Exception as e:
            logger.warning(
                f"Korail refused unpaid seat detail for {rsv_id}: {type(e).__name__}: {e}"
            )
            return []

        labels: list[str] = []
        for journey in payload.get("jrny_infos", {}).get("jrny_info") or []:
            if not isinstance(journey, dict):
                continue
            for seat in journey.get("seat_infos", {}).get("seat_info") or []:
                if not isinstance(seat, dict):
                    continue
                label = str(seat.get("h_seat_no") or "").strip()
                if label:
                    labels.append(label)
        if not labels:
            logger.debug(
                f"ReservationList for {rsv_id} carried no h_seat_no (keys={list(payload.keys())})"
            )
        return labels

    def _extract_departure_time(self, train) -> int:
        """
        Extract departure time from train object as HHMM integer.

        Args:
            train: Train object from korail2

        Returns:
            Departure time as integer (e.g., 944 for 09:44), 0 if extraction fails
        """
        try:
            # str(train) format: "[KTX] 4월 8일, 용산~광주송정(09:44~12:50), ..."
            # Use rsplit to handle station names with parentheses e.g. 울산(통도사)~서울(09:44~12:50)
            train_str = str(train)
            time_part = train_str.rsplit("(", 1)[1].split("~")[0]  # "09:44"
            time_str = "".join(time_part.split(":"))  # "0944"
            return int(time_str)
        except (IndexError, ValueError) as e:
            logger.error(f"Failed to extract departure time from train: {train}, error: {e}")
            return 0
