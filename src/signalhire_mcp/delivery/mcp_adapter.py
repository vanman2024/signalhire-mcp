"""Deliver an enrichment result by calling a tool on another MCP server.

This is the whole of the "CATS adapter". There is no CATS HTTP code in this
repository and there should never be any: CATS already has a maintained MCP
server with 200-odd tools, and duplicating its client here would mean two
implementations of the same API drifting apart. The same class serves any
future ATS that exposes an MCP server.

What this deliberately does not do
----------------------------------
It does not decide recruiting policy — which candidate matches which job,
whether to enrol someone in a cadence. It maps a revealed profile onto a tool
call. Anything smarter belongs in the system that owns the workflow.

Mounting vs. calling
--------------------
Two different things, both useful, often confused:

* `mcp.mount(create_proxy(...), namespace="ats")` exposes the remote server's
  tools *to agents* through this endpoint. That is composition, for discovery.
* This adapter *calls* the remote server from server-side background code. A
  mount would not help here — the worker is not an MCP client of itself.

A deployment usually wants both, and they are configured independently.
"""

from __future__ import annotations

import time
from typing import Any

from signalhire_mcp.delivery.base import DeliveryResult
from signalhire_mcp.delivery.normalize import RevealedProfile
from signalhire_mcp.inbox.models import IntegrationEvent
from signalhire_mcp.logging_setup import get_logger

logger = get_logger(__name__)


class McpAdapter:
    """Calls a named tool on a remote MCP server, once per revealed profile.

    The payload sent to the tool is the normalised profile as a dict, under a
    configurable argument name. Nothing about SignalHire's wire format leaks
    across this boundary, so the receiving tool has a stable contract even if
    SignalHire changes theirs.
    """

    def __init__(
        self,
        name: str,
        *,
        server: str,
        tool: str,
        argument: str = "profile",
        static_arguments: dict[str, Any] | None = None,
        auth_token: str | None = None,
        timeout: float = 60.0,
        skip_unsuccessful: bool = True,
    ) -> None:
        self.name = name
        self._server = server
        self._tool = tool
        self._argument = argument
        self._static = static_arguments or {}
        self._auth_token = auth_token
        self._timeout = timeout
        self._skip_unsuccessful = skip_unsuccessful

    async def deliver(
        self, event: IntegrationEvent, profiles: list[RevealedProfile]
    ) -> DeliveryResult:
        targets = [p for p in profiles if p.succeeded] if self._skip_unsuccessful else profiles
        if not targets:
            # Nothing to write is a success, not a failure. Retrying a callback
            # that contained only `credits_are_over` items would retry forever.
            return DeliveryResult.success("no successful profiles to deliver")

        try:
            client = self._build_client()
        except Exception as exc:  # noqa: BLE001 - surfaced as a retryable failure
            return DeliveryResult.failure(f"could not construct MCP client: {exc}")

        delivered = 0
        started = time.monotonic()
        try:
            async with client:
                for profile in targets:
                    arguments = {
                        self._argument: profile.model_dump(mode="json"),
                        **self._static,
                        "correlation_id": event.correlation_id,
                    }
                    result = await client.call_tool(self._tool, arguments)
                    if getattr(result, "is_error", False):
                        return DeliveryResult.failure(
                            f"{self._tool} reported an error for item {profile.item!r}: "
                            f"{_render(result)}"
                        )
                    delivered += 1
        except Exception as exc:  # noqa: BLE001 - network/protocol failures are retryable
            return DeliveryResult.failure(
                f"{self._tool} on {self._server} failed after {delivered}/{len(targets)} "
                f"profile(s): {type(exc).__name__}: {exc}"
            )

        elapsed = int((time.monotonic() - started) * 1000)
        return DeliveryResult.success(
            f"called {self._tool} for {delivered} profile(s) in {elapsed}ms"
        )

    def _build_client(self) -> Any:
        from fastmcp import Client

        # A fresh client per delivery. Holding a long-lived connection to a
        # remote MCP server across a worker that may idle for hours means
        # discovering the connection is dead at the least convenient moment;
        # reconnecting costs a few hundred milliseconds on a path that is
        # already asynchronous by nature.
        if self._auth_token:
            return Client(self._server, auth=self._auth_token, timeout=self._timeout)
        return Client(self._server, timeout=self._timeout)

    def describe(self) -> str:
        auth = "authenticated" if self._auth_token else "no auth"
        return f"mcp -> {self._server} calling {self._tool}() ({auth})"

    async def aclose(self) -> None:
        # Clients are per-delivery; nothing is held open.
        return None


def _render(result: Any) -> str:
    """Best-effort human-readable rendering of a failed tool result."""
    data = getattr(result, "data", None)
    if data is not None:
        return str(data)[:500]
    content = getattr(result, "content", None)
    if content:
        return str(content)[:500]
    return str(result)[:500]
