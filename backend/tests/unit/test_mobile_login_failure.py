from datetime import timedelta
from unittest.mock import MagicMock

import fakeredis

from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.runtime import MobileRuntime
from korail_bot.utils.timezone import utc_now


def test_a_failed_korail_login_keeps_the_linked_account(tmp_path, monkeypatch):
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=fakeredis.FakeRedis(decode_responses=True),
    )
    rail = MagicMock()
    rail.login.return_value = True
    monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: rail)
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

    # KorailService.login() answers False for a timeout just as for a bad password.
    rail.login.return_value = False
    response = http.post(
        "/api/mobile/trains",
        headers=headers,
        json={
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

    assert response.status_code == 428, response.json
    assert "잠시 후 다시 시도" in response.json["error"]
    assert runtime.storage.get_onboarded_account(owner) is not None
