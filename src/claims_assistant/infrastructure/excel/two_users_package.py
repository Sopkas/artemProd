"""S6-03: the files of the two-user manual run as workbooks the bot can be given.

``infrastructure/demo/two_users.py`` says what each user's package holds; this renders it
through the same builders as the templates the dialog sends out, so the files are read by
the contract like a customer's.
"""

from claims_assistant.domain import interactions
from claims_assistant.infrastructure.demo.two_users import STOP_ROWS, USERS, UserPackage, stop_rows
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.ledgers import (
    build_interactions_workbook,
    interaction_cells,
)

STOP_FILE = "ostanovka-kontragenty.xlsx"


def counterparties_file(user: UserPackage) -> str:
    return f"{user.key}-kontragenty.xlsx"


def interactions_file(user: UserPackage) -> str:
    return f"{user.key}-vzaimodeystviya.xlsx"


def file_notes() -> dict[str, str]:
    """What each file is for, in upload order."""
    notes = {}
    for user in USERS:
        notes[counterparties_file(user)] = f"{user.owner}: «Контрагенты», {len(user.rows)} орг."
        notes[interactions_file(user)] = (
            f"{user.owner}: «Взаимодействия», записей: {len(user.interactions)}"
        )
    notes[STOP_FILE] = f"остановка во время анализа: {STOP_ROWS} организаций"
    return notes


def build_two_users_package() -> dict[str, bytes]:
    """Every workbook of the manual run, keyed by the name it is saved under."""
    files = {}
    for user in USERS:
        files[counterparties_file(user)] = build_counterparties_template(user.rows)
        files[interactions_file(user)] = build_interactions_workbook(
            [interaction_cells(row) for row in user.interactions], interactions.COLUMN_TITLES
        )
    files[STOP_FILE] = build_counterparties_template(stop_rows())
    return files
