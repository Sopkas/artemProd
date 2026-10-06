"""S6-03: the files of the manual run with two live Telegram accounts.

The acceptance scenarios (``docs/acceptance-scenarios.md``) are walked by two people at
once, and what they must see is that neither ever gets the other's data. With the bot's
own template sent back by both, the two packages hold the same INNs and a swapped report
looks exactly like the right one. So each user has a package of their own:

- four companies, named after their owner, with priorities the rules produce from the
  rows — a high, a medium and a low in each;
- two interactions that begin with the owner's name, so a foreign line on «Хронология» or
  in a card is seen at once;
- **one INN in both packages, with different figures.** It is the sharpest check: the card
  of that INN must show each user their own debt and their own interaction.

The third file is for «остановка во время анализа». A demo check of a few companies ends
in a fraction of a second and cannot be stopped by hand; ten thousand rows keep it running
for several seconds, long enough to stop the bot in the middle.

Nothing here is real. The INNs pass the checksum and start with ``0000``: no tax office
has that code, so none can belong to a real organisation. Every row carries the cut-off
date ``ANALYSIS_DATE`` — a row with a debt needs one equal to the analysis date — so the
date is typed by hand in the dialog and «Сегодня» would reject the files.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.inn import InvalidInn, validate_inn
from claims_assistant.domain.interactions import InteractionRow
from claims_assistant.domain.scoring import Priority

# Every file is built for this analysis date; it is typed by hand in the dialog.
ANALYSIS_DATE = date(2026, 10, 1)

# In both packages, with different figures: whose data the bot shows is seen by it.
SHARED_INN = "0000330016"

# Rows of the file for «остановка во время анализа»: with RUN_REQUEST_LIMIT=30000 a demo
# check of it runs for about ten seconds.
STOP_ROWS = 10_000


@dataclass(frozen=True, slots=True)
class UserPackage:
    """What one user uploads, and what the rules must make of it."""

    key: str  # the prefix of the user's files
    owner: str  # the name the user's rows and comments carry
    rows: tuple[CounterpartyRow, ...]
    interactions: tuple[InteractionRow, ...]
    expected: dict[str, Priority]  # INN → the priority of the report

    @property
    def inns(self) -> tuple[str, ...]:
        return tuple(row.inn for row in self.rows)


def _row(inn: str, name: str, debt: str, overdue: int, paid: date) -> CounterpartyRow:
    return CounterpartyRow(
        inn=inn,
        name=name,
        cutoff_date=ANALYSIS_DATE,
        debt=Decimal(debt),
        overdue_days=overdue,
        last_payment_date=paid,
    )


ANDREY = UserPackage(
    key="andrey",
    owner="Андрей",
    rows=(
        _row("0000110010", "ООО «Альфа-Снаб» (пакет Андрея)", "900000.00", 75, date(2026, 9, 10)),
        _row("0000110028", "ООО «Бета-Строй» (пакет Андрея)", "300000.00", 40, date(2026, 9, 15)),
        _row(
            "0000110035", "ООО «Гамма-Логистик» (пакет Андрея)", "50000.00", 10, date(2026, 9, 20)
        ),
        _row(
            SHARED_INN, "ООО «Общий контрагент» (как у Андрея)", "111000.00", 35, date(2026, 9, 12)
        ),
    ),
    interactions=(
        InteractionRow(
            inn="0000110010",
            interaction_id="AND-1",
            happened_on=date(2026, 9, 22),
            comment="Запись Андрея: обещали оплатить до 15 октября.",
            channel="телефон",
        ),
        InteractionRow(
            inn=SHARED_INN,
            interaction_id="AND-2",
            happened_on=date(2026, 9, 24),
            comment="Запись Андрея: просят рассрочку на два месяца.",
            channel="письмо",
        ),
    ),
    expected={
        "0000110010": Priority.HIGH,  # overdue 75
        "0000110028": Priority.MEDIUM,  # overdue 40
        "0000110035": Priority.LOW,  # overdue 10, the base set is complete
        SHARED_INN: Priority.MEDIUM,  # overdue 35
    },
)

SERGEY = UserPackage(
    key="sergey",
    owner="Сергей",
    rows=(
        _row(
            "0000220013", "ООО «Дельта-Маркет» (пакет Сергея)", "1500000.00", 90, date(2026, 9, 5)
        ),
        _row("0000220020", "ООО «Эпсилон-Агро» (пакет Сергея)", "420000.00", 45, date(2026, 9, 18)),
        _row("0000220038", "ООО «Дзета-Сервис» (пакет Сергея)", "70000.00", 5, date(2026, 9, 25)),
        _row(
            SHARED_INN, "ООО «Общий контрагент» (как у Сергея)", "222000.00", 65, date(2026, 9, 8)
        ),
    ),
    interactions=(
        InteractionRow(
            inn="0000220013",
            interaction_id="SER-1",
            happened_on=date(2026, 9, 21),
            comment="Запись Сергея: директор не выходит на связь.",
            channel="телефон",
        ),
        InteractionRow(
            inn=SHARED_INN,
            interaction_id="SER-2",
            happened_on=date(2026, 9, 26),
            comment="Запись Сергея: гарантийное письмо получено.",
            channel="почта",
        ),
    ),
    expected={
        "0000220013": Priority.HIGH,  # overdue 90
        "0000220020": Priority.MEDIUM,  # overdue 45
        "0000220038": Priority.LOW,  # overdue 5, the base set is complete
        SHARED_INN: Priority.HIGH,  # overdue 65
    },
)

USERS: tuple[UserPackage, ...] = (ANDREY, SERGEY)


def _synthetic_inn(prefix: str) -> str:
    """Nine digits and the control digit that makes them a valid company INN."""
    for digit in "0123456789":
        try:
            return validate_inn(prefix + digit)
        except InvalidInn:
            continue
    raise ValueError("No control digit fits the prefix")


def stop_rows(count: int = STOP_ROWS) -> tuple[CounterpartyRow, ...]:
    """Enough companies to keep a demo check running while the bot is stopped."""
    return tuple(
        _row(
            _synthetic_inn(f"0000{50000 + index:05d}"),
            f"ООО «Нагрузка {index + 1}» (тест остановки)",
            f"{1000 * (index % 500 + 1)}.00",
            (index * 7) % 100,
            date(2026, 9, 1),
        )
        for index in range(count)
    )
