# Design

## Context

Facts below are from static reading of master (54556b1e7 / 6bf97fdc8) plus one live env check on node-22. Line numbers are as of that head.

**Which repository each node uses.**

| Entry point | Node | Orchestration store |
|---|---|---|
| `nhms-compute-scheduler.service` → `services.orchestrator.cli plan-production --submit --continuous --max-passes 1` (`infra/systemd/node22/nhms-compute-scheduler.service:13`) | 22 | `FileOrchestrationJournalRepository` + `FileJournalRetryService`, selected by `NHMS_SCHEDULER_DB_FREE_REQUIRED=true` (`scheduler_config/config.py:39,110`; `scheduler_core.py:122-136`) |
| Journal retention / restore units, operator CLIs (`demote-reserved`, `bind-reserved`, `released-reservation-recovery`, census, rollback/rollforward), `scripts/node22_manual_retry_failed_runs.py` | 22 | file journal |
| `nhms-compute-api.service` (:8001) | 22 | none: no `DATABASE_URL` in its process env, so `get_pipeline_store` (`apps/api/routes/pipeline.py:116-125`) answers 500 for any route that needs it |
| `nhms-display-api.service` (:8080) | 27 | SQLAlchemy `PipelineStore` read paths (`persistence.py`), read-only role |
| `nhms-node27-autopipe.service` | 27 | no repository class: `pipeline_job_provenance.py` projects node-22's `runs/<run_id>/input/pipeline_jobs.json` sidecar into `ops.pipeline_job` with raw psycopg2 |

The split itself is deliberate (`docs/runbooks/two-node-deployment-overview.md:28-32`): node-22's journal is the source of truth, and `ops.pipeline_job` is a derived read model.

**What no production entry point reaches.**
- `PsycopgOrchestratorRepository`: `chain_repository.py:41`, about 960 lines.
- `trigger-analysis` and `AnalysisOrchestrator`: `cli.py:73-86,507-512,802,879-881`; `chain_analysis_orchestrator.py`, 179 lines.
- The non-DB-free scheduler branches: `scheduler_core.py:128,222-224,241-250,525-535`; `scheduler_runtime.py:1856-1900`; `scheduler_adapters.py:180`.
- The SQL `RetryService`: `retry.py:251-415`.
- `get_retry_service`: `pipeline.py:128-132`; no `Depends()` uses it.
- `_retry_service_from_env`: `chain_compat_runtime.py:212-227`.

The operator rollback tool's historical export (`file_orchestration_migration.py:1451`) uses its own psycopg connection and does not depend on any of these.

