"""Query helpers for record versions."""

from typing import Any

from sqlalchemy import and_, exists, tuple_
from sqlalchemy.orm import aliased

from app.models.tables import QuarantineItem, RawRecord, Record

SourceRecordModel = type[Record] | type[RawRecord] | type[QuarantineItem]


def same_source_record(
    model: SourceRecordModel, source: str, record_type: str, source_record_id: str | None
) -> Any:
    return and_(
        model.source == source,
        model.record_type == record_type,
        model.source_record_id == source_record_id,
    )


def is_current_version() -> Any:
    """SQL condition: this Record row is the latest received version of its source record."""
    newer = aliased(Record)
    return ~exists().where(
        newer.source == Record.source,
        newer.record_type == Record.record_type,
        newer.source_record_id == Record.source_record_id,
        tuple_(newer.received_at, newer.id) > tuple_(Record.received_at, Record.id),
    )
