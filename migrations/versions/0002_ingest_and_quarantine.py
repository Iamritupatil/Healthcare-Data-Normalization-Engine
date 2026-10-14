"""Raw records, canonical records, the transformation log and quarantine.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from migrations.rls import protect_table

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(
    name: str, *, nullable: bool = False, default: bool = True
) -> sa.Column[sa.DateTime]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default else None,
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "raw_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.UniqueConstraint("org_id", "content_hash", name="uq_raw_records_content"),
    )
    op.create_index(
        "ix_raw_records_source_record",
        "raw_records",
        ["org_id", "source", "record_type", "source_record_id"],
    )

    op.create_table(
        "records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column(
            "raw_record_id", sa.Uuid(), sa.ForeignKey("raw_records.id"), nullable=False, unique=True
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Text(), nullable=False),
        sa.Column("mapping_version", sa.Integer(), nullable=False),
        sa.Column("resource", postgresql.JSONB(), nullable=False),
        _timestamp("received_at", default=False),
        _timestamp("created_at"),
        sa.Column("birth_year", sa.Integer(), nullable=True),
        sa.Column("family_prefix", sa.Text(), nullable=True),
        sa.Column("family_key", sa.Text(), nullable=True),
        sa.Column("given_key", sa.Text(), nullable=True),
        sa.Column("subject_key", sa.Text(), nullable=True),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_records_versions",
        "records",
        ["org_id", "source", "record_type", "source_record_id", "received_at"],
    )
    op.create_index(
        "ix_records_block_birth_year",
        "records",
        ["org_id", "birth_year"],
        postgresql_where=sa.text("record_type = 'Patient'"),
    )
    op.create_index(
        "ix_records_block_family_prefix",
        "records",
        ["org_id", "family_prefix"],
        postgresql_where=sa.text("record_type = 'Patient'"),
    )
    op.create_index(
        "ix_records_family_key",
        "records",
        ["org_id", "family_key"],
        postgresql_where=sa.text("record_type = 'Patient'"),
    )
    op.create_index(
        "ix_records_given_key",
        "records",
        ["org_id", "given_key"],
        postgresql_where=sa.text("record_type = 'Patient'"),
    )
    op.create_index(
        "ix_records_measurement",
        "records",
        ["org_id", "code", "effective_at"],
        postgresql_where=sa.text("record_type = 'Observation'"),
    )

    op.create_table(
        "transformation_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("record_id", sa.Uuid(), sa.ForeignKey("records.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("source_field", sa.Text(), nullable=True),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("original", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_transformation_log_record_id", "transformation_log", ["record_id"])

    op.create_table(
        "quarantine",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column(
            "raw_record_id", sa.Uuid(), sa.ForeignKey("raw_records.id"), nullable=False, unique=True
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Text(), nullable=True),
        sa.Column("mapping_version", sa.Integer(), nullable=False),
        sa.Column("reason_codes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("errors", postgresql.JSONB(), nullable=False),
        sa.Column("error_class", sa.Text(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        _timestamp("first_seen_at"),
        _timestamp("last_seen_at"),
        _timestamp("resolved_at", nullable=True, default=False),
        sa.Column("resolved_record_id", sa.Uuid(), sa.ForeignKey("records.id"), nullable=True),
        sa.CheckConstraint("status IN ('open', 'resolved', 'superseded')", name="status_valid"),
        sa.CheckConstraint("error_class IN ('permanent', 'internal')", name="error_class_valid"),
    )
    op.create_index("ix_quarantine_page", "quarantine", ["org_id", "first_seen_at", "id"])
    op.create_index(
        "ix_quarantine_source_record",
        "quarantine",
        ["org_id", "source", "record_type", "source_record_id"],
    )
    op.create_index(
        "ix_quarantine_reason_codes", "quarantine", ["reason_codes"], postgresql_using="gin"
    )

    # Raw records and canonical records are append-only for the app: no UPDATE, no DELETE.
    protect_table("raw_records", "SELECT, INSERT")
    protect_table("records", "SELECT, INSERT")
    protect_table("transformation_log", "SELECT, INSERT")
    protect_table("quarantine", "SELECT, INSERT, UPDATE")


def downgrade() -> None:
    op.drop_table("quarantine")
    op.drop_table("transformation_log")
    op.drop_table("records")
    op.drop_table("raw_records")