**Retry/attempt accounting today.**
- `retry_identity.py` already owns:
  - suffix parsing;
  - `effective_retry_attempt`;
  - attempt floors;
  - the per-member charge (#2542);
  - the manual-claim gate (#1201).
- Retry job ids are built by hand in five places (four after R4 deletes the `retry.py` one):
  - `chain_runtime_utils.py:448`
  - `file_journal_retry_helpers.py:55`
  - `chain_forecast_orchestrator_cycle.py:297`
  - `retry.py:~286`
  - `file_orchestration_journal.py:12459`
- Next-attempt formulas:
  - `chain.py:863`
  - `chain_forecast_execution.py:573/625`
  - `chain_forecast_orchestrator_cycle.py:296`, plus `int(job_id.rsplit("_retry_", 1)[1]) + 1` at `:676-683` and `:750`
  - `file_journal_retry_helpers.py:51`
  - `file_orchestration_journal.py:12432` (current-master path, suffix-aware), `:12455-12459` (non-master path, `source.retry_count + 1`, ignores the suffix) and `:12678` (manual path)
  - `retry.py:~285`
- Stage attempt reads: `scheduler_state_rows.py:517-656`. `chain_runtime_utils.py` `_stage_job_sort_key` is a third parser.
- Retry limits: there are two operator knobs, `NHMS_SCHEDULER_RETRY_LIMIT` (`scheduler_config/config.py:169-171`, default `DEFAULT_RETRY_LIMIT` = 3 at `scheduler_state_types.py:11`) and `SLURM_GATEWAY_MAX_RETRIES` (`services/slurm_gateway/config.py:60`, read through `RetryConfig.from_settings`, `retry.py:204-210`, used by `FileJournalRetryService`). `docs/runbooks/failed-basin-retry.md:240-243` documents raising the first.
- Status sets: `chain_forecast_orchestrator_cycle.py:224` and `:247` hard-code two sets, and both differ from the sets in `retry.py:67-78`.
- Two paths ignore the `_retry_<n>` suffix: the non-master path at `file_orchestration_journal.py:12455-12459` and the SQL `RetryService`. Neither has a test.

**Compat facades and late binding.**
- How owners reach the facades:
  - 16 owner modules read names off `services.orchestrator.scheduler` at call time, through `_scheduler.X` (459 reads, 167 distinct names).
  - 9 owner modules do the same with `services.orchestrator.chain`, through `_chain.X` (649 reads, 125 distinct names).
  - This late binding is why a test's `monkeypatch.setattr(scheduler, ...)` or `monkeypatch.setattr(chain, ...)` takes effect.
  - The owner decision is to keep it.
- What the facades receive at import time:
  - `scheduler_compat_runtime.py` (369 lines) is merged by `scheduler.py:688-690`. Its `EXPORTS` hold 42 names. Most are `_SCHEDULER_*_COMPAT_*` self-check tables, but it also defines production helpers: `_now` (:311), `_sleep` (:315), `_bounded_evidence_payload` (:195), `_evidence_status` (:208), and the `_source_*` / `_accepted_horizon_from_hours` aliases (:72-75). Every pass reads these through `_scheduler.` (`scheduler_core.py:155,709,920-926`; `scheduler_runtime.py:265,570,591,2263`).
  - `chain_compat_static.py` (803 lines) contributes 93 exports, merged by `chain.py:147-149`.
  - `chain_compat_runtime.py` (564 lines), merged by `chain.py:887-889`, defines `_utcnow` (:209, installed at :226). `_utcnow` is read through `_chain.` at `chain.py:697`, `chain_forecast_orchestrator_cycle.py:448` and `chain_forecast_orchestrator_runtime.py:290`.
  - Other installers:
    - `scheduler.py:230-234` (`install_scheduler_state_compat`, which also propagates facade patches back to the owner, `scheduler_state_compat.py:169-219`);
    - `scheduler.py:686` (`scheduler_candidate_runtime.EXPORTS`, 13 `_COMPAT_` tables);
    - facade-local `_COMPAT_` tables (`scheduler.py:238-292`; `chain.py:~185-204`, the chain-manifest forwarder self-check).
  - `_COMPAT_`-named entries in the final namespaces: 65 in `scheduler` and 150 in `chain`.
- What pins the facades in place:
  - `docs/governance/{CHAIN,SCHEDULER}_COMPATIBILITY_INVENTORY.md`;
  - `scripts/governance/entropy_audit/facade_guard.py` with `COMPATIBILITY_FACADE_CONFIGS` (`schema.py:206-217`);
  - helpers in `scripts/governance/audit_repo_entropy.py:257-269,408,627,688-694` and `tests/entropy_audit_helpers.py:587,598`;
  - their tests;
  - about 370 `_COMPAT_` assertions in `tests/test_orchestration_chain.py` and `tests/test_production_scheduler.py`;
  - the `FIRST_IMPORT_ALLOWLIST` (`tests/test_orchestrator_first_import.py:38-67`).
- `__module__` is also rewritten at `chain.py:258-268` and `scheduler.py:378,389,415`. The rewrite at `scheduler.py:415` is pinned by `orchestrator-structural-burndown`.

**`NHMS_SCHEDULER_DB_FREE_REQUIRED` is a cross-plane contract, not only a scheduler switch.**
- The path to the workers: the scheduler copies the raw env into every run manifest (`chain_manifests.py:252`). The gateway exports it to the job env (`services/slurm_gateway/real_backend.py:1048`) and only exports the DB-free worker switches (`NHMS_CANONICAL_DB_FREE`, `NHMS_FORCING_DB_FREE`, file registry) when it is true (`:1059-1075`, `:2370-2371`).
- Workers that read it: `workers/shud_runtime/runtime.py:145`, `workers/forcing_producer/file_store.py:1019`, `workers/output_parser/parser.py:1541`, `workers/canonical_converter/converter.py:54`, `packages/common/state_cli.py:430`.
- Where the deployment requires it: `infra/compose.compute.yml:21` (`:?`), `infra/env/compute.example:67`, `infra/env/compute.scheduler-dbfree.env.example:22,97`.
- In-repo settings: `scheduler_candidate_runtime.py:578` builds a shim config with `scheduler_db_free_required: False`, and the rollback tool passes `scheduler_db_free_required=True` (`file_orchestration_migration.py:1236`).

**Journal repository.**
- `FileOrchestrationJournalRepository` spans `file_orchestration_journal.py:1304-12266` and has 207 methods by AST count.
- The responsibility clusters C1–C18 are listed in Appendix B.
- Shape of the clusters:
  - C1–C6, C8–C9 and the C10 reads are the shared core;
  - C7 (reconcile inventory) consumes the core heavily, but nothing else depends on it except underscore callers;
  - the leaves are retention (C18), status/events (C17), identity-blocked release (C15), reclaim (C12), operator demote/bind (C14) and cohort projection (C16);
  - submit lifecycle (C13) has back-edges from C11 and C14.
- Constraints on moving code:
  - class-level `monkeypatch.setattr` in tests;
  - six test subclasses;
  - module-level patch points (`read_bytes_limited_no_follow`, `_utcnow`, `MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES`) that moved code must read off the facade module at call time (`tests/test_file_journal_late_lookup_seams.py`);
  - module-level dataclasses imported from the facade by other modules (operator receipts `:1104-1247`, imported by `operator_reserved_demotion.py`; retention dataclasses `:976-1020`, imported by `scheduler_journal_retention.py` and `scheduler_journal_archive.py`);
  - `@_install_public_read_attribution` wraps only `vars(cls)` (`file_journal_read_attribution.py:116-130`);
  - the cluster-map fixture `tests/fixtures/file_journal_cluster_map.json` with its late-lookup lint (`tests/test_file_journal_cluster_map.py:233-250`, which covers only `EXTRACTION_ORDER` siblings).

## Goals / Non-Goals

**Goals:**
- One orchestration write path, the file journal, in code, tests and specs.
- One module that decides retry ids, attempts, the default limit and the retryable statuses, with the historical cases pinned in one table.
- No programmatic namespace installation (`globals().update`, `setdefault` loops) in `chain` / `scheduler`, no `_*_COMPAT_*` self-check tables, and no inventories or guards whose only job is to keep those tables honest.
- A journal repository whose leaf responsibilities live in named modules, without any behaviour or format change.

**Non-Goals:**
- Changing journal on-disk formats, the SQL schema, the published OpenAPI contract, or the provenance projection.
- Unifying the suffix-ignoring retry path (`file_orchestration_journal.py:12455-12459`), or merging the two retry-limit knobs. Each is a behaviour change with its own issue.
- Replacing late binding through the facades (`_scheduler.X`, `_chain.X`) with direct imports, or retargeting the tests that monkeypatch through the facades. The owner chose to keep late binding.
- Removing the `__module__` rewrites at `chain.py:258-268` and `scheduler.py:378,389,415`. They give public names a stable module, and `orchestrator-structural-burndown` pins the one in `scheduler.py`.
- Removing `NHMS_SCHEDULER_DB_FREE_REQUIRED` from deployment templates or from the gateway/worker job env. It stays as the cross-plane DB-free contract.
- Changing worker-side DB modes.
- Deleting the `workers/data_adapters` GFS/IFS adapters, which `workers/data_adapters/cli.py` uses. Only the scheduler's `_default_adapters` goes.
- The cancel route (#3245) and the gateway backend default (#3246).
- Splitting the shared journal core (C1–C6, C8–C11), or breaking up single large methods such as `project_forecast_cohort_tasks`.

## Decisions

**D1. Retire the SQL orchestration write path; DB-free is the only scheduler mode.** Decided by the owner. The alternatives were "keep as legacy" and "delete only `trigger-analysis` plus `RetryService`", which keeps the costliest part. Every symbol has exactly one deleting slice (Appendix C). The slices:

- **R1. Delete the orchestrated analysis lane.**
  - Code: `trigger-analysis`, `AnalysisOrchestrator`, `chain_analysis.py`, `build_analysis_run_manifest` (`chain_manifests.py:762`), the analysis-only branches (`ANALYSIS_STAGES` / `AnalysisRunContext` uses, `chain_forecast_execution.py:1687`), and `AnalysisPipelineAlreadyActiveError`.
  - Specs: then triage the remaining `analysis-run-pipeline` requirements against the worker-level analysis branches (`workers/output_parser/parser.py`). Each one either gets a REMOVED delta or is rewritten as a worker-only requirement.
- **R2a. Make every test build the scheduler explicitly in DB-free mode.** Test-only PRs, split by file.
  - Each test passes explicit `adapters=`, the file backend selectors and a journal root.
  - The SQL-factory monkeypatches (`_active_repository_from_env`, `_orchestrator_repository_from_env`, `_retry_service_from_env`, `_canonical_readiness_provider_from_env`, `_forcing_producer_from_env`, `_default_adapters`) are replaced with injected collaborators.
  - The flag can still select during these PRs, so each one is green on its own.
- **R2b. Flip.**
  - `scheduler_db_free_required` becomes constant `True`. The constructor parameter stays, is accepted and is ignored, so the rollback tool's `True` and existing callers keep working.
  - The non-DB-free arms fail closed with a typed error.
  - `chain_manifests.py:252` writes `"true"` unconditionally, and `scheduler_candidate_runtime.py:578` stops forcing `False`.
  - The backend selectors keep their fail-closed `unset`/`postgres` handling: no default flips to `file`.
- **R2c. Delete the dead arms.**
  - `_default_adapters` and its re-export.
  - The SQL factory callers in `scheduler_core.py` (`:127`, `:214-226`, `:455-458`, `:525-535`, `:736-745`) and `scheduler_adapters.py:405-430`, with their `scheduler.py` re-exports.
  - The SQL reconcile store and its reset/dispose logic in `scheduler_runtime.py`.
  - The PostgreSQL scheduler lease.
  - The tests whose subject is these arms (`tests/test_gateway_reconcile_store_reset.py`, the `PostgresSchedulerLease` / `_postgres_advisory_lock_key` tests).
- **R3. Delete `PsycopgOrchestratorRepository`, `_active_repository_from_env`, `_orchestrator_repository_from_env` and `ForecastOrchestrator.from_env`.** Depends on R1 and R2c.
  - The `test_psycopg_*` tests that assert SQL text are deleted.
  - Candidate-state semantics tests are kept, after confirming that `chain_repository_state.py` already covers them on the file-journal path. Where it does not, they are migrated.
- **R4. Delete the SQL `RetryService` (`retry.py:251-402`), `get_retry_service`, `_retry_service_from_env` and their aliases.** Depends on R3. `_auto_retry_job_can_be_reused`, `_safe_error_message` and every other name in `retry.py` stay.
- **R5. Delete the `PipelineStore` write methods** that no production code calls once R2c is in: `create_job`, `reserve_job`, `reclaim_reservation`, `bind_reservation`.
  - Test fixtures that seed rows through them switch to ORM inserts.
  - `update_job_status` and `insert_event` stay because the cancel route uses them.

Each R slice also:
- updates the inventory text and the inventory-coupled tests that name the symbols it deletes, so the guards stay green until F3 removes them;
- adds MODIFIED/REMOVED deltas to this change for every main-spec passage that names those symbols (Appendix D).

**D2. Retry accounting: pin, then own.** Decided by the owner.

- **T1** runs after R4 and adds a table-driven test. It has two parts:
  - **Plain-data rows** call today's pure functions: rows A–F, M, N, Q, the `:676-683` / `:750` rsplit formulas, the inline `failure_number - 1` term, the non-master path, the status-set difference, and the default limit with both env overrides.
  - **Integration rows** G–L, O and P reference their existing integration tests. No pure function exists for them today.
- **T2** moves the following into `retry_identity.py` as pure functions:
  - `retry_job_id` (keeping the `max(attempt, 1)` clamp);
  - `next_stage_attempt`;
  - `mint_attempt(..., is_occupied)`;
  - `inline_retry_attempt`;
  - `next_after_failure_master` and `next_after_failure_non_master`, two named functions that keep today's divergence;
  - `stage_attempt`;
  - `restart_stage_from_basins`;
  - the default limit constant;
  - the two status sets, unchanged.
- In T2:
  - Call sites delegate to the owner.
  - The env overrides `NHMS_SCHEDULER_RETRY_LIMIT` and `SLURM_GATEWAY_MAX_RETRIES` keep their current readers; only the default moves.
  - Plain-data rows retarget to the owner functions with unchanged expected values, and row L becomes a plain-data row.
- T2 adds an AST guard. Outside `retry_identity.py`:
  - no f-string, concatenation, `%` / `.format` or `join` may build an id from the `_retry_` literal or `RETRY_JOB_ID_MARKER`, and no code may split on that marker;
  - no integer literal may be added to the result of any `retry_identity` function (the set is derived from the module, so a new owner function is covered automatically), of any call whose name contains `attempt`, or of a split on the marker.

  Calls to the owner's parsers, regex matches and `in` membership tests are allowed; splitting on the marker outside the owner is not. The allowlist starts empty, because T2 left no local site, and any entry needs a written reason.
- The alternative, unifying the paths while moving them, was rejected because it mixes a behaviour change into a move.

**D3. Narrow facade cleanup; late binding stays.** Re-decided by the owner after Stage 3 showed the facades are a production late-binding hub, not test-only.

- **F1.** Move the production helpers defined in compat modules into owner modules:
  - `_now`, `_sleep`, `_bounded_evidence_payload` and `_evidence_status` go to a scheduler owner, with the `_source_*` aliases left as explicit aliases;
  - `_utcnow` goes to a chain owner.

  The facades import these names explicitly, so `_scheduler.X` and `_chain.X` still resolve. A static guard is added: every attribute an owner module reads through `_scheduler.` / `_chain.`, and every name a test patches or reads on `scheduler` / `chain`, resolves on the facade.
- **F2.** Delete the programmatic installers and the self-check tables:
  - the three compat modules;
  - the `globals().update` sites at `chain.py:147-149,887-889` and `scheduler.py:230-234,686,688-690`;
  - `scheduler_candidate_runtime.EXPORTS`;
  - the facade-local `_COMPAT_` tables at `chain.py:~185-204` and `scheduler.py:238-292`.

  Each facade gets explicit alias lines for every name the F1 guard requires.

  Any patch-propagation behaviour in `scheduler_state_compat.py:169-219` is kept as an explicit function in an owner module. It is late binding, not bookkeeping. The ~370 test assertions that only read `_COMPAT_` tables are deleted. Tests that monkeypatch through the facades stay unchanged. `FIRST_IMPORT_ALLOWLIST` loses the deleted modules.

  F2 is split into chain-side and scheduler-side PRs; neither facade imports the other's compat modules.
- **F3.** Delete the inventories and guards:
  - both compatibility inventories;
  - `facade_guard.py`, `COMPATIBILITY_FACADE_CONFIGS` and the audit helpers that use them;
  - the facade parts of `tests/test_entropy_audit_facade_guard_{forwarders,aliases}.py` and `tests/test_entropy_audit_instruction_inventory.py`;
  - the `compatibility facade` glossary term (`schema.py:227`, `openspec/glossary.md:17`);
  - the governance docs that cite them: `services/orchestrator/AGENTS.md`, `entropy-budget.md`, `entropy-burndown-triage.md:247`, `STRUCTURAL_FILE_DISPOSITION_INVENTORY.md:163-164,229-230`, `GOVERNANCE_8_FINAL_EVIDENCE_MAP.md:18-19`.

**D4. Journal split by mixins, last, behind a characterization guard.**

- **J0** pins four sets for the class and module:
  1. Every method name resolvable on `FileOrchestrationJournalRepository`, with its `inspect.signature`.
  2. The attribution-wrapped public methods.
  3. The private methods called by the four known external modules: `journal_scope_census.py`, `file_orchestration_migration.py`, `scheduler_state_manual_retry.py` and `scripts/ops/node22_repair_placeholder_hydro_uris.py`.
  4. Every public module-level name importable from `services.orchestrator.file_orchestration_journal`.

  J0 also extends the cluster-map late-lookup lint to cover the new mixin modules.
- **J1–J5** move clusters into `file_journal_<cluster>.py` mixins. Suggested order below; the only real dependency is C13 after C14 (C14 calls into C13). The others are independent once J0 is in:
  - C18 retention;
  - C17, then C15;
  - C12, then C14 (its receipt dataclasses stay importable from the facade), then C16;
  - C13;
  - C7.

  The class composes the mixins. A mixin with public methods carries `@_install_public_read_attribution`. Moved code reads patch points off the facade module at call time, and the cluster map is updated. J0 stays unchanged throughout.
- Mixins are used, not collaborator objects, because class-level patches and the test subclasses resolve through the MRO.
- The split buys navigability, not fewer fixes, which is why it comes last.

**D5. Verification cadence.** Risk-tiered, decided by the owner. The issue asked for node-22 idle and submitting passes after every deploy; the owner approved batching for pure moves, and this is recorded as an owner-approved deviation from the issue.

- **Test-only PRs** (R2a, T1, J0, and the test parts of R3 and F2): CI full suite.
- **Behaviour-changing or deleting PRs** (R1, R2b, R2c, R3, R4, R5, F2): CI, then each one gets its own node-22 idle pass and submitting pass after its deploy. A slice that changes code which node-27 integration-marked suites construct or import directly (CI's unit shards exclude `integration`) also runs those suites on node-27. Spec-only and test-only PRs (R1 spec triage, R2a, R3-1, R5-1, F2 test-only parts) are CI only.
  - R4 and R5 also produce the node-27 C2 receipt (display read routes and deny-write), because they touch `apps/api/readonly_validation_probe.py` / `persistence.py`.
  - R2b additionally proves with a test that a Slurm export env built with `NHMS_SCHEDULER_DB_FREE_REQUIRED` unset still carries `NHMS_CANONICAL_DB_FREE=true` and the file-backend switches.
- **Pure-move code PRs** (T2, F1, J1–J5): CI, plus node-27 real-DB suites when those suites import the code. They deploy to node-22 by ff at a pass boundary. Each batch gets one idle pass and one submitting pass, at most three merged PRs or one calendar day per batch.
- **F3** (governance tooling and docs): CI only.
- F1 and F2 PRs that add explicit `_`-prefixed facade imports also add Guard Hook Seed rows to the inventories, so the facade guard stays at zero signals until F3 deletes it.

**D6. One umbrella change.**
- This change is the fixture for every slice issue; each issue's task group is its scope.
- Spec deltas state the end state. Appendix D lists every main-spec passage a slice must rewrite, and the slice adds that delta in its own PR.
- The change is archived in the final push of whichever slice merges last.

## Sketch seams under test

- **`ProductionScheduler` built in DB-free mode and run for one pass against a temp journal root.** This is the existing highest seam (`tests/test_production_scheduler.py` fixtures), and every R and F slice keeps it green.
- **`retry_identity` public functions over plain data.** The T1 table drives them alone.
- **The facade name-resolution guard (F1).** It is the one seam that proves late binding still works after the installers go.
- **`FileOrchestrationJournalRepository`'s pinned surface (J0) plus `tests/test_file_orchestration_journal.py`.** An unchanged guard plus an unchanged suite prove each pure move.
- **The `nhms-pipeline` parser surface (`cli.py`).** R1 removes one subcommand; every other subcommand must parse as before.
- **The Slurm job export env built by the gateway (`real_backend.py`).** R2b proves the DB-free worker switches no longer depend on the scheduler's process env.

## Risks / Trade-offs

- **R2a has the largest test churn.** Most of the 1,203 tests in `tests/test_production_scheduler.py` build the scheduler without the flag. Mitigation: retarget file by file while the flag still selects, so each PR is green. R2b flips only after that.
- **A redeploy from new templates could drop the job-env DB-free contract.** This is closed by R2b writing `scheduler_db_free_required=true` into manifests and by keeping the variable in templates.
- **F2 deletes a name that an owner reads through the facade, or that a test patches.** The F1 guard is computed from the code, so a missing alias fails CI. `_utcnow`-style call-time reads are covered by the same guard.
- **Moved journal code loses read attribution or a late-lookup patch point.** J0 catches this, together with the extended cluster-map lint and `tests/test_file_journal_late_lookup_seams.py`.
- **The programme is long, and slices can drift apart.** Every slice issue names its dependencies, Appendix C gives each symbol one owner, and the epic tracks order.

## Not yet specified

None. The analysis-lane triage that used to be here is task 1.6.

## Open Questions

None. The `scheduler_state_compat.py` question is answered in D3: its tables go, and its patch propagation is kept as an owner function. The `PipelineStore` question is answered in D1/R5: test-only callers do not count, and fixtures switch to ORM inserts.

## Appendix A: retry characterization rows (input to T1)

"Pin" names the existing integration test that already covers the row. T1 restates each row as plain data.

| Row | Input | Expected today | Existing pin |
|---|---|---|---|
| A | #2404: the failed row is under `..._full_<m>` at suffix 1, and the stage is now minted under `..._forecast_<m>` | The mint is floor+1, the suffix does not restart, and limit 3 blocks on time | `tests/test_retry_mint_floor.py::test_prefix_switch_does_not_reset_the_minted_attempt_and_the_budget_blocks_on_time`, `::test_failure_lane_prefix_switch_stays_within_the_limit` |
| B | #2404: the cohort digest changes | There is no escape from the budget | `::test_cohort_digest_changes_do_not_escape_the_budget`, `::test_failure_lane_cohort_digest_change_stays_within_the_limit` |
| C | #2404: sibling-model retry rows exist | They do not raise this model's floor | `::test_sibling_model_retry_rows_do_not_raise_this_models_floor` |
| D | #2404: an inline auto retry on a floored id | `_retry_N_retry_M` is never under-minted | `::test_inline_auto_retry_stacks_on_the_floored_id_and_never_under_mints` |
| E | #2404/#1201: the floor-derived id is already in the journal | The mint skips forward and never reuses an id | `::test_floor_derived_id_already_in_the_journal_keeps_the_minter_advancing` |
| F | #2542: cohort members have different floors | The master mints past the max floor, and each member is charged its own floor | `::test_mixed_floor_cohort_mints_past_every_member_and_charges_each_member_its_own_floor`, `tests/test_cohort_member_charge.py` |
| G | #1845: a sibling model-set's terminal forcing row is under the shared base id | It is not resumed | `tests/test_chain_cross_stage_state.py::test_sibling_model_set_terminal_forcing_row_under_shared_base_id_is_not_resumed` |
| H | #1845: this model-set's own terminal forcing row | It is resumed verbatim | `::test_own_model_set_terminal_forcing_row_is_resumed_verbatim` |
| I | #1845: re-entry after a nested partial retry | The mint is past the excluded subset row | `::test_reentry_after_nested_partial_retry_mints_past_the_excluded_subset_row` |
| J | #1845: same as I, with the budget spent | A durable decline | `::test_reentry_after_nested_partial_retry_with_spent_budget_declines_durably` |
| K | #1845: a concurrent pass after a fresh submit | The suffix does not advance | `::test_concurrent_retry_reservation_after_fresh_submit_does_not_advance_the_suffix` |
| L | #2394/#2416: cohort restart stage | Markered members take the earliest stage, markerless members are ignored, and an ordinary restart is kept | `::test_restart_stage_from_basins_takes_earliest_marker_and_ignores_markerless_members` and siblings |
| M | #2254: stacked `..._retry_1_retry_2`, or a malformed tail | The attempt is the last suffix and the next is 3; a malformed tail counts as 0 | `tests/test_orchestration_chain.py::test_stacked_retry_ids_mint_next_attempt_past_last_suffix_through_orchestrate_cycle`, `::test_next_retry_attempt_flat_suffixes_and_malformed_tail_are_unchanged` |
| N | #2393: an explicit `context.retry_attempt` | It takes precedence over stacked rows | `::test_explicit_context_retry_attempt_keeps_precedence_over_stacked_rows` |
| O | #1201: a manual attempt claim with no active manual decision | The claim is ignored | introduced by `e9aabef8a`; T1 names the test |
| P | #1356: retry identity from the selected-jobs snapshot | It derives from the snapshot only | introduced by `1255444bb`; T1 names the test |
| Q | `retry_count` is 0 on a master and the suffix is N | The attempt is N | `::test_forecast_retry_gate_honors_job_id_suffix_when_recorded_count_is_absent`, `::test_forecast_retry_gate_reads_the_last_stacked_retry_suffix` |

## Appendix B: journal repository clusters (input to J0–J5; class 1304-12266, 207 methods)

Line ranges are in `services/orchestrator/file_orchestration_journal.py` at 54556b1e7. Each slice re-reads them, because earlier slices shift lines.

| Cluster | Responsibility | Lines | Moves in |
|---|---|---|---|
| C1 | raw IO and byte cache | 7759-7935 | core (stays) |
| C2 | locking | 12043-12164 | core |
| C3 | segment and path IO | 12165-12268 | core |
| C4 | append, validate, sequence, materialize | 11189-11785 | core |
| C5 | cycle-rows read model | 7135-7758 | core |
| C6 | record iteration and replay | 7936-8551 | core |
| C7 | reconcile inventory (consumer of the core, not part of it) | 8552-10356, 11097 | J5 |
| C8 | context and job-row building | 10357-10656 | core |
| C9 | job write core | 10657-11188 | core |
| C10 | public read queries | 1453-2676 | core |
| C11 | basic writes | 2677-3226 | core |
| C12 | reservation reclaim | 3227-3474 | J3 |
| C13 | submit lifecycle and `permit_*_retry` | 3475-4903 | J4 |
| C14 | operator demote/bind | 4904-5829 | J3 |
| C15 | identity-blocked release/recover | 5830-6183 | J2 |
| C16 | forecast-cohort projection | 6184-6843 | J3 |
| C17 | status, events, stage statuses | 6844-7134 | J2 |
| C18 | retention | 1363-1452, 11786-12042 | J1 |

## Appendix C: one deleting slice per symbol (SQL path)

Line numbers are at 6bf97fdc8. Each slice re-greps before deleting. An inventory/compat binding is removed in the slice that deletes the symbol.

| Symbol or site | Deleting slice |
|---|---|
| `trigger-analysis` (`cli.py:16,73-86,507-512,802,879-881`), `AnalysisOrchestrator` (`chain_analysis_orchestrator.py`), `chain_analysis.py`, `build_analysis_run_manifest` (`chain_manifests.py:762`), `AnalysisPipelineAlreadyActiveError` (`chain.py:262`), analysis-only branches (`chain_forecast_execution.py:1687`, `ANALYSIS_STAGES`/`AnalysisRunContext` uses), compat bindings (`chain_compat_runtime.py:75-95,504`), `tests/test_analysis_pipeline.py`, writer-receipt case (`tests/test_gateway_reconcile_writer_receipts.py:134`) | R1 |
| Shared test helpers get an opt-in DB-free form (`tests/test_production_scheduler.py:729` subclass, `:37378` `_config`); tests whose subject is the non-DB-free default path are deleted (e.g. `:25534` with its `:25590,:25616` assertions, `:26142`, `:26200`); test construction of the non-DB-free scheduler, the `_default_adapters` patches (~35), and the SQL-factory string patches (`tests/test_production_scheduler.py`, `tests/test_file_orchestration_journal.py:6012-6175`). Excluded: the DB-free reachability guards (`pytest.fail` patches, e.g. `:40702-40770`, `:45483-45516`), rewritten by the slice that deletes the guarded symbol | R2a |
| Mode constant, fail-closed arms, `chain_manifests.py:252`, `scheduler_candidate_runtime.py:578` | R2b |
| `_default_adapters` (`scheduler_adapters.py:180`, re-export `scheduler.py:54`); `PsycopgModelRegistryStore.from_env` arm (`scheduler_core.py:127`); repository arm (`:214-226`); `StateManager.from_env` arm (`:455-458`); retry-service arm (`:525-535`); `PsycopgStateSnapshotRepository.from_env` arm (`:736-745`); `_canonical_readiness_provider_from_env` / `_forcing_producer_from_env` (`scheduler_adapters.py:405-430`, re-exports `scheduler.py:52,55`); SQL reconcile store with reset/dispose (`scheduler_runtime.py` `_restart_reconcile_store` and the code before it); PostgreSQL lease (`scheduler_core.py:241-250`, its `scheduler_lease.py` branch); `tests/test_gateway_reconcile_store_reset.py`; `tests/test_reconcile_sacct_parse.py:101-115`; `tests/test_qhh_production_bootstrap_scheduler.py` (kept (R2c-1): explicit PG-registry injection, independent of the deleted arms); `PostgresSchedulerLease` / `_postgres_advisory_lock_key` tests | R2c |
| `PsycopgOrchestratorRepository` (`chain_repository.py:41-1003`), `_active_repository_from_env` / `_orchestrator_repository_from_env` (`scheduler_adapters.py:433-442`, re-exports `scheduler.py:51,57`), `ForecastOrchestrator.from_env` (`chain_forecast_orchestrator_cycle.py:129-135`), exports (`__init__.py:25,36`), compat bindings (`chain_compat_runtime.py:134-137,189,204,505`; `chain_compat_static.py:254-257,316-332`), `test_psycopg_*` SQL-text tests (`tests/test_orchestration_chain.py` 13 `CapturingRepository` subclasses, `:8784`, `:11994`, facade asserts `:926,:2382-2396,:12822-12926`, imports `:37,:47`), `tests/test_real_database_integration.py:339-502` (confirmed on node-27), `tests/test_gateway_reconcile_reservation_lifecycle.py:280`, `tests/test_chain_repository_nfs_raw_manifest.py`, `tests/test_orchestrator_demote_reclaim_lifecycle.py:596-604`, `tests/test_production_scheduler.py:40763,43475,45512`, path string `tests/test_node27_connection_attribution_delegated.py:147` | R3 |
| `RetryService` (`retry.py:251-402`, and the import at `:13`), `get_retry_service` (`pipeline.py:128-132`) with its override (`readonly_validation_probe.py:54`), `_retry_service_from_env` (`chain_compat_runtime.py:212-227`) with its callers (`scheduler.py:35`, `chain_forecast_orchestrator_cycle.py:131`), aliases (`chain.py:96`, `chain_forecast_execution.py:105`, `chain_compat_static.py:338,378`, `chain_compat_runtime.py:224,561`), `isinstance` branch (`chain_forecast_execution.py:827`), annotation (`chain_forecast_orchestrator_cycle.py:109`), the `RetryService`-class tests in `tests/test_retry.py`, `tests/test_chain_cross_stage_state.py:244-249`, the `_retry_service_from_env` reachability guards (`tests/test_production_scheduler.py:40750,44700,45508`), and the dependency references in `tests/test_retry_route_fail_closed.py`, `tests/test_retry_cancel_consistency.py`, `tests/test_monitoring_api.py`, `tests/test_slurm_gateway_openapi_security.py`, `tests/test_e2e_m3.py` | R4 |
| `PipelineStore.create_job`, `reserve_job`, `reclaim_reservation`, `bind_reservation`; fixtures seeding through them (`tests/gateway_reconcile_helpers.py:63,80`, `tests/test_pipeline_persistence.py:13-95`, `tests/test_chain_cross_stage_state.py:415,620`, `tests/test_gateway_reconcile_inflight_identity.py:32`) switch to ORM inserts | R5 |

## Appendix D: main-spec passages each slice rewrites

Each listed passage gets a MODIFIED or REMOVED delta in this change, added in the slice's PR. Line numbers are at 6bf97fdc8.

| Spec passage | Slice |
|---|---|
| `analysis-run-pipeline` remaining requirements (`:25-86`) after triage (task 1.6) | R1 |
| `node22-dbfree-runtime` (already in this change); `slurm-array-runner-integration/spec.md` requirements "Configuration construction survives symlink loops in containment bases and parent segments" (`:71-135`, incl. the `:103` db-backed configuration arm, which is the scheduler mode) and "...survive an undeterminable home directory on both database arms" (`:137-159`) | R2b |
| `pipeline-job-persistence/spec.md:10,12-14,42-44,83` ("PostgreSQL-backed scheduler mode"); `file-orchestration-journal/spec.md:309,650` (non-DB-free reconciliation) | R2c |
| `file-orchestration-journal/spec.md:4-5` (Purpose), `:13`, `:48`, `:154`; `production-scheduler-orchestration/spec.md:729,759-761,772` (SQL probes); `job-retry-mechanism/spec.md:350,352,377,385-393` (DB-backed candidate-state projection) | R3 |
| `file-orchestration-journal/spec.md:107-115` (`_retry_service_from_env`); `real-slurm-gateway-contract/spec.md:66,72,78` ("RetryService MUST ..."); `job-retry-mechanism/spec.md:1044` (markers written by the SQL retry service) | R4 |
| Chain side: `chain-facade-deepening` (already in this change); `import-layering-guard/spec.md:23` | F2 chain |
| Scheduler side: `scheduler-facade-deepening` (already in this change); `scheduler-discovery-evidence-owner-compat/spec.md:31-40` | F2 scheduler |
| `compatibility-facade-governance` (whole capability: REMOVED); `facade-shrink/spec.md:22-33` ("Compatibility guard remains authoritative"); `orchestrator-structural-burndown/spec.md:434-436` (mentions the compatibility inventories) | F3 |
