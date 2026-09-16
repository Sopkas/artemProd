"""Shared INN validator for manual input and Excel import (S1-01)."""

from enum import StrEnum

# Weights for the control digit of a 10-digit legal-entity INN.
_LEGAL_WEIGHTS = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_LEGAL_LENGTH = 10
_INDIVIDUAL_LENGTH = 12


class InnProblem(StrEnum):
    EMPTY = "empty"
    NOT_DIGITS = "not_digits"
    WRONG_LENGTH = "wrong_length"
    UNSUPPORTED_TYPE = "unsupported_type"
    CHECKSUM = "checksum"


_MESSAGES = {
    InnProblem.EMPTY: "ИНН не указан.",
    InnProblem.NOT_DIGITS: "ИНН должен состоять только из цифр.",
    InnProblem.WRONG_LENGTH: "ИНН юридического лица состоит из 10 цифр.",
    InnProblem.UNSUPPORTED_TYPE: "ИНН из 12 цифр принадлежит ИП или физическому лицу; "
    "такой тип контрагента пока не поддерживается.",
    InnProblem.CHECKSUM: "Контрольная цифра ИНН не совпадает; проверьте значение.",
}


class InvalidInn(ValueError):
    """Message never includes the raw input: it may be pasted from private data."""

    def __init__(self, problem: InnProblem) -> None:
        super().__init__(_MESSAGES[problem])
        self.problem = problem


def validate_legal_inn(raw: str) -> str:
    """Return the normalized 10-digit INN or raise InvalidInn with the reason."""
    value = raw.strip()
    if not value:
        raise InvalidInn(InnProblem.EMPTY)
    if not value.isascii() or not value.isdigit():
        raise InvalidInn(InnProblem.NOT_DIGITS)
    if len(value) == _INDIVIDUAL_LENGTH:
        raise InvalidInn(InnProblem.UNSUPPORTED_TYPE)
    if len(value) != _LEGAL_LENGTH:
        raise InvalidInn(InnProblem.WRONG_LENGTH)
    digits = [int(char) for char in value]
    control = sum(w * d for w, d in zip(_LEGAL_WEIGHTS, digits, strict=False)) % 11 % 10
    if control != digits[-1]:
        raise InvalidInn(InnProblem.CHECKSUM)
    return value
