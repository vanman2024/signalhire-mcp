"""The delivery worker.

Drains the durable inbox and hands each event to that tenant's adapters,
recording the outcome of every attempt. This is the half of the system the old
implementation had no equivalent of: previously a failed write was a
`console.error` and a `continue`, so a Supabase outage during a batch of
reveals meant those candidates were simply never enriched and nothing anywhere
recorded that fact.

Why a plain asyncio task and not `fastmcp[tasks]`
-------------------------------------------------
`TasksExtension` solves a different problem: letting an MCP *client* start a
long tool call and poll it. Its work is initiated by a client request and its
default backend is in-memory, so tasks are lost on restart unless Redis is
added. This work is initiated by a webhook, must outlive any client
connection, and must survive restart — which is what the file-backed inbox
already provides. Wiring it through the tasks extension would add a dependency
and a durability gap in exchange for nothing.

Partial success
---------------
Adapters are tracked independently. If StaffHive accepts an event and the ATS
is down, the next attempt retries only the ATS. Re-delivering to StaffHive
would create duplicate records for a write that already succeeded — the exact
failure mode idempotency requirements exist to prevent, and one that is easy to
introduce by treating an event as a single unit.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from signalhire_mcp.config import Settings
from signalhire_mcp.delivery.normalize import normalize_payload
from signalhire_mcp.delivery.registry import TenantRegistry, UnknownTenantError
from signalhire_mcp.inbox.models import EventState, IntegrationEvent, utc_now
from signalhire_mcp.inbox.store import InboxStore
from signalhire_mcp.logging_setup import get_logger, set_run_id

logger = get_logger(__name__)


class DeliveryWorker:
    """Polls the inbox and delivers events through each tenant's adapters."""

    def __init__(
        self, settings: Settings, store: InboxStore, registry: TenantRegistry
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    # --- lifecycle ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="signalhire-delivery-worker")
        logger.info(
            "Delivery worker started (poll=%.1fs, max_attempts=%d)",
            self._settings.worker_poll_seconds,
            self._settings.max_delivery_attempts,
        )

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Delivery worker stopped")

    def nudge(self) -> None:
        """Wake the worker immediately.

        Called by the callback route after persisting. Without it a freshly
        received event waits for the next poll tick, which turns a sub-second
        pipeline into a multi-second one for no reason.
        """
        self._wake.set()

    # --- the loop -----------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping:
            try:
                processed = await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                logger.exception("Delivery worker iteration failed: %s", exc)
                processed = 0

            if processed:
                # More may be due immediately; do not sleep between batches.
                continue

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._settings.worker_poll_seconds
                )
            self._wake.clear()

    async def drain_once(self, limit: int = 10) -> int:
        """Deliver every event that is due. Returns how many were attempted.

        Public so tests can drive the worker deterministically instead of
        sleeping and hoping.
        """
        events = await self._store.claim_due_events(limit=limit)
        for event in events:
            await self._deliver(event)
        return len(events)

    # --- one event ----------------------------------------------------------

    async def _deliver(self, event: IntegrationEvent) -> None:
        set_run_id(event.correlation_id)
        event.attempts += 1

        try:
            adapters = self._registry.adapters_for(event.tenant_id)
        except UnknownTenantError as exc:
            # Not retryable: the tenant will not appear by waiting. Park it so
            # the payload survives for whoever fixes the configuration.
            event.state = EventState.FAILED
            event.last_error = str(exc)
            logger.error("Event %s parked: %s", event.event_id, exc)
            await self._store.save_event(event)
            return
        except Exception as exc:  # noqa: BLE001 - bad config is retryable after a fix
            event.last_error = f"adapter construction failed: {exc}"
            self._reschedule_or_park(event)
            await self._store.save_event(event)
            return

        if not adapters:
            # Nothing configured to deliver to. Mark delivered rather than
            # retrying forever — the payload is still on disk and readable.
            event.state = EventState.DELIVERED
            event.last_error = ""
            logger.warning(
                "Event %s has no adapters configured for tenant %r; stored only.",
                event.event_id,
                event.tenant_id,
            )
            await self._store.save_event(event)
            return

        profiles = normalize_payload(event.raw_payload)
        configured = [a.name for a in adapters]
        pending = set(event.pending_adapters(configured))

        for adapter in adapters:
            if adapter.name not in pending:
                continue
            started = time.monotonic()
            try:
                result = await adapter.deliver(event, profiles)
            except Exception as exc:  # noqa: BLE001 - an adapter bug must not stall the queue
                logger.exception("Adapter %s raised: %s", adapter.name, exc)
                result_ok, detail = False, f"adapter raised {type(exc).__name__}: {exc}"
            else:
                result_ok, detail = result.ok, result.detail

            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._store.record_attempt(event, adapter.name, result_ok, detail, elapsed_ms)

            if result_ok:
                logger.info(
                    "Event %s delivered to %s (%dms): %s",
                    event.event_id, adapter.name, elapsed_ms, detail,
                )
            else:
                logger.warning(
                    "Event %s delivery to %s failed (attempt %d): %s",
                    event.event_id, adapter.name, event.attempts, detail,
                )

        succeeded = [n for n in configured if event.adapters.get(n) and event.adapters[n].succeeded]
        outstanding = [n for n in configured if n not in succeeded]

        if not outstanding:
            event.state = EventState.DELIVERED
            event.last_error = ""
            logger.info("Event %s fully delivered to %s", event.event_id, ", ".join(configured))
        else:
            event.last_error = "; ".join(
                f"{n}: {event.adapters[n].last_error}"
                for n in outstanding
                if event.adapters.get(n) and event.adapters[n].last_error
            )
            self._reschedule_or_park(event, partial=bool(succeeded))

        await self._store.save_event(event)

    def _reschedule_or_park(self, event: IntegrationEvent, partial: bool = False) -> None:
        if event.attempts >= self._settings.max_delivery_attempts:
            event.state = EventState.FAILED
            logger.error(
                "Event %s parked after %d attempts. Last error: %s",
                event.event_id, event.attempts, event.last_error,
            )
            return

        event.state = EventState.PARTIAL if partial else EventState.RECEIVED
        event.schedule_retry(
            self._settings.backoff_base_seconds, self._settings.max_backoff_seconds
        )
        logger.info(
            "Event %s retry %d/%d scheduled for %s",
            event.event_id,
            event.attempts,
            self._settings.max_delivery_attempts,
            event.next_attempt_at.isoformat(),
        )


__all__ = ["DeliveryWorker", "utc_now"]
