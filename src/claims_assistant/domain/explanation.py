"""The explanation a report row carries (S5-04): the model's or the template's.

When the model is off, unavailable, over budget, or its answer was rejected by the
review (S5-06), the report still explains the row — from the rules alone, with the same
shape a reader expects: the priority, why it fired, what to do next, what is missing.
The template never invents anything: it only repeats the assessment. A note names why
the model's text is absent, so the reader knows the difference.
"""

from dataclasses import dataclass

from claims_assistant.domain.ai_review import Explanation
from claims_assistant.domain.scoring import Assessment, Priority

PRIORITY_WORDS = {
    Priority.CRITICAL: "критичный",
    Priority.HIGH: "высокий",
    Priority.MEDIUM: "средний",
    Priority.LOW: "низкий",
    Priority.UNKNOWN: "недостаточно данных",
}
_STATUS_NOTES = {
    "unavailable:budget": "лимит ИИ на проверку исчерпан",
    "unavailable:timeout": "модель не ответила в срок",
    "unavailable:unavailable": "модель недоступна",
    "unavailable:rate_limited": "модель ограничила число запросов",
    "unavailable:refused": "провайдер отклонил запрос",
}


@dataclass(frozen=True, slots=True)
class RowExplanation:
    text: str
    source: str  # "ai" | "template"
    note: str | None = None  # why the template stands in, when it does
    promises: tuple[str, ...] = ()  # "до ДД.ММ.ГГГГ (запись INT-1)"

    @property
    def from_model(self) -> bool:
        return self.source == "ai"


def template_explanation(assessment: Assessment) -> str:
    """Two to four sentences from the rules alone; nothing that is not in the assessment."""
    word = PRIORITY_WORDS[assessment.priority]
    if assessment.priority is Priority.UNKNOWN:
        opening = "Приоритет не определён: данных для оценки недостаточно."
    else:
        opening = f"Приоритет по правилам — {word}."
    parts = [opening]
    reasons = [signal.reason.rstrip(".") for signal in assessment.signals]
    if reasons:
        parts.append("Сработало: " + "; ".join(reasons) + ".")
    elif assessment.priority is not Priority.UNKNOWN:
        parts.append("Сигналов правил нет.")
    parts.append(assessment.next_step)
    if assessment.missing_data:
        parts.append(
            "Не хватает: " + "; ".join(item.rstrip(".") for item in assessment.missing_data) + "."
        )
    return " ".join(parts)


def explain_row(
    assessment: Assessment,
    explanation: Explanation | None,
    status: str | None,
) -> RowExplanation:
    """The model's accepted text when there is one, the template otherwise.

    ``status`` is the stored outcome (``accepted``, ``rejected:<code>``,
    ``unavailable:<code>``) or None when no provider was configured.
    """
    if explanation is not None:
        promises = tuple(
            f"до {p.due_on.strftime('%d.%m.%Y')} (запись {p.interaction_id})"
            for p in explanation.promises
        )
        return RowExplanation(text=explanation.text, source="ai", promises=promises)
    note = None
    if status is not None:
        if status.startswith("rejected:"):
            note = f"ответ модели отклонён проверкой ({status.split(':', 1)[1]})"
        else:
            note = _STATUS_NOTES.get(status, "пояснение ИИ недоступно")
    return RowExplanation(text=template_explanation(assessment), source="template", note=note)
