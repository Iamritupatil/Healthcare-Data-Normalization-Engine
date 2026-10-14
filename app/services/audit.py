"""Audit trail. Written in the same transaction as the access it describes."""

from typing import Any

from sqlalchemy.orm import Session

from app.models.tables import AuditLog
from app.observability.request_id import get_request_id
from app.services.auth import Principal


def audit(
    session: Session,
    principal: Principal,
    action: str,
    resource_type: str,
    resource_id: object | None = None,
    **details: Any,
) -> None:
    """Record who did what. `details` must hold ids, filters and counts only, never PHI."""
    session.add(
        AuditLog(
            org_id=principal.org_id,
            api_key_id=principal.api_key_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            request_id=get_request_id(),
            details=details,
        )
    )
