"""Job runners and the job processing that every runner shares.

The API only writes a job row and calls `JobRunner.submit`. Whatever runs jobs calls
`process_job`. Today that is the polling worker (worker.py), for which the pending row
itself is the queue. Moving to AWS SQS means adding a runner whose `submit` sends a
message and a consumer that calls `process_job`; the pipeline does not change.

Job kinds:
    upload     a CSV or JSON file, processed in chunks
    reprocess  open quarantine items re-run with the current mapping
    verify     candidate matches waiting for the verifier

Uploads are processed in chunks, one transaction per chunk, with progress saved in the
same transaction. A crash loses at most the current chunk, and re-running a chunk is
harmless because ingestion is idempotent (content hash). Inside a chunk each row runs
in a savepoint, so one bad row cannot abort the rest of the chunk.

Transient errors (lost connection, deadlock, pool timeout) are retried for the whole
chunk, outside any transaction, with backoff. Permanent errors never are.
"""

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import exc as sa_exc
from sqlalchemy import select, text, update
from sqlalchemy.orm import undefer

from app.config import Settings
from app.db import org_transaction, system_transaction
from app.models.tables import IngestJob, MatchLink, QuarantineItem
from app.observability.logging import exception_summary, get_logger
from app.pipeline.core import ReasonCode
from app.pipeline.mapping import MappingRegistry, RecordType, SourceMapping
from app.pipeline.match import ComparisonFacts
from app.pipeline.verify import MatchVerifier, verify_safely
from app.services.ingest import ingest_payload, quarantine_internal_error, reprocess_item
from app.services.matching import apply_verdict, enqueue_verification
from app.services.uploads import UPLOAD_FORMATS, parse_upload

log = get_logger(__name__)


class JobRunner(Protocol):
    def submit(self, job_id: UUID, org_id: UUID) -> None:
        """Hand a committed job to whatever executes jobs."""


class PollingWorkerRunner:
    """The pending row is the queue: a polling worker claims it with SKIP LOCKED."""

    def submit(self, job_id: UUID, org_id: UUID) -> None:
        log.info("job_submitted", job_id=str(job_id))


def get_job_runner(settings: Settings) -> JobRunner:
    if settings.job_runner == "worker":
        return PollingWorkerRunner()
    raise ValueError(f"unknown job runner {settings.job_runner!r}")


@dataclass(frozen=True)
class JobContext:
    """What processing needs besides the job itself."""

    registry: MappingRegistry
    settings: Settings
    verifier: MatchVerifier
    runner: JobRunner
    should_stop: Callable[[], bool] = lambda: False
    sleep: Callable[[float], None] = time.sleep


def is_transient(error: BaseException) -> bool:
    if isinstance(error, sa_exc.OperationalError | sa_exc.InterfaceError | sa_exc.TimeoutError):
        return True
    return isinstance(error, sa_exc.DBAPIError) and error.connection_invalidated


def with_retries[T](
    action: Callable[[], T], delays: tuple[float, ...], sleep: Callable[[float], None] = time.sleep
) -> T:
    for attempt in range(len(delays) + 1):
        try:
            return action()
        except Exception as error:
            if not is_transient(error) or attempt == len(delays):
                raise
            log.warning(
                "transient_error_retrying",
                attempt=attempt + 1,
                retry_in_seconds=delays[attempt],
                **exception_summary(error),
            )
            sleep(delays[attempt])
    raise AssertionError("unreachable")


def claim_next_job(settings: Settings) -> tuple[UUID, UUID] | None:
    with system_transaction() as session:
        row = session.execute(
            text("SELECT job_id, org_id FROM medvora_claim_job(:stale, :max_attempts)"),
            {"stale": settings.job_stale_after_seconds, "max_attempts": settings.job_max_attempts},
        ).one_or_none()
    return (row.job_id, row.org_id) if row else None


