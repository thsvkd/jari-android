"""Station name validation and management for Korail."""

import json
from typing import Optional

import requests

from korail_bot.models.station_snapshot import FALLBACK_STATIONS
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)

# API endpoint for station data
KORAIL_STATION_DB_URL = (
    "https://smart.letskorail.com:443/classes/com.korail.mobile.common.stationdata"
)

# Redis cache key and TTL
REDIS_STATION_CACHE_KEY = "korail:station_list"
REDIS_STATION_CACHE_TTL = 86400  # 24 hours

# Fallback static list (snapshot from Korail API on 2026-06-02).
# Used only when both Redis cache and API request fail.


class StationManager:
    """Manages station data with Redis caching."""

    _instance: Optional["StationManager"] = None
    _redis_client = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize_redis()
        return cls._instance

    def _initialize_redis(self):
        """Initialize Redis client (lazy loading)."""
        try:
            import redis

            from korail_bot.config.settings import settings

            self._redis_client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
            # Test connection
            self._redis_client.ping()
            logger.info("StationManager: Redis connected for station caching")
        except Exception as e:
            logger.warning(f"StationManager: Redis connection failed: {e}")
            logger.warning("StationManager: Will operate without Redis caching")
            self._redis_client = None

    def _fetch_stations_from_api(self) -> set[str]:
        """
        Fetch station list from Korail API.

        Returns:
            Set of station names
        """
        try:
            logger.info("Fetching station data from Korail API...")
            response = requests.get(
                KORAIL_STATION_DB_URL,
                headers={
                    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 5.1.1; Nexus 4 Build/LMY48T)"
                },
                timeout=10,
            )

            if response.status_code == 200:
                data = response.json()
                # 실측 응답 구조: {"stns": {"stn": [{"stn_nm": ..., ...}, ...]}}
                if isinstance(data, dict) and isinstance(data.get("stns"), dict):
                    stn_list = data["stns"].get("stn", [])
                    stations = {s["stn_nm"] for s in stn_list if s.get("stn_nm")}
                    if stations:
                        logger.info(f"Successfully fetched {len(stations)} stations from API")
                        return stations
                logger.warning(f"Unexpected API response format: {type(data)}")
                return FALLBACK_STATIONS

            logger.warning(f"API returned status code {response.status_code}")
            return FALLBACK_STATIONS

        except requests.exceptions.Timeout:
            logger.warning("API request timed out, using fallback station list")
            return FALLBACK_STATIONS
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch stations from API: {e}")
            return FALLBACK_STATIONS
        except Exception as e:
            logger.error(f"Unexpected error fetching station data: {e}", exc_info=True)
            return FALLBACK_STATIONS

    def _get_from_redis(self) -> set[str] | None:
        """
        Get station list from Redis cache.

        Returns:
            Set of station names or None if cache miss
        """
        if not self._redis_client:
            return None

        try:
            cached_data = self._redis_client.get(REDIS_STATION_CACHE_KEY)
            if cached_data:
                stations = set(json.loads(cached_data))
                logger.info(f"Loaded {len(stations)} stations from Redis cache")
                return stations
        except Exception as e:
            logger.warning(f"Failed to load stations from Redis: {e}")

        return None

    def _save_to_redis(self, stations: set[str]) -> None:
        """
        Save station list to Redis cache.

        Args:
            stations: Set of station names
        """
        if not self._redis_client:
            return

        try:
            data = json.dumps(list(stations))
            self._redis_client.setex(REDIS_STATION_CACHE_KEY, REDIS_STATION_CACHE_TTL, data)
            logger.info(
                f"Saved {len(stations)} stations to Redis cache (TTL={REDIS_STATION_CACHE_TTL}s)"
            )
        except Exception as e:
            logger.warning(f"Failed to save stations to Redis: {e}")

    def get_valid_stations(self, force_refresh: bool = False) -> set[str]:
        """
        Get valid station names with multi-tier caching.

        Caching strategy:
        1. Try Redis cache (24h TTL)
        2. If cache miss, fetch from API
        3. If API fails, use fallback static list
        4. Save successful API result to Redis

        Args:
            force_refresh: Force refresh from API

        Returns:
            Set of valid station names
        """
        # Try Redis cache first (unless forced refresh)
        if not force_refresh:
            cached_stations = self._get_from_redis()
            if cached_stations:
                return cached_stations

        # Fetch from API
        logger.info("Fetching fresh station data...")
        stations = self._fetch_stations_from_api()

        # Save to Redis if not fallback
        if stations != FALLBACK_STATIONS:
            self._save_to_redis(stations)

        return stations


# Global station manager instance
_station_manager = StationManager()


def get_valid_stations(force_refresh: bool = False) -> set[str]:
    """
    Get the current set of valid station names.

    Args:
        force_refresh: Force refresh from API

    Returns:
        Set of valid station names
    """
    return _station_manager.get_valid_stations(force_refresh=force_refresh)


def is_valid_station(station_name: str) -> bool:
    """
    Check if station name is valid.

    Args:
        station_name: Station name (without '역')

    Returns:
        True if station is valid, False otherwise
    """
    if not station_name:
        return False

    # Get valid stations from cache/API
    valid_stations = get_valid_stations()

    # 정확히 일치하는 역명 확인
    return station_name in valid_stations


def get_similar_stations(station_name: str, max_results: int = 5) -> list:
    """
    Get similar station names for suggestion.

    Args:
        station_name: User input station name
        max_results: Maximum number of suggestions

    Returns:
        List of similar station names
    """
    if not station_name:
        return []

    # Get valid stations from cache/API
    valid_stations = get_valid_stations()

    # 정확히 일치하면 빈 리스트 반환
    if station_name in valid_stations:
        return []

    matches = []

    # 1. 부분 문자열 매칭 (포함 관계)
    for valid_station in valid_stations:
        if station_name in valid_station or valid_station in station_name:
            matches.append(valid_station)

    # 2. 첫 글자 매칭 (접두사)
    if not matches and len(station_name) >= 1:
        for valid_station in valid_stations:
            if valid_station.startswith(station_name[0]):
                matches.append(valid_station)

    # 중복 제거 및 정렬
    matches = sorted(set(matches))

    return matches[:max_results]


def format_station_suggestions(similar_stations: list) -> str:
    """
    Format station suggestions for display.

    Args:
        similar_stations: List of similar station names

    Returns:
        Formatted suggestion string
    """
    if not similar_stations:
        return ""

    if len(similar_stations) == 1:
        return f"\n\n혹시 '{similar_stations[0]}'을(를) 찾으시나요?"

    suggestions = ", ".join(similar_stations)
    return f"\n\n비슷한 역: {suggestions}"
