"""HTTP transport for HubSpot, with the one distinction that matters.

A call against HubSpot has three outcomes, not two:

* it **succeeded** and you were told so;
* it **provably failed** -- HubSpot answered a 4xx with its own JSON error
  body (``status: error``, a ``category``), so nothing was applied.
  ``VALIDATION_ERROR`` "... already has that value." is the interesting one;
* the **outcome is unknown** -- the connection died, the request timed out,
  a gateway answered 502, the process was killed. The write may or may not
  have landed.

Collapsing the third case into the second is the bug, so this transport
keeps them apart at the type level: ``HubSpotRejected`` vs
``UnknownOutcome``.

Two HubSpot-specific behaviours are handled here because they are transport
concerns, not business ones:

* a ``429`` is answered by resending the **same** request. HubSpot documents
  no ``Retry-After`` and none was seen live, so the wait is the rolling
  window the response announces in ``X-HubSpot-RateLimit-Interval-Milliseconds``
  (10000 live), or ``default_wait`` when that header is absent. A throttled
  request was not processed, and every write this sync sends is an upsert by
  a unique property, so the resend is safe whatever happened before it.
  After ``max_throttle_retries`` the transport stops with a ``429``
  ``HubSpotRejected``: a definite "not applied", and the next delivery or
  ``resume`` sends it again;
* the **status code is read first**: a ``404`` is ``HubSpotNotFound`` (the
  lookups read it as "absent"), a ``429`` is throttling, a ``5xx`` is
  ``UnknownOutcome``. Only the remaining 4xx must carry a HubSpot JSON body
  to count as a definite "no" -- an HTML page from a proxy is not HubSpot
  speaking.

The service key goes out as ``Authorization: Bearer <key>`` and is never
logged, printed or put in an exception message.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

HUBSPOT_API_URL = "https://api.hubapi.com"
API_VERSION = "2026-09"
OBJECTS = f"/crm/objects/{API_VERSION}"

# A dropped response surfaces as one of these. They do not all descend from
# OSError, so catching OSError alone silently misses the interesting case.
TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
    OSError,
)

RATE_LIMIT_INTERVAL_HEADER = "X-HubSpot-RateLimit-Interval-Milliseconds"


class UnknownOutcome(Exception):
    """The request may or may not have been applied. Do not read it as a "no"."""


class HubSpotRejected(Exception):
    """HubSpot answered a 4xx with its own error body. Nothing was applied."""

    def __init__(self, status: int, body: str):
        parsed = _hubspot_error(body) or {}
        self.status = status
        self.category = parsed.get("category")
        self.error_type = parsed.get("errorType")
        self.message = parsed.get("message") or body[:200]
        super().__init__(f"HTTP {status} {self.category or ''}: {self.message}".replace("  ", " "))

    def says(self, fragment: str) -> bool:
        return fragment in (self.message or "")


class HubSpotNotFound(HubSpotRejected):
    """``404``: the object named in the URL does not exist."""


def _hubspot_error(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass
class Upserted:
    id: str
    new: bool
    properties: dict[str, Any]


class HubSpotClient:
    def __init__(
        self,
        base_url: str,
        service_key: str,
        *,
        timeout: float = 10.0,
        max_throttle_retries: int = 3,
        default_wait: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self._service_key = service_key
        self.timeout = timeout
        self.max_throttle_retries = max_throttle_retries
        self.default_wait = default_wait
        self.sleep = sleep
        # The X-HubSpot-RateLimit-* headers of the last response, for the
        # live smoke to print. Never the Authorization header.
        self.last_rate_headers: dict[str, str] = {}

    def __repr__(self) -> str:  # the key never appears in a repr or a log line
        return f"HubSpotClient({self.base_url!r})"

    # -- transport --------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        query: dict[str, str] | None = None,
    ) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        throttled = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Accept", "application/json")
            req.add_header("Authorization", f"Bearer {self._service_key}")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self._keep_rate_headers(resp.headers)
                    raw = resp.read().decode("utf-8")
                    try:
                        return json.loads(raw) if raw else {}
                    except ValueError:
                        # A 2xx that is not JSON did not come from HubSpot's
                        # API; the write's fate is as unknown as after a
                        # dropped connection.
                        raise UnknownOutcome(
                            f"{method} {path}: HTTP {resp.status} with a body that is not JSON"
                        ) from None
            except urllib.error.HTTPError as exc:
                self._keep_rate_headers(exc.headers)
                raw = exc.read().decode("utf-8", "replace")
                # The status code is read FIRST.
                if exc.code == 429:
                    if throttled >= self.max_throttle_retries:
                        raise HubSpotRejected(exc.code, raw) from exc
                    # Same URL, same body: the throttled request was never
                    # processed, and an upsert resent is the same upsert.
                    self.sleep(self._throttle_wait(exc.headers))
                    throttled += 1
                    continue
                if exc.code == 404:
                    raise HubSpotNotFound(exc.code, raw) from exc
                if exc.code >= 500 or _hubspot_error(raw) is None:
                    # Not a verdict on the write: a gateway can time out after
                    # handing the request on.
                    raise UnknownOutcome(
                        f"{method} {path}: HTTP {exc.code} is not a HubSpot answer; "
                        "the write's fate is unknown"
                    ) from exc
                raise HubSpotRejected(exc.code, raw) from exc
            except TRANSPORT_ERRORS as exc:
                # Nobody answered. The write's fate is unknowable from here.
                raise UnknownOutcome(f"{method} {path}: {type(exc).__name__}: {exc}") from exc

    def _keep_rate_headers(self, headers: Any) -> None:
        if headers is None:
            return
        self.last_rate_headers = {
            k: v for k, v in headers.items() if k.lower().startswith("x-hubspot-ratelimit")
        }

    def _throttle_wait(self, headers: Any) -> float:
        try:
            return int(headers.get(RATE_LIMIT_INTERVAL_HEADER)) / 1000.0
        except (TypeError, ValueError):
            return self.default_wait

    # -- writes -------------------------------------------------------------

    def upsert(self, object_type: str, id_property: str, value: str, properties: dict[str, Any]) -> Upserted:
        """One input to ``POST /crm/objects/2026-09/{type}/batch/upsert``.

        ``id_property`` must be a property HubSpot enforces as unique
        (``email`` on contacts, ``external_order_id`` on deals): that is what
        makes the second call answer the first record with ``new: false``
        instead of making another.
        """
        body = self._request(
            "POST",
            f"{OBJECTS}/{object_type}/batch/upsert",
            {"inputs": [{"id": value, "idProperty": id_property, "properties": properties}]},
        )
        (result,) = body["results"]
        return Upserted(str(result["id"]), bool(result["new"]), result.get("properties") or {})

    def upsert_contact(self, email: str, properties: dict[str, Any]) -> Upserted:
        return self.upsert("contacts", "email", email, {**properties, "email": email})

    def upsert_deal(self, external_order_id: str, properties: dict[str, Any]) -> Upserted:
        return self.upsert(
            "deals", "external_order_id", external_order_id,
            {**properties, "external_order_id": external_order_id},
        )

    def create_deal(self, properties: dict[str, Any]) -> dict[str, Any]:
        """A plain create. Only the anti-patterns call this."""
        return self._request("POST", f"{OBJECTS}/deals", {"properties": properties})

    def associate_deal_to_contact(self, deal_id: str, contact_id: str) -> None:
        """``PUT /crm/v4/.../associations/default/...``: idempotent, twice is one association."""
        self._request(
            "PUT", f"/crm/v4/objects/deals/{deal_id}/associations/default/contacts/{contact_id}"
        )

    # -- reads ----------------------------------------------------------------

    def search_deals(self, property_name: str, value: str) -> list[dict[str, Any]]:
        """``POST .../deals/search`` with one ``EQ`` filter. Lags behind writes."""
        body = self._request(
            "POST",
            f"{OBJECTS}/deals/search",
            {"filterGroups": [{"filters": [
                {"propertyName": property_name, "operator": "EQ", "value": value}
            ]}]},
        )
        return list(body.get("results") or [])

    def get_deal_by_external_order_id(self, external_order_id: str) -> dict[str, Any] | None:
        """``GET .../deals/{value}?idProperty=external_order_id``; ``None`` on a 404. Does not lag."""
        try:
            return self._request(
                "GET",
                f"{OBJECTS}/deals/{urllib.parse.quote(external_order_id, safe='')}",
                query={"idProperty": "external_order_id"},
            )
        except HubSpotNotFound:
            return None

    def deal_contact_ids(self, deal_id: str) -> list[str]:
        body = self._request("GET", f"/crm/v4/objects/deals/{deal_id}/associations/contacts")
        return [str(r["toObjectId"]) for r in body.get("results") or []]

    def get_deal_property(self, name: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/crm/properties/{API_VERSION}/deals/{name}")
        except HubSpotNotFound:
            return None
