"""Cross-cutting middleware.

Timing only, deliberately.

Why there is no response caching
--------------------------------
Every tool here either spends money or reports the state of something that is
actively changing. `check_credits` cached for ten minutes tells an agent it has
budget it already spent. `get_request_status` cached at all defeats its only
purpose — it exists to be polled. Caching a search result would hand back
candidates a recruiter has since revealed, so `exclude_revealed` would stop
excluding them and the account would be billed twice for the same person.

Why there is no rate limiting middleware
----------------------------------------
`RateLimitingMiddleware` counts MCP messages, not SignalHire requests, and the
two do not correspond: `get_request_status` makes zero outbound calls,
`batch_reveal_contacts` makes one for a hundred people. Pacing at SignalHire's
budget (600 elements/minute) would throttle the protocol itself, including the
initialize handshake.

Pacing belongs where the outbound calls happen — `SignalHireClient._request`
already backs off on 429 with jitter and honours `Retry-After`.

Why there is no response limiting
---------------------------------
`ResponseLimitingMiddleware` truncates oversized results. `get_enrichment_result`
can legitimately return a hundred profiles, and a silently truncated one is a
candidate list with people missing from the end — which looks like a successful
result and is not.
"""

from __future__ import annotations

from typing import Any

from signalhire_mcp.config import Settings
from signalhire_mcp.logging_setup import get_logger

logger = get_logger(__name__)


def build_middleware(settings: Settings) -> list[Any]:
    """Build the middleware stack.

    Order matters: middleware runs in registration order inbound and reverse
    order outbound, so timing wraps everything and reports the true total.
    """
    stack: list[Any] = []

    if settings.log_timing:
        from fastmcp.server.middleware.timing import TimingMiddleware

        stack.append(TimingMiddleware(logger=get_logger("signalhire_mcp.timing")))

    return stack
