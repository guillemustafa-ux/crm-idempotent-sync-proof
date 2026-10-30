"""The webhook's HMAC is checked over the raw bytes before anything else happens.

A bad signature is not "probably fine": nothing is parsed into the ledger,
nothing is sent to HubSpot. The receiver answers 401 and the source keeps
the event.
"""

from __future__ import annotations

import pytest

from crm_idempotent_sync_proof.sync import BadSignature, UnsupportedEvent, sign
from tests.conftest import WEBHOOK_SECRET


def tampered(webhook):
    raw, signature = webhook
    return raw.replace(b'"149.00"', b'"1.00"'), signature


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(lambda w: (w[0], "sha256=" + "0" * 64), id="wrong-hmac"),
        pytest.param(lambda w: (w[0], None), id="missing-header"),
        pytest.param(lambda w: (w[0], ""), id="empty-header"),
        pytest.param(lambda w: (w[0], sign(b"another-secret", w[0])), id="signed-with-another-secret"),
        pytest.param(tampered, id="body-changed-after-signing"),
    ],
)
def test_a_bad_signature_is_rejected_and_nothing_is_written(
    hubspot, engine_factory, ledger_factory, make_webhook, forge
):
    raw, signature = forge(make_webhook())
    with pytest.raises(BadSignature):
        engine_factory(hubspot).handle_webhook(raw, signature)
    assert ledger_factory().orders() == []
    assert hubspot.request_log == []


def test_an_engine_without_a_secret_accepts_nothing(hubspot, ledger_factory, client_factory, make_webhook):
    from crm_idempotent_sync_proof.sync import SyncEngine

    engine = SyncEngine(ledger=ledger_factory(), client=client_factory(hubspot), webhook_secret=None)
    with pytest.raises(BadSignature):
        engine.handle_webhook(*make_webhook())
    assert ledger_factory().orders() == [] and hubspot.request_log == []


def test_a_correctly_signed_event_of_another_type_is_refused_and_nothing_is_written(
    hubspot, engine_factory, ledger_factory, make_webhook
):
    raw, signature = make_webhook(event_type="order.refunded")
    assert signature == sign(WEBHOOK_SECRET, raw)
    with pytest.raises(UnsupportedEvent):
        engine_factory(hubspot).handle_webhook(raw, signature)
    assert ledger_factory().orders() == [] and hubspot.request_log == []
