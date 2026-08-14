"""Shared fixtures.

Every fixture builds the server through `create_server()` with injected
collaborators — a temp-directory store, a stub credential provider, a fake
SignalHire client. Nothing here touches the network or the real environment, so
the suite is safe to run anywhere and cannot spend credits.
"""

from __future__ import annotations

from typing import Any

import pytest

from signalhire_mcp.config import AuthMode, Settings, Transport
from signalhire_mcp.credentials.base import SignalHireCredential
from signalhire_mcp.delivery.base import DeliveryResult
from signalhire_mcp.delivery.registry import TenantRegistry
from signalhire_mcp.inbox.store import InboxStore
from signalhire_mcp.server import create_server

SAMPLE_CALLBACK: list[dict[str, Any]] = [
    {
        "item": "https://www.linkedin.com/in/jane-welder",
        "status": "success",
        "candidate": {
            "uid": "a" * 32,
            "fullName": "Jane Welder",
            "headLine": "Red Seal Welder at Acme Fabrication",
            "summary": "15 years structural welding.",
            "photo": {"url": "https://cdn.example.com/jane.jpg"},
            "locations": [{"name": "Calgary, Alberta, Canada"}],
            "skills": ["GMAW", "TIG", "Blueprint Reading"],
            "contacts": [
                {"type": "email", "value": "jane.work@acme.com", "rating": "100",
                 "subType": "work"},
                {"type": "email", "value": "jane@gmail.com", "rating": "100",
                 "subType": "personal"},
                {"type": "phone", "value": "+1 403-555-0100", "rating": "100",
                 "subType": "work_phone"},
            ],
            "social": [
                {"type": "li", "link": "https://www.linkedin.com/in/jane-welder",
                 "rating": "100"}
            ],
            "experience": [
                {"position": "Welder", "company": "Acme Fabrication", "current": True,
                 "started": "2015-01-01T00:00:00+00:00", "ended": None,
                 "summary": "Structural steel."},
                {"position": "Apprentice", "company": "Old Shop", "current": False,
                 "started": "2010-01-01T00:00:00+00:00",
                 "ended": "2014-12-31T00:00:00+00:00"},
            ],
            "education": [
                {"university": "SAIT", "faculty": "Welding", "degree": ["Red Seal"],
                 "startedYear": 2008, "endedYear": 2010}
            ],
        },
    },
    {"item": "nobody@example.com", "status": "failed"},
    {"item": "b" * 32, "status": "credits_are_over"},
]


class StubCredentials:
    """A credential provider that never reads the environment."""

    def __init__(self, tenant_id: str = "default") -> None:
        self._tenant_id = tenant_id

    async def resolve(self, context: Any | None = None) -> SignalHireCredential:
        return SignalHireCredential(api_key="stub-key", tenant_id=self._tenant_id)

    def describe(self) -> str:
        return "stub"


class RecordingAdapter:
    """Adapter that records what it received and can be told to fail."""

    def __init__(self, name: str = "recorder", fail_times: int = 0) -> None:
        self.name = name
        self.calls: list[tuple[str, int]] = []
        self.profiles: list[Any] = []
        self._fail_times = fail_times

    async def deliver(self, event, profiles) -> DeliveryResult:
        self.calls.append((event.event_id, len(profiles)))
        self.profiles = profiles
        if self._fail_times > 0:
            self._fail_times -= 1
            return DeliveryResult.failure("stubbed failure")
        return DeliveryResult.success("stored")

    def describe(self) -> str:
        return f"recording adapter {self.name}"

    async def aclose(self) -> None:
        return None


class FakeSignalHireClient:
    """Stands in for the REST client. Records calls, returns canned answers."""

    def __init__(self) -> None:
        self.reveals: list[dict[str, Any]] = []
        self.next_request_id = 4242
        self.credits = 1000

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def reveal(self, credential, *, items, callback_url, without_contacts=False) -> str:
        self.reveals.append(
            {"items": list(items), "callback_url": callback_url,
             "without_contacts": without_contacts, "tenant": credential.tenant_id}
        )
        return str(self.next_request_id)

    async def check_credits(self, credential, *, without_contacts=False) -> int:
        return 0 if without_contacts else self.credits

    async def search_by_query(self, credential, criteria, *, size=25) -> dict[str, Any]:
        return {
            "requestId": 7,
            "total": 1,
            "scrollId": "cursor-1",
            "profiles": [{"uid": "c" * 32, "fullName": "Search Result"}],
        }

    async def scroll_search(self, credential, request_id, scroll_id) -> dict[str, Any]:
        return {"requestId": request_id, "total": 1, "profiles": [], "scrollId": None}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        transport=Transport.HTTP,
        auth_mode=AuthMode.NONE,
        data_dir=tmp_path / "inbox",
        api_key="stub-key",
        public_base_url="https://signalhire.test",
        callback_secret="s3cret",
        worker_enabled=False,  # tests drive the worker explicitly
        backoff_base_seconds=0.01,
        max_backoff_seconds=0.02,
        log_timing=False,
    )


@pytest.fixture
def store(settings) -> InboxStore:
    store = InboxStore(settings.data_dir)
    store.initialize()
    return store


@pytest.fixture
def adapter() -> RecordingAdapter:
    return RecordingAdapter()


@pytest.fixture
def registry(adapter) -> TenantRegistry:
    registry = TenantRegistry({"default": {"adapters": []}, "acme": {"adapters": []}})
    # Inject the stub directly, bypassing config-driven construction.
    registry._adapters["default"] = [adapter]
    registry._adapters["acme"] = [adapter]
    return registry


@pytest.fixture
def fake_client() -> FakeSignalHireClient:
    return FakeSignalHireClient()


@pytest.fixture
def server(settings, store, registry, fake_client):
    return create_server(
        settings,
        credential_provider=StubCredentials(),
        registry=registry,
        client=fake_client,
        store=store,
        auth_provider=None,
    )


@pytest.fixture
async def client(server):
    """In-memory MCP client — the pattern from the FastMCP testing docs."""
    from fastmcp import Client

    async with Client(server) as mcp_client:
        yield mcp_client


@pytest.fixture
async def http(server):
    """Drive the real ASGI app, including custom routes, without binding a port.

    The FastMCP testing docs cover the in-memory Client but say nothing about
    custom HTTP routes. Entering the Starlette lifespan and talking to the app
    through an ASGI transport is what makes the callback endpoint — the most
    important code in this package — testable end to end.

    The lifespan runs inside its own task rather than directly in the fixture
    body. MCP's streamable-HTTP manager opens an anyio task group, and a task
    group must be exited by the task that entered it; pytest-asyncio sets up
    and tears down async-generator fixtures in *different* tasks, which
    otherwise fails teardown with "attempted to exit cancel scope in a
    different task". Owning the lifespan in one long-lived task keeps entry and
    exit together.
    """
    import asyncio

    import httpx2

    app = server.http_app(path="/mcp/")
    started = asyncio.Event()
    finish = asyncio.Event()
    failure: list[BaseException] = []

    async def own_lifespan() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await finish.wait()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the fixture
            failure.append(exc)
            started.set()

    task = asyncio.create_task(own_lifespan())
    await started.wait()
    if failure:
        raise failure[0]

    transport = httpx2.ASGITransport(app=app)
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        finish.set()
        await task
        if failure:
            raise failure[0]
