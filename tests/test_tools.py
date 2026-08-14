"""The MCP tool surface, exercised through an in-memory client.

This is the FastMCP-documented testing pattern: wrap the server in a `Client`
and assert on `result.data`.
"""

from __future__ import annotations

import pytest

from tests.conftest import SAMPLE_CALLBACK

EXPECTED_TOOLS = {
    "batch_reveal_contacts",
    "check_credits",
    "get_enrichment_result",
    "get_request_status",
    "list_failed_deliveries",
    "list_requests",
    "retry_delivery",
    "reveal_contact",
    "scroll_search_results",
    "search_prospects",
}


async def test_tool_surface(client):
    tools = {t.name for t in await client.list_tools()}
    assert tools == EXPECTED_TOOLS


async def test_export_results_is_gone(client):
    """It was a stub that reported success and did nothing.

    A tool that lies is worse than a missing one, because the agent believes it
    and stops looking for the data.
    """
    tools = {t.name for t in await client.list_tools()}
    assert "export_results" not in tools


async def test_reveal_records_a_correlation_record(client, store, fake_client):
    result = await client.call_tool(
        "reveal_contact", {"identifier": "https://linkedin.com/in/someone"}
    )

    assert result.data["request_id"] == "4242"
    assert result.data["credits_charged"] == 1

    record = await store.get_request("4242")
    assert record is not None, "the credit is spent; the record must survive"
    assert record.identifiers == ["https://linkedin.com/in/someone"]
    assert fake_client.reveals[0]["callback_url"].startswith("https://signalhire.test")


async def test_reveal_sends_the_tenant_scoped_callback_url(client, fake_client):
    """The URL carries both the tenant and the shared secret.

    The tenant is in the path so an inbound payload routes without parsing it;
    the secret is a query parameter because SignalHire sends no custom headers.
    """
    await client.call_tool("reveal_contact", {"identifier": "a@b.com"})
    url = fake_client.reveals[0]["callback_url"]

    assert url == "https://signalhire.test/signalhire/callback/default?secret=s3cret"


async def test_batch_over_one_hundred_is_rejected_not_split(client):
    """Silent splitting hides how much was actually submitted — and billed."""
    with pytest.raises(Exception) as exc:
        await client.call_tool(
            "batch_reveal_contacts", {"identifiers": [f"u{i}@x.com" for i in range(101)]}
        )
    assert "100" in str(exc.value)


async def test_status_for_an_unknown_request(client):
    result = await client.call_tool("get_request_status", {"request_id": "nope"})
    assert result.data["status"] == "unknown"


async def test_status_reports_awaiting_before_the_callback(client):
    await client.call_tool("reveal_contact", {"identifier": "a@b.com"})

    result = await client.call_tool("get_request_status", {"request_id": "4242"})

    assert result.data["status"] == "awaiting_callback"


async def test_status_and_results_after_a_callback(client, store):
    await client.call_tool("reveal_contact", {"identifier": "a@b.com"})
    event = await store.record_event(
        tenant_id="default", request_id="4242", correlation_id="c1",
        raw_payload=SAMPLE_CALLBACK,
    )
    await store.link_event_to_request("4242", event.event_id)

    status = await client.call_tool("get_request_status", {"request_id": "4242"})
    assert status.data["status"] == "callback_received"
    assert status.data["events"][0]["successful"] == 1

    results = await client.call_tool("get_enrichment_result", {"request_id": "4242"})
    assert results.data["count"] == 3
    assert results.data["successful"] == 1
    assert results.data["profiles"][0]["full_name"] == "Jane Welder"


async def test_results_are_readable_even_when_delivery_failed(client, store):
    """Delivery state and readability are independent.

    A parked event still contains the data that was paid for.
    """
    from signalhire_mcp.inbox.models import EventState

    await client.call_tool("reveal_contact", {"identifier": "a@b.com"})
    event = await store.record_event(
        tenant_id="default", request_id="4242", correlation_id="c1",
        raw_payload=SAMPLE_CALLBACK,
    )
    await store.link_event_to_request("4242", event.event_id)
    event.state = EventState.FAILED
    await store.save_event(event)

    results = await client.call_tool("get_enrichment_result", {"request_id": "4242"})
    assert results.data["successful"] == 1


async def test_failed_deliveries_are_listable_and_retryable(client, store):
    from signalhire_mcp.inbox.models import EventState

    event = await store.record_event(
        tenant_id="default", request_id="9", correlation_id="c1",
        raw_payload=SAMPLE_CALLBACK,
    )
    event.state = EventState.FAILED
    event.last_error = "downstream exploded"
    await store.save_event(event)

    listed = await client.call_tool("list_failed_deliveries", {})
    assert listed.data["count"] == 1
    assert listed.data["events"][0]["last_error"] == "downstream exploded"

    retried = await client.call_tool("retry_delivery", {"event_id": event.event_id})
    assert retried.data["state"] == "received"


async def test_retry_of_a_missing_event_is_an_error(client):
    with pytest.raises(Exception):
        await client.call_tool("retry_delivery", {"event_id": "nope"})


async def test_check_credits_names_the_pool(client):
    main = await client.call_tool("check_credits", {})
    assert main.data["credits"] == 1000
    assert main.data["pool"] == "with_contacts"

    other = await client.call_tool("check_credits", {"without_contacts": True})
    assert other.data["credits"] == 0
    assert other.data["pool"] == "without_contacts"


async def test_search_requires_at_least_one_filter(client):
    """An exclude-only search is rejected by SignalHire itself."""
    with pytest.raises(Exception) as exc:
        await client.call_tool("search_prospects", {})
    assert "filter" in str(exc.value).lower()


async def test_search_returns_profiles_and_warns_about_the_cursor(client):
    result = await client.call_tool("search_prospects", {"title": "Welder"})
    assert result.data["total"] == 1
    assert result.data["scroll_id"] == "cursor-1"
    assert "15 seconds" in result.data["note"]


async def test_scroll_rejects_a_non_numeric_request_id(client):
    with pytest.raises(Exception) as exc:
        await client.call_tool(
            "scroll_search_results", {"request_id": "abc", "scroll_id": "x"}
        )
    assert "numeric" in str(exc.value).lower()


async def test_list_requests(client):
    await client.call_tool("reveal_contact", {"identifier": "a@b.com"})
    result = await client.call_tool("list_requests", {})
    assert result.data["count"] == 1
    assert result.data["requests"][0]["request_id"] == "4242"


async def test_no_tool_accepts_a_tenant_argument(client):
    """Tenant must come from verified claims, never from the model.

    A `tenant_id` parameter would let any caller spend another customer's
    credits by asking for them.
    """
    for tool in await client.list_tools():
        # `input_schema` is the MCP SDK v2 name; `inputSchema` is bridged with a
        # deprecation warning and will stop working.
        properties = (tool.input_schema or {}).get("properties", {})
        assert "tenant_id" not in properties, f"{tool.name} exposes tenant_id"
        assert "tenant" not in properties, f"{tool.name} exposes tenant"
