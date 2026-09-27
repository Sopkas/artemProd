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
    name_without_form,
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
        ("ООО «Ромашка»", "ООО Ромашка"),
        ('ОАО "Ромашка"', "оао ромашка"),
        ("Ромашка   Плюс, ООО", "ООО Ромашка Плюс"),
    ],
)
def test_the_same_company_printed_two_ways_is_one_name(left, right):
    """Case, quotes and spaces differ between two prints; the form does not."""
    assert normalize_name(left) == normalize_name(right)


def test_the_legal_form_is_part_of_the_name(left="ООО Ромашка", right="АО Ромашка"):
    """Two different legal entities, and in a leasing portfolio such a pair is normal."""
    assert normalize_name(left) != normalize_name(right)
    assert name_without_form(left) == name_without_form(right)  # only the fallback key


@pytest.mark.parametrize(
    "printed, expected",
    [
        ("ООО «Ромашка»", "ромашка"),
        ("Ромашка, ООО", "ромашка"),
        ("ИП Данилов Максим Владимирович", "данилов максим владимирович"),
        ("Данилов Максим Владимирович, ИП", "данилов максим владимирович"),
        ("ИП-Сервис", "ип-сервис"),  # the form is inside the word, not the form
        ("ООО", "ооо"),  # nothing would be left, so nothing is dropped
    ],
)
def test_the_fallback_key_drops_the_form_only_where_it_is_written(printed, expected):
    assert name_without_form(printed) == expected


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
        (counterparty("ООО «Ромашка»"),), {INN_A: "ООО Ромашка", INN_B: "«Ромашка», ООО"}
    )
    assert linked.by_inn == {} and linked.ambiguous == ("ООО «Ромашка»",)


def test_a_company_the_source_never_named_cannot_take_part():
    linked = link_contracts((counterparty("ООО «Ромашка»"),), {INN_A: None, INN_B: "Ромашка"})
    assert linked.by_inn == {INN_B: (contract(),)} and linked.unnamed == (INN_A,)


def test_a_match_without_the_form_is_reported_for_a_human_to_glance_at():
    """One print carries the form, the other does not — likely the same client, not surely."""
    linked = link_contracts((counterparty("Ромашка"),), {INN_A: "ООО «Ромашка»"})
    assert [c.name for c in linked.by_inn[INN_A]] == ["Дог-1"]
    assert linked.without_form == ("Ромашка",) and linked.unknown == ()


def test_the_form_decides_between_two_namesakes():
    linked = link_contracts(
        (counterparty("ООО «Ромашка»", contract("Дог-1")),),
        {INN_A: "ООО Ромашка", INN_B: "АО Ромашка"},
    )
    assert linked.by_inn == {INN_A: (contract("Дог-1"),)}  # the exact key wins outright
    assert linked.ambiguous == () and linked.without_form == ()


def test_a_form_less_name_between_two_namesakes_is_attributed_to_neither():
    linked = link_contracts((counterparty("Ромашка"),), {INN_A: "ООО Ромашка", INN_B: "АО Ромашка"})
    assert linked.by_inn == {} and linked.ambiguous == ("Ромашка",)


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


def test_without_a_debt_report_nobody_is_reported_unnamed():
    """Found on the live source (27.09.2026): an INN Checko does not know gets no name, and
    «Качество данных» warned that the overdue report's contracts were not tied to it —
    in a check that had no overdue report at all."""
    linked = link_contracts((), {INN_A: None, INN_B: "Ромашка"})
    assert linked.unnamed == ()
