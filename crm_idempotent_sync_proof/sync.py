"""The sync: a signed "order paid" webhook becomes a HubSpot contact, a deal and the association between them.

    verify HMAC  ->  record delivery  ->  upsert contact by email
                 ->  upsert deal by external_order_id  ->  default association deal->contact

Every arrow after the signature is safe to repeat, and that is the design:

* the **contact** is an upsert with ``idProperty: email``;
* the **deal** is an upsert with ``idProperty: external_order_id``, a custom
  property created with ``hasUniqueValue: true``. HubSpot enforces the
  uniqueness "across all records of that object type", so two deliveries of
  one order -- replayed, retried after a crash, or racing on two workers --
  meet the same deal, and the second is answered ``new: false``;
* the **association** is ``PUT .../associations/default/...``, which twice is
  still one association.

So a resend is the recovery. There is no "did it land?" question to answer
before resending, because the resend is answered with the record the first
send made. What the design does **not** do is the obvious thing -- search
for the order, create it if not found -- because HubSpot's search lags
(measured live: 10.08 s), and inside that window "not found" is a lie. The
``naive_deal_sync`` below is that anti-pattern, kept so the tests can show
it duplicating.

The ledger records the intent before each send (so a killed process wakes up
knowing what was in flight) and the ids HubSpot answered (so the order can
be reconciled). An order already synced is not sent again: a replayed
webhook for it is a no-op.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .client import HubSpotClient, HubSpotRejected, UnknownOutcome
from .ledger import (
    STEP_ASSOCIATION,
    STEP_CONTACT,
    STEP_DEAL,
    SYNC_CONFLICT,
    SYNC_ERROR,
    SYNC_SYNCED,
    SYNC_UNKNOWN,
    Ledger,
    LedgerConflict,
    Order,
)

log = logging.getLogger("crm_idempotent_sync_proof")

SIGNATURE_HEADER = "X-Signature-SHA256"

# The named instants a crash can be scheduled at (the crash child) or another
# worker can be run at (the race tests).
SEAMS = (
    "before_contact_send",
    "after_contact_response",
    "before_deal_send",
    "after_deal_response",
    "before_association_send",
    "after_association_response",
)


class BadSignature(Exception):
    """The webhook's HMAC does not match its body. Nothing was read, recorded or sent."""


class UnsupportedEvent(Exception):
    """A correctly signed event this sync does not handle. Nothing was recorded or sent."""


