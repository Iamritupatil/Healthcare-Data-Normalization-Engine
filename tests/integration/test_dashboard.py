"""The dashboard page, its static files, and the two endpoints it relies on."""

from pathlib import Path

import pytest

from tests.integration.test_auth import assert_problem
from tests.integration.test_uploads_jobs import upload

SAMPLES = Path("app/static/samples")


def test_dashboard_is_served_with_a_strict_content_security_policy(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert '<script src="/ui/app.js"' in response.text
    assert "<script>" not in response.text  # no inline script for the policy to allow


@pytest.mark.parametrize("path", ["/ui/app.js", "/ui/app.css", "/ui/favicon.svg"])
def test_static_files_are_served(client, path):
    assert client.get(path).status_code == 200


def test_me_names_the_org_and_role(client, org):
    body = client.get("/v1/me", headers=org.headers("reader")).json()
    assert (body["org_id"], body["role"]) == (str(org.id), "reader")
    assert body["org_name"].startswith("Test Hospital")
    assert_problem(client.get("/v1/me"), 401, "unauthorized")


def test_sources_list_the_columns_each_source_sends(client, org):
    items = client.get("/v1/sources", headers=org.headers("ingest")).json()["items"]
    assert [item["source"] for item in items] == ["source-a", "source-b", "source-c"]
    patient = items[0]["record_types"]["Patient"]
    assert patient["id_columns"] == ["mrn"]
    assert {"mrn", "dob", "ssn"} <= set(patient["columns"])
    assert items[1]["timezone"] == "America/Chicago"


@pytest.mark.parametrize(
    ("source", "record_type", "filename"),
    [
        ("source-a", "Patient", "source-a-patients.csv"),
        ("source-b", "Patient", "source-b-patients.csv"),
        ("source-c", "Patient", "source-c-patients.csv"),
    ],
)
def test_sample_files_upload_and_quarantine_their_broken_rows(
    client, org, run_jobs, source, record_type, filename
):
    served = client.get(f"/ui/samples/{filename}")
    assert served.status_code == 200
    assert served.content == (SAMPLES / filename).read_bytes()
    job = upload(client, org, served.content, source=source, record_type=record_type).json()
    run_jobs(org)
    finished = client.get(f"/v1/jobs/{job['id']}", headers=org.headers("ingest")).json()
    assert finished["status"] == "completed_with_errors"
    assert finished["failed_rows"] == 3
    assert finished["normalized_rows"] == finished["total_rows"] - 3
