"""Stage 1 helpers: the content hash that makes ingestion idempotent."""

import hashlib
from collections.abc import Mapping
from typing import Any

from app import json_codec


def content_hash(source: str, record_type: str, payload: Mapping[str, Any]) -> str:
    """Hash a canonical form, so key order and whitespace in the request never change it."""
    canonical = json_codec.dumps(
        {"source": source, "type": record_type, "payload": payload}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def source_record_id(payload: Mapping[str, Any], key_fields: list[str]) -> str | None:
    """The record's id in the sending system, used to recognise new versions of a record."""
    parts: list[str] = []
    for name in key_fields:
        value = payload.get(name)
        if is_blank(value) or isinstance(value, dict | list | bool):
            return None
        parts.append(str(value).strip())
    return "|".join(parts)
