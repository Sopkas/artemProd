"""Stored AI explanations (S5-02): one step per counterparty, keyed by every version.

An answer is kept the moment it is received — accepted or rejected — under the step key
``(inn, "explanation", <provider:model:instruction:context:schema:rules>)``, so the same
answer is never bought twice: a resumed run and a re-sent report read the step, and a
change of any version is a new key. An unavailable model leaves no step (S5-03 decides
about retries and budget), so the next attempt may still get an answer.

The payload holds the raw answer text, the review result and the cost; it lives in the
run's own storage next to the assessment it explains and is purged with the run (S6-02).
The raw text may quote the customer's comments when they were sent: it stays in the
run's storage and never reaches the logs.
"""

import json
from dataclasses import dataclass
from datetime import date

from claims_assistant.domain.ai_review import Explanation, Promise, Rejected, RejectionCode
from claims_assistant.domain.serialization import PayloadError

from .recommendation import AiErrorCode, AiUnavailable, ExplanationOutcome

EXPLANATION_STEP = "explanation"
PAYLOAD_SCHEMA = 1


@dataclass(frozen=True, slots=True)
class StoredExplanation:
    """What the step keeps of one attempt; ``explanation`` is present only when accepted."""

    status: str  # accepted | rejected:<code> | unavailable:<code>
    versions: str
    provider: str
    model: str
    reference: str | None  # how the counterparty was named in the request, never the INN
    answer_text: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    explanation: Explanation | None = None
    rejected: Rejected | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def final(self) -> bool:
        """Accepted or rejected answers are final; an unavailable model may be retried."""
        return self.explanation is not None or self.rejected is not None

    @classmethod
    def from_outcome(
        cls, outcome: ExplanationOutcome, reference: str | None
    ) -> "StoredExplanation":
        answer = outcome.answer
        return cls(
            status=outcome.status,
            versions=outcome.versions,
            provider=outcome.provider,
            model=outcome.model,
            reference=reference,
            answer_text=answer.text if answer else None,
            input_tokens=answer.input_tokens if answer else 0,
            output_tokens=answer.output_tokens if answer else 0,
            latency_seconds=answer.latency_seconds if answer else 0.0,
            explanation=outcome.explanation,
            rejected=outcome.rejected,
            error_code=outcome.error.code.value if outcome.error else None,
            error_message=outcome.error.message if outcome.error else None,
        )

    def to_payload(self) -> str:
        explanation = None
        if self.explanation is not None:
            explanation = {
                "text": self.explanation.text,
                "promises": [
                    {
                        "interaction_id": p.interaction_id,
                        "due_on": p.due_on.isoformat(),
                        "quote": p.quote,
                    }
                    for p in self.explanation.promises
                ],
                "grounds": list(self.explanation.grounds),
                "schema_version": self.explanation.schema_version,
            }
        rejected = None
        if self.rejected is not None:
            rejected = {
                "code": self.rejected.code.value,
                "reason": self.rejected.reason,
                "detail": self.rejected.detail,
            }
        return json.dumps(
            {
                "schema": PAYLOAD_SCHEMA,
                "status": self.status,
                "versions": self.versions,
                "provider": self.provider,
                "model": self.model,
                "reference": self.reference,
                "answer_text": self.answer_text,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "latency_seconds": self.latency_seconds,
                "explanation": explanation,
                "rejected": rejected,
                "error_code": self.error_code,
                "error_message": self.error_message,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_payload(cls, payload: str) -> "StoredExplanation":
        try:
            data = json.loads(payload)
            if data.get("schema") != PAYLOAD_SCHEMA:
                raise PayloadError("explanation payload has another schema")
            explanation = None
            if data["explanation"] is not None:
                raw = data["explanation"]
                explanation = Explanation(
                    text=_str(raw["text"]),
                    promises=tuple(
                        Promise(
                            interaction_id=_str(p["interaction_id"]),
                            due_on=date.fromisoformat(_str(p["due_on"])),
                            quote=_opt_str(p.get("quote")),
                        )
                        for p in raw["promises"]
                    ),
                    grounds=tuple(_str(g) for g in raw["grounds"]),
                    schema_version=_str(raw["schema_version"]),
                )
            rejected = None
            if data["rejected"] is not None:
                raw = data["rejected"]
                rejected = Rejected(
                    code=RejectionCode(_str(raw["code"])),
                    reason=_str(raw["reason"]),
                    detail=_opt_str(raw.get("detail")),
                )
            return cls(
                status=_str(data["status"]),
                versions=_str(data["versions"]),
                provider=_str(data["provider"]),
                model=_str(data["model"]),
                reference=_opt_str(data.get("reference")),
                answer_text=_opt_str(data.get("answer_text")),
                input_tokens=_int(data.get("input_tokens", 0)),
                output_tokens=_int(data.get("output_tokens", 0)),
                latency_seconds=_number(data.get("latency_seconds", 0.0)),
                explanation=explanation,
                rejected=rejected,
                error_code=_opt_str(data.get("error_code")),
                error_message=_opt_str(data.get("error_message")),
            )
        except PayloadError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise PayloadError("explanation payload is not readable") from exc

    def as_error(self) -> AiUnavailable | None:
        if self.error_code is None:
            return None
        return AiUnavailable(AiErrorCode(self.error_code), self.error_message or "")


def _str(value: object) -> str:
    if type(value) is not str:
        raise PayloadError("expected str")
    return value


def _opt_str(value: object) -> str | None:
    return None if value is None else _str(value)


def _int(value: object) -> int:
    if type(value) is not int:
        raise PayloadError("expected int")
    return value


def _number(value: object) -> float:
    # A JSON number only: `true` must not pass as 1.0.
    if type(value) not in (int, float):
        raise PayloadError("expected number")
    return float(value)
