"""Cursor (keyset) pagination, newest first.

Page numbers shift when rows arrive while someone is paging; a cursor that says "after
this (timestamp, id)" does not. Cursors are opaque to clients. Tampering with one can at
worst page through your own org's rows, because row-level security still applies.
"""

import base64
import binascii
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Query
from sqlalchemy import Select, tuple_
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.api.errors import BadRequest

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class PageParams:
    limit: int
    cursor: str | None


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> PageParams:
    return PageParams(limit, cursor)


def encode_cursor(at: datetime, row_id: UUID) -> str:
    raw = json.dumps({"t": at.isoformat(), "i": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(data["t"]), UUID(data["i"])
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise BadRequest("the cursor is not valid; use next_cursor from a previous page") from None


def paginate[T](
    session: Session,
    query: Select[T],
    params: PageParams,
    *,
    at: InstrumentedAttribute[datetime],
    row_id: InstrumentedAttribute[UUID],
) -> tuple[Sequence[T], str | None]:
    if params.cursor:
        cursor_at, cursor_id = decode_cursor(params.cursor)
        query = query.where(tuple_(at, row_id) < tuple_(cursor_at, cursor_id))
    rows = session.scalars(query.order_by(at.desc(), row_id.desc()).limit(params.limit + 1)).all()
    page = rows[: params.limit]
    next_cursor = None
    if len(rows) > params.limit:
        last: Any = page[-1]
        next_cursor = encode_cursor(getattr(last, at.key), getattr(last, row_id.key))
    return page, next_cursor


def page_body(items: list[dict[str, Any]], next_cursor: str | None, limit: int) -> dict[str, Any]:
    return {"items": items, "next_cursor": next_cursor, "limit": limit}
