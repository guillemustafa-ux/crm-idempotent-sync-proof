"""A local HubSpot that speaks real HTTP over a real socket.

It encodes what was verified against a live HubSpot portal on 2026-10-30,
and as little else as the proof needs:

* ``POST /crm/objects/2026-09/contacts/batch/upsert`` with
  ``idProperty: email`` -- the first call answers ``new: true``, the second
  the same ``id`` with ``new: false``;
* ``POST /crm/objects/2026-09/deals/batch/upsert`` with
  ``idProperty: external_order_id`` (a custom property created with
  ``hasUniqueValue: true``) -- the same semantics;
* a plain ``POST /crm/objects/2026-09/deals`` carrying a unique value
  another deal already holds is a ``400`` ``VALIDATION_ERROR``, "Cannot set
  PropertyValueCoordinates{...} on <id>. <existing id> already has that
  value." -- and the ``<id>`` it names does **not** exist afterwards;
* ``GET /crm/objects/2026-09/deals/{value}?idProperty=external_order_id``
  answers ``200``; an id that does not exist answers ``404``;
* ``POST /crm/objects/2026-09/deals/search`` lags: a freshly created deal
  first appeared in search after 10.08 s live. Here a new object is
  invisible to search until ``search_lag`` seconds of the fake's clock have
  passed (10 s by default; the tests step a frozen clock);
* ``PUT /crm/v4/objects/deals/{id}/associations/default/contacts/{id}`` is
  idempotent: twice is still exactly one association (type 3 deal->contact,
  4 back). ``/crm/associations/2026-09/...`` answers ``404``;
* every response carries ``X-HubSpot-RateLimit-Daily`` (250000), ``-Max``
  (100), ``-Interval-Milliseconds`` (10000) and ``-Secondly`` (10).
  **No ``Retry-After``**: none is documented and none was seen.

Beyond what ran live, each modelled on the side that makes the client's job
harder and listed in the README: the bodies of the ``401``, ``404`` and
``429`` (HubSpot's documented ``status``/``message``/``category`` shape,
``errorType: RATE_LIMIT`` and ``policyName`` on the 429 -- no 429 was
provoked live), the shape of the association responses, and a ``400`` for
an ``idProperty`` that is not a unique property.

Faults are injectable at the exact seams that matter: a ``429`` before any
route runs, a gateway ``5xx``, and a connection dropped **before** or
**after** the server committed -- the "outcome unknown" case.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, NamedTuple
from urllib.parse import parse_qs, urlparse

API_VERSION = "2026-09"

# The bearer the fake accepts. Not a secret and deliberately not shaped like
# a real HubSpot key.
FAKE_SERVICE_KEY = "fake-service-key-for-the-local-proof"

# Measured live: 10.08 s until a fresh deal appeared in search (attempt 11,
# polled every 0.25 s). The default rounds it; tests step a frozen clock.
SEARCH_LAG_SECONDS = 10.0

# The properties HubSpot enforces as unique, per object type. ``email`` on
# contacts; on deals, the custom ``external_order_id`` created with
# ``hasUniqueValue: true``. Everything else -- ``dealname`` included -- is
# just a value.
UNIQUE_PROPERTIES: dict[str, tuple[str, ...]] = {
    "contacts": ("email",),
    "deals": ("external_order_id",),
}

# Sent on every response, as seen live.
RATE_LIMIT_HEADERS = {
    "X-HubSpot-RateLimit-Daily": "250000",
    "X-HubSpot-RateLimit-Max": "100",
    "X-HubSpot-RateLimit-Interval-Milliseconds": "10000",
    "X-HubSpot-RateLimit-Secondly": "10",
}

# Routes a fault can be armed on.
ROUTE_CONTACT_UPSERT = "contacts.upsert"
ROUTE_DEAL_UPSERT = "deals.upsert"
ROUTE_DEAL_CREATE = "deals.create"
ROUTE_DEAL_SEARCH = "deals.search"
ROUTE_DEAL_GET = "deals.get"
ROUTE_ASSOCIATION = "association"
ROUTE_ASSOCIATION_LIST = "association.list"
ROUTE_PROPERTY = "property"
ROUTE_UNKNOWN = "unknown"

FAULT_BEFORE_COMMIT = "before_commit"  # drop the wire; nothing was committed
FAULT_AFTER_COMMIT = "after_commit"  # committed; drop the wire before answering

ASSOCIATION_DEAL_TO_CONTACT = 3
ASSOCIATION_CONTACT_TO_DEAL = 4


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _error(message: str, category: str, **extra: Any) -> dict[str, Any]:
    """HubSpot's documented error shape."""
    return {
        "status": "error",
        "message": message,
        "correlationId": str(uuid.uuid4()),
        "category": category,
        **extra,
    }


