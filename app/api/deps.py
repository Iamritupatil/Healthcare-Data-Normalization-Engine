"""Request dependencies: API key -> org and role, role checks, rate limits, request bodies."""

from collections.abc import Callable
from typing import Annotated, Any

import structlog
from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader

from app import json_codec
from app.api.errors import BadRequest, Forbidden, Unauthorized
from app.api.limits import RateLimiter
from app.config import Settings, get_settings
from app.db import system_transaction
from app.jobs.runner import JobRunner
from app.pipeline.mapping import MappingRegistry
from app.services.auth import Principal, authenticate

api_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="API key issued to your organisation"
)


def get_principal(
    request: Request, api_key: Annotated[str | None, Security(api_key_header)]
) -> Principal:
    if not api_key:
        raise Unauthorized("send your API key in the X-API-Key header")
    with system_transaction() as session:
        principal = authenticate(session, api_key, get_settings().api_key_pepper.get_secret_value())
    if principal is None:
        raise Unauthorized("the API key is not valid or has been revoked")
    ids = {"org_id": str(principal.org_id), "api_key_id": str(principal.api_key_id)}
    structlog.contextvars.bind_contextvars(**ids)
    # For the access log line, which is written outside this thread's context.
    request.state.log_ids = ids
    return principal


def require_role(*roles: str) -> Callable[[Principal], Principal]:
    def check(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if principal.role not in roles:
            raise Forbidden(f"requires one of the roles: {', '.join(roles)}")
        return principal

    return check


# Ingest keys belong to sending systems and cannot read patient data back. Anyone who can
# start a job may poll it.
Reader = Annotated[Principal, Depends(require_role("reader", "admin"))]
Ingester = Annotated[Principal, Depends(require_role("ingest", "admin"))]
JobWatcher = Annotated[Principal, Depends(require_role("ingest", "reader", "admin"))]
Admin = Annotated[Principal, Depends(require_role("admin"))]


def get_registry(request: Request) -> MappingRegistry:
    registry: MappingRegistry = request.app.state.registry
    return registry


def get_job_runner(request: Request) -> JobRunner:
    runner: JobRunner = request.app.state.job_runner
    return runner


def _rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


def rate_limit(bucket: str, limit: Callable[[Settings], int], window_seconds: int) -> Any:
    def check(
        principal: Annotated[Principal, Depends(get_principal)],
        limiter: Annotated[RateLimiter, Depends(_rate_limiter)],
    ) -> None:
        limiter.hit(
            bucket,
            str(principal.api_key_id),
            limit=limit(get_settings()),
            window_seconds=window_seconds,
        )

    return Depends(check)


async def exact_json_body(request: Request) -> Any:
    """Parse JSON with numbers as Decimal, so 70.10 reaches the pipeline as 70.10."""
    body = await request.body()
    if not body:
        raise BadRequest("the request body is empty")
    try:
        return json_codec.loads(body)
    except ValueError:
        raise BadRequest("the request body is not valid JSON") from None


Registry = Annotated[MappingRegistry, Depends(get_registry)]
Runner = Annotated[JobRunner, Depends(get_job_runner)]
JsonBody = Annotated[Any, Depends(exact_json_body)]
