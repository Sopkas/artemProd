"""S8-09 (part A): the daily monitoring run over the real store and part B's texts.

The source is the demo provider: a company switched from the ordinary story to the alarm
one between two days gains a bankruptcy message, two cases and a proceeding — the same
records the rules of S8-06 read in Checko's answers. Only the source and the clock are
stand-ins; the database, the comparison and the words are the service's own.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from claims_assistant.application.external_guard import BUDGET_EXHAUSTED, BUDGET_MESSAGE
from claims_assistant.application.monitoring import (
    INTERNAL_FAILURE,
    Frequency,
    MonitoringPolicy,
    MonitoringScheduler,
    MonitoringService,
    WatchSettings,
    is_due,
    weekly_slot,
)
from claims_assistant.domain.external import (
    Coverage,
    DataMode,
    ExternalSnapshot,
    FetchStatus,
    ProviderError,
    Section,
)
from claims_assistant.domain.monitoring import MONITORED
from claims_assistant.domain.watchlist import Candidate, WatchEntry
from claims_assistant.infrastructure.demo.company_data import DemoCompanyDataProvider, DemoScenario
from claims_assistant.infrastructure.persistence.sqlite import open_sqlite_repository
from claims_assistant.infrastructure.persistence.watch import SqliteWatchStore
from claims_assistant.presentation.telegram.monitoring_notifier import TelegramMonitoringTexts

OWNER = 42
OTHER = 43
DEMO_NAME = "ДЕМО — вымышленная компания"


def inn10(nine: str) -> str:
    """A synthetic organization INN with a valid check digit."""
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    return nine + str(sum(int(d) * w for d, w in zip(nine, weights, strict=True)) % 11 % 10)


A = inn10("123456789")
B = inn10("770000001")
C = inn10("770000002")


def at(day: int, hour: int = 9, minute: int = 30) -> datetime:
    """A moment of October 2026 by the Moscow clock (the default business offset)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC) - timedelta(hours=3)


class Clock:
    def __init__(self, moment: datetime) -> None:
        self.now = moment

    def __call__(self) -> datetime:
        return self.now


def down(snapshot: ExternalSnapshot) -> ExternalSnapshot:
    return ExternalSnapshot(
        inn=snapshot.inn,
        section=snapshot.section,
        source=snapshot.source,
        mode=snapshot.mode,
        fetched_at=snapshot.fetched_at,
        status=FetchStatus.UNAVAILABLE,
        coverage=Coverage.UNAVAILABLE,
        missing=("Источник не ответил.",),
        error=ProviderError("unavailable", "Источник не ответил."),
    )


def out_of_requests(snapshot: ExternalSnapshot) -> ExternalSnapshot:
    """The guard's placeholder when the day's requests are spent: the source not asked."""
    return ExternalSnapshot(
        inn=snapshot.inn,
        section=snapshot.section,
        source="guard",
        mode=snapshot.mode,
        fetched_at=snapshot.fetched_at,
        status=FetchStatus.UNAVAILABLE,
        coverage=Coverage.UNAVAILABLE,
        missing=(BUDGET_MESSAGE,),
        error=ProviderError(BUDGET_EXHAUSTED, BUDGET_MESSAGE),
    )


class Source:
    """The guarded provider as the service sees it: ``scoped(budget)`` and ``fetch``."""

    def __init__(self) -> None:
        self.scenarios: dict[str, DemoScenario] = {}
        self.down: set[tuple[str, Section]] = set()
        self.broken: set[str] = set()
        self.spent: set[str] = set()  # companies the day's requests do not reach
        self.cut: set[tuple[str, Section]] = set()  # sections the requests ran out on
        self.mode = DataMode.DEMO
        self.calls: list[str] = []
        self.budgets: list = []
        self.before_fetch = None

    def scoped(self, budget):
        self.budgets.append(budget)
        return self

    async def fetch(self, request):
        self.calls.append(request.inn)
        if self.before_fetch is not None:
            await self.before_fetch(request.inn)
        if request.inn in self.broken:
            raise RuntimeError("synthetic failure")
        demo = DemoCompanyDataProvider(DemoScenario.ORDINARY, by_inn=self.scenarios)
        answers = []
        for snapshot in await demo.fetch(request):
            if (snapshot.inn, snapshot.section) in self.down:
                snapshot = down(snapshot)
            if snapshot.inn in self.spent or (snapshot.inn, snapshot.section) in self.cut:
                snapshot = out_of_requests(snapshot)
            answers.append(replace(snapshot, mode=self.mode))
        return tuple(answers)


