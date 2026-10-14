"""Request size limits and per-key rate limits.

Size: checked from Content-Length when present, and again while the body streams in,
because a chunked request has no Content-Length and a client can send more than it
declared. Nothing over the limit is ever fully read into memory.

Rate: fixed windows per API key, kept in process memory. That is right for one API
process; with several instances the counters would move to Redis.
"""

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import RateLimited, problem_body
from app.api.responses import ProblemJSONResponse


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, default_limit: int, path_limits: dict[str, int]) -> None:
        self.app = app
        self.default_limit = default_limit
        self.path_limits = path_limits

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.path_limits.get(scope["path"], self.default_limit)
        detail = f"the limit for this endpoint is {limit} bytes"
        for key, value in scope["headers"]:
            if key == b"content-length" and value.isdigit() and int(value) > limit:
                body = problem_body(
                    413, "payload-too-large", "Request is too large", detail, scope["path"]
                )
                await ProblemJSONResponse(body, status_code=413)(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # FastAPI re-raises HTTPExceptions raised while it reads the body.
                    raise HTTPException(413, detail)
            return message

        await self.app(scope, limited_receive, send)


@dataclass
class _Window:
    started: float
    count: int


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._windows: dict[tuple[str, str], _Window] = {}
        self._lock = threading.Lock()

    def hit(self, bucket: str, key: str, *, limit: int, window_seconds: int) -> None:
        """Count one request; raise RateLimited with Retry-After when over the limit."""
        now = self._clock()
        with self._lock:
            window = self._windows.get((bucket, key))
            if window is None or now - window.started >= window_seconds:
                window = self._windows[(bucket, key)] = _Window(now, 0)
            if window.count >= limit:
                retry_after = max(1, math.ceil(window.started + window_seconds - now))
                raise RateLimited(
                    f"limit is {limit} per {window_seconds} seconds for this API key",
                    headers={"Retry-After": str(retry_after)},
                )
            window.count += 1
