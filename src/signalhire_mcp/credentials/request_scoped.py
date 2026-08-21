"""Request-scoped credential resolution — the multi-tenant seam.

Two invariants that must survive any implementation:

* **The tenant identifier comes from verified token claims, never from a tool
  argument.** A tool argument is model-controlled: if `tenant_id` were a
  parameter, any caller could read or spend another tenant's credits by simply
  asking for them. The claim is verified by `signalhire_mcp.auth.verifier`
  before this provider ever runs.

* **A resolution failure raises `CredentialError`.** It must never fall back to
  the environment credential. Falling back would silently bill the wrong
  tenant's SignalHire account and file the results under the wrong customer,
  with no error anywhere.

The resolver is injected rather than imported so this repository never depends
on StaffHive. Anything with an `async def __call__(tenant_id) -> str` shape
works: a Supabase table, a secrets manager, an HTTP call to a vault.
"""

from __future__ import annotations

from typing import Any, Protocol

from signalhire_mcp.auth.claims import tenant_from_context
from signalhire_mcp.config import Settings
from signalhire_mcp.credentials.base import CredentialError, SignalHireCredential


class TenantKeyResolver(Protocol):
    """Maps a verified tenant identifier to that tenant's SignalHire API key."""

    async def __call__(self, tenant_id: str) -> str: ...


class RequestScopedCredentialProvider:
    """Resolves a per-request SignalHire credential from verified caller identity.

    Caches resolved keys per tenant for `cache_ttl_seconds`, because a secret
    store lookup on every reveal would add latency to an already-async flow for
    a value that changes approximately never.
    """

    def __init__(
        self,
        settings: Settings,
        resolver: TenantKeyResolver | None = None,
        *,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._settings = settings
        self._resolver = resolver
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, str]] = {}

    async def resolve(self, context: Any | None = None) -> SignalHireCredential:
        if self._resolver is None:
            raise CredentialError(
                "Request-scoped credential resolution is not configured. Either "
                "run this deployment single-tenant with EnvCredentialProvider, or "
                "inject a resolver mapping verified caller claims to a SignalHire "
                "API key."
            )

        tenant_id = tenant_from_context(context, self._settings.auth_tenant_claim)
        if not tenant_id:
            raise CredentialError(
                "No tenant claim on the verified token. Multi-tenant mode requires "
                f"SIGNALHIRE_AUTH_MODE=jwt and a {self._settings.auth_tenant_claim!r} "
                "claim; refusing to guess which tenant's credits to spend."
            )

        key = await self._resolve_key(tenant_id)
        return SignalHireCredential(
            api_key=key,
            base_url=self._settings.api_base_url,
            api_prefix=self._settings.api_prefix,
            tenant_id=tenant_id,
        )

    async def _resolve_key(self, tenant_id: str) -> str:
        import time

        now = time.monotonic()
        cached = self._cache.get(tenant_id)
        if cached and cached[0] > now:
            return cached[1]

        assert self._resolver is not None
        key = await self._resolver(tenant_id)
        if not key:
            raise CredentialError(
                f"No SignalHire API key on file for tenant {tenant_id!r}. Not "
                "falling back to the environment credential — that would bill "
                "the wrong account."
            )
        self._cache[tenant_id] = (now + self._cache_ttl, key)
        return key

    def describe(self) -> str:
        state = "resolver injected" if self._resolver else "no resolver"
        return f"request-scoped ({state})"
