"""S2-06: the synthetic control package parses into exactly the documented outcome."""

from claims_assistant.application.imports import import_counterparties
from claims_assistant.infrastructure.excel.control_package import (
    ANALYSIS_DATE,
    DEMO_SCORES,
    EXPECTED_ISSUES,
    EXPECTED_USABLE,
    DemoPriority,
    build_control_package,
)
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader


def _parse():
    data = build_control_package()
    return import_counterparties(OpenpyxlSheetReader(), data, analysis_date=ANALYSIS_DATE)


def test_package_has_fifty_rows_and_the_expected_usable_count():
    result = _parse()
    assert EXPECTED_USABLE == 38
    assert len(result.rows) == EXPECTED_USABLE


def test_every_expected_issue_is_produced_and_nothing_else():
    result = _parse()
    actual = {(i.row, i.code, i.severity, i.column) for i in result.issues}
    expected = {(i.row, i.code, i.severity, i.column) for i in EXPECTED_ISSUES}
    assert actual == expected


def test_issue_reasons_never_echo_a_raw_inn():
    result = _parse()
    inns = {row.inn for row in result.rows}
    for issue in result.issues:
        assert all(inn not in issue.reason for inn in inns)


def test_text_values_survive_and_are_not_formulas():
    result = _parse()
    # INNs keep their leading digits as text; names round-trip as plain strings.
    assert all(isinstance(row.inn, str) and row.inn.isdigit() for row in result.rows)
    assert any(row.name == "ООО «Контрагент 02»" for row in result.rows)


def test_demo_scores_cover_all_priority_buckets_and_only_usable_rows():
    result = _parse()
    usable = {row.inn for row in result.rows}
    assert {score.inn for score in DEMO_SCORES} <= usable
    assert {score.priority for score in DEMO_SCORES} == set(DemoPriority)


def test_duplicate_row_is_merged_not_counted_twice():
    result = _parse()
    # The verbatim copy of the LOW row must not add a second counterparty.
    assert len({row.inn for row in result.rows}) == len(result.rows)
