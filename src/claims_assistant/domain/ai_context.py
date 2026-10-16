"""S5-05: what the model is given about one counterparty, and the versioned instruction.

Pure domain code: no network, files or clock. ``build_context`` turns the row, the rule
result (S3-05/S4-07), the internal indicators (S4-06), the external facts (S3-04) and the
«Взаимодействия» chronology (S4-02) into a ``RecommendationContext`` — the only thing a
provider (S5-01) may send.

Three rules shape it:

- **One counterparty.** Data of another INN is never included: a mismatched row, result
  or snapshot is an error, rows of other INNs are dropped. There is nothing in the context
  about the package, the user, the run or the files.
- **Facts apart from comments.** ``values`` and ``facts`` are checked data with their
  source; ``comments`` are what people wrote (the customer's employee or the debtor) and
  are marked as statements, not facts. Each comment keeps its record ID and date, so a
  payment promise the model reports can point at the record it came from.
- **The rules decide the priority.** The context carries the priority, its signals and the
  base recommendation as given; the instruction forbids changing them. Checking the answer
  is S5-06, limits and the budget are S5-03.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import (
    CompanyStatus,
    ExternalSnapshot,
    Fact,
    FactKind,
    Section,
)
from claims_assistant.domain.indicators import (
    DebtTrend,
    InternalIndicators,
    PaymentStatus,
)
from claims_assistant.domain.interactions import InteractionRow
from claims_assistant.domain.scoring import (
    INTERNAL_DEBT,
    INTERNAL_LAST_PAYMENT,
    INTERNAL_OVERDUE,
    Assessment,
    Trend,
    finance_trends,
)

# Both versions go to the report and the stored explanation (S5-02): a changed context or
# instruction must be visible in what was produced with it.
CONTEXT_VERSION = "3"
INSTRUCTION_VERSION = "2"

# The rules' levels as the report names them (lower case: they go inside a sentence).
PRIORITY_WORDS = {
    "critical": "критический",
    "high": "высокий",
    "medium": "средний",
    "low": "низкий",
    "unknown": "недостаточно данных",
}

MAX_COMMENTS = 20  # the latest ones; the rest are counted, not sent
MAX_COMMENT_CHARS = 500

_SECTION_TITLES = {
    Section.COMPANY: "Сведения об организации",
    Section.BANKRUPTCY: "Сообщения ЕФРСБ",
    Section.FINANCES: "Финансовая отчётность",
    Section.COURTS: "Арбитражные дела",
    Section.FEDRESURS: "Сообщения Федресурса",
    Section.ENFORCEMENTS: "Исполнительные производства (ФССП)",
}
_FACT_TITLES = {
    FactKind.COMPANY_NAME: "Наименование",
    FactKind.COMPANY_STATUS: "Статус организации",
    FactKind.REVENUE: "Выручка",
    FactKind.NET_PROFIT: "Чистая прибыль",
    FactKind.BANKRUPTCY_EVENT: "Сообщение ЕФРСБ",
    FactKind.BANKRUPTCY_CLOSED: "ЕФРСБ: дело прекращено",
    FactKind.COURT_CASE: "Арбитражное дело (организация — ответчик)",
    FactKind.COURT_CASES_ACTIVE: "Незавершённых арбитражных дел, где организация — ответчик",
    FactKind.COURT_CLAIMS_TOTAL: "Сумма исков к организации по этим делам",
    FactKind.BANKRUPTCY_INTENT: "Намерение кредитора подать заявление о банкротстве",
    FactKind.BANKRUPTCY_CASE: "Дело о банкротстве организации (арбитражный суд)",
    FactKind.BANKRUPTCY_FILING: "Заявление о банкротстве направлено в суд (Федресурс)",
    FactKind.ENFORCEMENT: "Исполнительное производство (ФССП)",
    FactKind.ENFORCEMENTS_OPEN: "Незавершённых исполнительных производств",
    FactKind.ENFORCEMENT_DEBT_LEFT: "Остаток долга по исполнительным производствам",
}
_STATUS_TITLES = {
    CompanyStatus.ACTIVE: "действует",
    CompanyStatus.LIQUIDATING: "в процессе ликвидации",
    CompanyStatus.LIQUIDATED: "ликвидирована",
}

_TREND_TITLES = {
    FactKind.REVENUE: "Изменение выручки",
    FactKind.NET_PROFIT: "Изменение чистой прибыли",
}
_TREND_WORDS = {Trend.UP: "рост", Trend.DOWN: "снижение", Trend.FLAT: "без изменений"}

INSTRUCTION = """\
Ты помогаешь специалисту по работе с задолженностью. Тебе дают данные об одной организации
и готовый результат правил: приоритет, сигналы и базовую рекомендацию.