class _Reply(NamedTuple):
    status: int
    payload: Any
    detail: str = ""
    cut_before: bool = False
    cut_after: bool = False


@dataclass
class _Record:
    id: str
    properties: dict[str, str]
    created_at: float
    updated_at: float
    archived: bool = False

    def as_json(self, *, new: bool | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "properties": {**self.properties, "hs_object_id": self.id},
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
            "archived": self.archived,
        }
        if new is not None:
            out["new"] = new
        return out


@dataclass
class HubSpotState:
    clock: Callable[[], float]
    search_lag: float = SEARCH_LAG_SECONDS
    objects: dict[str, dict[str, _Record]] = field(
        default_factory=lambda: {"contacts": {}, "deals": {}}
    )
    # (deal_id, contact_id); the reverse direction is implied.
    associations: set[tuple[str, str]] = field(default_factory=set)
    next_id: int = 900001
    armed: list[tuple[str, str]] = field(default_factory=list)
    # One entry per armed 429: the route it applies to, or None for any.
    throttles: list[str | None] = field(default_factory=list)
    server_errors: list[tuple[str | None, int]] = field(default_factory=list)
    request_log: list[str] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def now(self) -> float:
        return self.clock()

    def new_id(self) -> str:
        value = str(self.next_id)
        self.next_id += 1
        return value

    def take_server_error(self, route: str) -> int | None:
        for i, (target, status) in enumerate(self.server_errors):
            if target is None or target == route:
                del self.server_errors[i]
                return status
        return None

    def take_fault(self, route: str) -> str | None:
        for i, (target, fault) in enumerate(self.armed):
            if target == route:
                del self.armed[i]
                return fault
        return None

    def live(self, object_type: str) -> list[_Record]:
        return [r for r in self.objects[object_type].values() if not r.archived]

    def find(self, object_type: str, prop: str, value: str) -> _Record | None:
        for record in self.live(object_type):
            if record.properties.get(prop) == value:
                return record
        return None

    def create(self, object_type: str, properties: dict[str, str], record_id: str | None = None) -> _Record:
        now = self.now()
        record = _Record(record_id or self.new_id(), dict(properties), now, now)
        self.objects[object_type][record.id] = record
        return record


_UPSERT = re.compile(rf"^/crm/objects/{API_VERSION}/(contacts|deals)/batch/upsert$")
_CREATE = re.compile(rf"^/crm/objects/{API_VERSION}/deals$")
_SEARCH = re.compile(rf"^/crm/objects/{API_VERSION}/deals/search$")
_GET = re.compile(rf"^/crm/objects/{API_VERSION}/deals/([^/]+)$")
_ASSOC_PUT = re.compile(r"^/crm/v4/objects/deals/([^/]+)/associations/default/contacts/([^/]+)$")
_ASSOC_LIST = re.compile(r"^/crm/v4/objects/deals/([^/]+)/associations/contacts$")
_PROPERTY = re.compile(rf"^/crm/properties/{API_VERSION}/deals/([^/]+)$")


