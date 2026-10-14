"""The final check before two records are treated as one person. It can only veto.

A verifier sees comparison facts (field outcomes, never patient data) for a pair that
already passed the strong-identifier rule. `agree` lets the pair become a match;
`disagree` or `unsure` sends it to human review. A verifier is never asked about pairs
in review, so it cannot promote one.

`verify_safely` wraps every call and fails closed: a timeout, an exception, a reply that
is not a valid verdict, or one that cites a field not in the facts all count as `unsure`.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from app.pipeline.match import FACT_FIELDS, ComparisonFacts


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["agree", "disagree", "unsure"]
    contradictions: list[str]
    fields_used: list[str]


class MatchVerifier(Protocol):
    name: str
    version: str

    def verify(self, facts: ComparisonFacts) -> Verdict: ...


class RuleVerifier:
    """Vetoes when the pair differs on middle initial, postal code or gender."""

    name = "rule"
    version = "1"
    checked = ("middle_initial", "postal_code", "gender")

    def verify(self, facts: ComparisonFacts) -> Verdict:
        contradictions = [field for field in self.checked if getattr(facts, field) == "disagree"]
        return Verdict(
            verdict="disagree" if contradictions else "agree",
            contradictions=contradictions,
            fields_used=list(self.checked),
        )


@dataclass(frozen=True)
class VerificationResult:
    verdict: Verdict
    verifier: str
    failure: str | None = None

    @property
    def confirmed(self) -> bool:
        return self.verdict.verdict == "agree"

    def as_dict(self) -> dict[str, Any]:
        return self.verdict.model_dump() | {"verifier": self.verifier, "failure": self.failure}


_UNSURE = Verdict(verdict="unsure", contradictions=[], fields_used=[])
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="match-verifier")


def verify_safely(
    verifier: MatchVerifier, facts: ComparisonFacts, *, timeout: float
) -> VerificationResult:
    label = f"{verifier.name}@{verifier.version}"
    future = _executor.submit(verifier.verify, facts)
    try:
        raw: Any = future.result(timeout=timeout)
    except Exception as error:
        # A finished future means the verifier itself raised (perhaps a TimeoutError of its own).
        if future.done():
            return VerificationResult(_UNSURE, label, f"raised {type(error).__name__}")
        return VerificationResult(_UNSURE, label, "timed out")
    try:
        if isinstance(raw, Verdict):
            verdict = raw
        elif isinstance(raw, str | bytes):
            verdict = Verdict.model_validate_json(raw)
        else:
            verdict = Verdict.model_validate(raw)
    except ValidationError:
        return VerificationResult(_UNSURE, label, "returned an invalid verdict")
    cited = set(verdict.fields_used) | set(verdict.contradictions)
    if not cited <= FACT_FIELDS:
        return VerificationResult(_UNSURE, label, "cited fields that are not in the facts")
    return VerificationResult(verdict, label)
