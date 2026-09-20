"""S5-04: the model's text when accepted, the template from the rules otherwise; the
report is built whatever the model did."""

import json
from datetime import date

import pytest

from claims_assistant.application.recommendation import AiErrorCode, AiUnavailable
from claims_assistant.domain.ai_review import Explanation, Promise
from claims_assistant.domain.analysis import RunStatus
from claims_assistant.domain.explanation import (
    RowExplanation,
    explain_row,
    template_explanation,
)
from claims_assistant.domain.scoring import Assessment, Priority, Signal
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider, default_answer
from tests.unit.test_analysis_pipeline import OWNER, guard, pipeline, sheet_rows
from tests.unit.test_explanations import package

INN = "7707083893"


def assessment(priority=Priority.HIGH, signals=(), missing=()):
    return Assessment(
        inn=INN,
        priority=priority,
        signals=signals,
        missing_data=missing,
        coverage={},
        base_complete=not missing,
    )


def test_template_repeats_the_assessment_and_nothing_else():
    signal = Signal("overdue_60", Priority.HIGH, "Просрочка больше 60 дней.", ("internal-overdue",))
    text = template_explanation(assessment(signals=(signal,), missing=("Нет истории долга.",)))
    assert text == (
        "Приоритет по правилам — высокий. Сработало: Просрочка больше 60 дней. "
        "Уточнить причины задержки и договорённости об оплате. Не хватает: Нет истории долга."
    )
    unknown = template_explanation(assessment(Priority.UNKNOWN))
    assert unknown.startswith("Приоритет не определён: данных для оценки недостаточно.")
    assert "Дополнить данные" in unknown
    quiet = template_explanation(assessment(Priority.LOW))
    assert "Сигналов правил нет." in quiet and "Работать в обычном порядке." in quiet


def test_explain_row_prefers_the_model_and_lists_its_promises():
    explanation = Explanation(
        "Клиент сообщил, что оплатит.", (Promise("INT-1", date(2026, 9, 30), "до 30.09"),), ()
    )
    explained = explain_row(assessment(), explanation, "accepted")
    assert explained == RowExplanation(
        "Клиент сообщил, что оплатит.", "ai", None, ("до 30.09.2026 (запись INT-1)",)
    )
    assert explained.from_model


@pytest.mark.parametrize(
    "status, note",
    [
        (None, None),
        ("rejected:new_amount", "ответ модели отклонён проверкой (new_amount)"),
        ("unavailable:budget", "лимит ИИ на проверку исчерпан"),
        ("unavailable:timeout", "модель не ответила в срок"),
        ("unavailable:refused", "провайдер отклонил запрос"),
        ("unavailable:something", "пояснение ИИ недоступно"),
    ],
)
def test_explain_row_falls_back_to_the_template_with_a_reason(status, note):
    explained = explain_row(assessment(), None, status)
    assert explained.source == "template" and explained.note == note
    assert explained.text.startswith("Приоритет по правилам — высокий.")


async def test_report_carries_the_model_text_and_the_template_side_by_side(tmp_path):
    run, files, repository = await package(tmp_path)
    bad = json.dumps({"explanation": "Долг вырос до 250000 руб.", "promises": [], "grounds": []})
    rejecting_for_a = StubRecommendationProvider(
        script=lambda request: (
            bad if request.context["counterparty_ref"] == "row-1" else default_answer(request)
        )
    )
    outcome = await pipeline(
        files, repository, guard(DemoCompanyDataProvider()), explainer=rejecting_for_a
    ).process(run)
    assert outcome.status == RunStatus.PARTIAL and "Пояснений ИИ нет у 1" in outcome.failure
    artifact = await repository.get_report(OWNER, run.id)
    data = files.read(artifact.stored_path)
    priorities = {row[0]: row for row in sheet_rows(data, "Приоритеты")}
    first, second = priorities["1234567894"], priorities["7707083893"]
    assert first[8].startswith("По правилам: Приоритет")
    assert "пояснение ИИ недоступно: ответ модели отклонён проверкой (new_amount)" in first[8]
    assert second[8].startswith("ИИ: По правилам")
    quality = " ".join(str(c) for row in sheet_rows(data, "Качество данных") for c in row if c)
    assert "Пояснение ИИ недоступно 1234567894" in quality
    about = " ".join(str(c) for row in sheet_rows(data, "О проверке") for c in row if c)
    assert "принято 1, отклонено проверкой 1, недоступно 0" in about


async def test_report_is_built_when_the_model_is_down(tmp_path):
    run, files, repository = await package(tmp_path)
    down = StubRecommendationProvider(failure=AiUnavailable(AiErrorCode.UNAVAILABLE, "Нет связи."))
    outcome = await pipeline(
        files, repository, guard(DemoCompanyDataProvider()), explainer=down
    ).process(run)
    assert outcome.status == RunStatus.PARTIAL
    artifact = await repository.get_report(OWNER, run.id)
    data = files.read(artifact.stored_path)
    for row in sheet_rows(data, "Приоритеты"):
        assert row[8].startswith("По правилам: ") and "модель недоступна" in row[8]
    about = " ".join(str(c) for row in sheet_rows(data, "О проверке") for c in row if c)
    assert "принято 0, отклонено проверкой 0, недоступно 2" in about


async def test_report_without_a_provider_has_the_template_and_no_ai_notes(tmp_path):
    run, files, repository = await package(tmp_path)
    outcome = await pipeline(files, repository, guard(DemoCompanyDataProvider())).process(run)
    assert outcome.status == RunStatus.COMPLETED
    artifact = await repository.get_report(OWNER, run.id)
    data = files.read(artifact.stored_path)
    for row in sheet_rows(data, "Приоритеты"):
        assert row[8].startswith("По правилам: ") and "недоступно" not in row[8]
    quality = " ".join(str(c) for row in sheet_rows(data, "Качество данных") for c in row if c)
    assert "Пояснение ИИ недоступно" not in quality
    about = " ".join(str(c) for row in sheet_rows(data, "О проверке") for c in row if c)
    assert "не запрашивались; в отчёте пояснения по правилам" in about
