"""Credential resolution for outbound SignalHire calls.

Three layers are deliberately kept apart:

1. Authentication of the *MCP caller*         -> `signalhire_mcp.auth.verifier`
2. Authorization to call a given tool         -> tool-level scope checks
3. The *SignalHire* credential the adapter uses -> this module

Conflating (1) and (3) is what limits a deployment to a single SignalHire
account. Keeping them apart is what lets one process serve several tenants,
each spending credits from their own account.

That separation matters more here than for most adapters, because SignalHire
credits are money and there is exactly one pool per API key. SignalHire has no
sub-account model: seats share one credit pool and share all revealed contacts.
So if several customers are served through one key, this process is the only
place that can attribute spend to a tenant — the vendor will not do it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_REDACTED = "***redacted***"


@dataclass(frozen=True)
class SignalHireCredential:
    """A resolved SignalHire credential plus the non-secret context it belongs to.

    The secret is never included in `repr`, `str`, or any dict/JSON form. Tool
    results are serialised straight to the model, so a credential that
    stringifies to its own secret is one careless log line away from leaking
    into a transcript.
    """

    api_key: str = field(repr=False)
    base_url: str = "https://www.signalhire.com"
    api_prefix: str = "/api/v1"
    # Non-secret label used for logging, correlation and credit attribution.
    # Never a key, never a token.
    tenant_id: str = "default"

    def auth_header(self) -> dict[str, str]:
        # SignalHire uses a bare `apikey` header, not Bearer.
        return {"apikey": self.api_key}

    # --- leak guards --------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"SignalHireCredential(base_url={self.base_url!r}, "
            f"tenant_id={self.tenant_id!r}, api_key={_REDACTED})"
        )

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:
        # f"{cred}" must not become a way to bypass the redacted repr.
        return self.__repr__()

    def redacted(self) -> dict[str, Any]:
        """Safe to log or return. Contains no secret."""
        return {
            "base_url": self.base_url,
            "tenant_id": self.tenant_id,
            "api_key": _REDACTED,
        }


class CredentialError(RuntimeError):
    """Raised when no usable SignalHire credential can be resolved."""


@runtime_checkable
class CredentialProvider(Protocol):
    """Resolves the SignalHire credential to use for one request.

    Implementations must never return the secret through any other channel, and
    must raise `CredentialError` rather than returning a placeholder or empty
    key — an empty key produces a 401 from SignalHire far from the cause.
    """

    async def resolve(self, context: Any | None = None) -> SignalHireCredential:
        """Return the credential for this request.

        `context` is the FastMCP request context when one is available. It is
        optional so startup validation and tests can call `resolve()` without
        constructing a request.
        """
        ...

    def describe(self) -> str:
        """Short non-secret description, used in startup logs."""
        ...
