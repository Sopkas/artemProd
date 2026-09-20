"""S5-02: answers are stored by version next to the assessment and never bought twice."""

import json
from datetime import date

import pytest

from claims_assistant.application.check_package import (
    StorageError,
    accept_counterparties,
    accept_ledger,
)
from claims_assistant.application.explanations import EXPLANATION_STEP, StoredExplanation
from claims_assistant.application.recommendation import (
    AiErrorCode,
    AiUnavailable,
    ExplanationOutcome,
)
from claims_assistant.domain.ai_review import Explanation, Promise, Rejected, RejectionCode
from claims_assistant.domain.analysis import FileKind, RunStatus
from claims_assistant.domain.external import DataMode
from claims_assistant.domain.serialization import PayloadError
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider
from claims_assistant.infrastructure.excel.counterparties import build_counterparties_template
from claims_assistant.infrastructure.excel.ledgers import build_interactions_workbook
from claims_assistant.infrastructure.excel.reader import OpenpyxlSheetReader
from claims_assistant.infrastructure.llm.stub import StubRecommendationProvider
from claims_assistant.infrastructure.memory.analysis import InMemoryAnalysisRepository
from claims_assistant.infrastructure.storage.local import LocalFileStorage
from tests.unit.test_analysis_pipeline import (
    DAY,
    OWNER,
    ROWS,
    CountingProvider,
    guard,
    pipeline,
    sheet_rows,
)

INN_A, INN_B = "1234567894", "7707083893"
SECRET = "Секретный комментарий клиента."


def test_stored_explanation_round_trips_accepted_rejected_and_unavailable():
    accepted = StoredExplanation(
        status="accepted",
        versions="stub:stub-1:i1:c1:s1:r0.1",
        provider="stub",
        model="stub-1",
        reference="row-1",
        answer_text='{"explanation": "…"}',
        input_tokens=120,
        output_tokens=30,
        latency_seconds=0.5,
        explanation=Explanation(
            text="Текст.",
            promises=(Promise("INT-1", date(2026, 9, 30), "до 30.09"), Promise("INT-2", DAY)),
            grounds=("internal-debt",),
        ),
    )
    assert StoredExplanation.from_payload(accepted.to_payload()) == accepted
    rejected = StoredExplanation(
        status="rejected:new_amount",
        versions="v",
        provider="stub",
        model="stub-1",
        reference="row-2",
        answer_text="…",
        rejected=Rejected(RejectionCode.NEW_AMOUNT, "Новое число.", "5000"),
    )
    assert StoredExplanation.from_payload(rejected.to_payload()) == rejected
    assert accepted.final and rejected.final
    unavailable = StoredExplanation.from_outcome(
        ExplanationOutcome(
            "stub", "stub-1", "v", error=AiUnavailable(AiErrorCode.TIMEOUT, "Не ответила.")
        ),
        "row-3",
    )
    assert not unavailable.final and unavailable.as_error().code is AiErrorCode.TIMEOUT
    assert StoredExplanation.from_payload(unavailable.to_payload()) == unavailable


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema=2),
        lambda d: d.update(status=5),
        lambda d: d["explanation"].update(text=None),
        lambda d: d["explanation"]["promises"][0].update(due_on="30.09.2026"),
        lambda d: d.pop("versions"),
    ],
)
def test_unreadable_explanation_payloads_are_rejected(mutate):
    stored = StoredExplanation(
        status="accepted",
        versions="v",
        provider="stub",
        model="stub-1",
        reference=None,
        explanation=Explanation("Текст.", (Promise("INT-1", DAY),), ()),
    )
    data = json.loads(stored.to_payload())
    mutate(data)
    with pytest.raises(PayloadError):
        StoredExplanation.from_payload(json.dumps(data))