def route_of(method: str, path: str) -> str:
    m = _UPSERT.match(path)
    if m and method == "POST":
        return ROUTE_CONTACT_UPSERT if m.group(1) == "contacts" else ROUTE_DEAL_UPSERT
    if _CREATE.match(path) and method == "POST":
        return ROUTE_DEAL_CREATE
    if _SEARCH.match(path) and method == "POST":
        return ROUTE_DEAL_SEARCH
    if _GET.match(path) and method == "GET":
        return ROUTE_DEAL_GET
    if _ASSOC_PUT.match(path) and method == "PUT":
        return ROUTE_ASSOCIATION
    if _ASSOC_LIST.match(path) and method == "GET":
        return ROUTE_ASSOCIATION_LIST
    if _PROPERTY.match(path) and method == "GET":
        return ROUTE_PROPERTY
    return ROUTE_UNKNOWN


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> HubSpotState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stderr
        pass

    def _read_body(self) -> bytes:
        # The request body must be consumed before any early reply (401,
        # 429): closing a socket with unread request bytes makes the OS
        # answer with RST, and on Windows the client then sees
        # ConnectionAbortedError instead of the status that was sent.
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json;charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in RATE_LIMIT_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cut_the_wire(self) -> None:
        """Drop the connection with no response at all.

        The caller sees a transport error. It cannot tell "the request never
        landed" from "the request landed and the answer was lost" -- which is
        the problem this repo is about.
        """
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
        except OSError:
            pass

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        raw = self._read_body()
        route = route_of(method, parsed.path)
        state = self.state
        with state._lock:
            if self.headers.get("Authorization") != f"Bearer {FAKE_SERVICE_KEY}":
                state.request_log.append(f"401 {method} {route}")
                self._send_json(
                    401,
                    _error(
                        "Authentication credentials not found.",
                        "INVALID_AUTHENTICATION",
                    ),
                )
                return
            if self._throttled(route):
                state.request_log.append(f"429 {method} {route}")
                self._send_json(
                    429,
                    _error(
                        "You have reached your ten_secondly_rolling limit.",
                        "RATE_LIMITS",
                        errorType="RATE_LIMIT",
                        policyName="TEN_SECONDLY_ROLLING",
                    ),
                )
                return
            status = state.take_server_error(route)
            if status is not None:
                state.request_log.append(f"{status} {method} {route}")
                body = f"<html><body><h1>{status}</h1></body></html>".encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = None
            reply = self._dispatch(method, route, parsed.path, parse_qs(parsed.query), body)
            if reply.cut_before:
                state.request_log.append(f"CUT-BEFORE {method} {route}")
                self._cut_the_wire()
                return
            if reply.cut_after:
                state.request_log.append(f"CUT-AFTER {method} {route} {reply.detail}".rstrip())
                self._cut_the_wire()
                return
            state.request_log.append(f"{reply.status} {method} {route} {reply.detail}".rstrip())
            self._send_json(reply.status, reply.payload)

    def _throttled(self, route: str) -> bool:
        for i, target in enumerate(self.state.throttles):
            if target is None or target == route:
                del self.state.throttles[i]
                return True
        return False

    # -- routes -------------------------------------------------------------

    def _dispatch(
        self, method: str, route: str, path: str, query: dict[str, list[str]], body: Any
    ) -> _Reply:
        if body is None:
            return _Reply(400, _error("Invalid input JSON", "VALIDATION_ERROR"))
        if route in (ROUTE_CONTACT_UPSERT, ROUTE_DEAL_UPSERT):
            return self._upsert("contacts" if route == ROUTE_CONTACT_UPSERT else "deals", route, body)
        if route == ROUTE_DEAL_CREATE:
            return self._create_deal(body)
        if route == ROUTE_DEAL_SEARCH:
            return self._search_deals(body)
        if route == ROUTE_DEAL_GET:
            return self._get_deal(_GET.match(path).group(1), query)  # type: ignore[union-attr]
        if route == ROUTE_ASSOCIATION:
            m = _ASSOC_PUT.match(path)
            return self._associate(m.group(1), m.group(2))  # type: ignore[union-attr]
        if route == ROUTE_ASSOCIATION_LIST:
            return self._associations_of(_ASSOC_LIST.match(path).group(1))  # type: ignore[union-attr]
        if route == ROUTE_PROPERTY:
            return self._property(_PROPERTY.match(path).group(1))  # type: ignore[union-attr]
        # Seen live: the unversioned-v4 association path under
        # /crm/associations/2026-09/ is a 404.
        return _Reply(404, _error(f"No route for {method} {path}", "OBJECT_NOT_FOUND"))

    def _upsert(self, object_type: str, route: str, body: dict[str, Any]) -> _Reply:
        state = self.state
        inputs = body.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            return _Reply(400, _error("inputs must be a non-empty list", "VALIDATION_ERROR"))
        for item in inputs:
            id_property = item.get("idProperty")
            if id_property not in UNIQUE_PROPERTIES[object_type]:
                # Assumption: the exact answer for a non-unique idProperty was
                # not tried live. What matters is that it is not an upsert.
                return _Reply(
                    400,
                    _error(
                        f"{id_property!r} is not a unique property of {object_type}",
                        "VALIDATION_ERROR",
                    ),
                )
            if not isinstance(item.get("id"), str) or not item["id"]:
                return _Reply(400, _error("every input needs an id", "VALIDATION_ERROR"))

        fault = state.take_fault(route)
        if fault == FAULT_BEFORE_COMMIT:
            return _Reply(0, None, cut_before=True)

        results = []
        details = []
        started = state.now()
        for item in inputs:
            id_property, value = item["idProperty"], item["id"]
            properties = {k: str(v) for k, v in (item.get("properties") or {}).items()}
            existing = state.find(object_type, id_property, value)
            if existing is not None:
                existing.properties.update(properties)
                existing.properties[id_property] = value
                existing.updated_at = state.now()
                results.append(existing.as_json(new=False))
                details.append(f"new=false id={existing.id}")
            else:
                record = state.create(object_type, {**properties, id_property: value})
                results.append(record.as_json(new=True))
                details.append(f"new=true id={record.id}")
        payload = {
            "status": "COMPLETE",
            "results": results,
            "startedAt": _iso(started),
            "completedAt": _iso(state.now()),
        }
        return _Reply(200, payload, " ".join(details), cut_after=fault == FAULT_AFTER_COMMIT)

    def _create_deal(self, body: dict[str, Any]) -> _Reply:
        state = self.state
        properties = {k: str(v) for k, v in (body.get("properties") or {}).items()}
        for prop in UNIQUE_PROPERTIES["deals"]:
            value = properties.get(prop)
            if value is None:
                continue
            holder = state.find("deals", prop, value)
            if holder is not None:
                # Seen live: the message names an id for the would-be deal,
                # and that id does not exist afterwards (GET 404, no ghost in
                # search). The coordinates' other fields are omitted here.
                ghost = state.new_id()
                return _Reply(
                    400,
                    _error(
                        f"Cannot set PropertyValueCoordinates{{propertyName={prop}, value={value}}} "
                        f"on {ghost}. {holder.id} already has that value.",
                        "VALIDATION_ERROR",
                    ),
                    f"duplicate {prop}={value}",
                )
        fault = state.take_fault(ROUTE_DEAL_CREATE)
        if fault == FAULT_BEFORE_COMMIT:
            return _Reply(0, None, cut_before=True)
        record = state.create("deals", properties)
        return _Reply(201, record.as_json(), f"id={record.id}", cut_after=fault == FAULT_AFTER_COMMIT)

    def _search_deals(self, body: dict[str, Any]) -> _Reply:
        """``EQ`` filters only; a deal is invisible until ``search_lag`` has passed since it was created."""
        state = self.state
        groups = body.get("filterGroups") or []
        indexed = [
            r for r in state.live("deals") if state.now() - r.created_at >= state.search_lag
        ]

        def matches(record: _Record, group: dict[str, Any]) -> bool:
            for f in group.get("filters") or []:
                if f.get("operator") != "EQ":
                    return False
                if record.properties.get(f.get("propertyName")) != str(f.get("value")):
                    return False
            return True

        found = [r for r in indexed if any(matches(r, g) for g in groups)] if groups else indexed
        return _Reply(
            200,
            {"total": len(found), "results": [r.as_json() for r in found]},
            f"total={len(found)}",
        )

    def _get_deal(self, ident: str, query: dict[str, list[str]]) -> _Reply:
        state = self.state
        id_property = (query.get("idProperty") or [None])[0]
        if id_property:
            record = state.find("deals", id_property, ident)
        else:
            record = state.objects["deals"].get(ident)
            if record is not None and record.archived:
                record = None
        if record is None:
            return _Reply(404, _error("Object not found.", "OBJECT_NOT_FOUND"), "absent")
        return _Reply(200, record.as_json(), f"id={record.id}")

    def _associate(self, deal_id: str, contact_id: str) -> _Reply:
        state = self.state
        deal = state.objects["deals"].get(deal_id)
        contact = state.objects["contacts"].get(contact_id)
        if deal is None or deal.archived or contact is None or contact.archived:
            return _Reply(404, _error("Object not found.", "OBJECT_NOT_FOUND"), "absent")
        fault = state.take_fault(ROUTE_ASSOCIATION)
        if fault == FAULT_BEFORE_COMMIT:
            return _Reply(0, None, cut_before=True)
        state.associations.add((deal_id, contact_id))  # a set: twice is once

        def spec(type_id: int) -> dict[str, Any]:
            return {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}

        payload = {
            "status": "COMPLETE",
            "results": [
                {"from": {"id": deal_id}, "to": {"id": contact_id},
                 "associationSpec": spec(ASSOCIATION_DEAL_TO_CONTACT)},
                {"from": {"id": contact_id}, "to": {"id": deal_id},
                 "associationSpec": spec(ASSOCIATION_CONTACT_TO_DEAL)},
            ],
        }
        return _Reply(200, payload, f"{deal_id}->{contact_id}", cut_after=fault == FAULT_AFTER_COMMIT)

    def _associations_of(self, deal_id: str) -> _Reply:
        contacts = sorted(c for d, c in self.state.associations if d == deal_id)
        return _Reply(
            200,
            {
                "results": [
                    {
                        "toObjectId": int(c),
                        "associationTypes": [
                            {"category": "HUBSPOT_DEFINED",
                             "typeId": ASSOCIATION_DEAL_TO_CONTACT, "label": None}
                        ],
                    }
                    for c in contacts
                ]
            },
            f"count={len(contacts)}",
        )

    def _property(self, name: str) -> _Reply:
        if name not in UNIQUE_PROPERTIES["deals"]:
            return _Reply(404, _error(f"Unable to find property {name}", "OBJECT_NOT_FOUND"), "absent")
        return _Reply(
            200,
            {
                "name": name,
                "label": "External order id",
                "type": "string",
                "fieldType": "text",
                "groupName": "dealinformation",
                "hasUniqueValue": True,
            },
            name,
        )


