"""The durable inbox.

These tests are about survival, not features. Each one corresponds to a way the
previous system lost data.
"""

from __future__ import annotations

import pytest

from signalhire_mcp.inbox.models import EventState, RevealRequest
from signalhire_mcp.inbox.store import InboxStore
from tests.conftest import SAMPLE_CALLBACK


async def test_event_is_readable_from_a_fresh_store(store, settings):
    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1",
        raw_payload=SAMPLE_CALLBACK,
    )

    reopened = InboxStore(settings.data_dir)
    found = await reopened.get_event(event.event_id)

    assert found is not None
    assert found.raw_payload == SAMPLE_CALLBACK


async def test_summary_counters_are_computed_at_receipt(store):
    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1",
        raw_payload=SAMPLE_CALLBACK,
    )
    assert event.item_count == 3
    assert event.success_count == 1


@pytest.mark.parametrize("payload", [None, {}, "nonsense", 42, []])
async def test_unexpected_payload_shapes_are_still_stored(store, payload):
    """Storing must never depend on the payload being well-formed.

    A vendor that changes its response shape should cost us a normalisation
    bug, not the data.
    """
    event = await store.record_event(
        tenant_id="default", request_id=None, correlation_id="c1", raw_payload=payload
    )
    found = await store.get_event(event.event_id)
    assert found is not None
    assert found.raw_payload == payload


async def test_claim_only_returns_due_events(store):
    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1", raw_payload=[]
    )
    event.attempts = 1
    event.schedule_retry(base_seconds=3600, max_seconds=7200)
    await store.save_event(event)

    assert await store.claim_due_events() == []


async def test_claiming_marks_delivering_so_it_is_not_claimed_twice(store):
    await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1", raw_payload=[]
    )

    first = await store.claim_due_events()
    second = await store.claim_due_events()

    assert len(first) == 1
    assert first[0].state is EventState.DELIVERING
    assert second == [], "a claimed event must not be handed out again"


async def test_stuck_delivering_events_are_recovered_on_restart(store, settings):
    """A process killed mid-delivery must not strand the event forever.

    Without recovery the event sits in DELIVERING, which the claim query skips,
    so it is never retried and never parked — loss that looks like success.
    """
    await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1", raw_payload=[]
    )
    claimed = await store.claim_due_events()
    assert claimed[0].state is EventState.DELIVERING

    reopened = InboxStore(settings.data_dir)
    recovered = reopened.recover_stuck_events()

    assert recovered == 1
    assert len(await reopened.claim_due_events()) == 1


async def test_delivered_events_leave_the_pending_directory(store):
    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1", raw_payload=[]
    )
    event.state = EventState.DELIVERED
    await store.save_event(event)

    stats = await store.stats()
    assert stats["pending"] == 0
    assert stats["delivered"] == 1
    assert await store.claim_due_events() == []


async def test_requeue_revives_a_parked_event(store):
    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1", raw_payload=[]
    )
    event.state = EventState.FAILED
    event.attempts = 99
    await store.save_event(event)

    revived = await store.requeue(event.event_id)

    assert revived is not None
    assert revived.state is EventState.RECEIVED
    assert revived.attempts == 0, "a manual retry starts the budget over"
    assert len(await store.claim_due_events()) == 1


async def test_parked_events_are_never_deleted(store):
    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1",
        raw_payload=SAMPLE_CALLBACK,
    )
    event.state = EventState.FAILED
    await store.save_event(event)

    found = await store.get_event(event.event_id)
    assert found is not None
    assert found.raw_payload == SAMPLE_CALLBACK, "the payload outlives the failure"


async def test_request_records_link_to_their_events(store):
    await store.record_request(
        RevealRequest(request_id="777", tenant_id="default", correlation_id="c1",
                      identifiers=["a@b.com"])
    )
    event = await store.record_event(
        tenant_id="default", request_id="777", correlation_id="c1", raw_payload=[]
    )
    await store.link_event_to_request("777", event.event_id)

    record = await store.get_request("777")
    assert record is not None
    assert event.event_id in record.event_ids

    found = await store.find_events_for_request("777")
    assert [e.event_id for e in found] == [event.event_id]


async def test_callback_for_an_unknown_request_is_not_rejected(store):
    """SignalHire will deliver results for requests a previous deploy submitted.

    Refusing those to preserve a foreign key would discard real, paid-for data.
    """
    event = await store.record_event(
        tenant_id="default", request_id="does-not-exist", correlation_id="c1",
        raw_payload=SAMPLE_CALLBACK,
    )
    await store.link_event_to_request("does-not-exist", event.event_id)

    assert await store.get_event(event.event_id) is not None


async def test_request_id_cannot_escape_the_data_directory(store, settings):
    """`Request-Id` is an attacker-controlled header, so it is sanitised.

    Without this a callback could choose where the process writes.
    """
    await store.record_request(
        RevealRequest(
            request_id="../../../../etc/cron.d/pwned",
            tenant_id="default",
            correlation_id="c1",
        )
    )

    written = list((settings.data_dir / "requests").glob("*.json"))
    assert len(written) == 1
    assert ".." not in written[0].name
    assert written[0].parent == settings.data_dir / "requests"


async def test_backoff_grows_and_is_capped(store):
    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1", raw_payload=[]
    )

    from signalhire_mcp.inbox.models import utc_now

    delays = []
    for attempt in range(1, 8):
        event.attempts = attempt
        # Measure from now: schedule_retry sets next_attempt_at relative to the
        # current time, not to whatever it was previously set to.
        event.schedule_retry(base_seconds=10, max_seconds=100)
        delays.append((event.next_attempt_at - utc_now()).total_seconds())

    assert delays[0] < delays[1] < delays[2], "backoff must grow"
    assert max(delays) <= 101, "backoff must be capped"
