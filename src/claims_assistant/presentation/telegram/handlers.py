import logging
import re
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, ErrorEvent, Message

from claims_assistant.application.analysis_repository import AnalysisRepository, RepositoryError
from claims_assistant.application.check_company import check_company
from claims_assistant.application.check_package import (
    FileStorage,
    LedgerAccepted,
    PackageAccepted,
    PackageConflict,
    accept_counterparties,
    accept_debt_report,
    accept_ledger,
    accept_payments_export,
    latest_run,
    launch_run,
    package_inns,
)
from claims_assistant.application.company_data import CompanyDataProvider
from claims_assistant.application.imports import SheetReader
from claims_assistant.application.internal_context import internal_context
from claims_assistant.application.package_checks import (
    PACKAGE_SHEET,
    PackageIntegrityError,
    review_package,
)
from claims_assistant.application.report_delivery import (
    ReportUnavailable,
    confirm_delivery,
    fetch_report,
)
from claims_assistant.domain.analysis import FileKind
from claims_assistant.domain.external import DataMode, Period, Section
from claims_assistant.domain.inn import InvalidInn, validate_inn
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
from claims_assistant.infrastructure.excel import ledgers
from claims_assistant.infrastructure.excel.counterparties import (
    build_counterparties_template,
    sample_rows,
)
from claims_assistant.infrastructure.excel.ledgers import (
    build_debt_history_template,
    build_interactions_template,
    build_payments_template,
)
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage

from . import texts
from .access import AccessMiddleware
from .card import format_card
from .menu import (
    ADD_DEBT_REPORT,
    ADD_HISTORY,
    ADD_INTERACTIONS,
    ADD_PAYMENTS,
    CANCEL,
    CHECK_INN,
    LAUNCH,
    NEW_CHECK,
    PAYMENTS_EXPORT,
    PAYMENTS_TEMPLATE,
    REPORT,
    STATUS,
    TODAY,
    cancel_menu,
    date_menu,
    launch_menu,
    main_menu,
    payments_source_menu,
)

logger = logging.getLogger(__name__)
_RU_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class InnDialog(StatesGroup):
    waiting_for_inn = State()


class CheckDialog(StatesGroup):
    waiting_for_date = State()
    waiting_for_file = State()
    confirming = State()
    # S4-01: optional files are added from the confirmation step and return to it.
    waiting_for_payments_source = State()
    waiting_for_payments_period = State()
    waiting_for_payments_file = State()
    waiting_for_export_inn = State()
    waiting_for_export_file = State()
    waiting_for_history_file = State()
    waiting_for_interactions_file = State()
    waiting_for_debt_report_file = State()


# A dash or whitespace between the dates; a bare hyphen only after a ДД.ММ.ГГГГ date,
# so ISO dates (2026-06-01) keep their own hyphens.
_PERIOD_SPLIT = re.compile(r"\s*[–—]\s*|\s+-\s+|\s+|(?<=\.\d{4})-(?=\d{2}\.)")


def parse_period(text: str, analysis_date: date) -> Period | None:
    """Two dates «start–end»; the end never passes the analysis date."""
    parts = [part for part in _PERIOD_SPLIT.split(text.strip()) if part]
    if len(parts) != 2:
        return None
    start, end = (parse_user_date(part) for part in parts)
    if start is None or end is None or start > end or end > analysis_date:
        return None
    return Period(start, end)


def parse_user_date(text: str, utc_offset_hours: int = 3) -> date | None:
    """Only unambiguous forms; anything else asks again instead of guessing.

    «Сегодня» is the calendar date in the business timezone (offset from UTC), so a
    user at 00:30 local time does not get yesterday's UTC date.
    """
    text = text.strip()
    if text.casefold() == TODAY.casefold():
        return datetime.now(timezone(timedelta(hours=utc_offset_hours))).date()
    try:
        if _ISO_DATE.fullmatch(text):
            return date.fromisoformat(text)
        match = _RU_DATE.fullmatch(text)
        if match:
            day, month, year = (int(part) for part in match.groups())
            return date(year, month, day)
    except ValueError:
        return None
    return None


def _template_samples(rows) -> tuple[str, ...]:
    """INNs of accepted rows that are the template's examples as sent: every field but the
    cut-off date the same. The same INN with the user's own figures is the user's company."""

    def key(row):
        return (row.inn, row.name, row.debt, row.overdue_days, row.last_payment_date)

    samples = {key(row) for row in sample_rows()}
    return tuple(row.inn for row in rows if key(row) in samples)


