"""Errors, request ids, logs, rate limits, pagination, metrics and the audit trail."""

import logging

import pytest
from sqlalchemy import select

from app import json_codec
from app.config import get_settings
from app.db import org_transaction
from app.models.tables import AuditLog
from tests.integration.conftest import patient_a
from tests.integration.test_auth import assert_problem


def test_every_error_status_uses_the_problem_shape(client, org, monkeypatch):
    headers = org.headers("ingest")
    assert_problem(client.post("/v1/records", content=b"[", headers=headers), 400, "bad-request")
    assert_problem(client.get("/v1/quarantine"), 401, "unauthorized")
    assert_problem(client.get("/v1/quarantine", headers=headers), 403, "forbidden")
    missing = "/v1/records/00000000-0000-0000-0000-000000000000"
    assert_problem(client.get(missing, headers=org.headers("reader")), 404, "not-found")
    assert_problem(client.get("/v1/nowhere", headers=headers), 404, "not-found")
    not_a_uuid = client.get("/v1/records/not-a-uuid", headers=org.headers("reader"))
    assert_problem(not_a_uuid, 400, "invalid-request")
    assert_problem(
        client.post("/v1/records", json=patient_a(dob=""), headers=headers),
        422,
        "validation-failed",
    )
    limited = get_settings().model_copy(update={"rate_limit_records_per_minute": 1})
    monkeypatch.setattr("app.api.deps.get_settings", lambda: limited)
    client.post("/v1/records", json=patient_a(), headers=headers)
    over = client.post("/v1/records", json=patient_a(), headers=headers)
    assert_problem(over, 429, "rate-limited")
    assert int(over.headers["retry-after"]) > 0


def test_request_validation_errors_do_not_echo_the_input(client, org):
    response = client.get("/v1/quarantine?limit=ROBERT-MILLER-1984", headers=org.headers("reader"))
    body = assert_problem(response, 400, "invalid-request")
    assert body["errors"][0]["field"] == "query.limit"
    assert "ROBERT" not in response.text


def test_a_callers_request_id_is_kept_and_returned(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-abc.123"})
    assert response.headers["x-request-id"] == "trace-abc.123"
    unsafe = client.get("/health", headers={"X-Request-ID": "bad id\nINJECTED"})
    assert unsafe.headers["x-request-id"].startswith("req_")


def _log_events(caplog) -> list[dict]:
    return [record.msg for record in caplog.records if isinstance(record.msg, dict)]


def test_one_request_id_finds_every_log_line_of_that_request(client, org, caplog):
    caplog.set_level(logging.INFO)
    client.post(
        "/v1/records",
        json=patient_a(),
        headers=org.headers("ingest", **{"X-Request-ID": "find-me-42"}),
    )
    events = [event for event in _log_events(caplog) if event.get("request_id") == "find-me-42"]
    assert {"request"} <= {event["event"] for event in events}
    access = next(event for event in events if event["event"] == "request")
    assert (access["status"], access["path"], access["org_id"]) == (201, "/v1/records", str(org.id))


def test_logs_never_contain_patient_data(client, org, caplog, run_jobs):
    caplog.set_level(logging.DEBUG)
    client.post("/v1/records", json=patient_a(), headers=org.headers("ingest"))
    client.post("/v1/records", json=patient_a(dob="xx"), headers=org.headers("ingest"))
    run_jobs(org)
    logged = json_codec.dumps(_log_events(caplog)) + " ".join(
        r.getMessage() for r in caplog.records
    )
    for value in ("Robert", "Miller", "1984", "03/07", "0000123", "912-34-5678", "201-3344"):
        assert value not in logged


def test_lists_page_with_a_cursor(client, org):
    for number in range(5):
        client.post(
            "/v1/records", json=patient_a(mrn=f"Q{number}", dob=""), headers=org.headers("ingest")
        )
    seen: list[str] = []
    cursor, pages = None, 0
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = client.get("/v1/quarantine", params=params, headers=org.headers("reader")).json()
        seen.extend(item["id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert (pages, len(seen), len(set(seen))) == (3, 5, 5)
    assert page["limit"] == 2
    filtered = client.get(
        "/v1/quarantine",
        params={"reason_code": "FIELD_MISSING", "source": "source-a"},
        headers=org.headers("reader"),
    ).json()
    assert len(filtered["items"]) == 5
    bad = client.get("/v1/quarantine", params={"cursor": "garbage"}, headers=org.headers("reader"))
    assert_problem(bad, 400, "bad-request")
    too_big = client.get("/v1/findings", params={"limit": 500}, headers=org.headers("reader"))
    assert_problem(too_big, 400, "invalid-request")


def test_metrics_count_by_source_and_status(client, org, run_jobs):
    client.post("/v1/records", json=patient_a(), headers=org.headers("ingest"))
    client.post("/v1/records", json=patient_a(mrn="2", dob=""), headers=org.headers("ingest"))
    metrics = client.get("/metrics", headers=org.headers("admin")).json()
    assert metrics["by_source"]["source-a"] == {
        "ingested": 2,
        "normalized": 1,
        "duplicate_records": 0,
        "quarantine": {"open": 1, "resolved": 0, "superseded": 0},
    }
    assert set(metrics["match_links"]) == {"pending_verification", "review", "match", "different"}


@pytest.mark.parametrize("path", ["", "/lineage", "/versions"])
def test_reading_patient_data_is_audited(client, org, path):
    record_id = client.post("/v1/records", json=patient_a(), headers=org.headers("ingest")).json()[
        "record"
    ]["id"]
    response = client.get(
        f"/v1/records/{record_id}{path}",
        headers=org.headers("reader", **{"X-Request-ID": "audit-me"}),
    )
    assert response.status_code == 200
    with org_transaction(org.id) as session:
        entry = session.scalars(select(AuditLog).where(AuditLog.request_id == "audit-me")).one()
    assert entry.resource_id == record_id
    assert entry.action.startswith("record.")
    assert entry.api_key_id is not None
