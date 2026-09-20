"""S5-06: the model's answer is checked against the context it was given.

The criterion: an incorrect answer is rejected with an understandable code, and the model
never changes the priority. Synthetic answers only — no model is called.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claims_assistant.domain.ai_context import build_context
from claims_assistant.domain.ai_review import (
    MAX_EXPLANATION_CHARS,
    Explanation,
    Rejected,
    RejectionCode,
    review_answer,
)
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.interactions import InteractionRow
from claims_assistant.domain.scoring import assess
from claims_assistant.infrastructure.checko import bankruptcy, finances
from claims_assistant.infrastructure.checko.company_data import normalize_company

INN = "1234567894"
DAY = date(2026, 9, 1)
NOW = datetime(2026, 9, 18, tzinfo=UTC)
PROMISE = "Клиент обещал оплатить до 15.09.2026."


def context(interactions=(), overdue=75):
    row = CounterpartyRow(
        inn=INN,
        cutoff_date=DAY,
        debt=Decimal("1000.00"),
        overdue_days=overdue,
        last_payment_date=date(2026, 6, 1),
    )
    company = normalize_company(
        {
            "meta": {"status": "ok"},
            "data": {
                "ИНН": INN,
                "ОГРН": "0000000000000",
                "НаимСокр": "ДЕМО",
                "ДатаВып": "2026-09-01",
                "Статус": {"Код": "001", "Наим": "Действует"},
            },
        },
        INN,
        NOW,
    )
    efrsb = bankruptcy.project_bankruptcy(INN, [], NOW, complete=True, unreadable=0)
    fin = finances.project_finances(
        INN, {2024: {"2110": 1000, "2400": 10}, 2025: {"2110": 600, "2400": -20}}, NOW
    )
    snapshots = [company, efrsb, fin]
    return build_context(
        row,
        assess(INN, snapshots, row, analysis_date=DAY),
        DAY,
        interactions=interactions,
        snapshots=snapshots,
    )


def talk(text=PROMISE, interaction_id="INT-8", day=date(2026, 8, 20)):
    return InteractionRow(inn=INN, interaction_id=interaction_id, happened_on=day, comment=text)


def answer(**fields):
    return json.dumps({"explanation": "Просрочка 75 дн., поэтому дело в работе.", **fields})


def code(result) -> str:
    assert isinstance(result, Rejected), result
    return result.code


def test_a_correct_answer_passes_with_its_promises_and_grounds():
    ctx = context(interactions=[talk()])
    grounds = [ctx.values[0].id, ctx.facts[0].id]
    result = review_answer(
        answer(
            explanation="Просрочка 75 дн. при долге 1000.00; клиент сообщил об оплате.",
            grounds=grounds,
            promises=[{"interaction_id": "INT-8", "due_on": "2026-09-15"}],
        ),
        ctx,
    )
    assert isinstance(result, Explanation)
    assert result.promises[0].due_on == date(2026, 9, 15)
    assert result.grounds == tuple(grounds)


def test_a_dict_answer_is_accepted_as_well_as_json_text():
    ctx = context()
    assert isinstance(review_answer({"explanation": "Просрочка 75 дн."}, ctx), Explanation)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ("не json", RejectionCode.NOT_JSON),
        ('["список"]', RejectionCode.SCHEMA),
        ("{}", RejectionCode.SCHEMA),
        ('{"explanation": 5}', RejectionCode.SCHEMA),
        ('{"explanation": "текст", "приоритет": "низкий"}', RejectionCode.SCHEMA),
        ('{"explanation": "текст", "grounds": "не список"}', RejectionCode.SCHEMA),
        ('{"explanation": "   "}', RejectionCode.EMPTY_EXPLANATION),
    ],
)
def test_answers_outside_the_schema_are_rejected(payload, expected):
    assert code(review_answer(payload, context())) == expected


def test_a_too_long_explanation_is_rejected():
    long_text = "Просрочка 75 дн. " * 200
    assert len(long_text) > MAX_EXPLANATION_CHARS
    assert code(review_answer(answer(explanation=long_text), context())) == RejectionCode.TOO_LONG


def test_grounds_outside_the_context_are_rejected():
    result = review_answer(answer(grounds=["fact-from-nowhere"]), context())
    assert code(result) == RejectionCode.UNKNOWN_GROUND
    assert result.detail == "fact-from-nowhere"


def test_an_invented_amount_is_rejected_but_a_rounded_one_is_not():
    ctx = context()
    invented = review_answer(answer(explanation="Долг вырос до 5000 руб."), ctx)
    assert code(invented) == RejectionCode.NEW_AMOUNT
    # −40.0% in the context may be retold as 40%.
    rounded = review_answer(answer(explanation="Выручка упала на 40%."), ctx)
    assert isinstance(rounded, Explanation)


def test_an_invented_date_is_rejected():
    ctx = context(interactions=[talk()])
    invented = review_answer(answer(explanation="Оплата ожидается 01.12.2026."), ctx)
    assert code(invented) == RejectionCode.NEW_DATE
    # A date from a comment is fine: the model may retell it.
    quoted = review_answer(answer(explanation="Клиент сообщил об оплате до 15.09.2026."), ctx)
    assert isinstance(quoted, Explanation)


@pytest.mark.parametrize(
    "text",
    [
        "Приоритет должен быть ниже: организация платит.",
        "Это скорее низкий риск, чем высокий.",
        "Рекомендую снизить приоритет до среднего.",
    ],
)
def test_arguing_with_the_priority_is_rejected(text):
    assert (
        code(review_answer(answer(explanation=text), context())) == RejectionCode.PRIORITY_CHANGED
    )


def test_the_answer_may_name_its_own_priority_and_missing_data():
    ctx = context()
    assert ctx.priority == "high"
    passing = review_answer(
        answer(explanation="Высокий приоритет: просрочка 75 дн. Данных о платежах недостаточно."),
        ctx,
    )
    assert isinstance(passing, Explanation)


@pytest.mark.parametrize(
    "promise,expected",
    [
        ({"interaction_id": "INT-404", "due_on": "2026-09-15"}, RejectionCode.UNKNOWN_INTERACTION),
        ({"interaction_id": "INT-8"}, RejectionCode.PROMISE_WITHOUT_DATE),
        ({"interaction_id": "INT-8", "due_on": "завтра"}, RejectionCode.PROMISE_WITHOUT_DATE),
        (
            {"interaction_id": "INT-8", "due_on": "2026-10-01"},
            RejectionCode.PROMISE_NOT_IN_COMMENT,
        ),
        (
            {"interaction_id": "INT-8", "due_on": "2026-09-15", "quote": "обещал заплатить всё"},
            RejectionCode.PROMISE_NOT_IN_COMMENT,
        ),
        (
            {"interaction_id": "INT-8", "due_on": "2026-09-15", "автор": "модель"},
            RejectionCode.SCHEMA,
        ),
    ],
)
def test_promises_are_checked_against_the_comment_they_point_at(promise, expected):
    ctx = context(interactions=[talk()])
    assert code(review_answer(answer(promises=[promise]), ctx)) == expected


def test_a_promise_needs_a_comment_in_the_context_at_all():
    # Comments were not sent (or there are none): a promise has nothing to rest on.
    result = review_answer(
        answer(promises=[{"interaction_id": "INT-8", "due_on": "2026-09-15"}]), context()
    )
    assert code(result) == RejectionCode.UNKNOWN_INTERACTION


def test_a_rejection_carries_a_stable_code_and_a_readable_reason():
    result = review_answer("не json", context())
    assert isinstance(result, Rejected)
    assert result.code == RejectionCode.NOT_JSON
    assert result.message.endswith("(not_json)") and result.reason.endswith(".")