@pytest.fixture
def world(tmp_path):
    repository = open_sqlite_repository(tmp_path / "claims.sqlite3")
    clock = Clock(at(16))
    store = SqliteWatchStore(repository.engine, clock)
    source = Source()

    def service(mode: DataMode = DataMode.DEMO, **policy) -> MonitoringService:
        return MonitoringService(
            store,
            source,
            TelegramMonitoringTexts(),
            mode=mode,
            policy=MonitoringPolicy(**policy),
            clock=clock,
        )

    yield SimpleNamespace(store=store, source=source, clock=clock, service=service)
    repository.close()


async def run(world, service: MonitoringService, day: int):
    world.clock.now = at(day)
    due = await service.due()
    assert due == date(2026, 10, day)
    return await service.run_day(due)


async def messages(world, day: int | None = None):
    """What waits in the outbox, oldest first; ``day`` keeps only that day's."""
    pending = await world.store.pending(100)
    if day is None:
        return pending
    mark = f"{day}.10.2026"
    return tuple(message for message in pending if mark in message.text.splitlines()[0])


# --- the day ---


async def test_the_first_check_is_a_baseline_and_the_summary_says_so(world):
    service = world.service()
    await service.add(OWNER, [Candidate(A, "ООО «Альфа»"), Candidate(B)])
    outcome = await run(world, service, 16)
    assert (outcome.checked, outcome.changed, outcome.failed) == (2, 0, 0)
    [summary] = await messages(world)
    assert summary.owner_id == OWNER and summary.inn is None
    assert summary.text.startswith("📋 Мониторинг на 16.10.2026. Организаций проверено: 2.")
    assert "Впервые проверено: 2 организации" in summary.text
    # The day is done: no second run today, and every company has its date. The source's
    # name fills a gap; the customer's own name stays (17.10.2026).
    assert await service.due() is None
    entries = await service.entries(OWNER)
    assert {entry.last_checked_on for entry in entries} == {date(2026, 10, 16)}
    assert {entry.inn: entry.name for entry in entries} == {A: "ООО «Альфа»", B: DEMO_NAME}
    assert len(await world.store.snapshots(OWNER, A)) == len(MONITORED)


async def test_what_appeared_is_sent_per_company_before_the_summary(world):
    service = world.service()
    await service.add(OWNER, [Candidate(A), Candidate(B)])
    await run(world, service, 16)
    world.source.scenarios[A] = DemoScenario.ALARM
    outcome = await run(world, service, 17)
    assert (outcome.checked, outcome.changed) == (2, 1)
    notice, summary = await messages(world, 17)
    assert notice.inn == A and notice.id < summary.id
    assert notice.text.startswith(f"🔔 Мониторинг, 17.10.2026: {DEMO_NAME}, ИНН {A}")
    assert "Новое арбитражное дело, организация — ответчик" in notice.text
    assert "Новое исполнительное производство (ФССП)" in notice.text
    assert "Новое сообщение ЕФРСБ" in notice.text
    assert summary.inn is None
    assert "Изменения у 1 организации — подробности выше" in summary.text
    entry = next(entry for entry in await service.entries(OWNER) if entry.inn == A)
    assert entry.last_change.startswith("Новое") and entry.last_change_on == date(2026, 10, 17)
    quiet = next(entry for entry in await service.entries(OWNER) if entry.inn == B)
    assert quiet.last_change is None


