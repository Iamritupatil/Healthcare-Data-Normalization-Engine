"""Pipeline counts for the caller's org, read from the database so they survive restarts."""

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import Row, func, select

from app.api.deps import Admin
from app.api.responses import ExactJSONResponse
from app.db import org_transaction
from app.models.tables import Finding, IngestJob, MatchLink, QuarantineItem, RawRecord, Record
from app.services.matching import DUPLICATE_RECORD

router = APIRouter(tags=["metrics"])


def _counts(rows: Iterable[Row[str, int]]) -> dict[str, int]:
    return {key: count for key, count in rows}


@router.get(
    "/metrics",
    summary="Counts by source and status for your organisation",
    description=(
        "Per source, every received record is accounted for: ingested = normalized + "
        "duplicate_records + quarantine.open + quarantine.superseded. (A resolved "
        "quarantine item became a normalized record, so it is counted there.)"
    ),
)
def get_metrics(principal: Admin) -> Response:
    by_source: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "ingested": 0,
            "normalized": 0,
            "duplicate_records": 0,
            "quarantine": {"open": 0, "resolved": 0, "superseded": 0},
        }
    )
    with org_transaction(principal.org_id) as session:
        for source, count in session.execute(
            select(RawRecord.source, func.count()).group_by(RawRecord.source)
        ):
            by_source[source]["ingested"] = count
        for source, count in session.execute(
            select(Record.source, func.count()).group_by(Record.source)
        ):
            by_source[source]["normalized"] = count
        for source, status, count in session.execute(
            select(QuarantineItem.source, QuarantineItem.status, func.count()).group_by(
                QuarantineItem.source, QuarantineItem.status
            )
        ):
            by_source[source]["quarantine"][status] = count
        for source, count in session.execute(
            select(RawRecord.source, func.count())
            .join(Finding, Finding.raw_record_id == RawRecord.id)
            .where(Finding.code == DUPLICATE_RECORD)
            .group_by(RawRecord.source)
        ):
            by_source[source]["duplicate_records"] = count
        links = _counts(
            session.execute(select(MatchLink.status, func.count()).group_by(MatchLink.status))
        )
        findings = _counts(
            session.execute(
                select(Finding.kind, func.count())
                .where(Finding.status == "open")
                .group_by(Finding.kind)
            )
        )
        jobs = _counts(
            session.execute(select(IngestJob.status, func.count()).group_by(IngestJob.status))
        )
    body: dict[str, Any] = {
        "by_source": dict(sorted(by_source.items())),
        "match_links": {
            status: links.get(status, 0)
            for status in ("pending_verification", "review", "match", "different")
        },
        "open_findings": {
            "conflict": findings.get("conflict", 0),
            "warning": findings.get("warning", 0),
        },
        "jobs": jobs,
    }
    return ExactJSONResponse(body)
