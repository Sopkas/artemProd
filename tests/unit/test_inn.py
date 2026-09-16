import pytest

from claims_assistant.domain.inn import InnProblem, InvalidInn, validate_legal_inn

# Synthetic values built for the checksum, not identifiers of real organizations.
VALID = "1234567894"
ZERO = "0000000000"


@pytest.mark.parametrize("raw", [VALID, ZERO, f" {VALID}\n"])
def test_valid_legal_inn_is_returned_normalized(raw):
    assert validate_legal_inn(raw) == raw.strip()


def test_zero_inn_passes_checksum_for_demo_fixtures():
    # PR #1 demo scenarios use this value; the validator must not reject it separately.
    assert validate_legal_inn(ZERO) == ZERO


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("", InnProblem.EMPTY),
        ("   ", InnProblem.EMPTY),
        ("12345six94", InnProblem.NOT_DIGITS),
        ("1234-567894", InnProblem.NOT_DIGITS),
        ("123 456 7894", InnProblem.NOT_DIGITS),
        ("123456789", InnProblem.WRONG_LENGTH),
        ("12345678941", InnProblem.WRONG_LENGTH),
        ("1234567895", InnProblem.CHECKSUM),
        ("1234567890", InnProblem.CHECKSUM),
    ],
)
def test_invalid_inn_reports_the_problem(raw, problem):
    with pytest.raises(InvalidInn) as info:
        validate_legal_inn(raw)
    assert info.value.problem is problem


def test_twelve_digit_inn_is_unsupported_not_invalid():
    with pytest.raises(InvalidInn) as info:
        validate_legal_inn("123456789012")
    assert info.value.problem is InnProblem.UNSUPPORTED_TYPE


def test_unicode_digits_are_not_accepted():
    with pytest.raises(InvalidInn) as info:
        validate_legal_inn("１２３４５６７８９４")
    assert info.value.problem is InnProblem.NOT_DIGITS


def test_invalid_inn_is_a_value_error_with_readable_message():
    with pytest.raises(ValueError) as info:
        validate_legal_inn("abc")
    assert str(info.value)
    assert "abc" not in str(info.value)
