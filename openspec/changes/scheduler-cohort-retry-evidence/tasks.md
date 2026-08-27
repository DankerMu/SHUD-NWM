## 1. Fixture and invariants

- [x] 1.1 Keep fixture level `expanded`, repair intensity `high`, and preserve the Invariant Matrix and boundary checklist in `design.md`.
- [x] 1.2 Preserve current master behavior: cohort conjunction, both whitelist memberships/differences, partial-advance, breaker threshold, read-only journal, and absence of branch-only `replay_manual_retry_admission` eligibility.

## 2. Per-model quarantine breaker (#1562)

- [x] 2.1 Change only the occurrence accessor's success gate so aggregate success still counts without projections and `partially_failed` counts only when this model's bounded projection succeeded.
- [x] 2.2 Keep failed/missing/malformed/truncated target projections at count 0, master plus reconciled per-model terminal at count 1, and all non-breaker `_job_is_terminal_success` consumers unchanged.
- [x] 2.3 Update the accessor docstring and `docs/runbooks/scheduler-dbfree-typed-reasons.md` with aggregate-success, partial per-model success, and 256-entry fail-toward-liveness semantics.
- [x] 2.4 Add regression tests for partial target success=1, partial target failure=0, aggregate success without projections=1, failed master=0, distinctness=1, and read-only bytes.

## 3. Bounded mixed-cohort veto evidence (#1199)

- [x] 3.1 Refactor the gate to evaluate the existing per-basin predicate fully while returning the identical all-basin conjunction verdict.
- [x] 3.2 Store at most the first mixed-cohort veto as one invocation-local fixed-shape record with schema/reason, cycle/run/job-stage, cohort/request counts, veto candidate/model/basin, decision, restart stage, and cause.
- [x] 3.3 Attach the record only to the veto candidate's returned `candidate_outcome`; omit it when all or zero basins qualify and never write it to the journal.
- [x] 3.4 Project the record into scheduler candidate execution evidence and retain it through bounded candidate compaction.
- [x] 3.5 Add tests for all whitelisted=True/no record, mixed=False/one exact record, multiple vetoes=first only, zero eligible=False/no record, restart-stage veto, branch-only marker-shaped input still not admitted, candidate binding, receipt projection, and compaction retention.
- [x] 3.6 Satisfy the commit-time structural ratchet: move the new gate/evidence logic into sub-1000-line `chain_forced_resubmit.py` and `chain_array_evidence.py` owners, retain existing cycle/array-accounting import and monkeypatch surfaces through thin forwarders, and move this change's veto matrix to `tests/test_forced_resubmit_veto.py` so `tests/test_warm_start_chaining.py` is byte-identical to the branch baseline.

## 4. Risk packs

- [x] 4.1 Public API / CLI / script entry: **selected** — additive `PipelineResult.candidate_outcomes` and scheduler receipt field; preserve existing constructors and consumers.
- [x] 4.2 Config / project setup: **not selected** — no configuration or deployment-unit change.
- [x] 4.3 File IO / path safety / overwrite: **not selected** — no new path/write behavior; journal byte-identity remains required evidence.
- [x] 4.4 Schema / columns / units / field names: **selected** — fixed-shape typed evidence and model-bound projection semantics; test exact keys/values.
- [x] 4.5 Auth / permissions / secrets: **not selected** — no authorization or secret-bearing input; evidence excludes raw mappings and paths.
- [x] 4.6 Concurrency / shared state / ordering: **selected** — state is per `CycleOrchestrationContext`, first veto in stable basin order, never a shared orchestrator attribute.
- [x] 4.7 Resource limits / large input / discovery: **selected** — one record per cohort, no lists, retained bounded summary, 256-projection fallback documented/tested.
- [x] 4.8 Legacy compatibility / examples: **selected** — old aggregate-success/no-projection rows, old journals, whitelist differences, and absent branch-only helper remain compatible.
- [x] 4.9 Error handling / rollback / partial outputs: **selected** — malformed/truncated rows undercount; partial-success and receipt-compaction paths have explicit tests; no rollback writes.
- [x] 4.10 Release / packaging / dependency compatibility: **not selected** — no dependency or package change.
- [x] 4.11 Documentation / migration notes: **selected** — spec deltas and typed-reason runbook update; no migration.
- [x] 4.12 Geospatial / CRS / basin geometry: **not selected** — no spatial behavior.
- [x] 4.13 Hydro-met time series / forcing windows: **not selected** — no cadence/window/forcing change.
- [x] 4.14 SHUD numerical runtime / conservation / NaN: **not selected** — no solver input or numerical behavior.
- [x] 4.15 PostGIS / TimescaleDB domain behavior: **not selected** — file-journal and returned evidence only.
- [x] 4.16 Slurm production lifecycle / mock-vs-real parity: **selected** — existing array task outcomes drive model success; submission decisions/templates stay unchanged.
- [x] 4.17 External hydro-met providers / snapshot reproducibility: **not selected** — no provider boundary.
- [x] 4.18 Run manifest / QC provenance: **not selected** — authority remains journal master provenance/projections, not run-manifest backfill.
- [x] 4.19 Published NHMS artifacts / display identity: **not selected** — no publish/display consumer.

## 5. Required evidence

- [x] 5.1 Red proof: stash source changes only; the new targeted breaker and veto/receipt tests fail on pre-change source, then restore immediately with no `red-proof` stash left.
- [x] 5.2 Run `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_warm_start_chaining.py tests/test_production_scheduler.py tests/test_scheduler_generation.py tests/test_scheduler_backfill.py` -> all pass.
- [x] 5.3 Run `uv run ruff check .` -> zero findings.
- [x] 5.4 Run `openspec validate scheduler-cohort-retry-evidence --strict --no-interactive` -> valid.
- [x] 5.5 On node-27 final pushed HEAD, run the focused backend tests and record the exact SHA/results; node-22 runtime receipt is not required because sbatch/gateway/resource/submission semantics do not change.
- [x] 5.6 Audit final diff: no threshold/cadence/whitelist/cohort-key/partial-advance/DB/Slurm-template behavior changed, no raw state evidence leaked, and every acceptance criterion from #1562/#1199 is met or the archived-branch deviation is recorded.
- [x] 5.7 Run the large-file commit guard and `tests/test_entropy_audit_script.py -k "structural_file_budget or compatibility_facade or chain"`; all newly authored source modules remain below 1000 lines and legacy forwarders remain identity/monkeypatch compatible.

## 6. Non-goals

- [x] 6.1 Do not add a capability registry, rewrite decision tokens, alter cohort grouping, admit branch-only replay markers, change breaker re-entry, or make scoring/filtering write the journal.
- [x] 6.2 Do not change the two sibling `_job_is_terminal_success` completion predicates or add node-22 production mutation solely to manufacture evidence.