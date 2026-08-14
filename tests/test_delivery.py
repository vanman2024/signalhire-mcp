"""The delivery worker and adapters.

The behaviour under test is the one the previous system did not have: a failed
downstream write is retried and stays visible, instead of becoming a log line
and a `continue`.
"""

from __future__ import annotations

from signalhire_mcp.delivery.base import DeliveryResult
from signalhire_mcp.delivery.registry import (
    TenantConfigError,
    TenantRegistry,
    UnknownTenantError,
)
from signalhire_mcp.delivery.worker import DeliveryWorker
from signalhire_mcp.inbox.models import EventState
from tests.conftest import RecordingAdapter, SAMPLE_CALLBACK


def _worker(settings, store, registry) -> DeliveryWorker:
    return DeliveryWorker(settings, store, registry)


async def test_successful_delivery_marks_the_event_delivered(
    settings, store, registry, adapter
):
    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1",
        raw_payload=SAMPLE_CALLBACK,
    )

    await _worker(settings, store, registry).drain_once()

    stored = await store.get_event(event.event_id)
    assert stored is not None
    assert stored.state is EventState.DELIVERED
    assert adapter.calls, "the adapter should have been called"


async def test_adapter_receives_normalised_profiles_not_raw_json(
    settings, store, registry, adapter
):
    await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1",
        raw_payload=SAMPLE_CALLBACK,
    )

    await _worker(settings, store, registry).drain_once()

    profiles = adapter.profiles
    assert len(profiles) == 3
    jane = profiles[0]
    assert jane.full_name == "Jane Welder"
    assert jane.primary_email == "jane@gmail.com", "personal is preferred for email"
    assert jane.primary_phone == "+1 403-555-0100"
    assert jane.current_company == "Acme Fabrication"


async def test_failure_is_retried_not_dropped(settings, store, registry):
    flaky = RecordingAdapter("flaky", fail_times=1)
    registry._adapters["default"] = [flaky]

    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1", raw_payload=[]
    )
    worker = _worker(settings, store, registry)

    await worker.drain_once()
    after_failure = await store.get_event(event.event_id)
    assert after_failure is not None
    assert after_failure.state is EventState.RECEIVED, "still queued, not lost"
    assert after_failure.attempts == 1
    assert "stubbed failure" in after_failure.last_error

    # Backoff in the test settings is ~10ms, so the next drain picks it up.
    import asyncio

    await asyncio.sleep(0.05)
    await worker.drain_once()

    after_success = await store.get_event(event.event_id)
    assert after_success is not None
    assert after_success.state is EventState.DELIVERED


async def test_event_parks_after_exhausting_attempts(settings, store, registry):
    always_fails = RecordingAdapter("broken", fail_times=999)
    registry._adapters["default"] = [always_fails]

    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1", raw_payload=[]
    )
    worker = _worker(settings, store, registry)

    import asyncio

    for _ in range(settings.max_delivery_attempts + 2):
        await worker.drain_once()
        await asyncio.sleep(0.03)

    parked = await store.get_event(event.event_id)
    assert parked is not None
    assert parked.state is EventState.FAILED
    assert parked.attempts >= settings.max_delivery_attempts
    # The point of parking rather than deleting.
    assert parked.raw_payload is not None
    assert "broken" in parked.last_error


async def test_partial_success_only_retries_the_failing_adapter(
    settings, store, registry
):
    """Re-delivering to an adapter that already succeeded creates duplicates."""
    good = RecordingAdapter("good")
    bad = RecordingAdapter("bad", fail_times=1)
    registry._adapters["default"] = [good, bad]

    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1", raw_payload=[]
    )
    worker = _worker(settings, store, registry)

    await worker.drain_once()
    mid = await store.get_event(event.event_id)
    assert mid is not None
    assert mid.state is EventState.PARTIAL
    assert mid.adapters["good"].succeeded is True
    assert mid.adapters["bad"].succeeded is False
    assert len(good.calls) == 1

    import asyncio

    await asyncio.sleep(0.05)
    await worker.drain_once()

    done = await store.get_event(event.event_id)
    assert done is not None
    assert done.state is EventState.DELIVERED
    assert len(good.calls) == 1, "the healthy adapter must not be called twice"
    assert len(bad.calls) == 2