async def test_a_case_from_a_day_the_source_was_down_is_still_new_the_next_day(world):
    service = world.service()
    await service.add(OWNER, [Candidate(A)])
    await run(world, service, 16)
    # 17.10: the courts do not answer — never «изменений нет», and the 16.10 snapshot stays.
    world.source.down = {(A, Section.COURTS)}
    await run(world, service, 17)
    [summary] = await messages(world, 17)
    assert "Изменений нет" not in summary.text
    assert "Проверено не полностью: 1 организация" in summary.text
    assert "арбитражные дела — источник не ответил" in summary.text
    courts = next(s for s in await world.store.snapshots(OWNER, A) if s.section is Section.COURTS)
    assert courts.status is FetchStatus.OK
    # 18.10: the courts answer, with the case that came while they were down.
    world.source.down = set()
    world.source.scenarios[A] = DemoScenario.ALARM
    await run(world, service, 18)
    notice, _summary = await messages(world, 18)
    assert "Новое арбитражное дело" in notice.text


async def test_a_restart_in_the_middle_does_not_ask_the_source_again(world):
    service = world.service()
    await service.add(OWNER, [Candidate(A), Candidate(B)])

    async def crash_on_the_second(inn):
        if len(world.source.calls) == 2:
            raise asyncio.CancelledError  # the bot stopped while asking about it

    world.source.before_fetch = crash_on_the_second
    world.clock.now = at(16)
    with pytest.raises(asyncio.CancelledError):
        await service.run_day(await service.due())
    first, second = world.source.calls
    assert await messages(world) == ()
    # Started again: the day is still due; the company done before the stop is not asked.
    world.source.before_fetch = None
    await run(world, service, 16)
    assert world.source.calls == [first, second, second]
    [summary] = await messages(world)
    assert "Организаций проверено: 2" in summary.text
    entries = await service.entries(OWNER)
    assert {entry.last_checked_on for entry in entries} == {date(2026, 10, 16)}


async def test_a_broken_company_is_named_and_does_not_stop_the_day(world):
    service = world.service()
    await service.add(OWNER, [Candidate(A), Candidate(B)])
    world.source.broken = {A}
    outcome = await run(world, service, 16)
    assert (outcome.checked, outcome.failed) == (2, 1)
    [summary] = await messages(world)
    assert "внутренняя ошибка бота" in summary.text
    assert await world.store.snapshots(OWNER, A) == ()
    # Not retried every minute; the next day tries it again.
    assert await service.due() is None
    world.source.broken = set()
    await run(world, service, 17)
    assert world.source.calls.count(A) == 2
    assert INTERNAL_FAILURE.startswith("Не удалось проверить")


async def test_a_quiet_day_follows_the_policy(world):
    talkative = world.service()
    await talkative.add(OWNER, [Candidate(A)])
    await run(world, talkative, 16)
    await run(world, talkative, 17)
    [summary] = await messages(world, 17)
    assert summary.text.endswith("Изменений нет.")
    # Silent on a quiet day — but the baseline day still says what it did.
    silent = world.service(quiet_summary=False)
    await silent.add(OTHER, [Candidate(B)])
    world.clock.now = at(18)
    await silent.run_day(date(2026, 10, 18))
    texts = [message.text for message in await messages(world, 18)]
    assert len(texts) == 1 and "Впервые проверено" in texts[0]  # OTHER's first day
    await silent.run_day(date(2026, 10, 19))
    assert await messages(world, 19) == ()


async def test_a_live_answer_is_never_compared_with_a_demo_one(world):
    await world.service().add(OWNER, [Candidate(A)])
    await run(world, world.service(), 16)
    world.source.mode = DataMode.LIVE
    world.source.scenarios[A] = DemoScenario.ALARM
    live = world.service(mode=DataMode.LIVE)
    await run(world, live, 17)
    [summary] = await messages(world, 17)
    assert "Впервые проверено: 1 организация" in summary.text


async def test_taken_off_and_back_again_starts_from_a_new_baseline(world):
    service = world.service()
    await service.add(OWNER, [Candidate(A)])
    await run(world, service, 16)
    removed = await service.remove(OWNER, [A])
    assert removed.removed == (A,) and removed.total == 0
    assert await world.store.snapshots(OWNER, A) == ()
    await service.add(OWNER, [Candidate(A)])
    world.source.scenarios[A] = DemoScenario.ALARM
    await run(world, service, 17)
    [summary] = await messages(world, 17)
    assert "Впервые проверено" in summary.text


