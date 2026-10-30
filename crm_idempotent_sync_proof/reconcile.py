"""Reconcile the ledger against HubSpot -- read-only, and never a repair.

For every order the ledger knows, one verdict:

* ``ok`` -- synced, and HubSpot agrees: the deal carrying our
  ``external_order_id`` has the id the ledger bound, and it is associated to
  the bound contact;
* ``pending`` -- the ledger has not finished the order (the last attempt was
  unknown, refused, or interrupted). ``resume`` settles it, and resending is
  safe;
* ``conflict`` -- the sync already refused to guess on this order;
* ``missing_in_hubspot`` -- no deal carries our ``external_order_id`` any
  more (someone deleted it);
* ``id_mismatch`` -- a deal carries it, but not the one the ledger bound
  (deleted and made again, by hand or by another integration);
* ``association_missing`` -- the deal is there, the contact is not
  associated to it.

The last three are reported with the ids on both sides and left alone. The
lookup is ``GET .../deals/{value}?idProperty=external_order_id``, not
search, because search lags behind writes and a reconcile run right after a
sync would report drift that is not there.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import HubSpotClient
from .ledger import DONE, STEPS, SYNC_CONFLICT, SYNC_SYNCED, Ledger
from .sync import external_order_id_for

OK = "ok"
PENDING = "pending"
CONFLICT = "conflict"
MISSING_IN_HUBSPOT = "missing_in_hubspot"
ID_MISMATCH = "id_mismatch"
ASSOCIATION_MISSING = "association_missing"


@dataclass
class Finding:
    order_id: str
    verdict: str
    detail: str


def reconcile(ledger: Ledger, client: HubSpotClient) -> list[Finding]:
    findings: list[Finding] = []
    for order in ledger.orders():
        oid = order.order_id
        if order.sync_status == SYNC_CONFLICT:
            findings.append(Finding(oid, CONFLICT, order.last_error or ""))
            continue
        if order.sync_status != SYNC_SYNCED:
            open_steps = [s for s in STEPS if ledger.intent(oid, s) != DONE]
            findings.append(Finding(
                oid, PENDING,
                f"status={order.sync_status}; steps not done: {', '.join(open_steps)}; "
                f"last error: {order.last_error or '-'}; run `resume`",
            ))
            continue
        external_id = external_order_id_for(order.order)
        deal = client.get_deal_by_external_order_id(external_id)
        if deal is None:
            findings.append(Finding(
                oid, MISSING_IN_HUBSPOT,
                f"ledger deal {order.hubspot_deal_id}; no HubSpot deal has external_order_id={external_id}",
            ))
            continue
        if str(deal["id"]) != order.hubspot_deal_id:
            findings.append(Finding(
                oid, ID_MISMATCH,
                f"ledger deal {order.hubspot_deal_id}; HubSpot deal with external_order_id={external_id} "
                f"is {deal['id']}",
            ))
            continue
        contacts = client.deal_contact_ids(str(deal["id"]))
        if order.hubspot_contact_id not in contacts:
            findings.append(Finding(
                oid, ASSOCIATION_MISSING,
                f"deal {deal['id']} is not associated to contact {order.hubspot_contact_id} "
                f"(associated: {', '.join(contacts) or 'none'})",
            ))
            continue
        findings.append(Finding(oid, OK, f"deal {deal['id']} -> contact {order.hubspot_contact_id}"))
    return findings
