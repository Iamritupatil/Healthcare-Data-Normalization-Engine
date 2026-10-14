import json
from dataclasses import replace
from pathlib import Path

from sqlalchemy import func, select

from app.db import org_transaction
from app.models.tables import QuarantineItem, RawRecord, Record
from app.pipeline.mapping import load_registry
from tests.integration.conftest import patient_a, patient_c
from tests.integration.test_auth import assert_problem


def count(org, model, *conditions) -> int:
    with org_transaction(org.id) as session:
        return session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


def test_ingest_returns_the_canonical_record(client, org):
    response = client.post("/v1/records", json=patient_a(), headers=org.headers("ingest"))
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "normalized"
    assert response.headers["location"] == f"/v1/records/{body['record']['id']}"
    resource = body["record"]["resource"]
    assert resource["identifier"][0] == {
        "system": "urn:medvora:source-a:mrn",
        "value": "0000123",
        "type": "MR",
    }
    assert resource["birthDate"] == "1984-03-07"


def test_same_payload_twice_is_one_raw_row_and_the_same_result(client, org):
    first = client.post("/v1/records", json=patient_a(), headers=org.headers("ingest"))
    # Key order and whitespace differ; the content is the same.
    reordered = json.dumps(
        {
            "payload": dict(reversed(patient_a()["payload"].items())),
            "record_type": "Patient",
            "source": "source-a",
        },
        indent=2,
    )
    second = client.post(
        "/v1/records",
        content=reordered,
        headers=org.headers("ingest", **{"content-type": "application/json"}),
    )
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["duplicate"] is True
    assert second.json()["record"] == first.json()["record"]
    assert count(org, RawRecord) == 1


def test_changed_record_becomes_a_new_version(client, org):
    old = client.post("/v1/records", json=patient_a(), headers=org.headers("ingest")).json()
    new = client.post(
        "/v1/records", json=patient_a(zip="10002"), headers=org.headers("ingest")
    ).json()
    assert new["record"]["id"] != old["record"]["id"]
    versions = client.get(
        f"/v1/records/{old['record']['id']}/versions", headers=org.headers("reader")
    ).json()
    assert [(v["version"], v["is_current"]) for v in versions["items"]] == [(2, True), (1, False)]
    old_now = client.get(f"/v1/records/{old['record']['id']}", headers=org.headers("reader"))
    assert old_now.json()["is_current"] is False
    assert count(org, Record) == 2


def test_lineage_traces_every_field_to_its_raw_value_and_rule(client, org):
    record_id = client.post("/v1/records", json=patient_a(), headers=org.headers("ingest")).json()[
        "record"
    ]["id"]
    lineage = client.get(f"/v1/records/{record_id}/lineage", headers=org.headers("reader")).json()
    assert lineage["raw"]["payload"] == patient_a()["payload"]
    assert lineage["mapping"]["version"] == 1
    steps = {step["field"]: step for step in lineage["transformations"]}
    assert steps["birthDate"]["original"] == "03/07/1984"
    assert steps["birthDate"]["result"] == "1984-03-07"
    assert (steps["birthDate"]["rule"], steps["birthDate"]["rule_version"]) == ("date.parse", 1)
    canonical_fields = {"identifier", "name", "birthDate", "gender", "telecom", "address"}
    traced = {"identifier", "name", "birthDate", "gender", "phone", "postalCode", "nationalId"}
    assert traced <= set(steps)
    assert canonical_fields <= set(lineage["record"]["resource"])


def test_decimal_precision_survives_json_end_to_end(client, org):
    body = (
        '{"source": "source-b", "record_type": "Observation", "payload": {"result_id": "R1",'
        ' "patient_id": "B-1", "test_code": "BODYWT", "value": 70.10, "uom": "kg",'
        ' "collected_at": "2026-10-01T10:30:00"}}'
    )
    response = client.post(
        "/v1/records",
        content=body,
        headers=org.headers("ingest", **{"content-type": "application/json"}),
    )
    assert response.status_code == 201
    assert '"valueQuantity":{"value":70.10,' in response.text
    assert response.json()["record"]["resource"]["effectiveDateTime"] == "2026-10-01T15:30:00Z"


