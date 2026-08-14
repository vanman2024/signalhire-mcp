"""SignalHire REST client.

httpx2, not httpx. FastMCP 4 replaced httpx with httpx2 across its HTTP stack,
and mixing the two means `except httpx.ConnectError` around anything that
touches FastMCP silently stops catching.

The credential is passed per call rather than held on the client. That is what
lets one process serve several tenants from one connection pool: the pool is a
property of the process, the API key is a property of the request. A client
constructed around a single key would need one pool per tenant, and would make
it easy to spend the wrong customer's credits by reaching for the wrong client.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any

from signalhire_mcp.config import Settings
from signalhire_mcp.credentials.base import SignalHireCredential
from signalhire_mcp.logging_setup import get_logger

logger = get_logger(__name__)

#: SignalHire returns 429 for both the Person API's 600 elements/minute budget
#: and the Search API's 3-concurrent limit. Both are worth waiting out.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class SignalHireError(RuntimeError):
    """A SignalHire request failed in a way the caller should see."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ApiResponse:
    status: int
    data: dict[str, Any]
    credits_left: int | None = None


class SignalHireClient:
    """Thin async client for the endpoints this server actually uses."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None

    async def start(self) -> None:
        import httpx2

        if self._client is None:
            self._client = httpx2.AsyncClient(timeout=self._settings.request_timeout)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- endpoints ----------------------------------------------------------

    async def check_credits(
        self, credential: SignalHireCredential, *, without_contacts: bool = False
    ) -> int:
        """Remaining credits in one of the two pools.

        SignalHire keeps two separate balances and the `withoutContacts` pool is
        usually zero on accounts that never bought it. Spending against an empty
        pool returns 402 even when the main balance has thousands, which is the
        single most confusing error this API produces.
        """
        params = {"withoutContacts": "true"} if without_contacts else None
        response = await self._request(
            credential, "GET", "/credits", params=params
        )
        return int(response.data.get("credits", 0))

    async def reveal(
        self,
        credential: SignalHireCredential,
        *,
        items: list[str],
        callback_url: str,
        without_contacts: bool = False,
    ) -> str:
        """Submit identifiers for reveal. Returns SignalHire's requestId.

        Credits are spent here, not when the callback arrives. That is why the
        caller must persist a correlation record before this returns — a
        requestId that is lost is a reveal that was paid for and can never be
        matched to anything.
        """
        if not items:
            raise SignalHireError("reveal() requires at least one identifier")
        if len(items) > 100:
            raise SignalHireError(
                f"SignalHire accepts at most 100 items per request; got {len(items)}. "
                "Split the batch before calling."
            )

        body: dict[str, Any] = {"items": items, "callbackUrl": callback_url}
        if without_contacts:
            body["withoutContacts"] = True

        response = await self._request(
            credential, "POST", "/candidate/search", json=body
        )
        request_id = response.data.get("requestId")
        if request_id is None:
            raise SignalHireError(
                f"SignalHire accepted the reveal but returned no requestId: {response.data}"
            )
        return str(request_id)

    async def search_by_query(
        self, credential: SignalHireCredential, criteria: dict[str, Any], *, size: int = 25
    ) -> dict[str, Any]:
        body = dict(criteria)
        body["size"] = size
        response = await self._request(
            credential, "POST", "/candidate/searchByQuery", json=body
        )
        return response.data

    async def scroll_search(
        self, credential: SignalHireCredential, request_id: int, scroll_id: str
    ) -> dict[str, Any]:
        """Next page of a search.

        `scrollId` expires after 15 seconds, so a retry that waits out a 429
        will usually find the cursor already dead. That is surfaced as a 404
        rather than retried into a confusing failure.
        """
        response = await self._request(
            credential,
            "POST",
            f"/candidate/scrollSearch/{request_id}",
            json={"scrollId": scroll_id},
            retry_on_429=False,
        )
        return response.data

    # --- transport ----------------------------------------------------------

    async def _request(
        self,
        credential: SignalHireCredential,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        retry_on_429: bool = True,
    ) -> ApiResponse:
        await self.start()
        assert self._client is not None

        url = f"{credential.base_url.rstrip('/')}{credential.api_prefix}{endpoint}"
        headers = {**credential.auth_header(), "Content-Type": "application/json"}

        last_error = ""
        for attempt in range(1, self._settings.max_retries + 1):
            try:
                response = await self._client.request(
                    method, url, json=json, params=params, headers=headers
                )
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= self._settings.max_retries:
                    raise SignalHireError(
                        f"{method} {endpoint} failed after {attempt} attempts: {last_error}"
                    ) from exc
                await self._backoff(attempt)
                continue

            credits_left = _int_or_none(response.headers.get("X-Credits-Left"))

            if response.status_code in _RETRYABLE_STATUS:
                if response.status_code == 429 and not retry_on_429:
                    raise SignalHireError(
                        "SignalHire rate limit hit and this endpoint cannot be safely "
                        "retried (the scroll cursor expires in 15 seconds). Restart the "
                        "search.",
                        status=429,
                    )
                last_error = f"HTTP {response.status_code}"
                if attempt >= self._settings.max_retries:
                    raise SignalHireError(
                        f"{method} {endpoint} still failing after {attempt} attempts: "
                        f"{last_error}",
                        status=response.status_code,
                    )
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            if response.status_code == 402:
                raise SignalHireError(
                    "SignalHire returned 402 (out of credits). Note there are two "
                    "separate pools: if this was a withoutContacts request, that pool "
                    "is likely zero even though the main balance is not.",
                    status=402,
                )
            if response.status_code == 401:
                raise SignalHireError(
                    "SignalHire rejected the API key (401).", status=401
                )
            if response.status_code == 406:
                raise SignalHireError(
                    "SignalHire rejected the request: more than 100 items.", status=406
                )
            if response.status_code >= 400:
                raise SignalHireError(
                    f"{method} {endpoint} -> HTTP {response.status_code}: "
                    f"{_excerpt(response)}",
                    status=response.status_code,
                )

            return ApiResponse(
                status=response.status_code,
                data=_json_or_empty(response),
                credits_left=credits_left,
            )

        raise SignalHireError(f"{method} {endpoint} exhausted retries: {last_error}")

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                await asyncio.sleep(min(float(retry_after), 60.0))
                return
            except ValueError:
                pass
        # Jitter, so several concurrent reveals that hit the same 429 do not all
        # come back at the same instant and trigger it again.
        delay = min(2.0 ** (attempt - 1), 30.0) * (0.5 + random.random() / 2)
        await asyncio.sleep(delay)


def _json_or_empty(response: Any) -> dict[str, Any]:
    try:
        parsed = response.json()
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _excerpt(response: Any) -> str:
    try:
        return response.text[:300]
    except Exception:  # noqa: BLE001
        return "<unreadable body>"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
