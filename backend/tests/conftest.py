"""Pytest configuration and shared fixtures.

The package is installed (editable) into the environment, so there is no
sys.path juggling here - `import korail_bot...` just works.

tests/integration and tests/e2e talk to a real Redis, which testcontainers
starts for them and which therefore needs a Docker daemon. tests/unit does
not touch Redis at all, so a run restricted to that directory skips the
container entirely and needs no Docker.

The `storage` fixture below is the way to reach that Redis. A test that
builds its own RedisStorage owns closing it, and the check in
pytest_sessionfinish says so out loud when one is left open.
"""

import os
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_UNIT_DIR = _TESTS_DIR / "unit"

_redis_container = None


def _only_unit_tests(config) -> bool:
    """
    True when every path on the command line lives under tests/unit.

    Deliberately conservative: anything it cannot place - a bare `pytest`, a
    `-k` expression, a path outside tests/unit - counts as "might need Redis"
    and the container is started as before.
    """
    args = [arg for arg in config.args if not arg.startswith("-")]
    if not args:
        return False

    for arg in args:
        # Strip the '::TestClass::test_name' part of a node id.
        path = Path(arg.split("::", 1)[0]).resolve()
        if not path.is_relative_to(_UNIT_DIR):
            return False
    return True


def pytest_configure(config):
    """Set up the environment, and a Redis container when one is needed."""
    global _redis_container

    # Secrets the application expects. Set before any project module is
    # imported so that korail_bot.config.settings picks them up.
    os.environ.setdefault("BOTTOKEN", "test-bot-token")
    os.environ.setdefault("SESSION_SECRET", "test-session-secret")
    os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")

    # Real fixed-account credentials inherited from the developer's
    # environment would make the bot skip the login prompts, so every test of
    # those prompts would exercise a different flow than it means to. Tests
    # that want the skip set it explicitly.
    os.environ.pop("USERID", None)
    os.environ.pop("USERPW", None)

    os.environ["REDIS_DB"] = "0"
    # The throwaway container runs without auth, and the unit tests never
    # connect at all. A REDIS_PASSWORD inherited from the developer's .env
    # would fail with "AUTH called without any password configured".
    os.environ.pop("REDIS_PASSWORD", None)

    if _only_unit_tests(config):
        # Nothing here opens a connection; these just keep settings importable.
        os.environ.setdefault("REDIS_HOST", "localhost")
        os.environ.setdefault("REDIS_PORT", "6379")
        return

    from testcontainers.redis import RedisContainer

    _redis_container = RedisContainer("redis:7-alpine")
    _redis_container.start()

    os.environ["REDIS_HOST"] = _redis_container.get_container_host_ip()
    os.environ["REDIS_PORT"] = str(_redis_container.get_exposed_port(6379))


def _connections_owned_by_the_process():
    """
    Redis connections that belong to the process rather than to a test.

    A module-level singleton opens its connection once, when its module is
    imported, and holds it until the interpreter exits - so it is never
    finalised while tests are running, and it is not what the check below is
    looking for.
    """
    import sys

    module = sys.modules.get("korail_bot.utils.station_codes")
    client = getattr(getattr(module, "_station_manager", None), "_redis_client", None)
    pool = getattr(client, "connection_pool", None)
    if pool is None:
        return set()
    held = list(pool._available_connections) + list(pool._in_use_connections)
    return {id(connection) for connection in held}


def pytest_sessionfinish(session, exitstatus):
    """
    Fail the run if a test left a Redis connection open.

    Not tidiness. An unclosed client keeps its socket until the garbage
    collector reaches it, and a socket collected with its file descriptor
    still open raises ResourceWarning - which filterwarnings turns into an
    error, inside a __del__, where it becomes an unraisable exception that
    pytest reports against whichever test happened to be running. The run
    fails on a test that did nothing wrong, and only sometimes, because it
    depends on when a collection happens to fall.

    So it is caught here instead, where the culprit is still nearby and the
    answer is always the same: close the storage in the teardown that opened
    it, or take it from the `storage` fixture, which does.
    """
    if _redis_container is None or exitstatus != 0:
        return

    import gc

    import redis

    gc.collect()
    theirs = _connections_owned_by_the_process()
    leaked = [
        connection
        for connection in gc.get_objects()
        if isinstance(connection, redis.connection.AbstractConnection)
        and connection._sock is not None
        and id(connection) not in theirs
    ]
    if leaked:
        session.exitstatus = 1
        print(
            f"\nERROR: {len(leaked)} Redis connection(s) still open at the end of the run.\n"
            "       A test built a storage and never closed it; see tests/conftest.py.\n"
            + "".join(f"       {connection!r}\n" for connection in leaked[:5])
        )


def pytest_unconfigure(config):
    """Clean up the Redis container after all tests."""
    global _redis_container
    if _redis_container:
        _redis_container.stop()
        _redis_container = None


@pytest.fixture(scope="session")
def redis_container():
    """The running Redis container, or None when the run skipped it."""
    return _redis_container


@pytest.fixture
def storage():
    """
    A storage on the throwaway Redis, handed over empty and left empty.

    Lives here rather than in each test module so that the closing at the end
    happens once and always - see pytest_sessionfinish above for what a
    forgotten one costs.

    Imported inside the function rather than at the top of the file because importing
    korail_bot reads the settings, and pytest_configure above has to have
    written the environment first.
    """
    from korail_bot.storage import RedisStorage

    storage = RedisStorage()
    storage.redis.flushdb()
    yield storage
    storage.redis.flushdb()
    storage.close()
