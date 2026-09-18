import pytest

from claims_assistant.domain.imports import ImportIssue, IssueSeverity


def issue(**overrides) -> ImportIssue:
    fields = dict(
        code="inn_invalid",
        severity=IssueSeverity.ERROR,
        sheet="Контрагенты",
        reason="Контрольная цифра ИНН не совпадает.",
        row=7,
        column="A",
    )
    fields.update(overrides)
    return ImportIssue(**fields)


def test_cell_issue_keeps_all_coordinates():
    item = issue()
    assert (item.sheet, item.row, item.column) == ("Контрагенты", 7, "A")
    assert item.file_id is None
    assert item.severity is IssueSeverity.ERROR


def test_row_and_sheet_level_issues_leave_coordinates_empty():
    assert issue(row=7, column=None).column is None
    sheet_level = issue(row=None, column=None, code="sheet_missing")
    assert sheet_level.row is None and sheet_level.column is None


def test_file_id_is_attached_later_by_the_caller():
    from dataclasses import replace

    stored = replace(issue(), file_id="f1")
    assert stored.file_id == "f1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"code": ""},
        {"code": "Inn Invalid"},
        {"code": "inn-invalid"},
        {"code": "ИНН"},
        {"reason": ""},
        {"reason": "   "},
        {"sheet": ""},
        {"row": 0},
        {"row": -1},
        {"row": 1.5},
        {"row": True},
        {"column": "a"},
        {"column": "A1"},
        {"column": ""},
        {"row": None, "column": "A"},
        {"severity": "error"},
        {"file_id": ""},
    ],
)
def test_invalid_issue_is_rejected(overrides):
    with pytest.raises(ValueError):
        issue(**overrides)


def test_severity_values_match_the_agreed_contract():
    assert {item.value for item in IssueSeverity} == {"error", "warning"}


def test_column_accepts_multi_letter_excel_columns():
    assert issue(column="AB").column == "AB"
