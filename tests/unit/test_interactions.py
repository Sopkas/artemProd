"""S4-02: the «Взаимодействия» sheet — chronology per INN, repeats and foreign INNs."""

from datetime import date

import pytest

from claims_assistant.application.ledger_imports import import_interactions
from claims_assistant.domain.imports import IssueSeverity
from claims_assistant.domain.interactions import (
    COLUMN_TITLES,
    InteractionRow,
    chronology,
    parse_interactions,
)
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.ledgers import (
    build_interactions_template,
    build_interactions_workbook,
)
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader

INN_A, INN_B = "7707083893", "7710140679"
PACKAGE = frozenset({INN_A, INN_B})
DAY = date(2026, 9, 1)


def interactions(rows, titles=None, known=PACKAGE, analysis_date=DAY):
    data = build_interactions_workbook(rows, titles or COLUMN_TITLES)
    return import_interactions(
        OpenpyxlSheetReader(), data, known_inns=known, analysis_date=analysis_date
    )


def codes(result):
    return [(issue.code, issue.severity, issue.row, issue.column) for issue in result.issues]


def test_valid_rows_become_a_chronology_per_inn():
    result = interactions(
        [
            [INN_B, "B-1", date(2026, 8, 5), "Письмо без ответа.", "письмо"],
            [INN_A, "A-2", date(2026, 8, 20), "Обещали оплатить до 31.08.", "телефон"],
            [INN_A, "A-1", date(2026, 8, 1), "Направлена претензия.", None],
            [INN_A, 3, date(2026, 8, 20), "Повторный звонок.", ""],
        ]
    )
    assert result.issues == ()
    assert [(row.inn, row.interaction_id) for row in result.rows] == [
        (INN_B, "B-1"),
        (INN_A, "A-1"),
        (INN_A, "A-2"),
        (INN_A, "3"),
    ]
    assert result.rows[1] == InteractionRow(
        INN_A, "A-1", date(2026, 8, 1), "Направлена претензия.", None
    )
    assert result.rows[3].channel is None  # an empty channel is "not given"
    assert set(chronology(result.rows)) == {INN_A, INN_B}


@pytest.mark.parametrize(
    "row, code, column",
    [
        (["", "A-1", date(2026, 8, 1), "Текст", None], "inn_missing", "A"),
        (["123", "A-1", date(2026, 8, 1), "Текст", None], "inn_invalid", "A"),
        ([INN_A, None, date(2026, 8, 1), "Текст", None], "interaction_id_missing", "B"),
        ([INN_A, 1.5, date(2026, 8, 1), "Текст", None], "interaction_id_invalid", "B"),
        ([INN_A, "A-1", None, "Текст", None], "interaction_date_missing", "C"),
        ([INN_A, "A-1", "вчера", "Текст", None], "date_invalid", "C"),
        ([INN_A, "A-1", date(2026, 8, 1), None, None], "comment_missing", "D"),
        ([INN_A, "A-1", date(2026, 8, 1), "   ", None], "comment_missing", "D"),
        ([INN_A, "A-1", date(2026, 8, 1), True, None], "comment_invalid", "D"),
        ([INN_A, "A-1", date(2026, 8, 1), "x" * 2001, None], "comment_too_long", "D"),
        ([INN_A, "A-1", date(2026, 8, 1), "Текст", "к" * 51], "channel_too_long", "E"),
    ],
)
def test_bad_cells_are_errors_with_their_column(row, code, column):
    result = interactions([row])
    assert result.rows == ()
    assert [(c, s, r, col) for c, s, r, col in codes(result) if r == 2] == [
        (code, IssueSeverity.ERROR, 2, column)
    ]


def test_issue_reasons_never_contain_the_comment():
    secret = "Клиент сказал, что директор уволен"
    result = interactions([["1234567894", "A-1", date(2026, 8, 1), secret, None]])
    assert result.rows == ()
    assert all(secret not in issue.reason for issue in result.issues)
    assert codes(result) == [("inn_not_in_package", IssueSeverity.ERROR, 2, "A")]


def test_after_analysis_date_is_excluded_with_a_warning():
    result = interactions([[INN_A, "A-1", date(2026, 9, 2), "Позже даты анализа.", None]])
    assert result.rows == ()
    assert codes(result) == [("interaction_after_analysis_date", IssueSeverity.WARNING, 2, "C")]


def test_repeated_id_same_data_warns_and_other_data_errors():
    result = interactions(
        [
            [INN_A, "A-1", date(2026, 8, 1), "Текст.", None],
            [INN_A, "A-1", date(2026, 8, 1), "Текст.", None],
            [INN_A, "A-1", date(2026, 8, 2), "Другой текст.", None],
            [INN_B, "A-1", date(2026, 8, 1), "Тот же ID у другого ИНН.", None],
        ]
    )
    assert [(row.inn, row.interaction_id) for row in result.rows] == [
        (INN_A, "A-1"),
        (INN_B, "A-1"),
    ]
    assert codes(result) == [
        ("duplicate_interaction", IssueSeverity.WARNING, 3, None),
        ("interaction_id_conflict", IssueSeverity.ERROR, 4, "B"),
    ]


def test_channel_column_is_optional_and_unknown_columns_warn():
    titles = ("ИНН", "ID взаимодействия", "Дата взаимодействия", "Комментарий", "Менеджер")
    result = interactions([[INN_A, "A-1", date(2026, 8, 1), "Текст.", "Иванов"]], titles=titles)
    assert [row.channel for row in result.rows] == [None]
    assert [c for c, *_ in codes(result)] == ["unknown_column"]


def test_missing_required_column_or_sheet_is_unusable():
    titles = ("ИНН", "ID взаимодействия", "Дата взаимодействия")
    result = interactions([[INN_A, "A-1", date(2026, 8, 1)]], titles=titles)
    assert result.rows == () and any(c == "comment_column_missing" for c, *_ in codes(result))
    result = import_interactions(
        OpenpyxlSheetReader(),
        build_counterparties_template(),
        known_inns=PACKAGE,
        analysis_date=DAY,
    )
    assert result.rows == () and len(result.issues) == 1 and result.issues[0].row is None


def test_without_package_context_every_valid_inn_is_kept():
    result = interactions([["1234567894", "A-1", date(2026, 8, 1), "Текст.", None]], known=None)
    assert len(result.rows) == 1


def test_template_round_trips():
    result = import_interactions(
        OpenpyxlSheetReader(),
        build_interactions_template(),
        known_inns=PACKAGE,
        analysis_date=DAY,
    )
    assert len(result.rows) == 1 and result.issues == ()


def test_parse_interactions_is_pure_over_the_header_and_rows():
    header = tuple(COLUMN_TITLES)
    rows = [(2, (INN_A, "A-1", date(2026, 8, 1), "Текст.", None))]
    result = parse_interactions(header, rows, known_inns=PACKAGE, analysis_date=DAY)
    assert len(result.rows) == 1
