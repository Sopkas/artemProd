from collections.abc import Sequence
from datetime import date, time

from claims_assistant.application.analysis_pipeline import RunSummary
from claims_assistant.domain.analysis import AnalysisRun, FileKind, RunStatus
from claims_assistant.domain.external import DataMode, Period
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.plural import companies
from claims_assistant.domain.scoring import Priority
from claims_assistant.domain.watchlist import WatchEntry

START = (
    "Здравствуйте! Это «Помощник претензионщика».\n\n"
    "Бот проверяет должников по вашему списку и подсказывает, с кем работать в первую "
    "очередь и почему: объединяет долги, платежи и взаимодействия из ваших файлов со "
    "сведениями об организации, сообщениями о банкротстве и финансами.\n\n"
    "• «Проверить ИНН» — карточка одной организации.\n"
    "• «Мониторинг» — каждый день проверяю ваш список и пишу, если появилось новое: "
    "банкротство, суды, ФССП.\n"
    "• «Новая проверка» — список из Excel и отчёт с приоритетами.\n"
    "Подробнее — «Помощь»."
)
HELP = (
    "Команды:\n"
    "/start — главное меню\n"
    "/inn — карточка одной организации по ИНН\n"
    "/monitor — мониторинг: список организаций и ежедневные уведомления\n"
    "/check — новая проверка по файлу «Контрагенты»\n"
    "/status — состояние последней проверки\n"
    "/report — прислать отчёт по последней проверке ещё раз\n"
    "/cancel — отменить ввод\n"
    "/about — о сервисе\n\n"
    "Как пройти проверку:\n"
    "1. «Новая проверка» → дата анализа → файл «Контрагенты» по шаблону, который пришлёт бот.\n"
    "2. По желанию добавьте платежи (по шаблону или выгрузкой из 1С), историю долга, "
    "взаимодействия и отчёт по договорам: чем больше данных, тем меньше «недостаточно данных».\n"
    "3. «Запустить проверку» — отчёт придёт файлом, обычно через несколько минут.\n\n"
    "Файлы — .xlsx (выгрузки 1С можно и .xls), до 10 МБ."
)
ABOUT = (
    "«Помощник претензионщика» помогает решить, каких должников проверить первыми, "
    "какие факты это объясняют и что сделать дальше.\n\n"
    "Приоритет считают правила, а не модель: у каждой причины есть значение, дата и "
    "источник. Если данных не хватает, бот так и пишет — «недостаточно данных», а не "
    "«рисков нет». Пояснение ИИ, если оно включено, только объясняет готовый результат.\n\n"
    "Отчёт помогает расставить очерёдность работы; он не является юридическим заключением."
)
INN_PROMPT = (
    "Введите ИНН организации (10 цифр) или ИП (12 цифр).\nЧтобы вернуться в меню, нажмите «Отмена»."
)
INN_RETRY = "Введите ИНН ещё раз или нажмите «Отмена»."
# S8-03: sent right after a valid INN, deleted when the card comes; the customer's words.
COUNTING = "💰 Считаю миллионы…"
# S8-05: under the card, while the model reads it (10–40 s, measured on #85).
REVIEWING = "🤖 Разбираю данные…"
INN_CANCELLED = "Проверка отменена."
NOTHING_TO_CANCEL = "Сейчас нечего отменять. Выберите пункт меню."
FALLBACK = "Выберите пункт меню или отправьте /help, чтобы увидеть доступные команды."
FILE_UNSUPPORTED = (
    "Файлы принимаются внутри новой проверки («Новая проверка» или /check) и в мониторинге "
    "(«Мониторинг» → «Добавить на мониторинг»)."
)
ERROR = "Не удалось обработать сообщение. Попробуйте ещё раз или отправьте /start."
CHECK_FAILED = "Не удалось выполнить проверку. Попробуйте позже или отправьте /start."

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
CHECK_DATE_PROMPT = (
    "Укажите дату анализа — день, на который считаются долги и просрочка.\n"
    "Нажмите «Сегодня» или введите дату в формате ДД.ММ.ГГГГ."
)
CHECK_DATE_INVALID = (
    "Дата не распознана. Введите ДД.ММ.ГГГГ или ГГГГ-ММ-ДД, либо нажмите «Сегодня»."
)
CHECK_FILE_PROMPT = (
    "Отправьте файл .xlsx с листом «Контрагенты» (до 10 МБ). "
    "Шаблон с примером строк — выше.\nЧтобы вернуться в меню, нажмите «Отмена»."
)
FILE_NOT_XLSX = "Нужен файл .xlsx. Отправьте файл ещё раз или нажмите «Отмена»."
FILE_TOO_LARGE = "Файл больше 10 МБ. Уменьшите выгрузку или нажмите «Отмена»."
CHECK_REJECTED_TITLE = "Файл не принят"
CHECK_SUMMARY_TITLE = "Пакет принят"
CHECK_DUPLICATE = "Этот файл уже есть в пакете; повторно не добавлен."
CHECK_CONFIRM = (
    "Нажмите «Запустить проверку». Дополнительно можно добавить платежи, историю долга, "
    "взаимодействия и отчёт по договорам или нажать «Отмена»."
)
PAYMENTS_SOURCE_PROMPT = (
    "Как пришлёте платежи?\n"
    "• «По нашему шаблону» — один файл на всех должников, период укажете вы.\n"
    "• «Выгрузка из 1С» — печатная форма по одной организации: период возьмём из самого "
    "файла, а вы скажете, чья это выгрузка."
)
EXPORT_INN_PROMPT = (
    "Отправьте ИНН организации, по которой сделана выгрузка: в самом файле его нет, "
    "а по названию в шапке опознать организацию нельзя.\n"
    "ИНН должен быть из файла «Контрагенты» этой проверки."
)
EXPORT_INN_NOT_IN_PACKAGE = (
    "Этой организации нет в файле «Контрагенты» этой проверки. "
    "Отправьте ИНН из него или нажмите «Отмена»."
)
EXPORT_FILE_PROMPT = (
    "Отправьте файл выгрузки .xlsx или .xls (до 10 МБ) — так, как его печатает 1С, "
    "перестраивать ничего не нужно.\nЧтобы вернуться к пакету, нажмите «Отмена»."
)
PAYMENTS_PERIOD_PROMPT = (
    "Укажите период, за который выгрузка платежей полная: ДД.ММ.ГГГГ–ДД.ММ.ГГГГ "
    "(например, 01.06.2026–31.08.2026).\n"
    "Указывая период, вы подтверждаете, что в выгрузке есть все поступления за него."
)
PAYMENTS_PERIOD_INVALID = (
    "Период не распознан. Введите две даты ДД.ММ.ГГГГ через дефис, "
    "начало не позже конца, конец не позже даты анализа."
)
PAYMENTS_FILE_PROMPT = (
    "Отправьте файл .xlsx с листом «Платежи» (до 10 МБ). "
    "Шаблон — выше.\nЧтобы вернуться к пакету, нажмите «Отмена»."
)
HISTORY_FILE_PROMPT = (
    "Отправьте файл .xlsx с листом «История долга» (до 10 МБ). "
    "Шаблон — выше.\nЧтобы вернуться к пакету, нажмите «Отмена»."
)
DEBT_REPORT_FILE_PROMPT = (
    "Отправьте «Отчёт по просроченным лизинговым платежам» из 1С (.xlsx или .xls, до 10 МБ) — "
    "так, как он выгружается, перестраивать ничего не нужно.\n"
    "Он не заменяет файл «Контрагенты»: ИНН берутся оттуда, а из отчёта — договоры, их "
    "просрочка и суммы. Договоры привязываются к организациям по названию; чьи названия "
    "не совпадут, будут названы в «Качестве данных» отчёта.\n"
    "Чтобы вернуться к пакету, нажмите «Отмена»."
)
INTERACTIONS_FILE_PROMPT = (
    "Отправьте файл .xlsx с листом «Взаимодействия» (до 10 МБ). "
    "Шаблон — выше.\nЧтобы вернуться к пакету, нажмите «Отмена»."
)
PACKAGE_BLOCKED_TITLE = "Проверка не запущена"
PACKAGE_REVIEW_TITLE = "Проверка пакета"
LEDGER_CANCELLED = "Файл не добавлен, пакет сохранён. " + CHECK_CONFIRM
FILE_KIND_LABELS = {
    FileKind.COUNTERPARTIES: "Контрагенты",
    FileKind.PAYMENTS: "Платежи",
    FileKind.DEBT_HISTORY: "История долга",
    FileKind.INTERACTIONS: "Взаимодействия",
    FileKind.DEBT_REPORT: "Отчёт по договорам",
}
# Both files are counted in companies, not in rows: that is what the user sees in them.
_BY_COMPANY = frozenset({FileKind.COUNTERPARTIES, FileKind.DEBT_REPORT})
# S8-03: the same «at work» mark as after an INN; the list is checked in the background.
CHECK_QUEUED_PREFIX = f"{COUNTING} Проверка поставлена в очередь"
CHECK_CANCELLED = "Новая проверка отменена. Черновик, если он был создан, не запускается."
STATUS_TITLE = "Последняя проверка"
STATUS_EMPTY = "Проверок пока нет. Нажмите «Новая проверка», чтобы загрузить список."
STATUS_REPORT_HINT = "Отчёт готов — нажмите «Отчёт» или /report, чтобы получить файл ещё раз."
RUN_FINISHED_TITLE = "Проверка завершена"
REPORT_EMPTY = "Готового отчёта пока нет. Он появится после завершения проверки."
REPORT_UNAVAILABLE = (
    "Файл отчёта не найден в хранилище. Запустите новую проверку или обратитесь к разработчику."
)
REPORT_SEND_FAILED = "Не удалось отправить файл. Попробуйте ещё раз: «Отчёт» или /report."

