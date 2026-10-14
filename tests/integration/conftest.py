"""Integration tests run the real app against a real Postgres, as the real database roles.

Every test gets fresh orgs, and row-level security keeps them apart, so tests do not need
to clean up after each other. Set REQUIRE_DB=1 (as CI does) to fail instead of skipping
when Postgres is not reachable.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import build_engine, org_transaction, set_org
from app.jobs.runner import JobContext, PollingWorkerRunner, process_job
from app.main import create_app
from app.models.tables import IngestJob
from app.pipeline.mapping import MappingRegistry, load_registry
from app.pipeline.verify import RuleVerifier
from scripts.seed import create_org

pytestmark = pytest.mark.integration

TABLES = (
    "audit_log, findings, match_links, transformation_log, quarantine, records, "
    "raw_records, ingest_jobs, api_keys, orgs"
)


@pytest.fixture(scope="session")
def owner_engine() -> Iterator[Engine]:
    settings = get_settings()
    engine = build_engine(settings.migration_database_url, pool_size=2)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        if os.environ.get("REQUIRE_DB"):
            raise
        pytest.skip("Postgres is not reachable; start it with `docker compose up -d db`")
    config = Config("alembic.ini")
    config.attributes["configure_logging"] = False
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    yield engine
    engine.dispose()


@dataclass
class Org:
    id: UUID
    keys: dict[str, str]
    name: str = field(default="")

    def headers(self, role: str = "admin", **extra: str) -> dict[str, str]:
        return {"X-API-Key": self.keys[role], **extra}


@pytest.fixture
def make_org(owner_engine: Engine):
    def make(name: str = "Test Hospital") -> Org:
        org_id, keys = create_org(f"{name} {uuid4().hex[:6]}")
        return Org(org_id, keys, name)

    return make


@pytest.fixture
def org(make_org) -> Org:
    return make_org()


@pytest.fixture
def other_org(make_org) -> Org:
    return make_org("Other Hospital")


@pytest.fixture
def registry() -> MappingRegistry:
    return load_registry(get_settings().mappings_dir)


@pytest.fixture
def client(owner_engine: Engine, registry: MappingRegistry) -> Iterator[TestClient]:
    with TestClient(create_app(registry=registry)) as test_client:
        yield test_client


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={"transient_retry_delays": (0.0, 0.0, 0.0)})


@pytest.fixture
def job_context(registry: MappingRegistry, settings: Settings) -> JobContext:
    return JobContext(
        registry=registry, settings=settings, verifier=RuleVerifier(), runner=PollingWorkerRunner()
    )


@pytest.fixture
def run_jobs(job_context: JobContext):
    """Process one org's pending jobs (and the jobs they create) the way a worker would."""

    def run(org: Org, context: JobContext | None = None) -> list[str]:
        statuses: list[str] = []
        while True:
            with org_transaction(org.id) as session:
                job = session.scalars(
                    select(IngestJob)
                    .where(IngestJob.status == "pending")
                    .order_by(IngestJob.created_at)
                    .limit(1)
                ).first()
                if job is None:
                    return statuses
                job.status, job.attempts = "running", job.attempts + 1
                job.heartbeat_at = datetime.now(UTC)
                job_id = job.id
            statuses.append(process_job(job_id, org.id, context or job_context))
            if statuses[-1] == "pending":  # put back by a graceful stop; a stopping worker exits
                return statuses

    return run


@contextmanager
def owner_session(engine: Engine, org_id: UUID | None = None) -> Iterator[Session]:
    """A transaction as the schema owner: still filtered by row-level security (FORCE)."""
    with Session(engine) as session, session.begin():
        if org_id is not None:
            set_org(session, org_id)
        yield session


@pytest.fixture
def owner(owner_engine: Engine):
    return lambda org_id=None: owner_session(owner_engine, org_id)


def retire_all_open_jobs(engine: Engine) -> None:
    """Fail every other org's leftover jobs, so claim tests only see their own."""
    with owner_session(engine) as session:
        session.execute(
            update(IngestJob)
            .where(IngestJob.status.in_(["pending", "running"]))
            .values(status="failed", last_error="retired by test setup")
        )


def patient_a(**overrides: Any) -> dict[str, Any]:
    payload = {
        "mrn": "0000123",
        "patient_name": "Robert Miller",
        "dob": "03/07/1984",
        "sex": "M",
        "ssn": "912-34-5678",
        "phone": "(555) 201-3344",
        "zip": "10001",
    }
    payload.update(overrides)
    return {"source": "source-a", "record_type": "Patient", "payload": payload}


def patient_b(**overrides: Any) -> dict[str, Any]:
    payload = {
        "patient_id": "B-00123",
        "first_name": "Bob",
        "last_name": "MILLER",
        "date_of_birth": "07-03-1984",
        "gender": "Male",
        "member_id": "BR12345678",
        "mobile": "+1 555-201-3344",
        "pincode": "10001",
    }
    payload.update(overrides)
    return {"source": "source-b", "record_type": "Patient", "payload": payload}


def patient_c(**overrides: Any) -> dict[str, Any]:
    payload = {
        "hospital_no": "C000123",
        "full_name": "MILLER, Robert",
        "birth_date": "1984-03-07",
        "sex_code": "1",
        "ssn": "912345678",
        "tel": "555 201 3344",
        "postcode": "10001",
    }
    payload.update(overrides)
    return {"source": "source-c", "record_type": "Patient", "payload": payload}
