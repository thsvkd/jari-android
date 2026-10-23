"""
No credential Korail quotes back reaches a log, whichever line prints it.

korail_mobile_api 2.x puts the text of Korail's reply into ``str(exc)`` unmasked (1.1.1 masked it), so
the masking is at the logging boundary (korail_bot.utils.log_redaction). Every string a reviewer
managed to get through earlier is pushed here through the real 2.6.2 client over a fake transport and
the service and gateway code that logs its errors, and the whole captured output - messages and
tracebacks with their cause chains - is searched for the markers.
"""

import contextlib
import io
import logging
import re
from datetime import timedelta

import fakeredis
import httpx
import pytest
from korail_mobile_api import KorailClient, KorailSession
from korail_mobile_api.errors import classify_app_error

from korail_bot.mobile.config import MobileConfig
from korail_bot.mobile.runtime import MobileRuntime
from korail_bot.models import OnboardedAccount
from korail_bot.services import korail_service
from korail_bot.services.korail_service import KorailService
from korail_bot.services.rail_service import SearchGaveUp
from korail_bot.utils.logger import get_logger
from korail_bot.utils.redaction import redact_text
from korail_bot.utils.timezone import utc_now

from .test_korail_current_login import _korail_answering_login

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

# Every string a reply could quote the request in, from the first review round to the third.
LEAKS = [
    '{"sid":"SYNTHETIC_SESSION"}',
    "https://h/p?txt%50wd=SYNTHETIC_PASSWORD",
    "https://h/p?next=txtPwd%3DSYNTHETIC_PASSWORD",
    "txtPwd=SYNTHETIC_HEAD&SYNTHETIC_TAIL",
    "Authorization: Bearer SYNTHETIC_TOKEN",
    "txtPwd=SYNTHETIC_PASSWORD JSESSIONID=SYNTHETIC_SESSION",
    "https://smart.letskorail.com/x?txtPwd=SYNTHETIC_PASSWORD&sid=SYNTHETIC_SESSION&ok=1",
    '{"txtPwd": "SYNTHETIC_PASSWORD", "strMbCrdNo":"SYNTHETIC_MEMBER"}',
    "Cookie: JSESSIONID=SYNTHETIC_SESSION; other=1",
    "Set-Cookie: JSESSIONID=SYNTHETIC_SESSION; Path=/",
    "txtPwd: SYNTHETIC_PASSWORD",
    "4111111111111111",
    "카드 4111 1111 1111 1111 으로",
    # A card after a separator hyphen is a card: 1.1.1 masked it, so does the boundary.
    "카드번호-4111111111111111",
    "S000: 카드번호-4111111111111111",
]
MARKERS = re.compile(r"SYNTHETIC_[A-Z]+|4111[ ]?1111[ ]?1111[ ]?1111")


def replies_quoting(text):
    """Korail's replies in every shape the service reads, each one carrying `text`."""
    return {
        "failed search": {"strResult": "FAIL", "h_msg_cd": "S000", "h_msg_txt": text},
        "update required": {"strResult": "FAIL", "h_msg_cd": "-8204", "h_msg_txt": text},
        "sold out": {"strResult": "FAIL", "h_msg_cd": "ERR211161", "h_msg_txt": text},
        "restricted account": {"h_msg_cd": "", "h_msg_txt": text, "rtnMsg": text},
        "code is the text": {"h_msg_cd": "", "h_msg_txt": "", "code": text},
        "code is the text, with a result": {"strResult": "FAIL", "h_msg_cd": text},
        "dynapath refusal": {"code": "-2000", "message": text},
    }


def logged_in_service(monkeypatch, body, status=200) -> KorailService:
    def reply(request):
        return httpx.Response(status, json=body)

    real = KorailClient
    monkeypatch.setattr(
        korail_service,
        "KorailClient",
        lambda config: real(config, transport=httpx.MockTransport(reply)),
    )
    service = KorailService()
    client = service._build_modern_client()
    client.session.current = KorailSession(
        jsessionid="synthetic-session", member_no="1234567890", raw={}
    )
    service._modern_client = client
    service._logged_in = True
    return service