_STATUS_LABELS = {
    RunStatus.DRAFT: "черновик, не запущена",
    RunStatus.QUEUED: "в очереди",
    RunStatus.RUNNING: "выполняется",
    RunStatus.COMPLETED: "завершена",
    RunStatus.PARTIAL: "завершена частично",
    RunStatus.FAILED: "не удалась",
}
_MAX_LISTED_ISSUES = 10
_PRIORITY_LABELS = {
    Priority.CRITICAL: "критичный",
    Priority.HIGH: "высокий",
    Priority.MEDIUM: "средний",
    Priority.LOW: "низкий",
    Priority.UNKNOWN: "недостаточно данных",
}
# The mark in front of a priority (customer's request, 11.10.2026: the point at a glance).
# ✅ is «checked in full, nothing found» and nothing more: the product ranks the work, it
# does not rate a company's reliability.
PRIORITY_MARKS = {
    Priority.CRITICAL: "❗️",
    Priority.HIGH: "❗️",
    Priority.MEDIUM: "⚠️",
    Priority.LOW: "✅",
    Priority.UNKNOWN: "❔",
}
CARD_NO_DETAILS = "Сначала проверьте организацию: «Проверить ИНН»."


def status_label(status: RunStatus) -> str:
    return _STATUS_LABELS[status]


