"""S8-08: the monitoring list — which INNs the bot watches, and how a list gets into it.

Agreed with the customer on 16.10.2026: the list is his own — whatever INNs he sends, in
a message, a file or a new 1C report; up to 100 companies (about 500 Checko requests a
day, the tariff is chosen for that); what he sends is **added** to the list, and what he
no longer needs can be taken out. Storing the list, the buttons and the daily run are the
monitoring service's (S8-09); here are the rules, with no storage and no clock.

How a message is read: every run of digits that has the length of an INN is one. Words,
numbering («1.», «2)»), dates and amounts around them are left alone, so a list copied
from anywhere — a column of a table, a line with names, a chat message — works as it is.
A run of nine or eleven digits is taken for a mistyped INN and named, not dropped: one
digit lost is the commonest slip, and silence would leave the company unwatched.

An error never repeats the value (``domain/inn``): it says the position in the list.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from claims_assistant.domain.inn import InnProblem, InvalidInn, validate_inn

__all__ = [
    "WATCH_LIMIT",
    "Candidate",
    "WatchEntry",
    "Invalid",
    "ParsedList",
    "AddResult",
    "RemoveResult",
    "parse_inns",
    "add",
    "remove",
]

WATCH_LIMIT = 100
# Lengths of an INN, and of one with a digit lost or added: taken for a typo, not skipped.
_INN_LENGTHS = (10, 12)
_TYPO_LENGTHS = (9, 11)
_DIGITS = re.compile(r"(?<!\d)\d+(?!\d)")
# «16.10.2026», «16.10.26»: a date is cut out before the digits are read.
_DATE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One company to watch: its INN and, when the source names it, its name."""

    inn: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class WatchEntry:
    """One company on the list as the «Мониторинг» file shows it (S8-09): what the service
    stores about it, whatever the storage."""

    inn: str
    added_on: date
    name: str | None = None
    last_checked_on: date | None = None
    last_change: str | None = None
    last_change_on: date | None = None


@dataclass(frozen=True, slots=True)
class Invalid:
    """A value that looks like an INN and is not one; ``position`` is its number in the
    message, or the row of the file."""

    position: int
    problem: InnProblem

    @property
    def reason(self) -> str:
        return str(InvalidInn(self.problem))


@dataclass(frozen=True, slots=True)
class ParsedList:
    candidates: tuple[Candidate, ...] = ()
    invalid: tuple[Invalid, ...] = ()
    repeats: int = 0  # the same INN more than once in what was sent


def unique(candidates: Iterable[Candidate]) -> tuple[tuple[Candidate, ...], int]:
    """First occurrence of every INN, in order, and how many repeats were dropped. A name
    from a later repeat fills a gap the first one left."""
    seen: dict[str, Candidate] = {}
    repeats = 0
    for candidate in candidates:
        first = seen.get(candidate.inn)
        if first is None:
            seen[candidate.inn] = candidate
            continue
        repeats += 1
        if first.name is None and candidate.name:
            seen[candidate.inn] = candidate
    return tuple(seen.values()), repeats


def parse_inns(text: str) -> ParsedList:
    """Every INN in a message, in the order written, without repeats."""
    found = []
    invalid = []
    position = 0
    for match in _DIGITS.finditer(_DATE.sub(" ", text)):
        digits = match.group()
        if len(digits) in _TYPO_LENGTHS:
            position += 1
            problem = InnProblem.NINE_DIGITS if len(digits) == 9 else InnProblem.WRONG_LENGTH
            invalid.append(Invalid(position, problem))
            continue
        if len(digits) not in _INN_LENGTHS:
            continue  # numbering, an amount, an ОГРН: not an INN and not a typo of one
        position += 1
        try:
            found.append(Candidate(validate_inn(digits)))
        except InvalidInn as error:
            invalid.append(Invalid(position, error.problem))
    candidates, repeats = unique(found)
    return ParsedList(candidates, tuple(invalid), repeats)


@dataclass(frozen=True, slots=True)
class AddResult:
    added: tuple[Candidate, ...] = ()
    already: tuple[Candidate, ...] = ()  # on the list before
    over_limit: tuple[Candidate, ...] = ()  # not added: the list is full
    total: int = 0  # on the list after
    limit: int = WATCH_LIMIT


def add(
    current: Sequence[str], candidates: Iterable[Candidate], limit: int = WATCH_LIMIT
) -> AddResult:
    """Add what is new, in the order sent, until the list is full.

    Nothing is dropped from the list to make room: which company the customer no longer
    needs is his call, so what does not fit is named, and he removes what he chooses.
    """
    if limit <= 0:
        raise ValueError("The list limit must be positive")
    present = set(current)
    room = max(0, limit - len(present))
    added, already, over = [], [], []
    for candidate in unique(candidates)[0]:
        if candidate.inn in present:
            already.append(candidate)
        elif room > 0:
            added.append(candidate)
            present.add(candidate.inn)
            room -= 1
        else:
            over.append(candidate)
    return AddResult(tuple(added), tuple(already), tuple(over), len(present), limit)


@dataclass(frozen=True, slots=True)
class RemoveResult:
    removed: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()  # asked to remove, but not on the list
    total: int = 0


def remove(current: Sequence[str], inns: Iterable[str]) -> RemoveResult:
    """Take these INNs off the list; one that was not on it is named, not an error."""
    present = set(current)
    removed, absent = [], []
    for inn in dict.fromkeys(inns):
        if inn in present:
            removed.append(inn)
            present.discard(inn)
        else:
            absent.append(inn)
    return RemoveResult(tuple(removed), tuple(absent), len(present))