def sign(secret: bytes, body: bytes) -> str:
    """``sha256=<hex HMAC-SHA256 of the raw body>``: what the source puts in ``X-Signature-SHA256``."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify_signature(secret: bytes | None, body: bytes, signature: str | None) -> bool:
    """Constant-time comparison over the **raw bytes**, before anything parses them."""
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign(secret, body), signature)


def external_order_id_for(order: dict[str, Any]) -> str:
    """The value planted in the unique ``external_order_id``: the source's own order id.

    Derived from the order, never generated at delivery time -- a value that
    changes on redelivery is not a key at all.
    """
    return str(order["id"])


def deal_name_for(order: dict[str, Any]) -> str:
    return f"Order {order['id']}"


def contact_properties_for(order: dict[str, Any]) -> dict[str, str]:
    return {"firstname": str(order.get("firstname", "")), "lastname": str(order.get("lastname", ""))}


def deal_properties_for(order: dict[str, Any]) -> dict[str, str]:
    return {
        "dealname": deal_name_for(order),
        "amount": str(order["amount"]),
        "pipeline": "default",
        "dealstage": "closedwon",
    }


@dataclass
class SyncResult:
    order_id: str
    status: str
    contact_id: str | None
    deal_id: str | None
    # What HubSpot said about this run's upserts: True = made now,
    # False = found the record an earlier send made, None = not sent this run.
    contact_created: bool | None = None
    deal_created: bool | None = None


class SyncEngine:
    def __init__(
        self,
        *,
        ledger: Ledger,
        client: HubSpotClient,
        webhook_secret: bytes | None = None,
        on_seam: Callable[[str], None] | None = None,
    ):
        self.ledger = ledger
        self.client = client
        self.webhook_secret = webhook_secret
        self._on_seam = on_seam

    def _seam(self, name: str) -> None:
        if self._on_seam is not None:
            self._on_seam(name)

    # -- entry points -----------------------------------------------------------

    def handle_webhook(self, raw_body: bytes, signature: str | None) -> SyncResult:
        """The receiver: verify, record, sync. A redelivery goes through exactly the same path.

        Raises ``BadSignature`` / ``UnsupportedEvent`` (nothing written
        anywhere), ``ConflictingRedelivery`` (nothing written), or the
        sync's own ``UnknownOutcome`` / ``HubSpotRejected`` /
        ``LedgerConflict`` after recording them on the order. An HTTP
        receiver answers 2xx only when this returns, so the source redelivers
        everything else.
        """
        if not verify_signature(self.webhook_secret, raw_body, signature):
            raise BadSignature("the X-Signature-SHA256 header does not match the body")
        event = json.loads(raw_body)
        if event.get("type") != "order.paid":
            raise UnsupportedEvent(f"event type {event.get('type')!r} is not handled")
        order = self.ledger.record_delivery(event["order"])
        return self.sync(order.order_id)

    def resume(self) -> dict[str, str]:
        """Settle every order that is not synced, from the payload recorded on arrival.

        Safe to run at any time, as often as you like: every step is an
        upsert or an idempotent PUT. One order's failure is recorded on its
        row and the walk continues. Orders the sync refused to guess about
        (``conflict``) are left for a human.
        """
        outcome: dict[str, str] = {}
        for order in self.ledger.unsynced():
            if order.sync_status == SYNC_CONFLICT:
                outcome[order.order_id] = SYNC_CONFLICT
                continue
            try:
                outcome[order.order_id] = self.sync(order.order_id).status
            except Exception as exc:  # noqa: BLE001 -- recorded, the walk goes on
                outcome[order.order_id] = f"{type(exc).__name__}: {exc}"
        return outcome

    def sync(self, order_id: str) -> SyncResult:
        order = self.ledger.get_order(order_id)
        if order is None:
            raise KeyError(order_id)
        if order.sync_status == SYNC_SYNCED:
            log.info("order %s already synced -> nothing sent", order_id)
            return SyncResult(order_id, SYNC_SYNCED, order.hubspot_contact_id, order.hubspot_deal_id)
        try:
            return self._sync(order)
        except UnknownOutcome as exc:
            self.ledger.record_error(order_id, SYNC_UNKNOWN, str(exc))
            raise
        except LedgerConflict as exc:
            self.ledger.record_error(order_id, SYNC_CONFLICT, str(exc))
            raise
        except HubSpotRejected as exc:
            self.ledger.record_error(order_id, SYNC_ERROR, str(exc))
            raise

    # -- the steps --------------------------------------------------------------

    def _sync(self, order: Order) -> SyncResult:
        oid = order.order_id
        result = SyncResult(oid, SYNC_SYNCED, order.hubspot_contact_id, order.hubspot_deal_id)

        if result.contact_id is None:
            self.ledger.begin(oid, STEP_CONTACT)
            self._seam("before_contact_send")
            contact = self.client.upsert_contact(order.email, contact_properties_for(order.order))
            self._seam("after_contact_response")
            self.ledger.bind_contact(oid, contact.id)
            result.contact_id, result.contact_created = contact.id, contact.new
            log.info("order %s contact by email -> %s (id=%s)", oid,
                     "CREATED" if contact.new else "FOUND (new=false)", contact.id)

        if result.deal_id is None:
            self.ledger.begin(oid, STEP_DEAL)
            self._seam("before_deal_send")
            deal = self.client.upsert_deal(
                external_order_id_for(order.order), deal_properties_for(order.order)
            )
            self._seam("after_deal_response")
            self.ledger.bind_deal(oid, deal.id)
            result.deal_id, result.deal_created = deal.id, deal.new
            log.info("order %s deal by external_order_id -> %s (id=%s)", oid,
                     "CREATED" if deal.new else "FOUND (new=false)", deal.id)

        if not order.associated:
            self.ledger.begin(oid, STEP_ASSOCIATION)
            self._seam("before_association_send")
            assert result.contact_id is not None and result.deal_id is not None
            self.client.associate_deal_to_contact(result.deal_id, result.contact_id)
            self._seam("after_association_response")
            self.ledger.mark_associated(oid)

        self.ledger.mark_synced(oid)
        return result


# -- the anti-pattern -------------------------------------------------------------


def naive_deal_sync(
    client: HubSpotClient,
    order: dict[str, Any],
    *,
    plant_unique_id: bool = False,
    on_seam: Callable[[str], None] | None = None,
) -> str:
    """Search for the order's deal; create it if the search finds nothing.

    The code most integrations start with, and it reads as correct. It is
    correct only once HubSpot's search index has caught up -- about ten
    seconds after the write, measured live. A redelivery, a retry after a
    crash, or a second worker inside that window searches, finds nothing,
    and creates a second deal.

    With ``plant_unique_id=False`` the order id lives only in ``dealname``,
    which nothing enforces: the duplicate is created. With
    ``plant_unique_id=True`` it is also written to the unique
    ``external_order_id``, and HubSpot refuses the second create with a
    ``400`` "already has that value." -- no duplicate, but a delivery that
    fails instead of succeeding.
    """
    if plant_unique_id:
        prop, value = "external_order_id", external_order_id_for(order)
    else:
        prop, value = "dealname", deal_name_for(order)
    found = client.search_deals(prop, value)
    if found:
        return str(found[0]["id"])
    if on_seam is not None:
        on_seam("after_search")
    properties = deal_properties_for(order)
    if plant_unique_id:
        properties["external_order_id"] = value
    return str(client.create_deal(properties)["id"])
