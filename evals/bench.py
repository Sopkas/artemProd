"""S5-07: ask several models the same nine questions and compare the answers.

Choosing a model by the first one that produced valid JSON is not a choice. This script
runs the scenarios of ``evals/scenarios.py`` against every model named on the command
line and prints one table: how many answers the review accepted, which rejection codes
came up, whether the wording went anywhere it should not have, what the run cost in
roubles and how long it took.

It needs the provider client (``infrastructure/llm/polza.py``) and a key in ``AI_API_KEY``,
so it runs from a machine that can reach the provider — and nothing here is a test: the
offline set that must always pass is ``tests/unit/test_ai_evals.py``.

    PYTHONPATH=src python evals/bench.py --dry-run
    PYTHONPATH=src python evals/bench.py --catalog --limit 15
    PYTHONPATH=src python evals/bench.py --models openai/gpt-4.1-mini,google/gemma-3-27b-it

``--dry-run`` builds every context and prints its size without asking anybody: it needs
no key and no network, and it is the way to check the set itself. ``--catalog`` lists the
newest chat models that accept a strict schema, with their prices, which is where the
eight or ten candidates come from.

What the numbers mean, and what they do not: «принято» counts answers our review (S5-06)
let through, and the word checks catch a model that claimed something it must not claim.
Neither says whether the explanation is any good — the scenarios marked «читать» are
printed in full for a person to judge, and that judgement stays the specialist's (S6-05).
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from claims_assistant.application.ai_guard import (  # noqa: E402
    AiPolicy,
    GuardedRecommendationProvider,
)
from claims_assistant.application.recommendation import (  # noqa: E402
    ExplanationOutcome,
    request_explanation,
    request_size,
)
from claims_assistant.domain.ai_context import build_request  # noqa: E402
from evals.scenarios import SCENARIOS, BenchScenario  # noqa: E402

CATALOG_URL = "https://polza.ai/api/v1/models"
RESULTS = ROOT / "evals" / "results"


@dataclass(slots=True)
class Result:
    """One model's answer to one scenario, with everything worth comparing."""

    model: str
    scenario: str
    attempt: int
    status: str  # accepted | rejected:<code> | unavailable:<code>
    seconds: float
    request_chars: int
    cost_rub: Decimal | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    text: str = ""  # the explanation as accepted, or the raw answer when rejected
    promises: int = 0
    complaints: tuple[str, ...] = ()  # what the word checks caught

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    @property
    def clean(self) -> bool:
        return self.accepted and not self.complaints

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "scenario": self.scenario,
            "attempt": self.attempt,
            "status": self.status,
            "seconds": round(self.seconds, 2),
            "request_chars": self.request_chars,
            "cost_rub": None if self.cost_rub is None else format(self.cost_rub, "f"),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "promises": self.promises,
            "complaints": list(self.complaints),
            "text": self.text,
        }


def judge(scenario: BenchScenario, outcome: ExplanationOutcome) -> tuple[str, ...]:
    """The word checks: what a model claimed that this scenario forbids.

    Deliberately blunt. A clean run here means «ничего запретного не сказано», not «ответ
    хороший»; the difference is what the specialist is for.
    """
    explanation = outcome.explanation
    if explanation is None:
        return ()
    text = explanation.text.casefold()
    complaints = []
    for phrase in scenario.forbidden:
        if phrase.casefold() in text:
            complaints.append(f"сказано «{phrase}»")
    if scenario.mentions and not any(word.casefold() in text for word in scenario.mentions):
        complaints.append("не названо " + " / ".join(scenario.mentions))
    if scenario.promises_expected is not None:
        found = len(explanation.promises)
        if found != scenario.promises_expected:
            complaints.append(f"обещаний {found}, ожидалось {scenario.promises_expected}")
    return tuple(complaints)


def _provider(key: str, model: str, structured: bool, policy: AiPolicy):
    """The provider client behind the same guard the pipeline puts in front of it.

    Without the guard a broken TLS handshake is scored as the model's failure. A on #60
    measured the link to the provider from his network: it drops on about half the
    attempts, which at nine scenarios would fill the table with his channel instead of
    anyone's answers. The guard retries exactly as production does.
    """
    try:
        from claims_assistant.infrastructure.llm.polza import PolzaRecommendationProvider
    except ModuleNotFoundError:
        raise SystemExit(
            "Нет клиента провайдера (infrastructure/llm/polza.py). Замер запускается с "
            "ветки, где он есть; набор сценариев проверяется без него: --dry-run."
        ) from None
    inner = PolzaRecommendationProvider(key, model=model, structured=structured)
    return GuardedRecommendationProvider(inner, policy)