def _date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _issue_line(issue: ImportIssue) -> str:
    where = []
    if issue.row is not None:
        where.append(f"строка {issue.row}")
    if issue.column is not None:
        where.append(f"колонка {issue.column}")
    prefix = ", ".join(where) or f"лист «{issue.sheet}»"
    return f"• {prefix}: {issue.reason}"


def _issues_block(issues: tuple[ImportIssue, ...]) -> list[str]:
    errors = [issue for issue in issues if issue.severity is IssueSeverity.ERROR]
    warnings = [issue for issue in issues if issue.severity is IssueSeverity.WARNING]
    lines = [f"Ошибок: {len(errors)}, предупреждений: {len(warnings)}"]
    listed = (errors + warnings)[:_MAX_LISTED_ISSUES]
    lines.extend(_issue_line(issue) for issue in listed)
    if len(issues) > _MAX_LISTED_ISSUES:
        lines.append(f"… и ещё {len(issues) - _MAX_LISTED_ISSUES}")
    return lines


def package_rejected(issues: tuple[ImportIssue, ...]) -> str:
    lines = [f"{CHECK_REJECTED_TITLE}: пригодных строк нет."]
    lines.extend(_issues_block(issues))
    lines.append("Исправьте файл и отправьте снова или нажмите «Отмена».")
    return "\n".join(lines)


