"""A worker that is killed for real, mid-sync.

Mocking a crash with ``raise`` proves less than it looks like: the stack
unwinds, ``finally`` blocks run, buffers flush, and destructors get their say.
None of that happens when a container is evicted, a deploy restarts the pod,
or someone closes the laptop.

So this runs in its own process and calls ``os._exit``, which returns to the
operating system immediately -- no unwinding, no flushing, no cleanup. The
parent is then left with exactly what a real crash leaves: whatever made it
to disk before the kill, and a HubSpot portal whose state nobody recorded.

Run as::

    python -m crm_idempotent_sync_proof._crash_child --db X --base-url Y \\
        --body-file W --signature S --secret K --crash-at after_deal_response
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from .client import HubSpotClient, UnknownOutcome
from .fake_hubspot import FAKE_SERVICE_KEY
from .ledger import Ledger
from .sync import SyncEngine, naive_deal_sync

# The exit code of the scripted death, so the parent can assert the kill was
# ours and not an unnoticed traceback.
CRASH_EXIT_CODE = 137
# Anything else that blows up exits with this code and a traceback on stderr.
# A test that asserts 137 must never be satisfied by a typo.
UNEXPECTED_EXIT_CODE = 3


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except Exception:  # noqa: BLE001 -- reported, with a code of its own
        traceback.print_exc()
        return UNEXPECTED_EXIT_CODE


def _main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--crash-at", required=True)
    parser.add_argument("--strategy", default="durable", choices=("durable", "naive"))
    args = parser.parse_args(argv)

    raw = Path(args.body_file).read_bytes()
    client = HubSpotClient(args.base_url, FAKE_SERVICE_KEY, sleep=lambda _: None)

    if args.strategy == "naive":
        # The naive path has no seams to arm, so the kill is scheduled by the
        # server instead: it commits, then drops the connection, and the
        # child dies on the resulting transport error before writing anything
        # down. Only that error is the scripted death.
        try:
            naive_deal_sync(client, json.loads(raw)["order"])
        except UnknownOutcome as exc:
            sys.stdout.write(f"[child] died on {type(exc).__name__}\n")
            sys.stdout.flush()
            os._exit(CRASH_EXIT_CODE)
        return 0

    def kill_here(seam: str) -> None:
        if seam == args.crash_at:
            sys.stdout.write(f"[child] dying at seam '{seam}'\n")
            sys.stdout.flush()
            os._exit(CRASH_EXIT_CODE)

    engine = SyncEngine(
        ledger=Ledger(args.db), client=client,
        webhook_secret=args.secret.encode(), on_seam=kill_here,
    )
    engine.handle_webhook(raw, args.signature)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
