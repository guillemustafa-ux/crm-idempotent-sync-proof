"""The operational ledger: what was received, what was sent, and what HubSpot answered.

One paid order becomes three writes against HubSpot -- the contact, the
deal, the association between them. Each has the same gap: HubSpot commits,
the process dies before the id is written down. The ledger's job here is
narrower than it would be against an API without upserts, and it is worth
saying exactly what it is:

* the **duplicate is prevented by HubSpot**, not by this file. Every write is
  an upsert by a property HubSpot enforces as unique (``email``,
  ``external_order_id``), or the idempotent default association. Resending
  any step after any crash finds the record the first send made;
* what the ledger adds is the **operational record**: which orders arrived
  (and how many times), the order exactly as first received (so a later
  delivery that disagrees is refused rather than silently applied), the
  HubSpot ids each order is bound to, and an **intent row committed before
  each send**, so a process killed mid-call wakes up knowing a call was in
  flight, and reconciliation can tell "pending" from "done";
* ``synchronous=FULL`` so "committed" means committed, not "in the OS page
  cache when the power went out";
* a deal id may be bound to **one** order only (a unique index), a binding is
  never silently overwritten with a different id, and ``CHECK`` constraints
  refuse an "associated" or "synced" order without both ids -- rejected by the
  database even if the code is wrong.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

# Steps, in the order they are applied.
STEP_CONTACT = "contact"
STEP_DEAL = "deal"
STEP_ASSOCIATION = "association"
STEPS = (STEP_CONTACT, STEP_DEAL, STEP_ASSOCIATION)

# Intent lifecycle.
PENDING = "pending"  # sent, or about to be; fate unknown until it is DONE
DONE = "done"  # HubSpot's answer is recorded

# Order sync status.
SYNC_PENDING = "pending"  # received; not every step is done yet
SYNC_SYNCED = "synced"  # contact, deal and association all recorded
SYNC_UNKNOWN = "unknown"  # the last attempt's outcome is unknown; resend is safe
SYNC_ERROR = "error"  # HubSpot provably refused the last attempt
SYNC_CONFLICT = "conflict"  # the sync refused to guess; a human decides


class ConflictingRedelivery(Exception):
    """The same order id arrived again with different content. Nothing was written."""

    def __init__(self, order_id: str, recorded_sha: str, received_sha: str):
        super().__init__(
            f"order {order_id} was first received with content sha256 {recorded_sha[:12]} "
            f"and now with {received_sha[:12]}; refusing to pick one"
        )
        self.order_id = order_id
        self.recorded_sha = recorded_sha
        self.received_sha = received_sha


class LedgerConflict(Exception):
    """HubSpot answered with an id that disagrees with the one already bound. Nothing was rebound."""


def canonical(order: dict[str, Any]) -> str:
    return json.dumps(order, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_sha(order: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(order).encode("utf-8")).hexdigest()


@dataclass
class Order:
    order_id: str
    order: dict[str, Any]
    payload_sha256: str
    email: str
    hubspot_contact_id: str | None
    hubspot_deal_id: str | None
    associated: bool
    deliveries: int
    sync_status: str
    last_error: str | None


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS orders (
    order_id            TEXT PRIMARY KEY,
    payload             TEXT NOT NULL,
    payload_sha256      TEXT NOT NULL,
    email               TEXT NOT NULL,
    hubspot_contact_id  TEXT,
    hubspot_deal_id     TEXT,
    associated          INTEGER NOT NULL DEFAULT 0 CHECK (associated IN (0, 1)),
    deliveries          INTEGER NOT NULL DEFAULT 1,
    sync_status         TEXT NOT NULL,
    last_error          TEXT,
    CHECK (associated = 0 OR (hubspot_contact_id IS NOT NULL AND hubspot_deal_id IS NOT NULL)),
    CHECK (sync_status <> '{SYNC_SYNCED}' OR associated = 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_one_order_per_deal ON orders (hubspot_deal_id);
CREATE TABLE IF NOT EXISTS intents (
    order_id  TEXT NOT NULL REFERENCES orders (order_id),
    step      TEXT NOT NULL,
    state     TEXT NOT NULL,
    PRIMARY KEY (order_id, step)
);
"""


