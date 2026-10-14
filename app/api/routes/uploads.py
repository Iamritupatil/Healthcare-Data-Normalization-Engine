"""CSV uploads become jobs; a runner processes them in the background."""

import os
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, Response, UploadFile

from app.api.deps import Ingester, Registry, Runner, rate_limit
from app.api.errors import BadRequest, PayloadTooLarge
from app.api.responses import ExactJSONResponse
from app.api.serializers import job_body
from app.config import get_settings
from app.db import org_transaction
from app.models.tables import IngestJob
from app.pipeline.mapping import RecordType, UnknownSourceError
from app.services.audit import audit
from app.services.uploads import UploadError, check_header, parse_csv

router = APIRouter(prefix="/v1", tags=["uploads"])


@router.post(
    "/uploads",
    status_code=202,
    summary="Upload a CSV file; returns a job to poll",
    dependencies=[rate_limit("uploads", lambda s: s.rate_limit_uploads_per_hour, 3600)],
)
def create_upload(
    principal: Ingester,
    registry: Registry,
    runner: Runner,
    file: Annotated[UploadFile, File(description="UTF-8 CSV with a header row")],
    source: Annotated[str, Form(examples=["source-a"])],
    record_type: Annotated[RecordType, Form()],
) -> Response:
    settings = get_settings()
    try:
        mapping = registry.active(source)
    except UnknownSourceError:
        raise BadRequest(f"unknown source {source!r}") from None
    if record_type not in mapping.record_types:
        raise BadRequest(f"{source} has no mapping for {record_type}")

    content = file.file.read(settings.upload_max_bytes + 1)
    if len(content) > settings.upload_max_bytes:
        raise PayloadTooLarge(f"the file limit is {settings.upload_max_bytes} bytes")
    try:
        parsed = parse_csv(content, max_rows=settings.upload_max_rows)
        check_header(parsed, mapping, record_type)
    except UploadError as exc:
        error = PayloadTooLarge if exc.status == 413 else BadRequest
        raise error(exc.detail) from None

    filename = os.path.basename(file.filename or "upload.csv")[:255]
    with org_transaction(principal.org_id) as session:
        job = IngestJob(
            id=uuid4(),
            org_id=principal.org_id,
            kind="upload",
            source=source,
            record_type=record_type,
            filename=filename,
            content=content,
            status="pending",
            total_rows=len(parsed.rows),
            created_by=principal.api_key_id,
        )
        session.add(job)
        session.flush()
        audit(
            session,
            principal,
            "upload.create",
            "ingest_job",
            job.id,
            rows=len(parsed.rows),
            source=source,
        )
        body = job_body(job)
    runner.submit(job.id, principal.org_id)
    return ExactJSONResponse(body, status_code=202, headers={"Location": f"/v1/jobs/{job.id}"})
