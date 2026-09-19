"""S4-05: the optional «Платежи» sheet (docs/data-contracts.md, section 2).

One row per incoming payment. Rows are validated and kept only for counterparties of the
package; the covered period and the "last payment" indicator are derived later (S4-06)
from the period the user confirms at upload. Pure domain code, like the «Контрагенты» parser.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from claims_assistant.domain.counterparties import ImportLimits
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.sheet_rules import (
    Cell,
    CellError,
    cell_at,
    column_letter,
    data_rows,
    issue,
    map_header,
    parse_amount,
    parse_date,
    parse_inn,
    parse_record_id,
)

SHEET_NAME = "Платежи"
COLUMN_TITLES = ("ИНН", "ID платежа", "Дата платежа", "Сумма платежа")
_FIELDS = {
    "ИНН": "inn",
    "ID платежа": "payment_id",
    "Дата платежа": "paid_on",
    "Сумма платежа": "amount",
}
_REQUIRED = frozenset(_FIELDS.values())


@dataclass(frozen=True, slots=True)
class PaymentRow:
    inn: str
    payment_id: str
    paid_on: date
    amount: Decimal

    def __post_init__(self) -> None:
        if not self.payment_id:
            raise ValueError("Payment ID is required")
        if not self.amount.is_finite() or self.amount <= 0:
            raise ValueError("A payment amount is a finite positive Decimal")


@dataclass(frozen=True, slots=True)
class PaymentsImport:
    rows: tuple[PaymentRow, ...] = ()
    issues: tuple[ImportIssue, ...] = ()


def _required(value: object, code: str, reason: str) -> object:
    if value is None:
        raise CellError(code, reason)
    return value


def parse_payments(
    header: tuple[Cell, ...],
    rows: Iterable[tuple[int, tuple[Cell, ...]]],
    *,
    known_inns: Collection[str] | None,
    analysis_date: date | None,
    limits: ImportLimits = ImportLimits(),
) -> PaymentsImport:
    """Validate the sheet: payments of package counterparties up to the analysis date.

    ``known_inns`` are the INNs of the «Контрагенты» file; a payment of any other INN never
    adds a counterparty and is reported. ``None`` skips that check (no package context).
    """
    issues: list[ImportIssue] = []
    mapping = map_header(header, _FIELDS, _REQUIRED, SHEET_NAME, issues)
    if mapping is None:
        return PaymentsImport(issues=tuple(issues))

    def column(field: str) -> str:
        return column_letter(mapping[field] + 1)

    rules = {
        "inn": parse_inn,
        "payment_id": lambda c: parse_record_id(c, prefix="payment_id", label="ID платежа"),
        "paid_on": lambda c: _required(
            parse_date(c), "payment_date_missing", "Дата платежа обязательна."
        ),
        "amount": lambda c: _required(
            parse_amount(c, prefix="payment_amount", label="Сумма платежа", positive=True),
            "payment_amount_missing",
            "Сумма платежа обязательна.",
        ),
    }
    accepted: dict[tuple[str, str], tuple[int, PaymentRow]] = {}
    for row_number, cells in data_rows(rows, limits.max_rows, SHEET_NAME, issues):
        values: dict[str, object] = {}
        row_issues = []
        for field, rule in rules.items():
            try:
                values[field] = rule(cell_at(cells, mapping, field))
            except CellError as error:
                row_issues.append(
                    issue(
                        SHEET_NAME,
                        error.code,
                        IssueSeverity.ERROR,
                        error.reason,
                        row_number,
                        column(field),
                    )
                )
        if row_issues:
            issues.extend(row_issues)
            continue
        payment = PaymentRow(**values)
        if known_inns is not None and payment.inn not in known_inns:
            issues.append(
                issue(
                    SHEET_NAME,
                    "inn_not_in_package",
                    IssueSeverity.ERROR,
                    "ИНН нет в файле «Контрагенты»; платёж не добавляет контрагента.",
                    row_number,
                    column("inn"),
                )
            )
            continue
        if analysis_date is not None and payment.paid_on > analysis_date:
            issues.append(
                issue(
                    SHEET_NAME,
                    "payment_after_analysis_date",
                    IssueSeverity.WARNING,
                    "Платёж позже даты анализа и исключён.",
                    row_number,
                    column("paid_on"),
                )
            )
            continue
        key = (payment.inn, payment.payment_id)
        existing = accepted.get(key)
        if existing is None:
            accepted[key] = (row_number, payment)
        elif existing[1] == payment:
            issues.append(
                issue(
                    SHEET_NAME,
                    "duplicate_payment",
                    IssueSeverity.WARNING,
                    f"Строка повторяет платёж из строки {existing[0]} и пропущена.",
                    row_number,
                )
            )
        else:
            issues.append(
                issue(
                    SHEET_NAME,
                    "payment_id_conflict",
                    IssueSeverity.ERROR,
                    f"ID платежа уже встречался в строке {existing[0]} с другими данными.",
                    row_number,
                    column("payment_id"),
                )
            )
    return PaymentsImport(
        rows=tuple(payment for _, payment in accepted.values()), issues=tuple(issues)
    )
