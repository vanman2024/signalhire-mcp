"""Authentication of the MCP caller."""

from signalhire_mcp.auth.claims import tenant_from_context
from signalhire_mcp.auth.verifier import (
    InsecureDeploymentError,
    auth_is_enforced,
    build_auth_provider,
)

__all__ = [
    "InsecureDeploymentError",
    "auth_is_enforced",
    "build_auth_provider",
    "tenant_from_context",
]
