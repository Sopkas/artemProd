from datetime import date

from claims_assistant.application.analysis_pipeline import RunSummary
from claims_assistant.domain.analysis import AnalysisRun, FileKind, RunStatus
from claims_assistant.domain.external import DataMode, Period
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.scoring import Priority

START = (
    "Здравствуйте! Это «Помощник претензионщика».\n\n"
    "Сервис поможет расставлять приоритеты в работе с должниками. "
    "Проверка контрагентов пока находится в разработке.\n\n"
    "Выберите «Проверить ИНН», «Новая проверка», «О сервисе» или «Помощь» в меню."
)
HELP = (
    "Сейчас доступны:\n"
    "/start — главное меню\n"
    "/help — помощь\n"
    "/about — о сервисе\n"
    "/inn — проверить ИНН\n"
    "/check — новая проверка по файлу «Контрагенты»\n"
    "/status — состояние последней проверки\n"
    "/report — отчёт по последней проверке\n"
    "/cancel — отменить ввод\n\n"
    "Можно также воспользоваться кнопками меню. "
    "Дополнительные файлы (платежи, взаимодействия, история долга) появятся позже."
)
ABOUT = (
    "«Помощник претензионщика» поможет объединить сведения о долгах, "
    "платежах и контрагентах, чтобы определить очерёдность работы.\n\n"
    "Проверка контрагентов находится в разработке. "
    "Сейчас доступны ввод ИНН, карточка сведений с источниками и ограничениями "
    "и загрузка списка контрагентов."
)
INN_PROMPT = "Введите ИНН юридического лица — 10 цифр.\nЧтобы вернуться в меню, нажмите «Отмена»."
INN_RETRY = "Введите ИНН ещё раз или нажмите «Отмена»."
INN_CANCELLED = "Проверка отменена."
NOTHING_TO_CANCEL = "Сейчас нечего отменять. Выберите пункт меню."
FALLBACK = "Выберите пункт меню или отправьте /help, чтобы увидеть доступные команды."
FILE_UNSUPPORTED = (
    "Файлы принимаются только внутри новой проверки: нажмите «Новая проверка» или /check."
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
    "Нажмите «Запустить проверку». Дополнительно можно добавить файлы «Платежи», "
    "«История долга» и «Взаимодействия» или нажать «Отмена»."
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
}
CHECK_QUEUED_PREFIX = "Проверка поставлена в очередь"
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
    unit = "организаций" if kind is FileKind.COUNTERPARTIES else "строк"
    text = f"{label} — {unit}: {rows}"
    if coverage is not None:
        text += f", период {period_text(coverage)}"
    return text


def package_summary(
    analysis_date: date,
    rows: int,
    issues: tuple[ImportIssue, ...],
    duplicate: bool,
    composition: tuple[str, ...] = (),
) -> str:
    lines = [CHECK_SUMMARY_TITLE]
    if duplicate:
        lines.append(CHECK_DUPLICATE)
    lines.append(f"Дата анализа: {_date(analysis_date)}")
    lines.append(f"Организаций: {rows}")
    lines.extend(_issues_block(issues))
    if any(issue.severity is IssueSeverity.ERROR for issue in issues):
        lines.append("Строки с ошибками в проверку не попадут.")
    lines.extend(_composition_block(composition))
    lines.append(CHECK_CONFIRM)
    return "\n".join(lines)


def _composition_block(composition: tuple[str, ...]) -> list[str]:
    if not composition:
        return []
    return ["Состав пакета:"] + [f"• {line}" for line in composition]


def ledger_summary(
    kind: FileKind,
    rows: int,
    issues: tuple[ImportIssue, ...],
    duplicate: bool,
    composition: tuple[str, ...],
) -> str:
    lines = [f"Файл «{FILE_KIND_LABELS[kind]}» принят"]
    if duplicate:
        lines.append(CHECK_DUPLICATE)
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
        counts = [
            f"{_PRIORITY_LABELS[priority]} — {summary.priorities.get(priority, 0)}"
            for priority in Priority
            if summary.priorities.get(priority, 0)
        ]
        lines.append("Приоритеты: " + (", ".join(counts) if counts else "нет"))
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