Задача: в 2–4 предложениях объяснить специалисту, почему получился такой приоритет и что
разумно сделать дальше. Отдельно перечисли обещания оплаты, если они есть в комментариях.

Правила:
1. Приоритет задан правилами. Не меняй его, не предлагай другой и не спорь с ним.
2. Пиши только то, что есть во входных данных. Не добавляй суммы, даты, события и выводы,
   которых там нет. Не пользуйся сведениями об этой организации из других источников.
3. Ссылайся только на переданные идентификаторы: ID оснований для фактов и показателей,
   ID взаимодействия для комментария. ID оснований и коды сигналов пиши только в grounds,
   не в тексте explanation: специалист их не видит. Называй факт словами («выручка
   за 2025 год упала на 40 %»), а не его ID.
4. Комментарии — слова сотрудника или клиента, а не проверенные факты. Так о них и пиши:
   «клиент сообщил», «в комментарии указано».
5. Обещание оплаты отмечай, только если в комментарии есть и обещание, и дата; укажи ID
   взаимодействия. Не называй обещание нарушенным: это проверяется по платежам.
6. Не давай юридических указаний, не называй процессуальные сроки и не оценивай
   вероятность банкротства.
7. Если данных мало, так и скажи и назови, чего не хватает; не заполняй пробелы догадками.
8. Отвечай по-русски, деловым языком, без обращения к клиенту и без эмоций.

Формат ответа — один объект JSON без текста вокруг и без других полей:
{"explanation": "...", "promises": [{"interaction_id": "...", "due_on": "ГГГГ-ММ-ДД",
"quote": "..."}], "grounds": ["id", ...]}
- explanation: 2–4 предложения, не длиннее 1200 символов;
- promises: обещания оплаты; due_on — дата из самого комментария, quote — часть его текста;
  если обещаний нет, оставь пустой список;
- grounds: ID переданных оснований и взаимодействий, на которые опирается объяснение.
Ответ, не соответствующий формату или называющий данные не из контекста, отклоняется
целиком, и специалист видит базовую рекомендацию.
"""

# S8-05: the /inn card. The customer (15.10.2026): «по текущим данным из Checko нет
# скоринга от ИИ — оч важно по арбитражным делам и банкротству, и выручка год к году».
# The same answer format and the same review (S5-06); the task is the open data, not the
# list's priority, and there are no comments to read.
CARD_CONTEXT_VERSION = "card-1"
CARD_INSTRUCTION_VERSION = "card-1"
CARD_INSTRUCTION = """\
Ты помогаешь специалисту по работе с задолженностью. Тебе дают открытые данные об одной
организации (сведения о ней, банкротство, арбитражные дела, исполнительные производства,
финансовую отчётность) и сигналы, которые по ним нашли правила.

Задача: в 2–4 предложениях сказать специалисту, что в этих данных важно для работы с
задолженностью организации: банкротство, иски к ней, долги у приставов, изменение выручки
и прибыли год к году; чем это опасно для взыскания и что стоит проверить.

Правила:
1. Не называй уровень приоритета или риска словами («высокий», «средний», «низкий»,
   «критичный»): приоритет в карточке уже показан правилами.
2. Пиши только то, что есть во входных данных. Не добавляй суммы, даты, события и выводы,
   которых там нет. Не пользуйся сведениями об этой организации из других источников.
