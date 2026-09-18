from datetime import date

from claims_assistant.domain.analysis import AnalysisRun, RunStatus
from claims_assistant.domain.external import DataMode
from claims_assistant.domain.imports import ImportIssue, IssueSeverity

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
CHECK_CONFIRM = "Нажмите «Запустить проверку» или «Отмена»."
CHECK_QUEUED_PREFIX = "Проверка поставлена в очередь"
CHECK_CANCELLED = "Новая проверка отменена. Черновик, если он был создан, не запускается."
STATUS_TITLE = "Последняя проверка"
STATUS_EMPTY = "Проверок пока нет. Нажмите «Новая проверка», чтобы загрузить список."

_STATUS_LABELS = {
    RunStatus.DRAFT: "черновик, не запущена",
    RunStatus.QUEUED: "в очереди",
    RunStatus.RUNNING: "выполняется",
    RunStatus.COMPLETED: "завершена",
    RunStatus.PARTIAL: "завершена частично",
    RunStatus.FAILED: "не удалась",
}
_MAX_LISTED_ISSUES = 10


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


def package_summary(
    analysis_date: date, rows: int, issues: tuple[ImportIssue, ...], duplicate: bool
) -> str:
    lines = [CHECK_SUMMARY_TITLE]
    if duplicate:
        lines.append(CHECK_DUPLICATE)
    lines.append(f"Дата анализа: {_date(analysis_date)}")
    lines.append(f"Организаций: {rows}")
    lines.extend(_issues_block(issues))
    if any(issue.severity is IssueSeverity.ERROR for issue in issues):
        lines.append("Строки с ошибками в проверку не попадут.")
    lines.append(CHECK_CONFIRM)
    return "\n".join(lines)


def check_queued(run: AnalysisRun) -> str:
    return (
        f"{CHECK_QUEUED_PREFIX}: дата анализа {_date(run.analysis_date)}, "
        f"файлов {len(run.files)}. Обработка идёт в фоне; состояние — /status."
    )


def run_status(run: AnalysisRun) -> str:
    lines = [
        f"{STATUS_TITLE}: {status_label(run.status)}",
        f"Дата анализа: {_date(run.analysis_date)}",
        f"Создана: {run.created_at.strftime('%d.%m.%Y %H:%M')} UTC",
        f"Файлов в пакете: {len(run.files)}",
    ]
    if run.failure:
        lines.append(f"Причина: {run.failure}")
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
