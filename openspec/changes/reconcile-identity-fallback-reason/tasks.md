Fixture level: expanded
Repair intensity: high
Project profile: NHMS
Upstream suggested level: absent
Issues: #1795, #1792, #1565
Minimal mergeable slice: one accepted-submit reconciliation boundary; #1565 depends on #1792 because fallback candidates pass through the shared runtime validator.

## 1. Runtime identity (#1792)

- [x] 1.1 Remove `submission_attempt` from `forecast_cohort_runtime_identity_matches` while preserving the field as immutable lineage evidence.
- [x] 1.2 Replace the old “attempt mismatch bites” test with a reclaim-shaped attempt-2 master / attempt-1 successful `hydro_run` regression that passes; keep strict stable-field and stale-array-layout regressions.

## 2. Comment-less unique fallback (#1565)

- [x] 2.1 Make comment capability tri-state: explicit present-without-`job_comment` enables fallback; probe failure and missing config line remain query-free `query_unavailable` / `comment_accounting_unproven`, with distinct warnings.
- [x] 2.2 Add one bounded attempt-window `nhms_forecast` accounting query: host-local bound rendering; shared byte/row/whole-query timeout budget; host-local-to-UTC Submit parsing; exact user/account and forecast family; bare-master deduplication; at most two masters.
- [x] 2.3 Bind only one fully validated candidate, allow an empty comment at both reserved identity gates, keep present-but-different fatal, and persist `slurm_name_window_unique` only on successful `matched_bound`.
- [x] 2.4 Pin unsuccessful pass evidence: zero -> `fallback_no_match`/0; ambiguity -> `ambiguous_fallback_match`/2; unique identity failure -> `identity_mismatch_blocked`/1; malformed Submit -> `query_unavailable`/`fallback_submit_unparsable`; no case binds, demotes, retries, or increments streak.
- [x] 2.5 Preserve or first establish only the #1564 durable held tuple on every unsuccessful fallback, and prove guarded operator demotion still accepts it.
- [x] 2.6 Prove exact-comment clusters, legacy/unversioned rows, unsupported contracts, visibility ordering, process/byte/row/time bounds, and exact-comment absence behavior are unchanged.

## 3. Terminal identity reason classes (#1795)

- [x] 3.1 Split the folded cohort-valid/runtime check and return one stable reason class from every terminal file-cohort identity failure site.
- [x] 3.2 Add optional `ReconcileOutcome.reconciliation_reason_class`, serialize it in `restart_reconcile.inflight.outcomes[]`, and preserve the original action, status, durable-write, pipeline-status, and pipeline-event values.
- [x] 3.3 Cover every reason token, the unchanged zero-write block, scheduler evidence serialization, and a genuine runtime mismatch distinct from live accounting/task failures.

## 4. Operations and evidence

- [x] 4.1 Update `docs/runbooks/failed-basin-retry.md`: automatic unique fallback, diagnostic outcomes, exact failure behavior, and the production-safe receipt rule.
- [x] 4.2 Run the focused gateway-reconcile suites, scheduler evidence tests, full `uv run pytest -q`, `uv run ruff check .`, and strict OpenSpec validation.
- [ ] 4.3 On node-22, use live read-only `scontrol`/`sacct` and a scratch journal to demonstrate one unique bind and one ambiguity refusal without `sbatch`, `scancel`, service changes, or production-journal writes; if no natural accounting rows exist, record the runbook-authorized no-fixture outcome.

## Risk packs considered (core)

- Public API / CLI / script entry: selected - scheduler reconcile and operator evidence are shared entry surfaces.
- Config / project setup: selected - `AccountingStoreFlags` capability classification controls fallback eligibility.
- File IO / path safety / overwrite: not selected - existing journal APIs own path safety; no new raw path operation.
- Schema / columns / units / field names: selected - accepted-submit source and evidence reason tokens are persisted/public contracts.
- Auth / permissions / secrets: selected - exact Slurm user/account ownership is a mandatory anti-cross-owner gate; no secrets are read.
- Concurrency / shared state / ordering: selected - attempt-scoped compare-and-swap and reserved-to-bound ordering must remain atomic.
- Resource limits / large input / discovery: selected - accounting output, rows, time, and distinct candidate count are bounded.
- Legacy compatibility / examples: selected - comment-storing, legacy, and unversioned paths must be byte-for-byte semantically unchanged.
- Error handling / rollback / partial outputs: selected - every unsuccessful fallback is zero-bind and preserves held durable authority.
- Release / packaging / dependency compatibility: not selected - no dependency or package surface changes.
- Documentation / migration notes: selected - production triage/runbook behavior changes.

