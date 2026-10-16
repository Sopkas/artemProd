"""S8-05: a model's reading of the /inn card — what the open data mean for the debt.

The customer (15.10.2026): the card is Checko's data, a lot of text and little use; he
wants an analysis of the client on open data, with the problems — bankruptcy, debts,
lawsuits — named outright. This asks the model once per card, under the same review as
the report (S5-06) and the same money as the checks (S5-03):

- **The month first.** The card reads the month's spend before asking; when the agreed
  ceiling is reached the model is not asked, and the card says so. What one card spent is
  added afterwards — a failed write is logged, never shown (the money is gone anyway).
- **One card, one small budget.** One call and at most one retry, a cap in roubles; a slow
  or failing model leaves the card as the rules made it.
- **The same data, the same answer.** An accepted or rejected answer is kept for a day
  under its versions and the exact request: the same INN asked twice is paid for once. An
  unavailable answer is not kept, so the next card may try again.

Nothing here decides what the user sees on a failure — the outcome says what happened;
the dialog shows the rules' card either way.
"""

import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from claims_assistant.domain.ai_context import build_card_context, build_request
from claims_assistant.domain.scoring import Assessment

from .ai_guard import AiBudget, AiRunLimits, ScopedExplainerFactory
from .ai_spend import AiSpendStore, MonthlyLimit, month_of
from .analysis_repository import RepositoryError
from .check_company import CompanyCheck
from .internal_context import InternalContext
from .recommendation import (
    AiLimits,
    ExplanationOutcome,
    RecommendationProvider,
    explanation_key,
    request_explanation,
)

logger = logging.getLogger(__name__)

# One card: the main call and one retry, a few thousand tokens, under a minute and a half,
# and no more than 3 ₽ — about ten times the price measured on #85 (≈ 0,3 ₽).
CARD_LIMITS = AiRunLimits(max_requests=2, max_tokens=30_000, max_seconds=90.0, max_rub=Decimal("3"))
CARD_CACHE_TTL = timedelta(days=1)
CARD_CACHE_SIZE = 500
# The reference the request names the company by: the INN never leaves (S5-05).
CARD_REFERENCE = "card"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CardReview:
    """What the card got: an outcome of the model, or why it was not asked."""

    outcome: ExplanationOutcome | None = None
    month_exhausted: bool = False
    cached: bool = False

    @property
    def text(self) -> str | None:
        """The accepted reading, or None: then the card stays as the rules made it."""
        if self.outcome is None or self.outcome.explanation is None:
            return None
        return self.outcome.explanation.text


class CardReviewer:
    def __init__(
        self,
        explainer: RecommendationProvider | ScopedExplainerFactory,
        *,
        limits: AiLimits = AiLimits(),
        card_limits: AiRunLimits = CARD_LIMITS,
        spend: AiSpendStore | None = None,
        month_limit_rub: Decimal | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._explainer = explainer
        self._limits = limits
        self._card_limits = card_limits
        self._spend = spend
        self._month_limit = month_limit_rub
        self._clock = clock
        self._kept: OrderedDict[str, tuple[datetime, ExplanationOutcome]] = OrderedDict()

    async def review(
        self, check: CompanyCheck, assessment: Assessment, internal: InternalContext | None
    ) -> CardReview:
        context = build_card_context(
            check.inn,
            assessment,
            internal.run.analysis_date if internal is not None else self._today(check),
            check.snapshots,
            row=internal.row if internal is not None else None,
            indicators=internal.indicators if internal is not None else None,
            reference=CARD_REFERENCE,
        )
        key = self._key(context)
        kept = self._get(key)
        if kept is not None:
            return CardReview(kept, cached=True)
        limits = await self._money_for_this_card()
        if limits is None:
            logger.info("ai_card_month_exhausted")
            return CardReview(month_exhausted=True)
        budget = AiBudget.for_run(limits, self._clock())
        explainer = self._explainer
        if isinstance(explainer, ScopedExplainerFactory):
            explainer = explainer.scoped(budget)
        outcome = await request_explanation(explainer, context, self._limits)
        await self._record_spend(budget)
        if outcome.explanation is not None or outcome.rejected is not None:
            self._put(key, outcome)
        return CardReview(outcome)

    def _today(self, check: CompanyCheck) -> date:
        """The day the source answered: what «today» is for the open data of the card."""
        return max(snapshot.fetched_at for snapshot in check.snapshots).date()

    def _key(self, context) -> str:
        body = json.dumps(build_request(context).context, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(body.encode()).hexdigest()
        return f"{explanation_key(self._explainer, context)}:{digest}"

    def _get(self, key: str) -> ExplanationOutcome | None:
        item = self._kept.get(key)
        if item is None:
            return None
        stored_at, outcome = item
        if self._clock() - stored_at > CARD_CACHE_TTL:
            del self._kept[key]
            return None
        return outcome

    def _put(self, key: str, outcome: ExplanationOutcome) -> None:
        self._kept[key] = (self._clock(), outcome)
        self._kept.move_to_end(key)
        while len(self._kept) > CARD_CACHE_SIZE:
            self._kept.popitem(last=False)

    async def _money_for_this_card(self) -> AiRunLimits | None:
        """The card's limits with the money left this month; None when none is left.

        Not knowing the balance means not spending: a failed read propagates, and the
        dialog shows the card without the reading.
        """
        if self._spend is None or self._month_limit is None:
            return self._card_limits
        spent = await self._spend.spent(month_of(self._clock()))
        month = MonthlyLimit(limit_rub=self._month_limit, spent_rub=spent)
        if month.reached:
            return None
        left = month.left_rub
        if self._card_limits.max_rub is not None:
            left = min(left, self._card_limits.max_rub)
        return replace(self._card_limits, max_rub=left)

    async def _record_spend(self, budget: AiBudget) -> None:
        if self._spend is None or budget.spent_rub <= 0:
            return
        try:
            total = await self._spend.add(month_of(self._clock()), budget.spent_rub)
        except RepositoryError:
            logger.error("ai_spend_not_recorded card_rub=%s", format(budget.spent_rub, "f"))
            return
        logger.info(
            "ai_spend card_rub=%s month_rub=%s", format(budget.spent_rub, "f"), format(total, "f")
        )