3. Ссылайся только на переданные ID оснований и пиши их только в grounds, не в тексте
   explanation. Называй факт словами («выручка за 2025 год выросла на 12 %»).
4. Сообщения ЕФРСБ приходят по ИНН и бывают о чужих делах, где организация — кредитор.
   Если роль организации не подтверждена, так и скажи; не называй её банкротом.
5. Не давай юридических указаний, не называй процессуальные сроки и не оценивай
   вероятность банкротства.
6. Если данных мало или раздел не проверен, так и скажи; не заполняй пробелы догадками.
7. Отвечай по-русски, деловым языком, без эмоций. Крупные суммы можно округлять:
   «3,4 млн ₽».

Формат ответа — один объект JSON без текста вокруг и без других полей:
{"explanation": "...", "promises": [], "grounds": ["id", ...]}
- explanation: 2–4 предложения, не длиннее 1200 символов;
- promises: всегда пустой список;
- grounds: ID переданных оснований, на которые опирается разбор.
Ответ, не соответствующий формату или называющий данные не из контекста, отклоняется
целиком, и специалист видит карточку по правилам.
"""
INSTRUCTIONS = {INSTRUCTION_VERSION: INSTRUCTION, CARD_INSTRUCTION_VERSION: CARD_INSTRUCTION}


def _text(value: object) -> str:
    if isinstance(value, CompanyStatus):
        return _STATUS_TITLES.get(value, value.value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


@dataclass(frozen=True, slots=True)
class ValueLine:
    """An internal value or indicator with the evidence ID the rules cite."""

    id: str
    title: str
    value: str
    observed_on: date | None = None


@dataclass(frozen=True, slots=True)
class FactLine:
    """One external fact with its section, source and record."""

    id: str
    section: str
    title: str
    value: str
    source: str
    record_id: str | None = None
    observed_on: date | None = None
    period: str | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class SignalLine:
    code: str
    level: str
    reason: str
    fact_ids: tuple[str, ...]
    value: str | None = None
    observed_on: date | None = None


@dataclass(frozen=True, slots=True)
class CommentLine:
    """A statement of a person, never a fact: kept apart and marked as such."""

    interaction_id: str
    happened_on: date
    text: str
    channel: str | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class RecommendationContext:
    inn: str  # never sent unless ``include_inn``; it is here to keep foreign data out
    analysis_date: date
    priority: str
    next_step: str
    base_complete: bool
    rules_version: str
    values: tuple[ValueLine, ...] = ()
    facts: tuple[FactLine, ...] = ()
    signals: tuple[SignalLine, ...] = ()
    comments: tuple[CommentLine, ...] = ()
    missing_data: tuple[str, ...] = ()
    comments_omitted: int = 0  # older comments left out by the limit
    reference: str | None = None  # opaque id of this counterparty in the request
    sends_inn: bool = False
    context_version: str = CONTEXT_VERSION
    instruction_version: str = INSTRUCTION_VERSION

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready request body: plain types, ISO dates, no objects of ours.

        The counterparty is named by ``inn`` only when the caller asked for it; otherwise
        by the opaque ``reference``, or by nothing at all when there is none.
        """
        who: dict[str, Any] = {}
        if self.sends_inn:
            who["inn"] = self.inn
        elif self.reference is not None:
            who["counterparty_ref"] = self.reference
        return {
            "context_version": self.context_version,
            "instruction_version": self.instruction_version,
            "rules_version": self.rules_version,
            **who,
            "analysis_date": self.analysis_date.isoformat(),
            # The word, not the code: a model repeats what it is given, and «high» in
            # quotes is not what a specialist reads (A on #72, live bot 30.09).
            "priority": PRIORITY_WORDS.get(self.priority, self.priority),
            "next_step": self.next_step,
            "base_complete": self.base_complete,
            "values": [_value_dict(value) for value in self.values],
            "facts": [_fact_dict(fact) for fact in self.facts],
            "signals": [_signal_dict(signal) for signal in self.signals],
            "comments": [_comment_dict(comment) for comment in self.comments],
            "missing_data": list(self.missing_data),
            "comments_omitted": self.comments_omitted,
        }


