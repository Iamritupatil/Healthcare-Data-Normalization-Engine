"""Helpers shared by migrations: row-level security, grants, and definer-function ownership.

Roles (created by deploy/postgres/init-roles.sh):
    medvora_owner   owns the schema and runs migrations; still subject to RLS (FORCE)
    medvora_app     what the API and worker log in as; not the owner, no BYPASSRLS
    medvora_system  cannot log in; owns the two SECURITY DEFINER functions that must see
                    across orgs (API key lookup, job claiming) and nothing else
"""

from alembic import op

APP_ROLE = "medvora_app"
SYSTEM_ROLE = "medvora_system"


def protect_table(table: str, grants: str, *, org_column: str = "org_id") -> None:
    """Enable and force row-level security, then grant the app role only what it needs."""
    condition = f"{org_column} = app_current_org()"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY org_isolation ON {table} USING ({condition}) WITH CHECK ({condition})"
    )
    op.execute(f"GRANT {grants} ON {table} TO {APP_ROLE}")


def hand_function_to_system_role(signature: str) -> None:
    """Make a SECURITY DEFINER function run as the narrow system role, callable by the app only.

    Changing a function's owner requires the new owner to hold CREATE on the schema, so it
    is granted for the duration of the ALTER and taken away again.
    """
    op.execute(f"GRANT CREATE ON SCHEMA public TO {SYSTEM_ROLE}")
    op.execute(f"ALTER FUNCTION {signature} OWNER TO {SYSTEM_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {SYSTEM_ROLE}")
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {APP_ROLE}")
