"""S6-04: the package the product is shown on — four counterparties, four stories.

The control package (S2-06) has fifty rows and predefined scores: it proves the parser
works. This one is the opposite — four rows, no predefined anything. Every priority here
is produced by the real rules (``docs/scoring.md``) from the files and the demo source, so
what the audience sees on the screen is what the product actually decides.

The four cases are the ones the roadmap asks for, and each exists to show a different
thing:

- **обычный должник** — the ordinary case: an overdue of a month, a priority of medium,
  and a card that reads like a normal working day;
- **серьёзный сигнал** — a long overdue and a bankruptcy message in the register: the
  register answers by INN whatever the role, so the message raises the priority and is
  shown as something to read, never as «этот контрагент банкротится»;
- **ухудшение платежей** — the point of the whole package. By its own row this company
  looks calm: twenty days overdue, below every threshold. The payments export and the debt
  history turn it into a high priority — no payments at all since June and a debt that
  doubled in a month. One file changes the answer, and that is the argument for the
  optional files;
- **неполные данные** — only an INN, and the external source that cannot answer either.
  The product says what it does not know instead of guessing, and the priority is
  «unknown», not «low».

Nothing here is real. The names are invented, the INNs are synthetic (valid by checksum
only), and the amounts are round numbers chosen to be readable from the back of a room.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from claims_assistant.domain.counterparties import CounterpartyRow
from claims_assistant.domain.debt_history import DebtSnapshot
from claims_assistant.domain.interactions import InteractionRow
from claims_assistant.domain.payments import PaymentRow
from claims_assistant.infrastructure.demo.company_data import DemoScenario

# Everything in the package is built for this analysis date; the demo is shown with it.
ANALYSIS_DATE = date(2026, 9, 1)

# The period the payments export is vouched for: it reaches the analysis date, so an
# absence of payments inside it is a fact, not a gap (docs/data-contracts.md §2).
PAYMENTS_FROM = date(2026, 6, 1)
PAYMENTS_TO = ANALYSIS_DATE


@dataclass(frozen=True, slots=True)
class DemoCompany:
    """One counterparty of the demo, with everything the package says about it."""

    key: str
    row: CounterpartyRow
    scenario: DemoScenario  # what the demo source answers for this INN
    expected_priority: str  # what the rules must produce; the test holds us to it
    story: str  # what is said about it while it is on the screen
    payments: tuple[PaymentRow, ...] = ()
    history: tuple[DebtSnapshot, ...] = ()
    interactions: tuple[InteractionRow, ...] = ()


ORDINARY_INN = "7701234560"
ALARM_INN = "7712345671"
WORSENING_INN = "7723456782"
INCOMPLETE_INN = "7734567893"


COMPANIES: tuple[DemoCompany, ...] = (
    DemoCompany(
        key="ordinary",
        row=CounterpartyRow(
            inn=ORDINARY_INN,
            name="ООО «Ромашка» (демо)",
            cutoff_date=ANALYSIS_DATE,
            debt=Decimal("180000.00"),
            overdue_days=35,
            last_payment_date=date(2026, 8, 20),
        ),
        scenario=DemoScenario.ORDINARY,
        expected_priority="medium",
        story="Обычная просрочка чуть больше месяца. Компания действует, платит, "
        "но с опозданием: средний приоритет, работать в обычном порядке.",
        payments=(
            PaymentRow(ORDINARY_INN, "PL-1001", date(2026, 6, 15), Decimal("120000.00")),
            PaymentRow(ORDINARY_INN, "PL-1002", date(2026, 7, 18), Decimal("95000.00")),
            PaymentRow(ORDINARY_INN, "PL-1003", date(2026, 8, 20), Decimal("140000.00")),
        ),
        history=(
            DebtSnapshot(ORDINARY_INN, date(2026, 8, 1), Decimal("205000.00")),
            DebtSnapshot(ORDINARY_INN, ANALYSIS_DATE, Decimal("180000.00")),
        ),
        interactions=(
            InteractionRow(
                inn=ORDINARY_INN,
                interaction_id="INT-101",
                happened_on=date(2026, 8, 12),
                comment="Созвонились с бухгалтерией, оплату обещают после 20 августа.",
                channel="телефон",
            ),
        ),
    ),
    DemoCompany(
        key="alarm",
        row=CounterpartyRow(
            inn=ALARM_INN,
            name="ООО «Вектор-Лизинг» (демо)",
            cutoff_date=ANALYSIS_DATE,
            debt=Decimal("2400000.00"),
            overdue_days=95,
            last_payment_date=date(2026, 6, 5),
        ),
        scenario=DemoScenario.ALARM,
        expected_priority="high",
        story="Просрочка больше трёх месяцев, и в реестре есть сообщение о банкротстве. "
        "Важно сказать вслух: реестр отвечает по ИНН независимо от роли — сообщение "
        "поднимает приоритет и требует прочтения, но само по себе не означает, что "
        "банкротится именно этот контрагент.",
        payments=(PaymentRow(ALARM_INN, "PL-2001", date(2026, 6, 5), Decimal("300000.00")),),
        history=(
            DebtSnapshot(ALARM_INN, date(2026, 8, 1), Decimal("2150000.00")),
            DebtSnapshot(ALARM_INN, ANALYSIS_DATE, Decimal("2400000.00")),
        ),
        interactions=(
            InteractionRow(
                inn=ALARM_INN,
                interaction_id="INT-201",
                happened_on=date(2026, 7, 3),
                comment="Письмо с требованием оплаты отправлено, ответа нет.",
                channel="почта",
            ),
            InteractionRow(
                inn=ALARM_INN,
                interaction_id="INT-202",
                happened_on=date(2026, 8, 19),
                comment="Телефон не отвечает вторую неделю.",
                channel="телефон",
            ),
        ),
    ),
    DemoCompany(
        key="worsening",
        row=CounterpartyRow(
            inn=WORSENING_INN,
            name="ООО «Северный путь» (демо)",
            cutoff_date=ANALYSIS_DATE,
            debt=Decimal("950000.00"),
            overdue_days=20,  # below every threshold: the row alone says «спокойно»
            last_payment_date=None,
        ),
        scenario=DemoScenario.ORDINARY,
        expected_priority="high",
        story="Главный файл говорит «20 дней, ничего страшного». Выгрузка платежей и "
        "история долга говорят другое: поступлений нет с начала июня, а долг за месяц "
        "вырос вдвое. Два средних сигнала складываются в высокий приоритет — и это "
        "ответ на вопрос, зачем нужны необязательные файлы.",
        payments=(),  # the export covers June to the analysis date and is empty
        history=(
            DebtSnapshot(WORSENING_INN, date(2026, 8, 1), Decimal("430000.00")),
            DebtSnapshot(WORSENING_INN, ANALYSIS_DATE, Decimal("950000.00")),
        ),
        interactions=(
            InteractionRow(
                inn=WORSENING_INN,
                interaction_id="INT-301",
                happened_on=date(2026, 8, 27),
                comment="Просят отсрочку до конца квартала, сумму не называют.",
                channel="письмо",
            ),
        ),
    ),
    DemoCompany(
        key="incomplete",
        row=CounterpartyRow(inn=INCOMPLETE_INN, name="ООО «Тихое» (демо)"),
        scenario=DemoScenario.INCOMPLETE,
        expected_priority="unknown",
        story="В файле только ИНН, и внешний источник тоже ответил не всё. "
        "Приоритет — «неизвестно», а не «низкий»: продукт говорит, чего он не знает, "
        "и перечисляет, каких данных не хватает.",
    ),
)

BY_KEY = {company.key: company for company in COMPANIES}

# What the demo source answers for each of them: one package, four different stories.
PACKAGE_SCENARIOS: dict[str, DemoScenario] = {
    company.row.inn: company.scenario for company in COMPANIES
}


def counterparty_rows() -> tuple[CounterpartyRow, ...]:
    return tuple(company.row for company in COMPANIES)


def payment_rows() -> tuple[PaymentRow, ...]:
    return tuple(row for company in COMPANIES for row in company.payments)


def debt_snapshots() -> tuple[DebtSnapshot, ...]:
    return tuple(row for company in COMPANIES for row in company.history)


def interaction_rows() -> tuple[InteractionRow, ...]:
    return tuple(row for company in COMPANIES for row in company.interactions)
