## 1. Fixture and Contract

- [x] 1.1 Confirm fixture review passes and `openspec validate production-status-skipped-duplicate-blocked --strict --no-interactive` succeeds.
- [x] 1.2 Preserve the governing invariant: raw duplicate status stays unchanged, both production projections report `blocked`, and unknown statuses remain `failed`.

## 2. Implementation

- [x] 2.1 Add the duplicate-submission alias to the single production status translator without changing taxonomy or unrelated aliases.
- [x] 2.2 Inspect producer, both evidence consumers, and readiness vocabulary; report any deviation from the declared single-source path.

## 3. Requirement-Driven Tests

- [x] 3.1 Add a direct oracle: `skipped_duplicate_submission -> blocked`, the result is in the taxonomy, and it differs from an unexpected status.
- [x] 3.2 Add `_candidate_stage_evidence_item` and `_stage_run_evidence` projection oracles that preserve raw status and emit `production_status=blocked`.
- [x] 3.3 Keep compatibility oracles for generic `skip` / `skipped` and `unexpected_status -> failed`.
- [x] 3.4 Produce a batched red proof for the new behavior tests against pre-change source and leave no `red-proof` stash.

## 4. Evidence Floor

- [x] 4.1 `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py` passes.
- [x] 4.2 `uv run ruff check .` passes.
- [x] 4.3 `openspec validate production-status-skipped-duplicate-blocked --strict --no-interactive` passes after implementation.

## 5. Non-Goals and Risk Closure

- [x] 5.1 Confirm reserve-gate, stage-terminal, readiness, taxonomy membership, and generic skip semantics are unchanged.
- [x] 5.2 Record implementation deviations, or explicitly record `No deviations`.