async def test_a_company_taken_off_while_it_is_checked_leaves_nothing(world):
    service = world.service()
    await service.add(OWNER, [Candidate(A), Candidate(B)])

    async def taken_off(inn):
        if inn == A:
            await service.remove(OWNER, [A])

    world.source.before_fetch = taken_off
    world.source.scenarios[A] = DemoScenario.ALARM
    outcome = await run(world, service, 16)
    assert outcome.checked == 1
    assert await world.store.snapshots(OWNER, A) == ()
    [summary] = await messages(world)
    assert "Организаций проверено: 1" in summary.text


async def test_every_list_is_its_owners(world):
    service = world.service()
    await service.add(OWNER, [Candidate(A)])
    await service.add(OTHER, [Candidate(B)])
    assert [entry.inn for entry in await service.entries(OWNER)] == [A]
    assert (await service.remove(OTHER, [A])).absent == (A,)
    await run(world, service, 16)
    owners = sorted(message.owner_id for message in await messages(world))
    assert owners == [OWNER, OTHER]
    assert await service.clear(OTHER) == 1
    assert await service.entries(OTHER) == ()
    assert [entry.inn for entry in await service.entries(OWNER)] == [A]


async def test_the_list_keeps_its_limit_and_the_newest_come_first(world):
    service = world.service(limit=2)
    first = await service.add(OWNER, [Candidate(A), Candidate(B), Candidate(C)])
    assert [c.inn for c in first.added] == [A, B] and [c.inn for c in first.over_limit] == [C]
    assert [entry.inn for entry in await service.entries(OWNER)] == [B, A]
    # A name sent later fills a gap — and replaces an earlier one: a correction is sent
    # again (17.10.2026: a name typed beside the wrong INN). Sent without a name, it stays.
    again = await service.add(OWNER, [Candidate(B, "ООО «Бета»"), Candidate(A, "ООО «Альфа»")])
    assert len(again.already) == 2 and again.total == 2
    second = await service.add(OWNER, [Candidate(A, "АО «Альфа-2»"), Candidate(B)])
    assert second.added == ()
    names = {entry.inn: entry.name for entry in await service.entries(OWNER)}
    assert names == {A: "АО «Альфа-2»", B: "ООО «Бета»"}
    assert all(entry.added_on == date(2026, 10, 16) for entry in await service.entries(OWNER))


async def test_the_day_is_due_from_its_hour_until_it_is_done(world):
    service = world.service(run_at=time(9, 0))
    await service.add(OWNER, [Candidate(A)])
    world.clock.now = at(16, 8, 59)
    assert await service.due() is None
    world.clock.now = at(16, 9, 0)
    assert await service.due() == date(2026, 10, 16)
    await service.run_day(date(2026, 10, 16))
    world.clock.now = at(16, 23, 59)
    assert await service.due() is None
    # The computer was off at 09:00 on the 17th: the day runs when the bot starts.
    world.clock.now = at(17, 14, 5)
    assert await service.due() == date(2026, 10, 17)
    # One budget for the whole day, with the policy's ceiling.
    assert len(world.source.budgets) == 1
    assert world.source.budgets[0].max_requests == MonitoringPolicy().max_requests


async def test_an_empty_day_sends_nothing(world):
    service = world.service()
    outcome = await run(world, service, 16)
    assert (outcome.owners, outcome.checked) == (0, 0)
    assert await messages(world) == ()


async def test_companies_the_requests_did_not_reach_go_first_next_day(world):
    """17.10.2026, before the first live run: the same newest companies would get the day's
    requests every day and the rest never. Not reached — not recorded, named, first next."""
    service = world.service()
    await service.add(OWNER, [Candidate(A), Candidate(B), Candidate(C)])  # store: C, B, A
    world.source.spent = {B, A}
    outcome = await run(world, service, 16)
    assert (outcome.checked, outcome.waiting) == (1, 2)
    [summary] = await messages(world)
    assert "Организаций проверено: 1" in summary.text
    assert summary.text.endswith(
        "Не дошла очередь: 2 организации — кончился лимит запросов к источнику. "
        "В следующий раз — в первую очередь."
    )
    waiting = {e.inn: e.last_checked_on for e in await service.entries(OWNER)}
    assert waiting == {A: None, B: None, C: date(2026, 10, 16)}
    assert await world.store.snapshots(OWNER, A) == ()
    # The next day starts with them, in the store's order, and the checked one comes last.
    world.source.spent = set()
    world.source.calls.clear()
    await run(world, service, 17)
    assert world.source.calls == [B, A, C]


