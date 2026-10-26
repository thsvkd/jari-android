"""What korail_mobile_api raises on Korail's replies, made by the library itself."""

from korail_mobile_api.read_parsers import parse_reservation_history_response

# The reply an account got on pit5 from 2026-10-19 02:57 UTC, after Korail had restricted it:
# JSON, but with no strResult.
BLOCKED_REPLY = {"h_msg_cd": "", "h_msg_txt": "", "rtnMsg": "비정상적인 접근입니다."}


def blocked_reply():
    """The exception the library raises on a typed read of a reply without strResult."""
    try:
        parse_reservation_history_response(BLOCKED_REPLY)
    except Exception as exc:
        return exc
    raise AssertionError("the parser took a reply without strResult")
