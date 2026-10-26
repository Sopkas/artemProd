"""S7-01 (presentation): the overdue report from the package to the report and the card.

The file is the customer's own 1C print, and it names no INN — the whole path here is
about the link by company name holding all the way: through the package, the saved import
step, the report's sheets and the card of one company. Values are synthetic.
"""

import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from openpyxl import Workbook
from openpyxl.styles import Alignment

from claims_assistant.application.analysis_queue import RunOutcome
from claims_assistant.application.check_package import (
    accept_counterparties,
    accept_debt_report,
)
from claims_assistant.application.internal_context import internal_context
from claims_assistant.domain.analysis import FileKind, RunStatus
from claims_assistant.domain.external import DataMode
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage
from claims_assistant.presentation.telegram.card import format_card
from tests.unit.test_analysis_pipeline import OWNER, guard, pipeline, sheet_rows

DAY = date(2026, 9, 1)
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
INN_1, INN_2 = "7707083893", "7710140679"
# The demo provider calls every company the same, and two companies of one name are
# exactly the case where contracts must NOT be attributed (tests/unit/test_contract_link).
# So the source here names them apart, as a real one would.
NAMES = {INN_1: "ООО «Ромашка»", INN_2: "АО «Василёк»"}
DEMO_NAME = NAMES[INN_1]


class NamingProvider:
    """The demo source with a name of its own for each company."""

    def __init__(self):
        self._inner = DemoCompanyDataProvider()

    async def fetch(self, request):
        snapshots = await self._inner.fetch(request)
        return tuple(_renamed(snapshot, NAMES.get(request.inn)) for snapshot in snapshots)


def _renamed(snapshot, name):
    from dataclasses import replace

    from claims_assistant.domain.external import FactKind

    if name is None:
        return snapshot
    facts = tuple(
        replace(fact, value=name) if fact.kind is FactKind.COMPANY_NAME else fact
        for fact in snapshot.facts
    )
    return replace(snapshot, facts=facts)


def debt_report(counterparties=None) -> bytes:
    """The print as 1C lays it out: three levels in one column, told apart by indent."""
    counterparties = counterparties or [
        (DEMO_NAME, [("Дог-1", 307, 1_200_000.0, "Экскаватор"), ("Дог-2", 30, 250_000.0, "Кран")])
    ]
    book = Workbook()
    sheet = book.active
    rows: list[tuple[int, list]] = [
        (0, ["Отчет по просроченным лизинговым платежам на дату"]),
        (0, ["Точка продаж", None, "Предмет лизинга", "Сумма", "Дней"]),
        (0, ["Контрагент"]),
        (0, ["Договор лизинга"]),
        (0, ["Департамент регионального развития"]),
    ]
    for name, contracts in counterparties:
        rows.append((2, [name]))
        for contract, days, amount, subject in contracts:
            rows.append((4, [contract, None, subject, amount, days]))
    rows.append((0, ["Итого", None, None, 1_450_000.0, None]))
    for indent, values in rows:
        sheet.append(values)
        sheet.cell(row=sheet.max_row, column=1).alignment = Alignment(indent=indent)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def deps(tmp_path):
    return {
        "repository": InMemoryAnalysisRepository(),
        "files": LocalFileStorage(tmp_path / "uploads"),
        "reader": OpenpyxlSheetReader(),
    }


async def package(deps, report=None):
    accepted = await accept_counterparties(
        OWNER,
        DAY,
        DataMode.DEMO,
        build_counterparties_template(),
        repository=deps["repository"],
        files=deps["files"],
        reader=deps["reader"],
    )
    result = await accept_debt_report(
        OWNER,
        accepted.run.id,
        report if report is not None else debt_report(),
        repository=deps["repository"],
        files=deps["files"],
        reader=deps["reader"],
    )
    return result


