"""S6-04: write the demo package to disk, ready to be sent to the bot.

    PYTHONPATH=src python examples/demo_package.py            # в examples/demo/
    PYTHONPATH=src python examples/demo_package.py --out /tmp/demo
    PYTHONPATH=src python examples/demo_package.py --what     # только показать состав

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


def describe() -> None:
    day = ANALYSIS_DATE.strftime("%d.%m.%Y")
    print(f"Дата анализа пакета: {day}")
    print(f"Период полноты выгрузки платежей: {PAYMENTS_FROM:%d.%m.%Y}–{PAYMENTS_TO:%d.%m.%Y}\n")
    for company in COMPANIES:
        name = company.row.name or company.row.inn
        print(f"{company.expected_priority.upper():8} {name}")
        print(f"         {company.story}\n")


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
    print(
        "Показ: DATA_PROVIDER=demo, DEMO_PACKAGE=true, AI_PROVIDER=stub. "
        "Сценарий — docs/demo-script.md."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