def period_text(period: Period) -> str:
    return f"{_date(period.start)}–{_date(period.end)}"


def composition_line(kind: FileKind, rows: int, coverage: Period | None) -> str:
    label = FILE_KIND_LABELS[kind]
    unit = "организаций" if kind in _BY_COMPANY else "строк"
    text = f"{label} — {unit}: {rows}"
    if coverage is not None:
        text += f", период {period_text(coverage)}"
    return text


def template_samples(inns: tuple[str, ...]) -> str:
    """Example rows of the template left in the file: they would be checked as the user's
    debtors, and their INNs belong to real companies (review A on #79)."""
    return (
        f"Похоже, в файле остались строки-примеры из шаблона (ИНН {', '.join(inns)}). "
        "Бот проверит их как ваших должников, а это ИНН настоящих организаций: на живом "
        "источнике они потратят запросы и попадут в отчёт. Если это не ваши организации, "
        "нажмите «Отмена», удалите строки-примеры и начните проверку заново."
    )


def package_summary(
    analysis_date: date,
    rows: int,
    issues: tuple[ImportIssue, ...],
    duplicate: bool,
    composition: tuple[str, ...] = (),
    samples: tuple[str, ...] = (),
) -> str:
    """``samples`` — INNs of rows that are the template's examples, left untouched."""
    lines = [CHECK_SUMMARY_TITLE]
    if duplicate:
        lines.append(CHECK_DUPLICATE)
    lines.append(f"Дата анализа: {_date(analysis_date)}")
    lines.append(f"Организаций: {rows}")
    lines.extend(_issues_block(issues))
    if any(issue.severity is IssueSeverity.ERROR for issue in issues):
        lines.append("Строки с ошибками в проверку не попадут.")
    if samples:
        lines.append(template_samples(samples))
    lines.extend(_composition_block(composition))
    lines.append(CHECK_CONFIRM)
    return "\n".join(lines)


def _composition_block(composition: tuple[str, ...]) -> list[str]:
    if not composition:
        return []
    return ["Состав пакета:"] + [f"• {line}" for line in composition]


def export_summary(export, rows: int, coverage: Period | None = None) -> str:
    """What the export's own header said — so a file about the wrong client is noticed.

    ``coverage`` is the part of the period the package counts on; when it is shorter than
    the header's (an export printed past the analysis date), the summary says so.
    """
    lines = []
    if export.counterparty:
        lines.append(f"Контрагент в файле: {export.counterparty}")
    if export.organization:
        lines.append(f"Организация: {export.organization}")
    if export.period is not None:
        lines.append(
            "Период выгрузки из файла: "
            f"{export.period.start.strftime('%d.%m.%Y')}–{export.period.end.strftime('%d.%m.%Y')}"
        )
    else:
        lines.append(
            "Период в файле не указан: полнота выгрузки неизвестна, "
            "показатель по платежам останется неопределённым."
        )
    if export.period is not None and coverage is not None and coverage != export.period:
        lines.append(
            "Поступления после даты анализа не учитываются, поэтому выгрузка полна "
            f"за {period_text(coverage)}."
        )
    lines.append(f"Платежей принято: {rows}. В пакет сохранён лист «Платежи» из этой выгрузки.")
    return "\n".join(lines)


