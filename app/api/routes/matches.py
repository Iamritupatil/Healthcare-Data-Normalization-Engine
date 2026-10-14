"""The human review queue for candidate matches."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import Admin, Reader
from app.api.errors import NotFound
from app.api.pagination import PageParams, page_body, page_params, paginate
from app.api.responses import ExactJSONResponse
from app.api.serializers import link_body
from app.db import org_transaction
from app.models.tables import MatchLink, Record
from app.services.audit import audit
from app.services.matching import record_human_decision

router = APIRouter(prefix="/v1", tags=["matches"])

LinkStatus = Literal["pending_verification", "review", "match", "different"]


@router.get(
    "/matches",
    summary="Candidate pairs by status, newest first (default: waiting for a human)",
)
def list_matches(
    principal: Reader,
    page: Annotated[PageParams, Depends(page_params)],
    status: Annotated[LinkStatus, Query()] = "review",
) -> Response:
    with org_transaction(principal.org_id) as session:
        rows, next_cursor = paginate(
            session,
            select(MatchLink).where(MatchLink.status == status),
            page,
            at=MatchLink.created_at,
            row_id=MatchLink.id,
        )
        record_ids = {row.record_id for row in rows} | {row.candidate_id for row in rows}
        sources = {
            record_id: {"source": source, "source_record_id": source_record_id}
            for record_id, source, source_record_id in session.execute(
                select(Record.id, Record.source, Record.source_record_id).where(
                    Record.id.in_(record_ids)
                )
            )
        }
        items = [
            link_body(row)
            | {"record": sources.get(row.record_id), "candidate": sources.get(row.candidate_id)}
            for row in rows
        ]
        audit(session, principal, "matches.list", "match_link", status=status, returned=len(items))
    return ExactJSONResponse(page_body(items, next_cursor, page.limit))


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["same", "different"]


@router.post(
    "/matches/{link_id}/decision",
    summary="Record a reviewer's decision: the same person, or different people",
    description=(
        "'same' links the records (status match) and records any conflicting fields. "
        "'different' keeps them apart; if they share a name and birth date, a "
        "POSSIBLE_NAMESAKE finding is added. Every decision is written to the audit log."
    ),
)
def decide_match(link_id: UUID, request: DecisionIn, principal: Admin) -> Response:
    with org_transaction(principal.org_id) as session:
        link = session.get(MatchLink, link_id, with_for_update=True)
        if link is None:
            raise NotFound(f"no candidate match {link_id}")
        previous = link.status
        record_human_decision(session, link, request.decision)
        audit(
            session,
            principal,
            "match.decision",
            "match_link",
            link_id,
            decision=request.decision,
            previous_status=previous,
            new_status=link.status,
        )
        session.flush()
        body = link_body(link)
    return ExactJSONResponse(body)
