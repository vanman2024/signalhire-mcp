"""Runtime configuration for the SignalHire MCP adapter.

Every setting is read from the environment. Nothing here knows about StaffHive,
recruiting workflows, or any particular consumer — those arrive as adapter
configuration, not as imports.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# FastMCP resolves `${VAR}` placeholders in fastmcp.json against the process
# environment, and documents that an unset variable leaves "the placeholder
# preserved as-is". So a missing secret arrives as the literal string
# "${SIGNALHIRE_API_KEY}" rather than as an error, and surfaces much later as a
# confusing 401. Catch it at startup instead.
_UNRESOLVED_PLACEHOLDER = re.compile(r"^\$\{[^}]*\}$")


class Transport(str, Enum):
    STDIO = "stdio"
    HTTP = "http"


class AuthMode(str, Enum):
    """Who verifies the MCP caller.

    PLATFORM  Something in front of this server authenticates before traffic
              arrives — a managed gateway or a reverse proxy terminating TLS.
              The server trusts that and says so at startup.
    JWT       This server verifies bearer tokens itself, via a JWKS URI. This
              is the mode that makes multi-tenant routing possible, because the
              tenant is read from *verified* claims.
    NONE      Nobody verifies anything. Local development only.

    There is deliberately no "allow unauthenticated" boolean. A flag that reads
    as "we gave up on auth" cannot distinguish a correctly-fronted deployment
    from an exposed one, and those need very different reactions from whoever
    reads the config next.
    """

    PLATFORM = "platform"
    JWT = "jwt"
    NONE = "none"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SIGNALHIRE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- SignalHire API -----------------------------------------------------
    api_base_url: str = "https://www.signalhire.com"
    api_prefix: str = "/api/v1"
    api_key: str = ""

    # --- Transport ----------------------------------------------------------
    transport: Transport = Transport.STDIO
    host: str = "0.0.0.0"
    port: int = 8000
    mcp_path: str = "/mcp/"

    # --- Public callback surface -------------------------------------------
    # The externally reachable base URL for THIS process, e.g.
    # https://signalhire.example.com. The callback path is appended to it.
    #
    # This replaces the old EXTERNAL_CALLBACK_URL, which pointed at a *different*
    # process. That split is the reason the callback never worked: the webhook
    # landed somewhere the MCP server could not read.
    public_base_url: str = ""
    callback_path: str = "/signalhire/callback"

    # Shared secret required as a query parameter on the callback URL.
    # SignalHire does not send custom headers, so a query parameter is the only
    # authentication channel the vendor leaves available.
    callback_secret: str = ""

    # --- Durable inbox ------------------------------------------------------
    # Where the raw callback payloads and delivery state live. Must be on a
    # writable volume that survives restarts — the whole point is that a
    # payload outlives the process that received it.
    data_dir: Path = Path("/var/lib/signalhire-mcp")

    # --- Delivery worker ----------------------------------------------------
    worker_enabled: bool = True
    worker_poll_seconds: float = 2.0
    # After this many failed attempts an event stops being retried and is
    # parked as `failed`. It is never deleted: a parked event is still readable
    # and can be requeued by hand through `retry_delivery`.
    max_delivery_attempts: int = 8
    # Exponential backoff base, in seconds: delay = base * 2**(attempt-1),
    # capped by max_backoff_seconds.
    backoff_base_seconds: float = 5.0
    max_backoff_seconds: float = 900.0

    # --- MCP authentication -------------------------------------------------
    # Left unset deliberately. When this server is the one serving HTTP an unset
    # mode is refused rather than defaulted, because guessing wrong in either
    # direction is bad: assume a gateway that is not there and the tools sit on
    # an open URL; assume none and a correctly-fronted deployment fails to start.
    auth_mode: AuthMode | None = None
    auth_jwks_uri: str = ""
    auth_issuer: str = ""
    auth_audience: str = ""
    # Claim carrying the tenant identifier in a verified token. Only consulted
    # under AuthMode.JWT — see credentials/request_scoped.py for why it must
    # never come from a tool argument.
    auth_tenant_claim: str = "tenant_id"

    # --- DNS-rebinding protection ------------------------------------------
    # FastMCP 4 validates Host/Origin on HTTP. A public deployment behind a
    # proxy must list the hostnames it answers to, or legitimate traffic is
    # rejected. Comma-separated; empty means FastMCP's default.
    allowed_hosts: str = ""
    allowed_origins: str = ""

    # --- Skills -------------------------------------------------------------
    # Directories of agent skills served as MCP resources, so a connecting
    # client can learn how to drive this server without a local install.
    # Empty means "the skills/ directory bundled with this package".
    skills_dir: str = ""
    # Re-scan on every request. Useful while editing a skill; wasteful in
    # production, where the files do not change without a redeploy.
    skills_reload: bool = False

    # --- Observability ------------------------------------------------------
    log_timing: bool = True

    # --- HTTP client --------------------------------------------------------
    request_timeout: float = 30.0
    max_retries: int = 4

    @field_validator(
        "api_key", "callback_secret", "public_base_url", "allowed_hosts",
        "allowed_origins", "auth_jwks_uri", "auth_issuer", "auth_audience",
        "skills_dir", mode="before",
    )
    @classmethod
    def _unresolved_is_unset(cls, value: object) -> object:
        """An unresolved `${VAR}` means the variable is not set.

        FastMCP interpolates `${VAR}` in fastmcp.json against the process
        environment and documents that an unset variable leaves "the
        placeholder preserved as-is". There is no `${VAR:-default}` syntax —
        that was verified against the tooling, not assumed — so every optional
        variable declared in fastmcp.json arrives as its own name when unset.

        Treating that as the empty string is the only reading that matches
        reality. The downstream checks then produce messages about the actual
        problem ("SIGNALHIRE_API_KEY is not set") rather than about pydantic.
        """
        if isinstance(value, str) and _UNRESOLVED_PLACEHOLDER.match(value.strip()):
            return ""
        return value

    @field_validator("data_dir", mode="before")
    @classmethod
    def _placeholder_data_dir_falls_back(cls, value: object) -> object:
        """An unresolved placeholder must not become a directory name.

        `Path("${SIGNALHIRE_DATA_DIR}")` is a perfectly valid relative path, so
        without this the server would happily create a directory called
        `${SIGNALHIRE_DATA_DIR}` in its working directory and persist callbacks
        there — surviving restarts, but not a redeploy, and invisible to anyone
        looking in the configured location.
        """
        if isinstance(value, str) and _UNRESOLVED_PLACEHOLDER.match(value.strip()):
            return Path("/var/lib/signalhire-mcp")
        return value

    @field_validator("auth_mode", mode="before")
    @classmethod
    def _blank_auth_mode_is_unset(cls, value: object) -> object:
        """Empty or unresolved means "not configured", not "invalid".

        An unset mode is refused later by the verifier, for an HTTP listener,
        with a message explaining the three options. Failing enum validation
        here would replace that with a pydantic error that tells the operator
        nothing about what to do.
        """
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or _UNRESOLVED_PLACEHOLDER.match(stripped):
                return None
            return stripped.lower()
        return value

    @field_validator("port", mode="after")
    @classmethod
    def _valid_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return value

    def callback_url(self, tenant_id: str = "default") -> str:
        """The URL handed to SignalHire as `callbackUrl`.

        Carries the tenant so a multi-tenant deployment can route an inbound
        payload without parsing it, and the shared secret because SignalHire
        sends no custom headers.
        """
        if not self.public_base_url:
            raise RuntimeError(
                "SIGNALHIRE_PUBLIC_BASE_URL is not set. SignalHire delivers reveal "
                "results by POSTing to a publicly reachable URL; without one, every "
                "reveal is billed and then discarded. Set it to this server's "
                "external HTTPS base URL."
            )
        base = self.public_base_url.rstrip("/")
        url = f"{base}{self.callback_path}/{tenant_id}"
        if self.callback_secret:
            url = f"{url}?secret={self.callback_secret}"
        return url

    def allowed_hosts_list(self) -> list[str] | None:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()] or None

    def allowed_origins_list(self) -> list[str] | None:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()] or None


_settings: Settings | None = None


def load_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings
