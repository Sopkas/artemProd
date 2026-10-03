"""S5-06: the model's answer is checked against the context it was given.

The criterion: an incorrect answer is rejected with an understandable code, and the model
never changes the priority. Synthetic answers only — no model is called.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claims_assistant.domain.ai_context import ContextLimits, build_context
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
        # The answers under test quote comments, so this context carries them (S5-05).
        limits=ContextLimits(include_comments=True),
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
    "text,found",
    [
        ("Основание revenue-2025: выручка упала на 40%.", "revenue-2025"),
        ("Сработал сигнал revenue_drop_30.", "revenue_drop_30"),
        ("Показатель internal-overdue равен 75 дн.", "internal-overdue"),
    ],
)
def test_an_internal_id_in_the_text_is_its_own_rejection(text, found):
    """Review A on #63: such IDs used to come out as new_amount «-0» or «30»."""
    result = review_answer(answer(explanation=text), context())
    assert code(result) == RejectionCode.INTERNAL_ID
    assert result.detail == found


@pytest.mark.parametrize(
    "text",
    [
        "Просрочка 75 дней (internal-overdue), выручка упала на 40% (revenue-2025).",
        "Просрочка 75 дней (основания: internal-overdue, internal-debt).",
        "Просрочка 75 дней (основание: internal-overdue).",
        "Просрочка 75 дней. Основания: internal-overdue, revenue_drop_30.",
    ],
)
def test_a_citation_of_known_ids_is_cut_not_rejected(text):
    """A on #72: 20 correct answers of the S5-07 run were lost to «(internal-overdue)».
    The IDs are checked in «grounds»; the specialist reads the sentence without them."""
    result = review_answer(answer(explanation=text), context())
    assert isinstance(result, Explanation), result
    assert "internal-" not in result.text and "revenue" not in result.text
    assert result.text.startswith("Просрочка 75 дней") and "( " not in result.text


def test_a_citation_of_an_unknown_id_is_not_cut():
    result = review_answer(answer(explanation="Просрочка 75 дней (fact-invented-9)."), context())
    assert code(result) == RejectionCode.NEW_AMOUNT


@pytest.mark.parametrize(
    "text,accepted",
    [
        ("Просрочка более 70 дней.", True),  # 75 in the data
        ("Просрочка свыше 60 дней.", True),
        ("Просрочка не менее 75 дней.", True),
        ("Просрочка менее 80 дней.", True),
        ("Нет платежей более 92 дней.", False),  # not true of 92, the largest near it
        ("Просрочка более 30 дней.", False),  # true, but too far to be a retelling
        ("Просрочка более 100 дней.", False),
        ("Просрочка 70 дней.", False),  # no bound: a plain new number
    ],
)
def test_a_rounded_bound_is_a_retelling_of_a_close_number(text, accepted):
    """A on #72, live bot 30.09: «более 90 дней» at 91 and «более 80 дней» at 82."""
    result = review_answer(answer(explanation=text), context())
    assert isinstance(result, Explanation) is accepted, result


def test_a_date_in_words_is_a_date():
    """A on #72: «25 сентября 2026 года» used to be rejected as the new number 25."""
    ctx = context(interactions=[talk()])
    for text in ("Клиент обещал оплату до 15 сентября 2026 года.", "Оплата до 15 сентября."):
        assert isinstance(review_answer(answer(explanation=text), ctx), Explanation), text
    invented = review_answer(answer(explanation="Оплата ожидается 1 декабря 2026 г."), ctx)
    assert code(invented) == RejectionCode.NEW_DATE and invented.detail == "01.12.2026"


def test_an_interaction_id_may_be_cited_and_is_not_a_number():
    ctx = context(interactions=[talk()])
    cited = review_answer(answer(explanation="В записи INT-8 клиент обещал оплату."), ctx)
    assert isinstance(cited, Explanation)


def test_an_id_does_not_lend_its_digits_to_the_text():
    """«revenue-2025» in the context is a name: it neither makes -2025 known nor glues to
    the next number, so a new number is still new."""
    ctx = context()
    assert code(review_answer(answer(explanation="Разница 20252025 руб."), ctx)) == "new_amount"


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
