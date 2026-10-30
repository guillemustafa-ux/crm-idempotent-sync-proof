"""Break the implementation on purpose, and check the suite notices.

A green suite proves nothing on its own -- tests that assert only what the
code already does are green forever, including after someone deletes the part
that mattered. So this script reintroduces, one at a time, the defects
this design exists to prevent, and reports which tests catch each one.

Every mutation is applied to a real source file, the suite is run against it,
and the file is restored afterwards (even if the run is interrupted). The
working tree must be clean before the run -- a mutation and an uncommitted
edit would be indistinguishable -- and is checked clean again at the end, so
"restored" is verified rather than assumed.

    python scripts/verify_the_suite_bites.py
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "crm_idempotent_sync_proof"


@dataclass
class Mutation:
    name: str
    defect: str
    path: Path
    old: str
    new: str
    # How the suite catches it, honestly: by a visible duplicate, by a request
    # that should not be on the wire, or only by looking at the ledger.
    evidence: str = ""


_DEAL_UPSERT = (
    "            deal = self.client.upsert_deal(\n"
    "                external_order_id_for(order.order), deal_properties_for(order.order)\n"
    "            )\n"
)

MUTATIONS = [
    Mutation(
        name="search-then-create",
        defect="Drop the idProperty: find the order's deal with a search and create it "
        "when the search finds nothing -- the code most integrations start with.",
        path=PKG / "sync.py",
        old=_DEAL_UPSERT,
        new="            from .client import Upserted\n\n"
        "            deal = Upserted(naive_deal_sync(self.client, order.order), True, {})\n",
        evidence="A visible duplicate: a second deal whenever the second send comes inside "
        "the search lag -- a replay on a lost ledger, a restart after a kill past the deal "
        "write, two simultaneous deliveries, the A/B itself.",
    ),
    Mutation(
        name="plain-create",
        defect="Create the deal with a plain POST, still carrying the unique "
        "external_order_id, instead of upserting by it.",
        path=PKG / "sync.py",
        old=_DEAL_UPSERT,
        new="            from .client import Upserted\n\n"
        "            created = self.client.create_deal({**deal_properties_for(order.order),\n"
        "                \"external_order_id\": external_order_id_for(order.order)})\n"
        "            deal = Upserted(str(created[\"id\"]), True, {})\n",
        evidence="NOT a duplicate: HubSpot refuses the second create with 400 VALIDATION_ERROR "
        "\"already has that value.\". What fails is every path that must resend the deal "
        "-- a lost ledger, a restart after the deal write, a lost response, the second of "
        "two simultaneous deliveries -- which now ends in an error instead of the deal.",
    ),
    Mutation(
        name="external-id-per-delivery",
        defect="Generate the external_order_id at delivery time instead of deriving it "
        "from the order, so a redelivery plants a value HubSpot has never seen.",
        path=PKG / "sync.py",
        old='    return str(order["id"])\n',
        new="    import uuid\n\n"
        "    return f\"{order['id']}-{uuid.uuid4().hex[:8]}\"\n",
        evidence="A visible duplicate: the upsert is still an upsert, but by a key that "
        "changes, so every resend creates.",
    ),
    Mutation(
        name="no-durable-intent",
        defect="Send each step without recording the intent first, so a process killed "
        "mid-call wakes up with no record that a call was in flight.",
        path=PKG / "ledger.py",
        old="        self._conn.execute(\n"
        '            "INSERT INTO intents (order_id, step, state) VALUES (?, ?, ?) "\n'
        '            "ON CONFLICT (order_id, step) DO UPDATE SET state = excluded.state",\n'
        "            (order_id, step, PENDING),\n"
        "        )\n",
        new="        return None\n",
        evidence="NOT a visible duplicate, by design: the resend after a kill is an upsert "
        "and finds the record either way. What fires is the record itself -- the crash "
        "tests' assertion that the killed child left an intent row in flight, and "
        "the ledger and replay tests that read the intents back.",
    ),
    Mutation(
        name="give-up-on-429",
        defect="Treat the first 429 as final instead of waiting the announced "
        "interval and resending the same request.",
        path=PKG / "client.py",
        old="                    if throttled >= self.max_throttle_retries:\n",
        new="                    if True:\n",
    ),
    Mutation(
        name="skip-signature-check",
        defect="Process the webhook without checking its HMAC.",
        path=PKG / "sync.py",
        old="        if not verify_signature(self.webhook_secret, raw_body, signature):\n",
        new="        if False:\n",
    ),
    Mutation(
        name="accept-conflicting-redelivery",
        defect="Accept a redelivery whose content differs from what was first recorded "
        "for the same order id, instead of refusing to choose between them.",
        path=PKG / "ledger.py",
        old="            elif row[0] != sha:\n",
        new="            elif False:\n",
    ),
]


def working_tree_dirty() -> str:
    """``git status --porcelain`` for the repo: empty when clean."""
    proc = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def run_suite() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    failed = sorted(
        {
            line.split("::")[1].split(" ")[0]
            for line in proc.stdout.splitlines()
            if line.startswith("FAILED") or line.startswith("ERROR")
        }
    )
    return proc.returncode, failed


def main() -> int:
    dirty = working_tree_dirty()
    if dirty:
        print("the working tree must be clean before mutating (commit or stash first):")
        print(dirty)
        return 1

    baseline_code, baseline_failed = run_suite()
    print(f"baseline: {'PASS' if baseline_code == 0 else 'FAIL'}")
    if baseline_code != 0:
        print("the suite must be green before mutating:", baseline_failed)
        return 1

    survived: list[str] = []
    for mutation in MUTATIONS:
        original = mutation.path.read_text(encoding="utf-8")
        if mutation.old not in original:
            print(f"\n!! {mutation.name}: anchor not found in {mutation.path.name}")
            return 1
        # ``newline="\n"``: on Windows the default would restore the file
        # with CRLF endings, and the repo pins LF.
        try:
            mutation.path.write_text(
                original.replace(mutation.old, mutation.new, 1), encoding="utf-8", newline="\n"
            )
            code, failed = run_suite()
        finally:
            mutation.path.write_text(original, encoding="utf-8", newline="\n")

        print(f"\n[{mutation.name}]")
        print(f"  defect: {mutation.defect}")
        if mutation.evidence:
            print(f"  how it is caught: {mutation.evidence}")
        if code == 0:
            print("  CAUGHT BY: nothing -- the suite does not cover this defect")
            survived.append(mutation.name)
        else:
            for name in failed:
                print(f"  caught by: {name}")

    print()
    dirty = working_tree_dirty()
    if dirty:
        print("the source files were NOT restored cleanly:")
        print(dirty)
        return 1
    if survived:
        print(f"{len(survived)} mutation(s) survived: {', '.join(survived)}")
        return 1
    print(f"all {len(MUTATIONS)} mutations were caught; the suite bites (working tree clean again)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
