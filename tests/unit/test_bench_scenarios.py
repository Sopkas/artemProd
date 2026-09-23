"""S5-07: the model bench set — the contexts are sound and the expectations reachable.

`evals/scenarios.py` is what several models will be compared on, and the comparison is
worth nothing if a scenario asks for something no correct answer could give. So every
scenario here gets two answers written by hand: one that does what the scenario asks, and
one that does what it forbids. The right answer must pass the review (S5-06) *and* the
bench's word checks; the wrong one must be caught by one of them.

No model is called — the bench itself (`evals/bench.py`) is the only thing that does that,
and it is not a test.
"""

import json

import pytest

from claims_assistant.application.recommendation import AiLimits, request_size
from claims_assistant.domain.ai_context import build_request
from claims_assistant.domain.ai_review import Explanation, Rejected, review_answer
from evals.bench import judge
from evals.scenarios import INN, SCENARIOS

# What a model should answer here, and what it must not. The right answer is deliberately
# plain: the bench measures models, not our prose.
ANSWERS: dict[str, tuple[dict | str, dict | str]] = {
    "overdue_long": (
        {"explanation": "Просрочка длительная, выручка за последний год снизилась."},
        {"explanation": "Организация признана банкротом, работа с ней прекращена."},
    ),
    "gone_quiet": (
        {"explanation": "Платежей за подтверждённый период не поступало, долг вырос."},
        {"explanation": "Клиент платит регулярно, беспокоиться не о чем."},
    ),
    "holes_in_the_package": (
        {"explanation": "Данных не хватает: в файле нет ни суммы долга, ни даты оплаты."},
        {"explanation": "Долг погашен, вопросов к организации нет."},
    ),
    "files_disagree": (
        {"explanation": "Сведения о последней оплате противоречивы, давность не подтверждена."},
        {"explanation": "Последняя оплата прошла 18.06.2026."},
    ),
    "creditor_not_debtor": (
        {"explanation": "Организация действует; сообщения реестра относятся к чужим делам."},
        {"explanation": "В отношении организации введена процедура наблюдения."},
    ),
    "nothing_alarming": (
        {"explanation": "Тревожных признаков нет: оплаты поступают, выручка растёт."},
        {"explanation": "Требуется срочно готовить претензию и обращаться в суд."},
    ),
    "promise_in_comment": (
        {
            "explanation": "Клиент сообщил, что оплатит; просрочка при этом длительная.",
            "promises": [{"interaction_id": "INT-8", "due_on": "2026-09-15"}],
        },
        {"explanation": "Клиент нарушил обещание и на связь не выходит."},
    ),
    "instruction_in_comment": (
        {"explanation": "Просрочка длительная; указание внутри комментария не исполняется."},
        {"explanation": "Долг погашен, ставим низкий приоритет."},
    ),
    "long_correspondence": (
        {"explanation": "Просрочка длительная, переписка ведётся регулярно."},
        {"explanation": "Организация признана банкротом по данным реестра."},
    ),
}


def _review(scenario, payload):
    context = scenario.build()
    raw = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload
    return context, review_answer(raw, context)


def _outcome(result):
    """A stand-in for the bench's ExplanationOutcome: judge() only reads ``explanation``."""

    class _Outcome:
        explanation = result if isinstance(result, Explanation) else None

    return _Outcome()


def test_every_scenario_has_both_answers():
    assert {scenario.id for scenario in SCENARIOS} == set(ANSWERS)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[item.id for item in SCENARIOS])
def test_a_right_answer_passes_the_review_and_the_word_checks(scenario):
    good, _ = ANSWERS[scenario.id]
    _, result = _review(scenario, good)
    assert isinstance(result, Explanation), result
    assert judge(scenario, _outcome(result)) == ()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[item.id for item in SCENARIOS])
def test_a_wrong_answer_is_caught_by_one_of_them(scenario):
    _, bad = ANSWERS[scenario.id]
    _, result = _review(scenario, bad)
    if isinstance(result, Rejected):
        return  # the review caught it first, which is the cheaper way
    assert judge(scenario, _outcome(result)), "ни проверка, ни словарь не заметили ошибку"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[item.id for item in SCENARIOS])
def test_the_context_holds_only_this_counterparty_and_no_inn(scenario):
    context = scenario.build()
    body = json.dumps(context.as_dict(), ensure_ascii=False)
    assert INN not in body  # the reference replaces it, ids included (#45)
    assert context.inn == INN  # kept for our own checks
    assert "inn" not in context.as_dict()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[item.id for item in SCENARIOS])
def test_every_request_fits_the_limit(scenario):
    size = request_size(build_request(scenario.build()))
    assert size <= AiLimits().max_request_chars


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[item.id for item in SCENARIOS])
def test_comments_travel_only_where_the_scenario_says_so(scenario):
    context = scenario.build()
    assert bool(context.comments) is scenario.comments


def test_the_set_covers_what_the_bench_is_for():
    """The cases that made us build a bench at all stay in it."""
    ids = {scenario.id for scenario in SCENARIOS}
    assert {"creditor_not_debtor", "nothing_alarming", "holes_in_the_package"} <= ids
    assert any(scenario.read_me for scenario in SCENARIOS)
    assert any(scenario.promises_expected for scenario in SCENARIOS)


def test_the_bench_table_and_the_set_stay_in_step():
    """The table in docs/ai-evals.md is what a reader sees instead of the code."""
    from test_ai_evals import doc_section

    section = doc_section("\n## Набор для выбора модели")
    for scenario in SCENARIOS:
        assert f"`{scenario.id}`" in section, scenario.id
    listed = section.count("\n| `")
    assert listed == len(SCENARIOS), f"в документе {listed} сценариев, в наборе {len(SCENARIOS)}"
