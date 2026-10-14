"""Database rows to response bodies."""

from typing import Any

from app.models.tables import Finding, IngestJob, MatchLink, QuarantineItem, Record


def record_body(record: Record, *, is_current: bool | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": record.id,
        "source": record.source,
        "record_type": record.record_type,
        "source_record_id": record.source_record_id,
        "mapping_version": record.mapping_version,
        "received_at": record.received_at,
        "resource": record.resource,
    }
    if is_current is not None:
        body["is_current"] = is_current
    return body


def quarantine_body(item: QuarantineItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "raw_record_id": item.raw_record_id,
        "source": item.source,
        "record_type": item.record_type,
        "source_record_id": item.source_record_id,
        "status": item.status,
        "reason_codes": item.reason_codes,
        "errors": item.errors,
        "error_class": item.error_class,
        "last_error": item.last_error,
        "attempts": item.attempts,
        "mapping_version": item.mapping_version,
        "first_seen_at": item.first_seen_at,
        "last_seen_at": item.last_seen_at,
        "resolved_at": item.resolved_at,
        "resolved_record_id": item.resolved_record_id,
    }


def finding_body(finding: Finding) -> dict[str, Any]:
    return {
        "id": finding.id,
        "kind": finding.kind,
        "code": finding.code,
        "status": finding.status,
        "record_id": finding.record_id,
        "other_record_id": finding.other_record_id,
        "field": finding.field,
        "details": finding.details,
        "created_at": finding.created_at,
    }


def link_body(link: MatchLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "record_id": link.record_id,
        "candidate_id": link.candidate_id,
        "status": link.status,
        "points": link.points,
        "breakdown": link.breakdown,
        "facts": link.facts,
        "rule_version": link.rule_version,
        "verdict": link.verdict,
        "decided_by": link.decided_by,
        "decided_at": link.decided_at,
        "created_at": link.created_at,
    }


def job_body(job: IngestJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "source": job.source,
        "record_type": job.record_type,
        "filename": job.filename,
        "total_rows": job.total_rows,
        "processed_rows": job.processed_rows,
        "normalized_rows": job.normalized_rows,
        "failed_rows": job.failed_rows,
        "duplicate_rows": job.duplicate_rows,
        "attempts": job.attempts,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
