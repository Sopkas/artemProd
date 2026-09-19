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

from claims_assistant.application.analysis_repository import AnalysisRepository
from claims_assistant.application.check_company import check_company
from claims_assistant.application.check_package import (
    FileStorage,
    PackageAccepted,
    accept_counterparties,
    latest_run,
    launch_run,
)
from claims_assistant.application.company_data import CompanyDataProvider
from claims_assistant.application.imports import SheetReader
from claims_assistant.application.report_delivery import (
    ReportUnavailable,
    confirm_delivery,
    fetch_report,
)
from claims_assistant.domain.external import DataMode
from claims_assistant.domain.inn import InvalidInn
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage

from . import texts
from .access import AccessMiddleware
from .card import format_card
from .menu import (
    CANCEL,
    CHECK_INN,
    LAUNCH,
    NEW_CHECK,
    REPORT,
    STATUS,
    TODAY,
    cancel_menu,
    date_menu,
    launch_menu,
    main_menu,
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
        if not isinstance(result, PackageAccepted):
            await message.answer(texts.package_rejected(result.issues), reply_markup=cancel_menu())
            return
        await state.update_data(run_id=result.run.id)
        await state.set_state(CheckDialog.confirming)
        await message.answer(
            texts.package_summary(
                result.run.analysis_date, len(result.rows), result.issues, result.duplicate
            ),
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
            await message.answer_document(document, reply_markup=main_menu())
        except Exception as exc:
            # Telegram refused the file: record it, keep the run and the artifact intact.
            logger.error("report_delivery_failed error_type=%s", type(exc).__name__)
            await confirm_delivery(report_file, repository, error="Не удалось отправить файл.")
            await message.answer(texts.REPORT_UNAVAILABLE, reply_markup=main_menu())
            return
        await confirm_delivery(report_file, repository)

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
        await message.answer(format_card(check), reply_markup=main_menu())

    # --- new check: date → file → launch ---

    @router.message(Command("check"))
    @router.message(F.text == NEW_CHECK)
    async def new_check(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(CheckDialog.waiting_for_date)
        await message.answer(texts.CHECK_DATE_PROMPT, reply_markup=date_menu())

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
            run = await launch_run(message.from_user.id, run_id, repository)
        except Exception as exc:
            logger.error("launch_failed error_type=%s", type(exc).__name__)
            await state.clear()
            await message.answer(texts.CHECK_FAILED, reply_markup=main_menu())
            return
        await state.clear()
        await message.answer(texts.check_queued(run), reply_markup=main_menu())

    @router.message(CheckDialog.waiting_for_file, F.text)
    async def remind_file(message: Message) -> None:
        await message.answer(texts.CHECK_FILE_PROMPT, reply_markup=cancel_menu())

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
