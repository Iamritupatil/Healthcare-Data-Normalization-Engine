"""Match links between patient records, and findings (conflicts and warnings).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from migrations.rls import protect_table

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "match_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("record_id", sa.Uuid(), sa.ForeignKey("records.id"), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), sa.ForeignKey("records.id"), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("breakdown", postgresql.JSONB(), nullable=False),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("verifier", sa.Text(), nullable=True),
        sa.Column("verdict", postgresql.JSONB(), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("record_id", "candidate_id", name="uq_match_links_pair"),
        sa.CheckConstraint(
            "status IN ('pending_verification', 'review', 'match', 'different')",
            name="status_valid",
        ),
        sa.CheckConstraint("decided_by IN ('verifier', 'human')", name="decided_by_valid"),
    )
    op.create_index("ix_match_links_candidate", "match_links", ["candidate_id"])
    op.create_index("ix_match_links_page", "match_links", ["org_id", "status", "created_at", "id"])

    op.create_table(
        "findings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Uuid(), sa.ForeignKey("records.id"), nullable=False),
        sa.Column("other_record_id", sa.Uuid(), sa.ForeignKey("records.id"), nullable=True),
        sa.Column("raw_record_id", sa.Uuid(), sa.ForeignKey("raw_records.id"), nullable=True),
        sa.Column("field", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('conflict', 'warning')", name="kind_valid"),
    )
    op.create_index("ix_findings_page", "findings", ["org_id", "created_at", "id"])
    op.create_index("ix_findings_record", "findings", ["record_id"])
    op.create_index("ix_findings_other_record", "findings", ["other_record_id"])
    op.create_index("ix_findings_raw_record", "findings", ["raw_record_id"])

    # Links move through statuses (verifier verdicts, human decisions), so they can be updated.
    protect_table("match_links", "SELECT, INSERT, UPDATE")
    protect_table("findings", "SELECT, INSERT")


def downgrade() -> None:
    op.drop_table("findings")
    op.drop_table("match_links")