def ledger_summary(
    kind: FileKind,
    rows: int,
    issues: tuple[ImportIssue, ...],
    duplicate: bool,
    composition: tuple[str, ...],
    contracts: int | None = None,
) -> str:
    lines = [f"Файл «{FILE_KIND_LABELS[kind]}» принят"]
    if duplicate:
        lines.append(CHECK_DUPLICATE)
    if kind is FileKind.DEBT_REPORT:
        # The report is read as companies with their contracts, so that is what is shown;
        # «строк» would mean nothing to someone looking at a 1C print.
        lines.append(f"Организаций: {rows}, договоров: {contracts or 0}")
        lines.append(
            "Договоры привяжутся к организациям проверки по названию — "
            "несовпавшие будут названы в отчёте, на листе «Качество данных»."
        )
    else:
        lines.append(f"Строк принято: {rows}")
    lines.extend(_issues_block(issues))
    if any(issue.severity is IssueSeverity.ERROR for issue in issues):
        lines.append("Строки с ошибками в проверку не попадут.")
    lines.extend(_composition_block(composition))
    lines.append(CHECK_CONFIRM)
    return "\n".join(lines)


def package_conflict(reason: str) -> str:
    return f"Файл не принят. {reason}"


def package_blocked(issues: tuple[ImportIssue, ...]) -> str:
    lines = [f"{PACKAGE_BLOCKED_TITLE}: пакет собран неверно."]
    lines.extend(f"• {issue.reason}" for issue in issues[:_MAX_LISTED_ISSUES])
    lines.append("Нажмите «Отмена» и соберите пакет заново.")
    return "\n".join(lines)


def package_review(issues: tuple[ImportIssue, ...]) -> str:
    """Cross-file findings shown once at launch; per-file issues were shown on upload."""
    lines = [f"{PACKAGE_REVIEW_TITLE}: замечаний {len(issues)}."]
    lines.extend(_issue_line(issue) for issue in issues[:_MAX_LISTED_ISSUES])
    if len(issues) > _MAX_LISTED_ISSUES:
        lines.append(f"… и ещё {len(issues) - _MAX_LISTED_ISSUES}")
    return "\n".join(lines)


def ledger_rejected(kind: FileKind, issues: tuple[ImportIssue, ...]) -> str:
    lines = [f"Файл «{FILE_KIND_LABELS[kind]}» не принят: пригодных строк нет."]
    lines.extend(_issues_block(issues))
    lines.append("Исправьте файл и отправьте снова или нажмите «Отмена».")
    return "\n".join(lines)


def check_queued(run: AnalysisRun) -> str:
    return (
        f"{CHECK_QUEUED_PREFIX}: дата анализа {_date(run.analysis_date)}, "
        f"файлов {len(run.files)}. Обработка идёт в фоне; состояние — /status."
    )


def report_caption(run: AnalysisRun) -> str:
    caption = f"Отчёт по проверке: дата анализа {_date(run.analysis_date)}."
    if run.mode is DataMode.DEMO:
        caption += " Демонстрационные данные."
    return caption


def run_status(run: AnalysisRun, has_report: bool = False) -> str:
    lines = [
        f"{STATUS_TITLE}: {status_label(run.status)}",
        f"Дата анализа: {_date(run.analysis_date)}",
        f"Создана: {run.created_at.strftime('%d.%m.%Y %H:%M')} UTC",
        f"Файлов в пакете: {len(run.files)}",
    ]
    if run.failure:
        lines.append(f"Причина: {run.failure}")
    if has_report:
        lines.append(STATUS_REPORT_HINT)
    if run.mode is DataMode.DEMO:
        lines.append("Режим: демонстрационные данные.")
    return "\n".join(lines)


