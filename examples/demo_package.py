"""S6-04: write the demo package to disk, ready to be sent to the bot.

    python examples/demo_package.py            # в examples/demo/
    python examples/demo_package.py --out /tmp/demo
    python examples/demo_package.py --what     # только показать состав

Run it from the repository root; the script finds ``src`` itself, so no PYTHONPATH (which
PowerShell would not take as a prefix anyway).

Four counterparties, four stories, no network and no clock: the same bytes every time, so
a rehearsal and the defence itself show the same numbers. What each file is for and what
to say while it is on the screen — `docs/demo-script.md`.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claims_assistant.infrastructure.demo.package import (  # noqa: E402
    ANALYSIS_DATE,
    COMPANIES,
    PAYMENTS_FROM,
    PAYMENTS_TO,
)
from claims_assistant.infrastructure.excel.demo_package import (  # noqa: E402
    FILE_NOTES,
    build_demo_package,
)
from claims_assistant.infrastructure.excel.report import PRIORITY_LABELS  # noqa: E402

SETTINGS = "DATA_PROVIDER=demo, DEMO_PACKAGE=true, AI_PROVIDER=off"


def how_to_enter() -> str:
    """What the presenter types: the package holds with this date and this period only."""
    return (
        f"Дата анализа: {ANALYSIS_DATE:%d.%m.%Y} — ввести руками, не «Сегодня».\n"
        "«Добавить платежи» → «По нашему шаблону» → период "
        f"{PAYMENTS_FROM:%d.%m.%Y}–{PAYMENTS_TO:%d.%m.%Y}."
    )


def describe() -> None:
    print(how_to_enter() + "\n")
    for company in COMPANIES:
        name = company.row.name or company.row.inn
        # The priority in the words of the report, and the INN for «/inn».
        print(f"{PRIORITY_LABELS[company.expected_priority]} — {name}, ИНН {company.row.inn}")
        print(f"    {company.story}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Демонстрационный пакет (S6-04)")
    parser.add_argument("--out", default=str(ROOT / "examples" / "demo"), help="куда сложить")
    parser.add_argument("--what", action="store_true", help="показать состав и выйти")
    args = parser.parse_args()

    if args.what:
        describe()
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in build_demo_package().items():
        (out / name).write_bytes(data)
        print(f"{name:32} {len(data):>7} Б  — {FILE_NOTES[name]}")
    print(f"\nГотово: {out}")
    print(how_to_enter())
    print(f"Показ: {SETTINGS}. Сценарий — docs/demo-script.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
