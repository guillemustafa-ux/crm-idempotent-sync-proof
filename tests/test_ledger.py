"""The ledger on its own: the intent is durable before the send, and a binding is never silently rewritten."""

from __future__ import annotations

import sqlite3

import pytest

from crm_idempotent_sync_proof.ledger import (
    DONE,
    PENDING,
    STEP_CONTACT,
    STEP_DEAL,
    SYNC_PENDING,
    ConflictingRedelivery,
    LedgerConflict,
)

ORDER = {"id": "ORD-1", "email": "ana@example.com", "amount": "10.00"}


def test_begin_commits_the_intent_before_anything_is_sent(ledger_factory):
    ledger_factory().record_delivery(ORDER)
    ledger_factory().begin("ORD-1", STEP_DEAL)
    # A different connection -- the next process -- sees it.
    assert ledger_factory().intent("ORD-1", STEP_DEAL) == PENDING


def test_binding_marks_the_intent_done_and_records_the_id(ledger_factory):
    ledger = ledger_factory()
    ledger.record_delivery(ORDER)
    ledger.begin("ORD-1", STEP_CONTACT)
    ledger.bind_contact("ORD-1", "900001")
    ledger.bind_contact("ORD-1", "900001")  # the same id again is fine
    reopened = ledger_factory()
    assert reopened.intent("ORD-1", STEP_CONTACT) == DONE
    assert reopened.get_order("ORD-1").hubspot_contact_id == "900001"


def test_a_binding_is_never_overwritten_with_a_different_id(ledger_factory):
    ledger = ledger_factory()
    ledger.record_delivery(ORDER)
    ledger.bind_deal("ORD-1", "900002")
    with pytest.raises(LedgerConflict, match="bound to 900002"):
        ledger.bind_deal("ORD-1", "900099")
    assert ledger_factory().get_order("ORD-1").hubspot_deal_id == "900002"


def test_one_deal_cannot_be_bound_to_two_orders(ledger_factory):
    ledger = ledger_factory()
    ledger.record_delivery(ORDER)
    ledger.record_delivery({**ORDER, "id": "ORD-2"})
    ledger.bind_deal("ORD-1", "900002")
    with pytest.raises(LedgerConflict, match="already bound to another order"):
        ledger.bind_deal("ORD-2", "900002")
    assert ledger_factory().get_order("ORD-2").hubspot_deal_id is None


def test_identical_redeliveries_are_counted_and_different_content_is_refused(ledger_factory):
    ledger = ledger_factory()
    ledger.record_delivery(ORDER)
    # Key order does not make it different content.
    ledger.record_delivery(dict(reversed(list(ORDER.items()))))
    with pytest.raises(ConflictingRedelivery):
        ledger.record_delivery({**ORDER, "amount": "11.00"})
    order = ledger_factory().get_order("ORD-1")
    assert (order.deliveries, order.order["amount"], order.sync_status) == (2, "10.00", SYNC_PENDING)


def test_the_schema_refuses_synced_or_associated_without_both_ids(ledger_factory, db_path):
    ledger_factory().record_delivery(ORDER)
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE orders SET sync_status = 'synced' WHERE order_id = 'ORD-1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE orders SET associated = 1 WHERE order_id = 'ORD-1'")
    finally:
        conn.close()
