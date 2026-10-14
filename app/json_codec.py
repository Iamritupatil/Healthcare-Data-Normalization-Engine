"""JSON encoding that never passes clinical numbers through `float`.

The standard library parses `70.10` as the float `70.1`, losing the precision the sender
used. We parse numbers with a fraction as `Decimal` and write `Decimal` back out as an
exact JSON number. The same encoder produces the canonical form used for content hashes.

`check_storable` is the gate in front of the database: valid JSON can still hold values
Postgres JSONB refuses (a NUL character, half of a UTF-16 surrogate pair, a number with
200,000 digits). Those are rejected at the edge as a 400 instead of failing in the
database as a 500, or failing a whole upload job in the worker.
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


# Records are flat; a few levels of nesting is plenty. Far below Python's recursion limit.
MAX_DEPTH = 32
# Postgres numeric allows far more; no clinical value needs more than this either side
# of the decimal point.
MAX_NUMBER_DIGITS = 1000


class UnstorableValue(ValueError):
    """A JSON value Postgres cannot store as it is. The message never contains the value."""


def loads(data: str | bytes) -> Any:
    """Parse JSON exactly. Nesting deep enough to exhaust the stack raises ValueError too."""
    try:
        return json.loads(data, parse_float=Decimal, parse_constant=_reject_constant)
    except RecursionError:
        raise UnstorableValue(f"nested deeper than {MAX_DEPTH} levels") from None


def check_storable(value: Any, *, max_depth: int = MAX_DEPTH) -> None:
    """Raise UnstorableValue unless Postgres JSONB can store `value` exactly as it is.

    Walks with an explicit stack rather than recursion, so depth cannot exhaust the stack.
    """
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, str):
            _check_text(item)
        elif isinstance(item, bool) or item is None:
            continue
        elif isinstance(item, int | Decimal):
            _check_number(Decimal(item))
        elif isinstance(item, Mapping | list | tuple):
            if depth >= max_depth:
                raise UnstorableValue(f"nested deeper than {max_depth} levels")
            if isinstance(item, Mapping):
                for key, child in item.items():
                    _check_text(key)
                    stack.append((child, depth + 1))
            else:
                stack.extend((child, depth + 1) for child in item)


def _check_text(text: str) -> None:
    if "\x00" in text:
        raise UnstorableValue("text contains a NUL character")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise UnstorableValue("text is not valid Unicode (an unpaired surrogate)") from None


def _check_number(number: Decimal) -> None:
    if not number.is_finite():
        raise UnstorableValue("numbers must be finite")
    exponent = number.as_tuple().exponent
    assert isinstance(exponent, int)  # finite numbers have an integer exponent
    if number.adjusted() >= MAX_NUMBER_DIGITS or -exponent > MAX_NUMBER_DIGITS:
        raise UnstorableValue(
            f"numbers may have at most {MAX_NUMBER_DIGITS} digits before or after the point"
        )


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
