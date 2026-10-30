"""Scenario 3 -- the same webhook, delivered again and again.

Webhook sources redeliver: on a timeout, on a 5xx, on a manual "resend"
from a dashboard, sometimes for no visible reason. Every one of those must
end where the first delivery ended: one contact, one deal, one association.

Two cases, because they prove different things. With the ledger intact, a
replay of a synced order sends **nothing** -- the ledger knows. With the
ledger gone (a new replica, a restored backup, a different worker's
database), the replay is sent again and **HubSpot** settles it: each upsert
answers the record the first delivery made, ``new: false``.
"""

from __future__ import annotations

import pytest

from crm_idempotent_sync_proof.fake_hubspot import (
    ROUTE_ASSOCIATION,
    ROUTE_CONTACT_UPSERT,
    ROUTE_DEAL_UPSERT,
)
from crm_idempotent_sync_proof.ledger import (
    DONE,
    STEP_ASSOCIATION,
    STEP_CONTACT,
    STEP_DEAL,
    SYNC_SYNCED,
    ConflictingRedelivery,
)
from tests.helpers import one_of_each, wire


def test_the_same_webhook_delivered_five_times_is_one_contact_one_deal_one_association(
    hubspot, engine_factory, ledger_factory, make_webhook
):
    webhook = make_webhook()
    results = [engine_factory(hubspot).handle_webhook(*webhook) for _ in range(5)]

    contact, deal = one_of_each(hubspot)
    assert deal["properties"]["external_order_id"] == "ORD-1001"
    assert contact["properties"]["email"] == "ana.perez@example.com"

    first, *replays = results
    assert (first.status, first.contact_created, first.deal_created) == (SYNC_SYNCED, True, True)
    for replay in replays:
        # Synced already: nothing is sent, and the result says so.
        assert (replay.status, replay.contact_created, replay.deal_created) == (SYNC_SYNCED, None, None)
        assert (replay.contact_id, replay.deal_id) == (contact["id"], deal["id"])

    # One send per step on the wire, ever.
    assert len(wire(hubspot, ROUTE_CONTACT_UPSERT)) == 1
    assert len(wire(hubspot, ROUTE_DEAL_UPSERT)) == 1
    assert len(wire(hubspot, ROUTE_ASSOCIATION)) == 1

    order = ledger_factory().get_order("ORD-1001")
    assert order.deliveries == 5
    assert order.sync_status == SYNC_SYNCED
    assert (order.hubspot_contact_id, order.hubspot_deal_id, order.associated) == (
        contact["id"], deal["id"], True,
    )
    ledger = ledger_factory()
    assert [ledger.intent("ORD-1001", s) for s in (STEP_CONTACT, STEP_DEAL, STEP_ASSOCIATION)] == [DONE] * 3


@pytest.mark.parametrize("deliveries", [2, 5])
def test_replays_against_a_lost_ledger_find_what_the_first_delivery_made(
    hubspot, engine_factory, ledger_factory, make_webhook, tmp_path, deliveries
):
    """Every delivery on a database of its own, all inside the search lag: HubSpot answers ``new: false``."""
    webhook = make_webhook()
    paths = [str(tmp_path / f"replica-{i}.db") for i in range(deliveries)]
    results = []
    for path in paths:
        results.append(engine_factory(hubspot, path).handle_webhook(*webhook))
        hubspot.advance(1)  # one second apart: well inside the search lag

    contact, deal = one_of_each(hubspot)
    assert [r.deal_created for r in results] == [True] + [False] * (deliveries - 1)
    assert [r.contact_created for r in results] == [True] + [False] * (deliveries - 1)
    assert {r.deal_id for r in results} == {deal["id"]}

    # Each delivery sent its upserts; HubSpot answered the first record every time.
    assert [line.split(" ")[3] for line in wire(hubspot, ROUTE_DEAL_UPSERT)] == (
        ["new=true"] + ["new=false"] * (deliveries - 1)
    )
    assert len(wire(hubspot, ROUTE_ASSOCIATION)) == deliveries
    for path in paths:
        order = ledger_factory(path).get_order("ORD-1001")
        assert (order.sync_status, order.hubspot_contact_id, order.hubspot_deal_id) == (
            SYNC_SYNCED, contact["id"], deal["id"],
        )


def test_a_redelivery_with_different_content_is_refused_and_writes_nothing(
    hubspot, engine_factory, ledger_factory, make_webhook
):
    """Same order id, different amount: the sync will not pick which one is true."""
    engine_factory(hubspot).handle_webhook(*make_webhook(amount="149.00"))
    sent_before = list(hubspot.request_log)
    recorded = ledger_factory().get_order("ORD-1001")

    with pytest.raises(ConflictingRedelivery) as caught:
        engine_factory(hubspot).handle_webhook(*make_webhook(amount="1.00", event_id="evt_0002"))

    assert caught.value.order_id == "ORD-1001"
    assert hubspot.request_log == sent_before  # nothing sent
    _, deal = one_of_each(hubspot)
    assert deal["properties"]["amount"] == "149.00"
    after = ledger_factory().get_order("ORD-1001")
    assert after == recorded  # nothing recorded: same payload, same count, same status
