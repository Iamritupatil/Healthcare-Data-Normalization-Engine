from datetime import UTC, datetime

from sqlalchemy import update

from app.models.tables import ApiKey
from tests.integration.conftest import patient_a


def assert_problem(response, status: int, slug: str) -> dict:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"].endswith(f"/problems/{slug}")
    assert body["status"] == status
    assert body["title"]
    assert body["instance"] == response.request.url.path
    assert body["request_id"] == response.headers["x-request-id"]
    return body


def test_health_needs_no_key(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_missing_key_is_401(client):
    assert_problem(client.get("/v1/quarantine"), 401, "unauthorized")


def test_unknown_or_malformed_key_is_401(client, org):
    real = org.keys["reader"]
    forged = real[:-4] + ("AAAA" if not real.endswith("AAAA") else "BBBB")
    for key in ("nonsense", forged):
        assert_problem(
            client.get("/v1/quarantine", headers={"X-API-Key": key}), 401, "unauthorized"
        )


def test_revoked_key_is_401(client, org, owner):
    with owner(org.id) as session:
        session.execute(
            update(ApiKey).where(ApiKey.role == "reader").values(revoked_at=datetime.now(UTC))
        )
    assert_problem(client.get("/v1/quarantine", headers=org.headers("reader")), 401, "unauthorized")


def test_wrong_role_is_403(client, org):
    body = assert_problem(
        client.post("/v1/records", json=patient_a(), headers=org.headers("reader")),
        403,
        "forbidden",
    )
    assert "ingest" in body["detail"]
    # Ingest keys belong to sending systems and cannot read patient data back.
    assert_problem(client.get("/v1/quarantine", headers=org.headers("ingest")), 403, "forbidden")
    assert_problem(client.get("/metrics", headers=org.headers("reader")), 403, "forbidden")


def test_keys_are_stored_only_as_hashes(org, owner):
    with owner(org.id) as session:
        stored = session.query(ApiKey.key_hash, ApiKey.key_prefix).all()
    for key in org.keys.values():
        assert all(key != key_hash and key not in key_hash for key_hash, _ in stored)
        assert any(key.startswith(f"mvk_{prefix}_") for _, prefix in stored)
