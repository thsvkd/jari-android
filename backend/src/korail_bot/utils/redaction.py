"""
Masks credentials and identifiers in text bound for a log.

``redact_text(text)`` is ``extra_rules(redact_text_1_1_1(text))``. The first half is korail-mobile-api
1.1.1's own redactor, copied verbatim (``_redaction_1_1_1.py``; Copyright (c) 2026 yakisoba0728,
Apache License 2.0, https://github.com/yakisoba0728/korail-mobile-api; license and notice:
``backend/LICENSES/korail-mobile-api.txt``). The library ran every exception message through it; 2.x
dropped it and puts Korail's own text into ``str(exc)`` as it came, so the project masks at the
logging boundary instead (:mod:`korail_bot.utils.log_redaction`).

What the project adds runs on the *output* of the original and may only put a redaction marker where
text was. It never reads the original's input again, so no rule can consume what another rule would
have hidden - the original's masking is a floor under whatever follows, by construction, and the
rules below are free to be blunt:

- a run of 13-19 digits is a card however it is separated, or whatever letter stands before it;
- Bearer/Basic tokens, whole Cookie/Set-Cookie lines, and a few more names (sid, token, secret, ...);
- a secret written percent-encoded: the text decoded is masked by the original and by the rules
  above, and used instead when that found something more.

Not a rule, a consequence: the original masks any 13-19 digit run, so the app's negative chat ids
(``chat_id=-2637222897571620``) come out as ``chat_id=-[REDACTED_CARD]`` in the log.
"""

import re
from urllib.parse import unquote

from korail_bot.utils._redaction_1_1_1 import redact_text as redact_text_1_1_1

# 1.1.1's card rule is "13-19 digits, spaces and hyphens between them, word boundaries around". Its
# blind spots are the edges: a letter before the digits ("카드번호4111…"), and separators other than
# a space or hyphen. The separators here add dots, slashes, the no-break space and the Unicode
# dashes; colons and commas are not separators, so a timestamp ("12:34:56") is not a card.
_SEPARATORS = r"[ \-./ ‐-―−]"
CARD_RE = re.compile(rf"(?<!\d)(?:\d{_SEPARATORS}*?){{13,19}}(?!\d)")

BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/\-]+=*")
COOKIE_RE = re.compile(r"(?i)\b((?:set-)?cookie\s*[:=]\s*)[^\r\n]*")

# Names the original does not know. Same shape as its rule: the name (quoted or not), "=" or ":", then
# a quoted string or everything up to a space or comma.
EXTRA_KEYS = (
    "sid",
    "authorization",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "passwd",
    "sessionid",
    "session_id",
)
EXTRA_KEY_VALUE_RE = re.compile(
    r"(?P<prefix>(?<![\w-])(?P<key_quote>[\"']?)(?:"
    + "|".join(sorted((re.escape(key) for key in EXTRA_KEYS), key=len, reverse=True))
    + r")(?P=key_quote)(?![\w-])\s*(?:=|:)\s*)"
    + r'(?P<value>"(?:\\.|[^"\\])*(?:"|$)'
    + r"|'(?:\\.|[^'\\])*(?:'|$)"
    + r"|[^\s,]+)",
    re.IGNORECASE,
)


def _mask_value(match: re.Match[str]) -> str:
    value = match.group("value")
    quote = value[0] if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0] else ""
    return f"{match.group('prefix')}{quote}[REDACTED]{quote}"


def _extra_rules(text: str) -> str:
    """The project's rules. Each one replaces what it matches with a marker; none reads anything else."""
    text = COOKIE_RE.sub(r"\1[REDACTED]", text)
    text = BEARER_RE.sub(r"\1 [REDACTED]", text)
    text = EXTRA_KEY_VALUE_RE.sub(_mask_value, text)
    return CARD_RE.sub("[REDACTED_CARD]", text)


# Percent-escapes that stay escaped when the text is decoded: a space or comma ends the value of
# "name=value" for the original's rule, so decoding "a%20b" would leave "b" outside the mask.
_KEEP_ESCAPED = re.compile(r"%(?=(?:09|0[a-dA-D]|20|2[cC]|[aA]0))")
_ESCAPED_MARK = "\ue000"


def _decoded(text: str) -> str:
    protected = _KEEP_ESCAPED.sub(_ESCAPED_MARK, text)
    return unquote(protected).replace(_ESCAPED_MARK, "%")


def redact_text(value: str) -> str:
    """The text with credentials, session ids, identifiers and card numbers hidden."""
    masked = _extra_rules(redact_text_1_1_1(value))
    if "%" in masked:
        # A secret written percent-encoded ("txt%50wd=…", "next=txtPwd%3D…") is invisible to every
        # rule above. What is left of the text is decoded and masked again; if that found something,
        # it is the result. Decoding only touches what was not already hidden (a marker has no "%").
        decoded = _decoded(masked)
        if decoded != masked:
            again = _extra_rules(redact_text_1_1_1(decoded))
            if again != decoded:
                return again
    return masked
