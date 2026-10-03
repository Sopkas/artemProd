"""Deterministic synthetic scenarios. No network, credentials or wall clock.

One scenario for the whole provider is what a single-purpose demo needs; the defence
package (S6-04) needs four stories in one report, so the scenario can also be chosen
**per INN** (``by_inn``). Without that mapping nothing changes: every company gets the
scenario the provider was built with. ``names`` does the same for the company name, so the
card of an INN from the package shows that company and not one name for all four.
"""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.domain.external import (
    CompanyStatus,
    Coverage,
    DataMode,
    Evidence,
    ExternalSnapshot,
    Fact,
    FactKind,
    FetchStatus,
    Period,
    ProviderError,
    Section,
)


class DemoScenario(StrEnum):
    ORDINARY = "ordinary"
    ALARM = "alarm"
    INCOMPLETE = "incomplete"
    ERROR = "error"


class DemoCompanyDataProvider:
    def __init__(
        self,
        scenario: DemoScenario = DemoScenario.ORDINARY,
        *,
        by_inn: Mapping[str, DemoScenario] | None = None,
        names: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(scenario, DemoScenario):
            raise ValueError("Choose an explicit DemoScenario")
        if by_inn and not all(isinstance(item, DemoScenario) for item in by_inn.values()):
            raise ValueError("Choose an explicit DemoScenario for every INN")
        self.scenario = scenario
        self.by_inn = dict(by_inn or {})
        self.names = dict(names or {})

    def scenario_for(self, inn: str) -> DemoScenario:
        """The story this company tells; the provider's own scenario when it has none."""
        return self.by_inn.get(inn, self.scenario)

    async def fetch(self, request: CompanyDataRequest) -> tuple[ExternalSnapshot, ...]:
        return tuple(self._section(request.inn, section) for section in request.sections)

    def _section(self, inn: str, section: Section) -> ExternalSnapshot:
        scenario = self.scenario_for(inn)
        source = "synthetic-demo-v1"
        fetched_at = datetime(2026, 1, 1, tzinfo=UTC)
        period = Period(date(2025, 1, 1), date(2025, 12, 31))
        base = dict(
            inn=inn,
            section=section,
            source=source,
            mode=DataMode.DEMO,
            fetched_at=fetched_at,
        )
        failed = scenario == DemoScenario.ERROR or (
            scenario == DemoScenario.INCOMPLETE and section == Section.FINANCES
        )
        if failed:
            return ExternalSnapshot(
                **base,
                status=FetchStatus.UNAVAILABLE,
                coverage=Coverage.UNAVAILABLE,
                missing=("Демонстрационный источник недоступен; раздел не проверен.",),
                error=ProviderError("demo_unavailable", "Синтетический пример сбоя источника."),
            )
        if scenario == DemoScenario.INCOMPLETE and section == Section.BANKRUPTCY:
            return ExternalSnapshot(
                **base,
                status=FetchStatus.OK,
                coverage=Coverage.PARTIAL,
                covered_period=Period(date(2025, 12, 1), date(2025, 12, 31)),
                missing=("Демовыборка охватывает только декабрь; остальные месяцы неизвестны.",),
            )
        evidence = Evidence(f"{inn}:{section}:demo-record", source, f"demo:{section}")
        facts = []

        def add(
            kind: FactKind,
            value: str | Decimal | CompanyStatus,
            *,
            unit: str | None = None,
            year: int | None = None,
        ) -> None:
            # ``year`` is an earlier year of the finances; the latest one keeps the plain ID.
            span = period if year is None else Period(date(year, 1, 1), date(year, 12, 31))
            facts.append(
                Fact(
                    id=f"{inn}:{section}:{kind}" + ("" if year is None else f":{year}"),
                    inn=inn,
                    kind=kind,
                    value=value,
                    evidence_ids=(evidence.id,),
                    observed_on=period.end if section != Section.FINANCES else None,
                    period=span if section == Section.FINANCES else None,
                    unit=unit,
                )
            )

        if section == Section.COMPANY:
            add(FactKind.COMPANY_NAME, self.names.get(inn, "ДЕМО — вымышленная компания"))
            add(FactKind.COMPANY_STATUS, CompanyStatus.ACTIVE)
        elif section == Section.FINANCES:
            # Two consecutive years: the base set of the rules needs both (docs/scoring.md),
            # and a «complete» section with one year read as «неполная» without a reason.
            previous = period.end.year - 1
            add(FactKind.REVENUE, Decimal("12000000.00"), unit="RUB", year=previous)
            add(FactKind.NET_PROFIT, Decimal("0.00"), unit="RUB", year=previous)
            add(FactKind.REVENUE, Decimal("12000000.00"), unit="RUB")
            add(FactKind.NET_PROFIT, Decimal("0.00"), unit="RUB")
        elif scenario == DemoScenario.ALARM:
            add(FactKind.BANKRUPTCY_EVENT, "ДЕМО — сообщение о введении наблюдения")
        return ExternalSnapshot(
            **base,
            status=FetchStatus.OK,
            coverage=Coverage.COMPLETE,
            covered_period=None if section == Section.COMPANY else period,
            facts=tuple(facts),
            # Even a complete empty selection has a synthetic source record.
            evidence=(evidence,),
        )
