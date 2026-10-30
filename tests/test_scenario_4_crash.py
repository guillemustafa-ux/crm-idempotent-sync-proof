"""Scenario 4 -- the gap between "HubSpot committed it" and "we wrote it down".

The process is killed for real (``os._exit`` in a child process, exit 137)
at the exact instruction after a HubSpot response arrives and before the
ledger records it. What is on disk afterwards is all the next run gets.

The kill points: after the contact upsert (so between the contact write and
the deal write), after the deal upsert, and after the association. The
restart is either the source redelivering the webhook or a ``resume`` over
the ledger; both resend the step whose intent was left pending, and HubSpot
answers it with the record the killed process made.
"""

from __future__ import annotations

import json

import pytest

from crm_idempotent_sync_proof.client import HubSpotRejected, UnknownOutcome
from crm_idempotent_sync_proof.fake_hubspot import (
    ROUTE_ASSOCIATION,
    ROUTE_CONTACT_UPSERT,
    ROUTE_DEAL_CREATE,
    ROUTE_DEAL_UPSERT,
)
from crm_idempotent_sync_proof.ledger import (
    DONE,
    PENDING,
    STEP_ASSOCIATION,
    STEP_CONTACT,
    STEP_DEAL,
    SYNC_PENDING,
    SYNC_SYNCED,
    SYNC_UNKNOWN,
)
from crm_idempotent_sync_proof.sync import naive_deal_sync
from tests.helpers import one_of_each, wire

# What the killed child leaves behind, per kill point: the intent states of
# (contact, deal, association) on disk, and what HubSpot already holds.
AFTER_KILL = {
    "after_contact_response": ((PENDING, None, None), (1, 0, 0)),
    "after_deal_response": ((DONE, PENDING, None), (1, 1, 0)),
    "after_association_response": ((DONE, DONE, PENDING), (1, 1, 1)),
}


@pytest.mark.parametrize("restart", ["redelivery", "resume"])
@pytest.mark.parametrize("crash_at", list(AFTER_KILL))
def test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates(
    hubspot, engine_factory, ledger_factory, run_child, make_webhook, crash_at, restart
):
    webhook = make_webhook()
    run_child(hubspot, webhook, crash_at=crash_at)

    # On disk: the order, and an intent row saying a send was in flight.
    ledger = ledger_factory()
    order = ledger.get_order("ORD-1001")
    assert order is not None and order.sync_status == SYNC_PENDING
    intents, held = AFTER_KILL[crash_at]
    assert tuple(ledger.intent("ORD-1001", s) for s in (STEP_CONTACT, STEP_DEAL, STEP_ASSOCIATION)) == intents
    # In HubSpot: the write the child never recorded.
    assert (len(hubspot.contacts()), len(hubspot.deals()), len(hubspot.associations())) == held

    if restart == "redelivery":
        result = engine_factory(hubspot).handle_webhook(*webhook)
        assert result.status == SYNC_SYNCED
    else:
        assert engine_factory(hubspot).resume() == {"ORD-1001": SYNC_SYNCED}

    contact, deal = one_of_each(hubspot)
    # The step that was in flight was resent and answered with what the dead
    # process made: ``new=false`` on the wire.
    if crash_at == "after_contact_response":
        assert [l.split(" ")[3] for l in wire(hubspot, ROUTE_CONTACT_UPSERT)] == ["new=true", "new=false"]
    if crash_at == "after_deal_response":
        assert [l.split(" ")[3] for l in wire(hubspot, ROUTE_DEAL_UPSERT)] == ["new=true", "new=false"]
    if crash_at == "after_association_response":
        # The association PUT is sent again, and HubSpot still holds one link.
        assert len(wire(hubspot, ROUTE_ASSOCIATION)) == 2
        assert len(hubspot.associations()) == 1
    order = ledger_factory().get_order("ORD-1001")
    assert (order.sync_status, order.hubspot_contact_id, order.hubspot_deal_id, order.associated) == (
        SYNC_SYNCED, contact["id"], deal["id"], True,
    )


def test_naive_retry_after_a_real_kill_makes_a_second_deal(hubspot, client_factory, run_child, make_webhook):
    """The anti-pattern, demonstrated rather than asserted.

    HubSpot commits the deal, the response is lost, the worker dies before
    writing anything down. The retry searches -- inside the search lag, so it
    finds nothing -- and creates the deal again.
    """
    webhook = make_webhook()
    hubspot.drop_response_after_commit(ROUTE_DEAL_CREATE)
    run_child(hubspot, webhook, crash_at="-", strategy="naive")
    assert len(hubspot.deals()) == 1  # HubSpot has it; nobody wrote it down

    hubspot.advance(2)  # the restart comes back two seconds later
    naive_deal_sync(client_factory(hubspot), json.loads(webhook[0])["order"])

    names = [d["properties"]["dealname"] for d in hubspot.deals()]
    assert names == ["Order ORD-1001", "Order ORD-1001"], (
        "this is the bug this repo exists to prevent: one order, two deals"
    )


def test_a_lost_response_on_the_deal_upsert_is_settled_by_resending_it(
    hubspot, engine_factory, ledger_factory, make_webhook
):
    """No crash: the process survives the dropped connection and still cannot tell whether the deal exists."""
    webhook = make_webhook()
    hubspot.drop_response_after_commit(ROUTE_DEAL_UPSERT)
    with pytest.raises(UnknownOutcome):
        engine_factory(hubspot).handle_webhook(*webhook)

    order = ledger_factory().get_order("ORD-1001")
    assert order.sync_status == SYNC_UNKNOWN and order.hubspot_deal_id is None
    assert "deals/batch/upsert" in order.last_error
    assert ledger_factory().intent("ORD-1001", STEP_DEAL) == PENDING
    assert len(hubspot.deals()) == 1 and hubspot.associations() == set()

    result = engine_factory(hubspot).handle_webhook(*webhook)
    assert (result.status, result.deal_created) == (SYNC_SYNCED, False)
    _, deal = one_of_each(hubspot)
    assert ledger_factory().get_order("ORD-1001").hubspot_deal_id == deal["id"]


def test_a_write_that_never_landed_is_actually_sent(hubspot, engine_factory, ledger_factory, make_webhook):
    """The mirror case: the connection dropped before the commit. "Do nothing on doubt" is not the answer either."""
    hubspot.drop_response_before_commit(ROUTE_DEAL_UPSERT)
    with pytest.raises(UnknownOutcome):
        engine_factory(hubspot).handle_webhook(*make_webhook())
    assert hubspot.deals() == []

    assert engine_factory(hubspot).resume() == {"ORD-1001": SYNC_SYNCED}
    one_of_each(hubspot)
    sent = wire(hubspot, ROUTE_DEAL_UPSERT)
    assert [line.split(" ")[0] for line in sent] == ["CUT-BEFORE", "200"]
    assert sent[1].split(" ")[3] == "new=true"


def test_a_5xx_is_an_unknown_outcome_not_a_rejection(hubspot, engine_factory, ledger_factory, make_webhook):
    hubspot.fail_next_with(502, route=ROUTE_DEAL_UPSERT)
    with pytest.raises(UnknownOutcome) as caught:
        engine_factory(hubspot).handle_webhook(*make_webhook())
    assert not isinstance(caught.value, HubSpotRejected)
    assert ledger_factory().get_order("ORD-1001").sync_status == SYNC_UNKNOWN

    assert engine_factory(hubspot).resume() == {"ORD-1001": SYNC_SYNCED}
    one_of_each(hubspot)
