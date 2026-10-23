"""
Masks credentials and identifiers in text bound for a log.

Vendored from korail-mobile-api 1.1.1 (``korail_mobile_api/redaction.py``,
Copyright (c) 2026 yakisoba0728, Apache License 2.0, https://github.com/yakisoba0728/korail-mobile-api;
license and notice: ``backend/LICENSES/korail-mobile-api.txt``).
The library ran every exception message through it; 2.x dropped it and puts Korail's own text into
``str(exc)`` as it came, so the project masks at the logging boundary instead
(:mod:`korail_bot.utils.log_redaction`). Changes from the original: the mutation-preview helpers are gone,
a few session/token names are added, a percent-encoded secret is read through, and a bare
13-19 digit number is taken for a card only when it passes the Luhn check and is not the negative id of
``chat_id=-…`` (the app's chat ids are negative integers that a card pattern would otherwise swallow).
"""

import re
from urllib.parse import unquote

_MAX_PASSENGERS = 9

SENSITIVE_KEYS = frozenset(
    key.casefold()
    for key in {
        # --- 인증·세션 ---
        "txtMemberNo",
        "txtPwd",
        "password",
        "JSESSIONID",
        "Cookie",
        "Set-Cookie",
        # --- 응답 메시지(서버가 입력을 인용할 수 있으므로) ---
        "h_msg_txt",
        # --- PNR·예약 식별 ---
        "rsvCnt",
        "pnrNo",
        "hidPnrNo",
        "txtPnrNo",
        "txtPrnNo",
        "pnr_no",
        "prnNo",  # 반환 응답 철자(RefundVerifyTicketDao.java:123,151)
        "h_pnr_no",
        "coptEntRsvNo",
        "pbpRsvNo",
        "pbp_reservation_no",
        "strVrRsvNo",
        "txtVrRsNo",
        "txtVrRsvSqNo",
        "h_vr_rsv_no",
        "virtual_reservation_no",
        "reservation_count",
        # --- 결제·정산 식별 ---
        "tkRetPwd",
        "tkRetNo",
        "h_tk_ret_no",
        "ticket_return_no",
        "return_password",
        "saleWctNo",
        "saleDt",
        "saleSqno",
        "saleDd",  # 세 번째 철자(PaymentService.java:12-14)
        "sale_window_no",
        "sale_date",
        "sale_sequence",
        "h_wct_no",
        "hidWctNo",
        "window_no",
        "h_orgtk_sale_dt",
        "h_orgtk_ret_sale_dt",
        "h_orgtk_wct_no",
        "h_orgtk_sale_sqno",
        "h_orgtk_ret_pwd",
        "h_orgtk_tk_ret_pwd",
        "h_orgtk_sale_wct_no",
        "original_sale_date",
        "original_window_no",
        "original_sale_sequence",
        "original_return_password",
        "h_lump_stl_tgt_no",
        "lump_sum_target_no",
        "lumpStlTgtNo",
        "lump_settlement_target_no",
        "lump_settlement_target_nos",
        # --- 카드·결제수단(PaymentMethod 맵, CARD_RE 가 못 잡는 변형) ---
        "hidStlCrCrdNo1",
        "hidVanPwd1",
        "hidCrdVlidTrm1",
        "hidAthnVal1",
        "hidAthnDvCd1",
        "hidIsmtMnthNum1",
        "hidCrdInpWayCd1",
        "mbCrdNo",
        "strMbCrdNo",
        "member_card_no",
        "h_stl_mb_crd_no",
        "h_stl_crd_no",
        "card_no",
        "stlCrdNo",
        "prepCrdNo",
        "prepaid_card_no",
        "settlement_card_no",
        "h_acnt_no",
        "account_no",
        "h_apv_no",
        "apvNo",
        "approval_no",
        "h_xpot_no",
        "point_no",
        # --- 고객 식별(회원번호·이름·전화·생년) ---
        "custMgNo",
        "custMgNo_",  # 인덱스 기본(custMgNo_1 등)
        "acepCustMgFlg",
        "acepCustMgNo",
        "acepCustNm",
        "acepCustTeln",
        "acepCustTeln2",
        "h_cust_no",
        "hidCustNo",
        "strCustNo",
        "encryptCustNo",
        "customer_no",
        "acceptance_customer_management_flag",
        "acceptance_customer_management_no",
        "acceptance_customer_name",
        "acceptance_customer_phone",
        "acceptance_customer_phone_2",
        "strCpNo",  # 로그인 응답 전화번호(LoginDao.java:84-107)
        "strCustNm",
        "strBtdt",
        "strEmailAdr",
        "txtCpNo",  # 예약대기 전화번호(ReservationWaitService.java:12)
        "custTeln",  # 비회원 반환 전화번호(s5/h.java:123)
        # --- 할인카드(N카드) ---
        # h_dcnt_crd_no 는 bearer credential(w4/a.java:100-101)
        "dcntCrdNo",
        "h_dcnt_crd_no",
        "discount_card_no",
        *(f"txtCardNo_{i}" for i in range(1, _MAX_PASSENGERS + 1)),
        "txtCardNo",
        # 할인카드 등록(NCardReservationDao.java:16,29,30)
        "apdCustName",
        "apdCustTeln",
        # --- 승객유형명(코드가 아닌 사람이 읽는 라벨) ---
        # 정책: 사람이 읽는 값은 가리고 코드(psg_tp_dv_cd)는 남긴다.
        "psgTpDvNm",
        "psgTpNm",
        "h_psg_tp_nm",
        "h_dcnt_knd_nm",
        "h_subt_dcs_cl_nm",
        "passenger_type_division_name",
        "member_division_name",
        "acceptance_kind_name",
        "mbDvNm",
        "pbpAcepKndNm",
        # --- 좌석·객실·플랫폼 ---
        "psrmClCd",
        "psrmClNm",
        "psrm_cl_cd",
        "psrm_cl_nm",
        "room_class_code",
        "room_class_name",
        "scarNo",
        "scar_no",
        "seatNo",
        "plfNo",
        "car_no",
        "seat_no",
        "platform_no",
        "h_srcar_no",
        "h_seat_no",
        "h_plf_no",
        # 좌석 지정 출력(SeatSearchActivity.java:679-680)
        *(f"txtSrcarNo{i}" for i in range(1, _MAX_PASSENGERS + 1)),
        *(f"txtSeatNo{i}" for i in range(1, _MAX_PASSENGERS + 1)),
        "txtSrcarNo",
        "txtSeatNo",
        # --- 쿠폰·할인증명·임시직렬 ---
        "h_coup_no",
        "h_cpn_no",
        "h_cert_pwd",
        "coupon_no",
        "certificate_password",
        "hidDscpNo",
        "hidFmlyNo",  # 다자녀 가족(a6/C1041A.java:75)
        "hidRsvChgNo",
        "h_rsv_chg_no",
        "reservation_change_no",
        "h_tmp_job_sqno1",
        "h_tmp_job_sqno2",
        "hidTmpJobSqno1",
        "hidTmpJobSqno2",
        "temporary_job_sequence_1",
        "temporary_job_sequence_2",
        "temporary_job_sequence",
        "tmpJobSqno",
        "chgTno",
        # --- DynaPath·부가서비스·기타 ---
        # (jari: 세션·토큰 이름 추가)
        "sid",
        "authorization",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "passwd",
        "sessionid",
        "session_id",
        "x-dynapath-m-token",
        "mutMrkVrfCd",
        "verification_code",
        "addSrvDvCd",
        "additional_service_code",
        "addSrvReqNo",
        "partner_reservation_no",
        "jrnyTpCd",
        "journey_type_code",
        "regDt",
        "registered_date",
        "wdrwPsbFlg",
        "withdrawal_possible_flag",
        "url",
        "image",
        "menuUrl",
        "contUrl",
        "contImage",
        # --- 구매자·동반자·좌석그룹 이름 ---
        "h_sgr_nm",
        "h_buy_ps_nm",
        "h_compa_nm",
        "h_compa_brth",
        "h_comp_nm",
        "h_comp_cert_no",
        "h_wct_nm",
        "seat_group_name",
        "buyer_name",
        "companion_name",
        "companion_birth_date",
        "window_name",
        "certificate_no",
        # --- 반환번호 4분할(RefundService.java:33) ---
        # 16자리가 5/4/5/2 로 분할되어 CARD_RE 에 안 걸림
        "retNo1",
        "retNo2",
        "retNo3",
        "retNo4",
        "retNo",
        "return_no_1",
        "return_no_2",
        "return_no_3",
        "return_no_4",
        "return_no",
        "strName",  # 요청자 이름(s5/c.java:71)
        "requester_name",
        "requester_phone",
        # --- 원표 4분할(ROrtg.java:8-11, RefundService.java:17) ---
        "ogtkSaleDt",
        "ogtkSaleDd",
        "ogtkSaleWctNo",
        "ogtkSaleSqno",
        "ogtkRetPwd",
        "ogtk_ret_pwd",
        "ogtk_sale_dt",
        "ogtk_sale_sqno",
        "ogtk_sale_wct_no",
        "original_sale_datetime",
        # --- 지연증명 원표(response/research/Cmpn.java:11-14) ---
        "dlayOgtkRetPwd",
        "dlayOgtkSaleDt",
        "dlayOgtkSaleSqno",
        "dlayOgtkWctNo",
        "delay_certificate_return_password",
        "delay_certificate_sale_date",
        "delay_certificate_sale_sequence",
        "delay_certificate_window_no",
        # --- 구매내역 ---
        "h_purchase_history",
        # --- poppMsg(서버 합성 안내, 입력 인용 가능, RefundVerifyTicketDao.java:66) ---
        "poppMsg",
        "popup_message",
        # --- 이중 인덱스 키(outer=journey/passenger, inner=seat/discount row) ---
        # _index_stripped 는 인덱스 하나만 떼므로 이중 인덱스는 열거 필요
        *(
            f"{prefix}{outer}_{inner}"
            for prefix in (
                "scarNo_",
                "seatNo_",
                "roomClsfCd_",
                "seatPsrmClCd_",
                "dscpNo_",
                "dlayOgtkWctNo_",
                "dlayOgtkSaleDd_",
                "dlayOgtkSaleSqno_",
                "dlayOgtkRetPwd_",
            )
            for outer in range(1, _MAX_PASSENGERS + 1)
            for inner in range(1, _MAX_PASSENGERS + 1)
        ),
    }
)


