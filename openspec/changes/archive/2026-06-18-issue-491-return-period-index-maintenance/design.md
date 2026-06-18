## Context

Issue #491 follows #487-#490 in the return-period bloat remediation DAG. #487 made `flood.run_product_quality` an explicit run-level quality source, #488 stopped future no-curve empty-row writes, #489 moved API/tile readiness to the explicit quality contract, and #490 added audited no-curve row cleanup. The remaining problem is index bloat on `flood.return_period_result`, especially NULL-oriented partial indexes and overlapping query indexes on a TimescaleDB hypertable.

Fixture level: expanded. Repair intensity: high.
Project profile: NHMS.

Change surface:
- DB operations scripts or SQL generation helpers for index inventory, query-plan capture, and manual maintenance plans.
- `scripts/select_ci_tests.py` targeted-test mapping for the new operational surface.
- `docs/runbooks/current-production-ops.md` or adjacent DB operations runbook.
- Existing hot-path query surfaces remain compatibility targets: `apps/api/routes/flood_alerts.py`, `services/tiles/mvt.py`, and tile publisher readiness queries.

## Goals / Non-Goals

**Goals:**
- Produce repeatable audit SQL/reporting for root hypertable and chunk index sizes, `pg_stat_user_indexes` usage, core query plans, and pre/post relation-size evidence.
- Classify known return-period indexes by hot-path purpose and identify unsafe NULL partial indexes or redundant wide indexes for operator review.
- Generate guarded, manual SQL/runbook steps with `lock_timeout`, failure handling, and rollback/retry notes for maintenance-window execution.
- Preserve summary, ranking/segments, timeline, GeoJSON fallback tile, MVT selected identity, flood valid-time discovery, and latest-ready-run quality behavior.

**Non-Goals:**
- Do not automatically execute production DDL, `DROP INDEX`, `REINDEX`, `VACUUM FULL`, `pg_repack`, Timescale compression, or destructive maintenance from application code.
- Do not change worker write semantics, API response contracts, tile payload contracts, or frontend behavior.
- Do not perform the production maintenance run inside this PR.

## Decisions

1. Generate operator evidence and SQL instead of adding automatic migration DDL.
   - Rationale: Timescale hypertable index operations can lock chunks and have production-specific timing and replication impact.
   - Alternative rejected: add a normal migration that drops/rebuilds indexes automatically; this violates the issue risk warning.

2. Treat query-plan evidence as a first-class artifact.
   - Rationale: index removal is only safe if summary/ranking/timeline/MVT/valid-times plans are captured before and after or at least modeled on staging.
   - Alternative rejected: classify solely by index names; names are useful hints but insufficient for hot-path safety.

3. Keep #490 row cleanup separate from index/space recovery.
   - Rationale: DELETE reduces candidate rows but does not guarantee filesystem space release; index maintenance and table/chunk recovery have different lock and rollback properties.

## Risk Packs Considered

- Public API / CLI / script entry: selected - new operator entrypoint/reporting command must have stable inputs and failures.
- Config / project setup: selected - production DB URL, readonly vs writer connection, and maintenance-window execution are environment-sensitive.
- File IO / path safety / overwrite: selected - reports/plans are written to operator-selected paths and must avoid accidental overwrite or ambiguous output.
- Schema / columns / units / field names: selected - SQL references concrete PostgreSQL/Timescale catalog columns and flood table/index names.
- Auth / permissions / secrets: selected - tooling must not log credentials or require secrets in generated reports.
- Concurrency / shared state / ordering: selected - index DDL and vacuum/reindex steps have lock ordering, timeout, and retry semantics.
- Resource limits / large input / discovery: selected - catalog/chunk discovery must be scoped to `flood.return_period_result` and bounded report outputs.
- Legacy compatibility / examples: selected - existing API/tile query contracts and old migration names remain documented.
- Error handling / rollback / partial outputs: selected - partial report or failed SQL generation must be detectable and not presented as executable success.
- Release / packaging / dependency compatibility: selected - scripts must use repository dependencies and CI routing without new heavyweight runtime dependencies unless justified.
- Documentation / migration notes: selected - operator runbook is part of the deliverable.
- PostGIS / TimescaleDB domain behavior: selected - hypertable/chunk indexes and Timescale non-concurrent index constraints are central.
- Destructive production DDL / space recovery: selected - generated `DROP INDEX`, `REINDEX`, `VACUUM FULL`, `pg_repack`, chunk rebuild, or compression material must be manual, operator-gated, and paired with lock/rollback evidence; automatic execution is a non-goal.
- Published NHMS artifacts / display identity: not selected - no artifact publication or display identity writes change.
- Run manifest / QC provenance: not selected - no run manifests or QC evidence are modified.
- Geospatial / CRS / basin geometry: not selected - no geometry semantics change.
- Hydro-met time series / forcing windows: not selected - no forecast/forcing time-series semantics change.
- SHUD numerical runtime / conservation / NaN: not selected - no solver/runtime behavior change.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm job lifecycle behavior change.
- External hydro-met providers / snapshot reproducibility: not selected - no provider download/discovery behavior change.

