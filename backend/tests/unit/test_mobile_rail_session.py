"""One Korail login serves a user's seat-map browsing, not one per car."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis
import pytest
from korail_mobile_api.errors import (
    KorailAppError,
    KorailAuthError,
    KorailSessionExpiredError,
    KorailSoldOutError,
)

from korail_bot.mobile import gateway as gateway_module
from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.runtime import MobileRuntime
from korail_bot.models import OnboardedAccount
from korail_bot.services.mini_app_gateway import KORAIL_DOWN, MiniAppError
from korail_bot.utils.timezone import utc_now


def _runtime(tmp_path):
    return MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=fakeredis.FakeRedis(decode_responses=True),
    )


def test_seat_map_reads_share_one_korail_login(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    rails = []

    def rail_service(owner):
        rail = MagicMock()
        rail.login.return_value = True
        rails.append(rail)
        return rail

    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", rail_service)
    now = [1000.0]
    monkeypatch.setattr(gateway_module, "monotonic", lambda: now[0])
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
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="015"))
    cars = f"/api/mobile/trains/{key}/cars?seatClass=general&passengerCount=1"

    # Registration verified the password with a client of its own; the reads
    # that follow log in once and keep that session across calls.
    for _ in range(3):
        assert http.get(cars, headers=headers).status_code == 200
    assert len(rails) == 2
    assert rails[1].login.call_count == 1
    assert rails[1].seat_cars.call_count == 3

    # Korail refusing a read is what an expired session looks like from here,
    # so the session is dropped and the retry logs in afresh.
    rails[1].seat_cars.side_effect = RuntimeError("session gone")
    assert http.get(cars, headers=headers).status_code == 503
    rails[1].close.assert_called_once()
    assert http.get(cars, headers=headers).status_code == 200
    assert len(rails) == 3

    # A session older than the train keys it serves is not reused either.
    now[0] += gateway_module.RAIL_SESSION_SECONDS + 1
    assert http.get(cars, headers=headers).status_code == 200
    assert len(rails) == 4
    rails[2].close.assert_called_once()

    # Unlinking the account closes the session it was logged in with.
    assert http.post("/api/mobile/logout", headers=headers).status_code == 200
    rails[3].close.assert_called_once()
    assert runtime.gateway._rails == {}


def test_empty_firebase_placeholder_means_no_push(tmp_path, monkeypatch):
    placeholder = tmp_path / "firebase-admin.json"
    placeholder.write_text("")
    monkeypatch.setenv("MOBILE_SECRET", "a" * 40)
    monkeypatch.setenv("MOBILE_REDIS_URL", "redis://localhost:1/1")
    monkeypatch.setenv("MOBILE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MOBILE_FCM_CREDENTIALS", str(placeholder))
    assert MobileConfig.from_env().fcm_credentials is None

    placeholder.write_text("{}")
    assert MobileConfig.from_env().fcm_credentials == str(placeholder)


def _expiring_user(tmp_path, monkeypatch):
    """A linked user whose cached Korail login Korail has since dropped (P058)."""
    runtime = _runtime(tmp_path)
    rails = []

    def rail_service(owner):
        rail = MagicMock()
        rail.login.return_value = True
        rail.login_unavailable = False
        rails.append(rail)
        return rail

    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", rail_service)
    alice = runtime.identity.register(
        "alice", "a long secure passphrase", runtime.identity.create_invite()
    )
    owner = runtime.identity.authenticate(alice["token"])["storage_id"]
    runtime.storage.save_onboarded_account(
        OnboardedAccount(chat_id=owner, korail_id="01012345678", korail_pw="rail-password")
    )
    return runtime, owner, rails


EXPIRED = KorailSessionExpiredError("P058", "세션이 만료되었습니다")


def test_a_read_korail_says_has_expired_logs_in_again_on_the_same_rail(tmp_path, monkeypatch):
    # Production, 2026-10-14: a login idle 28 minutes was gone at Korail though the gateway kept
    # it for RAIL_SESSION_SECONDS. The same rail logs in again (it keeps the reference formation
    # the train key was drawn with) and the read is asked once more.
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="015"))
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rail = rails[0]
    rail.seat_cars.side_effect = [EXPIRED, SimpleNamespace(cars=())]

    assert runtime.gateway.seat_cars(owner, key, "general", 1)["cars"] == []

    assert len(rails) == 1
    assert rail.login.call_count == 2
    rail.close.assert_not_called()


def test_a_booking_korail_turned_away_as_expired_is_asked_once_more(tmp_path, monkeypatch):
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    train = SimpleNamespace(
        train_no="015",
        departure_date=(utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
        departure_time="090000",
        arrival_time="113000",
        departure_station_name="서울",
        arrival_station_name="부산",
        train_class_name="KTX",
    )
    key = runtime.gateway.seat_maps.remember_train(owner, train)
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rail = rails[0]
    rail.seat_layout_is_reference.return_value = False
    rail.reserve_designated.side_effect = [EXPIRED, SimpleNamespace(pnr_no="PRIVATE")]
    rail.reservation_id.return_value = "R1"
    rail.payment_due.return_value = (train.departure_date, "101500")

    booked = runtime.gateway.reserve_designated(
        owner,
        {
            "trainKey": key,
            "seatClass": "general",
            "passengerCount": 1,
            "carNo": 3,
            "seats": [{"carNo": 3, "seatNo": "000041", "label": "5A"}],
        },
    )

    assert booked["reserved"] is True
    assert rail.reserve_designated.call_count == 2
    # The seats are read again on the fresh login before the second ask.
    assert rail.seat_inventory.call_count == 2
    assert rail.login.call_count == 2


def test_an_expired_login_korail_will_not_renew_is_said_as_korail_down(tmp_path, monkeypatch):
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="015"))
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rail = rails[0]
    rail.seat_cars.side_effect = EXPIRED
    rail.login.return_value = False
    rail.login_unavailable = True
    rail.login_unavailable_notice = None

    with pytest.raises(MiniAppError) as failure:
        runtime.gateway.seat_cars(owner, key, "general", 1)

    assert failure.value.status == 502
    assert failure.value.message == KORAIL_DOWN
    rail.close.assert_called_once()
    assert runtime.gateway._rails == {}


def test_an_expiry_that_comes_back_after_a_fresh_login_is_a_failed_read(tmp_path, monkeypatch):
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="015"))
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rails[0].seat_cars.side_effect = EXPIRED

    with pytest.raises(MiniAppError) as failure:
        runtime.gateway.seat_cars(owner, key, "general", 1)

    # Asked twice, not forever; the 502 drops the login so the next tap starts afresh.
    assert failure.value.status == 502
    assert rails[0].seat_cars.call_count == 3
    assert runtime.gateway._rails == {}


def test_a_booking_expiry_that_names_a_reservation_is_not_booked_again(tmp_path, monkeypatch):
    # Korail checks the session first, but an expiry that carries a reservation number may have
    # held something; booking again would leave that one untracked.
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    train = SimpleNamespace(
        train_no="015",
        departure_date=(utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
        departure_time="090000",
        arrival_time="113000",
        departure_station_name="서울",
        arrival_station_name="부산",
        train_class_name="KTX",
    )
    key = runtime.gateway.seat_maps.remember_train(owner, train)
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rail = rails[0]
    rail.seat_layout_is_reference.return_value = False
    rail.reserve_designated.side_effect = KorailSessionExpiredError(
        "P058", "세션이 만료되었습니다", raw={"h_msg_cd": "P058", "h_pnr_no": "320261018000000"}
    )

    with pytest.raises(MiniAppError) as failure:
        runtime.gateway.reserve_designated(
            owner,
            {
                "trainKey": key,
                "seatClass": "general",
                "passengerCount": 1,
                "carNo": 3,
                "seats": [{"carNo": 3, "seatNo": "000041", "label": "5A"}],
            },
        )

    assert failure.value.status == 502
    assert "예약 목록" in failure.value.message
    assert rail.reserve_designated.call_count == 1
    assert rail.login.call_count == 1


def test_a_login_the_client_already_forgot_is_logged_in_again(tmp_path, monkeypatch):
    # A P058 swallowed elsewhere leaves the client logged out; it then refuses before sending.
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="015"))
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rail = rails[0]
    rail.seat_cars.side_effect = [
        KorailAuthError("KORAIL request requires an authenticated session"),
        SimpleNamespace(cars=()),
    ]

    assert runtime.gateway.seat_cars(owner, key, "general", 1)["cars"] == []
    assert rail.login.call_count == 2


def test_an_expired_login_korail_refuses_to_renew_asks_to_connect_again(tmp_path, monkeypatch):
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="015"))
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rail = rails[0]
    rail.seat_cars.side_effect = EXPIRED
    rail.login.return_value = False

    with pytest.raises(MiniAppError) as failure:
        runtime.gateway.seat_cars(owner, key, "general", 1)

    assert failure.value.status == 428
    assert runtime.gateway._rails == {}


def test_a_train_list_and_one_car_are_read_again_after_an_expiry(tmp_path, monkeypatch):
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="015"))
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rail = rails[0]
    rail.seat_inventory.side_effect = [EXPIRED, MagicMock()]
    monkeypatch.setattr(
        runtime.gateway.seat_maps, "describe_inventory", lambda response: {"carNo": 3}
    )

    assert runtime.gateway.seat_inventory(owner, key, 3, "general", 1)["carNo"] == 3
    assert rail.login.call_count == 2

    rail.search_selectable_trains.side_effect = [EXPIRED, []]
    listed = runtime.gateway.list_trains(
        owner,
        {
            "conditions": {
                "v": 1,
                "action": "prepare_search",
                "dep_date": (utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
                "src_station": "서울",
                "dst_station": "부산",
                "dep_time": "0900",
                "max_dep_time": "1800",
                "train_type": "1",
                "seat_option": "2",
                "passenger_count": 1,
                "seat_strategy": "1",
                "seat_preference": "",
            }
        },
    )
    assert listed["trains"] == []
    assert rail.search_selectable_trains.call_count == 2


AUTH_GONE = KorailAppError("[3]인증정보에 문제가 있습니다.", "[3]인증정보에 문제가 있습니다.")


def test_a_booking_after_a_long_idle_that_korail_calls_an_auth_problem_is_asked_again(
    tmp_path, monkeypatch
):
    # Production, 2026-10-14 05:31 KST: 29.5 idle minutes after the seat map, the booking's
    # seat read came back FAIL "[3]인증정보에 문제가 있습니다." (not P058) and the booking failed.
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    train = SimpleNamespace(
        train_no="207",
        departure_date=(utc_now() + timedelta(days=3)).strftime("%Y%m%d"),
        departure_time="090400",
        arrival_time="100800",
        departure_station_name="서울",
        arrival_station_name="대전",
        train_class_name="KTX",
    )
    key = runtime.gateway.seat_maps.remember_train(owner, train)
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rail = rails[0]
    rail.seat_layout_is_reference.return_value = False
    rail.seat_inventory.side_effect = [AUTH_GONE, MagicMock()]
    rail.reserve_designated.return_value = SimpleNamespace(pnr_no="PRIVATE")
    rail.reservation_id.return_value = "R1"
    rail.payment_due.return_value = (train.departure_date, "101500")

    booked = runtime.gateway.reserve_designated(
        owner,
        {
            "trainKey": key,
            "seatClass": "general",
            "passengerCount": 1,
            "carNo": 1,
            "seats": [{"carNo": 1, "seatNo": "53", "label": "14A"}],
        },
    )

    assert booked["reserved"] is True
    assert rail.login.call_count == 2
    assert rail.reserve_designated.call_count == 1


def test_other_korail_refusals_are_not_mistaken_for_a_gone_login(tmp_path, monkeypatch):
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="015"))
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rail = rails[0]
    rail.seat_cars.side_effect = KorailSoldOutError("ERR211161", "잔여석이 없습니다")

    with pytest.raises(MiniAppError):
        runtime.gateway.seat_cars(owner, key, "general", 1)

    assert rail.login.call_count == 1
    assert rail.seat_cars.call_count == 2


def test_an_auth_problem_on_a_booking_that_names_a_reservation_is_not_booked_again(
    tmp_path, monkeypatch
):
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="207"))
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rail = rails[0]
    rail.seat_layout_is_reference.return_value = False
    rail.reserve_designated.side_effect = KorailAppError(
        "[3]인증정보에 문제가 있습니다.",
        "[3]인증정보에 문제가 있습니다.",
        raw={"strResult": "FAIL", "h_pnr_no": "320261018000000"},
    )

    with pytest.raises(MiniAppError) as failure:
        runtime.gateway.reserve_designated(
            owner,
            {
                "trainKey": key,
                "seatClass": "general",
                "passengerCount": 1,
                "carNo": 1,
                "seats": [{"carNo": 1, "seatNo": "53", "label": "14A"}],
            },
        )

    assert failure.value.status == 502
    assert rail.login.call_count == 1
    assert rail.reserve_designated.call_count == 1


@pytest.mark.parametrize(
    ("code", "message"),
    [("[3]인증정보에 문제가 있습니다.", None), (None, "[3]인증정보에 문제가 있습니다.")],
)
def test_the_auth_problem_is_read_from_either_its_code_or_its_message(
    tmp_path, monkeypatch, code, message
):
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="015"))
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rail = rails[0]
    rail.seat_cars.side_effect = [KorailAppError(code, message), SimpleNamespace(cars=())]

    assert runtime.gateway.seat_cars(owner, key, "general", 1)["cars"] == []
    assert rail.login.call_count == 2


def test_an_auth_problem_that_comes_back_after_a_fresh_login_is_a_failed_read(
    tmp_path, monkeypatch
):
    runtime, owner, rails = _expiring_user(tmp_path, monkeypatch)
    key = runtime.gateway.seat_maps.remember_train(owner, SimpleNamespace(train_no="015"))
    runtime.gateway.seat_cars(owner, key, "general", 1)
    rails[0].seat_cars.side_effect = AUTH_GONE

    with pytest.raises(MiniAppError) as failure:
        runtime.gateway.seat_cars(owner, key, "general", 1)

    assert failure.value.status == 502
    assert rails[0].login.call_count == 2
    assert rails[0].seat_cars.call_count == 3
