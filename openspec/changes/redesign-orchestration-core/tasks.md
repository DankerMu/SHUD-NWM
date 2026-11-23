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
- [x] 1.7 Verification: on node-22 after deploy, one idle pass and one submitting pass. `cli.py` is the scheduler unit's entrypoint.

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

- [x] 2.0 First, give the shared scheduler test helpers that other files import an opt-in DB-free form, without changing their defaults: the test `ProductionScheduler` subclass (`tests/test_production_scheduler.py:729`) and `_config` (`:37378`), e.g. `_config(..., db_free=True)`. Every existing caller passes unchanged. The range and file slices switch their callers to the opt-in. This is a test-only PR.
- [x] 2.1 Retarget the test files that build `ProductionScheduler` / `ProductionSchedulerConfig` without the DB-free flag (24 files; most of the volume is in `tests/test_production_scheduler.py`). Each test passes explicit `adapters=`, file backend selectors and a journal root, and replaces the `_default_adapters` patches and the SQL-factory string patches (Appendix C, R2a row) with injected collaborators. One PR per file, or per group of small files. `tests/test_production_scheduler.py` (61,872 lines) is split into three contiguous line ranges. The flag still selects during these PRs, so each one is green on its own. Acceptance for each PR: every scheduler construction in its scope is explicitly DB-free. Tests that patch SQL factories with `pytest.fail` to prove they are unreachable in DB-free mode (e.g. `tests/test_production_scheduler.py:40702-40770`, `:45483-45516`) are reachability guards, not retarget targets. They stay, and the slice that deletes each guarded symbol (R2c/R3/R4) rewrites them. `tests/test_qhh_production_bootstrap_scheduler.py` (integration, PG registry) is not retargeted here; R2c-1 owns it.
- [x] 2.2 Delete the tests whose subject is the non-DB-free default path. They would fail at R2b, and nothing in production reaches that path. Examples: `test_slurm_preflight_ready_without_factory_uses_default_orchestrator_path` (`tests/test_production_scheduler.py:25534`, asserting `repository-from-env` / `retry-service-from-env` at :25590, :25616), and the same pattern at :26142 and :26200. Each range slice deletes the ones in its range and lists them in the PR body. Also delete only the `db_free_required=False` arms of parametrized tests (e.g. `tests/test_production_scheduler.py:18327`, :19188). Duck-typed configs (`getattr(config, "db_free_required", False)` consumers) used by tests set `db_free_required=True` explicitly.

Suggested fixture level: compact - test-only.
Minimal mergeable slice: 2.0 (shared helpers) first; then one test file or range per PR, each green on its own.

Slice triage (R2a file and range slices: #3249, #3250, #3251, #3252, #3253, #3254, #3287, #3288, #3289; tasks 2.1, 2.2 per scope):
- Issue type: test. Fixture level: compact (agree): test-only, no production code.
- Blast radius: the slice's test files, plus every importer of a shared builder the slice edits. A test silently rewritten to assert less hides a scheduler regression; a test left on the non-DB-free path breaks at R2b.
- Governing oracle (simulates R2b, where the scheduler mode becomes constant True): `NHMS_SCHEDULER_DB_FREE_REQUIRED=true uv run pytest -q -rfE <scope>` passes, and so does the plain run. Baselines measured at d8e5001e6 (logs and per-test lists in `.workplans/r2a/`, gitignored, on the orchestrator's machine). The full-suite oracle run gave 761 failed and 20 errors:
  - #3249 `tests/test_file_orchestration_journal.py`: 0.
  - #3288: 0. #3253: 0. #3287: 15. #3289: 19.
  - #3254: 64 failed and 20 setup errors in its 16 issue files. The full-suite census also found 10 scheduler-test files that are in no slice's scope. They join #3254 because they are small and take the same edit: `tests/test_quarantine_identity_authority.py` (8), `test_scheduler_terminal_recency.py` (7), `test_retention_root_admission.py` (7), `test_retention_extra_roots.py` (3), `test_cli_cleanup_frontier.py` (2), `test_retention_unexpandable_roots.py`, `test_retention_frontier.py`, `test_retention_copyback_mutex_budget.py`, `test_orchestrator_demote_reserved_job_forcing.py`, `test_orchestrator_bind_reserved_job_forcing_lane.py` (1 each). Four of them import `retention_test_helpers._pass_scheduler` or `breaker_scheduler`.
  - `tests/test_production_scheduler.py`: 579 failing instances (381 functions). By def line at 6bf97fdc8: #3250 (lines 1-20000) 143 instances / 101 functions; #3251 (20001-40000) 336 / 199; #3252 (40001-end) 100 / 81. The per-function list is `.workplans/r2a/baseline-test_production_scheduler.tsv`. A range slice is done when none of its listed functions fails the oracle. The last of the three to merge must bring the whole file to 0.
  - Not R2a (oracle false positives): `tests/test_warm_start_chaining.py` (42), `test_state_manager.py` (5), `test_shud_runtime.py` (4), `test_slurm_array_contract.py` (1). These exercise worker-side DB-free switches that read the process env. R2b changes the scheduler mode and the manifest/export it writes, not how workers read their job env. R2b's task 3.4 re-checks them.
- Oracle blind spots, listed per scope with this grep, each hit handled by the rules below:
  - `git grep -nE 'NHMS_SCHEDULER_DB_FREE_REQUIRED|db_free_required(: bool)? ?= ?False|db_free_required=False|parametrize\(\s*"(scheduler_)?db_free(_required)?"|db_free_required=db_free_required' -- <scope>`. The parametrized False arms this adds are known at `tests/test_production_scheduler.py:19188,46238,46835,46861,47205` (d8e5001e6): `test_tilde_residue_preflight_allowed_roots_is_admitted_by_the_existing_enoent_arm`, `test_preflight_allowed_roots_admits_missing_root_without_blocker`, `test_scheduler_allowed_roots_admits_missing_root_without_blocker`, `test_scheduler_allowed_roots_absorbs_loop_behind_missing_root_through_enoent_arm`, `test_db_free_allowed_roots_admits_missing_root_in_canonical_form`. Their False arms are deleted under 2.2.
  - Known hits: tests that `delenv` the flag themselves, e.g. `_resolve_residue_env` in `tests/test_production_scheduler.py` (and its assertion), `tests/test_scheduler_backfill.py` `test_db_plane_*`, `tests/test_scheduler_root_check_loop_convergence.py:306`; subprocess env files that write the flag (the node-22 exact-cycle wrapper tests, `[submit-db-free-false]` arm); duck-typed configs defaulting to False (`_allowed_roots_config`, `db_free_required: bool = False`); `_resolve_*_for_mode(..., db_free_required=False)` calls.
  - Expected after each slice: every hit in scope is either set to the DB-free value or deleted under (b). Each remaining hit is listed in the PR body with its reason.
  - The R2a-0 helper tests that assert the env-unset default is False belong to R2b (task 3.4), not to R2a.
- Per failing test (FAILED or ERROR), exactly one of:
  - (a) Retarget. Keep the assertions and build the scheduler explicitly DB-free.
    - Explicit means: `_config(..., db_free=True)` / `scheduler_db_free_required=True` with injected `registry=`, `adapters=`, `active_repository=` and readiness/forcing collaborators. For paths that take no kwarg (`ProductionScheduler.from_env()`, `cli.main` / `_plan_production`, subprocess wrappers), the test itself sets the flag env and the file selectors, paths and journal root (e.g. `_set_db_free_scheduler_env`).
    - Any failure caused by missing selectors, roots or journal root is (a). This covers `TypeError ... NoneType`, `preflight_blocked` where `planned`/`submitted` was expected, and `scheduler_root_preflight_blocked` where an evidence-preflight block was expected. Fixture- or helper-level fixes for setup errors are also (a).
    - When a retargeted test keeps a raising patch on a DB factory (e.g. `_unexpected_registry` / `_unexpected_adapters`), it also patches the DB-free counterpart (`_db_free_file_registry_from_config`, `_db_free_default_adapters`) to raise. Otherwise a "blocks before registry/adapter construction" assertion becomes vacuous.
  - (b) Delete (task 2.2). Only when the asserted behaviour comes solely from a `not db_free_required` branch. The PR body cites that branch's file:line or its Appendix C R2c/R3/R4 symbol. Examples: `repository-from-env` / `retry-service-from-env` arms, `PsycopgModelRegistryStore.from_env`, `PsycopgStateSnapshotRepository.from_env`, `StateManager.from_env`, `_active_repository_from_env`, `_canonical_readiness_provider_from_env`, `_forcing_producer_from_env`, the DATABASE_URL Slurm preflight (`scheduler_gateway.py:29-38`), the SQL reconcile store, the PostgreSQL lease, `db_free_required=False` arms. If the property is mode-independent (path containment, approved roots, secret masking, block-before-lock ordering), the PR body names the DB-free test that already covers it; if none does, use (c) instead of (b).
  - (c) Assertion change. Only once the full DB-free env is in place, and only where DB-free mode yields a different value for the same subject. Examples: an evidence field that reports the mode; `require_runtime_roots` derived from the mode (`config.py:427`); a construction-time `ValueError` (`path_modes.py:193-197`) that DB-free mode expresses as a preflight blocker code, asserted as that code. Each change is listed in the PR body with old → new and the reason. A changed status, `execution_boundary` or error code that stops the pass at an earlier gate is never (c). Weakening (dropping an assertion, widening an expected set) is never allowed.
- Shared builders keep their defaults: add an opt-in kwarg or edit the call sites. When a slice edits a helper that other files import, its oracle also runs every importer: `git grep -lw <helper> -- tests`, minus the file that defines the helper. A plain `import` grep misses parenthesised multi-line imports. This covers `_slurm_roots`, `_gateway_config`, `_scheduler_env_roots`, `_set_scheduler_root_env`, `retention_test_helpers._pass_scheduler` and `breaker_scheduler`.
- Passing tests in scope:
  - Remove `_default_adapters` monkeypatches in tests already built DB-free. They are dead: DB-free construction calls `_db_free_default_adapters(config)`, never `_default_adapters` (`scheduler_core.py:124-128`), and R2c deletes the attribute.
  - Make constructions that rely only on the ambient env flag explicit.
  - Construction census per scope: count the `ProductionScheduler(` / `ProductionSchedulerConfig(` / `_config(` / `from_env(` sites with no explicit DB-free flag in the call and no flag env set in the same test. Expected after: 0, or each exception listed with its reason. This is the evidence for #3249, #3253 and #3288, where the oracle is already 0.
- Untouched: DB-free reachability guards. A guard is a test that is already DB-free, is green in the oracle baseline, and patches a DB factory to raise so as to prove that factory is not reached. They are identified by test name, e.g. `test_valid_db_free_from_env_uses_file_registry_and_canonical_readiness_without_db_factories` and, in `tests/test_file_orchestration_journal.py`, the raising `_active_repository_from_env` / `_orchestrator_repository_from_env` patches in `test_db_free_scheduler_from_env_*` and `test_db_free_scheduler_{status_sync,cancel}_blocks_without_default_db_orchestrator` (#3249 removed only their dead, non-raising `_default_adapters` patches). The slice that deletes each guarded symbol (R2c/R3/R4) rewrites them. Also untouched: `tests/test_qhh_production_bootstrap_scheduler.py` (R2c-1) and the R2a-0 helper tests.
- Risk packs selected: Legacy compatibility (no lost coverage → the oracle and plain runs green; every (b) deletion and (c) change listed with its reason; the census at 0). Not selected: every other pack (test-only).
- Required evidence per PR:
  - The oracle failure/error count for the scope (and importers): baseline above → 0.
  - The plain run green.
  - The construction census before → after.
  - `git grep -n '_default_adapters' -- <scope>` returns only reachability guards.
  - The blind-spot grep hits and their dispositions.
  - The (b) and (c) lists.
  - ruff; CI full suite.

Slice triage (R2a-0, #3286, task 2.0):
- Issue type: test. Fixture level: compact (agree): test-only, no production code, no shared entrypoint. Blast radius: the test files that import helpers from `tests/test_production_scheduler.py`; a changed default would silently flip their mode.
- Must preserve: every existing caller of `_config` (`tests/test_production_scheduler.py:37378`) and of the test `ProductionScheduler` subclass (`:729`) builds exactly the same config and scheduler as before; no existing test body changes. This includes callers that enter DB-free mode through the env var only, e.g. `_db_free_missing_forcing_repair_scheduler` (`:45225-45310`: `_set_db_free_scheduler_env` then `_config(...)` without a flag).
- Must add: `_config(..., db_free=True)`.
  - With `db_free` omitted, `_config` writes no `scheduler_db_free_required` key, so the env-driven default factory (`scheduler_config/config.py:110`) still decides.
  - With `db_free=True`, it sets `scheduler_db_free_required=True` explicitly. Passing both `db_free=` and `scheduler_db_free_required=` raises `TypeError` in the helper.
  - `db_free=True` only sets the mode flag. The file selectors, paths and journal root still come from `_set_db_free_scheduler_env` or explicit kwargs; the helper injects none of them.
- Non-goal: the test subclass is unchanged. The real `__init__` already picks registry, adapters, journal repository and readiness from the config's mode (`scheduler_core.py:118-150`). The subclass overrides only the always-ready readiness provider and the noop reconcile store, and neither touches a DB. `:45291` already builds it in DB-free mode. Its always-ready provider overriding the file readiness provider in DB-free tests is intended. The PR records this as a deviation from the issue's "two shared helpers" wording.
- Risk packs selected: Legacy compatibility (defaults unchanged → evidence (3) and the CI full suite with no existing test edited). Not selected: every other pack (test-only; no runtime, file, schema, config or concurrency change).
- Required evidence:
  1. `_config(tmp_path, db_free=True)` with `NHMS_SCHEDULER_DB_FREE_REQUIRED` unset → `scheduler_db_free_required is True`. Red before the helper change (`TypeError`, unknown kwarg), green after.
  2. One DB-free pass through the opt-in. Call `_set_db_free_scheduler_env(monkeypatch, tmp_path)`, then `monkeypatch.delenv("NHMS_SCHEDULER_DB_FREE_REQUIRED")` so only the opt-in can select the mode. Build `ProductionScheduler(_config(roots["workspace_root"], db_free=True, ...), ...)` as at `:45291-45306` and run one pass. Expected: `result.status != "preflight_blocked"`; `evidence["runtime_config"]["scheduler_db_free_required"] is True`; `evidence["runtime_config"]["db_free_runtime"]["required"] is True`; `evidence["lock"]["lock_type"] == "file"`.
  3. Defaults: `_config(tmp_path)` → `False` with the env var unset and `True` with it set to `true`; `_config(tmp_path, db_free=True, scheduler_db_free_required=False)` → `TypeError`.
  4. ruff; CI full suite.

## 3. R2b — Flip: DB-free is the only scheduler mode

- [x] 3.1 `scheduler_db_free_required` becomes constant `True` (`scheduler_config/config.py:39,110`). The constructor parameter is still accepted and ignored. The evidence fields `scheduler_db_free_required` (`scheduler_evidence.py:912`) and `db_free_runtime.required` report `true`.
- [x] 3.2 Make the non-DB-free arms in `scheduler_core.py` and `scheduler_runtime.py` fail closed with a typed error if reached. The backend selectors keep their fail-closed handling (unset → `db_free_selector_missing`, blank → `db_free_selector_blank`, `postgres`/`psycopg` → `db_free_selector_db_backed`, other → non-file; `scheduler_config/db_free.py:54-73`). No default flips to `file`.
- [x] 3.3 `chain_manifests.py:252` writes `scheduler_db_free_required: "true"` unconditionally. `scheduler_candidate_runtime.py:578` no longer forces `False`.
- [x] 3.4 Tests (also owns two R2a hand-offs: the R2a-0 `_config` helper tests that assert the env-unset default is `False`, and a re-check of the worker env-reader files `tests/test_warm_start_chaining.py`, `test_state_manager.py`, `test_shud_runtime.py`, `test_slurm_array_contract.py`):
  - building the scheduler from env with the flag unset, `false` or `true`, and with the file selectors set, yields the file journal, the file retry service, the NFS manifest adapters and the file lock;
  - with a selector unset, it blocks with `db_free_selector_missing` before the lock;
  - the gateway's Slurm export env built from a manifest with `NHMS_SCHEDULER_DB_FREE_REQUIRED` unset in the scheduler process still carries `NHMS_CANONICAL_DB_FREE=true`, `NHMS_FORCING_DB_FREE=true` and the file registry backend (`real_backend.py:1048-1075`).
- [x] 3.5 Add the Appendix D R2b spec deltas.
- [x] 3.6 Verification: on node-22, one idle pass and one submitting pass. The submitting pass's job env carries the DB-free worker switches, read from the job's env snapshot or sbatch script. On node-27, the integration-marked tests that construct the scheduler pass (`-m integration`, files that build `ProductionScheduler`).

Suggested fixture level: expanded - production scheduler mode, the manifest contract and the cross-plane job env.
Minimal mergeable slice: atomic - after group 2, the flip, the fail-closed arms and the manifest constant must land together, or a submitting pass could emit a manifest without the DB-free contract.

Slice triage (R2b, #3255, tasks 3.1-3.6):
- Issue type: behaviour change. Fixture level: expanded (agree): production scheduler mode, the manifest contract, and the cross-plane Slurm job env. Blast radius: every `ProductionScheduler` construction (prod `cli.py:457`, `ProductionScheduler.from_env`), every run manifest, the gateway export env (`real_backend.py:1048-1075`, `_db_free_slurm_runtime` `:2370`), and the tests that still reach the non-DB-free mode by explicit `False` or by an unset env.
- Recon at 91f7745f3 (line numbers drift; re-grep):
  - Mode: `scheduler_config/config.py:110` env default factory; `:275-276` normalises the field. Production code never passes `scheduler_db_free_required=False`; templates, compose and `scripts/validate_two_node_docker_runtime.py` already require `true`; `file_orchestration_migration.py:776,1236` already pass `True`.
  - Non-DB-free arms in `scheduler_core.py`: `__init__` else-branches (`:118-148`), `from_env` fall-through after `:184`, `_build_scheduler_lease` PG path (`:234`), `_orchestrator_for` (`:455`), `_default_orchestrator_for` (`:525-534`), `_lineage_provider` (`:736`); plus the `if not db_free: return ...` gates at `:716`, `:796`, `:837`, and the identical-arm `_cancel_orchestrator_for` (`:463`).
  - Non-DB-free arms in `scheduler_runtime.py`: `:220`, `:385`, `:627-640`, `:683`, `:729`, `:750`, `:835-837`, `:873`, `_restart_reconcile_store` (`:1861`).
  - Other modules read `getattr(config, "db_free_required", False)` (`scheduler_gateway.py`, `scheduler_preflight.py:534`, `scheduler_runtime_roots.py`, `scheduler_evidence.py`, `scheduler_evidence_payload.py:959`) and `path_modes.py` takes it as a parameter. They are not in 3.2's scope; with a constant-True config their False arms are unreachable from production, and R2c/R3 delete them.
  - Manifest: `chain_manifests.py:240` copies `os.getenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", "")`. `scheduler_candidate_runtime.py:576-578` builds a config shim with `False` when no config is passed (evidence redaction only).
  - There is no scheduler-mode exception class; typed reasons are evidence blocker codes (`db_free_selector_*`, `database_url_forbidden`), documented in `docs/runbooks/scheduler-dbfree-typed-reasons.md`.
- Must preserve:
  - Selector fail-closed handling is unchanged: unset → `db_free_selector_missing`, blank → `db_free_selector_blank`, `postgres`/`psycopg` → `db_free_selector_db_backed`, other → non-file (`scheduler_config/db_free.py:54-73`), all before the lock. No selector default flips to `file`.
  - `database_url_forbidden` still blocks before the lock.
  - The `ProductionSchedulerConfig(scheduler_db_free_required=...)` keyword stays accepted (the rollback/migration tool and existing callers pass it); any value yields `True`.
  - Every already-DB-free pass behaves byte-for-byte as before (evidence fields included), apart from the fields 3.1 names, the manifest field 3.3 names, and the `scheduler_core.py:525-526` `active_repository is None` case 3.2 names.
  - Worker env readers (`workers/*`, `packages/common/state_cli.py:430`) are untouched.
- Must change:
  - 3.1 `scheduler_db_free_required` / `db_free_required` are `True` whatever the env or the keyword says; `db_free_runtime.required` and the evidence field `scheduler_db_free_required` report `true`.
  - 3.2 Each non-DB-free arm listed above raises `SchedulerModeInvariantError` (direct subclass of `Exception`, not of `ValueError`/`RuntimeError`/`OSError`) with `code="scheduler_non_db_free_arm_reached"` and `site="<module>:<function>"`. The raise sits outside any existing `except Exception` (`scheduler_core.py` `_lineage_provider` SQL try, `scheduler_runtime.py` `_restart_reconcile_store` engine-build try). For compound gates only the `not db_free_required` component raises, and the DB-free outcome is unchanged: `scheduler_runtime.py:220` still returns `False` when `mutation_requested` is false; `:750` still skips the fence when the reader is not callable; `:837` keeps the DB-free branch. `scheduler_core.py:525-526`: DB-free with `active_repository is None` raises the same error instead of calling `_orchestrator_repository_from_env()` (an intended DB-free behaviour change). `:530-535`: the DB-free non-file-journal `None` arm stays. The arms stay in place for R2c to delete; `docs/runbooks/scheduler-dbfree-typed-reasons.md` lists the new code. The False arms inside `scheduler_config/config.py` (`:427`, `:665-672`, `:711`), `path_modes.py` and the other scheduler modules are not in 3.2's scope: they are dead after the flip and R2c/R3 delete them.
  - 3.3 `chain_manifests.py` writes `"true"` unconditionally; the candidate-runtime shim says `True`.
- Tests (3.4), each red before the change where it is a behaviour test:
  - From env with the flag unset, `false`, `true` (file selectors set): file journal, file retry service, NFS manifest adapters, file lock; and with the keyword `scheduler_db_free_required=False` too.
  - Selector unset → `db_free_selector_missing` before the lock, with the flag unset.
  - Gateway Slurm export env: the manifest comes from the real `build_cycle_stage_manifest` and the export env from the real `real_backend.py` builder, with `NHMS_SCHEDULER_DB_FREE_REQUIRED` deleted at both manifest build and export render (the gateway ORs its own process env at `real_backend.py:2371`). It asserts `NHMS_CANONICAL_DB_FREE=true`, `NHMS_FORCING_DB_FREE=true` and `NHMS_SCHEDULER_REGISTRY_BACKEND=file`. Red at HEAD (the manifest copies `""`), green after. A hand-built context is not acceptable evidence. The first test in this list also asserts the 3.1 evidence fields; one more test covers the 3.3 candidate-runtime shim with no config passed.
  - Each fail-closed arm: a test that forces the arm (e.g. by calling the method on a config stub reporting `False`, or by patching the property) and asserts the typed error and code. One test per arm or a parametrized table. Compound-gate tests also assert the preserved DB-free outcome (e.g. `_db_free_journal_mutation_blocked(cfg_true, mutation_requested=False) is False`).
  - R2a hand-offs: `tests/test_production_scheduler.py` `test_config_helper_db_free_default_follows_env` (asserts `False` with the env unset) is rewritten to the new constant; `:18590` `_unknown_home_config(db_free_required=False)` and any other explicit-`False` scheduler construction found by the grep below are retargeted or deleted under the R2a (a)/(b)/(c) rules. The four worker env-reader files (`test_warm_start_chaining.py`, `test_state_manager.py`, `test_shud_runtime.py`, `test_slurm_array_contract.py`) are re-run plain after the flip and stay green; their oracle failures came from the oracle setting the worker-read env, which R2b does not do.
  - Integration: `tests/test_qhh_production_bootstrap_scheduler.py` builds `ProductionSchedulerConfig` with no flag and an injected PG registry, so the flip changes its mode. It fails after the flip (no selectors → `preflight_blocked`, then `next()` over empty candidates), so R2b retargets all five constructions (`:162`, `:366`, `:489`, `:580`, `:664`) to the DB-free env (selectors, roots, journal root) with `DATABASE_URL` cleared (else `database_url_forbidden`), keeping the injected PG registry, which is its subject; R2c-1 still decides whether to retire it. `tests/test_orchestration_chain.py:20279` is unaffected (repository-level) but runs too.
- Governing oracle: full suite plain (`uv run pytest -q`) green with no `NHMS_SCHEDULER_DB_FREE_REQUIRED` in the env, and again with it set to `false`. Blind-spot grep over `tests/`: `git grep -nE 'scheduler_db_free_required=False|db_free_required=False|db_free=False|NHMS_SCHEDULER_DB_FREE_REQUIRED", "false"|delenv\("NHMS_SCHEDULER_DB_FREE_REQUIRED"' -- tests`; each hit is listed with its disposition.
- Spec (3.5): `specs/node22-dbfree-runtime/spec.md` is already in this change; re-check it against the implementation. Add MODIFIED deltas to `openspec/specs/slurm-array-runner-integration/spec.md` for: (i) "Configuration construction survives symlink loops in containment bases and parent segments": the requirement text and the scenarios at `:85-98` and `:129-135` are restated for the DB-free construction the scheduler always takes (construction does not raise; the root preflight refuses before the lock with the blocker codes pinned by `test_resolve_residue_config_final_segment_loop_converges_to_structured_refusal` and `test_final_segment_loop_refuses_on_the_construction_route`; the containment refusal remains a helper-level `_confined_path` contract). The `:100` scenario is restated only for the resolve arm (`_resolve_config_path_for_mode`), keeping the existing exclusion of the preserve-final arm and its ADR 0009 text, and cites `test_resolve_residue_config_parent_segment_loop_object_store_root_constructs`. (ii) "...survive an undeterminable home directory on both database arms" (`:137-159`): restated as a single DB-free construction contract, naming the test that replaces `tests/test_production_scheduler.py:18578-18590`. Every scenario name is kept (OpenSpec rejects a MODIFIED delta that drops one). design.md Appendix D's R2b row lists both requirements. The implementer drafts these deltas with code citations; the reviewers check them.
- Risk packs selected: Runtime/config contract (mode constant, selectors fail closed, typed error), Cross-plane contract (manifest → gateway export env → workers), Legacy compatibility (keyword accepted; no lost coverage). Not selected: schema/migration (none), concurrency (no lock or ordering change), security beyond the existing `database_url_forbidden`.
- Required evidence:
  1. The 3.4 tests, red before / green after where they test new behaviour.
  2. Full suite plain green with the env unset and with `false`; ruff; `openspec validate redesign-orchestration-core --strict --no-interactive`.
  3. Blind-spot grep hits and dispositions; the list of fail-closed arms with their tests.
  4. CI full suite.
  5. node-27: `-m integration` for `tests/test_qhh_production_bootstrap_scheduler.py` and `tests/test_orchestration_chain.py` on throwaway `nhms_it_*` DBs.
  6. node-22 (3.6): one idle pass and one submitting pass after the ff deploy at a pass boundary; the submitting job's env snapshot or sbatch script shows `NHMS_CANONICAL_DB_FREE=true` and `NHMS_FORCING_DB_FREE=true`. Deferred while node-22 is paused by the owner (2026-11-20): the PR merges after 1-5, master is not deployed to node-22 until the owner resumes it, and 3.6 is ticked in a later PR after those passes. The first ff on node-22 that contains R2b's merge commit is the 3.6 deploy: it goes to that merge commit (`git merge --ff-only <R2b-merge-sha>`), not to a later master, at a pass boundary, and the idle and submitting passes plus the env snapshot are taken before any later commit reaches node-22. A behaviour-changing slice after R2b (R2c onward) may merge before that, but node-22 still steps through R2b's merge SHA first, and each later slice then gets its own passes (D5). The R2b PR body uses `Part of #3255` (not `Closes`) and records the owner-paused node-22 deviation from AC4/D5 with its date; #3255 stays open with 3.6 and AC4 unticked until the follow-up PR ticks them.

## 4. R2c — Delete the dead non-DB-free arms

- [x] 4.1 Delete `_default_adapters` with its re-export, and the SQL-factory arms in `scheduler_core.py` and `scheduler_adapters.py` with their `scheduler.py` re-exports (Appendix C, R2c row). Update the inventory text and tests that name them. Rewrite the DB-free reachability guards that name these symbols. Retire or retarget `tests/test_qhh_production_bootstrap_scheduler.py` (it proves scheduler visibility under a PostgreSQL registry, the arm this slice deletes); if it is retargeted to the file registry, run it on node-27.
- [x] 4.2 Delete the SQL reconcile store, including its reset/dispose code, from `scheduler_runtime.py`. Delete `tests/test_gateway_reconcile_store_reset.py` and `tests/test_reconcile_sacct_parse.py::test_reset_reconcile_store_disposes_dead_engine` (:101-115).
- [x] 4.3 Delete the PostgreSQL scheduler lease (`scheduler_core.py:241-250`, its `scheduler_lease.py` branch) and its tests (`PostgresSchedulerLease`, `_postgres_advisory_lock_key` in `tests/test_production_scheduler.py`).
- [x] 4.4 Add the Appendix D R2c spec deltas. Update `infra/env/README.md` (:169, :321), `docs/runbooks/two-node-deployment-overview.md:423`, `docs/runbooks/scheduler-dbfree-typed-reasons.md:4` and `docs/runbooks/production-service-config.md:14` so the variable is described as the job-env contract, not a mode switch. `docs/runbooks/node22-db-retirement-runbook.md:51` (names `PsycopgOrchestratorRepository`) is updated in R3. Templates keep the variable.
- [x] 4.5 Verification: on node-22, one idle pass and one submitting pass.
- [ ] 4.6 Delete the remaining dead non-DB-free arms, the orphan `_scheduler_lock_evidence_root_preflight`, and their cascades, with the spec deltas (#3311, R2c follow-up). Verification: CI, plus node-27 for the two qhh integration files, plus one node-22 idle pass and one submitting pass.

Suggested fixture level: expanded - the scheduler lease (concurrency) and the reconcile path.
Minimal mergeable slice: 4.1 alone (factory arms and adapters); 4.2 and 4.3 follow as separate PRs, each green on its own, because the three arms share no code.

Slice triage (R2c-1, #3256, tasks 4.1, 4.4, 4.5):
- Issue type: deletion (behaviour-preserving for every reachable path). Fixture level: expanded (agree): shared scheduler entrypoint (`ProductionScheduler.__init__` / `from_env`) and the facade. Blast radius: `services/orchestrator/scheduler_core.py`, `scheduler_adapters.py`, the `scheduler.py` facade re-exports, `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` (and any inventory test that reads it), every test that patches or names a deleted symbol, the R2b arm table in `tests/test_scheduler_dbfree_only_mode.py`, and docs naming the deleted symbols (`docs/governance/STRUCTURAL_FILE_DISPOSITION_INVENTORY.md:161`, `docs/bugs.md:200`, the `tests/test_production_scheduler.py:889` docstring).
- Recon at 7625b4b0e (re-grep): `_default_adapters` (`scheduler_adapters.py:180`, re-export `scheduler.py:54`); `_canonical_readiness_provider_from_env` (`:408`, `scheduler.py:52`); `_forcing_producer_from_env` (`:427`, `scheduler.py:55`); in `scheduler_core.py`: the `__init__` non-DB-free else-branches (`PsycopgModelRegistryStore.from_env()`, `_default_adapters()`, `:131-132`), the `from_env` fall-through that calls `_active_repository_from_env()` / `_canonical_readiness_provider_from_env()` / `_forcing_producer_from_env()` (`:227-229`), `_orchestrator_for`'s `StateManager.from_env()` arm (`:465`), `_default_orchestrator_for`'s `_orchestrator_repository_from_env()` / `_retry_service_from_env()` arms (`:539`, `:548`), `_lineage_provider`'s `PsycopgStateSnapshotRepository.from_env()` arm (`:753-756`). Tests naming the three deleted functions: `tests/test_production_scheduler.py` (19 hits), `tests/test_scheduler_dbfree_only_mode.py` (3).
- Delete (4.1):
  - The three functions `_default_adapters`, `_canonical_readiness_provider_from_env`, `_forcing_producer_from_env` with their `scheduler.py` re-exports and their inventory rows/text.
  - The SQL-factory call sites listed above, i.e. the non-DB-free branch of each mode conditional in `scheduler_core.py`; each conditional collapses to its DB-free body. With it go the R2b `SchedulerModeInvariantError` raise sites that guarded these arms and every other `scheduler_core.py` / `scheduler_runtime.py` mode branch that is not the SQL reconcile store (4.2) or the PostgreSQL lease (4.3): the `__init__` and `run_once` guards, `_cancel_orchestrator_for`, the `if not db_free` gates (`_db_free_strict_warm_start_required_for`, `_strict_warm_start_for_candidate`, `_successor_warm_start_state_for_candidate`), `_db_free_lock_evidence`, `_db_free_journal_mutation_blocked`. Each collapses to its DB-free body with the DB-free outcome unchanged (compound gates keep their non-mode halves, e.g. `mutation_requested`, the rollback-fence reader check). `_default_orchestrator_for` keeps its `self.active_repository is None` refusal unchanged (same `SchedulerModeInvariantError`, same `site`); only the `not db_free_required` half and the `_orchestrator_repository_from_env()` / `_retry_service_from_env()` expression arms (`:536-549`) go. The retry-service expression collapses to `_db_free_file_retry_service_from_env(...) if isinstance(self.active_repository, FileOrchestrationJournalRepository) else None`. `test_db_free_default_orchestrator_without_a_repository_fails_closed` stays green unchanged.
  - `scheduler_config/config.py` `db_free_runtime_preflight` (`:715-721` dead early return and the `if self.scheduler_db_free_required:` guards beneath it; the #3303 hand-off recorded on #3256) collapses to its DB-free body; its DB-free coverage is the node-22 exact-cycle wrapper test rewritten in #3303.
  - `services/orchestrator/scheduler_mode_invariant.py` stays while any raise site remains, including the DB-free `_default_orchestrator_for` no-repository refusal (not a mode arm, so group 4 does not delete it); the module and its tests are deleted only after that site is retyped or removed by a separately owned change.
  - Not deleted here: `_active_repository_from_env` / `_orchestrator_repository_from_env` (R3), `_retry_service_from_env` (R4), `PsycopgModelRegistryStore` itself (other callers), the reconcile store (4.2), the lease (4.3), the remaining dead False arms in `config.py` (`:427`, `:665-672`), `path_modes.py`, `scheduler_gateway.py`, `scheduler_preflight.py`, `scheduler_runtime_roots.py`, `scheduler_evidence*.py`. These have no Appendix C owner; this slice files one follow-up issue for them (issue-scribe) and the PR body links it.
- Tests (4.1):
  - Every monkeypatch of a deleted symbol is removed (`monkeypatch.setattr` raises on a missing attribute). The DB-free reachability guards that patch them (e.g. `test_valid_db_free_from_env_uses_file_registry_and_canonical_readiness_without_db_factories`, `test_db_free_scheduler_fake_slurm_submission_writes_file_journal_without_database_url`, `test_db_free_runtime_root_run_once_blocks_before_lock`) keep proving "no DB factory is reached". Only the patches of the three deleted names are removed; every other DB-factory patch already in those guards stays unchanged (incl. `PsycopgMetStore.from_env`, `ForcingProducer.from_env`, `PsycopgStateSnapshotRepository.from_env`, `PsycopgOrchestratorRepository.from_env`, `PipelineStore`, `PostgresSchedulerLease` until 4.3). A deleted-name patch that injects a working value rather than failing (e.g. `tests/test_production_scheduler.py:41179`) is dead at HEAD; it is removed, and if the test's assertions need that adapter it is re-injected via `_db_free_default_adapters`.
  - The R2b arm-table rows in `tests/test_scheduler_dbfree_only_mode.py` for deleted raise sites are deleted; the "preserved DB-free outcome" tests for collapsed compound gates stay and stay green. Add one test that the deleted names are gone from the facade and owner modules (`not hasattr(...)`), unless an existing inventory/guard test already asserts it.
  - `tests/test_qhh_production_bootstrap_scheduler.py`: after R2b it injects `PsycopgModelRegistryStore` explicitly; this slice deletes only the `from_env` factory arm, so the test keeps working unchanged. Keep it: it injects `PsycopgModelRegistryStore` and adapters explicitly, so none of the deleted code is on its path; its subject is the bootstrap's PG registry rows satisfying the `ModelRegistryReader` contract the scheduler consumes (a configuration production cannot select). This is a deviation from the issue's "retire or retarget" and from design.md Appendix C; it is recorded in the PR body deviation section, the Appendix C R2c entry is amended to "kept (R2c-1): explicit PG-registry injection, independent of the deleted arms", and the test is re-run on node-27.
- Docs and spec (4.4): Appendix D R2c spec deltas (`pipeline-job-persistence/spec.md:10, 12-14, 42-44, 83`, "PostgreSQL-backed scheduler mode"; `file-orchestration-journal/spec.md:309, 650`, non-DB-free reconciliation), MODIFIED/REMOVED with every kept scenario name. 4.4 is the group's only spec task, so this slice also writes the deltas for code 4.2 deletes (`file-orchestration-journal:309, 650`; the reconcile half of `pipeline-job-persistence:83`); they state the end state after 4.2, which is valid because the change archives only at 19.1. At `:309` and `:650` only the "non-DB-free" clause is dropped; "generic" reconciliation/repository stays. Every scenario inside a MODIFIED requirement is re-read (incl. `pipeline-job-persistence:47-50, 85-108`) so none still implies a PostgreSQL-orchestrator write; the PR body notes that the SQL reconcile store still exists, fail-closed, at this merge. Update `infra/env/README.md` (:169, :321), `docs/runbooks/two-node-deployment-overview.md:423`, `docs/runbooks/scheduler-dbfree-typed-reasons.md:4`, `docs/runbooks/production-service-config.md:14` so `NHMS_SCHEDULER_DB_FREE_REQUIRED` is described as the job-env contract, not a mode switch; `docs/runbooks/scheduler-dbfree-typed-reasons.md` drops the deleted raise sites from its `scheduler_non_db_free_arm_reached` table. Templates keep the variable. `docs/bugs.md:200`, `docs/governance/STRUCTURAL_FILE_DISPOSITION_INVENTORY.md:161` and the `tests/test_production_scheduler.py:889` docstring name the deleted symbols: update or mark historical.
- Must preserve: every DB-free path byte-for-byte (selectors fail closed, `database_url_forbidden`, evidence fields); no factory that touches PostgreSQL becomes reachable; `_db_free_default_adapters` stays.
- Risk packs selected: Legacy compatibility (no lost coverage; guards still prove unreachability), Runtime/config contract (entrypoint behaviour unchanged). Not selected: concurrency (lease is 4.3), schema.
- Required evidence:
  1. `grep -rnw '_default_adapters\|_canonical_readiness_provider_from_env\|_forcing_producer_from_env' services` returns nothing (`_db_free_default_adapters` stays); same grep over `tests` and `docs/governance` lists only intentional mentions.
  2. The list of collapsed conditionals and removed raise sites, each with the DB-free test that still covers its body.
  3. The rewritten guards, each shown red if its kept factory patch is removed and the factory is made reachable (or an equivalent mutation).
  4. Full suite plain green; ruff; `openspec validate --strict`; CI full suite; inventory-coupled tests green.
  5. node-27: `-m integration tests/test_qhh_production_bootstrap_scheduler.py tests/test_orchestration_chain.py` on throwaway DBs.
  6. node-22 (4.5): one idle pass and one submitting pass after the ff deploy, deferred while node-22 is paused (2026-11-20): node-22 steps through R2b's merge SHA first, then this slice's merge SHA, each with its own passes. The PR uses `Part of #3256`; #3256 stays open with 4.5 unticked.

Slice triage (R2c-2, #3257, tasks 4.2, 4.5):
- Issue type: deletion (behaviour-preserving for every production path). Fixture level: expanded (agree): the scheduler's restart-reconcile path. Blast radius: `services/orchestrator/scheduler_runtime.py` (`_restart_reconcile_store`, `_reset_reconcile_store_after_error`, the `_run_restart_reconcile` caller at `:1529-1588`), the `scheduler_core.py` forwarders and `__init__` attributes (`:119-120`, `:235-243`), the `scheduler.py` constant `RECONCILE_DB_STATEMENT_TIMEOUT_MS` (`:317`) and its `scheduler_runtime.py:30` alias, the R2b arm table in `tests/test_scheduler_dbfree_only_mode.py`, `tests/test_gateway_reconcile_store_reset.py`, `tests/test_reconcile_sacct_parse.py:95-115`, `docs/runbooks/scheduler-dbfree-typed-reasons.md:884`, `services/orchestrator/scheduler_mode_invariant.py` docstring.
- Governing invariant: restart reconcile builds no SQL engine or store; for every run its `restart_reconcile` evidence block (and everything derived from it) is byte-identical to 133d8a529.
- Sibling surfaces: the lease arm that still raises (4.3, `scheduler_core.py` `_build_scheduler_lease`); the R2b arm table `tests/test_scheduler_dbfree_only_mode.py:368-374` and its module docstring `:13-16`; `docs/runbooks/scheduler-dbfree-typed-reasons.md:884`; the `scheduler_mode_invariant.py:4-8` docstring; the four evidence consumers named under Must preserve.
- Recon at 133d8a529: `_restart_reconcile_store` (`scheduler_runtime.py:1814-1858`) returns the injected `self._reconcile_store` if set; with `db_free_required` (constant True) it returns `self.active_repository` when it has `query_reserved_unbound_jobs` / `query_inflight_jobs` / `bind_reservation` / `update_job_status`, else None; the rest (raise `scheduler_runtime:_restart_reconcile_store`, then `create_engine` + `PipelineStore(Session(engine))`, `_reconcile_store_build_error`) is unreachable. `_reset_reconcile_store_after_error` (`:1784-1811`) rolls back / disposes a SQLAlchemy session; in production `_reconcile_store` is never set (`from_env` passes no `reconcile_store`), so it is a no-op there; it only acts on stores tests inject through `ProductionScheduler(reconcile_store=...)` (20+ call sites in `tests/test_production_scheduler.py`, `tests/test_source_scoped_dispatch.py:160`).
- Delete (4.2):
  - The non-DB-free tail of `_restart_reconcile_store`: the raise, the `database_url` read, `create_engine` / `Session` / `sqlalchemy_url` / `PipelineStore` imports and construction, and `_reconcile_store_build_error` (attribute in `scheduler_core.__init__`, writes here, and the `reconcile_store_build_failed` evidence branch in `_run_restart_reconcile`). `_restart_reconcile_store` collapses to: injected store if set, else the active repository when it has the four methods, else None. The `reconcile_store_unavailable` skip stays.
  - `_reset_reconcile_store_after_error`, its `scheduler_core.py` forwarder, and its two call sites in `_run_restart_reconcile`. The `except` blocks keep recording `reserved_unbound_error` / `inflight_error` and `status: error`; recovery still never aborts the pass.
  - `RECONCILE_DB_STATEMENT_TIMEOUT_MS` (`scheduler.py:317`, `scheduler_runtime.py:30`) if no other reader remains, and the `scheduler_runtime.py:29` alias of `RECONCILE_DB_CONNECT_TIMEOUT_SECONDS` (its only reader is `:1849`, deleted here). The `scheduler.py:61,241` / `scheduler_lease.py:25` definition and export stay (the PostgreSQL lease, 4.3, reads it).
  - The `scheduler_runtime:_restart_reconcile_store` row of `docs/runbooks/scheduler-dbfree-typed-reasons.md` (`:884`) and of the R2b arm table; after this slice the remaining `SchedulerModeInvariantError` sites are the lease (4.3) and the `_default_orchestrator_for` no-repository refusal.
  - Kept: the `reconcile_store=` constructor injection (test seam used widely, and DB-free tests inject file journal repositories through it); the `reconcile_*` modules (Out of Scope, shared and duck-typed); `PipelineStore` itself (R5).
- Tests (4.2): delete `tests/test_gateway_reconcile_store_reset.py` and `tests/test_reconcile_sacct_parse.py::test_reset_reconcile_store_disposes_dead_engine` with any helper it alone used (`_FakeStore`, `_DeadSession`, `_RecordingBind`). Delete the arm-table row. Behaviour change confined to injected stores: today an injected store that raises is dropped after the error (rollback raises `AttributeError` on a store without `.session`, so the except path sets `_reconcile_store = None`) and the next pass falls back to `active_repository`; after the deletion the injected store is kept. The implementer lists every test that injects a store whose reconcile methods raise (e.g. `_FailingReconcileStore`, `_PoisonedCycleStore`, `_MutatingReconcileStore`) and shows each stays green unchanged; any test whose assertion depends on the drop is reported, not silently rewritten.
- Docs and spec: no new spec delta; R2c-1 (4.4) already wrote the end state for the reconcile half (`pipeline-job-persistence` "Status Synchronization via sacct", `file-orchestration-journal` "Accepted forecast cohort submission is reconciled exactly once"). The implementer re-reads both and reports any clause this deletion falsifies. `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` and `docs/bugs.md` are grepped for the deleted names and updated or marked historical.
- Must preserve: restart-reconcile evidence for every DB-free outcome byte-for-byte (`status`, `reserved_unbound`, `inflight`, the error keys, `reconcile_store_unavailable`); no PostgreSQL engine or `PipelineStore` is constructed anywhere in `scheduler_runtime.py`. Consumers of that block, all unchanged: `services/orchestrator/operator_action_listing_held.py:103` (`status == "skipped"` -> `restart_reconcile_skipped`; `tests/test_operator_action_listing_held_reservations.py:466`), `services/orchestrator/scheduler_no_progress.py:204` (`status == "completed"` + `reserved_unbound.outcomes`), `services/orchestrator/scheduler_evidence_proofs.py:424` (`restart_reconcile_proof` from outcomes), and the bounded compaction `services/orchestrator/scheduler_evidence_payload.py:111,609` (`_BOUNDED_RESTART_RECONCILE_KEYS` = `status`, `reserved_unbound_error`, `inflight_error`). Dropping the `reconcile_store_build_failed` / `error_type` branch changes none of them: no consumer, schema or doc reads that reason, and it is unreachable at HEAD.
- Risk packs selected: Legacy compatibility (no lost coverage), Runtime/config contract (restart-reconcile evidence unchanged), Error handling / rollback (the rollback/dispose helper leaves both `except` blocks; they still record `reserved_unbound_error` / `inflight_error` and `status: error`, and the pass is never aborted: evidence 2 and the existing failing-store tests). Not selected: concurrency (lease is 4.3), schema.
- Required evidence:
  1. `grep -n "create_engine\|PipelineStore\|sqlalchemy\|_reset_reconcile_store_after_error\|_reconcile_store_build_error\|RECONCILE_DB_STATEMENT_TIMEOUT_MS" services/orchestrator/scheduler_runtime.py services/orchestrator/scheduler_core.py services/orchestrator/scheduler.py` returns nothing; same names over `tests` and `docs/governance` list only intentional mentions.
  2. The injected-raising-store test list with results (point above).
  3. A mutation on the reachable path of `_restart_reconcile_store` (when `_reconcile_store is None`, constructing `PipelineStore` in place of returning the active repository) turns `tests/test_production_scheduler.py:45904` (the fake-Slurm DB-free submission test, `dry_run=False`, its kept `PipelineStore` patch) red. The `:41185` guard runs with `dry_run=True` and never reaches reconcile, so it is not evidence here.
  4. Full suite plain green (proxy env unset); ruff; `openspec validate --strict`; CI full suite.
  5. node-27 integration: not required. `tests/test_qhh_production_bootstrap_scheduler.py` constructs `ProductionScheduler` (whose `__init__` loses `_reconcile_store_build_error`) but runs with `dry_run=True`, so `_run_restart_reconcile` returns None before the store is reached; CI shards cover the attribute removal.
  6. node-22 (4.5): one idle pass and one submitting pass after the ff deploy at a pass boundary. Expected: the submitting pass's evidence has `restart_reconcile.status == "completed"` (not `skipped` / `error`) and no `reconcile_store_build_failed`. node-22 (resumed by the owner 2026-11-21 after the 2026-11-20 pause) steps through R2b's merge SHA (`b316e32e0`), then R2c-1's (`133d8a529`), each with its own passes, before this slice's merge SHA. If those passes are not done when the PR is ready, it merges with `Part of #3257` and 4.5 unticked; 4.5 is ticked in a later PR.

Slice triage (R2c-3, #3258, tasks 4.3, 4.5):
- Issue type: deletion (behaviour-preserving for every production path). Fixture level: expanded (agree): scheduler concurrency (the lease taken before every pass).
- Blast radius: `services/orchestrator/scheduler_core.py` (`_build_scheduler_lease`), `scheduler_lease.py`, the `scheduler.py` facade imports and compat tables, `scheduler_mode_invariant.py` docstring, `tests/test_production_scheduler.py` (advisory-key test, pin test), `tests/test_scheduler_dbfree_only_mode.py` (factory guard, arm table, contract test, module docstring), `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md:125`, `docs/runbooks/scheduler-dbfree-typed-reasons.md`.
- Governing invariant: every scheduler pass takes the file lease (`FileSchedulerLease`) and nothing else; lock evidence (`lock.lock_type == "file"`, contention / stale-takeover / heartbeat fields, `lock_contended` outcome) is byte-identical to 7e33b58a5; no code path can open a PostgreSQL connection for locking.
- Sibling surfaces: the file lease and its heartbeat (`scheduler_lease.py` `FileSchedulerLease`, `_LeaseHeartbeat`), the late-binding compat lookup `_scheduler_compat_function` and the facade tables `_SCHEDULER_LEASE_COMPAT_REEXPORT_NAMES` / `_SCHEDULER_LEASE_COMPAT_LOOKUP_NAMES` (`scheduler.py:235-256`) with their pin test (`tests/test_production_scheduler.py:998-1051`) and inventory row (`docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md:125`); the migration lease (`file_orchestration_migration.py:1260`, file only); the lock-backend selector (`NHMS_SCHEDULER_LOCK_BACKEND`), which the DB-free selector preflight already refuses for any non-`file` value before the lock (`tests/test_scheduler_dbfree_only_mode.py` `test_selector_not_file_blocks_before_the_lock_with_the_flag_unset`); the last remaining `SchedulerModeInvariantError` site (`scheduler_core.py` `_default_orchestrator_for`).
- Recon at 7e33b58a5: `_build_scheduler_lease` (`scheduler_core.py:204-222`) returns `FileSchedulerLease` when `db_free_required` (constant True), then raises `scheduler_core:_build_scheduler_lease`; the `PostgresSchedulerLease` branch and the trailing file fallback after the raise are unreachable. `PostgresSchedulerLease` (`scheduler_lease.py:515-607`), `_postgres_advisory_lock_key` (`:610-613`) and `RECONCILE_DB_CONNECT_TIMEOUT_SECONDS` (`:25`, read only at `:541`) have no other production caller; `scheduler.py:61,63,69,240,241,247,255` re-export them. Tests: `tests/test_production_scheduler.py:50215` (`test_postgres_advisory_lock_key_uses_scheduler_compat_monkeypatch`) and any other `PostgresSchedulerLease` test; `tests/test_scheduler_dbfree_only_mode.py:94` (`_forbid_db_factories` patch) and the arm table `:368-400`, whose only row is the lease.
- Delete (4.3):
  - `PostgresSchedulerLease`, `_postgres_advisory_lock_key`, `RECONCILE_DB_CONNECT_TIMEOUT_SECONDS` and the `psycopg2` import inside the class, with their `__all__` entries (`scheduler_lease.py:744-751`) and the `scheduler.py` imports, re-export and lookup entries; the pin test and the inventory row are updated to the reduced name sets.
  - In `_build_scheduler_lease`: the `database_url` read, the `db_free_required` gate, the raise and the unreachable tail; it returns the file lease unconditionally (same arguments).
  - Kept: `scheduler_lock_backend` / `NHMS_SCHEDULER_LOCK_BACKEND` (config field, env templates, evidence key; the non-DB-free `{"file","postgres"}` validation in `config.py:674` is a dead False arm, deleted in 4.6 (#3311)), `scheduler_mode_invariant.py` (the `_default_orchestrator_for` refusal still raises it), the compat lookup mechanism itself (renamed in F2).
- Tests (4.3): delete the `PostgresSchedulerLease` / `_postgres_advisory_lock_key` tests; drop the `PostgresSchedulerLease` patch from `_forbid_db_factories` (deleted name); delete the now-empty arm table and `test_non_db_free_arm_fails_closed_with_the_typed_error`, and any helper only it used (`_report_db_backed`, `_assert_mode_error` if orphaned); keep the `SchedulerModeInvariantError` contract test (`test_scheduler_mode_invariant_error_is_a_direct_exception_subclass`, `:332-339`) with its `site` string retargeted from `scheduler_core:_build_scheduler_lease` to `scheduler_core:_default_orchestrator_for` and `test_db_free_default_orchestrator_without_a_repository_fails_closed`. Add one test that `_build_scheduler_lease` returns a `FileSchedulerLease` even when the config carries a `database_url` and `scheduler_lock_backend="postgres"` (no PG import, no lock file before acquire), and that the deleted names are absent from `scheduler` / `scheduler_lease` (`not hasattr`). File-lock contention tests stay unchanged and green (acceptance criterion).
- Folded in from the R2c-2 review: the `tests/test_scheduler_dbfree_only_mode.py:1-17` module docstring is rewritten to the end state (no arm table left; the one remaining typed refusal is `_default_orchestrator_for`); the stale `SCHEDULER_COMPATIBILITY_INVENTORY.md:125` claim that `tests/test_gateway_reconcile_*.py` imports `RECONCILE_DB_CONNECT_TIMEOUT_SECONDS` goes with the row update.
- Docs and spec: `docs/runbooks/scheduler-dbfree-typed-reasons.md` drops the lease row (one site left); the `services/orchestrator/scheduler_mode_invariant.py:3-8` docstring is rewritten to the end state (`_default_orchestrator_for` is the only raise site); `docs/runbooks/node22-db-retirement-runbook.md:31,43` describe the retired PostgreSQL lock as history and stay (R3 owns that runbook). No new spec delta: `node22-dbfree-runtime` ("no PostgreSQL advisory lock is attempted") stays true; the implementer re-reads `openspec/specs/node22-dbfree-runtime/spec.md:40-80` and `production-scheduler-orchestration/spec.md:1560-1580` and reports any clause the deletion falsifies.
- Must preserve: lock evidence and lock-contention outcomes byte-for-byte; selector fail-closed before the lock for `NHMS_SCHEDULER_LOCK_BACKEND=postgres`; consumers of `lock.*` evidence unchanged (`scripts/node22_scheduler_stall_health.py`, `services/orchestrator/scheduler_evidence*.py`, `scheduler_runtime.py` lock_contended / rollback fence).
- Risk packs selected: Concurrency/locking (the pass lease; file-lock contention tests + new lease-builder test), Legacy compatibility (facade re-export tables and their pin test), Runtime/config contract (lock-backend selector still fails closed). Not selected: schema, error handling (no except path changes).
- Required evidence:
  1. `grep -rnw 'PostgresSchedulerLease\|_postgres_advisory_lock_key\|RECONCILE_DB_CONNECT_TIMEOUT_SECONDS\|pg_try_advisory_lock' services scripts` returns nothing; same over `tests` and `docs/governance` lists only intentional mentions (`not hasattr` assertions).
  2. File-lock tests green and unedited (`git diff master -- tests/test_production_scheduler.py` touches none of their bodies): `test_lock_contention_reports_without_candidates_or_submission` (`:20651`), `test_lock_contended_pass_evidence_on_disk_carries_no_candidate_lists_or_source_cycles`, `test_stale_unowned_lock_is_not_unlinked`, `test_stale_lock_symlink_is_not_unlinked`, `test_legacy_only_guard_alias_makes_the_scheduler_pass_lock_contended`, `test_distinct_lease_and_journal_guard_envs_have_independent_semantics`, `test_stale_scheduler_lock_takeover_does_not_delete_fresh_contender_lock`, `test_db_free_file_lock_contention_is_bounded_and_no_submit`, `test_db_free_prepared_rollback_fence_blocks_current_scheduler_after_lease_release`, `test_db_free_file_lock_contention_masks_raw_existing_lock_payload`, and the heartbeat set `test_renew_bumps_heartbeat_seq_and_preserves_identity`, `test_atomic_guard_heartbeat_and_synchronous_renew_do_not_self_contend`, `test_lease_heartbeat_advances_then_detects_takeover`, `test_lease_heartbeat_shipping_test_*` (2), `test_lease_heartbeat_production_run_maps_renew_exception_to_lost_without_escape`.
  3. The new lease-builder test red when `_build_scheduler_lease` is mutated to return a `PostgresSchedulerLease`-like object for `scheduler_lock_backend="postgres"`; revert byte-identical.
  4. Full suite plain green (proxy env unset); ruff; `openspec validate --strict`; CI full suite.
  4b. node-27 (D5: an integration-marked suite builds the changed code): `run_once` acquires the lease unconditionally (`scheduler_runtime.py:698`, before any `dry_run` gate), and `tests/test_qhh_production_bootstrap_scheduler.py` (integration) calls `run_once()`; so `-m integration tests/test_qhh_production_bootstrap_scheduler.py` runs on node-27 against a throwaway `nhms_it_*` DB. Expected: all pass; leftover `nhms_it_*` DBs = 0.
  5. node-22 (4.5): one idle pass and one submitting pass after the ff deploy at a pass boundary; expected `lock.lock_type == "file"` and the pass not `lock_contended`. node-22 steps through R2b `b316e32e0`, R2c-1 `133d8a529`, R2c-2 `7e33b58a5`, each with its own passes, before this slice's merge SHA; if not done when the PR is ready it merges with `Part of #3258` and 4.5 unticked.

Slice triage (R2c-4, #3311, task 4.6):
- Issue type: deletion. Behaviour is preserved for every real `ProductionSchedulerConfig`, because `db_free_required` is pinned True at `config.py:280-281`.
  - Fixture level: expanded (agree). Triggers: shared scheduler config and evidence writer; facade and inventories; the project's orchestrator trigger (`openspec/project-profile.md:107`).
  - Width: one PR. Three real dependencies keep it together: row 16 before 17, row 10 before the database cascade, and row 4 before `_optional_config_path`. The review surface is mostly mechanical: 36 kwarg removals plus about 11 test sites.
- Governing invariant: for every real config, every evidence payload, blocker code, preflight verdict and canonicalised path is byte-identical before and after.
  - Only the unreachable arms and the duck-typed-stub fallbacks go.
  - `runtime_config.scheduler_db_free_required` and `db_free_runtime.required` keep reporting `true` (`tests/test_production_scheduler.py:15273`, `:38235-38236`).
- Recon at 76f8f1319: all sites are in `services/orchestrator/` and are byte-identical to the issue's 55a964bbf, except the `scheduler_runtime.py` / `scheduler_core.py` callers. #3257/#3258/#3260/#3261 removed none of them.

  | # | Site at HEAD | Note |
  |---|---|---|
  | 1 | `scheduler_config/config.py:432` | always True |
  | 2 | `config.py:670` | dead tail `:673-676` |
  | 3 | `config.py:706` | redundant operand |
  | 4 | `path_modes.py:14, 54, 68, 160, 175, 196, 210` | all 7 still present |
  | 5 | `path_modes.py:101` | dead arm `:101-118`, comment `:99-144` |
  | 6 | `scheduler_config/db_free.py:129, :141` | dead else `:145-154`; no test asserts `db_free_allowed_root_unsafe` |
  | 7 | `scheduler_runtime_roots.py:518, :519, :534` | dead else `:540-551`; `:519` is used only in that arm |
  | 8 | `scheduler_runtime_roots.py:17-20, :80-83` | constant `evidence_safe_paths` |
  | 9 | `scheduler_runtime_roots.py:15-16, :78-79` | `_scheduler_root_preflight_not_required` at `:172-179` |
  | 10 | `scheduler_gateway.py:20, :22, :57, :65, :73, :85` | dead else `:29-38`; callers `scheduler_runtime.py:1142`, `scheduler_candidate_execution_evidence.py:342` |
  | 11 | `scheduler_preflight.py:534, :553` | dead else `:559-567` |
  | 12 | `scheduler_evidence.py:447` | |
  | 13 | `scheduler_evidence.py:494` | |
  | 14 | `scheduler_evidence.py:803` | only the flag conjunct is dead; the tail stays live for `config is None` (`scheduler_candidate_runtime.py:588-589`, `scheduler_evidence.py:623`) |
  | 15 | `scheduler_evidence.py:809-812` | |
  | 16 | `scheduler_evidence.py:907, :912-929, :962-984` | `:912` becomes the literal `True` |
  | 17 | `scheduler_evidence_payload.py:959-972` | dead per the producer proof below |

  - Other drift: `scheduler_core.py:171-172` and `cli.py:403`, `:451`.
- Row 17 producer proof (in scope; delete after row 16):
  1. Compaction is reached only through `bounded_evidence_payload` (`scheduler_evidence_payload.py:1482`) and the `_compact_required_bounded_field(s)` / `_compact_retained_bounded_field` paths (`:809-845`, `:1175-1181`). These run only on the evidence write path (DI at `scheduler_runtime.py:2146`).
     - No `json.load`, `read_text` or `loads` appears in either evidence module, and no script calls the bounded functions.
     - `operator_action_listing.py:756` reads a persisted `runtime_config` but never compacts it.
     - So old on-disk evidence cannot reach compaction.
  2. The only in-process producer is `scheduler_evidence.py:333-336` (the injected `_scheduler_runtime_config_evidence`, `scheduler_core.py:492`, or its default). It always emits `scheduler_db_free_required` (`:912`) and `db_free_runtime` when it is callable (`:956-958`). The retained-summary fallback `:1543` copies the same mapping.
  3. The non-DB-free key list is a strict subset of the DB-free list. So any mapping that has no flag, no `database_url_configured` / `scheduler_*_backend` key and no `db_free_runtime` mapping compacts to the same dict either way. That covers the fixtures at `tests/test_production_scheduler.py:21414`, `:23496`, `:37737` and `tests/test_operator_action_listing.py:218`, `:1361`.
  - The PR body restates this proof.
- Change list:
  - Rows 1, 3:
    - Collapse to `True` / `bool(self.slurm_execution_enabled)`. The `require_runtime_roots` field and keyword stay accepted.
    - Sibling, in scope: delete `cli.py:403`, the `:451` kwarg, and the `scheduler_core.py:171-172` conjunct. `git grep` finds no test asserting `require_runtime_roots` False.
  - Row 2: the loop body becomes unconditional `object.__setattr__(self, attr, value)`. `_DB_FREE_SELECTOR_SPECS` keeps its tuple shape (unpacked at `:689`, `:710`, `:752`, re-exported).
  - Rows 4, 5: drop the `db_free_required` parameter from every `path_modes.py` helper. Drop the kwarg from the 36 `config.py` call lines and `db_free.py:135, :180, :267`.
    - `_expanduser_for_mode` keeps `except RuntimeError: return path`.
    - The two require helpers swallow the error.
    - `_confined_path_for_mode` keeps only its try/except body.
    - `_resolve_config_path_for_mode` becomes one arm and keeps the `# ADR 0009 clause 1` marker in its body, which the family guard requires.
    - Rewrite the comment: delete the "TEXTUALLY IDENTICAL ... retained deliberately" paragraph and its line ref; replace them with a one-paragraph clause-1 note that names the ADR 0009 amendment section.
    - New comments must not add `file.py:NNN` references.
    - The re-exports in `scheduler_config/__init__.py:151-190` are unchanged.
  - Row 6: delete `db_free.py:129` and the else; `elif` becomes `else`. Keep the clause-1 marker and fix the docstring.
  - Row 7: delete `:518`, `:519` and the else. Keep the clause-3 marker and fix the docstring.
  - Row 8: `evidence_safe_paths=True` at both call-site groups. The parameter stays on `_scheduler_root_check` and its siblings (see Out of scope).
  - Row 9: delete `roots.py:15-16`, `:78-79` and `_scheduler_root_preflight_not_required`, plus its forwarders `scheduler_candidate_runtime.py:499`, `:538`, `:868`.
  - Row 10: the DB-free check body only.
    - `evidence_safe_*` become the literal `True`, and `allowed_roots` is redacted.
    - Fix the comment at `:46-48` ("a DATABASE_URL_* blocker may precede it").
    - `database_url_forbidden` still blocks.
  - Row 11: delete `:534`; `elif` becomes `else`. Keep the clause-3 marker.
  - Rows 12, 13, 15: collapse to the DB-free arm:
    - row 12 to `{"error_type": type(error).__name__}`;
    - row 13 to `"[local-path]"`;
    - row 15 to `evidence_safe_paths = True`.

    The `config` parameters of `artifact_path_evidence` / `evidence_reservation_blocked_payload` stay (deviation).
  - Row 14: `if config is not None:`. The tail stays.
  - Row 16: `scheduler_runtime_config_evidence` writes `True`. `_scheduler_backend_evidence` drops its parameter, and its DB-free arm becomes unconditional.
  - Row 17: delete the non-DB-free arm `:959-972`, leaving the DB-free compaction as the only return.
  - Orphan `_scheduler_lock_evidence_root_preflight` (`roots.py:14-76`): delete it, its forwarders `scheduler_candidate_runtime.py:497`, `:536`, `:858`, and the inventory text.
  - Cascades: delete each symbol whose last caller goes.
    - From the database family:
      - `_database_url_blocker` (`scheduler_preflight.py:366`, last caller `scheduler_gateway.py:31`);
      - `_database_host` (`:391`);
      - `_database_host_is_local` (`:408`);
      - `_database_host_is_unsafe` (`:422`);
      - `_normalize_database_host` (`:439`);
      - `_is_unsafe_numeric_ipv4_like_host` (`:510`);
      - `LOCALHOST_NAMES` (`:40`);
      - `DATABASE_HOST_ALLOWED_RE` (`:50`).

      Their forwarders and name lists go too: `scheduler_candidate_runtime.py:227-231`, `:238`, `:772`, `:774-776` (`:773` `_database_host_ip_address` stays), `:807`, `:815`. `tests/test_production_scheduler.py:1076-1077` `_PREFLIGHT_OWNED_CONSTANT_NAMES` drops the two constants.
    - `_optional_config_path` (`roots.py:674`, last caller `path_modes.py:161`), plus forwarders `:512`, `:551`, `:819`.
    - Inventory text: `SCHEDULER_COMPATIBILITY_INVENTORY.md:127`, `:136`, `:166`, `:287-291` (`:292`, the `_database_host_ip_address` guard-hook row, stays), `:298`.
    - Kept, with reasons: `_database_host_ip_address` (`:446`) and its numeric-IPv4 helpers, live through `_gateway_self_reference_blocker` (`:140`); `_config_path_preserve_final_component`, `_config_path_relative_to_preserve_final`, `_optional_config_path_relative_to_preserve_final`, which are test-pinned (`test_production_scheduler.py:18441-18474`, `:18615`, `:18639`) and exercise `_canonical_parent`; the shim at `scheduler_candidate_runtime.py:578`, whose removal needs a DI signature change across `scheduler_evidence.py` hooks.
  - Carry-overs from PR #3324 (round-3 review, documentation):
    - (i) The T2-5 rule table `id-fstring` row: restore the constant-part qualifier "a constant part that fully matches `_retry_\d*` (including `_retry_` followed by a `FormattedValue`)". This is applied in this fixture.
    - (ii) `tests/test_retry_accounting_ast_guard.py` docstring `:7-9`: drop "an entry would need a written reason and a deliberate edit here". The allowlist is empty, and a miss is closed by tightening the guard.
    - (iii) Align "tightening the guard or by adding a row" (docstring `:121-126`, the T2-5 block) with the spec wording: a miss is recorded in the guard and closed by tightening it.
- Orphan retarget (`_scheduler_lock_evidence_root_preflight` to `_scheduler_runtime_root_preflight`):
  - For `workspace_root`, `lock_root` and `evidence_root`, `_scheduler_root_check` gets identical arguments (`roots.py:14-76` vs `:98-171`).
  - The runtime variant also checks further roots and `service_role`, so a retargeted test asserts the projection of `blockers` / `checks` onto those three fields plus the `allowed_roots` blocker field, `checks["allowed_roots_policy"]` and the top-level `allowed_roots`, in relative order.
  - Before deleting, an equivalence oracle runs both functions on the same configs and compares the projection. It is logged.
- Tests. Labels follow `tasks.md:76-82`: (a) retarget, (b) delete, (c) assertion change.
  - Retargets and deletions:
    - (a) Lock/evidence call sites `tests/test_production_scheduler.py:18546`, `:18664`, `:18887`, `:20829`, `:20834`, `:21203`, `:21225` and `tests/test_scheduler_root_check_loop_convergence.py:381` move to the runtime preflight projection. Expected values are unchanged.
    - (b) `:45886`: drop the lock lines; `runtime_preflight` already asserts.
    - (b) `:47131`: drop the `lock_preflight` loop element.
    - (b) `:46914-46917` and the `lock_evidence` param at `:47070`. `test_runtime_root_preflight_arms_adjudicate_allowed_roots_exactly_once` keeps the `runtime` param.
    - (a) Stub attributes no seam reads any more: `_allowed_roots_config` (`:46398-46412`); `_AllowedRootsConfigStub.scheduler_db_free_required` / `db_free_required` (`:46855-46860`) and their kwarg callers; `_PreflightRootsConfigStub.db_free_required` (`:19445-19455`, `:19486-19490`) and `verdict(..., db_free=)` (`:19943-19950`).
    - Row 5 test `test_db_free_and_db_backed_config_resolve_arms_agree_on_one_canonical_form` (`:18265-18268`): (b) drop `db_backed_arm` and the arm-parity assert; (a) keep `== loop` over the same shapes, rename the test without "db_backed", and fix the docstring.
    - Drop `db_free_required=` from the path_modes kwarg sites `:18178`, `:18231`, `:18266`, `:18617-18619` and `tests/test_scheduler_root_check_loop_convergence.py:201`.
      - The `evidence_safe_paths=False` ("db_backed") rows there call `_config_path_preserve_final_component_for_mode`. Re-observe their values after the change.
      - Any value that moves is (c), listed as old → new and never weakened.
    - Host-classifier tests `:25573-25612`: (b) delete both. Their subjects `_database_host_is_local` / `_database_host_is_unsafe` live only behind the deleted `scheduler_gateway.py:29-38` arm.
      - No gateway test covers numeric IPv4 hosts; the only ones are `localhost` URLs at `:47598-47729` and `tests/test_cli_publish_qdown.py:286`.
      - So add one parametrized `_gateway_self_reference_blocker` test (real backend, port == service port). Expected values were observed with `uv run --no-sync`:
        - `127.1`, `2130706433` and `0` → `SLURM_GATEWAY_SELF_REFERENCE`;
        - `0177.0.0.1` (parses to None) and `0xa9fe0101` (169.254.1.1) → no blocker.
  - Unchanged (the assertions stay true and none is weakened):
    - The `not in` assertions on False-arm-only codes (`:46395` users, `:46846` users, `:46310`).
    - `SCHEDULER_ROOT_ALLOWED_ROOTS_MISSING` at `:35572`, `:35623`.
    - `:38198` and `:18581-18601`: the keywords stay accepted.
    - The fixtures at `:21414`, `:23496`, `:37737`.
  - Stale test comments that name a deleted arm are updated.
  - Stale service comments are updated:
    - `scheduler_runtime_roots.py:578` names `_optional_config_path`;
    - `scheduler_runtime_roots.py:640-644` says `db_free_required=True` and "two database arms";
    - the `scheduler_preflight.py:520-528` docstring (row 11) says "unsafe on database-backed runtimes";
    - `path_modes.py:119-122` says "aligned with the arm above".
  - The `not in` assertions on the deleted False-arm codes become vacuous. They stay, and the PR body says so.
  - Rule: every (b) names the DB-free test that covers it, or records "unreachable". Every (c) lists old → new.
- Out of scope (reported):
  - Second order: once rows 8, 10 and 15 are done, every production caller passes a constant `True` for `evidence_safe_paths` / `evidence_safe_values`. The consumers are `_scheduler_root_check`, `_scheduler_root_preflight_payload`, `_scheduler_allowed_roots_policy_check`, `_storage_root_check`, `_slurm_env_check` and `_slurm_grib_env_check`. Their False arms are then test-only (`tests/test_scheduler_root_check_loop_convergence.py:185-205`). This becomes a follow-up issue; the parameters stay.
  - The orphan `_optional_config_path_relative_to_preserve_final`: follow-up.
  - `SimpleNamespace(db_free_required=True)` at `tests/test_production_scheduler.py:24511`.
  - `_MetStoreCanonicalReadinessProvider`, the qhh bootstrap tests, and the job-env contract (`chain_manifests.py:242`, `real_backend.py:1048`, `:2371`).
- Spec / ADR / doc deltas:
  - Spec deltas. `design.md:151` and `:236` require a delta for every main-spec passage that names what a deleting slice deletes. Appendix D omits these passages, so this slice adds them plus an Appendix D row. The orchestrator writes each as a MODIFIED block that keeps every scenario and states the DB-free end state, from the implementer's observation (Required evidence 7), not from inference. They go in this PR's final push.
    - `slurm-array-runner-integration`, requirement "Slurm-first heavy execution", scenario "compute-node database preflight" (main `:10-14`). New text: the Slurm preflight does not consult `DATABASE_URL`. A configured one is refused before the lock by the DB-free runtime preflight (`database_url_forbidden`).
    - Same spec, requirement "Array-capable model stages":
      - The "unresolvable allowed storage root" passages (`:44-54`; rows 6, 7, 9, 11 and the orphan).
      - "unresolvable general storage root at configuration construction" (`:56-62`; row 5).
      - New text for both: one runtime; lexical admission without a blocker on every plane; ENOENT admitted; no not-required payload.
    - Same spec, the requirements at `:169-221` (scenario `:178-182`) and `:223-253`. The row-4 re-raise at `path_modes.py:196`/`:210` is their last implementation. New text: construction hands the value down, and the root preflight refuses it (codes pinned at `tests/test_production_scheduler.py:18546`, `:18887`).
    - `runtime-evidence-and-operations/spec.md:580`: "lock/evidence root preflight" becomes "runtime root preflight".
    - `node22-dbfree-runtime/spec.md:83` stays true.
    - The implementer greps `openspec/specs` for `database-backed|db-backed|lock/evidence|not-required|DATABASE_URL` and reports any other clause the deletion falsifies. The orchestrator then adds a delta for it.
    - Outcome (written from `.workplans/3311/observed-behaviour.md`, rows 1-25; before and after byte-identical). The four requirements above are MODIFIED in `specs/slurm-array-runner-integration/spec.md`. `specs/runtime-evidence-and-operations/spec.md` is new and MODIFIES `:180-232` (tilde expansion; "the db-backed arm fails earlier"), `:233-365` (construction "aligned with the database-backed arm") and `:573-594` (`:580`).
      - The R2b ADDED tilde requirement's sentence "Every other root field aborts earlier in the deliberate re-raise" is corrected: that re-raise was the database-backed arm's and is deleted here.
      - The implementer's other hits F1-F4 (main `:72-132`) are already covered by R2b's MODIFIED delta, and F8 (`:308-312`) stays true.
      - F9: the `:52` clause "the db-free selector's allowed-roots lane records its existing unresolvable-root rejection" had no implementation before this PR (the `retry.py` lane was deleted in #3157, f9dd24aad). The MODIFIED text drops it and states the observed admission without a blocker, under ADR 0009 clause 3.
      - Reported, not in scope: `multibasin-state-idempotency/spec.md:10` ("database-backed or ..." candidate-state persistence).
  - The tasks.md 4.6 line and this block. `:233` changes to "deleted in 4.6 (#3311)".
  - `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md`: the lines above.
  - ADR 0009: one header line plus a trailing "修订（2026-11-22，#3311）" section. The orchestrator writes it; the precedents are `0002:5-41`, `0005:252`, `0007:47`, `0008:62`. It supersedes the "two arms" wording and the member count, which goes from 16 to 15 (`_optional_config_path` is deleted). The table stays as a historical snapshot.
  - `line_reference_baseline.json` is keyed by (path, reference text), so a deletion never fails.
    - Prune the stale `path_modes.py` entry (`scheduler_runtime_roots.py:576-590`) with `--write-baseline`, or leave it.
    - Do not add new `file.py:NNN` references.
  - `scheduler.py:311`: a governed-facade comment naming the two deleted constants. Leave it, or shrink it comment-only and re-run the facade audit.
- Must preserve:
  - Real-config evidence stays byte-identical: `[local-path]`, `error_type`, `scheduler_db_free_required: true`, `db_free_runtime.required`, and the compaction output.
  - The `scheduler_db_free_required=` and `require_runtime_roots=` keywords stay accepted.
  - The row-14 `config is None` tail.
  - `database_url_forbidden`.
  - `_scheduler_runtime_root_preflight`.
  - The `evidence_safe_paths` parameter on `_scheduler_root_check`.
  - The ADR 0009 markers in `_db_free_allowed_roots_and_blockers`, `_scheduler_allowed_roots_and_blockers`, `_preflight_allowed_roots` and `_resolve_config_path_for_mode`.
  - The `_database_host_ip_address` family.
  - The job-env contract.
- Risk packs:
  - Selected:
    - Legacy compatibility: no coverage lost; each (b) names its covering test.
    - Runtime/config contract: modes, selectors, preflight codes.
    - Evidence/redaction: rows 10, 12-16.
  - Not selected: schema, concurrency, cross-plane.
- Required evidence:
  1. A per-row table. For each arm deleted, give the DB-free test that covers it, or "no remaining test reaches it".
  2. One mutation per touched module, each restored and hash-checked: reinstate or force the False arm, then show a DB-free test red or the arm unreachable.
     - `path_modes.py`, `config.py` (row 1 `or False`), `db_free.py`, `scheduler_runtime_roots.py`, `scheduler_preflight.py` and `scheduler_gateway.py`.
     - `scheduler_evidence.py`: expect `test_production_scheduler.py:15273` (`payload["runtime_config"]["scheduler_db_free_required"] is True`) and the evidence-write tests red.
     - `scheduler_evidence_payload.py` (row 17).
  3. The row-17 producer proof, restated in the PR body.
  4. Acceptance greps:
     - `git grep -nE 'getattr\((context\.)?config, "(scheduler_)?db_free_required", False\)' -- services` returns nothing.
     - `git grep -n 'if not db_free_required' -- services/orchestrator/scheduler_config/path_modes.py` returns nothing.
     - `git grep -nw` for `_scheduler_lock_evidence_root_preflight`, `_scheduler_root_preflight_not_required`, `_database_url_blocker` and `_optional_config_path` over `services docs/governance tests` returns nothing, except the `_optional_config_path` mention in the `tests/test_production_scheduler.py:18160` comment.
     - `git grep -nw db_free_required -- services` leaves only `scheduler_config/config.py:280-281` (the local pin), the `db_free_required` property, and the `scheduler_mode_invariant.py:3` docstring.
  5. The lock/evidence retarget equivalence oracle.
  6. `uv run python scripts/governance/audit_repo_entropy.py --format json --structural-base-ref master` shows no `compatibility-facade-growth` for `scheduler.py` / `chain.py`, and no new line-reference finding.
  7. Observed DB-free behaviour for each spec passage that needs a delta, logged before and after the change; the two must be identical. For each passage below, build a real `ProductionSchedulerConfig` from its scenario inputs (`tmp_path`, `slurm_execution_enabled=True` wherever the passage says "Slurm execution is enabled"). Record whether construction raises, the constructed field value, and per plane (`_slurm_preflight`, `_scheduler_runtime_root_preflight`, `db_free_runtime_preflight`) the blocker codes/fields and the allowed-roots evidence.
     - `slurm-array-runner-integration` main `:10-14`: `DATABASE_URL` missing, localhost, and remote. Also record `run_once()` status, `execution_boundary` and `lock` reason, which back "refused before the lock".
     - `:44-54`: an allowed root through a regular-file ancestor, an untraversable ancestor (non-root user), a symlink loop, and ENOENT, each with `repair_missing_forcing` false and true (`:52`). Record the storage preflight, the runtime-root preflight and the db-free selector lane.
     - `:56-62`: a final-segment loop on the object-store / published-artifact / log / runtime / temp roots.
     - `:178-182`: an in-workspace evidence-dir final-segment loop.
     - `:223-253`: a workspace-root loop, a regular-file workspace root, an untraversable workspace root, and a lock-root loop (`:243-247`).
     - `runtime-evidence-and-operations/spec.md:580`: a self-loop lock root through `_scheduler_runtime_root_preflight` (code and `checks.lock_root` key set).

     Write the results as a table in `.workplans/3311/observed-behaviour.md` (gitignored) and restate the table in the PR body; each spec delta cites its row.
- Verification (owner policy 2026-11-22: no local full suite).
  - Local, the touched-module tests:
    - `tests/test_production_scheduler.py`
    - `tests/test_scheduler_root_check_loop_convergence.py`
    - `tests/test_scheduler_dbfree_only_mode.py`
    - `tests/test_scheduler_evidence_summary_lanes.py`
    - `tests/test_operator_action_listing.py`
    - `tests/test_preserve_final_component_loop_spelling.py`
    - `tests/test_orchestrator_first_import.py`
    - `tests/test_retry_accounting_table.py`
  - Local, the source-scanning guards:
    - `tests/test_path_canonicalization_family_guard.py` (critical)
    - `tests/test_status_literal_scan.py`
    - `tests/test_status_vocab_parity.py`
    - `tests/test_entropy_audit_line_references.py`
    - `tests/test_import_layering_guard.py`
    - `tests/test_entropy_audit_facade_guard_aliases.py`
    - `tests/test_entropy_audit_facade_guard_forwarders.py`
    - `tests/test_entropy_audit_instruction_inventory.py`
    - `tests/test_retry_accounting_ast_guard.py`
    - `tests/test_file_orchestration_journal_facade.py`
    - `tests/test_ci_shard_tests.py`
  - Local, the rest: `uv run ruff check . --no-cache` and `openspec validate redesign-orchestration-core --strict --no-interactive`.
  - CI: the full suite, every shard. This is the completeness gate.
  - node-27: a fresh-process import check of all 57 `integration`-marked files at HEAD found two that import the touched modules: `tests/test_qhh_production_bootstrap_scheduler.py` and `tests/test_qhh_output_stream_type_integration.py`. Run `-m integration` over them on throwaway `nhms_it_*` DBs and expect 0 leftover DBs.
  - node-22: this is a deleting PR, not a pure move, so it gets its own idle pass and submitting pass after the ff deploy (`design.md:226-227`). Compare the idle-pass evidence fields `runtime_config`, `artifact_path` and `[local-path]`.
    - Following the R2c-2/R2c-3 precedent, if node-22 cannot run the passes before merge, the PR merges as `Part of #3311` with 4.6 unticked, and 4.6 is ticked once the passes are recorded.
    - The PR body records that the CI full suite replaces the issue's acceptance criterion "`uv run pytest -q` full suite green" (owner decision 2026-11-22).
- Review focus:
  1. Every collapse keeps the DB-free half verbatim, including evidence strings and blocker order.
  2. The ADR 0009 clause markers survive in the four authority members, and the guard message shows 15 members (the guard prints the count; it does not assert it).
  3. The lock/evidence retarget projection equals the old output.
  4. `evidence_safe_paths` is passed as the literal `True` wherever the flag used to be.
  5. No test assertion is weakened. The row-5 change deletes only the arm-parity line.
  6. Row 17 goes only with its proof, and after row 16.
  7. Kept orphans are recorded with their reasons.

## 5. R3 — Delete `PsycopgOrchestratorRepository`

- [x] 5.1 Test-only PR first. For each `CapturingRepository(PsycopgOrchestratorRepository)` test and the candidate-state tests (`tests/test_orchestration_chain.py:8283-10049`), record whether it asserts SQL text (to delete in 5.2) or candidate-state semantics. For each semantics case, confirm `chain_repository_state.py` covers it on the file-journal path (`file_orchestration_journal.py:2025`), and migrate it if not.
- [x] 5.2 Delete the Appendix C R3 symbols and their SQL-text tests, plus `tests/test_real_database_integration.py:339-502` (each test confirmed SQL-only on node-27). Keep the `OrchestratorRepository` protocol. Update the inventory text and tests.
- [x] 5.3 Add the Appendix D R3 spec deltas, including the MODIFIED `file-orchestration-journal` contract-test requirement already in this change. Update `docs/runbooks/node22-db-retirement-runbook.md:51`, which names the deleted class.
- [x] 5.4 Verification: node-27 real-DB suites that remain pass. On node-22, one idle pass and one submitting pass.
- [x] 5.5 Delete the SQL `candidate_state` body and the lineage max-lead-hours helper left orphaned by R3-2, and rewrite stale R3 prose, line references and DB-lane spec wording (#3315, R3 follow-up). Verification: CI, plus the issue's acceptance greps. Done in PR #3327 (review round 1 clean; CI green).

Suggested fixture level: expanded - persistence layer deletion.
Minimal mergeable slice: 5.1 as a test-only PR; then 5.2 + 5.3 + 5.4 as the deleting PR.

Slice triage (R3-1, #3259, task 5.1):
- Issue type: test. Fixture level: compact (agree): test-only, no production code. Blast radius: the in-scope test files and any file a migrated test is added to; a semantics case left only on the SQL repository would be lost when R3-2 deletes it.
- Scope at b316e32e0 (re-grep `git grep -n PsycopgOrchestratorRepository -- tests`): every test that subclasses, instantiates or patches `PsycopgOrchestratorRepository`.
  - Semantics carriers: `tests/test_orchestration_chain.py` (`CapturingRepository` at :4869, :8127, :8169, :8213, :8249, :8371, :8439, :8512, :8588, :8661, :9926, :10015, :10668; `_SourceCycleRetrySupersessionRepository` :8750 and its users; the instance at :11960; the candidate-state band :8246-10078; the facade/inventory asserts :923, :2348-2362, :12788-12943; the integration oracle :20280) and `tests/test_chain_repository_nfs_raw_manifest.py:48, :77` (NFS raw-manifest synthesis and the required-gap projection; R3-2 deletes the whole file).
  - Also tabled with a one-line disposition each: `tests/test_real_database_integration.py:339-502` (3 tests), `tests/test_gateway_reconcile_reservation_lifecycle.py:280`, `tests/test_orchestrator_demote_reclaim_lifecycle.py:596-604`, `tests/test_production_scheduler.py:41221, 41225, 43952-43956, 45951, 45955`.
- Per test, exactly one disposition, recorded in the PR body table (a test that mixes kinds gets one row per half):
  - **delete (R3-2)**: it asserts SQL text, SQL parameters, the facade/export binding of the SQL class, or other behaviour that exists only in the SQL repository. Not deleted here. A parameter or SQL-text assertion that encodes a behaviour rule (a status set, a visibility predicate, e.g. `pending` counts as active at :8166) is split; its rule half needs covered-by or migrated-to. Every delete row cites the asserted line.
  - **delete (journal-divergent)**: the journal's pre-projection step (`file_orchestration_journal.py:1937-2068`: foreign-model exclusion, cohort membership, compaction) gives a different answer on the same geometry by design. The row cites the journal rule's line and names the journal test that pins the journal's answer. Never covered-by.
  - **covered-by `<test>`**: it asserts the semantics of an `OrchestratorRepository` method the file journal also implements (`candidate_state`: attempt/stage accounting, truncation, freshness window, supersession, retry/backoff projection, identity fields, ordering, NFS raw-manifest readiness; `has_active_pipeline` / `active_slurm_jobs`: the active status set and row visibility; `find_forcing_context`: package/station metadata), and a named test already drives the same method on `FileOrchestrationJournalRepository` through `candidate_state_from_rows` (`chain_repository_state.py:650`, called at `file_orchestration_journal.py:2025`) where applicable, on the same input geometry, asserting the same property. The row cites the covering test's seed lines and its assert line. The covering test must not construct, subclass or patch `PsycopgOrchestratorRepository`, must not be in the R3-2 delete set, and must not be `integration`-marked.
  - **migrated-to `<test>`**: a semantics case with no such file-journal test. The new test seeds the same rows through the file journal (e.g. `append_historical_pipeline_job`, the journal write API) and asserts the same outputs on the same method. Each migrated test is mutation-checked against the shared projection or the file-journal path and fails under the mutation. It must not be `integration`-marked.
  - **unaffected**: the test does not construct, subclass or patch the SQL class (e.g. :9306, :9457, :9570, :9621); it survives R3-2 unchanged. Listed only so the band is accounted for.
- Must preserve: no production code change; no existing test is edited or deleted in this PR (deletions belong to R3-2), so the plain suite only grows.
- Risk packs selected: Legacy compatibility (no lost semantics coverage → the table, the cited covering tests, mutation kills for migrations). Not selected: every other pack (test-only).
- Required evidence:
  1. The table: every in-scope test → delete | delete (journal-divergent) | covered-by | migrated-to | unaffected, with the line of the asserted property and, for covered-by, the covering test's seed and assert lines.
  2. For each migrated test: green, and red under its mutation (the mutation reverted).
  3. The new tests green; ruff; CI full suite.
  4. `git diff --numstat <base>..HEAD -- tests` shows 0 deleted lines in every pre-existing test file, and the collected node count for each touched test file goes up or stays the same.

Slice triage (R3-2, #3260, tasks 5.2, 5.3, 5.4):
- Issue type: deletion (behaviour-preserving for every production path). Fixture level: expanded (agree): persistence-layer deletion plus the chain facade's compat bindings.
- Governing invariant: the file journal (`FileOrchestrationJournalRepository`) is the only orchestration repository; no production or test-support code can construct a PostgreSQL orchestration repository; every DB-free outcome and every surviving test is unchanged.
- Blast radius: `services/orchestrator/chain_repository.py` (deleted), `chain_forecast_orchestrator_cycle.py` (`ForecastOrchestrator.from_env`), `scheduler_adapters.py` + `scheduler.py` (the two factories and their re-exports), `services/orchestrator/__init__.py` exports, `chain_compat_runtime.py` / `chain_compat_static.py` bindings and their pins, `file_orchestration_journal.py:76-80` (import retarget), the tests below, `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md`, `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md:129,158,204-205`, `docs/governance/STRUCTURAL_FILE_DISPOSITION_INVENTORY.md:77,161-162`, `docs/runbooks/node22-db-retirement-runbook.md:51-54`, spec deltas.
- Sibling surfaces: the `OrchestratorRepository` protocol (kept); `_retry_service_from_env` / `get_retry_service` / SQL `RetryService` (R4, kept: `ForecastOrchestrator.from_env` was the last production caller of `_retry_service_from_env`, which stays callable and patched until R4); `PsycopgStateSnapshotRepository` and the `get_earliest_clone_row_for_model_source` lineage arm (`scheduler_lineage.py:300-311`; not an R3 symbol, kept; `tests/test_scheduler_backfill.py:2417` is unaffected — the #3260 handoff note is superseded by this line); the provenance importer and the rollback tool's historical export (Out of Scope).
- Recon at e4e4b8c85: no production caller of `PsycopgOrchestratorRepository`, `_active_repository_from_env`, `_orchestrator_repository_from_env` or `ForecastOrchestrator.from_env` remains (the scheduler builds `ForecastOrchestrator(...)` directly, `scheduler_core.py:475`); none in `apps/`, `workers/`, `scripts/`, `packages/`, `infra/`. `chain_repository.py` also defines `ACTIVE_HYDRO_STATUSES` / `COMPLETED_HYDRO_STATUSES` (aliases of `scheduler_state_types`), `DEFAULT_CANDIDATE_STATE_EVENT_LIMIT` / `DEFAULT_CANDIDATE_STATE_JOB_LIMIT` (`:29-30`, imported by `file_orchestration_journal.py:76-80`) and six private aliases used only by the class.
- Delete (5.2):
  - `services/orchestrator/chain_repository.py` entirely. No new definitions: `scheduler_state_types.py:12-13` already defines both limits (100) and `:50` `DURABLE_HYDRO_SUCCESS_STATUSES`; `file_orchestration_journal.py:76-80` imports the limits from there and binds `DURABLE_HYDRO_SUCCESS_STATUSES as COMPLETED_HYDRO_STATUSES`, keeping the journal's own name (in its `__all__`, `:626`).
  - `ForecastOrchestrator.from_env`; `_active_repository_from_env` / `_orchestrator_repository_from_env` (`scheduler_adapters.py:398-407`) and their `scheduler.py:51,54` re-exports; the class in `services/orchestrator/__init__.py:25,36`; its bindings, drift checks, legacy import tokens and `IMPLEMENTATION_*` export names in `chain_compat_runtime.py:108-111,163,178,478,488-489` and `chain_compat_static.py:240-318`, with the matching inventory rows/text in `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md`, `SCHEDULER_COMPATIBILITY_INVENTORY.md:129,158,204-205`, `STRUCTURAL_FILE_DISPOSITION_INVENTORY.md:35` (the `wc -l` replay command names the deleted file) and `:161-162`, and the pins (`tests/test_orchestration_chain.py:12731` `test_chain_persistence_repository_compat_owner_aliases_match_owner_module_and_inventory` with its `expected_inventory_tokens` `:12746-12756`, `:12795-12953`, `tests/test_entropy_audit_instruction_inventory.py:160`) updated to the reduced sets.
  - Tests: every R3-1 table row with disposition delete / delete (journal-divergent) (`.workplans/3259/table.md`; PR #3307 body), including the 13 `CapturingRepository` subclasses' SQL-text halves, `tests/test_chain_repository_nfs_raw_manifest.py` (whole file; rows 41-42 migrated), the three `tests/test_real_database_integration.py` class tests (`:325`, `:377`, `:431`; each ends in a class assertion, so SQL-only), `tests/test_orchestration_chain.py:20301` integration half (row 40A), rows 43A, 44, 45-47. covered-by / migrated-to / unaffected rows are not touched.
  - Monkeypatches of the two deleted factory names outside the table (`tests/test_production_scheduler.py:26140, 26407, 26476, 35544, 35598, 35666, 35855, 36108, 36176, 36230, 36283, 39547, 41161, 41165, 45889`; `tests/test_file_orchestration_journal.py:6012-6013, 6062-6063, 6105, 6160`; `tests/test_scheduler_dbfree_only_mode.py:89-91`) are removed name by name; each guard keeps every other DB-factory patch (incl. `_retry_service_from_env`, R4) and stays green. A guard left with no patch and no assertion of its own is reported, not silently deleted.
  - Path strings and comments naming `chain_repository.py`: `tests/test_hydro_status_set_parity.py`: the `chain_repository_module` import (`:27`) and its assertions (`:145`, `:200`, `:263`) are deleted (retargeting them would assert `x is x`); the existing journal-alias assertion (`:150`, `assert journal_module.COMPLETED_HYDRO_STATUSES is durable`) stays; comments `:141,147,237` updated; `tests/test_file_journal_full_tree_budget_contract.py:372`, `tests/test_node27_connection_attribution_delegated.py:144,147,229`, `tests/test_node27_connection_attribution.py:909`; comment / docstring mentions in `scheduler_state_types.py:31,36`, `file_orchestration_journal.py:1486,1522,1966,1978,1980`, `tests/test_file_orchestration_journal.py:1676,1706,1944,1981,2058`, `tests/test_file_journal_repository_semantics.py:3`, `tests/test_production_scheduler.py:889,908,36368`, `tests/test_scheduler_dbfree_only_mode.py:427`; `scripts/governance/entropy_audit/line_reference_baseline.json:122-124` (re-generate or drop the stale keys the way the tool expects); `scripts/node27_parse_failure_residency_alert.py:21` docstring; `docs/spec/03_database_design.md:305`, `docs/runbooks/scheduler-dbfree-typed-reasons.md:895`, `docs/runbooks/production-ops/parse-failure-residency-alert.md:41`, `docs/governance/STRUCTURAL_FILE_DISPOSITION_INVENTORY.md:77` (update or mark historical).
- Spec (5.3): Appendix D R3 deltas, keeping every surviving scenario name:
  - `file-orchestration-journal`: MODIFIED for the requirements holding `:11-13` (responsibilities "currently supplied by `PsycopgOrchestratorRepository`"), `:46-48` ("preserve the existing DB-backed repository semantics") and `:153-155` (ordering "same as `PsycopgOrchestratorRepository`"), restated as the journal's own contract; the existing MODIFIED "File journal is contract-tested against DB semantics" block stays. The Purpose paragraph (`:4-5`) cannot change through a delta; it is rewritten at archive (19.1 gains that sub-bullet).
  - `production-scheduler-orchestration` (new delta file): MODIFIED the requirement holding `:729` (manual retry "when a caller uses the PostgreSQL repository"); MODIFIED "Hydro run status sets have one definition and a parity lock" as a whole block: drop the SQL completed-pipeline probe and SQL active probe clauses (`:759, :761`), drop `chain_repository` from the "SHALL remain importable" names (`chain` only), rewrite the "Aliases are the same object" WHEN (`:767`) without `chain_repository.*`, and omit the "SQL active probe counts a pending hydro run" scenario (a scenario can only be removed by omission inside a MODIFIED block; the reason goes in the PR body).
  - `job-retry-mechanism`: MODIFIED the requirement(s) holding `:350, 352, 377, 384-393` (file-journal and DB-backed candidate-state paths → the file-journal path); the DB halves of the reverse-truncation scenario and the "Friendly DB geometry" scenario are omitted inside the MODIFIED block (reason in the PR body). A scenario whose name says "DB" but whose body survives (e.g. `:384` "DB projection reports true population…") is renamed to describe the journal projection; each rename is listed in the PR body as a deviation from "keep every scenario name".
  - `canonical-conversion`: MODIFIED "Forcing usability of canonical quality flags" (`:236`) drops `services/orchestrator/chain_repository.py` from the out-of-scope DB-backed selection queries (keeps `workers/forcing_producer/store.py`).
  - `docs/runbooks/node22-db-retirement-runbook.md:51-54` becomes history ("supplied" → "supplied until R3 (#3260)").
- Must preserve: every DB-free scheduler, journal and candidate-state outcome; the R3-1 covered-by / migrated-to tests unchanged and green; the hydro status-set identity (`chain.COMPLETED_HYDRO_STATUSES` and the journal bind the same `scheduler_state_types` object); `OrchestratorRepository` protocol; R4 symbols until R4.
- Risk packs (issue-risk-contract vocabulary):
  - Selected: Public API / CLI / script entry (package `__all__` loses `services.orchestrator.PsycopgOrchestratorRepository`; `ForecastOrchestrator.from_env` classmethod deleted → evidence 1 grep shows no user in apps/workers/scripts/packages/infra). Legacy compatibility / examples (chain facade bindings, inventory pins, no lost semantics coverage → evidence 2, the R3-1 table). Documentation / migration notes (runbook, inventories, spec deltas → evidence 1 + `openspec validate`).
  - Not selected: Config / project setup (the `DATABASE_URL` read path in the deleted `from_env` goes, but no config key, template or env contract changes); Concurrency / shared state / ordering (no lock or scheduling code); Schema / columns / units / field names (no DB schema change; `ops.pipeline_job` stays the node-27 projection); File IO / path safety (no file writes change); Auth / permissions / secrets (the deleted factory read `DATABASE_URL`; nothing new reads secrets); Resource limits (the limits keep their values and owner); Error handling / rollback (no except path changes); Release / packaging (no dependency change; psycopg stays for other callers).
- Required evidence:
  1. `grep -rn PsycopgOrchestratorRepository services apps scripts tests` returns nothing (the issue's acceptance criterion; any facade-absence test spells the name without the literal, e.g. by concatenation, or uses the module-level `not in __all__` check). `grep -rnwE "_active_repository_from_env|_orchestrator_repository_from_env|chain_repository|test_chain_repository_nfs_raw_manifest" services apps scripts tests packages workers infra docs/governance` (whole-word, so the kept `chain_repository_state` module does not match; no `grep -v` filter) returns nothing except historical mentions, each listed (e.g. `scripts/ci/full_test_durations.json:33` may keep the stale key per `tests/test_ci_shard_tests.py:177`).
  2. The R3-1 table re-walked: each delete row → deleted (cite), each covered-by / migrated-to / unaffected row → still present and green (node ids run).
  3. `tests/test_hydro_status_set_parity.py` green; it asserts `file_orchestration_journal.COMPLETED_HYDRO_STATUSES is scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES` (existing `:150`), and `file_orchestration_journal.py` imports both limits from `scheduler_state_types` (checked on the import statement, since `is` on the int 100 proves nothing).
  4. Full suite plain green (proxy env unset); ruff; `openspec validate --strict`; CI full suite.
  5. node-27 (5.4): remaining real-DB suites pass on throwaway `nhms_it_*` DBs: `-m integration tests/test_real_database_integration.py tests/test_qhh_production_bootstrap_scheduler.py tests/test_orchestration_chain.py tests/test_readonly_db_discovery_integration.py tests/test_integration_gate.py` plus `tests/test_node27_*_integration.py`. Expected: all pass, leftover DBs 0.
  6. node-22 (5.4): one idle pass and one submitting pass after the ff deploy at a pass boundary, stepping through every earlier merged slice SHA first; if not done when the PR is ready it merges with `Part of #3260` and 5.4 unticked.

Slice triage (R3-3, #3315, task 5.5):
- Issue type: deletion plus doc/spec hygiene. No runtime behaviour changes: the SQL `candidate_state` body takes `self` and calls `self._fetch_optional` / `self._fetch_all`, which no surviving class provides. Its only caller was `PsycopgOrchestratorRepository`, deleted in R3-2 (#3260).
  - Fixture level: compact. Upstream suggested level: expanded (task group 5). Override: the only legacy-compatibility surface is the `chain._max_lead_hours_from_lineage` facade alias and its inventory entry, which have no importer (`git grep` returns only the definition, the alias and the inventory line); everything else is comments, docs and change-local spec text. It touches no live code path, shared contract or schema.
  - Width: one PR. The line-reference rewrites (C) must land with the deletion (A1) that shifts their targets.
- Recon at c6cd5610c, re-verified against the issue body:
  - A1: `chain_repository_state.py:459-647`, with the reads at `:476`, `:505`, `:552`, `:590`, `:612`. `candidate_state_from_rows` begins at `:650`.
  - A2: `chain_runtime_utils.py:605` and its `chain.py:608` alias, plus `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md:367`.
  - B1: `file_orchestration_journal.py:1956` (the issue says `:1953`).
  - B2-B4 and C1-C4: unchanged from the issue.
  - D: six `chain_repository_state.py:*` baseline keys plus the stale `safe_fs.py:10` key.
  - E1-E7: as in the issue; the implementer re-greps them.
- Change list (implementer): A1, A2, B1-B4, C1-C4, D and E1-E7, exactly as the issue body's Scope and Approach describe them.
  - Comments and docstrings name symbols, not new line numbers.
  - The baseline is regenerated with `--write-baseline`, and its diff only removes keys.
  - Test prose in `tests/test_production_scheduler.py` that cites `chain_repository_state.py:>=650` may become symbol references, but is not required (issue Out of scope).
- Spec (orchestrator, change-local, in this PR's final push):
  - The #3315 addendum (from the #3261 review):
    - `production-scheduler-orchestration` "A terminal run-manifest-missing retry SHALL consult the per-model forcing witness" (main `:1297-1349`): its "DB lane ... returns none there ... DB-lane behavior is unchanged" sentence is now a MODIFIED block saying the file-journal lane is the only candidate-state lane.
    - Main `:761` ("SQL completed-pipeline probe") is already superseded by R3's REMOVED + ADDED pair; no edit.
    - `job-retry-mechanism` "Cycle-granularity manual retry markers…" (change delta, from R4): the "DB read path" alignment clauses and of the three DB-parity AND bullets, two are removed and one is rewritten to keep only the suffix-widening fact.
      - Every journal-path rule and every scenario name stays.
      - The membership predicate now reads "SHALL follow this predicate" instead of "SHALL align with the DB read path's".
  - PR #3325 round-1 carry-overs (`.workplans/3311/carry-overs.md`, P2):
    - In `slurm-array-runner-integration` "Array-capable model stages": the `db_free_required_path_unsafe` example is narrowed to the ENOTDIR/ELOOP root rows observed, and the `_SYMLINK` code to a final-segment loop.
    - In the workspace-root loop requirement: the out-of-workspace lock-loop clause cites `test_resolve_residue_config_final_segment_loop_converges_to_structured_refusal[NHMS_SCHEDULER_LOCK_ROOT-expected_blockers1]`, and the rows 19-20 clause is narrowed to the preflight block with `SCHEDULER_ROOT_WORKSPACE_ROOT_OUT_OF_APPROVED_ROOT` (`workspace_root`).
    - Appendix D: the R2c-4 row names PR #3325's body as the durable copy of the observed table.
- Out of scope (reported):
  - R4/R5, the T1 table, and the test-file line references outside the checker's scan.
  - `openspec/glossary.md:37`.
  - `multibasin-state-idempotency/spec.md:10`.
  - From PR #3325: `scheduler_runtime.py:956`, `:1358` and `:1482` (`!= "not_required"`, now always true) and the `evidence_safe_paths` second-order follow-up.
- Must preserve: `candidate_state_from_rows` and its defaults stay byte for byte; so does every live code path. The `OrchestratorRepository` protocol stays.
- Risk packs: Legacy compatibility (no live caller, no facade/inventory drift). The entropy facade guard and the line-reference checker stay green. Not selected: everything else.
- Required evidence:
  1. The issue's acceptance greps, each with its output. The `_max_lead_hours_from_lineage` grep is scoped `-- services scripts tests docs`, because this block names the symbol (a deviation recorded in the PR body).
  2. The `line_reference_baseline.json` diff: removals only.
  3. `git diff master -- services/orchestrator/chain_repository_state.py` deletes `:459-647` and nothing else (no change from `def candidate_state_from_rows` on); `ruff check --no-cache .` passes.
  4. The PR body lists the E1-E4 and E7 rewrites as old → new.
- Verification (owner policy 2026-11-22: no local full suite):
  - Local tests:
    - `tests/test_entropy_audit_line_references.py`
    - the facade guards `tests/test_entropy_audit_facade_guard_aliases.py` and `tests/test_entropy_audit_facade_guard_forwarders.py`
    - `tests/test_entropy_audit_instruction_inventory.py`
    - `tests/test_file_orchestration_journal.py`
    - `tests/test_file_journal_repository_semantics.py`
    - `tests/test_orchestration_chain.py`
    - `tests/test_import_layering_guard.py`
    - `tests/test_retry_accounting_ast_guard.py`
    - every test that greps `CHAIN_COMPATIBILITY_INVENTORY.md` / `SCHEDULER_COMPATIBILITY_INVENTORY.md` (`git grep -l COMPATIBILITY_INVENTORY -- tests`)
  - Local checks: ruff and `openspec validate`.
  - CI: the full sharded suite replaces the issue's "full suite green" criterion.
  - node-22: none. This is a deviation from D5 (design.md:227), recorded in the PR body: the deleted body and helper have no referent (acceptance greps 1-2, the A1 import greps), so a pass exercises no changed path; module import is covered by CI's unit shards.
  - node-27: none. No integration-marked suite references the deleted symbols (they were removed in R3-2).

## 6. R4 — Delete the SQL `RetryService`

- [x] 6.1 Delete the Appendix C R4 symbols. Keep every other name in `retry.py`, including `_auto_retry_job_can_be_reused` and `_safe_error_message` (`retry.py:405-415`).
- [x] 6.2 Triage `tests/test_retry.py` test by test: delete the `RetryService`-class tests and keep the classify/backoff tests. Update the other Appendix C R4 test references. `check:api-types` stays clean (the OpenAPI document is unchanged).
- [x] 6.3 Add the Appendix D R4 spec deltas.
- [x] 6.4 Verification: node-27 C2 receipt (display read routes PASS, deny-write PASS), because `readonly_validation_probe.py` changes. On node-22, one idle pass and one submitting pass.

Suggested fixture level: expanded - an API dependency provider and the display deny-write probe.
Minimal mergeable slice: atomic - the service, its provider, its aliases (`chain.py:96`, `chain_forecast_execution.py:105`) and its only isinstance consumer are one component; deleting part of it fails at import.

Slice triage (R4, #3261, tasks 6.1-6.4):
- Issue type: deletion (behaviour-preserving for every production path; the OpenAPI document is unchanged). Fixture level: expanded (agree): an API dependency provider, the node-27 C2 read-only probe, and import-time chain compat guards.
- Governing invariant: the only retry service any production path builds is `FileJournalRetryService` (`scheduler_core.py:93-97,480`); no route resolves a SQL retry service; the display deny-write probe still proves no write dependency is constructed; the chain facade still imports cleanly.
- Recon at a29098294: the line numbers in this block replace Appendix C's stale R4 references (`chain.py:96`, `chain_forecast_execution.py:105,827`, `chain_compat_runtime.py:212-227,224,561`, `chain_forecast_orchestrator_cycle.py:131`, `test_production_scheduler.py:40750,44700,45508`) and those on the group's "Minimal mergeable slice" line.
- Blast radius: `services/orchestrator/retry.py` (class `:251-402` plus imports it alone used: `select`, `PipelineStore`, `datetime`/`UTC`), `apps/api/routes/pipeline.py:128-132` (`get_retry_service`) and its now-unused imports (`RetryConfig, RetryService` `:50`; `SlurmGatewaySettings, get_settings` `:51` if nothing else uses them), `apps/api/readonly_validation_probe.py:54` (override), `chain.py:94`, `chain_forecast_execution.py:104,826` (alias + `isinstance` branch), `chain_forecast_orchestrator_cycle.py:109` (annotation), `chain_compat_runtime.py:165-190,475,479,508` (`_retry_service_from_env` + local-factory guards/exports), `chain_compat_static.py:265,305-310` (alias token, local-factory family), `scheduler.py:35` (re-export), `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md:42` (the `rg` replay pattern contains `RetryService`), `:236,260,284`, `docs/runbooks/node22-control-plane-manual-recovery.md:464` (mark historical), comment/docstring mentions `chain_forecast_orchestrator_cycle.py:276`, `scheduler_state_failure.py:314`, `tests/test_production_scheduler.py:10272,10293,10295`, `tests/test_db_free_auto_retry_manifest.py:5` (update or mark historical), the tests below, spec deltas.
- Sibling surfaces: `FileJournalRetryService` and `_db_free_file_retry_service_from_env` (kept, the live path); every other `retry.py` name (classifiers, error-code sets, `RetryConfig`, `compute_backoff_seconds`, status sets, `RetryError` family, `_safe_error_message`, and — per the issue's Out of Scope — `_auto_retry_job_can_be_reused` / `REUSABLE_AUTO_RETRY_STATUSES`, which have zero callers after R4: reported for a follow-up, not deleted here); the retry route's 409 stub and the cancel route (#3245) keep their dependency chains (`pipeline.py:195-199,552-565`); `ForecastOrchestratorCycleMixin`'s duck-typed `handle_failed_job` branch (`:290-296`, out of scope); `openapi/nhms.v1.yaml` and `apps/frontend/src/api/types.ts` (no retry-service symbol).
- Delete (6.1): `RetryService`; `get_retry_service` and the probe override line; `_retry_service_from_env` with its install, export and `scheduler.py` re-export; the `RetryService` aliases (`chain.py:94` import name, `chain_forecast_execution.py:104`) and the `isinstance(service, RetryService)` branch (`chain_forecast_execution.py:826`, unreachable in production: a `FileJournalRetryService` has no `store`); the annotation becomes the protocol / file service type; the whole `_CHAIN_RETRY_COMPAT_LOCAL_FACTORY_*` family and the `chain-local-factory` classification (`chain_compat_static.py:305-310`, `chain_compat_runtime.py:181-190`) since `_retry_service_from_env` is its only member; `RetryService` leaves `_CHAIN_RETRY_COMPAT_ALIAS_NAMES` (`:265`); inventory rows updated.
- Tests (6.2), each with exactly one disposition recorded in the PR body (R3-1 rule: delete only when the subject is the SQL lane and a named file-lane test covers the mode-independent property; otherwise migrate onto `FileJournalRetryService` / an existing fake):
  - `tests/test_retry.py`: delete the `RetryService` tests (`:228, 236, 244, 255, 265, 288, 303, 313, 323, 403, 424, 443, 478, 495, 513, 528, 555, 600, 624, 642, 661`); each delete row cites a same-geometry file-lane test (the event-detail ones `:403, 424, 478, 495` by `tests/test_file_orchestration_journal.py:11643-11858` `test_file_journal_permanently_failed_event_*` / `test_master_permanently_failed_event_*`), or the test migrates; `:443` (the `SLURM_JOB_FAILED` unknown-reason ruling) and `:513` (`OUT_OF_MEMORY` at `retry_count == max_retries`: `limit_exhausted` with `auto_retry_skipped`) have no exact-geometry file-lane counterpart and migrate onto `FileJournalRetryService` unless one is cited; each migrated test fails under a mutation of the file-lane rule it pins (reverted); keep the classify/backoff/spec-parity/literal tests (`:90-219, 344-389`) and `:678, :687` (PipelineStore only, R5) with their helpers.
  - `tests/test_orchestration_chain.py`: `_DbFreeRetryGateService(RetryService)` (`:1652`, users `:1695, :1713, :16858`, imported by `tests/test_production_scheduler.py:54056, 54089, 54109, 54175`) becomes a standalone store-less fake that keeps the real policy (`should_auto_retry` / `retry_policy_for_job` via `classify_failure` and the `permanently_failed` check, as `retry.py:260-271` — a shared helper or a delegation, not a hand-written return value, so `test_production_scheduler.py:54089,54175` and `test_orchestration_chain.py:1695,1713` keep asserting policy), exposes `config`, has exploding `mark_permanently_failed` / `handle_failed_job`, and has no `repository` / `store` attribute (the `:16849` seam-3 test distinguishes the shape gate at `chain_forecast_orchestrator_cycle.py:279` from a method-name gate); the SQL-suffix tests (`:6093`, `:14205`) are migrated or deleted per the rule; the compat pin `:11047-11100` and `:11325` updated to the reduced sets; comments `:14175, 15148-15150, 16849`.
  - `tests/test_chain_cross_stage_state.py:244-250`, `tests/test_e2e_m3.py:22,111,138` (`test_e2e_m3_retry_chain`): migrate or delete per the rule, cited.
  - `get_retry_service` overrides/assertions: `tests/test_retry_cancel_consistency.py:20,890,1076-1101,1339`, `tests/test_retry_route_fail_closed.py:32,198,259,295,312-317`, `tests/test_monitoring_api.py:2052,2081`, `tests/test_slurm_gateway_openapi_security.py:818` — remove the deleted name; the `:295` "not resolved by the retry route" assertion becomes a `not hasattr(pipeline_routes, "get_retry_service")` check; `:312-317` keeps asserting the file lane's retired submit methods.
  - `_retry_service_from_env` patches: `tests/test_production_scheduler.py:26408, 26476, 41125, 45085, 45834`, `tests/test_scheduler_dbfree_only_mode.py:88-89` — removed name by name; each guard keeps its other patches and assertions (a guard left with none is reported, not deleted).
- Spec (6.3): extend the existing `file-orchestration-journal` MODIFIED "Scheduler can use a file-backed orchestration journal" (drop the `_retry_service_from_env()` mention in the scenario, delta `:107-108` = main `openspec/specs/file-orchestration-journal/spec.md:110-111`); new MODIFIED `real-slurm-gateway-contract` "Retryable Slurm errors are stable" (`:58-97`; "RetryService MUST treat the job as eligible" at `:66,72,78` → the file-journal retry classification); new MODIFIED block in the existing `job-retry-mechanism` delta for "Cycle-granularity manual retry markers require model attribution…" (`:441`; `:1044` "every marker written by the SQL retry service" → legacy markers). Also a MODIFIED `job-retry-mechanism` "Retry Guard — Non-Transient Error Exclusion" (`:109-184`; `:170` "consumed by both the DB plane and the file-journal plane" → the file-journal plane only), found false by the implementer's grep. Every scenario kept (openspec 1.13.2 rejects omission inside MODIFIED). The implementer greps `openspec/specs` for `RetryService` / `_retry_service_from_env` / `get_retry_service` and reports any other clause now false (e.g. `ops-monitoring-api:287`, `pipeline-control-state-consistency:44`, `retry-execution-contract:58`).
- Must preserve: OpenAPI document byte-identical (`tests/test_openapi_drift.py`, `cd apps/frontend && pnpm check:api-types`); display read routes and the C2 deny-write result (`write_dependency_constructed=False`, `write_executed=False`, 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` for retry and cancel); scheduler auto-retry decisions (file lane); every file-lane retry/event-detail test unchanged and green.
- Risk packs (issue-risk-contract vocabulary):
  - Selected: Public API / CLI / script entry (`get_retry_service` provider removed; OpenAPI unchanged → `test_openapi_drift` + `check:api-types`); Auth / permissions / secrets (the deny-write probe is a permission boundary → node-27 C2 receipt); Legacy compatibility / examples (chain facade aliases and import-time guards, inventory pins, no lost coverage → the disposition table); Documentation / migration notes (inventory, spec deltas).
  - Not selected: Config / project setup (no config key; `get_settings` import may go but settings are unchanged); Concurrency (no lock/scheduling change); Schema (no DB change); File IO (none); Resource limits (none); Error handling / rollback (the deleted SQL lane's error paths have no production caller; file lane unchanged); Release / packaging (no dependency change).
- Required evidence:
  1. `grep -rnw "RetryService\|get_retry_service\|_retry_service_from_env" services apps scripts tests docs` returns nothing except `FileJournalRetryService` substrings (excluded by `-w`) and intentional `not hasattr` / historical mentions, each listed.
  2. The test disposition table (every test above → delete+covered-by / migrated-to / kept), with covered-by citations; migrated tests green.
  3. `uv run pytest -q tests/test_openapi_drift.py` green and `cd apps/frontend && pnpm check:api-types` clean (local).
  4. Full suite plain green (proxy env unset); ruff; `openspec validate --strict`; CI full suite.
  5. node-27 C2 receipt (6.4), unattended, in a throwaway detached worktree of the PR head (display API not redeployed): `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`; `uv sync --all-extras --dev` in the worktree; `set -a; . /home/nwm/NWM/infra/env/display.env; set +a` (gitignored, so sourced from the production checkout); export `NHMS_DISPLAY_READONLY_DATABASE_URL` from the private store (never printed); `uv run python scripts/validate_readonly_db_boundary.py --run-id <id> --evidence-root <worktree>/artifacts/<dir>` (the validator rejects roots outside the repository `artifacts/`; copied out afterwards). Bound to the branch in the same worktree and interpreter: `git rev-parse HEAD` equals the PR head, `apps.api.readonly_validation_probe.__file__` is under the worktree, and `not hasattr(apps.api.routes.pipeline, "get_retry_service")`. Expected `summary.json`: top-level `status`, `lane_statuses.read_routes` and `lane_statuses.deny_write` all `PASS`; `manual_action_probes` has exactly `display_retry_manual_action` and `display_cancel_manual_action`, each `status=PASS`, `http_status=409`, `observed_error_code=CONTROL_PLANE_MANUAL_ACTION_REQUIRED`. CI oracle for the probe edit: `tests/test_readonly_db_validation_routes.py:808`.
  5b. node-27 integration (D5: `apps/api/routes/pipeline.py` is reachable from integration-marked suites via `production_closure`): `-m integration tests/test_real_readonly_db_probe_integration.py tests/test_real_readonly_db_route_smoke_integration.py tests/test_readonly_db_discovery_integration.py tests/test_real_database_integration.py` on throwaway `nhms_it_*` DBs. Expected: all pass, leftover DBs 0.
  6. node-22 (6.4): one idle pass and one submitting pass after the ff deploy at a pass boundary, stepping through every earlier merged slice SHA first; node-22 is still stepping through the earlier slice SHAs, so the PR merges with `Part of #3261` and 6.4's node-22 half unticked.

## 7. R5 — Delete orphaned `PipelineStore` write methods

- [x] 7.1 Switch the fixtures that seed rows through `create_job` / `reserve_job` / `reclaim_reservation` / `bind_reservation` (Appendix C, R5 row) to ORM inserts. This is a test-only PR.
- [x] 7.2 Delete the four methods. Test-only callers do not count as callers. Keep `update_job_status` and `insert_event` (cancel route).
- [x] 7.3 Verification: node-27 C2 receipt (read routes and deny-write); node-22 one idle pass and one submitting pass (D5 deleting slice). `tests/test_pipeline_persistence.py` runs on SQLite, so CI covers it.

Suggested fixture level: compact - deletion of unreferenced methods in one module plus fixture changes.
Minimal mergeable slice: 7.1 first, test-only; then 7.2 + 7.3.

Slice triage (R5-1, #3262, task 7.1):
- Issue type: test. Fixture level: compact (agree): test-only, no production code (`services/orchestrator/persistence.py` untouched). Blast radius: every test and helper that calls `create_job` / `reserve_job` / `reclaim_reservation` / `bind_reservation` on a `PipelineStore` (`persistence.py:111, :288, :334, :257`), directly or through production code; a fixture rewritten to seed different rows, or a double that loses the methods' conflict semantics, would silently change what the test proves.
- Premise for 7.2 (checked at ac9a8d85d): no production code calls these methods on a `PipelineStore` once R4 lands. `persistence.py:314` is `reserve_job`'s own call to `create_job` (deleted with it); `retry.py:293` is inside `RetryService` (`retry.py:251-402`, deleted by R4); `reconcile_reserved_phases.py:908` calls a duck-typed `store`, which in production is the file journal (`scheduler_runtime.py:1869-1880`, `chain_stage_execution.py:270`); `chain_forecast_orchestrator_cycle.py:864` calls the module function `reservation.py:336` → `repository.bind_pipeline_job_reservation`. So 7.2 (R5-2) cannot merge before R4; 7.1 is not blocked by this.
- Scope (re-grep `git grep -nE '\.(create_job|reserve_job|reclaim_reservation|bind_reservation)\(' -- tests`): only calls whose receiver is a `PipelineStore` (directly, or through a helper/fixture that returns one). Calls on `FileOrchestrationJournalRepository`, other repositories, or test fakes with the same method names are out of scope (e.g. `tests/test_file_orchestration_journal.py:2450`, `tests/test_gateway_reconcile_master_transitions.py:44`, `tests/test_gateway_reconcile_reserved_pins.py:311`, `_GuardStore` in `tests/test_gateway_reconcile_inflight_identity.py:352`); the PR body lists each excluded hit with its receiver type. Appendix C's list is a starting point, not the full set.
  - Also in scope: tests that pass a `PipelineStore` (incl. `_StoreRepo(...).store`) into surviving production code that calls these methods — today `reconcile_reserved_unbound_jobs` → `reconcile_reserved_phases.py:908` `store.bind_reservation` (e.g. `tests/test_gateway_reconcile_idempotency_barrier.py:474`, `tests/test_gateway_reconcile_reservation_lifecycle.py:109, :153, :206, :250, :570`, `tests/test_gateway_reconcile_grace_guard.py:82-484`). Find them with `git grep -nE 'reconcile_reserved_unbound_jobs\(' -- tests` and check the receiver of each hit. 7.1 migrates them to the ORM-backed double below or to the file-journal repository, with the choice and reason in the PR body.
- Per call site, exactly one disposition in the PR body table (a test body that both seeds directly and goes through a double gets one row per part, e.g. `tests/test_orchestration_chain.py:16564-16573`):
  - **seed → ORM**: the call only puts rows in place for a test whose subject is something else. Replace it with an ORM insert (`PipelineJob(...)` / `PipelineEvent(...)`) that writes the same column values the method wrote, including the defaults and derived fields it sets, added to the session and committed (then refreshed), unless the original call passed `commit=False`, in which case flushed. One shared helper is preferred over per-file copies. The test's assertions are unchanged. (`tests/test_orchestration_chain.py:541`, a plain insert inside `upsert_pipeline_job`, is this kind.)
  - **double → ORM-backed double**: the call sits inside a test repository double (`_StoreRepo` in `tests/gateway_reconcile_helpers.py:63, :80, :98`; `StoreBackedCycleRepository` in `tests/test_orchestration_chain.py:622, :647, :663`; `_RaceSemanticsCycleRepository` :16500) whose return value production code consumes, or it is the store production code calls (the `reconcile_reserved_unbound_jobs` tests above). Re-implement the method's semantics in one shared test helper with ORM statements: insert inside `begin_nested()` with `IntegrityError` → `None`; reclaim only when `slurm_job_id IS NULL AND status IN ('submission_failed', 'reservation_lost')`, filling identity columns only when NULL; bind is a no-op returning `None` when the row is missing or already bound, and sets `submitted_at` only when NULL (re-read each rule from `persistence.py` rather than trusting this summary). Do not reduce it to a plain insert.
  - **subject (R5-2)**: the test asserts the method's own behaviour (its return value or defaults, conflict/IntegrityError translation, reservation semantics), e.g. `tests/test_pipeline_persistence.py:11-25` (`status == "pending"` default). Not rewritten here; R5-2 deletes it with the method. The row cites the asserted line.
  - **production-path (R4-owned)**: reached only through `RetryService` (`retry.py:251-402`), which R4 deletes with its tests. Listed, not changed.
- Must preserve: no production code change; every rewritten test keeps its assertions byte-for-byte; the rows a rewritten fixture produces equal, column for column, the rows the method produced, except `created_at` / `updated_at` / `submitted_at`, which are compared for NULL vs non-NULL (or under a patched `utc_now`).
- Risk packs selected: Legacy compatibility (fixtures seed the same rows and doubles keep the methods' semantics → the equivalence checks below). Not selected: every other pack (test-only).
- Required evidence:
  1. The table: every in-scope call site → seed → ORM | double → ORM-backed double | subject (R5-2) | production-path (R4-owned), with the receiver type; the `reconcile_reserved_unbound_jobs` hits with their receiver and chosen migration; plus the excluded non-`PipelineStore` hits.
  2. Equivalence: for each distinct seeding shape, a check that the ORM helper and the original method produce identical rows on the same inputs (timestamp columns as above). For each double method, a check that the helper and the original method return the same result (row / `None`) and leave the same rows in the win, conflict, dead-reclaim, live-reclaim-refused and already-bound cases. These checks may be committed as helper tests, since they pin the helper (they are deleted or rewritten in R5-2 when the original methods go).
  3. `git grep` after the change: no call to the four methods on a `PipelineStore` remains outside the subject (R5-2) and production-path (R4-owned) rows, and no test passes a `PipelineStore` into `reconcile_reserved_unbound_jobs`.
  4. Touched test files green; ruff; CI full suite (`tests/test_pipeline_persistence.py` runs on SQLite).

Slice triage (R5-2, #3263, tasks 7.2, 7.3):
- Issue type: deletion (no production caller; behaviour-preserving). Fixture level: expanded (override of the issue's compact: `services/orchestrator/persistence.py` is the `pipeline_job` store under the project's `orchestrator`/`pipeline` trigger, `openspec/project-profile.md:107`; the Auth/permissions and Legacy-compatibility packs are selected, both expanded triggers in `issue-risk-contract.md:22`; R3-2's persistence deletion was expanded too). No spec delta: `grep -rnwE 'create_job|reserve_job|reclaim_reservation|PipelineStore' openspec/specs` hits only `node22-dbfree-runtime/spec.md:39` and `file-orchestration-journal/spec.md:111`, both forbidding DB-free mode from reaching a SQLAlchemy `PipelineStore` (unaffected by deleting methods); no requirement names `PipelineStore.bind_reservation`; design.md Appendix D (`:331-337`) assigns R5 no delta; the existing deltas are unchanged.
- Governing invariant: `PipelineStore` keeps only the surface the display API and cancel route use (`update_job_status`, `insert_event`, the `get_*`/`query_*` reads); no production path writes through it; the ORM-backed test doubles (`tests/pipeline_job_orm_helpers.py`) keep exactly the semantics R5-1 proved equal to the deleted methods.
- Recon at 582bc5b83: `git grep -nwE 'create_job|reserve_job|reclaim_reservation|bind_reservation' -- services apps scripts packages workers` hits only the four definitions (`persistence.py:111, :257, :288, :334`), `reserve_job`'s own `self.create_job` (`:314`), the `_UNSET_SUBMITTED_AT` sentinel and its comment (`:23-25`, used only by `create_job`), and unrelated same-name surfaces that stay: `reservation.py:336` (module function), `chain.py:90` / `chain_compat_static.py:154,183` (its facade), `file_orchestration_journal.py:2677` (the journal's method), `reconcile_reserved_phases.py:908` (duck-typed `store.bind_reservation`: production receiver is the file journal via `scheduler_runtime.py:1771-1781`), `chain_forecast_orchestrator_cycle.py:855`. Tests: `tests/test_pipeline_persistence.py:14` (subject) and `tests/test_pipeline_job_orm_helpers.py` (13 paired calls; its docstring says R5-2 deletes or rewrites it); the other hits have a non-`PipelineStore` receiver and stay: the journal (`tests/test_file_orchestration_journal.py:2451`, `tests/test_gateway_reconcile_master_transitions.py:44`), `OrmPipelineStore` (`tests/pipeline_job_orm_helpers.py:234-243`), `_GuardStore` (`tests/test_gateway_reconcile_inflight_identity.py:354`), and fakes in `tests/test_production_scheduler.py`, `tests/test_source_scoped_dispatch.py:97`, `tests/test_gateway_reconcile_reserved_pins.py:311`.
- Blast radius: `services/orchestrator/persistence.py` (the four methods, the sentinel, and any import only they used — `IntegrityError` is a local import inside `reserve_job`); `tests/test_pipeline_persistence.py:12-45`; `tests/test_pipeline_job_orm_helpers.py`; `tests/pipeline_job_orm_helpers.py` only if a reference to the deleted methods remains there (docstrings/comments citing them as the live original become historical). Added during implementation: `tests/status_literal_allowlist.json`, whose `services/orchestrator/persistence.py` entry (`reservation_lost`, `submission_failed`) was sourced only by `reclaim_reservation`'s status tuple and goes stale with it (`test_status_literal_scan`).
- Non-goals: the read methods; `update_job_status` / `insert_event` (cancel route `apps/api/routes/pipeline.py:658-659`, #3245); the ORM models; `RESERVED_STATUS` (still used by `query_reserved_unbound_jobs` `persistence.py:249` and imported by `tests/pipeline_job_orm_helpers.py:47`); `reservation.py` / the journal's same-name methods.
- Sibling surfaces (must stay): `RESERVED_STATUS` / `RESERVATION_ACTIVE_STATUSES` (`reservation.py:38-39`, `scheduler_state_rows.py:10`); `update_job_status`, `insert_event`, every read; `_restart_reconcile_store`'s method-name gate (`scheduler_runtime.py:1771-1781`, it names the journal's `bind_reservation`); `OrmPipelineStore` and the four `orm_*` helpers (consumed by `tests/gateway_reconcile_helpers.py`, `tests/test_orchestration_chain.py`, `tests/test_chain_cross_stage_state.py`, `tests/test_gateway_reconcile_inflight_identity.py` and the reconcile suites).
- Delete (7.2): the four methods, `_UNSET_SUBMITTED_AT` and its comment, imports left unused. Nothing else in `persistence.py`.
- Tests:
  - `tests/test_pipeline_persistence.py:12 test_pipeline_job_crud_with_sqlite_ops_schema`: the `create_job` call seeds through `seed_pipeline_job` with the same columns; the assertions that pin `create_job`'s own return (`job.status == "pending"`, `job.model_id`, `job.job_id` on the returned object) are dropped as subject-of-deleted-method; every `update_job_status` / read assertion stays byte-for-byte.
  - `tests/test_pipeline_job_orm_helpers.py`: rewritten, not deleted — the helpers stay load-bearing for the reconcile suites. Each paired case becomes a case against the helper with literal expectations, still run on both helper surfaces (`_HELPER_SURFACES` `:107-110`: the `functions` surface and the `orm_store` surface `OrmPipelineStore`, which the reconcile suites consume), with literal expectations equal to what the paired run asserted (returned `None` vs row, and the full row columns, timestamps under the same patched `utc_now` / NULL-vs-non-NULL rule). Every case keeps its name or a recorded rename, and each rewritten case is shown red under a mutation of the helper rule it pins, on both surfaces (reverted): reserve conflict → `None` with a usable session, reclaim status/NULL-identity predicate, bind missing/already-bound no-op and NULL-only `submitted_at`. `test_orm_store_is_not_a_pipeline_store_and_keeps_only_surviving_reads` is updated to the reduced `PipelineStore` surface.
- Must preserve: display read routes and C2 deny-write (`PipelineStore` is the display dependency); cancel route (`update_job_status` / `insert_event`, #3245); every reconcile test that drives production code with `OrmPipelineStore`; OpenAPI unchanged.
- Risk packs:
  - Selected: Legacy compatibility / examples (the helper pin module loses its oracle → literal expectations + mutation checks); Auth / permissions / secrets (display dependency module → node-27 C2 receipt).
  - Not selected: Public API (no route or provider changes; OpenAPI unchanged), Config, Concurrency (no production writer), Schema (ORM models untouched), File IO, Resource limits, Error handling (deleted paths have no caller), Release / packaging, Documentation (no doc names the four methods).
- Required evidence:
  1. `git grep -nE 'def (create_job|reserve_job|reclaim_reservation|bind_reservation)' services/orchestrator/persistence.py` empty; `git grep -nwE 'create_job|reserve_job|reclaim_reservation|_UNSET_SUBMITTED_AT' -- services apps scripts packages workers` empty; remaining test hits listed with receiver type.
  2. Rewrite table for `tests/test_pipeline_job_orm_helpers.py` (case → literal expectation source → mutation id red) and the `test_pipeline_persistence.py` assertion diff.
  3. Full local suite green (proxy env unset); ruff `--no-cache`; `openspec validate --strict`; CI full suite.
  4. node-27 C2 receipt (7.3), same unattended recipe as R4 evidence 5 (evidence root under the worktree's `artifacts/`, copied out; binding checks: HEAD equals PR head, `not hasattr(PipelineStore, "create_job")`); node-27 integration run of the four R4 5b files on throwaway DBs.
  5. node-22 (7.3): R5's own merge SHA needs an idle and a submitting pass (design.md:227; the collapsed cadence comment on #3227 covers SHAs only up to R4 `582bc5b83`). Expected: idle pass `status=planned`, `restart_reconcile.status=completed`, `lock.lock_type=file`; submitting pass `status=submitted`. The idle pass's `restart_reconcile` is the live check that `_restart_reconcile_store`'s method-name gate (`scheduler_runtime.py:1774-1781`) still accepts the file journal. The PR merges with `Part of #3263` until then.
- Review focus: (1) `tests/test_pipeline_job_orm_helpers.py` literal expectations equal the R5-1 paired outcomes on both surfaces, with mutation-red per case; (2) nothing in `persistence.py` beyond the four methods, the sentinel and dead imports is removed; (3) `test_pipeline_persistence.py` drops only `create_job`-return assertions; (4) display read routes / deny-write and the cancel route unchanged (C2 receipt).

## 8. T1 — Pin retry/attempt accounting in one table

- [x] 8.1 Add `tests/test_retry_accounting_table.py` with plain-data rows that call today's pure functions, wherever they live:
  - rows A–F, M, N and Q (design.md Appendix A);
  - the `rsplit("_retry_", 1)[1]) + 1` formulas (`chain_forecast_orchestrator_cycle.py:676-683`, `:750`);
  - the inline `failure_number - 1` term (`:296`);
  - the non-master path (`file_orchestration_journal.py:12455-12459`, pinned on the caller-snapshot `retry_count` it uses today);
  - the status-set difference between `chain_forecast_orchestrator_cycle.py:224,247` and `retry.py:67-78`, pinned as different;
  - the default limit, with `NHMS_SCHEDULER_RETRY_LIMIT` and `SLURM_GATEWAY_MAX_RETRIES` overrides.
- [x] 8.2 In the same file, add an index of integration rows G–L, O and P: each row's input and expected outcome, plus the existing test that pins it (Appendix A), asserted to exist by node id.
- [x] 8.3 Verification: CI only (test-only).

Suggested fixture level: compact - test-only.
Minimal mergeable slice: atomic - one new test file.

Slice triage (T1, #3264, tasks 8.1-8.3):
- Issue type: test (characterization; no production change). Fixture level: compact (agree): one new test file, no production code. Recon at 9e7af1aee (`.workplans/3264/recon.md`, anchors corrected by fixture review); the anchors in this block replace the stale ones in 8.1 and Appendix A.
- Governing invariant: the table pins what the code does today, divergences included, and is green on unchanged production code. A divergence is pinned as a divergence (two rows, same input where possible, different expected values), never reconciled. The table is the baseline T2 (9.1-9.7) must keep, so it covers every site T2 moves.
- Recon anchors (HEAD): `retry_identity.py` `split_retry_job_identity:47`, `retry_suffix_attempt:63`, `effective_retry_attempt:69`, `retry_attempt_floor:75`, `normalize_retry_attempt_floors:89`, `cohort_retry_attempt_floors:115`, `member_charged_retry_attempt:128`, `has_active_manual_retry_decision:162`; `file_journal_retry_helpers.py:51-55` `_next_current_master_retry_identity` (id built at `:55`); `chain.py:837` `_next_retry_attempt_for_stage`; `chain_runtime_utils.py:356` `_retry_attempt_from_basins`, `:448` `_pipeline_retry_job_id`, `:452` `_stage_job_sort_key`; `chain_forecast_execution.py:571,624` mint (`max(floors)+1`, skip-forward loop ~`:655-660`); `chain_forecast_orchestrator_cycle.py:196` `_retry_cycle_stage_job_id`, `:234` / `:243` inline status sets, `:288` `max(retry_count, suffix_attempt, failure_number - 1) + 1` in `_schedule_cycle_stage_retry` (`:248`), `:668-675` / `:741-748` `submission_attempt` increments in `_reserve_cycle_stage` (evidence reaches `repository.reserve_pipeline_job` ~`:770-778`); `file_orchestration_journal.py:12456-12459` non-master `int(source.get("retry_count") or 0) + 1` with the unstripped id, `:12678` manual path `effective_retry_attempt(...) + 1`; `scheduler_state_rows.py:517` `_state_retry_attempt`, `:636` `_job_retry_attempt`, `:656` `stage_retry_attempt_floors`, `:725` `_state_retry_limit`; `retry.py:64-77` status sets, `:201,204` `RetryConfig`; `scheduler_state_types.py:11` `DEFAULT_RETRY_LIMIT`; `scheduler_config/config.py:175` `NHMS_SCHEDULER_RETRY_LIMIT` via `_env_int` (`scheduler_runtime_roots.py:754`); `services/slurm_gateway/config.py:60,66` (`SLURM_GATEWAY_MAX_RETRIES`; `get_settings()` is `lru_cache`d, so build `SlurmGatewaySettings()` directly).
- Blast radius: `tests/test_retry_accounting_table.py` (new) only. No production file, no existing test, no conftest edit.
- Pure rows (8.1), plain data, calling the real function:
  - C (one-basin half): `retry_attempt_floor`. F: `cohort_retry_attempt_floors` + `member_charged_retry_attempt`. M: `split_retry_job_identity` / `retry_suffix_attempt` (stacked; malformed tail → attempt 0), `_next_retry_attempt_for_stage` with a minimal stage. N: `_retry_attempt_from_basins`. Q: `effective_retry_attempt`, `_next_current_master_retry_identity`.
  - `_pipeline_retry_job_id` (9.1): attempt 0 / -1 / 1 / 3 → `_retry_1` / `_retry_1` / `_retry_1` / `_retry_3`.
  - Stage attempt reads (9.3): `_state_retry_attempt` (`stage=None` → the flat `retry_count`; a stage set → the flat `retry_count` is excluded, only matching rows' suffixes and `stage_retry_attempt_floors` count, a non-matching stage row adds 0); `_stage_job_sort_key` (a persisted `retry_count` wins over the suffix; the suffix is parsed only when the count is missing; no clamp; `removesuffix("_retry_0")`; malformed tail → 0 — pinned as different from `effective_retry_attempt`'s max on the same input); `_job_retry_attempt` (stage None → recorded count only; non-matching stage → 0); `stage_retry_attempt_floors`.
  - Limits (9.4): `DEFAULT_RETRY_LIMIT == 3`; `RetryConfig().max_retries == 3`; `_state_retry_limit` (`retry_limit` wins over `max_retries`; empty → `DEFAULT_RETRY_LIMIT`; non-int → 0); `NHMS_SCHEDULER_RETRY_LIMIT` unset / empty / non-int → 3, `5` → 5, `-1` → -1 from `_env_int` (`scheduler_runtime_roots.py:754`) and → 0 at config construction (`scheduler_config/config.py:479` clamps `max(int(self.retry_limit), 0)`); `SLURM_GATEWAY_MAX_RETRIES` unset → 3, `5` → 5, `-1` → validation error, through `RetryConfig.from_settings(SlurmGatewaySettings())`. Pinned divergences: `_state_retry_limit` non-int → 0 vs `_env_int` non-int → default; `-1` accepted by the scheduler env vs rejected by the gateway env; the two env vars independent (one set, the other unchanged).
- Stub-driven rows (8.1), mandatory — if one cannot be reached with the stated stub, stop and report; it may not move to the index:
  - A and E mint half: `_floored_cycle_stage_retry_job_id` with a stub self (`repository.get_pipeline_job`), context (`run_id`, `active_basins[*].state_evidence`) and stage.
  - `_retry_cycle_stage_job_id` (N precedence, `:205-208`): `context.retry_attempt` set and positive (wins over stacked rows); `0` and absent (falsy: fall through to `_next_retry_attempt_for_stage`, driven with a non-empty `existing_jobs` so the result is `_retry_(max+1)`, not `_retry_1`); negative (the only value that reaches the `<= 0 → 1` clamp).
  - `:288` (D): drive `_schedule_cycle_stage_retry` with `current` carrying `accepted_submit_contract_version` and a shape that classifies as `master`, `retry_service.should_auto_retry → True`, and `backoff_schedule=[0]` or a monkeypatched `_chain.time.sleep` (an empty list falls back to 60s). Three rows where `retry_count`, `suffix_attempt` and `failure_number - 1` each alone sets the result. Today the master strips the base (`_retry_N` → `_retry_(N+1)`), unlike Appendix A row D's `_retry_N_retry_M` wording — pin today's value.
  - `:668-675` / `:741-748` increments: observe `submission_attempt` in the evidence passed to a stub `repository.reserve_pipeline_job`, with `supports_accepted_submit_reconcile=True`; the forcing branch also needs `forcing_member_tasks`. Rows: flat suffix, stacked suffix, malformed tail, `context.retry_attempt` larger than the suffix. Pin only the observed values (a negative-tail "difference" from `retry_suffix_attempt` is unobservable through the outer `max` and is not asserted).
  - Journal non-master path (`:12456-12459`) on a `tmp_path` file journal: the caller-snapshot `retry_count` differs from the durable row's, proving the snapshot is read; the id stacks.
  - Master vs non-master pair (9.6): the same input (`X_retry_1`, `retry_count=1`) through `_next_current_master_retry_identity` and the journal non-master path → `X_retry_2` vs `X_retry_1_retry_2`, pinned as different.
- Status sets: observe `_terminal_stage_needs_manual_retry` (`:234`) with `context.retry_attempt` non-None, `active_basins=()`, a non-empty `slurm_job_id`, only `status` varying; and `_terminal_stage_can_retry_after_upstream_refresh` (`:243`) with `refreshed_upstream_finished_at` set and no terminal time, only `status` varying; iterate every status in the `status_vocab` union. Pin `:234` == `MANUAL_RETRY_SOURCE_STATUSES`, `:243` == `FAILED_RETRY_STATUSES - {"permanently_failed"}`, and the two as different. A status whose result comes from another branch under the fixed shape is a fixture error — stop and report.
- Index (8.2): each row's input, expected outcome and pin node id(s), asserted to exist by parsing the target file (AST: a top-level `def test_<name>`), not by collection. Rows: A limit-blocks half (`tests/test_retry_mint_floor.py::test_prefix_switch_does_not_reset_the_minted_attempt_and_the_budget_blocks_on_time`, `::test_failure_lane_prefix_switch_stays_within_the_limit`); B (`::test_cohort_digest_changes_do_not_escape_the_budget`, `::test_failure_lane_cohort_digest_change_stays_within_the_limit`); C sibling half (`::test_sibling_model_retry_rows_do_not_raise_this_models_floor`); G-K (`tests/test_chain_cross_stage_state.py` per recon); L (`::test_restart_stage_from_basins_takes_earliest_marker_and_ignores_markerless_members`); O (`tests/test_orchestration_chain.py::test_manual_retry_claim_judgement_three_states`, `tests/test_production_scheduler.py::test_candidate_basin_manifest_mints_retry_attempt_only_under_active_manual_retry_decision`); P (`tests/test_orchestration_chain.py::test_file_journal_post_window_concurrent_public_cycles_submit_one_retry`; no dedicated pin exists — recorded as a gap in the PR body, not filled here); journal manual path `:12678` (9.2: `tests/test_file_orchestration_journal.py::test_file_journal_manual_repair_suffix_only_target_emits_next_durable_attempt`).
- Must preserve: no production change (`git diff --stat master...HEAD -- services packages apps scripts` empty); every existing pin untouched.
- Risk packs: Legacy compatibility (the rows are T2's baseline → one mutation per pinned function or inline formula turns its rows red, reverted); Config / project setup (env overrides → the limit rows, env set and restored by `monkeypatch`). Not selected: every other pack (test-only).
- Required evidence:
  1. The new file green on unchanged production code (`uv run pytest -q tests/test_retry_accounting_table.py`), with a row count per section and a map row → 9.x task it baselines.
  2. One mutation per pinned function or inline formula (C, F, M, N, Q, `_pipeline_retry_job_id`, `_state_retry_attempt`, `_stage_job_sort_key`, `_job_retry_attempt`, `stage_retry_attempt_floors`, `_state_retry_limit`, `_env_int`, A/E mint, `_retry_cycle_stage_job_id`, `:288` per term, both increments, journal non-master, both status sets), each red, production file restored byte-for-byte (hash).
  3. An index row whose node id is renamed fails the existence check (shown once, reverted).
  4. ruff `--no-cache`; CI full suite (test-only: CI only, no node-27/node-22).

## 9. T2 — `retry_identity.py` becomes the owner

- [x] 9.1 Id formatting. Add `retry_job_id(base, attempt)`, keeping the `max(attempt, 1)` clamp, and delegate the hand-built sites whose output the clamp cannot change: `chain_runtime_utils.py:448-449`, `chain_forecast_orchestrator_cycle.py:289`, `file_journal_retry_helpers.py:55`. The fourth, `file_orchestration_journal.py:12458-12459` (unclamped, stacking), is deferred to 9.2. Retarget their table rows.
- [x] 9.2 Attempt formulas. `retry_identity` gains `next_stage_attempt`, `mint_attempt(floors, is_occupied)`, `inline_retry_attempt`, `next_after_failure_master`, `next_after_failure_non_master`, `restart_stage_from_basins`, plus two names D2 does not list, `reservation_submission_attempt` and `next_effective_attempt` (needed by the 9.5 guard's empty allowlist). Delegated at HEAD cb5d3568b: `chain.py:837` `_next_retry_attempt_for_stage`, `chain_forecast_execution.py:624` floored mint (skip loop), `chain_forecast_orchestrator_cycle.py:288` and `:667-676` / `:740-749`, `file_journal_retry_helpers.py:51-55`, `file_orchestration_journal.py:12456-12459` (non-master, the last hand-built id site) and `:12678` (manual), `chain_runtime_utils.py:330` `_restart_stage_from_basins`. Deliberately not moved: `_mint_cycle_stage_retry_job_id` (`chain_forecast_execution.py:573`, composition only), `_retry_cycle_stage_job_id:206-210` (claim precedence, late binding), `_retry_attempt_from_basins` (`chain_runtime_utils.py:357`, no `+1`; its judgement already lives in the owner), and the floor-applies binding check (`chain_forecast_execution.py:650-651`). Row L becomes a plain-data row.
- [x] 9.3 Stage attempt reads. Add `stage_attempt(row_attempts, floor=None)` and delegate `scheduler_state_rows.py:517-656` (stage-less, canonical and non-canonical arms) and the `_stage_job_sort_key` parser (`stage_job_sort_attempt`), plus the seven scheduler-side manual `+ 1` sites in `scheduler_state_manual_retry.py` (`next_manual_attempt`, unclamped). Deliberately not moved: `stage_retry_attempt_floors`, `_job_retry_attempt`, and the `_fallback_previous_attempt` max.
- [x] 9.4 Limit and status sets. Recon at 649f4ee8b; delegated in #3323: `retry_identity` now holds `DEFAULT_RETRY_LIMIT`, `MANUAL_RETRYABLE_STATUSES` and `UPSTREAM_REFRESH_RETRYABLE_STATUSES` (frozensets, literal contents). `scheduler_state_types` re-imports the limit (`# noqa: F401`), `RetryConfig.max_retries` defaults to it, and `chain_forecast_orchestrator_cycle.py:236,245` (lines at 649f4ee8b) read the two sets. `tests/status_literal_allowlist.json` follows the two literals to `retry_identity.py`. The `retry.py:65-66` sets are not redirected (#3319).
  - The owner holds the default limit constant; `DEFAULT_RETRY_LIMIT` and `RetryConfig`'s default read it. `SlurmGatewaySettings.max_retries` keeps its literal default (the gateway must not import the orchestrator), and a parity test asserts that it equals the owner constant.
  - `NHMS_SCHEDULER_RETRY_LIMIT` and `SLURM_GATEWAY_MAX_RETRIES` keep their readers.
  - The two hard-coded status sets move to the owner unchanged.
- [x] 9.5 Add the AST guard (design.md D2) with an empty allowlist: `tests/test_retry_accounting_ast_guard.py` fails on:
  - id construction from `_retry_` / `RETRY_JOB_ID_MARKER` (f-string, concatenation, `%`, `.format`, `join`, `+=`);
  - splitting on that marker;
  - adding an integer literal to the result of a `retry_identity` function, of a call whose name contains `attempt`, or of a marker split.

  Unrelated f-strings such as `f"manual_retry_{key}"` (`scheduler_state_manual_retry.py:914`) and membership tests are not flagged.
- [x] 9.6 (after T1, independent of the guard) File the behaviour-change issue (#3319) for the non-master path and the status-set difference, citing the T1 rows it would edit.
- [ ] 9.7 Verification: per PR, CI. The T1 table's expected values are unchanged in every PR. node-22 batch passes per D5.

Suggested fixture level: expanded - persisted retry identity in a shared state machine.
Minimal mergeable slice: 9.1 alone; 9.2, 9.3, 9.4 follow as separate PRs, and 9.5 lands last when no local formula remains.

Slice triage (T2-1, #3265, tasks 9.1, 9.6):
- Issue type: refactor (pure move; no output changes for any input). Fixture level: expanded (agree with the issue's expanded). Two expanded triggers apply. First, persisted or shared state (`issue-risk-contract.md:22`): every id this slice formats becomes a durable `pipeline_job.job_id` in the file journal, and every later suffix parser reads it. Second, the project's `orchestrator`/`pipeline`/state-machine trigger (`openspec/project-profile.md:107`). No new spec delta is needed. The change's `specs/job-retry-mechanism/spec.md:3-21` already ADDs "Retry and attempt accounting SHALL have one owner". That requirement lists "retry job id formatting" (`:8`) and says "Moving a computation into the owner SHALL NOT change its result for any input" (`:20-21`). design.md Appendix D (`:327-340`) has no T2 row. `grep -rnE '_retry_<|_retry_\{|retry job id|retry_job_id' openspec/specs` returns 16 hits (among them `slurm-job-chain/spec.md:345-376`, `job-retry-mechanism/spec.md:350,876,896-898,1009,1042,1079,1790,2098,2173,2184,2724`, `retry-stage-evidence-supersession/spec.md:40`, `orchestrator-structural-burndown/spec.md:569`); every one describes the id shape and none names the formatting function, so all stay true under a pure formatter move. #3284, the UUID scenario at `job-retry-mechanism/spec.md:203,222`, is out of scope: it is spec-only, it predates #3227 and it changes no code. Folding it in would add a MODIFIED delta to a code PR whose fixture says no delta. It may land before or after this slice.
- Governing invariant: the three delegated sites (`chain_runtime_utils.py:448-449`, `chain_forecast_orchestrator_cycle.py:289`, `file_journal_retry_helpers.py:55`) format their ids through `retry_identity.retry_job_id`; `file_orchestration_journal.py:12458-12459` stays hand-built until 9.2 (which formats it unclamped inside `next_after_failure_non_master`), its value pinned by the new `snapshot-negative-unclamped` T1 row. Each delegating site returns byte-identical ids and counts to today for every input, including stacked bases, malformed suffixes and non-positive attempts. The T1 table (`tests/test_retry_accounting_table.py`) stays green with every expected value unchanged.
- Recon at 5570a72b4. These anchors replace 9.1's "four hand-built sites that remain after R4 deletes `retry.py:~286`"; R4 merged in #3316. The sweep was `git grep -nE '_retry_\{|RETRY_JOB_ID_MARKER\}|\+ *"_retry_"|_retry_" *\+' -- services packages apps scripts workers`, widened with `git grep -nE "[\"']_retry_[\"']|_retry_%|_retry_\{\}"`, `git grep -n RETRY_JOB_ID_MARKER`, an untruncated sweep for every string literal containing `_retry_` in `services packages scripts workers infra apps/**/*.py`, and shell/template forms (`_retry_$`, `_retry_{{`, `_retry_%`) repo-wide outside tests, docs and openspec (empty). It found exactly four sites that build a retry id:
  1. `chain_runtime_utils.py:448-449` `_pipeline_retry_job_id(base_job_id, attempt)` → `f"{base_job_id}_retry_{max(int(attempt), 1)}"`. This is the existing formatter and the only site that clamps. Its callers:
     - `chain_forecast_orchestrator_cycle.py:211` (`_retry_cycle_stage_job_id`, row N, after its own `attempt <= 0 → 1` at `:209-210`);
     - `chain_forecast_execution.py:657` (A/E skip-forward loop), through the late-bound wrapper `chain_forecast_execution.py:135-136` → facade alias `chain.py:593`.
  2. `chain_forecast_orchestrator_cycle.py:288-289` (`_schedule_cycle_stage_retry`, current-master branch, row D): `next_attempt = max(retry_count, suffix_attempt, failure_number - 1) + 1`; `next_job_id = f"{base_job_id}{RETRY_JOB_ID_MARKER}{next_attempt}"`. `suffix_attempt` comes from `split_retry_job_identity` and is never below 0, so `next_attempt >= 1` and the clamp is a no-op.
  3. `file_journal_retry_helpers.py:51-55` (`_next_current_master_retry_identity`, row Q): `retry_count = effective_retry_attempt(job_id, current.get("retry_count")) + 1`; `return f"{base_job_id}{RETRY_JOB_ID_MARKER}{retry_count}", retry_count`. `effective_retry_attempt` is never below 0 (`retry_identity.py:69-72`, `_coerce_attempt:155-158`), so the result is at least 1 and the clamp is a no-op. Callers: journal `:12335` (`handle_failed_job` master path) and `:12432` (`schedule_auto_retry` master path).
  4. `file_orchestration_journal.py:12458-12459` (`schedule_auto_retry`, non-master path): `next_retry_count = int(source.get("retry_count") or 0) + 1`; `retry_job_id = f"{source['job_id']}_retry_{next_retry_count}"`. The base is unstripped, so ids stack. The clamp is not a no-op here. `source["retry_count"]` is `_file_retry_job_int(job, "retry_count")` (`file_journal_retry_helpers.py:631-636,675`), which is `int(value or 0)` with no floor. Nothing between `schedule_auto_retry`'s entry (`:12394`) and `:12458` bounds it. The entry `handle_failed_job` (`:12345-12346`) gates only on `should_auto_retry` (`:12304-12305`). `classify_failure` (`retry.py:119-140`) blocks only on `attempt >= retry_limit`, so a negative attempt passes. Predicted from the arithmetic, not yet observed: a caller snapshot with `retry_count = -1` reaches `:12459` and yields `<id>_retry_0` with count 0. A clamped `retry_job_id` would yield `<id>_retry_1`, which also stops matching the persisted `retry_count` 0 (`:12486,12513`). Production producers write `retry_count >= 0` (`schemas/pipeline_job.schema.json:134-137` `minimum: 0`; journal mints are at least 1), but nothing in code enforces it on this path.

  Not id construction (parse or match only), so out of 9.1:
  - `chain.py:843` `prefix = f"{base_job_id}_retry_"` (`startswith` match). This line originally said 9.5 must not flag it. That is superseded by the T2-5 block: the guard as built flags it as `id-fstring`, and 9.2 deleted the site, so HEAD has no such line;
  - `db/migrations/000025_active_manual_retry_guard.sql:23` (`run_id || '_retry_'`): a SQL prefix match, invisible to the Python AST guard;
  - the `"_retry_"` marker literals at `chain_forecast_orchestrator_cycle.py:668,741` (9.2), `chain_runtime_utils.py:457` (9.3) and `scheduler_state_rows.py:886` (`"_retry_" in job_id`);
  - `operator_action_listing_held.py:100` (regex);
  - `scheduler_state_manual_retry.py:914` `f"manual_retry_{key}"` (state key; the task's grep pattern matches it).

  Tests outside the guard's scope: `tests/test_file_orchestration_journal.py:10819` (`f"{RETRY_JOB_ID_MARKER}{attempt}"`) and `apps/frontend/e2e/support/monitoring.mocked.ts:132` (mock fixture).
- Blast radius:
  - `services/orchestrator/retry_identity.py`: new `retry_job_id`. The module stays dependency-free.
  - `chain_runtime_utils.py:448-449`: the body becomes a delegate.
  - `chain_forecast_orchestrator_cycle.py:289`: delegates, and drops `RETRY_JOB_ID_MARKER` from the import at `:88`, which is its only other use. Ruff F401 would flag the leftover import.
  - `file_journal_retry_helpers.py:55`: delegates, and drops `RETRY_JOB_ID_MARKER` from `:47`, its only other use. No test imports `RETRY_JOB_ID_MARKER` from either module (`git grep -n RETRY_JOB_ID_MARKER -- tests` hits only `test_file_orchestration_journal.py:10816`, which imports it from `retry_identity`).
  - `tests/test_retry_accounting_table.py`.
  - The umbrella `tasks.md`: this block, the 9.1/9.6 ticks, 9.1's wording (becomes: three hand-built sites delegated; `file_orchestration_journal.py:12458-12459` deferred to 9.2; the stale `retry.py:~286` reference dropped), and one line added to 9.2 naming `:12458-12459` as the remaining id-formatting site.

  A wrong move would change persisted retry ids. That would split one stage's attempts across two id families, re-mint an occupied id, or let a stacked id lose a layer. Every budget read and suffix parser downstream would then miscount (#2254, #2404, #1845).
- Non-goals:
  - renaming or deleting `_pipeline_retry_job_id`. It is a facade name (`chain.py:593`) with a late-bound wrapper (`chain_forecast_execution.py:135-136`), a `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md:353` row and five direct imports in `tests/test_production_scheduler.py` (`:51955,52298,52399,52882,53990`). That belongs to F1/F2.
  - any attempt formula, including `:288`'s `max(...) + 1`, the helper's `effective + 1`, `:12458`'s `+ 1` and `:209-210`'s local clamp (all 9.2);
  - stage attempt reads and `_stage_job_sort_key` (9.3);
  - limits and status sets (9.4);
  - the AST guard (9.5);
  - unifying the master and non-master paths or the status sets (9.6 files the issue and changes nothing);
  - #3284.
- Sibling surfaces (must stay):
  - every parser of the id shape: `split_retry_job_identity` / `retry_suffix_attempt` / `effective_retry_attempt` (`retry_identity.py:47-72`), `_next_retry_attempt_for_stage` (`chain.py:835-855`), `_stage_job_sort_key` (`chain_runtime_utils.py:452-468`), the `rsplit` increments (`chain_forecast_orchestrator_cycle.py:668-675,741-748`), `scheduler_state_rows.py:886` and `operator_action_listing_held.py:100`;
  - the journal's occupied-id reuse check (`file_orchestration_journal.py:12460-12476`, `_file_auto_retry_job_can_be_reused`), which keys on the exact id string;
  - the master routing at `:12335` / `:12432`;
  - the facade alias and wrapper chain, `chain.py:593` → `chain_forecast_execution.py:135` → `:657`.
- Change (9.1):
  - `retry_identity.retry_job_id(base: str, attempt: int) -> str` returns `f"{base}{RETRY_JOB_ID_MARKER}{max(int(attempt), 1)}"`. This is byte-for-byte `_pipeline_retry_job_id`'s expression with the marker constant in place of the equal literal.
    - The `int()` coercion is kept: `"3"` → `_retry_3`, and `"x"` raises `ValueError` as today.
    - The base is not stripped or normalized, so a stacked base stays stacked.
  - Delegations:
    - `chain_runtime_utils._pipeline_retry_job_id(base_job_id, attempt)` stays a `def` with the same signature, and its body becomes `return retry_job_id(base_job_id, attempt)`. It is kept as a `def`, not a rebinding, so the facade/late-binding surface and `__name__` are unchanged.
    - `chain_forecast_orchestrator_cycle.py:289` → `retry_job_id(base_job_id, next_attempt)`. Equal for every input, since `next_attempt >= 1`.
    - `file_journal_retry_helpers.py:55` → `return retry_job_id(base_job_id, retry_count), retry_count`. Equal, since `retry_count >= 1`.
  - Journal non-master `:12459` (decided: not delegated here): delegating it to the clamped `retry_job_id` changes the result when the snapshot `retry_count <= -1` (see Recon 4). That violates the governing invariant and the ADDED requirement's "for any input".
    - Decided (orchestrator, option a): leave `:12458-12459` as is in this PR, and record the deviation from the issue's "four sites" in the PR body. 9.2 already lists `file_orchestration_journal.py:12455-12459` and moves the count and the id together into `next_after_failure_non_master`, which formats inside the owner with today's unclamped arithmetic. The 9.5 guard (empty allowlist) then catches the line if 9.2 misses it. This PR adds the negative-snapshot T1 row (Tests) so neither 9.2 nor a later clamp can change the value silently.
- Tests (`tests/test_retry_accounting_table.py`):
  - Retarget `test_pipeline_retry_job_id` (`:317-327`; ids `attempt-0-clamped`, `attempt-negative-clamped`, `attempt-1`, `attempt-3`) to call `retry_identity.retry_job_id("base", attempt)`, with expected values `base_retry_1` / `base_retry_1` / `base_retry_1` / `base_retry_3` unchanged. Update the section-2 header (`:313`) and the module docstring (`:12`).
  - Add direct rows for `retry_job_id`:
    - stacked base kept: `("X_retry_1", 2)` → `X_retry_1_retry_2`;
    - no stripping: `("X_retry_3", 1)` → `X_retry_3_retry_1`;
    - int coercion: `("base", "3")` → `base_retry_3`;
    - large attempt: `("base", 88)` → `base_retry_88`;
    - a non-int string raises `ValueError` (`("base", "x")`).
  - Add a delegate-parity row: `_pipeline_retry_job_id("base", a) == retry_job_id("base", a)` for `a` in `{-1, 0, 1, 3}`. This keeps the facade name pinned without a second literal table.
  - The rows that prove delegation keep calling their enclosing functions, with literal values unchanged:
    - D: `test_d_schedule_cycle_stage_retry_failure_number_formula`, 4 rows;
    - Q: `test_q_next_current_master_retry_identity`, 5 rows;
    - A/E: `test_ae_floored_cycle_stage_retry_job_id`, through `_pipeline_retry_job_id`;
    - N: `test_n_retry_cycle_stage_job_id_precedence`, through `_pipeline_retry_job_id`;
    - the master half of `test_master_and_non_master_paths_diverge_on_the_same_input`.
  - add a negative-snapshot row (pytest id `snapshot-negative-unclamped`) to `test_journal_non_master_auto_retry_reads_the_caller_snapshot_and_stacks`: durable 0, snapshot `-1`, expected `("job_1_retry_1_retry_0", 0)`. This is the arithmetic prediction. Pin the value or error observed on unchanged code first; if the journal rejects the id or count downstream, pin that error. This adds a row and changes no expected value.
- Spec: no delta, with the grep evidence under "Issue type". Tick 9.1 in `tasks.md` (and 9.6 once the issue is filed) in the final push. add one line to 9.2 naming `:12458-12459` as the remaining id-formatting site.
- Must preserve:
  - every T1 expected value (`git diff master -- tests/test_retry_accounting_table.py` shows only retargets and added rows, no edited literal);
  - every persisted id byte-for-byte: flat, stacked and clamped;
  - `_pipeline_retry_job_id`'s name, signature, facade alias and late-binding wrapper;
  - the five `test_production_scheduler.py` direct pins (`_retry_88`, `_retry_1`);
  - `retry_identity.py` staying import-free of orchestrator modules.
- Risk packs:
  - Selected:
    - Legacy compatibility / examples: persisted ids and the facade name. Covered by T1 unchanged, a format mutation turning every delegating row red, delegate parity, and `_pipeline_retry_job_id` kept.
    - Concurrency / shared state / ordering: the id is the journal's occupancy key and identity across concurrent passes, rows E and K. Covered by the E rows and the K index pin, unchanged.
  - Not selected:
    - Public API (no route or OpenAPI change);
    - Config (no env or default change; limits are 9.4);
    - File IO (journal writes unchanged);
    - Schema (`retry_count` and `job_id` columns unchanged);
    - Auth;
    - Resource limits;
    - Error handling (the `ValueError` on a non-int attempt is pinned unchanged);
    - Release / packaging (no dependency change);
    - Documentation (the inventory row `:353` stays true: the owner of `_pipeline_retry_job_id` is still `chain_runtime_utils`).
- Required evidence:
  1. Baseline, before editing production. `retry_job_id` does not exist yet, so:
     - run the new direct inputs (stacked base, no stripping, `"3"`, `88`, `"x"`) against `_pipeline_retry_job_id` and record today's values, then retarget them;
     - add the negative-snapshot journal row as is;
     - run `uv run pytest -q tests/test_retry_accounting_table.py` on unchanged code. Expected: green (162 + new rows).
  2. After the change, the same command stays green. `git diff master -- tests/test_retry_accounting_table.py` shows no edited expected literal; list every changed line as retarget or added.
  3. Format mutation: in `retry_identity.retry_job_id`, set the marker to `"_retryX_"`. Expected red:
     - the 4 retargeted `test_pipeline_retry_job_id` rows and every direct row except the `ValueError` row;
     - D (4) and Q (5);
     - A/E: the 6 rows whose expected value is a minted id (`A-floor-plus-one`, `A-cohort-max-floor`, `A-none-job-id-with-floor`, `A-suffix-equal-floor`, `E-snapshot-occupied-skips`, `E-journal-and-snapshot-occupied-skip`);
     - N (all 6);
     - `test_master_and_non_master_paths_diverge_on_the_same_input`.

     Expected green: the A pass-through rows (`A-no-floor-unchanged`, `A-no-floor-none-stays-none`, `A-suffix-above-floor`), the delegate-parity rows (both sides see the mutation) and the 3 journal non-master rows plus the new negative row. The journal rows staying green proves `:12459` was not delegated.

     Run a second mutation that drops the clamp in `retry_job_id`. Expected red: only `attempt-0-clamped` and `attempt-negative-clamped`. `N-negative-clamped` stays green because `:209-210` clamps first, as T1's `mutations.log:196-199` RC2 equivalent already shows. Restore the file byte-for-byte (hash).
  4. Residual grep: `git grep -nE '_retry_\{|RETRY_JOB_ID_MARKER\}|\+ *"_retry_"|_retry_" *\+' -- services packages apps scripts workers` must show exactly `retry_identity.py` (the `retry_job_id` line), `scheduler_state_manual_retry.py:914` (`manual_retry_{key}`, not an id) and `file_orchestration_journal.py:12459`. Nothing else.
  5. Full local suite green (proxy env unset); `uv run ruff check . --no-cache`; `openspec validate redesign-orchestration-core --strict --no-interactive`; CI full suite, every shard.
  6. node-27, per D5 "plus node-27 real-DB suites when those suites import the code". R4's 5b set was chosen for the display route, which this slice does not touch; the touched modules here are on the scheduler side. Run `-m integration tests/test_qhh_production_bootstrap_scheduler.py tests/test_real_database_integration.py` on throwaway `nhms_it_*` DBs. The first imports `ProductionScheduler` and, through it, chain and the journal; the second imports `apps.api.main`, so its transitive reach is not ruled out. `tests/test_orchestration_chain.py` no longer carries an integration marker at HEAD. Expected: all pass, with 0 leftover DBs.
  7. node-22 (D5, pure move; this is task 9.7's cadence): ff deploy at a pass boundary, then one idle pass (`status=planned`, `restart_reconcile.status=completed`, `lock.lock_type=file`) and one submitting pass (`status=submitted`), in a batch of at most three merged pure-move PRs or one calendar day. 9.1 and 9.6 are ticked in the final push, with no trailing docs commit. Until the batch has run, the PR merges with `Part of #3265`.
  8. Task 9.6: the behaviour-change issue is filed, citing the T1 test names it would edit (draft in `.workplans/3265/issue-9.6-draft.md`), and its URL is in the PR body.
- Review focus:
  1. Every delegated site is equal for all inputs, with the clamp proven a no-op at `:289` and `:55` by the lower bounds cited above, and the `int()` that `retry_job_id` adds proven a no-op there too (`next_attempt` is built from `int(retry_count)`, a `split_retry_job_identity` int and the int `failure_number` both callers pass, `chain_forecast_execution.py:327-330,944-946`; `effective_retry_attempt` returns an int). The journal `:12459` decision is applied as recorded, and its row pins today's negative-snapshot value.
  2. `retry_job_id` keeps `int()` and does not strip the base.
  3. No T1 literal is edited; retargets and additions only.
  4. `_pipeline_retry_job_id` keeps its name, signature and facade/late-binding surface.
  5. No unused `RETRY_JOB_ID_MARKER` import is left behind, and nothing outside the three sites changes.

Slice triage (T2-2, #3266, task 9.2):
- Issue type: refactor (a pure move: no output changes for any input). Fixture level: expanded, agreeing with the issue. The same two expanded triggers as T2-1 apply. First, persisted or shared state (`issue-risk-contract.md:22`): every attempt and id this slice computes becomes a durable `pipeline_job.job_id` / `retry_count` / `submission_attempt`, or the journal's occupancy key. Second, the project's orchestrator/state-machine trigger (`openspec/project-profile.md:107`).
  - Minimal mergeable slice: atomic (agree). The pieces are separable. `retry_job_id` already landed in 9.1, so no function depends on another's PR.
  - A split buys no verification, because each owner function has its own rows and its own mutation. It costs D5 batch slots (at most three pure-move PRs per node-22 batch).
  - The clean cut, if the orchestrator wants one, is `restart_stage_from_basins` plus row L as a separate PR. It shares no helper and no rows with the attempt functions. The journal non-master id site should not be split off: it is the last hand-built id and the target of both carry-over mutations.
  - Recommendation: one PR.
- Governing invariant: every attempt formula named in Change is computed by one `retry_identity` function, and each delegating site returns the same value, or raises the same exception, as today for every input. That includes stacked bases, malformed, negative or empty suffixes, `None` ids, non-int counts and non-positive claims.
  - The master/non-master divergence and the rsplit-vs-`retry_suffix_attempt` difference are kept as two named code paths.
  - `retry_identity.py` stays import-free of orchestrator modules.
  - The T1 table stays green with every expected value unchanged.
- Recon at HEAD cb5d3568b. These anchors replace 9.2's (which are at 6bf97fdc8); the T1/T2-1 anchors for `chain_runtime_utils` are off by one (`:330`, `:357`, `:449` at HEAD).
  1. Stage-scoped mint, `chain.py:837-857` `_next_retry_attempt_for_stage(jobs, *, base_job_id, stage)`.
     - `prefix = f"{base_job_id}_retry_"` (`:843`).
     - For each job: `ForecastOrchestrator._job_matches_stage(job, stage)` (`:846`) runs first, then `str(job.get("job_id") or "")`, then `startswith(prefix)`, then `retry_suffix_attempt(job_id)` kept if `> 0`.
     - Returns `max(attempts, default=0) + 1` (`:857`).
     - Callers: `chain_forecast_orchestrator_cycle.py:206` and `chain_forecast_execution.py:598`, both late-bound as `_chain._next_retry_attempt_for_stage`. The direct test imports are `tests/test_orchestration_chain.py:14157,14332,14348`.
     - Pinned by T1 M (`test_m_next_retry_attempt_for_stage`, 9 rows), N fall-through (`N-zero-falls-through`, `N-absent-falls-through`, `N-absent-no-rows`) and index I.
  2. N precedence, `chain_forecast_orchestrator_cycle.py:196-211` `_retry_cycle_stage_job_id`.
     - `attempt = context.retry_attempt or _chain._next_retry_attempt_for_stage(...)` (`:206-208`). This short-circuits: the stage scan runs only on a falsy claim.
     - Then `if attempt <= 0: attempt = 1` (`:209-210`), then `_chain._pipeline_retry_job_id(base_job_id, attempt)` (`:211`).
     - Class-patched at `tests/test_orchestration_chain.py:10116,10153`.
     - T1 N (6 rows).
     - Not in 9.2's list (see Change, "Deliberately not moved").
  3. Floored mint, `chain_forecast_execution.py:624-660` `_floored_cycle_stage_retry_job_id(self, context, stage, existing_jobs, job_id)`.
     - `floors = [floor for basin in context.active_basins if (floor := retry_attempt_floor(basin.get("state_evidence"), stage.stage)) is not None]` (`:645-649`).
     - `if not floors or (job_id is not None and retry_suffix_attempt(job_id) > max(floors)): return job_id` (`:650-651`).
     - Only after that: `base_job_id = _pipeline_job_id(...)`, `occupied = {str(job.get("job_id") or "") for job in existing_jobs}` and `get_pipeline_job = getattr(self.repository, "get_pipeline_job", None)` (`:652-654`).
     - `attempt = max(floors) + 1` (`:655`). The loop formats `candidate = _pipeline_retry_job_id(base_job_id, attempt)` and returns it if `candidate not in occupied and not (callable(get_pipeline_job) and get_pipeline_job(candidate) is not None)`; otherwise `attempt += 1` (`:656-660`).
     - The snapshot is checked before the journal: `get_pipeline_job` is not called for a snapshot hit.
     - `_pipeline_retry_job_id` here is the late-bound wrapper `:135-136`, which goes to the facade `chain.py:593`.
     - Callers: `:286` (`job_id=None`) and `:580`.
     - T1: A/E (`test_ae_floored_cycle_stage_retry_job_id`, 9 rows) plus index A-limit and B.
  4. `chain_forecast_execution.py:571-580` `_mint_cycle_stage_retry_job_id`: `job_id = self._retry_cycle_stage_job_id(...)`; `return _floored_cycle_stage_retry_job_id(..., job_id) or job_id`.
     - Composition only, no formula. Nothing moves.
     - Carry-over (#3318): its pins (index A-limit `tests/test_retry_mint_floor.py::test_prefix_switch_does_not_reset_the_minted_attempt_and_the_budget_blocks_on_time`, `::test_failure_lane_prefix_switch_stays_within_the_limit`; B `::test_cohort_digest_changes_do_not_escape_the_budget`, `::test_failure_lane_cohort_digest_change_stays_within_the_limit`) must be green.
  5. Inline (D), `chain_forecast_orchestrator_cycle.py:248-290` `_schedule_cycle_stage_retry`.
     - `retry_count = int(getattr(job, "retry_count", 0) or 0)` (`:264`; also feeds the backoff at `:265`, so it stays).
     - Current-master branch: `base_job_id, suffix_attempt = split_retry_job_identity(result.pipeline_job_id)` (`:287`), `next_attempt = max(retry_count, suffix_attempt, failure_number - 1) + 1` (`:288`), `next_job_id = retry_job_id(base_job_id, next_attempt)` (`:289`).
     - `suffix_attempt >= 0` (split floors at 0), so `next_attempt >= 1` and `retry_job_id`'s clamp is a no-op. This is the carry-over floor.
     - T1 D (4 rows); `tests/test_retry_mint_floor.py::test_inline_auto_retry_stacks_on_the_floored_id_and_never_under_mints`.
  6. Reservation increments, `chain_forecast_orchestrator_cycle.py:667-676` (forecast cohort) and `:740-749` (forcing array). They are byte-identical:
     ```python
     submission_attempt = max(int(context.retry_attempt or 1), 1)
     retry_marker = "_retry_"
     if retry_marker in pipeline_job_id:
         try:
             submission_attempt = max(submission_attempt, int(pipeline_job_id.rsplit(retry_marker, maxsplit=1)[1]) + 1)
         except ValueError:
             pass
     ```
     - A non-int claim (`"x"`) raises `ValueError` before the `try`. A non-int tail is swallowed.
     - A negative tail gives `max(>=1, tail+1)`, which is the claim. It differs from `retry_suffix_attempt`, but the difference is unobservable through the outer `max` (T1 note).
     - T1: `test_forecast_reservation_submission_attempt_increment` and `test_forcing_reservation_submission_attempt_increment` (7 rows each).
  7. Journal current master, `file_journal_retry_helpers.py:51-55` `_next_current_master_retry_identity(current)`.
     - `job_id = str(current.get("job_id") or "")`; `base_job_id, _ = split_retry_job_identity(job_id)`; `retry_count = effective_retry_attempt(job_id, current.get("retry_count")) + 1`; `return retry_job_id(base_job_id, retry_count), retry_count`.
     - `effective_retry_attempt >= 0` (`_coerce_attempt:164-168`, split floor), so the clamp is a no-op. This is the carry-over floor.
     - Callers: journal `:12335` (`handle_failed_job`) and `:12432` (`schedule_auto_retry` master branch). Both call the helper and need no edit.
     - The name is re-exported at journal `:250,716`, listed in `tests/fixtures/file_journal_cluster_map.json:322`, and imported by `tests/test_file_orchestration_journal.py:8645` and `tests/test_gateway_reconcile_master_transitions.py:382`.
     - T1 Q (`test_q_next_current_master_retry_identity`, 5 rows) and the master half of `test_master_and_non_master_paths_diverge_on_the_same_input`; `test_file_orchestration_journal.py::test_file_auto_retry_master_virtual_row_renders_public_lineage` (`:17201`, carry-over #3318) and `::test_next_current_master_retry_identity_is_stable_after_helper_consolidation` (`:8644`).
  8. Journal non-master, `file_orchestration_journal.py:12458-12459` (`schedule_auto_retry`).
     - `next_retry_count = int(source.get("retry_count") or 0) + 1`; `retry_job_id = f"{source['job_id']}_retry_{next_retry_count}"`.
     - The id is unstripped (stacks) and unclamped.
     - `source = _file_retry_job_record(job)` (`file_journal_retry_helpers.py:639-676`). It always carries a `str` `job_id` (`_safe_identity_text`) and an `int` `retry_count` (`_file_retry_job_int`, `int(value or 0)` with no floor), so neither the subscript nor `int()` can raise here, and swapping their evaluation order is unobservable.
     - The local name `retry_job_id` is assigned in `schedule_auto_retry` at `:12432` and `:12459`, and in `handle_failed_job` at `:12335`. The journal does not import `retry_identity.retry_job_id` today (`:292-297`).
     - T1: `test_journal_non_master_auto_retry_reads_the_caller_snapshot_and_stacks` (4 rows, incl. `snapshot-negative-unclamped` → `("job_1_retry_1_retry_0", 0)`) and the non-master half of the diverge test.
  9. Journal manual, `file_orchestration_journal.py:12678`: `next_retry_count = effective_retry_attempt(failed_job["job_id"], failed_job.get("retry_count")) + 1`.
     - `failed_job["job_id"]` can raise `KeyError`; that stays at the site.
     - T1 has an index row only (journal-manual → `tests/test_file_orchestration_journal.py::test_file_journal_manual_repair_suffix_only_target_emits_next_durable_attempt`, `:3401`). There is no plain-data row.
  10. Cohort restart, `chain_runtime_utils.py:330-354` `_restart_stage_from_basins(basins)`.
      - Per basin, `_canonical_restart_stage(basin.get("restart_stage"))` (`:423-433`: `None`/`""` → `None`; aliases `parse_output→parse`, `publish_tiles→publish`; anything outside `{s.stage for s in STAGES}` → `None`). Non-`None` values are collected.
      - Empty → `None`. Otherwise `stage_order = {stage.stage: index for index, stage in enumerate(STAGES)}` and `min(restart_stages, key=lambda s: stage_order.get(s, len(stage_order)))`.
      - `STAGES` = `('convert','forcing','forecast','parse','state_save_qc','publish')`.
      - Facade alias `chain.py:585`, late-bound wrapper `chain_forecast_control.py:56-57` (called at `:148`), in-module callers `:116,161`, inventory row `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md:345`.
      - Pins: index L `tests/test_chain_cross_stage_state.py::test_restart_stage_from_basins_takes_earliest_marker_and_ignores_markerless_members` (`:144`) and siblings `:52,86,111,119`; `tests/test_production_scheduler.py:61097`.
  11. Claim read, `chain_runtime_utils.py:357-393` `_retry_attempt_from_basins(basins)`.
      - Takes the max of the positive claims from `retry_attempt` / `manual_retry_attempt` (`_coerce_positive_int`, `:396`, facade alias `chain.py:587`), plus the evidence claim gated by `has_active_manual_retry_decision` with a log via `log_ignored_manual_retry_attempt_claim`. Both of those are already in the owner.
      - No `+ 1`, no mint.
      - T1 N (`test_n_retry_attempt_from_basins`, 8 rows).
- Blast radius:
  - Files changed:
    - `services/orchestrator/retry_identity.py` (new functions; still dependency-free);
    - `chain.py` (`:837-857` body; the `:95` import);
    - `chain_forecast_execution.py` (`:645-660` body; the `:32-39` import);
    - `chain_forecast_orchestrator_cycle.py` (`:288`, `:667-676`, `:740-749`; the `:86-91` import);
    - `file_journal_retry_helpers.py` (`:47,51-55`);
    - `file_orchestration_journal.py` (`:12458-12459`, `:12678`, the `:292-297` import);
    - `chain_runtime_utils.py` (`:330-354` body; the `:28-32` import);
    - `tests/test_retry_accounting_table.py`;
    - the umbrella `tasks.md` (this block, 9.2 tick, 9.2 wording).
  - Imports that go unused and must be dropped (F401):
    - `retry_suffix_attempt` at `chain.py:95` (its only use is `:854`);
    - `effective_retry_attempt` / `split_retry_job_identity` / `retry_job_id` at `file_journal_retry_helpers.py:47` (only uses `:53-55`);
    - Kept on purpose: `retry_suffix_attempt` at `chain_forecast_execution.py:38` (the binding check `:650` stays, Change 2), and `retry_job_id` / `split_retry_job_identity` at `chain_forecast_orchestrator_cycle.py:89-90` (`:287,289` stay).
    - Check each with `git grep -n` before deleting. Nothing imports these names from `file_journal_retry_helpers` (`git grep "file_journal_retry_helpers import"` hits only `_next_current_master_retry_identity`).
  - Facade guard: `chain.py` is a governed facade (`scripts/governance/entropy_audit/schema.py:214`). A new `from services.orchestrator.retry_identity import <fn>` there is a new imported symbol, which raises a `compatibility-facade-growth.new-facade-reexport` signal (`facade_guard.py:110-125`). A module import (`from services.orchestrator import retry_identity` plus an attribute call) is excluded (`_compatibility_imported_name_is_module`). `chain_runtime_utils.py` and the others are not governed.
  - A wrong move would change a persisted id, `retry_count` or `submission_attempt`. It could split one stage's attempts across id families, re-mint an occupied id, lose a stacked layer, or flip a cohort's restart stage. Every budget read and suffix parser downstream would then miscount (#2254, #2404, #1845, #2416).
- Non-goals:
  - any T1 expected value;
  - unifying master and non-master, or the rsplit increments with `retry_suffix_attempt` (D2; #3319 owns behaviour changes);
  - stage attempt reads and `_stage_job_sort_key` (9.3);
  - limits and status sets (9.4);
  - the AST guard (9.5);
  - renaming or deleting any delegate, facade alias or wrapper (F1/F2);
  - `_canonical_restart_stage` (shared with `chain_runtime_utils.py:226,245`; stays);
  - #3284;
  - the out-of-scope attempt arithmetic listed under Sibling surfaces (report, don't fix).
- Sibling surfaces (must stay):
  - parsers: `split_retry_job_identity` / `retry_suffix_attempt` / `effective_retry_attempt` / `_coerce_attempt` (`retry_identity.py:56-81,164-168`), with their 0 floors unchanged (carry-over #3320);
  - `ForecastOrchestrator._job_matches_stage` (`chain_forecast_orchestrator_cycle.py:1038`);
  - the journal occupancy check `:12460-12476` (keys on the exact id string);
  - the late-binding chains:
    - `chain.py:585,587,593`;
    - `chain_forecast_execution.py:135-136` → `:657`;
    - `chain_forecast_control.py:56-61`;
    - `_chain._next_retry_attempt_for_stage` at `cycle:206` / `execution:598`;
    - `_chain._pipeline_retry_job_id` at `cycle:211`;
  - the cluster map (`tests/test_file_journal_cluster_map.py`): no top-level def is added or removed in `file_orchestration_journal.py` or `file_journal_retry_helpers.py`; imports are bindings and need no map entry.
  - Out-of-scope attempt arithmetic found by the sweep. It is not in 9.2's list, matches no 9.5 guard pattern, and is reported here, not moved:
    - `scheduler_state_manual_retry.py:122,132` (`default_attempt=_state_retry_attempt(state) + 1`, the scheduler-side manual new attempt's default; decided: 9.3 (#3267), same family as the next item);
    - `scheduler_state_manual_retry.py:1021-1039` (`_fallback_previous_attempt(...) + 1`, the scheduler-side manual new attempt; arguably a "manual path" under spec `:10`; decided: 9.3 (#3267) picks it up with the scheduler-side attempt reads);
    - `chain_forecast_execution.py:866-876` (`attempt_floor - 1 > job.retry_count` → `job.retry_count = attempt_floor - 1`);
    - `chain_forecast_execution.py:327,944` (`retry_attempts += 1`, the failure counter, i.e. `failure_number`);
    - `file_orchestration_journal.py:3394-3399` (reclaim `submission_attempt` `+ 1`);
    - `file_orchestration_journal.py:11374,11513,11542` (event sequence counters, not attempts).
- Change (9.2). All of these are plain-data functions in `retry_identity.py`. Expressions are moved verbatim, with `"_retry_"` replaced by the equal `RETRY_JOB_ID_MARKER`.
  1. `next_stage_attempt(job_ids: Iterable[Any], base_job_id: str) -> int`.
     - `prefix = f"{base_job_id}{RETRY_JOB_ID_MARKER}"`; for each `job_id`, `text = str(job_id or "")`; keep `retry_suffix_attempt(text)` when `text.startswith(prefix)` and it is `> 0`; return `max(attempts, default=0) + 1`.
     - Delegate: `chain.py` `_next_retry_attempt_for_stage` stays a `def` with the same signature. Its body becomes two statements, `job_ids = (job.get("job_id") for job in jobs if ForecastOrchestrator._job_matches_stage(job, stage))` then `return retry_identity.next_stage_attempt(job_ids, base_job_id)`, so the facade guard does not classify it as a forwarding function (`scripts/governance/entropy_audit/facade_guard.py:454-466` treats a single `return <module alias>.f(...)` as forwarding, and a non-forwarding→forwarding change emits `new-monkeypatch-alias`, `:147-170`); laziness and filter-before-`.get` order are unchanged.
       - The generator keeps today's per-job order: filter, then `.get`.
       - Import the module (`from services.orchestrator import retry_identity`), not the symbol, so the facade guard sees no new re-export.
       - The stage filter stays at the site. It is chain topology and is class-patchable, not accounting.
  2. `mint_attempt(floors: Sequence[int], is_occupied: Callable[[int], bool]) -> int`: `attempt = max(floors) + 1; while is_occupied(attempt): attempt += 1; return attempt`. The precondition is that `floors` is non-empty; the site guarantees it.
     - Delegate (`_floored_cycle_stage_retry_job_id`):
       - The `floors` comprehension and the binding check `:650-651` stay at the site. The check is a parse and comparison, which spec `:64` allows.
       - The setup `:652-654` stays after the check.
       - The loop becomes `attempt = mint_attempt(floors, lambda a: (c := _pipeline_retry_job_id(base_job_id, a)) in occupied or (callable(get_pipeline_job) and get_pipeline_job(c) is not None))`, then `return _pipeline_retry_job_id(base_job_id, attempt)`.
       - The predicate is `not` of today's return test, with the same short-circuit: the snapshot first, and the journal only on a snapshot miss.
       - Formatting stays behind the late-bound `_pipeline_retry_job_id` (a T2-1 sibling surface). That adds one extra pure call for the returned id.
     - Rejected alternative: move the binding check into the owner. The setup `:652-654` would then run before the check, so a pass-through input could reach `context.run_id` / `self.repository` where today it returns first. That is not a pure move.
  3. `inline_retry_attempt(retry_count: int, suffix_attempt: int, failure_number: int) -> int` = `max(retry_count, suffix_attempt, failure_number - 1) + 1`.
     - Delegate: `chain_forecast_orchestrator_cycle.py:288`.
     - `:287` and `:289` stay as they are.
  4. `reservation_submission_attempt(pipeline_job_id: str, retry_attempt: Any) -> int`: the `:667-676` block verbatim (the `int(retry_attempt or 1)` raise preserved, `in` on a non-str raises as today, the `ValueError` swallowed only around the tail).
     - Delegates: `:667-676` and `:740-749` each become `submission_attempt = reservation_submission_attempt(pipeline_job_id, context.retry_attempt)`.
     - The name is not in D2's list. It is needed because the 9.5 guard flags `rsplit(...) + 1` with an empty allowlist, and D2 names no function for these sites. Recorded as a deviation.
  5. `next_effective_attempt(job_id: Any, recorded_count: Any) -> int` = `effective_retry_attempt(job_id, recorded_count) + 1`.
     - Delegate: the journal manual path `:12678` → `next_effective_attempt(failed_job["job_id"], failed_job.get("retry_count"))`.
     - The `effective_retry_attempt` import stays, because `:6438` uses it.
     - The name is not in D2's list. Master and manual already compute this identical expression, and the comment at `:12676-12677` names the master helper as the same derivation. Sharing it changes no output and is not D2's rejected unification (master vs non-master stay separate). Alternative: `next_after_failure_master(...)[1]`, which is equal for all inputs but misnamed for the manual path. Decided: `next_effective_attempt`.
  6. `next_after_failure_master(job_id: Any, recorded_count: Any) -> tuple[str, int]`: `text = str(job_id or ""); base, _ = split_retry_job_identity(text); count = next_effective_attempt(text, recorded_count); return retry_job_id(base, count), count`.
     - `effective_retry_attempt` re-applies `str(x or "")`, which is idempotent on `text`.
     - Delegate: `_next_current_master_retry_identity(current)` stays a `def` with the same signature in `file_journal_retry_helpers.py`. Its body becomes `return next_after_failure_master(current.get("job_id"), current.get("retry_count"))`.
     - Journal `:12335` and `:12432` are unchanged, because they already call the helper.
  7. `next_after_failure_non_master(job_id: Any, retry_count: Any) -> tuple[str, int]`: `count = int(retry_count or 0) + 1; return f"{job_id}{RETRY_JOB_ID_MARKER}{count}", count`.
     - Unstripped and unclamped. It does not use `retry_job_id` or `_coerce_attempt`.
     - Delegate: `:12458-12459` → `retry_job_id, next_retry_count = next_after_failure_non_master(source["job_id"], source.get("retry_count"))`.
     - Shadowing carry-over (#3320): the journal imports `next_after_failure_non_master` and `next_effective_attempt`, never `retry_job_id`, so the three locals (`:12335,12432,12459`) shadow nothing and stay as they are. If an implementer does import `retry_job_id` into the journal, the locals must be renamed first.
  8. `restart_stage_from_basins(restart_stages: Iterable[str | None], stage_order: Sequence[str]) -> str | None`: `stages = [s for s in restart_stages if s is not None]`; empty → `None`; `order = {name: i for i, name in enumerate(stage_order)}`; `min(stages, key=lambda s: order.get(s, len(order)))`.
     - Use `len` of the dict, as today, not of the sequence.
     - Delegate: `_restart_stage_from_basins(basins)` stays a `def`. Its body becomes `return restart_stage_from_basins((_canonical_restart_stage(basin.get("restart_stage")) for basin in basins), tuple(stage.stage for stage in STAGES))`.
     - Canonicalization stays at the site, because the owner must not import `chain_stages` (spec `:18-20`, plain data only).
  - Deliberately not moved, each recorded in the PR body and in 9.2's wording:
    - `_mint_cycle_stage_retry_job_id` (`:571-580`): composition only.
    - `_retry_cycle_stage_job_id` `:206-210`:
      - no guard-shaped expression;
      - moving the `or` either makes the stage scan eager, which changes which inputs reach it, or drops the late-bound `_chain._next_retry_attempt_for_stage`;
      - no T1 row discriminates its `<= 0 → 1` clamp: it and `retry_job_id`'s clamp mask each other (T1 RC2 equivalent mutant, `.workplans/3264/mutations.log:196-201`), so it stays at the site untouched.
    - `_retry_attempt_from_basins` (`chain_runtime_utils.py:357`):
      - it mints nothing (no `+ 1`);
      - its #1201 judgement and log already live in the owner;
      - moving it would need an owner name D2 does not list and would relocate or duplicate `_coerce_positive_int` (a facade alias at `chain.py:587`);
      - decided: not moved (a verbatim move as `retry_attempt_from_basins` was the rejected alternative).
    - The binding check `chain_forecast_execution.py:650-651` (Change 2).
- Tests (`tests/test_retry_accounting_table.py`). The rule: no expected literal is edited. Every new direct row's expected value is first observed on unchanged code through the enclosing function, so only inputs reachable through a site are used.
  - Retarget (as T2-1 did for `_pipeline_retry_job_id`): `test_q_next_current_master_retry_identity` (5 rows, `:296-311`) calls `retry_identity.next_after_failure_master(current["job_id"], current.get("retry_count"))`, with expected values unchanged. Add a delegate-parity row: `_next_current_master_retry_identity(c) == next_after_failure_master(c.get("job_id"), c.get("retry_count"))` over the same 5 inputs. That keeps the helper name pinned without a second literal table.
  - Kept on their enclosing function, unchanged, each with the reason it cannot retarget:
    - M (9, `_chain._next_retry_attempt_for_stage`): the stage filter (`M-other-stage-row-excluded`, `M-job-type-match`) stays at the site, so the rows must pass through it.
    - N (6, `_retry_cycle_stage_job_id`): its precedence stays at the site.
    - A/E (9, `_floored_cycle_stage_retry_job_id`): stub-driven. The binding check and probe stay at the site.
    - D (4, `_schedule_cycle_stage_retry`): stub-driven (the routing, `split` and the formatting stay).
    - Both increment tests (14, `_reserve_cycle_stage`): stub-driven. They observe `submission_attempt` in the reservation evidence.
    - Journal non-master (4) and the diverge test: `tmp_path` journal-driven. They prove the site passes the caller snapshot.
    - N claim (8, `_retry_attempt_from_basins`): not moved.
  - New direct rows:
    - `next_stage_attempt`:
      - `(["B_retry_1_retry_2"], "B")` → 3;
      - `(["B_retry_2", "B_retry_5"], "B")` → 6;
      - `(["B_retry_abc"], "B")` → 1;
      - `([None, ""], "B")` → 1;
      - `(["C_retry_4"], "B")` → 1;
      - `(["B_retry_0"], "B")` → 1;
      - `(["B_retry_-2"], "B")` → 1;
      - `([], "B")` → 1.
    - `mint_attempt` (the predicate records its calls):
      - `([1], ∅)` → 2;
      - `([1, 3], ∅)` → 4;
      - `([0], ∅)` → 1;
      - `([2], {3})` → 4;
      - `([2], {3, 4})` → 5, with calls `[3, 4, 5]`.
    - One new A/E-style row on `_floored_cycle_stage_retry_job_id`: floors `[2]`, snapshot `_retry_3`, journal stub recording lookups → `_retry_4`, and `get_pipeline_job` called only with `_retry_4` (snapshot-before-journal).
    - `inline_retry_attempt`:
      - `(5, 1, 1)` → 6;
      - `(0, 4, 1)` → 5;
      - `(0, 0, 7)` → 7;
      - `(2, 2, 1)` → 3;
      - `(0, 0, 0)` → 1.
    - `reservation_submission_attempt`, one row per `_INCREMENT_ROWS` input on `f"{FORECAST_BASE}{suffix}"`:
      - same expected values: 1/3/3/1/4/6/3;
      - plus `("X_retry_-3", None)` → 1;
      - plus `("X", "x")` raises `ValueError`.
    - `next_effective_attempt`:
      - `("X_retry_3", 0)` → 4;
      - `("X_retry_1", 5)` → 6;
      - `("X", "bad")` → 1;
      - `(None, None)` → 1.
    - `next_after_failure_non_master`:
      - `("job_1_retry_1", 1)` → `("job_1_retry_1_retry_2", 2)`;
      - `("job_1_retry_1", -1)` → `("job_1_retry_1_retry_0", 0)`;
      - `("job_1_retry_1", 0)` → `("job_1_retry_1_retry_1", 1)`.
  - Journal manual (`:12678`): the direct `next_effective_attempt` rows plus the existing index pin. The index row stays, since a file-journal manual retry needs a full repository.
  - Row L becomes plain data. This is the one sanctioned non-additive edit; the authority is D2 ("row L becomes a plain-data row") and the change's spec scenario `specs/job-retry-mechanism/spec.md:23-39`, which lists the cohort restart stage among the plain-data rows, not the index rows:
    - remove the `"L"` entry from `INDEX` and from the `test_index_rows_are_complete` literal list;
    - add `test_l_restart_stage_from_basins` (owner, plain data, `STAGE_NAMES = ('convert','forcing','forecast','parse','state_save_qc','publish')`):
      - `(["parse", None, "forecast"])` → `"forecast"`;
      - `(["forecast"])` → `"forecast"` (ordinary restart kept);
      - `(["publish", "convert"])` → `"convert"`;
      - `([None, None])` → `None`;
      - `([])` → `None`;
    - add a delegate row set on `_restart_stage_from_basins`:
      - `[{"restart_stage": "parse"}, {}, {"restart_stage": "forecast"}]` → `"forecast"`;
      - `[{"restart_stage": "parse_output"}]` → `"parse"`;
      - `[{"restart_stage": "download"}]` → `None`;
      - `[{"restart_stage": ""}]` → `None`;
      - `[{}]` → `None`;
      - `[{"restart_stage": "publish_tiles"}, {"restart_stage": "convert"}]` → `"convert"`.
    - The existing `tests/test_chain_cross_stage_state.py` pin is untouched.
  - Update the module docstring sections (`:9-22`): section 1 gains L, and section 7 loses L.
- Spec: no delta.
  - The change's `specs/job-retry-mechanism/spec.md:3-21` ADDs the owner requirement. It already lists next-attempt minting on "stage-scoped, floored, inline, current-master, non-master and manual paths" (`:9-10`) and "the cohort restart stage" (`:12`).
  - It also requires "plain data" with the one `is_occupied` seam (`:18-20`), which these signatures honour, and "SHALL NOT change its result for any input" (`:20-21`). `mint_attempt`'s predicate takes an attempt rather than an id. The site maps attempt to id through the late-bound `_pipeline_retry_job_id` and tests that id, so the seam is still the occupied-id check.
  - design.md Appendix D has no T2 row.
  - `git grep -nE "_next_retry_attempt_for_stage|_restart_stage_from_basins|_retry_attempt_from_basins|_floored_cycle_stage_retry_job_id|_mint_cycle_stage_retry_job_id|_next_current_master_retry_identity|_schedule_cycle_stage_retry|_reserve_cycle_stage|_retry_cycle_stage_job_id|failure_number|schedule_auto_retry" -- openspec/specs` returns 2 hits (`canonical-precip-copyback/spec.md:39`, `_run_cycle_chain`; `job-retry-mechanism/spec.md:2665`, `schedule_auto_retry` lineage). Neither names an owner of the formulas.
  - The `submission_attempt` hits (7) and the `restart_stage` hits (30) describe values, not the computing function. All stay true under a pure move.
  - `tasks.md`:
    - tick 9.2 in the final push;
    - reword 9.2 to the HEAD anchors;
    - record the two extra owner names and the four deliberate non-moves;
    - drop `:356` from its site list, with the reason (decided: not moved).
- Must preserve:
  - every T1 expected value. `git diff master -- tests/test_retry_accounting_table.py` shows only the Q retarget, added rows, the L move and docstring lines, each classified.
  - every persisted id, `retry_count` and `submission_attempt`, byte for byte;
  - every delegate as a `def` with an unchanged name and signature in its module (`_next_retry_attempt_for_stage`, `_floored_cycle_stage_retry_job_id`, `_next_current_master_retry_identity`, `_restart_stage_from_basins`), and every facade alias and late-bound read;
  - the 0 floors of `split_retry_job_identity` / `_coerce_attempt`;
  - the snapshot-before-journal probe order;
  - zero new compatibility-facade signals (expected guard output: no `new-monkeypatch-alias` / `new-facade-reexport` for `_next_retry_attempt_for_stage` or any delegate, because every delegate body has two or more statements or is not in a facade module);
  - no top-level def added to or removed from the journal or its helpers;
  - `retry_identity.py` imports only stdlib (`git grep -n "^from\|^import" services/orchestrator/retry_identity.py` shows no `services.`).
- Risk packs:
  - Selected:
    - Legacy compatibility / examples: persisted ids and counts, delegate names, and the facade and late-binding surface. Covered by T1 unchanged, per-function mutations, the delegate rows, and the facade audit.
    - Concurrency / shared state / ordering: the occupancy probe order and the occupied-id skip (rows E and K). Covered by the E rows, the new probe-order row, and the K index pin.
    - Error handling / rollback / partial outputs: the raise-vs-swallow split in the increments and the `KeyError` at `:12678`. Covered by the `ValueError` direct row; the `failed_job["job_id"]` subscript stays at the site.
  - Not selected:
    - Public API (no route or OpenAPI change);
    - Config (no env or default change; that is 9.4);
    - File IO (journal writes unchanged);
    - Schema (no column change);
    - Auth;
    - Resource limits;
    - Release / packaging;
    - Documentation (the inventory rows `:345,346,353` stay true: the owners of the delegates are unchanged).
- Required evidence. Red/green sets below are predictions from the code. A different observed set means stop and report.
  1. Baseline, before any production edit, at the recorded HEAD sha:
     - `uv run pytest -q tests/test_retry_accounting_table.py` → green, 172 passed (the count at #3265 merge, `.workplans/3265/green-table.log`).
     - Write the new direct rows first, against enclosing functions where the owner does not exist yet: the `_next_retry_attempt_for_stage` stub stage, `_floored_cycle_stage_retry_job_id` stubs, `_schedule_cycle_stage_retry` (D harness), `_reserved_submission_attempt`, `_next_current_master_retry_identity`, the journal non-master harness, and `_restart_stage_from_basins` for the L rows.
     - Record the observed values, then retarget the rows to the owner.
     - Hash every production file to be edited (`sha256sum`).
  2. After the change, the same command is green: 172 − 1 (L index pin) + the new rows. List every diff line of the table file as retarget, added, L-move or docstring.
  3. Per-function mutations, each applied in the owner, run on the table file, and then restored with a matching hash:
     - `next_stage_attempt` `+ 1` → `+ 2`. Red: M (9), N fall-through (3), its direct rows. Green: N claim rows, A/E, D.
     - `mint_attempt` `max(floors) + 1` → `+ 2`. Red: `A-floor-plus-one`, `A-cohort-max-floor`, `A-none-job-id-with-floor`, `A-suffix-equal-floor`, the direct rows with an empty occupied set, and the call-recording row (calls `[4, 5]`). Green: both E rows and the new probe-order row (they land on the same id), and the direct `([2], {3})` row.
     - Second `mint_attempt` mutation: delete the skip loop. Red: both E rows, the probe-order row, and the direct rows with an occupied set.
     - `inline_retry_attempt` `failure_number - 1` → `failure_number`. Red: `D-failure-number` and the direct `(0, 0, 7)` row. Green: the other 3 D rows and the other direct rows (`(0, 0, 0)` → 1 either way).
     - `reservation_submission_attempt` `+ 1` → `+ 2`. Red: `flat-suffix`, `stacked-suffix`, `context-attempt-zero` in both increment tests (6), plus the matching direct rows.
     - `next_effective_attempt` `+ 1` → `+ 2`. Red: the 5 retargeted Q-master rows, the diverge test, and the `next_effective_attempt` direct rows. Green: the Q-master parity rows (both sides see the mutation).
     - `next_after_failure_master` with an unstripped base (`retry_job_id(text, count)`). Red: 4 of the 5 retargeted Q-master rows (not `Q-master-first-retry`) and the diverge test. Green: the parity rows and the `next_effective_attempt` rows.
     - `restart_stage_from_basins` `min` → `max`. Red: the multi-stage L rows only.
  4. The two carry-over discriminating mutations (#3320):
     - (1) Inside `next_after_failure_non_master`, replace the f-string with `retry_job_id(job_id, count)`. Predicted red: only `snapshot-negative-unclamped` and the direct `-1` row. A literal edit at the journal site would hit the local-shadow `UnboundLocalError` and turn every row red, so it must be applied in the owner.
     - (2) Replace `_pipeline_retry_job_id`'s body with an unclamped `f"{base_job_id}_retry_{int(attempt)}"`. Predicted red: only `test_pipeline_retry_job_id_delegates_to_retry_job_id[-1]` and `[0]`. `N-negative-clamped` stays green because of `:209-210`.
     - Restore with matching hashes.
  5. The required existing pins are green: `tests/test_retry_mint_floor.py` (whole file: A-limit, B, C, D, E, F), `tests/test_chain_cross_stage_state.py`, `tests/test_file_orchestration_journal.py::test_file_auto_retry_master_virtual_row_renders_public_lineage`, `::test_file_journal_manual_repair_suffix_only_target_emits_next_durable_attempt`, `::test_next_current_master_retry_identity_is_stable_after_helper_consolidation`, `tests/test_gateway_reconcile_master_transitions.py`, `tests/test_file_journal_cluster_map.py`, and `tests/test_orchestration_chain.py -k "retry or restart"`.
  6. Residual greps, compared by content, not line:
     - (a) `git grep -nE '_retry_\{|RETRY_JOB_ID_MARKER\}|\+ *"_retry_"|_retry_" *\+|f"\{[A-Za-z_]+\}_retry_"' -- services packages apps scripts workers`.
       - Baseline at cb5d3568b (run): `chain.py:843`, `file_orchestration_journal.py:12459`, `retry_identity.py:53`, `scheduler_state_manual_retry.py:914`.
       - After: baseline minus `chain.py:843` and `file_orchestration_journal.py:12459`, plus the new `retry_identity.py` lines (the `next_stage_attempt` prefix and the `next_after_failure_non_master` f-string). `:914` is `manual_retry_{key}`, not an id.
     - (b) `git grep -nE '(effective_retry_attempt|retry_suffix_attempt|split_retry_job_identity)\(.*\+ *1|rsplit\(.*\+ *1|default=0\) *\+ *1|max\(floors\) *\+ *1|or 0\) *\+ *1|"_retry_"' -- services/orchestrator`.
       - Baseline at cb5d3568b (run): `chain.py:857`; `chain_forecast_execution.py:655`; `chain_forecast_orchestrator_cycle.py:668,673,741,746`; `chain_runtime_utils.py:458`; `file_journal_retry_helpers.py:54`; `file_orchestration_journal.py:11374,11513,11542,12458,12678`; `retry_identity.py:23,146`; `scheduler_state_rows.py:886`.
       - After: baseline minus `chain.py:857`, `chain_forecast_execution.py:655`, `chain_forecast_orchestrator_cycle.py:668,673,741,746`, `file_journal_retry_helpers.py:54` and `file_orchestration_journal.py:12458,12678`, plus the new `retry_identity.py` lines.
       - Remaining outside the owner, on purpose: the event-sequence counters `:11374,11513,11542` (not attempts), `chain_runtime_utils.py:458` (the `_stage_job_sort_key` parser, 9.3) and `scheduler_state_rows.py:886` (a match).
     - Any other hit is a missed site.
  7. `uv run python scripts/governance/audit_repo_entropy.py --format json --structural-base-ref master` → no new `compatibility-facade-growth` signal for `services/orchestrator/chain.py`.
  8. Full local suite green (proxy env unset); `uv run ruff check . --no-cache`; `openspec validate redesign-orchestration-core --strict --no-interactive`; CI full suite, every shard.
  9. node-27, per D5 "plus node-27 real-DB suites when those suites import the code". This is the same set as T2-1, because the touched modules are on the scheduler/chain/journal side: on throwaway `nhms_it_*` DBs, run `-m integration tests/test_qhh_production_bootstrap_scheduler.py tests/test_real_database_integration.py`. Expected: all pass, 0 leftover DBs.
  10. node-22 (D5 pure-move batch, task 9.7): ff deploy at a pass boundary, then one idle pass (`status=planned`, `restart_reconcile.status=completed`, `lock.lock_type=file`) and one submitting pass (`status=submitted`), within a batch of at most three merged pure-move PRs or one day. 9.2 is ticked in the final push, with no trailing docs commit. The PR merges with `Part of #3266` until the batch has run.
- Review focus:
  1. Each delegate is equal for every input. In particular:
     - `mint_attempt`'s predicate is the exact negation with the same short-circuit, and the setup still runs after the binding check;
     - `next_stage_attempt`'s generator keeps filter-then-`.get` order;
     - `restart_stage_from_basins` uses the dict `len`;
     - the increments keep the outer `int()` raise and the inner `ValueError` swallow.
  2. The divergences stay as two code paths. Non-master is unstripped and unclamped (`int(x or 0)`, not `_coerce_attempt`). Master keeps the 0-floored `effective_retry_attempt`. The rsplit increments do not become `retry_suffix_attempt`.
  3. No delegate is renamed or turned into a rebinding; late-bound `_chain.` reads stay; `chain.py` imports the module, not the symbol.
  4. The deviations are applied as recorded: the two extra owner names, the four non-moves, and no unused import left behind (F401).
  5. No T1 literal is edited except the sanctioned L removal from `INDEX` and from the completeness list.

Slice triage (T2-3, #3267, task 9.3):
- Issue type: refactor (a pure move: no output changes for any input). Fixture level: expanded, agreeing with the issue. The two T2 expanded triggers apply. First, persisted or shared state (`issue-risk-contract.md:22`): every stage attempt read here feeds a budget decision, a durable `retry_attempt_floor` / manual `new_attempt` in decision evidence, and the mint that turns into a `pipeline_job.job_id`. Second, the project's orchestrator/state-machine trigger (`openspec/project-profile.md:107`).
  - Scope, by orchestrator decision on the issue (two comments): task 9.3's text (`scheduler_state_rows.py:517-656` and the `_stage_job_sort_key` parser), plus the scheduler-side manual new-attempt arithmetic, `scheduler_state_manual_retry.py:122,132` and `:1021-1039`.
  - Minimal mergeable slice: atomic (agree). Three owner functions, each with its own rows and mutations. None depends on another's PR.
  - The clean cut, if the orchestrator wants one: `next_manual_attempt` plus the manual-site rows (sections F and G under Tests) as a separate PR. It shares no helper with the stage reads; it only consumes `_state_retry_attempt`'s value. A split costs a D5 batch slot (at most three pure-move PRs per node-22 batch) and buys no extra verification.
  - Decided (orchestrator): one PR.
- Governing invariant: every attempt computation named in Change is done by one `retry_identity` function. Each delegating site returns the same value, or raises the same exception at the same point, as today for every input. That includes `None` and non-int counts, negative counts and tails, malformed, stacked and empty suffixes, missing stages, alias stages, non-canonical stages, non-Mapping or non-int floors, legacy rows without `stage`, and states without a flat key.
  - Two divergences stay as separate code paths:
    - `_stage_job_sort_key`'s attempt: a persisted count wins, no clamp, negative tail kept. It differs from `effective_retry_attempt`, which takes the max and floors at 0 (D2: no unification while moving).
    - The stage-less read: recorded counts only, no clamp, so negatives pass through. It differs from the stage-scoped read, which goes through `effective_retry_attempt`, always `>= 0`.
  - `retry_identity.py` stays stdlib-only.
  - The T1 table stays green with every expected value unchanged.
- Recon at HEAD a66dd8306 (9.1 and 9.2 merged). These anchors replace T1's.
  1. Stage-scoped / flat dispatch, `scheduler_state_rows.py:517-582` `_state_retry_attempt(state, *, stage=None)`.
     - `canonical_stage = _canonical_downstream_stage(stage)` (`:574`).
     - Non-canonical and non-empty → `_state_non_canonical_stage_retry_attempt(state, str(stage))` (`:577`).
     - `None` / `""` → `_state_flat_retry_attempt(state)` if not `None`, else `_state_job_retry_attempt(state, None)`.
     - Canonical → `_state_job_retry_attempt(state, canonical_stage)`.
     - Pure dispatch, no arithmetic.
     - Name re-exported by `scheduler.py:198`, `scheduler_state.py:143`, `scheduler_state_compat.py:144` (`SCHEDULER_STATE_COMPAT_REEXPORT_NAMES`, wrapped by the installer: it needs `FunctionType` identity with the owner module's attr).
     - Imported by name by `scheduler_candidates.py:48`, `scheduler_state_evidence_owner.py:29`, `scheduler_state_failure.py:54`, `scheduler_state_manual_retry.py:30`, and tests (`test_file_journal_repository_semantics.py:1694`, `test_orchestration_chain.py:18376`, `test_production_scheduler.py` x94, `test_file_orchestration_migration.py:980,1090`).
     - No monkeypatch of it anywhere (`git grep` on `setattr(...` with `"_state_retry_attempt"` is empty).
  2. Non-canonical arm, `:584-602` `_state_non_canonical_stage_retry_attempt(state, stage)`.
     - `max((effective_retry_attempt(job.get("job_id"), _coerce_int(job.get("retry_count"), default=0)) for job in _state_jobs(state) if _job_stage_name(job) == stage and not _job_is_cycle_scope_row(job)), default=0)` (`:595-602`).
     - Filter first, then coerce: a non-matching row's count is never coerced.
     - Test import `test_production_scheduler.py` (1 hit).
  3. Flat read, `:604-609` `_state_flat_retry_attempt(state)`.
     - First of `retry_attempt` / `attempt` / `retry_count` not in `(None, "")` → `_coerce_int(value, default=0)`. Otherwise `None`.
     - A key-precedence read of the state mapping, no arithmetic.
  4. Row scan plus floor, `:611-628` `_state_job_retry_attempt(state, canonical_stage)`.
     - `jobs = _state_jobs(state)` (`:624`; job-limit window).
     - `scanned = max((_job_retry_attempt(job, canonical_stage) for job in jobs), default=0)` (`:625`).
     - `None` stage → `scanned`. Otherwise `max(scanned, _state_stage_retry_attempt_floor(state, canonical_stage))` (`:628`). The floor is read after the scan.
  5. Floor read, `:630-634` `_state_stage_retry_attempt_floor`.
     - `state.get(STAGE_RETRY_ATTEMPT_FLOORS_KEY) or {}`; non-Mapping → 0; else `_coerce_int(floors.get(stage), default=0)`.
     - Always returns an `int`, never `None`.
  6. Per-row read, `:636-647` `_job_retry_attempt(job, canonical_stage)`.
     - `recorded = _coerce_int(job.get("retry_count"), default=0)` (`:637`) runs FIRST for every row, before the stage check.
     - `None` stage → `recorded` (raw, may be negative). Stage mismatch (`_canonical_downstream_stage(_job_stage_name(job)) != canonical_stage`) → 0. Otherwise `effective_retry_attempt(job.get("job_id"), recorded)` (`:647`).
     - Its only formula is already the owner's `effective_retry_attempt`.
     - Observed at a66dd8306: `_state_retry_attempt({"pipeline_jobs": [{"job_id": "a", "stage": "forcing", "retry_count": float("inf")}]}, stage="forecast")` raises `OverflowError`. A non-matching row's count is coerced and can raise. A filter-then-coerce delegate would return 0 instead, so it is not a pure move.
  7. `:649-654` `_job_stage_name`: topology (`stage` then `job_type`). Imported by `scheduler_state_manual_retry.py:25`. Stays.
  8. Floors producer, `:656-704` `stage_retry_attempt_floors(jobs)`.
     - Per job: `stage = _canonical_downstream_stage(_job_stage_name(job))`; `None` → skip, so match first and the count is never read.
     - Then `attempt = effective_retry_attempt(job.get("job_id"), job.get("retry_count"))` (`:694`, raw count, no `_coerce_int`).
     - Ties join, `attempt < current or attempt <= 0` skips, `attempt > current` resets `floors[stage]` / `sources[stage] = []`.
     - `sources[stage].append(_stage_retry_attempt_floor_source(job))` (`:703`) projects lazily, contributors only.
     - Key order is first-positive-insertion; it is serialized into candidate state.
     - Sole production caller `chain_repository_state.py:702`, over the untruncated, terminal-filtered `dict(job)` copies (`:671-672`). Consumer `scheduler_state_identity_filter.py:344` narrows by the recorded sources.
     - Observed: a canonical row with `retry_count=inf` raises `OverflowError`, a non-canonical one does not.
  9. Sort-key parser, `chain_runtime_utils.py:449-473` `_stage_job_sort_key(job, stage)`, `:450-462`:
     ```python
     job_id = str(job.get("job_id") or "")
     base_job_id = str(job_id).removesuffix("_retry_0")
     persisted_attempt = _coerce_int(job.get("retry_count"), default=-1)
     attempt = persisted_attempt if persisted_attempt >= 0 else 0
     marker = "_retry_"
     if persisted_attempt < 0 and marker in job_id:
         try:
             attempt = int(job_id.rsplit(marker, maxsplit=1)[1])
         except ValueError:
             attempt = 0
         base_job_id = job_id.rsplit(marker, maxsplit=1)[0]
     elif marker in job_id:
         base_job_id = job_id.rsplit(marker, maxsplit=1)[0]
     ```
     - Then `stage_match` from `job.get("stage")` / `job.get("job_type")` / `base_job_id.endswith(f"_{stage.stage}")`, plus terminal and created times (`:463-473`).
     - The base feeds `stage_match`, so the parser yields `(base, attempt)`.
     - Only the last layer is stripped: `{"job_id": f"{B}_retry_1_retry_4"}` with no stage field → `(0, 4)`.
     - `_coerce_int` is the local `chain_runtime_utils.py:42` (facade alias `chain.py:576`): `int()` semantics (`True`→1, `2.9`→2, `"3"`→3, `"x"`/`None`→-1 default, `inf`→`OverflowError`), not `_coerce_attempt`.
     - `removesuffix("_retry_0")` is dead in effect: whenever it changes the id, `marker in job_id` holds and a branch overwrites the base with the same value. This is T1's SK0 equivalent mutant (`.workplans/3264/mutations.log:121-127`).
     - Callers: facade alias `chain.py:594`, late-bound wrapper `chain_forecast_cycle.py:78-79` (used at `:546`), inventory row `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md:354`, test pins `tests/test_warm_start_chaining.py::test_released_reservation_reenters_only_through_a_higher_attempt_retry_row` (`:2624`) plus T1.
     - No monkeypatch.
  10. Manual default, `scheduler_state_manual_retry.py:111-149` `_manual_retry_markers`.
      - `default_attempt=_state_retry_attempt(state) + 1` at `:122` (Mapping marker with `marker`/`requested`/`enabled`) and `:132` (truthy non-Mapping marker).
      - Evaluated eagerly, even when the payload's `new_attempt` / `retry_count` / `attempt` wins in `_first_state_int` (`scheduler_state_common.py:49-54`, which uses `default or 0` for a non-int value).
      - The stage-less read can be negative, so the default can be -2 (observed). The `+ 1` is unclamped.
      - `_manual_retry_markers` / `_manual_retry_marker_record` are re-exported by `scheduler.py:158,160`, `scheduler_state.py:108,110`, and `scheduler_state_compat.py:104,106`.
  11. Manual new attempt, `:1016-1039` `_manual_retry_new_attempt(state, *, previous_attempt)`. Seven `+ 1` sites:
      - `:1021` `_coerce_int(value, default=_fallback_previous_attempt(state, previous_attempt) + 1)` for the payload's `new_attempt` / `retry_count`. The default is eager.
      - `:1035` newest adopted marker, empty `retry_count`.
      - `:1037` newest adopted marker, non-pinning.
      - `:1038` newest adopted pinning marker; `_coerce_int(value, default=... + 1)`. Reachable only when the payload carries the key with `None`, e.g. the state's own `manual_retry: {"new_attempt": None}`, because `_manual_retry_payload:921-924` uses `setdefault`. Otherwise a pinning event resolves at `:1021`.
      - `:1039` no adopted marker.
      - Re-exported (`scheduler.py:161`, `scheduler_state.py:111`, compat `:107`). Caller `scheduler_state_failure.py:2252`. Pinned by roughly 100 asserts across `test_production_scheduler.py`, `test_file_orchestration_migration.py` and `test_file_orchestration_journal.py:1576,1666,3571`.
      - `_fallback_previous_attempt` (`:964-1014`):
        - lazy import of `scheduler_state_failure._candidate_failed_stage`;
        - canonical failed stage → `previous_attempt`;
        - otherwise `max([previous_attempt, *[_state_retry_attempt(state, stage=s) for s in _restarted_stage_family(state)]])` (`:1013-1014`);
        - no `+ 1`;
        - called directly by `tests/test_production_scheduler.py:8155` (parity, no literal);
        - with an empty family and a negative `previous_attempt` it returns the negative value (observed `-2` → new attempt `-1`).
      - Site reach of every row in Tests F/G was confirmed with `sys.settrace` on unchanged code (scratch script, not committed).
  - Compatibility propagation (`scheduler_state_compat.py:169-235`):
    - A patch of a `SCHEDULER_STATE_COMPAT_REEXPORT_NAMES` function on the `scheduler` facade is propagated, during wrapped calls, onto every `services.orchestrator.scheduler_state*` module that has that name. This includes `_coerce_int:56`, `_canonical_downstream_stage:55`, `_state_jobs:141` and `_state_retry_attempt:144`.
    - `retry_identity` is outside that prefix, so anything those names do must stay a module-global lookup in the `scheduler_state_*` site.
    - The only facade patch of one of them today is `test_production_scheduler.py:1605` (`_canonical_downstream_stage`, on the execution path, not an attempt read). The mechanism is still the contract.
  - Governed facades: only `scheduler.py` and `chain.py` (`scripts/governance/entropy_audit/schema.py:206-217`). The guard scans definitions in those two files only (`facade_guard.py:144-170`). This slice edits neither.
- Blast radius:
  - Files changed:
    - `services/orchestrator/retry_identity.py` (three new functions; still stdlib-only);
    - `scheduler_state_rows.py` (`:595-602`, `:624-628` bodies; the `:15` import);
    - `scheduler_state_manual_retry.py` (`:122`, `:132`, `:1021`, `:1035`, `:1037`, `:1038`, `:1039`; the `:8` import);
    - `chain_runtime_utils.py` (`:451-462` body; the `:28-33` import);
    - `tests/test_retry_accounting_table.py`;
    - the umbrella `tasks.md` (this block, the 9.3 tick, the 9.3 wording).
  - Not changed: `chain.py`, `scheduler.py`, `scheduler_state.py`, `scheduler_state_compat.py`, `chain_forecast_cycle.py`, `chain_repository_state.py`, both inventories. No import goes unused: `effective_retry_attempt` is still used at `scheduler_state_rows.py:597,647,694`, and `_coerce_int` is still used at `chain_runtime_utils.py:452` (now as the argument).
  - A wrong move would change a stage budget read (a candidate blocked early or let past its limit), the latest-row choice for a stage (`_stage_job_sort_key` → warm-start / reservation re-entry), or a manual `new_attempt` that becomes a durable id. Each of these re-mints or skips an attempt (#1179, #1298, #1579, #1293, #1300, #1577).
- Non-goals:
  - any T1 expected value;
  - unifying `_stage_job_sort_key`'s attempt with `effective_retry_attempt`, or clamping the stage-less read (D2; #3319 owns behaviour changes);
  - deleting the dead `removesuffix("_retry_0")` (moved verbatim; report only);
  - stage canonicalization, cycle-scope and stage-family judgement, or the job-limit window;
  - limits and status sets (9.4);
  - the AST guard (9.5);
  - renaming or deleting any delegate, facade alias, compat re-export or late-bound wrapper (F1/F2);
  - #3284.
- Sibling surfaces (must stay):
  - parsers `split_retry_job_identity` / `retry_suffix_attempt` / `effective_retry_attempt` / `_coerce_attempt` with their 0 floors;
  - the 9.2 owner functions;
  - topology and coercion helpers at their sites: `_canonical_downstream_stage`, `_job_stage_name`, `_job_is_cycle_scope_row`, `_state_jobs`, `_state_job_limit`, both `_coerce_int`s, `_first_state_int`, `_candidate_failed_stage`, `_restarted_stage_family`, `_marker_event_pins_attempt`, `_event_is_adopted_manual_retry_marker`;
  - `_state_flat_retry_attempt` (key precedence), `_state_stage_retry_attempt_floor` (floor read), `_job_retry_attempt` (per-row read; see Change, "Deliberately not moved");
  - `scheduler_state_identity_filter._narrow_stage_retry_attempt_floors` (consumer of the sources);
  - the late-binding chain `chain.py:594` → `chain_forecast_cycle.py:78-79`;
  - the compat installer's drift checks (`scheduler_state_compat.py:237-257`).
  - Not attempt arithmetic (sweep of `+ 1` in `services/orchestrator/scheduler*.py`): `scheduler_state_manual_retry.py:1127` (recovered-stage counter), `scheduler_lease.py:250`, `scheduler_no_progress.py:270`, and the status tallies. The out-of-scope sites T2-2 reported (`chain_forecast_execution.py:866-876,327,944`, journal `:3394-3399`) are unchanged and still reported, not moved.
- Change (9.3). Plain-data functions in `retry_identity.py`. Expressions are moved verbatim; inside the owner `"_retry_"` becomes the equal `RETRY_JOB_ID_MARKER`.
  1. `stage_attempt(row_attempts: Iterable[int], floor: int | None = None) -> int`: `scanned = max(row_attempts, default=0)`; `floor is None` → `scanned`; otherwise `max(scanned, floor)`.
     - Docstring: the stage attempt over row attempts the caller already read and scoped. With no `floor` (the stage-less read and the non-canonical arm) nothing is clamped, so negative recorded counts pass through.
     - Delegate A, `_state_job_retry_attempt` (`:624-628`):
       ```python
       jobs = _state_jobs(state)
       row_attempts = [_job_retry_attempt(job, canonical_stage) for job in jobs]
       if canonical_stage is None:
           return stage_attempt(row_attempts)
       return stage_attempt(row_attempts, _state_stage_retry_attempt_floor(state, canonical_stage))
       ```
       - The list is materialized before the floor is read, so the order is unchanged: every row is coerced (`_job_retry_attempt`), then the floor.
       - The `None` sentinel cannot collide, because `_state_stage_retry_attempt_floor` always returns an `int`.
     - Delegate B, `_state_non_canonical_stage_retry_attempt` (`:595-602`): `return stage_attempt(<the same generator expression>)`. The generator's `if` filter clause (`_job_stage_name(job) == stage and not _job_is_cycle_scope_row(job)`) runs before its element expression (`_coerce_int(job.get("retry_count"), default=0)`) for each job, so a non-matching row's count is never coerced: match-before-coerce is unchanged. Tests B pins this with the non-canonical `inf` row → 0. With `floor=None`, `max(gen, default=0)` is unchanged.
     - Rejected alternative: owner takes `(job_id, count)` pairs and the site filters first. That breaks the coerce-before-match order of Delegate A (the `OverflowError` row in Tests B).
  2. `stage_job_sort_attempt(job_id: str, persisted_attempt: int) -> tuple[str, int]`: the `:451,453-462` block verbatim (`removesuffix("_retry_0")` kept as the literal), returning `(base_job_id, attempt)`.
     - Delegate (`_stage_job_sort_key` stays a `def` with the same signature): `job_id = str(job.get("job_id") or "")`; `base_job_id, attempt = stage_job_sort_attempt(job_id, _coerce_int(job.get("retry_count"), default=-1))`; `stage_match` / times unchanged.
     - Coercion stays at the site (the local `_coerce_int`, facade alias `chain.py:576`; `int()` semantics; the `OverflowError` raise stays there).
     - Moving `removesuffix` after the `retry_count` read is unobservable (pure `str` op).
     - The name is not in D2's list (deviation, like 9.2's two extra names).
  3. `next_manual_attempt(previous_attempt: int) -> int` = `previous_attempt + 1`. Unclamped: it must not be `next_effective_attempt` or go near `retry_job_id`'s clamp (observed defaults -2, -1, 0).
     - Delegates: `:122` and `:132` → `default_attempt=next_manual_attempt(_state_retry_attempt(state))`; `:1021`, `:1038` → `_coerce_int(value, default=next_manual_attempt(_fallback_previous_attempt(state, previous_attempt)))` (the default stays eager); `:1035`, `:1037`, `:1039` → `return next_manual_attempt(_fallback_previous_attempt(state, previous_attempt))`.
     - `_state_retry_attempt` and `_fallback_previous_attempt` stay module-global lookups (compat propagation).
     - The name is not in D2's list (deviation). The spec's "manual path" (`:9-10`) covers it.
  - Decided (orchestrator), owner names: `stage_attempt` is the state-read aggregation, which matches spec `:11` "stage attempt reads over already-read rows". The parser gets `stage_job_sort_attempt`. One function cannot serve both, because their divergence is pinned (T1 `test_stage_job_sort_key_diverges_from_effective_retry_attempt`).
  - How little of `:517-656` changes: under these recommendations, none of the three functions the task names there gets a body edit (`_state_retry_attempt`, `_job_retry_attempt`, `stage_retry_attempt_floors`). The only edits in the range are the bodies of `_state_non_canonical_stage_retry_attempt` (`:595-602`) and `_state_job_retry_attempt` (`:624-628`), which delegate their aggregation to `stage_attempt`.
    - Their per-row attempt formula already lives in the owner: `effective_retry_attempt`, used at `:597,647,694`.
    - What stays local is topology (stage matching and canonicalization, cycle scope, the job-limit window), coercion through compat-propagated `_coerce_int`, key precedence and contributor bookkeeping.
    - 9.3's wording should say this plainly rather than "delegate `:517-656`".
  - Deliberately not moved, each recorded in the PR body and in 9.3's wording:
    - `_job_retry_attempt`. Decided (orchestrator): not moved, because there is nothing to move. Its formula is already `effective_retry_attempt`; the rest is scope (stage-less → raw recorded, mismatch → 0), and its coerce-first order is load-bearing. Alternative: a verbatim owner copy `stage_row_attempt(job_id, recorded, matches: bool | None)`, a fourth name D2 does not list, with no guard or spec requirement behind it.
    - `_state_flat_retry_attempt` / `_state_stage_retry_attempt_floor`: key-precedence and floor reads of the state mapping through the compat-propagated `_coerce_int`, with no arithmetic.
    - `_state_retry_attempt`: pure dispatch.
    - `stage_retry_attempt_floors`. Decided (orchestrator): not moved.
      - Its per-row attempt is already `effective_retry_attempt`. What is left is the tie-grouping that records contributor sources for the identity filter, with a lazy projection over the untruncated cycle-wide list.
      - A plain-data owner would have to either take an injected projection (a second callable seam against spec `:18-20`'s letter) or defer the projection to the final contributors. Deferring changes which rows get projected; that is pure for the `dict(job)` copies the only production caller passes, but not "every input".
      - Precedent: 9.2 left `_retry_attempt_from_basins` for the same reasons.
      - Alternative: `stage_attempt_floors(rows: Iterable[tuple[str, Any, Any, T]]) -> tuple[dict[str, int], dict[str, list[T]]]` with raw-row payloads; the site projects afterwards and keeps match-before-`.get` order.
    - `_fallback_previous_attempt`'s `max([previous_attempt, *family_floors])`. Decided (orchestrator): stays.
      - It has no `+ 1` and floors over values the owner already computes through `_state_retry_attempt`.
      - The family is the scope axis, and the function is called directly by a test.
      - It cannot reuse `stage_attempt`: for an empty family and `previous_attempt=-2`, `max([-2])` is -2, but `stage_attempt([], -2)` is 0.
  - Deviations, listed verbatim in the PR body and in 9.3's wording:
    1. Two extra owner names D2 does not list: `stage_job_sort_attempt` and `next_manual_attempt` (beside D2's `stage_attempt`).
    2. Manual-site scope: `scheduler_state_manual_retry.py:122,132,1021,1035,1037,1038,1039` are taken into 9.3 by orchestrator decision on #3267. They are not in 9.3's task text.
    3. The non-moves: `_job_retry_attempt`, `_state_flat_retry_attempt`, `_state_stage_retry_attempt_floor`, `_state_retry_attempt` (dispatch), `stage_retry_attempt_floors`, and `_fallback_previous_attempt`'s `max`.
    4. The existing section-3 T1 rows stay on their delegates (`test_state_retry_attempt`, `test_stage_job_sort_key`, `test_stage_job_sort_key_diverges_from_effective_retry_attempt`, `test_job_retry_attempt`, `test_stage_retry_attempt_floors`). D2, at `design.md:171`, says "Plain-data rows retarget to the owner functions". These rows cannot retarget, because the topology each row passes through stays at the site and the owner functions take already-scoped ints. The owner gets its own direct rows (A, C, E) instead.
- Tests (`tests/test_retry_accounting_table.py`, section 3). The rule: no expected literal is edited. Every delegate row's expected value below was observed at a66dd8306 by the triage (`uv run --no-sync`, scratch scripts), and the implementer re-observes them in Required evidence 1. B/D/F/G call the delegates; A/C/E call the owner.
  - Existing rows stay on their delegates, unchanged: `test_state_retry_attempt` (5), `test_stage_job_sort_key` (7), `test_stage_job_sort_key_diverges_from_effective_retry_attempt` (2), `test_job_retry_attempt` (4), `test_stage_retry_attempt_floors` (1). The delegate keeps the site's topology, so these rows still pass through it.
  - No delegate-parity rows. 9.2 added parity rows only where it retargeted a literal table to the owner and still needed the helper name pinned. Here no row is retargeted: every delegate row (B, D, F, G, H) carries its own literal. A parity row (`delegate(x) == owner(site-scoped x)`) would stay green under every owner mutation, because both sides see it. It would also stay green under a site that stopped delegating but copied the formula, so it adds no red.
  - A. `test_stage_attempt` (owner): `([], None)`→0; `([-3, -1], None)`→-1; `([2, 0, 5], None)`→5; `([], 4)`→4; `([1], 4)`→4; `([3], -2)`→3; `([], -2)`→0; `([0, 0], 0)`→0.
  - B. `test_state_retry_attempt`, new rows (state, stage → expected):
    - stage-less:
      - `{"pipeline_jobs": [{"job_id": "j", "retry_count": -3}]}` → -3;
      - the same with a second `-1` row → -1;
      - `{"retry_attempt": 4, "attempt": 5, "retry_count": 6}` → 4;
      - `{"attempt": 5, "retry_count": 6}` → 5;
      - `{"retry_attempt": "", "retry_count": 6}` → 6;
      - `{"retry_count": "x"}` → 0;
      - `{"retry_count": -2}` → -2;
      - `{}` → 0;
      - flat 7 with a `j_forecast_retry_5` forecast row and `stage=""` → 7.
    - canonical, `stage="forecast"` unless noted:
      - `stage="parse_output"` over `j_parse_retry_3`/`parse` → 3;
      - row stage `run_shud_forecast`, `j_retry_3` → 3;
      - `j_forecast_retry_1_retry_4` → 4;
      - `j_forecast_retry_abc` with `retry_count="x"` → 0;
      - `j_forecast` with `retry_count=-4` → 0;
      - no rows, floors `{"forecast": -2}` → 0;
      - `{"pipeline_jobs": [{"job_id": "j_forecast_retry_1", "stage": "forecast"}], "stage_retry_attempt_floors": {"forecast": "x"}}` → 1;
      - `{"pipeline_jobs": [{"job_id": "j_forecast_retry_1", "stage": "forecast"}], "stage_retry_attempt_floors": [5]}` → 1 (a non-Mapping floors value reads as floor 0, so the row's suffix 1 decides);
      - no rows, floors `{"forecast": "6"}` → 6;
      - `job_type="forecast"`, `j_retry_2` → 2;
      - a cycle-scope `job_cycle_gfs_2026050100_forecast_retry_3` row (`run_id` `cycle_gfs_2026050100`) → 3 (counted);
      - `job_limit=1`, rows `a` then `b_retry_5` → 0.
    - non-canonical, `stage="download"`:
      - `{"retry_count": 9, "pipeline_jobs": [{"job_id": "j_download_retry_3", "stage": "download", "model_id": "m", "run_id": "fcst_gfs_2026050100_m", "retry_count": 1}]}` → 3 (flat 9 excluded);
      - `{"pipeline_jobs": [{"job_id": "job_cycle_gfs_2026050100_download_retry_3", "stage": "download", "run_id": "cycle_gfs_2026050100"}]}` → 0 (cycle-scope row subtracted);
      - `{"pipeline_jobs": [{"job_id": "x_download_retry_3", "stage": "download", "run_id": "fcst_gfs_x"}]}` → 3 (model-less but not cycle grammar, so counted);
      - `{"retry_count": 9}` → 0;
      - `{"pipeline_jobs": [{"job_id": "x_download_retry_2", "stage": "download", "model_id": "m", "retry_count": "x"}]}` → 2;
      - `{"pipeline_jobs": [{"job_id": "j_forcing", "stage": "forcing", "retry_count": float("inf")}]}` → 0, no raise (match-before-coerce: the `if` filter clause drops the row before `_coerce_int` sees `inf`; observed 0 at a66dd8306).

    That is 27 rows. Plus `test_state_retry_attempt_coerces_every_row_before_matching`: a non-matching forcing row with `retry_count=float("inf")`, `stage="forecast"` → `pytest.raises(OverflowError)`; and an `inf` floor → `OverflowError` (2).
  - C. `test_stage_job_sort_attempt` (owner, `(job_id, persisted)` → `(base, attempt)`):
    - `("X_retry_2", 5)`→`("X", 5)`;
    - `("X_retry_2", 0)`→`("X", 0)`;
    - `("X_retry_2", -1)`→`("X", 2)`;
    - `("X_retry_abc", -1)`→`("X", 0)`;
    - `("X_retry_-3", -1)`→`("X", -3)`;
    - `("X_retry_1_retry_4", -1)`→`("X_retry_1", 4)`;
    - `("X_retry_1_retry_4", 2)`→`("X_retry_1", 2)`;
    - `("X_retry_", -1)`→`("X", 0)`;
    - `("X", -1)`→`("X", 0)`;
    - `("", -1)`→`("", 0)`;
    - `("X_retry_0", 2)`→`("X", 2)`;
    - `("X_retry_ 7", -1)`→`("X", 7)`;
    - `("X", 3)`→`("X", 3)`.

    That is 13 rows (values from the verbatim block, executed in a scratch script).
  - D. `test_stage_job_sort_key`, new rows (`FORECAST_BASE = B`, `[:2]`). None of these rows carries `stage` or `job_type`, so `stage_match` comes only from the returned base (the existing T1 rows with `stage` cannot discriminate the base):
    - `B_retry_2` with count -1 → (1, 2);
    - count `"x"` → (1, 2);
    - count `"3"` → (1, 3);
    - count `True` → (1, 1);
    - count `2.9` → (1, 2);
    - `B_retry_1_retry_4`, no stage → (0, 4);
    - `B_retry_` → (1, 0);
    - `B` → (1, 0);
    - `job_id=None` → (0, 0);
    - `{}` → (0, 0);
    - `B_retry_0` with count 2 → (1, 2);
    - `B_retry_ 7` → (1, 7);
    - `B_retry_2` with count 1 and no stage field → (1, 1) (base stripped in the count-present branch).

    That is 13 rows. Plus `retry_count=float("inf")` → `OverflowError` (1).
  - E. `test_next_manual_attempt` (owner): 0→1, 3→4, -2→-1, -3→-2 (4).
  - F. `test_manual_retry_marker_default_attempt` (`[m["attempt"] for m in _manual_retry_markers(state)]`):
    - `:122`:
      - `{"manual_retry": {"marker": True}, "retry_count": 2}`→[3];
      - `{"manual_retry": {"marker": True}, "pipeline_jobs": [{"job_id": "j", "retry_count": -3}]}`→[-2];
      - `{"manual_retry": {"marker": True, "new_attempt": "x"}, "retry_count": 2}`→[3];
      - `{"manual_retry": {"requested": True}, "retry_count": "x"}`→[1];
      - `{"manual_retry": {"marker": True, "new_attempt": 7}, "retry_count": 2}`→[7].
    - `:132`:
      - `{"manual_retry": True, "retry_count": 2}`→[3];
      - `{"manual_retry": "yes"}`→[1];
      - `{"manual_retry": True, "retry_count": -1}`→[0];
      - `{"manual_retry_marker": True, "attempt": 4}`→[5].

    That is 9 rows, each id naming its site line.
    - Eager-default raise row, `:122`: `{"manual_retry": {"marker": True, "new_attempt": 7}, "pipeline_jobs": [{"job_id": "j", "retry_count": float("inf")}]}` → `pytest.raises(OverflowError)`.
      - Observed at a66dd8306. The traceback runs `_manual_retry_markers:122` → `_state_retry_attempt:581` → `_state_job_retry_attempt:625` → `_job_retry_attempt:637` → `_coerce_int`.
      - The pinned `new_attempt: 7` would win, so the raise proves the default is evaluated eagerly. The control with `retry_count: 0` → `[7]`.
      - It turns red if a delegate makes the default lazy.
  - G. `test_manual_retry_new_attempt_sites` (`_manual_retry_new_attempt(state, previous_attempt=p)`). The fixture states are built inline:
    - Literal dicts, exactly as observed. They are derived from `tests/test_file_orchestration_migration.py:473-490`, where `_CYCLE_SCOPE_JOB` has `retry_count: 0`. Its pinning tests override that field (`{**_CYCLE_SCOPE_JOB, "retry_count": 4}`, `:591`), and the observation used the overridden 4. CYC's count reaches no G value, because CYC is a cycle-scope `download` row: it is outside the restarted family, and no canonical read matches `download`. Use the literal below, not an import from the migration test.
      - `CYC = {"job_id": "job_cycle_gfs_2026070100_download", "run_id": "cycle_gfs_2026070100", "model_id": None, "stage": "download", "status": "failed", "retry_count": 4, "updated_at": "2026-07-01T00:00:00Z"}`;
      - `OWN = {"job_id": "job_model_a_forecast", "run_id": "fcst_gfs_2026070100_model_a", "model_id": "model_a", "stage": "forecast", "status": "failed", "retry_count": 0, "updated_at": "2026-07-01T00:00:00Z"}` (identical to `:482-490`);
      - `EVENT(rc) = {"event_id": 9, "entity_type": "pipeline_job", "entity_id": "job_cycle_gfs_2026070100_download", "event_type": "retry", "created_at": "2026-07-01T01:00:00Z", "details": {"trigger": "manual", "manual_retry_marker": True, "retry_count": rc, "created_at": "2026-07-01T01:00:00Z", "previous_job_id": "job_cycle_gfs_2026070100_download"}}` (the `:493-521` helper's shape with `previous_job_id` set);
      - `ST(fs, rc) = {"pipeline_status": "failed", "retry_count": 0, "failed_stage": fs, "pipeline_jobs": [dict(CYC), dict(OWN)], "pipeline_events": [EVENT(rc)]}`;
      - `LIVE = {"pipeline_jobs": [{"job_id": "job_fcst_gfs_2026050100_m_forecast_retry_5", "stage": "forecast", "status": "cancelled", "model_id": "m", "run_id": "fcst_gfs_2026050100_m"}]}`;
      - `LIVE2 = {"pipeline_jobs": [{"job_id": "a_forecast_retry_2", "stage": "forecast", "status": "cancelled", "model_id": "m", "run_id": "fcst_gfs_2026050100_m"}, {"job_id": "a_convert_retry_4", "stage": "convert", "status": "cancelled", "model_id": "m", "run_id": "fcst_gfs_2026050100_m"}]}`;
      - `NAMED = {"failed_stage": "forecast", "pipeline_jobs": [{"job_id": "j_forecast_retry_9", "stage": "forecast", "status": "failed", "model_id": "m", "run_id": "fcst_gfs_2026050100_m"}]}`.

    Rows:
    - `:1021`: `{"manual_retry": {"marker": True, "new_attempt": "x"}}`, p=3 → 4; `{"manual_retry": {"marker": True, "new_attempt": 5}}`, p=0 → 5.
    - `:1035`: `ST("download", "")`, p=3 → 4 and p=-2 → 1.
    - `:1037`: `ST("forecast", 5)`, p=3 → 4 and p=-2 → -1.
    - `:1038`: `ST("download", "x")` plus `manual_retry={"marker": True, "new_attempt": None}`, p=3 → 4; the same with rc=7, p=0 → 7.
    - `:1039`:
      - `{}`, p=3/-2/0 → 4/-1/1;
      - `LIVE` (its `job_fcst_gfs_2026050100_m_forecast_retry_5` row is `cancelled`, so `_candidate_failed_stage` names nothing and the family is `{"forecast"}`), p=0 → 6 and p=7 → 8;
      - `LIVE2`, p=0 → 5;
      - `NAMED`, p=3 → 4.

    That is 15 rows, each id naming its site line.
    - Eager-default raise row, `:1021`: `{"manual_retry": {"marker": True, "new_attempt": 7}, "pipeline_jobs": [{**LIVE["pipeline_jobs"][0], "retry_count": float("inf")}]}`, p=0 → `pytest.raises(OverflowError)`.
      - Observed at a66dd8306. The traceback runs `_manual_retry_new_attempt:1021` → `_fallback_previous_attempt:1013` (family read `stage="forecast"`) → `_state_retry_attempt:582` → `_state_job_retry_attempt:625` → `_job_retry_attempt:637` → `_coerce_int`.
      - The pinned `new_attempt: 7` would win (the control with `retry_count: 0` → 7), so the raise proves the default is evaluated eagerly.
      - It turns red if a delegate makes the `:1021` default lazy.
    - Eager-default raise row, `:1038`: `ST("download", 7)` with `manual_retry={"marker": True, "new_attempt": None}` and OWN's `retry_count` set to `float("inf")`, p=0 → `pytest.raises(OverflowError)`.
      - Observed. The traceback runs `_manual_retry_new_attempt:1038` → `_fallback_previous_attempt:1013`.
      - The pinned event count 7 would win: the control with OWN `retry_count: 0` → 7.
    - Also observed: CYC `retry_count` 0 vs 4 gives identical values on every `ST` row (`[4, 1, 4, -1, 4, 7]`). That settles the migration-file reconciliation.
  - H. `test_stage_retry_attempt_floors_order` (delegate; `stage_retry_attempt_floors` is not moved, Decided (orchestrator)):
    - key order `["parse", "forecast"]` for rows `p_retry_2`/parse then `f_retry_1`/forecast;
    - a higher attempt resets the contributors (`a_retry_1`, `b_retry_3` → sources `["b_retry_3"]`);
    - a non-canonical `inf` count does not raise (`({}, {})`);
    - a canonical `inf` count → `OverflowError`.

    That is 4 rows. They pin the floors loop's match-first order and its contributor bookkeeping, which stay at the site (Decided (orchestrator): not moved).
  - New rows total 99: A 8, B 29 (27 value rows + 2 raise rows), C 13, D 14, E 4, F 10 (9 + the `:122` raise row), G 17 (15 + the `:1021` and `:1038` raise rows), H 4. Of these, 25 call the not-yet-existing owner (A, C, E); 74 are delegate rows, green on unchanged code.
  - Docstring section 3 (`:14`) gains "and the scheduler-side manual new attempt".
- Spec: no delta.
  - The change's `specs/job-retry-mechanism/spec.md:3-21` ADDs the owner requirement. It already lists "stage attempt reads over already-read rows" (`:11`) and the "manual" path (`:9-10`).
  - "plain data" and the one `is_occupied` seam (`:18-20`): the three signatures take ints and a `str`.
  - "SHALL NOT change its result for any input" (`:20-21`).
  - Which scenario covers the stage reads: "Historical retry cases are pinned in one table" (`:23-42`), row "suffix-over-count when the recorded count is absent" (`:32`). That row is `_stage_job_sort_key`'s count-missing branch and the stage-scoped `effective_retry_attempt` read. Its "plain-data rows … equal what the pre-move code returns" (`:41-42`) is the B/D/F/G literal rows.
  - The "Divergent paths are pinned as they are" scenario (`:44-55`) lists the non-master path, the `failure_number - 1` term, the two `rsplit` increments and the status-set difference. It does not list the two divergences this block's Governing invariant declares: the sort-key attempt vs `effective_retry_attempt`, and the unclamped stage-less read and manual `+ 1`.
    - This still means no delta. A pure move changes no result, so it is bound by the requirement's "SHALL NOT change its result for any input" (`:20-21`). T1 already pins the sort-key divergence (`test_stage_job_sort_key_diverges_from_effective_retry_attempt`), and the new B/F/G rows pin the unclamped values.
    - What the omission costs: a later unification of either divergence is not spec-bound to a behaviour-change issue the way the listed four are.
    - Decided (orchestrator): no delta in this PR. If the orchestrator wants that binding, add one line per divergence to `:45-52`. It is the same change's ADDED requirement, so `openspec validate` is the only extra cost.
  - The AST guard scenario (`:57-64`) does not match any 9.3 site: no `_retry_` id construction, and no `+ 1` on a parse call. 9.3 is required by D2 ownership and by `:11`, not by 9.5.
  - design.md Appendix D has no T2 row. design.md `:49` ("`_stage_job_sort_key` is a third parser") stays true.
  - `git grep` of the touched names over `openspec/specs`: one value-level hit, `job-retry-mechanism/spec.md:2281` (stage attempt from a raw projection), which stays true.
  - `docs/runbooks/failed-basin-retry.md:165,171,254` name `_state_retry_attempt`, `stage_retry_attempt_floors` and `_stage_job_sort_key` as the deciding functions. They remain the entry points, so the lines stay true.
  - `tasks.md`:
    - tick 9.3 in the final push;
    - reword 9.3 to the HEAD anchors;
    - record the four Deviations (Change): the two extra owner names, the manual-site scope, the non-moves, and the section-3 T1 rows kept on their delegates.
- Must preserve:
  - every T1 expected value. `git diff master -- tests/test_retry_accounting_table.py` shows only added rows and docstring lines, each classified.
  - every stage attempt, sort key and manual `new_attempt`, for every input, including the raise points;
  - the delegates (body edited) stay `def`s with unchanged names and signatures: `_state_non_canonical_stage_retry_attempt`, `_state_job_retry_attempt`, `_stage_job_sort_key`, `_manual_retry_markers`, `_manual_retry_new_attempt`.
  - untouched (byte-identical bodies, `git diff` shows no hunk in them): `_state_retry_attempt`, `_state_flat_retry_attempt`, `_state_stage_retry_attempt_floor`, `_job_retry_attempt`, `_job_stage_name`, `stage_retry_attempt_floors`, `_stage_retry_attempt_floor_source`, `_fallback_previous_attempt`, `_manual_retry_marker_record`.
  - the compat installer's identity and drift checks (import of `services.orchestrator.scheduler` succeeds);
  - `_coerce_int` / `_canonical_downstream_stage` / `_state_jobs` / `_state_retry_attempt` stay module-global lookups at their `scheduler_state_*` sites;
  - `git diff --stat master -- services/orchestrator/chain.py services/orchestrator/scheduler.py services/orchestrator/scheduler_state.py services/orchestrator/scheduler_state_compat.py services/orchestrator/chain_forecast_cycle.py` is empty;
  - coerce-before-match in `_state_job_retry_attempt`, match-before-coerce in the non-canonical arm and in the floors;
  - the eager manual defaults;
  - `retry_identity.py` imports only stdlib.
- Risk packs:
  - Selected:
    - Legacy compatibility / examples: persisted-attempt-bearing reads, delegate names, compat re-exports and propagation, late binding. Covered by T1 unchanged, the delegate rows, per-function and per-site mutations, the empty facade diff, and the audit.
    - Error handling / rollback / partial outputs: the three evaluation orders and their raise points. Covered by the `OverflowError` rows in B, D and H.
  - Not selected:
    - Concurrency (no occupancy probe in this slice);
    - Public API;
    - Config (9.4);
    - File IO;
    - Schema;
    - Auth;
    - Resource limits;
    - Release;
    - Documentation (runbook and inventory lines stay true).
- Required evidence. Red/green sets below are predictions from the code. A different observed set means stop and report.
  1. Baseline, before any production edit, at the recorded HEAD sha:
     - `uv run pytest -q tests/test_retry_accounting_table.py` → green, 222 passed (the count at #3266 merge, `.workplans/3266/green-table.log`; re-observe).
     - Add B, D, F, G and H first and run them on unchanged code → 74 passed, with the values above.
     - Hash every production file to be edited (`sha256sum`).
  2. Pre-change red: add A, C and E → 25 failed (`AttributeError` on `retry_identity.stage_attempt` / `stage_job_sort_attempt` / `next_manual_attempt`), everything else green.
  3. After the change, the same command → 321 passed (222 + 99). Classify every table-file diff line as added or docstring.
  4. Per-function mutations, each applied in the owner, run on the table file, then restored with a matching hash. M1–M3 were checked in a triage scratch simulation: the delegates were substituted in-process on unchanged code, no repo edit, over the B, F and G rows. The delegate matched today on every row, and M1–M3 turned red exactly the sets below.
     - M1, `stage_attempt` drop the floor (`return max(scanned, floor)` → `return scanned`). Red: T1 `stage-set-floor-counts`; B no-rows `"6"`-floor; A `([], 4)`, `([1], 4)`. Green: B negative / non-int / non-Mapping floor rows, A `([3], -2)` / `([], -2)` / `([0, 0], 0)`.
     - M2, `stage_attempt` clamp the floor-`None` branch (`max(scanned, 0)`). Red: B stage-less `-3` and `-1`; A `([-3, -1], None)`; F `:122` rows-negative (`[-2]`). Green: non-canonical rows (all `>= 0`) and G (the previous attempt is passed in).
     - M3, `stage_attempt` floor-`None` branch `scanned + 1`. Red:
       - T1 `stage-none-no-flat-recorded-counts-only`;
       - B stage-less rows without a flat key (`-3`, `-1`, `{}`) and all six non-canonical value rows (including the non-canonical `inf` row, 0 → 1);
       - A rows with `floor=None` (3);
       - F `:122` rows-negative and `:132` `"yes"`.

       Green: flat-key rows, canonical rows, G.
     - M4, `stage_job_sort_attempt` `if persisted_attempt < 0 and marker in job_id` → `if marker in job_id` (T1 SK). Red:
       - T1 `count-wins`, `zero-count-wins`, `count-wins-vs-max`;
       - D count `"3"`, count `True`, `B_retry_0` count 2, count-1-no-stage;
       - C `("X_retry_2", 5)`, `("X_retry_2", 0)`, `("X_retry_1_retry_4", 2)`, `("X_retry_0", 2)`.

       Green: D count `2.9` (2 either way), C `("X", 3)`.
     - M5, `stage_job_sort_attempt` drop the base strip in the `elif`. Red: D count `"3"`, count `True`, count `2.9`, count-1-no-stage (each `stage_match` 1 → 0); C `("X_retry_2", 5)`, `("X_retry_2", 0)`, `("X_retry_1_retry_4", 2)`. Green: every T1 row (the `[:2]` rows carry `stage`, and the divergence test reads `[1]` only), D count -1 / `"x"` (the `if` branch strips), C `("X_retry_0", 2)` (`removesuffix` covers it).
     - M4–M6 and SK0 were checked in a triage scratch simulation: the verbatim block was run as the owner and the delegate rebuilt in-process, with no repo edit. The rebuilt delegate equalled `_stage_job_sort_key` on every T1 and D row. The red sets above are the observed ones; SK0 turned nothing red.
     - M6, `stage_job_sort_attempt` clamp the tail (`max(int(...), 0)`). Red: T1 `negative-tail-no-clamp`, `negative-tail-vs-clamp`; C `("X_retry_-3", -1)`.
     - M7, `next_manual_attempt` `+ 1` → `+ 2`. Red: E (4); every F and G row except the three where a pinned value wins (F `new_attempt: 7`, G `:1021` `new_attempt: 5`, G `:1038` rc=7). The three eager-default raise rows (F `:122`, G `:1021`, G `:1038`) stay green: they raise before the `+ 1`.
     - Recorded, not required red: deleting `removesuffix("_retry_0")` in the owner is the SK0 equivalent mutant (stays green).
  5. Per-site mutations, each a one-site revert to the bare value (`next_manual_attempt(X)` → `X`), seven runs:
     - each turns red exactly its own F/G rows (minus the pinned-wins row at `:122`, `:1021` and `:1038`, and minus the eager-default raise rows, which raise either way);
     - hashes restored.

     This proves every site delegates and is reached.
  6. Required existing pins green.
     - The pin list is derived mechanically: 154 node ids at a66dd8306. An awk pass runs over every `tests/*.py` line that names a touched or in-scope function, and attributes the line to its enclosing top-level `def test_`.
       - Per file: 113 in `test_production_scheduler.py`; 30 in `test_file_orchestration_migration.py`; 6 in `test_file_orchestration_journal.py`; 1 each in `test_file_journal_repository_semantics.py`, `test_operator_reentry_confirmation.py`, `test_orchestration_chain.py`, `test_scheduler_held_reservation_block.py` and `test_warm_start_chaining.py`.
       - It includes `test_production_scheduler.py::test_projected_source_cycle_repair_gap_attempt_arithmetic_matches_the_control`, the only direct `_fallback_previous_attempt` caller.
     - The triage's copies, `.workplans/3267/required-pins.txt` and `required-pins.cmd`, are gitignored (`.gitignore:55` `.workplans/`) and exist only in the orchestrator's local checkout. They never reach the PR, CI or node-27. The implementer re-derives the list with the command below, which is the same command as `required-pins.cmd`, inlined here:
       ```bash
       pat='_state_retry_attempt|_fallback_previous_attempt|_manual_retry_new_attempt|_manual_retry_markers|_manual_retry_marker_record|_latest_manual_retry_marker|stage_retry_attempt_floors|STAGE_RETRY_ATTEMPT_FLOORS_KEY|_stage_job_sort_key|_state_non_canonical_stage_retry_attempt|_job_retry_attempt|_manual_retry_requested|_manual_retry_state_evidence'
       for f in $(git grep -lE "$pat" -- 'tests/*.py' | grep -v test_retry_accounting_table.py); do
         awk -v F="$f" -v P="$pat" '/^(async )?def test_/ {match($0,/def test_[A-Za-z0-9_]+/); name=substr($0,RSTART+4,RLENGTH-4)} $0 ~ P && name!="" {print F"::"name}' "$f"
       done | sort -u
       ```
     - Run the derived ids plus `tests/test_retry_mint_floor.py` with `uv run pytest -q`. Record the count of derived ids in the PR body: 154 at a66dd8306, on unchanged test files. A different count means a test file moved, so stop and report.
     - The list is a lower bound: it misses tests that reach these functions only through a helper defined outside a test. The full suite (9) is the completeness evidence.
  7. Residual greps, compared by content, not line:
     - (a) the T2-2 id-construction grep. Baseline at a66dd8306 (run): `retry_identity.py:57,96,170`, `scheduler_state_manual_retry.py:914` (`manual_retry_{key}`, not an id). After: unchanged, because 9.3 builds no id.
     - (b) the T2-2 formula grep. Baseline (run):
       - `chain_runtime_utils.py:454`;
       - `file_orchestration_journal.py:11376,11515,11544`;
       - `retry_identity.py:27,105,115,140,150,169,250`;
       - `scheduler_state_rows.py:886`.

       After: baseline minus `chain_runtime_utils.py:454` (the `"_retry_"` literal). Remaining on purpose: the event-sequence counters, and `scheduler_state_rows.py:886` (a match).
     - (c) the manual grep, the only evidence for these sites (the 9.5 guard does not match them): `git grep -nE '_state_retry_attempt\([^)]*\) *\+ *1|_fallback_previous_attempt\([^)]*\) *\+ *1|previous_attempt *\+ *1' -- services`. Baseline at a66dd8306 (run): exactly `scheduler_state_manual_retry.py:122,132,1021,1035,1037,1038,1039`. After: the seven manual lines gone, and the `next_manual_attempt` body line in `retry_identity.py` added (`previous_attempt + 1`).
     - (d) `git grep -nE '"_retry_0"|removesuffix\("_retry' -- services`. Baseline: `chain_runtime_utils.py:451`. After: only the owner line.

     Any other hit is a missed site.
  8. Facade audit: no governed facade is touched (the Must-preserve `git diff --stat` is empty). `uv run python scripts/governance/audit_repo_entropy.py --format json --structural-base-ref master` → no `compatibility-facade-growth` signal for `services/orchestrator/scheduler.py` or `services/orchestrator/chain.py`.
  9. Full local suite green (proxy env unset); `uv run ruff check . --no-cache`; `openspec validate redesign-orchestration-core --strict --no-interactive`; CI full suite, every shard.
  10. node-27, per D5 "plus node-27 real-DB suites when those suites import the code".
      - The import graph was checked at a66dd8306: each of the 57 `integration`-marked test files was imported in its own fresh `uv run --no-sync python` process, and `sys.modules` was checked for the four touched modules.
      - Nine import all four, transitively: `test_display_coverage_residual_debt_integration.py`, `test_forecast_series_run_identity_pushdown_integration.py`, `test_hydro_run_parsed_at_integration.py`, `test_node27_autopipeline_published_reparse.py`, `test_pipeline_job_provenance_importer_integration.py`, `test_qhh_output_stream_type_integration.py`, `test_qhh_production_bootstrap_scheduler.py`, `test_river_identity_normalization_integration.py` and `test_river_timeseries_stats_index_choice_integration.py`.
      - None of the other 48 `integration`-marked files imports any of the four touched modules: `scheduler_state_rows`, `scheduler_state_manual_retry`, `chain_runtime_utils`, `retry_identity`.
        - Per-file fresh-process check, re-run at a66dd8306: 48 files × 0 modules, 9 × 4, no import error.
        - There is no file that imports only one or two of the four, so the run list is exactly the nine.
        - This includes `test_real_database_integration.py` (0 modules), so T2-2's pair is not this slice's set.
      - On throwaway `nhms_it_*` DBs (`mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`), run `-m integration` over those nine files.
      - Expected: all pass, 0 leftover DBs. Run `df -h / /home` before and after.
  11. node-22 (D5 pure-move batch, task 9.7): ff deploy at a pass boundary, then one idle pass (`status=planned`, `restart_reconcile.status=completed`, `lock.lock_type=file`) and one submitting pass (`status=submitted`), within a batch of at most three merged pure-move PRs or one day.
      - Count 9.2 (#3321) into the batch if its batch has not run.
      - 9.3 is ticked in the final push, with no trailing docs commit.
      - The PR merges with `Part of #3267` until the batch has run.
- Review focus:
  1. Each delegate is equal for every input. In particular:
     - `_state_job_retry_attempt` still coerces every row (the list is materialized) before reading the floor;
     - the non-canonical arm keeps the generator's `if` filter clause ahead of the `_coerce_int` element expression (match-before-coerce; the non-canonical `inf` row stays 0);
     - `stage_attempt` with `floor=None` never clamps;
     - `stage_job_sort_attempt` keeps "count wins, `int()` tail, no clamp, `ValueError` → 0" and returns the base the site's `stage_match` uses;
     - the manual defaults stay eager.
  2. The divergences stay as code paths: the sort-key attempt vs `effective_retry_attempt`, and the unclamped stage-less read and manual `+ 1` vs the 0-floored parsers.
  3. Topology and coercion stay at the sites as module-global lookups: compat propagation, the `chain.py:576` alias, the `inf` raise points.
  4. The four Deviations (Change) are applied as recorded: two extra owner names, the manual-site scope, the non-moves, and the section-3 T1 rows kept on their delegates. No facade file changes.
  5. No T1 literal is edited.

Slice triage (T2-4, #3268, task 9.4):
- Issue type: refactor (pure move; no output changes for any input). Fixture level: expanded (agree). Two expanded triggers: persisted or shared state (`issue-risk-contract.md:22`: the limit gates every retry decision that mints a durable `pipeline_job.job_id`, and the two sets gate which terminal rows get re-run), and the project's orchestrator/state-machine trigger (`openspec/project-profile.md:107`).
  - Minimal mergeable slice: atomic (agree). Two constants and their readers. The limit and the sets share no code and could merge separately, but a split costs a D5 batch slot and buys no verification. Decision: one PR.
- Governing invariant: every site returns the same value as today for every input. The default is 3 everywhere, and each of the two predicates gives the same answer for every status, context and job. The T1 table stays green with every expected value unchanged.
- Recon at HEAD 649f4ee8b (master, after #3322). The production files 9.4 edits are byte-identical to 43d750061, the base of the observations.
  1. The default limit has three definitions:
     - `scheduler_state_types.py:11` `DEFAULT_RETRY_LIMIT = 3`, an `int`.
       - Re-exported by name: `scheduler_state.py:162`, and `scheduler.py:72` in its `# noqa: F401` import list. It is listed in `SCHEDULER_STATE_COMPAT_REEXPORT_NAMES` (`scheduler_state_compat.py:17`).
       - The compat installer does not wrap non-function names. It only checks `facade_reexports[name] is not owner_value` (`:249-251`), which holds for the int `3` whichever module defines it.
       - Readers of the name:
         - `scheduler_config/config.py:175` reads `_scheduler.DEFAULT_RETRY_LIMIT`, a facade attribute read at construction time.
         - `scheduler_state_rows.py` `_state_retry_limit` uses it as the empty-state default, through a function-local import from `scheduler_state_types`.
         - Tests: the `tests/test_retry_accounting_table.py` import plus its limit rows. `tests/test_cohort_member_charge.py:131` names it in a docstring only, which stays true.
       - No test or module monkeypatches it (`git grep -n 'setattr.*DEFAULT_RETRY_LIMIT'` is empty).
     - `retry.py:201` `RetryConfig.max_retries: int = 3`, on a frozen dataclass, evaluated at class creation.
       - `from_settings` (`:204-210`) passes `settings.max_retries`.
       - `RetryConfig()` is called with no argument at `file_orchestration_journal.py:12298` (`config or RetryConfig()`) and at `chain_forecast_orchestrator_cycle.py:124`.
       - The only production caller of `FileJournalRetryService(repository)` with no config is `scripts/node22_manual_retry_failed_runs.py:254`. The scheduler builds the service through `scheduler_core.py:97` with `RetryConfig.from_settings(SlurmGatewaySettings())`.
       - So both defaults are live: the owner constant is the effective default for the script, and the gateway literal is the default for the scheduler.
       - The class is aliased as `chain.RetryConfig` (`chain.py:94`, `chain_compat_static.py:265`), which is not touched.
     - `services/slurm_gateway/config.py:60` `max_retries: int = Field(default=3, ge=0)`, a literal. The env var `SLURM_GATEWAY_MAX_RETRIES` is read through `model_config` (`:65-66`). `slurm_gateway` imports nothing from `services.orchestrator` (`git grep` is empty). The literal stays.
  2. Both env readers stay as they are:
     - `NHMS_SCHEDULER_RETRY_LIMIT`:
       - `scheduler_config/config.py:174-176` `default_factory` calls `_env_int` (`scheduler_runtime_roots.py:754`). Unset, empty and non-int all give the default; anything else gives `int(...)`, so `-1` returns -1.
       - `config.py:479` `max(int(self.retry_limit), 0)` then clamps, so `-1` reads **0** at config construction.
     - `SLURM_GATEWAY_MAX_RETRIES` is read by pydantic with `ge=0`, so `-1` raises a `ValidationError`.
     - The T1 block's limits bullet is corrected in this fixture to "`_env_int` → -1; the config's `retry_limit` → 0 via the `config.py:479` clamp" (#3268 carry-over).
       - Baseline pins:
         - `test_env_int_reads_the_scheduler_retry_limit[negative-accepted]` → -1;
         - `test_scheduler_config_retry_limit_env[negative-accepted-clamped-to-0]` → 0;
         - `test_divergence_negative_limit_scheduler_accepts_gateway_rejects`, which asserts both values and the gateway's `ValidationError`.
       - Observed at 43d750061, with the env unset / `""` / `abc` / `5` / `-1`: `_env_int` gives 3/3/3/5/-1, and `ProductionSchedulerConfig(...).retry_limit` gives 3/3/3/5/0.
       - `_state_retry_limit` gives 3 for `{}`, 0 for `{"retry_limit": "abc"}`, and 2 for `{"retry_limit": 2, "max_retries": 5}`.
  3. The two hard-coded status sets, the only ones #3319 names, are inline `in {...}` displays in `ForecastOrchestratorCycleMixin` static methods in `chain_forecast_orchestrator_cycle.py`. #3319 cites them at `:234/:243`; 9.2 moved them down.
     - `:236`, in `_terminal_stage_needs_manual_retry` (`:216`): `status in {"failed", "submission_failed", "permanently_failed", "cancelled", "partially_failed"}`. It runs after the `reservation_lost` arm (`:218-229`), `_terminal_stage_needs_forced_resubmit` (`:230`) and the `context.retry_attempt is None` check (`:233`).
     - `:245`, in `_terminal_stage_can_retry_after_upstream_refresh` (`:239`): `if status not in {"failed", "submission_failed", "partially_failed"}: return False`. It runs after the `refreshed_upstream_finished_at is None` check (`:243`) and before `_pipeline_job_terminal_time` (`:247`).
     - Both compile to `frozenset` constants (observed in `fn.__code__.co_consts`). So neither set has a name, and no `is` comparison, mutation or monkeypatch can observe it. `str(job.get("status") or "")` keeps the membership test hashable.
     - Callers: `chain_forecast_execution.py:238,248`. Tests that cover them: `test_file_orchestration_journal.py`, `test_forced_resubmit_veto.py`, `test_orchestration_chain.py`, `test_warm_start_chaining.py`, and `test_retry_accounting_table.py`, which drives both over the whole status-vocab union.
     - Relations to the named sets in `retry.py`, which T1 already pins:
       - `:236` equals `MANUAL_RETRY_SOURCE_STATUSES`;
       - `:245` equals `FAILED_RETRY_STATUSES - {"permanently_failed"}`;
       - the two differ by `{permanently_failed, cancelled}`.

       #3319 item 2 owns any change, including replacing `:236` with the named set.
     - Truth tables observed over the 17-status vocab union: manual = {cancelled, failed, partially_failed, permanently_failed, submission_failed}; refresh = {failed, partially_failed, submission_failed}.
  4. Compat propagation (`scheduler_state_compat.py:169-235`) acts only on `_`-prefixed `FunctionType` names. The limit is an int and the sets are inline, so nothing here is patched through the facade.
- Blast radius:
  - Files changed:
    - `services/orchestrator/retry_identity.py`: three constants after `RETRY_JOB_ID_MARKER`, and the owner list in the docstring. It stays stdlib-only, so there is no `status_vocab` import and no cycle.
    - `scheduler_state_types.py:11`.
    - `retry.py:201`, plus one import.
    - `chain_forecast_orchestrator_cycle.py:236,245`, plus the names added to the `:86-93` import block.
    - `tests/test_retry_accounting_table.py`.
    - The umbrella `tasks.md`: the 9.4 tick and wording.
  - Not changed: `slurm_gateway/config.py`, `scheduler_config/config.py`, `scheduler_runtime_roots.py`, `scheduler_state_rows.py` (the function-local import stays), `scheduler.py`, `scheduler_state.py`, `scheduler_state_compat.py`, `chain.py`, both inventories, `docs/runbooks/failed-basin-retry.md` (the default `3` stays true), and `scripts/node22_manual_retry_failed_runs.py`.
  - A wrong move would change a retry budget, either blocking a candidate early or letting it past its limit. It could also let a `permanently_failed` or `cancelled` row be re-run after an upstream refresh (the #2404 / #1845 geometry). Both are persisted-attempt decisions.
- Non-goals:
  - Changing any T1 expected value.
  - Merging the two env knobs.
  - Changing `_state_retry_limit`'s non-int → 0.
  - Replacing `:236` with `MANUAL_RETRY_SOURCE_STATUSES`, or aligning `:245` (#3319 item 2).
  - The AST guard (9.5).
  - The non-master id shape.
- Sibling surfaces that must stay:
  - `retry.py:65-66` `FAILED_RETRY_STATUSES` / `MANUAL_RETRY_SOURCE_STATUSES`. These are mutable `set`s derived from `status_vocab`. Their readers are `file_orchestration_journal.py:12833,12839` and `scripts/node22_manual_retry_failed_runs.py:127`. They duplicate the owner literal by value, and that duplicate stays; T1 pins the two equal.
  - Reported, not moved:
    - `file_orchestration_migration.py:80` `_FAILED_STATUSES` has the same five elements, but it is the migration-only predecessor check (`:1963`).
    - `chain_forecast_execution.py:327` (`{failed, submission_failed, reservation_lost, permanently_failed}`) is a result-status set.
  - `_env_int`, the `config.py:479` clamp, `_state_retry_limit`, and the gateway literal with its `ge=0`.
- Change (9.4):
  1. `retry_identity.py` gains three constants with literal contents only:
     - `DEFAULT_RETRY_LIMIT = 3`;
     - `MANUAL_RETRYABLE_STATUSES = frozenset({"failed", "submission_failed", "permanently_failed", "cancelled", "partially_failed"})`;
     - `UPSTREAM_REFRESH_RETRYABLE_STATUSES = frozenset({"failed", "submission_failed", "partially_failed"})`.
  2. `scheduler_state_types.py:11` replaces the assignment with `from services.orchestrator.retry_identity import DEFAULT_RETRY_LIMIT  # noqa: F401`, following the `scheduler_state.py:156` re-export convention (isort places it after `packages.common`), so the name stays a module attribute for the three re-exports. A bare by-name import fails ruff F401, and `ruff --fix` would delete it. `retry.py:201` becomes `max_retries: int = DEFAULT_RETRY_LIMIT`.
  3. In the cycle file, `:236` becomes `status in MANUAL_RETRYABLE_STATUSES` and `:245` becomes `status not in UPSTREAM_REFRESH_RETRYABLE_STATUSES`. Both names are imported by name in the `:86-93` block, following the file's convention, so they are module globals at the site.
  - Deviations from D2's wording ("the default limit constant; the two status sets, unchanged"):
    - (a) D2 names no owner names, so these three names are new.
    - (b) The sets are `frozenset`s, the same type and value as today's compiled constants.
    - (c) The gateway keeps its literal, and the parity test is what binds it to the owner.
    - (d) `retry.py:65-66` are not redirected.
- Tests: `tests/test_retry_accounting_table.py`, sections 4 and 6.
  - Rule: no expected literal is edited. The existing rows stay as they are: `test_default_limit_constants`, `test_state_retry_limit`, the env, divergence and independence rows, and the three status-set rows.
  - New owner rows use `retry_identity.<name>` attribute access, so on unchanged code only those rows are red, not the whole collection.
  - Delegation to the owner is shown by the owner mutation or by an AST check, not by `is` or `==` on a small int (the T2-1 precedent).
  - Limits, 7 rows:
    - L1 `test_owner_default_retry_limit`: `retry_identity.DEFAULT_RETRY_LIMIT == 3` and `type(...) is int`.
    - L2 `test_retry_config_default_max_retries`: `RetryConfig.__dataclass_fields__["max_retries"].default == 3` and `RetryConfig().max_retries == 3`. It is kept separate from `test_default_limit_constants` so that an owner mutation reaches it; that test asserts `DEFAULT_RETRY_LIMIT` first.
    - L3 `test_gateway_default_equals_owner_limit`, 2 rows:
      - `SlurmGatewaySettings.model_fields["max_retries"].default == retry_identity.DEFAULT_RETRY_LIMIT`, which does not depend on the env;
      - `SlurmGatewaySettings().max_retries == retry_identity.DEFAULT_RETRY_LIMIT`, with both limit env vars cleared.
    - L4 `test_limit_defaults_read_the_owner`, 2 params:
      - in `retry.py`, the `RetryConfig.max_retries` `AnnAssign` value is `ast.Name(id="DEFAULT_RETRY_LIMIT")`, and the module has an `ImportFrom` of it from `services.orchestrator.retry_identity`;
      - in `scheduler_state_types.py`, there is no module-level `Assign`/`AnnAssign` to `DEFAULT_RETRY_LIMIT`, and there is an `ImportFrom` of it from `services.orchestrator.retry_identity`.
    - L5 `test_gateway_config_does_not_import_the_orchestrator`: the AST of `services/slurm_gateway/config.py` has no import of `services.orchestrator`. No other guard enforces this; the layering guard scans only `packages/` and `workers/`.
  - Status sets, 4 owner rows:
    - `MANUAL_RETRYABLE_STATUSES` equals the five-element frozenset, and its type is `frozenset`.
    - The same check for the three-element refresh set.
    - `MANUAL_RETRYABLE_STATUSES == retry.MANUAL_RETRY_SOURCE_STATUSES`.
    - `UPSTREAM_REFRESH_RETRYABLE_STATUSES == retry.FAILED_RETRY_STATUSES - {"permanently_failed"}`.
  - Status sets, 2 site rows, `test_terminal_stage_predicates_read_the_owner_names`:
    - `monkeypatch.setattr(chain_forecast_orchestrator_cycle, "MANUAL_RETRYABLE_STATUSES", frozenset({"succeeded"}))` flips `_terminal_stage_needs_manual_retry`: `succeeded` → True and `failed` → False.
    - The same check for the refresh name.
    - These rows catch a site that reverts to an equal literal, which no value row can see.
  - Status-set delegate rows, 30. They are green on unchanged code, and their values were OBSERVED. They pin the status coercion and the evaluation order, which stay at the site.
    - Manual, 12 rows. Defaults: `context.retry_attempt=1`, `slurm_job_id="4000"`, `stage="forecast"`.
      - True: `failed` with `retry_attempt=0`; `cancelled`; `permanently_failed`.
      - False:
        - `failed` with `retry_attempt=None`;
        - `succeeded`;
        - `status=None`, `"FAILED"`, `" failed"`, status missing, `status=5`;
        - `partially_failed` with `retry_attempt=None`;
        - `reservation_lost` with no `slurm_job_id`.
    - Refresh, 18 rows. Default: `refreshed_upstream_finished_at = 2026-05-01T06:00Z` (R).
      - True: `failed` with no time; `finished_at == R`; `R-1h`; `partially_failed` at `R-1h`.
      - False:
        - refreshed `None` with `failed`;
        - `R+1s`;
        - `submission_failed` at `R+1s`;
        - `permanently_failed` at `R-1h`;
        - `cancelled` with no time;
        - `status=None`, missing, `"FAILED"`, `" failed"`, `5` and `["failed"]` (6 rows);
        - `cancelled` with `finished_at="x"` (the status check runs before the time parse);
        - refreshed `None` with `failed` and `finished_at="x"`.
      - Raises: `failed` with `finished_at="x"` → `ValueError`.
  - New rows total 43: 7 limits, 4 owner set rows, 2 site rows and 30 delegate rows.
    - On unchanged code, 11 are red (L1, L3 ×2, L4 ×2, the 4 owner set rows, the 2 site rows) and 32 are green (L2, L5, the 30 delegate rows). The implementer recounts this and records the observed split.
    - The headers of sections 4 and 6 gain "owner-held default limit and status sets".
- Spec: no delta.
  - The change's `specs/job-retry-mechanism/spec.md:3-21` ADDs the requirement, and it already lists "the default retry-limit constant" and "the manual-retryable and upstream-refresh-retryable status sets".
  - `:17` says the two env overrides keep their current readers, and `:20-21` says the owner "SHALL NOT change its result for any input".
  - The new rows fall under two scenarios: "Historical retry cases are pinned in one table" and "Divergent paths are pinned as they are" (the latter lists the status-set difference).
  - Docs: `docs/runbooks/failed-basin-retry.md:252` names `_terminal_stage_needs_manual_retry` as an entry point, which stays true.
- Must preserve:
  - Every T1 expected value: `git diff master -- tests/test_retry_accounting_table.py` shows only added rows and docstring lines.
  - The default is 3 at every reader, and both env readers are unchanged: `-1` gives -1 from `_env_int`, 0 at config construction, and a `ValidationError` at the gateway.
  - The two predicates keep their evaluation order and the `str(job.get("status") or "")` coercion.
  - `retry_identity.py` imports only the stdlib. `slurm_gateway/config.py` keeps its literal and imports nothing from the orchestrator.
  - This diff is empty: `git diff --stat master -- services/orchestrator/chain.py services/orchestrator/scheduler.py services/orchestrator/scheduler_state.py services/orchestrator/scheduler_state_compat.py services/orchestrator/scheduler_state_rows.py services/orchestrator/scheduler_config services/orchestrator/scheduler_runtime_roots.py services/slurm_gateway`.
- Risk packs:
  - Selected:
    - Legacy compatibility (re-exports, the compat installer, the `RetryConfig` alias). Covered by T1, the existing pins, the owner, parity and AST rows, and the mutations.
    - Config / project setup (the two env readers and the three defaults). Covered by the existing limit rows plus L1-L5.
  - Not selected: Concurrency, Public API, File IO, Schema, Auth, Resource limits, Release, Documentation (the runbook lines stay true), and Error handling (the only raise, the `ValueError` at `:247`, is pinned by one row).
- Required evidence. Predictions come from the code. If an observed set differs, stop and report.
  1. Baseline before any production edit, at the recorded HEAD sha:
     - `uv run pytest -q tests/test_retry_accounting_table.py` passes 321.
     - Add the green rows (L2, L5 and the delegate rows) and run them on unchanged code; they pass with the values above.
     - Hash every production file to be edited.
  2. Pre-change red: add the red rows. Expected failures:
     - an `AttributeError` on `retry_identity.<name>` for L1, L3 and the owner set rows;
     - the site rows fail on `monkeypatch.setattr` of a missing attribute (`raising=True`);
     - L4 fails for both params: `retry.py`'s value is an `ast.Constant`, not an `ast.Name`, and `scheduler_state_types.py` still has the `Assign`.

     Everything else is green.
  3. After the change, the same command passes 321 + 43. Classify every line of the table-file diff as an addition or a docstring change.
  4. Mutations. Apply each one, run it on the table file, then restore the file and confirm its hash.
     - M1, owner `DEFAULT_RETRY_LIMIT` 3 → 4. Predicted red:
       - L1, L2, L3 ×2;
       - `test_default_limit_constants`;
       - the `test_state_retry_limit` empty and blank default rows;
       - the unset, empty and non-int rows of both `test_env_int_reads_the_scheduler_retry_limit` and `test_scheduler_config_retry_limit_env`;
       - `test_divergence_non_int_limit_state_reads_0_env_reads_default`;
       - `test_limit_envs_are_independent_gateway_set`.

       Green: `test_limit_envs_are_independent_scheduler_set` and every gateway env row.
     - M2, gateway literal 3 → 4. Predicted red: L3 ×2, `test_gateway_max_retries_env[unset]`, `test_limit_envs_are_independent_scheduler_set`. Green: L1, L2, `test_default_limit_constants`, `test_limit_envs_are_independent_gateway_set`.
     - M3, revert `RetryConfig.max_retries` to the literal 3: only the `retry.py` param of L4 is red. M4, revert `scheduler_state_types.py:11` to the literal: only the `scheduler_state_types` param of L4 is red.
     - M5, the gateway config imports the owner constant: only L5 is red.
     - M6, `MANUAL_RETRYABLE_STATUSES` drops `"cancelled"`. Predicted red:
       - the owner row and the manual relation row;
       - `test_manual_retry_inline_status_set_equals_manual_retry_source_statuses` and `test_the_two_inline_status_sets_differ`;
       - the manual `cancelled` delegate row.

       The site row stays green, because it replaces the set.
     - M7, `UPSTREAM_REFRESH_RETRYABLE_STATUSES` adds `"permanently_failed"`. Predicted red:
       - the owner row and the refresh relation row;
       - `test_upstream_refresh_inline_status_set_equals_failed_minus_permanently_failed` and `test_the_two_inline_status_sets_differ`;
       - the refresh `permanently_failed` `R-1h` delegate row.
     - M8, swap the two names at `:236` and `:245`.
       - Predicted red:
         - `test_manual_retry_inline_status_set_equals_manual_retry_source_statuses`, `test_upstream_refresh_inline_status_set_equals_failed_minus_permanently_failed` and `test_the_two_inline_status_sets_differ`;
         - both site rows;
         - five delegate rows: manual `cancelled` and manual `permanently_failed` (True → False), refresh `permanently_failed` at `R-1h` and refresh `cancelled` with no time (False → True), and refresh `cancelled` with `finished_at="x"` (False → `ValueError`).
       - Green: the 4 owner set rows.
     - M9, revert one site to the equal inline literal (`:236`, then `:245` separately). Predicted red: only that site's site row. The value rows stay green, which is why the site rows and residual grep (c) exist.
     - Any other red result in the table file is reported, not absorbed. The required pins are not part of the mutation runs; they are evidence item 5, run on unmutated code only.
  5. The required existing pins pass. Derive the list mechanically with the awk pass below, which reads top-level `def test_` lines, and write its output to a local pin list before running it:

     ```bash
     pat='_terminal_stage_needs_manual_retry|_terminal_stage_can_retry_after_upstream_refresh|DEFAULT_RETRY_LIMIT|RetryConfig[(][)]|_state_retry_limit|NHMS_SCHEDULER_RETRY_LIMIT|SLURM_GATEWAY_MAX_RETRIES|FileJournalRetryService[(][A-Za-z_.]*[)]|SCHEDULER_STATE_COMPAT_REEXPORT_NAMES|install_scheduler_state_compat'
     for f in $(git grep -lE "$pat" -- 'tests/*.py' | grep -v test_retry_accounting_table.py); do
       awk -v F="$f" -v P="$pat" '/^(async )?def test_/ {match($0,/def test_[A-Za-z0-9_]+/); name=substr($0,RSTART+4,RLENGTH-4)} $0 ~ P && name!="" {print F"::"name}' "$f"
     done | sort -u
     ```

     - Observed at 43d750061: 29 node ids, and running those ids with `uv run pytest -q` passes 32.
     - Re-derive the list at the PR's HEAD and confirm it is unchanged.
     - The list is a lower bound: it misses tests that reach these names only through helpers or class methods. CI is the completeness gate.
  6. Residual greps over `-- services packages scripts`, compared by content:
     - (a) Limit definitions and literal defaults: `git grep -nE 'DEFAULT_RETRY_LIMIT *=|max_retries: int *=|max_retries *= *Field|(retry_limit|max_retries)[A-Za-z_]* *(: *int)? *= *3( |$)|default=3[,)]'`.
       - Baseline: `retry.py:201`, `scheduler_state_types.py:11`, `slurm_gateway/config.py:60`.
       - After: `scheduler_state_types.py:11` no longer matches. `retry.py:201` still matches, now as `= DEFAULT_RETRY_LIMIT`, which is intended. The owner's `DEFAULT_RETRY_LIMIT = 3` is added. `slurm_gateway/config.py:60` stays on purpose.
     - (b) `DEFAULT_RETRY_LIMIT` readers: `git grep -nE 'DEFAULT_RETRY_LIMIT'`. After: the baseline minus the `scheduler_state_types.py:11` assignment, plus the owner definition, the `scheduler_state_types` and `retry.py` import lines, and `retry.py:201` (`max_retries: int = DEFAULT_RETRY_LIMIT`).
     - (c) Hard-coded retryable sets (`git grep -nE '\{ *"failed", *"submission_failed"'`). Baseline: `packages/common/status_vocab.py:93,95`, `chain_forecast_execution.py:327`, `chain_forecast_orchestrator_cycle.py:236,245`, `file_orchestration_migration.py:80`. After: `:236,:245` are gone and the owner's two literals are added. The other four stay on purpose.
     - (d) Named sets: `git grep -nE '(MANUAL_RETRY_SOURCE_STATUSES|FAILED_RETRY_STATUSES|_FAILED_STATUSES) *='`. Baseline: `file_orchestration_migration.py:80`, `retry.py:65,66`. After: unchanged.
     - Any other hit is a missed site. A `DEFAULT_RETRY_LIMIT` mention in the `retry_identity.py` docstring is an expected extra hit; record it rather than stopping.
  7. Facade audit. Neither governed facade is edited (the Must-preserve `git diff --stat` is empty).
     - `uv run python scripts/governance/audit_repo_entropy.py --format json --structural-base-ref master` must show no `compatibility-facade-growth` signal for `scheduler.py` or `chain.py`.
     - Compare the full finding set with the master baseline.
     - Editing `chain_forecast_orchestrator_cycle.py` shifts lines for the grandfathered refs in `scripts/governance/entropy_audit/line_reference_baseline.json`. Confirm the audit adds no new line-reference finding.
  8. Verification policy (owner decision 2026-11-22): no local full suite.
     - Local: the table, the required pins, mutations M1-M9, `uv run ruff check . --no-cache`, `openspec validate redesign-orchestration-core --strict --no-interactive`, and the residual greps.
     - CI is the completeness gate: the full suite, every shard.
  9. node-27, per D5 ("plus node-27 real-DB suites when those suites import the code").
     - The import graph was checked with a fresh `uv run --no-sync python` process per `integration`-marked file, checking `sys.modules` for the four touched modules (`retry_identity`, `retry`, `scheduler_state_types`, `chain_forecast_orchestrator_cycle`).
     - 16 files import at least one touched module:
       - Nine import `retry_identity`, `retry` and `scheduler_state_types` (two of them also import the cycle module); these are the nine #3267 ran.
       - Seven import `scheduler_state_types` only: `test_model_activation_audit_integration.py`, `test_mvt_national_identity_probe_cycles_integration.py`, `test_mvt_national_identity_probe_digest_integration.py`, `test_mvt_national_identity_probe_integration.py`, `test_real_database_integration.py`, `test_river_ts_read_path_surrogate_keys_coverage_integration.py`, `test_river_ts_read_path_surrogate_keys_integration.py`.
     - Run `-m integration` over all 16 on throwaway `nhms_it_*` DBs (`mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`).
     - Expected: everything passes and 0 DBs are left over. Run `df -h / /home` before and after.
  10. node-22: the D5 pure-move batch, task 9.7. Batch t2b2 (#3320, #3321, #3322) is full, so 9.4 opens the next batch.
      - ff deploy at a pass boundary, then one idle pass (`status=planned`, `restart_reconcile.status=completed`, `lock.lock_type=file`) and one submitting pass (`status=submitted`).
      - 9.4 is ticked in the final push, with no trailing docs commit.
      - The PR merges with `Part of #3268` until the batch has run.
- Review focus:
  1. The default is 3 at every reader, and each reader still resolves its name at the same time:
     - `RetryConfig` at class creation;
     - `_scheduler.DEFAULT_RETRY_LIMIT` at config construction;
     - `_state_retry_limit` through its unchanged function-local import.
  2. The set contents and the predicates' order and coercion are copied verbatim. `:245` stays a deny-list (`not in`) and `:236` stays an allow-list.
  3. The gateway literal is kept, and the parity test both binds it and checks layering. The owner stays stdlib-only.
  4. The env readers and the `-1` handling are unchanged.
  5. The duplicates that stay are recorded as deliberate: `retry.py:65-66`, `file_orchestration_migration.py:80`, `chain_forecast_execution.py:327`. #3319 owns any unification.
  6. No T1 literal is edited, and no governed facade file is touched.

Slice triage (T2-5, #3269, tasks 9.5, 9.7):
- Issue type: test-only. The slice adds one AST guard test and a change-local spec/design reword. It changes no production file and no T1 value.
- Fixture level: expanded, as the issue says ("the guard pins the owner boundary of persisted retry identity"). The guard decides which future PRs may build persisted job ids locally.
- Minimal mergeable slice: atomic. It lands only because HEAD has 0 hits.
- Depends on #3265–#3268, all merged; HEAD is b494dc9a9.
- Governing invariant: outside `services/orchestrator/retry_identity.py`, no production module builds a retry job id from `_retry_` / `RETRY_JOB_ID_MARKER`, splits on that marker, or adds an int to a retry reader's result. The allowlist is empty.
- Recon at b494dc9a9:
  1. Marker references outside the owner that are parse or membership uses, not construction:
     - `scheduler_state_rows.py:883` `"_retry_" in job_id`;
     - `operator_action_listing_held.py:100` `re.compile(r"_retry_\d+$")`;
     - `scheduler_state_manual_retry.py:914` `f"manual_retry_{key}"`, which is not an id.

     No production file outside the owner names `RETRY_JOB_ID_MARKER`.
  2. Reader results used outside the owner without `+ int`, which is legal: `chain_forecast_execution.py:651` (a comparison), `:871` (an assignment), `file_orchestration_journal.py:6440` (`max`), and `scheduler_state_rows.py:596,644,691`.
  3. The scheduler-state readers behind the 9.3 sites are not owner functions: `_state_retry_attempt` (`scheduler_state_rows.py:517`), `_job_retry_attempt` (`scheduler_state_rows.py:633`), `_fallback_previous_attempt` (`scheduler_state_manual_retry.py:964`), and `_retry_attempt_from_basins` (`chain_runtime_utils.py:354`; `chain_forecast_control.py:60` is a late-binding forwarder of the same name). Their family (`_state_job_retry_attempt` `:608`, `_state_non_canonical_stage_retry_attempt` `:584`, `_state_flat_retry_attempt` `:601`, `_candidate_manual_retry_attempt` `scheduler.py:652`, `_manual_retry_new_attempt` `scheduler_state_manual_retry.py:1016`) is likewise private. Without them in the reader set, the guard misses `_fallback_previous_attempt(...) + 1`, which is the 9.3 `+ 1` geometry.
- Guard design: `tests/test_retry_accounting_ast_guard.py`, following the house pattern of `tests/test_import_layering_guard.py`.
  - Module docstring with scope and rules; `REPO_ROOT`; `_SCAN_ROOTS = ("apps", "db", "packages", "scripts", "services", "workers")`.
  - `_EXCLUDED_DIR_PARTS` (`__pycache__ .venv node_modules .mypy_cache .pytest_cache`).
  - A pure `find_hits(source) -> list[(lineno, kind)]`, plus a repo walker. A `SyntaxError` surfaces.
  - `tests/` is out of scope, because T1 and the owner tests build ids on purpose.
  - `services/orchestrator/retry_identity.py` is excluded by exact repo-relative path. It is the only exclusion.
  - The allowlist is empty, so there is no allowlist file.
  - Marker (current rules, after fix passes 1-2 of PR #3324). Any of:
    - a str constant that starts with `_retry_` followed by `%`, `{`, a digit, or the end of the string;
    - a str constant containing a `%` placeholder (with optional key, flags, width and precision) or a `}` immediately before such a `_retry_`;
    - the name `RETRY_JOB_ID_MARKER`, or an attribute with that name;
    - a scoped alias: an assignment or annotated assignment of a marker or template constant (`X = "_retry_"`, `TPL = "{}_retry_{}"`, `X = RETRY_JOB_ID_MARKER`), or `from ... import RETRY_JOB_ID_MARKER as M`. A module-level alias holds everywhere; a function-level alias holds in that function and the functions nested in it.

    Field names such as `"_retry_count"`, `f"{stage}_retry_count"`, `prefix + "_retry_attempt_floor"`, `"{}_retry_limit".format(x)` and `k.removesuffix("_retry_count")` are therefore not markers.
  - Violation kinds:

    | Kind | Matches |
    |---|---|
    | `id-fstring` | an f-string with a marker `FormattedValue`, or a constant part that fully matches `_retry_\d*` (including `_retry_` followed by a `FormattedValue`). An f-string in the pattern position (`args[0]` / `pattern=`) of `re.compile` / `match` / `search` / `fullmatch` is exempt. |
    | `id-concat` | `BinOp` `Add` with a marker operand; `Mod` with a marker template, or with a marker as a direct Tuple element or Dict value of the right operand (any left side) |
    | `id-format` | `.format` / `.format_map` whose receiver is a marker, or with a marker as a positional argument, a keyword value, a `**{...}` Dict value or a `format_map` Dict value |
    | `id-join` | `join` with the marker as receiver, or as a direct argument element. Elements are reached through List/Tuple/Set literals, `BinOp`, `IfExp` and `Starred`, but never through generators, comprehensions or calls. |
    | `id-augassign` | `x += <marker>` |
    | `marker-split` | `rsplit` / `split` / `partition` / `rpartition` / `removesuffix` / `rstrip` with a marker as first argument or as `sep=` |
    | `parser+int` | `BinOp Add` of an int literal and an operand that derives from a reader, in either order |
    | `parser+=int` | `AugAssign Add` of an int on a name that derives from a reader |

  - "Derives from a reader" means any of these appears in the operand subtree (`ast.walk`):
    - a call to a reader;
    - a `marker-split` call;
    - a name assigned from such an expression earlier in the same function;
    - a Name receiver of `append` / `extend` / `add` / `insert` / `appendleft` whose argument derives from a reader;
    - the target of a `NamedExpr` whose value derives from a reader.

    Taint is per function. It binds only `ast.Name` targets and the Names inside Tuple/List/Starred targets. The base of an Attribute or Subscript target is never tainted, and closures are not followed. The docstring states this limit.
  - Reader set, which needs no hand-maintained owner list:
    - every public function defined in `retry_identity`: the module's top-level `FunctionDef` names, read from the AST of `retry_identity.py`, not `_`-prefixed (see below);
    - plus every call whose callee name (bare or attribute) contains `attempt`. This covers the four scheduler-state readers and their private family (Recon 3) with no list.
    - Measured at b494dc9a9: 0 hits. A new owner function, or a new `*attempt*` helper, is covered automatically.
    - A meta-test pins the four scheduler readers by module: `_state_retry_attempt` and `_job_retry_attempt` in `scheduler_state_rows`, `_fallback_previous_attempt` in `scheduler_state_manual_retry`, `_retry_attempt_from_basins` in `chain_runtime_utils`. Each still exists and is still matched by the rule.
    - The owner set is enumerated from the module's top-level `FunctionDef` nodes (AST of `retry_identity.py`, not `_`-prefixed), not with `inspect.isfunction`, so a future decorator cannot hide a function.
  - Known misses. The guard is a syntactic heuristic, so this list gives examples and is not exhaustive.
    - The guard catches every form T2 removed (the 26 pre-T2 hits) and the construction, split and increment shapes in the table.
    - Each listed miss has a not-flagged bite row, so tightening one later is a deliberate edit.
    - A miss found later is recorded in the guard and closed by tightening it. It never needs an allowlist entry.
    - `f"{a}x_retry_{n}"`: the marker does not start a constant part. It is accepted so that unrelated text stays clean.
    - A counter read from row data plus one, e.g. `int(source.get("retry_count") or 0) + 1`. This is the pre-T2 non-master formula `file_orchestration_journal.py:12458`. It cannot be told apart from legitimate counters such as `file_orchestration_journal.py:3396` `submission_attempt + 1` and `scheduler_state_manual_retry.py:1127`.
    - A regex-group parse plus one: `int(re.match(r"(.*)_retry_(\d+)$", j).group(2)) + 1`.
    - A late-binding call through `getattr(_chain, "_retry_attempt_from_basins")(...) + 1`. The callee name is a string; `chain_forecast_control.py:61` has this shape today without a `+ 1`.
    - `for` / comprehension targets, closures, and `sum(...)` over reader results.
    - Subscript-store taint: `by[j] = retry_suffix_attempt(j)`, then `max(by.values()) + 1`.
    - `operator.add(reader(...), 1)`.
    - `rfind` / `index` plus slicing, e.g. `int(j[j.rfind("_retry_") + 7:]) + 1`.
  - Fail-safe over-flag, accepted: `len(<reader>(...)) + 1` flags, because the reader may appear anywhere in the operand subtree.
  - Fix pass 1 (PR #3324 review) widened the matcher to cover:
    - templates that start with the marker (`"_retry_%d" % n`, `"_retry_{}".format(n)`);
    - width, keyed and positional placeholders before `_retry_`;
    - the marker as a direct `%`-tuple, dict or `.format` / `.format_map` argument;
    - splitter `sep=` keywords;
    - module-level template constants.

    It also narrowed three shapes that no longer flag:
    - `join` inspects only its receiver and the direct elements of its argument, so membership filters inside generator arguments no longer flag;
    - function-local aliases are scoped to their function;
    - an f-string in the pattern position (`args[0]` or `pattern=`) of `re.compile` / `match` / `search` / `fullmatch` is a regex, not an id. An f-string in the subject position still flags.

    The `join` narrowing adds two documented misses, each with a not-flagged row: a generator argument (`"".join(str(p) for p in (base, M, n))`) and a `map` argument (`"".join(map(str, [base, M, n]))`).
  - Each failure message is `path:lineno kind`, which names the module and line.
- Scratch run at HEAD b494dc9a9 (`.workplans/3269/matcher.py` with the derived reader set): 0 hits over the scan roots. At 5570a72b4 (pre-T2, through `git show`): 26 hits in 7 files. They cover every T2 id-construction and marker-split site. The `+ 1` at `chain.py:857` is caught only through the container taint above, and `file_orchestration_journal.py:12458` only through the adjacent f-string. `_restart_stage_from_basins`, the 9.3 stage reads and the 9.4 limit/sets carry neither, and are outside the guard's reach. Bite set: 0 bad (`.workplans/3269/matcher.py`, final variant).
  - The 9.1/9.2 id and `+ 1` sites: `chain.py:843`, `chain_forecast_execution.py:655,660`, cycle `:288,289,673(x2),746(x2)`, `chain_runtime_utils.py:449,454,460,463,465`, `file_journal_retry_helpers.py:54,55`, `file_orchestration_journal.py:12459,12678`.
  - The seven 9.3 manual sites: `scheduler_state_manual_retry.py:122,132,1021,1035,1037,1038,1039`.
  - `scheduler_state_rows.py:517-656` is not flagged, because those stage reads use `max` and have no `+ 1`.
- Tests (the guard is the deliverable). No existing test or T1 literal is touched.
  - Repo scan: `find_hits` over every scanned file `== []`. Scan roots exist, and the scanned count is at least 400 (a floor, not an exact number).
  - Bite rows, parametrised, each run through `find_hits` on a source string. Each kind gets at least one must-flag row; near-miss rows must not flag.
    - Must flag:
      - f-string with the literal, with the marker, and with a leading `_retry_` part;
      - concat with the literal, with `RETRY_JOB_ID_MARKER`, and with `retry_identity.RETRY_JOB_ID_MARKER`;
      - `%` and `.format`;
      - join, with the marker as receiver and with the marker in the list;
      - a local alias (`SEP = "_retry_"`) and an import-as;
      - `+=` of the literal;
      - `rsplit` with the literal, `rsplit(RETRY_JOB_ID_MARKER, maxsplit=1)`, `partition` and `split`;
      - `+ 1` over `effective_retry_attempt(...)`, over an attribute call, in reversed order (`1 + _state_retry_attempt(...)`), and over `max(c, retry_suffix_attempt(j), f - 1)`;
      - `split_retry_job_identity(j)[1] + 1`;
      - a tainted local `a + 1` and `a += 1`;
      - `_fallback_previous_attempt(...) + 1`;
      - `int(j.rsplit("_retry_", 1)[1]) + 1`;
      - `next_after_failure_master(j, c)[1] + 1`. Its name has no `attempt`, so only the derived owner set catches it. `next_effective_attempt(...) + 1` is also kept; the name rule catches it too.
    - Must not flag:
      - `f"manual_retry_{key}"`, `"_retry_" in job_id`, `re.compile(r"_retry_\d+$")`;
      - counters (`seq + 1`, `recovered.get(s, 0) + 1`);
      - a reader result passed to `max` or compared with `>`, and `- 1`;
      - `retry_job_id(base, next_effective_attempt(...))`;
      - `a, b = s.split(":")`;
      - `f"decision retry_{x} for manual_retry_{y}"`, `CODE = "retry_strict_warm_start_x"`;
      - taint that would have to cross a function boundary;
      - the documented miss `f"{a}x_retry_{n}"`.
  - Exclusion meta-test: the walker skips exactly `services/orchestrator/retry_identity.py`, and that file exists.
  - Reader meta-test: the AST-derived owner set is non-empty and includes `effective_retry_attempt`. Each of the four scheduler readers is defined in the module named in the Reader-set bullet, and its name is matched by the `attempt` rule.
  - Extra bite rows (from fixture review):
    - must flag: `def f(jobs):\n    attempts = []\n    for j in jobs:\n        attempts.append(retry_suffix_attempt(j))\n    return max(attempts, default=0) + 1`, a walrus `(a := effective_retry_attempt(j)) + 1`, and `_state_job_retry_attempt(s) + 1`;
    - must not flag: `def f(self, j):\n    self.a = effective_retry_attempt(j)\n    return self.count + 1`, `f"{stage}_retry_count"`, `prefix + "_retry_attempt_floor"`, `"{}_retry_limit".format(x)`, `k.removesuffix("_retry_count")`, and one row per documented miss, plus `def f(b, s):\n    for i, fl in enumerate(cohort_retry_attempt_floors(b, s)):\n        n = i + 1`
  - Mutation evidence, run once and logged:
    - Add `job_id = f"{base}_retry_{n}"` to a scanned production file. The repo test goes red and names the file and line. Restore it and check the hash.
    - The same with `attempt = effective_retry_attempt(j, c) + 1`.
    - Replace the AST-derived owner set with an empty set, so only the `attempt` name rule remains. The bite rows for `split_retry_job_identity(j)[1] + 1` and `next_after_failure_master(j, c)[1] + 1` go red. `next_effective_attempt(...) + 1` stays green through the name rule, which is expected.
- Spec and design delta (change-local, `openspec/changes/redesign-orchestration-core/`):
  - `specs/job-retry-mechanism/spec.md`:
    - `:19-20`: "an injected occupied-id predicate for the floored mint" becomes "an injected occupancy predicate for the floored mint" (#3321 carry-over). `mint_attempt(floors, is_occupied)` takes the attempt number, and the site maps it to an id; the result is the same for every input.
    - The guard scenario WHEN clause is widened to the guard's actual reach: f-string, concatenation, `%`/`.format` or `join`, splitting on the marker, and adding an integer literal to the result of any `retry_identity` function, of any call whose name contains `attempt`, or of a marker split. THEN is unchanged; AND is narrowed to owner parser calls, regex matches and membership tests, because splitting on the marker now fails.
  - `design.md` D2 guard bullet: the same widening. It also says the allowlist is empty because T2 left no local site.
  - `tasks.md`:
    - 9.5 is ticked with the wording below (applied in the final push).
    - 9.7 is NOT ticked here. Its evidence is the node-22 batches t2b2 (#3320–#3322, 649f4ee8b) and t2b3 (#3323), and that evidence is not recorded yet. A later PR's tasks.md edit ticks 9.7 citing the evidence files.
    - The PR merges as `Part of #3269`, and the issue closes after 9.7.
- Verification (owner policy 2026-11-22, no local full suite):
  - Local:
    - `uv run pytest -q tests/test_retry_accounting_ast_guard.py tests/test_retry_accounting_table.py`;
    - the repo-wide guards that walk `tests/*.py`: `tests/test_import_layering_guard.py` (contract scan), `tests/test_file_orchestration_journal_facade.py`, `tests/test_file_journal_cluster_map.py` and `tests/test_ci_shard_tests.py`;
    - `uv run ruff check . --no-cache`;
    - `openspec validate redesign-orchestration-core --strict --no-interactive`;
    - the mutations above.
  - CI: the full suite, every shard.
  - node-27: not needed. No integration-marked file imports the new test, and the test opens no DB.
  - node-22: not needed. The slice is test-only, so it takes no D5 batch slot (`design.md:226-227`).
    - D5 `:230` lists T2 as pure-move. This guard-only slice is the exception, and the PR body records it.
    - The PR body also records that 9.7 is left unticked, against the issue AC "tick this slice's tasks".
- Must preserve:
  - `git diff --stat master -- services apps packages workers scripts db` is empty;
  - `git diff master -- tests/test_retry_accounting_table.py` is empty;
  - `retry_identity.py` is untouched.
- Risk packs: Legacy compatibility (the guard encodes the owner boundary; covered by the scan, the bite rows and the mutations). Not selected: runtime packs (no runtime change).
- Review focus:
  1. The derived owner reader set and the four scheduler readers: a new owner function must be covered with no edit.
  2. Each kind has a must-flag row and a near-miss row. Every listed miss has a not-flagged row, and the list is stated as non-exhaustive.
  3. Taint stays per function and does not over-flag counters.
  4. The spec and design wording matches what the guard enforces.
  5. 9.7 is not ticked without recorded batch evidence.

## 10. F1 — Compat-defined production helpers move to owners; facade resolution guard

- [x] 10.1 Move `_now`, `_sleep`, `_bounded_evidence_payload` and `_evidence_status` (`scheduler_compat_runtime.py:195,208,311,315`) into a scheduler owner module. Keep the `_source_*` / `_accepted_horizon_from_hours` aliases (`:72-75`) as explicit aliases in `scheduler.py`. Move `_utcnow` (`chain_compat_runtime.py:162-163`, nested in the installer) into a chain owner module. Each facade imports the moved names explicitly. Owners keep reading them through `_scheduler.` / `_chain.`. The entropy facade guard flags every new `_`-prefixed facade import (`scripts/governance/entropy_audit/facade_guard.py:110-142,505-568`), so add a Guard Hook Seed row for each new name to the matching compatibility inventory, with removal condition "deleted with the inventory in F3".
- [x] 10.2 Add `tests/test_facade_name_resolution.py`. It statically collects every attribute read through `_scheduler.` / `_chain.` in `services/orchestrator/*.py`, and every name patched or read on `scheduler` / `chain` in `tests/`, and asserts that each one resolves on the imported facade.
- [ ] 10.3 Verification: CI, then node-22 batch passes per D5.

Suggested fixture level: expanded - moves helpers every scheduler pass calls.
Minimal mergeable slice: 10.2 alone first (the guard is green today); then 10.1 as a pure-move PR.

Slice triage (F1-1, #3270, task 10.2):
- Issue type: test. Fixture level: compact (agree). The PR adds one test file and changes no production code.
  - Width: one PR. Atomic, and green today: the installers still run when the facades are imported, so every name resolves.
- Purpose: this is the seam that lets F2 (task 11) delete the installers. After F2, a name that an owner reads through a facade, or that a test patches on it, must exist on the facade by an explicit import. If it does not, the read raises `AttributeError`, and a `raising=False` patch silently does nothing.
- Recon at a306fd5e4:
  - Owner modules bind facades as `from services.orchestrator import scheduler as _scheduler` (18 modules, including `scheduler_config/`) and `from services.orchestrator import chain as _chain` (11).
  - Tests bind them under many aliases:
    - `scheduler_module` (42 imports);
    - `legacy_chain` (24);
    - `chain_runtime`, `_chain_module`, `_chain`, `chain_module`, `chain_facade` and `chain`;
    - a bare `import services.orchestrator.chain`.
  - String targets like `"services.orchestrator.scheduler._now"` appear about 95 times over 15 distinct targets, some with nested attributes (`chain.time.monotonic`).
  - `from services.orchestrator.(scheduler|chain) import X`: 122 sites.
  - `raising=False` on these surfaces: 128.
  - `assert not hasattr(<facade alias>, ...)`: 10.
- The guard (`tests/test_facade_name_resolution.py`): AST-based and alias-aware per module, scanning `services/orchestrator/**/*.py` and `tests/**/*.py`.
  - What counts as a facade binding:
    - `from services.orchestrator import scheduler|chain [as A]`;
    - `import services.orchestrator.scheduler|chain [as A]`;
    - the bare dotted form `services.orchestrator.chain.X`.
  - Collected reads, keyed on the first attribute after the facade:
    - `A.X` loads and stores;
    - `getattr(A, "X")` with no default;
    - `setattr` / `monkeypatch.setattr` / `monkeypatch.delattr` / `mock.patch.object` / `patch.object` on `(A, "X", …)`;
    - string targets `"services.orchestrator.scheduler.X[.…]"` / `"…chain.X[.…]"` in `monkeypatch.setattr` / `monkeypatch.delattr` / `mock.patch` / `patch`;
    - `from services.orchestrator.scheduler|chain import X`.
    - Owner string dispatch: in `services/orchestrator/**`, a module-local function whose body calls `getattr(A, <its own parameter>)` (today `_scheduler_call` at `scheduler_candidate_manifest.py:21` and `scheduler_candidate_execution_evidence.py:37`). Each call to it with a literal first argument is a read of that name. 16 of its 29 literal names exist on the facade only through `scheduler_candidate_runtime.EXPORTS`, which 11.2 deletes.
    - A facade's imports from its own owner modules are not reads.
  - Not collected, since these are probes and not reads:
    - `hasattr(A, "X")`;
    - `getattr(A, "X", default)`;
    - a non-literal name in `tests/` (counted and reported; such a read raises on its own once the name goes).
  - In `services/orchestrator/**`, a non-literal `getattr`/`setattr` on a facade binding that is not a recognised dispatch wrapper fails the guard. This is green today: the two wrapper sites are the only ones.
  - `raising=False` patches ARE collected, because they are exactly the ones that go silent after F2. A target that does not resolve today goes into an exemption table inside the test only if the test deliberately injects a new attribute, and each entry carries a written reason and its site. The exemption table is empty at a306fd5e4. Any entry that resolves, or whose site is gone, fails the guard.
  - Assertion: `hasattr(importlib.import_module("services.orchestrator.scheduler"|"…chain"), X)` holds for every collected name. A failure lists every unresolved `(facade, name, file:line)`.
  - Meta-tests:
    1. Bite rows. Synthetic sources are run through the collector. A row must flag each read form above with a name that does not exist. A local wrapper plus `_scheduler_call("_no_such_name_3270")` is flagged, and a bare `getattr(_scheduler, name)` in an owner source outside a wrapper is flagged. A row must not flag `hasattr`, a `getattr` default, a non-facade alias of the same spelling, or a name that resolves.
    2. Coverage. Each facade yields at least one name in each of these forms: attribute load, from-import, literal `getattr`, `setattr(A, "X")` / `monkeypatch.setattr(A, "X")`, and string-target setattr. Owner string dispatch yields at least one on scheduler. Store, `delattr`, `patch.object`, `mock.patch` and bare-dotted have no AST site at a306fd5e4 and are covered only by bite rows. The counts are logged.
- Out of scope: production code; the F1 helper moves (10.1); the F2 installer deletion.
- Carry-over (orchestrator, in this PR's final push): tick 5.5 for #3315 / PR #3327 (`.workplans/3315/carry-overs.md`).
- Must preserve: no production file changes. `git diff --stat master -- services apps packages workers scripts db` is empty.
- Risk packs: Legacy compatibility (monkeypatch surface). Not selected: everything else.
- Required evidence:
  1. Green run at HEAD, logging the count of collected names per facade and per read form, plus the exemption table (expected empty).
  2. Mutation A (F2's own operation): in a scratch copy of the tree, replace `chain.py:851` `globals().update(_chain_compat_runtime.install_chain_runtime_compat())` with `pass`. The facade still imports, and the guard is red naming `(chain, _utcnow, services/orchestrator/chain_forecast_orchestrator_cycle.py:443)` and the other `_utcnow` owner sites. The working tree is not touched.
  3. Mutation B: in a scratch copy of `scheduler_candidate_manifest.py`, add `_scheduler_call("_no_such_name_3270")`; the guard is red naming `(scheduler, _no_such_name_3270, file:line)`.
  4. The bite-row results.
  5. ruff, `git diff --check`, `openspec validate`, and uv.lock unchanged.
- Verification (owner policy 2026-11-22: no local full suite):
  - Locally, run the new file plus these repo-wide guards (`test_import_layering_guard.py` and `test_retry_accounting_ast_guard.py` do not scan `tests/`; they are run as a cheap regression check):
    - `tests/test_import_layering_guard.py`
    - `tests/test_file_orchestration_journal_facade.py`
    - `tests/test_entropy_audit_facade_guard_aliases.py`
    - `tests/test_entropy_audit_facade_guard_forwarders.py`
    - `tests/test_retry_accounting_ast_guard.py`
    - `tests/test_ci_shard_tests.py`
  - CI's full sharded suite is the gate. Test-only, so no node-27 or node-22 run (D5).

Slice triage (F1-2, #3271, tasks 10.1, 10.3):
- Issue type: pure move. Fixture level: expanded (agree). Every scheduler pass calls these helpers, and the move changes the import surface of both facades.
  - Width: one PR, atomic. It moves five helpers and adds explicit facade bindings.
- Recon at 4330718cc. Task 10.1's `chain_compat_runtime.py:209` is stale; the location is `:162-163`.
  - `scheduler_compat_runtime.py` starts with `globals().update(_scheduler.__dict__)` (`:6`). Every free name in its bodies is therefore a snapshot copy of the facade's dict at import time, not a live facade read.
    - `_bounded_evidence_payload` (`:195-205`) reads `MAX_EVIDENCE_BYTES` and `_scheduler_evidence.bounded_evidence_payload`.
    - `_evidence_status` (`:208-209`) reads `_scheduler_evidence.evidence_status`.
    - `_now` (`:311-312`) reads `datetime` and `UTC`.
    - `_sleep` (`:315-318`) does a local `import time`.
    - All four are exported through `EXPORTS` (`:361-364`) and installed by `scheduler.py:671`.
    - The two evidence wrappers are named in `_SCHEDULER_EVIDENCE_COMPAT_WRAPPER_OWNER_NAMES` (`:248-249`) and in `_SCHEDULER_EVIDENCE_COMPAT_FACADE_WRAPPERS` (`{name: globals()[name] …}`).
    - The `_source_*` / `_accepted_horizon_from_hours` aliases are at `:72-75`.
  - `chain_compat_runtime.install_chain_runtime_compat` defines `_utcnow` inside the installer (`:162-163`, body `_chain.datetime.now(_chain.UTC)`), sets `_chain.__dict__["_utcnow"]` (`:165`) and exports the name (`:481`).
    - The installer calls `_chain._array_accounting_dependencies()` at `:253`, which reads `_utcnow` (`chain.py:670`), and it reads `_chain.__dict__["_utcnow"]` at `:258`.
    - `chain.py:851` runs the installer. The owners (`chain_forecast_*`) read `_chain._utcnow`.
  - `scheduler_runtime.py:126,174,263` already define `_bounded_evidence_payload`, `_evidence_status` and `_now` as `getattr(_scheduler, …)` forwarders. That rules `scheduler_runtime.py` out as a target, because a facade import from it would recurse.
  - `tests/test_time_helper_consolidation.py` counts module-level `def`s matching `_?utc_?now` against `tests/time_helper_ceiling.json` (`ceiling: 0`).
- Targets and bodies:
  - `_bounded_evidence_payload` and `_evidence_status` go into `scheduler_evidence.py`. That module owns `MAX_EVIDENCE_BYTES`, `bounded_evidence_payload` and `evidence_status`, and it does not import the facade.
    - `_bounded_evidence_payload(payload, *, reason, max_evidence_bytes=None)` returns `bounded_evidence_payload(payload, reason=reason, max_evidence_bytes=MAX_EVIDENCE_BYTES if max_evidence_bytes is None else max_evidence_bytes)`.
    - `_evidence_status(evidence, fallback)` returns `evidence_status(evidence, fallback)`.
    - Both stay `def` wrappers that read module globals at call time, not `x = y` aliases. That way the owner patches at `tests/test_production_scheduler.py:2187` and `:21421` are still observed.
  - `_now` and `_sleep` go into `scheduler_timing.py`, which has stdlib imports only. The bodies are verbatim except that `_now`'s `config` parameter is annotated `Any`, so the module's imports are unchanged. A `TYPE_CHECKING` import of `ProductionSchedulerConfig` was tried and rejected: the static import-closure scan in `tests/test_node27_connection_attribution_delegated.py` follows `TYPE_CHECKING` blocks, and it would count the DB connect closure as widened.
  - `_utcnow` goes into `chain_runtime_utils.py` as `from packages.common.contracts.time_normalization import utc_now as _utcnow  # noqa: F401 - re-exported to the chain facade`, on its own line next to that module's `format_iso_utc as _format_time` import. It is unused inside the module, so without the `noqa` ruff reports F401, and `ruff --fix` would delete it and break `import services.orchestrator.chain`.
    - It must not be a module-level `def`: the time-helper ceiling would count it, whereas the current nested `_utcnow` is not counted.
    - `utc_now` returns `datetime.now(UTC)` (`time_normalization.py:213-219`).
    - `chain.py` binds it with `_utcnow = chain_runtime_utils._utcnow` in the alias block at `chain.py:576-632`.
- Must preserve:
  - The patch targets that tests and production actually use observe the same binding before and after. The implementer writes one row per (helper, free name) with the old binding, the new binding, and the patch targets in use that affect it.
  - Recon backing this, re-grepped by the implementer:
    - `MAX_EVIDENCE_BYTES`: the roughly 9 `scheduler.MAX_EVIDENCE_BYTES` facade patches do not reach this helper, either before (compat snapshot) or after (`scheduler_evidence` global). No test patches `scheduler_evidence.MAX_EVIDENCE_BYTES`.
    - `bounded_evidence_payload` / `evidence_status`: a patch on `scheduler_evidence` is observed both before and after.
    - `datetime` / `UTC`: no test patches `chain.datetime`, `chain.UTC`, `scheduler.datetime`, `scheduler_timing.*` or `time_normalization.*`, and no test uses freezegun.
    - `"services.orchestrator.chain.time.sleep"` patches the global `time` module, which `_sleep` sees both before and after.
  - Owners keep reading `_scheduler._now`, `_scheduler._sleep`, `_scheduler._bounded_evidence_payload`, `_scheduler._evidence_status` and `_chain._utcnow` through the facade, so the roughly 15 `"services.orchestrator.scheduler._now"`-style patches keep working.
  - The facades bind the owners' objects: `scheduler._now is scheduler_timing._now`, `chain._utcnow is chain_runtime_utils._utcnow`, and likewise for the rest.
  - The compat tables keep their content, and every compat-table test stays green unchanged. The compat module gets the moved names only from its `globals().update(_scheduler.__dict__)` snapshot.
  - The `_source_*` / `_accepted_horizon_from_hours` aliases also become explicit alias lines in `scheduler.py`. The compat module keeps its own copies, because its tables read them.
- Change list:
  - Move the bodies as specified above. Delete them from the compat modules, including the `_chain.__dict__["_utcnow"] = …` line and the `_utcnow` export entry. Drop the four scheduler names from the compat `EXPORTS` tuple (`:361-364`).
  - Add the explicit facade bindings in the top import blocks:
    - `scheduler.py` gets `from services.orchestrator.scheduler_evidence import _bounded_evidence_payload, _evidence_status`, `from services.orchestrator.scheduler_timing import _now, _sleep`, and the four `_source_*` / `_accepted_horizon_from_hours` alias lines, all before `scheduler.py:665`.
    - `chain.py` gets the `_utcnow` alias before `chain.py:849`.
    - The compat module does not import the moved names.
  - Add one single-line Guard Hook Seed row per new facade binding: 8 in `SCHEDULER_COMPATIBILITY_INVENTORY.md` and 1 in `CHAIN_COMPATIBILITY_INVENTORY.md`.
    - Scheduler: `_now` and `_sleep` → `scheduler_timing`; `_bounded_evidence_payload` and `_evidence_status` → `scheduler_evidence`; `_source_forecast_hours`, `_source_policy_identity`, `_source_object_identity` and `_accepted_horizon_from_hours` → `scheduler_candidates`.
    - Chain: `_utcnow` → `chain_runtime_utils`.
    - Coverage is per physical line (`facade_guard.py:551-610`). Template: ``- `_now`: owner `services.orchestrator.scheduler_timing`; retention reason: late-binding facade read and monkeypatch path; removal condition: deleted with the inventory in F3; verification command: `uv run pytest -q tests/test_facade_name_resolution.py tests/test_production_scheduler.py`.`` The chain row uses `tests/test_orchestration_chain.py`.
    - The spec deltas' "no compatibility inventory is edited" is F3's end state. Editing the inventory in F1 is the transition under `design.md:232`, and the PR body says so.
  - Do not write `file.py:NNN` in comments in shipped Python; the hard-line-reference baseline checks for it.
- Out of scope: deleting the compat modules, installers and tables (F2); replacing late binding. `.workplans/3270/carry-overs.md` is for F2.
- Risk packs: Legacy compatibility (facade and monkeypatch surface); Runtime contract (the clock and evidence helpers on every pass). Not selected: schema, concurrency, cross-plane.
- Required evidence:
  1. The free-name table described above.
  2. Identity checks in a fresh process, covering all five helpers and the four aliases.
  3. The F1 guard green (`tests/test_facade_name_resolution.py`). The entropy facade guard shows no `compatibility-facade-growth` (`uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json --structural-base-ref master`).
  4. A mutation. In a scratch tree, delete `_now` from the new `scheduler_timing` import line in `scheduler.py`. `scheduler` still imports, and `tests/test_facade_name_resolution.py` goes red naming `_now`: the getattr read at `scheduler_runtime.py:264` and the `"services.orchestrator.scheduler._now"` patch-string sites (`tests/test_production_scheduler.py:24969` and 14 others). Restore the line and check the hash.
     - Deleting the chain `_utcnow` alias instead makes `import services.orchestrator.chain` raise `NameError` during the installer. Record that as the expected failure mode, not as guard evidence.
  5. ruff, `git diff --check`, `openspec validate`, and uv.lock unchanged.
- Verification (owner policy 2026-11-22: no local full suite):
  - Locally, run:
    - `tests/test_facade_name_resolution.py`
    - `tests/test_production_scheduler.py`
    - `tests/test_orchestration_chain.py`
    - `tests/test_scheduler_evidence_summary_lanes.py`
    - `tests/test_time_helper_consolidation.py`
    - the inventory and facade guards: `tests/test_entropy_audit_facade_guard_aliases.py`, `tests/test_entropy_audit_facade_guard_forwarders.py`, `tests/test_entropy_audit_instruction_inventory.py` and `tests/test_entropy_audit_line_references.py`
    - `tests/test_import_layering_guard.py`
    - `tests/test_orchestrator_first_import.py`
    - every test that names the compat modules or a moved name (`git grep -lE "scheduler_compat_runtime|chain_compat_runtime|scheduler_timing|_utcnow|_bounded_evidence_payload|_evidence_status" -- tests`)
  - CI: the full sharded suite.
  - node-27:
    - First, a fresh-process import check over all integration-marked files at HEAD (the `tasks.md:461` method).
    - Then run `-m integration` over every file that imports the touched modules (at least `tests/test_qhh_production_bootstrap_scheduler.py`) on throwaway `nhms_it_*` DBs, after `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`.
    - Expected: all pass, 0 leftover DBs.
  - node-22: this is a pure move, so it joins the D5 pure-move batch: one idle and one submitting pass per batch of at most 3 merged PRs or one day.
    - The open batch t2b3 holds #3323 (merged 2026-11-22T06:48Z). #3271 joins it if it merges the same day; otherwise it opens a new batch.
    - node-22 is down (NFS mounts), so the PR merges as `Part of #3271`. 10.1 is ticked; 10.3 stays unticked until the batch passes are recorded, as with 9.7 / `Part of #3269`.
- Review focus:
  1. Import placement: both facades and the compat installers import cleanly, and the compat tables are unchanged.
  2. The evidence wrappers read `scheduler_evidence` globals at call time, so the owner patches are observed.
  3. `_utcnow` is an alias of `utc_now`, not a new module-level `def`.
  4. The Guard Hook Seed rows have one physical line each with all metadata, and the audit shows no `compatibility-facade-growth`.

## 11. F2 — Delete installers and `_COMPAT_` tables

- [x] 11.0a Chain side, test-only first: delete the test assertions that read `_CHAIN_*_COMPAT_*` tables (`tests/test_orchestration_chain.py`, including the inventory-text tests that also read tables at :1246, :1286, :12146, :12471; `tests/test_file_journal_read_blocked_consumers.py:255-257`). Inventory-text tests that read no table stay for F3.
- [x] 11.0b Scheduler side, test-only first: delete the `_SCHEDULER_*_COMPAT_*` assertions, including `tests/test_production_scheduler.py:1086-1129` and the inventory-text tests that also read tables (e.g. `:816`). Inventory-text tests that read no table stay for F3.
- [x] 11.0c Scheduler state-compat patch propagation: rewrite the propagation in `scheduler_state_compat.py:169-219` as an explicit owner function, with a test that a patch on the facade is seen by the `scheduler_state_*` owners. The installer still runs.
- [x] 11.1 Chain side.
  - Replace `globals().update(...)` at `chain.py:147-149,887-889` with explicit alias lines for every name the F1 guard requires.
  - Delete `chain_compat_static.py`, `chain_compat_runtime.py` and the chain-manifest forwarder self-check (`chain.py:~185-204`).
  - Add Guard Hook Seed rows to the chain inventory for every new explicit `_`-prefixed alias, with removal condition "deleted with the inventory in F3", so the facade guard stays at zero signals.
- [x] 11.2 Scheduler side.
  - Do the same for `scheduler.py:230-234,238-292,686,688-690`: `scheduler_compat_runtime.py`, `scheduler_candidate_runtime.EXPORTS`, `install_scheduler_state_compat`.
  - The installer is deleted; propagation runs through the owner function from 11.0c.
  - Add Guard Hook Seed rows to the scheduler inventory for every new explicit `_`-prefixed alias, as in 11.1.
  - `scheduler_lease._scheduler_compat_function` (`scheduler_lease.py:46-54`) stays a late-binding lookup, renamed without "compat".
- [x] 11.3 Remove the deleted modules from `FIRST_IMPORT_ALLOWLIST` (`tests/test_orchestrator_first_import.py:38-67`) and the path strings in `tests/test_sqlalchemy_driver_explicit.py:120`, `tests/test_node27_connection_attribution*.py` and `tests/test_file_journal_full_tree_budget_contract.py:374`. (chain #3272, scheduler #3273)
- [x] 11.4 Test: importing `chain` / `scheduler` leaves no `_COMPAT_` name in either namespace, and the F1 guard stays green. (chain #3272, scheduler #3273)
- [x] 11.5 Add the Appendix D F2 spec deltas for each side: chain side `chain-facade-deepening` and `import-layering-guard:23`; scheduler side `scheduler-facade-deepening` and `scheduler-discovery-evidence-owner-compat:31-40`. (chain #3272, scheduler #3273)
- [ ] 11.6 Verification: on node-22, one idle pass and one submitting pass for each PR of this group that changes production code (11.0c, 11.1, 11.2). (chain side code done, #3272; node-22 D5 pending for #3292, #3272, #3273)

Suggested fixture level: expanded - legacy compatibility surface and import-time behaviour.
Minimal mergeable slice: 11.0a and 11.0b (test-only) first; 11.0c next; then 11.1 (chain side) and 11.2 (scheduler side), which are independent; neither facade imports the other's compat modules.

Slice triage (F2-1, #3290, task 11.0a):
- Issue type: test (deletion and retarget). Fixture level: compact (agree): test-only, with no production code change.
  - Width: one PR, covering one assertion family across two test files.
  - D5 (`design.md:226-227`): F2's test-only parts are verified by CI only.
- Recon at c4e1dcd34:
  - `git grep -nE "_CHAIN_[A-Z0-9_]*COMPAT_[A-Z0-9_]*" -- tests` gives 212 lines:
    - 211 in `tests/test_orchestration_chain.py`, in 11 clusters: `:1227-1310`, `:10351-10418`, `:10490-10532`, `:10583`, `:10698-10753`, `:10802-10854`, `:10951-11002`, `:11051-11140`, `:11374-11476`, `:11526-11530`, `:11824`;
    - 1 in `tests/test_file_journal_read_blocked_consumers.py:250-257`.
  - Read forms:
    - direct attribute reads (`legacy_chain._CHAIN_X_COMPAT_Y`);
    - `vars(legacy_chain)` prefix scans;
    - inventory-text token strings (`"…" in inventory_text`);
    - absence probes (`not hasattr(legacy_chain, "<name>")`, `:11106-11112`).
  - The chain compat modules install only `_CHAIN_*` names (`chain_compat_static.py:725`, `chain_compat_runtime.py:427-478`) and write no behaviour. Deleting table assertions therefore removes no coverage of behaviour that 11.1 changes. Chain-side patch propagation is covered by `:10543-10546` and `:10880-10881` (no table read, unchanged) and by the retargeted binding loops below.
  - The issue's and 11.0a's line refs `:12146` and `:12471` are stale at c4e1dcd34: they now point at unrelated tests. The table-reading inventory tests are the 11 clusters above.
- Rules. Every assertion that reads a table gets exactly one label. Only the label names echo R2a (`tasks.md:76-82`); the definitions below govern, and there is no (c).
  - Decision test: replace each table-derived name list with its HEAD literal. The assertion is (b) if either of these holds afterwards:
    - a `_CHAIN_*_COMPAT_*` table, a `vars()` scan or another `_CHAIN_*` attribute is still an operand;
    - the assertion now compares only literals, e.g. `tuple(f for f, _ in bindings) == fields`.

    Otherwise it is (a).
  - **(b) delete.** These assertions are about the table or the self-check: its contents, length or uniqueness, `*_MISSING == ()`, the `vars()` prefix scan, `MappingProxyType`-ness, and `*_FORWARDERS[n] is owner.x` (a tautology, since the table is built from `getattr(owner, owner_name)`). A test left with no assertion is deleted whole.
  - **(a) retarget.** The table only supplies names to a behavioural assertion about owner or facade objects. Replace the table read with a literal tuple or dict copied from the table's HEAD value. Otherwise the assertion stays byte-identical. Cases:
    - `set(chain_stage_execution.__all__) == {"StageExecutionDependencies", *owner_names}`;
    - `StageExecutionDependencies.__dataclass_fields__ == dependency_fields`;
    - `callable(getattr(legacy_chain.ForecastOrchestrator, facade_name))`;
    - `:11454` `is not getattr(chain_manifests, owner_name)`;
    - the worker-adapter dynamic_adapters behaviour loop.
    - `*_FACADE_ALIASES[n] is <owner>.n` (`:10519`, `:10532`, `:11132`) and `:11446` (`FACADE is OWNER`). These tables are import-time snapshots of `chain.__dict__`, so they assert that each facade alias is the owner's object. Retarget them as `getattr(legacy_chain, n) is getattr(<owner>, n)` over the literal names.
    - The dependency-binding loops, all (a):
      - `:1304-1306` (`_array_accounting_dependencies()`);
      - `:10537-10541` and `:10865-10868` (`_chain_stage_execution_dependencies()`);
      - `:11476-11480` (the owner signature accepts each argument);
      - `:11524-11527` and `:11822-11825` (the forwarder passes the facade binding as the owner kwarg).

      The `_CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDINGS` literal is defined once per test.
    - Mechanics:
      - In `(*compat_tokens, ...)` inventory loops (`:10416`, `:10583`, `:10802`, `:10951`, `:11290`, `:11374`, `:11530`), drop only `*compat_tokens` and the scan. Never materialise the scan as literal `_CHAIN_*` strings.
      - A loop or `inventory_text` binding left empty is deleted.
      - Loop headers may drop variables that only fed deleted (b) lines (ruff B007).
      - Where the test already has an `expected_*` literal equal to the table (e.g. worker-adapter `:10698-10704`, persistence `:10984`), reuse it instead of making a new copy.
  - **Kept for F3.** Inventory-text assertions that only check a token string appears in the inventory doc read no table and stay unchanged. Where such a test also has table-reading assertions, only those go.
  - **Kept (absence probe).** The `not hasattr(legacy_chain, "<name>")` assertions (`:11106-11112`) read no table and stay unchanged. They are listed in the after-count residual set.
  - `tests/test_file_journal_read_blocked_consumers.py:250-257` is (b): delete the comment, both imports and the assertion.
    - `ForecastOrchestrator._retry_job_for_stage_result` stays asserted callable by the retry retarget at `:11134-11135`, which uses the literal from the `chain_compat_static.py:301` block.
    - The owner function stays covered at `:270` and `:582`.
  - The `:11072-11074` loop (`"forwarder" not in` over a test-local literal) is vacuous. It goes with the classification table it belongs to, as (b).
  - Behaviour tests are otherwise unchanged. No assertion is weakened, and no behavioural assertion is deleted.
- Out of scope:
  - production code;
  - the scheduler side (11.0b, #3291);
  - inventory-text tests;
  - `tests/test_facade_name_resolution.py`, which only collects fewer names afterwards.
- Carry-overs (`.workplans/3270/carry-overs.md`):
  - #1 and #2 are deferred to the 11.1 fixture; `test_facade_name_resolution.py` is untouched here.
  - #3 has nothing to carry on the chain side, because the compat modules install only `_CHAIN_*`. The scheduler side is 11.0c.
- Must preserve: `git diff --stat master -- services apps packages workers scripts db` is empty. Every behavioural assertion survives, either as it was or retargeted.
  - Deviation: the issue says delete, but assertions whose subject is owner or facade behaviour are retargeted instead, per D3 "behaviour tests are unchanged". This is recorded in the PR body.
- Risk packs: legacy compatibility (no coverage lost beyond the self-check tables F2 deletes). Not selected: everything else.
- Required evidence:
  1. A ledger at `.workplans/3290/ledger.md`. For each touched test, give each table-reading assertion's label, old and new text for each (a), and the literal-equals-HEAD-table check for each (a) literal. The check is `uv run --no-sync python -c "import services.orchestrator.chain as c; assert c.<TABLE> == <literal>"` run at c4e1dcd34, with its output pasted.
  2. Before/after counts:
     - the collected-test count of the two files;
     - `git grep -nE "_CHAIN_[A-Z0-9_]*COMPAT_" -- tests`: 212 lines before, and after exactly the residual set. That set is the inventory-text token literals at `:1254-1258`, `:10373-10377` and `:10962-10976`, plus the absence probes at `:11108-11110`. These line numbers are at c4e1dcd34; the ledger lists the final lines.
     - `git grep -nE "chain_compat_(static|runtime)" -- tests` leaves only `tests/test_orchestrator_first_import.py:41,44` and `tests/test_file_journal_full_tree_budget_contract.py:374`, which are 11.3's scope.
  3. The touched files are green, and so is `tests/test_facade_name_resolution.py`.
  4. Clean runs of `uv run --no-sync ruff check . --no-cache`, `git diff --check` and `openspec validate redesign-orchestration-core --strict --no-interactive`.
- Verification (owner policy 2026-11-22: no local full suite):
  - Locally, run:
    - `tests/test_orchestration_chain.py`
    - `tests/test_file_journal_read_blocked_consumers.py`
    - `tests/test_facade_name_resolution.py`
    - the inventory and facade guards: `tests/test_entropy_audit_facade_guard_aliases.py`, `tests/test_entropy_audit_facade_guard_forwarders.py`, `tests/test_entropy_audit_instruction_inventory.py`
  - CI: the full sharded suite.
  - No node-27 or node-22 run: the change is test-only.

Slice triage (F2-2, #3291, task 11.0b):
- Issue type: test (deletion and retarget). Fixture level: compact (agree). The PR is test-only, with no production code change, and touches one assertion family in two test files. D5 (`design.md:226-227`) makes F2's test-only parts CI only.
- Recon at 06154fbb2:
  - `git grep -nE "_SCHEDULER_[A-Z0-9_]*COMPAT_[A-Z0-9_]*" -- tests` gives 144 lines:
    - 142 in `tests/test_production_scheduler.py`, in clusters `:964-1134`, `:1241-1340`, `:1421-1459`, `:1758-1834`, `:2261-2357`, `:2461` and `:58072`;
    - 2 in `tests/test_scheduler_dbfree_only_mode.py:411-412` (`name not in _SCHEDULER_LEASE_COMPAT_{REEXPORT,LOOKUP}_NAMES`).
  - The issue's `:816` and 11.0b's `:1086-1129` refs have drifted. The table-reading tests are the clusters above, re-grepped by the implementer.
  - Unlike the chain side, the scheduler compat surfaces install behaviour as well as tables:
    - `scheduler_candidate_runtime.EXPORTS` installs 165 non-table names (159 functions, the forwarders);
    - `install_scheduler_state_compat` installs patch-propagating wrappers (`_SCHEDULER_STATE_COMPAT_WRAPPERS`), which 11.0c rewrites as an explicit owner function;
    - `scheduler_compat_runtime.EXPORTS` installs 34 tables plus the 4 aliases that #3271 made explicit.

    A table read can therefore be the only coverage of a forwarder or wrapper's behaviour. The decision test below keeps that coverage as a retarget.
- Rules. Every assertion that reads a table gets exactly one label. The label names echo R2a (`tasks.md:76-82`), but the definitions below govern, and there is no (c). They are the same as F2-1's (#3290 block) with scheduler names:
  - Decision test: substitute each table-derived name list with its HEAD literal. If a `_SCHEDULER_*_COMPAT_*` table, a `vars()` scan or another `_SCHEDULER_*` attribute is still an operand, or the assertion now compares only literals, the label is (b). Otherwise it is (a).
  - **(b) delete.** The subject is the table or the self-check: contents, length or uniqueness, set equality between tables, `*_MISSING == ()`, `*_EXPORTS == tuple(...)`, `vars()` scans, `*_FACADE_{REEXPORTS,ALIASES,DIRECTS,WRAPPERS}[n] is getattr(scheduler_module, n)`, and `*_OWNER_{REEXPORTS,ALIASES,DIRECTS,WRAPPERS}[n] is getattr(owner, …)` / `*_ORIGINALS[n] is owner.n` (tautologies). The `*_FACADE_*` tables are import-time snapshots of the facade namespace. After their deletion, facade-is-owner identity is still asserted by the adjacent `getattr(scheduler_module, n) is getattr(owner, …)` lines (`:993`, `:1047`, `:1124`, `:1328`, `:1805`, `:2307`). The execution, evidence and cancellation wrappers are covered by the literal-driven delegate tests (`:1547`, `:2025`, `:2345`). A test left with no assertion is deleted whole.
  - **(a) retarget.** The table only supplies names to a behavioural assertion about facade or owner objects. Replace the table read with a literal tuple or dict copied from the table's HEAD value; the behavioural assertion otherwise stays byte-identical. Cases:
    - `getattr(scheduler_module, n) is getattr(owner, n)` loops, such as the `direct_names` loop and `:58072`'s discovery alias loop;
    - `_SCHEDULER_STATE_COMPAT_WRAPPERS[n] is getattr(scheduler_module, n)` (`:991`) is the only one. Over the 137 literal wrapper names (the HEAD value of `_SCHEDULER_STATE_COMPAT_WRAPPER_NAMES`) it becomes `getattr(scheduler_module, n) is not getattr(scheduler_state_module, n)` plus `callable(getattr(scheduler_module, n))`. It pins that the facade binding is a distinct wrapper until 11.2 deletes the installer. It is not patch-propagation coverage.
    - `hasattr(owner, n)` and `callable(getattr(scheduler_module.ProductionScheduler, n))` over table names;
    - The patch-propagation checks that take names from a table: lease `:1064` and cancellation `:2357`/`:2460`. No state-compat propagation test exists at HEAD. That test belongs to 11.0c, and carry-over #3 is satisfied there, not here.
  - `tests/test_scheduler_dbfree_only_mode.py:411-412` is (b). Facade and owner absence stay asserted by `:408-410`.
  - Mechanics:
    - The inventory loops over table-derived facade or method names (`:1342`, `:1461`, `:1836`, `:2340`) are (a). Replace the names with the test's local literals, reusing the literal dicts or tuples already introduced for the same test's (a) lines, e.g. `(*alias_owner_names, *forwarder_names)`. The scheduler side has no `vars()`/`compat_tokens` scan. Quoted `"_SCHEDULER_*"` token tuples are kept for F3 unchanged. Never materialise table names as literal `_SCHEDULER_*` strings.
    - A loop or `inventory_text` binding left empty is deleted.
    - Loop headers may drop variables that only fed deleted (b) lines (ruff B007).
    - Where the test already has an `expected_*` literal equal to the table, reuse it.
  - **Kept for F3.** Inventory-text assertions that only check a token string appears in the inventory doc read no table. They stay unchanged. Where such a test also has table-reading assertions, only those go.
  - There are no `_SCHEDULER_*` absence probes at HEAD.
  - Behaviour tests are otherwise unchanged. No assertion is weakened, and no behavioural assertion is deleted.
- Out of scope:
  - production code;
  - the chain side (done in #3330);
  - 11.0c (the state-compat propagation rewrite);
  - inventory-text tests;
  - `tests/test_facade_name_resolution.py`.
- Carry-overs:
  - `.workplans/3270/carry-overs.md` #3 (state-compat patch propagation) belongs to 11.0c.
  - `.workplans/3290/carry-overs.md` #2 applies here too: the 53 token strings kept for F3 name tables that 11.2 deletes. F3 owns them.
- Must preserve: `git diff --stat master -- services apps packages workers scripts db` is empty. Every behavioural assertion survives, either as it was or retargeted.
  - Deviation: the issue says delete. Assertions whose subject is owner or facade behaviour are retargeted, per D3 "behaviour tests are unchanged". This is recorded in the PR body.
- Risk packs: legacy compatibility (no coverage lost beyond the self-check tables). Not selected: everything else.
- Required evidence:
  1. A ledger at `.workplans/3291/ledger.md`. For each touched test it gives each table-reading assertion's label, old and new text for each (a), and the literal-equals-HEAD-table check for each (a) literal. The check is `uv run --no-sync python -c "import services.orchestrator.scheduler as s; assert s.<TABLE> == <literal>"` run at 06154fbb2, with its output pasted.
  2. Before and after counts:
     - the collected-test count of the two files;
     - `git grep -nE "_SCHEDULER_[A-Z0-9_]*COMPAT_" -- tests`: 144 lines before (53 quoted inventory-token lines plus 91 table reads). After, exactly 53 lines remain, all quoted inventory-token strings in `tests/test_production_scheduler.py`. At 06154fbb2 they are at `:1015-1018`, `:1053-1056`, `:1130-1134`, `:1334-1340`, `:1452-1459`, `:1823-1834` and `:2326-2338`; the ledger lists the final lines. `tests/test_scheduler_dbfree_only_mode.py` has 0. No added diff line contains `_SCHEDULER_`. The collected-test count is expected to be unchanged, since no test is left without an assertion.
  3. The touched files green; `tests/test_facade_name_resolution.py` green.
  4. `uv run --no-sync ruff check . --no-cache`, `git diff --check`, and `openspec validate redesign-orchestration-core --strict --no-interactive`.
- Verification (owner policy 2026-11-22: no local full suite):
  - Locally, run:
    - `tests/test_production_scheduler.py`
    - `tests/test_scheduler_dbfree_only_mode.py`
    - `tests/test_facade_name_resolution.py`
    - the inventory and facade guards: `tests/test_entropy_audit_facade_guard_aliases.py`, `tests/test_entropy_audit_facade_guard_forwarders.py` and `tests/test_entropy_audit_instruction_inventory.py`
  - CI: the full sharded suite.
  - No node-27 or node-22 run: the change is test-only.

Slice triage (F2-3, #3292, task 11.0c):
- Issue type: refactor (production code, runtime rebinding). Fixture level: expanded (agree). The mechanism runs on every candidate-state call of every pass. Width: one PR, atomic (one mechanism).
- Recon at cb814a249:
  - The propagation is `install_scheduler_state_compat` (`scheduler_state_compat.py:169-233`), installed by `scheduler.py:227`. Every one of the 137 facade wrappers (`:221-227`) enters `compat_bindings` (`:203-219`). Under one module-global `RLock` (`:183`), that context manager loops over all 137 originals. For each name whose facade binding is neither the original nor its wrapper (`compat_override`, `:185-190`), it `setattr`s the facade value onto every loaded `services.orchestrator.scheduler_state*` module that has the name, plus the owner (`compat_target_modules`, `:192-201`). It restores the old values on exit.
  - In production nothing patches the facade, so every override is `None`. Each state call only pays for the lock and the 137-name loop.
  - Measured dependence (`.workplans/3292/`: `propprobe.py`, `rec.jsonl`, `probe-disable.log`, `probe-files.txt`).
    - The probe is an in-process pytest plugin that replaces the `compat_override` closure cell. It was run over the 51 test files that reference `services.orchestrator.scheduler` (`probe-files.txt`).
    - Record mode: 4457 passed, 20 skipped. Non-`None` overrides came from 45 tests and named only 3 names: `_candidate_state_decision` (168 calls), `_object_manifest_is_missing` (20) and `_state_active_jobs` (1).
    - Disable mode (overrides forced to `None`): 3 failed, 4454 passed. All three failures are in `tests/test_production_scheduler.py`:
      - `test_candidate_state_decision_scheduler_monkeypatch_active_jobs_compat` (`:5470`) patches `_state_active_jobs`;
      - `test_candidate_state_decision_scheduler_monkeypatch_raw_manifest_repair_compat` (`:5500`) patches `_object_manifest_is_missing`;
      - `test_repaired_raw_manifest_downstream_retry_stays_open_for_unknown_codes` (`:29146`) patches `_object_manifest_is_missing` through `_raw_manifest_decision` (`:28873`).
    - The only owner-internal call sites that observe those patches are `scheduler_state_decision.py:138` (`active_jobs = _state_active_jobs(decision_state)`) and `scheduler_state_failure.py:1361` (`missing = _object_manifest_is_missing(candidate, value)`).
    - `_candidate_state_decision` has no caller inside `scheduler_state_*`. Rebinding it there is observed by nothing.
    - The probe sees only facade overrides. The converse dependence, tests that patch the owner module and rely on the direct global lookup at the seam, is `tests/test_production_scheduler.py:13362`, `:13411` and `:13667`, all on `scheduler_state_failure._object_manifest_is_missing`. `:13411` and `:13667` reach `:1361`; `:13362` returns before the probe. An AST scan of `tests/` and `scripts/` finds no other patch of `_state_active_jobs` or `_object_manifest_is_missing` on any module. Among the 137 names, only four are patched on the facade: those three plus `_canonical_downstream_stage` (`:1884`), which does not enter a state wrapper.
  - An existing late-binding precedent is `scheduler_lease._scheduler_compat_function` (`scheduler_lease.py:42-50`). It does `sys.modules.get("services.orchestrator.scheduler")`, then `getattr(..., name, None)`, and falls back when the result is not callable. 11.2 renames it.
- Design (D3: "patch propagation is kept as an explicit function in an owner module. It is late binding, not bookkeeping"):
  - Add one owner function to `services/orchestrator/scheduler_state_common.py`:

    ```python
    _FACADE_MODULE = "services.orchestrator.scheduler"
    _FACADE_OWNER_MODULE = "services.orchestrator.scheduler_state"


    def _facade_seam(name: str, fallback: Callable[..., Any]) -> Callable[..., Any]:
        """Return a monkeypatch on the scheduler facade's ``name`` at call time, else ``fallback``."""
        facade = sys.modules.get(_FACADE_MODULE)
        value = getattr(facade, name, None) if facade is not None else None
        if not callable(value):
            return fallback
        owner = sys.modules.get(_FACADE_OWNER_MODULE)
        unpatched = getattr(owner, name, None) if owner is not None else None
        if getattr(value, "__wrapped__", value) is unpatched:
            return fallback
        return value
    ```

    A facade binding counts as a patch only when it is neither the facade's own re-export of `scheduler_state.<name>` nor the installer's `functools.wraps` wrapper of it. This is the `compat_override` rule (`scheduler_state_compat.py:185-190`).
    - Without the rule, the always-callable wrapper would shadow patches on the owner module. `tests/test_production_scheduler.py:13411` and `:13667` patch `scheduler_state_failure._object_manifest_is_missing` directly and call `_artifact_uri_missing_status`.
    - The rule also holds after 11.2, when the facade binds `scheduler_state.<name>` directly.
    - The docstring says why: the facade is a monkeypatch surface (spec `scheduler-facade-deepening`, "Owner modules SHALL keep reading facade names at call time"), and a patch on the owner module still wins when the facade is unpatched.
    - Precedence (fixture-review probe): unpatched gives the owner; facade-only gives the facade; owner-only gives the owner; both patched gives the facade (as the old `compat_override`); facade absent gives `fallback`.
  - The two seams call through it:
    - `scheduler_state_decision.py:138` becomes `_facade_seam("_state_active_jobs", _state_active_jobs)(decision_state)`;
    - `scheduler_state_failure.py:1361` becomes `_facade_seam("_object_manifest_is_missing", _object_manifest_is_missing)(candidate, value)`.

    Their comments say the name is a facade monkeypatch seam. No other call site changes. The seam list is exactly the measured dependence set.
  - Strip the propagation from `install_scheduler_state_compat`. Delete `compat_override`, `compat_target_modules`, `compat_bindings`, the `RLock` and the `sys`/`contextmanager`/`RLock` imports, plus their 3 returned keys: `_SCHEDULER_STATE_COMPAT_LOCK`, `_scheduler_state_compat_override` and `_scheduler_state_compat_bindings`.
    - The installer still runs and still installs a distinct `functools.wraps` wrapper per name. Each wrapper is now a plain pass-through, `return original(*args, **kwargs)`, so the 11.0b pin at `tests/test_production_scheduler.py:1264-1265` still holds until 11.2.
    - The remaining returned keys and runtime checks (`:235-277`) are unchanged.
  - Lock removal is behaviour-neutral. The wrapped names reached from the cohort thread pool (`scheduler_execution.py:410` → `reservation.py:387`) are pure helpers: `_ensure_utc`, `_format_utc`, `_evidence_safe`, `_candidate_production_identity`, `_candidate_canonical_product_id` and `_redact_secret_manifest_for_evidence`. The I/O-bearing names are wired at pass level (`scheduler_core.py:505-506,867-870`). With every override `None`, the lock serialised calls but guarded no state. A facade patch is now read per call, not `setattr` on shared modules, so no shared mutation is left to guard.
  - `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md:122`, group `scheduler-state-monkeypatch-bindings`, is the one prose edit, made by the implementer in the same PR:
    - drop `_SCHEDULER_STATE_COMPAT_LOCK`, `_scheduler_state_compat_override` and `_scheduler_state_compat_bindings` from the names;
    - say propagation is `scheduler_state_common._facade_seam` at the two seams;
    - keep the group id, the four tokens asserted at `tests/test_production_scheduler.py:1289-1292`, the `:152` selector row and the `:171` Guard Hook Seed row byte-identical.
- New test, in a new file `tests/test_scheduler_state_facade_seam.py`. Its subject is the owner, not the facade wrapper:
  - Patch `services.orchestrator.scheduler._state_active_jobs`, then call the OWNER `scheduler_state_decision._candidate_state_decision` directly. The decision observes the patched jobs, as at `:5470`. This path never touches a facade wrapper, so it proves the owner reads the facade.
  - The same for `_object_manifest_is_missing` through the owner's raw-manifest repair path. Reuse the geometry of `:5500`, built locally; do not import test helpers across files.
  - `_facade_seam` unit cases:
    - the facade absent from `sys.modules` (`monkeypatch.delitem(sys.modules, ..., raising=False)`) returns `fallback`;
    - a non-callable facade attribute returns `fallback`;
    - a callable patch is returned as is;
    - an unpatched facade (its binding is the installer's wrapper of `scheduler_state.<name>`) returns `fallback`, including when `fallback` is a patch on the owner module;
    - with both patched, the facade patch wins.
- Must preserve:
  - Every test outside the new file is unchanged. Unpatched production behaviour is unchanged: same decisions and evidence, and the facade still exposes the same names and wrapper objects.
  - The three dependent tests stay green without edits.
  - A patch on the owner module still wins when the facade is unpatched. `test_object_probe_store_error_is_contained_with_its_own_unsafe_reason` (`:13396`) and `test_file_kind_check_never_converts_a_present_verdict` (`:13647`) stay green without edits.
  - `tests/test_facade_name_resolution.py` stays green.
  - `git diff --stat master` touches only `scheduler_state_common.py`, `scheduler_state_decision.py`, `scheduler_state_failure.py`, `scheduler_state_compat.py`, the new test, the inventory row and the 11.0c tick.
  - Deviation from the issue's PR boundary ("one owner module"): the seam call sites live in two owner modules besides the one that defines the function, plus the one inventory prose row (`docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md:122`; the facade guard reads only the Guard Hook Seed section, `scripts/governance/entropy_audit/facade_guard.py:537-546`). Recorded in the PR body.
- Risk packs: legacy compatibility (monkeypatch surface), runtime rebinding / concurrency (lock removal). Not selected: DB, Slurm, evidence schema.
- Out of scope:
  - deleting the installer or its wrappers (11.2);
  - renaming `scheduler_lease._scheduler_compat_function` (11.2);
  - the chain side;
  - teaching `tests/test_facade_name_resolution.py` the new helper. Its `sys.modules` lookup is not a facade binding, so the guard does not scan it (`tests/test_facade_name_resolution.py:79-80`). Do not replace it with `from services.orchestrator import scheduler`, which the guard would fail and which is a circular import.

    Carry-over for 11.2/F3:
    - unify `_facade_seam` with the lease helper when 11.2 renames it. The lease helper has no unpatched check, which is safe only while lease names are direct re-exports.
    - have the F1 guard see both seams' literal names.
- Required evidence:
  1. Red runs, each with its log in `.workplans/3292/`:
     - revert only the two seam call sites to direct calls: the three dependent tests and both owner-path cases in the new file fail;
     - stub `_facade_seam` to always return `fallback`: the same failures;
     - drop the unpatched check, so the facade binding is returned whenever it is callable: `:13396`, `:13647` and the new owner-patch unit case fail;
     - green with the change.
  2. Negative check: confirm with `git grep -n "compat_bindings\|compat_override\|RLock" -- services/orchestrator/scheduler_state_compat.py` returning nothing.
  3. The four record-mode files green: `tests/test_production_scheduler.py`, `tests/test_completion_scoring_event_evidence.py`, `tests/test_scheduler_terminal_recency.py` and `tests/test_scheduler_backfill.py`. Also green: the new test, `tests/test_facade_name_resolution.py`, `tests/test_orchestrator_first_import.py`, and the inventory and facade guards (`tests/test_entropy_audit_facade_guard_aliases.py`, `tests/test_entropy_audit_facade_guard_forwarders.py`, `tests/test_entropy_audit_instruction_inventory.py`).
  4. `uv run --no-sync ruff check . --no-cache`, `git diff --check`, and `openspec validate redesign-orchestration-core --strict --no-interactive`.
- Verification (owner policy 2026-11-22: no local full suite):
  - Locally: evidence 1-4.
  - CI: the full sharded suite.
  - node-27: the integration-marked files that import a touched module. The fresh-process import check over all 57 integration files at cb814a249 gives 16. The orchestrator runs them on throwaway `nhms_it_*` DBs after the push.
  - node-22 (D5): its own idle pass and submitting pass after the ff deploy at a pass boundary. Pending while node-22's mounts are down; recorded as owed on #3292 like #3311.

Slice triage (F2-4, #3272, tasks 11.1, 11.3/11.4/11.5/11.6 chain side):
- Issue type: refactor (deletion of import-time installers and self-check tables). Fixture level: expanded (agree): legacy compatibility surface and import-time behaviour. Width: one PR, atomic. After #3290 the chain installers have no remaining reader.
- Recon at 0c16f184d:
  - The installers:
    - `chain.py:138-140` imports `chain_compat_static` and does `globals().update(_chain_compat_static.CHAIN_COMPAT_STATIC_EXPORTS)`;
    - `chain.py:850-852` imports `chain_compat_runtime` and does `globals().update(_chain_compat_runtime.install_chain_runtime_compat())`;
    - the facade-local self-check at `chain.py:182-199` covers `_CHAIN_MANIFEST_COMPAT_TOP_LEVEL_FACADE_MISSING` and its `RuntimeError`, plus `_CHAIN_MANIFEST_COMPAT_TOP_LEVEL_FORWARDERS`. `MappingProxyType` (`chain.py:11`) is used only there.
  - What they install, measured in-process: 87 static keys and 49 runtime keys. Every key starts with `_CHAIN_` and contains `_COMPAT_`; no function, class or alias is installed. `chain_compat_runtime.py:6` copies `_chain.__dict__`'s `_CHAIN_*` names into its own namespace. Neither module `setattr`s anything onto `chain` or its classes.
  - Their only behaviour beyond the tables is import-time self-checks: 33 `raise RuntimeError` in `chain_compat_static.py` and 27 in `chain_compat_runtime.py`. They check owner or facade names for missing, drift or callable status.
  - F1 guard (`tests/test_facade_name_resolution.py` `scan_tree()`) at 0c16f184d: 955 chain reads over 172 distinct names. None starts with `_CHAIN_`, and none is supplied only by the installers. So 11.1's "explicit alias lines for every name the F1 guard requires" is the empty set, and no Guard Hook Seed row is added. The implementer re-runs this check after the deletion.
  - Other readers:
    - `tests/test_orchestrator_first_import.py:41-46` has `FIRST_IMPORT_ALLOWLIST` entries for both modules. `:47-49`, `chain_forecast_orchestrator_cycle`, cites `chain.py:889` calling the mixin's `_chain_stage_execution_dependencies()` mid-import, and that call is inside `install_chain_runtime_compat`.
    - `tests/test_file_journal_full_tree_budget_contract.py:371-375` puts `chain_compat_static.py` in `_ALLOWED_SLURM_ID_LOOKUP_SITES`. Its `:260` names `query_pipeline_job_by_slurm_id`.
    - A prose comment at `chain_array_accounting.py:103` cites "the compatibility inventory's `_CHAIN_ARRAY_ACCOUNTING_COMPAT_*` name set".
    - `tests/test_orchestration_chain.py` keeps 28 quoted `_CHAIN_*` token lines, which F3 owns: inventory tokens at `:1256-1260`, `:10457-10461`, `:10918-10932` and absence probes `:11018-11020`. After this slice the probes are trivially true. They stay unchanged for F3; the new 11.4 test subsumes them.
    - `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` names the tables and modules. F3 deletes it.
  - `openspec/specs/import-layering-guard/spec.md:23` requires "`services/orchestrator/chain_compat_runtime.py` SHALL take its cycle-alias owner module from the contract module". The module is deleted.
- Changes:
  1. **11.1, production.**
     - Delete `chain.py:138-140`, `:182-200` (the self-check and its trailing blank line) and `:850-852`, plus the `MappingProxyType` import at `:11`. `_SAFE_ID_RE` and `_SAFE_AREA_RE` at `:201-202` stay; `:522` reads `_SAFE_ID_RE`, and the `# ruff: noqa: F821` at `chain.py:1` would hide a `NameError`. `_publish_quality_state` and `build_model_run_assembly` stay.
     - Delete `services/orchestrator/chain_compat_static.py` and `services/orchestrator/chain_compat_runtime.py`.
     - Rewrite the `chain_array_accounting.py:103` comment so it names no `_COMPAT_` table. The thin wrappers stay: they keep the module-attribute import and monkeypatch paths.
  2. **11.3 (chain).**
     - Remove the two `FIRST_IMPORT_ALLOWLIST` entries.
     - Re-measure each remaining entry whose reason cites the deleted installer. Remove an entry only if that module now imports first cleanly, per the test's own stale-entry rule if it has one; otherwise rewrite the reason to the current cause.
     - Fixture-review probe: `chain_forecast_orchestrator_cycle` still fails first import, with the same error before and after (`cannot import name 'ForecastOrchestratorCycleMixin'`). It stays, with its reason rewritten to: "chain imports ForecastOrchestratorCycleMixin from this module as a ForecastOrchestrator base; a first import re-enters chain through the top-level `from services.orchestrator import chain as _chain` before the mixin class is defined".
     - Remove `chain_compat_static.py` from `_ALLOWED_SLURM_ID_LOOKUP_SITES`.
     - Re-run `git grep -nE "chain_compat_(static|runtime)" -- services scripts tests packages workers apps`. It must be empty, except a comment explaining a removal.
  3. **11.4 (chain).** Add a test function in `tests/test_orchestrator_first_import.py`, reusing its fresh-subprocess helpers (`_run_python` and `_probe`):
     - import `services.orchestrator.chain` in a fresh subprocess, so no earlier import masks the result, and assert that `vars(chain)` has no name containing `_COMPAT_`;
     - assert that neither `services.orchestrator.chain_compat_static` nor `services.orchestrator.chain_compat_runtime` is in `sys.modules` after that import;
     - assert reservation facade-is-owner identity over the literal names `ReservationResult`, `bind_reservation`, `reserve_candidate` and `slurm_comment_for`: `getattr(chain, n) is getattr(services.orchestrator.reservation, n)`. This is carry-over #1 of `.workplans/3290/carry-overs.md`; the guarantee was enforced only by the deleted raise loop `chain_compat_static.py:178-180`.
     - F1 guard green.
  4. **11.5 (chain), spec.**
     - `chain-facade-deepening` is already in the change. Confirm the code satisfies its MODIFIED requirement text; no edit is expected.
     - Add `specs/import-layering-guard/spec.md` to the change: a MODIFIED copy of the requirement "Contract SHALL remove the shims and empty the allowlist" with the `chain_compat_runtime.py` clause removed. The orchestrator writes it before implementation.
  5. **Self-check ledger.** `.workplans/3272/selfchecks.md` covers each of the 60 deleted `raise RuntimeError` sites and the `chain.py:185` one. Each gets one label:
     - (i) "the facade alias line raises `AttributeError` at import if the owner name is gone" (cite the `chain.py` line);
     - (ii) "covered by a named behaviour test" (cite test id);
     - (iii) "covered by the F1 guard";
     - (iv) "uncovered".

     Each (iv) whose subject is behaviour, such as a `ForecastOrchestrator` method that must stay callable or an owner and facade identity, gets a literal assertion in the 11.4 test. Each (iv) whose subject is the table itself is dropped with the table. Nothing is silently lost.

     Labels (i) and (iii) are admissible only for existence checks: an owner or facade name is missing, or not callable. Any other kind of check needs (ii), citing a test that asserts the same relation (`is` or `==`), or (iv). That covers identity, `__all__`, field-fixture, dependency-binding, constructor-signature and `__module__` checks. (iii) is judged against the F1 set after the deletion, which has 160 names, not HEAD's 172.
- Must preserve:
  - Every chain name that resolved at HEAD still resolves to the same object. The exceptions are `_CHAIN_*_COMPAT_*`, `_chain_compat_static`, `_chain_compat_runtime` and `MappingProxyType`. Evidence 2 checks this.
  - All behaviour tests are unchanged, except the allowlist and site-set edits above.
  - The 28 F3 token lines are byte-identical.
  - No `file.py:NNN` line reference appears in shipped Python comments. There is no module-level `_utcnow` def; `chain._utcnow` stays as `chain_runtime_utils._utcnow`.
- Carry-overs:
  - `.workplans/3270/carry-overs.md` #1 is fixed here. The sentence "Such a read raises on its own once the name is gone" in `tests/test_facade_name_resolution.py`'s module docstring is softened to exclude `monkeypatch.setattr(..., raising=False)`. With the installers gone the guard is the only line of defence, so its docstring must be exact. This is a docstring-only edit.
  - `.workplans/3270/carry-overs.md` #2 (silent synthetic forms, 0 HEAD sites) is deferred to F3 (#3274), which owns the guard.
- Ticks: this PR ticks 11.1 only. 11.3-11.5 are shared with the scheduler side; each is annotated "(chain side done, #3272)" and ticked when 11.2 (#3273) lands. 11.6 is ticked only once the node-22 D5 receipts for #3292, #3272 and #3273 all exist (corrected in #3273 per PR #3333 review).
- Out of scope:
  - the scheduler side (11.2, #3273);
  - deleting or editing either compatibility inventory, unless a guard test fails without an edit. If one does, make the minimal edit and record it as a deviation;
  - the F3 token lines and the absence probes;
  - `__module__` rewrites.
- Risk packs: legacy compatibility (import surface), import-time ordering (first-import). Not selected: DB, Slurm, evidence schema, concurrency.
- Required evidence (`.workplans/3272/`):
  1. `selfchecks.md` (change 5).
  2. Namespace diff, each side in its own fresh process.
     - Record, for every key of `vars(chain)` and for every name in `dir(chain.ForecastOrchestrator)` (MRO-resolved via `getattr`), the triple `(type name, __module__, __qualname__)`. Where an owner exists, also record an in-process `is` check against it.
     - Diff HEAD against after. The only removed `vars(chain)` keys are the 138 `_CHAIN_*_COMPAT_*` keys plus `_chain_compat_static`, `_chain_compat_runtime` and `MappingProxyType`.
     - No other key or `ForecastOrchestrator` attribute is added, removed, or changes its record.
  3. F1 guard after the deletion: zero unresolved reads, and no `_CHAIN_` name required.
     - Reads drop from 955 to 835 and distinct names from 172 to 160.
     - The 12 removed names are exactly those read only by the two deleted modules: `MappingProxyType`, `__dict__`, `_canonical_converter_module`, `_chain_stages_module`, `_chain_types_module`, `_tile_publisher_module`, `_tile_publisher_publisher_module`, `_time_normalization_module`, `persistence`, `production_contract`, `reservation` and `retry`.
     - Each removed name except `MappingProxyType` still resolves on `chain`, per evidence 2.
     - Any name the new 11.4 test adds is listed separately.
  4. Red runs. Each is a source edit to `chain.py`, made in the working tree and reverted afterwards, because a rebinding inside the pytest process does not reach the subprocess:
     - re-add `globals().update({"_CHAIN_X_COMPAT_Y": 1})`;
     - separately, add `reserve_candidate = lambda *a, **k: None` near the end of `chain.py`.

     Each makes the new 11.4 test fail. Confirm that `git diff` afterwards contains only the intended change.
  5. `uv run --no-sync ruff check . --no-cache`, `git diff --check`, and `openspec validate redesign-orchestration-core --strict --no-interactive`.
- Verification (owner policy 2026-11-22: no local full suite):
  - Locally:
    - `tests/test_orchestration_chain.py`, the new 11.4 test, `tests/test_facade_name_resolution.py` and `tests/test_orchestrator_first_import.py`;
    - `tests/test_file_journal_full_tree_budget_contract.py` and `tests/test_file_journal_read_blocked_consumers.py`;
    - `tests/test_import_layering_guard.py`;
    - the inventory and facade guards: `tests/test_entropy_audit_facade_guard_aliases.py`, `tests/test_entropy_audit_facade_guard_forwarders.py` and `tests/test_entropy_audit_instruction_inventory.py`;
    - every test file that imports `services.orchestrator.chain_*` or `chain` and names a deleted module (re-grep).
  - CI: the full sharded suite.
  - node-27: the integration-marked files that import `services.orchestrator.chain`, found with a fresh-process import check. The orchestrator runs them after the push.
  - node-22 (D5, 11.6 chain): its own idle pass and submitting pass after the ff deploy. Pending while node-22's mounts are down; #3272 stays open until then.

Slice triage (F2-5, #3273, tasks 11.2, 11.3/11.4/11.5 scheduler side):
- Issue type: refactor, deleting import-time installers and self-check tables. Fixture level: expanded (agree): legacy compatibility surface, import-time behaviour, and the late-binding monkeypatch surface used on every pass. Width: one PR, atomic.
- Recon at a97491fc2 (in-process measurements, `tests/test_facade_name_resolution.py` `scan_tree()`):
  - Installers in `services/orchestrator/scheduler.py`:
    - `:222`/`:227` `install_scheduler_state_compat`. Since #3292 it installs 137 pass-through `functools.wraps` wrappers over names the facade already imports explicitly from `scheduler_state` (153 of the 154 `SCHEDULER_STATE_COMPAT_REEXPORT_NAMES`). The one name not imported explicitly is `_completed_upstream_stage_retry_evidence`, which only the `setdefault` loop installs. The installer also adds its `_SCHEDULER_STATE_COMPAT_*` tables.
    - `:231-282`, facade-local lease tables: `_SCHEDULER_LEASE_COMPAT_{REEXPORT_NAMES,LOOKUP_NAMES,REEXPORT_MISSING,FACADE_MISSING,OWNER_REEXPORTS,FACADE_REEXPORTS,EXPORTS}`, plus their drift `raise`s and loop variables. The lease names themselves are explicit imports.
    - `:673-675`: `globals().update(_scheduler_candidate_runtime.EXPORTS)`. It installs 182 keys: 13 `_COMPAT_` tables and 169 other names (159 functions: 136 defined in `scheduler_candidate_runtime`, whose `__module__` reads `services.orchestrator.scheduler` because of its namespace copy, 21 from `scheduler_preflight`, 1 each from `scheduler_candidates` and `scheduler_discovery`; plus 10 values).
    - `:677-679`: `globals().update(_scheduler_compat_runtime.EXPORTS)`. It installs 38 keys: 34 `_COMPAT_` tables and 4 aliases. The aliases (`_accepted_horizon_from_hours`, `_source_forecast_hours`, `_source_object_identity`, `_source_policy_identity`) were already made explicit by #3271.
  - F1 at a97491fc2: 1817 scheduler reads over 281 distinct names. 115 of the 169 non-table candidate-runtime names are required: 103 defined in `scheduler_candidate_runtime`, 11 in `scheduler_preflight`, 1 in `scheduler_discovery`. 31 of the 137 state-wrapper names are required. All 4 compat-runtime aliases are required, and they are already explicit.
  - `.workplans/3270/carry-overs.md` #3: with all three installers removed and nothing added, the guard reports 528 unresolved reads across 13 owner modules. Removing only the EXPORTS line breaks facade import: `scheduler_compat_runtime` raises `RuntimeError` on `_source_horizon_metadata`. So the lines at `:675` and `:679` go together.
  - F1 scans only `services/orchestrator` and `tests` (`tests/test_facade_name_resolution.py:104`). `apps`, `scripts`, `workers` and `packages` are not scanned.
  - `scheduler_candidate_runtime.py:6` copies the namespace (`globals().update(_scheduler.__dict__)`) when it is imported mid-way through the facade. That is the owner's own snapshot, not facade installation, and it stays: late binding and `__module__` rewrites are out of scope. Importing it explicitly at the same point in `scheduler.py` keeps the timing.
  - `scheduler_lease._scheduler_compat_function` (`scheduler_lease.py:42-50`, callers `:77`, `:344`, `:382`, `:394`, `:398`) is pinned by `tests/test_production_scheduler.py:1352`.
  - Other readers:
    - `tests/test_orchestrator_first_import.py:49-57` has `FIRST_IMPORT_ALLOWLIST` entries for `scheduler_candidate_runtime` and `scheduler_compat_runtime`. Both reasons cite "scheduler.py reads its EXPORTS".
    - `tests/test_production_scheduler.py:963` and `:1264-1265`: the 11.0b pin says the facade binding is a distinct wrapper until 11.2.
    - `tests/test_production_scheduler.py:1512-1564` imports `scheduler_compat_runtime`.
    - `tests/test_production_scheduler.py:54732` imports from `scheduler_candidate_runtime`. That stays.
    - `tests/test_scheduler_state_facade_seam.py`'s wrapper case.
    - `services/orchestrator/scheduler_runtime_roots.py:537` is a comment.
    - `tests/test_hydro_status_set_parity.py:255` is a docstring path.
    - `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` and `STRUCTURAL_FILE_DISPOSITION_INVENTORY.md:47` are prose that F3 owns.
- Changes:
  1. **11.2, production.**
     - Delete `scheduler.py:222` and `:227` and `services/orchestrator/scheduler_state_compat.py`.
     - Add `_completed_upstream_stage_retry_evidence` to the explicit `scheduler_state` import block. It is read by `tests/test_production_scheduler.py:1257`/`:1264`.
     - Delete the lease tables at `scheduler.py:231-282`, including their `raise`s and loop variables. Keep the explicit lease imports.
     - Replace `:673-679` with an explicit `from services.orchestrator.scheduler_candidate_runtime import (...)` block at the same point in the file, over exactly the reader set below.
       - Import a name from `scheduler_preflight` / `scheduler_discovery` / `scheduler_candidates` when its candidate-runtime binding is that module's object (`getattr(owner, v.__name__) is v`; `_source_horizon_metadata` is `source_horizon_metadata as _source_horizon_metadata`). Otherwise import it from `scheduler_candidate_runtime`. That gives 104 + 11 + 1 + 1 = 117 names.
       - `__module__` cannot decide this: the namespace copy rewrites `__name__`, so every candidate-runtime function reports `services.orchestrator.scheduler`.
       - Delete `services/orchestrator/scheduler_compat_runtime.py`.
       - Delete `scheduler_candidate_runtime.EXPORTS` and its 13 `_COMPAT_` tables, plus any self-check that reads only them. Keep every function and value the explicit block or another module reads.
     - Rename `scheduler_lease._scheduler_compat_function` to `_scheduler_facade_function`, a late-binding lookup with no "compat" in the name, and update its 5 callers. Its behaviour is unchanged.
     - **Reader set.** It is the union of:
       - the F1-required scheduler names after the deletion;
       - every name a scanned test reaches through a positive `hasattr(<facade>, ...)` probe, or through a non-literal `getattr`/`setattr` over a literal name tuple. These are the F1 report's non-literal sites; negative probes such as `:1379` do not count. At a97491fc2 this adds `_slurm_status_sync_failed_evidence`, `_execution_mutation_value` and `_completed_upstream_stage_retry_evidence`;
       - bare global loads inside `scheduler.py`'s own `def` bodies that resolve to an installed name. F1 skips them and F821 is noqa'd. Today the only one is `_normalize_allowed_cycle_hours_utc` at `:439`, which is already F1-required;
       - every `scheduler.<name>` / `from services.orchestrator.scheduler import <name>` / `_scheduler.<name>` / string patch target in `apps scripts workers packages` (measured by the fixture review: none);
       - the names in the `scheduler-facade-deepening` public entrypoints.

       Dropping no-reader names is intended (design.md:192 and D6). `openspec/specs/facade-shrink/spec.md:13-20` ("old private names remain importable") is premised on an existing caller. The 10 preflight private names that leave have zero callers; `reader-set.md` says so in one line. No delta is needed.

       A non-table name that is in no reader set leaves the facade. The ledger lists each one, and the PR body records the count.
  2. **`_facade_seam` narrowing** (`.workplans/3292/carry-overs.md` #7. #6 and #10, the aggregator `scheduler_state` being patched, stay as at HEAD. Nothing patches the aggregator, so they are deferred to F3 (#3274) with #1-#2). The installer wrappers are gone, so the unpatched rule becomes identity only: `value is getattr(owner, name, None)`, with no `__wrapped__` unwrapping. A `functools.wraps(original)(spy)` patch on the facade is then seen as a patch. Update `tests/test_scheduler_state_facade_seam.py`:
     - replace `test_facade_seam_treats_installer_wrapper_as_unpatched` with a case where a `functools.wraps(original)` spy counts as a patch;
     - keep the direct re-export case;
     - in `test_facade_seam_owner_module_patch_wins_while_facade_unpatched`, change `facade_binding.__wrapped__ is scheduler_state_module._object_manifest_is_missing` to `facade_binding is scheduler_state_module._object_manifest_is_missing`;
     - rewrite the `_facade_seam` docstring sentence to: "A facade binding counts as a patch only when it is not the facade's own re-export of `scheduler_state.<name>` (identity)."
  3. **Tests (11.3/11.4 scheduler).**
     - `:1264-1265` flips to `getattr(scheduler_module, n) is getattr(scheduler_state_module, n)` over the same 137 literal names.
     - `:963` and `:1512-1564`: retarget any import of a deleted module to the owner, or delete an assertion whose only subject was a deleted table. Use the F2-2 decision test (`tasks.md`, block F2-2).
     - Remove the `scheduler_compat_runtime` `FIRST_IMPORT_ALLOWLIST` entry unconditionally. `test_orchestrator_first_import.py:99` requires the allowlist to be a subset of existing modules.
     - Keep the `scheduler_candidate_runtime` entry, which the fixture review measured as still failing. Rewrite its reason to: "the facade's explicit `from services.orchestrator.scheduler_candidate_runtime import (...)` at its end re-enters this module mid-import (`ImportError: cannot import name ... from partially initialized module`)". Use the actual measured message.
     - The three path-string files named in task 11.3 have no scheduler-side strings (re-grep gives 0).
     - Fix the stale comment at `tests/test_production_scheduler.py:13655-13660` (#3292 carry-over #5).
     - Add an 11.4 scheduler test function to `tests/test_orchestrator_first_import.py`, next to the chain one and reusing `_run_python`/`_probe`. In a fresh subprocess, `import services.orchestrator.scheduler` must leave no name containing `_COMPAT_` in `vars(scheduler)`, and no `services.orchestrator.scheduler_compat_runtime` / `scheduler_state_compat` in `sys.modules`. The F1 guard stays green.
  4. **Guard Hook Seed rows.** Each new explicit `_`-prefixed facade import that the facade guard reports as a signal needs a single-line Guard Hook Seed row in `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md`, with the removal condition "deleted with the inventory in F3".
     - One row per owner module that lists its names in backticks is allowed. A line covers a signal if it contains any of the signal's tokens and carries owner, retention, removal-condition and verification metadata (`scripts/governance/entropy_audit/facade_guard.py:536-580`).
     - The facade guard must report zero signals.
     - No other inventory prose changes.
  5. **11.5 scheduler, spec.**
     - `scheduler-facade-deepening` is already in the change; confirm the code satisfies it. Carry-over #9 of `.workplans/3292/carry-overs.md` (the 2-seam propagation boundary stated in the spec or the guard) is deferred to F3 (#3274).
     - Add `specs/scheduler-discovery-evidence-owner-compat/spec.md` to the change: a MODIFIED copy of the requirement holding the scenario at `:31-40`. It drops only `scheduler_compat_runtime` from the consumer list and adds "through an explicit import or alias line". The orchestrator writes it before implementation.
  6. **Carry-overs applied here.**
     - `.workplans/3272/carry-overs.md` #1: the opening docstring of `tests/test_facade_name_resolution.py` no longer says that either facade has installers.
     - `.workplans/3272/carry-overs.md` #5: `tasks.md` 11.6 is reworded. The orchestrator does this.
     - `.workplans/3272/carry-overs.md` #7: change `tests/test_node27_connection_attribution.py:894` from `chain.py:17` to `:16`, or drop the line number.
  7. **Ledger** `.workplans/3273/selfchecks.md`. The fixture review counted 23 sites: state_compat 3, compat_runtime 11, candidate_runtime 4 (`:357-483`), and `scheduler.py:231-282` 5. Label every deleted `raise RuntimeError` in `scheduler_state_compat.py`, `scheduler_compat_runtime.py`, `scheduler_candidate_runtime.py` (table self-checks) and `scheduler.py:231-282` with the F2-4 labels (i)-(iv), under the F2-4 admissibility rule: (i) and (iii) only for existence checks, and an identity, binding, signature or `__module__` check needs (ii) with a test asserting the same relation, or (iv). Each behavioural (iv) gets a literal assertion in the 11.4 scheduler test.
- Must preserve:
  - Every scheduler name in the reader set resolves to the same object as at HEAD, with one exception: the 137 state names now bind the owner function directly instead of the pass-through wrapper.
  - Unpatched behaviour is identical.
  - The two `_facade_seam` seams behave as in #3292 for every patch form except the `wraps` spy. That form is now observed, which is the intended fix.
  - The lease late-binding lookup behaves the same.
  - The 53 quoted F3 token lines in `tests/test_production_scheduler.py` are byte-identical.
  - No `file.py:NNN` line reference appears in shipped Python. There is no module-level `_utcnow`.
- Out of scope:
  - the chain side (done, #3333);
  - inventories beyond the Guard Hook Seed rows (F3);
  - `scheduler_candidate_runtime`'s own namespace copy and `__module__` values;
  - unifying the lease lookup with `_facade_seam`, and teaching the F1 guard the seam literals. Both are deferred to F3 (#3274) with `.workplans/3292/carry-overs.md` #1-#2.
- Risk packs: legacy compatibility (import and monkeypatch surface); import-time ordering (first import, mid-import snapshot); runtime rebinding (state wrappers removed). Not selected: DB, Slurm, evidence schema.
- Required evidence (`.workplans/3273/`):
  1. `reader-set.md`: the F1-required set before and after, the other-roots reader scan, and the final explicit import list. Names leaving the facade are listed with "no reader" proof.
  2. `namespace-diff.md`. Each side runs in its own fresh process. For every `vars(scheduler)` key and every `dir(scheduler.ProductionScheduler)` name, record `(type name, __module__, __qualname__)`, plus an in-process `is` check against the owner. The only changes allowed are:
     - removed `_scheduler_compat_runtime`, `_scheduler_candidate_runtime`, `install_scheduler_state_compat`, `_scheduler_state_compat_wrapper` and `MappingProxyType` if unused;
     - removed `_COMPAT_` tables, 64 keys in all: 13 candidate-runtime, 34 compat-runtime, 10 state and 7 lease;
     - removed no-reader names (listed);
     - the 137 state names, each changed from wrapper to owner function (`is` owner after).
  3. `f1-counts.log`: zero unresolved reads and no `_COMPAT_` name required, with distinct counts before and after. The fixture review's scratch run measured 1817 → 1816 reads and 281 distinct names for the production-only deletion.
  4. `red-runs.log`, from working-tree source edits that are reverted afterwards:
     - re-add `globals().update({"_SCHEDULER_X_COMPAT_Y": 1})` to `scheduler.py`;
     - drop one required explicit import (F1 must fail);
     - rebind one state name on the facade to a wrapper (the `:1264` identity test must fail);
     - restore `__wrapped__` unwrapping in `_facade_seam` (the new wraps-spy case must fail).
  5. `uv run --no-sync ruff check . --no-cache`, `git diff --check`, and `openspec validate redesign-orchestration-core --strict --no-interactive`.
- Verification (owner policy 2026-11-22: no local full suite):
  - Locally:
    - `tests/test_production_scheduler.py`, `tests/test_scheduler_state_facade_seam.py`, `tests/test_scheduler_backfill.py`, `tests/test_completion_scoring_event_evidence.py` and `tests/test_scheduler_terminal_recency.py`;
    - `tests/test_scheduler_dbfree_only_mode.py`, `tests/test_orchestrator_first_import.py` and `tests/test_facade_name_resolution.py`;
    - the inventory and facade guards: `tests/test_entropy_audit_facade_guard_aliases.py`, `tests/test_entropy_audit_facade_guard_forwarders.py` and `tests/test_entropy_audit_instruction_inventory.py`;
    - `tests/test_operator_action_status_closure.py` and `tests/test_entropy_audit_line_references.py`;
    - every other test file that names a deleted module or a renamed symbol (re-grep).
  - CI: the full sharded suite.
  - node-27: the integration-marked files that import a touched module, found by a fresh-process import check. The orchestrator runs them after the push.
  - node-22 (D5, 11.6 scheduler): its own idle pass and submitting pass after the ff deploy. Pending while node-22's mounts are down; #3273 stays open until then.
- Ticks: this PR's final push ticks 11.2, 11.3, 11.4 and 11.5, because both sides are then done. 11.6 is ticked only when the node-22 D5 receipts for #3292, #3272 and #3273 all exist.

## 12. F3 — Delete the compatibility inventories and the facade guard

- [x] 12.1 Delete `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md`, `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md`, `scripts/governance/entropy_audit/facade_guard.py`, `COMPATIBILITY_FACADE_CONFIGS` (`schema.py:206-217`), and the helpers that use them (`scripts/governance/audit_repo_entropy.py:257-269,408,627,688-694`; `tests/entropy_audit_helpers.py:587,598`).
- [x] 12.2 Delete the facade-specific tests in `tests/test_entropy_audit_facade_guard_{forwarders,aliases}.py`, `tests/test_entropy_audit_instruction_inventory.py`, `tests/test_entropy_audit_report_contract.py` and `tests/test_entropy_audit_structural_budget.py`, and the inventory-text tests (`tests/test_production_scheduler.py:776-848`, `tests/test_orchestration_chain.py:841`).
- [x] 12.3 Remove the `compatibility facade` glossary term (`schema.py:227` `required_glossary_terms`, `openspec/glossary.md:17`). Update `services/orchestrator/AGENTS.md` (regenerate it from `instructions/agents/` if it is generated), `docs/governance/entropy-budget.md`, `entropy-burndown-triage.md:247`, `STRUCTURAL_FILE_DISPOSITION_INVENTORY.md:163-164,229-230` and `GOVERNANCE_8_FINAL_EVIDENCE_MAP.md:18-19`.
- [x] 12.4 Add the REMOVED delta for `compatibility-facade-governance`, the MODIFIED/REMOVED delta for `facade-shrink` ("Compatibility guard remains authoritative"), and the MODIFIED delta for `orchestrator-structural-burndown/spec.md:434-436`. Reword the comment at `services/orchestrator/chain_array_accounting.py:102` that names the inventories. Confirm with `grep -rni "compatibility inventory\|COMPATIBILITY_INVENTORY\|facade guard" docs scripts tests services --exclude='*.jsonl'` that nothing cites them. `openspec/specs/` is excluded because the deltas reach the main specs only at archive, and append-only `*.jsonl` logs are history.
- [x] 12.5 Verification: CI only, with the entropy audit report-only job green.

Suggested fixture level: compact - governance tooling and docs, no runtime code.
Minimal mergeable slice: atomic - the inventories and the guard that reads them go together, or the guard fails on a missing file.

Slice triage (F3-1, #3274, tasks 12.1-12.5):
- Issue type: deletion (governance docs and tooling, their tests, spec deltas). Fixture level: compact (agree). There is no runtime code; the only service-side change is one comment. Width: one PR, atomic. The inventories and the guard that reads them go together.
- Recon at f45e332b2 (`git grep -lni "compatibility inventory\|COMPATIBILITY_INVENTORY\|facade guard\|facade_guard\|COMPATIBILITY_FACADE"`, excluding `*.jsonl`). Hits per file:

  | Group | File | Hits |
  |---|---|---|
  | Tooling | `scripts/governance/entropy_audit/facade_guard.py` | 80 |
  | Tooling | `scripts/governance/audit_repo_entropy.py` | 53 |
  | Tooling | `scripts/governance/entropy_audit/report.py` (`:43-46`, `:82-91`) | 10 |
  | Tooling | `scripts/governance/entropy_audit/schema.py` | 3 |
  | Tooling | `scripts/governance/entropy_audit/constants.py` (`:35` check id, `:142`, `:145`, `:246`) | 3 |
  | Tooling | `scripts/ci/full_test_durations.json` (`:62-63`) | 2 |
  | Tests | `tests/test_entropy_audit_facade_guard_aliases.py` | 89 |
  | Tests | `tests/test_entropy_audit_facade_guard_forwarders.py` | 66 |
  | Tests | `tests/test_entropy_audit_instruction_inventory.py` | 17 |
  | Tests | `tests/entropy_audit_helpers.py` | 12 |
  | Tests | `tests/test_entropy_audit_report_contract.py` | 6 |
  | Tests | `tests/test_entropy_audit_structural_budget.py` | 2 |
  | Tests | `tests/test_production_scheduler.py` | 1 |
  | Tests | `tests/test_orchestration_chain.py` | 1 |
  | Docs | `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` | 7 |
  | Docs | `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` | 3 |
  | Docs | `docs/governance/GOVERNANCE_8_FINAL_EVIDENCE_MAP.md` | 5 |
  | Docs | `docs/governance/entropy-burndown-triage.md` | 4 |
  | Docs | `docs/governance/STRUCTURAL_FILE_DISPOSITION_INVENTORY.md` | 4 |
  | Docs | `services/orchestrator/AGENTS.md` (`:5`, `:12-13`, `:30`, `:36-40`, `:67`) | 2 |

  - `tests/test_openapi_drift.py` also matches the wide pattern, but its "compatibility facade" is the OpenAPI `main` facade. That is unrelated and out of scope.
  - The F3-owned test tokens are the inventory-text assertions: 53 quoted `_SCHEDULER_*` token lines in `tests/test_production_scheduler.py` and 28 `_CHAIN_*` lines in `tests/test_orchestration_chain.py`, together with the tests and helpers that read the inventory text (`_scheduler_inventory_text`, `_scheduler_inventory_governed_group_ids` and the chain equivalents). The `not hasattr(legacy_chain, "_CHAIN_RETRY_COMPAT_*")` absence probes (`tests/test_orchestration_chain.py:11017-11023`) are already subsumed by the 11.4 fresh-subprocess tests, so they are deleted too.
- Spec deltas, written by the orchestrator before implementation:
  - `specs/compatibility-facade-governance/spec.md` REMOVES all three requirements. `.openspec.yaml` gains `retire_capabilities: true`; a scratch `openspec archive` of the whole change succeeds and retires the capability.
  - `specs/facade-shrink/spec.md` REMOVES "Compatibility guard remains authoritative".
  - `specs/orchestrator-structural-burndown/spec.md` MODIFIES "Entropy audit enforcement and its corpus split without report drift". The inventory clauses at `:434-436` and in the scenario "the corpus collects identically…" are dropped, and `STRUCTURAL_FILE_DISPOSITION_INVENTORY.md` is named in their place.
  - `specs/import-layering-guard/spec.md`, the existing MODIFIED delta from #3272, drops the clause that made the inventories own the contract modules.
  - Accepted as is: `openspec/specs/gateway-reconcile-test-partitioning/spec.md:20` lists "compatibility-inventory" among the checks in a WHEN clause. With no such check left, the clause is vacuous, not false.
- Changes (implementer):
  1. **12.1.** Delete the following:
     - both inventories and `facade_guard.py`;
     - `COMPATIBILITY_FACADE_CONFIGS` (`schema.py`) and the facade dataclasses that only the guard uses;
     - the guard constants (`constants.py:142`, `:145`, `:246`) and the `compatibility-facade-growth` check id (`:35`);
     - the report wiring (`report.py:43-46`, `:82-91`), together with the matching markdown section and summary key. The report-contract test fixes the list of report keys, so remove the facade key from that list.
     - the helpers in `audit_repo_entropy.py` that use any of the above, including `_mod_facade_guard` (`:47`, `:867`) and the facade entries in `__all__` (`:627-695`), and the matching helpers in `tests/entropy_audit_helpers.py`.

     Keep `STRUCTURAL_COMPATIBILITY_PATTERN` (`constants.py:499`). It is the structural-budget scan, not the facade guard; if it turns out to be facade-only, say why in the PR.
  2. **12.2.** Delete:
     - `tests/test_entropy_audit_facade_guard_{forwarders,aliases}.py` whole, or only their facade parts if they also test something else (record which);
     - the facade parts of `tests/test_entropy_audit_instruction_inventory.py`, `tests/test_entropy_audit_report_contract.py` and `tests/test_entropy_audit_structural_budget.py`;
     - the inventory-text tests and helpers in `tests/test_production_scheduler.py` and `tests/test_orchestration_chain.py`. Where one test function mixes inventory-text assertions with behavioural ones, delete only the inventory part. No behavioural assertion is deleted.

     Then drop the two facade-guard entries from `scripts/ci/full_test_durations.json`.

     Mixed test files, where only the named lines change:
     - `tests/test_entropy_audit_instruction_inventory.py`: the module docstring, and the two `See docs/governance/*_COMPATIBILITY_INVENTORY.md` fixture lines in `test_scoped_agent_context_reports_stale_scoped_context`. That test's assertion is only about the missing runbook.
     - `tests/test_entropy_audit_report_contract.py`: `:80-93` (the guard block), `:132` (the check id) and `:260` (the section line).
     - `tests/test_entropy_audit_structural_budget.py`: `:211` and `:216`.

     Deleted whole: `test_compatibility_facade_guard_current_repo_passes_with_inventories`.

     All 7 scheduler and 9 chain inventory-text tests are mixed. In each, cut the trailing inventory segment, including `test_chain_http_slurm_gateway_client_legacy_subclass_uses_owner_module` (find these with grep `_chain_inventory_text`), and drop the local variables that become dead (e.g. `expected_inventory_tokens` near `:10917`, F841).

     The absence loop in `test_chain_retry_compat_facade_matches_owner_module_and_inventory` (`:11017-11023`) loses its three `_CHAIN_RETRY_COMPAT_*` entries, which the 11.4 `_COMPAT_` scans subsume. `assert not hasattr(legacy_chain, "_retry_service_from_env")` stays, because it is the repository's only pin for that name.
  3. **12.3.** Keep the `compatibility facade` glossary term and its `required_glossary_terms` entry. Spec `scoped-agent-context-governance/spec.md:43-46` requires it, and `packages/common/AGENTS.md`, `reconcile.py` and `audit_repo_entropy.py` still use it. This deviates from design.md F3 and is recorded in the PR body. Rewrite `openspec/glossary.md:17` to: "| compatibility facade | A module that keeps old import, monkeypatch, or call surfaces stable while delegating behavior to real owner modules. Each name it serves is an explicit import or alias line; local bug fixes that do not add ownership surface are not facade growth. |" Rewrite `services/orchestrator/AGENTS.md` so it no longer cites the inventories or a guard. Its rule becomes: "a new facade name is an explicit import or alias line; `tests/test_facade_name_resolution.py` must stay green; do not grow facade surface without a reader". If `AGENTS.md` is generated, edit the source and regenerate.
  4. Update the governance docs that cite the deleted files: `entropy-budget.md`, `entropy-burndown-triage.md`, `STRUCTURAL_FILE_DISPOSITION_INVENTORY.md` and `GOVERNANCE_8_FINAL_EVIDENCE_MAP.md`.
     - Historical rows that only record past evidence may keep the name if a note says the file was deleted in #3274.
     - Current-authority text must not point at a deleted file.
     - Also fix the stale `scheduler_state_compat`, `scheduler_compat_runtime` and `_scheduler_compat_function` mentions that the #3273 reviewers listed in `STRUCTURAL_FILE_DISPOSITION_INVENTORY.md:35`, `:47` and `:161`.
  5. **12.4.** Check the `chain_array_accounting.py` comment at `:99-105`. 48ade5279 already reworded it, so no change is expected.
  6. **Carry-over docstrings.** `tests/test_facade_name_resolution.py`'s module docstring does not mention the inventories or the facade guard; confirm this, and no change is expected. Also confirm that `entropy-budget.md:15`'s "compatibility facade" names the audit script and not an inventory, so it stays. Carry-over #2 of `.workplans/3270/carry-overs.md` (silent synthetic forms, zero sites at HEAD) is closed by adding one docstring sentence that names those forms as uncollected. No code change.
- Out of scope, filed as one follow-up issue by the orchestrator:
  - the runtime carry-overs from #3292 and #3273: unify `scheduler_lease._scheduler_facade_function` with `_facade_seam`; teach the F1 guard `_facade_seam` literals; the aggregator-patch combination; and stating the two-seam boundary in the spec;
  - `scheduler_candidate_runtime`'s seven values that no longer have a reader, and its namespace copy;
  - the chain imports that are kept only so their names resolve;
  - test names that still contain "compat".
- Must preserve:
  - Every other entropy-audit check behaves as before, under the evidence 3 rule.
  - CI's entropy audit report-only job and the Production Topology Hard Gate stay green.
  - No behavioural test assertion is deleted.
  - No `file.py:NNN` line reference appears in shipped Python.
- Risk packs: governance tooling (report schema consumers). Not selected: runtime, DB, Slurm.
- Required evidence (`.workplans/3274/`):
  1. `grep -rni "compatibility inventory\|COMPATIBILITY_INVENTORY\|facade guard" docs scripts tests services --exclude='*.jsonl'` returns nothing, apart from historical-row notes, each of which is listed. The historical-row exception (`GOVERNANCE_8_FINAL_EVIDENCE_MAP.md`, `entropy-burndown-triage.md`) departs from the issue's "returns nothing" and is recorded as a deviation in the PR body. In addition, `git grep -n "compatibility_facade\|COMPATIBILITY_FACADE\|compatibility-facade-growth\|_compatibility_inventory\|facade_guard" -- scripts tests services docs ':!*.jsonl'` returns nothing.
  2. `ledger.md` lists every deleted test function, labelled "facade/inventory only" or "mixed, behavioural part kept" (with that test's id). It also gives the collected-test counts before and after for each touched test file.
  3. CI's report-only commands run locally on f45e332b2 and on the branch tip: `uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json --structural-base-ref f45e332b2`, the same with `--format markdown`, and `--mode hard-gate --format json` (exit 0). The diff must show:
     - `metadata.compatibility_facade_guard` and the `## Compatibility Facade Guard` section are gone;
     - `executed_check_families` loses exactly `compatibility-facade-growth`;
     - the other stable metadata keys (as defined by `orchestrator-structural-burndown`) are equal;
     - the set of findings check ids and the `module_heatmap` / `high_spread_patterns` key sets are equal;
     - every remaining `findings` / `structural_file_budget` entry is attributed in `ledger.md` to a path this PR deleted or edited.
  4. Clean runs of `uv run --no-sync ruff check . --no-cache`, `git diff --check`, `npx --yes markdownlint-cli2@0.17.2 "docs/**/*.md"` and `openspec validate redesign-orchestration-core --strict --no-interactive`.
- Verification:
  - Locally:
    - every touched test file;
    - `tests/test_entropy_audit_*.py`;
    - `tests/test_facade_name_resolution.py` and `tests/test_orchestrator_first_import.py`;
    - `tests/test_entropy_audit_line_references.py`.
  - CI: the full sharded suite, with the entropy audit report-only job green.
  - No node-27 or node-22 run: governance tooling only (12.5).

## 13. J0 — Characterization guard for the journal class

- [x] 13.1 Add `tests/test_file_journal_class_surface.py`. It pins the four sets from design.md D4 as checked-in sorted lists:
  - method names with signatures;
  - attribution-wrapped public methods;
  - private methods called by the four named external modules;
  - public module-level names importable from the facade.
- [x] 13.2 Extend the cluster-map late-lookup lint (`tests/test_file_journal_cluster_map.py:233-250`) to cover every `file_journal_*.py` mixin module listed in the cluster map, not only `EXTRACTION_ORDER`.
- [x] 13.3 Verification: CI only.

Suggested fixture level: compact - test-only.
Minimal mergeable slice: atomic - one guard file plus the lint extension it needs.

Slice triage (J0, #3275, tasks 13.1-13.3):
- Issue type: test (characterization guard). Fixture level: compact (agree): test-only, one new guard file plus a lint extension. Width: one PR, atomic.
- Recon at 900e35446:
  - `services/orchestrator/file_orchestration_journal.py` has 12993 lines. The module has two decorated classes: `FileOrchestrationJournalRepository` at `:1305-1306` and `FileJournalRetryService` at `:12289-12290`.
  - `_install_public_read_attribution` is defined at `file_journal_read_attribution.py:116-137`.
    - It wraps only the public functions in `vars(cls)`, so the class's own dict, not the MRO.
    - It skips generators and anything that already has `__wrapped__`.
    - The wrapper comes from `_attributed_public_method` (`:93-114`) through `functools.wraps`.
    - Consequence: a public method moved into an undecorated mixin silently loses attribution. That is why D4 says each mixin with public methods carries the decorator, and why this guard pins the wrapped set.
  - The four external modules named in D4 contain these private attribute calls: `journal_scope_census.py` 6, `file_orchestration_migration.py` 4, `scripts/ops/node22_repair_placeholder_hydro_uris.py` 3, and `scheduler_state_manual_retry.py` 0. The last one only cites journal helpers in comments.
  - The cluster map (`tests/fixtures/file_journal_cluster_map.json`, `tests/test_file_journal_cluster_map.py`) assigns top-level units to eight clusters. Its late-lookup lint (`late_lookup_problems`, `:233-250`) iterates `EXTRACTION_ORDER = CLUSTERS[1:]`, a hard-coded tuple (`:73`). A new `file_journal_<cluster>.py` mixin added by J1-J5 would therefore escape the lint unless the tuple is also edited.
- Changes (implementer; test-only):
  1. **13.1.** Add `tests/test_file_journal_class_surface.py` and its checked-in data file `tests/fixtures/file_journal_class_surface.json`. The data file holds four sorted lists, generated once from HEAD by a function inside the test module. When an opt-in env var (for example `NHMS_REGENERATE_CLASS_SURFACE=1`) is set, that function rewrites the file. The default run only compares.
     - (1) For both classes, every non-dunder name in `dir(cls)`, resolved to its raw `vars(base)` entry through `cls.__mro__` (`object` excluded). The owning base and `__qualname__` are not recorded. Record its kind, one of `function`, `staticmethod`, `classmethod`, `property` or `value`.
       - For callables, record a normalised signature of the underlying function: `__func__` for static and class methods; `fget` for properties, plus which of `fset` and `fdel` exist. `inspect.signature` follows `__wrapped__`.
       - For `value`, record `type(v).__name__` and `repr(v)`.

       Normalisation: each parameter renders as `str(param)`, except its default. A default renders as `=facade.NAME` when it `is` a module-level object of `file_orchestration_journal` named NAME (today `_UNSET`), and as `repr(default)` otherwise. The generator asserts that no row contains ` at 0x`, so a mixin that defines its own sentinel fails. Annotations stay the quoted strings that `from __future__ import annotations` produces. Every mixin module starts with that import, and a signature diff caused by an evaluated annotation is fixed in the mixin, never by regenerating.
     - (2) For both classes, resolve each public name to its raw entry in `vars(base)` for the first `base` in `cls.__mro__` that has it (`object` excluded). The entry is an attribution wrapper when it is a plain function whose `__code__` is the inner function's code object, obtained once as `_attributed_public_method("probe", lambda self: None).__code__`. `functools.wraps` copies `__qualname__`, so qualname cannot identify the wrapper. Pin these names; the fixture review counted 67 on the repository and 6 on the retry service. Separately, pin the public plain functions the installer deliberately skips: generator functions, and functions that already carry a foreign `__wrapped__` (today `open_retention_cycle`, a `@contextmanager`). Properties, staticmethods and classmethods are outside this set. Converse: every public plain function resolved this way is either a wrapper or in the pinned skip list.
     - (3) The private class attributes the four D4 modules reach: AST `Attribute` nodes `X._name` in those modules where `_name` resolves on either class through the MRO (`object` excluded) and is not defined by the scanned module itself. At HEAD this is 13 names on `FileOrchestrationJournalRepository`: census 6; migration 4, including the staticmethod `_pipeline_job_row`; the repair script 3. The test re-scans the four modules, asserts the result equals the pinned list, and asserts each name still resolves. Module-level private names are reached by from-import only, and set 4(b) pins them.
     - (4) The facade's import contract, recorded as name -> coarse tag (`class`, `function` or `value`). It is the union of:
       - (a) every public name defined at the facade's top level by `def`, `class`, `Assign` or `AnnAssign` at HEAD. A name later moved to a sibling must still resolve on the facade;
       - (b) every name, public or private, that a production `*.py` outside the facade and the `file_journal_*` siblings imports from the facade, or reads as `<facade alias>.X`. Production means `services/`, `apps/`, `scripts/`, `workers/` and `packages/`. The import forms are `from services.orchestrator.file_orchestration_journal import X` and level-1 `from .file_orchestration_journal import X`.

       Names the facade imports only for its own use are excluded: a pure move makes some of them unused, and ruff F401 forces their removal. The test asserts that every pinned name resolves on the facade with the same tag. Private names that tests import from the facade fail loudly at import time, so they are not pinned; silently-rebinding patched names are covered by the cluster map's `patched` and late-lookup rules.
     The test asserts that the live sets equal the data file. A deliberate change must regenerate the file and appear in the diff. J1-J5 must leave the file unchanged, because a pure move preserves all four sets.

     The generator writes `json.dumps(..., indent=2, sort_keys=True) + "\n"`. A byte-level serialisation test follows the pattern of `tests/test_file_journal_cluster_map.py:615-618`. The regeneration branch ends with `pytest.fail("regenerated; rerun without the env var")`, so a regenerating run never passes. The new test file must not `setattr` or `patch` the facade module: the cluster-map patch scanner reads every test that mentions the facade. Red run (b) therefore uses a synthetic class or a scratch copy.
  2. **13.2.** In `tests/test_file_journal_cluster_map.py`, `coverage_problems` also reports every on-disk `services/orchestrator/file_journal_*.py` that is not `sibling_path(root, c)` for some `c` in `EXTRACTION_ORDER`, with the message `<rel>: file_journal sibling is not a registered cluster; add it to CLUSTERS and the map`.
     - `coverage_problems`, `host_units` and `late_lookup_problems` keep iterating `EXTRACTION_ORDER`. `shape_problems` already ties the map keys to `CLUSTERS`, so all three then cover every mixin. A J slice registers its mixin by adding the cluster to `CLUSTERS` and to the map.
     - Synthetic test: an on-disk `file_journal_newmixin.py` that is not in `CLUSTERS` is reported. HEAD reports nothing.
  3. **Orchestrator `openspec` edits**, from `.workplans/3274/carry-overs.md`:
     - `specs/orchestrator-structural-burndown/spec.md:7` loses the stale count "fifteen";
     - the scenario titles "... inventory is updated with the extraction" in `specs/scheduler-facade-deepening/spec.md` and `specs/chain-facade-deepening/spec.md` stay. `openspec validate` rejects renaming a scenario inside a MODIFIED block, and the THEN text ("no compatibility inventory is edited … does not exist") is authoritative.
- Must preserve:
  - No production file changes.
  - Existing cluster-map tests stay green unchanged, except for the new registration rule.
  - No `file.py:NNN` line reference appears in shipped Python.
- Out of scope: moving any method (J1-J5); extending the map's coverage rules beyond the lint.
- Required evidence (`.workplans/3275/`):
  1. `surface.md`: the four set sizes and the generation command.
  2. `red-runs.log`, each a temporary edit reverted afterwards:
     - (a) rename one public method on the class, which must fail set 1;
     - (b) move one public method into an undecorated mixin base in a scratch copy, which must fail set 2;
     - (c) drop one private attribute used by an external module, which must fail set 3;
     - (d) add a public module-level name, which must fail set 4;
     - (e) the unregistered-sibling synthetic case fails on HEAD's `coverage_problems` and passes after the change.
  3. `uv run --no-sync ruff check . --no-cache`, `git diff --check` and `openspec validate redesign-orchestration-core --strict --no-interactive`.
- Verification:
  - Locally: the new test, `tests/test_file_journal_cluster_map.py`, and the other `tests/test_file_journal_*.py` files that collect quickly (at least the cluster-map and full-tree budget tests).
  - CI: the full suite (13.3).
  - No node-27 or node-22 run: test-only.

## 14. J1 — Retention mixin

- [x] 14.1 Move C18 (Appendix B) into `services/orchestrator/file_journal_retention.py` as a mixin that the class composes. The retention dataclasses stay importable from the facade. Update the cluster map. J0, `tests/test_file_journal_late_lookup_seams.py` and `tests/test_scheduler_journal_retention_archive.py` stay unchanged and green.
  - J1-J5 carry-over from J0 (#3275): set 1 renders a `_UNSET` default as `=facade._UNSET`, and set 4 requires every pinned 4(a) name (e.g. `FileJournalRetentionCycle*`, `OperatorBindReceipt`, `PERMANENT_FAILURE_SOURCE_STATUSES`) to keep resolving on the facade. When a slice moves such a name, or the last class-body use of `_UNSET`, out of the facade, the facade keeps the re-import with `# noqa: F401`. Adding it to `__all__` is not an option: `tests/test_file_orchestration_journal_facade.py` requires every `__all__` name to have a consumer. Facade-only imports (`COHORT_MEMBERSHIP_*`, `journal_read_lane`) may be dropped under F401 without touching set 4, whose live side counts only sibling imports already pinned as 4(a).
- [ ] 14.2 Verification: CI, then node-22 batch per D5.

Suggested fixture level: expanded - moves persisted-state writer code.
Minimal mergeable slice: atomic - one cluster, pure move.

Slice triage (J1, #3276, tasks 14.1-14.2):
- Issue type: refactor (pure move). Fixture level: expanded (agree): it moves persisted-state writer code (retention removal unlinks journal files). Width: one PR, atomic.
- Recon at 1a3c43c22. The Appendix B ranges (at 54556b1e7) have shifted by 2-3 lines; the HEAD ranges are below.
  - C18 is 7 methods of `FileOrchestrationJournalRepository` in `services/orchestrator/file_orchestration_journal.py`:
    - `discover_retention_cycles` `:1365-1390` (public, attribution-wrapped);
    - `open_retention_cycle` `:1393-1453` (public, `@contextmanager`, in J0's skip list);
    - `_discover_retention_cycles_unlocked` `:11789-11835`, `_inspect_retention_cycle_unlocked` `:11837-11910`, `_retention_cycle_members_unlocked` `:11912-11952`, `_retention_member_for_path` `:11954-11972`, `_remove_retention_members_unlocked` `:11974-12043`.
  - Module-level names they read:
    - Facade-defined: the five `FileJournalRetention*` dataclasses (`:979-1085`), `_RetentionDiscoveryBudget` (`:1289-1302`), `FileOrchestrationJournalError`.
    - Facade-resident patched name (cluster map `patched`): `stat_no_follow`. It is also used outside C18, so the facade keeps its import.
    - Imported into the facade from owner modules: `packages.common.safe_fs` (`SafeFilesystemError`, `read_bytes_durable_no_follow`, `unlink_no_follow_durable`), `file_journal_paths` (`_cycle_read_source_segments`, `_iter_retention_surface_files`, `_journal_segment_identity_from_path`, `_latest_identity_from_path`, `_relative_evidence`), `file_journal_primitives._normalize_file_source_id`, `file_journal_retry_helpers._job_blocks_rollback_quiescence`, `file_journal_candidates._released_identity_blocked_row`, `scheduler_state._ensure_utc`, `accepted_submit_identity.AcceptedSubmitEvidenceError`, `chain_types.OrchestratorError`, `format_cycle_time`, plus stdlib (`hashlib`, `stat`, `datetime`, `Path`, `contextmanager`, `Iterable`, `Sequence`).
  - Consumers:
    - production: `scheduler_journal_retention.py:392,438` and `scheduler_journal_restore.py:131`, both via the instance;
    - tests: a class-level `monkeypatch.setattr(FileOrchestrationJournalRepository, "_remove_retention_members_unlocked", ...)` at `tests/test_scheduler_journal_retention_archive.py:269`, and instance-level patches at `tests/test_scheduler_journal_retention_planning.py:267,271`. Both resolve through the MRO.
  - The retention dataclasses are imported from the facade by `scheduler_journal_retention.py:25-26`, `scheduler_journal_archive.py:33` and tests. `_RetentionDiscoveryBudget` is imported by `file_journal_paths.py:42` (under `TYPE_CHECKING`) and read as `file_orchestration_journal._RetentionDiscoveryBudget` by `tests/test_file_journal_late_lookup_seams.py:189`.
- Changes (implementer):
  1. New `services/orchestrator/file_journal_retention.py`, starting with `from __future__ import annotations`. It holds one top-level class, `_FileJournalRetentionMixin`, decorated `@_install_public_read_attribution` (imported from `file_journal_read_attribution`). The class contains the 7 methods, moved verbatim: same bodies, decorators, signatures and docstrings; only name resolution changes.
  2. Name resolution in the moved code:
     - the patched name `stat_no_follow` is read as an attribute of the facade module at call time (a function-local `from services.orchestrator import file_orchestration_journal`, then `file_orchestration_journal.stat_no_follow(...)`). It is never bound by import, at top level or in a function body;
     - facade-defined names are resolved in two ways. Names used only in annotations (signature annotations, and local variable annotations such as `members: tuple[FileJournalRetentionMember, ...]`) come from one module-level `if TYPE_CHECKING:` block that imports them from the facade, as `file_journal_paths.py` does; at HEAD these are the five `FileJournalRetention*` classes. Names read at runtime come from function-local imports from the facade, placed immediately after the method docstring and listing only the names that method's body reads at runtime. Annotations stay byte-identical, so J0 set 1 is unchanged. The sibling must not import the facade at top level outside `TYPE_CHECKING` (circular import);
     - names the facade itself imports from owner modules (stdlib, `safe_fs` except `stat_no_follow`, other `file_journal_*` siblings, `scheduler_state`, `accepted_submit_identity`, `chain_types`, `time_normalization`) are imported at the sibling's top level from those owner modules.
  3. The facade imports `_FileJournalRetentionMixin` and declares `class FileOrchestrationJournalRepository(_FileJournalRetentionMixin):`, keeping its own `@_install_public_read_attribution`. The 7 methods are deleted from the class body. The retention dataclasses and `_RetentionDiscoveryBudget` stay in the facade unchanged, as does `__all__`. Facade imports left unused by the move are removed (ruff F401), except a pinned J0 4(a) name or `_UNSET`, which keep `# noqa: F401` (tasks 14.1 carry-over). Expected removals: `read_bytes_durable_no_follow` and `unlink_no_follow_durable` (from `safe_fs`), and `_iter_retention_surface_files` (from `file_journal_paths`). No `# noqa` is needed.
  4. Cluster map (`tests/test_file_journal_cluster_map.py` + `tests/fixtures/file_journal_cluster_map.json`): add `"retention"` to `CLUSTERS` (end of the tuple), `clusters["retention"] = ["_FileJournalRetentionMixin"]`, and update `patched[*].callers` for every patched name the moved code references. Expected: `stat_no_follow` gains `"retention": ["_FileJournalRetentionMixin"]`. Re-derive the full set; do not guess. Update the module docstring's cluster count and order sentence.
  5. Any other repo guard that enumerates per-file entries and now sees code move from the facade to the new file is updated in the same diff, as a pure relocation of an existing entry listed in `moves.md`. Expected result: "none". Check `tests/status_literal_allowlist.json`, `scripts/governance/entropy_audit/line_reference_baseline.json`, `pyproject.toml` per-file-ignores, and the first-import allowlist.
- Must preserve:
  - `tests/fixtures/file_journal_class_surface.json` is byte-identical, and `tests/test_file_journal_class_surface.py` is unchanged and green (J0).
  - `tests/test_file_journal_late_lookup_seams.py` and `tests/test_scheduler_journal_retention_archive.py` are unchanged and green.
  - No behaviour, signature, exception, log or on-disk format change. The method bodies are AST-identical to HEAD after name-resolution rewrites, and evidence 1 proves it.
  - No other cluster moves; the shared core stays.
  - No `file.py:NNN` line reference is added in shipped Python.
- Deviation (record in the PR body): the diff also edits `tests/test_file_journal_cluster_map.py` (`CLUSTERS` and its docstring), because the 13.2 registration rule requires it, and ticks 14.1 in `tasks.md`.
- Out of scope: moving the retention dataclasses; other clusters (J2-J5); renaming anything; the stale comment line references noted in #3275.
- Required evidence (`.workplans/3276/`):
  1. `ast-parity.md`: for each of the 7 methods, `ast.dump` of HEAD's whole `FunctionDef` (decorators, args, returns, body) versus the moved one, after normalising only the documented name-resolution rewrites (the inserted function-local import lines and `file_orchestration_journal.stat_no_follow` → `stat_no_follow`); expect identical. Check `ast.get_docstring` on the un-normalised moved node equals HEAD's. Also confirm the facade diff only deletes those 7 methods, plus import-line and class-header edits.
  2. `red-runs.log`, each a temporary edit reverted afterwards:
     - (a) bind `stat_no_follow` by top-level import in the sibling: the cluster-map late-lookup lint fails;
     - (b) drop `@_install_public_read_attribution` from the mixin: J0 set 2 fails;
     - (c) remove `"retention"` from `CLUSTERS`: the 13.2 unregistered-sibling rule fails; the log names that message (the shape check's unknown-cluster-key failure also fires).
  3. `moves.md`: every guard and allowlist entry relocated under Change 5, or "none".
  4. `checks.log`: `uv run --no-sync ruff check . --no-cache`, `git diff --check`, `openspec validate redesign-orchestration-core --strict --no-interactive`, and the local test list.
- Verification:
  - Locally: `tests/test_file_journal_*.py`, `tests/test_file_orchestration_journal*.py`, `tests/test_scheduler_journal_retention*.py` (these cover restore and archive), `tests/test_status_literal_scan.py`, `tests/test_status_vocab_parity.py`, `tests/test_import_layering_guard.py`, `tests/test_orchestrator_first_import.py`, `tests/test_facade_name_resolution.py`, and every test file that scans `services/**` or `tests/**` and names the facade or `file_journal_`.
  - node-27: a fresh-process import check over all `integration`-marked files at HEAD (the `tasks.md` F-slice method) lists the files that import `services.orchestrator.file_orchestration_journal` or `services.orchestrator.file_journal_retention`. After `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`, run `-m integration` over them on throwaway `nhms_it_*` DBs. Expected: all pass, 0 leftover DBs.
  - CI: the full suite.
  - node-22: the D5 pure-move batch (14.2).

## 15. J2 — Status/events and identity-blocked release mixins

- [x] 15.1 Move C17 into `file_journal_events.py`. A mixin with public methods carries `@_install_public_read_attribution`. J0 stays unchanged.
- [x] 15.2 Move C15 into `file_journal_release.py`. J0 stays unchanged.
- [ ] 15.3 Verification: CI, then node-22 batch per D5.

Suggested fixture level: expanded - moves persisted-state writer code.
Minimal mergeable slice: 15.1 alone; 15.2 is its own PR.

Slice triage (J2a, #3277, tasks 15.1, 15.3):
- Issue type: refactor (pure move). Fixture level: expanded (agree): it moves pipeline-job status and event writers (persisted journal records). Width: one PR, atomic; 15.2 (C15) is its own PR (#3278).
- Recon at db35158c6, J1 merged. The Appendix B range for C17 (6844-7134 at 54556b1e7) has shifted by about 90 lines; the HEAD ranges are below.
  - C17 is 7 methods of `FileOrchestrationJournalRepository` in `services/orchestrator/file_orchestration_journal.py`, all undecorated:
    - public, attribution-wrapped: `update_pipeline_job_status` `:6755-6827`, `insert_pipeline_event` `:6829-6873`, `append_historical_pipeline_event` `:6875-6918`, `update_forecast_cycle_status` `:6940-6961`, `list_stage_statuses` `:6963-7000`;
    - private: `_pipeline_event_target` `:6920-6938`, `_list_stage_statuses_for_source` `:7002-7044`.
  - Module-level names they read at runtime:
    - Facade-defined: `FileOrchestrationJournalError`, `_historical_pipeline_event_row`.
    - Facade-resident patched name (cluster map `patched`): `_utcnow`. It is also used elsewhere in the facade, so the facade keeps its import.
    - Imported into the facade from owner modules: `packages.common.status_vocab` (`PIPELINE_JOB_SUCCESS`, `PIPELINE_JOB_TERMINAL_OVERRIDES`), `file_journal_candidates` (`_blocked_stage_status`, `_cycle_time_from_job`, `_pipeline_event_entity_type`, `_public_scheduler_row`, `_resolved_caller_evidence`, `_source_cycle_from_cycle_id`, `_source_id_from_job`), `file_journal_primitives` (`_cycle_id_for_file_source`, `_normalize_file_source_id`, `_required_safe_identity`, `_safe_identity_text`), `file_journal_paths._optional_safe_identity`, `file_journal_replay._db_compatible_stage_status_order_key`, `file_journal_retry_helpers._durable_error_message`, `scheduler_state._format_utc`, `accepted_submit_identity` (`accepted_submit_contract_is_current`, `accepted_submit_row_kind`), `chain_types.OrchestratorError`, `datetime`.
    - Annotation-only: `Any`, `Mapping` (typing / collections.abc, not facade-defined).
  - No `_UNSET` defaults.
  - The rest of the class calls `self.update_pipeline_job_status`, which resolves through the MRO.
  - Consumers:
    - production: about 55 call sites through `repository.` / `self.repository.` instances (`chain_*`, `scheduler_*`, `file_orchestration_migration.py:1415`). The `chain.py` and `chain_forecast_orchestrator_runtime.py` defs of the same names are other classes.
    - tests: instance-level `monkeypatch.setattr(repository, "insert_pipeline_event", ...)` at `tests/test_orchestration_chain.py:3909,4586,4757`. Test subclasses of the repository (`RecordingJournalRepository` in `test_orchestration_chain.py`, and others) may override these methods and call `super()`; both resolve through the MRO.
    - No class-level patch of a C17 name.
- Changes (implementer):
  1. New `services/orchestrator/file_journal_events.py`: a module docstring, then `from __future__ import annotations`. It holds one top-level class, `_FileJournalEventsMixin`, decorated `@_install_public_read_attribution` (imported from `file_journal_read_attribution`). The class contains the 7 methods, moved verbatim: same bodies, decorators, signatures and docstrings; only name resolution changes.
  2. Name resolution in the moved code:
     - the patched name `_utcnow` is read as an attribute of the facade module at call time (a function-local `from services.orchestrator import file_orchestration_journal`, then `file_orchestration_journal._utcnow()`). It is never bound by import, at top level or in a function body;
     - facade-defined names read at runtime (`FileOrchestrationJournalError`, `_historical_pipeline_event_row`) come from function-local imports from the facade, placed as the first statements of the body (none of the 7 methods has a docstring) and listing only the names that method's body reads at runtime. Facade-defined names used only in annotations, if any, go in one module-level `if TYPE_CHECKING:` block importing from the facade. The sibling must not import the facade at top level outside `TYPE_CHECKING` (circular import);
     - `_utcnow` is not an owner-module import, even though the facade imports it from `time_normalization`: it stays a facade attribute read. Other names the facade itself imports from owner modules (stdlib, `typing`/`collections.abc`, `status_vocab`, other `file_journal_*` siblings, `scheduler_state`, `accepted_submit_identity`, `chain_types`) are imported at the sibling's top level from those owner modules.
  3. The facade imports `_FileJournalEventsMixin` and declares `class FileOrchestrationJournalRepository(_FileJournalRetentionMixin, _FileJournalEventsMixin):`, keeping its own `@_install_public_read_attribution`. The 7 methods are deleted from the class body. `__all__` and every facade-defined name stay. Facade imports left unused by the move are removed (ruff F401), except a pinned J0 4(a) name or `_UNSET`, which keep `# noqa: F401` (tasks 14.1 carry-over). Names listed in `__all__` are never unused for ruff. Expected removals: `PIPELINE_JOB_SUCCESS`, `PIPELINE_JOB_TERMINAL_OVERRIDES`, `_db_compatible_stage_status_order_key`; `_blocked_stage_status` and `_pipeline_event_entity_type` stay because `__all__` lists them. Verify; do not assume.
  4. Cluster map (`tests/test_file_journal_cluster_map.py` + `tests/fixtures/file_journal_cluster_map.json`): append `"events"` to `CLUSTERS`, add `clusters["events"] = ["_FileJournalEventsMixin"]`, and re-derive `patched[*].callers` with the module's own derivation. Expected: `_utcnow` gains `"events": ["_FileJournalEventsMixin"]`, and nothing else changes. Update the module docstring's cluster count and order sentence.
  5. Any other repo guard that enumerates per-file entries and now sees code move from the facade to the new file is updated in the same diff, as a pure relocation of an existing entry listed in `moves.md`. Expected result: exactly one relocation. In `tests/status_literal_allowlist.json` the entry `{"file": "services/orchestrator/file_orchestration_journal.py", "members": ["cancelled", "failed", "succeeded"], "reason": "distinct-vocabulary"}` (the `update_pipeline_job_status` terminal guard; the facade has no other literal with those members) is re-keyed to `services/orchestrator/file_journal_events.py`, same members and reason, and placed between the `file_journal_candidates.py` and `file_journal_retry_helpers.py` rows (the scan enforces `(file, members)` order). Delete the facade row; do not keep both. The scan reads `git ls-files`, so before `git add` the only symptom is a stale facade row: relocate it, never just delete it. Nothing else relocates: `line_reference_baseline.json` (the facade's two entries sit outside C17), `pyproject.toml` per-file-ignores, the first-import allowlist, and the facade `__all__` consumer rule (no `__all__` name loses its last consumer) all stay unchanged.
- Must preserve:
  - `tests/fixtures/file_journal_class_surface.json` is byte-identical, and `tests/test_file_journal_class_surface.py` is unchanged and green (J0).
  - `tests/test_file_journal_late_lookup_seams.py` is unchanged and green.
  - No behaviour, signature, exception, log or on-disk format change. The method `FunctionDef`s are AST-identical to HEAD after the documented name-resolution rewrites, and evidence 1 proves it.
  - No other cluster moves; the shared core stays; `_FileJournalRetentionMixin` is untouched.
  - No `file.py:NNN` line reference is added in shipped Python.
- Deviation (record in the PR body): the diff also edits `tests/test_file_journal_cluster_map.py` (`CLUSTERS` and its docstring), because the 13.2 registration rule requires it, and `tests/status_literal_allowlist.json` (one entry re-keyed to the new file, a pure relocation), and ticks 15.1 in `tasks.md`.
- Out of scope: C15 (#3278) and the other clusters; renaming anything; moving facade-defined helpers such as `_historical_pipeline_event_row`; the J1 carry-overs (spec cluster count, retention seam test, `_cycle_write_owner` guard).
- Required evidence (`.workplans/3277/`):
  1. `ast-parity.md`: for each of the 7 methods, `ast.dump` of HEAD's whole `FunctionDef` (decorators, args, returns, body) versus the moved one, after normalising only the documented name-resolution rewrites (dropping the inserted function-local imports, and mapping `file_orchestration_journal._utcnow` → `_utcnow`); expect identical. Check `ast.get_docstring` on the un-normalised moved node equals HEAD's. Also confirm the facade diff only deletes those 7 methods, plus import-line and class-header edits.
  2. `red-runs.log`, each a temporary edit reverted afterwards:
     - (a) bind `_utcnow` by import in the sibling (top level or function body): the cluster-map late-lookup lint fails;
     - (a') import `utc_now` from `packages.common.contracts.time_normalization` and call it directly: `test_patched_callers_match_the_ast` fails (`_utcnow` loses its events caller);
     - (b) drop `@_install_public_read_attribution` from the mixin: J0 set 2 fails;
     - (c) remove `"events"` from `CLUSTERS`: the 13.2 unregistered-sibling rule fails; the log names that message (the shape check's unknown-cluster-key failure also fires).
  3. `moves.md`: the one allowlist relocation above, with the scan output before and after (run with a temporary `GIT_INDEX_FILE` that includes the new file).
  4. `checks.log`: `uv run --no-sync ruff check . --no-cache`, `git diff --check`, `openspec validate redesign-orchestration-core --strict --no-interactive`, and the local test list. Re-run `git ls-files`-based scanners with a temporary `GIT_INDEX_FILE` that includes the new file.
- Verification:
  - Locally: `tests/test_file_journal_*.py`, `tests/test_file_orchestration_journal*.py`, `tests/test_orchestration_chain.py` (insert/update consumers and subclasses), `tests/test_file_journal_repository_semantics.py`, `tests/test_status_literal_scan.py`, `tests/test_status_vocab_parity.py`, `tests/test_import_layering_guard.py`, `tests/test_orchestrator_first_import.py`, `tests/test_facade_name_resolution.py`, `tests/test_operator_action_listing.py`, `tests/test_operator_action_status_closure.py`, and every test file that scans `services/**` or `tests/**` and names the facade or `file_journal_`.
  - node-27: a fresh-process import check over all `integration`-marked files lists the files that import `services.orchestrator.file_orchestration_journal` or `services.orchestrator.file_journal_events`. After `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`, run `-m integration` over them on throwaway `nhms_it_*` DBs. Expected: all pass, 0 leftover DBs.
  - CI: the full suite.
  - node-22: the D5 pure-move batch (15.3).

Slice triage (J2b, #3278, tasks 15.2, 15.3):
- Issue type: refactor (pure move). Fixture level: expanded (agree): it moves the identity-blocked release/recover writers, which append persisted journal records. Width: one PR, atomic.
- Recon at 4fd1597a9, J1 and J2a merged. The Appendix B range for C15 (5830-6183 at 54556b1e7) has shifted by about 90 lines; the HEAD ranges are below.
  - C15 is 4 methods of `FileOrchestrationJournalRepository` in `services/orchestrator/file_orchestration_journal.py`. All are undecorated and all have docstrings:
    - public, attribution-wrapped: `release_identity_blocked_reservation` `:5739-5878`, `recover_released_identity_blocked_reservation` `:5986-6091`;
    - private: `_record_identity_released_signal_failure` `:5880-5938`, `_append_pipeline_job_event_unlocked` `:5940-5984`.
  - **Decision: `_record_identity_released_signal_failure` stays in the facade class body.**
    - It is the only class method that uses `LOGGER` (`:5931`).
    - `tests/test_file_journal_cluster_map.py::test_logger_users_stay_in_the_facade` requires every top-level unit that references `LOGGER`, whether by name or as `<facade alias>.LOGGER`, to live in the facade cluster, because tests filter on the facade's logger name.
    - Moving the method would break that rule, or need a guard change; leaving one method behind needs neither. `release_identity_blocked_reservation` keeps calling it as `self._record_identity_released_signal_failure(...)`, which resolves through the MRO.
    - So the slice moves 3 methods.
  - Module-level names the 3 moved methods read at runtime:
    - Facade-defined, per method: release reads `FileOrchestrationJournalError`, `IDENTITY_RELEASED_OPERATOR_SIGNAL` and `_released_reservation_recovery_command`; recover reads `FileOrchestrationJournalError` and `OPERATOR_RECOVERY_ATTESTATION_FIELD`; `_append_pipeline_job_event_unlocked` reads only `_utcnow` (as a facade attribute). None of them is a cluster-map patched name.
    - Facade-resident patched name: `_utcnow`. It is also used elsewhere in the facade, so the facade keeps its import.
    - Imported into the facade from owner modules:
      - `accepted_submit_identity`: `ACCEPTED_SUBMIT_CONTRACT_VERSION`, `ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD`, `AcceptedSubmitTransition`, `IDENTITY_MISMATCH_RELEASED_DECISION`, `accepted_submit_contract_is_current`, `accepted_submit_row_kind`, `apply_accepted_submit_transition`;
      - `chain_types.OrchestratorError` and `packages.common.safe_fs.SafeFilesystemError`;
      - `file_journal_retry_helpers._accepted_submit_attempt_anchor`, `file_journal_candidates` (`_accepted_submit_source_cycle_from_job_id`, `_public_scheduler_row`), `file_journal_paths._optional_safe_identity`, `file_journal_primitives._required_safe_identity`;
      - `scheduler_state._format_utc`.

      Re-derive this list per method.
    - Annotation-only: `Any`, `Mapping`, `datetime` (stdlib, not facade-defined).
  - No `_UNSET` defaults.
  - The rest of the class calls `self._append_pipeline_job_event_unlocked` from other clusters, which resolves through the MRO.
  - Consumers:
    - production: `reconcile_reserved_support.py:296` via `getattr(store, "release_identity_blocked_reservation", None)`, and `operator_released_reservation_recovery.py:204` via the instance;
    - tests:
      - an instance-level `monkeypatch.setattr(repository, "_append_pipeline_job_event_unlocked", ...)` at `tests/test_orchestrator_demote_projection_faults.py:693`;
      - a wrapper store at `tests/test_gateway_reconcile_identity_release.py:325` that delegates to the inner repository;
      - no class-level patch of a C15 name.
- Changes (implementer):
  1. New `services/orchestrator/file_journal_release.py`: a module docstring, then `from __future__ import annotations`. It holds one top-level class, `_FileJournalReleaseMixin`, decorated `@_install_public_read_attribution`. The class contains the 3 moved methods, moved verbatim and in HEAD order: same bodies, decorators, signatures and docstrings; only name resolution changes.
  2. Name resolution in the moved code, following the J1/J2a precedent:
     - `_utcnow` is read as an attribute of the facade module at call time (`from services.orchestrator import file_orchestration_journal`, then `file_orchestration_journal._utcnow()`). It is never bound by import, at top level or in a function body, and it is not imported from `time_normalization`;
     - facade-defined names read at runtime come from function-local imports from the facade, placed immediately after the method docstring and listing only the names that method reads at runtime. Facade-defined names used only in annotations, if any, go in one module-level `if TYPE_CHECKING:` block. The sibling must not import the facade at top level outside `TYPE_CHECKING`;
     - other names the facade imports from owner modules are imported at the sibling's top level from those owner modules.
  3. The facade imports `_FileJournalReleaseMixin` and declares `class FileOrchestrationJournalRepository(_FileJournalRetentionMixin, _FileJournalEventsMixin, _FileJournalReleaseMixin):`, keeping its own decorator.
     - The 3 methods are deleted from the class body; `_record_identity_released_signal_failure` stays where it is.
     - `__all__` and every facade-defined name stay.
     - Facade imports left unused are removed (ruff F401), except a pinned J0 4(a) name or `_UNSET` (`# noqa: F401`). Names listed in `__all__` are never unused for ruff.
     - Expected removals: none (`IDENTITY_MISMATCH_RELEASED_DECISION` is in `__all__`). Verify; do not assume.
  4. Cluster map: append `"release"` to `CLUSTERS`, add `clusters["release"] = ["_FileJournalReleaseMixin"]`, and re-derive `patched[*].callers` with the module's own derivation. Expected: `_utcnow` gains `"release": ["_FileJournalReleaseMixin"]`, and nothing else changes. Update the docstring's cluster count and order sentence.
  5. File-keyed guards. Expected result: nothing relocates, and `tests/status_literal_allowlist.json` stays byte-identical. The 3 moved methods hold no `Set`/`Tuple` with two or more `str` members (only `(None, "")` and an exception-class tuple). The facade keeps the same 9 `(file, members)` hit keys, the new file has 0 hits, and `compare_status_literals` returns `[]`. Run the scan with the new file indexed, in a temporary `GIT_INDEX_FILE` or after `git add`. Also unchanged:
     - `line_reference_baseline.json`: the facade's two entries sit outside C15, and the new file matches no `LINE_REFERENCE_PATTERNS`;
     - `pyproject.toml` per-file-ignores: the moved code needs neither B904 nor BLE001;
     - the first-import allowlist: the sibling imports cleanly first;
     - the facade `__all__` consumer rule: no listed name loses its last consumer.
     `moves.md` records "none" with the scan output before and after. Any deviation from "none" is a stop-and-report.
     The new module's docstring notes that the facade core (`_emit_committed_projection_fault_event`) also calls `_append_pipeline_job_event_unlocked`, and that `_record_identity_released_signal_failure` stays in the facade under the LOGGER rule.
- Must preserve:
  - The J0 data file is byte-identical, and the J0 test is unchanged and green.
  - `tests/test_file_journal_late_lookup_seams.py`, `file_journal_retention.py` and `file_journal_events.py` are unchanged.
  - `test_logger_users_stay_in_the_facade` stays green unchanged.
  - No behaviour, signature, exception, log or on-disk format change. The moved `FunctionDef`s are AST-identical to HEAD after the documented rewrites.
  - No `file.py:NNN` line reference is added in shipped Python.
- Deviations (record in the PR body):
  - the diff also edits `tests/test_file_journal_cluster_map.py` (`CLUSTERS` and its docstring) and ticks 15.2 in `tasks.md`;
  - C15 moves without `_record_identity_released_signal_failure`, which the LOGGER rule keeps in the facade.
- Out of scope: changing the LOGGER rule or giving the mixin its own logger; other clusters; renaming; the J1 and J2a carry-overs (seam tests, spec cluster count, `_cycle_write_owner` guard).
- Required evidence (`.workplans/3278/`):
  1. `ast-parity.md`:
     - for each of the 3 methods, `ast.dump` of HEAD's whole `FunctionDef` versus the moved one, after dropping the inserted function-local imports and mapping `file_orchestration_journal._utcnow` → `_utcnow`. Expect identical, with docstrings equal;
     - the facade diff only deletes those 3 methods, plus import-line and class-header edits;
     - `_record_identity_released_signal_failure` is unchanged in the facade.
  2. `red-runs.log`, each a temporary edit reverted afterwards:
     - (a) bind `_utcnow` by import in the sibling: the late-lookup lint fails;
     - (a') call `utc_now` from `time_normalization` directly in every moved site: `test_patched_callers_match_the_ast` fails;
     - (b) drop the mixin decorator: J0 set 2 fails;
     - (c) remove `"release"` from `CLUSTERS`: the 13.2 unregistered-sibling message is named, and the shape check also fires;
     - (d) additionally move `_record_identity_released_signal_failure` into the mixin, resolving `LOGGER`, `IDENTITY_RELEASED_SIGNAL_FAILED_EVENT` and its other facade-defined names by one function-local import from the facade (Change 2). Expected: exactly `test_logger_users_stay_in_the_facade` fails, naming `('_FileJournalReleaseMixin', 'release')`. The J0 test and `tests/test_production_scheduler.py -k release_signal` stay green, which shows that only the guard blocks the move.
  3. `moves.md`: allowlist and guard relocations with the scan before and after, or "none".
  4. `checks.log`: `uv run --no-sync ruff check . --no-cache`, `git diff --check`, `openspec validate redesign-orchestration-core --strict --no-interactive`, and the local test list. `git ls-files` scanners are re-run with the new file indexed, each in its own temporary index; the entropy tests that run `git init` inside `tmp_path` run on the real index.
- Verification:
  - Locally:
    - `tests/test_file_journal_*.py` and `tests/test_file_orchestration_journal*.py`;
    - `tests/test_gateway_reconcile_identity_*.py`, `tests/test_orchestrator_demote_projection_faults.py`, `tests/test_orchestrator_demote_core_cas.py`, `tests/test_orchestrator_demote_reclaim_lifecycle.py`, `tests/test_scheduler_held_reservation_block.py`, `tests/test_cli_subcommand_surface.py`, and `tests/test_production_scheduler.py -k "release_signal or identity_blocked or released_identity"`. The `release_signal` tests are the only behavioural cover of the mixin → facade `_record_identity_released_signal_failure` → mixin `_append_pipeline_job_event_unlocked` hop and of its facade-logger `caplog`. Run every other test file that calls the 3 moved methods (grep) in full;
    - `tests/test_status_literal_scan.py`, `tests/test_status_vocab_parity.py`, `tests/test_import_layering_guard.py`, `tests/test_orchestrator_first_import.py`, `tests/test_facade_name_resolution.py`, `tests/test_operator_action_listing.py`, `tests/test_operator_action_status_closure.py`;
    - every test file that scans `services/**` or `tests/**` and names the facade or `file_journal_`.
  - node-27: a fresh-process import check over all `integration`-marked files, then `-m integration` over the files that import the facade or `file_journal_release`, on throwaway `nhms_it_*` DBs. Expected: all pass, 0 leftover DBs.
  - CI: the full suite.
  - node-22: the D5 pure-move batch (15.3).
## 16. J3 — Reclaim, operator and cohort-projection mixins

- [x] 16.1 Move C12 into `file_journal_reclaim.py`.
- [x] 16.2 Move C14 into `file_journal_operator.py`. The receipt dataclasses stay importable from the facade.
- [ ] 16.3 Move C16 into `file_journal_cohort.py`, with `project_forecast_cohort_tasks` moved whole.
- [ ] 16.4 Verification: CI, then node-22 batch per D5. J0 stays unchanged after each PR.

Suggested fixture level: expanded - moves persisted-state writer code.
Minimal mergeable slice: 16.1 alone; 16.2 and 16.3 are their own PRs.

Slice triage (J3a, #3279, tasks 16.1, 16.4):
- Issue type: refactor (pure move). Fixture level: expanded (agree): it moves the reservation-reclaim writer, which rewrites persisted pipeline-job records. Width: one PR, atomic.
- Recon at 73c42d8fb, after J1, J2a and J2b merged. The Appendix B range for C12 is 3227-3474 at 54556b1e7; it has shifted about 90 lines.
  - C12 is a single method of `FileOrchestrationJournalRepository`:
    - `reclaim_pipeline_job_reservation`, `:3137-3383`, in `services/orchestrator/file_orchestration_journal.py`;
    - public and attribution-wrapped, undecorated, with no docstring;
    - it takes no `_UNSET` defaults.
  - Module-level names it reads at runtime:
    - Facade-defined: `FileOrchestrationJournalError`.
    - Facade-resident patched name: `_utcnow`. The facade uses it elsewhere too, so the facade keeps its import.
    - Imported into the facade from owner modules:
      - `accepted_submit_identity`: `AcceptedSubmitTransition`, `OPERATOR_VERIFIED_ABSENCE_DECISION`, `SLURM_ACCOUNTING_SUBMITTED_AT_FIELD`, `SLURM_BINDING_SOURCE_FIELD`, `accepted_submit_contract_is_current`, `accepted_submit_row_kind`, `apply_accepted_submit_transition`;
      - `file_journal_retry_helpers`: `_accepted_submit_attempt_anchor`, `_accepted_submit_master_identity`, `_is_legacy_unversioned_forecast_cohort_master`;
      - `file_journal_candidates`: `_cycle_time_from_job`, `_source_id_from_job`;
      - `file_journal_paths._optional_safe_identity` and `scheduler_state._format_utc`;
      - `forcing_submit_identity`: `forcing_attempt_comment_for`, `forcing_member_identity_is_complete`.
    - `field` is a local variable (Store context), not `dataclasses.field`. Do not import it.
    - Annotation-only: `Any`.
  - Expected F401 removal: `forcing_attempt_comment_for`. Its only facade uses are at `:3319`, inside C12, and in a `:5622` comment. It is not in `__all__` and not in J0. Production modules import it from `forcing_submit_identity`, not from the facade. Verify.
  - Status literals: the method holds the `{"submission_failed", "reservation_lost"}` set literal (`:3230`). It is the facade's only literal with those members, so the facade row at `tests/status_literal_allowlist.json:36` is re-keyed to `services/orchestrator/file_journal_reclaim.py`.
  - Consumers:
    - production: `reservation.py:261` calls `getattr(repository, "reclaim_pipeline_job_reservation", None)` inside `reserve_candidate` (`:171`); the chain reaches it via `chain_forecast_orchestrator_cycle.py:818` (`_chain.reserve_candidate`). The scheduler reaches it only through the chain;
    - tests: 39 direct calls across 16 files; the fakes in `tests/test_orchestration_chain.py:364,666,11047` and `tests/gateway_reconcile_helpers.py:86`, and the `OrchestratorRepository` Protocol stub at `chain.py:387`, are other classes;
    - no class-level patch of the name.
  - The class header is already 120 characters, so a fourth base needs a wrapped base list. Use one base per line with a trailing comma (below): it passes E501/I and `ruff format` leaves it unchanged.
- Changes (implementer):
  1. New `services/orchestrator/file_journal_reclaim.py`. It starts with a module docstring, then `from __future__ import annotations`, then one top-level class, `_FileJournalReclaimMixin`, decorated `@_install_public_read_attribution`. Move the method verbatim; only name resolution changes.
  2. Name resolution follows the J1, J2a and J2b precedent:
     - `_utcnow` is read off the facade at call time, as `file_orchestration_journal._utcnow()`. It is never bound by import, at top level or in a function body, and never imported from `time_normalization`.
     - `FileOrchestrationJournalError` comes from a function-local facade import, placed as the first statements of the body because the method has no docstring.
     - Every other owner-module name is imported at the sibling's top level from its owner.
     - The sibling never imports the facade at top level outside `TYPE_CHECKING`.
  3. Facade changes:
     - Import `_FileJournalReclaimMixin` and wrap the class header to one base per line, in this order:
       ```python
       class FileOrchestrationJournalRepository(
           _FileJournalRetentionMixin,
           _FileJournalEventsMixin,
           _FileJournalReleaseMixin,
           _FileJournalReclaimMixin,
       ):
       ```
       The facade keeps its own decorator.
     - Delete the method. `__all__` and the facade-defined names stay.
     - Remove facade imports the move leaves unused (F401). Exceptions: a pinned J0 4(a) name and `_UNSET` stay, marked `# noqa: F401`.
  4. Cluster map: append `"reclaim"` to `CLUSTERS` and add `clusters["reclaim"] = ["_FileJournalReclaimMixin"]`. Re-derive `patched[*].callers`; the expected only change is that `_utcnow` gains `"reclaim": ["_FileJournalReclaimMixin"]`. Update the docstring's cluster count and order sentence.
  5. File-keyed guards. Run the status-literal scan with the new file indexed. Expected: delete `:36` and insert `{"file": "services/orchestrator/file_journal_reclaim.py", "members": ["reservation_lost", "submission_failed"], "reason": "distinct-vocabulary"},` between `:24` (`file_journal_events.py`) and `:25` (the first `file_journal_retry_helpers.py` row); there is no `file_journal_release.py` row. Relocate, never just delete. Also check `line_reference_baseline.json`, `pyproject.toml` per-file-ignores, the first-import allowlist and the facade `__all__` consumer rule. A deviation from the expected result is stop-and-report.
- Must preserve:
  - The J0 data file is byte-identical, and the J0 test is unchanged and green.
  - These files are unchanged: `tests/test_file_journal_late_lookup_seams.py`, `file_journal_retention.py`, `file_journal_events.py`, `file_journal_release.py`.
  - No behaviour, signature, exception, log or on-disk format change. The moved `FunctionDef` is AST-identical to HEAD's after the documented rewrites.
  - No `file.py:NNN` line reference is added in shipped Python.
- Deviations to record in the PR body:
  - `tests/test_file_journal_cluster_map.py`: `CLUSTERS` and its docstring;
  - the one `tests/status_literal_allowlist.json` relocation;
  - the 16.1 tick in `tasks.md`.
- Out of scope:
  - C14 (#3280) and C16 (#3281), and every other cluster;
  - renaming anything;
  - the J1-J2b carry-overs: seam tests, the spec cluster count, the `_cycle_write_owner` guard, and the release docstring clause.
- Required evidence, in `.workplans/3279/`:
  1. `ast-parity.md`:
     - `ast.dump` of HEAD's whole `FunctionDef` against the moved one, after dropping the inserted function-local imports and mapping `file_orchestration_journal._utcnow` back to `_utcnow`; they must be identical;
     - the facade diff only deletes the method, plus the import lines and the class header.
  2. `red-runs.log`, each a temporary edit reverted afterwards:
     - (a) binding `_utcnow` by import in the sibling makes the late-lookup lint fail;
     - (a') calling `utc_now` directly at every moved site makes `test_patched_callers_match_the_ast` fail;
     - (b) dropping the mixin decorator makes J0 set 2 fail;
     - (c) removing `"reclaim"` from `CLUSTERS` makes the 13.2 unregistered-sibling message appear; the shape check and the callers check also fire;
     - (e) keeping the facade allowlist row instead of relocating it makes the status-literal scan report a stale facade row and an unlisted sibling literal. Run it with `file_journal_reclaim.py` indexed (temporary index); untracked, only the stale-row half fires.
  3. `moves.md`: the relocation, with the scan output before and after.
  4. `checks.log`:
     - `uv run --no-sync ruff check . --no-cache`, `git diff --check`, `openspec validate redesign-orchestration-core --strict --no-interactive`, and the local test list;
     - each `git ls-files` scanner re-run in its own temporary index with the new file;
     - the entropy tests that `git init` inside `tmp_path` run on the real index.
- Verification:
  - Locally:
    - `tests/test_file_journal_*.py` and `tests/test_file_orchestration_journal*.py`;
    - every test file that calls `reclaim_pipeline_job_reservation`, found by grep;
    - every test file that calls `reserve_candidate` or imports `tests/forcing_submit_ambiguity_helpers.py` / `tests/orchestrator_bind_reserved_job_helpers.py` (which wrap it), plus every test file that imports `services.orchestrator.reservation`. At 73c42d8fb these are: `test_file_journal_legacy_unversioned_reserved_guard.py`, `test_forcing_submit_ambiguity.py`, `test_forcing_submit_ambiguity_reservation.py`, `test_gateway_reconcile_grace_guard.py`, `test_gateway_reconcile_idempotency_barrier.py`, `test_gateway_reconcile_reservation_lifecycle.py`, `test_operator_action_listing_held_reservations.py`, `test_orchestration_chain.py`, `test_orchestrator_bind_reserved_job_cas.py`, `test_orchestrator_bind_reserved_job_cli.py`, `test_orchestrator_bind_reserved_job_forcing.py`, `test_orchestrator_demote_reclaim_lifecycle.py`, `test_orchestrator_demote_reserved_job_forcing.py`, `test_orchestrator_first_import.py`;
    - `tests/test_status_literal_scan.py`, `tests/test_status_vocab_parity.py`, `tests/test_import_layering_guard.py`, `tests/test_orchestrator_first_import.py`, `tests/test_facade_name_resolution.py`, `tests/test_operator_action_listing.py`, `tests/test_operator_action_status_closure.py`;
    - every test file that scans `services/**` or `tests/**` and names the facade or `file_journal_`.
  - node-27: a fresh-process import check over the `integration` files, then `-m integration` over the files that import the facade or `file_journal_reclaim`, on throwaway `nhms_it_*` DBs. Expect all to pass and no leftover DBs.
  - CI: the full suite.
  - node-22: the D5 pure-move batch, task 16.4. This is the second batch, after J1, J2a and J2b.

Slice triage (J3b, #3280, tasks 16.2, 16.4):
- Issue type: refactor (pure move). Fixture level: expanded (agree): it moves the operator demote/bind writers, which rewrite persisted reserved-job records. Width: one PR, atomic.
- Recon at 33099522f, after J1, J2a, J2b and J3a merged. The Appendix B range for C14 (4904-5829 at 54556b1e7) has moved up by about 330 lines; the HEAD ranges are below.
  - C14 is 5 methods of `FileOrchestrationJournalRepository` in `services/orchestrator/file_orchestration_journal.py`. All are undecorated, all have docstrings, none uses `LOGGER`, and none has an `_UNSET` default:
    - public, attribution-wrapped:
      - `demote_operator_verified_reserved_job` `:4571-4839`;
      - `bind_operator_verified_reserved_job` `:5054-5302`;
    - private:
      - `_forcing_demote_input_refusal` `:4841-4875`;
      - `_demote_operator_verified_forcing_locked` `:4877-5052`;
      - `_bind_operator_verified_forcing_locked` `:5304-5495`.
  - Facade-defined names read at runtime, per method:
    - demote: `FileOrchestrationJournalError`, `OperatorDemoteReceipt`, `OperatorDemoteResult`, `ProjectionWarning`;
    - `_forcing_demote_input_refusal`: `FileOrchestrationJournalError`;
    - `_demote_operator_verified_forcing_locked`: the same four names as demote;
    - bind: `FileOrchestrationJournalError`, `OperatorBindReceipt`, `OperatorBindResult`, `_OPERATOR_BIND_COMMIT_REFUSALS`, `_OPERATOR_BIND_SLURM_ID_RE`, `_OperatorBindAudit`;
    - `_bind_operator_verified_forcing_locked`: `FileOrchestrationJournalError`, `OperatorBindReceipt`, `OperatorBindResult`, `_OPERATOR_BIND_FORCING_ARRAY_SPEC_RE`, `_OperatorBindAudit`.

    Signature annotations also read facade-defined names, and a function-local import does not bind them: `OperatorDemoteReceipt` and `OperatorDemoteResult` (demote), `OperatorDemoteResult` (`_demote_operator_verified_forcing_locked`), `OperatorBindResult` (bind and `_bind_operator_verified_forcing_locked`). Import exactly these three from the facade under a top-level `if TYPE_CHECKING:` block (precedent `file_journal_retention.py:43`), in addition to the function-local imports. Without that block ruff reports F821 five times. The local `list[ProjectionWarning]` annotations are covered by the local import.
  - Facade-resident patched name: `_utcnow`, read in demote, the demote forcing branch and the bind forcing branch. The facade still uses it elsewhere, so the facade keeps its import.
  - Names the facade imports from owner modules, which the sibling imports from those owners:
    - `accepted_submit_identity`: `ACCEPTED_SUBMIT_CONTRACT_VERSION`, `ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD`, `AcceptedSubmitTransition`, `OPERATOR_VERIFIED_ABSENCE_DECISION`, `accepted_submit_contract_is_current`, `accepted_submit_row_kind`, `apply_accepted_submit_transition`, `is_forecast_cohort_stage_name`, `normalize_slurm_accounting_submitted_at`;
    - `scheduler_state_types.ACTIVE_HYDRO_STATUSES`;
    - `reservation.SLURM_COMMENT_PREFIX`;
    - `file_journal_retry_helpers`: `_accepted_submit_attempt_anchor`, `_bounded_cohort_members`, `_journal_record_for_write`, `_operator_evidence_text`, `_operator_owner_evidence_text`, `_projection_error_reason`, `_projection_error_type`, `_redact_durable_error_message_fields`, `_strict_utc_datetime`;
    - `file_journal_candidates`: `_accepted_submit_source_cycle_from_job_id`, `_forcing_submitline_array_spec`;
    - `file_journal_paths._optional_safe_identity`;
    - `scheduler_state._format_utc`;
    - `forcing_submit_identity`: `forcing_submit_identity_is_complete`, `is_forcing_stage_name`;
    - `collections.abc.Mapping`.

    Stdlib annotation-only names: `Any`, `datetime`.
  - Expected F401 removals: `_forcing_submitline_array_spec`, `_operator_evidence_text`, `_operator_owner_evidence_text` and `is_forcing_stage_name`. None of them is in `__all__` or J0, and no module reads them through the facade. Verify this.
  - The receipt dataclasses (`OperatorBindReceipt`, `OperatorBindResult`, `OperatorDemoteReceipt`, `OperatorDemoteResult`, `_OperatorBindAudit`, `ProjectionWarning`) and the `_OPERATOR_BIND_*` constants stay in the facade, per task 16.2. `operator_reserved_demotion.py` and others import them from there.
  - Status literals: the 5 methods hold no `Tuple`/`Set`/`List` with two or more `str` members. Expected allowlist result: unchanged. Verify this with the real scan.
  - Consumers:
    - production: `operator_reserved_demotion.py:95` and `operator_reserved_bind.py:94`, both through the instance;
    - tests: the shared helpers `tests/orchestrator_demote_reserved_job_helpers.py` and `tests/orchestrator_bind_reserved_job_helpers.py`, plus the `test_orchestrator_demote_*`, `test_orchestrator_bind_reserved_job_*`, `test_gateway_reconcile_*`, `test_hydro_run_attempt_triage.py` and `test_file_journal_legacy_unversioned_reserved_guard.py` files;
    - an instance-level `monkeypatch.setattr(held_repository, "demote_operator_verified_reserved_job", ...)` at `tests/test_orchestrator_demote_cli_security.py:396`;
    - no class-level patch.
  - C14 calls into C13 (`permit_*_retry` and submit lifecycle) through `self`. These calls resolve through the MRO while C13 stays in the facade.
- Changes (implementer):
  1. Create `services/orchestrator/file_journal_operator.py`. It starts with a module docstring, then `from __future__ import annotations`, then one top-level class `_FileJournalOperatorMixin`, decorated with `@_install_public_read_attribution`. Move the 5 methods verbatim and in HEAD order; only name resolution changes. The docstring notes that the receipt dataclasses stay in the facade.
  2. Name resolution follows the J1-J3a precedent:
     - `_utcnow` is read off the facade at call time, as `file_orchestration_journal._utcnow()`. It is never bound by import and never imported from `time_normalization`.
     - Facade-defined runtime names come from function-local facade imports placed right after the method docstring. Each method imports only the names its own body reads.
     - Owner-module names are imported at the sibling's top level.
     - The sibling never imports the facade at top level outside `TYPE_CHECKING`. That block holds only `OperatorBindResult`, `OperatorDemoteReceipt` and `OperatorDemoteResult`.
     - The two existing lazy imports stay function-local and verbatim at their HEAD positions: `from services.orchestrator import reconcile` (`:4931`) and `from services.orchestrator.reconcile import _submitline_comment_key` (`:5147`). Do not hoist or merge them: `reconcile` imports the facade at load time, and the facade imports this sibling.
     - Function-local import lists follow ruff isort order (`_OPERATOR_BIND_*` constants first); the facade's new `file_journal_operator` import sorts before `file_journal_reclaim`.
  3. In the facade:
     - Import `_FileJournalOperatorMixin` and append it as a fifth base, one per line with a trailing comma, after `_FileJournalReclaimMixin`. The facade keeps its own decorator.
     - Delete the 5 methods. `__all__` and all facade-defined names stay.
     - Remove the imports that become unused (F401). Exceptions: a pinned J0 4(a) name or `_UNSET` keeps `# noqa: F401`.
  4. In the cluster map:
     - Append `"operator"` to `CLUSTERS` and add `clusters["operator"] = ["_FileJournalOperatorMixin"]`.
     - Re-derive `patched[*].callers`. The expected only change is that `_utcnow` gains `"operator": ["_FileJournalOperatorMixin"]`.
     - Update the docstring's cluster count and order sentence.
  5. File-keyed guards. Expected result: `tests/status_literal_allowlist.json` stays byte-identical and no row relocates. One addition: the four moved `except Exception` handlers (HEAD `:4800`, `:4817`, `:5014`, `:5026`) trip BLE001 outside the facade's `pyproject.toml:146` ignore. Add `"services/orchestrator/file_journal_operator.py" = ["BLE001"]` to `[tool.ruff.lint.per-file-ignores]` directly above the `file_journal_paths.py` row (`:145`), as #2945 did for `file_journal_paths.py`. Use no inline `# noqa`, add no B904, and leave the facade's row unchanged. Also check `line_reference_baseline.json`, the first-import allowlist and the facade `__all__` consumer rule. Any other deviation is stop-and-report.
- Must preserve:
  - The J0 data file and test are unchanged and green.
  - `tests/test_file_journal_late_lookup_seams.py` and the four earlier J siblings are unchanged.
  - No behaviour, signature, exception, log or on-disk format change. The moved `FunctionDef`s are AST-identical to HEAD after the documented rewrites.
  - No `file.py:NNN` line reference is added in shipped Python.
- Deviations, recorded in the PR body:
  - `tests/test_file_journal_cluster_map.py` changes (`CLUSTERS` and its docstring);
  - the 16.2 tick in `tasks.md`;
  - `pyproject.toml`: one per-file-ignores row for `file_journal_operator.py` (outside the issue's PR Boundary).
- Out of scope:
  - moving the receipt dataclasses or `_OPERATOR_BIND_*` constants;
  - C13 (#3282), C16 (#3281) and the other clusters;
  - renaming;
  - the J1-J3a carry-overs.
- Required evidence (`.workplans/3280/`):
  1. `ast-parity.md`, with three parts:
     - For each of the 5 methods, compare `ast.dump` of HEAD's whole `FunctionDef` with the moved one, after dropping the inserted function-local imports (only `from services.orchestrator import file_orchestration_journal` and `from services.orchestrator.file_orchestration_journal import …`; a filter on `module == "services.orchestrator"` also strips the `reconcile` lazy import and false-reds `_demote_operator_verified_forcing_locked`) and mapping `file_orchestration_journal._utcnow` back to `_utcnow`. They must be identical, docstrings included.
     - The facade diff only deletes those methods, plus the import lines and the class header.
     - Run the parity script against a tampered copy and show that it fails.
  2. `red-runs.log`, each a temporary edit reverted afterwards:
     - (a) binding `_utcnow` by import in the sibling makes the late-lookup lint fail;
     - (a') calling `utc_now` directly at every moved site makes `test_patched_callers_match_the_ast` fail;
     - (b) dropping the mixin decorator makes J0 set 2 fail;
     - (c) removing `"operator"` from `CLUSTERS` makes the 13.2 unregistered-sibling message appear; the shape check and the callers check also fire.
  3. `moves.md`: "none", with the status-literal scan before and after.
  4. `checks.log`:
     - `uv run --no-sync ruff check . --no-cache`, `git diff --check`, `openspec validate redesign-orchestration-core --strict --no-interactive`, and the local test list;
     - each `git ls-files` scanner re-run in its own temporary index that contains the new file;
     - the entropy tests that `git init` inside `tmp_path` run on the real index.
- Verification:
  - Locally:
    - `tests/test_file_journal_*.py` and `tests/test_file_orchestration_journal*.py`;
    - C14 consumers (callers of the 5 methods, importers of the two helper modules, importers of `operator_reserved_demotion` / `operator_reserved_bind`), at 33099522f: `test_file_journal_legacy_unversioned_reserved_guard.py`, `test_file_orchestration_journal.py`, `test_gateway_reconcile_binding_provenance_normalizer.py`, `test_gateway_reconcile_comment_fallback_guard.py`, `test_hydro_run_attempt_triage.py`, `test_hydro_status_set_parity.py`, `test_operator_action_listing_held_reservations.py`, `test_orchestration_chain.py`, `test_orchestrator_bind_reserved_job_cas.py`, `test_orchestrator_bind_reserved_job_cli.py`, `test_orchestrator_bind_reserved_job_forcing.py`, `test_orchestrator_bind_reserved_job_lane.py`, `test_orchestrator_demote_cli_security.py`, `test_orchestrator_demote_core_cas.py`, `test_orchestrator_demote_projection_faults.py`, `test_orchestrator_demote_reclaim_lifecycle.py`, `test_orchestrator_demote_reserved_job_forcing.py`, `test_scheduler_journal_root_authority.py`;
    - `tests/test_entropy_audit_line_references.py`;
    - `tests/test_status_literal_scan.py`, `tests/test_status_vocab_parity.py`, `tests/test_import_layering_guard.py`, `tests/test_orchestrator_first_import.py`, `tests/test_facade_name_resolution.py`, `tests/test_operator_action_listing.py`, `tests/test_operator_action_status_closure.py`;
    - every test file that scans `services/**` or `tests/**` and names the facade or `file_journal_`.
  - node-27: a fresh-process import check over all `integration`-marked files, then `-m integration` over the files that import the facade or `file_journal_operator`, on throwaway `nhms_it_*` DBs. Expected: all pass and 0 leftover DBs.
  - CI: the full suite.
  - node-22: the D5 pure-move batch (16.4), the second batch.

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
  - rewrite the `file-orchestration-journal` Purpose (`:4-5`) so it no longer says "contract-tested against the database semantics" (R3-2; Purpose text is outside the delta format);
  - run `openspec archive redesign-orchestration-core --yes`.

Suggested fixture level: none - bookkeeping inside the last slice's PR.
Minimal mergeable slice: atomic - rides in the last slice's final push.