async def finish(deps, run):
    """The worker is what marks a run finished; the card only looks at finished ones."""
    await deps["repository"].transition(OWNER, run.id, RunStatus.QUEUED)
    await deps["repository"].claim_next()
    await deps["repository"].finish(run.id, RunOutcome(RunStatus.COMPLETED))


async def run_check(deps):
    run = (await package(deps)).run
    outcome = await pipeline(deps["files"], deps["repository"], guard(NamingProvider())).process(
        run
    )
    artifact = await deps["repository"].get_report(OWNER, run.id)
    return outcome, deps["files"].read(artifact.stored_path)


async def test_the_report_is_attached_to_the_package_as_its_own_kind(deps):
    result = await package(deps)
    assert result.rows == 1  # one counterparty came out of the print
    run = await deps["repository"].get_run(OWNER, result.run.id)
    kinds = [file.kind for file in run.files]
    assert kinds == [FileKind.COUNTERPARTIES, FileKind.DEBT_REPORT]


async def test_the_contracts_reach_the_report_and_the_priorities_sheet(deps):
    _, data = await run_check(deps)
    contracts = [row for row in sheet_rows(data, "Договоры") if row[0]]
    assert [(row[0], row[2], row[3]) for row in contracts] == [
        (INN_1, "Дог-1", 307),  # worst overdue first
        (INN_1, "Дог-2", 30),
    ]
    assert contracts[0][4] == Decimal("1200000.00") and contracts[0][5] == "Экскаватор"
    priorities = {row[0]: row for row in sheet_rows(data, "Приоритеты")}
    assert priorities[INN_1][10] == 2 and priorities[INN_1][11] == 307
    # The other company has no contracts in the report: empty, not zero — «не знаем».
    assert priorities[INN_2][10] is None and priorities[INN_2][11] is None


async def test_about_the_check_says_what_the_report_gave(deps):
    """It said «не используется; пригодных строк этого вида: 0» next to two contracts."""
    _, data = await run_check(deps)
    about = " ".join(str(c) for row in sheet_rows(data, "О проверке") for c in row if c)
    assert (
        "Отчёт по договорам: использован — договоры в карточке и на листе «Договоры»; договоров: 2"
    ) in about


async def test_a_name_that_matches_nobody_is_named_in_the_quality_sheet(deps):
    report = debt_report([("ООО «Совсем другая»", [("Дог-9", 10, 1.0, "Станок")])])
    run = (await package(deps, report)).run
    await pipeline(deps["files"], deps["repository"], guard(NamingProvider())).process(run)
    artifact = await deps["repository"].get_report(OWNER, run.id)
    data = deps["files"].read(artifact.stored_path)
    quality = " ".join(str(c) for row in sheet_rows(data, "Качество данных") for c in row if c)
    assert "не отнесены ни к одной организации" in quality
    contracts = sheet_rows(data, "Договоры")
    assert contracts == [("Договоры не приложены", None, None, None, None, None, None, None)]


async def test_a_company_printed_twice_in_one_report_keeps_all_its_contracts(deps):
    """30.09.2026: the second entry of a company in the same file lost its contracts, and
    «Качество данных» said the company was «in two reports» about a single file."""
    report = debt_report(
        [
            (DEMO_NAME, [("Дог-1", 307, 1_200_000.0, "Экскаватор")]),
            (DEMO_NAME, [("Дог-3", 40, 300_000.0, "Бульдозер")]),
        ]
    )
    accepted = await package(deps, report)
    assert (accepted.rows, accepted.contracts) == (1, 2)  # what the upload summary says
    await pipeline(deps["files"], deps["repository"], guard(NamingProvider())).process(accepted.run)
    artifact = await deps["repository"].get_report(OWNER, accepted.run.id)
    data = deps["files"].read(artifact.stored_path)
    contracts = [(row[0], row[2], row[4]) for row in sheet_rows(data, "Договоры") if row[0]]
    assert contracts == [
        (INN_1, "Дог-1", Decimal("1200000.00")),
        (INN_1, "Дог-3", Decimal("300000.00")),
    ]
    quality = " ".join(str(c) for row in sheet_rows(data, "Качество данных") for c in row if c)
    assert "двух отчётах" not in quality


