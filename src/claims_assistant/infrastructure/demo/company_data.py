"""Deterministic synthetic scenarios. No network, credentials or wall clock."""

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
    def __init__(self, scenario: DemoScenario = DemoScenario.ORDINARY) -> None:
        if not isinstance(scenario, DemoScenario):
            raise ValueError("Choose an explicit DemoScenario")
        self.scenario = scenario

    async def fetch(self, request: CompanyDataRequest) -> tuple[ExternalSnapshot, ...]:
        return tuple(self._section(request.inn, section) for section in request.sections)

    def _section(self, inn: str, section: Section) -> ExternalSnapshot:
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
        failed = self.scenario == DemoScenario.ERROR or (
            self.scenario == DemoScenario.INCOMPLETE and section == Section.FINANCES
        )
        if failed:
            return ExternalSnapshot(
                **base,
                status=FetchStatus.UNAVAILABLE,
                coverage=Coverage.UNAVAILABLE,
                missing=("Демонстрационный источник недоступен; раздел не проверен.",),
                error=ProviderError("demo_unavailable", "Синтетический пример сбоя источника."),
            )
        if self.scenario == DemoScenario.INCOMPLETE and section == Section.BANKRUPTCY:
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
            kind: FactKind, value: str | Decimal | CompanyStatus, *, unit: str | None = None
        ) -> None:
            facts.append(
                Fact(
                    id=f"{inn}:{section}:{kind}",
                    inn=inn,
                    kind=kind,
                    value=value,
                    evidence_ids=(evidence.id,),
                    observed_on=period.end if section != Section.FINANCES else None,
                    period=period if section == Section.FINANCES else None,
                    unit=unit,
                )
            )

        if section == Section.COMPANY:
            add(FactKind.COMPANY_NAME, "ДЕМО — вымышленная компания")
            add(FactKind.COMPANY_STATUS, CompanyStatus.ACTIVE)
        elif section == Section.FINANCES:
            add(FactKind.REVENUE, Decimal("12000000.00"), unit="RUB")
            add(FactKind.NET_PROFIT, Decimal("0.00"), unit="RUB")
        elif self.scenario == DemoScenario.ALARM:
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
