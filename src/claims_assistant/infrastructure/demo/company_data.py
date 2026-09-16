"""Deterministic synthetic scenarios. No network, credentials or wall clock."""

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from claims_assistant.application.company_data import CompanyDataRequest
from claims_assistant.domain.external import (
    Coverage,
    DataMode,
    Evidence,
    ExternalSnapshot,
    Fact,
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
                coverage=Coverage.PARTIAL,
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

        def add(kind: str, value: str | Decimal, *, unit: str | None = None) -> None:
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
            add("company_name", "ДЕМО — вымышленная компания")
            add("company_status", "active")
        elif section == Section.FINANCES:
            add("revenue", Decimal("12000000.00"), unit="RUB")
            add("net_profit", Decimal("0.00"), unit="RUB")
        elif self.scenario == DemoScenario.ALARM:
            add("bankruptcy_event", "ДЕМО — сообщение о введении наблюдения")
        return ExternalSnapshot(
            **base,
            status=FetchStatus.OK,
            coverage=Coverage.COMPLETE,
            covered_period=period,
            facts=tuple(facts),
            # Even a complete empty selection has a synthetic source record.
            evidence=(evidence,),
        )
