## Context
Change surface: shared MVT cache/producer boundary, all six routes, prewarm envelope, overload response, env/runbook, role settings.
Cold baseline: docs/runbooks/receipts/2026-09-17-issue2121-cold-prewarm.md; EXPLAIN raw /home/nwm/tmp/issue2121-cold-20260917/explain-gfs-z4.json.
Must preserve: URL validation before SQL, source/cycle identity and coverage checks, cache keys/bytes/ETags, PNG classifications, read-only boundaries, all existing 422/424/413 failures and B's issued-request accounting.
Governing invariant: at most C cold participants per process, C strictly below effective finite pool capacity; neither overload nor single-flight waiting holds a DB connection; admitted generation releases its DB checkout before releasing its permit.
Sibling surfaces: six MVT entrypoints, digest reads, DB/file cache probes, local/flock single-flight wait, producer, cache writes, exception cleanup, HTTP envelope/OpenAPI, cron prewarm, retention, catalog warm loop.

## Goals / Non-Goals
Goals: hot tiles and catalog stay available when cold generation saturates; controlled 503 rather than waiting for QueuePool timeout; full default-cycle prewarm; one coordinated deployment.
Non-goals: SQL optimization, separate engines, digest TTL caching, frontend throttling, widening discharge zooms, cache purges, cancellation/retry framework.

## Decisions
Use a process-wide concurrency gate at the common MVT boundary, after a first cache probe but before single-flight waiting. Nonblocking admission avoids parking unbounded request threads.
Release the read-only Session transaction before admission and lock waiting. Source identity is materialized as primitive dict/TileInput fields, not ORM objects; do not alter its value or recompute under an unrelated cache key.
Admitted work still rechecks cache under the existing lock, then generates if absent. Release any renewed transaction before permit release in finally; success and every exception path are covered.
One gate per worker, safely initialized under concurrent first requests; its size derives from the same effective bounded pool configuration as the engine. Default cold ceiling is half capacity, configurable via NHMS_DISPLAY_MVT_COLD_LIMIT, always clamped below capacity. Capacity 1 admits zero cold work rather than stealing the sole reserved slot. Production cap 8 with capacity 16.
Busy response: ApiError-compatible envelope code MVT_COLD_GENERATION_BUSY, status 503, Retry-After: 1, Cache-Control: no-store. Reuse the existing typed error transport; if adding internal response headers support, canonical request-id must not be overrideable and existing callers remain unchanged.
No digest TTL: dropping freshness for warm speed is unnecessary with reserved connections and risks stale identity.
Prewarm discovers the same newest source cycle and uses every published time in the original discovery order; remove only the fixed 12h truncation. Do not add normalization, sorting or deduplication: the existing select_lead_window preserves supplied strings/order/multiplicity (scripts/node27_mvt_prewarm.py:259-262). Preserve source-interleaved ordering, deadline=540, timeout=30, workers=8 and fixed discharge z3/z4. Do not retry 503 or hide incomplete warming.
SUMMARY_SCHEMA becomes v4; remove lead_hours and publish prewarm_scope=full_cycle. Existing per_source available/warmed and ok/failed counters remain truthful; deadline-skipped stays separate. Historical v2/v3 receipts are not rewritten.
Role changes are deployment-only: statement_timeout 30s bounds stuck queries; parallel ceiling 2 bounds future parallel plans (measured GFS plan was serial). Restore the exact previous role settings on rollback, not guessed defaults.

## Risks / Trade-offs
Full-cycle request count/cost is larger; only a standalone full-cycle prewarm in a fresh empty namespace, with no discharge failures/deadline skips, proves cold-budget fit. Contention uses a second fresh namespace; its later recovery fill cannot substitute for cold performance. Current 56+56 time fixture plans 1611 warm requests; this is not a runtime constant or guarantee.
The cold baseline had no real users in its isolated workers. Joint oracle must mix user burst and prewarm within the same tested worker set, not separate APIs.
Semaphore-only-around-producer leaves digest/lock holders checked out; the lifecycle regression must exercise real QueuePool checkouts, not fake counter echoes.
The regression traverses a complete public route with real pre-gate identity checkout, because the generation gate alone does not bound every identity SQL checkout. Live logs are inspected for QueuePool errors separately from HTTP outcomes; PostgreSQL activity samples are not treated as exact in-process pool checkout counts.
Fast-path identity reads still briefly acquire connections. The guarantee excludes arbitrary unrelated DB monopolization, but includes cold saturation and duplicate-key waiters caused by this service.
503 is visible backpressure, not failure concealment: count accepted and rejected cold requests, require useful cold successes, hot/catalog 200, and post-prewarm full-axis hits.

## Migration Plan
One PR and deployment unit for A+#2346, after committed baseline. Validate candidate on node-27 isolated namespace, then clean ff-only production sync with recorded SHA/env/role backup, same-window config/restart, C1-C4 evidence, and restore exact prior state if gates fail.
No DB schema migration. Capture max_connections/reserve, other demand and headroom at deployment; configured potential display connections (not idle snapshot alone) govern admission.
Runbook documents cap/pool/worker multiplication, role parameters, full-cycle prewarm, retention ages, 503 policy and rollback.
Review focus: resource ownership around every wait/exception, public envelope/OpenAPI consistency, full-axis request completeness, live evidence against real same-worker contention.
