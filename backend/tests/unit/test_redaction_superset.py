"""
korail_bot.utils.redaction masks everything korail-mobile-api 1.1.1's redactor did, and more.

The reference is a verbatim copy of 1.1.1's module (korail_bot/utils/_redaction_1_1_1.py), which the
project's redact_text runs first and builds on, so the superset holds by construction. These tests
are what keeps it so: the copy is pinned by digest, and generated corpora - single rules, and every
combination of rules in every order - are held to "whatever the reference hides, so do we".
"""

import collections
import hashlib
import os
import random
import re
from pathlib import Path

import pytest

from korail_bot.utils import _redaction_1_1_1 as reference
from korail_bot.utils.redaction import redact_text

SEPARATORS = ["", " ", "-", "  ", " - ", "--", "---", ".", "/", " ", "–", " — ", "- -"]
CARD_PREFIXES = [
    "", "-", ":", "=", " ", "카드번호", "카드번호-", "카드번호 ", "txtPwd=", '"', "'", "https://h/p?q=",
    "x ", "code=", "[", "(", "card:", "Card No. ", "S000: ", "id=-", "n=", "chat_id=-",
]  # fmt: skip
CARD_SUFFIXES = ["", "-", ".", " ok", '"', "'", "&a=1", ")", "카드", " 4", ",", "\n", "abc"]


def luhn_complete(body: str) -> str:
    total = 0
    for index, character in enumerate(reversed(body)):
        number = int(character)
        if index % 2 == 0:
            number = number * 2 - 9 if number > 4 else number * 2
        total += number
    return body + str((10 - total % 10) % 10)


def card_corpus(count=6000):
    chooser = random.Random(20261023)
    corpus = []
    for index in range(count):
        length = chooser.randint(13, 19)
        digits = "".join(chooser.choice("0123456789") for _ in range(length - 1))
        number = luhn_complete(digits) if index % 2 == 0 else digits + chooser.choice("0123456789")
        # One separator for every gap, or a fresh one in each.
        if chooser.random() < 0.5:
            gap = chooser.choice(SEPARATORS)
            shown = gap.join(number)
        else:
            shown = "".join(c + chooser.choice(SEPARATORS) for c in number[:-1]) + number[-1]
        group = chooser.choice([4, 4, 4, 5, 6])
        if chooser.random() < 0.5:
            gap = chooser.choice(SEPARATORS)
            shown = gap.join(number[i : i + group] for i in range(0, len(number), group))
        text = chooser.choice(CARD_PREFIXES) + shown + chooser.choice(CARD_SUFFIXES)
        corpus.append((shown, text))
    return corpus


VALUES = ["SYNTHETIC_VALUE", "abc123XYZ", "한글값입니다", "a.b-c_d/e=f"]


def key_corpus():
    corpus = []
    for key in sorted(reference.SENSITIVE_KEYS):
        for form in (
            "{k}={v}",
            "{k}: {v}",
            '"{k}":"{v}"',
            "'{k}': '{v}'",
            "?a=1&{k}={v}&b=2",
            "{K}={v}",
            "text before {k}={v} text after",
            '{{"x": 1, "{k}": "{v}"}}',
            "S000: {k}={v}",
        ):
            for value in VALUES[:2] if "{K}" not in form else VALUES[:1]:
                corpus.append((value, form.format(k=key, K=key.upper(), v=value)))
    for form in (
        "JSESSIONID={v}",
        "jsessionid={v}; Path=/",
        "Cookie: JSESSIONID={v}",
        "Set-Cookie: JSESSIONID={v}; HttpOnly",
        "x=1&JSESSIONID={v}&y=2",
        "Cookie: {v}",
    ):
        for value in VALUES:
            corpus.append((value, form.format(v=value)))
    return corpus


CARDS = card_corpus()
KEYS = key_corpus()


def test_the_corpus_is_large():
    assert len(CARDS) + len(KEYS) >= 10000


def test_every_card_the_original_masks_is_masked():
    unmasked_by_reference = 0
    failures = []
    for shown, text in CARDS:
        if shown in reference.redact_text(text):
            unmasked_by_reference += 1
            continue
        if shown in redact_text(text):
            failures.append(text)
    assert not failures, failures[:10]
    # The comparison has substance: the reference masks a good part of the corpus. (It misses the
    # rest - a dot between groups, a letter before the digits - which the next test holds to account.)
    assert len(CARDS) - unmasked_by_reference > 2000


def test_every_card_is_masked_however_it_is_written():
    # Beyond the original: whatever it missed, a card is masked anyway.
    left = [text for shown, text in CARDS if shown in redact_text(text)]
    assert not left, left[:10]


def test_every_credential_the_original_masks_is_masked():
    failures = []
    for secret, text in KEYS:
        if secret in reference.redact_text(text):
            continue
        if secret in redact_text(text):
            failures.append(text)
    assert not failures, failures[:10]


@pytest.mark.parametrize(
    "text",
    [
        "4111  1111  1111  1111",
        "4111 - 1111 - 1111 - 1111",
        "4111--1111--1111--1111",
        "4111 1111 1111 1111",
        "4111–1111–1111–1111",
        "카드번호4111111111111111",
        "카드번호-4111111111111111",
        "-4111111111111111",
        "4111111111111111111",
    ],
)
def test_a_card_is_masked_however_it_is_written(text):
    assert "[REDACTED_CARD]" in redact_text(text)
    assert "1111" not in redact_text(text).replace("[REDACTED_CARD]", "")


