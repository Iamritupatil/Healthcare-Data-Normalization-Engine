"""Browser tests for the dashboard, driven by Playwright against a running stack.

They are skipped unless asked for, because they need the API and worker running:

    docker compose up -d
    pip install playwright && playwright install chromium   # or use an installed browser:
    MEDVORA_E2E=1 pytest tests/e2e                         # MEDVORA_E2E_CHANNEL=msedge|chrome

MEDVORA_BASE_URL (default http://127.0.0.1:8000) picks the server, MEDVORA_E2E_DB_NAME
(default medvora) the database it uses, and MEDVORA_E2E_HEADED=1 shows the browser.
Each test gets its own fresh organisation, so tests never see each other's data.
"""

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.config import get_settings
from scripts.seed import create_org

ENABLED = os.environ.get("MEDVORA_E2E") == "1"
BASE_URL = os.environ.get("MEDVORA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
SAMPLES = Path(__file__).resolve().parents[2] / "app" / "static" / "samples"
# Chrome reports every 4xx/5xx fetch as a console error; tests trigger those on purpose.
EXPECTED_CONSOLE = ("Failed to load resource",)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if ENABLED:
        return
    skip = pytest.mark.skip(reason="browser tests: start the stack, then set MEDVORA_E2E=1")
    for item in items:
        if "e2e" in item.path.parts:
            item.add_marker(skip)


@dataclass
class Org:
    name: str
    keys: dict[str, str]
    http: httpx.Client = field(repr=False)

    def post_record(self, source: str, record_type: str, payload: dict[str, Any]) -> httpx.Response:
        return self.http.post(
            "/v1/records",
            json={"source": source, "record_type": record_type, "payload": payload},
            headers={"X-API-Key": self.keys["ingest"]},
        )

    def get(self, path: str, **params: Any) -> Any:
        return self.http.get(path, params=params, headers={"X-API-Key": self.keys["admin"]}).json()

    def wait_until(self, condition: Callable[[], bool], seconds: float = 60) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.5)
        raise AssertionError("timed out waiting for the worker")


def _make_org(label: str) -> Org:
    # The test session points at the test database; these orgs belong to the running stack's.
    settings = get_settings().model_copy(
        update={"db_name": os.environ.get("MEDVORA_E2E_DB_NAME", "medvora")}
    )
    name = f"{label} {uuid4().hex[:4]}"
    _, keys = create_org(name, settings=settings)
    return Org(name=name, keys=keys, http=httpx.Client(base_url=BASE_URL, timeout=30))


@pytest.fixture
def org() -> Org:
    return _make_org("E2E Hospital")


@pytest.fixture
def other_org() -> Org:
    return _make_org("E2E Other Hospital")


@pytest.fixture(scope="session")
def playwright_api() -> Any:
    return pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="session")
def browser(playwright_api: Any) -> Iterator[Any]:
    with playwright_api.sync_playwright() as playwright:
        launched = playwright.chromium.launch(
            channel=os.environ.get("MEDVORA_E2E_CHANNEL") or None,
            headless=os.environ.get("MEDVORA_E2E_HEADED") != "1",
        )
        yield launched
        launched.close()


@pytest.fixture
def expect(playwright_api: Any) -> Any:
    playwright_api.expect.set_options(timeout=15_000)
    return playwright_api.expect


@dataclass
class Dashboard:
    page: Any
    errors: list[str]

    def open(self, key: str, section: str = "overview") -> None:
        self.page.goto(f"{BASE_URL}/")
        self.page.get_by_label("API key").fill(key)
        self.page.get_by_role("button", name="Connect").click()
        self.page.locator("#who").wait_for(state="visible")
        if section != "overview":
            self.go(section)

    def go(self, section: str) -> None:
        self.page.evaluate("hash => { location.hash = hash; }", f"#/{section}")
        self.page.wait_for_load_state("networkidle")


@pytest.fixture
def dashboard(browser: Any, request: pytest.FixtureRequest) -> Iterator[Dashboard]:
    viewport = getattr(request, "param", {"width": 1280, "height": 900})
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    errors: list[str] = []
    page.on(
        "console", lambda message: errors.append(message.text) if message.type == "error" else None
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield Dashboard(page, errors)
    context.close()
    unexpected = [e for e in errors if not e.startswith(EXPECTED_CONSOLE)]
    assert unexpected == [], f"browser errors: {unexpected}"
