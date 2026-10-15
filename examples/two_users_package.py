"""S6-03: write the files of the two-user manual run to disk.

    python examples/two_users_package.py            # в examples/demo/s6-03/
    python examples/two_users_package.py --out /tmp/s6-03
    python examples/two_users_package.py --what     # только показать состав

Run it from the repository root; the script finds ``src`` itself, so no PYTHONPATH.

Each of the two users gets a package of their own, with one INN in both, and there is one
large file for stopping the bot in the middle of a check. Who sends what and what to look
at — `docs/acceptance-scenarios.md`.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claims_assistant.infrastructure.demo.two_users import (  # noqa: E402
    ANALYSIS_DATE,
    SHARED_INN,
    USERS,
)
from claims_assistant.infrastructure.excel.report import PRIORITY_LABELS  # noqa: E402
from claims_assistant.infrastructure.excel.two_users_package import (  # noqa: E402
    build_two_users_package,
    file_notes,
)

SETTINGS = "DATA_PROVIDER=demo, DEMO_PACKAGE=false, AI_PROVIDER=off, RUN_REQUEST_LIMIT=30000"


def how_to_enter() -> str:
    """What both users type: the files hold with this analysis date only."""
    return f"Дата анализа: {ANALYSIS_DATE:%d.%m.%Y} — ввести руками, не «Сегодня»."


def describe() -> None:
    print(how_to_enter())
    print(f"Общий ИНН в обоих пакетах: {SHARED_INN}\n")
    for user in USERS:
        print(f"{user.owner}:")
        for row in user.rows:
            label = PRIORITY_LABELS[user.expected[row.inn]]
            figures = f"долг {row.debt}, просрочка {row.overdue_days}"
            print(f"    {row.inn}  {label:8}  {row.name}, {figures}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Файлы ручного прогона двух пользователей (S6-03)")
    parser.add_argument(
        "--out", default=str(ROOT / "examples" / "demo" / "s6-03"), help="куда сложить"
    )
    parser.add_argument("--what", action="store_true", help="показать состав и выйти")
    args = parser.parse_args()

    if args.what:
        describe()
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    notes = file_notes()
    for name, data in build_two_users_package().items():
        (out / name).write_bytes(data)
        print(f"{name:30} {len(data):>7} Б  — {notes[name]}")
    print(f"\nГотово: {out}")
    print(how_to_enter())
    print(f"Прогон: {SETTINGS}. Сценарии — docs/acceptance-scenarios.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
