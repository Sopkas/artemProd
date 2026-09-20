"""S2-05: the Excel report on four sheets (docs/data-contracts.md, «Выходной Excel»).

``build_report`` turns an ``AnalysisReport`` into .xlsx bytes. Every sheet has the same
layout: row 1 — a title naming the run and, in demo mode, the demo mark; row 2 — the
header; data from row 3 with frozen panes and a filter.

Text is always written as a string cell: a value such as ``=HYPERLINK(...)`` from a user
file or an external API stays text and is never evaluated as a formula.
"""

import io
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.worksheet import Worksheet

from claims_assistant.domain.explanation import explain_row
from claims_assistant.domain.external import (
    CompanyStatus,
    Coverage,
    DataMode,
    ExternalSnapshot,
    Fact,
    FactKind,
)
from claims_assistant.domain.imports import ImportIssue, IssueSeverity
from claims_assistant.domain.report import DEMO_SCORE_NOTE, AnalysisReport, ReportRow
from claims_assistant.domain.scoring import (
    INTERNAL_DEBT,
    INTERNAL_LAST_PAYMENT,
    INTERNAL_OVERDUE,
    Priority,
    report_order_key,
)

SHEETS = ("Приоритеты", "Основания", "Качество данных", "О проверке", "Хронология")
_MAX_CELL = 32_767  # Excel's limit for one cell

PRIORITY_LABELS = {
    Priority.CRITICAL: "Критичный",
    Priority.HIGH: "Высокий",
    Priority.MEDIUM: "Средний",
    Priority.LOW: "Низкий",
    Priority.UNKNOWN: "Недостаточно данных",
}
_COVERAGE_LABELS = {
    Coverage.COMPLETE: "полные",
    Coverage.PARTIAL: "неполные",
    Coverage.UNAVAILABLE: "недоступны",
}
_PART_LABELS = {
    "internal": "внутренние данные",
    "company": "организация",
    "bankruptcy": "ЕФРСБ",
    "finances": "финансы",
}
_FACT_LABELS = {
    FactKind.COMPANY_NAME: "Название организации",
    FactKind.COMPANY_STATUS: "Статус организации",
    FactKind.REVENUE: "Выручка",
    FactKind.NET_PROFIT: "Чистая прибыль",
    FactKind.BANKRUPTCY_EVENT: "Сообщение ЕФРСБ",
}
_STATUS_LABELS = {
    CompanyStatus.ACTIVE: "действует",
    CompanyStatus.LIQUIDATING: "в процессе ликвидации",
    CompanyStatus.LIQUIDATED: "ликвидирована",
}
_FILE_SOURCE = "Файл «Контрагенты»"
_BOLD = Font(bold=True)
_WRAP = Alignment(wrap_text=True, vertical="top")


def _text(value: str) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", value)[:_MAX_CELL]


def _day(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _moment(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")


def _money(value: Decimal, unit: str | None = "RUB") -> str:
    text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} ₽" if unit in (None, "RUB") else f"{text} {unit}"


def _put(sheet: Worksheet, row: int, column: int, value: object) -> None:
    cell = sheet.cell(row=row, column=column)
    if isinstance(value, str):
        cell.value = _text(value)
        cell.data_type = "s"  # never a formula, whatever the first character is
    elif isinstance(value, Decimal):
        cell.value = value
        cell.number_format = "#,##0.00"
    elif isinstance(value, date):
        cell.value = value
        cell.number_format = "DD.MM.YYYY"
    else:
        cell.value = value
    cell.alignment = _WRAP


def _table(
    sheet: Worksheet,
    title: str,
    header: tuple[str, ...],
    rows: Iterable[tuple[object, ...]],
    widths: tuple[int, ...],
) -> None:
    _put(sheet, 1, 1, title)
    sheet.cell(row=1, column=1).font = _BOLD
    for column, name in enumerate(header, start=1):
        _put(sheet, 2, column, name)
        sheet.cell(row=2, column=column).font = _BOLD
    last = 2
    for last, values in enumerate(rows, start=3):
        for column, value in enumerate(values, start=1):
            if value is not None:
                _put(sheet, last, column, value)
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(row=2, column=column).column_letter].width = width
    sheet.freeze_panes = "A3"
    sheet.auto_filter.ref = f"A2:{sheet.cell(row=2, column=len(header)).column_letter}{last}"


