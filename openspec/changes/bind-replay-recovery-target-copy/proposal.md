# Bind replay.py's recovery-target copy to the contract (#1245)

## Why

After #1244/PR #1246 the contract SoT ledger names exactly one
recovery-target copy outside the bound set:
`scripts/node27_timeseries_decompression_replay.py:24-32` — a six-field
`TARGET` literal plus a synthetic `TARGET_RELATION` string, neither
derived from `contract.RECOVERY_TARGET_FIELDS` nor covered by any
guard. Worse, the tests covering them are tautological over the four
IDENTITY fields (hypertable_schema/name, chunk_schema/name):
`tests/test_node27_timeseries_decompression_replay.py:62` feeds the
fake cursor `replay.TARGET_RELATION` and :80/:81/:86 assert against
`replay.TARGET`/`replay.TARGET_RELATION` — both sides move together,
so flipping those replay fields wholesale ships green, and flipping
the contract alone leaves this file entirely unreacting
(bidirectionally blind). The two RANGE fields are instead weakly
pinned by the stub's hardcoded catalog datetimes (tests :59/:63): a
replay-side range flip already fails today, but with a symptom-level
error (`authorized recovery target range differs` via replay.py
:92-93), not a message naming the drifted copy. Not fully inert:
`TARGET` is the default value of the `target`
parameter at :138 (a real fail-closed oracle on the default path,
:92-93), and its KEY SET generates the six required CLI flags
(:213-219) and reassembles the dict in `main()` (:232).
`TARGET_RELATION` is production-unreferenced (production recomputes at
:159); it serves only its own tests.

## What Changes

Issue #1245's recommended KISS route — zero behavior change, values and
key order verbatim:

1. replay.py: `from packages.common import node27_container_contract
   as contract` (precedent: capture.py:42; direction scripts→packages
   is allowed and already used by this file for `evidence_io`/
   `safe_fs`), then
   `TARGET = dict(contract.RECOVERY_TARGET_FIELDS)` and
   `TARGET_RELATION = f"{contract.RECOVERY_TARGET_CHUNK_SCHEMA}.`
   `{contract.RECOVERY_TARGET_CHUNK_NAME}"`. Key order of
   `RECOVERY_TARGET_FIELDS` matches the current literal exactly
   (hypertable_schema, hypertable_name, chunk_schema, chunk_name,
   range_start, range_end); the key SET is the CLI contract (:213-219
   flag generation, :232 reassembly), while order only affects
   usage/help text — receipt JSON bytes are order-independent because
   `_canonical` (:61-63) serializes with `sort_keys=True`.
2. Replay tests: break the tautology at the assertion side — import
   the contract and anchor the expectations to it
   (`receipt["target"] == dict(contract.RECOVERY_TARGET_FIELDS)`,
   `decompress_return_relation` and the `decompress_chunk` call params
   against the contract-derived relation). The `_responses` stub keeps
   returning `replay.TARGET_RELATION` (it simulates the DB echoing
   what production computed), so a replay-side deviation makes the
   production comparison and the contract-anchored asserts disagree.
   The stub's catalog datetimes (:59/:63) are ALSO derived from
   `contract.RECOVERY_TARGET_RANGE_START/END` (via
   `datetime.fromisoformat(... .replace("Z", "+00:00"))`) — same
   DB-echo semantics as the relation, and it removes the last
   test-side literal a retarget would have to hand-edit in this file.
3. Drift guard (supervisor tests :2823+): add two clauses —
   `dict(replay.TARGET) == dict(contract.RECOVERY_TARGET_FIELDS)` and
   `replay.TARGET_RELATION ==` the contract-derived relation — and
   update the docstring, scoped as in change 4: no production Python
   copy remains outside the bound set (replay was the last); runbook
   prose and the intentional test byte-freezes are not derivation
   sites; the plan_author e2e stays the independent non-derived oracle.
4. Contract SoT ledger (comment text ONLY): move replay's
   `TARGET`/`TARGET_RELATION` into the bound list (binding mode:
   derived directly), and replace the "Coverage is NOT total" paragraph
   with the closure statement scoped precisely: no PRODUCTION PYTHON
   copy of the six-field target remains outside the bound set; runbook
   prose (docs/runbooks/tier-node27-timeseries-storage.md) and the
   intentional test byte-freeze literals are not derivation sites; the
   requirement's naming-with-follow-up rule stays for any future copy.

### Red-proof honesty (corrected from the issue's AC framing)

Under the derivation route the new guard clauses are definitional
against a CONTRACT-side flip (both sides move together), so the issue's
"flip `contract.RECOVERY_TARGET_CHUNK_NAME` → the new guard must go
red" is unsatisfiable as written. The honest proof obligations are:

- (a) replay-side reversion: scratch-replace the derivation with a
  drifted literal (chunk_name flipped — chosen deliberately: it is one
  of the four fields the OLD tests were fully tautological over, so a
  red here proves the tautology is broken; a range field would have
  gone red even pre-change via the stub datetimes) → the new guard
  clause AND the contract-anchored replay asserts go red. This is the
  non-tautology proof.
- (b)/(c) contract-side flips (`RECOVERY_TARGET_CHUNK_NAME`,
  `RECOVERY_TARGET_RANGE_START` separately) → the EXISTING bound-set
  net goes red (byte-freezes, schema-consts drift-guard clause,
  plan_author gate, e2e — the 6-failure set recorded in PR #1246 for
  the chunk_name flip; the range flip reddens 5 of them because
  `RECOVERY_PREFLIGHT_SQL` interpolates only the four identity fields
  and its freeze cannot react to a range change; all failures live in
  the capture/live_evidence/supervisor suites, benchmark contributes
  zero), while the new replay clauses AND the replay
  fake-DB tests stay green by derivation — the latter holds for the
  range fields ONLY because the stub datetimes are also derived
  (change 2); with literal stub datetimes a contract range flip would
  have turned both replay tests red through the :92-93 fail-closed
  path. Recorded honestly, not claimed as sensitivity they don't have.

## Non-goals

- Changing any recovery-target value or key; runtime target selection;
  receipt schema/fields; `produce_recovery_receipt` failure semantics.
- Touching #1242/#1243/#1244 deliverables (`RECOVERY_TARGET_FIELDS`,
  schema consts, supervisor `target_args`, capture SQL constants,
  verifier argv derivation).
- The real-DB integration test
  (`test_real_timescaledb_production_entrypoint_...`) — it builds its
  own ephemeral target and passes explicit argv; the default parameter
  is not on its path. Untouched.
- #1240 (INVOCATION_ARGV island), #1090 (RECORD/EXEC docker argv
  split).
- `packages/` importing `scripts/` (forbidden direction; only comment
  edits in packages).
