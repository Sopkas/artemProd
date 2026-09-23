"""S7-01: whose contracts are these — linking the overdue report to the check's rows.

The customer's «Отчет по просроченным лизинговым платежам» names no INN (23.09.2026:
the column «appears in another variant»), so the contracts in it are tied to a company by
**name**, against the names the external source gave us. Both files come out of the same
1C, so the names match far more often than they would between unrelated systems — but
«far more often» is not «always», and everything that did not match is said out loud
rather than dropped.

The comparison ignores what differs between two prints of the same name: case, quotes,
the legal form («ООО», «АО», «ИП»…) wherever it stands, punctuation and repeated spaces.
It never ignores the words themselves: «Ромашка» and «Ромашка-Плюс» stay two companies.

When two companies of one check normalise to the same name, neither gets the contracts:
a guess here would put someone else's debt into a claim letter.
"""

import re
from dataclasses import dataclass, field

from claims_assistant.domain.debt_report import ContractDebt, CounterpartyDebt

_LEGAL_FORMS = (
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
)
_PUNCTUATION = re.compile(r"[«»\"'`.,;:()\[\]/\\-]+")
_SPACES = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """The name as both prints agree on it; empty when nothing is left to compare."""
    text = _PUNCTUATION.sub(" ", name.casefold())
    words = [word for word in _SPACES.split(text) if word and word not in _LEGAL_FORMS]
    return " ".join(words)


@dataclass(frozen=True, slots=True)
class LinkedContracts:
    """Contracts by INN, plus what could not be tied to anybody — and why."""

    by_inn: dict[str, tuple[ContractDebt, ...]] = field(default_factory=dict)
    unknown: tuple[str, ...] = ()  # names of the report that no company of the check has
    ambiguous: tuple[str, ...] = ()  # names that two companies of the check share
    unnamed: tuple[str, ...] = ()  # companies we have no name for (the source was silent)

    @property
    def linked(self) -> int:
        return len(self.by_inn)


def link_contracts(
    report: tuple[CounterpartyDebt, ...], names: dict[str, str | None]
) -> LinkedContracts:
    """Tie every counterparty of the overdue report to an INN of the check by name.

    ``names`` is the check's own INN → company name (from the source, S1-05); a company
    whose name we never learned cannot take part — its INN is reported as ``unnamed``.
    """
    index: dict[str, list[str]] = {}
    unnamed = []
    for inn, name in names.items():
        key = normalize_name(name or "")
        if not key:
            unnamed.append(inn)
            continue
        index.setdefault(key, []).append(inn)

    by_inn: dict[str, list[ContractDebt]] = {}
    unknown: list[str] = []
    ambiguous: list[str] = []
    for counterparty in report:
        key = normalize_name(counterparty.name)
        owners = index.get(key, [])
        if len(owners) > 1:
            ambiguous.append(counterparty.name)
            continue
        if not owners:
            unknown.append(counterparty.name)
            continue
        by_inn.setdefault(owners[0], []).extend(counterparty.contracts)
    return LinkedContracts(
        by_inn={inn: tuple(contracts) for inn, contracts in by_inn.items()},
        unknown=tuple(unknown),
        ambiguous=tuple(ambiguous),
        unnamed=tuple(sorted(set(unnamed))),
    )


def worst_days(contracts: tuple[ContractDebt, ...]) -> int | None:
    days = [contract.days for contract in contracts if contract.days is not None]
    return max(days) if days else None
