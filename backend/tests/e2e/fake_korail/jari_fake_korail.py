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
import requests
from korail2 import TrainType
from korail2.korail2 import Reservation, Ticket, Train
from korail_mobile_api import (
    KorailSeatAssignment,
    ReservationDetailJourney,
    ReservationHistoryTrain,
    ReservationSeatDetail,
    TicketReservationDetailResponse,
)
from korail_mobile_api.errors import KorailReservationRefusedError, KorailTransportError
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
# 가짜 코레일에 신청된 예약 대기: 예약번호 → 열차(JSON). 코레일의 예약 목록 역할이에요.
WAITLIST_KEY = "e2e:waitlists"
# 시나리오를 바꾼 뒤 코레일이 로그인에 답하지 않은 횟수. 스택이 시나리오를 바꿀 때 지워요.
LOGIN_UNREACHABLE_KEY = "e2e:login_unreachable"

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
    # 시나리오를 바꾼 뒤 처음 몇 번의 로그인에 코레일이 답하지 않는지(HTTP 503). 그 뒤로는 login 대로 답해요.
    "login_unreachable": 0,
    # 예약 워커의 조회 결과: seats(몇 번 헛돈 뒤 빈자리) | sold_out(계속 매진) | unavailable(조회 실패)
    # | blocked(코레일이 strResult 없는 응답을 줌: 2026-10-19 운영에서 계정이 막혔을 때의 답)
    "search": "seats",
    # 열차 목록(앱의 열차 조회·에이전트의 find_trains) 결과: trains(시간표대로) | sold_out(모든 열차 매진)
    # | unavailable(코레일이 답하지 않음, HTTP 503). 조회 실패와 매진을 따로 재현하려고 둬요.
    "list": "trains",
    "seat_after_polls": 2,
    # 결제 확인이 돌려줄 답: OUTSTANDING | PAID | RELEASED | UNKNOWN
    "outcome": "OUTSTANDING",
    "deadline_minutes": 10,
    # 좌석표에서 팔린 좌석(호차별 좌석 표시 목록). 없으면 짝수 줄 D 석만 팔린 것으로 둬요.
    "sold_seats": None,
    # 서버가 앱에 돌려주는 모양의 시나리오: 잔여석 0 으로 채워 넣은 호차 번호와 모두 "팔 수 없음"인 좌석표.
    # 이 가짜는 KorailService 통째로 바꾸므로 채우는 로직(KorailService._with_unlisted_cars)은 여기서 돌지 않아요.
    # 그쪽은 단위 테스트(test_korail_seat_map.py)가 검증하고, 이 시나리오는 그 결과를 그리는 앱 화면을 검증해요.
    "sold_out_cars": [],
    # 코레일이 호차를 하나도 안 줘서 다른 날짜의 편성으로 좌석표를 그린 좌석 등급(general | special).
    # 실제 서버는 이때 seat_layout_is_reference 가 참이 되고 좌석표의 빈자리는 그날의 것이에요.
    "reference_layout": [],
    # 코레일이 취소를 거절하는 예약번호. 번호는 예약이 잡힌 뒤에야 알 수 있어, 테스트가 /status 에서 읽어 넣어요.
    # 거절한 호출도 기록에 "cancel" 로 남고(refused: true), 예약은 그대로 결제 대기예요.
    "cancel_refused": [],
    # 예약 대기 신청에 코레일이 하는 답: ok(대기 등록) | refused(코레일이 거절) | unavailable(HTTP 503)
    "waitlist_request": "ok",
    # 신청된 예약 대기를 다시 읽을 때: waiting(대기 중) | assigned(좌석 배정, 결제 대기) | paid(결제되어 승차권)
    # | ended(목록에서 사라짐) | unavailable(코레일이 답하지 않음)
    "waitlist": "waiting",
    # 예약 대기 취소를 코레일이 거절하는지
    "waitlist_cancel_refused": False,
    "trains": DEFAULT_TRAINS,
    # 예약 워커가 시작할 때 운영 설정 대신 쓰는 값(초). None 이면 스택 설정 그대로예요.
    # 조회 실패가 이만큼 이어지면 감시를 멈춰요(SEARCH_FAILURE_STOP_SECONDS).
    "failure_stop_seconds": None,
    # 감시를 이만큼 하면 멈춰요(WATCH_MAX_SECONDS). 재시작 뒤에도 처음 시작한 때부터 세요.
    "watch_max_seconds": None,
}

