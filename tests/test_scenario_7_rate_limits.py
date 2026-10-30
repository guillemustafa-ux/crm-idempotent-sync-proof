"""Scenario 7 -- a 429 in the middle of the flow.

HubSpot answers a throttled request with ``429``, ``errorType: RATE_LIMIT``
and a ``policyName``, and sends ``X-HubSpot-RateLimit-*`` headers on every
response (seen live: ``-Max`` 100 per ``-Interval-Milliseconds`` 10000).
It documents **no** ``Retry-After``, and none was seen. So the client waits
the announced interval and resends the same request. That is safe here for
a reason worth stating: a throttled request was not processed, and even if
an *earlier* send of the same step had landed, the resend is an upsert by a
unique property and finds it.
"""

from __future__ import annotations

import pytest

from crm_idempotent_sync_proof.client import HubSpotRejected
from crm_idempotent_sync_proof.fake_hubspot import (
    ROUTE_ASSOCIATION,
    ROUTE_CONTACT_UPSERT,
    ROUTE_DEAL_UPSERT,
)
from crm_idempotent_sync_proof.ledger import (
    DONE,
    PENDING,
    STEP_CONTACT,
    STEP_DEAL,
    SYNC_ERROR,
    SYNC_SYNCED,
)
from tests.helpers import one_of_each, wire


def test_a_429_on_every_step_is_waited_out_and_nothing_is_written_twice(
    hubspot, engine_factory, make_webhook, sleeps
):
    for route in (ROUTE_CONTACT_UPSERT, ROUTE_DEAL_UPSERT, ROUTE_ASSOCIATION):
        hubspot.throttle_next(1, route=route)

    result = engine_factory(hubspot).handle_webhook(*make_webhook())

    assert (result.status, result.contact_created, result.deal_created) == (SYNC_SYNCED, True, True)
    one_of_each(hubspot)
    # The wait is the announced rolling window, 10000 ms, once per 429.
    assert sleeps == [10.0, 10.0, 10.0]
    for route in (ROUTE_CONTACT_UPSERT, ROUTE_DEAL_UPSERT, ROUTE_ASSOCIATION):
        assert [line.split(" ")[0] for line in wire(hubspot, route)] == ["429", "200"]


def test_two_429s_in_a_row_wait_the_interval_each_time(hubspot, engine_factory, make_webhook, sleeps):
    hubspot.throttle_next(2, route=ROUTE_DEAL_UPSERT)
    engine_factory(hubspot).handle_webhook(*make_webhook())
    one_of_each(hubspot)
    assert sleeps == [10.0, 10.0]
    assert [line.split(" ")[0] for line in wire(hubspot, ROUTE_DEAL_UPSERT)] == ["429", "429", "200"]


def test_throttling_past_the_retry_budget_is_a_definite_no_and_resume_finishes(
    hubspot, engine_factory, ledger_factory, make_webhook, sleeps
):
    """Four 429s against a budget of three resends: the delivery fails cleanly, and nothing is half-made."""
    hubspot.throttle_next(4, route=ROUTE_DEAL_UPSERT)
    with pytest.raises(HubSpotRejected) as caught:
        engine_factory(hubspot).handle_webhook(*make_webhook())
    assert (caught.value.status, caught.value.error_type) == (429, "RATE_LIMIT")
    assert sleeps == [10.0, 10.0, 10.0]

    ledger = ledger_factory()
    order = ledger.get_order("ORD-1001")
    assert order.sync_status == SYNC_ERROR
    assert order.last_error.startswith("HTTP 429")
    assert (ledger.intent("ORD-1001", STEP_CONTACT), ledger.intent("ORD-1001", STEP_DEAL)) == (DONE, PENDING)
    assert (len(hubspot.contacts()), len(hubspot.deals())) == (1, 0)

    assert engine_factory(hubspot).resume() == {"ORD-1001": SYNC_SYNCED}
    one_of_each(hubspot)
    assert len(wire(hubspot, ROUTE_CONTACT_UPSERT)) == 1  # the bound contact is not sent again
