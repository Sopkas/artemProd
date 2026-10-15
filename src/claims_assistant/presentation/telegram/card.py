"""Plain-text company card for Telegram (S1-03).

Two forms of one card. The brief one is what the user gets first: the conclusion with its
mark in the first line, the reasons, the owner's key figures (customer's request,
11.10.2026: less text, the point up front). The full one — every fact with its source,
date and coverage — is sent on «Подробнее».
"""

from datetime import date, datetime
from decimal import Decimal

from claims_assistant.application.check_company import CompanyCheck
from claims_assistant.application.internal_context import InternalContext
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
from claims_assistant.domain.indicators import (
    DebtDynamics,
    DebtTrend,
    PaymentRecency,
    PaymentStatus,
)
from claims_assistant.domain.manual_checks import MANUAL_CHECKS
from claims_assistant.domain.scoring import Assessment, Priority, combine

from .texts import PRIORITY_MARKS

DEMO_BANNER = "ДЕМО: синтетические данные, не сведения о реальной организации."

_SECTION_TITLES = {
    Section.COMPANY: "Организация",
    Section.BANKRUPTCY: "Сообщения о банкротстве",
    Section.FINANCES: "Финансы",
    Section.COURTS: "Арбитражные дела (ответчик, незавершённые)",
    Section.FEDRESURS: "Намерения кредиторов о банкротстве (Федресурс)",
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
    FactKind.BANKRUPTCY_CLOSED: "Дело прекращено",
    FactKind.COURT_CASE: "Дело",
    FactKind.COURT_CASES_ACTIVE: "Незавершённых дел",
    FactKind.COURT_CLAIMS_TOTAL: "Сумма исков",
    FactKind.BANKRUPTCY_INTENT: "Сообщение",
}
_COVERAGE_LABELS = {
    Coverage.COMPLETE: "проверено полностью",
    Coverage.PARTIAL: "неполные данные",
    Coverage.UNAVAILABLE: "раздел недоступен",
}
_UNIT_SUFFIX = {"RUB": " ₽"}


_MAX_INTERACTIONS = 5
_MAX_COMMENT_CHARS = 200  # a comment may hold 2000; the card shows the beginning
TELEGRAM_MESSAGE_LIMIT = 4096
# The internal block never pushes the card past the Telegram limit: interactions are
# dropped one by one (newest kept) until the whole text fits.
_CARD_BUDGET = TELEGRAM_MESSAGE_LIMIT - 96
_MAX_CONTRACTS = 5  # the rest are counted: the card is one Telegram message


# The words of the first line, after the mark of the priority (``texts.PRIORITY_MARKS``).
_HEADLINES = {
    Priority.CRITICAL: "Критичный — требует внимания",
    Priority.HIGH: "Высокий — требует внимания",
    Priority.MEDIUM: "Средний — есть основания присмотреться",
    Priority.LOW: "Низкий — проверено полностью, значимых сигналов нет",
    Priority.UNKNOWN: "Недостаточно данных — проверка неполная",
}
_PART_TITLES = {
    Section.COMPANY.value: "организация",
    Section.BANKRUPTCY.value: "сообщения о банкротстве",
    Section.FINANCES.value: "финансы",
    "internal": "внутренние данные",
}
_MAX_REASONS = 3
DETAILS_HINT = "Все факты с источниками и датами — «Подробнее»."
# The register answers by INN whatever the company's role in the case (#52): it lists the
# creditor's messages too. Without the owner's data they alone do not make the mark.
_ROLE_UNCONFIRMED = "unresolved_bankruptcy_event"
ROLE_UNCONFIRMED_LINE = (
    f"{PRIORITY_MARKS[Priority.MEDIUM]} Есть сообщения ЕФРСБ — роль организации не подтверждена"
)


# Neither good nor bad: the external sections were read in full and said nothing alarming,
# but there is no data of the owner's, so no priority of the list.
EXTERNAL_CLEAN_MARK = "ℹ️"


