"""The runbook's commands: reconcile the ledger against HubSpot, and resume what is stuck.

``reconcile`` only reads. It names every disagreement with the ids on both
sides and never repairs one: a deal someone deleted, a deal id that no
longer matches, an association that is gone -- each is a human's call.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from crm_idempotent_sync_proof.client import UnknownOutcome
from crm_idempotent_sync_proof.fake_hubspot import ROUTE_DEAL_UPSERT
from crm_idempotent_sync_proof.ledger import SYNC_SYNCED
from crm_idempotent_sync_proof.reconcile import (
    ASSOCIATION_MISSING,
    ID_MISMATCH,
    MISSING_IN_HUBSPOT,
    OK,
    PENDING,
    reconcile,
)
from tests.conftest import ROOT


def synced(engine_factory, hubspot, make_webhook, order_id: str, email: str):
    return engine_factory(hubspot).handle_webhook(*make_webhook(order_id, email))


def test_reconcile_names_every_kind_of_drift_and_writes_nothing(
    hubspot, engine_factory, ledger_factory, client_factory, make_webhook
):
    ok = synced(engine_factory, hubspot, make_webhook, "ORD-1", "a@example.com")
    deleted = synced(engine_factory, hubspot, make_webhook, "ORD-2", "b@example.com")
    unlinked = synced(engine_factory, hubspot, make_webhook, "ORD-3", "c@example.com")
    replaced = synced(engine_factory, hubspot, make_webhook, "ORD-4", "d@example.com")
    hubspot.drop_response_after_commit(ROUTE_DEAL_UPSERT)
    with pytest.raises(UnknownOutcome):
        synced(engine_factory, hubspot, make_webhook, "ORD-5", "e@example.com")

    hubspot.archive_deal(deleted.deal_id)  # someone deleted it in the UI
    hubspot.remove_association(unlinked.deal_id, unlinked.contact_id)
    hubspot.archive_deal(replaced.deal_id)  # ...and someone made a new one with our id
    new_deal = hubspot.plant_deal({"external_order_id": "ORD-4", "dealname": "Order ORD-4"})

    ledger_before = ledger_factory().orders()
    log_before = len(hubspot.request_log)
    findings = {f.order_id: f for f in reconcile(ledger_factory(), client_factory(hubspot))}

    assert {k: f.verdict for k, f in findings.items()} == {
        "ORD-1": OK,
        "ORD-2": MISSING_IN_HUBSPOT,
        "ORD-3": ASSOCIATION_MISSING,
        "ORD-4": ID_MISMATCH,
        "ORD-5": PENDING,
    }
    assert ok.deal_id in findings["ORD-1"].detail
    assert deleted.deal_id in findings["ORD-2"].detail
    assert unlinked.contact_id in findings["ORD-3"].detail
    assert replaced.deal_id in findings["ORD-4"].detail and new_deal in findings["ORD-4"].detail
    assert "deal" in findings["ORD-5"].detail  # the step left in flight is named
    # Read-only: only GETs on the wire, and the ledger is exactly as it was.
    assert all(" GET " in line for line in hubspot.request_log[log_before:])
    assert ledger_factory().orders() == ledger_before


def test_a_deal_a_human_deleted_is_not_recreated_by_a_replay_or_a_resume(
    hubspot, engine_factory, ledger_factory, make_webhook
):
    webhook = make_webhook()
    result = engine_factory(hubspot).handle_webhook(*webhook)
    hubspot.archive_deal(result.deal_id)
    log_before = len(hubspot.request_log)

    assert engine_factory(hubspot).handle_webhook(*webhook).status == SYNC_SYNCED
    assert engine_factory(hubspot).resume() == {}
    assert hubspot.deals() == [] and len(hubspot.request_log) == log_before


def cli(hubspot, db_path, *args, key=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUBSPOT_")}
    env["HUBSPOT_BASE_URL"] = hubspot.base_url
    if key is not False:
        env["HUBSPOT_SERVICE_KEY"] = key if key is not None else hubspot.service_key
    return subprocess.run(
        [sys.executable, "-m", "crm_idempotent_sync_proof", *args, "--db", db_path],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT), env=env,
    )


def test_the_cli_reconcile_prints_one_line_per_order_and_exits_1_on_drift(
    hubspot, engine_factory, db_path, make_webhook
):
    first = engine_factory(hubspot).handle_webhook(*make_webhook("ORD-1", "a@example.com"))
    engine_factory(hubspot).handle_webhook(*make_webhook("ORD-2", "b@example.com"))

    clean = cli(hubspot, db_path, "reconcile")
    assert clean.returncode == 0, clean.stderr
    assert [line.split()[:2] for line in clean.stdout.splitlines()] == [["ORD-1", OK], ["ORD-2", OK]]

    hubspot.archive_deal(first.deal_id)
    drift = cli(hubspot, db_path, "reconcile")
    assert drift.returncode == 1
    assert drift.stdout.splitlines()[0].split()[:2] == ["ORD-1", MISSING_IN_HUBSPOT]


def test_the_cli_resume_settles_a_stuck_order(hubspot, engine_factory, ledger_factory, db_path, make_webhook):
    hubspot.drop_response_after_commit(ROUTE_DEAL_UPSERT)
    with pytest.raises(UnknownOutcome):
        engine_factory(hubspot).handle_webhook(*make_webhook())

    proc = cli(hubspot, db_path, "resume")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"ORD-1001 {SYNC_SYNCED}"
    assert len(hubspot.deals()) == 1
    assert ledger_factory().get_order("ORD-1001").sync_status == SYNC_SYNCED


def test_the_cli_strips_quotes_from_the_service_key_and_never_prints_it(hubspot, engine_factory, db_path, make_webhook):
    """A ``.env`` written as ``HUBSPOT_SERVICE_KEY="..."`` is read with the quotes on some loaders."""
    engine_factory(hubspot).handle_webhook(*make_webhook())
    proc = cli(hubspot, db_path, "reconcile", key=f'"{hubspot.service_key}"')
    assert proc.returncode == 0, proc.stderr
    assert hubspot.service_key not in proc.stdout + proc.stderr


def test_the_cli_without_a_service_key_refuses_and_sends_nothing(hubspot, db_path):
    proc = cli(hubspot, db_path, "reconcile", key=False)
    assert proc.returncode == 2
    assert "HUBSPOT_SERVICE_KEY is not set" in proc.stderr
    assert hubspot.request_log == []
