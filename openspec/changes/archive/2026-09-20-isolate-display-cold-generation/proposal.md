## Why
#2121-B merged as #2449 before the genuine cold receipt committed at af61cccc. That receipt proves the current 12h envelope completes in 18.771s (183 misses, no failures; max 3.125s), but does not solve #2346's real user cold-tile pool starvation. The owner requires #2121-A and #2346 step two to ship together.

## What Changes
- Bound MVT cold work per API worker while reserving pool capacity and releasing identity/cache-read connections before admission and single-flight waits; excess cold requests return typed 503 + Retry-After.
- Extend default-cycle prewarm to every published valid time for GFS/IFS at existing z3/z4, with existing deadline and error accounting. Summary v4 replaces misleading fixed lead_hours with prewarm_scope=full_cycle.
- Preserve workers=8, timeout=30s, deadline=540s, display pool=8+8 and uvicorn workers=2 based on receipt; set a production cold cap of 8/worker, strictly below its 16-connection ceiling.
- In one deployment window set display-role statement_timeout=30s and max_parallel_workers_per_gather=2, after the recorded GFS z4 EXPLAIN (2032.815ms, no parallel nodes). This is a resource ceiling, not a claim of query speedup.
- Record same-worker cold burst / warm playback and whole-axis prewarm evidence, retention compatibility, capacity and rollback.

## Capabilities
### New Capabilities
- `display-cold-admission`: bounded cold work with available warm/catalog capacity.
### Modified Capabilities
- `prewarm-source-outcomes`: v4 full-cycle contract, preserving B source accounting.

## Impact
apps/api/routes/hydro_display.py, existing typed error transport if required, services/tiles cache/session boundary only as necessary, scripts/node27_mvt_prewarm.py, existing tests, OpenAPI and generated consumers if affected, display env example, runbook, node-27 role configuration and live receipts. No frontend feature change, new engine, digest TTL, SQL rewrite, cache purge, retry framework or Slurm change.
Spec migration also updates the still-active display-v2 umbrella's `precipitation-raster-overlay` prewarm requirement and adds scoped supersession notes to its design/tasks. Its future archive must not reinstate the old lead window; historical I13 execution records remain unchanged.

Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (expanded: public API, concurrency and production config)
Blast radius: all six MVT routes, pool availability, prewarm and typed overload transport
Selected risk packs: CLI/API; Config; File IO; Schema; Auth; Concurrency; Resource limits; Compatibility; Error handling; Documentation
Evidence floor: deterministic real-pool regression red/green; existing targeted tests; OpenAPI drift; node-27 same-worker live cold/heat receipt and role/capacity/retention evidence.
