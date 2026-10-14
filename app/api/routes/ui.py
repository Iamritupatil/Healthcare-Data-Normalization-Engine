"""The operator dashboard: a static page served next to the API at `/`.

The page holds no data of its own. It calls the same API with the key the user pastes,
so it can do exactly what that key's role allows. A strict Content-Security-Policy
allows only this server's own scripts and styles, and the page renders every value as
text, never as HTML.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

router = APIRouter(include_in_schema=False)


@router.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers=SECURITY_HEADERS)
