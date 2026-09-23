"""
e2e 스택 전용 가짜 코레일.

서버와 예약 워커(별도 프로세스)가 모두 시작할 때 sitecustomize 로 이 모듈을 불러
KorailService 의 네트워크 메서드만 바꿔요. 조회·예약 루프, 결제 감시, 저장, API 는
전부 실제 코드가 돌아요. 돌려주는 객체도 손으로 지은 것이 아니라 korail2·
korail_mobile_api 의 실제 클래스를 코레일 응답 필드로 만든 것이라, 라이브러리가
읽는 속성 이름이 틀리면 여기서도 똑같이 드러나요.

시나리오는 가짜 Redis 의 한 키(JSON)에 있어서 서버·워커가 같이 보고, 스택의 제어
경로가 테스트마다 바꿔요.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import redis
from korail2 import TrainType
from korail2.korail2 import Reservation, Train
from korail_mobile_api.models import (
    PhysicalSeat,
    SeatCar,
    SeatCarListResponse,
    SeatInventoryResponse,
    TrainSummary,
)
from korail_mobile_api.mutation_models import ReservationHoldResponse

KST = ZoneInfo("Asia/Seoul")
SCENARIO_KEY = "e2e:scenario"
LOG_KEY = "e2e:log"
COUNTER_KEY = "e2e:counter"

# 한 번의 조회가 돌려줄 열차. 시각은 조회한 출발 시각부터의 분, 좌석 코드는 코레일 것 그대로
# ("11" 예약 가능, "13" 매진, "00" 없음), wait 은 예약대기 가능(9) 여부예요.
DEFAULT_TRAINS = [
    {
        "no": "00101",
        "name": "KTX",
        "group": "KTX",
        "offset": 0,
        "minutes": 161,
        "general": "11",
        "special": "11",
        "wait": -1,
    },
    {
        "no": "00103",
        "name": "KTX",
        "group": "KTX",
        "offset": 60,
        "minutes": 158,
        "general": "13",
        "special": "13",
        "wait": 9,
    },
    {
        "no": "00105",
        "name": "KTX-산천",
        "group": "KTX",
        "offset": 120,
        "minutes": 165,
        "general": "13",
        "special": "11",
        "wait": -1,
    },
    {
        "no": "00107",
        "name": "KTX",
        "group": "KTX",
        "offset": 180,
        "minutes": 160,
        "general": "11",
        "special": "13",
        "wait": -1,
    },
]

DEFAULT_SCENARIO = {
    # 코레일이 이 계정을 받아 주는지
    "login": True,
    # 예약 워커의 조회 결과: seats(몇 번 헛돈 뒤 빈자리) | sold_out(계속 매진) | unavailable(조회 실패)
    "search": "seats",
    "seat_after_polls": 2,
    # 결제 확인이 돌려줄 답: OUTSTANDING | PAID | RELEASED | UNKNOWN
    "outcome": "OUTSTANDING",
    "deadline_minutes": 10,
    # 좌석표에서 팔린 좌석(호차별 좌석 표시 목록). 없으면 짝수 줄 D 석만 팔린 것으로 둬요.
    "sold_seats": None,
    "trains": DEFAULT_TRAINS,
}


def _client() -> redis.Redis:
    url = os.environ["MOBILE_REDIS_URL"]
    return redis.Redis.from_url(url, decode_responses=True)


def scenario() -> dict:
    raw = _client().get(SCENARIO_KEY)
    return {**DEFAULT_SCENARIO, **(json.loads(raw) if raw else {})}


def _log(event: str, **fields) -> None:
    """무엇이 코레일에 갔는지 남겨요. 통합 테스트가 '실제로 호출됐는지'를 여기서 확인해요."""
    _client().rpush(LOG_KEY, json.dumps({"event": event, **fields}, ensure_ascii=False))


def _next_id(prefix: str) -> str:
    return f"{prefix}{_client().incr(COUNTER_KEY):08d}"


def _clock(base: str, plus_minutes: int) -> tuple[str, str]:
    """HHMM(SS) 에 분을 더한 (날짜 넘김 일수, HHMMSS)."""
    start = datetime.strptime(base[:4].ljust(4, "0"), "%H%M")
    moment = start + timedelta(minutes=plus_minutes)
    return str((moment.date() - start.date()).days), moment.strftime("%H%M%S")


def _schedule(dep_date: str, dep_time: str) -> list[dict]:
    rows = []
    for spec in scenario()["trains"]:
        _, dep = _clock(dep_time, spec["offset"])
        _, arr = _clock(dep_time, spec["offset"] + spec["minutes"])
        rows.append({**spec, "dep_date": dep_date, "dep": dep, "arr": arr})
    return rows


def _deadline() -> tuple[str, str]:
    moment = datetime.now(KST) + timedelta(minutes=scenario()["deadline_minutes"])
    return moment.strftime("%Y%m%d"), moment.strftime("%H%M%S")


def _korail2_train(row: dict, src: str, dst: str) -> Train:
    return Train(
        {
            "h_trn_clsf_cd": "100",
            "h_trn_clsf_nm": row["name"],
            "h_trn_gp_cd": "100",
            "h_trn_no": row["no"],
            "h_dpt_rs_stn_nm": src,
            "h_dpt_rs_stn_cd": "0001",
            "h_dpt_dt": row["dep_date"],
            "h_dpt_tm": row["dep"],
            "h_arv_rs_stn_nm": dst,
            "h_arv_rs_stn_cd": "0020",
            "h_arv_dt": row["dep_date"],
            "h_arv_tm": row["arr"],
            "h_run_dt": row["dep_date"],
            "h_rsv_psb_flg": "Y" if "11" in (row["general"], row["special"]) else "N",
            "h_rsv_psb_nm": "예약가능",
            "h_spe_rsv_cd": row["special"],
            "h_gen_rsv_cd": row["general"],
            "h_wait_rsv_flg": str(row["wait"]),
        }
    )


class FakeKorail:
    """KorailService 에 덮어쓸 메서드 모음. self 는 실제 KorailService 인스턴스예요."""

    def _login_with_current_api(self, username: str, password: str) -> bool:
        accepted = bool(scenario()["login"])
        _log("login", username=username, accepted=accepted)
        return accepted

    def close(self) -> None:
        self._logged_in = False

    def search_waitlist_trains(
        self,
        *,
        dep_date,
        src_locate,
        dst_locate,
        dep_time,
        max_dep_time,
        train_type,
        passenger_count,
    ) -> list:
        _log("list_trains", dep_date=dep_date, src=src_locate, dst=dst_locate)
        return [
            TrainSummary(
                train_no=row["no"],
                departure_date=row["dep_date"],
                departure_time=row["dep"],
                arrival_time=row["arr"],
                departure_station_name=src_locate,
                arrival_station_name=dst_locate,
                run_date=row["dep_date"],
                train_class_name=row["name"],
                train_group_name=row["group"],
                train_class_code="00",
                general_reservation_code=row["general"],
                special_reservation_code=row["special"],
                general_availability_name="예약가능" if row["general"] == "11" else "매진",
                special_availability_name="예약가능" if row["special"] == "11" else "매진",
                wait_reservation_flag=" 9" if row["wait"] == 9 else "-1",
            )
            for row in _schedule(dep_date, dep_time)
            # 실제 search_waitlist_trains 와 같은 거르기: KTX 계열만, 마감 시각은 미만.
            if (train_type != TrainType.KTX or row["group"] == "KTX")
            and (max_dep_time == "2400" or int(row["dep"][:4]) < int(max_dep_time))
        ]

    def search_trains(
        self,
        dep_date,
        src_locate,
        dst_locate,
        dep_time="000000",
        max_dep_time="2400",
        train_type=None,
        passenger_count=1,
        verbose=True,
        include_no_seats=False,
        train_numbers=None,
    ) -> list:
        from korail_bot.services.rail_service import SearchUnavailableError

        state = scenario()
        polls = getattr(self, "_e2e_polls", 0) + 1
        self._e2e_polls = polls
        _log("search", polls=polls, mode=state["search"])
        if state["search"] == "unavailable":
            raise SearchUnavailableError("가짜 코레일: 조회 실패")
        if state["search"] == "sold_out" or polls <= state["seat_after_polls"]:
            return []
        rows = [
            row
            for row in _schedule(dep_date, dep_time)
            # 실제 코레일 필터처럼 번호를 그대로 비교해요(앞의 0 을 떼면 다른 번호예요).
            if not train_numbers or row["no"] in train_numbers
        ]
        open_rows = [dict(row, general="11") for row in rows[:1]]
        return [_korail2_train(row, src_locate, dst_locate) for row in open_rows]

    def reserve_train(self, train, option=None, passenger_count=1, seat_preference=None):
        limit_date, limit_time = _deadline()
        rsv_id = _next_id("E2E")
        _log("reserve_train", rsv_id=rsv_id, train_no=train.train_no, passengers=passenger_count)
        return Reservation(
            {
                "h_trn_clsf_cd": train.train_type,
                "h_trn_clsf_nm": train.train_type_name,
                "h_trn_gp_cd": train.train_group,
                "h_trn_no": train.train_no,
                "h_dpt_rs_stn_nm": train.dep_name,
                "h_dpt_rs_stn_cd": train.dep_code,
                "h_dpt_tm": train.dep_time,
                "h_arv_rs_stn_nm": train.arr_name,
                "h_arv_rs_stn_cd": train.arr_code,
                "h_arv_tm": train.arr_time,
                "h_run_dt": train.dep_date,
                "h_rsv_psb_flg": "Y",
                "h_spe_rsv_cd": train.special_seat,
                "h_gen_rsv_cd": train.general_seat,
                "h_wait_rsv_flg": "-1",
                "h_pnr_no": rsv_id,
                "h_tot_seat_cnt": f"{passenger_count:03d}",
                "h_ntisu_lmt_dt": limit_date,
                "h_ntisu_lmt_tm": limit_time,
                "h_rsv_amt": f"{59800 * passenger_count:08d}",
            }
        )

    def seat_cars(self, train, seat_class, passenger_count=1, *, allow_layout_reference=True):
        name = "일반실" if seat_class == "general" else "특실"
        numbers = (1, 2, 3) if seat_class == "general" else (4,)
        return SeatCarListResponse(
            train_no=train.train_no,
            cars=tuple(SeatCar(no, name, 40, ()) for no in numbers),
        )

    def seat_layout_is_reference(self, train, seat_class, passenger_count=1) -> bool:
        return False

    def seat_inventory(
        self, train, car_no, seat_class, passenger_count=1, *, allow_layout_reference=False
    ):
        sold = scenario()["sold_seats"]
        sold_here = set((sold or {}).get(str(car_no), [])) if sold is not None else None
        seats = []
        for row in range(1, 15):
            for index, column in enumerate("ABCD"):
                label = f"{row}{column}"
                taken = (
                    label in sold_here
                    if sold_here is not None
                    else (row % 2 == 0 and column == "D")
                )
                seats.append(
                    PhysicalSeat(
                        seat_no=f"{car_no:02d}{row:02d}{index + 1:02d}",
                        sale_possible="N" if taken else "Y",
                        direction_code="1" if row <= 7 else "2",
                        other_attribute_code="000",
                        requested_attribute_code="000",
                        floor=None,
                        specification=label,
                        sequence_no=str(len(seats) + 1),
                        message_code="",
                        message="",
                        visual_message_division_code="",
                    )
                )
        return SeatInventoryResponse(
            layout_type=2,
            arrangement_code="4",
            remaining_count=sum(seat.sale_possible == "Y" for seat in seats),
            total_count=len(seats),
            seats=tuple(seats),
            car_no=car_no,
        )

    def reserve_designated(self, train, inventory, targets, *, passenger_count, seat_class):
        limit_date, limit_time = _deadline()
        pnr = _next_id("E2E")
        _log(
            "reserve_designated", pnr=pnr, train_no=train.train_no, seats=[t.label for t in targets]
        )
        return ReservationHoldResponse(
            pnr_no=pnr, payment_deadline_date=limit_date, payment_deadline_time=limit_time
        )

    def release_unpaid_hold(self, hold) -> None:
        _log("release", pnr=getattr(hold, "pnr_no", None) or getattr(hold, "rsv_id", None))

    def reservation_outcome(self, rsv_id, *, train_no=None, dep_date=None, dep_time=None):
        from korail_bot.models import ReservationOutcome

        return ReservationOutcome[scenario()["outcome"]]

    def cancel_reservation(self, rsv_id: str) -> bool:
        _log("cancel", rsv_id=rsv_id)
        return True

    def request_waitlist(
        self, *, dep_date, src_locate, dst_locate, dep_time, train_no, passenger_count
    ):
        _log("waitlist", train_no=train_no)
        return {"train_no": train_no}

    def issued_tickets(self):
        return []


def _is_loopback(host) -> bool:
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _block_outside_network() -> None:
    """
    이 프로세스가 루프백 밖으로 나가는 연결을 모두 막아요.

    가짜로 바꾸지 못한 코레일 호출(예: 역 목록)이나 앞으로 생길 외부 호출이 조용히
    실제 서버에 닿는 대신, 그 자리에서 실패해 테스트가 드러내요.
    """
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address[0]):
            _log("blocked", host=str(address[0]))
            raise OSError(f"e2e: 루프백 밖 연결을 막았어요 ({address[0]})")

    def connect(sock, address):
        check(sock, address)
        return real_connect(sock, address)

    def connect_ex(sock, address):
        check(sock, address)
        return real_connect_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex


def install() -> None:
    """코레일 호출을 가짜로 바꾸고 바깥 연결을 막아요. 로컬 Redis 가 아니면 거부해요."""
    host = urlsplit(os.environ.get("MOBILE_REDIS_URL", "")).hostname
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"가짜 코레일은 로컬 e2e Redis 에서만 켤 수 있어요 (지금: {host!r})")
    _block_outside_network()
    from korail_bot.models.station_snapshot import FALLBACK_STATIONS
    from korail_bot.services.korail_service import KorailService
    from korail_bot.utils.station_codes import StationManager

    for name, member in vars(FakeKorail).items():
        if callable(member) and not name.startswith("__"):
            setattr(KorailService, name, member)
    # 역 목록도 코레일에서 받아 와요. 내장 목록으로 답해요.
    StationManager._fetch_stations_from_api = lambda self: set(FALLBACK_STATIONS)
    # 스택은 서버가 이 기록을 남겨야 준비됐다고 봐요. 가짜 없이 뜬 서버는 쓰지 않아요.
    _log("installed", pid=os.getpid())
