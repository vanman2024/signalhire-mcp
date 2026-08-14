"""The MCP tool surface.

Curated business capabilities, not a one-to-one wrapper over SignalHire's REST
endpoints. An agent should be able to enrich a person without orchestrating
four calls and a polling loop.

Two things that changed from the previous surface and are worth knowing:

* `get_request_status` used to be unable to return anything but "unknown",
  because nothing ever wrote a correlation record and the cache it consulted
  was keyed by candidate uid while it looked up by request id. It now reads the
  durable inbox and can answer truthfully.
* `export_results` was a stub that returned the string "to be implemented". It
  is gone. A tool that reports success without doing anything is worse than a
  missing tool, because the agent believes it.

Client-side logging (`ctx.info`) is deliberately not used. It is deprecated as
of the 2026-07-28 protocol (SEP-2577), and on a sessionless connection there is
no back-channel to deliver it. Operational detail goes to the server log, where
an operator can actually find it; the caller gets it in the return value.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from signalhire_mcp.auth.claims import tenant_from_context
from signalhire_mcp.delivery.normalize import normalize_payload
from signalhire_mcp.inbox.models import EventState, RevealRequest
from signalhire_mcp.logging_setup import get_logger, new_correlation_id, set_run_id
from signalhire_mcp.runtime import Runtime
from signalhire_mcp.signalhire.client import SignalHireError

logger = get_logger(__name__)

_MAX_BATCH = 100


def register(mcp, runtime: Runtime) -> None:
    """Register every tool against the given runtime."""

    settings = runtime.settings

    def _tenant() -> str:
        """The tenant for the current request.

        Read from verified claims when this server verifies tokens, otherwise
        the single configured tenant. Never a tool argument — a model-supplied
        tenant id would let any caller spend another customer's credits.
        """
        claimed = tenant_from_context(None, settings.auth_tenant_claim)
        return claimed or "default"

    # -- reveal ------------------------------------------------------------

    @mcp.tool
    async def reveal_contact(
        identifier: Annotated[
            str, Field(description="LinkedIn URL, email address, phone number, or 32-char UID")
        ],
        without_contacts: Annotated[
            bool,
            Field(
                description=(
                    "Leave false. True draws on a separate 'without contacts' credit "
                    "pool that is empty on most accounts and returns 402 even when the "
                    "main balance is healthy. Check check_credits(without_contacts=true) first."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Reveal contact details for one person.

        Asynchronous by nature: SignalHire bills the credit now and POSTs the
        result to this server's callback endpoint later. The returned
        request_id is durable — poll it with get_request_status().
        """
        return await _submit_reveal([identifier], without_contacts)

    @mcp.tool
    async def batch_reveal_contacts(
        identifiers: Annotated[
            list[str], Field(description="Up to 100 LinkedIn URLs, emails, phones, or UIDs")
        ],
        without_contacts: Annotated[
            bool, Field(description="Leave false. See reveal_contact().")
        ] = False,
    ) -> dict[str, Any]:
        """Reveal contact details for several people in one request.

        Costs one credit per identifier, charged at submission. Results arrive
        together on the callback endpoint.
        """
        if isinstance(identifiers, str):
            # Some clients serialise list arguments as a JSON string.
            import json

            try:
                identifiers = json.loads(identifiers)
            except ValueError:
                raise ValueError(
                    "identifiers must be a list of strings"
                ) from None
        if len(identifiers) > _MAX_BATCH:
            raise ValueError(
                f"SignalHire accepts at most {_MAX_BATCH} identifiers per request; "
                f"got {len(identifiers)}. Split the batch — this tool does not split "
                "silently, because a partial submission that looks complete is how "
                "people lose track of what they paid for."
            )
        return await _submit_reveal(identifiers, without_contacts)

    async def _submit_reveal(
        identifiers: list[str], without_contacts: bool
    ) -> dict[str, Any]:
        correlation_id = new_correlation_id()
        set_run_id(correlation_id)

        tenant = _tenant()
        credential = await runtime.credential_for(tenant)
        callback_url = settings.callback_url(tenant)

        request_id = await runtime.client.reveal(
            credential,
            items=identifiers,
            callback_url=callback_url,
            without_contacts=without_contacts,
        )

        # Persist the correlation record before returning. The credit is
        # already spent at this point; a request_id that only exists in the
        # response is one restart away from being unmatchable.
        await runtime.store.record_request(
            RevealRequest(
                request_id=request_id,
                tenant_id=tenant,
                correlation_id=correlation_id,
                identifiers=identifiers,
                callback_url=callback_url.split("?")[0],
                without_contacts=without_contacts,
            )
        )

        logger.info(
            "Reveal submitted: tenant=%s request_id=%s items=%d",
            tenant, request_id, len(identifiers),
        )

        return {
            "request_id": request_id,
            "status": "submitted",
            "identifiers": len(identifiers),
            "credits_charged": len(identifiers),
            "correlation_id": correlation_id,
            "next": "Poll get_request_status(request_id) — results arrive by webhook.",
        }

    # -- status ------------------------------------------------------------

    @mcp.tool
    async def get_request_status(
        request_id: Annotated[str, Field(description="request_id from a reveal call")],
    ) -> dict[str, Any]:
        """Check whether a reveal's results have arrived and been delivered.

        Reads the durable inbox, so it answers correctly across restarts and
        reports where a delivery failed rather than only whether it finished.
        """
        record = await runtime.store.get_request(request_id)
        events = await runtime.store.find_events_for_request(request_id)

        if record is None and not events:
            return {
                "request_id": request_id,
                "status": "unknown",
                "detail": (
                    "No record of this request. Either it was submitted by a different "
                    "deployment, or it predates the durable inbox."
                ),
            }

        if not events:
            return {
                "request_id": request_id,
                "status": "awaiting_callback",
                "submitted_at": record.submitted_at.isoformat() if record else None,
                "identifiers": record.identifiers if record else [],
                "detail": "Submitted to SignalHire; the callback has not arrived yet.",
            }

        return {
            "request_id": request_id,
            "status": "callback_received",
            "submitted_at": record.submitted_at.isoformat() if record else None,
            "events": [
                {
                    "event_id": e.event_id,
                    "received_at": e.received_at.isoformat(),
                    "delivery_state": e.state.value,
                    "items": e.item_count,
                    "successful": e.success_count,
                    "attempts": e.attempts,
                    "last_error": e.last_error or None,
                    "adapters": {
                        name: {"succeeded": p.succeeded, "attempts": p.attempts,
                               "last_error": p.last_error or None}
                        for name, p in e.adapters.items()
                    },
                }
                for e in events
            ],
        }

    @mcp.tool
    async def get_enrichment_result(
        request_id: Annotated[
            str, Field(description="request_id whose revealed profiles you want")
        ],
    ) -> dict[str, Any]:
        """Return the revealed profiles for a request, normalised.

        This is the payoff of persisting callbacks: the data is readable
        directly, without asking SignalHire again and paying a second time.
        """
        events = await runtime.store.find_events_for_request(request_id)
        if not events:
            return {
                "request_id": request_id,
                "status": "no_results",
                "detail": "No callback has been stored for this request.",
            }

        profiles = []
        for event in events:
            profiles.extend(normalize_payload(event.raw_payload))

        return {
            "request_id": request_id,
            "status": "ok",
            "count": len(profiles),
            "successful": sum(1 for p in profiles if p.succeeded),
            "profiles": [p.model_dump(mode="json") for p in profiles],
        }

    @mcp.tool
    async def list_requests(
        limit: Annotated[int, Field(ge=1, le=100, description="How many to return")] = 20,
    ) -> dict[str, Any]:
        """List recently submitted reveal requests."""
        records = await runtime.store.list_requests(limit=limit)
        return {
            "count": len(records),
            "requests": [
                {
                    "request_id": r.request_id,
                    "tenant_id": r.tenant_id,
                    "submitted_at": r.submitted_at.isoformat(),
                    "state": r.state.value,
                    "identifiers": len(r.identifiers),
                    "event_ids": r.event_ids,
                }
                for r in records
            ],
        }

    # -- operations --------------------------------------------------------

    @mcp.tool
    async def list_failed_deliveries(
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """List callbacks that arrived but could not be delivered downstream.

        These are the ones that would previously have vanished into a log line.
        The payload is still on disk; retry_delivery() puts them back in the queue.
        """
        events = await runtime.store.list_events(state=EventState.FAILED, limit=limit)
        return {
            "count": len(events),
            "events": [
                {
                    "event_id": e.event_id,
                    "tenant_id": e.tenant_id,
                    "request_id": e.request_id,
                    "received_at": e.received_at.isoformat(),
                    "attempts": e.attempts,
                    "items": e.item_count,
                    "last_error": e.last_error,
                    "failing_adapters": [
                        n for n, p in e.adapters.items() if not p.succeeded
                    ],
                }
                for e in events
            ],
        }

    @mcp.tool
    async def retry_delivery(
        event_id: Annotated[str, Field(description="event_id from list_failed_deliveries()")],
    ) -> dict[str, Any]:
        """Put a parked callback back into the delivery queue.

        Resets the attempt counter, on the assumption that whoever asked for
        the retry fixed the underlying problem first.
        """
        event = await runtime.store.requeue(event_id)
        if event is None:
            raise ValueError(f"No stored event with id {event_id!r}")
        runtime.worker.nudge()
        return {
            "event_id": event.event_id,
            "state": event.state.value,
            "detail": "Requeued; the worker will retry shortly.",
        }

    @mcp.tool
    async def check_credits(
        without_contacts: Annotated[
            bool,
            Field(
                description=(
                    "False (default) reads the main balance. True reads the separate "
                    "'without contacts' pool, which is zero on most accounts."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Remaining SignalHire credits.

        SignalHire keeps two independent balances. A 402 on a reveal almost
        always means the *other* pool was used, not that the account is empty.
        """
        credential = await runtime.credential_for(_tenant())
        credits = await runtime.client.check_credits(
            credential, without_contacts=without_contacts
        )
        return {
            "credits": credits,
            "pool": "without_contacts" if without_contacts else "with_contacts",
            "tenant_id": credential.tenant_id,
        }

    # -- search ------------------------------------------------------------

    @mcp.tool
    async def search_prospects(
        title: Annotated[
            str | None, Field(description="Current job title. Boolean: AND, OR, NOT, ()")
        ] = None,
        location: Annotated[
            list[str] | None, Field(description="One or more cities, states, or countries")
        ] = None,
        company: Annotated[str | None, Field(description="Current company. Boolean supported")] = None,
        keywords: Annotated[
            str | None, Field(description="Skills, education, bio. Boolean supported")
        ] = None,
        years_experience_from: Annotated[int | None, Field(ge=0)] = None,
        years_experience_to: Annotated[int | None, Field(ge=0, le=50)] = None,
        open_to_work: Annotated[bool | None, Field(description="Only job seekers")] = None,
        exclude_revealed: Annotated[
            bool | None, Field(description="Skip profiles already revealed — saves credits")
        ] = None,
        size: Annotated[int, Field(ge=1, le=100, description="Results per page")] = 25,
    ) -> dict[str, Any]:
        """Search the SignalHire database. Free — consumes no credits.

        Returns profile summaries and UIDs only. Feed those UIDs to
        batch_reveal_contacts() to get contact details, which does cost credits.
        """
        criteria: dict[str, Any] = {}
        if title:
            criteria["currentTitle"] = title
        if location:
            criteria["location"] = location
        if company:
            criteria["currentCompany"] = company
        if keywords:
            criteria["keywords"] = keywords
        if years_experience_from is not None:
            criteria["yearsOfCurrentExperienceFrom"] = years_experience_from
        if years_experience_to is not None:
            criteria["yearsOfCurrentExperienceTo"] = years_experience_to
        if open_to_work is not None:
            criteria["openToWork"] = open_to_work
        if exclude_revealed is not None:
            criteria["excludeRevealed"] = exclude_revealed

        if not criteria:
            raise ValueError(
                "Give at least one search filter. An unfiltered search is rejected by "
                "SignalHire, and exclude-only filters must be combined with a real one."
            )

        credential = await runtime.credential_for(_tenant())
        data = await runtime.client.search_by_query(credential, criteria, size=size)

        return {
            "total": data.get("total", 0),
            "returned": len(data.get("profiles", [])),
            "profiles": data.get("profiles", []),
            "request_id": data.get("requestId"),
            "scroll_id": data.get("scrollId"),
            "note": (
                "scroll_id expires 15 seconds after this response. Call "
                "scroll_search_results() immediately or restart the search."
            ),
        }

    @mcp.tool
    async def scroll_search_results(
        request_id: Annotated[str, Field(description="requestId from search_prospects()")],
        scroll_id: Annotated[str, Field(description="scroll_id from the previous page")],
    ) -> dict[str, Any]:
        """Fetch the next page of a search.

        The cursor expires 15 seconds after the previous response, so this
        cannot be retried after a rate-limit wait — it fails fast instead.
        """
        try:
            numeric = int(request_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"request_id must be the numeric id from search_prospects(); got {request_id!r}"
            ) from None

        credential = await runtime.credential_for(_tenant())
        try:
            data = await runtime.client.scroll_search(credential, numeric, scroll_id)
        except SignalHireError as exc:
            if exc.status == 404:
                raise ValueError(
                    "The scroll cursor has expired (15 second limit) or is invalid. "
                    "Restart the search."
                ) from exc
            raise

        return {
            "returned": len(data.get("profiles", [])),
            "profiles": data.get("profiles", []),
            "scroll_id": data.get("scrollId"),
            "has_more": bool(data.get("scrollId")),
        }
