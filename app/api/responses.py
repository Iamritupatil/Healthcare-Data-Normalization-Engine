"""JSON responses that keep clinical decimals exact.

FastAPI's default encoder turns Decimal into float (70.10 becomes 70.1). Routes return
`ExactJSONResponse` directly, which FastAPI passes through without re-encoding.
"""

from typing import Any

from starlette.responses import JSONResponse

from app import json_codec


class ExactJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json_codec.dumps(content).encode("utf-8")


class ProblemJSONResponse(ExactJSONResponse):
    media_type = "application/problem+json"
