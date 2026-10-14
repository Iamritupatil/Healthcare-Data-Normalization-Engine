"""Creates the FastAPI app. Run with: uvicorn --factory app.main:create_app"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.limits import BodySizeLimitMiddleware, RateLimiter
from app.api.responses import ExactJSONResponse
from app.api.routes import (
    findings,
    health,
    jobs,
    matches,
    metrics,
    patients,
    quarantine,
    records,
    uploads,
)
from app.config import Settings, get_settings
from app.db import get_engine
from app.jobs.runner import get_job_runner
from app.observability.logging import configure_logging, get_logger
from app.observability.middleware import RequestContextMiddleware
from app.pipeline.mapping import MappingRegistry, load_registry

log = get_logger(__name__)

DESCRIPTION = """
Turns patient and observation records from different source systems into one canonical,
FHIR-like shape. Every input ends up either normalized or quarantined with reasons, and
every normalized field can be traced back to its raw value and the rule that changed it.

Authenticate with the `X-API-Key` header. Every error is an RFC 9457 problem document.
"""

# Room for multipart boundaries and form fields around a file at the size limit.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("api_started", sources=app.state.registry.sources())
    yield
    get_engine().dispose()


def create_app(
    settings: Settings | None = None, registry: MappingRegistry | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Medvora",
        version="0.1.0",
        summary="Healthcare record normalization engine",
        description=DESCRIPTION,
        lifespan=lifespan,
        default_response_class=ExactJSONResponse,
    )
    # Loading here means a broken mapping file stops the app at startup, not mid-request.
    app.state.registry = registry or load_registry(settings.mappings_dir)
    app.state.rate_limiter = RateLimiter()
    app.state.job_runner = get_job_runner(settings)

    register_error_handlers(app)
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=settings.max_request_bytes,
        path_limits={"/v1/uploads": settings.upload_max_bytes + MULTIPART_OVERHEAD_BYTES},
    )
    # Added last, so it is outermost: even a 413 from the size limit carries a request id.
    app.add_middleware(RequestContextMiddleware)

    routers = (health, records, patients, matches, uploads, jobs, quarantine, findings, metrics)
    for module in routers:
        app.include_router(module.router)
    return app
