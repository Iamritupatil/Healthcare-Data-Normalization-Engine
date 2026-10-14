"""Request context: request id, one access log line per request, and a last-resort 500.

Unhandled errors are answered here instead of being re-raised to the server, because the
server would log the exception message, and messages from the database or validation
libraries can contain patient data.
"""

import time

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import problem_body
from app.api.responses import ProblemJSONResponse
from app.observability.logging import exception_summary, get_logger
from app.observability.request_id import (
    HEADER,
    accept_or_create,
    reset_request_id,
    set_request_id,
)

log = get_logger("medvora.access")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value for key, value in scope["headers"]}
        incoming = headers.get(HEADER.lower())
        request_id = accept_or_create(incoming.decode("latin-1") if incoming else None)
        token = set_request_id(request_id)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status = 500
        started = False
        began = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((HEADER.lower().encode(), request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as error:
            log.error("unhandled_error", **exception_summary(error))
            if not started:
                body = problem_body(
                    500, "internal-error", "Internal server error", None, scope["path"]
                )
                await ProblemJSONResponse(body, status_code=500)(scope, receive, send_with_id)
        finally:
            log.info(
                "request",
                method=scope["method"],
                path=scope["path"],
                status=status,
                duration_ms=round((time.perf_counter() - began) * 1000, 1),
                **scope.get("state", {}).get("log_ids", {}),
            )
            structlog.contextvars.clear_contextvars()
            reset_request_id(token)
