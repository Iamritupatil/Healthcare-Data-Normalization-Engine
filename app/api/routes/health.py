"""Liveness and readiness. No API key needed."""

from fastapi import APIRouter
from sqlalchemy import text

from app.db import system_transaction

router = APIRouter(tags=["health"])


@router.get("/health", summary="The process is up")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", summary="The process is up and the database answers")
def ready() -> dict[str, str]:
    with system_transaction() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ready"}
