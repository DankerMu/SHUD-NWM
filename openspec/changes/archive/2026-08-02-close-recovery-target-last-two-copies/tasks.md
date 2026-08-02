# Tasks: close-recovery-target-last-two-copies

Fixture level: compact · Repair intensity: light · Issue #1244

Triage note: S, terminal follow-up of the #1087→#1242 lane; fully
locally verifiable, zero runtime behavior change. Risk axes: (1) both
rendered artifacts must be BYTE-EQUAL for the pinned values —
catalog_post SQL (executes on node-27 during live capture; the frozen
string keeps `/* capture:catalog_post */` as the exact leading token —
NOTE the test psql stub matches by substring containment over the
joined argv, NOT by prefix (supervisor tests :1794-1817
`all(token in argv)` semantics), so the real constraint is that the
marker appears verbatim and is unique among stub responses; the
startswith assertion is kept purely as a byte-freeze detail) and the
verifier's expected argv tail (equality oracle on the armed lane);
(2) the verifier derivation must come from `evidence.RECOVERY_TARGET`
(:201-208), NOT a new contract import — transitive closure via the
existing guard (`evidence.RECOVERY_TARGET == contract fields`) is the
design; (3) fixture-rebuild honesty: after rebuild, verifier tail vs
its own fixture is definitional — the retained independent oracles are
(a) the six-field deviation-rejection parametrize, (b) the guard
chain, and (c) the pre-existing e2e
`test_real_state_machine_bundle_verifies_task_4_5_pass`
(tests/test_node27_timeseries_compression_live_evidence.py:4845),
whose decompress argv comes from plan_author's independent literals
through the REAL `verify_bundle` path — it must stay untouched and is
the direct proof the rebuild did not hollow the oracle; (4)
`len(argv) != 20` cardinality (argv[:5]=5 + sha=1 + tail=14) and
argv[:5] prefix checks stay byte-untouched; other kinds untouched;
(5) 270-suite is dense — the fixture rebuild touches only the
decompress argv literals (tests/...live_evidence.py:1009-1020),
nothing else; (6) import order: `_CATALOG_BODY_SQL` is defined at
capture.py:625-636, so the new module-level `CATALOG_POST_SQL` MUST be
placed AFTER :636 (defining it beside `RECOVERY_PREFLIGHT_SQL` :612
would NameError at import and error out all 431 tests). Single review
round.

Must preserve:
- All six recovery-target values verbatim.
- Rendered catalog_post SQL byte-identical (current literal at
  scripts/node27_timeseries_compression_capture.py:446-452).
- Verifier expected decompress argv content identical
  (scripts/node27_timeseries_compression_live_evidence.py:651-680;
  tail :662-678).
- `schemas/**`, supervisor script, benchmark, plan_author untouched;
  `packages/**` carries NO value or logic change — the ONLY permitted
  packages edit is the source-of-truth comment ledger update in task 4
  (contract comment :85-107).
- `test_real_state_machine_bundle_verifies_task_4_5_pass` untouched.
- Baselines green at master e47afaaf: supervisor 127, capture 10,
  live_evidence 270, benchmark 24 (collect-only).

## Implementation tasks

- [x] 1. Capture: extract the catalog_post SQL to a module-level
  `CATALOG_POST_SQL` interpolating `RECOVERY_TARGET[...]` six fields
  (same derivation pattern as `RECOVERY_PREFLIGHT_SQL`, but placed
  AFTER `_CATALOG_BODY_SQL` :636 — see risk axis 6);
  `_capture_catalog_post` uses it; no other changes.
- [x] 2. Verifier: in `_validate_exact_command_argv` kind
  `decompress`, build the expected tail from
  `RECOVERY_TARGET["hypertable_schema"]` etc. (its own :201-208
  constant); keep list shape, ordering, `--receipt-path` association
  handling, cardinality, and prefix checks byte-identical in effect;
  no new imports.
- [x] 3. Tests: (a) byte-freeze `CATALOG_POST_SQL` vs the frozen
  pre-change literal (marker-verbatim + leading-token assertion, with
  the honest substring-matcher rationale from risk axis 1) in capture
  tests; (b) live_evidence: rebuild the decompress argv fixture
  (:1009-1020) from `evidence.RECOVERY_TARGET`; add a
  deviation-rejection case PARAMETRIZED over ALL SIX recovery-target
  fields — each single-field deviation in the tail raises
  `EvidenceError("decompress argv differs")` (satisfies the "any
  single field" scenario); (c) update the widened drift guard
  docstring (tests/...supervisor.py:2823-2857): DELETE the "Two
  production copies remain KNOWN UNBOUND" paragraph (it becomes false)
  and state the new closure: catalog_post SQL derives via capture's
  RECOVERY_TARGET; verifier argv tail derives from its own constant,
  closed transitively by this guard's evidence↔contract clause; the
  plan_author e2e (:4845) remains the independent non-derived oracle.
- [x] 4. Contract SoT ledger (packages/common/node27_container_contract.py
  :85-107, comment text ONLY): move catalog_post SQL and the verifier
  argv tail into the bound list (with their binding mode); replace the
  "two production copies are known unbound" paragraph with the one
  remaining named copy: `scripts/node27_timeseries_decompression_replay.py`
  `TARGET`/`TARGET_RELATION` (:24-32) — NOT fully inert (TARGET is the
  default parameter at :138) — with the tracked follow-up issue number
  the orchestrator supplies before merge (requirement: any copy outside
  the bound set MUST be named with a tracked follow-up).
- [x] 5. Red proof (scratch mutations, restored, outputs recorded):
  (a) flip `contract.RECOVERY_TARGET_CHUNK_NAME` → widened drift guard
  red AND catalog_post byte-freeze red; (b) flip
  `evidence.RECOVERY_TARGET["chunk_name"]` alone → drift guard red
  (evidence↔contract clause) AND
  `test_real_state_machine_bundle_verifies_task_4_5_pass` red (the
  plan_author-derived argv no longer matches the verifier's derived
  tail) — this is the direct proof the fixture rebuild kept an
  independent oracle.
- [x] 6. Oracle: `uv run pytest -q
  tests/test_node27_timeseries_compression_capture.py
  tests/test_node27_timeseries_compression_live_evidence.py
  tests/test_node27_timeseries_compression_supervisor.py
  tests/test_node27_timeseries_compression_benchmark.py` → green
  (431 baseline + new); `uv run ruff check .`; `git diff --stat` →
  capture + live_evidence + contract (comment-only) + capture test
  file + live_evidence test file + supervisor test file (task 3(c)
  docstring) (+ fixture tasks.md); `openspec validate
  close-recovery-target-last-two-copies --strict --no-interactive`.

## Required evidence

- Byte-freeze outputs (catalog_post SQL sha256 before/after); argv
  acceptance + six-field deviation-rejection outputs; red-proof
  outputs incl. the e2e red on evidence-side flip; pytest counts;
  ruff; the replay.py follow-up issue number/URL referenced by the
  task-4 ledger (merge precondition, supplied by the orchestrator).

## Non-goals

- Value changes; schema edits; supervisor/benchmark/plan_author edits;
  packages logic changes (comment ledger only); other argv kinds;
  #1240/#1090 surfaces; binding replay.py's TARGET (named + tracked
  instead).