def _headline(assessment: Assessment, internal: InternalContext | None) -> str:
    priority = assessment.priority
    if internal is not None:
        return f"{PRIORITY_MARKS[priority]} {_HEADLINES[priority]}"
    # Without the owner's data there is no priority of the list: only what the external
    # sections say, and whether they were read in full.
    if assessment.signals:
        # The mark comes from the other external rules; the register's messages stay
        # among the reasons below.
        others = [signal for signal in assessment.signals if signal.code != _ROLE_UNCONFIRMED]
        if not others:
            return ROLE_UNCONFIRMED_LINE
        priority = combine(others, base_complete=False)
        return f"{PRIORITY_MARKS[priority]} {_HEADLINES[priority]} (по внешним данным)"
    external = [part for part in assessment.coverage if part != "internal"]
    if all(assessment.coverage[part] is Coverage.COMPLETE for part in external):
        # Not ✅: without the owner's data the check is not complete, and a green tick read
        # first would pass for «reliable» (review A on #86). ✅ belongs to Priority.LOW only.
        return f"{EXTERNAL_CLEAN_MARK} Во внешних данных значимых сигналов нет"
    return f"{PRIORITY_MARKS[Priority.UNKNOWN]} Внешние данные неполные"


def _company_name(check: CompanyCheck, internal: InternalContext | None) -> str | None:
    for snapshot in check.snapshots:
        for fact in snapshot.facts:
            if fact.kind is FactKind.COMPANY_NAME and isinstance(fact.value, str):
                return fact.value
    return internal.row.name if internal is not None else None


def _brief_courts(check: CompanyCheck) -> str | None:
    """S8-01: the claim load in one line — the customer asked to see it at a glance. The
    rules do not raise a priority on it yet (no agreed threshold), so it is a figure here."""
    snapshot = next((s for s in check.snapshots if s.section is Section.COURTS), None)
    if snapshot is None or snapshot.status is not FetchStatus.OK:
        return None
    count = next((f.value for f in snapshot.facts if f.kind is FactKind.COURT_CASES_ACTIVE), None)
    if not isinstance(count, int):
        return None
    if count == 0:
        return "Арбитраж: незавершённых дел, где организация ответчик, нет."
    claims = next((f.value for f in snapshot.facts if f.kind is FactKind.COURT_CLAIMS_TOTAL), None)
    total = f", иски на {_money(claims, 'RUB')}" if isinstance(claims, Decimal) else ""
    return f"Арбитраж: незавершённых дел, где организация ответчик, — {count}{total}."


def _brief_internal(context: InternalContext) -> list[str]:
    row = context.row
    figures = []
    if row.debt is not None:
        figures.append(f"Долг {_money(row.debt, 'RUB')}")
    if row.overdue_days is not None:
        figures.append(f"просрочка {row.overdue_days} дн.")
    payment = context.indicators.payment
    if payment.status is PaymentStatus.CONFIRMED:
        figures.append(f"платёж {_date(payment.last_payment)}")
    elif payment.status is PaymentStatus.NONE_SINCE:
        figures.append(f"поступлений нет с {_date(payment.no_payments_since)}")
    lines = [f"Внутренние данные — проверка от {_date(context.run.analysis_date)}"]
    if figures:
        line = " · ".join(figures)
        lines.append(line[0].upper() + line[1:])
    if context.contracts:
        lines.append(f"Договоры с просрочкой: {len(context.contracts)}")
    if context.interactions:
        last = context.interactions[-1]
        lines.append(
            f"Взаимодействия: {len(context.interactions)}, последнее {_date(last.happened_on)}"
        )
    return lines


