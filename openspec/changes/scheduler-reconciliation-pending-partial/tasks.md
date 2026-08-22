## 1. Fixture and Invariant Gate

- [x] 1.1 Pass one read-only fixture review and strict validation before implementation.
- [x] 1.2 Preserve the governing invariant across producer, quality, pass counts, readiness recount, nested defer, multi-hop confirmed submission provenance, final-span timing, durable no-op, compaction, and downstream stop surfaces.
- [x] 1.3 Keep Part A and Part B in one mergeable PR; record any implementation deviation.

## 2. Part A — Evidence and Readiness

- [x] 2.1 Add one governed reconciliation-status family consumed by candidate quality so all three tokens are non-success while failed classification remains false.
- [x] 2.2 Make producer partial counting include cycle/stage reconciliation statuses for attempted cycle-derived evidence.
- [x] 2.3 Add `reconciling` to the review-blocked pass vocabulary without adding it to passed or live-submitted compatibility.
- [x] 2.4 Make readiness partial and producer-partial predicates recognize all three tokens while blocked/failed/submitted inference remain false.
- [x] 2.5 Add direct truth-table tests for candidate quality and readiness outcome flags.
- [x] 2.6 Add a produced-artifact/readiness test: zero-submission reconciling row yields partial count one, public readiness blocked, and no status/cardinality errors.

## 3. Part B — Nested Defer State Machine

- [x] 3.1 Extend the nested defer family with `submit_result_ambiguous` and `reconcile_unverified` only.
- [x] 3.2 Add an explicit governed status-to-cycle-terminal mapping; duplicate skip maps to its skip terminal and reconciliation statuses map to `reconciling`.
- [x] 3.3 Return from the retry helper before pending-task mutation or aggregation synthesis, preserving full-cohort restoration.
- [x] 3.4 Flip the two existing collapse tests into positive defer oracles: no failed task stamp, no downstream stage, no attempt N+1, cycle `reconciling`.
- [x] 3.5 For nested `reconcile_unverified`, prove no second partial/failed cycle-status write lands beyond the nested producer's existing write/event.
- [x] 3.6 Preserve duplicate-skip, `submission_failed`, ordinary partial retry success/failure, and unknown fail-closed behavior.
- [x] 3.7 When nested reconciliation replaces a stage with a raw pending result, preserve an already-confirmed non-empty Slurm master identity without restoring stale task outcomes or inferring submission from the pending token itself.
- [x] 3.8 Attribute both nested reconciliation defer terminals as `basin_count=N`, `submitted_count=0`, `failed_count=0` in the final stage span while retaining real-failure timing.
- [x] 3.9 Add scheduler-produced artifact oracles proving the confirmed first dispatch remains submitted/called/not-proven-absent through compaction; bare pending remains non-submitted.
- [x] 3.10 Apply the same governed empty-raw-only identity preservation to the outer same-stage retry replacement without changing raw retry fields, task rows, retry quota, or non-pending behavior.
- [x] 3.11 Add an outer whole-array failure -> ambiguous retry produced-artifact oracle covering returned, persisted, and bounded submission proofs.
- [x] 3.12 Replace adjacent-result provenance with one stage-loop-local confirmed-master owner shared by indexed, trailing, and nested projection points; empty-ID intermediate results SHALL NOT clear it.
- [x] 3.13 Add a normal full-chain single-hop oracle that necessarily exercises the indexed outer replacement and proves the same returned identity/raw metadata/no-work contract as the trailing oracle.
- [x] 3.14 Add a real multi-hop confirmed -> empty-ID rejected `submission_failed` -> empty-ID ambiguous retry oracle, including durable attempt separation and produced/persisted/bounded exact submission proof.

## 4. Red Proof and Evidence Floor

- [x] 4.1 Produce one batched pre-change red run for the Part A truth table/artifact tests and Part B nested defer tests; leave no `red-proof` stash.
- [x] 4.2 Run new Round 1 invariant tests red on `56e17cbacb31a0040797f839a6528d1a0e987b9c` and green after the fix; record the exact command and outcomes.
- [x] 4.3 Run the Phase 6.2 outer-retry invariant tests red on `ab0ad599cda2990376569378cfe3950118b641ff` and green after the fix; record the exact command and outcomes.
- [x] 4.4 Run the Round 2 indexed and multi-hop invariant tests red on `4ce5fce88424c98ec215ccf70c5f46162b238bea` and green after the stage-owner redesign; record exact commands/outcomes.
- [x] 4.5 The existing focused #1326 bundle passes.
- [x] 4.6 `uv run pytest -q tests/test_orchestration_chain.py tests/test_production_readiness_validation.py tests/test_production_scheduler.py` passes.
- [x] 4.7 `uv run ruff check .` passes.
- [x] 4.8 `openspec validate scheduler-reconciliation-pending-partial --strict --no-interactive` passes.

## 5. Scope and Oracle Integrity

- [x] 5.1 Confirm no production-status alias, DB schema, Slurm gateway, reserve-gate, forcing-ready-partial, or public API change entered the diff.
- [x] 5.2 Confirm no existing test/spec/CI oracle was weakened; former collapse tests are replaced only because the governed contract explicitly changes.
- [x] 5.3 Record inspected unchanged sibling surfaces and implementation deviations, including the original red-proof chronology deviation, Round 1 contract closure, Phase 6.2 outer-retry invariant miss, and Round 2 depth retro.
