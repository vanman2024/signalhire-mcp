"""Which adapters run for which tenant.

Configured as JSON, from `SIGNALHIRE_TENANTS` or a file at
`SIGNALHIRE_TENANTS_FILE`. JSON rather than environment variables per tenant
because the shape is a list of lists — several tenants, each with several
adapters — and flattening that into `SIGNALHIRE_TENANT_3_ADAPTER_2_URL` is how
configuration becomes unreadable.

    {
      "default": {
        "adapters": [
          {"type": "webhook", "name": "staffhive",
           "url": "https://staffhive.example.com/api/webhooks/signalhire/relay",
           "secret_env": "STAFFHIVE_RELAY_SECRET"}
        ]
      },
      "acme": {
        "signalhire_api_key_env": "ACME_SIGNALHIRE_KEY",
        "adapters": [
          {"type": "webhook", "name": "staffhive", "url": "...",
           "secret_env": "STAFFHIVE_RELAY_SECRET"},
          {"type": "mcp", "name": "cats", "server": "http://127.0.0.1:3000/mcp/",
           "tool": "upsert_candidate_from_enrichment",
           "auth_token_env": "CATS_MCP_TOKEN"}
        ]
      }
    }

Secrets are referenced by environment variable name (`*_env`), never inlined.
A config file with an API key in it ends up in a backup, a log, or a paste.

An unknown tenant is an error, not a fallback to `default`. Falling back would
mean a typo in a tenant id silently routes one customer's revealed contacts
into another customer's system.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from signalhire_mcp.delivery.base import Adapter
from signalhire_mcp.delivery.mcp_adapter import McpAdapter
from signalhire_mcp.delivery.webhook import WebhookAdapter
from signalhire_mcp.logging_setup import get_logger

logger = get_logger(__name__)

# FastMCP leaves `${VAR}` intact in fastmcp.json when the variable is unset, and
# there is no `${VAR:-default}` syntax — verified against the tooling, not
# assumed. So an unconfigured SIGNALHIRE_TENANTS arrives as its own name, which
# is not JSON. Treat it as unconfigured rather than reporting a parse error that
# names a line and column in a value the operator never wrote.
_UNRESOLVED_PLACEHOLDER = re.compile(r"^\$\{[^}]*\}$")


def _unset_if_placeholder(value: str) -> str:
    stripped = (value or "").strip()
    return "" if _UNRESOLVED_PLACEHOLDER.match(stripped) else stripped


class TenantConfigError(RuntimeError):
    """The tenant configuration is malformed or refers to a missing secret."""


class UnknownTenantError(RuntimeError):
    """A callback or tool call named a tenant that is not configured."""


class TenantRegistry:
    """Resolves a tenant id to its adapters and its SignalHire key."""

    def __init__(self, tenants: dict[str, dict[str, Any]]) -> None:
        self._raw = tenants
        self._adapters: dict[str, list[Adapter]] = {}

    # --- construction -------------------------------------------------------

    @classmethod
    def from_env(cls, *, default_adapters: list[dict[str, Any]] | None = None) -> TenantRegistry:
        raw = _unset_if_placeholder(os.getenv("SIGNALHIRE_TENANTS", ""))
        path = _unset_if_placeholder(os.getenv("SIGNALHIRE_TENANTS_FILE", ""))

        if path:
            try:
                raw = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                raise TenantConfigError(
                    f"SIGNALHIRE_TENANTS_FILE={path} could not be read: {exc}"
                ) from exc

        if not raw:
            # Single-tenant deployment. Still goes through the same code path,
            # so the multi-tenant case is not a separate, less-tested branch.
            return cls({"default": {"adapters": default_adapters or []}})

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TenantConfigError(f"Tenant configuration is not valid JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise TenantConfigError(
                "Tenant configuration must be a JSON object keyed by tenant id."
            )
        return cls(parsed)

    # --- lookup -------------------------------------------------------------

    def tenant_ids(self) -> list[str]:
        return sorted(self._raw)

    def knows(self, tenant_id: str) -> bool:
        return tenant_id in self._raw

    def require(self, tenant_id: str) -> dict[str, Any]:
        try:
            return self._raw[tenant_id]
        except KeyError:
            raise UnknownTenantError(
                f"Unknown tenant {tenant_id!r}. Configured: {', '.join(self.tenant_ids()) or 'none'}. "
                "Refusing to fall back to the default tenant — that would file one "
                "customer's revealed contacts under another."
            ) from None

    def api_key_for(self, tenant_id: str) -> str | None:
        """Per-tenant SignalHire key, if this tenant brings its own (BYOK).

        None means "use the process-wide credential", which is the agency model:
        one key, many customers, spend attributed by this server because
        SignalHire has no sub-account model to do it for us.
        """
        config = self.require(tenant_id)
        env_name = config.get("signalhire_api_key_env")
        if not env_name:
            return None
        key = os.getenv(str(env_name), "")
        if not key:
            raise TenantConfigError(
                f"Tenant {tenant_id!r} declares signalhire_api_key_env="
                f"{env_name!r} but that variable is empty in this process."
            )
        return key

    def adapters_for(self, tenant_id: str) -> list[Adapter]:
        """Build (once) and return this tenant's delivery adapters."""
        if tenant_id in self._adapters:
            return self._adapters[tenant_id]

        config = self.require(tenant_id)
        specs = config.get("adapters") or []
        if not isinstance(specs, list):
            raise TenantConfigError(f"Tenant {tenant_id!r}: 'adapters' must be a list.")

        built = [_build_adapter(tenant_id, spec) for spec in specs]
        names = [a.name for a in built]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise TenantConfigError(
                f"Tenant {tenant_id!r} has duplicate adapter names: {sorted(duplicates)}. "
                "Names key the per-adapter delivery state, so duplicates would "
                "make one adapter's success mark another's."
            )

        self._adapters[tenant_id] = built
        return built

    async def aclose(self) -> None:
        for adapters in self._adapters.values():
            for adapter in adapters:
                try:
                    await adapter.aclose()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Adapter %s failed to close: %s", adapter.name, exc)


