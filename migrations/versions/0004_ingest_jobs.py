"""Upload, reprocess and match-verification jobs, and the function workers use to claim them.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from migrations.rls import SYSTEM_ROLE, hand_function_to_system_role, protect_table

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLAIM = "medvora_claim_job(integer, integer)"


def upgrade() -> None:
    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("record_type", sa.Text(), nullable=True),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column(
            "params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("processed_rows", sa.Integer(), nullable=False),
        sa.Column("normalized_rows", sa.Integer(), nullable=False),
        sa.Column("failed_rows", sa.Integer(), nullable=False),
        sa.Column("duplicate_rows", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("api_keys.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('upload', 'reprocess', 'verify')", name="kind_valid"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'completed_with_errors', 'failed')",
            name="status_valid",
        ),
    )
    op.create_index("ix_ingest_jobs_org_id", "ingest_jobs", ["org_id"])
    op.create_index(
        "ix_ingest_jobs_claimable",
        "ingest_jobs",
        ["created_at"],
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    op.add_column("raw_records", sa.Column("job_id", sa.Uuid(), nullable=True))
    op.add_column("raw_records", sa.Column("row_number", sa.Integer(), nullable=True))
    op.create_foreign_key(None, "raw_records", "ingest_jobs", ["job_id"], ["id"])

    protect_table("ingest_jobs", "SELECT, INSERT, UPDATE")
    op.execute(
        f"CREATE POLICY system_claim ON ingest_jobs TO {SYSTEM_ROLE} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, UPDATE ON ingest_jobs TO {SYSTEM_ROLE}")

    # Workers serve every org, so claiming must look across orgs. With FORCE ROW LEVEL
    # SECURITY even the table owner is filtered, which is why this runs as the system role
    # that has its own policy above. SKIP LOCKED lets several workers poll at once without
    # ever taking the same job. A job whose worker stopped sending heartbeats is taken over.
    op.execute(
        """
        CREATE FUNCTION medvora_claim_job(p_stale_after_seconds integer, p_max_attempts integer)
        RETURNS TABLE (job_id uuid, org_id uuid)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
        BEGIN
            UPDATE public.ingest_jobs AS j
               SET status = 'failed',
                   finished_at = now(),
                   last_error = coalesce(j.last_error, 'worker stopped responding too many times')
             WHERE j.status = 'running'
               AND j.heartbeat_at < now() - make_interval(secs => p_stale_after_seconds)
               AND j.attempts >= p_max_attempts;

            RETURN QUERY
            UPDATE public.ingest_jobs AS j
               SET status = 'running',
                   heartbeat_at = now(),
                   started_at = coalesce(j.started_at, now()),
                   attempts = j.attempts + 1
             WHERE j.id = (
                   SELECT c.id
                     FROM public.ingest_jobs AS c
                    WHERE c.status = 'pending'
                       OR (c.status = 'running'
                           AND c.heartbeat_at
                               < now() - make_interval(secs => p_stale_after_seconds))
                    ORDER BY c.created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1)
            RETURNING j.id, j.org_id;
        END;
        $$
        """
    )
    hand_function_to_system_role(CLAIM)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {CLAIM}")
    op.drop_constraint("raw_records_job_id_fkey", "raw_records", type_="foreignkey")
    op.drop_column("raw_records", "row_number")
    op.drop_column("raw_records", "job_id")
    op.drop_table("ingest_jobs")