def process_job(job_id: UUID, org_id: UUID, context: JobContext) -> str:
    """Run a claimed job to the end, resuming from its saved progress. Returns the final status."""
    with org_transaction(org_id) as session:
        job = session.get_one(IngestJob, job_id, options=[undefer(IngestJob.content)])
        kind, content, start = job.kind, job.content, job.processed_rows
        source, record_type = job.source, cast(RecordType | None, job.record_type)
        # Jobs created before JSON uploads existed have no format: they are CSV.
        upload_type = job.params.get("format", "csv")

    log.info("job_started", job_id=str(job_id), kind=kind, resume_from=start)
    delays = context.settings.transient_retry_delays
    if kind == "upload":
        if content is None or source is None or record_type is None:
            raise ValueError(f"upload job {job_id} has no file, source or record type")
        if upload_type not in UPLOAD_FORMATS:
            raise ValueError(f"upload job {job_id} has an unknown format")
        rows = parse_upload(content, upload_type, max_rows=context.settings.upload_max_rows).rows
        mapping = context.registry.active(source)
        size = context.settings.job_chunk_size
        for chunk_start in range(start, len(rows), size):
            if context.should_stop():
                return _release(job_id, org_id)
            chunk = rows[chunk_start : chunk_start + size]
            action = partial(
                _upload_chunk, job_id, org_id, mapping, record_type, chunk, chunk_start
            )
            _submit(context, org_id, with_retries(action, delays, context.sleep))
    elif kind == "reprocess":
        while True:
            if context.should_stop():
                return _release(job_id, org_id)
            reprocess = partial(
                _reprocess_chunk, job_id, org_id, context.registry, context.settings.job_chunk_size
            )
            more, verify_job = with_retries(reprocess, delays, context.sleep)
            _submit(context, org_id, verify_job)
            if not more:
                break
    else:
        with_retries(partial(_verify_links, job_id, org_id, context), delays, context.sleep)
    return _finish(job_id, org_id)


def _submit(context: JobContext, org_id: UUID, job_id: UUID | None) -> None:
    if job_id is not None:
        context.runner.submit(job_id, org_id)


def _upload_chunk(
    job_id: UUID,
    org_id: UUID,
    mapping: SourceMapping,
    record_type: RecordType,
    rows: list[dict[str, Any]],
    chunk_start: int,
) -> UUID | None:
    """One chunk in one transaction. Returns the verification job it queued, if any."""
    counts: Counter[str] = Counter()
    pending: list[UUID] = []
    verify_job = None
    with org_transaction(org_id) as session:
        for offset, row in enumerate(rows):
            row_number = chunk_start + offset + 1
            try:
                with session.begin_nested():
                    outcome = ingest_payload(
                        session,
                        org_id=org_id,
                        mapping=mapping,
                        record_type=record_type,
                        payload=row,
                        job_id=job_id,
                        row_number=row_number,
                    )
            except Exception as error:
                if is_transient(error):
                    raise
                log.error(
                    "row_failed", job_id=str(job_id), row=row_number, **exception_summary(error)
                )
                with session.begin_nested():
                    quarantine_internal_error(
                        session,
                        org_id=org_id,
                        mapping=mapping,
                        record_type=record_type,
                        payload=row,
                        job_id=job_id,
                        row_number=row_number,
                        error=error,
                    )
                counts["failed"] += 1
                continue
            pending.extend(outcome.pending_link_ids)
            if outcome.duplicate or outcome.status == "duplicate_record":
                counts["duplicate"] += 1
            counts["failed" if outcome.status == "quarantined" else "normalized"] += 1
        if pending:
            verify_job = enqueue_verification(session, org_id, pending)
        session.execute(
            update(IngestJob)
            .where(IngestJob.id == job_id)
            .values(
                processed_rows=chunk_start + len(rows),
                normalized_rows=IngestJob.normalized_rows + counts["normalized"],
                failed_rows=IngestJob.failed_rows + counts["failed"],
                duplicate_rows=IngestJob.duplicate_rows + counts["duplicate"],
                heartbeat_at=datetime.now(UTC),
            )
        )
    log.info(
        "chunk_done", job_id=str(job_id), rows=len(rows), pending_matches=len(pending), **counts
    )
    return verify_job