_INDEX_SUFFIX_RE = re.compile(r"^(?P<base>.*?)_?(?P<index>\d+)$")


def _index_stripped(name: str) -> str | None:
    """Name without its trailing row index, or None."""
    match = _INDEX_SUFFIX_RE.match(name)
    if match is None:
        return None
    return match.group("base") or None


def is_sensitive_key(name: str) -> bool:
    """Whether ``name`` names a sensitive value: case-insensitive, trailing row index ignored."""
    folded = name.casefold()
    if folded in SENSITIVE_KEYS:
        return True
    base = _index_stripped(folded)
    return base is not None and base in SENSITIVE_KEYS


# Digits and the separators a card number is written with; a candidate is a card when it passes Luhn,
# a hyphen before it or not ("카드번호-4111…" is a card). The one exemption is a negative id named as
# one: the app's chat ids are negative integers ("chat_id=-2637222897571620"), and one in ten of
# them would pass Luhn by chance.
CARD_RE = re.compile(r"(?<!\w)\d(?:[ -]?\d){12,18}(?!\w)")
_NEGATIVE_ID_BEFORE = re.compile(r"(?i)(?:chat_?id|owner|user_?id|\bid)\s*[=:]\s*-$")
SESSION_RE = re.compile(r"(?i)(JSESSIONID=)[^&;\s]+")
BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/\-]+=*")
COOKIE_RE = re.compile(r"(?i)\b((?:set-)?cookie\s*[:=]\s*)[^\r\n]*")
SENSITIVE_KEY_VALUE_RE = re.compile(
    r"(?P<prefix>(?<![\w-])(?P<key_quote>[\"']?)(?:"
    + "|".join(sorted((re.escape(key) for key in SENSITIVE_KEYS), key=len, reverse=True))
    + r")(?P=key_quote)(?![\w-])\s*(?:=|:)\s*)"
    + r'(?P<value>"(?:\\.|[^"\\])*(?:"|$)'
    + r"|'(?:\\.|[^'\\])*(?:'|$)"
    + r"|[^\s,]+)",
    re.IGNORECASE,
)