async def test_an_adapter_that_raises_does_not_stall_the_queue(
    settings, store, registry
):
    class Exploding:
        name = "exploding"

        async def deliver(self, event, profiles):
            raise RuntimeError("boom")

        def describe(self) -> str:
            return "exploding"

        async def aclose(self) -> None:
            return None

    registry._adapters["default"] = [Exploding()]
    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1", raw_payload=[]
    )

    await _worker(settings, store, registry).drain_once()

    stored = await store.get_event(event.event_id)
    assert stored is not None
    assert stored.state is EventState.RECEIVED
    assert "boom" in stored.last_error


async def test_unknown_tenant_parks_immediately(settings, store, registry):
    """Not retryable: a tenant will not appear by waiting."""
    event = await store.record_event(
        tenant_id="ghost", request_id="1", correlation_id="c1", raw_payload=[]
    )

    await _worker(settings, store, registry).drain_once()

    stored = await store.get_event(event.event_id)
    assert stored is not None
    assert stored.state is EventState.FAILED
    assert "Unknown tenant" in stored.last_error


async def test_no_adapters_configured_stores_without_retrying(settings, store):
    registry = TenantRegistry({"default": {"adapters": []}})
    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1", raw_payload=[]
    )

    await _worker(settings, store, registry).drain_once()

    stored = await store.get_event(event.event_id)
    assert stored is not None
    assert stored.state is EventState.DELIVERED


async def test_callback_with_only_failures_is_a_successful_delivery(
    settings, store, registry, adapter
):
    """A payload of `credits_are_over` items has nothing to write.

    Treating that as a delivery failure would retry it until it parked.
    """
    from signalhire_mcp.delivery.mcp_adapter import McpAdapter

    mcp_adapter = McpAdapter("ats", server="http://unused/mcp/", tool="noop")
    registry._adapters["default"] = [mcp_adapter]

    event = await store.record_event(
        tenant_id="default", request_id="1", correlation_id="c1",
        raw_payload=[{"item": "x", "status": "credits_are_over"}],
    )

    await _worker(settings, store, registry).drain_once()

    stored = await store.get_event(event.event_id)
    assert stored is not None
    assert stored.state is EventState.DELIVERED


# --- registry ------------------------------------------------------------


def test_unknown_tenant_is_an_error_not_a_fallback():
    registry = TenantRegistry({"default": {"adapters": []}})
    try:
        registry.require("acme")
    except UnknownTenantError as exc:
        assert "Refusing to fall back" in str(exc)
    else:
        raise AssertionError("expected UnknownTenantError")


def test_duplicate_adapter_names_are_rejected():
    registry = TenantRegistry(
        {
            "default": {
                "adapters": [
                    {"type": "webhook", "name": "same", "url": "https://a.test"},
                    {"type": "webhook", "name": "same", "url": "https://b.test"},
                ]
            }
        }
    )
    try:
        registry.adapters_for("default")
    except TenantConfigError as exc:
        assert "duplicate adapter names" in str(exc)
    else:
        raise AssertionError("expected TenantConfigError")


def test_missing_secret_env_is_rejected_at_construction(monkeypatch):
    monkeypatch.delenv("NOPE_SECRET", raising=False)
    registry = TenantRegistry(
        {
            "default": {
                "adapters": [
                    {"type": "webhook", "name": "relay", "url": "https://a.test",
                     "secret_env": "NOPE_SECRET"}
                ]
            }
        }
    )
    try:
        registry.adapters_for("default")
    except TenantConfigError as exc:
        assert "NOPE_SECRET" in str(exc)
    else:
        raise AssertionError("expected TenantConfigError")


def test_secrets_are_referenced_by_env_name_never_inlined(monkeypatch):
    monkeypatch.setenv("RELAY_SECRET", "hunter2")
    registry = TenantRegistry(
        {
            "default": {
                "adapters": [
                    {"type": "webhook", "name": "relay", "url": "https://a.test",
                     "secret_env": "RELAY_SECRET"}
                ]
            }
        }
    )
    adapters = registry.adapters_for("default")
    assert "hunter2" not in adapters[0].describe()


def test_delivery_result_helpers():
    assert DeliveryResult.success("ok").ok is True
    assert DeliveryResult.failure("bad").ok is False
