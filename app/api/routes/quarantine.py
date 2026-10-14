"""The quarantine: records that failed, why, and re-running them after a fix."""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from app.api.deps import Admin, Reader, Runner, filter_query, plain_text_field
from app.api.errors import NotFound
from app.api.pagination import PageParams, page_body, page_params, paginate
from app.api.responses import ExactJSONResponse
from app.api.serializers import job_body, quarantine_body
from app.db import org_transaction
from app.models.tables import IngestJob, QuarantineItem
from app.services.audit import audit

router = APIRouter(prefix="/v1", tags=["quarantine"])

QuarantineStatus = Literal["open", "resolved", "superseded"]


@router.get("/quarantine", summary="Quarantined records, newest first")
def list_quarantine(
    principal: Reader,
    page: Annotated[PageParams, Depends(page_params)],
    source: Annotated[str | None, filter_query()] = None,
    reason_code: Annotated[str | None, filter_query(examples=["DATE_AMBIGUOUS"])] = None,
    status: Annotated[QuarantineStatus | None, Query()] = None,
    source_record_id: Annotated[str | None, filter_query()] = None,
) -> Response:
    query = select(QuarantineItem)
    if source:
        query = query.where(QuarantineItem.source == source)
    if reason_code:
        query = query.where(QuarantineItem.reason_codes.contains([reason_code]))
    if status:
        query = query.where(QuarantineItem.status == status)
    if source_record_id:
        query = query.where(QuarantineItem.source_record_id == source_record_id)
    with org_transaction(principal.org_id) as session:
        rows, next_cursor = paginate(
            session, query, page, at=QuarantineItem.first_seen_at, row_id=QuarantineItem.id
        )
        filters = {"source": source, "reason_code": reason_code, "status": status}
        audit(session, principal, "quarantine.list", "quarantine", returned=len(rows), **filters)
        items = [quarantine_body(row) for row in rows]
    return ExactJSONResponse(page_body(items, next_cursor, page.limit))


@router.get("/quarantine/{item_id}", summary="One quarantined record")
def get_quarantine_item(item_id: UUID, principal: Reader) -> Response:
    with org_transaction(principal.org_id) as session:
        item = session.get(QuarantineItem, item_id)
        if item is None:
            raise NotFound(f"no quarantine item {item_id}")
        audit(session, principal, "quarantine.read", "quarantine", item_id)
        body = quarantine_body(item)
    return ExactJSONResponse(body)


class ReprocessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[UUID] | None = Field(default=None, max_length=1000)
    source: str | None = plain_text_field()
    reason_code: str | None = plain_text_field()

    @model_validator(mode="after")
    def _needs_a_selector(self) -> "ReprocessIn":
        if not (self.ids or self.source or self.reason_code):
            raise ValueError("give ids, or a source and/or reason_code filter")
        return self


@router.post(
    "/quarantine/reprocess",
    status_code=202,
    summary="Re-run open quarantined records with the current mapping; returns a job",
)
def reprocess(request: ReprocessIn, principal: Admin, runner: Runner) -> Response:
    params = request.model_dump(mode="json", exclude_none=True)
    count_query = (
        select(func.count()).select_from(QuarantineItem).where(QuarantineItem.status == "open")
    )
    if request.ids:
        count_query = count_query.where(QuarantineItem.id.in_(request.ids))
    if request.source:
        count_query = count_query.where(QuarantineItem.source == request.source)
    if request.reason_code:
        count_query = count_query.where(QuarantineItem.reason_codes.contains([request.reason_code]))
    with org_transaction(principal.org_id) as session:
        total = session.scalar(count_query) or 0
        job = IngestJob(
            id=uuid4(),
            org_id=principal.org_id,
            kind="reprocess",
            params=params,
            status="pending",
            total_rows=total,
            created_by=principal.api_key_id,
        )
        session.add(job)
        session.flush()
        audit(session, principal, "quarantine.reprocess", "ingest_job", job.id, items=total)
        body = job_body(job)
    runner.submit(job.id, principal.org_id)
    return ExactJSONResponse(body, status_code=202, headers={"Location": f"/v1/jobs/{job.id}"})
