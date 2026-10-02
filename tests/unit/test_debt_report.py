"""S7-01: the customer's overdue-payments report, read by its indents.

The shape is the customer's (23.09.2026): three levels in one column, the values spread
over other columns, «Итого» and «Отбор» rows in between. Values here are synthetic.
"""

from datetime import date
from decimal import Decimal

import pytest

from claims_assistant.domain.debt_report import (
    CounterpartyDebt,
    map_columns,
    read_debt_report,
)

INN = "1234567894"
# The columns as the print lays them out: the value columns sit far to the right.
COLS = {"name": 0, "manager": 7, "due": 27, "subject": 28, "amount": 47, "overdue": 55, "days": 66}
WIDTH = 70


def row(indent, name, **values):
    cells = [None] * WIDTH
    cells[COLS["name"]] = name
    for field, value in values.items():
        cells[COLS[field]] = value
    return indent, tuple(cells)


def legend():
    return [
        row(0, "Отчет по просроченным лизинговым платежам на дату"),
        row(
            0,
            "Точка продаж",
            due="Срок действия до",
            subject="Предмет лизинга",
            amount="Сумма",
            overdue="Сумма просроченной задолженности с вычетом н/р дней",
            days="Дней",
        ),
        row(0, "Контрагент", manager="Текущий менеджер контрагента"),
        row(0, "Договор лизинга", manager="Состояние изъятия"),
    ]


def report(step=2):
    """One sales point, two counterparties, contracts with their own overdue and days."""
    return legend() + [
        row(0, "Владивосток", amount=1000, overdue=1000, days=307),
        row(step, "РОМАШКА ООО", manager="Иванова И.И."),
        row(
            step * 2,
            "Дог. фин. лизинга № 1",
            amount=600,
            overdue=600,
            days=307,
            due=date(2026, 9, 20),
            subject="Тягач",
        ),
        row(step * 2, "Дог. фин. лизинга № 2", amount=400, overdue=400, days=30),
        row(step, "Данилов Максим Владимирович, ИП"),
        row(step * 2, "Дог. фин. лизинга № 3", amount=250, overdue=250, days=95),
        row(0, "Итого", amount=1250),
    ]


def test_the_levels_are_read_from_the_indent_not_from_the_name():
    result = read_debt_report(report())
    assert [c.name for c in result.counterparties] == [
        "РОМАШКА ООО",
        "Данилов Максим Владимирович, ИП",  # a person is a counterparty, not a sales point
    ]
    assert all(c.group == "Владивосток" for c in result.counterparties)
    assert result.contracts == 3 and result.issues == ()


@pytest.mark.parametrize("step", [2, 3])
def test_the_indent_step_differs_between_exports_and_both_are_read(step):
    """2/4 in the customer's own print, 3/6 in the one their tooling produced."""
    result = read_debt_report(report(step))
    assert len(result.counterparties) == 2 and result.contracts == 3


def test_every_contract_keeps_its_own_overdue_and_days():
    first = read_debt_report(report()).counterparties[0]
    assert [(c.overdue, c.days) for c in first.contracts] == [
        (Decimal("600"), 307),
        (Decimal("400"), 30),
    ]
    assert first.contracts[0].due_until == date(2026, 9, 20)
    assert first.contracts[0].subject == "Тягач"


def test_a_contract_with_no_overdue_keeps_its_zero():
    """Found by A on #53: Decimal(0) is falsy, so a paid-up contract took the whole
    «Сумма» instead of its zero and inflated the counterparty's debt."""
    rows = legend() + [
        row(0, "Владивосток"),
        row(2, "РОМАШКА ООО"),
        row(4, "Дог. фин. лизинга № 1", amount=1000000, overdue=0, days=0),
        row(4, "Дог. фин. лизинга № 2", amount=250000, overdue=250000, days=95),
    ]
    result = read_debt_report(rows)
    first = result.counterparties[0]
    assert [c.overdue for c in first.contracts] == [Decimal("0"), Decimal("250000")]
    assert first.total_overdue == Decimal("250000")  # not 1 250 000
    assert first.worst_days == 95


def test_the_sum_column_is_used_when_the_report_has_only_that_one():
    header = [None] * WIDTH
    header[COLS["amount"]] = "Сумма"
    header[COLS["days"]] = "Дней"
    rows = [(0, tuple(header)), row(0, "Владивосток"), row(2, "РОМАШКА ООО")]
    rows.append(row(4, "Дог. фин. лизинга № 1", amount=700, days=40))
    contract = read_debt_report(rows).counterparties[0].contracts[0]
    assert contract.overdue == Decimal("700") and contract.days == 40


