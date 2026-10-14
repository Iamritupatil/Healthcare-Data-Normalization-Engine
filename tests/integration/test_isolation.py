"""Org isolation is enforced by the database, not just by the code."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError

from app.db import get_engine, org_transaction, system_transaction
from app.models.tables import Finding, MatchLink, RawRecord, Record
from tests.integration.conftest import patient_a, patient_c

ORG_TABLES = [
    "orgs",
    "api_keys",
    "raw_records",
    "records",
    "transformation_log",
    "quarantine",
    "match_links",
    "findings",
    "ingest_jobs",
    "audit_log",
]


def test_other_orgs_record_is_a_404_not_a_403(client, org, other_org):
    created = client.post("/v1/records", json=patient_a(), headers=org.headers("ingest"))
    record_id = created.json()["record"]["id"]
    assert client.get(f"/v1/records/{record_id}", headers=org.headers("reader")).status_code == 200
    for path in ("", "/lineage", "/versions"):
        response = client.get(f"/v1/records/{record_id}{path}", headers=other_org.headers("reader"))
        assert response.status_code == 404
    response = client.get(f"/v1/patients/{record_id}/matches", headers=other_org.headers("reader"))
    assert response.status_code == 404


@pytest.mark.parametrize("table", ORG_TABLES)
def test_query_without_an_org_returns_no_rows(client, org, table):
    client.post("/v1/records", json=patient_a(), headers=org.headers("ingest"))
    with system_transaction() as session:
        assert session.scalar(text(f"SELECT count(*) FROM {table}")) == 0  # noqa: S608


def test_each_org_sees_only_its_own_rows(client, org, other_org):
    client.post("/v1/records", json=patient_a(), headers=org.headers("ingest"))
    client.post("/v1/records", json=patient_a(mrn="999"), headers=other_org.headers("ingest"))
    with org_transaction(org.id) as session:
        assert set(session.scalars(select(RawRecord.org_id))) == {org.id}


def test_the_org_setting_does_not_leak_to_the_next_transaction():
    # One connection, two transactions: exactly what a pooled connection goes through.
    with get_engine().connect() as connection:
        with connection.begin():
            connection.execute(
                text("SELECT set_config('app.org_id', :org, true)"), {"org": str(uuid4())}
            )
            assert connection.scalar(text("SELECT app_current_org()")) is not None
        with connection.begin():
            assert connection.scalar(text("SELECT app_current_org()")) is None


def test_the_owner_is_filtered_too_because_rls_is_forced(client, org, owner):
    client.post("/v1/records", json=patient_a(), headers=org.headers("ingest"))
    with owner() as session:
        assert session.scalar(select(func.count()).select_from(RawRecord)) == 0
    with owner(org.id) as session:
        assert session.scalar(select(func.count()).select_from(RawRecord)) == 1


def test_writing_a_row_for_another_org_is_refused(org, other_org):
    insert = text(
        "INSERT INTO raw_records (id, org_id, source, record_type, content_hash, payload)"
        " VALUES (:id, :org, 's', 'Patient', 'h', '{}')"
    )
    with (
        pytest.raises(ProgrammingError, match="row-level security"),
        org_transaction(org.id) as session,
    ):
        session.execute(insert, {"id": uuid4(), "org": other_org.id})


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE raw_records SET payload = '{}'",
        "DELETE FROM raw_records",
        "UPDATE records SET resource = '{}'",
        "DELETE FROM audit_log",
    ],
)
def test_app_user_cannot_change_raw_records_records_or_the_audit_log(client, org, statement):
    client.post("/v1/records", json=patient_a(), headers=org.headers("ingest"))
    with (
        pytest.raises(ProgrammingError, match="permission denied"),
        org_transaction(org.id) as session,
    ):
        session.execute(text(statement))


def test_matching_never_crosses_orgs(client, org, other_org, run_jobs):
    client.post("/v1/records", json=patient_a(), headers=org.headers("ingest"))
    client.post("/v1/records", json=patient_c(), headers=other_org.headers("ingest"))
    run_jobs(org)
    run_jobs(other_org)
    for tenant in (org, other_org):
        with org_transaction(tenant.id) as session:
            assert session.scalar(select(func.count()).select_from(MatchLink)) == 0
            assert session.scalar(select(func.count()).select_from(Finding)) == 0
            assert session.scalar(select(func.count()).select_from(Record)) == 1
