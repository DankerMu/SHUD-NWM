# Tasks

## Evidence Floor

Every slice PR, before merge:
- `uv run ruff check . --no-cache` clean, and the CI full suite green (every `Unit Tests (full)` shard).
- If the slice adds or edits a spec delta in this change: `openspec validate redesign-orchestration-core --strict --no-interactive` passes.
- The node checks named in its task group's Verification task, at the D5 cadence.
- The slice's task boxes are ticked in this file in the PR's final push.

Order, dependencies and verification follow design.md D1–D5. Appendix C says which slice deletes each SQL-path symbol. Appendix D says which slice rewrites each main-spec passage. Every PR runs `uv run ruff check . --no-cache` and the CI full suite.

## 1. R1 — Retire the orchestrated analysis lane

- [x] 1.1 Before deleting anything, grep evidence writers, logs and pickles for any dependence on `AnalysisOrchestrator.__module__` (rewritten at `chain_compat_runtime.py:78`).
- [x] 1.2 Remove `trigger-analysis` from `services/orchestrator/cli.py`: the import at :16, the handler at :73-86, the click command at :507-512, and the argparse entries at :802 and :879-881.
- [x] 1.3 Delete the Appendix C R1 symbols: `chain_analysis_orchestrator.py`, `chain_analysis.py`, `build_analysis_run_manifest`, `AnalysisPipelineAlreadyActiveError`, the analysis-only branches, and the compat/`__init__` bindings. Update the inventory text and the inventory-coupled tests that name them.
- [x] 1.4 Delete `tests/test_analysis_pipeline.py`, the `AnalysisOrchestrator` assertions in `tests/test_orchestration_chain.py` (:37, :13364, :13376, :13547, :13746, :14210), and the writer-receipt case at `tests/test_gateway_reconcile_writer_receipts.py:134`.
- [x] 1.5 CLI test: every remaining subcommand parses exactly as before, and `trigger-analysis` is an argparse usage error (exit 2).
- [x] 1.6 (separate PR after 1.1–1.5; spec-only) Triage the remaining `analysis-run-pipeline` requirements (`:25-86`) against the worker analysis branches (e.g. `workers/output_parser/parser.py`, `run_type='analysis'`). For each requirement, add a REMOVED delta or rewrite it as a worker-only requirement in this change.
- [ ] 1.7 Verification: on node-22 after deploy, one idle pass and one submitting pass. `cli.py` is the scheduler unit's entrypoint.

Suggested fixture level: expanded - removes a CLI subcommand on the scheduler's entrypoint module.
Minimal mergeable slice: 1.1–1.5 + 1.7 (code deletion) - atomic: the subcommand, the only class it drives, its tests and orphans form one unreachable component; leaving any part keeps an import of a deleted module. 1.6 (spec triage) follows as its own spec-only PR.

