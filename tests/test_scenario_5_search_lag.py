"""Scenario 5 -- the footgun: "search, then create if not found".

HubSpot's docs: "It may take a few moments for newly created or updated CRM
objects to appear in search results." Measured live on 2026-10-30: a fresh
deal first appeared in search after 10.08 s. Inside that window a search
for a deal that exists answers "not found", and the code that trusted it
creates a second one.

The A/B is the same two deliveries, one second apart, through two
strategies:

* A, the naive path: search by the order id in ``dealname``, create if not
  found -> **two deals**;
* B, the design under test: upsert by the unique ``external_order_id`` ->
  **one deal**, the second delivery answered ``new: false``.

Two controls make the result honest. Naive code is fine once the index has
caught up, so the duplicate is the lag and not a bug in the search. And
naive code that also plants the unique property gets a ``400`` instead of a
duplicate -- the unique property is the backstop, but the delivery fails.
"""

from __future__ import annotations

import json
import logging

import pytest

from crm_idempotent_sync_proof.client import HubSpotRejected
from crm_idempotent_sync_proof.fake_hubspot import SEARCH_LAG_SECONDS
from crm_idempotent_sync_proof.sync import naive_deal_sync
from tests.helpers import one_of_each

log = logging.getLogger("crm_idempotent_sync_proof")


def order_of(webhook) -> dict:
    return json.loads(webhook[0])["order"]


def test_a_naive_search_then_create_duplicates_inside_the_search_lag(hubspot, client_factory, make_webhook):
    order = order_of(make_webhook())
    first = naive_deal_sync(client_factory(hubspot), order)
    hubspot.advance(1)
    second = naive_deal_sync(client_factory(hubspot), order)

    assert first != second
    assert [d["properties"]["dealname"] for d in hubspot.deals()] == ["Order ORD-1001"] * 2
    log.info("A naive search-then-create, 2 deliveries 1 s apart -> %d deals", len(hubspot.deals()))


def test_b_the_upsert_by_unique_property_makes_one_deal_inside_the_search_lag(
    hubspot, engine_factory, make_webhook, tmp_path
):
    """Two deliveries one second apart, each on a database of its own, so no ledger can help."""
    webhook = make_webhook()
    first = engine_factory(hubspot, str(tmp_path / "a.db")).handle_webhook(*webhook)
    hubspot.advance(1)
    second = engine_factory(hubspot, str(tmp_path / "b.db")).handle_webhook(*webhook)

    _, deal = one_of_each(hubspot)
    assert (first.deal_created, second.deal_created) == (True, False)
    assert first.deal_id == second.deal_id == deal["id"]
    log.info("B upsert by external_order_id, 2 deliveries 1 s apart -> %d deal", len(hubspot.deals()))


def test_control_the_naive_path_is_fine_once_the_index_has_caught_up(hubspot, client_factory, make_webhook):
    order = order_of(make_webhook())
    first = naive_deal_sync(client_factory(hubspot), order)
    hubspot.advance(SEARCH_LAG_SECONDS)
    second = naive_deal_sync(client_factory(hubspot), order)
    assert first == second and len(hubspot.deals()) == 1


def test_control_naive_create_on_the_unique_property_is_refused_not_duplicated(
    hubspot, client_factory, make_webhook
):
    """The live 400: no duplicate -- and a delivery that fails instead of succeeding."""
    order = order_of(make_webhook())
    first = naive_deal_sync(client_factory(hubspot), order, plant_unique_id=True)
    hubspot.advance(1)
    with pytest.raises(HubSpotRejected) as caught:
        naive_deal_sync(client_factory(hubspot), order, plant_unique_id=True)

    rejected = caught.value
    assert (rejected.status, rejected.category) == (400, "VALIDATION_ERROR")
    assert rejected.says("propertyName=external_order_id, value=ORD-1001")
    assert rejected.says(f"{first} already has that value.")
    assert [d["id"] for d in hubspot.deals()] == [first]
