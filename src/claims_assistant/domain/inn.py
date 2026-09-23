"""Shared INN validator for manual input and Excel import (S1-01, S7-02).

Both kinds a claims specialist meets are accepted: a company's ten digits and an
entrepreneur's twelve. The customer's portfolio holds both (23.09.2026), and refusing
the twelve-digit ones simply hid part of it from the check.

The checks are the official ones: one control digit for a company, two for an
entrepreneur. An error never repeats the input — it may be pasted from private data.
"""

from enum import StrEnum

# Weights of the control digits: one for a company, two for an entrepreneur.
_LEGAL_WEIGHTS = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_ELEVENTH_WEIGHTS = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_TWELFTH_WEIGHTS = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_LEGAL_LENGTH = 10
_INDIVIDUAL_LENGTH = 12


class InnKind(StrEnum):
    LEGAL = "legal"  # организация: 10 цифр
    ENTREPRENEUR = "entrepreneur"  # ИП или физическое лицо: 12 цифр


def inn_kind(value: str) -> InnKind:
    """Which kind a validated INN is; the length is what tells them apart."""
    return InnKind.LEGAL if len(value) == _LEGAL_LENGTH else InnKind.ENTREPRENEUR


class InnProblem(StrEnum):
    EMPTY = "empty"
    NOT_DIGITS = "not_digits"
    WRONG_LENGTH = "wrong_length"
    UNSUPPORTED_TYPE = "unsupported_type"
    CHECKSUM = "checksum"


_MESSAGES = {
    InnProblem.EMPTY: "ИНН не указан.",
    InnProblem.NOT_DIGITS: "ИНН должен состоять только из цифр.",
    InnProblem.WRONG_LENGTH: "ИНН состоит из 10 цифр у организации или из 12 у ИП.",
    InnProblem.UNSUPPORTED_TYPE: "Этот тип контрагента не поддерживается.",
    InnProblem.CHECKSUM: "Контрольная цифра ИНН не совпадает; проверьте значение.",
}


class InvalidInn(ValueError):
    """Message never includes the raw input: it may be pasted from private data."""

    def __init__(self, problem: InnProblem) -> None:
        super().__init__(_MESSAGES[problem])
        self.problem = problem


def _control(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(w * d for w, d in zip(weights, digits, strict=False)) % 11 % 10


def validate_inn(raw: str) -> str:
    """Return the normalized INN of a company or an entrepreneur, or raise InvalidInn."""
    value = raw.strip()
    if not value:
        raise InvalidInn(InnProblem.EMPTY)
    if not value.isascii() or not value.isdigit():
        raise InvalidInn(InnProblem.NOT_DIGITS)
    if len(value) not in (_LEGAL_LENGTH, _INDIVIDUAL_LENGTH):
        raise InvalidInn(InnProblem.WRONG_LENGTH)
    digits = [int(char) for char in value]
    if len(value) == _LEGAL_LENGTH:
        if _control(digits, _LEGAL_WEIGHTS) != digits[-1]:
            raise InvalidInn(InnProblem.CHECKSUM)
        return value
    # An entrepreneur's INN carries two control digits; both have to hold.
    if _control(digits, _ELEVENTH_WEIGHTS) != digits[10]:
        raise InvalidInn(InnProblem.CHECKSUM)
    if _control(digits, _TWELFTH_WEIGHTS) != digits[11]:
        raise InvalidInn(InnProblem.CHECKSUM)
    return value


def validate_legal_inn(raw: str) -> str:
    """Only a company's INN; an entrepreneur's is refused with a clear reason.

    Nothing in the product calls this today — the import, the card and the stored payloads
    all take both kinds through ``validate_inn``. It stays for the sections that exist for
    companies only (the company card and the accounting statements, S7-02): when their
    method is wired, that is where a legal entity has to be required rather than assumed.
    """
    # The kind is judged before the checksum: «this is an entrepreneur» is the useful
    # answer here, not «the control digit does not match».
    if raw.strip().isdigit() and len(raw.strip()) == _INDIVIDUAL_LENGTH:
        raise InvalidInn(InnProblem.UNSUPPORTED_TYPE)
    return validate_inn(raw)