async def test_a_resumed_check_keeps_the_contracts_without_reading_the_file_again(deps):
    """The import step carries them, like every other file of the package (S3-03)."""
    run = (await package(deps)).run
    await pipeline(deps["files"], deps["repository"], guard(NamingProvider())).process(run)

    class Blind:
        def read(self, path):
            raise AssertionError("the package must not be read again")

        def save(self, run_id, data):
            return deps["files"].save(run_id, data)

        def remove(self, path):
            deps["files"].remove(path)

    blind = Blind()
    outcome = await pipeline(blind, deps["repository"], guard(NamingProvider())).process(
        await deps["repository"].get_run(OWNER, run.id)
    )
    assert outcome.status.value in ("completed", "partial")


async def test_the_card_shows_the_contracts_of_this_company(deps):
    from claims_assistant.application.check_company import check_company

    run = (await package(deps)).run
    await pipeline(deps["files"], deps["repository"], guard(NamingProvider())).process(run)
    await finish(deps, run)
    check = await check_company(INN_1, guard(NamingProvider()))
    context = await internal_context(
        OWNER,
        INN_1,
        deps["repository"],
        deps["files"],
        deps["reader"],
        steps=deps["repository"],
    )
    assert [contract.name for contract in context.contracts] == ["Дог-1", "Дог-2"]
    card = format_card(check, context)
    assert "Договоры с просрочкой: 2" in card
    assert "Дог-1 · 307 дн." in card and "Экскаватор" in card


async def test_the_card_shows_nothing_when_the_check_has_no_saved_link(deps):
    """The card never works the link out itself: it would not see the namesakes the
    report saw, and the call is made from the card (review B on #58)."""
    run = (await package(deps)).run
    await pipeline(deps["files"], deps["repository"], guard(NamingProvider())).process(run)
    await finish(deps, run)
    context = await internal_context(
        OWNER, INN_1, deps["repository"], deps["files"], deps["reader"], steps=None
    )
    assert context.contracts == ()


async def test_the_card_does_not_show_a_namesakes_contracts(deps):
    """The report refuses to attribute a name two companies share; so does the card."""
    from dataclasses import replace as _replace

    run = (await package(deps)).run
    same = {INN_1: "ООО «Ромашка»", INN_2: "ООО Ромашка"}

    class Namesakes(NamingProvider):
        async def fetch(self, request):
            snapshots = await self._inner.fetch(request)
            return tuple(_renamed(snapshot, same.get(request.inn)) for snapshot in snapshots)

    await pipeline(deps["files"], deps["repository"], guard(Namesakes())).process(run)
    await finish(deps, run)
    context = await internal_context(
        OWNER, INN_1, deps["repository"], deps["files"], deps["reader"], steps=deps["repository"]
    )
    assert context.contracts == ()
    assert _replace  # the import is used by the helper above


async def test_a_resumed_check_leaves_the_card_the_link_its_report_was_built_with(deps):
    """The link was saved before the report and written once: a pass cut short with
    «Василёк» unnamed (a transient failure) left the card without its contracts, while the
    resumed pass named it and gave them to it in the report."""
    from tests.unit.test_analysis_pipeline import CountingProvider

    report = debt_report([("АО «Василёк»", [("Дог-B", 300, 300_000.0, "Кран")])])
    run = (await package(deps, report)).run
    provider = CountingProvider(NamingProvider(), failing={INN_2})
    cut_short = pipeline(deps["files"], deps["repository"], guard(provider))

    def died(_report):
        raise RuntimeError("the process died while the report was being built")

    cut_short._build_report = died
    with pytest.raises(RuntimeError):
        await cut_short.process(run)

    provider.failing.clear()
    resumed = await pipeline(deps["files"], deps["repository"], guard(provider)).process(run)
    assert resumed.status in (RunStatus.COMPLETED, RunStatus.PARTIAL)
    data = deps["files"].read((await deps["repository"].get_report(OWNER, run.id)).stored_path)
    assert [(row[0], row[2]) for row in sheet_rows(data, "Договоры") if row[0]] == [
        (INN_2, "Дог-B")
    ]
    await finish(deps, run)
    context = await internal_context(
        OWNER, INN_2, deps["repository"], deps["files"], deps["reader"], steps=deps["repository"]
    )
    assert [contract.name for contract in context.contracts] == ["Дог-B"]


