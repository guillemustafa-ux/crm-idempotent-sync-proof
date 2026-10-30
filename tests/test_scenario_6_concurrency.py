"""Scenario 6 -- two deliveries of the same webhook, at the same moment.

Sources fan out: a retry fires while the first attempt is still running, two
replicas pick up the same queue message, a "resend" lands mid-flight. Both
workers are past the contact step and send the deal in the same instant --
a barrier holds each at ``before_deal_send`` until the other is there too.

HubSpot enforces the unique property "across all records of that object
type", so the second upsert meets the first deal. The naive path, held at
the same instant between its search and its create, makes two.
"""

from __future__ import annotations

import json
import threading

import pytest

from crm_idempotent_sync_proof.client import HubSpotClient
from crm_idempotent_sync_proof.ledger import SYNC_SYNCED, Ledger
from crm_idempotent_sync_proof.sync import SyncEngine, naive_deal_sync
from tests.conftest import WEBHOOK_SECRET
from tests.helpers import one_of_each


def run_together(workers) -> list:
    """Run each callable on its own thread; return their results, re-raising any failure."""
    results: list = [None] * len(workers)
    errors: list[BaseException] = []

    def target(i, work):
        try:
            results[i] = work()
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=target, args=(i, w)) for i, w in enumerate(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    if errors:
        raise errors[0]
    return results


@pytest.mark.parametrize("ledgers", ["one-shared-ledger", "a-ledger-each"])
def test_two_simultaneous_deliveries_make_one_deal(hubspot, make_webhook, tmp_path, ledgers):
    webhook = make_webhook()
    barrier = threading.Barrier(2, timeout=10)
    paths = [str(tmp_path / "shared.db")] * 2 if ledgers == "one-shared-ledger" else [
        str(tmp_path / "a.db"), str(tmp_path / "b.db"),
    ]

    def worker(path: str):
        def work():
            ledger = Ledger(path)  # a connection of its own, on its own thread
            try:
                engine = SyncEngine(
                    ledger=ledger,
                    client=HubSpotClient(hubspot.base_url, hubspot.service_key, sleep=lambda _: None),
                    webhook_secret=WEBHOOK_SECRET,
                    on_seam=lambda seam: barrier.wait() if seam == "before_deal_send" else None,
                )
                return engine.handle_webhook(*webhook)
            finally:
                ledger.close()
        return work

    first, second = run_together([worker(p) for p in paths])

    _, deal = one_of_each(hubspot)
    assert first.status == second.status == SYNC_SYNCED
    assert first.deal_id == second.deal_id == deal["id"]
    assert sorted([first.deal_created, second.deal_created]) == [False, True]
    for path in set(paths):
        ledger = Ledger(path)
        try:
            order = ledger.get_order("ORD-1001")
            assert (order.sync_status, order.hubspot_deal_id, order.associated) == (SYNC_SYNCED, deal["id"], True)
            if ledgers == "one-shared-ledger":
                assert order.deliveries == 2
        finally:
            ledger.close()


def test_two_simultaneous_naive_deliveries_make_two_deals(hubspot, make_webhook):
    order = json.loads(make_webhook()[0])["order"]
    barrier = threading.Barrier(2, timeout=10)

    def work():
        client = HubSpotClient(hubspot.base_url, hubspot.service_key, sleep=lambda _: None)
        return naive_deal_sync(client, order, on_seam=lambda seam: barrier.wait())

    first, second = run_together([work, work])
    assert first != second
    assert len(hubspot.deals()) == 2, "both searched, both found nothing, both created"
