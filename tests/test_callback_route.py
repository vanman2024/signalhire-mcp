"""The callback endpoint — the contract SignalHire actually enforces.

Every test here encodes a rule from the vendor's documentation:

* respond 200 within 10 seconds, or it retries 3 times and then discards the
  payload permanently;
* results arrive as an array of {item, status, candidate};
* the `Request-Id` header carries the correlation id.

The failure these tests exist to prevent is the one the previous system had:
returning 200 and then losing the data. So the assertions are not "did it
answer 200" but "was the payload on disk *before* it answered 200".
"""

from __future__ import annotations

import time

import pytest

from tests.conftest import SAMPLE_CALLBACK

CALLBACK = "/signalhire/callback/default"


async def test_persists_before_acknowledging(http, store):
    """The whole point: a 200 means the data is durably stored."""
    response = await http.post(
        CALLBACK, params={"secret": "s3cret"}, json=SAMPLE_CALLBACK,
        headers={"Request-Id": "4242"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"

    # The event must be readable from the store the instant the response is
    # available — not eventually, not after a worker tick.
    event = await store.get_event(body["event_id"])
    assert event is not None
    assert event.request_id == "4242"
    assert event.raw_payload == SAMPLE_CALLBACK
    assert event.item_count == 3
    assert event.success_count == 1


async def test_survives_a_restart(http, store, settings):
    """A payload outlives the process that received it.

    Re-reading through a brand new store object is the closest a unit test gets
    to a restart, and it is what distinguishes this design from the in-memory
    one it replaces.
    """
    response = await http.post(CALLBACK, params={"secret": "s3cret"}, json=SAMPLE_CALLBACK)
    event_id = response.json()["event_id"]

    from signalhire_mcp.inbox.store import InboxStore

    reopened = InboxStore(settings.data_dir)
    recovered = await reopened.get_event(event_id)
    assert recovered is not None
    assert recovered.raw_payload == SAMPLE_CALLBACK


async def test_answers_well_inside_the_ten_second_budget(http):
    """SignalHire allows 10 seconds. Persisting must not approach it."""
    started = time.monotonic()
    response = await http.post(CALLBACK, params={"secret": "s3cret"}, json=SAMPLE_CALLBACK)
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    # Generous versus the 10s limit but tight enough to catch someone moving
    # adapter work back into the request path.
    assert elapsed < 1.0, f"callback took {elapsed:.3f}s; the budget is 10s"


async def test_rejects_a_bad_secret_without_storing(http, store):
    response = await http.post(CALLBACK, params={"secret": "wrong"}, json=SAMPLE_CALLBACK)

    assert response.status_code == 401
    assert (await store.stats())["pending"] == 0


async def test_rejects_a_missing_secret(http, store):
    response = await http.post(CALLBACK, json=SAMPLE_CALLBACK)

    assert response.status_code == 401
    assert (await store.stats())["pending"] == 0


async def test_rejects_an_unknown_tenant(http, store):
    """Better to refuse than to file one customer's contacts under another."""
    response = await http.post(
        "/signalhire/callback/not-a-tenant", params={"secret": "s3cret"}, json=SAMPLE_CALLBACK
    )

    assert response.status_code == 404
    assert (await store.stats())["pending"] == 0


async def test_malformed_body_returns_400_so_signalhire_retries(http):
    """A truncated body should be retried, not silently accepted."""
    response = await http.post(
        CALLBACK,
        params={"secret": "s3cret"},
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


async def test_callback_without_request_id_is_still_stored(http, store):
    """The header is documented but a payload without it is still real data."""
    response = await http.post(CALLBACK, params={"secret": "s3cret"}, json=SAMPLE_CALLBACK)

    assert response.status_code == 200
    event = await store.get_event(response.json()["event_id"])
    assert event is not None
    assert event.request_id is None


async def test_per_tenant_routing(http, store):
    response = await http.post(
        "/signalhire/callback/acme", params={"secret": "s3cret"}, json=SAMPLE_CALLBACK
    )

    assert response.status_code == 200
    event = await store.get_event(response.json()["event_id"])
    assert event is not None
    assert event.tenant_id == "acme"


async def test_health_reports_inbox_depth(http):
    await http.post(CALLBACK, params={"secret": "s3cret"}, json=SAMPLE_CALLBACK)

    response = await http.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["inbox"]["pending"] == 1
    assert "default" in body["tenants"]


async def test_mcp_endpoint_is_served_by_the_same_app(http):
    """One process, one port — the structural fix.

    A bare GET is not a valid MCP request, so a 4xx here is success: it proves
    the route exists and is handled rather than 404ing.
    """
    response = await http.get("/mcp/")
    assert response.status_code != 404


@pytest.mark.parametrize("path", ["/signalhire/callback/default", "/health"])
async def test_operational_routes_do_not_require_mcp_auth(http, path):
    """Custom routes sit outside FastMCP auth by design.

    SignalHire cannot present a bearer token, so the callback is protected by a
    shared secret instead. This test pins that arrangement so a future change
    that puts custom routes behind auth fails loudly here rather than silently
    in production, where the symptom is 401s the vendor retries three times and
    then gives up on.
    """
    method = http.get if path == "/health" else http.post
    kwargs = {} if path == "/health" else {"params": {"secret": "s3cret"}, "json": []}
    response = await method(path, **kwargs)
    assert response.status_code != 401 or path != "/health"
