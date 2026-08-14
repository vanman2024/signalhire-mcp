"""The durable inbox — file-backed, atomic, restart-safe.

Why a file store and not a database
-----------------------------------
The requirement is that a callback survives the process that received it. A
single-host deployment gets that from the filesystem, with no service to
operate, no connection pool to exhaust at exactly the moment a webhook arrives,
and no network dependency on the one code path that must not fail. Redis and
Postgres both add a remote call *inside* the 10-second acknowledgement budget,
which is the opposite of what this path needs.

The tradeoff is real and worth stating: this store is correct for one writer
process. Running two replicas against the same directory would let both claim
the same event. If this ever needs to scale horizontally, `InboxStore` is the
one class to reimplement — nothing above it knows how storage works.

On-disk layout
--------------
    {data_dir}/events/pending/{event_id}.json   received | delivering | partial
    {data_dir}/events/done/{event_id}.json      delivered
    {data_dir}/events/failed/{event_id}.json    parked after exhausting retries
    {data_dir}/requests/{request_id}.json       correlation records

State changes are a rename between directories, so the worker's poll only ever
walks the pending set. Scanning one flat directory would get slower with every
successful reveal, which is a strange property for a system whose success case
is the common one.

Every write is tmp-file + `os.replace`, which is atomic on POSIX and on Windows
for same-volume replaces. A process killed mid-write leaves either the old
record or the new one, never a truncated file that fails to parse on restart.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

from signalhire_mcp.inbox.models import (
    AdapterProgress,
    DeliveryAttempt,
    EventState,
    IntegrationEvent,
    RequestState,
    RevealRequest,
    utc_now,
)
from signalhire_mcp.logging_setup import get_logger

logger = get_logger(__name__)

_PENDING_STATES = {EventState.RECEIVED, EventState.DELIVERING, EventState.PARTIAL}


class InboxStore:
    """Durable storage for received callbacks and submitted requests."""

    def __init__(self, data_dir: Path) -> None:
        self._root = Path(data_dir)
        self._pending = self._root / "events" / "pending"
        self._done = self._root / "events" / "done"
        self._failed = self._root / "events" / "failed"
        self._requests = self._root / "requests"
        self._lock = asyncio.Lock()

    # --- lifecycle ----------------------------------------------------------

    def initialize(self) -> None:
        """Create the directory tree and fail loudly if it is not writable.

        Called at startup rather than on first write. A permissions problem
        discovered when the first callback arrives means that callback is
        already lost; discovered at boot it just stops the process.
        """
        for path in (self._pending, self._done, self._failed, self._requests):
            path.mkdir(parents=True, exist_ok=True)

        probe = self._root / ".write-probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"Inbox directory {self._root} is not writable: {exc}. SignalHire "
                "callbacks must be persisted before they are acknowledged; without "
                "a writable directory every reveal would be billed and then lost."
            ) from exc

    def recover_stuck_events(self) -> int:
        """Reset events left in DELIVERING by a process that died mid-attempt.

        Without this they sit in the pending directory in a state the worker
        skips, so they are never retried and never parked — invisible loss that
        looks exactly like success.
        """
        recovered = 0
        for path in self._pending.glob("*.json"):
            try:
                event = IntegrationEvent.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.error("Unreadable event file %s: %s", path.name, exc)
                continue
            if event.state is EventState.DELIVERING:
                event.state = EventState.RECEIVED
                event.next_attempt_at = utc_now()
                self._write_atomic(path, event.model_dump_json(indent=2))
                recovered += 1
        if recovered:
            logger.warning(
                "Recovered %d event(s) stranded in DELIVERING by a previous shutdown.",
                recovered,
            )
        return recovered

    # --- the acknowledgement path ------------------------------------------

    async def record_event(
        self,
        *,
        tenant_id: str,
        request_id: str | None,
        correlation_id: str,
        raw_payload: object,
    ) -> IntegrationEvent:
        """Persist a callback. This runs *before* the 200 goes back to SignalHire.

        Everything here is deliberately cheap: no network, no parsing beyond
        counting items, no adapter work. SignalHire allows 10 seconds and then
        retries three times before discarding the payload permanently, so this
        function existing at all is what turns "the webhook fired and nothing
        happened" into a recoverable condition.
        """
        item_count, success_count = _summarize(raw_payload)
        event = IntegrationEvent(
            event_id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            raw_payload=raw_payload,
            item_count=item_count,
            success_count=success_count,
        )
        path = self._pending / f"{event.event_id}.json"
        await asyncio.to_thread(self._write_atomic, path, event.model_dump_json(indent=2))
        return event

    async def link_event_to_request(self, request_id: str, event_id: str) -> None:
        """Best-effort back-reference from the correlation record to the payload.

        Best-effort on purpose: a callback for an unknown request is still worth
        keeping. SignalHire will happily deliver results for a request submitted
        by a previous deployment, and refusing those would discard real data to
        preserve a foreign key.
        """
        async with self._lock:
            record = await asyncio.to_thread(self._read_request, request_id)
            if record is None:
                return
            if event_id not in record.event_ids:
                record.event_ids.append(event_id)
            record.state = RequestState.CALLBACK_RECEIVED
            await asyncio.to_thread(
                self._write_atomic,
                self._requests / f"{_safe(request_id)}.json",
                record.model_dump_json(indent=2),
            )

    # --- correlation records ------------------------------------------------

    async def record_request(self, request: RevealRequest) -> None:
        await asyncio.to_thread(
            self._write_atomic,
            self._requests / f"{_safe(request.request_id)}.json",
            request.model_dump_json(indent=2),
        )

    async def get_request(self, request_id: str) -> RevealRequest | None:
        return await asyncio.to_thread(self._read_request, request_id)

    async def list_requests(self, limit: int = 20) -> list[RevealRequest]:
        def _load() -> list[RevealRequest]:
            records: list[RevealRequest] = []
            for path in sorted(
                self._requests.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )[:limit]:
                try:
                    records.append(
                        RevealRequest.model_validate_json(path.read_text(encoding="utf-8"))
                    )
                except (OSError, ValueError):
                    continue
            return records

        return await asyncio.to_thread(_load)

    # --- worker interface ---------------------------------------------------

    async def claim_due_events(self, limit: int = 10) -> list[IntegrationEvent]:
        """Return events whose retry time has arrived, marking them DELIVERING.

        Claiming under the lock is what stops the worker from picking up the
        same event twice if a poll overruns the poll interval.
        """
        async with self._lock:
            return await asyncio.to_thread(self._claim_due_sync, limit)

    def _claim_due_sync(self, limit: int) -> list[IntegrationEvent]:
        now = utc_now()
        claimed: list[IntegrationEvent] = []
        for path in sorted(self._pending.glob("*.json")):
            if len(claimed) >= limit:
                break
            try:
                event = IntegrationEvent.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.error("Unreadable event file %s: %s", path.name, exc)
                continue
            if event.state is EventState.DELIVERING:
                continue
            if event.state not in _PENDING_STATES:
                continue
            if event.next_attempt_at > now:
                continue
            event.state = EventState.DELIVERING
            self._write_atomic(path, event.model_dump_json(indent=2))
            claimed.append(event)
        return claimed

    async def save_event(self, event: IntegrationEvent) -> None:
        """Persist an event and file it under the directory its state implies."""
        async with self._lock:
            await asyncio.to_thread(self._save_event_sync, event)

    def _save_event_sync(self, event: IntegrationEvent) -> None:
        target_dir = {
            EventState.DELIVERED: self._done,
            EventState.FAILED: self._failed,
        }.get(event.state, self._pending)

        destination = target_dir / f"{event.event_id}.json"
        self._write_atomic(destination, event.model_dump_json(indent=2))

        # Remove the record from wherever it used to live.
        for directory in (self._pending, self._done, self._failed):
            if directory == target_dir:
                continue
            stale = directory / f"{event.event_id}.json"
            if stale.exists():
                try:
                    stale.unlink()
                except OSError as exc:
                    logger.error("Could not remove stale record %s: %s", stale, exc)

    def record_attempt(
        self, event: IntegrationEvent, adapter: str, ok: bool, detail: str, duration_ms: int
    ) -> None:
        """Fold one adapter result into the event. Pure in-memory; caller saves."""
        progress = event.adapters.get(adapter) or AdapterProgress(adapter=adapter)
        progress.attempts += 1
        progress.succeeded = ok
        progress.last_error = "" if ok else detail
        progress.last_attempt_at = utc_now()
        event.adapters[adapter] = progress

        event.history.append(
            DeliveryAttempt(
                adapter=adapter, ok=ok, detail=detail[:2000], duration_ms=duration_ms
            )
        )
        # History is unbounded otherwise, and a permanently failing adapter
        # would grow the file until the disk filled.
        if len(event.history) > 50:
            event.history = event.history[-50:]

    async def get_event(self, event_id: str) -> IntegrationEvent | None:
        def _load() -> IntegrationEvent | None:
            for directory in (self._pending, self._done, self._failed):
                path = directory / f"{_safe(event_id)}.json"
                if path.exists():
                    try:
                        return IntegrationEvent.model_validate_json(
                            path.read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError):
                        return None
            return None

        return await asyncio.to_thread(_load)

    async def find_events_for_request(self, request_id: str) -> list[IntegrationEvent]:
        def _load() -> list[IntegrationEvent]:
            found: list[IntegrationEvent] = []
            for directory in (self._pending, self._done, self._failed):
                for path in directory.glob("*.json"):
                    try:
                        event = IntegrationEvent.model_validate_json(
                            path.read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError):
                        continue
                    if event.request_id == request_id:
                        found.append(event)
            return found

        return await asyncio.to_thread(_load)

    async def list_events(
        self, state: EventState | None = None, limit: int = 20
    ) -> list[IntegrationEvent]:
        def _load() -> list[IntegrationEvent]:
            directories = (
                [self._pending, self._done, self._failed]
                if state is None
                else [
                    {
                        EventState.DELIVERED: self._done,
                        EventState.FAILED: self._failed,
                    }.get(state, self._pending)
                ]
            )
            events: list[IntegrationEvent] = []
            for directory in directories:
                for path in directory.glob("*.json"):
                    try:
                        event = IntegrationEvent.model_validate_json(
                            path.read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError):
                        continue
                    if state is not None and event.state is not state:
                        continue
                    events.append(event)
            events.sort(key=lambda e: e.received_at, reverse=True)
            return events[:limit]

        return await asyncio.to_thread(_load)

    async def requeue(self, event_id: str) -> IntegrationEvent | None:
        """Move a parked event back into the retry rotation.

        Resets the attempt counter, because the operator requeuing it has
        presumably fixed whatever was broken; carrying the old count forward
        would park it again after one failure.
        """
        event = await self.get_event(event_id)
        if event is None:
            return None
        event.state = EventState.RECEIVED
        event.attempts = 0
        event.next_attempt_at = utc_now()
        event.last_error = ""
        for progress in event.adapters.values():
            if not progress.succeeded:
                progress.attempts = 0
        await self.save_event(event)
        return event

    async def stats(self) -> dict[str, int]:
        def _count() -> dict[str, int]:
            return {
                "pending": _count_json(self._pending),
                "delivered": _count_json(self._done),
                "failed": _count_json(self._failed),
                "requests": _count_json(self._requests),
            }

        return await asyncio.to_thread(_count)

    # --- primitives ---------------------------------------------------------

    def _read_request(self, request_id: str) -> RevealRequest | None:
        path = self._requests / f"{_safe(request_id)}.json"
        if not path.exists():
            return None
        try:
            return RevealRequest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            # fsync so the record survives a host power loss, not merely a
            # process crash. A callback that only reached the page cache is
            # exactly as lost as one never written.
            os.fsync(handle.fileno())
        os.replace(tmp, path)


def _count_json(directory: Path) -> int:
    try:
        return sum(1 for _ in directory.glob("*.json"))
    except OSError:
        return 0


def _safe(value: str) -> str:
    """Make an externally supplied id safe to use as a filename.

    `request_id` comes off an HTTP header, so it is attacker-controlled. Without
    this, a `Request-Id` of `../../etc/cron.d/x` would let a caller choose where
    this process writes.
    """
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)[:128] or "unknown"


def _summarize(payload: object) -> tuple[int, int]:
    """Count items and successes without trusting the payload's shape."""
    if not isinstance(payload, list):
        return (0, 0)
    total = len(payload)
    ok = 0
    for item in payload:
        if isinstance(item, dict) and item.get("status") == "success":
            ok += 1
    return (total, ok)


__all__ = ["InboxStore"]