async def test_a_company_the_requests_ran_out_on_midway_goes_first_next_day(world):
    """Review B on #105: the limit ran out after three of its sections. It is recorded —
    those three answered — and goes first next day, not after a whole round."""
    service = world.service()
    await service.add(OWNER, [Candidate(A), Candidate(B), Candidate(C)])  # store: C, B, A
    world.source.cut = {(B, Section.COURTS), (B, Section.ENFORCEMENTS)}
    outcome = await run(world, service, 16)
    assert (outcome.checked, outcome.waiting, outcome.unchecked) == (3, 0, 1)
    assert await world.store.limited(OWNER) == frozenset({B})
    world.source.cut = set()
    world.source.calls.clear()
    await run(world, service, 17)
    assert world.source.calls == [B, C, A]
    # Checked in full now: an ordinary place again.
    assert await world.store.limited(OWNER) == frozenset()


async def test_a_day_nothing_could_be_asked_still_says_so(world):
    service = world.service(quiet_summary=False)  # silence on a quiet day — not on this one
    await service.add(OWNER, [Candidate(A)])
    world.source.spent = {A}
    outcome = await run(world, service, 16)
    assert (outcome.checked, outcome.waiting) == (0, 1)
    [summary] = await messages(world)
    assert summary.text == (
        "📋 Мониторинг на 16.10.2026. Проверить не удалось: кончился лимит запросов к "
        "источнику. Ждут проверки: 1 организация, в следующий раз — в первую очередь."
    )


# --- pause and frequency (S8-12) ---


async def test_a_paused_list_asks_nothing_and_goes_on_from_where_it_stopped(world):
    """On a pause not one request and no summary; after it the comparison is with the last
    check before the pause, so what appeared meanwhile still comes."""
    service = world.service()
    await service.add(OWNER, [Candidate(A)])
    await service.add(OTHER, [Candidate(B)])
    await run(world, service, 16)
    paused = await service.pause(OWNER)
    assert paused == WatchSettings(Frequency.DAILY, date(2026, 10, 16))
    world.source.scenarios[A] = DemoScenario.ALARM  # a case appears during the pause
    world.source.calls.clear()
    await run(world, service, 17)
    assert world.source.calls == [B]  # the other owner's list runs as usual
    assert [m.owner_id for m in await messages(world, 17)] == [OTHER]
    assert await service.next_run(OWNER) is None
    await service.resume(OWNER)
    await run(world, service, 18)
    notice, _summary = [m for m in await messages(world, 18) if m.owner_id == OWNER]
    assert notice.inn == A and "Новое арбитражное дело" in notice.text


async def test_weekdays_leave_the_weekend_out(world):
    service = world.service()
    await service.add(OWNER, [Candidate(A)])
    await service.set_frequency(OWNER, Frequency.WEEKDAYS)
    for day in (16, 17, 18, 19):  # Friday, Saturday, Sunday, Monday
        world.source.calls.clear()
        await run(world, service, day)
        assert world.source.calls == ([A] if day in (16, 19) else []), day
    assert await messages(world, 17) == () and await messages(world, 18) == ()
    world.clock.now = at(17, 10)  # Saturday after the run: the next one is Monday
    assert await service.next_run(OWNER) == date(2026, 10, 19)


async def test_weekly_checks_each_company_on_its_weekday(world):
    """A seventh of the list a day: about the same requests every day, a hundred companies
    within the free hundred. A company not checked yet waits for its own weekday."""
    service = world.service()
    await service.add(OWNER, [Candidate(A), Candidate(B)])
    await service.set_frequency(OWNER, Frequency.WEEKLY)
    checked: dict[str, list[int]] = {}
    for day in range(16, 31):
        world.source.calls.clear()
        await run(world, service, day)
        for inn in world.source.calls:
            checked.setdefault(inn, []).append(day)
    for inn in (A, B):
        assert len(checked[inn]) == 2, inn
        assert checked[inn][1] - checked[inn][0] == 7
        assert {date(2026, 10, d).weekday() for d in checked[inn]} == {weekly_slot(inn)}


