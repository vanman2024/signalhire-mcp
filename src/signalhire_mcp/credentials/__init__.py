"""Credential resolution for outbound SignalHire calls."""

from signalhire_mcp.credentials.base import (
    CredentialError,
    CredentialProvider,
    SignalHireCredential,
)
from signalhire_mcp.credentials.env import EnvCredentialProvider
from signalhire_mcp.credentials.request_scoped import (
    RequestScopedCredentialProvider,
    TenantKeyResolver,
)

__all__ = [
    "CredentialError",
    "CredentialProvider",
    "EnvCredentialProvider",
    "RequestScopedCredentialProvider",
    "SignalHireCredential",
    "TenantKeyResolver",
]