class Ledger:
    def __init__(self, path: str):
        self.path = path
        # Autocommit mode; every multi-statement change opens its own
        # ``BEGIN IMMEDIATE`` so two workers on one file serialise cleanly.
        self._conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _tx(self):
        return _Transaction(self._conn)

    # -- deliveries -----------------------------------------------------------

    def record_delivery(self, order: dict[str, Any]) -> Order:
        """Record that ``order`` arrived. Idempotent for identical content.

        A redelivery with **different** content for the same order id is
        refused with ``ConflictingRedelivery`` and changes nothing: the sync
        will not decide which of two versions of an order is the real one.
        """
        order_id = str(order["id"])
        sha = content_sha(order)
        with self._tx():
            row = self._conn.execute(
                "SELECT payload_sha256 FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO orders (order_id, payload, payload_sha256, email, sync_status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (order_id, canonical(order), sha, str(order["email"]), SYNC_PENDING),
                )
            elif row[0] != sha:
                raise ConflictingRedelivery(order_id, row[0], sha)
            else:
                self._conn.execute(
                    "UPDATE orders SET deliveries = deliveries + 1 WHERE order_id = ?", (order_id,)
                )
        order_row = self.get_order(order_id)
        assert order_row is not None
        return order_row

    # -- intents and bindings -------------------------------------------------

    def begin(self, order_id: str, step: str) -> None:
        """Commit the intent to send ``step`` -- before the first byte leaves."""
        self._conn.execute(
            "INSERT INTO intents (order_id, step, state) VALUES (?, ?, ?) "
            "ON CONFLICT (order_id, step) DO UPDATE SET state = excluded.state",
            (order_id, step, PENDING),
        )

    def _bind(self, order_id: str, step: str, column: str, value: str) -> None:
        with self._tx():
            try:
                cur = self._conn.execute(
                    f"UPDATE orders SET {column} = ? "
                    f"WHERE order_id = ? AND ({column} IS NULL OR {column} = ?)",
                    (value, order_id, value),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerConflict(
                    f"order {order_id}: HubSpot {step} {value} is already bound to another order"
                ) from exc
            if cur.rowcount != 1:
                bound = self._conn.execute(
                    f"SELECT {column} FROM orders WHERE order_id = ?", (order_id,)
                ).fetchone()
                raise LedgerConflict(
                    f"order {order_id}: HubSpot answered {step} {value}, but the ledger is bound "
                    f"to {bound[0] if bound else None}; not rebinding"
                )
            self._done(order_id, step)

    def _done(self, order_id: str, step: str) -> None:
        self._conn.execute(
            "UPDATE intents SET state = ? WHERE order_id = ? AND step = ?", (DONE, order_id, step)
        )

    def bind_contact(self, order_id: str, contact_id: str) -> None:
        self._bind(order_id, STEP_CONTACT, "hubspot_contact_id", contact_id)

    def bind_deal(self, order_id: str, deal_id: str) -> None:
        self._bind(order_id, STEP_DEAL, "hubspot_deal_id", deal_id)

    def mark_associated(self, order_id: str) -> None:
        with self._tx():
            self._conn.execute("UPDATE orders SET associated = 1 WHERE order_id = ?", (order_id,))
            self._done(order_id, STEP_ASSOCIATION)

    def mark_synced(self, order_id: str) -> None:
        self._conn.execute(
            "UPDATE orders SET sync_status = ?, last_error = NULL WHERE order_id = ?",
            (SYNC_SYNCED, order_id),
        )

    def record_error(self, order_id: str, status: str, message: str) -> None:
        self._conn.execute(
            "UPDATE orders SET sync_status = ?, last_error = ? WHERE order_id = ?",
            (status, message, order_id),
        )

    # -- reads ------------------------------------------------------------------

    def get_order(self, order_id: str) -> Order | None:
        row = self._conn.execute(
            "SELECT order_id, payload, payload_sha256, email, hubspot_contact_id, hubspot_deal_id, "
            "associated, deliveries, sync_status, last_error FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        return Order(row[0], json.loads(row[1]), row[2], row[3], row[4], row[5],
                     bool(row[6]), row[7], row[8], row[9])

    def orders(self) -> list[Order]:
        ids = [r[0] for r in self._conn.execute("SELECT order_id FROM orders ORDER BY order_id")]
        return [o for o in (self.get_order(i) for i in ids) if o is not None]

    def unsynced(self) -> list[Order]:
        return [o for o in self.orders() if o.sync_status != SYNC_SYNCED]

    def intent(self, order_id: str, step: str) -> str | None:
        row = self._conn.execute(
            "SELECT state FROM intents WHERE order_id = ? AND step = ?", (order_id, step)
        ).fetchone()
        return row[0] if row else None


class _Transaction:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, exc, tb) -> None:
        self._conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