def _title(report: AnalysisReport, sheet: str) -> str:
    meta = report.meta
    text = f"{sheet} — проверка {meta.run_id}, дата анализа {_day(meta.analysis_date)}"
    if meta.mode is DataMode.DEMO:
        text += " — ДЕМО: синтетические данные"
        if meta.demo_scores:
            text += ", оценки приоритета заданы заранее"
    return text


def _ordered(report: AnalysisReport) -> list[ReportRow]:
    return sorted(report.rows, key=lambda r: report_order_key(r.assessment, r.counterparty))


def _company_name(row: ReportRow) -> str | None:
    if row.counterparty.name:
        return row.counterparty.name
    for snapshot in row.snapshots:
        for fact in snapshot.facts:
            if fact.kind is FactKind.COMPANY_NAME and isinstance(fact.value, str):
                return fact.value
    return None


def _coverage_text(row: ReportRow, demo_scores: bool) -> str:
    assessment = row.assessment
    if demo_scores:
        return "не проверялась (демо)"
    if assessment.base_complete:
        return "полная"
    gaps = [
        f"{_PART_LABELS.get(part, part)} — {_COVERAGE_LABELS[coverage]}"
        for part, coverage in assessment.coverage.items()
        if coverage is not Coverage.COMPLETE
    ]
    return "неполная: " + ", ".join(gaps) if gaps else "неполная"


def _reasons(row: ReportRow, demo_scores: bool) -> str:
    if demo_scores:
        return DEMO_SCORE_NOTE
    signals = row.assessment.signals
    if signals:
        parts = []
        for signal in signals:
            ids = list(signal.fact_ids[:5]) + (["…"] if len(signal.fact_ids) > 5 else [])
            parts.append(signal.reason + (f" (основания: {', '.join(ids)})" if ids else ""))
        return "; ".join(parts)
    if row.assessment.priority is Priority.UNKNOWN:
        return "Сигналов нет; данных для оценки недостаточно — см. «Качество данных»."
    return "Сигналов нет; базовый набор данных полный."


def _priorities(report: AnalysisReport) -> list[tuple[object, ...]]:
    demo = report.meta.demo_scores
    return [
        (
            row.counterparty.inn,
            _company_name(row),
            row.counterparty.debt,
            row.counterparty.overdue_days,
            PRIORITY_LABELS[row.assessment.priority],
            _coverage_text(row, demo),
            _reasons(row, demo),
            "Рекомендация в демо не формируется." if demo else row.assessment.next_step,
            *_explanation_cells(row, demo),
        )
        for row in _ordered(report)
    ]


def _explanation_cells(row: ReportRow, demo_scores: bool) -> tuple[str, str]:
    """«Пояснение» and «Обещания оплаты» (S5-04): the model's accepted text or the
    template from the rules, with a note on why the model's text is absent."""
    if demo_scores:
        return ("Пояснение в демо не формируется.", "")
    explained = explain_row(row.assessment, row.explanation, row.explanation_status)
    prefix = "ИИ: " if explained.from_model else "По правилам: "
    text = prefix + explained.text
    if explained.note:
        text += f" (пояснение ИИ недоступно: {explained.note})"
    return (text, "; ".join(explained.promises))


def _fact_value(fact: Fact) -> str:
    value = fact.value
    if value is None:
        return f"неизвестно: {fact.missing_reason}"
    if isinstance(value, CompanyStatus):
        return _STATUS_LABELS[value]
    if isinstance(value, Decimal):
        return _money(value, fact.unit)
    if isinstance(value, date):
        return _day(value)
    return str(value)


def _fact_when(fact: Fact) -> str | None:
    if fact.period is not None:
        return f"{_day(fact.period.start)}–{_day(fact.period.end)}"
    return _day(fact.observed_on) if fact.observed_on else None


