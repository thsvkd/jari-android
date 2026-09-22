"""Use the real gateway and services with only railway/process boundaries faked."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis
import pytest
from korail_mobile_api import PhysicalSeat, SeatCar, SeatCarListResponse, SeatInventoryResponse

from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.runtime import MobileRuntime
from korail_bot.mobile.worker import apply_result
from korail_bot.models import (
    OnboardedAccount,
    PaymentStatus,
    ReservationOutcome,
    RunningReservation,
    TrainSearchParams,
    UserSession,
)
from korail_bot.services.mini_app_gateway import MiniAppGateway
from korail_bot.services.mini_app_service import MiniAppSubmission
from korail_bot.utils.timezone import utc_now


def test_real_gateway_delegates_booking_schedule_and_favourites(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    rail = MagicMock()
    rail.login.return_value = True
    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: rail)
    start = MagicMock(return_value=True)
    monkeypatch.setattr(runtime.reservation, "start_reservation_process", start)
    alice = runtime.identity.register(
        "alice", "a long secure passphrase", runtime.identity.create_invite()
    )
    owner = runtime.identity.authenticate(alice["token"])["storage_id"]
    http = runtime.app.test_client()
    headers = {"Authorization": "Bearer " + alice["token"]}
    assert http.post(
        "/api/mobile/register",
        headers=headers,
        json={"username": "01012345678", "password": "rail password"},
    ).json == {"registered": True}
    conditions = {
        "v": 1,
        "action": "prepare_search",
        "dep_date": (utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
        "src_station": "서울",
        "dst_station": "부산",
        "dep_time": "0900",
        "max_dep_time": "1800",
        "train_type": "1",
        "seat_option": "2",
        "passenger_count": 2,
        "seat_strategy": "1",
        "seat_preference": "A,D:3-5",
    }
    response = http.post(
        "/api/mobile/search",
        headers=headers,
        json={"conditions": conditions, "trains": ["101", "105"]},
    )
    assert response.status_code == 200, response.json
    assert response.json["started"]
    params = start.call_args.kwargs["search_params"]
    assert params.train_numbers == ["101", "105"]
    assert params.passenger_count == 2
    assert params.seats_wanted.encode() == "A,D:3-5"
    assert start.call_args.kwargs["chat_id"] == owner
    assert start.call_args.kwargs["password"] == "rail password"
    runtime.storage.delete_user_session(owner)
    scheduled = http.post(
        "/api/mobile/schedule",
        headers=headers,
        json={
            "conditions": conditions,
            "start_at": (utc_now() + timedelta(minutes=10)).isoformat(),
        },
    )
    assert scheduled.status_code == 200, scheduled.json
    assert runtime.storage.get_scheduled_search(owner) is not None
    saved = http.post(
        "/api/mobile/favourites", headers=headers, json={"conditions": conditions, "name": "집으로"}
    )
    assert saved.status_code == 200
    saved_condition = saved.json["favourites"][0]["conditions"]
    assert saved_condition.get("dep_date", "") == ""
    assert saved_condition.get("trains", []) == []
    assert "seat_targets" not in saved_condition
    listed = http.get("/api/mobile/favourites", headers=headers)
    assert listed.status_code == 200
    assert listed.json["favourites"] == saved.json["favourites"]
    fav_id = saved.json["favourites"][0]["id"]
    bob = runtime.identity.register(
        "bobby", "a long secure passphrase", runtime.identity.create_invite()
    )
    assert (
        http.delete(
            "/api/mobile/favourites/" + fav_id, headers={"Authorization": "Bearer " + bob["token"]}
        ).status_code
        == 404
    )
    assert http.delete("/api/mobile/favourites/" + fav_id, headers=headers).status_code == 200
    assert http.post("/api/mobile/logout", headers=headers, json={}).status_code == 200
    assert runtime.storage.get_scheduled_search(owner) is None
    assert runtime.storage.get_resume_credentials(owner) is None
    assert runtime.storage.get_onboarded_account(owner) is None
    runtime.storage.close()


def test_draft_carries_the_seat_plan_back_to_the_app():
    # Without this the app reopens a half-finished booking with every chosen
    # seat forgotten, and the user picks the whole car again.
    plan = {
        "strategy": "consecutive",
        "passengerCount": 2,
        "trains": [
            {
                "trainNo": "015",
                "seatClass": "general",
                "targets": [
                    {
                        "carNo": 3,
                        "seatNo": "000041",
                        "label": "5A",
                        "row": 5,
                        "column": "A",
                        "adjacencyGroup": "5:left",
                        "position": 1,
                        "rowPosition": 1,
                    },
                    {
                        "carNo": 3,
                        "seatNo": "000042",
                        "label": "5B",
                        "row": 5,
                        "column": "B",
                        "adjacencyGroup": "5:left",
                        "position": 2,
                        "rowPosition": 2,
                    },
                ],
            }
        ],
    }
    info = {
        "depDate": (utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
        "srcLocate": "서울",
        "dstLocate": "부산",
        "depTime": "090000",
        "maxDepTime": "1800",
        "trainType": "KTX",
        "specialInfo": "GENERAL_ONLY",
        "passengerCount": 2,
        "seatStrategy": "consecutive",
        "seatPlan": json.dumps(plan),
    }

    conditions = MiniAppGateway._conditions_of(info)
    assert conditions["seat_plan"]["strategy"] == "consecutive"
    assert [seat["label"] for seat in conditions["seat_plan"]["trains"][0]["targets"]] == [
        "5A",
        "5B",
    ]
    # The app hands the draft straight back as its conditions, so the draft
    # has to be something the submission boundary accepts, seat plan and all.
    assert (conditions["v"], conditions["action"]) == (1, "prepare_search")
    submission = MiniAppSubmission.parse(json.dumps(conditions, ensure_ascii=False))
    assert submission.passenger_count == 2
    assert submission.seat_strategy == "1"
    assert json.loads(submission.seat_plan_json)["trains"][0]["targets"][1]["rowPosition"] == 2

    assert "seat_plan" not in MiniAppGateway._conditions_of({"depDate": "20260913"})
    # An unreadable plan costs the seats, never the rest of the draft.
    assert "seat_plan" not in MiniAppGateway._conditions_of(
        {"depDate": "20260913", "seatPlan": "{nonsense"}
    )


def test_railway_registration_accepts_member_number_and_normalizes_phone(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    rail = MagicMock()
    rail.login.return_value = True
    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: rail)
    session = runtime.identity.register(
        "alice", "a long secure passphrase", runtime.identity.create_invite()
    )
    owner = runtime.identity.authenticate(session["token"])["storage_id"]
    http = runtime.app.test_client()
    headers = {"Authorization": "Bearer " + session["token"]}

    member = http.post(
        "/api/mobile/register",
        headers=headers,
        json={"username": "2456789012", "password": "rail password"},
    )
    assert member.status_code == 200
    rail.login.assert_called_with("2456789012", "rail password")
    assert runtime.storage.get_onboarded_account(owner).korail_id == "2456789012"

    phone = http.post(
        "/api/mobile/register",
        headers=headers,
        json={"username": "01012345678", "password": "rail password"},
    )
    assert phone.status_code == 200
    rail.login.assert_called_with("010-1234-5678", "rail password")
    assert runtime.storage.get_onboarded_account(owner).korail_id == "010-1234-5678"
    runtime.storage.close()


def test_auth_only_runtime_requires_no_bot_redis_or_rail(tmp_path, monkeypatch):
    monkeypatch.delenv("BOTTOKEN", raising=False)
    runtime = MobileRuntime(MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40))
    session = runtime.identity.register(
        "alice", "a long secure passphrase", runtime.identity.create_invite()
    )
    client = runtime.app.test_client()
    headers = {"Authorization": "Bearer " + session["token"]}
    response = client.post("/api/mobile/bootstrap", headers=headers, json={})
    assert response.status_code == 200
    assert response.json["capabilities"]["korail"] is False
    assert response.json["capabilities"]["scheduledSearch"] is False
    assert client.post("/api/mobile/search", headers=headers, json={}).status_code == 503


def test_callback_persists_event_and_clears_only_own_search(tmp_path):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    params = TrainSearchParams(
        dep_date="20260913", src_locate="서울", dst_locate="부산", dep_time="080000"
    )
    for owner in (-100, -200):
        runtime.storage.save_user_session(UserSession(chat_id=owner, in_progress=True))
        runtime.storage.save_running_reservation(
            RunningReservation(
                chat_id=owner, process_id=123, korail_id="phone", search_params=params
            )
        )
    apply_result(runtime.storage, runtime.notifications, -100, "예약 완료", 0)
    assert runtime.storage.get_running_reservation(-100) is None
    assert runtime.storage.get_running_reservation(-200) is not None
    assert runtime.storage.get_payment_status(-100).reminder_active
    assert runtime.notifications.items(-100)[0]["text"] == "예약 완료"
    assert runtime.notifications.items(-200) == []
    runtime.storage.close()


def test_payment_reminders_survive_runtime_recreation(tmp_path):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    runtime.storage.save_payment_status(
        PaymentStatus(
            chat_id=-100,
            completed=False,
            reminder_active=True,
            reservation_id="R1",
            expires_at=utc_now() + timedelta(minutes=5),
        )
    )
    runtime.remind_pending()
    runtime.remind_pending()
    assert len(runtime.notifications.items(-100)) == 1
    assert runtime.notifications.items(-100)[0]["kind"] == "payment"
    runtime.storage.close()


def test_payment_watchdog_uses_mobile_account_and_durable_sink(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    runtime.storage.save_onboarded_account(
        OnboardedAccount(chat_id=-100, korail_id="01012345678", korail_pw="rail-password")
    )
    runtime.storage.save_payment_status(
        PaymentStatus(
            chat_id=-100,
            completed=False,
            reminder_active=True,
            reservation_id="R1",
            expires_at=utc_now() + timedelta(minutes=5),
        )
    )
    rail = MagicMock()
    rail.login.return_value = True
    rail.reservation_outcome.return_value = ReservationOutcome.PAID
    watchdog = runtime.services[2]
    monkeypatch.setattr(watchdog, "_rail_service", lambda: rail)
    assert watchdog.tick() == 1
    assert runtime.storage.get_payment_status(-100).completed
    assert runtime.notifications.items(-100)
    assert runtime.notifications.items(-200) == []
    assert "rail-password" not in str(runtime.notifications.items(-100))
    runtime.storage.close()


@pytest.mark.parametrize(
    "extra",
    [
        {"operator": "srt"},
        {"seat_preference": "AD:3-5"},
        {"seat_preference": "A:9-3"},
    ],
)
def test_unsupported_conditions_are_rejected_not_ignored(extra):
    from korail_bot.mobile.gateway import MobileGateway
    from korail_bot.services.mini_app_gateway import MiniAppError

    with pytest.raises(MiniAppError):
        MobileGateway._submission(extra)


def test_waitlist_condition_is_validated_and_preserved():
    from korail_bot.mobile.gateway import MobileGateway

    submission = MobileGateway._submission(
        {
            "v": 1,
            "action": "prepare_search",
            "dep_date": (utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
            "src_station": "서울",
            "dst_station": "부산",
            "dep_time": "0900",
            "max_dep_time": "1200",
            "train_type": "1",
            "seat_option": "2",
            "passenger_count": 1,
            "seat_strategy": "1",
            "seat_preference": "",
            "waitlist": True,
        }
    )

    assert submission.waitlist is True
    assert submission.as_train_info()["waitlist"] is True


def test_designated_seat_flow_is_owner_scoped_rechecks_and_persists(tmp_path, monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    rail = MagicMock()
    rail.login.return_value = True
    train = SimpleNamespace(
        train_no="015",
        departure_date=(utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
        departure_time="090000",
        arrival_time="113000",
        departure_station_name="서울",
        arrival_station_name="부산",
        train_class_name="KTX",
        general_reservation_code="11",
        special_reservation_code="13",
        wait_reservation_flag=" 9",
    )
    rail.search_selectable_trains.return_value = [train]
    rail.describe_waitlist_train.return_value = {
        "no": "015",
        "label": "09:00~11:30 KTX",
        "dep_time": "090000",
        "arr_time": "113000",
        "name": "KTX",
        "soldout": False,
        "waitlistEligible": True,
    }
    rail.seat_cars.return_value = SeatCarListResponse(cars=(SeatCar(3, "일반실", 1, ()),))
    seat = PhysicalSeat(
        seat_no="000041",
        sale_possible="Y",
        direction_code="1",
        other_attribute_code="000",
        requested_attribute_code="000",
        floor=None,
        specification="5A",
        sequence_no="1",
        message_code="",
        message="",
        visual_message_division_code="",
    )
    rail.seat_inventory.return_value = SeatInventoryResponse(
        layout_type=2,
        arrangement_code="4",
        remaining_count=1,
        total_count=70,
        seats=(seat,),
        car_no=3,
    )
    hold = SimpleNamespace(pnr_no="PRIVATE")
    rail.reserve_designated.return_value = hold
    rail.reservation_id.return_value = "R1"
    rail.payment_due.return_value = (
        (utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
        "101500",
    )
    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: rail)
    runtime.storage.save_onboarded_account(
        OnboardedAccount(chat_id=-100, korail_id="01012345678", korail_pw="rail-password")
    )
    alice = runtime.identity.register(
        "alice", "a long secure passphrase", runtime.identity.create_invite()
    )
    owner = runtime.identity.authenticate(alice["token"])["storage_id"]
    runtime.storage.save_onboarded_account(
        OnboardedAccount(chat_id=owner, korail_id="01012345678", korail_pw="rail-password")
    )
    http = runtime.app.test_client()
    headers = {"Authorization": "Bearer " + alice["token"]}
    conditions = {
        "v": 1,
        "action": "prepare_search",
        "dep_date": train.departure_date,
        "src_station": "서울",
        "dst_station": "부산",
        "dep_time": "0900",
        "max_dep_time": "1800",
        "train_type": "1",
        "seat_option": "2",
        "passenger_count": 1,
        "seat_strategy": "1",
    }

    listed = http.post("/api/mobile/trains", headers=headers, json=conditions)
    assert listed.status_code == 200, listed.json
    train_key = listed.json["trains"][0]["trainKey"]
    assert listed.json["trains"][0]["generalAvailable"] is True
    assert listed.json["trains"][0]["specialAvailable"] is False

    cars = http.get(
        f"/api/mobile/trains/{train_key}/cars?seatClass=general&passengerCount=1",
        headers=headers,
    )
    assert cars.status_code == 200
    assert cars.json["cars"][0]["carNo"] == 3
    seats = http.get(
        f"/api/mobile/trains/{train_key}/cars/3/seats?seatClass=general&passengerCount=1",
        headers=headers,
    )
    assert seats.status_code == 200
    target = seats.json["seats"][0]

    bob = runtime.identity.register(
        "bobby", "a long secure passphrase", runtime.identity.create_invite()
    )
    assert (
        http.get(
            f"/api/mobile/trains/{train_key}/cars?seatClass=general&passengerCount=1",
            headers={"Authorization": "Bearer " + bob["token"]},
        ).status_code
        == 404
    )

    reserved = http.post(
        "/api/mobile/reservations/designated",
        headers=headers,
        json={
            "trainKey": train_key,
            "seatClass": "general",
            "passengerCount": 1,
            "carNo": 3,
            "seats": [target],
        },
    )
    assert reserved.status_code == 200, reserved.json
    assert reserved.json["reserved"] is True
    assert reserved.json["pending"][0]["reservationId"] == "R1"
    assert reserved.json["pending"][0]["seatLabels"] == ["5A"]
    status = runtime.storage.get_payment_status(owner)
    assert status.reservation_id == "R1"
    assert status.seat_labels == ["5A"]
    assert runtime.notifications.items(owner)[0]["kind"] == "payment"
    assert rail.seat_inventory.call_count == 2
    runtime.storage.close()


def car_inventory(car_no):
    """One car holding one sellable seat."""
    seat = PhysicalSeat(
        seat_no=f"{car_no:06d}",
        sale_possible="Y",
        direction_code="1",
        other_attribute_code="000",
        requested_attribute_code="000",
        floor=None,
        specification="5A",
        sequence_no="1",
        message_code="",
        message="",
        visual_message_division_code="",
    )
    return SeatInventoryResponse(
        layout_type=2,
        arrangement_code="4",
        remaining_count=1,
        total_count=70,
        seats=(seat,),
        car_no=car_no,
    )


def listed_train(tmp_path, monkeypatch, *, cars, inventories):
    """A runtime with one listed train, ready for a whole-formation seat read."""
    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    rail = MagicMock()
    rail.login.return_value = True
    rail.seat_layout_is_reference.return_value = False
    train = SimpleNamespace(
        train_no="015",
        departure_date=(utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
        departure_time="090000",
        arrival_time="113000",
        departure_station_name="서울",
        arrival_station_name="부산",
        train_class_name="KTX",
        general_reservation_code="11",
        special_reservation_code="13",
        wait_reservation_flag=" 9",
    )
    rail.search_selectable_trains.return_value = [train]
    rail.describe_waitlist_train.return_value = {
        "no": "015",
        "label": "09:00~11:30 KTX",
        "dep_time": "090000",
        "arr_time": "113000",
        "name": "KTX",
        "soldout": False,
        "waitlistEligible": True,
    }
    rail.seat_cars.return_value = SeatCarListResponse(
        cars=tuple(SeatCar(car_no, "일반실", 1, ()) for car_no in cars)
    )
    rail.seat_inventory.side_effect = inventories
    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: rail)
    alice = runtime.identity.register(
        "alice", "a long secure passphrase", runtime.identity.create_invite()
    )
    owner = runtime.identity.authenticate(alice["token"])["storage_id"]
    runtime.storage.save_onboarded_account(
        OnboardedAccount(chat_id=owner, korail_id="01012345678", korail_pw="rail-password")
    )
    http = runtime.app.test_client()
    headers = {"Authorization": "Bearer " + alice["token"]}
    listed = http.post(
        "/api/mobile/trains",
        headers=headers,
        json={
            "v": 1,
            "action": "prepare_search",
            "dep_date": train.departure_date,
            "src_station": "서울",
            "dst_station": "부산",
            "dep_time": "0900",
            "max_dep_time": "1800",
            "train_type": "1",
            "seat_option": "2",
            "passenger_count": 1,
            "seat_strategy": "1",
        },
    )
    assert listed.status_code == 200, listed.json
    return runtime, http, headers, listed.json["trains"][0]["trainKey"], rail


def test_whole_formation_read_returns_every_car_in_one_request(tmp_path, monkeypatch):
    # Per-car reads log in to Korail each time and trip the rail limit partway
    # through a long train, which is what this route exists to avoid.
    runtime, http, headers, train_key, rail = listed_train(
        tmp_path,
        monkeypatch,
        cars=(1, 3, 5),
        inventories=[car_inventory(1), car_inventory(3), car_inventory(5)],
    )

    logins = rail.login.call_count

    response = http.get(
        f"/api/mobile/trains/{train_key}/seats?seatClass=general&passengerCount=1",
        headers=headers,
    )

    assert response.status_code == 200, response.json
    assert [item["carNo"] for item in response.json["inventories"]] == [1, 3, 5]
    assert response.json["failedCars"] == []
    assert response.json["layoutReference"] is False
    # Same shape the app already caches per car, so it can store these as-is.
    first = response.json["inventories"][0]
    assert first["seats"][0]["label"] == "5A"
    assert {"layoutType", "arrangementCode", "remainingCount", "totalCount"} <= set(first)
    assert first["layoutReference"] is False
    # One login and one car list for the whole formation, then one read per
    # car - against three logins and three car lists if the app asked per car.
    assert rail.login.call_count - logins == 1
    assert rail.seat_cars.call_count == 1
    assert rail.seat_inventory.call_count == 3
    runtime.storage.close()


def test_one_unreadable_car_does_not_hide_the_rest_or_look_empty(tmp_path, monkeypatch):
    runtime, http, headers, train_key, rail = listed_train(
        tmp_path,
        monkeypatch,
        cars=(1, 3, 5),
        inventories=[car_inventory(1), RuntimeError("Korail said no"), car_inventory(5)],
    )

    response = http.get(
        f"/api/mobile/trains/{train_key}/seats?seatClass=general&passengerCount=1",
        headers=headers,
    )

    assert response.status_code == 200, response.json
    assert [item["carNo"] for item in response.json["inventories"]] == [1, 5]
    # Car 3 is reported as unread, never as a car with no seats left.
    assert response.json["failedCars"] == [3]
    runtime.storage.close()


def test_a_formation_with_no_bookable_car_is_not_an_error(tmp_path, monkeypatch):
    # Nothing failed here - there is simply nothing to show. Two empty lists
    # say that, where an error would claim the read broke.
    runtime, http, headers, train_key, rail = listed_train(
        tmp_path, monkeypatch, cars=(), inventories=[]
    )

    response = http.get(
        f"/api/mobile/trains/{train_key}/seats?seatClass=general&passengerCount=1",
        headers=headers,
    )

    assert response.status_code == 200, response.json
    assert response.json == {"inventories": [], "failedCars": [], "layoutReference": False}
    rail.seat_inventory.assert_not_called()
    runtime.storage.close()


def test_three_failures_in_a_row_stop_the_read_instead_of_holding_the_lock(tmp_path, monkeypatch):
    # An expired session fails every car alike. Trying all eighteen would hold
    # a striped mutation lock for one HTTP timeout per remaining car.
    runtime, http, headers, train_key, rail = listed_train(
        tmp_path,
        monkeypatch,
        cars=(1, 3, 5, 7, 9, 11),
        inventories=[
            car_inventory(1),
            RuntimeError("Korail said no"),
            RuntimeError("Korail said no"),
            RuntimeError("Korail said no"),
        ],
    )

    response = http.get(
        f"/api/mobile/trains/{train_key}/seats?seatClass=general&passengerCount=1",
        headers=headers,
    )

    assert response.status_code == 200, response.json
    assert [item["carNo"] for item in response.json["inventories"]] == [1]
    # Cars 9 and 11 were never attempted but are still reported as unread, so
    # the app cannot mistake them for cars with no seats left.
    assert response.json["failedCars"] == [3, 5, 7, 9, 11]
    assert rail.seat_inventory.call_count == 4
    runtime.storage.close()


def test_a_success_between_failures_keeps_the_read_going(tmp_path, monkeypatch):
    runtime, http, headers, train_key, rail = listed_train(
        tmp_path,
        monkeypatch,
        cars=(1, 3, 5, 7, 9),
        inventories=[
            RuntimeError("Korail said no"),
            RuntimeError("Korail said no"),
            car_inventory(5),
            RuntimeError("Korail said no"),
            RuntimeError("Korail said no"),
        ],
    )

    response = http.get(
        f"/api/mobile/trains/{train_key}/seats?seatClass=general&passengerCount=1",
        headers=headers,
    )

    assert response.status_code == 200, response.json
    assert [item["carNo"] for item in response.json["inventories"]] == [5]
    assert response.json["failedCars"] == [1, 3, 7, 9]
    assert rail.seat_inventory.call_count == 5
    runtime.storage.close()


def test_a_formation_that_reads_nowhere_is_an_error_not_a_sold_out_train(tmp_path, monkeypatch):
    runtime, http, headers, train_key, _ = listed_train(
        tmp_path,
        monkeypatch,
        cars=(1, 3),
        inventories=[RuntimeError("Korail said no"), RuntimeError("Korail said no")],
    )

    response = http.get(
        f"/api/mobile/trains/{train_key}/seats?seatClass=general&passengerCount=1",
        headers=headers,
    )

    # The gateway raises 502 like the per-car route; the API turns 502 into 503
    # so Cloudflare cannot swap the body for one without CORS headers.
    assert response.status_code == 503
    assert "좌석표를 불러오지 못했어요" in response.json["error"]
    runtime.storage.close()


def test_scheduler_executes_persisted_conditions_without_chat(tmp_path, monkeypatch):
    from freezegun import freeze_time

    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    scheduler = runtime.gateway.scheduler
    params = TrainSearchParams(
        dep_date=(utc_now() + timedelta(days=2)).strftime("%Y%m%d"),
        src_locate="서울",
        dst_locate="부산",
        dep_time="090000",
    )
    start_at = utc_now() + timedelta(minutes=1)
    scheduler.schedule(-100, "01012345678", "rail-password", params, start_at)
    start = MagicMock(return_value=True)
    monkeypatch.setattr(runtime.reservation, "start_reservation_process", start)
    with freeze_time(start_at + timedelta(seconds=1)):
        scheduler.tick()
    assert start.call_args.kwargs["chat_id"] == -100
    assert start.call_args.kwargs["password"] == "rail-password"
    assert runtime.storage.get_scheduled_search(-100) is None
    assert runtime.notifications.items(-100)
    runtime.storage.close()


def test_unlink_cannot_finish_while_scheduled_start_holds_credentials(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    client = fakeredis.FakeRedis(decode_responses=True)
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "identity.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=client,
    )
    runtime.storage.save_onboarded_account(
        OnboardedAccount(chat_id=-100, korail_id="01012345678", korail_pw="rail-password")
    )
    scheduler = runtime.gateway.scheduler
    params = TrainSearchParams(
        dep_date=(utc_now() + timedelta(days=2)).strftime("%Y%m%d"),
        src_locate="서울",
        dst_locate="부산",
        dep_time="090000",
    )
    scheduled = scheduler.schedule(
        -100, "01012345678", "rail-password", params, utc_now() + timedelta(minutes=1)
    )
    paused, release, logout_entered, logout_done = (threading.Event() for _ in range(4))
    order = []

    def notify(*args, **kwargs):
        paused.set()
        assert release.wait(5)

    def start(*args, **kwargs):
        order.append("start")
        return True

    def logout():
        logout_entered.set()
        result = runtime.gateway.logout(-100)
        order.append("logout")
        logout_done.set()
        return result

    monkeypatch.setattr(runtime.notifications, "send_message", notify)
    monkeypatch.setattr(runtime.reservation, "start_reservation_process", start)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            firing = pool.submit(
                scheduler._fire, scheduled, scheduled.start_at + timedelta(seconds=1)
            )
            assert paused.wait(5)
            unlink = pool.submit(logout)
            assert logout_entered.wait(5)
            finished_early = logout_done.wait(0.15)
            release.set()
            firing.result(timeout=5)
            assert unlink.result(timeout=5) == {"registered": False}
        assert not finished_early
        assert order == ["start", "logout"]
        scheduler._fire(scheduled, scheduled.start_at + timedelta(seconds=2))
        assert order == ["start", "logout"]
        assert runtime.storage.get_onboarded_account(-100) is None
    finally:
        release.set()
        runtime.storage.close()
