"""Structured JSON logs.

Rule for every log call in this codebase: ids, codes and counts only. Never names, birth
dates, payloads, or exception messages from the database or validation libraries, which
can echo row values. `exception_summary` gives a log-safe description of an error.
"""

import logging
import sys
import traceback
from typing import Any

import structlog


def configure_logging(level: str = "INFO") -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Route uvicorn's own loggers through the same JSON handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


def get_logger(name: str) -> Any:
    return structlog.stdlib.get_logger(name)


def exception_summary(exc: BaseException) -> dict[str, Any]:
    """Error type, SQLSTATE if any, and where it was raised; never the message itself."""
    frames = traceback.extract_tb(exc.__traceback__)
    origin = frames[-1] if frames else None
    summary: dict[str, Any] = {"error_type": type(exc).__name__}
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate:
        summary["sqlstate"] = sqlstate
    if origin is not None:
        summary["error_at"] = (
            f"{origin.filename.rsplit('/', 1)[-1]}:{origin.lineno} in {origin.name}"
        )
    return summary
