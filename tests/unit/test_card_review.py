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

from claims_assistant.application.ai_guard import (
    BUDGET_MESSAGE,
    AiPolicy,
    GuardedRecommendationProvider,
)
from claims_assistant.application.analysis_repository import RepositoryError
from claims_assistant.application.card_review import CARD_REFERENCE, CardReview, CardReviewer
from claims_assistant.application.check_company import CompanyCheck
from claims_assistant.application.recommendation import AiErrorCode, AiLimits, AiUnavailable
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
from claims_assistant.infrastructure.checko import courts, enforcements, finances
from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider
from claims_assistant.presentation.telegram.card import (
    REVIEW_CARD_LIMIT,
    REVIEW_FAILED,
    REVIEW_FOOTER,
    REVIEW_MONTH,
    REVIEW_NO_DATA,
    REVIEW_NOT_PREPARED,
    REVIEW_REJECTED,
    REVIEW_TIMEOUT,
    REVIEW_TITLE,
    REVIEW_TOO_MUCH,
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


def bailiffs(in_source: int | None):
    records = [
        {"ИспПрНомер": f"{n}/26/1-ИП", "ИспПрДата": "2026-09-01", "ОстЗадолж": 50_000.0}
        for n in range(3)
    ]
    return enforcements.project_enforcements(
        INN, records, 1, NOW, total=in_source, left=Decimal(150_000)
    )


@pytest.mark.parametrize(
    ("in_source", "title"),
    [
        (3, "Незавершённых исполнительных производств"),
        (250, "Незавершённых исполнительных производств — не меньше (источник отдал не все)"),
    ],
)
def test_a_cut_bailiffs_list_reaches_the_model_as_a_floor(in_source, title):
    """Live 17.10.2026: with «всего может быть больше» only in missing_data the model wrote
    «74 производства» of «не меньше 74». The title of the figure says it now."""
    snapshots = (bailiffs(in_source),)
    request = build_request(
        build_card_context(INN, assess(INN, snapshots), NOW.date(), snapshots, reference="card")
    )
    (open_count,) = [
        fact for fact in request.context["facts"] if fact["id"].startswith("enforcements-open")
    ]
    assert open_count["title"] == title and open_count["value"] == "3"
    assert "«не меньше 74" in CARD_INSTRUCTION


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


def failed(error: AiUnavailable) -> CardReview:
    return CardReview(type("Outcome", (), {"explanation": None, "rejected": None, "error": error}))


def test_the_reading_is_marked_as_the_models_and_names_the_company():
    """It comes up to 90 s after the card: another card may be in between."""
    model_answer = review_answer(answer(GOOD), context())
    outcome = type("Outcome", (), {"explanation": model_answer, "rejected": None, "error": None})
    text = format_review(CardReview(outcome), check())
    assert text.splitlines()[0] == f"{REVIEW_TITLE} — ИНН {INN}"
    assert GOOD in text and text.endswith(REVIEW_FOOTER)


def test_no_reading_says_why_in_one_line():
    month = format_review(CardReview(month_exhausted=True), check())
    assert month.startswith(f"🤖 Разбора ИИ нет (ИНН {INN}): {REVIEW_MONTH}")
    assert "по правилам" in month and "\n" not in month
    rejected = review_answer(answer("Иски на 9 млн ₽."), context())
    outcome = type("Outcome", (), {"explanation": None, "rejected": rejected, "error": None})
    assert REVIEW_REJECTED in format_review(CardReview(outcome), check())
    timeout = AiUnavailable(AiErrorCode.TIMEOUT, "Модель не ответила в срок.")
    assert REVIEW_TIMEOUT in format_review(failed(timeout), check())
    down = AiUnavailable(AiErrorCode.UNAVAILABLE, "Сервис ИИ недоступен.")
    assert REVIEW_FAILED in format_review(failed(down), check())


def test_our_own_limits_are_not_called_the_models_failure():
    """A request too large and a card's money spent are ours: the model was fine, and the
    guard's figures («71684 > 24000») are for the log."""
    large = AiUnavailable(AiErrorCode.BUDGET, "Запрос больше лимита (71684 > 24000).")
    text = format_review(failed(large), check())
    assert REVIEW_TOO_MUCH in text and "недоступна" not in text and "71684" not in text
    spent = format_review(failed(AiUnavailable(AiErrorCode.BUDGET, BUDGET_MESSAGE)), check())
    assert REVIEW_CARD_LIMIT in spent and "недоступна" not in spent
    # The reviewer itself failed (the month's spend unread): nobody asked the model.
    unread = format_review(None, check())
    assert REVIEW_NOT_PREPARED in unread and "модель" not in unread


async def test_a_card_too_large_for_the_request_says_so():
    company = check()
    review = await CardReviewer(
        StubRecommendationProvider(), limits=AiLimits(max_request_chars=500)
    ).review(company, assess(INN, company.snapshots), None)
    assert review.outcome.error.code is AiErrorCode.BUDGET
    assert REVIEW_TOO_MUCH in format_review(review, company)


async def test_a_model_that_never_answers_is_not_called_a_spent_limit():
    """18.10.2026 at the customer's: two timeouts, then the card's two calls were spent and
    the reading said «исчерпан лимит расходов». Nothing was paid; the model did not answer."""

    class Silent:
        name = "silent"
        model = "s"

        async def explain(self, request, limits):
            raise AiUnavailable(AiErrorCode.TIMEOUT, "Модель не ответила в срок.")

    guard = GuardedRecommendationProvider(
        Silent(), AiPolicy(max_retries=2), clock=lambda: NOW, sleep=no_sleep
    )
    company = check()
    review = await CardReviewer(guard, clock=lambda: NOW).review(
        company, assess(INN, company.snapshots), None
    )
    assert review.outcome.error.code is AiErrorCode.TIMEOUT
    text = format_review(review, company)
    assert REVIEW_TIMEOUT in text and REVIEW_CARD_LIMIT not in text


# --- review A on #96 ------------------------------------------------------------------


def busy_defendant() -> CompanyCheck:
    """A hundred open cases and a hundred proceedings: 71 684 characters before the cap."""
    from claims_assistant.infrastructure.checko import enforcements

    records = [case(f"А00-{n}/2026", 500_000) for n in range(100)]
    for n, record in enumerate(records):
        record["Дата"] = f"2026-{1 + n % 9:02d}-{1 + n % 28:02d}"
    bankruptcy = dict(case("А00-999/2026", 2_000_000), Тип="Б", Дата="2026-03-01")
    proceedings = [
        {
            "ИспПрНомер": f"{n}/26/00001-ИП",
            "ИспПрДата": f"2026-{1 + n % 9:02d}-{1 + n % 28:02d}",
            "ПредмИсп": "Налог (сбор), включая пени",
            "СудПристНаим": "ОСП по Синтетическому району",
            "ОстЗадолж": 10_000.0,
        }
        for n in range(100)
    ]
    return CompanyCheck(
        INN,
        (
            finances.project_finances(INN, YEARS, NOW),
            courts.project_courts(INN, [bankruptcy, *records[:99]], 101, Decimal(52_000_000), NOW),
            enforcements.project_enforcements(INN, proceedings, 1, NOW),
        ),
    )


def test_a_busy_defendant_still_fits_the_request_with_its_totals_whole():
    from claims_assistant.application.recommendation import request_size
    from claims_assistant.domain.ai_context import FACT_CAPS
    from claims_assistant.domain.external import FactKind

    company = busy_defendant()
    built = build_card_context(INN, assess(INN, company.snapshots), NOW.date(), company.snapshots)
    request = build_request(built)
    assert request_size(request) < 24_000
    titles = [fact.title for fact in built.facts]
    assert (
        titles.count("Арбитражное дело (организация — ответчик)") == FACT_CAPS[FactKind.COURT_CASE]
    )
    assert titles.count("Исполнительное производство (ФССП)") == FACT_CAPS[FactKind.ENFORCEMENT]
    # The bankruptcy case goes whatever its date, and so do the section's totals.
    assert "Дело о банкротстве организации (арбитражный суд)" in titles
    totals = {fact.title: fact.value for fact in built.facts}
    assert totals["Незавершённых арбитражных дел, где организация — ответчик"] == "101"
    assert any("новейших из 99; итоги раздела — по всем" in note for note in built.missing_data)
    # The newest go: the latest date of the cases is among those sent.
    dates = [f.observed_on for f in built.facts if f.title.startswith("Арбитражное дело")]
    every = [
        f.observed_on for s in company.snapshots for f in s.facts if f.kind is FactKind.COURT_CASE
    ]
    assert max(dates) == max(every)


def test_a_fact_a_signal_cites_is_never_cut():
    from claims_assistant.domain.scoring import Priority, Signal

    company = busy_defendant()
    assessment = assess(INN, company.snapshots)
    oldest = min(
        (f for s in company.snapshots for f in s.facts if f.id.startswith("court-case-")),
        key=lambda f: f.observed_on,
    )
    signal = Signal("test", Priority.MEDIUM, "Проверка.", (oldest.id,))
    cited = replace(assessment, signals=(*assessment.signals, signal))
    built = build_card_context(INN, cited, NOW.date(), company.snapshots)
    assert any(fact.id.endswith(oldest.id.split(":")[-1]) for fact in built.facts)


async def test_no_open_data_no_question_to_the_model():
    from claims_assistant.domain.external import (
        Coverage,
        DataMode,
        ExternalSnapshot,
        FetchStatus,
        ProviderError,
        Section,
    )

    def failed(section):
        return ExternalSnapshot(
            INN,
            section,
            "checko",
            DataMode.LIVE,
            NOW,
            FetchStatus.RATE_LIMITED,
            Coverage.UNAVAILABLE,
            missing=("Лимит запросов исчерпан.",),
            error=ProviderError("rate_limited", "Лимит запросов исчерпан."),
        )

    company = CompanyCheck(INN, (failed(Section.COMPANY), failed(Section.COURTS)))
    model = Priced(lambda request: answer(GOOD))
    review = await reviewer(model).review(company, assess(INN, company.snapshots), None)
    assert review.no_data and model.requests == []
    assert REVIEW_NO_DATA in format_review(review, company)
