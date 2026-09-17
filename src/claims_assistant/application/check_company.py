"""Single-company check scenario (S1-03): validate the INN, fetch every section."""

from dataclasses import dataclass

from claims_assistant.domain.external import DataMode, ExternalSnapshot
from claims_assistant.domain.inn import validate_legal_inn

from .company_data import CompanyDataProvider, CompanyDataRequest, RequestLimits


@dataclass(frozen=True, slots=True)
class CompanyCheck:
    inn: str
    snapshots: tuple[ExternalSnapshot, ...]

    @property
    def mode(self) -> DataMode:
        return self.snapshots[0].mode


async def check_company(
    raw_inn: str,
    provider: CompanyDataProvider,
    *,
    limits: RequestLimits = RequestLimits(),
) -> CompanyCheck:
    """Raise InvalidInn for a bad INN; source failures come back inside the snapshots."""
    inn = validate_legal_inn(raw_inn)
    snapshots = await provider.fetch(CompanyDataRequest(inn=inn, limits=limits))
    return CompanyCheck(inn=inn, snapshots=snapshots)