async def package(tmp_path, *, with_interactions=True):
    repository = InMemoryAnalysisRepository()
    files = LocalFileStorage(tmp_path / "uploads")
    reader = OpenpyxlSheetReader()
    result = await accept_counterparties(
        OWNER,
        DAY,
        DataMode.DEMO,
        build_counterparties_template(ROWS),
        repository=repository,
        files=files,
        reader=reader,
    )
    if with_interactions:
        await accept_ledger(
            OWNER,
            result.run.id,
            FileKind.INTERACTIONS,
            build_interactions_workbook([[INN_A, "INT-1", date(2026, 8, 20), SECRET, "тел."]]),
            coverage=None,
            repository=repository,
            files=files,
            reader=reader,
        )
    await repository.transition(OWNER, result.run.id, RunStatus.QUEUED)
    run = await repository.claim_next()
    return run, files, repository


def explanation_steps(steps):
    return [s for s in steps if s.step == EXPLANATION_STEP]


def about_sheet(files, artifact) -> str:
    rows = sheet_rows(files.read(artifact.stored_path), "О проверке")
    return " ".join(str(c) for row in rows for c in row if c is not None)


async def test_pipeline_asks_once_per_company_stores_the_answer_and_links_it(tmp_path):
    run, files, repository = await package(tmp_path)
    explainer = StubRecommendationProvider()
    outcome = await pipeline(
        files, repository, guard(DemoCompanyDataProvider()), explainer=explainer
    ).process(run)
    assert outcome.status == RunStatus.COMPLETED
    assert [r.context["counterparty_ref"] for r in explainer.requests] == ["row-1", "row-2"]
    steps = explanation_steps(await repository.list_steps(run.id))
    assert sorted(s.inn for s in steps) == [INN_A, INN_B]
    stored = StoredExplanation.from_payload(steps[0].payload)
    assert stored.status == "accepted" and stored.explanation is not None
    assert steps[0].version == stored.versions and stored.versions.startswith("stub:stub-1:i")
    assert stored.input_tokens > 0 and stored.reference in {"row-1", "row-2"}
    # The report names the AI version; the explanation text is rendered in S5-04.
    artifact = await repository.get_report(OWNER, run.id)
    assert "stub stub-1; контекст" in about_sheet(files, artifact)
    # Comments stay home unless the setting allows them out.
    bodies = [json.dumps(r.context, ensure_ascii=False) for r in explainer.requests]
    assert all(SECRET not in body for body in bodies)


async def test_comments_are_sent_only_when_the_setting_allows(tmp_path):
    run, files, repository = await package(tmp_path)
    explainer = StubRecommendationProvider()
    await pipeline(
        files,
        repository,
        guard(DemoCompanyDataProvider()),
        explainer=explainer,
        ai_send_comments=True,
    ).process(run)
    bodies = [json.dumps(r.context, ensure_ascii=False) for r in explainer.requests]
    assert any(SECRET in body for body in bodies)


async def test_repeated_claim_after_the_report_does_not_ask_the_model_again(tmp_path):
    run, files, repository = await package(tmp_path)
    explainer = StubRecommendationProvider()
    external = CountingProvider(DemoCompanyDataProvider())
    first = await pipeline(files, repository, guard(external), explainer=explainer).process(run)
    assert len(explainer.requests) == 2
    await repository.recover_interrupted()
    resumed = await repository.claim_next()
    again = await pipeline(files, repository, guard(external), explainer=explainer).process(resumed)
    assert again == first and len(explainer.requests) == 2


async def test_answers_stored_before_a_crash_are_read_back_not_bought_again(tmp_path):
    """Crash after the explanations but before the report file: the stored answers
    serve the resumed run, the model is not asked, the report links them."""
    run, files, repository = await package(tmp_path)
    explainer = StubRecommendationProvider()
    external = CountingProvider(DemoCompanyDataProvider())

    class DiesOnSave:
        def read(self, path):
            return files.read(path)

        def save(self, run_id, data):
            raise StorageError("disk full")

        def remove(self, path):
            files.remove(path)

    outcome = await pipeline(
        DiesOnSave(), repository, guard(external), explainer=explainer
    ).process(run)
    assert outcome.status == RunStatus.FAILED and len(explainer.requests) == 2
    assert len(explanation_steps(await repository.list_steps(run.id))) == 2

    await repository.recover_interrupted()
    resumed = await repository.claim_next()
    outcome = await pipeline(files, repository, guard(external), explainer=explainer).process(
        resumed
    )
    assert outcome.status == RunStatus.COMPLETED
    assert len(explainer.requests) == 2  # the same answers were not bought twice
    assert external.calls == [INN_A, INN_B]  # nor were the sections fetched twice
    assert await repository.get_report(OWNER, run.id) is not None


