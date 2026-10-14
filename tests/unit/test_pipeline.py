from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app import json_codec
from app.pipeline.ingest import content_hash
from app.pipeline.mapping import SourceMapping, load_mapping_file, load_registry
from app.pipeline.run import Normalized, Rejected, run_pipeline

TODAY = date(2026, 10, 14)
REGISTRY = load_registry(Path("mappings"))

SAME_PATIENT = {
    "source-a": {
        "mrn": "0000123",
        "patient_name": "ROBERT MILLER",
        "dob": "03/07/1984",
        "sex": "M",
        "ssn": "912-34-5678",
        "phone": "(555) 201-3344",
        "zip": "10001",
    },
    "source-b": {
        "patient_id": "B-00123",
        "first_name": "Robert",
        "last_name": "MILLER",
        "date_of_birth": "07-03-1984",
        "gender": "Male",
        "member_id": "BR12345678",
        "mobile": "+1 555-201-3344",
        "pincode": "10001",
    },
    "source-c": {
        "hospital_no": "C000123",
        "full_name": "MILLER, Robert",
        "birth_date": "1984-03-07",
        "sex_code": "1",
        "ssn": "912345678",
        "tel": "555 201 3344",
        "postcode": "10001",
    },
}


def normalize(source: str, record_type: str, payload: dict) -> Normalized:
    result = run_pipeline(REGISTRY.active(source), record_type, payload, today=TODAY)  # type: ignore[arg-type]
    assert isinstance(result, Normalized), getattr(result, "errors", None)
    return result


@pytest.mark.parametrize("source", sorted(SAME_PATIENT))
def test_every_source_produces_the_same_canonical_patient(source):
    resource = normalize(source, "Patient", SAME_PATIENT[source]).resource
    assert resource["name"] == {"text": "Robert Miller", "family": "Miller", "given": ["Robert"]}
    assert resource["birthDate"] == date(1984, 3, 7)
    assert resource["gender"] == "male"
    assert resource["address"] == [{"postalCode": "10001"}]
    assert resource["telecom"][0]["value"].endswith("5552013344")
    assert resource["meta"] == {"source": source, "mappingVersion": 1}


def test_national_id_is_typed_and_compacted():
    resource = normalize("source-c", "Patient", SAME_PATIENT["source-c"]).resource
    assert {"system": "http://hl7.org/fhir/sid/us-ssn", "value": "912345678", "type": "NI"} in (
        resource["identifier"]
    )


def test_every_canonical_field_has_a_lineage_entry():
    result = normalize("source-a", "Patient", SAME_PATIENT["source-a"])
    logged = {step.field for step in result.transformations}
    assert {
        "identifier",
        "name",
        "birthDate",
        "gender",
        "nationalId",
        "phone",
        "postalCode",
    } <= logged
    birth = next(step for step in result.transformations if step.field == "birthDate")
    assert (birth.source_field, birth.original, birth.rule.name, birth.rule.version) == (
        "dob",
        "03/07/1984",
        "date.parse",
        1,
    )


def test_observation_conversion_is_logged_with_its_factor():
    result = normalize(
        "source-a",
        "Observation",
        {
            "obs_id": "A-1",
            "mrn": "0000123",
            "test": "WT",
            "result": "154",
            "units": "lbs",
            "taken_at": "10/01/2026 10:30",
        },
    )
    quantity = result.resource["valueQuantity"]
    assert quantity == {
        "value": Decimal("69.85"),
        "unit": "kg",
        "system": "http://unitsofmeasure.org",
        "code": "kg",
    }
    conversion = next(t for t in result.transformations if t.field == "valueQuantity")
    assert conversion.rule.name == "unit.lb_av_to_kg"
    assert conversion.details["factor"] == "0.45359237"


def test_all_problems_are_reported_at_once():
    payload = dict(SAME_PATIENT["source-c"], birth_date="03/04/1985", sex_code="9")
    result = run_pipeline(REGISTRY.active("source-c"), "Patient", payload, today=TODAY)
    assert isinstance(result, Rejected)
    assert result.reason_codes == ["CODE_UNMAPPED", "DATE_AMBIGUOUS"]
    assert {error.source_field for error in result.errors} == {"birth_date", "sex_code"}


def test_missing_birth_date_is_rejected():
    payload = dict(SAME_PATIENT["source-a"], dob="")
    result = run_pipeline(REGISTRY.active("source-a"), "Patient", payload, today=TODAY)
    assert isinstance(result, Rejected)
    assert result.reason_codes == ["FIELD_MISSING"]
    assert result.errors[0].field == "birthDate"


def test_unmapped_source_fields_become_a_warning_not_an_error():
    result = normalize(
        "source-a", "Patient", dict(SAME_PATIENT["source-a"], favourite_colour="red")
    )
    assert [w.code.value for w in result.warnings] == ["FIELD_IGNORED"]
    assert "favourite_colour" in result.warnings[0].message


def test_validation_messages_never_echo_the_value():
    payload = dict(SAME_PATIENT["source-a"], dob="12/31/2099")
    result = run_pipeline(REGISTRY.active("source-a"), "Patient", payload, today=TODAY)
    assert isinstance(result, Rejected)
    assert "2099" not in json_codec.dumps([e.as_dict() for e in result.errors])


class TestContentHash:
    def test_key_order_does_not_matter(self):
        assert content_hash("s", "Patient", {"a": 1, "b": "x"}) == content_hash(
            "s", "Patient", {"b": "x", "a": 1}
        )

    def test_any_change_changes_the_hash(self):
        base = content_hash("s", "Patient", {"a": "1"})
        assert base != content_hash("s", "Patient", {"a": "2"})
        assert base != content_hash("other", "Patient", {"a": "1"})
        assert base != content_hash("s", "Observation", {"a": "1"})


class TestMappingFiles:
    def _mapping(self, **changes) -> dict:
        data = yaml.safe_load(Path("mappings/source-a/v1.yaml").read_text(encoding="utf-8"))
        data.update(changes)
        return data

    def test_shipped_mappings_load(self):
        assert REGISTRY.sources() == ["source-a", "source-b", "source-c"]

    def test_typo_in_a_target_fails_loudly(self):
        data = self._mapping()
        data["record_types"]["Patient"]["fields"]["dob"]["to"] = "birthdate"
        with pytest.raises(ValidationError, match="unknown target 'birthdate'"):
            SourceMapping.model_validate(data)

    def test_unknown_time_zone_fails(self):
        with pytest.raises(ValidationError, match="unknown time zone"):
            SourceMapping.model_validate(self._mapping(timezone="Mars/Olympus"))

    def test_gender_codes_must_be_fhir_values(self):
        data = self._mapping()
        data["record_types"]["Patient"]["fields"]["sex"]["codes"]["M"] = "man"
        with pytest.raises(ValidationError, match="not FHIR genders"):
            SourceMapping.model_validate(data)

    def test_required_targets_must_be_mapped(self):
        data = self._mapping()
        del data["record_types"]["Patient"]["fields"]["dob"]
        with pytest.raises(ValidationError, match="birthDate"):
            SourceMapping.model_validate(data)

    def test_file_location_must_match_its_version(self, tmp_path):
        target = tmp_path / "source-a" / "v2.yaml"
        target.parent.mkdir()
        target.write_text(Path("mappings/source-a/v1.yaml").read_text(encoding="utf-8"))
        with pytest.raises(ValueError, match=r"must live at source-a/v1.yaml"):
            load_mapping_file(target)