## Domain risk packs considered

- Geospatial / CRS / basin geometry: not selected - no spatial data.
- Hydro-met time series / forcing windows: not selected - the window is Slurm submission time, not forcing time.
- SHUD numerical runtime / conservation / NaN: not selected - no solver or numerical behavior.
- PostGIS / TimescaleDB domain behavior: not selected - file journal only; no DB semantics.
- Slurm production lifecycle / mock-vs-real parity: selected - fallback consumes live accounting and binds accepted submissions.
- External hydro-met providers / snapshot reproducibility: not selected - no provider boundary.
- Run manifest / QC provenance: selected - cohort/runtime identity must stay bound to the accepted attempt without treating lineage as identity.
- Published NHMS artifacts / display identity: not selected - no publication/display surface.

## Invariant Matrix

- Governing invariant: a reserved cohort binds only one uniquely owned, attempt-window-bound Slurm master whose submission-stable identity matches; uncertainty remains fail-closed and names its failed clause.
- Source-of-truth identity/contract: current accepted-submit master, immutable attempt anchor, expected Slurm user/account, cohort members, and live accounting master/task ids.
- Producers: reservation/reclaim writers and frozen per-model `hydro_run` writers.
- Validators/preflight: comment capability probe, fallback parser/classifier, runtime identity validator, terminal cohort identity validator, `AcceptedSubmitTransition`.
- Storage/cache/query: file orchestration journal, bounded `sacct` subprocess/cache, accepted-submit transition tuple.
- Public routes/entrypoints: scheduler restart reconcile and `nhms-pipeline demote-reserved-job` compatibility boundary.
- Frontend/downstream consumers: scheduler evidence/no-progress consumers; no frontend behavior change.
- Failure paths/rollback/stale state: zero/ambiguous/foreign/malformed candidates, unknown capability, stale attempt snapshots, compare-and-swap loss.
- Evidence/audit/readiness: restart-reconcile reserved/inflight outcomes, focused/full pytest, strict OpenSpec, node-22 scratch receipt.
- Regression rows:
  - explicit no-comment + one owned in-window candidate + valid identity -> bind once with fallback source.
  - explicit no-comment + zero candidates -> `fallback_no_match`/0; two candidates -> `ambiguous_fallback_match`/2; wrong identity -> `identity_mismatch_blocked`/1; malformed Submit -> `query_unavailable`/`fallback_submit_unparsable`; all stay reserved/unbound with the held tuple preserved.
  - attempt-2 master + attempt-1 immutable successful runtime rows -> identity passes; stable-field mismatch still blocks.
  - each terminal identity failure site -> unchanged `identity_mismatch_blocked` plus its unique reason and zero durable writes.
  - comment-storing and unversioned sibling paths -> existing exact-comment/legacy behavior unchanged.

## Non-goals

- Automatic absence, retry permission, `squeue`, production held-row manufacture, DB/display changes, and out-of-scope identity-mismatch producers.

## Required evidence

- `uv run pytest -q tests/test_gateway_reconcile_file_cohort_identity.py tests/test_gateway_reconcile_file_cohort_comment.py tests/test_gateway_reconcile_comment_capability.py tests/test_gateway_reconcile_comment_accounting.py tests/test_gateway_reconcile_identity_invariants.py tests/test_gateway_reconcile_identity_release.py`: explicit inputs cover tri-state capability, local-time/Submit boundaries, byte/row/time failure, zero/unique/ambiguous candidates, both comment gates, ownership, source token, held tuple, stale attempt, and all terminal reason tokens.
- Focused `tests/test_production_scheduler.py` restart-reconcile evidence cases selected by exact node ids or `-k` expression.
- `uv run pytest -q`
- `uv run ruff check .`
- `openspec validate reconcile-identity-fallback-reason --strict --no-interactive`
- node-22 receipt per task 4.3; node-27 live DB/display receipt is not required because no DB/display semantics change.
