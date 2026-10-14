"""Database engine, sessions, and the per-transaction org setting that row-level security reads.

The org is set with `set_config('app.org_id', ..., true)`. The final `true` makes it
transaction-local (the function form of SET LOCAL): pooled connections are reused across
requests, and a plain SET would carry one org's id into the next request.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app import json_codec
from app.config import get_settings


def build_engine(url: str, *, pool_size: int = 5, max_overflow: int = 0) -> Engine:
    settings = get_settings()
    return create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
        json_serializer=json_codec.dumps,
        json_deserializer=json_codec.loads,
    )


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return build_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def set_org(session: Session, org_id: UUID) -> None:
    session.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org_id)})


@contextmanager
def org_transaction(org_id: UUID) -> Iterator[Session]:
    """One transaction scoped to one org. Commits on success, rolls back on any error."""
    with _session_factory()() as session, session.begin():
        set_org(session, org_id)
        yield session


@contextmanager
def system_transaction() -> Iterator[Session]:
    """A transaction with no org set: row-level security hides every org's rows.

    Used only to call the narrow SECURITY DEFINER functions (key lookup, job claiming).
    """
    with _session_factory()() as session, session.begin():
        yield session
