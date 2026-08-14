"""Resources and prompts.

Resources here are all *operational*: credit balances, inbox depth, stored
payloads. Nothing caches candidate records.

That is a deliberate line, borrowed from the CATS adapter's reasoning. A stale
answer about a person is worse than no answer: a recruiter screens a list,
someone is added to Do Not Contact, the screen runs again and the cached result
says it is still fine to call them. A faster wrong answer is not an improvement.
Balances and queue depths have no such hazard.
"""

from __future__ import annotations

import json

from signalhire_mcp.delivery.normalize import normalize_payload
from signalhire_mcp.runtime import Runtime


def register(mcp, runtime: Runtime) -> None:
    @mcp.resource("signalhire://credits")
    async def credits() -> str:
        """Both credit pools, and which one you should be spending."""
        credential = await runtime.credential_for()
        with_contacts = await runtime.client.check_credits(credential, without_contacts=False)
        try:
            without = await runtime.client.check_credits(credential, without_contacts=True)
        except Exception:  # noqa: BLE001 - the pool may not exist on this plan
            without = 0
        return json.dumps(
            {
                "with_contacts": with_contacts,
                "without_contacts": without,
                "spend_from": "with_contacts",
                "note": (
                    "Reveals default to the with_contacts pool. Only pass "
                    "without_contacts=true if the second number above is non-zero."
                ),
            },
            indent=2,
        )

    @mcp.resource("signalhire://inbox/stats")
    async def inbox_stats() -> str:
        """Queue depth: how many callbacks are stored, delivered, and parked."""
        stats = await runtime.store.stats()
        return json.dumps(
            {
                **stats,
                "worker_running": runtime.worker.is_running,
                "tenants": runtime.registry.tenant_ids(),
            },
            indent=2,
        )

    @mcp.resource("signalhire://events/{event_id}")
    async def stored_event(event_id: str) -> str:
        """One stored callback, including its raw payload and delivery history."""
        event = await runtime.store.get_event(event_id)
        if event is None:
            raise ValueError(f"No stored event with id {event_id!r}")
        profiles = normalize_payload(event.raw_payload)
        return json.dumps(
            {
                "event": event.model_dump(mode="json"),
                "profiles": [p.model_dump(mode="json") for p in profiles],
            },
            indent=2,
        )

    @mcp.resource("signalhire://requests/{request_id}")
    async def stored_request(request_id: str) -> str:
        """The correlation record for one submitted reveal."""
        record = await runtime.store.get_request(request_id)
        if record is None:
            raise ValueError(f"No stored request with id {request_id!r}")
        return json.dumps(record.model_dump(mode="json"), indent=2)

    # -- prompts -----------------------------------------------------------

    @mcp.prompt
    def enrich_people() -> str:
        """How to enrich a list of people without wasting credits."""
        return (
            "Enriching people through SignalHire:\n"
            "\n"
            "1. check_credits() — one credit is spent per identifier at submission, "
            "not on delivery, so confirm the balance covers the batch first.\n"
            "2. batch_reveal_contacts(identifiers=[...]) — up to 100 per call. It "
            "returns a request_id immediately; the data arrives later by webhook.\n"
            "3. get_request_status(request_id) — reports whether the callback landed "
            "and whether downstream delivery succeeded.\n"
            "4. get_enrichment_result(request_id) — the revealed profiles.\n"
            "\n"
            "Do not re-submit an identifier because a result has not appeared yet. "
            "The credit is already spent; re-submitting spends another. If the "
            "callback never arrives, check signalhire://inbox/stats and the server "
            "logs rather than paying twice.\n"
            "\n"
            "Never pass without_contacts=true unless check_credits(without_contacts=true) "
            "returned a non-zero number. That pool is empty on most accounts and "
            "produces a 402 that looks like the account is out of credits entirely."
        )

    @mcp.prompt
    def search_then_enrich() -> str:
        """How to go from a search to contact details."""
        return (
            "Searching costs nothing; revealing costs a credit per person. So:\n"
            "\n"
            "1. search_prospects(title=..., location=[...], exclude_revealed=true) — "
            "free. exclude_revealed skips people you already paid for.\n"
            "2. Review the returned profiles and decide who is actually worth "
            "revealing. This is the step that controls spend.\n"
            "3. batch_reveal_contacts(identifiers=[uid, uid, ...]) for that subset.\n"
            "\n"
            "Boolean operators work in title, company and keywords: "
            '\"(Welder OR Fabricator) AND NOT Apprentice\".\n'
            "\n"
            "If the search returns a scroll_id, call scroll_search_results() straight "
            "away — the cursor dies after 15 seconds."
        )

    @mcp.prompt
    def diagnose_missing_results() -> str:
        """What to check when a reveal never produced data."""
        return (
            "A reveal was submitted and nothing came back. Work down this list:\n"
            "\n"
            "1. get_request_status(request_id) — does the server have a record?\n"
            "   - 'awaiting_callback': SignalHire has not POSTed yet. Normal for a "
            "few minutes on a large batch.\n"
            "   - 'callback_received' with delivery_state 'failed': the data arrived "
            "and is stored; delivery downstream is what broke. Read last_error.\n"
            "   - 'unknown': no record. The request predates this deployment, or the "
            "submission never completed.\n"
            "2. list_failed_deliveries() — parked callbacks, with the failing adapter "
            "named. The payload is safe on disk; nothing was lost.\n"
            "3. retry_delivery(event_id) once the downstream problem is fixed.\n"
            "4. GET /health — inbox counts and whether the delivery worker is running.\n"
            "\n"
            "If SignalHire never called back at all, the usual causes are a callback "
            "URL that is not publicly reachable, a TLS certificate the vendor will not "
            "accept, or a mismatched callback secret returning 401. SignalHire retries "
            "three times and then discards permanently, so check the server log for "
            "rejected callbacks."
        )
