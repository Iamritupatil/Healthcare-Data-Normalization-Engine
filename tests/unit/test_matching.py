import time
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from app.pipeline import match
from app.pipeline.match import ComparisonFacts, NameFrequency, PatientFeatures
from app.pipeline.verify import RuleVerifier, Verdict, verify_safely
from app.services.verifiers import VERDICT_SCHEMA, LlmVerifier

ROBERT = PatientFeatures(
    family="miller",
    given="robert",
    middle_initial=None,
    birth_date=date(1984, 3, 7),
    gender="male",
    postal_code="10001",
    phone="5552013344",
    email=None,
    national_id=("http://hl7.org/fhir/sid/us-ssn", "912345678"),
    insurance_member_id=None,
)


def decide(a: PatientFeatures, b: PatientFeatures, frequency: NameFrequency | None = None) -> str:
    facts = match.compare(a, b, frequency)
    return match.decide(match.score(facts), facts)


class TestDecisions:
    def test_same_person_with_a_strong_identifier_goes_to_verification(self):
        other = replace(ROBERT, given="bob", national_id=None)
        assert decide(ROBERT, other) == "pending_verification"

    def test_swapped_day_and_month_still_links(self):
        other = replace(ROBERT, birth_date=date(1984, 7, 3))
        facts = match.compare(ROBERT, other)
        assert facts.birth_date == "swapped"
        assert match.decide(match.score(facts), facts) == "pending_verification"

    def test_same_name_birth_date_and_gender_alone_is_only_review(self):
        stranger = replace(ROBERT, phone="5559990000", postal_code="94110", national_id=None)
        assert decide(ROBERT, stranger) == "review"

    def test_namesake_with_a_different_national_id_never_matches(self):
        namesake = replace(
            ROBERT, phone="5559990000", national_id=("http://hl7.org/fhir/sid/us-ssn", "987654321")
        )
        facts = match.compare(ROBERT, namesake)
        assert match.decide(match.score(facts), facts) == "no_match"
        assert match.is_namesake(facts)

    def test_twins_sharing_phone_and_address_go_to_review(self):
        twin = replace(ROBERT, given="william", national_id=None)
        assert decide(ROBERT, twin) == "review"

    def test_different_people_are_not_linked(self):
        other = replace(
            ROBERT,
            family="sharma",
            given="priya",
            birth_date=date(1990, 1, 2),
            gender="female",
            phone=None,
            national_id=None,
        )
        assert decide(ROBERT, other) == "no_match"

    def test_missing_fields_add_nothing(self):
        sparse = PatientFeatures("miller", "robert", None, None, None, None, None, None, None, None)
        facts = match.compare(ROBERT, sparse)
        assert facts.birth_date == facts.phone == facts.national_id == "missing"
        assert set(match.score(facts).breakdown) == {"family_name", "given_name"}

    def test_ids_from_different_issuers_are_not_compared(self):
        other = replace(ROBERT, national_id=("urn:other-country", "912345678"))
        assert match.compare(ROBERT, other).national_id == "missing"

    def test_common_names_earn_fewer_points(self):
        facts = match.compare(ROBERT, ROBERT, NameFrequency(family=0.08, given=0.02))
        breakdown = match.score(facts).breakdown
        assert (breakdown["family_name"], breakdown["given_name"]) == (1, 1)

    def test_facts_hold_no_patient_data(self):
        dumped = match.compare(ROBERT, ROBERT).model_dump_json()
        for value in ("miller", "robert", "1984", "5552013344", "912345678"):
            assert value not in dumped


def test_features_from_a_canonical_patient():
    resource = {
        "identifier": [
            {"system": "urn:a", "value": "1", "type": "MR"},
            {"system": "urn:ssn", "value": "912345678", "type": "NI"},
        ],
        "name": {"text": "Mary Ann Muñoz", "family": "Muñoz", "given": ["Mary", "Ann"]},
        "birthDate": "1984-03-07",
        "gender": "unknown",
        "telecom": [
            {"system": "phone", "value": "+15552013344"},
            {"system": "email", "value": "m@x.org"},
        ],
    }
    features = PatientFeatures.from_resource(resource)
    assert (features.family, features.given, features.middle_initial) == ("munoz", "mary", "a")
    assert features.gender is None  # "unknown" is no evidence either way
    assert (features.phone, features.email) == ("5552013344", "m@x.org")
    assert features.national_id == ("urn:ssn", "912345678")
    assert (features.birth_year, features.family_prefix) == (1984, "mun")