def _external_grounds(inn: str, snapshot: ExternalSnapshot) -> list[tuple[object, ...]]:
    evidence = {item.id: item for item in snapshot.evidence}
    rows = []
    for fact in snapshot.facts:
        item = evidence.get(fact.evidence_ids[0])
        reference = None
        if item is not None:
            reference = item.url or f"запись {item.record_id}"
        rows.append(
            (
                fact.id,
                inn,
                _FACT_LABELS[fact.kind],
                _fact_value(fact),
                _fact_when(fact),
                item.source if item is not None else snapshot.source,
                reference,
            )
        )
    return rows


def _internal_grounds(row: ReportRow) -> list[tuple[object, ...]]:
    line = row.counterparty
    cutoff = _day(line.cutoff_date) if line.cutoff_date else None
    where = "лист «Контрагенты»"
    rows = []
    if line.debt is not None:
        rows.append((INTERNAL_DEBT, line.inn, "Сумма долга", _money(line.debt), cutoff))
    if line.overdue_days is not None:
        rows.append((INTERNAL_OVERDUE, line.inn, "Дней просрочки", str(line.overdue_days), cutoff))
    if line.last_payment_date is not None:
        rows.append(
            (
                INTERNAL_LAST_PAYMENT,
                line.inn,
                "Дата последнего платежа",
                _day(line.last_payment_date),
                None,
            )
        )
    return [(*item, _FILE_SOURCE, where) for item in rows]


def _grounds(report: AnalysisReport) -> list[tuple[object, ...]]:
    rows = []
    for row in _ordered(report):
        rows.extend(_internal_grounds(row))
        for snapshot in row.snapshots:
            rows.extend(_external_grounds(row.counterparty.inn, snapshot))
    return rows


def _quality(report: AnalysisReport) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    labels = dict(report.meta.files)
    # Package-level issues first, then file by file in upload order, then by cell.
    order = {file_id: index for index, (file_id, _) in enumerate(report.meta.files)}

    def key(issue: ImportIssue) -> tuple[int, int, str]:
        return order.get(issue.file_id, -1), issue.row or 0, issue.column or ""

    for issue in sorted(report.import_issues, key=key):
        where = [f"лист «{issue.sheet}»"]
        if issue.file_id in labels:
            where.insert(0, f"файл «{labels[issue.file_id]}»")
        if issue.row is not None:
            where.append(f"строка {issue.row}")
        if issue.column is not None:
            where.append(f"колонка {issue.column}")
        category = "Ошибка строки" if issue.severity is IssueSeverity.ERROR else "Предупреждение"
        # Import issues never carry the raw INN (it may be private or malformed).
        rows.append((category, None, ", ".join(where), issue.reason))
    for row in _ordered(report):
        for reason in row.assessment.missing_data:
            if reason != DEMO_SCORE_NOTE:
                rows.append(("Пропуск или ограничение данных", row.counterparty.inn, None, reason))
    for row in _ordered(report):
        status = row.explanation_status
        if status is not None and status != "accepted":
            note = explain_row(row.assessment, None, status).note
            rows.append(("Пояснение ИИ недоступно", row.counterparty.inn, None, note))
    return rows or [("Замечаний нет", None, None, None)]


def _chronology(report: AnalysisReport) -> list[tuple[object, ...]]:
    """«Взаимодействия» per company in report order, then by date (S4-04); no AI reading."""
    names = {row.counterparty.inn: _company_name(row) for row in _ordered(report)}
    order = {inn: index for index, inn in enumerate(names)}
    items = sorted(report.interactions, key=lambda i: (order[i.inn], i.happened_on))
    return [
        (
            item.inn,
            names[item.inn],
            _day(item.happened_on),
            item.interaction_id,
            item.channel,
            item.comment,
        )
        for item in items
    ] or [(None, None, None, None, None, "Файл «Взаимодействия» в пакет не загружен.")]


