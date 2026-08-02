# Tasks: bind-replay-recovery-target-copy

Fixture level: compact · Repair intensity: light · Issue #1245

Triage note: XS-S, terminal closure of the #1087→#1242→#1244 lane;
fully locally verifiable, zero runtime behavior change. Risk axes:
(1) key SET is the CLI contract — `_parser()` :213-219 generates the
six required flags with `for name in TARGET` and `main()` :232
reassembles the dict; `contract.RECOVERY_TARGET_FIELDS` key set AND
insertion order equal the current literal (verified pre-edit; order
only affects usage/help text — receipt bytes are order-independent via
`_canonical` :61-63 `sort_keys=True`); (2) the derivation makes the
new guard clauses definitional vs contract-side flips — red proof MUST
follow the corrected framing in proposal.md (replay-side reversion is
the non-tautology proof; contract-side flips are caught by the
existing net, recorded honestly); (3) the `_responses` stub stays on
DB-echo semantics but its inputs derive from the contract: :62 keeps
returning `replay.TARGET_RELATION`, and the catalog datetimes :59/:63
are rebuilt from `contract.RECOVERY_TARGET_RANGE_START/END` via
`datetime.fromisoformat(... .replace("Z", "+00:00"))` — otherwise the
stub datetimes become the last unnamed test-side retarget hand-edit
and a contract range flip turns both replay fake-DB tests red through
replay.py :92-93 (only the ASSERTIONS anchor expectations to the
contract; the stub models what the DB echoes); (4) the integration
test (:106-179) is untouched — note it is NOT just collected:
`SQL Migration Dry Run` (ci.yml :184-185) runs `pytest -q -m
integration` against a real TimescaleDB service and EXECUTES replay.py
as a subprocess (tests :149-168), making it the only real-execution
oracle for the new `packages.common` import (local macOS has no DB);
(5) CI: replay.py hits BOTH the backend filter and the narrow
`database` filter, so this PR runs "SQL Migration Dry Run" in addition
to targeted Unit Tests — keep the PR non-draft; note for the future:
a replay.py-only PR selects only the replay test file, so the extended
drift guard (supervisor suite) rides targeted CI only when the
supervisor test file itself is in the diff — the contract-anchored
asserts in the replay test file are the targeted-CI-resident guard.
Single review round.

Must preserve:
- All six recovery-target values AND key set/order verbatim; receipt
  JSON bytes unchanged (guaranteed by `sort_keys=True`, verified by
  the unchanged round-trip assert :79).
- `CATALOG_SQL`, `IDENTITY_SQL`, `produce_recovery_receipt` logic,
  `_parser`/`main` code untouched (they consume `TARGET` unchanged).
- `test_real_timescaledb_production_entrypoint_decompresses_ephemeral_exact_fixture`
  untouched.
- `packages/**` carries NO value/logic change — ONLY the SoT ledger
  comment edit (contract :85-111 region as of master 86edf883).
- Baselines green at master 86edf883: replay 2 passed + 1 skipped
  (integration), supervisor 127, capture 11, live_evidence 277.

## Implementation tasks

- [x] 1. replay.py: add `from packages.common import
  node27_container_contract as contract`; replace the `TARGET` literal
  (:24-31) with `TARGET = dict(contract.RECOVERY_TARGET_FIELDS)` and
  `TARGET_RELATION` (:32) with the f-string over
  `contract.RECOVERY_TARGET_CHUNK_SCHEMA`/`RECOVERY_TARGET_CHUNK_NAME`;
  a short comment stating the derivation (issue #1245) and that the
  key set doubles as the CLI flag contract. No other changes.
- [x] 2. Replay tests: import the contract; re-anchor the assertions
  in `test_fake_db_exact_decompression_publishes_structured_receipt`
  (:80/:81/:86) to contract-derived expectations (six-field dict; the
  relation string built from the contract chunk consts). Keep the
  `_responses` stub on `replay.TARGET_RELATION` AND derive its catalog
  datetimes :59/:63 from `contract.RECOVERY_TARGET_RANGE_START/END`
  (risk axis 3). The mismatch test (:89-103) needs no assertion change
  (it asserts failure semantics, not target identity).
- [x] 3. Drift guard: extend
  `test_recovery_target_constants_match_the_live_evidence_schema_consts`
  (supervisor tests :2823+) with the replay import and two clauses:
  `dict(replay.TARGET) == dict(contract.RECOVERY_TARGET_FIELDS)` and
  `replay.TARGET_RELATION == f"{contract.RECOVERY_TARGET_CHUNK_SCHEMA}.{contract.RECOVERY_TARGET_CHUNK_NAME}"`;
  update the docstring closure statement, scoped precisely: no
  production Python copy remains outside the bound set (replay was the
  last), the plan_author e2e stays the independent non-derived oracle.
- [x] 4. Contract SoT ledger (comment text ONLY): move replay
  `TARGET`/`TARGET_RELATION` into the bound "derived from it directly"
  bullet; delete the "Coverage is NOT total ... tracked in issue
  #1245" paragraph and state closure scoped to production Python
  copies (runbook prose and intentional test byte-freeze literals are
  not derivation sites; any future copy must be named with a tracked
  follow-up per the spec requirement).
- [x] 5. Red proof (scratch mutations, restored, outputs recorded,
  per proposal.md corrected framing):
  (a) replace replay's derivation with a drifted literal
  (`chunk_name` → other value — deliberately one of the four fields
  the OLD tests were tautological over; keep `TARGET_RELATION` derived
  or matching the drift — record which) → new guard clause red AND the
  contract-anchored replay assert red;
  (b) flip `contract.RECOVERY_TARGET_CHUNK_NAME` alone → existing net
  red: expect the PR #1246-recorded 6-failure set, all located in the
  capture/live_evidence/supervisor suites (benchmark contributed zero
  in #1244 and is not in this fixture's oracle set — replay replaces
  it); new replay clauses AND replay fake-DB tests
  green-by-derivation (record honestly);
  (c) flip `contract.RECOVERY_TARGET_RANGE_START` alone → existing net
  red — 5 failures, not 6: the catalog_post byte-freeze +
  schema-consts clause + plan_author gate + state-machine/validator +
  e2e; the `RECOVERY_PREFLIGHT_SQL` byte-freeze interpolates only the
  four identity fields (capture.py:602-613 carries no range values) so
  it cannot react to a range flip; replay tests green ONLY because
  task 2 derived the stub datetimes — record that this is the task-2
  dependency, not guard sensitivity. Restore and verify clean diff.
- [x] 6. Oracle: `uv run pytest -q
  tests/test_node27_timeseries_decompression_replay.py
  tests/test_node27_timeseries_compression_supervisor.py
  tests/test_node27_timeseries_compression_capture.py
  tests/test_node27_timeseries_compression_live_evidence.py` → green
  (baseline 2+1skip / 127 / 11 / 277 + any new asserts stay within
  existing tests); `uv run ruff check .`; `git diff --stat` → replay
  script + replay tests + supervisor tests + contract (comment-only)
  (+ fixture tasks.md); `openspec validate
  bind-replay-recovery-target-copy --strict --no-interactive`.

## Required evidence

- Key SET + insertion-order verification output (contract fields ==
  pre-change literal; the set is the CLI contract, order is usage-text
  only); red-proof outputs for (a)/(b)/(c) with the honest
  definitional note; pytest counts; ruff; comment-only proof for
  packages diff.

## Non-goals

- Value/key changes; receipt schema or failure-semantics changes;
  integration-test edits; #1240/#1090 surfaces; deleting the default
  parameter (issue's rejected option B); packages logic changes.
