from korail_bot.models.station_snapshot import FALLBACK_STATIONS
from korail_bot.utils.station_codes import StationManager


def test_station_list_is_fetched_once_and_a_failed_fetch_is_retried(monkeypatch):
    answers = [FALLBACK_STATIONS, {"서울", "부산"}]
    calls = []

    def fetch(self):
        calls.append(1)
        return answers[min(len(calls), len(answers)) - 1]

    monkeypatch.setattr(StationManager, "_fetch_stations_from_api", fetch)
    manager = StationManager()

    assert manager.get_valid_stations() is FALLBACK_STATIONS
    assert manager.get_valid_stations() == {"서울", "부산"}
    assert manager.get_valid_stations() == {"서울", "부산"}
    assert len(calls) == 2

    manager.get_valid_stations(force_refresh=True)
    assert len(calls) == 3
