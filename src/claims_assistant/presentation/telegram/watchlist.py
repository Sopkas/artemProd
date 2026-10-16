"""S8-08: what the user reads after sending a list for monitoring, or taking one out.

One message, the outcome first: how many were added, then what was not and why — already
there, repeated, not an INN, over the limit, a report's company without an INN — and the
list's size last, against its limit. Long lists are cut to their first few lines: the
numbers are what the user needs, the full list is the «Мониторинг» file (S8-09).
"""

from collections.abc import Sequence

from claims_assistant.domain.plural import companies
from claims_assistant.domain.watchlist import AddResult, Candidate, Invalid, RemoveResult

MAX_LINES = 5


def _cut(lines: Sequence[str]) -> list[str]:
    shown = [f"  {line}" for line in lines[:MAX_LINES]]
    if len(lines) > MAX_LINES:
        shown.append(f"  …и ещё {len(lines) - MAX_LINES}")
    return shown


def _invalid(items: Sequence[Invalid], *, from_file: bool) -> list[str]:
    where = "строка" if from_file else "№"
    return _cut([f"{where} {item.position}: {item.reason}" for item in items])


def _candidate(candidate: Candidate) -> str:
    return f"{candidate.inn} — {candidate.name}" if candidate.name else candidate.inn


def format_added(
    result: AddResult,
    invalid: Sequence[Invalid] = (),
    *,
    repeats: int = 0,
    without_inn: int = 0,
    from_file: bool = False,
) -> str:
    lines = [f"Добавлено на мониторинг: {len(result.added)}."]
    if result.already:
        lines.append(f"Уже были на мониторинге: {len(result.already)}.")
    if repeats:
        lines.append(f"Повторы пропущены: {repeats}.")
    if invalid:
        lines.append(f"Не ИНН, проверьте ({len(invalid)}):")
        lines += _invalid(invalid, from_file=from_file)
    if without_inn:
        lines.append(
            f"Без ИНН в отчёте: {companies(without_inn)} — по названию мониторинг не ведётся."
        )
    if result.over_limit:
        lines.append(
            f"Не добавлены — на мониторинге не больше {result.limit} организаций "
            f"({len(result.over_limit)}). Уберите ненужные и пришлите эти ИНН снова:"
        )
        lines += _cut([_candidate(candidate) for candidate in result.over_limit])
    lines.append(f"Сейчас на мониторинге: {result.total} из {result.limit}.")
    return "\n".join(lines)


def format_removed(result: RemoveResult, invalid: Sequence[Invalid] = (), limit: int = 0) -> str:
    lines = [f"Убрано с мониторинга: {len(result.removed)}."]
    if result.absent:
        lines.append(f"Не было на мониторинге ({len(result.absent)}):")
        lines += _cut(list(result.absent))
    if invalid:
        lines.append(f"Не ИНН, проверьте ({len(invalid)}):")
        lines += _invalid(invalid, from_file=False)
    total = f"{result.total} из {limit}" if limit else str(result.total)
    lines.append(f"Сейчас на мониторинге: {total}.")
    return "\n".join(lines)