def _reprocess_chunk(
    job_id: UUID, org_id: UUID, registry: MappingRegistry, size: int
) -> tuple[bool, UUID | None]:
    counts: Counter[str] = Counter()
    pending: list[UUID] = []
    verify_job = None
    with org_transaction(org_id) as session:
        job = session.get_one(IngestJob, job_id)
        params = dict(job.params)
        query = select(QuarantineItem).where(QuarantineItem.status == "open")
        if params.get("ids"):
            query = query.where(QuarantineItem.id.in_([UUID(value) for value in params["ids"]]))
        if params.get("source"):
            query = query.where(QuarantineItem.source == params["source"])
        if params.get("reason_code"):
            query = query.where(QuarantineItem.reason_codes.contains([params["reason_code"]]))
        if params.get("cursor"):
            query = query.where(QuarantineItem.id > UUID(params["cursor"]))
        items = session.scalars(
            query.order_by(QuarantineItem.id).limit(size).with_for_update(skip_locked=True)
        ).all()
        for item in items:
            try:
                with session.begin_nested():
                    outcome = reprocess_item(session, item, registry)
            except Exception as error:
                if is_transient(error):
                    raise
                log.error("reprocess_failed", job_id=str(job_id), **exception_summary(error))
                session.execute(
                    update(QuarantineItem)
                    .where(QuarantineItem.id == item.id)
                    .values(
                        attempts=QuarantineItem.attempts + 1,
                        last_seen_at=datetime.now(UTC),
                        error_class="internal",
                        last_error=f"{ReasonCode.INTERNAL_ERROR.value}: {type(error).__name__}",
                    )
                )
                counts["failed"] += 1
                continue
            counts["resolved" if outcome.resolved else "failed"] += 1
            pending.extend(outcome.pending_link_ids)
        if pending:
            verify_job = enqueue_verification(session, org_id, pending)
        if items:
            params["cursor"] = str(items[-1].id)
        job.params = params
        job.processed_rows += len(items)
        job.normalized_rows += counts["resolved"]
        job.failed_rows += counts["failed"]
        job.heartbeat_at = datetime.now(UTC)
    log.info("chunk_done", job_id=str(job_id), rows=len(items), **counts)
    return len(items) == size, verify_job


def _verify_links(job_id: UUID, org_id: UUID, context: JobContext) -> None:
    """Ask the verifier about each pending link. The verifier runs outside any transaction."""
    with org_transaction(org_id) as session:
        job = session.get_one(IngestJob, job_id)
        link_ids = [UUID(value) for value in job.params.get("link_ids", [])]
        pending = session.execute(
            select(MatchLink.id, MatchLink.facts).where(
                MatchLink.id.in_(link_ids), MatchLink.status == "pending_verification"
            )
        ).all()
    timeout = context.settings.match_verifier_timeout_seconds
    results = {
        link_id: verify_safely(
            context.verifier, ComparisonFacts.model_validate(facts), timeout=timeout
        )
        for link_id, facts in pending
    }
    counts: Counter[str] = Counter()
    with org_transaction(org_id) as session:
        for link_id, result in results.items():
            link = session.get(MatchLink, link_id, with_for_update=True)
            if link is None:
                continue
            apply_verdict(session, link, result)
            counts[link.status] += 1
            if result.failure:
                counts["verifier_failures"] += 1
        session.execute(
            update(IngestJob)
            .where(IngestJob.id == job_id)
            .values(
                processed_rows=len(link_ids),
                normalized_rows=counts["match"],
                heartbeat_at=datetime.now(UTC),
            )
        )
    log.info("links_verified", job_id=str(job_id), verifier=context.verifier.name, **counts)


def _finish(job_id: UUID, org_id: UUID) -> str:
    with org_transaction(org_id) as session:
        job = session.get_one(IngestJob, job_id)
        job.status = "completed_with_errors" if job.failed_rows else "completed"
        job.finished_at = job.heartbeat_at = datetime.now(UTC)
        status = job.status
        totals = {
            "processed": job.processed_rows,
            "normalized": job.normalized_rows,
            "failed": job.failed_rows,
            "duplicates": job.duplicate_rows,
        }
    log.info("job_finished", job_id=str(job_id), status=status, **totals)
    return status


def _release(job_id: UUID, org_id: UUID) -> str:
    """Graceful stop: put the job back so the next worker resumes it at once."""
    with org_transaction(org_id) as session:
        session.execute(
            update(IngestJob)
            .where(IngestJob.id == job_id, IngestJob.status == "running")
            .values(status="pending", heartbeat_at=None, attempts=IngestJob.attempts - 1)
        )
    log.info("job_released", job_id=str(job_id))
    return "pending"


def mark_failed(job_id: UUID, org_id: UUID, error: BaseException) -> None:
    with org_transaction(org_id) as session:
        session.execute(
            update(IngestJob)
            .where(IngestJob.id == job_id, IngestJob.status == "running")
            .values(
                status="failed",
                finished_at=datetime.now(UTC),
                last_error=f"{type(error).__name__} after retries",
            )
        )
