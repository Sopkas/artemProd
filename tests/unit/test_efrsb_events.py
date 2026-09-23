"""S3-06: how a message of the bankruptcy register is read (docs/efrsb-events.md).

The decisions under test: an unknown type is «check it», a closed case is history and
raises nothing, and no message — whatever its type — declares this company bankrupt,
because the register answers by INN and the company is often the creditor.
"""

from datetime import UTC, datetime

import pytest

from claims_assistant.domain.efrsb import EventKind, classify, label, raises_priority
from claims_assistant.domain.external import FactKind
from claims_assistant.domain.scoring import Priority, assess
from claims_assistant.infrastructure.checko import bankruptcy

INN = "1234567894"
NOW = datetime(2026, 9, 23, tzinfo=UTC)


def record(name, code=None, day="2026-05-01", number="A-1"):
    return {"Дата": day, "Тип": code, "ТипНаим": name, "НомерДела": number, "GUID": "G-1"}


def snapshot(records):
    return bankruptcy.project_bankruptcy(INN, records, NOW, complete=True, unreadable=0)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Решение о признании должника банкротом", EventKind.PROCEDURE),
        ("Определение о введении наблюдения", EventKind.PROCEDURE),
        ("Сообщение о намерении обратиться в суд с заявлением о банкротстве", EventKind.INTENTION),
        ("Сведения о судебном акте", EventKind.CASE),
        ("Сведения о собрании кредиторов", EventKind.CASE),
        ("Сведения о получении требования кредитора", EventKind.CASE),
        ("Объявление о проведении торгов", EventKind.CASE),
        ("Прекращение производства по делу о банкротстве", EventKind.CLOSURE),
        ("Об отмене ранее опубликованного сообщения", EventKind.CLOSURE),
        ("Об отказе в признании должника банкротом", EventKind.CLOSURE),
        # Both found by A on #52 — and both from the customer's own trade.
        ("Сообщение об отказе от исполнения договора", EventKind.CASE),
        ("Сообщение о завершении конкурсного производства", EventKind.PROCEDURE),
        ("Нечто, чего мы раньше не видели", EventKind.OTHER),
    ],
)
def test_the_kind_of_a_message_is_read_from_its_russian_name(name, expected):
    assert classify(None, name) is expected
    assert label(classify(None, name))


def test_a_known_machine_code_wins_over_the_name():
    assert classify("ArbitralDecree", "что угодно") is EventKind.CASE
    assert classify("СовершенноНовыйКод", "Прекращение производства") is EventKind.CLOSURE


def test_only_a_closed_case_stops_raising_the_priority():
    assert raises_priority(EventKind.OTHER) is True  # unknown means «check it»
    assert raises_priority(EventKind.PROCEDURE) is True
    assert raises_priority(EventKind.CLOSURE) is False


def test_a_closed_case_stays_as_evidence_but_fires_nothing():
    snap = snapshot([record("Прекращение производства по делу")])
    (fact,) = snap.facts
    assert fact.kind is FactKind.BANKRUPTCY_CLOSED
    assert snap.evidence[0].record_id == "G-1"  # still linked to the register
    assert any("прекращённых делах" in reason for reason in snap.missing)
    assessment = assess(INN, [snap])
    assert not [signal for signal in assessment.signals if "bankruptcy" in signal.code]


def test_a_live_message_still_raises_and_says_the_role_is_unknown():
    snap = snapshot([record("Сведения о судебном акте", "ArbitralDecree")])
    (fact,) = snap.facts
    assert fact.kind is FactKind.BANKRUPTCY_EVENT
    assert any("Роль организации" in reason for reason in snap.missing)
    assessment = assess(INN, [snap])
    codes = {signal.code for signal in assessment.signals}
    assert "unresolved_bankruptcy_event" in codes
    # The rules never call it a confirmed procedure: that comes from the company's status.
    assert "confirmed_procedure" not in codes
    assert assessment.priority is Priority.HIGH


def test_a_message_about_a_procedure_is_still_not_a_confirmed_procedure():
    """Even «признан банкротом» may be someone else's case: the INN is only mentioned."""
    snap = snapshot([record("Решение о признании должника банкротом")])
    assessment = assess(INN, [snap])
    assert {signal.code for signal in assessment.signals} == {"unresolved_bankruptcy_event"}
    assert assessment.priority is Priority.HIGH


def test_closed_and_live_messages_together_keep_the_live_one():
    snap = snapshot(
        [
            record("Прекращение производства по делу", number="A-1"),
            record("Сведения о собрании кредиторов", number="A-2", day="2026-06-01"),
        ]
    )
    kinds = [fact.kind for fact in snap.facts]
    assert kinds == [FactKind.BANKRUPTCY_CLOSED, FactKind.BANKRUPTCY_EVENT]
    assert len(snap.evidence) == 2
    assert {signal.code for signal in assess(INN, [snap]).signals} == {
        "unresolved_bankruptcy_event"
    }


def test_a_refusal_to_perform_a_contract_is_a_live_event_not_a_closed_case():
    """«Отказ от исполнения договора» is about a lease — exactly our customer's business —
    and silencing it because of the word «отказ» would hide the thing that matters."""
    snap = snapshot([record("Сообщение об отказе от исполнения договора")])
    assert snap.facts[0].kind is FactKind.BANKRUPTCY_EVENT
    assert {s.code for s in assess(INN, [snap]).signals} == {"unresolved_bankruptcy_event"}


def test_the_end_of_a_bankruptcy_is_not_history():
    """After «завершение конкурсного производства» the debtor is struck off the register:
    the strongest signal there is, and the word «завершение» must not bury it."""
    snap = snapshot([record("Сообщение о завершении конкурсного производства")])
    assert snap.facts[0].kind is FactKind.BANKRUPTCY_EVENT
    assert any("Роль организации" in reason for reason in snap.missing)
