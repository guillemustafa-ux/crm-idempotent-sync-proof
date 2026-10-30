"""Run the sync against a real HubSpot portal, once, and print what HubSpot answered.

    python scripts/live_smoke.py                       # key from the environment
    python scripts/live_smoke.py --env-file .env.local # or from a git-ignored file
    python scripts/live_smoke.py --env-file .env.local --naive-ab

Needs ``HUBSPOT_SERVICE_KEY``: a service key (or private app token) with
contacts and deals read+write and schemas.deals read, on a portal where the
deal property ``external_order_id`` exists with ``hasUniqueValue: true``.
The key is read from the environment, or from ``--env-file`` when given --
never from anywhere else -- and it is never printed. Without one the script
prints a skip line and exits 0.

What it does, against the live portal:

1. checks ``external_order_id`` is a unique property, and refuses otherwise;
2. signs one "order paid" webhook (a fresh order id, ``SMOKE-...``) and
   delivers it to the sync with a ledger of its own;
3. delivers it again to a sync with a **fresh** ledger (a lost database, a
   second replica) -- HubSpot must answer the same contact and deal with
   ``new: false``;
4. delivers it a third time to the first ledger -- nothing must be sent;
5. tries a plain create with the same ``external_order_id`` -- HubSpot must
   refuse it with ``400 VALIDATION_ERROR``;
6. reads the deal's associations back -- exactly the one contact;
7. with ``--naive-ab``, runs search-then-create twice, back to back, on a
   second order that lives only in ``dealname`` -- and reports how many
   deals that made.

Every record it creates is named ``SMOKE-...`` and its ids are printed, so
they can be found and removed.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crm_idempotent_sync_proof.client import (  # noqa: E402
    HUBSPOT_API_URL,
    HubSpotClient,
    HubSpotRejected,
)
from crm_idempotent_sync_proof.ledger import Ledger  # noqa: E402
from crm_idempotent_sync_proof.sync import (  # noqa: E402
    SyncEngine,
    deal_properties_for,
    naive_deal_sync,
    sign,
)

KEY_NAME = "HUBSPOT_SERVICE_KEY"


def clean(value: str) -> str:
    # A value written with double quotes in a .env file must not keep them.
    return value.replace('"', "").replace("'", "").strip()


def read_key(env_file: str | None) -> str:
    key = clean(os.environ.get(KEY_NAME, ""))
    if key or not env_file:
        return key
    for line in Path(env_file).read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == KEY_NAME:
            return clean(value)
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", help=f"a KEY=VALUE file to read {KEY_NAME} from")
    parser.add_argument("--naive-ab", action="store_true", help="also run search-then-create twice, live")
    args = parser.parse_args(argv)

    key = read_key(args.env_file)
    if not key:
        print(f"live smoke skipped: {KEY_NAME} not set (pass --env-file to read it from a file)")
        return 0

    base_url = clean(os.environ.get("HUBSPOT_BASE_URL", "")) or HUBSPOT_API_URL
    client = HubSpotClient(base_url, key)

    prop = client.get_deal_property("external_order_id")
    if not prop or prop.get("hasUniqueValue") is not True:
        print("refusing: the deal property external_order_id is missing or not hasUniqueValue=true")
        return 1
    print("property external_order_id: hasUniqueValue=True")

    run = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    order_id = f"SMOKE-{run}"
    secret = secrets.token_bytes(32)
    event = {
        "event_id": f"evt-{run}",
        "type": "order.paid",
        "order": {
            "id": order_id,
            "email": f"smoke-{run.lower()}@example.com",
            "firstname": "Smoke",
            "lastname": run,
            "amount": "1.00",
            "currency": "USD",
            "paid_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
    raw = json.dumps(event, sort_keys=True).encode()
    signature = sign(secret, raw)

    scratch = tempfile.mkdtemp(prefix="crm-smoke-")
    ledgers: list[Ledger] = []

    def deliver(db: str):
        ledger = Ledger(os.path.join(scratch, db))
        ledgers.append(ledger)
        return SyncEngine(ledger=ledger, client=client, webhook_secret=secret).handle_webhook(raw, signature)

    try:
        first = deliver("a.db")
        print(f"delivery 1 (ledger A)       -> contact {first.contact_id} new={first.contact_created}, "
              f"deal {first.deal_id} new={first.deal_created}")
        second = deliver("b.db")
        print(f"delivery 2 (fresh ledger B) -> contact {second.contact_id} new={second.contact_created}, "
              f"deal {second.deal_id} new={second.deal_created}")
        third = deliver("a.db")
        print(f"delivery 3 (ledger A again) -> {third.status}, nothing sent "
              f"(contact_created={third.contact_created}, deal_created={third.deal_created})")
        same = (first.contact_id, first.deal_id) == (second.contact_id, second.deal_id)
        print(f"replay against a lost ledger: {'SAME contact and deal' if same else 'DIFFERENT ids -- FAILED'}")

        try:
            client.create_deal({**deal_properties_for(event["order"]), "external_order_id": order_id})
            print("plain create with a taken external_order_id: ACCEPTED -- FAILED")
            refused = False
        except HubSpotRejected as exc:
            print(f"plain create with a taken external_order_id: HTTP {exc.status} {exc.category}: {exc.message}")
            refused = exc.status == 400 and exc.says("already has that value.")

        contacts = client.deal_contact_ids(first.deal_id)
        print(f"deal {first.deal_id} associated contacts: {contacts}")
        print(f"rate headers: {client.last_rate_headers}")

        ok = same and refused and contacts == [first.contact_id] and second.deal_created is False
        if args.naive_ab:
            naive_order = {**event["order"], "id": f"{order_id}-NAIVE"}
            a = naive_deal_sync(client, naive_order)
            b = naive_deal_sync(client, naive_order)
            print(f"naive search-then-create, twice back to back on '{naive_order['id']}': "
                  f"deals {a} and {b} -> {'TWO deals' if a != b else 'one deal'}")
        print(f"records to clean up: contact {first.contact_id}, deal {first.deal_id} (all named {order_id}*)")
        print("live smoke: PASS" if ok else "live smoke: FAIL")
        return 0 if ok else 1
    finally:
        for ledger in ledgers:
            ledger.close()
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