def by_weekday(per_day: int) -> list[str]:
    """``per_day`` synthetic INNs for every weekly slot, Monday's first."""
    slots: dict[int, list[str]] = {slot: [] for slot in range(7)}
    number = 500_000_000
    while any(len(inns) < per_day for inns in slots.values()):
        inn = inn10(str(number))
        if len(slots[weekly_slot(inn)]) < per_day:
            slots[weekly_slot(inn)].append(inn)
        number += 1
    return [inn for slot in range(7) for inn in slots[slot]]


def business(day: date, hour: int = 9, minute: int = 30) -> datetime:
    """A moment of ``day`` by the Moscow clock, for runs past the end of October."""
    return datetime.combine(day, time(hour, minute), tzinfo=UTC) - timedelta(hours=3)


async def checked_on(world, service: MonitoringService, day: date) -> list[str]:
    """The companies the run of ``day`` asks the source about."""
    world.clock.now = business(day)
    assert await service.due() == day
    world.source.calls.clear()
    await service.run_day(day)
    return list(world.source.calls)


def of_weekday(inns: list[str], day: date) -> set[str]:
    return {inn for inn in inns if weekly_slot(inn) == day.weekday()}


async def test_a_resume_after_two_weeks_checks_a_seventh_and_keeps_the_days(world):
    """Andrey's review of #111: «a week has passed» checked the whole list on the day of a
    resume and then moved a seventh of it off its day for good."""
    inns = by_weekday(3)
    start = date(2026, 10, 16)
    world.clock.now = business(start, 8)
    service = world.service()
    await service.add(OWNER, [Candidate(inn) for inn in inns])
    await service.set_frequency(OWNER, Frequency.WEEKLY)
    for offset in range(7):
        day = start + timedelta(days=offset)
        assert set(await checked_on(world, service, day)) == of_weekday(inns, day)
    await service.pause(OWNER)
    for offset in range(7, 21):
        assert await checked_on(world, service, start + timedelta(days=offset)) == []
    resumed = start + timedelta(days=21)
    world.clock.now = business(resumed, 8)  # before the day's run
    await service.resume(OWNER)
    for offset in range(21, 35):
        day = start + timedelta(days=offset)
        assert set(await checked_on(world, service, day)) == of_weekday(inns, day), day


async def test_a_switch_from_every_day_keeps_a_seventh_a_day(world):
    inns = by_weekday(3)
    start = date(2026, 10, 16)
    world.clock.now = business(start, 8)
    service = world.service()
    await service.add(OWNER, [Candidate(inn) for inn in inns])
    for offset in range(3):
        assert len(await checked_on(world, service, start + timedelta(days=offset))) == 21
    await service.set_frequency(OWNER, Frequency.WEEKLY)  # after the third day's run
    for offset in range(3, 17):
        day = start + timedelta(days=offset)
        assert set(await checked_on(world, service, day)) == of_weekday(inns, day), day


async def test_a_weekday_missed_is_made_up_once_and_the_day_stays(world):
    """Its own weekday came without its check: the requests ran out before it, or the
    computer was off. The next run makes it up, and the week after it is back on its day."""
    [inn] = by_weekday(1)[:1]  # Monday's
    monday = date(2026, 10, 19)
    world.clock.now = business(monday, 8)
    service = world.service()
    await service.add(OWNER, [Candidate(inn)])
    await service.set_frequency(OWNER, Frequency.WEEKLY)
    world.source.spent = {inn}
    assert await checked_on(world, service, monday) == [inn]  # asked, nothing answered
    world.source.spent = set()
    seen = [
        offset
        for offset in range(1, 23)
        if offset != 14  # the computer off on the third Monday
        and await checked_on(world, service, monday + timedelta(days=offset))
    ]
    assert seen == [1, 7, 15, 21]


