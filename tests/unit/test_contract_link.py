"""S7-01: whose contracts are these — the overdue report is tied to a check by name.

The report carries no INN, so this is the one place where a company is recognised by its
name. The tests below are about the two ways that can go wrong: a name that matches
nobody, and a name that matches two companies at once.
"""

from datetime import date
from decimal import Decimal

import pytest

from claims_assistant.application.contract_link import (
    link_contracts,
    normalize_name,
    worst_days,
)
from claims_assistant.domain.debt_report import ContractDebt, CounterpartyDebt

INN_A, INN_B = "7707083893", "7710140679"


def contract(name="Дог-1", days=30, overdue="1000.00"):
    return ContractDebt(name=name, overdue=Decimal(overdue), days=days)


def counterparty(name, *contracts):
    return CounterpartyDebt(name=name, contracts=contracts or (contract(),))


@pytest.mark.parametrize(
    "left, right",
    [
        ("ООО «Ромашка»", "Ромашка, ООО"),
        ('ОАО "Ромашка"', "ромашка оао"),
        ("Данилов Максим Владимирович, ИП", "ИП Данилов Максим Владимирович"),
        ("Ромашка   Плюс", "Ромашка Плюс"),
    ],
)
def test_the_same_company_printed_two_ways_is_one_name(left, right):
    assert normalize_name(left) == normalize_name(right)


def test_different_companies_stay_different():
    assert normalize_name("ООО «Ромашка»") != normalize_name("ООО «Ромашка-Плюс»")


def test_contracts_go_to_the_company_the_name_points_at():
    linked = link_contracts(
        (counterparty("ООО «Ромашка»", contract("Дог-1", 307)), counterparty("Василёк, АО")),
        {INN_A: "Ромашка, ООО", INN_B: 'АО "Василёк"'},
    )
    assert [c.name for c in linked.by_inn[INN_A]] == ["Дог-1"]
    assert linked.by_inn[INN_B][0].days == 30
    assert linked.unknown == () and linked.ambiguous == () and linked.unnamed == ()


def test_a_name_no_company_of_the_check_has_is_reported_not_dropped():
    linked = link_contracts((counterparty("ООО «Чужая»"),), {INN_A: "Ромашка"})
    assert linked.by_inn == {} and linked.unknown == ("ООО «Чужая»",)


def test_two_companies_with_one_name_get_nothing():
    """A guess here would put someone else's debt into a claim letter."""
    linked = link_contracts(
        (counterparty("ООО «Ромашка»"),), {INN_A: "Ромашка", INN_B: "«Ромашка», ООО"}
    )
    assert linked.by_inn == {} and linked.ambiguous == ("ООО «Ромашка»",)


def test_a_company_the_source_never_named_cannot_take_part():
    linked = link_contracts((counterparty("ООО «Ромашка»"),), {INN_A: None, INN_B: "Ромашка"})
    assert linked.by_inn == {INN_B: (contract(),)} and linked.unnamed == (INN_A,)


def test_contracts_of_one_company_printed_twice_are_kept_together():
    linked = link_contracts(
        (
            counterparty("Ромашка", contract("Дог-1")),
            counterparty("ООО Ромашка", contract("Дог-2")),
        ),
        {INN_A: "Ромашка"},
    )
    assert [c.name for c in linked.by_inn[INN_A]] == ["Дог-1", "Дог-2"]


def test_worst_days_ignores_contracts_that_do_not_say():
    contracts = (contract(days=30), contract(days=None), contract(days=307))
    assert worst_days(contracts) == 307
    assert worst_days((ContractDebt(name="Дог", due_until=date(2026, 1, 1)),)) is None
    assert worst_days(()) is None
