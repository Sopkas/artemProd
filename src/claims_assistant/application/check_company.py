"""Single-company check scenario (S1-03): validate the INN, fetch every section."""

from dataclasses import dataclass

from claims_assistant.domain.external import DataMode, ExternalSnapshot
from claims_assistant.domain.inn import validate_inn

from .company_data import CompanyDataProvider, CompanyDataRequest, RequestLimits


@dataclass(frozen=True, slots=True)
class CompanyCheck:
    inn: str
    snapshots: tuple[ExternalSnapshot, ...]

    def __post_init__(self) -> None:
        if not self.snapshots:
            raise ValueError("Company check must contain snapshots")
        if any(snapshot.inn != self.inn for snapshot in self.snapshots):
            raise ValueError("Snapshots must belong to the requested company")
        if len({snapshot.mode for snapshot in self.snapshots}) != 1:
            raise ValueError("Demo and live snapshots cannot be mixed")
        if len({snapshot.section for snapshot in self.snapshots}) != len(self.snapshots):
            raise ValueError("Snapshot sections must be unique")

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
    inn = validate_inn(raw_inn)
    request = CompanyDataRequest(inn=inn, limits=limits)
    snapshots = await provider.fetch(request)
    if tuple(snapshot.section for snapshot in snapshots) != request.sections:
        raise ValueError("Provider must return every requested section in order")
    return CompanyCheck(inn=inn, snapshots=snapshots)
