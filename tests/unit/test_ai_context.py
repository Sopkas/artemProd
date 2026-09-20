"""S5-05: the context for the model and the versioned instruction.

The criterion: the model gets only what concerns this counterparty, facts stay apart from
comments, and a promise can be traced to the interaction it came from.
"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from claims_assistant.domain.ai_context import (
    CONTEXT_VERSION,
    INSTRUCTION,
    INSTRUCTION_VERSION,
    ContextLimits,
    build_context,
    build_request,
    versions,
)
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.debt_history import DebtSnapshot
from claims_assistant.domain.external import Period
from claims_assistant.domain.indicators import internal_indicators
from claims_assistant.domain.interactions import InteractionRow
from claims_assistant.domain.payments import PaymentRow
from claims_assistant.domain.scoring import Priority, assess
from claims_assistant.infrastructure.checko import bankruptcy, finances
from claims_assistant.infrastructure.checko.company_data import normalize_company

INN = "1234567894"
OTHER = "7707083893"
DAY = date(2026, 9, 1)
NOW = datetime(2026, 9, 18, tzinfo=UTC)
SECRET = "Клиент прислал реквизиты и телефон бухгалтера."


def row(inn=INN, debt="1000.00", overdue=75, last=date(2026, 6, 1)):
    return CounterpartyRow(
        inn=inn,
        cutoff_date=DAY,
        debt=Decimal(debt) if debt is not None else None,
        overdue_days=overdue,
        last_payment_date=last,
    )


def snapshots(inn=INN, years=None, events=()):
    company = normalize_company(
        {
            "meta": {"status": "ok"},
            "data": {
                "ИНН": inn,
                "ОГРН": "0000000000000",
                "НаимСокр": "ДЕМО",
                "ДатаВып": "2026-09-01",
                "Статус": {"Код": "001", "Наим": "Действует"},
            },
        },
        inn,
        NOW,
    )
    efrsb = bankruptcy.project_bankruptcy(inn, list(events), NOW, complete=True, unreadable=0)
    fin = finances.project_finances(
        inn, years or {2024: {"2110": 1000, "2400": 10}, 2025: {"2110": 600, "2400": -20}}, NOW
    )
    return [company, efrsb, fin]


def talk(day, text, interaction_id="INT-1", inn=INN, channel="телефон"):
    return InteractionRow(
        inn=inn, interaction_id=interaction_id, happened_on=day, comment=text, channel=channel
    )


def context_for(line=None, interactions=(), indicators=None, snaps=None, limits=None, ref=None):
    """Comments are off by default in the contract; most tests here are about them."""
    line = line or row()
    snaps = snapshots() if snaps is None else snaps
    assessment = assess(line.inn, snaps, line, indicators=indicators, analysis_date=DAY)
    return build_context(
        line,
        assessment,
        DAY,
        indicators=indicators,
        interactions=interactions,
        snapshots=snaps,
        limits=limits or ContextLimits(include_comments=True),
        reference=ref,
    )


def test_context_holds_the_rule_result_as_given():
    context = context_for()
    assert context.inn == INN and context.analysis_date == DAY
    assert context.priority == Priority.HIGH.value  # overdue_60 + revenue drop
    assert context.next_step and context.rules_version == "0.1"
    assert {signal.code for signal in context.signals} >= {"overdue_60", "revenue_drop_30"}
    assert all(signal.level in {"critical", "high", "medium"} for signal in context.signals)


def test_only_this_counterparty_is_in_the_context():
    interactions = [talk(DAY, "Наш разговор"), talk(DAY, SECRET, "INT-9", inn=OTHER)]
    context = context_for(interactions=interactions)
    assert [c.interaction_id for c in context.comments] == ["INT-1"]
    body = json.dumps(context.as_dict(), ensure_ascii=False)
    assert OTHER not in body and SECRET not in body
    assert "Наш разговор" in body


def test_the_inn_is_not_sent_unless_the_caller_asks():
    plain = context_for()
    assert plain.inn == INN  # kept for the checks
    assert "inn" not in plain.as_dict() and "counterparty_ref" not in plain.as_dict()
    assert INN not in json.dumps(plain.as_dict(), ensure_ascii=False)
    referenced = context_for(ref="row-3")
    assert referenced.as_dict()["counterparty_ref"] == "row-3"
    assert INN not in json.dumps(referenced.as_dict(), ensure_ascii=False)
    asked = context_for(limits=ContextLimits(include_inn=True))
    assert asked.as_dict()["inn"] == INN
    with pytest.raises(ValueError):  # a reference that would leak the INN anyway
        context_for(ref=f"run-1/{INN}")


def test_comments_are_left_out_until_the_caller_asks_for_them():
    context = build_context(
        row(),
        assess(INN, snapshots(), row(), analysis_date=DAY),
        DAY,
        interactions=[talk(DAY, SECRET)],
    )
    assert context.comments == () and context.comments_omitted == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"snaps": snapshots(OTHER)},
        {"indicators": internal_indicators(row(inn=OTHER), DAY)},
    ],
)
def test_foreign_data_is_an_error_not_a_filter(kwargs):
    with pytest.raises(ValueError):
        context_for(**kwargs)


def test_comments_are_separate_from_facts_and_marked_as_statements():
    context = context_for(interactions=[talk(date(2026, 8, 20), "Обещали оплатить до 15.09.2026")])
    assert [c.text for c in context.comments] == ["Обещали оплатить до 15.09.2026"]
    # The comment is nowhere among the checked data.
    checked = json.dumps(
        [[_ for _ in context.facts], [v.value for v in context.values]], default=str
    )
    assert "Обещали" not in checked
    assert all(c.interaction_id and c.happened_on for c in context.comments)
    # The instruction is what tells the model how to treat them.
    assert "слова сотрудника или клиента, а не проверенные факты" in INSTRUCTION


def test_a_promise_can_be_traced_to_its_record():
    interactions = [
        talk(date(2026, 7, 1), "Звонок без ответа", "INT-7"),
        talk(date(2026, 8, 20), "Обещали оплатить до 15.09.2026", "INT-8", channel="письмо"),
    ]
    comments = context_for(interactions=interactions).comments
    assert [c.interaction_id for c in comments] == ["INT-7", "INT-8"]  # chronological
    promise = comments[1]
    assert promise.happened_on == date(2026, 8, 20) and promise.channel == "письмо"
    assert "ID взаимодействия для комментария" in INSTRUCTION


def test_older_comments_are_counted_not_sent_and_long_ones_are_cut():
    interactions = [
        talk(DAY - timedelta(days=n), f"Разговор {n}", f"INT-{n}") for n in range(5, 0, -1)
    ]
    interactions.append(talk(DAY, "Я" * 50, "INT-0"))
    limits = ContextLimits(max_comments=3, max_comment_chars=10, include_comments=True)
    context = context_for(interactions=interactions, limits=limits)
    assert context.comments_omitted == 3
    assert [c.interaction_id for c in context.comments] == ["INT-2", "INT-1", "INT-0"]
    last = context.comments[-1]
    assert last.text == "Я" * 10 and last.truncated is True


def test_comments_can_be_turned_off_without_hiding_that_they_exist():
    interactions = [talk(DAY, SECRET, "INT-3")]
    limits = ContextLimits(include_comments=False, max_comments=5)
    context = context_for(interactions=interactions, limits=limits)
    assert context.comments == () and context.comments_omitted == 1
    assert SECRET not in json.dumps(context.as_dict(), ensure_ascii=False)


def test_facts_carry_their_section_source_and_record():
    events = [{"Дата": "2026-05-01", "Тип": "Публикация о намерении", "Номер": "A-1"}]
    context = context_for(snaps=snapshots(events=events))
    by_title = {fact.title: fact for fact in context.facts}
    assert by_title["Сообщение ЕФРСБ"].record_id == "A-1"
    assert by_title["Сообщение ЕФРСБ"].section == "Сообщения ЕФРСБ"
    assert by_title["Статус организации"].value == "действует"
    revenue = [fact for fact in context.facts if fact.title == "Выручка"]
    assert all(fact.period and fact.unit == "RUB" for fact in revenue)
    # Every signal's ground is either a fact or an internal value of this context.
    known = {fact.id for fact in context.facts} | {value.id for value in context.values}
    cited = {fact_id for signal in context.signals for fact_id in signal.fact_ids}
    assert cited <= known


def test_indicators_replace_the_row_values_and_say_what_is_not_confirmed():
    snaps = snapshots()
    indicators = internal_indicators(
        row(last=None),
        DAY,
        payments=[PaymentRow(INN, "1", date(2026, 7, 20), Decimal("10"))],
        periods=(Period(date(2026, 7, 1), date(2026, 8, 20)),),
        history=[DebtSnapshot(INN, date(2026, 8, 1), Decimal("400"))],
        finances=snaps[2],
    )
    context = context_for(line=row(last=None), indicators=indicators, snaps=snaps)
    values = {value.title: value.value for value in context.values}
    assert values["Последний платёж в предоставленном периоде"].endswith(
        "(давность не подтверждена)"
    )
    assert values["Изменение долга за месяц"].startswith("в 2.50 раза")
    assert values["Изменение выручки"].startswith("-40.0%")


def test_missing_data_travels_with_the_context():
    context = context_for(line=row(debt=None, overdue=None, last=None))
    assert context.base_complete is False
    assert context.missing_data
    assert not [value for value in context.values if value.title == "Сумма долга"]


def test_request_is_json_ready_and_carries_both_versions():
    request = build_request(context_for(interactions=[talk(DAY, "Позвонили")]))
    body = json.dumps({"instruction": request.instruction, "context": request.context})
    assert json.loads(body)["context"]["context_version"] == CONTEXT_VERSION
    assert request.instruction_version == INSTRUCTION_VERSION
    assert versions() == f"контекст {CONTEXT_VERSION}, инструкция {INSTRUCTION_VERSION}"
    # Nothing about the package, the user, the run or the files travels with it.
    assert not {"run_id", "owner_id", "files", "package", "stored_path"} & set(request.context)


def test_instruction_forbids_changing_the_priority_and_inventing_facts():
    for rule in (
        "Не меняй его",
        "Не добавляй суммы, даты, события и выводы",
        "Не называй обещание нарушенным",
        "не называй процессуальные сроки",
    ):
        assert rule in INSTRUCTION
