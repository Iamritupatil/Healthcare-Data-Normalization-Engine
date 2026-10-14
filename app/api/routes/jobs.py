"""Job status."""

from uuid import UUID

from fastapi import APIRouter, Response

from app.api.deps import JobWatcher
from app.api.errors import NotFound
from app.api.responses import ExactJSONResponse
from app.api.serializers import job_body
from app.db import org_transaction
from app.models.tables import IngestJob

router = APIRouter(prefix="/v1", tags=["jobs"])


@router.get(
    "/jobs/{job_id}",
    summary="Job progress",
    description=(
        "status is pending, running, completed, completed_with_errors or failed. "
        "duplicate_rows counts rows already received before; they are also counted in "
        "normalized_rows or failed_rows according to their earlier outcome."
    ),
)
def get_job(job_id: UUID, principal: JobWatcher) -> Response:
    with org_transaction(principal.org_id) as session:
        job = session.get(IngestJob, job_id)
        if job is None:
            raise NotFound(f"no job {job_id}")
        body = job_body(job)
    return ExactJSONResponse(body)
