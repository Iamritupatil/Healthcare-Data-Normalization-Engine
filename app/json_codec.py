"""JSON encoding that never passes clinical numbers through `float`.

The standard library parses `70.10` as the float `70.1`, losing the precision the sender
used. We parse numbers with a fraction as `Decimal` and write `Decimal` back out as an
exact JSON number. The same encoder produces the canonical form used for content hashes.
"""

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def loads(data: str | bytes) -> Any:
    return json.loads(data, parse_float=Decimal, parse_constant=_reject_constant)


def dumps(obj: Any, *, sort_keys: bool = False) -> str:
    parts: list[str] = []
    _encode(obj, parts, sort_keys)
    return "".join(parts)


def format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not serialized; attach a time zone first")
    utc = value.astimezone(UTC)
    return utc.isoformat().replace("+00:00", "Z")


def _encode(obj: Any, out: list[str], sort_keys: bool) -> None:
    if obj is None:
        out.append("null")
    elif obj is True:
        out.append("true")
    elif obj is False:
        out.append("false")
    elif isinstance(obj, str):
        out.append(json.dumps(obj, ensure_ascii=False))
    elif isinstance(obj, int):
        out.append(str(int(obj)))
    elif isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError("NaN and infinite decimals are not valid JSON")
        out.append(str(obj))
    elif isinstance(obj, float):
        out.append(json.dumps(obj, allow_nan=False))
    elif isinstance(obj, datetime):
        out.append(f'"{format_datetime(obj)}"')
    elif isinstance(obj, date):
        out.append(f'"{obj.isoformat()}"')
    elif isinstance(obj, UUID):
        out.append(f'"{obj}"')
    elif isinstance(obj, Enum):
        _encode(obj.value, out, sort_keys)
    elif isinstance(obj, Mapping):
        items = sorted(obj.items()) if sort_keys else obj.items()
        out.append("{")
        for index, (key, value) in enumerate(items):
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be strings, got {type(key).__name__}")
            if index:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _encode(value, out, sort_keys)
        out.append("}")
    elif isinstance(obj, list | tuple):
        out.append("[")
        for index, value in enumerate(obj):
            if index:
                out.append(",")
            _encode(value, out, sort_keys)
        out.append("]")
    else:
        raise TypeError(f"{type(obj).__name__} is not JSON serializable")
