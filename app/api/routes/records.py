"""Single-record ingest, dry-run validation, and reading records with their lineage."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import Ingester, JsonBody, Reader, Registry, Runner, rate_limit
from app.api.errors import BadRequest, NotFound, ValidationFailed
from app.api.pagination import PageParams, page_body, page_params, paginate
from app.api.responses import ExactJSONResponse
from app.api.serializers import finding_body, quarantine_body, record_body
from app.db import org_transaction
from app.models.tables import Finding, QuarantineItem, RawRecord, Record, TransformationLog
from app.pipeline.core import Transformation
from app.pipeline.mapping import MappingRegistry, RecordType, SourceMapping, UnknownSourceError
from app.pipeline.run import Normalized, run_pipeline
from app.services.audit import audit
from app.services.ingest import IngestOutcome, ingest_payload, local_today
from app.services.matching import enqueue_verification
from app.services.records import is_current_version

router = APIRouter(prefix="/v1", tags=["records"])

records_rate_limit = rate_limit("records", lambda s: s.rate_limit_records_per_minute, 60)


class RecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(examples=["source-a"])
    record_type: RecordType
    payload: dict[str, Any] = Field(
        description="The record as the source system sends it. Numbers keep their precision."
    )


_EXAMPLE = {
    "source": "source-a",
    "record_type": "Patient",
    "payload": {
        "mrn": "00123",
        "patient_name": "Robert Miller",
        "dob": "03/07/1984",
        "sex": "M",
        "phone": "(555) 201-3344",
        "zip": "10001",
    },
}
RECORD_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": RecordIn.model_json_schema(), "example": _EXAMPLE}
        },
    }
}


def parse_record_in(body: Any, registry: MappingRegistry) -> tuple[RecordIn, SourceMapping]:
    try:
        record_in = RecordIn.model_validate(body)
    except ValidationError as exc:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"]), "code": e["type"], "message": e["msg"]}
            for e in exc.errors(include_input=False, include_url=False)
        ]
        raise BadRequest("the request envelope is invalid", errors=errors) from None
    try:
        mapping = registry.active(record_in.source)
    except UnknownSourceError:
        raise BadRequest(
            f"unknown source {record_in.source!r}; known sources: {', '.join(registry.sources())}"
        ) from None
    if record_in.record_type not in mapping.record_types:
        raise BadRequest(f"{record_in.source} has no mapping for {record_in.record_type}")
    return record_in, mapping


def transformation_body(step: Transformation) -> dict[str, Any]:
    return {
        "field": step.field,
        "source_field": step.source_field,
        "rule": step.rule.name,
        "rule_version": step.rule.version,
        "original": step.original,
        "result": step.result,
        "details": step.details,
    }


def _outcome_body(outcome: IngestOutcome) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": outcome.status,
        "duplicate": outcome.duplicate,
        "raw_record_id": outcome.raw_record_id,
    }
    if outcome.record is not None:
        body["record"] = record_body(outcome.record)
    if outcome.duplicate_of is not None:
        body["duplicate_of"] = outcome.duplicate_of
    if outcome.warnings:
        body["warnings"] = outcome.warnings
    return body


@router.post(
    "/records",
    status_code=201,
    summary="Ingest one record",
    description=(
        "201: normalized and stored. 200: this exact payload was already received (the "
        "earlier result is returned), or it is a measurement already stored from another "
        "source (status duplicate_record). 422: kept but quarantined; the body says why."
    ),
    openapi_extra=RECORD_BODY,
    dependencies=[records_rate_limit],
)
def ingest_record(
    body: JsonBody, principal: Ingester, registry: Registry, runner: Runner
) -> Response:
    record_in, mapping = parse_record_in(body, registry)
    verify_job = None
    with org_transaction(principal.org_id) as session:
        outcome = ingest_payload(
            session,
            org_id=principal.org_id,
            mapping=mapping,
            record_type=record_in.record_type,
            payload=record_in.payload,
        )
        audit(
            session,
            principal,
            "record.ingest",
            "raw_record",
            outcome.raw_record_id,
            outcome=outcome.status,
            duplicate=outcome.duplicate,
        )
        if outcome.pending_link_ids:
            verify_job = enqueue_verification(
                session, principal.org_id, outcome.pending_link_ids, principal.api_key_id
            )
        response_body = _outcome_body(outcome)
        item = outcome.quarantine
        problem = None
        if item is not None:
            problem = ValidationFailed(
                f"{len(item.errors)} problem(s) found; the record was quarantined",
                quarantine_id=item.id,
                quarantine_status=item.status,
                raw_record_id=outcome.raw_record_id,
                duplicate=outcome.duplicate,
                reason_codes=item.reason_codes,
                errors=item.errors,
                warnings=outcome.warnings,
            )

    # Raised only after the transaction has committed, so the quarantine row is kept.
    if problem is not None:
        raise problem
    if verify_job is not None:
        runner.submit(verify_job, principal.org_id)
    record_id = outcome.record.id if outcome.record is not None else outcome.duplicate_of
    created = outcome.status == "normalized" and not outcome.duplicate
    return ExactJSONResponse(
        response_body,
        status_code=201 if created else 200,
        headers={"Location": f"/v1/records/{record_id}"},
    )


@router.post(
    "/validate",
    summary="Dry run: normalize and validate without storing anything",
    openapi_extra=RECORD_BODY,
    dependencies=[records_rate_limit],
)
def validate_record(body: JsonBody, principal: Ingester, registry: Registry) -> Response:
    record_in, mapping = parse_record_in(body, registry)
    result = run_pipeline(
        mapping, record_in.record_type, record_in.payload, today=local_today(mapping.timezone)
    )
    response_body: dict[str, Any] = {
        "valid": isinstance(result, Normalized),
        "mapping_version": result.mapping_version,
        "warnings": [warning.as_dict() for warning in result.warnings],
    }
    if isinstance(result, Normalized):
        response_body["source_record_id"] = result.source_record_id
        response_body["resource"] = result.resource
        response_body["transformations"] = [transformation_body(t) for t in result.transformations]
    else:
        response_body["reason_codes"] = result.reason_codes
        response_body["errors"] = [error.as_dict() for error in result.errors]
    return ExactJSONResponse(response_body)


def _get_record(session: Session, record_id: UUID) -> Record:
    # Another org's record is invisible under row-level security, so it is a 404, not a 403.
    record = session.get(Record, record_id)
    if record is None:
        raise NotFound(f"no record {record_id}")
    return record


def _is_current(session: Session, record_id: UUID) -> bool:
    current = session.scalar(select(Record.id).where(Record.id == record_id, is_current_version()))
    return current is not None


@router.get("/records/{record_id}", summary="A canonical record")
def get_record(record_id: UUID, principal: Reader) -> Response:
    with org_transaction(principal.org_id) as session:
        record = _get_record(session, record_id)
        body = record_body(record, is_current=_is_current(session, record_id))
        audit(session, principal, "record.read", "record", record_id)
    return ExactJSONResponse(body)


@router.get(
    "/records/{record_id}/lineage",
    summary="The raw payload, every transformation applied, and the mapping version used",
)
def get_lineage(record_id: UUID, principal: Reader, registry: Registry) -> Response:
    with org_transaction(principal.org_id) as session:
        record = _get_record(session, record_id)
        raw = session.get_one(RawRecord, record.raw_record_id)
        steps = session.scalars(
            select(TransformationLog)
            .where(TransformationLog.record_id == record_id)
            .order_by(TransformationLog.position)
        ).all()
        warnings = session.scalars(
            select(Finding).where(Finding.record_id == record_id, Finding.kind == "warning")
        ).all()
        quarantine = session.scalars(
            select(QuarantineItem).where(QuarantineItem.raw_record_id == record.raw_record_id)
        ).one_or_none()
        mapping = registry.get(record.source, record.mapping_version)
        body = {
            "record": record_body(record, is_current=_is_current(session, record_id)),
            "raw": {
                "id": raw.id,
                "content_hash": raw.content_hash,
                "received_at": raw.received_at,
                "job_id": raw.job_id,
                "row_number": raw.row_number,
                "payload": raw.payload,
            },
            "mapping": {
                "source": mapping.source,
                "version": mapping.version,
                "description": mapping.description.strip(),
                "timezone": mapping.timezone,
            },
            "transformations": [
                {
                    "field": step.field,
                    "source_field": step.source_field,
                    "rule": step.rule,
                    "rule_version": step.rule_version,
                    "original": step.original,
                    "result": step.result,
                    "details": step.details,
                }
                for step in steps
            ],
            "warnings": [finding_body(finding) for finding in warnings],
            "quarantine_history": quarantine_body(quarantine) if quarantine else None,
        }
        audit(session, principal, "record.lineage", "record", record_id)
    return ExactJSONResponse(body)


@router.get(
    "/records/{record_id}/versions",
    summary="All normalized versions of the same source record, newest first",
)
def get_versions(
    record_id: UUID, principal: Reader, page: Annotated[PageParams, Depends(page_params)]
) -> Response:
    with org_transaction(principal.org_id) as session:
        record = _get_record(session, record_id)
        same = (
            Record.source == record.source,
            Record.record_type == record.record_type,
            Record.source_record_id == record.source_record_id,
        )
        ordered = session.execute(
            select(
                Record.id, func.row_number().over(order_by=(Record.received_at, Record.id))
            ).where(*same)
        )
        numbers = {version_id: number for version_id, number in ordered}
        latest = max(numbers.values())
        rows, next_cursor = paginate(
            session, select(Record).where(*same), page, at=Record.received_at, row_id=Record.id
        )
        items = [
            record_body(row, is_current=numbers[row.id] == latest) | {"version": numbers[row.id]}
            for row in rows
        ]
        audit(session, principal, "record.versions", "record", record_id, returned=len(items))
    return ExactJSONResponse(page_body(items, next_cursor, page.limit))
