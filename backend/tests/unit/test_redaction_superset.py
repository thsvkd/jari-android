"""
korail_bot.utils.redaction masks everything korail-mobile-api 1.1.1's redactor did, and more.

The reference is a verbatim copy of 1.1.1's module (_redaction_1_1_1.py). A corpus of texts is made
with a known secret in each; wherever the reference hides the secret, so must the project's. The
corpus is generated, not hand-picked, so a separator or a prefix nobody thought of is still tried.
"""

import random

import pytest

from korail_bot.utils.redaction import redact_text

from . import _redaction_1_1_1 as reference

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


NAMED_NEGATIVE_ID = "chat_id=-"
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
        if text.startswith(NAMED_NEGATIVE_ID):
            continue  # the one number deliberately let through; see CARD_RE
        if shown in reference.redact_text(text):
            unmasked_by_reference += 1
            continue
        if shown in redact_text(text):
            failures.append(text)
    assert not failures, failures[:10]
    # The comparison has substance: the reference masks a good part of the corpus. (It misses the
    # rest - a dot between groups, a letter before the digits - which the next test holds to account.)
    compared = len([1 for _, text in CARDS if not text.startswith(NAMED_NEGATIVE_ID)])
    assert compared - unmasked_by_reference > 2000


def test_every_card_is_masked_unless_it_is_a_named_negative_id():
    # Beyond the original: whatever it missed, a card is masked anyway, however it is written.
    left = [
        text
        for shown, text in CARDS
        if shown in redact_text(text) and not text.startswith(NAMED_NEGATIVE_ID)
    ]
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


def test_a_negative_id_named_as_one_stays_readable_and_other_numbers_too():
    assert redact_text("chat_id=-2637222897571620") == "chat_id=-2637222897571620"
    assert redact_text("owner: -1001234567890") == "owner: -1001234567890"
    assert redact_text("code=-8204 h_msg_cd: -2000 port 8081 pid 12345") == (
        "code=-8204 h_msg_cd: -2000 port 8081 pid 12345"
    )
    # Not a card, not an id: twelve digits.
    assert redact_text("n=123456789012") == "n=123456789012"
