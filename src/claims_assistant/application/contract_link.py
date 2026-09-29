"""S7-01: whose contracts are these — linking the overdue report to the check's rows.

The customer's «Отчет по просроченным лизинговым платежам» names no INN (23.09.2026:
the column «appears in another variant»), so the contracts in it are tied to a company by
**name**, against the names the external source gave us. Both files come out of the same
1C, so the names match far more often than they would between unrelated systems — but
«far more often» is not «always», and everything that did not match is said out loud
rather than dropped.

Two keys, tried in that order (review B on #58):

1. **with the legal form** — «ООО Ромашка» and «АО Ромашка» are two different legal
   entities, and in a leasing portfolio such a pair is not exotic. The form is printed
   from the same field in both exports, so it is a difference we can trust;
2. **without it**, and only when exactly one company is left — for the case where one
   print carries the form and the other does not. Such a match is reported, so a person
   can glance at it.

Either key ignores what differs between two prints of one name: case, quotes, punctuation
and repeated spaces. Neither ignores the words themselves: «Ромашка» and «Ромашка-Плюс»
stay two companies, and a hyphen is kept inside a word so «ИП-Сервис» is not read as the
form «ИП» plus «Сервис».

When two companies of one check share a key, neither gets the contracts: a guess here
would put someone else's debt into a claim letter.
"""

import re
from dataclasses import dataclass, field

from claims_assistant.domain.debt_report import ContractDebt, CounterpartyDebt

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
_PUNCTUATION = re.compile(r"[«»\"'`.,;:()\[\]/\\]+")
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
    report: tuple[CounterpartyDebt, ...], names: dict[str, str | None]
) -> LinkedContracts:
    """Tie every counterparty of the overdue report to an INN of the check by name.

    ``names`` is the check's own INN → company name (from the source, S1-05); a company
    whose name we never learned cannot take part — its INN is reported as ``unnamed``.
    """
    strict = _index(names, normalize_name)
    loose = _index(names, name_without_form)
    # Only worth saying when there is a report to tie: without one, a company the source
    # does not know simply has no contracts, and a warning about them would be noise.
    unnamed = (
        [inn for inn, name in names.items() if not normalize_name(name or "")] if report else []
    )

    by_inn: dict[str, list[ContractDebt]] = {}
    unknown: list[str] = []
    ambiguous: list[str] = []
    without_form: list[str] = []
    for counterparty in report:
        owners = strict.get(normalize_name(counterparty.name), [])
        if len(owners) == 1:
            by_inn.setdefault(owners[0], []).extend(counterparty.contracts)
            continue
        if len(owners) > 1:
            ambiguous.append(counterparty.name)
            continue
        relaxed = loose.get(name_without_form(counterparty.name), [])
        if len(relaxed) == 1:
            without_form.append(counterparty.name)
            by_inn.setdefault(relaxed[0], []).extend(counterparty.contracts)
        elif relaxed:
            ambiguous.append(counterparty.name)
        else:
            unknown.append(counterparty.name)
    return LinkedContracts(
        by_inn={inn: tuple(contracts) for inn, contracts in by_inn.items()},
        unknown=tuple(unknown),
        ambiguous=tuple(ambiguous),
        unnamed=tuple(sorted(set(unnamed))),
        without_form=tuple(without_form),
    )


def worst_days(contracts: tuple[ContractDebt, ...]) -> int | None:
    days = [contract.days for contract in contracts if contract.days is not None]
    return max(days) if days else None
