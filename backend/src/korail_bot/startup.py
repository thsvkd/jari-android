"""
The work the app does once, on its way up.

app.py is a module that runs top to bottom when it is imported, which is what
a WSGI entry point has to be and what makes it the one part of the bot no test
ever executes. Anything in it with a decision in it lives here instead, as a
function that can be called with a stand-in for Redis and for Telegram.

Everything here is best effort. These are things done for the user's
convenience before the bot is doing anything for them at all, and none of them
is worth refusing to start over: a menu that failed to publish keeps whatever
it had, and the searches this app manages run over a different connection than
the one that failed.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from korail_bot.config.settings import settings
from korail_bot.services.telegram_service import TelegramService
from korail_bot.storage.base import StorageInterface
from korail_bot.telegramBot.messages import Messages
from korail_bot.utils.logger import get_logger

logger = get_logger(__name__)

MINI_APP_MENU_TEXT = "예약 열기"


def _menu_launch_url(url: str, bot_username: str) -> str:
    """Add the return transport a profile/menu Mini App needs."""
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"transport": "start", "bot": bot_username.lstrip("@")})
    return urlunsplit(parsed._replace(query=urlencode(query)))


def publish_mini_app_menu(telegram: TelegramService) -> None:
    """Synchronize Telegram's persistent chat menu with Mini App settings."""
    try:
        if not settings.mini_app_enabled():
            if telegram.reset_chat_menu_button():
                logger.info("Default Telegram command menu button restored")
            else:
                logger.warning("Could not restore the default Telegram menu button")
            return

        username = telegram.get_bot_username()
        if not username:
            logger.warning("Could not discover the bot username - Mini App menu stays unchanged")
            return

        url = _menu_launch_url(settings.MINI_APP_URL or "", username)
        if telegram.set_chat_menu_button(MINI_APP_MENU_TEXT, url):
            logger.info("Telegram Mini App menu button published")
        else:
            logger.warning("Could not publish the Telegram Mini App menu button")
    except Exception as exc:
        logger.warning(f"Could not synchronize the Telegram Mini App menu button: {exc}")


def publish_command_menus(storage: StorageInterface, telegram: TelegramService) -> int:
    """
    Put the command menu back where it belongs, for everyone and for operators.

    Two lists, because Telegram shows the narrowest one that matches a chat
    rather than merging them: the default reaches every chat, and each
    developer chat gets one scoped to itself that carries the operator's tools
    as well as the public commands.

    Republished on every start rather than only when developer mode is
    claimed. The list grows with the bot, and a chat that claimed the mode two
    releases ago would otherwise be left with the menu of that release.

    Args:
        storage: Where the developer chats are recorded
        telegram: How the lists are published

    Returns:
        How many developer chats got a list of their own
    """
    publish_mini_app_menu(telegram)

    if telegram.set_my_commands(Messages.PUBLIC_COMMANDS):
        logger.info("Command menu published to Telegram")
    else:
        logger.warning("Could not publish the command menu - the previous one stays in place")

    try:
        operators = storage.get_all_developers()
    except Exception as e:
        # The public menu is already out, and the operator menus are a
        # convenience for one or two people. Not a reason to fail a start.
        logger.error(f"Could not read the developer chats: {e}")
        return 0

    published = 0
    for operator in operators:
        if telegram.set_my_commands(Messages.DEVELOPER_COMMANDS, chat_id=operator):
            logger.info(f"Operator command menu published for chat_id={operator}")
            published += 1
        else:
            logger.warning(f"Could not publish the operator menu for chat_id={operator}")

    return published