def format_brief(
    check: CompanyCheck, assessment: Assessment, internal: InternalContext | None = None
) -> str:
    """The card in a few lines: the conclusion first, then why, then the owner's figures."""
    lines: list[str] = []
    if check.mode == "demo":
        lines += [DEMO_BANNER, ""]
    lines.append(_headline(assessment, internal))
    name = _company_name(check, internal)
    lines.append(f"{name}, ИНН {check.inn}" if name else f"ИНН {check.inn}")
    if assessment.signals:
        lines.append("")
        lines += [f"• {signal.reason}" for signal in assessment.signals[:_MAX_REASONS]]
        if len(assessment.signals) > _MAX_REASONS:
            lines.append(f"• ещё причин: {len(assessment.signals) - _MAX_REASONS}")
    courts = _brief_courts(check)
    if courts:
        lines += ["", courts]
    if internal is not None:
        lines.append("")
        lines += _brief_internal(internal)
    else:
        lines += [
            "",
            "Своих данных по этой организации нет: приоритет считается в проверке списка.",
        ]
    gaps = [
        _PART_TITLES.get(part, part)
        for part, coverage in assessment.coverage.items()
        if coverage is not Coverage.COMPLETE and (part != "internal" or internal is not None)
    ]
    if gaps:
        lines.append("Проверено не полностью: " + ", ".join(gaps) + ".")
        # Why, when the source gave one reason for all of it: a spent limit or a lost
        # connection reads differently from «nothing found» and must not be a button away.
        causes = {
            snapshot.error.message
            for snapshot in check.snapshots
            if snapshot.status is not FetchStatus.OK and snapshot.error is not None
        }
        if len(causes) == 1:
            lines.append(f"Причина: {causes.pop()}")
    lines += ["", *(f"{manual.title}: бот не проверяет." for manual in MANUAL_CHECKS), DETAILS_HINT]
    return "\n".join(lines)


def format_card(check: CompanyCheck, internal: InternalContext | None = None) -> str:
    lines: list[str] = []
    if check.mode == "demo":
        lines.append(DEMO_BANNER)
        lines.append("")
    lines.append(f"ИНН {check.inn}")
    for snapshot in check.snapshots:
        lines.append("")
        lines.extend(_section(snapshot))
    footer = [
        "",
        *(manual.line() for manual in MANUAL_CHECKS),
        "",
        "Здесь факты и полнота проверки; вывод — в краткой карточке.",
    ]
    if internal is None:
        return "\n".join(lines + footer)
    budget = _CARD_BUDGET - len("\n".join(lines + footer)) - 1
    return "\n".join(lines + [""] + _internal(internal, budget) + footer)


def _internal(context: InternalContext, budget: int) -> list[str]:
    """The owner's own data on this company (S4-04): which files, what they give, what
    is missing. Amounts and comments are the owner's; nothing here is sent anywhere.
    ``budget`` is how many characters the block may take so the card still fits one
    Telegram message; interactions give way first."""
    run = context.run
    row = context.row
    lines = [f"Внутренние данные — проверка от {_date(run.analysis_date)}"]
    lines.append("  Файлы: " + "; ".join(context.files))
    if row.debt is not None:
        when = f" на {_date(row.cutoff_date)}" if row.cutoff_date else ""
        lines.append(f"  Долг: {_money(row.debt, 'RUB')}{when}")
    if row.overdue_days is not None:
        lines.append(f"  Просрочка: {row.overdue_days} дн.")
    lines.append("  Давность платежа: " + _payment_line(context.indicators.payment))
    lines.append("  Долг за месяц: " + _debt_line(context.indicators.debt))
    revenue = context.indicators.revenue
    if revenue is not None:
        sign = "+" if revenue.percent >= 0 else "−"
        lines.append(
            f"  Выручка за {revenue.year}: {sign}{abs(revenue.percent):.0f} % к предыдущему году"
        )
    lines.extend(_contract_lines(context.contracts))
    tail = [f"  Не хватает: {note}" for note in context.indicators.missing]
    if not context.interactions:
        return lines + ["  Взаимодействия: файл не загружен."] + tail
    total = len(context.interactions)
    shown = list(context.interactions[-_MAX_INTERACTIONS:])
    while True:
        block = [f"  Взаимодействия: {total}, последние {len(shown)}:"] + [
            _interaction_line(item) for item in shown
        ]
        if len("\n".join(lines + block + tail)) <= budget or not shown:
            return lines + block + tail
        shown = shown[1:]  # drop the oldest shown; the newest stay


