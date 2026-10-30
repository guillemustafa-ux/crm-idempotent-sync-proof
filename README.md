# crm-idempotent-sync-proof

[![proof](https://github.com/guillemustafa-ux/crm-idempotent-sync-proof/actions/workflows/ci.yml/badge.svg)](https://github.com/guillemustafa-ux/crm-idempotent-sync-proof/actions/workflows/ci.yml)

A harness for the gap that quietly doubles deals in a CRM sync: **HubSpot
commits the write, and your process dies -- or a second delivery arrives --
before anyone can tell.** Applied to one concrete flow -- a signed "order
paid" webhook that must become a HubSpot contact, a deal, and the
association between them -- with runnable evidence that:

1. **the same webhook delivered again and again** ends with exactly one
   contact, one deal and one association -- with the ledger intact, and
   with the ledger lost;
2. **a real process kill** (`os._exit`) between the contact write and the
   deal write, or after the deal write and before the ledger records it,
   restarts into the same one contact, one deal, one association;
3. **the obvious code duplicates**: "search for the order, create it if not
   found" makes a second deal whenever the second attempt lands inside
   HubSpot's search lag -- measured at 10.08 s on a live portal -- while the
   upsert by a unique property does not;
4. **two simultaneous deliveries** make one deal, and **a 429** is waited
   out and resent without writing anything twice;
5. it **refuses to guess** when the facts disagree -- a redelivery whose
   content differs, a deal someone deleted, a deal id that no longer
   matches -- and names the ids instead.

You can verify all of it in about two minutes, on your machine, with **no
HubSpot account and no network access**. There are no runtime dependencies:
the fake HubSpot, the transport, the webhook verification and the ledger are
standard library, so nothing here can be attributed to a framework being
clever.

This is the sibling of
[xero-idempotent-sync-proof](https://github.com/guillemustafa-ux/xero-idempotent-sync-proof)
and [qbo-idempotent-sync-proof](https://github.com/guillemustafa-ux/qbo-idempotent-sync-proof),
which prove the same property against accounting APIs, and of
[idempotent-writes-proof](https://github.com/guillemustafa-ux/idempotent-writes-proof),
which proves the general mechanism against a generic third-party API. This
repo applies it to what HubSpot offers -- and HubSpot offers something the
other two do not, which makes the design smaller.

## The failure, precisely

A call to HubSpot has three outcomes, not two:

* it **succeeded** and you were told so;
* it **provably failed** -- HubSpot answered a 4xx with its own error body,
  so nothing was applied. `400 VALIDATION_ERROR` "... already has that
  value." is the interesting one;
* the **outcome is unknown** -- the connection died, the request timed out,
  the container was evicted mid-flight, a gateway answered 502.

Almost every client collapses the third case into the second, and the
natural `except ConnectionError: retry` treats "it never landed" and "it
landed and I wasn't told" identically. Against an API where the retry is a
create, one of those two retries is a duplicate deal -- a pipeline that
counts the same revenue twice.

```
    webhook verified, delivery recorded   ─┐
                                           │   ← a crash here is free
    POST contacts/batch/upsert (email)     ─┤
                                           │   ← a crash here leaves a contact nobody recorded
    POST deals/batch/upsert                ─┤   idProperty: external_order_id (hasUniqueValue)
                                           │   ← a crash here leaves a deal nobody recorded
    PUT  associations/default              ─┤
                                           │   ← a crash here leaves an association nobody recorded
    ledger: synced                         ─┘
```

## What HubSpot gives you decides the strategy

HubSpot documents **no idempotency key and no request deduplication** for
these endpoints. What it does offer is the **upsert by a unique property**:
`POST /crm/objects/2026-09/{type}/batch/upsert` with an `idProperty` that
HubSpot enforces as unique -- `email` on contacts, and on deals a custom
property created with `hasUniqueValue: true`. HubSpot's docs on unique
properties: "uniqueness is enforced across all records of that object
type". The first upsert answers `new: true`; every later one answers the
same `id` with `new: false`. That turns "resend it" into both the question
and the answer, **with no time window** -- unlike Xero's six-minute
`Idempotency-Key`, the unique value lasts as long as the record.

| The step | HubSpot offers | How an in-flight write is settled |
|---|---|---|
| Contact | upsert with `idProperty: email` | Resend the upsert. HubSpot answers the contact the first send made, `new: false`. |
| Deal | upsert with `idProperty: external_order_id` (`hasUniqueValue: true`), the order id from the source | Resend the upsert, same answer. A plain create carrying the same value is refused with `400 VALIDATION_ERROR` -- the backstop, not the path. |
| Association | `PUT /crm/v4/objects/deals/{id}/associations/default/contacts/{id}` | Resend it. Twice is still one association. |
| "search, then create if not found" | search lags behind writes ("It may take a few moments for newly created or updated CRM objects to appear in search results.") | **Nothing.** Inside the lag the search says "not found" about a deal that exists. This design never decides anything on a search. |

The last row is the footgun this repo exists to show, and the reason the
deal carries a value HubSpot enforces as unique rather than one the sync
merely looks for.

## The design under test

* **Verify first, over the raw bytes.** The webhook's
  `X-Signature-SHA256: sha256=<hex>` is an HMAC-SHA256 of the raw body,
  compared in constant time **before anything parses it**. A bad, missing
  or forged signature, a body changed after signing, or an engine with no
  secret configured: nothing reaches the ledger, nothing reaches HubSpot.
* **Every write is an upsert by a unique property or an idempotent PUT.**
  So a resend is the recovery: there is no "did it land?" to answer before
  resending, because the resend is answered with the record the first send
  made. A redelivery, a restart after a kill, two workers at once and a
  resend after a 429 are all the same operation.
* **The unique value is derived, not generated.** `external_order_id` is
  the source's own order id, so any process, at any later time, with no
  memory of the attempt, plants the same one. A value minted at delivery
  time would make every resend a new deal (the `external-id-per-delivery`
  mutation below).
* **Intent before send -- and what it buys here, stated plainly.** Each
  step's intent is committed to SQLite (WAL, `synchronous=FULL`) before the
  request leaves. Here, unlike an API without upserts, the row is **not**
  what prevents the duplicate -- the unique property is. What the row buys
  is the operational record: a killed process wakes up knowing which step
  was in flight, `reconcile` can tell "pending" from "done", and the order
  is never reported synced while a step is open.
* **The ledger short-circuits, HubSpot settles.** An order already synced
  is not sent again: a replay with the ledger intact puts nothing on the
  wire. With the ledger lost -- a new replica, a restored backup -- the
  replay is sent, and HubSpot's `new: false` settles it.
* **A redelivery must say the same thing.** The order is recorded as first
  received (canonical JSON, SHA-256). A later delivery for the same order
  id with different content is refused with `ConflictingRedelivery` and
  changes nothing -- the sync does not choose which version is true.
* **The ledger refuses to rebind.** A bound contact or deal id is never
  overwritten with a different one; one deal id cannot be bound to two
  orders (a unique index); an order cannot be `associated` or `synced`
  without both ids (`CHECK` constraints). Enforced by the database, not by
  a code review.
* **429 means resend the same request, after the window HubSpot announced.**
  No 429 was provoked live and the documentation pages read list no
  `Retry-After`; what HubSpot does send, on every response (seen live), is `X-HubSpot-RateLimit-Interval-Milliseconds`
  (10000 live). The client waits that long and resends, up to three times,
  then stops with a definite `429` -- a throttled request was not
  processed -- and the next delivery or `resume` sends it again.
* **The status code is read before the body.** A `404` is "absent", a
  `429` is throttling, a `5xx` -- or any error without HubSpot's JSON body
  -- is `UnknownOutcome`, never a rejection: a gateway can answer 502 after
  handing the request on.
* **Recovery walks every order.** `resume` settles each unsynced order
  from the payload recorded on arrival; one order's failure is recorded on
  its row and the walk continues.

## Verify it in two minutes

```bash
git clone https://github.com/guillemustafa-ux/crm-idempotent-sync-proof
cd crm-idempotent-sync-proof
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -v --log-cli-level=INFO
```

The suite runs the sync against a **fake HubSpot** that speaks real HTTP
over a real socket and encodes the behaviours verified live (see
[Scope, honestly](#scope-honestly)) -- with a frozen clock that moves only
when a test says so, so "one second later, inside the search lag" is
exactly that -- and injects faults at the exact seams where deals get
doubled. The crash scenarios are not mocked: a **child process is killed
with `os._exit`** right after a HubSpot response arrives, so no stack
unwinds, no `finally` runs and no buffer flushes -- exactly what an evicted
container leaves behind. Recovery then runs in a fresh process with nothing
but the database.

```text
tests/test_scenario_3_replay.py
  order ORD-1001 contact by email -> CREATED (id=900001)
  order ORD-1001 deal by external_order_id -> CREATED (id=900002)
  order ORD-1001 already synced -> nothing sent
  order ORD-1001 already synced -> nothing sent
  order ORD-1001 already synced -> nothing sent
  order ORD-1001 already synced -> nothing sent
  test_the_same_webhook_delivered_five_times_is_one_contact_one_deal_one_association PASSED

  order ORD-1001 contact by email -> CREATED (id=900001)
  order ORD-1001 deal by external_order_id -> CREATED (id=900002)
  order ORD-1001 contact by email -> FOUND (new=false) (id=900001)
  order ORD-1001 deal by external_order_id -> FOUND (new=false) (id=900002)
  test_replays_against_a_lost_ledger_find_what_the_first_delivery_made[2] PASSED

tests/test_scenario_4_crash.py
  order ORD-1001 deal by external_order_id -> FOUND (new=false) (id=900002)
  test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_deal_response-resume] PASSED

  test_naive_retry_after_a_real_kill_makes_a_second_deal PASSED

tests/test_scenario_5_search_lag.py
  A naive search-then-create, 2 deliveries 1 s apart -> 2 deals
  test_a_naive_search_then_create_duplicates_inside_the_search_lag PASSED

  order ORD-1001 contact by email -> CREATED (id=900001)
  order ORD-1001 deal by external_order_id -> CREATED (id=900002)
  order ORD-1001 contact by email -> FOUND (new=false) (id=900001)
  order ORD-1001 deal by external_order_id -> FOUND (new=false) (id=900002)
  B upsert by external_order_id, 2 deliveries 1 s apart -> 1 deal
  test_b_the_upsert_by_unique_property_makes_one_deal_inside_the_search_lag PASSED

64 passed
```

Note the words: **CREATED** and **FOUND (new=false)**. Same sync code, same
request on the wire, different answers from HubSpot -- one deal either way.
The `new` flag comes from HubSpot's own upsert response; the fake returns it
because the live portal did.

### What each scenario proves

| # | Test | Cut point | Proven |
|---|---|---|---|
| 3a | `test_the_same_webhook_delivered_five_times_is_one_contact_one_deal_one_association` | -- | five deliveries, one request per step on the wire; the ledger counts 5 deliveries and binds the ids HubSpot holds |
| 3b | `test_replays_against_a_lost_ledger_find_what_the_first_delivery_made[2]`, `[5]` | every delivery on a database of its own, one second apart | each delivery sends every step; HubSpot answers `new=true` once, then `new=false`; one contact, one deal, one association |
| 3c | `test_a_redelivery_with_different_content_is_refused_and_writes_nothing` | same order id, a different amount | `ConflictingRedelivery`; nothing on the wire, the ledger row unchanged, the deal's amount unchanged |
| 4a | `test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_contact_response-*]` | `os._exit` after the contact upsert, before the ledger records it -- between the contact write and the deal write | the killed child left the contact intent in flight; the restart (redelivery or `resume`) resends the contact upsert, answered `new=false`; one of each |
| 4b | `...[after_deal_response-*]` | `os._exit` after the deal upsert, before the ledger records it | HubSpot holds a deal the ledger does not know; the restart's deal upsert is answered `new=false`; one deal |
| 4c | `...[after_association_response-*]` | `os._exit` after the association `PUT` | the `PUT` is sent again; still one association |
| 4d | `test_naive_retry_after_a_real_kill_makes_a_second_deal` | naive create committed, response lost, worker killed; retry 2 s later | **the anti-pattern, demonstrated**: the retry's search finds nothing inside the lag, and one order becomes two deals |
| 4e | `test_a_lost_response_on_the_deal_upsert_is_settled_by_resending_it` | connection cut after HubSpot commits; the process survives | `UnknownOutcome`, the order `unknown` with the deal in flight; the redelivery is answered `new=false` |
| 4f | `test_a_write_that_never_landed_is_actually_sent` | connection cut **before** the commit | the mirror case: `resume` sends it and HubSpot creates it -- "do nothing on doubt" is not the answer either |
| 4g | `test_a_5xx_is_an_unknown_outcome_not_a_rejection` | `502` with an HTML body | `UnknownOutcome`, not `HubSpotRejected`; `resume` settles it |
| 5a | `test_a_naive_search_then_create_duplicates_inside_the_search_lag` | two deliveries 1 s apart | **A: two deals** |
| 5b | `test_b_the_upsert_by_unique_property_makes_one_deal_inside_the_search_lag` | the same two deliveries, each on its own database | **B: one deal**, the second answered `new=false` |
| 5c | `test_control_the_naive_path_is_fine_once_the_index_has_caught_up` | the second delivery 10 s later | one deal: the duplicate is the lag, not a broken search |
| 5d | `test_control_naive_create_on_the_unique_property_is_refused_not_duplicated` | naive code that also writes `external_order_id` | the live `400 VALIDATION_ERROR` "... already has that value."; no duplicate, and a delivery that fails |
| 6a | `test_two_simultaneous_deliveries_make_one_deal[one-shared-ledger]` | two threads, one database, held at `before_deal_send` until both are there | one deal; one result `deal_created=True`, the other `False`; the ledger counts 2 deliveries |
| 6b | `test_two_simultaneous_deliveries_make_one_deal[a-ledger-each]` | the same, two databases | both ledgers bound to the same deal |
| 6c | `test_two_simultaneous_naive_deliveries_make_two_deals` | two threads held between search and create | **two deals** |
| 7a | `test_a_429_on_every_step_is_waited_out_and_nothing_is_written_twice` | one `429` on each of the three steps | a 10.0 s wait each (the announced interval), each 429 followed by one 200; one of each |
| 7b | `test_two_429s_in_a_row_wait_the_interval_each_time` | two `429` on the deal upsert | two waits, one deal |
| 7c | `test_throttling_past_the_retry_budget_is_a_definite_no_and_resume_finishes` | four `429` against three resends | a definite `429` (`RATE_LIMIT`), the order `error` with the deal in flight and the contact bound; `resume` finishes without resending the contact |
| R1 | `test_reconcile_names_every_kind_of_drift_and_writes_nothing` | five orders: one fine, a deal deleted, an association removed, a deal re-made by hand, one in flight | `ok`, `missing_in_hubspot`, `association_missing`, `id_mismatch`, `pending`, each with the ids; only `GET`s on the wire, the ledger untouched |
| R2 | `test_a_deal_a_human_deleted_is_not_recreated_by_a_replay_or_a_resume` | deal deleted after the sync | the replay and `resume` send nothing; the deal stays deleted |
| R3 | `test_an_order_the_sync_refused_to_guess_about_is_left_for_a_human` | an order in `conflict` | `resume` skips it, `reconcile` reports it; nothing sent |

The remaining tests are the fake's own (16 in
`tests/test_fake_hubspot.py`, over raw HTTP, pinning each live fact: the
upsert `new` flags, the 400 and its ghost id, the 404 on a miss, the search
lag, the idempotent association, the rate headers, the 429 shape and the
dropped connections), the ledger's (6), the crash child's
(2), the webhook signature's (7), the runbook commands'
(4 more) and the live smoke's skip path (2).

## Proof that the suite bites

A green suite proves nothing on its own -- tests that assert only what the
code already does stay green after someone deletes the part that mattered.
So the repo ships the check, and CI runs it:

```bash
python scripts/verify_the_suite_bites.py
```

It refuses to run on a dirty tree, reintroduces each defect into the real
source, runs the suite, reports what caught it, restores the file, and
verifies the tree is clean again (abridged below: the script lists every
test that caught each mutation):

```text
baseline: PASS

[search-then-create]
  defect: Drop the idProperty: find the order's deal with a search and create it when the search finds nothing -- the code most integrations start with.
  how it is caught: A visible duplicate: a second deal whenever the second send comes inside the search lag -- a replay on a lost ledger, a restart after a kill past the deal write, two simultaneous deliveries, the A/B itself.
  caught by: test_a_429_on_every_step_is_waited_out_and_nothing_is_written_twice
  caught by: test_a_5xx_is_an_unknown_outcome_not_a_rejection
  caught by: test_a_lost_response_on_the_deal_upsert_is_settled_by_resending_it
  caught by: test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_deal_response-redelivery]
  caught by: test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_deal_response-resume]
  caught by: test_a_write_that_never_landed_is_actually_sent
  … 19 tests in all

[plain-create]
  defect: Create the deal with a plain POST, still carrying the unique external_order_id, instead of upserting by it.
  how it is caught: NOT a duplicate: HubSpot refuses the second create with 400 VALIDATION_ERROR "already has that value.". What fails is every path that must resend the deal -- a lost ledger, a restart after the deal write, a lost response, the second of two simultaneous deliveries -- which now ends in an error instead of the deal.
  caught by: test_a_429_on_every_step_is_waited_out_and_nothing_is_written_twice
  caught by: test_a_5xx_is_an_unknown_outcome_not_a_rejection
  caught by: test_a_lost_response_on_the_deal_upsert_is_settled_by_resending_it
  caught by: test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_deal_response-redelivery]
  caught by: test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_deal_response-resume]
  caught by: test_a_write_that_never_landed_is_actually_sent
  … 17 tests in all

[external-id-per-delivery]
  defect: Generate the external_order_id at delivery time instead of deriving it from the order, so a redelivery plants a value HubSpot has never seen.
  how it is caught: A visible duplicate: the upsert is still an upsert, but by a key that changes, so every resend creates.
  caught by: test_a_lost_response_on_the_deal_upsert_is_settled_by_resending_it
  caught by: test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_deal_response-redelivery]
  caught by: test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_deal_response-resume]
  caught by: test_b_the_upsert_by_unique_property_makes_one_deal_inside_the_search_lag
  caught by: test_control_naive_create_on_the_unique_property_is_refused_not_duplicated
  caught by: test_reconcile_names_every_kind_of_drift_and_writes_nothing
  … 14 tests in all

[no-durable-intent]
  defect: Send each step without recording the intent first, so a process killed mid-call wakes up with no record that a call was in flight.
  how it is caught: NOT a visible duplicate, by design: the resend after a kill is an upsert and finds the record either way. What fires is the record itself -- the crash tests' assertion that the killed child left an intent row in flight, and the ledger and replay tests that read the intents back.
  caught by: test_a_lost_response_on_the_deal_upsert_is_settled_by_resending_it
  caught by: test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_association_response-redelivery]
  caught by: test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_association_response-resume]
  caught by: test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_contact_response-redelivery]
  caught by: test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_contact_response-resume]
  caught by: test_a_real_kill_after_a_hubspot_write_restarts_without_duplicates[after_deal_response-redelivery]
  … 11 tests in all

[give-up-on-429]
  defect: Treat the first 429 as final instead of waiting the announced interval and resending the same request.
  caught by: test_a_429_on_every_step_is_waited_out_and_nothing_is_written_twice
  caught by: test_throttling_past_the_retry_budget_is_a_definite_no_and_resume_finishes
  caught by: test_two_429s_in_a_row_wait_the_interval_each_time

[skip-signature-check]
  defect: Process the webhook without checking its HMAC.
  caught by: test_a_bad_signature_is_rejected_and_nothing_is_written[body-changed-after-signing]
  caught by: test_a_bad_signature_is_rejected_and_nothing_is_written[empty-header]
  caught by: test_a_bad_signature_is_rejected_and_nothing_is_written[missing-header]
  caught by: test_a_bad_signature_is_rejected_and_nothing_is_written[signed-with-another-secret]
  caught by: test_a_bad_signature_is_rejected_and_nothing_is_written[wrong-hmac]
  caught by: test_an_engine_without_a_secret_accepts_nothing
  caught by: test_an_unexpected_exception_in_the_child_has_its_own_exit_code

[accept-conflicting-redelivery]
  defect: Accept a redelivery whose content differs from what was first recorded for the same order id, instead of refusing to choose between them.
  caught by: test_a_redelivery_with_different_content_is_refused_and_writes_nothing
  caught by: test_identical_redeliveries_are_counted_and_different_content_is_refused

all 7 mutations were caught; the suite bites (working tree clean again)
```

Two of those results say something true rather than something flattering:

* **`plain-create` doubles nothing.** HubSpot refuses the second create on
  the unique `external_order_id` with `400 VALIDATION_ERROR` -- the
  backstop works even for the wrong code. What fails is every path that
  must resend the deal: the replay on a lost ledger, the restart after a
  kill past the deal write, the lost response, the second of two
  simultaneous deliveries. Each now ends in an error the source will retry
  forever, instead of in the deal. No duplicate; an outage.
* **`no-durable-intent` doubles nothing either.** The resend after a kill
  is an upsert and finds the record with or without the row. What fires is
  the record itself: every crash test fails on its assertion that the
  killed child left an intent in flight, before any count of records is
  read; 4e and 7c fail on the step they expect in flight; the ledger and
  replay tests fail reading the intents back. The row is the operational
  record, not the duplicate guard -- the same honest result the Xero
  sibling reports.

## Runbook

**Replay a webhook safely.** Resend it from the source, or `POST` the same
bytes with the same signature to the receiver. That is the whole procedure,
and it is safe at any time:

* the order is synced -> nothing is sent (`SyncResult.status == "synced"`,
  `contact_created`/`deal_created` are `None`);
* the order is half done (a crash, a lost response, a 429 past the budget)
  -> the open steps are resent; HubSpot answers the records the first
  attempt made, `new: false`;
* the ledger is gone -> every step is resent and HubSpot settles each one
  the same way.

A replay **with different content** for the same order id is refused
(`ConflictingRedelivery`, nothing written). Decide which version is true
outside the sync.

One thing to know before replaying on purpose: an upsert is an update (the
documented semantics: "if the contacts already exist, they'll be updated";
live, the second upsert was only seen with identical properties). A
resent deal upsert writes the properties the sync owns -- `dealname`,
`amount`, `pipeline`, `dealstage` -- back over whatever a human changed in
HubSpot since. A synced order is never resent, so this only happens when the
ledger is lost.

**Settle what is stuck.** `resume` walks every order that is not synced and
resends its open steps from the payload recorded on arrival:

```bash
export HUBSPOT_SERVICE_KEY=...          # never printed
python -m crm_idempotent_sync_proof resume --db sync.db
```

It prints `ORDER status` per order and exits 1 if any is still not synced.

**Reconcile the ledger against HubSpot.** Read-only; it repairs nothing:

```bash
python -m crm_idempotent_sync_proof reconcile --db sync.db
```

One line per order, exit 1 on any drift. It looks the deal up with
`GET /crm/objects/2026-09/deals/{external_order_id}?idProperty=external_order_id`,
never with search, because a reconcile run right after a sync would
otherwise report drift that is only the search lag.

| Verdict | Means | What to do |
|---|---|---|
| `ok` | synced; HubSpot has the deal the ledger bound, associated to the bound contact | nothing |
| `pending` | the ledger has not finished the order; the line names the open steps and the last error | `resume` |
| `missing_in_hubspot` | no deal carries the order's `external_order_id` any more | someone deleted it; decide whether it should exist |
| `id_mismatch` | a deal carries it, but not the one the ledger bound | it was deleted and made again; decide which is real |
| `association_missing` | the deal is there; the bound contact is not associated to it | re-associate by hand, or find out why it was removed |
| `conflict` | the sync already refused to guess on this order | read `last_error` |

**What the sync refuses to guess** -- each tested, each reported with the
ids on both sides:

* **which of two versions of an order is true**: a redelivery whose content
  differs from the first is refused, and nothing is written
  (`test_a_redelivery_with_different_content_is_refused_and_writes_nothing`);
* **whether a deleted deal should come back**: a synced order is never
  resent, so a deal a human deleted stays deleted -- `reconcile` reports it
  (`test_a_deal_a_human_deleted_is_not_recreated_by_a_replay_or_a_resume`);
* **which of two deal ids is ours**: the ledger never rebinds an order to a
  different contact or deal id, and never binds one deal to two orders
  (`LedgerConflict`, pinned in `tests/test_ledger.py`). The sync marks such
  an order `conflict`, and a `conflict` order is left out of `resume` and
  reported by `reconcile` (R3). In this flow a bound step is never resent,
  so the guard is a backstop against a corrupted or hand-edited ledger
  rather than a path the scenarios reach;
* **whether an unknown outcome landed**: it is neither "failed" nor "done";
  the order stays `unknown` with the step in flight until a resend answers
  it.

## Layout

```
crm_idempotent_sync_proof/
  ledger.py        # SQLite operational record: deliveries and their content hash, intent
                   # before send, bound ids, one-deal-one-order index, CHECK constraints
  client.py        # transport that keeps "failed" and "unknown" apart; Bearer key, never
                   # printed; status before body; 429 -> same request after the interval
  sync.py          # verify -> record -> upsert contact -> upsert deal -> associate;
                   # resume; the search-then-create anti-pattern, kept for the tests
  reconcile.py     # read-only ledger-vs-HubSpot verdicts
  __main__.py      # `reconcile` and `resume` from a shell
  fake_hubspot.py  # HubSpot on a socket: upserts by unique property, the 400 on a taken
                   # unique value, search lag on a steppable clock, idempotent association,
                   # rate headers, 429, 5xx and dropped-connection faults
  _crash_child.py  # a worker killed for real with os._exit, mid-step
scripts/
  verify_the_suite_bites.py   # mutation harness (7 defects)
  live_smoke.py               # the sync against a live portal, if a key is given
tests/
  conftest.py, helpers.py
  test_fake_hubspot.py, test_ledger.py, test_crash_child.py, test_live_smoke.py
  test_scenario_3_replay.py
  test_scenario_4_crash.py
  test_scenario_5_search_lag.py
  test_scenario_6_concurrency.py
  test_scenario_7_rate_limits.py
  test_webhook_signature.py
  test_runbook.py
```

Python 3.10+. No runtime dependencies; `pytest` only to run the proof.

## Scope, honestly

The suite runs against a **local fake**. The behaviours it encodes were
**verified against a live portal on 2026-10-30**, with a service key (a
"clave de servicio"; legacy private apps are not available on that
account) scoped to contacts and deals read+write and schemas.deals
read+write:

* the deal property `external_order_id` was created through
  `/crm/properties/2026-09/deals` with `hasUniqueValue: true` (`201`);
* `POST /crm/objects/2026-09/contacts/batch/upsert` by `email`: the first
  call answered `new: true`, the second the same id with `new: false`;
* `POST /crm/objects/2026-09/deals/batch/upsert` with
  `idProperty: external_order_id` exists and behaves the same: `new: true`,
  then the same id with `new: false`;
* a plain create carrying a taken unique value: `HTTP 400 VALIDATION_ERROR`
  `Cannot set PropertyValueCoordinates{...propertyName=external_order_id, value=...} on <id>. <existing id> already has that value.`
  The `<id>` named
  in the message does **not** exist afterwards (`GET` 404, no ghost in
  search). The fake's message omits the coordinates' other fields;
* `GET /crm/objects/2026-09/deals/{value}?idProperty=external_order_id`
  answers `200`;
* search lag: a freshly created deal first appeared in `/deals/search`
  after **10.08 s** (attempt 11, polled 0.25 s apart). The fake's default
  lag is 10 s; the tests step a frozen clock across it;
* association: `PUT /crm/associations/2026-09/...` answered `404`;
  `PUT /crm/v4/objects/deals/{id}/associations/default/contacts/{id}`
  answered `200`, and twice was still exactly one association (type 3
  deal-to-contact, 4 back);
* every response carried `X-HubSpot-RateLimit-Daily` 250000, `-Max` 100,
  `-Interval-Milliseconds` 10000 and `-Secondly` 10 (documented as
  deprecated, still sent). No 429 was provoked.

From HubSpot's documentation, not provoked live: Free and Starter apps get
100 requests per 10 seconds and 250,000 per day; search is limited to 5
requests per second per account; there is no idempotency key; "Partial
upserts are not supported when using `email` as the `idProperty`" (the sync
sends one input per upsert).

And it goes **beyond what ran live** in these places, each modelled on the
side that makes the client's job harder:

* the answer to the **loser of two simultaneous upserts** of the same order:
  the fake serialises them and answers `new: false`; real HubSpot might
  answer the loser with a `400` instead. Either way nothing is duplicated --
  a `400` leaves the order in `error` and `resume` settles it -- but the
  exact response was not observed live;

* the **bodies** of the `401`, `404` and `429`: HubSpot's documented
  `status`/`message`/`category` shape, and on the 429 `errorType:
  RATE_LIMIT` with `policyName` (`TEN_SECONDLY_ROLLING` here). The client
  branches on the status code first; on a 429 it reads only the interval
  header;
* the shape of the association responses (the client reads nothing from
  the `PUT` and only `toObjectId` from the list);
* a `400` for an upsert whose `idProperty` is not a unique property (not
  tried live; the sync never sends one);
* emails are matched exactly as sent; HubSpot's own normalisation of
  addresses is not modelled;
* only the **creation** of a deal lags in search; the lag after an update
  is not modelled.

Deliberately not covered: a specific webhook vendor (the signature is a
generic HMAC-SHA256 of the raw body), webhook timestamps and replay windows
(replays are welcome here -- they are idempotent), contact merges, deal
line items, multi-currency, and the receiver's HTTP layer, which is one
mapping: 2xx only when `handle_webhook` returns, 401 on `BadSignature`, 409
on `ConflictingRedelivery`, 5xx otherwise so the source redelivers.

### LIVE

To run the sync against a real portal:

1. A HubSpot account (a free one works) and a key with contacts and deals
   read+write and schemas.deals read+write (the live run used that set; whether
   read alone suffices was not tried): a service key, or a private app token
   where private apps are still offered.
2. The deal property `external_order_id` with `hasUniqueValue: true`. The
   script checks it and refuses to run without it.
3. `HUBSPOT_SERVICE_KEY` in the environment, or in a git-ignored file
   passed with `--env-file`. The key is never printed, and the script reads
   no file it was not given.
4. `python scripts/live_smoke.py --env-file .env.local` -- it checks the
   property, signs one `SMOKE-...` order, delivers it to the sync with a
   ledger of its own, delivers it again on a **fresh** ledger (HubSpot must
   answer the same contact and deal, `new: false`), a third time on the
   first ledger (nothing sent), tries a plain create with the same
   `external_order_id` (must be the `400`), reads the association back, and
   prints the ids to clean up. `--naive-ab` adds search-then-create twice,
   back to back, on a second order that lives only in `dealname`. Without a
   key it prints a skip line and exits 0.

Run on 2026-10-30 against a free HubSpot portal with a service key,
`python scripts/live_smoke.py --env-file .env.local --naive-ab`. The block
below is **abridged, not verbatim**: ids shortened to their last six digits,
the rate-header dict summarised, the 400 message and long lines reflowed,
and the `(contact_created=None, deal_created=None)` detail and the final
`records to clean up` line dropped. Rerunning the command prints the full
lines.

```
property external_order_id: hasUniqueValue=True
delivery 1 (ledger A)       -> contact …418302 new=True,  deal …864254 new=True
delivery 2 (fresh ledger B) -> contact …418302 new=False, deal …864254 new=False
delivery 3 (ledger A again) -> synced, nothing sent
replay against a lost ledger: SAME contact and deal
plain create with a taken external_order_id: HTTP 400 VALIDATION_ERROR:
  ... propertyName=external_order_id, value=SMOKE-20261030-175301} on …342538.
  …864254 already has that value.
deal …864254 associated contacts: ['…418302']
rate headers: daily 250000, max 100 per interval-milliseconds 10000
naive search-then-create, twice back to back on 'SMOKE-20261030-175301-NAIVE':
  deals …600975 and …025815 -> TWO deals
live smoke: PASS
```

The last line is the point of the repository, observed on a real portal:
the naive path created two deals for one order; the upsert path never did.
The id named in the `400` (`…342538`) does not exist afterwards -- checked
by `GET`, which answered 404, on an earlier probe of the same error.
