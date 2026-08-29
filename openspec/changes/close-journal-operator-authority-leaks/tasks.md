## 1. Close legacy operator-decision writers (#1805)

- [x] 1.1 Make legacy `transition_pipeline_job_submit_evidence` reject `operator_verified_absence` with the typed-authority error before lock/mutation while preserving the versioned path.
- [x] 1.2 Make `record_pipeline_job_reconciliation` reject the same token before lock/mutation.
- [x] 1.3 Add negative tests for both APIs proving byte-identical journal state and zero events, plus positive controls for legitimate legacy decisions.

## 2. Stop manual-retry attestation inheritance (#1804)

- [x] 2.1 Clear `operator_recovery_attested_at` in the manual-retry successor constructor without modifying the attested source row or retry lineage.
- [x] 2.2 Add a source-selection regression proving `reservation_lost` is outside `MANUAL_RETRY_SOURCE_STATUSES`.
- [x] 2.3 Force an attested released row through the clone seam and prove the persisted successor has no attestation and fails `_operator_recovery_attested`, while the typed recovery API remains green.

## 3. Evidence Floor

- [x] 3.1 Produce one batched red proof for the new behavior tests against pre-change production source, restore the source immediately, and leave no `red-proof` stash.
- [x] 3.2 Run `uv run pytest -q tests/test_orchestrator_demote_core_cas.py tests/test_file_orchestration_journal.py` with all issue scenarios passing.
- [x] 3.3 Run `uv run ruff check .`, `openspec validate close-journal-operator-authority-leaks --strict --no-interactive`, and `git diff --check` successfully.
- [x] 3.4 After push, run the same focused db-free pytest on node-27; node-22 runtime validation is not required because no sbatch, Slurm gateway, SHUD runtime, or scheduling behavior changes.

## 4. Scope And Compatibility Audit

- [x] 4.1 Confirm the dedicated typed demotion/recovery APIs, automatic retry, reclaim, PostgreSQL plane, operator CLI, and legal legacy reconciliation remain unchanged.
- [x] 4.2 Audit sibling retry constructors and record why no additional write lane can inherit `operator_recovery_attested_at`.
- [x] 4.3 Complete the final acceptance-criterion, selected-risk-pack, oracle-integrity, and no-deviation audit for both #1805 and #1804.