Slice triage (R1, #3248):
- Issue type: refactor (deletion). Fixture level: expanded (agree with upstream). Blast radius: the `nhms-pipeline` entrypoint of the node-22 scheduler unit; a dangling import of a deleted module breaks every subcommand.
- Governing invariant: no production entrypoint reaches the analysis lane, and after it is deleted every `nhms-pipeline` subcommand other than `trigger-analysis` parses and exits exactly as before on both parsers, and every module under `services.orchestrator` still imports.
- Sibling surfaces: the click parser (`_click_main`, the production path because click is a hard dependency, `cli.py:1036-1041`) and the argparse fallback (`_argparse_main`); the `launch-file-journal-rollback-writer` argument validation (the writer-receipt case at `tests/test_gateway_reconcile_writer_receipts.py:134`); the chain namespace install (`chain_compat_runtime.py:75-95,504`, `chain_compat_static.py:111,144,386,393`, the `__module__` rewrite tuple at `chain.py:258-268`); `tests/test_orchestrator_first_import.py` (`FIRST_IMPORT_ALLOWLIST` :47, asserted at :106); the facade guard and the inventories; the gateway job-type template map (unchanged, see Keep).
- Delete (re-grep at the current head first):
  - `trigger-analysis` in `cli.py` (both parsers; task 1.2).
  - `chain_analysis_orchestrator.py`, `chain_analysis.py` (and its import at `chain.py:37`).
  - `build_analysis_run_manifest` (`chain_manifests.py:762`, with its references at `:71,:471`).
  - `AnalysisPipelineAlreadyActiveError`: the class at `chain_config.py:41`, the alias at `chain.py:238`, its entry in the `__module__` tuple at `chain.py:262`.
  - `has_active_analysis_run`: the protocol method (`chain.py:390`), the SQL implementation (`chain_repository.py:207`), the compat name tables (`chain_compat_static.py:262,290`), the assertions at `tests/test_orchestration_chain.py:12830,12858`, test fakes (`tests/test_e2e_m3.py:170` and any other).
  - `AnalysisRunContext` (`chain_types.py:112`) and every use: narrow each `ForecastRunContext | AnalysisRunContext` annotation to `ForecastRunContext` (`chain.py:404`, `chain_forecast_execution.py`, `chain_forecast_orchestrator_runtime.py`, `chain_forecast_templates.py`, `chain_manifests.py:828`, `chain_repository.py:357`), delete the `isinstance(context, AnalysisRunContext)` branches (`chain_forecast_execution.py:1687`, `chain_forecast_templates.py:46,68`), `_validate_analysis_template_context` (`chain_forecast_orchestrator_runtime.py:140`) and the aliases (`chain_forecast_execution.py:95`, `chain_forecast_templates.py:7`, imports at `chain.py:70`, `chain_manifests.py:48`, `chain_repository.py:11`).
  - The compat bindings (`chain_compat_runtime.py:75-95,504`; `chain_compat_static.py:111,144,386,393`) and the stale ruff per-file-ignore at `pyproject.toml:133`.
  - Tests: `tests/test_analysis_pipeline.py`; in `tests/test_orchestration_chain.py` the `AnalysisOrchestrator` assertions (task 1.4), the `build_analysis_run_manifest` tests (`:13609-13734`, `:14124-14164`) and the `AnalysisRunContext` uses (`:38,909,1033,2408,2445-2455,14138`); the writer-receipt case at `tests/test_gateway_reconcile_writer_receipts.py:134`; the `FIRST_IMPORT_ALLOWLIST` entry at `tests/test_orchestrator_first_import.py:47`.
  - Inventory text: `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` (every row naming a deleted symbol), `docs/governance/STRUCTURAL_FILE_DISPOSITION_INVENTORY.md` (every hit: `:35` `wc -l` evidence command, `:68-69`, `:164`); stale comments at `run_identity.py:35`, `tests/test_run_identity.py:32`, `tests/test_retention_extra_roots.py:134`.
- Keep (out of R1): `ANALYSIS_STAGES` (`chain_stages.py:93`) and its re-export, `DEFAULT_JOB_TYPE_TEMPLATES`, `config/job_type_templates.yaml`, `infra/sbatch/analysis_*.sbatch` — the gateway and sbatch template-coverage tests use them as the production job-type catalog (`tests/test_real_slurm_gateway.py:21,988,1048,1057`, `tests/test_orchestrator.py:11,295`); worker `run_type='analysis'` branches (task 1.6); the `'analysis_run'` ordering literal in the SQL repository (`chain_repository.py:950`, deleted with the class in R3).
- Risk packs selected:
  - Public API / CLI / script entry → task 1.5 parser test.
  - Legacy compatibility → 1.1 grep, the compat/inventory removals above, the first-import test.
  - Release / packaging → 1.7 (the scheduler unit imports `cli.py`).
- Not selected: Config / project setup, File IO / path safety, Schema / units, Auth / secrets, Concurrency, Resource limits, Error handling / rollback, Documentation (no runtime path of any changes; the deleted code is unreachable from production). The analysis-run-pipeline REMOVED deltas already in this change cover the spec side; 1.6 handles the rest.
- Required evidence:
  - 1.1: `git grep -n 'chain_analysis_orchestrator\|AnalysisOrchestrator'` over the repo plus, on node-22 (read-only), `grep -rl` over the scheduler evidence/state roots under `/ghdc/data/nwm/` and the scheduler journal logs, and a search of `*.pkl`/`*.pickle` files there. Expected: no hit outside the code being deleted. Any hit → stop and report.
  - 1.5, on both parsers: `_argparse_main(["trigger-analysis","--model-id","m","--date-range","r"])` raises `SystemExit` with code 2; `_click_main([...same...])` raises `click.UsageError` (`exit_code == 2`; `standalone_mode=False`), or a subprocess `python -m services.orchestrator.cli trigger-analysis ...` returns rc 2. Both parsers' subcommand sets equal the pre-change set minus `trigger-analysis`, asserted as literal sets: publish-tiles, publish-qdown, cleanup, migrate-scheduler-state, prepare-file-journal-rollback, launch-file-journal-rollback-writer, complete-file-journal-rollforward, plan-production, recover-released-identity-blocked-reservation, demote-reserved-job, bind-reserved-job, census-job-id-scope, list-operator-actions, confirm-operator-reentry (14 names, the same on both parsers; verified with `--help` on both at fb8b5c118). The `trigger-analysis` assertion is red on the pre-change source and green after.
  - Sweep: `git grep -n 'AnalysisOrchestrator\|chain_analysis_orchestrator\|trigger_analysis\|trigger-analysis\|build_analysis_run_manifest\|AnalysisPipelineAlreadyActiveError\|AnalysisRunContext\|has_active_analysis_run\|chain_analysis\b' -- services apps workers scripts packages tests docs/governance pyproject.toml` returns nothing except the new 1.5 test's literal `trigger-analysis`.
  - ruff; CI full suite.
  - 1.7: ff deploy on node-22 at a pass boundary (timer stopped, unit inactive, `git pull --ff-only`, timer restored). Pass when the next idle pass exits 0 with no import error in its journal/log, and a later submitting pass submits at least one job that Slurm accepts; the PR comment cites the pass timestamps and evidence paths.

## 2. R2a — Every test builds the scheduler explicitly in DB-free mode

- [ ] 2.0 First, give the shared scheduler test helpers that other files import an opt-in DB-free form, without changing their defaults: the test `ProductionScheduler` subclass (`tests/test_production_scheduler.py:729`) and `_config` (`:37378`), e.g. `_config(..., db_free=True)`. Every existing caller passes unchanged. The range and file slices switch their callers to the opt-in. This is a test-only PR.
- [ ] 2.1 Retarget the test files that build `ProductionScheduler` / `ProductionSchedulerConfig` without the DB-free flag (24 files; most of the volume is in `tests/test_production_scheduler.py`). Each test passes explicit `adapters=`, file backend selectors and a journal root, and replaces the `_default_adapters` patches and the SQL-factory string patches (Appendix C, R2a row) with injected collaborators. One PR per file, or per group of small files. `tests/test_production_scheduler.py` (61,872 lines) is split into three contiguous line ranges. The flag still selects during these PRs, so each one is green on its own. Acceptance for each PR: every scheduler construction in its scope is explicitly DB-free. Tests that patch SQL factories with `pytest.fail` to prove they are unreachable in DB-free mode (e.g. `tests/test_production_scheduler.py:40702-40770`, `:45483-45516`) are reachability guards, not retarget targets. They stay, and the slice that deletes each guarded symbol (R2c/R3/R4) rewrites them. `tests/test_qhh_production_bootstrap_scheduler.py` (integration, PG registry) is not retargeted here; R2c-1 owns it.
- [ ] 2.2 Delete the tests whose subject is the non-DB-free default path. They would fail at R2b, and nothing in production reaches that path. Examples: `test_slurm_preflight_ready_without_factory_uses_default_orchestrator_path` (`tests/test_production_scheduler.py:25534`, asserting `repository-from-env` / `retry-service-from-env` at :25590, :25616), and the same pattern at :26142 and :26200. Each range slice deletes the ones in its range and lists them in the PR body. Also delete only the `db_free_required=False` arms of parametrized tests (e.g. `tests/test_production_scheduler.py:18327`, :19188). Duck-typed configs (`getattr(config, "db_free_required", False)` consumers) used by tests set `db_free_required=True` explicitly.

Suggested fixture level: compact - test-only.
Minimal mergeable slice: 2.0 (shared helpers) first; then one test file or range per PR, each green on its own.

## 3. R2b — Flip: DB-free is the only scheduler mode

- [ ] 3.1 `scheduler_db_free_required` becomes constant `True` (`scheduler_config/config.py:39,110`). The constructor parameter is still accepted and ignored. The evidence fields `scheduler_db_free_required` (`scheduler_evidence.py:912`) and `db_free_runtime.required` report `true`.
- [ ] 3.2 Make the non-DB-free arms in `scheduler_core.py` and `scheduler_runtime.py` fail closed with a typed error if reached. The backend selectors keep their fail-closed handling (unset → `db_free_selector_missing`, blank → `db_free_selector_blank`, `postgres`/`psycopg` → `db_free_selector_db_backed`, other → non-file; `scheduler_config/db_free.py:54-73`). No default flips to `file`.
- [ ] 3.3 `chain_manifests.py:252` writes `scheduler_db_free_required: "true"` unconditionally. `scheduler_candidate_runtime.py:578` no longer forces `False`.
- [ ] 3.4 Tests:
  - building the scheduler from env with the flag unset, `false` or `true`, and with the file selectors set, yields the file journal, the file retry service, the NFS manifest adapters and the file lock;
  - with a selector unset, it blocks with `db_free_selector_missing` before the lock;
  - the gateway's Slurm export env built from a manifest with `NHMS_SCHEDULER_DB_FREE_REQUIRED` unset in the scheduler process still carries `NHMS_CANONICAL_DB_FREE=true`, `NHMS_FORCING_DB_FREE=true` and the file registry backend (`real_backend.py:1048-1075`).
- [ ] 3.5 Add the Appendix D R2b spec deltas.
- [ ] 3.6 Verification: on node-22, one idle pass and one submitting pass. The submitting pass's job env carries the DB-free worker switches, read from the job's env snapshot or sbatch script. On node-27, the integration-marked tests that construct the scheduler pass (`-m integration`, files that build `ProductionScheduler`).

Suggested fixture level: expanded - production scheduler mode, the manifest contract and the cross-plane job env.
Minimal mergeable slice: atomic - after group 2, the flip, the fail-closed arms and the manifest constant must land together, or a submitting pass could emit a manifest without the DB-free contract.

## 4. R2c — Delete the dead non-DB-free arms

- [ ] 4.1 Delete `_default_adapters` with its re-export, and the SQL-factory arms in `scheduler_core.py` and `scheduler_adapters.py` with their `scheduler.py` re-exports (Appendix C, R2c row). Update the inventory text and tests that name them. Rewrite the DB-free reachability guards that name these symbols. Retire or retarget `tests/test_qhh_production_bootstrap_scheduler.py` (it proves scheduler visibility under a PostgreSQL registry, the arm this slice deletes); if it is retargeted to the file registry, run it on node-27.
- [ ] 4.2 Delete the SQL reconcile store, including its reset/dispose code, from `scheduler_runtime.py`. Delete `tests/test_gateway_reconcile_store_reset.py` and `tests/test_reconcile_sacct_parse.py::test_reset_reconcile_store_disposes_dead_engine` (:101-115).
- [ ] 4.3 Delete the PostgreSQL scheduler lease (`scheduler_core.py:241-250`, its `scheduler_lease.py` branch) and its tests (`PostgresSchedulerLease`, `_postgres_advisory_lock_key` in `tests/test_production_scheduler.py`).
- [ ] 4.4 Add the Appendix D R2c spec deltas. Update `infra/env/README.md` (:169, :321), `docs/runbooks/two-node-deployment-overview.md:423`, `docs/runbooks/scheduler-dbfree-typed-reasons.md:4` and `docs/runbooks/production-service-config.md:14` so the variable is described as the job-env contract, not a mode switch. `docs/runbooks/node22-db-retirement-runbook.md:51` (names `PsycopgOrchestratorRepository`) is updated in R3. Templates keep the variable.
- [ ] 4.5 Verification: on node-22, one idle pass and one submitting pass.

Suggested fixture level: expanded - the scheduler lease (concurrency) and the reconcile path.
Minimal mergeable slice: 4.1 alone (factory arms and adapters); 4.2 and 4.3 follow as separate PRs, each green on its own, because the three arms share no code.

## 5. R3 — Delete `PsycopgOrchestratorRepository`

- [ ] 5.1 Test-only PR first. For each `CapturingRepository(PsycopgOrchestratorRepository)` test and the candidate-state tests (`tests/test_orchestration_chain.py:8283-10049`), record whether it asserts SQL text (to delete in 5.2) or candidate-state semantics. For each semantics case, confirm `chain_repository_state.py` covers it on the file-journal path (`file_orchestration_journal.py:2025`), and migrate it if not.
- [ ] 5.2 Delete the Appendix C R3 symbols and their SQL-text tests, plus `tests/test_real_database_integration.py:339-502` (each test confirmed SQL-only on node-27). Keep the `OrchestratorRepository` protocol. Update the inventory text and tests.
- [ ] 5.3 Add the Appendix D R3 spec deltas, including the MODIFIED `file-orchestration-journal` contract-test requirement already in this change. Update `docs/runbooks/node22-db-retirement-runbook.md:51`, which names the deleted class.
- [ ] 5.4 Verification: node-27 real-DB suites that remain pass. On node-22, one idle pass and one submitting pass.

Suggested fixture level: expanded - persistence layer deletion.
Minimal mergeable slice: 5.1 as a test-only PR; then 5.2 + 5.3 + 5.4 as the deleting PR.

## 6. R4 — Delete the SQL `RetryService`

- [ ] 6.1 Delete the Appendix C R4 symbols. Keep every other name in `retry.py`, including `_auto_retry_job_can_be_reused` and `_safe_error_message` (`retry.py:405-415`).
- [ ] 6.2 Triage `tests/test_retry.py` test by test: delete the `RetryService`-class tests and keep the classify/backoff tests. Update the other Appendix C R4 test references. `check:api-types` stays clean (the OpenAPI document is unchanged).
- [ ] 6.3 Add the Appendix D R4 spec deltas.
- [ ] 6.4 Verification: node-27 C2 receipt (display read routes PASS, deny-write PASS), because `readonly_validation_probe.py` changes. On node-22, one idle pass and one submitting pass.

Suggested fixture level: expanded - an API dependency provider and the display deny-write probe.
Minimal mergeable slice: atomic - the service, its provider, its aliases (`chain.py:96`, `chain_forecast_execution.py:105`) and its only isinstance consumer are one component; deleting part of it fails at import.

## 7. R5 — Delete orphaned `PipelineStore` write methods

- [ ] 7.1 Switch the fixtures that seed rows through `create_job` / `reserve_job` / `reclaim_reservation` / `bind_reservation` (Appendix C, R5 row) to ORM inserts. This is a test-only PR.
- [ ] 7.2 Delete the four methods. Test-only callers do not count as callers. Keep `update_job_status` and `insert_event` (cancel route).
- [ ] 7.3 Verification: node-27 C2 receipt (read routes and deny-write); node-22 one idle pass and one submitting pass (D5 deleting slice). `tests/test_pipeline_persistence.py` runs on SQLite, so CI covers it.

Suggested fixture level: compact - deletion of unreferenced methods in one module plus fixture changes.
Minimal mergeable slice: 7.1 first, test-only; then 7.2 + 7.3.

## 8. T1 — Pin retry/attempt accounting in one table

- [ ] 8.1 Add `tests/test_retry_accounting_table.py` with plain-data rows that call today's pure functions, wherever they live:
  - rows A–F, M, N and Q (design.md Appendix A);
  - the `rsplit("_retry_", 1)[1]) + 1` formulas (`chain_forecast_orchestrator_cycle.py:676-683`, `:750`);
  - the inline `failure_number - 1` term (`:296`);
  - the non-master path (`file_orchestration_journal.py:12455-12459`, pinned on the caller-snapshot `retry_count` it uses today);
  - the status-set difference between `chain_forecast_orchestrator_cycle.py:224,247` and `retry.py:67-78`, pinned as different;
  - the default limit, with `NHMS_SCHEDULER_RETRY_LIMIT` and `SLURM_GATEWAY_MAX_RETRIES` overrides.
- [ ] 8.2 In the same file, add an index of integration rows G–L, O and P: each row's input and expected outcome, plus the existing test that pins it (Appendix A), asserted to exist by node id.
- [ ] 8.3 Verification: CI only (test-only).

Suggested fixture level: compact - test-only.
Minimal mergeable slice: atomic - one new test file.

## 9. T2 — `retry_identity.py` becomes the owner

- [ ] 9.1 Id formatting. Add `retry_job_id(base, attempt)`, keeping the `max(attempt, 1)` clamp, and delegate the four hand-built sites that remain after R4 deletes `retry.py:~286`. Retarget their table rows.
- [ ] 9.2 Attempt formulas. Add `next_stage_attempt`, `mint_attempt(..., is_occupied)`, `inline_retry_attempt`, `next_after_failure_master`, `next_after_failure_non_master` and `restart_stage_from_basins`, and delegate every call site: `chain.py:863`, `chain_forecast_execution.py:573,625`, `chain_forecast_orchestrator_cycle.py:296,676-683,750`, `file_journal_retry_helpers.py:51-54`, `file_orchestration_journal.py:12432,12455-12459,12678`, `chain_runtime_utils.py:329,356`. Row L becomes a plain-data row.
- [ ] 9.3 Stage attempt reads. Add `stage_attempt` and delegate `scheduler_state_rows.py:517-656` and the `_stage_job_sort_key` parser.
- [ ] 9.4 Limit and status sets.
  - The owner holds the default limit constant; `DEFAULT_RETRY_LIMIT` and `RetryConfig`'s default read it. `SlurmGatewaySettings.max_retries` keeps its literal default (the gateway must not import the orchestrator), and a parity test asserts that it equals the owner constant.
  - `NHMS_SCHEDULER_RETRY_LIMIT` and `SLURM_GATEWAY_MAX_RETRIES` keep their readers.
  - The two hard-coded status sets move to the owner unchanged.
- [ ] 9.5 Add the AST guard (design.md D2) with an empty allowlist. It matches only `_retry_` / `RETRY_JOB_ID_MARKER` id construction, so other f-strings such as `f"manual_retry_{key}"` (`scheduler_state_manual_retry.py:914`) are not flagged.
- [ ] 9.6 (after T1, independent of the guard) File the behaviour-change issue for the non-master path and the status-set difference, citing the T1 rows it would edit.
- [ ] 9.7 Verification: per PR, CI. The T1 table's expected values are unchanged in every PR. node-22 batch passes per D5.

Suggested fixture level: expanded - persisted retry identity in a shared state machine.
Minimal mergeable slice: 9.1 alone; 9.2, 9.3, 9.4 follow as separate PRs, and 9.5 lands last when no local formula remains.

## 10. F1 — Compat-defined production helpers move to owners; facade resolution guard

- [ ] 10.1 Move `_now`, `_sleep`, `_bounded_evidence_payload` and `_evidence_status` (`scheduler_compat_runtime.py:195,208,311,315`) into a scheduler owner module. Keep the `_source_*` / `_accepted_horizon_from_hours` aliases (`:72-75`) as explicit aliases in `scheduler.py`. Move `_utcnow` (`chain_compat_runtime.py:209`) into a chain owner module. Each facade imports the moved names explicitly. Owners keep reading them through `_scheduler.` / `_chain.`. The entropy facade guard flags every new `_`-prefixed facade import (`scripts/governance/entropy_audit/facade_guard.py:110-142,505-568`), so add a Guard Hook Seed row for each new name to the matching compatibility inventory, with removal condition "deleted with the inventory in F3".
- [ ] 10.2 Add `tests/test_facade_name_resolution.py`. It statically collects every attribute read through `_scheduler.` / `_chain.` in `services/orchestrator/*.py`, and every name patched or read on `scheduler` / `chain` in `tests/`, and asserts that each one resolves on the imported facade.
- [ ] 10.3 Verification: CI, then node-22 batch passes per D5.

Suggested fixture level: expanded - moves helpers every scheduler pass calls.
Minimal mergeable slice: 10.2 alone first (the guard is green today); then 10.1 as a pure-move PR.

## 11. F2 — Delete installers and `_COMPAT_` tables

- [ ] 11.0a Chain side, test-only first: delete the test assertions that read `_CHAIN_*_COMPAT_*` tables (`tests/test_orchestration_chain.py`, including the inventory-text tests that also read tables at :1246, :1286, :12146, :12471; `tests/test_file_journal_read_blocked_consumers.py:255-257`). Inventory-text tests that read no table stay for F3.
- [ ] 11.0b Scheduler side, test-only first: delete the `_SCHEDULER_*_COMPAT_*` assertions, including `tests/test_production_scheduler.py:1086-1129` and the inventory-text tests that also read tables (e.g. `:816`). Inventory-text tests that read no table stay for F3.
- [ ] 11.0c Scheduler state-compat patch propagation: rewrite the propagation in `scheduler_state_compat.py:169-219` as an explicit owner function, with a test that a patch on the facade is seen by the `scheduler_state_*` owners. The installer still runs.
- [ ] 11.1 Chain side.
  - Replace `globals().update(...)` at `chain.py:147-149,887-889` with explicit alias lines for every name the F1 guard requires.
  - Delete `chain_compat_static.py`, `chain_compat_runtime.py` and the chain-manifest forwarder self-check (`chain.py:~185-204`).
  - Add Guard Hook Seed rows to the chain inventory for every new explicit `_`-prefixed alias, with removal condition "deleted with the inventory in F3", so the facade guard stays at zero signals.
- [ ] 11.2 Scheduler side.
  - Do the same for `scheduler.py:230-234,238-292,686,688-690`: `scheduler_compat_runtime.py`, `scheduler_candidate_runtime.EXPORTS`, `install_scheduler_state_compat`.
  - The installer is deleted; propagation runs through the owner function from 11.0c.
  - Add Guard Hook Seed rows to the scheduler inventory for every new explicit `_`-prefixed alias, as in 11.1.
  - `scheduler_lease._scheduler_compat_function` (`scheduler_lease.py:46-54`) stays a late-binding lookup, renamed without "compat".
- [ ] 11.3 Remove the deleted modules from `FIRST_IMPORT_ALLOWLIST` (`tests/test_orchestrator_first_import.py:38-67`) and the path strings in `tests/test_sqlalchemy_driver_explicit.py:120`, `tests/test_node27_connection_attribution*.py` and `tests/test_file_journal_full_tree_budget_contract.py:374`.
- [ ] 11.4 Test: importing `chain` / `scheduler` leaves no `_COMPAT_` name in either namespace, and the F1 guard stays green.
- [ ] 11.5 Add the Appendix D F2 spec deltas for each side: chain side `chain-facade-deepening` and `import-layering-guard:23`; scheduler side `scheduler-facade-deepening` and `scheduler-discovery-evidence-owner-compat:31-40`.
- [ ] 11.6 Verification: on node-22, one idle pass and one submitting pass for each PR of this group that changes production code (11.0c, 11.1, 11.2).

Suggested fixture level: expanded - legacy compatibility surface and import-time behaviour.
Minimal mergeable slice: 11.0a and 11.0b (test-only) first; 11.0c next; then 11.1 (chain side) and 11.2 (scheduler side), which are independent; neither facade imports the other's compat modules.

## 12. F3 — Delete the compatibility inventories and the facade guard

- [ ] 12.1 Delete `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md`, `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md`, `scripts/governance/entropy_audit/facade_guard.py`, `COMPATIBILITY_FACADE_CONFIGS` (`schema.py:206-217`), and the helpers that use them (`scripts/governance/audit_repo_entropy.py:257-269,408,627,688-694`; `tests/entropy_audit_helpers.py:587,598`).
- [ ] 12.2 Delete the facade-specific tests in `tests/test_entropy_audit_facade_guard_{forwarders,aliases}.py`, `tests/test_entropy_audit_instruction_inventory.py`, `tests/test_entropy_audit_report_contract.py` and `tests/test_entropy_audit_structural_budget.py`, and the inventory-text tests (`tests/test_production_scheduler.py:776-848`, `tests/test_orchestration_chain.py:841`).
- [ ] 12.3 Remove the `compatibility facade` glossary term (`schema.py:227` `required_glossary_terms`, `openspec/glossary.md:17`). Update `services/orchestrator/AGENTS.md` (regenerate it from `instructions/agents/` if it is generated), `docs/governance/entropy-budget.md`, `entropy-burndown-triage.md:247`, `STRUCTURAL_FILE_DISPOSITION_INVENTORY.md:163-164,229-230` and `GOVERNANCE_8_FINAL_EVIDENCE_MAP.md:18-19`.
- [ ] 12.4 Add the REMOVED delta for `compatibility-facade-governance`, the MODIFIED/REMOVED delta for `facade-shrink` ("Compatibility guard remains authoritative"), and the MODIFIED delta for `orchestrator-structural-burndown/spec.md:434-436`. Reword the comment at `services/orchestrator/chain_array_accounting.py:102` that names the inventories. Confirm with `grep -rni "compatibility inventory\|COMPATIBILITY_INVENTORY\|facade guard" docs scripts tests services --exclude='*.jsonl'` that nothing cites them. `openspec/specs/` is excluded because the deltas reach the main specs only at archive, and append-only `*.jsonl` logs are history.
- [ ] 12.5 Verification: CI only, with the entropy audit report-only job green.

Suggested fixture level: compact - governance tooling and docs, no runtime code.
Minimal mergeable slice: atomic - the inventories and the guard that reads them go together, or the guard fails on a missing file.

## 13. J0 — Characterization guard for the journal class

- [ ] 13.1 Add `tests/test_file_journal_class_surface.py`. It pins the four sets from design.md D4 as checked-in sorted lists:
  - method names with signatures;
  - attribution-wrapped public methods;
  - private methods called by the four named external modules;
  - public module-level names importable from the facade.
- [ ] 13.2 Extend the cluster-map late-lookup lint (`tests/test_file_journal_cluster_map.py:233-250`) to cover every `file_journal_*.py` mixin module listed in the cluster map, not only `EXTRACTION_ORDER`.
- [ ] 13.3 Verification: CI only.

Suggested fixture level: compact - test-only.
Minimal mergeable slice: atomic - one guard file plus the lint extension it needs.

## 14. J1 — Retention mixin

- [ ] 14.1 Move C18 (Appendix B) into `services/orchestrator/file_journal_retention.py` as a mixin that the class composes. The retention dataclasses stay importable from the facade. Update the cluster map. J0, `tests/test_file_journal_late_lookup_seams.py` and `tests/test_scheduler_journal_retention_archive.py` stay unchanged and green.
- [ ] 14.2 Verification: CI, then node-22 batch per D5.

Suggested fixture level: expanded - moves persisted-state writer code.
Minimal mergeable slice: atomic - one cluster, pure move.

## 15. J2 — Status/events and identity-blocked release mixins

- [ ] 15.1 Move C17 into `file_journal_events.py`. A mixin with public methods carries `@_install_public_read_attribution`. J0 stays unchanged.
- [ ] 15.2 Move C15 into `file_journal_release.py`. J0 stays unchanged.
- [ ] 15.3 Verification: CI, then node-22 batch per D5.

Suggested fixture level: expanded - moves persisted-state writer code.
Minimal mergeable slice: 15.1 alone; 15.2 is its own PR.

## 16. J3 — Reclaim, operator and cohort-projection mixins

- [ ] 16.1 Move C12 into `file_journal_reclaim.py`.
- [ ] 16.2 Move C14 into `file_journal_operator.py`. The receipt dataclasses stay importable from the facade.
- [ ] 16.3 Move C16 into `file_journal_cohort.py`, with `project_forecast_cohort_tasks` moved whole.
- [ ] 16.4 Verification: CI, then node-22 batch per D5. J0 stays unchanged after each PR.

Suggested fixture level: expanded - moves persisted-state writer code.
Minimal mergeable slice: 16.1 alone; 16.2 and 16.3 are their own PRs.

## 17. J4 — Submit-lifecycle mixin

- [ ] 17.1 Move C13, including `permit_*_retry`, into `file_journal_submit.py`. The back-edges from C11 and C14 resolve through `self`. J0 stays unchanged.
- [ ] 17.2 Verification: CI, then node-22 batch per D5.

Suggested fixture level: expanded - moves persisted-state writer code.
Minimal mergeable slice: atomic - one cluster, pure move.

## 18. J5 — Reconcile-inventory mixin

- [ ] 18.1 Move C7 into `file_journal_reconcile_inventory.py`, keeping the underscore names that `file_orchestration_migration.py` and `journal_scope_census.py` call. J0 stays unchanged.
- [ ] 18.2 Verification: CI, then node-22 batch per D5.

Suggested fixture level: expanded - moves persisted-state writer code.
Minimal mergeable slice: atomic - one cluster, pure move.

## 19. Archive

- [ ] 19.1 In the final push of whichever slice merges last:
  - tick every task;
  - give each touched capability spec a real Purpose paragraph where it says TBD;
  - run `openspec archive redesign-orchestration-core --yes`.

Suggested fixture level: none - bookkeeping inside the last slice's PR.
Minimal mergeable slice: atomic - rides in the last slice's final push.