def _text_of(outcome: ExplanationOutcome) -> str:
    if outcome.explanation is not None:
        return outcome.explanation.text
    if outcome.answer is not None:
        return outcome.answer.text
    return outcome.error.message if outcome.error else ""


async def run_model(
    model: str,
    scenarios: tuple[BenchScenario, ...],
    *,
    key: str,
    repeat: int,
    structured: bool,
    policy: AiPolicy,
    pause: float,
) -> list[Result]:
    provider = _provider(key, model, structured, policy)
    limits = policy.limits
    results: list[Result] = []
    for scenario in scenarios:
        context = scenario.build()
        size = request_size(build_request(context))
        for attempt in range(1, repeat + 1):
            started = time.monotonic()
            outcome = await request_explanation(provider, context, limits)
            seconds = time.monotonic() - started
            answer = outcome.answer
            result = Result(
                model=model,
                scenario=scenario.id,
                attempt=attempt,
                status=outcome.status,
                seconds=seconds,
                request_chars=size,
                cost_rub=getattr(answer, "cost_rub", None) if answer else None,
                input_tokens=answer.input_tokens if answer else 0,
                output_tokens=answer.output_tokens if answer else 0,
                text=_text_of(outcome),
                promises=len(outcome.explanation.promises) if outcome.explanation else 0,
                complaints=judge(scenario, outcome),
            )
            results.append(result)
            mark = "+" if result.clean else ("~" if result.accepted else "-")
            print(f"  {mark} {scenario.id:24} {result.status:28} {seconds:5.1f} с", flush=True)
            if pause:
                await asyncio.sleep(pause)
    return results


# --- the report -------------------------------------------------------------------------


@dataclass(slots=True)
class ModelSummary:
    model: str
    results: list[Result] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def accepted(self) -> int:
        return sum(1 for item in self.results if item.accepted)

    @property
    def clean(self) -> int:
        return sum(1 for item in self.results if item.clean)

    @property
    def cost(self) -> Decimal | None:
        known = [item.cost_rub for item in self.results if item.cost_rub is not None]
        return sum(known, Decimal(0)) if known else None

    @property
    def answered(self) -> list[Result]:
        """Calls that came back at all; a dropped connection is not a slow model."""
        return [item for item in self.results if not item.status.startswith("unavailable")]

    @property
    def lost(self) -> int:
        return self.total - len(self.answered)

    @property
    def median_seconds(self) -> float:
        answered = self.answered
        return statistics.median(item.seconds for item in answered) if answered else 0.0

    @property
    def codes(self) -> str:
        counts: dict[str, int] = {}
        for item in self.answered:
            if not item.accepted:
                counts[item.status] = counts.get(item.status, 0) + 1
        return ", ".join(f"{code} × {n}" for code, n in sorted(counts.items())) or "—"


def report(summaries: list[ModelSummary], scenarios: tuple[BenchScenario, ...]) -> str:
    lines = [
        "| Модель | Принято | Без замечаний | Отклонения | Связь оборвалась | "
        "₽ за прогон | Секунды (медиана) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for summary in sorted(summaries, key=lambda s: (-s.clean, s.median_seconds)):
        cost = "—" if summary.cost is None else f"{summary.cost:.3f}"
        lines.append(
            f"| `{summary.model}` | {summary.accepted}/{summary.total} | "
            f"{summary.clean}/{summary.total} | {summary.codes} | {summary.lost} | {cost} | "
            f"{summary.median_seconds:.1f} |"
        )
    lines.append("")
    lines.append(
        "«Связь оборвалась» — вызовы, не дошедшие до модели после повторов; медиана "
        "считается только по дошедшим."
    )
    lines.append("")
    lines.append("Замечания по сценариям:")
    lines.append("")
    for summary in summaries:
        for item in summary.results:
            if item.complaints:
                said = "; ".join(item.complaints)
                lines.append(f"- `{summary.model}` / `{item.scenario}`: {said}")
    if not any(item.complaints for s in summaries for item in s.results):
        lines.append("- нет")
    lines.append("")
    lines.append("Ответы, которые читает человек:")
    lines.append("")
    for scenario in scenarios:
        if not scenario.read_me:
            continue
        lines.append(f"**{scenario.id}** — {scenario.asks}")
        lines.append("")
        for summary in summaries:
            for item in summary.results:
                if item.scenario == scenario.id and item.attempt == 1:
                    lines.append(f"- `{summary.model}`: {item.text or '—'}")
        lines.append("")
    return "\n".join(lines)


# --- the catalogue ----------------------------------------------------------------------


async def show_catalog(key: str, limit: int, structured_only: bool) -> None:
    """The newest chat models that take a strict schema, with their prices in roubles."""
    import aiohttp

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60), trust_env=False
    ) as session:
        async with session.get(
            CATALOG_URL, params={"type": "chat"}, headers={"Authorization": f"Bearer {key}"}
        ) as response:
            if response.status != 200:
                print(f"Каталог не отдан: HTTP {response.status}")
                return
            body = await response.json()
    # The endpoint ignores ?type=chat (A checked), so the filter has to be ours.
    models = [
        item
        for item in body.get("data", [])
        if isinstance(item, dict) and item.get("type") == "chat"
    ]
    rows = []
    for item in models:
        top = item.get("top_provider") or {}
        supported = top.get("supported_parameters") or []
        if structured_only and "response_format" not in supported:
            continue
        pricing = top.get("pricing") or {}
        rows.append(
            (
                item.get("created") or 0,
                item.get("id", "?"),
                pricing.get("prompt_per_million", "?"),
                pricing.get("completion_per_million", "?"),
                top.get("context_length") or 0,
            )
        )
    rows.sort(reverse=True)
    print(f"Чат-моделей всего: {len(models)}; со строгой схемой: {len(rows)}\n")
    print(f"{'модель':48} {'₽/1М вход':>12} {'₽/1М выход':>12} {'контекст':>10}  появилась")
    for created, name, prompt, completion, length in rows[:limit]:
        when = datetime.fromtimestamp(created, UTC).strftime("%Y-%m-%d") if created else "?"
        print(f"{name:48} {prompt:>12} {completion:>12} {length:>10}  {when}")


