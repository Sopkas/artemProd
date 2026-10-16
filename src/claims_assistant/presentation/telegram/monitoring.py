"""S8-09 (texts, part B): what the daily monitoring writes.

Two kinds of message, both built from ``domain.monitoring.compare`` and nothing else:

- **One per company with a change worth writing** (``format_changes``): who, what appeared,
  when and the link to the source; the sections that could not be read today, with the
  reason — a change there is unknown, not absent.
- **One summary of the day** (``format_daily_summary``): how many were checked, which
  changed, which were checked for the first time (a baseline: changes start tomorrow),
  and which could not be read and why. «Изменений нет» is said only when every company
  was compared in full — a failed source is never silence (agreed 16.10.2026).

Whether the summary goes out on a quiet day is the customer's call (S8-07, open); the
caller decides, the text is ready either way.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from claims_assistant.domain.external import Section
from claims_assistant.domain.monitoring import Comparison, SectionState
from claims_assistant.domain.plural import companies, of_companies

# Telegram refuses longer messages; long lists are cut well before it.
MESSAGE_LIMIT = 4096
MAX_CHANGES = 10
MAX_NAMES = 10

SECTION_NAMES = {
    Section.COMPANY: "сведения об организации",
    Section.BANKRUPTCY: "сообщения ЕФРСБ",
    Section.COURTS: "арбитражные дела",
    Section.FEDRESURS: "Федресурс",
    Section.ENFORCEMENTS: "ФССП",
    Section.FINANCES: "финансы",
}


@dataclass(frozen=True, slots=True)
class Checked:
    """One company of the day's run: whom it names and what the comparison found."""

    inn: str
    name: str | None
    comparison: Comparison


def _who(inn: str, name: str | None) -> str:
    return f"{name}, ИНН {inn}" if name else f"ИНН {inn}"


def _day(value: date) -> str:
    return f"{value:%d.%m.%Y}"


def _cut(text: str) -> str:
    if len(text) <= MESSAGE_LIMIT:
        return text
    return text[: MESSAGE_LIMIT - 1].rsplit("\n", 1)[0] + "\n…"


def _inside(sentence: str) -> str:
    """A reason after a dash: «Источник не ответил» → «источник не ответил»; an acronym at
    the start («ФНС …») keeps its capitals."""
    if len(sentence) > 1 and sentence[0].isupper() and sentence[1].islower():
        return sentence[0].lower() + sentence[1:]
    return sentence


def _unchecked(comparison: Comparison) -> list[str]:
    """«арбитражные дела — лимит запросов исчерпан» for every section not read today."""
    reasons = {}
    for section, note in comparison.notes:
        reasons.setdefault(section, _inside(note.rstrip(".")))
    return [
        f"{SECTION_NAMES.get(s, s.value)} — {reasons[s]}" if s in reasons
        else SECTION_NAMES.get(s, s.value)
        for s in comparison.unchecked
    ]  # fmt: skip


def format_changes(
    inn: str, name: str | None, comparison: Comparison, checked_on: date
) -> str | None:
    """The message about one company, or None when there is nothing worth writing."""
    changes = comparison.to_notify
    if not changes:
        return None
    lines = [f"🔔 Мониторинг, {_day(checked_on)}: {_who(inn, name)}", ""]
    for change in changes[:MAX_CHANGES]:
        when = f" ({_day(change.observed_on)})" if change.observed_on else ""
        lines.append(f"• {change.text}{when}")
        if change.url:
            lines.append(f"  {change.url}")
    if len(changes) > MAX_CHANGES:
        lines.append(f"• …и ещё {len(changes) - MAX_CHANGES}")
    unchecked = _unchecked(comparison)
    if unchecked:
        lines += ["", "Не проверено сегодня, изменения там неизвестны:"]
        lines += [f"  {line}" for line in unchecked]
    return _cut("\n".join(lines))


def format_daily_summary(day: date, checked: Sequence[Checked]) -> str:
    """The day in one message: how many, which changed, what is not known and why."""
    lines = [f"📋 Мониторинг на {_day(day)}. Организаций проверено: {len(checked)}."]
    changed = [item for item in checked if item.comparison.to_notify]
    first = [
        item
        for item in checked
        if any(state is SectionState.BASELINE for _s, state in item.comparison.states)
    ]
    unread = [item for item in checked if item.comparison.unchecked]
    if changed:
        lines += ["", f"Изменения у {of_companies(len(changed))} — подробности выше:"]
        lines += [f"  {_who(item.inn, item.name)}" for item in changed[:MAX_NAMES]]
        if len(changed) > MAX_NAMES:
            lines.append(f"  …и ещё {len(changed) - MAX_NAMES}")
    elif checked and all(item.comparison.nothing_new for item in checked):
        lines += ["", "Изменений нет."]
    else:
        lines += ["", "Новых изменений не найдено, но проверено не всё — ниже."]
    if first:
        lines += [
            "",
            f"Впервые проверено: {companies(len(first))}. Это точка отсчёта — "
            "изменения будут видны со следующей проверки.",
        ]
    if unread:
        reasons = Counter(line for item in unread for line in _unchecked(item.comparison))
        lines += ["", f"Проверено не полностью: {companies(len(unread))}."]
        lines += [f"  {reason}: {count}" for reason, count in reasons.most_common(5)]
    return _cut("\n".join(lines))