def test_quarantined_record_is_a_422_problem_and_is_kept(client, org):
    response = client.post(
        "/v1/records", json=patient_c(birth_date="03/04/1985"), headers=org.headers("ingest")
    )
    body = assert_problem(response, 422, "validation-failed")
    assert body["reason_codes"] == ["DATE_AMBIGUOUS"]
    assert body["errors"][0]["field"] == "birthDate"
    assert body["errors"][0]["source_field"] == "birth_date"
    assert count(org, RawRecord) == 1
    item = client.get(f"/v1/quarantine/{body['quarantine_id']}", headers=org.headers("reader"))
    assert item.json()["status"] == "open"

    again = client.post(
        "/v1/records", json=patient_c(birth_date="03/04/1985"), headers=org.headers("ingest")
    )
    assert again.status_code == 422
    assert (again.json()["quarantine_id"], again.json()["duplicate"]) == (
        body["quarantine_id"],
        True,
    )


def test_corrected_resend_settles_the_quarantined_version(client, org):
    bad = client.post("/v1/records", json=patient_a(dob=""), headers=org.headers("ingest"))
    assert bad.json()["reason_codes"] == ["FIELD_MISSING"]
    fixed = client.post("/v1/records", json=patient_a(), headers=org.headers("ingest"))
    assert fixed.status_code == 201
    item = client.get(
        f"/v1/quarantine/{bad.json()['quarantine_id']}", headers=org.headers("reader")
    )
    assert item.json()["status"] == "superseded"
    assert item.json()["resolved_record_id"] == fixed.json()["record"]["id"]


def test_reprocess_after_a_mapping_fix(client, org, run_jobs, job_context, tmp_path):
    bad = client.post("/v1/records", json=patient_c(sex_code="9"), headers=org.headers("ingest"))
    assert bad.json()["reason_codes"] == ["CODE_UNMAPPED"]

    # The fix ships as a new mapping version; v1 stays, so old log rows still point at it.
    for source in ("source-a", "source-b", "source-c"):
        (tmp_path / source).mkdir()
        (tmp_path / source / "v1.yaml").write_text(
            Path(f"mappings/{source}/v1.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
    v2 = (tmp_path / "source-c" / "v1.yaml").read_text(encoding="utf-8")
    v2 = v2.replace("version: 1", "version: 2").replace(
        '"2": female }', '"2": female, "9": unknown }'
    )
    (tmp_path / "source-c" / "v2.yaml").write_text(v2, encoding="utf-8")
    fixed_context = replace(job_context, registry=load_registry(tmp_path))

    job = client.post(
        "/v1/quarantine/reprocess",
        json={"reason_code": "CODE_UNMAPPED"},
        headers=org.headers("admin"),
    )
    assert job.status_code == 202
    assert job.json()["total_rows"] == 1
    assert run_jobs(org, fixed_context)[0] == "completed"

    item = client.get(
        f"/v1/quarantine/{bad.json()['quarantine_id']}", headers=org.headers("reader")
    ).json()
    assert item["status"] == "resolved"
    assert item["mapping_version"] == 2
    record = client.get(f"/v1/records/{item['resolved_record_id']}", headers=org.headers("reader"))
    assert record.json()["mapping_version"] == 2
    assert record.json()["resource"]["gender"] == "unknown"


def test_dry_run_validates_without_storing(client, org):
    ok = client.post("/v1/validate", json=patient_a(), headers=org.headers("ingest")).json()
    assert ok["valid"] is True
    assert ok["resource"]["birthDate"] == "1984-03-07"
    bad = client.post(
        "/v1/validate", json=patient_a(dob="xx"), headers=org.headers("ingest")
    ).json()
    assert (bad["valid"], bad["reason_codes"]) == (False, ["DATE_UNPARSEABLE"])
    assert count(org, RawRecord) == 0
    assert count(org, QuarantineItem) == 0


def test_bad_requests_are_400_problems(client, org):
    headers = org.headers("ingest")
    unknown = client.post("/v1/records", json=patient_a() | {"source": "nope"}, headers=headers)
    assert "known sources" in assert_problem(unknown, 400, "bad-request")["detail"]
    malformed = client.post(
        "/v1/records", content=b"{not json", headers=headers | {"content-type": "application/json"}
    )
    assert_problem(malformed, 400, "bad-request")
    envelope = client.post(
        "/v1/records", json={"source": "source-a", "payload": {}}, headers=headers
    )
    body = assert_problem(envelope, 400, "bad-request")
    assert body["errors"] == [
        {"field": "record_type", "code": "missing", "message": "Field required"}
    ]