# --- the command line -------------------------------------------------------------------


def dry_run(scenarios: tuple[BenchScenario, ...]) -> None:
    head = f"{'сценарий':26} {'приоритет':10} {'сигналов':>9} {'комм.':>6} {'символов':>9}"
    print(head + "  что проверяем")
    for scenario in scenarios:
        context = scenario.build()
        size = request_size(build_request(context))
        mark = " (читать)" if scenario.read_me else ""
        print(
            f"{scenario.id:26} {context.priority:10} {len(context.signals):>9} "
            f"{len(context.comments):>6} {size:>9}  {scenario.title}{mark}"
        )
    print("\nМодель не вызывалась: это проверка самого набора.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Замер моделей на сценариях S5-07")
    parser.add_argument("--models", help="через запятую, как в каталоге провайдера")
    parser.add_argument("--scenarios", help="через запятую; по умолчанию все")
    parser.add_argument("--repeat", type=int, default=1, help="прогонов на сценарий")
    parser.add_argument("--timeout", type=float, default=90.0, help="секунд на один вызов")
    parser.add_argument(
        "--retries", type=int, default=2, help="повторов при обрыве связи (не при плохом ответе)"
    )
    parser.add_argument("--pause", type=float, default=0.0, help="пауза между вызовами, секунд")
    parser.add_argument(
        "--no-schema",
        action="store_true",
        help="без response_format: для моделей, которые строгую схему не принимают",
    )
    parser.add_argument("--dry-run", action="store_true", help="собрать контексты, ничего не звать")
    parser.add_argument("--catalog", action="store_true", help="показать модели провайдера")
    parser.add_argument("--limit", type=int, default=20, help="сколько моделей показать")
    parser.add_argument("--out", default=str(RESULTS), help="куда сложить подробности")
    args = parser.parse_args()

    chosen = SCENARIOS
    if args.scenarios:
        wanted = {name.strip() for name in args.scenarios.split(",") if name.strip()}
        chosen = tuple(item for item in SCENARIOS if item.id in wanted)
        if not chosen:
            print("Таких сценариев нет.")
            return 2

    if args.dry_run:
        dry_run(chosen)
        return 0

    key = os.environ.get("AI_API_KEY", "").strip()
    if not key:
        print("Нужен ключ провайдера в AI_API_KEY (в репозиторий он не кладётся).")
        return 2

    if args.catalog:
        asyncio.run(show_catalog(key, args.limit, structured_only=not args.no_schema))
        return 0

    if not args.models:
        print("Укажите --models: что с чем сравниваем.")
        return 2

    models = [name.strip() for name in args.models.split(",") if name.strip()]
    policy = AiPolicy(timeout_seconds=args.timeout, max_retries=args.retries)
    summaries: list[ModelSummary] = []
    for model in models:
        print(f"\n{model}")
        results = asyncio.run(
            run_model(
                model,
                chosen,
                key=key,
                repeat=args.repeat,
                structured=not args.no_schema,
                policy=policy,
                pause=args.pause,
            )
        )
        summaries.append(ModelSummary(model=model, results=results))

    text = report(summaries, chosen)
    print("\n" + text)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}.json").write_text(
        json.dumps(
            [item.to_dict() for summary in summaries for item in summary.results],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / f"{stamp}.md").write_text(text + "\n", encoding="utf-8")
    print(f"\nПодробности: {out / f'{stamp}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
