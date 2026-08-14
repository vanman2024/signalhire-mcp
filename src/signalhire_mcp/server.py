"""The single server factory.

Every entrypoint calls `create_server()`. There is exactly one place a
`FastMCP` instance is constructed and exactly one place components are
registered.

Two structural bugs this design makes impossible, both present in the previous
implementation:

* Tools were registered at import time against a module-level `AppState` whose
  fields were filled in by the lifespan. Importing the module — which is what
  `fastmcp inspect`, `fastmcp run server.py:mcp` and every test do — produced a
  server whose tools would raise `AttributeError: 'NoneType'` on first call.
* The callback receiver was constructed inside the lifespan on a background
  thread, so it existed only when the server was run one specific way. Under
  stdio it bound a port nobody could reach; on FastMCP Cloud it fought the MCP
  listener for the same port.
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan as fastmcp_lifespan

from signalhire_mcp import resources as resource_module
from signalhire_mcp import routes as route_module
from signalhire_mcp import tools as tool_module
from signalhire_mcp.auth.verifier import (
    auth_is_enforced,
    build_auth_provider,
    require_callback_protection,
)
from signalhire_mcp.config import Settings, load_settings
from signalhire_mcp.credentials.base import CredentialProvider
from signalhire_mcp.credentials.env import EnvCredentialProvider
from signalhire_mcp.delivery.registry import TenantRegistry
from signalhire_mcp.delivery.worker import DeliveryWorker
from signalhire_mcp.inbox.store import InboxStore
from signalhire_mcp.logging_setup import configure_logging, get_logger
from signalhire_mcp.observability import build_middleware
from signalhire_mcp.runtime import Runtime
from signalhire_mcp.signalhire.client import SignalHireClient

logger = get_logger(__name__)

SERVER_NAME = "SignalHire"

INSTRUCTIONS = """\
Contact enrichment and prospect search through SignalHire's 900M+ profile
database, with durable handling of SignalHire's asynchronous results.

How reveals actually work, because it governs everything else:
  SignalHire bills a credit the moment you submit an identifier, and delivers
  the result later by POSTing to this server. So a reveal is never "free to
  retry" — re-submitting an identifier because the answer has not appeared yet
  spends another credit for data that is already paid for and on its way.

  Submit with reveal_contact() or batch_reveal_contacts(), then poll
  get_request_status(request_id) and read the data with
  get_enrichment_result(request_id).

Credits:
  There are two independent balances. Reveals normally draw on the
  "with contacts" pool. The "without contacts" pool is separate and is zero on
  most accounts — spending against it returns 402 even when the main balance
  has thousands, which is the most common confusing error from this API. Only
  pass without_contacts=true after check_credits(without_contacts=true)
  returns a non-zero number.

Cost control:
  search_prospects() is free and returns UIDs. Reveal only the subset you
  actually want. exclude_revealed=true skips people already paid for.

Limits:
  100 identifiers per reveal, 600 elements/minute, 3 concurrent searches.
  Search scroll cursors expire 15 seconds after they are issued.