## Invariant Matrix

Governing invariant: return-period index maintenance MUST be evidence-driven and operator-gated; generated guidance must not silently remove an index needed by a documented hot path or execute destructive production DDL.

Source-of-truth identity/contract: `flood.return_period_result` table/index names, core hot-path SQL surfaces, generated audit report, and generated manual maintenance SQL.

Surfaces:
- Producers: audit/plan script or SQL generator for index inventory, query-plan probes, and maintenance SQL.
- Validators/preflight: DB dialect/schema/table/index checks; output-path no-clobber checks; explicit dry-run/report mode.
- Storage/cache/query: PostgreSQL catalogs, Timescale chunk metadata, `pg_stat_user_indexes`, `pg_relation_size`, `pg_total_relation_size`.
- Public routes/entrypoints: operator CLI/script only; API route code remains a compatibility target.
- Frontend/downstream consumers: flood summary/ranking/timeline/MVT/valid-time consumers remain unchanged.
- Failure paths/rollback/stale state: missing Timescale metadata, insufficient DB privileges, partial report write, stale stats, lock-timeout failure.
- Evidence/audit/readiness: report includes index inventory, classification, query-plan probes, pre/post size SQL, manual execution checklist, and no-production-execution statement.

Regression rows:
- Audit against a mocked catalog with known NULL partial indexes -> report marks them as drop/investigate candidates without executing DDL.
- Audit with missing Timescale chunk metadata -> report still includes root-table evidence and records unavailable chunk evidence without aborting unrelated output.
- Maintenance SQL generation -> includes `lock_timeout`/transaction guidance and comments requiring maintenance-window approval.
- Readonly connection mode + audit command -> report/inventory/query-plan templates are allowed; manual maintenance SQL is generated only as file output and is not executed.
- Writer connection mode without explicit maintenance artifact request -> no destructive DDL is executed and output states operator approval is required.
- Summary endpoint hot path with `run_id`, optional `valid_time`, and usable flags -> generated probe covers warning-level counts and result segment/usable curve evidence without changing API response semantics.
- Ranking/segments hot path with `run_id`, optional `valid_time`, pagination, and usable flags -> generated probe covers count and ordered list SQL without removing required ordering/index identity.
- Timeline hot path with `run_id`, `river_network_version_id`, and `segment_id` -> generated probe covers segment timeline lookup and max-point behavior.
- GeoJSON fallback tile hot path with `run_id`, `duration`, `valid_time`, and bbox/limit filters -> generated probe covers bounded return-period result selection and preserves stable empty/error behavior.
- MVT selected-identity hot path with `run_id`, basin/network identity, `duration`, `max_over_window`, and `valid_time` -> generated probe covers tile source-row lookup and selected identity join.
- Valid-time discovery with `run_id`, basin/network identity, `duration`, and `max_over_window` -> generated probe covers descending valid-time discovery and truncation evidence.
- Latest-ready-run quality behavior -> generated probe confirms readiness is driven by `hydro.hydro_run` plus `flood.run_product_quality` and does not require scanning `return_period_result`.

## Risks / Trade-offs

- [Risk] Catalog queries differ between PostgreSQL/Timescale versions. -> Mitigation: isolate Timescale-specific probes and degrade to explicit unavailable evidence.
- [Risk] Operators mistake generated SQL for an automatically safe production action. -> Mitigation: generated SQL and runbook must require explicit maintenance-window approval and show lock/rollback notes.
- [Risk] Index classification overfits current query names. -> Mitigation: include query-plan probes and mark uncertain indexes as investigate rather than drop.
- [Risk] Long-running full test suites slow iteration. -> Mitigation: update targeted CI mapping to route DB ops script/docs tests to focused tests.

## Migration Plan

1. Land audit/report tooling, tests, and runbook only.
2. Run the audit in staging or production readonly where possible; capture report.
3. During an approved maintenance window, run generated SQL manually with writer credentials and lock timeout.
4. Capture post-maintenance size and query-plan evidence.
5. If any query plan regresses, stop further index changes and restore/recreate the affected index from the generated plan.

## Open Questions

- Exact production execution choice (`REINDEX`, `VACUUM FULL`, `pg_repack`, chunk rebuild, Timescale compression) depends on live audit output and maintenance-window constraints, so this PR must preserve it as an operator decision rather than hard-code one path.
