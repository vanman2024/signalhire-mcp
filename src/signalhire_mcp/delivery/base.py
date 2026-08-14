"""The adapter seam.

An adapter takes a normalised enrichment result and writes it somewhere. That
"somewhere" is deliberately unconstrained: StaffHive's database, an ATS reached
over MCP, a queue, another product. The worker does not know or care which.

This is the seam that keeps the system from being CATS-specific. CATS is one
adapter, reached over MCP like any other; adding Bullhorn or Greenhouse later
means configuring another `McpAdapter`, not writing another integration.

Contract, in three parts:

* `deliver` must be **idempotent**. The worker retries, and a partial failure
  re-delivers to the adapters that have not yet succeeded. An adapter that
  creates a duplicate record on the second call will create duplicates in
  production, because retries are normal here, not exceptional.
* `deliver` must **not raise** for an expected failure. Return
  `DeliveryResult(ok=False, detail=...)` so the worker can schedule a retry
  with a readable reason. Raising is reserved for programmer error.
* `deliver` must be **bounded in time**. The worker awaits it; an adapter that
  hangs stalls delivery for every other event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from signalhire_mcp.delivery.normalize import RevealedProfile
from signalhire_mcp.inbox.models import IntegrationEvent


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    detail: str = ""

    @classmethod
    def success(cls, detail: str = "") -> DeliveryResult:
        return cls(ok=True, detail=detail)

    @classmethod
    def failure(cls, detail: str) -> DeliveryResult:
        return cls(ok=False, detail=detail)


@runtime_checkable
class Adapter(Protocol):
    """Writes an enrichment result to one downstream system."""

    #: Stable identifier. Persisted per event to track which adapters have
    #: already succeeded, so renaming one silently re-delivers every event that
    #: is still pending. Treat it as part of the on-disk format.
    name: str

    async def deliver(
        self, event: IntegrationEvent, profiles: list[RevealedProfile]
    ) -> DeliveryResult: ...

    def describe(self) -> str:
        """Short non-secret description, used in startup logs."""
        ...

    async def aclose(self) -> None:
        """Release any held resources. Called on shutdown."""
        ...
