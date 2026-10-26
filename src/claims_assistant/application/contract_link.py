"""S7-01: whose contracts are these — linking the overdue report to the check's rows.

The customer's «Отчет по просроченным лизинговым платежам» usually names no INN
(23.09.2026: the column «appears in another variant»). An entry that does print one is
tied by it and by nothing else: the INN is the company, and a namesake with another INN
is another company — whether or not it is in the check. Every entry without an INN is tied
by **name**, against the names the external source gave us (or, when it gave none, as for
an ИП, the name in the customer's own «Контрагенты»). Both files come out of the same 1C,
so the names match far more often than they would between unrelated systems — but «far
more often» is not «always», and everything that did not match is said out loud rather
than dropped.

Two keys, tried in that order (review B on #58):

1. **with the legal form** — «ООО Ромашка» and «АО Ромашка» are two different legal
   entities, and in a leasing portfolio such a pair is not exotic. The form is printed
   from the same field in both exports, so it is a difference we can trust;
2. **without it**, and only when exactly one company is left — for the case where one
   print carries the form and the other does not. Two *different* forms stay two
   companies, except the 2014 renames (ЗАО → АО/НАО, ОАО → ПАО/АО): the source has the
   new name, an old 1C card may keep the old one. Such a match is reported, so a person
   can glance at it.

Either key ignores what differs between two prints of one name: case, quotes, punctuation
and repeated spaces. Neither ignores the words themselves: «Ромашка» and «Ромашка-Плюс»
stay two companies, and a hyphen is kept inside a word so «ИП-Сервис» is not read as the
form «ИП» plus «Сервис».

When two companies of one check share a key, neither gets the contracts: a guess here
would put someone else's debt into a claim letter.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from claims_assistant.domain.debt_report import SHEET_NAME as DEBT_REPORT_SHEET
from claims_assistant.domain.debt_report import ContractDebt, CounterpartyDebt
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.sheet_rules import issue

_LEGAL_FORMS = frozenset(
    {
        "ооо",
        "оао",
        "зао",
        "пао",
        "ао",
        "ип",
        "нао",
        "ано",
        "спк",
        "кфх",
        "гуп",
        "муп",
        "фгуп",
        "тсж",
        "нко",
    }
)
# The hyphen is not here on purpose: it belongs to the word («ИП-Сервис», «Ромашка-Плюс»).
# Typographic quotes too: a name typed by hand gets “ ” or „ “ from autocorrect.
_PUNCTUATION = re.compile(r"[«»\"'`“”„‟‘’‚‹›.,;:()\[\]/\\]+")
_SPACES = re.compile(r"\s+")


def _split(name: str) -> tuple[str, frozenset[str]]:
    """The name as (what it is called, which legal forms were printed with it).

    The form is taken only from the edges, where it is written — «ООО Ромашка» and
    «Ромашка, ООО» are one company, and the side it sits on is a печатная привычка. A
    form-looking word inside the name belongs to it: «ИП-Сервис» is not «Сервис».
    """
    text = _PUNCTUATION.sub(" ", name.casefold())
    words = [word for word in _SPACES.split(text) if word]
    forms: set[str] = set()
    while len(words) > 1 and words[0] in _LEGAL_FORMS:
        forms.add(words[0])
        words = words[1:]
    while len(words) > 1 and words[-1] in _LEGAL_FORMS:
        forms.add(words[-1])
        words = words[:-1]
    return " ".join(words), frozenset(forms)


# One company under its old and new form (the 2014 reform of joint-stock companies).
_RENAMED = frozenset(
    frozenset(pair) for pair in (("зао", "ао"), ("зао", "нао"), ("оао", "ао"), ("оао", "пао"))
)


def _forms_agree(left: frozenset[str], right: frozenset[str]) -> bool:
    """Whether two prints of one name may be one company: one of them has no form, or
    the forms are a rename. «АО Ромашка» and «ООО Ромашка» are two legal entities."""
    return not left or not right or (len(left) == len(right) == 1 and left | right in _RENAMED)


def same_company(left: str | None, right: str | None) -> bool:
    """Two entries of one name are one company unless both print an INN and they differ."""
    return not left or not right or left == right


def normalize_name(name: str) -> str:
    """The strict key: the name and the legal form printed with it, in a fixed order."""
    core, forms = _split(name)
    return core if not forms else f"{core} [{'|'.join(sorted(forms))}]"


def name_without_form(name: str) -> str:
    """The fallback key: the same name with the legal form dropped."""
    return _split(name)[0]


@dataclass(frozen=True, slots=True)
class LinkedContracts:
    """Contracts by INN, plus what could not be tied to anybody — and why."""

    by_inn: dict[str, tuple[ContractDebt, ...]] = field(default_factory=dict)
    unknown: tuple[str, ...] = ()  # names of the report that no company of the check has
    ambiguous: tuple[str, ...] = ()  # names that two companies of the check share
    unnamed: tuple[str, ...] = ()  # companies we have no name for (the source was silent)
    # Matched only after dropping the legal form: worth a human glance (review B on #58).
    without_form: tuple[str, ...] = ()
    # Matched to the name of the customer's own «Контрагенты» row, because the source gave
    # none (an ИП, S7-02): also worth a glance.
    by_own_name: tuple[str, ...] = ()

    @property
    def linked(self) -> int:
        return len(self.by_inn)


def _index(names: dict[str, str | None], key) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for inn, name in names.items():
        value = key(name or "")
        if value:
            index.setdefault(value, []).append(inn)
    return index


def link_contracts(
    report: tuple[CounterpartyDebt, ...],
    names: dict[str, str | None],
    *,
    own_names: frozenset[str] = frozenset(),
) -> LinkedContracts:
    """Tie every counterparty of the overdue report to an INN of the check.

    An entry that prints an INN is tied by it: to that company if it is in the check, to
    nobody (``unknown``) if it is not — a namesake in the check is never a candidate.
    The rest are tied by name. ``names`` is the check's own INN → company name; ``own_names``
    are the INNs whose name is the customer's own row rather than the source's (S7-02: the
    source names no ИП), and a match to them is reported. A company with no name at all
    cannot take part by name — its INN is reported as ``unnamed``.
    """
    strict = _index(names, normalize_name)
    loose = _index(names, name_without_form)
    forms = {inn: _split(name or "")[1] for inn, name in names.items()}
    # A name the report itself prints with an INN: an entry of it without one could be
    # any of them (``merge_repeats`` has already joined it where only one INN was left).
    printed_with_inn = {normalize_name(c.name) for c in report if c.inn}

    by_inn: dict[str, list[ContractDebt]] = {}
    unknown: list[str] = []
    ambiguous: list[str] = []
    without_form: list[str] = []
    by_own_name: list[str] = []
    by_name = False
    for counterparty in report:
        if counterparty.inn:
            if counterparty.inn in names:
                by_inn.setdefault(counterparty.inn, []).extend(counterparty.contracts)
            else:
                unknown.append(counterparty.name)
            continue
        by_name = True
        key = normalize_name(counterparty.name)
        if key in printed_with_inn:
            ambiguous.append(counterparty.name)
            continue
        owners = strict.get(key, [])
        if len(owners) > 1:
            ambiguous.append(counterparty.name)
            continue
        if not owners:
            core, printed = _split(counterparty.name)
            owners = [inn for inn in loose.get(core, []) if _forms_agree(printed, forms[inn])]
            if len(owners) > 1:
                ambiguous.append(counterparty.name)
                continue
            if not owners:
                unknown.append(counterparty.name)
                continue
            without_form.append(counterparty.name)
        if owners[0] in own_names:
            by_own_name.append(counterparty.name)
        by_inn.setdefault(owners[0], []).extend(counterparty.contracts)
    # Only worth saying when a name had to be matched: without that, a company the source
    # does not know simply has no contracts, and a warning about them would be noise.
    unnamed = (
        [inn for inn, name in names.items() if not normalize_name(name or "") and inn not in by_inn]
        if by_name
        else []
    )
    return LinkedContracts(
        by_inn={inn: tuple(contracts) for inn, contracts in by_inn.items()},
        unknown=tuple(unknown),
        ambiguous=tuple(ambiguous),
        unnamed=tuple(sorted(set(unnamed))),
        without_form=tuple(without_form),
        by_own_name=tuple(by_own_name),
    )


def merge_repeats(
    report: Sequence[CounterpartyDebt],
) -> tuple[tuple[CounterpartyDebt, ...], tuple[ImportIssue, ...]]:
    """One report, one debtor: entries of one name in one file are one company.

    The customer's report lists a client under every sales point it deals with, and the
    print repeats its own table under filters («Отбор: …»). So the entries of one name are
    joined, in the order they come, and a contract printed again is kept once, with the
    values of its first print (a value the first print lacks is taken from the repeat).
    A reprint is the report's usual shape and is not reported; a reprint that disagrees
    on the sum or the days is. A name met under two sales points is said out loud: two
    companies can share a printed name. Where the report prints INNs, two of them under
    one name are two companies and stay apart; an entry without one joins its name only
    when the report gives that name a single INN (or none).
    Repeats across *different* reports are another matter, handled by the package review.
    """
    inns_of: dict[str, set[str]] = {}
    for counterparty in report:
        if counterparty.inn:
            inns_of.setdefault(normalize_name(counterparty.name), set()).add(counterparty.inn)
    order: list[int] = []
    joined: dict[int, CounterpartyDebt] = {}
    slots: dict[str, list[int]] = {}  # normalised name → its companies, in order
    groups: dict[int, list[str]] = {}
    disagree = 0
    for index, counterparty in enumerate(report):
        name = normalize_name(counterparty.name)
        single = len(inns_of.get(name, ())) <= 1
        key = next(
            (
                slot
                for slot in slots.get(name, ())
                if joined[slot].inn == counterparty.inn
                or (single and same_company(joined[slot].inn, counterparty.inn))
            ),
            index,
        )
        if counterparty.group and counterparty.group not in groups.setdefault(key, []):
            groups[key].append(counterparty.group)
        first = joined.get(key)
        if first is None:
            order.append(key)
            slots.setdefault(name, []).append(key)
            joined[key] = counterparty
            continue
        known = {contract.name: contract for contract in first.contracts}
        new: list[ContractDebt] = []
        for contract in counterparty.contracts:
            printed = known.get(contract.name)
            if printed is None:
                new.append(contract)
                known[contract.name] = contract
                continue
            disagree += _disagrees(printed, contract)
            known[contract.name] = _fill(printed, contract)
        joined[key] = replace(
            first,
            inn=first.inn or counterparty.inn,
            note=first.note or counterparty.note,
            contracts=tuple(known[contract.name] for contract in (*first.contracts, *new)),
        )
    if len(order) == len(report):
        return tuple(report), ()
    issues = [
        issue(
            DEBT_REPORT_SHEET,
            "debt_report_company_in_several_groups",
            IssueSeverity.WARNING,
            f"«{joined[key].name}» есть в отчёте в нескольких точках продаж "
            f"({', '.join(groups[key])}); договоры объединены — проверьте, что это одна "
            "организация.",
        )
        for key in order
        if len(groups.get(key, ())) > 1
    ]
    if disagree:
        issues.append(
            issue(
                DEBT_REPORT_SHEET,
                "debt_report_contracts_disagree",
                IssueSeverity.WARNING,
                f"Договоры, напечатанные в отчёте дважды, расходятся по сумме или дням "
                f"({disagree}); взяты значения первой печати.",
            )
        )
    return tuple(joined[key] for key in order), tuple(issues)


def _disagrees(first: ContractDebt, repeat: ContractDebt) -> bool:
    return any(
        a is not None and b is not None and a != b
        for a, b in ((first.overdue, repeat.overdue), (first.days, repeat.days))
    )


def _fill(first: ContractDebt, repeat: ContractDebt) -> ContractDebt:
    """The first print, with what it lacks taken from the repeat."""
    return replace(
        first,
        overdue=first.overdue if first.overdue is not None else repeat.overdue,
        days=first.days if first.days is not None else repeat.days,
        due_until=first.due_until or repeat.due_until,
        subject=first.subject or repeat.subject,
        note=first.note or repeat.note,
    )


def worst_days(contracts: tuple[ContractDebt, ...]) -> int | None:
    days = [contract.days for contract in contracts if contract.days is not None]
    return max(days) if days else None
