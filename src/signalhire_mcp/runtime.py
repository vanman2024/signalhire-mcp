"""Objects shared between the MCP tools, the HTTP routes, and the worker.

Held in one container, created by `create_server` and captured by closure,
rather than in module-level globals. The previous implementation used a
module-level `AppState` whose fields were populated in the lifespan, which
meant importing the module gave you an object with `client=None` and no
indication that it was unusable — every test and every `fastmcp inspect` run
saw a half-built server.
"""

from __future__ import annotations

from dataclasses import dataclass

from signalhire_mcp.config import Settings
from signalhire_mcp.credentials.base import CredentialProvider
from signalhire_mcp.delivery.registry import TenantRegistry
from signalhire_mcp.delivery.worker import DeliveryWorker
from signalhire_mcp.inbox.store import InboxStore
from signalhire_mcp.signalhire.client import SignalHireClient


@dataclass
class Runtime:
    settings: Settings
    store: InboxStore
    registry: TenantRegistry
    client: SignalHireClient
    credentials: CredentialProvider
    worker: DeliveryWorker

    async def credential_for(self, tenant_id: str | None = None):
        """Resolve the SignalHire credential for the current request.

        Prefers a tenant's own key (BYOK) when the registry has one, and falls
        back to the process credential (the agency model). `tenant_id` is only
        ever supplied from verified claims or from a configured default — never
        from a tool argument. See credentials/request_scoped.py.
        """
        from signalhire_mcp.credentials.base import SignalHireCredential

        resolved = tenant_id or "default"
        if self.registry.knows(resolved):
            own_key = self.registry.api_key_for(resolved)
            if own_key:
                return SignalHireCredential(
                    api_key=own_key,
                    base_url=self.settings.api_base_url,
                    api_prefix=self.settings.api_prefix,
                    tenant_id=resolved,
                )
        return await self.credentials.resolve()
