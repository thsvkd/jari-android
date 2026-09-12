"""코레일 KTX 예매 텔레그램 봇."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

#: The version, read from the installed distribution's metadata. The number
#: itself is declared in pyproject.toml, which is the one place a release
#: edits.
#:
#: Metadata is a copy written at install time, so it can in principle lag the
#: declaration. Nothing here has to guard against that: scripts/server.sh syncs
#: the environment before it starts the bot, and it does so with --frozen, so
#: a lockfile left behind by a bump stops the start with a message about it
#: rather than serving the old number quietly.
#:
#: A checkout that was never installed has no metadata to read. The bot still
#: starts and still runs; it just cannot name itself, and says so rather than
#: inventing a number that would be announced as if it were a release.
try:
    __version__ = _distribution_version("korail-ktx-bot")
except PackageNotFoundError:  # pragma: no cover - needs an uninstalled checkout
    __version__ = "0.0.0+unknown"
