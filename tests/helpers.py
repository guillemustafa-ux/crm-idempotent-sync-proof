"""Assertions shared by the scenarios: what HubSpot holds, read from the fake directly."""

from __future__ import annotations

from typing import Any


def one_of_each(hubspot) -> tuple[dict[str, Any], dict[str, Any]]:
    """Exactly one contact, one deal, and one association between them -- or the test fails."""
    contacts, deals, associations = hubspot.contacts(), hubspot.deals(), hubspot.associations()
    assert len(contacts) == 1, f"expected one contact, HubSpot holds {len(contacts)}: {contacts}"
    assert len(deals) == 1, f"expected one deal, HubSpot holds {len(deals)}: {deals}"
    assert associations == {(deals[0]["id"], contacts[0]["id"])}, associations
    return contacts[0], deals[0]


def wire(hubspot, route: str) -> list[str]:
    """Every request the fake logged on ``route``, in order (``"200 POST deals.upsert new=false id=..."``)."""
    return [line for line in hubspot.request_log if line.split(" ")[2:3] == [route]]
