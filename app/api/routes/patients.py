"""A patient's match group: every record linked to it by 'match' links, directly or not."""

from uuid import UUID

from fastapi import APIRouter, Response
from sqlalchemy import or_, select

from app.api.deps import Reader
from app.api.errors import NotFound
from app.api.responses import ExactJSONResponse
from app.api.serializers import finding_body, link_body, record_body
from app.db import org_transaction
from app.models.tables import Finding, MatchLink, Record
from app.services.audit import audit
from app.services.matching import match_group_ids
from app.services.records import is_current_version

router = APIRouter(prefix="/v1", tags=["patients"])


@router.get(
    "/patients/{record_id}/matches",
    summary="Match group, candidate links and open conflicts for a patient record",
    description=(
        "members: every record linked to this one by 'match' links, directly or not. "
        "links: every candidate pair touching the group, in any status, with its score, "
        "comparison facts and verdict."
    ),
)
def get_matches(record_id: UUID, principal: Reader) -> Response:
    with org_transaction(principal.org_id) as session:
        record = session.get(Record, record_id)
        if record is None or record.record_type != "Patient":
            raise NotFound(f"no patient record {record_id}")
        group_ids = match_group_ids(session, record_id)
        members = session.scalars(select(Record).where(Record.id.in_(group_ids))).all()
        current = set(
            session.scalars(select(Record.id).where(Record.id.in_(group_ids), is_current_version()))
        )
        touches_group = or_(
            MatchLink.record_id.in_(group_ids), MatchLink.candidate_id.in_(group_ids)
        )
        links = session.scalars(select(MatchLink).where(touches_group)).all()
        conflicts = session.scalars(
            select(Finding).where(
                Finding.kind == "conflict",
                Finding.status == "open",
                or_(Finding.record_id.in_(group_ids), Finding.other_record_id.in_(group_ids)),
            )
        ).all()
        body = {
            "record_id": record_id,
            "members": [record_body(m, is_current=m.id in current) for m in members],
            "links": [link_body(link) for link in links],
            "conflicts": [finding_body(finding) for finding in conflicts],
        }
        audit(session, principal, "patient.matches", "record", record_id, members=len(members))
    return ExactJSONResponse(body)
