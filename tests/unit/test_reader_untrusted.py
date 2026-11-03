"""A sheet is read as its cells are stored, whatever the file says about itself (review g6).

Every file here is synthetic: the bot's own template, or the .xls fixture, with one record
changed by hand the way a careless generator — or a hostile one — would write it.
"""

import io
import re
import struct
import zipfile
from pathlib import Path

import pytest

from claims_assistant.application.imports import CorruptWorkbook, import_counterparties
from claims_assistant.domain.counterparties import ImportLimits
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.reader import MAX_COLUMNS, OpenpyxlSheetReader

LEGACY = Path(__file__).parent.parent / "fixtures" / "excel" / "legacy.xls"
SHEET_XML = "xl/worksheets/sheet1.xml"


def _edit_sheet(data: bytes, edit) -> bytes:
    """The same .xlsx with its first sheet's XML passed through ``edit``."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == SHEET_XML:
                content = edit(content.decode()).encode()
            target.writestr(info, content)
    return out.getvalue()


def _dimension(ref: str) -> bytes:
    return _edit_sheet(
        build_counterparties_template(),
        lambda xml: re.sub(r'<dimension ref="[^"]*"', f'<dimension ref="{ref}"', xml),
    )


def _rk(
    data: bytes,
    row: int,
    column: int,
    *,
    to: tuple[int, int] | None = None,
    value: int | None = None,
) -> bytes:
    """The .xls with one RK (number) record moved, or given another whole number."""
    for found in re.finditer(re.escape(b"\x7e\x02\x0a\x00"), data):
        at = found.start() + 4
        cell_row, cell_column, xf, rk = struct.unpack("<HHHI", data[at : at + 10])
        if (cell_row, cell_column) == (row, column):
            if value is not None:
                rk = (value << 2) | 0x02  # an integer RK
            record = struct.pack("<HHHI", *(to or (row, column)), xf, rk)
            return data[:at] + record + data[at + 10 :]
    raise AssertionError(f"no RK record at {row}:{column}")


# --- .xlsx: <dimension> is a claim, not the truth ---


@pytest.mark.parametrize("ref", ["A1", "A1:F2", "A1:B3", "A1:F3"])
def test_rows_and_columns_past_a_wrong_dimension_are_still_read(ref):
    """An understated dimension used to drop data without a word: «A1» read as empty."""
    result = import_counterparties(OpenpyxlSheetReader(), _dimension(ref))
    assert [row.inn for row in result.rows] == ["7707083893", "7710140679"]
    assert result.rows[1].debt is not None  # columns past «B» are there too
    reader = OpenpyxlSheetReader()
    outline = reader.read_outline(_dimension(ref), ImportLimits())
    for read in (
        reader.read_rows(_dimension(ref), ImportLimits()),
        [cells for _indent, cells in outline],
    ):
        assert len(read) == 3 and all(len(cells) == 6 for cells in read)


def test_an_inflated_dimension_does_not_pad_every_row_to_16384_cells():
    """«A1:XFD…» in a few kilobytes used to cost ~150 KB of memory a row."""
    rows = "".join(f'<row r="{i}"><c r="A{i}"><v>1</v></c></row>' for i in range(1, 501))
    data = _edit_sheet(
        build_counterparties_template(),
        lambda xml: re.sub(
            r"<sheetData>.*</sheetData>",
            f"<sheetData>{rows}</sheetData>",
            re.sub(r'<dimension ref="[^"]*"', '<dimension ref="A1:XFD500"', xml),
        ),
    )
    read = OpenpyxlSheetReader().read_rows(data, ImportLimits())
    assert len(read) == 500 and {len(cells) for cells in read} == {1}


def test_a_sheet_is_not_read_wider_than_the_cap():
    """A cell at XFD in every row: the row is cut, not padded to 16 384 cells."""
    rows = "".join(
        f'<row r="{i}"><c r="A{i}"><v>1</v></c><c r="XFD{i}"><v>2</v></c></row>'
        for i in range(1, 51)
    )
    data = _edit_sheet(
        build_counterparties_template(),
        lambda xml: re.sub(r"<sheetData>.*</sheetData>", f"<sheetData>{rows}</sheetData>", xml),
    )
    reader = OpenpyxlSheetReader()
    assert {len(cells) for cells in reader.read_rows(data, ImportLimits())} == {MAX_COLUMNS}
    outline = reader.read_outline(data, ImportLimits())
    assert {len(cells) for _indent, cells in outline} == {MAX_COLUMNS}


# --- a sheet that breaks while it is read is a broken file, not a crash ---


@pytest.mark.parametrize(
    "edit",
    [
        lambda xml: xml[: len(xml) // 2],  # cut off in the middle
        lambda xml: xml.replace("<v>45</v>", "<v>4x5</v>"),  # a number that is none
    ],
    ids=["truncated", "bad-number"],
)
def test_broken_sheet_content_is_a_file_level_issue(edit):
    data = _edit_sheet(build_counterparties_template(), edit)
    result = import_counterparties(OpenpyxlSheetReader(), data)
    assert result.rows == () and len(result.issues) == 1
    reader = OpenpyxlSheetReader()
    with pytest.raises(CorruptWorkbook):
        reader.read_rows(data, ImportLimits())
    with pytest.raises(CorruptWorkbook):
        reader.read_outline(data, ImportLimits())


@pytest.mark.parametrize("serial", [15, 3_000_000])  # ambiguous in 1900; past year 9999
def test_a_legacy_date_cell_that_is_no_date_comes_back_as_its_number(serial):
    data = _rk(LEGACY.read_bytes(), 1, 2, value=serial)
    reader = OpenpyxlSheetReader()
    assert reader.read_rows(data, ImportLimits())[1][2] == serial
    assert reader.read_outline(data, ImportLimits())[1][1][2] == serial


# --- .xls: one cell in the far corner does not build the whole grid ---


def test_a_far_legacy_cell_does_not_widen_every_row():
    """Moved to (65535, 255): xlrd used to pad all 65 536 rows to 256 columns."""
    data = _rk(LEGACY.read_bytes(), 4, 1, to=(65535, 255))
    reader = OpenpyxlSheetReader()
    limits = ImportLimits(max_rows=100)
    rows = reader.read_rows(data, limits)
    assert {len(cells) for cells in rows} == {5}  # the rows read are as wide as the table
    assert rows[1][0] == "1234567894"
    outline = reader.read_outline(data, limits)
    assert {len(cells) for _indent, cells in outline} == {5}
    with OpenpyxlSheetReader._xls(data, ImportLimits(), on_demand=True) as book:
        sheet = book.sheet_by_index(0)
        stored = sum(sheet.row_len(index) for index in range(sheet.nrows))
    assert stored < 1_000  # ragged: the cells the file holds, not 65 536 × 256 of them
