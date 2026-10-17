"""Plain-text company card for Telegram (S1-03).

Two forms of one card. The brief one is what the user gets first: the conclusion with its
mark in the first line, the reasons, the owner's key figures (customer's request,
11.10.2026: less text, the point up front). The full one — every fact with its source,
date and coverage — is sent on «Подробнее».
"""

from collections.abc import Mapping
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal

from claims_assistant.application.ai_guard import BUDGET_MESSAGE
from claims_assistant.application.card_review import CardReview
from claims_assistant.application.check_company import CompanyCheck
from claims_assistant.application.internal_context import InternalContext
from claims_assistant.application.recommendation import AiErrorCode
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
from claims_assistant.domain.scoring import (
    Assessment,
    FinanceTrend,
    Priority,
    Trend,
    combine,
    finance_trends,
)

from .texts import PRIORITY_MARKS

DEMO_BANNER = "ДЕМО: синтетические данные, не сведения о реальной организации."

_SECTION_TITLES = {
    Section.COMPANY: "Организация",
    # S8-01: the register by its name. «Сообщения о банкротстве — не найдено» read as «no
    # bankruptcy» to the customer, while a case was in court; EFRSB is one source of several.
    Section.BANKRUPTCY: "Сообщения ЕФРСБ",
    Section.FINANCES: "Финансы",
    Section.COURTS: "Арбитражные дела (ответчик, незавершённые)",
    Section.FEDRESURS: "Заявления о банкротстве (Федресурс)",
    Section.ENFORCEMENTS: "Исполнительные производства (ФССП, незавершённые)",
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
    FactKind.BANKRUPTCY_CASE: "Дело о банкротстве",
    FactKind.BANKRUPTCY_FILING: "Сообщение",
    FactKind.ENFORCEMENT: "Производство",
    FactKind.ENFORCEMENTS_OPEN: "Незавершённых производств",
    FactKind.ENFORCEMENT_DEBT_LEFT: "Остаток долга",
}
# Totals that are true on a day, not over a period: «на 15.10.2026», not «за».
_AS_OF = frozenset({FactKind.COURT_CLAIMS_TOTAL, FactKind.ENFORCEMENT_DEBT_LEFT})
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
# Record lists of the source: the full card shows the newest few of each and counts the
# rest. A large company (live, 17.10.2026) has a hundred cases and a hundred proceedings
# in its answer — 23 708 characters, which Telegram refused, so «Подробнее» failed. Every
# record stays in the report's «Основания» and with the source.
_LISTED = {
    FactKind.COURT_CASE: 5,
    FactKind.ENFORCEMENT: 5,
    FactKind.BANKRUPTCY_EVENT: 10,
    FactKind.BANKRUPTCY_INTENT: 10,
    FactKind.BANKRUPTCY_FILING: 10,
}