def _build_adapter(tenant_id: str, spec: Any) -> Adapter:
    if not isinstance(spec, dict):
        raise TenantConfigError(f"Tenant {tenant_id!r}: each adapter must be an object.")

    kind = str(spec.get("type") or "").lower()
    name = str(spec.get("name") or kind or "adapter")

    if kind == "webhook":
        url = spec.get("url")
        if not url:
            raise TenantConfigError(f"Tenant {tenant_id!r} adapter {name!r}: 'url' is required.")
        return WebhookAdapter(
            name,
            url=str(url),
            secret=_secret(spec.get("secret_env")),
            secret_header=str(spec.get("secret_header") or "X-SignalHire-Relay-Secret"),
            timeout=float(spec.get("timeout") or 30.0),
            headers=dict(spec.get("headers") or {}),
        )

    if kind == "mcp":
        server = spec.get("server")
        tool = spec.get("tool")
        if not server or not tool:
            raise TenantConfigError(
                f"Tenant {tenant_id!r} adapter {name!r}: 'server' and 'tool' are required."
            )
        return McpAdapter(
            name,
            server=str(server),
            tool=str(tool),
            argument=str(spec.get("argument") or "profile"),
            static_arguments=dict(spec.get("arguments") or {}),
            auth_token=_secret(spec.get("auth_token_env")),
            timeout=float(spec.get("timeout") or 60.0),
            skip_unsuccessful=bool(spec.get("skip_unsuccessful", True)),
        )

    raise TenantConfigError(
        f"Tenant {tenant_id!r} adapter {name!r}: unknown type {kind!r}. "
        "Supported: 'webhook', 'mcp'."
    )


def _secret(env_name: Any) -> str | None:
    if not env_name:
        return None
    value = os.getenv(str(env_name), "")
    if not value:
        raise TenantConfigError(
            f"Adapter references {env_name!r} for a secret, but that environment "
            "variable is empty in this process."
        )
    return value