def run_finished(run: AnalysisRun, summary: RunSummary | None) -> str:
    lines = [
        f"{RUN_FINISHED_TITLE}: {status_label(run.status)}",
        f"Дата анализа: {_date(run.analysis_date)}",
    ]
    if run.failure:
        lines.append(f"Причина: {run.failure}")
    if summary is not None:
        lines.append(f"Организаций: {summary.companies}, проверено полностью: {summary.checked}")
        # One line a priority, its mark first: read at a glance, not as a sentence.
        counts = [
            f"{PRIORITY_MARKS[priority]} {_PRIORITY_LABELS[priority].capitalize()} — "
            f"{summary.priorities[priority]}"
            for priority in Priority
            if summary.priorities.get(priority, 0)
        ]
        # No row got a priority (every one had errors): said, not left out (review A on #86).
        lines.extend(counts or ["Приоритетов нет."])
        if summary.explanations_missing:
            lines.append(
                f"Пояснений ИИ нет у {summary.explanations_missing} организаций — "
                "в отчёте рекомендация по правилам."
            )
        lines.append("Отчёт — файлом ниже; повторно: «Отчёт» или /report.")
    if run.mode is DataMode.DEMO:
        lines.append("Режим: демонстрационные данные.")
    return "\n".join(lines)


def inn_invalid(reason: str) -> str:
    # The reason never echoes the user's input; see domain.inn.InvalidInn.
    return f"{reason}\n{INN_RETRY}"


def access_denied(user_id: int) -> str:
    return (
        "Доступ к боту ограничен.\n"
        f"Ваш Telegram ID: {user_id}\n"
        "Передайте его разработчику для подключения."
    )


# --- S8-09: monitoring ---

WATCH_UNAVAILABLE = "Мониторинг в этом запуске бота выключен."
WATCH_RULES = (
    "Пишу, когда появляется новое: дело о банкротстве, заявление или намерение кредитора "
    "подать на банкротство, сообщение ЕФРСБ, исполнительное производство ФССП, "
    "арбитражный иск к организации от 100 000 ₽, смена статуса. Первая проверка — точка "
    "отсчёта: об изменениях пишу со следующей."
)
WATCH_ADD_PROMPT = (
    "Пришлите ИНН сообщением — можно списком: через запятую, столбиком, вместе с "
    "названиями. Или файл: отчёт из 1С с колонкой «ИНН» либо таблицу, где одна из колонок "
    "называется «ИНН» (.xlsx или .xls, до 10 МБ).\n"
    "Чтобы вернуться, нажмите «Отмена»."
)
WATCH_REMOVE_PROMPT = (
    "Пришлите ИНН, которые убрать с мониторинга, — можно списком.\n"
    "Чтобы вернуться, нажмите «Отмена»."
)
WATCH_NO_INN = (
    "В сообщении не нашлось ИНН — 10 цифр у организации или 12 у ИП. "
    "Пришлите ещё раз или нажмите «Отмена»."
)
WATCH_FILE_TYPE = "Нужен файл .xlsx или .xls. Пришлите другой файл или нажмите «Отмена»."
WATCH_FILE_UNREADABLE = "Файл не удалось прочитать. Пришлите другой файл или ИНН сообщением."
WATCH_MENU_HINT = "Выберите действие на клавиатуре ниже."
WATCH_STEP_CANCELLED = "Отменено. " + WATCH_MENU_HINT
WATCH_CLEAR_HINT = "Нажмите «Да, очистить список» или «Отмена»."
WATCH_FILE_NOT_HERE = (
    "Файл здесь не нужен: пришлите ИНН сообщением или нажмите «Отмена». Добавить список "
    "файлом — «Добавить на мониторинг»."
)
BACK_TO_MENU = "Главное меню."
UNWATCHED_SHORT = "Убрано с мониторинга"
WATCH_LIST_EMPTY = "На мониторинге пока никого нет: «Добавить на мониторинг»."
WATCH_CLEAR_EMPTY = "Список и так пуст."
WATCH_CLEAR_KEPT = "Список не тронут."
WATCH_FAILED = "Не удалось обновить список мониторинга. Попробуйте ещё раз позже."
UNWATCH_ABSENT = "Этой организации уже нет на мониторинге."
_RECENT = 5
WATCH_NAME_LATER = (
    "Названия, которых нет в списке, бот возьмёт из источника при первой проверке; можно "
    "прислать ИНН вместе с названием в одной строке."
)


