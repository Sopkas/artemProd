"""Plain-text company card for Telegram (S1-03). Shows facts and limits, never a priority."""

from datetime import date, datetime
from decimal import Decimal

from claims_assistant.application.check_company import CompanyCheck
from claims_assistant.domain.external import (
    CompanyStatus,
    Coverage,
    ExternalSnapshot,
    Fact,
    FactKind,
    FetchStatus,
    Period,
    Section,
)

DEMO_BANNER = "ДЕМО: синтетические данные, не сведения о реальной организации."

_SECTION_TITLES = {
    Section.COMPANY: "Организация",
    Section.BANKRUPTCY: "Сообщения о банкротстве",
    Section.FINANCES: "Финансы",
}
_STATUS_LABELS = {
    CompanyStatus.ACTIVE: "действует",
    CompanyStatus.LIQUIDATING: "в стадии ликвидации",
    CompanyStatus.LIQUIDATED: "ликвидирована",
}
_FACT_LABELS = {
    FactKind.COMPANY_NAME: "Название",
    FactKind.COMPANY_STATUS: "Статус",
    FactKind.REVENUE: "Выручка",
    FactKind.NET_PROFIT: "Чистая прибыль",
    FactKind.BANKRUPTCY_EVENT: "Событие",
}
_COVERAGE_LABELS = {
    Coverage.COMPLETE: "проверено полностью",
    Coverage.PARTIAL: "неполные данные",
    Coverage.UNAVAILABLE: "раздел недоступен",
}
_UNIT_SUFFIX = {"RUB": " ₽"}


def format_card(check: CompanyCheck) -> str:
    lines: list[str] = []
    if check.mode == "demo":
        lines.append(DEMO_BANNER)
        lines.append("")
    lines.append(f"ИНН {check.inn}")
    for snapshot in check.snapshots:
        lines.append("")
        lines.extend(_section(snapshot))
    lines.append("")
    lines.append("Карточка показывает полученные факты и полноту проверки без оценки очерёдности.")
    return "\n".join(lines)


def _section(snapshot: ExternalSnapshot) -> list[str]:
    title = _SECTION_TITLES[snapshot.section]
    lines = [f"{title} — {_COVERAGE_LABELS[snapshot.coverage]}"]
    if snapshot.status != FetchStatus.OK:
        if snapshot.error is not None:
            lines.append(f"  Причина: {snapshot.error.message}")
        for reason in snapshot.missing:
            lines.append(f"  Ограничение: {reason}")
        lines.append(f"  Источник: {snapshot.source}, получено {_date(snapshot.fetched_at)}")
        return lines
    for fact in snapshot.facts:
        lines.append(f"  {_fact(fact)}")
    if not snapshot.facts and snapshot.section == Section.BANKRUPTCY:
        if snapshot.coverage == Coverage.COMPLETE:
            lines.append("  За проверенный период сообщений не найдено.")
        else:
            lines.append("  В полученной части данных сообщений нет; проверка неполная.")
    if snapshot.covered_period is not None:
        lines.append(f"  Период: {_period(snapshot.covered_period)}")
    for reason in snapshot.missing:
        lines.append(f"  Ограничение: {reason}")
    lines.append(f"  Источник: {snapshot.source}, получено {_date(snapshot.fetched_at)}")
    return lines


def _fact(fact: Fact) -> str:
    label = _FACT_LABELS[fact.kind]
    if fact.value is None:
        return f"{label}: неизвестно ({fact.missing_reason})"
    when = _period(fact.period) if fact.period is not None else _date(fact.observed_on)
    if fact.kind == FactKind.COMPANY_STATUS:
        return f"{label}: {_STATUS_LABELS[fact.value]} (на {when})"
    if isinstance(fact.value, Decimal):
        return f"{label} за {when}: {_money(fact.value, fact.unit)}"
    return f"{label}: {fact.value} ({when})"


def _money(value: Decimal, unit: str | None) -> str:
    whole, _, cents = f"{value:,.2f}".partition(".")
    suffix = _UNIT_SUFFIX.get(unit, f" {unit}") if unit else ""
    return f"{whole.replace(',', ' ')},{cents}{suffix}"


def _period(period: Period) -> str:
    if period.start.replace(day=1, month=1) == period.start and period.end == date(
        period.end.year, 12, 31
    ):
        return str(period.start.year) if period.start.year == period.end.year else _span(period)
    return _span(period)


def _span(period: Period) -> str:
    return f"{_date(period.start)} – {_date(period.end)}"


def _date(value: date | datetime | None) -> str:
    if value is None:
        return "дата неизвестна"
    return value.strftime("%d.%m.%Y")
