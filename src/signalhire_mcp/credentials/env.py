"""Single-tenant credential resolution from the environment."""

from __future__ import annotations

from typing import Any

from signalhire_mcp.config import Settings
from signalhire_mcp.credentials.base import (
    CredentialError,
    SignalHireCredential,
)


class EnvCredentialProvider:
    """Serves one SignalHire credential, read from settings, for every request.

    Correct for a deployment that serves a single SignalHire account — one
    customer's own key under BYOK, or your own key when you are the only
    consumer. For several tenants in one process, see
    `RequestScopedCredentialProvider`.
    """

    def __init__(self, settings: Settings, tenant_id: str = "default") -> None:
        self._settings = settings
        self._tenant_id = tenant_id

    async def resolve(self, context: Any | None = None) -> SignalHireCredential:
        key = self._settings.api_key
        if not key:
            raise CredentialError(
                "SIGNALHIRE_API_KEY is not set. Every SignalHire call needs it, "
                "and requests without it fail with a 401 that does not name the "
                "cause."
            )
        return SignalHireCredential(
            api_key=key,
            base_url=self._settings.api_base_url,
            api_prefix=self._settings.api_prefix,
            tenant_id=self._tenant_id,
        )

    def describe(self) -> str:
        state = "configured" if self._settings.api_key else "MISSING"
        return f"environment ({state}, tenant={self._tenant_id})"
