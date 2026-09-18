"""Shared shape of one import problem (S2-03 contract, agreed in PR #6).

Parsers (S2-04 and the later sheets) produce issues without file_id; the upload
scenario attaches the stored file's id. The reason is for people and must never contain
cell contents or correspondence; the code is for programs and must stay stable.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

_CODE = re.compile(r"[a-z][a-z0-9_]*")
_COLUMN = re.compile(r"[A-Z]{1,3}")


class IssueSeverity(StrEnum):
    ERROR = "error"  # the row or file is unusable
    WARNING = "warning"  # accepted with a remark (merged duplicate, ignored column)


@dataclass(frozen=True, slots=True)
class ImportIssue:
    code: str
    severity: IssueSeverity
    sheet: str
    reason: str
    file_id: str | None = None
    row: int | None = None  # Excel row number starting at 1; None for sheet/file issues
    column: str | None = None  # Excel column letters; None for row-level issues

    def __post_init__(self) -> None:
        if not _CODE.fullmatch(self.code):
            raise ValueError("Issue code must be a stable snake_case identifier")
        if not isinstance(self.severity, IssueSeverity):
            raise ValueError("Severity must be an IssueSeverity")
        if not self.sheet or not self.reason.strip():
            raise ValueError("Sheet and reason must not be empty")
        if self.file_id is not None and not self.file_id:
            raise ValueError("file_id must be None or a non-empty identifier")
        if self.row is not None and (type(self.row) is not int or self.row < 1):
            raise ValueError("Row must be an Excel row number starting at 1")
        if self.column is not None:
            if self.row is None:
                raise ValueError("A column needs a row")
            if not _COLUMN.fullmatch(self.column):
                raise ValueError("Column must be Excel letters such as A or AB")