# 차단 의심 응답 본문. 운영에서 실제로 온 본문은 남지 않았어요(그래서 이번에 키·본문 일부를 로그에 남겨요).
# 판정에 쓰는 것은 strResult 가 없다는 것뿐이라, 여느 코레일 답처럼 생긴 키로 채워요.
BLOCKED_REPLY = {"h_msg_cd": "", "h_msg_txt": "", "rtnMsg": "비정상적인 접근입니다."}

# install 이 덮어쓰기 전의 실제 KorailService 메서드. blocked 조회는 실제 코드로 돌아요.
_REAL: dict = {}


class _ReplyAdapter(requests.adapters.BaseAdapter):
    """korail2 의 HTTP 세션에 끼우는 전송로: 소켓을 열지 않고 정해 둔 JSON 을 HTTP 200 으로 돌려줘요."""

    def __init__(self, body: dict):
        super().__init__()
        self.body = body

    def send(self, request, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(self.body, ensure_ascii=False).encode()
        response.headers["Content-Type"] = "application/json;charset=UTF-8"
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response

    def close(self) -> None:
        pass


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
        state = scenario()
        if _client().incr(LOGIN_UNREACHABLE_KEY) <= state["login_unreachable"]:
            _log("login", username=username, accepted=False, unreachable=True, pid=os.getpid())
            # 실제 클라이언트가 5xx 를 받았을 때 올리는 그 예외예요.
            raise KorailTransportError(
                "KORAIL HTTP 503 for POST /classes/com.korail.mobile.login.Login"
            )
        accepted = bool(state["login"])
        _log("login", username=username, accepted=accepted, pid=os.getpid())
        # 코레일 회원번호 역할. 실제 로그인 세션의 member_card_no 처럼 계정마다 달라요.
        self._e2e_member = f"MB{username}" if accepted else None
        return accepted

    def member_number(self):
        return getattr(self, "_e2e_member", None)

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
        wanted=None,
    ) -> list:
        mode = scenario()["list"]
        _log("list_trains", dep_date=dep_date, src=src_locate, dst=dst_locate, mode=mode)
        if mode == "unavailable":
            # 실제 클라이언트가 5xx 를 받았을 때 올리는 그 예외예요.
            raise KorailTransportError(
                "KORAIL HTTP 503 for POST /classes/com.korail.mobile.seatMovie.ScheduleView"
            )
        rows = _schedule(dep_date, dep_time)
        if mode == "sold_out":
            # 코레일 코드 그대로: 13 매진. 열차는 있고 좌석만 없어요.
            rows = [dict(row, general="13", special="13") for row in rows]
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
            for row in rows
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
        _log("search", polls=polls, mode=state["search"], pid=os.getpid())
        if state["search"] == "unavailable":
            raise SearchUnavailableError("가짜 코레일: 조회 실패")
        if state["search"] == "blocked":
            # 실제 korail2 클라이언트(KorailService._build_client)가 실제 KorailService.search_trains
            # 로 조회해요. 응답만 정해 둔 것이라 korail2 의 _result_check 가 KeyError 를 내는 데서부터
            # 차단 판정과 로그까지 운영 코드가 그대로 돌아요.
            client = self._build_client(self._username or "e2e", "unused")
            client._session.mount("https://", _ReplyAdapter(BLOCKED_REPLY))
            client.logined = True
            self._korail_instance = client
            return _REAL["search_trains"](
                self,
                dep_date,
                src_locate,
                dst_locate,
                dep_time,
                max_dep_time,
                train_type,
                passenger_count,
                verbose,
                include_no_seats,
                train_numbers,
            )
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

    def reserve_train(
        self, train, option=None, passenger_count=1, seat_preference=None, seat_direction=""
    ):
        limit_date, limit_time = _deadline()
        rsv_id = _next_id("E2E")
        _log(
            "reserve_train",
            rsv_id=rsv_id,
            train_no=train.train_no,
            passengers=passenger_count,
            **({"seat_direction": seat_direction} if seat_direction else {}),
        )
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
            cars=tuple(
                SeatCar(no, name, 0 if no in scenario()["sold_out_cars"] else 40, ())
                for no in numbers
            ),
        )

    def seat_layout_is_reference(self, train, seat_class, passenger_count=1) -> bool:
        return seat_class in scenario()["reference_layout"]

    def seat_inventory(
        self, train, car_no, seat_class, passenger_count=1, *, allow_layout_reference=False
    ):
        sold = scenario()["sold_seats"]
        car_sold_out = car_no in scenario()["sold_out_cars"]
        sold_here = set((sold or {}).get(str(car_no), [])) if sold is not None else None
        seats = []
        for row in range(1, 15):
            for index, column in enumerate("ABCD"):
                label = f"{row}{column}"
                taken = car_sold_out or (
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
                        message="4인 동반석" if row in (7, 8) else "",
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
        # 실제 KorailService.reserve_designated 와 같은 검사(같은 예외·문구)예요. 팔린 좌석이나
        # 좌석표와 다른 표시로는 잡히지 않아야, 에이전트가 고른 좌석을 그대로 믿지 않는지 드러나요.
        if passenger_count < 1 or len(targets) != passenger_count:
            raise ValueError("선택한 좌석 수와 승객 수가 같아야 합니다.")
        if inventory.car_no is None or any(target.car_no != inventory.car_no for target in targets):
            raise ValueError("한 번의 지정 예약에서는 같은 호차의 좌석만 선택할 수 있습니다.")
        physical_by_number = {seat.seat_no: seat for seat in inventory.seats}
        for target in targets:
            physical = physical_by_number.get(target.seat_no)
            if physical is None or physical.sale_possible != "Y":
                _log("reserve_refused", reason="not_for_sale", seat=target.label)
                raise ValueError("선택한 좌석 중 현재 판매 가능한 좌석이 없습니다.")
            if physical.specification != target.label:
                _log("reserve_refused", reason="label", seat=target.label)
                raise ValueError("좌석 정보가 바뀌었습니다. 좌석표를 다시 불러와 주세요.")
            # 실제 예약 요청이 쓰는 라이브러리 변환도 거쳐요. 좌석표 필드가 틀리면 여기서 드러나요.
            KorailSeatAssignment.from_inventory(inventory, physical)
        limit_date, limit_time = _deadline()
        pnr = _next_id("E2E")
        _log(
            "reserve_designated", pnr=pnr, train_no=train.train_no, seats=[t.label for t in targets]
        )
        return ReservationHoldResponse(
            pnr_no=pnr, payment_deadline_date=limit_date, payment_deadline_time=limit_time
        )

    def reserve_any_seat(self, train, *, passenger_count, seat_class):
        limit_date, limit_time = _deadline()
        pnr = _next_id("E2E")
        _log("reserve_any_seat", pnr=pnr, train_no=train.train_no, seat_class=seat_class)
        return ReservationHoldResponse(
            pnr_no=pnr, payment_deadline_date=limit_date, payment_deadline_time=limit_time
        )

    def release_unpaid_hold(self, hold) -> None:
        _log("release", pnr=getattr(hold, "pnr_no", None) or getattr(hold, "rsv_id", None))

    def reservation_outcome(self, rsv_id, *, train_no=None, dep_date=None, dep_time=None):
        from korail_bot.models import ReservationOutcome

        return ReservationOutcome[scenario()["outcome"]]

    def cancel_reservation(self, rsv_id: str) -> bool:
        refused = rsv_id in scenario()["cancel_refused"]
        _log("cancel", rsv_id=rsv_id, refused=refused)
        return not refused

    def _register_standby(self, train, passenger_count):
        # 실제 request_waitlist 가 열차를 다시 찾고 예약 대기 대상(" 9")인지 본 뒤에 오는 자리예요.
        mode = scenario()["waitlist_request"]
        _log("waitlist", train_no=train.train_no, passengers=passenger_count, mode=mode)
        if mode == "unavailable":
            raise KorailTransportError(
                "KORAIL HTTP 503 for POST /classes/com.korail.mobile.certification.TicketReservation"
            )
        if mode == "refused":
            raise KorailReservationRefusedError("WRR800029", "동일한 예약 내역이 있습니다.")
        pnr = _next_id("WAIT")
        _client().hset(
            WAITLIST_KEY,
            pnr,
            json.dumps(
                {
                    "train_no": train.train_no,
                    "dep_date": train.departure_date,
                    "dep_time": train.departure_time,
                    "arr_time": train.arrival_time,
                    "passengers": passenger_count,
                    # 코레일 예약 목록은 계정마다 따로예요. 신청한 회원의 목록에만 보여요.
                    "member": self.member_number(),
                }
            ),
        )
        return ReservationHoldResponse(str_result="SUCC", pnr_no=pnr, journey_count="0001")

    def _reservation_rows(self):
        mode = scenario()["waitlist"]
        _log("waitlist_list", mode=mode)
        if mode == "unavailable":
            raise KorailTransportError(
                "KORAIL HTTP 503 for GET /classes/com.korail.mobile.reservation.ReservationView"
            )
        if mode not in ("waiting", "assigned"):
            return ()
        limit_date, limit_time = _deadline()
        rows = []
        for pnr, raw in _client().hgetall(WAITLIST_KEY).items():
            train = json.loads(raw)
            if train.get("member") != self.member_number():
                continue
            fields = {
                "h_pnr_no": pnr,
                "h_trn_no": train["train_no"],
                "h_run_dt": train["dep_date"],
                "h_dpt_tm": train["dep_time"],
                "h_arv_tm": train["arr_time"],
                "h_tot_seat_cnt": f"{train['passengers']:03d}",
            }
            if mode == "assigned":
                fields.update({"h_ntisu_lmt_dt": limit_date, "h_ntisu_lmt_tm": limit_time})
            rows.append(
                ReservationHistoryTrain(
                    pnr_no=pnr,
                    train_no=train["train_no"],
                    run_date=train["dep_date"],
                    departure_time=train["dep_time"],
                    arrival_time=train["arr_time"],
                    seat_count=train["passengers"],
                    raw=fields,
                )
            )
        return tuple(rows)

    def _hold_detail(self, rsv_id):
        assigned = scenario()["waitlist"] == "assigned"
        _log("waitlist_detail", assigned=assigned)
        seats = (ReservationSeatDetail(car_no="3", seat_no="7A", room_class_code="1"),)
        return TicketReservationDetailResponse(
            str_result="SUCC",
            pnr_no=rsv_id,
            journeys=(ReservationDetailJourney(seats=seats if assigned else ()),),
        )

    def _ticket_rows(self):
        if scenario()["waitlist"] != "paid":
            return []
        tickets = []
        for raw in _client().hgetall(WAITLIST_KEY).values():
            train = json.loads(raw)
            if train.get("member") != self.member_number():
                continue
            tickets.append(
                Ticket(
                    {
                        "ticket_list": [
                            {
                                "train_info": [
                                    {
                                        "h_trn_clsf_cd": "100",
                                        "h_trn_clsf_nm": "KTX",
                                        "h_trn_gp_cd": "100",
                                        "h_trn_no": train["train_no"],
                                        "h_dpt_rs_stn_nm": "서울",
                                        "h_dpt_rs_stn_cd": "0001",
                                        "h_dpt_dt": train["dep_date"],
                                        "h_dpt_tm": train["dep_time"],
                                        "h_arv_rs_stn_nm": "부산",
                                        "h_arv_rs_stn_cd": "0020",
                                        "h_arv_dt": train["dep_date"],
                                        "h_arv_tm": train["arr_time"],
                                        "h_run_dt": train["dep_date"],
                                        "h_seat_cnt": f"{train['passengers']:03d}",
                                        # 신청한 날 팔린 승차권: 시각이 없어 대기로 산 것인지는 단정하지 않아요.
                                        "h_orgtk_sale_dt": datetime.now(KST).strftime("%Y%m%d"),
                                        "h_rcvd_amt": "00059800",
                                        "h_srcar_no": "3",
                                        "h_seat_no": "7A",
                                    }
                                ]
                            }
                        ]
                    }
                )
            )
        return tickets

    def cancel_waitlist(self, rsv_id, journey_count=""):
        refused = bool(scenario()["waitlist_cancel_refused"])
        _log("waitlist_cancel", rsv_id=rsv_id, refused=refused)
        if refused:
            raise KorailReservationRefusedError("ERR000000", "취소할 수 없는 예약입니다.")
        _client().hdel(WAITLIST_KEY, rsv_id)
        return True

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

    _REAL["search_trains"] = KorailService.search_trains
    for name, member in vars(FakeKorail).items():
        if callable(member) and not name.startswith("__"):
            setattr(KorailService, name, member)
    # 시나리오가 정한 감시 한도를 이 프로세스의 설정으로 써요. 워커는 감시마다 새로 뜨니 테스트마다 바뀌어요.
    from korail_bot.config.settings import settings

    state = scenario()
    for key, name in (
        ("failure_stop_seconds", "KORAIL_FAILURE_STOP_SECONDS"),
        ("watch_max_seconds", "WATCH_MAX_SECONDS"),
    ):
        if state.get(key) is not None:
            setattr(settings, name, float(state[key]))
    # 예약 대기 확인 간격의 하한(운영 10분)을 풀어요. 스택이 짧은 간격을 줘서 배정까지 몇 초 안에 봐요.
    from korail_bot.mobile import config

    config.WAITLIST_CHECK_MIN_MINUTES = 0
    # 역 목록도 코레일에서 받아 와요. 내장 목록으로 답해요.
    StationManager._fetch_stations_from_api = lambda self: set(FALLBACK_STATIONS)
    # 스택은 서버가 이 기록을 남겨야 준비됐다고 봐요. 가짜 없이 뜬 서버는 쓰지 않아요.
    _log("installed", pid=os.getpid())
