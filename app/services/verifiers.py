"""Choosing the match verifier (MATCH_VERIFIER=rule|llm), and the optional LLM verifier.

The LLM verifier is off by default. It is sent comparison facts only, which hold field
outcomes such as "agree" or "missing" and never names, dates or identifiers. Only the
configured model receives them: there is no fallback to another model. Its answer is
constrained to the Verdict JSON schema, and anything unexpected (a refusal, a cut-off
reply, a network error) raises, which verify_safely turns into "unsure", i.e. review.
"""

from typing import Any

import anthropic

from app.config import Settings
from app.pipeline.match import FACT_FIELDS, ComparisonFacts
from app.pipeline.verify import MatchVerifier, RuleVerifier, Verdict

_FIELD_NAMES = sorted(FACT_FIELDS)

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["agree", "disagree", "unsure"]},
        "contradictions": {"type": "array", "items": {"type": "string", "enum": _FIELD_NAMES}},
        "fields_used": {"type": "array", "items": {"type": "string", "enum": _FIELD_NAMES}},
    },
    "required": ["verdict", "contradictions", "fields_used"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You review candidate patient-record matches for a healthcare data pipeline. A scoring \
rule has already found that two records probably describe the same person. Your job is \
to catch contradictions the rule may have missed; you cannot make a match, only stop one.

You receive comparison facts, never the records. Each field says whether the two records \
agree, disagree, are missing a value, or (for names and dates) relate in a specific way: \
"close" means one typo apart, "nickname" a known short form, "swapped" a day/month swap. \
Frequencies are the share of the organisation's patients with that name.

Missing is not disagreement: hospitals collect different fields. Answer "disagree" when \
the facts contain a contradiction that two records of one person would be unlikely to \
show, "unsure" when the facts are too thin to judge, and "agree" otherwise. List the \
contradicting fields in contradictions and every field you relied on in fields_used, \
using only the field names given."""


class LlmVerifier:
    name = "llm"

    def __init__(self, client: anthropic.Anthropic, model: str) -> None:
        self._client = client
        self._model = model
        self.version = model

    def verify(self, facts: ComparisonFacts) -> Verdict:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": facts.model_dump_json()}],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
            },
        )
        if response.stop_reason != "end_turn":
            raise RuntimeError(f"verifier stopped with {response.stop_reason}")
        text = next(block.text for block in response.content if block.type == "text")
        return Verdict.model_validate_json(text)


def get_verifier(settings: Settings) -> MatchVerifier:
    if settings.match_verifier == "rule":
        return RuleVerifier()
    if settings.anthropic_api_key is None:
        raise RuntimeError("MATCH_VERIFIER=llm needs ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        timeout=settings.match_verifier_timeout_seconds,
        max_retries=1,
    )
    return LlmVerifier(client, settings.match_verifier_model)
