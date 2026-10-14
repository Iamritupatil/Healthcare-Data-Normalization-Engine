"""Findings: conflicts between matched records, and warnings raised during normalization."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import or_, select

from app.api.deps import Reader
from app.api.pagination import PageParams, page_body, page_params, paginate
from app.api.responses import ExactJSONResponse
from app.api.serializers import finding_body
from app.db import org_transaction
from app.models.tables import Finding
from app.services.audit import audit

router = APIRouter(prefix="/v1", tags=["findings"])


@router.get("/findings", summary="Conflicts and warnings, newest first")
def list_findings(
    principal: Reader,
    page: Annotated[PageParams, Depends(page_params)],
    kind: Annotated[Literal["conflict", "warning"] | None, Query()] = None,
    code: Annotated[str | None, Query(examples=["CONFLICT_BIRTHDATE"])] = None,
    record_id: Annotated[UUID | None, Query()] = None,
) -> Response:
    query = select(Finding)
    if kind:
        query = query.where(Finding.kind == kind)
    if code:
        query = query.where(Finding.code == code)
    if record_id:
        query = query.where(
            or_(Finding.record_id == record_id, Finding.other_record_id == record_id)
        )
    with org_transaction(principal.org_id) as session:
        rows, next_cursor = paginate(session, query, page, at=Finding.created_at, row_id=Finding.id)
        audit(
            session, principal, "findings.list", "finding", returned=len(rows), kind=kind, code=code
        )
        items = [finding_body(row) for row in rows]
    return ExactJSONResponse(page_body(items, next_cursor, page.limit))
