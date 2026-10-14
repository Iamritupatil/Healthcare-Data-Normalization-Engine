"""Database tables. The schema itself is created only by Alembic migrations (migrations/).

Every table carries `org_id` and is protected by row-level security. Ids are generated
in Python so related rows can be linked before anything is flushed.
"""

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, deferred, mapped_column


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        list[str]: ARRAY(Text),
        datetime: DateTime(timezone=True),
        str: Text,
    }


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (CheckConstraint("role IN ('admin', 'ingest', 'reader')", name="role_valid"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"), index=True)
    name: Mapped[str]
    key_prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    role: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]


class RawRecord(Base):
    """Exactly what was received. The app's database user can insert and read, never change."""

    __tablename__ = "raw_records"
    __table_args__ = (
        UniqueConstraint("org_id", "content_hash", name="uq_raw_records_content"),
        Index(
            "ix_raw_records_source_record", "org_id", "source", "record_type", "source_record_id"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    source: Mapped[str]
    record_type: Mapped[str]
    source_record_id: Mapped[str | None]
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]]
    # clock_timestamp(), not now(): now() is fixed for the whole transaction, and two
    # versions of one record arriving in the same chunk must still be ordered.
    received_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("ingest_jobs.id"))
    row_number: Mapped[int | None]


class Record(Base):
    """A canonical record. Append-only: a corrected source record becomes a new row.

    The current version of a source record is the most recently received one.
    """

    __tablename__ = "records"
    __table_args__ = (
        Index(
            "ix_records_versions",
            "org_id",
            "source",
            "record_type",
            "source_record_id",
            "received_at",
        ),
        Index(
            "ix_records_block_birth_year",
            "org_id",
            "birth_year",
            postgresql_where=text("record_type = 'Patient'"),
        ),
        Index(
            "ix_records_block_family_prefix",
            "org_id",
            "family_prefix",
            postgresql_where=text("record_type = 'Patient'"),
        ),
        Index(
            "ix_records_family_key",
            "org_id",
            "family_key",
            postgresql_where=text("record_type = 'Patient'"),
        ),
        Index(
            "ix_records_given_key",
            "org_id",
            "given_key",
            postgresql_where=text("record_type = 'Patient'"),
        ),
        Index(
            "ix_records_measurement",
            "org_id",
            "code",
            "effective_at",
            postgresql_where=text("record_type = 'Observation'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    raw_record_id: Mapped[UUID] = mapped_column(ForeignKey("raw_records.id"), unique=True)
    source: Mapped[str]
    record_type: Mapped[str]
    source_record_id: Mapped[str]
    mapping_version: Mapped[int]
    resource: Mapped[dict[str, Any]]
    received_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Patients: blocking and name-frequency keys (accent-free, case-folded).
    birth_year: Mapped[int | None]
    family_prefix: Mapped[str | None]
    family_key: Mapped[str | None]
    given_key: Mapped[str | None]
    # Observations: what was measured, for whom (source patient id), and when.
    subject_key: Mapped[str | None]
    code: Mapped[str | None]
    effective_at: Mapped[datetime | None]


class TransformationLog(Base):
    __tablename__ = "transformation_log"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    record_id: Mapped[UUID] = mapped_column(ForeignKey("records.id"), index=True)
    position: Mapped[int]
    field: Mapped[str]
    source_field: Mapped[str | None]
    rule: Mapped[str]
    rule_version: Mapped[int]
    original: Mapped[Any] = mapped_column(JSONB)
    result: Mapped[Any] = mapped_column(JSONB)
    details: Mapped[dict[str, Any]]


class QuarantineItem(Base):
    __tablename__ = "quarantine"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'resolved', 'superseded')", name="status_valid"),
        CheckConstraint("error_class IN ('permanent', 'internal')", name="error_class_valid"),
        Index("ix_quarantine_page", "org_id", "first_seen_at", "id"),
        Index("ix_quarantine_source_record", "org_id", "source", "record_type", "source_record_id"),
        Index("ix_quarantine_reason_codes", "reason_codes", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    raw_record_id: Mapped[UUID] = mapped_column(ForeignKey("raw_records.id"), unique=True)
    source: Mapped[str]
    record_type: Mapped[str]
    source_record_id: Mapped[str | None]
    mapping_version: Mapped[int]
    reason_codes: Mapped[list[str]]
    errors: Mapped[list[Any]]
    error_class: Mapped[str]
    last_error: Mapped[str]
    attempts: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="open")
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    resolved_at: Mapped[datetime | None]
    resolved_record_id: Mapped[UUID | None] = mapped_column(ForeignKey("records.id"))


class MatchLink(Base):
    """A candidate pair. Only status 'match' makes two records count as one person."""

    __tablename__ = "match_links"
    __table_args__ = (
        UniqueConstraint("record_id", "candidate_id", name="uq_match_links_pair"),
        CheckConstraint(
            "status IN ('pending_verification', 'review', 'match', 'different')",
            name="status_valid",
        ),
        CheckConstraint("decided_by IN ('verifier', 'human')", name="decided_by_valid"),
        Index("ix_match_links_candidate", "candidate_id"),
        Index("ix_match_links_page", "org_id", "status", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    record_id: Mapped[UUID] = mapped_column(ForeignKey("records.id"))
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("records.id"))
    points: Mapped[int]
    breakdown: Mapped[dict[str, Any]]
    facts: Mapped[dict[str, Any]]
    status: Mapped[str]
    rule_version: Mapped[int]
    verifier: Mapped[str | None]
    verdict: Mapped[dict[str, Any] | None]
    decided_by: Mapped[str | None]
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint("kind IN ('conflict', 'warning')", name="kind_valid"),
        Index("ix_findings_page", "org_id", "created_at", "id"),
        Index("ix_findings_record", "record_id"),
        Index("ix_findings_other_record", "other_record_id"),
        Index("ix_findings_raw_record", "raw_record_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    kind: Mapped[str]
    code: Mapped[str]
    record_id: Mapped[UUID] = mapped_column(ForeignKey("records.id"))
    other_record_id: Mapped[UUID | None] = mapped_column(ForeignKey("records.id"))
    # Set when the finding is about a received record that was not stored as new data.
    raw_record_id: Mapped[UUID | None] = mapped_column(ForeignKey("raw_records.id"))
    field: Mapped[str | None]
    details: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(default="open")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class IngestJob(Base):
    __tablename__ = "ingest_jobs"
    __table_args__ = (
        CheckConstraint("kind IN ('upload', 'reprocess', 'verify')", name="kind_valid"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'completed_with_errors', 'failed')",
            name="status_valid",
        ),
        Index(
            "ix_ingest_jobs_claimable",
            "created_at",
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"), index=True)
    kind: Mapped[str]
    source: Mapped[str | None]
    record_type: Mapped[str | None]
    filename: Mapped[str | None]
    content: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))
    params: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(default="pending")
    total_rows: Mapped[int] = mapped_column(default=0)
    processed_rows: Mapped[int] = mapped_column(default=0)
    normalized_rows: Mapped[int] = mapped_column(default=0)
    failed_rows: Mapped[int] = mapped_column(default=0)
    duplicate_rows: Mapped[int] = mapped_column(default=0)
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("api_keys.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class AuditLog(Base):
    """Who touched which patient data, and when. The app can append and read, never change."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_org_time", "org_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    api_key_id: Mapped[UUID | None] = mapped_column(ForeignKey("api_keys.id"))
    action: Mapped[str]
    resource_type: Mapped[str]
    resource_id: Mapped[str | None]
    request_id: Mapped[str | None]
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