def search(service):
    return service.search_waitlist_trains(
        dep_date=DAY,
        src_locate="서울",
        dst_locate="부산",
        dep_time="000000",
        max_dep_time="2400",
        train_type=korail_service.TrainType.KTX,
        passenger_count=1,
    )


@pytest.fixture
def everything_logged(caplog):
    """All output of every logger, at every level, formatted the way a handler formats it."""
    caplog.set_level(logging.DEBUG)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    yield lambda: caplog.text + "\n" + stream.getvalue()
    logging.getLogger().removeHandler(handler)


def assert_clean(output):
    assert not MARKERS.search(output), output


@pytest.mark.parametrize("text", LEAKS)
@pytest.mark.parametrize("shape", list(replies_quoting("x")))
def test_a_failed_watch_pass_logs_no_credential(monkeypatch, everything_logged, text, shape):
    service = logged_in_service(monkeypatch, replies_quoting(text)[shape])

    with pytest.raises(Exception) as raised:
        search(service)
    with contextlib.suppress(SearchGaveUp):
        service.note_search_failure(raised.value)

    assert_clean(everything_logged())


@pytest.mark.parametrize("text", LEAKS)
@pytest.mark.parametrize("shape", list(replies_quoting("x")))
def test_a_failed_train_list_logs_no_credential_and_keeps_the_code(
    monkeypatch, tmp_path, everything_logged, text, shape
):
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=fakeredis.FakeRedis(decode_responses=True),
    )
    try:
        runtime.storage.save_onboarded_account(
            OnboardedAccount(chat_id=OWNER, korail_id="01012345678", korail_pw="rail-password")
        )
        service = logged_in_service(monkeypatch, replies_quoting(text)[shape])
        service.login = lambda *_: True
        monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: service)

        with pytest.raises(Exception):  # noqa: B017 - a MiniAppError, whatever its status
            runtime.gateway.list_trains(OWNER, TRIP)
    finally:
        runtime.storage.close()

    output = everything_logged()
    assert_clean(output)
    [line] = [row for row in output.splitlines() if "Could not list current Korail trains" in row][
        :1
    ] or [""]
    assert "code=" in line


@pytest.mark.parametrize(
    ("shape", "code"),
    [
        ("failed search", "code=S000"),
        ("update required", "code=-8204"),
        ("sold out", "code=ERR211161"),
        ("dynapath refusal", "code=-2000"),
        ("code is the text", "code=<redacted>"),
    ],
)
def test_the_diagnostics_stay_identifiable_from_codes(
    monkeypatch, tmp_path, everything_logged, shape, code
):
    runtime = MobileRuntime(
        MobileConfig(str(tmp_path / "app.sqlite3"), "a" * 40, "redis://localhost:1/1"),
        redis_client=fakeredis.FakeRedis(decode_responses=True),
    )
    try:
        runtime.storage.save_onboarded_account(
            OnboardedAccount(chat_id=OWNER, korail_id="01012345678", korail_pw="rail-password")
        )
        service = logged_in_service(
            monkeypatch, replies_quoting("txtPwd=SYNTHETIC_PASSWORD")[shape]
        )
        service.login = lambda *_: True
        monkeypatch.setattr(runtime.gateway.conversation, "_rail_service", lambda owner: service)
        with pytest.raises(Exception):  # noqa: B017
            runtime.gateway.list_trains(OWNER, TRIP)
    finally:
        runtime.storage.close()

    output = everything_logged()
    assert_clean(output)
    assert code in output


def test_a_restricted_account_reply_is_still_told_by_its_notice(monkeypatch, everything_logged):
    body = {"h_msg_cd": "", "h_msg_txt": "", "rtnMsg": "비정상적인 접근입니다."}
    service = logged_in_service(monkeypatch, body)
    service._last_modern_reply = httpx.Response(200, json=body)

    with pytest.raises(Exception) as raised:
        search(service)
    service.is_block_signal(raised.value)

    output = everything_logged()
    assert "answered without strResult, like a block" in output
    assert "비정상적인 접근입니다." in output


