from __future__ import annotations

import time

import pytest

from crm_idempotent_sync_proof.fake_hubspot import FakeHubSpot


@pytest.fixture
def hubspot():
    """A HubSpot stand-in whose clock is **frozen** and moves only on ``advance()``.

    So "one second after the first delivery" is exactly that, and a deal
    created by the test is invisible to search until the test says the
    search lag has passed -- no wall-clock time creeps in between two calls.
    """
    frozen_at = time.time()
    fake = FakeHubSpot(clock=lambda: frozen_at).start()
    yield fake
    fake.stop()
