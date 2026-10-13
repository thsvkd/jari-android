"""A Korail that does not take logins must not cost the user a saved account."""

from unittest.mock import MagicMock

import pytest

from korail_bot.handlers.conversation_handler import ConversationHandler
from korail_bot.models import OnboardedAccount, UserProgress, UserSession

CHAT = 4242
NOTICE = "01:30부터 04:30까지 시스템 점검 중입니다."


class _Rail:
    def __init__(self, ok=False, unavailable=False, notice=None):
        self.ok = ok
        self.login_unavailable = unavailable
        self.login_unavailable_notice = notice

    def login(self, korail_id, korail_pw):
        return self.ok


def _handler(rail, session):
    storage = MagicMock()
    storage.get_onboarded_account.return_value = OnboardedAccount(
        chat_id=CHAT, korail_id="01012345678", korail_pw="secret"
    )
    storage.get_user_session.return_value = session
    handler = ConversationHandler(storage, MagicMock(), MagicMock(), access_service=MagicMock())
    handler._rail_service = lambda chat_id: rail
    handler.uses_server_account = lambda chat_id: False
    return handler, storage


def _sent(handler):
    return handler.telegram.send_message.call_args.args[1]


@pytest.mark.parametrize("notice", [None, NOTICE])
def test_resume_keeps_account_when_korail_is_unavailable(notice):
    session = UserSession(chat_id=CHAT, in_progress=True, last_action=UserProgress.STARTED)
    handler, storage = _handler(_Rail(unavailable=True, notice=notice), session)

    assert handler.resume_with_registered_account(CHAT, session) is True

    storage.delete_onboarded_account.assert_not_called()
    assert session.last_action != UserProgress.PW_INPUT_SUCCESS
    assert session.credentials is None
    text = _sent(handler)
    assert "로그인을 받지 않습니다" in text
    assert "그대로 두었습니다" in text
    assert "다시 시도" in text
    assert ("코레일 안내: " + NOTICE in text) == bool(notice)
    assert "다시 등록" not in text


@pytest.mark.parametrize("step", [UserProgress.START_ACCEPTED, UserProgress.ID_INPUT_SUCCESS])
def test_a_yes_to_start_while_korail_is_down_does_not_wait_for_a_phone_number(step):
    # "예" to /start saves START_ACCEPTED before the login is tried; left there, the next
    # message would be read as a phone number for an account that is already registered.
    session = UserSession(chat_id=CHAT, in_progress=True, last_action=step)
    handler, storage = _handler(_Rail(unavailable=True), session)

    handler.resume_with_registered_account(CHAT, session)

    assert session.last_action == UserProgress.STARTED
    storage.save_user_session.assert_called_with(session)
    storage.delete_onboarded_account.assert_not_called()


def test_resume_moves_a_stale_progress_marker_off_login_success():
    session = UserSession(chat_id=CHAT, last_action=UserProgress.PW_INPUT_SUCCESS)
    handler, storage = _handler(_Rail(unavailable=True), session)

    handler.resume_with_registered_account(CHAT, session, announce=False)

    assert session.last_action != UserProgress.PW_INPUT_SUCCESS
    storage.delete_onboarded_account.assert_not_called()


def test_resume_deletes_account_when_login_is_refused():
    session = UserSession(chat_id=CHAT, in_progress=True, last_action=UserProgress.STARTED)
    handler, storage = _handler(_Rail(unavailable=False), session)

    assert handler.resume_with_registered_account(CHAT, session) is True

    storage.delete_onboarded_account.assert_called_once_with(CHAT)
    assert session.last_action == UserProgress.STARTED
    assert "등록된 정보는 지웠으니" in _sent(handler)


def test_resume_treats_a_non_true_flag_as_refused():
    # A bare MagicMock attribute is truthy but not `True`; only a real flag counts.
    session = UserSession(chat_id=CHAT)
    rail = _Rail()
    rail.login_unavailable = "yes"
    handler, storage = _handler(rail, session)

    handler.resume_with_registered_account(CHAT, session)

    storage.delete_onboarded_account.assert_called_once_with(CHAT)


def _submission():
    submission = MagicMock()
    submission.as_train_info.return_value = {"depDate": "20261020"}
    return submission


@pytest.mark.parametrize("notice", [None, NOTICE])
def test_mini_app_keeps_account_when_korail_is_unavailable(notice):
    session = UserSession(chat_id=CHAT)
    handler, storage = _handler(_Rail(unavailable=True, notice=notice), session)

    handler._apply_mini_app_submission(CHAT, _submission())

    storage.delete_onboarded_account.assert_not_called()
    assert session.in_progress is False
    assert session.train_info == {}
    handler.telegram.send_message.assert_called_once()
    text = _sent(handler)
    assert "로그인을 받지 않습니다" in text
    assert "앱에서 다시 보내주세요" in text
    assert ("코레일 안내: " + NOTICE in text) == bool(notice)
    assert "휴대전화" not in text


def test_mini_app_deletes_account_when_login_is_refused():
    session = UserSession(chat_id=CHAT)
    handler, storage = _handler(_Rail(unavailable=False), session)

    handler._apply_mini_app_submission(CHAT, _submission())

    storage.delete_onboarded_account.assert_called_once_with(CHAT)
    texts = [c.args[1] for c in handler.telegram.send_message.call_args_list]
    assert any("등록된 계정으로 로그인할 수 없어" in t for t in texts)
    assert session.last_action == UserProgress.START_ACCEPTED
