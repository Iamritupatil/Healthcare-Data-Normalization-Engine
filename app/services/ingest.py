"""Stages 1 and 6 around the pure pipeline: keep the raw record, then store the outcome.

Everything here runs inside a transaction opened by the caller with the org already set,
so row-level security scopes every query to that org.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.tables import Finding, QuarantineItem, RawRecord, Record, TransformationLog
from app.pipeline import match
from app.pipeline.core import FieldError, ReasonCode
from app.pipeline.ingest import content_hash, source_record_id
from app.pipeline.mapping import MappingRegistry, RecordType, SourceMapping
from app.pipeline.run import Normalized, Rejected, run_pipeline
from app.services import matching
from app.services.records import same_source_record

Status = Literal["normalized", "quarantined", "duplicate_record"]


@dataclass
class IngestOutcome:
    status: Status
    duplicate: bool
    raw_record_id: UUID
    record: Record | None = None
    quarantine: QuarantineItem | None = None
    duplicate_of: UUID | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    pending_link_ids: list[UUID] = field(default_factory=list)


@dataclass(frozen=True)
class _Raw:
    id: UUID
    received_at: datetime
    source: str
    record_type: str
    source_record_id: str | None


@dataclass(frozen=True)
class _Stored:
    status: Literal["normalized", "duplicate_record"]
    record: Record | None = None
    duplicate_of: UUID | None = None
    pending_link_ids: list[UUID] = field(default_factory=list)


def local_today(timezone: str | None) -> date:
    """Today where the source is, so a baby registered just after local midnight is valid."""
    return datetime.now(ZoneInfo(timezone) if timezone else UTC).date()


def ingest_payload(
    session: Session,
    *,
    org_id: UUID,
    mapping: SourceMapping,
    record_type: RecordType,
    payload: Mapping[str, Any],
    job_id: UUID | None = None,
    row_number: int | None = None,
) -> IngestOutcome:
    digest = content_hash(mapping.source, record_type, payload)
    srid = source_record_id(payload, mapping.record_types[record_type].source_record_id)
    inserted = session.execute(
        pg_insert(RawRecord)
        .values(
            id=uuid4(),
            org_id=org_id,
            source=mapping.source,
            record_type=record_type,
            source_record_id=srid,
            content_hash=digest,
            payload=payload,
            job_id=job_id,
            row_number=row_number,
        )
        .on_conflict_do_nothing(constraint="uq_raw_records_content")
        .returning(RawRecord.id, RawRecord.received_at)
    ).one_or_none()
    if inserted is None:
        return _previous_outcome(session, digest)

    raw = _Raw(inserted.id, inserted.received_at, mapping.source, record_type, srid)
    result = run_pipeline(mapping, record_type, payload, today=local_today(mapping.timezone))
    warnings = [warning.as_dict() for warning in result.warnings]
    if isinstance(result, Rejected):
        item = _store_rejected(session, org_id, raw, result)
        return IngestOutcome("quarantined", False, raw.id, quarantine=item, warnings=warnings)
    stored = _store_result(session, org_id, raw, result)
    return IngestOutcome(
        stored.status,
        False,
        raw.id,
        record=stored.record,
        duplicate_of=stored.duplicate_of,
        warnings=warnings,
        pending_link_ids=stored.pending_link_ids,
    )


def _previous_outcome(session: Session, digest: str) -> IngestOutcome:
    """The same payload was received before: report what happened to it then."""
    raw_id = session.scalars(select(RawRecord.id).where(RawRecord.content_hash == digest)).one()
    record = session.scalars(select(Record).where(Record.raw_record_id == raw_id)).one_or_none()
    if record is not None:
        return IngestOutcome("normalized", True, raw_id, record=record)
    duplicate_of = session.scalar(
        select(Finding.record_id).where(
            Finding.raw_record_id == raw_id, Finding.code == matching.DUPLICATE_RECORD
        )
    )
    if duplicate_of is not None:
        return IngestOutcome("duplicate_record", True, raw_id, duplicate_of=duplicate_of)
    item = session.scalars(
        select(QuarantineItem).where(QuarantineItem.raw_record_id == raw_id)
    ).one()
    return IngestOutcome("quarantined", True, raw_id, quarantine=item)


def _store_result(session: Session, org_id: UUID, raw: _Raw, result: Normalized) -> _Stored:
    if raw.record_type == "Observation":
        existing = matching.find_duplicate_measurement(
            session, raw.source, result.source_record_id, result.resource
        )
        if existing is not None:
            # The same measurement from another source is not stored again; the raw
            # record and this finding keep what was received.
            matching.add_duplicate_finding(
                session,
                org_id,
                existing,
                raw.id,
                raw.source,
                result.source_record_id,
                result.resource,
            )
            return _Stored("duplicate_record", duplicate_of=existing.id)
    record = Record(
        id=uuid4(),
        org_id=org_id,
        raw_record_id=raw.id,
        source=raw.source,
        record_type=raw.record_type,
        source_record_id=result.source_record_id,
        mapping_version=result.mapping_version,
        resource=result.resource,
        received_at=raw.received_at,
    )
    features = None
    if raw.record_type == "Patient":
        features = match.PatientFeatures.from_resource(result.resource)
        record.birth_year = features.birth_year
        record.family_prefix = features.family_prefix
        record.family_key = features.family or None
        record.given_key = features.given_key
    else:
        record.subject_key = result.resource["subject"]["identifier"]["value"]
        record.code = result.resource["code"]["coding"][0]["code"]
        record.effective_at = result.resource["effectiveDateTime"]
    session.add(record)
    session.flush()  # the models declare no relationships, so insert the parent first
    session.add_all(
        TransformationLog(
            id=uuid4(),
            org_id=org_id,
            record_id=record.id,
            position=position,
            field=step.field,
            source_field=step.source_field,
            rule=step.rule.name,
            rule_version=step.rule.version,
            original=step.original,
            result=step.result,
            details=step.details,
        )
        for position, step in enumerate(result.transformations)
    )
    session.add_all(
        Finding(
            id=uuid4(),
            org_id=org_id,
            kind="warning",
            code=warning.code.value,
            record_id=record.id,
            field=warning.field,
            details={"message": warning.message},
        )
        for warning in result.warnings
    )
    session.flush()
    _supersede_older_quarantine(session, record)
    pending = []
    if features is not None:
        pending = matching.match_patient(session, org_id, record, features)
    return _Stored("normalized", record=record, pending_link_ids=pending)


def _supersede_older_quarantine(session: Session, record: Record) -> None:
    """A newer good version of a source record settles its older quarantined versions."""
    key = (record.source, record.record_type, record.source_record_id)
    older_raw = select(RawRecord.id).where(
        same_source_record(RawRecord, *key), RawRecord.received_at < record.received_at
    )
    session.execute(
        update(QuarantineItem)
        .where(
            QuarantineItem.status == "open",
            same_source_record(QuarantineItem, *key),
            QuarantineItem.raw_record_id.in_(older_raw),
        )
        .values(status="superseded", resolved_at=func.now(), resolved_record_id=record.id)
        .execution_options(synchronize_session=False)
    )


def _summary(errors: list[FieldError]) -> str:
    return "; ".join(f"{error.field}: {error.code.value}" for error in errors)


def _store_rejected(session: Session, org_id: UUID, raw: _Raw, result: Rejected) -> QuarantineItem:
    # A late-arriving old version that fails is already settled by the newer good one.
    newer_exists = raw.source_record_id is not None and session.scalar(
        select(
            exists().where(
                same_source_record(Record, raw.source, raw.record_type, raw.source_record_id),
                Record.received_at > raw.received_at,
            )
        )
    )
    item = QuarantineItem(
        id=uuid4(),
        org_id=org_id,
        raw_record_id=raw.id,
        source=raw.source,
        record_type=raw.record_type,
        source_record_id=raw.source_record_id,
        mapping_version=result.mapping_version,
        reason_codes=result.reason_codes,
        errors=[error.as_dict() for error in result.errors],
        error_class="permanent",
        last_error=_summary(result.errors),
        attempts=1,
        status="superseded" if newer_exists else "open",
    )
    session.add(item)
    session.flush()
    return item


def quarantine_internal_error(
    session: Session,
    *,
    org_id: UUID,
    mapping: SourceMapping,
    record_type: RecordType,
    payload: Mapping[str, Any],
    job_id: UUID | None,
    row_number: int | None,
    error: BaseException,
) -> None:
    """Keep a row that crashed the pipeline, so nothing is ever dropped silently."""
    digest = content_hash(mapping.source, record_type, payload)
    raw_id = session.scalar(
        pg_insert(RawRecord)
        .values(
            id=uuid4(),
            org_id=org_id,
            source=mapping.source,
            record_type=record_type,
            source_record_id=source_record_id(
                payload, mapping.record_types[record_type].source_record_id
            ),
            content_hash=digest,
            payload=payload,
            job_id=job_id,
            row_number=row_number,
        )
        .on_conflict_do_nothing(constraint="uq_raw_records_content")
        .returning(RawRecord.id)
    )
    if raw_id is None:
        raw_id = session.scalars(select(RawRecord.id).where(RawRecord.content_hash == digest)).one()
    error_name = type(error).__name__
    session.execute(
        pg_insert(QuarantineItem)
        .values(
            id=uuid4(),
            org_id=org_id,
            raw_record_id=raw_id,
            source=mapping.source,
            record_type=record_type,
            source_record_id=None,
            mapping_version=mapping.version,
            reason_codes=[ReasonCode.INTERNAL_ERROR.value],
            errors=[{"field": "*", "code": ReasonCode.INTERNAL_ERROR.value, "message": error_name}],
            error_class="internal",
            last_error=f"{ReasonCode.INTERNAL_ERROR.value}: {error_name}",
            attempts=1,
            status="open",
        )
        .on_conflict_do_nothing(index_elements=["raw_record_id"])
    )


@dataclass(frozen=True)
class ReprocessOutcome:
    resolved: bool
    pending_link_ids: list[UUID] = field(default_factory=list)


def reprocess_item(
    session: Session, item: QuarantineItem, registry: MappingRegistry
) -> ReprocessOutcome:
    """Re-run the pipeline from the stored raw payload with the source's current mapping."""
    raw_row = session.get_one(RawRecord, item.raw_record_id)
    mapping = registry.active(item.source)
    result = run_pipeline(
        mapping,
        cast(RecordType, item.record_type),
        raw_row.payload,
        today=local_today(mapping.timezone),
    )
    now = datetime.now(UTC)
    item.attempts += 1
    item.last_seen_at = now
    item.mapping_version = result.mapping_version
    if isinstance(result, Rejected):
        item.reason_codes = result.reason_codes
        item.errors = [error.as_dict() for error in result.errors]
        item.error_class = "permanent"
        item.last_error = _summary(result.errors)
        session.flush()
        return ReprocessOutcome(resolved=False)
    raw = _Raw(
        raw_row.id,
        raw_row.received_at,
        raw_row.source,
        raw_row.record_type,
        raw_row.source_record_id,
    )
    stored = _store_result(session, item.org_id, raw, result)
    item.status = "resolved"
    item.resolved_at = now
    item.resolved_record_id = stored.record.id if stored.record else stored.duplicate_of
    session.flush()
    return ReprocessOutcome(resolved=True, pending_link_ids=stored.pending_link_ids)
