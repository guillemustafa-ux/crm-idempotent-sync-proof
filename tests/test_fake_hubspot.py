"""The fake's own behaviour, over raw HTTP: each test pins one fact seen on a live portal.

If the fake drifted from HubSpot, every scenario built on it would prove
something about a service that does not exist. So the facts the proof leans
on are pinned here without the client in between.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

import pytest

from crm_idempotent_sync_proof.fake_hubspot import (
    RATE_LIMIT_HEADERS,
    ROUTE_DEAL_CREATE,
    ROUTE_DEAL_UPSERT,
    SEARCH_LAG_SECONDS,
)

V = "/crm/objects/2026-09"


def call(fake, method, path, body=None, *, key=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(fake.base_url + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key != "":
        req.add_header("Authorization", f"Bearer {key or fake.service_key}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"null"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null"), dict(exc.headers)


def upsert(fake, object_type, id_property, value, properties=None):
    return call(fake, "POST", f"{V}/{object_type}/batch/upsert", {
        "inputs": [{"id": value, "idProperty": id_property, "properties": properties or {}}]
    })


def test_contact_upsert_by_email_answers_new_then_the_same_id_not_new(hubspot):
    s1, b1, _ = upsert(hubspot, "contacts", "email", "ana@example.com", {"firstname": "Ana"})
    s2, b2, _ = upsert(hubspot, "contacts", "email", "ana@example.com", {"firstname": "Ana"})
    assert (s1, s2) == (200, 200)
    assert b1["results"][0]["new"] is True
    assert b2["results"][0]["new"] is False
    assert b1["results"][0]["id"] == b2["results"][0]["id"]
    assert len(hubspot.contacts()) == 1


def test_deal_upsert_by_external_order_id_answers_new_then_the_same_id_not_new(hubspot):
    _, b1, _ = upsert(hubspot, "deals", "external_order_id", "ORD-1", {"dealname": "Order ORD-1"})
    _, b2, _ = upsert(hubspot, "deals", "external_order_id", "ORD-1", {"amount": "10"})
    assert [b1["results"][0]["new"], b2["results"][0]["new"]] == [True, False]
    assert b1["results"][0]["id"] == b2["results"][0]["id"]
    (deal,) = hubspot.deals()
    assert deal["properties"]["external_order_id"] == "ORD-1"
    assert deal["properties"]["dealname"] == "Order ORD-1"
    assert deal["properties"]["amount"] == "10"  # an upsert is an update


def test_an_upsert_on_a_property_that_is_not_unique_is_refused(hubspot):
    status, body, _ = upsert(hubspot, "deals", "dealname", "Order ORD-1")
    assert status == 400 and body["category"] == "VALIDATION_ERROR"
    assert hubspot.deals() == []


def test_a_plain_create_with_a_taken_unique_value_is_a_400_and_creates_nothing(hubspot):
    s1, first, _ = call(hubspot, "POST", f"{V}/deals", {"properties": {"external_order_id": "ORD-1"}})
    s2, body, _ = call(hubspot, "POST", f"{V}/deals", {"properties": {"external_order_id": "ORD-1"}})
    assert s1 == 201
    assert s2 == 400
    assert body["category"] == "VALIDATION_ERROR"
    assert body["message"].startswith("Cannot set PropertyValueCoordinates{")
    assert "propertyName=external_order_id, value=ORD-1" in body["message"]
    assert body["message"].endswith(f"{first['id']} already has that value.")
    # The id the message names for the would-be deal does not exist afterwards.
    ghost = body["message"].split(" on ")[1].split(".")[0]
    assert ghost != first["id"]
    assert call(hubspot, "GET", f"{V}/deals/{ghost}")[0] == 404
    assert len(hubspot.deals()) == 1


def test_a_plain_create_without_a_unique_value_always_creates(hubspot):
    for _ in range(2):
        assert call(hubspot, "POST", f"{V}/deals", {"properties": {"dealname": "Order ORD-1"}})[0] == 201
    assert len(hubspot.deals()) == 2


def test_get_by_id_property_finds_the_deal_and_a_miss_is_404(hubspot):
    upsert(hubspot, "deals", "external_order_id", "ORD-1")
    status, body, _ = call(hubspot, "GET", f"{V}/deals/ORD-1?idProperty=external_order_id")
    assert status == 200 and body["properties"]["external_order_id"] == "ORD-1"
    status, body, _ = call(hubspot, "GET", f"{V}/deals/ORD-2?idProperty=external_order_id")
    assert status == 404 and body["category"] == "OBJECT_NOT_FOUND"


def test_search_does_not_see_a_new_deal_until_the_lag_has_passed(hubspot):
    upsert(hubspot, "deals", "external_order_id", "ORD-1", {"dealname": "Order ORD-1"})
    query = {"filterGroups": [{"filters": [
        {"propertyName": "dealname", "operator": "EQ", "value": "Order ORD-1"}]}]}
    assert call(hubspot, "POST", f"{V}/deals/search", query)[1]["total"] == 0
    hubspot.advance(SEARCH_LAG_SECONDS - 0.01)
    assert call(hubspot, "POST", f"{V}/deals/search", query)[1]["total"] == 0
    hubspot.advance(0.01)
    _, body, _ = call(hubspot, "POST", f"{V}/deals/search", query)
    assert body["total"] == 1 and body["results"][0]["properties"]["dealname"] == "Order ORD-1"


def test_the_default_association_is_idempotent(hubspot):
    contact = upsert(hubspot, "contacts", "email", "ana@example.com")[1]["results"][0]["id"]
    deal = upsert(hubspot, "deals", "external_order_id", "ORD-1")[1]["results"][0]["id"]
    path = f"/crm/v4/objects/deals/{deal}/associations/default/contacts/{contact}"
    assert call(hubspot, "PUT", path)[0] == 200
    status, body, _ = call(hubspot, "PUT", path)
    assert status == 200
    assert [r["associationSpec"]["associationTypeId"] for r in body["results"]] == [3, 4]
    assert hubspot.associations() == {(deal, contact)}
    _, listed, _ = call(hubspot, "GET", f"/crm/v4/objects/deals/{deal}/associations/contacts")
    assert [r["toObjectId"] for r in listed["results"]] == [int(contact)]


def test_the_unversioned_association_path_is_a_404(hubspot):
    assert call(hubspot, "PUT", "/crm/associations/2026-09/deals/1/associations/default/contacts/2")[0] == 404


def test_the_unique_property_reports_has_unique_value(hubspot):
    status, body, _ = call(hubspot, "GET", "/crm/properties/2026-09/deals/external_order_id")
    assert status == 200 and body["hasUniqueValue"] is True


@pytest.mark.parametrize("key", ["", "wrong-key"])
def test_a_missing_or_wrong_bearer_is_401_and_writes_nothing(hubspot, key):
    status, body, _ = call(
        hubspot, "POST", f"{V}/deals/batch/upsert",
        {"inputs": [{"id": "ORD-1", "idProperty": "external_order_id", "properties": {}}]}, key=key,
    )
    assert status == 401 and body["category"] == "INVALID_AUTHENTICATION"
    assert hubspot.deals() == []


def test_every_response_carries_the_rate_limit_headers_seen_live(hubspot):
    _, _, ok = upsert(hubspot, "contacts", "email", "ana@example.com")
    _, _, missing = call(hubspot, "GET", f"{V}/deals/ORD-9?idProperty=external_order_id")
    for headers in (ok, missing):
        for name, value in RATE_LIMIT_HEADERS.items():
            assert headers[name] == value
    assert RATE_LIMIT_HEADERS == {
        "X-HubSpot-RateLimit-Daily": "250000",
        "X-HubSpot-RateLimit-Max": "100",
        "X-HubSpot-RateLimit-Interval-Milliseconds": "10000",
        "X-HubSpot-RateLimit-Secondly": "10",
    }


def test_a_429_is_rate_limit_shaped_with_no_retry_after_and_writes_nothing(hubspot):
    hubspot.throttle_next(1, route=ROUTE_DEAL_UPSERT)
    status, body, headers = upsert(hubspot, "deals", "external_order_id", "ORD-1")
    assert status == 429
    assert body["errorType"] == "RATE_LIMIT"
    assert body["policyName"] == "TEN_SECONDLY_ROLLING"
    assert "Retry-After" not in headers
    assert headers["X-HubSpot-RateLimit-Interval-Milliseconds"] == "10000"
    assert hubspot.deals() == []
    assert upsert(hubspot, "deals", "external_order_id", "ORD-1")[0] == 200
    assert len(hubspot.deals()) == 1


def test_a_dropped_response_after_commit_leaves_the_deal_in_place(hubspot):
    hubspot.drop_response_after_commit(ROUTE_DEAL_CREATE)
    with pytest.raises((http.client.RemoteDisconnected, ConnectionError, urllib.error.URLError)):
        call(hubspot, "POST", f"{V}/deals", {"properties": {"dealname": "x"}})
    assert len(hubspot.deals()) == 1
    assert hubspot.request_log[-1].startswith("CUT-AFTER POST deals.create id=")


def test_a_dropped_response_before_commit_leaves_nothing(hubspot):
    hubspot.drop_response_before_commit(ROUTE_DEAL_UPSERT)
    with pytest.raises((http.client.RemoteDisconnected, ConnectionError, urllib.error.URLError)):
        upsert(hubspot, "deals", "external_order_id", "ORD-1")
    assert hubspot.deals() == []