def _composition(run, rows: dict) -> tuple[str, ...]:
    """One line per package file, in upload order; row counts come from the dialog state."""
    return tuple(
        texts.composition_line(file.kind, rows.get(file.id, 0), file.coverage) for file in run.files
    )


def create_dispatcher(
    allowed_ids: frozenset[int],
    provider: CompanyDataProvider | None = None,
    repository: AnalysisRepository | None = None,
    files: FileStorage | None = None,
    reader: SheetReader | None = None,
    mode: DataMode = DataMode.DEMO,
    business_utc_offset_hours: int = 3,
) -> Dispatcher:
    if provider is None:
        provider = DemoCompanyDataProvider()
    if repository is None:
        repository = InMemoryAnalysisRepository()
    if files is None:
        files = LocalFileStorage(Path("data/uploads"))
    if reader is None:
        reader = OpenpyxlSheetReader()
    # Dialog state lives in memory: it is short and may be lost on restart (S0-03, remark 2).
    dispatcher = Dispatcher()
    router = Router(name="shell")
    router.message.outer_middleware(AccessMiddleware(allowed_ids))
    in_check = StateFilter(CheckDialog.waiting_for_file, CheckDialog.confirming)

    # --- documents inside the check dialog come first, before the generic refusal ---

    @router.message(in_check, F.document)
    async def receive_package(message: Message, state: FSMContext, bot: Bot) -> None:
        document = message.document
        name = (document.file_name or "").lower()
        if not name.endswith(".xlsx"):
            await message.answer(texts.FILE_NOT_XLSX, reply_markup=cancel_menu())
            return
        if document.file_size is None or document.file_size > texts.MAX_UPLOAD_BYTES:
            await message.answer(texts.FILE_TOO_LARGE, reply_markup=cancel_menu())
            return
        data = await state.get_data()
        analysis_date = date.fromisoformat(data["analysis_date"])
        buffer = BytesIO()
        await bot.download(document, destination=buffer)
        try:
            result = await accept_counterparties(
                message.from_user.id,
                analysis_date,
                mode,
                buffer.getvalue(),
                repository=repository,
                files=files,
                reader=reader,
                run_id=data.get("run_id"),
            )
        except Exception as exc:
            logger.error("package_failed error_type=%s", type(exc).__name__)
            await state.clear()
            await message.answer(texts.CHECK_FAILED, reply_markup=main_menu())
            return
        if isinstance(result, PackageConflict):
            await message.answer(texts.package_conflict(result.reason), reply_markup=launch_menu())
            return
        if not isinstance(result, PackageAccepted):
            await message.answer(texts.package_rejected(result.issues), reply_markup=cancel_menu())
            return
        rows = dict(data.get("rows", {}))
        rows[result.file.id] = len(result.rows)
        composition = _composition(result.run, rows)
        await state.update_data(run_id=result.run.id, rows=rows)
        await state.set_state(CheckDialog.confirming)
        await message.answer(
            texts.package_summary(
                result.run.analysis_date,
                len(result.rows),
                result.issues,
                result.duplicate,
                composition,
                samples=_template_samples(result.rows),
            ),
            reply_markup=launch_menu(),
        )

    # Files made from our template are .xlsx; the customer's own prints from 1C come in
    # either format (S4-05).
    _SPREADSHEET = (".xlsx", ".xls")

    ledger_states = {
        CheckDialog.waiting_for_payments_file: FileKind.PAYMENTS,
        CheckDialog.waiting_for_history_file: FileKind.DEBT_HISTORY,
        CheckDialog.waiting_for_interactions_file: FileKind.INTERACTIONS,
        CheckDialog.waiting_for_debt_report_file: FileKind.DEBT_REPORT,
    }

    @router.message(StateFilter(*ledger_states), F.document)
    async def receive_ledger(message: Message, state: FSMContext, bot: Bot) -> None:
        kind = ledger_states[await state.get_state()]
        document = message.document
        name = (document.file_name or "").lower()
        # The overdue report is printed by 1C and comes in the old format too (S7-01).
        allowed = (".xlsx", ".xls") if kind is FileKind.DEBT_REPORT else (".xlsx",)
        if not name.endswith(allowed):
            await message.answer(texts.FILE_NOT_XLSX, reply_markup=cancel_menu())
            return
        if document.file_size is None or document.file_size > texts.MAX_UPLOAD_BYTES:
            await message.answer(texts.FILE_TOO_LARGE, reply_markup=cancel_menu())
            return
        data = await state.get_data()
        if kind is FileKind.DEBT_REPORT:
            await _receive_debt_report(message, state, bot, document, data)
            return
        coverage = None
        if kind is FileKind.PAYMENTS:
            coverage = Period(
                date.fromisoformat(data["period_start"]), date.fromisoformat(data["period_end"])
            )
        buffer = BytesIO()
        await bot.download(document, destination=buffer)
        try:
            result = await accept_ledger(
                message.from_user.id,
                data["run_id"],
                kind,
                buffer.getvalue(),
                coverage=coverage,
                repository=repository,
                files=files,
                reader=reader,
            )
        except Exception as exc:
            logger.error("ledger_failed error_type=%s", type(exc).__name__)
            await state.clear()
            await message.answer(texts.CHECK_FAILED, reply_markup=main_menu())
            return
        if not isinstance(result, LedgerAccepted):
            await message.answer(
                texts.ledger_rejected(kind, result.issues), reply_markup=cancel_menu()
            )
            return
        rows = dict(data.get("rows", {}))
        rows[result.file.id] = result.rows
        await state.update_data(rows=rows)
        await state.set_state(CheckDialog.confirming)
        composition = _composition(result.run, rows)
        await message.answer(
            texts.ledger_summary(kind, result.rows, result.issues, result.duplicate, composition),
            reply_markup=launch_menu(),
        )

    async def _receive_debt_report(message, state, bot, document, data) -> None:
        buffer = BytesIO()
        await bot.download(document, destination=buffer)
        try:
            result = await accept_debt_report(
                message.from_user.id,
                data["run_id"],
                buffer.getvalue(),
                repository=repository,
                files=files,
                reader=reader,
            )
        except Exception as exc:
            logger.error("debt_report_failed error_type=%s", type(exc).__name__)
            await state.clear()
            await message.answer(texts.CHECK_FAILED, reply_markup=main_menu())
            return
        if not isinstance(result, LedgerAccepted):
            await message.answer(
                texts.ledger_rejected(FileKind.DEBT_REPORT, result.issues),
                reply_markup=cancel_menu(),
            )
            return
        rows = dict(data.get("rows", {}))
        rows[result.file.id] = result.rows
        await state.update_data(rows=rows)
        await state.set_state(CheckDialog.confirming)
        await message.answer(
            texts.ledger_summary(
                FileKind.DEBT_REPORT,
                result.rows,
                result.issues,
                result.duplicate,
                _composition(result.run, rows),
                contracts=result.contracts,
            ),
            reply_markup=launch_menu(),
        )

    @router.message(CheckDialog.waiting_for_export_file, F.document)
    async def receive_export_file(message: Message, state: FSMContext, bot: Bot) -> None:
        document = message.document
        # A 1C print arrives in the old format as often as in the new one, and the reader
        # tells them apart by the file's own signature, not by its name (review B on #56).
        if not (document.file_name or "").lower().endswith(_SPREADSHEET):
            await message.answer(texts.FILE_NOT_XLSX, reply_markup=cancel_menu())
            return
        if document.file_size is None or document.file_size > texts.MAX_UPLOAD_BYTES:
            await message.answer(texts.FILE_TOO_LARGE, reply_markup=cancel_menu())
            return
        data = await state.get_data()
        buffer = BytesIO()
        await bot.download(document, destination=buffer)
        try:
            result = await accept_payments_export(
                message.from_user.id,
                data["run_id"],
                buffer.getvalue(),
                inn=data["export_inn"],
                repository=repository,
                files=files,
                reader=reader,
                sheets=ledgers,
            )
        except Exception as exc:
            logger.error("export_failed error_type=%s", type(exc).__name__)
            await state.clear()
            await message.answer(texts.CHECK_FAILED, reply_markup=main_menu())
            return
        if not isinstance(result, LedgerAccepted):
            await message.answer(
                texts.ledger_rejected(FileKind.PAYMENTS, result.issues),
                reply_markup=cancel_menu(),
            )
            return
        rows = dict(data.get("rows", {}))
        rows[result.file.id] = result.rows
        await state.update_data(rows=rows)
        await state.set_state(CheckDialog.confirming)
        composition = _composition(result.run, rows)
        summary = texts.ledger_summary(
            FileKind.PAYMENTS, result.rows, result.issues, result.duplicate, composition
        )
        await message.answer(
            texts.export_summary(result.export, result.rows, result.file.coverage)
            + "\n\n"
            + summary,
            reply_markup=launch_menu(),
        )

    @router.message(F.document | F.photo | F.video | F.audio | F.voice | F.animation | F.video_note)
    async def attachment(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.FILE_UNSUPPORTED, reply_markup=main_menu())

    # --- menu commands leave any dialog ---

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.START, reply_markup=main_menu())

    @router.message(Command("help"))
    @router.message(F.text == "Помощь")
    async def help_command(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.HELP, reply_markup=main_menu())

    @router.message(Command("about"))
    @router.message(F.text == "О сервисе")
    async def about(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.ABOUT, reply_markup=main_menu())

    @router.message(Command("status"))
    @router.message(F.text == STATUS)
    async def status(message: Message, state: FSMContext) -> None:
        await state.clear()
        run = await latest_run(message.from_user.id, repository)
        if run is None:
            await message.answer(texts.STATUS_EMPTY, reply_markup=main_menu())
            return
        has_report = await repository.get_report(message.from_user.id, run.id) is not None
        await message.answer(texts.run_status(run, has_report), reply_markup=main_menu())

    @router.message(Command("report"))
    @router.message(F.text == REPORT)
    async def report(message: Message, state: FSMContext) -> None:
        await state.clear()
        try:
            report_file = await fetch_report(message.from_user.id, repository, files)
        except ReportUnavailable:
            await message.answer(texts.REPORT_UNAVAILABLE, reply_markup=main_menu())
            return
        if report_file is None:
            await message.answer(texts.REPORT_EMPTY, reply_markup=main_menu())
            return
        document = BufferedInputFile(report_file.data, filename=report_file.filename)
        try:
            await message.answer_document(
                document, caption=texts.report_caption(report_file.run), reply_markup=main_menu()
            )
        except Exception as exc:
            # Telegram refused the file: record it, keep the run and the artifact intact.
            logger.error("report_delivery_failed error_type=%s", type(exc).__name__)
            await confirm_delivery(report_file, repository, error="Не удалось отправить файл.")
            await message.answer(texts.REPORT_SEND_FAILED, reply_markup=main_menu())
            return
        try:
            await confirm_delivery(report_file, repository)
        except RepositoryError as exc:
            # The user already has the file; a bookkeeping failure is not their problem.
            logger.error("report_delivery_unrecorded error_type=%s", type(exc).__name__)

    # --- INN card ---

    @router.message(Command("inn"))
    @router.message(F.text == CHECK_INN)
    async def ask_inn(message: Message, state: FSMContext) -> None:
        await state.set_state(InnDialog.waiting_for_inn)
        await message.answer(texts.INN_PROMPT, reply_markup=cancel_menu())

    @router.message(InnDialog.waiting_for_inn, Command("cancel"))
    @router.message(InnDialog.waiting_for_inn, F.text == CANCEL)
    async def cancel_inn(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.INN_CANCELLED, reply_markup=main_menu())

    @router.message(InnDialog.waiting_for_inn, F.text)
    async def receive_inn(message: Message, state: FSMContext) -> None:
        try:
            check = await check_company(message.text or "", provider)
        except InvalidInn as error:
            await message.answer(texts.inn_invalid(str(error)), reply_markup=cancel_menu())
            return
        except Exception as exc:
            # The INN and the provider's message may be private; log only the error type.
            logger.error("check_failed error_type=%s", type(exc).__name__)
            await state.clear()
            await message.answer(texts.CHECK_FAILED, reply_markup=main_menu())
            return
        await state.clear()
        # S4-04: the owner's own files on this company, if a finished check names it.
        internal = None
        try:
            finances = next((s for s in check.snapshots if s.section is Section.FINANCES), None)
            internal = await internal_context(
                message.from_user.id,
                check.inn,
                repository,
                files,
                reader,
                finances=finances,
                steps=repository,
            )
        except Exception as exc:
            # The card is still useful without the internal block; log the type only.
            logger.error("internal_context_failed error_type=%s", type(exc).__name__)
        await message.answer(format_card(check, internal), reply_markup=main_menu())

    # --- new check: date → file → launch ---

    @router.message(Command("check"))
    @router.message(F.text == NEW_CHECK)
    async def new_check(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(CheckDialog.waiting_for_date)
        await message.answer(texts.CHECK_DATE_PROMPT, reply_markup=date_menu())

    optional_steps = StateFilter(
        CheckDialog.waiting_for_payments_source,
        CheckDialog.waiting_for_payments_period,
        CheckDialog.waiting_for_payments_file,
        CheckDialog.waiting_for_export_inn,
        CheckDialog.waiting_for_export_file,
        CheckDialog.waiting_for_history_file,
        CheckDialog.waiting_for_interactions_file,
        CheckDialog.waiting_for_debt_report_file,
    )

    @router.message(optional_steps, Command("cancel"))
    @router.message(optional_steps, F.text == CANCEL)
    async def cancel_ledger(message: Message, state: FSMContext) -> None:
        # Only the optional file is dropped; the draft package stays for confirmation.
        await state.set_state(CheckDialog.confirming)
        await message.answer(texts.LEDGER_CANCELLED, reply_markup=launch_menu())

    @router.message(StateFilter(CheckDialog), Command("cancel"))
    @router.message(StateFilter(CheckDialog), F.text == CANCEL)
    async def cancel_check(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.CHECK_CANCELLED, reply_markup=main_menu())

    @router.message(CheckDialog.waiting_for_date, F.text)
    async def receive_date(message: Message, state: FSMContext) -> None:
        analysis_date = parse_user_date(message.text or "", business_utc_offset_hours)
        if analysis_date is None:
            await message.answer(texts.CHECK_DATE_INVALID, reply_markup=date_menu())
            return
        await state.update_data(analysis_date=analysis_date.isoformat())
        await state.set_state(CheckDialog.waiting_for_file)
        template = BufferedInputFile(build_counterparties_template(), filename="kontragenty.xlsx")
        await message.answer_document(template)
        await message.answer(texts.CHECK_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.confirming, F.text == LAUNCH)
    async def launch(message: Message, state: FSMContext) -> None:
        run_id = (await state.get_data())["run_id"]
        try:
            draft = await repository.get_run(message.from_user.id, run_id)
            review = await review_package(draft, files, reader)
        except PackageIntegrityError:
            logger.error("package_integrity run_id=%s", run_id)
            await state.clear()
            await message.answer(texts.CHECK_FAILED, reply_markup=main_menu())
            return
        except Exception as exc:
            logger.error("package_review_failed error_type=%s", type(exc).__name__)
            await state.clear()
            await message.answer(texts.CHECK_FAILED, reply_markup=main_menu())
            return
        if review.blocking:
            await message.answer(texts.package_blocked(review.blocking), reply_markup=launch_menu())
            return
        try:
            run = await launch_run(message.from_user.id, run_id, repository)
        except Exception as exc:
            logger.error("launch_failed error_type=%s", type(exc).__name__)
            await state.clear()
            await message.answer(texts.CHECK_FAILED, reply_markup=main_menu())
            return
        await state.clear()
        cross_file = tuple(
            issue
            for issue in review.issues
            if issue.sheet == PACKAGE_SHEET or issue.code.endswith("_across_files")
        )
        text = texts.check_queued(run)
        if cross_file:
            text = texts.package_review(cross_file) + "\n\n" + text
        await message.answer(text, reply_markup=main_menu())

    @router.message(CheckDialog.confirming, F.text == ADD_PAYMENTS)
    async def add_payments(message: Message, state: FSMContext) -> None:
        await state.set_state(CheckDialog.waiting_for_payments_source)
        await message.answer(texts.PAYMENTS_SOURCE_PROMPT, reply_markup=payments_source_menu())

    @router.message(CheckDialog.waiting_for_payments_source, F.text == PAYMENTS_TEMPLATE)
    async def payments_by_template(message: Message, state: FSMContext) -> None:
        await state.set_state(CheckDialog.waiting_for_payments_period)
        await message.answer(texts.PAYMENTS_PERIOD_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.waiting_for_payments_source, F.text == PAYMENTS_EXPORT)
    async def payments_by_export(message: Message, state: FSMContext) -> None:
        await state.set_state(CheckDialog.waiting_for_export_inn)
        await message.answer(texts.EXPORT_INN_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.waiting_for_payments_source, F.text)
    async def remind_payments_source(message: Message) -> None:
        await message.answer(texts.PAYMENTS_SOURCE_PROMPT, reply_markup=payments_source_menu())

    @router.message(CheckDialog.waiting_for_export_inn, F.text)
    async def receive_export_inn(message: Message, state: FSMContext) -> None:
        try:
            inn = validate_inn(message.text or "")
        except InvalidInn as error:
            await message.answer(str(error), reply_markup=cancel_menu())
            return
        # The export names no INN, so this answer is its only link to the package: check it
        # while it can still be retyped, not after the file (block 5 of the manual test).
        data = await state.get_data()
        try:
            run = await repository.get_run(message.from_user.id, data["run_id"])
            known = await package_inns(run, files, reader)
        except Exception as exc:
            logger.error("export_inn_failed error_type=%s", type(exc).__name__)
            await state.clear()
            await message.answer(texts.CHECK_FAILED, reply_markup=main_menu())
            return
        if inn not in known:
            await message.answer(texts.EXPORT_INN_NOT_IN_PACKAGE, reply_markup=cancel_menu())
            return
        await state.update_data(export_inn=inn)
        await state.set_state(CheckDialog.waiting_for_export_file)
        await message.answer(texts.EXPORT_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.waiting_for_export_file, F.text)
    async def remind_export_file(message: Message) -> None:
        await message.answer(texts.EXPORT_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.waiting_for_payments_period, F.text)
    async def receive_period(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        analysis_date = date.fromisoformat(data["analysis_date"])
        period = parse_period(message.text or "", analysis_date)
        if period is None:
            await message.answer(texts.PAYMENTS_PERIOD_INVALID, reply_markup=cancel_menu())
            return
        await state.update_data(
            period_start=period.start.isoformat(), period_end=period.end.isoformat()
        )
        await state.set_state(CheckDialog.waiting_for_payments_file)
        template = BufferedInputFile(build_payments_template(), filename="platezhi.xlsx")
        await message.answer_document(template)
        await message.answer(texts.PAYMENTS_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.confirming, F.text == ADD_HISTORY)
    async def add_history(message: Message, state: FSMContext) -> None:
        await state.set_state(CheckDialog.waiting_for_history_file)
        template = BufferedInputFile(build_debt_history_template(), filename="istoriya-dolga.xlsx")
        await message.answer_document(template)
        await message.answer(texts.HISTORY_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.confirming, F.text == ADD_INTERACTIONS)
    async def add_interactions(message: Message, state: FSMContext) -> None:
        await state.set_state(CheckDialog.waiting_for_interactions_file)
        template = BufferedInputFile(build_interactions_template(), filename="vzaimodeystviya.xlsx")
        await message.answer_document(template)
        await message.answer(texts.INTERACTIONS_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.confirming, F.text == ADD_DEBT_REPORT)
    async def add_debt_report(message: Message, state: FSMContext) -> None:
        # No template to send: the file is the customer's own 1C print (S7-01).
        await state.set_state(CheckDialog.waiting_for_debt_report_file)
        await message.answer(texts.DEBT_REPORT_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.waiting_for_debt_report_file, F.text)
    async def remind_debt_report_file(message: Message) -> None:
        await message.answer(texts.DEBT_REPORT_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.waiting_for_file, F.text)
    async def remind_file(message: Message) -> None:
        await message.answer(texts.CHECK_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.waiting_for_payments_file, F.text)
    async def remind_payments_file(message: Message) -> None:
        await message.answer(texts.PAYMENTS_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.waiting_for_history_file, F.text)
    async def remind_history_file(message: Message) -> None:
        await message.answer(texts.HISTORY_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.waiting_for_interactions_file, F.text)
    async def remind_interactions_file(message: Message) -> None:
        await message.answer(texts.INTERACTIONS_FILE_PROMPT, reply_markup=cancel_menu())

    @router.message(CheckDialog.confirming, F.text)
    async def remind_launch(message: Message) -> None:
        await message.answer(texts.CHECK_CONFIRM, reply_markup=launch_menu())

    @router.message(StateFilter(None), Command("cancel"))
    async def nothing_to_cancel(message: Message) -> None:
        await message.answer(texts.NOTHING_TO_CANCEL, reply_markup=main_menu())

    @router.message()
    async def fallback(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.FALLBACK, reply_markup=main_menu())

    @dispatcher.errors()
    async def on_error(event: ErrorEvent) -> bool:
        # Never log exception text or the update: both can contain private data.
        logger.error(
            "handler_failed update_id=%s error_type=%s",
            event.update.update_id,
            type(event.exception).__name__,
        )
        message = event.update.message
        if (
            message is not None
            and message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in allowed_ids
        ):
            try:
                await message.answer(texts.ERROR, reply_markup=main_menu())
            except Exception as exc:
                logger.error("error_reply_failed error_type=%s", type(exc).__name__)
        return True

    dispatcher.include_router(router)
    return dispatcher
