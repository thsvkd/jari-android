"""Korail login identifiers keep phone and membership formats distinct."""

import pytest

from korail_bot.utils.validators import InputValidator


@pytest.mark.parametrize(
    ("typed", "normalized"),
    [
        ("12345678", "12345678"),
        ("2456789012", "2456789012"),
        ("01012345678", "010-1234-5678"),
        ("011 123 4567", "011-123-4567"),
    ],
)
def test_normalize_korail_login_supports_membership_and_phone(typed, normalized):
    assert InputValidator.validate_korail_login(typed) is None
    assert InputValidator.normalize_korail_login(typed) == normalized


@pytest.mark.parametrize("typed", ["", "1234567", "123456789", "member-123", "010-12-34"])
def test_invalid_korail_login_explains_both_supported_formats(typed):
    error = InputValidator.validate_korail_login(typed)
    assert error is not None
    assert "휴대전화 번호" in error
    assert "회원번호" in error
