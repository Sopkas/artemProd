"""S5-06: the model's answer is checked against the context it was given.

The criterion: an incorrect answer is rejected with an understandable code, and the model
never changes the priority. Synthetic answers only — no model is called.
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claims_assistant.domain.ai_context import ContextLimits, build_card_context, build_context
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


@pytest.mark.parametrize("priority", ["high", "medium", "low"])
@pytest.mark.parametrize(
    "text",
    [
        "Приоритет высокий; приоритет критический по сути.",
        "Ситуация критическая для взыскания.",
        "Ситуация критичная.",
        "Приоритет критичен для взыскания.",
    ],
)
def test_the_critical_level_is_caught_in_every_form(priority, text):
    """Review G2: the context names the level «критический», and only «критичн» was
    looked for — the very word the model is given went through."""
    ctx = replace(context(), priority=priority)
    assert code(review_answer(answer(explanation=text), ctx)) == RejectionCode.PRIORITY_CHANGED


@pytest.mark.parametrize(
    "priority,text",
    [
        ("low", "Приоритет низкий: риск невысокий."),
        ("medium", "Просрочка 75 дн., долговая нагрузка высокая."),
        ("medium", "Приоритет средний. Выручка низкая."),
        ("medium", "Приоритет средний. Долговая нагрузка высокая, выручка низкая."),
        ("low", "Срочно позвонить должнику. Выручка высокая."),
        ("medium", "Приоритет по просрочке 75 дн. средний."),
        ("high", "Просрочка 75 дн., в среднем платежи редкие."),
        ("high", "Просрочка 75 дн., выручка низкая."),
        ("high", "Критически важно проверить сообщения ЕФРСБ."),
        # Round 4: a level attached to a metric of the debtor, in any case and number.
        ("medium", "Приоритет средний: выручка низкая."),
        ("high", "Приоритет высокий. Это связано с просрочкой 75 дн. и низкой выручкой."),
        ("medium", "Приоритет средний. Это объясняется тем, что выручка низкая."),
        ("high", "Приоритет высокий. Средняя выручка снизилась."),
        ("medium", "Долговая нагрузка — высокая."),
        ("low", "Рентабельность низкая, ликвидность высока."),
        ("high", "При низкой платёжеспособности и средних ставках долг растёт."),
        ("low", "Высокий оборот, высокая доля выручки от лизинга."),
        ("medium", "Прибыль низкая, высокая стоимость обслуживания долга."),
    ],
)
def test_a_level_word_about_the_debtor_is_not_a_priority(priority, text):
    """Review G2: «высок», «средн», «низк» anywhere were a priority — «нагрузка высокая»
    or «в среднем» cost the whole answer as priority_changed. Every level word counts
    now; these are the narrow cases that do not."""
    ctx = replace(context(), priority=priority)
    assert isinstance(review_answer(answer(explanation=text), ctx), Explanation)


@pytest.mark.parametrize(
    "priority,text",
    [
        ("medium", "Приоритет: высокий."),
        ("high", "Риск банкротства низкий."),
        ("high", "Риск остаётся невысоким."),
        ("critical", "Высокий риск банкротства."),
        # The fix's review: the whole sentence counts, not two words inside a clause.
        ("high", "Долг погашен, поэтому приоритет на самом деле низкий."),
        ("low", "Приоритет у дела высокий."),
        ("low", "Приоритет, на наш взгляд, высокий."),
        ("low", "Приоритет (по правилам низкий) фактически высокий."),
        ("low", "Приоритет взыскания с этого должника — высокий."),
        ("low", "Риск невозврата долга высокий."),
        ("medium", "Приоритет следует считать высоким."),
        ("medium", "Уровень приоритета на деле — высокий."),
        ("medium", "Риски для взыскания высокие."),
        ("medium", "Высокоприоритетный должник."),
        ("high", "Ситуация средней тяжести, приоритет можно считать низким"),
        # Round 3: the next sentence pointing back, and the nouns that mean the same.
        ("low", "Приоритет по правилам низкий. На наш взгляд, он высокий."),
        ("low", "Приоритет низкий. Но на деле он должен быть высоким."),
        ("low", "Приоритет низкий. Высокий — по сути."),
        ("low", "Приоритет низкий. Это по правилам. Но он высокий."),
        ("low", "Срочность взыскания высокая."),
        ("low", "Уровень угрозы высокий."),
        ("low", "Важность дела высокая, работать в первую очередь."),
        ("low", "Опасность невозврата высокая."),
        # Round 4: any level that disagrees counts, no anchor or pronoun needed.
        ("low", "Приоритет низкий. На деле — высокий."),
        ("low", "Приоритет низкий. На самом деле высокий."),
        ("low", "Приоритет по правилам низкий. Фактически высокий."),
        ("low", "Приоритет низкий. Мы бы поставили высокий."),
        ("low", "Правила дали низкий приоритет. Однако правильнее считать высоким."),
        ("medium", "Приоритет средний. Реально же — высокий."),
        ("high", "Приоритет высокий. Мы считаем, что достаточно низкого."),
        ("low", "Приоритет низкий. Им стоит заняться как высоким."),
        ("low", "Приоритет низкий. Но высокий по сути."),
        ("low", "Приоритет низкий. Скорее высокий."),
        ("low", "Значимость дела высокая."),
        ("low", "Ставим высокую очерёдность взыскания."),
        ("high", "Оцениваем дело низко, приоритет низок."),
        ("low", "Приоритет в среднем высокий."),
        # A metric next to the level, not agreeing with it: the level is of something else.
        ("low", "Важность из-за нагрузки высокая."),
        ("low", "Приоритет из-за выручки высокий."),
        ("medium", "Приоритет высокий, выручка низкая."),
        # Safe side: a level of a metric off the list, or not next to it, is a level.
        ("medium", "Уровень долговой нагрузки высокий."),
        ("high", "Безопасность сделки низкая."),
    ],
)
def test_a_level_that_disagrees_with_the_priority_is_rejected(priority, text):
    """Review G2 of the fix: a level counted only near «приоритет», «риск» or a pronoun,
    and every round found a wording without one. Any level that disagrees counts now."""
    ctx = replace(context(), priority=priority)
    assert code(review_answer(answer(explanation=text), ctx)) == RejectionCode.PRIORITY_CHANGED


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


@pytest.mark.parametrize(
    "comment",
    [
        "Клиент обещал оплатить до 15 сентября 2026 года.",
        "Клиент обещал оплатить до 15 сентября.",
        "Обещали оплатить 15.09.",
    ],
)
def test_a_promise_dated_as_people_write_it_is_found_in_the_comment(comment):
    """Review G2: only «15.09.2026» and ISO dates were read in a comment, so a promise of
    «15 сентября» or «15.09.» was always promise_not_in_comment, and retelling its date
    new_date. Without a year it is the first such day after the comment (2026-08-20)."""
    ctx = context(interactions=[talk(text=comment)])
    promise = {"interaction_id": "INT-8", "due_on": "2026-09-15"}
    result = review_answer(answer(promises=[promise]), ctx)
    assert isinstance(result, Explanation), result
    assert result.promises[0].due_on == date(2026, 9, 15)
    for text in ("Клиент сообщил об оплате до 15 сентября.", "Клиент оплатит 15.09.2026."):
        assert isinstance(review_answer(answer(explanation=text), ctx), Explanation), text
    # Another day is still not in the comment.
    other = {"interaction_id": "INT-8", "due_on": "2026-09-16"}
    assert code(review_answer(answer(promises=[other]), ctx)) == "promise_not_in_comment"


@pytest.mark.parametrize(
    "comment,due_on",
    [
        ("Оплатили частично 3.06 млн ₽, остаток без срока.", "2027-06-03"),
        ("Созвон в 12.05, клиент обещал перезвонить.", "2027-05-12"),
    ],
)
def test_a_decimal_or_a_time_in_a_comment_is_no_date(comment, due_on):
    """Review G2 of the fix: «d.dd» without its closing dot read as a date, so an amount
    or a clock time legitimised an invented promise and the same date in the text."""
    ctx = context(interactions=[talk(text=comment)])
    promise = {"interaction_id": "INT-8", "due_on": due_on}
    assert code(review_answer(answer(promises=[promise]), ctx)) == "promise_not_in_comment"
    day = date.fromisoformat(due_on).strftime("%d.%m.%Y")
    retold = answer(explanation=f"Клиент обещал оплатить до {day}.")
    assert code(review_answer(retold, ctx)) == "new_date"


def test_a_date_without_a_year_is_the_next_such_day_after_the_comment():
    ctx = context(interactions=[talk(text="Обещали оплатить 15.01.", day=date(2026, 12, 20))])
    for due_on, accepted in (("2027-01-15", True), ("2026-01-15", False)):
        promise = {"interaction_id": "INT-8", "due_on": due_on}
        result = review_answer(answer(promises=[promise]), ctx)
        assert isinstance(result, Explanation) is accepted, due_on


def test_a_promise_names_the_customers_own_record_not_the_masked_one():
    """With the INN kept back, a record ID made of it is masked in the request (review
    G2); the promise the report shows points at the record the customer knows."""
    row = InteractionRow(
        inn=INN, interaction_id=f"{INN}-1", happened_on=date(2026, 8, 20), comment=PROMISE
    )
    ctx = context(interactions=[row])
    masked = ctx.comments[0].interaction_id
    assert INN not in masked
    promise = {"interaction_id": masked, "due_on": "2026-09-15"}
    result = review_answer(answer(promises=[promise], grounds=[masked]), ctx)
    assert isinstance(result, Explanation), result
    assert result.promises[0].interaction_id == f"{INN}-1"


def test_a_year_of_the_reports_retold_alone_is_known():
    """Review G2: with one year of reports there is no trend line, and the year was only
    in the period (cut out as dates) and the record ID (glued by a space into 20252025)."""
    fin = finances.project_finances(INN, {2025: {"2110": 600, "2400": 100}}, NOW)
    ctx = build_card_context(INN, assess(INN, [fin], analysis_date=DAY), DAY, [fin])
    assert not ctx.values
    result = review_answer(answer(explanation="Выручка за 2025 год — 600 ₽."), ctx)
    assert isinstance(result, Explanation), result


def test_the_number_of_comments_not_sent_may_be_named():
    """The model is told how many comments were left out (rule 7: say what is missing)."""
    ctx = replace(context(), comments=(), comments_omitted=3)
    result = review_answer(answer(explanation="3 комментария в запрос не переданы."), ctx)
    assert isinstance(result, Explanation), result


def test_a_number_closing_a_comment_does_not_glue_to_the_next_field():
    ctx = context(interactions=[talk(text="Клиент обещал оплатить до 15.09.2026 сумму 50000")])
    result = review_answer(answer(explanation="Клиент сообщил, что оплатит 50000."), ctx)
    assert isinstance(result, Explanation), result


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