@pytest.mark.parametrize("text", LEAKS)
def test_a_login_korail_answers_with_a_quote_logs_no_credential(
    monkeypatch, everything_logged, text
):
    for reply in (
        {"strResult": "FAIL", "h_msg_cd": "WRC000392", "h_msg_txt": text},
        {"strResult": "FAIL", "h_msg_cd": "SEMGTK", "h_msg_txt": text},
        {"strResult": "FAIL", "h_msg_cd": "-8204", "h_msg_txt": text},
        {"code": "-2000", "message": text},
        {"h_msg_cd": "", "rtnMsg": text},
    ):
        service = KorailService()
        monkeypatch.setattr(
            service,
            "_build_modern_client",
            lambda reply=reply: _korail_answering_login(httpx.Response(200, json=reply)),
        )
        service.login("1234567890", "synthetic-password")
        service._username, service._password = "1234567890", "synthetic-password"
        # The watch's way back in: a second login attempt, then the verdict it gives the loop.
        with contextlib.suppress(Exception):
            service.ensure_logged_in()

    assert_clean(everything_logged())


@pytest.mark.parametrize("text", LEAKS)
def test_the_logging_boundary_masks_messages_tracebacks_and_their_causes(everything_logged, text):
    logger = get_logger("test_log_masking")
    error = classify_app_error("S000", text)

    logger.error("plain %s", text)
    logger.error(f"f-string {text}")
    logger.error("the error: %s", error)
    try:
        try:
            raise error
        except Exception as cause:
            raise RuntimeError("wrapper") from cause
    except RuntimeError:
        logger.error("with traceback", exc_info=True)
        logger.exception("and exception()")
    logger.warning("with stack", stack_info=True, extra={"x": text})

    assert_clean(everything_logged())


def test_ordinary_log_lines_are_not_touched(everything_logged):
    logger = get_logger("test_log_masking")

    logger.info(
        "Search page %d failed (%s); chat_id=%s", 2, "KorailTransportError", -2637222897571620
    )

    output = everything_logged()
    assert "Search page 2 failed (KorailTransportError); chat_id=-2637222897571620" in output


def test_the_user_facing_error_text_of_a_reservation_process_is_masked():
    # telebotBackProcess puts str(e) into the chat's "오류 내용" line through redact_text.
    error = classify_app_error("S000", "txtPwd=SYNTHETIC_PASSWORD JSESSIONID=SYNTHETIC_SESSION")

    assert not MARKERS.search(redact_text(str(error)))
    assert "S000" in redact_text(str(error))


def test_a_card_after_a_hyphen_is_masked_but_a_negative_id_and_korails_negative_codes_are_not():
    assert redact_text("카드번호-4111111111111111") == "카드번호-[REDACTED_CARD]"
    assert redact_text("4111-1111-1111-1111") == "[REDACTED_CARD]"
    assert redact_text("chat_id=-2637222897571620") == "chat_id=-2637222897571620"
    # An id that happens to pass Luhn is still an id when it is named as one.
    assert redact_text("chat_id=-4111111111111111") == "chat_id=-4111111111111111"
    assert redact_text("code=-8204 h_msg_cd: -2000") == "code=-8204 h_msg_cd: -2000"


# ---------------------------------------------- what Python prints when nothing catches it

CHAIN = """
from korail_mobile_api.errors import classify_app_error

cause = classify_app_error("S000", "txtPwd=SYNTHETIC_PASSWORD JSESSIONID=SYNTHETIC_SESSION")
"""


def run_child(code):
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )


def assert_child_clean(done):
    assert done.returncode != 0
    assert "S000" in done.stderr  # still readable
    assert not MARKERS.search(done.stderr), done.stderr
    assert not MARKERS.search(done.stdout)


def test_an_uncaught_exception_with_its_causes_is_printed_masked():
    # The path the worker took: SearchGaveUp raised from the Korail error, escaping the entry point.
    done = run_child(
        "import korail_bot.mobile.worker\n"
        + CHAIN
        + """
from korail_bot.services.rail_service import SearchGaveUp
try:
    raise cause
except Exception as error:
    raise SearchGaveUp("blocked", f"{type(error).__name__}: {error}") from error
"""
    )

    assert_child_clean(done)
    assert "SearchGaveUp" in done.stderr


