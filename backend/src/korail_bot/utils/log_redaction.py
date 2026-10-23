"""
Credentials never reach a log: every record is masked as it is made.

korail_mobile_api 1.1.1 ran each exception message through its own redactor, so a Korail reply that
quoted the request (a password, a session id) stayed out of any ``%s`` of the exception. 2.x puts the
reply's text in ``str(exc)`` as it came. Masking at each call site would miss the next one, so the
masking sits where every log line passes: the record factory. It covers the message, the traceback
with its ``__cause__``/``__context__`` chain and the stack of every record, whatever handler or
formatter reads it afterwards (the server, the workers, the bot, the e2e stack, a test's caplog).

Installed when :mod:`korail_bot` is imported, so every entry point has it before it logs.
"""

import logging

from korail_bot.utils.redaction import redact_text

_FORMATTER = logging.Formatter()


def _redacted(record: logging.LogRecord) -> logging.LogRecord:
    try:
        message = record.getMessage()
        masked = redact_text(message)
        if masked != message:
            record.msg, record.args = masked, ()
        if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
            # Formatter.format keeps an exc_text that is already there, in place of formatting
            # the exception again, so every formatter after this one prints the masked text.
            record.exc_text = redact_text(_FORMATTER.formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
    except Exception:
        record.msg, record.args, record.exc_info, record.exc_text = (
            "[log record withheld: it could not be masked]",
            (),
            None,
            None,
        )
    return record


def install() -> None:
    """Wrap the log record factory once; calling it again changes nothing."""
    factory = logging.getLogRecordFactory()
    if getattr(factory, "redacts_credentials", False):
        return

    def make_record(*args, **kwargs) -> logging.LogRecord:
        return _redacted(factory(*args, **kwargs))

    make_record.redacts_credentials = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(make_record)
