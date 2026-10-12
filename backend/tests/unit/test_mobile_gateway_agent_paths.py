"""에이전트(MCP)가 기대는 gateway 동작: 감시할 수 있는 열차 판정과 열차 목록의 끝."""

from datetime import timedelta
from unittest.mock import MagicMock

import fakeredis
import pytest

from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.runtime import MobileRuntime
from korail_bot.models import OnboardedAccount, UserSession
from korail_bot.utils.timezone import utc_now

OWNER = -100
DAY = (utc_now() + timedelta(days=3)).strftime("%Y%m%d")
TRIP = {
    "v": 1,
    "action": "prepare_search",
    "dep_date": DAY,
    "src_station": "서울",
    "dst_station": "부산",
    "dep_time": "0000",
    "max_dep_time": "2400",
    "train_type": "1",
    "seat_option": "1",
    "passenger_count": 1,
    "seat_strategy": "1",
}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=fakeredis.FakeRedis(decode_responses=True),
    )
    rail = MagicMock()
    rail.login.return_value = True
    rail.search_cut_short = False
    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: rail)
    runtime.rail = rail
    runtime.storage.save_onboarded_account(
        OnboardedAccount(chat_id=OWNER, korail_id="01012345678", korail_pw="rail-password")
    )
    yield runtime
    runtime.storage.close()


def listed(runtime, *options):
    """Options are (number, name, HHMM) or (number, name, HHMM, group)."""
    session = UserSession(chat_id=OWNER)
    session.train_info = {
        "depDate": DAY,
        "srcLocate": "서울",
        "dstLocate": "부산",
        "trainOptions": [
            {
                "no": number,
                "name": name,
                "dep_time": f"{clock}00",
                **({"trainGroup": group[0]} if group else {}),
            }
            for number, name, clock, *group in options
        ],
    }
    runtime.storage.save_user_session(session)


OPTIONS = (("001", "KTX", "0600"), ("003", "KTX-산천", "1430"), ("005", "ITX-새마을", "1500"))


def test_a_ktx_watch_can_only_use_the_ktx_trains_of_the_list(runtime):
    listed(runtime, *OPTIONS)
    assert runtime.gateway.listed_trains(OWNER, TRIP) == ["001", "003"]
    assert runtime.gateway.listed_trains(OWNER, {**TRIP, "train_type": "2"}) == [
        "001",
        "003",
        "005",
    ]


def test_a_watch_can_only_use_the_trains_inside_its_own_window(runtime):
    listed(runtime, *OPTIONS)
    afternoon = {**TRIP, "dep_time": "1400", "max_dep_time": "1500", "train_type": "2"}
    assert runtime.gateway.listed_trains(OWNER, afternoon) == ["003"]


def test_the_list_of_another_trip_offers_nothing(runtime):
    listed(runtime, *OPTIONS)
    assert runtime.gateway.listed_trains(OWNER, {**TRIP, "dst_station": "대전"}) == []


def test_a_list_stopped_by_the_page_limit_says_it_is_truncated(runtime):
    runtime.rail.search_selectable_trains.return_value = []
    runtime.rail.search_cut_short = True
    assert runtime.gateway.list_trains(OWNER, TRIP)["truncated"] is True


def test_a_window_without_trains_is_an_empty_list(runtime):
    runtime.rail.search_selectable_trains.return_value = []
    answer = runtime.gateway.list_trains(OWNER, TRIP)
    assert answer["trains"] == []
    assert answer["truncated"] is False


def test_a_ktx_watch_follows_the_train_group_the_worker_filters_on(runtime):
    # 워커는 이름이 아니라 열차 그룹이 KTX 인 열차만 봐요.
    listed(
        runtime,
        ("001", "KTX-이음", "0600", "KTX"),
        ("003", "KTX 이름의 다른 그룹", "0700", "ITX"),
    )
    assert runtime.gateway.listed_trains(OWNER, TRIP) == ["001"]


def test_a_train_key_says_which_train_it_is(runtime, monkeypatch):
    train = MagicMock(train_no="043", departure_date="20261013")
    monkeypatch.setattr(runtime.gateway.seat_maps, "get_train", lambda owner, key: train)
    assert runtime.gateway.reserved_train(OWNER, "k") == {"trainNo": "043", "depDate": "20261013"}


def test_an_unknown_train_key_names_no_train(runtime):
    assert runtime.gateway.reserved_train(OWNER, "never-issued") is None