def test_an_uncaught_exception_in_a_thread_is_printed_masked():
    done = run_child(
        "import threading\nimport korail_bot\n"
        + CHAIN
        + """
def run():
    raise RuntimeError(f"gave up: {cause}") from cause
thread = threading.Thread(target=run, name="watch")
thread.start()
thread.join()
raise SystemExit(3)
"""
    )

    assert done.returncode == 3
    assert "S000" in done.stderr
    assert not MARKERS.search(done.stderr), done.stderr
    assert "Exception in thread watch" in done.stderr


def test_an_exception_that_cannot_be_raised_is_printed_masked():
    done = run_child(
        "import korail_bot\n"
        + CHAIN
        + """
class Holder:
    def __del__(self):
        raise cause
Holder()
raise SystemExit(4)
"""
    )

    assert done.returncode == 4
    assert not MARKERS.search(done.stderr), done.stderr


def test_a_keyboard_interrupt_prints_its_chain_masked_and_not_the_raw_context():
    done = run_child(
        "import korail_bot\n"
        + CHAIN
        + """
try:
    raise cause
except Exception:
    raise KeyboardInterrupt
"""
    )

    assert done.returncode != 0
    assert "KeyboardInterrupt" in done.stderr
    assert "S000" in done.stderr
    assert not MARKERS.search(done.stderr), done.stderr


UNCAUGHT = """
try:
    raise cause
except Exception as error:
    raise RuntimeError(f"gave up: {error}") from error
"""

BROKEN_STDERR = """
import sys

class Broken:
    calls = 0
    def write(self, text):
        Broken.calls += 1
        if Broken.calls <= LIMIT and not sys.is_finalizing():
            raise BlockingIOError(11, "write could not complete without blocking")
        return len(text)
    def flush(self):
        # (Python flushes stderr once more when it exits; that one is not the hook's.)
        if Broken.calls <= LIMIT and not sys.is_finalizing():
            raise BlockingIOError(11, "flush could not complete without blocking")

sys.stderr = Broken()
"""


@pytest.mark.parametrize("limit", [1, 1_000_000])
def test_a_stderr_that_fails_does_not_bring_back_the_unmasked_exception(limit):
    done = run_child(
        "import korail_bot\n" + CHAIN + BROKEN_STDERR.replace("LIMIT", str(limit)) + UNCAUGHT
    )

    # Python's own exit status for an uncaught exception, whatever the hook did.
    assert done.returncode == 1
    assert "Error in sys.excepthook" not in done.stderr
    assert not MARKERS.search(done.stderr), done.stderr
    assert not MARKERS.search(done.stdout)


def test_a_thread_whose_stderr_fails_does_not_bring_back_the_unmasked_exception():
    done = run_child(
        "import threading\nimport korail_bot\n"
        + CHAIN
        + BROKEN_STDERR.replace("LIMIT", "1000000")
        + """
def run():
    raise RuntimeError(f"gave up: {cause}") from cause
thread = threading.Thread(target=run)
thread.start()
thread.join()
raise SystemExit(5)
"""
    )

    assert done.returncode == 5
    assert not MARKERS.search(done.stderr), done.stderr


def test_a_masking_that_fails_prints_a_fixed_line_and_names_nothing():
    done = run_child(
        "import korail_bot\nimport korail_bot.utils.log_redaction as hook\n"
        + CHAIN
        + """
def broken(text):
    raise RuntimeError("masking failed")
hook.redact_text = broken
"""
        + UNCAUGHT
    )

    assert done.returncode == 1
    assert "details redacted; logging failed" in done.stderr
    assert not MARKERS.search(done.stderr), done.stderr


def test_an_ordinary_uncaught_exception_still_exits_with_status_1():
    done = run_child("import korail_bot\nraise RuntimeError('boom')")

    assert done.returncode == 1
    assert "RuntimeError: boom" in done.stderr