"""


def create_server(
    settings: Settings | None = None,
    *,
    credential_provider: CredentialProvider | None = None,
    registry: TenantRegistry | None = None,
    client: SignalHireClient | None = None,
    auth_provider: Any | None = None,
    store: InboxStore | None = None,
) -> FastMCP:
    """Build a fully configured SignalHire MCP server.

    Every collaborator is injectable so tests can supply a temp-directory store
    and a stub credential provider without touching the environment or the
    network.
    """
    settings = settings or load_settings()
    configure_logging()

    store = store or InboxStore(settings.data_dir)
    registry = registry or TenantRegistry.from_env()
    credentials = credential_provider or EnvCredentialProvider(settings)
    sh_client = client or SignalHireClient(settings)

    if auth_provider is None:
        auth_provider = build_auth_provider(settings)
        require_callback_protection(settings)
        multi_tenant_capable = auth_is_enforced(settings)
    else:
        multi_tenant_capable = True

    worker = DeliveryWorker(settings, store, registry)
    runtime = Runtime(
        settings=settings,
        store=store,
        registry=registry,
        client=sh_client,
        credentials=credentials,
        worker=worker,
    )

    @fastmcp_lifespan
    async def app_lifespan(_server: FastMCP):
        # Fail here rather than on the first callback. A store that cannot be
        # written is not a degraded mode — it is silent, billed data loss.
        store.initialize()
        recovered = store.recover_stuck_events()
        await sh_client.start()

        if settings.worker_enabled:
            worker.start()

        stats = await store.stats()
        logger.info(
            "SignalHire MCP ready: tenants=%s credentials=%s auth=%s "
            "inbox(pending=%d delivered=%d failed=%d) recovered=%d",
            ",".join(registry.tenant_ids()) or "none",
            credentials.describe(),
            "per-tenant" if multi_tenant_capable else "single-tenant",
            stats["pending"], stats["delivered"], stats["failed"], recovered,
        )
        if not settings.public_base_url:
            logger.warning(
                "SIGNALHIRE_PUBLIC_BASE_URL is unset. Reveals will fail: SignalHire "
                "needs a publicly reachable callback URL, and without one every "
                "reveal is billed and then discarded."
            )

        try:
            yield {"runtime": runtime}
        finally:
            await worker.stop()
            await sh_client.aclose()
            await registry.aclose()

    mcp = FastMCP(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=_version(),
        auth=auth_provider,
        lifespan=app_lifespan,
    )

    for middleware in build_middleware(settings):
        mcp.add_middleware(middleware)

    tool_module.register(mcp, runtime)
    resource_module.register(mcp, runtime)
    route_module.register(mcp, runtime)

    _register_skills(mcp, settings)
    _mount_configured_servers(mcp)

    # Handy for tests and for anything that needs to reach inside.
    mcp.signalhire_runtime = runtime  # type: ignore[attr-defined]
    return mcp


def _register_skills(mcp: FastMCP, settings: Settings) -> None:
    """Serve agent skills as MCP resources.

    A client connecting to this server can read `skill://signalhire-enrichment/
    SKILL.md` and learn the workflow — the credit model, the two-pool 402 trap,
    how to diagnose a reveal that produced nothing — without anyone having
    installed a skill locally first. The instructions the operator wrote travel
    with the server they describe.

    The bundled directory lives inside the package so it survives a wheel
    build. `SIGNALHIRE_SKILLS_DIR` overrides it, and can point at several
    directories separated by the path separator, so a deployment can add its
    own house rules alongside the bundled ones.
    """
    from pathlib import Path

    bundled = Path(__file__).resolve().parent / "skills"
    if settings.skills_dir:
        roots = [Path(p) for p in settings.skills_dir.split(os.pathsep) if p.strip()]
        roots.append(bundled)
    else:
        roots = [bundled]

    existing = [r for r in roots if r.is_dir()]
    if not existing:
        logger.warning("No skills directory found (looked in %s); skills not served.",
                       ", ".join(str(r) for r in roots))
        return

    try:
        from fastmcp.server.providers.skills import SkillsDirectoryProvider

        mcp.add_provider(
            SkillsDirectoryProvider(roots=existing, reload=settings.skills_reload)
        )
        logger.info("Serving skills from %s", ", ".join(str(r) for r in existing))
    except Exception as exc:  # noqa: BLE001 - a skills problem must not stop startup
        logger.error("Could not register skills provider: %s", exc)


def _mount_configured_servers(mcp: FastMCP) -> None:
    """Mount other MCP servers under a namespace, if configured.

    This is composition for *discovery*: it lets an agent connected here also
    reach the ATS tools as `ats_*` without configuring a second MCP server.

    It is not how this server writes to an ATS. That happens through the
    delivery adapters, which call the remote server from the worker. The two
    are configured separately on purpose — you may well want to write to CATS
    without also exposing its 200 tools to whatever model is connected here.

    Configured as SIGNALHIRE_MOUNTS='{"ats": "http://127.0.0.1:3000/mcp/"}'.
    """
    import json
    import os

    raw = os.getenv("SIGNALHIRE_MOUNTS", "").strip()
    if not raw:
        return

    try:
        mounts = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("SIGNALHIRE_MOUNTS is not valid JSON, ignoring it: %s", exc)
        return

    if not isinstance(mounts, dict):
        logger.error("SIGNALHIRE_MOUNTS must be an object of namespace -> server URL.")
        return

    from fastmcp.server import create_proxy

    for namespace, target in mounts.items():
        try:
            mcp.mount(create_proxy(str(target)), namespace=str(namespace))
            logger.info("Mounted %s under namespace %r", target, namespace)
        except Exception as exc:  # noqa: BLE001 - a bad mount must not stop startup
            logger.error("Could not mount %s as %r: %s", target, namespace, exc)


def _version() -> str:
    from signalhire_mcp import __version__

    return __version__