async def test_rejected_answers_are_stored_unavailable_ones_are_not(tmp_path):
    run, files, repository = await package(tmp_path)
    rejecting = StubRecommendationProvider(
        script=lambda request: json.dumps(
            {"explanation": "Долг вырос до 250000 руб.", "promises": [], "grounds": []}
        )
    )
    await pipeline(
        files, repository, guard(DemoCompanyDataProvider()), explainer=rejecting
    ).process(run)
    steps = explanation_steps(await repository.list_steps(run.id))
    assert len(steps) == 2
    stored = StoredExplanation.from_payload(steps[0].payload)
    assert stored.status == "rejected:new_amount" and stored.explanation is None
    assert stored.answer_text and "250000" in stored.answer_text  # kept for S5-07 evaluation

    run2, files2, repository2 = await package(tmp_path / "two")
    down = StubRecommendationProvider(failure=AiUnavailable(AiErrorCode.UNAVAILABLE, "Нет связи."))
    outcome = await pipeline(
        files2, repository2, guard(DemoCompanyDataProvider()), explainer=down
    ).process(run2)
    # The report is still built; the missing explanations make the check partial (S5-03).
    assert outcome.status == RunStatus.PARTIAL and "Пояснений ИИ нет у 2" in outcome.failure
    assert explanation_steps(await repository2.list_steps(run2.id)) == []


async def test_without_a_provider_nothing_changes(tmp_path):
    run, files, repository = await package(tmp_path)
    await pipeline(files, repository, guard(DemoCompanyDataProvider())).process(run)
    assert explanation_steps(await repository.list_steps(run.id)) == []
    artifact = await repository.get_report(OWNER, run.id)
    assert "ИИ-пояснения не используются" in about_sheet(files, artifact)


async def test_a_new_model_version_is_a_new_key_and_a_new_request(tmp_path):
    run, files, repository = await package(tmp_path)
    first = StubRecommendationProvider()
    line = pipeline(files, repository, guard(DemoCompanyDataProvider()), explainer=first)
    imported = await line._import(run)
    snapshots = await line._fetch_all(run, imported.rows)
    await line._report(run, imported, snapshots)
    assert len(first.requests) == 2

    class NewModel(StubRecommendationProvider):
        model = "stub-2"

    second = NewModel()
    line = pipeline(files, repository, guard(DemoCompanyDataProvider()), explainer=second)
    await line._report(run, imported, snapshots)
    steps = explanation_steps(await repository.list_steps(run.id))
    assert len(steps) == 4 and len(second.requests) == 2
    assert {s.version.split(":")[1] for s in steps} == {"stub-1", "stub-2"}
    # The same model again: nothing new is asked.
    line = pipeline(files, repository, guard(DemoCompanyDataProvider()), explainer=first)
    await line._report(run, imported, snapshots)
    assert len(first.requests) == 2


def test_latency_must_be_a_number():
    stored = StoredExplanation(
        status="accepted", versions="v", provider="stub", model="stub-1", reference=None
    )
    data = json.loads(stored.to_payload())
    data["latency_seconds"] = True
    with pytest.raises(PayloadError):
        StoredExplanation.from_payload(json.dumps(data))
    data["latency_seconds"] = 2
    assert StoredExplanation.from_payload(json.dumps(data)).latency_seconds == 2.0


async def test_the_step_key_is_built_in_one_place():
    """Review B on #41: the key the answer is saved under is the key it is looked up by."""
    from claims_assistant.application.recommendation import explanation_key
    from tests.unit.test_recommendation import context_for

    context = await context_for()
    provider = StubRecommendationProvider()
    key = explanation_key(provider, context)
    versions = (context.instruction_version, context.context_version, context.rules_version)
    assert key == "stub:stub-1:i{}:c{}:s1:r{}".format(*versions)
    from claims_assistant.application.recommendation import request_explanation

    outcome = await request_explanation(provider, context)
    assert outcome.versions == key
