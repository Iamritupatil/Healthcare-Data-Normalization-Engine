"""Request ids: one id per request, on every log line, in every error body and response header."""

import re
from contextvars import ContextVar, Token
from uuid import uuid4

HEADER = "X-Request-ID"
_VALID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_current: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    return f"req_{uuid4().hex[:20]}"


def accept_or_create(incoming: str | None) -> str:
    """Reuse a caller's id only if it is short and plain, so it can't inject into logs."""
    if incoming and _VALID.fullmatch(incoming):
        return incoming
    return new_request_id()


def get_request_id() -> str | None:
    return _current.get()


def set_request_id(value: str | None) -> Token[str | None]:
    return _current.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    _current.reset(token)