async def test_a_company_the_source_does_not_name_is_linked_by_the_owners_own_name(deps):
    """S7-02: the source names no ИП, so their contracts were never linked. The name of the
    «Контрагенты» row stands in, and the match is put up for a glance."""
    from dataclasses import replace as _replace

    from claims_assistant.domain.external import FactKind

    class Silent(NamingProvider):
        async def fetch(self, request):
            snapshots = await super().fetch(request)
            if request.inn != INN_2:
                return snapshots
            return tuple(
                _replace(s, facts=tuple(f for f in s.facts if f.kind is not FactKind.COMPANY_NAME))
                for s in snapshots
            )

    # The sample row of INN_2 is called «ООО «Синтетический контрагент»» in «Контрагенты».
    report = debt_report([("Синтетический контрагент, ООО", [("Дог-S", 45, 1.0, "Станок")])])
    run = (await package(deps, report)).run
    await pipeline(deps["files"], deps["repository"], guard(Silent())).process(run)
    data = deps["files"].read((await deps["repository"].get_report(OWNER, run.id)).stored_path)
    assert [(row[0], row[2]) for row in sheet_rows(data, "Договоры") if row[0]] == [
        (INN_2, "Дог-S")
    ]
    quality = " ".join(str(c) for row in sheet_rows(data, "Качество данных") for c in row if c)
    assert "по названию из файла «Контрагенты»" in quality


INN_ELSEWHERE = "7736050003"  # a valid INN that is not in the check


def debt_report_with_inn(counterparties) -> bytes:
    """The other variant of the print: an «ИНН» column next to the name."""
    book = Workbook()
    sheet = book.active
    rows: list[tuple[int, list]] = [
        (0, ["Отчет по просроченным лизинговым платежам на дату"]),
        (0, ["Точка продаж", "ИНН", "Предмет лизинга", "Сумма", "Дней"]),
        (0, ["Контрагент"]),
        (0, ["Договор лизинга"]),
        (0, ["Департамент регионального развития"]),
    ]
    for name, inn, contracts in counterparties:
        rows.append((2, [name, inn]))
        for contract, days, amount, subject in contracts:
            rows.append((4, [contract, None, subject, amount, days]))
    for indent, values in rows:
        sheet.append(values)
        sheet.cell(row=sheet.max_row, column=1).alignment = Alignment(indent=indent)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


async def test_an_inn_printed_in_the_report_keeps_a_namesakes_contracts_off_the_check(deps):
    """The report prints another INN under the name of a company of the check: the
    contracts went to that company, silently, because only the name was looked at."""
    report = debt_report_with_inn(
        [(DEMO_NAME, INN_ELSEWHERE, [("Дог-Ч", 400, 900_000.0, "Тягач")])]
    )
    run = (await package(deps, report)).run
    await pipeline(deps["files"], deps["repository"], guard(NamingProvider())).process(run)
    data = deps["files"].read((await deps["repository"].get_report(OWNER, run.id)).stored_path)
    assert not any(row[0] == INN_1 for row in sheet_rows(data, "Договоры"))
    quality = " ".join(str(c) for row in sheet_rows(data, "Качество данных") for c in row if c)
    assert "не отнесены ни к одной организации" in quality