def test_ids_and_codes_that_are_not_cards_are_left_alone():
    # 1.1.1 masks any 13-19 digit run, so the app's negative chat ids (16 digits) are masked too.
    assert redact_text("chat_id=-2637222897571620") == "chat_id=-[REDACTED_CARD]"
    assert redact_text("code=-8204 h_msg_cd: -2000 port 8081 pid 12345") == (
        "code=-8204 h_msg_cd: -2000 port 8081 pid 12345"
    )
    assert redact_text("n=123456789012 at 12:34:56") == "n=123456789012 at 12:34:56"


VENDORED = Path(reference.__file__)
VENDORED_DIGEST = "6c36dd9dd45633776a61ec58095bf7251a91b8335b867779b1371710cd51f04c"


def test_the_vendored_original_is_the_file_that_was_vendored():
    assert hashlib.sha256(VENDORED.read_bytes()).hexdigest() == VENDORED_DIGEST


def test_redact_text_is_the_originals_output_with_the_rules_applied_after_it():
    from korail_bot.utils.redaction import _extra_rules

    for _, text in CARDS[:300] + KEYS[:300]:
        assert redact_text(text) == _extra_rules(reference.redact_text(text)) or "%" in text


# ------------------------------------------------ rules in combination

# Every piece is a way a secret turns up; {m} is the secret, unique to the piece.
PIECES = [
    "Bearer {m}",
    "Basic {m}",
    "bearer {m}",
    "Authorization: Bearer {m}",
    "Authorization: {m}",
    "Authorization={m}",
    "Cookie: {m}",
    "Cookie: a=b; JSESSIONID={m}",
    "Set-Cookie: JSESSIONID={m}; Path=/",
    "Set-Cookie: {m}",
    "txtPwd={m}",
    "txtPwd: {m}",
    "JSESSIONID={m}",
    "jsessionid={m}",
    "sid={m}",
    "SID: {m}",
    "token={m}",
    "access_token={m}",
    "secret={m}",
    "?a=1&txtPwd={m}&b=2",
    "?a=1&sid={m}&b=2",
    "https://h/p?strMbCrdNo={m}",
    "txt%50wd={m}",
    "next=txtPwd%3D{m}",
    "next=sid%3D{m}",
    "txtPwd={m}%20tail",
    "txtPwd%3D{m}",
    '{{"txtPwd": "{m}"}}',
    '{{"sid":"{m}","x":1}}',
    '"token": "{m}"',
    "'txtPwd': '{m}'",
    "Bearer txtPwd={m}",
    "Basic JSESSIONID={m}",
    "Bearer sid={m}",
    "Cookie: txtPwd={m}",
    "Authorization: Bearer txtPwd={m}",
    "Bearer Basic {m}",
    "txtPwd=Bearer {m}",
    "sid=Basic {m}",
    "txtMemberNo={m}&txtPwd=",
    "card {m}",
    "카드번호-{m}",
    "카드번호{m}",
]
JOINERS = [" ", ", ", "; ", "&", "\n", " | ", "", "?", " - ", ": ", "\t", "=", "%", " %20 "]
WRAPPERS = ["", "S000: ", "Error: ", "KorailAppError: ", "카드번호 ", "rtnMsg=", "(", '"']
FUZZ_CASES = int(os.environ.get("JARI_REDACTION_FUZZ", "12000"))


def card_text(chooser):
    digits = "".join(chooser.choice("0123456789") for _ in range(chooser.randint(13, 19) - 1))
    number = luhn_complete(digits)
    gap = chooser.choice(SEPARATORS)
    return gap.join(number)


def combination_corpus(count, seed=20261024):
    chooser = random.Random(seed)
    for index in range(count):
        size = chooser.choice([1, 2, 2, 3, 3, 4])
        markers = []
        parts = []
        for slot in range(size):
            piece = chooser.choice(PIECES)
            if "card" in piece or "카드번호" in piece:
                marker = card_text(chooser)
            else:
                marker = f"SYNTHETIC_{index}_{slot}"
            markers.append(marker)
            parts.append(piece.format(m=marker))
        text = chooser.choice(WRAPPERS)
        for part in parts:
            text += part + chooser.choice(JOINERS)
        yield markers, text


def test_every_combination_of_rules_hides_what_the_original_hides():
    cases = 0
    original_hid = 0
    only_ours_hid = 0
    failures = []
    for markers, text in combination_corpus(FUZZ_CASES):
        cases += 1
        original = reference.redact_text(text)
        ours = redact_text(text)
        for marker in markers:
            if marker not in original:
                original_hid += 1
                if marker in ours:
                    failures.append(text)
            elif marker not in ours:
                only_ours_hid += 1
        if len(failures) > 10:
            break
    assert not failures, failures[:10]
    assert cases == FUZZ_CASES
    # The comparison has substance, and the project's rules add to the original's.
    assert original_hid > FUZZ_CASES // 2
    assert only_ours_hid > FUZZ_CASES // 10


WORD = re.compile(r"\w+")


def test_ours_never_shows_a_word_of_the_text_more_often_than_the_original_does():
    # Words, not just the markers: a card's digit groups, a key's value, a token, anywhere in a
    # combination. Where the original removed a word, ours has not put one back.
    failures = []
    for _, text in combination_corpus(FUZZ_CASES, seed=7):
        if "%" in text:
            continue  # decoding rewrites the text around the secrets; the marker test covers these
        original = collections.Counter(WORD.findall(reference.redact_text(text)))
        ours = collections.Counter(WORD.findall(redact_text(text)))
        for word in set(WORD.findall(text)):
            if not (any(c.isdigit() for c in word) or "SYNTHETIC" in word):
                continue  # a plain word ("카드번호") can come loose of a digit it was glued to
            if ours[word] > original[word]:
                failures.append((word, text))
    assert not failures, failures[:10]
