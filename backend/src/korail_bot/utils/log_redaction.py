"""
Credentials never reach a log: every record is masked as it is made.

korail_mobile_api 1.1.1 ran each exception message through its own redactor, so a Korail reply that
quoted the request (a password, a session id) stayed out of any ``%s`` of the exception. 2.x puts the
reply's text in ``str(exc)`` as it came. Masking at each call site would miss the next one, so the
masking sits where every log line passes: the record factory. It covers the message, the traceback
with its ``__cause__``/``__context__`` chain and the stack of every record, whatever handler or
formatter reads it afterwards (the server, the workers, the bot, the e2e stack, a test's caplog).

Installed when :mod:`korail_bot` is imported, so every entry point has it before it logs.

An exception nothing catches never goes through logging: Python's own hooks print it to stderr, with
the same ``str(exc)`` and cause chain. The excepthooks of the main thread and of every other thread,
and the hook for errors that cannot be raised (a ``__del__``, a callback), are replaced here by ones
that print the same text masked.
"""

import logging
import sys
import threading
import traceback

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


def _masked_traceback(exc_type, exc, tb) -> str:
    """The traceback Python would print, cause and context chain and notes included, masked."""
    try:
        return redact_text("".join(traceback.format_exception(exc_type, exc, tb)))
    except Exception:
        return f"{getattr(exc_type, '__name__', 'Exception')}: [withheld: it could not be masked]\n"


def _write(text: str) -> None:
    stream = sys.stderr
    if stream is not None:
        stream.write(text)
        stream.flush()


def _excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)  # nothing in it to hide
        return
    _write(_masked_traceback(exc_type, exc, tb))


def _thread_excepthook(args) -> None:
    if issubclass(args.exc_type, SystemExit):
        return
    name = args.thread.name if args.thread is not None else "<unknown>"
    _write(
        f"Exception in thread {name}:\n"
        + _masked_traceback(args.exc_type, args.exc_value, args.exc_traceback)
    )


def _unraisablehook(unraisable) -> None:
    message = unraisable.err_msg or "Exception ignored in"
    _write(redact_text(f"{message}: {unraisable.object!r}")[:300] + "\n")
    if unraisable.exc_type is not None:
        _write(
            _masked_traceback(unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback)
        )


def install() -> None:
    """Wrap the log record factory and the excepthooks once; calling it again changes nothing."""
    if sys.excepthook is sys.__excepthook__:
        sys.excepthook = _excepthook
    if threading.excepthook is threading.__excepthook__:
        threading.excepthook = _thread_excepthook
    if sys.unraisablehook is sys.__unraisablehook__:
        sys.unraisablehook = _unraisablehook
    factory = logging.getLogRecordFactory()
    if getattr(factory, "redacts_credentials", False):
        return

    def make_record(*args, **kwargs) -> logging.LogRecord:
        return _redacted(factory(*args, **kwargs))

    make_record.redacts_credentials = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(make_record)
