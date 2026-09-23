import pytest

from claims_assistant.domain.inn import (
    InnKind,
    InnProblem,
    InvalidInn,
    inn_kind,
    validate_inn,
    validate_legal_inn,
)

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


ENTREPRENEUR = "500100732259"  # synthetic: built to satisfy both control digits


def test_twelve_digit_inn_is_refused_where_only_a_company_fits():
    with pytest.raises(InvalidInn) as info:
        validate_legal_inn(ENTREPRENEUR)
    assert info.value.problem is InnProblem.UNSUPPORTED_TYPE


@pytest.mark.parametrize(
    "raw,kind",
    [
        (VALID, InnKind.LEGAL),
        (ENTREPRENEUR, InnKind.ENTREPRENEUR),
        (f" {ENTREPRENEUR} ", InnKind.ENTREPRENEUR),
    ],
)
def test_both_kinds_are_accepted_where_the_portfolio_has_both(raw, kind):
    """S7-02: the customer works with companies and entrepreneurs alike."""
    value = validate_inn(raw)
    assert value == raw.strip() and inn_kind(value) is kind


@pytest.mark.parametrize(
    "raw,problem",
    [
        ("500100732250", InnProblem.CHECKSUM),  # the twelfth digit is wrong
        ("500100732279", InnProblem.CHECKSUM),  # the eleventh digit is wrong
        ("12345678901", InnProblem.WRONG_LENGTH),
        ("", InnProblem.EMPTY),
    ],
)
def test_an_entrepreneur_inn_is_checked_by_both_control_digits(raw, problem):
    with pytest.raises(InvalidInn) as info:
        validate_inn(raw)
    assert info.value.problem is problem


def test_unicode_digits_are_not_accepted():
    with pytest.raises(InvalidInn) as info:
        validate_legal_inn("１２３４５６７８９４")
    assert info.value.problem is InnProblem.NOT_DIGITS


def test_invalid_inn_is_a_value_error_with_readable_message():
    with pytest.raises(ValueError) as info:
        validate_legal_inn("abc")
    assert str(info.value)
    assert "abc" not in str(info.value)
