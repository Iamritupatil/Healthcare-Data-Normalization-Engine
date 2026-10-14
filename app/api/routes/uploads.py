"""File uploads become jobs; a runner processes them in the background."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, Response, UploadFile

from app.api.deps import Ingester, Registry, Runner, rate_limit
from app.api.errors import BadRequest, PayloadTooLarge, UnsupportedMediaType
from app.api.responses import ExactJSONResponse
from app.api.serializers import job_body
from app.config import get_settings
from app.db import org_transaction
from app.models.tables import IngestJob
from app.pipeline.mapping import RecordType, UnknownSourceError
from app.services.audit import audit
from app.services.uploads import (
    UploadError,
    check_header,
    parse_upload,
    safe_filename,
    upload_format,
)

router = APIRouter(prefix="/v1", tags=["uploads"])

_UPLOAD_ERRORS = {413: PayloadTooLarge, 415: UnsupportedMediaType}


@router.post(
    "/uploads",
    status_code=202,
    summary="Upload a CSV file or a JSON array of records; returns a job to poll",
    description=(
        "CSV: UTF-8 with a header row; every value is read as text. JSON: an array of "
        "objects, each one a payload as POST /v1/records takes it (numbers keep their "
        "precision). A name ending in .json, or the type application/json, means JSON. "
        "The whole file is checked before the job is created: 400 if it cannot be read "
        "or stored, 413 over the size or row limit, 415 for other file types."
    ),
    dependencies=[rate_limit("uploads", lambda s: s.rate_limit_uploads_per_hour, 3600)],
)
def create_upload(
    principal: Ingester,
    registry: Registry,
    runner: Runner,
    file: Annotated[UploadFile, File(description="UTF-8 CSV with a header row, or JSON")],
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
        file_format = upload_format(file.filename, file.content_type)
        parsed = parse_upload(content, file_format, max_rows=settings.upload_max_rows)
        check_header(parsed, mapping, record_type)
    except UploadError as exc:
        raise _UPLOAD_ERRORS.get(exc.status, BadRequest)(exc.detail) from None

    filename = safe_filename(file.filename)
    with org_transaction(principal.org_id) as session:
        job = IngestJob(
            id=uuid4(),
            org_id=principal.org_id,
            kind="upload",
            source=source,
            record_type=record_type,
            filename=filename,
            content=content,
            # The worker parses the file again, so it must know the format chosen here.
            params={"format": file_format},
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
            format=file_format,
        )
        body = job_body(job)
    runner.submit(job.id, principal.org_id)
    return ExactJSONResponse(body, status_code=202, headers={"Location": f"/v1/jobs/{job.id}"})