def _contract_lines(contracts) -> list[str]:
    """Contracts of the overdue report, worst overdue first (S7-01).

    The specialist calls about a contract, so the card names them instead of one total;
    when there are many, the rest are counted rather than dropped silently.
    """
    if not contracts:
        return []
    lines = [f"  Договоры с просрочкой: {len(contracts)}"]
    for contract in contracts[:_MAX_CONTRACTS]:
        parts = [contract.name]
        if contract.days is not None:
            parts.append(f"{contract.days} дн.")
        if contract.overdue is not None:
            parts.append(_money(contract.overdue, "RUB"))
        if contract.subject:
            parts.append(contract.subject)
        lines.append("    " + " · ".join(parts))
    if len(contracts) > _MAX_CONTRACTS:
        lines.append(f"    ещё {len(contracts) - _MAX_CONTRACTS}")
    return lines


def _interaction_line(item) -> str:
    channel = f" ({item.channel})" if item.channel else ""
    comment = item.comment
    if len(comment) > _MAX_COMMENT_CHARS:
        comment = comment[: _MAX_COMMENT_CHARS - 1].rstrip() + "…"
    return f"    {_date(item.happened_on)}{channel}: {comment}"


def _payment_line(recency: PaymentRecency) -> str:
    if recency.status is PaymentStatus.CONFIRMED:
        return (
            f"последний платёж {_date(recency.last_payment)}, {recency.age_days} дн. назад "
            "(подтверждено)"
        )
    if recency.status is PaymentStatus.NONE_SINCE:
        return (
            f"поступлений нет с {_date(recency.no_payments_since)} — {recency.age_days} дн. "
            "(выгрузка полная)"
        )
    if recency.status is PaymentStatus.IN_PERIOD:
        last = (
            f"последний платёж в выгрузке {_date(recency.period_last)}; "
            if recency.period_last
            else ""
        )
        return (
            last + "давность не подтверждена" + (f" — {recency.reason}" if recency.reason else "")
        )
    if recency.status is PaymentStatus.CONFLICT:
        return "неизвестна до исправления — файлы противоречат друг другу"
    return "неизвестна" + (f" — {recency.reason}" if recency.reason else "")


def _debt_line(dynamics: DebtDynamics) -> str:
    if dynamics.trend is DebtTrend.COMPUTED:
        ratio = f"×{dynamics.ratio:.2f}".replace(".", ",")
        return (
            f"{_money(dynamics.previous, 'RUB')} ({_date(dynamics.previous_on)}) → "
            f"{_money(dynamics.current, 'RUB')} ({_date(dynamics.current_on)}), {ratio}"
        )
    if dynamics.trend is DebtTrend.APPEARED:
        return f"долг появился: {_money(dynamics.current, 'RUB')} ({_date(dynamics.current_on)})"
    if dynamics.trend is DebtTrend.NO_DEBT:
        return "долга нет ни сейчас, ни месяц назад"
    if dynamics.trend is DebtTrend.NO_BASE:
        return "нет среза месяц назад" + (f" — {dynamics.reason}" if dynamics.reason else "")
    if dynamics.trend is DebtTrend.CONFLICT:
        return "неизвестна до исправления — файлы противоречат друг другу"
    return "неизвестна" + (f" — {dynamics.reason}" if dynamics.reason else "")


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
    if not snapshot.facts and snapshot.section == Section.FEDRESURS:
        lines.append("  Кредиторы о намерении подать на банкротство не сообщали.")
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
    if fact.kind is FactKind.COURT_CLAIMS_TOTAL and isinstance(fact.value, Decimal):
        return f"{label} на {when}: {_money(fact.value, fact.unit)}"
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
