"""Reading verified identity off the request.

Isolated in its own module so there is exactly one place that decides where a
tenant identifier is allowed to come from. Everything else calls
`tenant_from_context`; nothing else reaches into the token.
"""

from __future__ import annotations

from typing import Any

from signalhire_mcp.logging_setup import get_logger

logger = get_logger(__name__)


def tenant_from_context(context: Any | None, claim: str) -> str | None:
    """Extract a tenant identifier from *verified* token claims.

    Returns None when there is no authenticated token, rather than raising —
    the caller decides whether that is fatal. `EnvCredentialProvider` does not
    care; `RequestScopedCredentialProvider` treats it as an error.

    This deliberately never consults tool arguments, HTTP headers, query
    parameters, or the request body. Those are all attacker- or
    model-controlled. Only claims that `build_auth_provider` has already
    cryptographically verified are trusted.
    """
    token = _access_token(context)
    if token is None:
        return None

    claims = getattr(token, "claims", None) or {}
    value = claims.get(claim)
    if value is None:
        return None
    if not isinstance(value, str):
        logger.warning(
            "Tenant claim %r is %s, expected a string; ignoring it.", claim, type(value).__name__
        )
        return None
    return value or None


def _access_token(context: Any | None) -> Any | None:
    """Best-effort retrieval of the verified access token.

    Tries the explicitly passed context first so tests can inject one, then
    falls back to FastMCP's ambient dependency. Outside a request there is no
    token and that is not an error.
    """
    if context is not None:
        token = getattr(context, "access_token", None)
        if token is not None:
            return token

    try:
        from fastmcp.server.dependencies import get_access_token

        return get_access_token()
    except Exception:
        # No request in flight, or no auth configured. Both mean "no verified
        # identity", which is a legitimate state, not a failure.
        return None
