import pytest

from claims_assistant.application.check_company import CompanyCheck, check_company
from claims_assistant.application.company_data import CompanyDataRequest, RequestLimits
from claims_assistant.domain.external import DataMode, FetchStatus, Section
from claims_assistant.domain.inn import InnProblem, InvalidInn
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario

# Synthetic identifiers for the checksum, not real organizations.
VALID_INN = "1234567894"
ZERO_INN = "0000000000"


async def test_valid_inn_is_normalized_and_all_sections_are_fetched():
    check = await check_company(f" {VALID_INN}\n", DemoCompanyDataProvider())
    assert isinstance(check, CompanyCheck)
    assert check.inn == VALID_INN
    assert [snapshot.section for snapshot in check.snapshots] == list(Section)
    assert all(snapshot.inn == VALID_INN for snapshot in check.snapshots)
    assert check.mode == DataMode.DEMO


async def test_demo_zero_inn_passes_end_to_end():
    check = await check_company(ZERO_INN, DemoCompanyDataProvider(DemoScenario.ALARM))
    assert check.inn == ZERO_INN


@pytest.mark.parametrize(
    ("raw", "problem"), [("123", InnProblem.WRONG_LENGTH), ("", InnProblem.EMPTY)]
)
async def test_invalid_inn_is_rejected_before_the_provider_is_called(raw, problem):
    class ExplodingProvider:
        async def fetch(self, request: CompanyDataRequest):
            raise AssertionError("Provider must not be called for an invalid INN")

    with pytest.raises(InvalidInn) as info:
        await check_company(raw, ExplodingProvider())
    assert info.value.problem is problem


async def test_source_failure_is_part_of_the_result_not_an_exception():
    check = await check_company(VALID_INN, DemoCompanyDataProvider(DemoScenario.ERROR))
    assert all(snapshot.status == FetchStatus.UNAVAILABLE for snapshot in check.snapshots)
    assert all(snapshot.error is not None for snapshot in check.snapshots)


async def test_programming_errors_in_the_provider_propagate():
    class BrokenProvider:
        async def fetch(self, request: CompanyDataRequest):
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await check_company(VALID_INN, BrokenProvider())


async def test_limits_are_passed_to_the_provider():
    seen: list[CompanyDataRequest] = []

    class RecordingProvider:
        async def fetch(self, request: CompanyDataRequest):
            seen.append(request)
            return await DemoCompanyDataProvider().fetch(request)

    limits = RequestLimits(timeout_seconds=5, max_requests=3, max_pages=2)
    await check_company(VALID_INN, RecordingProvider(), limits=limits)
    assert seen[0].limits == limits
    assert seen[0].sections == tuple(Section)


@pytest.mark.parametrize("variant", ["empty", "missing", "wrong_inn", "mixed_mode", "duplicate"])
async def test_provider_contract_violation_is_rejected(variant):
    from dataclasses import replace

    class InvalidProvider:
        async def fetch(self, request):
            snapshots = await DemoCompanyDataProvider().fetch(request)
            if variant == "empty":
                return ()
            if variant == "missing":
                return snapshots[:1]
            if variant == "wrong_inn":
                return await DemoCompanyDataProvider().fetch(CompanyDataRequest(inn=ZERO_INN))
            if variant == "mixed_mode":
                return (replace(snapshots[0], mode=DataMode.LIVE), *snapshots[1:])
            return (snapshots[0], snapshots[0], snapshots[2])

    with pytest.raises(ValueError):
        await check_company(VALID_INN, InvalidProvider())