def test_nicknames_resolve_to_the_formal_name():
    assert match.canonical_given("Bob") == match.canonical_given("robert") == "robert"


AGREE_ALL = match.compare(ROBERT, ROBERT)


class _Slow:
    name, version = "slow", "1"

    def verify(self, facts: ComparisonFacts) -> Verdict:
        time.sleep(1)
        return Verdict(verdict="agree", contradictions=[], fields_used=[])


class _Returns:
    name, version = "fixed", "1"

    def __init__(self, value: object) -> None:
        self.value = value

    def verify(self, facts: ComparisonFacts) -> Any:  # deliberately returns bad values
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class TestVerifier:
    def test_rule_verifier_agrees_when_nothing_contradicts(self):
        result = verify_safely(RuleVerifier(), AGREE_ALL, timeout=5)
        assert result.confirmed

    def test_rule_verifier_vetoes_on_a_different_postal_code(self):
        facts = match.compare(ROBERT, replace(ROBERT, postal_code="94110"))
        result = verify_safely(RuleVerifier(), facts, timeout=5)
        assert not result.confirmed
        assert result.verdict.contradictions == ["postal_code"]

    @pytest.mark.parametrize(
        ("verifier", "failure"),
        [
            (_Slow(), "timed out"),
            (_Returns(RuntimeError("boom")), "raised RuntimeError"),
            (_Returns("{not json"), "returned an invalid verdict"),
            (
                _Returns({"verdict": "yes", "contradictions": [], "fields_used": []}),
                "returned an invalid verdict",
            ),
            (
                _Returns({"verdict": "agree", "contradictions": [], "fields_used": ["shoe_size"]}),
                "cited fields that are not in the facts",
            ),
        ],
    )
    def test_any_failure_counts_as_unsure_which_means_review(self, verifier, failure):
        result = verify_safely(verifier, AGREE_ALL, timeout=0.2)
        assert result.verdict.verdict == "unsure"
        assert not result.confirmed
        assert result.failure == failure

    def test_a_valid_json_verdict_is_accepted(self):
        verdict = '{"verdict": "agree", "contradictions": [], "fields_used": ["phone"]}'
        assert verify_safely(_Returns(verdict), AGREE_ALL, timeout=1).confirmed


class TestLlmVerifier:
    def _client(self, text: str, stop_reason: str = "end_turn"):
        calls: list[dict] = []

        def create(**kwargs):
            calls.append(kwargs)
            block = SimpleNamespace(type="text", text=text)
            return SimpleNamespace(stop_reason=stop_reason, content=[block])

        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        return client, calls

    def test_sends_only_comparison_facts_and_parses_the_verdict(self):
        client, calls = self._client(
            '{"verdict": "disagree", "contradictions": ["gender"], "fields_used": ["gender"]}'
        )
        verdict = LlmVerifier(client, "claude-haiku-5-5").verify(AGREE_ALL)  # type: ignore[arg-type]
        assert verdict.verdict == "disagree"
        (call,) = calls
        assert call["messages"][0]["content"] == AGREE_ALL.model_dump_json()
        assert call["output_config"]["format"]["schema"] == VERDICT_SCHEMA
        # Exactly the configured model sees the facts: no fallback to another one.
        assert call["model"] == "claude-haiku-5-5"
        assert "fallbacks" not in call
        assert "betas" not in call

    def test_a_refusal_or_cut_off_reply_fails_closed(self):
        client, _ = self._client("", stop_reason="refusal")
        result = verify_safely(LlmVerifier(client, "claude-haiku-5-5"), AGREE_ALL, timeout=1)  # type: ignore[arg-type]
        assert result.verdict.verdict == "unsure"
