"""Operate the ledger from a shell.

    python -m crm_idempotent_sync_proof reconcile --db sync.db
    python -m crm_idempotent_sync_proof resume --db sync.db

Reads ``HUBSPOT_SERVICE_KEY`` (and optionally ``HUBSPOT_BASE_URL``) from the
environment. The key is never printed. ``reconcile`` exits 1 when any order
is not ``ok``; ``resume`` exits 1 when any order is still not synced.
"""

from __future__ import annotations

import argparse
import os
import sys

from .client import HUBSPOT_API_URL, HubSpotClient
from .ledger import SYNC_SYNCED, Ledger
from .reconcile import OK, reconcile
from .sync import SyncEngine


def env(name: str, default: str = "") -> str:
    # A .env value written with double quotes reaches some loaders with the
    # quotes still on it; a quoted key or URL never matches anything.
    return (os.environ.get(name) or default).replace('"', "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m crm_idempotent_sync_proof")
    parser.add_argument("command", choices=("reconcile", "resume"))
    parser.add_argument("--db", required=True, help="the ledger's SQLite file")
    args = parser.parse_args(argv)

    key = env("HUBSPOT_SERVICE_KEY")
    if not key:
        print("HUBSPOT_SERVICE_KEY is not set; nothing was sent", file=sys.stderr)
        return 2
    client = HubSpotClient(env("HUBSPOT_BASE_URL", HUBSPOT_API_URL), key)
    ledger = Ledger(args.db)
    try:
        if args.command == "reconcile":
            findings = reconcile(ledger, client)
            for f in findings:
                print(f"{f.order_id} {f.verdict} {f.detail}")
            return 0 if all(f.verdict == OK for f in findings) else 1
        outcome = SyncEngine(ledger=ledger, client=client).resume()
        for order_id, status in outcome.items():
            print(f"{order_id} {status}")
        return 0 if all(s == SYNC_SYNCED for s in outcome.values()) else 1
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
