from dataclasses import replace

from sqlalchemy import func, select

from app.db import org_transaction
from app.models.tables import AuditLog, Finding, MatchLink, Record
from app.pipeline.verify import Verdict
from tests.integration.conftest import patient_a, patient_b, patient_c


def links(org) -> list[MatchLink]:
    with org_transaction(org.id) as session:
        return list(session.scalars(select(MatchLink)))


def findings(org, code: str) -> list[Finding]:
    with org_transaction(org.id) as session:
        return list(session.scalars(select(Finding).where(Finding.code == code)))


def ingest(client, org, body) -> dict:
    response = client.post("/v1/records", json=body, headers=org.headers("ingest"))
    assert response.status_code in (200, 201), response.text
    return response.json()


def test_same_patient_from_a_and_b_is_linked_after_verification(client, org, run_jobs):
    a = ingest(client, org, patient_a())
    b = ingest(client, org, patient_b())
    (link,) = links(org)
    assert link.status == "pending_verification"
    assert link.facts["phone"] == "agree"
    assert link.facts["given_name"] == "nickname"

    # Until verification finishes, nothing treats them as one person.
    group = client.get(f"/v1/patients/{a['record']['id']}/matches", headers=org.headers("reader"))
    assert [m["id"] for m in group.json()["members"]] == [a["record"]["id"]]

    assert run_jobs(org) == ["completed"]
    (link,) = links(org)
    assert (link.status, link.decided_by) == ("match", "verifier")
    assert link.verdict is not None
    assert link.verdict["verdict"] == "agree"
    group = client.get(
        f"/v1/patients/{a['record']['id']}/matches", headers=org.headers("reader")
    ).json()
    assert {m["id"] for m in group["members"]} == {a["record"]["id"], b["record"]["id"]}
    assert group["conflicts"] == []


def test_swapped_birth_date_links_and_records_a_conflict_with_both_values(client, org, run_jobs):
    ingest(client, org, patient_a())
    ingest(client, org, patient_b(date_of_birth="03-07-1984"))  # day and month swapped
    run_jobs(org)
    (link,) = links(org)
    assert link.status == "match"
    (conflict,) = findings(org, "CONFLICT_BIRTHDATE")
    values = {conflict.details["value"], conflict.details["other_value"]}
    assert values == {"1984-03-07", "1984-07-03"}


def test_same_name_and_birth_date_without_a_strong_identifier_goes_to_review(client, org, run_jobs):
    ingest(client, org, patient_a(ssn="", phone=""))
    ingest(client, org, patient_c(hospital_no="C999", ssn="", tel="555 999 0000", postcode="94110"))
    run_jobs(org)
    (link,) = links(org)
    assert link.status == "review"
    queue = client.get("/v1/matches", headers=org.headers("reader")).json()
    assert [item["id"] for item in queue["items"]] == [str(link.id)]
    assert queue["items"][0]["facts"]["birth_date"] == "agree"
    assert queue["items"][0]["record"]["source"] in {"source-a", "source-c"}


def test_namesakes_with_different_national_ids_are_flagged_not_merged(client, org, run_jobs):
    ingest(client, org, patient_a())
    ingest(client, org, patient_c(hospital_no="C777", ssn="987-65-4321", tel="555 999 0000"))
    run_jobs(org)
    assert links(org) == []
    (namesake,) = findings(org, "POSSIBLE_NAMESAKE")
    assert "national ids differ" in namesake.details["reason"]


def test_verifier_veto_sends_the_pair_to_review(client, org, run_jobs):
    ingest(client, org, patient_a())
    ingest(client, org, patient_b(pincode="94110"))
    run_jobs(org)
    (link,) = links(org)
    assert link.status == "review"
    assert link.verdict is not None
    assert link.verdict["contradictions"] == ["postal_code"]


class _BrokenVerifier:
    name, version = "broken", "1"

    def verify(self, facts) -> Verdict:
        raise ConnectionError


def test_a_failing_verifier_fails_closed(client, org, run_jobs, job_context):
    ingest(client, org, patient_a())
    ingest(client, org, patient_b())
    broken = replace(job_context, verifier=_BrokenVerifier())
    run_jobs(org, broken)
    (link,) = links(org)
    assert link.status == "review"
    assert link.verdict is not None
    assert link.verdict["failure"] == "raised ConnectionError"


def test_reviewer_decisions_are_applied_and_audited(client, org, run_jobs):
    ingest(client, org, patient_a(ssn="", phone=""))
    ingest(client, org, patient_c(hospital_no="C999", ssn="", tel="555 999 0000"))
    run_jobs(org)
    (link,) = links(org)
    assert (
        client.post(
            f"/v1/matches/{link.id}/decision",
            json={"decision": "different"},
            headers=org.headers("reader"),
        ).status_code
        == 403
    )
    decided = client.post(
        f"/v1/matches/{link.id}/decision",
        json={"decision": "different"},
        headers=org.headers("admin"),
    )
    assert decided.json()["status"] == "different"
    assert decided.json()["decided_by"] == "human"
    assert len(findings(org, "POSSIBLE_NAMESAKE")) == 1
    with org_transaction(org.id) as session:
        entry = session.scalars(select(AuditLog).where(AuditLog.action == "match.decision")).one()
    assert entry.details == {
        "decision": "different",
        "previous_status": "review",
        "new_status": "different",
    }

    same = client.post(
        f"/v1/matches/{link.id}/decision", json={"decision": "same"}, headers=org.headers("admin")
    )
    assert same.json()["status"] == "match"


def test_same_measurement_from_two_sources_is_stored_once(client, org, run_jobs):
    ingest(client, org, patient_a())
    ingest(client, org, patient_b())
    run_jobs(org)
    glucose_a = {
        "source": "source-a",
        "record_type": "Observation",
        "payload": {
            "obs_id": "A-1-GLU",
            "mrn": "0000123",
            "test": "GLU",
            "result": "99",
            "units": "mg/dL",
            "taken_at": "10/01/2026 10:30",
        },
    }
    # The same test, written in Chicago local time (one hour behind New York).
    glucose_b = {
        "source": "source-b",
        "record_type": "Observation",
        "payload": {
            "result_id": "B-1-2",
            "patient_id": "B-00123",
            "test_code": "FBS",
            "value": "5.5",
            "uom": "mmol/L",
            "collected_at": "2026-10-01T09:30:00",
        },
    }
    first = ingest(client, org, glucose_a)
    second = client.post("/v1/records", json=glucose_b, headers=org.headers("ingest"))
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate_record"
    assert second.json()["duplicate_of"] == first["record"]["id"]
    with org_transaction(org.id) as session:
        stored = session.scalar(
            select(func.count()).select_from(Record).where(Record.record_type == "Observation")
        )
    assert stored == 1
    (finding,) = findings(org, "DUPLICATE_RECORD")
    assert finding.details["values_differ"] is False
    assert finding.details["existing_source"] == "source-a"