def _luhn(digits: str) -> bool:
    total = 0
    for index, character in enumerate(reversed(digits)):
        number = int(character)
        if index % 2:
            number = number * 2 - 9 if number > 4 else number * 2
        total += number
    return total % 10 == 0


def _redact_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if _NEGATIVE_ID_BEFORE.search(match.string, 0, match.start()):
        return match.group(0)
    return "[REDACTED_CARD]" if 13 <= len(digits) <= 19 and _luhn(digits) else match.group(0)


def _redact_sensitive_key_value(match: re.Match[str]) -> str:
    value = match.group("value")
    quote = value[0] if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0] else ""
    return f"{match.group('prefix')}{quote}[REDACTED]{quote}"


def _redact(text: str) -> str:
    # Schemes and cookies first: "Authorization: Bearer abc" would otherwise be read as the value "Bearer".
    redacted = COOKIE_RE.sub(r"\1[REDACTED]", text)
    redacted = BEARER_RE.sub(r"\1 [REDACTED]", redacted)
    redacted = SENSITIVE_KEY_VALUE_RE.sub(_redact_sensitive_key_value, redacted)
    redacted = SESSION_RE.sub(r"\1[REDACTED]", redacted)
    return CARD_RE.sub(_redact_card, redacted)


def redact_text(value: str) -> str:
    """The text with credentials, session ids, identifiers and card numbers hidden.

    A secret written percent-encoded (``txt%50wd=…``, ``next=txtPwd%3D…``) is read through: when
    decoding it exposes something to hide, the decoded, masked text is returned instead.
    """
    if "%" in value:
        decoded = unquote(value)
        if decoded != value:
            masked = _redact(decoded)
            if masked != decoded:
                return masked
    return _redact(value)
