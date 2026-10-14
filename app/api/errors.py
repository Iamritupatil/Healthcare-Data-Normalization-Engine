"""Every error response uses RFC 9457 Problem Details (application/problem+json).

Shape: type, title, status, detail, instance, plus request_id and any extension members
(for example quarantine_id and errors for a quarantined record).
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import exc as sa_exc
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.responses import ProblemJSONResponse
from app.config import get_settings
from app.observability.logging import exception_summary, get_logger
from app.observability.request_id import get_request_id

log = get_logger(__name__)


class ProblemError(Exception):
    status: int = 500
    slug: str = "internal-error"
    title: str = "Internal server error"

    def __init__(
        self, detail: str | None = None, *, headers: dict[str, str] | None = None, **extensions: Any
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.headers = headers
        self.extensions = extensions


class BadRequest(ProblemError):
    status, slug, title = 400, "bad-request", "Bad request"


class Unauthorized(ProblemError):
    status, slug, title = 401, "unauthorized", "Missing or invalid API key"


class Forbidden(ProblemError):
    status, slug, title = 403, "forbidden", "This API key's role does not allow this"


class NotFound(ProblemError):
    status, slug, title = 404, "not-found", "Not found"


class PayloadTooLarge(ProblemError):
    status, slug, title = 413, "payload-too-large", "Request is too large"


class ValidationFailed(ProblemError):
    status, slug, title = 422, "validation-failed", "Record failed validation"


class RateLimited(ProblemError):
    status, slug, title = 429, "rate-limited", "Too many requests"


class ServiceUnavailable(ProblemError):
    status, slug, title = 503, "database-unavailable", "The database is unavailable"


def problem_body(
    status: int, slug: str, title: str, detail: str | None, instance: str, **extensions: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": f"{get_settings().problem_type_base}{slug}",
        "title": title,
        "status": status,
    }
    if detail:
        body["detail"] = detail
    body["instance"] = instance
    body["request_id"] = get_request_id()
    body.update(extensions)
    return body


def problem_response(
    request: Request,
    status: int,
    slug: str,
    title: str,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
    **extensions: Any,
) -> ProblemJSONResponse:
    body = problem_body(status, slug, title, detail, request.url.path, **extensions)
    return ProblemJSONResponse(body, status_code=status, headers=headers)


_HTTP_PROBLEMS = {
    404: ("not-found", "Not found"),
    405: ("method-not-allowed", "Method not allowed"),
    413: ("payload-too-large", "Request is too large"),
    415: ("unsupported-media-type", "Unsupported media type"),
}


async def _problem_error(request: Request, exc: Exception) -> ProblemJSONResponse:
    assert isinstance(exc, ProblemError)
    return problem_response(
        request, exc.status, exc.slug, exc.title, exc.detail, exc.headers, **exc.extensions
    )


async def _request_validation_error(request: Request, exc: Exception) -> ProblemJSONResponse:
    assert isinstance(exc, RequestValidationError)
    # The validation error's own input values are left out: they may be patient data.
    errors = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "code": error["type"],
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    return problem_response(
        request,
        400,
        "invalid-request",
        "The request is malformed",
        f"{len(errors)} problem(s) with the request",
        errors=errors,
    )


async def _http_exception(request: Request, exc: Exception) -> ProblemJSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    slug, title = _HTTP_PROBLEMS.get(exc.status_code, ("http-error", "Request failed"))
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != title else None
    headers = dict(exc.headers) if exc.headers else None
    return problem_response(request, exc.status_code, slug, title, detail, headers)


async def _database_unavailable(request: Request, exc: Exception) -> ProblemJSONResponse:
    log.warning("database_unavailable", **exception_summary(exc))
    return problem_response(
        request,
        503,
        ServiceUnavailable.slug,
        ServiceUnavailable.title,
        "Try again shortly.",
        {"Retry-After": "5"},
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _problem_error)
    app.add_exception_handler(RequestValidationError, _request_validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(sa_exc.OperationalError, _database_unavailable)
    app.add_exception_handler(sa_exc.InterfaceError, _database_unavailable)
    app.add_exception_handler(sa_exc.TimeoutError, _database_unavailable)
