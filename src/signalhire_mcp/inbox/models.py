"""Records that survive the process.

Two record types, deliberately separate:

`RevealRequest` is written when we *submit* to SignalHire. It is the
correlation record — without it a callback arrives carrying a `Request-Id` that
means nothing to us, and there is no way to answer "which candidates did this
concern?". The previous implementation never wrote one, which is why
`get_request_status` could only ever return "unknown".

`IntegrationEvent` is written when a callback *arrives*, before it is
acknowledged. It owns the raw payload and the delivery state machine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventState(str, Enum):
    """Where a received callback is in its lifecycle.

    RECEIVED   Persisted, not yet delivered anywhere.
    DELIVERING A worker is mid-attempt. Reset to RECEIVED on startup, because a
               process that died mid-attempt leaves this state behind and
               nothing else would ever pick the event up again.
    DELIVERED  Every configured adapter reported success.
    PARTIAL    At least one adapter succeeded and at least one is still failing.
               Retries continue, but only for the adapters that have not
               succeeded — re-delivering to a healthy adapter would duplicate
               writes.
    FAILED     Retries exhausted. Parked, never deleted, requeueable by hand.
    """

    RECEIVED = "received"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    PARTIAL = "partial"
    FAILED = "failed"


class RequestState(str, Enum):
    SUBMITTED = "submitted"
    CALLBACK_RECEIVED = "callback_received"


class DeliveryAttempt(BaseModel):
    """One attempt to hand an event to one adapter."""

    adapter: str
    attempted_at: datetime = Field(default_factory=utc_now)
    ok: bool
    detail: str = ""
    duration_ms: int = 0


class AdapterProgress(BaseModel):
    """Per-adapter delivery state for one event.

    Tracked per adapter rather than per event because adapters fail
    independently. A Supabase write can succeed while an ATS write is down; on
    retry only the ATS write should be repeated.
    """

    adapter: str
    succeeded: bool = False
    attempts: int = 0
    last_error: str = ""
    last_attempt_at: datetime | None = None


class IntegrationEvent(BaseModel):
    """A SignalHire callback, persisted before it was acknowledged."""

    event_id: str
    tenant_id: str
    # SignalHire's `Request-Id` header. Absent on malformed callbacks, so it is
    # optional — the payload is still worth keeping.
    request_id: str | None = None
    correlation_id: str

    received_at: datetime = Field(default_factory=utc_now)
    state: EventState = EventState.RECEIVED

    #: The untouched callback body. Kept verbatim and forever. If normalisation
    #: has a bug, this is what makes reprocessing possible instead of
    #: re-billing the reveal.
    raw_payload: Any = None

    #: Summary counters, derived once at receipt so listing tools do not have to
    #: re-parse every payload.
    item_count: int = 0
    success_count: int = 0

    attempts: int = 0
    next_attempt_at: datetime = Field(default_factory=utc_now)
    last_error: str = ""

    adapters: dict[str, AdapterProgress] = Field(default_factory=dict)
    history: list[DeliveryAttempt] = Field(default_factory=list)

    def pending_adapters(self, configured: list[str]) -> list[str]:
        """Adapters that still need this event delivered to them."""
        return [
            name
            for name in configured
            if not self.adapters.get(name, AdapterProgress(adapter=name)).succeeded
        ]

    def schedule_retry(self, base_seconds: float, max_seconds: float) -> None:
        """Exponential backoff from the current attempt count."""
        delay = min(base_seconds * (2 ** max(self.attempts - 1, 0)), max_seconds)
        self.next_attempt_at = utc_now() + timedelta(seconds=delay)


class RevealRequest(BaseModel):
    """Correlation record for one submitted reveal."""

    request_id: str
    tenant_id: str
    correlation_id: str
    identifiers: list[str] = Field(default_factory=list)
    submitted_at: datetime = Field(default_factory=utc_now)
    state: RequestState = RequestState.SUBMITTED
    callback_url: str = ""
    without_contacts: bool = False
    #: Set when the matching callback lands, so a caller can walk from a
    #: request straight to the stored result.
    event_ids: list[str] = Field(default_factory=list)
