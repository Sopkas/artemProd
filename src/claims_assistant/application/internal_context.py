"""Internal data of one company for the INN card (S4-04).

The card is built from external sources; when the owner has a finished check whose
package names the same INN, the card also shows what the owner's own files say: the
internal indicators (S4-06) and the chronology of interactions (S4-02). The package is
re-read from storage through the same review as the pipeline, so the card and the report
cannot disagree. Only the owner's checks are searched — the repository is owner-scoped —
and the newest finished check that contains the INN wins.
"""

from dataclasses import dataclass

from claims_assistant.domain.analysis import AnalysisRun, FileKind, RunStatus
from claims_assistant.domain.counterparties import CounterpartyRow, ImportLimits
from claims_assistant.domain.debt_report import ContractDebt
from claims_assistant.domain.external import ExternalSnapshot, Period
from claims_assistant.domain.indicators import InternalIndicators, internal_indicators
from claims_assistant.domain.interactions import InteractionRow, chronology
from claims_assistant.domain.report import file_labels

from .analysis_repository import AnalysisRepository
from .check_package import FileStorage, StorageError, package_inns
from .contract_link import link_contracts
from .imports import SheetReader
from .package_checks import PackageIntegrityError, review_package

FINISHED = frozenset({RunStatus.COMPLETED, RunStatus.PARTIAL})
_NEWEST_RUNS = 5  # how many of the owner's finished checks are searched for the INN


@dataclass(frozen=True, slots=True)
class InternalContext:
    run: AnalysisRun
    row: CounterpartyRow  # the owner's line on this company
    indicators: InternalIndicators
    interactions: tuple[InteractionRow, ...]  # this INN only, in date order
    files: tuple[str, ...]  # labels of the package files the data came from
    # S7-01: contracts of the overdue report tied to this company by name, worst first.
    contracts: tuple[ContractDebt, ...] = ()


def payment_periods(run: AnalysisRun) -> tuple[Period, ...]:
    return tuple(
        file.coverage
        for file in run.files
        if file.kind is FileKind.PAYMENTS and file.coverage is not None
    )


async def internal_context(
    owner_id: int,
    inn: str,
    repository: AnalysisRepository,
    files: FileStorage,
    reader: SheetReader,
    *,
    finances: ExternalSnapshot | None = None,
    company_name: str | None = None,
    limits: ImportLimits = ImportLimits(),
) -> InternalContext | None:
    """Indicators and chronology of ``inn`` from the owner's newest finished check, or None.

    ``finances`` is the card's fresh external snapshot of the finances section, so the
    revenue indicator is computed from the same facts the card shows. ``company_name`` is
    what the source calls this company: the overdue report (S7-01) names no INN, so its
    contracts are tied to the card by name, exactly as the report does it.
    """
    runs = [run for run in await repository.list_runs(owner_id) if run.status in FINISHED]
    for run in runs[:_NEWEST_RUNS]:
        # Cheap first: only the «Контрагенты» file says whether the INN is in the package;
        # the optional files are parsed once, for the check that has it.
        try:
            if inn not in await package_inns(run, files, reader, limits):
                continue
            review = await review_package(run, files, reader, limits)
        except (PackageIntegrityError, StorageError):
            continue  # a broken package is the pipeline's business, not the card's
        row = next((item for item in review.counterparties if item.inn == inn), None)
        if row is None:
            continue
        indicators = internal_indicators(
            row,
            run.analysis_date,
            review.payments,
            payment_periods(run),
            review.history,
            finances,
        )
        interactions = chronology(review.interactions).get(inn, ())
        # The overdue report names companies, not INNs; the card links them the same way
        # the report does, and shows nothing when the name did not match (S7-01).
        linked = link_contracts(review.contracts, {inn: company_name})
        contracts = sorted(linked.by_inn.get(inn, ()), key=lambda c: (-(c.days or 0), c.name))
        return InternalContext(
            run=run,
            row=row,
            indicators=indicators,
            interactions=interactions,
            files=tuple(label for _, label in file_labels(run.files)),
            contracts=tuple(contracts),
        )
    return None