def watch_schedule(run_at: time, utc_offset_hours: int) -> str:
    zone = "по Москве" if utc_offset_hours == 3 else f"(UTC{utc_offset_hours:+d})"
    return f"Проверка — каждый день в {run_at:%H:%M} {zone}."


def _watched(entry: WatchEntry) -> str:
    return f"{entry.inn} — {entry.name}" if entry.name else entry.inn


def watch_overview(entries: Sequence[WatchEntry], limit: int, schedule: str) -> str:
    """The «Мониторинг» screen: how many, when, the newest few; the rest is the file."""
    if not entries:
        return "\n".join(
            [
                "🔔 Мониторинг: в списке пока никого нет.",
                schedule,
                "",
                WATCH_RULES,
                "",
                "Нажмите «Добавить на мониторинг» и пришлите ИНН сообщением или файлом.",
            ]
        )
    lines = [f"🔔 Мониторинг: {len(entries)} из {limit}.", schedule]
    checked = [entry.last_checked_on for entry in entries if entry.last_checked_on]
    if checked:
        lines.append(f"Последняя проверка: {_date(max(checked))}.")
    waiting = sum(1 for entry in entries if entry.last_checked_on is None)
    if waiting:
        lines.append(f"Ждут первой проверки: {waiting}.")
    lines += ["", "Последние добавленные:"]
    lines += [f"  {_watched(entry)}" for entry in entries[:_RECENT]]
    if len(entries) > _RECENT:
        lines.append(f"  …и ещё {len(entries) - _RECENT} — весь список: «Список файлом».")
    if any(entry.name is None for entry in entries):
        lines.append(WATCH_NAME_LATER)
    lines += ["", WATCH_RULES]
    return "\n".join(lines)


_RENAMED_SHOWN = 3


def watch_renamed(renames: Sequence[tuple[str, str | None, str]]) -> str:
    """A name sent again for a company already on the list replaces the earlier one. Each
    change is shown, «было → стало», so a stray word taken for a name is seen at once:
    «7707083893 тоже проверь» makes «тоже проверь» the name (Andrey's review of #114)."""
    lines = [f"Названия обновлены: {len(renames)}."]
    for inn, old, new in renames[:_RENAMED_SHOWN]:
        lines.append(f"• {inn}: {old or 'без названия'} → {new}")
    if len(renames) > _RENAMED_SHOWN:
        lines.append(f"…и ещё {len(renames) - _RENAMED_SHOWN} — в «Список файлом».")
    return "\n".join(lines)


def watch_added_footer(schedule: str) -> str:
    return f"{schedule} Первая проверка — точка отсчёта: об изменениях пишу со следующей."


def watch_clear_prompt(count: int) -> str:
    return (
        f"Убрать с мониторинга весь список — {companies(count)}? Прошлые проверки забудутся: "
        "если добавить организацию снова, первая проверка станет новой точкой отсчёта."
    )


def watch_cleared(count: int) -> str:
    return f"Список очищен: убрано {companies(count)}."


def watch_file_caption(count: int, limit: int) -> str:
    return f"Список на мониторинге: {count} из {limit}."


def watch_waiting(count: int) -> str:
    """Under the day's summary: the companies the day's requests did not reach."""
    return (
        f"Не дошла очередь: {companies(count)} — кончился лимит запросов к источнику. "
        "В следующий раз — в первую очередь."
    )


def watch_nothing_reached(day: date, count: int) -> str:
    return (
        f"📋 Мониторинг на {_date(day)}. Проверить не удалось: кончился лимит запросов к "
        f"источнику. Ждут проверки: {companies(count)}, в следующий раз — в первую очередь."
    )


def unwatched(inn: str, total: int, limit: int) -> str:
    return f"ИНН {inn} убран с мониторинга. Сейчас на мониторинге: {total} из {limit}."
