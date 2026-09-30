"""S5-06: checking the model's answer against the context it was given.

Pure domain code: no network, files or clock. ``review_answer(payload, context)`` returns
either an ``Explanation`` that may be shown, or a ``Rejected`` with a stable code — it
never raises and never lets a doubtful answer through. The reasons it can reject:

- the answer is not the agreed JSON, or its fields have other types (S5-05 contract);
- it cites a ground or an interaction that is not in the context;
- it names an amount or a date the context does not contain — the model must not add
  facts of its own;
- it puts an internal ID (a ground's ID or a signal's code) into a sentence the
  specialist reads: those belong in ``grounds``, and «revenue_drop_30» is not a word. A
  list of known IDs in brackets or at the end («(основания: internal-overdue)») is only a
  citation: it is cut from the text, not a reason to lose a correct answer;
- it argues with the priority: the rules own it, the model only explains it;
- a payment promise has no date, or its date is not in the comment it points at.

A rejected answer is not an error of the run: S5-04 shows the rule-based recommendation
instead. The codes are stable, so the reason can be counted and shown to the user.
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from claims_assistant.domain.ai_context import RecommendationContext

ANSWER_SCHEMA_VERSION = "1"
MAX_EXPLANATION_CHARS = 1200
MAX_PROMISES = 10

_ANSWER_FIELDS = {"explanation", "promises", "grounds"}
_PROMISE_FIELDS = {"interaction_id", "due_on", "quote"}

# «1 234,56», «1234.5», «75», «-40.0» — a number with an optional decimal part.
_NUMBER = re.compile(r"-?\d[\d  ]*(?:[.,]\d+)?")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_RU_DATE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip
# «25 сентября 2026 года», «25 сентября»: a date in words is a date, not the number 25.
_WORD_DATE = re.compile(
    rf"\b(\d{{1,2}})\s+({'|'.join(_MONTHS)})(?:\s+(\d{{4}})(?:\s*(?:года|г\.))?)?",
    re.IGNORECASE,
)
# «более 90 дней» when the data say 91: a rounded bound, true of the fact it retells.
_BOUND_MORE = re.compile(r"(?:более|больше|свыше|превыша\w*)(?:\s+чем)?\s*$", re.IGNORECASE)
_BOUND_AT_LEAST = re.compile(r"не\s+менее\s*$", re.IGNORECASE)
_BOUND_LESS = re.compile(r"(?:менее|меньше)(?:\s+чем)?\s*$", re.IGNORECASE)
_BOUND_SLACK = Decimal("1.25")  # «более 90» may stand for up to 112, not for 200

# The priority is the rules' answer; naming another one or asking to change it is a reason
# to reject, whatever the wording around it.
_PRIORITY_WORDS = {"critical": "критичн", "high": "высок", "medium": "средн", "low": "низк"}
_PRIORITY_CLAIMS = (
    "приоритет должен",
    "снизить приоритет",
    "повысить приоритет",
    "изменить приоритет",
)


class RejectionCode(StrEnum):
    NOT_JSON = "not_json"
    SCHEMA = "schema"
    EMPTY_EXPLANATION = "empty_explanation"
    TOO_LONG = "too_long"
    UNKNOWN_GROUND = "unknown_ground"
    UNKNOWN_INTERACTION = "unknown_interaction"
    NEW_AMOUNT = "new_amount"
    NEW_DATE = "new_date"
    INTERNAL_ID = "internal_id"
    PRIORITY_CHANGED = "priority_changed"
    PROMISE_WITHOUT_DATE = "promise_without_date"
    PROMISE_NOT_IN_COMMENT = "promise_not_in_comment"


_REASONS = {
    RejectionCode.NOT_JSON: "Ответ модели не разобран как JSON.",
    RejectionCode.SCHEMA: "Ответ модели не соответствует согласованной схеме.",
    RejectionCode.EMPTY_EXPLANATION: "Объяснение пустое.",
    RejectionCode.TOO_LONG: "Объяснение длиннее допустимого.",
    RejectionCode.UNKNOWN_GROUND: "Ответ ссылается на основание, которого нет в контексте.",
    RejectionCode.UNKNOWN_INTERACTION: "Ответ ссылается на взаимодействие, которого нет "
    "в контексте.",
    RejectionCode.NEW_AMOUNT: "В объяснении есть число, которого нет во входных данных.",
    RejectionCode.NEW_DATE: "В объяснении есть дата, которой нет во входных данных.",
    RejectionCode.INTERNAL_ID: "В объяснении есть служебный идентификатор основания или сигнала.",
    RejectionCode.PRIORITY_CHANGED: "Ответ оспаривает или меняет приоритет, заданный правилами.",
    RejectionCode.PROMISE_WITHOUT_DATE: "Обещание оплаты без даты.",
    RejectionCode.PROMISE_NOT_IN_COMMENT: "Даты обещания нет в комментарии, на который "
    "оно ссылается.",
}


@dataclass(frozen=True, slots=True)
class Promise:
    """A payment promise the model found, tied to the interaction it came from."""

    interaction_id: str
    due_on: date
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class Explanation:
    text: str
    promises: tuple[Promise, ...] = ()
    grounds: tuple[str, ...] = ()
    schema_version: str = ANSWER_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class Rejected:
    code: RejectionCode
    reason: str
    detail: str | None = None  # a safe hint, never a piece of the answer's text

    @property
    def message(self) -> str:
        return f"{self.reason} ({self.code})"


def _reject(code: RejectionCode, detail: str | None = None) -> Rejected:
    return Rejected(code=code, reason=_REASONS[code], detail=detail)


def _dates(text: str) -> set[date]:
    found = set()
    for match in _ISO_DATE.finditer(text):
        year, month, day = (int(part) for part in match.groups())
        try:
            found.add(date(year, month, day))
        except ValueError:
            continue
    for match in _RU_DATE.finditer(text):
        day, month, year = (int(part) for part in match.groups())
        try:
            found.add(date(year, month, day))
        except ValueError:
            continue
    return found


def _word_dates(text: str) -> list[tuple[int, int, int | None]]:
    """Day, month and year (None when not written) of every date in words."""
    found = []
    for match in _WORD_DATE.finditer(text):
        day = int(match.group(1))
        month = _MONTHS.index(match.group(2).lower()) + 1
        found.append((day, month, int(match.group(3)) if match.group(3) else None))
    return found


def _strip_dates(text: str) -> str:
    return _WORD_DATE.sub(" ", _RU_DATE.sub(" ", _ISO_DATE.sub(" ", text)))


def _decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(" ", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def _numbers(text: str) -> set[Decimal]:
    found = set()
    for match in _NUMBER.finditer(_strip_dates(text)):
        value = _decimal(match.group())
        if value is not None:
            found.add(value)
    return found


def _claims(text: str) -> list[tuple[Decimal, str | None]]:
    """Every number of the text with the bound written before it: «more», «at_least»,
    «less» or None for a plain number."""
    stripped = _strip_dates(text)
    found = []
    for match in _NUMBER.finditer(stripped):
        value = _decimal(match.group())
        if value is None:
            continue
        before = stripped[max(0, match.start() - 20) : match.start()]
        bound = None
        if _BOUND_AT_LEAST.search(before):
            bound = "at_least"
        elif _BOUND_MORE.search(before):
            bound = "more"
        elif _BOUND_LESS.search(before):
            bound = "less"
        found.append((value, bound))
    return found


def _bound_holds(known: Decimal, value: Decimal, bound: str | None) -> bool:
    """A bound is a retelling when a known number satisfies it and is close to it."""
    if bound is None or value <= 0 or known <= 0:
        return False
    if bound == "more":
        return value < known <= value * _BOUND_SLACK
    if bound == "at_least":
        return value <= known <= value * _BOUND_SLACK
    return value / _BOUND_SLACK <= known < value


def _citations(ids: set[str]) -> re.Pattern[str] | None:
    """A list of known IDs cited in brackets or as the closing «Основания: …»."""
    if not ids:
        return None
    one = "|".join(re.escape(item) for item in sorted(ids, key=len, reverse=True))
    many = rf"(?:{one})(?:\s*[,;]\s*(?:{one}))*"
    label = r"(?:(?:основани[еяй]|id)\s*:?\s*)?"
    return re.compile(
        rf"\s*\({label}{many}\)|[\s.;,]*\b(?:основани[еяй]|id)\s*:\s*{many}\s*\.?\s*$",
        re.IGNORECASE,
    )


def _id_pattern(ids: set[str]) -> re.Pattern[str] | None:
    """The IDs as whole tokens: «INT-8» in «INT-8,» but not in «INT-80»."""
    if not ids:
        return None
    alternatives = "|".join(re.escape(item) for item in sorted(ids, key=len, reverse=True))
    return re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])")


def _strip_ids(text: str, pattern: re.Pattern[str] | None) -> str:
    """IDs are names, not numbers: «efrsb-event-0» must not read as -0, nor
    «revenue-2025 2025» as -20252025 (review A on #63)."""
    return pattern.sub(" ", text) if pattern is not None else text


def _rounds_to(known: Decimal, value: Decimal) -> bool:
    """A rounded retelling is fine: 40 for -40.0, 2,5 for 2.50 — a new number is not."""
    places = -value.as_tuple().exponent if value.as_tuple().exponent < 0 else 0
    try:
        return round(known, places) == value or round(abs(known), places) == abs(value)
    except (InvalidOperation, ValueError):
        return False


def _context_text(context: RecommendationContext) -> str:
    """Everything the model was given, as one text: what may be quoted back."""
    parts = [
        context.next_step,
        context.analysis_date.isoformat(),
        *(f"{value.title} {value.value} {value.observed_on or ''}" for value in context.values),
        *(
            f"{fact.title} {fact.value} {fact.observed_on or ''} "
            f"{fact.period or ''} {fact.unit or ''}"
            for fact in context.facts
        ),
        *(
            f"{signal.reason} {signal.value or ''} {signal.observed_on or ''}"
            for signal in context.signals
        ),
        *(
            f"{comment.interaction_id} {comment.happened_on.isoformat()} {comment.text}"
            for comment in context.comments
        ),
        *(fact.record_id or "" for fact in context.facts),
        *context.missing_data,
    ]
    return " ".join(str(part) for part in parts)


def _field(data: dict[str, Any], key: str, kind: type, required: bool = True) -> Any:
    if key not in data:
        if required:
            raise _SchemaError(key)
        return None
    value = data[key]
    if type(value) is not kind:
        raise _SchemaError(key)
    return value


class _SchemaError(Exception):
    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def _parse(payload: object) -> dict[str, Any] | Rejected:
    data = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except ValueError:
            return _reject(RejectionCode.NOT_JSON)
    if not isinstance(data, dict):
        return _reject(RejectionCode.SCHEMA, "не объект")
    if set(data) - _ANSWER_FIELDS:
        return _reject(RejectionCode.SCHEMA, "лишние поля")
    return data


def _promises(raw: list[Any], context: RecommendationContext) -> tuple[Promise, ...] | Rejected:
    if len(raw) > MAX_PROMISES:
        return _reject(RejectionCode.SCHEMA, "слишком много обещаний")
    comments = {comment.interaction_id: comment for comment in context.comments}
    promises = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - _PROMISE_FIELDS:
            return _reject(RejectionCode.SCHEMA, "обещание не по схеме")
        try:
            interaction_id = _field(item, "interaction_id", str)
            due_on = _field(item, "due_on", str, required=False)
            quote = _field(item, "quote", str, required=False)
        except _SchemaError as error:
            return _reject(RejectionCode.SCHEMA, f"поле обещания: {error.field}")
        comment = comments.get(interaction_id)
        if comment is None:
            return _reject(RejectionCode.UNKNOWN_INTERACTION, interaction_id)
        if not due_on:
            return _reject(RejectionCode.PROMISE_WITHOUT_DATE, interaction_id)
        try:
            day = date.fromisoformat(due_on)
        except ValueError:
            return _reject(RejectionCode.PROMISE_WITHOUT_DATE, interaction_id)
        # The date must be in the comment the promise points at, not invented next to it.
        if day not in _dates(comment.text):
            return _reject(RejectionCode.PROMISE_NOT_IN_COMMENT, interaction_id)
        if quote is not None and quote not in comment.text:
            return _reject(RejectionCode.PROMISE_NOT_IN_COMMENT, interaction_id)
        promises.append(Promise(interaction_id=interaction_id, due_on=day, quote=quote))
    return tuple(promises)


def _priority_conflict(text: str, priority: str) -> bool:
    """Naming a level other than the given one, or asking to change it, is a conflict.

    «Недостаточно данных» is not a level here: the instruction asks to say exactly that
    when something is missing, and the priority ``unknown`` has no word of its own.
    """
    lowered = text.lower()
    if any(claim in lowered for claim in _PRIORITY_CLAIMS):
        return True
    return any(word in lowered for name, word in _PRIORITY_WORDS.items() if name != priority)


def review_answer(payload: object, context: RecommendationContext) -> Explanation | Rejected:
    """Check one answer against its context; never raises, never guesses what was meant."""
    data = _parse(payload)
    if isinstance(data, Rejected):
        return data
    try:
        explanation = _field(data, "explanation", str)
        grounds = _field(data, "grounds", list, required=False) or []
        raw_promises = _field(data, "promises", list, required=False) or []
    except _SchemaError as error:
        return _reject(RejectionCode.SCHEMA, f"поле: {error.field}")

    known_ids = {value.id for value in context.values} | {fact.id for fact in context.facts}
    known_ids |= {comment.interaction_id for comment in context.comments}

    # A citation is already checked in «grounds»; the specialist does not read IDs, so
    # the list is cut from what is shown instead of costing the whole answer (A on #72).
    citations = _citations(known_ids | {signal.code for signal in context.signals})
    text = explanation.strip()
    if citations is not None:
        text = re.sub(r"\s+([.,;:])", r"\1", citations.sub("", text)).strip()
    if not text:
        return _reject(RejectionCode.EMPTY_EXPLANATION)
    if len(text) > MAX_EXPLANATION_CHARS:
        return _reject(RejectionCode.TOO_LONG, f"{len(text)} символов")

    if any(not isinstance(ground, str) for ground in grounds):
        return _reject(RejectionCode.SCHEMA, "основание не строка")
    unknown = [ground for ground in grounds if ground not in known_ids]
    if unknown:
        return _reject(RejectionCode.UNKNOWN_GROUND, unknown[0])

    if _priority_conflict(text, context.priority):
        return _reject(RejectionCode.PRIORITY_CHANGED)

    # Grounds and signals are named by us, for us; an interaction's ID is the customer's
    # own record number («запись INT-1» in the report) and may be cited.
    internal = {value.id for value in context.values} | {fact.id for fact in context.facts}
    internal |= {signal.code for signal in context.signals}
    internal -= {comment.interaction_id for comment in context.comments}
    found = _id_pattern(internal)
    if found is not None and (match := found.search(text)):
        return _reject(RejectionCode.INTERNAL_ID, match.group())

    ids = _id_pattern(internal | {comment.interaction_id for comment in context.comments})
    source = _strip_ids(_context_text(context), ids)
    text_only = _strip_ids(text, ids)
    known_dates = _dates(source)
    for day in _dates(text_only) - known_dates:
        return _reject(RejectionCode.NEW_DATE, day.isoformat())
    for day, month, year in _word_dates(text_only):
        if not any(
            (known.day, known.month) == (day, month) and year in (None, known.year)
            for known in known_dates
        ):
            written = f"{day:02d}.{month:02d}" + (f".{year}" if year else "")
            return _reject(RejectionCode.NEW_DATE, written)
    known_numbers = _numbers(source)
    for number, bound in _claims(text_only):
        # A bound is checked as a bound: «более 75 дней» is not true of 75 days.
        if not any(
            _bound_holds(known, number, bound) if bound else _rounds_to(known, number)
            for known in known_numbers
        ):
            return _reject(RejectionCode.NEW_AMOUNT, str(number))

    promises = _promises(raw_promises, context)
    if isinstance(promises, Rejected):
        return promises
    return Explanation(text=text, promises=promises, grounds=tuple(grounds))
