## 1. Joint implementation
- [x] 1.1 Implement shared cold admission and DB checkout release around admission/single-flight/generation across all six MVT routes.
- [x] 1.2 Preserve typed errors/request-id; expose 503 Retry-After/no-store and update OpenAPI/current generated consumers as required.
- [x] 1.3 Replace 12h prewarm truncation with complete default-cycle selection and v4 full_cycle summary; migrate affected tests/callers/docs without compatibility aliases.
- [x] 1.4 Add deterministic real-QueuePool saturation/duplicate-wait/cleanup regression and full-cycle regression; retain relevant single-flight and identity coverage.
- [x] 1.5 Update display.example and runbook with pool/cold cap/role settings, full-cycle footprint, admission, retention compatibility and joint rollback.
- [x] 1.6 Reconcile the still-active display-v2 umbrella prewarm spec and scope notes so its eventual archive cannot restore the obsolete lead window (parent-owned).

## 2. Verification and deployment
- [x] 2.1 Independent fixture review PASS and openspec validate isolate-display-cold-generation --strict --no-interactive.
- [ ] 2.2 On node-27, new behavioral regressions fail against pre-change source and pass after; targeted prewarm, MVT scaling, lock, API-error and OpenAPI drift suites pass.
- [ ] 2.3 Local ruff, strict OpenSpec and relevant OpenAPI/generated API-type checks pass.
- [ ] 2.4 Verify live retention/disk capacity, then measure standalone full-cycle cold prewarm in a fresh empty namespace: counts/time/cache growth, no discharge failures/deadline skips, fresh files not selected for deletion. A later contention-recovery fill is not cold-budget proof.
- [ ] 2.5 Node-27 same-worker live cold burst plus hot/catalog probes: positive cold successes, declared bounded 503 excess, hot/catalog 200, zero river/cache-hit/catalog 500s and zero QueuePool timeout; capture per-request cold max/p95 and actual workload.
- [ ] 2.6 Node-27 complete-axis 4x playback after prewarm: actual browser/proxy-to-candidate or equivalent public HTTP traffic with explicit scope, predominantly cache hits, zero fast-surface 500s; record what was actually exercised.
- [ ] 2.7 In the same deployment window apply code/cold cap and display-role timeout/parallel ceiling, with before/after capacity, role settings, EXPLAIN and production C1-C4 live receipt; preserve exact rollback state.
- [ ] 2.8 Publish receipt with A keep/change decisions and pre/post failure/latency comparison, all #2346 body/comment acceptance items mapped; cross-review and CI must be clean before merge.

## Evidence Floor and oracle integrity
Baseline receipt already committed at af61cccc after B merge d3d09d9be: 183/183 misses, 18.771s, p95 2.494663s, max 3.124972s, no failures/skips. EXPLAIN GFS z4/12/6 at that cycle: 2032.815ms, no parallel nodes (session timeout 30s only; role unchanged). Full-axis costs and same-worker isolation are NOT inferred from that baseline.
Required deterministic failure is observable pool starvation/fast request failure on base, not merely AttributeError for a new helper. Use real SQLAlchemy checkout/release behavior with controlled producers, and real node-27 HTTP/PG for live evidence. No production DB writes except explicitly scoped role ALTER settings.
The saturation regression must traverse a public route including real identity/digest checkout before the shared cache helper. Full-cycle cold performance and same-worker contention use separate initially empty cache namespaces. Preserve partial samples on failures; inspect server logs for QueuePool errors rather than equating HTTP 500 counts with timeout counts; sampled PostgreSQL sessions are not labeled QueuePool checkout measurements.
Targeted command: uv run pytest tests/test_node27_mvt_prewarm.py tests/test_hydro_display_mvt_scaling.py tests/test_mvt_tile_generation_lock.py tests/test_api_errors_logging.py tests/test_openapi_drift.py tests/test_openapi_31_contract.py -q; include any new test file and API types checks changed by implementation. Remote TMPDIR=/home/nwm/tmp; inspect /,/home,/data/GHDC first.
Role/config deployment is authorized by this ordered issue request, not a reason to mutate before candidate tests. No node-22 operation.

## Risk packs
- Selected Public API / CLI / script entry: all six 503/header contracts and v4 prewarm CLI; 1.2, 1.3, 2.2/2.3.
- Selected Config / project setup: cold cap clamps against effective pool, env template, production role config; 1.1/1.5, 2.2/2.7.
- Selected File IO / path safety / overwrite: existing file cache/single-flight preserved, isolated namespace and retention dry-run; 2.2/2.4/2.5. Cache format/purge changes are non-goals.
- Selected Schema / columns / units / field names: OpenAPI 503 and prewarm v4, no DB schema migration; 1.2/1.3, 2.2/2.3.
- Selected Auth / permissions / secrets: canonical request-id survives error headers, display remains role-readonly/Slurm-disabled, no credentials in receipts; 2.2/2.7.
- Selected Concurrency / shared state / ordering: single per-worker gate, no DB checkout during wait, cleanup-before-permit-release, source-interleaved prewarm; 1.1/1.4, 2.2/2.5.
- Selected Resource limits / large input / discovery: finite cap, preserved deadlines, all published times, disk/connection/CPU evidence; 2.2/2.4/2.5/2.7.
- Selected Legacy compatibility / examples: URL/error/cache identity preserved, current schema consumers migrated, historical receipts retained; 1.2/1.3/1.5, 2.2/2.3.
- Selected Error handling / rollback / partial outputs: 503 versus original business errors, producer exceptions release state, explicit incomplete prewarm, exact joint rollback; 1.1/1.2/1.4, 2.2/2.7.
- Not selected Release / packaging / dependency compatibility: no new dependencies or packaging change.
- Selected Documentation / migration notes: current runbook + env example + durable live receipts; 1.5, 2.8.
