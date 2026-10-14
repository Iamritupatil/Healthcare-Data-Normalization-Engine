import random
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update

from app.config import get_settings
from app.db import org_transaction
from app.jobs.runner import _upload_chunk, claim_next_job, process_job
from app.main import create_app
from app.models.tables import IngestJob, QuarantineItem, RawRecord, Record
from app.services.uploads import parse_csv
from scripts.generate_data import make_people, patient_rows, to_csv
from tests.integration.conftest import retire_all_open_jobs
from tests.integration.test_auth import assert_problem


def source_a_csv(rows: int, bad: int, seed: int = 7) -> bytes:
    people = make_people(rows, random.Random(seed))
    return to_csv(patient_rows("source-a", people, bad=bad, seed=seed)).encode()


def upload(client, org, content: bytes, source: str = "source-a", record_type: str = "Patient"):
    return client.post(
        "/v1/uploads",
        files={"file": ("patients.csv", content, "text/csv")},
        data={"source": source, "record_type": record_type},
        headers=org.headers("ingest"),
    )


def job(org, job_id: str) -> IngestJob:
    with org_transaction(org.id) as session:
        return session.get_one(IngestJob, UUID(job_id))


def test_a_thousand_rows_with_five_bad_ones(client, org, run_jobs):
    response = upload(client, org, source_a_csv(1000, bad=5))
    assert response.status_code == 202
    body = response.json()
    assert (body["status"], body["total_rows"]) == ("pending", 1000)
    assert response.headers["location"] == f"/v1/jobs/{body['id']}"

    assert run_jobs(org)[0] == "completed_with_errors"
    status = client.get(f"/v1/jobs/{body['id']}", headers=org.headers("ingest")).json()
    assert status["status"] == "completed_with_errors"
    assert (status["processed_rows"], status["normalized_rows"], status["failed_rows"]) == (
        1000,
        995,
        5,
    )
    with org_transaction(org.id) as session:
        quarantined = session.scalar(select(func.count()).select_from(QuarantineItem))
        normalized = session.scalar(select(func.count()).select_from(Record))
        raw = session.scalar(select(func.count()).select_from(RawRecord))
    # Nothing is dropped: every row ends normalized or quarantined.
    assert (raw, normalized, quarantined) == (1000, 995, 5)


def test_a_crashed_worker_is_taken_over_and_nothing_is_duplicated(
    client, org, owner_engine, job_context, owner
):
    retire_all_open_jobs(owner_engine)
    content = source_a_csv(1200, bad=0, seed=11)
    job_id = upload(client, org, content).json()["id"]
    assert claim_next_job(job_context.settings) == (UUID(job_id), org.id)
    rows = parse_csv(content, max_rows=10_000).rows
    mapping = job_context.registry.active("source-a")
    # The worker finished one chunk, then died without a word.
    _upload_chunk(UUID(job_id), org.id, mapping, "Patient", rows[:500], 0)
    with owner(org.id) as session:
        session.execute(
            update(IngestJob)
            .where(IngestJob.id == job_id)
            .values(heartbeat_at=datetime.now(UTC) - timedelta(hours=1))
        )
    # Another worker takes the stale job over and resumes after the saved chunk.
    assert claim_next_job(job_context.settings) == (UUID(job_id), org.id)
    assert process_job(UUID(job_id), org.id, job_context) == "completed"
    finished = job(org, job_id)
    assert (finished.attempts, finished.processed_rows, finished.normalized_rows) == (2, 1200, 1200)
    with org_transaction(org.id) as session:
        assert session.scalar(select(func.count()).select_from(RawRecord)) == 1200
        assert session.scalar(select(func.count()).select_from(Record)) == 1200


def test_replaying_a_chunk_whose_progress_was_lost_creates_no_duplicates(
    client, org, run_jobs, owner
):
    job_id = upload(client, org, source_a_csv(300, bad=0, seed=3)).json()["id"]
    run_jobs(org)
    with owner(org.id) as session:
        session.execute(
            update(IngestJob)
            .where(IngestJob.id == job_id)
            .values(status="pending", processed_rows=0, normalized_rows=0, duplicate_rows=0)
        )
    run_jobs(org)
    replayed = job(org, job_id)
    assert (replayed.normalized_rows, replayed.duplicate_rows) == (300, 300)
    with org_transaction(org.id) as session:
        assert session.scalar(select(func.count()).select_from(RawRecord)) == 300


def test_two_workers_never_take_the_same_job(client, make_org, owner_engine, job_context):
    retire_all_open_jobs(owner_engine)
    tenants = [make_org() for _ in range(3)]
    expected = set()
    for tenant in tenants:
        for _ in range(4):
            expected.add(upload(client, tenant, source_a_csv(5, bad=0)).json()["id"])
    claims: list[tuple] = []
    lock = threading.Lock()

    def worker() -> None:
        while (claimed := claim_next_job(job_context.settings)) is not None:
            with lock:
                claims.append(claimed)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    claimed_ids = [str(job_id) for job_id, _ in claims]
    assert len(claimed_ids) == len(set(claimed_ids)) == 12
    assert set(claimed_ids) == expected


def test_graceful_stop_puts_the_job_back_after_the_current_chunk(
    client, org, run_jobs, job_context, monkeypatch
):
    job_id = upload(client, org, source_a_csv(1200, bad=0, seed=5)).json()["id"]
    chunks_done: list[int] = []

    def counting_chunk(*args):
        result = _upload_chunk(*args)
        chunks_done.append(args[-1])
        return result

    monkeypatch.setattr("app.jobs.runner._upload_chunk", counting_chunk)
    # SIGTERM arrives while the first chunk is being processed.
    stopping = replace(job_context, should_stop=lambda: len(chunks_done) > 0)
    assert run_jobs(org, stopping) == ["pending"]
    monkeypatch.undo()
    paused = job(org, job_id)
    assert (paused.status, paused.processed_rows, paused.attempts) == ("pending", 500, 0)
    assert run_jobs(org)[0] == "completed"
    assert job(org, job_id).processed_rows == 1200


def test_upload_must_have_the_id_column(client, org):
    response = upload(client, org, b"name,dob\nBob,01/01/1980\n")
    assert "missing the id column" in assert_problem(response, 400, "bad-request")["detail"]


def test_oversized_upload_is_413_before_it_is_read(owner_engine, org):
    from fastapi.testclient import TestClient

    small = get_settings().model_copy(update={"upload_max_bytes": 1_000})
    with TestClient(create_app(settings=small)) as client:
        response = upload(client, org, b"mrn\n" + b"1\n" * 40_000)
    assert_problem(response, 413, "payload-too-large")


def test_a_body_without_content_length_is_still_capped(client, org):
    def stream() -> Iterator[bytes]:
        yield b'{"source": "source-a", "record_type": "Patient", "payload": {"x": "'
        for _ in range(1100):
            yield b"a" * 1024
        yield b'"}}'

    response = client.post(
        "/v1/records",
        content=stream(),
        headers=org.headers("ingest", **{"content-type": "application/json"}),
    )
    assert "content-length" not in response.request.headers
    assert_problem(response, 413, "payload-too-large")
