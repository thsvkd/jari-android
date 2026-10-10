"""결제 대기 예약과 다른 감시가 함께 있을 때의 취소·표시."""

from datetime import timedelta
from unittest.mock import MagicMock

import fakeredis
import pytest

from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.runtime import MobileRuntime
from korail_bot.models import (
    MultiReservationStatus,
    OnboardedAccount,
    PaymentStatus,
    ReservationPaymentStatus,
    RunningReservation,
    SingleReservationInfo,
    TrainSearchParams,
)
from korail_bot.services.mini_app_gateway import MiniAppError
from korail_bot.utils.timezone import utc_now

OWNER = -100


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=fakeredis.FakeRedis(decode_responses=True),
    )
    rail = MagicMock()
    rail.login.return_value = True
    rail.cancel_reservation.return_value = True
    monkeypatch.setattr(runtime.gateway.pending_payments, "_rail_service", lambda: rail)
    runtime.rail = rail
    runtime.storage.save_onboarded_account(
        OnboardedAccount(chat_id=OWNER, korail_id="01012345678", korail_pw="rail-password")
    )
    yield runtime
    runtime.storage.close()


def hold_one_seat(runtime):
    runtime.storage.save_payment_status(
        PaymentStatus(
            chat_id=OWNER,
            completed=False,
            reminder_active=True,
            reservation_id="R1",
            train_info="KTX 101 서울 → 부산",
            expires_at=utc_now() + timedelta(minutes=20),
        )
    )


def watch_another_trip(runtime):
    runtime.storage.save_running_reservation(
        RunningReservation(
            chat_id=OWNER,
            process_id=123,
            korail_id="phone",
            search_params=TrainSearchParams(
                dep_date="20261020", src_locate="서울", dst_locate="대전", dep_time="080000"
            ),
        )
    )


def test_a_held_seat_can_be_given_back_while_another_trip_is_watched(runtime):
    hold_one_seat(runtime)
    watch_another_trip(runtime)

    result = runtime.gateway.cancel_pending(OWNER)

    assert result["cancelled"] is True
    runtime.rail.cancel_reservation.assert_called_once_with("R1")
    assert runtime.storage.get_running_reservation(OWNER) is not None


def hold_half_a_party(runtime, *, train_no="", dep_date=""):
    runtime.storage.save_multi_reservation_status(
        MultiReservationStatus(
            chat_id=OWNER,
            reservations=[
                SingleReservationInfo(
                    reservation_id="R1",
                    reservation_obj=None,
                    reserved_at=utc_now(),
                    expires_at=utc_now() + timedelta(minutes=20),
                    status=ReservationPaymentStatus.PENDING,
                    seat_number=1,
                    train_no=train_no,
                    dep_date=dep_date,
                    train_info="KTX 101 서울 → 부산",
                )
            ],
            total_seats=2,
            seat_strategy="random",
            created_at=utc_now(),
            manually_stopped=False,
        )
    )


def test_a_party_still_being_filled_is_not_cancelled_under_the_search(runtime):
    # 한 번에 여럿을 잡는 감시가 남은 좌석을 찾는 중이면, 잡은 좌석을 돌려주는 순간 감시가 다시 잡아 와요.
    hold_half_a_party(runtime)
    watch_another_trip(runtime)

    with pytest.raises(MiniAppError):
        runtime.gateway.cancel_pending(OWNER)

    runtime.rail.cancel_reservation.assert_not_called()


def test_a_leftover_party_from_another_trip_does_not_block_cancelling(runtime):
    hold_half_a_party(runtime, train_no="101", dep_date="20261001")
    watch_another_trip(runtime)

    assert runtime.gateway.cancel_pending(OWNER)["cancelled"] is True


def test_stopping_the_search_clears_the_seat_counter_that_blocks_cancelling(runtime):
    hold_one_seat(runtime)
    watch_another_trip(runtime)
    runtime.storage.set_current_seat_index(OWNER, 1)

    runtime.gateway.cancel_search(OWNER)

    assert runtime.storage.get_current_seat_index(OWNER) is None
    assert runtime.gateway.cancel_pending(OWNER)["cancelled"] is True


def test_watched_trains_keep_their_departure_labels_after_the_picker_is_gone(runtime):
    from korail_bot.models import UserSession

    session = UserSession(chat_id=OWNER, in_progress=True)
    session.train_info = {
        "depDate": "20261020",
        "srcLocate": "서울",
        "dstLocate": "부산",
        "depTime": "080000",
        "maxDepTime": "1800",
        "trainType": "KTX",
        "trainTypeShow": "KTX",
        "specialInfo": "GENERAL_ONLY",
        "specialInfoShow": "일반실만",
        "selectedTrains": ["101"],
        "trainOptions": [
            {"no": "101", "label": "07:00→09:40 KTX"},
            {"no": "103", "label": "08:00→10:40 KTX"},
        ],
    }
    params = runtime.gateway.conversation._build_search_params(session)
    runtime.storage.save_running_reservation(
        RunningReservation(chat_id=OWNER, process_id=123, korail_id="phone", search_params=params)
    )
    runtime.storage.delete_user_session(OWNER)

    running = runtime.gateway._running(OWNER)

    assert running["trainLabels"] == {"101": "07:00→09:40 KTX"}
    assert running["selectedTrains"] == ["101"]
