"""S4-04: the INN card shows the owner's own data on the company from a finished check."""

from datetime import date
from decimal import Decimal

from claims_assistant.application.analysis_queue import RunOutcome
from claims_assistant.application.check_company import check_company
from claims_assistant.application.check_package import accept_counterparties, accept_ledger
from claims_assistant.application.internal_context import internal_context
from claims_assistant.domain.analysis import FileKind, RunStatus
from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.external import DataMode, Period, Section
from claims_assistant.domain.indicators import DebtTrend, PaymentStatus
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.ledgers import (
    build_debt_history_workbook,
    build_interactions_workbook,
    build_payments_workbook,
)
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage
from claims_assistant.presentation.telegram.card import format_card

OWNER = 42
DAY = date(2026, 9, 1)
INN = "7707083893"
OTHER = "7710140679"
ROWS = (
    CounterpartyRow(
        inn=INN,
        name="ООО «Синтетический контрагент»",
        cutoff_date=DAY,
        debt=Decimal("200.00"),
        overdue_days=45,
        last_payment_date=date(2026, 7, 15),
    ),
    CounterpartyRow(inn=OTHER, cutoff_date=DAY, debt=Decimal("5.00")),
)


async def finished_check(deps, rows=ROWS, *, with_files: bool = True, finish: bool = True):
    result = await accept_counterparties(
        OWNER, DAY, DataMode.DEMO, build_counterparties_template(rows), **deps
    )
    run_id = result.run.id
    if with_files:
        await accept_ledger(
            OWNER,
            run_id,
            FileKind.PAYMENTS,
            build_payments_workbook([[INN, "P-1", date(2026, 7, 15), 50.0]]),
            coverage=Period(date(2026, 6, 1), DAY),
            **deps,
        )
        await accept_ledger(
            OWNER,
            run_id,
            FileKind.DEBT_HISTORY,
            build_debt_history_workbook([[INN, date(2026, 8, 1), 100.0]]),
            coverage=None,
            **deps,
        )
        await accept_ledger(
            OWNER,
            run_id,
            FileKind.INTERACTIONS,
            build_interactions_workbook(
                [
                    [INN, "I-2", date(2026, 8, 20), "Обещали оплатить.", "телефон"],
                    [INN, "I-1", date(2026, 8, 1), "Направлена претензия.", None],
                ]
            ),
            coverage=None,
            **deps,
        )
    repository = deps["repository"]
    await repository.transition(OWNER, run_id, RunStatus.QUEUED)
    await repository.claim_next()
    if finish:
        await repository.finish(run_id, RunOutcome(RunStatus.COMPLETED))
    return run_id


def deps_for(tmp_path):
    return dict(
        repository=InMemoryAnalysisRepository(),
        files=LocalFileStorage(tmp_path / "uploads"),
        reader=OpenpyxlSheetReader(),
    )


async def test_context_comes_from_the_newest_finished_check_that_names_the_inn(tmp_path):
    deps = deps_for(tmp_path)
    run_id = await finished_check(deps)
    context = await internal_context(OWNER, INN, **deps)
    assert context is not None and context.run.id == run_id
    assert context.row.debt == Decimal("200.00")
    assert context.indicators.payment.status is PaymentStatus.CONFIRMED
    assert context.indicators.payment.last_payment == date(2026, 7, 15)
    assert context.indicators.debt.trend is DebtTrend.COMPUTED
    assert context.indicators.debt.ratio == Decimal("2")
    assert [i.interaction_id for i in context.interactions] == ["I-1", "I-2"]
    assert context.files == (
        "Контрагенты",
        "Платежи за 01.06.2026–01.09.2026",
        "История долга",
        "Взаимодействия",
    )
    # A company of the package without optional data still gets a context.
    other = await internal_context(OWNER, OTHER, **deps)
    assert other is not None and other.interactions == ()
    assert other.indicators.debt.trend is DebtTrend.NO_BASE


async def test_no_context_without_a_finished_check_or_for_another_owner(tmp_path):
    deps = deps_for(tmp_path)
    assert await internal_context(OWNER, INN, **deps) is None
    await finished_check(deps, finish=False)  # still running
    assert await internal_context(OWNER, INN, **deps) is None
    await finished_check(deps)
    assert await internal_context(OWNER, INN, **deps) is not None
    assert await internal_context(99, INN, **deps) is None  # another user sees nothing
    assert await internal_context(OWNER, "1234567894", **deps) is None  # not in the package


async def test_card_shows_files_indicators_chronology_and_gaps(tmp_path):
    deps = deps_for(tmp_path)
    await finished_check(deps)
    check = await check_company(INN, DemoCompanyDataProvider())
    finances = next(s for s in check.snapshots if s.section is Section.FINANCES)
    context = await internal_context(OWNER, INN, finances=finances, **deps)
    text = format_card(check, context)
    assert "Внутренние данные — проверка от 01.09.2026" in text
    assert (
        "Файлы: Контрагенты; Платежи за 01.06.2026–01.09.2026; История долга; Взаимодействия"
        in text
    )
    assert "Долг: 200,00 ₽ на 01.09.2026" in text and "Просрочка: 45 дн." in text
    assert "Давность платежа: последний платёж 15.07.2026, 48 дн. назад (подтверждено)" in text
    assert "Долг за месяц: 100,00 ₽ (01.08.2026) → 200,00 ₽ (01.09.2026), ×2,00" in text
    assert "Взаимодействия: 2, последние:" in text
    assert "01.08.2026: Направлена претензия." in text
    assert "20.08.2026 (телефон): Обещали оплатить." in text
    assert "Не хватает" not in text
    # Without the optional files the card says so and names the gaps.
    other = await internal_context(OWNER, OTHER, **deps)
    text = format_card(check, other)
    assert "Взаимодействия: файл не загружен." in text
    assert "Долг за месяц: нет среза месяц назад" in text
    # What counts as missing is the domain's call (S4-06): the card only relays it.
    assert [line for line in text.splitlines() if line.startswith("  Не хватает:")] == [
        f"  Не хватает: {note}" for note in other.indicators.missing
    ]


def test_card_without_context_is_unchanged():
    import asyncio

    check = asyncio.run(check_company(INN, DemoCompanyDataProvider()))
    assert "Внутренние данные" not in format_card(check)