# The words of the first line, after the mark of the priority (``texts.PRIORITY_MARKS``).
_HEADLINES = {
    Priority.CRITICAL: "Критичный — требует внимания",
    Priority.HIGH: "Высокий — требует внимания",
    Priority.MEDIUM: "Средний — есть основания присмотреться",
    Priority.LOW: "Низкий — проверено полностью, значимых сигналов нет",
    Priority.UNKNOWN: "Недостаточно данных — проверка неполная",
}
# Every section has one (a test holds it): a missing title showed the code, «courts».
_PART_TITLES = {
    Section.COMPANY.value: "организация",
    Section.BANKRUPTCY.value: "сообщения ЕФРСБ",
    Section.FINANCES.value: "финансы",
    Section.COURTS.value: "арбитражные дела",
    Section.FEDRESURS.value: "Федресурс",
    Section.ENFORCEMENTS.value: "ФССП",
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

# S8-04: a mark beside each figure (customer, 15.10.2026: «выручка снизилась → красный
# восклицательный знак»). A figure on which a rule fired gets the mark of that rule's
# priority — ⚠️ while the finance rules are «Средний» — so the line never says more or less
# than the first one; otherwise only the direction.
TREND_MARKS = {Trend.UP: "🔺", Trend.DOWN: "🔻", Trend.FLAT: "➖"}
_TREND_TITLES = {FactKind.REVENUE: "Выручка", FactKind.NET_PROFIT: "Чистая прибыль"}
# A figure to read at a glance; the exact sums are in «Подробнее». Smallest first: a figure
# is printed at the first scale where, rounded, it stays under a thousand.
_SCALES = (
    (Decimal(1), "", 0),
    (Decimal(10**3), "тыс.", 0),
    (Decimal(10**6), "млн", 1),
    (Decimal(10**9), "млрд", 1),
)


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


def _brief_finances(check: CompanyCheck, assessment: Assessment) -> list[str]:
    """Revenue and net profit of the last year against the one before, one line each."""
    snapshot = next((s for s in check.snapshots if s.section is Section.FINANCES), None)
    if snapshot is None or snapshot.status is not FetchStatus.OK:
        return []
    fired = {signal.code: signal.level for signal in assessment.signals}
    return [_trend_line(trend, fired) for trend in finance_trends(snapshot)]


def _trend_line(trend: FinanceTrend, fired: Mapping[str, Priority]) -> str:
    if trend.alarm is not None:
        mark = PRIORITY_MARKS[fired.get(trend.alarm, Priority.MEDIUM)]
    else:
        mark = TREND_MARKS[trend.trend]
    before = trend.year - 1
    if trend.trend is Trend.FLAT:
        change = f"без изменений к {before}"
    elif trend.percent is None:
        # «−150 %» of a loss means nothing: the previous year is named instead.
        change = f"в {before} — {_short_figure(trend.previous)}"
    else:
        change = f"{_percent(trend)} к {before}"
    title = f"{_TREND_TITLES[trend.kind]} {trend.year}"
    return f"{mark} {title}: {_short_figure(trend.value)}, {change}"


def _percent(trend: FinanceTrend) -> str:
    """The rule's own figure when it fired (its reason reads the same); otherwise cut toward
    zero, so a fall just short of the threshold never reads as the threshold: −29,96 % is
    «−29,9 %» under 🔻, not «−30 %»."""
    value = trend.percent
    if trend.alarm is None:
        value = value.quantize(Decimal("0.1"), rounding=ROUND_DOWN)
    figure = f"{abs(value):.1f}".replace(".", ",").removesuffix(",0")
    return f"{'+' if value > 0 else '−'}{figure} %"


def _short_figure(value: Decimal) -> str:
    """«112 млн ₽», «1,5 млн ₽», «760 тыс. ₽», «убыток 300 тыс. ₽». The scale is chosen after
    rounding: 999 950 ₽ is «1 млн ₽», not «1 000 тыс. ₽» (review B on #97)."""
    amount = abs(value)
    for scale, word, places in _SCALES:
        figure = (amount / scale).quantize(Decimal(1).scaleb(-places))
        if figure < 1000:
            break
    text = f"{figure:,f}".replace(",", " ").replace(".", ",").removesuffix(",0")
    unit = f" {word}" if word else ""
    return f"{'убыток ' if value < 0 else ''}{text}{unit} ₽"


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


def _brief_enforcements(check: CompanyCheck) -> str | None:
    """S8-01: the bailiffs in one line, next to the claim load: a debt already decided.
    Like the courts, a figure here — no agreed threshold raises a priority on it yet."""
    snapshot = next((s for s in check.snapshots if s.section is Section.ENFORCEMENTS), None)
    if snapshot is None or snapshot.status is not FetchStatus.OK:
        return None
    count = next((f.value for f in snapshot.facts if f.kind is FactKind.ENFORCEMENTS_OPEN), None)
    if not isinstance(count, int):
        return None
    if count == 0:
        return "ФССП: незавершённых исполнительных производств нет."
    left = next((f.value for f in snapshot.facts if f.kind is FactKind.ENFORCEMENT_DEBT_LEFT), None)
    total = f", остаток долга {_money(left, 'RUB')}" if isinstance(left, Decimal) and left else ""
    return f"ФССП: незавершённых исполнительных производств — {count}{total}."


NO_BURDEN = "Арбитраж и ФССП: незавершённых дел и производств нет."


def _open_count(check: CompanyCheck, section: Section, kind: FactKind) -> int | None:
    snapshot = next((s for s in check.snapshots if s.section is section), None)
    if snapshot is None or snapshot.status is not FetchStatus.OK:
        return None
    count = next((f.value for f in snapshot.facts if f.kind is kind), None)
    return count if isinstance(count, int) else None


def _brief_burden(check: CompanyCheck) -> list[str]:
    """The courts and the bailiffs; when both were read and found nothing — one line."""
    cases = _open_count(check, Section.COURTS, FactKind.COURT_CASES_ACTIVE)
    proceedings = _open_count(check, Section.ENFORCEMENTS, FactKind.ENFORCEMENTS_OPEN)
    if cases == 0 and proceedings == 0:
        return [NO_BURDEN]
    return [line for line in (_brief_courts(check), _brief_enforcements(check)) if line]


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
    # The figures in one paragraph: revenue and profit (S8-04), then the claim load and
    # the bailiffs (S8-01). The brief card stays short, as the customer asked.
    figures = _brief_finances(check, assessment) + _brief_burden(check)
    if figures:
        lines += ["", *figures]
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
        cause = snapshot.error.message if snapshot.error is not None else None
        if cause is not None:
            lines.append(f"  Причина: {cause}")
        # S8-01: the live source puts the same text in both; said once, as the reason.
        for reason in snapshot.missing:
            if reason != cause:
                lines.append(f"  Ограничение: {reason}")
        lines.append(f"  Источник: {snapshot.source}, получено {_date(snapshot.fetched_at)}")
        return lines
    shown, hidden = _listed(snapshot.facts)
    for fact in shown:
        lines.append(f"  {_fact(fact)}")
    for kind, count in hidden.items():
        lines.append(f"  …и ещё {count}; здесь — {_LISTED[kind]} новейших")
    if not snapshot.facts and snapshot.section == Section.FEDRESURS:
        lines.append("  Сообщений о заявлении о банкротстве организации нет.")
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


def _listed(facts: tuple[Fact, ...]) -> tuple[list[Fact], dict[FactKind, int]]:
    """The facts to print, in their order, and how many records of each long list are not:
    of a listed kind only the newest few stay."""
    keep: set[int] = set()
    hidden: dict[FactKind, int] = {}
    for kind, limit in _LISTED.items():
        group = [fact for fact in facts if fact.kind is kind]
        newest = sorted(group, key=lambda fact: fact.observed_on or date.min, reverse=True)
        keep.update(id(fact) for fact in newest[:limit])
        if len(group) > limit:
            hidden[kind] = len(group) - limit
    return [f for f in facts if f.kind not in _LISTED or id(f) in keep], hidden


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """A text in parts Telegram takes (4096 characters each), cut at line ends; a line
    longer than a message is cut where it must. The last guard of the full card: whatever
    a source returns, «Подробнее» answers."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current or not parts:
        parts.append(current)
    return parts


def _fact(fact: Fact) -> str:
    label = _FACT_LABELS[fact.kind]
    if fact.value is None:
        return f"{label}: неизвестно ({fact.missing_reason})"
    when = _period(fact.period) if fact.period is not None else _date(fact.observed_on)
    if fact.kind == FactKind.COMPANY_STATUS:
        return f"{label}: {_STATUS_LABELS[fact.value]} (на {when})"
    if fact.kind in _AS_OF and isinstance(fact.value, Decimal):
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


# S8-05: the model's reading, a message of its own under the card. The card is the rules'
# and stays whatever the model says; the reading is marked as the model's. It names the
# company: it comes up to 90 s after the card, and the user may have checked another INN
# in between (updates are handled concurrently).
REVIEW_TITLE = "🤖 Разбор ИИ по открытым данным"
REVIEW_FOOTER = "Проверен по данным карточки; приоритет — по правилам."
REVIEW_NONE = "🤖 Разбора ИИ нет ({who}): {why}. Карточка — по правилам."
REVIEW_MONTH = "лимит расходов на ИИ за месяц исчерпан, модель не спрашивали"
REVIEW_REJECTED = "ответ модели не прошёл проверку по данным карточки"
REVIEW_TOO_MUCH = "данных слишком много для одного разбора, модель не спрашивали"
REVIEW_CARD_LIMIT = "исчерпан лимит расходов на одну карточку"
REVIEW_TIMEOUT = "модель не ответила вовремя"
REVIEW_FAILED = "модель недоступна"
REVIEW_NOT_PREPARED = "не удалось подготовить разбор"
REVIEW_NO_DATA = "источник открытых данных не ответил, модель не спрашивали"


def format_review(
    review: CardReview | None, check: CompanyCheck, internal: InternalContext | None = None
) -> str:
    """The reading under the card, or one line on why there is none."""
    name = _company_name(check, internal)
    who = f"{name}, ИНН {check.inn}" if name else f"ИНН {check.inn}"
    if review is not None and review.text is not None:
        return f"{REVIEW_TITLE} — {who}\n\n{review.text}\n\n{REVIEW_FOOTER}"
    return REVIEW_NONE.format(who=who, why=_no_review(review))


def _no_review(review: CardReview | None) -> str:
    """Why there is no reading, in the user's words: the guard's own messages are for the
    log («Запрос больше лимита (71684 > 24000).»), and not every failure is the model's."""
    if review is None:
        # The reviewer itself failed — the month's spend unread, say: nobody asked the model.
        return REVIEW_NOT_PREPARED
    if review.no_data:
        # Not one section answered: nothing to read, so the model was not paid to say so.
        return REVIEW_NO_DATA
    if review.month_exhausted:
        return REVIEW_MONTH
    outcome = review.outcome
    if outcome is not None and outcome.rejected is not None:
        return REVIEW_REJECTED
    error = outcome.error if outcome is not None else None
    if error is not None and error.code is AiErrorCode.BUDGET:
        # Our limits, not the model: the card's money is spent, or the request is too large.
        return REVIEW_CARD_LIMIT if error.message == BUDGET_MESSAGE else REVIEW_TOO_MUCH
    if error is not None and error.code is AiErrorCode.TIMEOUT:
        return REVIEW_TIMEOUT
    return REVIEW_FAILED
