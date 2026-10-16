"""S8-05: the model's reading under the /inn card.

The customer (15.10.2026): «по текущим данным из Checko нет скоринга от ИИ — оч важно по
арбитражным делам и банкротству, и выручка год к году». Snapshots here are built by the
adapters from synthetic records; the model is the stub with a scripted answer.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from claims_assistant.application.ai_guard import AiPolicy, GuardedRecommendationProvider
from claims_assistant.application.analysis_repository import RepositoryError
from claims_assistant.application.card_review import CARD_REFERENCE, CardReview, CardReviewer
from claims_assistant.application.check_company import CompanyCheck
from claims_assistant.application.recommendation import AiErrorCode, AiUnavailable
from claims_assistant.domain.ai_context import (
    CARD_CONTEXT_VERSION,
    CARD_INSTRUCTION,
    CARD_INSTRUCTION_VERSION,
    INSTRUCTION,
    INSTRUCTION_VERSION,
    build_card_context,
    build_request,
)
from claims_assistant.domain.ai_review import Explanation, Rejected, RejectionCode, review_answer
from claims_assistant.domain.scoring import assess
from claims_assistant.infrastructure.checko import courts, finances
from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider
from claims_assistant.presentation.telegram.card import (
    REVIEW_FOOTER,
    REVIEW_MONTH,
    REVIEW_REJECTED,
    REVIEW_TITLE,
    format_review,
)

INN = "1234567894"
NOW = datetime(2026, 10, 16, 9, tzinfo=UTC)

# The customer's example of 15.10.2026, in round synthetic figures: revenue +12 %, net
# profit 3,8 → 0,76 million.
YEARS = {
    2024: {"2110": 100_000_000, "2400": 3_800_000},
    2025: {"2110": 112_000_000, "2400": 760_000},
}


def case(number: str, claim: int) -> dict:
    return {
        "Номер": number,
        "UUID": f"uuid-{number}",
        "Дата": "2026-09-24",
        "Суд": "Арбитражный суд Синтетической области",
        "Ист": [{"ИНН": "0000001011", "Наим": "ООО «Кредитор»"}],
        "Ответ": [{"ИНН": INN, "Наим": "ООО «ТЕСТ»"}],
        "СуммИск": claim,
        "Тип": "Г",
    }


def check() -> CompanyCheck:
    records = [case("А00-1/2026", 2_400_000), case("А00-2/2026", 1_000_000)]
    return CompanyCheck(
        INN,
        (
            finances.project_finances(INN, YEARS, NOW),
            courts.project_courts(INN, records, 2, Decimal(3_400_000), NOW),
        ),
    )


def context():
    company = check()
    return build_card_context(
        INN, assess(INN, company.snapshots), NOW.date(), company.snapshots, reference="card"
    )


def answer(text: str, grounds=()) -> str:
    return json.dumps(
        {"explanation": text, "promises": [], "grounds": list(grounds)}, ensure_ascii=False
    )


# --- the context ------------------------------------------------------------------------


def test_the_card_has_its_own_instruction_and_versions():
    request = build_request(context())
    assert request.instruction == CARD_INSTRUCTION != INSTRUCTION
    assert request.instruction_version == CARD_INSTRUCTION_VERSION
    assert request.context["context_version"] == CARD_CONTEXT_VERSION
    # The list's instruction is chosen by its own version, as before.
    listed = replace(context(), instruction_version=INSTRUCTION_VERSION)
    assert build_request(listed).instruction == INSTRUCTION


def test_revenue_and_profit_year_over_year_are_in_the_context():
    values = {
        value["title"]: value["value"] for value in build_request(context()).context["values"]
    }
    assert values["Изменение выручки"].startswith("+12.0% за 2024–2025 годы")
    assert values["Изменение чистой прибыли"].startswith("-80.0% за 2024–2025 годы")


def test_after_a_loss_year_the_trend_is_a_word_not_a_percent():
    years = {2024: {"2400": -100_000}, 2025: {"2400": 20_000}}
    snapshots = (finances.project_finances(INN, years, NOW),)
    built = build_card_context(INN, assess(INN, snapshots), NOW.date(), snapshots)
    (value,) = [v for v in built.values if v.title == "Изменение чистой прибыли"]
    assert value.value.startswith("рост за 2024–2025 годы; в 2024 году убыток")


def test_a_zero_base_is_called_zero_not_a_loss():
    years = {2024: {"2400": 0}, 2025: {"2400": 50_000}}
    snapshots = (finances.project_finances(INN, years, NOW),)
    built = build_card_context(INN, assess(INN, snapshots), NOW.date(), snapshots)
    (value,) = [v for v in built.values if v.title == "Изменение чистой прибыли"]
    assert value.value.startswith("рост за 2024–2025 годы; в 2024 году ноль")


def test_the_inn_never_leaves_and_there_are_no_comments():
    request = build_request(context())
    body = json.dumps(request.context, ensure_ascii=False)
    assert INN not in body
    assert request.context["counterparty_ref"] == "card"
    assert request.context["comments"] == []


def test_a_context_of_another_inn_is_refused():
    company = check()
    with pytest.raises(ValueError, match="INN"):
        build_card_context("7707083893", assess(INN, company.snapshots), NOW.date(), ())


# --- the review reads millions ---------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "К организации два незавершённых иска на 3,4 млн ₽.",
        "Иски к организации — около 3 млн ₽, крупнейший на 2,4 млн рублей.",
        "Выручка за 2025 год выросла на 12 %, а чистая прибыль снизилась на 80 %.",
        "Иск на 1 миллион рублей.",
    ],
)
def test_a_rounded_retelling_in_millions_is_accepted(text):
    result = review_answer(answer(text), context())
    assert isinstance(result, Explanation), result


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ("Иски к организации — на 5 млн ₽.", "5×1 000 000"),
        ("Иски к организации — на 3,4 млрд ₽.", "3.4×1 000 000 000"),
        ("Долг у приставов — 125 тыс. ₽.", "125×1 000"),
    ],
)
def test_a_new_amount_in_millions_is_rejected(text, detail):
    result = review_answer(answer(text), context())
    assert isinstance(result, Rejected)
    assert (result.code, result.detail) == (RejectionCode.NEW_AMOUNT, detail)


def test_thousands_retell_a_sum_in_roubles():
    company = check()
    snapshots = company.snapshots
    built = build_card_context(INN, assess(INN, snapshots), NOW.date(), snapshots)
    text = "Крупнейший иск — 2 400 тыс. ₽."
    assert isinstance(review_answer(answer(text), built), Explanation)


# --- the service ------------------------------------------------------------------------


class Priced(StubRecommendationProvider):
    """The stub with the price the provider reports (≈ 0,3 ₽ a card, measured on #85)."""

    async def explain(self, request, limits):
        return replace(await super().explain(request, limits), cost_rub=Decimal("0.30"))


class Spend:
    def __init__(self, start=Decimal(0), fail_add=False):
        self.total = start
        self.added: list[Decimal] = []
        self.fail_add = fail_add

    async def spent(self, month):
        assert month == "2026-10"
        return self.total

    async def add(self, month, amount):
        if self.fail_add:
            raise RepositoryError("Сбой хранилища расходов ИИ.")
        self.added.append(amount)
        self.total += amount
        return self.total


GOOD = "Выручка за 2025 год выросла на 12 %, а к организации два иска на 3,4 млн ₽."


async def no_sleep(_seconds):
    return None


def reviewer(inner, **kwargs) -> CardReviewer:
    guard = GuardedRecommendationProvider(inner, AiPolicy(), clock=lambda: NOW, sleep=no_sleep)
    return CardReviewer(guard, clock=lambda: NOW, **kwargs)


async def test_an_accepted_reading_is_shown_and_its_price_counted():
    model = Priced(lambda request: answer(GOOD))
    spend = Spend()
    company = check()
    review = await reviewer(model, spend=spend, month_limit_rub=Decimal(1000)).review(
        company, assess(INN, company.snapshots), None
    )
    assert review.text == GOOD
    assert spend.added == [Decimal("0.30")]
    (request,) = model.requests
    assert request.instruction == CARD_INSTRUCTION
    assert request.context["counterparty_ref"] == CARD_REFERENCE


async def test_the_same_card_twice_is_paid_for_once():
    model = Priced(lambda request: answer(GOOD))
    spend = Spend()
    service = reviewer(model, spend=spend, month_limit_rub=Decimal(1000))
    company = check()
    first = await service.review(company, assess(INN, company.snapshots), None)
    second = await service.review(company, assess(INN, company.snapshots), None)
    assert (first.cached, second.cached) == (False, True)
    assert second.text == GOOD
    assert len(model.requests) == 1 and spend.added == [Decimal("0.30")]


async def test_a_rejected_answer_is_kept_too_an_unavailable_one_is_not():
    rejected = Priced(lambda request: answer("Иски на 9 млн ₽."))
    service = reviewer(rejected)
    company = check()
    for _ in range(2):
        review = await service.review(company, assess(INN, company.snapshots), None)
        assert review.text is None and review.outcome.rejected is not None
    assert len(rejected.requests) == 1

    down = StubRecommendationProvider(failure=AiUnavailable(AiErrorCode.REFUSED, "Отказ."))
    service = reviewer(down)
    for _ in range(2):
        review = await service.review(company, assess(INN, company.snapshots), None)
        assert review.outcome.error is not None
    assert len(down.requests) == 2


async def test_a_spent_month_is_not_asked():
    model = Priced(lambda request: answer(GOOD))
    company = check()
    review = await reviewer(
        model, spend=Spend(Decimal(1000)), month_limit_rub=Decimal(1000)
    ).review(company, assess(INN, company.snapshots), None)
    assert review.month_exhausted and review.outcome is None
    assert model.requests == []


async def test_a_failing_model_is_asked_once_more_and_no_further():
    """One retry at most: a card is not worth a run's patience."""
    model = StubRecommendationProvider(failure=AiUnavailable(AiErrorCode.UNAVAILABLE, "Сбой."))
    company = check()
    review = await reviewer(model).review(company, assess(INN, company.snapshots), None)
    assert review.outcome.error.code is AiErrorCode.UNAVAILABLE
    assert len(model.requests) == 2  # the call and its one retry


async def test_a_failed_spend_write_does_not_take_the_reading_away(caplog):
    model = Priced(lambda request: answer(GOOD))
    company = check()
    review = await reviewer(
        model, spend=Spend(fail_add=True), month_limit_rub=Decimal(1000)
    ).review(company, assess(INN, company.snapshots), None)
    assert review.text == GOOD
    assert "ai_spend_not_recorded" in caplog.text


# --- what the user reads ----------------------------------------------------------------


def test_the_reading_is_marked_as_the_models():
    model_answer = review_answer(answer(GOOD), context())
    outcome = type("Outcome", (), {"explanation": model_answer, "rejected": None, "error": None})
    text = format_review(CardReview(outcome))
    assert text.startswith(REVIEW_TITLE) and GOOD in text and text.endswith(REVIEW_FOOTER)


def test_no_reading_says_why_in_one_line():
    month = format_review(CardReview(month_exhausted=True))
    assert REVIEW_MONTH in month and "по правилам" in month
    rejected = review_answer(answer("Иски на 9 млн ₽."), context())
    outcome = type("Outcome", (), {"explanation": None, "rejected": rejected, "error": None})
    assert REVIEW_REJECTED in format_review(CardReview(outcome))
    error = AiUnavailable(AiErrorCode.TIMEOUT, "Модель не ответила в срок.")
    outcome = type("Outcome", (), {"explanation": None, "rejected": None, "error": error})
    assert "модель недоступна (модель не ответила в срок)" in format_review(CardReview(outcome))
    assert "модель недоступна" in format_review(None)