def test_a_company_is_due_on_its_day_and_only_owed_days_are_made_up():
    monday = date(2026, 10, 19)
    [inn] = by_weekday(1)[:1]
    weekly = WatchSettings(Frequency.WEEKLY)

    def due(day, *, checked=None, added=date(2026, 10, 1), settings=weekly, limited=False):
        entry = WatchEntry(inn, added, last_checked_on=checked)
        return is_due(entry, day, settings, limited)

    tuesday = monday + timedelta(days=1)
    assert due(monday)  # its own weekday, never checked
    assert due(monday, checked=monday - timedelta(days=1))  # on its day even after a switch
    assert not due(tuesday, checked=monday)
    assert not due(tuesday, added=tuesday)  # on the list since today: waits for Monday
    assert due(tuesday, added=monday)  # added on its Monday after the run: owed
    assert due(tuesday, checked=monday - timedelta(days=7))  # Monday came without it
    assert due(monday + timedelta(days=6), checked=monday - timedelta(days=7))
    assert not due(
        tuesday, checked=monday - timedelta(days=7), settings=replace(weekly, changed_on=tuesday)
    )
    assert due(tuesday, checked=monday, limited=True)  # the requests ran out on it
    assert due(tuesday, settings=WatchSettings(Frequency.DAILY))
    assert not due(monday + timedelta(days=5), settings=WatchSettings(Frequency.WEEKDAYS))


async def test_settings_are_the_owners_and_outlive_a_cleared_list(world):
    service = world.service()
    assert await service.settings(OWNER) == WatchSettings()
    await service.add(OWNER, [Candidate(A)])
    await service.set_frequency(OWNER, Frequency.WEEKLY)
    await service.pause(OWNER)
    assert await service.clear(OWNER) == 1
    kept = await service.settings(OWNER)
    assert kept.frequency is Frequency.WEEKLY and kept.paused
    assert await service.settings(OTHER) == WatchSettings()
    # A pause twice keeps its first day; a resume keeps the frequency and dates the change.
    world.clock.now = at(18)
    assert (await service.pause(OWNER)).paused_since == date(2026, 10, 16)
    resumed = WatchSettings(Frequency.WEEKLY, None, date(2026, 10, 18))
    assert await service.resume(OWNER) == resumed
    # Neither a resume without a pause nor the same frequency again moves that date.
    world.clock.now = at(20)
    assert await service.resume(OWNER) == resumed
    assert await service.set_frequency(OWNER, Frequency.WEEKLY) == resumed
    assert await service.settings(OWNER) == resumed


# --- the outbox ---


async def test_sent_and_refused_messages_leave_the_queue_and_age_out(world):
    service = world.service()
    await service.add(OWNER, [Candidate(A)])
    await service.add(OTHER, [Candidate(B)])
    await run(world, service, 16)
    first, second = await messages(world)
    await world.store.mark_sent(first.id)
    await world.store.mark_failed(second.id, "TelegramForbiddenError")
    assert await world.store.pending(10) == ()
    # Done messages older than the keep period go; waiting ones would stay.
    world.clock.now = at(16) + timedelta(days=31)
    removed = await world.store.purge_messages(world.clock.now - timedelta(days=30))
    assert removed == 2


# --- the scheduler ---


async def test_the_scheduler_delivers_then_runs_the_due_day_then_delivers_again(world):
    service = world.service()
    await service.add(OWNER, [Candidate(A)])
    world.clock.now = at(16)
    deliver = AsyncMock(return_value=0)
    scheduler = MonitoringScheduler(service, deliver, clock=world.clock)
    outcome = await scheduler.tick()
    assert outcome is not None and outcome.checked == 1
    assert deliver.await_count == 2
    # Nothing due on the next tick: one delivery round, no run.
    assert await scheduler.tick() is None
    assert deliver.await_count == 3


async def test_a_failed_run_waits_before_it_is_tried_again(world):
    clock = Clock(at(16))
    service = SimpleNamespace(
        due=AsyncMock(return_value=date(2026, 10, 16)),
        run_day=AsyncMock(side_effect=RuntimeError("database is locked")),
    )
    deliver = AsyncMock(side_effect=RuntimeError("Telegram is away"))
    scheduler = MonitoringScheduler(service, deliver, retry_after=900, clock=clock)
    assert await scheduler.tick() is None  # neither failure escapes the loop
    clock.now += timedelta(minutes=1)
    await scheduler.tick()
    assert service.run_day.await_count == 1
    clock.now += timedelta(minutes=15)
    await scheduler.tick()
    assert service.run_day.await_count == 2
