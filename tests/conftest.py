from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from crm_idempotent_sync_proof._crash_child import CRASH_EXIT_CODE
from crm_idempotent_sync_proof.client import HubSpotClient
from crm_idempotent_sync_proof.fake_hubspot import FakeHubSpot
from crm_idempotent_sync_proof.ledger import Ledger
from crm_idempotent_sync_proof.sync import SyncEngine, sign

ROOT = Path(__file__).resolve().parent.parent

# The shared secret the webhook source signs with. Test-only.
WEBHOOK_SECRET = b"whsec-test-only-not-a-real-secret"


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


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "sync.db")


@pytest.fixture
def ledger_factory(db_path):
    """Opens the ledger fresh each time.

    Every recovery in this suite is driven from a **new** ``Ledger`` object.
    In-memory state is never allowed to help across a crash boundary.
    """
    opened: list[Ledger] = []

    def make(path: str | None = None) -> Ledger:
        ledger = Ledger(path or db_path)
        opened.append(ledger)
        return ledger

    yield make
    for ledger in opened:
        ledger.close()


@pytest.fixture
def sleeps() -> list[float]:
    """Every wait the client asked for. Nothing in the proof really sleeps."""
    return []


@pytest.fixture
def client_factory(sleeps):
    def make(fake: FakeHubSpot, *, key: str | None = None) -> HubSpotClient:
        return HubSpotClient(
            fake.base_url,
            key if key is not None else fake.service_key,
            timeout=5.0,
            sleep=sleeps.append,
        )

    return make


@pytest.fixture
def engine_factory(ledger_factory, client_factory):
    """A brand-new engine: fresh ledger connection, fresh client, no memory.

    ``on_seam`` is the same hook the crash child uses; a test can run another
    worker inside it to force an interleaving a real race only produces
    sometimes.
    """

    def make(
        fake: FakeHubSpot,
        path: str | None = None,
        *,
        on_seam: Callable[[str], None] | None = None,
    ) -> SyncEngine:
        return SyncEngine(
            ledger=ledger_factory(path),
            client=client_factory(fake),
            webhook_secret=WEBHOOK_SECRET,
            on_seam=on_seam,
        )

    return make


@pytest.fixture
def make_webhook():
    """An "order paid" webhook as the source sends it: the raw bytes and their signature."""

    def make(
        order_id: str = "ORD-1001",
        email: str = "ana.perez@example.com",
        amount: str = "149.00",
        *,
        event_id: str = "evt_0001",
        firstname: str = "Ana",
        lastname: str = "Perez",
        event_type: str = "order.paid",
    ) -> tuple[bytes, str]:
        event: dict[str, Any] = {
            "event_id": event_id,
            "type": event_type,
            "order": {
                "id": order_id,
                "email": email,
                "firstname": firstname,
                "lastname": lastname,
                "amount": amount,
                "currency": "USD",
                "paid_at": "2026-10-30T12:00:00Z",
            },
        }
        raw = json.dumps(event, sort_keys=True).encode("utf-8")
        return raw, sign(WEBHOOK_SECRET, raw)

    return make


@pytest.fixture
def run_child(tmp_path, db_path):
    """Deliver one webhook to a worker in a separate process, and kill it mid-flight.

    Asserts the death was the scripted one (exit code 137): a seam that
    fired, or, for the naive strategy, the transport error the server
    arranged. An unexpected exception exits with a different code and fails
    the test with its traceback.
    """

    def run(fake: FakeHubSpot, webhook: tuple[bytes, str], crash_at: str, strategy: str = "durable"):
        raw, signature = webhook
        body_file = tmp_path / "webhook.json"
        body_file.write_bytes(raw)
        proc = subprocess.run(
            [
                sys.executable, "-m", "crm_idempotent_sync_proof._crash_child",
                "--db", db_path,
                "--base-url", fake.base_url,
                "--body-file", str(body_file),
                "--signature", signature,
                "--secret", WEBHOOK_SECRET.decode(),
                "--crash-at", crash_at,
                "--strategy", strategy,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT),
        )
        assert proc.returncode == CRASH_EXIT_CODE, (
            f"child was supposed to die the scripted death at '{crash_at}' "
            f"(strategy {strategy}), but exited {proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
        return proc

    return run
