"""Input validation utilities."""

import re
from datetime import datetime

# What a chat is told when its answer to a yes/no question was neither.
#
# validate_yes_no returns this itself. It is named here because the three
# callers that show it cannot prove to a type checker that a None answer came
# with a message - the tuple can hold that combination even though nothing
# produces it - and naming the message beats repeating a literal at each one.
YES_NO_RETRY = "Y/예 또는 N/아니오를 입력해주세요."


class InputValidator:
    """
    Validator for user inputs in the reservation flow.

    Every validate_* returns the reason the input cannot be used, or None when
    it can. There is no separate boolean: a message and a verdict cannot then
    disagree, and callers cannot be handed "invalid, but here is no reason to
    show the user" - which is what they used to have to defend against.

    validate_yes_no is the exception, and returns a tuple. Its answer is not
    the same question as its validity: yes, no, and "that was not an answer"
    are three outcomes, and the third one is re-asked rather than declined.
    """

    @staticmethod
    def normalize_phone_number(phone: str) -> str | None:
        """
        Put a Korean mobile number into the hyphenated form Korail expects.

        People type their number every way imaginable - '01012345678',
        '010 1234 5678', '010.1234.5678'. Korail wants '010-1234-5678', so
        the input is reduced to digits and rebuilt rather than rejected.

        Args:
            phone: Phone number as typed

        Returns:
            Normalized number, or None when it is not a mobile number
        """
        if not phone:
            return None

        # Only separators may be dropped. Stripping anything else would turn
        # a typo into a different, valid-looking number: '010-1234-567a'
        # would silently become '010-123-4567'.
        if not re.fullmatch(r"[0-9\s.\-()+]+", phone.strip()):
            return None

        digits = "".join(character for character in phone if character.isdigit())

        if not digits.startswith("01"):
            return None

        # 010-1234-5678 (11 digits) and the older 011-123-4567 (10 digits)
        if len(digits) == 11:
            return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"

        return None

    @staticmethod
    def validate_phone_number(phone: str) -> str | None:
        """
        Validate a Korean mobile number, in whatever shape it was typed.

        Hyphens are optional: the number is normalized first and only the
        result has to look like a mobile number. Rejecting '01012345678' for
        punctuation would be pedantry, not validation.

        Args:
            phone: Phone number string

        Returns:
            The reason it is not valid, or None when it is
        """
        if not phone or not phone.strip():
            return "전화번호를 입력해주세요."

        phone = phone.strip()

        # Anything that is not a digit or common separator is not a typo.
        if not re.fullmatch(r"[0-9\s.\-()+]+", phone):
            return "전화번호는 숫자로만 입력해주세요. (예: 010-1234-5678)"

        if InputValidator.normalize_phone_number(phone) is None:
            return "올바른 전화번호 형식이 아닙니다. (예: 010-1234-5678)"

        return None

    @staticmethod
    def validate_date(date_str: str) -> str | None:
        """
        Validate date in YYYYMMDD format with enhanced validation.

        Args:
            date_str: Date string

        Returns:
            The reason it is not valid, or None when it is
        """
        if not date_str:
            return "날짜를 입력해주세요."

        # Trim whitespace
        date_str = date_str.strip()

        if not date_str:
            return "날짜를 입력해주세요."

        # Check for non-digit characters
        if not date_str.isdigit():
            return "날짜는 숫자만 입력해주세요. (예: 20250101)"

        # Check length
        if len(date_str) != 8:
            return "날짜는 8자리로 입력해주세요. (예: 20250101)"

        # Check if date is valid
        try:
            year = int(date_str[:4])
            month = int(date_str[4:6])
            day = int(date_str[6:8])

            # Validate year range (reasonable range: 2020-2100)
            if year < 2020 or year > 2100:
                return f"연도가 유효하지 않습니다. (입력: {year}년)"

            # Validate month
            if month < 1 or month > 12:
                return f"월이 유효하지 않습니다. (입력: {month}월)"

            # Validate day
            if day < 1 or day > 31:
                return f"일이 유효하지 않습니다. (입력: {day}일)"

            # Parse date to check if it's valid (e.g., Feb 30 would fail)
            datetime(year, month, day)

        except ValueError:
            return "유효하지 않은 날짜입니다. (예: 2월 30일은 존재하지 않습니다)"

        # Check if date is not in the past
        from korail_bot.utils.timezone import RAIL_TIMEZONE

        rail_now = datetime.now(RAIL_TIMEZONE)
        today = rail_now.strftime("%Y%m%d")
        if date_str < today:
            return "과거 날짜는 선택할 수 없습니다."

        # Check if date is too far in the future (e.g., more than 1 year ahead)
        from datetime import timedelta

        max_future_date = (rail_now + timedelta(days=365)).strftime("%Y%m%d")
        if date_str > max_future_date:
            return "예매 가능한 기간을 초과했습니다. (최대 1년 이내)"

        return None

    @staticmethod
    def validate_time(time_str: str) -> str | None:
        """
        Validate time in HHMM format with enhanced validation.

        Args:
            time_str: Time string

        Returns:
            The reason it is not valid, or None when it is
        """
        if not time_str:
            return "시간을 입력해주세요."

        # Trim whitespace
        time_str = time_str.strip()

        if not time_str:
            return "시간을 입력해주세요."

        # Check for non-digit characters
        if not time_str.isdigit():
            return "시간은 숫자만 입력해주세요. (예: 1430은 14시 30분)"

        # Check length
        if len(time_str) != 4:
            return "시간은 4자리로 입력해주세요. (예: 1430은 14시 30분)"

        try:
            hours = int(time_str[:2])
            minutes = int(time_str[2:4])
        except ValueError:
            return "시간 형식이 올바르지 않습니다. (예: 1430)"

        # Validate hours
        if hours < 0 or hours > 23:
            return f"시간은 00-23 사이여야 합니다. (입력: {hours}시)"

        # Validate minutes
        if minutes < 0 or minutes > 59:
            return f"분은 00-59 사이여야 합니다. (입력: {minutes}분)"

        return None

    @staticmethod
    def validate_station_name(station: str) -> str | None:
        """
        Validate a station name against Korail's station list.

        Args:
            station: Station name

        Returns:
            The reason it is not valid, or None when it is
        """
        # Import here to avoid circular dependency
        from korail_bot.utils.station_codes import (
            format_station_suggestions,
            get_similar_stations,
            is_valid_station,
        )

        if not station:
            return "역 이름을 입력해주세요."

        # Trim whitespace
        station = station.strip()

        if not station:
            return "역 이름을 입력해주세요."

        # Check for '역' suffix
        if "역" in station:
            return "'역'을 제외한 이름을 입력해주세요. (예: 광명)"

        # Check minimum length
        if len(station) < 2:
            return "역 이름이 너무 짧습니다. 최소 2자 이상 입력해주세요."

        # Check maximum length (reasonable limit)
        if len(station) > 10:
            return "역 이름이 너무 깁니다. 올바른 역 이름을 입력해주세요."

        # Check for invalid characters
        if not station.replace(" ", "").replace("-", "").isalnum():
            # Allow Korean, numbers, spaces, hyphens and brackets.
            #
            # Brackets are not decoration: Korail disambiguates four of its
            # own stations with them - 울산(통도사), 진부(오대산), 판교(경기),
            # 판교(충남). Refusing them here rejected the name the station
            # list itself holds, so those four could not be reached at all,
            # and the error blamed the user for a typo they had not made.
            if not all(
                c.isalnum()
                or c
                in [
                    " ",
                    "-",
                    "(",
                    ")",
                    "ㄱ",
                    "ㄴ",
                    "ㄷ",
                    "ㄹ",
                    "ㅁ",
                    "ㅂ",
                    "ㅅ",
                    "ㅇ",
                    "ㅈ",
                    "ㅊ",
                    "ㅋ",
                    "ㅌ",
                    "ㅍ",
                    "ㅎ",
                ]
                for c in station
            ):
                return "역 이름에 특수문자를 사용할 수 없습니다."

        # Check against Korail's station list. It runs to hundreds of names
        # and is fetched and cached rather than shipped with the client, which
        # is what station_codes is for.
        if not is_valid_station(station):
            # Get similar stations for suggestion
            similar = get_similar_stations(station)
            suggestion_text = format_station_suggestions(similar)

            error_msg = f"'{station}'은(는) 존재하지 않는 역입니다.{suggestion_text}"
            return error_msg

        return None

    @staticmethod
    def validate_yes_no(answer: str) -> tuple[bool | None, str | None]:
        """
        Validate yes/no answer with enhanced security.

        Three answers, not two: yes, no, and "that was not an answer". The
        third one has to stay distinct from no, because the caller re-asks on
        it rather than proceeding as if the user had declined.

        Args:
            answer: User's answer

        Returns:
            Tuple of (True, None), (False, None), or (None, error_message)
        """
        if not answer:
            return None, YES_NO_RETRY

        # Trim and convert to uppercase
        answer = answer.strip().upper()

        if not answer:
            return None, YES_NO_RETRY

        # Check length (prevent long inputs)
        if len(answer) > 10:
            return None, f"입력이 너무 깁니다. {YES_NO_RETRY}"

        # Valid positive responses
        if answer in ["Y", "예", "YES", "네", "ㅇ"]:
            return True, None
        # Valid negative responses
        elif answer in ["N", "아니오", "NO", "아니요", "ㄴ"]:
            return False, None
        else:
            return None, YES_NO_RETRY

    @staticmethod
    def validate_train_type_choice(choice: str) -> str | None:
        """
        Validate train type choice (1 or 2) with enhanced validation.

        Args:
            choice: User's choice

        Returns:
            The reason it is not valid, or None when it is
        """
        if not choice:
            return "열차 종류를 선택해주세요. (1: KTX만, 2: 전체)"

        # Trim whitespace
        choice = choice.strip()

        if not choice:
            return "열차 종류를 선택해주세요. (1: KTX만, 2: 전체)"

        # Check if it's a digit
        if not choice.isdigit():
            return "숫자를 입력해주세요. (1 또는 2)"

        # Validate choice
        if choice not in ["1", "2"]:
            return "1 또는 2를 입력해주세요. (1: KTX만, 2: 전체)"

        return None

    @staticmethod
    def validate_special_option_choice(choice: str) -> str | None:
        """
        Validate special seat option choice (1, 2, 3, or 4) with enhanced validation.

        Args:
            choice: User's choice

        Returns:
            The reason it is not valid, or None when it is
        """
        if not choice:
            return "좌석 옵션을 선택해주세요. (1~4)"

        # Trim whitespace
        choice = choice.strip()

        if not choice:
            return "좌석 옵션을 선택해주세요. (1~4)"

        # Check if it's a digit
        if not choice.isdigit():
            return "숫자를 입력해주세요. (1, 2, 3, 또는 4)"

        # Validate choice
        if choice not in ["1", "2", "3", "4"]:
            return "1, 2, 3, 4 중 하나를 선택해주세요."

        return None

    @staticmethod
    def validate_passenger_count(count_str: str) -> str | None:
        """
        Validate passenger count with enhanced validation.

        Args:
            count_str: Passenger count as string

        Returns:
            The reason it is not valid, or None when it is
        """
        if not count_str:
            return "승객 수를 입력해주세요."

        # Trim whitespace
        count_str = count_str.strip()

        if not count_str:
            return "승객 수를 입력해주세요."

        # Check if it's a digit
        if not count_str.isdigit():
            return "승객 수는 숫자만 입력해주세요. (1~9)"

        try:
            count = int(count_str)
        except ValueError:
            return "유효한 숫자를 입력해주세요."

        # Validate range
        if count < 1:
            return "승객 수는 최소 1명 이상이어야 합니다."

        if count > 9:
            return "승객 수는 최대 9명까지 가능합니다."

        return None

    @staticmethod
    def validate_seat_row_range(text: str) -> str | None:
        """
        Validate a typed row range, as "1-15" or a single "7".

        Rows are typed rather than offered as buttons because how many there
        are depends on the carriage, and a keyboard claiming to know would be
        wrong on half the trains.

        Args:
            text: The range as typed

        Returns:
            The reason it cannot be used, or None when it can
        """
        if not text or not text.strip():
            return "좌석 번호 범위를 입력해주세요. (예: 1-15)"

        # Any of the dashes a phone keyboard might produce.
        cleaned = text.strip().replace("–", "-").replace("~", "-")
        parts = [piece.strip() for piece in cleaned.split("-")]

        if len(parts) > 2 or not all(part.isdigit() for part in parts if part):
            return "숫자와 - 만 사용해주세요. (예: 1-15, 또는 7)"

        numbers = [int(part) for part in parts if part]
        if not numbers:
            return "좌석 번호 범위를 입력해주세요. (예: 1-15)"

        if any(number < 1 for number in numbers):
            return "좌석 번호는 1번부터입니다."

        # An upper bound rather than a real one: KTX and SRT carriages run to
        # the teens, and a three-digit row is a typo, not a train.
        if any(number > 99 for number in numbers):
            return "좌석 번호는 99번까지만 지정할 수 있습니다."

        if len(numbers) == 2 and numbers[0] > numbers[1]:
            return "앞 숫자가 뒤 숫자보다 클 수 없습니다. (예: 1-15)"

        return None

    @staticmethod
    def parse_seat_row_range(text: str) -> tuple[int, int]:
        """
        Read a row range that validate_seat_row_range has already accepted.

        Split from the validation so that neither has to carry the other's
        job - the same division every validate_* here follows. Calling it on
        input that has not been validated is a programming error, not a user
        one.

        Args:
            text: The range as typed, known to be valid

        Returns:
            The lowest and highest acceptable row; a bare "7" means 7 to 7
        """
        cleaned = text.strip().replace("–", "-").replace("~", "-")
        numbers = [int(part) for part in (p.strip() for p in cleaned.split("-")) if part]
        if len(numbers) == 1:
            return numbers[0], numbers[0]
        return numbers[0], numbers[1]

    @staticmethod
    def validate_seat_strategy_choice(choice: str) -> str | None:
        """
        Validate seat strategy choice (1 or 2).

        Args:
            choice: User's choice

        Returns:
            The reason it is not valid, or None when it is
        """
        if not choice:
            return "좌석 배치 방식을 선택해주세요. (1: 연속 좌석, 2: 랜덤 배치)"

        # Trim whitespace
        choice = choice.strip()

        if not choice:
            return "좌석 배치 방식을 선택해주세요. (1: 연속 좌석, 2: 랜덤 배치)"

        # Check if it's a digit
        if not choice.isdigit():
            return "숫자를 입력해주세요. (1 또는 2)"

        # Validate choice
        if choice not in ["1", "2"]:
            return "1 또는 2를 입력해주세요. (1: 연속 좌석, 2: 랜덤 배치)"

        return None

    @staticmethod
    def validate_password(password: str) -> str | None:
        """
        Validate password input with basic security checks.

        Args:
            password: Password string

        Returns:
            The reason it is not valid, or None when it is
        """
        if not password:
            return "비밀번호를 입력해주세요."

        # Check minimum length
        if len(password) < 4:
            return "비밀번호가 너무 짧습니다."

        # Check maximum length (reasonable limit)
        if len(password) > 50:
            return "비밀번호가 너무 깁니다."

        # No pattern blocklist here on purpose. This value is only ever
        # encrypted and posted to Korail's login endpoint - it reaches neither
        # SQL nor a page - so screening it for 'SELECT' or 'DROP' cannot
        # prevent an injection. It only rejects real passwords: anything
        # containing 'drop' ("Raindrop2024") was refused as malicious.
        if any(character in password for character in ("\n", "\r", "\t")):
            return "비밀번호에 줄바꿈이나 탭을 포함할 수 없습니다."

        return None
