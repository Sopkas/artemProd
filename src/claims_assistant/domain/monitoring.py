"""S8-06 (part B): what counts as a change between two checks of one organization.

Pure domain code: no network, storage or clock. On input — the section snapshots of one INN
from the previous check and from today's; on output — what appeared since, and which
sections today could not tell. Keeping the snapshots, the schedule and the message are the
monitoring service's (part A).

What to watch was agreed with the customer on 16.10.2026: bankruptcy, debts (ФССП) and
arbitration; an arbitration case matters when the organization is the defendant and the
claim is 100 000 ₽ or more. One more is added here: a change of the company's status — a
liquidation is no less a reason to act than a bankruptcy.

Rules:

- **Only what appeared is a change.** A case that was closed or a proceeding that ended is
  good news and is not sent; the card shows the state on the day.
- **A record is known by the source's own identifier** (the case UUID or number, the GUID of
  a message, the proceeding number), not by its place in the list.
- **The first check is the baseline:** a section without a previous successful snapshot
  reports nothing as new — otherwise the first day sends every case the company has.
- **A failed section is «не проверено», never «изменений нет».** If the previous snapshot
  failed too, the section is a baseline again: nothing to compare with.
- **A cut list** (the newest hundred) may let an older record in when a newer one leaves.
  If the previous list was partial, a record older than its oldest one is not taken as new.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from claims_assistant.domain.external import (
    CompanyStatus,
    Coverage,
    ExternalSnapshot,
    Fact,
    FactKind,
    FetchStatus,
    Section,
)

__all__ = [
    "RULES_VERSION",
    "COURT_CLAIM_THRESHOLD",
    "MONITORED",
    "Change",
    "SectionState",
    "Comparison",
    "claim_of",
    "compare",
]

RULES_VERSION = "0.1"
# Agreed with the customer on 16.10.2026: a case as the defendant from this claim on.
COURT_CLAIM_THRESHOLD = Decimal("100000")
# Finances come once a year and are not a daily change.
MONITORED = (
    Section.COMPANY,
    Section.BANKRUPTCY,
    Section.COURTS,
    Section.FEDRESURS,
    Section.ENFORCEMENTS,
)
# The records whose appearance is a change, and how the change is named.
_NEW = {
    FactKind.BANKRUPTCY_CASE: "Новое дело о банкротстве организации в арбитражном суде",
    FactKind.BANKRUPTCY_FILING: "На Федресурсе — заявление о банкротстве организации",
    FactKind.BANKRUPTCY_INTENT: "Кредитор объявил о намерении подать на банкротство организации",
    FactKind.BANKRUPTCY_EVENT: "Новое сообщение ЕФРСБ",
    FactKind.COURT_CASE: "Новое арбитражное дело, организация — ответчик",
    FactKind.ENFORCEMENT: "Новое исполнительное производство (ФССП)",
}
_STATUS_LABELS = {
    CompanyStatus.ACTIVE: "действует",
    CompanyStatus.LIQUIDATING: "в стадии ликвидации",
    CompanyStatus.LIQUIDATED: "ликвидирована",
}
# The adapters write the claim into the case's label: «… · иск 2 400 000 ₽ · …»
# (infrastructure/checko/courts.py), «требование» in a bankruptcy case.
_CLAIM = re.compile(r"(?:^|· )(?:иск|требование) (\d{1,3}(?: \d{3})*) ₽")


class SectionState(StrEnum):
    COMPARED = "compared"
    # The first successful check, or the previous one failed: nothing to compare with.
    BASELINE = "baseline"
    # Today's answer failed or the section was not asked: changes are unknown.
    UNCHECKED = "unchecked"


@dataclass(frozen=True, slots=True)
class Change:
    inn: str
    section: Section
    kind: FactKind
    # A sentence for the message: what happened, then the record as the source names it.
    text: str
    observed_on: date | None
    record_id: str
    url: str | None = None
    # False for a case below the threshold: kept for a summary, not a reason to write.
    notify: bool = True


@dataclass(frozen=True, slots=True)
class Comparison:
    inn: str
    changes: tuple[Change, ...]
    states: tuple[tuple[Section, SectionState], ...]
    # Why a section is unchecked or partial today, in the source's words.
    notes: tuple[tuple[Section, str], ...] = ()

    @property
    def to_notify(self) -> tuple[Change, ...]:
        return tuple(change for change in self.changes if change.notify)

    @property
    def unchecked(self) -> tuple[Section, ...]:
        return tuple(s for s, state in self.states if state is SectionState.UNCHECKED)

    @property
    def nothing_new(self) -> bool:
        """«Изменений нет» is only true when every watched section was compared today."""
        return not self.to_notify and all(
            state is SectionState.COMPARED for _section, state in self.states
        )


def claim_of(fact: Fact) -> Decimal | None:
    """The claim of one case as its label names it; None when the source named none."""
    if not isinstance(fact.value, str):
        return None
    match = _CLAIM.search(fact.value)
    return None if match is None else Decimal(match.group(1).replace(" ", ""))


def _by_section(snapshots: Iterable[ExternalSnapshot], inn: str) -> dict[Section, ExternalSnapshot]:
    result = {}
    for snapshot in snapshots:
        if snapshot.inn != inn:
            raise ValueError("Snapshots of one comparison must share the INN")
        result[snapshot.section] = snapshot
    return result


def _record(snapshot: ExternalSnapshot, fact: Fact) -> tuple[str, str | None]:
    """The source's identifier of the fact's record and the page a person can open."""
    evidence = {item.id: item for item in snapshot.evidence}
    item = evidence[fact.evidence_ids[0]]  # the contract checks it is there
    return item.record_id, item.url


def _status(snapshot: ExternalSnapshot) -> CompanyStatus | None:
    for fact in snapshot.facts:
        if fact.kind is FactKind.COMPANY_STATUS and isinstance(fact.value, CompanyStatus):
            return fact.value
    return None


def _new_records(before: ExternalSnapshot, now: ExternalSnapshot) -> list[Change]:
    known = {(fact.kind, _record(before, fact)[0]) for fact in before.facts if fact.kind in _NEW}
    # A cut list: an older record entering it is not a new one.
    oldest = None
    if before.coverage is Coverage.PARTIAL:
        dates = [f.observed_on for f in before.facts if f.kind in _NEW and f.observed_on]
        oldest = min(dates, default=None)
    changes = []
    for fact in now.facts:
        if fact.kind not in _NEW:
            continue
        record_id, url = _record(now, fact)
        if (fact.kind, record_id) in known:
            continue
        if oldest is not None and fact.observed_on is not None and fact.observed_on < oldest:
            continue
        text = f"{_NEW[fact.kind]}: {fact.value}"
        notify = True
        if fact.kind is FactKind.COURT_CASE:
            claim = claim_of(fact)
            if claim is None:
                text += " (сумма иска не указана)"  # unknown is not «below the threshold»
            else:
                notify = claim >= COURT_CLAIM_THRESHOLD
        changes.append(
            Change(
                now.inn,
                now.section,
                fact.kind,
                text,
                fact.observed_on,
                record_id,
                url,
                notify,
            )
        )
    return changes


def _status_change(before: ExternalSnapshot, now: ExternalSnapshot) -> list[Change]:
    old, new = _status(before), _status(now)
    if old is None or new is None or old is new:
        return []
    fact = next(f for f in now.facts if f.kind is FactKind.COMPANY_STATUS)
    record_id, url = _record(now, fact)
    return [
        Change(
            now.inn,
            now.section,
            FactKind.COMPANY_STATUS,
            f"Статус организации изменился: был «{_STATUS_LABELS[old]}», "
            f"теперь «{_STATUS_LABELS[new]}»",
            fact.observed_on,
            record_id,
            url,
        )
    ]


def compare(
    inn: str,
    previous: Iterable[ExternalSnapshot],
    current: Iterable[ExternalSnapshot],
) -> Comparison:
    """Changes of one organization since the previous check.

    ``previous`` is the last *successful* snapshot of each section — the service keeps it
    over a failed day, so a case that appeared while the source was down is still new.
    """
    before = _by_section(previous, inn)
    now = _by_section(current, inn)
    changes: list[Change] = []
    states = []
    notes = []
    for section in MONITORED:
        today = now.get(section)
        if today is None:
            states.append((section, SectionState.UNCHECKED))
            notes.append((section, "Раздел сегодня не запрашивался."))
            continue
        if today.status is not FetchStatus.OK:
            states.append((section, SectionState.UNCHECKED))
            notes.extend((section, note) for note in today.missing)
            continue
        if today.coverage is Coverage.PARTIAL:
            notes.extend((section, note) for note in today.missing)
        last = before.get(section)
        if last is None or last.status is not FetchStatus.OK:
            states.append((section, SectionState.BASELINE))
            continue
        states.append((section, SectionState.COMPARED))
        changes += _status_change(last, today) if section is Section.COMPANY else []
        changes += _new_records(last, today)
    return Comparison(inn, tuple(changes), tuple(states), tuple(notes))