def _drop_empty(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _value_dict(value: ValueLine) -> dict[str, Any]:
    return _drop_empty(
        {
            "id": value.id,
            "title": value.title,
            "value": value.value,
            "observed_on": value.observed_on.isoformat() if value.observed_on else None,
        }
    )


def _fact_dict(fact: FactLine) -> dict[str, Any]:
    return _drop_empty(
        {
            "id": fact.id,
            "section": fact.section,
            "title": fact.title,
            "value": fact.value,
            "source": fact.source,
            "record_id": fact.record_id,
            "observed_on": fact.observed_on.isoformat() if fact.observed_on else None,
            "period": fact.period,
            "unit": fact.unit,
        }
    )


def _signal_dict(signal: SignalLine) -> dict[str, Any]:
    return _drop_empty(
        {
            "code": signal.code,
            "level": signal.level,
            "reason": signal.reason,
            "fact_ids": list(signal.fact_ids),
            "value": signal.value,
            "observed_on": signal.observed_on.isoformat() if signal.observed_on else None,
        }
    )


def _comment_dict(comment: CommentLine) -> dict[str, Any]:
    return _drop_empty(
        {
            "interaction_id": comment.interaction_id,
            "happened_on": comment.happened_on.isoformat(),
            "text": comment.text,
            "channel": comment.channel,
            "truncated": comment.truncated or None,
        }
    )


@dataclass(frozen=True, slots=True)
class ContextLimits:
    max_comments: int = MAX_COMMENTS
    max_comment_chars: int = MAX_COMMENT_CHARS
    # Both default to off: what leaves the service is the caller's explicit decision
    # (S5-01/S5-03), not a forgotten default.
    #
    # ``include_comments``: the customer's own texts need their consent; without it the
    # explanation rests on facts and indicators, and the count still shows they exist.
    # ``include_inn``: the model does not need it to explain anything, while for an
    # outside provider it identifies the debtor. Off, the request carries the opaque
    # ``reference`` the caller passes instead, and the mapping stays here (S5-02).
    include_comments: bool = False
    include_inn: bool = False
    # Record ids of the sources (ОГРН of the company, GUID of an EFRSB message) are the
    # link to the evidence; ОГРН identifies the company as surely as the INN, so a
    # customer who wants nothing identifying sent can drop them.
    include_record_ids: bool = True

    def __post_init__(self) -> None:
        if self.max_comments < 0 or self.max_comment_chars < 1:
            raise ValueError("Context limits must be positive")


def _row_values(row: CounterpartyRow, indicators: InternalIndicators | None) -> list[ValueLine]:
    values = []
    if row.debt is not None:
        values.append(ValueLine(INTERNAL_DEBT, "Сумма долга", _text(row.debt), row.cutoff_date))
    if row.overdue_days is not None:
        values.append(
            ValueLine(INTERNAL_OVERDUE, "Дней просрочки", str(row.overdue_days), row.cutoff_date)
        )
    if indicators is None:
        if row.last_payment_date is not None:
            values.append(
                ValueLine(
                    INTERNAL_LAST_PAYMENT,
                    "Последний платёж",
                    row.last_payment_date.isoformat(),
                    row.last_payment_date,
                )
            )
        return values
    return values + _indicator_values(indicators)


def _indicator_values(indicators: InternalIndicators) -> list[ValueLine]:
    """Indicators as the report states them: an unconfirmed value says so in its text."""
    values = []
    payment = indicators.payment
    if payment.status is PaymentStatus.CONFIRMED and payment.last_payment is not None:
        values.append(
            ValueLine(
                payment.source or INTERNAL_LAST_PAYMENT,
                "Последний подтверждённый платёж",
                f"{payment.last_payment.isoformat()} ({payment.age_days} дн. назад)",
                payment.last_payment,
            )
        )
    elif payment.status is PaymentStatus.NONE_SINCE and payment.no_payments_since is not None:
        values.append(
            ValueLine(
                payment.source or INTERNAL_LAST_PAYMENT,
                "Поступления в полной выгрузке",
                f"нет с {payment.no_payments_since.isoformat()} ({payment.age_days} дн.)",
                payment.no_payments_since,
            )
        )
    elif payment.status is PaymentStatus.IN_PERIOD and payment.period_last is not None:
        values.append(
            ValueLine(
                payment.source or INTERNAL_LAST_PAYMENT,
                "Последний платёж в предоставленном периоде",
                f"{payment.period_last.isoformat()} (давность не подтверждена)",
                payment.period_last,
            )
        )
    debt = indicators.debt
    if debt.trend is DebtTrend.COMPUTED and debt.ratio is not None and debt.previous_on:
        values.append(
            ValueLine(
                "internal-debt-history",
                "Изменение долга за месяц",
                f"в {debt.ratio:.2f} раза с {debt.previous_on.isoformat()}",
                debt.current_on,
            )
        )
    elif debt.trend is DebtTrend.APPEARED and debt.previous_on is not None:
        values.append(
            ValueLine(
                "internal-debt-history",
                "Изменение долга за месяц",
                f"долг появился: на {debt.previous_on.isoformat()} долга не было",
                debt.current_on,
            )
        )
    revenue = indicators.revenue
    if revenue is not None:
        values.append(
            ValueLine(
                revenue.fact_ids[1],
                "Изменение выручки",
                f"{revenue.percent:.1f}% за {revenue.year - 1}–{revenue.year} годы",
            )
        )
    return values


def _mask(text: str | None, inn: str, reference: str | None) -> str | None:
    """Identifiers are built from the INN by some providers («<ИНН>:company:status»).

    With the INN kept back, it must not travel inside an id either: every occurrence is
    replaced by the same reference the request uses, so the caller can map an id in the
    answer back by replacing it again.
    """
    if text is None or inn not in text:
        return text
    return text.replace(inn, reference or "ref")


# A defendant with a hundred cases and a hundred proceedings made a request of 71 684
# characters against the limit of 24 000 (review A on #96): the model was never asked for
# exactly the companies the customer wanted it for. The totals of a section stay whole;
# of its records the newest go, and the rest are counted. Every record of a bankruptcy
# goes — there are few, and they are what matters most.
FACT_CAPS = {
    FactKind.COURT_CASE: 8,
    FactKind.ENFORCEMENT: 8,
    FactKind.BANKRUPTCY_EVENT: 10,
    FactKind.BANKRUPTCY_CLOSED: 3,
}


def _newest_first(fact: Fact) -> tuple[bool, date]:
    return (fact.observed_on is not None, fact.observed_on or date.min)


def _kept(snapshot: ExternalSnapshot, cited: set[str]) -> tuple[list[Fact], list[str]]:
    """The facts of a section that go to the model, in their order, and a note for each
    kind cut short. A fact a signal cites always goes: the answer may name it."""
    dropped: set[str] = set()
    notes = []
    for kind, cap in FACT_CAPS.items():
        facts = [fact for fact in snapshot.facts if fact.kind is kind and fact.value is not None]
        newest = {fact.id for fact in sorted(facts, key=_newest_first, reverse=True)[:cap]}
        cut = {fact.id for fact in facts} - newest - cited
        if cut:
            dropped |= cut
            section = _SECTION_TITLES.get(snapshot.section, snapshot.section.value)
            title = _FACT_TITLES.get(kind, kind.value)
            notes.append(
                f"{section}: «{title}» — в запросе {len(facts) - len(cut)} новейших из "
                f"{len(facts)}; итоги раздела — по всем."
            )
    return [fact for fact in snapshot.facts if fact.id not in dropped], notes


def _facts(
    snapshots: Iterable[ExternalSnapshot], cited: set[str] = frozenset()
) -> tuple[list[FactLine], list[str]]:
    lines, notes = [], []
    for snapshot in snapshots:
        evidence = {item.id: item for item in snapshot.evidence}
        facts, cut = _kept(snapshot, set(cited))
        notes += cut
        for fact in facts:
            if fact.value is None:  # an unknown value is a gap, it belongs to missing_data
                continue
            source = next(
                (evidence[e] for e in fact.evidence_ids if e in evidence),
                None,
            )
            period = None
            if fact.period is not None:
                period = f"{fact.period.start.isoformat()}–{fact.period.end.isoformat()}"
            lines.append(
                FactLine(
                    id=fact.id,
                    section=_SECTION_TITLES.get(snapshot.section, snapshot.section.value),
                    title=_FACT_TITLES.get(fact.kind, fact.kind.value),
                    value=_text(fact.value),
                    source=source.source if source else snapshot.source,
                    record_id=source.record_id if source else None,
                    observed_on=fact.observed_on,
                    period=period,
                    unit=fact.unit,
                )
            )
    return lines, notes


def _comments(
    interactions: Sequence[InteractionRow], limits: ContextLimits
) -> tuple[list[CommentLine], int]:
    """The latest interactions, oldest first; older ones are counted, not sent."""
    if not limits.include_comments:
        return [], len(interactions)
    ordered = sorted(interactions, key=lambda row: row.happened_on)
    omitted = max(0, len(ordered) - limits.max_comments)
    lines = []
    for row in ordered[omitted:]:
        text = row.comment.strip()
        truncated = len(text) > limits.max_comment_chars
        lines.append(
            CommentLine(
                interaction_id=row.interaction_id,
                happened_on=row.happened_on,
                text=text[: limits.max_comment_chars] if truncated else text,
                channel=row.channel,
                truncated=truncated,
            )
        )
    return lines, omitted


def build_context(
    row: CounterpartyRow,
    assessment: Assessment,
    analysis_date: date,
    indicators: InternalIndicators | None = None,
    interactions: Sequence[InteractionRow] = (),
    snapshots: Iterable[ExternalSnapshot] = (),
    limits: ContextLimits = ContextLimits(),
    reference: str | None = None,
) -> RecommendationContext:
    """Everything the model may see about this counterparty, and nothing else.

    ``reference`` names the counterparty in the request while the INN stays here: the
    caller keeps the mapping (S5-02) and may pass a row number or any opaque id — it must
    not contain the INN itself.

    Raises ``ValueError`` if the result, the indicators or a snapshot belong to another
    INN, or if the reference would leak it; interactions of other INNs are dropped, as
    they come from the shared file.
    """
    inn = row.inn
    if indicators is not None and indicators.inn != inn:
        raise ValueError("Indicators belong to another INN")
    comments, omitted = _comments([r for r in interactions if r.inn == inn], limits)
    return _assemble(
        inn,
        assessment,
        analysis_date,
        _row_values(row, indicators),
        snapshots,
        limits,
        reference,
        comments=tuple(comments),
        comments_omitted=omitted,
    )


def _trend_values(snapshots: Sequence[ExternalSnapshot]) -> list[ValueLine]:
    """S8-05: revenue and profit year over year, as the card's marks read them (S8-04).

    The model may only retell numbers it was given: without these lines «выручка выросла
    на 12 %» would be a new number and the whole answer would go.
    """
    finances = next((s for s in snapshots if s.section is Section.FINANCES), None)
    values = []
    for trend in finance_trends(finances):
        title = _TREND_TITLES[trend.kind]
        span = f"{trend.year - 1}–{trend.year} годы"
        if trend.percent is not None:
            change = f"{trend.percent:+.1f}% за {span}"
        else:
            # No percent of a base that is not positive: a loss, or nothing at all.
            base = "убыток" if trend.previous < 0 else "ноль"
            change = f"{_TREND_WORDS[trend.trend]} за {span}; в {trend.year - 1} году {base}"
        values.append(
            ValueLine(
                f"finance-trend-{trend.kind.value}",
                title,
                f"{change} (было {_text(trend.previous)}, стало {_text(trend.value)})",
            )
        )
    return values


def build_card_context(
    inn: str,
    assessment: Assessment,
    analysis_date: date,
    snapshots: Iterable[ExternalSnapshot],
    row: CounterpartyRow | None = None,
    indicators: InternalIndicators | None = None,
    limits: ContextLimits = ContextLimits(),
    reference: str | None = None,
) -> RecommendationContext:
    """S8-05: the /inn card — the open data of one company, and the owner's row if a
    finished check of the owner has it. Comments are never sent here."""
    if row is not None and row.inn != inn:
        raise ValueError("Row belongs to another INN")
    if indicators is not None and indicators.inn != inn:
        raise ValueError("Indicators belong to another INN")
    snapshots = tuple(snapshots)
    values = _row_values(row, indicators) if row is not None else []
    return _assemble(
        inn,
        assessment,
        analysis_date,
        values + _trend_values(snapshots),
        snapshots,
        limits,
        reference,
        context_version=CARD_CONTEXT_VERSION,
        instruction_version=CARD_INSTRUCTION_VERSION,
    )


def _assemble(
    inn: str,
    assessment: Assessment,
    analysis_date: date,
    values: Sequence[ValueLine],
    snapshots: Iterable[ExternalSnapshot],
    limits: ContextLimits,
    reference: str | None,
    *,
    comments: tuple[CommentLine, ...] = (),
    comments_omitted: int = 0,
    context_version: str = CONTEXT_VERSION,
    instruction_version: str = INSTRUCTION_VERSION,
) -> RecommendationContext:
    if assessment.inn != inn:
        raise ValueError("Assessment belongs to another INN")
    snapshots = tuple(snapshots)
    if any(snapshot.inn != inn for snapshot in snapshots):
        raise ValueError("Snapshot belongs to another INN")
    if reference is not None and inn in reference:
        raise ValueError("The reference must not contain the INN")
    values = tuple(values)
    cited = {fact_id for signal in assessment.signals for fact_id in signal.fact_ids}
    fact_lines, cut = _facts(snapshots, cited)
    facts = tuple(fact_lines)
    signals = tuple(
        SignalLine(
            code=signal.code,
            level=signal.level.value,
            reason=signal.reason,
            fact_ids=signal.fact_ids,
            value=signal.value,
            observed_on=signal.observed_on,
        )
        for signal in assessment.signals
    )
    if not limits.include_record_ids:
        facts = tuple(replace(fact, record_id=None) for fact in facts)
    if not limits.include_inn:
        # The INN is kept back, so it must not ride along inside an identifier either.
        values = tuple(replace(value, id=_mask(value.id, inn, reference)) for value in values)
        facts = tuple(
            replace(
                fact,
                id=_mask(fact.id, inn, reference),
                record_id=_mask(fact.record_id, inn, reference),
            )
            for fact in facts
        )
        signals = tuple(
            replace(
                signal,
                fact_ids=tuple(_mask(fact_id, inn, reference) for fact_id in signal.fact_ids),
            )
            for signal in signals
        )
    return RecommendationContext(
        inn=inn,
        analysis_date=analysis_date,
        priority=assessment.priority.value,
        next_step=assessment.next_step,
        base_complete=assessment.base_complete,
        rules_version=assessment.rules_version,
        values=values,
        facts=facts,
        signals=signals,
        comments=comments,
        missing_data=assessment.missing_data + tuple(cut),
        comments_omitted=comments_omitted,
        reference=reference,
        sends_inn=limits.include_inn,
        context_version=context_version,
        instruction_version=instruction_version,
    )


@dataclass(frozen=True, slots=True)
class Request:
    """What a provider (S5-01) sends: the instruction and one counterparty's context."""

    instruction: str = field(default=INSTRUCTION)
    context: dict[str, Any] = field(default_factory=dict)
    instruction_version: str = INSTRUCTION_VERSION
    context_version: str = CONTEXT_VERSION


def build_request(context: RecommendationContext) -> Request:
    return Request(
        instruction=INSTRUCTIONS[context.instruction_version],
        context=context.as_dict(),
        instruction_version=context.instruction_version,
        context_version=context.context_version,
    )


def versions() -> str:
    """For «О проверке» and the stored explanation (S5-02)."""
    return f"контекст {CONTEXT_VERSION}, инструкция {INSTRUCTION_VERSION}"
