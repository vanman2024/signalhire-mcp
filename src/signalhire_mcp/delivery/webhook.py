"""Deliver an enrichment result by POSTing it to an HTTP endpoint.

This is the relay that replaces the failing serverless path. The difference is
not the transport — it is still an HTTP POST to StaffHire — but *who owns the
retry*. Previously SignalHire owned it: three attempts against a Vercel
function that had already returned 200 and then died mid-`after()`, after which
the payload was discarded permanently. Now this process owns it, backed by a
record on disk, and it can keep trying for as long as it takes.

That also removes the 10-second pressure from the receiving end. StaffHive's
handler no longer has to finish PDF generation and an ATS sync inside a webhook
timeout, because the thing waiting on it is a worker with a backoff schedule,
not a vendor with a 3-strike policy.
"""

from __future__ import annotations

import time
from typing import Any

from signalhire_mcp.delivery.base import DeliveryResult
from signalhire_mcp.delivery.normalize import RevealedProfile
from signalhire_mcp.inbox.models import IntegrationEvent
from signalhire_mcp.logging_setup import get_logger

logger = get_logger(__name__)

#: Status codes worth trying again. A 4xx other than 408/429 means the request
#: itself is wrong, and repeating it unchanged will fail identically until
#: someone edits the config — so it parks quickly instead of burning the retry
#: budget on a request that cannot succeed.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class WebhookAdapter:
    """POSTs the normalised event to a configured URL."""

    def __init__(
        self,
        name: str,
        *,
        url: str,
        secret: str | None = None,
        secret_header: str = "X-SignalHire-Relay-Secret",
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self._url = url
        self._secret = secret
        self._secret_header = secret_header
        self._timeout = timeout
        self._headers = headers or {}
        self._client: Any | None = None

    async def deliver(
        self, event: IntegrationEvent, profiles: list[RevealedProfile]
    ) -> DeliveryResult:
        body = {
            "event_id": event.event_id,
            "tenant_id": event.tenant_id,
            "request_id": event.request_id,
            "correlation_id": event.correlation_id,
            "received_at": event.received_at.isoformat(),
            "profiles": [p.model_dump(mode="json") for p in profiles],
            # The untouched vendor payload rides along so the receiver can
            # recover anything normalisation dropped without calling back here.
            "raw": event.raw_payload,
        }

        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"
        # Correlation travels with the request so a failure can be traced from
        # this server's log to the receiver's without matching on timestamps.
        headers["X-Correlation-Id"] = event.correlation_id
        if self._secret:
            headers[self._secret_header] = self._secret

        started = time.monotonic()
        try:
            client = await self._get_client()
            response = await client.post(self._url, json=body, headers=headers)
        except Exception as exc:  # noqa: BLE001 - network failures are retryable
            return DeliveryResult.failure(f"POST {self._url} failed: {type(exc).__name__}: {exc}")

        elapsed = int((time.monotonic() - started) * 1000)

        if 200 <= response.status_code < 300:
            return DeliveryResult.success(f"HTTP {response.status_code} in {elapsed}ms")

        detail = _body_excerpt(response)
        if response.status_code in _RETRYABLE_STATUS:
            return DeliveryResult.failure(
                f"HTTP {response.status_code} from {self._url} (retryable): {detail}"
            )
        return DeliveryResult.failure(
            f"HTTP {response.status_code} from {self._url} (client error — will retry, but "
            f"this usually needs a config change, not time): {detail}"
        )

    async def _get_client(self) -> Any:
        import httpx2

        if self._client is None:
            self._client = httpx2.AsyncClient(timeout=self._timeout)
        return self._client

    def describe(self) -> str:
        auth = "with secret" if self._secret else "UNAUTHENTICATED"
        return f"webhook -> {self._url} ({auth})"

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _body_excerpt(response: Any) -> str:
    try:
        return response.text[:500]
    except Exception:  # noqa: BLE001
        return "<unreadable body>"
