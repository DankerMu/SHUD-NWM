# Design: retire-test-only-legacy-helpers (#3148)

Change surface: the rows of #3148. Each row names a symbol and its location in
`services/orchestrator/{persistence,retry,reservation,file_orchestration_journal,reconcile,scheduler_core}.py`,
`packages/common/{forecast_store,grid_registry_store,manifest_index,best_available,state_manager,safe_fs,openapi_auth_security,forcing_domain_handoff,node27_pgdata_evidence}.py`,
`workers/{canonical_converter/converter,forcing_producer/producer,mapping_builder/binding}.py` or
`scripts/node27_timeseries_compression.py`. The change also covers the tests whose only subject is a retired symbol.

Must preserve:
- Every production call path and every live import of these modules.
- Protocol and ABC conformance. A method that a `Protocol` or `ABC`, or a store or repository interface, declares
  stays on every implementation that is required to conform.
- Governed compatibility surfaces stay. Examples:
  - `chain_compat_static.py` `_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_*_METHOD_NAMES`, which includes
    `query_pipeline_job_by_slurm_id`;
  - `docs/governance/*COMPATIBILITY_INVENTORY*.md`;
  - the facade guard (`scripts/governance/entropy_audit/facade_guard.py`).

  A listed name is kept, unless its removal condition is met and the inventory or guard list is updated in the same
  commit, with the guard test still green.
- Living-spec obligations. A symbol named by a Requirement in `openspec/specs/**` is kept. Examples:
  - `PsycopgForecastStore.station_series` (met-station-series-api, forcing-narrow-store);
  - `query_released_identity_blocked_jobs` and `record_pipeline_job_reconciliation`
    (production-scheduler-orchestration);
  - `state_index_evidence` (file-state-snapshot-index).

  Such a symbol may be retired only if this change also carries a MODIFIED or REMOVED delta for that capability.
- Shared test harnesses that drive live protocols. A symbol that a shared test helper or fake uses to drive live
  production code is kept, or its implementation is moved verbatim into the test helper, and that move is recorded as
  a deviation. Examples:
  - `PipelineStore.reserve_job` and `reclaim_reservation`, behind `tests/gateway_reconcile_helpers.py` `_StoreRepo`;
  - the Barrier-based concurrency tests of `services/orchestrator/reservation.py` `reserve()`.

  No live concurrency or idempotency coverage may be lost.
- Coverage of live behaviour. A test is deleted only when every assertion in it targets a retired symbol. A test that
  also covers live behaviour is narrowed instead of deleted, and the narrowing is listed as a deviation.

Must add/change: deletions only, plus an evidence table in the PR.
The table columns are: symbol | production refs | test refs | protocol/compat listing | verdict | tests removed or
narrowed.

Governing invariant: after the change, no production-reachable behaviour, interface conformance or governed
compatibility name is lost, and no retained symbol is referenced only by its own tests without a recorded keep
reason.

Sibling surfaces:
- the dynamic-reach checks from #3147: `getattr`/`importlib`, owner callbacks, `monkeypatch` strings, `mock.patch`
  strings, `pyproject`, sbatch, systemd, CI;
- store protocol conformance tests;
- `apps/api/routes/pipeline.py`, which imports `persistence` and `retry`;
- `scripts/node22_manual_retry_failed_runs.py`, an operator tool that imports `retry`;
- the selector tables, which may name deleted test files.

Seams under test: an import smoke; the protocol and compatibility guard tests
(`rg -l "chain_compat_static|COMPAT_.*METHOD_NAMES|Protocol" tests`); the suites of the touched modules; the facade
guard node ids from #3147.

Required evidence: see `tasks.md` §2.

Non-goals: see `proposal.md`.

Review focus:
- the keep/retire verdict per row, against protocols and compatibility lists;
- tests deleted versus narrowed, so that no live coverage is lost;
- the operator script and API route imports;
- the selector tables.
