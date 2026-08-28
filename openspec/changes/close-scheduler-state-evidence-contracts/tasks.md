## 1. Retry-state truth (#1579 and #1572)

- [x] 1.1 Remove the top-level flat retry count from every explicit stage-scoped attempt derivation while preserving stage-less flat-first behavior and the existing canonical/raw-stage row predicates. Evidence floor: a cross-stage persisted count yields zero for unrelated explicit stages under both large and small windows, while stage-less controls and #1179 floor regressions remain green.
- [x] 1.2 Make the PostgreSQL candidate-state job read provide the complete scoped row population to the shared projection, without SQL stage aliases or retry-suffix parsing. Evidence floor: returned `pipeline_jobs` remains pure-freshness top-`job_limit`, `pipeline_jobs_total` is the true row count, and `state_truncated` matches it.
- [x] 1.3 Add an `@pytest.mark.integration` node-27 real-DB regression for reverse truncation, friendly geometry, exact totals, hard returned-row bounds, and DB/file-journal per-stage attempt parity. The test MUST obtain the live `integration_database_url`, construct a real `PsycopgOrchestratorRepository`, and insert/read real `ops.pipeline_job` rows; `CapturingRepository`, SQL-result stubs, and other mock repositories are forbidden as this oracle. Input: an old `*_forecast_retry_N` row plus at least `job_limit` fresher other-stage rows. Expected: stage attempt N on both repositories, exact total, and no file-journal behavior change.

## 2. Submit-attempt provenance (#1692)

- [x] 2.1 Separate confirmed submission from submit-call provenance in candidate evidence and return `unknown_after_attempt` only for producer-proven gateway-crossed ambiguous attempts. Evidence floor: no confirmed identity keeps `submitted=false`; a hand-built token-only pending/reconciling result without gateway provenance produces `slurm_submit_called=false`, execution proof `slurm_submit_proven_absent=true`, and a no-mutation proof that remains proven-absent rather than manufacturing unknown or positive evidence.
- [x] 2.2 Add a real `ProductionScheduler` regression that exercises first-attempt gateway ambiguity through model-run evidence, execution proof, no-mutation proof, persisted artifact, and bounded compaction. Expected: zero confirmed submits, no proven absence, and submit/mutation outcomes remain `unknown_after_attempt` at every layer; keep the token-only proven-absence control in the same requirement-driven matrix.
- [x] 2.3 Preserve true proven-no-submit and confirmed-submit controls, including existing failed-without-ID and confirmed-ID nested/outer pending cases.

## 3. Scheduler pass discovery (#1575)

- [x] 3.1 Establish one governed scheduler pass filename predicate and use it in both readiness root discovery and evidence retention without renaming the no-progress tracker or changing explicit-file input.
- [x] 3.2 Add mixed-root regressions covering tracker, temp/unrelated JSON, passed pass evidence, stable-blocked pass evidence, sorting, and the 16-file cap. Expected: only governed `scheduler_*.json` pass artifacts become readiness items or consume the cap.
- [x] 3.3 Update `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md` to describe the narrowed root-scan contract; keep the shared-root recipes valid.

## 4. Registry skip-cause closure (#1553)

- [x] 4.1 Carry `unreadable_required_files` atomically through publisher skipped-model state and every current not-publishable diagnostic into refresh classification evidence.
- [x] 4.2 Extend the shared bounded skip-cause key set, runtime allow-list/projection, and JSON Schema with an optional `unreadable_required_files` list using existing collection/string bounds. Evidence floor: old receipts without the key remain valid.
- [x] 4.3 Add end-to-end unreadable-file coverage from a skipped model to `registry_cutover_removal_refused`, plus runtime/JSON-Schema parity and truncation coverage. Existing `missing_required_files` set-equality consumers must remain unchanged and green.
- [x] 4.4 Update `docs/runbooks/current-production-ops.md` so operators read all three machine-readable cause lists.

## 5. Verification and delivery

- [x] 5.1 Run focused local tests: `uv run pytest -q tests/test_production_scheduler.py -k "retry_attempt or floor"`; `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py`; `uv run pytest -q tests/test_production_readiness_validation.py -k "scheduler and evidence"`; and `uv run pytest -q tests/test_publish_scheduler_file_registry.py tests/test_basins_discovery.py tests/test_scheduler_file_provider_refresh.py`.
- [x] 5.2 Run `uv run ruff check .` and `openspec validate close-scheduler-state-evidence-contracts --strict --no-interactive` locally.
- [x] 5.3 Push the frozen implementation head, then on node-27 perform the clean-tree/`git pull --ff-only` guard and run the issue #1572 real-DB oracle as `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=<node-27 test DSN> uv run pytest -q -m integration tests/ -k "candidate_state and (truncat or job_limit or stage_attempt)"` (`evidence/node27-real-db-5356f011.md`). The selected test MUST be the real-repository test from 1.3 (live `integration_database_url`, real `PsycopgOrchestratorRepository`, real `ops.pipeline_job` inserts) and MUST NOT be satisfied by existing local `CapturingRepository`/SQL-stub candidate-state tests. Record exact pushed SHA, selected test node id, command, totals, stage attempts, and results; do not connect to node-22 archived PostgreSQL.
- [ ] 5.4 Complete six-lens risk-adaptive cross-review, independent verifier adjudication, clean final gap sweep, required CI, SHA/branch-tip/oracle-integrity gates, Chinese work summary, and pre-authorized merge.

## Evidence Floor

- Fixture: `broad-expanded`; repair intensity: high; issues: #1579, #1575, #1692, #1572, #1553.
- Required local oracle: all commands in 5.1 and 5.2 pass on the final reviewed head; every new behavior test has a batched pre-change red proof and leaves no `red-proof` stash.
- Required live oracle: an `@pytest.mark.integration` node-27 PostgreSQL candidate-state test using `integration_database_url`, a real `PsycopgOrchestratorRepository`, and real `ops.pipeline_job` inserts proves reverse-window stage attempt, true total/truncation, returned-row bound, and DB/file-journal parity on the pushed final behavior head. `CapturingRepository` and SQL stubs are explicitly non-oracles. No node-22 Slurm runtime receipt is required because sbatch/gateway scheduling behavior is unchanged.
- Must preserve: stage-less flat reads, #1179 floors/identity narrowing/pure-freshness selection, confirmed submission, true no-submit, explicit readiness file input, historical receipts, registry gate behavior, and missing-file-only set consumers.
- Non-goals: DB schema migration, retry/gateway policy, tracker rename, relaxed evidence validation, registry publishability, frontend/display, or SHUD numerical changes.