async def test_two_reports_naming_two_inns_alike_are_not_one_client_printed_twice(deps):
    """A name taken by the first report was dropped from the second as «the same client»,
    although the two reports print two different INNs for it."""
    run = (
        await package(
            deps,
            debt_report_with_inn([(DEMO_NAME, INN_ELSEWHERE, [("Дог-Ч", 400, 1.0, "Тягач")])]),
        )
    ).run
    await accept_debt_report(
        OWNER,
        run.id,
        debt_report_with_inn([(DEMO_NAME, INN_1, [("Дог-1", 307, 1.0, "Экскаватор")])]),
        repository=deps["repository"],
        files=deps["files"],
        reader=deps["reader"],
    )
    run = await deps["repository"].get_run(OWNER, run.id)
    await pipeline(deps["files"], deps["repository"], guard(NamingProvider())).process(run)
    data = deps["files"].read((await deps["repository"].get_report(OWNER, run.id)).stored_path)
    assert [(row[0], row[2]) for row in sheet_rows(data, "Договоры") if row[0]] == [
        (INN_1, "Дог-1")
    ]
    quality = " ".join(str(c) for row in sheet_rows(data, "Качество данных") for c in row if c)
    assert "двух отчётах" not in quality


async def two_reports(deps, first: bytes, second: bytes) -> bytes:
    """A package with two overdue reports, attached in this order, checked to the end."""
    run = (await package(deps, first)).run
    await accept_debt_report(
        OWNER,
        run.id,
        second,
        repository=deps["repository"],
        files=deps["files"],
        reader=deps["reader"],
    )
    run = await deps["repository"].get_run(OWNER, run.id)
    await pipeline(deps["files"], deps["repository"], guard(NamingProvider())).process(run)
    return deps["files"].read((await deps["repository"].get_report(OWNER, run.id)).stored_path)


@pytest.mark.parametrize("without_inn_first", [True, False])
async def test_a_report_without_inns_does_not_hand_a_namesake_the_contracts_another_report_ties(
    deps, without_inn_first
):
    """The same client in two variants of the print, one without the «ИНН» column and one
    with an INN that is not in the check. Attached in this order, the entry without an INN
    was kept, the INN one dropped as its repeat — and the name alone gave the contract to
    the namesake of the check. One file holding both entries never did."""
    plain = debt_report([(DEMO_NAME, [("Дог-Ч", 400, 900_000.0, "Тягач")])])
    with_inn = debt_report_with_inn(
        [(DEMO_NAME, INN_ELSEWHERE, [("Дог-Ч", 400, 900_000.0, "Тягач")])]
    )
    first, second = (plain, with_inn) if without_inn_first else (with_inn, plain)
    data = await two_reports(deps, first, second)
    assert not any(row[0] == INN_1 for row in sheet_rows(data, "Договоры"))
    quality = " ".join(str(c) for row in sheet_rows(data, "Качество данных") for c in row if c)
    assert "не отнесены ни к одной организации" in quality


async def test_an_entry_without_an_inn_of_a_name_two_reports_print_with_two_inns_is_ambiguous(
    deps,
):
    """Another report prints the name under two INNs: the entry without one could be either
    of them, so it goes to nobody — neither silently dropped nor given to the namesake."""
    data = await two_reports(
        deps,
        debt_report([(DEMO_NAME, [("Дог-Х", 90, 1.0, "Кран")])]),
        debt_report_with_inn(
            [
                (DEMO_NAME, INN_1, [("Дог-1", 307, 1.0, "Экскаватор")]),
                (DEMO_NAME, INN_ELSEWHERE, [("Дог-Ч", 400, 1.0, "Тягач")]),
            ]
        ),
    )
    assert [(row[0], row[2]) for row in sheet_rows(data, "Договоры") if row[0]] == [
        (INN_1, "Дог-1")
    ]
    quality = " ".join(str(c) for row in sheet_rows(data, "Качество данных") for c in row if c)
    assert "такое название носят несколько организаций" in quality