class FakeHubSpot:
    """The server, its state, and the knobs a test turns."""

    def __init__(self, *, clock: Callable[[], float] | None = None, search_lag: float = SEARCH_LAG_SECONDS):
        self._offset = 0.0
        base_clock = clock or time.time
        self.state = HubSpotState(clock=lambda: base_clock() + self._offset, search_lag=search_lag)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.state = self.state  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "FakeHubSpot":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def service_key(self) -> str:
        return FAKE_SERVICE_KEY

    def now(self) -> float:
        return self.state.now()

    def advance(self, seconds: float) -> None:
        """Move the fake's clock forward; search catches up with what it has not indexed yet."""
        with self.state._lock:
            self._offset += seconds

    # -- fault injection -------------------------------------------------------

    def drop_response_after_commit(self, route: str) -> None:
        """Commit the next write on ``route``, then drop the connection: the outcome is unknown to the caller."""
        with self.state._lock:
            self.state.armed.append((route, FAULT_AFTER_COMMIT))

    def drop_response_before_commit(self, route: str) -> None:
        """Drop the connection before the next write on ``route`` commits: it never landed."""
        with self.state._lock:
            self.state.armed.append((route, FAULT_BEFORE_COMMIT))

    def throttle_next(self, n: int, *, route: str | None = None) -> None:
        """Answer the next ``n`` requests (on ``route``, or on any route) with a ``429``."""
        with self.state._lock:
            self.state.throttles.extend([route] * n)

    def fail_next_with(self, status: int, *, route: str | None = None) -> None:
        """A gateway ``5xx`` with an HTML body, before any route runs: nothing is committed."""
        with self.state._lock:
            self.state.server_errors.append((route, status))

    # -- test controls (not API routes) -------------------------------------------

    def archive_deal(self, deal_id: str) -> None:
        """Someone deleted the deal in the HubSpot UI."""
        with self.state._lock:
            self.state.objects["deals"][deal_id].archived = True

    def remove_association(self, deal_id: str, contact_id: str) -> None:
        with self.state._lock:
            self.state.associations.discard((deal_id, contact_id))

    def plant_deal(self, properties: dict[str, str]) -> str:
        """A deal someone else created, already indexed for search."""
        with self.state._lock:
            record = self.state.create("deals", properties)
            record.created_at -= self.state.search_lag
            return record.id

    # -- inspection ---------------------------------------------------------------

    def contacts(self) -> list[dict[str, Any]]:
        with self.state._lock:
            return [r.as_json() for r in self.state.live("contacts")]

    def deals(self) -> list[dict[str, Any]]:
        with self.state._lock:
            return [r.as_json() for r in self.state.live("deals")]

    def deal_exists(self, deal_id: str) -> bool:
        with self.state._lock:
            record = self.state.objects["deals"].get(deal_id)
            return record is not None and not record.archived

    def associations(self) -> set[tuple[str, str]]:
        with self.state._lock:
            return set(self.state.associations)

    @property
    def request_log(self) -> list[str]:
        with self.state._lock:
            return list(self.state.request_log)