def test_the_counterparty_carries_the_sum_and_the_worst_of_its_contracts():
    first = read_debt_report(report()).counterparties[0]
    assert first.total_overdue == Decimal("1000")
    assert first.worst_days == 307  # the report's own subtotal shows the same
    empty = CounterpartyDebt(name="Без договоров")
    assert empty.total_overdue is None and empty.worst_days is None


def test_totals_and_filter_rows_are_not_data():
    rows = report()
    rows.insert(-1, row(0, 'Отбор: Дней просрочки по договору Больше или равно "90"'))
    result = read_debt_report(rows)
    assert [c.name for c in result.counterparties] == [
        "РОМАШКА ООО",
        "Данилов Максим Владимирович, ИП",
    ]
    assert all("Итого" not in c.name for c in result.counterparties)


# The filtered table of the customer's print: the same legend, other column positions.
SHIFTED = {
    "name": 0,
    "manager": 2,
    "due": 15,
    "subject": 19,
    "amount": 31,
    "overdue": 34,
    "days": 40,
}


def shifted(indent, name, **values):
    cells = [None] * WIDTH
    cells[SHIFTED["name"]] = name
    for field, value in values.items():
        cells[SHIFTED[field]] = value
    return indent, tuple(cells)


def filtered_table():
    return [
        shifted(0, 'Отбор: Дней просрочки по договору Больше или равно "90"'),
        shifted(
            0,
            "Точка продаж",
            due="Срок действия до",
            subject="Предмет лизинга",
            amount="Сумма",
            overdue="Сумма просроченной задолженности с вычетом н/р дней",
            days="Дней",
        ),
        shifted(0, "Контрагент"),
        shifted(0, "Договор лизинга"),
        shifted(0, "Владивосток", amount=900, overdue=900, days=120),
        shifted(2, "Данилов Максим Владимирович, ИП"),
        shifted(4, "Дог. фин. лизинга № 3", amount=250, overdue=250, days=95),
        shifted(2, "МАГИСТРАЛЬ ООО"),  # only in the filtered table, as in the real file
        shifted(4, "Дог. фин. лизинга № 9", amount=650, overdue=650, days=120),
        shifted(0, "Итого", amount=900),
    ]


def test_every_table_of_the_print_is_read_by_its_own_legend():
    """30.09, the customer's real report: its «Отбор» tables put «Дней» in other columns.
    Read by the first legend, their contracts came without sums; four companies are only
    there."""
    result = read_debt_report(report() + filtered_table())
    names = [c.name for c in result.counterparties]
    assert names == [
        "РОМАШКА ООО",
        "Данилов Максим Владимирович, ИП",
        "Данилов Максим Владимирович, ИП",
        "МАГИСТРАЛЬ ООО",
    ]
    only_there = result.counterparties[-1]
    assert [(k.name, k.overdue, k.days) for k in only_there.contracts] == [
        ("Дог. фин. лизинга № 9", Decimal("650"), 120)
    ]
    reprint = result.counterparties[2].contracts[0]
    assert (reprint.overdue, reprint.days) == (Decimal("250"), 95)
    assert result.issues == ()


def test_days_that_are_not_days_are_left_unknown():
    """The fictional file the customer sent first has amounts in the «Дней» column."""
    rows = legend() + [
        row(0, "Владивосток"),
        row(2, "РОМАШКА ООО"),
        row(4, "Дог. фин. лизинга № 1", amount=600, overdue=600, days=965237.83),
    ]
    contract = read_debt_report(rows).counterparties[0].contracts[0]
    assert contract.days is None and contract.overdue == Decimal("600")


def test_the_inn_column_is_used_when_the_report_has_one():
    cols = dict(COLS, inn=2)

    def with_inn(indent, name, inn=None):
        cells = [None] * WIDTH
        cells[cols["name"]] = name
        if inn is not None:
            cells[cols["inn"]] = inn
        return indent, tuple(cells)

    rows = legend()
    header = [None] * WIDTH
    header[cols["inn"]] = "ИНН"
    header[cols["days"]] = "Дней"
    rows.append((0, tuple(header)))
    rows += [
        with_inn(0, "Владивосток"),
        with_inn(2, "РОМАШКА ООО", inn=INN),
        row(4, "Дог. фин. лизинга № 1", amount=600, overdue=600, days=307),
    ]
    result = read_debt_report(rows)
    assert result.counterparties[0].inn == INN


def test_a_file_that_is_not_this_report_is_refused():
    result = read_debt_report([(0, ("Просто текст", None)), (0, ("Ещё строка", None))])
    assert result.counterparties == ()
    assert [i.code for i in result.issues] == ["report_not_recognised"]


def test_the_column_map_is_taken_from_the_legend():
    columns = map_columns(legend())
    assert columns["days"] == COLS["days"] and columns["overdue"] == COLS["overdue"]
    assert "inn" not in columns  # this variant of the report has no INN column