def _explanations_summary(report: AnalysisReport) -> str:
    statuses = [row.explanation_status for row in report.rows]
    if not any(status is not None for status in statuses):
        return "не запрашивались; в отчёте пояснения по правилам"
    accepted = sum(1 for s in statuses if s == "accepted")
    rejected = sum(1 for s in statuses if s and s.startswith("rejected:"))
    unavailable = sum(1 for s in statuses if s and s.startswith("unavailable:"))
    return (
        f"принято {accepted}, отклонено проверкой {rejected}, недоступно {unavailable}; "
        "без пояснения ИИ — шаблон по правилам"
    )


def _about(report: AnalysisReport) -> list[tuple[object, ...]]:
    meta = report.meta
    errors = sum(1 for i in report.import_issues if i.severity is IssueSeverity.ERROR)
    warnings = len(report.import_issues) - errors
    rows: list[tuple[object, ...]] = [
        ("ID проверки", meta.run_id),
        ("Дата анализа", _day(meta.analysis_date)),
        (
            "Режим",
            "ДЕМО — синтетические данные" if meta.mode is DataMode.DEMO else "Реальные данные",
        ),
        (
            "Оценки приоритета",
            "демонстрационные, заданы заранее"
            if meta.demo_scores
            else f"по правилам версии {meta.rules_version}",
        ),
        (
            "Внешние данные не старее",
            _moment(meta.checked_at) if meta.checked_at else "внешние источники не запрашивались",
        ),
        ("Версия правил", meta.rules_version),
        ("Версия ИИ", meta.ai_version or "ИИ-пояснения не используются"),
        ("Пояснения ИИ", _explanations_summary(report)),
        ("Отчёт сформирован", _moment(meta.created_at)),
        ("Организаций в отчёте", str(len(report.rows))),
        ("Замечания импорта", f"ошибок: {errors}, предупреждений: {warnings}"),
    ]
    rows.extend(("Состав пакета", line) for line in meta.package)
    rows.append(
        (
            "Ограничения",
            "Отчёт помогает расставить очерёдность работы; он не является юридическим "
            "заключением и не оценивает вероятность банкротства.",
        )
    )
    return rows


def build_report(report: AnalysisReport) -> bytes:
    """Return the .xlsx bytes of the five-sheet report."""
    workbook = Workbook()
    priorities = workbook.active
    priorities.title = SHEETS[0]
    _table(
        priorities,
        _title(report, SHEETS[0]),
        (
            "ИНН",
            "Название",
            "Долг, ₽",
            "Дней просрочки",
            "Приоритет",
            "Полнота",
            "Причины",
            "Следующий шаг",
            "Пояснение",
            "Обещания оплаты",
        ),
        _priorities(report),
        (14, 34, 16, 12, 20, 36, 60, 44, 80, 30),
    )
    _table(
        workbook.create_sheet(SHEETS[1]),
        _title(report, SHEETS[1]),
        (
            "ID факта",
            "ИНН",
            "Показатель / событие",
            "Значение",
            "Период / дата",
            "Источник",
            "Ссылка",
        ),
        _grounds(report),
        (22, 14, 26, 36, 24, 22, 30),
    )
    _table(
        workbook.create_sheet(SHEETS[2]),
        _title(report, SHEETS[2]),
        ("Категория", "ИНН", "Где", "Описание"),
        _quality(report),
        (30, 14, 40, 70),
    )
    _table(
        workbook.create_sheet(SHEETS[3]),
        _title(report, SHEETS[3]),
        ("Параметр", "Значение"),
        _about(report),
        (26, 80),
    )
    _table(
        workbook.create_sheet(SHEETS[4]),
        _title(report, SHEETS[4]),
        ("ИНН", "Название", "Дата", "ID взаимодействия", "Канал", "Комментарий"),
        _chronology(report),
        (14, 34, 12, 20, 14, 80),
    )
    # INN columns stay textual so leading digits are shown as written.
    for sheet, column in (
        (priorities, "A"),
        (workbook[SHEETS[1]], "B"),
        (workbook[SHEETS[2]], "B"),
        (workbook[SHEETS[4]], "A"),
    ):
        for cell in sheet[column][2:]:
            cell.number_format = "@"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
