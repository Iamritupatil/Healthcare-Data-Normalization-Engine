"""Orgs, API keys, and the org-isolation building blocks.

Revision ID: 0001
Revises:
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from migrations.rls import SYSTEM_ROLE, hand_function_to_system_role, protect_table

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTHENTICATE = "medvora_find_api_key(text)"


def upgrade() -> None:
    # NULLIF + missing_ok: an unset org reads as NULL, so every policy matches no rows,
    # whether the pooled connection has seen the setting before or not.
    op.execute(
        """
        CREATE FUNCTION app_current_org() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT NULLIF(current_setting('app.org_id', true), '')::uuid $$
        """
    )

    op.create_table(
        "orgs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False, unique=True),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('admin', 'ingest', 'reader')", name="role_valid"),
    )
    op.create_index("ix_api_keys_org_id", "api_keys", ["org_id"])

    protect_table("orgs", "SELECT", org_column="id")
    protect_table("api_keys", "SELECT")
    op.execute(f"CREATE POLICY system_lookup ON api_keys FOR SELECT TO {SYSTEM_ROLE} USING (true)")
    op.execute(f"GRANT SELECT ON api_keys TO {SYSTEM_ROLE}")

    # The org is unknown until the key is checked, so the lookup must see every org's keys.
    # It finds one active key by its public prefix; the app then verifies the secret part
    # against key_hash in constant time.
    op.execute(
        """
        CREATE FUNCTION medvora_find_api_key(p_key_prefix text)
        RETURNS TABLE (api_key_id uuid, org_id uuid, role text, key_hash text)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
            SELECT k.id, k.org_id, k.role, k.key_hash
              FROM public.api_keys AS k
             WHERE k.key_prefix = p_key_prefix
               AND k.revoked_at IS NULL
        $$
        """
    )
    hand_function_to_system_role(AUTHENTICATE)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {AUTHENTICATE}")
    op.drop_table("api_keys")
    op.drop_table("orgs")
    op.execute("DROP FUNCTION app_current_org()")
