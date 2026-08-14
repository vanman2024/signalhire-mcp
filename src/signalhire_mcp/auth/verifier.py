"""Authentication of the *MCP caller*.

Distinct from the SignalHire credential the adapter uses downstream (see
`signalhire_mcp.credentials`). Conflating the two is what limits a deployment
to one SignalHire account; keeping them apart is what lets a multi-tenant
deployment verify a caller identity and then resolve the right SignalHire
connection.

This matters concretely here. The previous deployment listened on
`0.0.0.0:8000` with no authentication configured in the application at all —
every tool reachable by anyone who could reach the URL, including the ones that
spend credits. On this server an unauthenticated caller does not just read
data, they can bill the account: each `reveal_contact` costs a credit whether
or not the result is ever collected.

Three ways that gets addressed, and the operator has to say which one applies,
because the server cannot detect it.
"""

from __future__ import annotations

from typing import Any

from signalhire_mcp.config import AuthMode, Settings, Transport
from signalhire_mcp.logging_setup import get_logger

logger = get_logger(__name__)


class InsecureDeploymentError(RuntimeError):
    """An HTTP listener was started without saying who authenticates callers."""


def _require_mode(settings: Settings) -> AuthMode:
    """Resolve the auth mode, refusing to guess for a self-served HTTP listener."""
    if settings.auth_mode is not None:
        return settings.auth_mode

    # A JWKS URI on its own is an unambiguous statement of intent.
    if settings.auth_jwks_uri:
        return AuthMode.JWT

    if settings.transport is not Transport.HTTP:
        # stdio: the transport is already private to the user who launched it.
        return AuthMode.NONE

    raise InsecureDeploymentError(
        "Refusing to serve HTTP without knowing who authenticates callers.\n"
        "\n"
        "Every reveal this server performs spends a SignalHire credit, so an "
        "open endpoint is a way to bill your account, not just to read it.\n"
        "\n"
        "Set SIGNALHIRE_AUTH_MODE to one of:\n"
        "\n"
        "  platform  Something in front of this server authenticates first —\n"
        "            a managed gateway, or a reverse proxy terminating TLS.\n"
        "\n"
        "  jwt       This server verifies bearer tokens itself. Also set:\n"
        "              SIGNALHIRE_AUTH_JWKS_URI=https://<issuer>/.well-known/jwks.json\n"
        "              SIGNALHIRE_AUTH_ISSUER=https://<issuer>/\n"
        "              SIGNALHIRE_AUTH_AUDIENCE=<this server's audience>\n"
        "            Required for multi-tenant routing: the tenant is read from\n"
        "            the verified claims.\n"
        "\n"
        "  none      Nobody authenticates. Local development only.\n"
        "\n"
        "There is no default because guessing wrong is harmful either way: "
        "assume a gateway that is not there and the credit-spending tools sit "
        "on an open URL; assume none and a correctly-fronted deployment fails "
        "to start.\n"
        "\n"
        "Note: the callback endpoint is deliberately exempt. FastMCP custom "
        "routes are never behind auth middleware, and SignalHire cannot present "
        "a bearer token — it is protected by SIGNALHIRE_CALLBACK_SECRET instead."
    )


def build_auth_provider(settings: Settings) -> Any | None:
    """Return the FastMCP auth provider, or None when this server does not verify.

    Returning None does not mean "unauthenticated" — under `platform` it means
    verification happened upstream, before this process was reached.
    """
    mode = _require_mode(settings)

    if mode is AuthMode.JWT:
        if not settings.auth_jwks_uri:
            raise InsecureDeploymentError(
                "SIGNALHIRE_AUTH_MODE=jwt requires SIGNALHIRE_AUTH_JWKS_URI so "
                "tokens can be verified against the issuer's public keys."
            )
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        logger.info(
            "MCP auth: this server verifies JWTs (issuer=%s audience=%s tenant_claim=%s)",
            settings.auth_issuer or "<any>",
            settings.auth_audience or "<any>",
            settings.auth_tenant_claim,
        )
        return JWTVerifier(
            jwks_uri=settings.auth_jwks_uri,
            issuer=settings.auth_issuer or None,
            audience=settings.auth_audience or None,
        )

    if mode is AuthMode.PLATFORM:
        logger.info(
            "MCP auth: delegated to the platform. This server trusts that a "
            "gateway in front of it has already verified the caller, and "
            "performs no verification itself. If nothing is in front of it, "
            "every credit-spending tool is exposed."
        )
        return None

    if settings.transport is Transport.HTTP:
        logger.warning(
            "MCP auth DISABLED on an HTTP listener (SIGNALHIRE_AUTH_MODE=none). "
            "Every tool, including the ones that spend SignalHire credits, is "
            "callable by anyone who can reach this URL. Do not use this for a "
            "deployment."
        )
    return None


def require_callback_protection(settings: Settings) -> None:
    """Refuse to serve an unauthenticated callback endpoint.

    The callback route sits outside FastMCP's auth middleware by necessity —
    SignalHire cannot present a bearer token — so the shared secret is the only
    thing standing between the internet and this server's delivery pipeline.

    Without it, anyone who can reach the URL can POST forged enrichment
    results, which the server will durably store and then hand to every
    configured adapter: fabricated emails and phone numbers written into the
    ATS and the candidate database, indistinguishable from real ones. That is a
    worse outcome than the endpoint being unavailable.

    `auth_mode=none` already declares "this is local development", so it is the
    one case where the check is relaxed.
    """
    if settings.transport is not Transport.HTTP:
        return
    if settings.callback_secret:
        return

    try:
        mode = _require_mode(settings)
    except InsecureDeploymentError:
        # The auth mode itself is unset; that error is the more useful one and
        # will be raised on its own.
        return

    if mode is AuthMode.NONE:
        logger.warning(
            "SIGNALHIRE_CALLBACK_SECRET is empty. The callback endpoint accepts "
            "unauthenticated POSTs, so anyone who can reach it can inject forged "
            "enrichment results. Tolerated only because SIGNALHIRE_AUTH_MODE=none "
            "declares this a development deployment."
        )
        return

    raise InsecureDeploymentError(
        "SIGNALHIRE_CALLBACK_SECRET is not set.\n"
        "\n"
        "The callback endpoint cannot sit behind MCP authentication — SignalHire "
        "sends no bearer token and no custom headers — so this shared secret is "
        "the only control on it. Without one, anyone who can reach the URL can "
        "POST forged enrichment results that this server will store and then "
        "write into every configured destination.\n"
        "\n"
        "Generate one:\n"
        "    openssl rand -hex 32\n"
        "\n"
        "Set SIGNALHIRE_CALLBACK_SECRET to it. The value is appended to the "
        "callbackUrl handed to SignalHire, so no further configuration is needed."
    )


def auth_is_enforced(settings: Settings) -> bool:
    """Whether verified claims are available for per-tenant routing.

    Only true when this server verifies tokens itself — claims come from the
    tokens it validated. Under `platform` the gateway authenticates but this
    server sees no claims, so multi-tenant credential resolution is not
    available and the deployment must be single-tenant.
    """
    try:
        return _require_mode(settings) is AuthMode.JWT
    except InsecureDeploymentError:
        return False
