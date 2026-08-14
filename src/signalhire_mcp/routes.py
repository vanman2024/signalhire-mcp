"""The HTTP surface that lives alongside `/mcp/`.

This module is the fix. Everything else in this package supports it.

The old architecture ran the webhook receiver as a *separate process*: a
FastAPI app started by `uvicorn.run()` on a daemon thread, or on a different
host entirely. SignalHire's results landed in that process's memory, and the
MCP server — the thing with the tools — had no way to read them. No handler was
ever registered against the receiver either, so in practice every payload was
logged and dropped.

FastMCP custom routes remove the split. `/signalhire/callback/{tenant}` is
served by the same ASGI app as `/mcp/`, in the same process, sharing the same
store. One port, one deployment, no cross-process handoff.

Two properties this handler must have, both dictated by the vendor:

* **Answer within 10 seconds.** SignalHire retries a non-200 three times and
  then discards the payload permanently. So this handler persists and returns;
  it never normalises, never calls an adapter, never touches the network.
* **Persist before acknowledging.** Returning 200 is a promise that the data is
  safe. The serverless implementation returned 200 and then did the real work
  in `after()`, which the platform kills at the function timeout — so the
  promise was routinely false and nobody could tell.

Custom routes are deliberately outside FastMCP's auth middleware, which is
correct here: SignalHire cannot present a bearer token. The route authenticates
with a shared secret in the query string, which is the only channel the vendor
leaves open.
"""

from __future__ import annotations

import hmac

from starlette.requests import Request
from starlette.responses import JSONResponse

from signalhire_mcp.logging_setup import get_logger, new_correlation_id, set_run_id
from signalhire_mcp.runtime import Runtime

logger = get_logger(__name__)


def register(mcp, runtime: Runtime) -> None:
    """Attach the callback and health routes to the FastMCP HTTP app."""
    settings = runtime.settings
    callback_path = settings.callback_path.rstrip("/")

    @mcp.custom_route(f"{callback_path}/{{tenant_id}}", methods=["POST"])
    async def signalhire_callback(request: Request) -> JSONResponse:
        correlation_id = new_correlation_id()
        set_run_id(correlation_id)

        tenant_id = request.path_params.get("tenant_id", "default")

        # Authenticate before touching the body. Without this check an
        # unauthenticated caller could fill the disk by POSTing junk, and the
        # store would dutifully persist all of it.
        if settings.callback_secret:
            supplied = request.query_params.get("secret", "")
            if not hmac.compare_digest(supplied, settings.callback_secret):
                logger.warning(
                    "Rejected callback for tenant %r: bad or missing secret (from %s)",
                    tenant_id,
                    request.client.host if request.client else "unknown",
                )
                return JSONResponse({"error": "unauthorized"}, status_code=401)

        if not runtime.registry.knows(tenant_id):
            # 404 rather than persisting under a guessed tenant. Filing one
            # customer's revealed contacts under another is worse than losing
            # them, and SignalHire's retries make this visible rather than silent.
            logger.error(
                "Rejected callback for unknown tenant %r. Configured: %s",
                tenant_id,
                ", ".join(runtime.registry.tenant_ids()) or "none",
            )
            return JSONResponse({"error": "unknown tenant"}, status_code=404)

        request_id = request.headers.get("Request-Id")

        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            # A 400 here is deliberate: it makes SignalHire retry. If the body
            # was truncated in transit, a retry is exactly what we want.
            logger.error("Callback for tenant %r had an unparseable body: %s", tenant_id, exc)
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        try:
            event = await runtime.store.record_event(
                tenant_id=tenant_id,
                request_id=request_id,
                correlation_id=correlation_id,
                raw_payload=payload,
            )
        except Exception as exc:  # noqa: BLE001
            # Refuse the acknowledgement. A 500 buys three more attempts from
            # SignalHire, which is the only remaining chance to keep this data.
            logger.exception("Could not persist callback for tenant %r: %s", tenant_id, exc)
            return JSONResponse({"error": "could not persist"}, status_code=500)

        if request_id:
            try:
                await runtime.store.link_event_to_request(request_id, event.event_id)
            except Exception as exc:  # noqa: BLE001
                # The payload is already safe; a missing back-reference is a
                # reporting nuisance, not data loss. Never fail the ack for it.
                logger.warning("Could not link event %s to request %s: %s",
                               event.event_id, request_id, exc)

        logger.info(
            "Callback stored: tenant=%s request_id=%s event=%s items=%d success=%d",
            tenant_id, request_id, event.event_id, event.item_count, event.success_count,
        )

        # Delivery happens on the worker, after this response is on the wire.
        runtime.worker.nudge()

        return JSONResponse(
            {"status": "accepted", "event_id": event.event_id, "items": event.item_count},
            status_code=200,
        )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Liveness plus the numbers an operator actually needs.

        Reports the inbox backlog, because "the service is up" and "callbacks
        are being delivered" are different questions and only the second one
        matters to whoever is being paged.
        """
        try:
            stats = await runtime.store.stats()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"status": "degraded", "error": f"inbox unreadable: {exc}"}, status_code=503
            )

        return JSONResponse(
            {
                "status": "healthy",
                "service": "signalhire-mcp",
                "inbox": stats,
                "tenants": runtime.registry.tenant_ids(),
                "worker_running": runtime.worker.is_running,
            }
        )
