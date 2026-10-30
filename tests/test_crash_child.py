"""The crash child itself: a scripted death and an unexpected exception must not look alike."""

from __future__ import annotations

import subprocess
import sys

from crm_idempotent_sync_proof._crash_child import CRASH_EXIT_CODE, UNEXPECTED_EXIT_CODE
from tests.conftest import ROOT, WEBHOOK_SECRET


def test_an_unexpected_exception_in_the_child_has_its_own_exit_code(hubspot, db_path, tmp_path, make_webhook):
    """A child that blows up on a bad signature is not "the scripted death".

    If the child answered 137 to every exception, a test asserting the kill
    would pass on a typo.
    """
    raw, _ = make_webhook()
    body = tmp_path / "webhook.json"
    body.write_bytes(raw)
    proc = subprocess.run(
        [
            sys.executable, "-m", "crm_idempotent_sync_proof._crash_child",
            "--db", db_path, "--base-url", hubspot.base_url,
            "--body-file", str(body), "--signature", "sha256=" + "0" * 64,
            "--secret", WEBHOOK_SECRET.decode(), "--crash-at", "after_deal_response",
        ],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
    )
    assert proc.returncode == UNEXPECTED_EXIT_CODE != CRASH_EXIT_CODE, proc.stdout
    assert "BadSignature" in proc.stderr
    assert hubspot.request_log == []


def test_the_fake_clock_only_moves_on_advance(hubspot):
    before = hubspot.now()
    hubspot.advance(1.5)
    assert hubspot.now() == before + 1.5
